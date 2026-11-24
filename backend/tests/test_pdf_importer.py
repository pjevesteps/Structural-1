from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2
import numpy as np
import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.geometry import plan_to_staad
from backend.app.main import app
from backend.app.models import ProjectState
from backend.app.pdf_importer.inspector import PdfTooLargeError, validate_pdf_bytes
from backend.app.staad import write_staad_preview


def _make_sample_project(project_id: str = "TEST-P2") -> dict:
    return {
        "project_info": {
            "project_id": project_id,
            "project_name": "Phase 2 Test Frame",
            "location": "Test City",
            "design_code": "To be confirmed by engineer",
            "materials": {
                "fc_mpa": 28.0,
                "fy_mpa": 415.0,
                "concrete_density_kn_m3": 24.0,
            },
        },
        "levels": [
            {"id": "BASE", "elevation_m": 0.0},
            {"id": "2F", "elevation_m": 3.5},
        ],
        "grids": {
            "x_grids": [{"label": "1", "coord_m": 0.0}],
            "y_grids": [{"label": "A", "coord_m": 0.0}],
        },
        "columns": [
            {
                "id": "C0",
                "grid": "A-1",
                "x_m": 0.0,
                "y_m": 0.0,
                "width_mm": 400.0,
                "depth_mm": 400.0,
            }
        ],
        "beams": [],
    }


def _build_synthetic_multipage_pdf() -> bytes:
    """
    Create a 2-page PDF with PyMuPDF:
      - Page 1 (vector): 3 X-grids (1, 2, 3 at x=120, 300, 480 pt -> 0, 6, 12 m at 30 pt/m)
                         2 Y-grids (A, B at y=330, 180 pt -> 0, 5 m in plan Y-up)
                         6 filled column squares (12x12 pt -> 400x400 mm) at intersections.
      - Page 2 (scanned): high-contrast raster image of 2x2 grids + 4 filled columns embedded
                          with no vector drawings, exercising scanned classification + OpenCV fallback.
    """
    doc = pymupdf.open()

    # --- Page 1: Vector structural plan ---
    page1 = doc.new_page(width=600, height=450)
    x_coords = [("1", 120.0), ("2", 300.0), ("3", 480.0)]
    y_coords = [("A", 330.0), ("B", 180.0)]

    shape = page1.new_shape()
    # Vertical grid lines (x_grids)
    for label, x in x_coords:
        shape.draw_line(pymupdf.Point(x, 70.0), pymupdf.Point(x, 390.0))
        shape.finish(color=(0.2, 0.2, 0.2), width=0.8)
        page1.insert_text(pymupdf.Point(x - 3.5, 58.0), label, fontsize=11)

    # Horizontal grid lines (y_grids)
    for label, y in y_coords:
        shape.draw_line(pymupdf.Point(65.0, y), pymupdf.Point(535.0, y))
        shape.finish(color=(0.2, 0.2, 0.2), width=0.8)
        page1.insert_text(pymupdf.Point(45.0, y + 4.0), label, fontsize=11)

    # Filled column rectangles at every grid intersection
    col_half = 6.0  # 12 pt = 0.4 m = 400 mm at 30 pt/m
    for _, y in y_coords:
        for _, x in x_coords:
            rect = pymupdf.Rect(x - col_half, y - col_half, x + col_half, y + col_half)
            shape.draw_rect(rect)
            shape.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.6)

    shape.commit()

    # --- Page 2: Scanned raster plan ---
    raster = np.full((600, 800, 3), 255, dtype=np.uint8)
    # Draw crisp grid lines (thin 2px lines)
    for gx in (200, 560):
        cv2.line(raster, (gx, 80), (gx, 520), (40, 40, 40), 2)
    for gy in (180, 440):
        cv2.line(raster, (100, gy), (700, gy), (40, 40, 40), 2)
    # Draw solid black column squares (24x24 px) at the 4 intersections
    for gy in (180, 440):
        for gx in (200, 560):
            cv2.rectangle(raster, (gx - 12, gy - 12), (gx + 12, gy + 12), (0, 0, 0), -1)

    ok, png_buf = cv2.imencode(".png", raster)
    assert ok
    page2 = doc.new_page(width=600, height=450)
    page2.insert_image(page2.rect, stream=png_buf.tobytes())

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    projects_dir = tmp_path / "data" / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "DATA_DIR", projects_dir)
    client = TestClient(app)
    return client, tmp_path / "data"


def test_staad_preview_writes_real_newlines(tmp_path: Path):
    project = ProjectState.model_validate(_make_sample_project("STAAD-NL"))
    out_file = tmp_path / "preview.std"
    result = write_staad_preview(project, out_file)

    assert result["nodes"] == 2
    assert result["members"] == 1
    content = out_file.read_text(encoding="utf-8")
    assert "\\n" not in content
    lines = content.splitlines()
    assert lines[0] == "STAAD SPACE"
    assert lines[1] == "UNIT METER KN"
    assert "JOINT COORDINATES" in lines
    assert "FINISH" in lines


