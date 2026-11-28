from __future__ import annotations

import io
import math
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..models import ProjectState

# Engineering paper palette (high contrast for DOCX/PDF print & screen)
BG_COLOR = (248, 250, 252)
CARD_BG = (255, 255, 255)
NAVY = (15, 23, 42)
SLATE_DARK = (51, 65, 85)
SLATE_MID = (100, 116, 139)
SLATE_LIGHT = (203, 213, 225)
GRID_COLOR = (226, 232, 240)

COL_COLOR = (2, 132, 199)       # Sky-600
BEAM_PRI_COLOR = (5, 150, 105)  # Emerald-600
BEAM_SEC_COLOR = (217, 119, 6)  # Amber-600
BEAM_TIE_COLOR = (99, 102, 241) # Indigo-500
CANT_COLOR = (147, 51, 234)     # Purple-600
FAIL_COLOR = (220, 38, 38)      # Red-600
LOAD_GRAV_COLOR = (220, 38, 38)
LOAD_LAT_COLOR = (234, 88, 12)  # Orange-600
FTG_STROKE = (124, 58, 237)
FTG_FILL = (237, 233, 254, 140)
SLAB_FILL = (186, 230, 253, 85)
SLAB_STROKE = (56, 189, 248)
RW_FILL = (148, 163, 184, 125)
RW_STROKE = (71, 85, 105)
DEFL_OK_COLOR = (37, 99, 235)   # Blue-600
GHOST_COLOR = (148, 163, 184)


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "calibrib.ttf"]
        if bold
        else ["arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "calibri.ttf"]
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _project_iso(
    x: float,
    y: float,
    z: float,
    center: tuple[float, float, float],
    scale: float,
    cx: float,
    cy: float,
    yaw: float = -0.68,
    pitch: float = 0.42,
) -> tuple[float, float, float]:
    """Orthographic isometric projection (STAAD right-handed Y-up) returning (sx, sy, depth)."""
    dx = x - center[0]
    dy = y - center[1]
    dz = z - center[2]
    cyaw, syaw = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)

    x1 = dx * cyaw - dz * syaw
    z1 = dx * syaw + dz * cyaw
    y2 = dy * cp - z1 * sp
    z2 = dy * sp + z1 * cp

    return cx + x1 * scale, cy - y2 * scale, z2


def _draw_dashed_line(
    draw: ImageDraw.ImageDraw,
    p1: tuple[float, float],
    p2: tuple[float, float],
    fill: tuple[int, int, int],
    width: int = 2,
    dash: int = 8,
    gap: int = 6,
) -> None:
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    dist = math.hypot(dx, dy)
    if dist < 1.0:
        return
    ux, uy = dx / dist, dy / dist
    pos = 0.0
    while pos < dist:
        end = min(pos + dash, dist)
        draw.line(
            [(p1[0] + ux * pos, p1[1] + uy * pos), (p1[0] + ux * end, p1[1] + uy * end)],
            fill=fill,
            width=width,
        )
        pos += dash + gap


def _draw_arrow(
    draw: ImageDraw.ImageDraw,
    p1: tuple[float, float],
    p2: tuple[float, float],
    fill: tuple[int, int, int],
    width: int = 2,
    head_len: float = 9.0,
) -> None:
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    dist = math.hypot(dx, dy)
    if dist < 2.0:
        return
    draw.line([p1, p2], fill=fill, width=width)
    ux, uy = dx / dist, dy / dist
    px, py = -uy, ux
    h1 = (p2[0] - ux * head_len + px * (head_len * 0.42), p2[1] - uy * head_len + py * (head_len * 0.42))
    h2 = (p2[0] - ux * head_len - px * (head_len * 0.42), p2[1] - uy * head_len - py * (head_len * 0.42))
    draw.polygon([p2, h1, h2], fill=fill)


