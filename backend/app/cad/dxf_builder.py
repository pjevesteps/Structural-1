from __future__ import annotations

from datetime import datetime, timezone
from html import escape as xml_escape
import io
import math

import ezdxf
from ezdxf.enums import TextEntityAlignment

from ..models import (
    CadDrawingOptions,
    CadDrawingState,
    CadSheetSummary,
    ProjectState,
)
from ..reports.bom_engine import parse_bar_callout

SHEET_DIMENSIONS_MM: dict[str, tuple[float, float]] = {
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
}

SHEET_GAP_MM = 100.0

# (layer_name, ACI color, linetype)
CAD_LAYER_SPECS: list[tuple[str, int, str]] = [
    ("S-TITLEBLK", 7, "CONTINUOUS"),
    ("S-GRID", 1, "CENTER"),
    ("S-GRID-BUBB", 4, "CONTINUOUS"),
    ("S-DIM", 3, "CONTINUOUS"),
    ("S-FTG", 6, "CONTINUOUS"),
    ("S-COL", 4, "CONTINUOUS"),
    ("S-COL-HATCH", 8, "CONTINUOUS"),
    ("S-BEAM", 2, "CONTINUOUS"),
    ("S-SLAB", 5, "CONTINUOUS"),
    ("S-OPENING", 1, "DASHED"),
    ("S-REBAR", 3, "CONTINUOUS"),
    ("S-SCHED", 7, "CONTINUOUS"),
    ("S-TEXT", 7, "CONTINUOUS"),
]

