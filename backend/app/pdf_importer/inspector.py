from math import hypot
from pathlib import Path
import re

import cv2
import numpy as np
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

# Words to split apart after collapsing single-character AutoCAD spacing (e.g. "FRONTELEVATION" -> "FRONT ELEVATION")
COMPOUND_KEYWORDS = (
    "TABLEOFCONTENTS",
    "VICINITYMAP",
    "EXTERIORPERSPECTIVE",
    "SITEDEVELOPMENTPLAN",
    "SITEDEVELOPMENT",
    "LOTPLAN",
    "BASEMENTFLOORPLAN",
    "GROUNDFLOORPLAN",
    "FIRSTFLOORPLAN",
    "SECONDFLOORPLAN",
    "THIRDFLOORPLAN",
    "FOURTHFLOORPLAN",
    "FIFTHFLOORPLAN",
    "SIXTHFLOORPLAN",
    "MEZZANINEFLOORPLAN",
    "TYPICALFLOORPLAN",
    "STRUCTURALFLOORPLAN",
    "STRUCTURALPLAN",
    "FOUNDATIONPLAN",
    "FRAMINGPLAN",
    "BEAMPLAN",
    "SLABPLAN",
    "ROOFDECKPLAN",
    "FLOORPLAN",
    "ROOFPLAN",
    "FRONTELEVATION",
    "REARELEVATION",
    "LEFTELEVATION",
    "RIGHTELEVATION",
    "SIDEELEVATION",
    "ELEVATION",
    "CROSSSECTION",
    "LONGITUDINALSECTION",
    "TRANSVERSECTION",
    "SECTION",
    "DOORSANDWINDOWSSCHEDULE",
    "DOORSANDWINDOWS",
    "SCHEDULE",
)

COMPOUND_REPLACEMENTS = {
    "TABLEOFCONTENTS": "TABLE OF CONTENTS",
    "VICINITYMAP": "VICINITY MAP",
    "EXTERIORPERSPECTIVE": "EXTERIOR PERSPECTIVE",
    "SITEDEVELOPMENTPLAN": "SITE DEVELOPMENT PLAN",
    "SITEDEVELOPMENT": "SITE DEVELOPMENT",
    "LOTPLAN": "LOT PLAN",
    "BASEMENTFLOORPLAN": "BASEMENT FLOOR PLAN",
    "GROUNDFLOORPLAN": "GROUND FLOOR PLAN",
    "FIRSTFLOORPLAN": "FIRST FLOOR PLAN",
    "SECONDFLOORPLAN": "SECOND FLOOR PLAN",
    "THIRDFLOORPLAN": "THIRD FLOOR PLAN",
    "FOURTHFLOORPLAN": "FOURTH FLOOR PLAN",
    "FIFTHFLOORPLAN": "FIFTH FLOOR PLAN",
    "SIXTHFLOORPLAN": "SIXTH FLOOR PLAN",
    "MEZZANINEFLOORPLAN": "MEZZANINE FLOOR PLAN",
    "TYPICALFLOORPLAN": "TYPICAL FLOOR PLAN",
    "STRUCTURALFLOORPLAN": "STRUCTURAL FLOOR PLAN",
    "STRUCTURALPLAN": "STRUCTURAL PLAN",
    "FOUNDATIONPLAN": "FOUNDATION PLAN",
    "FRAMINGPLAN": "FRAMING PLAN",
    "BEAMPLAN": "BEAM PLAN",
    "SLABPLAN": "SLAB PLAN",
    "ROOFDECKPLAN": "ROOF DECK PLAN",
    "FLOORPLAN": "FLOOR PLAN",
    "ROOFPLAN": "ROOF PLAN",
    "FRONTELEVATION": "FRONT ELEVATION",
    "REARELEVATION": "REAR ELEVATION",
    "LEFTELEVATION": "LEFT ELEVATION",
    "RIGHTELEVATION": "RIGHT ELEVATION",
    "SIDEELEVATION": "SIDE ELEVATION",
    "ELEVATION": "ELEVATION",
    "CROSSSECTION": "CROSS SECTION",
    "LONGITUDINALSECTION": "LONGITUDINAL SECTION",
    "TRANSVERSECTION": "TRANSVERSE SECTION",
    "SECTION": "SECTION",
    "DOORSANDWINDOWSSCHEDULE": "DOORS AND WINDOWS SCHEDULE",
    "DOORSANDWINDOWS": "DOORS AND WINDOWS",
    "SCHEDULE": "SCHEDULE",
}

