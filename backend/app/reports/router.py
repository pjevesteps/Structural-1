from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict

from .. import storage
from ..models import (
    BomCostParameters,
    EngineerSignatory,
    ReportGenerationOptions,
    ReportState,
)
from ..profile import load_engineer_profile
from .bom_engine import export_bom_csv, generate_report_state
from .docx_builder import build_calculation_report_docx
from .pdf_builder import build_calculation_report_pdf

router = APIRouter(
    prefix="/api/projects/{project_id}/reports",
    tags=["reports-bom"],
)


class GenerateReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signatory: EngineerSignatory | None = None
    bom_params: BomCostParameters | None = None
    options: ReportGenerationOptions | None = None


def _load_or_404(project_id: str):
    try:
        return storage.load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _ensure_report_ready(project):
    if project.reporting.bom is None:
        try:
            project.reporting = generate_report_state(project)
            storage.save_project(project)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@router.post("/generate", response_model=ReportState)
def generate_report(project_id: str, payload: GenerateReportRequest | None = None):
    project = _load_or_404(project_id)
    req = payload if payload is not None else GenerateReportRequest()
    try:
        state = generate_report_state(
            project,
            signatory=req.signatory,
            bom_params=req.bom_params,
            options=req.options,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    project.reporting = state
    storage.save_project(project)
    return state


@router.get("/download/docx")
def download_report_docx(project_id: str):
    project = _load_or_404(project_id)
    _ensure_report_ready(project)
    profile = load_engineer_profile()
    try:
        docx_bytes = build_calculation_report_docx(project, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_Structural_Calculation_Report.docx"',
        },
    )


@router.get("/download/pdf")
def download_report_pdf(project_id: str):
    project = _load_or_404(project_id)
    _ensure_report_ready(project)
    profile = load_engineer_profile()
    try:
        pdf_bytes = build_calculation_report_pdf(project, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_Structural_Calculation_Report.pdf"',
        },
    )


@router.get("/download/bom-csv")
def download_bom_csv(project_id: str):
    project = _load_or_404(project_id)
    _ensure_report_ready(project)
    try:
        csv_text = export_bom_csv(project)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_Structural_BOM.csv"',
        },
    )
