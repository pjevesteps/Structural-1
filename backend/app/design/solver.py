from dataclasses import dataclass, field
import math
import numpy as np

from ..models import (
    AnalyticalMember,
    AnalyticalNode,
    BeamDistributedLoad,
    ProjectState,
    SupportReactionEnvelope,
)

PRIMARY_CASES = [
    "EX+",
    "EX-",
    "EZ+",
    "EZ-",
    "WX+",
    "WX-",
    "WZ+",
    "WZ-",
    "DL",
    "LL",
    "LR",
]
PRIMARY_IDX = {name: i for i, name in enumerate(PRIMARY_CASES)}

# 4-point Gauss-Legendre quadrature on [-1, 1] (exact up to degree 7 polynomials)
_GL_XI = np.array(
    [
        -0.8611363115940526,
        -0.3399810435848563,
        0.3399810435848563,
        0.8611363115940526,
    ],
    dtype=float,
)
_GL_W = np.array(
    [
        0.3478548451374538,
        0.6521451548625461,
        0.6521451548625461,
        0.3478548451374538,
    ],
    dtype=float,
)


@dataclass
class BeamForceEnvelope:
    member_id: int
    mu_neg_start_knm: float = 0.0
    mu_pos_mid_knm: float = 0.0
    mu_neg_end_knm: float = 0.0
    vu_max_kn: float = 0.0
    governing_combo: str = ""


@dataclass
class ColumnComboDemand:
    combo_name: str
    pu_kn: float  # positive = compression
    mux_knm: float
    muy_knm: float
    vu_kn: float


@dataclass
class ColumnForceEnvelope:
    member_id: int
    pu_max_kn: float = 0.0
    pu_min_kn: float = 0.0
    mux_max_knm: float = 0.0
    muy_max_knm: float = 0.0
    vu_max_kn: float = 0.0
    governing_combo: str = ""
    demands: list[ColumnComboDemand] = field(default_factory=list)


@dataclass
class AnalysisResults:
    """Unified output of internal 3D DSM solver or parsed STAAD.Pro .anl file."""

    # Elastic displacements (mm) for primary seismic cases: case_key -> node_id -> (dx, dy, dz)
    seismic_displacements_mm: dict[str, dict[int, tuple[float, float, float]]]
    support_reactions: list[SupportReactionEnvelope]
    beam_envelopes: dict[int, BeamForceEnvelope]
    column_envelopes: dict[int, ColumnForceEnvelope]
    # Primary gravity equilibrium diagnostics (DL / LL / LR total vertical reactions in kN)
    primary_vertical_reactions_kn: dict[str, float] = field(default_factory=dict)
    primary_vertical_applied_kn: dict[str, float] = field(default_factory=dict)


def _rotation_matrix_3x3(
    na: AnalyticalNode,
    nb: AnalyticalNode,
    beta_deg: float = 0.0,
) -> tuple[np.ndarray, float]:
    """
    Build 3x3 local-from-global direction cosine matrix R (rows = ex, ey, ez)
    in STAAD Y-up global coordinate system.
    """
    dx = nb.x_m - na.x_m
    dy = nb.y_m - na.y_m
    dz = nb.z_m - na.z_m
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length <= 1e-9:
        raise ValueError(f"Zero-length member between nodes {na.node_id} and {nb.node_id}.")

    ex = np.array([dx / length, dy / length, dz / length], dtype=float)
    if abs(ex[1]) >= 0.999:
        # Vertical member (column): local y along -X (if upward) / +X (if downward), local z along +Z
        ey0 = np.array([-1.0 if ex[1] > 0 else 1.0, 0.0, 0.0], dtype=float)
        ez0 = np.cross(ex, ey0)
        ez0 /= np.linalg.norm(ez0)
    else:
        up = np.array([0.0, 1.0, 0.0], dtype=float)
        ez0 = np.cross(ex, up)
        ez0 /= np.linalg.norm(ez0)
        ey0 = np.cross(ez0, ex)
        ey0 /= np.linalg.norm(ey0)

    if abs(beta_deg) > 1e-6:
        rad = math.radians(beta_deg)
        cb, sb = math.cos(rad), math.sin(rad)
        ey = cb * ey0 + sb * ez0
        ez = -sb * ey0 + cb * ez0
    else:
        ey, ez = ey0, ez0

    return np.vstack([ex, ey, ez]), length


