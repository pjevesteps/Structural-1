from pathlib import Path
import json
import math
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.loads import (
    beam_load_total_force_kn,
    compute_seismic_coefficients,
    compute_wind_kz,
    generate_loads,
)
from backend.app.main import app
from backend.app.models import (
    ProjectState,
    SeismicParameters,
    WindParameters,
)
from backend.app.staad import write_staad_preview


def _make_multistory_with_opening_payload(project_id: str = "P5-BLDG") -> dict:
    """
    4-story (5-level) 2x2 bay frame (6.0m x 5.0m + 4.0m x 4.0m bays -> mix of two-way & one-way)
    with a stairwell opening on 2F inside bay (0..6, 0..5).
    """
    cols = []
    cid = 1
    for gx_lbl, xm in [("1", 0.0), ("2", 6.0), ("3", 8.5)]:
        for gy_lbl, ym in [("A", 0.0), ("B", 5.0), ("C", 9.0)]:
            cols.append(
                {
                    "id": f"C{cid}",
                    "grid": f"{gy_lbl}-{gx_lbl}",
                    "x_m": xm,
                    "y_m": ym,
                    "width_mm": 400.0,
                    "depth_mm": 400.0,
                }
            )
            cid += 1

    return {
        "project_info": {
            "project_id": project_id,
            "project_name": f"Phase 5 Test ({project_id})",
            "location": "Baguio City",
            "design_code": "NSCP 2015",
            "materials": {
                "fc_mpa": 28.0,
                "fy_mpa": 415.0,
                "concrete_density_kn_m3": 23.54,
            },
        },
        "levels": [
            {"id": "B1", "elevation_m": -2.8},
            {"id": "1F", "elevation_m": 0.0},
            {"id": "2F", "elevation_m": 3.0},
            {"id": "3F", "elevation_m": 6.0},
            {"id": "ROOF", "elevation_m": 9.0},
        ],
        "columns": cols,
        "openings": [
            {
                "id": "OP-STAIR-2F",
                "label": "Stairwell",
                "category": "stairwell",
                "floor_level": "2F",
                "polygon_m": [[1.0, 1.0], [3.0, 1.0], [3.0, 3.0], [1.0, 3.0]],
                "area_sqm": 4.0,
            }
        ],
    }


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    projects_dir = tmp_path / "data" / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "DATA_DIR", projects_dir)
    client = TestClient(app)
    return client, tmp_path / "data"


def _setup_sized_project(client: TestClient, pid: str) -> dict:
    assert client.post("/api/projects", json=_make_multistory_with_opening_payload(pid)).status_code == 200
    assert (
        client.post(
            f"/api/projects/{pid}/geometry/generate",
            json={"default_support_type": "FIXED", "include_base_tie_beams": True},
        ).status_code
        == 200
    )
    assert client.post(f"/api/projects/{pid}/sizing/auto-size").status_code == 200
    return client.get(f"/api/projects/{pid}").json()


