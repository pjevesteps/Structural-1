from __future__ import annotations

from datetime import datetime, timezone

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
from .bom_engine import (
    compute_bom_summary,
    compute_readiness_warnings,
    export_bom_csv,
    generate_report_state,
)
from .deliverables import (
    bom_artifact_path,
    check_bom_prerequisites,
    check_report_prerequisites,
    get_deliverables_status,
    persist_bom_artifact,
    persist_structural_report_artifacts,
    report_artifact_paths,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/reports",
    tags=["reports-bom"],
)


class GenerateReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signatory: EngineerSignatory | None = None
    bom_params: BomCostParameters | None = None
    options: ReportGenerationOptions | None = None


class GenerateStructuralReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signatory: EngineerSignatory | None = None
    options: ReportGenerationOptions | None = None


class GenerateBomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signatory: EngineerSignatory | None = None
    bom_params: BomCostParameters | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_or_404(project_id: str):
    try:
        return storage.load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _apply_signatory(project, signatory: EngineerSignatory | None) -> EngineerSignatory:
    active_sig = (
        signatory.model_copy(deep=True)
        if signatory is not None
        else project.reporting.signatory.model_copy(deep=True)
    )
    if not active_sig.project_address.strip() and project.project_info.location.strip():
        active_sig.project_address = project.project_info.location.strip()
    return active_sig


def _ensure_report_ready(project):
    if project.reporting.bom is None:
        try:
            project.reporting = generate_report_state(project)
            storage.save_project(project)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


def _build_and_store_structural_report(project, profile) -> dict:
    from .docx_builder import build_calculation_report_docx
    from .pdf_builder import build_calculation_report_pdf

    include_bom = bool(project.reporting.options.include_bom_cost_estimate and project.reporting.bom is not None)
    docx_bytes = build_calculation_report_docx(project, profile=profile, include_bom=include_bom)
    pdf_bytes = build_calculation_report_pdf(project, profile=profile, include_bom=include_bom)
    return persist_structural_report_artifacts(project, docx_bytes, pdf_bytes, profile=profile)


# =============================================================================
# DECOUPLED DELIVERABLE STATUS
# =============================================================================


@router.get("/status")
def get_reports_status(project_id: str):
    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    status = get_deliverables_status(project, profile=profile)
    status["readiness_warnings"] = compute_readiness_warnings(
        project, project.reporting.signatory, profile=profile
    )
    return status


# =============================================================================
# DECOUPLED STRUCTURAL ANALYSIS & DESIGN REPORT WORKFLOW
# =============================================================================