def _transform_12x12(r3: np.ndarray) -> np.ndarray:
    t = np.zeros((12, 12), dtype=float)
    for blk in range(4):
        s = blk * 3
        t[s : s + 3, s : s + 3] = r3
    return t


def _torsional_j(b_m: float, h_m: float) -> float:
    a = max(b_m, h_m)
    c = min(b_m, h_m)
    ratio = c / a if a > 1e-9 else 1.0
    return a * (c ** 3) * (1.0 / 3.0 - 0.21 * ratio * (1.0 - (ratio ** 4) / 12.0))


def _local_stiffness_12x12(
    e_kn_m2: float,
    g_kn_m2: float,
    area_m2: float,
    j_m4: float,
    iy_m4: float,
    iz_m4: float,
    length_m: float,
) -> np.ndarray:
    L = length_m
    L2 = L * L
    L3 = L2 * L
    k = np.zeros((12, 12), dtype=float)

    # Axial (0, 6)
    ea = e_kn_m2 * area_m2 / L
    k[0, 0] = k[6, 6] = ea
    k[0, 6] = k[6, 0] = -ea

    # Torsion (3, 9)
    gj = g_kn_m2 * j_m4 / L
    k[3, 3] = k[9, 9] = gj
    k[3, 9] = k[9, 3] = -gj

    # Bending in x-y plane about local z (DOFs 1, 5, 7, 11)
    eiz = e_kn_m2 * iz_m4
    k[1, 1] = k[7, 7] = 12.0 * eiz / L3
    k[1, 7] = k[7, 1] = -12.0 * eiz / L3
    k[1, 5] = k[5, 1] = k[1, 11] = k[11, 1] = 6.0 * eiz / L2
    k[5, 7] = k[7, 5] = k[7, 11] = k[11, 7] = -6.0 * eiz / L2
    k[5, 5] = k[11, 11] = 4.0 * eiz / L
    k[5, 11] = k[11, 5] = 2.0 * eiz / L

    # Bending in x-z plane about local y (DOFs 2, 4, 8, 10)
    eiy = e_kn_m2 * iy_m4
    k[2, 2] = k[8, 8] = 12.0 * eiy / L3
    k[2, 8] = k[8, 2] = -12.0 * eiy / L3
    k[2, 4] = k[4, 2] = k[2, 10] = k[10, 2] = -6.0 * eiy / L2
    k[4, 8] = k[8, 4] = k[8, 10] = k[10, 8] = 6.0 * eiy / L2
    k[4, 4] = k[10, 10] = 4.0 * eiy / L
    k[4, 10] = k[10, 4] = 2.0 * eiy / L

    return k