def test_nscp_seismic_coefficients_bounds_and_story_force_summation(isolated_env):
    """
    Verify NSCP 2015 Na/Nv/Ca/Cv interpolation, fundamental period T, base shear bounds
    (V_max, V_min, V_min_z4), and exact story force summation sum(Fx) == V_design_x.
    """
    # Exact check: Zone 4, Soil SD, Source A, 5 km -> Na=1.2, Nv=1.6, Ca=0.528, Cv=1.024
    z, na, nv, ca, cv = compute_seismic_coefficients(
        SeismicParameters(zone=4, soil_profile="SD", seismic_source_type="A", fault_distance_km=5.0)
    )
    assert z == pytest.approx(0.40)
    assert na == pytest.approx(1.20)
    assert nv == pytest.approx(1.60)
    assert ca == pytest.approx(0.44 * 1.2, abs=1e-4)
    assert cv == pytest.approx(0.64 * 1.6, abs=1e-4)

    # Mid-distance linear interpolation check: Source A at 3.5 km (halfway between 2 and 5)
    _, na_mid, nv_mid, _, _ = compute_seismic_coefficients(
        SeismicParameters(zone=4, soil_profile="SD", seismic_source_type="A", fault_distance_km=3.5)
    )
    assert na_mid == pytest.approx(1.35, abs=1e-4)
    assert nv_mid == pytest.approx(1.80, abs=1e-4)

    # Zone 2 sets Na = Nv = 1.0
    z2, na2, nv2, ca2, cv2 = compute_seismic_coefficients(
        SeismicParameters(zone=2, soil_profile="SD", seismic_source_type="A", fault_distance_km=2.0)
    )
    assert (z2, na2, nv2, ca2, cv2) == (0.20, 1.0, 1.0, 0.28, 0.40)

    client, _ = isolated_env
    pid = "P5-SEIS"

    # Pre-flight: missing Phase 3 / Phase 4 returns 400
    assert client.post("/api/projects", json=_make_multistory_with_opening_payload(pid)).status_code == 200
    pre = client.post(f"/api/projects/{pid}/loads/generate")
    assert pre.status_code == 400
    assert "module 03" in pre.json()["detail"].lower()

    assert client.post(f"/api/projects/{pid}/geometry/generate").status_code == 200
    pre2 = client.post(f"/api/projects/{pid}/loads/generate")
    assert pre2.status_code == 400
    assert "module 04" in pre2.json()["detail"].lower()

    assert client.post(f"/api/projects/{pid}/sizing/auto-size").status_code == 200
    gen = client.post(f"/api/projects/{pid}/loads/generate")
    assert gen.status_code == 200, gen.text
    lg = gen.json()
    ss = lg["seismic_summary"]

    # Height from B1 (-2.8m) to ROOF (9.0m) = 11.8 m
    assert ss["building_height_m"] == pytest.approx(11.8, abs=1e-3)
    expected_t = 0.0731 * (11.8 ** 0.75)
    assert ss["period_t_sec"] == pytest.approx(expected_t, abs=1e-3)
    assert ss["total_seismic_weight_kn"] > 0.0

    # Verify V_max, V_min, V_min_z4 bounds and governing V_design_x
    w = ss["total_seismic_weight_kn"]
    v_max = (2.5 * ss["ca"] * 1.0 / 8.5) * w
    v_min = 0.11 * ss["ca"] * 1.0 * w
    v_min_z4 = (0.8 * 0.40 * ss["nv"] * 1.0 / 8.5) * w
    v_calc = (ss["cv"] * 1.0 / (8.5 * ss["period_t_sec"])) * w
    v_expected = max(min(v_calc, v_max), v_min, v_min_z4)

    assert ss["v_max_kn"] == pytest.approx(v_max, rel=1e-3)
    assert ss["v_min_kn"] == pytest.approx(v_min, rel=1e-3)
    assert ss["v_min_z4_kn"] == pytest.approx(v_min_z4, rel=1e-3)
    assert ss["v_design_x_kn"] == pytest.approx(v_expected, rel=1e-3)

    # Story force summation == V_design_x and cumulative shear at lowest story == V_design_x
    sum_fx = sum(st["fx_kn"] for st in ss["stories"])
    assert sum_fx == pytest.approx(ss["v_design_x_kn"], rel=1e-3)
    assert ss["stories"][0]["cumulative_shear_x_kn"] == pytest.approx(ss["v_design_x_kn"], rel=1e-3)


