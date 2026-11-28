from io import BytesIO
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from docx import Document
from fastapi.testclient import TestClient
import pymupdf

from backend.app.main import app
from backend.app.models import EngineerSignatory
from backend.app.profile import EngineerProfile, resolve_signatory


def _make_full_project_payload(pid: str = "PROF-01") -> dict:
    return {
        "project_info": {
            "project_id": pid,
            "project_name": "Profile Auto-Fill Frame",
            "location": "Baguio City, Philippines",
            "design_code": "NSCP 2015",
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
            "x_grids": [{"label": "A", "coord_m": 0.0}, {"label": "B", "coord_m": 5.0}],
            "y_grids": [{"label": "1", "coord_m": 0.0}, {"label": "2", "coord_m": 4.0}],
        },
        "columns": [
            {"id": "C1", "grid": "A-1", "x_m": 0.0, "y_m": 0.0, "width_mm": 350.0, "depth_mm": 350.0},
            {"id": "C2", "grid": "B-1", "x_m": 5.0, "y_m": 0.0, "width_mm": 350.0, "depth_mm": 350.0},
            {"id": "C3", "grid": "A-2", "x_m": 0.0, "y_m": 4.0, "width_mm": 350.0, "depth_mm": 350.0},
            {"id": "C4", "grid": "B-2", "x_m": 5.0, "y_m": 4.0, "width_mm": 350.0, "depth_mm": 350.0},
        ],
        "beams": [],
    }


def test_profile_get_put_defaults_corrupt_recovery_and_utf8(tmp_path, monkeypatch):
    vol = tmp_path / "prof_vol"
    monkeypatch.setenv("DATA_DIR", str(vol))
    monkeypatch.setenv("AUTH_ENABLED", "false")

    client = TestClient(app)

    # 1. Missing profile returns empty defaults with Cache-Control: no-store
    r0 = client.get("/api/profile")
    assert r0.status_code == 200
    assert "no-store" in (r0.headers.get("cache-control") or "")
    assert r0.json()["full_name"] == ""

    # 2. PUT creates missing parent directories, coerces null -> "", strips whitespace, saves UTF-8 (Ñ/ñ)
    put_payload = {
        "full_name": "  Engr. Niño Peñaflor, RCE  ",
        "prc_license_no": " 0145678 ",
        "prc_validity": "2028-09-30",
        "ptr_no": "PTR-889900",
        "ptr_date_issued": "2026-01-10",
        "ptr_place_issued": "Parañaque City",
        "tin": None,
        "firm_name": "Peñaflor Structural Partners",
        "office_address": "Suite 4B, Doña Soledad Ave., Parañaque",
        "contact_info": "+63 917 555 0199",
    }
    r_put = client.put("/api/profile", json=put_payload)
    assert r_put.status_code == 200, r_put.text
    saved = r_put.json()
    assert saved["full_name"] == "Engr. Niño Peñaflor, RCE"
    assert saved["prc_license_no"] == "0145678"
    assert saved["tin"] == ""
    assert (vol / "engineer_profile.json").is_file()

    r_get = client.get("/api/profile")
    assert r_get.status_code == 200
    assert r_get.json()["full_name"] == "Engr. Niño Peñaflor, RCE"
    assert r_get.json()["ptr_place_issued"] == "Parañaque City"

    # 3. Corrupted engineer_profile.json recovers gracefully to empty defaults
    (vol / "engineer_profile.json").write_text("{corrupt json", encoding="utf-8")
    r_rec = client.get("/api/profile")
    assert r_rec.status_code == 200
    assert r_rec.json()["full_name"] == ""


def test_profile_isolated_per_user_when_auth_enabled(tmp_path, monkeypatch):
    vol = tmp_path / "beta_prof_vol"
    monkeypatch.setenv("DATA_DIR", str(vol))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_SECRET", "profile-test-secret-0123456789")
    monkeypatch.setenv("BETA_USERS", "alice:apass,bob:bpass")

    alice = TestClient(app)
    bob = TestClient(app)

    assert alice.post("/api/auth/login", json={"username": "alice", "password": "apass"}).status_code == 200
    assert bob.post("/api/auth/login", json={"username": "bob", "password": "bpass"}).status_code == 200

    assert alice.put("/api/profile", json={"full_name": "Engr. Alice Reyes", "prc_license_no": "111111"}).status_code == 200
    assert bob.put("/api/profile", json={"full_name": "Engr. Bob Santos", "prc_license_no": "222222"}).status_code == 200

    assert (vol / "users" / "alice" / "engineer_profile.json").is_file()
    assert (vol / "users" / "bob" / "engineer_profile.json").is_file()
    assert alice.get("/api/profile").json()["full_name"] == "Engr. Alice Reyes"
    assert bob.get("/api/profile").json()["full_name"] == "Engr. Bob Santos"


