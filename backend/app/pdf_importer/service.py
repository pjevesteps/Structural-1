from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

import pymupdf

from .. import storage
from ..models import Column, FloorOpening, GridLine, Level, ProjectState
from .detector import (
    extract_levels_from_elevation_or_section,
    probe_columns_at_grid_intersections,
    run_detection,
)
from .inspector import (
    DEFAULT_RENDER_DPI,
    InvalidPdfError,
    _extract_text_blocks_px,
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
    BulkReviewRequest,
    CandidateReviewUpdate,
    ColumnCandidate,
    CopyOpeningsRequest,
    CopyOpeningsResponse,
    DetectedOpening,
    ExtractRequest,
    ExtractionResponse,
    GridCandidate,
    ImportDocumentState,
    ImportDocumentSummary,
    LevelExtractionResponse,
    LevelUpdateRequest,
    LockMasterGridRequest,
    ManualColumnRequest,
    ManualGridRequest,
    ManualGridTraceRequest,
    MasterGridDefinition,
    MasterGridLine,
    OpeningCreateRequest,
    PageUpdateRequest,
    PlanRegion,
    PointPx,
    RegionCreateRequest,
    RegionUpdateRequest,
    ReviewStatus,
    ScaleCalibration,
    ScaleCalibrationRequest,
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
    root = storage.DATA_DIR.parent / "imports"
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
        pages, regions = inspect_and_render_pdf(pdf_path, pages_dir, dpi=dpi)
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

    if page.sheet_type == "floor_plan" and not any(r.page_number == page_number for r in state.regions):
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
        floor_level=req.floor_level.strip(),
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
    if req.floor_level is not None:
        region.floor_level = req.floor_level.strip()
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

    output = run_detection(
        pdf_path=pdf_path,
        png_path=png_path,
        page_info=page,
        calibration=cal,
        method=req.method,
        options=options,
        region=region,
        existing_grids=existing_region_grids,
        pixels_per_meter=effective_ppm,
        origin_px=effective_origin,
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

    existing_col_ids = {c.column_id for c in state.column_candidates}
    next_num = 1
    for col in output.columns:
        if col.column_id in existing_col_ids:
            while f"C{next_num}" in existing_col_ids:
                next_num += 1
            col.column_id = f"C{next_num}"
        existing_col_ids.add(col.column_id)

    if options.detect_grids:
        state.grid_candidates.extend(output.grids)
    if options.detect_columns:
        state.column_candidates.extend(output.columns)
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
) -> LevelExtractionResponse:
    state = get_import(project_id, doc_id)
    page = _get_page(state, page_number)

    pdf_path = _original_pdf_path(project_id, doc_id)
    png_path = _pages_dir(project_id, doc_id) / page.image_filename

    levels, warnings = extract_levels_from_elevation_or_section(pdf_path, png_path, page)

    state.detected_levels = [
        lvl
        for lvl in state.detected_levels
        if not (lvl.page_number == page_number and not lvl.engineer_approved)
    ]
    state.detected_levels.extend(levels)
    _save_state(state)

    _append_audit(
        project_id,
        doc_id,
        "levels_extracted",
        {
            "page_number": page_number,
            "sheet_type": page.sheet_type,
            "levels_detected": len(levels),
            "warnings": warnings,
        },
    )

    notice = (
        f"Extracted {len(levels)} structural floor-to-floor level(s) from page {page_number}."
        + (f" Warnings: {' '.join(warnings)}" if warnings else " Review and approve levels before transferring.")
    )
    return LevelExtractionResponse(
        document=state,
        page_number=page_number,
        levels_detected=len(levels),
        warnings=warnings,
        notice=notice,
    )


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
        },
    )

    return TransferLevelsResponse(
        project_id=project_id,
        doc_id=doc_id,
        transferred_levels=len(approved_levels),
        total_project_levels=len(updated_project.levels),
        notice=(
            f"Transferred {len(approved_levels)} engineer-approved level(s) into project '{project_id}' "
            f"(sorted by elevation_m)."
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

    state.grid_candidates = [g for g in state.grid_candidates if g.id != element_id]
    state.column_candidates = [c for c in state.column_candidates if c.id != element_id]

    if len(state.grid_candidates) == orig_g and len(state.column_candidates) == orig_c:
        raise KeyError(f"Element '{element_id}' not found.")

    elem_type = "grid" if len(state.grid_candidates) < orig_g else "column"
    _save_state(state)
    _append_audit(
        project_id,
        doc_id,
        "element_deleted",
        {"element_id": element_id, "element_type": elem_type},
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


def transfer_approved(
    project_id: str,
    doc_id: str,
    req: TransferRequest,
) -> TransferResponse:
    project = _ensure_project_exists(project_id)
    state = _load_state(project_id, doc_id)

    approved_grids = [
        g
        for g in state.grid_candidates
        if (g.status == ReviewStatus.APPROVED or g.engineer_approved)
        and (req.page_number is None or g.page_number == req.page_number)
        and (req.region_id is None or g.region_id == req.region_id)
    ]
    approved_cols = [
        c
        for c in state.column_candidates
        if (c.status == ReviewStatus.APPROVED or c.engineer_approved)
        and (req.page_number is None or c.page_number == req.page_number)
        and (req.region_id is None or c.region_id == req.region_id)
    ]
    approved_openings = [
        o
        for o in state.openings
        if (o.engineer_approved or o.review_status == ReviewStatus.APPROVED)
        and (req.page_number is None or o.page_number == req.page_number)
        and (req.region_id is None or o.region_id == req.region_id)
    ]

    if not approved_grids and not approved_cols and not approved_openings:
        raise ValueError(
            "No engineer-approved grids, columns, or openings are ready to transfer. "
            "Approve at least one element before transferring."
        )

    involved_pages = sorted(
        {g.page_number for g in approved_grids}
        | {c.page_number for c in approved_cols}
        | {o.page_number for o in approved_openings}
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

    for c in approved_cols:
        assert c.x_m is not None and c.y_m is not None
        reg = _find_region(state, c.region_id)
        col_map[c.column_id] = Column(
            id=c.column_id,
            grid=c.grid,
            floor_level=reg.floor_level if (reg and reg.floor_level) else "",
            x_m=round(c.x_m, 4),
            y_m=round(c.y_m, 4),
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
    updated_project = ProjectState.model_validate(updated_payload)

    storage.save_project(updated_project)

    for g in approved_grids:
        g.transferred = True
    for c in approved_cols:
        c.transferred = True
    for o in approved_openings:
        o.transferred = True

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
            "column_ids": [c.column_id for c in approved_cols],
        },
    )

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
            f"{len(approved_cols)} column(s), and {openings_transferred} opening(s) "
            f"into project '{project_id}'. Independent structural review remains required."
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
