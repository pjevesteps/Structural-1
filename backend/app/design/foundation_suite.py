from __future__ import annotations

import math

from ..models import (
    CombinedFootingDesign,
    DesignParameters,
    FootingDesignResult,
    MatFoundationDesign,
    ProjectState,
    SupportReactionEnvelope,
)


def _bar_area_mm2(dia_mm: float) -> float:
    return math.pi * (dia_mm ** 2) / 4.0


def _pick_bar_spacing(as_req_mm2_per_m: float, dia_mm: int, max_s: int = 250, min_s: int = 100) -> str:
    ab = _bar_area_mm2(dia_mm)
    raw = (ab * 1000.0) / max(as_req_mm2_per_m, 1.0)
    s = int(math.floor(min(raw, float(max_s)) / 25.0) * 25)
    s = max(min_s, s)
    return f"{dia_mm}mm @ {s}mm o.c."


def compute_net_bearing_kpa(
    project: ProjectState,
    thickness_mm: float,
    params: DesignParameters | None = None,
) -> tuple[float, float, float]:
    """
    Single source of truth for net allowable soil bearing capacity q_net (kPa).
    Accounts for embedment overburden and groundwater table D_w < D_f (submerged gamma' = gamma_s - 9.81),
    floored at 0.5 * q_a.
    Returns (q_net_kpa, overburden_kpa, qa_gross_kpa).
    """
    soil = project.soil_investigation
    dp = params if params is not None else project.analysis_design.params
    mat = project.member_sizing.material

    # Prefer soil_investigation; if legacy caller only mutated DesignParameters, honor that too
    qa = soil.allowable_bearing_kpa
    if abs(qa - 150.0) < 1e-6 and abs(dp.soil_bearing_qa_kpa - 150.0) > 1e-6:
        qa = dp.soil_bearing_qa_kpa
    gamma_s = soil.soil_unit_weight_kn_m3
    if abs(gamma_s - 18.0) < 1e-6 and abs(dp.soil_unit_weight_kn_m3 - 18.0) > 1e-6:
        gamma_s = dp.soil_unit_weight_kn_m3
    df = soil.embedment_depth_m
    if abs(df - 1.5) < 1e-6 and abs(dp.footing_embedment_m - 1.5) > 1e-6:
        df = dp.footing_embedment_m

    gamma_c = mat.density_kn_m3
    t_m = max(0.0, thickness_mm / 1000.0)
    dw = soil.water_table_depth_m

    if dw is not None and dw < df:
        h_dry = max(0.0, dw)
        h_sub = max(0.0, df - h_dry)
        gamma_sub = max(6.0, gamma_s - 9.81)
        overburden = h_dry * gamma_s + h_sub * gamma_sub + t_m * max(gamma_c - gamma_s, 0.0)
        # Terzaghi water-table bearing reduction factor Rw in [0.5, 1.0]
        rw = 0.5 + 0.5 * (h_dry / max(df, 0.1))
        q_net = max(0.5 * qa, qa * rw - overburden)
    else:
        overburden = df * gamma_s + t_m * max(gamma_c - gamma_s, 0.0)
        q_net = max(0.5 * qa, qa - overburden)

    return round(q_net, 2), round(overburden, 2), round(qa, 2)


def _col_dims_by_node(project: ProjectState) -> dict[int, tuple[float, float]]:
    sec_by_id = {s.section_id: s for s in project.member_sizing.sections}
    asgn_by_mid = {a.member_id: a for a in project.member_sizing.member_assignments}
    dims: dict[int, tuple[float, float]] = {}
    for m in project.structural_model.members:
        if m.member_type != "column":
            continue
        asgn = asgn_by_mid.get(m.member_id)
        sec = sec_by_id.get(asgn.section_id) if asgn else None
        if sec is None:
            continue
        d = (sec.width_mm / 1000.0, sec.depth_mm / 1000.0)
        dims.setdefault(m.start_node, d)
        dims.setdefault(m.end_node, d)
    return dims


