from __future__ import annotations

from io import BytesIO, StringIO
from pathlib import Path
import sys

import docx
import ezdxf
import fitz

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.cad.dxf_builder import build_dxf_bytes, get_available_cad_items
from backend.app.design.auto_resolve import AutoResolveRequest, run_auto_resolve
from backend.app.design.rc_design import run_analysis_and_design
from backend.app.design.retaining_wall import design_retaining_wall
from backend.app.main import app
from backend.app.design.secondary_elements import (
    design_roof_truss,
    design_stair,
    design_strap_footing,
)
from backend.app.geometry.generator import generate_structural_model
from backend.app.loads.engine import generate_loads
from backend.app.models import (
    CadDrawingOptions,
    CadSheetConfig,
    Column,
    GridLine,
    Grids,
    Level,
    ProjectInfo,
    ProjectMaterialProperties,
    ProjectState,
    ReportState,
    RetainingWallInput,
    RoofTrussInput,
    StairDesignInput,
)
from backend.app.reports.bom_engine import compute_bom_summary
from backend.app.reports.docx_builder import build_calculation_report_docx
from backend.app.reports.pdf_builder import build_calculation_report_pdf
from backend.app.sizing.engine import auto_size_members


def _make_frame(project_id: str = "SEC-TEST") -> ProjectState:
    levels = [
        Level(id="GF", elevation_m=0.0),
        Level(id="2F", elevation_m=3.2),
    ]
    grids = Grids(
        x_grids=[GridLine(label="A", coord_m=0.0), GridLine(label="B", coord_m=4.5)],
        y_grids=[GridLine(label="1", coord_m=0.0), GridLine(label="2", coord_m=4.0)],
    )
    cols = [
        Column(id="C-A1", grid="A-1", floor_level="GF", x_m=0.0, y_m=0.0, width_mm=400, depth_mm=400),
        Column(id="C-B1", grid="B-1", floor_level="GF", x_m=4.5, y_m=0.0, width_mm=400, depth_mm=400),
        Column(id="C-A2", grid="A-2", floor_level="GF", x_m=0.0, y_m=4.0, width_mm=400, depth_mm=400),
        Column(id="C-B2", grid="B-2", floor_level="GF", x_m=4.5, y_m=4.0, width_mm=400, depth_mm=400),
    ]
    p = ProjectState(
        project_info=ProjectInfo(
            project_id=project_id,
            project_name="Secondary Elements Frame",
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


def test_retaining_wall_stability_seismic_kae_and_rebar():
    p = _make_frame("RW-01")
    rw_ok = design_retaining_wall(
        p,
        RetainingWallInput(
            wall_id="RW-1",
            retained_height_m=2.6,
            base_width_m=2.8,
            friction_angle_deg=32.0,
        ),
    )
    assert rw_ok.ka > 0.25 and rw_ok.k0 > rw_ok.ka
    assert rw_ok.k_ae_seismic > rw_ok.ka
    assert rw_ok.fs_overturning >= 1.5
    assert rw_ok.fs_sliding >= 1.5
    assert rw_ok.status == "PASS"
    assert "mm @" in rw_ok.stem_vertical_bars
    assert "mm @" in rw_ok.base_heel_toe_bars

    # Deliberately unstable tall cantilevered wall with tiny base -> FAIL
    rw_fail = design_retaining_wall(
        p,
        RetainingWallInput(
            wall_id="RW-BAD",
            retained_height_m=5.0,
            base_width_m=1.0,
            is_restrained_top=False,
        ),
    )
    assert rw_fail.status == "FAIL"


def test_two_way_slab_schedule_auto_generated_on_design_run():
    p = _make_frame("SLAB-01")
    slabs = p.analysis_design.slab_designs
    assert len(slabs) >= 1
    for s in slabs:
        assert s.lx_m > 0 and s.ly_m >= s.lx_m
        assert "mm @" in s.short_span_cont_neg
        assert "mm @" in s.short_span_pos
        assert "mm @" in s.long_span_pos
        assert s.status in ("PASS", "FAIL")


def test_stair_roof_truss_strap_footing_and_bom_cad_report_integration():
    p = _make_frame("SEC-INT")

    # Baseline BOM with no optional secondary elements configured
    bom_base = compute_bom_summary(p, p.reporting.bom_params)
    assert bom_base.total_structural_steel_kg == 0.0
    assert all(r.element_group not in ("Retaining Walls", "RC Stairs", "Strap Beams") for r in bom_base.concrete_rows)

    # Configure retaining wall, stair, roof truss, and strap footing
    rw_in = RetainingWallInput(wall_id="RW-1", retained_height_m=2.8, total_length_m=10.0, base_width_m=2.0)
    st_in = StairDesignInput(stair_id="ST-1", flight_span_m=3.6, waist_thickness_mm=185.0)
    tr_in = RoofTrussInput(truss_id="TR-1", span_m=9.0, truss_spacing_m=3.0, pitch_deg=18.0, truss_type="Pratt")

    rw_res = design_retaining_wall(p, rw_in)
    st_res = design_stair(p, st_in)
    tr_res = design_roof_truss(p, tr_in)
    sup_nodes = [n.node_id for n in p.structural_model.nodes if n.is_support]
    sf_res = design_strap_footing(p, exterior_node_id=sup_nodes[0], interior_node_id=sup_nodes[1], strap_id="SF-1")

    assert st_res.mu_knm_per_m > 0 and st_res.deflection_status == "PASS"
    assert tr_res.estimated_steel_kg > 50.0 and tr_res.status == "PASS"
    assert sf_res.strap_h_mm >= 550.0 and sf_res.status == "PASS"

    p.analysis_design.retaining_wall_inputs = [rw_in]
    p.analysis_design.retaining_wall_designs = [rw_res]
    p.analysis_design.stair_inputs = [st_in]
    p.analysis_design.stair_designs = [st_res]
    p.analysis_design.roof_truss_inputs = [tr_in]
    p.analysis_design.roof_truss_designs = [tr_res]
    p.analysis_design.strap_footing_designs = [sf_res]

    # BOM must now include retaining wall/stair/strap concrete + rebar + roof truss structural steel
    bom_full = compute_bom_summary(p, p.reporting.bom_params)
    assert bom_full.total_concrete_net_m3 > bom_base.total_concrete_net_m3
    assert bom_full.total_rebar_kg > bom_base.total_rebar_kg
    assert bom_full.total_structural_steel_kg == round(tr_res.estimated_steel_kg, 2)
    assert bom_full.grand_total_cost_php > bom_base.grand_total_cost_php

    # DOCX & PDF reports include Secondary Elements & Retaining Walls
    p.reporting = ReportState(bom=bom_full)
    docx_bytes = build_calculation_report_docx(p)
    doc = docx.Document(BytesIO(docx_bytes))
    assert "Secondary Elements & Retaining Walls" in "\n".join(par.text for par in doc.paragraphs)

    pdf_bytes = build_calculation_report_pdf(p)
    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    assert "Secondary Elements & Retaining Walls" in "\n".join(page.get_text() for page in pdf_doc)

    # Module 07 CAD optional items are listed in available_items and render valid DXF when selected
    avail_keys = {it["key"] for it in get_available_cad_items(p)}
    assert {"detail_retaining_wall", "schedule_slabs_stairs", "detail_roof_truss"}.issubset(avail_keys)

    custom_opts = CadDrawingOptions(
        preset="custom",
        sheets=[
            CadSheetConfig(
                sheet_no="S-4",
                title="SECONDARY DETAILS",
                items=["schedule_slabs_stairs", "detail_retaining_wall", "detail_roof_truss"],
            )
        ],
    )
    dxf_bytes, cad_state = build_dxf_bytes(p, options=custom_opts)
    dxf_doc = ezdxf.read(StringIO(dxf_bytes.decode("utf-8")))
    assert len(dxf_doc.audit().errors) == 0
    assert cad_state.total_entities > 50


def test_module04_auto_size_preliminary_retaining_walls_and_preserves_spatial_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    p = _make_frame("RW-M04-AUTO")
    p.analysis_design.retaining_wall_inputs = [
        RetainingWallInput(
            wall_id="RW-SPATIAL",
            label="North Basement Wall",
            retained_height_m=3.0,
            total_length_m=4.5,
            stem_thickness_mm=180.0,
            base_width_m=0.8,
            toe_width_m=0.15,
            base_thickness_mm=200.0,
            floor_level="GF",
            start_xyz_m=[0.0, 0.0, 0.0],
            end_xyz_m=[4.5, 0.0, 0.0],
            backfill_side="left",
        )
    ]
    storage.save_project(p)

    client = TestClient(app)
    resp = client.post(
        f"/api/projects/{p.project_info.project_id}/sizing/auto-size",
        json={"preserve_manual_overrides": False},
    )
    assert resp.status_code == 200, resp.text

    loaded = storage.load_project(p.project_info.project_id)
    assert len(loaded.analysis_design.retaining_wall_inputs) == 1
    rw = loaded.analysis_design.retaining_wall_inputs[0]
    # H = 3.0 m -> B = ceil(1.8 * 10)/10 = 1.80, toe = 0.45, stem = 300, base = 350
    assert rw.base_width_m == 1.8
    assert rw.toe_width_m == 0.45
    assert rw.stem_thickness_mm == 300.0
    assert rw.base_thickness_mm == 350.0
    # Spatial fields preserved
    assert rw.floor_level == "GF"
    assert rw.start_xyz_m == [0.0, 0.0, 0.0]
    assert rw.end_xyz_m == [4.5, 0.0, 0.0]
    assert rw.backfill_side == "left"
    # retaining_wall_designs synced immediately
    assert len(loaded.analysis_design.retaining_wall_designs) == 1
    rw_des = loaded.analysis_design.retaining_wall_designs[0]
    assert rw_des.wall_id == "RW-SPATIAL"
    assert rw_des.start_xyz_m == [0.0, 0.0, 0.0]
    assert rw_des.end_xyz_m == [4.5, 0.0, 0.0]
    assert rw_des.backfill_side == "left"


def test_auto_resolve_converges_failing_retaining_wall_to_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    p = _make_frame("RW-AUTORESOLVE")
    # Narrow base_width_m=1.2 for H=2.8m -> fails overturning/sliding initially
    failing_rw = RetainingWallInput(
        wall_id="RW-1",
        label="Perimeter Wall",
        retained_height_m=2.8,
        total_length_m=6.0,
        stem_thickness_mm=250.0,
        base_width_m=1.2,
        toe_width_m=0.3,
        base_thickness_mm=300.0,
        friction_angle_deg=32.0,
        is_restrained_top=True,
        floor_level="GF",
        start_xyz_m=[0.0, 0.0, 0.0],
        end_xyz_m=[6.0, 0.0, 0.0],
        backfill_side="exterior",
    )
    initial_des = design_retaining_wall(p, failing_rw)
    assert initial_des.status == "FAIL"

    p.analysis_design.retaining_wall_inputs = [failing_rw]
    p.analysis_design.retaining_wall_designs = [initial_des]
    storage.save_project(p)

    # First verify upsize_retaining_walls=False leaves it untouched
    p_disabled = storage.load_project(p.project_info.project_id)
    res_disabled = run_auto_resolve(
        p_disabled,
        AutoResolveRequest(max_iterations=3, upsize_retaining_walls=False),
    )
    assert p_disabled.analysis_design.retaining_wall_inputs[0].base_width_m == 1.2
    assert res_disabled.history[0]["fail_retaining_walls"] == 0

    # Now test via HTTP endpoint with upsize_retaining_walls=True (default)
    client = TestClient(app)
    resp = client.post(
        f"/api/projects/{p.project_info.project_id}/design/auto-resolve",
        json={"max_iterations": 10, "upsize_retaining_walls": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["converged"] is True
    assert data["iterations_run"] >= 2
    assert data["history"][0]["fail_retaining_walls"] == 1
    assert any("Upsized retaining wall RW-1" in a for h in data["history"] for a in h["actions"])
    assert data["analysis_design"]["retaining_wall_designs"][0]["status"] == "PASS"

    saved = storage.load_project(p.project_info.project_id)
    assert saved.analysis_design.retaining_wall_inputs[0].base_width_m > 1.2
    assert saved.analysis_design.retaining_wall_designs[0].status == "PASS"
    assert saved.analysis_design.retaining_wall_inputs[0].start_xyz_m == [0.0, 0.0, 0.0]


def test_nodal_displacements_persisted_and_3d_displaced_report_figure():
    from backend.app.reports.render_3d_views import render_3d_displaced_shape_png

    p = _make_frame("NODAL-3D")
    nd = p.analysis_design.nodal_displacements_mm
    assert "EX+" in nd and "EZ+" in nd
    assert len(nd["EX+"]) == len(p.structural_model.nodes)
    for nid, vec in nd["EX+"].items():
        assert isinstance(nid, str)
        assert isinstance(vec, list) and len(vec) == 3
        assert all(isinstance(v, float) for v in vec)

    # Render with true per-node displacements
    png_nodal = render_3d_displaced_shape_png(p)
    assert png_nodal[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png_nodal) > 5_000

    # Render with legacy empty nodal_displacements_mm fallback
    p.analysis_design.nodal_displacements_mm = {}
    png_fallback = render_3d_displaced_shape_png(p)
    assert png_fallback[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png_fallback) > 5_000


def test_auto_resolve_thickens_failing_slab_to_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    p = _make_frame("SLAB-AUTORESOLVE")
    assert len(p.member_sizing.slab_assignments) >= 1

    # Force first slab to an undersized 75 mm thickness (below t_min_mm)
    target_sa = p.member_sizing.slab_assignments[0]
    target_sa.thickness_mm = 75.0
    target_sa.manual_override = False
    p.load_generation = generate_loads(p)
    ad, _ = run_analysis_and_design(p)
    p.analysis_design = ad
    assert any(s.status != "PASS" for s in p.analysis_design.slab_designs)
    storage.save_project(p)

    # upsize_slabs=False should leave slab thickness untouched
    p_disabled = storage.load_project(p.project_info.project_id)
    res_disabled = run_auto_resolve(
        p_disabled,
        AutoResolveRequest(max_iterations=2, upsize_slabs=False),
    )
    assert p_disabled.member_sizing.slab_assignments[0].thickness_mm == 75.0
    assert res_disabled.history[0]["fail_slabs"] == 0

    # HTTP Auto-Resolve with default upsize_slabs=True thickens in +25 mm steps until PASS
    client = TestClient(app)
    resp = client.post(
        f"/api/projects/{p.project_info.project_id}/design/auto-resolve",
        json={"max_iterations": 8, "upsize_slabs": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["converged"] is True
    assert data["history"][0]["fail_slabs"] >= 1
    assert any("Thickened slab" in a for h in data["history"] for a in h["actions"])
    assert all(s["status"] == "PASS" for s in data["analysis_design"]["slab_designs"])

    saved = storage.load_project(p.project_info.project_id)
    new_t = saved.member_sizing.slab_assignments[0].thickness_mm
    assert new_t > 75.0 and (new_t - 75.0) % 25.0 == 0.0


def test_approval_emits_slab_and_retaining_wall_warning_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    p = _make_frame("APPROVE-WARN")
    # Ensure primary frame passes first
    res = run_auto_resolve(p, AutoResolveRequest(max_iterations=6))
    assert res.converged is True

    # Introduce a failing slab and a failing retaining wall
    p.member_sizing.slab_assignments[0].thickness_mm = 75.0
    p.member_sizing.slab_assignments[0].manual_override = False
    failing_rw = RetainingWallInput(
        wall_id="RW-WARN",
        retained_height_m=4.5,
        base_width_m=1.0,
        is_restrained_top=False,
    )
    rw_des = design_retaining_wall(p, failing_rw)
    assert rw_des.status == "FAIL"
    p.analysis_design.retaining_wall_inputs = [failing_rw]
    p.analysis_design.retaining_wall_designs = [rw_des]

    # Re-run analysis so slab_designs reflects the 75 mm failing slab and warning issues are emitted
    p.load_generation = generate_loads(p)
    ad, _ = run_analysis_and_design(p)
    p.analysis_design = ad
    issue_codes = {iss.code: iss.severity for iss in ad.validation_issues}
    assert issue_codes.get("SLAB_FAIL") == "warning"
    assert issue_codes.get("RETAINING_WALL_FAIL") == "warning"

    storage.save_project(p)
    client = TestClient(app)
    resp = client.post(f"/api/projects/{p.project_info.project_id}/design/approve")
    assert resp.status_code == 200, resp.text
    approved = resp.json()
    assert approved["engineer_approved"] is True
    approved_codes = {iss["code"]: iss["severity"] for iss in approved["validation_issues"]}
    assert approved_codes.get("SLAB_FAIL") == "warning"
    assert approved_codes.get("RETAINING_WALL_FAIL") == "warning"
