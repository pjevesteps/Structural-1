from __future__ import annotations

from typing import Any

from ..models import ProjectState

ENGINEERING_DISCLAIMER = (
    "Engineering Disclaimer: These computer-aided structural design calculations are generated in "
    "accordance with the National Structural Code of the Philippines (NSCP 2015, Vol. 1, 7th Ed.) "
    "and ACI 318 strength design provisions using the analytical model, material strengths, soil "
    "parameters, and loading criteria defined by the user. All outputs, member schedules, and "
    "detailing callouts must be independently reviewed, verified, signed, and sealed by a licensed "
    "Civil/Structural Engineer prior to Office of the Building Official (OBO) permit submittal "
    "and construction per NBCP (PD 1096) Section 302-5."
)


def framing_system_summary(project: ProjectState) -> tuple[str, str]:
    """Return (framing_system_label, ductility_clause) matching the actual design run."""
    sp = project.load_generation.seismic
    smrf = project.member_sizing.options.seismic_smrf_zone4
    rx = sp.r_factor_x
    rz = sp.r_factor_z
    if smrf or max(rx, rz) >= 8.0:
        return (
            f"Special Moment Resisting Frame (SMRF) — Rx = {rx:.1f}, Rz = {rz:.1f}, I = {sp.importance_factor_i:.2f}, rho = {sp.redundancy_rho:.2f}",
            "NSCP 2015 Sec. 418 & Table 208-11A (Zone 4 SMRF)",
        )
    if max(rx, rz) >= 5.0:
        return (
            f"Intermediate Moment Resisting Frame (IMRF) — Rx = {rx:.1f}, Rz = {rz:.1f}, I = {sp.importance_factor_i:.2f}, rho = {sp.redundancy_rho:.2f}",
            "NSCP 2015 Sec. 418.4 & Table 208-11A",
        )
    return (
        f"Ordinary Moment Resisting Frame (OMRF) — Rx = {rx:.1f}, Rz = {rz:.1f}, I = {sp.importance_factor_i:.2f}, rho = {sp.redundancy_rho:.2f}",
        "NSCP 2015 Table 208-11A",
    )


def nscp_code_reference_rows() -> list[list[str]]:
    return [
        ["NSCP 2015 Sec. 203", "LRFD (Strength) & ASD (Service) Load Combinations"],
        ["NSCP 2015 Sec. 204 & 205", "Dead Loads (Table 204-1) & Floor/Roof Live Loads (Tables 205-1/205-2)"],
        ["NSCP 2015 Sec. 207", "Main Wind Force Resisting System (MWFRS) Velocity Pressure & Story Forces"],
        ["NSCP 2015 Sec. 208", "Equivalent Static Seismic Base Shear (Sec. 208.5) & Inelastic Story Drift (Sec. 208.6.4)"],
        ["NSCP 2015 Sec. 406.6.3.1", "Cracked Section Stiffness Modifiers (0.70 Ig Columns, 0.35 Ig Beams)"],
        ["NSCP 2015 Sec. 409 & 422", "RC Beam Flexure (phi = 0.90) & One-Way Shear / Stirrups (phi = 0.75)"],
        ["NSCP 2015 Sec. 410 & 422.4", "RC Column Axial-Flexural Interaction (P-M-M, phi = 0.65) & Transverse Ties"],
        ["NSCP 2015 Sec. 413 & 422.6", "Spread/Combined/Mat Footing Bearing, One-Way Shear & Two-Way Punching Shear"],
        ["NSCP 2015 Sec. 418", "SMRF Special Detailing, Confinement & Strong-Column/Weak-Beam (SCWB >= 1.20)"],
        ["NBCP (PD 1096) Sec. 302-5", "Structural Design Calculations, Geotechnical Requirements & Bill of Materials"],
    ]