def design_combined_footings(
    project: ProjectState,
    support_reactions: list[SupportReactionEnvelope],
    params: DesignParameters,
) -> list[CombinedFootingDesign]:
    """
    Design every node group in `project.merged_footing_groups` as a CombinedFootingDesign.
    2-column groups are centered on the load resultant along their column axis;
    3+ column groups use a rectangular 2D bounding pad with overhangs.
    """
    if not project.merged_footing_groups:
        return []

    sm = project.structural_model
    mat = project.member_sizing.material
    fc = mat.fc_mpa
    fy = mat.fy_main_mpa
    node_by_id = {n.node_id: n for n in sm.nodes}
    rxn_by_nid = {r.node_id: r for r in support_reactions}
    col_dims = _col_dims_by_node(project)

    results: list[CombinedFootingDesign] = []
    for idx, raw_group in enumerate(project.merged_footing_groups, start=1):
        nids = sorted({int(nid) for nid in raw_group if int(nid) in node_by_id})
        if len(nids) < 2:
            continue

        nodes = [node_by_id[nid] for nid in nids]
        ps_list = [max(rxn_by_nid[nid].max_asd_fy_kn if nid in rxn_by_nid else 150.0, 10.0) for nid in nids]
        pu_list = [
            max(rxn_by_nid[nid].max_lrfd_pu_kn if nid in rxn_by_nid else 1.4 * ps, 1.35 * ps)
            for nid, ps in zip(nids, ps_list)
        ]
        total_ps = sum(ps_list)
        total_pu = sum(pu_list)

        # Resultant coordinates
        cx = sum(p * n.x_m for p, n in zip(ps_list, nodes)) / max(total_ps, 1e-6)
        cz = sum(p * n.z_m for p, n in zip(ps_list, nodes)) / max(total_ps, 1e-6)
        avg_y = sum(n.y_m for n in nodes) / len(nodes)

        if len(nodes) == 2:
            dx = nodes[1].x_m - nodes[0].x_m
            dz = nodes[1].z_m - nodes[0].z_m
            dist = max(math.hypot(dx, dz), 0.5)
            orient_deg = round(math.degrees(math.atan2(dz, dx)), 1)
            x_bar = (ps_list[1] * dist) / max(total_ps, 1e-6)
            l_min = max(2.0 * max(x_bar, dist - x_bar) + 0.8, dist + 1.0)
            b_min = 1.2
        else:
            orient_deg = 0.0
            span_x = max(n.x_m for n in nodes) - min(n.x_m for n in nodes)
            span_z = max(n.z_m for n in nodes) - min(n.z_m for n in nodes)
            l_min = max(span_x + 1.2, 1.8)
            b_min = max(span_z + 1.2, 1.8)
            cx = 0.5 * (max(n.x_m for n in nodes) + min(n.x_m for n in nodes))
            cz = 0.5 * (max(n.z_m for n in nodes) + min(n.z_m for n in nodes))

        chosen = None
        for t_mm in range(400, 1250, 50):
            q_net, _, _ = compute_net_bearing_kpa(project, float(t_mm), params)
            a_req = total_ps / max(q_net, 10.0)
            l_m = round(math.ceil(max(l_min, math.sqrt(a_req * 1.4)) * 10.0) / 10.0, 2)
            b_m = round(math.ceil(max(b_min, a_req / max(l_m, 0.5)) * 10.0) / 10.0, 2)
            area = l_m * b_m
            q_max = total_ps / max(area, 0.1)
            bearing_dcr = q_max / max(q_net, 1e-6)

            d_mm = max(150.0, t_mm - params.clear_cover_footing_mm - 20.0)
            d_m = d_mm / 1000.0
            qu_kpa = total_pu / max(area, 0.1)

            # One-way beam shear across width B
            vu_1w = max(pu_list) * 0.65
            phi_vc_1w = 0.75 * 0.17 * math.sqrt(fc) * (b_m * 1000.0) * d_mm / 1000.0
            dcr_1w = vu_1w / max(phi_vc_1w, 1e-6)

            # Two-way punching shear around each column in the group
            max_dcr_2w = 0.0
            for nid, pu_col in zip(nids, pu_list):
                c_x, c_z = col_dims.get(nid, (0.40, 0.40))
                bo_mm = 2.0 * ((c_x + d_m) + (c_z + d_m)) * 1000.0
                vu_2w = max(0.0, pu_col - qu_kpa * (c_x + d_m) * (c_z + d_m))
                phi_vc_2w = 0.75 * 0.33 * math.sqrt(fc) * bo_mm * d_mm / 1000.0
                max_dcr_2w = max(max_dcr_2w, vu_2w / max(phi_vc_2w, 1e-6))

            chosen = (l_m, b_m, float(t_mm), d_mm, q_max, q_net, bearing_dcr, dcr_1w, max_dcr_2w, qu_kpa)
            if bearing_dcr <= 1.0 and dcr_1w <= 1.0 and max_dcr_2w <= 1.0:
                break

        assert chosen is not None
        l_m, b_m, t_mm, d_mm, q_max, q_net, bearing_dcr, dcr_1w, max_dcr_2w, qu_kpa = chosen

        # Flexural design (per meter width)
        span_est = max(l_m * 0.55, 1.2)
        mu_top = (qu_kpa * (span_est ** 2)) / 10.0
        mu_bot = (qu_kpa * ((max(0.4, 0.2 * l_m)) ** 2)) * 0.5
        mu_trans = (qu_kpa * ((max(0.4, 0.5 * (b_m - 0.4))) ** 2)) * 0.5

        as_min = 0.0018 * 1000.0 * t_mm

        def _req_as(mu_knm: float) -> float:
            rn = (mu_knm * 1e6) / max(0.90 * 1000.0 * (d_mm ** 2), 1.0)
            disc = max(0.05, 1.0 - (2.0 * rn) / (0.85 * fc))
            rho = (0.85 * fc / fy) * (1.0 - math.sqrt(disc))
            return max(rho * 1000.0 * d_mm, as_min)

        top_long = _pick_bar_spacing(_req_as(mu_top), 16)
        bot_long = _pick_bar_spacing(_req_as(mu_bot), 16)
        bot_trans = _pick_bar_spacing(_req_as(mu_trans), 16)

        gov = max(bearing_dcr, dcr_1w, max_dcr_2w)
        marks = [node_by_id[nid].grid_ref or f"N{nid}" for nid in nids]
        results.append(
            CombinedFootingDesign(
                combined_id=f"CF-{idx}",
                merged_node_ids=nids,
                merged_footing_marks=marks,
                center_x_m=round(cx, 3),
                center_z_m=round(cz, 3),
                y_m=round(avg_y, 3),
                orientation_deg=orient_deg,
                length_l_m=l_m,
                width_b_m=b_m,
                thickness_mm=t_mm,
                total_pu_kn=round(total_pu, 2),
                total_ps_kn=round(total_ps, 2),
                q_max_kpa=round(q_max, 2),
                q_net_allow_kpa=round(q_net, 2),
                bearing_dcr=round(bearing_dcr, 3),
                one_way_shear_dcr=round(dcr_1w, 3),
                punching_shear_dcr=round(max_dcr_2w, 3),
                top_long_bars=top_long,
                bot_long_bars=bot_long,
                bot_trans_bars=bot_trans,
                status="PASS" if gov <= 1.0 else "FAIL",
            )
        )
    return results


