from ..models import LoadCheckIssue, ProjectState


def validate_loads(project: ProjectState) -> list[LoadCheckIssue]:
    """Validate NSCP 2015 generated loads, seismic/wind parameters, and member references."""
    issues: list[LoadCheckIssue] = []
    lg = project.load_generation
    model = project.structural_model

    valid_mids = {m.member_id for m in model.members}
    invalid_load_mids = sorted({b.member_id for b in lg.beam_loads if b.member_id not in valid_mids})
    if invalid_load_mids:
        issues.append(
            LoadCheckIssue(
                severity="error",
                code="INVALID_BEAM_LOAD_REF",
                message=(
                    f"{len(invalid_load_mids)} beam load(s) reference member ID(s) missing from "
                    f"Phase 3 geometry: {invalid_load_mids[:8]}"
                ),
            )
        )

    if lg.seismic_summary is None or lg.seismic_summary.total_seismic_weight_kn <= 0.0:
        issues.append(
            LoadCheckIssue(
                severity="error",
                code="ZERO_SEISMIC_WEIGHT",
                message=(
                    "Total seismic weight W is <= 0 kN. Ensure Phase 3 geometry, Phase 4 sizing, "
                    "and elevated framing levels exist before approving loads."
                ),
            )
        )
    else:
        for axis_label, ratio in (
            ("X", lg.seismic_summary.base_shear_ratio_x),
            ("Z", lg.seismic_summary.base_shear_ratio_z),
        ):
            if ratio > 0.35:
                issues.append(
                    LoadCheckIssue(
                        severity="warning",
                        code="HIGH_BASE_SHEAR_RATIO",
                        message=(
                            f"Seismic base shear ratio V{axis_label}/W = {ratio * 100:.1f}% exceeds 35%. "
                            "Verify R-factor, near-source distance, and soil profile."
                        ),
                    )
                )
            elif ratio < 0.03:
                issues.append(
                    LoadCheckIssue(
                        severity="warning",
                        code="LOW_BASE_SHEAR_RATIO",
                        message=(
                            f"Seismic base shear ratio V{axis_label}/W = {ratio * 100:.2f}% is below 3%. "
                            "Verify seismic zone, Ca/Cv, and building height."
                        ),
                    )
                )

    if lg.wind.basic_wind_speed_kph < 150.0:
        issues.append(
            LoadCheckIssue(
                severity="warning",
                code="LOW_WIND_SPEED",
                message=(
                    f"Basic wind speed ({lg.wind.basic_wind_speed_kph:.0f} kph) is below 150 kph. "
                    "Verify against NSCP 2015 Figure 207A.5-1A/B for the project site."
                ),
            )
        )

    # Check if foundation supports sit at multiple elevations (stepped/hillside foundation) with Kzt == 1.0
    support_elevs = sorted({round(n.y_m, 2) for n in model.nodes if n.is_support})
    if len(support_elevs) > 1 and abs(lg.wind.topographic_kzt - 1.0) <= 1e-4:
        issues.append(
            LoadCheckIssue(
                severity="warning",
                code="STEPPED_FOUNDATION_KZT",
                message=(
                    f"Supports exist at {len(support_elevs)} elevations (stepped/hillside foundation) "
                    "while wind topographic factor Kzt = 1.0. Verify if ridge/escarpment Kzt > 1.0 applies."
                ),
            )
        )

    # Roof level check: non-deck roof level with is_roof=False
    for fl in lg.floor_loads:
        up = fl.floor_level.upper()
        is_deck = "DECK" in up or up.strip() in ("RD", "R-DECK")
        if ("ROOF" in up or up.strip() in ("RF", "R")) and not is_deck and not fl.is_roof:
            issues.append(
                LoadCheckIssue(
                    severity="warning",
                    code="ROOF_NOT_MARKED_AS_ROOF",
                    message=(
                        f"Level '{fl.floor_level}' looks like a roof level but 'Roof?' is unchecked "
                        "(live load currently routed to LL instead of LR)."
                    ),
                    floor_level=fl.floor_level,
                )
            )

    if not lg.options.include_lrfd_combos and not lg.options.include_asd_combos:
        issues.append(
            LoadCheckIssue(
                severity="warning",
                code="NO_LOAD_COMBINATIONS",
                message="Both LRFD and ASD load combinations are disabled. Enable at least one set before STAAD export.",
            )
        )

    return issues
