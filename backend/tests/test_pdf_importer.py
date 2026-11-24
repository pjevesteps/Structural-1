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

    # Concrete gray-shaded column rectangles at every grid intersection
    col_half = 6.0  # 12 pt = 0.4 m = 400 mm at 30 pt/m
    for _, y in y_coords:
        for _, x in x_coords:
            rect = pymupdf.Rect(x - col_half, y - col_half, x + col_half, y + col_half)
            shape.draw_rect(rect)
            shape.finish(color=(0.1, 0.1, 0.1), fill=(0.52, 0.52, 0.52), width=0.6)

    shape.commit()

    # --- Page 2: Scanned raster plan ---
    raster = np.full((600, 800, 3), 255, dtype=np.uint8)
    # Draw crisp grid lines (thin 2px lines)
    for gx in (200, 560):
        cv2.line(raster, (gx, 80), (gx, 520), (40, 40, 40), 2)
    for gy in (180, 440):
        cv2.line(raster, (100, gy), (700, gy), (40, 40, 40), 2)
    # Draw concrete gray-shaded column squares (24x24 px) at the 4 intersections
    for gy in (180, 440):
        for gx in (200, 560):
            cv2.rectangle(raster, (gx - 12, gy - 12), (gx + 12, gy + 12), (128, 128, 128), -1)
            cv2.rectangle(raster, (gx - 12, gy - 12), (gx + 12, gy + 12), (20, 20, 20), 1)

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


def _build_dashed_and_bordered_pdf() -> bytes:
    """
    Create a 2-page synthetic PDF to test border exclusion, dashed grid merging, and label matching:
      - Page 1 (vector):
          * Outer sheet border drawn as 4 lines inside the outer 4% margin (must be ignored)
          * Dashed vertical grids ("1" at x=160 pt, "2" at x=420 pt) drawn as short colinear segments
          * Dashed horizontal grids ("A" at y=320 pt, "B" at y=150 pt) drawn as short colinear segments
          * Text labels ("1", "2", "A", "B") placed near grid endpoints
      - Page 2 (scanned raster):
          * Outer border rectangle inside outer 4% margin (must be ignored)
          * Dashed vertical and horizontal grid lines (must be merged via HoughLinesP + clustering)
    """
    doc = pymupdf.open()

    # --- Page 1: Vector with outer border + dashed grids + endpoint labels ---
    p1 = doc.new_page(width=600, height=450)
    shape = p1.new_shape()

    # Outer sheet border lines within outer 4% margin (x=12, 588; y=10, 440) -> MUST be ignored
    shape.draw_line(pymupdf.Point(12.0, 10.0), pymupdf.Point(588.0, 10.0))
    shape.finish(color=(0, 0, 0), width=1.2)
    shape.draw_line(pymupdf.Point(12.0, 440.0), pymupdf.Point(588.0, 440.0))
    shape.finish(color=(0, 0, 0), width=1.2)
    shape.draw_line(pymupdf.Point(12.0, 10.0), pymupdf.Point(12.0, 440.0))
    shape.finish(color=(0, 0, 0), width=1.2)
    shape.draw_line(pymupdf.Point(588.0, 10.0), pymupdf.Point(588.0, 440.0))
    shape.finish(color=(0, 0, 0), width=1.2)

    # Dashed vertical grid lines (short 14pt dashes with 10pt gaps) spanning y=80..380
    for label, x in [("1", 160.0), ("2", 420.0)]:
        y = 80.0
        while y < 380.0:
            y_end = min(y + 14.0, 380.0)
            shape.draw_line(pymupdf.Point(x, y), pymupdf.Point(x, y_end))
            shape.finish(color=(0.2, 0.2, 0.2), width=0.8)
            y += 24.0
        p1.insert_text(pymupdf.Point(x - 3.5, 68.0), label, fontsize=11)

    # Dashed horizontal grid lines (short 14pt dashes with 10pt gaps) spanning x=90..510
    for label, y in [("A", 320.0), ("B", 150.0)]:
        x = 90.0
        while x < 510.0:
            x_end = min(x + 14.0, 510.0)
            shape.draw_line(pymupdf.Point(x, y), pymupdf.Point(x_end, y))
            shape.finish(color=(0.2, 0.2, 0.2), width=0.8)
            x += 24.0
        p1.insert_text(pymupdf.Point(72.0, y + 4.0), label, fontsize=11)

    shape.commit()

    # --- Page 2: Scanned raster with outer border + dashed grid lines ---
    raster = np.full((600, 800, 3), 255, dtype=np.uint8)
    # Outer sheet border within outer 4% margin (x=15..785, y=12..588) -> MUST be ignored
    cv2.rectangle(raster, (15, 12), (785, 588), (0, 0, 0), 2)
    # Dashed vertical grids at x=220 and x=580
    for gx in (220, 580):
        for y in range(90, 510, 36):
            cv2.line(raster, (gx, y), (gx, min(y + 22, 510)), (20, 20, 20), 2)
    # Dashed horizontal grids at y=180 and y=430
    for gy in (180, 430):
        for x in range(110, 690, 36):
            cv2.line(raster, (x, gy), (min(x + 22, 690), gy), (20, 20, 20), 2)

    ok, png_buf = cv2.imencode(".png", raster)
    assert ok
    p2 = doc.new_page(width=600, height=450)
    p2.insert_image(p2.rect, stream=png_buf.tobytes())

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_dashed_grids_labels_and_border_exclusion(isolated_env):
    client, _ = isolated_env
    pid = "GRID-TUNE"
    assert client.post("/api/projects", json=_make_sample_project(pid)).status_code == 200

    pdf_bytes = _build_dashed_and_bordered_pdf()
    up = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("dashed_grids.pdf", pdf_bytes, "application/pdf")},
    )
    assert up.status_code == 200, up.text
    doc_id = up.json()["doc_id"]

    # 1. Default ExtractionOptions on vector Page 1: border ignored, dashed grids merged & labeled
    ext1 = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract",
        json={
            "method": "vector",
            "options": {
                "border_margin_ratio": 0.04,
                "min_span_ratio": 0.28,
                "cluster_tolerance_px": 8.0,
                "merge_dashed_segments": True,
                "detect_grid_labels": True,
            },
        },
    )
    assert ext1.status_code == 200, ext1.text
    body1 = ext1.json()
    assert body1["grids_detected"] == 4

    p1_grids = [g for g in body1["document"]["grid_candidates"] if g["page_number"] == 1]
    x_grids = [g for g in p1_grids if g["axis"] == "x"]
    y_grids = [g for g in p1_grids if g["axis"] == "y"]
    assert sorted(g["label"] for g in x_grids) == ["1", "2"]
    assert sorted(g["label"] for g in y_grids) == ["A", "B"]

    # Confirm no outer border lines leaked in (all coords strictly inside 4% margin)
    p1_info = body1["document"]["pages"][0]
    for g in x_grids:
        assert p1_info["width_px"] * 0.04 < g["coord_px"] < p1_info["width_px"] * 0.96
    for g in y_grids:
        assert p1_info["height_px"] * 0.04 < g["coord_px"] < p1_info["height_px"] * 0.96

    # 2. Disabling merge_dashed_segments excludes dashed lines
    ext_no_dash = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract",
        json={
            "method": "vector",
            "options": {"merge_dashed_segments": False},
        },
    )
    assert ext_no_dash.status_code == 200
    assert ext_no_dash.json()["grids_detected"] == 0

    # 3. Scanned Page 2 via raster detection: outer border ignored, dashed grids merged
    ext2 = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/2/extract",
        json={
            "method": "raster",
            "options": {
                "border_margin_ratio": 0.04,
                "min_span_ratio": 0.28,
                "cluster_tolerance_px": 8.0,
                "merge_dashed_segments": True,
            },
        },
    )
    assert ext2.status_code == 200, ext2.text
    body2 = ext2.json()
    p2_grids = [g for g in body2["document"]["grid_candidates"] if g["page_number"] == 2]
    p2_x = [g for g in p2_grids if g["axis"] == "x"]
    p2_y = [g for g in p2_grids if g["axis"] == "y"]
    assert len(p2_x) == 2
    assert len(p2_y) == 2

    p2_info = body2["document"]["pages"][1]
    for g in p2_x:
        assert p2_info["width_px"] * 0.04 < g["coord_px"] < p2_info["width_px"] * 0.96
    for g in p2_y:
        assert p2_info["height_px"] * 0.04 < g["coord_px"] < p2_info["height_px"] * 0.96


