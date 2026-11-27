from datetime import datetime, timezone
from math import hypot, sqrt
import re

from ..models import (
    AnalyticalMember,
    AnalyticalNode,
    FloorOpening,
    GeometryCheckIssue,
    GeometryGenerationOptions,
    Level,
    ProjectState,
    SlabPanel,
    StructuralModelState,
)
from .validator import validate_structural_model

FLOOR_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("B1", "BASEMENT", "BSMT", "BASEMENT 1", "BASEMENT FLOOR", "BASEMENT FLOOR FINISHED LINE"),
    ("GF", "GROUND", "GROUND FLOOR", "GL"),
    ("L1", "1F", "F1", "FIRST", "FIRST FLOOR", "1ST", "1ST FLOOR", "LEVEL 1"),
    ("L2", "2F", "F2", "SECOND", "SECOND FLOOR", "2ND", "2ND FLOOR", "LEVEL 2"),
    ("L3", "3F", "F3", "THIRD", "THIRD FLOOR", "3RD", "3RD FLOOR", "LEVEL 3"),
    ("L4", "4F", "F4", "FOURTH", "FOURTH FLOOR", "4TH", "4TH FLOOR", "LEVEL 4"),
    ("L5", "5F", "F5", "FIFTH", "FIFTH FLOOR", "5TH", "5TH FLOOR", "LEVEL 5"),
    ("L6", "6F", "F6", "SIXTH", "SIXTH FLOOR", "6TH", "6TH FLOOR", "LEVEL 6"),
    ("MEZZ", "MEZZANINE"),
    ("PH", "PENTHOUSE"),
    ("RD", "ROOF DECK", "ROOF", "ROOF LINE", "ROOF BEAM", "RB"),
)
NUMERIC_LEVEL_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(?:m)?\s*$", re.IGNORECASE)
NTH_LEVEL_RE = re.compile(
    r"^(?:L\s*(\d+)|(\d+)\s*F|F\s*(\d+)|LEVEL\s*(\d+)|(\d+)(?:ST|ND|RD|TH)(?:\s+FLOOR)?)$",
    re.IGNORECASE,
)


def _canonical_floor_key(label: str) -> str | None:
    norm = re.sub(r"\s+", " ", label.strip().upper())
    if not norm:
        return None
    for idx, group in enumerate(FLOOR_ALIAS_GROUPS):
        if norm in group:
            return f"ALIAS_{idx}"
    # Substring fallback for verbose architectural names like "FOURTH FLOOR FINISHED FLOOR LINE"
    for idx, group in enumerate(FLOOR_ALIAS_GROUPS):
        for alias in group:
            if len(alias) >= 4 and re.search(rf"\b{re.escape(alias)}\b", norm):
                return f"ALIAS_{idx}"
    m = NTH_LEVEL_RE.match(norm)
    if m:
        num = next(g for g in m.groups() if g is not None)
        return f"FLOOR_NUM_{int(num)}"
    return None


def _resolve_level_index(raw_level: str, levels: list[Level]) -> int | None:
    """
    Resolve a column/opening floor_level string against project.levels:
      1. Case-insensitive trimmed match against level.id or level.name.
      2. Common floor aliases (B1/BASEMENT, L1/1F/FIRST, L2/2F/SECOND, L3/3F/THIRD,
         L4/4F/FOURTH, RD/ROOF DECK, plus generic L<n>/<n>F/LEVEL <n>).
      3. Numeric string match (e.g. "14" or "14.0") within 0.05 m of level.elevation_m.
    """
    cleaned = raw_level.strip()
    if not cleaned:
        return None
    upper = cleaned.upper()

    # 1. Exact case-insensitive match against level.id and optional level.name
    for idx, lv in enumerate(levels):
        if lv.id.strip().upper() == upper:
            return idx
        lv_name = str(getattr(lv, "name", "") or "").strip()
        if lv_name and lv_name.upper() == upper:
            return idx

    # 2. Common floor alias match
    target_key = _canonical_floor_key(cleaned)
    if target_key is not None:
        for idx, lv in enumerate(levels):
            if _canonical_floor_key(lv.id) == target_key:
                return idx
            lv_name = str(getattr(lv, "name", "") or "").strip()
            if lv_name and _canonical_floor_key(lv_name) == target_key:
                return idx

    # 3. Numeric elevation match within 0.05 m
    m_num = NUMERIC_LEVEL_RE.match(cleaned)
    if m_num:
        target_elev = float(m_num.group(1))
        best_idx: int | None = None
        best_diff = float("inf")
        for idx, lv in enumerate(levels):
            diff = abs(lv.elevation_m - target_elev)
            if diff <= 0.05 + 1e-9 and diff < best_diff:
                best_diff = diff
                best_idx = idx
        if best_idx is not None:
            return best_idx

    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _polygon_area(poly: list[tuple[float, float]]) -> float:
    n = len(poly)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) * 0.5


