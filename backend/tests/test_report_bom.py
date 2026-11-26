from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from docx import Document
from fastapi.testclient import TestClient
import pymupdf
import pytest

from backend.app import main as main_mod
from backend.app import storage as storage_mod
from backend.app.models import (
    BomCostParameters,
    Column,
    EngineerSignatory,
    Level,
    ProjectInfo,
    ProjectMaterialProperties,
    ProjectState,
)
from backend.app.reports.bom_engine import (
    compute_bom_summary,
    parse_bar_callout,
    parse_spacing_callout,
    pns49_unit_mass_kg_m,
)


def _make_sample_project(pid: str = "REP-TEST-01", footing_depth_m: float = 1.5) -> ProjectState:
    return ProjectState(
        project_info=ProjectInfo(
            project_id=pid,
            project_name="Baguio 3-Storey RC Residence",
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
            Level(id="3F", elevation_m=6.0),
            Level(id="ROOF", elevation_m=9.0),
        ],
        columns=[
            Column(id="C-A1", grid="A-1", floor_level="1F", x_m=0.0, y_m=0.0, width_mm=400, depth_mm=400),
            Column(id="C-B1", grid="B-1", floor_level="1F", x_m=4.0, y_m=0.0, width_mm=400, depth_mm=400),
            Column(id="C-A2", grid="A-2", floor_level="1F", x_m=0.0, y_m=4.0, width_mm=400, depth_mm=400),
            Column(id="C-B2", grid="B-2", floor_level="1F", x_m=4.0, y_m=4.0, width_mm=400, depth_mm=400),
        ],
    )


@pytest.fixture()
def report_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "projects"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage_mod, "DATA_DIR", data_dir)
    return TestClient(main_mod.app), data_dir


def _run_phases_3_to_6(client: TestClient, pid: str, footing_depth_m: float = 1.5) -> dict:
    p = _make_sample_project(pid, footing_depth_m=footing_depth_m)
    assert client.post("/api/projects", json=p.model_dump()).status_code == 200
    r3 = client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={
            "default_support_type": "FIXED",
            "include_base_tie_beams": True,
            "footing_depth_m": footing_depth_m,
        },
    )
    assert r3.status_code == 200, r3.text
    r4 = client.post(f"/api/projects/{pid}/sizing/auto-size")
    assert r4.status_code == 200, r4.text
    r5 = client.post(f"/api/projects/{pid}/loads/generate")
    assert r5.status_code == 200, r5.text
    r6 = client.post(f"/api/projects/{pid}/design/run")
    assert r6.status_code == 200, r6.text
    return client.get(f"/api/projects/{pid}").json()


def test_pns49_rebar_mass_waste_scaling_and_commercial_pieces(report_client):
    client, _ = report_client
    pid = "REP-REBAR-01"
    raw = _run_phases_3_to_6(client, pid, footing_depth_m=1.2)
    project = ProjectState.model_validate(raw)

    assert parse_bar_callout("3-16mm") == (3, 16)
    assert parse_bar_callout("9-16mm @ 175mm o.c.") == (9, 16)
    assert parse_spacing_callout("10mm @ 100mm o.c.") == (10, 100.0)
    assert pns49_unit_mass_kg_m(16) == pytest.approx(256.0 / 162.0, rel=1e-6)

    params_6m = BomCostParameters(rebar_waste_splice_pct=12.0, commercial_bar_length_m=6.0)
    bom_6m = compute_bom_summary(project, params_6m)
    assert len(bom_6m.rebar_rows) >= 2

    for row in bom_6m.rebar_rows:
        expected_unit = (row.diameter_mm ** 2) / 162.0
        assert row.unit_mass_kg_m == pytest.approx(expected_unit, abs=1e-4)
        assert row.gross_length_m == pytest.approx(row.net_length_m * 1.12, abs=0.05)
        assert row.total_weight_kg == pytest.approx(row.gross_length_m * expected_unit, rel=0.005)
        assert row.commercial_pieces == math.ceil(row.gross_length_m / 6.0)
        if row.diameter_mm <= 12:
            assert "Grade 40" in row.grade_label
        else:
            assert "Grade 60" in row.grade_label

    # Switching commercial bar length to 12.0 m halves (ceiling) commercial pieces
    params_12m = BomCostParameters(rebar_waste_splice_pct=12.0, commercial_bar_length_m=12.0)
    bom_12m = compute_bom_summary(project, params_12m)
    for r6, r12 in zip(bom_6m.rebar_rows, bom_12m.rebar_rows):
        assert r12.commercial_pieces == math.ceil(r12.gross_length_m / 12.0)
        assert r12.commercial_pieces <= r6.commercial_pieces


