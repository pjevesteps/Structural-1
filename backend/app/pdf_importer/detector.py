from dataclasses import dataclass
from math import hypot
from pathlib import Path
from uuid import uuid4
import re

import cv2
import numpy as np
import pymupdf

from .inspector import px_to_plan_m
from .schemas import (
    ColumnCandidate,
    DetectedLevel,
    DetectionMethod,
    ExtractionOptions,
    GridCandidate,
    PageInfo,
    PlanRegion,
    ReviewStatus,
    ScaleCalibration,
)

LABEL_PATTERN = re.compile(r"^[A-Za-z0-9]{1,3}$")

LEVEL_KEYWORDS = (
    "BASEMENT",
    "NATURAL GRADE",
    "NGL",
    "GROUND FLOOR",
    "FIRST FLOOR",
    "SECOND FLOOR",
    "THIRD FLOOR",
    "FOURTH FLOOR",
    "FIFTH FLOOR",
    "SIXTH FLOOR",
    "ROOF DECK",
    "ROOF LINE",
    "ROOF BEAM",
    "FIREWALL",
    "PARAPET",
    "APEX",
    "FINISHED FLOOR",
    "FFL",
    "LEVEL",
)

ELEV_VALUE_RE = re.compile(
    r"(?:(?:EL\.?|ELEV\.?|FFL\.?)\s*)?([+-]?\s*\d+(?:\.\d+)?)\s*(?:m|M|mts?\.?|meters?)\b",
    re.IGNORECASE,
)
ELEV_PREFIX_ONLY_RE = re.compile(
    r"(?:EL\.?|ELEV\.?)\s*([+-]?\s*\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
LEVEL_TAG_RE = re.compile(
    r"\b(LEVEL\s*\d+|BASEMENT(?:\s*\d+)?|NATURAL\s+GRADE|GROUND\s+FLOOR|"
    r"FIRST\s+FLOOR|SECOND\s+FLOOR|THIRD\s+FLOOR|FOURTH\s+FLOOR|FIFTH\s+FLOOR|"
    r"ROOF\s+DECK|ROOF\s+LINE|FIREWALL)\b",
    re.IGNORECASE,
)


@dataclass
class DetectionOutput:
    method_used: str
    grids: list[GridCandidate]
    columns: list[ColumnCandidate]
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
) -> tuple[float, float]:
    if calibration is None:
        return 400.0, 400.0
    w_mm = width_px * calibration.meters_per_px * 1000.0
    d_mm = height_px * calibration.meters_per_px * 1000.0
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
    endpoint_radius_px: float = 48.0,
) -> str | None:
    """Match a short (1-3 char alphanumeric) grid label near either endpoint of a grid line."""
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


def _extract_rect_from_drawing(drawing: dict, ox: float, oy: float, sx: float, sy: float):
    """Extract axis-aligned rectangle bounds in px if a drawing item represents a closed 4-edge box."""
    items = drawing.get("items", [])
    rects: list[tuple[float, float, float, float]] = []
    line_segs: list[tuple[float, float, float, float]] = []

    for item in items:
        op = item[0]
        if op == "re":
            r = item[1]
            x0 = (float(r.x0) - ox) * sx
            y0 = (float(r.y0) - oy) * sy
            x1 = (float(r.x1) - ox) * sx
            y1 = (float(r.y1) - oy) * sy
            rects.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
        elif op == "qu":
            q = item[1]
            xs = [(float(p.x) - ox) * sx for p in (q.ul, q.ur, q.ll, q.lr)]
            ys = [(float(p.y) - oy) * sy for p in (q.ul, q.ur, q.ll, q.lr)]
            rects.append((min(xs), min(ys), max(xs), max(ys)))
        elif op == "l":
            p1, p2 = item[1], item[2]
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