FLOOR_TITLE_RE = re.compile(
    r"\b((?:BASEMENT|GROUND|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|MEZZANINE|TYPICAL|UPPER|LOWER|ROOF\s+DECK)"
    r"(?:\s+[A-Z0-9\-]{1,12}){0,2}\s*(?:FLOOR\s+PLAN|PLAN)|[A-Z0-9\-]{1,12}\s+FLOOR\s+PLAN|ROOF\s+DECK\s+PLAN|ROOF\s+PLAN)\b",
    re.IGNORECASE,
)

# Cross-reference phrases inside callouts/notes that should never be treated as standalone plan titles
_CROSS_REF_PHRASES = (
    "SEE ",
    "REFER TO",
    "UP TO ",
    "DOWN TO ",
    "OPEN TO BELOW",
    "OPEN BELOW",
    "ABOVE",
    "BELOW",
)

# Canonical level matching rules (ordered specifically: ROOF DECK before ROOF)
_CANONICAL_LEVEL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ROOF DECK", ("RD", "ROOF DECK", "ROOFDECK")),
    ("ROOF BEAM", ("RB", "ROOF BEAM", "ROOF")),
    ("ROOF LINE", ("ROOF", "RL", "RB")),
    ("ROOF PLAN", ("ROOF", "RB", "RL")),
    ("ROOF", ("ROOF", "RB", "RL")),
    ("PENTHOUSE", ("PH", "PENTHOUSE")),
    ("MEZZANINE", ("MEZZ", "MZ", "MEZZANINE")),
    ("BASEMENT", ("B1", "BASEMENT", "BSMT")),
    ("GROUND", ("GF", "G/F", "GROUND", "1F")),
    ("FIRST", ("1F", "GF")),
    ("1ST", ("1F", "GF")),
    ("SECOND", ("2F", "2ND")),
    ("2ND", ("2F", "2ND")),
    ("THIRD", ("3F", "3RD")),
    ("3RD", ("3F", "3RD")),
    ("FOURTH", ("4F", "4TH")),
    ("4TH", ("4F", "4TH")),
    ("FIFTH", ("5F", "5TH")),
    ("5TH", ("5F", "5TH")),
    ("SIXTH", ("6F", "6TH")),
    ("6TH", ("6F", "6TH")),
)

