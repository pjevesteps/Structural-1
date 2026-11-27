from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.main import app
from backend.app.models import ProjectState


def _sample_project_payload(project_id: str, project_name: str) -> dict:
    return {
        "project_info": {
            "project_id": project_id,
            "project_name": project_name,
            "location": "Manila, Philippines",
            "design_code": "NSCP 2015",
            "materials": {
                "fc_mpa": 28.0,
                "fy_mpa": 415.0,
                "concrete_density_kn_m3": 23.54,
            },
        },
        "levels": [{"id": "BASE", "elevation_m": 0.0}],
        "columns": [],
    }


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    projects_dir = data_root / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "DATA_DIR", projects_dir)
    return TestClient(app), data_root, projects_dir


def test_list_projects_handles_valid_and_corrupted_json_without_crashing(isolated_storage):
    """
    Verify GET /api/projects:
      - Returns valid projects with updated_at, stage, status, corrupted=False
      - Surfaces corrupted JSON & schema-invalid JSON with corrupted=True and error reason
      - Never crashes when a bad file exists in data/projects/
    """
    client, _data_root, projects_dir = isolated_storage

    # 1. Save a valid project
    res = client.post("/api/projects", json=_sample_project_payload("VALID-01", "Valid Tower"))
    assert res.status_code == 200, res.text

    # 2. Write a syntactically broken JSON and a Pydantic-invalid JSON
    time.sleep(0.02)
    (projects_dir / "BROKEN-JSON.json").write_text("{not valid json", encoding="utf-8")
    (projects_dir / "BAD-SCHEMA.json").write_text('{"unknown_extra_key": 123}', encoding="utf-8")

    list_res = client.get("/api/projects")
    assert list_res.status_code == 200, list_res.text
    items = list_res.json()
    by_id = {item["project_id"]: item for item in items}

    assert "VALID-01" in by_id
    assert by_id["VALID-01"]["corrupted"] is False
    assert by_id["VALID-01"]["project_name"] == "Valid Tower"
    assert by_id["VALID-01"]["updated_at"]
    assert by_id["VALID-01"]["stage"]

    assert "BROKEN-JSON" in by_id
    assert by_id["BROKEN-JSON"]["corrupted"] is True
    assert by_id["BROKEN-JSON"]["error"]

    assert "BAD-SCHEMA" in by_id
    assert by_id["BAD-SCHEMA"]["corrupted"] is True
    assert by_id["BAD-SCHEMA"]["error"]

    # Deleting a corrupted project via DELETE /api/projects/{id} succeeds cleanly
    del_bad = client.delete("/api/projects/BROKEN-JSON")
    assert del_bad.status_code == 200, del_bad.text
    assert del_bad.json() == {"deleted": True, "project_id": "BROKEN-JSON"}
    assert not (projects_dir / "BROKEN-JSON.json").exists()


def test_delete_project_removes_all_artifacts_returns_404_on_repeat_and_blocks_traversal(isolated_storage):
    """
    Verify DELETE /api/projects/{project_id} and storage.delete_project():
      - Removes primary .json, imports/<id>/, revisions/<id>/, exports/<id>/, and exports/<id>_*
      - Returns 404 on repeat delete
      - Strictly rejects path-traversal and whitespace IDs (both via API and directly)
    """
    client, data_root, projects_dir = isolated_storage
    pid = "CLEAN-DEL-01"

    # Create project + associated artifact directories/files
    storage.save_project(ProjectState.model_validate(_sample_project_payload(pid, "Delete Me")))
    imports_dir = data_root / "imports" / pid / "pdf-abc123"
    imports_dir.mkdir(parents=True, exist_ok=True)
    (imports_dir / "state.json").write_text("{}", encoding="utf-8")

    rev_dir = data_root / "revisions" / pid
    rev_dir.mkdir(parents=True, exist_ok=True)
    (rev_dir / "rev0.json").write_text("{}", encoding="utf-8")

    exp_dir = data_root / "exports"
    exp_dir.mkdir(parents=True, exist_ok=True)
    exp_file = exp_dir / f"{pid}_geometry_preview.std"
    exp_file.write_text("STAAD SPACE\n", encoding="utf-8")

    # Delete via API
    r1 = client.delete(f"/api/projects/{pid}")
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"deleted": True, "project_id": pid}
    assert not (projects_dir / f"{pid}.json").exists()
    assert not (data_root / "imports" / pid).exists()
    assert not (data_root / "revisions" / pid).exists()
    assert not exp_file.exists()

    # Repeat delete -> 404
    r2 = client.delete(f"/api/projects/{pid}")
    assert r2.status_code == 404

    # Path traversal & invalid IDs rejected directly by storage.delete_project()
    for bad_id in ("../secret", "..\\secret", "a/b", "a\\b", "bad id", "", "proj.json", ".."):
        with pytest.raises(ValueError):
            storage.delete_project(bad_id)

    # Path traversal / invalid ID rejected via HTTP API with 400
    r_bad_space = client.delete("/api/projects/bad%20id")
    assert r_bad_space.status_code == 400
    r_bad_dot = client.delete("/api/projects/proj.json")
    assert r_bad_dot.status_code == 400
