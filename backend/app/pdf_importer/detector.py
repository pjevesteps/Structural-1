from dataclasses import dataclass, field
import math
from math import hypot
from pathlib import Path
from uuid import uuid4
import re

import cv2
import numpy as np
import pymupdf

from .inspector import px_to_plan_m
from .schemas import (
    BeamCandidate,
    ColumnCandidate,
    DetectedLevel,
    DetectedOpening,
    DetectionMethod,
    ExtractionOptions,
    GridCandidate,
    PageInfo,
    PlanRegion,
    PointPx,
    ReviewStatus,
    ScaleCalibration,
)

LABEL_PATTERN = re.compile(r"^[A-Za-z0-9]{1,3}'?$")

NON_FLOOR_DATUM_EXCLUDE = (
    "CEILING",
    "FIREWALL",
    "PARAPET",
    "NATURAL GRADE",
    "NGL",
    "GRADE LINE",
    "ROAD",
    "SIDEWALK",
    "SILL",
    "LINTEL",
    "COPING",
    "RIDGE",
    "BUILDING HEIGHT",
    "SCALE",
)

STRUCTURAL_FLOOR_KEYWORDS = (
    "BASEMENT",
    "GROUND FLOOR LINE",
    "GROUND FLOOR",
    "FIRST FLOOR LINE",
    "FIRST FLOOR",
    "1ST FLOOR LINE",
    "1ST FLOOR",
    "SECOND FLOOR LINE",
    "SECOND FLOOR",
    "2ND FLOOR LINE",
    "2ND FLOOR",
    "THIRD FLOOR LINE",
    "THIRD FLOOR",
    "3RD FLOOR LINE",
    "3RD FLOOR",
    "FOURTH FLOOR",
    "4TH FLOOR",
    "FIFTH FLOOR",
    "5TH FLOOR",
    "SIXTH FLOOR",
    "6TH FLOOR",
    "MEZZANINE",
    "PENTHOUSE",
    "ROOF DECK",
    "ROOF LINE",
    "ROOF BEAM",
    "ROOF APEX",
    "EAVES LINE",
    "FINISHED FLOOR",
    "FLOOR FINISHED",
    "FFL",
)
NTH_FLOOR_RE = re.compile(r"\b\d+(?:ST|ND|RD|TH)\s+FLOOR(?:\s+LINE)?\b", re.IGNORECASE)
MM_STORY_DIM_RE = re.compile(r"^\s*([1-9]\d{3}(?:\.\d+)?)\s*(?:mm)?\s*$", re.IGNORECASE)
MIN_STORY_STEP_M = 1.50

ELEV_VALUE_RE = re.compile(
    r"(?:(?:EL\.?|ELEV\.?|FFL\.?)\s*)?([+-]?\s*\d+(?:\.\d+)?)\s*(?:m|M|mts?\.?|meters?)\b",
    re.IGNORECASE,
)
ELEV_PREFIX_ONLY_RE = re.compile(
    r"(?:EL\.?|ELEV\.?)\s*([+-]?\s*\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
LEVEL_TAG_RE = re.compile(
    r"\b(LEVEL\s*\d+|BASEMENT(?:\s*\d+)?|GROUND\s+FLOOR(?:\s+LINE)?|"
    r"FIRST\s+FLOOR(?:\s+LINE)?|1ST\s+FLOOR(?:\s+LINE)?|SECOND\s+FLOOR(?:\s+LINE)?|2ND\s+FLOOR(?:\s+LINE)?|"
    r"THIRD\s+FLOOR(?:\s+LINE)?|3RD\s+FLOOR(?:\s+LINE)?|"
    r"FOURTH\s+FLOOR|4TH\s+FLOOR|FIFTH\s+FLOOR|5TH\s+FLOOR|\d+(?:ST|ND|RD|TH)\s+FLOOR(?:\s+LINE)?|"
    r"MEZZANINE|PENTHOUSE|ROOF\s+DECK|ROOF\s+LINE|ROOF\s+BEAM|ROOF\s+APEX|EAVES\s+LINE)\b",
    re.IGNORECASE,
)


@dataclass
class DetectionOutput:
    method_used: str
    grids: list[GridCandidate]
    columns: list[ColumnCandidate]
    beams: list[BeamCandidate] = field(default_factory=list)
    notice: str | None = None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def _index_to_letters(idx: int) -> str:
    """0 -> A, 1 -> B, ..., 25 -> Z, 26 -> AA."""
    result = ""
    n = idx
    while True:
        result = chr(ord("A") + (n % 26)) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result


def _round_mm(value_mm: float, step: float = 10.0) -> float:
    rounded = round(value_mm / step) * step
    return max(100.0, float(rounded))


def _estimate_column_dims_mm(
    width_px: float,
    height_px: float,
    calibration: ScaleCalibration | None,
    pixels_per_meter: float | None = None,
) -> tuple[float, float]:
    ppm = (
        calibration.pixels_per_m
        if calibration is not None
        else (pixels_per_meter if (pixels_per_meter is not None and pixels_per_meter > 0) else None)
    )
    if ppm is None or ppm <= 0:
        return 400.0, 400.0
    w_mm = (width_px / ppm) * 1000.0
    d_mm = (height_px / ppm) * 1000.0
    if 120.0 <= w_mm <= 3000.0 and 120.0 <= d_mm <= 3000.0:
        return _round_mm(w_mm), _round_mm(d_mm)
    return 400.0, 400.0


def _region_bounds(page_info: PageInfo, region: PlanRegion | None) -> tuple[float, float, float, float]:
    if region is None:
        return 0.0, 0.0, float(page_info.width_px), float(page_info.height_px)
    return (
        float(region.x_px),
        float(region.y_px),
        float(region.x_px + region.width_px),
        float(region.y_px + region.height_px),
    )


def _exclude_region_boxes(exclude_regions: list[PlanRegion] | None) -> list[tuple[float, float, float, float]]:
    if not exclude_regions:
        return []
    return [
        (float(r.x_px), float(r.y_px), float(r.x_px + r.width_px), float(r.y_px + r.height_px))
        for r in exclude_regions
    ]


def _point_in_any_box(x: float, y: float, boxes: list[tuple[float, float, float, float]]) -> bool:
    for ex0, ey0, ex1, ey1 in boxes:
        if ex0 <= x <= ex1 and ey0 <= y <= ey1:
            return True
    return False


def _segment_excluded_by_boxes(
    axis: str,
    coord: float,
    lo: float,
    hi: float,
    boxes: list[tuple[float, float, float, float]],
) -> bool:
    if not boxes:
        return False
    mid = 0.5 * (lo + hi)
    if axis == "x":
        return _point_in_any_box(coord, mid, boxes)
    return _point_in_any_box(mid, coord, boxes)


def _mask_image_exclude_boxes(
    img: np.ndarray,
    offset_x: float,
    offset_y: float,
    boxes: list[tuple[float, float, float, float]],
) -> np.ndarray:
    if not boxes:
        return img
    masked = img.copy()
    h, w = masked.shape[:2]
    for ex0, ey0, ex1, ey1 in boxes:
        x0 = max(0, int(math.floor(ex0 - offset_x)))
        y0 = max(0, int(math.floor(ey0 - offset_y)))
        x1 = min(w, int(math.ceil(ex1 - offset_x)))
        y1 = min(h, int(math.ceil(ey1 - offset_y)))
        if x1 > x0 and y1 > y0:
            masked[y0:y1, x0:x1] = 255
    return masked


def _in_outer_border_margin(
    axis: str,
    coord: float,
    span_start: float,
    span_end: float,
    bounds: tuple[float, float, float, float],
    margin_ratio: float,
) -> bool:
    """Return True if a segment lies within the outer border_margin_ratio of bounds edges."""
    bx0, by0, bx1, by1 = bounds
    bw = max(bx1 - bx0, 1.0)
    bh = max(by1 - by0, 1.0)
    lo, hi = min(span_start, span_end), max(span_start, span_end)

    if margin_ratio <= 0.0:
        return False
    margin_x = bw * margin_ratio
    margin_y = bh * margin_ratio

    if axis == "x":
        if coord <= bx0 + margin_x or coord >= bx1 - margin_x:
            return True
        if hi <= by0 + margin_y or lo >= by1 - margin_y:
            return True
        return False

    if coord <= by0 + margin_y or coord >= by1 - margin_y:
        return True
    if hi <= bx0 + margin_x or lo >= bx1 - margin_x:
        return True
    return False


def _clip_segment_to_bounds(
    axis: str,
    coord: float,
    span_start: float,
    span_end: float,
    bounds: tuple[float, float, float, float],
) -> tuple[float, float] | None:
    bx0, by0, bx1, by1 = bounds
    lo, hi = min(span_start, span_end), max(span_start, span_end)
    if axis == "x":
        if coord < bx0 or coord > bx1:
            return None
        clipped_lo = max(lo, by0)
        clipped_hi = min(hi, by1)
        return (clipped_lo, clipped_hi) if clipped_hi > clipped_lo else None
    if coord < by0 or coord > by1:
        return None
    clipped_lo = max(lo, bx0)
    clipped_hi = min(hi, bx1)
    return (clipped_lo, clipped_hi) if clipped_hi > clipped_lo else None


def _cluster_1d(items: list[tuple[float, float, float]], tol: float) -> list[dict]:
    """
    Cluster colinear segments by primary coordinate within `tol` px.
    Each item is (coord, span_start, span_end) where span_start <= span_end.
    """
    if not items:
        return []
    sorted_items = sorted(items, key=lambda t: t[0])
    clusters: list[list[tuple[float, float, float]]] = [[sorted_items[0]]]
    for item in sorted_items[1:]:
        cluster_mean = sum(x[0] for x in clusters[-1]) / len(clusters[-1])
        if abs(item[0] - cluster_mean) <= tol:
            clusters[-1].append(item)
        else:
            clusters.append([item])

    results = []
    for cl in clusters:
        coord = sum(x[0] for x in cl) / len(cl)
        intervals = sorted((min(x[1], x[2]), max(x[1], x[2])) for x in cl)
        merged: list[tuple[float, float]] = [intervals[0]]
        for s, e in intervals[1:]:
            if s <= merged[-1][1] + 2.0:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        covered = sum(e - s for s, e in merged)
        max_single = max(e - s for s, e in merged)
        min_span = merged[0][0]
        max_span = merged[-1][1]
        results.append(
            {
                "coord": coord,
                "min_span": min_span,
                "max_span": max_span,
                "span": max_span - min_span,
                "covered": covered,
                "max_single": max_single,
                "count": len(cl),
            }
        )
    return results


def _cluster_qualifies_as_grid(
    cl: dict,
    min_span_px: float,
    merge_dashed: bool,
) -> bool:
    if cl["span"] < min_span_px:
        return False
    if not merge_dashed:
        return cl["max_single"] >= min_span_px
    if cl["max_single"] >= min_span_px * 0.65:
        return True
    if cl["covered"] >= cl["span"] * 0.25:
        return True
    if cl["count"] >= 3 and cl["covered"] >= cl["span"] * 0.10:
        return True
    return False


def _find_grid_label(
    axis: str,
    coord: float,
    span_min: float,
    span_max: float,
    words: list[tuple[int, float, float, str]],
    used_word_indices: set[int],
    endpoint_radius_px: float = 65.0,
) -> str | None:
    """Match a short (1-3 char alphanumeric, optional prime like 1') grid label near either endpoint."""
    best_idx: int | None = None
    best_label: str | None = None
    best_score = float("inf")

    for w_idx, wx, wy, text in words:
        if w_idx in used_word_indices:
            continue
        cleaned = text.strip()
        if not LABEL_PATTERN.match(cleaned):
            continue

        if axis == "x":
            d_start = hypot(wx - coord, wy - span_min)
            d_end = hypot(wx - coord, wy - span_max)
            perp = abs(wx - coord)
            inside = span_min + 4.0 < wy < span_max - 4.0
        else:
            d_start = hypot(wx - span_min, wy - coord)
            d_end = hypot(wx - span_max, wy - coord)
            perp = abs(wy - coord)
            inside = span_min + 4.0 < wx < span_max - 4.0

        end_dist = min(d_start, d_end)
        if end_dist > endpoint_radius_px or perp > 32.0:
            continue

        penalty = 18.0 if inside and end_dist > 12.0 else 0.0
        score = end_dist + perp * 0.5 + penalty
        if score < best_score:
            best_score = score
            best_idx = w_idx
            best_label = cleaned

    if best_idx is not None:
        used_word_indices.add(best_idx)
    return best_label


def _deduplicate_boxes(boxes: list[dict]) -> list[dict]:
    """Merge overlapping/concentric rectangle detections (e.g. stroke + fill of same column)."""
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda b: (not b.get("filled", False), -(b["w"] * b["h"])))
    kept: list[dict] = []
    for box in ordered:
        merged = False
        for existing in kept:
            dist = hypot(box["cx"] - existing["cx"], box["cy"] - existing["cy"])
            tol = max(max(box["w"], box["h"]), max(existing["w"], existing["h"])) * 0.65
            if dist <= max(tol, 8.0):
                existing["filled"] = existing.get("filled", False) or box.get("filled", False)
                merged = True
                break
        if not merged:
            kept.append(dict(box))
    return kept


