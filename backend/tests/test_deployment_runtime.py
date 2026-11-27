from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.main import app


def test_health_and_dynamic_data_dir_override(tmp_path, monkeypatch):
    """
    Verify:
      1. DATA_DIR env override dynamically redirects get_data_dir() at runtime.
      2. Startup lifespan / writes initialize subdirectories on a fresh empty volume.
      3. GET /api/health returns 200 with status='ok' and the active data_dir.
      4. Saving and listing a project persists strictly inside the temp DATA_DIR.
    """
    fresh_vol = tmp_path / "railway_vol"
    assert not fresh_vol.exists()
    monkeypatch.setenv("DATA_DIR", str(fresh_vol))

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200, health.text
        body = health.json()
        assert body["status"] == "ok"
        assert Path(body["data_dir"]).resolve() == fresh_vol.resolve()
        assert (fresh_vol / "projects").is_dir()
        assert (fresh_vol / "imports").is_dir()
        assert (fresh_vol / "exports").is_dir()

        payload = {
            "project_info": {
                "project_id": "CLOUD-01",
                "project_name": "Cloud Volume Test",
                "location": "Manila",
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
        r_save = client.post("/api/projects", json=payload)
        assert r_save.status_code == 200, r_save.text
        assert (fresh_vol / "projects" / "CLOUD-01.json").is_file()

        r_list = client.get("/api/projects")
        assert r_list.status_code == 200
        assert any(p["project_id"] == "CLOUD-01" for p in r_list.json())


def test_api_nonexistent_returns_404_json_not_index_html(tmp_path, monkeypatch):
    """
    Verify /api/nonexistent always returns 404 JSON even when frontend/dist is built and mounted.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "vol2"))
    client = TestClient(app)

    r_missing = client.get("/api/nonexistent")
    assert r_missing.status_code == 404
    assert "application/json" in (r_missing.headers.get("content-type") or "")
    assert "detail" in r_missing.json()

    r_missing_nested = client.get("/api/does/not/exist")
    assert r_missing_nested.status_code == 404
    assert "application/json" in (r_missing_nested.headers.get("content-type") or "")