def _point_on_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float, tol: float = 1e-4) -> bool:
    cross = abs((px - x1) * (y2 - y1) - (py - y1) * (x2 - x1))
    seg_len = hypot(x2 - x1, y2 - y1)
    if seg_len <= 1e-9:
        return hypot(px - x1, py - y1) <= tol
    if cross / seg_len > tol:
        return False
    dot = (px - x1) * (x2 - x1) + (py - y1) * (y2 - y1)
    return -tol * seg_len <= dot <= seg_len * seg_len + tol * seg_len


def _point_strictly_inside_polygon(px: float, py: float, polygon: list[list[float]], boundary_tol: float = 0.02) -> bool:
    """Return True if (px, py) lies strictly inside the polygon (not on its boundary edges)."""
    n = len(polygon)
    if n < 3:
        return False
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if _point_on_segment(px, py, x1, y1, x2, y2, tol=boundary_tol):
            return False

    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _clip_polygon_to_rect(
    polygon: list[list[float]],
    rx0: float,
    rz0: float,
    rx1: float,
    rz1: float,
) -> list[tuple[float, float]]:
    """
    Clip an arbitrary (convex or concave) polygon against an axis-aligned rectangle
    [rx0, rx1] x [rz0, rz1] using Sutherland-Hodgman clipping.
    """
    pts: list[tuple[float, float]] = [(float(p[0]), float(p[1])) for p in polygon]
    if len(pts) < 3:
        return []

    def clip_edge(poly: list[tuple[float, float]], inside_fn, intersect_fn) -> list[tuple[float, float]]:
        if not poly:
            return []
        out: list[tuple[float, float]] = []
        s = poly[-1]
        for e in poly:
            e_in = inside_fn(e)
            s_in = inside_fn(s)
            if e_in:
                if not s_in:
                    out.append(intersect_fn(s, e))
                out.append(e)
            elif s_in:
                out.append(intersect_fn(s, e))
            s = e
        return out

    # Left: x >= rx0
    pts = clip_edge(
        pts,
        lambda p: p[0] >= rx0,
        lambda s, e: (rx0, s[1] + (e[1] - s[1]) * (rx0 - s[0]) / (e[0] - s[0] + 1e-12)),
    )
    # Right: x <= rx1
    pts = clip_edge(
        pts,
        lambda p: p[0] <= rx1,
        lambda s, e: (rx1, s[1] + (e[1] - s[1]) * (rx1 - s[0]) / (e[0] - s[0] + 1e-12)),
    )
    # Bottom: z >= rz0
    pts = clip_edge(
        pts,
        lambda p: p[1] >= rz0,
        lambda s, e: (s[0] + (e[0] - s[0]) * (rz0 - s[1]) / (e[1] - s[1] + 1e-12), rz0),
    )
    # Top: z <= rz1
    pts = clip_edge(
        pts,
        lambda p: p[1] <= rz1,
        lambda s, e: (s[0] + (e[0] - s[0]) * (rz1 - s[1]) / (e[1] - s[1] + 1e-12), rz1),
    )
    return pts