def _equivalent_nodal_loads_local(
    length_m: float,
    r3: np.ndarray,
    w1_gy: float,
    w2_gy: float,
    d1_m: float,
    d2_m: float,
) -> np.ndarray:
    """
    Compute exact consistent equivalent nodal load vector f_eq (12,) in local coordinates
    for a linear global-Y distributed load w(x) from w1_gy at d1_m to w2_gy at d2_m.
    Returns f_eq (applied to nodes); fixed-end reactions are f_fe = -f_eq.
    """
    L = length_m
    a = max(0.0, min(L, d1_m))
    b = max(a, min(L, d2_m))
    if b - a <= 1e-7:
        return np.zeros(12, dtype=float)

    ey_glob = r3[:, 1]  # global Y unit vector expressed in local [x, y, z]
    f_eq = np.zeros(12, dtype=float)
    jac = 0.5 * (b - a)
    mid = 0.5 * (a + b)

    for xi, wt in zip(_GL_XI, _GL_W):
        x = mid + jac * xi
        s = x / L
        t_seg = (x - a) / (b - a)
        w_gy = w1_gy + t_seg * (w2_gy - w1_gy)
        wx = w_gy * ey_glob[0]
        wy = w_gy * ey_glob[1]
        wz = w_gy * ey_glob[2]

        # Linear axial shape functions
        na1 = 1.0 - s
        na2 = s

        # Hermite cubic bending shape functions for x-y plane (about z)
        n1 = 1.0 - 3.0 * s * s + 2.0 * s * s * s
        n2 = L * (s - 2.0 * s * s + s * s * s)
        n3 = 3.0 * s * s - 2.0 * s * s * s
        n4 = L * (-s * s + s * s * s)

        dx_w = jac * wt
        f_eq[0] += na1 * wx * dx_w
        f_eq[6] += na2 * wx * dx_w

        f_eq[1] += n1 * wy * dx_w
        f_eq[5] += n2 * wy * dx_w
        f_eq[7] += n3 * wy * dx_w
        f_eq[11] += n4 * wy * dx_w

        # In x-z plane, positive rotation ry rotates +x toward -z -> sign flip on rotation shape terms
        f_eq[2] += n1 * wz * dx_w
        f_eq[4] += (-n2) * wz * dx_w
        f_eq[8] += n3 * wz * dx_w
        f_eq[10] += (-n4) * wz * dx_w

    return f_eq


def _condense_releases(
    k_loc: np.ndarray,
    f_fe_mat: np.ndarray,
    rel_start: bool,
    rel_end: bool,
) -> tuple[np.ndarray, np.ndarray, list[int], list[int], np.ndarray | None, np.ndarray | None]:
    """
    Condense out local rotational DOFs [4,5] (start) and/or [10,11] (end).
    f_fe_mat has shape (12, n_cases) representing fixed-end reactions (-f_eq).
    """
    released: list[int] = []
    if rel_start:
        released.extend([4, 5])
    if rel_end:
        released.extend([10, 11])
    if not released:
        return k_loc, f_fe_mat, [], list(range(12)), None, None

    kept = [i for i in range(12) if i not in released]
    k_kk = k_loc[np.ix_(kept, kept)]
    k_kr = k_loc[np.ix_(kept, released)]
    k_rk = k_loc[np.ix_(released, kept)]
    k_rr = k_loc[np.ix_(released, released)]

    k_rr_inv = np.linalg.inv(k_rr)
    k_cond_sub = k_kk - k_kr @ k_rr_inv @ k_rk
    f_cond_sub = f_fe_mat[kept, :] - k_kr @ (k_rr_inv @ f_fe_mat[released, :])

    k_out = np.zeros((12, 12), dtype=float)
    k_out[np.ix_(kept, kept)] = k_cond_sub
    f_out = np.zeros_like(f_fe_mat)
    f_out[kept, :] = f_cond_sub
    return k_out, f_out, released, kept, k_rr_inv, k_rk


def _max_positive_span_moment(
    length_m: float,
    v_start: float,
    m_start_hog: float,
    load_segs_wy: list[tuple[float, float, float, float]],
) -> float:
    """
    Evaluate bending moment diagram M(x) at interior stations to find maximum positive (sagging)
    moment on a beam.
    Sign convention:
      - v_start: upward local shear at start (positive -> increases sagging moment moving +x)
      - m_start_hog: hogging moment magnitude at start (m_start_hog = +f_loc[5], positive = hogging -> negative sagging)
      - load_segs_wy: list of (d1, d2, wy1, wy2) where wy < 0 is downward load.
    """
    max_pos = 0.0
    n_pts = 11
    for i in range(1, n_pts):
        x = length_m * (i / n_pts)
        m_sag = -m_start_hog + v_start * x
        for d1, d2, wy1, wy2 in load_segs_wy:
            if x <= d1 or d2 <= d1:
                continue
            x_hi = min(x, d2)
            seg_len = x_hi - d1
            t_hi = (x_hi - d1) / (d2 - d1)
            w_hi = wy1 + t_hi * (wy2 - wy1)
            # Integral of wy(u) * (x - u) du from d1 to x_hi (wy < 0 reduces sagging)
            # Split trapezoid into rectangle wy1 + triangle (w_hi - wy1)
            area_rect = wy1 * seg_len
            arm_rect = x - (d1 + 0.5 * seg_len)
            area_tri = 0.5 * (w_hi - wy1) * seg_len
            arm_tri = x - (d1 + (2.0 / 3.0) * seg_len)
            m_sag += area_rect * arm_rect + area_tri * arm_tri
        if m_sag > max_pos:
            max_pos = m_sag
    return max_pos


