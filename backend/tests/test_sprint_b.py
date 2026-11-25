from __future__ import annotations

import io
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ezdxf
from fastapi.testclient import TestClient
import pytest

from backend.app import main as main_mod
from backend.app import storage as storage_mod
from backend.app.models import (
    Column,
    Level,
    ProjectInfo,
    ProjectMaterialProperties,
    ProjectState,
)
from backend.tests.test_e2e_pipeline import lint_staad_text


def _shell_project(pid: str, with_columns: bool = True) -> ProjectState:
    cols = (
        [
            Column(id="C-A1", grid="A-1", floor_level="1F", x_m=0.0, y_m=0.0, width_mm=400, depth_mm=400),
            Column(id="C-B1", grid="B-1", floor_level="1F", x_m=4.0, y_m=0.0, width_mm=400, depth_mm=400),
            Column(id="C-A2", grid="A-2", floor_level="1F", x_m=0.0, y_m=4.0, width_mm=400, depth_mm=400),
            Column(id="C-B2", grid="B-2", floor_level="1F", x_m=4.0, y_m=4.0, width_mm=400, depth_mm=400),
        ]
        if with_columns
        else []
    )
    return ProjectState(
        project_info=ProjectInfo(
            project_id=pid,
            project_name="Sprint B Frame",
            location="Baguio City",
            design_code="NSCP 2015",
            materials=ProjectMaterialProperties(fc_mpa=28.0, fy_mpa=415.0, concrete_density_kn_m3=23.54),
        ),
        levels=[Level(id="1F", elevation_m=0.0), Level(id="2F", elevation_m=3.0), Level(id="ROOF", elevation_m=6.0)],
        columns=cols,
    )


@pytest.fixture()
def sb_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "projects"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage_mod, "DATA_DIR", data_dir)
    return TestClient(main_mod.app)


def _build_architectural_dxf() -> bytes:
    """2x2-bay-less 4m x 4m frame in millimetres ($INSUNITS=4): 2 X-grids, 2 Y-grids, 4 columns, 1 stair opening."""
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    for lname in ("S-GRID", "S-COL", "A-STAIR"):
        doc.layers.add(lname)
    msp = doc.modelspace()
    r = 300.0
    for lbl, x in (("A", 0.0), ("B", 4000.0)):
        msp.add_line((x, -1000.0), (x, 5000.0), dxfattribs={"layer": "S-GRID", "linetype": "CENTER"})
        msp.add_circle((x, 5000.0 + r), r, dxfattribs={"layer": "S-GRID"})
        msp.add_text(lbl, height=200, dxfattribs={"layer": "S-GRID"}).set_placement((x, 5000.0 + r))
    for lbl, y in (("1", 0.0), ("2", 4000.0)):
        msp.add_line((-1000.0, y), (5000.0, y), dxfattribs={"layer": "S-GRID", "linetype": "CENTER"})
        msp.add_circle((-1000.0 - r, y), r, dxfattribs={"layer": "S-GRID"})
        msp.add_text(lbl, height=200, dxfattribs={"layer": "S-GRID"}).set_placement((-1000.0 - r, y))
    for x in (0.0, 4000.0):
        for y in (0.0, 4000.0):
            msp.add_lwpolyline(
                [(x - 200, y - 200), (x + 200, y - 200), (x + 200, y + 200), (x - 200, y + 200)],
                close=True,
                dxfattribs={"layer": "S-COL"},
            )
    msp.add_lwpolyline(
        [(1000, 1000), (2500, 1000), (2500, 2500), (1000, 2500)],
        close=True,
        dxfattribs={"layer": "A-STAIR"},
    )
    msp.add_text("STAIR", height=150, dxfattribs={"layer": "A-STAIR"}).set_placement((1500, 1700))
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


