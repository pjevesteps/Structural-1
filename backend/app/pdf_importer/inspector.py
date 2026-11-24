from math import hypot
from pathlib import Path
import pymupdf

from .schemas import (
    PageClassification,
    PageInfo,
    ScaleCalibration,
    UNIT_TO_METERS,
)

MAX_PDF_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
DEFAULT_RENDER_DPI = 150


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


def inspect_and_render_pdf(
    pdf_path: Path,
    pages_dir: Path,
    dpi: int = DEFAULT_RENDER_DPI,
) -> list[PageInfo]:
    """Inspect all pages of an immutable PDF and render PNG previews into pages_dir."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise InvalidPdfError(f"Unable to open PDF document: {exc}") from exc

    try:
        if len(doc) == 0:
            raise InvalidPdfError("PDF document contains no pages.")

        pages: list[PageInfo] = []
        for idx, page in enumerate(doc):
            page_number = idx + 1
            classification, drawing_count, image_count, text_word_count, image_coverage = (
                classify_page(page)
            )
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            image_filename = f"page_{page_number:03d}.png"
            pix.save(str(pages_dir / image_filename))

            pages.append(
                PageInfo(
                    page_number=page_number,
                    width_pt=round(float(page.rect.width), 3),
                    height_pt=round(float(page.rect.height), 3),
                    width_px=int(pix.width),
                    height_px=int(pix.height),
                    dpi=dpi,
                    classification=classification,
                    drawing_count=drawing_count,
                    image_count=image_count,
                    text_word_count=text_word_count,
                    image_coverage=image_coverage,
                    image_filename=image_filename,
                )
            )
        return pages
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
