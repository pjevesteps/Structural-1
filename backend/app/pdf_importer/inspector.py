from math import hypot
from pathlib import Path
import re
import pymupdf

from .schemas import (
    PageClassification,
    PageInfo,
    PlanRegion,
    ScaleCalibration,
    SheetType,
    UNIT_TO_METERS,
)

MAX_PDF_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
DEFAULT_RENDER_DPI = 150

SHEET_NUM_RE = re.compile(r"^(?:[A-Z]{1,2}-?\d{1,3}[A-Z]?)$")
FLOOR_TITLE_RE = re.compile(
    r"([A-Z0-9][A-Z0-9\s\-/&]{0,36}?(?:FLOOR PLAN|ROOF DECK PLAN))",
    re.IGNORECASE,
)
GENERAL_TITLE_RE = re.compile(
    r"([A-Z0-9][A-Z0-9\s\-/&]{0,42}?(?:"
    r"TABLE OF CONTENTS|VICINITY MAP|SITE DEVELOPMENT PLAN|SITE DEVELOPMENT|LOT PLAN|"
    r"ROOF DECK PLAN|FLOOR PLAN|ROOF PLAN|ELEVATION|CROSS SECTION|LONGITUDINAL SECTION|"
    r"SECTION|SCHEDULE OF DOORS AND WINDOWS|DOORS AND WINDOWS|SCHEDULE))",
    re.IGNORECASE,
)


class InvalidPdfError(ValueError):
    """Raised when an uploaded file fails PDF validation."""


class PdfTooLargeError(ValueError):
    """Raised when an uploaded PDF exceeds the maximum allowed size."""


def validate_pdf_bytes(data: bytes, max_bytes: int = MAX_PDF_SIZE_BYTES) -> None:
    if not data:
        raise InvalidPdfError("Uploaded file is empty.")
    if len(data) > max_bytes:
        raise PdfTooLargeError(
            f"PDF exceeds maximum allowed size of {max_bytes // (1024 * 1024)} MB."
        )
    if not data.startswith(b"%PDF-"):
        raise InvalidPdfError("Invalid PDF file: missing %PDF- header.")


def classify_page(page: pymupdf.Page) -> tuple[PageClassification, int, int, int, float]:
    """Classify a PDF page as 'vector', 'scanned', or 'hybrid' using PyMuPDF metrics."""
    drawings = page.get_drawings()
    drawing_count = len(drawings)

    words = page.get_text("words")
    text_word_count = len(words)

    images = page.get_images(full=True)
    image_count = len(images)

    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    covered_area = 0.0
    seen_xrefs: set[int] = set()
    for img in images:
        xref = img[0]
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        try:
            for rect in page.get_image_rects(xref):
                clipped = rect & page.rect
                if not clipped.is_empty:
                    covered_area += float(clipped.width * clipped.height)
        except Exception:
            continue

    image_coverage = round(min(covered_area / page_area, 1.0), 4)

    has_significant_vectors = drawing_count >= 6
    has_large_raster = image_count > 0 and image_coverage >= 0.35

    if has_significant_vectors and has_large_raster:
        classification: PageClassification = "hybrid"
    elif has_significant_vectors:
        classification = "vector"
    elif has_large_raster:
        if drawing_count >= 2 or text_word_count >= 15:
            classification = "hybrid"
        else:
            classification = "scanned"
    elif drawing_count > 0 or text_word_count > 0:
        classification = "vector"
    else:
        classification = "scanned"

    return classification, drawing_count, image_count, text_word_count, image_coverage


def _extract_text_lines_px(page: pymupdf.Page, width_px: int, height_px: int) -> list[dict]:
    """Extract normalized text lines with bounding boxes in rendered PNG pixel coordinates."""
    ox = float(page.rect.x0)
    oy = float(page.rect.y0)
    sx = width_px / max(float(page.rect.width), 1.0)
    sy = height_px / max(float(page.rect.height), 1.0)

    lines: list[dict] = []
    text_dict = page.get_text("dict")
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            raw_text = " ".join(str(s.get("text", "")).strip() for s in spans if str(s.get("text", "")).strip())
            text = re.sub(r"\s+", " ", raw_text).strip()
            if not text:
                continue
            bx0, by0, bx1, by1 = line.get("bbox", (0, 0, 0, 0))
            max_font = max((float(s.get("size", 0.0)) for s in spans), default=0.0)
            x0 = (float(bx0) - ox) * sx
            y0 = (float(by0) - oy) * sy
            x1 = (float(bx1) - ox) * sx
            y1 = (float(by1) - oy) * sy
            lines.append(
                {
                    "text": text,
                    "upper": text.upper(),
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "cx": (x0 + x1) * 0.5,
                    "cy": (y0 + y1) * 0.5,
                    "font_size": max_font,
                }
            )
    return lines


