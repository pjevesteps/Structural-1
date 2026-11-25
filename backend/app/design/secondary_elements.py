from __future__ import annotations

import math

from ..models import (
    DesignParameters,
    ProjectState,
    RoofTrussDesign,
    RoofTrussInput,
    SlabDesignSchedule,
    StairDesignInput,
    StairDesignResult,
    StrapFootingDesign,
)


def _bar_area_mm2(dia_mm: float) -> float:
    return math.pi * (dia_mm ** 2) / 4.0


def _pick_spacing(as_req_mm2_per_m: float, dia_mm: int, max_s: int = 300, min_s: int = 75) -> str:
    ab = _bar_area_mm2(dia_mm)
    raw = (ab * 1000.0) / max(as_req_mm2_per_m, 1.0)
    s = int(math.floor(min(raw, float(max_s)) / 25.0) * 25)
    s = max(min_s, s)
    return f"{dia_mm}mm @ {s}mm o.c."


def _flexural_as_per_m(mu_knm: float, t_mm: float, cover_mm: float, dia_mm: int, fc: float, fy: float) -> tuple[float, bool]:
    d_mm = max(50.0, t_mm - cover_mm - 0.5 * dia_mm)
    as_min = 0.0018 * 1000.0 * t_mm
    rn = (max(mu_knm, 0.0) * 1e6) / max(0.90 * 1000.0 * (d_mm ** 2), 1.0)
    disc = 1.0 - (2.0 * rn) / (0.85 * fc)
    if disc <= 0.05:
        return max(as_min, 0.012 * 1000.0 * d_mm), False
    rho = (0.85 * fc / fy) * (1.0 - math.sqrt(disc))
    return max(rho * 1000.0 * d_mm, as_min), True