def _assign_grid_ref(
    x_px: float,
    y_px: float,
    grids: list[GridCandidate],
    tol_px: float,
) -> tuple[str, bool]:
    x_grids = [g for g in grids if g.axis == "x"]
    y_grids = [g for g in grids if g.axis == "y"]

    nearest_x = min(x_grids, key=lambda g: abs(g.coord_px - x_px), default=None)
    nearest_y = min(y_grids, key=lambda g: abs(g.coord_px - y_px), default=None)

    x_hit = nearest_x if (nearest_x and abs(nearest_x.coord_px - x_px) <= tol_px) else None
    y_hit = nearest_y if (nearest_y and abs(nearest_y.coord_px - y_px) <= tol_px) else None

    if x_hit and y_hit:
        return f"{y_hit.label}-{x_hit.label}", True
    if y_hit:
        return y_hit.label, False
    if x_hit:
        return x_hit.label, False
    return "", False


def _extract_rect_from_drawing(
    drawing: dict,
    rot_mat: pymupdf.Matrix,
    ox: float,
    oy: float,
    sx: float,
    sy: float,
):
    """Extract axis-aligned rectangle bounds in display px (respecting page.rotation_matrix)."""
    items = drawing.get("items", [])
    rects: list[tuple[float, float, float, float]] = []
    line_segs: list[tuple[float, float, float, float]] = []

    for item in items:
        op = item[0]
        if op == "re":
            r = item[1] * rot_mat
            x0 = (float(r.x0) - ox) * sx
            y0 = (float(r.y0) - oy) * sy
            x1 = (float(r.x1) - ox) * sx
            y1 = (float(r.y1) - oy) * sy
            rects.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
        elif op == "qu":
            q = item[1]
            pts = [p * rot_mat for p in (q.ul, q.ur, q.ll, q.lr)]
            xs = [(float(p.x) - ox) * sx for p in pts]
            ys = [(float(p.y) - oy) * sy for p in pts]
            rects.append((min(xs), min(ys), max(xs), max(ys)))
        elif op == "l":
            p1 = item[1] * rot_mat
            p2 = item[2] * rot_mat
            line_segs.append(
                (
                    (float(p1.x) - ox) * sx,
                    (float(p1.y) - oy) * sy,
                    (float(p2.x) - ox) * sx,
                    (float(p2.y) - oy) * sy,
                )
            )

    if not rects and len(line_segs) in (4, 5):
        xs = [v for seg in line_segs for v in (seg[0], seg[2])]
        ys = [v for seg in line_segs for v in (seg[1], seg[3])]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        w, h = x1 - x0, y1 - y0
        if w > 2.0 and h > 2.0:
            axis_aligned = all(
                abs(seg[0] - seg[2]) <= 1.5 or abs(seg[1] - seg[3]) <= 1.5
                for seg in line_segs
            )
            if axis_aligned and (drawing.get("closePath") or drawing.get("fill") is not None):
                rects.append((x0, y0, x1, y1))
                line_segs = []

    return rects, line_segs


def _format_grid_ref(x_label: str, y_label: str) -> str:
    """Format intersection grid reference putting alphabetic axis first when mixed (e.g. A-1, A-1', B-3)."""
    x_alpha = bool(re.match(r"^[A-Za-z]+'?$", x_label.strip()))
    y_alpha = bool(re.match(r"^[A-Za-z]+'?$", y_label.strip()))
    if x_alpha and not y_alpha:
        return f"{x_label}-{y_label}"
    return f"{y_label}-{x_label}"


def _box_iou(a: dict, b: dict) -> float:
    ix0 = max(a["x0"], b["x0"])
    iy0 = max(a["y0"], b["y0"])
    ix1 = min(a["x1"], b["x1"])
    iy1 = min(a["y1"], b["y1"])
    iw = max(0.0, ix1 - ix0)
    ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    area_a = max((a["x1"] - a["x0"]) * (a["y1"] - a["y0"]), 1e-6)
    area_b = max((b["x1"] - b["x0"]) * (b["y1"] - b["y0"]), 1e-6)
    return inter / max(area_a + area_b - inter, 1e-6)


def _fill_brightness(fill) -> float:
    """Return approximate luminance in [0.0, 1.0] for a PyMuPDF drawing fill tuple."""
    if fill is None:
        return 1.0
    if isinstance(fill, (int, float)):
        return float(fill)
    vals = [float(v) for v in fill]
    if len(vals) == 1:
        return vals[0]
    if len(vals) == 3:
        return (vals[0] + vals[1] + vals[2]) / 3.0
    if len(vals) >= 4:
        c, m, y, k = vals[0], vals[1], vals[2], vals[3]
        return (1.0 - (c + m + y) / 3.0) * (1.0 - k)
    return 1.0


def _extract_text_word_boxes_px(
    page: pymupdf.Page,
    page_info: PageInfo,
    pad_px: float = 3.0,
) -> list[tuple[float, float, float, float, float, float, float, float]]:
    """
    Return all embedded PDF word bounding boxes in rendered pixel coordinates:
    (raw_x0, raw_y0, raw_x1, raw_y1, pad_x0, pad_y0, pad_x1, pad_y1).
    """
    rot_mat = page.rotation_matrix
    ox = float(page.rect.x0)
    oy = float(page.rect.y0)
    sx = float(page_info.width_px) / max(float(page.rect.width), 1.0)
    sy = float(page_info.height_px) / max(float(page.rect.height), 1.0)
    boxes: list[tuple[float, float, float, float, float, float, float, float]] = []
    for w_item in page.get_text("words"):
        txt = str(w_item[4]).strip()
        if not txt:
            continue
        wr = pymupdf.Rect(w_item[:4]) * rot_mat
        x0 = (float(wr.x0) - ox) * sx
        y0 = (float(wr.y0) - oy) * sy
        x1 = (float(wr.x1) - ox) * sx
        y1 = (float(wr.y1) - oy) * sy
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        if x1 > x0 and y1 > y0:
            p = pad_px if len(txt) >= 2 else min(pad_px, 1.0)
            boxes.append((x0, y0, x1, y1, x0 - p, y0 - p, x1 + p, y1 + p))
    return boxes


def _overlaps_text_boxes(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    text_boxes: list[tuple[float, float, float, float, float, float, float, float]],
    max_overlap_ratio: float = 0.25,
) -> bool:
    """Return True if candidate center lies inside a text box or padded overlap exceeds max_overlap_ratio."""
    if not text_boxes:
        return False
    cx = (x0 + x1) * 0.5
    cy = (y0 + y1) * 0.5
    cand_area = max((x1 - x0) * (y1 - y0), 1e-6)
    for rx0, ry0, rx1, ry1, tx0, ty0, tx1, ty1 in text_boxes:
        if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
            return True
        ix0 = max(x0, tx0)
        iy0 = max(y0, ty0)
        ix1 = min(x1, tx1)
        iy1 = min(y1, ty1)
        if ix1 > ix0 and iy1 > iy0:
            inter = (ix1 - ix0) * (iy1 - iy0)
            if inter / cand_area > max_overlap_ratio:
                return True
    return False


def _verify_uniform_gray_interior(
    full_img: np.ndarray,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    inset_px: int = 1,
    min_mid_gray_ratio: float = 0.70,
    max_black_ratio: float = 0.20,
    max_std_dev: float = 35.0,
) -> bool:
    """Strict uniform mid-gray interior check: kills text anti-aliasing and high-contrast furniture."""
    h_full, w_full = full_img.shape[:2]
    ix0 = max(0, int(round(x0)) + inset_px)
    iy0 = max(0, int(round(y0)) + inset_px)
    ix1 = min(w_full, int(round(x1)) - inset_px)
    iy1 = min(h_full, int(round(y1)) - inset_px)
    if ix1 - ix0 < 2 or iy1 - iy0 < 2:
        return False
    interior = full_img[iy0:iy1, ix0:ix1]
    if interior.size < 4:
        return False
    mid_gray_ratio = float(np.mean((interior >= 60) & (interior <= 215)))
    black_ratio = float(np.mean(interior < 50))
    std_dev = float(np.std(interior))
    return (
        mid_gray_ratio >= min_mid_gray_ratio
        and black_ratio <= max_black_ratio
        and std_dev <= max_std_dev
    )


def _verify_vector_hatched_interior(
    full_img: np.ndarray,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    opts: ExtractionOptions,
) -> bool:
    """Verify a closed 4-corner vector rectangle truly contains repeating hatch strokes (not hollow/pitch-black)."""
    h_full, w_full = full_img.shape[:2]
    bw = max(x1 - x0, 1.0)
    bh = max(y1 - y0, 1.0)
    inset = max(1, int(min(bw, bh) * 0.12))
    ix0 = max(0, int(round(x0)) + inset)
    iy0 = max(0, int(round(y0)) + inset)
    ix1 = min(w_full, int(round(x1)) - inset)
    iy1 = min(h_full, int(round(y1)) - inset)
    if ix1 - ix0 < 2 or iy1 - iy0 < 2:
        return False
    interior = full_img[iy0:iy1, ix0:ix1]
    if interior.size < 4:
        return False

    non_white_ratio = float(np.mean(interior < 235))
    solid_black_ratio = float(np.mean(interior < 35))
    if non_white_ratio < opts.column_min_fill_ratio or solid_black_ratio > 0.85:
        return False

    gx = cv2.Sobel(interior, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(interior, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    canny = cv2.Canny(interior, 40, 120)
    edge_density = max(float(np.mean(canny > 0)), float(np.mean(mag > 55.0)))

    ih, iw = interior.shape[:2]
    active_quads = 4
    if ih >= 4 and iw >= 4:
        mh, mw = ih // 2, iw // 2
        quads = (
            interior[:mh, :mw],
            interior[:mh, mw:],
            interior[mh:, :mw],
            interior[mh:, mw:],
        )
        active_quads = sum(1 for q in quads if float(np.mean(q < 235)) >= 0.15)
    return edge_density >= 0.16 and active_quads >= 3


def _merge_touching_boxes(
    boxes: list[tuple[float, float, float, float]],
    tol_px: float = 2.0,
) -> list[tuple[float, float, float, float]]:
    """
    Merge touching or overlapping bounding boxes within tol_px (e.g., AutoCAD SOLID
    column hatches exported as two adjacent right triangles sharing a diagonal).
    """
    if not boxes:
        return []
    n = len(boxes)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 1, n):
            bx0, by0, bx1, by1 = boxes[j]
            if (
                ax0 - tol_px <= bx1
                and bx0 - tol_px <= ax1
                and ay0 - tol_px <= by1
                and by0 - tol_px <= ay1
            ):
                union(i, j)

    groups: dict[int, list[tuple[float, float, float, float]]] = {}
    for i, box in enumerate(boxes):
        groups.setdefault(find(i), []).append(box)

    merged: list[tuple[float, float, float, float]] = []
    for grp in groups.values():
        merged.append(
            (
                min(b[0] for b in grp),
                min(b[1] for b in grp),
                max(b[2] for b in grp),
                max(b[3] for b in grp),
            )
        )
    return merged


def _numpy_erode_3x3(mask: np.ndarray) -> np.ndarray:
    """Pure-NumPy 3x3 binary erosion (zero cv2/scipy dependency)."""
    if mask.shape[0] < 3 or mask.shape[1] < 3:
        return np.zeros_like(mask, dtype=bool)
    out = np.zeros_like(mask, dtype=bool)
    out[1:-1, 1:-1] = (
        mask[:-2, :-2]
        & mask[:-2, 1:-1]
        & mask[:-2, 2:]
        & mask[1:-1, :-2]
        & mask[1:-1, 1:-1]
        & mask[1:-1, 2:]
        & mask[2:, :-2]
        & mask[2:, 1:-1]
        & mask[2:, 2:]
    )
    return out


def _numpy_dilate_3x3(mask: np.ndarray) -> np.ndarray:
    """Pure-NumPy 3x3 binary dilation (zero cv2/scipy dependency)."""
    if mask.shape[0] < 3 or mask.shape[1] < 3:
        return mask.copy()
    out = np.zeros_like(mask, dtype=bool)
    out[1:-1, 1:-1] = (
        mask[:-2, :-2]
        | mask[:-2, 1:-1]
        | mask[:-2, 2:]
        | mask[1:-1, :-2]
        | mask[1:-1, 1:-1]
        | mask[1:-1, 2:]
        | mask[2:, :-2]
        | mask[2:, 1:-1]
        | mask[2:, 2:]
    )
    return out


def _detect_dark_columns_raster_200dpi_numpy(
    pdf_path: Path,
    page_info: PageInfo,
    bounds: tuple[float, float, float, float],
    effective_ppm: float | None,
    text_boxes: list[tuple[float, float, float, float, float, float, float, float]],
    opts: ExtractionOptions,
    title_block_y: float,
    margin_ratio: float,
) -> list[dict]:
    """
    200 DPI PyMuPDF + pure-NumPy raster fallback for solid black or densely hatched
    rectangular columns when vector detection finds < 2 columns in a floor plan region.
    Uses ONLY pymupdf + numpy (zero cv2/scipy).
    """
    fallback_dpi = 200.0
    scale_to_200 = fallback_dpi / max(float(page_info.dpi), 1.0)
    scale_from_200 = 1.0 / scale_to_200

    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_info.page_number - 1]
        pix = page.get_pixmap(dpi=int(fallback_dpi), colorspace=pymupdf.csGRAY)
        img200 = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    finally:
        doc.close()

    h200, w200 = img200.shape[:2]
    bx0, by0, bx1, by1 = bounds
    rx0 = max(0, int(round(bx0 * scale_to_200)))
    ry0 = max(0, int(round(by0 * scale_to_200)))
    rx1 = min(w200, int(round(bx1 * scale_to_200)))
    ry1 = min(h200, int(round(by1 * scale_to_200)))
    if rx1 - rx0 < 6 or ry1 - ry0 < 6:
        return []

    crop = img200[ry0:ry1, rx0:rx1]
    # Dark pixels (solid black or dense dark hatch)
    dark_mask = crop < 100
    # Pure-NumPy 3x3 morphological opening strips thin 1-2px walls/gridlines while preserving solid blobs
    opened = _numpy_dilate_3x3(_numpy_erode_3x3(dark_mask))

    ch, cw = opened.shape
    ys_fg, xs_fg = np.nonzero(opened)
    if ys_fg.size == 0:
        return []

    # Size thresholds at 200 DPI
    if effective_ppm is not None and effective_ppm > 0:
        ppm200 = effective_ppm * scale_to_200
        min_px200 = max(6.0, min(opts.min_column_size_m, 0.18) * ppm200)
        max_px200 = max(min_px200 + 6.0, max(opts.max_column_size_m, 1.50) * ppm200)
    else:
        pt_to_200 = fallback_dpi / 72.0
        min_px200 = 4.0 * pt_to_200
        max_px200 = 45.0 * pt_to_200

    max_aspect = max(3.0, float(opts.column_max_aspect_ratio))
    visited = np.zeros((ch, cw), dtype=bool)
    results: list[dict] = []

    for start_y, start_x in zip(ys_fg.tolist(), xs_fg.tolist()):
        if visited[start_y, start_x]:
            continue
        stack = [(start_y, start_x)]
        visited[start_y, start_x] = True
        min_x = max_x = start_x
        min_y = max_y = start_y
        area = 0
        while stack:
            cy_i, cx_i = stack.pop()
            area += 1
            if cx_i < min_x:
                min_x = cx_i
            if cx_i > max_x:
                max_x = cx_i
            if cy_i < min_y:
                min_y = cy_i
            if cy_i > max_y:
                max_y = cy_i
            for ny, nx in (
                (cy_i - 1, cx_i),
                (cy_i + 1, cx_i),
                (cy_i, cx_i - 1),
                (cy_i, cx_i + 1),
            ):
                if 0 <= ny < ch and 0 <= nx < cw and opened[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))

        bw200 = float(max_x - min_x + 1)
        bh200 = float(max_y - min_y + 1)
        if not (min_px200 <= bw200 <= max_px200 and min_px200 <= bh200 <= max_px200):
            continue
        aspect = max(bw200, bh200) / max(min(bw200, bh200), 1e-6)
        if aspect > max_aspect:
            continue
        fill_ratio = float(area) / max(bw200 * bh200, 1.0)
        if fill_ratio < 0.50:
            continue

        x0 = (rx0 + min_x) * scale_from_200
        y0 = (ry0 + min_y) * scale_from_200
        x1 = (rx0 + max_x + 1) * scale_from_200
        y1 = (ry0 + max_y + 1) * scale_from_200
        cx = (x0 + x1) * 0.5
        cy = (y0 + y1) * 0.5
        if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
            continue
        if cy >= title_block_y:
            continue
        if margin_ratio > 0.0 and _in_outer_border_margin("x", cx, y0, y1, bounds, margin_ratio):
            continue
        if _overlaps_text_boxes(x0, y0, x1, y1, text_boxes):
            continue

        results.append(
            {
                "cx": cx,
                "cy": cy,
                "w": x1 - x0,
                "h": y1 - y0,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "source": "raster_200dpi",
                "fill_kind": "solid_black_raster",
                "base_conf": 0.79,
            }
        )
    return results


