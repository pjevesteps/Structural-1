from ..models import ProjectState, PropertyCheckIssue


def validate_member_sizing(project: ProjectState) -> list[PropertyCheckIssue]:
    """
    Validate member section assignments, slab thicknesses, and NSCP 2015 SMRF (Zone 4)
    preliminary dimensional checks against project.structural_model.
    """
    issues: list[PropertyCheckIssue] = []
    model = project.structural_model
    sizing = project.member_sizing
    opts = sizing.options

    if not model.members:
        issues.append(
            PropertyCheckIssue(
                severity="error",
                code="NO_GEOMETRY_MEMBERS",
                message="Structural geometry has no members yet. Generate the 3D frame in Module 03 first.",
            )
        )
        return issues

    sec_by_id = {s.section_id: s for s in sizing.sections}
    assign_by_mid = {a.member_id: a for a in sizing.member_assignments}
    member_by_id = {m.member_id: m for m in model.members}
    panel_by_id = {p.panel_id: p for p in model.slab_panels}

    # 1. Section dimensional validity
    for sec in sizing.sections:
        if sec.width_mm <= 0 or sec.depth_mm <= 0:
            issues.append(
                PropertyCheckIssue(
                    severity="error",
                    code="INVALID_SECTION_DIM",
                    message=(
                        f"Section '{sec.section_id}' has non-positive dimensions "
                        f"({sec.width_mm} x {sec.depth_mm} mm)."
                    ),
                    entity_ids=[sec.section_id],
                )
            )

    # 2. Completeness & referential integrity of member assignments
    missing_mids: list[int] = []
    for m in model.members:
        asgn = assign_by_mid.get(m.member_id)
        if asgn is None:
            missing_mids.append(m.member_id)
        elif asgn.section_id not in sec_by_id:
            issues.append(
                PropertyCheckIssue(
                    severity="error",
                    code="UNKNOWN_SECTION_REF",
                    message=f"Member M{m.member_id} references unknown section '{asgn.section_id}'.",
                    entity_ids=[f"M{m.member_id}", asgn.section_id],
                )
            )
    if missing_mids:
        sample = ", ".join(f"M{mid}" for mid in missing_mids[:10])
        suffix = f" (+{len(missing_mids) - 10} more)" if len(missing_mids) > 10 else ""
        issues.append(
            PropertyCheckIssue(
                severity="error",
                code="UNASSIGNED_MEMBER",
                message=f"{len(missing_mids)} member(s) have no assigned section: {sample}{suffix}.",
                entity_ids=[f"M{mid}" for mid in missing_mids],
            )
        )

    # 3. NSCP 2015 Seismic SMRF (Zone 4) preliminary dimensional checks
    if opts.seismic_smrf_zone4:
        # Track column sections attached to each node for beam-vs-column width check
        node_columns: dict[int, list[tuple[int, float, float, str]]] = {}
        for m in model.members:
            if m.member_type != "column":
                continue
            asgn = assign_by_mid.get(m.member_id)
            sec = sec_by_id.get(asgn.section_id) if asgn else None
            if sec is None or sec.width_mm <= 0 or sec.depth_mm <= 0:
                continue
            node_columns.setdefault(m.start_node, []).append(
                (m.member_id, sec.width_mm, sec.depth_mm, sec.section_id)
            )
            node_columns.setdefault(m.end_node, []).append(
                (m.member_id, sec.width_mm, sec.depth_mm, sec.section_id)
            )

        # Check column section minimum dimensions (>= 300 mm per NSCP 2015 Sec. 418.7.2.1)
        checked_col_secs: set[str] = set()
        checked_beam_secs: set[str] = set()
        for m in model.members:
            asgn = assign_by_mid.get(m.member_id)
            sec = sec_by_id.get(asgn.section_id) if asgn else None
            if sec is None or sec.width_mm <= 0 or sec.depth_mm <= 0:
                continue

            if m.member_type == "column" and sec.section_id not in checked_col_secs:
                checked_col_secs.add(sec.section_id)
                if sec.width_mm < 300.0 or sec.depth_mm < 300.0:
                    issues.append(
                        PropertyCheckIssue(
                            severity="warning",
                            code="SMRF_COLUMN_MIN_DIM",
                            message=(
                                f"Column section '{sec.section_id}' ({sec.width_mm:.0f}x{sec.depth_mm:.0f} mm) "
                                "is below NSCP 2015 SMRF minimum 300 mm dimension (Sec. 418.7.2.1)."
                            ),
                            entity_ids=[sec.section_id],
                        )
                    )

            if m.member_type == "primary_beam":
                if sec.section_id not in checked_beam_secs:
                    checked_beam_secs.add(sec.section_id)
                    ratio = sec.width_mm / max(sec.depth_mm, 1e-6)
                    if sec.width_mm < 250.0 or ratio < 0.30:
                        issues.append(
                            PropertyCheckIssue(
                                severity="warning",
                                code="SMRF_BEAM_MIN_WIDTH",
                                message=(
                                    f"Primary beam section '{sec.section_id}' ({sec.width_mm:.0f}x{sec.depth_mm:.0f} mm, "
                                    f"b/h={ratio:.2f}) violates NSCP 2015 SMRF minimum width >= 250 mm "
                                    "and b/h >= 0.30 (Sec. 418.6.2.1)."
                                ),
                                entity_ids=[sec.section_id],
                            )
                        )

                # Beam wider than framing column check at connected nodes
                for nid in (m.start_node, m.end_node):
                    for col_mid, col_w, col_d, col_sec_id in node_columns.get(nid, []):
                        col_min_dim = min(col_w, col_d)
                        if sec.width_mm > col_min_dim + 1e-6:
                            issues.append(
                                PropertyCheckIssue(
                                    severity="warning",
                                    code="BEAM_WIDER_THAN_COLUMN",
                                    message=(
                                        f"Primary beam M{m.member_id} ({sec.section_id}, b={sec.width_mm:.0f} mm) "
                                        f"exceeds framing column M{col_mid} ({col_sec_id}, min dim={col_min_dim:.0f} mm) "
                                        f"at node N{nid}."
                                    ),
                                    entity_ids=[f"M{m.member_id}", f"M{col_mid}", f"N{nid}"],
                                )
                            )
                            break

        if not opts.include_cracked_modifiers:
            issues.append(
                PropertyCheckIssue(
                    severity="info",
                    code="CRACKED_MODIFIERS_OFF",
                    message=(
                        "Cracked section modifiers (0.70 Ig columns / 0.35 Ig beams per NSCP 2015 "
                        "Sec. 406.6.3.1.1) are currently disabled for this SMRF frame."
                    ),
                )
            )

    # 4. Slab thickness checks
    for sa in sizing.slab_assignments:
        panel = panel_by_id.get(sa.panel_id)
        if sa.slab_tag.upper() == "VOID" or (panel is not None and panel.slab_action == "void"):
            continue
        if sa.thickness_mm < sa.min_code_thickness_mm - 1e-6:
            issues.append(
                PropertyCheckIssue(
                    severity="warning",
                    code="SLAB_UNDER_MIN_THICKNESS",
                    message=(
                        f"Slab panel '{sa.panel_id}' ({sa.floor_level}, {sa.slab_tag}) assigned "
                        f"{sa.thickness_mm:.0f} mm is thinner than NSCP minimum {sa.min_code_thickness_mm:.1f} mm."
                    ),
                    entity_ids=[sa.panel_id],
                )
            )

    # Suppress unused variable warning for member_by_id if needed
    _ = member_by_id
    return issues