def _build_architectural_multipage_set_pdf() -> bytes:
    """
    Build a 4-page synthetic architectural PDF set:
      - Page 1 (A1): Cover / Table of Contents listing Floor Plan, Elevation, Schedule -> cover_toc
      - Page 2 (A4): Dual Floor Plan sheet with vertical center divider + BASEMENT FLOOR PLAN (left)
                     and FIRST FLOOR PLAN (right), each with identical shifted 2x2 grids + 4 columns
      - Page 3 (A10): Front Elevation sheet with datum lines, left-side level names (including a
                      duplicate "LEVEL 2" tag), and right-side elevation values
      - Page 4 (A14): Schedule of Doors and Windows -> schedule_detail
    """
    doc = pymupdf.open()

    # --- Page 1 (A1): Cover / Table of Contents ---
    p1 = doc.new_page(width=800, height=560)
    p1.insert_text(pymupdf.Point(80, 90), "TABLE OF CONTENTS", fontsize=18)
    p1.insert_text(pymupdf.Point(80, 130), "A1 - COVER SHEET AND VICINITY MAP", fontsize=11)
    p1.insert_text(pymupdf.Point(80, 155), "A4 - BASEMENT FLOOR PLAN / FIRST FLOOR PLAN", fontsize=11)
    p1.insert_text(pymupdf.Point(80, 180), "A10 - FRONT ELEVATION", fontsize=11)
    p1.insert_text(pymupdf.Point(80, 205), "A14 - SCHEDULE OF DOORS AND WINDOWS", fontsize=11)
    p1.insert_text(pymupdf.Point(710, 520), "A1", fontsize=16)

    # --- Page 2 (A4): Dual Floor Plan sheet ---
    p2 = doc.new_page(width=800, height=560)
    s2 = p2.new_shape()
    # Vertical center viewport divider at x=400 (50% width), above bottom 12% title block (y=492)
    s2.draw_line(pymupdf.Point(400.0, 25.0), pymupdf.Point(400.0, 470.0))
    s2.finish(color=(0.3, 0.3, 0.3), width=1.2)

    # Left viewport: BASEMENT FLOOR PLAN (grids at x=90, 270; y=350, 190)
    p2.insert_text(pymupdf.Point(100, 445), "BASEMENT FLOOR PLAN", fontsize=12)
    for label, x in [("1", 90.0), ("2", 270.0)]:
        s2.draw_line(pymupdf.Point(x, 90.0), pymupdf.Point(x, 400.0))
        s2.finish(color=(0.2, 0.2, 0.2), width=0.8)
        p2.insert_text(pymupdf.Point(x - 3, 78.0), label, fontsize=10)
    for label, y in [("A", 350.0), ("B", 190.0)]:
        s2.draw_line(pymupdf.Point(55.0, y), pymupdf.Point(345.0, y))
        s2.finish(color=(0.2, 0.2, 0.2), width=0.8)
        p2.insert_text(pymupdf.Point(36.0, y + 4), label, fontsize=10)
    for y in (350.0, 190.0):
        for x in (90.0, 270.0):
            s2.draw_rect(pymupdf.Rect(x - 6, y - 6, x + 6, y + 6))
            s2.finish(color=(0.1, 0.1, 0.1), fill=(0.52, 0.52, 0.52), width=0.6)

    # Right viewport: FIRST FLOOR PLAN (shifted by +400 pt in X, 0 in Y -> x=490, 670; y=350, 190)
    p2.insert_text(pymupdf.Point(500, 445), "FIRST FLOOR PLAN", fontsize=12)
    for y in (350.0, 190.0):
        for x in (490.0, 670.0):
            s2.draw_rect(pymupdf.Rect(x - 6, y - 6, x + 6, y + 6))
            s2.finish(color=(0.1, 0.1, 0.1), fill=(0.52, 0.52, 0.52), width=0.6)

    s2.commit()
    p2.insert_text(pymupdf.Point(710, 520), "A4", fontsize=16)

    # --- Page 3 (A10): Front Elevation with 4 datum lines (including duplicate LEVEL 2) ---
    p3 = doc.new_page(width=800, height=560)
    p3.insert_text(pymupdf.Point(290, 460), "FRONT ELEVATION", fontsize=14)
    s3 = p3.new_shape()
    datum_specs = [
        (390.0, "BASEMENT FLOOR FINISHED LINE", "-2.60 m"),
        (310.0, "FIRST FLOOR FINISHED FLOOR LINE", "0.00 m"),
        (220.0, "LEVEL 2 SECOND FLOOR", "2.80 m"),
        (130.0, "LEVEL 2 ROOF DECK", "5.80 m"),
    ]
    for y, left_label, right_elev in datum_specs:
        s3.draw_line(pymupdf.Point(160.0, y), pymupdf.Point(650.0, y))
        s3.finish(color=(0.15, 0.15, 0.15), width=0.9)
        p3.insert_text(pymupdf.Point(35.0, y - 4.0), left_label, fontsize=9)
        p3.insert_text(pymupdf.Point(665.0, y - 4.0), right_elev, fontsize=10)
    s3.commit()
    p3.insert_text(pymupdf.Point(705, 520), "A10", fontsize=16)

    # --- Page 4 (A14): Schedule of Doors and Windows ---
    p4 = doc.new_page(width=800, height=560)
    p4.insert_text(pymupdf.Point(120, 90), "SCHEDULE OF DOORS AND WINDOWS", fontsize=16)
    p4.insert_text(pymupdf.Point(120, 140), "D1 - MAIN ENTRY DOOR 1.00 x 2.10m", fontsize=10)
    p4.insert_text(pymupdf.Point(705, 520), "A14", fontsize=16)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_architectural_sheet_classification_and_master_grid(isolated_env):
    client, _ = isolated_env
    pid = "ARCH-SET"
    assert client.post("/api/projects", json=_make_sample_project(pid)).status_code == 200

    pdf_bytes = _build_architectural_multipage_set_pdf()
    up = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("arch_set.pdf", pdf_bytes, "application/pdf")},
    )
    assert up.status_code == 200, up.text
    doc_state = up.json()
    doc_id = doc_state["doc_id"]
    pages = doc_state["pages"]

    assert len(pages) == 4
    # 1. Verify sheet classification strictly from each page's own embedded text
    assert pages[0]["sheet_type"] == "cover_toc"
    assert pages[0]["sheet_number"] == "A1"

    assert pages[1]["sheet_type"] == "floor_plan"
    assert pages[1]["sheet_number"] == "A4"
    assert pages[1]["viewport_count"] == 2
    assert "BASEMENT FLOOR PLAN" in pages[1]["sheet_title"]
    assert "FIRST FLOOR PLAN" in pages[1]["sheet_title"]

    assert pages[2]["sheet_type"] == "elevation"
    assert pages[2]["sheet_number"] == "A10"

    assert pages[3]["sheet_type"] == "schedule_detail"
    assert pages[3]["sheet_number"] == "A14"

    # Verify auto-created dual PlanRegion boxes on Page 2 (excluding bottom 12% title block)
    p2_regions = [r for r in doc_state["regions"] if r["page_number"] == 2]
    assert len(p2_regions) == 2
    left_reg, right_reg = p2_regions[0], p2_regions[1]
    assert "BASEMENT FLOOR PLAN" in left_reg["name"]
    assert "FIRST FLOOR PLAN" in right_reg["name"]
    assert left_reg["height_px"] <= pages[1]["height_px"] * 0.89

    # 2. Extract grids in Left region, approve them, set origins, and propagate master grid to Right region
    p2 = pages[1]
    sx = p2["width_px"] / p2["width_pt"]
    sy = p2["height_px"] / p2["height_pt"]

    ext_left = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/2/extract",
        json={"method": "vector", "region_id": left_reg["region_id"]},
    )
    assert ext_left.status_code == 200, ext_left.text
    assert ext_left.json()["grids_detected"] == 4
    left_grids = [
        g for g in ext_left.json()["document"]["grid_candidates"] if g["region_id"] == left_reg["region_id"]
    ]
    for g in left_grids:
        assert (
            client.patch(
                f"/api/projects/{pid}/pdf-imports/{doc_id}/candidates/{g['id']}",
                json={"status": "approved"},
            ).status_code
            == 200
        )

    # Set origins at Grid A-1 in Left (90, 350 pt) and Right (490, 350 pt)
    assert (
        client.patch(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{left_reg['region_id']}",
            json={"origin_px": {"x_px": 90.0 * sx, "y_px": 350.0 * sy}},
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{right_reg['region_id']}",
            json={"origin_px": {"x_px": 490.0 * sx, "y_px": 350.0 * sy}},
        ).status_code
        == 200
    )

    prop = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{right_reg['region_id']}/apply-master-grid",
        json={"source_region_id": left_reg["region_id"]},
    )
    assert prop.status_code == 200, prop.text
    prop_body = prop.json()
    assert prop_body["copied_grids"] == 4
    assert prop_body["probed_columns"] == 4

    right_cols = [
        c for c in prop_body["document"]["column_candidates"] if c["region_id"] == right_reg["region_id"]
    ]
    assert len(right_cols) == 4
    assert sorted(c["grid"] for c in right_cols) == ["A-1", "A-2", "B-1", "B-2"]


