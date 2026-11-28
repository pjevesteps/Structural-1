from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4

import pymupdf

from .. import storage
from ..models import Column, FloorOpening, GridLine, Level, ManualBeamOverride, ProjectState
from .detector import (
    extract_levels_from_elevation_or_section,
    probe_columns_at_grid_intersections,
    run_detection,
)
from .inspector import (
    DEFAULT_RENDER_DPI,
    InvalidPdfError,
    _extract_text_blocks_px,
    auto_assign_region_floor_levels,
    build_floor_plan_regions,
    compute_scale_metrics,
    inspect_and_render_pdf,
    px_to_plan_m,
    validate_pdf_bytes,
)
from .schemas import (
    ApplyMasterGridRequest,
    ApplyMasterGridResponse,
    AuditEntry,
    BeamCandidate,
    BulkReviewRequest,
    CandidateReviewUpdate,
    ColumnCandidate,
    CopyOpeningsRequest,
    CopyOpeningsResponse,
    DetectedLevel,
    DetectedOpening,
    DiagnosticIssue,
    ExtractRequest,
    ExtractionResponse,
    GridCandidate,
    ImportDocumentState,
    ImportDocumentSummary,
    LevelCreateRequest,
    LevelExtractionResponse,
    LevelUpdateRequest,
    LockMasterGridRequest,
    ManualBeamRequest,
    ManualColumnRequest,
    ManualGridRequest,
    ManualGridTraceRequest,
    MasterGridDefinition,
    MasterGridLine,
    OpeningCreateRequest,
    PageUpdateRequest,
    PlanRegion,
    PointPx,
    PreflightDiagnosticsResponse,
    QuickUnitScaleRequest,
    RegionCreateRequest,
    RegionUpdateRequest,
    ReviewStatus,
    ScaleCalibration,
    ScaleCalibrationRequest,
    SnapAndCleanRequest,
    StampMasterGridRequest,
    TransferLevelsRequest,
    TransferLevelsResponse,
    TransferRecord,
    TransferRequest,
    TransferResponse,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_imports_root() -> Path:
    root = storage.get_active_data_dir() / "imports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_id(value: str, label: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
    if not safe or safe != value:
        raise ValueError(f"{label} may contain only letters, numbers, hyphens and underscores.")
    return safe


def _project_imports_dir(project_id: str) -> Path:
    safe_pid = _safe_id(project_id, "Project ID")
    return get_imports_root() / safe_pid


def _doc_dir(project_id: str, doc_id: str) -> Path:
    safe_did = _safe_id(doc_id, "Document ID")
    return _project_imports_dir(project_id) / safe_did


def _state_path(project_id: str, doc_id: str) -> Path:
    return _doc_dir(project_id, doc_id) / "state.json"


def _audit_path(project_id: str, doc_id: str) -> Path:
    return _doc_dir(project_id, doc_id) / "audit.jsonl"


def _original_pdf_path(project_id: str, doc_id: str) -> Path:
    return _doc_dir(project_id, doc_id) / "original.pdf"


def _original_dxf_path(project_id: str, doc_id: str) -> Path:
    return _doc_dir(project_id, doc_id) / "original.dxf"


def _pages_dir(project_id: str, doc_id: str) -> Path:
    return _doc_dir(project_id, doc_id) / "pages"


def _append_audit(project_id: str, doc_id: str, event: str, details: dict | None = None) -> None:
    entry = AuditEntry(
        timestamp=_utc_now(),
        event=event,
        project_id=project_id,
        doc_id=doc_id,
        details=details or {},
    )
    path = _audit_path(project_id, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(entry.model_dump_json() + "\n")


def _save_state(state: ImportDocumentState) -> None:
    state.updated_at = _utc_now()
    path = _state_path(state.project_id, state.doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")


def _load_state(project_id: str, doc_id: str) -> ImportDocumentState:
    path = _state_path(project_id, doc_id)
    if not path.exists():
        raise FileNotFoundError(f"PDF import '{doc_id}' not found for project '{project_id}'.")
    return ImportDocumentState.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _ensure_project_exists(project_id: str) -> ProjectState:
    return storage.load_project(project_id)


def _get_page(state: ImportDocumentState, page_number: int):
    for p in state.pages:
        if p.page_number == page_number:
            return p
    raise ValueError(f"Page {page_number} does not exist (document has {state.page_count} page(s)).")


def _get_region(state: ImportDocumentState, region_id: str) -> PlanRegion:
    for r in state.regions:
        if r.region_id == region_id:
            return r
    raise KeyError(f"Region '{region_id}' not found.")


def _find_region(state: ImportDocumentState, region_id: str | None) -> PlanRegion | None:
    if not region_id:
        return None
    for r in state.regions:
        if r.region_id == region_id:
            return r
    return None


def _resolve_region_origin(state: ImportDocumentState, region: PlanRegion) -> PointPx | None:
    if region.origin_px is not None:
        return region.origin_px
    cal = state.calibrations.get(str(region.page_number))
    if cal is not None:
        op = cal.origin_px
        if (
            region.x_px <= op.x_px <= region.x_px + region.width_px
            and region.y_px <= op.y_px <= region.y_px + region.height_px
        ):
            return op
    return None


def _resolve_scale_and_origin(
    state: ImportDocumentState,
    page_number: int,
    region_id: str | None = None,
    require_confirmed: bool = False,
) -> tuple[float, PointPx]:
    """
    Resolve (pixels_per_m, origin_px) for a page/region.
    If region has origin_px set, uses region.origin_px; otherwise uses page calibration origin_px.
    If page is uncalibrated but master_grid exists (from a confirmed page when require_confirmed=True),
    falls back to master_grid.source_pixels_per_meter.
    """
    cal = state.calibrations.get(str(page_number))
    region = _find_region(state, region_id)

    ppm: float | None = None
    origin: PointPx | None = region.origin_px if region is not None else None

    if cal is not None:
        if require_confirmed and not cal.confirmed:
            raise ValueError(
                f"Page {page_number} scale calibration must be confirmed by an engineer before transferring elements."
            )
        ppm = cal.pixels_per_m
        if origin is None:
            origin = cal.origin_px
    elif state.master_grid is not None:
        src_cal = state.calibrations.get(str(state.master_grid.source_page_number))
        if require_confirmed and (src_cal is None or not src_cal.confirmed):
            raise ValueError(
                f"Page {page_number} (or Master Grid source page {state.master_grid.source_page_number}) "
                "scale calibration must be confirmed before transferring elements."
            )
        ppm = state.master_grid.source_pixels_per_meter
        if origin is None and src_cal is not None:
            origin = src_cal.origin_px
    else:
        raise ValueError(
            f"Page {page_number} scale calibration must be confirmed by an engineer before transferring elements."
        )

    if origin is None:
        raise ValueError(f"Page {page_number} requires a calibration or region origin_px.")
    return ppm, origin


def _px_to_m_with_origin(x_px: float, y_px: float, ppm: float, origin: PointPx) -> tuple[float, float]:
    return (
        round((x_px - origin.x_px) / ppm, 4),
        round((origin.y_px - y_px) / ppm, 4),
    )


def _polygon_area_sqm(polygon_m: list[list[float]]) -> float:
    """Compute polygon area in square meters via the shoelace formula."""
    n = len(polygon_m)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = polygon_m[i]
        x2, y2 = polygon_m[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return round(abs(total) * 0.5, 4)


def _recalculate_page_coordinates(state: ImportDocumentState, page_number: int) -> None:
    cal = state.calibrations.get(str(page_number))
    if cal is None:
        return
    for grid in state.grid_candidates:
        if grid.page_number != page_number:
            continue
        reg = _find_region(state, grid.region_id)
        origin = reg.origin_px if (reg and reg.origin_px is not None) else cal.origin_px
        if grid.axis == "x":
            grid.coord_m, _ = _px_to_m_with_origin(grid.coord_px, grid.y1_px, cal.pixels_per_m, origin)
        else:
            _, grid.coord_m = _px_to_m_with_origin(grid.x1_px, grid.coord_px, cal.pixels_per_m, origin)
    for col in state.column_candidates:
        if col.page_number != page_number:
            continue
        reg = _find_region(state, col.region_id)
        origin = reg.origin_px if (reg and reg.origin_px is not None) else cal.origin_px
        col.x_m, col.y_m = _px_to_m_with_origin(col.x_px, col.y_px, cal.pixels_per_m, origin)
    for beam in state.beam_candidates:
        if beam.page_number != page_number:
            continue
        reg = _find_region(state, beam.region_id)
        origin = reg.origin_px if (reg and reg.origin_px is not None) else cal.origin_px
        beam.x1_m, beam.y1_m = _px_to_m_with_origin(beam.x1_px, beam.y1_px, cal.pixels_per_m, origin)
        beam.x2_m, beam.y2_m = _px_to_m_with_origin(beam.x2_px, beam.y2_px, cal.pixels_per_m, origin)
        dx_m = beam.x2_m - beam.x1_m
        dy_m = beam.y2_m - beam.y1_m
        beam.length_m = round(math.hypot(dx_m, dy_m), 4)
        beam.angle_deg = round(math.degrees(math.atan2(abs(dy_m), abs(dx_m))), 2)


def _recompute_page_story_heights(state: ImportDocumentState, page_number: int) -> None:
    page_lvls = [lvl for lvl in state.detected_levels if lvl.page_number == page_number]
    page_lvls.sort(key=lambda l: (l.elevation_m, -l.y_px))
    for idx, lvl in enumerate(page_lvls):
        if idx == 0:
            lvl.story_height_m = None
        else:
            lvl.story_height_m = round(lvl.elevation_m - page_lvls[idx - 1].elevation_m, 4)


def _derive_level_id(name: str, used_ids: set[str]) -> str:
    upper = name.upper()
    mapping = [
        ("BASEMENT", "B1"),
        ("GROUND FLOOR", "GF"),
        ("FIRST FLOOR", "1F"),
        ("1ST FLOOR", "1F"),
        ("SECOND FLOOR", "2F"),
        ("2ND FLOOR", "2F"),
        ("THIRD FLOOR", "3F"),
        ("3RD FLOOR", "3F"),
        ("FOURTH FLOOR", "4F"),
        ("4TH FLOOR", "4F"),
        ("FIFTH FLOOR", "5F"),
        ("5TH FLOOR", "5F"),
        ("SIXTH FLOOR", "6F"),
        ("6TH FLOOR", "6F"),
        ("MEZZANINE", "MEZZ"),
        ("PENTHOUSE", "PH"),
        ("ROOF DECK", "RD"),
        ("ROOF LINE", "ROOF"),
        ("ROOF BEAM", "RB"),
    ]
    base = ""
    for kw, code in mapping:
        if kw in upper:
            base = code
            break
    if not base:
        m = re.search(r"(\d+)(?:ST|ND|RD|TH)\s+FLOOR|LEVEL\s*(\d+)", upper)
        if m:
            num = m.group(1) or m.group(2)
            base = f"{num}F"
        else:
            slug = re.sub(r"[^A-Z0-9]+", "_", upper).strip("_")
            base = (slug[:12] or "LVL").rstrip("_")

    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def upload_pdf(
    project_id: str,
    filename: str,
    content: bytes,
    dpi: int = DEFAULT_RENDER_DPI,
) -> ImportDocumentState:
    _ensure_project_exists(project_id)
    validate_pdf_bytes(content)

    doc_id = f"pdf-{uuid4().hex[:10]}"
    doc_root = _doc_dir(project_id, doc_id)
    pages_dir = _pages_dir(project_id, doc_id)
    pdf_path = _original_pdf_path(project_id, doc_id)

    doc_root.mkdir(parents=True, exist_ok=False)
    pdf_path.write_bytes(content)

    try:
        project = _ensure_project_exists(project_id)
        pages, regions = inspect_and_render_pdf(
            pdf_path, pages_dir, dpi=dpi, project_levels=project.levels
        )
    except InvalidPdfError:
        for child in sorted(doc_root.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        if doc_root.exists():
            doc_root.rmdir()
        raise

    now = _utc_now()
    safe_name = Path(filename or "plan.pdf").name
    state = ImportDocumentState(
        doc_id=doc_id,
        project_id=project_id,
        filename=safe_name,
        sha256=hashlib.sha256(content).hexdigest(),
        file_size_bytes=len(content),
        created_at=now,
        updated_at=now,
        page_count=len(pages),
        pages=pages,
        regions=regions,
    )
    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "pdf_uploaded",
        {
            "filename": safe_name,
            "sha256": state.sha256,
            "file_size_bytes": state.file_size_bytes,
            "page_count": state.page_count,
            "classifications": {str(p.page_number): p.classification for p in pages},
            "sheet_types": {str(p.page_number): p.sheet_type for p in pages},
            "auto_regions_count": len(regions),
        },
    )
    return state


def append_pdf(
    project_id: str,
    doc_id: str | None,
    filename: str,
    content: bytes,
    dpi: int = DEFAULT_RENDER_DPI,
) -> ImportDocumentState:
    """
    Append pages from another PDF onto an existing imported PDF document using
    PyMuPDF doc.insert_pdf() -> temporary .tmp file -> os.replace() swap,
    preserving all existing page classifications, calibrations, regions, levels, and tracings.
    """
    _ensure_project_exists(project_id)
    validate_pdf_bytes(content)

    target_doc_id = doc_id
    if not target_doc_id:
        existing_list = list_imports(project_id)
        if not existing_list:
            return upload_pdf(project_id, filename, content, dpi=dpi)
        target_doc_id = existing_list[0].doc_id

    state = get_import(project_id, target_doc_id)
    pdf_path = _original_pdf_path(project_id, state.doc_id)
    pages_dir = _pages_dir(project_id, state.doc_id)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Original PDF missing for import '{state.doc_id}'.")

    tmp_path = _doc_dir(project_id, state.doc_id) / "original.pdf.tmp"
    try:
        existing_doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise InvalidPdfError(f"Unable to open existing PDF '{state.doc_id}': {exc}") from exc

    try:
        try:
            new_doc = pymupdf.open(stream=content, filetype="pdf")
        except Exception as exc:
            raise InvalidPdfError(f"Unable to open appended PDF document: {exc}") from exc
        try:
            if len(new_doc) == 0:
                raise InvalidPdfError("Appended PDF document contains no pages.")
            old_page_count = len(existing_doc)
            added_count = len(new_doc)
            existing_doc.insert_pdf(new_doc)
            existing_doc.save(str(tmp_path))
        finally:
            new_doc.close()
    finally:
        existing_doc.close()

    os.replace(str(tmp_path), str(pdf_path))

    project = _ensure_project_exists(project_id)
    new_pages, new_regions = inspect_and_render_pdf(
        pdf_path,
        pages_dir,
        dpi=dpi,
        start_page_index=old_page_count,
        project_levels=project.levels,
    )

    state.pages.extend(new_pages)
    state.regions.extend(new_regions)
    state.page_count = len(state.pages)
    state.file_size_bytes = pdf_path.stat().st_size
    state.updated_at = _utc_now()
    _save_state(state)
    _append_audit(
        project_id,
        state.doc_id,
        "pdf_appended",
        {
            "appended_filename": Path(filename or "appended.pdf").name,
            "added_pages": added_count,
            "previous_page_count": old_page_count,
            "new_page_count": state.page_count,
            "new_regions_count": len(new_regions),
        },
    )
    return state


def list_imports(project_id: str) -> list[ImportDocumentSummary]:
    _ensure_project_exists(project_id)
    base = _project_imports_dir(project_id)
    if not base.exists():
        return []

    summaries: list[ImportDocumentSummary] = []
    for state_file in sorted(base.glob("*/state.json")):
        try:
            st = ImportDocumentState.model_validate(json.loads(state_file.read_text(encoding="utf-8")))
            summaries.append(
                ImportDocumentSummary(
                    doc_id=st.doc_id,
                    project_id=st.project_id,
                    filename=st.filename,
                    page_count=st.page_count,
                    created_at=st.created_at,
                    updated_at=st.updated_at,
                    calibrated_pages=len(st.calibrations),
                    confirmed_pages=sum(1 for c in st.calibrations.values() if c.confirmed),
                    approved_grids=sum(1 for g in st.grid_candidates if g.status == ReviewStatus.APPROVED),
                    approved_columns=sum(1 for c in st.column_candidates if c.status == ReviewStatus.APPROVED),
                    approved_levels=sum(1 for l in st.detected_levels if l.engineer_approved),
                    approved_openings=sum(1 for o in st.openings if o.engineer_approved),
                    transfers_count=len(st.transfers),
                )
            )
        except Exception:
            continue
    summaries.sort(key=lambda s: s.created_at, reverse=True)
    return summaries


def get_import(project_id: str, doc_id: str) -> ImportDocumentState:
    _ensure_project_exists(project_id)
    return _load_state(project_id, doc_id)


def get_page_image_path(project_id: str, doc_id: str, page_number: int) -> Path:
    state = get_import(project_id, doc_id)
    page = _get_page(state, page_number)
    img_path = _pages_dir(project_id, doc_id) / page.image_filename
    if not img_path.exists():
        raise FileNotFoundError(f"Rendered image missing for page {page_number}.")
    return img_path


def calibrate_page(
    project_id: str,
    doc_id: str,
    page_number: int,
    req: ScaleCalibrationRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    _get_page(state, page_number)

    pixel_distance, real_distance_m, meters_per_px, pixels_per_m = compute_scale_metrics(
        req.p1.x_px,
        req.p1.y_px,
        req.p2.x_px,
        req.p2.y_px,
        req.known_distance,
        req.unit,
    )
    origin_px = req.origin_px if req.origin_px is not None else PointPx(x_px=req.p1.x_px, y_px=req.p1.y_px)
    now = _utc_now()

    calibration = ScaleCalibration(
        page_number=page_number,
        p1=req.p1,
        p2=req.p2,
        origin_px=origin_px,
        known_distance=req.known_distance,
        unit=req.unit,
        pixel_distance=round(pixel_distance, 4),
        real_distance_m=round(real_distance_m, 6),
        meters_per_px=round(meters_per_px, 8),
        pixels_per_m=round(pixels_per_m, 4),
        confirmed=req.confirmed,
        calibrated_at=now,
        confirmed_at=now if req.confirmed else None,
        notes=req.notes,
    )
    state.calibrations[str(page_number)] = calibration
    _recalculate_page_coordinates(state, page_number)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "scale_calibrated",
        {
            "page_number": page_number,
            "known_distance": req.known_distance,
            "unit": req.unit,
            "pixel_distance": calibration.pixel_distance,
            "real_distance_m": calibration.real_distance_m,
            "meters_per_px": calibration.meters_per_px,
            "confirmed": calibration.confirmed,
        },
    )
    if req.confirmed:
        _append_audit(
            project_id,
            doc_id,
            "scale_confirmed",
            {"page_number": page_number, "meters_per_px": calibration.meters_per_px},
        )
    return state


def confirm_calibration(project_id: str, doc_id: str, page_number: int) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    _get_page(state, page_number)

    cal = state.calibrations.get(str(page_number))
    if cal is None:
        raise ValueError(f"Page {page_number} has no scale calibration to confirm.")

    cal.confirmed = True
    cal.confirmed_at = _utc_now()
    _recalculate_page_coordinates(state, page_number)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "scale_confirmed",
        {"page_number": page_number, "meters_per_px": cal.meters_per_px},
    )
    return state


def set_page_unit(
    project_id: str,
    doc_id: str,
    page_number: int,
    req: QuickUnitScaleRequest,
) -> ImportDocumentState:
    """
    1-click unit scale override: reinterprets the existing ScaleCalibration's known_distance
    in req.unit, confirms calibration, updates detected_cad_unit for CAD docs, and recalculates
    all grid and column coordinates in meters on page_number.
    """
    state = get_import(project_id, doc_id)
    _get_page(state, page_number)

    cal = state.calibrations.get(str(page_number))
    if cal is None:
        raise ValueError(
            f"Page {page_number} has not been calibrated yet. Run 2-point Scale Calibration first."
        )

    prev_unit = cal.unit
    pixel_distance, real_distance_m, meters_per_px, pixels_per_m = compute_scale_metrics(
        cal.p1.x_px,
        cal.p1.y_px,
        cal.p2.x_px,
        cal.p2.y_px,
        cal.known_distance,
        req.unit,
    )
    now = _utc_now()
    cal.unit = req.unit
    cal.pixel_distance = round(pixel_distance, 4)
    cal.real_distance_m = round(real_distance_m, 6)
    cal.meters_per_px = round(meters_per_px, 8)
    cal.pixels_per_m = round(pixels_per_m, 4)
    cal.confirmed = True
    cal.confirmed_at = now
    if state.source_format == "CAD":
        state.detected_cad_unit = req.unit

    _recalculate_page_coordinates(state, page_number)
    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "unit_scale_overridden",
        {
            "page_number": page_number,
            "previous_unit": prev_unit,
            "new_unit": req.unit,
            "known_distance": cal.known_distance,
            "meters_per_px": cal.meters_per_px,
            "pixels_per_m": cal.pixels_per_m,
        },
    )
    return state


def update_page(
    project_id: str,
    doc_id: str,
    page_number: int,
    req: PageUpdateRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    page = _get_page(state, page_number)

    if req.sheet_type is not None:
        page.sheet_type = req.sheet_type
    if req.sheet_number is not None:
        page.sheet_number = req.sheet_number.strip()
    if req.sheet_title is not None:
        page.sheet_title = req.sheet_title.strip()

    project = _ensure_project_exists(project_id)
    if page.sheet_type in ("floor_plan", "structural_plan", "roof_plan") and not any(
        r.page_number == page_number for r in state.regions
    ):
        pdf_path = _original_pdf_path(project_id, doc_id)
        png_path = _pages_dir(project_id, doc_id) / page.image_filename
        if pdf_path.exists():
            doc = pymupdf.open(pdf_path)
            try:
                pg = doc[page_number - 1]
                blocks = _extract_text_blocks_px(pg, page.width_px, page.height_px)
                vp_count, new_regions = build_floor_plan_regions(
                    pg,
                    png_path,
                    page_number,
                    page.width_px,
                    page.height_px,
                    blocks,
                    page.sheet_title,
                )
                page.viewport_count = vp_count
                state.regions.extend(new_regions)
            finally:
                doc.close()

    if project.levels:
        auto_assign_region_floor_levels(
            state.pages,
            [r for r in state.regions if r.page_number == page_number],
            project.levels,
        )

    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "page_updated",
        {
            "page_number": page_number,
            "sheet_type": page.sheet_type,
            "sheet_number": page.sheet_number,
            "sheet_title": page.sheet_title,
        },
    )
    return state


def create_region(
    project_id: str,
    doc_id: str,
    req: RegionCreateRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    _get_page(state, req.page_number)

    region = PlanRegion(
        region_id=f"reg-p{req.page_number}-{uuid4().hex[:6]}",
        page_number=req.page_number,
        name=req.name.strip(),
        region_type=req.region_type,
        floor_level="" if req.region_type == "elevation" else req.floor_level.strip(),
        x_px=round(req.x_px, 2),
        y_px=round(req.y_px, 2),
        width_px=round(req.width_px, 2),
        height_px=round(req.height_px, 2),
        origin_px=req.origin_px,
        auto_detected=False,
    )
    state.regions.append(region)
    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "region_created",
        {
            "region_id": region.region_id,
            "page_number": req.page_number,
            "name": region.name,
            "region_type": region.region_type,
            "floor_level": region.floor_level,
        },
    )
    return state


def update_region(
    project_id: str,
    doc_id: str,
    region_id: str,
    req: RegionUpdateRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    region = _get_region(state, region_id)

    if req.name is not None and req.name.strip():
        region.name = req.name.strip()
    if req.region_type is not None:
        region.region_type = req.region_type
    if req.floor_level is not None:
        region.floor_level = req.floor_level.strip()
    if region.region_type == "elevation":
        region.floor_level = ""
    if req.origin_px is not None:
        region.origin_px = req.origin_px
    if req.x_px is not None:
        region.x_px = req.x_px
    if req.y_px is not None:
        region.y_px = req.y_px
    if req.width_px is not None:
        region.width_px = req.width_px
    if req.height_px is not None:
        region.height_px = req.height_px

    if req.origin_px is not None:
        _recalculate_page_coordinates(state, region.page_number)

    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "region_updated",
        {
            "region_id": region_id,
            "name": region.name,
            "floor_level": region.floor_level,
            "origin_px": region.origin_px.model_dump() if region.origin_px else None,
        },
    )
    return state


def delete_region(
    project_id: str,
    doc_id: str,
    region_id: str,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    _get_region(state, region_id)

    state.regions = [r for r in state.regions if r.region_id != region_id]
    for g in state.grid_candidates:
        if g.region_id == region_id:
            g.region_id = None
    for c in state.column_candidates:
        if c.region_id == region_id:
            c.region_id = None
    for o in state.openings:
        if o.region_id == region_id:
            o.region_id = None
    for b in state.beam_candidates:
        if b.region_id == region_id:
            b.region_id = None

    _save_state(state)
    _append_audit(project_id, doc_id, "region_deleted", {"region_id": region_id})
    return state


def extract_page(
    project_id: str,
    doc_id: str,
    page_number: int,
    req: ExtractRequest,
) -> ExtractionResponse:
    state = get_import(project_id, doc_id)
    page = _get_page(state, page_number)
    cal = state.calibrations.get(str(page_number))

    region: PlanRegion | None = None
    if req.region_id is not None:
        region = _get_region(state, req.region_id)
        if region.page_number != page_number:
            raise ValueError(f"Region '{req.region_id}' belongs to page {region.page_number}, not page {page_number}.")
        if region.region_type != "plan":
            raise ValueError(
                f"Region '{region.name}' is an elevation region; floor plan extraction only targets 'plan' regions."
            )

    exclude_regions = [
        r for r in state.regions if r.page_number == page_number and r.region_type == "elevation"
    ]

    pdf_path = _original_pdf_path(project_id, doc_id)
    png_path = _pages_dir(project_id, doc_id) / page.image_filename
    options = req.resolved_options()

    effective_ppm = (
        cal.pixels_per_m
        if cal is not None
        else (state.master_grid.source_pixels_per_meter if state.master_grid is not None else None)
    )
    effective_origin = (
        region.origin_px
        if (region is not None and region.origin_px is not None)
        else (cal.origin_px if cal is not None else None)
    )

    existing_region_grids = [
        g
        for g in state.grid_candidates
        if g.page_number == page_number
        and g.status != ReviewStatus.REJECTED
        and (req.region_id is None or g.region_id == req.region_id or g.region_id is None)
    ]
    existing_region_columns = [
        c
        for c in state.column_candidates
        if c.page_number == page_number
        and c.status != ReviewStatus.REJECTED
        and (req.region_id is None or c.region_id == req.region_id or c.region_id is None)
    ]
    page_openings = [
        o
        for o in state.openings
        if o.page_number == page_number
        and (req.region_id is None or o.region_id == req.region_id or o.region_id is None)
    ]

    output = run_detection(
        pdf_path=pdf_path,
        png_path=png_path,
        page_info=page,
        calibration=cal,
        method=req.method,
        options=options,
        region=region,
        existing_grids=existing_region_grids,
        existing_columns=existing_region_columns,
        openings=page_openings,
        pixels_per_meter=effective_ppm,
        origin_px=effective_origin,
        exclude_regions=exclude_regions,
    )

    if req.replace_unreviewed:
        if options.detect_grids:
            state.grid_candidates = [
                g
                for g in state.grid_candidates
                if not (
                    g.page_number == page_number
                    and g.source != "manual"
                    and g.status == ReviewStatus.PENDING
                    and (req.region_id is None or g.region_id == req.region_id)
                )
            ]
        if options.detect_columns:
            state.column_candidates = [
                c
                for c in state.column_candidates
                if not (
                    c.page_number == page_number
                    and c.source != "manual"
                    and not c.engineer_approved
                    and c.status != ReviewStatus.APPROVED
                    and (req.region_id is None or c.region_id == req.region_id)
                )
            ]
        if options.detect_diagonal_beams:
            state.beam_candidates = [
                b
                for b in state.beam_candidates
                if not (
                    b.page_number == page_number
                    and b.source != "manual"
                    and not b.engineer_approved
                    and b.status != ReviewStatus.APPROVED
                    and (req.region_id is None or b.region_id == req.region_id)
                )
            ]

    existing_col_ids = {c.column_id for c in state.column_candidates}
    next_num = 1
    for col in output.columns:
        if col.column_id in existing_col_ids:
            while f"C{next_num}" in existing_col_ids:
                next_num += 1
            col.column_id = f"C{next_num}"
        existing_col_ids.add(col.column_id)

    existing_beam_ids = {b.beam_id for b in state.beam_candidates if b.beam_id}
    next_bnum = 1
    for bm in output.beams:
        if not bm.beam_id or bm.beam_id in existing_beam_ids:
            while f"DB{next_bnum}" in existing_beam_ids:
                next_bnum += 1
            bm.beam_id = f"DB{next_bnum}"
        existing_beam_ids.add(bm.beam_id)

    if options.detect_grids:
        state.grid_candidates.extend(output.grids)
    if options.detect_columns:
        state.column_candidates.extend(output.columns)
    if options.detect_diagonal_beams:
        state.beam_candidates.extend(output.beams)
    _recalculate_page_coordinates(state, page_number)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "extraction_run",
        {
            "page_number": page_number,
            "region_id": req.region_id,
            "method_requested": req.method,
            "method_used": output.method_used,
            "options": options.model_dump(),
            "grids_detected": len(output.grids) if options.detect_grids else 0,
            "columns_detected": len(output.columns) if options.detect_columns else 0,
            "beams_detected": len(output.beams) if options.detect_diagonal_beams else 0,
        },
    )

    return ExtractionResponse(
        document=state,
        page_number=page_number,
        method_requested=req.method,
        method_used=output.method_used,
        grids_detected=len(output.grids) if options.detect_grids else 0,
        columns_detected=len(output.columns) if options.detect_columns else 0,
        notice=output.notice,
    )


def extract_levels(
    project_id: str,
    doc_id: str,
    page_number: int,
    region_id: str | None = None,
) -> LevelExtractionResponse:
    state = get_import(project_id, doc_id)
    page = _get_page(state, page_number)
    cal = state.calibrations.get(str(page_number))

    region: PlanRegion | None = None
    if region_id is not None:
        region = _get_region(state, region_id)
        if region.page_number != page_number:
            raise ValueError(
                f"Region '{region_id}' belongs to page {region.page_number}, not page {page_number}."
            )
        if region.region_type != "elevation":
            raise ValueError(
                f"Region '{region.name}' has region_type='{region.region_type}'; "
                "set it to 'elevation' before extracting region levels."
            )

    pdf_path = _original_pdf_path(project_id, doc_id)
    png_path = _pages_dir(project_id, doc_id) / page.image_filename

    levels, warnings = extract_levels_from_elevation_or_section(
        pdf_path,
        png_path,
        page,
        region=region,
        calibration=cal,
    )

    if region_id is not None:
        # Selective replacement: replace ONLY un-transferred levels from this specific elevation region
        # (preserves manual levels with region_id=None, transferred levels, and levels from other regions).
        state.detected_levels = [
            lvl
            for lvl in state.detected_levels
            if not (
                lvl.page_number == page_number
                and lvl.region_id == region_id
                and not lvl.transferred
            )
        ]
    else:
        state.detected_levels = [
            lvl
            for lvl in state.detected_levels
            if not (
                lvl.page_number == page_number
                and lvl.region_id is None
                and not lvl.engineer_approved
                and not lvl.transferred
            )
        ]
    state.detected_levels.extend(levels)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "levels_extracted",
        {
            "page_number": page_number,
            "region_id": region_id,
            "sheet_type": page.sheet_type,
            "levels_detected": len(levels),
            "warnings": warnings,
        },
    )

    scope_desc = f"region '{region.name}' on page {page_number}" if region is not None else f"page {page_number}"
    notice = (
        f"Extracted {len(levels)} structural floor-to-floor level(s) from {scope_desc}."
        + (f" Warnings: {' '.join(warnings)}" if warnings else " Review and approve levels before transferring.")
    )
    return LevelExtractionResponse(
        document=state,
        page_number=page_number,
        levels_detected=len(levels),
        warnings=warnings,
        notice=notice,
    )


def create_manual_level(
    project_id: str,
    doc_id: str,
    req: LevelCreateRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    _get_page(state, req.page_number)
    lvl = DetectedLevel(
        level_id=f"lvl-{uuid4().hex[:8]}",
        name=req.name.strip(),
        elevation_m=round(req.elevation_m, 4),
        story_height_m=None,
        page_number=req.page_number,
        y_px=round(req.y_px, 2),
        confidence=1.0,
        review_status=ReviewStatus.APPROVED if req.engineer_approved else ReviewStatus.PENDING,
        engineer_approved=req.engineer_approved,
        warnings=[],
    )
    state.detected_levels.append(lvl)
    _recompute_page_story_heights(state, req.page_number)
    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "level_created_manual",
        {"page_number": req.page_number, "name": lvl.name, "elevation_m": lvl.elevation_m},
    )
    return state


def update_level(
    project_id: str,
    doc_id: str,
    level_id: str,
    req: LevelUpdateRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    for lvl in state.detected_levels:
        if lvl.level_id == level_id:
            if req.name is not None and req.name.strip():
                lvl.name = req.name.strip()
            if req.elevation_m is not None:
                lvl.elevation_m = round(req.elevation_m, 4)
            elif req.story_height_m is not None:
                # Adjust elevation_m relative to the level immediately below on this page
                page_lvls = sorted(
                    [x for x in state.detected_levels if x.page_number == lvl.page_number],
                    key=lambda x: (x.elevation_m, -x.y_px),
                )
                idx = next((i for i, x in enumerate(page_lvls) if x.level_id == level_id), 0)
                if idx > 0:
                    lvl.elevation_m = round(page_lvls[idx - 1].elevation_m + req.story_height_m, 4)
                lvl.story_height_m = round(req.story_height_m, 4)

            if req.review_status is not None and req.engineer_approved is not None:
                lvl.review_status = req.review_status
                lvl.engineer_approved = req.engineer_approved
            elif req.engineer_approved is not None:
                lvl.engineer_approved = req.engineer_approved
                lvl.review_status = ReviewStatus.APPROVED if req.engineer_approved else ReviewStatus.PENDING
            elif req.review_status is not None:
                lvl.review_status = req.review_status
                lvl.engineer_approved = req.review_status == ReviewStatus.APPROVED

            if req.elevation_m is not None or req.story_height_m is not None:
                _recompute_page_story_heights(state, lvl.page_number)

            _save_state(state)
            _append_audit(
                project_id,
                doc_id,
                "level_reviewed",
                {
                    "level_id": level_id,
                    "name": lvl.name,
                    "elevation_m": lvl.elevation_m,
                    "story_height_m": lvl.story_height_m,
                    "review_status": lvl.review_status.value,
                    "engineer_approved": lvl.engineer_approved,
                },
            )
            return state

    raise KeyError(f"Level '{level_id}' not found.")


def delete_level(
    project_id: str,
    doc_id: str,
    level_id: str,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    target = next((l for l in state.detected_levels if l.level_id == level_id), None)
    if target is None:
        raise KeyError(f"Level '{level_id}' not found.")
    pnum = target.page_number
    state.detected_levels = [l for l in state.detected_levels if l.level_id != level_id]
    _recompute_page_story_heights(state, pnum)
    _save_state(state)
    _append_audit(project_id, doc_id, "level_deleted", {"level_id": level_id, "page_number": pnum})
    return state


def transfer_levels(
    project_id: str,
    doc_id: str,
    req: TransferLevelsRequest,
) -> TransferLevelsResponse:
    project = _ensure_project_exists(project_id)
    state = _load_state(project_id, doc_id)

    approved_levels = [
        lvl
        for lvl in state.detected_levels
        if (lvl.engineer_approved or lvl.review_status == ReviewStatus.APPROVED)
        and (req.page_number is None or lvl.page_number == req.page_number)
    ]
    if not approved_levels:
        raise ValueError(
            "No engineer-approved floor levels are ready to transfer. Approve at least one level first."
        )

    approved_levels.sort(key=lambda l: l.elevation_m)

    if req.replace_existing:
        used_ids: set[str] = set()
        new_levels: list[Level] = []
    else:
        used_ids = {lv.id for lv in project.levels}
        new_levels = list(project.levels)

    for det in approved_levels:
        lid = _derive_level_id(det.name, used_ids)
        new_levels.append(Level(id=lid, elevation_m=round(det.elevation_m, 4)))

    new_levels.sort(key=lambda lv: lv.elevation_m)
    valid_level_ids = {lv.id for lv in new_levels}
    new_beams = [b for b in project.beams if b.level in valid_level_ids]

    updated_payload = project.model_dump()
    updated_payload["levels"] = [lv.model_dump() for lv in new_levels]
    updated_payload["beams"] = [b.model_dump() for b in new_beams]
    updated_project = ProjectState.model_validate(updated_payload)

    storage.save_project(updated_project)

    for det in approved_levels:
        det.transferred = True

    auto_mapped_regions = auto_assign_region_floor_levels(
        state.pages, state.regions, updated_project.levels
    )

    involved_pages = sorted({det.page_number for det in approved_levels})
    state.transfers.append(
        TransferRecord(
            transferred_at=_utc_now(),
            page_numbers=involved_pages,
            merge_mode="replace_levels" if req.replace_existing else "merge_levels",
            levels_count=len(approved_levels),
        )
    )
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "levels_transferred",
        {
            "page_numbers": involved_pages,
            "replace_existing": req.replace_existing,
            "levels_transferred": len(approved_levels),
            "level_ids": [lv.id for lv in new_levels],
            "auto_mapped_regions": auto_mapped_regions,
        },
    )

    mapped_suffix = (
        f" Auto-mapped {auto_mapped_regions} plan region(s) to project levels."
        if auto_mapped_regions > 0
        else ""
    )
    return TransferLevelsResponse(
        project_id=project_id,
        doc_id=doc_id,
        transferred_levels=len(approved_levels),
        total_project_levels=len(updated_project.levels),
        notice=(
            f"Transferred {len(approved_levels)} engineer-approved level(s) into project '{project_id}' "
            f"(sorted by elevation_m).{mapped_suffix}"
        ),
        project=updated_project,
        document=state,
    )


def add_manual_traced_grid(
    project_id: str,
    doc_id: str,
    req: ManualGridTraceRequest,
) -> ImportDocumentState:
    """
    Trace a manual grid line from (p1, p2), snapped cleanly to vertical (grid_x) or horizontal (grid_y),
    marked engineer_approved=True, persisted to state.json, and logged to audit.jsonl.
    """
    state = get_import(project_id, doc_id)
    page = _get_page(state, req.page_number)
    region = _get_region(state, req.region_id) if req.region_id else None

    if region is not None:
        rx0, ry0 = region.x_px, region.y_px
        rx1, ry1 = region.x_px + region.width_px, region.y_px + region.height_px
    else:
        rx0, ry0, rx1, ry1 = 0.0, 0.0, float(page.width_px), float(page.height_px)

    if req.element_type == "grid_x":
        axis = "x"
        coord_px = (req.p1.x_px + req.p2.x_px) * 0.5
        span_lo = min(req.p1.y_px, req.p2.y_px)
        span_hi = max(req.p1.y_px, req.p2.y_px)
        if span_hi - span_lo < 10.0:
            span_lo, span_hi = ry0, ry1
        else:
            span_lo = max(span_lo, ry0)
            span_hi = min(span_hi, ry1)
        x1, y1, x2, y2 = coord_px, span_lo, coord_px, span_hi
    else:
        axis = "y"
        coord_px = (req.p1.y_px + req.p2.y_px) * 0.5
        span_lo = min(req.p1.x_px, req.p2.x_px)
        span_hi = max(req.p1.x_px, req.p2.x_px)
        if span_hi - span_lo < 10.0:
            span_lo, span_hi = rx0, rx1
        else:
            span_lo = max(span_lo, rx0)
            span_hi = min(span_hi, rx1)
        x1, y1, x2, y2 = span_lo, coord_px, span_hi, coord_px

    coord_m = None
    cal = state.calibrations.get(str(req.page_number))
    ppm = cal.pixels_per_m if cal is not None else (
        state.master_grid.source_pixels_per_meter if state.master_grid is not None else None
    )
    origin = (region.origin_px if (region and region.origin_px is not None) else (cal.origin_px if cal else None))
    if ppm is not None and origin is not None:
        if axis == "x":
            coord_m, _ = _px_to_m_with_origin(coord_px, y1, ppm, origin)
        else:
            _, coord_m = _px_to_m_with_origin(x1, coord_px, ppm, origin)

    candidate = GridCandidate(
        id=f"grid-{uuid4().hex[:8]}",
        page_number=req.page_number,
        region_id=req.region_id,
        axis=axis,
        label=req.label.strip(),
        x1_px=round(x1, 2),
        y1_px=round(y1, 2),
        x2_px=round(x2, 2),
        y2_px=round(y2, 2),
        coord_px=round(coord_px, 2),
        coord_m=coord_m,
        source="manual",
        confidence=1.0,
        status=ReviewStatus.APPROVED,
        engineer_approved=True,
        notes="Manually traced grid",
    )
    state.grid_candidates.append(candidate)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "manual_grid_added",
        {
            "element_id": candidate.id,
            "page_number": req.page_number,
            "region_id": req.region_id,
            "element_type": req.element_type,
            "label": candidate.label,
            "coord_px": candidate.coord_px,
            "coord_m": candidate.coord_m,
        },
    )
    return state


def delete_element(
    project_id: str,
    doc_id: str,
    element_id: str,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    orig_g = len(state.grid_candidates)
    orig_c = len(state.column_candidates)
    orig_b = len(state.beam_candidates)

    state.grid_candidates = [g for g in state.grid_candidates if g.id != element_id]
    state.column_candidates = [c for c in state.column_candidates if c.id != element_id]
    state.beam_candidates = [b for b in state.beam_candidates if b.id != element_id]

    if (
        len(state.grid_candidates) == orig_g
        and len(state.column_candidates) == orig_c
        and len(state.beam_candidates) == orig_b
    ):
        raise KeyError(f"Element '{element_id}' not found.")

    if len(state.grid_candidates) < orig_g:
        elem_type = "grid"
    elif len(state.column_candidates) < orig_c:
        elem_type = "column"
    else:
        elem_type = "beam"
    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "element_deleted",
        {"element_id": element_id, "element_type": elem_type},
    )
    return state


def clear_region_grids(
    project_id: str,
    doc_id: str,
    region_id: str,
) -> ImportDocumentState:
    """Delete all grid_x and grid_y candidates in region_id atomically; leave columns, openings, and other regions untouched."""
    state = get_import(project_id, doc_id)
    region = _get_region(state, region_id)

    before_count = len(state.grid_candidates)
    state.grid_candidates = [g for g in state.grid_candidates if g.region_id != region_id]
    removed_count = before_count - len(state.grid_candidates)

    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "region_grids_cleared",
        {
            "region_id": region_id,
            "page_number": region.page_number,
            "removed_grids_count": removed_count,
        },
    )
    return state