def test_resolve_signatory_precedence_and_placeholders():
    prof = EngineerProfile(
        full_name="Engr. Juan Dela Cruz, RCE",
        prc_license_no="0123456",
        prc_validity="2028-12-31",
        ptr_no="998877",
        ptr_date_issued="2026-01-05",
        ptr_place_issued="Baguio City",
        tin="123-456-789",
        firm_name="Dela Cruz Structural",
        office_address="Line 1\nLine 2, Baguio",
        contact_info="engr@firm.ph",
    )

    # 1. Explicit project-level values take precedence; blank/placeholders fall back to profile
    sig = EngineerSignatory(
        engineer_name="Engr. Override Name",
        prc_license_no="TBD",
        prc_validity="n/a",
        ptr_no="-",
        ptr_date="",
        ptr_place="NA",
        tin="  ",
    )
    resolved = resolve_signatory(sig, prof)
    assert resolved["engineer_name"] == "Engr. Override Name"
    assert resolved["prc_license_no"] == "0123456"
    assert resolved["prc_validity"] == "2028-12-31"
    assert resolved["ptr_no"] == "998877"
    assert resolved["ptr_date"] == "2026-01-05"
    assert resolved["ptr_place"] == "Baguio City"
    assert resolved["tin"] == "123-456-789"
    assert resolved["office_address"] == "Line 1 Line 2, Baguio"

    # 2. When profile is empty, original project placeholders/defaults are preserved
    sig_only = EngineerSignatory(prc_license_no="TBD", ptr_no="-")
    resolved_empty = resolve_signatory(sig_only, EngineerProfile())
    assert resolved_empty["prc_license_no"] == "TBD"
    assert resolved_empty["ptr_no"] == "-"
    assert resolved_empty["engineer_name"] == ""


def test_exports_auto_populate_from_saved_engineer_profile(tmp_path, monkeypatch):
    vol = tmp_path / "export_prof_vol"
    monkeypatch.setenv("DATA_DIR", str(vol))
    monkeypatch.setenv("AUTH_ENABLED", "false")

    client = TestClient(app)

    # Save engineer profile with Ñ/ñ characters
    r_prof = client.put(
        "/api/profile",
        json={
            "full_name": "Engr. Niño Peñaflor, RCE",
            "prc_license_no": "0199887",
            "prc_validity": "2029-05-15",
            "ptr_no": "PTR-776655",
            "ptr_date_issued": "2026-01-20",
            "ptr_place_issued": "Parañaque City",
            "tin": "444-555-666-000",
            "firm_name": "Peñaflor Structural",
            "office_address": "Doña Soledad Ave., Parañaque",
            "contact_info": "+63 917 888 9999",
        },
    )
    assert r_prof.status_code == 200

    # Create and run project through Modules 03 -> 04 -> 05 -> 06 with BLANK project signatory
    pid = "PROF-EXP-01"
    assert client.post("/api/projects", json=_make_full_project_payload(pid)).status_code == 200
    assert client.post(f"/api/projects/{pid}/geometry/generate", json={}).status_code == 200
    assert client.post(f"/api/projects/{pid}/sizing/auto-size", json={}).status_code == 200
    assert client.post(f"/api/projects/{pid}/loads/generate", json={}).status_code == 200
    assert client.post(f"/api/projects/{pid}/design/run", json={}).status_code == 200

    # Module 07 DXF export picks up saved EngineerProfile
    r_dxf = client.get(f"/api/projects/{pid}/cad/download/dxf")
    assert r_dxf.status_code == 200, r_dxf.text
    dxf_text = r_dxf.content.decode("utf-8", errors="replace")
    assert "Engr. Niño Peñaflor, RCE" in dxf_text
    assert "0199887" in dxf_text
    assert "PTR-776655" in dxf_text
    assert "Peñaflor Structural" in dxf_text

    # Module 08 OBO PDF export picks up saved EngineerProfile (and renders Ñ/ñ without crashing)
    r_pdf = client.get(f"/api/projects/{pid}/reports/download/pdf")
    assert r_pdf.status_code == 200, r_pdf.text
    pdf_doc = pymupdf.open(stream=r_pdf.content, filetype="pdf")
    try:
        pdf_text = "\n".join(page.get_text("text") for page in pdf_doc)
    finally:
        pdf_doc.close()
    assert "Niño Peñaflor" in pdf_text
    assert "0199887" in pdf_text
    assert "PTR-776655" in pdf_text
    assert "Peñaflor Structural" in pdf_text

    # Module 08 OBO DOCX export picks up saved EngineerProfile
    r_docx = client.get(f"/api/projects/{pid}/reports/download/docx")
    assert r_docx.status_code == 200, r_docx.text
    docx_obj = Document(BytesIO(r_docx.content))
    docx_text = "\n".join(
        cell.text for tbl in docx_obj.tables for row in tbl.rows for cell in row.cells
    )
    assert "Engr. Niño Peñaflor, RCE" in docx_text
    assert "0199887" in docx_text
    assert "Peñaflor Structural" in docx_text

    # Module 08 readiness warnings should NOT complain about blank engineer_name / prc_license_no
    # when the saved EngineerProfile supplies them!
    r_status = client.get(f"/api/projects/{pid}/reports/status")
    assert r_status.status_code == 200, r_status.text
    warns = r_status.json().get("readiness_warnings", [])
    assert not any("engineer name is blank" in w.lower() for w in warns), warns
    assert not any("prc license number is blank" in w.lower() for w in warns), warns

