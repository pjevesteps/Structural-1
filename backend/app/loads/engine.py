from datetime import datetime, timezone
import math

from ..models import (
    AnalyticalMember,
    AnalyticalNode,
    BeamDistributedLoad,
    FloorGravityLoad,
    LoadCombinationDef,
    LoadGenerationOptions,
    LoadGenerationState,
    ProjectState,
    SeismicParameters,
    SeismicSummary,
    SlabPanel,
    StorySeismicResult,
    StoryWindResult,
    WindParameters,
    WindSummary,
)
from .validator import validate_loads

OCCUPANCY_PRESETS: dict[str, dict] = {
    "residential": {
        "sdl_finish_kpa": 1.20,
        "sdl_ceiling_mep_kpa": 0.50,
        "sdl_partitions_kpa": 1.00,
        "live_load_kpa": 1.90,
        "is_roof": False,
        "exterior_wall_line_kn_m": 7.50,
        "interior_wall_line_kn_m": 4.50,
        "apply_exterior_wall_perimeter": True,
        "apply_interior_wall_beams": False,
    },
    "parking": {
        "sdl_finish_kpa": 1.00,
        "sdl_ceiling_mep_kpa": 0.50,
        "sdl_partitions_kpa": 0.00,
        "live_load_kpa": 2.40,
        "is_roof": False,
        "exterior_wall_line_kn_m": 7.50,
        "interior_wall_line_kn_m": 4.50,
        "apply_exterior_wall_perimeter": True,
        "apply_interior_wall_beams": False,
    },
    "corridor_stairs": {
        "sdl_finish_kpa": 1.20,
        "sdl_ceiling_mep_kpa": 0.50,
        "sdl_partitions_kpa": 1.00,
        "live_load_kpa": 4.80,
        "is_roof": False,
        "exterior_wall_line_kn_m": 7.50,
        "interior_wall_line_kn_m": 4.50,
        "apply_exterior_wall_perimeter": True,
        "apply_interior_wall_beams": False,
    },
    "roof_deck": {
        "sdl_finish_kpa": 1.20,
        "sdl_ceiling_mep_kpa": 0.50,
        "sdl_partitions_kpa": 0.00,
        "live_load_kpa": 1.90,
        "is_roof": False,
        "exterior_wall_line_kn_m": 3.00,
        "interior_wall_line_kn_m": 0.00,
        "apply_exterior_wall_perimeter": True,
        "apply_interior_wall_beams": False,
    },
    "roof_non_accessible": {
        "sdl_finish_kpa": 0.50,
        "sdl_ceiling_mep_kpa": 0.50,
        "sdl_partitions_kpa": 0.00,
        "live_load_kpa": 1.00,
        "is_roof": True,
        "exterior_wall_line_kn_m": 0.00,
        "interior_wall_line_kn_m": 0.00,
        "apply_exterior_wall_perimeter": False,
        "apply_interior_wall_beams": False,
    },
    "commercial": {
        "sdl_finish_kpa": 1.20,
        "sdl_ceiling_mep_kpa": 0.70,
        "sdl_partitions_kpa": 1.00,
        "live_load_kpa": 2.40,
        "is_roof": False,
        "exterior_wall_line_kn_m": 7.50,
        "interior_wall_line_kn_m": 4.50,
        "apply_exterior_wall_perimeter": True,
        "apply_interior_wall_beams": False,
    },
}

# NSCP 2015 Table 208-5 (Near-Source Factor Na)
_NA_TABLE: dict[str, list[tuple[float, float]]] = {
    "A": [(2.0, 1.5), (5.0, 1.2), (10.0, 1.0)],
    "B": [(2.0, 1.3), (5.0, 1.0), (10.0, 1.0)],
    "C": [(2.0, 1.0), (5.0, 1.0), (10.0, 1.0)],
}

# NSCP 2015 Table 208-6 (Near-Source Factor Nv)
_NV_TABLE: dict[str, list[tuple[float, float]]] = {
    "A": [(2.0, 2.0), (5.0, 1.6), (10.0, 1.2), (15.0, 1.0)],
    "B": [(2.0, 1.6), (5.0, 1.2), (10.0, 1.0), (15.0, 1.0)],
    "C": [(2.0, 1.0), (5.0, 1.0), (10.0, 1.0), (15.0, 1.0)],
}

# NSCP 2015 Table 208-7 (Ca)
_CA_ZONE2: dict[str, float] = {
    "SA": 0.16,
    "SB": 0.20,
    "SC": 0.24,
    "SD": 0.28,
    "SE": 0.34,
}
_CA_ZONE4_BASE: dict[str, float] = {
    "SA": 0.32,
    "SB": 0.40,
    "SC": 0.40,
    "SD": 0.44,
    "SE": 0.36,
}

# NSCP 2015 Table 208-8 (Cv)
_CV_ZONE2: dict[str, float] = {
    "SA": 0.16,
    "SB": 0.20,
    "SC": 0.32,
    "SD": 0.40,
    "SE": 0.64,
}
_CV_ZONE4_BASE: dict[str, float] = {
    "SA": 0.32,
    "SB": 0.40,
    "SC": 0.56,
    "SD": 0.64,
    "SE": 0.96,
}

