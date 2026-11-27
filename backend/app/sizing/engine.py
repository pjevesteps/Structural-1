from datetime import datetime, timezone
import math

from ..models import (
    MaterialProperties,
    MemberPropertyAssignment,
    MemberSizingState,
    ProjectState,
    SectionDefinition,
    SizingOptions,
    SlabPropertyAssignment,
)
from ..geometry.generator import _is_roof_framing_level_id
from .validator import validate_member_sizing

COL_COLORS = ("#38bdf8", "#0ea5e9", "#7dd3fc", "#0284c7")
PRIMARY_BEAM_COLORS = ("#45c9a8", "#10b981", "#34d399", "#14b8a6", "#2dd4bf")
TIE_BEAM_COLORS = ("#f59e0b", "#d97706")
SEC_BEAM_COLORS = ("#fb923c", "#c084fc", "#f97316")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_ec_kn_m2(fc_mpa: float) -> float:
    """NSCP 2015 / ACI 318 normal-weight concrete elastic modulus Ec = 4700 * sqrt(f'c) MPa in kN/m^2."""
    return round(4700.0 * math.sqrt(max(fc_mpa, 1.0)) * 1000.0, 2)


def _round_up(value: float, increment: float) -> float:
    inc = max(float(increment), 1.0)
    return float(math.ceil(value / inc) * inc)