# =============================================================================
# 1. ACI 318 Coefficient Method 2-Way / 1-Way Slab Reinforcement Schedules
# =============================================================================
def design_slabs(
    project: ProjectState,
    params: DesignParameters,
) -> list[SlabDesignSchedule]:
    """
    Auto-generate ACI 318 / NSCP 2015 Coefficient Method 2-way and 1-way RC slab
    reinforcement schedules for all active (non-void) SlabPanels.
    """
    sm = project.structural_model
    ms = project.member_sizing
    lg = project.load_generation
    mat = ms.material
    fc = mat.fc_mpa
    fy = mat.fy_main_mpa
    gamma_c = mat.density_kn_m3

    slab_asgn_by_pid = {s.panel_id: s for s in ms.slab_assignments}
    fl_by_lvl = {fl.floor_level: fl for fl in lg.floor_loads}

    out: list[SlabDesignSchedule] = []
    for panel in sm.slab_panels:
        if panel.slab_action == "void" or panel.net_area_sqm <= 0.0:
            continue
        sa = slab_asgn_by_pid.get(panel.panel_id)
        t_mm = sa.thickness_mm if sa is not None else 125.0
        lx = max(0.5, min(panel.lx_m, panel.ly_m))
        ly = max(lx, max(panel.lx_m, panel.ly_m))
        m_ratio = lx / max(ly, 1e-6)  # <= 1.0

        fl = fl_by_lvl.get(panel.floor_level)
        sdl = (
            (fl.sdl_finish_kpa + fl.sdl_ceiling_mep_kpa + fl.sdl_partitions_kpa)
            if fl is not None
            else 2.2
        )
        ll = fl.live_load_kpa if fl is not None else 2.4
        sw = gamma_c * (t_mm / 1000.0)
        wu = 1.2 * (sw + sdl) + 1.6 * ll

        # ACI 318 Coefficient Method (Case 4 / typical interior-edge panel)
        if m_ratio >= 0.50:
            # 2-way slab coefficients interpolated smoothly by aspect ratio m = lx/ly
            # m=1.0 -> Ca_neg=0.050, Ca_pos=0.036, Cb_neg=0.050, Cb_pos=0.036
            # m=0.5 -> Ca_neg=0.089, Ca_pos=0.062, Cb_neg=0.020, Cb_pos=0.014
            t_interp = (1.0 - m_ratio) / 0.50  # 0 at square, 1 at m=0.5
            ca_neg = 0.050 + 0.039 * t_interp
            ca_pos = 0.036 + 0.026 * t_interp
            cb_neg = 0.050 - 0.030 * t_interp
            cb_pos = 0.036 - 0.022 * t_interp
            mu_s_cont = ca_neg * wu * (lx ** 2)
            mu_s_disc = 0.67 * mu_s_cont
            mu_s_pos = ca_pos * wu * (lx ** 2)
            mu_l_neg = cb_neg * wu * (ly ** 2)
            mu_l_pos = cb_pos * wu * (ly ** 2)
            # Minimum thickness per NSCP 408.3.1.2 (perimeter / 180, min 100 mm)
            t_min_mm = max(100.0, (2.0 * (lx + ly) * 1000.0) / 180.0)
        else:
            # 1-way slab coefficients (ACI 6.5.2)
            mu_s_cont = wu * (lx ** 2) / 10.0
            mu_s_disc = wu * (lx ** 2) / 24.0
            mu_s_pos = wu * (lx ** 2) / 14.0
            mu_l_neg = 0.0
            mu_l_pos = 0.0
            t_min_mm = max(100.0, (lx * 1000.0) / 24.0)

        dia = 10 if t_mm <= 130.0 else 12
        max_s = int(min(3.0 * t_mm, 300.0))

        as_sc, ok1 = _flexural_as_per_m(mu_s_cont, t_mm, 20.0, dia, fc, fy)
        as_sd, ok2 = _flexural_as_per_m(mu_s_disc, t_mm, 20.0, dia, fc, fy)
        as_sp, ok3 = _flexural_as_per_m(mu_s_pos, t_mm, 20.0, dia, fc, fy)
        as_ln, ok4 = _flexural_as_per_m(mu_l_neg, t_mm, 20.0 + dia, dia, fc, fy)
        as_lp, ok5 = _flexural_as_per_m(mu_l_pos, t_mm, 20.0 + dia, dia, fc, fy)

        status = "PASS" if (ok1 and ok2 and ok3 and ok4 and ok5 and t_mm >= t_min_mm - 1.0) else "FAIL"
        out.append(
            SlabDesignSchedule(
                panel_id=panel.panel_id,
                floor_level=panel.floor_level,
                lx_m=round(lx, 2),
                ly_m=round(ly, 2),
                thickness_mm=round(t_mm, 1),
                short_span_cont_neg=_pick_spacing(as_sc, dia, max_s),
                short_span_disc_neg=_pick_spacing(as_sd, dia, max_s),
                short_span_pos=_pick_spacing(as_sp, dia, max_s),
                long_span_neg=_pick_spacing(as_ln, dia, max_s),
                long_span_pos=_pick_spacing(as_lp, dia, max_s),
                status=status,
            )
        )
    return out


# =============================================================================
# 2. Waist-Slab RC Stair Design (NSCP 2015 / ACI 318)
# =============================================================================
def design_stair(project: ProjectState, inp: StairDesignInput) -> StairDesignResult:
    mat = project.member_sizing.material
    fc = mat.fc_mpa
    fy = mat.fy_main_mpa
    gamma_c = mat.density_kn_m3

    # Inclined waist slab dead load + triangular steps + floor finish (1.2 kPa)
    cos_a = inp.tread_mm / math.hypot(inp.riser_mm, inp.tread_mm)
    t_waist_m = inp.waist_thickness_mm / 1000.0
    t_avg_step_m = 0.5 * (inp.riser_mm / 1000.0)
    dl_kpa = gamma_c * (t_waist_m / max(cos_a, 0.5) + t_avg_step_m) + 1.2
    wu_kpa = 1.2 * dl_kpa + 1.6 * inp.live_load_kpa

    # Simply-supported / semi-continuous flight (w L^2 / 8 conservatively)
    mu_knm_per_m = (wu_kpa * (inp.flight_span_m ** 2)) / 8.0

    main_dia = 12 if mu_knm_per_m <= 28.0 else 16
    as_main, flex_ok = _flexural_as_per_m(mu_knm_per_m, inp.waist_thickness_mm, 20.0, main_dia, fc, fy)
    as_dist = 0.0018 * 1000.0 * inp.waist_thickness_mm

    # ACI 318 Table 7.3.1.1 simply supported solid one-way slab min thickness L / 20
    t_min_mm = (inp.flight_span_m * 1000.0) / 20.0
    defl_ok = inp.waist_thickness_mm >= t_min_mm and flex_ok

    return StairDesignResult(
        stair_id=inp.stair_id,
        flight_span_m=round(inp.flight_span_m, 2),
        riser_mm=round(inp.riser_mm, 1),
        tread_mm=round(inp.tread_mm, 1),
        width_m=round(inp.width_m, 2),
        waist_thickness_mm=round(inp.waist_thickness_mm, 1),
        live_load_kpa=round(inp.live_load_kpa, 2),
        mu_knm_per_m=round(mu_knm_per_m, 2),
        main_bars=_pick_spacing(as_main, main_dia, max_s=250),
        dist_bars=_pick_spacing(as_dist, 10, max_s=300),
        deflection_status="PASS" if defl_ok else "FAIL",
    )


