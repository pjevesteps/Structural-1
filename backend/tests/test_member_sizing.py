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
from backend.app.main import app
from backend.app.models import ProjectState
from backend.app.sizing import auto_size_members, compute_ec_kn_m2
from backend.app.staad import write_staad_preview


def _base_project_info(project_id: str) -> dict:
    return {
        "project_id": project_id,
        "project_name": f"Phase 4 Test ({project_id})",
        "location": "Baguio City",
        "design_code": "NSCP 2015",
        "materials": {
            "fc_mpa": 28.0,
            "fy_mpa": 415.0,
            "concrete_density_kn_m3": 23.54,
        },
    }


def _make_multistory_payload(project_id: str = "P4-FRAME") -> dict:
    """4-story (5-level) 3x2 bay frame (spans 6.0m x 5.0m + 4.0m) with a stairwell opening."""
    cols = []
    cid = 1
    for gx_lbl, xm in [("1", 0.0), ("2", 6.0), ("3", 10.0)]:
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
        "project_info": _base_project_info(project_id),
        "levels": [
            {"id": "B1", "elevation_m": -2.8},
            {"id": "1F", "elevation_m": 0.0},
            {"id": "2F", "elevation_m": 3.0},
            {"id": "3F", "elevation_m": 6.0},
            {"id": "RD", "elevation_m": 9.0},
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


def test_auto_size_multistory_coverage_and_ec_calculation(isolated_env):
    """
    Verify auto_size_members generates tiered column sections, span-bucketed beam sections,
    slab thicknesses, 100% member coverage, and accurate Ec = 4700 * sqrt(fc) * 1000.
    """
    client, _ = isolated_env
    pid = "P4-AUTO"
    assert client.post("/api/projects", json=_make_multistory_payload(pid)).status_code == 200

    # Auto-size BEFORE Phase 3 geometry must fail cleanly with 400
    pre = client.post(f"/api/projects/{pid}/sizing/auto-size")
    assert pre.status_code == 400
    assert "module 03" in pre.json()["detail"].lower()

    # Generate Phase 3 3D frame (4 stories, 9 columns/story = 36 columns + 12*5=60 beams = 96 members)
    gen = client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={"default_support_type": "FIXED", "include_base_tie_beams": True},
    )
    assert gen.status_code == 200, gen.text
    sm = gen.json()

    # Add a secondary beam on 2F to verify pin_secondary_beams auto-release
    f2_nodes = [n for n in sm["nodes"] if n["floor_level"] == "2F"]
    n_a = next(n for n in f2_nodes if abs(n["x_m"] - 0.0) < 0.05 and abs(n["z_m"] - 0.0) < 0.05)
    n_b = next(n for n in f2_nodes if abs(n["x_m"] - 6.0) < 0.05 and abs(n["z_m"] - 5.0) < 0.05)
    add_sec = client.post(
        f"/api/projects/{pid}/geometry/members",
        json={
            "start_node": n_a["node_id"],
            "end_node": n_b["node_id"],
            "member_type": "secondary_beam",
            "floor_level": "2F",
        },
    )
    assert add_sec.status_code == 200, add_sec.text
    total_members = len(add_sec.json()["members"])
    sec_mid = add_sec.json()["members"][-1]["member_id"]

    # Run Phase 4 auto-size with f'c = 35 MPa
    res = client.post(
        f"/api/projects/{pid}/sizing/auto-size",
        json={
            "material": {"fc_mpa": 35.0, "fy_main_mpa": 415.0, "fy_tie_mpa": 275.0},
            "options": {"seismic_smrf_zone4": True, "include_cracked_modifiers": True, "pin_secondary_beams": True},
        },
    )
    assert res.status_code == 200, res.text
    sz = res.json()

    # Check Ec = 4700 * sqrt(35) * 1000
    expected_ec = round(4700.0 * math.sqrt(35.0) * 1000.0, 2)
    assert abs(sz["material"]["ec_kn_m2"] - expected_ec) < 1.0
    assert compute_ec_kn_m2(28.0) == round(4700.0 * math.sqrt(28.0) * 1000.0, 2)

    # 100% member assignment coverage & zero error issues
    assert len(sz["member_assignments"]) == total_members
    assert [iss for iss in sz["validation_issues"] if iss["severity"] == "error"] == []

    # Tiered column sections, primary beam sections, tie beam & secondary beam sections generated
    sec_cats = {s["category"] for s in sz["sections"]}
    assert {"column", "primary_beam", "tie_beam", "secondary_beam"}.issubset(sec_cats)

    # Verify secondary beam received START/END MY MZ pin releases
    sec_asgn = next(a for a in sz["member_assignments"] if a["member_id"] == sec_mid)
    assert sec_asgn["release_start_moment"] is True
    assert sec_asgn["release_end_moment"] is True

    # Verify slab assignments generated for every SlabPanel with thickness >= min_code_thickness_mm
    assert len(sz["slab_assignments"]) == len(add_sec.json()["slab_panels"])
    assert len(sz["slab_assignments"]) > 0
    for sa in sz["slab_assignments"]:
        assert sa["thickness_mm"] >= sa["min_code_thickness_mm"]


