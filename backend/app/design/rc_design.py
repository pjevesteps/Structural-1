import csv
from datetime import datetime, timezone
import io
import math

from ..models import (
    AnalysisDesignState,
    BeamDesignResult,
    ColumnDesignResult,
    DesignCheckIssue,
    DesignParameters,
    FootingDesignResult,
    ProjectState,
    StoryDriftResult,
    SupportReactionEnvelope,
)
from .anl_parser import parse_staad_anl
from .solver import AnalysisResults, BeamForceEnvelope, ColumnForceEnvelope, solve_frame_dsm


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bar_area_mm2(dia_mm: float) -> float:
    return math.pi * (dia_mm ** 2) / 4.0


def _beta1(fc_mpa: float) -> float:
    return max(0.65, min(0.85, 0.85 - 0.05 * max(0.0, fc_mpa - 28.0) / 7.0))


def compute_story_drifts(
    project: ProjectState,
    results: AnalysisResults,
) -> list[StoryDriftResult]:
    """
    NSCP 2015 Section 208.6.4 story drift & Table 208-9 soft-story irregularity check.
      - delta_m = 0.7 * R * delta_s (Eq. 208-21)
      - allowable = 0.025 * h (T < 0.7 s) else 0.020 * h
    """
    model = project.structural_model
    lg = project.load_generation
    seis = lg.seismic
    seis_sum = lg.seismic_summary

    levels = sorted(project.levels, key=lambda lv: lv.elevation_m)
    if len(levels) < 2:
        return []
    base_elev = levels[0].elevation_m
    elevated = [lv for lv in levels if lv.elevation_m > base_elev + 1e-4]

    period_t = seis_sum.period_t_sec if seis_sum is not None else 0.5
    drift_limit_factor = 0.025 if period_t < 0.7 else 0.020

    shear_by_lvl: dict[str, tuple[float, float]] = {}
    if seis_sum is not None:
        for st in seis_sum.stories:
            shear_by_lvl[st.floor_level] = (st.cumulative_shear_x_kn, st.cumulative_shear_z_kn)

    disp_x_map = results.seismic_displacements_mm.get("EX+", {})
    disp_z_map = results.seismic_displacements_mm.get("EZ+", {})

    raw_rows: list[dict] = []
    prev_elev = base_elev
    prev_dx = 0.0
    prev_dz = 0.0

    for lv in elevated:
        h_m = max(lv.elevation_m - prev_elev, 0.1)
        lvl_nodes = [n for n in model.nodes if abs(n.y_m - lv.elevation_m) <= 0.15]
        free_nodes = [n for n in lvl_nodes if not n.is_support and n.support_type == "NONE"]
        use_nodes = free_nodes if free_nodes else lvl_nodes

        dx_vals = [abs(disp_x_map[n.node_id][0]) for n in use_nodes if n.node_id in disp_x_map]
        dz_vals = [abs(disp_z_map[n.node_id][2]) for n in use_nodes if n.node_id in disp_z_map]
        avg_dx = sum(dx_vals) / len(dx_vals) if dx_vals else prev_dx
        avg_dz = sum(dz_vals) / len(dz_vals) if dz_vals else prev_dz

        ds_x = abs(avg_dx - prev_dx)
        ds_z = abs(avg_dz - prev_dz)
        dm_x = 0.7 * seis.r_factor_x * ds_x
        dm_z = 0.7 * seis.r_factor_z * ds_z
        allow_mm = drift_limit_factor * h_m * 1000.0
        ratio_x = dm_x / allow_mm if allow_mm > 1e-6 else 0.0
        ratio_z = dm_z / allow_mm if allow_mm > 1e-6 else 0.0

        vx_kn, vz_kn = shear_by_lvl.get(lv.id, (0.0, 0.0))
        kx = (vx_kn / (ds_x / 1000.0)) if ds_x > 1e-4 else 0.0
        kz = (vz_kn / (ds_z / 1000.0)) if ds_z > 1e-4 else 0.0

        raw_rows.append(
            {
                "floor_level": lv.id,
                "elevation_m": round(lv.elevation_m, 3),
                "story_height_m": round(h_m, 3),
                "elastic_disp_x_mm": round(avg_dx, 3),
                "elastic_disp_z_mm": round(avg_dz, 3),
                "delta_s_x_mm": round(ds_x, 3),
                "delta_s_z_mm": round(ds_z, 3),
                "delta_m_x_mm": round(dm_x, 3),
                "delta_m_z_mm": round(dm_z, 3),
                "allowable_drift_mm": round(allow_mm, 2),
                "drift_ratio_x": round(ratio_x, 4),
                "drift_ratio_z": round(ratio_z, 4),
                "stiffness_x_kn_m": round(kx, 1),
                "stiffness_z_kn_m": round(kz, 1),
                "soft_story_flag": False,
                "status": "PASS" if max(ratio_x, ratio_z) <= 1.0 else "FAIL",
            }
        )
        prev_elev = lv.elevation_m
        prev_dx = avg_dx
        prev_dz = avg_dz

    # Soft-story irregularity check (NSCP 2015 Table 208-9)
    n_st = len(raw_rows)
    for i in range(n_st - 1):
        soft = False
        for key in ("stiffness_x_kn_m", "stiffness_z_kn_m"):
            ki = raw_rows[i][key]
            k_above = raw_rows[i + 1][key]
            if ki > 0 and k_above > 0 and ki < 0.70 * k_above:
                soft = True
            above_3 = [raw_rows[j][key] for j in range(i + 1, min(n_st, i + 4)) if raw_rows[j][key] > 0]
            if ki > 0 and len(above_3) >= 2:
                avg3 = sum(above_3) / len(above_3)
                if ki < 0.80 * avg3:
                    soft = True
        raw_rows[i]["soft_story_flag"] = soft

    return [StoryDriftResult(**r) for r in raw_rows]


