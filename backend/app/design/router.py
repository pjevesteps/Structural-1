from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict

from .. import storage
from ..models import (
    AnalysisDesignState,
    DesignParameters,
    MemberOverrideRequest,
    PinDesignSnapshotRequest,
    RetainingWallInput,
    RoofTrussInput,
    StairDesignInput,
)
from .auto_resolve import AutoResolveRequest, AutoResolveResponse, run_auto_resolve
from .rc_design import (
    export_schedules_csv,
    override_member_design,
    run_analysis_and_design,
    run_design_from_anl,
)
from .retaining_wall import design_retaining_wall
from .secondary_elements import design_roof_truss, design_stair, design_strap_footing

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


class StrapFootingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strap_id: str = "SF-1"
    exterior_node_id: int
    interior_node_id: int


@router.post("/retaining-walls", response_model=AnalysisDesignState)
def upsert_retaining_wall(project_id: str, payload: RetainingWallInput):
    project = _load_or_404(project_id)
    ad = project.analysis_design
    ad.retaining_wall_inputs = [
        x for x in ad.retaining_wall_inputs if x.wall_id != payload.wall_id
    ] + [payload]
    ad.retaining_wall_designs = [design_retaining_wall(project, rw) for rw in ad.retaining_wall_inputs]
    storage.save_project(project)
    return ad


@router.delete("/retaining-walls/{wall_id}", response_model=AnalysisDesignState)
def delete_retaining_wall(project_id: str, wall_id: str):
    project = _load_or_404(project_id)
    ad = project.analysis_design
    ad.retaining_wall_inputs = [x for x in ad.retaining_wall_inputs if x.wall_id != wall_id]
    ad.retaining_wall_designs = [x for x in ad.retaining_wall_designs if x.wall_id != wall_id]
    storage.save_project(project)
    return ad


@router.post("/stairs", response_model=AnalysisDesignState)
def upsert_stair(project_id: str, payload: StairDesignInput):
    project = _load_or_404(project_id)
    ad = project.analysis_design
    ad.stair_inputs = [x for x in ad.stair_inputs if x.stair_id != payload.stair_id] + [payload]
    ad.stair_designs = [design_stair(project, st) for st in ad.stair_inputs]
    storage.save_project(project)
    return ad


@router.delete("/stairs/{stair_id}", response_model=AnalysisDesignState)
def delete_stair(project_id: str, stair_id: str):
    project = _load_or_404(project_id)
    ad = project.analysis_design
    ad.stair_inputs = [x for x in ad.stair_inputs if x.stair_id != stair_id]
    ad.stair_designs = [x for x in ad.stair_designs if x.stair_id != stair_id]
    storage.save_project(project)
    return ad


@router.post("/roof-trusses", response_model=AnalysisDesignState)
def upsert_roof_truss(project_id: str, payload: RoofTrussInput):
    project = _load_or_404(project_id)
    ad = project.analysis_design
    ad.roof_truss_inputs = [x for x in ad.roof_truss_inputs if x.truss_id != payload.truss_id] + [payload]
    ad.roof_truss_designs = [design_roof_truss(project, tr) for tr in ad.roof_truss_inputs]
    storage.save_project(project)
    return ad


@router.delete("/roof-trusses/{truss_id}", response_model=AnalysisDesignState)
def delete_roof_truss(project_id: str, truss_id: str):
    project = _load_or_404(project_id)
    ad = project.analysis_design
    ad.roof_truss_inputs = [x for x in ad.roof_truss_inputs if x.truss_id != truss_id]
    ad.roof_truss_designs = [x for x in ad.roof_truss_designs if x.truss_id != truss_id]
    storage.save_project(project)
    return ad


@router.post("/strap-footings", response_model=AnalysisDesignState)
def upsert_strap_footing(project_id: str, payload: StrapFootingRequest):
    project = _load_or_404(project_id)
    try:
        res = design_strap_footing(
            project,
            exterior_node_id=payload.exterior_node_id,
            interior_node_id=payload.interior_node_id,
            strap_id=payload.strap_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ad = project.analysis_design
    ad.strap_footing_designs = [
        x for x in ad.strap_footing_designs if x.strap_id != payload.strap_id
    ] + [res]
    storage.save_project(project)
    return ad


@router.delete("/strap-footings/{strap_id}", response_model=AnalysisDesignState)
def delete_strap_footing(project_id: str, strap_id: str):
    project = _load_or_404(project_id)
    ad = project.analysis_design
    ad.strap_footing_designs = [x for x in ad.strap_footing_designs if x.strap_id != strap_id]
    storage.save_project(project)
    return ad


@router.patch("/override-member", response_model=AnalysisDesignState)
def patch_override_member(project_id: str, payload: MemberOverrideRequest):
    project = _load_or_404(project_id)
    try:
        ad = override_member_design(project, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    storage.save_project(project)
    return ad


@router.post("/pin-snapshot", response_model=AnalysisDesignState)
def post_pin_snapshot(project_id: str, payload: PinDesignSnapshotRequest):
    project = _load_or_404(project_id)
    ad = project.analysis_design
    ad.pinned_design_views.append(
        {
            "id": f"snap-{uuid.uuid4().hex[:8]}",
            "title": payload.title.strip(),
            "caption": payload.caption.strip(),
            "image_data_url": payload.image_data_url,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    storage.save_project(project)
    return ad
