from __future__ import annotations

import io
from pathlib import Path
import sys
import zipfile

import pymupdf
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import auth, storage
from backend.app.geometry.generator import generate_structural_model
from backend.app.sizing.engine import auto_size_members
from backend.app.loads.engine import generate_loads
from backend.app.design.rc_design import run_analysis_and_design
from backend.app.design.retaining_wall import design_retaining_wall
from backend.app.models import (
    Column,
    EngineerSignatory,
    GridLine,
    Grids,
    Level,
    ProjectInfo,
    ProjectMaterialProperties,
    ProjectState,
    ReportState,
    RetainingWallInput,
)
from backend.app.profile import (
    EngineerProfile,
    load_engineer_profile,
    resolve_signatory,
    save_engineer_profile,
)
from backend.app.reports.bom_engine import compute_bom_summary
from backend.app.reports.docx_builder import build_calculation_report_docx
from backend.app.reports.pdf_builder import build_calculation_report_pdf
from backend.app.reports.render_3d_views import (
    render_3d_displaced_shape_png,
    render_3d_model_and_loads_png,
)


def _make_full_project(project_id: str = "REP-3D-01") -> ProjectState:
    p = ProjectState(
        project_info=ProjectInfo(
            project_id=project_id,
            project_name="3D Figure Report Frame",
            location="Baguio City, Philippines",
            design_code="NSCP 2015",
            materials=ProjectMaterialProperties(
                fc_mpa=28.0,
                fy_mpa=415.0,
                concrete_density_kn_m3=23.54,
            ),
        ),
        levels=[
            Level(id="GF", elevation_m=0.0),
            Level(id="2F", elevation_m=3.2),
            Level(id="ROOF", elevation_m=6.4),
        ],
        grids=Grids(
            x_grids=[GridLine(label="A", coord_m=0.0), GridLine(label="B", coord_m=5.0)],
            y_grids=[GridLine(label="1", coord_m=0.0), GridLine(label="2", coord_m=4.0)],
        ),
        columns=[
            Column(id="C-A1", grid="A-1", floor_level="GF", x_m=0.0, y_m=0.0, width_mm=400, depth_mm=400),
            Column(id="C-B1", grid="B-1", floor_level="GF", x_m=5.0, y_m=0.0, width_mm=400, depth_mm=400),
            Column(id="C-A2", grid="A-2", floor_level="GF", x_m=0.0, y_m=4.0, width_mm=400, depth_mm=400),
            Column(id="C-B2", grid="B-2", floor_level="GF", x_m=5.0, y_m=4.0, width_mm=400, depth_mm=400),
        ],
    )
    p.structural_model = generate_structural_model(p)
    p.member_sizing = auto_size_members(p)
    p.load_generation = generate_loads(p)
    ad, _ = run_analysis_and_design(p)
    rw_in = RetainingWallInput(
        wall_id="RW-1",
        retained_height_m=2.6,
        total_length_m=5.0,
        floor_level="GF",
        start_xyz_m=[0.0, 0.0, 0.0],
        end_xyz_m=[5.0, 0.0, 0.0],
    )
    ad.retaining_wall_inputs = [rw_in]
    ad.retaining_wall_designs = [design_retaining_wall(p, rw_in)]
    p.analysis_design = ad
    bom = compute_bom_summary(p, p.reporting.bom_params)
    p.reporting = ReportState(bom=bom)
    return p


