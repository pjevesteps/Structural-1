from __future__ import annotations

import math
import re

from ..models import (
    AnalysisDesignState,
    BeamDesignResult,
    ColumnDesignResult,
    DesignParameters,
    ProjectState,
    SmrfJointCheckResult,
)

_BAR_RE = re.compile(r"(\d+)\s*-\s*(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)


def _bar_area_mm2(dia_mm: float) -> float:
    return math.pi * (dia_mm ** 2) / 4.0


def _beta1(fc_mpa: float) -> float:
    return max(0.65, min(0.85, 0.85 - 0.05 * max(0.0, fc_mpa - 28.0) / 7.0))


def parse_bar_callout_area_mm2(callout: str, fallback_dia_mm: float = 16.0) -> tuple[float, float]:
    """Parse a rebar callout like '4-16mm' or '8-20mm' into (As_mm2, dia_mm)."""
    m = _BAR_RE.search(callout or "")
    if not m:
        return 2.0 * _bar_area_mm2(fallback_dia_mm), fallback_dia_mm
    count = max(1, int(m.group(1)))
    dia = max(8.0, float(m.group(2)))
    return count * _bar_area_mm2(dia), dia


def nominal_beam_moment_knm(
    as_mm2: float,
    b_mm: float,
    h_mm: float,
    fc_mpa: float,
    fy_mpa: float,
    clear_cover_mm: float,
    stirrup_dia_mm: float,
    bar_dia_mm: float,
) -> float:
    """Compute nominal beam flexural capacity Mn = As * fy * (d - a/2) in kN-m."""
    if as_mm2 <= 1e-6 or b_mm <= 1e-6:
        return 0.0
    d_mm = max(50.0, h_mm - clear_cover_mm - stirrup_dia_mm - 0.5 * bar_dia_mm)
    a_mm = (as_mm2 * fy_mpa) / max(0.85 * fc_mpa * b_mm, 1.0)
    lever_mm = max(0.5 * d_mm, d_mm - 0.5 * a_mm)
    return (as_mm2 * fy_mpa * lever_mm) / 1e6


def nominal_column_moment_knm(
    pu_kn: float,
    b_mm: float,
    h_mm: float,
    ast_mm2: float,
    fc_mpa: float,
    fy_mpa: float,
    cover_mm: float,
    tie_dia_mm: float,
    bar_dia_mm: float,
) -> float:
    """
    Compute nominal uniaxial column flexural capacity Mn (kN-m, WITHOUT phi reduction)
    at factored axial load Pu (kN) using the 4-point tied-column P-M interaction envelope.
    Bending is about the axis perpendicular to `h_mm` (depth in bending plane = h_mm).
    """
    ag = b_mm * h_mm
    b1 = _beta1(fc_mpa)
    d_eff = max(50.0, h_mm - cover_mm - tie_dia_mm - 0.5 * bar_dia_mm)
    d_prime = h_mm - d_eff

    p0_kn = (0.85 * fc_mpa * (ag - ast_mm2) + fy_mpa * ast_mm2) / 1000.0
    pn_max = 0.80 * p0_kn
    pt_kn = -ast_mm2 * fy_mpa / 1000.0

    # Balanced point (nominal, unfactored)
    ey = fy_mpa / 200000.0
    cb = (0.003 / (0.003 + ey)) * d_eff
    ab = min(h_mm, b1 * cb)
    cc_kn = 0.85 * fc_mpa * b_mm * ab / 1000.0
    as_face = 0.5 * ast_mm2
    eps_s_prime = 0.003 * max(0.0, cb - d_prime) / max(cb, 1.0)
    fs_prime = min(fy_mpa, 200000.0 * eps_s_prime)
    cs_kn = as_face * (fs_prime - 0.85 * fc_mpa) / 1000.0
    ts_kn = as_face * fy_mpa / 1000.0
    pn_b = cc_kn + cs_kn - ts_kn
    mn_b_knm = max(
        (
            cc_kn * (0.5 * h_mm - 0.5 * ab)
            + cs_kn * (0.5 * h_mm - d_prime)
            + ts_kn * (d_eff - 0.5 * h_mm)
        )
        / 1000.0,
        1.0,
    )

    # Pure bending (Pu = 0)
    a0 = (as_face * fy_mpa) / max(0.85 * fc_mpa * b_mm, 1.0)
    mn_0_knm = max(
        as_face * fy_mpa * max(d_eff - 0.5 * a0, 0.5 * (d_eff - d_prime)) / 1e6,
        1.0,
    )

    # Piecewise linear interpolation along nominal (Pn, Mn) envelope
    if pu_kn >= pn_max:
        mn = 0.05 * mn_b_knm
    elif pu_kn >= pn_b:
        t = (pn_max - pu_kn) / max(pn_max - pn_b, 1.0)
        mn = (0.10 * mn_b_knm) + t * (0.90 * mn_b_knm)
    elif pu_kn >= 0.0:
        t = pu_kn / max(pn_b, 1.0)
        mn = mn_0_knm + t * (mn_b_knm - mn_0_knm)
    elif pu_kn >= pt_kn:
        t = (pu_kn - pt_kn) / max(0.0 - pt_kn, 1.0)
        mn = t * mn_0_knm
    else:
        mn = 0.01

    return max(mn, 0.5)


def evaluate_smrf_joints(
    project: ProjectState,
    beams: list[BeamDesignResult],
    columns: list[ColumnDesignResult],
    params: DesignParameters,
) -> list[SmrfJointCheckResult]:
    """
    Evaluate NSCP 2015 Section 418.7.3 (Strong-Column / Weak-Beam: Sum(Mnc) >= 1.2 * Sum(Mnb))
    and Section 418.8.4 (Beam-Column Joint Horizontal Shear Vu <= phi * Vn) at every
    non-support elevated beam-column intersection in both orthogonal X and Z directions.
    """
    model = project.structural_model
    sizing = project.member_sizing
    mat = sizing.material
    fc = mat.fc_mpa
    fy = mat.fy_main_mpa

    node_by_id = {n.node_id: n for n in model.nodes}
    beam_by_mid = {b.member_id: b for b in beams}
    col_by_mid = {c.member_id: c for c in columns}
    asgn_by_mid = {a.member_id: a for a in sizing.member_assignments}

    # Build 3D joint connectivity maps
    cols_at_node: dict[int, list] = {}
    beams_at_node: dict[int, list] = {}
    for m in model.members:
        if m.member_type == "column":
            cols_at_node.setdefault(m.start_node, []).append(m)
            cols_at_node.setdefault(m.end_node, []).append(m)
        else:
            beams_at_node.setdefault(m.start_node, []).append(m)
            beams_at_node.setdefault(m.end_node, []).append(m)

    results: list[SmrfJointCheckResult] = []
    for n in sorted(model.nodes, key=lambda x: (x.y_m, x.node_id)):
        if n.is_support or n.floor_level.startswith("FTG_"):
            continue
        inc_cols = [m for m in cols_at_node.get(n.node_id, []) if m.member_id in col_by_mid]
        inc_beams = [m for m in beams_at_node.get(n.node_id, []) if m.member_id in beam_by_mid]
        if not inc_cols or not inc_beams:
            continue

        # Classify incident beams by direction (+X, -X, +Z, -Z)
        # Entry tuple: (member, beam_design, as_top, mn_neg, as_bot, mn_pos)
        dir_beams: dict[str, list[tuple]] = {"+X": [], "-X": [], "+Z": [], "-Z": []}
        for bm in inc_beams:
            other_nid = bm.end_node if bm.start_node == n.node_id else bm.start_node
            other = node_by_id.get(other_nid)
            if other is None:
                continue
            dx = other.x_m - n.x_m
            dz = other.z_m - n.z_m
            b_des = beam_by_mid[bm.member_id]
            top_callout = (
                b_des.bars_top_start if bm.start_node == n.node_id else b_des.bars_top_end
            )
            as_top, dia_top = parse_bar_callout_area_mm2(top_callout, params.main_bar_dia_beam_mm)
            as_bot_mid, dia_bot = parse_bar_callout_area_mm2(
                b_des.bars_bot_mid, params.main_bar_dia_beam_mm
            )
            # NSCP Sec. 418.6.3.2: positive moment reinforcement at joint face >= 50% of negative
            as_bot = max(as_bot_mid, 0.5 * as_top)

            mn_neg = nominal_beam_moment_knm(
                as_top,
                b_des.width_mm,
                b_des.depth_mm,
                fc,
                fy,
                params.clear_cover_beam_mm,
                params.stirrup_tie_dia_mm,
                dia_top,
            )
            mn_pos = nominal_beam_moment_knm(
                as_bot,
                b_des.width_mm,
                b_des.depth_mm,
                fc,
                fy,
                params.clear_cover_beam_mm,
                params.stirrup_tie_dia_mm,
                dia_bot,
            )
            entry = (bm, b_des, as_top, mn_neg, as_bot, mn_pos)
            if abs(dx) >= abs(dz):
                dir_beams["+X" if dx >= 0.0 else "-X"].append(entry)
            else:
                dir_beams["+Z" if dz >= 0.0 else "-Z"].append(entry)

        # Governing column dimensions at joint (use column below if present, else first column)
        col_below = [
            m
            for m in inc_cols
            if node_by_id.get(m.start_node, n).y_m < n.y_m - 1e-3
            or node_by_id.get(m.end_node, n).y_m < n.y_m - 1e-3
        ]
        ref_col_m = col_below[0] if col_below else inc_cols[0]
        ref_col_des = col_by_mid[ref_col_m.member_id]
        ref_beta = asgn_by_mid[ref_col_m.member_id].beta_angle_deg if ref_col_m.member_id in asgn_by_mid else 0.0
        col_dim_x = ref_col_des.depth_mm if abs((ref_beta % 180.0) - 90.0) < 1.0 else ref_col_des.width_mm
        col_dim_z = ref_col_des.width_mm if abs((ref_beta % 180.0) - 90.0) < 1.0 else ref_col_des.depth_mm

        # Confinement classification per NSCP Sec. 418.8.4.1 (ACI 318 Table 18.8.4.1)
        def _face_confined(face_key: str, col_face_width_mm: float) -> bool:
            if not dir_beams[face_key]:
                return False
            max_bw = max(item[1].width_mm for item in dir_beams[face_key])
            return max_bw >= 0.75 * col_face_width_mm

        conf_px = _face_confined("+X", col_dim_z)
        conf_nx = _face_confined("-X", col_dim_z)
        conf_pz = _face_confined("+Z", col_dim_x)
        conf_nz = _face_confined("-Z", col_dim_x)
        n_conf = sum([conf_px, conf_nx, conf_pz, conf_nz])
        if n_conf == 4:
            gamma_joint = 1.7
        elif n_conf == 3 or (conf_px and conf_nx) or (conf_pz and conf_nz):
            gamma_joint = 1.25
        else:
            gamma_joint = 1.0

        avg_story_h_m = sum(max(col_by_mid[cm.member_id].length_m, 2.0) for cm in inc_cols) / len(inc_cols)

        # Evaluate orthogonal X and Z planes and track governing SCWB and Joint Shear
        dir_evals: list[tuple[float, float, float, float, float, float]] = []
        for axis, pos_key, neg_key in (("X", "+X", "-X"), ("Z", "+Z", "-Z")):
            pos_list = dir_beams[pos_key]
            neg_list = dir_beams[neg_key]
            if not pos_list and not neg_list:
                continue

            # Sum nominal column capacities Mnc in this bending plane
            sum_mnc = 0.0
            for cm in inc_cols:
                c_des = col_by_mid[cm.member_id]
                ast_mm2, c_dia = parse_bar_callout_area_mm2(c_des.main_bars, params.main_bar_dia_col_mm)
                beta = asgn_by_mid[cm.member_id].beta_angle_deg if cm.member_id in asgn_by_mid else 0.0
                rotated = abs((beta % 180.0) - 90.0) < 1.0
                if axis == "X":
                    h_plane = c_des.depth_mm if rotated else c_des.width_mm
                    b_plane = c_des.width_mm if rotated else c_des.depth_mm
                else:
                    h_plane = c_des.width_mm if rotated else c_des.depth_mm
                    b_plane = c_des.depth_mm if rotated else c_des.width_mm

                # Governing axial load giving minimum Mnc across factored Pu range (NSCP 418.7.3.2)
                pu_samples = [
                    c_des.pu_min_kn,
                    0.5 * (c_des.pu_min_kn + c_des.pu_max_kn),
                    c_des.pu_max_kn,
                ]
                mnc_col = min(
                    nominal_column_moment_knm(
                        pu_val,
                        b_plane,
                        h_plane,
                        ast_mm2,
                        fc,
                        fy,
                        params.clear_cover_column_mm,
                        params.stirrup_tie_dia_mm,
                        c_dia,
                    )
                    for pu_val in pu_samples
                )
                sum_mnc += mnc_col

            # Sum nominal beam capacities Mnb for both sway directions (+ and -)
            pos_neg_sum = sum(it[3] for it in pos_list)
            pos_pos_sum = sum(it[5] for it in pos_list)
            neg_neg_sum = sum(it[3] for it in neg_list)
            neg_pos_sum = sum(it[5] for it in neg_list)

            sway_1_mnb = pos_neg_sum + neg_pos_sum
            sway_2_mnb = pos_pos_sum + neg_neg_sum
            if sway_1_mnb >= sway_2_mnb:
                sum_mnb = sway_1_mnb
                t1_as = sum(it[2] for it in pos_list)
                t2_as = sum(it[4] for it in neg_list)
            else:
                sum_mnb = sway_2_mnb
                t1_as = sum(it[2] for it in neg_list)
                t2_as = sum(it[4] for it in pos_list)

            scwb_ratio = sum_mnc / max(sum_mnb, 1e-6)

            # Horizontal joint shear Vu (NSCP Sec. 418.8.2 & 418.8.4)
            t_force_kn = 1.25 * (t1_as + t2_as) * fy / 1000.0
            v_col_kn = (1.25 * sum_mnb) / max(avg_story_h_m, 1.5)
            vu_kn = max(0.0, t_force_kn - v_col_kn)

            h_c = col_dim_x if axis == "X" else col_dim_z
            b_c = col_dim_z if axis == "X" else col_dim_x
            max_beam_w = max((it[1].width_mm for it in pos_list + neg_list), default=b_c)
            b_j = min(b_c, max_beam_w + h_c)
            a_j_mm2 = max(1.0, b_j * h_c)
            phi_vn_kn = 0.85 * gamma_joint * math.sqrt(fc) * a_j_mm2 / 1000.0
            js_dcr = vu_kn / max(phi_vn_kn, 1e-6)

            dir_evals.append((scwb_ratio, sum_mnc, sum_mnb, js_dcr, vu_kn, phi_vn_kn))

        if not dir_evals:
            continue

        # Governing SCWB = lowest scwb_ratio across X/Z; governing joint shear = highest js_dcr
        gov_scwb = min(dir_evals, key=lambda x: x[0])
        gov_js = max(dir_evals, key=lambda x: x[3])

        scwb_ratio_val = round(gov_scwb[0], 3)
        js_dcr_val = round(gov_js[3], 3)

        results.append(
            SmrfJointCheckResult(
                joint_node_id=n.node_id,
                floor_level=n.floor_level,
                grid_ref=n.grid_ref or "",
                sum_mnc_knm=round(gov_scwb[1], 2),
                sum_mnb_knm=round(gov_scwb[2], 2),
                scwb_ratio=scwb_ratio_val,
                scwb_status="PASS" if scwb_ratio_val >= 1.20 else "FAIL",
                joint_shear_vu_kn=round(gov_js[4], 2),
                joint_shear_phi_vn_kn=round(gov_js[5], 2),
                joint_shear_dcr=js_dcr_val,
                joint_shear_status="PASS" if js_dcr_val <= 1.0 else "FAIL",
            )
        )

    return results


def smrf_scwb_auto_resolve_hook(
    project: ProjectState,
    ad_state: AnalysisDesignState,
) -> dict:
    """
    Modular Auto-Resolve check hook for NSCP Sec. 418 SMRF SCWB & Joint Shear.
    Flags column section IDs framing into any joint failing SCWB < 1.20 or Joint Shear DCR > 1.0.
    """
    failing = [
        j
        for j in ad_state.smrf_joint_checks
        if j.scwb_status == "FAIL" or j.joint_shear_status == "FAIL"
    ]
    if not failing:
        return {}

    fail_nids = {j.joint_node_id for j in failing}
    asgn_by_mid = {a.member_id: a for a in project.member_sizing.member_assignments}
    col_secs: set[str] = set()
    for m in project.structural_model.members:
        if m.member_type == "column" and (m.start_node in fail_nids or m.end_node in fail_nids):
            asgn = asgn_by_mid.get(m.member_id)
            if asgn:
                col_secs.add(asgn.section_id)

    scwb_cnt = sum(1 for j in failing if j.scwb_status == "FAIL")
    js_cnt = sum(1 for j in failing if j.joint_shear_status == "FAIL")
    note = (
        f"NSCP Sec. 418 SMRF: {scwb_cnt} SCWB (<1.20) & {js_cnt} joint-shear failure(s) "
        f"-> targeting columns {sorted(col_secs)}"
    )
    return {
        "fail_col_sections": sorted(col_secs),
        "extra_fail_count": len(failing),
        "notes": [note],
    }
