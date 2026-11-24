from pathlib import Path
import re
from .models import ProjectState
from .geometry import build_node_map


def _safe_group_name(label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label.upper()).strip("_")
    return f"_LVL_{slug or 'FLOOR'}"


def _format_id_list(ids: list[int], per_line: int = 12) -> list[str]:
    chunks = []
    for i in range(0, len(ids), per_line):
        chunks.append(" ".join(str(x) for x in ids[i : i + per_line]))
    return chunks


def write_staad_preview(project: ProjectState, destination: Path):
    """
    Write geometry-only STAAD.Pro .std preview with real newlines.
    Uses Phase 3 analytical structural_model when populated (with START GROUP DEFINITION
    and SUPPORTS); falls back cleanly to Phase 1 column/beam mapping when empty.
    """
    model = project.structural_model
    if model.nodes and model.members:
        lines = ["STAAD SPACE", "UNIT METER KN", "JOINT COORDINATES"]
        for n in sorted(model.nodes, key=lambda x: x.node_id):
            lines.append(f"{n.node_id} {n.x_m:.3f} {n.y_m:.3f} {n.z_m:.3f};")

        lines.append("MEMBER INCIDENCES")
        for m in sorted(model.members, key=lambda x: x.member_id):
            lines.append(f"{m.member_id} {m.start_node} {m.end_node};")

        # Member groups
        col_ids = [m.member_id for m in model.members if m.member_type == "column"]
        beam_ids = [
            m.member_id
            for m in model.members
            if m.member_type in ("primary_beam", "secondary_beam", "trimmer_beam")
        ]
        tie_ids = [m.member_id for m in model.members if m.member_type == "tie_beam"]

        level_groups: dict[str, list[int]] = {}
        for m in model.members:
            if m.floor_level:
                level_groups.setdefault(m.floor_level, []).append(m.member_id)

        if col_ids or beam_ids or tie_ids or level_groups:
            lines.append("START GROUP DEFINITION")
            lines.append("MEMBER")
            if col_ids:
                lines.append(f"_COLUMNS {' '.join(str(x) for x in col_ids)}")
            if beam_ids:
                lines.append(f"_BEAMS {' '.join(str(x) for x in beam_ids)}")
            if tie_ids:
                lines.append(f"_TIE_BEAMS {' '.join(str(x) for x in tie_ids)}")
            for lvl_name, mids in level_groups.items():
                lines.append(f"{_safe_group_name(lvl_name)} {' '.join(str(x) for x in mids)}")
            lines.append("END GROUP DEFINITION")

        # Supports
        fixed_ids = sorted(n.node_id for n in model.nodes if n.is_support and n.support_type == "FIXED")
        pinned_ids = sorted(n.node_id for n in model.nodes if n.is_support and n.support_type == "PINNED")
        if fixed_ids or pinned_ids:
            lines.append("SUPPORTS")
            if fixed_ids:
                lines.append(f"{' '.join(str(x) for x in fixed_ids)} FIXED")
            if pinned_ids:
                lines.append(f"{' '.join(str(x) for x in pinned_ids)} PINNED")

        lines += ["FINISH", ""]
        content = "\n".join(lines)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return {
            "path": str(destination),
            "nodes": len(model.nodes),
            "members": len(model.members),
            "supports": len(fixed_ids) + len(pinned_ids),
            "source": "structural_model",
            "std_preview": content,
            "notice": "Geometry-only STAAD.Pro preview from analytical model; sections and loads required before analysis.",
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