GENERAL_TITLE_RE = re.compile(
    r"\b("
    r"TABLE OF CONTENTS|VICINITY MAP|EXTERIOR PERSPECTIVE|"
    r"SITE DEVELOPMENT PLAN|SITE DEVELOPMENT|LOT PLAN|"
    r"(?:BASEMENT|GROUND|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|MEZZANINE|TYPICAL)?\s*FLOOR PLAN|"
    r"ROOF DECK PLAN|ROOF PLAN|"
    r"(?:FRONT|REAR|LEFT|RIGHT|SIDE)?\s*ELEVATION|"
    r"CROSS SECTION|LONGITUDINAL SECTION|SECTION|"
    r"DOORS AND WINDOWS SCHEDULE|SCHEDULE OF DOORS AND WINDOWS|DOORS AND WINDOWS|SCHEDULE"
    r")\b",
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


def normalize_spaced_text(text: str) -> str:
    """
    Collapse AutoCAD spaced-out single-character runs such as:
      'F L O O R  P L A N' -> 'FLOOR PLAN'
      'L\\nO\\nT\\n \\nP\\nL\\nA\\nN' -> 'LOT PLAN'
      'B A S E M E N T F L O O R P L A N' -> 'BASEMENT FLOOR PLAN'
      'C\\nR\\nO\\nS\\nS\\n \\nS\\nE\\nC\\nT\\nI\\nO\\nN' -> 'CROSS SECTION'
    """
    if not text:
        return ""

    # Preserve explicit space-only lines (word breaks between vertically split letters)
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n[ \t]+\n", "  ", s)
    # Collapse runs of 3+ single letters separated by single space or single newline
    s = re.sub(
        r"(?<![A-Za-z])([A-Za-z])(?:[ \t\n]([A-Za-z])){2,}(?![A-Za-z])",
        lambda m: re.sub(r"\s+", "", m.group(0)),
        s,
    )
    # Normalize remaining whitespace
    s = re.sub(r"\s+", " ", s).strip().upper()

    # Re-insert clean word boundaries for merged AutoCAD architectural titles
    for compact in COMPOUND_KEYWORDS:
        if compact in s:
            s = s.replace(compact, f" {COMPOUND_REPLACEMENTS[compact]} ")

    return re.sub(r"\s+", " ", s).strip()


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
    rot_mat = page.rotation_matrix
    for img in images:
        xref = img[0]
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        try:
            for rect in page.get_image_rects(xref):
                disp_rect = rect * rot_mat
                clipped = disp_rect & page.rect
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


def _to_display_rect_px(
    bbox: tuple[float, float, float, float],
    page: pymupdf.Page,
    width_px: int,
    height_px: int,
) -> tuple[float, float, float, float]:
    """Transform an unrotated PyMuPDF bbox into rendered PNG display pixel coordinates."""
    r = pymupdf.Rect(bbox) * page.rotation_matrix
    ox = float(page.rect.x0)
    oy = float(page.rect.y0)
    sx = width_px / max(float(page.rect.width), 1.0)
    sy = height_px / max(float(page.rect.height), 1.0)
    x0 = (float(r.x0) - ox) * sx
    y0 = (float(r.y0) - oy) * sy
    x1 = (float(r.x1) - ox) * sx
    y1 = (float(r.y1) - oy) * sy
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _extract_text_blocks_px(page: pymupdf.Page, width_px: int, height_px: int) -> list[dict]:
    """
    Extract text blocks & lines in rendered PNG display pixel coordinates (respecting page rotation),
    with spaced-letter normalization applied to both block and line text.
    """
    items: list[dict] = []
    text_dict = page.get_text("dict")
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_lines: list[str] = []
        max_font = 0.0
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            raw_line = " ".join(str(s.get("text", "")).strip() for s in spans if str(s.get("text", "")).strip())
            if raw_line:
                block_lines.append(raw_line)
            max_font = max(max_font, max((float(s.get("size", 0.0)) for s in spans), default=0.0))

        raw_block = "\n".join(block_lines).strip()
        if not raw_block:
            continue
        norm = normalize_spaced_text(raw_block)
        if not norm:
            continue
        x0, y0, x1, y1 = _to_display_rect_px(block.get("bbox", (0, 0, 0, 0)), page, width_px, height_px)
        items.append(
            {
                "raw": raw_block,
                "norm": norm,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "cx": (x0 + x1) * 0.5,
                "cy": (y0 + y1) * 0.5,
                "font_size": max_font,
            }
        )
    return items


def _extract_sheet_number(
    words: list,
    page: pymupdf.Page,
    width_px: int,
    height_px: int,
) -> str:
    """
    Strictly extract sheet_number from the bottom-right title block corner ONLY:
    x >= 80% of page width and y >= 84% of page height (in display pixel coords).
    Never picks up section-cut callouts (e.g. 'A12') inside the drawing area.
    """
    min_x = width_px * 0.80
    min_y = height_px * 0.84

    candidates: list[tuple[float, str]] = []
    for w in words:
        raw = str(w[4]).strip().upper().rstrip(".,;:")
        if not SHEET_NUM_RE.match(raw):
            continue
        x0, y0, x1, y1 = _to_display_rect_px(w[:4], page, width_px, height_px)
        cx = (x0 + x1) * 0.5
        cy = (y0 + y1) * 0.5
        if cx >= min_x and cy >= min_y:
            # Prefer discipline-prefixed sheet codes (A1..A14, S1..S10, etc.)
            if raw[0] in ("A", "S", "E", "P", "M", "C"):
                dist_corner = hypot((width_px - cx) / max(width_px, 1), (height_px - cy) / max(height_px, 1))
                candidates.append((dist_corner, raw))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]
    return ""


