import json
import os
from pathlib import Path
from .models import ProjectState

_ENV_DATA_ROOT = os.environ.get("STRUCTURAL_AI_DATA_DIR", "").strip()
_DEFAULT_DATA_ROOT = (
    Path(_ENV_DATA_ROOT).resolve()
    if _ENV_DATA_ROOT
    else Path(__file__).resolve().parents[2] / "data"
)

DATA_DIR = _DEFAULT_DATA_ROOT / "projects"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_exports_dir() -> Path:
    exports_dir = DATA_DIR.parent / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    return exports_dir


def project_path(project_id: str):
    safe = "".join(ch for ch in project_id if ch.isalnum() or ch in "-_")
    if not safe or safe != project_id:
        raise ValueError("Project ID may contain only letters, numbers, hyphens and underscores.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{safe}.json"


def save_project(project: ProjectState):
    project_path(project.project_info.project_id).write_text(
        project.model_dump_json(indent=2), encoding="utf-8"
    )


def load_project(project_id: str):
    path = project_path(project_id)
    if not path.exists():
        raise FileNotFoundError(project_id)
    return ProjectState.model_validate(json.loads(path.read_text(encoding="utf-8")))


def list_projects():
    items = []
    if not DATA_DIR.exists():
        return items
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            info = dict(raw["project_info"])
            info["revision_count"] = len(raw.get("revisions") or [])
            lg = raw.get("load_generation") or {}
            info["seismic_zone"] = (lg.get("seismic") or {}).get("zone", 4)
            sm = raw.get("structural_model") or {}
            ms = raw.get("member_sizing") or {}
            ad = raw.get("analysis_design") or {}
            cd = raw.get("cad_drawings") or {}
            rp = raw.get("reporting") or {}
            info["phases_done"] = {
                "01": True,
                "02": bool((raw.get("columns") or []) or len(raw.get("levels") or []) > 1),
                "03": bool(sm.get("nodes")),
                "04": bool(ms.get("member_assignments")),
                "05": bool(lg.get("generated_at")),
                "06": bool(ad.get("generated_at")),
                "07": bool(cd.get("generated_at")),
                "08": bool(rp.get("generated_at")),
            }
            items.append(info)
        except Exception:
            continue
    return items