def lock_master_grid(
    project_id: str,
    doc_id: str,
    req: LockMasterGridRequest,
) -> ImportDocumentState:
    """Compute and lock the document-level MasterGridDefinition in meters from a source region."""
    state = get_import(project_id, doc_id)
    source_region = _get_region(state, req.source_region_id)

    cal = state.calibrations.get(str(source_region.page_number))
    if cal is None:
        raise ValueError(
            f"Page {source_region.page_number} must be scale-calibrated before locking a Master Grid."
        )
    origin = _resolve_region_origin(state, source_region)
    if origin is None:
        raise ValueError(
            f"Region '{source_region.name}' requires an origin_px (Set Origin (0,0)) before locking the Master Grid."
        )

    source_grids = [
        g
        for g in state.grid_candidates
        if (g.status == ReviewStatus.APPROVED or g.engineer_approved)
        and (
            g.region_id == source_region.region_id
            or (
                g.region_id is None
                and g.page_number == source_region.page_number
                and (
                    (g.axis == "x" and source_region.x_px <= g.coord_px <= source_region.x_px + source_region.width_px)
                    or (g.axis == "y" and source_region.y_px <= g.coord_px <= source_region.y_px + source_region.height_px)
                )
            )
        )
    ]
    if not source_grids:
        raise ValueError(
            f"Region '{source_region.name}' has no approved grid lines to lock as Master Grid."
        )

    x_by_label: dict[str, MasterGridLine] = {}
    y_by_label: dict[str, MasterGridLine] = {}
    for g in source_grids:
        if g.axis == "x":
            offset_m, _ = _px_to_m_with_origin(g.coord_px, g.y1_px, cal.pixels_per_m, origin)
            x_by_label[g.label] = MasterGridLine(label=g.label, offset_m=offset_m)
        else:
            _, offset_m = _px_to_m_with_origin(g.x1_px, g.coord_px, cal.pixels_per_m, origin)
            y_by_label[g.label] = MasterGridLine(label=g.label, offset_m=offset_m)

    master = MasterGridDefinition(
        source_region_id=source_region.region_id,
        source_page_number=source_region.page_number,
        source_pixels_per_meter=cal.pixels_per_m,
        grid_x_m=sorted(x_by_label.values(), key=lambda line: line.offset_m),
        grid_y_m=sorted(y_by_label.values(), key=lambda line: line.offset_m),
    )
    state.master_grid = master
    source_region.origin_px = origin
    _recalculate_page_coordinates(state, source_region.page_number)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "master_grid_locked",
        {
            "source_region_id": master.source_region_id,
            "source_page_number": master.source_page_number,
            "source_pixels_per_meter": master.source_pixels_per_meter,
            "grid_x_count": len(master.grid_x_m),
            "grid_y_count": len(master.grid_y_m),
        },
    )
    return state


