from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import math
import re

from ..models import (
    BomCostLine,
    BomCostParameters,
    BomSummary,
    ConcreteQuantityRow,
    EngineerSignatory,
    ProjectRevisionSnapshot,
    ProjectState,
    RebarQuantityRow,
    ReportGenerationOptions,
    ReportState,
)

REGIONAL_COST_PRESETS: dict[str, dict] = {
    "car_baguio": {
        "key": "car_baguio",
        "label": "CAR / Baguio City (Mountain Hauling)",
        "notes": "Includes mountain hauling premium for ready-mix concrete, steel bars & aggregates.",
        "rate_concrete_php_m3": 6850.0,
        "rate_rebar_php_kg": 54.0,
        "rate_tie_wire_php_kg": 98.0,
        "rate_formwork_php_m2": 680.0,
        "rate_chb_wall_php_m2": 980.0,
        "labor_pct_of_materials": 35.0,
        "labor_concrete_pct": 32.0,
        "labor_rebar_pct": 38.0,
        "labor_formwork_pct": 42.0,
    },
    "ncr_manila": {
        "key": "ncr_manila",
        "label": "NCR / Metro Manila",
        "notes": "Direct batching plant supply with Metro Manila skilled labor wage rates.",
        "rate_concrete_php_m3": 6200.0,
        "rate_rebar_php_kg": 50.0,
        "rate_tie_wire_php_kg": 92.0,
        "rate_formwork_php_m2": 640.0,
        "rate_chb_wall_php_m2": 920.0,
        "labor_pct_of_materials": 38.0,
        "labor_concrete_pct": 35.0,
        "labor_rebar_pct": 40.0,
        "labor_formwork_pct": 45.0,
    },
    "cebu_visayas": {
        "key": "cebu_visayas",
        "label": "Central Visayas / Cebu",
        "notes": "Metro Cebu commercial ready-mix & PNS 49 rebar supply with regional labor rates.",
        "rate_concrete_php_m3": 6400.0,
        "rate_rebar_php_kg": 52.0,
        "rate_tie_wire_php_kg": 95.0,
        "rate_formwork_php_m2": 630.0,
        "rate_chb_wall_php_m2": 940.0,
        "labor_pct_of_materials": 34.0,
        "labor_concrete_pct": 30.0,
        "labor_rebar_pct": 36.0,
        "labor_formwork_pct": 40.0,
    },
    "davao_mindanao": {
        "key": "davao_mindanao",
        "label": "Davao Region / Mindanao",
        "notes": "Davao City ready-mix & reinforcing steel supply with competitive local labor rates.",
        "rate_concrete_php_m3": 6100.0,
        "rate_rebar_php_kg": 51.0,
        "rate_tie_wire_php_kg": 94.0,
        "rate_formwork_php_m2": 600.0,
        "rate_chb_wall_php_m2": 900.0,
        "labor_pct_of_materials": 32.0,
        "labor_concrete_pct": 28.0,
        "labor_rebar_pct": 34.0,
        "labor_formwork_pct": 38.0,
    },
}


def get_regional_cost_presets() -> list[dict]:
    """Return all Philippine regional cost & trade-split labor presets."""
    return [dict(v) for v in REGIONAL_COST_PRESETS.values()]

_BAR_CALLOUT_RE = re.compile(r"(\d+)\s*[-xX]\s*(\d+)\s*mm", re.IGNORECASE)
_SPACING_CALLOUT_RE = re.compile(
    r"(\d+)\s*mm\s*@\s*(\d+(?:\.\d+)?)\s*mm",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_bar_callout(callout: str, fallback_count: int = 2, fallback_dia: int = 16) -> tuple[int, int]:
    """Parse bar callouts such as '3-16mm' or '9-16mm @ 175mm o.c.' -> (count, diameter_mm)."""
    if not callout:
        return fallback_count, fallback_dia
    m = _BAR_CALLOUT_RE.search(callout)
    if not m:
        return fallback_count, fallback_dia
    return max(1, int(m.group(1))), max(8, int(m.group(2)))


def parse_spacing_callout(
    callout: str,
    fallback_dia: int = 10,
    fallback_spacing_mm: float = 150.0,
) -> tuple[int, float]:
    """Parse stirrup/tie callouts such as '10mm @ 100mm o.c.' -> (diameter_mm, spacing_mm)."""
    if not callout:
        return fallback_dia, fallback_spacing_mm
    m = _SPACING_CALLOUT_RE.search(callout)
    if not m:
        return fallback_dia, fallback_spacing_mm
    return max(8, int(m.group(1))), max(25.0, float(m.group(2)))


def pns49_unit_mass_kg_m(diameter_mm: int) -> float:
    """Nominal linear mass (kg/m) per PNS 49:2020: d^2 / 162."""
    return (float(diameter_mm) ** 2) / 162.0


def _validate_prerequisites(project: ProjectState) -> None:
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
            "Cannot generate calculation report & BOM until required modules are completed: "
            + ", ".join(missing)
            + "."
        )


