from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.main import app
from backend.app.models import AnalyticalMember, AnalyticalNode, ProjectState


def _make_2x2_project(project_id: str) -> dict:
    return {
        "project_info": {
            "project_id": project_id,
            "project_name": "Geometry Cleanup & Manual Beam Test",
            "location": "Manila",
            "design_code": "NSCP 2015",
            "materials": {
                "fc_mpa": 28.0,
                "fy_mpa": 415.0,
                "concrete_density_kn_m3": 24.0,
            },
        },
        "levels": [
            {"id": "BASE", "elevation_m": 0.0},
            {"id": "2F", "elevation_m": 3.5},
        ],
        "grids": {
            "x_grids": [
                {"label": "A", "coord_m": 0.0},
                {"label": "B", "coord_m": 5.0},
            ],
            "y_grids": [
                {"label": "1", "coord_m": 0.0},
                {"label": "2", "coord_m": 4.0},
            ],
        },
        "columns": [
            {"id": "C1", "grid": "A-1", "x_m": 0.0, "y_m": 0.0, "width_mm": 350.0, "depth_mm": 350.0},
            {"id": "C2", "grid": "B-1", "x_m": 5.0, "y_m": 0.0, "width_mm": 350.0, "depth_mm": 350.0},
            {"id": "C3", "grid": "A-2", "x_m": 0.0, "y_m": 4.0, "width_mm": 350.0, "depth_mm": 350.0},
            {"id": "C4", "grid": "B-2", "x_m": 5.0, "y_m": 4.0, "width_mm": 350.0, "depth_mm": 350.0},
        ],
        "beams": [],
    }


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    projects_dir = data_dir / "projects"
    exports_dir = data_dir / "exports"
    projects_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(storage, "DATA_DIR", projects_dir)
    return TestClient(app)