def test_elevation_level_extraction_and_transfer(isolated_env):
    client, data_root = isolated_env
    pid = "ELEV-LVL"
    assert client.post("/api/projects", json=_make_sample_project(pid)).status_code == 200

    pdf_bytes = _build_architectural_multipage_set_pdf()
    up = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("arch_set.pdf", pdf_bytes, "application/pdf")},
    )
    assert up.status_code == 200, up.text
    doc_id = up.json()["doc_id"]

    # 1. Extract levels from Elevation Page 3 (A10)
    ext = client.post(f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/3/extract-levels")
    assert ext.status_code == 200, ext.text
    body = ext.json()
    assert body["levels_detected"] == 4

    # Verify duplicate "LEVEL 2" warning was flagged
    assert any("LEVEL 2" in w and "Duplicate" in w for w in body["warnings"])

    levels = sorted(body["document"]["detected_levels"], key=lambda l: l["elevation_m"])
    assert [l["elevation_m"] for l in levels] == [-2.6, 0.0, 2.8, 5.8]
    assert levels[0]["story_height_m"] is None
    assert abs(levels[1]["story_height_m"] - 2.6) < 1e-4
    assert abs(levels[2]["story_height_m"] - 2.8) < 1e-4
    assert abs(levels[3]["story_height_m"] - 3.0) < 1e-4

    # 2. Engineer renames the duplicate top level and approves all 4 levels
    assert (
        client.patch(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/levels/{levels[3]['level_id']}",
            json={"name": "ROOF DECK", "engineer_approved": True},
        ).status_code
        == 200
    )
    for lvl in levels[:3]:
        assert (
            client.patch(
                f"/api/projects/{pid}/pdf-imports/{doc_id}/levels/{lvl['level_id']}",
                json={"review_status": "approved", "engineer_approved": True},
            ).status_code
            == 200
        )

    # 3. Transfer approved levels into ProjectState.levels
    tx = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/transfer-levels",
        json={"page_number": 3, "replace_existing": True},
    )
    assert tx.status_code == 200, tx.text
    tx_body = tx.json()
    assert tx_body["transferred_levels"] == 4
    assert tx_body["total_project_levels"] == 4

    reloaded = ProjectState.model_validate(
        json.loads((data_root / "projects" / f"{pid}.json").read_text(encoding="utf-8"))
    )
    assert [lv.elevation_m for lv in reloaded.levels] == [-2.6, 0.0, 2.8, 5.8]
    assert len({lv.id for lv in reloaded.levels}) == 4


