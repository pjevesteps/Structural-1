from __future__ import annotations

from pathlib import Path
import sys

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import main as main_mod
from backend.app import storage as storage_mod
from backend.app.design.foundation_suite import compute_net_bearing_kpa
from backend.app.design.rc_design import run_analysis_and_design
from backend.app.geometry.generator import generate_structural_model
from backend.app.loads.engine import generate_loads
from backend.app.models import (
    Column,
    GridLine,
    Grids,
    Level,
    ProjectInfo,
    ProjectMaterialProperties,
    ProjectState,
    SoilInvestigationParams,
)
from backend.app.sizing.engine import auto_size_members
from backend.app.staad import write_staad_preview


def _make_project(project_id: str, bay_x: float = 4.0) -> ProjectState:
    levels = [
        Level(id="GF", elevation_m=0.0),
        Level(id="2F", elevation_m=3.2),
        Level(id="RD", elevation_m=6.4),
    ]
    grids = Grids(
        x_grids=[GridLine(label="A", coord_m=0.0), GridLine(label="B", coord_m=bay_x)],
        y_grids=[GridLine(label="1", coord_m=0.0), GridLine(label="2", coord_m=4.0)],
    )
    cols = []
    for fl in ("GF", "2F"):
        for gx, xl in ((0.0, "A"), (bay_x, "B")):
            for gz, zl in ((0.0, "1"), (4.0, "2")):
                cols.append(
                    Column(
                        id=f"C-{fl}-{xl}{zl}",
                        grid=f"{xl}-{zl}",
                        floor_level=fl,
                        x_m=gx,
                        y_m=gz,
                        width_mm=400,
                        depth_mm=400,
                    )
                )
    p = ProjectState(
        project_info=ProjectInfo(
            project_id=project_id,
            project_name="Foundation Suite Test",
            location="Baguio City, Philippines",
            design_code="NSCP 2015",
            materials=ProjectMaterialProperties(
                fc_mpa=28.0,
                fy_mpa=415.0,
                concrete_density_kn_m3=23.54,
            ),
        ),
        levels=levels,
        grids=grids,
        columns=cols,
    )
    p.structural_model = generate_structural_model(p)
    p.member_sizing = auto_size_members(p)
    p.load_generation = generate_loads(p)
    ad, _ = run_analysis_and_design(p)
    p.analysis_design = ad
    return p


def test_soil_parameters_and_water_table_affect_qnet_and_footing_sizes(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path / "projects")
    p = _make_project("SOIL-01", bay_x=4.5)
    storage_mod.save_project(p)

    q_dry, _, _ = compute_net_bearing_kpa(p, 400.0)
    base_avg_b = sum(f.b_m for f in p.analysis_design.footing_designs) / len(
        p.analysis_design.footing_designs
    )

    client = TestClient(main_mod.app)
    r = client.put(
        f"/api/projects/{p.project_info.project_id}/design/soil-parameters",
        json={
            "soil_investigation": {
                "report_ref": "BH-01 Soft Clay",
                "allowable_bearing_kpa": 100.0,
                "soil_unit_weight_kn_m3": 19.0,
                "embedment_depth_m": 2.0,
                "water_table_depth_m": 0.5,
                "friction_angle_deg": 28.0,
                "cohesion_kpa": 10.0,
                "subgrade_modulus_ks_kn_m3": 12000.0,
            },
            "recalculate": True,
        },
    )
    assert r.status_code == 200, r.text
    reloaded = storage_mod.load_project(p.project_info.project_id)
    q_wet, _, _ = compute_net_bearing_kpa(reloaded, 400.0)
    assert q_wet < q_dry
    assert q_wet >= 50.0  # floored at 0.5 * qa = 50 kPa

    wet_avg_b = sum(f.b_m for f in reloaded.analysis_design.footing_designs) / len(
        reloaded.analysis_design.footing_designs
    )
    assert wet_avg_b > base_avg_b


def test_merge_clashes_persists_across_rerun_and_unmerges_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path / "projects")
    # Close bay_x = 1.2 m so isolated pads clash along X
    p = _make_project("CF-01", bay_x=1.2)
    p.soil_investigation = SoilInvestigationParams(allowable_bearing_kpa=90.0)
    ad, _ = run_analysis_and_design(p)
    p.analysis_design = ad
    assert len(p.analysis_design.footing_clashes) >= 2
    initial_iso_count = len(p.analysis_design.footing_designs)
    storage_mod.save_project(p)

    client = TestClient(main_mod.app)
    r_merge = client.post(
        f"/api/projects/{p.project_info.project_id}/design/merge-footings",
        json={"merge_all_clashes": True},
    )
    assert r_merge.status_code == 200, r_merge.text
    body = r_merge.json()
    assert len(body["combined_footing_designs"]) >= 1
    assert body["combined_footing_designs"][0]["combined_id"] == "CF-1"
    assert len(body["footing_designs"]) < initial_iso_count
    assert len(body["footing_clashes"]) == 0

    # Full design re-run must preserve merged_footing_groups and CF-1
    r_rerun = client.post(f"/api/projects/{p.project_info.project_id}/design/run")
    assert r_rerun.status_code == 200, r_rerun.text
    rerun_body = r_rerun.json()
    assert len(rerun_body["combined_footing_designs"]) >= 1
    assert rerun_body["combined_footing_designs"][0]["combined_id"] == "CF-1"

    # Unmerge CF-1 restores isolated pads
    r_del = client.delete(f"/api/projects/{p.project_info.project_id}/design/merge-footings/CF-1")
    assert r_del.status_code == 200, r_del.text
    del_body = r_del.json()
    assert len(del_body["combined_footing_designs"]) == len(body["combined_footing_designs"]) - 1
    assert len(del_body["footing_designs"]) > len(body["footing_designs"])


def test_mat_raft_foundation_design_and_staad_winkler_springs(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path / "projects")
    p = _make_project("MAT-01", bay_x=4.0)
    storage_mod.save_project(p)

    client = TestClient(main_mod.app)
    r = client.put(
        f"/api/projects/{p.project_info.project_id}/design/soil-parameters",
        json={
            "foundation_system": "mat_raft",
            "soil_investigation": {
                "allowable_bearing_kpa": 120.0,
                "subgrade_modulus_ks_kn_m3": 15000.0,
            },
            "recalculate": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["footing_designs"] == []
    assert body["combined_footing_designs"] == []
    assert body["footing_clashes"] == []
    mat = body["mat_foundation_design"]
    assert mat is not None
    assert mat["mat_id"] == "MAT-1"
    assert mat["thickness_mm"] >= 400.0
    assert mat["q_max_kpa"] > 0.0
    assert mat["max_punching_dcr"] <= 1.0
    assert "mm @" in mat["top_bars_x"]
    assert "mm @" in mat["bot_bars_z"]

    # Verify STAAD export emits Winkler KFY springs for mat_raft
    reloaded = storage_mod.load_project(p.project_info.project_id)
    std_path = tmp_path / "mat_raft.std"
    write_staad_preview(reloaded, std_path)
    std_text = std_path.read_text(encoding="utf-8")
    assert "WINKLER MAT SPRINGS" in std_text
    assert "FIXED BUT FY KFY" in std_text
