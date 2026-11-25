from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
import sys

import docx
import fitz
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import main as main_mod
from backend.app import storage as storage_mod
from backend.app.design.rc_design import (
    detect_footing_clashes,
    run_analysis_and_design,
)
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
    ReportState,
)
from backend.app.reports.bom_engine import compute_bom_summary
from backend.app.reports.docx_builder import build_calculation_report_docx
from backend.app.reports.pdf_builder import build_calculation_report_pdf
from backend.app.sizing.engine import auto_size_members

# Valid 1x1 red PNG as base64 data URL
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URL = f"data:image/png;base64,{_TINY_PNG_B64}"


def _make_analyzed_project(project_id: str, bay_x: float = 4.0) -> ProjectState:
    levels = [
        Level(id="GF", elevation_m=0.0),
        Level(id="2F", elevation_m=3.2),
    ]
    grids = Grids(
        x_grids=[GridLine(label="A", coord_m=0.0), GridLine(label="B", coord_m=bay_x)],
        y_grids=[GridLine(label="1", coord_m=0.0), GridLine(label="2", coord_m=4.0)],
    )
    cols = [
        Column(id="C-A1", grid="A-1", floor_level="GF", x_m=0.0, y_m=0.0, width_mm=400, depth_mm=400),
        Column(id="C-B1", grid="B-1", floor_level="GF", x_m=bay_x, y_m=0.0, width_mm=400, depth_mm=400),
        Column(id="C-A2", grid="A-2", floor_level="GF", x_m=0.0, y_m=4.0, width_mm=400, depth_mm=400),
        Column(id="C-B2", grid="B-2", floor_level="GF", x_m=bay_x, y_m=4.0, width_mm=400, depth_mm=400),
    ]
    p = ProjectState(
        project_info=ProjectInfo(
            project_id=project_id,
            project_name="Design Viewer Pass 1 Test",
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


def test_footing_clash_detection_flags_close_overlapping_pads():
    # Closely spaced columns (1.2 m apart in X) with 1.6 m+ pads -> guaranteed overlap
    p = _make_analyzed_project("CLASH-01", bay_x=1.2)
    for f in p.analysis_design.footing_designs:
        f.b_m = 1.8
        f.l_m = 1.8
    clashes = detect_footing_clashes(p, p.analysis_design.footing_designs)
    assert len(clashes) >= 2
    for c in clashes:
        assert c["overlap_area_m2"] > 0.0
        assert "overlap" in c["message"].lower()
        assert c["footing_a"] and c["footing_b"]


def test_patch_override_member_flips_failing_beam_and_footing_to_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path / "projects")
    p = _make_analyzed_project("OVR-01", bay_x=4.5)

    # Deliberately make the first beam fail flexure/shear via high demand & tiny bars
    bm = p.analysis_design.beam_designs[0]
    bm.mu_neg_start_knm = 120.0
    bm.bars_top_start = "2-12mm"
    bm.flexure_dcr = 2.5
    bm.governing_dcr = 2.5
    bm.status = "FAIL"

    # Deliberately make the first footing fail bearing with tiny pad
    ftg = p.analysis_design.footing_designs[0]
    ftg.b_m = 0.8
    ftg.l_m = 0.8
    ftg.bearing_dcr = 2.0
    ftg.governing_dcr = 2.0
    ftg.status = "FAIL"

    storage_mod.save_project(p)
    client = TestClient(main_mod.app)

    # 1. Override beam with deeper section + heavier top bars -> flips to PASS
    r_bm = client.patch(
        f"/api/projects/{p.project_info.project_id}/design/override-member",
        json={
            "target_type": "beam",
            "target_id": f"M{bm.member_id}",
            "updates": {
                "depth_mm": 550.0,
                "bars_top_start": "4-20mm",
                "bars_bot_mid": "4-20mm",
                "bars_top_end": "4-20mm",
                "stirrups_support": "10mm @ 100mm o.c.",
            },
        },
    )
    assert r_bm.status_code == 200, r_bm.text
    updated_bm = next(x for x in r_bm.json()["beam_designs"] if x["member_id"] == bm.member_id)
    assert updated_bm["status"] == "PASS"
    assert updated_bm["governing_dcr"] <= 1.0

    # 2. Override footing to a large pad -> flips to PASS and updates footing_clashes
    r_ftg = client.patch(
        f"/api/projects/{p.project_info.project_id}/design/override-member",
        json={
            "target_type": "footing",
            "target_id": f"N{ftg.node_id}",
            "updates": {
                "b_m": 2.4,
                "l_m": 2.4,
                "thickness_mm": 450.0,
            },
        },
    )
    assert r_ftg.status_code == 200, r_ftg.text
    updated_ftg = next(x for x in r_ftg.json()["footing_designs"] if x["node_id"] == ftg.node_id)
    assert updated_ftg["status"] == "PASS"
    assert updated_ftg["governing_dcr"] <= 1.0


def test_pin_snapshot_embeds_in_docx_and_pdf_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path / "projects")
    p = _make_analyzed_project("SNAP-01", bay_x=4.0)
    storage_mod.save_project(p)
    client = TestClient(main_mod.app)

    r_pin = client.post(
        f"/api/projects/{p.project_info.project_id}/design/pin-snapshot",
        json={
            "title": "2F Framing Plan — Governing DCR Map",
            "caption": "All beams <= 0.85 DCR under governing NSCP 2015 LRFD seismic combos.",
            "image_data_url": _TINY_PNG_DATA_URL,
        },
    )
    assert r_pin.status_code == 200, r_pin.text
    views = r_pin.json()["pinned_design_views"]
    assert len(views) == 1
    assert views[0]["title"] == "2F Framing Plan — Governing DCR Map"
    assert views[0]["id"].startswith("snap-")

    reloaded = storage_mod.load_project(p.project_info.project_id)
    reloaded.reporting = ReportState(
        bom=compute_bom_summary(reloaded, reloaded.reporting.bom_params)
    )

    # Verify DOCX embeds heading, title, caption, and inline picture
    docx_bytes = build_calculation_report_docx(reloaded)
    doc = docx.Document(BytesIO(docx_bytes))
    docx_text = "\n".join(par.text for par in doc.paragraphs)
    assert "Visual Design Verification & Utilization Maps" in docx_text
    assert "2F Framing Plan" in docx_text
    assert len(doc.inline_shapes) >= 1

    # Verify PDF embeds heading, title, and raster image
    pdf_bytes = build_calculation_report_pdf(reloaded)
    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pdf_text = "\n".join(page.get_text() for page in pdf_doc)
    assert "Visual Design Verification & Utilization Maps" in pdf_text
    assert "2F Framing Plan" in pdf_text
    total_imgs = sum(len(page.get_images()) for page in pdf_doc)
    assert total_imgs >= 1