def test_geometry_integrity_diagnostics_and_cleanup_endpoints(isolated_env):
    """
    Verify structured detection and safe cleanup of:
      - DISJOINTED_SUBSTRUCTURE
      - ORPHAN_NODE
      - DUPLICATE_NODE (with member rewiring onto primary_node_id)
      - DUPLICATE_MEMBER (order-independent {start, end})
      - Batch clean-all-duplicates
    """
    client = isolated_env
    pid = "TEST-GEOM-CLEANUP"
    storage.save_project(ProjectState.model_validate(_make_2x2_project(pid)))

    gen = client.post(f"/api/projects/{pid}/geometry/generate", json={})
    assert gen.status_code == 200, gen.text

    # Inject duplicate node, duplicate member (reversed endpoints), orphan node, and disjointed substructure
    proj = storage.load_project(pid)
    model = proj.structural_model
    top_nodes = [n for n in model.nodes if n.floor_level == "2F"]
    assert len(top_nodes) == 4
    n_a1 = next(n for n in top_nodes if abs(n.x_m - 0.0) < 0.01 and abs(n.z_m - 0.0) < 0.01)
    n_b2 = next(n for n in top_nodes if abs(n.x_m - 5.0) < 0.01 and abs(n.z_m - 4.0) < 0.01)

    max_nid = max(n.node_id for n in model.nodes)
    max_mid = max(m.member_id for m in model.members)

    # 1. Duplicate node within 0.02 m of n_a1, with a diagonal beam wired to it -> n_b2
    dup_nid = max_nid + 1
    model.nodes.append(
        AnalyticalNode(
            node_id=dup_nid,
            x_m=n_a1.x_m + 0.01,
            y_m=n_a1.y_m,
            z_m=n_a1.z_m + 0.01,
            floor_level="2F",
            grid_ref="DUP",
            is_support=False,
            support_type="NONE",
        )
    )
    rewired_mid = max_mid + 1
    model.members.append(
        AnalyticalMember(
            member_id=rewired_mid,
            start_node=dup_nid,
            end_node=n_b2.node_id,
            member_type="primary_beam",
            floor_level="2F",
            grid_ref="DIAG",
            length_m=6.4,
            manual_override=True,
        )
    )

    # 2. Duplicate member overlapping an existing beam (reversed {end, start})
    existing_beam = next(m for m in model.members if m.member_type != "column" and m.floor_level == "2F")
    dup_mid = max_mid + 2
    model.members.append(
        AnalyticalMember(
            member_id=dup_mid,
            start_node=existing_beam.end_node,
            end_node=existing_beam.start_node,
            member_type="primary_beam",
            floor_level="2F",
            grid_ref="DUP-BEAM",
            length_m=existing_beam.length_m,
            manual_override=True,
        )
    )

    # 3. Orphan node (zero connected members)
    orphan_nid = max_nid + 2
    model.nodes.append(
        AnalyticalNode(
            node_id=orphan_nid,
            x_m=12.0,
            y_m=3.5,
            z_m=0.0,
            floor_level="2F",
            grid_ref="ORPHAN",
            is_support=False,
            support_type="NONE",
        )
    )

    # 4. Disjointed 2-node substructure
    sub_n1, sub_n2 = max_nid + 3, max_nid + 4
    model.nodes.extend(
        [
            AnalyticalNode(
                node_id=sub_n1, x_m=20.0, y_m=0.0, z_m=0.0, floor_level="BASE", is_support=True, support_type="FIXED"
            ),
            AnalyticalNode(
                node_id=sub_n2, x_m=20.0, y_m=3.5, z_m=0.0, floor_level="2F", is_support=False, support_type="NONE"
            ),
        ]
    )
    sub_mid = max_mid + 3
    model.members.append(
        AnalyticalMember(
            member_id=sub_mid,
            start_node=sub_n1,
            end_node=sub_n2,
            member_type="column",
            floor_level="BASE",
            length_m=3.5,
        )
    )
    storage.save_project(proj)

    # Validate -> verify all 4 structured issue types
    val = client.post(f"/api/projects/{pid}/geometry/validate")
    assert val.status_code == 200, val.text
    issues = val.json()["validation_issues"]

    dup_node_iss = next((i for i in issues if i["code"] == "DUPLICATE_NODE"), None)
    dup_mem_iss = next((i for i in issues if i["code"] == "DUPLICATE_MEMBER"), None)
    orphan_iss = next((i for i in issues if i["code"] == "ORPHAN_NODE"), None)
    disjoint_iss = next((i for i in issues if i["code"] == "DISJOINTED_SUBSTRUCTURE"), None)

    assert dup_node_iss is not None
    assert dup_node_iss["primary_node_id"] == n_a1.node_id
    assert dup_node_iss["duplicate_node_id"] == dup_nid

    assert dup_mem_iss is not None
    assert dup_mem_iss["primary_member_id"] == existing_beam.member_id
    assert dup_mem_iss["duplicate_member_id"] == dup_mid

    assert orphan_iss is not None
    assert orphan_nid in orphan_iss["node_ids"]

    assert disjoint_iss is not None
    assert len(disjoint_iss["substructures"]) >= 2
    assert sub_n1 in disjoint_iss["node_ids"] and sub_mid in disjoint_iss["member_ids"]

    # Step A: Delete duplicate member
    res_dm = client.post(
        f"/api/projects/{pid}/geometry/delete-duplicate-member",
        json={"primary_member_id": existing_beam.member_id, "duplicate_member_id": dup_mid},
    )
    assert res_dm.status_code == 200, res_dm.text
    mids_after_dm = {m["member_id"] for m in res_dm.json()["members"]}
    assert existing_beam.member_id in mids_after_dm
    assert dup_mid not in mids_after_dm
    assert not any(i["code"] == "DUPLICATE_MEMBER" for i in res_dm.json()["validation_issues"])

    # Step B: Merge duplicate node -> verify rewired_mid now starts at n_a1.node_id!
    res_mn = client.post(
        f"/api/projects/{pid}/geometry/merge-duplicate-node",
        json={"primary_node_id": n_a1.node_id, "duplicate_node_id": dup_nid},
    )
    assert res_mn.status_code == 200, res_mn.text
    nids_after_mn = {n["node_id"] for n in res_mn.json()["nodes"]}
    assert n_a1.node_id in nids_after_mn
    assert dup_nid not in nids_after_mn
    rewired_m = next((m for m in res_mn.json()["members"] if m["member_id"] == rewired_mid), None)
    assert rewired_m is not None
    assert rewired_m["start_node"] == n_a1.node_id
    assert rewired_m["end_node"] == n_b2.node_id
    assert not any(i["code"] == "DUPLICATE_NODE" for i in res_mn.json()["validation_issues"])

    # Step C: Delete orphan nodes
    res_or = client.post(f"/api/projects/{pid}/geometry/delete-orphan-nodes")
    assert res_or.status_code == 200, res_or.text
    assert orphan_nid not in {n["node_id"] for n in res_or.json()["nodes"]}
    assert not any(i["code"] == "ORPHAN_NODE" for i in res_or.json()["validation_issues"])

    # Step D: Batch clean-all-duplicates
    proj2 = storage.load_project(pid)
    m2 = proj2.structural_model
    m2.nodes.append(
        AnalyticalNode(
            node_id=900, x_m=n_b2.x_m + 0.005, y_m=n_b2.y_m, z_m=n_b2.z_m, floor_level="2F", is_support=False, support_type="NONE"
        )
    )
    m2.members.append(
        AnalyticalMember(
            member_id=901,
            start_node=existing_beam.start_node,
            end_node=existing_beam.end_node,
            member_type="primary_beam",
            floor_level="2F",
            length_m=existing_beam.length_m,
        )
    )
    storage.save_project(proj2)

    res_all = client.post(f"/api/projects/{pid}/geometry/clean-all-duplicates")
    assert res_all.status_code == 200, res_all.text
    assert not any(i["code"] in ("DUPLICATE_NODE", "DUPLICATE_MEMBER") for i in res_all.json()["validation_issues"])


