from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from .models import ProjectState
from .storage import save_project, load_project, list_projects, get_exports_dir
from .staad import write_staad_preview, simulate_placeholder
from .pdf_importer import router as pdf_importer_router
from .geometry import router as geometry_router
from .sizing import router as sizing_router
from .loads import router as loads_router
from .design import router as design_router
from .reports import router as reports_router
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
