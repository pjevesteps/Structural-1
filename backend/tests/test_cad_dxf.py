from __future__ import annotations

import io
import json
from pathlib import Path
import zipfile

import ezdxf
from fastapi.testclient import TestClient
import pymupdf
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


def test_custom_per_sheet_composer_pdf_zip_and_overflow_guard(cad_client):
    client, _ = cad_client
    pid = "CAD-COMPOSER-01"
    _run_prereqs(client, pid)

    # 1. Custom per-sheet assignment: S-1 = Notes + Foundation + 1F, S-2 = 2F + ROOF, S-3 = Schedules + SMRF Longitudinal Details
    custom_payload = {
        "options": {
            "sheet_size": "A1",
            "plan_scale": 100,
            "detail_scale": 25,
            "preset": "custom",
            "sheets": [
                {
                    "sheet_no": "S-1",
                    "title": "GENERAL NOTES, FOUNDATION & GROUND FLOOR PLAN",
                    "plan_scale": 75,
                    "items": ["general_notes", "foundation_plan", "floor_plan:1F"],
                },
                {
                    "sheet_no": "S-2",
                    "title": "UPPER FLOOR & ROOF FRAMING PLANS",
                    "plan_scale": 100,
                    "items": ["floor_plan:2F", "floor_plan:ROOF"],
                },
                {
                    "sheet_no": "S-3",
                    "title": "SCHEDULES & SMRF LONGITUDINAL DETAILS",
                    "items": [
                        "schedule_footings",
                        "schedule_columns",
                        "schedule_beams",
                        "detail_beam_longitudinal",
                        "detail_column_longitudinal",
                    ],
                },
            ],
        }
    }
    gen = client.post(f"/api/projects/{pid}/cad/generate", json=custom_payload)
    assert gen.status_code == 200, gen.text
    st = gen.json()
    assert [s["sheet_no"] for s in st["sheets"]] == ["S-1", "S-2", "S-3"]
    assert "Foundation & Tie-Beam Plan" in st["sheets"][0]["contents"]
    assert "1F Framing Plan" in st["sheets"][0]["contents"]
    assert "2F Framing Plan" in st["sheets"][1]["contents"]
    assert len(st["available_items"]) >= 10
    assert all(s["entity_count"] > 20 for s in st["sheets"])

    # Verify SMRF longitudinal detail texts & true DXF layer lineweights
    dxf_res = client.get(f"/api/projects/{pid}/cad/download/dxf")
    assert dxf_res.status_code == 200
    doc = ezdxf.read(io.StringIO(dxf_res.content.decode("utf-8")))
    assert doc.audit().errors == []
    assert doc.layers.get("S-COL").dxf.lineweight == 50
    assert doc.layers.get("S-BEAM").dxf.lineweight == 35
    assert doc.layers.get("S-GRID").dxf.lineweight == 13
    all_texts = "\n".join(
        e.dxf.text for e in doc.modelspace() if e.dxftype() == "TEXT"
    )
    assert "SMRF BEAM LONGITUDINAL ELEVATION" in all_texts
    assert "SMRF COLUMN & PEDESTAL ELEVATION" in all_texts
    assert "CLASS B LAP (40db)" in all_texts

    # 2. True-scale multi-page vector PDF (A1 landscape = 841x594 mm -> ~2383.9 x 1683.8 pt)
    pdf_res = client.get(f"/api/projects/{pid}/cad/download/pdf")
    assert pdf_res.status_code == 200
    assert pdf_res.headers["content-type"].startswith("application/pdf")
    assert pdf_res.content.startswith(b"%PDF-")
    pdf_doc = pymupdf.open(stream=pdf_res.content, filetype="pdf")
    assert pdf_doc.page_count == 3
    p0_rect = pdf_doc[0].rect
    assert p0_rect.width > p0_rect.height  # landscape
    assert p0_rect.width == pytest.approx(841.0 * 72.0 / 25.4, rel=0.01)
    assert p0_rect.height == pytest.approx(594.0 * 72.0 / 25.4, rel=0.01)
    pdf_doc.close()

    # 3. Individual-sheet DXF ZIP export
    zip_res = client.get(f"/api/projects/{pid}/cad/download/zip")
    assert zip_res.status_code == 200
    assert zip_res.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(zip_res.content), "r") as zf:
        names = sorted(zf.namelist())
        assert names == [f"{pid}_S-1.dxf", f"{pid}_S-2.dxf", f"{pid}_S-3.dxf"]
        for nm in names:
            sub_doc = ezdxf.read(io.StringIO(zf.read(nm).decode("utf-8")))
            assert sub_doc.audit().errors == []

    # 4. Overflow guard: pack 4 plans onto A3 at aggressive 1:40 scale -> overflow=True & A3 PDF dims
    ov_payload = {
        "options": {
            "sheet_size": "A3",
            "plan_scale": 40,
            "preset": "custom",
            "sheets": [
                {
                    "sheet_no": "S-1",
                    "title": "CROWDED A3 TEST",
                    "plan_scale": 40,
                    "items": [
                        "general_notes",
                        "foundation_plan",
                        "floor_plan:1F",
                        "floor_plan:2F",
                        "floor_plan:ROOF",
                    ],
                }
            ],
        }
    }
    ov_res = client.post(f"/api/projects/{pid}/cad/generate", json=ov_payload)
    assert ov_res.status_code == 200, ov_res.text
    ov_state = ov_res.json()
    assert ov_state["sheets"][0]["overflow"] is True
    assert any("overflow" in w.lower() for w in ov_state["warnings"])

    a3_pdf = client.get(f"/api/projects/{pid}/cad/download/pdf")
    assert a3_pdf.status_code == 200
    a3_doc = pymupdf.open(stream=a3_pdf.content, filetype="pdf")
    assert a3_doc.page_count == 1
    assert a3_doc[0].rect.width == pytest.approx(420.0 * 72.0 / 25.4, rel=0.01)
    assert a3_doc[0].rect.height == pytest.approx(297.0 * 72.0 / 25.4, rel=0.01)
    a3_doc.close()