def test_manually_added_beam_included_in_dsm_analysis_and_rc_design(isolated_env):
    """
    Verify a beam added manually in Module 03 AFTER Module 04 sizing and Module 05 load
    generation automatically gets a valid section, enters the 3D DSM solver with self-weight,
    and appears in Module 06's beam_designs with non-zero forces.
    """
    client = isolated_env
    pid = "TEST-MANUAL-BEAM-DSM"
    storage.save_project(ProjectState.model_validate(_make_2x2_project(pid)))

    # 1. Run Module 03 -> Module 04 -> Module 05
    r_geo = client.post(f"/api/projects/{pid}/geometry/generate", json={})
    assert r_geo.status_code == 200, r_geo.text
    r_size = client.post(f"/api/projects/{pid}/sizing/auto-size", json={})
    assert r_size.status_code == 200, r_size.text
    r_load = client.post(f"/api/projects/{pid}/loads/generate", json={})
    assert r_load.status_code == 200, r_load.text

    # 2. Go back to Module 03 and manually add a diagonal beam on 2F (A-1 to B-2)
    nodes_2f = [n for n in r_geo.json()["nodes"] if n["floor_level"] == "2F"]
    n_a1 = next(n for n in nodes_2f if abs(n["x_m"] - 0.0) < 0.01 and abs(n["z_m"] - 0.0) < 0.01)
    n_b2 = next(n for n in nodes_2f if abs(n["x_m"] - 5.0) < 0.01 and abs(n["z_m"] - 4.0) < 0.01)

    r_add = client.post(
        f"/api/projects/{pid}/geometry/members",
        json={
            "start_node": n_a1["node_id"],
            "end_node": n_b2["node_id"],
            "member_type": "secondary_beam",
            "floor_level": "2F",
            "grid_ref": "DIAG-MAN",
        },
    )
    assert r_add.status_code == 200, r_add.text
    manual_beam = next(
        (m for m in r_add.json()["members"] if m["grid_ref"] == "DIAG-MAN"),
        None,
    )
    assert manual_beam is not None
    manual_mid = manual_beam["member_id"]

    # Also test jumping straight into Module 06 even if a beam's assignment was never created in Module 04
    proj = storage.load_project(pid)
    proj.member_sizing.member_assignments = [
        a for a in proj.member_sizing.member_assignments if a.member_id != manual_mid
    ]
    storage.save_project(proj)

    # 3. Run Module 06 Analysis & RC Design directly
    r_des = client.post(f"/api/projects/{pid}/design/run", json={})
    assert r_des.status_code == 200, r_des.text
    beam_designs = r_des.json()["beam_designs"]
    man_design = next((b for b in beam_designs if b["member_id"] == manual_mid), None)
    assert man_design is not None, f"Manual beam M{manual_mid} missing from Module 06 beam_designs!"
    assert man_design["vu_max_kn"] > 0.0
    assert (man_design["mu_pos_mid_knm"] > 0.0) or (man_design["mu_neg_start_knm"] > 0.0)
    assert man_design["width_mm"] > 0.0 and man_design["depth_mm"] > 0.0


