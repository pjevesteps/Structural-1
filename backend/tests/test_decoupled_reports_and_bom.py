from __future__ import annotations

import io
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from docx import Document
from fastapi.testclient import TestClient
import pymupdf

from backend.app.main import app


def _make_project_payload(pid: str = "DEC-01") -> dict:
    return {
        "project_info": {
            "project_id": pid,
            "project_name": "Baguio 2-Storey SMRF Office",
            "location": "Baguio City, Benguet",
            "design_code": "NSCP 2015",
            "materials": {
                "fc_mpa": 28.0,
                "fy_mpa": 415.0,
                "concrete_density_kn_m3": 23.54,
            },
        },
        "levels": [
            {"id": "GF", "elevation_m": 0.0},
            {"id": "2F", "elevation_m": 3.2},
            {"id": "ROOF", "elevation_m": 6.4},
        ],
        "columns": [
            {"id": "C1", "grid": "A-1", "x_m": 0.0, "y_m": 0.0, "width_mm": 400.0, "depth_mm": 400.0},
            {"id": "C2", "grid": "B-1", "x_m": 5.0, "y_m": 0.0, "width_mm": 400.0, "depth_mm": 400.0},
            {"id": "C3", "grid": "A-2", "x_m": 0.0, "y_m": 4.0, "width_mm": 400.0, "depth_mm": 400.0},
            {"id": "C4", "grid": "B-2", "x_m": 5.0, "y_m": 4.0, "width_mm": 400.0, "depth_mm": 400.0},
        ],
    }


def _run_through_phase6(client: TestClient, pid: str) -> None:
    assert client.post("/api/projects", json=_make_project_payload(pid)).status_code == 200
    assert client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={"default_support_type": "FIXED", "include_base_tie_beams": True, "footing_depth_m": 1.2},
    ).status_code == 200
    assert client.post(f"/api/projects/{pid}/sizing/auto-size", json={}).status_code == 200
    assert client.post(f"/api/projects/{pid}/loads/generate", json={}).status_code == 200
    assert client.post(f"/api/projects/{pid}/design/run", json={}).status_code == 200


