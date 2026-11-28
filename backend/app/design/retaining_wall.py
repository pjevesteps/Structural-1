from __future__ import annotations

import math

from ..models import ProjectState, RetainingWallDesign, RetainingWallInput


def _bar_area_mm2(dia_mm: float) -> float:
    return math.pi * (dia_mm ** 2) / 4.0


def _pick_spacing_callout(
    as_req_mm2_per_m: float,
    dia_mm: int,
    max_spacing_mm: int = 300,
    min_spacing_mm: int = 100,
) -> str:
    ab = _bar_area_mm2(dia_mm)
    raw_s = (ab * 1000.0) / max(as_req_mm2_per_m, 1.0)
    s = int(math.floor(min(raw_s, float(max_spacing_mm)) / 25.0) * 25)
    s = max(min_spacing_mm, s)
    return f"{dia_mm}mm @ {s}mm o.c."


def _mononobe_okabe_kae(phi_deg: float, kh: float, kv: float = 0.0) -> float:
    """
    Mononobe-Okabe seismic active earth pressure coefficient K_ae for vertical wall
    with horizontal backfill (beta = 0, i = 0, delta = phi / 2).
    """
    phi = math.radians(phi_deg)
    delta = math.radians(0.5 * phi_deg)
    theta = math.atan2(kh, max(1.0 - kv, 0.5))
    if phi - theta <= math.radians(2.0):
        # Soil near liquefaction / sliding limit under extreme kh -> conservative cap
        return min(1.25, math.tan(math.radians(45.0 - phi_deg / 2.0)) ** 2 + 0.75 * kh)
    num = math.cos(phi - theta) ** 2
    term = math.sqrt(
        (math.sin(phi + delta) * math.sin(phi - theta))
        / max(math.cos(delta + theta), 0.1)
    )
    denom = math.cos(theta) * math.cos(delta + theta) * ((1.0 + term) ** 2)
    return max(0.0, num / max(denom, 1e-6))