def test_base_tie_beams_with_and_without_footing_depth(isolated_env):
    """
    Verify:
      1. footing_depth_m=1.50 + include_base_tie_beams=True:
         - GF grade nodes remain at y_m=0.0 with floor_level='GF'
         - Footing support nodes drop to y_m=-1.50 with floor_level='FTG_GF' and is_support=True
         - Pedestal columns (is_pedestal=True) connect FTG_GF -> GF
         - Base tie beams (member_type='tie_beam') sit at GF (y_m=0.0, floor_level='GF')
      2. footing_depth_m=0.0 + include_base_tie_beams=True and False:
         - No regression; supports sit directly on GF at y_m=0.0.
    """
    client = isolated_env
    pid = "TEST-GF-TIE-PEDESTAL"
    payload = _make_2x2_project(pid)
    payload["levels"] = [
        {"id": "GF", "elevation_m": 0.0},
        {"id": "2F", "elevation_m": 3.5},
        {"id": "ROOF_LEVEL", "elevation_m": 7.0},
    ]
    storage.save_project(ProjectState.model_validate(payload))

    # Case 1: footing_depth_m = 1.50, include_base_tie_beams = True
    r1 = client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={"include_base_tie_beams": True, "footing_depth_m": 1.50},
    )
    assert r1.status_code == 200, r1.text
    m1 = r1.json()
    node_by_id = {n["node_id"]: n for n in m1["nodes"]}

    gf_nodes = [n for n in m1["nodes"] if n["floor_level"] == "GF"]
    ftg_nodes = [n for n in m1["nodes"] if n["floor_level"] == "FTG_GF"]
    assert len(gf_nodes) == 4
    assert all(abs(n["y_m"] - 0.0) < 1e-4 for n in gf_nodes)
    assert all(n["is_support"] is False for n in gf_nodes)

    assert len(ftg_nodes) == 4
    assert all(abs(n["y_m"] - (-1.50)) < 1e-4 for n in ftg_nodes)
    assert all(n["is_support"] is True for n in ftg_nodes)

    pedestals = [m for m in m1["members"] if m["is_pedestal"]]
    assert len(pedestals) == 4

    gf_tie_beams = [
        m for m in m1["members"] if m["member_type"] == "tie_beam" and m["floor_level"] == "GF"
    ]
    assert len(gf_tie_beams) == 4
    for tb in gf_tie_beams:
        na = node_by_id[tb["start_node"]]
        nb = node_by_id[tb["end_node"]]
        assert na["floor_level"] == "GF" and nb["floor_level"] == "GF"
        assert abs(na["y_m"] - 0.0) < 1e-4 and abs(nb["y_m"] - 0.0) < 1e-4

    # Case 2: footing_depth_m = 0.0, include_base_tie_beams = True
    r2 = client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={"include_base_tie_beams": True, "footing_depth_m": 0.0},
    )
    assert r2.status_code == 200, r2.text
    m2 = r2.json()
    assert not any(n["floor_level"].startswith("FTG_") for n in m2["nodes"])
    assert sum(1 for n in m2["nodes"] if n["floor_level"] == "GF" and n["is_support"]) == 4
    assert sum(1 for m in m2["members"] if m["member_type"] == "tie_beam" and m["floor_level"] == "GF") == 4

    # Case 3: footing_depth_m = 0.0, include_base_tie_beams = False
    r3 = client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={"include_base_tie_beams": False, "footing_depth_m": 0.0},
    )
    assert r3.status_code == 200, r3.text
    m3 = r3.json()
    assert sum(1 for m in m3["members"] if m["member_type"] == "tie_beam") == 0
    assert sum(1 for n in m3["nodes"] if n["floor_level"] == "GF" and n["is_support"]) == 4