def _floor_story_heights(project: ProjectState) -> tuple[dict[str, float], float, float]:
    """Return (height_by_level_id, typical_story_height_m, base_elevation_m)."""
    levels = sorted(project.levels, key=lambda lv: lv.elevation_m)
    if not levels:
        return {}, 3.0, 0.0
    base_elev = levels[0].elevation_m
    heights: dict[str, float] = {}
    story_spans: list[float] = []
    for idx, lv in enumerate(levels):
        if idx == 0:
            h = (levels[1].elevation_m - lv.elevation_m) if len(levels) > 1 else 3.0
        else:
            h = lv.elevation_m - levels[idx - 1].elevation_m
            if h > 0.1:
                story_spans.append(h)
        heights[lv.id] = max(h, 2.4)
    typical_h = (sum(story_spans) / len(story_spans)) if story_spans else 3.0
    return heights, typical_h, base_elev


def compute_bom_summary(
    project: ProjectState,
    bom_params: BomCostParameters,
) -> BomSummary:
    """Compute concrete, formwork, PNS 49 rebar, optional CHB masonry, and PHP cost estimate."""
    _validate_prerequisites(project)

    sm = project.structural_model
    ms = project.member_sizing
    lg = project.load_generation
    ad = project.analysis_design
    dp = ad.params

    sec_by_id = {s.section_id: s for s in ms.sections}
    asgn_by_mid = {a.member_id: a for a in ms.member_assignments}
    slab_asgn_by_pid = {s.panel_id: s for s in ms.slab_assignments}
    level_elev_by_id = {lv.id: lv.elevation_m for lv in project.levels}
    story_h_by_lvl, typical_h, base_elev = _floor_story_heights(project)

    waste_conc_mult = 1.0 + (bom_params.concrete_waste_pct / 100.0)
    waste_rebar_mult = 1.0 + (bom_params.rebar_waste_splice_pct / 100.0)

    # ------------------------------------------------------------------
    # 1. Concrete & Formwork Takeoff
    # ------------------------------------------------------------------
    ftg_count = len(ad.footing_designs)
    ftg_vol = 0.0
    ftg_fw = 0.0
    for f in ad.footing_designs:
        t_m = f.thickness_mm / 1000.0
        ftg_vol += f.b_m * f.l_m * t_m
        ftg_fw += 2.0 * (f.b_m + f.l_m) * t_m

    ped_count = 0
    ped_vol = 0.0
    ped_fw = 0.0

    col_count = 0
    col_vol = 0.0
    col_fw = 0.0

    beam_count = 0
    beam_vol = 0.0
    beam_fw = 0.0

    member_by_id = {m.member_id: m for m in sm.members}

    for m in sm.members:
        asgn = asgn_by_mid.get(m.member_id)
        sec = sec_by_id.get(asgn.section_id) if asgn else None
        if sec is None or m.length_m <= 0.0:
            continue
        b_m = sec.width_mm / 1000.0
        h_m = sec.depth_mm / 1000.0
        vol = b_m * h_m * m.length_m

        if m.member_type == "column":
            fw = 2.0 * (b_m + h_m) * m.length_m
            if m.is_pedestal:
                ped_count += 1
                ped_vol += vol
                ped_fw += fw
            else:
                col_count += 1
                col_vol += vol
                col_fw += fw
        else:
            beam_count += 1
            beam_vol += vol
            if m.member_type == "tie_beam":
                beam_fw += 2.0 * h_m * m.length_m
            else:
                beam_fw += (b_m + 2.0 * h_m) * m.length_m

    slab_count = 0
    slab_vol = 0.0
    slab_fw = 0.0
    active_slabs: list[tuple[float, float]] = []  # (net_area_sqm, thickness_mm)
    for panel in sm.slab_panels:
        if panel.slab_action == "void" or panel.net_area_sqm <= 0.0:
            continue
        sa = slab_asgn_by_pid.get(panel.panel_id)
        t_mm = sa.thickness_mm if sa is not None else 125.0
        if t_mm <= 0.0:
            continue
        slab_count += 1
        slab_vol += panel.net_area_sqm * (t_mm / 1000.0)
        elev = level_elev_by_id.get(panel.floor_level, base_elev + 1.0)
        if elev > base_elev + 0.05:
            slab_fw += panel.net_area_sqm
        active_slabs.append((panel.net_area_sqm, t_mm))

    concrete_rows = [
        ConcreteQuantityRow(
            element_group="Isolated Footings",
            member_count=ftg_count,
            net_volume_m3=round(ftg_vol, 3),
            gross_volume_m3=round(ftg_vol * waste_conc_mult, 3),
            formwork_area_m2=round(ftg_fw, 2),
        ),
        ConcreteQuantityRow(
            element_group="Column Pedestals",
            member_count=ped_count,
            net_volume_m3=round(ped_vol, 3),
            gross_volume_m3=round(ped_vol * waste_conc_mult, 3),
            formwork_area_m2=round(ped_fw, 2),
        ),
        ConcreteQuantityRow(
            element_group="Columns",
            member_count=col_count,
            net_volume_m3=round(col_vol, 3),
            gross_volume_m3=round(col_vol * waste_conc_mult, 3),
            formwork_area_m2=round(col_fw, 2),
        ),
        ConcreteQuantityRow(
            element_group="Beams & Tie Beams",
            member_count=beam_count,
            net_volume_m3=round(beam_vol, 3),
            gross_volume_m3=round(beam_vol * waste_conc_mult, 3),
            formwork_area_m2=round(beam_fw, 2),
        ),
        ConcreteQuantityRow(
            element_group="RC Slabs",
            member_count=slab_count,
            net_volume_m3=round(slab_vol, 3),
            gross_volume_m3=round(slab_vol * waste_conc_mult, 3),
            formwork_area_m2=round(slab_fw, 2),
        ),
    ]

    # Optional Pass C-2 secondary RC elements (only appended when configured so legacy BOM stays identical)
    if ad.retaining_wall_inputs:
        rw_vol = 0.0
        rw_fw = 0.0
        for rw in ad.retaining_wall_inputs:
            stem_v = (rw.stem_thickness_mm / 1000.0) * rw.retained_height_m * rw.total_length_m
            base_v = rw.base_width_m * (rw.base_thickness_mm / 1000.0) * rw.total_length_m
            rw_vol += stem_v + base_v
            rw_fw += (2.0 * rw.retained_height_m + 2.0 * (rw.base_thickness_mm / 1000.0)) * rw.total_length_m
        concrete_rows.append(
            ConcreteQuantityRow(
                element_group="Retaining Walls",
                member_count=len(ad.retaining_wall_inputs),
                net_volume_m3=round(rw_vol, 3),
                gross_volume_m3=round(rw_vol * waste_conc_mult, 3),
                formwork_area_m2=round(rw_fw, 2),
            )
        )

    if ad.stair_inputs:
        st_vol = 0.0
        st_fw = 0.0
        for st in ad.stair_inputs:
            t_m = st.waist_thickness_mm / 1000.0
            n_steps = max(1, int(round((st.flight_span_m * 1000.0) / max(st.tread_mm, 150.0))))
            step_vol = n_steps * 0.5 * (st.riser_mm / 1000.0) * (st.tread_mm / 1000.0) * st.width_m
            slope_len = math.hypot(st.flight_span_m, n_steps * (st.riser_mm / 1000.0))
            st_vol += slope_len * t_m * st.width_m + step_vol
            st_fw += slope_len * st.width_m + n_steps * (st.riser_mm / 1000.0) * st.width_m
        concrete_rows.append(
            ConcreteQuantityRow(
                element_group="RC Stairs",
                member_count=len(ad.stair_inputs),
                net_volume_m3=round(st_vol, 3),
                gross_volume_m3=round(st_vol * waste_conc_mult, 3),
                formwork_area_m2=round(st_fw, 2),
            )
        )

    if ad.strap_footing_designs:
        node_by_id = {n.node_id: n for n in sm.nodes}
        sf_vol = 0.0
        sf_fw = 0.0
        for sf in ad.strap_footing_designs:
            n1 = node_by_id.get(sf.exterior_node_id)
            n2 = node_by_id.get(sf.interior_node_id)
            span_m = math.hypot(n2.x_m - n1.x_m, n2.z_m - n1.z_m) if (n1 and n2) else 4.0
            sb = sf.strap_b_mm / 1000.0
            sh = sf.strap_h_mm / 1000.0
            sf_vol += sb * sh * span_m
            sf_fw += 2.0 * sh * span_m
        concrete_rows.append(
            ConcreteQuantityRow(
                element_group="Strap Beams",
                member_count=len(ad.strap_footing_designs),
                net_volume_m3=round(sf_vol, 3),
                gross_volume_m3=round(sf_vol * waste_conc_mult, 3),
                formwork_area_m2=round(sf_fw, 2),
            )
        )

    total_conc_net = round(sum(r.net_volume_m3 for r in concrete_rows), 3)
    total_conc_gross = round(sum(r.gross_volume_m3 for r in concrete_rows), 3)
    total_formwork = round(sum(r.formwork_area_m2 for r in concrete_rows), 2)

    # ------------------------------------------------------------------
    # 2. PNS 49:2020 Rebar Takeoff (linear meters grouped by diameter)
    # ------------------------------------------------------------------
    rebar_net_m: dict[int, float] = {}

    def _add_bar(dia_mm: int, length_m: float) -> None:
        if length_m > 0.0:
            rebar_net_m[dia_mm] = rebar_net_m.get(dia_mm, 0.0) + length_m

    cover_beam_m = dp.clear_cover_beam_mm / 1000.0
    cover_col_m = dp.clear_cover_column_mm / 1000.0
    cover_ftg_m = dp.clear_cover_footing_mm / 1000.0

    # 2a. Beams
    for b in ad.beam_designs:
        length_m = max(b.length_m, 0.0)
        if length_m <= 0.0:
            continue
        n_ts, d_ts = parse_bar_callout(b.bars_top_start, 2, dp.main_bar_dia_beam_mm)
        n_te, d_te = parse_bar_callout(b.bars_top_end, 2, dp.main_bar_dia_beam_mm)
        n_bm, d_bm = parse_bar_callout(b.bars_bot_mid, 2, dp.main_bar_dia_beam_mm)

        n_top_cont = min(n_ts, n_te)
        extra_start = max(0, n_ts - n_top_cont)
        extra_end = max(0, n_te - n_top_cont)

        _add_bar(d_ts, n_top_cont * length_m)
        _add_bar(d_ts, extra_start * (0.30 * length_m))
        _add_bar(d_te, extra_end * (0.30 * length_m))
        _add_bar(d_bm, n_bm * length_m)

        # Stirrups: support confinement zones (2 * 0.25*L) + midspan (0.50*L)
        d_sup, s_sup_mm = parse_spacing_callout(b.stirrups_support, dp.stirrup_tie_dia_mm, 100.0)
        d_mid, s_mid_mm = parse_spacing_callout(b.stirrups_midspan, dp.stirrup_tie_dia_mm, 150.0)
        b_core = max(0.05, (b.width_mm / 1000.0) - 2.0 * cover_beam_m)
        h_core = max(0.05, (b.depth_mm / 1000.0) - 2.0 * cover_beam_m)
        stirrup_perim = 2.0 * (b_core + h_core) + 0.15

        n_sup = int(math.ceil((0.50 * length_m) / (s_sup_mm / 1000.0))) + 1
        n_mid = int(math.ceil((0.50 * length_m) / (s_mid_mm / 1000.0)))
        _add_bar(d_sup, n_sup * stirrup_perim)
        _add_bar(d_mid, n_mid * stirrup_perim)

    # 2b. Columns & Pedestals
    for c in ad.column_designs:
        length_m = max(c.length_m, 0.0)
        if length_m <= 0.0:
            continue
        n_main, d_main = parse_bar_callout(c.main_bars, 4, dp.main_bar_dia_col_mm)
        _add_bar(d_main, n_main * length_m)

        d_conf, s_conf_mm = parse_spacing_callout(c.ties_confined, dp.stirrup_tie_dia_mm, 100.0)
        d_mid, s_mid_mm = parse_spacing_callout(c.ties_mid, dp.stirrup_tie_dia_mm, 150.0)
        b_core = max(0.05, (c.width_mm / 1000.0) - 2.0 * cover_col_m)
        h_core = max(0.05, (c.depth_mm / 1000.0) - 2.0 * cover_col_m)
        tie_perim = 2.0 * (b_core + h_core) + 0.15

        lo = min(
            0.5 * length_m,
            max(c.width_mm / 1000.0, c.depth_mm / 1000.0, length_m / 6.0, 0.45),
        )
        l_conf = min(length_m, 2.0 * lo)
        l_mid = max(0.0, length_m - l_conf)
        n_conf = int(math.ceil(l_conf / (s_conf_mm / 1000.0))) + 1
        n_mid = int(math.ceil(l_mid / (s_mid_mm / 1000.0))) if l_mid > 1e-4 else 0
        _add_bar(d_conf, n_conf * tie_perim)
        _add_bar(d_mid, n_mid * tie_perim)

    # 2c. Isolated Footings
    hook_len_m = 0.15  # 90-degree standard footing bar hook at each end
    for f in ad.footing_designs:
        nx, dx = parse_bar_callout(f.bottom_bars_x, 4, dp.main_bar_dia_beam_mm)
        nz, dz = parse_bar_callout(f.bottom_bars_z, 4, dp.main_bar_dia_beam_mm)
        cut_x = max(0.20, f.l_m - 2.0 * cover_ftg_m + 2.0 * hook_len_m)
        cut_z = max(0.20, f.b_m - 2.0 * cover_ftg_m + 2.0 * hook_len_m)
        _add_bar(dx, nx * cut_x)
        _add_bar(dz, nz * cut_z)

    # 2d. RC Slabs (2-way bottom mat + top negative strips, min 0.0018*t or 10mm @ 200mm o.c.)
    for area_sqm, t_mm in active_slabs:
        dia_slab = 12 if t_mm >= 175.0 else 10
        bar_area = math.pi * (dia_slab ** 2) / 4.0
        as_min_per_m = 0.0018 * 1000.0 * t_mm
        s_req_mm = (bar_area / max(as_min_per_m, 1e-6)) * 1000.0
        s_mm = max(100.0, min(200.0, math.floor(s_req_mm / 25.0) * 25.0))
        # 2 orthogonal directions * 1.5 layers (full bottom mat + 50% top support strips)
        bars_per_m_both_ways = 2.0 * 1.5 * (1000.0 / s_mm)
        _add_bar(dia_slab, area_sqm * bars_per_m_both_ways)

    # 2e. Optional Pass C-2 Secondary RC Elements Rebar (only when configured)
    if ad.retaining_wall_designs:
        rw_in_by_id = {x.wall_id: x for x in ad.retaining_wall_inputs}
        for rw in ad.retaining_wall_designs:
            rwi = rw_in_by_id.get(rw.wall_id)
            b_base_m = rwi.base_width_m if rwi else 1.8
            dv, sv = parse_spacing_callout(rw.stem_vertical_bars, 12, 150.0)
            dh, sh = parse_spacing_callout(rw.stem_horizontal_bars, 12, 200.0)
            db, sb = parse_spacing_callout(rw.base_heel_toe_bars, 12, 175.0)
            # 2 faces of stem vertical + horizontal, 2 layers of base slab
            _add_bar(dv, 2.0 * (rw.total_length_m / (sv / 1000.0)) * (rw.retained_height_m + 0.5))
            _add_bar(dh, 2.0 * (rw.retained_height_m / (sh / 1000.0)) * rw.total_length_m)
            _add_bar(db, 2.0 * (rw.total_length_m / (sb / 1000.0)) * b_base_m)

    if ad.stair_designs:
        for st in ad.stair_designs:
            dm, sm_s = parse_spacing_callout(st.main_bars, 12, 150.0)
            dd, sd_s = parse_spacing_callout(st.dist_bars, 10, 250.0)
            _add_bar(dm, (st.width_m / (sm_s / 1000.0)) * (st.flight_span_m * 1.25))
            _add_bar(dd, (st.flight_span_m * 1.15 / (sd_s / 1000.0)) * st.width_m)

    if ad.strap_footing_designs:
        node_by_id = {n.node_id: n for n in sm.nodes}
        for sf in ad.strap_footing_designs:
            n1 = node_by_id.get(sf.exterior_node_id)
            n2 = node_by_id.get(sf.interior_node_id)
            span_m = math.hypot(n2.x_m - n1.x_m, n2.z_m - n1.z_m) if (n1 and n2) else 4.0
            nt, dt = parse_bar_callout(sf.strap_top_bars, 4, 20)
            nb, db_s = parse_bar_callout(sf.strap_bot_bars, 3, 20)
            ds_s, ss_s = parse_spacing_callout(sf.strap_stirrups, 10, 150.0)
            _add_bar(dt, nt * (span_m + 0.8))
            _add_bar(db_s, nb * (span_m + 0.8))
            perim_m = 2.0 * ((sf.strap_b_mm + sf.strap_h_mm) / 1000.0)
            _add_bar(ds_s, (span_m / (ss_s / 1000.0)) * perim_m)

    rebar_rows: list[RebarQuantityRow] = []
    for dia in sorted(rebar_net_m.keys()):
        net_l = rebar_net_m[dia]
        gross_l = net_l * waste_rebar_mult
        unit_m = pns49_unit_mass_kg_m(dia)
        weight_kg = gross_l * unit_m
        pcs = int(math.ceil(gross_l / float(bom_params.commercial_bar_length_m)))
        grade = "Grade 40 (PNS 280)" if dia <= 12 else "Grade 60 (PNS 420)"
        rebar_rows.append(
            RebarQuantityRow(
                diameter_mm=dia,
                grade_label=grade,
                unit_mass_kg_m=round(unit_m, 4),
                net_length_m=round(net_l, 2),
                gross_length_m=round(gross_l, 2),
                total_weight_kg=round(weight_kg, 2),
                commercial_pieces=pcs,
            )
        )

    total_rebar_kg = round(sum(r.total_weight_kg for r in rebar_rows), 2)
    rebar_ratio = round(total_rebar_kg / total_conc_net, 2) if total_conc_net > 1e-6 else 0.0
    total_tie_wire_kg = round(total_rebar_kg * (bom_params.tie_wire_pct_of_rebar / 100.0), 2)

    # ------------------------------------------------------------------
    # 3. CHB Masonry Wall Area (from Module 05 wall line loads)
    # ------------------------------------------------------------------
    total_chb_area = 0.0
    if bom_params.include_chb_masonry and lg.beam_loads:
        wall_mids = {
            bl.member_id
            for bl in lg.beam_loads
            if "wall" in (bl.source or "").lower()
        }
        for mid in sorted(wall_mids):
            mem = member_by_id.get(mid)
            if mem is None or mem.length_m <= 0.0:
                continue
            h_story = story_h_by_lvl.get(mem.floor_level, typical_h)
            total_chb_area += mem.length_m * h_story
    total_chb_area = round(total_chb_area, 2)

    # ------------------------------------------------------------------
    # 4. NBCP Sec. 302-5f Cost Estimate Lines (PHP, with trade-split labor)
    # ------------------------------------------------------------------
    base_labor_pct = bom_params.labor_pct_of_materials
    conc_labor_pct = (
        bom_params.labor_concrete_pct if bom_params.labor_concrete_pct is not None else base_labor_pct
    )
    rebar_labor_pct = (
        bom_params.labor_rebar_pct if bom_params.labor_rebar_pct is not None else base_labor_pct
    )
    fw_labor_pct = (
        bom_params.labor_formwork_pct if bom_params.labor_formwork_pct is not None else base_labor_pct
    )

    raw_items: list[tuple[str, str, float, str, float, float]] = [
        (
            "01",
            f"Ready-Mix Structural Concrete (f'c = {ms.material.fc_mpa:.0f} MPa, incl. {bom_params.concrete_waste_pct:.1f}% waste)",
            total_conc_gross,
            "m³",
            bom_params.rate_concrete_php_m3,
            conc_labor_pct,
        ),
        (
            "02",
            f"Reinforcing Steel Bars (PNS 49:2020, incl. {bom_params.rebar_waste_splice_pct:.1f}% lap/waste)",
            total_rebar_kg,
            "kg",
            bom_params.rate_rebar_php_kg,
            rebar_labor_pct,
        ),
        (
            "03",
            f"#16 G.I. Tie Wire ({bom_params.tie_wire_pct_of_rebar:.1f}% of rebar mass)",
            total_tie_wire_kg,
            "kg",
            bom_params.rate_tie_wire_php_kg,
            rebar_labor_pct,
        ),
        (
            "04",
            "Formwork, Phenolic Plywood, Framing & Shoring",
            total_formwork,
            "m²",
            bom_params.rate_formwork_php_m2,
            fw_labor_pct,
        ),
    ]
    if bom_params.include_chb_masonry and total_chb_area > 0.0:
        raw_items.append(
            (
                "05",
                "CHB Masonry Walls (150mm/100mm w/ Mortar & Plaster)",
                total_chb_area,
                "m²",
                bom_params.rate_chb_wall_php_m2,
                base_labor_pct,
            )
        )

    # Optional structural steel from configured roof trusses
    total_steel_kg = round(sum(t.estimated_steel_kg for t in ad.roof_truss_designs), 2)
    if total_steel_kg > 0.0:
        raw_items.append(
            (
                f"{len(raw_items) + 1:02d}",
                f"Structural Steel Roof Trusses ({len(ad.roof_truss_designs)} truss type(s), incl. gussets)",
                total_steel_kg,
                "kg",
                bom_params.rate_structural_steel_php_kg,
                base_labor_pct,
            )
        )

    cost_lines: list[BomCostLine] = []
    for item_no, desc, qty, unit, unit_rate, item_labor_pct in raw_items:
        mat_cost = round(qty * unit_rate, 2)
        lab_cost = round(mat_cost * (item_labor_pct / 100.0), 2)
        tot_cost = round(mat_cost + lab_cost, 2)
        cost_lines.append(
            BomCostLine(
                item_no=item_no,
                description=desc,
                quantity=qty,
                unit=unit,
                unit_cost_php=round(unit_rate, 2),
                material_cost_php=mat_cost,
                labor_cost_php=lab_cost,
                total_cost_php=tot_cost,
            )
        )

    total_mat = round(sum(c.material_cost_php for c in cost_lines), 2)
    total_lab = round(sum(c.labor_cost_php for c in cost_lines), 2)
    grand_total = round(total_mat + total_lab, 2)

    return BomSummary(
        total_concrete_net_m3=total_conc_net,
        total_concrete_gross_m3=total_conc_gross,
        total_rebar_kg=total_rebar_kg,
        rebar_ratio_kg_per_m3=rebar_ratio,
        total_tie_wire_kg=total_tie_wire_kg,
        total_formwork_m2=total_formwork,
        total_chb_area_m2=total_chb_area,
        total_structural_steel_kg=total_steel_kg,
        total_material_cost_php=total_mat,
        total_labor_cost_php=total_lab,
        grand_total_cost_php=grand_total,
        concrete_rows=concrete_rows,
        rebar_rows=rebar_rows,
        cost_lines=cost_lines,
    )