def stamp_master_grid(
    project_id: str,
    doc_id: str,
    target_region_id: str,
    req: StampMasterGridRequest,
) -> ImportDocumentState:
    """
    Stamp the locked Master Grid cleanly onto target_region_id anchored at req.anchor_px.
    Does NOT run auto-line scanning, giving exact grid intersections immediately.
    """
    state = get_import(project_id, doc_id)
    if state.master_grid is None:
        raise ValueError("No Master Grid is locked yet. Lock a Master Grid from a calibrated floor region first.")

    target_region = _get_region(state, target_region_id)
    if target_region.region_type != "plan":
        raise ValueError(f"Target region '{target_region.name}' is an elevation region; Master Grid only targets 'plan' regions.")
    target_region.origin_px = req.anchor_px

    target_cal = state.calibrations.get(str(target_region.page_number))
    ppm = target_cal.pixels_per_m if target_cal is not None else state.master_grid.source_pixels_per_meter

    if req.clear_existing_grids:
        state.grid_candidates = [g for g in state.grid_candidates if g.region_id != target_region_id]

    rx0, ry0 = target_region.x_px, target_region.y_px
    rx1, ry1 = target_region.x_px + target_region.width_px, target_region.y_px + target_region.height_px

    stamped_count = 0
    for gx in state.master_grid.grid_x_m:
        x_px = req.anchor_px.x_px + gx.offset_m * ppm
        if not (rx0 - 2.0 <= x_px <= rx1 + 2.0):
            continue
        x_clamped = min(max(x_px, rx0), rx1)
        state.grid_candidates.append(
            GridCandidate(
                id=f"grid-{uuid4().hex[:8]}",
                page_number=target_region.page_number,
                region_id=target_region_id,
                axis="x",
                label=gx.label,
                x1_px=round(x_clamped, 2),
                y1_px=round(ry0, 2),
                x2_px=round(x_clamped, 2),
                y2_px=round(ry1, 2),
                coord_px=round(x_clamped, 2),
                coord_m=gx.offset_m,
                source="manual",
                confidence=1.0,
                status=ReviewStatus.APPROVED,
                engineer_approved=True,
                notes=f"Stamped from Master Grid ({state.master_grid.source_region_id})",
            )
        )
        stamped_count += 1

    for gy in state.master_grid.grid_y_m:
        y_px = req.anchor_px.y_px - gy.offset_m * ppm
        if not (ry0 - 2.0 <= y_px <= ry1 + 2.0):
            continue
        y_clamped = min(max(y_px, ry0), ry1)
        state.grid_candidates.append(
            GridCandidate(
                id=f"grid-{uuid4().hex[:8]}",
                page_number=target_region.page_number,
                region_id=target_region_id,
                axis="y",
                label=gy.label,
                x1_px=round(rx0, 2),
                y1_px=round(y_clamped, 2),
                x2_px=round(rx1, 2),
                y2_px=round(y_clamped, 2),
                coord_px=round(y_clamped, 2),
                coord_m=gy.offset_m,
                source="manual",
                confidence=1.0,
                status=ReviewStatus.APPROVED,
                engineer_approved=True,
                notes=f"Stamped from Master Grid ({state.master_grid.source_region_id})",
            )
        )
        stamped_count += 1

    # Recompute coordinates of any existing columns in target region relative to the new anchor
    for col in state.column_candidates:
        if col.region_id == target_region_id:
            col.x_m, col.y_m = _px_to_m_with_origin(col.x_px, col.y_px, ppm, req.anchor_px)

    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "master_grid_stamped",
        {
            "target_region_id": target_region_id,
            "anchor_px": req.anchor_px.model_dump(),
            "pixels_per_m": ppm,
            "stamped_grids": stamped_count,
        },
    )
    return state


