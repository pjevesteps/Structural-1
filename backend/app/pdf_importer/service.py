from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .. import storage
from ..models import Column, GridLine, Level, ProjectState
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
    ExtractRequest,
    ExtractionResponse,
    GridCandidate,
    ImportDocumentState,
    ImportDocumentSummary,
    LevelExtractionResponse,
    LevelUpdateRequest,
    ManualColumnRequest,
    ManualGridRequest,
    PageUpdateRequest,
    PlanRegion,
    PointPx,
    RegionCreateRequest,
    RegionUpdateRequest,
    ReviewStatus,
    ScaleCalibration,
    ScaleCalibrationRequest,
    TransferLevelsRequest,
    TransferLevelsResponse,
    TransferRecord,
    TransferRequest,
    TransferResponse,
)
import pymupdf


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


def _recalculate_page_coordinates(state: ImportDocumentState, page_number: int) -> None:
    cal = state.calibrations.get(str(page_number))
    if cal is None:
        return
    for grid in state.grid_candidates:
        if grid.page_number != page_number:
            continue
        if grid.axis == "x":
            grid.coord_m, _ = px_to_plan_m(grid.coord_px, grid.y1_px, cal)
        else:
            _, grid.coord_m = px_to_plan_m(grid.x1_px, grid.coord_px, cal)
    for col in state.column_candidates:
        if col.page_number != page_number:
            continue
        col.x_m, col.y_m = px_to_plan_m(col.x_px, col.y_px, cal)


def _recompute_page_story_heights(state: ImportDocumentState, page_number: int) -> None:
    page_lvls = [lvl for lvl in state.detected_levels if lvl.page_number == page_number]
    page_lvls.sort(key=lambda l: (-l.y_px, l.elevation_m))
    for idx, lvl in enumerate(page_lvls):
        if idx == 0:
            lvl.story_height_m = None
        else:
            lvl.story_height_m = round(lvl.elevation_m - page_lvls[idx - 1].elevation_m, 4)