def _cluster_coords(values: list[float], tol: float) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    groups: list[list[float]] = [[ordered[0]]]
    for v in ordered[1:]:
        mean = sum(groups[-1]) / len(groups[-1])
        if abs(v - mean) <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [round(sum(g) / len(g), 4) for g in groups]


def _openings_for_level(
    openings: list[FloorOpening],
    level_id: str,
    levels: list[Level] | None = None,
) -> list[FloorOpening]:
    lvl_upper = level_id.strip().upper()
    target_idx = _resolve_level_index(level_id, levels) if levels else None
    matched: list[FloorOpening] = []
    for op in openings:
        raw = op.floor_level.strip()
        if not raw or raw.upper() == lvl_upper:
            matched.append(op)
        elif levels is not None and target_idx is not None:
            if _resolve_level_index(raw, levels) == target_idx:
                matched.append(op)
    return matched


def _side_is_connected(
    a_id: int,
    b_id: int,
    fixed_axis: str,
    fixed_val: float,
    lo: float,
    hi: float,
    level_nodes: list[AnalyticalNode],
    beam_edges: set[tuple[int, int]],
    tol: float,
) -> bool:
    """Verify node a_id connects to node b_id along a straight boundary line via one or more collinear beams."""
    if (min(a_id, b_id), max(a_id, b_id)) in beam_edges:
        return True
    # Check chained collinear nodes along this edge
    on_line = []
    for n in level_nodes:
        if fixed_axis == "x":
            if abs(n.x_m - fixed_val) <= tol and lo - tol <= n.z_m <= hi + tol:
                on_line.append((n.z_m, n.node_id))
        else:
            if abs(n.z_m - fixed_val) <= tol and lo - tol <= n.x_m <= hi + tol:
                on_line.append((n.x_m, n.node_id))
    on_line.sort(key=lambda item: item[0])
    ids = [item[1] for item in on_line]
    if a_id not in ids or b_id not in ids:
        return False
    ia, ib = ids.index(a_id), ids.index(b_id)
    if ia > ib:
        ia, ib = ib, ia
    for k in range(ia, ib):
        u, v = ids[k], ids[k + 1]
        if (min(u, v), max(u, v)) not in beam_edges:
            return False
    return True


def _is_roof_level_id(level_id: str) -> bool:
    u = (level_id or "").strip().upper()
    return "ROOF" in u or u in {"RB", "RD", "RF"}


def is_perimeter_beam_pair(
    a: AnalyticalNode,
    b: AnalyticalNode,
    lv_nodes: list[AnalyticalNode],
    tol: float = 0.15,
) -> bool:
    """
    Return True if beam (a, b) lies on the outer perimeter boundary of lv_nodes.
    Handles both rectangular and L-shaped / stepped floor footprints by checking
    both global min/max bounds and local one-sided emptiness along the beam span.
    """
    if not lv_nodes:
        return True
    xs = [n.x_m for n in lv_nodes]
    zs = [n.z_m for n in lv_nodes]
    min_x, max_x = min(xs), max(xs)
    min_z, max_z = min(zs), max(zs)

    # Beam roughly parallel to X axis (constant Z)
    if abs(a.z_m - b.z_m) <= tol:
        z_line = (a.z_m + b.z_m) * 0.5
        if abs(z_line - min_z) <= tol or abs(z_line - max_z) <= tol:
            return True
        x_lo, x_hi = min(a.x_m, b.x_m) - tol, max(a.x_m, b.x_m) + tol
        has_below = any(x_lo <= n.x_m <= x_hi and n.z_m < z_line - tol for n in lv_nodes)
        has_above = any(x_lo <= n.x_m <= x_hi and n.z_m > z_line + tol for n in lv_nodes)
        return not (has_below and has_above)

    # Beam roughly parallel to Z axis (constant X)
    if abs(a.x_m - b.x_m) <= tol:
        x_line = (a.x_m + b.x_m) * 0.5
        if abs(x_line - min_x) <= tol or abs(x_line - max_x) <= tol:
            return True
        z_lo, z_hi = min(a.z_m, b.z_m) - tol, max(a.z_m, b.z_m) + tol
        has_left = any(z_lo <= n.z_m <= z_hi and n.x_m < x_line - tol for n in lv_nodes)
        has_right = any(z_lo <= n.z_m <= z_hi and n.x_m > x_line + tol for n in lv_nodes)
        return not (has_left and has_right)

    # Diagonal/skewed beams are kept as perimeter/explicit
    return True


