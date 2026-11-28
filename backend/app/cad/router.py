from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from .. import storage
from ..models import CadDrawingOptions, CadDrawingState
from ..profile import load_engineer_profile
from .dwg_converter import (
    DwgConverterUnavailableError,
    convert_dxf_to_dwg,
    detect_dwg_converter,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/cad",
    tags=["cad-drawings"],
)


class GenerateCadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: CadDrawingOptions | None = None


def _load_or_404(project_id: str):
    try:
        return storage.load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/generate", response_model=CadDrawingState)
def generate_cad_drawings(project_id: str, payload: GenerateCadRequest | None = None):
    from .dxf_builder import build_dxf_bytes

    project = _load_or_404(project_id)
    req = payload if payload is not None else GenerateCadRequest()
    profile = load_engineer_profile()
    try:
        _, state = build_dxf_bytes(project, options=req.options, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    project.cad_drawings = state
    storage.save_project(project)
    return state


@router.get("/dwg-status")
def get_cad_dwg_status(project_id: str):
    _load_or_404(project_id)
    return detect_dwg_converter()


@router.get("/download/dwg")
def download_cad_dwg(project_id: str):
    from .dxf_builder import build_dxf_bytes

    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    try:
        dxf_bytes, state = build_dxf_bytes(project, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if project.cad_drawings.generated_at is None:
        project.cad_drawings = state
        storage.save_project(project)

    try:
        dwg_bytes = convert_dxf_to_dwg(
            dxf_bytes,
            dwg_version=project.cad_drawings.options.dwg_version,
        )
    except DwgConverterUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return Response(
        content=dwg_bytes,
        media_type="application/acad",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_Structural_Plans.dwg"',
        },
    )


@router.get("/download/dxf")
def download_cad_dxf(project_id: str):
    from .dxf_builder import build_dxf_bytes

    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    try:
        dxf_bytes, state = build_dxf_bytes(project, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if project.cad_drawings.generated_at is None:
        project.cad_drawings = state
        storage.save_project(project)

    return Response(
        content=dxf_bytes,
        media_type="application/dxf",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_Structural_Plans.dxf"',
        },
    )


@router.get("/download/pdf")
def download_cad_pdf(project_id: str):
    from .dxf_builder import build_sheets_pdf

    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    try:
        pdf_bytes = build_sheets_pdf(project, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_Structural_Plans.pdf"',
        },
    )


@router.get("/download/zip")
def download_cad_zip(project_id: str):
    from .dxf_builder import build_sheets_zip

    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    try:
        zip_bytes = build_sheets_zip(project, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_Structural_Plans_Sheets.zip"',
        },
    )


@router.get("/preview-svg")
def preview_cad_sheet_svg(
    project_id: str,
    sheet_no: str | None = Query(default=None),
):
    from .dxf_builder import render_sheet_svg

    project = _load_or_404(project_id)
    profile = load_engineer_profile()
    try:
        return render_sheet_svg(project, sheet_no=sheet_no, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