def test_single_upper_floor_column_import_still_generates_gf_tie_beams_and_pedestals(isolated_env):
    """
    Regression test for real-world PDF imports where all columns were tagged on a single
    upper plan (e.g. '2F') on a 3-level building [GF (0m), 2F (3m), ROOF (6m)]:
    columns must extrude down to GF (0m) so GF tie beams and -1.5m footing pedestals
    are generated in both 3D and 2D views.
    """
    client = isolated_env
    pid = "TEST-SINGLE-2F-COLS"
    payload = _make_2x2_project(pid)
    payload["levels"] = [
        {"id": "GF", "elevation_m": 0.0},
        {"id": "2F", "elevation_m": 3.0},
        {"id": "ROOF", "elevation_m": 6.0},
    ]
    for col in payload["columns"]:
        col["floor_level"] = "2F"
    storage.save_project(ProjectState.model_validate(payload))

    r = client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={"include_base_tie_beams": True, "footing_depth_m": 1.50},
    )
    assert r.status_code == 200, r.text
    sm = r.json()

    gf_nodes = [n for n in sm["nodes"] if n["floor_level"] == "GF"]
    ftg_nodes = [n for n in sm["nodes"] if n["floor_level"] == "FTG_GF"]
    gf_tie_beams = [
        m for m in sm["members"] if m["member_type"] == "tie_beam" and m["floor_level"] == "GF"
    ]
    assert len(gf_nodes) == 4
    assert len(ftg_nodes) == 4
    assert all(abs(n["y_m"] - (-1.50)) < 1e-4 for n in ftg_nodes)
    assert len(gf_tie_beams) == 4


def test_geometry_add_cantilever_via_end_xyz_and_edge_beam_survives_regeneration(isolated_env):
    """
    Verify POST /geometry/members supports creating a cantilever_beam from an existing start_node
    to a free-end tip coordinate end_xyz_m (synthesizing the tip node cleanly), adding an edge_beam
    between two cantilever tips, and retaining both across regenerate + validate.
    """
    client = isolated_env
    pid = "TEST-CANTILEVER-API"
    storage.save_project(ProjectState.model_validate(_make_2x2_project(pid)))

    gen = client.post(f"/api/projects/{pid}/geometry/generate", json={})
    assert gen.status_code == 200, gen.text
    sm0 = gen.json()
    top_nodes = [n for n in sm0["nodes"] if n["floor_level"] == "2F"]
    n_b1 = next(n for n in top_nodes if abs(n["x_m"] - 5.0) < 0.01 and abs(n["z_m"] - 0.0) < 0.01)
    n_b2 = next(n for n in top_nodes if abs(n["x_m"] - 5.0) < 0.01 and abs(n["z_m"] - 4.0) < 0.01)

    # Create two 1.5 m cantilevers projecting to x = 6.5 m
    c1 = client.post(
        f"/api/projects/{pid}/geometry/members",
        json={
            "start_node": n_b1["node_id"],
            "end_xyz_m": [6.5, 3.0, 0.0],
            "member_type": "cantilever_beam",
            "floor_level": "2F",
        },
    )
    assert c1.status_code == 200, c1.text
    c2 = client.post(
        f"/api/projects/{pid}/geometry/members",
        json={
            "start_node": n_b2["node_id"],
            "end_xyz_m": [6.5, 3.0, 4.0],
            "member_type": "cantilever_beam",
            "floor_level": "2F",
        },
    )
    assert c2.status_code == 200, c2.text
    sm_after_cant = c2.json()
    tip1 = next(n for n in sm_after_cant["nodes"] if abs(n["x_m"] - 6.5) < 0.01 and abs(n["z_m"] - 0.0) < 0.01)
    tip2 = next(n for n in sm_after_cant["nodes"] if abs(n["x_m"] - 6.5) < 0.01 and abs(n["z_m"] - 4.0) < 0.01)

    # Connect the two cantilever tips with an edge_beam
    eb = client.post(
        f"/api/projects/{pid}/geometry/members",
        json={
            "start_node": tip1["node_id"],
            "end_node": tip2["node_id"],
            "member_type": "edge_beam",
            "floor_level": "2F",
        },
    )
    assert eb.status_code == 200, eb.text

    # Regenerate -> both cantilevers and the tip-to-tip edge_beam survive with 0 validation errors
    regen = client.post(f"/api/projects/{pid}/geometry/generate", json={})
    assert regen.status_code == 200, regen.text
    sm_regen = regen.json()
    assert len([m for m in sm_regen["members"] if m["member_type"] == "cantilever_beam"]) == 2
    assert len([m for m in sm_regen["members"] if m["member_type"] == "edge_beam"]) == 1
    assert [i for i in sm_regen["validation_issues"] if i["severity"] == "error"] == []

    # Verify explicit end_node=None with replicate_all_floors=True also succeeds without 422
    n_a1 = next(n for n in sm_regen["nodes"] if n["floor_level"] == "2F" and abs(n["x_m"]) < 0.01 and abs(n["z_m"]) < 0.01)
    c_rep = client.post(
        f"/api/projects/{pid}/geometry/members",
        json={
            "start_node": n_a1["node_id"],
            "end_node": None,
            "end_xyz_m": [-1.2, 3.0, 0.0],
            "member_type": "cantilever_beam",
            "floor_level": "2F",
            "replicate_all_floors": True,
        },
    )
    assert c_rep.status_code == 200, c_rep.text
    sm_rep = c_rep.json()
    assert any(
        m["member_type"] == "cantilever_beam" and m["length_m"] == pytest.approx(1.2, abs=0.05)
        for m in sm_rep["members"]
    )
    assert [i for i in sm_rep["validation_issues"] if i["severity"] == "error"] == []


