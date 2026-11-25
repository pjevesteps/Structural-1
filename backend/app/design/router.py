from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict

from .. import storage
from ..models import AnalysisDesignState, DesignParameters
from .auto_resolve import AutoResolveRequest, AutoResolveResponse, run_auto_resolve
from .rc_design import export_schedules_csv, run_analysis_and_design, run_design_from_anl

router = APIRouter(
    prefix="/api/projects/{project_id}/design",
    tags=["analysis-design"],
)


class RunDesignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    params: DesignParameters | None = None


def _load_or_404(project_id: str):
    try:
        return storage.load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/run", response_model=AnalysisDesignState)
def run_internal_design(project_id: str, payload: RunDesignRequest | None = None):
    project = _load_or_404(project_id)
    req = payload if payload is not None else RunDesignRequest()
    try:
        state, _ = run_analysis_and_design(project, params=req.params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    project.analysis_design = state
    storage.save_project(project)
    return state


@router.post("/auto-resolve", response_model=AutoResolveResponse)
def auto_resolve_design(project_id: str, payload: AutoResolveRequest | None = None):
    project = _load_or_404(project_id)
    req = payload if payload is not None else AutoResolveRequest()
    try:
        result = run_auto_resolve(project, req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    storage.save_project(project)
    return result


@router.post("/upload-anl", response_model=AnalysisDesignState)
async def upload_staad_anl(project_id: str, file: UploadFile = File(...)):
    project = _load_or_404(project_id)
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded .anl file is empty.")
    anl_text = raw_bytes.decode("utf-8", errors="replace")

    try:
        state, _ = run_design_from_anl(
            project,
            anl_text=anl_text,
            filename=file.filename or "uploaded.anl",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    project.analysis_design = state
    storage.save_project(project)
    return state


@router.post("/auto-resolve", response_model=AutoResolveResponse)
def auto_resolve_design(project_id: str, payload: AutoResolveRequest | None = None):
    project = _load_or_404(project_id)
    req = payload if payload is not None else AutoResolveRequest()
    try:
        result = run_auto_resolve(project, req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    storage.save_project(project)
    return result


@router.post("/approve", response_model=AnalysisDesignState)
def approve_design(project_id: str):
    project = _load_or_404(project_id)
    ad = project.analysis_design
    if ad.generated_at is None:
        raise HTTPException(
            status_code=400,
            detail="Analysis & RC design have not been run yet. Run Module 06 first.",
        )

    errors = [iss for iss in ad.validation_issues if iss.severity == "error"]
    failing_members = (
        sum(1 for b in ad.beam_designs if b.status == "FAIL")
        + sum(1 for c in ad.column_designs if c.status == "FAIL")
        + sum(1 for f in ad.footing_designs if f.status == "FAIL")
        + sum(1 for d in ad.story_drifts if d.status == "FAIL")
    )
    if errors or failing_members > 0:
        detail = (
            errors[0].message
            if errors
            else f"{failing_members} failing drift/member/footing check(s) remain."
        )
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve design while failing checks remain: {detail}",
        )

    ad.engineer_approved = True
    storage.save_project(project)
    return ad


@router.get("/export-csv")
def export_design_csv(project_id: str):
    project = _load_or_404(project_id)
    if project.analysis_design.generated_at is None:
        raise HTTPException(
            status_code=400,
            detail="Run Analysis & RC Design before exporting reinforcement schedules.",
        )
    csv_text = export_schedules_csv(project)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_rc_schedules.csv"',
        },
    )