def build_governing_demand_rows(project: ProjectState) -> list[list[str]]:
    """Compact governing demand envelope across beams, columns, and foundation supports."""
    ad = project.analysis_design
    rows: list[list[str]] = []

    if ad.beam_designs:
        max_b_neg = max(ad.beam_designs, key=lambda b: max(b.mu_neg_start_knm, b.mu_neg_end_knm))
        max_b_pos = max(ad.beam_designs, key=lambda b: b.mu_pos_mid_knm)
        max_b_v = max(ad.beam_designs, key=lambda b: b.vu_max_kn)
        rows.append(
            [
                "Beams — Max Negative Moment (Mu-)",
                f"M{max_b_neg.member_id} ({max_b_neg.section_id}, {max_b_neg.floor_level})",
                f"{max(max_b_neg.mu_neg_start_knm, max_b_neg.mu_neg_end_knm):.2f} kN·m",
                max_b_neg.governing_combo or "Envelope",
                f"DCR {max_b_neg.flexure_dcr:.2f} ({max_b_neg.status})",
            ]
        )
        rows.append(
            [
                "Beams — Max Positive Moment (Mu+)",
                f"M{max_b_pos.member_id} ({max_b_pos.section_id}, {max_b_pos.floor_level})",
                f"{max_b_pos.mu_pos_mid_knm:.2f} kN·m",
                max_b_pos.governing_combo or "Envelope",
                f"DCR {max_b_pos.flexure_dcr:.2f} ({max_b_pos.status})",
            ]
        )
        rows.append(
            [
                "Beams — Max Factored Shear (Vu)",
                f"M{max_b_v.member_id} ({max_b_v.section_id}, {max_b_v.floor_level})",
                f"{max_b_v.vu_max_kn:.2f} kN",
                max_b_v.governing_combo or "Envelope",
                f"DCR {max_b_v.shear_dcr:.2f} ({max_b_v.status})",
            ]
        )

    if ad.column_designs:
        max_c_pu = max(ad.column_designs, key=lambda c: c.pu_max_kn)
        max_c_mu = max(ad.column_designs, key=lambda c: max(c.mux_max_knm, c.muy_max_knm))
        max_c_v = max(ad.column_designs, key=lambda c: c.vu_max_kn)
        rows.append(
            [
                "Columns — Max Factored Axial (Pu)",
                f"M{max_c_pu.member_id} ({max_c_pu.section_id}, {max_c_pu.floor_level})",
                f"{max_c_pu.pu_max_kn:.2f} kN",
                max_c_pu.governing_combo or "Envelope",
                f"P-M DCR {max_c_pu.pm_dcr:.2f} ({max_c_pu.status})",
            ]
        )
        rows.append(
            [
                "Columns — Max Biaxial Moment (Mu)",
                f"M{max_c_mu.member_id} ({max_c_mu.section_id}, {max_c_mu.floor_level})",
                f"{max(max_c_mu.mux_max_knm, max_c_mu.muy_max_knm):.2f} kN·m",
                max_c_mu.governing_combo or "Envelope",
                f"P-M DCR {max_c_mu.pm_dcr:.2f} ({max_c_mu.status})",
            ]
        )
        rows.append(
            [
                "Columns — Max Factored Shear (Vu)",
                f"M{max_c_v.member_id} ({max_c_v.section_id}, {max_c_v.floor_level})",
                f"{max_c_v.vu_max_kn:.2f} kN",
                max_c_v.governing_combo or "Envelope",
                f"Shear DCR {max_c_v.shear_dcr:.2f} ({max_c_v.status})",
            ]
        )

    if ad.support_reactions:
        max_r_asd = max(ad.support_reactions, key=lambda r: r.max_asd_fy_kn)
        min_r_asd = min(ad.support_reactions, key=lambda r: r.min_asd_fy_kn)
        max_r_pu = max(ad.support_reactions, key=lambda r: r.max_lrfd_pu_kn)
        rows.append(
            [
                "Supports — Max Service Gravity (ASD Fy)",
                f"N{max_r_asd.node_id} ({max_r_asd.grid_ref or max_r_asd.floor_level})",
                f"{max_r_asd.max_asd_fy_kn:.2f} kN",
                max_r_asd.governing_asd_combo or "ASD Envelope",
                f"Min Fy = {min_r_asd.min_asd_fy_kn:.2f} kN",
            ]
        )
        rows.append(
            [
                "Supports — Max Factored Axial (LRFD Pu)",
                f"N{max_r_pu.node_id} ({max_r_pu.grid_ref or max_r_pu.floor_level})",
                f"{max_r_pu.max_lrfd_pu_kn:.2f} kN",
                max_r_pu.governing_lrfd_combo or "LRFD Envelope",
                f"Mu = {max(max_r_pu.max_lrfd_mux_knm, max_r_pu.max_lrfd_muz_knm):.1f} kN·m",
            ]
        )

    return rows


def _implied_capacity(demand: float, dcr: float) -> str:
    if demand <= 1e-6 or dcr <= 1e-6:
        return "Adequate (Min. Reinf.)"
    return f"{demand / dcr:,.1f}"


