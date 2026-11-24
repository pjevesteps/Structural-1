from pathlib import Path
from .models import ProjectState
from .geometry import build_node_map

def write_staad_preview(project: ProjectState, destination: Path):
    """Write geometry only; intentionally not a complete analysis model."""
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")
    return {"path": str(destination), "nodes": len(joints), "members": count,
            "notice": "Geometry-only preview; not ready for engineering analysis."}

def simulate_placeholder(project: ProjectState):
    return {"mode": "PLACEHOLDER", "status": "NOT_ANALYZED",
            "project_id": project.project_info.project_id,
            "message": "No solver was run. No engineering results are available."}