def test_concrete_and_formwork_exact_geometry_hand_verification(report_client):
    client, _ = report_client
    pid = "REP-CONC-01"
    raw = _run_phases_3_to_6(client, pid, footing_depth_m=1.5)
    project = ProjectState.model_validate(raw)

    bom_params = BomCostParameters(concrete_waste_pct=5.0)
    bom = compute_bom_summary(project, bom_params)
    by_group = {r.element_group: r for r in bom.concrete_rows}

    sec_by_id = {s.section_id: s for s in project.member_sizing.sections}
    asgn_by_mid = {a.member_id: a for a in project.member_sizing.member_assignments}
    slab_by_pid = {s.panel_id: s for s in project.member_sizing.slab_assignments}
    lvl_elev = {lv.id: lv.elevation_m for lv in project.levels}
    base_elev = min(lvl_elev.values())

    # 1. Footings hand check
    exp_ftg_vol = sum(f.b_m * f.l_m * (f.thickness_mm / 1000.0) for f in project.analysis_design.footing_designs)
    exp_ftg_fw = sum(2.0 * (f.b_m + f.l_m) * (f.thickness_mm / 1000.0) for f in project.analysis_design.footing_designs)
    assert by_group["Isolated Footings"].member_count == len(project.analysis_design.footing_designs)
    assert by_group["Isolated Footings"].net_volume_m3 == pytest.approx(exp_ftg_vol, abs=0.005)
    assert by_group["Isolated Footings"].gross_volume_m3 == pytest.approx(exp_ftg_vol * 1.05, abs=0.005)
    assert by_group["Isolated Footings"].formwork_area_m2 == pytest.approx(exp_ftg_fw, abs=0.02)

    # 2. Pedestals (is_pedestal=True) & Columns (is_pedestal=False)
    exp_ped_vol = 0.0
    exp_ped_fw = 0.0
    exp_ped_cnt = 0
    exp_col_vol = 0.0
    exp_col_fw = 0.0
    exp_col_cnt = 0
    exp_bm_vol = 0.0
    exp_bm_fw = 0.0
    exp_bm_cnt = 0

    for m in project.structural_model.members:
        sec = sec_by_id[asgn_by_mid[m.member_id].section_id]
        b = sec.width_mm / 1000.0
        h = sec.depth_mm / 1000.0
        v = b * h * m.length_m
        if m.member_type == "column":
            fw = 2.0 * (b + h) * m.length_m
            if m.is_pedestal:
                exp_ped_cnt += 1
                exp_ped_vol += v
                exp_ped_fw += fw
            else:
                exp_col_cnt += 1
                exp_col_vol += v
                exp_col_fw += fw
        else:
            exp_bm_cnt += 1
            exp_bm_vol += v
            exp_bm_fw += (2.0 * h * m.length_m) if m.member_type == "tie_beam" else ((b + 2.0 * h) * m.length_m)

    assert exp_ped_cnt == 4  # 4 dropped pedestals when footing_depth_m = 1.5
    assert by_group["Column Pedestals"].member_count == exp_ped_cnt
    assert by_group["Column Pedestals"].net_volume_m3 == pytest.approx(exp_ped_vol, abs=0.005)
    assert by_group["Column Pedestals"].formwork_area_m2 == pytest.approx(exp_ped_fw, abs=0.02)

    assert by_group["Columns"].member_count == exp_col_cnt
    assert by_group["Columns"].net_volume_m3 == pytest.approx(exp_col_vol, abs=0.005)
    assert by_group["Columns"].formwork_area_m2 == pytest.approx(exp_col_fw, abs=0.02)

    assert by_group["Beams & Tie Beams"].member_count == exp_bm_cnt
    assert by_group["Beams & Tie Beams"].net_volume_m3 == pytest.approx(exp_bm_vol, abs=0.005)
    assert by_group["Beams & Tie Beams"].formwork_area_m2 == pytest.approx(exp_bm_fw, abs=0.02)

    # 3. Slabs hand check
    exp_slab_vol = 0.0
    exp_slab_fw = 0.0
    for panel in project.structural_model.slab_panels:
        if panel.slab_action == "void" or panel.net_area_sqm <= 0:
            continue
        t_mm = slab_by_pid[panel.panel_id].thickness_mm
        exp_slab_vol += panel.net_area_sqm * (t_mm / 1000.0)
        if lvl_elev[panel.floor_level] > base_elev + 0.05:
            exp_slab_fw += panel.net_area_sqm

    assert by_group["RC Slabs"].net_volume_m3 == pytest.approx(exp_slab_vol, abs=0.005)
    assert by_group["RC Slabs"].formwork_area_m2 == pytest.approx(exp_slab_fw, abs=0.02)


