import json
from pathlib import Path
from .models import ProjectState

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "projects"
DATA_DIR.mkdir(parents=True, exist_ok=True)

def project_path(project_id: str):
    safe = "".join(ch for ch in project_id if ch.isalnum() or ch in "-_")
    if not safe or safe != project_id:
        raise ValueError("Project ID may contain only letters, numbers, hyphens and underscores.")
    return DATA_DIR / f"{safe}.json"

def save_project(project: ProjectState):
    project_path(project.project_info.project_id).write_text(project.model_dump_json(indent=2), encoding="utf-8")

def load_project(project_id: str):
    path = project_path(project_id)
    if not path.exists():
        raise FileNotFoundError(project_id)
    return ProjectState.model_validate(json.loads(path.read_text(encoding="utf-8")))

def list_projects():
    items = []
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8"))["project_info"])
        except Exception:
            continue
    return items