def test_pdf_upload_validation(isolated_env):
    client, _ = isolated_env
    pid = "VAL-001"
    r = client.post("/api/projects", json=_make_sample_project(pid))
    assert r.status_code == 200

    # Empty file rejected
    r_empty = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert r_empty.status_code == 400

    # Non-%PDF- header rejected
    r_bad = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("fake.pdf", b"NOT_A_PDF_HEADER", "application/pdf")},
    )
    assert r_bad.status_code == 400
    assert "%PDF-" in r_bad.json()["detail"]

    # Oversized file rejected by size validator
    with pytest.raises(PdfTooLargeError):
        validate_pdf_bytes(b"%PDF-1.7" + b"0" * 1024, max_bytes=100)


def test_pdf_importer_end_to_end(isolated_env):
    client, data_root = isolated_env
    pid = "P2-E2E"

    # 1. Create Phase 1 project and confirm Phase 1 endpoints remain intact
    assert client.get("/api/health").status_code == 200
    assert client.post("/api/projects", json=_make_sample_project(pid)).status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 200
    assert client.post(f"/api/projects/{pid}/validate").status_code == 200

    # 2. Upload synthetic multi-page vector + scanned PDF
    pdf_bytes = _build_synthetic_multipage_pdf()
    up_res = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("structural_plan.pdf", pdf_bytes, "application/pdf")},
    )
    assert up_res.status_code == 200, up_res.text
    doc_state = up_res.json()
    doc_id = doc_state["doc_id"]

    assert doc_state["page_count"] == 2
    assert doc_state["pages"][0]["classification"] == "vector"
    assert doc_state["pages"][1]["classification"] == "scanned"

    # Verify immutable storage layout on disk
    doc_dir = data_root / "imports" / pid / doc_id
    assert (doc_dir / "original.pdf").read_bytes() == pdf_bytes
    assert (doc_dir / "pages" / "page_001.png").exists()
    assert (doc_dir / "pages" / "page_002.png").exists()
    assert (doc_dir / "state.json").exists()
    assert (doc_dir / "audit.jsonl").exists()

    # Verify PNG preview endpoint
    img_res = client.get(f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/image")
    assert img_res.status_code == 200
    assert img_res.headers["content-type"] == "image/png"
    assert img_res.content.startswith(b"\x89PNG")

    # 3. Extract vector grids & columns on Page 1
    ext_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract",
        json={"method": "auto"},
    )
    assert ext_res.status_code == 200, ext_res.text
    ext_body = ext_res.json()
    assert ext_body["method_used"] == "vector"
    assert ext_body["grids_detected"] == 5
    assert ext_body["columns_detected"] == 6

    doc_state = ext_body["document"]
    p1_grids = [g for g in doc_state["grid_candidates"] if g["page_number"] == 1]
    p1_cols = [c for c in doc_state["column_candidates"] if c["page_number"] == 1]
    x_labels = sorted(g["label"] for g in p1_grids if g["axis"] == "x")
    y_labels = sorted(g["label"] for g in p1_grids if g["axis"] == "y")
    assert x_labels == ["1", "2", "3"]
    assert y_labels == ["A", "B"]

    # 4. Calibrate Page 1 using 2 points (Grid A-1 -> Grid A-2 = 6000 mm = 6.0 m), unconfirmed first
    page1 = doc_state["pages"][0]
    sx = page1["width_px"] / page1["width_pt"]
    sy = page1["height_px"] / page1["height_pt"]

    cal_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/calibrate",
        json={
            "p1": {"x_px": 120.0 * sx, "y_px": 330.0 * sy},
            "p2": {"x_px": 300.0 * sx, "y_px": 330.0 * sy},
            "known_distance": 6000.0,
            "unit": "mm",
            "confirmed": False,
        },
    )
    assert cal_res.status_code == 200, cal_res.text
    doc_state = cal_res.json()
    cal = doc_state["calibrations"]["1"]
    assert cal["confirmed"] is False
    assert abs(cal["real_distance_m"] - 6.0) < 1e-4

    # Verify plan meter coordinates after calibration
    x_map = {g["label"]: g["coord_m"] for g in doc_state["grid_candidates"] if g["axis"] == "x"}
    y_map = {g["label"]: g["coord_m"] for g in doc_state["grid_candidates"] if g["axis"] == "y"}
    assert abs(x_map["1"] - 0.0) < 0.05
    assert abs(x_map["2"] - 6.0) < 0.05
    assert abs(x_map["3"] - 12.0) < 0.05
    assert abs(y_map["A"] - 0.0) < 0.05
    assert abs(y_map["B"] - 5.0) < 0.05

    # Verify geometry.py plan_to_staad compatibility
    first_col = doc_state["column_candidates"][0]
    staad_xyz = plan_to_staad(first_col["x_m"], first_col["y_m"], 3.5)
    assert staad_xyz == (first_col["x_m"], 3.5, first_col["y_m"])

    # 5. Engineer review: approve all grids, approve 4 columns, reject 1, leave 1 pending
    for g in doc_state["grid_candidates"]:
        r_rev = client.patch(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/candidates/{g['id']}",
            json={"status": "approved"},
        )
        assert r_rev.status_code == 200

    cols = doc_state["column_candidates"]
    for c in cols[:4]:
        assert (
            client.patch(
                f"/api/projects/{pid}/pdf-imports/{doc_id}/candidates/{c['id']}",
                json={"status": "approved"},
            ).status_code
            == 200
        )
    assert (
        client.patch(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/candidates/{cols[4]['id']}",
            json={"status": "rejected"},
        ).status_code
        == 200
    )
    # cols[5] remains pending

    # Add 1 manual column (auto-approved) at (x=210 pt -> 3.0 m, y=255 pt -> 2.5 m)
    man_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/candidates/columns",
        json={
            "page_number": 1,
            "x_px": 210.0 * sx,
            "y_px": 255.0 * sy,
            "column_id": "CM1",
            "grid": "MID",
            "width_mm": 450.0,
            "depth_mm": 450.0,
            "status": "approved",
        },
    )
    assert man_res.status_code == 200, man_res.text
    man_col = [c for c in man_res.json()["column_candidates"] if c["column_id"] == "CM1"][0]
    assert abs(man_col["x_m"] - 3.0) < 0.05
    assert abs(man_col["y_m"] - 2.5) < 0.05

    # 6. Transfer MUST be blocked while scale calibration is unconfirmed
    blocked_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/transfer",
        json={"page_number": 1, "merge_mode": "replace_geometry"},
    )
    assert blocked_res.status_code == 400
    assert "confirmed" in blocked_res.json()["detail"].lower()

    # 7. Confirm scale calibration, then transfer approved elements to ProjectState
    conf_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/confirm-calibration"
    )
    assert conf_res.status_code == 200
    assert conf_res.json()["calibrations"]["1"]["confirmed"] is True

    tx_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/transfer",
        json={"page_number": 1, "merge_mode": "replace_geometry"},
    )
    assert tx_res.status_code == 200, tx_res.text
    tx = tx_res.json()
    assert tx["transferred_x_grids"] == 3
    assert tx["transferred_y_grids"] == 2
    assert tx["transferred_columns"] == 5  # 4 approved vector + 1 manual (rejected & pending excluded)
    assert tx["total_project_columns"] == 5

    # Reload project from JSON storage and verify strict ProjectState schema compliance
    reloaded = ProjectState.model_validate(
        json.loads((data_root / "projects" / f"{pid}.json").read_text(encoding="utf-8"))
    )
    assert len(reloaded.grids.x_grids) == 3
    assert len(reloaded.grids.y_grids) == 2
    assert len(reloaded.columns) == 5
    col_ids = {c.id for c in reloaded.columns}
    assert "CM1" in col_ids
    assert cols[4]["column_id"] not in col_ids  # rejected excluded
    assert cols[5]["column_id"] not in col_ids  # pending excluded

    # Verify audit trail recorded every phase
    audit_res = client.get(f"/api/projects/{pid}/pdf-imports/{doc_id}/audit")
    assert audit_res.status_code == 200
    events = [entry["event"] for entry in audit_res.json()]
    for expected in (
        "pdf_uploaded",
        "extraction_run",
        "scale_calibrated",
        "candidate_reviewed",
        "candidate_added",
        "scale_confirmed",
        "transferred_to_project",
    ):
        assert expected in events

    # 8. Test OpenCV raster fallback on scanned Page 2
    p2_ext = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/2/extract",
        json={"method": "auto"},
    )
    assert p2_ext.status_code == 200, p2_ext.text
    p2_body = p2_ext.json()
    assert p2_body["method_used"] == "raster"
    assert p2_body["grids_detected"] >= 4
    assert p2_body["columns_detected"] >= 4

    # 9. Test experimental VLM placeholder returns clean non-fabricated response
    vlm_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract",
        json={"method": "vlm", "replace_unreviewed": False},
    )
    assert vlm_res.status_code == 200
    vlm_body = vlm_res.json()
    assert vlm_body["method_used"] == "vlm_placeholder"
    assert vlm_body["grids_detected"] == 0
    assert vlm_body["columns_detected"] == 0
    assert "placeholder" in vlm_body["notice"].lower()