def test_spatial_retaining_wall_and_decoupled_roof_truss_reaction_transfer(isolated_env):
    """
    Verify:
    1. Spatial RetainingWallInput (floor_level, start_xyz_m, end_xyz_m, backfill_side) derives
       total_length_m from spatial endpoints and propagates spatial fields onto RetainingWallDesign.
    2. design_roof_truss() computes unfactored support reactions (r_dl_kn, r_lr_kn, r_wl_down_kn,
       r_wl_uplift_kn) and line loads (w_dl_kn_m, w_lr_kn_m) without adding truss stiffness
       members to the 3D RC frame.
    3. loads/engine.py generate_loads() transfers truss DL & LR onto ROOF beams and adds truss DL
       to ROOF seismic weight when include_roof_slab=False, is idempotent on re-run, and skips
       transfer when include_roof_slab=True (no double-counting with RC roof slabs).
    """
    client = isolated_env
    pid = "TEST-RW-TRUSS-01"
    payload = _make_2x2_project(pid)
    payload["levels"] = [
        {"id": "GF", "elevation_m": 0.0},
        {"id": "2F", "elevation_m": 3.0},
        {"id": "ROOF", "elevation_m": 6.0},
    ]
    storage.save_project(ProjectState.model_validate(payload))

    # Generate 3D frame (default include_roof_slab=False) and auto-size
    gen = client.post(f"/api/projects/{pid}/geometry/generate", json={"include_roof_slab": False})
    assert gen.status_code == 200, gen.text
    orig_member_count = len(gen.json()["members"])
    assert client.post(f"/api/projects/{pid}/sizing/auto-size", json={}).status_code == 200

    # 1. Place spatial retaining wall on GF from (0, 0, 0) -> (5, 0, 0)
    rw_res = client.post(
        f"/api/projects/{pid}/design/retaining-walls",
        json={
            "wall_id": "RW-SOUTH",
            "label": "South Perimeter Wall",
            "retained_height_m": 2.6,
            "total_length_m": 99.0,  # should be derived as 5.0 m from start_xyz_m -> end_xyz_m
            "floor_level": "GF",
            "start_xyz_m": [0.0, 0.0, 0.0],
            "end_xyz_m": [5.0, 0.0, 0.0],
            "backfill_side": "exterior",
        },
    )
    assert rw_res.status_code == 200, rw_res.text
    rw_designs = rw_res.json()["retaining_wall_designs"]
    assert len(rw_designs) == 1
    rw = rw_designs[0]
    assert rw["total_length_m"] == pytest.approx(5.0, abs=0.01)
    assert rw["floor_level"] == "GF"
    assert rw["start_xyz_m"] == [0.0, 0.0, 0.0]
    assert rw["end_xyz_m"] == [5.0, 0.0, 0.0]
    assert rw["backfill_side"] == "exterior"

    # 2. Baseline loads BEFORE roof truss
    lg_base = client.post(f"/api/projects/{pid}/loads/generate", json={})
    assert lg_base.status_code == 200, lg_base.text
    roof_wt_before = next(
        s["weight_kn"] for s in lg_base.json()["seismic_summary"]["stories"] if s["floor_level"] == "ROOF"
    )
    assert not any(bl["source"].startswith("roof_truss_") for bl in lg_base.json()["beam_loads"])

    # 3. Design standalone roof truss targeting ROOF beams -> computes reactions, does NOT alter RC members
    tr_res = client.post(
        f"/api/projects/{pid}/design/roof-trusses",
        json={
            "truss_id": "TR-MAIN",
            "span_m": 8.0,
            "truss_spacing_m": 2.5,
            "pitch_deg": 20.0,
            "truss_type": "Pratt",
            "target_floor_level": "ROOF",
            "transfer_to_roof_beams": True,
        },
    )
    assert tr_res.status_code == 200, tr_res.text
    tr = tr_res.json()["roof_truss_designs"][0]
    assert tr["r_dl_kn"] > 0.0
    assert tr["r_lr_kn"] > 0.0
    assert tr["r_wl_down_kn"] > 0.0
    assert tr["r_wl_uplift_kn"] > 0.0
    assert tr["w_dl_kn_m"] == pytest.approx(tr["r_dl_kn"] / 2.5, abs=0.01)
    assert tr["w_lr_kn_m"] == pytest.approx(tr["r_lr_kn"] / 2.5, abs=0.01)
    assert tr["transferred_to_roof_beams"] is True

    # 3D RC frame member count must remain completely decoupled (unchanged)
    proj_check = storage.load_project(pid)
    assert len(proj_check.structural_model.members) == orig_member_count

    # 4. Generate loads -> transfers truss DL & LR onto ROOF beams and increases ROOF seismic weight
    lg1 = client.post(f"/api/projects/{pid}/loads/generate", json={})
    assert lg1.status_code == 200, lg1.text
    truss_loads_1 = [bl for bl in lg1.json()["beam_loads"] if bl["source"].startswith("roof_truss_")]
    assert len(truss_loads_1) > 0
    assert any(bl["load_case"] == "DL" for bl in truss_loads_1)
    assert any(bl["load_case"] == "LR" for bl in truss_loads_1)
    roof_wt_after = next(
        s["weight_kn"] for s in lg1.json()["seismic_summary"]["stories"] if s["floor_level"] == "ROOF"
    )
    assert roof_wt_after > roof_wt_before

    # Idempotency check: re-running loads/generate yields identical count of truss beam loads
    lg2 = client.post(f"/api/projects/{pid}/loads/generate", json={})
    assert lg2.status_code == 200
    truss_loads_2 = [bl for bl in lg2.json()["beam_loads"] if bl["source"].startswith("roof_truss_")]
    assert len(truss_loads_2) == len(truss_loads_1)

    # 5. No double-counting: if include_roof_slab=True creates ROOF slab panels, truss loads are skipped
    gen_slab = client.post(f"/api/projects/{pid}/geometry/generate", json={"include_roof_slab": True})
    assert gen_slab.status_code == 200
    assert client.post(f"/api/projects/{pid}/sizing/auto-size", json={}).status_code == 200
    lg_slab = client.post(f"/api/projects/{pid}/loads/generate", json={})
    assert lg_slab.status_code == 200
    assert not any(bl["source"].startswith("roof_truss_") for bl in lg_slab.json()["beam_loads"])


