from datetime import datetime, timezone
import gc
import json
import os
from pathlib import Path
import shutil
import stat
from .models import ProjectState

_DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
_DEFAULT_PROJECTS_DIR = _DEFAULT_DATA_ROOT / "projects"

# Kept as module attribute for backward compatibility with tests that monkeypatch storage.DATA_DIR
DATA_DIR = _DEFAULT_PROJECTS_DIR


def get_data_dir() -> Path:
    """
    Resolve the persistent data root dynamically at runtime:
      1. Honor monkeypatched storage.DATA_DIR (legacy test isolation).
      2. Otherwise read DATA_DIR (Railway volume) or STRUCTURAL_AI_DATA_DIR env var.
      3. Default to <repo>/data.
    """
    if DATA_DIR != _DEFAULT_PROJECTS_DIR:
        return Path(DATA_DIR).resolve().parent
    env_val = (os.getenv("DATA_DIR") or os.getenv("STRUCTURAL_AI_DATA_DIR") or "").strip()
    if env_val:
        return Path(env_val).resolve()
    return _DEFAULT_DATA_ROOT.resolve()


def get_projects_dir() -> Path:
    if DATA_DIR != _DEFAULT_PROJECTS_DIR:
        p = Path(DATA_DIR).resolve()
    else:
        p = get_data_dir() / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure_data_dirs() -> Path:
    """Create persistent volume root and required subdirectories on startup or before writes."""
    root = get_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    get_projects_dir().mkdir(parents=True, exist_ok=True)
    (root / "imports").mkdir(parents=True, exist_ok=True)
    (root / "exports").mkdir(parents=True, exist_ok=True)
    (root / "revisions").mkdir(parents=True, exist_ok=True)
    return root


def get_exports_dir() -> Path:
    exports_dir = get_data_dir() / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    return exports_dir


def validate_project_id(project_id: str) -> str:
    """
    Strictly validate project_id as a single safe path segment:
    rejects empty strings, '..', '/', '\\', whitespace, or any character outside [A-Za-z0-9_-].
    """
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("Project ID must be a non-empty string.")
    if ".." in project_id or "/" in project_id or "\\" in project_id or any(ch.isspace() for ch in project_id):
        raise ValueError("Project ID contains invalid path characters or whitespace.")
    safe = "".join(ch for ch in project_id if ch.isalnum() or ch in "-_")
    if not safe or safe != project_id:
        raise ValueError("Project ID may contain only letters, numbers, hyphens and underscores.")
    return safe


def project_path(project_id: str) -> Path:
    safe = validate_project_id(project_id)
    base = get_projects_dir().resolve()
    target = (base / f"{safe}.json").resolve()
    if not target.is_relative_to(base):
        raise ValueError("Project path escapes storage directory.")
    return target


def save_project(project: ProjectState):
    project_path(project.project_info.project_id).write_text(
        project.model_dump_json(indent=2), encoding="utf-8"
    )


def load_project(project_id: str):
    path = project_path(project_id)
    if not path.exists():
        raise FileNotFoundError(project_id)
    return ProjectState.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _derive_stage(phases_done: dict[str, bool]) -> tuple[str, str]:
    stage_labels = [
        ("08", "Module 08 · Reports & BOM", "reports"),
        ("07", "Module 07 · CAD Drawings", "drawings"),
        ("06", "Module 06 · RC Design", "designed"),
        ("05", "Module 05 · NSCP Loads", "loaded"),
        ("04", "Module 04 · Member Sizing", "sized"),
        ("03", "Module 03 · 3D Geometry", "modeled"),
        ("02", "Module 02 · Plan Import", "imported"),
        ("01", "Module 01 · Setup", "setup"),
    ]
    for key, label, status in stage_labels:
        if phases_done.get(key):
            return label, status
    return "Module 01 · Setup", "setup"


