from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys

import docx
import fitz

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.design.auto_resolve import AutoResolveRequest, run_auto_resolve
from backend.app.design.rc_design import run_analysis_and_design
from backend.app.geometry.generator import generate_structural_model
from backend.app.loads.engine import generate_loads
from backend.app.models import (
    AnalysisDesignState,
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


def _make_2story_frame(project_id: str = "SMRF-TEST") -> ProjectState:
    levels = [
        Level(id="GF", elevation_m=0.0),
        Level(id="2F", elevation_m=3.2),
        Level(id="RD", elevation_m=6.4),
    ]
    grids = Grids(
        x_grids=[GridLine(label="A", coord_m=0.0), GridLine(label="B", coord_m=5.0), GridLine(label="C", coord_m=10.0)],
        y_grids=[GridLine(label="1", coord_m=0.0), GridLine(label="2", coord_m=5.0)],
    )
    cols = []
    gx_labels = [(0.0, "A"), (5.0, "B"), (10.0, "C")]
    gz_labels = [(0.0, "1"), (5.0, "2")]
    for fl in ("GF", "2F"):
        for gx, xl in gx_labels:
            for gz, zl in gz_labels:
                cols.append(
                    Column(
                        id=f"C-{fl}-{xl}{zl}",
                        grid=f"{xl}-{zl}",
                        floor_level=fl,
                        x_m=gx,
                        y_m=gz,
                        width_mm=400.0,
                        depth_mm=400.0,
                    )
                )

    p = ProjectState(
        project_info=ProjectInfo(
            project_id=project_id,
            project_name="SMRF SCWB Verification Frame",
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
    return p


def test_smrf_scwb_and_joint_shear_populated_and_in_reports():
    p = _make_2story_frame("SMRF-01")
    ad, _ = run_analysis_and_design(p)
    p.analysis_design = ad

    # 6 columns x 2 elevated floors (2F & RD) = 12 elevated non-support joints
    assert len(ad.smrf_joint_checks) == 12
    for j in ad.smrf_joint_checks:
        assert j.joint_node_id >= 1
        assert j.floor_level in ("2F", "RD")
        assert j.sum_mnc_knm > 0.0
        assert j.sum_mnb_knm > 0.0
        assert abs(j.scwb_ratio - round(j.sum_mnc_knm / j.sum_mnb_knm, 3)) <= 0.01
        assert j.scwb_status == ("PASS" if j.scwb_ratio >= 1.20 else "FAIL")
        assert j.joint_shear_phi_vn_kn > 0.0
        assert j.joint_shear_dcr >= 0.0
        assert j.joint_shear_status == ("PASS" if j.joint_shear_dcr <= 1.0 else "FAIL")

    # Verify DOCX & PDF reports include the NSCP Sec. 418 SMRF Joint & SCWB Summary table
    p.reporting = ReportState(bom=compute_bom_summary(p, p.reporting.bom_params))
    docx_bytes = build_calculation_report_docx(p)
    doc = docx.Document(BytesIO(docx_bytes))
    full_docx_text = "\n".join(par.text for par in doc.paragraphs)
    assert "NSCP Sec. 418 SMRF Joint" in full_docx_text

    pdf_bytes = build_calculation_report_pdf(p)
    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    full_pdf_text = "\n".join(page.get_text() for page in pdf_doc)
    assert "NSCP Sec. 418 SMRF Joint" in full_pdf_text


def test_smrf_scwb_pass_vs_fail_sensitivity():
    p = _make_2story_frame("SMRF-02")

    # Force weak columns (250x250) with deep heavy beams (350x650) -> guaranteed SCWB < 1.20 failure
    for sec in p.member_sizing.sections:
        if sec.category == "column":
            sec.width_mm = 250.0
            sec.depth_mm = 250.0
        else:
            sec.width_mm = 350.0
            sec.depth_mm = 650.0
    p.load_generation = generate_loads(p)
    ad_weak, _ = run_analysis_and_design(p)

    fail_joints = [j for j in ad_weak.smrf_joint_checks if j.scwb_status == "FAIL"]
    assert len(fail_joints) > 0
    assert any(j.scwb_ratio < 1.20 for j in fail_joints)
    assert any(iss.code == "SMRF_SCWB_FAIL" for iss in ad_weak.validation_issues)

    # Now upsize columns to 650x650 -> SCWB ratio should increase dramatically and pass
    for sec in p.member_sizing.sections:
        if sec.category == "column":
            sec.width_mm = 650.0
            sec.depth_mm = 650.0
    p.load_generation = generate_loads(p)
    ad_strong, _ = run_analysis_and_design(p)

    min_weak = min(j.scwb_ratio for j in ad_weak.smrf_joint_checks)
    min_strong = min(j.scwb_ratio for j in ad_strong.smrf_joint_checks)
    assert min_strong > min_weak * 2.0
    assert all(j.scwb_status == "PASS" for j in ad_strong.smrf_joint_checks)
    assert all(j.joint_shear_status == "PASS" for j in ad_strong.smrf_joint_checks)


def test_auto_resolve_enforce_scwb_upsizes_columns():
    p = _make_2story_frame("SMRF-03")

    # Start with small columns (300x300) and deep beams (300x600) so SCWB fails
    for sec in p.member_sizing.sections:
        if sec.category == "column":
            sec.width_mm = 300.0
            sec.depth_mm = 300.0
        else:
            sec.width_mm = 300.0
            sec.depth_mm = 600.0
    p.load_generation = generate_loads(p)

    ad_before, _ = run_analysis_and_design(p)
    initial_scwb_fails = sum(1 for j in ad_before.smrf_joint_checks if j.scwb_status == "FAIL")
    assert initial_scwb_fails > 0

    initial_col_sizes = {
        s.section_id: (s.width_mm, s.depth_mm)
        for s in p.member_sizing.sections
        if s.category == "column"
    }

    res = run_auto_resolve(
        p,
        AutoResolveRequest(max_iterations=8, enforce_scwb=True),
    )

    final_col_sizes = {
        s.section_id: (s.width_mm, s.depth_mm)
        for s in p.member_sizing.sections
        if s.category == "column"
    }
    # Verify Auto-Resolve upsized at least one column section
    assert any(
        final_col_sizes[sid][0] > initial_col_sizes[sid][0]
        for sid in initial_col_sizes
    )
    assert res.iterations_run >= 2
    assert any("NSCP Sec. 418 SMRF" in act for h in res.history for act in h["actions"])
    final_scwb_fails = sum(
        1 for j in res.analysis_design.smrf_joint_checks if j.scwb_status == "FAIL"
    )
    assert final_scwb_fails < initial_scwb_fails


def test_smrf_backward_compatibility_legacy_json():
    # Legacy AnalysisDesignState JSON without smrf_joint_checks must parse cleanly under extra="forbid"
    legacy_payload = {
        "generated_at": "2026-01-01T00:00:00Z",
        "source": "internal_dsm",
        "engineer_approved": False,
    }
    state = AnalysisDesignState.model_validate(legacy_payload)
    assert state.smrf_joint_checks == []