def _extract_sheet_number(
    lines: list[dict],
    words: list,
    page: pymupdf.Page,
    width_px: int,
    height_px: int,
) -> str:
    """Extract sheet number (e.g., A4, A12, S-1) from the bottom-right title block."""
    ox = float(page.rect.x0)
    oy = float(page.rect.y0)
    sx = width_px / max(float(page.rect.width), 1.0)
    sy = height_px / max(float(page.rect.height), 1.0)

    candidates: list[tuple[float, str]] = []
    for w in words:
        wx0, wy0, wx1, wy1, raw = w[:5]
        token = str(raw).strip().upper().rstrip(".,;:")
        if not SHEET_NUM_RE.match(token):
            continue
        # Exclude common non-sheet tokens like 2F, 3F, C1, B1 if outside title block
        cx = ((float(wx0) + float(wx1)) * 0.5 - ox) * sx
        cy = ((float(wy0) + float(wy1)) * 0.5 - oy) * sy
        if cx >= width_px * 0.70 and cy >= height_px * 0.78:
            # Prefer discipline sheet prefixes (A, S, E, P, M, C, CS, AR, ST) closest to bottom-right
            prefix_bonus = 0.0 if token[0] in ("A", "S", "E", "P", "M", "C") else 0.5
            dist_corner = hypot((width_px - cx) / max(width_px, 1), (height_px - cy) / max(height_px, 1))
            candidates.append((prefix_bonus + dist_corner, token))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    # Fallback: check bottom-right lines for patterns like "SHEET NO: A4"
    for ln in lines:
        if ln["cx"] >= width_px * 0.65 and ln["cy"] >= height_px * 0.78:
            m = re.search(r"\b([A-Z]{1,2}-?\d{1,3}[A-Z]?)\b", ln["upper"])
            if m and m.group(1)[0] in ("A", "S", "E", "P", "M", "C"):
                return m.group(1)
    return ""


def classify_sheet_type_from_text(full_upper_text: str) -> SheetType:
    """
    Classify an architectural PDF sheet strictly from its own embedded text
    (never relying on Page 1 TOC).
    """
    if "TABLE OF CONTENTS" in full_upper_text or "VICINITY MAP" in full_upper_text:
        return "cover_toc"
    if "LOT PLAN" in full_upper_text or "SITE DEVELOPMENT" in full_upper_text:
        return "lot_site"
    if "FLOOR PLAN" in full_upper_text or "ROOF DECK PLAN" in full_upper_text:
        return "floor_plan"
    if "ROOF PLAN" in full_upper_text and "DECK" not in full_upper_text:
        return "roof_plan"
    if "ELEVATION" in full_upper_text:
        return "elevation"
    if (
        "CROSS SECTION" in full_upper_text
        or "LONGITUDINAL SECTION" in full_upper_text
        or "SECTION" in full_upper_text
    ):
        return "section"
    if "SCHEDULE" in full_upper_text or "DOORS AND WINDOWS" in full_upper_text:
        return "schedule_detail"
    return "unknown"


def _extract_sheet_title(lines: list[dict], sheet_type: SheetType) -> str:
    """Extract clean human-readable sheet title from callout/title lines."""
    matched_titles: list[str] = []
    seen: set[str] = set()

    if sheet_type == "floor_plan":
        for ln in sorted(lines, key=lambda x: (x["cx"], x["cy"])):
            for m in FLOOR_TITLE_RE.finditer(ln["upper"]):
                title = re.sub(r"\s+", " ", m.group(1)).strip(" -:/|")
                if title and title not in seen:
                    seen.add(title)
                    matched_titles.append(title)
        if matched_titles:
            return " / ".join(matched_titles[:4])

    for ln in sorted(lines, key=lambda x: (-x["font_size"], x["cy"], x["cx"])):
        for m in GENERAL_TITLE_RE.finditer(ln["upper"]):
            title = re.sub(r"\s+", " ", m.group(1)).strip(" -:/|")
            if title and title not in seen:
                seen.add(title)
                matched_titles.append(title)
    if matched_titles:
        return " / ".join(matched_titles[:3])
    return ""