def test_dxf_upload_extracts_grids_columns_openings_and_transfers(sb_client):
    client = sb_client
    pid = "SB-CAD-01"
    p = _shell_project(pid, with_columns=False)
    assert client.post("/api/projects", json=p.model_dump()).status_code == 200

    res = client.post(
        f"/api/projects/{pid}/pdf-import/upload-cad",
        files={"file": ("arch_plan.dxf", _build_architectural_dxf(), "application/dxf")},
    )
    assert res.status_code == 200, res.text
    doc = res.json()
    assert doc["source_format"] == "CAD"
    assert doc["page_count"] == 1
    assert all(c["source"] == "cad" for c in doc["column_candidates"])
    assert len(doc["column_candidates"]) == 4
    assert {g["label"] for g in doc["grid_candidates"] if g["axis"] == "x"} == {"A", "B"}
    assert {g["label"] for g in doc["grid_candidates"] if g["axis"] == "y"} == {"1", "2"}
    assert len(doc["openings"]) == 1
    cal = doc["calibrations"]["1"]
    assert cal["confirmed"] is True
    for col in doc["column_candidates"]:
        assert col["width_mm"] == pytest.approx(400.0, abs=1.0)
    xs = sorted({round(c["x_m"], 2) for c in doc["column_candidates"]})
    assert xs == [0.0, 4.0]

    # Existing Module 02 canvas image endpoint works on the CAD preview
    img = client.get(f"/api/projects/{pid}/pdf-imports/{doc['doc_id']}/pages/1/image")
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"

    # Transfer into project (existing pipeline) and generate Phase 3 geometry
    region_id = doc["regions"][0]["region_id"]
    tx = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc['doc_id']}/transfer",
        json={"page_number": 1, "region_id": region_id, "merge_mode": "replace_geometry"},
    )
    assert tx.status_code == 200, tx.text
    assert tx.json()["transferred_columns"] == 4
    assert tx.json()["transferred_openings"] == 1
    gen = client.post(f"/api/projects/{pid}/geometry/generate", json={"default_support_type": "FIXED"})
    assert gen.status_code == 200, gen.text
    assert len(gen.json()["nodes"]) > 0


def test_dwg_upload_without_converter_returns_503(sb_client, monkeypatch: pytest.MonkeyPatch):
    from backend.app.cad import dwg_converter as dwg_mod

    client = sb_client
    pid = "SB-DWG-01"
    assert client.post("/api/projects", json=_shell_project(pid).model_dump()).status_code == 200
    monkeypatch.setattr(
        dwg_mod,
        "detect_dwg_converter",
        lambda: {"available": False, "backend": None, "executable_path": None, "install_hint": dwg_mod.INSTALL_HINT},
    )
    res = client.post(
        f"/api/projects/{pid}/pdf-import/upload-cad",
        files={"file": ("plan.dwg", b"AC1027fakebinary", "application/acad")},
    )
    assert res.status_code == 503
    assert "ODA File Converter" in res.json()["detail"]


