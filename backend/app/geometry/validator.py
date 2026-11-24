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
                issues.append(
                    GeometryCheckIssue(
                        severity="error",
                        code="DUPLICATE_NODE",
                        message=(
                            f"Coincident nodes N{na.node_id} and N{nb.node_id} at "
                            f"({na.x_m:.2f}, {na.y_m:.2f}, {na.z_m:.2f}) m."
                        ),
                        floor_level=na.floor_level,
                        entity_ids=[f"N{na.node_id}", f"N{nb.node_id}"],
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
            issues.append(
                GeometryCheckIssue(
                    severity="error",
                    code="DUPLICATE_MEMBER",
                    message=(
                        f"Duplicate members M{seen_pairs[pair]} and M{m.member_id} "
                        f"share nodes N{pair[0]}-N{pair[1]}."
                    ),
                    floor_level=m.floor_level,
                    entity_ids=[f"M{seen_pairs[pair]}", f"M{m.member_id}"],
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

    # 4. Floating / transfer columns (warning)
    min_y = min(n.y_m for n in model.nodes)
    for m in model.members:
        if m.member_type != "column":
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
                )
            )

    return issues