def test_gravity_tributary_distribution_honors_openings_and_equilibrium(isolated_env):
    """
    Verify slab-to-beam tributary distribution (one-way & two-way) honors opening area
    deductions and matches total vertical reaction sum(w * L) == q * net_area_sqm within 1%.
    """
    client, _ = isolated_env
    pid = "P5-GRAV"
    _setup_sized_project(client, pid)

    gen = client.post(f"/api/projects/{pid}/loads/generate")
    assert gen.status_code == 200, gen.text

    proj = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    lg = proj.load_generation
    member_by_id = {m.member_id: m for m in proj.structural_model.members}
    slab_asgn_by_pid = {s.panel_id: s for s in proj.member_sizing.slab_assignments}
    fl_by_lvl = {fl.floor_level: fl for fl in lg.floor_loads}
    density = proj.member_sizing.material.density_kn_m3

    # Verify ROOF auto-seeded with is_roof=True and live_load_kpa=1.0
    assert fl_by_lvl["ROOF"].is_roof is True
    assert fl_by_lvl["ROOF"].live_load_kpa == pytest.approx(1.0)
    assert fl_by_lvl["ROOF"].sdl_partitions_kpa == pytest.approx(0.0)

    # Expected total vertical slab DL, SDL, LL, and LR from panel net areas (opening deducted!)
    exp_slab_dl = 0.0
    exp_slab_sdl = 0.0
    exp_slab_ll = 0.0
    exp_slab_lr = 0.0
    saw_opening_deduction = False
    for panel in proj.structural_model.slab_panels:
        if panel.slab_action == "void":
            continue
        fl = fl_by_lvl.get(panel.floor_level)
        if fl is None:
            continue
        if panel.opening_deduction_sqm > 0:
            saw_opening_deduction = True
        t_m = slab_asgn_by_pid[panel.panel_id].thickness_mm / 1000.0
        exp_slab_dl += panel.net_area_sqm * t_m * density
        sdl = fl.sdl_finish_kpa + fl.sdl_ceiling_mep_kpa + fl.sdl_partitions_kpa
        exp_slab_sdl += panel.net_area_sqm * sdl
        if fl.is_roof:
            exp_slab_lr += panel.net_area_sqm * fl.live_load_kpa
        else:
            exp_slab_ll += panel.net_area_sqm * fl.live_load_kpa

    assert saw_opening_deduction is True

    act_slab_dl = sum(
        beam_load_total_force_kn(b, member_by_id[b.member_id].length_m)
        for b in lg.beam_loads
        if b.source == "slab_dl"
    )
    act_slab_sdl = sum(
        beam_load_total_force_kn(b, member_by_id[b.member_id].length_m)
        for b in lg.beam_loads
        if b.source == "slab_sdl"
    )
    act_slab_ll = sum(
        beam_load_total_force_kn(b, member_by_id[b.member_id].length_m)
        for b in lg.beam_loads
        if b.source == "slab_ll"
    )
    act_slab_lr = sum(
        beam_load_total_force_kn(b, member_by_id[b.member_id].length_m)
        for b in lg.beam_loads
        if b.source == "slab_lr"
    )

    assert act_slab_dl == pytest.approx(exp_slab_dl, rel=0.01)
    assert act_slab_sdl == pytest.approx(exp_slab_sdl, rel=0.01)
    assert act_slab_ll == pytest.approx(exp_slab_ll, rel=0.01)
    assert act_slab_lr == pytest.approx(exp_slab_lr, rel=0.01)
    assert any(b.source == "ext_chb_wall" for b in lg.beam_loads)


def test_wind_scaling_v_squared_and_kzt_and_combo_vertical_ev(isolated_env):
    """
    Verify wind story forces scale strictly with V^2 and Kzt, and LRFD/ASD combinations
    include Ev = 0.5 * Ca * I when include_vertical_ev=True (and exclude when False).
    """
    client, _ = isolated_env
    pid = "P5-WIND"
    _setup_sized_project(client, pid)

    # Kz sanity check per NSCP 2015 Table 207B.3-1 at z <= 4.5 m
    assert compute_wind_kz(4.0, "B") == pytest.approx(0.57, abs=0.01)
    assert compute_wind_kz(4.0, "C") == pytest.approx(0.85, abs=0.01)
    assert compute_wind_kz(4.0, "D") == pytest.approx(1.03, abs=0.01)

    r1 = client.post(
        f"/api/projects/{pid}/loads/generate",
        json={
            "wind": {"basic_wind_speed_kph": 200.0, "exposure_category": "B", "topographic_kzt": 1.0},
            "seismic": {"include_vertical_ev": True},
        },
    )
    assert r1.status_code == 200, r1.text
    lg1 = r1.json()
    wx1 = lg1["wind_summary"]["base_shear_x_kn"]
    wz1 = lg1["wind_summary"]["base_shear_z_kn"]
    assert wx1 > 0.0 and wz1 > 0.0

    # Double Kzt -> exact 2x wind base shear
    r2 = client.patch(
        f"/api/projects/{pid}/loads/config",
        json={
            "wind": {"basic_wind_speed_kph": 200.0, "exposure_category": "B", "topographic_kzt": 1.8},
        },
    )
    assert r2.status_code == 200
    lg2 = r2.json()
    assert lg2["wind_summary"]["base_shear_x_kn"] == pytest.approx(wx1 * 1.8, rel=1e-3)
    assert lg2["wind_summary"]["base_shear_z_kn"] == pytest.approx(wz1 * 1.8, rel=1e-3)

    # Scale wind speed 200 -> 300 kph -> (300/200)^2 = 2.25x wind base shear
    r3 = client.patch(
        f"/api/projects/{pid}/loads/config",
        json={
            "wind": {"basic_wind_speed_kph": 300.0, "exposure_category": "B", "topographic_kzt": 1.0},
        },
    )
    assert r3.status_code == 200
    lg3 = r3.json()
    assert lg3["wind_summary"]["base_shear_x_kn"] == pytest.approx(wx1 * 2.25, rel=1e-3)
    assert lg3["wind_summary"]["base_shear_z_kn"] == pytest.approx(wz1 * 2.25, rel=1e-3)

    # Verify LRFD & ASD combos include Ev = 0.5 * Ca * I when include_vertical_ev=True
    ca = lg1["seismic_summary"]["ca"]
    ev = round(0.5 * ca * 1.0, 4)
    lrfd_ex_pos = next(
        c for c in lg1["combinations"] if c["design_basis"] == "LRFD" and "EX+" in c["factors"] and c["factors"]["DL"] > 1.0
    )
    lrfd_ex_neg = next(
        c for c in lg1["combinations"] if c["design_basis"] == "LRFD" and "EX+" in c["factors"] and c["factors"]["DL"] < 1.0
    )
    assert lrfd_ex_pos["factors"]["DL"] == pytest.approx(1.2 + ev, abs=1e-4)
    assert lrfd_ex_neg["factors"]["DL"] == pytest.approx(0.9 - ev, abs=1e-4)

    asd_ex_pos = next(
        c
        for c in lg1["combinations"]
        if c["design_basis"] == "ASD" and "EX+" in c["factors"] and "LL" not in c["factors"] and c["factors"]["DL"] > 0.8
    )
    assert asd_ex_pos["factors"]["DL"] == pytest.approx(1.0 + 0.7 * ev, abs=1e-4)

    # Disable Ev -> DL reverts to 1.2 / 0.9 (LRFD) and 1.0 (ASD)
    r4 = client.patch(
        f"/api/projects/{pid}/loads/config",
        json={"seismic": {"include_vertical_ev": False}},
    )
    assert r4.status_code == 200
    lg4 = r4.json()
    lrfd_no_ev = next(
        c for c in lg4["combinations"] if c["design_basis"] == "LRFD" and "EX+" in c["factors"] and c["factors"]["DL"] > 1.0
    )
    assert lrfd_no_ev["factors"]["DL"] == pytest.approx(1.2, abs=1e-4)