def apply_master_grid(
    project_id: str,
    doc_id: str,
    target_region_id: str,
    req: ApplyMasterGridRequest,
) -> ApplyMasterGridResponse:
    state = get_import(project_id, doc_id)
    source_region = _get_region(state, req.source_region_id)
    target_region = _get_region(state, target_region_id)
    if source_region.region_type != "plan" or target_region.region_type != "plan":
        raise ValueError("Master Grid propagation only supports 'plan' regions.")

    if source_region.region_id == target_region.region_id:
        raise ValueError("Source and target regions must differ.")

    src_origin = _resolve_region_origin(state, source_region)
    if src_origin is None:
        raise ValueError(f"Source region '{source_region.name}' requires an origin_px before propagating grids.")

    tgt_origin = target_region.origin_px
    if tgt_origin is None:
        raise ValueError(f"Target region '{target_region.name}' requires an origin_px before receiving the master grid.")

    source_grids = [
        g
        for g in state.grid_candidates
        if (g.status == ReviewStatus.APPROVED or g.engineer_approved)
        and (
            g.region_id == source_region.region_id
            or (
                g.region_id is None
                and g.page_number == source_region.page_number
                and (
                    (g.axis == "x" and source_region.x_px <= g.coord_px <= source_region.x_px + source_region.width_px)
                    or (g.axis == "y" and source_region.y_px <= g.coord_px <= source_region.y_px + source_region.height_px)
                )
            )
        )
    ]
    if not source_grids:
        raise ValueError(f"Source region '{source_region.name}' has no approved grid lines to copy.")

    dx = tgt_origin.x_px - src_origin.x_px
    dy = tgt_origin.y_px - src_origin.y_px

    state.grid_candidates = [
        g
        for g in state.grid_candidates
        if not (g.region_id == target_region_id and g.status == ReviewStatus.PENDING)
    ]
    state.column_candidates = [
        c
        for c in state.column_candidates
        if not (c.region_id == target_region_id and c.status == ReviewStatus.PENDING)
    ]

    tx0, ty0 = target_region.x_px, target_region.y_px
    tx1, ty1 = target_region.x_px + target_region.width_px, target_region.y_px + target_region.height_px
    target_cal = state.calibrations.get(str(target_region.page_number))

    copied_grids: list[GridCandidate] = []
    for sg in source_grids:
        nx1 = min(max(sg.x1_px + dx, tx0), tx1)
        nx2 = min(max(sg.x2_px + dx, tx0), tx1)
        ny1 = min(max(sg.y1_px + dy, ty0), ty1)
        ny2 = min(max(sg.y2_px + dy, ty0), ty1)
        ncoord = (sg.coord_px + dx) if sg.axis == "x" else (sg.coord_px + dy)

        if sg.axis == "x" and not (tx0 <= ncoord <= tx1):
            continue
        if sg.axis == "y" and not (ty0 <= ncoord <= ty1):
            continue

        coord_m = sg.coord_m
        if coord_m is None and target_cal is not None:
            coord_m = (
                _px_to_m_with_origin(ncoord, ny1, target_cal.pixels_per_m, tgt_origin)[0]
                if sg.axis == "x"
                else _px_to_m_with_origin(nx1, ncoord, target_cal.pixels_per_m, tgt_origin)[1]
            )

        copied_grids.append(
            GridCandidate(
                id=f"grid-{uuid4().hex[:8]}",
                page_number=target_region.page_number,
                region_id=target_region_id,
                axis=sg.axis,
                label=sg.label,
                x1_px=round(nx1, 2),
                y1_px=round(ny1, 2),
                x2_px=round(nx2, 2),
                y2_px=round(ny2, 2),
                coord_px=round(ncoord, 2),
                coord_m=coord_m,
                source=sg.source,
                confidence=sg.confidence,
                status=ReviewStatus.PENDING,
                engineer_approved=False,
                notes=f"Propagated from {source_region.name}",
            )
        )

    target_page = _get_page(state, target_region.page_number)
    pdf_path = _original_pdf_path(project_id, doc_id)
    png_path = _pages_dir(project_id, doc_id) / target_page.image_filename

    probed_cols = probe_columns_at_grid_intersections(
        pdf_path=pdf_path,
        png_path=png_path,
        page_info=target_page,
        target_region=target_region,
        target_grids=copied_grids,
        calibration=target_cal,
    )

    existing_col_ids = {c.column_id for c in state.column_candidates}
    next_num = 1
    for col in probed_cols:
        if col.column_id in existing_col_ids:
            while f"C{next_num}" in existing_col_ids:
                next_num += 1
            col.column_id = f"C{next_num}"
        existing_col_ids.add(col.column_id)

    state.grid_candidates.extend(copied_grids)
    state.column_candidates.extend(probed_cols)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "master_grid_applied",
        {
            "source_region_id": source_region.region_id,
            "target_region_id": target_region_id,
            "shift_px": {"dx": round(dx, 2), "dy": round(dy, 2)},
            "copied_grids": len(copied_grids),
            "probed_columns": len(probed_cols),
        },
    )

    return ApplyMasterGridResponse(
        document=state,
        source_region_id=source_region.region_id,
        target_region_id=target_region_id,
        copied_grids=len(copied_grids),
        probed_columns=len(probed_cols),
        notice=(
            f"Copied {len(copied_grids)} master grid line(s) from '{source_region.name}' into "
            f"'{target_region.name}' and probed {len(probed_cols)} intersection column(s)."
        ),
    )