def _load_grayscale_image(
    pdf_path: Path | None,
    png_path: Path | None,
    page_info: PageInfo,
) -> np.ndarray | None:
    if png_path is not None and png_path.exists():
        img = cv2.imread(str(png_path), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            return img
    if pdf_path is not None and pdf_path.exists():
        doc = pymupdf.open(pdf_path)
        try:
            page = doc[page_info.page_number - 1]
            pix = page.get_pixmap(dpi=page_info.dpi, colorspace=pymupdf.csGRAY)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
            return arr
        finally:
            doc.close()
    return None


def detect_shaded_hatched_columns(
    pdf_path: Path | None,
    png_path: Path | None,
    page_info: PageInfo,
    calibration: ScaleCalibration | None = None,
    options: ExtractionOptions | None = None,
    region: PlanRegion | None = None,
    grids: list[GridCandidate] | None = None,
    pixels_per_meter: float | None = None,
    origin_px: PointPx | None = None,
    use_vector: bool = True,
    exclude_regions: list[PlanRegion] | None = None,
) -> list[ColumnCandidate]:
    """
    Hybrid vector + strict mid-gray raster detector for shaded or hatched square/rectangular columns.
    - Masks out all embedded PDF text word bounding boxes (never flags dimension numbers or room labels).
    - Never uses morphological closing on dark pixels (which fuses text into fake boxes).
    - Vector path detects solid non-white/non-black rectangular fills and closed 4-corner vector
      rectangles containing interior parallel/cross hatch strokes.
    - Raster path isolates strictly mid-gray pixels (60..215) with light 3x3 opening and uniform-gray
      interior verification (mid_gray >= 0.70, black <= 0.20, std_dev <= 35.0).
    """
    opts = options if options is not None else ExtractionOptions()
    full_img = _load_grayscale_image(pdf_path, png_path, page_info)
    if full_img is None:
        return []

    exclude_boxes = _exclude_region_boxes(exclude_regions)
    if exclude_boxes:
        full_img = _mask_image_exclude_boxes(full_img, 0.0, 0.0, exclude_boxes)

    full_h, full_w = full_img.shape[:2]
    bounds = _region_bounds(page_info, region)
    bx0, by0, bx1, by1 = bounds
    region_id = region.region_id if region is not None else None
    margin_ratio = opts.border_margin_ratio if region is None else 0.0
    title_block_y = float(page_info.height_px) * 0.88
    min_rect_ratio = max(float(opts.column_min_rectangularity), 0.78)

    # Resolve effective scale (pixels per meter)
    effective_ppm: float | None = None
    if calibration is not None and calibration.confirmed:
        effective_ppm = calibration.pixels_per_m
    elif pixels_per_meter is not None and pixels_per_meter > 0:
        effective_ppm = pixels_per_meter
    elif calibration is not None:
        effective_ppm = calibration.pixels_per_m

    if effective_ppm is not None and effective_ppm > 0:
        min_px = max(4.0, opts.min_column_size_m * effective_ppm)
        max_px = max(min_px + 4.0, opts.max_column_size_m * effective_ppm)
    else:
        dpi_scale = float(page_info.dpi) / 150.0
        min_px = 8.0 * dpi_scale
        max_px = 36.0 * dpi_scale

    effective_origin: PointPx | None = origin_px
    if effective_origin is None:
        if region is not None and region.origin_px is not None:
            effective_origin = region.origin_px
        elif calibration is not None:
            effective_origin = calibration.origin_px

    text_boxes: list[tuple[float, float, float, float]] = []
    verified: list[dict] = []

    # --- 1. Embedded Text Masking + Primary Vector Path (PyMuPDF) ---
    if pdf_path is not None and pdf_path.exists():
        doc = pymupdf.open(pdf_path)
        try:
            page = doc[page_info.page_number - 1]
            text_boxes = _extract_text_word_boxes_px(page, page_info, pad_px=3.0)

            if use_vector:
                rot_mat = page.rotation_matrix
                ox = float(page.rect.x0)
                oy = float(page.rect.y0)
                sx = float(page_info.width_px) / max(float(page.rect.width), 1.0)
                sy = float(page_info.height_px) / max(float(page.rect.height), 1.0)

                closed_stroke_rects: list[tuple[float, float, float, float]] = []
                short_segments: list[tuple[float, float, float, float, float]] = []
                dark_fill_subshapes: list[tuple[float, float, float, float]] = []
                pt_to_px = (sx + sy) * 0.5
                dark_min_px = (
                    max(4.0, min(opts.min_column_size_m, 0.18) * effective_ppm)
                    if (effective_ppm is not None and effective_ppm > 0)
                    else 4.0 * pt_to_px
                )
                dark_max_px = (
                    max(dark_min_px + 4.0, max(opts.max_column_size_m, 1.50) * effective_ppm)
                    if (effective_ppm is not None and effective_ppm > 0)
                    else 45.0 * pt_to_px
                )
                dark_max_aspect = max(3.0, float(opts.column_max_aspect_ratio))

                for drawing in page.get_drawings():
                    fill = drawing.get("fill")
                    items = drawing.get("items", [])
                    has_curve = any(it[0] == "c" for it in items)
                    has_explicit_rect = any(it[0] in ("re", "qu") for it in items)
                    rects, line_segs = _extract_rect_from_drawing(drawing, rot_mat, ox, oy, sx, sy)

                    # 1a-dark. Collect dark fill-only & polygonal/triangulated vector sub-shapes (RGB luminance < 0.25)
                    if fill is not None and not (has_curve and not has_explicit_rect):
                        avg_rgb_dark = _fill_brightness(fill)
                        if avg_rgb_dark < 0.25:
                            cand_boxes: list[tuple[float, float, float, float]] = list(rects)
                            if not cand_boxes and len(line_segs) >= 2:
                                xs_l = [v for seg in line_segs for v in (seg[0], seg[2])]
                                ys_l = [v for seg in line_segs for v in (seg[1], seg[3])]
                                lx0, lx1 = min(xs_l), max(xs_l)
                                ly0, ly1 = min(ys_l), max(ys_l)
                                if lx1 - lx0 >= 1.5 and ly1 - ly0 >= 1.5:
                                    cand_boxes.append((lx0, ly0, lx1, ly1))
                            elif not cand_boxes and drawing.get("rect") is not None:
                                dr = drawing["rect"] * rot_mat
                                dx0 = (float(dr.x0) - ox) * sx
                                dy0 = (float(dr.y0) - oy) * sy
                                dx1 = (float(dr.x1) - ox) * sx
                                dy1 = (float(dr.y1) - oy) * sy
                                dx0, dx1 = min(dx0, dx1), max(dx0, dx1)
                                dy0, dy1 = min(dy0, dy1), max(dy0, dy1)
                                if dx1 - dx0 >= 1.5 and dy1 - dy0 >= 1.5:
                                    cand_boxes.append((dx0, dy0, dx1, dy1))

                            for cx0, cy0, cx1, cy1 in cand_boxes:
                                cw_b = cx1 - cx0
                                ch_b = cy1 - cy0
                                if cw_b <= dark_max_px * 1.15 and ch_b <= dark_max_px * 1.15:
                                    mcx, mcy = (cx0 + cx1) * 0.5, (cy0 + cy1) * 0.5
                                    if bx0 <= mcx <= bx1 and by0 <= mcy <= by1 and mcy < title_block_y:
                                        dark_fill_subshapes.append((cx0, cy0, cx1, cy1))

                    # 1a. Solid filled rectangles (non-white, and non-pure-black unless any_filled)
                    if fill is not None and not (has_curve and not has_explicit_rect):
                        avg_rgb = _fill_brightness(fill)
                        is_non_white = avg_rgb < 0.92
                        is_non_black = avg_rgb >= 0.15
                        fill_allowed = (
                            is_non_white
                            if opts.column_fill_mode == "any_filled"
                            else (is_non_white and is_non_black)
                        )
                        if fill_allowed:
                            for x0, y0, x1, y1 in rects:
                                w = x1 - x0
                                h = y1 - y0
                                if not (min_px <= w <= max_px and min_px <= h <= max_px):
                                    continue
                                aspect = max(w, h) / max(min(w, h), 1e-6)
                                if aspect > opts.column_max_aspect_ratio:
                                    continue
                                cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
                                if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
                                    continue
                                if cy >= title_block_y:
                                    continue
                                if margin_ratio > 0.0 and _in_outer_border_margin("x", cx, y0, y1, bounds, margin_ratio):
                                    continue
                                if _overlaps_text_boxes(x0, y0, x1, y1, text_boxes):
                                    continue

                                if opts.column_fill_mode == "any_filled":
                                    inset = max(1, int(min(w, h) * 0.12))
                                    ix0 = max(0, int(round(x0)) + inset)
                                    iy0 = max(0, int(round(y0)) + inset)
                                    ix1 = min(full_w, int(round(x1)) - inset)
                                    iy1 = min(full_h, int(round(y1)) - inset)
                                    if ix1 <= ix0 or iy1 <= iy0:
                                        continue
                                    if float(np.mean(full_img[iy0:iy1, ix0:ix1] < 235)) < opts.column_min_fill_ratio:
                                        continue
                                    fill_kind = "filled"
                                else:
                                    if not _verify_uniform_gray_interior(full_img, x0, y0, x1, y1):
                                        continue
                                    fill_kind = "gray_shaded"

                                verified.append(
                                    {
                                        "cx": cx,
                                        "cy": cy,
                                        "w": w,
                                        "h": h,
                                        "x0": x0,
                                        "y0": y0,
                                        "x1": x1,
                                        "y1": y1,
                                        "source": "vector",
                                        "fill_kind": fill_kind,
                                        "base_conf": 0.83,
                                    }
                                )

                    # 1b. Collect closed 4-corner vector rectangles & short segments for true vector hatches
                    if opts.column_fill_mode == "shaded_or_hatched":
                        if fill is None and not (has_curve and not has_explicit_rect):
                            for x0, y0, x1, y1 in rects:
                                w = x1 - x0
                                h = y1 - y0
                                if not (min_px <= w <= max_px and min_px <= h <= max_px):
                                    continue
                                aspect = max(w, h) / max(min(w, h), 1e-6)
                                if aspect > opts.column_max_aspect_ratio:
                                    continue
                                cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
                                if not (bx0 <= cx <= bx1 and by0 <= cy <= by1) or cy >= title_block_y:
                                    continue
                                if margin_ratio > 0.0 and _in_outer_border_margin("x", cx, y0, y1, bounds, margin_ratio):
                                    continue
                                if _overlaps_text_boxes(x0, y0, x1, y1, text_boxes):
                                    continue
                                closed_stroke_rects.append((x0, y0, x1, y1))

                        for lx1, ly1, lx2, ly2 in line_segs:
                            seg_len = hypot(lx2 - lx1, ly2 - ly1)
                            if 2.0 <= seg_len <= max_px * 1.55:
                                mx = (lx1 + lx2) * 0.5
                                my = (ly1 + ly2) * 0.5
                                if bx0 <= mx <= bx1 and by0 <= my <= by1 and my < title_block_y:
                                    short_segments.append((lx1, ly1, lx2, ly2, seg_len))

                # Evaluate closed 4-corner vector rectangles that enclose >= 4 interior hatch strokes
                if opts.column_fill_mode == "shaded_or_hatched" and closed_stroke_rects and short_segments:
                    for x0, y0, x1, y1 in closed_stroke_rects:
                        w = x1 - x0
                        h = y1 - y0
                        max_hatch_len = hypot(w, h) * 1.08
                        tol = 1.5
                        interior_strokes = 0
                        for lx1, ly1, lx2, ly2, seg_len in short_segments:
                            if seg_len > max_hatch_len:
                                continue
                            if (
                                x0 - tol <= lx1 <= x1 + tol
                                and y0 - tol <= ly1 <= y1 + tol
                                and x0 - tol <= lx2 <= x1 + tol
                                and y0 - tol <= ly2 <= y1 + tol
                            ):
                                # Exclude segments lying right on the rectangle's 4 outer border edges
                                on_v_edge = (abs(lx1 - x0) <= 1.2 and abs(lx2 - x0) <= 1.2) or (
                                    abs(lx1 - x1) <= 1.2 and abs(lx2 - x1) <= 1.2
                                )
                                on_h_edge = (abs(ly1 - y0) <= 1.2 and abs(ly2 - y0) <= 1.2) or (
                                    abs(ly1 - y1) <= 1.2 and abs(ly2 - y1) <= 1.2
                                )
                                if not (on_v_edge or on_h_edge):
                                    interior_strokes += 1
                                    if interior_strokes >= 4:
                                        break
                        if interior_strokes >= 4 and _verify_vector_hatched_interior(full_img, x0, y0, x1, y1, opts):
                            verified.append(
                                {
                                    "cx": (x0 + x1) * 0.5,
                                    "cy": (y0 + y1) * 0.5,
                                    "w": w,
                                    "h": h,
                                    "x0": x0,
                                    "y0": y0,
                                    "x1": x1,
                                    "y1": y1,
                                    "source": "vector",
                                    "fill_kind": "hatched",
                                    "base_conf": 0.82,
                                }
                            )

                # 1c. Merge touching dark fill sub-shapes (e.g., split AutoCAD SOLID triangles within 2 pt)
                if dark_fill_subshapes and (
                    opts.column_fill_mode == "any_filled"
                    or (opts.column_fill_mode == "shaded_or_hatched" and len(verified) < 2)
                ):
                    merged_dark = _merge_touching_boxes(dark_fill_subshapes, tol_px=2.0 * pt_to_px)
                    for x0, y0, x1, y1 in merged_dark:
                        w = x1 - x0
                        h = y1 - y0
                        if not (dark_min_px <= w <= dark_max_px and dark_min_px <= h <= dark_max_px):
                            continue
                        aspect = max(w, h) / max(min(w, h), 1e-6)
                        if aspect > dark_max_aspect:
                            continue
                        cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
                        if not (bx0 <= cx <= bx1 and by0 <= cy <= by1) or cy >= title_block_y:
                            continue
                        if margin_ratio > 0.0 and _in_outer_border_margin("x", cx, y0, y1, bounds, margin_ratio):
                            continue
                        if _overlaps_text_boxes(x0, y0, x1, y1, text_boxes):
                            continue
                        inset = max(1, int(min(w, h) * 0.12))
                        ix0 = max(0, int(round(x0)) + inset)
                        iy0 = max(0, int(round(y0)) + inset)
                        ix1 = min(full_w, int(round(x1)) - inset)
                        iy1 = min(full_h, int(round(y1)) - inset)
                        if ix1 > ix0 and iy1 > iy0:
                            interior = full_img[iy0:iy1, ix0:ix1]
                            if float(np.mean(interior < 235)) < max(0.45, opts.column_min_fill_ratio):
                                continue
                        verified.append(
                            {
                                "cx": cx,
                                "cy": cy,
                                "w": w,
                                "h": h,
                                "x0": x0,
                                "y0": y0,
                                "x1": x1,
                                "y1": y1,
                                "source": "vector",
                                "fill_kind": "solid_black",
                                "base_conf": 0.84,
                            }
                        )
        finally:
            doc.close()

    # --- 2. Strict Mid-Gray Raster Path (no black pixels, no heavy closing that fuses text) ---
    rx0 = max(0, int(round(bx0)))
    ry0 = max(0, int(round(by0)))
    rx1 = min(full_w, int(round(bx1)))
    ry1 = min(full_h, int(round(by1)))
    crop = full_img[ry0:ry1, rx0:rx1]
    offset_x, offset_y = float(rx0), float(ry0)

    if crop.size > 0:
        if opts.column_fill_mode == "any_filled":
            mask_raw = (crop <= 215).astype(np.uint8) * 255
            k_open = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
            mask = cv2.morphologyEx(mask_raw, cv2.MORPH_OPEN, k_open)
        else:
            # Strictly mid-gray pixels ONLY (60..215): physically excludes all black text, walls,
            # outlines, and grid lines without any morphological closing!
            mask_raw = ((crop >= 60) & (crop <= 215)).astype(np.uint8) * 255
            k_open = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            mask = cv2.morphologyEx(mask_raw, cv2.MORPH_OPEN, k_open)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            area = float(cv2.contourArea(cnt))
            if area < min_px * min_px * 0.65:
                continue

            bx, by, bw_box, bh_box = cv2.boundingRect(cnt)
            bw_f, bh_f = float(bw_box), float(bh_box)
            if not (min_px <= bw_f <= max_px and min_px <= bh_f <= max_px):
                continue

            (cx_l, cy_l), (rw, rh), _ = cv2.minAreaRect(cnt)
            rw, rh = float(rw), float(rh)
            if rw <= 0.0 or rh <= 0.0:
                continue
            if not (min_px <= rw <= max_px and min_px <= rh <= max_px):
                continue

            aspect = max(bw_f, bh_f) / max(min(bw_f, bh_f), 1e-6)
            if aspect > opts.column_max_aspect_ratio:
                continue

            rectangularity = area / max(bw_f * bh_f, 1.0)
            min_area_rect_ratio = area / max(rw * rh, 1.0)
            if max(rectangularity, min_area_rect_ratio) < min_rect_ratio:
                continue

            hull = cv2.convexHull(cnt)
            hull_area = float(cv2.contourArea(hull))
            solidity = area / max(hull_area, 1.0)
            if solidity < 0.88:
                continue

            peri = float(cv2.arcLength(cnt, True))
            if peri <= 0.0:
                continue
            approx = cv2.approxPolyDP(cnt, 0.03 * peri, True)
            circularity = (4.0 * np.pi * area) / max(peri * peri, 1e-6)
            # Reject circles/hexagons/grid bubbles while tolerating tiny 1-2px corner notches on squares
            if len(approx) > 6 or (circularity > 0.855 and len(approx) > 4):
                continue

            x0 = offset_x + float(bx)
            y0 = offset_y + float(by)
            x1 = x0 + bw_f
            y1 = y0 + bh_f
            cx = offset_x + float(cx_l)
            cy = offset_y + float(cy_l)
            if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
                continue
            if cy >= title_block_y:
                continue
            if margin_ratio > 0.0 and _in_outer_border_margin("x", cx, y0, y1, bounds, margin_ratio):
                continue
            if _overlaps_text_boxes(x0, y0, x1, y1, text_boxes):
                continue

            if opts.column_fill_mode == "any_filled":
                inset = 1
                ix0 = max(0, int(round(x0)) + inset)
                iy0 = max(0, int(round(y0)) + inset)
                ix1 = min(full_w, int(round(x1)) - inset)
                iy1 = min(full_h, int(round(y1)) - inset)
                if ix1 <= ix0 or iy1 <= iy0:
                    continue
                if float(np.mean(full_img[iy0:iy1, ix0:ix1] < 235)) < opts.column_min_fill_ratio:
                    continue
                fill_kind = "filled"
            else:
                if not _verify_uniform_gray_interior(
                    full_img,
                    x0,
                    y0,
                    x1,
                    y1,
                    inset_px=1,
                    min_mid_gray_ratio=0.70,
                    max_black_ratio=0.20,
                    max_std_dev=35.0,
                ):
                    continue
                fill_kind = "gray_shaded"

            verified.append(
                {
                    "cx": cx,
                    "cy": cy,
                    "w": bw_f,
                    "h": bh_f,
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "source": "raster",
                    "fill_kind": fill_kind,
                    "base_conf": 0.78,
                }
            )

    # --- 2b. 200 DPI PyMuPDF + NumPy Raster Fallback (zero cv2/scipy) when < 2 columns found ---
    if (
        len(verified) < 2
        and opts.column_fill_mode != "gray_shaded_only"
        and pdf_path is not None
        and pdf_path.exists()
    ):
        verified.extend(
            _detect_dark_columns_raster_200dpi_numpy(
                pdf_path=pdf_path,
                page_info=page_info,
                bounds=bounds,
                effective_ppm=effective_ppm,
                text_boxes=text_boxes,
                opts=opts,
                title_block_y=title_block_y,
                margin_ratio=margin_ratio,
            )
        )

    if not verified:
        return []

    # --- 3. Deduplicate overlapping vector + raster candidates ---
    verified.sort(
        key=lambda c: (
            0 if c["source"] == "vector" else 1,
            -c["base_conf"],
            -(c["w"] * c["h"]),
        )
    )
    merged: list[dict] = []
    for cand in verified:
        is_dup = False
        for existing in merged:
            iou = _box_iou(cand, existing)
            dist = hypot(cand["cx"] - existing["cx"], cand["cy"] - existing["cy"])
            min_side = min(min(cand["w"], cand["h"]), min(existing["w"], existing["h"]))
            if iou > 0.35 or dist < max(min_side * 0.6, 6.0):
                is_dup = True
                break
        if not is_dup:
            merged.append(cand)

    merged.sort(key=lambda b: (-round(b["cy"] / 10.0), b["cx"]))

    # --- 5. Grid Alignment, Confidence Boost, Optional Snapping & Filtering ---
    active_grids = [g for g in (grids or []) if g.status != ReviewStatus.REJECTED]
    x_grids = [g for g in active_grids if g.axis == "x"]
    y_grids = [g for g in active_grids if g.axis == "y"]
    pt_to_px_snap = float(page_info.dpi) / 72.0
    min_snap_r = (
        0.15 * effective_ppm
        if (effective_ppm is not None and effective_ppm > 0)
        else 5.0 * pt_to_px_snap
    )
    probe_r = max(float(opts.column_probe_radius_px), min_snap_r)

    columns: list[ColumnCandidate] = []
    for cand in merged:
        cx = cand["cx"]
        cy = cand["cy"]
        w = cand["w"]
        h = cand["h"]
        base_conf = cand["base_conf"]

        best_pair: tuple[GridCandidate, GridCandidate] | None = None
        best_inter_dist = float("inf")
        for gx in x_grids:
            for gy in y_grids:
                d = hypot(cx - gx.coord_px, cy - gy.coord_px)
                if d <= probe_r and d < best_inter_dist:
                    best_inter_dist = d
                    best_pair = (gx, gy)

        grid_ref = ""
        conf = base_conf
        near_grid = False

        if best_pair is not None:
            gx, gy = best_pair
            near_grid = True
            grid_ref = _format_grid_ref(gx.label, gy.label)
            conf = min(0.98, round(base_conf + 0.15, 2))
            if opts.snap_columns_to_grids:
                cx = gx.coord_px
                cy = gy.coord_px
        else:
            nearest_x = min(x_grids, key=lambda g: abs(g.coord_px - cx), default=None)
            nearest_y = min(y_grids, key=lambda g: abs(g.coord_px - cy), default=None)
            x_hit = nearest_x if (nearest_x is not None and abs(nearest_x.coord_px - cx) <= probe_r) else None
            y_hit = nearest_y if (nearest_y is not None and abs(nearest_y.coord_px - cy) <= probe_r) else None
            if x_hit is not None or y_hit is not None:
                near_grid = True
                if y_hit is not None:
                    grid_ref = y_hit.label
                elif x_hit is not None:
                    grid_ref = x_hit.label
                conf = min(0.92, round(base_conf + 0.05, 2))

        if opts.require_near_grid and not near_grid:
            continue
        if exclude_boxes and _point_in_any_box(cx, cy, exclude_boxes):
            continue

        x_m: float | None = None
        y_m: float | None = None
        if effective_ppm is not None and effective_ppm > 0 and effective_origin is not None:
            x_m = round((cx - effective_origin.x_px) / effective_ppm, 4)
            y_m = round((effective_origin.y_px - cy) / effective_ppm, 4)
        elif calibration is not None:
            x_m, y_m = px_to_plan_m(cx, cy, calibration)

        w_mm, d_mm = _estimate_column_dims_mm(w, h, calibration, pixels_per_meter=effective_ppm)
        columns.append(
            ColumnCandidate(
                id=_new_id("col"),
                page_number=page_info.page_number,
                region_id=region_id,
                column_id=f"C{len(columns) + 1}",
                grid=grid_ref,
                x_px=round(cx, 2),
                y_px=round(cy, 2),
                width_px=round(w, 2),
                height_px=round(h, 2),
                x_m=x_m,
                y_m=y_m,
                width_mm=w_mm,
                depth_mm=d_mm,
                source="vector" if cand["source"] == "vector" else "raster",
                confidence=round(conf, 2),
                status=ReviewStatus.PENDING,
                notes=f"Detected {cand['fill_kind']} column ({round(w)}x{round(h)} px)",
            )
        )

    return columns


def _point_in_polygon_px(px: float, py: float, poly: list[PointPx]) -> bool:
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i].x_px, poly[i].y_px
        xj, yj = poly[j].x_px, poly[j].y_px
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def detect_diagonal_beams(
    pdf_path: Path,
    page_info: PageInfo,
    calibration: ScaleCalibration | None = None,
    region: PlanRegion | None = None,
    grids: list[GridCandidate] | None = None,
    columns: list[ColumnCandidate] | None = None,
    openings: list[DetectedOpening] | None = None,
    pixels_per_meter: float | None = None,
    origin_px: PointPx | None = None,
    exclude_regions: list[PlanRegion] | None = None,
) -> list[BeamCandidate]:
    """
    Detect non-orthogonal (diagonal/skewed) framing beams (10 deg <= angle <= 80 deg) whose
    endpoints snap cleanly to two distinct ColumnCandidate centers or X x Y grid intersections.
    Excludes elevation regions, segments inside DetectedOpening polygons (e.g. X-crosses),
    and short hatch/stair-tread strokes (< 0.8 m or < 24 px).
    """
    bounds = _region_bounds(page_info, region)
    bx0, by0, bx1, by1 = bounds
    exclude_boxes = _exclude_region_boxes(exclude_regions)
    region_id = region.region_id if region is not None else None

    effective_ppm: float | None = None
    if calibration is not None and calibration.confirmed:
        effective_ppm = calibration.pixels_per_m
    elif pixels_per_meter is not None and pixels_per_meter > 0:
        effective_ppm = pixels_per_meter
    elif calibration is not None:
        effective_ppm = calibration.pixels_per_m

    effective_origin: PointPx | None = origin_px
    if effective_origin is None:
        if region is not None and region.origin_px is not None:
            effective_origin = region.origin_px
        elif calibration is not None:
            effective_origin = calibration.origin_px

    min_len_px = max(24.0, 0.8 * effective_ppm) if (effective_ppm and effective_ppm > 0) else 24.0
    snap_tol_px = max(18.0, 0.15 * effective_ppm) if (effective_ppm and effective_ppm > 0) else 18.0

    # Build anchor points: column centers + X x Y grid intersections
    anchors: list[tuple[float, float, str]] = []
    for c in columns or []:
        if c.status == ReviewStatus.REJECTED:
            continue
        if bx0 - snap_tol_px <= c.x_px <= bx1 + snap_tol_px and by0 - snap_tol_px <= c.y_px <= by1 + snap_tol_px:
            if not _point_in_any_box(c.x_px, c.y_px, exclude_boxes):
                anchors.append((c.x_px, c.y_px, c.grid or c.column_id))

    active_grids = [g for g in (grids or []) if g.status != ReviewStatus.REJECTED]
    x_grids = [g for g in active_grids if g.axis == "x"]
    y_grids = [g for g in active_grids if g.axis == "y"]
    for gx in x_grids:
        for gy in y_grids:
            ix, iy = gx.coord_px, gy.coord_px
            if bx0 - snap_tol_px <= ix <= bx1 + snap_tol_px and by0 - snap_tol_px <= iy <= by1 + snap_tol_px:
                if not _point_in_any_box(ix, iy, exclude_boxes):
                    if not any(hypot(ix - ax, iy - ay) <= 6.0 for ax, ay, _ in anchors):
                        anchors.append((ix, iy, _format_grid_ref(gx.label, gy.label)))

    if len(anchors) < 2:
        return []

    def _nearest_anchor(px: float, py: float) -> tuple[int, float, float, str] | None:
        best_idx = -1
        best_d = snap_tol_px
        for idx, (ax, ay, lbl) in enumerate(anchors):
            d = hypot(px - ax, py - ay)
            if d <= best_d:
                best_d = d
                best_idx = idx
        if best_idx < 0:
            return None
        ax, ay, lbl = anchors[best_idx]
        return best_idx, ax, ay, lbl

    active_openings = [o for o in (openings or []) if len(o.polygon_px) >= 3]
    seen_pairs: dict[tuple[int, int], tuple[float, tuple[float, float, float, float, str, float]]] = {}

    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_info.page_number - 1]
        rot_mat = page.rotation_matrix
        ox = float(page.rect.x0)
        oy = float(page.rect.y0)
        sx = float(page_info.width_px) / max(float(page.rect.width), 1.0)
        sy = float(page_info.height_px) / max(float(page.rect.height), 1.0)

        for drawing in page.get_drawings():
            _, line_segs = _extract_rect_from_drawing(drawing, rot_mat, ox, oy, sx, sy)
            for x1, y1, x2, y2 in line_segs:
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                seg_len = hypot(dx, dy)
                if seg_len < min_len_px:
                    continue
                angle_deg = abs(math.degrees(math.atan2(dy, dx)))
                if not (10.0 <= angle_deg <= 80.0):
                    continue
                mx, my = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
                if not (bx0 <= mx <= bx1 and by0 <= my <= by1):
                    continue
                if exclude_boxes and (
                    _point_in_any_box(mx, my, exclude_boxes)
                    or _point_in_any_box(x1, y1, exclude_boxes)
                    or _point_in_any_box(x2, y2, exclude_boxes)
                ):
                    continue
                if any(_point_in_polygon_px(mx, my, op.polygon_px) for op in active_openings):
                    continue

                a1 = _nearest_anchor(x1, y1)
                a2 = _nearest_anchor(x2, y2)
                if a1 is None or a2 is None or a1[0] == a2[0]:
                    continue
                i1, sx1, sy1, lbl1 = a1
                i2, sx2, sy2, lbl2 = a2
                snapped_len = hypot(sx2 - sx1, sy2 - sy1)
                if snapped_len < min_len_px:
                    continue
                pair_key = (min(i1, i2), max(i1, i2))
                if i1 > i2:
                    sx1, sy1, lbl1, sx2, sy2, lbl2 = sx2, sy2, lbl2, sx1, sy1, lbl1
                gref = f"{lbl1}-{lbl2}".strip("-") if (lbl1 or lbl2) else ""
                snapped_angle = round(abs(math.degrees(math.atan2(abs(sy2 - sy1), abs(sx2 - sx1)))), 2)
                prev = seen_pairs.get(pair_key)
                if prev is None or snapped_len > prev[0]:
                    seen_pairs[pair_key] = (snapped_len, (sx1, sy1, sx2, sy2, gref, snapped_angle))
    finally:
        doc.close()

    beams: list[BeamCandidate] = []
    for idx, (_, (_, (sx1, sy1, sx2, sy2, gref, ang))) in enumerate(sorted(seen_pairs.items()), start=1):
        x1_m = y1_m = x2_m = y2_m = length_m = None
        if effective_ppm is not None and effective_ppm > 0 and effective_origin is not None:
            x1_m = round((sx1 - effective_origin.x_px) / effective_ppm, 4)
            y1_m = round((effective_origin.y_px - sy1) / effective_ppm, 4)
            x2_m = round((sx2 - effective_origin.x_px) / effective_ppm, 4)
            y2_m = round((effective_origin.y_px - sy2) / effective_ppm, 4)
            length_m = round(hypot(x2_m - x1_m, y2_m - y1_m), 4)
        elif calibration is not None:
            x1_m, y1_m = px_to_plan_m(sx1, sy1, calibration)
            x2_m, y2_m = px_to_plan_m(sx2, sy2, calibration)
            length_m = round(hypot(x2_m - x1_m, y2_m - y1_m), 4)

        beams.append(
            BeamCandidate(
                id=_new_id("beam"),
                page_number=page_info.page_number,
                region_id=region_id,
                beam_id=f"DB{idx}",
                grid_ref=gref,
                member_type="primary_beam",
                x1_px=round(sx1, 2),
                y1_px=round(sy1, 2),
                x2_px=round(sx2, 2),
                y2_px=round(sy2, 2),
                x1_m=x1_m,
                y1_m=y1_m,
                x2_m=x2_m,
                y2_m=y2_m,
                angle_deg=ang,
                length_m=length_m,
                source="vector",
                confidence=0.88,
                status=ReviewStatus.PENDING,
                notes=f"Detected diagonal beam ({ang:.1f} deg)",
            )
        )
    return beams