def test_full_staad_export_download_approval_and_legacy_json_compat(isolated_env, tmp_path: Path):
    """
    Verify full analysis-ready STAAD.Pro .std export order & syntax, /staad/download attachment,
    engineer approval gate, and backward compatibility with legacy project JSONs lacking load_generation.
    """
    client, _ = isolated_env
    pid = "P5-STD"
    _setup_sized_project(client, pid)

    # Add a stepped support at 1F so we can verify supported nodes are excluded from FLOOR DIAPHRAGM
    proj_before = client.get(f"/api/projects/{pid}").json()
    f1_node = next(n for n in proj_before["structural_model"]["nodes"] if n["floor_level"] == "1F")
    stepped_nid = f1_node["node_id"]
    sup_resp = client.patch(
        f"/api/projects/{pid}/geometry/nodes/{stepped_nid}/support",
        json={"support_type": "FIXED"},
    )
    assert sup_resp.status_code == 200, sup_resp.text

    gen = client.post(f"/api/projects/{pid}/loads/generate")
    assert gen.status_code == 200, gen.text

    # Approve loads
    appr = client.post(f"/api/projects/{pid}/loads/approve")
    assert appr.status_code == 200, appr.text
    assert appr.json()["engineer_approved"] is True

    # Full STAAD .std via /staad-preview and /staad/download
    prev = client.post(f"/api/projects/{pid}/staad-preview")
    assert prev.status_code == 200, prev.text
    body = prev.json()
    assert body["source"] == "structural_model_full_analysis"
    std = body["std_preview"]

    dl_resp = client.get(f"/api/projects/{pid}/staad/download")
    assert dl_resp.status_code == 200
    assert f'filename="{pid}.std"' in dl_resp.headers.get("content-disposition", "")
    assert dl_resp.text == std

    # Verify required STAAD.Pro blocks and strict Reference-Load -> Diaphragm -> UBC -> Primary ordering
    required_blocks = [
        "DEFINE REFERENCE LOADS",
        "LOAD R1 LOADTYPE Dead TITLE REF DEAD MASS (DL)",
        "SELFWEIGHT Y -1.0",
        "MEMBER LOAD",
        "END DEFINE REFERENCE LOADS",
        "FLOOR DIAPHRAGM",
        "DEFINE UBC LOAD",
        "LOAD 1 LOADTYPE Seismic TITLE SEISMIC +X (NSCP 2015)",
        "UBC LOAD X 1.0",
        "PERFORM ANALYSIS",
        "CHANGE",
        "LOAD 5 LOADTYPE Wind TITLE WIND +X (NSCP 2015)",
        "JOINT LOAD",
        "LOAD 9 LOADTYPE Dead TITLE DEAD LOAD (DL)",
        "REFERENCE LOAD",
        "R1 1.0",
        "LOAD COMB 101 1.4 DL",
        "PERFORM ANALYSIS PRINT STATICS CHECK",
        "FINISH",
    ]
    for blk in required_blocks:
        assert blk in std, f"Missing expected STAAD block: {blk}"

    # 1 & 2. DEFINE REFERENCE LOADS & END DEFINE REFERENCE LOADS strictly BEFORE FLOOR DIAPHRAGM
    assert std.index("SUPPORTS") < std.index("DEFINE REFERENCE LOADS")
    assert std.index("DEFINE REFERENCE LOADS") < std.index("LOAD R1 LOADTYPE Dead")
    assert std.index("END DEFINE REFERENCE LOADS") < std.index("FLOOR DIAPHRAGM")
    assert std.index("FLOOR DIAPHRAGM") < std.index("DEFINE UBC LOAD")
    assert std.index("DEFINE UBC LOAD") < std.index("LOAD 1 LOADTYPE Seismic")
    assert std.index("LOAD 1 LOADTYPE Seismic") < std.index("LOAD 9 LOADTYPE Dead")
    assert std.index("LOAD COMB 101") < std.index("PERFORM ANALYSIS PRINT STATICS CHECK")
    assert std.index("PERFORM ANALYSIS PRINT STATICS CHECK") < std.index("FINISH")

    # 3. Supported base & stepped 1F nodes are excluded from FLOOR DIAPHRAGM joint lists
    dia_block = std[std.index("FLOOR DIAPHRAGM") : std.index("DEFINE UBC LOAD")]
    proj_after = client.get(f"/api/projects/{pid}").json()
    supported_nids = {
        str(n["node_id"])
        for n in proj_after["structural_model"]["nodes"]
        if n["is_support"] or n["support_type"] != "NONE"
    }
    assert str(stepped_nid) in supported_nids
    assert "HEI 0.000 JOI" in dia_block
    # None of the supported B1/stepped nodes should appear as diaphragm joints
    dia_joint_tokens: set[str] = set()
    for line in dia_block.splitlines():
        if "JOI" in line:
            after_joi = line.split("JOI", 1)[1]
            dia_joint_tokens.update(tok for tok in after_joi.split() if tok.isdigit())
        elif line.strip() and line.strip()[0].isdigit():
            dia_joint_tokens.update(tok for tok in line.split() if tok.isdigit())
    assert str(stepped_nid) not in dia_joint_tokens
    assert dia_joint_tokens.isdisjoint(supported_nids)

    # 4. DEFINE UBC LOAD does NOT duplicate SELFWEIGHT / MEMBER WEIGHT when DEFINE REFERENCE LOADS is emitted
    ubc_block = std[std.index("DEFINE UBC LOAD") : std.index("LOAD 1 LOADTYPE Seismic")]
    assert "SELFWEIGHT" not in ubc_block
    assert "MEMBER WEIGHT" not in ubc_block

    # 5. LOAD 9 references R1 1.0 cleanly (and does not duplicate SELFWEIGHT)
    load9_block = std[std.index("LOAD 9 LOADTYPE Dead") : std.index("LOAD 10 LOADTYPE Live")]
    assert "REFERENCE LOAD" in load9_block
    assert "R1 1.0" in load9_block
    assert "SELFWEIGHT" not in load9_block

    # 6. Verify live_load_seismic_fraction > 0 adds scaled R2 inside DEFINE REFERENCE LOADS
    r_ll_mass = client.patch(
        f"/api/projects/{pid}/loads/config",
        json={"seismic": {"live_load_seismic_fraction": 0.25}},
    )
    assert r_ll_mass.status_code == 200
    std_ll = client.get(f"/api/projects/{pid}/staad/download").text
    ref_block_ll = std_ll[
        std_ll.index("DEFINE REFERENCE LOADS") : std_ll.index("END DEFINE REFERENCE LOADS")
    ]
    assert "LOAD R2 LOADTYPE Live TITLE REF LIVE MASS (LL)" in ref_block_ll

    # Verify explicit_story_forces + REPEAT LOAD option
    r_rep = client.patch(
        f"/api/projects/{pid}/loads/config",
        json={
            "seismic": {"staad_seismic_method": "explicit_story_forces"},
            "options": {"use_repeat_load_combos": True, "include_rigid_diaphragm": False},
        },
    )
    assert r_rep.status_code == 200
    std_rep = client.get(f"/api/projects/{pid}/staad/download").text
    assert "DEFINE UBC LOAD" not in std_rep
    assert "FLOOR DIAPHRAGM" not in std_rep
    assert "REPEAT LOAD" in std_rep
    assert "LOAD 101 LOADTYPE None TITLE 1.4 DL" in std_rep

    # Backward compatibility: legacy JSON files without load_generation still load cleanly
    for legacy_path in (
        ROOT / "data" / "projects" / "DEMO-001.json",
        ROOT / "frontend" / "public" / "sample_project.json",
    ):
        if legacy_path.exists():
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            raw.pop("load_generation", None)
            loaded = ProjectState.model_validate(raw)
            assert loaded.load_generation.generated_at is None
            assert loaded.load_generation.combinations == []
            # Exporting legacy project without Phase 5 loads preserves earlier behavior
            out_legacy = write_staad_preview(loaded, tmp_path / f"{legacy_path.stem}.std")
            assert "DEFINE UBC LOAD" not in out_legacy["std_preview"]
            assert "LOAD COMB" not in out_legacy["std_preview"]