def _design_beam_flexure_zone(
    mu_knm: float,
    b_mm: float,
    d_mm: float,
    fc: float,
    fy: float,
    beta1: float,
    bar_dia_mm: int,
    clear_cover_mm: float,
    stirrup_dia_mm: int,
) -> tuple[float, str, float, bool]:
    """Return (as_req_mm2, bar_callout, dcr, two_layers_needed)."""
    ab = _bar_area_mm2(bar_dia_mm)
    rho_min = max(0.25 * math.sqrt(fc) / fy, 1.4 / fy)
    as_min = rho_min * b_mm * d_mm

    c_max = 0.375 * d_mm
    a_max = beta1 * c_max
    as_max = (0.85 * fc * b_mm * a_max) / fy
    phi_mn_max_knm = 0.90 * as_max * fy * (d_mm - 0.5 * a_max) / 1e6

    mu = max(0.0, mu_knm)
    rn = (mu * 1e6) / max(0.90 * b_mm * (d_mm ** 2), 1.0)
    disc = 1.0 - (2.0 * rn) / (0.85 * fc)

    over_reinforced = disc <= 0.0 or mu > phi_mn_max_knm
    if over_reinforced:
        as_req = max(as_max, as_min)
        n_bars = max(2, int(math.ceil(as_req / ab)))
        dcr = mu / max(phi_mn_max_knm, 1e-6)
    else:
        rho_req = (0.85 * fc / fy) * (1.0 - math.sqrt(disc))
        as_req = max(rho_req * b_mm * d_mm, as_min)
        n_bars = max(2, int(math.ceil(as_req / ab)))
        as_prov = n_bars * ab
        a_prov = (as_prov * fy) / (0.85 * fc * b_mm)
        phi_mn_prov = 0.90 * as_prov * fy * max(d_mm - 0.5 * a_prov, 10.0) / 1e6
        dcr = mu / max(phi_mn_prov, 1e-6)

    # Clear spacing check for 1 layer
    avail_w = b_mm - 2.0 * (clear_cover_mm + stirrup_dia_mm)
    clear_sp = (avail_w - n_bars * bar_dia_mm) / max(n_bars - 1, 1)
    two_layers = clear_sp < max(25.0, float(bar_dia_mm))

    return round(as_req, 1), f"{n_bars}-{bar_dia_mm}mm", round(dcr, 3), two_layers