def detect_vector_elements(
    pdf_path: Path,
    page_info: PageInfo,
    calibration: ScaleCalibration | None = None,
    options: ExtractionOptions | None = None,
    region: PlanRegion | None = None,
    png_path: Path | None = None,
    existing_grids: list[GridCandidate] | None = None,
    existing_columns: list[ColumnCandidate] | None = None,
    openings: list[DetectedOpening] | None = None,
    pixels_per_meter: float | None = None,
    origin_px: PointPx | None = None,
    exclude_regions: list[PlanRegion] | None = None,
) -> DetectionOutput:
    """Extract grid lines (including merged dashed segments + endpoint labels) and shaded/hatched columns."""
    opts = options if options is not None else ExtractionOptions()
    bounds = _region_bounds(page_info, region)
    exclude_boxes = _exclude_region_boxes(exclude_regions)
    bx0, by0, bx1, by1 = bounds
    bw = max(bx1 - bx0, 1.0)
    bh = max(by1 - by0, 1.0)
    region_id = region.region_id if region is not None else None

    grids: list[GridCandidate] = []
    if opts.detect_grids:
        doc = pymupdf.open(pdf_path)
        try:
            page = doc[page_info.page_number - 1]
            rot_mat = page.rotation_matrix
            ox = float(page.rect.x0)
            oy = float(page.rect.y0)
            sx = float(page_info.width_px) / max(float(page.rect.width), 1.0)
            sy = float(page_info.height_px) / max(float(page.rect.height), 1.0)

            min_v_span = bh * opts.min_span_ratio
            min_h_span = bw * opts.min_span_ratio
            min_seg_len = 2.5 if opts.merge_dashed_segments else 8.0

            vert_segs: list[tuple[float, float, float]] = []
            horiz_segs: list[tuple[float, float, float]] = []

            for drawing in page.get_drawings():
                rects, line_segs = _extract_rect_from_drawing(drawing, rot_mat, ox, oy, sx, sy)

                for x0, y0, x1, y1 in rects:
                    w = x1 - x0
                    h = y1 - y0
                    if w <= 3.5 and h >= min_seg_len:
                        xc = (x0 + x1) * 0.5
                        clipped = _clip_segment_to_bounds("x", xc, y0, y1, bounds)
                        if (
                            clipped
                            and not _segment_excluded_by_boxes("x", xc, clipped[0], clipped[1], exclude_boxes)
                            and not _in_outer_border_margin("x", xc, clipped[0], clipped[1], bounds, opts.border_margin_ratio)
                        ):
                            vert_segs.append((xc, clipped[0], clipped[1]))
                    elif h <= 3.5 and w >= min_seg_len:
                        yc = (y0 + y1) * 0.5
                        clipped = _clip_segment_to_bounds("y", yc, x0, x1, bounds)
                        if (
                            clipped
                            and not _segment_excluded_by_boxes("y", yc, clipped[0], clipped[1], exclude_boxes)
                            and not _in_outer_border_margin("y", yc, clipped[0], clipped[1], bounds, opts.border_margin_ratio)
                        ):
                            horiz_segs.append((yc, clipped[0], clipped[1]))

                for x1, y1, x2, y2 in line_segs:
                    dx = abs(x2 - x1)
                    dy = abs(y2 - y1)
                    length = hypot(dx, dy)
                    if length < min_seg_len:
                        continue
                    if dx <= max(2.0, length * 0.025):
                        xc = (x1 + x2) * 0.5
                        clipped = _clip_segment_to_bounds("x", xc, y1, y2, bounds)
                        if clipped and (clipped[1] - clipped[0]) >= min_seg_len:
                            if (
                                not _segment_excluded_by_boxes("x", xc, clipped[0], clipped[1], exclude_boxes)
                                and not _in_outer_border_margin("x", xc, clipped[0], clipped[1], bounds, opts.border_margin_ratio)
                            ):
                                vert_segs.append((xc, clipped[0], clipped[1]))
                    elif dy <= max(2.0, length * 0.025):
                        yc = (y1 + y2) * 0.5
                        clipped = _clip_segment_to_bounds("y", yc, x1, x2, bounds)
                        if clipped and (clipped[1] - clipped[0]) >= min_seg_len:
                            if (
                                not _segment_excluded_by_boxes("y", yc, clipped[0], clipped[1], exclude_boxes)
                                and not _in_outer_border_margin("y", yc, clipped[0], clipped[1], bounds, opts.border_margin_ratio)
                            ):
                                horiz_segs.append((yc, clipped[0], clipped[1]))

            words: list[tuple[int, float, float, str]] = []
            if opts.detect_grid_labels or opts.require_grid_label:
                pad = 70.0
                for idx_w, w_item in enumerate(page.get_text("words")):
                    wr = pymupdf.Rect(w_item[:4]) * rot_mat
                    text = w_item[4]
                    wx = ((float(wr.x0) + float(wr.x1)) * 0.5 - ox) * sx
                    wy = ((float(wr.y0) + float(wr.y1)) * 0.5 - oy) * sy
                    if bx0 - pad <= wx <= bx1 + pad and by0 - pad <= wy <= by1 + pad:
                        if not _point_in_any_box(wx, wy, exclude_boxes):
                            words.append((idx_w, wx, wy, str(text)))

            vert_clusters = _cluster_1d(vert_segs, tol=opts.cluster_tolerance_px)
            horiz_clusters = _cluster_1d(horiz_segs, tol=opts.cluster_tolerance_px)

            raw_grids: list[tuple[float, float, GridCandidate]] = []
            used_word_indices: set[int] = set()
            used_x_labels: set[str] = set()
            x_idx = 0
            for cl in sorted(vert_clusters, key=lambda c: c["coord"]):
                if not _cluster_qualifies_as_grid(cl, min_v_span, opts.merge_dashed_segments):
                    continue
                if _in_outer_border_margin("x", cl["coord"], cl["min_span"], cl["max_span"], bounds, opts.border_margin_ratio):
                    continue

                label = (
                    _find_grid_label("x", cl["coord"], cl["min_span"], cl["max_span"], words, used_word_indices)
                    if (opts.detect_grid_labels or opts.require_grid_label)
                    else None
                )
                if not label:
                    if opts.require_grid_label:
                        continue
                    x_idx += 1
                    label = str(x_idx)
                    while label in used_x_labels:
                        x_idx += 1
                        label = str(x_idx)
                used_x_labels.add(label)

                coord_m = None
                if calibration is not None:
                    coord_m, _ = px_to_plan_m(cl["coord"], cl["min_span"], calibration)

                is_solid = cl["max_single"] >= min_v_span * 0.75
                conf = 0.92 if is_solid else 0.85
                raw_grids.append(
                    (
                        conf,
                        cl["span"],
                        GridCandidate(
                            id=_new_id("grid"),
                            page_number=page_info.page_number,
                            region_id=region_id,
                            axis="x",
                            label=label,
                            x1_px=round(cl["coord"], 2),
                            y1_px=round(cl["min_span"], 2),
                            x2_px=round(cl["coord"], 2),
                            y2_px=round(cl["max_span"], 2),
                            coord_px=round(cl["coord"], 2),
                            coord_m=coord_m,
                            source="vector",
                            confidence=conf,
                            status=ReviewStatus.PENDING,
                        ),
                    )
                )

            used_y_labels: set[str] = set()
            y_idx = 0
            for cl in sorted(horiz_clusters, key=lambda c: -c["coord"]):
                if not _cluster_qualifies_as_grid(cl, min_h_span, opts.merge_dashed_segments):
                    continue
                if _in_outer_border_margin("y", cl["coord"], cl["min_span"], cl["max_span"], bounds, opts.border_margin_ratio):
                    continue

                label = (
                    _find_grid_label("y", cl["coord"], cl["min_span"], cl["max_span"], words, used_word_indices)
                    if (opts.detect_grid_labels or opts.require_grid_label)
                    else None
                )
                if not label:
                    if opts.require_grid_label:
                        continue
                    label = _index_to_letters(y_idx)
                    y_idx += 1
                    while label in used_y_labels:
                        label = _index_to_letters(y_idx)
                        y_idx += 1
                used_y_labels.add(label)

                coord_m = None
                if calibration is not None:
                    _, coord_m = px_to_plan_m(cl["min_span"], cl["coord"], calibration)

                is_solid = cl["max_single"] >= min_h_span * 0.75
                conf = 0.92 if is_solid else 0.85
                raw_grids.append(
                    (
                        conf,
                        cl["span"],
                        GridCandidate(
                            id=_new_id("grid"),
                            page_number=page_info.page_number,
                            region_id=region_id,
                            axis="y",
                            label=label,
                            x1_px=round(cl["min_span"], 2),
                            y1_px=round(cl["coord"], 2),
                            x2_px=round(cl["max_span"], 2),
                            y2_px=round(cl["coord"], 2),
                            coord_px=round(cl["coord"], 2),
                            coord_m=coord_m,
                            source="vector",
                            confidence=conf,
                            status=ReviewStatus.PENDING,
                        ),
                    )
                )

            best_by_key: dict[tuple[str, str], tuple[float, float, GridCandidate]] = {}
            for conf, span, g in raw_grids:
                key = (g.axis, g.label.upper())
                existing = best_by_key.get(key)
                if existing is None or (conf, span) > (existing[0], existing[1]):
                    best_by_key[key] = (conf, span, g)
            grids = [item[2] for item in best_by_key.values()]
            grids.sort(key=lambda g: (0 if g.axis == "x" else 1, g.coord_px if g.axis == "x" else -g.coord_px))
        finally:
            doc.close()

    snap_grids = grids if opts.detect_grids and grids else (existing_grids or [])
    columns: list[ColumnCandidate] = []
    if opts.detect_columns:
        columns = detect_shaded_hatched_columns(
            pdf_path=pdf_path,
            png_path=png_path,
            page_info=page_info,
            calibration=calibration,
            options=opts,
            region=region,
            grids=snap_grids,
            pixels_per_meter=pixels_per_meter,
            origin_px=origin_px,
            use_vector=True,
            exclude_regions=exclude_regions,
        )

    beams: list[BeamCandidate] = []
    if opts.detect_diagonal_beams and (region is None or region.region_type == "plan"):
        all_cols_for_beams = list(columns) + [
            c for c in (existing_columns or []) if c.status != ReviewStatus.REJECTED
        ]
        beams = detect_diagonal_beams(
            pdf_path=pdf_path,
            page_info=page_info,
            calibration=calibration,
            region=region,
            grids=snap_grids,
            columns=all_cols_for_beams,
            openings=openings,
            pixels_per_meter=pixels_per_meter,
            origin_px=origin_px,
            exclude_regions=exclude_regions,
        )

    scope_label = f" in region '{region.name}'" if region is not None else ""
    beam_suffix = f", {len(beams)} diagonal beam(s)" if beams else ""
    if not opts.detect_grids and opts.detect_columns:
        notice = (
            f"Shaded/hatched column scan{scope_label} ({opts.column_fill_mode}) found "
            f"{len(columns)} column candidate(s){beam_suffix}; existing grids preserved."
        )
    else:
        notice = (
            f"Vector extraction{scope_label} found {len(grids)} grid line(s), {len(columns)} column candidate(s){beam_suffix}. "
            "Engineer review is required before transfer."
        )
    return DetectionOutput(
        method_used="vector",
        grids=grids,
        columns=columns,
        beams=beams,
        notice=notice,
    )