def test_decoupled_structural_report_and_bom_lifecycle_and_stale_detection(tmp_path: Path, monkeypatch):
    vol = tmp_path / "decoupled_vol"
    monkeypatch.setenv("DATA_DIR", str(vol))
    monkeypatch.setenv("AUTH_ENABLED", "false")

    client = TestClient(app)
    pid = "DEC-LIFE-01"

    # 0. Before Phase 6 -> prerequisites not met; independent status reports missing + clear prereqs
    assert client.post("/api/projects", json=_make_project_payload(pid)).status_code == 200
    st0 = client.get(f"/api/projects/{pid}/reports/status")
    assert st0.status_code == 200
    d0 = st0.json()
    assert d0["structural_report"]["status"] == "missing"
    assert d0["structural_report"]["prerequisites_met"] is False
    assert len(d0["structural_report"]["missing_prerequisites"]) >= 1
    assert d0["bom"]["status"] == "missing"
    assert d0["bom"]["prerequisites_met"] is False
    assert client.post(f"/api/projects/{pid}/reports/structural-report/generate").status_code == 400
    assert client.post(f"/api/projects/{pid}/reports/bom/generate").status_code == 400

    # Complete Phases 3..6
    assert client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={"default_support_type": "FIXED", "include_base_tie_beams": True, "footing_depth_m": 1.2},
    ).status_code == 200
    assert client.post(f"/api/projects/{pid}/sizing/auto-size", json={}).status_code == 200
    assert client.post(f"/api/projects/{pid}/loads/generate", json={}).status_code == 200
    assert client.post(f"/api/projects/{pid}/design/run", json={}).status_code == 200

    # Save EngineerProfile with Ñ/ñ
    assert client.put(
        "/api/profile",
        json={
            "full_name": "Engr. Niño Peñaflor, RCE",
            "prc_license_no": "0199123",
            "prc_validity": "2029-12-31",
            "ptr_no": "PTR-2026-889",
            "ptr_date_issued": "2026-01-12",
            "ptr_place_issued": "Parañaque City",
            "tin": "123-456-789-000",
            "firm_name": "Peñaflor Structural Consultancy",
        },
    ).status_code == 200

    # Missing downloads return 404 with actionable message
    miss_pdf = client.get(f"/api/projects/{pid}/reports/structural-report/download/pdf")
    assert miss_pdf.status_code == 404
    assert "Generate Structural Report" in miss_pdf.json()["detail"]
    miss_bom = client.get(f"/api/projects/{pid}/reports/bom/download/csv")
    assert miss_bom.status_code == 404
    assert "Generate BOM" in miss_bom.json()["detail"]

    # 1. Generate ONLY Structural Report -> structural_report='ready', bom stays 'missing'
    gen_sr = client.post(f"/api/projects/{pid}/reports/structural-report/generate", json={})
    assert gen_sr.status_code == 200, gen_sr.text
    sr_body = gen_sr.json()
    assert sr_body["status"] == "ready"
    assert sr_body["deliverables"]["bom"]["status"] == "missing"

    # Preview & download Structural Report PDF/DOCX
    prev_pdf = client.get(f"/api/projects/{pid}/reports/structural-report/preview/pdf")
    assert prev_pdf.status_code == 200
    assert prev_pdf.headers["content-type"].startswith("application/pdf")
    assert "inline;" in prev_pdf.headers["content-disposition"]
    assert prev_pdf.headers["x-deliverable-status"] == "ready"
    orig_pdf_bytes = prev_pdf.content

    dl_docx = client.get(f"/api/projects/{pid}/reports/structural-report/download/docx")
    assert dl_docx.status_code == 200
    assert "attachment;" in dl_docx.headers["content-disposition"]
    assert dl_docx.headers["x-deliverable-status"] == "ready"
    orig_docx_bytes = dl_docx.content

    # Verify enriched NSCP 2015 sections in BOTH PDF and DOCX
    pdf_doc = pymupdf.open(stream=orig_pdf_bytes, filetype="pdf")
    try:
        assert pdf_doc.page_count >= 5
        pdf_text = "\n".join(pg.get_text("text") for pg in pdf_doc)
    finally:
        pdf_doc.close()
    for expected in (
        "Niño Peñaflor",
        "0199123",
        "Special Moment Resisting Frame (SMRF)",
        "1.2 Governing NSCP 2015 & NBCP Code References",
        "4.2 Governing Member & Support Demand Envelopes",
        "Section 5B - Per-Member Design Calculation Sheets (NSCP 2015)",
        "Engineering Disclaimer",
        "Section 7 - Professional Certification & Dry Seal Block",
    ):
        assert expected in pdf_text, f"Missing '{expected}' in PDF report"

    docx_obj = Document(io.BytesIO(orig_docx_bytes))
    docx_text = "\n".join(p.text for p in docx_obj.paragraphs) + "\n" + "\n".join(
        cell.text for tbl in docx_obj.tables for row in tbl.rows for cell in row.cells
    )
    for expected in (
        "Engr. Niño Peñaflor, RCE",
        "Special Moment Resisting Frame (SMRF)",
        "1.2 Governing NSCP 2015 & NBCP Code References",
        "4.2 Governing Member & Support Demand Envelopes",
        "Section 5B · Per-Member Design Calculation Sheets (NSCP 2015)",
        "Engineering Disclaimer",
        "Section 7 · Professional Certification & Dry Seal Block",
    ):
        assert expected in docx_text, f"Missing '{expected}' in DOCX report"

    # 2. Generate ONLY BOM -> bom='ready', Structural Report stays 'ready' & untouched!
    gen_bom = client.post(
        f"/api/projects/{pid}/reports/bom/generate",
        json={"bom_params": {"rate_concrete_php_m3": 7200.0, "rate_rebar_php_kg": 58.0}},
    )
    assert gen_bom.status_code == 200, gen_bom.text
    bom_body = gen_bom.json()
    assert bom_body["status"] == "ready"
    assert bom_body["deliverables"]["structural_report"]["status"] == "ready"

    # Existing PDF bytes are strictly unchanged by BOM generation
    assert client.get(f"/api/projects/{pid}/reports/structural-report/download/pdf").content == orig_pdf_bytes

    dl_csv = client.get(f"/api/projects/{pid}/reports/bom/download/csv")
    assert dl_csv.status_code == 200
    assert dl_csv.headers["content-type"].startswith("text/csv")
    assert dl_csv.headers["x-deliverable-status"] == "ready"
    assert "GRAND TOTAL" in dl_csv.text

    # 3. Updating EngineerProfile marks BOTH deliverables 'stale', while downloads remain allowed!
    assert client.put(
        "/api/profile",
        json={"full_name": "Engr. Updated Signatory, RCE", "prc_license_no": "0999888"},
    ).status_code == 200

    st_stale = client.get(f"/api/projects/{pid}/reports/status").json()
    assert st_stale["structural_report"]["status"] == "stale"
    assert st_stale["bom"]["status"] == "stale"

    # Downloads are NOT blocked when stale, and report X-Deliverable-Status: stale
    stale_pdf = client.get(f"/api/projects/{pid}/reports/structural-report/download/pdf")
    assert stale_pdf.status_code == 200
    assert stale_pdf.headers["x-deliverable-status"] == "stale"
    stale_csv = client.get(f"/api/projects/{pid}/reports/bom/download/csv")
    assert stale_csv.status_code == 200
    assert stale_csv.headers["x-deliverable-status"] == "stale"

    # 4. Regenerating ONLY BOM makes bom='ready' while structural_report stays 'stale'
    regen_bom = client.post(f"/api/projects/{pid}/reports/bom/generate", json={})
    assert regen_bom.status_code == 200
    assert regen_bom.json()["deliverables"]["bom"]["status"] == "ready"
    assert regen_bom.json()["deliverables"]["structural_report"]["status"] == "stale"