@router.post("/structural-report/generate")
def generate_structural_report(project_id: str, payload: GenerateStructuralReportRequest | None = None):
    project = _load_or_404(project_id)
    missing = check_report_prerequisites(project)
    if missing:
        raise HTTPException(status_code=400, detail=" ".join(missing))

    req = payload if payload is not None else GenerateStructuralReportRequest()
    active_sig = _apply_signatory(project, req.signatory)
    active_opts = (
        req.options.model_copy(deep=True)
        if req.options is not None
        else project.reporting.options.model_copy(deep=True)
    )
    profile = load_engineer_profile()
    project.reporting.signatory = active_sig
    project.reporting.options = active_opts
    project.reporting.readiness_warnings = compute_readiness_warnings(project, active_sig, profile=profile)
    if project.reporting.generated_at is None:
        project.reporting.generated_at = _utc_now()

    try:
        _build_and_store_structural_report(project, profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    storage.save_project(project)
    deliverables = get_deliverables_status(project, profile=profile)
    return {
        "deliverable": "structural_report",
        **deliverables["structural_report"],
        "deliverables": deliverables,
        "reporting": project.reporting.model_dump(),
    }


def _serve_stored_structural_artifact(project_id: str, fmt: str, inline: bool = False) -> Response:
    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    status = get_deliverables_status(project, profile=profile)["structural_report"]
    docx_path, pdf_path = report_artifact_paths(project_id)
    target = pdf_path if fmt == "pdf" else docx_path

    if not target.is_file():
        missing_prereqs = status["missing_prerequisites"]
        if missing_prereqs:
            raise HTTPException(
                status_code=400,
                detail="Structural Report cannot be downloaded yet: " + " ".join(missing_prereqs),
            )
        raise HTTPException(
            status_code=404,
            detail="Structural Report has not been generated yet. Click 'Generate Structural Report' first.",
        )

    media_type = (
        "application/pdf"
        if fmt == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    disp = "inline" if inline else "attachment"
    return Response(
        content=target.read_bytes(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disp}; filename="{target.name}"',
            "X-Deliverable-Status": status["status"],
        },
    )


@router.get("/structural-report/download/docx")
def download_structural_report_docx(project_id: str):
    return _serve_stored_structural_artifact(project_id, fmt="docx", inline=False)


@router.get("/structural-report/download/pdf")
def download_structural_report_pdf(project_id: str):
    return _serve_stored_structural_artifact(project_id, fmt="pdf", inline=False)


@router.get("/structural-report/preview/pdf")
def preview_structural_report_pdf(project_id: str):
    return _serve_stored_structural_artifact(project_id, fmt="pdf", inline=True)


# =============================================================================
# DECOUPLED BILL OF MATERIALS (BOM & COST ESTIMATE) WORKFLOW
# =============================================================================


@router.post("/bom/generate")
def generate_bom_only(project_id: str, payload: GenerateBomRequest | None = None):
    project = _load_or_404(project_id)
    missing = check_bom_prerequisites(project)
    if missing:
        raise HTTPException(status_code=400, detail=" ".join(missing))

    req = payload if payload is not None else GenerateBomRequest()
    active_sig = _apply_signatory(project, req.signatory)
    active_bom_params = (
        req.bom_params.model_copy(deep=True)
        if req.bom_params is not None
        else project.reporting.bom_params.model_copy(deep=True)
    )

    try:
        bom = compute_bom_summary(project, active_bom_params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    profile = load_engineer_profile()
    project.reporting.signatory = active_sig
    project.reporting.bom_params = active_bom_params
    project.reporting.bom = bom
    project.reporting.readiness_warnings = compute_readiness_warnings(project, active_sig, profile=profile)
    if project.reporting.generated_at is None:
        project.reporting.generated_at = _utc_now()

    csv_text = export_bom_csv(project)
    persist_bom_artifact(project, csv_text, profile=profile)
    storage.save_project(project)

    deliverables = get_deliverables_status(project, profile=profile)
    return {
        "deliverable": "bom",
        **deliverables["bom"],
        "bom": bom.model_dump(),
        "deliverables": deliverables,
        "reporting": project.reporting.model_dump(),
    }


@router.get("/bom/download/csv")
def download_bom_only_csv(project_id: str):
    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    status = get_deliverables_status(project, profile=profile)["bom"]
    csv_path = bom_artifact_path(project_id)

    if not csv_path.is_file():
        missing_prereqs = status["missing_prerequisites"]
        if missing_prereqs:
            raise HTTPException(
                status_code=400,
                detail="BOM cannot be exported yet: " + " ".join(missing_prereqs),
            )
        raise HTTPException(
            status_code=404,
            detail="BOM has not been generated yet. Click 'Generate BOM & Cost Estimate' first.",
        )

    return PlainTextResponse(
        content=csv_path.read_text(encoding="utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{csv_path.name}"',
            "X-Deliverable-Status": status["status"],
        },
    )


# =============================================================================
# LEGACY COMBINED ENDPOINTS (100% BACKWARD COMPATIBLE)
# =============================================================================


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
    profile = load_engineer_profile()
    # Persist both deliverable artifacts so independent status reflects 'ready'
    csv_text = export_bom_csv(project)
    persist_bom_artifact(project, csv_text, profile=profile)
    _build_and_store_structural_report(project, profile)
    storage.save_project(project)
    return state


@router.get("/download/docx")
def download_report_docx(project_id: str):
    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    status = get_deliverables_status(project, profile=profile)["structural_report"]
    docx_path, _ = report_artifact_paths(project_id)

    if docx_path.is_file() and status["status"] == "ready":
        return Response(
            content=docx_path.read_bytes(),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f'attachment; filename="{docx_path.name}"',
                "X-Deliverable-Status": status["status"],
            },
        )

    _ensure_report_ready(project)
    from .docx_builder import build_calculation_report_docx

    try:
        docx_bytes = build_calculation_report_docx(project, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_Structural_Calculation_Report.docx"',
            "X-Deliverable-Status": status["status"],
        },
    )


@router.get("/download/pdf")
def download_report_pdf(project_id: str):
    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    status = get_deliverables_status(project, profile=profile)["structural_report"]
    _, pdf_path = report_artifact_paths(project_id)

    if pdf_path.is_file() and status["status"] == "ready":
        return Response(
            content=pdf_path.read_bytes(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{pdf_path.name}"',
                "X-Deliverable-Status": status["status"],
            },
        )

    _ensure_report_ready(project)
    from .pdf_builder import build_calculation_report_pdf

    try:
        pdf_bytes = build_calculation_report_pdf(project, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_Structural_Calculation_Report.pdf"',
            "X-Deliverable-Status": status["status"],
        },
    )


@router.get("/download/bom-csv")
def download_bom_csv(project_id: str):
    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    status = get_deliverables_status(project, profile=profile)["bom"]
    csv_path = bom_artifact_path(project_id)

    if csv_path.is_file() and status["status"] == "ready":
        return PlainTextResponse(
            content=csv_path.read_text(encoding="utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{csv_path.name}"',
                "X-Deliverable-Status": status["status"],
            },
        )

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
            "X-Deliverable-Status": status["status"],
        },
    )
