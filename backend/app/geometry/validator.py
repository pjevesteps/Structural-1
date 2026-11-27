from math import sqrt
from ..models import GeometryCheckIssue, StructuralModelState


def _dist_3d(a, b) -> float:
    return sqrt((a.x_m - b.x_m) ** 2 + (a.y_m - b.y_m) ** 2 + (a.z_m - b.z_m) ** 2)


def validate_structural_model(model: StructuralModelState) -> list[GeometryCheckIssue]:
    """
    Run structural geometry integrity checks on an analytical model:
      - Errors: duplicate coincident nodes, zero-length members (< 0.05 m), duplicate members
        sharing (start_node, end_node), or zero supports.
      - Warnings: floating / transfer columns (column start node above lowest elevation without
        a column directly below), orphan/unconnected nodes, or beams exceeding 10.0 m.
    """
    issues: list[GeometryCheckIssue] = []

    if not model.nodes:
        issues.append(
            GeometryCheckIssue(
                severity="error",
                code="EMPTY_MODEL",
                message="Analytical model has no nodes. Generate geometry first.",
            )
        )
        return issues

    node_by_id = {n.node_id: n for n in model.nodes}
    tol = max(min(model.options.snap_tolerance_m, 0.05), 1e-4)

    # 1. Duplicate coincident nodes (error)
    nodes_list = model.nodes
    for i in range(len(nodes_list)):
        na = nodes_list[i]
        for j in range(i + 1, len(nodes_list)):
            nb = nodes_list[j]
            if _dist_3d(na, nb) < tol:
                prim_id = min(na.node_id, nb.node_id)
                dup_id = max(na.node_id, nb.node_id)
                issues.append(
                    GeometryCheckIssue(
                        severity="error",
                        code="DUPLICATE_NODE",
                        message=(
                            f"Coincident nodes N{prim_id} and N{dup_id} at "
                            f"({na.x_m:.2f}, {na.y_m:.2f}, {na.z_m:.2f}) m."
                        ),
                        floor_level=na.floor_level,
                        entity_ids=[f"N{prim_id}", f"N{dup_id}"],
                        primary_node_id=prim_id,
                        duplicate_node_id=dup_id,
                        node_ids=[prim_id, dup_id],
                    )
                )

    # 2. Zero-length / invalid / duplicate members (error) & long beams (warning)
    seen_pairs: dict[tuple[int, int], int] = {}
    connected_nodes: set[int] = set()
    column_end_nodes: set[int] = set()

    for m in model.members:
        na = node_by_id.get(m.start_node)
        nb = node_by_id.get(m.end_node)
        if na is None or nb is None:
            issues.append(
                GeometryCheckIssue(
                    severity="error",
                    code="UNKNOWN_NODE_REF",
                    message=f"Member M{m.member_id} references unknown node(s) ({m.start_node}, {m.end_node}).",
                    floor_level=m.floor_level,
                    entity_ids=[f"M{m.member_id}"],
                )
            )
            continue

        connected_nodes.add(m.start_node)
        connected_nodes.add(m.end_node)
        if m.member_type == "column":
            column_end_nodes.add(m.end_node)

        actual_len = _dist_3d(na, nb)
        if m.start_node == m.end_node or actual_len < 0.05 or m.length_m < 0.05:
            issues.append(
                GeometryCheckIssue(
                    severity="error",
                    code="ZERO_LENGTH_MEMBER",
                    message=f"Member M{m.member_id} has near-zero length ({actual_len:.3f} m < 0.05 m).",
                    floor_level=m.floor_level,
                    entity_ids=[f"M{m.member_id}"],
                )
            )

        pair = (min(m.start_node, m.end_node), max(m.start_node, m.end_node))
        if pair in seen_pairs:
            prim_mid = seen_pairs[pair]
            dup_mid = m.member_id
            issues.append(
                GeometryCheckIssue(
                    severity="error",
                    code="DUPLICATE_MEMBER",
                    message=(
                        f"Duplicate members M{prim_mid} and M{dup_mid} "
                        f"share nodes N{pair[0]}-N{pair[1]}."
                    ),
                    floor_level=m.floor_level,
                    entity_ids=[f"M{prim_mid}", f"M{dup_mid}"],
                    primary_member_id=prim_mid,
                    duplicate_member_id=dup_mid,
                    member_ids=[prim_mid, dup_mid],
                    node_ids=[pair[0], pair[1]],
                )
            )
        else:
            seen_pairs[pair] = m.member_id

        if m.member_type != "column" and actual_len > 10.0:
            issues.append(
                GeometryCheckIssue(
                    severity="warning",
                    code="LONG_BEAM_SPAN",
                    message=(
                        f"Beam M{m.member_id} on {m.floor_level} spans {actual_len:.2f} m (> 10.0 m). "
                        "Verify framing depth and deflection."
                    ),
                    floor_level=m.floor_level,
                    entity_ids=[f"M{m.member_id}"],
                )
            )

    # 3. Supports check (error if 0 supports)
    supports = [n for n in model.nodes if n.is_support and n.support_type != "NONE"]
    if not supports:
        issues.append(
            GeometryCheckIssue(
                severity="error",
                code="NO_SUPPORTS",
                message="Analytical model has 0 supports. Assign FIXED or PINNED supports to base nodes.",
            )
        )

    # 4. Floating / transfer columns (warning) — exclude buried footing pedestals (is_pedestal=True)
    min_y = min(n.y_m for n in model.nodes)
    for m in model.members:
        if m.member_type != "column" or m.is_pedestal:
            continue
        start_n = node_by_id.get(m.start_node)
        if start_n is None:
            continue
        if start_n.y_m > min_y + 1e-4 and m.start_node not in column_end_nodes:
            if start_n.is_support and start_n.support_type != "NONE":
                msg = (
                    f"Stepped-base / transfer column M{m.member_id} starts at {start_n.floor_level} "
                    f"(y={start_n.y_m:.2f} m, above lowest level {min_y:.2f} m) with no column below. "
                    f"Assigned {start_n.support_type} support — confirm foundation vs transfer framing."
                )
            else:
                msg = (
                    f"Floating / transfer column M{m.member_id} starts at {start_n.floor_level} "
                    f"(y={start_n.y_m:.2f} m) with no column or support directly below it."
                )
            issues.append(
                GeometryCheckIssue(
                    severity="warning",
                    code="FLOATING_OR_TRANSFER_COLUMN",
                    message=msg,
                    floor_level=start_n.floor_level,
                    entity_ids=[f"M{m.member_id}", f"N{start_n.node_id}"],
                )
            )

    # 5. Orphan / unconnected nodes (warning)
    for n in model.nodes:
        if n.node_id not in connected_nodes:
            issues.append(
                GeometryCheckIssue(
                    severity="warning",
                    code="ORPHAN_NODE",
                    message=f"Node N{n.node_id} on {n.floor_level} is not connected to any member.",
                    floor_level=n.floor_level,
                    entity_ids=[f"N{n.node_id}"],
                    node_ids=[n.node_id],
                )
            )

    # 6. Disjointed / disconnected frame substructure check (BFS connected components)
    adj: dict[int, set[int]] = {n.node_id: set() for n in model.nodes}
    for m in model.members:
        if m.start_node in adj and m.end_node in adj:
            adj[m.start_node].add(m.end_node)
            adj[m.end_node].add(m.start_node)

    visited: set[int] = set()
    components: list[set[int]] = []
    for n in model.nodes:
        if n.node_id in visited:
            continue
        comp: set[int] = set()
        queue = [n.node_id]
        visited.add(n.node_id)
        while queue:
            cur = queue.pop()
            comp.add(cur)
            for nxt in adj.get(cur, ()):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        components.append(comp)

    if len(components) > 1:
        components.sort(key=len, reverse=True)
        substructures_payload: list[dict[str, list[int]]] = []
        for comp in components:
            c_nodes = sorted(comp)
            c_members = sorted(
                m.member_id
                for m in model.members
                if m.start_node in comp or m.end_node in comp
            )
            substructures_payload.append({"node_ids": c_nodes, "member_ids": c_members})

        detached_nodes = sorted(nid for comp in components[1:] for nid in comp)
        detached_node_set = set(detached_nodes)
        detached_members = sorted(
            m.member_id
            for m in model.members
            if m.start_node in detached_node_set or m.end_node in detached_node_set
        )
        first_det_floor = ""
        if detached_nodes:
            first_n = node_by_id.get(detached_nodes[0])
            if first_n is not None:
                first_det_floor = first_n.floor_level
        node_preview = ", ".join(f"N{nid}" for nid in detached_nodes[:10])
        if len(detached_nodes) > 10:
            node_preview += f" (+{len(detached_nodes) - 10} more)"
        mem_preview = ", ".join(f"M{mid}" for mid in detached_members[:10])
        if len(detached_members) > 10:
            mem_preview += f" (+{len(detached_members) - 10} more)"
        issues.append(
            GeometryCheckIssue(
                severity="warning",
                code="DISJOINTED_SUBSTRUCTURE",
                message=(
                    f"Analytical model has {len(components)} disconnected substructures. "
                    f"Detached node(s): {node_preview}"
                    + (f"; detached member(s): {mem_preview}" if mem_preview else "")
                    + ". Tie detached columns/nodes with beams in Module 03's 2D Framing Editor."
                ),
                floor_level=first_det_floor,
                entity_ids=[f"N{nid}" for nid in detached_nodes[:12]]
                + [f"M{mid}" for mid in detached_members[:12]],
                node_ids=detached_nodes,
                member_ids=detached_members,
                substructures=substructures_payload,
            )
        )

    return issues
