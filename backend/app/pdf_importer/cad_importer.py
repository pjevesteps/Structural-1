from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import math
import re
from uuid import uuid4

import ezdxf
import pymupdf

from .. import storage
from ..cad.dwg_converter import convert_dwg_to_dxf
from .inspector import DEFAULT_RENDER_DPI
from .schemas import (
    CadLayerCategory,
    CadLayerInfo,
    ColumnCandidate,
    DetectedOpening,
    GridCandidate,
    ImportDocumentState,
    LengthUnit,
    PageInfo,
    PlanRegion,
    PointPx,
    ReviewStatus,
    ScaleCalibration,
    UpdateCadLayersRequest,
)
from . import service as pdf_service

# DXF $INSUNITS -> (LengthUnit, meters per drawing unit)
_INSUNITS_MAP: dict[int, tuple[LengthUnit, float]] = {
    1: ("in", 0.0254),  # inches
    2: ("ft", 0.3048),  # feet
    4: ("mm", 0.001),   # millimeters
    5: ("cm", 0.01),    # centimeters
    6: ("m", 1.0),      # meters
}

_GRID_LABEL_RE = re.compile(r"^([A-Z]{1,2}'?|\d{1,2}'?)$")
_SHORT_MARKER_RE = re.compile(
    r"^(?:[A-Z]{1,2}'?|\d{1,2}'?|[+-]?\d+(?:\.\d{1,3})?\s*(?:m|M|mm|MM)?|"
    r"(?:EL\.?\s*[+-]?\d+(?:\.\d+)?)|(?:GF|GL|1F|2F|3F|4F|5F|6F|7F|8F|9F|B1|B2|RD|RB|RF|ROOF|BASE).*)$",
    re.IGNORECASE,
)