def test_spaced_text_page_override_and_region_crud(isolated_env):
    from backend.app.pdf_importer.inspector import normalize_spaced_text

    assert "FLOOR PLAN" in normalize_spaced_text("F L O O R  P L A N")
    assert "BASEMENT FLOOR PLAN" in normalize_spaced_text("B A S E M E N T F L O O R P L A N")
    assert "LEFT ELEVATION" in normalize_spaced_text("L\nE\nF\nT\n \nE\nL\nE\nV\nA\nT\nI\nO\nN")
    assert "CROSS SECTION" in normalize_spaced_text("C\nR\nO\nS\nS\n \nS\nE\nC\nT\nI\nO\nN")

    client, _ = isolated_env
    pid = "OVERRIDE-REG"
    assert client.post("/api/projects", json=_make_sample_project(pid)).status_code == 200

    pdf_bytes = _build_architectural_multipage_set_pdf()
    up = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("arch_set.pdf", pdf_bytes, "application/pdf")},
    )
    assert up.status_code == 200
    doc_id = up.json()["doc_id"]

    # Override Page 4 sheet_type to elevation via PATCH /pages/{page_number}
    ov = client.patch(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/4",
        json={"sheet_type": "elevation"},
    )
    assert ov.status_code == 200
    assert ov.json()["pages"][3]["sheet_type"] == "elevation"

    # Manually create and delete a PlanRegion on Page 2
    cr = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/regions",
        json={
            "page_number": 2,
            "name": "Custom Plan Box",
            "floor_level": "2F",
            "x_px": 50.0,
            "y_px": 50.0,
            "width_px": 300.0,
            "height_px": 300.0,
        },
    )
    assert cr.status_code == 200
    created_reg = [r for r in cr.json()["regions"] if r["name"] == "Custom Plan Box"][0]
    assert created_reg["floor_level"] == "2F"

    dl = client.delete(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{created_reg['region_id']}"
    )
    assert dl.status_code == 200
    assert not any(r["region_id"] == created_reg["region_id"] for r in dl.json()["regions"])