def classify_sheet_type_from_text(norm_upper_text: str) -> SheetType:
    """
    Classify an architectural PDF sheet strictly from its own normalized embedded text
    (never relying on Page 1 TOC), with smart fallback clues when main titles use SHX vector strokes.
    """
    t = norm_upper_text
    if "TABLE OF CONTENTS" in t or "VICINITY MAP" in t or "VICINITY" in t or "EXTERIOR PERSPECTIVE" in t:
        return "cover_toc"
    if "LOT PLAN" in t or "SITE DEVELOPMENT" in t:
        return "lot_site"
    if (
        "STRUCTURAL PLAN" in t
        or "STRUCTURAL FLOOR PLAN" in t
        or "FRAMING PLAN" in t
        or "FOUNDATION PLAN" in t
        or "BEAM PLAN" in t
        or "SLAB PLAN" in t
    ):
        return "structural_plan"
    if "FLOOR PLAN" in t or "ROOF DECK PLAN" in t:
        return "floor_plan"
    if "ROOF PLAN" in t and "ROOF DECK PLAN" not in t:
        return "roof_plan"
    if "CROSS SECTION" in t or "LONGITUDINAL SECTION" in t or "TRANSVERSE SECTION" in t:
        return "section"
    if "ELEVATION" in t:
        return "elevation"
    if re.search(r"\bSECTION\b", t):
        return "section"
    if "DOORS AND WINDOWS" in t or "SCHEDULE" in t or ("NO. OF SETS" in t and "DOOR TYPE" in t):
        return "schedule_detail"

    # Smart fallback clues when main sheet title uses AutoCAD SHX vector strokes
    if ("NATURAL GRADE" in t or "BUILDING HEIGHT" in t) and ("FINISHED FLOOR" in t or "LEVEL" in t):
        return "elevation"
    if ("BEARING" in t or "TIE POINT" in t or re.search(r"[NS]\s*\d+[°º]\s*\d+", t)) and "LOT" in t:
        return "lot_site"
    if "NO. OF SETS" in t or "DOOR TYPE" in t or "WINDOW TYPE" in t:
        return "schedule_detail"
    return "unknown"


def _extract_sheet_title(blocks: list[dict], sheet_type: SheetType, full_norm: str) -> str:
    """Extract clean human-readable sheet title from normalized callout/title text."""
    matched_titles: list[str] = []
    seen: set[str] = set()

    if sheet_type in ("floor_plan", "structural_plan"):
        for blk in sorted(blocks, key=lambda x: (x["cx"], x["cy"])):
            for m in FLOOR_TITLE_RE.finditer(blk["norm"]):
                title = re.sub(r"\s+", " ", m.group(1)).strip(" -:/|")
                if title and title not in seen:
                    seen.add(title)
                    matched_titles.append(title)
        if not matched_titles:
            for m in FLOOR_TITLE_RE.finditer(full_norm):
                title = re.sub(r"\s+", " ", m.group(1)).strip(" -:/|")
                if title and title not in seen:
                    seen.add(title)
                    matched_titles.append(title)
        if matched_titles:
            return " / ".join(matched_titles[:4])

    for blk in sorted(blocks, key=lambda x: (-x["font_size"], x["cy"], x["cx"])):
        for m in GENERAL_TITLE_RE.finditer(blk["norm"]):
            title = re.sub(r"\s+", " ", m.group(1)).strip(" -:/|")
            if title and title not in seen:
                seen.add(title)
                matched_titles.append(title)
    if not matched_titles:
        for m in GENERAL_TITLE_RE.finditer(full_norm):
            title = re.sub(r"\s+", " ", m.group(1)).strip(" -:/|")
            if title and title not in seen:
                seen.add(title)
                matched_titles.append(title)
    if matched_titles:
        return " / ".join(matched_titles[:3])
    return ""


