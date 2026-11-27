from contextlib import asynccontextmanager
import os
from pathlib import Path
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from .auth import (
    authenticate_request,
    clear_session_cookie,
    create_session_token,
    current_user,
    is_auth_enabled,
    set_session_cookie,
    verify_credentials,
)
from .models import ProjectRevisionSnapshot, ProjectState
from .profile import EngineerProfile, load_engineer_profile, save_engineer_profile
from .storage import (
    delete_project,
    ensure_data_dirs,
    get_data_dir,
    get_exports_dir,
    list_projects,
    load_project,
    save_project,
)
from .staad import write_staad_preview, simulate_placeholder
from .pdf_importer import router as pdf_importer_router
from .pdf_importer import cad_importer as pdf_cad_importer
from .pdf_importer.router import _handle_error as _handle_pdf_error
from .pdf_importer.schemas import ImportDocumentState
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

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    ensure_data_dirs()
    yield


def _build_cors_config() -> tuple[list[str], bool]:
    raw = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000",
    ).strip()
    if raw == "*":
        return ["*"], False
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins:
        return ["*"], False
    return origins, True


_cors_origins, _cors_credentials = _build_cors_config()

app = FastAPI(title="AI Structural Engineering Office", version="0.1.0", lifespan=_lifespan)


@app.middleware("http")
async def _beta_auth_middleware(request: Request, call_next):
    if not is_auth_enabled():
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if not (path == "/api" or path.startswith("/api/")):
        return await call_next(request)
    if path == "/api/health" or path == "/api/auth" or path.startswith("/api/auth/"):
        return await call_next(request)

    username = authenticate_request(request)
    if not username:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    token = current_user.set(username)
    try:
        return await call_next(request)
    finally:
        current_user.reset(token)


# Register CORSMiddleware AFTER auth middleware so CORSMiddleware is outermost
# and 401 responses still include valid CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(pdf_importer_router)
app.include_router(geometry_router)
app.include_router(sizing_router)
app.include_router(loads_router)
app.include_router(design_router)
app.include_router(reports_router)
app.include_router(cad_router)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.1.0", "data_dir": str(get_data_dir())}


@app.get("/api/auth/me")
def auth_me(request: Request):
    enabled = is_auth_enabled()
    user = authenticate_request(request) if enabled else None
    return JSONResponse(
        content={
            "auth_enabled": enabled,
            "authenticated": bool(user) if enabled else False,
            "username": user if enabled else None,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/auth/login")
def auth_login(payload: LoginRequest, request: Request):
    if not is_auth_enabled():
        return {"status": "ok", "username": None, "auth_enabled": False}
    username = verify_credentials(payload.username, payload.password)
    if not username:
        return JSONResponse(status_code=401, content={"detail": "Invalid username or password."})
    token = create_session_token(username)
    resp = JSONResponse(content={"status": "ok", "username": username})
    set_session_cookie(resp, request, token)
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    resp = JSONResponse(content={"status": "ok"})
    clear_session_cookie(resp, request)
    return resp


@app.get("/api/profile")
def get_profile():
    profile = load_engineer_profile()
    return JSONResponse(
        content=profile.model_dump(),
        headers={"Cache-Control": "no-store"},
    )


@app.put("/api/profile", response_model=EngineerProfile)
def put_profile(payload: EngineerProfile):
    return save_engineer_profile(payload)

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


@app.delete("/api/projects/{project_id}")
def remove_project(project_id: str):
    try:
        return delete_project(project_id)
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


@app.post("/api/projects/{project_id}/pdf-import/upload-cad", response_model=ImportDocumentState)
async def upload_cad_plan_alias(project_id: str, file: UploadFile = File(...)):
    try:
        content = await file.read()
        return pdf_cad_importer.upload_cad_plan(project_id, file.filename or "plan.dxf", content)
    except Exception as exc:
        raise _handle_pdf_error(exc)


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


# Single-container static frontend serving (mounted AFTER all /api routers)
FRONTEND_DIST = (Path(__file__).resolve().parents[2] / "frontend" / "dist").resolve()
if FRONTEND_DIST.is_dir():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend_spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found.")
        if full_path:
            candidate = (FRONTEND_DIST / full_path).resolve()
            if candidate.is_relative_to(FRONTEND_DIST) and candidate.is_file():
                return FileResponse(candidate)
        index_html = FRONTEND_DIST / "index.html"
        if index_html.is_file():
            return FileResponse(index_html)
        raise HTTPException(status_code=404, detail="Frontend bundle not found.")