# =============================================================================
# 3. Eccentric Property-Line Strap Footing Design
# =============================================================================
def design_strap_footing(
    project: ProjectState,
    exterior_node_id: int,
    interior_node_id: int,
    strap_id: str = "SF-1",
) -> StrapFootingDesign:
    sm = project.structural_model
    ad = project.analysis_design
    mat = project.member_sizing.material
    fc = mat.fc_mpa
    fy = mat.fy_main_mpa
    fyt = mat.fy_tie_mpa
    qa = ad.params.soil_bearing_qa_kpa

    node_by_id = {n.node_id: n for n in sm.nodes}
    rxn_by_nid = {r.node_id: r for r in ad.support_reactions}

    n_ext = node_by_id.get(exterior_node_id)
    n_int = node_by_id.get(interior_node_id)
    if n_ext is None or n_int is None:
        raise ValueError("Exterior or interior support node ID not found in structural model.")
    if exterior_node_id == interior_node_id:
        raise ValueError("Exterior and interior node IDs must be different.")

    s_center = math.hypot(n_int.x_m - n_ext.x_m, n_int.z_m - n_ext.z_m)
    if s_center < 1.0:
        raise ValueError("Support nodes are too close (< 1.0 m) for a strap footing.")

    r_ext_env = rxn_by_nid.get(exterior_node_id)
    r_int_env = rxn_by_nid.get(interior_node_id)
    p1_serv = max(r_ext_env.max_asd_fy_kn if r_ext_env else 250.0, 50.0)
    p2_serv = max(r_int_env.max_asd_fy_kn if r_int_env else 400.0, 50.0)
    p1_u = max(r_ext_env.max_lrfd_pu_kn if r_ext_env else 1.4 * p1_serv, 1.35 * p1_serv)

    # Exterior pad width B1 along strap line; eccentricity e = (B1 - c1)/2
    col_w_m = 0.40
    q_net = max(30.0, qa - ad.params.footing_embedment_m * ad.params.soil_unit_weight_kn_m3)
    b1 = max(1.2, round(math.ceil(math.sqrt(p1_serv / q_net) * 10.0) / 10.0, 2))
    ecc = max(0.25, 0.5 * (b1 - col_w_m))
    s_prime = max(0.8, s_center - ecc)

    # Moment equilibrium about interior column -> soil reaction R1 on exterior pad, R2 on interior pad
    r1_serv = (p1_serv * s_center) / s_prime
    r2_serv = max(0.2 * p2_serv, p1_serv + p2_serv - r1_serv)

    l1 = max(1.2, round(math.ceil((r1_serv / (q_net * b1)) * 10.0) / 10.0, 2))
    b2 = max(1.2, round(math.ceil(math.sqrt(r2_serv / q_net) * 10.0) / 10.0, 2))
    l2 = b2

    # Strap beam factored shear & negative hogging moment (tension on TOP)
    r1_u = (p1_u * s_center) / s_prime
    vu_strap = max(abs(r1_u - p1_u), 0.25 * p1_u)
    mu_strap = p1_u * ecc

    strap_b = 400.0
    strap_h = max(550.0, math.ceil((s_center * 1000.0 / 9.0) / 50.0) * 50.0)
    d_strap = strap_h - 75.0 - 10.0 - 10.0

    # Top negative flexural steel (tension at top of strap beam)
    rn = (mu_strap * 1e6) / max(0.90 * strap_b * (d_strap ** 2), 1.0)
    disc = 1.0 - (2.0 * rn) / (0.85 * fc)
    flex_ok = disc > 0.05
    rho_req = (0.85 * fc / fy) * (1.0 - math.sqrt(max(0.05, disc)))
    rho_min = max(0.25 * math.sqrt(fc) / fy, 1.4 / fy)
    as_top = max(rho_req, rho_min) * strap_b * d_strap
    as_bot = max(0.5 * as_top, rho_min * strap_b * d_strap)

    ab20 = _bar_area_mm2(20.0)
    n_top = max(3, int(math.ceil(as_top / ab20)))
    n_bot = max(3, int(math.ceil(as_bot / ab20)))

    # Shear design
    vc_kn = 0.17 * math.sqrt(fc) * strap_b * d_strap / 1000.0
    vs_req = max(0.0, (vu_strap / 0.75) - vc_kn)
    vs_max = 0.66 * math.sqrt(fc) * strap_b * d_strap / 1000.0
    shear_ok = vs_req <= vs_max
    av10 = 2.0 * _bar_area_mm2(10.0)
    s_req = (av10 * fyt * d_strap) / (vs_req * 1000.0) if vs_req > 1e-4 else 200.0
    s_stir = max(75, int(math.floor(min(s_req, d_strap / 2.0, 200.0) / 25.0) * 25))

    return StrapFootingDesign(
        strap_id=strap_id,
        exterior_node_id=exterior_node_id,
        interior_node_id=interior_node_id,
        eccentricity_m=round(ecc, 3),
        ext_pad_b_m=round(b1, 2),
        ext_pad_l_m=round(l1, 2),
        int_pad_b_m=round(b2, 2),
        int_pad_l_m=round(l2, 2),
        strap_b_mm=strap_b,
        strap_h_mm=strap_h,
        strap_top_bars=f"{n_top}-20mm",
        strap_bot_bars=f"{n_bot}-20mm",
        strap_stirrups=f"10mm @ {s_stir}mm o.c.",
        status="PASS" if (flex_ok and shear_ok) else "FAIL",
    )