def design_beams(
    project: ProjectState,
    results: AnalysisResults,
    params: DesignParameters,
) -> list[BeamDesignResult]:
    """Design all beam/tie/trimmer members per NSCP 2015 / ACI 318 flexure & shear."""
    model = project.structural_model
    sizing = project.member_sizing
    mat = sizing.material
    fc = mat.fc_mpa
    fy = mat.fy_main_mpa
    fyt = mat.fy_tie_mpa
    b1 = _beta1(fc)

    sec_by_id = {s.section_id: s for s in sizing.sections}
    asgn_by_mid = {a.member_id: a for a in sizing.member_assignments}
    db = params.main_bar_dia_beam_mm
    ds = params.stirrup_tie_dia_mm
    av = 2.0 * _bar_area_mm2(ds)

    out: list[BeamDesignResult] = []
    for m in sorted(model.members, key=lambda x: x.member_id):
        if m.member_type == "column":
            continue
        asgn = asgn_by_mid.get(m.member_id)
        sec = sec_by_id.get(asgn.section_id) if asgn else None
        if sec is None:
            continue
        env = results.beam_envelopes.get(m.member_id, BeamForceEnvelope(member_id=m.member_id))

        b_mm = sec.width_mm
        h_mm = sec.depth_mm
        d_mm = max(50.0, h_mm - params.clear_cover_beam_mm - ds - 0.5 * db)

        notes: list[str] = []
        as_ts, bars_ts, dcr_ts, lay_ts = _design_beam_flexure_zone(
            env.mu_neg_start_knm, b_mm, d_mm, fc, fy, b1, db, params.clear_cover_beam_mm, ds
        )
        as_bm, bars_bm, dcr_bm, lay_bm = _design_beam_flexure_zone(
            env.mu_pos_mid_knm, b_mm, d_mm, fc, fy, b1, db, params.clear_cover_beam_mm, ds
        )
        as_te, bars_te, dcr_te, lay_te = _design_beam_flexure_zone(
            env.mu_neg_end_knm, b_mm, d_mm, fc, fy, b1, db, params.clear_cover_beam_mm, ds
        )
        flex_dcr = max(dcr_ts, dcr_bm, dcr_te)
        if lay_ts or lay_bm or lay_te:
            notes.append("2 bar layers needed for clear spacing")
        if flex_dcr > 1.0:
            notes.append("Flexural demand exceeds tension-controlled section capacity; increase depth/width")

        # Shear design
        vc_kn = 0.17 * math.sqrt(fc) * b_mm * d_mm / 1000.0
        phi_vc = 0.75 * vc_kn
        vs_max_kn = 0.66 * math.sqrt(fc) * b_mm * d_mm / 1000.0
        phi_vn_max = 0.75 * (vc_kn + vs_max_kn)
        vs_req_kn = max(0.0, (env.vu_max_kn / 0.75) - vc_kn)

        s_smrf = min(d_mm / 4.0, 6.0 * db, 150.0)
        s_str = (av * fyt * d_mm) / (vs_req_kn * 1000.0) if vs_req_kn > 1e-4 else s_smrf
        s_sup = max(50, int(math.floor(min(s_smrf, s_str) / 25.0) * 25))
        s_mid = max(75, int(math.floor(min(d_mm / 2.0, 200.0) / 25.0) * 25))

        phi_vs_prov = 0.75 * (av * fyt * d_mm / s_sup) / 1000.0
        phi_vn_prov = min(phi_vn_max, phi_vc + phi_vs_prov)
        shear_dcr = max(
            env.vu_max_kn / max(phi_vn_max, 1e-6),
            env.vu_max_kn / max(phi_vn_prov, 1e-6),
        )
        if vs_req_kn > vs_max_kn:
            notes.append("Shear exceeds ACI/NSCP 0.66*sqrt(fc)*b*d concrete crushing limit")
        elif s_str < 50.0:
            notes.append("Required stirrup spacing < 50mm; increase section or stirrup diameter")

        gov_dcr = round(max(flex_dcr, shear_dcr), 3)
        out.append(
            BeamDesignResult(
                member_id=m.member_id,
                section_id=sec.section_id,
                floor_level=m.floor_level,
                width_mm=b_mm,
                depth_mm=h_mm,
                length_m=round(m.length_m, 3),
                mu_neg_start_knm=round(env.mu_neg_start_knm, 2),
                mu_pos_mid_knm=round(env.mu_pos_mid_knm, 2),
                mu_neg_end_knm=round(env.mu_neg_end_knm, 2),
                vu_max_kn=round(env.vu_max_kn, 2),
                governing_combo=env.governing_combo or "LRFD",
                as_req_top_start_mm2=as_ts,
                as_req_bot_mid_mm2=as_bm,
                as_req_top_end_mm2=as_te,
                bars_top_start=bars_ts,
                bars_bot_mid=bars_bm,
                bars_top_end=bars_te,
                stirrups_support=f"{ds}mm @ {s_sup}mm o.c.",
                stirrups_midspan=f"{ds}mm @ {s_mid}mm o.c.",
                flexure_dcr=round(flex_dcr, 3),
                shear_dcr=round(shear_dcr, 3),
                governing_dcr=gov_dcr,
                status="PASS" if gov_dcr <= 1.0 else "FAIL",
                notes=notes,
            )
        )
    return out


def _uniaxial_column_moment_capacity(
    pu_kn: float,
    b_mm: float,
    h_mm: float,
    ast_mm2: float,
    fc: float,
    fy: float,
    beta1: float,
    cover_mm: float,
    tie_dia_mm: int,
    bar_dia_mm: int,
) -> tuple[float, float, float]:
    """
    Compute (phi_pn_max_kn, phi_pt_kn, phi_mn_at_pu_knm) along a principal axis
    using the 4-point tied-column P-M interaction envelope.
    """
    ag = b_mm * h_mm
    d_eff = max(50.0, h_mm - cover_mm - tie_dia_mm - 0.5 * bar_dia_mm)
    d_prime = h_mm - d_eff

    p0_kn = (0.85 * fc * (ag - ast_mm2) + fy * ast_mm2) / 1000.0
    phi_pn_max = 0.80 * 0.65 * p0_kn
    phi_pt = -0.90 * ast_mm2 * fy / 1000.0

    # Balanced point
    ey = fy / 200000.0
    cb = (0.003 / (0.003 + ey)) * d_eff
    ab = min(h_mm, beta1 * cb)
    cc_kn = 0.85 * fc * b_mm * ab / 1000.0
    as_face = 0.5 * ast_mm2
    eps_s_prime = 0.003 * max(0.0, cb - d_prime) / max(cb, 1.0)
    fs_prime = min(fy, 200000.0 * eps_s_prime)
    cs_kn = as_face * (fs_prime - 0.85 * fc) / 1000.0
    ts_kn = as_face * fy / 1000.0
    pn_b = cc_kn + cs_kn - ts_kn
    mn_b_knm = (
        cc_kn * (0.5 * h_mm - 0.5 * ab)
        + cs_kn * (0.5 * h_mm - d_prime)
        + ts_kn * (d_eff - 0.5 * h_mm)
    ) / 1000.0
    phi_pb = 0.65 * pn_b
    phi_mb = max(0.65 * mn_b_knm, 1.0)

    # Pure bending (Pu = 0, phi = 0.90)
    a0 = (as_face * fy) / (0.85 * fc * b_mm)
    mn_0_knm = as_face * fy * max(d_eff - 0.5 * a0, 0.5 * (d_eff - d_prime)) / 1e6
    phi_m0 = max(0.90 * mn_0_knm, 1.0)

    # Piecewise linear interpolation along (P, M) envelope
    if pu_kn >= phi_pn_max:
        phi_m = 0.05 * phi_mb
    elif pu_kn >= phi_pb:
        t = (phi_pn_max - pu_kn) / max(phi_pn_max - phi_pb, 1.0)
        phi_m = (0.10 * phi_mb) + t * (0.90 * phi_mb)
    elif pu_kn >= 0.0:
        t = pu_kn / max(phi_pb, 1.0)
        phi_m = phi_m0 + t * (phi_mb - phi_m0)
    elif pu_kn >= phi_pt:
        t = (pu_kn - phi_pt) / max(0.0 - phi_pt, 1.0)
        phi_m = t * phi_m0
    else:
        phi_m = 0.01

    return phi_pn_max, phi_pt, max(phi_m, 0.5)