def detect_vector_elements(
    pdf_path: Path,
    page_info: PageInfo,
    calibration: ScaleCalibration | None = None,
    options: ExtractionOptions | None = None,
    region: PlanRegion | None = None,
) -> DetectionOutput:
    """Extract grid lines (including merged dashed segments + endpoint labels) and columns via PyMuPDF."""
    opts = options if options is not None else ExtractionOptions()
    bounds = _region_bounds(page_info, region)
    bx0, by0, bx1, by1 = bounds
    bw = max(bx1 - bx0, 1.0)
    bh = max(by1 - by0, 1.0)
    region_id = region.region_id if region is not None else None

    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_info.page_number - 1]
        ox = float(page.rect.x0)
        oy = float(page.rect.y0)
        sx = float(page_info.width_px) / max(float(page.rect.width), 1.0)
        sy = float(page_info.height_px) / max(float(page.rect.height), 1.0)

        min_dim = min(bw, bh)
        min_v_span = bh * opts.min_span_ratio
        min_h_span = bw * opts.min_span_ratio
        min_col_px = 5.0
        max_col_px = min_dim * 0.14
        min_seg_len = 2.5 if opts.merge_dashed_segments else 8.0

        vert_segs: list[tuple[float, float, float]] = []
        horiz_segs: list[tuple[float, float, float]] = []
        raw_boxes: list[dict] = []

        for drawing in page.get_drawings():
            is_filled = drawing.get("fill") is not None
            rects, line_segs = _extract_rect_from_drawing(drawing, ox, oy, sx, sy)

            for x0, y0, x1, y1 in rects:
                w = x1 - x0
                h = y1 - y0
                if w <= 3.5 and h >= min_seg_len:
                    xc = (x0 + x1) * 0.5
                    clipped = _clip_segment_to_bounds("x", xc, y0, y1, bounds)
                    if clipped and not _in_outer_border_margin("x", xc, clipped[0], clipped[1], bounds, opts.border_margin_ratio):
                        vert_segs.append((xc, clipped[0], clipped[1]))
                elif h <= 3.5 and w >= min_seg_len:
                    yc = (y0 + y1) * 0.5
                    clipped = _clip_segment_to_bounds("y", yc, x0, x1, bounds)
                    if clipped and not _in_outer_border_margin("y", yc, clipped[0], clipped[1], bounds, opts.border_margin_ratio):
                        horiz_segs.append((yc, clipped[0], clipped[1]))
                elif min_col_px <= w <= max_col_px and min_col_px <= h <= max_col_px:
                    cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
                    if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
                        continue
                    if _in_outer_border_margin("x", cx, y0, y1, bounds, opts.border_margin_ratio):
                        continue
                    ratio = w / max(h, 1e-6)
                    if 0.25 <= ratio <= 4.0:
                        raw_boxes.append({"cx": cx, "cy": cy, "w": w, "h": h, "filled": is_filled})

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
                        if not _in_outer_border_margin("x", xc, clipped[0], clipped[1], bounds, opts.border_margin_ratio):
                            vert_segs.append((xc, clipped[0], clipped[1]))
                elif dy <= max(2.0, length * 0.025):
                    yc = (y1 + y2) * 0.5
                    clipped = _clip_segment_to_bounds("y", yc, x1, x2, bounds)
                    if clipped and (clipped[1] - clipped[0]) >= min_seg_len:
                        if not _in_outer_border_margin("y", yc, clipped[0], clipped[1], bounds, opts.border_margin_ratio):
                            horiz_segs.append((yc, clipped[0], clipped[1]))

        words: list[tuple[int, float, float, str]] = []
        if opts.detect_grid_labels:
            pad = 50.0
            for idx_w, w_item in enumerate(page.get_text("words")):
                wx0, wy0, wx1, wy1, text = w_item[:5]
                wx = ((float(wx0) + float(wx1)) * 0.5 - ox) * sx
                wy = ((float(wy0) + float(wy1)) * 0.5 - oy) * sy
                if bx0 - pad <= wx <= bx1 + pad and by0 - pad <= wy <= by1 + pad:
                    words.append((idx_w, wx, wy, str(text)))

        vert_clusters = _cluster_1d(vert_segs, tol=opts.cluster_tolerance_px)
        horiz_clusters = _cluster_1d(horiz_segs, tol=opts.cluster_tolerance_px)

        grids: list[GridCandidate] = []
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
                if opts.detect_grid_labels
                else None
            )
            if not label or label in used_x_labels:
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
            grids.append(
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
                    confidence=0.92 if is_solid else 0.85,
                    status=ReviewStatus.PENDING,
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
                if opts.detect_grid_labels
                else None
            )
            if not label or label in used_y_labels:
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
                    source="vector",
                    confidence=0.92 if is_solid else 0.85,
                    status=ReviewStatus.PENDING,
                )
            )

        deduped = _deduplicate_boxes(raw_boxes)
        deduped.sort(key=lambda b: (-round(b["cy"] / 10.0), b["cx"]))

        columns: list[ColumnCandidate] = []
        for idx, box in enumerate(deduped, start=1):
            tol = max(max(box["w"], box["h"]) * 0.9, 14.0)
            grid_ref, on_intersection = _assign_grid_ref(box["cx"], box["cy"], grids, tol)
            x_m, y_m = (None, None)
            if calibration is not None:
                x_m, y_m = px_to_plan_m(box["cx"], box["cy"], calibration)
            w_mm, d_mm = _estimate_column_dims_mm(box["w"], box["h"], calibration)

            if box["filled"] and on_intersection:
                conf = 0.93
            elif on_intersection:
                conf = 0.85
            elif box["filled"]:
                conf = 0.78
            else:
                conf = 0.65

            columns.append(
                ColumnCandidate(
                    id=_new_id("col"),
                    page_number=page_info.page_number,
                    region_id=region_id,
                    column_id=f"C{idx}",
                    grid=grid_ref,
                    x_px=round(box["cx"], 2),
                    y_px=round(box["cy"], 2),
                    width_px=round(box["w"], 2),
                    height_px=round(box["h"], 2),
                    x_m=x_m,
                    y_m=y_m,
                    width_mm=w_mm,
                    depth_mm=d_mm,
                    source="vector",
                    confidence=conf,
                    status=ReviewStatus.PENDING,
                )
            )

        scope_label = f" in region '{region.name}'" if region is not None else ""
        return DetectionOutput(
            method_used="vector",
            grids=grids,
            columns=columns,
            notice=(
                f"Vector extraction{scope_label} found {len(grids)} grid line(s) and {len(columns)} column candidate(s). "
                "Engineer review is required before transfer."
            ),
        )
    finally:
        doc.close()