def auto_size_members(
    project: ProjectState,
    material: MaterialProperties | None = None,
    options: SizingOptions | None = None,
    preserve_manual_overrides: bool = False,
) -> MemberSizingState:
    """
    Generate preliminary NSCP 2015 concrete sections, member assignments, cracked section modifiers,
    moment releases, and slab panel thicknesses from Phase 3 analytical geometry.
    """
    model = project.structural_model
    if not model.members:
        raise ValueError(
            "Structural geometry has not been generated yet. Run Module 03 (Generate 3D Frame) first."
        )

    mat = (
        material.model_copy(deep=True)
        if material is not None
        else project.member_sizing.material.model_copy(deep=True)
    )
    mat.ec_kn_m2 = compute_ec_kn_m2(mat.fc_mpa)
    opts = (
        options.model_copy(deep=True)
        if options is not None
        else project.member_sizing.options.model_copy(deep=True)
    )
    inc = float(opts.round_increment_mm)

    levels = sorted(project.levels, key=lambda lv: lv.elevation_m)
    level_idx_by_id = {lv.id: idx for idx, lv in enumerate(levels)}
    num_stories = max(1, len(levels) - 1)
    node_by_id = {n.node_id: n for n in model.nodes}

    prev_sizing = project.member_sizing
    prev_sec_by_id = {s.section_id: s for s in prev_sizing.sections}
    prev_manual_members = (
        {a.member_id: a for a in prev_sizing.member_assignments if a.manual_override}
        if preserve_manual_overrides
        else {}
    )
    prev_manual_slabs = (
        {s.panel_id: s for s in prev_sizing.slab_assignments if s.manual_override}
        if preserve_manual_overrides
        else {}
    )

    sections: list[SectionDefinition] = []
    section_ids_seen: set[str] = set()
    member_assignments: list[MemberPropertyAssignment] = []

    def add_section(sec: SectionDefinition) -> None:
        if sec.section_id not in section_ids_seen:
            sections.append(sec)
            section_ids_seen.add(sec.section_id)

    # --- 1. Column Preliminary Sizing (tiered by story elevation) ---
    col_members = [m for m in model.members if m.member_type == "column"]
    if col_members:
        num_tiers = 3 if num_stories >= 4 else (2 if num_stories >= 2 else 1)

        def col_story_index(m) -> int:
            if getattr(m, "is_pedestal", False):
                return 0
            if m.floor_level in level_idx_by_id:
                return min(level_idx_by_id[m.floor_level], num_stories - 1)
            na = node_by_id.get(m.start_node)
            nb = node_by_id.get(m.end_node)
            low_y = min(na.y_m if na else 0.0, nb.y_m if nb else 0.0)
            best_i = min(range(len(levels)), key=lambda i: abs(levels[i].elevation_m - low_y), default=0)
            return min(best_i, num_stories - 1)

        def story_to_tier(s_idx: int) -> int:
            if num_tiers == 1:
                return 0
            return min(num_tiers - 1, int((s_idx * num_tiers) / num_stories))

        tier_members: dict[int, list] = {}
        for m in col_members:
            t_idx = story_to_tier(col_story_index(m))
            tier_members.setdefault(t_idx, []).append(m)

        # Compute practical square column sizes per tier (monotonically non-increasing going upward)
        min_col = 300.0 if opts.seismic_smrf_zone4 else 250.0
        tier_dims: dict[int, float] = {}
        for t_idx in sorted(tier_members.keys()):
            max_above = max(max(1, num_stories - col_story_index(m)) for m in tier_members[t_idx])
            raw_dim = 250.0 + 35.0 * max_above
            dim = max(min_col, _round_up(raw_dim, inc))
            tier_dims[t_idx] = dim

        # Group identical tier dimensions cleanly into C1, C2, C3... (or keep distinct tiers C1/C2/C3 when sizes differ)
        tier_sec_id: dict[int, str] = {}
        dim_to_sec_id: dict[float, str] = {}
        c_counter = 1
        for t_idx in sorted(tier_dims.keys()):
            dim = tier_dims[t_idx]
            if dim in dim_to_sec_id:
                tier_sec_id[t_idx] = dim_to_sec_id[dim]
                continue
            sid = f"C{c_counter}"
            dim_to_sec_id[dim] = sid
            tier_sec_id[t_idx] = sid
            add_section(
                SectionDefinition(
                    section_id=sid,
                    label=f"{sid} ({int(dim)}x{int(dim)})",
                    category="column",
                    width_mm=dim,
                    depth_mm=dim,
                    material_name=mat.name,
                    color_hex=COL_COLORS[(c_counter - 1) % len(COL_COLORS)],
                    cracked_ix=0.70,
                    cracked_iy=0.70,
                    cracked_iz=0.70,
                )
            )
            c_counter += 1

        for t_idx, members_in_tier in tier_members.items():
            sid = tier_sec_id[t_idx]
            for m in members_in_tier:
                if m.member_id in prev_manual_members:
                    kept = prev_manual_members[m.member_id].model_copy(deep=True)
                    if kept.section_id in prev_sec_by_id:
                        add_section(prev_sec_by_id[kept.section_id].model_copy(deep=True))
                    member_assignments.append(kept)
                else:
                    member_assignments.append(
                        MemberPropertyAssignment(
                            member_id=m.member_id,
                            section_id=sid,
                            beta_angle_deg=0.0,
                            release_start_moment=False,
                            release_end_moment=False,
                            manual_override=False,
                        )
                    )

    # --- 2. Beam Preliminary Sizing (grouped by category & span bucket) ---
    def calc_beam_dims(length_m: float, min_h: float, min_b: float) -> tuple[float, float]:
        h = _round_up(max(min_h, (length_m * 1000.0) / opts.beam_span_depth_ratio), inc)
        req_b = max(min_b, 0.5 * h)
        if opts.seismic_smrf_zone4:
            req_b = max(req_b, 0.30 * h)
        b = _round_up(req_b, inc)
        return b, h

    # 2a. Primary beams -> B1, B2, ... (sorted descending by depth, then width)
    primary_beams = [m for m in model.members if m.member_type == "primary_beam"]
    if primary_beams:
        min_b_primary = 250.0 if opts.seismic_smrf_zone4 else 200.0
        beam_dim_map = {m.member_id: calc_beam_dims(m.length_m, 350.0, min_b_primary) for m in primary_beams}
        unique_dims = sorted(set(beam_dim_map.values()), key=lambda pair: (-pair[1], -pair[0]))
        dim_to_bid: dict[tuple[float, float], str] = {}
        for idx, (b, h) in enumerate(unique_dims, start=1):
            sid = f"B{idx}"
            dim_to_bid[(b, h)] = sid
            add_section(
                SectionDefinition(
                    section_id=sid,
                    label=f"{sid} ({int(b)}x{int(h)})",
                    category="primary_beam",
                    width_mm=b,
                    depth_mm=h,
                    material_name=mat.name,
                    color_hex=PRIMARY_BEAM_COLORS[(idx - 1) % len(PRIMARY_BEAM_COLORS)],
                    cracked_ix=0.35,
                    cracked_iy=0.35,
                    cracked_iz=0.35,
                )
            )
        for m in primary_beams:
            if m.member_id in prev_manual_members:
                kept = prev_manual_members[m.member_id].model_copy(deep=True)
                if kept.section_id in prev_sec_by_id:
                    add_section(prev_sec_by_id[kept.section_id].model_copy(deep=True))
                member_assignments.append(kept)
            else:
                member_assignments.append(
                    MemberPropertyAssignment(
                        member_id=m.member_id,
                        section_id=dim_to_bid[beam_dim_map[m.member_id]],
                        beta_angle_deg=0.0,
                        release_start_moment=False,
                        release_end_moment=False,
                        manual_override=False,
                    )
                )

    # 2b. Tie beams -> TB1, TB2, ...
    tie_beams = [m for m in model.members if m.member_type == "tie_beam"]
    if tie_beams:
        min_b_tie = 300.0 if opts.seismic_smrf_zone4 else 250.0
        tie_dim_map = {m.member_id: calc_beam_dims(m.length_m, 400.0, min_b_tie) for m in tie_beams}
        unique_ties = sorted(set(tie_dim_map.values()), key=lambda pair: (-pair[1], -pair[0]))
        dim_to_tid: dict[tuple[float, float], str] = {}
        for idx, (b, h) in enumerate(unique_ties, start=1):
            sid = f"TB{idx}"
            dim_to_tid[(b, h)] = sid
            add_section(
                SectionDefinition(
                    section_id=sid,
                    label=f"{sid} ({int(b)}x{int(h)})",
                    category="tie_beam",
                    width_mm=b,
                    depth_mm=h,
                    material_name=mat.name,
                    color_hex=TIE_BEAM_COLORS[(idx - 1) % len(TIE_BEAM_COLORS)],
                    cracked_ix=0.35,
                    cracked_iy=0.35,
                    cracked_iz=0.35,
                )
            )
        for m in tie_beams:
            if m.member_id in prev_manual_members:
                kept = prev_manual_members[m.member_id].model_copy(deep=True)
                if kept.section_id in prev_sec_by_id:
                    add_section(prev_sec_by_id[kept.section_id].model_copy(deep=True))
                member_assignments.append(kept)
            else:
                member_assignments.append(
                    MemberPropertyAssignment(
                        member_id=m.member_id,
                        section_id=dim_to_tid[tie_dim_map[m.member_id]],
                        beta_angle_deg=0.0,
                        release_start_moment=False,
                        release_end_moment=False,
                        manual_override=False,
                    )
                )

    # 2c. Secondary & trimmer beams -> SB1, SB2, ... (pinned if pin_secondary_beams=True)
    sec_beams = [m for m in model.members if m.member_type in ("secondary_beam", "trimmer_beam")]
    if sec_beams:
        min_b_sec = 250.0 if opts.seismic_smrf_zone4 else 200.0
        sec_dim_map = {m.member_id: calc_beam_dims(m.length_m, 350.0, min_b_sec) for m in sec_beams}
        unique_secs = sorted(set(sec_dim_map.values()), key=lambda pair: (-pair[1], -pair[0]))
        dim_to_sbid: dict[tuple[float, float], str] = {}
        for idx, (b, h) in enumerate(unique_secs, start=1):
            sid = f"SB{idx}"
            dim_to_sbid[(b, h)] = sid
            add_section(
                SectionDefinition(
                    section_id=sid,
                    label=f"{sid} ({int(b)}x{int(h)})",
                    category="secondary_beam",
                    width_mm=b,
                    depth_mm=h,
                    material_name=mat.name,
                    color_hex=SEC_BEAM_COLORS[(idx - 1) % len(SEC_BEAM_COLORS)],
                    cracked_ix=0.35,
                    cracked_iy=0.35,
                    cracked_iz=0.35,
                )
            )
        for m in sec_beams:
            if m.member_id in prev_manual_members:
                kept = prev_manual_members[m.member_id].model_copy(deep=True)
                if kept.section_id in prev_sec_by_id:
                    add_section(prev_sec_by_id[kept.section_id].model_copy(deep=True))
                member_assignments.append(kept)
            else:
                member_assignments.append(
                    MemberPropertyAssignment(
                        member_id=m.member_id,
                        section_id=dim_to_sbid[sec_dim_map[m.member_id]],
                        beta_angle_deg=0.0,
                        release_start_moment=opts.pin_secondary_beams,
                        release_end_moment=opts.pin_secondary_beams,
                        manual_override=False,
                    )
                )

    member_assignments.sort(key=lambda a: a.member_id)

    # --- 3. Slab Panel Preliminary Thickness ---
    include_roof_slab = bool(getattr(model.options, "include_roof_slab", False))
    raw_slabs: list[tuple[str, str, float, float, bool]] = []
    for panel in model.slab_panels:
        if not include_roof_slab and _is_roof_framing_level_id(panel.floor_level):
            continue
        if panel.slab_action == "void":
            raw_slabs.append((panel.panel_id, panel.floor_level, 0.0, 0.0, True))
        elif panel.slab_action == "one_way":
            short_mm = min(panel.lx_m, panel.ly_m) * 1000.0
            min_code = round(max(100.0, short_mm / 20.0), 1)
            t_mm = max(125.0, float(math.ceil(min_code / 10.0) * 10.0))
            raw_slabs.append((panel.panel_id, panel.floor_level, t_mm, min_code, False))
        else:
            perim_mm = 2.0 * (panel.lx_m + panel.ly_m) * 1000.0
            min_code = round(max(100.0, perim_mm / 180.0), 1)
            t_mm = max(125.0, float(math.ceil(min_code / 10.0) * 10.0))
            raw_slabs.append((panel.panel_id, panel.floor_level, t_mm, min_code, False))

    unique_slab_t = sorted({t for _, _, t, _, is_void in raw_slabs if not is_void})
    t_to_tag = {t: f"S{idx}" for idx, t in enumerate(unique_slab_t, start=1)}

    slab_assignments: list[SlabPropertyAssignment] = []
    for panel_id, floor_level, t_mm, min_code, is_void in raw_slabs:
        if panel_id in prev_manual_slabs:
            kept_slab = prev_manual_slabs[panel_id].model_copy(deep=True)
            kept_slab.min_code_thickness_mm = min_code
            slab_assignments.append(kept_slab)
        else:
            slab_assignments.append(
                SlabPropertyAssignment(
                    panel_id=panel_id,
                    floor_level=floor_level,
                    slab_tag="VOID" if is_void else t_to_tag[t_mm],
                    thickness_mm=t_mm,
                    min_code_thickness_mm=min_code,
                    manual_override=False,
                )
            )

    sizing_state = MemberSizingState(
        generated_at=_utc_now(),
        material=mat,
        options=opts,
        sections=sections,
        member_assignments=member_assignments,
        slab_assignments=slab_assignments,
        validation_issues=[],
        engineer_approved=False,
    )

    temp_project = project.model_copy(update={"member_sizing": sizing_state})
    sizing_state.validation_issues = validate_member_sizing(temp_project)
    return sizing_state


