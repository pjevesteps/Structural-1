from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from .models import ProjectRevisionSnapshot, ProjectState
from .storage import save_project, load_project, list_projects, get_exports_dir
from .staad import write_staad_preview, simulate_placeholder
from .pdf_importer import router as pdf_importer_router
from .geometry import router as geometry_router
from .sizing import router as sizing_router
from .loads import router as loads_router
from .design import router as design_router
from .reports import router as reports_router
from .reports.bom_engine import (
    compare_against_revision,
    create_revision_snapshot,
    get_regional_cost_presets,
    restore_revision_snapshot,
)
from .cad import router as cad_router

app = FastAPI(title="AI Structural Engineering Office", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(pdf_importer_router)
app.include_router(geometry_router)
app.include_router(sizing_router)
app.include_router(loads_router)
app.include_router(design_router)
app.include_router(reports_router)
app.include_router(cad_router)

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.1.0"}

@app.get("/api/projects")
def projects():
    return list_projects()

@app.post("/api/projects")
def create_project(project: ProjectState):
    try:
        save_project(project)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"saved": True, "project_id": project.project_info.project_id}

@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    try:
        return load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/projects/{project_id}/validate")
def validate_project(project_id: str):
    try:
        p = load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    warnings = []
    if len(p.levels) < 2: warnings.append("At least two levels are needed for vertical members.")
    if not p.columns: warnings.append("No columns are defined.")
    if not p.beams: warnings.append("No beams are defined.")
    return {"valid_schema": True, "warnings": warnings, "engineering_review_required": True}

@app.post("/api/projects/{project_id}/staad-preview")
def staad_preview(project_id: str):
    try:
        p = load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    path = get_exports_dir() / f"{project_id}_geometry_preview.std"
    try:
        return write_staad_preview(p, path)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid geometry reference: {exc}")

@app.post("/api/projects/{project_id}/simulate")
def simulate(project_id: str):
    try:
        p = load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    return simulate_placeholder(p)


class CreateRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="Revision Snapshot", max_length=200)
    notes: str = Field(default="", max_length=1000)


@app.get("/api/reports/cost-presets")
def list_regional_cost_presets():
    return {"presets": get_regional_cost_presets()}


@app.post("/api/projects/{project_id}/revisions", response_model=ProjectRevisionSnapshot)
def create_project_revision(project_id: str, payload: CreateRevisionRequest):
    try:
        p = load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    snap = create_revision_snapshot(p, label=payload.label, notes=payload.notes)
    p.revisions.append(snap)
    save_project(p)
    return snap


@app.post("/api/projects/{project_id}/revisions/{revision_id}/restore", response_model=ProjectState)
def restore_project_revision(project_id: str, revision_id: str):
    try:
        p = load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        restored = restore_revision_snapshot(p, revision_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    save_project(restored)
    return restored


@app.get("/api/projects/{project_id}/revisions/compare")
def compare_project_revision(
    project_id: str,
    base_rev: str = Query(..., min_length=1),
):
    try:
        p = load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        return compare_against_revision(p, base_rev)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
