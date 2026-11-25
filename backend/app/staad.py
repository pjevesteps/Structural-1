from pathlib import Path
import re
from .models import ProjectState
from .geometry import build_node_map


def _safe_group_name(prefix: str, label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label.upper()).strip("_")
    return f"{prefix}{slug or 'GROUP'}"


def _wrap_staad_ids(
    ids: list[int],
    prefix: str = "",
    suffix: str = "",
    max_len: int = 72,
) -> list[str]:
    """
    Format a STAAD.Pro member/joint ID command with '-' line continuations
    whenever a line would exceed max_len (72) characters.
    """
    tokens = [str(x) for x in ids]
    if not tokens:
        return []

    lines: list[str] = []
    cur = prefix.rstrip()

    for tok in tokens:
        candidate = f"{cur} {tok}" if cur else tok
        # Leave 2 chars room for trailing ' -' when more tokens or suffix may follow
        if cur and len(candidate) > max_len - 2:
            lines.append(f"{cur} -")
            cur = tok
        else:
            cur = candidate

    if suffix:
        cand_with_suffix = f"{cur} {suffix}" if cur else suffix
        if cur and len(cand_with_suffix) > max_len:
            lines.append(f"{cur} -")
            lines.append(suffix)
        else:
            lines.append(cand_with_suffix)
    elif cur:
        lines.append(cur)

    return lines