# =============================================================================
# 4. Steel Roof Truss Design (NSCP Ch. 5 / AISC 360 Double-Angle Catalog)
# =============================================================================
_DOUBLE_ANGLE_CATALOG: list[tuple[str, float, float, float]] = [
    # (label, Area_mm2, r_min_mm, weight_kg_m)
    ("2L 50x50x6", 1128.0, 15.1, 8.86),
    ("2L 65x65x6", 1488.0, 19.8, 11.68),
    ("2L 75x75x6", 1728.0, 22.9, 13.56),
    ("2L 75x75x8", 2272.0, 22.7, 17.84),
    ("2L 90x90x8", 2752.0, 27.4, 21.60),
    ("2L 100x100x8", 3072.0, 30.6, 24.12),
    ("2L 100x100x10", 3800.0, 30.4, 29.83),
    ("2L 125x125x10", 4800.0, 38.2, 37.68),
]


def _aisc_compression_phi_pn_kn(area_mm2: float, r_mm: float, length_mm: float, fy_mpa: float) -> float:
    e_mpa = 200000.0
    kl_r = max(1.0, length_mm / max(r_mm, 1.0))
    fe = (math.pi ** 2) * e_mpa / (kl_r ** 2)
    if kl_r <= 4.71 * math.sqrt(e_mpa / fy_mpa):
        fcr = (0.658 ** (fy_mpa / fe)) * fy_mpa
    else:
        fcr = 0.877 * fe
    return 0.90 * fcr * area_mm2 / 1000.0


def _aisc_tension_phi_pn_kn(area_mm2: float, fy_mpa: float) -> float:
    return 0.90 * fy_mpa * area_mm2 / 1000.0