def _column_pm_dcr(
    env: ColumnForceEnvelope,
    b_mm: float,
    h_mm: float,
    ast_mm2: float,
    fc: float,
    fy: float,
    beta1: float,
    params: DesignParameters,
) -> tuple[float, str]:
    """Evaluate worst biaxial P-M interaction DCR across all LRFD combo demands."""
    demands = env.demands
    if not demands:
        return 0.0, env.governing_combo or "LRFD"

    worst_dcr = 0.0
    worst_combo = env.governing_combo or demands[0].combo_name
    for dem in demands:
        phi_pn_max, phi_pt, phi_mnx = _uniaxial_column_moment_capacity(
            dem.pu_kn,
            b_mm,
            h_mm,
            ast_mm2,
            fc,
            fy,
            beta1,
            params.clear_cover_column_mm,
            params.stirrup_tie_dia_mm,
            params.main_bar_dia_col_mm,
        )
        _, _, phi_mny = _uniaxial_column_moment_capacity(
            dem.pu_kn,
            h_mm,
            b_mm,
            ast_mm2,
            fc,
            fy,
            beta1,
            params.clear_cover_column_mm,
            params.stirrup_tie_dia_mm,
            params.main_bar_dia_col_mm,
        )
        if dem.pu_kn > phi_pn_max:
            dcr = dem.pu_kn / max(phi_pn_max, 1e-6)
        elif dem.pu_kn < phi_pt:
            dcr = abs(dem.pu_kn / min(phi_pt, -1e-6))
        else:
            rx = abs(dem.mux_knm) / max(phi_mnx, 1e-6)
            ry = abs(dem.muy_knm) / max(phi_mny, 1e-6)
            biaxial = ((rx ** 1.5) + (ry ** 1.5)) ** (1.0 / 1.5)
            axial = max(0.0, dem.pu_kn / max(phi_pn_max, 1e-6))
            dcr = max(biaxial, axial)

        if dcr > worst_dcr:
            worst_dcr = dcr
            worst_combo = dem.combo_name

    return round(worst_dcr, 3), worst_combo