def ensure_all_members_sized(project: ProjectState) -> bool:
    """
    Guarantee every analytical member in project.structural_model.members (including
    beams added manually in Module 03 after running Module 04/05) has a valid section
    definition and MemberPropertyAssignment in project.member_sizing.
    Returns True if any missing assignment or default section was created.
    """
    model = project.structural_model
    if not model.members:
        return False

    sizing = project.member_sizing
    mat = sizing.material
    if mat.ec_kn_m2 <= 0:
        mat.ec_kn_m2 = compute_ec_kn_m2(mat.fc_mpa)
    opts = sizing.options

    sec_by_id = {s.section_id: s for s in sizing.sections}
    asgn_by_mid = {a.member_id: a for a in sizing.member_assignments}
    changed = False

    def _find_or_create_default_section(member_type: str) -> str:
        nonlocal changed
        if member_type == "column":
            existing = next((s for s in sizing.sections if s.category == "column"), None)
            if existing is not None:
                return existing.section_id
            sid = "C-AUTO"
            if sid not in sec_by_id:
                dim = 350.0 if opts.seismic_smrf_zone4 else 300.0
                sec = SectionDefinition(
                    section_id=sid,
                    label=f"{sid} ({int(dim)}x{int(dim)})",
                    category="column",
                    width_mm=dim,
                    depth_mm=dim,
                    material_name=mat.name,
                    color_hex=COL_COLORS[0],
                    cracked_ix=0.70,
                    cracked_iy=0.70,
                    cracked_iz=0.70,
                )
                sizing.sections.append(sec)
                sec_by_id[sid] = sec
                changed = True
            return sid

        if member_type == "tie_beam":
            existing = next((s for s in sizing.sections if s.category == "tie_beam"), None)
            if existing is not None:
                return existing.section_id
            sid = "TB-AUTO"
            if sid not in sec_by_id:
                b = 300.0 if opts.seismic_smrf_zone4 else 250.0
                h = 400.0
                sec = SectionDefinition(
                    section_id=sid,
                    label=f"{sid} ({int(b)}x{int(h)})",
                    category="tie_beam",
                    width_mm=b,
                    depth_mm=h,
                    material_name=mat.name,
                    color_hex=TIE_BEAM_COLORS[0],
                    cracked_ix=0.35,
                    cracked_iy=0.35,
                    cracked_iz=0.35,
                )
                sizing.sections.append(sec)
                sec_by_id[sid] = sec
                changed = True
            return sid

        if member_type in ("secondary_beam", "trimmer_beam"):
            existing = next((s for s in sizing.sections if s.category == "secondary_beam"), None)
            if existing is not None:
                return existing.section_id
            sid = "SB-AUTO"
            if sid not in sec_by_id:
                b = 250.0 if opts.seismic_smrf_zone4 else 200.0
                h = 350.0
                sec = SectionDefinition(
                    section_id=sid,
                    label=f"{sid} ({int(b)}x{int(h)})",
                    category="secondary_beam",
                    width_mm=b,
                    depth_mm=h,
                    material_name=mat.name,
                    color_hex=SEC_BEAM_COLORS[0],
                    cracked_ix=0.35,
                    cracked_iy=0.35,
                    cracked_iz=0.35,
                )
                sizing.sections.append(sec)
                sec_by_id[sid] = sec
                changed = True
            return sid

        # primary_beam (default)
        existing = next((s for s in sizing.sections if s.category == "primary_beam"), None)
        if existing is not None:
            return existing.section_id
        sid = "B-AUTO"
        if sid not in sec_by_id:
            b = 250.0 if opts.seismic_smrf_zone4 else 200.0
            h = 400.0
            sec = SectionDefinition(
                section_id=sid,
                label=f"{sid} ({int(b)}x{int(h)})",
                category="primary_beam",
                width_mm=b,
                depth_mm=h,
                material_name=mat.name,
                color_hex=PRIMARY_BEAM_COLORS[0],
                cracked_ix=0.35,
                cracked_iy=0.35,
                cracked_iz=0.35,
            )
            sizing.sections.append(sec)
            sec_by_id[sid] = sec
            changed = True
        return sid

    for m in model.members:
        existing_asgn = asgn_by_mid.get(m.member_id)
        if existing_asgn is not None and existing_asgn.section_id in sec_by_id:
            continue
        sec_id = _find_or_create_default_section(m.member_type)
        is_sec = m.member_type in ("secondary_beam", "trimmer_beam")
        new_asgn = MemberPropertyAssignment(
            member_id=m.member_id,
            section_id=sec_id,
            beta_angle_deg=0.0,
            release_start_moment=bool(is_sec and opts.pin_secondary_beams),
            release_end_moment=bool(is_sec and opts.pin_secondary_beams),
            manual_override=False,
        )
        if existing_asgn is not None:
            sizing.member_assignments = [
                new_asgn if a.member_id == m.member_id else a for a in sizing.member_assignments
            ]
        else:
            sizing.member_assignments.append(new_asgn)
        asgn_by_mid[m.member_id] = new_asgn
        changed = True

    if changed:
        sizing.member_assignments.sort(key=lambda a: a.member_id)
        if sizing.generated_at is None:
            sizing.generated_at = _utc_now()
    return changed