def test_cost_estimate_totals_and_rate_sensitivity(report_client):
    client, _ = report_client
    pid = "REP-COST-01"
    _run_phases_3_to_6(client, pid, footing_depth_m=0.0)

    r1 = client.post(
        f"/api/projects/{pid}/reports/generate",
        json={
            "bom_params": {
                "rate_concrete_php_m3": 6500.0,
                "rate_rebar_php_kg": 52.0,
                "labor_pct_of_materials": 35.0,
                "include_chb_masonry": True,
            }
        },
    )
    assert r1.status_code == 200, r1.text
    b1 = r1.json()["bom"]
    assert b1["total_material_cost_php"] > 0
    assert b1["total_labor_cost_php"] == pytest.approx(b1["total_material_cost_php"] * 0.35, abs=0.1)
    assert b1["grand_total_cost_php"] == pytest.approx(
        b1["total_material_cost_php"] + b1["total_labor_cost_php"], abs=0.02
    )

    # Increase concrete rate & labor % -> grand total must strictly increase accurately
    r2 = client.post(
        f"/api/projects/{pid}/reports/generate",
        json={
            "bom_params": {
                "rate_concrete_php_m3": 8000.0,
                "rate_rebar_php_kg": 60.0,
                "labor_pct_of_materials": 45.0,
                "include_chb_masonry": False,
            }
        },
    )
    assert r2.status_code == 200, r2.text
    b2 = r2.json()["bom"]
    assert b2["total_chb_area_m2"] == 0.0
    assert b2["total_labor_cost_php"] == pytest.approx(b2["total_material_cost_php"] * 0.45, abs=0.1)
    assert b2["grand_total_cost_php"] == pytest.approx(
        b2["total_material_cost_php"] + b2["total_labor_cost_php"], abs=0.02
    )
    assert all("CHB" not in line["description"] for line in b2["cost_lines"])


