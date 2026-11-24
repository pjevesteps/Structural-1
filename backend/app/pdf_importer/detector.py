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
    DetectionMethod,
    GridCandidate,
    PageInfo,
    ScaleCalibration,
)

LABEL_PATTERN = re.compile(r"^[A-Za-z0-9]{1,4}$")


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
    # Guard against line-thickness artifacts if detected box is unrealistically tiny/huge
    if 120.0 <= w_mm <= 3000.0 and 120.0 <= d_mm <= 3000.0:
        return _round_mm(w_mm), _round_mm(d_mm)
    return 400.0, 400.0


def _cluster_1d(items: list[tuple[float, float, float]], tol: float) -> list[dict]:
    """
    Cluster segments by primary coordinate.
    Each item is (coord, span_start, span_end) where span_start <= span_end.
    Returns list of dicts with avg coord, min_span, max_span, total_len, count.
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


def _find_grid_label(
    axis: str,
    coord: float,
    span_min: float,
    span_max: float,
    words: list[tuple[float, float, str]],
    max_perp_dist: float = 28.0,
    max_end_dist: float = 65.0,
) -> str | None:
    """Match a short grid bubble label near either endpoint of a detected grid line."""
    best_label: str | None = None
    best_score = float("inf")

    for wx, wy, text in words:
        cleaned = text.strip()
        if not LABEL_PATTERN.match(cleaned):
            continue
        if axis == "x":
            perp = abs(wx - coord)
            end_dist = min(abs(wy - span_min), abs(wy - span_max))
            inside = span_min - 5.0 <= wy <= span_max + 5.0
        else:
            perp = abs(wy - coord)
            end_dist = min(abs(wx - span_min), abs(wx - span_max))
            inside = span_min - 5.0 <= wx <= span_max + 5.0

        if perp > max_perp_dist or end_dist > max_end_dist:
            continue
        # Prefer labels just outside endpoints rather than deep inside the drawing
        penalty = 15.0 if inside and end_dist > 15.0 else 0.0
        score = perp * 1.5 + end_dist + penalty
        if score < best_score:
            best_score = score
            best_label = cleaned

    return best_label


def _deduplicate_boxes(
    boxes: list[dict],
) -> list[dict]:
    """Merge overlapping/concentric rectangle detections (e.g. stroke + fill of same column)."""
    if not boxes:
        return []
    # Prefer filled boxes first so fill metadata is preserved
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

    # Detect closed 4-line axis-aligned rectangle paths (common in CAD exports)
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


def extract_vector(
    pdf_path: Path,
    page_info: PageInfo,
    calibration: ScaleCalibration | None = None,
) -> DetectionOutput:
    """Extract grid lines and column rectangles directly from PDF vector primitives via PyMuPDF."""
    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_info.page_number - 1]
        ox = float(page.rect.x0)
        oy = float(page.rect.y0)
        sx = page_info.width_px / max(float(page.rect.width), 1.0)
        sy = page_info.height_px / max(float(page.rect.height), 1.0)

        min_dim = float(min(page_info.width_px, page_info.height_px))
        min_grid_span = max(min_dim * 0.25, 80.0)
        min_col_px = 5.0
        max_col_px = min_dim * 0.12

        vert_segs: list[tuple[float, float, float]] = []
        horiz_segs: list[tuple[float, float, float]] = []
        raw_boxes: list[dict] = []

        for drawing in page.get_drawings():
            is_filled = drawing.get("fill") is not None
            rects, line_segs = _extract_rect_from_drawing(drawing, ox, oy, sx, sy)

            for x0, y0, x1, y1 in rects:
                w = x1 - x0
                h = y1 - y0
                # Very thin long rectangles are stroked/filled CAD lines
                if w <= 3.5 and h >= 20.0:
                    vert_segs.append(((x0 + x1) * 0.5, y0, y1))
                elif h <= 3.5 and w >= 20.0:
                    horiz_segs.append(((y0 + y1) * 0.5, x0, x1))
                elif min_col_px <= w <= max_col_px and min_col_px <= h <= max_col_px:
                    ratio = w / max(h, 1e-6)
                    if 0.25 <= ratio <= 4.0:
                        raw_boxes.append(
                            {
                                "cx": (x0 + x1) * 0.5,
                                "cy": (y0 + y1) * 0.5,
                                "w": w,
                                "h": h,
                                "filled": is_filled,
                            }
                        )

            for x1, y1, x2, y2 in line_segs:
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                length = hypot(dx, dy)
                if length < 8.0:
                    continue
                if dx <= max(2.0, length * 0.02):
                    vert_segs.append(((x1 + x2) * 0.5, min(y1, y2), max(y1, y2)))
                elif dy <= max(2.0, length * 0.02):
                    horiz_segs.append(((y1 + y2) * 0.5, min(x1, x2), max(x1, x2)))

        # Extract word centers for grid bubble labels
        words: list[tuple[float, float, str]] = []
        for w_item in page.get_text("words"):
            wx0, wy0, wx1, wy1, text = w_item[:5]
            wx = ((float(wx0) + float(wx1)) * 0.5 - ox) * sx
            wy = ((float(wy0) + float(wy1)) * 0.5 - oy) * sy
            words.append((wx, wy, str(text)))

        # Cluster grid lines
        vert_clusters = _cluster_1d(vert_segs, tol=4.5)
        horiz_clusters = _cluster_1d(horiz_segs, tol=4.5)

        grids: list[GridCandidate] = []
        used_x_labels: set[str] = set()
        x_idx = 0
        for cl in sorted(vert_clusters, key=lambda c: c["coord"]):
            is_solid = cl["max_single"] >= min_grid_span * 0.75
            is_dashed = (
                cl["span"] >= min_grid_span
                and cl["count"] >= 3
                and cl["covered"] >= cl["span"] * 0.2
            )
            if not (is_solid or is_dashed):
                continue
            # Ignore page border lines right at extreme edge (within 6px)
            if cl["coord"] < 6.0 or cl["coord"] > page_info.width_px - 6.0:
                continue
            label = _find_grid_label("x", cl["coord"], cl["min_span"], cl["max_span"], words)
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

            grids.append(
                GridCandidate(
                    id=_new_id("grid"),
                    page_number=page_info.page_number,
                    axis="x",
                    label=label,
                    x1_px=round(cl["coord"], 2),
                    y1_px=round(cl["min_span"], 2),
                    x2_px=round(cl["coord"], 2),
                    y2_px=round(cl["max_span"], 2),
                    coord_px=round(cl["coord"], 2),
                    coord_m=coord_m,
                    source="vector",
                    confidence=0.92 if is_solid else 0.84,
                    status="pending",
                )
            )

        used_y_labels: set[str] = set()
        y_idx = 0
        # Sort bottom-to-top in plan Y (descending y_px) so A is at bottom, B above, etc.
        for cl in sorted(horiz_clusters, key=lambda c: -c["coord"]):
            is_solid = cl["max_single"] >= min_grid_span * 0.75
            is_dashed = (
                cl["span"] >= min_grid_span
                and cl["count"] >= 3
                and cl["covered"] >= cl["span"] * 0.2
            )
            if not (is_solid or is_dashed):
                continue
            if cl["coord"] < 6.0 or cl["coord"] > page_info.height_px - 6.0:
                continue
            label = _find_grid_label("y", cl["coord"], cl["min_span"], cl["max_span"], words)
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

            grids.append(
                GridCandidate(
                    id=_new_id("grid"),
                    page_number=page_info.page_number,
                    axis="y",
                    label=label,
                    x1_px=round(cl["min_span"], 2),
                    y1_px=round(cl["coord"], 2),
                    x2_px=round(cl["max_span"], 2),
                    y2_px=round(cl["coord"], 2),
                    coord_px=round(cl["coord"], 2),
                    coord_m=coord_m,
                    source="vector",
                    confidence=0.92 if is_solid else 0.84,
                    status="pending",
                )
            )

        # Deduplicate and build column candidates
        deduped = _deduplicate_boxes(raw_boxes)
        # Sort columns bottom-to-top, left-to-right (plan order)
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
                    status="pending",
                )
            )

        return DetectionOutput(
            method_used="vector",
            grids=grids,
            columns=columns,
            notice=(
                f"Vector extraction found {len(grids)} grid line(s) and {len(columns)} column candidate(s). "
                "Engineer review is required before transfer."
            ),
        )
    finally:
        doc.close()


def extract_raster(
    png_path: Path,
    page_info: PageInfo,
    calibration: ScaleCalibration | None = None,
) -> DetectionOutput:
    """OpenCV raster fallback detector for scanned or hybrid plan images."""
    img = cv2.imread(str(png_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Rendered page image not found: {png_path}")

    h_img, w_img = img.shape[:2]
    min_dim = float(min(h_img, w_img))
    min_grid_len = max(int(min_dim * 0.22), 60)

    # 1. Detect grid lines using Canny + Probabilistic Hough Transform
    blurred = cv2.GaussianBlur(img, (3, 3), 0)
    edges = cv2.Canny(blurred, 50, 150, apertureSize=3)
    raw_lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180.0,
        threshold=60,
        minLineLength=min_grid_len,
        maxLineGap=18,
    )

    vert_segs: list[tuple[float, float, float]] = []
    horiz_segs: list[tuple[float, float, float]] = []
    if raw_lines is not None:
        for line in np.asarray(raw_lines).reshape(-1, 4):
            x1, y1, x2, y2 = [float(v) for v in line]
            dx = abs(x2 - x1)
            dy = abs(y2 - y1)
            length = hypot(dx, dy)
            if length < min_grid_len:
                continue
            if dx <= max(3.0, length * 0.025):
                vert_segs.append(((x1 + x2) * 0.5, min(y1, y2), max(y1, y2)))
            elif dy <= max(3.0, length * 0.025):
                horiz_segs.append(((y1 + y2) * 0.5, min(x1, x2), max(x1, x2)))

    vert_clusters = _cluster_1d(vert_segs, tol=8.0)
    horiz_clusters = _cluster_1d(horiz_segs, tol=8.0)

    grids: list[GridCandidate] = []
    for idx, cl in enumerate(sorted(vert_clusters, key=lambda c: c["coord"]), start=1):
        if cl["coord"] < 8.0 or cl["coord"] > w_img - 8.0:
            continue
        coord_m = None
        if calibration is not None:
            coord_m, _ = px_to_plan_m(cl["coord"], cl["min_span"], calibration)
        grids.append(
            GridCandidate(
                id=_new_id("grid"),
                page_number=page_info.page_number,
                axis="x",
                label=str(idx),
                x1_px=round(cl["coord"], 2),
                y1_px=round(cl["min_span"], 2),
                x2_px=round(cl["coord"], 2),
                y2_px=round(cl["max_span"], 2),
                coord_px=round(cl["coord"], 2),
                coord_m=coord_m,
                source="raster",
                confidence=0.72,
                status="pending",
            )
        )

    for idx, cl in enumerate(sorted(horiz_clusters, key=lambda c: -c["coord"])):
        if cl["coord"] < 8.0 or cl["coord"] > h_img - 8.0:
            continue
        coord_m = None
        if calibration is not None:
            _, coord_m = px_to_plan_m(cl["min_span"], cl["coord"], calibration)
        grids.append(
            GridCandidate(
                id=_new_id("grid"),
                page_number=page_info.page_number,
                axis="y",
                label=_index_to_letters(idx),
                x1_px=round(cl["min_span"], 2),
                y1_px=round(cl["coord"], 2),
                x2_px=round(cl["max_span"], 2),
                y2_px=round(cl["coord"], 2),
                coord_px=round(cl["coord"], 2),
                coord_m=coord_m,
                source="raster",
                confidence=0.72,
                status="pending",
            )
        )

    # 2. Detect solid/filled rectangular column blobs via morphological opening + contours
    _, binary = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    min_col_px = 6.0
    max_col_px = min_dim * 0.12
    raw_boxes: list[dict] = []

    # Try adaptive opening kernels to cleanly strip thin/anti-aliased grid lines while preserving column blocks
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
            ratio = wf / max(hf, 1e-6)
            if not (0.25 <= ratio <= 4.0):
                continue
            rectangularity = area / max(wf * hf, 1.0)
            if rectangularity < 0.62:
                continue
            raw_boxes.append(
                {
                    "cx": x + wf * 0.5,
                    "cy": y + hf * 0.5,
                    "w": wf,
                    "h": hf,
                    "filled": True,
                }
            )
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
                status="pending",
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


class ExperimentalVLMDetector:
    """
    Experimental Vision-Language Model (VLM) detector placeholder interface.

    Intentionally does not invoke external APIs or fabricate structural geometry.
    Future phases may plug in a local or approved cloud vision model adapter
    implementing this interface, still gated by engineer review.
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
) -> DetectionOutput:
    """Dispatch page detection across vector (PyMuPDF), raster (OpenCV), and VLM placeholder."""
    if method == "vlm":
        return ExperimentalVLMDetector().detect(png_path, page_info, calibration)
    if method == "vector":
        return extract_vector(pdf_path, page_info, calibration)
    if method == "raster":
        return extract_raster(png_path, page_info, calibration)

    # method == "auto"
    if page_info.classification == "vector":
        result = extract_vector(pdf_path, page_info, calibration)
        if result.grids or result.columns:
            return result
        fallback = extract_raster(png_path, page_info, calibration)
        fallback.method_used = "raster_fallback"
        return fallback

    if page_info.classification == "hybrid":
        vec = extract_vector(pdf_path, page_info, calibration)
        if vec.grids or vec.columns:
            return vec
        ras = extract_raster(png_path, page_info, calibration)
        ras.method_used = "raster_fallback"
        return ras

    # scanned
    return extract_raster(png_path, page_info, calibration)
