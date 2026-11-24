from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.geometry import generate_structural_model, validate_structural_model
from backend.app.main import app
from backend.app.models import (
    AnalyticalMember,
    AnalyticalNode,
    GeometryGenerationOptions,
    ProjectState,
    StructuralModelState,
)
from backend.app.staad import write_staad_preview


def _base_project_info(project_id: str) -> dict:
    return {
        "project_id": project_id,
        "project_name": f"Phase 3 Test {project_id}",
        "location": "Baguio City",
        "design_code": "NSCP 2015",
        "materials": {
            "fc_mpa": 27.6,
            "fy_mpa": 414.0,
            "concrete_density_kn_m3": 24.0,
        },
    }


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    projects_dir = tmp_path / "data" / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "DATA_DIR", projects_dir)
    client = TestClient(app)
    return client, tmp_path / "data"


def test_stepped_base_partial_basement_generation():
    """
    Stepped-base building:
      - Partial basement (B1 at 0.0 m) has ONLY 2 columns along x=0.0 m (y=0, y=5)
      - Full upper floor (1F at 2.8 m) has 4 columns (x=0, x=6) x (y=0, y=5), framing up to 2F (5.8 m)
    Verifies:
      - Supports are assigned at BOTH stepped base elevations (0.0 m and 2.8 m)
      - Exactly 2 columns span 0.0->2.8 m (no basement columns extruded at x=6.0 m)
      - Exactly 4 columns span 2.8->5.8 m
    """
    project = ProjectState.model_validate(
        {
            "project_info": _base_project_info("STEP-001"),
            "levels": [
                {"id": "B1", "elevation_m": 0.0},
                {"id": "1F", "elevation_m": 2.8},
                {"id": "2F", "elevation_m": 5.8},
            ],
            "columns": [
                # Partial basement columns at x=0 only
                {"id": "C-B1-1", "grid": "A-1", "floor_level": "B1", "x_m": 0.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C-B1-2", "grid": "B-1", "floor_level": "B1", "x_m": 0.0, "y_m": 5.0, "width_mm": 400, "depth_mm": 400},
                # Full 1F footprint (4 columns supporting 2F)
                {"id": "C-1F-1", "grid": "A-1", "floor_level": "1F", "x_m": 0.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C-1F-2", "grid": "B-1", "floor_level": "1F", "x_m": 0.0, "y_m": 5.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C-1F-3", "grid": "A-2", "floor_level": "1F", "x_m": 6.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C-1F-4", "grid": "B-2", "floor_level": "1F", "x_m": 6.0, "y_m": 5.0, "width_mm": 400, "depth_mm": 400},
            ],
        }
    )

    model = generate_structural_model(
        project,
        GeometryGenerationOptions(default_support_type="FIXED", include_base_tie_beams=True),
    )

    # Nodes: 2 on B1 + 4 on 1F + 4 on 2F = 10 nodes
    assert len(model.nodes) == 10
    b1_nodes = [n for n in model.nodes if n.floor_level == "B1"]
    assert len(b1_nodes) == 2
    assert all(n.x_m == 0.0 for n in b1_nodes)

    cols = [m for m in model.members if m.member_type == "column"]
    assert len(cols) == 6
    b1_cols = [m for m in cols if m.floor_level == "B1"]
    f1_cols = [m for m in cols if m.floor_level == "1F"]
    assert len(b1_cols) == 2
    assert len(f1_cols) == 4

    # Stepped supports at BOTH 0.0 m (2 basement bases) and 2.8 m (2 stepped bases at x=6.0)
    supports = [n for n in model.nodes if n.is_support and n.support_type == "FIXED"]
    assert len(supports) == 4
    support_elevs = sorted(n.y_m for n in supports)
    assert support_elevs == [0.0, 0.0, 2.8, 2.8]

    # Zero error-level issues
    errors = [iss for iss in model.validation_issues if iss.severity == "error"]
    assert errors == []


def test_opening_beam_skip_and_slab_panel_deduction():
    """
    3x2 grid (x=0, 4, 8; z=0, 4) with:
      - A stairwell opening spanning across the interior grid x=4 (x=3..5, z=1..3 -> skips beam at x=4 on 2F!)
      - An airwell opening strictly inside Bay 1 (x=0.5..2.5, z=0.5..2.5 -> area 4.0 sqm deducted from 16.0 sqm -> net 12.0 sqm!)
    """
    project = ProjectState.model_validate(
        {
            "project_info": _base_project_info("OPEN-001"),
            "levels": [
                {"id": "BASE", "elevation_m": 0.0},
                {"id": "2F", "elevation_m": 3.0},
            ],
            "columns": [
                {"id": "C1", "grid": "A-1", "x_m": 0.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C2", "grid": "A-2", "x_m": 4.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C3", "grid": "A-3", "x_m": 8.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C4", "grid": "B-1", "x_m": 0.0, "y_m": 4.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C5", "grid": "B-2", "x_m": 4.0, "y_m": 4.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C6", "grid": "B-3", "x_m": 8.0, "y_m": 4.0, "width_mm": 400, "depth_mm": 400},
            ],
            "openings": [
                {
                    "id": "OPN-STAIR",
                    "label": "STAIR-1",
                    "category": "stairwell",
                    "floor_level": "2F",
                    "polygon_m": [[3.0, 1.0], [5.0, 1.0], [5.0, 3.0], [3.0, 3.0]],
                    "area_sqm": 4.0,
                }
            ],
        }
    )

    # 1. Beam at x=4 (midpoint (4, 2) inside STAIR-1) MUST be skipped
    model_skip = generate_structural_model(
        project,
        GeometryGenerationOptions(skip_beams_through_openings=True, auto_detect_slab_panels=True),
    )
    node_by_id = {n.node_id: n for n in model_skip.nodes}
    f2_beams = [m for m in model_skip.members if m.floor_level == "2F" and m.member_type == "primary_beam"]
    crossing_present = any(
        abs(node_by_id[m.start_node].x_m - 4.0) < 0.05 and abs(node_by_id[m.end_node].x_m - 4.0) < 0.05
        for m in f2_beams
    )
    assert not crossing_present
    assert any(iss.code == "BEAM_SKIPPED_OPENING" and iss.severity == "info" for iss in model_skip.validation_issues)

    # 2. Opening strictly inside Bay 1 (x=0..4, z=0..4, gross=16 sqm) deducts exact area=4.0 sqm -> net=12.0 sqm (two_way)
    project_panel = ProjectState.model_validate(
        {
            **project.model_dump(),
            "openings": [
                {
                    "id": "OPN-AIR",
                    "label": "AIRWELL-1",
                    "category": "airwell",
                    "floor_level": "2F",
                    "polygon_m": [[0.5, 0.5], [2.5, 0.5], [2.5, 2.5], [0.5, 2.5]],
                    "area_sqm": 4.0,
                }
            ],
        }
    )
    model_panels = generate_structural_model(
        project_panel,
        GeometryGenerationOptions(skip_beams_through_openings=True, auto_detect_slab_panels=True),
    )
    assert len(model_panels.slab_panels) == 2
    bay1 = next(p for p in model_panels.slab_panels if p.polygon_m[0][0] == 0.0)
    bay2 = next(p for p in model_panels.slab_panels if p.polygon_m[0][0] == 4.0)
    assert abs(bay1.gross_area_sqm - 16.0) < 1e-3
    assert abs(bay1.opening_deduction_sqm - 4.0) < 1e-3
    assert abs(bay1.net_area_sqm - 12.0) < 1e-3
    assert bay1.slab_action == "two_way"
    assert abs(bay2.net_area_sqm - 16.0) < 1e-3


def test_transfer_floating_column_warning():
    """A column starting above lowest elevation with no column below triggers a FLOATING_OR_TRANSFER_COLUMN warning."""
    model = StructuralModelState(
        nodes=[
            AnalyticalNode(node_id=1, x_m=0.0, y_m=0.0, z_m=0.0, floor_level="BASE", is_support=True, support_type="FIXED"),
            AnalyticalNode(node_id=2, x_m=0.0, y_m=3.0, z_m=0.0, floor_level="2F"),
            AnalyticalNode(node_id=3, x_m=3.0, y_m=3.0, z_m=0.0, floor_level="2F"),
            AnalyticalNode(node_id=4, x_m=3.0, y_m=6.0, z_m=0.0, floor_level="3F"),
        ],
        members=[
            AnalyticalMember(member_id=1, start_node=1, end_node=2, member_type="column", floor_level="BASE", length_m=3.0),
            AnalyticalMember(member_id=2, start_node=2, end_node=3, member_type="primary_beam", floor_level="2F", length_m=3.0),
            # Floating/transfer column starting at N3 (y=3.0 m) with no column below!
            AnalyticalMember(member_id=3, start_node=3, end_node=4, member_type="column", floor_level="2F", length_m=3.0),
        ],
    )
    issues = validate_structural_model(model)
    assert any(
        iss.severity == "warning" and iss.code == "FLOATING_OR_TRANSFER_COLUMN" and "M3" in iss.entity_ids
        for iss in issues
    )


def test_manual_member_support_approval_and_staad_export(isolated_env, tmp_path: Path):
    """End-to-end API test: generate -> manual beam add/delete -> support toggle -> approve -> upgraded STAAD .std export."""
    client, _ = isolated_env
    pid = "P3-E2E"

    payload = {
        "project_info": _base_project_info(pid),
        "levels": [
            {"id": "BASE", "elevation_m": 0.0},
            {"id": "2F", "elevation_m": 3.2},
            {"id": "ROOF", "elevation_m": 6.4},
        ],
        "columns": [
            {"id": "C1", "grid": "A-1", "x_m": 0.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
            {"id": "C2", "grid": "A-2", "x_m": 6.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
            {"id": "C3", "grid": "B-1", "x_m": 0.0, "y_m": 5.0, "width_mm": 400, "depth_mm": 400},
            {"id": "C4", "grid": "B-2", "x_m": 6.0, "y_m": 5.0, "width_mm": 400, "depth_mm": 400},
        ],
    }
    assert client.post("/api/projects", json=payload).status_code == 200

    # 1. Generate 3D frame
    gen = client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={"default_support_type": "FIXED", "include_base_tie_beams": True},
    )
    assert gen.status_code == 200, gen.text
    sm = gen.json()
    assert len(sm["nodes"]) == 12
    assert len(sm["slab_panels"]) == 2  # 2F and ROOF bays

    # 2. Manually add a diagonal/secondary beam on 2F between opposite corners, then delete it
    f2_nodes = [n for n in sm["nodes"] if n["floor_level"] == "2F"]
    n_start = next(n for n in f2_nodes if n["x_m"] == 0.0 and n["z_m"] == 0.0)
    n_end = next(n for n in f2_nodes if n["x_m"] == 6.0 and n["z_m"] == 5.0)
    add_res = client.post(
        f"/api/projects/{pid}/geometry/members",
        json={
            "start_node": n_start["node_id"],
            "end_node": n_end["node_id"],
            "member_type": "secondary_beam",
            "floor_level": "2F",
        },
    )
    assert add_res.status_code == 200, add_res.text
    added_member = add_res.json()["members"][-1]
    assert added_member["manual_override"] is True

    del_res = client.delete(
        f"/api/projects/{pid}/geometry/members/{added_member['member_id']}"
    )
    assert del_res.status_code == 200
    assert not any(m["member_id"] == added_member["member_id"] for m in del_res.json()["members"])

    # 3. Toggle one base node from FIXED to PINNED
    base_nodes = [n for n in del_res.json()["nodes"] if n["floor_level"] == "BASE"]
    sup_res = client.patch(
        f"/api/projects/{pid}/geometry/nodes/{base_nodes[0]['node_id']}/support",
        json={"support_type": "PINNED"},
    )
    assert sup_res.status_code == 200
    updated_base = next(n for n in sup_res.json()["nodes"] if n["node_id"] == base_nodes[0]["node_id"])
    assert updated_base["support_type"] == "PINNED"
    assert updated_base["is_support"] is True

    # 4. Approve geometry
    app_res = client.post(f"/api/projects/{pid}/geometry/approve")
    assert app_res.status_code == 200, app_res.text
    assert app_res.json()["engineer_approved"] is True

    # 5. Export upgraded STAAD .std preview directly from structural_model
    reloaded = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    out_std = tmp_path / "p3_model.std"
    staad_res = write_staad_preview(reloaded, out_std)
    assert staad_res["source"] == "structural_model"
    assert staad_res["nodes"] == 12
    assert staad_res["supports"] == 4

    text = out_std.read_text(encoding="utf-8")
    assert "\\n" not in text
    assert "STAAD SPACE\nUNIT METER KN\nJOINT COORDINATES\n" in text
    assert "MEMBER INCIDENCES\n" in text
    assert "START GROUP DEFINITION\nMEMBER\n" in text
    assert "_COLUMNS " in text
    assert "_BEAMS " in text
    assert "_TIE_BEAMS " in text
    assert "SUPPORTS\n" in text
    assert " FIXED\n" in text
    assert " PINNED\n" in text
    assert text.strip().endswith("FINISH")

    # 6. Verify existing DEMO-001.json and legacy (pre-Phase 3) JSON backward compatibility with extra="forbid"
    demo_path = ROOT / "data" / "projects" / "DEMO-001.json"
    if demo_path.exists():
        raw_demo = json.loads(demo_path.read_text(encoding="utf-8"))
        ProjectState.model_validate(raw_demo)
        legacy_raw = dict(raw_demo)
        legacy_raw.pop("structural_model", None)
        legacy_raw.pop("openings", None)
        legacy_demo = ProjectState.model_validate(legacy_raw)
        assert legacy_demo.structural_model.nodes == []
        assert legacy_demo.openings == []


def test_flexible_column_level_and_numeric_elevation_matching():
    """
    Verify columns tagged with numeric elevations (e.g. "14" or "14.0") or common floor aliases
    (e.g. "L4", "BASEMENT", "FIRST", "ROOF DECK") resolve cleanly against project.levels without
    UNKNOWN_COLUMN_LEVEL warnings, while truly unknown labels include available levels in the warning.
    """
    project = ProjectState.model_validate(
        {
            "project_info": _base_project_info("FLEX-LVL"),
            "levels": [
                {"id": "B1", "elevation_m": -2.6},
                {"id": "1F", "elevation_m": 0.0},
                {"id": "2F", "elevation_m": 3.0},
                {"id": "3F", "elevation_m": 6.0},
                {"id": "4F", "elevation_m": 11.0},
                {"id": "RD", "elevation_m": 14.0},
            ],
            "columns": [
                # Exact + alias matches ("BASEMENT" -> B1, "FIRST" -> 1F, "L2" -> 2F, "L3" -> 3F, "L4" -> 4F)
                {"id": "C_B", "grid": "A-1", "floor_level": "BASEMENT", "x_m": 0.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C_1", "grid": "A-1", "floor_level": "FIRST", "x_m": 0.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C_2", "grid": "A-1", "floor_level": "L2", "x_m": 0.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C_3", "grid": "A-1", "floor_level": "L3", "x_m": 0.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C_4", "grid": "A-1", "floor_level": "L4", "x_m": 0.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                # Numeric elevation match ("14" -> RD at 14.0 m -> top story 4F..RD; "11.0" -> 4F at 11.0 m)
                {"id": "C_E14", "grid": "A-2", "floor_level": "14", "x_m": 6.0, "y_m": 0.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C_E11", "grid": "B-1", "floor_level": "11.0", "x_m": 0.0, "y_m": 5.0, "width_mm": 400, "depth_mm": 400},
                {"id": "C_RD", "grid": "B-2", "floor_level": "ROOF DECK", "x_m": 6.0, "y_m": 5.0, "width_mm": 400, "depth_mm": 400},
            ],
        }
    )

    model = generate_structural_model(project)
    unknown_issues = [iss for iss in model.validation_issues if iss.code == "UNKNOWN_COLUMN_LEVEL"]
    assert unknown_issues == [], f"Unexpected UNKNOWN_COLUMN_LEVEL warnings: {unknown_issues}"

    # Verify C_E14 ("14") and C_4 ("L4") created nodes at 4F (11.0 m) and RD (14.0 m)
    node_elevs_at_a2 = sorted(n.y_m for n in model.nodes if abs(n.x_m - 6.0) < 0.05 and abs(n.z_m) < 0.05)
    assert node_elevs_at_a2 == [11.0, 14.0]
    node_elevs_at_a1 = sorted(n.y_m for n in model.nodes if abs(n.x_m) < 0.05 and abs(n.z_m) < 0.05)
    assert node_elevs_at_a1 == [-2.6, 0.0, 3.0, 6.0, 11.0, 14.0]

    # Verify a truly unknown level still emits UNKNOWN_COLUMN_LEVEL listing available project levels
    bad_project = project.model_copy(deep=True)
    bad_project.columns.append(
        project.columns[0].model_copy(update={"id": "C_BAD", "floor_level": "HELIPAD"})
    )
    bad_model = generate_structural_model(bad_project)
    bad_unknown = [iss for iss in bad_model.validation_issues if iss.code == "UNKNOWN_COLUMN_LEVEL"]
    assert len(bad_unknown) == 1
    assert "HELIPAD" in bad_unknown[0].message
    assert "Available project levels" in bad_unknown[0].message
    assert "4F" in bad_unknown[0].message and "RD" in bad_unknown[0].message