def test_docx_pdf_bom_csv_downloads_and_legacy_json_compatibility(report_client):
    client, data_dir = report_client
    pid = "REP-EXPORT-01"
    _run_phases_3_to_6(client, pid, footing_depth_m=1.2)

    # Readiness warnings check with blank signatory & pending geotech on 3-storey building
    r_warn = client.post(f"/api/projects/{pid}/reports/generate")
    assert r_warn.status_code == 200
    warns = r_warn.json()["readiness_warnings"]
    assert any("engineer name" in w.lower() for w in warns)
    assert any("prc license" in w.lower() for w in warns)
    assert any("geotechnical" in w.lower() for w in warns)

    # Now populate full signatory & geotech reference
    sig = EngineerSignatory(
        engineer_name="Engr. Maria Clara Reyes, CE",
        prc_license_no="0145678",
        prc_validity="2028-11-30",
        ptr_no="PTR-2026-0091",
        ptr_date="2026-01-10",
        ptr_place="Baguio City",
        tin="234-567-890-000",
        project_owner="Cordillera Estates Corp.",
        project_address="Session Road, Baguio City",
        geotech_report_ref="Geotech Report #GEO-2026-114 (Baguio Soils Lab)",
    )
    r_gen = client.post(
        f"/api/projects/{pid}/reports/generate",
        json={
            "signatory": sig.model_dump(),
            "options": {"include_staad_snippet": True},
        },
    )
    assert r_gen.status_code == 200, r_gen.text
    rep_state = r_gen.json()
    assert not any("engineer name" in w.lower() or "prc license" in w.lower() or "geotechnical" in w.lower() for w in rep_state["readiness_warnings"])

    # 1. Download DOCX & parse with python-docx
    docx_res = client.get(f"/api/projects/{pid}/reports/download/docx")
    assert docx_res.status_code == 200
    assert docx_res.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert docx_res.content[:2] == b"PK"  # valid ZIP/OOXML
    doc = Document(io.BytesIO(docx_res.content))
    all_docx_text = "\n".join(p.text for p in doc.paragraphs)
    for tbl in doc.tables:
        for row in tbl.rows:
            all_docx_text += "\n" + " | ".join(c.text for c in row.cells)
    assert "Baguio 3-Storey RC Residence" in all_docx_text
    assert "Engr. Maria Clara Reyes, CE" in all_docx_text
    assert "0145678" in all_docx_text
    assert "Section 3" in all_docx_text
    assert "Section 5" in all_docx_text
    assert "Section 6" in all_docx_text
    assert "Isolated Footings" in all_docx_text
    assert "TOTAL REBAR" in all_docx_text
    assert "STAAD SPACE" in all_docx_text

    # Verify OBO PDF is multi-page (>= 3 pages) with Soil Investigation & Running Headers
    pdf_res = client.get(f"/api/projects/{pid}/reports/download/pdf")
    assert pdf_res.status_code == 200
    assert pdf_res.content[:5] == b"%PDF-"
    pdf_doc = pymupdf.open(stream=pdf_res.content, filetype="pdf")
    assert pdf_doc.page_count >= 3
    full_pdf_text = "\n".join(pg.get_text() for pg in pdf_doc)
    assert "NSCP 2015 / NBCP Sec. 302-5" in full_pdf_text
    assert "Section 5" in full_pdf_text
    assert "Section 6" in full_pdf_text
    pdf_doc.close()

    # 2. Download PDF & parse with PyMuPDF
    pdf_res = client.get(f"/api/projects/{pid}/reports/download/pdf")
    assert pdf_res.status_code == 200
    assert pdf_res.headers["content-type"].startswith("application/pdf")
    assert pdf_res.content.startswith(b"%PDF-")
    pdf_doc = pymupdf.open(stream=pdf_res.content, filetype="pdf")
    assert pdf_doc.page_count >= 2
    pdf_text = "\n".join(pdf_doc[i].get_text("text") for i in range(pdf_doc.page_count))
    pdf_doc.close()
    assert "Baguio 3-Storey RC Residence" in pdf_text
    assert "Engr. Maria Clara Reyes, CE" in pdf_text
    assert "Isolated Footings" in pdf_text
    assert "GRAND TOTAL" in pdf_text.upper()

    # 3. Download BOM CSV & validate rows
    csv_res = client.get(f"/api/projects/{pid}/reports/download/bom-csv")
    assert csv_res.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_res.text)))
    flat = "\n".join(",".join(r) for r in rows)
    assert "=== CONCRETE & FORMWORK TAKEOFF ===" in flat
    assert "=== PNS 49:2020 REINFORCING STEEL BREAKDOWN ===" in flat
    assert "=== STRUCTURAL COST ESTIMATE (NBCP SEC. 302-5f, PHP) ===" in flat
    assert "GRAND TOTAL" in flat

    # 4. Legacy JSON without 'reporting' field loads cleanly under extra='forbid'
    disk_file = data_dir / f"{pid}.json"
    raw_disk = json.loads(disk_file.read_text(encoding="utf-8"))
    raw_disk.pop("reporting", None)
    reloaded = ProjectState.model_validate(raw_disk)
    assert reloaded.reporting.generated_at is None
    assert reloaded.reporting.bom is None


def test_report_generate_rejects_missing_phase6_with_400(report_client):
    client, _ = report_client
    pid = "REP-NOP6-01"
    p = _make_sample_project(pid)
    assert client.post("/api/projects", json=p.model_dump()).status_code == 200
    res = client.post(f"/api/projects/{pid}/reports/generate")
    assert res.status_code == 400
    assert "Phase 6" in res.json()["detail"]


def test_regional_cost_presets_and_trade_split_labor_rates(report_client):
    client, _ = report_client
    pid = "REP-REG-01"
    _run_phases_3_to_6(client, pid, footing_depth_m=0.0)

    # 1. Fetch Philippine regional presets endpoint
    pr = client.get("/api/reports/cost-presets")
    assert pr.status_code == 200
    presets = {p["key"]: p for p in pr.json()["presets"]}
    assert set(presets.keys()) == {"car_baguio", "ncr_manila", "cebu_visayas", "davao_mindanao"}
    # Baguio mountain hauling concrete rate exceeds Metro Manila
    assert presets["car_baguio"]["rate_concrete_php_m3"] > presets["ncr_manila"]["rate_concrete_php_m3"]

    # 2. Apply trade-split labor overrides and verify exact per-line labor math
    baguio = presets["car_baguio"]
    gen = client.post(
        f"/api/projects/{pid}/reports/generate",
        json={
            "bom_params": {
                "region_preset": "car_baguio",
                "rate_concrete_php_m3": baguio["rate_concrete_php_m3"],
                "rate_rebar_php_kg": baguio["rate_rebar_php_kg"],
                "rate_formwork_php_m2": baguio["rate_formwork_php_m2"],
                "labor_pct_of_materials": 35.0,
                "labor_concrete_pct": 25.0,
                "labor_rebar_pct": 40.0,
                "labor_formwork_pct": 50.0,
            }
        },
    )
    assert gen.status_code == 200, gen.text
    lines = {ln["item_no"]: ln for ln in gen.json()["bom"]["cost_lines"]}
    assert lines["01"]["labor_cost_php"] == pytest.approx(lines["01"]["material_cost_php"] * 0.25, abs=0.02)
    assert lines["02"]["labor_cost_php"] == pytest.approx(lines["02"]["material_cost_php"] * 0.40, abs=0.02)
    assert lines["04"]["labor_cost_php"] == pytest.approx(lines["04"]["material_cost_php"] * 0.50, abs=0.02)