def create_opening(
    project_id: str,
    doc_id: str,
    req: OpeningCreateRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    _get_page(state, req.page_number)
    region = _get_region(state, req.region_id) if req.region_id else None

    floor_level = req.floor_level.strip() or (region.floor_level if region else "")
    approved = req.engineer_approved or req.review_status == ReviewStatus.APPROVED
    opening = DetectedOpening(
        opening_id=f"opn-{uuid4().hex[:8]}",
        page_number=req.page_number,
        region_id=req.region_id,
        floor_level=floor_level,
        category=req.category,
        label=req.label.strip(),
        polygon_px=[PointPx(x_px=round(p.x_px, 2), y_px=round(p.y_px, 2)) for p in req.polygon_px],
        review_status=ReviewStatus.APPROVED if approved else req.review_status,
        engineer_approved=approved,
    )
    state.openings.append(opening)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "opening_created",
        {
            "opening_id": opening.opening_id,
            "page_number": opening.page_number,
            "region_id": opening.region_id,
            "category": opening.category,
            "label": opening.label,
            "vertices": len(opening.polygon_px),
        },
    )
    return state


def delete_opening(
    project_id: str,
    doc_id: str,
    opening_id: str,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    before = len(state.openings)
    state.openings = [o for o in state.openings if o.opening_id != opening_id]
    if len(state.openings) == before:
        raise KeyError(f"Opening '{opening_id}' not found.")
    _save_state(state)
    _append_audit(project_id, doc_id, "opening_deleted", {"opening_id": opening_id})
    return state


def copy_openings(
    project_id: str,
    doc_id: str,
    target_region_id: str,
    req: CopyOpeningsRequest,
) -> CopyOpeningsResponse:
    state = get_import(project_id, doc_id)
    source_region = _get_region(state, req.source_region_id)
    target_region = _get_region(state, target_region_id)
    if source_region.region_type != "plan" or target_region.region_type != "plan":
        raise ValueError("Master Grid propagation only supports 'plan' regions.")

    if source_region.region_id == target_region.region_id:
        raise ValueError("Source and target regions must differ.")

    src_origin = _resolve_region_origin(state, source_region)
    if src_origin is None:
        raise ValueError(f"Source region '{source_region.name}' requires an origin_px before copying openings.")
    tgt_origin = _resolve_region_origin(state, target_region)
    if tgt_origin is None:
        raise ValueError(f"Target region '{target_region.name}' requires an origin_px before receiving openings.")

    src_cal = state.calibrations.get(str(source_region.page_number))
    tgt_cal = state.calibrations.get(str(target_region.page_number))
    src_ppm = src_cal.pixels_per_m if src_cal else (state.master_grid.source_pixels_per_meter if state.master_grid else None)
    if src_ppm is None:
        raise ValueError(f"Page {source_region.page_number} must be calibrated before copying openings via meters.")
    tgt_ppm = tgt_cal.pixels_per_m if tgt_cal else src_ppm

    src_openings = [
        o
        for o in state.openings
        if (o.engineer_approved or o.review_status == ReviewStatus.APPROVED)
        and (
            o.region_id == source_region.region_id
            or (o.region_id is None and o.page_number == source_region.page_number)
        )
    ]
    if not src_openings:
        raise ValueError(f"Source region '{source_region.name}' has no approved openings to copy.")

    copied = 0
    for op in src_openings:
        new_poly: list[PointPx] = []
        for pt in op.polygon_px:
            xm, ym = _px_to_m_with_origin(pt.x_px, pt.y_px, src_ppm, src_origin)
            nx = round(tgt_origin.x_px + xm * tgt_ppm, 2)
            ny = round(tgt_origin.y_px - ym * tgt_ppm, 2)
            new_poly.append(PointPx(x_px=nx, y_px=ny))

        state.openings.append(
            DetectedOpening(
                opening_id=f"opn-{uuid4().hex[:8]}",
                page_number=target_region.page_number,
                region_id=target_region_id,
                floor_level=target_region.floor_level or target_region.name or op.floor_level,
                category=op.category,
                label=op.label,
                polygon_px=new_poly,
                review_status=ReviewStatus.APPROVED,
                engineer_approved=True,
            )
        )
        copied += 1

    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "openings_copied",
        {
            "source_region_id": source_region.region_id,
            "target_region_id": target_region_id,
            "copied_openings": copied,
        },
    )

    return CopyOpeningsResponse(
        document=state,
        source_region_id=source_region.region_id,
        target_region_id=target_region_id,
        copied_openings=copied,
        notice=f"Copied {copied} opening(s) from '{source_region.name}' into '{target_region.name}'.",
    )


def add_manual_column(
    project_id: str,
    doc_id: str,
    req: ManualColumnRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    _get_page(state, req.page_number)
    cal = state.calibrations.get(str(req.page_number))
    region = _find_region(state, req.region_id)

    existing_ids = {c.column_id for c in state.column_candidates}
    col_id = (req.column_id or "").strip()
    if not col_id:
        idx = 1
        while f"C{idx}" in existing_ids:
            idx += 1
        col_id = f"C{idx}"

    x_m, y_m = (None, None)
    width_px, height_px = 18.0, 18.0
    ppm = cal.pixels_per_m if cal is not None else (
        state.master_grid.source_pixels_per_meter if state.master_grid is not None else None
    )
    origin = (region.origin_px if (region and region.origin_px is not None) else (cal.origin_px if cal else None))
    if ppm is not None and origin is not None:
        x_m, y_m = _px_to_m_with_origin(req.x_px, req.y_px, ppm, origin)
        width_px = max(8.0, round((req.width_mm / 1000.0) * ppm, 2))
        height_px = max(8.0, round((req.depth_mm / 1000.0) * ppm, 2))

    candidate = ColumnCandidate(
        id=f"col-{uuid4().hex[:8]}",
        page_number=req.page_number,
        region_id=req.region_id,
        column_id=col_id,
        grid=req.grid.strip(),
        x_px=round(req.x_px, 2),
        y_px=round(req.y_px, 2),
        width_px=width_px,
        height_px=height_px,
        x_m=x_m,
        y_m=y_m,
        width_mm=req.width_mm,
        depth_mm=req.depth_mm,
        source="manual",
        confidence=1.0,
        status=req.status,
        engineer_approved=(req.status == ReviewStatus.APPROVED),
        notes=req.notes,
    )
    state.column_candidates.append(candidate)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "candidate_added",
        {
            "candidate_type": "column",
            "candidate_id": candidate.id,
            "column_id": candidate.column_id,
            "page_number": req.page_number,
            "region_id": req.region_id,
            "x_px": candidate.x_px,
            "y_px": candidate.y_px,
            "x_m": candidate.x_m,
            "y_m": candidate.y_m,
            "status": candidate.status.value,
        },
    )
    return state


def add_manual_grid(
    project_id: str,
    doc_id: str,
    req: ManualGridRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    page = _get_page(state, req.page_number)
    cal = state.calibrations.get(str(req.page_number))
    region = _find_region(state, req.region_id)

    if req.axis == "x":
        x1, y1, x2, y2 = req.coord_px, 0.0, req.coord_px, float(page.height_px)
    else:
        x1, y1, x2, y2 = 0.0, req.coord_px, float(page.width_px), req.coord_px

    coord_m = None
    ppm = cal.pixels_per_m if cal else (state.master_grid.source_pixels_per_meter if state.master_grid else None)
    origin = (region.origin_px if (region and region.origin_px is not None) else (cal.origin_px if cal else None))
    if ppm is not None and origin is not None:
        coord_m = (
            _px_to_m_with_origin(req.coord_px, 0.0, ppm, origin)[0]
            if req.axis == "x"
            else _px_to_m_with_origin(0.0, req.coord_px, ppm, origin)[1]
        )

    candidate = GridCandidate(
        id=f"grid-{uuid4().hex[:8]}",
        page_number=req.page_number,
        region_id=req.region_id,
        axis=req.axis,
        label=req.label.strip(),
        x1_px=round(x1, 2),
        y1_px=round(y1, 2),
        x2_px=round(x2, 2),
        y2_px=round(y2, 2),
        coord_px=round(req.coord_px, 2),
        coord_m=coord_m,
        source="manual",
        confidence=1.0,
        status=req.status,
        engineer_approved=(req.status == ReviewStatus.APPROVED),
        notes=req.notes,
    )
    state.grid_candidates.append(candidate)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "candidate_added",
        {
            "candidate_type": "grid",
            "candidate_id": candidate.id,
            "axis": candidate.axis,
            "label": candidate.label,
            "page_number": req.page_number,
            "coord_px": candidate.coord_px,
            "coord_m": candidate.coord_m,
        },
    )
    return state


def _nearest_grid_ref_px(
    state: ImportDocumentState,
    page_number: int,
    region_id: str | None,
    x_px: float,
    y_px: float,
    tol_px: float,
) -> str:
    x_grids = [
        g
        for g in state.grid_candidates
        if g.page_number == page_number
        and g.axis == "x"
        and g.status != ReviewStatus.REJECTED
        and (region_id is None or g.region_id == region_id or g.region_id is None)
    ]
    y_grids = [
        g
        for g in state.grid_candidates
        if g.page_number == page_number
        and g.axis == "y"
        and g.status != ReviewStatus.REJECTED
        and (region_id is None or g.region_id == region_id or g.region_id is None)
    ]
    best_x = min(x_grids, key=lambda g: abs(g.coord_px - x_px), default=None)
    best_y = min(y_grids, key=lambda g: abs(g.coord_px - y_px), default=None)
    if (
        best_x is not None
        and best_y is not None
        and abs(best_x.coord_px - x_px) <= tol_px
        and abs(best_y.coord_px - y_px) <= tol_px
    ):
        return f"{best_x.label}-{best_y.label}"
    return ""


def _endpoint_anchor_label(
    state: ImportDocumentState,
    page_number: int,
    region_id: str | None,
    x_px: float,
    y_px: float,
    tol_px: float,
) -> str:
    best_col = None
    best_dist = tol_px + 1e-6
    for c in state.column_candidates:
        if (
            c.page_number == page_number
            and c.status != ReviewStatus.REJECTED
            and (region_id is None or c.region_id == region_id or c.region_id is None)
        ):
            d = math.hypot(c.x_px - x_px, c.y_px - y_px)
            if d < best_dist:
                best_dist = d
                best_col = c
    if best_col is not None:
        return best_col.grid or best_col.column_id
    return _nearest_grid_ref_px(state, page_number, region_id, x_px, y_px, tol_px)


