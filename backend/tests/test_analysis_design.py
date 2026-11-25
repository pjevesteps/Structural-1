import csv
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.design import (
    compute_story_drifts,
    parse_staad_anl,
    run_analysis_and_design,
    solve_frame_dsm,
)
from backend.app.main import app
from backend.app.models import DesignParameters, ProjectState, SectionDefinition


def _make_multistory_payload(project_id: str = "P6-FRAME") -> dict:
    """3-story (4-level) 2x2 bay RC frame (5.0m x 4.5m bays)."""
    cols = []
    cid = 1
    for gx_lbl, xm in [("1", 0.0), ("2", 5.0), ("3", 9.5)]:
        for gy_lbl, ym in [("A", 0.0), ("B", 4.5), ("C", 9.0)]:
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
            "project_name": f"Phase 6 Test ({project_id})",
            "location": "Baguio City",
            "design_code": "NSCP 2015",
            "materials": {
                "fc_mpa": 28.0,
                "fy_mpa": 415.0,
                "concrete_density_kn_m3": 23.54,
            },
        },
        "levels": [
            {"id": "1F", "elevation_m": 0.0},
            {"id": "2F", "elevation_m": 3.0},
            {"id": "3F", "elevation_m": 6.0},
            {"id": "ROOF", "elevation_m": 9.0},
        ],
        "columns": cols,
    }


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    projects_dir = tmp_path / "data" / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "DATA_DIR", projects_dir)
    client = TestClient(app)
    return client, tmp_path / "data"


def _setup_loaded_project(client: TestClient, pid: str) -> ProjectState:
    assert client.post("/api/projects", json=_make_multistory_payload(pid)).status_code == 200
    assert (
        client.post(
            f"/api/projects/{pid}/geometry/generate",
            json={"default_support_type": "FIXED", "include_base_tie_beams": True},
        ).status_code
        == 200
    )
    assert client.post(f"/api/projects/{pid}/sizing/auto-size").status_code == 200
    assert client.post(f"/api/projects/{pid}/loads/generate").status_code == 200
    return ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())


def test_dsm_solver_static_vertical_equilibrium_and_drift_limits(isolated_env):
    """
    1. Internal 3D DSM solver achieves static vertical equilibrium within 2% for DL, LL, and LR.
    2. Story drift check computes delta_m = 0.7 * R * delta_s accurately and applies 0.025*h
       when T < 0.7 s vs 0.020*h when T >= 0.7 s.
    """
    client, _ = isolated_env
    pid = "P6-EQUIL"
    project = _setup_loaded_project(client, pid)

    results = solve_frame_dsm(project)

    # Static vertical equilibrium check (sum(Fy_reactions) == total applied gravity load within 2%)
    for case_key in ("DL", "LL", "LR"):
        applied = results.primary_vertical_applied_kn[case_key]
        reacted = results.primary_vertical_reactions_kn[case_key]
        assert applied > 0.0, f"Expected non-zero applied vertical load for {case_key}"
        assert reacted == pytest.approx(applied, rel=0.02)

    # Drift check with T < 0.7 s -> allowable = 0.025 * h
    assert project.load_generation.seismic_summary is not None
    assert project.load_generation.seismic_summary.period_t_sec < 0.7
    drifts_short = compute_story_drifts(project, results)
    assert len(drifts_short) == 3
    r_x = project.load_generation.seismic.r_factor_x
    for d in drifts_short:
        assert d.delta_m_x_mm == pytest.approx(0.7 * r_x * d.delta_s_x_mm, abs=0.05)
        assert d.allowable_drift_mm == pytest.approx(0.025 * d.story_height_m * 1000.0, abs=0.05)

    # Drift check with T >= 0.7 s -> allowable = 0.020 * h
    project_long_t = project.model_copy(deep=True)
    project_long_t.load_generation.seismic_summary.period_t_sec = 0.85
    drifts_long = compute_story_drifts(project_long_t, results)
    for d in drifts_long:
        assert d.allowable_drift_mm == pytest.approx(0.020 * d.story_height_m * 1000.0, abs=0.05)