def _detect_center_viewport_divider_x_vector(
    page: pymupdf.Page,
    width_px: int,
    height_px: int,
) -> float | None:
    """
    Search strictly between 46% and 56% of page width for a true vertical frame divider
    spanning >= 80% of the active drawing height (above bottom 12% title block).
    """
    rot_mat = page.rotation_matrix
    ox = float(page.rect.x0)
    oy = float(page.rect.y0)
    sx = width_px / max(float(page.rect.width), 1.0)
    sy = height_px / max(float(page.rect.height), 1.0)

    top_y = height_px * 0.03
    usable_h = height_px * 0.88
    min_x = width_px * 0.46
    max_x = width_px * 0.56
    min_div_span = (usable_h - top_y) * 0.80

    vert_segments: list[tuple[float, float, float]] = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if item[0] == "l":
                p1 = item[1] * rot_mat
                p2 = item[2] * rot_mat
                x1, y1 = (float(p1.x) - ox) * sx, (float(p1.y) - oy) * sy
                x2, y2 = (float(p2.x) - ox) * sx, (float(p2.y) - oy) * sy
                if abs(x2 - x1) <= 3.0 and abs(y2 - y1) >= 20.0:
                    xc = (x1 + x2) * 0.5
                    ys, ye = max(min(y1, y2), top_y), min(max(y1, y2), usable_h)
                    if min_x <= xc <= max_x and ye - ys >= 20.0:
                        vert_segments.append((xc, ys, ye))
            elif item[0] == "re":
                r = item[1] * rot_mat
                x0, y0 = (float(r.x0) - ox) * sx, (float(r.y0) - oy) * sy
                x1, y1 = (float(r.x1) - ox) * sx, (float(r.y1) - oy) * sy
                if abs(x1 - x0) <= 4.0 and abs(y1 - y0) >= 20.0:
                    xc = (x0 + x1) * 0.5
                    ys, ye = max(min(y0, y1), top_y), min(max(y0, y1), usable_h)
                    if min_x <= xc <= max_x and ye - ys >= 20.0:
                        vert_segments.append((xc, ys, ye))

    if not vert_segments:
        return None

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