def add_manual_beam(
    project_id: str,
    doc_id: str,
    req: ManualBeamRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    _get_page(state, req.page_number)
    region = _get_region(state, req.region_id) if req.region_id else None
    if region is not None and region.region_type != "plan":
        raise ValueError(f"Region '{region.name}' is an elevation region; diagonal beams only target 'plan' regions.")

    if math.hypot(req.x2_px - req.x1_px, req.y2_px - req.y1_px) < 4.0:
        raise ValueError("Beam endpoints must be distinct (at least 4 px apart).")

    existing_ids = {b.beam_id for b in state.beam_candidates if b.beam_id}
    beam_id = (req.beam_id or "").strip()
    if not beam_id:
        idx = 1
        while f"DB{idx}" in existing_ids:
            idx += 1
        beam_id = f"DB{idx}"

    cal = state.calibrations.get(str(req.page_number))
    ppm = cal.pixels_per_m if cal is not None else (
        state.master_grid.source_pixels_per_meter if state.master_grid is not None else None
    )
    origin = (region.origin_px if (region and region.origin_px is not None) else (cal.origin_px if cal else None))

    x1_m = y1_m = x2_m = y2_m = length_m = None
    if ppm is not None and origin is not None:
        x1_m, y1_m = _px_to_m_with_origin(req.x1_px, req.y1_px, ppm, origin)
        x2_m, y2_m = _px_to_m_with_origin(req.x2_px, req.y2_px, ppm, origin)
        length_m = round(math.hypot(x2_m - x1_m, y2_m - y1_m), 4)

    angle_deg = round(
        math.degrees(math.atan2(abs(req.y2_px - req.y1_px), abs(req.x2_px - req.x1_px))),
        2,
    )
    anchor_tol = max(18.0, 0.2 * ppm) if ppm is not None else 20.0
    lbl1 = _endpoint_anchor_label(state, req.page_number, req.region_id, req.x1_px, req.y1_px, anchor_tol)
    lbl2 = _endpoint_anchor_label(state, req.page_number, req.region_id, req.x2_px, req.y2_px, anchor_tol)
    grid_ref = f"{lbl1}->{lbl2}" if (lbl1 and lbl2) else "DIAG"

    candidate = BeamCandidate(
        id=f"beam-{uuid4().hex[:8]}",
        page_number=req.page_number,
        region_id=req.region_id,
        beam_id=beam_id,
        grid_ref=grid_ref,
        member_type=req.member_type,
        x1_px=round(req.x1_px, 2),
        y1_px=round(req.y1_px, 2),
        x2_px=round(req.x2_px, 2),
        y2_px=round(req.y2_px, 2),
        x1_m=x1_m,
        y1_m=y1_m,
        x2_m=x2_m,
        y2_m=y2_m,
        angle_deg=angle_deg,
        length_m=length_m,
        source="manual",
        confidence=1.0,
        status=req.status,
        engineer_approved=(req.status == ReviewStatus.APPROVED),
        notes=req.notes or "Manually drawn diagonal beam",
    )
    state.beam_candidates.append(candidate)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "candidate_added",
        {
            "candidate_type": "beam",
            "candidate_id": candidate.id,
            "beam_id": candidate.beam_id,
            "member_type": candidate.member_type,
            "page_number": req.page_number,
            "region_id": req.region_id,
            "length_m": candidate.length_m,
            "angle_deg": candidate.angle_deg,
            "status": candidate.status.value,
        },
    )
    return state


def update_candidate(
    project_id: str,
    doc_id: str,
    candidate_id: str,
    req: CandidateReviewUpdate,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)

    for grid in state.grid_candidates:
        if grid.id == candidate_id:
            if req.status is not None:
                grid.status = req.status
                grid.engineer_approved = (req.status == ReviewStatus.APPROVED)
            if req.label is not None and req.label.strip():
                grid.label = req.label.strip()
            if req.coord_px is not None:
                grid.coord_px = round(req.coord_px, 2)
                if grid.axis == "x":
                    grid.x1_px = grid.coord_px
                    grid.x2_px = grid.coord_px
                else:
                    grid.y1_px = grid.coord_px
                    grid.y2_px = grid.coord_px
                cal = state.calibrations.get(str(grid.page_number))
                ppm = cal.pixels_per_m if cal is not None else (
                    state.master_grid.source_pixels_per_meter if state.master_grid is not None else None
                )
                reg = _find_region(state, grid.region_id)
                origin = reg.origin_px if (reg and reg.origin_px is not None) else (cal.origin_px if cal else None)
                if ppm is not None and origin is not None:
                    if grid.axis == "x":
                        grid.coord_m, _ = _px_to_m_with_origin(grid.coord_px, grid.y1_px, ppm, origin)
                    else:
                        _, grid.coord_m = _px_to_m_with_origin(grid.x1_px, grid.coord_px, ppm, origin)
            if req.notes is not None:
                grid.notes = req.notes
            _save_state(state)
            _append_audit(
                project_id,
                doc_id,
                "candidate_reviewed",
                {
                    "candidate_type": "grid",
                    "candidate_id": candidate_id,
                    "status": grid.status.value,
                    "label": grid.label,
                    "coord_px": grid.coord_px,
                    "coord_m": grid.coord_m,
                },
            )
            return state

    for col in state.column_candidates:
        if col.id == candidate_id:
            if req.status is not None:
                col.status = req.status
                col.engineer_approved = (req.status == ReviewStatus.APPROVED)
            if req.column_id is not None and req.column_id.strip():
                col.column_id = req.column_id.strip()
            if req.grid is not None:
                col.grid = req.grid.strip()
            if req.width_mm is not None:
                col.width_mm = req.width_mm
            if req.depth_mm is not None:
                col.depth_mm = req.depth_mm
            moved = False
            if req.x_px is not None:
                col.x_px = round(req.x_px, 2)
                moved = True
            if req.y_px is not None:
                col.y_px = round(req.y_px, 2)
                moved = True
            if moved:
                cal = state.calibrations.get(str(col.page_number))
                ppm = cal.pixels_per_m if cal is not None else (
                    state.master_grid.source_pixels_per_meter if state.master_grid is not None else None
                )
                reg = _find_region(state, col.region_id)
                origin = reg.origin_px if (reg and reg.origin_px is not None) else (cal.origin_px if cal else None)
                if ppm is not None and origin is not None:
                    col.x_m, col.y_m = _px_to_m_with_origin(col.x_px, col.y_px, ppm, origin)
                if req.grid is None:
                    tol_px = max(12.0, 0.15 * ppm) if ppm is not None else 15.0
                    inferred = _nearest_grid_ref_px(state, col.page_number, col.region_id, col.x_px, col.y_px, tol_px)
                    if inferred:
                        col.grid = inferred
            if req.notes is not None:
                col.notes = req.notes
            _save_state(state)
            _append_audit(
                project_id,
                doc_id,
                "candidate_reviewed",
                {
                    "candidate_type": "column",
                    "candidate_id": candidate_id,
                    "status": col.status.value,
                    "column_id": col.column_id,
                    "x_px": col.x_px,
                    "y_px": col.y_px,
                    "x_m": col.x_m,
                    "y_m": col.y_m,
                    "grid": col.grid,
                },
            )
            return state

    for beam in state.beam_candidates:
        if beam.id == candidate_id:
            if req.status is not None:
                beam.status = req.status
                beam.engineer_approved = (req.status == ReviewStatus.APPROVED)
            if req.beam_id is not None and req.beam_id.strip():
                beam.beam_id = req.beam_id.strip()
            if req.member_type is not None:
                beam.member_type = req.member_type
            moved = False
            if req.x1_px is not None:
                beam.x1_px = round(req.x1_px, 2)
                moved = True
            if req.y1_px is not None:
                beam.y1_px = round(req.y1_px, 2)
                moved = True
            if req.x2_px is not None:
                beam.x2_px = round(req.x2_px, 2)
                moved = True
            if req.y2_px is not None:
                beam.y2_px = round(req.y2_px, 2)
                moved = True
            if moved:
                beam.angle_deg = round(
                    math.degrees(math.atan2(abs(beam.y2_px - beam.y1_px), abs(beam.x2_px - beam.x1_px))),
                    2,
                )
                cal = state.calibrations.get(str(beam.page_number))
                ppm = cal.pixels_per_m if cal is not None else (
                    state.master_grid.source_pixels_per_meter if state.master_grid is not None else None
                )
                reg = _find_region(state, beam.region_id)
                origin = reg.origin_px if (reg and reg.origin_px is not None) else (cal.origin_px if cal else None)
                if ppm is not None and origin is not None:
                    beam.x1_m, beam.y1_m = _px_to_m_with_origin(beam.x1_px, beam.y1_px, ppm, origin)
                    beam.x2_m, beam.y2_m = _px_to_m_with_origin(beam.x2_px, beam.y2_px, ppm, origin)
                    beam.length_m = round(math.hypot(beam.x2_m - beam.x1_m, beam.y2_m - beam.y1_m), 4)
            if req.notes is not None:
                beam.notes = req.notes
            _save_state(state)
            _append_audit(
                project_id,
                doc_id,
                "candidate_reviewed",
                {
                    "candidate_type": "beam",
                    "candidate_id": candidate_id,
                    "status": beam.status.value,
                    "beam_id": beam.beam_id,
                    "member_type": beam.member_type,
                },
            )
            return state

    raise KeyError(f"Candidate '{candidate_id}' not found.")


def bulk_review_candidates(
    project_id: str,
    doc_id: str,
    req: BulkReviewRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    target_ids = set(req.candidate_ids)
    updated_count = 0

    for grid in state.grid_candidates:
        if grid.id in target_ids:
            grid.status = req.status
            grid.engineer_approved = (req.status == ReviewStatus.APPROVED)
            updated_count += 1
    for col in state.column_candidates:
        if col.id in target_ids:
            col.status = req.status
            col.engineer_approved = (req.status == ReviewStatus.APPROVED)
            updated_count += 1
    for beam in state.beam_candidates:
        if beam.id in target_ids:
            beam.status = req.status
            beam.engineer_approved = (req.status == ReviewStatus.APPROVED)
            updated_count += 1

    if updated_count == 0:
        raise KeyError("No matching candidates found for bulk review.")

    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "candidate_reviewed",
        {
            "bulk": True,
            "status": req.status.value,
            "updated_count": updated_count,
            "candidate_ids": sorted(target_ids),
        },
    )
    return state


def snap_and_clean(
    project_id: str,
    doc_id: str,
    req: SnapAndCleanRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    _get_page(state, req.page_number)

    cal = state.calibrations.get(str(req.page_number))
    if cal is None or not cal.confirmed:
        raise ValueError(
            f"Page {req.page_number} scale calibration must be confirmed before running Snap & Clean."
        )

    if req.region_id is not None:
        reg = _get_region(state, req.region_id)
        if reg.page_number != req.page_number:
            raise ValueError(
                f"Region '{req.region_id}' belongs to page {reg.page_number}, not page {req.page_number}."
            )
        if reg.region_type != "plan":
            raise ValueError(
                f"Region '{reg.name}' is an elevation region; Snap & Clean only targets 'plan' regions."
            )

    elevation_region_ids = {r.region_id for r in state.regions if r.region_type == "elevation"}

    def _in_scope(page_num: int, region_id: str | None, status: ReviewStatus) -> bool:
        if page_num != req.page_number or status == ReviewStatus.REJECTED:
            return False
        if req.region_id is not None:
            return region_id == req.region_id
        return region_id not in elevation_region_ids

    ppm = cal.pixels_per_m
    snap_tol_px = (req.snap_tolerance_mm / 1000.0) * ppm
    merge_grid_tol_px = (req.merge_grid_tolerance_mm / 1000.0) * ppm
    merge_col_tol_px = (req.merge_column_tolerance_mm / 1000.0) * ppm

    merged_grids = 0
    merged_columns = 0
    snapped_columns = 0
    snapped_beams = 0

    if req.merge_duplicates:
        # 1) Merge duplicate same-axis grids within merge_grid_tol_px
        drop_grid_ids: set[str] = set()
        grouped_grids: dict[tuple[str | None, str], list[GridCandidate]] = {}
        for g in state.grid_candidates:
            if _in_scope(g.page_number, g.region_id, g.status):
                grouped_grids.setdefault((g.region_id, g.axis), []).append(g)

        for _, group in grouped_grids.items():
            group.sort(key=lambda g: g.coord_px)
            clusters: list[list[GridCandidate]] = []
            for g in group:
                if clusters and abs(g.coord_px - clusters[-1][-1].coord_px) <= merge_grid_tol_px:
                    clusters[-1].append(g)
                else:
                    clusters.append([g])
            for cl in clusters:
                if len(cl) <= 1:
                    continue
                cl.sort(
                    key=lambda g: (
                        1 if (g.status == ReviewStatus.APPROVED or g.engineer_approved) else 0,
                        1 if g.source == "manual" else 0,
                        1 if (g.label and not g.label.startswith("G")) else 0,
                        g.confidence,
                    ),
                    reverse=True,
                )
                for dup in cl[1:]:
                    drop_grid_ids.add(dup.id)
                    merged_grids += 1
        if drop_grid_ids:
            state.grid_candidates = [g for g in state.grid_candidates if g.id not in drop_grid_ids]

        # 2) Merge duplicate columns within merge_col_tol_px
        drop_col_ids: set[str] = set()
        grouped_cols: dict[str | None, list[ColumnCandidate]] = {}
        for c in state.column_candidates:
            if _in_scope(c.page_number, c.region_id, c.status):
                grouped_cols.setdefault(c.region_id, []).append(c)

        for _, group in grouped_cols.items():
            group.sort(
                key=lambda c: (
                    1 if (c.status == ReviewStatus.APPROVED or c.engineer_approved) else 0,
                    1 if c.source == "manual" else 0,
                    c.confidence,
                ),
                reverse=True,
            )
            kept: list[ColumnCandidate] = []
            for c in group:
                if any(math.hypot(c.x_px - k.x_px, c.y_px - k.y_px) <= merge_col_tol_px for k in kept):
                    drop_col_ids.add(c.id)
                    merged_columns += 1
                else:
                    kept.append(c)
        if drop_col_ids:
            state.column_candidates = [c for c in state.column_candidates if c.id not in drop_col_ids]

    if req.snap_columns:
        scoped_x_grids = [
            g
            for g in state.grid_candidates
            if g.axis == "x" and _in_scope(g.page_number, g.region_id, g.status)
        ]
        scoped_y_grids = [
            g
            for g in state.grid_candidates
            if g.axis == "y" and _in_scope(g.page_number, g.region_id, g.status)
        ]

        for col in state.column_candidates:
            if not _in_scope(col.page_number, col.region_id, col.status):
                continue
            cand_x = [g for g in scoped_x_grids if col.region_id is None or g.region_id in (col.region_id, None)]
            cand_y = [g for g in scoped_y_grids if col.region_id is None or g.region_id in (col.region_id, None)]
            best_x = min(cand_x, key=lambda g: abs(g.coord_px - col.x_px), default=None)
            best_y = min(cand_y, key=lambda g: abs(g.coord_px - col.y_px), default=None)
            changed = False
            matched_x = None
            matched_y = None
            if best_x is not None and abs(best_x.coord_px - col.x_px) <= snap_tol_px:
                if abs(best_x.coord_px - col.x_px) > 1e-3:
                    col.x_px = round(best_x.coord_px, 2)
                    changed = True
                matched_x = best_x
            if best_y is not None and abs(best_y.coord_px - col.y_px) <= snap_tol_px:
                if abs(best_y.coord_px - col.y_px) > 1e-3:
                    col.y_px = round(best_y.coord_px, 2)
                    changed = True
                matched_y = best_y
            if matched_x is not None and matched_y is not None:
                new_ref = f"{matched_x.label}-{matched_y.label}"
                if col.grid != new_ref:
                    col.grid = new_ref
                    changed = True
            if changed:
                snapped_columns += 1

        # Also snap diagonal beam endpoints to nearest column center or grid intersection
        anchors: list[tuple[float, float, str, str | None]] = []
        for col in state.column_candidates:
            if _in_scope(col.page_number, col.region_id, col.status):
                anchors.append((col.x_px, col.y_px, col.grid or col.column_id, col.region_id))
        for gx in scoped_x_grids:
            for gy in scoped_y_grids:
                if gx.region_id == gy.region_id or gx.region_id is None or gy.region_id is None:
                    anchors.append((gx.coord_px, gy.coord_px, f"{gx.label}-{gy.label}", gx.region_id or gy.region_id))

        for bm in state.beam_candidates:
            if not _in_scope(bm.page_number, bm.region_id, bm.status):
                continue
            reg_anchors = [a for a in anchors if bm.region_id is None or a[3] in (bm.region_id, None)]
            if not reg_anchors:
                continue
            a1 = min(reg_anchors, key=lambda a: math.hypot(a[0] - bm.x1_px, a[1] - bm.y1_px))
            a2 = min(reg_anchors, key=lambda a: math.hypot(a[0] - bm.x2_px, a[1] - bm.y2_px))
            nx1, ny1, lbl1 = bm.x1_px, bm.y1_px, ""
            nx2, ny2, lbl2 = bm.x2_px, bm.y2_px, ""
            if math.hypot(a1[0] - bm.x1_px, a1[1] - bm.y1_px) <= snap_tol_px:
                nx1, ny1, lbl1 = round(a1[0], 2), round(a1[1], 2), a1[2]
            if math.hypot(a2[0] - bm.x2_px, a2[1] - bm.y2_px) <= snap_tol_px:
                nx2, ny2, lbl2 = round(a2[0], 2), round(a2[1], 2), a2[2]
            if math.hypot(nx2 - nx1, ny2 - ny1) >= 4.0 and (
                nx1 != bm.x1_px or ny1 != bm.y1_px or nx2 != bm.x2_px or ny2 != bm.y2_px
            ):
                bm.x1_px, bm.y1_px, bm.x2_px, bm.y2_px = nx1, ny1, nx2, ny2
                if lbl1 and lbl2:
                    bm.grid_ref = f"{lbl1}->{lbl2}"
                snapped_beams += 1

    _recalculate_page_coordinates(state, req.page_number)
    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "snap_and_clean",
        {
            "page_number": req.page_number,
            "region_id": req.region_id,
            "snap_tolerance_mm": req.snap_tolerance_mm,
            "merge_grid_tolerance_mm": req.merge_grid_tolerance_mm,
            "merge_column_tolerance_mm": req.merge_column_tolerance_mm,
            "merged_grids": merged_grids,
            "merged_columns": merged_columns,
            "snapped_columns": snapped_columns,
            "snapped_beams": snapped_beams,
        },
    )
    return state