def solve_frame_dsm(project: ProjectState) -> AnalysisResults:
    """
    Run 3D linear elastic Direct Stiffness Method (DSM) analysis for all 11 primary
    NSCP 2015 load cases and linearly superpose all Phase 5 LRFD & ASD combinations.
    """
    model = project.structural_model
    sizing = project.member_sizing
    lg = project.load_generation

    if not model.nodes or not model.members:
        raise ValueError("Phase 3 structural geometry has not been generated yet.")
    if not sizing.sections or not sizing.member_assignments:
        raise ValueError("Phase 4 member sizing has not been run yet.")
    if lg.generated_at is None:
        raise ValueError("Phase 5 NSCP 2015 loads have not been generated yet.")

    nodes = sorted(model.nodes, key=lambda n: n.node_id)
    members = sorted(model.members, key=lambda m: m.member_id)
    node_by_id = {n.node_id: n for n in nodes}
    node_idx = {n.node_id: i for i, n in enumerate(nodes)}
    sec_by_id = {s.section_id: s for s in sizing.sections}
    asgn_by_mid = {a.member_id: a for a in sizing.member_assignments}

    mat = sizing.material
    e_mod = mat.ec_kn_m2
    g_mod = e_mod / (2.0 * (1.0 + mat.poisson))
    density = mat.density_kn_m3
    use_cracked = sizing.options.include_cracked_modifiers

    n_nodes = len(nodes)
    n_dof = n_nodes * 6
    n_prim = len(PRIMARY_CASES)

    K_glob = np.zeros((n_dof, n_dof), dtype=float)
    F_glob = np.zeros((n_dof, n_prim), dtype=float)

    # Index distributed beam loads by member_id and load_case
    loads_by_mid: dict[int, list[BeamDistributedLoad]] = {}
    for bld in lg.beam_loads:
        loads_by_mid.setdefault(bld.member_id, []).append(bld)

    applied_vert = {"DL": 0.0, "LL": 0.0, "LR": 0.0}

    # Cache element data for force recovery
    elem_cache: list[dict] = []

    for m in members:
        na = node_by_id[m.start_node]
        nb = node_by_id[m.end_node]
        asgn = asgn_by_mid.get(m.member_id)
        sec = sec_by_id.get(asgn.section_id) if asgn else None
        if sec is None:
            raise ValueError(f"Member {m.member_id} has no assigned section in Phase 4.")

        b_m = sec.width_mm / 1000.0
        h_m = sec.depth_mm / 1000.0
        area = b_m * h_m
        iz = (b_m * (h_m ** 3) / 12.0) * (sec.cracked_iz if use_cracked else 1.0)
        iy = (h_m * (b_m ** 3) / 12.0) * (sec.cracked_iy if use_cracked else 1.0)
        j_val = _torsional_j(b_m, h_m) * (sec.cracked_ix if use_cracked else 1.0)

        r3, L = _rotation_matrix_3x3(na, nb, asgn.beta_angle_deg)
        T = _transform_12x12(r3)
        k_loc = _local_stiffness_12x12(e_mod, g_mod, area, j_val, iy, iz, L)

        # Build local fixed-end reaction matrix f_fe_raw (12, n_prim) and track local wy segments for midspan M+
        f_fe_raw = np.zeros((12, n_prim), dtype=float)
        wy_segs_by_case: dict[str, list[tuple[float, float, float, float]]] = {
            "DL": [],
            "LL": [],
            "LR": [],
        }

        # Self-weight in DL (global Y downward)
        w_sw = -area * density
        f_eq_sw = _equivalent_nodal_loads_local(L, r3, w_sw, w_sw, 0.0, L)
        f_fe_raw[:, PRIMARY_IDX["DL"]] -= f_eq_sw
        applied_vert["DL"] += abs(w_sw) * L
        ey_y = r3[1, 1]
        if abs(ey_y) > 0.1:
            wy_segs_by_case["DL"].append((0.0, L, w_sw * ey_y, w_sw * ey_y))

        # Distributed member loads from Phase 5
        for bld in loads_by_mid.get(m.member_id, []):
            c_idx = PRIMARY_IDX[bld.load_case]
            if bld.load_shape == "UNI":
                d1 = 0.0 if (abs(bld.d1_m) <= 1e-4 and abs(bld.d2_m) <= 1e-4) else bld.d1_m
                d2 = L if (abs(bld.d1_m) <= 1e-4 and abs(bld.d2_m) <= 1e-4) else min(L, bld.d2_m)
                w1, w2 = bld.w1_kn_m, bld.w1_kn_m
            else:
                d1 = max(0.0, min(L, bld.d1_m))
                d2 = max(d1, min(L, bld.d2_m))
                w1, w2 = bld.w1_kn_m, bld.w2_kn_m

            f_eq = _equivalent_nodal_loads_local(L, r3, w1, w2, d1, d2)
            f_fe_raw[:, c_idx] -= f_eq
            applied_vert[bld.load_case] += 0.5 * (abs(w1) + abs(w2)) * max(d2 - d1, 0.0)
            if abs(ey_y) > 0.1 and d2 > d1:
                wy_segs_by_case[bld.load_case].append((d1, d2, w1 * ey_y, w2 * ey_y))

        k_eff, f_fe_eff, released, kept, k_rr_inv, k_rk = _condense_releases(
            k_loc,
            f_fe_raw,
            asgn.release_start_moment,
            asgn.release_end_moment,
        )

        # Assemble global stiffness and global equivalent nodal loads (-f_fe_eff)
        ia = node_idx[m.start_node] * 6
        ib = node_idx[m.end_node] * 6
        dofs = list(range(ia, ia + 6)) + list(range(ib, ib + 6))

        k_g = T.T @ k_eff @ T
        f_eq_g = T.T @ (-f_fe_eff)

        K_glob[np.ix_(dofs, dofs)] += k_g
        F_glob[dofs, :] += f_eq_g

        elem_cache.append(
            {
                "member": m,
                "length": L,
                "dofs": dofs,
                "T": T,
                "k_loc": k_loc,
                "f_fe_raw": f_fe_raw,
                "released": released,
                "kept": kept,
                "k_rr_inv": k_rr_inv,
                "k_rk": k_rk,
                "wy_segs": wy_segs_by_case,
            }
        )

    # Apply Nodal Seismic Story Forces (EX+, EX-, EZ+, EZ-)
    if lg.seismic_summary is not None:
        for st in lg.seismic_summary.stories:
            lvl_nodes = [n for n in nodes if abs(n.y_m - st.elevation_m) <= 0.15]
            free_nodes = [n for n in lvl_nodes if not n.is_support and n.support_type == "NONE"]
            target_nodes = free_nodes if free_nodes else lvl_nodes
            if not target_nodes:
                continue
            fx_per = st.fx_kn / len(target_nodes)
            fz_per = st.fz_kn / len(target_nodes)
            for n in target_nodes:
                base_dof = node_idx[n.node_id] * 6
                F_glob[base_dof + 0, PRIMARY_IDX["EX+"]] += fx_per
                F_glob[base_dof + 0, PRIMARY_IDX["EX-"]] -= fx_per
                F_glob[base_dof + 2, PRIMARY_IDX["EZ+"]] += fz_per
                F_glob[base_dof + 2, PRIMARY_IDX["EZ-"]] -= fz_per

    # Apply Nodal Wind Story Forces (WX+, WX-, WZ+, WZ-) on windward face nodes
    if lg.wind_summary is not None:
        for st in lg.wind_summary.stories:
            lvl_nodes = [n for n in nodes if abs(n.y_m - st.elevation_m) <= 0.15]
            if not lvl_nodes:
                continue
            min_x = min(n.x_m for n in lvl_nodes)
            max_x = max(n.x_m for n in lvl_nodes)
            min_z = min(n.z_m for n in lvl_nodes)
            max_z = max(n.z_m for n in lvl_nodes)

            for key, axis_off, sign, face in (
                ("WX+", 0, 1.0, [n for n in lvl_nodes if abs(n.x_m - min_x) <= 0.25]),
                ("WX-", 0, -1.0, [n for n in lvl_nodes if abs(n.x_m - max_x) <= 0.25]),
                ("WZ+", 2, 1.0, [n for n in lvl_nodes if abs(n.z_m - min_z) <= 0.25]),
                ("WZ-", 2, -1.0, [n for n in lvl_nodes if abs(n.z_m - max_z) <= 0.25]),
            ):
                targets = face if face else lvl_nodes
                total_f = (st.fx_kn if axis_off == 0 else st.fz_kn) * sign
                f_per = total_f / len(targets)
                for n in targets:
                    F_glob[node_idx[n.node_id] * 6 + axis_off, PRIMARY_IDX[key]] += f_per

    # Identify restrained vs free DOFs
    restrained: list[int] = []
    support_nodes: list[AnalyticalNode] = []
    for n in nodes:
        if n.is_support and n.support_type != "NONE":
            support_nodes.append(n)
            b = node_idx[n.node_id] * 6
            if n.support_type == "FIXED":
                restrained.extend(range(b, b + 6))
            elif n.support_type == "PINNED":
                restrained.extend([b, b + 1, b + 2])

    if not restrained:
        raise ValueError("Model has no supports defined. Assign supports in Module 03 before running analysis.")

    restrained_set = set(restrained)
    free_dofs = [i for i in range(n_dof) if i not in restrained_set]

    K_ff = K_glob[np.ix_(free_dofs, free_dofs)].copy()
    F_f = F_glob[free_dofs, :]

    # Add tiny rotational stabilization on near-zero diagonal entries (e.g., all-pinned beam joints)
    diag = np.diag(K_ff)
    max_diag = float(np.max(np.abs(diag))) if len(diag) else 1.0
    eps = max(1e-6, max_diag * 1e-12)
    for idx_i, d_val in enumerate(diag):
        if abs(d_val) < eps:
            K_ff[idx_i, idx_i] += eps

    try:
        U_f = np.linalg.solve(K_ff, F_f)
    except np.linalg.LinAlgError:
        U_f, *_ = np.linalg.lstsq(K_ff, F_f, rcond=None)

    U_glob = np.zeros((n_dof, n_prim), dtype=float)
    U_glob[free_dofs, :] = U_f

    # Support reactions: R = K * U - F
    R_glob = K_glob @ U_glob - F_glob

    # Primary vertical equilibrium diagnostics
    prim_vert_rxn: dict[str, float] = {}
    for key in ("DL", "LL", "LR"):
        c_idx = PRIMARY_IDX[key]
        total_fy = sum(float(R_glob[node_idx[n.node_id] * 6 + 1, c_idx]) for n in support_nodes)
        prim_vert_rxn[key] = round(total_fy, 3)

    # Elastic displacements (mm) for EX+ and EZ+
    seismic_disps: dict[str, dict[int, tuple[float, float, float]]] = {"EX+": {}, "EZ+": {}}
    for key in ("EX+", "EZ+"):
        c_idx = PRIMARY_IDX[key]
        for n in nodes:
            b = node_idx[n.node_id] * 6
            seismic_disps[key][n.node_id] = (
                float(U_glob[b + 0, c_idx]) * 1000.0,
                float(U_glob[b + 1, c_idx]) * 1000.0,
                float(U_glob[b + 2, c_idx]) * 1000.0,
            )

    # Recover member local end forces for all 11 primary cases: shape (12, 11) per member
    member_prim_forces: dict[int, np.ndarray] = {}
    for ec in elem_cache:
        u_g = U_glob[ec["dofs"], :]
        u_loc = ec["T"] @ u_g
        if ec["released"]:
            kept = ec["kept"]
            rel = ec["released"]
            u_loc_full = u_loc.copy()
            u_loc_full[rel, :] = -ec["k_rr_inv"] @ (
                ec["f_fe_raw"][rel, :] + ec["k_rk"] @ u_loc[kept, :]
            )
            f_loc = ec["k_loc"] @ u_loc_full + ec["f_fe_raw"]
        else:
            f_loc = ec["k_loc"] @ u_loc + ec["f_fe_raw"]
        member_prim_forces[ec["member"].member_id] = f_loc

    combos = lg.combinations
    lrfd_combos = [c for c in combos if c.design_basis == "LRFD"]
    asd_combos = [c for c in combos if c.design_basis == "ASD"]

    def _combo_vec(factors: dict[str, float]) -> np.ndarray:
        v = np.zeros(n_prim, dtype=float)
        for k, fac in factors.items():
            if k in PRIMARY_IDX:
                v[PRIMARY_IDX[k]] = fac
        return v

    # --- Support Reaction Envelopes ---
    support_envelopes: list[SupportReactionEnvelope] = []
    for n in support_nodes:
        b = node_idx[n.node_id] * 6
        r_node_prim = R_glob[b : b + 6, :]  # (6, 11)

        max_asd_fy = -1e18
        min_asd_fy = 1e18
        gov_asd = ""
        for c in asd_combos or combos:
            vec = _combo_vec(c.factors)
            r_c = r_node_prim @ vec
            fy = float(r_c[1])
            if fy > max_asd_fy:
                max_asd_fy = fy
                gov_asd = c.name
            if fy < min_asd_fy:
                min_asd_fy = fy

        max_lrfd_pu = 0.0
        max_lrfd_mx = 0.0
        max_lrfd_mz = 0.0
        max_lrfd_v = 0.0
        gov_lrfd = ""
        for c in lrfd_combos or combos:
            vec = _combo_vec(c.factors)
            r_c = r_node_prim @ vec
            pu = float(r_c[1])
            if pu > max_lrfd_pu:
                max_lrfd_pu = pu
                gov_lrfd = c.name
            max_lrfd_mx = max(max_lrfd_mx, abs(float(r_c[3])))
            max_lrfd_mz = max(max_lrfd_mz, abs(float(r_c[5])))
            v_res = math.hypot(float(r_c[0]), float(r_c[2]))
            max_lrfd_v = max(max_lrfd_v, v_res)

        if max_asd_fy < -1e17:
            max_asd_fy = 0.0
            min_asd_fy = 0.0

        support_envelopes.append(
            SupportReactionEnvelope(
                node_id=n.node_id,
                grid_ref=n.grid_ref,
                floor_level=n.floor_level,
                max_asd_fy_kn=round(max_asd_fy, 2),
                min_asd_fy_kn=round(min_asd_fy, 2),
                max_lrfd_pu_kn=round(max_lrfd_pu, 2),
                max_lrfd_mux_knm=round(max_lrfd_mx, 2),
                max_lrfd_muz_knm=round(max_lrfd_mz, 2),
                max_lrfd_v_kn=round(max_lrfd_v, 2),
                governing_asd_combo=gov_asd,
                governing_lrfd_combo=gov_lrfd,
            )
        )

    # --- Member Force Envelopes (LRFD) ---
    beam_envelopes: dict[int, BeamForceEnvelope] = {}
    column_envelopes: dict[int, ColumnForceEnvelope] = {}
    design_combos = lrfd_combos if lrfd_combos else combos

    for ec in elem_cache:
        m: AnalyticalMember = ec["member"]
        f_prim = member_prim_forces[m.member_id]  # (12, 11)
        if m.member_type == "column":
            env = ColumnForceEnvelope(member_id=m.member_id, pu_min_kn=1e18)
            best_score = -1.0
            for c in design_combos:
                vec = _combo_vec(c.factors)
                fc = f_prim @ vec
                # Axial compression: start node local +fx (fc[0]) or -fc[6]
                pu = 0.5 * (float(fc[0]) - float(fc[6]))
                mux = max(abs(float(fc[5])), abs(float(fc[11])))
                muy = max(abs(float(fc[4])), abs(float(fc[10])))
                vu = max(
                    math.hypot(float(fc[1]), float(fc[2])),
                    math.hypot(float(fc[7]), float(fc[8])),
                )
                env.demands.append(
                    ColumnComboDemand(
                        combo_name=c.name,
                        pu_kn=round(pu, 3),
                        mux_knm=round(mux, 3),
                        muy_knm=round(muy, 3),
                        vu_kn=round(vu, 3),
                    )
                )
                if pu > env.pu_max_kn:
                    env.pu_max_kn = pu
                if pu < env.pu_min_kn:
                    env.pu_min_kn = pu
                if mux > env.mux_max_knm:
                    env.mux_max_knm = mux
                if muy > env.muy_max_knm:
                    env.muy_max_knm = muy
                if vu > env.vu_max_kn:
                    env.vu_max_kn = vu
                score = abs(pu) * 0.05 + mux + muy
                if score > best_score:
                    best_score = score
                    env.governing_combo = c.name
            if env.pu_min_kn > 1e17:
                env.pu_min_kn = 0.0
            column_envelopes[m.member_id] = env
        else:
            env_b = BeamForceEnvelope(member_id=m.member_id)
            best_m = -1.0
            for c in design_combos:
                vec = _combo_vec(c.factors)
                fc = f_prim @ vec
                # Start end hogging moment = max(0, +fc[5]) (or abs end negative moment under reversal)
                # Under seismic reversal, either sign at ends can cause top tension: take abs for hogging envelope
                # while keeping positive midspan sagging exact!
                mu_start = abs(float(fc[5]))
                mu_end = abs(float(fc[11]))
                vu = max(abs(float(fc[1])), abs(float(fc[7])))

                # Combine scaled wy segments for exact midspan sagging moment
                scaled_segs: list[tuple[float, float, float, float]] = []
                for g_key in ("DL", "LL", "LR"):
                    fac = c.factors.get(g_key, 0.0)
                    if abs(fac) > 1e-6:
                        for d1, d2, wy1, wy2 in ec["wy_segs"][g_key]:
                            scaled_segs.append((d1, d2, wy1 * fac, wy2 * fac))

                mu_pos = _max_positive_span_moment(
                    length_m=ec["length"],
                    v_start=float(fc[1]),
                    m_start_hog=float(fc[5]),
                    load_segs_wy=scaled_segs,
                )

                if mu_start > env_b.mu_neg_start_knm:
                    env_b.mu_neg_start_knm = mu_start
                if mu_end > env_b.mu_neg_end_knm:
                    env_b.mu_neg_end_knm = mu_end
                if mu_pos > env_b.mu_pos_mid_knm:
                    env_b.mu_pos_mid_knm = mu_pos
                if vu > env_b.vu_max_kn:
                    env_b.vu_max_kn = vu

                peak_m = max(mu_start, mu_end, mu_pos)
                if peak_m > best_m:
                    best_m = peak_m
                    env_b.governing_combo = c.name

            beam_envelopes[m.member_id] = env_b

    return AnalysisResults(
        seismic_displacements_mm=seismic_disps,
        support_reactions=support_envelopes,
        beam_envelopes=beam_envelopes,
        column_envelopes=column_envelopes,
        primary_vertical_reactions_kn=prim_vert_rxn,
        primary_vertical_applied_kn={k: round(v, 3) for k, v in applied_vert.items()},
    )