def test_decoupled_report_artifacts_isolated_per_user_when_auth_enabled(tmp_path: Path, monkeypatch):
    vol = tmp_path / "beta_decoupled_vol"
    monkeypatch.setenv("DATA_DIR", str(vol))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_SECRET", "decoupled-test-secret-0123456789")
    monkeypatch.setenv("BETA_USERS", "alice:apass,bob:bpass")

    alice = TestClient(app)
    bob = TestClient(app)

    assert alice.post("/api/auth/login", json={"username": "alice", "password": "apass"}).status_code == 200
    assert bob.post("/api/auth/login", json={"username": "bob", "password": "bpass"}).status_code == 200

    assert alice.put("/api/profile", json={"full_name": "Engr. Alice Reyes", "prc_license_no": "111222"}).status_code == 200
    assert bob.put("/api/profile", json={"full_name": "Engr. Bob Santos", "prc_license_no": "333444"}).status_code == 200

    pid = "ISO-01"
    _run_through_phase6(alice, pid)
    _run_through_phase6(bob, pid)

    # Alice generates Structural Report only; Bob generates BOM only
    assert alice.post(f"/api/projects/{pid}/reports/structural-report/generate", json={}).status_code == 200
    assert bob.post(f"/api/projects/{pid}/reports/bom/generate", json={}).status_code == 200

    st_a = alice.get(f"/api/projects/{pid}/reports/status").json()
    st_b = bob.get(f"/api/projects/{pid}/reports/status").json()

    assert st_a["structural_report"]["status"] == "ready"
    assert st_a["bom"]["status"] == "missing"
    assert st_b["structural_report"]["status"] == "missing"
    assert st_b["bom"]["status"] == "ready"

    # Artifacts exist only under each user's isolated directory
    assert (vol / "users" / "alice" / "exports" / "reports" / pid / f"{pid}_Structural_Calculation_Report.pdf").is_file()
    assert not (vol / "users" / "bob" / "exports" / "reports" / pid / f"{pid}_Structural_Calculation_Report.pdf").is_file()
    assert (vol / "users" / "bob" / "exports" / "reports" / pid / f"{pid}_Structural_BOM.csv").is_file()
    assert not (vol / "users" / "alice" / "exports" / "reports" / pid / f"{pid}_Structural_BOM.csv").is_file()

    # Alice's PDF contains Alice's signatory, never Bob's
    pdf_a = pymupdf.open(
        stream=alice.get(f"/api/projects/{pid}/reports/structural-report/download/pdf").content,
        filetype="pdf",
    )
    try:
        txt_a = "\n".join(pg.get_text("text") for pg in pdf_a)
    finally:
        pdf_a.close()
    assert "Engr. Alice Reyes" in txt_a
    assert "Engr. Bob Santos" not in txt_a