def generate_structural_model(
    project: ProjectState,
    options: GeometryGenerationOptions | None = None,
) -> StructuralModelState:
    """
    Generate a 3D analytical structural wireframe (STAAD Y-up: X, Y=elevation, Z=plan Y)
    with stepped-base supports, opening-aware framing beams, slab panel detection, and
    automated integrity validation.
    """
    opts = options if options is not None else GeometryGenerationOptions()
    tol = opts.snap_tolerance_m
    levels = sorted(project.levels, key=lambda lv: lv.elevation_m)
    saved_manual_beams = [mb.model_copy(deep=True) for mb in project.structural_model.manual_beams]

    info_issues: list[GeometryCheckIssue] = []
    if len(levels) < 2 or not project.columns:
        model = StructuralModelState(
            generated_at=_utc_now(),
            options=opts,
            nodes=[],
            members=[],
            manual_beams=saved_manual_beams,
            slab_panels=[],
            validation_issues=[],
            engineer_approved=False,
        )
        model.validation_issues = validate_structural_model(model)
        return model

    available_levels_str = ", ".join(f"{lv.id} ({lv.elevation_m:g} m)" for lv in levels)

    nodes: list[AnalyticalNode] = []
    members: list[AnalyticalMember] = []

    def get_or_create_node(x_m: float, y_m: float, z_m: float, floor_level: str, grid_ref: str = "") -> int:
        for existing in nodes:
            if existing.floor_level == floor_level and hypot(existing.x_m - x_m, existing.z_m - z_m) <= tol:
                if not existing.grid_ref and grid_ref:
                    existing.grid_ref = grid_ref
                return existing.node_id
        nid = len(nodes) + 1
        nodes.append(
            AnalyticalNode(
                node_id=nid,
                x_m=round(x_m, 4),
                y_m=round(y_m, 4),
                z_m=round(z_m, 4),
                floor_level=floor_level,
                grid_ref=grid_ref,
                is_support=False,
                support_type="NONE",
            )
        )
        return nid

    # 1. Columns & vertical stacks (deterministic rule: column on L[i] connects L[i] -> L[i+1])
    seen_member_pairs: set[tuple[int, int]] = set()
    for col in project.columns:
        fl = col.floor_level.strip()
        if not fl:
            story_indices = list(range(len(levels) - 1))
        else:
            idx = _resolve_level_index(fl, levels)
            if idx is None:
                info_issues.append(
                    GeometryCheckIssue(
                        severity="warning",
                        code="UNKNOWN_COLUMN_LEVEL",
                        message=(
                            f"Column '{col.id}' references unknown floor_level '{col.floor_level}'; skipped. "
                            f"Available project levels: [{available_levels_str}]."
                        ),
                        floor_level=col.floor_level,
                        entity_ids=[col.id],
                    )
                )
                continue
            story_indices = [idx] if idx < len(levels) - 1 else [len(levels) - 2]

        for i in story_indices:
            low_lv = levels[i]
            up_lv = levels[i + 1]
            n_low = get_or_create_node(col.x_m, low_lv.elevation_m, col.y_m, low_lv.id, col.grid)
            n_up = get_or_create_node(col.x_m, up_lv.elevation_m, col.y_m, up_lv.id, col.grid)
            pair = (min(n_low, n_up), max(n_low, n_up))
            if n_low == n_up or pair in seen_member_pairs:
                continue
            seen_member_pairs.add(pair)
            length_m = round(abs(up_lv.elevation_m - low_lv.elevation_m), 4)
            members.append(
                AnalyticalMember(
                    member_id=len(members) + 1,
                    start_node=n_low,
                    end_node=n_up,
                    member_type="column",
                    floor_level=low_lv.id,
                    grid_ref=col.grid,
                    length_m=length_m,
                )
            )

    # 2. Automatic stepped supports: lowest node of every continuous vertical column stack
    node_by_id = {n.node_id: n for n in nodes}
    col_end_nodes = {m.end_node for m in members if m.member_type == "column"}
    for m in members:
        if m.member_type == "column" and m.start_node not in col_end_nodes:
            base_node = node_by_id[m.start_node]
            base_node.is_support = True
            base_node.support_type = opts.default_support_type

    # 3. Primary beams & base tie beams per level (with opening awareness)
    level_beam_edges: dict[str, set[tuple[int, int]]] = {lv.id: set() for lv in levels}

    for lv_idx, lv in enumerate(levels):
        if lv_idx == 0 and not opts.include_base_tie_beams:
            continue
        beam_type = "tie_beam" if lv_idx == 0 else "primary_beam"
        lv_nodes = [n for n in nodes if n.floor_level == lv.id]
        if len(lv_nodes) < 2:
            continue

        lv_openings = (
            _openings_for_level(project.openings, lv.id, levels)
            if (opts.skip_beams_through_openings and lv_idx > 0)
            else []
        )
        candidate_pairs: list[tuple[AnalyticalNode, AnalyticalNode, str]] = []

        # Group along Z (constant z_m -> X-direction beams)
        z_clusters = _cluster_coords([n.z_m for n in lv_nodes], tol)
        for zc in z_clusters:
            row = sorted([n for n in lv_nodes if abs(n.z_m - zc) <= tol], key=lambda n: n.x_m)
            for a, b in zip(row, row[1:]):
                candidate_pairs.append((a, b, f"{a.grid_ref}-{b.grid_ref}".strip("-")))

        # Group along X (constant x_m -> Z-direction beams)
        x_clusters = _cluster_coords([n.x_m for n in lv_nodes], tol)
        for xc in x_clusters:
            col_line = sorted([n for n in lv_nodes if abs(n.x_m - xc) <= tol], key=lambda n: n.z_m)
            for a, b in zip(col_line, col_line[1:]):
                candidate_pairs.append((a, b, f"{a.grid_ref}-{b.grid_ref}".strip("-")))

        is_top_roof = (lv_idx == len(levels) - 1) and (
            _is_roof_level_id(lv.id) or opts.roof_framing_style == "perimeter_only"
        )
        perimeter_only_here = is_top_roof and opts.roof_framing_style == "perimeter_only"

        for a, b, gref in candidate_pairs:
            pair = (min(a.node_id, b.node_id), max(a.node_id, b.node_id))
            if pair in seen_member_pairs or a.node_id == b.node_id:
                continue
            if perimeter_only_here and not is_perimeter_beam_pair(a, b, lv_nodes, tol=max(tol, 0.15)):
                continue
            dist = round(hypot(b.x_m - a.x_m, b.z_m - a.z_m), 4)
            if dist < 0.05 or dist > opts.max_beam_span_m:
                continue

            # Opening check: omit horizontal beam if its midpoint falls strictly inside an opening polygon
            if lv_openings:
                mx = (a.x_m + b.x_m) * 0.5
                mz = (a.z_m + b.z_m) * 0.5
                crossed = next(
                    (op for op in lv_openings if _point_strictly_inside_polygon(mx, mz, op.polygon_m)),
                    None,
                )
                if crossed is not None:
                    info_issues.append(
                        GeometryCheckIssue(
                            severity="info",
                            code="BEAM_SKIPPED_OPENING",
                            message=(
                                f"Skipped beam N{a.node_id}-N{b.node_id} on {lv.id} crossing "
                                f"{crossed.category} opening '{crossed.label}'."
                            ),
                            floor_level=lv.id,
                            entity_ids=[f"N{a.node_id}", f"N{b.node_id}", crossed.id],
                        )
                    )
                    continue

            seen_member_pairs.add(pair)
            level_beam_edges[lv.id].add(pair)
            members.append(
                AnalyticalMember(
                    member_id=len(members) + 1,
                    start_node=a.node_id,
                    end_node=b.node_id,
                    member_type=beam_type,
                    floor_level=lv.id,
                    grid_ref=gref,
                    length_m=dist,
                )
            )

    # 3b. Replay saved coordinate-anchored manual & diagonal beams so regenerations never wipe user overrides
    replay_xz_tol = max(tol, 0.25)
    replay_y_tol = 0.10
    for mb in saved_manual_beams:
        sx, sy, sz = mb.start_xyz_m
        ex, ey, ez = mb.end_xyz_m
        na = min(
            (
                n
                for n in nodes
                if hypot(n.x_m - sx, n.z_m - sz) <= replay_xz_tol and abs(n.y_m - sy) <= replay_y_tol
            ),
            key=lambda n: hypot(n.x_m - sx, n.z_m - sz) + abs(n.y_m - sy),
            default=None,
        )
        nb = min(
            (
                n
                for n in nodes
                if hypot(n.x_m - ex, n.z_m - ez) <= replay_xz_tol and abs(n.y_m - ey) <= replay_y_tol
            ),
            key=lambda n: hypot(n.x_m - ex, n.z_m - ez) + abs(n.y_m - ey),
            default=None,
        )
        if na is None or nb is None or na.node_id == nb.node_id:
            continue
        pair = (min(na.node_id, nb.node_id), max(na.node_id, nb.node_id))
        if pair in seen_member_pairs:
            continue
        length_m = round(
            sqrt((nb.x_m - na.x_m) ** 2 + (nb.y_m - na.y_m) ** 2 + (nb.z_m - na.z_m) ** 2),
            4,
        )
        if length_m < 0.05:
            continue
        fl_id = na.floor_level or mb.floor_level
        gref = mb.grid_ref or f"{na.grid_ref}-{nb.grid_ref}".strip("-") or "MANUAL"
        seen_member_pairs.add(pair)
        if na.floor_level == nb.floor_level and fl_id in level_beam_edges:
            level_beam_edges[fl_id].add(pair)
        members.append(
            AnalyticalMember(
                member_id=len(members) + 1,
                start_node=na.node_id,
                end_node=nb.node_id,
                member_type=mb.member_type,
                floor_level=fl_id,
                grid_ref=gref,
                length_m=length_m,
                manual_override=True,
            )
        )

    # 4. Slab Panel Detection (framing levels above base)
    slab_panels: list[SlabPanel] = []
    if opts.auto_detect_slab_panels:
        for lv_idx, lv in enumerate(levels):
            if lv_idx == 0:
                continue
            lv_nodes = [n for n in nodes if n.floor_level == lv.id]
            beam_edges = level_beam_edges.get(lv.id, set())
            if len(lv_nodes) < 4 or len(beam_edges) < 4:
                continue

            xs = _cluster_coords([n.x_m for n in lv_nodes], tol)
            zs = _cluster_coords([n.z_m for n in lv_nodes], tol)
            lv_openings = _openings_for_level(project.openings, lv.id, levels)

            def find_node(xv: float, zv: float) -> AnalyticalNode | None:
                for n in lv_nodes:
                    if abs(n.x_m - xv) <= tol and abs(n.z_m - zv) <= tol:
                        return n
                return None

            panel_idx = 0
            for xi in range(len(xs) - 1):
                x0, x1 = xs[xi], xs[xi + 1]
                for zi in range(len(zs) - 1):
                    z0, z1 = zs[zi], zs[zi + 1]
                    n00 = find_node(x0, z0)
                    n10 = find_node(x1, z0)
                    n11 = find_node(x1, z1)
                    n01 = find_node(x0, z1)
                    if not (n00 and n10 and n11 and n01):
                        continue

                    bottom_ok = _side_is_connected(n00.node_id, n10.node_id, "z", z0, x0, x1, lv_nodes, beam_edges, tol)
                    top_ok = _side_is_connected(n01.node_id, n11.node_id, "z", z1, x0, x1, lv_nodes, beam_edges, tol)
                    left_ok = _side_is_connected(n00.node_id, n01.node_id, "x", x0, z0, z1, lv_nodes, beam_edges, tol)
                    right_ok = _side_is_connected(n10.node_id, n11.node_id, "x", x1, z0, z1, lv_nodes, beam_edges, tol)
                    if not (bottom_ok and top_ok and left_ok and right_ok):
                        continue

                    lx_m = round(x1 - x0, 4)
                    ly_m = round(z1 - z0, 4)
                    if lx_m <= 0.05 or ly_m <= 0.05:
                        continue
                    gross_area = round(lx_m * ly_m, 4)

                    deduction = 0.0
                    for op in lv_openings:
                        clipped = _clip_polygon_to_rect(op.polygon_m, x0, z0, x1, z1)
                        deduction += _polygon_area(clipped)
                    deduction = round(min(deduction, gross_area), 4)
                    net_area = round(max(0.0, gross_area - deduction), 4)

                    if net_area <= 0.10 * gross_area:
                        action = "void"
                    elif max(lx_m, ly_m) / max(min(lx_m, ly_m), 1e-6) >= 2.0:
                        action = "one_way"
                    else:
                        action = "two_way"

                    panel_idx += 1
                    slab_panels.append(
                        SlabPanel(
                            panel_id=f"S-{lv.id}-{panel_idx}",
                            floor_level=lv.id,
                            polygon_m=[[x0, z0], [x1, z0], [x1, z1], [x0, z1]],
                            lx_m=lx_m,
                            ly_m=ly_m,
                            gross_area_sqm=gross_area,
                            opening_deduction_sqm=deduction,
                            net_area_sqm=net_area,
                            slab_action=action,
                        )
                    )

    # 5. Column Pedestal Drop to Top of Footing (keeps base tie beams at grade elevation)
    if opts.footing_depth_m > 1e-4:
        drop_m = round(opts.footing_depth_m, 4)
        supported_base_nodes = [
            n for n in list(nodes) if n.is_support and n.support_type in ("FIXED", "PINNED")
        ]
        for grade_node in supported_base_nodes:
            orig_support = grade_node.support_type
            orig_floor = grade_node.floor_level
            grade_node.is_support = False
            grade_node.support_type = "NONE"

            ftg_nid = len(nodes) + 1
            ftg_node = AnalyticalNode(
                node_id=ftg_nid,
                x_m=grade_node.x_m,
                y_m=round(grade_node.y_m - drop_m, 4),
                z_m=grade_node.z_m,
                floor_level=f"FTG_{orig_floor}",
                grid_ref=grade_node.grid_ref,
                is_support=True,
                support_type=orig_support,
            )
            nodes.append(ftg_node)
            members.append(
                AnalyticalMember(
                    member_id=len(members) + 1,
                    start_node=ftg_nid,
                    end_node=grade_node.node_id,
                    member_type="column",
                    floor_level=orig_floor,
                    grid_ref=grade_node.grid_ref,
                    length_m=drop_m,
                    manual_override=False,
                    is_pedestal=True,
                )
            )

    model = StructuralModelState(
        generated_at=_utc_now(),
        options=opts,
        nodes=nodes,
        members=members,
        manual_beams=saved_manual_beams,
        slab_panels=slab_panels,
        validation_issues=[],
        engineer_approved=False,
    )
    model.validation_issues = validate_structural_model(model) + info_issues
    return model