def build_member_calculation_sheets(
    project: ProjectState,
    max_groups_per_type: int = 12,
) -> dict[str, Any]:
    """
    Group identical member section tags and pick the governing (worst-case DCR) member per tag
    to produce concise, auditable NSCP 2015 Per-Member Design Calculation Sheets without
    bloating large multi-storey reports.
    """
    ad = project.analysis_design
    mat = project.member_sizing.material
    dp = ad.params

    # 1. Governing Beams grouped by section_id
    beam_groups: dict[str, list] = {}
    for b in ad.beam_designs:
        beam_groups.setdefault(b.section_id, []).append(b)
    beam_sheets: list[dict[str, Any]] = []
    for sec_id in sorted(beam_groups.keys())[:max_groups_per_type]:
        grp = beam_groups[sec_id]
        gov = max(grp, key=lambda x: x.governing_dcr)
        mu_gov = max(gov.mu_neg_start_knm, gov.mu_pos_mid_knm, gov.mu_neg_end_knm)
        d_eff_mm = max(50.0, gov.depth_mm - dp.clear_cover_beam_mm - dp.stirrup_tie_dia_mm - 0.5 * dp.main_bar_dia_beam_mm)
        phi_mn_str = _implied_capacity(mu_gov, gov.flexure_dcr)
        phi_vn_str = _implied_capacity(gov.vu_max_kn, gov.shear_dcr)
        beam_sheets.append(
            {
                "title": f"Beam Section {sec_id} — Governing Member M{gov.member_id} ({gov.floor_level}, 1 of {len(grp)} member(s))",
                "status": gov.status,
                "rows": [
                    ["Section & Geometry", f"b = {gov.width_mm:.0f} mm, h = {gov.depth_mm:.0f} mm, d = {d_eff_mm:.0f} mm, L = {gov.length_m:.2f} m", "NSCP Sec. 409.3"],
                    ["Material Strengths", f"f'c = {mat.fc_mpa:.1f} MPa, fy = {mat.fy_main_mpa:.1f} MPa, fyt = {mat.fy_tie_mpa:.1f} MPa, cc = {dp.clear_cover_beam_mm:.0f} mm", "NSCP Sec. 419 / 420"],
                    ["Governing Load Combination", gov.governing_combo or "NSCP LRFD Envelope", "NSCP Sec. 203.3"],
                    ["Design Moments (Mu- / Mu+ / Mu-)", f"Start: {gov.mu_neg_start_knm:.2f} kN·m | Mid: {gov.mu_pos_mid_knm:.2f} kN·m | End: {gov.mu_neg_end_knm:.2f} kN·m", "3D DSM Envelope"],
                    [
                        "Longitudinal Flexure Check",
                        f"Mu,max = {mu_gov:.2f} kN·m vs phi*Mn = {phi_mn_str} kN·m | As,req(top/bot) = {max(gov.as_req_top_start_mm2, gov.as_req_top_end_mm2):.0f}/{gov.as_req_bot_mid_mm2:.0f} mm² | Top: {gov.bars_top_start} / {gov.bars_top_end}, Bot: {gov.bars_bot_mid} | DCR = {gov.flexure_dcr:.3f}",
                        "NSCP Sec. 409.5 / 422.2",
                    ],
                    [
                        "Transverse Shear & Stirrups",
                        f"Vu,max = {gov.vu_max_kn:.2f} kN vs phi*Vn = {phi_vn_str} kN | Support: {gov.stirrups_support} | Midspan: {gov.stirrups_midspan} | DCR = {gov.shear_dcr:.3f}",
                        "NSCP Sec. 422.5 / 418.6.4",
                    ],
                    [
                        "Governing Verification",
                        f"Max DCR = {gov.governing_dcr:.3f} (<= 1.00 required) — {gov.status}",
                        "NSCP Sec. 409.5",
                    ],
                ],
            }
        )

    # 2. Governing Columns grouped by section_id
    col_groups: dict[str, list] = {}
    for c in ad.column_designs:
        col_groups.setdefault(c.section_id, []).append(c)
    col_sheets: list[dict[str, Any]] = []
    for sec_id in sorted(col_groups.keys())[:max_groups_per_type]:
        grp = col_groups[sec_id]
        gov = max(grp, key=lambda x: x.governing_dcr)
        ag_mm2 = gov.width_mm * gov.depth_mm
        mu_biax = max(gov.mux_max_knm, gov.muy_max_knm)
        phi_pn_str = _implied_capacity(gov.pu_max_kn, gov.pm_dcr)
        phi_vn_str = _implied_capacity(gov.vu_max_kn, gov.shear_dcr)
        col_sheets.append(
            {
                "title": f"Column Section {sec_id} — Governing Member M{gov.member_id} ({gov.floor_level}, 1 of {len(grp)} member(s))",
                "status": gov.status,
                "rows": [
                    ["Section & Geometry", f"b = {gov.width_mm:.0f} mm, h = {gov.depth_mm:.0f} mm, Ag = {ag_mm2:,.0f} mm², L = {gov.length_m:.2f} m", "NSCP Sec. 418.7.2"],
                    ["Material Strengths", f"f'c = {mat.fc_mpa:.1f} MPa, fy = {mat.fy_main_mpa:.1f} MPa, fyt = {mat.fy_tie_mpa:.1f} MPa, cc = {dp.clear_cover_column_mm:.0f} mm", "NSCP Sec. 419 / 420"],
                    ["Governing Load Combination", gov.governing_combo or "NSCP LRFD Envelope", "NSCP Sec. 203.3"],
                    ["Design Forces (Pu / Mux / Muy)", f"Pu,max = {gov.pu_max_kn:.2f} kN (Pu,min = {gov.pu_min_kn:.2f} kN) | Mux = {gov.mux_max_knm:.2f} kN·m | Muy = {gov.muy_max_knm:.2f} kN·m", "3D DSM Envelope"],
                    [
                        "Axial-Flexural (P-M-M) Check",
                        f"Pu = {gov.pu_max_kn:.2f} kN (Cap. @ e = {phi_pn_str} kN, Mu,max = {mu_biax:.2f} kN·m) | Main Bars: {gov.main_bars} (rho = {gov.rho_provided * 100:.2f}%, 1.0%-6.0% SMRF) | P-M DCR = {gov.pm_dcr:.3f}",
                        "NSCP Sec. 410.5 / 422.4",
                    ],
                    [
                        "Transverse Shear & Confinement",
                        f"Vu,max = {gov.vu_max_kn:.2f} kN vs phi*Vn = {phi_vn_str} kN | Confined (lo): {gov.ties_confined} | Midheight: {gov.ties_mid} | Shear DCR = {gov.shear_dcr:.3f}",
                        "NSCP Sec. 418.7.5 / 422.5",
                    ],
                    [
                        "Governing Verification",
                        f"Max DCR = {gov.governing_dcr:.3f} (<= 1.00 required) — {gov.status}",
                        "NSCP Sec. 410.5",
                    ],
                ],
            }
        )

    # 3. Governing Isolated Footings grouped by size/tag
    ftg_groups: dict[str, list] = {}
    for f in ad.footing_designs:
        key = f"{f.b_m:.2f}x{f.l_m:.2f}x{f.thickness_mm:.0f}"
        ftg_groups.setdefault(key, []).append(f)
    ftg_sheets: list[dict[str, Any]] = []
    for size_key in sorted(ftg_groups.keys())[:max_groups_per_type]:
        grp = ftg_groups[size_key]
        gov = max(grp, key=lambda x: x.governing_dcr)
        marks = ", ".join(x.footing_tag for x in grp[:6]) + (f" (+{len(grp) - 6})" if len(grp) > 6 else "")
        ftg_sheets.append(
            {
                "title": f"Footing Group {gov.footing_tag} ({gov.b_m:.2f} x {gov.l_m:.2f} m x {gov.thickness_mm:.0f} mm — {len(grp)} footing(s): {marks})",
                "status": gov.status,
                "rows": [
                    ["Governing Support & Geometry", f"Node N{gov.node_id} ({gov.grid_ref or '-'}) @ Elev. {gov.base_elevation_m:.2f} m | B = {gov.b_m:.2f} m, L = {gov.l_m:.2f} m, t = {gov.thickness_mm:.0f} mm", "NSCP Sec. 413.3"],
                    ["Service & Factored Loads", f"P_service = {gov.p_service_kn:.2f} kN (ASD) | Pu = {gov.pu_kn:.2f} kN (LRFD)", "NSCP Sec. 203"],
                    ["Soil Bearing Check", f"q_actual = {gov.q_actual_kpa:.1f} kPa vs q_allow = {gov.q_allow_kpa:.1f} kPa | Bearing DCR = {gov.bearing_dcr:.3f}", "NSCP Sec. 304 / 413"],
                    ["Shear Checks (1-Way & Punching)", f"One-Way Shear DCR = {gov.one_way_shear_dcr:.3f} | Two-Way Punching Shear DCR = {gov.punching_shear_dcr:.3f}", "NSCP Sec. 422.5 / 422.6"],
                    ["Flexural Reinforcement", f"Bottom X: {gov.bottom_bars_x} | Bottom Z: {gov.bottom_bars_z} (cc = {dp.clear_cover_footing_mm:.0f} mm)", "NSCP Sec. 413.2.7"],
                    ["Governing Verification", f"Max DCR = {gov.governing_dcr:.3f} (<= 1.00 required) — {gov.status}", "NSCP Sec. 413"],
                ],
            }
        )

    return {
        "beam_sheets": beam_sheets,
        "col_sheets": col_sheets,
        "ftg_sheets": ftg_sheets,
        "total_beam_groups": len(beam_groups),
        "total_col_groups": len(col_groups),
        "total_ftg_groups": len(ftg_groups),
    }