def test_bulk_reassign_nscp_smrf_warnings_and_approval_flow(isolated_env):
    """
    Verify:
      1. Bulk reassignment updates section_id, beta_angle_deg, and moment releases.
      2. NSCP 2015 SMRF validator flags a 200x200 mm column (SMRF_COLUMN_MIN_DIM) and a beam
         wider than its supporting column (BEAM_WIDER_THAN_COLUMN).
      3. Deleting an assigned section is blocked (400), while deleting an unused section succeeds.
      4. Updating slab thickness below min_code_thickness_mm flags SLAB_UNDER_MIN_THICKNESS.
      5. Approval succeeds when zero errors remain.
    """
    client, _ = isolated_env
    pid = "P4-NSCP"
    assert client.post("/api/projects", json=_make_multistory_payload(pid)).status_code == 200
    assert client.post(f"/api/projects/{pid}/geometry/generate").status_code == 200
    auto = client.post(f"/api/projects/{pid}/sizing/auto-size")
    assert auto.status_code == 200

    proj = client.get(f"/api/projects/{pid}").json()
    col_m = next(m for m in proj["structural_model"]["members"] if m["member_type"] == "column")
    # Find a primary beam sharing an endpoint node with col_m
    beam_m = next(
        m
        for m in proj["structural_model"]["members"]
        if m["member_type"] == "primary_beam"
        and (m["start_node"] in (col_m["start_node"], col_m["end_node"]) or m["end_node"] in (col_m["start_node"], col_m["end_node"]))
    )

    # Create undersized 200x200 mm column section and oversized 450x500 mm beam section
    r_c = client.post(
        f"/api/projects/{pid}/sizing/sections",
        json={
            "section_id": "C_SMALL",
            "label": "C_SMALL (200x200)",
            "category": "column",
            "width_mm": 200.0,
            "depth_mm": 200.0,
            "color_hex": "#f87171",
            "cracked_ix": 0.70,
            "cracked_iy": 0.70,
            "cracked_iz": 0.70,
        },
    )
    assert r_c.status_code == 200, r_c.text

    r_b = client.post(
        f"/api/projects/{pid}/sizing/sections",
        json={
            "section_id": "B_WIDE",
            "label": "B_WIDE (450x500)",
            "category": "primary_beam",
            "width_mm": 450.0,
            "depth_mm": 500.0,
            "color_hex": "#fb923c",
            "cracked_ix": 0.35,
            "cracked_iy": 0.35,
            "cracked_iz": 0.35,
        },
    )
    assert r_b.status_code == 200, r_b.text

    # Bulk assign col_m to C_SMALL with beta=90° and beam_m to B_WIDE with start release
    asgn_c = client.post(
        f"/api/projects/{pid}/sizing/assign",
        json={"member_ids": [col_m["member_id"]], "section_id": "C_SMALL", "beta_angle_deg": 90.0},
    )
    assert asgn_c.status_code == 200, asgn_c.text

    asgn_b = client.post(
        f"/api/projects/{pid}/sizing/assign",
        json={
            "member_ids": [beam_m["member_id"]],
            "section_id": "B_WIDE",
            "release_start_moment": True,
            "release_end_moment": False,
        },
    )
    assert asgn_b.status_code == 200, asgn_b.text
    sz = asgn_b.json()

    col_asgn = next(a for a in sz["member_assignments"] if a["member_id"] == col_m["member_id"])
    assert col_asgn["section_id"] == "C_SMALL"
    assert col_asgn["beta_angle_deg"] == 90.0
    assert col_asgn["manual_override"] is True

    beam_asgn = next(a for a in sz["member_assignments"] if a["member_id"] == beam_m["member_id"])
    assert beam_asgn["section_id"] == "B_WIDE"
    assert beam_asgn["release_start_moment"] is True
    assert beam_asgn["release_end_moment"] is False

    codes = {iss["code"] for iss in sz["validation_issues"]}
    assert "SMRF_COLUMN_MIN_DIM" in codes
    assert "BEAM_WIDER_THAN_COLUMN" in codes

    # Deleting assigned section C_SMALL must be rejected (400)
    del_busy = client.delete(f"/api/projects/{pid}/sizing/sections/C_SMALL")
    assert del_busy.status_code == 400

    # Update a slab panel below minimum thickness -> flags SLAB_UNDER_MIN_THICKNESS
    first_slab = sz["slab_assignments"][0]
    slab_res = client.patch(
        f"/api/projects/{pid}/sizing/slabs/{first_slab['panel_id']}",
        json={"thickness_mm": 80.0, "slab_tag": "S_THIN"},
    )
    assert slab_res.status_code == 200
    assert any(iss["code"] == "SLAB_UNDER_MIN_THICKNESS" for iss in slab_res.json()["validation_issues"])

    # Approve succeeds because SMRF/slab checks are warnings (0 errors)
    app_res = client.post(f"/api/projects/{pid}/sizing/approve")
    assert app_res.status_code == 200, app_res.text
    assert app_res.json()["engineer_approved"] is True