def test_project_revision_snapshots_restore_no_recursion_and_bom_delta(report_client):
    client, data_dir = report_client
    pid = "REP-REV-01"
    _run_phases_3_to_6(client, pid, footing_depth_m=1.2)
    assert client.post(f"/api/projects/{pid}/reports/generate").status_code == 200

    # 1. Freeze Rev. 0 snapshot
    r0 = client.post(
        f"/api/projects/{pid}/revisions",
        json={"label": "Rev. 0 — Initial Permit Submission", "notes": "Baseline 400mm columns"},
    )
    assert r0.status_code == 200, r0.text
    rev0 = r0.json()
    assert rev0["revision_id"] == "REV-0"
    assert rev0["total_concrete_m3"] > 0
    assert rev0["total_rebar_kg"] > 0
    assert rev0["grand_total_cost_php"] > 0
    assert "revisions" not in rev0["frozen_state"]

    # 2. Upsize column section C1 by +150mm & re-run Phase 6 + Module 08
    proj = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    c1 = next(s for s in proj.member_sizing.sections if s.section_id == "C1")
    orig_w, orig_d = c1.width_mm, c1.depth_mm
    upd_sec = client.post(
        f"/api/projects/{pid}/sizing/sections",
        json=c1.model_copy(
            update={"width_mm": orig_w + 150.0, "depth_mm": orig_d + 150.0}
        ).model_dump(),
    )
    assert upd_sec.status_code == 200, upd_sec.text
    assert client.post(f"/api/projects/{pid}/design/run").status_code == 200
    assert client.post(f"/api/projects/{pid}/reports/generate").status_code == 200

    # 3. Compare modified working state against REV-0 -> positive concrete & cost deltas
    cmp_res = client.get(f"/api/projects/{pid}/revisions/compare", params={"base_rev": "REV-0"})
    assert cmp_res.status_code == 200, cmp_res.text
    delta = cmp_res.json()
    assert delta["base_rev"] == "REV-0"
    assert delta["concrete_delta_m3"] > 0.5
    assert delta["concrete_delta_pct"] > 0.0
    assert delta["cost_delta_php"] > 0.0
    assert delta["cost_delta_pct"] > 0.0

    # 4. Freeze Rev. 1, then restore Rev. 0 -> working state reverts while BOTH REV-0 & REV-1 remain intact
    r1 = client.post(
        f"/api/projects/{pid}/revisions",
        json={"label": "Rev. 1 — Upsized Columns", "notes": "+150mm column C1"},
    )
    assert r1.status_code == 200
    assert r1.json()["revision_id"] == "REV-1"
    assert "revisions" not in r1.json()["frozen_state"]

    rest = client.post(f"/api/projects/{pid}/revisions/REV-0/restore")
    assert rest.status_code == 200, rest.text
    restored = ProjectState.model_validate(rest.json())
    assert [r.revision_id for r in restored.revisions] == ["REV-0", "REV-1"]
    restored_c1 = next(s for s in restored.member_sizing.sections if s.section_id == "C1")
    assert restored_c1.width_mm == pytest.approx(orig_w)
    assert restored_c1.depth_mm == pytest.approx(orig_d)
    # Zero recursive nesting on disk
    disk_raw = json.loads((data_dir / f"{pid}.json").read_text(encoding="utf-8"))
    for rev in disk_raw["revisions"]:
        assert "revisions" not in rev["frozen_state"]
    # Delta vs REV-0 is now 0 after restoring
    zero_delta = client.get(f"/api/projects/{pid}/revisions/compare", params={"base_rev": "REV-0"}).json()
    assert zero_delta["concrete_delta_m3"] == pytest.approx(0.0, abs=0.01)
    assert zero_delta["cost_delta_php"] == pytest.approx(0.0, abs=0.1)