def test_labeled_grids_only_drops_unlabeled_walls(isolated_env):
    """require_grid_label=True drops unlabeled walls/dimension lines while preserving labeled grids (A, 1')."""
    client, _ = isolated_env
    pid = "LABELED-ONLY"
    assert client.post("/api/projects", json=_make_sample_project(pid)).status_code == 200

    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=450)
    sh = pg.new_shape()

    # Labeled vertical grid "A" at x=160 pt
    sh.draw_line(pymupdf.Point(160.0, 80.0), pymupdf.Point(160.0, 380.0))
    sh.finish(color=(0.2, 0.2, 0.2), width=0.8)
    pg.insert_text(pymupdf.Point(157.0, 68.0), "A", fontsize=11)

    # Unlabeled interior wall vertical line at x=280 pt (NO label -> must be dropped!)
    sh.draw_line(pymupdf.Point(280.0, 80.0), pymupdf.Point(280.0, 380.0))
    sh.finish(color=(0.2, 0.2, 0.2), width=0.8)

    # Labeled horizontal grid with prime "1'" at y=300 pt
    sh.draw_line(pymupdf.Point(90.0, 300.0), pymupdf.Point(510.0, 300.0))
    sh.finish(color=(0.2, 0.2, 0.2), width=0.8)
    pg.insert_text(pymupdf.Point(68.0, 304.0), "1'", fontsize=11)

    # Unlabeled horizontal dimension chain line at y=160 pt (NO label -> must be dropped!)
    sh.draw_line(pymupdf.Point(90.0, 160.0), pymupdf.Point(510.0, 160.0))
    sh.finish(color=(0.2, 0.2, 0.2), width=0.8)

    sh.commit()
    pdf_bytes = doc.tobytes()
    doc.close()

    up = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("labeled_only.pdf", pdf_bytes, "application/pdf")},
    )
    assert up.status_code == 200
    doc_id = up.json()["doc_id"]

    # Default require_grid_label=True -> keeps ONLY "A" and "1'"
    ext_strict = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract",
        json={"method": "vector", "options": {"require_grid_label": True}},
    )
    assert ext_strict.status_code == 200
    assert ext_strict.json()["grids_detected"] == 2
    labels = sorted(g["label"] for g in ext_strict.json()["document"]["grid_candidates"])
    assert labels == ["1'", "A"]

    # When require_grid_label=False -> keeps all 4 lines
    ext_all = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract",
        json={"method": "vector", "options": {"require_grid_label": False}},
    )
    assert ext_all.status_code == 200
    assert ext_all.json()["grids_detected"] == 4