def compute_readiness_warnings(
    project: ProjectState,
    signatory: EngineerSignatory,
) -> list[str]:
    """Return non-blocking OBO / NBCP permit readiness warnings."""
    warnings: list[str] = []
    ad = project.analysis_design

    fail_beams = sum(1 for b in ad.beam_designs if b.status == "FAIL")
    fail_cols = sum(1 for c in ad.column_designs if c.status == "FAIL")
    fail_ftgs = sum(1 for f in ad.footing_designs if f.status == "FAIL")
    fail_drifts = sum(1 for d in ad.story_drifts if d.status == "FAIL")

    if fail_beams > 0:
        warnings.append(
            f"{fail_beams} beam member(s) currently have status FAIL in Module 06 RC Design."
        )
    if fail_cols > 0:
        warnings.append(
            f"{fail_cols} column member(s) currently have status FAIL in Module 06 RC Design."
        )
    if fail_ftgs > 0:
        warnings.append(
            f"{fail_ftgs} isolated footing(s) currently have status FAIL in Module 06 RC Design."
        )
    if fail_drifts > 0:
        warnings.append(
            f"{fail_drifts} story level(s) exceed NSCP 2015 inelastic drift limits."
        )

    if not signatory.engineer_name.strip():
        warnings.append("Signatory engineer name is blank — required for OBO permit sign & seal.")
    if not signatory.prc_license_no.strip():
        warnings.append("Engineer PRC license number is blank — required for NBCP Sec. 302-5.")

    elevated_storeys = max(0, len(project.levels) - 1)
    geotech_txt = (signatory.geotech_report_ref or "").strip().lower()
    if elevated_storeys >= 3 and (not geotech_txt or "pending" in geotech_txt):
        warnings.append(
            f"Building has {elevated_storeys} elevated storeys (>= 3 storeys): attach a finalized "
            "Geotechnical / Soil Boring Report reference per NBCP Sec. 302-5."
        )

    return warnings