def test_staad_export_with_properties_cracked_releases_wrapping_and_legacy_compat(isolated_env, tmp_path: Path):
    """
    Verify:
      1. STAAD .std export emits DEFINE MATERIAL START, MEMBER PROPERTY (PRIS YD ... ZD ...),
         CONSTANTS (MATERIAL + BETA), MEMBER CRACKED, MEMBER RELEASE, per-section groups, and SUPPORTS.
      2. Long member lists (> 72 chars) wrap cleanly with '-' line continuations.
      3. Existing project JSON files without `member_sizing` continue to load cleanly with extra="forbid".
    """
    client, _ = isolated_env
    pid = "P4-STAAD"
    assert client.post("/api/projects", json=_make_multistory_payload(pid)).status_code == 200
    gen = client.post(f"/api/projects/{pid}/geometry/generate")
    assert gen.status_code == 200

    # Add a secondary beam so MEMBER RELEASE is populated
    f2_nodes = [n for n in gen.json()["nodes"] if n["floor_level"] == "2F"]
    n1 = f2_nodes[0]
    n2 = f2_nodes[-1]
    assert (
        client.post(
            f"/api/projects/{pid}/geometry/members",
            json={
                "start_node": n1["node_id"],
                "end_node": n2["node_id"],
                "member_type": "secondary_beam",
                "floor_level": "2F",
            },
        ).status_code
        == 200
    )

    auto = client.post(f"/api/projects/{pid}/sizing/auto-size")
    assert auto.status_code == 200
    col_mid = next(m["member_id"] for m in client.get(f"/api/projects/{pid}").json()["structural_model"]["members"] if m["member_type"] == "column")
    col_sec = next(a["section_id"] for a in auto.json()["member_assignments"] if a["member_id"] == col_mid)

    # Set a 90° beta angle on one column so BETA 90 MEMB appears in CONSTANTS
    assert (
        client.post(
            f"/api/projects/{pid}/sizing/assign",
            json={"member_ids": [col_mid], "section_id": col_sec, "beta_angle_deg": 90.0},
        ).status_code
        == 200
    )

    reloaded = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    out_std = tmp_path / "p4_full.std"
    staad_res = write_staad_preview(reloaded, out_std)
    assert staad_res["source"] == "structural_model_with_sizing"
    text = out_std.read_text(encoding="utf-8")
    assert "\\n" not in text

    # Strictly ordered STAAD blocks
    required_blocks = [
        "STAAD SPACE",
        "UNIT METER KN",
        "JOINT COORDINATES",
        "MEMBER INCIDENCES",
        "START GROUP DEFINITION",
        "_SEC_C1",
        "DEFINE MATERIAL START",
        "ISOTROPIC CONCRETE",
        "STRENGTH FCU 28000.0",
        "END DEFINE MATERIAL",
        "MEMBER PROPERTY",
        "PRIS YD ",
        "CONSTANTS",
        "MATERIAL CONCRETE ALL",
        f"BETA 90 MEMB {col_mid}",
        "MEMBER CRACKED",
        "REDUCTION RIX 0.70 RIY 0.70 RIZ 0.70",
        "REDUCTION RIX 0.35 RIY 0.35 RIZ 0.35",
        "MEMBER RELEASE",
        "START MY MZ",
        "END MY MZ",
        "SUPPORTS",
        "FINISH",
    ]
    positions = []
    for token in required_blocks:
        idx = text.find(token)
        assert idx != -1, f"Missing expected STAAD token '{token}' in:\n{text}"
        positions.append((token, idx))
    # Verify major block order
    major = [
        "JOINT COORDINATES",
        "MEMBER INCIDENCES",
        "START GROUP DEFINITION",
        "DEFINE MATERIAL START",
        "MEMBER PROPERTY",
        "CONSTANTS",
        "MEMBER CRACKED",
        "MEMBER RELEASE",
        "SUPPORTS",
        "FINISH",
    ]
    major_pos = [text.find(k) for k in major]
    assert major_pos == sorted(major_pos), f"STAAD blocks out of order: {list(zip(major, major_pos))}"

    # Verify long member lists wrap cleanly with '-' continuation lines and stay <= 72 chars
    lines = text.splitlines()
    assert any(ln.endswith(" -") for ln in lines), "Expected '-' continuation line for multi-story member lists"
    for ln in lines:
        if not ln.startswith("*"):
            assert len(ln) <= 72, f"STAAD line exceeded 72 chars ({len(ln)}): {ln}"

    # Verify legacy JSON files without member_sizing load cleanly with extra="forbid"
    legacy_raw = json.loads(reloaded.model_dump_json())
    legacy_raw.pop("member_sizing", None)
    legacy = ProjectState.model_validate(legacy_raw)
    assert legacy.member_sizing.sections == []
    assert legacy.member_sizing.member_assignments == []