def _detect_center_viewport_divider_x_raster(png_path: Path) -> float | None:
    """
    Scan rendered grayscale PNG columns strictly between 46% and 56% of image width
    (above bottom 12% title block and below top 3% border).
    Require a vertical frame divider spanning >= 80% of the active drawing height so
    interior walls or Grid E lines that stop short of the outer frame never cause a split.
    """
    if not png_path.exists():
        return None
    img = cv2.imread(str(png_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    ih, iw = img.shape[:2]
    y0 = int(ih * 0.03)
    y1 = int(ih * 0.88)
    x0 = int(iw * 0.46)
    x1 = int(iw * 0.56)
    if y1 <= y0 or x1 <= x0:
        return None

    dark = img[y0:y1, x0:x1] < 200
    if dark.shape[1] >= 3:
        band_dark = dark.copy()
        band_dark[:, 1:] |= dark[:, :-1]
        band_dark[:, :-1] |= dark[:, 1:]
    else:
        band_dark = dark

    col_coverage = band_dark.mean(axis=0)
    best_idx = int(np.argmax(col_coverage))
    if float(col_coverage[best_idx]) >= 0.80:
        return float(x0 + best_idx)
    return None


def _is_cross_ref_or_note_block(norm_text: str) -> bool:
    """Return True if a normalized text block is a cross-reference callout or long note paragraph."""
    if not norm_text:
        return True
    if any(phrase in norm_text for phrase in _CROSS_REF_PHRASES):
        return True
    if len(norm_text) > 90 or norm_text.count(".") >= 2:
        return True
    return False


def match_title_to_project_level(title: str, project_levels: list | None) -> str:
    """
    Deterministically map a region name or sheet title (e.g. 'GROUND FLOOR PLAN', '2ND FLOOR PLAN',
    'ROOF DECK PLAN', 'ROOF PLAN') to a matching level.id in project_levels.
    Returns '' when no confident match exists.
    """
    if not title or not project_levels:
        return ""
    norm = normalize_spaced_text(title)
    if not norm:
        return ""

    upper_to_id: dict[str, str] = {}
    for lv in project_levels:
        lid = lv.id if hasattr(lv, "id") else (lv.get("id", "") if isinstance(lv, dict) else str(lv))
        lid = str(lid).strip()
        if lid:
            upper_to_id[lid.upper()] = lid
    if not upper_to_id:
        return ""

    # 1. Canonical keyword rules (ROOF DECK is checked strictly before ROOF and stops fallthrough)
    for kw, candidates in _CANONICAL_LEVEL_RULES:
        if kw in norm:
            for cand in candidates:
                if cand in upper_to_id:
                    return upper_to_id[cand]
            # Avoid falling through from ROOF DECK to generic ROOF
            if kw == "ROOF DECK":
                return ""

    # 2. Numeric floor / basement patterns (e.g., "7TH FLOOR PLAN", "LEVEL 7", "B2 PLAN")
    m_fl = re.search(r"\b(\d+)(?:ST|ND|RD|TH)?\s*(?:FLOOR|FLR|LEVEL|LVL)\b", norm)
    if m_fl:
        cand = f"{m_fl.group(1)}F"
        if cand in upper_to_id:
            return upper_to_id[cand]
    m_bsmt = re.search(r"\b(?:BASEMENT\s*(\d+)|B(\d+))\b", norm)
    if m_bsmt:
        num = m_bsmt.group(1) or m_bsmt.group(2)
        cand = f"B{num}"
        if cand in upper_to_id:
            return upper_to_id[cand]

    # 3. Whole-word exact ID match (e.g. "GF", "2F", "RD", "ROOF", "MEZZ")
    for lid_upper, lid in sorted(upper_to_id.items(), key=lambda kv: -len(kv[0])):
        if len(lid_upper) >= 2 and re.search(rf"\b{re.escape(lid_upper)}\b", norm):
            return lid

    return ""


def auto_assign_region_floor_levels(
    pages: list[PageInfo],
    regions: list[PlanRegion],
    project_levels: list | None,
) -> int:
    """
    Auto-populate PlanRegion.floor_level from region name (or parent page sheet_title for single-region pages)
    against project_levels without ever overwriting a non-empty user-assigned floor_level.
    """
    if not project_levels or not regions:
        return 0

    page_by_num = {p.page_number: p for p in pages}
    plan_count_by_page: dict[int, int] = {}
    for r in regions:
        if r.region_type == "plan":
            plan_count_by_page[r.page_number] = plan_count_by_page.get(r.page_number, 0) + 1

    mapped = 0
    for reg in regions:
        if reg.region_type != "plan" or reg.floor_level.strip():
            continue
        matched = match_title_to_project_level(reg.name, project_levels)
        if not matched and plan_count_by_page.get(reg.page_number, 0) == 1:
            pg = page_by_num.get(reg.page_number)
            if pg is not None and pg.sheet_title:
                matched = match_title_to_project_level(pg.sheet_title, project_levels)
        if matched:
            reg.floor_level = matched
            mapped += 1
    return mapped


def build_floor_plan_regions(
    page: pymupdf.Page,
    png_path: Path,
    page_number: int,
    width_px: int,
    height_px: int,
    blocks: list[dict],
    sheet_title: str,
) -> tuple[int, list[PlanRegion]]:
    """Detect single vs dual floor plan viewports (pixel + vector divider + titles) and create PlanRegions."""
    margin_x = round(width_px * 0.025, 2)
    margin_y = round(height_px * 0.03, 2)
    usable_bottom = round(height_px * 0.88, 2)
    usable_h = round(usable_bottom - margin_y, 2)

    divider_x = _detect_center_viewport_divider_x_raster(png_path)
    if divider_x is None:
        divider_x = _detect_center_viewport_divider_x_vector(page, width_px, height_px)
    split_x = divider_x if divider_x is not None else (width_px * 0.5)

    # Collect candidate plan titles, filtering out cross-reference callouts & long note paragraphs
    raw_matches: list[dict] = []
    for blk in blocks:
        if blk["cy"] > usable_bottom + height_px * 0.04:
            continue
        if _is_cross_ref_or_note_block(blk["norm"]):
            continue
        norm_txt = blk["norm"]
        norm_len = max(len(norm_txt), 1)
        bx0 = float(blk.get("x0", blk["cx"]))
        bx1 = float(blk.get("x1", blk["cx"]))
        bw = max(bx1 - bx0, 0.0)
        for m in FLOOR_TITLE_RE.finditer(norm_txt):
            title = re.sub(r"\s+", " ", m.group(1)).strip(" -:/|")
            if not title:
                continue
            mx0 = bx0 + (m.start() / norm_len) * bw
            mx1 = bx0 + (m.end() / norm_len) * bw
            mcx = (mx0 + mx1) * 0.5
            raw_matches.append(
                {
                    "title": title,
                    "cx": mcx,
                    "x0": mx0,
                    "x1": mx1,
                    "font_size": float(blk.get("font_size", 0.0) or 0.0),
                }
            )

    # Prefer prominent/title-sized headings over small annotation text when font_size is available
    max_font = max((item["font_size"] for item in raw_matches), default=0.0)
    if max_font > 0.0:
        prominent_matches = [item for item in raw_matches if item["font_size"] >= max_font * 0.72]
    else:
        prominent_matches = raw_matches

    distinct_titles = list(
        dict.fromkeys(item["title"] for item in sorted(prominent_matches, key=lambda x: x["cx"]))
    )

    # Suppress a false center divider if a single prominent plan title spans across divider_x
    if divider_x is not None and len(distinct_titles) == 1:
        if any(item["x0"] < divider_x < item["x1"] for item in prominent_matches):
            divider_x = None
            split_x = width_px * 0.5

    left_titles: list[str] = []
    right_titles: list[str] = []
    for item in sorted(prominent_matches, key=lambda x: x["cx"]):
        title = item["title"]
        if item["cx"] < split_x:
            if title not in left_titles:
                left_titles.append(title)
        else:
            if title not in right_titles:
                right_titles.append(title)

    has_opposite_distinct = bool(left_titles) and bool(right_titles) and (set(left_titles) != set(right_titles))
    is_dual = (divider_x is not None) or has_opposite_distinct

    if is_dual:
        sx_round = round(split_x, 2)
        left_name = (
            left_titles[0]
            if left_titles
            else (distinct_titles[0] if distinct_titles else f"Page {page_number} Left Plan")
        )
        right_name = (
            right_titles[0]
            if right_titles
            else (distinct_titles[1] if len(distinct_titles) >= 2 else f"Page {page_number} Right Plan")
        )
        left_w = max(20.0, round(sx_round - margin_x - 4.0, 2))
        right_x = round(sx_round + 4.0, 2)
        right_w = max(20.0, round((float(width_px) - margin_x) - right_x, 2))
        regions = [
            PlanRegion(
                region_id=f"reg-p{page_number}-L",
                page_number=page_number,
                name=left_name,
                x_px=margin_x,
                y_px=margin_y,
                width_px=left_w,
                height_px=usable_h,
                auto_detected=True,
            ),
            PlanRegion(
                region_id=f"reg-p{page_number}-R",
                page_number=page_number,
                name=right_name,
                x_px=right_x,
                y_px=margin_y,
                width_px=right_w,
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
            x_px=margin_x,
            y_px=margin_y,
            width_px=max(20.0, round(float(width_px) - 2.0 * margin_x, 2)),
            height_px=usable_h,
            auto_detected=True,
        )
    ]


def inspect_and_render_pdf(
    pdf_path: Path,
    pages_dir: Path,
    dpi: int = DEFAULT_RENDER_DPI,
    start_page_index: int = 0,
    project_levels: list | None = None,
) -> tuple[list[PageInfo], list[PlanRegion]]:
    """
    Inspect pages of a PDF starting at 0-based start_page_index, render PNG previews into pages_dir,
    classify each sheet strictly from its own normalized text, and auto-create PlanRegions for floor plans.
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
        start_idx = max(0, int(start_page_index))
        for idx in range(start_idx, len(doc)):
            page = doc[idx]
            page_number = idx + 1
            classification, drawing_count, image_count, text_word_count, image_coverage = (
                classify_page(page)
            )
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            image_filename = f"page_{page_number:03d}.png"
            png_path = pages_dir / image_filename
            pix.save(str(png_path))

            width_px = int(pix.width)
            height_px = int(pix.height)
            blocks = _extract_text_blocks_px(page, width_px, height_px)
            words = page.get_text("words")
            full_norm = normalize_spaced_text(page.get_text("text"))

            sheet_number = _extract_sheet_number(words, page, width_px, height_px)
            sheet_type = classify_sheet_type_from_text(full_norm)
            sheet_title = _extract_sheet_title(blocks, sheet_type, full_norm)

            viewport_count = 1
            if sheet_type in ("floor_plan", "structural_plan", "roof_plan"):
                viewport_count, page_regions = build_floor_plan_regions(
                    page,
                    png_path,
                    page_number,
                    width_px,
                    height_px,
                    blocks,
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
        if project_levels:
            auto_assign_region_floor_levels(pages, regions, project_levels)
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