def design_columns(
    project: ProjectState,
    results: AnalysisResults,
    params: DesignParameters,
) -> list[ColumnDesignResult]:
    """Design all column members for biaxial P-M interaction and SMRF shear confinement."""
    model = project.structural_model
    sizing = project.member_sizing
    mat = sizing.material
    fc = mat.fc_mpa
    fy = mat.fy_main_mpa
    fyt = mat.fy_tie_mpa
    b1 = _beta1(fc)

    sec_by_id = {s.section_id: s for s in sizing.sections}
    asgn_by_mid = {a.member_id: a for a in sizing.member_assignments}
    db = params.main_bar_dia_col_mm
    ds = params.stirrup_tie_dia_mm
    ab = _bar_area_mm2(db)
    av = 2.0 * _bar_area_mm2(ds)

    out: list[ColumnDesignResult] = []
    for m in sorted(model.members, key=lambda x: x.member_id):
        if m.member_type != "column":
            continue
        asgn = asgn_by_mid.get(m.member_id)
        sec = sec_by_id.get(asgn.section_id) if asgn else None
        if sec is None:
            continue
        env = results.column_envelopes.get(m.member_id, ColumnForceEnvelope(member_id=m.member_id))

        b_mm = sec.width_mm
        h_mm = sec.depth_mm
        ag = b_mm * h_mm
        notes: list[str] = []

        # Auto-select symmetric reinforcement count (4, 8, 12, 16) with 0.01 <= rho <= 0.04
        candidates = [4, 8, 12, 16]
        valid_cands = [n for n in candidates if 0.01 <= (n * ab) / ag <= 0.04]
        if not valid_cands:
            # Find smallest even multiple of 4 reaching >= 1% (capped at 20 bars)
            min_n = max(4, int(math.ceil((0.01 * ag) / ab / 4.0) * 4))
            valid_cands = [min(min_n, 20)]

        chosen_n = valid_cands[-1]
        chosen_dcr = 999.0
        chosen_combo = env.governing_combo or "LRFD"

        for n_bars in valid_cands:
            dcr, combo = _column_pm_dcr(env, b_mm, h_mm, n_bars * ab, fc, fy, b1, params)
            chosen_n = n_bars
            chosen_dcr = dcr
            chosen_combo = combo
            if dcr <= 1.0:
                break

        rho_prov = round((chosen_n * ab) / ag, 4)
        if rho_prov > 0.06:
            notes.append(f"Reinforcement ratio rho={rho_prov*100:.2f}% exceeds NSCP 6% maximum")
        elif rho_prov > 0.04:
            notes.append(f"Reinforcement ratio rho={rho_prov*100:.2f}% exceeds 4% practical lap-splice limit")
        if chosen_dcr > 1.0:
            notes.append("Biaxial P-M demand exceeds capacity at max reinforcement; increase column section")

        # Shear & SMRF tie confinement
        d_mm = max(50.0, max(b_mm, h_mm) - params.clear_cover_column_mm - ds - 0.5 * db)
        nu_n = max(0.0, env.pu_min_kn * 1000.0)
        vc_kn = 0.17 * (1.0 + nu_n / (14.0 * ag)) * math.sqrt(fc) * min(b_mm, h_mm) * d_mm / 1000.0
        s_conf = max(50, int(math.floor(min(min(b_mm, h_mm) / 4.0, 6.0 * db, 100.0) / 25.0) * 25))
        s_mid = max(75, int(math.floor(min(6.0 * db, 150.0) / 25.0) * 25))
        vs_prov_kn = (av * fyt * d_mm / s_conf) / 1000.0
        vs_max_kn = 0.66 * math.sqrt(fc) * min(b_mm, h_mm) * d_mm / 1000.0
        phi_vn = 0.75 * (vc_kn + min(vs_prov_kn, vs_max_kn))
        shear_dcr = round(env.vu_max_kn / max(phi_vn, 1e-6), 3)
        if shear_dcr > 1.0:
            notes.append("Column shear demand exceeds confined tie shear capacity")

        gov_dcr = round(max(chosen_dcr, shear_dcr), 3)
        status = "PASS" if (gov_dcr <= 1.0 and rho_prov <= 0.06) else "FAIL"

        out.append(
            ColumnDesignResult(
                member_id=m.member_id,
                section_id=sec.section_id,
                floor_level=m.floor_level,
                width_mm=b_mm,
                depth_mm=h_mm,
                length_m=round(m.length_m, 3),
                pu_max_kn=round(env.pu_max_kn, 2),
                pu_min_kn=round(env.pu_min_kn, 2),
                mux_max_knm=round(env.mux_max_knm, 2),
                muy_max_knm=round(env.muy_max_knm, 2),
                vu_max_kn=round(env.vu_max_kn, 2),
                governing_combo=chosen_combo,
                rho_provided=rho_prov,
                main_bars=f"{chosen_n}-{db}mm",
                ties_confined=f"{ds}mm @ {s_conf}mm o.c.",
                ties_mid=f"{ds}mm @ {s_mid}mm o.c.",
                pm_dcr=chosen_dcr,
                shear_dcr=shear_dcr,
                governing_dcr=gov_dcr,
                status=status,
                notes=notes,
            )
        )
    return out


