from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .inspector import InvalidPdfError, PdfTooLargeError
from .schemas import (
    AuditEntry,
    BulkReviewRequest,
    CandidateReviewUpdate,
    ExtractRequest,
    ExtractionResponse,
    ImportDocumentState,
    ImportDocumentSummary,
    ManualColumnRequest,
    ManualGridRequest,
    ScaleCalibrationRequest,
    TransferRequest,
    TransferResponse,
)
from . import service

router = APIRouter(
    prefix="/api/projects/{project_id}/pdf-imports",
    tags=["pdf-importer"],
)


def _handle_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else exc))
    if isinstance(exc, PdfTooLargeError):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, (InvalidPdfError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("", response_model=ImportDocumentState)
async def upload_pdf_plan(project_id: str, file: UploadFile = File(...)):
    try:
        content = await file.read()
        return service.upload_pdf(project_id, file.filename or "plan.pdf", content)
    except Exception as exc:
        raise _handle_error(exc)


@router.get("", response_model=list[ImportDocumentSummary])
def list_pdf_imports(project_id: str):
    try:
        return service.list_imports(project_id)
    except Exception as exc:
        raise _handle_error(exc)


@router.get("/{doc_id}", response_model=ImportDocumentState)
def get_pdf_import(project_id: str, doc_id: str):
    try:
        return service.get_import(project_id, doc_id)
    except Exception as exc:
        raise _handle_error(exc)


@router.get("/{doc_id}/audit", response_model=list[AuditEntry])
def get_pdf_import_audit(project_id: str, doc_id: str):
    try:
        return service.read_audit_log(project_id, doc_id)
    except Exception as exc:
        raise _handle_error(exc)


@router.get("/{doc_id}/pages/{page_number}/image")
def get_pdf_page_image(project_id: str, doc_id: str, page_number: int):
    try:
        img_path = service.get_page_image_path(project_id, doc_id, page_number)
        return FileResponse(path=img_path, media_type="image/png")
    except Exception as exc:
        raise _handle_error(exc)


@router.post("/{doc_id}/pages/{page_number}/calibrate", response_model=ImportDocumentState)
def calibrate_pdf_page(
    project_id: str,
    doc_id: str,
    page_number: int,
    payload: ScaleCalibrationRequest,
):
    try:
        return service.calibrate_page(project_id, doc_id, page_number, payload)
    except Exception as exc:
        raise _handle_error(exc)


@router.post(
    "/{doc_id}/pages/{page_number}/confirm-calibration",
    response_model=ImportDocumentState,
)
def confirm_pdf_page_calibration(project_id: str, doc_id: str, page_number: int):
    try:
        return service.confirm_calibration(project_id, doc_id, page_number)
    except Exception as exc:
        raise _handle_error(exc)


@router.post("/{doc_id}/pages/{page_number}/extract", response_model=ExtractionResponse)
def extract_pdf_page(
    project_id: str,
    doc_id: str,
    page_number: int,
    payload: ExtractRequest | None = None,
):
    try:
        req = payload if payload is not None else ExtractRequest()
        return service.extract_page(project_id, doc_id, page_number, req)
    except Exception as exc:
        raise _handle_error(exc)


@router.post("/{doc_id}/candidates/columns", response_model=ImportDocumentState)
def add_manual_column_candidate(
    project_id: str,
    doc_id: str,
    payload: ManualColumnRequest,
):
    try:
        return service.add_manual_column(project_id, doc_id, payload)
    except Exception as exc:
        raise _handle_error(exc)


@router.post("/{doc_id}/candidates/grids", response_model=ImportDocumentState)
def add_manual_grid_candidate(
    project_id: str,
    doc_id: str,
    payload: ManualGridRequest,
):
    try:
        return service.add_manual_grid(project_id, doc_id, payload)
    except Exception as exc:
        raise _handle_error(exc)


@router.patch("/{doc_id}/candidates/{candidate_id}", response_model=ImportDocumentState)
def review_candidate(
    project_id: str,
    doc_id: str,
    candidate_id: str,
    payload: CandidateReviewUpdate,
):
    try:
        return service.update_candidate(project_id, doc_id, candidate_id, payload)
    except Exception as exc:
        raise _handle_error(exc)


@router.post("/{doc_id}/candidates/bulk-review", response_model=ImportDocumentState)
def bulk_review(
    project_id: str,
    doc_id: str,
    payload: BulkReviewRequest,
):
    try:
        return service.bulk_review_candidates(project_id, doc_id, payload)
    except Exception as exc:
        raise _handle_error(exc)


@router.post("/{doc_id}/transfer", response_model=TransferResponse)
def transfer_approved_to_project(
    project_id: str,
    doc_id: str,
    payload: TransferRequest | None = None,
):
    try:
        req = payload if payload is not None else TransferRequest()
        return service.transfer_approved(project_id, doc_id, req)
    except Exception as exc:
        raise _handle_error(exc)