def test_dwg_status_and_binary_dwg_export_mocked_and_503_fallback(cad_client, monkeypatch: pytest.MonkeyPatch):
    from backend.app.cad import dwg_converter as dwg_mod

    client, _ = cad_client
    pid = "CAD-DWG-01"
    _run_prereqs(client, pid)
    assert client.post(f"/api/projects/{pid}/cad/generate").status_code == 200

    # 1. Live /cad/dwg-status returns expected metadata contract
    st_res = client.get(f"/api/projects/{pid}/cad/dwg-status")
    assert st_res.status_code == 200
    st = st_res.json()
    assert isinstance(st["available"], bool)
    assert "backend" in st and "executable_path" in st and "ODA File Converter" in st["install_hint"]

    # 2. When no converter binary is available -> HTTP 503 with clear ODA setup guidance
    monkeypatch.setattr(
        dwg_mod,
        "detect_dwg_converter",
        lambda: {
            "available": False,
            "backend": None,
            "executable_path": None,
            "install_hint": dwg_mod.INSTALL_HINT,
        },
    )
    unavail = client.get(f"/api/projects/{pid}/cad/download/dwg")
    assert unavail.status_code == 503
    assert "ODA File Converter" in unavail.json()["detail"]

    # 3. When ODA File Converter is available (mocked odafc.convert) -> streams binary .dwg bytes
    fake_dwg_payload = b"AC1027\x00\x00\x00\x00\x00BINARY_DWG_TEST_STREAM"

    def _fake_oda_convert(src: str, dst: str, version: str = "R2013", replace: bool = True):
        assert Path(src).is_file()
        assert version == "R2013"
        Path(dst).write_bytes(fake_dwg_payload)

    monkeypatch.setattr(
        dwg_mod,
        "detect_dwg_converter",
        lambda: {
            "available": True,
            "backend": "oda_file_converter",
            "executable_path": "ODAFileConverter.exe",
            "install_hint": dwg_mod.INSTALL_HINT,
        },
    )
    monkeypatch.setattr(dwg_mod.odafc, "convert", _fake_oda_convert)

    ok_res = client.get(f"/api/projects/{pid}/cad/download/dwg")
    assert ok_res.status_code == 200
    assert ok_res.headers["content-type"].startswith("application/acad")
    assert ok_res.content == fake_dwg_payload
    assert ok_res.content.startswith(b"AC1027")