def test_rc_beam_column_footing_design_and_undersized_member_fail(isolated_env):
    """
    3. RC beam design, column P-M design, and isolated footing design produce valid rebar
       callouts and flag deliberately undersized beam/column sections as FAIL (DCR > 1.0).
    """
    client, _ = isolated_env
    pid = "P6-RC"
    _setup_loaded_project(client, pid)

    run_resp = client.post(
        f"/api/projects/{pid}/design/run",
        json={"params": {"soil_bearing_qa_kpa": 180.0, "main_bar_dia_beam_mm": 16, "main_bar_dia_col_mm": 20}},
    )
    assert run_resp.status_code == 200, run_resp.text
    ad = run_resp.json()
    assert ad["source"] == "internal_dsm"
    assert len(ad["beam_designs"]) > 0
    assert len(ad["column_designs"]) > 0
    assert len(ad["footing_designs"]) == 9  # 9 base supports

    # Check bar callouts & footing grouping
    for b in ad["beam_designs"]:
        assert "-16mm" in b["bars_top_start"]
        assert "-16mm" in b["bars_bot_mid"]
        assert "10mm @" in b["stirrups_support"]
    for c in ad["column_designs"]:
        assert "-20mm" in c["main_bars"]
        assert "10mm @" in c["ties_confined"]
        assert c["rho_provided"] >= 0.01
    for f in ad["footing_designs"]:
        assert f["footing_tag"].startswith("F")
        assert f["b_m"] >= 1.0
        assert f["thickness_mm"] >= 300.0
        assert "-16mm @" in f["bottom_bars_x"]

    # Now deliberately assign a tiny undersized beam (120x180) and column (160x160) -> must flag FAIL
    proj = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    tiny_beam_sec = SectionDefinition(
        section_id="B_TINY",
        label="B_TINY (120x180)",
        category="primary_beam",
        width_mm=120.0,
        depth_mm=180.0,
        cracked_ix=0.35,
        cracked_iy=0.35,
        cracked_iz=0.35,
    )
    tiny_col_sec = SectionDefinition(
        section_id="C_TINY",
        label="C_TINY (160x160)",
        category="column",
        width_mm=160.0,
        depth_mm=160.0,
        cracked_ix=0.70,
        cracked_iy=0.70,
        cracked_iz=0.70,
    )
    assert client.post(f"/api/projects/{pid}/sizing/sections", json=tiny_beam_sec.model_dump()).status_code == 200
    assert client.post(f"/api/projects/{pid}/sizing/sections", json=tiny_col_sec.model_dump()).status_code == 200

    first_beam = next(m for m in proj.structural_model.members if m.member_type == "primary_beam" and m.floor_level == "2F")
    first_col = next(m for m in proj.structural_model.members if m.member_type == "column" and m.floor_level == "1F")
    assert (
        client.post(
            f"/api/projects/{pid}/sizing/assign",
            json={"member_ids": [first_beam.member_id], "section_id": "B_TINY"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/projects/{pid}/sizing/assign",
            json={"member_ids": [first_col.member_id], "section_id": "C_TINY"},
        ).status_code
        == 200
    )

    fail_run = client.post(f"/api/projects/{pid}/design/run")
    assert fail_run.status_code == 200, fail_run.text
    ad_fail = fail_run.json()
    b_res = next(b for b in ad_fail["beam_designs"] if b["member_id"] == first_beam.member_id)
    c_res = next(c for c in ad_fail["column_designs"] if c["member_id"] == first_col.member_id)
    assert b_res["status"] == "FAIL" and b_res["governing_dcr"] > 1.0
    assert c_res["status"] == "FAIL" and c_res["governing_dcr"] > 1.0

    # Approval must be blocked while FAIL members remain
    appr_blocked = client.post(f"/api/projects/{pid}/design/approve")
    assert appr_blocked.status_code == 400


def test_staad_anl_parser_upload_and_error_handling(isolated_env):
    """
    4. STAAD .anl parser extracts joint displacements, support reactions, and beam end forces
       from a synthetic STAAD .anl snippet, and rejects ** ERROR files with no results.
    """
    client, _ = isolated_env
    pid = "P6-ANL"
    proj = _setup_loaded_project(client, pid)

    f1_sup = next(n for n in proj.structural_model.nodes if n.is_support)
    f2_node = next(n for n in proj.structural_model.nodes if n.floor_level == "2F")
    f3_node = next(n for n in proj.structural_model.nodes if n.floor_level == "3F")
    beam_m = next(m for m in proj.structural_model.members if m.member_type == "primary_beam")
    col_m = next(m for m in proj.structural_model.members if m.member_type == "column")

    synthetic_anl = f"""
    STAAD.Pro CONNECT Edition
    JOINT DISPLACEMENT (CM/MM)
    NODE    L/C    X-TRANS    Y-TRANS    Z-TRANS    X-ROTAN    Y-ROTAN    Z-ROTAN
    {f2_node.node_id}       1      4.200     -0.100      0.300      0.000      0.000      0.000
            3      0.200     -0.100      3.800      0.000      0.000      0.000
    {f3_node.node_id}       1      8.100     -0.200      0.500      0.000      0.000      0.000
            3      0.400     -0.200      7.400      0.000      0.000      0.000

    SUPPORT REACTIONS - UNIT KN METE
    NODE    L/C    FORCE-X    FORCE-Y    FORCE-Z    MOM-X      MOM-Y      MOM-Z
    {f1_sup.node_id}     102     12.500    420.000     -8.000     15.000      1.000     22.000
           202      8.000    310.000     -5.000     10.000      0.500     14.000

    BEAM END FORCES - UNIT KN METE
    BEAM    L/C   NODE     FX         FY         FZ         MX         MY         MZ
    {beam_m.member_id}     102    {beam_m.start_node}      5.000     85.000      0.000      0.000      0.000     72.000
                  {beam_m.end_node}     -5.000     85.000      0.000      0.000      0.000    -68.000
    {col_m.member_id}     102    {col_m.start_node}    410.000     18.000     12.000      1.000     25.000     35.000
                  {col_m.end_node}   -410.000    -18.000    -12.000     -1.000     20.000     30.000
    END OF LATEST ANALYSIS RESULT
    """

    parsed = parse_staad_anl(synthetic_anl, proj)
    assert parsed.seismic_displacements_mm["EX+"][f2_node.node_id][0] == pytest.approx(4.2)
    assert parsed.seismic_displacements_mm["EZ+"][f3_node.node_id][2] == pytest.approx(7.4)
    assert len(parsed.support_reactions) == 1
    assert parsed.support_reactions[0].max_asd_fy_kn == pytest.approx(310.0)
    assert parsed.support_reactions[0].max_lrfd_pu_kn == pytest.approx(420.0)
    assert parsed.beam_envelopes[beam_m.member_id].mu_neg_start_knm == pytest.approx(72.0)
    assert parsed.column_envelopes[col_m.member_id].pu_max_kn == pytest.approx(410.0)

    # Multipart upload endpoint
    up = client.post(
        f"/api/projects/{pid}/design/upload-anl",
        files={"file": ("model_run.anl", synthetic_anl.encode("utf-8"), "text/plain")},
    )
    assert up.status_code == 200, up.text
    ad = up.json()
    assert ad["source"] == "staad_anl"
    assert ad["source_filename"] == "model_run.anl"

    # Error-terminated .anl must return HTTP 400 with clear detail
    bad_anl = "** ERROR: NO REFERENCE LOAD CASE OF TYPE MASS/GRAVITY DEFINED.\nFINISH\n"
    bad = client.post(
        f"/api/projects/{pid}/design/upload-anl",
        files={"file": ("broken.anl", bad_anl.encode("utf-8"), "text/plain")},
    )
    assert bad.status_code == 400
    assert "error" in bad.json()["detail"].lower()


def test_csv_export_staad_concrete_design_block_and_legacy_json_compat(isolated_env):
    """
    5. CSV schedule export returns valid Beam/Column/Footing rows, STAAD .std includes
       START CONCRETE DESIGN ... END CONCRETE DESIGN, and legacy JSONs without
       analysis_design load cleanly.
    """
    client, _ = isolated_env
    pid = "P6-CSV"
    _setup_loaded_project(client, pid)

    assert client.post(f"/api/projects/{pid}/design/run").status_code == 200

    # Export CSV
    csv_resp = client.get(f"/api/projects/{pid}/design/export-csv")
    assert csv_resp.status_code == 200
    assert f'filename="{pid}_rc_schedules.csv"' in csv_resp.headers.get("content-disposition", "")
    rows = list(csv.reader(io.StringIO(csv_resp.text)))
    flat = "\n".join(",".join(r) for r in rows)
    assert "=== BEAM REINFORCEMENT SCHEDULE ===" in flat
    assert "=== COLUMN REINFORCEMENT SCHEDULE ===" in flat
    assert "=== ISOLATED FOOTING SCHEDULE ===" in flat
    assert len(rows) > 15

    # STAAD .std concrete design block
    std = client.get(f"/api/projects/{pid}/staad/download").text
    for tok in (
        "START CONCRETE DESIGN",
        "CODE ACI",
        "FC 28000 ALL",
        "FYMAIN 415000 ALL",
        "FYSEC 275000 ALL",
        "TRACK 1 ALL",
        "DESIGN BEAM",
        "DESIGN COLUMN",
        "END CONCRETE DESIGN",
    ):
        assert tok in std, f"Missing expected STAAD concrete design line: {tok}"
    assert std.index("PERFORM ANALYSIS PRINT STATICS CHECK") < std.index("START CONCRETE DESIGN")
    assert std.index("END CONCRETE DESIGN") < std.index("FINISH")

    # Legacy JSONs without analysis_design still load cleanly under extra="forbid"
    for legacy_path in (
        ROOT / "data" / "projects" / "DEMO-001.json",
        ROOT / "frontend" / "public" / "sample_project.json",
    ):
        if legacy_path.exists():
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            raw.pop("analysis_design", None)
            loaded = ProjectState.model_validate(raw)
            assert loaded.analysis_design.generated_at is None
            assert loaded.analysis_design.beam_designs == []