# Dark-mode AutoCAD color palette for in-browser SVG preview
ACI_TO_SVG_HEX: dict[int, str] = {
    1: "#f87171",  # red
    2: "#facc15",  # yellow
    3: "#4ade80",  # green
    4: "#22d3ee",  # cyan
    5: "#60a5fa",  # blue
    6: "#e879f9",  # magenta
    7: "#e2e8f0",  # white
    8: "#64748b",  # dark gray hatch
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_cad_prerequisites(project: ProjectState) -> None:
    sm = project.structural_model
    ms = project.member_sizing
    ad = project.analysis_design
    missing: list[str] = []
    if not sm.nodes or not sm.members:
        missing.append("Phase 3 (3D Structural Geometry)")
    if not ms.sections or not ms.member_assignments:
        missing.append("Phase 4 (Preliminary Member Sizing & Properties)")
    if ad.generated_at is None:
        missing.append("Phase 6 (Analysis Bridge & RC Member Design)")
    if missing:
        raise ValueError(
            "Cannot generate AutoCAD DXF drawings until required modules are completed: "
            + ", ".join(missing)
            + "."
        )


def _add_rect(msp, x0: float, y0: float, x1: float, y1: float, layer: str, linetype: str | None = None):
    attrs = {"layer": layer}
    if linetype:
        attrs["linetype"] = linetype
    return msp.add_lwpolyline(
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
        close=True,
        dxfattribs=attrs,
    )


def _add_text(
    msp,
    text: str,
    x: float,
    y: float,
    height: float,
    layer: str,
    align: TextEntityAlignment = TextEntityAlignment.LEFT,
    rotation: float = 0.0,
):
    ent = msp.add_text(
        str(text),
        dxfattribs={"layer": layer, "height": max(height, 1.0), "rotation": rotation},
    )
    ent.set_placement((x, y), align=align)
    return ent


def _draw_title_block(
    msp,
    ox: float,
    w: float,
    h: float,
    k: float,
    project: ProjectState,
    sheet_no: str,
    sheet_title: str,
    scale_label: str,
) -> tuple[float, float, float, float]:
    """
    Draw outer border, inner frame, and right-hand NBCP title block.
    Returns usable drawing content box (cx0, cy0, cx1, cy1).
    """
    left_m = 20.0 * k
    margin = 10.0 * k
    tb_w = 112.0 * k

    # Outer paper edge & inner frame
    _add_rect(msp, ox, 0.0, ox + w, h, "S-TITLEBLK")
    _add_rect(msp, ox + left_m, margin, ox + w - margin, h - margin, "S-TITLEBLK")

    tb_x0 = ox + w - margin - tb_w
    tb_x1 = ox + w - margin
    tb_y0 = margin
    tb_y1 = h - margin

    # Vertical divider separating drawing area from title block
    msp.add_line((tb_x0, tb_y0), (tb_x0, tb_y1), dxfattribs={"layer": "S-TITLEBLK"})

    pi = project.project_info
    sig = project.reporting.signatory
    rev = project.reporting.options.report_revision
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Divide title block vertically into clean NBCP sections from top to bottom
    tb_h = tb_y1 - tb_y0
    splits = [
        (0.88, 1.00),  # Header / Firm
        (0.72, 0.88),  # Project Name & Code
        (0.56, 0.72),  # Owner & Location
        (0.30, 0.56),  # Engineer Sign & Seal (PRC/PTR/TIN)
        (0.16, 0.30),  # Sheet Title
        (0.08, 0.16),  # Scale / Date / Rev
        (0.00, 0.08),  # Sheet No.
    ]
    for f0, _ in splits[:-1]:
        yy = tb_y0 + f0 * tb_h
        msp.add_line((tb_x0, yy), (tb_x1, yy), dxfattribs={"layer": "S-TITLEBLK"})

    pad = 4.0 * k
    tx = tb_x0 + pad
    tcx = 0.5 * (tb_x0 + tb_x1)

    def box_y(frac: float) -> float:
        return tb_y0 + frac * tb_h

    # 1. Header
    _add_text(msp, "STRUCTURAL AI OFFICE", tcx, box_y(0.955), 4.2 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)
    _add_text(msp, "NSCP 2015 / ACI 318 / NBCP PD 1096", tcx, box_y(0.915), 2.3 * k, "S-TEXT", TextEntityAlignment.MIDDLE_CENTER)

    # 2. Project Name & Code
    _add_text(msp, "PROJECT:", tx, box_y(0.855), 2.1 * k, "S-TEXT")
    proj_name = pi.project_name[:34]
    _add_text(msp, proj_name, tx, box_y(0.815), 3.2 * k, "S-TITLEBLK")
    if len(pi.project_name) > 34:
        _add_text(msp, pi.project_name[34:68], tx, box_y(0.78), 2.8 * k, "S-TITLEBLK")
    _add_text(msp, f"CODE: {pi.project_id}", tx, box_y(0.742), 2.3 * k, "S-TEXT")

    # 3. Owner & Location
    _add_text(msp, "OWNER / CLIENT:", tx, box_y(0.695), 2.0 * k, "S-TEXT")
    _add_text(msp, (sig.project_owner or "TO BE SPECIFIED")[:34], tx, box_y(0.66), 2.6 * k, "S-TITLEBLK")
    _add_text(msp, "LOCATION:", tx, box_y(0.62), 2.0 * k, "S-TEXT")
    loc = (sig.project_address or pi.location or "PHILIPPINES")[:36]
    _add_text(msp, loc, tx, box_y(0.585), 2.4 * k, "S-TITLEBLK")

    # 4. Sign & Seal Block
    _add_text(msp, "DESIGNED & SEALED BY:", tx, box_y(0.535), 2.1 * k, "S-TEXT")
    _add_text(msp, (sig.engineer_name or "ENGINEER OF RECORD")[:32], tx, box_y(0.495), 2.9 * k, "S-TITLEBLK")
    _add_text(msp, (sig.title or "Civil / Structural Engineer")[:36], tx, box_y(0.46), 2.1 * k, "S-TEXT")
    _add_text(msp, f"PRC NO.: {sig.prc_license_no or 'TBD'}", tx, box_y(0.42), 2.2 * k, "S-TEXT")
    _add_text(msp, f"VALID UNTIL: {sig.prc_validity or 'TBD'}", tx, box_y(0.39), 2.0 * k, "S-TEXT")
    _add_text(msp, f"PTR NO.: {sig.ptr_no or 'TBD'}", tx, box_y(0.36), 2.0 * k, "S-TEXT")
    _add_text(msp, f"PTR DATE/PLACE: {sig.ptr_date or '-'} / {sig.ptr_place or '-'}", tx, box_y(0.332), 1.85 * k, "S-TEXT")
    _add_text(msp, f"TIN: {sig.tin or 'TBD'}", tx, box_y(0.308), 2.0 * k, "S-TEXT")

    # 5. Sheet Title
    _add_text(msp, "SHEET CONTENTS:", tx, box_y(0.275), 2.0 * k, "S-TEXT")
    title_words = sheet_title.upper()
    _add_text(msp, title_words[:30], tx, box_y(0.232), 3.1 * k, "S-TITLEBLK")
    if len(title_words) > 30:
        _add_text(msp, title_words[30:62], tx, box_y(0.192), 2.9 * k, "S-TITLEBLK")

    # 6. Scale / Date / Revision
    _add_text(msp, f"SCALE: {scale_label}", tx, box_y(0.136), 2.1 * k, "S-TEXT")
    _add_text(msp, f"DATE: {date_str}", tx, box_y(0.112), 2.0 * k, "S-TEXT")
    _add_text(msp, rev[:34], tx, box_y(0.088), 1.8 * k, "S-TEXT")

    # 7. Sheet Number
    _add_text(msp, "SHEET NO.", tx, box_y(0.058), 2.0 * k, "S-TEXT")
    _add_text(msp, sheet_no, tcx, box_y(0.032), 7.5 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)

    return (ox + left_m + 6.0 * k, margin + 6.0 * k, tb_x0 - 6.0 * k, h - margin - 6.0 * k)


def _extract_grid_axes(project: ProjectState) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Return sorted (label, coord_m) lists for X and Z gridlines."""
    if project.grids.x_grids:
        x_axes = sorted([(g.label, g.coord_m) for g in project.grids.x_grids], key=lambda item: item[1])
    else:
        xs = sorted({round(n.x_m, 2) for n in project.structural_model.nodes})
        x_axes = [(chr(65 + i) if i < 26 else f"X{i + 1}", val) for i, val in enumerate(xs)]

    if project.grids.y_grids:
        z_axes = sorted([(g.label, g.coord_m) for g in project.grids.y_grids], key=lambda item: item[1])
    else:
        zs = sorted({round(n.z_m, 2) for n in project.structural_model.nodes})
        z_axes = [(str(i + 1), val) for i, val in enumerate(zs)]

    return x_axes, z_axes


def _draw_plan_in_box(
    msp,
    project: ProjectState,
    box: tuple[float, float, float, float],
    k: float,
    plan_scale: int,
    title: str,
    mode: str,  # "foundation" | "framing"
    floor_level: str | None = None,
    floor_elev_m: float | None = None,
    warnings: list[str] | None = None,
) -> None:
    """Render a scaled Foundation Plan or Floor Framing Plan inside `box` (mm)."""
    vx0, vy0, vx1, vy1 = box
    vw = max(vx1 - vx0, 20.0)
    vh = max(vy1 - vy0, 20.0)

    sm = project.structural_model
    ms = project.member_sizing
    ad = project.analysis_design
    node_by_id = {n.node_id: n for n in sm.nodes}
    sec_by_id = {s.section_id: s for s in ms.sections}
    asgn_by_mid = {a.member_id: a for a in ms.member_assignments}
    slab_by_pid = {s.panel_id: s for s in ms.slab_assignments}

    x_axes, z_axes = _extract_grid_axes(project)
    all_x = [n.x_m for n in sm.nodes] + [c for _, c in x_axes]
    all_z = [n.z_m for n in sm.nodes] + [c for _, c in z_axes]
    if not all_x or not all_z:
        return

    min_x, max_x = min(all_x), max(all_x)
    min_z, max_z = min(all_z), max(all_z)
    span_x = max(max_x - min_x, 2.0)
    span_z = max(max_z - min_z, 2.0)
    mid_x = 0.5 * (min_x + max_x)
    mid_z = 0.5 * (min_z + max_z)

    # Reserve title strip at bottom and grid bubbles + dimensions around plan
    title_h = 12.0 * k
    pad_x = 24.0 * k
    pad_y = 24.0 * k
    avail_w = max(vw - 2.0 * pad_x, 20.0)
    avail_h = max(vh - title_h - 2.0 * pad_y, 20.0)

    nominal_mm_per_m = 1000.0 / float(plan_scale)
    fit_mm_per_m = min(avail_w / span_x, avail_h / span_z)
    eff_mm_per_m = min(nominal_mm_per_m, fit_mm_per_m)
    eff_scale = int(round(1000.0 / max(eff_mm_per_m, 1e-4)))
    if warnings is not None and eff_mm_per_m < nominal_mm_per_m * 0.98:
        msg = f"{title}: auto-fitted to ~1:{eff_scale} to fit viewport cleanly (requested 1:{plan_scale})."
        if msg not in warnings:
            warnings.append(msg)

    cx = 0.5 * (vx0 + vx1)
    cy = vy0 + title_h + 0.5 * (vh - title_h)

    def to_xy(xm: float, zm: float) -> tuple[float, float]:
        return (cx + (xm - mid_x) * eff_mm_per_m, cy + (zm - mid_z) * eff_mm_per_m)

    # Plan title underline at bottom of viewport
    _add_text(
        msp,
        f"{title}   (SCALE 1:{eff_scale})",
        cx,
        vy0 + 4.5 * k,
        3.2 * k,
        "S-TITLEBLK",
        TextEntityAlignment.MIDDLE_CENTER,
    )
    msp.add_line(
        (cx - min(vw * 0.38, 65.0 * k), vy0 + 2.0 * k),
        (cx + min(vw * 0.38, 65.0 * k), vy0 + 2.0 * k),
        dxfattribs={"layer": "S-TITLEBLK"},
    )

    # 1. Gridlines (S-GRID), Bubbles (S-GRID-BUBB), & Bay Dimensions (S-DIM)
    ext = 9.0 * k
    bub_r = 3.2 * k
    px0, py0 = to_xy(min_x, min_z)
    px1, py1 = to_xy(max_x, max_z)

    for lbl, xm in x_axes:
        gx, _ = to_xy(xm, min_z)
        msp.add_line((gx, py0 - ext), (gx, py1 + ext), dxfattribs={"layer": "S-GRID"})
        # Top & bottom bubbles
        for by in (py1 + ext + bub_r, py0 - ext - bub_r):
            msp.add_circle((gx, by), radius=bub_r, dxfattribs={"layer": "S-GRID-BUBB"})
            _add_text(msp, lbl, gx, by, 2.2 * k, "S-GRID-BUBB", TextEntityAlignment.MIDDLE_CENTER)

    for lbl, zm in z_axes:
        _, gz = to_xy(min_x, zm)
        msp.add_line((px0 - ext, gz), (px1 + ext, gz), dxfattribs={"layer": "S-GRID"})
        for bx in (px0 - ext - bub_r, px1 + ext + bub_r):
            msp.add_circle((bx, gz), radius=bub_r, dxfattribs={"layer": "S-GRID-BUBB"})
            _add_text(msp, lbl, bx, gz, 2.2 * k, "S-GRID-BUBB", TextEntityAlignment.MIDDLE_CENTER)

    # Bay dimensions along top (X) and left (Z)
    if len(x_axes) >= 2:
        dim_y = py1 + ext * 0.55
        msp.add_line((px0, dim_y), (px1, dim_y), dxfattribs={"layer": "S-DIM"})
        for (_, xa), (_, xb) in zip(x_axes, x_axes[1:]):
            gxa, _ = to_xy(xa, min_z)
            gxb, _ = to_xy(xb, min_z)
            msp.add_line((gxa, dim_y - 1.2 * k), (gxa, dim_y + 1.2 * k), dxfattribs={"layer": "S-DIM"})
            msp.add_line((gxb, dim_y - 1.2 * k), (gxb, dim_y + 1.2 * k), dxfattribs={"layer": "S-DIM"})
            _add_text(
                msp,
                f"{abs(xb - xa):.2f}m",
                0.5 * (gxa + gxb),
                dim_y + 1.8 * k,
                1.9 * k,
                "S-DIM",
                TextEntityAlignment.BOTTOM_CENTER,
            )

    if len(z_axes) >= 2:
        dim_x = px0 - ext * 0.55
        msp.add_line((dim_x, py0), (dim_x, py1), dxfattribs={"layer": "S-DIM"})
        for (_, za), (_, zb) in zip(z_axes, z_axes[1:]):
            _, gza = to_xy(min_x, za)
            _, gzb = to_xy(min_x, zb)
            msp.add_line((dim_x - 1.2 * k, gza), (dim_x + 1.2 * k, gza), dxfattribs={"layer": "S-DIM"})
            msp.add_line((dim_x - 1.2 * k, gzb), (dim_x + 1.2 * k, gzb), dxfattribs={"layer": "S-DIM"})
            _add_text(
                msp,
                f"{abs(zb - za):.2f}m",
                dim_x - 1.8 * k,
                0.5 * (gza + gzb),
                1.9 * k,
                "S-DIM",
                TextEntityAlignment.BOTTOM_CENTER,
                rotation=90.0,
            )

    # 2. Foundation Mode: Footings, Pedestals/Support Columns & Base Tie Beams
    if mode == "foundation":
        for f in ad.footing_designs:
            nd = node_by_id.get(f.node_id)
            if nd is None:
                continue
            fx, fy = to_xy(nd.x_m, nd.z_m)
            hw = 0.5 * f.b_m * eff_mm_per_m
            hl = 0.5 * f.l_m * eff_mm_per_m
            _add_rect(msp, fx - hw, fy - hl, fx + hw, fy + hl, "S-FTG")
            # Inner dashed footing step line
            _add_rect(
                msp,
                fx - hw * 0.88,
                fy - hl * 0.88,
                fx + hw * 0.88,
                fy + hl * 0.88,
                "S-FTG",
                linetype="DASHED",
            )
            _add_text(
                msp,
                f.footing_tag,
                fx + hw + 1.2 * k,
                fy + hl * 0.5,
                2.2 * k,
                "S-FTG",
                TextEntityAlignment.LEFT,
            )
            elev_sign = "+" if f.base_elevation_m > 0 else ""
            _add_text(
                msp,
                f"TOF EL.{elev_sign}{f.base_elevation_m:.2f}m",
                fx + hw + 1.2 * k,
                fy - hl * 0.35,
                1.65 * k,
                "S-TEXT",
                TextEntityAlignment.LEFT,
            )

        # Draw base tie beams (and grade beams connecting support-level columns)
        base_tie_beams = [m for m in sm.members if m.member_type == "tie_beam"]
        for m in base_tie_beams:
            _draw_beam_entity(msp, m, node_by_id, sec_by_id, asgn_by_mid, to_xy, eff_mm_per_m, k)

        # Draw columns touching any support node (pedestals or lowest columns)
        support_nids = {n.node_id for n in sm.nodes if n.is_support}
        drawn_col_xy: set[tuple[int, int]] = set()
        for m in sm.members:
            if m.member_type != "column":
                continue
            if m.start_node not in support_nids and m.end_node not in support_nids:
                continue
            nd = node_by_id.get(m.start_node)
            if nd is None:
                continue
            key = (int(round(nd.x_m * 100)), int(round(nd.z_m * 100)))
            if key in drawn_col_xy:
                continue
            drawn_col_xy.add(key)
            _draw_column_entity(msp, m, nd, sec_by_id, asgn_by_mid, to_xy, eff_mm_per_m, k)
        return

    # 3. Framing Plan Mode: Columns, Beams, Slab Panels & Openings at floor_level
    assert floor_level is not None
    # Beams on this floor
    floor_beams = [
        m
        for m in sm.members
        if m.member_type != "column"
        and (
            m.floor_level == floor_level
            or (
                floor_elev_m is not None
                and m.start_node in node_by_id
                and abs(node_by_id[m.start_node].y_m - floor_elev_m) <= 0.12
            )
        )
    ]
    for m in floor_beams:
        _draw_beam_entity(msp, m, node_by_id, sec_by_id, asgn_by_mid, to_xy, eff_mm_per_m, k)

    # Columns framing into or out of this floor elevation
    drawn_col_xy = set()
    for m in sm.members:
        if m.member_type != "column" or m.is_pedestal:
            continue
        na = node_by_id.get(m.start_node)
        nb = node_by_id.get(m.end_node)
        if na is None or nb is None:
            continue
        touches = (
            m.floor_level == floor_level
            or na.floor_level == floor_level
            or nb.floor_level == floor_level
            or (
                floor_elev_m is not None
                and (abs(na.y_m - floor_elev_m) <= 0.12 or abs(nb.y_m - floor_elev_m) <= 0.12)
            )
        )
        if not touches:
            continue
        key = (int(round(na.x_m * 100)), int(round(na.z_m * 100)))
        if key in drawn_col_xy:
            continue
        drawn_col_xy.add(key)
        _draw_column_entity(msp, m, na, sec_by_id, asgn_by_mid, to_xy, eff_mm_per_m, k)

    # Slab thickness tags
    for panel in sm.slab_panels:
        if panel.floor_level != floor_level or panel.slab_action == "void":
            continue
        pts = panel.polygon_m
        if not pts:
            continue
        cx_m = sum(p[0] for p in pts) / len(pts)
        cz_m = sum(p[1] for p in pts) / len(pts)
        sx, sy = to_xy(cx_m, cz_m)
        sa = slab_by_pid.get(panel.panel_id)
        t_mm = sa.thickness_mm if sa else 125.0
        tag = sa.slab_tag if sa else "S1"
        _add_text(
            msp,
            f"{tag}-{t_mm:.0f}mm",
            sx,
            sy,
            1.9 * k,
            "S-SLAB",
            TextEntityAlignment.MIDDLE_CENTER,
        )

    # Floor openings with diagonal X void lines
    for op in project.openings:
        if op.floor_level and op.floor_level != floor_level:
            continue
        pts_xy = [to_xy(pt[0], pt[1]) for pt in op.polygon_m]
        if len(pts_xy) < 3:
            continue
        msp.add_lwpolyline(pts_xy, close=True, dxfattribs={"layer": "S-OPENING"})
        xs_op = [p[0] for p in pts_xy]
        ys_op = [p[1] for p in pts_xy]
        msp.add_line((min(xs_op), min(ys_op)), (max(xs_op), max(ys_op)), dxfattribs={"layer": "S-OPENING"})
        msp.add_line((min(xs_op), max(ys_op)), (max(xs_op), min(ys_op)), dxfattribs={"layer": "S-OPENING"})
        _add_text(
            msp,
            "OPENING",
            0.5 * (min(xs_op) + max(xs_op)),
            0.5 * (min(ys_op) + max(ys_op)),
            1.8 * k,
            "S-OPENING",
            TextEntityAlignment.MIDDLE_CENTER,
        )


def _draw_column_entity(msp, member, node, sec_by_id, asgn_by_mid, to_xy, eff_mm_per_m: float, k: float):
    asgn = asgn_by_mid.get(member.member_id)
    sec = sec_by_id.get(asgn.section_id) if asgn else None
    w_m = (sec.width_mm / 1000.0) if sec else 0.40
    d_m = (sec.depth_mm / 1000.0) if sec else 0.40
    sec_tag = sec.section_id if sec else "C1"

    cx, cy = to_xy(node.x_m, node.z_m)
    hw = max(0.5 * w_m * eff_mm_per_m, 1.3 * k)
    hd = max(0.5 * d_m * eff_mm_per_m, 1.3 * k)
    pts = [(cx - hw, cy - hd), (cx + hw, cy - hd), (cx + hw, cy + hd), (cx - hw, cy + hd)]
    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "S-COL"})

    hatch = msp.add_hatch(dxfattribs={"layer": "S-COL-HATCH"})
    hatch.set_solid_fill(color=8)
    hatch.paths.add_polyline_path(pts, is_closed=True)

    _add_text(
        msp,
        sec_tag,
        cx - hw - 1.0 * k,
        cy + hd + 1.0 * k,
        1.9 * k,
        "S-COL",
        TextEntityAlignment.BOTTOM_RIGHT,
    )


def _draw_beam_entity(msp, member, node_by_id, sec_by_id, asgn_by_mid, to_xy, eff_mm_per_m: float, k: float):
    na = node_by_id.get(member.start_node)
    nb = node_by_id.get(member.end_node)
    if na is None or nb is None:
        return
    ax, ay = to_xy(na.x_m, na.z_m)
    bx, by = to_xy(nb.x_m, nb.z_m)
    dx = bx - ax
    dy = by - ay
    seg_len = math.hypot(dx, dy)
    if seg_len < 1e-3:
        return

    asgn = asgn_by_mid.get(member.member_id)
    sec = sec_by_id.get(asgn.section_id) if asgn else None
    w_m = (sec.width_mm / 1000.0) if sec else 0.30
    sec_tag = sec.section_id if sec else ("TB1" if member.member_type == "tie_beam" else "B1")

    half_w = max(0.5 * w_m * eff_mm_per_m, 0.75 * k)
    nx = -dy / seg_len
    ny = dx / seg_len

    # Parallel double-line beam edges
    msp.add_line((ax + nx * half_w, ay + ny * half_w), (bx + nx * half_w, by + ny * half_w), dxfattribs={"layer": "S-BEAM"})
    msp.add_line((ax - nx * half_w, ay - ny * half_w), (bx - nx * half_w, by - ny * half_w), dxfattribs={"layer": "S-BEAM"})
    # Centerline (dashed)
    msp.add_line((ax, ay), (bx, by), dxfattribs={"layer": "S-BEAM", "linetype": "CENTER"})

    ang = math.degrees(math.atan2(dy, dx))
    if ang > 90.0:
        ang -= 180.0
    elif ang < -90.0:
        ang += 180.0
    mx = 0.5 * (ax + bx) + nx * (half_w + 1.5 * k)
    my = 0.5 * (ay + by) + ny * (half_w + 1.5 * k)
    _add_text(msp, sec_tag, mx, my, 1.85 * k, "S-BEAM", TextEntityAlignment.MIDDLE_CENTER, rotation=ang)


def _draw_general_notes(
    msp,
    project: ProjectState,
    box: tuple[float, float, float, float],
    k: float,
) -> None:
    x0, y0, x1, y1 = box
    mat = project.member_sizing.material
    dp = project.analysis_design.params
    sig = project.reporting.signatory
    sm = project.structural_model

    _add_rect(msp, x0, y0, x1, y1, "S-SCHED")
    pad = 5.0 * k
    cur_y = y1 - 8.0 * k
    _add_text(
        msp,
        "GENERAL STRUCTURAL NOTES (NSCP 2015 / ACI 318)",
        x0 + pad,
        cur_y,
        3.0 * k,
        "S-TITLEBLK",
    )
    cur_y -= 3.5 * k
    msp.add_line((x0 + pad, cur_y), (x1 - pad, cur_y), dxfattribs={"layer": "S-SCHED"})
    cur_y -= 5.0 * k

    notes = [
        "1. DESIGN CODES & STANDARDS:",
        "   - National Structural Code of the Philippines (NSCP 2015, 7th Ed.).",
        "   - ACI 318 Building Code Requirements for Structural Concrete.",
        "   - National Building Code of the Philippines (PD 1096).",
        "2. CONCRETE MATERIALS:",
        f"   - Minimum 28-day compressive strength f'c = {mat.fc_mpa:.1f} MPa.",
        f"   - Elastic Modulus Ec = {mat.ec_kn_m2 / 1000.0:,.1f} MPa (Unit wt = {mat.density_kn_m3:.2f} kN/m3).",
        "   - All concrete shall be ready-mixed and vibrated mechanically.",
        "3. REINFORCING STEEL (PNS 49:2020):",
        f"   - Main bars (>=16mm): Grade 60, fy = {mat.fy_main_mpa:.0f} MPa.",
        f"   - Stirrups / Ties (10-12mm): Grade 40, fyt = {mat.fy_tie_mpa:.0f} MPa.",
        "   - Provide 135-deg seismic hooks (min 6db or 75mm extension).",
        "4. MINIMUM CLEAR CONCRETE COVERS:",
        f"   - Beams & Girders: {dp.clear_cover_beam_mm:.0f} mm",
        f"   - Columns & Pedestals: {dp.clear_cover_column_mm:.0f} mm",
        f"   - Footings (cast against earth): {dp.clear_cover_footing_mm:.0f} mm",
        "   - RC Slabs: 20 mm clear cover.",
        "5. LAP SPLICES & DEVELOPMENT LENGTHS:",
        "   - Stagger lap splices away from max moment & plastic hinge zones.",
        "   - Minimum tension lap splice = 40db (Class B per ACI 318).",
        "   - Top beam bars spliced at midspan; bottom bars near supports.",
        "6. FOUNDATION & GEOTECHNICAL DESIGN:",
        f"   - Allowable soil bearing capacity qa = {dp.soil_bearing_qa_kpa:.1f} kPa.",
        f"   - Footing embedment Df = {dp.footing_embedment_m:.2f} m (Pedestal drop = {sm.options.footing_depth_m:.2f} m).",
        f"   - Ref: {(sig.geotech_report_ref or 'Verify bearing on site')[:52]}",
        "7. CONSTRUCTION VERIFICATION:",
        "   - Verify all dimensions on architectural plans prior to pouring.",
        "   - Notify Engineer of Record before pouring footings/columns/slabs.",
    ]

    line_step = 4.5 * k
    for line in notes:
        if cur_y < y0 + 5.0 * k:
            break
        is_hdr = line[0].isdigit()
        _add_text(
            msp,
            line,
            x0 + pad,
            cur_y,
            (2.25 if is_hdr else 1.95) * k,
            "S-TITLEBLK" if is_hdr else "S-TEXT",
        )
        cur_y -= line_step * (1.15 if is_hdr else 0.96)


def _draw_cad_table(
    msp,
    x0: float,
    y_top: float,
    total_w: float,
    k: float,
    title: str,
    headers: list[str],
    rows: list[list[str]],
    col_weights: list[float],
    max_rows: int = 12,
) -> float:
    """Draw a clean CAD linework schedule table and return bottom Y coordinate."""
    row_h = 6.2 * k
    hdr_h = 7.2 * k
    title_h = 7.5 * k

    _add_text(msp, title, x0, y_top - 4.2 * k, 2.8 * k, "S-TITLEBLK")
    cur_y = y_top - title_h

    w_sum = sum(col_weights)
    col_ws = [total_w * (cw / w_sum) for cw in col_weights]
    col_xs = [x0]
    for cw in col_ws:
        col_xs.append(col_xs[-1] + cw)

    shown_rows = rows[:max_rows]
    table_h = hdr_h + len(shown_rows) * row_h
    y_bot = cur_y - table_h

    # Outer table frame & vertical column dividers
    _add_rect(msp, x0, y_bot, x0 + total_w, cur_y, "S-SCHED")
    for cx in col_xs[1:-1]:
        msp.add_line((cx, y_bot), (cx, cur_y), dxfattribs={"layer": "S-SCHED"})

    # Header separator line & texts
    hdr_bot = cur_y - hdr_h
    msp.add_line((x0, hdr_bot), (x0 + total_w, hdr_bot), dxfattribs={"layer": "S-SCHED"})
    for j, htxt in enumerate(headers):
        _add_text(
            msp,
            htxt,
            0.5 * (col_xs[j] + col_xs[j + 1]),
            cur_y - 0.5 * hdr_h,
            2.0 * k,
            "S-TITLEBLK",
            TextEntityAlignment.MIDDLE_CENTER,
        )

    # Data rows
    for idx, rvals in enumerate(shown_rows):
        ry_top = hdr_bot - idx * row_h
        ry_bot = ry_top - row_h
        if idx < len(shown_rows) - 1:
            msp.add_line((x0, ry_bot), (x0 + total_w, ry_bot), dxfattribs={"layer": "S-SCHED"})
        for j, val in enumerate(rvals):
            _add_text(
                msp,
                str(val)[:24],
                0.5 * (col_xs[j] + col_xs[j + 1]),
                0.5 * (ry_top + ry_bot),
                1.85 * k,
                "S-TEXT",
                TextEntityAlignment.MIDDLE_CENTER,
            )

    return y_bot


def _draw_typical_details(
    msp,
    project: ProjectState,
    box: tuple[float, float, float, float],
    k: float,
    detail_scale: int,
) -> None:
    """Draw 1:detail_scale Typical Column Section, Beam Section & Isolated Footing Plan/Section."""
    x0, y0, x1, y1 = box
    w = x1 - x0
    ad = project.analysis_design
    dp = ad.params
    mm_per_mm_real = 1.0 / float(detail_scale)

    col_w = w / 3.0
    col_box = (x0, y0, x0 + col_w - 6.0 * k, y1)
    bm_box = (x0 + col_w + 3.0 * k, y0, x0 + 2.0 * col_w - 3.0 * k, y1)
    ftg_box = (x0 + 2.0 * col_w + 6.0 * k, y0, x1, y1)

    # ---- 1. Typical RC Column Section ----
    rep_col = ad.column_designs[0] if ad.column_designs else None
    cb_mm = rep_col.width_mm if rep_col else 400.0
    ch_mm = rep_col.depth_mm if rep_col else 400.0
    n_cbars, d_cbar = parse_bar_callout(rep_col.main_bars if rep_col else "8-20mm", 8, dp.main_bar_dia_col_mm)

    c_cx = 0.5 * (col_box[0] + col_box[2])
    c_cy = 0.54 * (col_box[1] + col_box[3])
    max_draw = min(col_box[2] - col_box[0], col_box[3] - col_box[1]) * 0.48
    c_scale = min(mm_per_mm_real, max_draw / max(cb_mm, ch_mm, 200.0))
    cw_d = cb_mm * c_scale
    ch_d = ch_mm * c_scale
    cov_d = dp.clear_cover_column_mm * c_scale

    _add_rect(msp, c_cx - cw_d / 2, c_cy - ch_d / 2, c_cx + cw_d / 2, c_cy + ch_d / 2, "S-COL")
    # Closed tie
    tx0, ty0 = c_cx - cw_d / 2 + cov_d, c_cy - ch_d / 2 + cov_d
    tx1, ty1 = c_cx + cw_d / 2 - cov_d, c_cy + ch_d / 2 - cov_d
    _add_rect(msp, tx0, ty0, tx1, ty1, "S-REBAR")
    # 135-deg seismic hook tails at top-right corner
    hk = 2.8 * k
    msp.add_line((tx1, ty1), (tx1 - hk, ty1 - hk * 0.6), dxfattribs={"layer": "S-REBAR"})
    msp.add_line((tx1, ty1), (tx1 - hk * 0.6, ty1 - hk), dxfattribs={"layer": "S-REBAR"})

    # Longitudinal rebar dots distributed along perimeter
    bar_r = max(0.9 * k, (d_cbar * 0.5) * c_scale)
    bar_pts = [(tx0 + bar_r, ty0 + bar_r), (tx1 - bar_r, ty0 + bar_r), (tx1 - bar_r, ty1 - bar_r), (tx0 + bar_r, ty1 - bar_r)]
    if n_cbars >= 8:
        bar_pts += [
            (c_cx, ty0 + bar_r),
            (c_cx, ty1 - bar_r),
            (tx0 + bar_r, c_cy),
            (tx1 - bar_r, c_cy),
        ]
    for bx, by in bar_pts:
        msp.add_circle((bx, by), radius=bar_r, dxfattribs={"layer": "S-REBAR"})

    # Dimensions & labels
    msp.add_line((c_cx - cw_d / 2, c_cy - ch_d / 2 - 5 * k), (c_cx + cw_d / 2, c_cy - ch_d / 2 - 5 * k), dxfattribs={"layer": "S-DIM"})
    _add_text(msp, f"{cb_mm:.0f}mm", c_cx, c_cy - ch_d / 2 - 7.2 * k, 1.9 * k, "S-DIM", TextEntityAlignment.TOP_CENTER)
    msp.add_line((c_cx - cw_d / 2 - 5 * k, c_cy - ch_d / 2), (c_cx - cw_d / 2 - 5 * k, c_cy + ch_d / 2), dxfattribs={"layer": "S-DIM"})
    _add_text(msp, f"{ch_mm:.0f}mm", c_cx - cw_d / 2 - 7.2 * k, c_cy, 1.9 * k, "S-DIM", TextEntityAlignment.BOTTOM_CENTER, rotation=90.0)
    _add_text(msp, f"MAIN: {rep_col.main_bars if rep_col else '8-20mm'}", c_cx, c_cy + ch_d / 2 + 5.5 * k, 1.9 * k, "S-REBAR", TextEntityAlignment.BOTTOM_CENTER)
    _add_text(msp, f"TIES: {rep_col.ties_confined if rep_col else '10mm @ 100mm o.c.'}", c_cx, c_cy + ch_d / 2 + 2.5 * k, 1.8 * k, "S-TEXT", TextEntityAlignment.BOTTOM_CENTER)
    _add_text(msp, f"TYPICAL RC COLUMN SECTION (1:{detail_scale})", c_cx, col_box[1] + 4.0 * k, 2.3 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)

    # ---- 2. Typical RC Beam Section ----
    rep_bm = ad.beam_designs[0] if ad.beam_designs else None
    bb_mm = rep_bm.width_mm if rep_bm else 300.0
    bh_mm = rep_bm.depth_mm if rep_bm else 500.0
    n_top, d_top = parse_bar_callout(rep_bm.bars_top_start if rep_bm else "3-16mm", 3, dp.main_bar_dia_beam_mm)
    n_bot, d_bot = parse_bar_callout(rep_bm.bars_bot_mid if rep_bm else "3-16mm", 3, dp.main_bar_dia_beam_mm)

    b_cx = 0.5 * (bm_box[0] + bm_box[2])
    b_cy = 0.54 * (bm_box[1] + bm_box[3])
    b_scale = min(mm_per_mm_real, max_draw / max(bb_mm, bh_mm, 200.0))
    bw_d = bb_mm * b_scale
    bh_d = bh_mm * b_scale
    bcov_d = dp.clear_cover_beam_mm * b_scale

    _add_rect(msp, b_cx - bw_d / 2, b_cy - bh_d / 2, b_cx + bw_d / 2, b_cy + bh_d / 2, "S-BEAM")
    stx0, sty0 = b_cx - bw_d / 2 + bcov_d, b_cy - bh_d / 2 + bcov_d
    stx1, sty1 = b_cx + bw_d / 2 - bcov_d, b_cy + bh_d / 2 - bcov_d
    _add_rect(msp, stx0, sty0, stx1, sty1, "S-REBAR")
    msp.add_line((stx1, sty1), (stx1 - hk, sty1 - hk * 0.6), dxfattribs={"layer": "S-REBAR"})

    br = max(0.85 * k, (d_top * 0.5) * b_scale)
    for row_n, ry in ((max(2, min(n_top, 5)), sty1 - br), (max(2, min(n_bot, 5)), sty0 + br)):
        for i in range(row_n):
            frac = i / max(row_n - 1, 1)
            rx = (stx0 + br) + frac * max((stx1 - br) - (stx0 + br), 0.1)
            msp.add_circle((rx, ry), radius=br, dxfattribs={"layer": "S-REBAR"})

    msp.add_line((b_cx - bw_d / 2, b_cy - bh_d / 2 - 5 * k), (b_cx + bw_d / 2, b_cy - bh_d / 2 - 5 * k), dxfattribs={"layer": "S-DIM"})
    _add_text(msp, f"{bb_mm:.0f}mm", b_cx, b_cy - bh_d / 2 - 7.2 * k, 1.9 * k, "S-DIM", TextEntityAlignment.TOP_CENTER)
    msp.add_line((b_cx - bw_d / 2 - 5 * k, b_cy - bh_d / 2), (b_cx - bw_d / 2 - 5 * k, b_cy + bh_d / 2), dxfattribs={"layer": "S-DIM"})
    _add_text(msp, f"{bh_mm:.0f}mm", b_cx - bw_d / 2 - 7.2 * k, b_cy, 1.9 * k, "S-DIM", TextEntityAlignment.BOTTOM_CENTER, rotation=90.0)
    _add_text(msp, f"TOP: {rep_bm.bars_top_start if rep_bm else '3-16mm'} | BOT: {rep_bm.bars_bot_mid if rep_bm else '3-16mm'}", b_cx, b_cy + bh_d / 2 + 5.5 * k, 1.85 * k, "S-REBAR", TextEntityAlignment.BOTTOM_CENTER)
    _add_text(msp, f"STIRRUPS: {rep_bm.stirrups_support if rep_bm else '10mm @ 100mm o.c.'}", b_cx, b_cy + bh_d / 2 + 2.5 * k, 1.8 * k, "S-TEXT", TextEntityAlignment.BOTTOM_CENTER)
    _add_text(msp, f"TYPICAL RC BEAM SECTION (1:{detail_scale})", b_cx, bm_box[1] + 4.0 * k, 2.3 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)

    # ---- 3. Typical Isolated Footing Plan & Section ----
    rep_ftg = ad.footing_designs[0] if ad.footing_designs else None
    fb_mm = (rep_ftg.b_m * 1000.0) if rep_ftg else 1800.0
    ft_mm = rep_ftg.thickness_mm if rep_ftg else 400.0
    df_mm = max(dp.footing_embedment_m * 1000.0, ft_mm + 400.0)

    fw_tot = ftg_box[2] - ftg_box[0]
    fh_tot = ftg_box[3] - ftg_box[1]
    f_cx_plan = ftg_box[0] + fw_tot * 0.27
    f_cx_sec = ftg_box[0] + fw_tot * 0.74
    f_cy = ftg_box[1] + fh_tot * 0.52

    f_scale = min(mm_per_mm_real, (fw_tot * 0.36) / max(fb_mm, 1000.0), (fh_tot * 0.45) / max(df_mm, fb_mm))
    fw_d = fb_mm * f_scale
    ft_d = max(ft_mm * f_scale, 4.0 * k)
    df_d = min(df_mm * f_scale, fh_tot * 0.55)
    ped_d = max(cb_mm * f_scale, 4.0 * k)

    # Plan view (left) with orthogonal bottom rebar grid
    _add_rect(msp, f_cx_plan - fw_d / 2, f_cy - fw_d / 2, f_cx_plan + fw_d / 2, f_cy + fw_d / 2, "S-FTG")
    _add_rect(msp, f_cx_plan - ped_d / 2, f_cy - ped_d / 2, f_cx_plan + ped_d / 2, f_cy + ped_d / 2, "S-COL")
    fcov = max(dp.clear_cover_footing_mm * f_scale, 1.5 * k)
    for i in range(5):
        frac = i / 4.0
        pos = (f_cx_plan - fw_d / 2 + fcov) + frac * (fw_d - 2 * fcov)
        msp.add_line((pos, f_cy - fw_d / 2 + fcov), (pos, f_cy + fw_d / 2 - fcov), dxfattribs={"layer": "S-REBAR"})
        pos_y = (f_cy - fw_d / 2 + fcov) + frac * (fw_d - 2 * fcov)
        msp.add_line((f_cx_plan - fw_d / 2 + fcov, pos_y), (f_cx_plan + fw_d / 2 - fcov, pos_y), dxfattribs={"layer": "S-REBAR"})
    _add_text(msp, "PLAN", f_cx_plan, f_cy - fw_d / 2 - 4.5 * k, 1.9 * k, "S-TEXT", TextEntityAlignment.TOP_CENTER)

    # Cross-section view (right) showing Df, pedestal, thickness t & hooked bars
    sec_bot = f_cy - df_d * 0.45
    sec_ftg_top = sec_bot + ft_d
    sec_ngl = sec_bot + df_d
    _add_rect(msp, f_cx_sec - fw_d / 2, sec_bot, f_cx_sec + fw_d / 2, sec_ftg_top, "S-FTG")
    _add_rect(msp, f_cx_sec - ped_d / 2, sec_ftg_top, f_cx_sec + ped_d / 2, sec_ngl + 3.0 * k, "S-COL")
    # Grade line
    msp.add_line((f_cx_sec - fw_d * 0.65, sec_ngl), (f_cx_sec + fw_d * 0.65, sec_ngl), dxfattribs={"layer": "S-DIM", "linetype": "DASHED"})
    _add_text(msp, "NGL/FFL", f_cx_sec + fw_d * 0.66, sec_ngl, 1.6 * k, "S-DIM", TextEntityAlignment.MIDDLE_LEFT)
    # Hooked bottom bar in section
    ry = sec_bot + max(fcov, 1.5 * k)
    msp.add_lwpolyline(
        [
            (f_cx_sec - fw_d / 2 + fcov, sec_ftg_top - fcov * 0.6),
            (f_cx_sec - fw_d / 2 + fcov, ry),
            (f_cx_sec + fw_d / 2 - fcov, ry),
            (f_cx_sec + fw_d / 2 - fcov, sec_ftg_top - fcov * 0.6),
        ],
        dxfattribs={"layer": "S-REBAR"},
    )
    _add_text(msp, f"Df={dp.footing_embedment_m:.2f}m | t={ft_mm:.0f}mm", f_cx_sec, sec_bot - 4.5 * k, 1.8 * k, "S-DIM", TextEntityAlignment.TOP_CENTER)
    _add_text(
        msp,
        f"BOT BARS: {rep_ftg.bottom_bars_x if rep_ftg else '9-16mm @ 175mm o.c.'} B.W.",
        0.5 * (ftg_box[0] + ftg_box[2]),
        f_cy + df_d * 0.62 + 3.0 * k,
        1.85 * k,
        "S-REBAR",
        TextEntityAlignment.BOTTOM_CENTER,
    )
    _add_text(
        msp,
        f"TYPICAL ISOLATED FOOTING ({rep_ftg.footing_tag if rep_ftg else 'F1'}) (1:{detail_scale})",
        0.5 * (ftg_box[0] + ftg_box[2]),
        ftg_box[1] + 4.0 * k,
        2.3 * k,
        "S-TITLEBLK",
        TextEntityAlignment.MIDDLE_CENTER,
    )


def build_cad_document(
    project: ProjectState,
    options: CadDrawingOptions | None = None,
) -> tuple[ezdxf.document.Drawing, CadDrawingState, dict[str, tuple[float, float, float, float]]]:
    """
    Build the complete multi-sheet AutoCAD R2010 DXF document and return:
      (doc, CadDrawingState, sheet_bounds_map {sheet_no: (ox, 0, ox + w, h)})
    """
    _validate_cad_prerequisites(project)
    opts = options.model_copy(deep=True) if options is not None else project.cad_drawings.options.model_copy(deep=True)
    w, h = SHEET_DIMENSIONS_MM[opts.sheet_size]
    k = w / 841.0

    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()

    # Register standard structural layers
    layer_names: list[str] = []
    for lname, aci, ltype in CAD_LAYER_SPECS:
        if lname not in doc.layers:
            doc.layers.add(lname, color=aci, linetype=ltype)
        layer_names.append(lname)

    sheets: list[CadSheetSummary] = []
    sheet_bounds: dict[str, tuple[float, float, float, float]] = {}
    warnings: list[str] = []
    prefix = opts.sheet_prefix.strip().upper() or "S"

    def _start_sheet(idx: int, sheet_no: str, title: str, scale_lbl: str):
        ox = idx * (w + SHEET_GAP_MM)
        before_count = len(msp)
        content_box = _draw_title_block(msp, ox, w, h, k, project, sheet_no, title, scale_lbl)
        sheet_bounds[sheet_no] = (ox, 0.0, ox + w, h)
        # Create matching Paper Space layout tab
        if sheet_no not in doc.layouts:
            psp = doc.layouts.new(sheet_no)
            psp.page_setup(size=(w, h), margins=(0, 0, 0, 0), units="mm")
            psp.add_viewport(
                center=(w * 0.5, h * 0.5),
                size=(w, h),
                view_center_point=(ox + w * 0.5, h * 0.5),
                view_height=h,
            )
        return ox, before_count, content_box

    sheet_idx = 0

    # =========================================================================
    # SHEET S-1: GENERAL STRUCTURAL NOTES & FOUNDATION PLAN
    # =========================================================================
    if opts.include_general_notes or opts.include_foundation_plan:
        sheet_no = f"{prefix}-{sheet_idx + 1}"
        title = "GENERAL STRUCTURAL NOTES & FOUNDATION PLAN"
        ox, before_n, (cx0, cy0, cx1, cy1) = _start_sheet(
            sheet_idx, sheet_no, title, f"1:{opts.plan_scale}"
        )
        contents: list[str] = []

        if opts.include_general_notes and opts.include_foundation_plan:
            notes_w = (cx1 - cx0) * 0.32
            _draw_general_notes(msp, project, (cx0, cy0, cx0 + notes_w, cy1), k)
            contents.append("NSCP 2015 / ACI 318 General Structural Notes")
            _draw_plan_in_box(
                msp,
                project,
                (cx0 + notes_w + 8.0 * k, cy0, cx1, cy1),
                k,
                opts.plan_scale,
                "FOUNDATION & TIE-BEAM PLAN",
                mode="foundation",
                warnings=warnings,
            )
            contents.append("Foundation Plan (Footings, Pedestals, Tie Beams & TOF Elevations)")
        elif opts.include_general_notes:
            _draw_general_notes(msp, project, (cx0, cy0, cx1, cy1), k)
            contents.append("NSCP 2015 / ACI 318 General Structural Notes")
        else:
            _draw_plan_in_box(
                msp,
                project,
                (cx0, cy0, cx1, cy1),
                k,
                opts.plan_scale,
                "FOUNDATION & TIE-BEAM PLAN",
                mode="foundation",
                warnings=warnings,
            )
            contents.append("Foundation Plan (Footings, Pedestals, Tie Beams & TOF Elevations)")

        sheets.append(
            CadSheetSummary(
                sheet_no=sheet_no,
                title=title,
                contents=contents,
                entity_count=len(msp) - before_n,
            )
        )
        sheet_idx += 1

    # =========================================================================
    # SHEET S-2: FLOOR & ROOF FRAMING PLANS
    # =========================================================================
    if opts.include_framing_plans:
        sheet_no = f"{prefix}-{sheet_idx + 1}"
        title = "FLOOR & ROOF FRAMING PLANS"
        ox, before_n, (cx0, cy0, cx1, cy1) = _start_sheet(
            sheet_idx, sheet_no, title, f"1:{opts.plan_scale}"
        )
        sorted_lvls = sorted(project.levels, key=lambda lv: lv.elevation_m)
        base_elev = sorted_lvls[0].elevation_m if sorted_lvls else 0.0
        elevated_lvls = [lv for lv in sorted_lvls if lv.elevation_m > base_elev + 0.05]
        if not elevated_lvls:
            elevated_lvls = sorted_lvls

        n_plans = len(elevated_lvls)
        cols_grid = 1 if n_plans == 1 else (2 if n_plans <= 4 else 3)
        rows_grid = int(math.ceil(n_plans / cols_grid))
        cell_w = (cx1 - cx0) / cols_grid
        cell_h = (cy1 - cy0) / rows_grid
        gap = 6.0 * k

        contents = []
        for i, lv in enumerate(elevated_lvls):
            r = i // cols_grid
            c = i % cols_grid
            bx0 = cx0 + c * cell_w + gap * 0.5
            bx1 = cx0 + (c + 1) * cell_w - gap * 0.5
            by1 = cy1 - r * cell_h - gap * 0.5
            by0 = cy1 - (r + 1) * cell_h + gap * 0.5
            plan_title = f"{lv.id} FRAMING PLAN (EL. +{lv.elevation_m:.2f}m)"
            _draw_plan_in_box(
                msp,
                project,
                (bx0, by0, bx1, by1),
                k,
                opts.plan_scale,
                plan_title,
                mode="framing",
                floor_level=lv.id,
                floor_elev_m=lv.elevation_m,
                warnings=warnings,
            )
            contents.append(plan_title)

        sheets.append(
            CadSheetSummary(
                sheet_no=sheet_no,
                title=title,
                contents=contents,
                entity_count=len(msp) - before_n,
            )
        )
        sheet_idx += 1

    # =========================================================================
    # SHEET S-3: RC MEMBER SCHEDULES & TYPICAL CROSS-SECTION DETAILS
    # =========================================================================
    if opts.include_schedules or opts.include_typical_details:
        sheet_no = f"{prefix}-{sheet_idx + 1}"
        title = "RC MEMBER SCHEDULES & TYPICAL DETAILS"
        ox, before_n, (cx0, cy0, cx1, cy1) = _start_sheet(
            sheet_idx, sheet_no, title, f"1:{opts.detail_scale} / NTS"
        )
        contents = []
        ad = project.analysis_design
        tot_w = cx1 - cx0
        tot_h = cy1 - cy0

        sched_bottom_y = cy1 - (tot_h * 0.54 if opts.include_typical_details else tot_h)

        if opts.include_schedules:
            # Build compact deduplicated schedules by Tag / Section Mark
            ftg_seen: dict[str, list[str]] = {}
            for f in ad.footing_designs:
                if f.footing_tag not in ftg_seen:
                    ftg_seen[f.footing_tag] = [
                        f.footing_tag,
                        f"{f.b_m:.2f}x{f.l_m:.2f}x{f.thickness_mm:.0f}",
                        f.bottom_bars_x,
                        f.bottom_bars_z,
                        f"{f.base_elevation_m:+.2f}m",
                    ]
            ftg_rows = [ftg_seen[t] for t in sorted(ftg_seen.keys())]

            col_seen: dict[str, list[str]] = {}
            for c in ad.column_designs:
                if c.section_id not in col_seen:
                    col_seen[c.section_id] = [
                        c.section_id,
                        f"{c.width_mm:.0f}x{c.depth_mm:.0f}",
                        c.main_bars,
                        c.ties_confined,
                        c.ties_mid,
                    ]
            col_rows = [col_seen[t] for t in sorted(col_seen.keys())]

            bm_seen: dict[str, list[str]] = {}
            for b in ad.beam_designs:
                if b.section_id not in bm_seen:
                    bm_seen[b.section_id] = [
                        b.section_id,
                        f"{b.width_mm:.0f}x{b.depth_mm:.0f}",
                        b.bars_top_start,
                        b.bars_bot_mid,
                        b.bars_top_end,
                        b.stirrups_support,
                    ]
            bm_rows = [bm_seen[t] for t in sorted(bm_seen.keys())]

            half_w = (tot_w - 10.0 * k) * 0.5
            y_after_ftg = _draw_cad_table(
                msp,
                cx0,
                cy1,
                half_w,
                k,
                "SCHEDULE OF ISOLATED FOOTINGS",
                ["TAG", "B x L x t", "BOT BARS X", "BOT BARS Z", "TOF ELEV"],
                ftg_rows or [["F1", "-", "-", "-", "0.00m"]],
                [10, 22, 26, 26, 16],
                max_rows=8,
            )
            _draw_cad_table(
                msp,
                cx0 + half_w + 10.0 * k,
                cy1,
                half_w,
                k,
                "SCHEDULE OF COLUMNS & PEDESTALS",
                ["MARK", "SIZE (mm)", "MAIN BARS", "CONFINED TIES", "MID TIES"],
                col_rows or [["C1", "-", "-", "-", "-"]],
                [12, 18, 18, 26, 26],
                max_rows=8,
            )

            bm_top_y = min(y_after_ftg - 8.0 * k, cy1 - tot_h * 0.27)
            y_after_bm = _draw_cad_table(
                msp,
                cx0,
                bm_top_y,
                tot_w,
                k,
                "SCHEDULE OF BEAMS & TIE BEAMS",
                ["MARK", "SIZE (mm)", "TOP START", "BOT MID", "TOP END", "STIRRUPS (SUPPORT)"],
                bm_rows or [["B1", "-", "-", "-", "-", "-"]],
                [10, 14, 16, 16, 16, 28],
                max_rows=8,
            )
            sched_bottom_y = min(sched_bottom_y, y_after_bm - 6.0 * k)
            contents.extend(["Schedule of Footings", "Schedule of Columns", "Schedule of Beams"])

        if opts.include_typical_details:
            det_top = max(cy0 + 85.0 * k, min(sched_bottom_y, cy0 + tot_h * 0.44))
            _draw_typical_details(msp, project, (cx0, cy0, cx1, det_top), k, opts.detail_scale)
            contents.append(f"Typical RC Column, Beam & Footing Details (1:{opts.detail_scale})")

        sheets.append(
            CadSheetSummary(
                sheet_no=sheet_no,
                title=title,
                contents=contents,
                entity_count=len(msp) - before_n,
            )
        )
        sheet_idx += 1

    if not sheets:
        raise ValueError("Enable at least one drawing section option before generating CAD sheets.")

    state = CadDrawingState(
        generated_at=_utc_now(),
        options=opts,
        sheets=sheets,
        total_entities=len(msp),
        layers=layer_names,
        warnings=warnings,
    )
    return doc, state, sheet_bounds


def build_dxf_bytes(project: ProjectState, options: CadDrawingOptions | None = None) -> tuple[bytes, CadDrawingState]:
    """Build DXF file as UTF-8 bytes alongside its CadDrawingState metadata."""
    doc, state, _ = build_cad_document(project, options=options)
    text_stream = io.StringIO()
    doc.write(text_stream)
    return text_stream.getvalue().encode("utf-8"), state


def render_sheet_svg(
    project: ProjectState,
    sheet_no: str | None = None,
    options: CadDrawingOptions | None = None,
) -> dict:
    """
    Render a selected sheet from the generated ezdxf document into layer-grouped SVG
    (<g data-layer="...">) for instant interactive browser CAD preview.
    """
    doc, state, sheet_bounds = build_cad_document(project, options=options)
    target_no = sheet_no or state.sheets[0].sheet_no
    if target_no not in sheet_bounds:
        target_no = state.sheets[0].sheet_no

    ox, oy, x1, y1 = sheet_bounds[target_no]
    sw = x1 - ox
    sh = y1 - oy
    sheet_meta = next(s for s in state.sheets if s.sheet_no == target_no)

    layer_color_map: dict[str, str] = {}
    layer_ltype_map: dict[str, str] = {}
    for lname, aci, ltype in CAD_LAYER_SPECS:
        layer_color_map[lname] = ACI_TO_SVG_HEX.get(aci, "#e2e8f0")
        layer_ltype_map[lname] = ltype

    groups: dict[str, list[str]] = {lname: [] for lname, _, _ in CAD_LAYER_SPECS}

    def _in_sheet(x: float) -> bool:
        return (ox - 1.0) <= x <= (x1 + 1.0)

    def _sx(x: float) -> float:
        return x - ox

    def _sy(y: float) -> float:
        return sh - (y - oy)

    def _dash_attr(ltype_name: str) -> str:
        lt = (ltype_name or "").upper()
        if "CENTER" in lt:
            return ' stroke-dasharray="6,2,1.5,2"'
        if "DASH" in lt:
            return ' stroke-dasharray="3.5,2.2"'
        return ""

    msp = doc.modelspace()
    for ent in msp:
        dxftype = ent.dxftype()
        layer = ent.dxf.layer if hasattr(ent.dxf, "layer") else "S-TEXT"
        groups.setdefault(layer, [])
        color_hex = layer_color_map.get(layer, "#e2e8f0")
        ent_ltype = getattr(ent.dxf, "linetype", "BYLAYER")
        eff_ltype = layer_ltype_map.get(layer, "CONTINUOUS") if ent_ltype == "BYLAYER" else ent_ltype
        dash = _dash_attr(eff_ltype)
        sw_px = 0.7 if layer == "S-TITLEBLK" else (0.45 if layer in ("S-GRID", "S-DIM") else 0.55)

        if dxftype == "LINE":
            p1 = ent.dxf.start
            p2 = ent.dxf.end
            if not (_in_sheet(p1.x) or _in_sheet(p2.x)):
                continue
            groups[layer].append(
                f'<line x1="{_sx(p1.x):.2f}" y1="{_sy(p1.y):.2f}" '
                f'x2="{_sx(p2.x):.2f}" y2="{_sy(p2.y):.2f}" '
                f'stroke="{color_hex}" stroke-width="{sw_px}"{dash} />'
            )
        elif dxftype == "LWPOLYLINE":
            pts = list(ent.get_points("xy"))
            if not pts or not any(_in_sheet(px) for px, _ in pts):
                continue
            pts_str = " ".join(f"{_sx(px):.2f},{_sy(py):.2f}" for px, py in pts)
            tag = "polygon" if ent.closed else "polyline"
            groups[layer].append(
                f'<{tag} points="{pts_str}" fill="none" stroke="{color_hex}" '
                f'stroke-width="{sw_px}"{dash} />'
            )
        elif dxftype == "CIRCLE":
            c = ent.dxf.center
            if not _in_sheet(c.x):
                continue
            r = float(ent.dxf.radius)
            fill = color_hex if layer == "S-REBAR" else "none"
            groups[layer].append(
                f'<circle cx="{_sx(c.x):.2f}" cy="{_sy(c.y):.2f}" r="{r:.2f}" '
                f'fill="{fill}" stroke="{color_hex}" stroke-width="{sw_px}" />'
            )
        elif dxftype == "ARC":
            c = ent.dxf.center
            if not _in_sheet(c.x):
                continue
            r = float(ent.dxf.radius)
            a1 = math.radians(float(ent.dxf.start_angle))
            a2 = math.radians(float(ent.dxf.end_angle))
            x_start, y_start = c.x + r * math.cos(a1), c.y + r * math.sin(a1)
            x_end, y_end = c.x + r * math.cos(a2), c.y + r * math.sin(a2)
            sweep = (float(ent.dxf.end_angle) - float(ent.dxf.start_angle)) % 360.0
            large = 1 if sweep > 180.0 else 0
            groups[layer].append(
                f'<path d="M {_sx(x_start):.2f} {_sy(y_start):.2f} '
                f'A {r:.2f} {r:.2f} 0 {large} 0 {_sx(x_end):.2f} {_sy(y_end):.2f}" '
                f'fill="none" stroke="{color_hex}" stroke-width="{sw_px}"{dash} />'
            )
        elif dxftype == "HATCH":
            for path in ent.paths:
                if hasattr(path, "vertices") and path.vertices:
                    if not any(_in_sheet(v[0]) for v in path.vertices):
                        continue
                    pts_str = " ".join(f"{_sx(v[0]):.2f},{_sy(v[1]):.2f}" for v in path.vertices)
                    groups[layer].append(
                        f'<polygon points="{pts_str}" fill="{color_hex}" fill-opacity="0.45" stroke="none" />'
                    )
        elif dxftype in ("TEXT", "MTEXT"):
            txt = ent.dxf.text if dxftype == "TEXT" else ent.plain_text()
            if not txt:
                continue
            h_pt = float(getattr(ent.dxf, "height", getattr(ent.dxf, "char_height", 2.5)))
            rot = float(getattr(ent.dxf, "rotation", 0.0))
            if dxftype == "TEXT":
                align, p1, p2 = ent.get_placement()
                pt = p2 if (p2 is not None and align != TextEntityAlignment.LEFT) else p1
                anchor = "middle" if "CENTER" in align.name else ("end" if "RIGHT" in align.name else "start")
                baseline = "central" if "MIDDLE" in align.name else ("hanging" if "TOP" in align.name else "auto")
            else:
                pt = ent.dxf.insert
                anchor = "start"
                baseline = "hanging"
            if not _in_sheet(pt.x):
                continue
            tx, ty = _sx(pt.x), _sy(pt.y)
            tf = f' transform="rotate({-rot:.1f} {tx:.2f} {ty:.2f})"' if abs(rot) > 0.1 else ""
            groups[layer].append(
                f'<text x="{tx:.2f}" y="{ty:.2f}" fill="{color_hex}" '
                f'font-family="Consolas, monospace" font-size="{h_pt * 1.12:.2f}" '
                f'text-anchor="{anchor}" dominant-baseline="{baseline}"{tf}>'
                f"{xml_escape(txt)}</text>"
            )

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {sw:.1f} {sh:.1f}" '
        f'width="100%" height="100%" class="cad-svg-canvas">',
        f'<rect x="0" y="0" width="{sw:.1f}" height="{sh:.1f}" fill="#0b111e" />',
    ]
    for lname, elems in groups.items():
        if not elems:
            continue
        svg_parts.append(f'<g data-layer="{xml_escape(lname)}">')
        svg_parts.extend(elems)
        svg_parts.append("</g>")
    svg_parts.append("</svg>")

    return {
        "sheet_no": target_no,
        "title": sheet_meta.title,
        "sheet_size": state.options.sheet_size,
        "width_mm": sw,
        "height_mm": sh,
        "layers": [lname for lname, elems in groups.items() if elems],
        "svg": "\n".join(svg_parts),
    }