def _run_to_loads(client: TestClient, pid: str, seismic: dict | None = None) -> dict:
    assert client.post("/api/projects", json=_shell_project(pid).model_dump()).status_code == 200
    assert (
        client.post(
            f"/api/projects/{pid}/geometry/generate",
            json={"default_support_type": "FIXED", "include_base_tie_beams": True},
        ).status_code
        == 200
    )
    assert client.post(f"/api/projects/{pid}/sizing/auto-size").status_code == 200
    body = {"seismic": seismic} if seismic else None
    res = client.post(f"/api/projects/{pid}/loads/generate", json=body) if body else client.post(
        f"/api/projects/{pid}/loads/generate"
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_rayleigh_method_b_capped_and_pdelta_std_passes_lint(sb_client):
    client = sb_client
    pid = "SB-RAY-01"
    lg = _run_to_loads(
        client,
        pid,
        seismic={"use_method_b_period": True, "use_pdelta": True, "pdelta_iterations": 3},
    )
    ss = lg["seismic_summary"]
    seis = lg["seismic"]
    h_n = ss["building_height_m"]
    ta = 0.0731 * (h_n ** 0.75)
    assert seis["computed_rayleigh_tx_sec"] is not None and seis["computed_rayleigh_tz_sec"] is not None
    assert 0.0 < seis["computed_rayleigh_tx_sec"] <= 1.3 * ta + 1e-3
    assert 0.0 < seis["computed_rayleigh_tz_sec"] <= 1.3 * ta + 1e-3
    assert ss["period_t_sec"] == pytest.approx(
        max(seis["computed_rayleigh_tx_sec"], seis["computed_rayleigh_tz_sec"]), abs=1e-3
    )

    std_text = client.post(f"/api/projects/{pid}/staad-preview").json()["std_preview"]
    assert "PDELTA 3 ANALYSIS PRINT STATICS CHECK" in std_text
    assert "PERFORM ANALYSIS PRINT STATICS CHECK" not in std_text
    assert lint_staad_text(std_text) == []

    # Default (flags off) still emits PERFORM ANALYSIS and leaves Rayleigh periods empty
    pid2 = "SB-RAY-02"
    lg2 = _run_to_loads(client, pid2)
    assert lg2["seismic"]["computed_rayleigh_tx_sec"] is None
    std2 = client.post(f"/api/projects/{pid2}/staad-preview").json()["std_preview"]
    assert "PERFORM ANALYSIS PRINT STATICS CHECK" in std2
    assert lint_staad_text(std2) == []

    # Phase 6 still runs with P-Delta enabled
    assert client.post(f"/api/projects/{pid}/design/run").status_code == 200


def test_auto_resolve_converges_undersized_frame_and_updates_self_weight(sb_client):
    client = sb_client
    pid = "SB-AUTO-01"
    _run_to_loads(client, pid)

    proj = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    for sec in proj.member_sizing.sections:
        if sec.category == "column":
            tiny = sec.model_copy(update={"width_mm": 200.0, "depth_mm": 200.0})
            assert client.post(f"/api/projects/{pid}/sizing/sections", json=tiny.model_dump()).status_code == 200
    assert client.post(f"/api/projects/{pid}/loads/generate").status_code == 200
    before = client.post(f"/api/projects/{pid}/design/run").json()
    w_before = client.get(f"/api/projects/{pid}").json()["load_generation"]["seismic_summary"]["total_seismic_weight_kn"]
    fails_before = (
        sum(1 for b in before["beam_designs"] if b["status"] == "FAIL")
        + sum(1 for c in before["column_designs"] if c["status"] == "FAIL")
        + sum(1 for f in before["footing_designs"] if f["status"] == "FAIL")
        + sum(1 for d in before["story_drifts"] if d["status"] == "FAIL")
    )
    assert fails_before > 0

    res = client.post(f"/api/projects/{pid}/design/auto-resolve", json={"max_iterations": 15})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["converged"] is True
    assert body["iterations_run"] >= 2
    assert body["history"][-1]["fail_beams"] == 0
    assert body["history"][-1]["fail_columns"] == 0
    assert body["history"][-1]["fail_drifts"] == 0
    ad = body["analysis_design"]
    assert all(x["status"] == "PASS" for x in ad["beam_designs"] + ad["column_designs"] + ad["footing_designs"])
    assert all(d["status"] == "PASS" for d in ad["story_drifts"])

    after = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    col_secs = [s for s in after.member_sizing.sections if s.category == "column"]
    assert all(s.width_mm > 200.0 for s in col_secs)
    # Seismic weight refreshed with heavier columns
    assert after.load_generation.seismic_summary.total_seismic_weight_kn > w_before


def test_auto_resolve_respects_locked_sections(sb_client):
    client = sb_client
    pid = "SB-AUTO-02"
    _run_to_loads(client, pid)
    proj = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    col_secs = [s for s in proj.member_sizing.sections if s.category == "column"]
    for sec in col_secs:
        tiny = sec.model_copy(update={"width_mm": 200.0, "depth_mm": 200.0})
        client.post(f"/api/projects/{pid}/sizing/sections", json=tiny.model_dump())
    col_mids = [
        m.member_id for m in proj.structural_model.members if m.member_type == "column"
    ]
    for sec in col_secs:
        mids = [
            a.member_id
            for a in proj.member_sizing.member_assignments
            if a.section_id == sec.section_id
        ]
        assert (
            client.post(
                f"/api/projects/{pid}/sizing/assign",
                json={"member_ids": mids, "section_id": sec.section_id, "beta_angle_deg": 0.0},
            ).status_code
            == 200
        )
    # Mark all assignments manual_override so columns are locked
    p2 = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    for a in p2.member_sizing.member_assignments:
        if a.member_id in col_mids:
            a.manual_override = True
    storage_mod.save_project(p2)
    assert client.post(f"/api/projects/{pid}/loads/generate").status_code == 200

    res = client.post(f"/api/projects/{pid}/design/auto-resolve", json={"max_iterations": 3})
    assert res.status_code == 200, res.text
    after = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    for s in after.member_sizing.sections:
        if s.category == "column":
            assert s.width_mm == pytest.approx(200.0)