def generate_report_state(
    project: ProjectState,
    signatory: EngineerSignatory | None = None,
    bom_params: BomCostParameters | None = None,
    options: ReportGenerationOptions | None = None,
) -> ReportState:
    """Compute BOM, readiness warnings, and assemble persisted ReportState."""
    active_sig = (
        signatory.model_copy(deep=True)
        if signatory is not None
        else project.reporting.signatory.model_copy(deep=True)
    )
    if not active_sig.project_address.strip() and project.project_info.location.strip():
        active_sig.project_address = project.project_info.location.strip()

    active_bom_params = (
        bom_params.model_copy(deep=True)
        if bom_params is not None
        else project.reporting.bom_params.model_copy(deep=True)
    )
    active_opts = (
        options.model_copy(deep=True)
        if options is not None
        else project.reporting.options.model_copy(deep=True)
    )

    bom = compute_bom_summary(project, active_bom_params)
    warnings = compute_readiness_warnings(project, active_sig)

    return ReportState(
        generated_at=_utc_now(),
        signatory=active_sig,
        bom_params=active_bom_params,
        options=active_opts,
        bom=bom,
        readiness_warnings=warnings,
    )


def export_bom_csv(project: ProjectState) -> str:
    """Export Concrete & Formwork, PNS 49 Rebar, and PHP Cost Estimate as CSV."""
    rep = project.reporting
    bom = rep.bom if rep.bom is not None else compute_bom_summary(project, rep.bom_params)

    buf = io.StringIO()
    w = csv.writer(buf)

    w.writerow(["=== STRUCTURAL BILL OF MATERIALS & COST ESTIMATE ==="])
    w.writerow(["Project ID", project.project_info.project_id])
    w.writerow(["Project Name", project.project_info.project_name])
    w.writerow(["Location", rep.signatory.project_address or project.project_info.location])
    w.writerow(["Revision", rep.options.report_revision])
    w.writerow([])

    w.writerow(["=== CONCRETE & FORMWORK TAKEOFF ==="])
    w.writerow(
        [
            "Element Group",
            "Member Count",
            "Net Volume (m3)",
            "Gross Volume (m3)",
            "Formwork Area (m2)",
        ]
    )
    for row in bom.concrete_rows:
        w.writerow(
            [
                row.element_group,
                row.member_count,
                f"{row.net_volume_m3:.3f}",
                f"{row.gross_volume_m3:.3f}",
                f"{row.formwork_area_m2:.2f}",
            ]
        )
    w.writerow(
        [
            "TOTAL",
            sum(r.member_count for r in bom.concrete_rows),
            f"{bom.total_concrete_net_m3:.3f}",
            f"{bom.total_concrete_gross_m3:.3f}",
            f"{bom.total_formwork_m2:.2f}",
        ]
    )
    w.writerow([])

    w.writerow(["=== PNS 49:2020 REINFORCING STEEL BREAKDOWN ==="])
    w.writerow(
        [
            "Diameter (mm)",
            "Grade",
            "Unit Mass (kg/m)",
            "Net Length (m)",
            "Gross Length (m)",
            "Total Weight (kg)",
            f"Commercial Pieces ({rep.bom_params.commercial_bar_length_m:g}m)",
        ]
    )
    for row in bom.rebar_rows:
        w.writerow(
            [
                row.diameter_mm,
                row.grade_label,
                f"{row.unit_mass_kg_m:.4f}",
                f"{row.net_length_m:.2f}",
                f"{row.gross_length_m:.2f}",
                f"{row.total_weight_kg:.2f}",
                row.commercial_pieces,
            ]
        )
    w.writerow(
        [
            "TOTAL",
            "",
            "",
            f"{sum(r.net_length_m for r in bom.rebar_rows):.2f}",
            f"{sum(r.gross_length_m for r in bom.rebar_rows):.2f}",
            f"{bom.total_rebar_kg:.2f}",
            sum(r.commercial_pieces for r in bom.rebar_rows),
        ]
    )
    w.writerow(["Rebar-to-Concrete Ratio (kg/m3)", f"{bom.rebar_ratio_kg_per_m3:.2f}"])
    w.writerow(["#16 G.I. Tie Wire (kg)", f"{bom.total_tie_wire_kg:.2f}"])
    w.writerow([])

    w.writerow(["=== STRUCTURAL COST ESTIMATE (NBCP SEC. 302-5f, PHP) ==="])
    w.writerow(
        [
            "Item No.",
            "Description",
            "Quantity",
            "Unit",
            "Unit Cost (PHP)",
            "Material Cost (PHP)",
            "Labor Cost (PHP)",
            "Total Cost (PHP)",
        ]
    )
    for line in bom.cost_lines:
        w.writerow(
            [
                line.item_no,
                line.description,
                f"{line.quantity:.2f}",
                line.unit,
                f"{line.unit_cost_php:.2f}",
                f"{line.material_cost_php:.2f}",
                f"{line.labor_cost_php:.2f}",
                f"{line.total_cost_php:.2f}",
            ]
        )
    w.writerow(
        [
            "GRAND TOTAL",
            "",
            "",
            "",
            "",
            f"{bom.total_material_cost_php:.2f}",
            f"{bom.total_labor_cost_php:.2f}",
            f"{bom.grand_total_cost_php:.2f}",
        ]
    )

    return buf.getvalue()