def _detect_center_viewport_divider_x(
    page: pymupdf.Page,
    width_px: int,
    height_px: int,
) -> float | None:
    """Detect a vertical center viewport divider (42%-58% of width, above bottom 12% title block)."""
    ox = float(page.rect.x0)
    oy = float(page.rect.y0)
    sx = width_px / max(float(page.rect.width), 1.0)
    sy = height_px / max(float(page.rect.height), 1.0)

    usable_h = height_px * 0.88
    min_x = width_px * 0.42
    max_x = width_px * 0.58
    min_div_span = usable_h * 0.35

    vert_segments: list[tuple[float, float, float]] = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                x1, y1 = (float(p1.x) - ox) * sx, (float(p1.y) - oy) * sy
                x2, y2 = (float(p2.x) - ox) * sx, (float(p2.y) - oy) * sy
                if abs(x2 - x1) <= 3.0 and abs(y2 - y1) >= 20.0:
                    xc = (x1 + x2) * 0.5
                    ys, ye = min(y1, y2), min(max(y1, y2), usable_h)
                    if min_x <= xc <= max_x and ye - ys >= 20.0:
                        vert_segments.append((xc, ys, ye))
            elif item[0] == "re":
                r = item[1]
                x0, y0 = (float(r.x0) - ox) * sx, (float(r.y0) - oy) * sy
                x1, y1 = (float(r.x1) - ox) * sx, (float(r.y1) - oy) * sy
                # Check thin vertical bar or either vertical edge of a viewport frame near center
                if abs(x1 - x0) <= 4.0 and abs(y1 - y0) >= 20.0:
                    xc = (x0 + x1) * 0.5
                    ys, ye = min(y0, y1), min(max(y0, y1), usable_h)
                    if min_x <= xc <= max_x and ye - ys >= 20.0:
                        vert_segments.append((xc, ys, ye))

    if not vert_segments:
        return None

    # Cluster nearby center vertical segments
    vert_segments.sort(key=lambda t: t[0])
    clusters: list[list[tuple[float, float, float]]] = [[vert_segments[0]]]
    for seg in vert_segments[1:]:
        mean_x = sum(s[0] for s in clusters[-1]) / len(clusters[-1])
        if abs(seg[0] - mean_x) <= 8.0:
            clusters[-1].append(seg)
        else:
            clusters.append([seg])

    best_x: float | None = None
    best_span = 0.0
    for cl in clusters:
        span = max(s[2] for s in cl) - min(s[1] for s in cl)
        if span >= min_div_span and span > best_span:
            best_span = span
            best_x = sum(s[0] for s in cl) / len(cl)
    return best_x


def _build_floor_plan_regions(
    page: pymupdf.Page,
    page_number: int,
    width_px: int,
    height_px: int,
    lines: list[dict],
    sheet_title: str,
) -> tuple[int, list[PlanRegion]]:
    """Detect single vs dual floor plan viewports and create initial PlanRegion boxes."""
    usable_h = round(height_px * 0.88, 2)
    divider_x = _detect_center_viewport_divider_x(page, width_px, height_px)
    split_x = divider_x if divider_x is not None else (width_px * 0.5)

    left_titles: list[str] = []
    right_titles: list[str] = []
    all_titles: list[tuple[float, str]] = []
    for ln in lines:
        if ln["cy"] > usable_h + height_px * 0.04:
            continue
        for m in FLOOR_TITLE_RE.finditer(ln["upper"]):
            title = re.sub(r"\s+", " ", m.group(1)).strip(" -:/|")
            if not title:
                continue
            all_titles.append((ln["cx"], title))
            if ln["cx"] < split_x:
                if title not in left_titles:
                    left_titles.append(title)
            else:
                if title not in right_titles:
                    right_titles.append(title)

    distinct_titles = list(dict.fromkeys(t for _, t in sorted(all_titles, key=lambda x: x[0])))
    is_dual = (divider_x is not None) or (len(distinct_titles) >= 2) or (bool(left_titles) and bool(right_titles))

    if is_dual:
        sx_round = round(split_x, 2)
        left_name = left_titles[0] if left_titles else (distinct_titles[0] if distinct_titles else f"Page {page_number} Left Floor Plan")
        right_name = (
            right_titles[0]
            if right_titles
            else (distinct_titles[1] if len(distinct_titles) >= 2 else f"Page {page_number} Right Floor Plan")
        )
        regions = [
            PlanRegion(
                region_id=f"reg-p{page_number}-L",
                page_number=page_number,
                name=left_name,
                x_px=0.0,
                y_px=0.0,
                width_px=sx_round,
                height_px=usable_h,
                auto_detected=True,
            ),
            PlanRegion(
                region_id=f"reg-p{page_number}-R",
                page_number=page_number,
                name=right_name,
                x_px=sx_round,
                y_px=0.0,
                width_px=round(float(width_px) - sx_round, 2),
                height_px=usable_h,
                auto_detected=True,
            ),
        ]
        return 2, regions

    single_name = distinct_titles[0] if distinct_titles else (sheet_title or f"Page {page_number} Floor Plan")
    return 1, [
        PlanRegion(
            region_id=f"reg-p{page_number}-1",
            page_number=page_number,
            name=single_name,
            x_px=0.0,
            y_px=0.0,
            width_px=float(width_px),
            height_px=usable_h,
            auto_detected=True,
        )
    ]