def detect_raster_elements(
    png_path: Path,
    page_info: PageInfo,
    calibration: ScaleCalibration | None = None,
    options: ExtractionOptions | None = None,
    region: PlanRegion | None = None,
    existing_grids: list[GridCandidate] | None = None,
    pixels_per_meter: float | None = None,
    origin_px: PointPx | None = None,
    exclude_regions: list[PlanRegion] | None = None,
) -> DetectionOutput:
    """OpenCV raster fallback detector with border margin exclusion, dashed-grid merging, and shaded/hatched column scanning."""
    opts = options if options is not None else ExtractionOptions()
    full_img = cv2.imread(str(png_path), cv2.IMREAD_GRAYSCALE)
    if full_img is None:
        raise FileNotFoundError(f"Rendered page image not found: {png_path}")

    exclude_boxes = _exclude_region_boxes(exclude_regions)
    full_h, full_w = full_img.shape[:2]
    if region is not None:
        rx0 = max(0, int(round(region.x_px)))
        ry0 = max(0, int(round(region.y_px)))
        rx1 = min(full_w, int(round(region.x_px + region.width_px)))
        ry1 = min(full_h, int(round(region.y_px + region.height_px)))
        img = full_img[ry0:ry1, rx0:rx1]
        offset_x, offset_y = float(rx0), float(ry0)
        region_id = region.region_id
    else:
        img = full_img
        offset_x, offset_y = 0.0, 0.0
        region_id = None

    if exclude_boxes:
        img = _mask_image_exclude_boxes(img, offset_x, offset_y, exclude_boxes)

    grids: list[GridCandidate] = []
    if opts.detect_grids:
        h_img, w_img = img.shape[:2]
        bounds = (offset_x, offset_y, offset_x + float(w_img), offset_y + float(h_img))
        min_dim = min(float(w_img), float(h_img))
        min_v_span = float(h_img) * opts.min_span_ratio
        min_h_span = float(w_img) * opts.min_span_ratio

        blurred = cv2.GaussianBlur(img, (3, 3), 0)
        edges = cv2.Canny(blurred, 50, 150, apertureSize=3)

        if opts.merge_dashed_segments:
            hough_min_len = max(12, int(min_dim * 0.018))
            hough_threshold = 22
            hough_max_gap = 22
        else:
            hough_min_len = max(int(min_dim * opts.min_span_ratio * 0.8), 60)
            hough_threshold = 60
            hough_max_gap = 18

        raw_lines = cv2.HoughLinesP(
            edges,
            rho=1,
            theta=np.pi / 180.0,
            threshold=hough_threshold,
            minLineLength=hough_min_len,
            maxLineGap=hough_max_gap,
        )

        vert_segs: list[tuple[float, float, float]] = []
        horiz_segs: list[tuple[float, float, float]] = []
        if raw_lines is not None:
            for line in np.asarray(raw_lines).reshape(-1, 4):
                x1, y1, x2, y2 = [float(v) for v in line]
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                length = hypot(dx, dy)
                if length < hough_min_len:
                    continue
                gx1, gy1, gx2, gy2 = x1 + offset_x, y1 + offset_y, x2 + offset_x, y2 + offset_y
                if dx <= max(3.0, length * 0.025):
                    xc = (gx1 + gx2) * 0.5
                    ys, ye = min(gy1, gy2), max(gy1, gy2)
                    if not _in_outer_border_margin("x", xc, ys, ye, bounds, opts.border_margin_ratio):
                        vert_segs.append((xc, ys, ye))
                elif dy <= max(3.0, length * 0.025):
                    yc = (gy1 + gy2) * 0.5
                    xs, xe = min(gx1, gx2), max(gx1, gx2)
                    if not _in_outer_border_margin("y", yc, xs, xe, bounds, opts.border_margin_ratio):
                        horiz_segs.append((yc, xs, xe))

        vert_clusters = _cluster_1d(vert_segs, tol=opts.cluster_tolerance_px)
        horiz_clusters = _cluster_1d(horiz_segs, tol=opts.cluster_tolerance_px)

        x_idx = 0
        for cl in sorted(vert_clusters, key=lambda c: c["coord"]):
            if not _cluster_qualifies_as_grid(cl, min_v_span, opts.merge_dashed_segments):
                continue
            if _in_outer_border_margin("x", cl["coord"], cl["min_span"], cl["max_span"], bounds, opts.border_margin_ratio):
                continue
            x_idx += 1
            coord_m = None
            if calibration is not None:
                coord_m, _ = px_to_plan_m(cl["coord"], cl["min_span"], calibration)
            grids.append(
                GridCandidate(
                    id=_new_id("grid"),
                    page_number=page_info.page_number,
                    region_id=region_id,
                    axis="x",
                    label=str(x_idx),
                    x1_px=round(cl["coord"], 2),
                    y1_px=round(cl["min_span"], 2),
                    x2_px=round(cl["coord"], 2),
                    y2_px=round(cl["max_span"], 2),
                    coord_px=round(cl["coord"], 2),
                    coord_m=coord_m,
                    source="raster",
                    confidence=0.72,
                    status=ReviewStatus.PENDING,
                )
            )

        y_idx = 0
        for cl in sorted(horiz_clusters, key=lambda c: -c["coord"]):
            if not _cluster_qualifies_as_grid(cl, min_h_span, opts.merge_dashed_segments):
                continue
            if _in_outer_border_margin("y", cl["coord"], cl["min_span"], cl["max_span"], bounds, opts.border_margin_ratio):
                continue
            label = _index_to_letters(y_idx)
            y_idx += 1
            coord_m = None
            if calibration is not None:
                _, coord_m = px_to_plan_m(cl["min_span"], cl["coord"], calibration)
            grids.append(
                GridCandidate(
                    id=_new_id("grid"),
                    page_number=page_info.page_number,
                    region_id=region_id,
                    axis="y",
                    label=label,
                    x1_px=round(cl["min_span"], 2),
                    y1_px=round(cl["coord"], 2),
                    x2_px=round(cl["max_span"], 2),
                    y2_px=round(cl["coord"], 2),
                    coord_px=round(cl["coord"], 2),
                    coord_m=coord_m,
                    source="raster",
                    confidence=0.72,
                    status=ReviewStatus.PENDING,
                )
            )

    snap_grids = grids if opts.detect_grids and grids else (existing_grids or [])
    columns: list[ColumnCandidate] = []
    if opts.detect_columns:
        columns = detect_shaded_hatched_columns(
            pdf_path=None,
            png_path=png_path,
            page_info=page_info,
            calibration=calibration,
            options=opts,
            region=region,
            grids=snap_grids,
            pixels_per_meter=pixels_per_meter,
            origin_px=origin_px,
            use_vector=False,
            exclude_regions=exclude_regions,
        )

    if not opts.detect_grids and opts.detect_columns:
        notice = (
            f"Raster shaded/hatched column scan ({opts.column_fill_mode}) found "
            f"{len(columns)} column candidate(s); existing grids preserved."
        )
    else:
        notice = (
            f"OpenCV raster extraction found {len(grids)} grid line(s) and {len(columns)} column candidate(s). "
            "Raster detections are approximate and require engineer verification."
        )
    return DetectionOutput(
        method_used="raster",
        grids=grids,
        columns=columns,
        notice=notice,
    )