def design_mat_foundation(
    project: ProjectState,
    support_reactions: list[SupportReactionEnvelope],
    params: DesignParameters,
    overhang_m: float = 0.75,
) -> MatFoundationDesign | None:
    """
    NSCP 2015 / ACI 318 Rigid Mat / Raft Foundation design:
      - Computes biaxial eccentricities ex, ez and corner pressures q_max / q_min
      - Flags uplift_detected if q_min < 0
      - Checks 2-way punching shear around every interior, edge & corner column
        and auto-steps thickness_mm (400..1500 mm in 50 mm steps)
      - Designs orthogonal top & bottom rebar mats (>= 0.0018 * b * t)
    """
    sm = project.structural_model
    sup_nodes = [n for n in sm.nodes if n.is_support]
    if not sup_nodes:
        return None

    mat = project.member_sizing.material
    fc = mat.fc_mpa
    fy = mat.fy_main_mpa
    rxn_by_nid = {r.node_id: r for r in support_reactions}
    col_dims = _col_dims_by_node(project)

    min_x = min(n.x_m for n in sup_nodes) - overhang_m
    max_x = max(n.x_m for n in sup_nodes) + overhang_m
    min_z = min(n.z_m for n in sup_nodes) - overhang_m
    max_z = max(n.z_m for n in sup_nodes) + overhang_m
    bx = max(1.5, max_x - min_x)
    lz = max(1.5, max_z - min_z)
    area = bx * lz
    cx = 0.5 * (min_x + max_x)
    cz = 0.5 * (min_z + max_z)
    y_base = min(n.y_m for n in sup_nodes)

    total_ps = 0.0
    total_pu = 0.0
    sum_px = 0.0
    sum_pz = 0.0
    node_loads: list[tuple[int, float, float, float, float]] = []
    for n in sup_nodes:
        r = rxn_by_nid.get(n.node_id)
        ps = max(r.max_asd_fy_kn if r else 100.0, 1.0)
        pu = max(r.max_lrfd_pu_kn if r else 1.4 * ps, 1.35 * ps)
        total_ps += ps
        total_pu += pu
        sum_px += ps * n.x_m
        sum_pz += ps * n.z_m
        node_loads.append((n.node_id, n.x_m, n.z_m, ps, pu))

    x_bar = sum_px / max(total_ps, 1e-6)
    z_bar = sum_pz / max(total_ps, 1e-6)
    ex = x_bar - cx
    ez = z_bar - cz

    q_avg = total_ps / max(area, 0.1)
    q_max = q_avg * (1.0 + 6.0 * abs(ex) / bx + 6.0 * abs(ez) / lz)
    q_min = q_avg * (1.0 - 6.0 * abs(ex) / bx - 6.0 * abs(ez) / lz)
    uplift = q_min < 0.0
    qu_avg = total_pu / max(area, 0.1)

    chosen = None
    for t_mm in range(400, 1550, 50):
        q_net, _, _ = compute_net_bearing_kpa(project, float(t_mm), params)
        bearing_dcr = q_max / max(q_net, 1e-6)
        d_mm = max(200.0, t_mm - params.clear_cover_footing_mm - 20.0)
        d_m = d_mm / 1000.0

        gov_nid = None
        max_punch_dcr = 0.0
        for nid, nx, nz, _, pu_col in node_loads:
            c_x, c_z = col_dims.get(nid, (0.40, 0.40))
            near_x = min(nx - min_x, max_x - nx) < (0.5 * c_x + d_m + 0.1)
            near_z = min(nz - min_z, max_z - nz) < (0.5 * c_z + d_m + 0.1)
            if near_x and near_z:
                # Corner column (2-sided punching perimeter)
                bo_m = (c_x + 0.5 * d_m) + (c_z + 0.5 * d_m)
                a_trib = (c_x + 0.5 * d_m) * (c_z + 0.5 * d_m)
            elif near_x:
                # Edge column along X edge (3-sided perimeter)
                bo_m = 2.0 * (c_x + 0.5 * d_m) + (c_z + d_m)
                a_trib = (c_x + 0.5 * d_m) * (c_z + d_m)
            elif near_z:
                # Edge column along Z edge (3-sided perimeter)
                bo_m = (c_x + d_m) + 2.0 * (c_z + 0.5 * d_m)
                a_trib = (c_x + d_m) * (c_z + 0.5 * d_m)
            else:
                # Interior column (4-sided perimeter)
                bo_m = 2.0 * ((c_x + d_m) + (c_z + d_m))
                a_trib = (c_x + d_m) * (c_z + d_m)

            vu_2w = max(0.0, pu_col - qu_avg * a_trib)
            phi_vc_2w = 0.75 * 0.33 * math.sqrt(fc) * (bo_m * 1000.0) * d_mm / 1000.0
            dcr_2w = vu_2w / max(phi_vc_2w, 1e-6)
            if dcr_2w > max_punch_dcr:
                max_punch_dcr = dcr_2w
                gov_nid = nid

        chosen = (float(t_mm), d_mm, q_net, bearing_dcr, gov_nid, max_punch_dcr)
        if max_punch_dcr <= 1.0:
            break

    assert chosen is not None
    t_mm, d_mm, q_net, bearing_dcr, gov_nid, max_punch_dcr = chosen

    # Orthogonal strip flexural design (w_u * L_bay^2 / 10)
    xs_u = sorted({round(n.x_m, 2) for n in sup_nodes})
    zs_u = sorted({round(n.z_m, 2) for n in sup_nodes})
    bay_x = max([xs_u[i + 1] - xs_u[i] for i in range(len(xs_u) - 1)], default=4.0)
    bay_z = max([zs_u[i + 1] - zs_u[i] for i in range(len(zs_u) - 1)], default=4.0)
    as_min = 0.0018 * 1000.0 * t_mm

    def _mat_as(span_m: float, factor: float) -> float:
        mu = (qu_avg * (span_m ** 2)) / factor
        rn = (mu * 1e6) / max(0.90 * 1000.0 * (d_mm ** 2), 1.0)
        disc = max(0.05, 1.0 - (2.0 * rn) / (0.85 * fc))
        rho = (0.85 * fc / fy) * (1.0 - math.sqrt(disc))
        return max(rho * 1000.0 * d_mm, as_min)

    top_x = _pick_bar_spacing(_mat_as(bay_x, 10.0), 16)
    bot_x = _pick_bar_spacing(_mat_as(bay_x, 12.0), 16)
    top_z = _pick_bar_spacing(_mat_as(bay_z, 10.0), 16)
    bot_z = _pick_bar_spacing(_mat_as(bay_z, 12.0), 16)

    status = "PASS" if (bearing_dcr <= 1.0 and max_punch_dcr <= 1.0 and not uplift) else "FAIL"
    return MatFoundationDesign(
        mat_id="MAT-1",
        y_m=round(y_base, 3),
        min_x_m=round(min_x, 2),
        max_x_m=round(max_x, 2),
        min_z_m=round(min_z, 2),
        max_z_m=round(max_z, 2),
        width_bx_m=round(bx, 2),
        length_lz_m=round(lz, 2),
        overhang_m=round(overhang_m, 2),
        thickness_mm=t_mm,
        total_pu_kn=round(total_pu, 2),
        total_ps_kn=round(total_ps, 2),
        eccentricity_ex_m=round(ex, 3),
        eccentricity_ez_m=round(ez, 3),
        q_max_kpa=round(q_max, 2),
        q_min_kpa=round(q_min, 2),
        q_net_allow_kpa=round(q_net, 2),
        uplift_detected=uplift,
        bearing_dcr=round(bearing_dcr, 3),
        governing_punching_node_id=gov_nid,
        max_punching_dcr=round(max_punch_dcr, 3),
        top_bars_x=top_x,
        top_bars_z=top_z,
        bot_bars_x=bot_x,
        bot_bars_z=bot_z,
        status=status,
    )