def list_projects():
    items = []
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return items
    for path in projects_dir.glob("*.json"):
        try:
            mtime = path.stat().st_mtime
            updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except OSError:
            updated_at = datetime.now(timezone.utc).isoformat()

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            # Validate full schema so corrupted/broken JSONs are surfaced cleanly instead of crashing later
            ProjectState.model_validate(raw)
            info = dict(raw["project_info"])
            info["revision_count"] = len(raw.get("revisions") or [])
            lg = raw.get("load_generation") or {}
            info["seismic_zone"] = (lg.get("seismic") or {}).get("zone", 4)
            sm = raw.get("structural_model") or {}
            ms = raw.get("member_sizing") or {}
            ad = raw.get("analysis_design") or {}
            cd = raw.get("cad_drawings") or {}
            rp = raw.get("reporting") or {}
            phases_done = {
                "01": True,
                "02": bool((raw.get("columns") or []) or len(raw.get("levels") or []) > 1),
                "03": bool(sm.get("nodes")),
                "04": bool(ms.get("member_assignments")),
                "05": bool(lg.get("generated_at")),
                "06": bool(ad.get("generated_at")),
                "07": bool(cd.get("generated_at")),
                "08": bool(rp.get("generated_at")),
            }
            stage_label, status_key = _derive_stage(phases_done)
            info["phases_done"] = phases_done
            info["updated_at"] = updated_at
            info["stage"] = stage_label
            info["status"] = status_key
            info["corrupted"] = False
            info["error"] = None
            items.append(info)
        except Exception as exc:
            err_msg = str(exc).splitlines()[0][:180] if str(exc) else "Invalid project JSON"
            items.append(
                {
                    "project_id": path.stem,
                    "project_name": f"{path.stem} (Corrupted)",
                    "location": "",
                    "design_code": "",
                    "revision_count": 0,
                    "seismic_zone": 4,
                    "phases_done": {},
                    "updated_at": updated_at,
                    "stage": "Corrupted",
                    "status": "corrupted",
                    "corrupted": True,
                    "error": err_msg,
                }
            )

    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return items


def _handle_remove_readonly(func, path_str, exc_info):
    try:
        os.chmod(path_str, stat.S_IWRITE)
        func(path_str)
    except OSError:
        pass


def delete_project(project_id: str) -> dict:
    """
    Safely delete a project's primary .json and all project-scoped artifact directories
    (imports, revisions, exports) after strict single-segment ID validation and
    Path.resolve().is_relative_to(...) confinement checks.
    """
    safe = validate_project_id(project_id)
    projects_base = get_projects_dir().resolve()
    data_root = get_data_dir().resolve()

    proj_file = (projects_base / f"{safe}.json").resolve()
    if not proj_file.is_relative_to(projects_base):
        raise ValueError("Project file path escapes projects directory.")

    scoped_dirs = [
        (data_root / "imports" / safe).resolve(),
        (data_root / "revisions" / safe).resolve(),
        (data_root / "exports" / safe).resolve(),
    ]
    for d in scoped_dirs:
        if not d.is_relative_to(data_root):
            raise ValueError("Project artifact directory escapes data root.")

    exports_root = (data_root / "exports").resolve()
    export_files: list[Path] = []
    if exports_root.exists() and exports_root.is_relative_to(data_root):
        for candidate in exports_root.glob(f"{safe}_*"):
            resolved = candidate.resolve()
            if resolved.is_relative_to(exports_root):
                export_files.append(resolved)

    exists_any = (
        proj_file.exists()
        or any(d.exists() for d in scoped_dirs)
        or bool(export_files)
    )
    if not exists_any:
        raise FileNotFoundError(safe)

    # Release any uncollected PyMuPDF / file handles on Windows before unlinking
    gc.collect()

    if proj_file.exists():
        proj_file.unlink()

    for d in scoped_dirs:
        if d.exists():
            shutil.rmtree(d, onerror=_handle_remove_readonly)

    for ef in export_files:
        if ef.is_file():
            try:
                ef.unlink()
            except OSError:
                pass
        elif ef.is_dir():
            shutil.rmtree(ef, onerror=_handle_remove_readonly)

    return {"deleted": True, "project_id": safe}