def get_preflight_diagnostics(
    project_id: str,
    doc_id: str,
    page_number: int,
    region_id: str | None = None,
) -> PreflightDiagnosticsResponse:
    project = _ensure_project_exists(project_id)
    state = _load_state(project_id, doc_id)
    _get_page(state, page_number)

    if region_id is not None:
        reg = _get_region(state, region_id)
        if reg.page_number != page_number:
            raise ValueError(
                f"Region '{region_id}' belongs to page {reg.page_number}, not page {page_number}."
            )
        if reg.region_type != "plan":
            raise ValueError(
                f"Region '{reg.name}' is an elevation region; pre-flight diagnostics only inspect 'plan' regions."
            )

    elevation_region_ids = {r.region_id for r in state.regions if r.region_type == "elevation"}

    def _in_scope(pnum: int, reg_id: str | None, status: ReviewStatus) -> bool:
        if pnum != page_number or status == ReviewStatus.REJECTED:
            return False
        if region_id is not None:
            return reg_id == region_id
        return reg_id not in elevation_region_ids

    x_grids = [
        g for g in state.grid_candidates if g.axis == "x" and _in_scope(g.page_number, g.region_id, g.status)
    ]
    y_grids = [
        g for g in state.grid_candidates if g.axis == "y" and _in_scope(g.page_number, g.region_id, g.status)
    ]
    cols = [
        c for c in state.column_candidates if _in_scope(c.page_number, c.region_id, c.status)
    ]
    beams = [
        b for b in state.beam_candidates if _in_scope(b.page_number, b.region_id, b.status)
    ]

    cal = state.calibrations.get(str(page_number))
    ppm = cal.pixels_per_m if cal is not None else (
        state.master_grid.source_pixels_per_meter if state.master_grid is not None else None
    )

    x_coords_m = sorted([g.coord_m for g in x_grids if g.coord_m is not None])
    y_coords_m = sorted([g.coord_m for g in y_grids if g.coord_m is not None])
    footprint_x_m = round(x_coords_m[-1] - x_coords_m[0], 3) if len(x_coords_m) >= 2 else None
    footprint_y_m = round(y_coords_m[-1] - y_coords_m[0], 3) if len(y_coords_m) >= 2 else None

    bays: list[float] = []
    for arr in (x_coords_m, y_coords_m):
        for i in range(len(arr) - 1):
            span = round(arr[i + 1] - arr[i], 3)
            if span > 1e-4:
                bays.append(span)
    min_bay_m = min(bays) if bays else None
    max_bay_m = max(bays) if bays else None

    issues: list[DiagnosticIssue] = []

    # 1) Duplicate same-axis grids (< 150 mm, or < 8 px if uncalibrated)
    dup_grid_tol_px = (0.15 * ppm) if ppm is not None else 8.0
    dup_grid_ids: list[str] = []
    for axis_name, arr in (("X", x_grids), ("Y", y_grids)):
        arr_sorted = sorted(arr, key=lambda g: g.coord_px)
        for i in range(len(arr_sorted) - 1):
            g1, g2 = arr_sorted[i], arr_sorted[i + 1]
            if g1.region_id != g2.region_id:
                continue
            gap_px = abs(g2.coord_px - g1.coord_px)
            if gap_px < dup_grid_tol_px:
                dup_grid_ids.extend([g1.id, g2.id])
    if dup_grid_ids:
        uniq = list(dict.fromkeys(dup_grid_ids))
        issues.append(
            DiagnosticIssue(
                severity="warning",
                code="duplicate_grids",
                message=f"{len(uniq)} grid line(s) are closer than 150 mm to an adjacent same-axis grid. Run Merge Duplicates.",
                candidate_ids=uniq,
            )
        )

    # 2) Duplicate columns (< 200 mm, or < 10 px if uncalibrated)
    dup_col_tol_px = (0.20 * ppm) if ppm is not None else 10.0
    dup_col_ids: list[str] = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            if cols[i].region_id != cols[j].region_id:
                continue
            if math.hypot(cols[i].x_px - cols[j].x_px, cols[i].y_px - cols[j].y_px) < dup_col_tol_px:
                dup_col_ids.extend([cols[i].id, cols[j].id])
    if dup_col_ids:
        uniq = list(dict.fromkeys(dup_col_ids))
        issues.append(
            DiagnosticIssue(
                severity="warning",
                code="duplicate_columns",
                message=f"{len(uniq)} column(s) overlap within 200 mm. Run Merge Duplicates.",
                candidate_ids=uniq,
            )
        )

    # 3) Orphan columns (> 150 mm from any X x Y grid intersection)
    if x_grids and y_grids:
        orphan_tol_px = (0.15 * ppm) if ppm is not None else 12.0
        orphan_ids: list[str] = []
        for col in cols:
            dx = min((abs(col.x_px - gx.coord_px) for gx in x_grids), default=1e9)
            dy = min((abs(col.y_px - gy.coord_px) for gy in y_grids), default=1e9)
            if math.hypot(dx, dy) > orphan_tol_px:
                orphan_ids.append(col.id)
        if orphan_ids:
            issues.append(
                DiagnosticIssue(
                    severity="warning",
                    code="orphan_columns",
                    message=f"{len(orphan_ids)} column(s) are > 150 mm from any grid intersection. Run Snap Columns to Grids.",
                    candidate_ids=orphan_ids,
                )
            )

    # 4) Extreme bay spans (< 1.0 m or > 12.0 m)
    extreme_grid_ids: list[str] = []
    extreme_spans: list[str] = []
    for axis_name, arr in (("X", x_grids), ("Y", y_grids)):
        arr_m = sorted([g for g in arr if g.coord_m is not None], key=lambda g: g.coord_m or 0.0)
        for i in range(len(arr_m) - 1):
            g1, g2 = arr_m[i], arr_m[i + 1]
            if g1.region_id != g2.region_id:
                continue
            span = abs((g2.coord_m or 0.0) - (g1.coord_m or 0.0))
            if span > 0.15 and (span < 1.0 or span > 12.0):
                extreme_grid_ids.extend([g1.id, g2.id])
                extreme_spans.append(f"{g1.label}-{g2.label}={span:.2f}m")
    if extreme_grid_ids:
        uniq = list(dict.fromkeys(extreme_grid_ids))
        issues.append(
            DiagnosticIssue(
                severity="warning",
                code="extreme_bay_span",
                message=f"Unusual bay span(s) detected (<1.0 m or >12.0 m): {', '.join(extreme_spans[:4])}. Check scale/unit.",
                candidate_ids=uniq,
            )
        )

    # 5) Disconnected diagonal beams (either endpoint > 250 mm from all columns and grid intersections)
    if beams:
        beam_tol_px = (0.25 * ppm) if ppm is not None else 18.0
        anchor_pts: list[tuple[float, float]] = [(c.x_px, c.y_px) for c in cols]
        for gx in x_grids:
            for gy in y_grids:
                anchor_pts.append((gx.coord_px, gy.coord_px))
        disconnected_ids: list[str] = []
        for bm in beams:
            if not anchor_pts:
                disconnected_ids.append(bm.id)
                continue
            d1 = min(math.hypot(bm.x1_px - ax, bm.y1_px - ay) for ax, ay in anchor_pts)
            d2 = min(math.hypot(bm.x2_px - ax, bm.y2_px - ay) for ax, ay in anchor_pts)
            if bm.member_type == "cantilever_beam":
                # Cantilever beams only require one anchored support end; flag only if NEITHER end is anchored
                if d1 > beam_tol_px and d2 > beam_tol_px:
                    disconnected_ids.append(bm.id)
            elif d1 > beam_tol_px or d2 > beam_tol_px:
                disconnected_ids.append(bm.id)
        if disconnected_ids:
            issues.append(
                DiagnosticIssue(
                    severity="warning",
                    code="disconnected_beam",
                    message=f"{len(disconnected_ids)} diagonal beam(s) have an endpoint > 250 mm from any column or grid intersection.",
                    candidate_ids=disconnected_ids,
                )
            )

    # 6) Missing floor-level assignment on plan regions for multi-story projects
    if len(project.levels) > 2:
        plan_regions = [
            r
            for r in state.regions
            if r.page_number == page_number
            and r.region_type == "plan"
            and (region_id is None or r.region_id == region_id)
        ]
        unassigned = [r.name for r in plan_regions if not r.floor_level.strip()]
        if unassigned:
            issues.append(
                DiagnosticIssue(
                    severity="info",
                    code="missing_floor_level",
                    message=f"Plan region(s) missing floor level assignment ({', '.join(unassigned[:3])}). Assign a level or 'ALL'.",
                    candidate_ids=[],
                )
            )

    return PreflightDiagnosticsResponse(
        doc_id=doc_id,
        page_number=page_number,
        x_grid_count=len(x_grids),
        y_grid_count=len(y_grids),
        column_count=len(cols),
        approved_column_count=sum(1 for c in cols if c.status == ReviewStatus.APPROVED or c.engineer_approved),
        diagonal_beam_count=len(beams),
        approved_diagonal_beam_count=sum(1 for b in beams if b.status == ReviewStatus.APPROVED or b.engineer_approved),
        footprint_x_m=footprint_x_m,
        footprint_y_m=footprint_y_m,
        min_bay_m=min_bay_m,
        max_bay_m=max_bay_m,
        issues=issues,
    )