def test_floor_to_floor_elevation_filtering_only(isolated_env):
    """
    Elevation extraction drops NATURAL GRADE LINE, fourth floor ceiling line, FIREWALL A/B,
    and micro-step slivers < 1.50 m, keeping ONLY structural floor-to-floor levels and computing
    accurate story_height_m across skipped non-floor lines.
    """
    client, _ = isolated_env
    pid = "FLOOR-ONLY-ELEV"
    assert client.post("/api/projects", json=_make_sample_project(pid)).status_code == 200

    doc = pymupdf.open()
    pg = doc.new_page(width=800, height=560)
    pg.insert_text(pymupdf.Point(300, 505), "LEFT ELEVATION", fontsize=13)
    sh = pg.new_shape()

    # Mix of structural floor levels + non-floor architectural datums
    rows = [
        (430.0, "BASEMENT FLOOR FINISHED LINE", "0.00 m"),
        (375.0, "NATURAL GRADE LINE", "2.60 m"),             # MUST BE DROPPED
        (360.0, "FIRST FLOOR FINISHED FLOOR LINE", "2.80 m"),
        (290.0, "SECOND FLOOR FINISHED FLOOR LINE", "5.80 m"),
        (225.0, "fourth floor ceiling line", "11.00 m"),      # MUST BE DROPPED
        (190.0, "ROOF DECK FINISHED FLOOR LINE", "8.60 m"),
        (140.0, "FIREWALL A", "11.40 m"),                     # MUST BE DROPPED
        (115.0, "FIREWALL B", "12.00 m"),                     # MUST BE DROPPED
    ]
    for y, lbl, elev in rows:
        sh.draw_line(pymupdf.Point(150.0, y), pymupdf.Point(650.0, y))
        sh.finish(color=(0.2, 0.2, 0.2), width=0.9)
        pg.insert_text(pymupdf.Point(30.0, y - 4.0), lbl, fontsize=9)
        pg.insert_text(pymupdf.Point(665.0, y - 4.0), elev, fontsize=9)
    sh.commit()
    pdf_bytes = doc.tobytes()
    doc.close()

    up = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("elev_filter.pdf", pdf_bytes, "application/pdf")},
    )
    assert up.status_code == 200
    doc_id = up.json()["doc_id"]

    ext = client.post(f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract-levels")
    assert ext.status_code == 200, ext.text
    lvls = sorted(ext.json()["document"]["detected_levels"], key=lambda l: l["elevation_m"])

    # Only the 4 structural floor levels remain
    assert len(lvls) == 4
    names_upper = [l["name"].upper() for l in lvls]
    assert not any("NATURAL GRADE" in n or "CEILING" in n or "FIREWALL" in n for n in names_upper)
    assert [l["elevation_m"] for l in lvls] == [0.0, 2.8, 5.8, 8.6]
    assert lvls[0]["story_height_m"] is None
    assert abs(lvls[1]["story_height_m"] - 2.8) < 1e-4
    assert abs(lvls[2]["story_height_m"] - 3.0) < 1e-4
    assert abs(lvls[3]["story_height_m"] - 2.8) < 1e-4


def test_manual_grid_master_grid_stamp_and_polygon_openings(isolated_env):
    """
    Test:
      1. Manual grid tracing + deleting an element
      2. Locking Master Grid in meters from Left region and stamping onto Right region at new anchor_px
      3. Non-rectangular polygon opening creation, copying to Right region, and transferring to
         ProjectState.openings with accurate shoelace area_sqm and strict ProjectState validation.
    """
    client, data_root = isolated_env
    pid = "MASTER-OPN"
    assert client.post("/api/projects", json=_make_sample_project(pid)).status_code == 200

    pdf_bytes = _build_architectural_multipage_set_pdf()
    up = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("arch_master.pdf", pdf_bytes, "application/pdf")},
    )
    assert up.status_code == 200
    doc_state = up.json()
    doc_id = doc_state["doc_id"]

    p2 = doc_state["pages"][1]
    sx = p2["width_px"] / p2["width_pt"]
    sy = p2["height_px"] / p2["height_pt"]
    # Calibrate Page 2: 180 pt = 6.0 m -> 30 pt/m (confirmed)
    cal = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/2/calibrate",
        json={
            "p1": {"x_px": 90.0 * sx, "y_px": 350.0 * sy},
            "p2": {"x_px": 270.0 * sx, "y_px": 350.0 * sy},
            "known_distance": 6.0,
            "unit": "m",
            "confirmed": True,
        },
    )
    assert cal.status_code == 200

    p2_regions = [r for r in cal.json()["regions"] if r["page_number"] == 2]
    left_reg, right_reg = p2_regions[0], p2_regions[1]

    # Set Left region origin at Grid A-1' (90, 350 pt) and floor_level
    assert (
        client.patch(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{left_reg['region_id']}",
            json={"origin_px": {"x_px": 90.0 * sx, "y_px": 350.0 * sy}, "floor_level": "B1"},
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{right_reg['region_id']}",
            json={"floor_level": "1F"},
        ).status_code
        == 200
    )

    # 1. Manually trace grids A (x=0m), B (x=6m), 1' (y=0m), 2 (y=4m -> 350 - 120 = 230 pt) + a stray grid to delete
    for etype, label, p1_pt, p2_pt in [
        ("grid_x", "A", (90.0, 90.0), (90.0, 400.0)),
        ("grid_x", "B", (270.0, 90.0), (270.0, 400.0)),
        ("grid_y", "1'", (50.0, 350.0), (340.0, 350.0)),
        ("grid_y", "2", (50.0, 230.0), (340.0, 230.0)),
        ("grid_x", "TEMP", (180.0, 90.0), (180.0, 400.0)),
    ]:
        r = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/elements/manual-grid",
            json={
                "page_number": 2,
                "region_id": left_reg["region_id"],
                "element_type": etype,
                "label": label,
                "p1": {"x_px": p1_pt[0] * sx, "y_px": p1_pt[1] * sy},
                "p2": {"x_px": p2_pt[0] * sx, "y_px": p2_pt[1] * sy},
            },
        )
        assert r.status_code == 200, r.text

    temp_grid = [g for g in r.json()["grid_candidates"] if g["label"] == "TEMP"][0]
    del_res = client.delete(f"/api/projects/{pid}/pdf-imports/{doc_id}/elements/{temp_grid['id']}")
    assert del_res.status_code == 200
    assert not any(g["label"] == "TEMP" for g in del_res.json()["grid_candidates"])

    # 2. Lock Master Grid from Left region and stamp onto Right region at anchor (490, 350 pt)
    lock_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/master-grid",
        json={"source_region_id": left_reg["region_id"]},
    )
    assert lock_res.status_code == 200, lock_res.text
    mg = lock_res.json()["master_grid"]
    assert [(g["label"], round(g["offset_m"], 2)) for g in mg["grid_x_m"]] == [("A", 0.0), ("B", 6.0)]
    assert [(g["label"], round(g["offset_m"], 2)) for g in mg["grid_y_m"]] == [("1'", 0.0), ("2", 4.0)]

    stamp_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{right_reg['region_id']}/stamp-master-grid",
        json={
            "anchor_px": {"x_px": 490.0 * sx, "y_px": 350.0 * sy},
            "clear_existing_grids": True,
        },
    )
    assert stamp_res.status_code == 200, stamp_res.text
    right_grids = [
        g for g in stamp_res.json()["grid_candidates"] if g["region_id"] == right_reg["region_id"]
    ]
    assert len(right_grids) == 4
    rx_map = {g["label"]: g["coord_m"] for g in right_grids if g["axis"] == "x"}
    ry_map = {g["label"]: g["coord_m"] for g in right_grids if g["axis"] == "y"}
    assert abs(rx_map["A"] - 0.0) < 0.02
    assert abs(rx_map["B"] - 6.0) < 0.02
    assert abs(ry_map["1'"] - 0.0) < 0.02
    assert abs(ry_map["2"] - 4.0) < 0.02

    # 3. Trace non-rectangular L-shaped stairwell polygon in Left region:
    #    Meters relative to (90, 350 pt) at 30 pt/m:
    #    (0,0) -> (4,0) -> (4,2) -> (2,2) -> (2,4) -> (0,4)
    #    Exact area = 4*2 + 2*2 = 12.0 sqm!
    poly_m_expected = [(0.0, 0.0), (4.0, 0.0), (4.0, 2.0), (2.0, 2.0), (2.0, 4.0), (0.0, 4.0)]
    poly_px = [
        {"x_px": (90.0 + xm * 30.0) * sx, "y_px": (350.0 - ym * 30.0) * sy}
        for xm, ym in poly_m_expected
    ]
    op_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/openings",
        json={
            "page_number": 2,
            "region_id": left_reg["region_id"],
            "floor_level": "B1",
            "category": "stairwell",
            "label": "STAIR-L",
            "polygon_px": poly_px,
        },
    )
    assert op_res.status_code == 200, op_res.text
    assert len(op_res.json()["openings"]) == 1

    # Copy openings from Left region into Right region
    cp_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{right_reg['region_id']}/copy-openings",
        json={"source_region_id": left_reg["region_id"]},
    )
    assert cp_res.status_code == 200, cp_res.text
    assert cp_res.json()["copied_openings"] == 1

    # Transfer Right region grids + copied opening to ProjectState
    tx_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/transfer",
        json={"page_number": 2, "region_id": right_reg["region_id"], "merge_mode": "merge"},
    )
    assert tx_res.status_code == 200, tx_res.text
    tx = tx_res.json()
    assert tx["transferred_openings"] == 1
    assert tx["total_project_openings"] == 1

    # Reload ProjectState from JSON storage and verify exact area_sqm=12.0 and strict validation
    reloaded = ProjectState.model_validate(
        json.loads((data_root / "projects" / f"{pid}.json").read_text(encoding="utf-8"))
    )
    assert len(reloaded.openings) == 1
    opn = reloaded.openings[0]
    assert opn.label == "STAIR-L"
    assert opn.category == "stairwell"
    assert opn.floor_level == "1F"
    assert abs(opn.area_sqm - 12.0) < 0.05


