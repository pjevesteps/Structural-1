from math import hypot
from .models import ProjectState

def plan_to_staad(x_m: float, y_plan_m: float, elevation_m: float):
    """Map plan X/Y and elevation to STAAD global X/Y-up/Z."""
    return (x_m, elevation_m, y_plan_m)

def build_node_map(project: ProjectState):
    levels = sorted(project.levels, key=lambda item: item.elevation_m)
    node_map, joints = {}, []
    node_id = 1
    for level in levels:
        for column in project.columns:
            node_map[(column.id, level.id)] = node_id
            x, y, z = plan_to_staad(column.x_m, column.y_m, level.elevation_m)
            joints.append(f"{node_id} {x:.3f} {y:.3f} {z:.3f}")
            node_id += 1
    return levels, node_map, joints

def infer_span(project: ProjectState, start_col: str, end_col: str):
    cols = {c.id: c for c in project.columns}
    a, b = cols[start_col], cols[end_col]
    return hypot(b.x_m-a.x_m, b.y_m-a.y_m)