def test_3d_report_figures_embedded_in_docx_and_pdf_full_and_minimal():
    p = _make_full_project("REP-3D-FULL")

    # 1. Direct PNG rendering checks
    png_model = render_3d_model_and_loads_png(p)
    png_disp = render_3d_displaced_shape_png(p)
    for raw in (png_model, png_disp):
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
        im = Image.open(io.BytesIO(raw))
        assert im.width >= 800 and im.height >= 500

    # 2. DOCX embeds at least 2 PNG images in word/media/
    docx_bytes = build_calculation_report_docx(p)
    with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as zf:
        media_pngs = [n for n in zf.namelist() if n.startswith("word/media/") and n.endswith(".png")]
        assert len(media_pngs) >= 2, f"Expected >= 2 embedded PNGs in DOCX, got {media_pngs}"

    # 3. PDF embeds at least 2 image XObjects across its pages
    pdf_bytes = build_calculation_report_pdf(p)
    pdf_doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    total_pdf_images = sum(len(page.get_images(full=True)) for page in pdf_doc)
    full_pdf_text = "\n".join(page.get_text() for page in pdf_doc)
    pdf_doc.close()
    assert total_pdf_images >= 2, f"Expected >= 2 embedded images in PDF, got {total_pdf_images}"
    assert "3D Structural Model & Applied Loads" in full_pdf_text
    assert "3D Displaced Shape & Story Drift Envelope" in full_pdf_text

    # 4. Minimal / sparse project fixture (empty nodes & story_drifts) never crashes
    minimal = ProjectState(
        project_info=ProjectInfo(
            project_id="REP-MIN-01",
            project_name="Minimal Fixture",
            location="Manila",
            design_code="NSCP 2015",
            materials=ProjectMaterialProperties(fc_mpa=28.0, fy_mpa=415.0, concrete_density_kn_m3=23.54),
        ),
        levels=[Level(id="GF", elevation_m=0.0)],
        grids=Grids(x_grids=[], y_grids=[]),
        columns=[],
    )
    min_png1 = render_3d_model_and_loads_png(minimal)
    min_png2 = render_3d_displaced_shape_png(minimal)
    assert min_png1.startswith(b"\x89PNG\r\n\x1a\n")
    assert min_png2.startswith(b"\x89PNG\r\n\x1a\n")
    min_docx = build_calculation_report_docx(minimal, include_bom=False)
    min_pdf = build_calculation_report_pdf(minimal, include_bom=False)
    with zipfile.ZipFile(io.BytesIO(min_docx), "r") as zf:
        assert len([n for n in zf.namelist() if n.startswith("word/media/")]) >= 2
    min_pdf_doc = pymupdf.open(stream=min_pdf, filetype="pdf")
    assert sum(len(pg.get_images(full=True)) for pg in min_pdf_doc) >= 2
    min_pdf_doc.close()


def test_resolve_signatory_prefills_from_profile_and_preserves_overrides_auth_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")

    # AUTH_ENABLED=false -> global engineer_profile.json
    tok = auth.current_user.set(None)
    try:
        prof_global = save_engineer_profile(
            EngineerProfile(
                full_name="Engr. Maria Santos, CE",
                prc_license_no="0123456",
                prc_validity="2028-12-31",
                ptr_no="PTR-778899",
                ptr_date_issued="2026-01-10",
                ptr_place_issued="Baguio City",
                tin="111-222-333-000",
            )
        )
        assert load_engineer_profile().full_name == "Engr. Maria Santos, CE"

        # Blank & placeholder fields fall back to EngineerProfile; explicit overrides win
        sig_in = EngineerSignatory(
            engineer_name="",
            prc_license_no="TBD",
            prc_validity="N/A",
            ptr_no="PTR-PROJECT-OVERRIDE",
            ptr_date="-",
            ptr_place="",
            tin="na",
            project_owner="Acme Builders",
            project_address="Session Rd, Baguio",
        )
        resolved = resolve_signatory(sig_in, prof_global)
        assert resolved["engineer_name"] == "Engr. Maria Santos, CE"
        assert resolved["prc_license_no"] == "0123456"
        assert resolved["prc_validity"] == "2028-12-31"
        assert resolved["ptr_no"] == "PTR-PROJECT-OVERRIDE"
        assert resolved["ptr_date"] == "2026-01-10"
        assert resolved["ptr_place"] == "Baguio City"
        assert resolved["tin"] == "111-222-333-000"
        assert resolved["project_owner"] == "Acme Builders"
        assert resolved["project_address"] == "Session Rd, Baguio"
    finally:
        auth.current_user.reset(tok)

    # AUTH_ENABLED=true -> per-user isolated engineer_profile.json under users/<username>/
    tok_u = auth.current_user.set("engr_juan")
    try:
        prof_user = save_engineer_profile(
            EngineerProfile(
                full_name="Engr. Juan Dela Cruz, CE",
                prc_license_no="0999888",
                prc_validity="2029-06-30",
                ptr_no="PTR-JUAN-01",
                ptr_date_issued="2026-02-01",
                ptr_place_issued="Makati City",
                tin="999-888-777-000",
            )
        )
        resolved_u = resolve_signatory(EngineerSignatory(), prof_user)
        assert resolved_u["engineer_name"] == "Engr. Juan Dela Cruz, CE"
        assert resolved_u["prc_license_no"] == "0999888"
        assert resolved_u["ptr_place"] == "Makati City"
    finally:
        auth.current_user.reset(tok_u)

    # Global profile remains untouched by per-user profile
    assert load_engineer_profile().full_name == "Engr. Maria Santos, CE"