def design_roof_truss(project: ProjectState, inp: RoofTrussInput) -> RoofTrussDesign:
    """
    Design a pitched steel roof truss (Pratt, Howe, or Fink) under NSCP 2015 gravity
    (roofing + purlins + ceiling + roof live load) and wind pressure using double-angle sections.
    """
    span = inp.span_m
    spacing = inp.truss_spacing_m
    pitch_rad = math.radians(inp.pitch_deg)
    rise = 0.5 * span * math.tan(pitch_rad)
    half_top_len = math.hypot(0.5 * span, rise)

    # NSCP gravity + wind roof loads on horizontal projection (kPa)
    dl_kpa = 0.65  # GI roofing + purlins + ceiling + self-weight allowance
    lr_kpa = 0.60  # NSCP Table 205-2 roof live load
    wind_v = project.load_generation.wind.basic_wind_speed_kph if project.load_generation else 250.0
    qz_kpa = 0.613 * 0.85 * ((wind_v / 3.6) ** 2) / 1000.0
    w_down_kpa = max(1.2 * dl_kpa + 1.6 * lr_kpa, 1.2 * dl_kpa + 0.5 * lr_kpa + 0.6 * qz_kpa)

    wu_line = w_down_kpa * spacing  # kN/m along truss span
    mu_max = (wu_line * (span ** 2)) / 8.0
    vu_max = 0.5 * wu_line * span

    # Chord axial forces at midspan & support
    h_eff = max(rise, 0.35)
    pu_bot_tens = mu_max / h_eff
    pu_top_comp = pu_bot_tens / max(math.cos(pitch_rad), 0.5)

    # Panel count & web geometry by truss type
    n_panels = max(4, int(round(span / 1.5) // 2 * 2))
    panel_len_m = span / n_panels
    chord_unbraced_mm = (half_top_len / (n_panels / 2.0)) * 1000.0
    web_len_mm = math.hypot(panel_len_m, 0.5 * rise) * 1000.0

    # Web axial demand (Howe has compression diagonals; Pratt/Fink have compression verticals)
    web_angle = math.atan2(0.5 * rise, panel_len_m)
    pu_web = vu_max / max(math.sin(web_angle), 0.35)
    if inp.truss_type == "Fink":
        pu_web *= 0.85

    def _select_section(pu_kn: float, unbraced_mm: float, is_comp: bool) -> tuple[str, float, float]:
        best = _DOUBLE_ANGLE_CATALOG[-1]
        best_dcr = 999.0
        for label, area, r_min, wt in _DOUBLE_ANGLE_CATALOG:
            cap = (
                _aisc_compression_phi_pn_kn(area, r_min, unbraced_mm, inp.steel_fy_mpa)
                if is_comp
                else _aisc_tension_phi_pn_kn(area, inp.steel_fy_mpa)
            )
            dcr = pu_kn / max(cap, 1e-6)
            best = (label, area, r_min, wt)
            best_dcr = dcr
            if dcr <= 0.90:
                break
        return best[0], round(best_dcr, 3), best[3]

    top_sec, top_dcr, top_wt = _select_section(pu_top_comp, chord_unbraced_mm, is_comp=True)
    bot_sec, bot_dcr, bot_wt = _select_section(pu_bot_tens, panel_len_m * 1000.0, is_comp=False)
    web_sec, web_dcr, web_wt = _select_section(pu_web, web_len_mm, is_comp=True)

    # Total steel mass for one truss (+10% gusset plates & bolts)
    total_top_m = 2.0 * half_top_len
    total_bot_m = span
    total_web_m = (n_panels - 1) * (web_len_mm / 1000.0) * 1.15
    est_steel_kg = 1.10 * (total_top_m * top_wt + total_bot_m * bot_wt + total_web_m * web_wt)

    max_chord_dcr = max(top_dcr, bot_dcr)
    status = "PASS" if (max_chord_dcr <= 1.0 and web_dcr <= 1.0) else "FAIL"

    return RoofTrussDesign(
        truss_id=inp.truss_id,
        span_m=round(span, 2),
        truss_spacing_m=round(spacing, 2),
        pitch_deg=round(inp.pitch_deg, 1),
        truss_type=inp.truss_type,
        steel_fy_mpa=round(inp.steel_fy_mpa, 1),
        top_chord_section=top_sec,
        bottom_chord_section=bot_sec,
        web_section=web_sec,
        max_chord_dcr=max_chord_dcr,
        max_web_dcr=web_dcr,
        estimated_steel_kg=round(est_steel_kg, 1),
        status=status,
    )