# NSCP 2015 Table 207B.3-1 exposure constants: (alpha, zg_m, z_min_m)
_WIND_EXPOSURE: dict[str, tuple[float, float, float]] = {
    "B": (7.0, 365.76, 4.5),
    "C": (9.5, 274.32, 4.5),
    "D": (11.5, 213.36, 4.5),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _interp_piecewise(x: float, pts: list[tuple[float, float]]) -> float:
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            t = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
            return y0 + t * (y1 - y0)
    return pts[-1][1]


def compute_seismic_coefficients(params: SeismicParameters) -> tuple[float, float, float, float, float]:
    """Return (z_factor, na, nv, ca, cv) per NSCP 2015 Tables 208-3 through 208-8."""
    z_factor = 0.40 if params.zone == 4 else 0.20
    if params.zone == 2:
        na = 1.0
        nv = 1.0
        ca = _CA_ZONE2[params.soil_profile]
        cv = _CV_ZONE2[params.soil_profile]
    else:
        na = round(_interp_piecewise(params.fault_distance_km, _NA_TABLE[params.seismic_source_type]), 4)
        nv = round(_interp_piecewise(params.fault_distance_km, _NV_TABLE[params.seismic_source_type]), 4)
        ca = round(_CA_ZONE4_BASE[params.soil_profile] * na, 4)
        cv = round(_CV_ZONE4_BASE[params.soil_profile] * nv, 4)
    return z_factor, na, nv, ca, cv


def compute_wind_kz(z_m: float, exposure: str) -> float:
    """Compute velocity pressure exposure coefficient Kz per NSCP 2015 Table 207B.3-1."""
    alpha, zg_m, z_min_m = _WIND_EXPOSURE.get(exposure, _WIND_EXPOSURE["B"])
    z_eff = max(min(z_m, zg_m), z_min_m)
    return round(2.01 * ((z_eff / zg_m) ** (2.0 / alpha)), 4)


def beam_load_total_force_kn(load: BeamDistributedLoad, member_length_m: float) -> float:
    """Integrate downward force magnitude (kN) for a UNI or TRAP distributed beam load."""
    if load.load_shape == "UNI":
        span = (load.d2_m - load.d1_m) if load.d2_m > load.d1_m else member_length_m
        return abs(load.w1_kn_m) * max(span, 0.0)
    span = max(load.d2_m - load.d1_m, 0.0)
    return 0.5 * (abs(load.w1_kn_m) + abs(load.w2_kn_m)) * span


def _is_roof_deck_label(label: str) -> bool:
    up = label.upper()
    return "ROOF DECK" in up or "ROOFDECK" in up or up.strip() in ("RD", "R-DECK")


def _is_roof_label(label: str) -> bool:
    up = label.upper()
    if _is_roof_deck_label(label):
        return False
    return "ROOF" in up or up.strip() in ("RF", "R")


def _is_parking_label(label: str) -> bool:
    up = label.upper()
    return any(tok in up for tok in ("PARK", "GARAGE", "BASEMENT", "BSMT", "B1"))


def _default_floor_load_for_level(level_id: str) -> FloorGravityLoad:
    if _is_roof_deck_label(level_id):
        preset = "roof_deck"
    elif _is_roof_label(level_id):
        preset = "roof_non_accessible"
    elif _is_parking_label(level_id):
        preset = "parking"
    else:
        preset = "residential"
    cfg = OCCUPANCY_PRESETS[preset]
    return FloorGravityLoad(
        floor_level=level_id,
        occupancy_preset=preset,
        **cfg,
    )


def seed_default_floor_loads(project: ProjectState) -> list[FloorGravityLoad]:
    """
    Auto-initialize FloorGravityLoad rows from project.levels.
    Skips the lowest base foundation level if it has no elevated slab framing.
    """
    levels = sorted(project.levels, key=lambda lv: lv.elevation_m)
    if not levels:
        return []

    model = project.structural_model
    slab_levels = {p.floor_level for p in model.slab_panels if p.slab_action != "void"}
    primary_beam_levels = {
        m.floor_level
        for m in model.members
        if m.member_type in ("primary_beam", "secondary_beam", "trimmer_beam")
    }

    seeded: list[FloorGravityLoad] = []
    for idx, lv in enumerate(levels):
        if idx == 0 and lv.id not in slab_levels and lv.id not in primary_beam_levels:
            continue
        seeded.append(_default_floor_load_for_level(lv.id))

    # If everything somehow got filtered out, include levels above base (or all levels)
    if not seeded:
        target = levels[1:] if len(levels) > 1 else levels
        seeded = [_default_floor_load_for_level(lv.id) for lv in target]
    return seeded


def _convex_hull_2d(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Monotonic chain 2D convex hull of unique points (rounded to 1 cm)."""
    pts = sorted({(round(x, 2), round(z, 2)) for x, z in points})
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) < -1e-6:
            lower.pop()
        lower.append(p)

    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) < -1e-6:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def _point_on_segment_2d(
    px: float, pz: float, ax: float, az: float, bx: float, bz: float, tol: float = 0.20
) -> bool:
    vx, vz = bx - ax, bz - az
    len_sq = vx * vx + vz * vz
    if len_sq < 1e-8:
        return math.hypot(px - ax, pz - az) <= tol
    t = max(0.0, min(1.0, ((px - ax) * vx + (pz - az) * vz) / len_sq))
    proj_x, proj_z = ax + t * vx, az + t * vz
    return math.hypot(px - proj_x, pz - proj_z) <= tol


def _identify_perimeter_beams(
    beams: list[AnalyticalMember],
    node_by_id: dict[int, AnalyticalNode],
    tol: float = 0.25,
) -> set[int]:
    """Return member_ids of perimeter primary beams on a floor (bounding box + convex hull)."""
    if not beams:
        return set()

    pts: list[tuple[float, float]] = []
    for b in beams:
        na = node_by_id.get(b.start_node)
        nb = node_by_id.get(b.end_node)
        if na and nb:
            pts.append((na.x_m, na.z_m))
            pts.append((nb.x_m, nb.z_m))
    if not pts:
        return set()

    min_x = min(p[0] for p in pts)
    max_x = max(p[0] for p in pts)
    min_z = min(p[1] for p in pts)
    max_z = max(p[1] for p in pts)
    hull = _convex_hull_2d(pts)
    hull_edges = list(zip(hull, hull[1:] + hull[:1])) if len(hull) >= 3 else []

    perimeter_ids: set[int] = set()
    for b in beams:
        na = node_by_id.get(b.start_node)
        nb = node_by_id.get(b.end_node)
        if not na or not nb:
            continue
        on_bbox = (
            (abs(na.x_m - min_x) <= tol and abs(nb.x_m - min_x) <= tol)
            or (abs(na.x_m - max_x) <= tol and abs(nb.x_m - max_x) <= tol)
            or (abs(na.z_m - min_z) <= tol and abs(nb.z_m - min_z) <= tol)
            or (abs(na.z_m - max_z) <= tol and abs(nb.z_m - max_z) <= tol)
        )
        if on_bbox:
            perimeter_ids.add(b.member_id)
            continue
        for (hx0, hz0), (hx1, hz1) in hull_edges:
            if _point_on_segment_2d(na.x_m, na.z_m, hx0, hz0, hx1, hz1, tol) and _point_on_segment_2d(
                nb.x_m, nb.z_m, hx0, hz0, hx1, hz1, tol
            ):
                perimeter_ids.add(b.member_id)
                break
    return perimeter_ids


def _match_edge_beams(
    beams: list[AnalyticalMember],
    node_by_id: dict[int, AnalyticalNode],
    axis: str,
    fixed_coord: float,
    span_start: float,
    span_end: float,
    tol: float = 0.15,
) -> list[tuple[AnalyticalMember, float, float, float, float, bool]]:
    """
    Find beams lying along an edge of a rectangular slab panel.
    Returns list of (member, beam_d1, beam_d2, edge_u1, edge_u2, reversed_dir) where:
      - beam_d1..beam_d2 are distances along the member from start_node -> end_node
      - edge_u1..edge_u2 are coordinates along the panel edge [0, edge_len]
      - reversed_dir is True when beam start->end runs opposite to increasing edge u
    """
    matched: list[tuple[AnalyticalMember, float, float, float, float, bool]] = []
    for m in beams:
        na = node_by_id.get(m.start_node)
        nb = node_by_id.get(m.end_node)
        if not na or not nb or m.length_m <= 1e-4:
            continue
        if axis == "x":
            if abs(na.z_m - fixed_coord) > tol or abs(nb.z_m - fixed_coord) > tol:
                continue
            c_start, c_end = na.x_m, nb.x_m
        else:
            if abs(na.x_m - fixed_coord) > tol or abs(nb.x_m - fixed_coord) > tol:
                continue
            c_start, c_end = na.z_m, nb.z_m

        lo = max(min(c_start, c_end), span_start)
        hi = min(max(c_start, c_end), span_end)
        if hi - lo <= 0.05:
            continue

        reversed_dir = c_end < c_start
        if not reversed_dir:
            d1 = max(0.0, lo - c_start)
            d2 = min(m.length_m, hi - c_start)
        else:
            d1 = max(0.0, c_start - hi)
            d2 = min(m.length_m, c_start - lo)
        u1 = lo - span_start
        u2 = hi - span_start

        if d2 - d1 > 0.05:
            matched.append((m, d1, d2, u1, u2, reversed_dir))
    return matched


def _staad_member_length_3dp(
    m: AnalyticalMember,
    node_by_id: dict[int, AnalyticalNode],
) -> float:
    """Compute exact Euclidean member length from 3-decimal STAAD JOINT COORDINATES."""
    na = node_by_id.get(m.start_node)
    nb = node_by_id.get(m.end_node)
    if na is None or nb is None:
        return m.length_m
    dx = round(nb.x_m, 3) - round(na.x_m, 3)
    dy = round(nb.y_m, 3) - round(na.y_m, 3)
    dz = round(nb.z_m, 3) - round(na.z_m, 3)
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _clamp_segment_to_member(d1_m: float, d2_m: float, l_actual: float) -> tuple[float, float] | None:
    """
    Clamp distributed load segment [d1_m, d2_m] strictly inside STAAD 3-decimal length L_actual.
    Returns None if the clamped segment is degenerate (< 0.005 m).
    """
    if l_actual <= 0.008:
        return None
    max_d2 = math.floor((l_actual - 0.001) * 1000.0) / 1000.0
    c_d1 = max(0.0, min(round(d1_m, 3), max(0.0, round(l_actual - 0.002, 3))))
    c_d2 = min(round(d2_m, 3), max_d2)
    if c_d2 - c_d1 < 0.005:
        return None
    return round(c_d1, 3), round(c_d2, 3)


def _emit_edge_load_segments(
    edge_matches: list[tuple[AnalyticalMember, float, float, float, float, bool]],
    edge_len: float,
    short_span: float,
    q_kpa: float,
    load_case: str,
    source: str,
    is_two_way: bool,
    is_short_edge: bool,
    out_loads: list[BeamDistributedLoad],
    node_by_id: dict[int, AnalyticalNode] | None = None,
) -> None:
    """
    Emit statically exact UNI or TRAP BeamDistributedLoad segments along matched beams
    for a single panel edge, clamping distances strictly inside 3-decimal STAAD length.
    """
    if not edge_matches or q_kpa <= 1e-6 or edge_len <= 1e-4:
        return

    w_peak = q_kpa * short_span * 0.5
    half_s = short_span * 0.5

    if not is_two_way:
        for m, d1, d2, _u1, _u2, _rev in edge_matches:
            l_actual = _staad_member_length_3dp(m, node_by_id) if node_by_id else m.length_m
            is_full = abs(d1) <= 0.02 and abs(d2 - l_actual) <= 0.02
            if is_full:
                c_d1, c_d2 = 0.0, 0.0
            else:
                clamped = _clamp_segment_to_member(d1, d2, l_actual)
                if clamped is None:
                    continue
                c_d1, c_d2 = clamped
            out_loads.append(
                BeamDistributedLoad(
                    member_id=m.member_id,
                    load_case=load_case,
                    load_shape="UNI",
                    w1_kn_m=round(-w_peak, 4),
                    w2_kn_m=0.0,
                    d1_m=c_d1,
                    d2_m=c_d2,
                    source=source,
                )
            )
        return

    if is_short_edge or edge_len - short_span <= 1e-3:
        mid = edge_len * 0.5
        profile = [
            (0.0, mid, 0.0, -w_peak),
            (mid, edge_len, -w_peak, 0.0),
        ]
    else:
        profile = [
            (0.0, half_s, 0.0, -w_peak),
            (half_s, edge_len - half_s, -w_peak, -w_peak),
            (edge_len - half_s, edge_len, -w_peak, 0.0),
        ]

    for m, d1, d2, u1, u2, reversed_dir in edge_matches:
        edge_span = u2 - u1
        if edge_span <= 1e-4:
            continue
        l_actual = _staad_member_length_3dp(m, node_by_id) if node_by_id else m.length_m
        for seg_u0, seg_u1, seg_w0, seg_w1 in profile:
            ov0 = max(u1, seg_u0)
            ov1 = min(u2, seg_u1)
            if ov1 - ov0 <= 1e-3:
                continue
            seg_len = seg_u1 - seg_u0
            wa = seg_w0 + ((ov0 - seg_u0) / seg_len) * (seg_w1 - seg_w0)
            wb = seg_w0 + ((ov1 - seg_u0) / seg_len) * (seg_w1 - seg_w0)
            frac0 = (ov0 - u1) / edge_span
            frac1 = (ov1 - u1) / edge_span
            if not reversed_dir:
                bd0 = d1 + frac0 * (d2 - d1)
                bd1 = d1 + frac1 * (d2 - d1)
                w_start, w_end = wa, wb
            else:
                bd0 = d2 - frac1 * (d2 - d1)
                bd1 = d2 - frac0 * (d2 - d1)
                w_start, w_end = wb, wa
            clamped = _clamp_segment_to_member(bd0, bd1, l_actual)
            if clamped is None:
                continue
            c_d1, c_d2 = clamped
            out_loads.append(
                BeamDistributedLoad(
                    member_id=m.member_id,
                    load_case=load_case,
                    load_shape="TRAP",
                    w1_kn_m=round(w_start, 4),
                    w2_kn_m=round(w_end, 4),
                    d1_m=c_d1,
                    d2_m=c_d2,
                    source=source,
                )
            )


def _distribute_slab_panel_loads(
    panel: SlabPanel,
    slab_thickness_mm: float,
    density_kn_m3: float,
    floor_cfg: FloorGravityLoad,
    floor_beams: list[AnalyticalMember],
    node_by_id: dict[int, AnalyticalNode],
    out_loads: list[BeamDistributedLoad],
) -> tuple[float, float, float]:
    """
    Distribute slab DL (self-weight), SDL, and LL/LR from a single SlabPanel to its boundary beams.
    Returns (panel_dl_kn, panel_sdl_kn, panel_ll_kn) for seismic weight accounting.
    """
    if panel.slab_action == "void" or panel.gross_area_sqm <= 1e-6 or panel.net_area_sqm <= 1e-6:
        return 0.0, 0.0, 0.0

    xs = [pt[0] for pt in panel.polygon_m]
    zs = [pt[1] for pt in panel.polygon_m]
    x_min, x_max = min(xs), max(xs)
    z_min, z_max = min(zs), max(zs)
    dx = x_max - x_min
    dz = z_max - z_min
    if dx <= 1e-4 or dz <= 1e-4:
        return 0.0, 0.0, 0.0

    net_ratio = panel.net_area_sqm / panel.gross_area_sqm
    t_m = slab_thickness_mm / 1000.0
    q_slab_sw = t_m * density_kn_m3 * net_ratio
    q_sdl = (
        floor_cfg.sdl_finish_kpa + floor_cfg.sdl_ceiling_mep_kpa + floor_cfg.sdl_partitions_kpa
    ) * net_ratio
    q_ll = floor_cfg.live_load_kpa * net_ratio
    ll_case = "LR" if floor_cfg.is_roof else "LL"
    ll_source = "slab_lr" if floor_cfg.is_roof else "slab_ll"

    bottom_beams = _match_edge_beams(floor_beams, node_by_id, "x", z_min, x_min, x_max)
    top_beams = _match_edge_beams(floor_beams, node_by_id, "x", z_max, x_min, x_max)
    left_beams = _match_edge_beams(floor_beams, node_by_id, "z", x_min, z_min, z_max)
    right_beams = _match_edge_beams(floor_beams, node_by_id, "z", x_max, z_min, z_max)

    short_span = min(dx, dz)
    is_two_way = panel.slab_action == "two_way"

    # Build (edge_matches, edge_len, is_short_edge) tuples
    if is_two_way:
        edges = [
            (bottom_beams, dx, dx < dz),
            (top_beams, dx, dx < dz),
            (left_beams, dz, dz < dx),
            (right_beams, dz, dz < dx),
        ]
    else:
        # One-way slab spans across short direction -> loads the two long-edge beams
        if dx >= dz:
            edges = [(bottom_beams, dx, False), (top_beams, dx, False)]
        else:
            edges = [(left_beams, dz, False), (right_beams, dz, False)]

    # Verify matched edges exist; if a one-way edge is missing a beam, scale remaining edge so equilibrium holds
    active_edges = [e for e in edges if e[0]]
    if not active_edges:
        # Fallback: try all four edges if one-way long edges were absent
        active_edges = [
            e
            for e in [
                (bottom_beams, dx, False),
                (top_beams, dx, False),
                (left_beams, dz, False),
                (right_beams, dz, False),
            ]
            if e[0]
        ]

    # Compute exact theoretical load components
    components = [
        (q_slab_sw, "DL", "slab_dl"),
        (q_sdl, "DL", "slab_sdl"),
        (q_ll, ll_case, ll_source),
    ]

    for q_val, lcase, src in components:
        if q_val <= 1e-6 or not active_edges:
            continue
        start_idx = len(out_loads)
        for matches, elen, is_short in active_edges:
            _emit_edge_load_segments(
                matches,
                elen,
                short_span,
                q_val,
                lcase,
                src,
                is_two_way=is_two_way,
                is_short_edge=is_short,
                out_loads=out_loads,
                node_by_id=node_by_id,
            )

        # Static equilibrium normalization if an edge had missing/partial beam coverage
        expected_force = q_val * dx * dz
        member_len_by_id = {m.member_id: m.length_m for m in floor_beams}
        actual_force = sum(
            beam_load_total_force_kn(ld, member_len_by_id.get(ld.member_id, 0.0))
            for ld in out_loads[start_idx:]
        )
        if actual_force > 1e-6 and abs(actual_force - expected_force) / expected_force > 0.005:
            scale = expected_force / actual_force
            for ld in out_loads[start_idx:]:
                ld.w1_kn_m = round(ld.w1_kn_m * scale, 4)
                ld.w2_kn_m = round(ld.w2_kn_m * scale, 4)

    return (
        q_slab_sw * dx * dz,
        q_sdl * dx * dz,
        q_ll * dx * dz,
    )


def _build_combinations(
    ca: float,
    seismic: SeismicParameters,
    options: LoadGenerationOptions,
) -> list[LoadCombinationDef]:
    """Generate NSCP 2015 Section 203.3.1 (LRFD) & Section 203.4.2 (ASD) load combinations."""
    combos: list[LoadCombinationDef] = []
    ev = round(0.5 * ca * seismic.importance_factor_i, 4) if seismic.include_vertical_ev else 0.0
    rho = round(seismic.redundancy_rho, 4)
    f1 = round(options.f1_live_load_factor, 4)

    def _clean(factors: dict[str, float]) -> dict[str, float]:
        return {k: round(v, 4) for k, v in factors.items() if abs(v) > 1e-6}

    if options.include_lrfd_combos:
        cid = 101
        combos.append(
            LoadCombinationDef(
                combo_id=cid,
                name="1.4 DL",
                design_basis="LRFD",
                factors=_clean({"DL": 1.4}),
            )
        )
        cid += 1
        combos.append(
            LoadCombinationDef(
                combo_id=cid,
                name="1.2 DL + 1.6 LL + 0.5 LR",
                design_basis="LRFD",
                factors=_clean({"DL": 1.2, "LL": 1.6, "LR": 0.5}),
            )
        )
        cid += 1
        combos.append(
            LoadCombinationDef(
                combo_id=cid,
                name=f"1.2 DL + 1.6 LR + {f1:g} LL",
                design_basis="LRFD",
                factors=_clean({"DL": 1.2, "LR": 1.6, "LL": f1}),
            )
        )
        cid += 1
        for w_dir in ("WX+", "WX-", "WZ+", "WZ-"):
            combos.append(
                LoadCombinationDef(
                    combo_id=cid,
                    name=f"1.2 DL + 1.0 {w_dir} + {f1:g} LL + 0.5 LR",
                    design_basis="LRFD",
                    factors=_clean({"DL": 1.2, w_dir: 1.0, "LL": f1, "LR": 0.5}),
                )
            )
            cid += 1
        for w_dir in ("WX+", "WX-", "WZ+", "WZ-"):
            combos.append(
                LoadCombinationDef(
                    combo_id=cid,
                    name=f"0.9 DL + 1.0 {w_dir}",
                    design_basis="LRFD",
                    factors=_clean({"DL": 0.9, w_dir: 1.0}),
                )
            )
            cid += 1
        dl_seis_pos = round(1.2 + ev, 4)
        for e_dir in ("EX+", "EX-", "EZ+", "EZ-"):
            combos.append(
                LoadCombinationDef(
                    combo_id=cid,
                    name=f"{dl_seis_pos:g} DL + {rho:g} {e_dir} + {f1:g} LL",
                    design_basis="LRFD",
                    factors=_clean({"DL": dl_seis_pos, e_dir: rho, "LL": f1}),
                )
            )
            cid += 1
        dl_seis_neg = round(0.9 - ev, 4)
        for e_dir in ("EX+", "EX-", "EZ+", "EZ-"):
            combos.append(
                LoadCombinationDef(
                    combo_id=cid,
                    name=f"{dl_seis_neg:g} DL + {rho:g} {e_dir}",
                    design_basis="LRFD",
                    factors=_clean({"DL": dl_seis_neg, e_dir: rho}),
                )
            )
            cid += 1

    if options.include_asd_combos:
        cid = 201
        combos.append(
            LoadCombinationDef(
                combo_id=cid,
                name="1.0 DL",
                design_basis="ASD",
                factors=_clean({"DL": 1.0}),
            )
        )
        cid += 1
        combos.append(
            LoadCombinationDef(
                combo_id=cid,
                name="1.0 DL + 1.0 LL",
                design_basis="ASD",
                factors=_clean({"DL": 1.0, "LL": 1.0}),
            )
        )
        cid += 1
        combos.append(
            LoadCombinationDef(
                combo_id=cid,
                name="1.0 DL + 0.75 LL + 0.75 LR",
                design_basis="ASD",
                factors=_clean({"DL": 1.0, "LL": 0.75, "LR": 0.75}),
            )
        )
        cid += 1
        for w_dir in ("WX+", "WX-", "WZ+", "WZ-"):
            combos.append(
                LoadCombinationDef(
                    combo_id=cid,
                    name=f"1.0 DL + 0.6 {w_dir}",
                    design_basis="ASD",
                    factors=_clean({"DL": 1.0, w_dir: 0.6}),
                )
            )
            cid += 1
        asd_dl_e = round(1.0 + 0.7 * ev, 4)
        asd_e = round(0.7 * rho, 4)
        for e_dir in ("EX+", "EX-", "EZ+", "EZ-"):
            combos.append(
                LoadCombinationDef(
                    combo_id=cid,
                    name=f"{asd_dl_e:g} DL + {asd_e:g} {e_dir}",
                    design_basis="ASD",
                    factors=_clean({"DL": asd_dl_e, e_dir: asd_e}),
                )
            )
            cid += 1
        for w_dir in ("WX+", "WX-", "WZ+", "WZ-"):
            combos.append(
                LoadCombinationDef(
                    combo_id=cid,
                    name=f"1.0 DL + 0.75 LL + 0.45 {w_dir}",
                    design_basis="ASD",
                    factors=_clean({"DL": 1.0, "LL": 0.75, w_dir: 0.45}),
                )
            )
            cid += 1
        asd_dl_e75 = round(1.0 + 0.525 * ev, 4)
        asd_e75 = round(0.525 * rho, 4)
        for e_dir in ("EX+", "EX-", "EZ+", "EZ-"):
            combos.append(
                LoadCombinationDef(
                    combo_id=cid,
                    name=f"{asd_dl_e75:g} DL + 0.75 LL + {asd_e75:g} {e_dir}",
                    design_basis="ASD",
                    factors=_clean({"DL": asd_dl_e75, "LL": 0.75, e_dir: asd_e75}),
                )
            )
            cid += 1
        for w_dir in ("WX+", "WX-", "WZ+", "WZ-"):
            combos.append(
                LoadCombinationDef(
                    combo_id=cid,
                    name=f"0.6 DL + 0.6 {w_dir}",
                    design_basis="ASD",
                    factors=_clean({"DL": 0.6, w_dir: 0.6}),
                )
            )
            cid += 1
        asd_dl_ovt = round(0.6 - 0.7 * ev, 4)
        for e_dir in ("EX+", "EX-", "EZ+", "EZ-"):
            combos.append(
                LoadCombinationDef(
                    combo_id=cid,
                    name=f"{asd_dl_ovt:g} DL + {asd_e:g} {e_dir}",
                    design_basis="ASD",
                    factors=_clean({"DL": asd_dl_ovt, e_dir: asd_e}),
                )
            )
            cid += 1

    return combos


def generate_loads(
    project: ProjectState,
    floor_loads: list[FloorGravityLoad] | None = None,
    seismic: SeismicParameters | None = None,
    wind: WindParameters | None = None,
    options: LoadGenerationOptions | None = None,
) -> LoadGenerationState:
    """
    Compute NSCP 2015 gravity tributary beam loads, CHB wall line loads, Section 208 static
    seismic weights & story shears, Section 207 MWFRS wind story forces, and LRFD/ASD
    load combinations.
    """
    model = project.structural_model
    sizing = project.member_sizing
    if not model.nodes or not model.members:
        raise ValueError(
            "Phase 3 structural geometry has not been generated yet. Run Module 03 (Generate 3D Frame) first."
        )
    if not sizing.member_assignments or not sizing.sections:
        raise ValueError(
            "Phase 4 member sizing has not been run yet. Run Module 04 (Auto-Size Members) first."
        )

    prev = project.load_generation
    seis_params = (
        seismic.model_copy(deep=True) if seismic is not None else prev.seismic.model_copy(deep=True)
    )
    wind_params = (
        wind.model_copy(deep=True) if wind is not None else prev.wind.model_copy(deep=True)
    )
    opts = (
        options.model_copy(deep=True) if options is not None else prev.options.model_copy(deep=True)
    )

    if floor_loads is not None and len(floor_loads) > 0:
        active_floor_loads = [fl.model_copy(deep=True) for fl in floor_loads]
    elif prev.floor_loads:
        active_floor_loads = [fl.model_copy(deep=True) for fl in prev.floor_loads]
    else:
        active_floor_loads = seed_default_floor_loads(project)

    floor_cfg_by_level = {fl.floor_level: fl for fl in active_floor_loads}

    node_by_id = {n.node_id: n for n in model.nodes}
    sec_by_id = {s.section_id: s for s in sizing.sections}
    asgn_by_mid = {a.member_id: a for a in sizing.member_assignments}
    slab_asgn_by_pid = {s.panel_id: s for s in sizing.slab_assignments}
    density = sizing.material.density_kn_m3

    levels = sorted(project.levels, key=lambda lv: lv.elevation_m)
    base_elev = levels[0].elevation_m if levels else min(n.y_m for n in model.nodes)
    elev_by_level = {lv.id: lv.elevation_m for lv in levels}

    # --- 1. Gravity & Tributary Beam Loads ---
    beam_loads: list[BeamDistributedLoad] = []
    beams_by_level: dict[str, list[AnalyticalMember]] = {}
    for m in model.members:
        if m.member_type != "column":
            beams_by_level.setdefault(m.floor_level, []).append(m)

    # Track per-level slab/wall/live totals for seismic weight
    slab_dl_by_level: dict[str, float] = {}
    slab_sdl_by_level: dict[str, float] = {}
    slab_ll_by_level: dict[str, float] = {}
    wall_dl_by_level: dict[str, float] = {}

    for panel in model.slab_panels:
        fl_cfg = floor_cfg_by_level.get(panel.floor_level)
        if fl_cfg is None:
            continue
        slab_asgn = slab_asgn_by_pid.get(panel.panel_id)
        t_mm = slab_asgn.thickness_mm if slab_asgn is not None else 125.0
        lvl_beams = beams_by_level.get(panel.floor_level, [])
        p_dl, p_sdl, p_ll = _distribute_slab_panel_loads(
            panel=panel,
            slab_thickness_mm=t_mm,
            density_kn_m3=density,
            floor_cfg=fl_cfg,
            floor_beams=lvl_beams,
            node_by_id=node_by_id,
            out_loads=beam_loads,
        )
        slab_dl_by_level[panel.floor_level] = slab_dl_by_level.get(panel.floor_level, 0.0) + p_dl
        slab_sdl_by_level[panel.floor_level] = slab_sdl_by_level.get(panel.floor_level, 0.0) + p_sdl
        if not fl_cfg.is_roof:
            slab_ll_by_level[panel.floor_level] = slab_ll_by_level.get(panel.floor_level, 0.0) + p_ll

    # CHB Wall Line Loads on primary beams
    for fl_cfg in active_floor_loads:
        lvl = fl_cfg.floor_level
        lvl_primary = [
            m for m in beams_by_level.get(lvl, []) if m.member_type == "primary_beam"
        ]
        if not lvl_primary:
            continue
        perim_ids = _identify_perimeter_beams(lvl_primary, node_by_id)
        for m in lvl_primary:
            if m.member_id in perim_ids:
                if fl_cfg.apply_exterior_wall_perimeter and fl_cfg.exterior_wall_line_kn_m > 0:
                    beam_loads.append(
                        BeamDistributedLoad(
                            member_id=m.member_id,
                            load_case="DL",
                            load_shape="UNI",
                            w1_kn_m=round(-fl_cfg.exterior_wall_line_kn_m, 4),
                            w2_kn_m=0.0,
                            d1_m=0.0,
                            d2_m=0.0,
                            source="ext_chb_wall",
                        )
                    )
                    wall_dl_by_level[lvl] = (
                        wall_dl_by_level.get(lvl, 0.0)
                        + fl_cfg.exterior_wall_line_kn_m * m.length_m
                    )
            else:
                if fl_cfg.apply_interior_wall_beams and fl_cfg.interior_wall_line_kn_m > 0:
                    beam_loads.append(
                        BeamDistributedLoad(
                            member_id=m.member_id,
                            load_case="DL",
                            load_shape="UNI",
                            w1_kn_m=round(-fl_cfg.interior_wall_line_kn_m, 4),
                            w2_kn_m=0.0,
                            d1_m=0.0,
                            d2_m=0.0,
                            source="int_chb_wall",
                        )
                    )
                    wall_dl_by_level[lvl] = (
                        wall_dl_by_level.get(lvl, 0.0)
                        + fl_cfg.interior_wall_line_kn_m * m.length_m
                    )

    # --- 2. Story Seismic Weights ---
    # Identify elevated framing levels above base_elev
    elevated_levels = [lv for lv in levels if lv.elevation_m > base_elev + 1e-4]

    def _closest_level_id(y_m: float) -> str | None:
        if not levels:
            return None
        best = min(levels, key=lambda lv: abs(lv.elevation_m - y_m))
        return best.id if abs(best.elevation_m - y_m) <= 0.25 else None

    frame_sw_by_level: dict[str, float] = {lv.id: 0.0 for lv in levels}
    for m in model.members:
        asgn = asgn_by_mid.get(m.member_id)
        sec = sec_by_id.get(asgn.section_id) if asgn else None
        if sec is None:
            continue
        vol_m3 = (sec.width_mm / 1000.0) * (sec.depth_mm / 1000.0) * m.length_m
        wt_kn = vol_m3 * density
        if m.member_type == "column":
            na = node_by_id.get(m.start_node)
            nb = node_by_id.get(m.end_node)
            if na and nb:
                low_lv = _closest_level_id(min(na.y_m, nb.y_m))
                high_lv = _closest_level_id(max(na.y_m, nb.y_m))
                if low_lv and low_lv in frame_sw_by_level:
                    frame_sw_by_level[low_lv] += 0.5 * wt_kn
                if high_lv and high_lv in frame_sw_by_level:
                    frame_sw_by_level[high_lv] += 0.5 * wt_kn
        else:
            lvl_id = m.floor_level if m.floor_level in frame_sw_by_level else None
            if lvl_id is None:
                na = node_by_id.get(m.start_node)
                if na:
                    lvl_id = _closest_level_id(na.y_m)
            if lvl_id and lvl_id in frame_sw_by_level:
                frame_sw_by_level[lvl_id] += wt_kn

    story_weights: list[tuple[str, float, float]] = []  # (level_id, elev_m, weight_kn)
    for lv in elevated_levels:
        w_i = (
            frame_sw_by_level.get(lv.id, 0.0)
            + slab_dl_by_level.get(lv.id, 0.0)
            + slab_sdl_by_level.get(lv.id, 0.0)
            + wall_dl_by_level.get(lv.id, 0.0)
            + seis_params.live_load_seismic_fraction * slab_ll_by_level.get(lv.id, 0.0)
        )
        story_weights.append((lv.id, lv.elevation_m, round(w_i, 3)))

    total_w = round(sum(w for _, _, w in story_weights), 3)

    # --- 3. NSCP 2015 Section 208 Seismic Base Shear & Story Distribution ---
    z_factor, na, nv, ca, cv = compute_seismic_coefficients(seis_params)
    top_elev = elevated_levels[-1].elevation_m if elevated_levels else base_elev
    h_n = round(max(top_elev - base_elev, 0.0), 4)
    period_t = round(seis_params.ct_coeff * (h_n ** 0.75), 4) if h_n > 0 else 0.0

    def _compute_base_shear(r_factor: float) -> tuple[float, float, float, float, float]:
        if total_w <= 0.0:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        v_max = (2.5 * ca * seis_params.importance_factor_i / r_factor) * total_w
        v_min = 0.11 * ca * seis_params.importance_factor_i * total_w
        v_min_z4 = (
            (0.8 * z_factor * nv * seis_params.importance_factor_i / r_factor) * total_w
            if seis_params.zone == 4
            else 0.0
        )
        if period_t > 1e-6:
            v_calc = (cv * seis_params.importance_factor_i / (r_factor * period_t)) * total_w
        else:
            v_calc = v_max
        v_design = max(min(v_calc, v_max), v_min, v_min_z4)
        ft = min(0.07 * period_t * v_design, 0.25 * v_design) if period_t > 0.7 else 0.0
        return v_design, v_max, v_min, v_min_z4, ft

    vx, v_max_x, v_min_x, v_min_z4_x, ft_x = _compute_base_shear(seis_params.r_factor_x)
    vz, _v_max_z, _v_min_z, _v_min_z4_z, ft_z = _compute_base_shear(seis_params.r_factor_z)

    wh_sum = sum(w * max(elev - base_elev, 0.0) for _, elev, w in story_weights)
    raw_fx: list[float] = []
    raw_fz: list[float] = []
    for idx, (_lid, elev, w) in enumerate(story_weights):
        h_i = max(elev - base_elev, 0.0)
        frac = (w * h_i / wh_sum) if wh_sum > 1e-6 else (1.0 / len(story_weights) if story_weights else 0.0)
        is_top = idx == len(story_weights) - 1
        raw_fx.append((vx - ft_x) * frac + (ft_x if is_top else 0.0))
        raw_fz.append((vz - ft_z) * frac + (ft_z if is_top else 0.0))

    seismic_stories: list[StorySeismicResult] = []
    for idx, (lid, elev, w) in enumerate(story_weights):
        cum_x = sum(raw_fx[idx:])
        cum_z = sum(raw_fz[idx:])
        seismic_stories.append(
            StorySeismicResult(
                floor_level=lid,
                elevation_m=round(elev, 3),
                weight_kn=round(w, 3),
                fx_kn=round(raw_fx[idx], 3),
                fz_kn=round(raw_fz[idx], 3),
                cumulative_shear_x_kn=round(cum_x, 3),
                cumulative_shear_z_kn=round(cum_z, 3),
            )
        )

    seismic_summary = SeismicSummary(
        z_factor=z_factor,
        na=na,
        nv=nv,
        ca=ca,
        cv=cv,
        building_height_m=h_n,
        period_t_sec=period_t,
        total_seismic_weight_kn=round(total_w, 3),
        v_design_x_kn=round(vx, 3),
        v_design_z_kn=round(vz, 3),
        v_max_kn=round(v_max_x, 3),
        v_min_kn=round(v_min_x, 3),
        v_min_z4_kn=round(v_min_z4_x, 3),
        ft_x_kn=round(ft_x, 3),
        ft_z_kn=round(ft_z, 3),
        base_shear_ratio_x=round(vx / total_w, 4) if total_w > 0 else 0.0,
        base_shear_ratio_z=round(vz / total_w, 4) if total_w > 0 else 0.0,
        stories=seismic_stories,
    )

    # --- 4. NSCP 2015 Section 207 Wind Loads (MWFRS) ---
    v_ms = wind_params.basic_wind_speed_kph / 3.6
    all_x = [n.x_m for n in model.nodes]
    all_z = [n.z_m for n in model.nodes]
    building_bx = max(max(all_x) - min(all_x), 0.0) if all_x else 0.0
    building_bz = max(max(all_z) - min(all_z), 0.0) if all_z else 0.0

    # Roof height velocity pressure qh
    kh = compute_wind_kz(max(top_elev - base_elev, 0.0), wind_params.exposure_category)
    qh_kpa = 0.000613 * kh * wind_params.topographic_kzt * wind_params.directionality_kd * (v_ms ** 2)

    wind_stories: list[StoryWindResult] = []
    for idx, lv in enumerate(elevated_levels):
        z_above = max(lv.elevation_m - base_elev, 0.0)
        prev_elev = base_elev if idx == 0 else elevated_levels[idx - 1].elevation_m
        next_elev = elevated_levels[idx + 1].elevation_m if idx + 1 < len(elevated_levels) else None
        h_below = 0.5 * max(lv.elevation_m - prev_elev, 0.0)
        h_above = 0.5 * max(next_elev - lv.elevation_m, 0.0) if next_elev is not None else 0.0
        h_trib = h_below + h_above

        # Floor-specific width if non-footing nodes exist at this elevation, else overall building width
        lvl_nodes = [
            n
            for n in model.nodes
            if not n.floor_level.startswith("FTG_") and abs(n.y_m - lv.elevation_m) <= 0.15
        ]
        if len(lvl_nodes) >= 2:
            bx = max(max(n.x_m for n in lvl_nodes) - min(n.x_m for n in lvl_nodes), 0.0) or building_bx
            bz = max(max(n.z_m for n in lvl_nodes) - min(n.z_m for n in lvl_nodes), 0.0) or building_bz
        else:
            bx, bz = building_bx, building_bz

        kz = compute_wind_kz(z_above, wind_params.exposure_category)
        qz_kpa = 0.000613 * kz * wind_params.topographic_kzt * wind_params.directionality_kd * (v_ms ** 2)
        p_x_kpa = qz_kpa * wind_params.gust_factor_g * wind_params.cp_windward - qh_kpa * wind_params.gust_factor_g * wind_params.cp_leeward_x
        p_z_kpa = qz_kpa * wind_params.gust_factor_g * wind_params.cp_windward - qh_kpa * wind_params.gust_factor_g * wind_params.cp_leeward_z
        w_fx = max(p_x_kpa, 0.0) * h_trib * bz
        w_fz = max(p_z_kpa, 0.0) * h_trib * bx
        wind_stories.append(
            StoryWindResult(
                floor_level=lv.id,
                elevation_m=round(lv.elevation_m, 3),
                kz=kz,
                qz_kpa=round(qz_kpa, 4),
                fx_kn=round(w_fx, 3),
                fz_kn=round(w_fz, 3),
            )
        )

    wind_summary = WindSummary(
        base_shear_x_kn=round(sum(s.fx_kn for s in wind_stories), 3),
        base_shear_z_kn=round(sum(s.fz_kn for s in wind_stories), 3),
        stories=wind_stories,
    )

    # --- 5. NSCP 2015 Load Combinations ---
    combinations = _build_combinations(ca=ca, seismic=seis_params, options=opts)

    state = LoadGenerationState(
        generated_at=_utc_now(),
        floor_loads=active_floor_loads,
        seismic=seis_params,
        wind=wind_params,
        options=opts,
        beam_loads=beam_loads,
        seismic_summary=seismic_summary,
        wind_summary=wind_summary,
        combinations=combinations,
        validation_issues=[],
        engineer_approved=False,
    )

    temp_project = project.model_copy(update={"load_generation": state})
    state.validation_issues = validate_loads(temp_project)
    return state