def test_column_to_beam_t_junction_splits_host_beam_and_persists_on_regenerate(isolated_env):
    """
    Verify:
      1. Adding a manual beam from a column node onto the interior of an existing same-floor beam
         (via end_xyz_m) creates a shared T-junction node, splits the host beam into two connected
         segments, and records a ManualBeamOverride.
      2. Re-running POST /geometry/generate replays the exact same T-junction split cleanly,
         preserving slab panels and keeping cantilever/manual beams intact.
    """
    client = isolated_env
    pid = "TEST-TJUNC-SPLIT"
    storage.save_project(ProjectState.model_validate(_make_2x2_project(pid)))

    r_gen = client.post(f"/api/projects/{pid}/geometry/generate", json={})
    assert r_gen.status_code == 200, r_gen.text
    g0 = r_gen.json()
    orig_slab_count = len(g0["slab_panels"])
    assert orig_slab_count > 0

    nodes_2f = [n for n in g0["nodes"] if n["floor_level"] == "2F"]
    n_a1 = next(n for n in nodes_2f if abs(n["x_m"] - 0.0) < 0.01 and abs(n["z_m"] - 0.0) < 0.01)
    n_a2 = next(n for n in nodes_2f if abs(n["x_m"] - 0.0) < 0.01 and abs(n["z_m"] - 4.0) < 0.01)
    n_b2 = next(n for n in nodes_2f if abs(n["x_m"] - 5.0) < 0.01 and abs(n["z_m"] - 4.0) < 0.01)

    # Host beam on 2F runs from A-2 (0, 4) to B-2 (5, 4). Target T-junction midpoint at (2.5, y, 4.0).
    orig_host = next(
        (
            m
            for m in g0["members"]
            if m["floor_level"] == "2F"
            and {m["start_node"], m["end_node"]} == {n_a2["node_id"], n_b2["node_id"]}
        ),
        None,
    )
    assert orig_host is not None

    # 1. Add secondary beam from column node A-1 (0, 0) onto host beam midpoint (2.5, y, 4.0) via end_xyz_m
    r_add = client.post(
        f"/api/projects/{pid}/geometry/members",
        json={
            "start_node": n_a1["node_id"],
            "end_xyz_m": [2.5, n_a1["y_m"], 4.0],
            "member_type": "secondary_beam",
            "floor_level": "2F",
            "grid_ref": "TJUNC-1",
        },
    )
    assert r_add.status_code == 200, r_add.text
    g1 = r_add.json()

    # Shared T-junction node must exist at (2.5, y, 4.0)
    t_node = next(
        (
            n
            for n in g1["nodes"]
            if n["floor_level"] == "2F" and abs(n["x_m"] - 2.5) < 0.05 and abs(n["z_m"] - 4.0) < 0.05
        ),
        None,
    )
    assert t_node is not None, "Expected T-junction AnalyticalNode at (2.5, 4.0) on 2F"

    # Unsplit host beam (A-2 <-> B-2) must be gone and replaced by two segments meeting at t_node
    m_2f = [m for m in g1["members"] if m["floor_level"] == "2F" and m["member_type"] != "column"]
    assert not any({m["start_node"], m["end_node"]} == {n_a2["node_id"], n_b2["node_id"]} for m in m_2f)
    assert any({m["start_node"], m["end_node"]} == {n_a2["node_id"], t_node["node_id"]} for m in m_2f)
    assert any({m["start_node"], m["end_node"]} == {t_node["node_id"], n_b2["node_id"]} for m in m_2f)
    assert any(
        {m["start_node"], m["end_node"]} == {n_a1["node_id"], t_node["node_id"]}
        and m["member_type"] == "secondary_beam"
        for m in m_2f
    )
    assert len(g1["manual_beams"]) == 1

    # 2. Regenerate geometry -> T-junction split must replay identically and preserve slab panels
    r_regen = client.post(f"/api/projects/{pid}/geometry/generate", json={})
    assert r_regen.status_code == 200, r_regen.text
    g2 = r_regen.json()

    t_node_2 = next(
        (
            n
            for n in g2["nodes"]
            if n["floor_level"] == "2F" and abs(n["x_m"] - 2.5) < 0.05 and abs(n["z_m"] - 4.0) < 0.05
        ),
        None,
    )
    assert t_node_2 is not None, "T-junction node must persist across POST /geometry/generate"

    n2_a1 = next(n for n in g2["nodes"] if n["floor_level"] == "2F" and abs(n["x_m"] - 0.0) < 0.01 and abs(n["z_m"] - 0.0) < 0.01)
    n2_a2 = next(n for n in g2["nodes"] if n["floor_level"] == "2F" and abs(n["x_m"] - 0.0) < 0.01 and abs(n["z_m"] - 4.0) < 0.01)
    n2_b2 = next(n for n in g2["nodes"] if n["floor_level"] == "2F" and abs(n["x_m"] - 5.0) < 0.01 and abs(n["z_m"] - 4.0) < 0.01)
    m2_2f = [m for m in g2["members"] if m["floor_level"] == "2F" and m["member_type"] != "column"]

    assert not any({m["start_node"], m["end_node"]} == {n2_a2["node_id"], n2_b2["node_id"]} for m in m2_2f)
    assert any({m["start_node"], m["end_node"]} == {n2_a2["node_id"], t_node_2["node_id"]} for m in m2_2f)
    assert any({m["start_node"], m["end_node"]} == {t_node_2["node_id"], n2_b2["node_id"]} for m in m2_2f)
    assert any(
        {m["start_node"], m["end_node"]} == {n2_a1["node_id"], t_node_2["node_id"]}
        and m["member_type"] == "secondary_beam"
        for m in m2_2f
    )
    assert len(g2["slab_panels"]) >= orig_slab_count