def design_footings(
    project: ProjectState,
    support_reactions: list[SupportReactionEnvelope],
    params: DesignParameters,
) -> list[FootingDesignResult]:
    """Size isolated square spread footings for bearing, one-way shear, punching shear & flexure."""
    model = project.structural_model
    sizing = project.member_sizing
    mat = sizing.material
    fc = mat.fc_mpa
    fy = mat.fy_main_mpa
    gamma_c = mat.density_kn_m3
    qa = params.soil_bearing_qa_kpa
    df = params.footing_embedment_m
    gamma_s = params.soil_unit_weight_kn_m3
    db = params.main_bar_dia_beam_mm
    ab = _bar_area_mm2(db)

    node_by_id = {n.node_id: n for n in model.nodes}
    sec_by_id = {s.section_id: s for s in sizing.sections}
    asgn_by_mid = {a.member_id: a for a in sizing.member_assignments}

    # Map support node_id -> column dimensions (cx_m, cz_m)
    col_dims_by_node: dict[int, tuple[float, float]] = {}
    for m in model.members:
        if m.member_type != "column":
            continue
        asgn = asgn_by_mid.get(m.member_id)
        sec = sec_by_id.get(asgn.section_id) if asgn else None
        if sec is None:
            continue
        dims = (sec.width_mm / 1000.0, sec.depth_mm / 1000.0)
        col_dims_by_node.setdefault(m.start_node, dims)
        col_dims_by_node.setdefault(m.end_node, dims)

    raw_footings: list[dict] = []
    for rxn in sorted(support_reactions, key=lambda r: r.node_id):
        node = node_by_id.get(rxn.node_id)
        base_elev = node.y_m if node else 0.0
        cx_m, cz_m = col_dims_by_node.get(rxn.node_id, (0.40, 0.40))
        c_min = min(cx_m, cz_m)

        p_serv = max(rxn.max_asd_fy_kn, 1.0)
        pu = max(rxn.max_lrfd_pu_kn, 1.4 * p_serv)

        chosen = None
        for t_mm in range(300, 950, 50):
            t_m = t_mm / 1000.0
            overburden = df * gamma_s + t_m * max(gamma_c - gamma_s, 0.0)
            q_net = qa - overburden
            if q_net <= 5.0:
                b_m = max(1.0, math.ceil(math.sqrt(p_serv / max(qa, 1.0)) * 10.0) / 10.0)
            else:
                b_m = max(1.0, math.ceil(math.sqrt(p_serv / q_net) * 10.0) / 10.0)
            l_m = b_m
            q_actual = p_serv / (b_m * l_m) + overburden
            bearing_dcr = q_actual / max(qa, 1e-6)

            d_mm = max(100.0, t_mm - params.clear_cover_footing_mm - db)
            d_m = d_mm / 1000.0
            qu_kpa = pu / (b_m * l_m)

            # One-way beam shear at distance d from column face
            cant_1w = max(0.0, 0.5 * (b_m - c_min) - d_m)
            vu_1w = qu_kpa * b_m * cant_1w
            phi_vc_1w = 0.75 * 0.17 * math.sqrt(fc) * (b_m * 1000.0) * d_mm / 1000.0
            dcr_1w = vu_1w / max(phi_vc_1w, 1e-6)

            # Two-way punching shear at d/2 perimeter around column
            bx = cx_m + d_m
            bz = cz_m + d_m
            bo_mm = 2.0 * (bx + bz) * 1000.0
            vu_2w = max(0.0, pu - qu_kpa * bx * bz)
            phi_vc_2w = 0.75 * 0.33 * math.sqrt(fc) * bo_mm * d_mm / 1000.0
            dcr_2w = vu_2w / max(phi_vc_2w, 1e-6)

            chosen = (b_m, l_m, float(t_mm), d_mm, q_actual, bearing_dcr, dcr_1w, dcr_2w, qu_kpa)
            if dcr_1w <= 1.0 and dcr_2w <= 1.0:
                break

        assert chosen is not None
        b_m, l_m, t_mm, d_mm, q_actual, bearing_dcr, dcr_1w, dcr_2w, qu_kpa = chosen

        # Flexural reinforcement at column face
        cant = max(0.0, 0.5 * (b_m - c_min))
        mu_knm = qu_kpa * b_m * (cant ** 2) * 0.5
        b_mm = b_m * 1000.0
        rn = (mu_knm * 1e6) / max(0.90 * b_mm * (d_mm ** 2), 1.0)
        disc = max(0.01, 1.0 - (2.0 * rn) / (0.85 * fc))
        rho_flex = (0.85 * fc / fy) * (1.0 - math.sqrt(disc))
        as_min = 0.0018 * b_mm * t_mm
        as_req = max(rho_flex * b_mm * d_mm, as_min)
        n_bars = max(4, int(math.ceil(as_req / ab)))
        avail_w = max(100.0, b_mm - 2.0 * params.clear_cover_footing_mm)
        spacing = max(75, int(math.floor((avail_w / max(n_bars - 1, 1)) / 25.0) * 25))
        bar_str = f"{n_bars}-{db}mm @ {spacing}mm o.c."

        gov_dcr = round(max(bearing_dcr, dcr_1w, dcr_2w), 3)
        raw_footings.append(
            {
                "key": (round(b_m, 2), round(l_m, 2), int(t_mm)),
                "node_id": rxn.node_id,
                "grid_ref": rxn.grid_ref,
                "base_elevation_m": round(base_elev, 3),
                "p_service_kn": round(p_serv, 2),
                "pu_kn": round(pu, 2),
                "b_m": round(b_m, 2),
                "l_m": round(l_m, 2),
                "thickness_mm": t_mm,
                "q_actual_kpa": round(q_actual, 2),
                "q_allow_kpa": round(qa, 2),
                "bearing_dcr": round(bearing_dcr, 3),
                "one_way_shear_dcr": round(dcr_1w, 3),
                "punching_shear_dcr": round(dcr_2w, 3),
                "governing_dcr": gov_dcr,
                "bottom_bars_x": bar_str,
                "bottom_bars_z": bar_str,
                "status": "PASS" if gov_dcr <= 1.0 else "FAIL",
            }
        )

    # Group identical (B, L, t) footings into F1, F2, ...
    unique_keys = sorted({r["key"] for r in raw_footings})
    tag_map = {k: f"F{i}" for i, k in enumerate(unique_keys, start=1)}

    return [
        FootingDesignResult(
            footing_tag=tag_map[r["key"]],
            **{k: v for k, v in r.items() if k != "key"},
        )
        for r in raw_footings
    ]