def _draw_cross_hatched_rect(shape, x0: float, y0: float, x1: float, y1: float, step: float = 2.4):
    """Draw a bordered rectangular column with dense 45° / 135° cross-hatch strokes inside."""
    shape.draw_rect(pymupdf.Rect(x0, y0, x1, y1))
    shape.finish(color=(0.1, 0.1, 0.1), width=0.8)
    w = x1 - x0
    h = y1 - y0
    # 45-degree hatch lines (bottom-left to top-right)
    d = step
    while d < w + h:
        px1 = x0 + max(0.0, d - h)
        py1 = y0 + min(h, d)
        px2 = x0 + min(w, d)
        py2 = y0 + max(0.0, d - w)
        shape.draw_line(pymupdf.Point(px1, py1), pymupdf.Point(px2, py2))
        shape.finish(color=(0.15, 0.15, 0.15), width=0.55)
        d += step
    # 135-degree hatch lines (top-left to bottom-right)
    d = step
    while d < w + h:
        px1 = x0 + max(0.0, d - h)
        py1 = y1 - min(h, d)
        px2 = x0 + min(w, d)
        py2 = y1 - max(0.0, d - w)
        shape.draw_line(pymupdf.Point(px1, py1), pymupdf.Point(px2, py2))
        shape.finish(color=(0.15, 0.15, 0.15), width=0.55)
        d += step


