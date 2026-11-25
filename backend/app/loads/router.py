from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict

from .. import storage
from ..models import (
    FloorGravityLoad,
    LoadGenerationOptions,
    LoadGenerationState,
    SeismicParameters,
    WindParameters,
)
from ..staad import write_staad_preview
from .engine import generate_loads
from .validator import validate_loads

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["load-generation"],
)


class GenerateLoadsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    floor_loads: list[FloorGravityLoad] | None = None
    seismic: SeismicParameters | None = None
    wind: WindParameters | None = None
    options: LoadGenerationOptions | None = None


class UpdateLoadsConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    floor_loads: list[FloorGravityLoad] | None = None
    seismic: SeismicParameters | None = None
    wind: WindParameters | None = None
    options: LoadGenerationOptions | None = None


def _load_or_404(project_id: str):
    try:
        return storage.load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/loads/generate", response_model=LoadGenerationState)
def run_generate_loads(project_id: str, payload: GenerateLoadsRequest | None = None):
    project = _load_or_404(project_id)
    req = payload if payload is not None else GenerateLoadsRequest()
    try:
        lg = generate_loads(
            project,
            floor_loads=req.floor_loads,
            seismic=req.seismic,
            wind=req.wind,
            options=req.options,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    project.load_generation = lg
    storage.save_project(project)
    return lg


@router.patch("/loads/config", response_model=LoadGenerationState)
def update_loads_config(project_id: str, payload: UpdateLoadsConfigRequest):
    project = _load_or_404(project_id)
    try:
        lg = generate_loads(
            project,
            floor_loads=payload.floor_loads,
            seismic=payload.seismic,
            wind=payload.wind,
            options=payload.options,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    project.load_generation = lg
    storage.save_project(project)
    return lg


@router.post("/loads/approve", response_model=LoadGenerationState)
def approve_loads(project_id: str):
    project = _load_or_404(project_id)
    lg = project.load_generation
    if lg.generated_at is None:
        raise HTTPException(
            status_code=400,
            detail="Loads have not been generated yet. Run Generate NSCP Loads first.",
        )

    lg.validation_issues = validate_loads(project)
    errors = [iss for iss in lg.validation_issues if iss.severity == "error"]
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve loads while {len(errors)} error(s) remain: {errors[0].message}",
        )

    lg.engineer_approved = True
    storage.save_project(project)
    return lg


@router.get("/staad/download")
def download_staad_file(project_id: str):
    project = _load_or_404(project_id)
    dest = Path(__file__).resolve().parents[3] / "data" / "exports" / f"{project_id}.std"
    try:
        result = write_staad_preview(project, dest)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid geometry reference: {exc}")

    return PlainTextResponse(
        content=result["std_preview"],
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}.std"',
        },
    )
