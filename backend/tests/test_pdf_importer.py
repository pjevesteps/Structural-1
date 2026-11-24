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
            s2.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.6)

    # Right viewport: FIRST FLOOR PLAN (shifted by +400 pt in X, 0 in Y -> x=490, 670; y=350, 190)
    p2.insert_text(pymupdf.Point(500, 445), "FIRST FLOOR PLAN", fontsize=12)
    for y in (350.0, 190.0):
        for x in (490.0, 670.0):
            s2.draw_rect(pymupdf.Rect(x - 6, y - 6, x + 6, y + 6))
            s2.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.6)

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