def _pct_delta(cur: float, base: float) -> float:
    if abs(base) < 1e-6:
        return 0.0
    return round(((cur - base) / base) * 100.0, 2)


def _live_bom_or_none(project: ProjectState) -> BomSummary | None:
    try:
        return compute_bom_summary(project, project.reporting.bom_params)
    except ValueError:
        return project.reporting.bom


def create_revision_snapshot(
    project: ProjectState,
    label: str,
    notes: str = "",
) -> ProjectRevisionSnapshot:
    """Capture an immutable revision snapshot of the working project state (excluding nested revisions)."""
    existing_ids = {r.revision_id for r in project.revisions}
    rev_num = len(project.revisions)
    while f"REV-{rev_num}" in existing_ids:
        rev_num += 1
    rev_id = f"REV-{rev_num}"

    bom = _live_bom_or_none(project)
    # Persist computed BOM into reporting if Phase 6 exists and reporting.bom wasn't populated yet
    if bom is not None and project.reporting.bom is None:
        project.reporting.bom = bom

    frozen = project.model_dump(exclude={"project_info", "revisions"})
    assert "revisions" not in frozen

    return ProjectRevisionSnapshot(
        revision_id=rev_id,
        label=label.strip() or f"Rev. {rev_num} Snapshot",
        created_at=_utc_now(),
        notes=notes.strip(),
        node_count=len(project.structural_model.nodes),
        member_count=len(project.structural_model.members),
        total_concrete_m3=bom.total_concrete_gross_m3 if bom is not None else 0.0,
        total_rebar_kg=bom.total_rebar_kg if bom is not None else 0.0,
        grand_total_cost_php=bom.grand_total_cost_php if bom is not None else 0.0,
        frozen_state=frozen,
    )