def _collect_design_issues(
    drifts: list[StoryDriftResult],
    reactions: list[SupportReactionEnvelope],
    beams: list[BeamDesignResult],
    columns: list[ColumnDesignResult],
    footings: list[FootingDesignResult],
) -> list[DesignCheckIssue]:
    issues: list[DesignCheckIssue] = []

    fail_drifts = [d for d in drifts if d.status == "FAIL"]
    if fail_drifts:
        issues.append(
            DesignCheckIssue(
                severity="error",
                code="DRIFT_LIMIT_EXCEEDED",
                message=(
                    f"{len(fail_drifts)} story level(s) exceed NSCP 2015 inelastic drift limit "
                    f"(max ratio = {max(max(d.drift_ratio_x, d.drift_ratio_z) for d in fail_drifts):.2f}). "
                    "Stiffen columns/beams in Module 04."
                ),
                entity_ids=[d.floor_level for d in fail_drifts],
            )
        )

    soft_stories = [d for d in drifts if d.soft_story_flag]
    if soft_stories:
        issues.append(
            DesignCheckIssue(
                severity="warning",
                code="SOFT_STORY_IRREGULARITY",
                message=(
                    f"Soft-story stiffness irregularity flagged at {', '.join(d.floor_level for d in soft_stories)} "
                    "(NSCP 2015 Table 208-9: Ki < 70% K_above or < 80% 3-story avg)."
                ),
                entity_ids=[d.floor_level for d in soft_stories],
            )
        )

    uplift_nodes = [r for r in reactions if r.min_asd_fy_kn < -1.0]
    if uplift_nodes:
        issues.append(
            DesignCheckIssue(
                severity="warning",
                code="SUPPORT_UPLIFT_DETECTED",
                message=(
                    f"{len(uplift_nodes)} support node(s) experience net service uplift "
                    f"(min Fy = {min(r.min_asd_fy_kn for r in uplift_nodes):.1f} kN). Check tie-down / overturning."
                ),
                entity_ids=[f"N{r.node_id}" for r in uplift_nodes],
            )
        )

    fail_beams = [b for b in beams if b.status == "FAIL"]
    if fail_beams:
        issues.append(
            DesignCheckIssue(
                severity="error",
                code="BEAM_DESIGN_FAIL",
                message=(
                    f"{len(fail_beams)} beam(s) failed NSCP 2015 flexure/shear checks "
                    f"(max DCR = {max(b.governing_dcr for b in fail_beams):.2f}). Upsize section in Module 04."
                ),
                entity_ids=[f"M{b.member_id}" for b in fail_beams[:12]],
            )
        )

    fail_cols = [c for c in columns if c.status == "FAIL"]
    if fail_cols:
        issues.append(
            DesignCheckIssue(
                severity="error",
                code="COLUMN_DESIGN_FAIL",
                message=(
                    f"{len(fail_cols)} column(s) failed biaxial P-M or shear checks "
                    f"(max DCR = {max(c.governing_dcr for c in fail_cols):.2f}). Upsize column section in Module 04."
                ),
                entity_ids=[f"M{c.member_id}" for c in fail_cols[:12]],
            )
        )

    fail_ftgs = [f for f in footings if f.status == "FAIL"]
    if fail_ftgs:
        issues.append(
            DesignCheckIssue(
                severity="error",
                code="FOOTING_DESIGN_FAIL",
                message=(
                    f"{len(fail_ftgs)} footing(s) failed bearing or punching/one-way shear checks "
                    f"(max DCR = {max(f.governing_dcr for f in fail_ftgs):.2f})."
                ),
                entity_ids=[f"N{f.node_id}" for f in fail_ftgs[:12]],
            )
        )

    return issues


def _build_state_from_results(
    project: ProjectState,
    results: AnalysisResults,
    params: DesignParameters,
    source: str,
    source_filename: str = "",
) -> AnalysisDesignState:
    drifts = compute_story_drifts(project, results)
    beams = design_beams(project, results, params)
    columns = design_columns(project, results, params)
    footings = design_footings(project, results.support_reactions, params)
    issues = _collect_design_issues(drifts, results.support_reactions, beams, columns, footings)

    return AnalysisDesignState(
        generated_at=_utc_now(),
        source=source,
        source_filename=source_filename,
        params=params,
        story_drifts=drifts,
        support_reactions=results.support_reactions,
        beam_designs=beams,
        column_designs=columns,
        footing_designs=footings,
        validation_issues=issues,
        engineer_approved=False,
    )


def run_analysis_and_design(
    project: ProjectState,
    params: DesignParameters | None = None,
) -> tuple[AnalysisDesignState, AnalysisResults]:
    """Run internal 3D DSM solver + NSCP 2015 drift & RC beam/column/footing design."""
    active_params = (
        params.model_copy(deep=True)
        if params is not None
        else project.analysis_design.params.model_copy(deep=True)
    )
    results = solve_frame_dsm(project)
    state = _build_state_from_results(project, results, active_params, source="internal_dsm")
    return state, results


