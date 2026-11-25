from __future__ import annotations

import io
import json
from pathlib import Path

import ezdxf
from fastapi.testclient import TestClient
import pytest

from backend.app import main as main_mod
from backend.app import storage as storage_mod
from backend.app.models import (
    Column,
    FloorOpening,
    Level,
    ProjectInfo,
    ProjectMaterialProperties,
    ProjectState,
)


def _make_cad_project(pid: str = "CAD-TEST-01") -> ProjectState:
    return ProjectState(
        project_info=ProjectInfo(
            project_id=pid,
            project_name="Baguio 3-Storey RC Frame CAD",
            location="Baguio City, Benguet",
            design_code="NSCP 2015",
            materials=ProjectMaterialProperties(
                fc_mpa=28.0,
                fy_mpa=415.0,
                concrete_density_kn_m3=23.54,
            ),
        ),
        levels=[
            Level(id="1F", elevation_m=0.0),
            Level(id="2F", elevation_m=3.0),
            Level(id="ROOF", elevation_m=6.0),
        ],
        columns=[
            Column(id="C-A1", grid="A-1", floor_level="1F", x_m=0.0, y_m=0.0, width_mm=400, depth_mm=400),
            Column(id="C-B1", grid="B-1", floor_level="1F", x_m=4.0, y_m=0.0, width_mm=400, depth_mm=400),
            Column(id="C-A2", grid="A-2", floor_level="1F", x_m=0.0, y_m=4.0, width_mm=400, depth_mm=400),
            Column(id="C-B2", grid="B-2", floor_level="1F", x_m=4.0, y_m=4.0, width_mm=400, depth_mm=400),
        ],
        openings=[
            FloorOpening(
                id="OP-1",
                label="STAIR-2F",
                category="stairwell",
                floor_level="2F",
                polygon_m=[[1.0, 1.0], [2.5, 1.0], [2.5, 2.5], [1.0, 2.5]],
                area_sqm=2.25,
            )
        ],
    )


@pytest.fixture()
def cad_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "projects"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage_mod, "DATA_DIR", data_dir)
    return TestClient(main_mod.app), data_dir


def _run_prereqs(client: TestClient, pid: str) -> None:
    p = _make_cad_project(pid)
    assert client.post("/api/projects", json=p.model_dump()).status_code == 200
    assert (
        client.post(
            f"/api/projects/{pid}/geometry/generate",
            json={"default_support_type": "FIXED", "include_base_tie_beams": True, "footing_depth_m": 1.5},
        ).status_code
        == 200
    )
    assert client.post(f"/api/projects/{pid}/sizing/auto-size").status_code == 200
    assert client.post(f"/api/projects/{pid}/loads/generate").status_code == 200
    assert client.post(f"/api/projects/{pid}/design/run").status_code == 200
    assert (
        client.post(
            f"/api/projects/{pid}/reports/generate",
            json={
                "signatory": {
                    "engineer_name": "Engr. Jose Rizal, CE",
                    "prc_license_no": "0189600",
                    "ptr_no": "PTR-2026-881",
                    "project_owner": "Highland Developments Inc.",
                }
            },
        ).status_code
        == 200
    )


def test_dxf_audit_layers_entities_schedules_and_signatory(cad_client):
    client, _ = cad_client
    pid = "CAD-DXF-01"
    _run_prereqs(client, pid)

    gen = client.post(f"/api/projects/{pid}/cad/generate")
    assert gen.status_code == 200, gen.text
    state = gen.json()
    assert state["generated_at"] is not None
    assert [s["sheet_no"] for s in state["sheets"]] == ["S-1", "S-2", "S-3"]
    assert state["total_entities"] > 100

    # Download DXF and parse with ezdxf
    res = client.get(f"/api/projects/{pid}/cad/download/dxf")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/dxf")
    dxf_text = res.content.decode("utf-8")
    doc = ezdxf.read(io.StringIO(dxf_text))

    auditor = doc.audit()
    assert auditor.errors == [], f"ezdxf audit errors: {auditor.errors}"

    # Verify required layers exist and carry non-zero entities
    required_layers = {"S-GRID", "S-COL", "S-BEAM", "S-FTG", "S-SCHED", "S-TITLEBLK", "S-REBAR", "S-DIM"}
    for lname in required_layers:
        assert lname in doc.layers, f"Missing CAD layer: {lname}"

    msp = doc.modelspace()
    layer_counts: dict[str, int] = {}
    all_texts: list[str] = []
    for ent in msp:
        l = ent.dxf.layer
        layer_counts[l] = layer_counts.get(l, 0) + 1
        if ent.dxftype() == "TEXT":
            all_texts.append(ent.dxf.text)
        elif ent.dxftype() == "MTEXT":
            all_texts.append(ent.plain_text())

    for lname in required_layers:
        assert layer_counts.get(lname, 0) > 0, f"Layer {lname} has 0 entities"

    joined = "\n".join(all_texts)
    # Footing tags, beam/column marks, opening label, and Module 08 signatory details
    assert "F1" in joined
    assert "C1" in joined
    assert "B1" in joined
    assert "OPENING" in joined
    assert "Engr. Jose Rizal, CE" in joined
    assert "0189600" in joined
    assert "SCHEDULE OF ISOLATED FOOTINGS" in joined
    assert "TYPICAL RC COLUMN SECTION" in joined

    # Paper Space layouts exist for S-1, S-2, S-3
    for sno in ("S-1", "S-2", "S-3"):
        assert sno in doc.layouts


def test_cad_svg_preview_sheet_options_and_legacy_json_compat(cad_client):
    client, data_dir = cad_client
    pid = "CAD-SVG-01"
    _run_prereqs(client, pid)

    gen = client.post(
        f"/api/projects/{pid}/cad/generate",
        json={"options": {"sheet_size": "A2", "plan_scale": 75, "sheet_prefix": "ST"}},
    )
    assert gen.status_code == 200, gen.text
    assert [s["sheet_no"] for s in gen.json()["sheets"]] == ["ST-1", "ST-2", "ST-3"]

    for sno in ("ST-1", "ST-2", "ST-3"):
        svg_res = client.get(f"/api/projects/{pid}/cad/preview-svg", params={"sheet_no": sno})
        assert svg_res.status_code == 200, svg_res.text
        body = svg_res.json()
        assert body["sheet_no"] == sno
        assert body["sheet_size"] == "A2"
        assert "<svg" in body["svg"]
        assert 'data-layer="S-TITLEBLK"' in body["svg"]
        assert len(body["layers"]) >= 4

    # Legacy project JSON without cad_drawings loads cleanly under extra="forbid"
    disk_path = data_dir / f"{pid}.json"
    raw = json.loads(disk_path.read_text(encoding="utf-8"))
    raw.pop("cad_drawings", None)
    reloaded = ProjectState.model_validate(raw)
    assert reloaded.cad_drawings.generated_at is None
    assert reloaded.cad_drawings.sheets == []


def test_cad_generate_rejects_missing_phase6_with_400(cad_client):
    client, _ = cad_client
    pid = "CAD-NOP6-01"
    p = _make_cad_project(pid)
    assert client.post("/api/projects", json=p.model_dump()).status_code == 200
    res = client.post(f"/api/projects/{pid}/cad/generate")
    assert res.status_code == 400
    assert "Phase 6" in res.json()["detail"]