_CLUTTER_LAYER_TOKENS = (
    "DEFPOINTS",
    "FURN",
    "HATCH",
    "DIM",
    "TITLE",
    "BORDER",
    "TBLK",
    "LANDSCAPE",
    "PLANT",
    "CAR",
    "VEHICLE",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _detect_unit_and_scale(doc: ezdxf.document.Drawing) -> tuple[LengthUnit, float]:
    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    if insunits in _INSUNITS_MAP:
        return _INSUNITS_MAP[insunits]

    # Unitless DXF: infer scale from coordinate extents
    xs: list[float] = []
    ys: list[float] = []
    for ent in doc.modelspace():
        et = ent.dxftype()
        if et == "LINE":
            xs.extend([ent.dxf.start.x, ent.dxf.end.x])
            ys.extend([ent.dxf.start.y, ent.dxf.end.y])
        elif et == "LWPOLYLINE":
            for px, py in ent.get_points("xy"):
                xs.append(px)
                ys.append(py)
        elif et == "CIRCLE":
            xs.append(ent.dxf.center.x)
            ys.append(ent.dxf.center.y)
    if xs and ys:
        extent = max(max(xs) - min(xs), max(ys) - min(ys))
        if extent > 1000.0:
            return ("mm", 0.001)
        if extent > 120.0:
            return ("cm", 0.01)
    return ("m", 1.0)


def _detect_unit_scale_to_m(doc: ezdxf.document.Drawing) -> float:
    return _detect_unit_and_scale(doc)[1]


def _classify_cad_layer(layer_name: str) -> CadLayerCategory:
    up = (layer_name or "0").strip().upper()
    if any(tok in up for tok in ("GRID", "AXIS")):
        return "grid"
    if any(tok in up for tok in ("OPEN", "VOID", "STAIR", "SHAFT", "AIRWELL")):
        return "opening"
    if any(tok in up for tok in ("COL", "BEAM", "SLAB", "FTG", "FOOTING", "FOUND", "STRUCT", "S-", "ST-")):
        return "structural"
    if any(tok in up for tok in ("DEFPOINTS", "DIM", "TEXT", "ANNO", "NOTE", "TITLE", "BORDER", "TBLK", "HATCH", "SYMBOL")):
        return "annotation"
    if any(tok in up for tok in ("FURN", "EQPT", "FIXT", "DOOR", "WIN", "WALL", "ARCH", "A-")):
        return "architectural"
    return "other"


def _default_layer_selected(name: str, is_on: bool, is_frozen: bool, category: CadLayerCategory) -> bool:
    if not is_on or is_frozen:
        return False
    up = (name or "").strip().upper()
    if any(tok in up for tok in _CLUTTER_LAYER_TOKENS):
        return False
    if category == "annotation":
        return False
    return True


def _is_short_grid_or_level_marker(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 28:
        return False
    return bool(_SHORT_MARKER_RE.match(cleaned))


def _is_dashed_or_center(ent, doc: ezdxf.document.Drawing) -> bool:
    lt = (getattr(ent.dxf, "linetype", "BYLAYER") or "").upper()
    if lt == "BYLAYER":
        lname = getattr(ent.dxf, "layer", "0")
        if lname in doc.layers:
            lt = (doc.layers.get(lname).dxf.linetype or "").upper()
    return any(tok in lt for tok in ("CENTER", "DASH", "HIDDEN", "PHANTOM"))


def _polygon_area(pts: list[tuple[float, float]]) -> float:
    n = len(pts)
    if n < 3:
        return 0.0
    acc = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        acc += x0 * y1 - x1 * y0
    return abs(acc) * 0.5


def _segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    def ccw(p, q, r):
        return (r[1] - p[1]) * (q[0] - p[0]) > (q[1] - p[1]) * (r[0] - p[0])

    return ccw(a1, b1, b2) != ccw(a2, b1, b2) and ccw(a1, a2, b1) != ccw(a1, a2, b2)


def _cluster_values(entries: list[tuple[float, dict]], tol: float = 0.12) -> list[list[tuple[float, dict]]]:
    if not entries:
        return []
    ordered = sorted(entries, key=lambda e: e[0])
    clusters: list[list[tuple[float, dict]]] = [[ordered[0]]]
    for val, meta in ordered[1:]:
        mean_c = sum(v for v, _ in clusters[-1]) / len(clusters[-1])
        if abs(val - mean_c) <= tol:
            clusters[-1].append((val, meta))
        else:
            clusters.append([(val, meta)])
    return clusters


def _parse_dxf_document(
    doc: ezdxf.document.Drawing,
    u2m: float,
) -> tuple[
    tuple[float, float, float, float],
    list[dict],
    list[dict],
    list[dict],
    list[dict],
    list[CadLayerInfo],
]:
    """
    Parse all supported modelspace primitives (LINE, LWPOLYLINE, HATCH, CIRCLE, TEXT, MTEXT),
    compute full-modelspace bounding box in meters (for viewport-locked rendering), and build
    CadLayerInfo metadata with entity counts and frozen/off states.
    """
    layer_flags: dict[str, tuple[bool, bool]] = {}
    for layer_rec in doc.layers:
        lname = str(layer_rec.dxf.name)
        try:
            is_on = not bool(layer_rec.is_off())
        except Exception:
            is_on = True
        try:
            is_frozen = bool(layer_rec.is_frozen())
        except Exception:
            is_frozen = False
        layer_flags[lname] = (is_on, is_frozen)

    layer_counts: dict[str, int] = {}
    lines_m: list[dict] = []
    polys_m: list[dict] = []
    circles_m: list[dict] = []
    texts_m: list[dict] = []

    for ent in doc.modelspace():
        et = ent.dxftype()
        if et not in ("LINE", "LWPOLYLINE", "HATCH", "CIRCLE", "TEXT", "MTEXT"):
            continue
        layer = str(getattr(ent.dxf, "layer", "0") or "0")
        layer_up = layer.upper()
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
        dashed = _is_dashed_or_center(ent, doc)

        if et == "LINE":
            p1 = (ent.dxf.start.x * u2m, ent.dxf.start.y * u2m)
            p2 = (ent.dxf.end.x * u2m, ent.dxf.end.y * u2m)
            if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) > 1e-4:
                lines_m.append({"p1": p1, "p2": p2, "layer": layer, "layer_up": layer_up, "dashed": dashed})
        elif et == "LWPOLYLINE":
            pts = [(px * u2m, py * u2m) for px, py in ent.get_points("xy")]
            if len(pts) >= 2:
                is_closed = bool(ent.closed) or (
                    len(pts) >= 4 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 0.02
                )
                clean_pts = pts[:-1] if (len(pts) >= 4 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 0.02) else pts
                if len(clean_pts) == 2 and not is_closed:
                    lines_m.append({"p1": clean_pts[0], "p2": clean_pts[1], "layer": layer, "layer_up": layer_up, "dashed": dashed})
                elif len(clean_pts) >= 3:
                    polys_m.append({"pts": clean_pts, "closed": is_closed, "layer": layer, "layer_up": layer_up, "hatched": False})
        elif et == "HATCH":
            for bpath in ent.paths:
                if hasattr(bpath, "vertices") and len(bpath.vertices) >= 3:
                    pts = [(v[0] * u2m, v[1] * u2m) for v in bpath.vertices]
                    polys_m.append({"pts": pts, "closed": True, "layer": layer, "layer_up": layer_up, "hatched": True})
        elif et == "CIRCLE":
            circles_m.append(
                {
                    "cx": ent.dxf.center.x * u2m,
                    "cy": ent.dxf.center.y * u2m,
                    "r": abs(float(ent.dxf.radius)) * u2m,
                    "layer": layer,
                    "layer_up": layer_up,
                }
            )
        elif et in ("TEXT", "MTEXT"):
            txt = (ent.dxf.text if et == "TEXT" else ent.plain_text()).strip()
            if not txt:
                continue
            if et == "TEXT":
                align, p1, p2 = ent.get_placement()
                pt = p2 if (p2 is not None and align.name != "LEFT") else p1
            else:
                pt = ent.dxf.insert
            texts_m.append({"x": pt.x * u2m, "y": pt.y * u2m, "text": txt, "layer": layer, "layer_up": layer_up})

    if not lines_m and not polys_m and not circles_m:
        raise ValueError("Uploaded DXF contains no modelspace geometry (lines, polylines, or circles).")

    all_x: list[float] = []
    all_y: list[float] = []
    for ln in lines_m:
        all_x.extend([ln["p1"][0], ln["p2"][0]])
        all_y.extend([ln["p1"][1], ln["p2"][1]])
    for pl in polys_m:
        for px, py in pl["pts"]:
            all_x.append(px)
            all_y.append(py)
    for ci in circles_m:
        all_x.extend([ci["cx"] - ci["r"], ci["cx"] + ci["r"]])
        all_y.extend([ci["cy"] - ci["r"], ci["cy"] + ci["r"]])

    full_bounds_m = (min(all_x), max(all_x), min(all_y), max(all_y))

    all_layer_names = sorted(set(layer_counts.keys()) | set(layer_flags.keys()))
    cad_layers: list[CadLayerInfo] = []
    for lname in all_layer_names:
        cnt = layer_counts.get(lname, 0)
        if cnt == 0 and lname not in layer_counts:
            # Skip empty boilerplate layers with 0 entities unless explicitly referenced
            continue
        is_on, is_frozen = layer_flags.get(lname, (True, False))
        cat = _classify_cad_layer(lname)
        sel = _default_layer_selected(lname, is_on, is_frozen, cat)
        cad_layers.append(
            CadLayerInfo(
                name=lname,
                entity_count=cnt,
                is_on=is_on,
                is_frozen=is_frozen,
                selected=sel,
                category=cat,
            )
        )

    # Fallback: if smart defaults leave zero layers selected, select all non-frozen/on layers
    if cad_layers and not any(ly.selected for ly in cad_layers):
        for ly in cad_layers:
            if ly.is_on and not ly.is_frozen:
                ly.selected = True
    if cad_layers and not any(ly.selected for ly in cad_layers):
        for ly in cad_layers:
            ly.selected = True

    return full_bounds_m, lines_m, polys_m, circles_m, texts_m, cad_layers


def _filter_primitives_by_layers(
    lines_m: list[dict],
    polys_m: list[dict],
    circles_m: list[dict],
    texts_m: list[dict],
    selected_layers: set[str],
    cad_layers: list[CadLayerInfo],
    keep_annotation_markers: bool = True,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    layer_by_name = {ly.name: ly for ly in cad_layers}

    f_lines = [ln for ln in lines_m if ln["layer"] in selected_layers]
    f_polys = [pl for pl in polys_m if pl["layer"] in selected_layers]
    f_circles = [ci for ci in circles_m if ci["layer"] in selected_layers]
    f_texts: list[dict] = []
    for tx in texts_m:
        if tx["layer"] in selected_layers:
            f_texts.append(tx)
        elif keep_annotation_markers:
            ly = layer_by_name.get(tx["layer"])
            if (
                ly is not None
                and ly.category == "annotation"
                and ly.is_on
                and not ly.is_frozen
                and _is_short_grid_or_level_marker(tx["text"])
            ):
                f_texts.append(tx)
    return f_lines, f_polys, f_circles, f_texts


def _render_dxf_preview(
    project_id: str,
    doc_id: str,
    full_bounds_m: tuple[float, float, float, float],
    lines_m: list[dict],
    polys_m: list[dict],
    circles_m: list[dict],
    texts_m: list[dict],
) -> tuple[int, int, float, float, float, float, float, callable]:
    """
    Render filtered DXF primitives to original.pdf and pages/page_001.png using the
    FULL modelspace bounding box so toggling outer layers never shifts pixel coordinates.
    """
    min_x_m, max_x_m, min_y_m, max_y_m = full_bounds_m
    span_x_m = max(max_x_m - min_x_m, 2.0)
    span_y_m = max(max_y_m - min_y_m, 2.0)

    page_w_pt = 960.0
    page_h_pt = 680.0
    margin_pt = 48.0
    pt_per_m = min((page_w_pt - 2.0 * margin_pt) / span_x_m, (page_h_pt - 2.0 * margin_pt) / span_y_m)
    mid_x_m = 0.5 * (min_x_m + max_x_m)
    mid_y_m = 0.5 * (min_y_m + max_y_m)

    def m_to_pt(xm: float, ym: float) -> tuple[float, float]:
        return (
            page_w_pt * 0.5 + (xm - mid_x_m) * pt_per_m,
            page_h_pt * 0.5 - (ym - mid_y_m) * pt_per_m,
        )

    pdf_doc = pymupdf.open()
    pg = pdf_doc.new_page(width=page_w_pt, height=page_h_pt)
    shape = pg.new_shape()

    for ln in lines_m:
        a = m_to_pt(*ln["p1"])
        b = m_to_pt(*ln["p2"])
        col_rgb = (0.75, 0.25, 0.25) if ("GRID" in ln["layer_up"] or ln["dashed"]) else (0.18, 0.22, 0.28)
        shape.draw_line(pymupdf.Point(a), pymupdf.Point(b))
        shape.finish(color=col_rgb, width=0.6)

    for pl in polys_m:
        pts_pt = [pymupdf.Point(m_to_pt(px, py)) for px, py in pl["pts"]]
        if pl["closed"]:
            pts_pt.append(pts_pt[0])
        shape.draw_polyline(pts_pt)
        fill_rgb = (0.55, 0.65, 0.78) if (pl["hatched"] or "COL" in pl["layer_up"]) else None
        stroke_rgb = (0.8, 0.15, 0.15) if any(t in pl["layer_up"] for t in ("OPEN", "VOID", "STAIR")) else (0.1, 0.2, 0.35)
        shape.finish(color=stroke_rgb, fill=fill_rgb, width=0.8)

    for ci in circles_m:
        cpt = pymupdf.Point(m_to_pt(ci["cx"], ci["cy"]))
        shape.draw_circle(cpt, max(ci["r"] * pt_per_m, 1.5))
        shape.finish(color=(0.15, 0.45, 0.7), width=0.6)

    shape.commit()

    for tx in texts_m[:250]:
        pt = pymupdf.Point(m_to_pt(tx["x"], tx["y"]))
        pg.insert_text(pt, tx["text"][:28], fontsize=8, color=(0.15, 0.15, 0.15))

    pages_dir = pdf_service._pages_dir(project_id, doc_id)
    pages_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_service._original_pdf_path(project_id, doc_id)
    pdf_doc.save(pdf_path)

    zoom = DEFAULT_RENDER_DPI / 72.0
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    img_filename = "page_001.png"
    pix.save(pages_dir / img_filename)
    width_px, height_px = pix.width, pix.height
    pdf_doc.close()

    sx = width_px / page_w_pt
    sy = height_px / page_h_pt
    px_per_m = pt_per_m * sx

    def m_to_px(xm: float, ym: float) -> tuple[float, float]:
        x_pt, y_pt = m_to_pt(xm, ym)
        return round(x_pt * sx, 2), round(y_pt * sy, 2)

    return width_px, height_px, page_w_pt, page_h_pt, sx, sy, px_per_m, m_to_px


def upload_cad_plan(project_id: str, filename: str, raw_bytes: bytes) -> ImportDocumentState:
    """
    Import an architectural .dxf or .dwg floor plan directly into Module 02:
      1. Converts .dwg -> .dxf via local DWG converter if needed (raises DwgConverterUnavailableError -> HTTP 503).
      2. Inspects doc.layers & modelspace geometry via ezdxf ($INSUNITS aware), applying smart default layer filtering.
      3. Persists original.dxf alongside viewport-locked original.pdf & page_001.png.
      4. Extracts GridCandidates, ColumnCandidates, and DetectedOpenings in exact meters with pre-confirmed calibration.
    """
    project = storage.load_project(project_id)
    if not raw_bytes:
        raise ValueError("Uploaded CAD file is empty.")

    lower_name = (filename or "plan.dxf").lower()
    if lower_name.endswith(".dwg"):
        dxf_bytes = convert_dwg_to_dxf(raw_bytes)
    elif lower_name.endswith(".dxf"):
        dxf_bytes = raw_bytes
    else:
        raise ValueError("Unsupported CAD format. Upload an AutoCAD .dxf or .dwg file.")

    try:
        dxf_text = dxf_bytes.decode("utf-8", errors="replace")
        doc = ezdxf.read(io.StringIO(dxf_text))
    except Exception as exc:
        raise ValueError(f"Could not parse DXF file: {exc}") from exc

    detected_unit, u2m = _detect_unit_and_scale(doc)
    full_bounds_m, all_lines_m, all_polys_m, all_circles_m, all_texts_m, cad_layers = _parse_dxf_document(doc, u2m)
    selected_set = {ly.name for ly in cad_layers if ly.selected}
    lines_m, polys_m, circles_m, texts_m = _filter_primitives_by_layers(
        all_lines_m,
        all_polys_m,
        all_circles_m,
        all_texts_m,
        selected_set,
        cad_layers,
        keep_annotation_markers=True,
    )

    min_x_m, max_x_m, min_y_m, max_y_m = full_bounds_m

    # --- 2. Extract Grid Bubbles (circle + short grid label text) ---
    bubbles: list[dict] = []
    for ci in circles_m:
        if not (0.10 <= ci["r"] <= 1.20):
            continue
        best_txt = None
        best_d = ci["r"] * 1.4 + 0.15
        for tx in texts_m:
            lbl = tx["text"].strip().upper()
            if not _GRID_LABEL_RE.match(lbl):
                continue
            d = math.hypot(tx["x"] - ci["cx"], tx["y"] - ci["cy"])
            if d <= best_d:
                best_d = d
                best_txt = lbl
        if best_txt:
            bubbles.append({"cx": ci["cx"], "cy": ci["cy"], "r": ci["r"], "label": best_txt})

    # --- 3. Extract Gridlines (vertical = X-grid, horizontal = Y-grid) ---
    raw_x_grids: list[tuple[float, dict]] = []
    raw_y_grids: list[tuple[float, dict]] = []

    def _find_endpoint_bubble(p1: tuple[float, float], p2: tuple[float, float]) -> str | None:
        for b in bubbles:
            for pt in (p1, p2):
                if math.hypot(b["cx"] - pt[0], b["cy"] - pt[1]) <= max(b["r"] * 2.2, 1.2):
                    return b["label"]
        return None

    for ln in lines_m:
        p1, p2 = ln["p1"], ln["p2"]
        dx = abs(p2[0] - p1[0])
        dy = abs(p2[1] - p1[1])
        seg_len = math.hypot(dx, dy)
        if seg_len < 1.5:
            continue
        is_grid_layer = "GRID" in ln["layer_up"]
        bub_lbl = _find_endpoint_bubble(p1, p2)
        if not (is_grid_layer or ln["dashed"] or bub_lbl):
            continue
        if dx <= 0.05 and dy >= 1.5:
            raw_x_grids.append((0.5 * (p1[0] + p2[0]), {"p1": p1, "p2": p2, "label": bub_lbl}))
        elif dy <= 0.05 and dx >= 1.5:
            raw_y_grids.append((0.5 * (p1[1] + p2[1]), {"p1": p1, "p2": p2, "label": bub_lbl}))

    x_clusters = _cluster_values(raw_x_grids, tol=0.12)
    y_clusters = _cluster_values(raw_y_grids, tol=0.12)

    extracted_x_grids: list[dict] = []
    for idx, cl in enumerate(x_clusters):
        xm = sum(v for v, _ in cl) / len(cl)
        ys_c = [m["p1"][1] for _, m in cl] + [m["p2"][1] for _, m in cl]
        lbl = next((m["label"] for _, m in cl if m["label"]), chr(65 + idx) if idx < 26 else f"X{idx + 1}")
        extracted_x_grids.append({"axis": "x", "label": lbl, "coord_raw_m": xm, "y1_m": min(ys_c), "y2_m": max(ys_c)})

    extracted_y_grids: list[dict] = []
    for idx, cl in enumerate(y_clusters):
        ym = sum(v for v, _ in cl) / len(cl)
        xs_c = [m["p1"][0] for _, m in cl] + [m["p2"][0] for _, m in cl]
        lbl = next((m["label"] for _, m in cl if m["label"]), str(idx + 1))
        extracted_y_grids.append({"axis": "y", "label": lbl, "coord_raw_m": ym, "x1_m": min(xs_c), "x2_m": max(xs_c)})

    # --- 4. Extract Columns & Openings from closed polygons ---
    extracted_columns: list[dict] = []
    extracted_openings: list[dict] = []

    for pl in polys_m:
        if not pl["closed"]:
            continue
        pts = pl["pts"]
        pxs = [p[0] for p in pts]
        pys = [p[1] for p in pts]
        w_m = max(pxs) - min(pxs)
        h_m = max(pys) - min(pys)
        area = _polygon_area(pts)
        cx_m = 0.5 * (min(pxs) + max(pxs))
        cy_m = 0.5 * (min(pys) + max(pys))
        lup = pl["layer_up"]

        is_open_layer = any(tok in lup for tok in ("OPEN", "VOID", "STAIR", "SHAFT", "AIRWELL"))
        diag_lines_in_poly = []
        if w_m >= 0.6 and h_m >= 0.6 and area >= 0.5:
            for ln in lines_m:
                p1, p2 = ln["p1"], ln["p2"]
                ldx, ldy = abs(p2[0] - p1[0]), abs(p2[1] - p1[1])
                if ldx > 0.25 and ldy > 0.25:
                    if (
                        min(pxs) - 0.15 <= min(p1[0], p2[0])
                        and max(p1[0], p2[0]) <= max(pxs) + 0.15
                        and min(pys) - 0.15 <= min(p1[1], p2[1])
                        and max(p1[1], p2[1]) <= max(pys) + 0.15
                    ):
                        diag_lines_in_poly.append((p1, p2))
        has_x_cross = any(
            _segments_intersect(a[0], a[1], b[0], b[1])
            for i, a in enumerate(diag_lines_in_poly)
            for b in diag_lines_in_poly[i + 1 :]
        )

        if (is_open_layer or has_x_cross) and area >= 0.4:
            if not any(math.hypot(o["cx"] - cx_m, o["cy"] - cy_m) < 0.3 for o in extracted_openings):
                cat = "stairwell" if "STAIR" in lup else ("airwell" if "AIR" in lup else "shaft_other")
                lbl = next(
                    (
                        tx["text"]
                        for tx in texts_m
                        if min(pxs) <= tx["x"] <= max(pxs) and min(pys) <= tx["y"] <= max(pys)
                    ),
                    f"OPEN-{len(extracted_openings) + 1}",
                )
                if "STAIR" in lbl.upper():
                    cat = "stairwell"
                elif "AIR" in lbl.upper():
                    cat = "airwell"
                extracted_openings.append({"pts": pts, "cx": cx_m, "cy": cy_m, "category": cat, "label": lbl[:24]})
            continue

        if 0.20 <= w_m <= 1.50 and 0.20 <= h_m <= 1.50:
            aspect = max(w_m, h_m) / max(min(w_m, h_m), 1e-4)
            rect_ratio = area / max(w_m * h_m, 1e-6)
            is_col_layer = "COL" in lup
            near_grid = any(abs(g["coord_raw_m"] - cx_m) <= 0.35 for g in extracted_x_grids) or any(
                abs(g["coord_raw_m"] - cy_m) <= 0.35 for g in extracted_y_grids
            )
            if aspect <= 3.0 and rect_ratio >= 0.80 and (is_col_layer or pl["hatched"] or near_grid):
                if not any(math.hypot(c["x_m"] - cx_m, c["y_m"] - cy_m) < 0.18 for c in extracted_columns):
                    extracted_columns.append(
                        {
                            "x_m": cx_m,
                            "y_m": cy_m,
                            "width_mm": round(w_m * 1000.0, 1),
                            "depth_mm": round(h_m * 1000.0, 1),
                        }
                    )

    if extracted_x_grids and extracted_y_grids:
        origin_x_m = min(g["coord_raw_m"] for g in extracted_x_grids)
        origin_y_m = min(g["coord_raw_m"] for g in extracted_y_grids)
    elif extracted_columns:
        origin_x_m = min(c["x_m"] for c in extracted_columns)
        origin_y_m = min(c["y_m"] for c in extracted_columns)
    else:
        origin_x_m = min_x_m
        origin_y_m = min_y_m

    doc_id = f"cad-{uuid4().hex[:10]}"
    doc_dir = pdf_service._doc_dir(project_id, doc_id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    # Persist normalized DXF for viewport-locked layer re-rendering
    pdf_service._original_dxf_path(project_id, doc_id).write_bytes(dxf_bytes)

    width_px, height_px, page_w_pt, page_h_pt, sx, sy, px_per_m, m_to_px = _render_dxf_preview(
        project_id,
        doc_id,
        full_bounds_m,
        lines_m,
        polys_m,
        circles_m,
        texts_m,
    )
    margin_pt = 48.0
    img_filename = "page_001.png"

    orig_x_px, orig_y_px = m_to_px(origin_x_m, origin_y_m)
    p2_x_px, p2_y_px = m_to_px(origin_x_m + 1.0, origin_y_m)
    now = _utc_now()

    default_level = project.levels[0].id if project.levels else "1F"
    region_id = f"reg-{uuid4().hex[:8]}"
    region = PlanRegion(
        region_id=region_id,
        page_number=1,
        name=f"CAD Plan ({filename})",
        region_type="plan",
        floor_level=default_level,
        x_px=round(margin_pt * 0.5 * sx, 2),
        y_px=round(margin_pt * 0.5 * sy, 2),
        width_px=round((page_w_pt - margin_pt) * sx, 2),
        height_px=round((page_h_pt - margin_pt) * sy, 2),
        origin_px=PointPx(x_px=orig_x_px, y_px=orig_y_px),
        auto_detected=True,
    )

    known_in_unit = round(1.0 / u2m, 4)
    calibration = ScaleCalibration(
        page_number=1,
        p1=PointPx(x_px=orig_x_px, y_px=orig_y_px),
        p2=PointPx(x_px=p2_x_px, y_px=p2_y_px),
        origin_px=PointPx(x_px=orig_x_px, y_px=orig_y_px),
        known_distance=known_in_unit,
        unit=detected_unit,
        pixel_distance=round(px_per_m, 4),
        real_distance_m=1.0,
        meters_per_px=round(1.0 / px_per_m, 8),
        pixels_per_m=round(px_per_m, 6),
        confirmed=True,
        calibrated_at=now,
        confirmed_at=now,
        notes=f"Exact CAD calibration from DXF coordinates (unit={detected_unit}, scale={u2m:g} m/unit).",
    )

    grid_cands: list[GridCandidate] = []
    for g in extracted_x_grids:
        x1_px, y1_px = m_to_px(g["coord_raw_m"], g["y1_m"])
        x2_px, y2_px = m_to_px(g["coord_raw_m"], g["y2_m"])
        grid_cands.append(
            GridCandidate(
                id=f"gx-{uuid4().hex[:8]}",
                page_number=1,
                region_id=region_id,
                axis="x",
                label=g["label"],
                x1_px=x1_px,
                y1_px=y1_px,
                x2_px=x2_px,
                y2_px=y2_px,
                coord_px=x1_px,
                coord_m=round(g["coord_raw_m"] - origin_x_m, 4),
                source="cad",
                confidence=0.99,
                status=ReviewStatus.APPROVED,
                engineer_approved=True,
            )
        )
    for g in extracted_y_grids:
        x1_px, y1_px = m_to_px(g["x1_m"], g["coord_raw_m"])
        x2_px, y2_px = m_to_px(g["x2_m"], g["coord_raw_m"])
        grid_cands.append(
            GridCandidate(
                id=f"gy-{uuid4().hex[:8]}",
                page_number=1,
                region_id=region_id,
                axis="y",
                label=g["label"],
                x1_px=x1_px,
                y1_px=y1_px,
                x2_px=x2_px,
                y2_px=y2_px,
                coord_px=y1_px,
                coord_m=round(g["coord_raw_m"] - origin_y_m, 4),
                source="cad",
                confidence=0.99,
                status=ReviewStatus.APPROVED,
                engineer_approved=True,
            )
        )

    col_cands: list[ColumnCandidate] = []
    for idx, col in enumerate(extracted_columns, start=1):
        cx_px, cy_px = m_to_px(col["x_m"], col["y_m"])
        rel_x = round(col["x_m"] - origin_x_m, 4)
        rel_y = round(col["y_m"] - origin_y_m, 4)
        gx_lbl = min(extracted_x_grids, key=lambda g: abs(g["coord_raw_m"] - col["x_m"]))["label"] if extracted_x_grids else ""
        gy_lbl = min(extracted_y_grids, key=lambda g: abs(g["coord_raw_m"] - col["y_m"]))["label"] if extracted_y_grids else ""
        grid_ref = f"{gx_lbl}-{gy_lbl}" if (gx_lbl and gy_lbl) else ""
        col_cands.append(
            ColumnCandidate(
                id=f"col-{uuid4().hex[:8]}",
                page_number=1,
                region_id=region_id,
                column_id=f"C-{grid_ref}" if grid_ref else f"C{idx}",
                grid=grid_ref,
                x_px=cx_px,
                y_px=cy_px,
                width_px=max(round((col["width_mm"] / 1000.0) * px_per_m, 2), 6.0),
                height_px=max(round((col["depth_mm"] / 1000.0) * px_per_m, 2), 6.0),
                x_m=rel_x,
                y_m=rel_y,
                width_mm=col["width_mm"],
                depth_mm=col["depth_mm"],
                source="cad",
                confidence=0.98,
                status=ReviewStatus.APPROVED,
                engineer_approved=True,
            )
        )

    opening_cands: list[DetectedOpening] = []
    for op in extracted_openings:
        poly_px = [PointPx(x_px=m_to_px(px, py)[0], y_px=m_to_px(px, py)[1]) for px, py in op["pts"]]
        opening_cands.append(
            DetectedOpening(
                opening_id=f"op-{uuid4().hex[:8]}",
                page_number=1,
                region_id=region_id,
                floor_level=default_level,
                category=op["category"],
                label=op["label"],
                polygon_px=poly_px,
                review_status=ReviewStatus.APPROVED,
                engineer_approved=True,
            )
        )

    page_info = PageInfo(
        page_number=1,
        width_pt=page_w_pt,
        height_pt=page_h_pt,
        width_px=width_px,
        height_px=height_px,
        dpi=DEFAULT_RENDER_DPI,
        classification="vector",
        drawing_count=len(lines_m) + len(polys_m) + len(circles_m),
        image_count=0,
        text_word_count=len(texts_m),
        image_coverage=0.0,
        image_filename=img_filename,
        sheet_number="CAD-1",
        sheet_title=filename,
        sheet_type="floor_plan",
        viewport_count=1,
    )

    state = ImportDocumentState(
        doc_id=doc_id,
        project_id=project_id,
        filename=filename,
        source_format="CAD",
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        file_size_bytes=len(raw_bytes),
        created_at=now,
        updated_at=now,
        page_count=1,
        pages=[page_info],
        regions=[region],
        calibrations={"1": calibration},
        grid_candidates=grid_cands,
        column_candidates=col_cands,
        openings=opening_cands,
        cad_layers=cad_layers,
        detected_cad_unit=detected_unit,
    )

    pdf_service._save_state(state)
    pdf_service._append_audit(
        project_id,
        doc_id,
        "upload_cad",
        {
            "filename": filename,
            "detected_cad_unit": detected_unit,
            "unit_scale_to_m": u2m,
            "layers_total": len(cad_layers),
            "layers_selected": len(selected_set),
            "grids": len(grid_cands),
            "columns": len(col_cands),
            "openings": len(opening_cands),
        },
    )
    return state


def update_cad_layers(
    project_id: str,
    doc_id: str,
    req: UpdateCadLayersRequest,
) -> ImportDocumentState:
    """
    Update selected DXF layers and re-render original.pdf & pages/page_001.png from original.dxf
    using the FULL modelspace bounding box (viewport-locked) so pixel coordinates of existing
    regions, calibrations, master_grid, and approved/manual items never shift.
    """
    state = pdf_service.get_import(project_id, doc_id)
    if state.source_format != "CAD":
        raise ValueError("DXF layer filtering is only available for CAD (.dxf/.dwg) imports.")

    dxf_path = pdf_service._original_dxf_path(project_id, doc_id)
    if not dxf_path.is_file():
        raise ValueError(
            "Original DXF file is missing on this legacy CAD import. Re-upload the .dxf file to enable layer filtering."
        )

    try:
        dxf_text = dxf_path.read_text(encoding="utf-8", errors="replace")
        doc = ezdxf.read(io.StringIO(dxf_text))
    except Exception as exc:
        raise ValueError(f"Could not parse stored DXF file: {exc}") from exc

    _, u2m = _detect_unit_and_scale(doc)
    full_bounds_m, all_lines_m, all_polys_m, all_circles_m, all_texts_m, fresh_layers = _parse_dxf_document(doc, u2m)

    selected_set = set(req.selected_layers)
    # Preserve existing layer catalog order/entries and update selected flags
    if state.cad_layers:
        for ly in state.cad_layers:
            ly.selected = ly.name in selected_set
    else:
        for ly in fresh_layers:
            ly.selected = ly.name in selected_set
        state.cad_layers = fresh_layers

    lines_m, polys_m, circles_m, texts_m = _filter_primitives_by_layers(
        all_lines_m,
        all_polys_m,
        all_circles_m,
        all_texts_m,
        selected_set,
        state.cad_layers,
        keep_annotation_markers=False,
    )

    _render_dxf_preview(
        project_id,
        doc_id,
        full_bounds_m,
        lines_m,
        polys_m,
        circles_m,
        texts_m,
    )

    if state.pages:
        state.pages[0].drawing_count = len(lines_m) + len(polys_m) + len(circles_m)
        state.pages[0].text_word_count = len(texts_m)

    pdf_service._save_state(state)
    pdf_service._append_audit(
        project_id,
        doc_id,
        "cad_layers_updated",
        {
            "selected_layers": sorted(selected_set),
            "drawing_count": len(lines_m) + len(polys_m) + len(circles_m),
            "text_word_count": len(texts_m),
        },
    )
    return state