def write_staad_preview(project: ProjectState, destination: Path):
    """
    Write STAAD.Pro .std file with real newlines and 72-character '-' line continuations.
    - When Phase 4 member_sizing (sections + member_assignments) is populated, emits full
      DEFINE MATERIAL, MEMBER PROPERTY (PRIS YD/ZD), CONSTANTS (MATERIAL + BETA),
      MEMBER CRACKED, MEMBER RELEASE, per-section groups, and SUPPORTS.
    - When Phase 3 structural_model is populated without sizing, emits Phase 3 geometry + groups + supports.
    - Falls back cleanly to Phase 1 column/beam mapping when structural_model is empty.
    """
    model = project.structural_model
    sizing = project.member_sizing
    has_sizing = bool(
        model.nodes
        and model.members
        and sizing.sections
        and sizing.member_assignments
    )

    if model.nodes and model.members:
        lines = ["STAAD SPACE", "UNIT METER KN", "JOINT COORDINATES"]
        for n in sorted(model.nodes, key=lambda x: x.node_id):
            lines.append(f"{n.node_id} {n.x_m:.3f} {n.y_m:.3f} {n.z_m:.3f};")

        lines.append("MEMBER INCIDENCES")
        for m in sorted(model.members, key=lambda x: x.member_id):
            lines.append(f"{m.member_id} {m.start_node} {m.end_node};")

        # Member groups
        col_ids = sorted(m.member_id for m in model.members if m.member_type == "column")
        beam_ids = sorted(
            m.member_id
            for m in model.members
            if m.member_type in ("primary_beam", "secondary_beam", "trimmer_beam")
        )
        tie_ids = sorted(m.member_id for m in model.members if m.member_type == "tie_beam")

        level_groups: dict[str, list[int]] = {}
        for m in sorted(model.members, key=lambda x: x.member_id):
            if m.floor_level:
                level_groups.setdefault(m.floor_level, []).append(m.member_id)

        sec_by_id = {s.section_id: s for s in sizing.sections}
        valid_mids = {m.member_id for m in model.members}
        section_member_map: dict[str, list[int]] = {}
        if has_sizing:
            for asgn in sorted(sizing.member_assignments, key=lambda a: a.member_id):
                if asgn.member_id in valid_mids and asgn.section_id in sec_by_id:
                    section_member_map.setdefault(asgn.section_id, []).append(asgn.member_id)

        if col_ids or beam_ids or tie_ids or level_groups or section_member_map:
            lines.append("START GROUP DEFINITION")
            lines.append("MEMBER")
            if col_ids:
                lines.extend(_wrap_staad_ids(col_ids, prefix="_COLUMNS"))
            if beam_ids:
                lines.extend(_wrap_staad_ids(beam_ids, prefix="_BEAMS"))
            if tie_ids:
                lines.extend(_wrap_staad_ids(tie_ids, prefix="_TIE_BEAMS"))
            for lvl_name, mids in level_groups.items():
                lines.extend(_wrap_staad_ids(mids, prefix=_safe_group_name("_LVL_", lvl_name)))
            if has_sizing:
                for sec in sizing.sections:
                    mids = section_member_map.get(sec.section_id, [])
                    if mids:
                        lines.extend(_wrap_staad_ids(mids, prefix=_safe_group_name("_SEC_", sec.section_id)))
            lines.append("END GROUP DEFINITION")

        # Phase 4: Material, Section Properties, Constants/Beta, Cracked Modifiers, Releases
        if has_sizing:
            mat = sizing.material
            mat_name = re.sub(r"[^A-Za-z0-9_]+", "_", (mat.name or "CONCRETE").upper()).strip("_") or "CONCRETE"
            lines.append("DEFINE MATERIAL START")
            lines.append(f"ISOTROPIC {mat_name}")
            lines.append(f"E {mat.ec_kn_m2:.2f}")
            lines.append(f"POISSON {mat.poisson:.3f}")
            lines.append(f"DENSITY {mat.density_kn_m3:.3f}")
            lines.append(f"ALPHA {mat.alpha:.1e}")
            lines.append(f"DAMP {mat.damp:.3f}")
            lines.append("TYPE CONCRETE")
            lines.append(f"STRENGTH FCU {mat.fc_mpa * 1000.0:.1f}")
            lines.append("END DEFINE MATERIAL")

            lines.append("MEMBER PROPERTY")
            for sec in sizing.sections:
                mids = section_member_map.get(sec.section_id, [])
                if not mids:
                    continue
                yd_m = sec.depth_mm / 1000.0
                zd_m = sec.width_mm / 1000.0
                lines.append(f"* Section {sec.section_id}: {sec.width_mm:.0f}x{sec.depth_mm:.0f} mm")
                lines.extend(
                    _wrap_staad_ids(
                        mids,
                        suffix=f"PRIS YD {yd_m:.3f} ZD {zd_m:.3f}",
                    )
                )

            lines.append("CONSTANTS")
            lines.append(f"MATERIAL {mat_name} ALL")
            beta_groups: dict[float, list[int]] = {}
            for asgn in sorted(sizing.member_assignments, key=lambda a: a.member_id):
                if asgn.member_id in valid_mids and abs(asgn.beta_angle_deg) > 1e-6:
                    beta_groups.setdefault(round(asgn.beta_angle_deg, 2), []).append(asgn.member_id)
            for deg, mids in sorted(beta_groups.items()):
                deg_str = f"{deg:g}"
                lines.extend(_wrap_staad_ids(mids, prefix=f"BETA {deg_str} MEMB"))

            if sizing.options.include_cracked_modifiers:
                cracked_groups: dict[tuple[float, float, float], list[int]] = {}
                for asgn in sorted(sizing.member_assignments, key=lambda a: a.member_id):
                    if asgn.member_id not in valid_mids:
                        continue
                    sec = sec_by_id.get(asgn.section_id)
                    if sec is None:
                        continue
                    key = (round(sec.cracked_ix, 2), round(sec.cracked_iy, 2), round(sec.cracked_iz, 2))
                    cracked_groups.setdefault(key, []).append(asgn.member_id)
                if cracked_groups:
                    lines.append("MEMBER CRACKED")
                    for (ix, iy, iz), mids in cracked_groups.items():
                        lines.extend(
                            _wrap_staad_ids(
                                mids,
                                suffix=f"REDUCTION RIX {ix:.2f} RIY {iy:.2f} RIZ {iz:.2f}",
                            )
                        )

            start_rel = sorted(
                a.member_id
                for a in sizing.member_assignments
                if a.member_id in valid_mids and a.release_start_moment
            )
            end_rel = sorted(
                a.member_id
                for a in sizing.member_assignments
                if a.member_id in valid_mids and a.release_end_moment
            )
            if start_rel or end_rel:
                lines.append("MEMBER RELEASE")
                if start_rel:
                    lines.extend(_wrap_staad_ids(start_rel, suffix="START MY MZ"))
                if end_rel:
                    lines.extend(_wrap_staad_ids(end_rel, suffix="END MY MZ"))

        # Supports
        fixed_ids = sorted(n.node_id for n in model.nodes if n.is_support and n.support_type == "FIXED")
        pinned_ids = sorted(n.node_id for n in model.nodes if n.is_support and n.support_type == "PINNED")
        if fixed_ids or pinned_ids:
            lines.append("SUPPORTS")
            if fixed_ids:
                lines.extend(_wrap_staad_ids(fixed_ids, suffix="FIXED"))
            if pinned_ids:
                lines.extend(_wrap_staad_ids(pinned_ids, suffix="PINNED"))

        lines += ["FINISH", ""]
        content = "\n".join(lines)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return {
            "path": str(destination),
            "nodes": len(model.nodes),
            "members": len(model.members),
            "supports": len(fixed_ids) + len(pinned_ids),
            "source": "structural_model_with_sizing" if has_sizing else "structural_model",
            "std_preview": content,
            "notice": (
                "STAAD.Pro geometry + preliminary member properties, material, cracked modifiers & releases exported; "
                "loads and design parameters required before analysis."
                if has_sizing
                else "Geometry-only STAAD.Pro preview from analytical model; sections and loads required before analysis."
            ),
        }

    # Phase 1 fallback when structural_model is empty
    levels, node_map, joints = build_node_map(project)
    lines = ["STAAD SPACE", "UNIT METER KN", "JOINT COORDINATES"]
    lines += [j + ";" for j in joints]
    lines.append("MEMBER INCIDENCES")
    member_id, count = 1, 0
    for lower, upper in zip(levels, levels[1:]):
        for col in project.columns:
            lines.append(f"{member_id} {node_map[(col.id, lower.id)]} {node_map[(col.id, upper.id)]};")
            member_id += 1
            count += 1
    for beam in project.beams:
        lines.append(f"{member_id} {node_map[(beam.start_col, beam.level)]} {node_map[(beam.end_col, beam.level)]};")
        member_id += 1
        count += 1
    lines += ["FINISH", ""]
    content = "\n".join(lines)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return {
        "path": str(destination),
        "nodes": len(joints),
        "members": count,
        "source": "phase1_fallback",
        "std_preview": content,
        "notice": "Geometry-only preview; not ready for engineering analysis.",
    }


def simulate_placeholder(project: ProjectState):
    return {
        "mode": "PLACEHOLDER",
        "status": "NOT_ANALYZED",
        "project_id": project.project_info.project_id,
        "message": "No solver was run. No engineering results are available.",
    }