def _init_canvas(title: str, subtitle: str, width: int = 1200, height: int = 760) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (width, height), BG_COLOR + (255,))
    draw = ImageDraw.Draw(img, "RGBA")
    # Outer frame border
    draw.rectangle([(1, 1), (width - 2, height - 2)], outline=SLATE_LIGHT, width=2)
    # Navy header banner
    draw.rectangle([(0, 0), (width, 64)], fill=NAVY + (255,))
    f_title = _load_font(20, bold=True)
    f_sub = _load_font(13, bold=False)
    draw.text((22, 12), title, fill=(255, 255, 255), font=f_title)
    draw.text((22, 39), subtitle, fill=(203, 213, 225), font=f_sub)
    # Footer legend bar background
    draw.rectangle([(0, height - 44), (width, height)], fill=CARD_BG + (255,))
    draw.line([(0, height - 44), (width, height - 44)], fill=SLATE_LIGHT, width=1)
    return img, draw


def _encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _compute_bounds_and_scale(
    pts: list[tuple[float, float, float]],
    width: int,
    height: int,
    margin_scale: float = 0.56,
) -> tuple[tuple[float, float, float], float, float, float, tuple[float, float, float, float, float, float]]:
    arr = np.array(pts, dtype=float)
    min_x, min_y, min_z = float(arr[:, 0].min()), float(arr[:, 1].min()), float(arr[:, 2].min())
    max_x, max_y, max_z = float(arr[:, 0].max()), float(arr[:, 1].max()), float(arr[:, 2].max())
    center = ((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, (min_z + max_z) * 0.5)
    span = max(max_x - min_x, max_y - min_y, max_z - min_z, 3.0)
    usable_w = width - 160
    usable_h = height - 150
    scale = (min(usable_w, usable_h) * margin_scale) / span
    cx = width * 0.47
    cy = 64 + usable_h * 0.54
    return center, scale, cx, cy, (min_x, max_x, min_y, max_y, min_z, max_z)


def render_3d_model_and_loads_png(project: ProjectState) -> bytes:
    """
    Render a high-resolution isometric PNG of the 3D structural model (nodes, columns, beams,
    slab panels, footings, retaining walls, grid/level axes) with representative gravity and
    lateral applied load vectors. Pure in-memory PNG bytes; never mutates project or writes disk.
    """
    W, H = 1200, 760
    pi = project.project_info
    sm = project.structural_model
    ad = project.analysis_design
    lg = project.load_generation

    subtitle = (
        f"{pi.project_name} ({pi.project_id}) · {len(sm.nodes)} Nodes, {len(sm.members)} Members, "
        f"{len(sm.slab_panels)} Slab Panels, {len(ad.retaining_wall_designs)} Retaining Walls"
    )
    img, draw = _init_canvas("FIGURE 1 · 3D STRUCTURAL MODEL & APPLIED LOADS", subtitle, W, H)
    f_lbl = _load_font(12, bold=True)
    f_sm = _load_font(11, bold=False)

    if not sm.nodes:
        draw.rectangle([(180, 260), (W - 180, 460)], fill=CARD_BG, outline=SLATE_LIGHT, width=2)
        draw.text((220, 325), "3D Analytical Geometry Not Yet Generated", fill=NAVY, font=_load_font(18, bold=True))
        draw.text(
            (220, 365),
            f"Defined levels: {len(project.levels)} · Defined columns: {len(project.columns)}. "
            "Run Module 03 (Generate 3D Frame) to populate 3D model.",
            fill=SLATE_MID,
            font=_load_font(13, bold=False),
        )
        return _encode_png(img)

    node_by_id = {n.node_id: n for n in sm.nodes}
    pts = [(_safe_float(n.x_m), _safe_float(n.y_m), _safe_float(n.z_m)) for n in sm.nodes]
    center, scale, cx, cy, (min_x, max_x, min_y, max_y, min_z, max_z) = _compute_bounds_and_scale(pts, W, H)

    def proj(x: float, y: float, z: float) -> tuple[float, float, float]:
        return _project_iso(x, y, z, center, scale, cx, cy)

    # 1. Ground-plane reference grids
    y_grid = min_y
    pad_g = max(0.6, (max_x - min_x) * 0.08)
    for xg in project.grids.x_grids:
        gx = _safe_float(xg.coord_m)
        p1 = proj(gx, y_grid, min_z - pad_g)
        p2 = proj(gx, y_grid, max_z + pad_g)
        draw.line([(p1[0], p1[1]), (p2[0], p2[1])], fill=GRID_COLOR, width=1)
        draw.text((p1[0] - 8, p1[1] + 4), str(xg.label), fill=SLATE_MID, font=f_sm)
    for yg in project.grids.y_grids:
        gz = _safe_float(yg.coord_m)
        p1 = proj(min_x - pad_g, y_grid, gz)
        p2 = proj(max_x + pad_g, y_grid, gz)
        draw.line([(p1[0], p1[1]), (p2[0], p2[1])], fill=GRID_COLOR, width=1)
        draw.text((p2[0] + 6, p2[1] - 6), str(yg.label), fill=SLATE_MID, font=f_sm)

    # 2. Translucent slab panels
    level_elev = {lv.id: _safe_float(lv.elevation_m) for lv in project.levels}
    for sp in sm.slab_panels:
        if sp.slab_action == "void" or len(sp.polygon_m) < 3:
            continue
        ye = level_elev.get(sp.floor_level, 0.0)
        poly_2d = [(proj(_safe_float(p[0]), ye, _safe_float(p[1]))[0], proj(_safe_float(p[0]), ye, _safe_float(p[1]))[1]) for p in sp.polygon_m]
        draw.polygon(poly_2d, fill=SLAB_FILL, outline=SLAB_STROKE)

    # 3. Footings at base supports
    for f in ad.footing_designs:
        n = node_by_id.get(f.node_id)
        if n is None:
            continue
        hb = max(0.3, _safe_float(f.b_m, 1.5) * 0.5)
        hl = max(0.3, _safe_float(f.l_m, 1.5) * 0.5)
        nx, ny, nz = _safe_float(n.x_m), _safe_float(n.y_m), _safe_float(n.z_m)
        fp = [
            proj(nx - hb, ny, nz - hl)[:2],
            proj(nx + hb, ny, nz - hl)[:2],
            proj(nx + hb, ny, nz + hl)[:2],
            proj(nx - hb, ny, nz + hl)[:2],
        ]
        draw.polygon(fp, fill=FTG_FILL, outline=FAIL_COLOR if f.status == "FAIL" else FTG_STROKE)

    # 4. Retaining walls
    for rw in ad.retaining_wall_designs:
        h_rw = max(0.5, _safe_float(rw.retained_height_m, 2.8))
        if rw.start_xyz_m and rw.end_xyz_m and len(rw.start_xyz_m) == 3 and len(rw.end_xyz_m) == 3:
            sx, sy, sz = map(_safe_float, rw.start_xyz_m)
            ex, ey, ez = map(_safe_float, rw.end_xyz_m)
            c1, c2 = proj(sx, sy, sz)[:2], proj(ex, ey, ez)[:2]
            c3, c4 = proj(ex, ey + h_rw, ez)[:2], proj(sx, sy + h_rw, sz)[:2]
            draw.polygon([c1, c2, c3, c4], fill=RW_FILL, outline=FAIL_COLOR if rw.status == "FAIL" else RW_STROKE)
            draw.text(((c1[0] + c2[0]) * 0.5 - 12, (c3[1] + c4[1]) * 0.5 - 14), rw.wall_id, fill=NAVY, font=f_sm)

    # 5. Frame members sorted back-to-front by isometric depth
    beam_status = {b.member_id: b.status for b in ad.beam_designs}
    col_status = {c.member_id: c.status for c in ad.column_designs}
    segs = []
    for m in sm.members:
        na = node_by_id.get(m.start_node)
        nb = node_by_id.get(m.end_node)
        if na is None or nb is None:
            continue
        pa = proj(_safe_float(na.x_m), _safe_float(na.y_m), _safe_float(na.z_m))
        pb = proj(_safe_float(nb.x_m), _safe_float(nb.y_m), _safe_float(nb.z_m))
        if m.member_type == "column":
            col = FAIL_COLOR if col_status.get(m.member_id) == "FAIL" else COL_COLOR
            lw = 4
        elif m.member_type in ("secondary_beam", "trimmer_beam"):
            col = FAIL_COLOR if beam_status.get(m.member_id) == "FAIL" else BEAM_SEC_COLOR
            lw = 2
        elif m.member_type == "tie_beam":
            col = FAIL_COLOR if beam_status.get(m.member_id) == "FAIL" else BEAM_TIE_COLOR
            lw = 3
        elif m.member_type == "cantilever_beam":
            col = FAIL_COLOR if beam_status.get(m.member_id) == "FAIL" else CANT_COLOR
            lw = 3
        else:
            col = FAIL_COLOR if beam_status.get(m.member_id) == "FAIL" else BEAM_PRI_COLOR
            lw = 3
        segs.append(((pa[2] + pb[2]) * 0.5, pa, pb, col, lw))

    segs.sort(key=lambda s: s[0])
    for _, pa, pb, col, lw in segs:
        draw.line([(pa[0], pa[1]), (pb[0], pb[1])], fill=col, width=lw)

    # Support triangles at base nodes
    for n in sm.nodes:
        if n.is_support:
            p = proj(_safe_float(n.x_m), _safe_float(n.y_m), _safe_float(n.z_m))
            draw.polygon([(p[0], p[1]), (p[0] - 5, p[1] + 8), (p[0] + 5, p[1] + 8)], fill=NAVY)

    # 6. Representative Gravity Load Arrows along upper-floor beams
    top_beams = [
        m for m in sm.members
        if m.member_type != "column"
        and node_by_id.get(m.start_node)
        and node_by_id.get(m.end_node)
        and abs(_safe_float(node_by_id[m.start_node].y_m) - max_y) <= 0.15
    ]
    sample_beams = top_beams[:6]
    for bm in sample_beams:
        na, nb = node_by_id[bm.start_node], node_by_id[bm.end_node]
        mx = (_safe_float(na.x_m) + _safe_float(nb.x_m)) * 0.5
        my = (_safe_float(na.y_m) + _safe_float(nb.y_m)) * 0.5
        mz = (_safe_float(na.z_m) + _safe_float(nb.z_m)) * 0.5
        p_tip = proj(mx, my, mz)
        p_tail = (p_tip[0], p_tip[1] - 34)
        _draw_arrow(draw, p_tail, (p_tip[0], p_tip[1] - 3), LOAD_GRAV_COLOR, width=2, head_len=8.0)

    # 7. Lateral Seismic / Wind Arrows at each story elevation (left-hand face)
    seismic_stories = {s.floor_level: s for s in (lg.seismic_summary.stories if lg and lg.seismic_summary else [])}
    sorted_lvls = sorted(project.levels, key=lambda lv: _safe_float(lv.elevation_m))
    for lv in sorted_lvls:
        ye = _safe_float(lv.elevation_m)
        # Level tag on right edge
        p_tag = proj(max_x + pad_g * 0.8, ye, max_z)
        draw.text((p_tag[0] + 8, p_tag[1] - 6), f"{lv.id} ({ye:.2f}m)", fill=SLATE_DARK, font=f_sm)
        if ye <= min_y + 0.05:
            continue
        p_lat = proj(min_x, ye, (min_z + max_z) * 0.5)
        st = seismic_stories.get(lv.id)
        fx_val = _safe_float(st.fx_kn) if st else 0.0
        _draw_arrow(draw, (p_lat[0] - 62, p_lat[1]), (p_lat[0] - 8, p_lat[1]), LOAD_LAT_COLOR, width=3, head_len=9.0)
        lbl_lat = f"Fx={fx_val:.0f} kN" if fx_val > 0 else "EQX / WL"
        draw.text((p_lat[0] - 118, p_lat[1] - 16), lbl_lat, fill=LOAD_LAT_COLOR, font=f_lbl)

    # Load summary inset card (top-right)
    card_x0, card_y0, card_x1, card_y1 = W - 275, 80, W - 20, 215
    draw.rectangle([(card_x0, card_y0), (card_x1, card_y1)], fill=CARD_BG, outline=SLATE_LIGHT, width=1)
    draw.text((card_x0 + 12, card_y0 + 10), "APPLIED LOAD SUMMARY", fill=NAVY, font=f_lbl)
    ss = lg.seismic_summary if (lg and lg.seismic_summary) else None
    vb_kn = max(_safe_float(getattr(ss, "v_design_x_kn", 0.0)), _safe_float(getattr(ss, "v_design_z_kn", 0.0))) if ss else 0.0
    wt_kn = _safe_float(getattr(ss, "total_seismic_weight_kn", 0.0)) if ss else 0.0
    n_bl = len(lg.beam_loads) if lg else 0
    wind_v = _safe_float(lg.wind.basic_wind_speed_kph, 250.0) if (lg and lg.wind) else 250.0
    zone = lg.seismic.zone if (lg and lg.seismic) else 4
    lines = [
        f"Seismic Zone {zone} · Vb = {vb_kn:.1f} kN",
        f"Seismic Weight W = {wt_kn:.1f} kN",
        f"Basic Wind Speed = {wind_v:.0f} kph",
        f"Gravity Beam Loads = {n_bl} entries",
        f"Gravity DL / LL Arrows (Red, Down)",
        f"Lateral EQ / WL Arrows (Orange)",
    ]
    for idx, txt in enumerate(lines):
        draw.text((card_x0 + 12, card_y0 + 32 + idx * 16), txt, fill=SLATE_DARK, font=f_sm)

    # Bottom Legend
    legend_items = [
        ("Columns", COL_COLOR),
        ("Primary/Edge Beams", BEAM_PRI_COLOR),
        ("Secondary Beams", BEAM_SEC_COLOR),
        ("Tie Beams", BEAM_TIE_COLOR),
        ("Footings", FTG_STROKE),
        ("Retaining Walls", RW_STROKE),
        ("Gravity Loads", LOAD_GRAV_COLOR),
        ("Lateral Loads", LOAD_LAT_COLOR),
    ]
    lx = 22
    ly = H - 28
    for name, color in legend_items:
        draw.rectangle([(lx, ly), (lx + 14, ly + 10)], fill=color)
        draw.text((lx + 20, ly - 2), name, fill=SLATE_DARK, font=f_sm)
        lx += 142

    return _encode_png(img)


def render_3d_displaced_shape_png(project: ProjectState) -> bytes:
    """
    Render a high-resolution isometric PNG comparing the dashed undeformed 3D frame against
    the scaled displaced shape derived from project.analysis_design.story_drifts.
    Pure in-memory PNG bytes; safe fallback when story_drifts or nodes are empty.
    """
    W, H = 1200, 760
    pi = project.project_info
    sm = project.structural_model
    ad = project.analysis_design
    drifts = list(ad.story_drifts or [])

    subtitle = (
        f"{pi.project_name} ({pi.project_id}) · NSCP 2015 Sec. 208.6.4 Inelastic Story Drift & Lateral Deflection Envelope"
    )
    img, draw = _init_canvas("FIGURE 2 · 3D DISPLACED SHAPE & DRIFT ENVELOPE", subtitle, W, H)
    f_lbl = _load_font(12, bold=True)
    f_sm = _load_font(11, bold=False)

    if not sm.nodes:
        draw.rectangle([(180, 260), (W - 180, 460)], fill=CARD_BG, outline=SLATE_LIGHT, width=2)
        draw.text((220, 325), "3D Displaced Shape Unavailable", fill=NAVY, font=_load_font(18, bold=True))
        draw.text(
            (220, 365),
            "Run Modules 03–06 (Geometry, Sizing, Loads & Analysis) to generate lateral displacement results.",
            fill=SLATE_MID,
            font=_load_font(13, bold=False),
        )
        return _encode_png(img)

    node_by_id = {n.node_id: n for n in sm.nodes}
    pts = [(_safe_float(n.x_m), _safe_float(n.y_m), _safe_float(n.z_m)) for n in sm.nodes]
    center, scale, cx, cy, (min_x, max_x, min_y, max_y, min_z, max_z) = _compute_bounds_and_scale(
        pts, W, H, margin_scale=0.52
    )

    def proj(x: float, y: float, z: float) -> tuple[float, float, float]:
        return _project_iso(x, y, z, center, scale, cx, cy)

    # Build elevation -> (dx_m, dz_m, drift_ratio, status) map from story_drifts
    sorted_drifts = sorted(drifts, key=lambda d: _safe_float(d.elevation_m))
    disp_map: dict[str, tuple[float, float, float, str]] = {}
    cum_dx_mm = 0.0
    cum_dz_mm = 0.0
    max_disp_mm = 0.0
    max_drift_ratio = 0.0
    any_fail = False
    nominal_mode = False

    for d in sorted_drifts:
        el_mm_x = _safe_float(getattr(d, "elastic_disp_x_mm", 0.0))
        el_mm_z = _safe_float(getattr(d, "elastic_disp_z_mm", 0.0))
        in_mm_x = _safe_float(getattr(d, "delta_m_x_mm", getattr(d, "inelastic_drift_x_mm", 0.0)))
        in_mm_z = _safe_float(getattr(d, "delta_m_z_mm", getattr(d, "inelastic_drift_z_mm", 0.0)))
        cum_dx_mm += in_mm_x
        cum_dz_mm += in_mm_z
        dx_mm = el_mm_x if abs(el_mm_x) > 1e-4 else cum_dx_mm
        dz_mm = el_mm_z if abs(el_mm_z) > 1e-4 else cum_dz_mm
        dr_max = max(_safe_float(d.drift_ratio_x), _safe_float(d.drift_ratio_z))
        max_disp_mm = max(max_disp_mm, abs(dx_mm), abs(dz_mm))
        max_drift_ratio = max(max_drift_ratio, dr_max)
        if d.status == "FAIL":
            any_fail = True
        disp_map[f"{_safe_float(d.elevation_m):.2f}"] = (dx_mm / 1000.0, dz_mm / 1000.0, dr_max, d.status)

    # Fallback gentle lateral mode if story_drifts is empty or all zero
    bldg_h = max(max_y - min_y, 1.0)
    bldg_span = max(max_x - min_x, max_z - min_z, 3.0)
    if max_disp_mm <= 1e-4:
        nominal_mode = True
        for lv in project.levels:
            ye = _safe_float(lv.elevation_m)
            frac = max(0.0, (ye - min_y) / bldg_h)
            disp_map[f"{ye:.2f}"] = (0.012 * frac, 0.004 * frac, 0.0, "PASS")
        max_disp_mm = 12.0

    # Check for persisted per-node displacements (EX+ / EZ+) before falling back to story_drifts
    nodal_disps = ad.nodal_displacements_mm or {}
    ex_nodes = nodal_disps.get("EX+") or {}
    ez_nodes = nodal_disps.get("EZ+") or {}
    use_per_node = bool(ex_nodes or ez_nodes)
    nodal_peak_mm = 0.0
    if use_per_node:
        for cmap in (ex_nodes, ez_nodes):
            for vec in cmap.values():
                if isinstance(vec, (list, tuple)) and len(vec) >= 3:
                    nodal_peak_mm = max(
                        nodal_peak_mm,
                        abs(_safe_float(vec[0])),
                        abs(_safe_float(vec[1])),
                        abs(_safe_float(vec[2])),
                    )
        if nodal_peak_mm > 1e-4:
            max_disp_mm = max(max_disp_mm, nodal_peak_mm)
            nominal_mode = False
        else:
            use_per_node = False

    # Auto amplification so deflected shape is clearly visible (~12% of building span)
    target_shift_m = bldg_span * 0.12
    peak_disp_m = max(max_disp_mm / 1000.0, 1e-4)
    amp = max(5.0, min(250.0, target_shift_m / peak_disp_m))

    def node_disp(n) -> tuple[float, float, float, float, str]:
        ny = _safe_float(n.y_m)
        key = f"{ny:.2f}"
        # Story drift status & ratio lookup
        st_dr, st_status = 0.0, "PASS"
        if key in disp_map:
            _, _, st_dr, st_status = disp_map[key]
        else:
            best_diff = 0.25
            for k_str, val in disp_map.items():
                diff = abs(float(k_str) - ny)
                if diff < best_diff:
                    best_diff = diff
                    st_dr, st_status = val[2], val[3]

        if use_per_node:
            nid_str = str(n.node_id)
            vx = ex_nodes.get(nid_str)
            vz = ez_nodes.get(nid_str)
            dx_m = _safe_float(vx[0]) / 1000.0 if (vx and len(vx) >= 3) else 0.0
            dy_m = _safe_float(vx[1]) / 1000.0 if (vx and len(vx) >= 3) else (
                _safe_float(vz[1]) / 1000.0 if (vz and len(vz) >= 3) else 0.0
            )
            dz_m = _safe_float(vz[2]) / 1000.0 if (vz and len(vz) >= 3) else (
                _safe_float(vx[2]) / 1000.0 if (vx and len(vx) >= 3) else 0.0
            )
            return dx_m, dy_m, dz_m, st_dr, st_status

        if key in disp_map:
            val = disp_map[key]
            return val[0], 0.0, val[1], val[2], val[3]
        best_val = (0.0, 0.0, 0.0, 0.0, "PASS")
        best_diff = 0.25
        for k_str, val in disp_map.items():
            diff = abs(float(k_str) - ny)
            if diff < best_diff:
                best_diff = diff
                best_val = (val[0], 0.0, val[1], val[2], val[3])
        return best_val

    # 1. Ghosted dashed undeformed wireframe
    for m in sm.members:
        na, nb = node_by_id.get(m.start_node), node_by_id.get(m.end_node)
        if na is None or nb is None:
            continue
        pa = proj(_safe_float(na.x_m), _safe_float(na.y_m), _safe_float(na.z_m))
        pb = proj(_safe_float(nb.x_m), _safe_float(nb.y_m), _safe_float(nb.z_m))
        _draw_dashed_line(draw, (pa[0], pa[1]), (pb[0], pb[1]), GHOST_COLOR, width=1, dash=7, gap=5)

    # 2. Deflected 3D frame (sorted back-to-front)
    defl_segs = []
    for m in sm.members:
        na, nb = node_by_id.get(m.start_node), node_by_id.get(m.end_node)
        if na is None or nb is None:
            continue
        dxa, dya, dza, dra, sta = node_disp(na)
        dxb, dyb, dzb, drb, stb = node_disp(nb)
        pa = proj(_safe_float(na.x_m) + dxa * amp, _safe_float(na.y_m) + dya * amp, _safe_float(na.z_m) + dza * amp)
        pb = proj(_safe_float(nb.x_m) + dxb * amp, _safe_float(nb.y_m) + dyb * amp, _safe_float(nb.z_m) + dzb * amp)
        st = "FAIL" if (sta == "FAIL" or stb == "FAIL") else "PASS"
        dr = max(dra, drb)
        if st == "FAIL":
            col = FAIL_COLOR
        elif dr > 0.75:
            col = BEAM_SEC_COLOR
        else:
            col = DEFL_OK_COLOR if m.member_type == "column" else BEAM_PRI_COLOR
        lw = 4 if m.member_type == "column" else 3
        defl_segs.append(((pa[2] + pb[2]) * 0.5, pa, pb, col, lw))

    defl_segs.sort(key=lambda s: s[0])
    for _, pa, pb, col, lw in defl_segs:
        draw.line([(pa[0], pa[1]), (pb[0], pb[1])], fill=col, width=lw)

    # Support triangles
    for n in sm.nodes:
        if n.is_support:
            p = proj(_safe_float(n.x_m), _safe_float(n.y_m), _safe_float(n.z_m))
            draw.polygon([(p[0], p[1]), (p[0] - 5, p[1] + 8), (p[0] + 5, p[1] + 8)], fill=NAVY)

    # Story displacement callouts along right side
    for d in sorted_drifts:
        ye = _safe_float(d.elevation_m)
        dx_m, dz_m, dr_r, st_s = disp_map.get(f"{ye:.2f}", (0.0, 0.0, 0.0, "PASS"))
        p_u = proj(max_x, ye, max_z)
        p_d = proj(max_x + dx_m * amp, ye, max_z + dz_m * amp)
        _draw_arrow(draw, (p_u[0], p_u[1]), (p_d[0], p_d[1]), FAIL_COLOR if st_s == "FAIL" else DEFL_OK_COLOR, width=2, head_len=7.0)
        lbl = f"{d.floor_level}: Δx={dx_m * 1000:.1f}mm, Δz={dz_m * 1000:.1f}mm (DR={dr_r:.2f})"
        draw.text((p_d[0] + 10, p_d[1] - 7), lbl, fill=FAIL_COLOR if st_s == "FAIL" else NAVY, font=f_sm)

    # Drift Envelope Annotation Card (top-right)
    card_x0, card_y0, card_x1, card_y1 = W - 295, 80, W - 20, 215
    draw.rectangle([(card_x0, card_y0), (card_x1, card_y1)], fill=CARD_BG, outline=SLATE_LIGHT, width=1)
    draw.text((card_x0 + 12, card_y0 + 10), "DISPLACEMENT & DRIFT SUMMARY", fill=NAVY, font=f_lbl)
    status_txt = "NOMINAL REFERENCE" if nominal_mode else ("FAIL (EXCEEDS LIMIT)" if any_fail else "PASS (NSCP 2015)")
    status_col = SLATE_MID if nominal_mode else (FAIL_COLOR if any_fail else BEAM_PRI_COLOR)
    summary_lines = [
        (f"Visual Scale Factor: {amp:.0f}x", SLATE_DARK),
        (f"Peak Lateral Disp: {0.0 if nominal_mode else max_disp_mm:.2f} mm", SLATE_DARK),
        (f"Max Drift Ratio: {max_drift_ratio:.3f} (Limit 1.00)", SLATE_DARK),
        (f"Stories Checked: {len(sorted_drifts)}", SLATE_DARK),
        (f"Drift Status: {status_txt}", status_col),
    ]
    for idx, (txt, col) in enumerate(summary_lines):
        draw.text((card_x0 + 12, card_y0 + 34 + idx * 18), txt, fill=col, font=f_sm)

    # Bottom Legend
    legend_items = [
        ("Undeformed Frame (Dashed)", GHOST_COLOR),
        ("Displaced Columns (PASS)", DEFL_OK_COLOR),
        ("Displaced Beams (PASS)", BEAM_PRI_COLOR),
        ("Drift > 75% Limit", BEAM_SEC_COLOR),
        ("Drift FAIL (> 1.00)", FAIL_COLOR),
    ]
    lx = 22
    ly = H - 28
    for name, color in legend_items:
        draw.rectangle([(lx, ly), (lx + 14, ly + 10)], fill=color)
        draw.text((lx + 20, ly - 2), name, fill=SLATE_DARK, font=f_sm)
        lx += 215

    return _encode_png(img)