def _derive_level_id(name: str, used_ids: set[str]) -> str:
    upper = name.upper()
    mapping = [
        ("BASEMENT", "B1"),
        ("NATURAL GRADE", "NGL"),
        ("NGL", "NGL"),
        ("GROUND FLOOR", "GF"),
        ("FIRST FLOOR", "1F"),
        ("SECOND FLOOR", "2F"),
        ("THIRD FLOOR", "3F"),
        ("FOURTH FLOOR", "4F"),
        ("FIFTH FLOOR", "5F"),
        ("SIXTH FLOOR", "6F"),
        ("ROOF DECK", "RD"),
        ("ROOF LINE", "ROOF"),
        ("ROOF BEAM", "RB"),
        ("FIREWALL", "FW"),
        ("PARAPET", "PAR"),
    ]
    base = ""
    for kw, code in mapping:
        if kw in upper:
            base = code
            break
    if not base:
        m = re.search(r"LEVEL\s*(\d+)", upper)
        if m:
            base = f"L{m.group(1)}"
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

    # If user switches a sheet to floor_plan and it has no regions yet, auto-propose regions
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

    output = run_detection(
        pdf_path=pdf_path,
        png_path=png_path,
        page_info=page,
        calibration=cal,
        method=req.method,
        options=options,
        region=region,
    )

    if req.replace_unreviewed:
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
        state.column_candidates = [
            c
            for c in state.column_candidates
            if not (
                c.page_number == page_number
                and c.source != "manual"
                and c.status == ReviewStatus.PENDING
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

    state.grid_candidates.extend(output.grids)
    state.column_candidates.extend(output.columns)
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
            "grids_detected": len(output.grids),
            "columns_detected": len(output.columns),
        },
    )

    return ExtractionResponse(
        document=state,
        page_number=page_number,
        method_requested=req.method,
        method_used=output.method_used,
        grids_detected=len(output.grids),
        columns_detected=len(output.columns),
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

    # Replace pending levels on this page while keeping already-approved levels intact
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
        f"Extracted {len(levels)} floor level(s) from page {page_number}."
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
            if req.story_height_m is not None:
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

            if req.elevation_m is not None and req.story_height_m is None:
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
                    "review_status": lvl.review_status.value,
                    "engineer_approved": lvl.engineer_approved,
                },
            )
            return state

    raise KeyError(f"Level '{level_id}' not found.")


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

    # Resolve origins (allow fallback to page calibration origin if it lies within the region)
    src_origin = source_region.origin_px
    if src_origin is None:
        src_cal = state.calibrations.get(str(source_region.page_number))
        if src_cal is not None:
            op = src_cal.origin_px
            if (
                source_region.x_px <= op.x_px <= source_region.x_px + source_region.width_px
                and source_region.y_px <= op.y_px <= source_region.y_px + source_region.height_px
            ):
                src_origin = op
    if src_origin is None:
        raise ValueError(f"Source region '{source_region.name}' requires an origin_px before propagating grids.")

    tgt_origin = target_region.origin_px
    if tgt_origin is None:
        raise ValueError(f"Target region '{target_region.name}' requires an origin_px before receiving the master grid.")

    # Collect approved X & Y grids from source region
    source_grids = [
        g
        for g in state.grid_candidates
        if g.status == ReviewStatus.APPROVED
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

    # Clear prior unreviewed grids/columns in target region so repeated runs stay clean
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
                px_to_plan_m(ncoord, ny1, target_cal)[0]
                if sg.axis == "x"
                else px_to_plan_m(nx1, ncoord, target_cal)[1]
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


def add_manual_column(
    project_id: str,
    doc_id: str,
    req: ManualColumnRequest,
) -> ImportDocumentState:
    state = get_import(project_id, doc_id)
    _get_page(state, req.page_number)
    cal = state.calibrations.get(str(req.page_number))

    existing_ids = {c.column_id for c in state.column_candidates}
    col_id = (req.column_id or "").strip()
    if not col_id:
        idx = 1
        while f"C{idx}" in existing_ids:
            idx += 1
        col_id = f"C{idx}"

    x_m, y_m = (None, None)
    width_px, height_px = 18.0, 18.0
    if cal is not None:
        x_m, y_m = px_to_plan_m(req.x_px, req.y_px, cal)
        width_px = max(8.0, round((req.width_mm / 1000.0) * cal.pixels_per_m, 2))
        height_px = max(8.0, round((req.depth_mm / 1000.0) * cal.pixels_per_m, 2))

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

    if req.axis == "x":
        x1, y1, x2, y2 = req.coord_px, 0.0, req.coord_px, float(page.height_px)
        coord_m = px_to_plan_m(req.coord_px, 0.0, cal)[0] if cal else None
    else:
        x1, y1, x2, y2 = 0.0, req.coord_px, float(page.width_px), req.coord_px
        coord_m = px_to_plan_m(0.0, req.coord_px, cal)[1] if cal else None

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
            updated_count += 1
    for col in state.column_candidates:
        if col.id in target_ids:
            col.status = req.status
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
        if g.status == ReviewStatus.APPROVED
        and (req.page_number is None or g.page_number == req.page_number)
        and (req.region_id is None or g.region_id == req.region_id)
    ]
    approved_cols = [
        c
        for c in state.column_candidates
        if c.status == ReviewStatus.APPROVED
        and (req.page_number is None or c.page_number == req.page_number)
        and (req.region_id is None or c.region_id == req.region_id)
    ]

    if not approved_grids and not approved_cols:
        raise ValueError(
            "No engineer-approved grids or columns are ready to transfer. "
            "Approve at least one candidate before transferring."
        )

    involved_pages = sorted({g.page_number for g in approved_grids} | {c.page_number for c in approved_cols})
    for pnum in involved_pages:
        cal = state.calibrations.get(str(pnum))
        if cal is None or not cal.confirmed:
            raise ValueError(
                f"Page {pnum} scale calibration must be confirmed by an engineer before transferring elements."
            )
        _recalculate_page_coordinates(state, pnum)

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
    else:
        x_map = {g.label: g for g in project.grids.x_grids}
        y_map = {g.label: g for g in project.grids.y_grids}
        col_map = {c.id: c for c in project.columns}

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
        col_map[c.column_id] = Column(
            id=c.column_id,
            grid=c.grid,
            x_m=round(c.x_m, 4),
            y_m=round(c.y_m, 4),
            width_mm=c.width_mm,
            depth_mm=c.depth_mm,
        )

    new_x_grids = sorted(x_map.values(), key=lambda g: g.coord_m)
    new_y_grids = sorted(y_map.values(), key=lambda g: g.coord_m)
    new_columns = list(col_map.values())
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
    updated_project = ProjectState.model_validate(updated_payload)

    storage.save_project(updated_project)

    for g in approved_grids:
        g.transferred = True
    for c in approved_cols:
        c.transferred = True

    record = TransferRecord(
        transferred_at=_utc_now(),
        page_numbers=involved_pages,
        merge_mode=req.merge_mode,
        x_grids_count=x_count,
        y_grids_count=y_count,
        columns_count=len(approved_cols),
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
            "column_ids": [c.column_id for c in approved_cols],
        },
    )

    return TransferResponse(
        project_id=project_id,
        doc_id=doc_id,
        transferred_x_grids=x_count,
        transferred_y_grids=y_count,
        transferred_columns=len(approved_cols),
        total_project_columns=len(updated_project.columns),
        notice=(
            f"Transferred {x_count} X-grid(s), {y_count} Y-grid(s), and "
            f"{len(approved_cols)} column(s) into project '{project_id}'. "
            "Independent structural review remains required."
        ),
        project=updated_project,
        document=state,
    )


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