def probe_columns_at_grid_intersections(
    pdf_path: Path,
    png_path: Path,
    page_info: PageInfo,
    target_region: PlanRegion,
    target_grids: list[GridCandidate],
    calibration: ScaleCalibration | None = None,
) -> list[ColumnCandidate]:
    """
    Intersection-gated shaded/hatched column probing inside `target_region` using propagated master grid lines.
    Only keeps candidate columns near (x_grid, y_grid) intersections inside `target_region`.
    """
    x_grids = [g for g in target_grids if g.axis == "x"]
    y_grids = [g for g in target_grids if g.axis == "y"]
    if not x_grids or not y_grids:
        return []

    probe_opts = ExtractionOptions(
        detect_grids=False,
        detect_columns=True,
        require_near_grid=True,
        snap_columns_to_grids=False,
    )
    detected = detect_shaded_hatched_columns(
        pdf_path=pdf_path if page_info.classification in ("vector", "hybrid") else None,
        png_path=png_path,
        page_info=page_info,
        calibration=calibration,
        options=probe_opts,
        region=target_region,
        grids=target_grids,
        use_vector=page_info.classification in ("vector", "hybrid"),
    )

    bx0, by0, bx1, by1 = _region_bounds(page_info, target_region)
    probed: list[ColumnCandidate] = []
    matched_intersections: set[tuple[str, str]] = set()

    for col in detected:
        if not (bx0 <= col.x_px <= bx1 and by0 <= col.y_px <= by1):
            continue
        tol = max(max(col.width_px, col.height_px) * 1.15, probe_opts.column_probe_radius_px)
        best_pair = None
        best_dist = float("inf")
        for gx in x_grids:
            for gy in y_grids:
                d = hypot(col.x_px - gx.coord_px, col.y_px - gy.coord_px)
                if d <= tol and d < best_dist:
                    best_dist = d
                    best_pair = (gx, gy)
        if best_pair is None:
            continue

        gx, gy = best_pair
        key = (gy.label, gx.label)
        if key in matched_intersections:
            continue
        matched_intersections.add(key)

        x_m = gx.coord_m if gx.coord_m is not None else col.x_m
        y_m = gy.coord_m if gy.coord_m is not None else col.y_m
        probed.append(
            ColumnCandidate(
                id=_new_id("col"),
                page_number=page_info.page_number,
                region_id=target_region.region_id,
                column_id=f"C{len(probed) + 1}",
                grid=_format_grid_ref(gx.label, gy.label),
                x_px=col.x_px,
                y_px=col.y_px,
                width_px=col.width_px,
                height_px=col.height_px,
                x_m=x_m,
                y_m=y_m,
                width_mm=col.width_mm,
                depth_mm=col.depth_mm,
                source=col.source,
                confidence=max(col.confidence, 0.88),
                status=ReviewStatus.PENDING,
                notes=f"Intersection-probed in {target_region.name}",
            )
        )

    return probed