def design_retaining_wall(
    project: ProjectState,
    inp: RetainingWallInput,
) -> RetainingWallDesign:
    """
    NSCP 2015 Section 209 Basement / Cantilever Retaining Wall design:
      - Rankine active Ka & Jaky at-rest K0
      - Mononobe-Okabe dynamic increment K_ae using project Seismic Zone Z (kh = 0.5 * Z)
      - Optional hydrostatic pressure from water_table_height_m
      - Overturning & sliding stability checks (FS >= 1.5 static, >= 1.1 seismic)
      - Toe soil bearing pressure check (<= allowable_bearing_kpa)
      - RC stem vertical/horizontal & base heel/toe reinforcement per NSCP 2015 / ACI 318
    """
    mat = project.member_sizing.material
    fc = mat.fc_mpa
    fy = mat.fy_main_mpa
    gamma_c = mat.density_kn_m3

    zone = project.load_generation.seismic.zone if project.load_generation else 4
    z_factor = 0.40 if zone == 4 else 0.20
    kh = 0.5 * z_factor  # Standard NSCP/AASHTO pseudo-static horizontal acceleration

    phi_rad = math.radians(inp.friction_angle_deg)
    ka = (1.0 - math.sin(phi_rad)) / max(1.0 + math.sin(phi_rad), 1e-6)
    k0 = 1.0 - math.sin(phi_rad)
    k_ae = _mononobe_okabe_kae(inp.friction_angle_deg, kh)
    delta_kae = max(0.0, k_ae - ka)

    # Top-restrained basement walls develop at-rest pressure K0; free cantilevers use Ka
    k_lat = k0 if inp.is_restrained_top else ka

    h_stem = inp.retained_height_m
    t_base = inp.base_thickness_mm / 1000.0
    t_stem = inp.stem_thickness_mm / 1000.0
    h_total = h_stem + t_base
    b_base = inp.base_width_m
    b_toe = min(inp.toe_width_m, max(0.0, b_base - t_stem))
    b_heel = max(0.0, b_base - b_toe - t_stem)

    gamma_s = inp.soil_unit_weight_kn_m3
    gamma_w = 9.81
    hw = min(inp.water_table_height_m, h_total)
    gamma_sub = max(6.0, gamma_s - gamma_w)

    # Static lateral thrust (per meter length)
    if hw <= 1e-4:
        p_soil_kn = 0.5 * k_lat * gamma_s * (h_total ** 2)
        p_water_kn = 0.0
        max_p_static = k_lat * (gamma_s * h_total + inp.surcharge_kpa)
        y_soil = h_total / 3.0
    else:
        h_dry = max(0.0, h_total - hw)
        p1 = k_lat * gamma_s * h_dry
        p2 = p1 + k_lat * gamma_sub * hw
        p_soil_kn = 0.5 * p1 * h_dry + 0.5 * (p1 + p2) * hw
        p_water_kn = 0.5 * gamma_w * (hw ** 2)
        max_p_static = p2 + gamma_w * hw + k_lat * inp.surcharge_kpa
        y_soil = h_total / 3.0

    p_surch_kn = k_lat * inp.surcharge_kpa * h_total
    p_static_kn = p_soil_kn + p_water_kn + p_surch_kn

    # Mononobe-Okabe dynamic increment applied at 0.6 * H from base
    p_seis_inc_kn = 0.5 * delta_kae * gamma_s * (h_total ** 2)
    max_lat_pressure = max_p_static + delta_kae * gamma_s * h_total

    # Stabilizing vertical weights & resisting moments about toe (per meter length)
    w_stem = t_stem * h_stem * gamma_c
    x_stem = b_toe + 0.5 * t_stem

    w_base = b_base * t_base * gamma_c
    x_base = 0.5 * b_base

    w_soil = b_heel * h_stem * gamma_s
    x_soil = b_toe + t_stem + 0.5 * b_heel

    w_surch = b_heel * inp.surcharge_kpa
    x_surch = x_soil

    v_total = w_stem + w_base + w_soil
    v_with_surch = v_total + w_surch
    m_res = w_stem * x_stem + w_base * x_base + w_soil * x_soil

    m_ot_static = (
        p_soil_kn * y_soil
        + p_water_kn * (hw / 3.0 if hw > 0 else 0.0)
        + p_surch_kn * (0.5 * h_total)
    )
    m_ot_seismic = m_ot_static + p_seis_inc_kn * (0.6 * h_total)

    fs_ot_static = m_res / max(m_ot_static, 1e-6)
    fs_ot_seis = m_res / max(m_ot_seismic, 1e-6)

    mu_base = math.tan(math.radians(2.0 * inp.friction_angle_deg / 3.0))
    kp = (1.0 + math.sin(phi_rad)) / max(1.0 - math.sin(phi_rad), 1e-6)
    # Passive resistance in front of toe / embedded base slab (conservative 50% mobilization)
    h_toe_embed = t_base + (0.35 if inp.is_restrained_top else 0.15)
    p_passive_kn = 0.5 * (0.5 * kp * gamma_s * (h_toe_embed ** 2))
    # Restrained basement slab at top carries ~35% of lateral thrust as a propped cantilever
    base_thrust_ratio = 0.65 if inp.is_restrained_top else 1.0
    v_res_slide = mu_base * v_total + p_passive_kn
    fs_sl_static = v_res_slide / max(base_thrust_ratio * p_static_kn, 1e-6)
    fs_sl_seis = v_res_slide / max(base_thrust_ratio * (p_static_kn + p_seis_inc_kn), 1e-6)

    # Toe bearing pressure (service static)
    x_net = (m_res + w_surch * x_surch - m_ot_static) / max(v_with_surch, 1e-6)
    ecc = abs(0.5 * b_base - x_net)
    if ecc <= b_base / 6.0:
        q_toe = (v_with_surch / b_base) * (1.0 + 6.0 * ecc / b_base)
    else:
        q_toe = (2.0 * v_with_surch) / max(3.0 * x_net, 0.15 * b_base)

    # Stem structural design (LRFD 1.6*H)
    p_stem_soil = 0.5 * k_lat * gamma_s * (h_stem ** 2)
    p_stem_surch = k_lat * inp.surcharge_kpa * h_stem
    p_stem_water = 0.5 * gamma_w * (max(0.0, hw - t_base) ** 2)
    if inp.is_restrained_top:
        # Propped cantilever (fixed base, pinned at ground floor slab): M_base = w*L^2/15 + q*L^2/8
        mu_stem_stat = 1.6 * (
            (k_lat * gamma_s * (h_stem ** 3)) / 15.0
            + (k_lat * inp.surcharge_kpa * (h_stem ** 2)) / 8.0
            + (gamma_w * (max(0.0, hw - t_base) ** 3)) / 15.0
        )
        vu_stem = 1.6 * (0.65 * p_stem_soil + 0.625 * p_stem_surch + 0.65 * p_stem_water)
    else:
        # Free cantilever stem
        mu_stem_stat = 1.6 * (
            p_stem_soil * (h_stem / 3.0)
            + p_stem_surch * (0.5 * h_stem)
            + p_stem_water * (max(0.0, hw - t_base) / 3.0)
        )
        vu_stem = 1.6 * (p_stem_soil + p_stem_surch + p_stem_water)

    # Seismic stem moment (1.0*H_static + 1.0*E_ae)
    mu_stem_seis = (mu_stem_stat / 1.6) + (0.5 * delta_kae * gamma_s * (h_stem ** 2)) * (0.6 * h_stem) * (
        0.55 if inp.is_restrained_top else 1.0
    )
    mu_stem = max(mu_stem_stat, mu_stem_seis)

    # Flexural & shear capacity of stem (1000 mm unit strip)
    cover_mm = 50.0
    d_stem = max(80.0, inp.stem_thickness_mm - cover_mm - 8.0)
    phi_vc_stem = 0.75 * 0.17 * math.sqrt(fc) * 1000.0 * d_stem / 1000.0
    stem_shear_ok = vu_stem <= phi_vc_stem

    rn = (mu_stem * 1e6) / max(0.90 * 1000.0 * (d_stem ** 2), 1.0)
    disc = 1.0 - (2.0 * rn) / (0.85 * fc)
    stem_flex_ok = disc > 0.05
    if stem_flex_ok:
        rho_req = (0.85 * fc / fy) * (1.0 - math.sqrt(disc))
    else:
        rho_req = 0.015
    as_min_vert = 0.0018 * 1000.0 * inp.stem_thickness_mm
    as_vert = max(rho_req * 1000.0 * d_stem, as_min_vert)
    as_horiz = 0.0020 * 1000.0 * inp.stem_thickness_mm

    # Base slab heel/toe flexure
    d_base = max(120.0, inp.base_thickness_mm - 75.0 - 8.0)
    mu_heel = 1.4 * (gamma_s * h_stem + inp.surcharge_kpa + gamma_c * t_base) * (b_heel ** 2) * 0.5
    mu_toe = 1.6 * q_toe * (b_toe ** 2) * 0.5
    mu_base = max(mu_heel, mu_toe, 10.0)
    rn_b = (mu_base * 1e6) / max(0.90 * 1000.0 * (d_base ** 2), 1.0)
    disc_b = max(0.05, 1.0 - (2.0 * rn_b) / (0.85 * fc))
    rho_b = (0.85 * fc / fy) * (1.0 - math.sqrt(disc_b))
    as_base = max(rho_b * 1000.0 * d_base, 0.0018 * 1000.0 * inp.base_thickness_mm)

    vert_dia = 16 if as_vert > 900.0 else 12
    base_dia = 16 if as_base > 900.0 else 12
    stem_vert_str = _pick_spacing_callout(as_vert, vert_dia, max_spacing_mm=250)
    stem_horiz_str = _pick_spacing_callout(as_horiz, 12, max_spacing_mm=250)
    base_bars_str = _pick_spacing_callout(as_base, base_dia, max_spacing_mm=250)

    stability_ok = (
        fs_ot_static >= 1.5
        and fs_sl_static >= 1.5
        and fs_ot_seis >= 1.1
        and fs_sl_seis >= 1.1
        and q_toe <= inp.allowable_bearing_kpa
        and stem_shear_ok
        and stem_flex_ok
    )

    effective_len_m = inp.total_length_m
    if inp.start_xyz_m is not None and inp.end_xyz_m is not None:
        spatial_len = math.hypot(
            inp.end_xyz_m[0] - inp.start_xyz_m[0],
            inp.end_xyz_m[2] - inp.start_xyz_m[2],
        )
        if spatial_len >= 0.5:
            effective_len_m = round(spatial_len, 2)

    return RetainingWallDesign(
        wall_id=inp.wall_id,
        label=inp.label,
        retained_height_m=round(h_stem, 3),
        total_length_m=round(effective_len_m, 2),
        floor_level=inp.floor_level,
        start_xyz_m=list(inp.start_xyz_m) if inp.start_xyz_m is not None else None,
        end_xyz_m=list(inp.end_xyz_m) if inp.end_xyz_m is not None else None,
        backfill_side=inp.backfill_side,
        ka=round(ka, 3),
        k0=round(k0, 3),
        k_ae_seismic=round(k_ae, 3),
        max_lateral_pressure_kpa=round(max_lat_pressure, 2),
        stem_mu_knm_per_m=round(mu_stem, 2),
        stem_vu_kn_per_m=round(vu_stem, 2),
        fs_overturning=round(fs_ot_static, 2),
        fs_sliding=round(fs_sl_static, 2),
        max_toe_bearing_kpa=round(q_toe, 2),
        stem_vertical_bars=stem_vert_str,
        stem_horizontal_bars=stem_horiz_str,
        base_heel_toe_bars=base_bars_str,
        status="PASS" if stability_ok else "FAIL",
    )
