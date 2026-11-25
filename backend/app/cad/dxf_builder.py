from __future__ import annotations

from datetime import datetime, timezone
from html import escape as xml_escape
import io
import math
import zipfile

import ezdxf
from ezdxf.enums import TextEntityAlignment
from reportlab.lib import colors as rl_colors
from reportlab.lib.units import mm as RL_MM
from reportlab.pdfgen import canvas as rl_canvas

from ..models import (
    CadDrawingOptions,
    CadDrawingState,
    CadSheetConfig,
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

# (layer_name, ACI color, linetype, AutoCAD DXF lineweight in 1/100 mm)
CAD_LAYER_SPECS: list[tuple[str, int, str, int]] = [
    ("S-TITLEBLK", 7, "CONTINUOUS", 35),
    ("S-GRID", 1, "CENTER", 13),
    ("S-GRID-BUBB", 4, "CONTINUOUS", 18),
    ("S-DIM", 3, "CONTINUOUS", 18),
    ("S-FTG", 6, "CONTINUOUS", 50),
    ("S-COL", 4, "CONTINUOUS", 50),
    ("S-COL-HATCH", 8, "CONTINUOUS", 13),
    ("S-BEAM", 2, "CONTINUOUS", 35),
    ("S-SLAB", 5, "CONTINUOUS", 18),
    ("S-OPENING", 1, "DASHED", 25),
    ("S-REBAR", 3, "CONTINUOUS", 30),
    ("S-SCHED", 7, "CONTINUOUS", 25),
    ("S-TEXT", 7, "CONTINUOUS", 18),
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

# Crisp CAD-to-Print plot style colors on white paper for vector PDF export
ACI_TO_PRINT_HEX: dict[int, str] = {
    1: "#b91c1c",  # red
    2: "#92400e",  # yellow -> dark amber/brown
    3: "#166534",  # green -> deep emerald
    4: "#0f4c81",  # cyan -> deep steel blue
    5: "#1d4ed8",  # blue
    6: "#6b21a8",  # magenta -> deep violet
    7: "#111827",  # white -> near-black ink
    8: "#94a3b8",  # hatch -> neutral slate
}

SCHEDULE_KEYS = ("schedule_footings", "schedule_columns", "schedule_beams", "schedule_slabs_stairs")
DETAIL_KEYS = (
    "detail_footing_section",
    "detail_column_section",
    "detail_beam_section",
    "detail_beam_longitudinal",
    "detail_column_longitudinal",
    "detail_retaining_wall",
    "detail_roof_truss",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_pdf_text(text: str) -> str:
    return (
        str(text)
        .replace("\u20b1", "PHP ")
        .replace("\u2014", " - ")
        .replace("\u2013", "-")
        .replace("\u2022", "-")
        .replace("\u2192", "->")
        .replace("\u00b0", " deg")
        .replace("\u00d8", "D")
        .replace("\u00f8", "d")
    )


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


def get_available_cad_items(project: ProjectState) -> list[dict[str, str]]:
    """Return all selectable CAD drawing items for this project."""
    items: list[dict[str, str]] = [
        {"key": "general_notes", "label": "General Structural Notes", "group": "Plans"},
        {"key": "foundation_plan", "label": "Foundation & Tie-Beam Plan", "group": "Plans"},
    ]
    sorted_lvls = sorted(project.levels, key=lambda lv: lv.elevation_m)
    for lv in sorted_lvls:
        items.append(
            {
                "key": f"floor_plan:{lv.id}",
                "label": f"{lv.id} Framing Plan (EL. {lv.elevation_m:+.2f}m)",
                "group": "Plans",
            }
        )
    items.extend(
        [
            {"key": "schedule_footings", "label": "Schedule of Footings", "group": "Schedules"},
            {"key": "schedule_columns", "label": "Schedule of Columns", "group": "Schedules"},
            {"key": "schedule_beams", "label": "Schedule of Beams", "group": "Schedules"},
            {"key": "schedule_slabs_stairs", "label": "Schedule of Slabs & Stairs", "group": "Schedules"},
            {
                "key": "detail_footing_section",
                "label": "Typical Isolated Footing Plan & Section",
                "group": "Details",
            },
            {
                "key": "detail_column_section",
                "label": "Typical RC Column Cross-Section",
                "group": "Details",
            },
            {
                "key": "detail_beam_section",
                "label": "Typical RC Beam Cross-Section",
                "group": "Details",
            },
            {
                "key": "detail_beam_longitudinal",
                "label": "SMRF Beam Longitudinal Elevation",
                "group": "Details",
            },
            {
                "key": "detail_column_longitudinal",
                "label": "SMRF Column & Pedestal Longitudinal",
                "group": "Details",
            },
            {
                "key": "detail_retaining_wall",
                "label": "Basement Retaining Wall Section (NSCP Sec. 209)",
                "group": "Details",
            },
            {
                "key": "detail_roof_truss",
                "label": "Steel Roof Truss Elevation Detail",
                "group": "Details",
            },
        ]
    )
    return items


def build_default_sheet_configs(
    project: ProjectState,
    options: CadDrawingOptions,
) -> list[CadSheetConfig]:
    """Auto-build a sensible default per-page sheet plan when options.sheets is empty."""
    prefix = options.sheet_prefix.strip().upper() or "S"
    sorted_lvls = sorted(project.levels, key=lambda lv: lv.elevation_m)
    base_elev = sorted_lvls[0].elevation_m if sorted_lvls else 0.0
    elevated_lvls = [lv for lv in sorted_lvls if lv.elevation_m > base_elev + 0.05]

    # Pick ground/main floor vs upper floors cleanly
    ground_lvl = None
    upper_lvls = list(elevated_lvls)
    if len(sorted_lvls) >= 2:
        low_id = sorted_lvls[0].id.upper()
        if any(tok in low_id for tok in ("BSMT", "BASEMENT", "SUB")) and elevated_lvls:
            ground_lvl = elevated_lvls[0]
            upper_lvls = elevated_lvls[1:]
        else:
            ground_lvl = sorted_lvls[0]
            upper_lvls = elevated_lvls
    elif sorted_lvls:
        ground_lvl = sorted_lvls[0]
        upper_lvls = []

    configs: list[CadSheetConfig] = []

    if options.preset == "a3_individual_floors":
        idx = 1
        if options.include_general_notes:
            configs.append(
                CadSheetConfig(
                    sheet_no=f"{prefix}-{idx}",
                    title="GENERAL STRUCTURAL NOTES",
                    items=["general_notes"],
                )
            )
            idx += 1
        if options.include_foundation_plan:
            configs.append(
                CadSheetConfig(
                    sheet_no=f"{prefix}-{idx}",
                    title="FOUNDATION & TIE-BEAM PLAN",
                    items=["foundation_plan"],
                )
            )
            idx += 1
        if options.include_framing_plans:
            plan_lvls = ([ground_lvl] if ground_lvl else []) + upper_lvls
            for lv in plan_lvls:
                configs.append(
                    CadSheetConfig(
                        sheet_no=f"{prefix}-{idx}",
                        title=f"{lv.id} FRAMING PLAN",
                        items=[f"floor_plan:{lv.id}"],
                    )
                )
                idx += 1
        if options.include_schedules:
            configs.append(
                CadSheetConfig(
                    sheet_no=f"{prefix}-{idx}",
                    title="RC MEMBER SCHEDULES",
                    items=list(SCHEDULE_KEYS),
                )
            )
            idx += 1
        if options.include_typical_details:
            configs.append(
                CadSheetConfig(
                    sheet_no=f"{prefix}-{idx}",
                    title="TYPICAL RC CROSS-SECTIONS & SMRF DETAILS",
                    items=list(DETAIL_KEYS),
                )
            )
        return configs

    # Default: a1_permit_set (or custom fallback when sheets is empty)
    idx = 1
    if options.include_general_notes or options.include_foundation_plan:
        s1_items: list[str] = []
        if options.include_general_notes:
            s1_items.append("general_notes")
        if options.include_foundation_plan:
            s1_items.append("foundation_plan")
        if options.include_framing_plans and ground_lvl is not None:
            s1_items.append(f"floor_plan:{ground_lvl.id}")
        s1_title = (
            f"GENERAL NOTES, FOUNDATION & {ground_lvl.id} PLAN"
            if (options.include_foundation_plan and options.include_framing_plans and ground_lvl is not None)
            else "GENERAL STRUCTURAL NOTES & FOUNDATION PLAN"
        )
        configs.append(CadSheetConfig(sheet_no=f"{prefix}-{idx}", title=s1_title, items=s1_items))
        idx += 1

    if options.include_framing_plans:
        s2_lvls = upper_lvls if upper_lvls else ([ground_lvl] if ground_lvl else [])
        if s2_lvls:
            configs.append(
                CadSheetConfig(
                    sheet_no=f"{prefix}-{idx}",
                    title="FLOOR & ROOF FRAMING PLANS",
                    items=[f"floor_plan:{lv.id}" for lv in s2_lvls],
                )
            )
            idx += 1

    if options.include_schedules or options.include_typical_details:
        s3_items: list[str] = []
        if options.include_schedules:
            s3_items.extend(SCHEDULE_KEYS)
        if options.include_typical_details:
            s3_items.extend(DETAIL_KEYS)
        configs.append(
            CadSheetConfig(
                sheet_no=f"{prefix}-{idx}",
                title="RC MEMBER SCHEDULES & TYPICAL DETAILS",
                items=s3_items,
            )
        )

    return configs


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
    Returns printable content box (cx0, cy0, cx1, cy1).
    """
    left_m = 20.0 * k
    margin = 10.0 * k
    tb_w = 112.0 * k

    _add_rect(msp, ox, 0.0, ox + w, h, "S-TITLEBLK")
    _add_rect(msp, ox + left_m, margin, ox + w - margin, h - margin, "S-TITLEBLK")

    tb_x0 = ox + w - margin - tb_w
    tb_x1 = ox + w - margin
    tb_y0 = margin
    tb_y1 = h - margin
    msp.add_line((tb_x0, tb_y0), (tb_x0, tb_y1), dxfattribs={"layer": "S-TITLEBLK"})

    pi = project.project_info
    sig = project.reporting.signatory
    rev = project.reporting.options.report_revision
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tb_h = tb_y1 - tb_y0
    splits = [
        (0.88, 1.00),
        (0.72, 0.88),
        (0.56, 0.72),
        (0.30, 0.56),
        (0.16, 0.30),
        (0.08, 0.16),
        (0.00, 0.08),
    ]
    for f0, _ in splits[:-1]:
        yy = tb_y0 + f0 * tb_h
        msp.add_line((tb_x0, yy), (tb_x1, yy), dxfattribs={"layer": "S-TITLEBLK"})

    pad = 4.0 * k
    tx = tb_x0 + pad
    tcx = 0.5 * (tb_x0 + tb_x1)

    def box_y(frac: float) -> float:
        return tb_y0 + frac * tb_h

    _add_text(msp, "STRUCTURAL AI OFFICE", tcx, box_y(0.955), 4.2 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)
    _add_text(msp, "NSCP 2015 / ACI 318 / NBCP PD 1096", tcx, box_y(0.915), 2.3 * k, "S-TEXT", TextEntityAlignment.MIDDLE_CENTER)

    _add_text(msp, "PROJECT:", tx, box_y(0.855), 2.1 * k, "S-TEXT")
    _add_text(msp, pi.project_name[:34], tx, box_y(0.815), 3.2 * k, "S-TITLEBLK")
    if len(pi.project_name) > 34:
        _add_text(msp, pi.project_name[34:68], tx, box_y(0.78), 2.8 * k, "S-TITLEBLK")
    _add_text(msp, f"CODE: {pi.project_id}", tx, box_y(0.742), 2.3 * k, "S-TEXT")

    _add_text(msp, "OWNER / CLIENT:", tx, box_y(0.695), 2.0 * k, "S-TEXT")
    _add_text(msp, (sig.project_owner or "TO BE SPECIFIED")[:34], tx, box_y(0.66), 2.6 * k, "S-TITLEBLK")
    _add_text(msp, "LOCATION:", tx, box_y(0.62), 2.0 * k, "S-TEXT")
    loc = (sig.project_address or pi.location or "PHILIPPINES")[:36]
    _add_text(msp, loc, tx, box_y(0.585), 2.4 * k, "S-TITLEBLK")

    _add_text(msp, "DESIGNED & SEALED BY:", tx, box_y(0.535), 2.1 * k, "S-TEXT")
    _add_text(msp, (sig.engineer_name or "ENGINEER OF RECORD")[:32], tx, box_y(0.495), 2.9 * k, "S-TITLEBLK")
    _add_text(msp, (sig.title or "Civil / Structural Engineer")[:36], tx, box_y(0.46), 2.1 * k, "S-TEXT")
    _add_text(msp, f"PRC NO.: {sig.prc_license_no or 'TBD'}", tx, box_y(0.42), 2.2 * k, "S-TEXT")
    _add_text(msp, f"VALID UNTIL: {sig.prc_validity or 'TBD'}", tx, box_y(0.39), 2.0 * k, "S-TEXT")
    _add_text(msp, f"PTR NO.: {sig.ptr_no or 'TBD'}", tx, box_y(0.36), 2.0 * k, "S-TEXT")
    _add_text(msp, f"PTR DATE/PLACE: {sig.ptr_date or '-'} / {sig.ptr_place or '-'}", tx, box_y(0.332), 1.85 * k, "S-TEXT")
    _add_text(msp, f"TIN: {sig.tin or 'TBD'}", tx, box_y(0.308), 2.0 * k, "S-TEXT")

    _add_text(msp, "SHEET CONTENTS:", tx, box_y(0.275), 2.0 * k, "S-TEXT")
    title_words = sheet_title.upper()
    _add_text(msp, title_words[:30], tx, box_y(0.232), 3.0 * k, "S-TITLEBLK")
    if len(title_words) > 30:
        _add_text(msp, title_words[30:62], tx, box_y(0.192), 2.7 * k, "S-TITLEBLK")

    _add_text(msp, f"SCALE: {scale_label}", tx, box_y(0.136), 2.1 * k, "S-TEXT")
    _add_text(msp, f"DATE: {date_str}", tx, box_y(0.112), 2.0 * k, "S-TEXT")
    _add_text(msp, rev[:34], tx, box_y(0.088), 1.8 * k, "S-TEXT")

    _add_text(msp, "SHEET NO.", tx, box_y(0.058), 2.0 * k, "S-TEXT")
    _add_text(msp, sheet_no, tcx, box_y(0.032), 7.5 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)

    return (ox + left_m + 6.0 * k, margin + 6.0 * k, tb_x0 - 6.0 * k, h - margin - 6.0 * k)


def _extract_grid_axes(project: ProjectState) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
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


def _draw_scale_bar_and_north(
    msp,
    box: tuple[float, float, float, float],
    eff_mm_per_m: float,
    k: float,
) -> None:
    """Draw a graphic scale bar (0, 1m, 2m, 5m) and crisp North arrow inside a plan viewport."""
    vx0, vy0, vx1, vy1 = box
    # Graphic scale bar at bottom-left above plan title
    sb_x = vx0 + 5.0 * k
    sb_y = vy0 + 9.5 * k
    bar_h = 1.6 * k
    max_bar_w = max((vx1 - vx0) * 0.30, 18.0 * k)
    marks_m = [0.0, 1.0, 2.0, 5.0]
    if 5.0 * eff_mm_per_m > max_bar_w:
        marks_m = [0.0, 1.0, 2.0]

    for m0, m1 in zip(marks_m, marks_m[1:]):
        sx0 = sb_x + m0 * eff_mm_per_m
        sx1 = sb_x + m1 * eff_mm_per_m
        _add_rect(msp, sx0, sb_y, sx1, sb_y + bar_h, "S-DIM")
    # Fill first 1m segment with hatch/cross line for classic architectural scale bar look
    if len(marks_m) >= 2:
        sx1 = sb_x + marks_m[1] * eff_mm_per_m
        msp.add_line((sb_x, sb_y + bar_h * 0.5), (sx1, sb_y + bar_h * 0.5), dxfattribs={"layer": "S-DIM"})
    for m_val in marks_m:
        sx = sb_x + m_val * eff_mm_per_m
        lbl = "0" if m_val == 0 else f"{m_val:g}m"
        _add_text(msp, lbl, sx, sb_y + bar_h + 0.9 * k, 1.55 * k, "S-DIM", TextEntityAlignment.BOTTOM_CENTER)

    # North Arrow at top-right of plan viewport
    nx = vx1 - 8.5 * k
    ny = vy1 - 10.0 * k
    r = 4.2 * k
    msp.add_circle((nx, ny), radius=r, dxfattribs={"layer": "S-TITLEBLK"})
    msp.add_lwpolyline(
        [(nx, ny + r * 0.95), (nx - r * 0.45, ny - r * 0.65), (nx, ny - r * 0.2), (nx + r * 0.45, ny - r * 0.65)],
        close=True,
        dxfattribs={"layer": "S-TITLEBLK"},
    )
    _add_text(msp, "N", nx, ny + r + 1.2 * k, 2.1 * k, "S-TITLEBLK", TextEntityAlignment.BOTTOM_CENTER)


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
) -> bool:
    """
    Render a scaled Foundation Plan or Floor Framing Plan inside `box` (mm).
    Returns True if the requested `plan_scale` physically overflowed the viewport box.
    """
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
    if mode == "foundation":
        for f in ad.footing_designs:
            nd = node_by_id.get(f.node_id)
            if nd is not None:
                all_x.extend([nd.x_m - 0.5 * f.b_m, nd.x_m + 0.5 * f.b_m])
                all_z.extend([nd.z_m - 0.5 * f.l_m, nd.z_m + 0.5 * f.l_m])
    if not all_x or not all_z:
        return False

    min_x, max_x = min(all_x), max(all_x)
    min_z, max_z = min(all_z), max(all_z)
    span_x = max(max_x - min_x, 2.0)
    span_z = max(max_z - min_z, 2.0)
    mid_x = 0.5 * (min_x + max_x)
    mid_z = 0.5 * (min_z + max_z)

    title_h = 13.0 * k
    pad_x = 24.0 * k
    pad_y = 24.0 * k
    avail_w = max(vw - 2.0 * pad_x, 16.0)
    avail_h = max(vh - title_h - 2.0 * pad_y, 16.0)

    nominal_mm_per_m = 1000.0 / float(plan_scale)
    fit_mm_per_m = min(avail_w / span_x, avail_h / span_z)
    overflowed = fit_mm_per_m < nominal_mm_per_m * 0.98
    eff_mm_per_m = min(nominal_mm_per_m, fit_mm_per_m)
    eff_scale = int(round(1000.0 / max(eff_mm_per_m, 1e-4)))

    if overflowed and warnings is not None:
        rec_scale = next((sc for sc in (75, 100, 125, 150, 200, 250, 300) if sc >= eff_scale), eff_scale)
        msg = (
            f"{title}: requested 1:{plan_scale} overflows viewport — auto-fitted to ~1:{eff_scale}. "
            f"Use 1:{rec_scale} or split onto another sheet."
        )
        if msg not in warnings:
            warnings.append(msg)

    cx = 0.5 * (vx0 + vx1)
    cy = vy0 + title_h + 0.5 * (vh - title_h)

    def to_xy(xm: float, zm: float) -> tuple[float, float]:
        return (cx + (xm - mid_x) * eff_mm_per_m, cy + (zm - mid_z) * eff_mm_per_m)

    # Elevation tag in title bar
    elev_tag = ""
    if mode == "foundation":
        if ad.footing_designs:
            min_tof = min(f.base_elevation_m for f in ad.footing_designs)
            elev_tag = f"  [TOF EL. {min_tof:+.2f}m]"
    elif floor_elev_m is not None:
        elev_tag = f"  [EL. {floor_elev_m:+.2f}m]"

    _add_text(
        msp,
        f"{title}{elev_tag}   (SCALE 1:{eff_scale})",
        cx,
        vy0 + 4.5 * k,
        3.0 * k,
        "S-TITLEBLK",
        TextEntityAlignment.MIDDLE_CENTER,
    )
    msp.add_line(
        (cx - min(vw * 0.40, 68.0 * k), vy0 + 2.0 * k),
        (cx + min(vw * 0.40, 68.0 * k), vy0 + 2.0 * k),
        dxfattribs={"layer": "S-TITLEBLK"},
    )

    # Graphic scale bar & North arrow
    _draw_scale_bar_and_north(msp, box, eff_mm_per_m, k)

    # 1. Anti-Collision Gridlines (S-GRID), Staggered Bubbles (S-GRID-BUBB), & Dimensions (S-DIM)
    ext = 8.5 * k
    bub_r = 3.0 * k
    bub_d = 2.0 * bub_r
    stagger_shift = 1.35 * bub_d
    px0, py0 = to_xy(min_x, min_z)
    px1, py1 = to_xy(max_x, max_z)

    # X gridlines (vertical) with anti-collision bubble staggering
    prev_gx: float | None = None
    stagger_x = False
    for lbl, xm in x_axes:
        gx, _ = to_xy(xm, min_z)
        msp.add_line((gx, py0 - ext), (gx, py1 + ext), dxfattribs={"layer": "S-GRID"})
        if prev_gx is not None and abs(gx - prev_gx) < 2.2 * bub_d:
            stagger_x = not stagger_x
        else:
            stagger_x = False
        prev_gx = gx

        extra_y = stagger_shift if stagger_x else 0.0
        dx_dog = 0.45 * bub_r if stagger_x else 0.0
        for sign_y, edge_y in ((1.0, py1 + ext), (-1.0, py0 - ext)):
            if stagger_x:
                knee_y = edge_y + sign_y * (0.45 * stagger_shift)
                by = edge_y + sign_y * (extra_y + bub_r)
                msp.add_line((gx, edge_y), (gx + dx_dog, knee_y), dxfattribs={"layer": "S-GRID-BUBB"})
                msp.add_line((gx + dx_dog, knee_y), (gx + dx_dog, by - sign_y * bub_r), dxfattribs={"layer": "S-GRID-BUBB"})
                bx_c = gx + dx_dog
            else:
                bx_c = gx
                by = edge_y + sign_y * bub_r
            msp.add_circle((bx_c, by), radius=bub_r, dxfattribs={"layer": "S-GRID-BUBB"})
            _add_text(msp, lbl, bx_c, by, 2.1 * k, "S-GRID-BUBB", TextEntityAlignment.MIDDLE_CENTER)

    # Z gridlines (horizontal) with anti-collision bubble staggering
    prev_gz: float | None = None
    stagger_z = False
    for lbl, zm in z_axes:
        _, gz = to_xy(min_x, zm)
        msp.add_line((px0 - ext, gz), (px1 + ext, gz), dxfattribs={"layer": "S-GRID"})
        if prev_gz is not None and abs(gz - prev_gz) < 2.2 * bub_d:
            stagger_z = not stagger_z
        else:
            stagger_z = False
        prev_gz = gz

        extra_x = stagger_shift if stagger_z else 0.0
        dy_dog = 0.45 * bub_r if stagger_z else 0.0
        for sign_x, edge_x in ((-1.0, px0 - ext), (1.0, px1 + ext)):
            if stagger_z:
                knee_x = edge_x + sign_x * (0.45 * stagger_shift)
                bx = edge_x + sign_x * (extra_x + bub_r)
                msp.add_line((edge_x, gz), (knee_x, gz + dy_dog), dxfattribs={"layer": "S-GRID-BUBB"})
                msp.add_line((knee_x, gz + dy_dog), (bx - sign_x * bub_r, gz + dy_dog), dxfattribs={"layer": "S-GRID-BUBB"})
                by_c = gz + dy_dog
            else:
                bx = edge_x + sign_x * bub_r
                by_c = gz
            msp.add_circle((bx, by_c), radius=bub_r, dxfattribs={"layer": "S-GRID-BUBB"})
            _add_text(msp, lbl, bx, by_c, 2.1 * k, "S-GRID-BUBB", TextEntityAlignment.MIDDLE_CENTER)

    # Bay dimensions along top (X) and left (Z)
    if len(x_axes) >= 2:
        dim_y = py1 + ext * 0.55
        msp.add_line((px0, dim_y), (px1, dim_y), dxfattribs={"layer": "S-DIM"})
        for (_, xa), (_, xb) in zip(x_axes, x_axes[1:]):
            gxa, _ = to_xy(xa, min_z)
            gxb, _ = to_xy(xb, min_z)
            msp.add_line((gxa, dim_y - 1.2 * k), (gxa, dim_y + 1.2 * k), dxfattribs={"layer": "S-DIM"})
            msp.add_line((gxb, dim_y - 1.2 * k), (gxb, dim_y + 1.2 * k), dxfattribs={"layer": "S-DIM"})
            if abs(gxb - gxa) >= 6.0 * k:
                _add_text(
                    msp,
                    f"{abs(xb - xa):.2f}m",
                    0.5 * (gxa + gxb),
                    dim_y + 1.6 * k,
                    1.8 * k,
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
            if abs(gzb - gza) >= 6.0 * k:
                _add_text(
                    msp,
                    f"{abs(zb - za):.2f}m",
                    dim_x - 1.6 * k,
                    0.5 * (gza + gzb),
                    1.8 * k,
                    "S-DIM",
                    TextEntityAlignment.BOTTOM_CENTER,
                    rotation=90.0,
                )

    # 2. Foundation Mode
    if mode == "foundation":
        for f in ad.footing_designs:
            nd = node_by_id.get(f.node_id)
            if nd is None:
                continue
            fx, fy = to_xy(nd.x_m, nd.z_m)
            hw = 0.5 * f.b_m * eff_mm_per_m
            hl = 0.5 * f.l_m * eff_mm_per_m
            _add_rect(msp, fx - hw, fy - hl, fx + hw, fy + hl, "S-FTG")
            _add_rect(
                msp,
                fx - hw * 0.88,
                fy - hl * 0.88,
                fx + hw * 0.88,
                fy + hl * 0.88,
                "S-FTG",
                linetype="DASHED",
            )
            _add_text(msp, f.footing_tag, fx + hw + 1.1 * k, fy + hl * 0.5, 2.1 * k, "S-FTG", TextEntityAlignment.LEFT)
            _add_text(
                msp,
                f"TOF EL.{f.base_elevation_m:+.2f}m",
                fx + hw + 1.1 * k,
                fy - hl * 0.35,
                1.55 * k,
                "S-TEXT",
                TextEntityAlignment.LEFT,
            )

        for m in sm.members:
            if m.member_type == "tie_beam":
                _draw_beam_entity(msp, m, node_by_id, sec_by_id, asgn_by_mid, to_xy, eff_mm_per_m, k)

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
        return overflowed

    # 3. Framing Plan Mode
    assert floor_level is not None
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

    # Slab tags (suppress on very tiny slivers < 1.8 m2)
    for panel in sm.slab_panels:
        if panel.floor_level != floor_level or panel.slab_action == "void":
            continue
        if panel.net_area_sqm < 1.8 or not panel.polygon_m:
            continue
        cx_m = sum(p[0] for p in panel.polygon_m) / len(panel.polygon_m)
        cz_m = sum(p[1] for p in panel.polygon_m) / len(panel.polygon_m)
        sx, sy = to_xy(cx_m, cz_m)
        sa = slab_by_pid.get(panel.panel_id)
        t_mm = sa.thickness_mm if sa else 125.0
        tag = sa.slab_tag if sa else "S1"
        _add_text(msp, f"{tag}-{t_mm:.0f}mm", sx, sy, 1.85 * k, "S-SLAB", TextEntityAlignment.MIDDLE_CENTER)

    # Floor openings
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
            1.75 * k,
            "S-OPENING",
            TextEntityAlignment.MIDDLE_CENTER,
        )

    return overflowed


def _draw_column_entity(msp, member, node, sec_by_id, asgn_by_mid, to_xy, eff_mm_per_m: float, k: float):
    asgn = asgn_by_mid.get(member.member_id)
    sec = sec_by_id.get(asgn.section_id) if asgn else None
    w_m = (sec.width_mm / 1000.0) if sec else 0.40
    d_m = (sec.depth_mm / 1000.0) if sec else 0.40
    sec_tag = sec.section_id if sec else "C1"

    cx, cy = to_xy(node.x_m, node.z_m)
    hw = max(0.5 * w_m * eff_mm_per_m, 1.25 * k)
    hd = max(0.5 * d_m * eff_mm_per_m, 1.25 * k)
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
        1.85 * k,
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

    half_w = max(0.5 * w_m * eff_mm_per_m, 0.7 * k)
    nx = -dy / seg_len
    ny = dx / seg_len

    msp.add_line((ax + nx * half_w, ay + ny * half_w), (bx + nx * half_w, by + ny * half_w), dxfattribs={"layer": "S-BEAM"})
    msp.add_line((ax - nx * half_w, ay - ny * half_w), (bx - nx * half_w, by - ny * half_w), dxfattribs={"layer": "S-BEAM"})
    msp.add_line((ax, ay), (bx, by), dxfattribs={"layer": "S-BEAM", "linetype": "CENTER"})

    # Suppress redundant beam text tags on short stub spans (< 1.4 m) to avoid collisions
    if member.length_m < 1.4:
        return

    ang = math.degrees(math.atan2(dy, dx))
    if ang > 90.0:
        ang -= 180.0
    elif ang < -90.0:
        ang += 180.0
    mx = 0.5 * (ax + bx) + nx * (half_w + 1.6 * k)
    my = 0.5 * (ay + by) + ny * (half_w + 1.6 * k)
    _add_text(msp, sec_tag, mx, my, 1.8 * k, "S-BEAM", TextEntityAlignment.MIDDLE_CENTER, rotation=ang)


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
    pad = 4.5 * k
    cur_y = y1 - 7.5 * k
    _add_text(msp, "GENERAL STRUCTURAL NOTES (NSCP 2015 / ACI 318)", x0 + pad, cur_y, 2.8 * k, "S-TITLEBLK")
    cur_y -= 3.2 * k
    msp.add_line((x0 + pad, cur_y), (x1 - pad, cur_y), dxfattribs={"layer": "S-SCHED"})
    cur_y -= 4.8 * k

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
        "   - Stagger lap splices away from plastic hinge zones.",
        "   - Minimum tension lap splice = 40db (Class B per ACI 318).",
        "   - Top beam bars spliced at midspan; bottom bars near supports.",
        "6. FOUNDATION & GEOTECHNICAL DESIGN:",
        f"   - Allowable soil bearing capacity qa = {dp.soil_bearing_qa_kpa:.1f} kPa.",
        f"   - Footing embedment Df = {dp.footing_embedment_m:.2f} m (Pedestal drop = {sm.options.footing_depth_m:.2f} m).",
        f"   - Ref: {(sig.geotech_report_ref or 'Verify bearing on site')[:48]}",
        "7. CONSTRUCTION VERIFICATION:",
        "   - Verify all dimensions on architectural plans prior to pouring.",
        "   - Notify Engineer of Record before pouring footings/columns/slabs.",
    ]

    avail_h = max(cur_y - (y0 + 4.0 * k), 20.0)
    line_step = min(4.3 * k, avail_h / max(len(notes), 1))
    txt_h = min(1.9 * k, line_step * 0.56)
    for line in notes:
        if cur_y < y0 + 4.0 * k:
            break
        is_hdr = line[0].isdigit()
        _add_text(
            msp,
            line,
            x0 + pad,
            cur_y,
            txt_h * (1.12 if is_hdr else 1.0),
            "S-TITLEBLK" if is_hdr else "S-TEXT",
        )
        cur_y -= line_step * (1.08 if is_hdr else 0.96)


def _draw_cad_table(
    msp,
    x0: float,
    y_top: float,
    total_w: float,
    max_h: float,
    k: float,
    title: str,
    headers: list[str],
    rows: list[list[str]],
    col_weights: list[float],
    max_rows: int = 8,
) -> float:
    title_h = 6.5 * k
    hdr_h = 6.5 * k
    shown_rows = rows[:max_rows]
    row_h = min(5.8 * k, max(3.6 * k, (max_h - title_h - hdr_h) / max(len(shown_rows), 1)))

    _add_text(msp, title, x0, y_top - 3.8 * k, 2.5 * k, "S-TITLEBLK")
    cur_y = y_top - title_h

    w_sum = sum(col_weights)
    col_ws = [total_w * (cw / w_sum) for cw in col_weights]
    col_xs = [x0]
    for cw in col_ws:
        col_xs.append(col_xs[-1] + cw)

    table_h = hdr_h + len(shown_rows) * row_h
    y_bot = cur_y - table_h

    _add_rect(msp, x0, y_bot, x0 + total_w, cur_y, "S-SCHED")
    for cx in col_xs[1:-1]:
        msp.add_line((cx, y_bot), (cx, cur_y), dxfattribs={"layer": "S-SCHED"})

    hdr_bot = cur_y - hdr_h
    msp.add_line((x0, hdr_bot), (x0 + total_w, hdr_bot), dxfattribs={"layer": "S-SCHED"})
    for j, htxt in enumerate(headers):
        _add_text(
            msp,
            htxt,
            0.5 * (col_xs[j] + col_xs[j + 1]),
            cur_y - 0.5 * hdr_h,
            1.85 * k,
            "S-TITLEBLK",
            TextEntityAlignment.MIDDLE_CENTER,
        )

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
                1.75 * k,
                "S-TEXT",
                TextEntityAlignment.MIDDLE_CENTER,
            )

    return y_bot


def _draw_schedule_item(msp, project: ProjectState, key: str, box: tuple[float, float, float, float], k: float) -> None:
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    ad = project.analysis_design

    if key == "schedule_footings":
        seen: dict[str, list[str]] = {}
        for f in ad.footing_designs:
            if f.footing_tag not in seen:
                seen[f.footing_tag] = [
                    f.footing_tag,
                    f"{f.b_m:.2f}x{f.l_m:.2f}x{f.thickness_mm:.0f}",
                    f.bottom_bars_x,
                    f.bottom_bars_z,
                    f"{f.base_elevation_m:+.2f}m",
                ]
        rows = [seen[t] for t in sorted(seen.keys())] or [["F1", "-", "-", "-", "0.00m"]]
        _draw_cad_table(
            msp, x0, y1, w, h, k, "SCHEDULE OF ISOLATED FOOTINGS",
            ["TAG", "B x L x t", "BOT BARS X", "BOT BARS Z", "TOF ELEV"],
            rows, [10, 22, 26, 26, 16],
        )
    elif key == "schedule_columns":
        seen = {}
        for c in ad.column_designs:
            if c.section_id not in seen:
                seen[c.section_id] = [
                    c.section_id,
                    f"{c.width_mm:.0f}x{c.depth_mm:.0f}",
                    c.main_bars,
                    c.ties_confined,
                    c.ties_mid,
                ]
        rows = [seen[t] for t in sorted(seen.keys())] or [["C1", "-", "-", "-", "-"]]
        _draw_cad_table(
            msp, x0, y1, w, h, k, "SCHEDULE OF COLUMNS & PEDESTALS",
            ["MARK", "SIZE (mm)", "MAIN BARS", "CONFINED TIES", "MID TIES"],
            rows, [12, 18, 18, 26, 26],
        )
    elif key == "schedule_beams":
        seen = {}
        for b in ad.beam_designs:
            if b.section_id not in seen:
                seen[b.section_id] = [
                    b.section_id,
                    f"{b.width_mm:.0f}x{b.depth_mm:.0f}",
                    b.bars_top_start,
                    b.bars_bot_mid,
                    b.bars_top_end,
                    b.stirrups_support,
                ]
        rows = [seen[t] for t in sorted(seen.keys())] or [["B1", "-", "-", "-", "-", "-"]]
        _draw_cad_table(
            msp, x0, y1, w, h, k, "SCHEDULE OF BEAMS & TIE BEAMS",
            ["MARK", "SIZE (mm)", "TOP START", "BOT MID", "TOP END", "STIRRUPS (SUP)"],
            rows, [10, 14, 16, 16, 16, 28],
        )
        return

    if key == "schedule_slabs_stairs":
        rows = [
            [
                s.panel_id,
                s.floor_level,
                f"{s.thickness_mm:.0f}mm",
                s.short_span_cont_neg,
                s.short_span_pos,
                s.long_span_pos,
            ]
            for s in ad.slab_designs[:12]
        ]
        for st in ad.stair_designs[:4]:
            rows.append([
                st.stair_id,
                f"Span {st.flight_span_m:.1f}m",
                f"{st.waist_thickness_mm:.0f}mm",
                st.main_bars,
                st.dist_bars,
                st.deflection_status,
            ])
        if not rows:
            rows = [["S1", "-", "125mm", "10mm @ 200mm", "10mm @ 200mm", "10mm @ 250mm"]]
        _draw_cad_table(
            msp, x0, y1, w, h, k, "SCHEDULE OF RC SLABS & STAIRS",
            ["MARK", "LEVEL / SPAN", "THK", "SHORT NEG / MAIN", "SHORT POS / DIST", "LONG POS / STAT"],
            rows, [14, 14, 10, 22, 20, 20],
        )


def _draw_detail_item(
    msp,
    project: ProjectState,
    key: str,
    box: tuple[float, float, float, float],
    k: float,
    detail_scale: int,
) -> None:
    x0, y0, x1, y1 = box
    w = max(x1 - x0, 20.0)
    h = max(y1 - y0, 20.0)
    cx = 0.5 * (x0 + x1)
    cy = y0 + h * 0.54
    ad = project.analysis_design
    dp = ad.params
    mm_per_mm_real = 1.0 / float(detail_scale)
    hk = 2.6 * k

    if key == "detail_column_section":
        rep_col = ad.column_designs[0] if ad.column_designs else None
        cb_mm = rep_col.width_mm if rep_col else 400.0
        ch_mm = rep_col.depth_mm if rep_col else 400.0
        n_cbars, d_cbar = parse_bar_callout(rep_col.main_bars if rep_col else "8-20mm", 8, dp.main_bar_dia_col_mm)

        c_scale = min(mm_per_mm_real, (min(w, h) * 0.44) / max(cb_mm, ch_mm, 200.0))
        cw_d, ch_d = cb_mm * c_scale, ch_mm * c_scale
        cov_d = dp.clear_cover_column_mm * c_scale

        _add_rect(msp, cx - cw_d / 2, cy - ch_d / 2, cx + cw_d / 2, cy + ch_d / 2, "S-COL")
        tx0, ty0 = cx - cw_d / 2 + cov_d, cy - ch_d / 2 + cov_d
        tx1, ty1 = cx + cw_d / 2 - cov_d, cy + ch_d / 2 - cov_d
        _add_rect(msp, tx0, ty0, tx1, ty1, "S-REBAR")
        msp.add_line((tx1, ty1), (tx1 - hk, ty1 - hk * 0.6), dxfattribs={"layer": "S-REBAR"})
        msp.add_line((tx1, ty1), (tx1 - hk * 0.6, ty1 - hk), dxfattribs={"layer": "S-REBAR"})

        bar_r = max(0.85 * k, (d_cbar * 0.5) * c_scale)
        pts = [(tx0 + bar_r, ty0 + bar_r), (tx1 - bar_r, ty0 + bar_r), (tx1 - bar_r, ty1 - bar_r), (tx0 + bar_r, ty1 - bar_r)]
        if n_cbars >= 8:
            pts += [(cx, ty0 + bar_r), (cx, ty1 - bar_r), (tx0 + bar_r, cy), (tx1 - bar_r, cy)]
        for bx, by in pts:
            msp.add_circle((bx, by), radius=bar_r, dxfattribs={"layer": "S-REBAR"})

        msp.add_line((cx - cw_d / 2, cy - ch_d / 2 - 4 * k), (cx + cw_d / 2, cy - ch_d / 2 - 4 * k), dxfattribs={"layer": "S-DIM"})
        _add_text(msp, f"{cb_mm:.0f}mm", cx, cy - ch_d / 2 - 5.8 * k, 1.75 * k, "S-DIM", TextEntityAlignment.TOP_CENTER)
        msp.add_line((cx - cw_d / 2 - 4 * k, cy - ch_d / 2), (cx - cw_d / 2 - 4 * k, cy + ch_d / 2), dxfattribs={"layer": "S-DIM"})
        _add_text(msp, f"{ch_mm:.0f}mm", cx - cw_d / 2 - 5.8 * k, cy, 1.75 * k, "S-DIM", TextEntityAlignment.BOTTOM_CENTER, rotation=90.0)
        _add_text(msp, f"MAIN: {rep_col.main_bars if rep_col else '8-20mm'}", cx, cy + ch_d / 2 + 4.8 * k, 1.75 * k, "S-REBAR", TextEntityAlignment.BOTTOM_CENTER)
        _add_text(msp, f"TIES: {rep_col.ties_confined if rep_col else '10mm @ 100mm o.c.'}", cx, cy + ch_d / 2 + 2.0 * k, 1.65 * k, "S-TEXT", TextEntityAlignment.BOTTOM_CENTER)
        _add_text(msp, f"TYPICAL RC COLUMN SECTION (1:{detail_scale})", cx, y0 + 3.5 * k, 2.1 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)
        return

    if key == "detail_beam_section":
        rep_bm = ad.beam_designs[0] if ad.beam_designs else None
        bb_mm = rep_bm.width_mm if rep_bm else 300.0
        bh_mm = rep_bm.depth_mm if rep_bm else 500.0
        n_top, d_top = parse_bar_callout(rep_bm.bars_top_start if rep_bm else "3-16mm", 3, dp.main_bar_dia_beam_mm)
        n_bot, _ = parse_bar_callout(rep_bm.bars_bot_mid if rep_bm else "3-16mm", 3, dp.main_bar_dia_beam_mm)

        b_scale = min(mm_per_mm_real, (min(w, h) * 0.44) / max(bb_mm, bh_mm, 200.0))
        bw_d, bh_d = bb_mm * b_scale, bh_mm * b_scale
        bcov_d = dp.clear_cover_beam_mm * b_scale

        _add_rect(msp, cx - bw_d / 2, cy - bh_d / 2, cx + bw_d / 2, cy + bh_d / 2, "S-BEAM")
        stx0, sty0 = cx - bw_d / 2 + bcov_d, cy - bh_d / 2 + bcov_d
        stx1, sty1 = cx + bw_d / 2 - bcov_d, cy + bh_d / 2 - bcov_d
        _add_rect(msp, stx0, sty0, stx1, sty1, "S-REBAR")
        msp.add_line((stx1, sty1), (stx1 - hk, sty1 - hk * 0.6), dxfattribs={"layer": "S-REBAR"})

        br = max(0.8 * k, (d_top * 0.5) * b_scale)
        for row_n, ry in ((max(2, min(n_top, 5)), sty1 - br), (max(2, min(n_bot, 5)), sty0 + br)):
            for i in range(row_n):
                frac = i / max(row_n - 1, 1)
                rx = (stx0 + br) + frac * max((stx1 - br) - (stx0 + br), 0.1)
                msp.add_circle((rx, ry), radius=br, dxfattribs={"layer": "S-REBAR"})

        msp.add_line((cx - bw_d / 2, cy - bh_d / 2 - 4 * k), (cx + bw_d / 2, cy - bh_d / 2 - 4 * k), dxfattribs={"layer": "S-DIM"})
        _add_text(msp, f"{bb_mm:.0f}mm", cx, cy - bh_d / 2 - 5.8 * k, 1.75 * k, "S-DIM", TextEntityAlignment.TOP_CENTER)
        msp.add_line((cx - bw_d / 2 - 4 * k, cy - bh_d / 2), (cx - bw_d / 2 - 4 * k, cy + bh_d / 2), dxfattribs={"layer": "S-DIM"})
        _add_text(msp, f"{bh_mm:.0f}mm", cx - bw_d / 2 - 5.8 * k, cy, 1.75 * k, "S-DIM", TextEntityAlignment.BOTTOM_CENTER, rotation=90.0)
        _add_text(msp, f"TOP: {rep_bm.bars_top_start if rep_bm else '3-16mm'} | BOT: {rep_bm.bars_bot_mid if rep_bm else '3-16mm'}", cx, cy + bh_d / 2 + 4.8 * k, 1.7 * k, "S-REBAR", TextEntityAlignment.BOTTOM_CENTER)
        _add_text(msp, f"STIRRUPS: {rep_bm.stirrups_support if rep_bm else '10mm @ 100mm o.c.'}", cx, cy + bh_d / 2 + 2.0 * k, 1.65 * k, "S-TEXT", TextEntityAlignment.BOTTOM_CENTER)
        _add_text(msp, f"TYPICAL RC BEAM SECTION (1:{detail_scale})", cx, y0 + 3.5 * k, 2.1 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)
        return

    if key == "detail_footing_section":
        rep_ftg = ad.footing_designs[0] if ad.footing_designs else None
        rep_col = ad.column_designs[0] if ad.column_designs else None
        cb_mm = rep_col.width_mm if rep_col else 400.0
        fb_mm = (rep_ftg.b_m * 1000.0) if rep_ftg else 1800.0
        ft_mm = rep_ftg.thickness_mm if rep_ftg else 400.0
        df_mm = max(dp.footing_embedment_m * 1000.0, ft_mm + 400.0)

        f_cx_plan = x0 + w * 0.27
        f_cx_sec = x0 + w * 0.74
        f_scale = min(mm_per_mm_real, (w * 0.36) / max(fb_mm, 1000.0), (h * 0.44) / max(df_mm, fb_mm))
        fw_d = fb_mm * f_scale
        ft_d = max(ft_mm * f_scale, 4.0 * k)
        df_d = min(df_mm * f_scale, h * 0.50)
        ped_d = max(cb_mm * f_scale, 4.0 * k)

        _add_rect(msp, f_cx_plan - fw_d / 2, cy - fw_d / 2, f_cx_plan + fw_d / 2, cy + fw_d / 2, "S-FTG")
        _add_rect(msp, f_cx_plan - ped_d / 2, cy - ped_d / 2, f_cx_plan + ped_d / 2, cy + ped_d / 2, "S-COL")
        fcov = max(dp.clear_cover_footing_mm * f_scale, 1.4 * k)
        for i in range(5):
            frac = i / 4.0
            pos_x = (f_cx_plan - fw_d / 2 + fcov) + frac * (fw_d - 2 * fcov)
            pos_y = (cy - fw_d / 2 + fcov) + frac * (fw_d - 2 * fcov)
            msp.add_line((pos_x, cy - fw_d / 2 + fcov), (pos_x, cy + fw_d / 2 - fcov), dxfattribs={"layer": "S-REBAR"})
            msp.add_line((f_cx_plan - fw_d / 2 + fcov, pos_y), (f_cx_plan + fw_d / 2 - fcov, pos_y), dxfattribs={"layer": "S-REBAR"})
        _add_text(msp, "PLAN", f_cx_plan, cy - fw_d / 2 - 4.0 * k, 1.75 * k, "S-TEXT", TextEntityAlignment.TOP_CENTER)

        sec_bot = cy - df_d * 0.45
        sec_ftg_top = sec_bot + ft_d
        sec_ngl = sec_bot + df_d
        _add_rect(msp, f_cx_sec - fw_d / 2, sec_bot, f_cx_sec + fw_d / 2, sec_ftg_top, "S-FTG")
        _add_rect(msp, f_cx_sec - ped_d / 2, sec_ftg_top, f_cx_sec + ped_d / 2, sec_ngl + 2.5 * k, "S-COL")
        msp.add_line((f_cx_sec - fw_d * 0.62, sec_ngl), (f_cx_sec + fw_d * 0.62, sec_ngl), dxfattribs={"layer": "S-DIM", "linetype": "DASHED"})
        _add_text(msp, "NGL/FFL", f_cx_sec + fw_d * 0.64, sec_ngl, 1.5 * k, "S-DIM", TextEntityAlignment.MIDDLE_LEFT)
        ry = sec_bot + max(fcov, 1.4 * k)
        msp.add_lwpolyline(
            [
                (f_cx_sec - fw_d / 2 + fcov, sec_ftg_top - fcov * 0.5),
                (f_cx_sec - fw_d / 2 + fcov, ry),
                (f_cx_sec + fw_d / 2 - fcov, ry),
                (f_cx_sec + fw_d / 2 - fcov, sec_ftg_top - fcov * 0.5),
            ],
            dxfattribs={"layer": "S-REBAR"},
        )
        _add_text(msp, f"Df={dp.footing_embedment_m:.2f}m | t={ft_mm:.0f}mm", f_cx_sec, sec_bot - 4.0 * k, 1.7 * k, "S-DIM", TextEntityAlignment.TOP_CENTER)
        _add_text(msp, f"BOT: {rep_ftg.bottom_bars_x if rep_ftg else '9-16mm @ 175mm'} B.W.", cx, sec_ngl + 4.5 * k, 1.7 * k, "S-REBAR", TextEntityAlignment.BOTTOM_CENTER)
        _add_text(msp, f"TYPICAL ISOLATED FOOTING ({rep_ftg.footing_tag if rep_ftg else 'F1'}) (1:{detail_scale})", cx, y0 + 3.5 * k, 2.1 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)
        return

    if key == "detail_beam_longitudinal":
        # Continuous RC Beam SMRF Longitudinal Elevation
        rep_bm = ad.beam_designs[0] if ad.beam_designs else None
        span_d = w * 0.72
        col_w = w * 0.08
        bm_h = min(h * 0.28, 22.0 * k)
        col_ext = bm_h * 0.65

        bx0 = cx - span_d * 0.5
        bx1 = cx + span_d * 0.5
        by_bot = cy - bm_h * 0.5
        by_top = cy + bm_h * 0.5

        # End supporting columns
        _add_rect(msp, bx0 - col_w, by_bot - col_ext, bx0, by_top + col_ext, "S-COL")
        _add_rect(msp, bx1, by_bot - col_ext, bx1 + col_w, by_top + col_ext, "S-COL")
        # Beam concrete profile
        msp.add_line((bx0, by_top), (bx1, by_top), dxfattribs={"layer": "S-BEAM"})
        msp.add_line((bx0, by_bot), (bx1, by_bot), dxfattribs={"layer": "S-BEAM"})

        cov = bm_h * 0.14
        top_y = by_top - cov
        bot_y = by_bot + cov
        # Top & bottom continuous bars with 90-deg hooks into columns
        msp.add_lwpolyline(
            [(bx0 - col_w * 0.65, by_bot + cov), (bx0 - col_w * 0.65, top_y), (bx1 + col_w * 0.65, top_y), (bx1 + col_w * 0.65, by_bot + cov)],
            dxfattribs={"layer": "S-REBAR"},
        )
        msp.add_lwpolyline(
            [(bx0 - col_w * 0.45, by_top - cov), (bx0 - col_w * 0.45, bot_y), (bx1 + col_w * 0.45, bot_y), (bx1 + col_w * 0.45, by_top - cov)],
            dxfattribs={"layer": "S-REBAR"},
        )
        # Top extra negative bars extending L/4 past column faces
        q_len = span_d * 0.25
        msp.add_line((bx0 - col_w * 0.5, top_y - cov * 0.7), (bx0 + q_len, top_y - cov * 0.7), dxfattribs={"layer": "S-REBAR"})
        msp.add_line((bx1 - q_len, top_y - cov * 0.7), (bx1 + col_w * 0.5, top_y - cov * 0.7), dxfattribs={"layer": "S-REBAR"})

        # Seismic stirrup zones: dense 2h ends vs wider midspan
        zone_2h = span_d * 0.24
        for sx in (
            [bx0 + (i + 1) * (zone_2h / 6.0) for i in range(6)]
            + [bx0 + zone_2h + (i + 1) * ((span_d - 2.0 * zone_2h) / 5.0) for i in range(4)]
            + [bx1 - zone_2h + i * (zone_2h / 6.0) for i in range(6)]
        ):
            msp.add_line((sx, bot_y), (sx, top_y), dxfattribs={"layer": "S-REBAR"})

        # Dimension & zone callouts
        dim_y = by_top + col_ext + 3.0 * k
        msp.add_line((bx0, dim_y), (bx0 + q_len, dim_y), dxfattribs={"layer": "S-DIM"})
        _add_text(msp, "L/4 TOP EXTRA", bx0 + q_len * 0.5, dim_y + 1.2 * k, 1.55 * k, "S-DIM", TextEntityAlignment.BOTTOM_CENTER)
        msp.add_line((bx1 - q_len, dim_y), (bx1, dim_y), dxfattribs={"layer": "S-DIM"})
        _add_text(msp, "L/4 TOP EXTRA", bx1 - q_len * 0.5, dim_y + 1.2 * k, 1.55 * k, "S-DIM", TextEntityAlignment.BOTTOM_CENTER)

        dim_bot = by_bot - col_ext - 3.0 * k
        _add_text(msp, f"2h: {rep_bm.stirrups_support if rep_bm else '10mm @ 100mm'}", bx0 + zone_2h * 0.5, dim_bot, 1.5 * k, "S-REBAR", TextEntityAlignment.TOP_CENTER)
        _add_text(msp, f"MID: {rep_bm.stirrups_midspan if rep_bm else '10mm @ 200mm'}", cx, dim_bot, 1.5 * k, "S-TEXT", TextEntityAlignment.TOP_CENTER)
        _add_text(msp, f"SMRF BEAM LONGITUDINAL ELEVATION (1:{detail_scale})", cx, y0 + 3.5 * k, 2.1 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)
        return

    if key == "detail_column_longitudinal":
        # SMRF Column & Footing Pedestal Longitudinal Elevation
        rep_col = ad.column_designs[0] if ad.column_designs else None
        col_h = h * 0.64
        col_w = min(w * 0.16, 14.0 * k)
        ftg_w = col_w * 3.2
        ftg_h = col_h * 0.14
        col_bot = y0 + 13.0 * k
        ftg_top = col_bot + ftg_h
        col_top = col_bot + col_h

        # Footing base & column shaft
        _add_rect(msp, cx - ftg_w * 0.5, col_bot, cx + ftg_w * 0.5, ftg_top, "S-FTG")
        _add_rect(msp, cx - col_w * 0.5, ftg_top, cx + col_w * 0.5, col_top, "S-COL")
        # Slab/beam joint stub at top
        beam_h = col_h * 0.12
        msp.add_line((cx - ftg_w * 0.45, col_top), (cx + ftg_w * 0.45, col_top), dxfattribs={"layer": "S-BEAM"})
        msp.add_line((cx - ftg_w * 0.45, col_top - beam_h), (cx - col_w * 0.5, col_top - beam_h), dxfattribs={"layer": "S-BEAM"})
        msp.add_line((cx + col_w * 0.5, col_top - beam_h), (cx + ftg_w * 0.45, col_top - beam_h), dxfattribs={"layer": "S-BEAM"})
        # NGL / Grade line
        ngl_y = ftg_top + col_h * 0.20
        msp.add_line((cx - ftg_w * 0.60, ngl_y), (cx + ftg_w * 0.60, ngl_y), dxfattribs={"layer": "S-DIM", "linetype": "DASHED"})
        _add_text(msp, f"NGL (Df={dp.footing_embedment_m:.2f}m)", cx + ftg_w * 0.62, ngl_y, 1.5 * k, "S-DIM", TextEntityAlignment.MIDDLE_LEFT)

        # Vertical main bars with L-dowel hooks in footing & staggered Class B lap splice at mid-height
        cov = col_w * 0.16
        bar_lx = cx - col_w * 0.5 + cov
        bar_rx = cx + col_w * 0.5 - cov
        msp.add_lwpolyline([(bar_lx - col_w * 0.55, col_bot + ftg_h * 0.35), (bar_lx, col_bot + ftg_h * 0.35), (bar_lx, col_top)], dxfattribs={"layer": "S-REBAR"})
        msp.add_lwpolyline([(bar_rx + col_w * 0.55, col_bot + ftg_h * 0.35), (bar_rx, col_bot + ftg_h * 0.35), (bar_rx, col_top)], dxfattribs={"layer": "S-REBAR"})

        # Staggered Class B lap splice indicator in middle half
        mid_y = 0.5 * (ftg_top + col_top - beam_h)
        lap_h = col_h * 0.16
        msp.add_line((bar_lx + 0.9 * k, mid_y - lap_h * 0.5), (bar_lx + 0.9 * k, mid_y + lap_h * 0.5), dxfattribs={"layer": "S-REBAR"})
        msp.add_line((bar_rx - 0.9 * k, mid_y - lap_h * 0.3), (bar_rx - 0.9 * k, mid_y + lap_h * 0.7), dxfattribs={"layer": "S-REBAR"})
        _add_text(msp, "CLASS B LAP (40db)", cx + col_w * 0.75, mid_y, 1.55 * k, "S-REBAR", TextEntityAlignment.MIDDLE_LEFT)

        # Confinement ties in Lo zones (bottom & top) vs wider mid-height ties
        lo_h = col_h * 0.22
        tie_ys = (
            [ftg_top + (i + 1) * (lo_h / 5.0) for i in range(5)]
            + [ftg_top + lo_h + (i + 1) * ((col_top - beam_h - ftg_top - 2 * lo_h) / 4.0) for i in range(3)]
            + [col_top - beam_h - lo_h + i * (lo_h / 5.0) for i in range(5)]
        )
        for ty in tie_ys:
            msp.add_line((bar_lx, ty), (bar_rx, ty), dxfattribs={"layer": "S-REBAR"})

        _add_text(msp, f"Lo: {rep_col.ties_confined if rep_col else '10mm @ 100mm'}", cx - col_w * 0.75, col_top - beam_h - lo_h * 0.5, 1.5 * k, "S-DIM", TextEntityAlignment.MIDDLE_RIGHT)
        _add_text(msp, f"MID: {rep_col.ties_mid if rep_col else '10mm @ 150mm'}", cx - col_w * 0.75, mid_y, 1.5 * k, "S-TEXT", TextEntityAlignment.MIDDLE_RIGHT)
        _add_text(msp, f"SMRF COLUMN & PEDESTAL ELEVATION (1:{detail_scale})", cx, y0 + 3.5 * k, 2.1 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)
        return

    if key == "detail_retaining_wall":
        rw = ad.retaining_wall_designs[0] if ad.retaining_wall_designs else None
        rw_in = ad.retaining_wall_inputs[0] if ad.retaining_wall_inputs else None
        base_w = min(w * 0.56, 48.0 * k)
        base_h = h * 0.12
        stem_h = h * 0.52
        stem_w = base_w * 0.18
        bx0 = cx - base_w * 0.5
        by0 = y0 + 13.0 * k
        toe_w = base_w * 0.28
        sx0 = bx0 + toe_w
        # Inverted-T retaining wall profile
        _add_rect(msp, bx0, by0, bx0 + base_w, by0 + base_h, "S-FTG")
        _add_rect(msp, sx0, by0 + base_h, sx0 + stem_w, by0 + base_h + stem_h, "S-COL")
        # Vertical & base rebar lines
        msp.add_line((sx0 + stem_w * 0.25, by0 + base_h * 0.35), (sx0 + stem_w * 0.25, by0 + base_h + stem_h - 1.5 * k), dxfattribs={"layer": "S-REBAR"})
        msp.add_line((sx0 + stem_w * 0.75, by0 + base_h * 0.35), (sx0 + stem_w * 0.75, by0 + base_h + stem_h - 1.5 * k), dxfattribs={"layer": "S-REBAR"})
        msp.add_line((bx0 + 1.5 * k, by0 + base_h * 0.35), (bx0 + base_w - 1.5 * k, by0 + base_h * 0.35), dxfattribs={"layer": "S-REBAR"})
        msp.add_line((bx0 + 1.5 * k, by0 + base_h * 0.70), (bx0 + base_w - 1.5 * k, by0 + base_h * 0.70), dxfattribs={"layer": "S-REBAR"})
        h_lbl = f"H={rw.retained_height_m:.2f}m" if rw else "H=2.80m"
        v_lbl = rw.stem_vertical_bars if rw else "12mm @ 150mm o.c."
        b_lbl = rw.base_heel_toe_bars if rw else "12mm @ 175mm o.c."
        _add_text(msp, f"{h_lbl} · VERT: {v_lbl}", cx + base_w * 0.08, by0 + base_h + stem_h * 0.65, 1.5 * k, "S-REBAR", TextEntityAlignment.MIDDLE_LEFT)
        _add_text(msp, f"BASE: {b_lbl}", cx, by0 - 3.2 * k, 1.5 * k, "S-DIM", TextEntityAlignment.TOP_CENTER)
        wall_tag = rw_in.wall_id if rw_in else "RW-1"
        _add_text(msp, f"BASEMENT RETAINING WALL {wall_tag} SECTION (1:{detail_scale})", cx, y0 + 3.5 * k, 2.0 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)
        return

    if key == "detail_roof_truss":
        tr = ad.roof_truss_designs[0] if ad.roof_truss_designs else None
        tw = w * 0.80
        th = h * 0.36
        tx0 = cx - tw * 0.5
        tx1 = cx + tw * 0.5
        ty0 = y0 + 18.0 * k
        ty_apex = ty0 + th
        # Bottom chord + pitched top chords
        msp.add_line((tx0, ty0), (tx1, ty0), dxfattribs={"layer": "S-BEAM"})
        msp.add_line((tx0, ty0), (cx, ty_apex), dxfattribs={"layer": "S-BEAM"})
        msp.add_line((cx, ty_apex), (tx1, ty0), dxfattribs={"layer": "S-BEAM"})
        # King post & web diagonals (4 panels)
        msp.add_line((cx, ty0), (cx, ty_apex), dxfattribs={"layer": "S-REBAR"})
        for frac in (0.25, 0.75):
            px = tx0 + frac * tw
            py_top = ty0 + (1.0 - abs(frac - 0.5) * 2.0) * th
            msp.add_line((px, ty0), (px, py_top), dxfattribs={"layer": "S-REBAR"})
            msp.add_line((px, py_top), (cx, ty0), dxfattribs={"layer": "S-REBAR"})
        tc = tr.top_chord_section if tr else "2L 75x75x6"
        bc = tr.bottom_chord_section if tr else "2L 65x65x6"
        wb = tr.web_section if tr else "2L 50x50x6"
        sp_lbl = f"L={tr.span_m:.1f}m ({tr.truss_type})" if tr else "L=8.0m (Pratt)"
        _add_text(msp, f"TOP: {tc} | BOT: {bc} | WEB: {wb}", cx, ty0 - 4.0 * k, 1.5 * k, "S-TEXT", TextEntityAlignment.TOP_CENTER)
        _add_text(msp, f"STEEL ROOF TRUSS ELEVATION - {sp_lbl}", cx, y0 + 3.5 * k, 2.0 * k, "S-TITLEBLK", TextEntityAlignment.MIDDLE_CENTER)


def _compose_sheet_items(
    msp,
    project: ProjectState,
    sheet_cfg: CadSheetConfig,
    content_box: tuple[float, float, float, float],
    k: float,
    eff_plan_scale: int,
    detail_scale: int,
    warnings: list[str],
) -> tuple[list[str], bool]:
    """
    Pack all items assigned to `sheet_cfg` into a clean anti-collision multi-row layout
    inside `content_box`. Returns (contents_descriptions, overflow_flag).
    """
    cx0, cy0, cx1, cy1 = content_box
    tot_w = max(cx1 - cx0, 20.0)
    tot_h = max(cy1 - cy0, 20.0)
    gap = 6.0 * k

    lvl_by_id = {lv.id: lv for lv in project.levels}
    items = list(sheet_cfg.items)
    has_notes = "general_notes" in items
    plan_items = [it for it in items if it == "foundation_plan" or it.startswith("floor_plan:")]
    sched_items = [it for it in items if it in SCHEDULE_KEYS]
    det_items = [it for it in items if it in DETAIL_KEYS]

    contents: list[str] = []
    overflow = False

    # 1. Allocate left column for General Notes if present alongside other items
    work_x0 = cx0
    if has_notes:
        if plan_items or sched_items or det_items:
            notes_w = min(tot_w * 0.30, 215.0 * k)
            _draw_general_notes(msp, project, (cx0, cy0, cx0 + notes_w, cy1), k)
            work_x0 = cx0 + notes_w + gap
        else:
            _draw_general_notes(msp, project, (cx0, cy0, cx1, cy1), k)
            work_x0 = cx1
        contents.append("NSCP 2015 / ACI 318 General Structural Notes")

    rem_w = max(cx1 - work_x0, 20.0)
    has_plans = len(plan_items) > 0
    has_scheds = len(sched_items) > 0
    has_dets = len(det_items) > 0

    # 2. Allocate vertical bands cleanly among Plans, Schedules, and Details
    bands: list[tuple[str, float]] = []
    if has_plans:
        bands.append(("plans", 1.25 if (has_scheds or has_dets) else 1.0))
    if has_scheds:
        bands.append(("scheds", 1.05 if has_dets else 1.0))
    if has_dets:
        bands.append(("dets", 0.95))

    band_boxes: dict[str, tuple[float, float, float, float]] = {}
    if bands:
        w_sum = sum(bw for _, bw in bands)
        usable_h = tot_h - gap * max(len(bands) - 1, 0)
        cur_top = cy1
        for name, weight in bands:
            bh = usable_h * (weight / w_sum)
            band_boxes[name] = (work_x0, cur_top - bh, cx1, cur_top)
            cur_top -= bh + gap

    # 3. Render Plans in multi-row viewport grid
    if has_plans:
        px0, py0, px1, py1 = band_boxes["plans"]
        pw = px1 - px0
        ph = py1 - py0
        n_p = len(plan_items)
        if n_p == 1:
            n_cols, n_rows = 1, 1
        elif n_p == 2:
            # Side-by-side on wide landscape box, stacked if tall/narrow
            n_cols, n_rows = (2, 1) if pw >= ph * 1.1 else (1, 2)
        elif n_p <= 4:
            n_cols, n_rows = 2, 2
        elif n_p <= 6:
            n_cols, n_rows = 3, 2
        else:
            n_cols = 3
            n_rows = int(math.ceil(n_p / 3.0))

        cell_w = (pw - gap * (n_cols - 1)) / n_cols
        cell_h = (ph - gap * (n_rows - 1)) / n_rows

        for i, pkey in enumerate(plan_items):
            r = i // n_cols
            c = i % n_cols
            bx0 = px0 + c * (cell_w + gap)
            bx1 = bx0 + cell_w
            by1 = py1 - r * (cell_h + gap)
            by0 = by1 - cell_h

            if pkey == "foundation_plan":
                ptitle = "FOUNDATION & TIE-BEAM PLAN"
                ov = _draw_plan_in_box(
                    msp, project, (bx0, by0, bx1, by1), k, eff_plan_scale, ptitle, mode="foundation", warnings=warnings
                )
                overflow = overflow or ov
                contents.append("Foundation & Tie-Beam Plan")
            else:
                lvl_id = pkey.split(":", 1)[1]
                lv = lvl_by_id.get(lvl_id)
                elev = lv.elevation_m if lv is not None else None
                ptitle = f"{lvl_id} FRAMING PLAN"
                ov = _draw_plan_in_box(
                    msp, project, (bx0, by0, bx1, by1), k, eff_plan_scale, ptitle,
                    mode="framing", floor_level=lvl_id, floor_elev_m=elev, warnings=warnings,
                )
                overflow = overflow or ov
                contents.append(f"{lvl_id} Framing Plan")

    # 4. Render RC Schedules cleanly (paired top row + full-width beam schedule when all 3 present)
    if has_scheds:
        sx0, sy0, sx1, sy1 = band_boxes["scheds"]
        sw = sx1 - sx0
        sh = sy1 - sy0
        if len(sched_items) == 3 and "schedule_beams" in sched_items:
            top_pair = [s for s in sched_items if s != "schedule_beams"]
            half_w = (sw - gap) * 0.5
            top_h = (sh - gap) * 0.5
            _draw_schedule_item(msp, project, top_pair[0], (sx0, sy1 - top_h, sx0 + half_w, sy1), k)
            _draw_schedule_item(msp, project, top_pair[1], (sx0 + half_w + gap, sy1 - top_h, sx1, sy1), k)
            _draw_schedule_item(msp, project, "schedule_beams", (sx0, sy0, sx1, sy1 - top_h - gap), k)
        else:
            n_s = len(sched_items)
            row_h = (sh - gap * (n_s - 1)) / n_s
            for i, skey in enumerate(sched_items):
                ry1 = sy1 - i * (row_h + gap)
                _draw_schedule_item(msp, project, skey, (sx0, ry1 - row_h, sx1, ry1), k)
        for skey in sched_items:
            contents.append(skey.replace("_", " ").title())

    # 5. Render Typical & SMRF Longitudinal Details in multi-column / multi-row detail grid
    if has_dets:
        dx0, dy0, dx1, dy1 = band_boxes["dets"]
        dw = dx1 - dx0
        dh = dy1 - dy0
        n_d = len(det_items)
        if n_d <= 3:
            d_cols, d_rows = n_d, 1
        elif n_d <= 5:
            d_cols, d_rows = (n_d, 1) if dw >= dh * 2.2 else (3, 2)
        else:
            d_cols = 3
            d_rows = int(math.ceil(n_d / 3.0))

        cell_w = (dw - gap * (d_cols - 1)) / d_cols
        cell_h = (dh - gap * (d_rows - 1)) / d_rows
        for i, dkey in enumerate(det_items):
            r = i // d_cols
            c = i % d_cols
            bx0 = dx0 + c * (cell_w + gap)
            bx1 = bx0 + cell_w
            by1 = dy1 - r * (cell_h + gap)
            by0 = by1 - cell_h
            _draw_detail_item(msp, project, dkey, (bx0, by0, bx1, by1), k, detail_scale)
            contents.append(dkey.replace("_", " ").title())

    return contents, overflow


def _build_dxf_for_sheet_configs(
    project: ProjectState,
    opts: CadDrawingOptions,
    sheet_configs: list[CadSheetConfig],
) -> tuple[ezdxf.document.Drawing, CadDrawingState, dict[str, tuple[float, float, float, float]]]:
    w, h = SHEET_DIMENSIONS_MM[opts.sheet_size]
    k = w / 841.0

    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()

    layer_names: list[str] = []
    for lname, aci, ltype, lw in CAD_LAYER_SPECS:
        if lname not in doc.layers:
            doc.layers.add(lname, color=aci, linetype=ltype, lineweight=lw)
        layer_names.append(lname)

    sheets: list[CadSheetSummary] = []
    sheet_bounds: dict[str, tuple[float, float, float, float]] = {}
    warnings: list[str] = []

    for idx, scfg in enumerate(sheet_configs):
        ox = idx * (w + SHEET_GAP_MM)
        before_count = len(msp)
        eff_plan_scale = scfg.plan_scale or opts.plan_scale
        has_plans = any(it == "foundation_plan" or it.startswith("floor_plan:") for it in scfg.items)
        has_dets = any(it in DETAIL_KEYS for it in scfg.items)
        if has_plans and has_dets:
            scale_lbl = f"1:{eff_plan_scale} / 1:{opts.detail_scale}"
        elif has_plans:
            scale_lbl = f"1:{eff_plan_scale}"
        elif has_dets:
            scale_lbl = f"1:{opts.detail_scale} / NTS"
        else:
            scale_lbl = "NTS"

        content_box = _draw_title_block(msp, ox, w, h, k, project, scfg.sheet_no, scfg.title, scale_lbl)
        sheet_bounds[scfg.sheet_no] = (ox, 0.0, ox + w, h)

        if scfg.sheet_no not in doc.layouts:
            psp = doc.layouts.new(scfg.sheet_no)
            psp.page_setup(size=(w, h), margins=(0, 0, 0, 0), units="mm")
            psp.add_viewport(
                center=(w * 0.5, h * 0.5),
                size=(w, h),
                view_center_point=(ox + w * 0.5, h * 0.5),
                view_height=h,
            )

        contents, overflow = _compose_sheet_items(
            msp,
            project,
            scfg,
            content_box,
            k,
            eff_plan_scale,
            opts.detail_scale,
            warnings,
        )
        sheets.append(
            CadSheetSummary(
                sheet_no=scfg.sheet_no,
                title=scfg.title,
                contents=contents,
                entity_count=len(msp) - before_count,
                overflow=overflow,
            )
        )

    resolved_opts = opts.model_copy(deep=True)
    resolved_opts.sheets = [s.model_copy(deep=True) for s in sheet_configs]

    state = CadDrawingState(
        generated_at=_utc_now(),
        options=resolved_opts,
        sheets=sheets,
        total_entities=len(msp),
        layers=layer_names,
        warnings=warnings,
        available_items=get_available_cad_items(project),
    )
    return doc, state, sheet_bounds


def build_cad_document(
    project: ProjectState,
    options: CadDrawingOptions | None = None,
) -> tuple[ezdxf.document.Drawing, CadDrawingState, dict[str, tuple[float, float, float, float]]]:
    """
    Build the complete multi-sheet AutoCAD R2010 DXF document and return:
      (doc, CadDrawingState, sheet_bounds_map {sheet_no: (ox, 0, ox + w, h)})
    """
    _validate_cad_prerequisites(project)
    opts = (
        options.model_copy(deep=True)
        if options is not None
        else project.cad_drawings.options.model_copy(deep=True)
    )
    sheet_configs = opts.sheets if opts.sheets else build_default_sheet_configs(project, opts)
    if not sheet_configs:
        raise ValueError("Enable at least one drawing section or add a sheet before generating CAD drawings.")

    return _build_dxf_for_sheet_configs(project, opts, sheet_configs)


def build_dxf_bytes(project: ProjectState, options: CadDrawingOptions | None = None) -> tuple[bytes, CadDrawingState]:
    doc, state, _ = build_cad_document(project, options=options)
    text_stream = io.StringIO()
    doc.write(text_stream)
    return text_stream.getvalue().encode("utf-8"), state


def build_sheets_zip(project: ProjectState, options: CadDrawingOptions | None = None) -> bytes:
    """Build a ZIP archive containing one standalone .dxf file per configured sheet."""
    _validate_cad_prerequisites(project)
    opts = (
        options.model_copy(deep=True)
        if options is not None
        else project.cad_drawings.options.model_copy(deep=True)
    )
    sheet_configs = opts.sheets if opts.sheets else build_default_sheet_configs(project, opts)
    if not sheet_configs:
        raise ValueError("No sheets configured for ZIP export.")

    pid = project.project_info.project_id
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for scfg in sheet_configs:
            single_doc, _, _ = _build_dxf_for_sheet_configs(project, opts, [scfg])
            s_io = io.StringIO()
            single_doc.write(s_io)
            safe_sno = scfg.sheet_no.replace("/", "-").replace(" ", "_")
            zf.writestr(f"{pid}_{safe_sno}.dxf", s_io.getvalue().encode("utf-8"))
    return buf.getvalue()


def build_sheets_pdf(project: ProjectState, options: CadDrawingOptions | None = None) -> bytes:
    """
    Render each configured CAD sheet as a true-scale landscape vector PDF page
    (A1 = 841x594mm, A2 = 594x420mm, A3 = 420x297mm) with crisp CAD-to-Print colors.
    """
    doc, state, sheet_bounds = build_cad_document(project, options=options)
    w_mm, h_mm = SHEET_DIMENSIONS_MM[state.options.sheet_size]

    layer_aci: dict[str, int] = {lname: aci for lname, aci, _, _ in CAD_LAYER_SPECS}
    layer_ltype: dict[str, str] = {lname: ltype for lname, _, ltype, _ in CAD_LAYER_SPECS}
    layer_lw: dict[str, float] = {lname: (lw / 100.0) for lname, _, _, lw in CAD_LAYER_SPECS}

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(w_mm * RL_MM, h_mm * RL_MM))
    c.setTitle(f"{project.project_info.project_name} - Structural Plans")
    c.setAuthor(project.reporting.signatory.engineer_name or "Structural AI Office")

    msp_entities = list(doc.modelspace())

    for sh_meta in state.sheets:
        ox, oy, x1, y1 = sheet_bounds[sh_meta.sheet_no]
        c.setPageSize((w_mm * RL_MM, h_mm * RL_MM))

        def _in_sheet(x: float) -> bool:
            return (ox - 1.0) <= x <= (x1 + 1.0)

        def _px(x: float) -> float:
            return (x - ox) * RL_MM

        def _py(y: float) -> float:
            return (y - oy) * RL_MM

        for ent in msp_entities:
            dxftype = ent.dxftype()
            layer = ent.dxf.layer if hasattr(ent.dxf, "layer") else "S-TEXT"
            aci = layer_aci.get(layer, 7)
            hex_col = ACI_TO_PRINT_HEX.get(aci, "#111827")
            col = rl_colors.HexColor(hex_col)
            lw_mm = layer_lw.get(layer, 0.25)
            ent_lt = getattr(ent.dxf, "linetype", "BYLAYER")
            eff_lt = layer_ltype.get(layer, "CONTINUOUS") if ent_lt == "BYLAYER" else ent_lt

            c.saveState()
            c.setStrokeColor(col)
            c.setFillColor(col)
            c.setLineWidth(max(lw_mm * RL_MM * 0.75, 0.35))
            lt_up = (eff_lt or "").upper()
            if "CENTER" in lt_up:
                c.setDash([6, 2, 1.5, 2], 0)
            elif "DASH" in lt_up:
                c.setDash([3.5, 2.2], 0)

            if dxftype == "LINE":
                p1, p2 = ent.dxf.start, ent.dxf.end
                if _in_sheet(p1.x) or _in_sheet(p2.x):
                    c.line(_px(p1.x), _py(p1.y), _px(p2.x), _py(p2.y))
            elif dxftype == "LWPOLYLINE":
                pts = list(ent.get_points("xy"))
                if pts and any(_in_sheet(px) for px, _ in pts):
                    path = c.beginPath()
                    path.moveTo(_px(pts[0][0]), _py(pts[0][1]))
                    for px, py in pts[1:]:
                        path.lineTo(_px(px), _py(py))
                    if ent.closed:
                        path.close()
                    c.drawPath(path, stroke=1, fill=0)
            elif dxftype == "CIRCLE":
                ctr = ent.dxf.center
                if _in_sheet(ctr.x):
                    r_pt = float(ent.dxf.radius) * RL_MM
                    do_fill = 1 if layer == "S-REBAR" else 0
                    c.circle(_px(ctr.x), _py(ctr.y), r_pt, stroke=1, fill=do_fill)
            elif dxftype == "ARC":
                ctr = ent.dxf.center
                if _in_sheet(ctr.x):
                    r_pt = float(ent.dxf.radius) * RL_MM
                    a1 = float(ent.dxf.start_angle)
                    a2 = float(ent.dxf.end_angle)
                    extent = (a2 - a1) % 360.0
                    c.arc(
                        _px(ctr.x) - r_pt,
                        _py(ctr.y) - r_pt,
                        _px(ctr.x) + r_pt,
                        _py(ctr.y) + r_pt,
                        startAng=a1,
                        extent=extent,
                    )
            elif dxftype == "HATCH":
                c.setFillColor(rl_colors.HexColor("#cbd5e1"))
                for bpath in ent.paths:
                    if hasattr(bpath, "vertices") and bpath.vertices:
                        if not any(_in_sheet(v[0]) for v in bpath.vertices):
                            continue
                        p = c.beginPath()
                        p.moveTo(_px(bpath.vertices[0][0]), _py(bpath.vertices[0][1]))
                        for v in bpath.vertices[1:]:
                            p.lineTo(_px(v[0]), _py(v[1]))
                        p.close()
                        c.drawPath(p, stroke=0, fill=1)
            elif dxftype in ("TEXT", "MTEXT"):
                txt = ent.dxf.text if dxftype == "TEXT" else ent.plain_text()
                if txt:
                    h_mm_txt = float(getattr(ent.dxf, "height", getattr(ent.dxf, "char_height", 2.5)))
                    rot = float(getattr(ent.dxf, "rotation", 0.0))
                    if dxftype == "TEXT":
                        align, p1, p2 = ent.get_placement()
                        pt = p2 if (p2 is not None and align != TextEntityAlignment.LEFT) else p1
                        align_name = align.name
                    else:
                        pt = ent.dxf.insert
                        align_name = "TOP_LEFT"
                    if _in_sheet(pt.x):
                        font_size = max(h_mm_txt * RL_MM * 0.92, 3.2)
                        font_name = "Helvetica-Bold" if layer == "S-TITLEBLK" else "Helvetica"
                        c.setFont(font_name, font_size)
                        safe_txt = _safe_pdf_text(txt)
                        c.translate(_px(pt.x), _py(pt.y))
                        if abs(rot) > 0.1:
                            c.rotate(rot)
                        dy_off = -font_size * 0.35 if "MIDDLE" in align_name else (-font_size * 0.85 if "TOP" in align_name else 0.0)
                        if "CENTER" in align_name:
                            c.drawCentredString(0.0, dy_off, safe_txt)
                        elif "RIGHT" in align_name:
                            c.drawRightString(0.0, dy_off, safe_txt)
                        else:
                            c.drawString(0.0, dy_off, safe_txt)

            c.restoreState()

        c.showPage()

    c.save()
    return buf.getvalue()


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

    layer_color_map = {lname: ACI_TO_SVG_HEX.get(aci, "#e2e8f0") for lname, aci, _, _ in CAD_LAYER_SPECS}
    layer_ltype_map = {lname: ltype for lname, _, ltype, _ in CAD_LAYER_SPECS}
    layer_lw_map = {lname: lw for lname, _, _, lw in CAD_LAYER_SPECS}

    groups: dict[str, list[str]] = {lname: [] for lname, _, _, _ in CAD_LAYER_SPECS}

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
        sw_px = max(layer_lw_map.get(layer, 25) / 50.0, 0.35)

        if dxftype == "LINE":
            p1, p2 = ent.dxf.start, ent.dxf.end
            if not (_in_sheet(p1.x) or _in_sheet(p2.x)):
                continue
            groups[layer].append(
                f'<line x1="{_sx(p1.x):.2f}" y1="{_sy(p1.y):.2f}" '
                f'x2="{_sx(p2.x):.2f}" y2="{_sy(p2.y):.2f}" '
                f'stroke="{color_hex}" stroke-width="{sw_px:.2f}"{dash} />'
            )
        elif dxftype == "LWPOLYLINE":
            pts = list(ent.get_points("xy"))
            if not pts or not any(_in_sheet(px) for px, _ in pts):
                continue
            pts_str = " ".join(f"{_sx(px):.2f},{_sy(py):.2f}" for px, py in pts)
            tag = "polygon" if ent.closed else "polyline"
            groups[layer].append(
                f'<{tag} points="{pts_str}" fill="none" stroke="{color_hex}" '
                f'stroke-width="{sw_px:.2f}"{dash} />'
            )
        elif dxftype == "CIRCLE":
            ctr = ent.dxf.center
            if not _in_sheet(ctr.x):
                continue
            r = float(ent.dxf.radius)
            fill = color_hex if layer == "S-REBAR" else "none"
            groups[layer].append(
                f'<circle cx="{_sx(ctr.x):.2f}" cy="{_sy(ctr.y):.2f}" r="{r:.2f}" '
                f'fill="{fill}" stroke="{color_hex}" stroke-width="{sw_px:.2f}" />'
            )
        elif dxftype == "ARC":
            ctr = ent.dxf.center
            if not _in_sheet(ctr.x):
                continue
            r = float(ent.dxf.radius)
            a1 = math.radians(float(ent.dxf.start_angle))
            a2 = math.radians(float(ent.dxf.end_angle))
            x_start, y_start = ctr.x + r * math.cos(a1), ctr.y + r * math.sin(a1)
            x_end, y_end = ctr.x + r * math.cos(a2), ctr.y + r * math.sin(a2)
            sweep = (float(ent.dxf.end_angle) - float(ent.dxf.start_angle)) % 360.0
            large = 1 if sweep > 180.0 else 0
            groups[layer].append(
                f'<path d="M {_sx(x_start):.2f} {_sy(y_start):.2f} '
                f'A {r:.2f} {r:.2f} 0 {large} 0 {_sx(x_end):.2f} {_sy(y_end):.2f}" '
                f'fill="none" stroke="{color_hex}" stroke-width="{sw_px:.2f}"{dash} />'
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
        "overflow": sheet_meta.overflow,
        "width_mm": sw,
        "height_mm": sh,
        "layers": [lname for lname, elems in groups.items() if elems],
        "svg": "\n".join(svg_parts),
    }