def _parse_elevation_m(text: str) -> float | None:
    upper = text.upper()
    # Exclude architectural scale callouts like "SCALE: 1:100M" or "1:45M"
    if "SCALE" in upper or re.search(r"\b1\s*:\s*\d+", upper):
        return None
    m = ELEV_VALUE_RE.search(text)
    if m:
        try:
            return float(m.group(1).replace(" ", ""))
        except ValueError:
            return None
    m2 = ELEV_PREFIX_ONLY_RE.search(text)
    if m2:
        try:
            return float(m2.group(1).replace(" ", ""))
        except ValueError:
            return None
    return None


def _is_excluded_non_floor_datum(upper_text: str) -> bool:
    return any(ex in upper_text for ex in NON_FLOOR_DATUM_EXCLUDE)


def _is_structural_floor_label(upper_text: str) -> bool:
    if _is_excluded_non_floor_datum(upper_text):
        return False
    if any(kw in upper_text for kw in STRUCTURAL_FLOOR_KEYWORDS):
        return True
    return bool(NTH_FLOOR_RE.search(upper_text))


def _normalize_level_tag(name: str) -> str:
    upper = re.sub(r"\s+", " ", name.upper()).strip()
    m = LEVEL_TAG_RE.search(upper)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return upper


def extract_levels_from_elevation_or_section(
    pdf_path: Path,
    png_path: Path,
    page_info: PageInfo,
    region: PlanRegion | None = None,
    calibration: ScaleCalibration | None = None,
) -> tuple[list[DetectedLevel], list[str]]:
    """
    Extract ONLY structural floor-to-floor & roof framing levels from elevation/section sheets
    or a region-scoped elevation box:
      - Ignores ceiling, firewall, parapet, natural grade/NGL, road, sidewalk, sill, lintel,
        coping, ridge, eave, building height, and scale text.
      - Filters micro-step non-story slivers (< 1.50 m gap from an adjacent structural floor).
      - Sorts kept levels ascending by elevation_m and computes floor-to-floor story_height_m.
    """
    page_w = float(page_info.width_px)
    page_h = float(page_info.height_px)
    bounds = _region_bounds(page_info, region)
    bx0, by0, bx1, by1 = bounds
    bw = max(bx1 - bx0, 1.0)
    bh = max(by1 - by0, 1.0)
    region_id = region.region_id if region is not None else None

    if region is not None:
        top_limit = by0
        bottom_limit = by1
        min_datum_span = bw * 0.35
        pad_x = max(bw * 0.08, 36.0)
        pad_y = max(bh * 0.05, 18.0)
        text_x_lo, text_x_hi = bx0 - pad_x, bx1 + pad_x
        text_y_lo, text_y_hi = by0 - pad_y, by1 + pad_y
        right_elev_x = bx0 + bw * 0.45
    else:
        top_limit = page_h * 0.03
        bottom_limit = page_h * 0.88
        min_datum_span = page_w * 0.45
        text_x_lo, text_x_hi = 0.0, page_w
        text_y_lo, text_y_hi = 0.0, bottom_limit + page_h * 0.02
        right_elev_x = page_w * 0.45

    horiz_segs: list[tuple[float, float, float]] = []
    text_lines: list[dict] = []

    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_info.page_number - 1]
        rot_mat = page.rotation_matrix
        ox = float(page.rect.x0)
        oy = float(page.rect.y0)
        sx = page_w / max(float(page.rect.width), 1.0)
        sy = page_h / max(float(page.rect.height), 1.0)

        for drawing in page.get_drawings():
            rects, line_segs = _extract_rect_from_drawing(drawing, rot_mat, ox, oy, sx, sy)
            for x0, y0, x1, y1 in rects:
                if (y1 - y0) <= 3.5 and (x1 - x0) >= 6.0:
                    yc = (y0 + y1) * 0.5
                    if top_limit <= yc <= bottom_limit:
                        clipped = _clip_segment_to_bounds("y", yc, x0, x1, bounds) if region is not None else (x0, x1)
                        if clipped:
                            horiz_segs.append((yc, clipped[0], clipped[1]))
            for x1, y1, x2, y2 in line_segs:
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                length = hypot(dx, dy)
                if length >= 4.0 and dy <= max(2.5, length * 0.02):
                    yc = (y1 + y2) * 0.5
                    if top_limit <= yc <= bottom_limit:
                        seg_lo, seg_hi = min(x1, x2), max(x1, x2)
                        clipped = _clip_segment_to_bounds("y", yc, seg_lo, seg_hi, bounds) if region is not None else (seg_lo, seg_hi)
                        if clipped:
                            horiz_segs.append((yc, clipped[0], clipped[1]))

        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            block_full_upper = " ".join(
                str(s.get("text", "")).strip()
                for ln in block.get("lines", [])
                for s in ln.get("spans", [])
            ).upper()
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                raw = " ".join(str(s.get("text", "")).strip() for s in spans if str(s.get("text", "")).strip())
                text = re.sub(r"\s+", " ", raw).strip()
                if not text:
                    continue
                lr = pymupdf.Rect(line.get("bbox", (0, 0, 0, 0))) * rot_mat
                x0 = (float(lr.x0) - ox) * sx
                y0 = (float(lr.y0) - oy) * sy
                x1 = (float(lr.x1) - ox) * sx
                y1 = (float(lr.y1) - oy) * sy
                x0, x1 = min(x0, x1), max(x0, x1)
                y0, y1 = min(y0, y1), max(y0, y1)
                cx = (x0 + x1) * 0.5
                cy = (y0 + y1) * 0.5
                if not (text_x_lo <= cx <= text_x_hi and text_y_lo <= cy <= text_y_hi):
                    continue
                text_lines.append(
                    {
                        "text": text,
                        "upper": text.upper(),
                        "block_upper": block_full_upper,
                        "x0": x0,
                        "y0": y0,
                        "x1": x1,
                        "y1": y1,
                        "cx": cx,
                        "cy": cy,
                    }
                )
    finally:
        doc.close()

    # Detect stroked AutoCAD SHX fonts / scanned elevations with too few selectable PDF words
    total_selectable_words = sum(len(t["text"].split()) for t in text_lines)
    if total_selectable_words < 3:
        return [], [
            "No selectable PDF text found (stroked AutoCAD SHX font) — add levels manually below or upload the .dxf file."
        ]

    # Mark lines stacked immediately adjacent to a "CEILING" line (e.g. "fourth floor" + "ceiling line")
    ceiling_lines = [t for t in text_lines if "CEILING" in t["upper"]]
    for t in text_lines:
        t["is_ceiling_part"] = ("CEILING" in t["upper"]) or ("CEILING" in t["block_upper"])
        if not t["is_ceiling_part"]:
            for cln in ceiling_lines:
                if abs(t["cy"] - cln["cy"]) <= 24.0 and abs(t["cx"] - cln["cx"]) <= bw * 0.14:
                    t["is_ceiling_part"] = True
                    break

    datum_clusters = [
        cl
        for cl in _cluster_1d(horiz_segs, tol=6.0)
        if cl["span"] >= min_datum_span
    ]
    if not datum_clusters and png_path.exists():
        img = cv2.imread(str(png_path), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            if region is not None:
                fh, fw = img.shape[:2]
                rx0 = max(0, int(round(bx0)))
                ry0 = max(0, int(round(by0)))
                rx1 = min(fw, int(round(bx1)))
                ry1 = min(fh, int(round(by1)))
                crop_img = img[ry0:ry1, rx0:rx1]
                off_x, off_y = float(rx0), float(ry0)
            else:
                crop_img = img
                off_x, off_y = 0.0, 0.0
            if crop_img.size > 0:
                edges = cv2.Canny(cv2.GaussianBlur(crop_img, (3, 3), 0), 50, 150)
                raw_lines = cv2.HoughLinesP(
                    edges,
                    rho=1,
                    theta=np.pi / 180.0,
                    threshold=30,
                    minLineLength=max(20, int(bw * 0.04)),
                    maxLineGap=25,
                )
                r_segs: list[tuple[float, float, float]] = []
                if raw_lines is not None:
                    for ln in np.asarray(raw_lines).reshape(-1, 4):
                        lx1, ly1, lx2, ly2 = [float(v) for v in ln]
                        y_mid = (ly1 + ly2) * 0.5 + off_y
                        if abs(ly2 - ly1) <= 3.0 and top_limit <= y_mid <= bottom_limit:
                            r_segs.append((y_mid, min(lx1, lx2) + off_x, max(lx1, lx2) + off_x))
                datum_clusters = [
                    cl
                    for cl in _cluster_1d(r_segs, tol=7.0)
                    if cl["span"] >= min_datum_span
                ]

    all_structural_lines = [
        t
        for t in text_lines
        if not t["is_ceiling_part"] and _is_structural_floor_label(t["upper"])
    ]
    # If AutoCAD elevation uses short datum ticks instead of full-width lines, synthesize clusters from labels
    if not datum_clusters and all_structural_lines:
        seen_ys: list[float] = []
        for t in sorted(all_structural_lines, key=lambda item: -item["cy"]):
            if not any(abs(t["cy"] - sy) <= 22.0 for sy in seen_ys):
                seen_ys.append(t["cy"])
                datum_clusters.append({"coord": t["cy"], "span": min_datum_span})

    datum_clusters.sort(key=lambda c: -c["coord"])

    right_elev_lines = [
        (t, val)
        for t in text_lines
        if (val := _parse_elevation_m(t["text"])) is not None
    ]
    # Vertical / rotated millimeter dimension strings (e.g. 3000, 2800, 2700 -> 3.00m, 2.80m, 2.70m)
    mm_dim_lines: list[tuple[dict, float]] = []
    for t in text_lines:
        m_mm = MM_STORY_DIM_RE.match(t["text"])
        if m_mm:
            mm_val = float(m_mm.group(1))
            if 1500.0 <= mm_val <= 9500.0:
                mm_dim_lines.append((t, round(mm_val / 1000.0, 4)))

    candidates: list[DetectedLevel] = []
    global_warnings: list[str] = []
    y_match_tol = max(bh * (0.045 if region is not None else 0.032), 24.0 if region is not None else 28.0)

    for cl in datum_clusters:
        dy_coord = cl["coord"]

        nearby_all = [t for t in text_lines if abs(t["cy"] - dy_coord) <= y_match_tol]
        nearby_struct = [t for t in all_structural_lines if abs(t["cy"] - dy_coord) <= y_match_tol]

        # Must match a structural floor/roof framing label (never keep unlabeled or excluded datums)
        if not nearby_struct:
            continue

        closest_struct_dist = min(abs(t["cy"] - dy_coord) for t in nearby_struct)
        excluded_closer = [
            t
            for t in nearby_all
            if (not t["is_ceiling_part"])
            and _is_excluded_non_floor_datum(t["upper"])
            and closest_struct_dist > 20.0
            and abs(t["cy"] - dy_coord) + 2.0 < closest_struct_dist
        ]
        if excluded_closer:
            continue

        nearby_struct.sort(key=lambda t: (abs(t["cy"] - dy_coord), t["y0"]))
        chosen_kw: list[dict] = []
        for t in nearby_struct:
            if not chosen_kw:
                chosen_kw.append(t)
            elif abs(t["cy"] - chosen_kw[0]["cy"]) <= 20.0 and abs(t["x0"] - chosen_kw[0]["x0"]) <= bw * 0.22:
                chosen_kw.append(t)
        chosen_kw.sort(key=lambda t: (t["y0"], t["x0"]))
        name = " ".join(t["text"] for t in chosen_kw).strip()
        name_clean = re.sub(r"\s*[+-]?\d+(?:\.\d+)?\s*(?:m|M|mts?\.?)\b.*$", "", name).strip(" -:|") or name
        if _is_excluded_non_floor_datum(name_clean.upper()):
            continue

        nearby_elev_right = [
            (t, v)
            for t, v in right_elev_lines
            if abs(t["cy"] - dy_coord) <= y_match_tol and t["cx"] >= right_elev_x
        ]
        nearby_elev_any = [
            (t, v) for t, v in right_elev_lines if abs(t["cy"] - dy_coord) <= y_match_tol
        ]
        elev_candidates = nearby_elev_right if nearby_elev_right else nearby_elev_any
        elev_candidates.sort(key=lambda item: abs(item[0]["cy"] - dy_coord))

        item_warnings: list[str] = []
        if elev_candidates:
            elevation_m = round(elev_candidates[0][1], 4)
            conf = 0.92
        else:
            elevation_m = 0.0
            conf = 0.55
            item_warnings.append("No elevation value matched near datum line; verify elevation_m.")

        candidates.append(
            DetectedLevel(
                level_id=_new_id("lvl"),
                name=name_clean,
                elevation_m=elevation_m,
                story_height_m=None,
                page_number=page_info.page_number,
                region_id=region_id,
                y_px=round(dy_coord, 2),
                confidence=conf,
                review_status=ReviewStatus.PENDING,
                engineer_approved=False,
                warnings=item_warnings,
            )
        )

    # Fallback: pair adjacent datum lines with nearby vertical/rotated mm dimensions (e.g. 3000, 2800, 2700)
    # when datum labels have no inline EL. +X.XXm values
    if len(candidates) >= 2 and mm_dim_lines and all(c.confidence < 0.80 for c in candidates):
        candidates.sort(key=lambda l: -l.y_px)  # bottom-to-top (Ground Floor first)
        used_dim_ids: set[int] = set()
        cum_elev = 0.0
        candidates[0].elevation_m = 0.0
        candidates[0].confidence = 0.85
        candidates[0].warnings = []
        for idx in range(1, len(candidates)):
            low_y = candidates[idx - 1].y_px
            high_y = candidates[idx].y_px
            mid_y = 0.5 * (low_y + high_y)
            between = [
                (di, t, m_val)
                for di, (t, m_val) in enumerate(mm_dim_lines)
                if di not in used_dim_ids and (high_y - 12.0) <= t["cy"] <= (low_y + 12.0)
            ]
            if between:
                between.sort(key=lambda item: abs(item[1]["cy"] - mid_y))
                di_best, _, story_m = between[0]
                used_dim_ids.add(di_best)
                cum_elev = round(cum_elev + story_m, 4)
                candidates[idx].elevation_m = cum_elev
                candidates[idx].confidence = 0.85
                candidates[idx].warnings = []
            else:
                cum_elev = round(cum_elev + 3.0, 4)
                candidates[idx].elevation_m = cum_elev
                candidates[idx].confidence = 0.60
                candidates[idx].warnings = [
                    "No millimeter dimension matched between datums; defaulted +3.00 m — verify."
                ]
    elif (
        len(candidates) >= 2
        and all(c.confidence < 0.80 for c in candidates)
        and calibration is not None
        and calibration.confirmed
        and calibration.pixels_per_m > 0
    ):
        # Confirmed ScaleCalibration fallback: derive elevations from vertical pixel spacing
        candidates.sort(key=lambda l: -l.y_px)
        base_y = candidates[0].y_px
        for idx, cand in enumerate(candidates):
            cand.elevation_m = round((base_y - cand.y_px) / calibration.pixels_per_m, 3) if idx > 0 else 0.0
            cand.confidence = 0.82
            cand.warnings = []

    # Sort ascending by elevation_m (tie-break bottom-to-top by descending y_px)
    candidates.sort(key=lambda l: (l.elevation_m, -l.y_px))

    # Filter out micro-step non-story slivers (gap < 1.50 m from adjacent kept structural floor)
    levels: list[DetectedLevel] = []
    for cand in candidates:
        if not levels:
            levels.append(cand)
            continue
        gap = cand.elevation_m - levels[-1].elevation_m
        if gap < MIN_STORY_STEP_M:
            # Prefer higher-confidence or primary "FLOOR"/"ROOF DECK" label over a secondary sliver
            prev = levels[-1]
            cand_prio = (cand.confidence, 1 if ("FLOOR" in cand.name.upper() or "ROOF DECK" in cand.name.upper()) else 0)
            prev_prio = (prev.confidence, 1 if ("FLOOR" in prev.name.upper() or "ROOF DECK" in prev.name.upper()) else 0)
            if cand_prio > prev_prio:
                levels[-1] = cand
            continue
        levels.append(cand)

    # Check duplicate level tags across kept structural floor levels
    tag_counts: dict[str, int] = {}
    for lvl in levels:
        tag = _normalize_level_tag(lvl.name)
        tag_counts[tag] = tag_counts.get(tag, 0) + 1
    for lvl in levels:
        tag = _normalize_level_tag(lvl.name)
        if tag_counts.get(tag, 0) > 1:
            msg = f"Duplicate level label detected: '{tag}'."
            if msg not in lvl.warnings:
                lvl.warnings.append(msg)
            if msg not in global_warnings:
                global_warnings.append(msg)

    # Strictly compute floor-to-floor story_height_m between consecutive kept structural levels
    for idx, lvl in enumerate(levels):
        if idx == 0:
            lvl.story_height_m = None
        else:
            diff = round(lvl.elevation_m - levels[idx - 1].elevation_m, 4)
            lvl.story_height_m = diff
            if diff > 10.0:
                msg = f"Unusually large story height ({diff:.2f} m) above '{levels[idx - 1].name}' — verify elevations."
                if msg not in lvl.warnings:
                    lvl.warnings.append(msg)
                if msg not in global_warnings:
                    global_warnings.append(msg)

    return levels, global_warnings


# Backward-compatible aliases
extract_vector = detect_vector_elements
extract_raster = detect_raster_elements


class ExperimentalVLMDetector:
    """
    Experimental Vision-Language Model (VLM) detector placeholder interface.

    Intentionally does not invoke external APIs or fabricate structural geometry.
    """

    provider_name: str = "unconfigured"
    model_name: str = "placeholder"
    is_configured: bool = False

    def detect(
        self,
        png_path: Path,
        page_info: PageInfo,
        calibration: ScaleCalibration | None = None,
    ) -> DetectionOutput:
        _ = (png_path, page_info, calibration)
        return DetectionOutput(
            method_used="vlm_placeholder",
            grids=[],
            columns=[],
            notice=(
                "Experimental VLM detection is a placeholder interface only "
                "(NOT_CONFIGURED). No vision model was invoked and no structural "
                "elements were fabricated. Use Vector or Raster extraction, or place columns manually."
            ),
        )


def run_detection(
    pdf_path: Path,
    png_path: Path,
    page_info: PageInfo,
    calibration: ScaleCalibration | None = None,
    method: DetectionMethod = "auto",
    options: ExtractionOptions | None = None,
    region: PlanRegion | None = None,
    existing_grids: list[GridCandidate] | None = None,
    existing_columns: list[ColumnCandidate] | None = None,
    openings: list[DetectedOpening] | None = None,
    pixels_per_meter: float | None = None,
    origin_px: PointPx | None = None,
    exclude_regions: list[PlanRegion] | None = None,
) -> DetectionOutput:
    """Dispatch page detection across vector (PyMuPDF), raster (OpenCV), and VLM placeholder."""
    opts = options if options is not None else ExtractionOptions()
    if method == "vlm":
        return ExperimentalVLMDetector().detect(png_path, page_info, calibration)
    if method == "vector":
        return detect_vector_elements(
            pdf_path,
            page_info,
            calibration,
            options=opts,
            region=region,
            png_path=png_path,
            existing_grids=existing_grids,
            existing_columns=existing_columns,
            openings=openings,
            pixels_per_meter=pixels_per_meter,
            origin_px=origin_px,
            exclude_regions=exclude_regions,
        )
    if method == "raster":
        return detect_raster_elements(
            png_path,
            page_info,
            calibration,
            options=opts,
            region=region,
            existing_grids=existing_grids,
            pixels_per_meter=pixels_per_meter,
            origin_px=origin_px,
            exclude_regions=exclude_regions,
        )

    if page_info.classification == "vector":
        result = detect_vector_elements(
            pdf_path,
            page_info,
            calibration,
            options=opts,
            region=region,
            png_path=png_path,
            existing_grids=existing_grids,
            existing_columns=existing_columns,
            openings=openings,
            pixels_per_meter=pixels_per_meter,
            origin_px=origin_px,
            exclude_regions=exclude_regions,
        )
        if result.grids or result.columns or result.beams:
            return result
        fallback = detect_raster_elements(
            png_path,
            page_info,
            calibration,
            options=opts,
            region=region,
            existing_grids=existing_grids,
            pixels_per_meter=pixels_per_meter,
            origin_px=origin_px,
            exclude_regions=exclude_regions,
        )
        fallback.method_used = "raster_fallback"
        return fallback

    if page_info.classification == "hybrid":
        vec = detect_vector_elements(
            pdf_path,
            page_info,
            calibration,
            options=opts,
            region=region,
            png_path=png_path,
            existing_grids=existing_grids,
            existing_columns=existing_columns,
            openings=openings,
            pixels_per_meter=pixels_per_meter,
            origin_px=origin_px,
            exclude_regions=exclude_regions,
        )
        if vec.grids or vec.columns or vec.beams:
            return vec
        ras = detect_raster_elements(
            png_path,
            page_info,
            calibration,
            options=opts,
            region=region,
            existing_grids=existing_grids,
            pixels_per_meter=pixels_per_meter,
            origin_px=origin_px,
            exclude_regions=exclude_regions,
        )
        ras.method_used = "raster_fallback"
        return ras

    return detect_raster_elements(
        png_path,
        page_info,
        calibration,
        options=opts,
        region=region,
        existing_grids=existing_grids,
        pixels_per_meter=pixels_per_meter,
        origin_px=origin_px,
        exclude_regions=exclude_regions,
    )