def test_shaded_hatched_column_scanner_filters_non_columns_and_preserves_grids(isolated_env):
    """
    Verify tuned shaded/hatched column scanner:
      1. Gray-shaded square columns (0.40 m x 0.40 m) and cross-hatched rectangular columns
         (0.30 m x 0.60 m) ARE detected.
      2. Hollow furniture/room rectangles, solid pitch-black stove icon, circular tag,
         long thin wall (0.20 m x 4.00 m), and large gray L-shaped earth backfill (3.0 m x 4.0 m)
         are ALL REJECTED under column_fill_mode="shaded_or_hatched".
      3. detect_grids=False, detect_columns=True preserves existing stamped/manual grids while
         detecting only the shaded/hatched columns and snapping on-grid hits.
    """
    client, _ = isolated_env
    pid = "COL-SCAN"
    assert client.post("/api/projects", json=_make_sample_project(pid)).status_code == 200

    # Scale: 30 pt = 1.0 m
    pt_per_m = 30.0
    doc = pymupdf.open()
    pg = doc.new_page(width=760, height=540)
    sh = pg.new_shape()

    # Grid intersection centers (in pt): A=120, B=300; 1'=380, 2=220
    # 1. Two gray-shaded square columns (0.40 m x 0.40 m = 12 x 12 pt) at A-1' and B-1'
    gray_centers_pt = [(120.0, 380.0), (300.0, 380.0)]
    for cx, cy in gray_centers_pt:
        hw = 0.40 * pt_per_m * 0.5  # 6 pt
        sh.draw_rect(pymupdf.Rect(cx - hw, cy - hw, cx + hw, cy + hw))
        sh.finish(color=(0.1, 0.1, 0.1), fill=(0.55, 0.55, 0.55), width=0.7)

    # 2. Two cross-hatched rectangular columns (0.30 m x 0.60 m = 9 x 18 pt):
    #    one on-grid at A-2 (120, 220) and one off-grid along a skewed wall at (420, 290)
    hatch_centers_pt = [(120.0, 220.0), (420.0, 290.0)]
    for cx, cy in hatch_centers_pt:
        hw = 0.30 * pt_per_m * 0.5  # 4.5 pt
        hh = 0.60 * pt_per_m * 0.5  # 9.0 pt
        _draw_cross_hatched_rect(sh, cx - hw, cy - hh, cx + hw, cy + hh, step=2.2)

    # --- Distractors that MUST ALL BE REJECTED under shaded_or_hatched ---
    # 3a. Hollow furniture rectangle (0.50 m x 0.80 m = 15 x 24 pt) + hollow room (2.0 m x 2.0 m = 60 x 60 pt)
    sh.draw_rect(pymupdf.Rect(195.0, 285.0, 210.0, 309.0))
    sh.finish(color=(0.15, 0.15, 0.15), width=0.8)
    sh.draw_rect(pymupdf.Rect(180.0, 120.0, 240.0, 180.0))
    sh.finish(color=(0.15, 0.15, 0.15), width=0.8)

    # 3b. Solid pitch-black stove icon (0.50 m x 0.50 m = 15 x 15 pt)
    sh.draw_rect(pymupdf.Rect(345.0, 130.0, 360.0, 145.0))
    sh.finish(color=(0.0, 0.0, 0.0), fill=(0.0, 0.0, 0.0), width=0.6)

    # 3c. Circular shaded tag (diameter 0.50 m = radius 7.5 pt)
    sh.draw_circle(pymupdf.Point(430.0, 150.0), 7.5)
    sh.finish(color=(0.1, 0.1, 0.1), fill=(0.55, 0.55, 0.55), width=0.7)

    # 3d. Long thin gray wall (0.20 m x 4.00 m = 6 x 120 pt)
    sh.draw_rect(pymupdf.Rect(500.0, 140.0, 506.0, 260.0))
    sh.finish(color=(0.1, 0.1, 0.1), fill=(0.55, 0.55, 0.55), width=0.6)

    # 3e. Large gray L-shaped earth backfill (3.0 m x 4.0 m = 90 x 120 pt)
    l_pts = [
        pymupdf.Point(560.0, 160.0),
        pymupdf.Point(650.0, 160.0),
        pymupdf.Point(650.0, 220.0),
        pymupdf.Point(605.0, 220.0),
        pymupdf.Point(605.0, 280.0),
        pymupdf.Point(560.0, 280.0),
    ]
    sh.draw_polyline(l_pts)
    sh.finish(color=(0.2, 0.2, 0.2), fill=(0.60, 0.60, 0.60), closePath=True, width=0.7)

    sh.commit()
    pdf_bytes = doc.tobytes()
    doc.close()

    up = client.post(
        f"/api/projects/{pid}/pdf-imports",
        files={"file": ("col_scan.pdf", pdf_bytes, "application/pdf")},
    )
    assert up.status_code == 200, up.text
    doc_state = up.json()
    doc_id = doc_state["doc_id"]
    p1 = doc_state["pages"][0]
    sx = p1["width_px"] / p1["width_pt"]
    sy = p1["height_px"] / p1["height_pt"]

    # Calibrate page at 30 pt/m (180 pt = 6.0 m) and confirm
    cal = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/calibrate",
        json={
            "p1": {"x_px": 120.0 * sx, "y_px": 380.0 * sy},
            "p2": {"x_px": 300.0 * sx, "y_px": 380.0 * sy},
            "known_distance": 6.0,
            "unit": "m",
            "confirmed": True,
        },
    )
    assert cal.status_code == 200, cal.text

    # Create a PlanRegion and trace/stamp 4 grids (A, B, 1', 2)
    reg_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/regions",
        json={
            "page_number": 1,
            "name": "First Floor Plan",
            "floor_level": "1F",
            "x_px": 40.0 * sx,
            "y_px": 40.0 * sy,
            "width_px": 680.0 * sx,
            "height_px": 440.0 * sy,
            "origin_px": {"x_px": 120.0 * sx, "y_px": 380.0 * sy},
        },
    )
    assert reg_res.status_code == 200, reg_res.text
    region_id = reg_res.json()["regions"][0]["region_id"]

    for etype, label, p1_pt, p2_pt in [
        ("grid_x", "A", (120.0, 60.0), (120.0, 450.0)),
        ("grid_x", "B", (300.0, 60.0), (300.0, 450.0)),
        ("grid_y", "1'", (60.0, 380.0), (680.0, 380.0)),
        ("grid_y", "2", (60.0, 220.0), (680.0, 220.0)),
    ]:
        gr = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/elements/manual-grid",
            json={
                "page_number": 1,
                "region_id": region_id,
                "element_type": etype,
                "label": label,
                "p1": {"x_px": p1_pt[0] * sx, "y_px": p1_pt[1] * sy},
                "p2": {"x_px": p2_pt[0] * sx, "y_px": p2_pt[1] * sy},
            },
        )
        assert gr.status_code == 200, gr.text
    stamped_grid_ids = {g["id"] for g in gr.json()["grid_candidates"]}
    assert len(stamped_grid_ids) == 4

    # Run "Scan Columns Only" (detect_grids=False, detect_columns=True, shaded_or_hatched)
    scan_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract",
        json={
            "method": "auto",
            "region_id": region_id,
            "options": {
                "detect_grids": False,
                "detect_columns": True,
                "column_fill_mode": "shaded_or_hatched",
                "min_column_size_m": 0.18,
                "max_column_size_m": 1.10,
                "column_max_aspect_ratio": 2.5,
                "column_min_rectangularity": 0.78,
                "column_min_fill_ratio": 0.35,
                "require_near_grid": False,
            },
        },
    )
    assert scan_res.status_code == 200, scan_res.text
    body = scan_res.json()

    # Existing stamped grids MUST be untouched
    after_grid_ids = {g["id"] for g in body["document"]["grid_candidates"]}
    assert after_grid_ids == stamped_grid_ids
    assert body["grids_detected"] == 0

    # EXACTLY the 4 shaded/hatched columns detected; ZERO distractors (hollow, stove, circle, wall, backfill)
    cols = body["document"]["column_candidates"]
    assert body["columns_detected"] == 4, f"Expected 4 columns, got {len(cols)}: {cols}"
    assert len(cols) == 4

    # Verify each expected center (2 gray squares + 2 cross-hatched rectangles) was matched
    expected_px = [(cx * sx, cy * sy) for cx, cy in (gray_centers_pt + hatch_centers_pt)]
    for ex, ey in expected_px:
        assert any(
            abs(c["x_px"] - ex) <= 8.0 and abs(c["y_px"] - ey) <= 8.0 for c in cols
        ), f"Expected column near ({ex:.1f}, {ey:.1f}) missing in {cols}"

    # Verify grid snapping & tagging for on-grid columns + off-grid column kept
    tagged = sorted(c["grid"] for c in cols if c["grid"])
    assert tagged == ["A-1'", "A-2", "B-1'"]
    off_grid = [c for c in cols if not c["grid"]]
    assert len(off_grid) == 1

    # Verify require_near_grid=True drops the off-grid column while keeping the 3 on-grid columns
    near_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract",
        json={
            "method": "auto",
            "region_id": region_id,
            "options": {
                "detect_grids": False,
                "detect_columns": True,
                "column_fill_mode": "shaded_or_hatched",
                "require_near_grid": True,
            },
        },
    )
    assert near_res.status_code == 200, near_res.text
    assert near_res.json()["columns_detected"] == 3
    assert {g["id"] for g in near_res.json()["document"]["grid_candidates"]} == stamped_grid_ids

    # Verify gray_shaded_only keeps ONLY the 2 gray-shaded square columns
    gray_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract",
        json={
            "method": "auto",
            "region_id": region_id,
            "options": {
                "detect_grids": False,
                "detect_columns": True,
                "column_fill_mode": "gray_shaded_only",
            },
        },
    )
    assert gray_res.status_code == 200, gray_res.text
    assert gray_res.json()["columns_detected"] == 2