def detect_raster_elements(
    png_path: Path,
    page_info: PageInfo,
    calibration: ScaleCalibration | None = None,
    options: ExtractionOptions | None = None,
    region: PlanRegion | None = None,
) -> DetectionOutput:
    """OpenCV raster fallback detector with border margin exclusion and colinear dashed-segment merging."""
    opts = options if options is not None else ExtractionOptions()
    full_img = cv2.imread(str(png_path), cv2.IMREAD_GRAYSCALE)
    if full_img is None:
        raise FileNotFoundError(f"Rendered page image not found: {png_path}")

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

    grids: list[GridCandidate] = []
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

    _, binary = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    min_col_px = 6.0
    max_col_px = min_dim * 0.14
    raw_boxes: list[dict] = []

    base_k = max(7, int(min_dim * 0.009) | 1)
    for k_size in (base_k, max(5, base_k - 2)):
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k_size, k_size))
        opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
        contours, _ = cv2.findContours(opened, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_col_px * min_col_px * 0.6:
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            wf, hf = float(w), float(h)
            if not (min_col_px <= wf <= max_col_px and min_col_px <= hf <= max_col_px):
                continue
            cx, cy = offset_x + x + wf * 0.5, offset_y + y + hf * 0.5
            if _in_outer_border_margin("x", cx, offset_y + float(y), offset_y + float(y + h), bounds, opts.border_margin_ratio):
                continue
            ratio = wf / max(hf, 1e-6)
            if not (0.25 <= ratio <= 4.0):
                continue
            rectangularity = area / max(wf * hf, 1.0)
            if rectangularity < 0.62:
                continue
            raw_boxes.append({"cx": cx, "cy": cy, "w": wf, "h": hf, "filled": True})
        if raw_boxes:
            break

    deduped = _deduplicate_boxes(raw_boxes)
    deduped.sort(key=lambda b: (-round(b["cy"] / 10.0), b["cx"]))

    columns: list[ColumnCandidate] = []
    for idx, box in enumerate(deduped, start=1):
        tol = max(max(box["w"], box["h"]) * 1.1, 18.0)
        grid_ref, on_intersection = _assign_grid_ref(box["cx"], box["cy"], grids, tol)
        x_m, y_m = (None, None)
        if calibration is not None:
            x_m, y_m = px_to_plan_m(box["cx"], box["cy"], calibration)
        w_mm, d_mm = _estimate_column_dims_mm(box["w"], box["h"], calibration)

        columns.append(
            ColumnCandidate(
                id=_new_id("col"),
                page_number=page_info.page_number,
                region_id=region_id,
                column_id=f"C{idx}",
                grid=grid_ref,
                x_px=round(box["cx"], 2),
                y_px=round(box["cy"], 2),
                width_px=round(box["w"], 2),
                height_px=round(box["h"], 2),
                x_m=x_m,
                y_m=y_m,
                width_mm=w_mm,
                depth_mm=d_mm,
                source="raster",
                confidence=0.76 if on_intersection else 0.62,
                status=ReviewStatus.PENDING,
            )
        )

    return DetectionOutput(
        method_used="raster",
        grids=grids,
        columns=columns,
        notice=(
            f"OpenCV raster extraction found {len(grids)} grid line(s) and {len(columns)} column candidate(s). "
            "Raster detections are approximate and require engineer verification."
        ),
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
    Intersection-gated column probing inside `target_region` using propagated master grid lines.
    Only keeps candidate columns near (x_grid, y_grid) intersections inside `target_region`.
    """
    x_grids = [g for g in target_grids if g.axis == "x"]
    y_grids = [g for g in target_grids if g.axis == "y"]
    if not x_grids or not y_grids:
        return []

    raw_output = (
        detect_vector_elements(pdf_path, page_info, calibration=calibration, region=target_region)
        if page_info.classification in ("vector", "hybrid")
        else detect_raster_elements(png_path, page_info, calibration=calibration, region=target_region)
    )
    if not raw_output.columns and page_info.classification == "hybrid":
        raw_output = detect_raster_elements(png_path, page_info, calibration=calibration, region=target_region)

    bx0, by0, bx1, by1 = _region_bounds(page_info, target_region)
    probed: list[ColumnCandidate] = []
    matched_intersections: set[tuple[str, str]] = set()

    for col in raw_output.columns:
        if not (bx0 <= col.x_px <= bx1 and by0 <= col.y_px <= by1):
            continue
        tol = max(max(col.width_px, col.height_px) * 1.15, 24.0)
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
                grid=f"{gy.label}-{gx.label}",
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


def _contains_level_keyword(upper_text: str) -> bool:
    return any(kw in upper_text for kw in LEVEL_KEYWORDS)


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
) -> tuple[list[DetectedLevel], list[str]]:
    """
    Extract floor level datum lines (>= 45% width above bottom title block), match left-side
    level keywords and right-side elevation values, compute story heights, and flag warnings
    for duplicate level tags or inconsistent elevations.
    """
    page_w = float(page_info.width_px)
    page_h = float(page_info.height_px)
    top_limit = page_h * 0.03
    bottom_limit = page_h * 0.88
    min_datum_span = page_w * 0.45

    horiz_segs: list[tuple[float, float, float]] = []
    text_lines: list[dict] = []

    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_info.page_number - 1]
        ox = float(page.rect.x0)
        oy = float(page.rect.y0)
        sx = page_w / max(float(page.rect.width), 1.0)
        sy = page_h / max(float(page.rect.height), 1.0)

        for drawing in page.get_drawings():
            rects, line_segs = _extract_rect_from_drawing(drawing, ox, oy, sx, sy)
            for x0, y0, x1, y1 in rects:
                if (y1 - y0) <= 3.5 and (x1 - x0) >= 6.0:
                    yc = (y0 + y1) * 0.5
                    if top_limit <= yc <= bottom_limit:
                        horiz_segs.append((yc, x0, x1))
            for x1, y1, x2, y2 in line_segs:
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                length = hypot(dx, dy)
                if length >= 4.0 and dy <= max(2.5, length * 0.02):
                    yc = (y1 + y2) * 0.5
                    if top_limit <= yc <= bottom_limit:
                        horiz_segs.append((yc, min(x1, x2), max(x1, x2)))

        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                raw = " ".join(str(s.get("text", "")).strip() for s in spans if str(s.get("text", "")).strip())
                text = re.sub(r"\s+", " ", raw).strip()
                if not text:
                    continue
                bx0, by0, bx1, by1 = line.get("bbox", (0, 0, 0, 0))
                x0 = (float(bx0) - ox) * sx
                y0 = (float(by0) - oy) * sy
                x1 = (float(bx1) - ox) * sx
                y1 = (float(by1) - oy) * sy
                cy = (y0 + y1) * 0.5
                if cy > bottom_limit + page_h * 0.02:
                    continue
                text_lines.append(
                    {
                        "text": text,
                        "upper": text.upper(),
                        "x0": x0,
                        "y0": y0,
                        "x1": x1,
                        "y1": y1,
                        "cx": (x0 + x1) * 0.5,
                        "cy": cy,
                    }
                )
    finally:
        doc.close()

    # Fallback to OpenCV horizontal lines if vector segments did not yield datum lines
    datum_clusters = [
        cl
        for cl in _cluster_1d(horiz_segs, tol=6.0)
        if cl["span"] >= min_datum_span
    ]
    if not datum_clusters and png_path.exists():
        img = cv2.imread(str(png_path), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            edges = cv2.Canny(cv2.GaussianBlur(img, (3, 3), 0), 50, 150)
            raw_lines = cv2.HoughLinesP(
                edges,
                rho=1,
                theta=np.pi / 180.0,
                threshold=30,
                minLineLength=max(20, int(page_w * 0.04)),
                maxLineGap=25,
            )
            r_segs: list[tuple[float, float, float]] = []
            if raw_lines is not None:
                for ln in np.asarray(raw_lines).reshape(-1, 4):
                    x1, y1, x2, y2 = [float(v) for v in ln]
                    if abs(y2 - y1) <= 3.0 and top_limit <= (y1 + y2) * 0.5 <= bottom_limit:
                        r_segs.append(((y1 + y2) * 0.5, min(x1, x2), max(x1, x2)))
            datum_clusters = [
                cl
                for cl in _cluster_1d(r_segs, tol=7.0)
                if cl["span"] >= min_datum_span
            ]

    # Sort datum lines from bottom of sheet to top (descending y_px)
    datum_clusters.sort(key=lambda c: -c["coord"])

    # Partition text lines into keyword lines (left) and elevation lines (right)
    left_keyword_lines = [
        t for t in text_lines if _contains_level_keyword(t["upper"]) and t["cx"] <= page_w * 0.58
    ]
    right_elev_lines = [
        (t, val)
        for t in text_lines
        if (val := _parse_elevation_m(t["text"])) is not None
    ]

    levels: list[DetectedLevel] = []
    global_warnings: list[str] = []
    y_match_tol = max(page_h * 0.04, 34.0)

    # Match each datum line to nearest left-side level keyword text and right-side elevation value
    for cl in datum_clusters:
        dy_coord = cl["coord"]
        nearby_kw = [t for t in left_keyword_lines if abs(t["cy"] - dy_coord) <= y_match_tol]
        # Prefer right-side elevation values first, then any side near this datum line
        nearby_elev_right = [
            (t, v) for t, v in right_elev_lines if abs(t["cy"] - dy_coord) <= y_match_tol and t["cx"] >= page_w * 0.45
        ]
        nearby_elev_any = [
            (t, v) for t, v in right_elev_lines if abs(t["cy"] - dy_coord) <= y_match_tol
        ]

        if not nearby_kw and not nearby_elev_any:
            continue

        # Build level name by combining closely stacked keyword lines on the left side
        nearby_kw.sort(key=lambda t: (abs(t["cy"] - dy_coord), t["y0"]))
        chosen_kw: list[dict] = []
        for t in nearby_kw:
            if not chosen_kw:
                chosen_kw.append(t)
            elif abs(t["cy"] - chosen_kw[0]["cy"]) <= 22.0 and abs(t["x0"] - chosen_kw[0]["x0"]) <= page_w * 0.20:
                chosen_kw.append(t)
        chosen_kw.sort(key=lambda t: (t["y0"], t["x0"]))
        name = " ".join(t["text"] for t in chosen_kw).strip() if chosen_kw else f"LEVEL @ y={round(dy_coord)}"
        # Strip trailing elevation string if embedded in the same text block
        name_clean = re.sub(r"\s*[+-]?\d+(?:\.\d+)?\s*(?:m|M|mts?\.?)\b.*$", "", name).strip(" -:|") or name

        elev_candidates = nearby_elev_right if nearby_elev_right else nearby_elev_any
        elev_candidates.sort(key=lambda item: abs(item[0]["cy"] - dy_coord))
        item_warnings: list[str] = []
        if elev_candidates:
            elevation_m = round(elev_candidates[0][1], 4)
            conf = 0.90 if chosen_kw else 0.75
        else:
            elevation_m = 0.0
            conf = 0.55
            item_warnings.append("No elevation value matched near datum line; verify elevation_m.")

        levels.append(
            DetectedLevel(
                level_id=_new_id("lvl"),
                name=name_clean,
                elevation_m=elevation_m,
                story_height_m=None,
                page_number=page_info.page_number,
                y_px=round(dy_coord, 2),
                confidence=conf,
                review_status=ReviewStatus.PENDING,
                engineer_approved=False,
                warnings=item_warnings,
            )
        )

    # Check duplicate level tags/names across detected levels
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

    # Compute story heights between adjacent levels (ordered bottom-to-top by y_px descending)
    for idx in range(1, len(levels)):
        prev_lvl = levels[idx - 1]
        curr_lvl = levels[idx]
        diff = round(curr_lvl.elevation_m - prev_lvl.elevation_m, 4)
        curr_lvl.story_height_m = diff

        if diff <= 0.0:
            msg = (
                f"Inconsistent elevation difference ({diff:+.2f} m) between "
                f"'{prev_lvl.name}' ({prev_lvl.elevation_m} m) and '{curr_lvl.name}' ({curr_lvl.elevation_m} m)."
            )
            if msg not in curr_lvl.warnings:
                curr_lvl.warnings.append(msg)
            if msg not in global_warnings:
                global_warnings.append(msg)
        elif diff > 10.0:
            msg = f"Unusually large story height ({diff:.2f} m) above '{prev_lvl.name}' — verify elevations."
            if msg not in curr_lvl.warnings:
                curr_lvl.warnings.append(msg)
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
) -> DetectionOutput:
    """Dispatch page detection across vector (PyMuPDF), raster (OpenCV), and VLM placeholder."""
    opts = options if options is not None else ExtractionOptions()
    if method == "vlm":
        return ExperimentalVLMDetector().detect(png_path, page_info, calibration)
    if method == "vector":
        return detect_vector_elements(pdf_path, page_info, calibration, options=opts, region=region)
    if method == "raster":
        return detect_raster_elements(png_path, page_info, calibration, options=opts, region=region)

    if page_info.classification == "vector":
        result = detect_vector_elements(pdf_path, page_info, calibration, options=opts, region=region)
        if result.grids or result.columns:
            return result
        fallback = detect_raster_elements(png_path, page_info, calibration, options=opts, region=region)
        fallback.method_used = "raster_fallback"
        return fallback

    if page_info.classification == "hybrid":
        vec = detect_vector_elements(pdf_path, page_info, calibration, options=opts, region=region)
        if vec.grids or vec.columns:
            return vec
        ras = detect_raster_elements(png_path, page_info, calibration, options=opts, region=region)
        ras.method_used = "raster_fallback"
        return ras

    return detect_raster_elements(png_path, page_info, calibration, options=opts, region=region)