def inspect_and_render_pdf(
    pdf_path: Path,
    pages_dir: Path,
    dpi: int = DEFAULT_RENDER_DPI,
) -> tuple[list[PageInfo], list[PlanRegion]]:
    """
    Inspect all pages of an immutable PDF, render PNG previews into pages_dir,
    classify each sheet strictly from its own text, and auto-create PlanRegions for floor plans.
    """
    pages_dir.mkdir(parents=True, exist_ok=True)
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise InvalidPdfError(f"Unable to open PDF document: {exc}") from exc

    try:
        if len(doc) == 0:
            raise InvalidPdfError("PDF document contains no pages.")

        pages: list[PageInfo] = []
        regions: list[PlanRegion] = []
        for idx, page in enumerate(doc):
            page_number = idx + 1
            classification, drawing_count, image_count, text_word_count, image_coverage = (
                classify_page(page)
            )
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            image_filename = f"page_{page_number:03d}.png"
            pix.save(str(pages_dir / image_filename))

            width_px = int(pix.width)
            height_px = int(pix.height)
            lines = _extract_text_lines_px(page, width_px, height_px)
            words = page.get_text("words")
            full_upper = " ".join(ln["upper"] for ln in lines)

            sheet_number = _extract_sheet_number(lines, words, page, width_px, height_px)
            sheet_type = classify_sheet_type_from_text(full_upper)
            sheet_title = _extract_sheet_title(lines, sheet_type)

            viewport_count = 1
            if sheet_type == "floor_plan":
                viewport_count, page_regions = _build_floor_plan_regions(
                    page,
                    page_number,
                    width_px,
                    height_px,
                    lines,
                    sheet_title,
                )
                regions.extend(page_regions)

            pages.append(
                PageInfo(
                    page_number=page_number,
                    width_pt=round(float(page.rect.width), 3),
                    height_pt=round(float(page.rect.height), 3),
                    width_px=width_px,
                    height_px=height_px,
                    dpi=dpi,
                    classification=classification,
                    drawing_count=drawing_count,
                    image_count=image_count,
                    text_word_count=text_word_count,
                    image_coverage=image_coverage,
                    image_filename=image_filename,
                    sheet_number=sheet_number,
                    sheet_title=sheet_title,
                    sheet_type=sheet_type,
                    viewport_count=viewport_count,
                )
            )
        return pages, regions
    finally:
        doc.close()


def compute_scale_metrics(
    x1_px: float,
    y1_px: float,
    x2_px: float,
    y2_px: float,
    known_distance: float,
    unit: str,
) -> tuple[float, float, float, float]:
    """Return (pixel_distance, real_distance_m, meters_per_px, pixels_per_m)."""
    if unit not in UNIT_TO_METERS:
        raise ValueError(f"Unsupported calibration unit: {unit}")
    pixel_distance = hypot(x2_px - x1_px, y2_px - y1_px)
    if pixel_distance <= 1e-6:
        raise ValueError("Calibration points must be distinct (non-zero pixel distance).")
    if known_distance <= 0:
        raise ValueError("Known distance must be positive.")

    real_distance_m = known_distance * UNIT_TO_METERS[unit]
    meters_per_px = real_distance_m / pixel_distance
    pixels_per_m = pixel_distance / real_distance_m
    return pixel_distance, real_distance_m, meters_per_px, pixels_per_m


def px_to_plan_m(
    x_px: float,
    y_px: float,
    calibration: ScaleCalibration,
) -> tuple[float, float]:
    """
    Convert image pixel coordinates (origin top-left, +Y down) into plan meters
    (origin at calibration.origin_px, +X right, +Y up) compatible with
    backend.app.geometry.plan_to_staad(x_m, y_plan_m, elevation_m).
    """
    x_m = round((x_px - calibration.origin_px.x_px) * calibration.meters_per_px, 4)
    y_m = round((calibration.origin_px.y_px - y_px) * calibration.meters_per_px, 4)
    return x_m, y_m