def compute_footing_coverage_ratio(
    project: ProjectState,
    footings: list[FootingDesignResult],
    combined: list[CombinedFootingDesign],
) -> float:
    """Return total shallow footing plan area / building footprint area."""
    sup_nodes = [n for n in project.structural_model.nodes if n.is_support]
    if len(sup_nodes) < 2:
        return 0.0
    span_x = max(n.x_m for n in sup_nodes) - min(n.x_m for n in sup_nodes)
    span_z = max(n.z_m for n in sup_nodes) - min(n.z_m for n in sup_nodes)
    footprint = max(span_x, 1.0) * max(span_z, 1.0)
    ftg_area = sum(f.b_m * f.l_m for f in footings) + sum(c.width_b_m * c.length_l_m for c in combined)
    return round(ftg_area / max(footprint, 1.0), 3)


def cluster_connected_clashes(clashes: list[dict]) -> list[list[int]]:
    """Cluster pairwise clash edges into connected components of node IDs."""
    adj: dict[int, set[int]] = {}
    for cl in clashes:
        na = cl.get("node_a")
        nb = cl.get("node_b")
        if na is None or nb is None:
            continue
        adj.setdefault(int(na), set()).add(int(nb))
        adj.setdefault(int(nb), set()).add(int(na))

    visited: set[int] = set()
    groups: list[list[int]] = []
    for start in sorted(adj.keys()):
        if start in visited:
            continue
        comp: list[int] = []
        stack = [start]
        visited.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in sorted(adj.get(cur, set())):
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        if len(comp) >= 2:
            groups.append(sorted(comp))
    return groups