def test_trap_load_distances_clamped_strictly_below_staad_member_length_and_print_tables(isolated_env):
    """
    Regression test for STAAD **WARNING** TRAP LOAD BEYOND MEMBER LENGTH:
    Uses non-integer fractional column coordinates (e.g., 3.3334 m, 4.1277 m) that previously
    rounded d2_m up past STAAD's 3-decimal JOINT COORDINATES Euclidean length, and verifies:
      1. Every TRAP line in the exported .std satisfies 0.0 <= d1 < d2 < L_actual.
      2. PRINT SUPPORT REACTIONS, PRINT JOINT DISPLACEMENTS, and PRINT MEMBER FORCES appear
         right after PERFORM ANALYSIS PRINT STATICS CHECK and before START CONCRETE DESIGN.
    """
    from backend.tests.test_e2e_pipeline import lint_staad_text

    client, _ = isolated_env
    pid = "P5-TRAP-CLAMP"
    cols = []
    cid = 1
    for gx, xm in [("A", 0.0), ("B", 3.3334), ("C", 7.4611)]:
        for gy, ym in [("1", 0.0), ("2", 3.7776), ("3", 7.9053)]:
            cols.append(
                {
                    "id": f"C{cid}",
                    "grid": f"{gx}-{gy}",
                    "x_m": xm,
                    "y_m": ym,
                    "width_mm": 400.0,
                    "depth_mm": 400.0,
                }
            )
            cid += 1

    payload = {
        "project_info": {
            "project_id": pid,
            "project_name": "Fractional Span TRAP Clamping Test",
            "location": "Baguio City",
            "design_code": "NSCP 2015",
            "materials": {"fc_mpa": 28.0, "fy_mpa": 415.0, "concrete_density_kn_m3": 23.54},
        },
        "levels": [
            {"id": "1F", "elevation_m": 0.0},
            {"id": "2F", "elevation_m": 3.0},
            {"id": "RD", "elevation_m": 6.0},
        ],
        "columns": cols,
    }
    assert client.post("/api/projects", json=payload).status_code == 200
    assert client.post(f"/api/projects/{pid}/geometry/generate").status_code == 200
    assert client.post(f"/api/projects/{pid}/sizing/auto-size").status_code == 200
    assert client.post(f"/api/projects/{pid}/loads/generate").status_code == 200

    std = client.get(f"/api/projects/{pid}/staad/download").text
    assert "TRAP GY" in std

    # Verify linter finds ZERO errors (including strict 0 <= d1 < d2 < L_actual on every TRAP line)
    assert lint_staad_text(std, require_ref_load_before_diaphragm=True) == []

    # Verify explicit post-analysis .anl table print commands for Module 06 bridge
    for cmd in (
        "PRINT SUPPORT REACTIONS",
        "PRINT JOINT DISPLACEMENTS",
        "PRINT MEMBER FORCES",
    ):
        assert cmd in std
        assert std.index("PERFORM ANALYSIS PRINT STATICS CHECK") < std.index(cmd)
        assert std.index(cmd) < std.index("START CONCRETE DESIGN")
