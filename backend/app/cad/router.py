from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from .. import storage
from ..models import CadDrawingOptions, CadDrawingState
from .dxf_builder import (
    build_dxf_bytes,
    build_sheets_pdf,
    build_sheets_zip,
    render_sheet_svg,
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
    project = _load_or_404(project_id)
    req = payload if payload is not None else GenerateCadRequest()
    try:
        _, state = build_dxf_bytes(project, options=req.options)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    project.cad_drawings = state
    storage.save_project(project)
    return state


@router.get("/download/dxf")
def download_cad_dxf(project_id: str):
    project = _load_or_404(project_id)
    try:
        dxf_bytes, state = build_dxf_bytes(project)
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
    project = _load_or_404(project_id)
    try:
        pdf_bytes = build_sheets_pdf(project)
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
    project = _load_or_404(project_id)
    try:
        zip_bytes = build_sheets_zip(project)
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
    project = _load_or_404(project_id)
    try:
        return render_sheet_svg(project, sheet_no=sheet_no)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