def restore_revision_snapshot(project: ProjectState, revision_id: str) -> ProjectState:
    """Restore working state from `revision_id` while preserving full `revisions` history."""
    snap = next((r for r in project.revisions if r.revision_id == revision_id), None)
    if snap is None:
        raise KeyError(f"Revision '{revision_id}' not found.")

    restored_dict = {
        "project_info": project.project_info.model_dump(),
        **{k: v for k, v in snap.frozen_state.items() if k not in ("project_info", "revisions")},
        "revisions": [r.model_dump() for r in project.revisions],
    }
    return ProjectState.model_validate(restored_dict)


def compare_against_revision(project: ProjectState, base_rev: str) -> dict:
    """Compute Concrete, Rebar, Cost, Member, and Footing deltas between `base_rev` and current state."""
    snap = next((r for r in project.revisions if r.revision_id == base_rev), None)
    if snap is None:
        raise KeyError(f"Revision '{base_rev}' not found.")

    cur_bom = _live_bom_or_none(project)
    cur_conc = cur_bom.total_concrete_gross_m3 if cur_bom is not None else 0.0
    cur_rebar = cur_bom.total_rebar_kg if cur_bom is not None else 0.0
    cur_cost = cur_bom.grand_total_cost_php if cur_bom is not None else 0.0

    base_conc = snap.total_concrete_m3
    base_rebar = snap.total_rebar_kg
    base_cost = snap.grand_total_cost_php

    base_members = snap.member_count
    cur_members = len(project.structural_model.members)

    base_ftgs = len(
        (snap.frozen_state.get("analysis_design") or {}).get("footing_designs") or []
    )
    cur_ftgs = len(project.analysis_design.footing_designs)

    return {
        "base_rev": snap.revision_id,
        "base_label": snap.label,
        "base_created_at": snap.created_at,
        "concrete_base_m3": round(base_conc, 3),
        "concrete_current_m3": round(cur_conc, 3),
        "concrete_delta_m3": round(cur_conc - base_conc, 3),
        "concrete_delta_pct": _pct_delta(cur_conc, base_conc),
        "rebar_base_kg": round(base_rebar, 2),
        "rebar_current_kg": round(cur_rebar, 2),
        "rebar_delta_kg": round(cur_rebar - base_rebar, 2),
        "rebar_delta_pct": _pct_delta(cur_rebar, base_rebar),
        "cost_base_php": round(base_cost, 2),
        "cost_current_php": round(cur_cost, 2),
        "cost_delta_php": round(cur_cost - base_cost, 2),
        "cost_delta_pct": _pct_delta(cur_cost, base_cost),
        "member_count_base": base_members,
        "member_count_current": cur_members,
        "member_count_delta": cur_members - base_members,
        "footing_count_base": base_ftgs,
        "footing_count_current": cur_ftgs,
        "footing_count_delta": cur_ftgs - base_ftgs,
    }