def run_design_from_anl(
    project: ProjectState,
    anl_text: str,
    filename: str = "uploaded.anl",
    params: DesignParameters | None = None,
) -> tuple[AnalysisDesignState, AnalysisResults]:
    """Parse uploaded STAAD.Pro .anl results and run NSCP 2015 drift & RC design."""
    if not project.structural_model.nodes or not project.member_sizing.sections:
        raise ValueError("Phase 3 geometry and Phase 4 member sizing must exist before importing a STAAD .anl file.")
    active_params = (
        params.model_copy(deep=True)
        if params is not None
        else project.analysis_design.params.model_copy(deep=True)
    )
    results = parse_staad_anl(anl_text, project)
    state = _build_state_from_results(
        project,
        results,
        active_params,
        source="staad_anl",
        source_filename=filename,
    )
    return state, results


def export_schedules_csv(project: ProjectState) -> str:
    """Export Beam, Column, and Footing reinforcement schedules as a clean CSV string."""
    ad = project.analysis_design
    buf = io.StringIO()
    w = csv.writer(buf)

    w.writerow(["=== BEAM REINFORCEMENT SCHEDULE ==="])
    w.writerow(
        [
            "Member ID",
            "Section",
            "Floor",
            "Width (mm)",
            "Depth (mm)",
            "Length (m)",
            "Mu- Start (kNm)",
            "Mu+ Mid (kNm)",
            "Mu- End (kNm)",
            "Vu Max (kN)",
            "Top Start Bars",
            "Bot Mid Bars",
            "Top End Bars",
            "Stirrups (Support)",
            "Stirrups (Midspan)",
            "Flexure DCR",
            "Shear DCR",
            "Governing DCR",
            "Status",
        ]
    )
    for b in ad.beam_designs:
        w.writerow(
            [
                b.member_id,
                b.section_id,
                b.floor_level,
                b.width_mm,
                b.depth_mm,
                b.length_m,
                b.mu_neg_start_knm,
                b.mu_pos_mid_knm,
                b.mu_neg_end_knm,
                b.vu_max_kn,
                b.bars_top_start,
                b.bars_bot_mid,
                b.bars_top_end,
                b.stirrups_support,
                b.stirrups_midspan,
                b.flexure_dcr,
                b.shear_dcr,
                b.governing_dcr,
                b.status,
            ]
        )

    w.writerow([])
    w.writerow(["=== COLUMN REINFORCEMENT SCHEDULE ==="])
    w.writerow(
        [
            "Member ID",
            "Section",
            "Floor",
            "Width (mm)",
            "Depth (mm)",
            "Height (m)",
            "Pu Max (kN)",
            "Pu Min (kN)",
            "Mux Max (kNm)",
            "Muy Max (kNm)",
            "Vu Max (kN)",
            "Main Bars",
            "Rho (%)",
            "Ties (Confined)",
            "Ties (Mid)",
            "P-M DCR",
            "Shear DCR",
            "Governing DCR",
            "Status",
        ]
    )
    for c in ad.column_designs:
        w.writerow(
            [
                c.member_id,
                c.section_id,
                c.floor_level,
                c.width_mm,
                c.depth_mm,
                c.length_m,
                c.pu_max_kn,
                c.pu_min_kn,
                c.mux_max_knm,
                c.muy_max_knm,
                c.vu_max_kn,
                c.main_bars,
                round(c.rho_provided * 100.0, 2),
                c.ties_confined,
                c.ties_mid,
                c.pm_dcr,
                c.shear_dcr,
                c.governing_dcr,
                c.status,
            ]
        )

    w.writerow([])
    w.writerow(["=== ISOLATED FOOTING SCHEDULE ==="])
    w.writerow(
        [
            "Tag",
            "Node ID",
            "Grid Ref",
            "Base Elev (m)",
            "P Service (kN)",
            "Pu (kN)",
            "B (m)",
            "L (m)",
            "Thickness (mm)",
            "q Actual (kPa)",
            "q Allow (kPa)",
            "Bearing DCR",
            "1-Way Shear DCR",
            "Punching DCR",
            "Governing DCR",
            "Bottom Bars X",
            "Bottom Bars Z",
            "Status",
        ]
    )
    for f in ad.footing_designs:
        w.writerow(
            [
                f.footing_tag,
                f.node_id,
                f.grid_ref,
                f.base_elevation_m,
                f.p_service_kn,
                f.pu_kn,
                f.b_m,
                f.l_m,
                f.thickness_mm,
                f.q_actual_kpa,
                f.q_allow_kpa,
                f.bearing_dcr,
                f.one_way_shear_dcr,
                f.punching_shear_dcr,
                f.governing_dcr,
                f.bottom_bars_x,
                f.bottom_bars_z,
                f.status,
            ]
        )

    return buf.getvalue()