def transfer_approved(
    project_id: str,
    doc_id: str,
    req: TransferRequest,
) -> TransferResponse:
    project = _ensure_project_exists(project_id)
    state = _load_state(project_id, doc_id)

    if req.region_id is not None:
        target_reg = _get_region(state, req.region_id)
        if target_reg.region_type != "plan":
            raise ValueError(
                f"Region '{target_reg.name}' is an elevation region; transfer only targets 'plan' regions."
            )

    elevation_region_ids = {r.region_id for r in state.regions if r.region_type == "elevation"}

    approved_grids = [
        g
        for g in state.grid_candidates
        if (g.status == ReviewStatus.APPROVED or g.engineer_approved)
        and (g.region_id not in elevation_region_ids)
        and (req.page_number is None or g.page_number == req.page_number)
        and (req.region_id is None or g.region_id == req.region_id)
    ]
    approved_cols = [
        c
        for c in state.column_candidates
        if (c.status == ReviewStatus.APPROVED or c.engineer_approved)
        and (c.region_id not in elevation_region_ids)
        and (req.page_number is None or c.page_number == req.page_number)
        and (req.region_id is None or c.region_id == req.region_id)
    ]
    approved_openings = [
        o
        for o in state.openings
        if (o.engineer_approved or o.review_status == ReviewStatus.APPROVED)
        and (o.region_id not in elevation_region_ids)
        and (req.page_number is None or o.page_number == req.page_number)
        and (req.region_id is None or o.region_id == req.region_id)
    ]
    approved_beams = [
        b
        for b in state.beam_candidates
        if (b.status == ReviewStatus.APPROVED or b.engineer_approved)
        and (b.region_id not in elevation_region_ids)
        and (req.page_number is None or b.page_number == req.page_number)
        and (req.region_id is None or b.region_id == req.region_id)
    ]

    if not approved_grids and not approved_cols and not approved_openings and not approved_beams:
        raise ValueError(
            "No engineer-approved grids, columns, beams, or openings are ready to transfer. "
            "Approve at least one element before transferring."
        )

    involved_pages = sorted(
        {g.page_number for g in approved_grids}
        | {c.page_number for c in approved_cols}
        | {o.page_number for o in approved_openings}
        | {b.page_number for b in approved_beams}
    )

    # Verify scale confirmation for every involved page and recompute coordinates cleanly
    for g in approved_grids:
        ppm, origin = _resolve_scale_and_origin(state, g.page_number, g.region_id, require_confirmed=True)
        if g.axis == "x":
            g.coord_m, _ = _px_to_m_with_origin(g.coord_px, g.y1_px, ppm, origin)
        else:
            _, g.coord_m = _px_to_m_with_origin(g.x1_px, g.coord_px, ppm, origin)

    for c in approved_cols:
        ppm, origin = _resolve_scale_and_origin(state, c.page_number, c.region_id, require_confirmed=True)
        c.x_m, c.y_m = _px_to_m_with_origin(c.x_px, c.y_px, ppm, origin)

    for b in approved_beams:
        ppm, origin = _resolve_scale_and_origin(state, b.page_number, b.region_id, require_confirmed=True)
        b.x1_m, b.y1_m = _px_to_m_with_origin(b.x1_px, b.y1_px, ppm, origin)
        b.x2_m, b.y2_m = _px_to_m_with_origin(b.x2_px, b.y2_px, ppm, origin)
        b.length_m = round(math.hypot(b.x2_m - b.x1_m, b.y2_m - b.y1_m), 4)

    seen_batch_col_ids: set[str] = set()
    for c in approved_cols:
        if c.column_id in seen_batch_col_ids:
            raise ValueError(
                f"Duplicate approved column ID '{c.column_id}'. Rename duplicate columns before transferring."
            )
        seen_batch_col_ids.add(c.column_id)

    if req.merge_mode == "replace_geometry":
        x_map: dict[str, GridLine] = {}
        y_map: dict[str, GridLine] = {}
        col_map: dict[str, Column] = {}
        opening_map: dict[str, FloorOpening] = {}
    else:
        x_map = {g.label: g for g in project.grids.x_grids}
        y_map = {g.label: g for g in project.grids.y_grids}
        col_map = {c.id: c for c in project.columns}
        opening_map = {o.id: o for o in project.openings}

    x_count = 0
    y_count = 0
    for g in approved_grids:
        assert g.coord_m is not None
        gl = GridLine(label=g.label, coord_m=round(g.coord_m, 4))
        if g.axis == "x":
            x_map[g.label] = gl
            x_count += 1
        else:
            y_map[g.label] = gl
            y_count += 1

    def _column_floors_overlap(fl_a: str, fl_b: str) -> bool:
        a = fl_a.strip().upper()
        b = fl_b.strip().upper()
        if not a or a == "ALL" or not b or b == "ALL":
            return True
        return a == b

    for c in approved_cols:
        assert c.x_m is not None and c.y_m is not None
        reg = _find_region(state, c.region_id)
        inc_fl = reg.floor_level if (reg and reg.floor_level) else ""
        inc_x = round(c.x_m, 4)
        inc_y = round(c.y_m, 4)

        target_key = c.column_id
        if req.merge_mode == "merge" and target_key not in col_map:
            colocated = next(
                (
                    existing
                    for existing in col_map.values()
                    if math.hypot(existing.x_m - inc_x, existing.y_m - inc_y) <= 0.10 + 1e-9
                    and _column_floors_overlap(existing.floor_level, inc_fl)
                ),
                None,
            )
            if colocated is not None:
                target_key = colocated.id
                col_map[target_key] = Column(
                    id=colocated.id,
                    grid=c.grid or colocated.grid,
                    floor_level=inc_fl or colocated.floor_level,
                    x_m=inc_x,
                    y_m=inc_y,
                    width_mm=c.width_mm,
                    depth_mm=c.depth_mm,
                )
                continue

        col_map[target_key] = Column(
            id=target_key,
            grid=c.grid,
            floor_level=inc_fl,
            x_m=inc_x,
            y_m=inc_y,
            width_mm=c.width_mm,
            depth_mm=c.depth_mm,
        )

    openings_transferred = 0
    for o in approved_openings:
        ppm, origin = _resolve_scale_and_origin(state, o.page_number, o.region_id, require_confirmed=True)
        poly_m = [
            list(_px_to_m_with_origin(pt.x_px, pt.y_px, ppm, origin))
            for pt in o.polygon_px
        ]
        area_sqm = _polygon_area_sqm(poly_m)
        if area_sqm <= 0.0:
            raise ValueError(f"Opening '{o.label}' has zero polygon area; check its vertices.")
        reg = _find_region(state, o.region_id)
        floor_lbl = o.floor_level or (reg.floor_level if reg else "")
        opening_map[o.opening_id] = FloorOpening(
            id=o.opening_id,
            label=o.label,
            category=o.category,
            floor_level=floor_lbl,
            polygon_m=poly_m,
            area_sqm=area_sqm,
        )
        openings_transferred += 1

    new_x_grids = sorted(x_map.values(), key=lambda g: g.coord_m)
    new_y_grids = sorted(y_map.values(), key=lambda g: g.coord_m)
    new_columns = list(col_map.values())
    new_openings = list(opening_map.values())
    valid_col_ids = {c.id for c in new_columns}
    new_beams = [
        b for b in project.beams if b.start_col in valid_col_ids and b.end_col in valid_col_ids
    ]

    updated_payload = project.model_dump()
    updated_payload["grids"] = {
        "x_grids": [g.model_dump() for g in new_x_grids],
        "y_grids": [g.model_dump() for g in new_y_grids],
    }
    updated_payload["columns"] = [c.model_dump() for c in new_columns]
    updated_payload["beams"] = [b.model_dump() for b in new_beams]
    updated_payload["openings"] = [o.model_dump() for o in new_openings]

    roof_note = ""
    if req.auto_add_roof_level:
        existing_levels = [Level.model_validate(lv) for lv in updated_payload.get("levels", [])]
        existing_roof = next(
            (
                lv
                for lv in existing_levels
                if "ROOF" in lv.id.upper() or lv.id.upper() in {"RB", "RD", "RF"}
            ),
            None,
        )
        if existing_roof is not None:
            roof_note = (
                f" Reused existing roof datum '{existing_roof.id}' at +{existing_roof.elevation_m:.2f} m "
                f"({req.roof_framing_style.replace('_', ' ')})."
            )
        elif existing_levels:
            top_elev = max(lv.elevation_m for lv in existing_levels)
            roof_elev = round(top_elev + req.default_roof_height_m, 3)
            existing_levels.append(Level(id="ROOF", elevation_m=roof_elev))
            existing_levels.sort(key=lambda lv: lv.elevation_m)
            updated_payload["levels"] = [lv.model_dump() for lv in existing_levels]
            roof_note = (
                f" Auto-added ROOF level at +{roof_elev:.2f} m "
                f"({req.roof_framing_style.replace('_', ' ')})."
            )
        sm_dict = updated_payload.get("structural_model") or {}
        sm_opts = sm_dict.get("options") or {}
        sm_opts["roof_framing_style"] = req.roof_framing_style
        sm_dict["options"] = sm_opts
        updated_payload["structural_model"] = sm_dict

    beams_transferred = 0
    if approved_beams:
        sm_dict = updated_payload.get("structural_model") or {}
        existing_overrides = [
            ManualBeamOverride.model_validate(mb) for mb in sm_dict.get("manual_beams", [])
        ]
        # Always preserve user-authored Module 03 overrides (non imp_beam_ IDs)
        preserved = [
            mb
            for mb in existing_overrides
            if not mb.override_id.startswith("imp_beam_") or req.merge_mode == "merge"
        ]
        override_map: dict[str, ManualBeamOverride] = {mb.override_id: mb for mb in preserved}

        proj_levels = [Level.model_validate(lv) for lv in updated_payload.get("levels", [])]
        proj_levels.sort(key=lambda lv: lv.elevation_m)
        min_elev = proj_levels[0].elevation_m if proj_levels else 0.0
        elevated_levels = [lv for lv in proj_levels if lv.elevation_m > min_elev + 1e-4]
        default_target_levels = elevated_levels if elevated_levels else proj_levels

        for b in approved_beams:
            assert b.x1_m is not None and b.y1_m is not None and b.x2_m is not None and b.y2_m is not None
            reg = _find_region(state, b.region_id)
            fl = (reg.floor_level.strip() if reg and reg.floor_level else "")
            if fl and fl.upper() != "ALL":
                matched_lvls = [lv for lv in proj_levels if lv.id.upper() == fl.upper()]
                target_lvls = matched_lvls if matched_lvls else default_target_levels
            else:
                target_lvls = default_target_levels
            for lv in target_lvls:
                ov_id = f"imp_beam_{b.id}_{lv.id}"
                override_map[ov_id] = ManualBeamOverride(
                    override_id=ov_id,
                    floor_level=lv.id,
                    start_xyz_m=[round(b.x1_m, 4), round(lv.elevation_m, 4), round(b.y1_m, 4)],
                    end_xyz_m=[round(b.x2_m, 4), round(lv.elevation_m, 4), round(b.y2_m, 4)],
                    member_type=b.member_type,
                    grid_ref=b.grid_ref or b.beam_id or "DIAG",
                )
            beams_transferred += 1

        sm_dict["manual_beams"] = [mb.model_dump() for mb in override_map.values()]
        updated_payload["structural_model"] = sm_dict

    updated_project = ProjectState.model_validate(updated_payload)

    storage.save_project(updated_project)

    for g in approved_grids:
        g.transferred = True
    for c in approved_cols:
        c.transferred = True
    for o in approved_openings:
        o.transferred = True
    for b in approved_beams:
        b.transferred = True

    record = TransferRecord(
        transferred_at=_utc_now(),
        page_numbers=involved_pages,
        merge_mode=req.merge_mode,
        x_grids_count=x_count,
        y_grids_count=y_count,
        columns_count=len(approved_cols),
        openings_count=openings_transferred,
    )
    state.transfers.append(record)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "transferred_to_project",
        {
            "page_numbers": involved_pages,
            "merge_mode": req.merge_mode,
            "x_grids_transferred": x_count,
            "y_grids_transferred": y_count,
            "columns_transferred": len(approved_cols),
            "openings_transferred": openings_transferred,
            "beams_transferred": beams_transferred,
            "column_ids": [c.column_id for c in approved_cols],
        },
    )

    beam_note = f", and {beams_transferred} diagonal beam(s)" if beams_transferred else ""
    return TransferResponse(
        project_id=project_id,
        doc_id=doc_id,
        transferred_x_grids=x_count,
        transferred_y_grids=y_count,
        transferred_columns=len(approved_cols),
        transferred_openings=openings_transferred,
        total_project_columns=len(updated_project.columns),
        total_project_openings=len(updated_project.openings),
        notice=(
            f"Transferred {x_count} X-grid(s), {y_count} Y-grid(s), "
            f"{len(approved_cols)} column(s), {openings_transferred} opening(s){beam_note} "
            f"into project '{project_id}'.{roof_note} Independent structural review remains required."
        ),
        project=updated_project,
        document=state,
    )


approve_and_transfer = transfer_approved


def read_audit_log(project_id: str, doc_id: str) -> list[AuditEntry]:
    get_import(project_id, doc_id)
    path = _audit_path(project_id, doc_id)
    if not path.exists():
        return []
    entries: list[AuditEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(AuditEntry.model_validate(json.loads(line)))
    return entries
