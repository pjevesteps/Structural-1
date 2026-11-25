import csv
import io
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.design.solver import solve_frame_dsm
from backend.app.main import app
from backend.app.models import ProjectState


# ---------------------------------------------------------------------------
# STAAD.Pro .std Grammar & Referential Integrity Linter
# ---------------------------------------------------------------------------

def lint_staad_text(
    std_text: str,
    require_ref_load_before_diaphragm: bool = True,
) -> list[str]:
    """
    Validate an exported STAAD.Pro .std text against core STAAD grammar, block ordering,
    line-length, mass-modeling (DEFINE REFERENCE LOADS before FLOOR DIAPHRAGM, no supported
    nodes in diaphragm JOI lists, no double-counted UBC mass), and joint/member referential
    integrity rules.

    Returns a list of human-readable error strings (empty list == valid).
    """
    errors: list[str] = []

    if "\n" not in std_text or "\\n" in std_text:
        errors.append("STAAD text must use real newlines and must not contain escaped '\\n' sequences.")

    lines = std_text.splitlines()

    # 1. Line length <= 79 chars for non-comment data lines
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("*"):
            continue
        if len(raw) > 79:
            errors.append(f"Line {lineno} exceeds 79 characters ({len(raw)} chars): {raw[:60]}...")

    # 2. Required block order
    ordered_markers = [
        "STAAD SPACE",
        "JOINT COORDINATES",
        "MEMBER INCIDENCES",
        "DEFINE MATERIAL START",
        "MEMBER PROPERTY",
        "CONSTANTS",
        "SUPPORTS",
        "LOAD 1",
        "LOAD 9",
        "LOAD COMB",
        "PERFORM ANALYSIS",
        "START CONCRETE DESIGN",
        "FINISH",
    ]
    positions: dict[str, int] = {}
    for marker in ordered_markers:
        idx = std_text.find(marker)
        if idx == -1:
            errors.append(f"Missing required STAAD block/keyword: '{marker}'.")
        else:
            positions[marker] = idx

    # Verify monotonic order for all present markers (note: PERFORM ANALYSIS may also appear
    # inside LOAD 1..4 for UBC auto, so check final PERFORM ANALYSIS PRINT STATICS CHECK too)
    for prev_m, next_m in zip(ordered_markers, ordered_markers[1:]):
        if prev_m in positions and next_m in positions:
            if prev_m == "LOAD COMB" and next_m == "PERFORM ANALYSIS":
                final_pa = std_text.rfind("PERFORM ANALYSIS")
                if positions[prev_m] > final_pa:
                    errors.append(
                        f"Block ordering violation: '{prev_m}' must precede final 'PERFORM ANALYSIS'."
                    )
            elif prev_m == "PERFORM ANALYSIS" and next_m == "START CONCRETE DESIGN":
                final_pa = std_text.rfind("PERFORM ANALYSIS")
                if final_pa > positions[next_m]:
                    errors.append(
                        f"Block ordering violation: final 'PERFORM ANALYSIS' must precede '{next_m}'."
                    )
            elif positions[prev_m] > positions[next_m]:
                errors.append(
                    f"Block ordering violation: '{prev_m}' (pos {positions[prev_m]}) "
                    f"appears after '{next_m}' (pos {positions[next_m]})."
                )

    # Mass-modeling ordering & deduplication checks (Reference Loads before Diaphragm & UBC)
    if require_ref_load_before_diaphragm and "FLOOR DIAPHRAGM" in std_text:
        ref_start = std_text.find("DEFINE REFERENCE LOADS")
        ref_end = std_text.find("END DEFINE REFERENCE LOADS")
        dia_pos = std_text.find("FLOOR DIAPHRAGM")
        if ref_start == -1 or ref_end == -1:
            errors.append(
                "FLOOR DIAPHRAGM is present without DEFINE REFERENCE LOADS / END DEFINE REFERENCE LOADS."
            )
        elif not (ref_start < ref_end < dia_pos):
            errors.append(
                "DEFINE REFERENCE LOADS / END DEFINE REFERENCE LOADS must appear strictly before FLOOR DIAPHRAGM."
            )
        if "DEFINE UBC LOAD" in std_text and "LOAD 1" in std_text:
            ubc_start = std_text.find("DEFINE UBC LOAD")
            load1_pos = std_text.find("LOAD 1")
            if ubc_start < load1_pos:
                ubc_block = std_text[ubc_start:load1_pos]
                if "SELFWEIGHT" in ubc_block or "MEMBER WEIGHT" in ubc_block:
                    errors.append(
                        "DEFINE UBC LOAD duplicates SELFWEIGHT or MEMBER WEIGHT while DEFINE REFERENCE LOADS is active."
                    )
        if "LOAD 9" in std_text and "LOAD 10" in std_text:
            l9_block = std_text[std_text.find("LOAD 9") : std_text.find("LOAD 10")]
            if "REFERENCE LOAD" not in l9_block or "R1 1.0" not in l9_block:
                errors.append("LOAD 9 should reference R1 1.0 when DEFINE REFERENCE LOADS is active.")

    # 3. Parse JOINT COORDINATES, MEMBER INCIDENCES, SUPPORTS, MEMBER PROPERTY, FLOOR DIAPHRAGM
    known_joints: set[int] = set()
    known_members: set[int] = set()
    incidence_refs: list[tuple[int, int, int]] = []
    support_refs: list[int] = []
    prop_refs: list[int] = []
    diaphragm_joint_refs: list[int] = []

    section: str | None = None
    top_headers = (
        "JOINT COORDINATES",
        "MEMBER INCIDENCES",
        "START GROUP DEFINITION",
        "END GROUP DEFINITION",
        "DEFINE MATERIAL START",
        "END DEFINE MATERIAL",
        "MEMBER PROPERTY",
        "CONSTANTS",
        "MEMBER CRACKED",
        "MEMBER RELEASE",
        "SUPPORTS",
        "DEFINE REFERENCE LOADS",
        "END DEFINE REFERENCE LOADS",
        "FLOOR DIAPHRAGM",
        "DEFINE UBC LOAD",
        "LOAD ",
        "LOAD COMB",
        "PERFORM ANALYSIS",
        "START CONCRETE DESIGN",
        "FINISH",
    )

    def _is_header(s: str) -> bool:
        up = s.upper()
        return any(up.startswith(h) for h in top_headers)

    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("*"):
            continue
        up = s.upper()
        if up.startswith("JOINT COORDINATES"):
            section = "JOINTS"
            continue
        if up.startswith("MEMBER INCIDENCES"):
            section = "MEMBERS"
            continue
        if up.startswith("MEMBER PROPERTY"):
            section = "PROP"
            continue
        if up.startswith("SUPPORTS"):
            section = "SUPPORTS"
            continue
        if up.startswith("FLOOR DIAPHRAGM"):
            section = "DIAPHRAGM"
            continue
        if _is_header(s):
            section = None
            continue

        if section == "JOINTS":
            for stmt in s.split(";"):
                toks = stmt.strip().split()
                if len(toks) >= 4 and toks[0].isdigit():
                    known_joints.add(int(toks[0]))
        elif section == "MEMBERS":
            for stmt in s.split(";"):
                toks = stmt.strip().split()
                if len(toks) >= 3 and toks[0].isdigit() and toks[1].isdigit() and toks[2].isdigit():
                    mid, na, nb = int(toks[0]), int(toks[1]), int(toks[2])
                    known_members.add(mid)
                    incidence_refs.append((mid, na, nb))
        elif section == "SUPPORTS":
            # Strip trailing FIXED/PINNED and continuation '-'
            cleaned = re.sub(r"\b(FIXED|PINNED)\b", "", up).replace("-", " ")
            for tok in cleaned.split():
                if tok.isdigit():
                    support_refs.append(int(tok))
        elif section == "PROP":
            # Extract member IDs appearing before PRIS (or on '-' continuation lines)
            before_pris = up.split("PRIS", 1)[0].replace("-", " ")
            for tok in before_pris.split():
                if tok.isdigit():
                    prop_refs.append(int(tok))
        elif section == "DIAPHRAGM":
            if "JOI" in up:
                after_joi = up.split("JOI", 1)[1].replace("-", " ")
                for tok in after_joi.split():
                    if tok.isdigit():
                        diaphragm_joint_refs.append(int(tok))
            elif up[0].isdigit():
                for tok in up.replace("-", " ").split():
                    if tok.isdigit():
                        diaphragm_joint_refs.append(int(tok))

    if not known_joints:
        errors.append("No joints parsed from JOINT COORDINATES.")
    if not known_members:
        errors.append("No members parsed from MEMBER INCIDENCES.")

    for mid, na, nb in incidence_refs:
        if na not in known_joints or nb not in known_joints:
            errors.append(
                f"Member {mid} references undefined joint(s): ({na}, {nb})."
            )
    support_set = set(support_refs)
    for nid in support_refs:
        if nid not in known_joints:
            errors.append(f"SUPPORTS references undefined joint {nid}.")
    for mid in prop_refs:
        if mid not in known_members:
            errors.append(f"MEMBER PROPERTY references undefined member {mid}.")
    for nid in diaphragm_joint_refs:
        if nid not in known_joints:
            errors.append(f"FLOOR DIAPHRAGM references undefined joint {nid}.")
        elif nid in support_set:
            errors.append(
                f"FLOOR DIAPHRAGM JOI list includes supported node {nid} (must exclude supported nodes)."
            )

    return errors


# ---------------------------------------------------------------------------
# Isolated Storage & Synthetic Multi-Page Architectural Vector PDF Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect project JSON storage and PDF imports strictly into tmp_path."""
    projects_dir = tmp_path / "data" / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "DATA_DIR", projects_dir)
    client = TestClient(app)
    yield client, tmp_path / "data"

    # Clean up any temporary .std export file written during /staad/download so workspace stays clean
    exports_dir = ROOT / "data" / "exports"
    if exports_dir.exists():
        for leftover in exports_dir.glob("E2E-PIPE-*"):
            leftover.unlink(missing_ok=True)


def _build_e2e_architectural_pdf(pdf_path: Path) -> dict:
    """
    Generate a synthetic 3-page architectural vector PDF using PyMuPDF:
      - Page 1 (Elevation): 4 structural floor-to-floor levels (0.00, 2.80, 5.80, 8.60 m)
        plus non-floor distractors (NATURAL GRADE LINE, SECOND FLOOR CEILING LINE, FIREWALL A).
      - Page 2 (Dual Plan — Basement Left / 1F Right, 25 pt/m):
        2x2 bay grid (A, B, C at 0, 4, 8 m; 1, 2, 3 at 0, 4, 8 m),
        4 gray-shaded 0.40x0.40 m columns on partial Basement,
        9 gray-shaded 0.40x0.40 m columns on 1F,
        plus text distractors ("4.00", "8.00", "PARKING", "STORAGE").
      - Page 3 (2F Plan, 25 pt/m):
        Matching 9 gray-shaded columns + stairwell opening box inside bay (4..8, 4..8).

    Returns geometry metadata (scale pt/m, anchors, distractor text boxes in PDF points).
    """
    doc = pymupdf.open()
    pt_per_m = 25.0  # 8.0 m = 200 pt; 0.40 m column = 10 pt square
    col_half_pt = 0.40 * pt_per_m * 0.5  # 5.0 pt

    # --- Page 1: Elevation with floor-to-floor levels + distractors ---
    p1 = doc.new_page(width=800, height=560)
    p1.insert_text(pymupdf.Point(300, 510), "FRONT ELEVATION", fontsize=13)
    sh1 = p1.new_shape()
    elev_rows = [
        (430.0, "BASEMENT FLOOR FINISHED LINE", "0.00 m"),
        (378.0, "NATURAL GRADE LINE", "2.60 m"),                # distractor
        (360.0, "FIRST FLOOR FINISHED FLOOR LINE", "2.80 m"),
        (296.0, "SECOND FLOOR CEILING LINE", "5.40 m"),         # distractor
        (280.0, "SECOND FLOOR FINISHED FLOOR LINE", "5.80 m"),
        (200.0, "ROOF DECK FINISHED FLOOR LINE", "8.60 m"),
        (145.0, "FIREWALL A", "9.80 m"),                        # distractor
    ]
    for y_pt, label, elev_txt in elev_rows:
        sh1.draw_line(pymupdf.Point(150.0, y_pt), pymupdf.Point(650.0, y_pt))
        sh1.finish(color=(0.2, 0.2, 0.2), width=0.9)
        p1.insert_text(pymupdf.Point(24.0, y_pt - 4.0), label, fontsize=8.5)
        p1.insert_text(pymupdf.Point(662.0, y_pt - 4.0), elev_txt, fontsize=8.5)
    sh1.commit()

    # --- Page 2: Dual Plan (Basement Left, 1F Right) ---
    p2 = doc.new_page(width=920, height=520)
    sh2 = p2.new_shape()
    # Outer drawing border & center viewport divider at x=460 (50% of 920 pt)
    sh2.draw_rect(pymupdf.Rect(20.0, 20.0, 900.0, 450.0))
    sh2.finish(color=(0.15, 0.15, 0.15), width=1.0)
    sh2.draw_line(pymupdf.Point(460.0, 20.0), pymupdf.Point(460.0, 450.0))
    sh2.finish(color=(0.15, 0.15, 0.15), width=1.0)

    bsmt_anchor_pt = (110.0, 360.0)   # Grid A-1 on Left half
    f1_anchor_pt = (560.0, 360.0)     # Grid A-1 on Right half

    # Partial Basement: 4 columns at (A/B x 1/2) -> (0,0), (4,0), (0,4), (4,4) m
    bsmt_cols_m = [(0.0, 0.0), (4.0, 0.0), (0.0, 4.0), (4.0, 4.0)]
    for xm, ym in bsmt_cols_m:
        cx = bsmt_anchor_pt[0] + xm * pt_per_m
        cy = bsmt_anchor_pt[1] - ym * pt_per_m
        sh2.draw_rect(pymupdf.Rect(cx - col_half_pt, cy - col_half_pt, cx + col_half_pt, cy + col_half_pt))
        sh2.finish(color=(0.1, 0.1, 0.1), fill=(0.55, 0.55, 0.55), width=0.6)

    # Full 1F: 9 columns at (A/B/C x 1/2/3)
    full_cols_m = [(xm, ym) for xm in (0.0, 4.0, 8.0) for ym in (0.0, 4.0, 8.0)]
    for xm, ym in full_cols_m:
        cx = f1_anchor_pt[0] + xm * pt_per_m
        cy = f1_anchor_pt[1] - ym * pt_per_m
        sh2.draw_rect(pymupdf.Rect(cx - col_half_pt, cy - col_half_pt, cx + col_half_pt, cy + col_half_pt))
        sh2.finish(color=(0.1, 0.1, 0.1), fill=(0.55, 0.55, 0.55), width=0.6)
    sh2.commit()

    # Titles & text distractors on Page 2
    p2.insert_text(pymupdf.Point(145.0, 430.0), "BASEMENT FLOOR PLAN", fontsize=11)
    p2.insert_text(pymupdf.Point(600.0, 430.0), "FIRST FLOOR PLAN", fontsize=11)
    distractor_specs_p2 = [
        (145.0, 362.0, "4.00"),
        (245.0, 362.0, "8.00"),
        (140.0, 315.0, "STORAGE"),
        (240.0, 225.0, "PARKING"),
        (595.0, 362.0, "4.00"),
        (695.0, 362.0, "8.00"),
        (590.0, 315.0, "PARKING"),
        (690.0, 225.0, "STORAGE"),
    ]
    for tx, ty, txt in distractor_specs_p2:
        p2.insert_text(pymupdf.Point(tx, ty), txt, fontsize=10)

    # Capture actual word bounding boxes on Page 2 for strict non-overlap verification
    p2_distractor_words = [
        w[:4]
        for w in p2.get_text("words")
        if w[4].upper() in {"4.00", "8.00", "PARKING", "STORAGE"}
    ]

    # --- Page 3: 2F Plan (9 columns + stairwell opening visual) ---
    p3 = doc.new_page(width=760, height=520)
    sh3 = p3.new_shape()
    f2_anchor_pt = (200.0, 360.0)
    for xm, ym in full_cols_m:
        cx = f2_anchor_pt[0] + xm * pt_per_m
        cy = f2_anchor_pt[1] - ym * pt_per_m
        sh3.draw_rect(pymupdf.Rect(cx - col_half_pt, cy - col_half_pt, cx + col_half_pt, cy + col_half_pt))
        sh3.finish(color=(0.1, 0.1, 0.1), fill=(0.55, 0.55, 0.55), width=0.6)

    # Hollow stairwell outline inside bay (4..8, 4..8) -> (5..7, 5..7) m (must NOT be flagged as column)
    op_x0 = f2_anchor_pt[0] + 5.0 * pt_per_m
    op_x1 = f2_anchor_pt[0] + 7.0 * pt_per_m
    op_y0 = f2_anchor_pt[1] - 7.0 * pt_per_m
    op_y1 = f2_anchor_pt[1] - 5.0 * pt_per_m
    sh3.draw_rect(pymupdf.Rect(op_x0, op_y0, op_x1, op_y1))
    sh3.finish(color=(0.2, 0.2, 0.2), width=0.8)
    sh3.commit()
    p3.insert_text(pymupdf.Point(250.0, 430.0), "SECOND FLOOR PLAN", fontsize=11)
    p3.insert_text(pymupdf.Point(op_x0 + 6.0, op_y0 + 28.0), "STAIR", fontsize=9)

    doc.save(pdf_path)
    doc.close()

    return {
        "pt_per_m": pt_per_m,
        "bsmt_anchor_pt": bsmt_anchor_pt,
        "f1_anchor_pt": f1_anchor_pt,
        "f2_anchor_pt": f2_anchor_pt,
        "p2_distractor_words": p2_distractor_words,
    }


# ---------------------------------------------------------------------------
# Full End-to-End Pipeline Test: Phase 1 -> Phase 6
# ---------------------------------------------------------------------------

def test_full_pipeline_phase1_to_phase6(isolated_env, tmp_path: Path):
    """
    Exercise the complete Structural AI Office workflow sequentially through real FastAPI
    endpoints on a single persisted project in isolated tmp_path storage:
      Phase 1 (Project Init)
      -> Phase 2 (PDF Upload, Scale, Floor-to-Floor Levels, Master Grid Stamp,
                  Shaded Column Scan, Stairwell Opening, Multi-Floor Transfer)
      -> Phase 3 (3D Frame Generation, Manual Diagonal Beam Persistence across Regen, Approval)
      -> Phase 4 (Auto-Size Members, Beta 90 Bulk Override, Approval)
      -> Phase 5 (NSCP 2015 Gravity/Seismic/Wind/Combos, Approval, .std Download + Grammar Linter)
      -> Phase 6 (3D DSM Analysis, Drift/RC/Footing Design, Static Equilibrium, CSV Schedule Export)
      -> Disk JSON Round-Trip Validation (ProjectState, extra="forbid")
    """
    client, data_root = isolated_env
    pid = "E2E-PIPE-01"

    # =========================================================================
    # PHASE 1: Create Project Shell via API
    # =========================================================================
    init_payload = {
        "project_info": {
            "project_id": pid,
            "project_name": "E2E Baguio 3-Story Residential Frame",
            "location": "Baguio City, Benguet",
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
    create_res = client.post("/api/projects", json=init_payload)
    assert create_res.status_code == 200, create_res.text

    # =========================================================================
    # PHASE 2: PDF Upload, Levels, Calibration, Master Grid, Columns, Openings, Transfer
    # =========================================================================
    pdf_path = tmp_path / "e2e_arch_plans.pdf"
    meta = _build_e2e_architectural_pdf(pdf_path)
    pt_per_m = meta["pt_per_m"]

    with pdf_path.open("rb") as fh:
        up_res = client.post(
            f"/api/projects/{pid}/pdf-imports",
            files={"file": ("e2e_arch_plans.pdf", fh.read(), "application/pdf")},
        )
    assert up_res.status_code == 200, up_res.text
    doc_state = up_res.json()
    doc_id = doc_state["doc_id"]
    assert len(doc_state["pages"]) == 3

    # 2a. Extract floor-to-floor levels on Page 1 (Elevation) & assert distractors excluded
    ext_lvl = client.post(f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/1/extract-levels")
    assert ext_lvl.status_code == 200, ext_lvl.text
    detected_levels = sorted(
        ext_lvl.json()["document"]["detected_levels"],
        key=lambda lv: lv["elevation_m"],
    )
    assert len(detected_levels) == 4
    assert [lv["elevation_m"] for lv in detected_levels] == [0.0, 2.8, 5.8, 8.6]
    lvl_names_upper = [lv["name"].upper() for lv in detected_levels]
    assert not any(
        bad in nm for nm in lvl_names_upper for bad in ("NATURAL GRADE", "FIREWALL", "CEILING")
    )

    # Approve all 4 extracted levels and transfer to ProjectState.levels
    for lv in detected_levels:
        upd = client.patch(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/levels/{lv['level_id']}",
            json={"review_status": "approved", "engineer_approved": True},
        )
        assert upd.status_code == 200, upd.text

    tx_lvls = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/transfer-levels",
        json={"replace_existing": True},
    )
    assert tx_lvls.status_code == 200, tx_lvls.text
    proj_after_lvls = client.get(f"/api/projects/{pid}").json()
    sorted_proj_lvls = sorted(proj_after_lvls["levels"], key=lambda lv: lv["elevation_m"])
    assert len(sorted_proj_lvls) == 4
    lvl_bsmt_id = sorted_proj_lvls[0]["id"]  # 0.00 m
    lvl_1f_id = sorted_proj_lvls[1]["id"]    # 2.80 m
    lvl_2f_id = sorted_proj_lvls[2]["id"]    # 5.80 m

    # 2b. Calibrate Page 2 and Page 3 at 25 pt/m (200 pt = 8.0 m, confirmed)
    p2_info = doc_state["pages"][1]
    p3_info = doc_state["pages"][2]
    sx2 = p2_info["width_px"] / p2_info["width_pt"]
    sy2 = p2_info["height_px"] / p2_info["height_pt"]
    sx3 = p3_info["width_px"] / p3_info["width_pt"]
    sy3 = p3_info["height_px"] / p3_info["height_pt"]

    f1_ax, f1_ay = meta["f1_anchor_pt"]
    bsmt_ax, bsmt_ay = meta["bsmt_anchor_pt"]
    f2_ax, f2_ay = meta["f2_anchor_pt"]

    for pg_num, ax, ay, sx, sy in (
        (2, f1_ax, f1_ay, sx2, sy2),
        (3, f2_ax, f2_ay, sx3, sy3),
    ):
        cal_res = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/{pg_num}/calibrate",
            json={
                "p1": {"x_px": ax * sx, "y_px": ay * sy},
                "p2": {"x_px": (ax + 8.0 * pt_per_m) * sx, "y_px": ay * sy},
                "known_distance": 8.0,
                "unit": "m",
                "confirmed": True,
            },
        )
        assert cal_res.status_code == 200, cal_res.text

    # 2c. Configure Basement (Left) & 1F (Right) regions on Page 2, and 2F region on Page 3
    cur_doc = client.get(f"/api/projects/{pid}/pdf-imports/{doc_id}").json()
    p2_regions = sorted(
        [r for r in cur_doc["regions"] if r["page_number"] == 2],
        key=lambda r: r["x_px"],
    )
    if len(p2_regions) >= 2:
        bsmt_reg_id = p2_regions[0]["region_id"]
        f1_reg_id = p2_regions[1]["region_id"]
        assert (
            client.patch(
                f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{bsmt_reg_id}",
                json={
                    "name": "Basement Plan",
                    "floor_level": lvl_bsmt_id,
                    "origin_px": {"x_px": bsmt_ax * sx2, "y_px": bsmt_ay * sy2},
                },
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{f1_reg_id}",
                json={
                    "name": "First Floor Plan",
                    "floor_level": lvl_1f_id,
                    "origin_px": {"x_px": f1_ax * sx2, "y_px": f1_ay * sy2},
                },
            ).status_code
            == 200
        )
    else:
        r_bsmt = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/regions",
            json={
                "page_number": 2,
                "name": "Basement Plan",
                "floor_level": lvl_bsmt_id,
                "x_px": 25.0 * sx2,
                "y_px": 25.0 * sy2,
                "width_px": 430.0 * sx2,
                "height_px": 420.0 * sy2,
                "origin_px": {"x_px": bsmt_ax * sx2, "y_px": bsmt_ay * sy2},
            },
        )
        assert r_bsmt.status_code == 200, r_bsmt.text
        bsmt_reg_id = r_bsmt.json()["regions"][-1]["region_id"]
        r_1f = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/regions",
            json={
                "page_number": 2,
                "name": "First Floor Plan",
                "floor_level": lvl_1f_id,
                "x_px": 465.0 * sx2,
                "y_px": 25.0 * sy2,
                "width_px": 430.0 * sx2,
                "height_px": 420.0 * sy2,
                "origin_px": {"x_px": f1_ax * sx2, "y_px": f1_ay * sy2},
            },
        )
        assert r_1f.status_code == 200, r_1f.text
        f1_reg_id = r_1f.json()["regions"][-1]["region_id"]

    p3_regions = [r for r in cur_doc["regions"] if r["page_number"] == 3]
    if p3_regions:
        f2_reg_id = p3_regions[0]["region_id"]
        assert (
            client.patch(
                f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{f2_reg_id}",
                json={
                    "name": "Second Floor Plan",
                    "floor_level": lvl_2f_id,
                    "origin_px": {"x_px": f2_ax * sx3, "y_px": f2_ay * sy3},
                },
            ).status_code
            == 200
        )
    else:
        r_2f = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/regions",
            json={
                "page_number": 3,
                "name": "Second Floor Plan",
                "floor_level": lvl_2f_id,
                "x_px": 40.0 * sx3,
                "y_px": 40.0 * sy3,
                "width_px": 680.0 * sx3,
                "height_px": 420.0 * sy3,
                "origin_px": {"x_px": f2_ax * sx3, "y_px": f2_ay * sy3},
            },
        )
        assert r_2f.status_code == 200, r_2f.text
        f2_reg_id = r_2f.json()["regions"][-1]["region_id"]

    # 2d. Trace 1F grids (A, B, C at 0, 4, 8 m; 1, 2, 3 at 0, 4, 8 m), lock Master Grid, stamp onto Basement & 2F
    for label, xm in (("A", 0.0), ("B", 4.0), ("C", 8.0)):
        gx_pt = f1_ax + xm * pt_per_m
        gr = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/elements/manual-grid",
            json={
                "page_number": 2,
                "region_id": f1_reg_id,
                "element_type": "grid_x",
                "label": label,
                "p1": {"x_px": gx_pt * sx2, "y_px": 90.0 * sy2},
                "p2": {"x_px": gx_pt * sx2, "y_px": 400.0 * sy2},
            },
        )
        assert gr.status_code == 200, gr.text

    for label, ym in (("1", 0.0), ("2", 4.0), ("3", 8.0)):
        gy_pt = f1_ay - ym * pt_per_m
        gr = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/elements/manual-grid",
            json={
                "page_number": 2,
                "region_id": f1_reg_id,
                "element_type": "grid_y",
                "label": label,
                "p1": {"x_px": 500.0 * sx2, "y_px": gy_pt * sy2},
                "p2": {"x_px": 840.0 * sx2, "y_px": gy_pt * sy2},
            },
        )
        assert gr.status_code == 200, gr.text

    lock_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/master-grid",
        json={"source_region_id": f1_reg_id},
    )
    assert lock_res.status_code == 200, lock_res.text

    for target_reg, ax, ay, sx, sy in (
        (bsmt_reg_id, bsmt_ax, bsmt_ay, sx2, sy2),
        (f2_reg_id, f2_ax, f2_ay, sx3, sy3),
    ):
        st_res = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/regions/{target_reg}/stamp-master-grid",
            json={"anchor_px": {"x_px": ax * sx, "y_px": ay * sy}, "clear_existing_grids": True},
        )
        assert st_res.status_code == 200, st_res.text

    # 2e. Run Scan Columns Only across Basement (4), 1F (9), and 2F (9) & verify 0 text overlaps
    scan_opts = {
        "detect_grids": False,
        "detect_columns": True,
        "column_fill_mode": "shaded_or_hatched",
        "snap_columns_to_grids": True,
    }
    for pg_num, reg_id, expected_cols in (
        (2, bsmt_reg_id, 4),
        (2, f1_reg_id, 9),
        (3, f2_reg_id, 9),
    ):
        ext = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/pages/{pg_num}/extract",
            json={"method": "auto", "region_id": reg_id, "options": scan_opts},
        )
        assert ext.status_code == 200, ext.text
        reg_cols = [
            c for c in ext.json()["document"]["column_candidates"] if c["region_id"] == reg_id
        ]
        assert len(reg_cols) == expected_cols, (
            f"Expected {expected_cols} columns in region {reg_id}, got {len(reg_cols)}"
        )

    # Assert zero detected columns on Page 2 overlap any text distractor ("4.00", "8.00", "PARKING", "STORAGE")
    doc_after_scan = client.get(f"/api/projects/{pid}/pdf-imports/{doc_id}").json()
    p2_cols = [c for c in doc_after_scan["column_candidates"] if c["page_number"] == 2]
    assert len(p2_cols) == 13  # 4 Basement + 9 1F
    for col in p2_cols:
        cx_pt = col["x_px"] / sx2
        cy_pt = col["y_px"] / sy2
        for wx0, wy0, wx1, wy1 in meta["p2_distractor_words"]:
            assert not (wx0 - 2.0 <= cx_pt <= wx1 + 2.0 and wy0 - 2.0 <= cy_pt <= wy1 + 2.0), (
                f"Detected column at ({cx_pt:.1f}, {cy_pt:.1f}) overlapped distractor word box!"
            )

    # Bulk-accept all detected columns
    all_col_ids = [c["id"] for c in doc_after_scan["column_candidates"]]
    assert (
        client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/candidates/bulk-review",
            json={"candidate_ids": all_col_ids, "status": "approved"},
        ).status_code
        == 200
    )

    # 2f. Trace stairwell opening polygon on 2F inside bay (4..8, 4..8): (5,5)-(7,5)-(7,7)-(5,7) -> 4.0 sqm
    op_poly_px = [
        {"x_px": (f2_ax + xm * pt_per_m) * sx3, "y_px": (f2_ay - ym * pt_per_m) * sy3}
        for xm, ym in ((5.0, 5.0), (7.0, 5.0), (7.0, 7.0), (5.0, 7.0))
    ]
    op_res = client.post(
        f"/api/projects/{pid}/pdf-imports/{doc_id}/openings",
        json={
            "page_number": 3,
            "region_id": f2_reg_id,
            "floor_level": lvl_2f_id,
            "category": "stairwell",
            "label": "STAIR-2F",
            "polygon_px": op_poly_px,
        },
    )
    assert op_res.status_code == 200, op_res.text

    # 2g. Transfer 1F (replace_geometry), Basement (merge), and 2F (merge) into ProjectState
    for pg_num, reg_id, mode in (
        (2, f1_reg_id, "replace_geometry"),
        (2, bsmt_reg_id, "merge"),
        (3, f2_reg_id, "merge"),
    ):
        tx = client.post(
            f"/api/projects/{pid}/pdf-imports/{doc_id}/transfer",
            json={"page_number": pg_num, "region_id": reg_id, "merge_mode": mode},
        )
        assert tx.status_code == 200, tx.text

    # =========================================================================
    # PHASE 3: Geometry Generation, Manual Diagonal Beam Persistence & Approval
    # =========================================================================
    gen1 = client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={"default_support_type": "FIXED", "include_base_tie_beams": True},
    )
    assert gen1.status_code == 200, gen1.text
    sm1 = gen1.json()
    assert len(sm1["nodes"]) > 0
    assert any(m["member_type"] == "column" for m in sm1["members"])
    assert any(m["member_type"] == "primary_beam" for m in sm1["members"])
    assert len(sm1["slab_panels"]) > 0

    # Zero error-level issues and zero UNKNOWN_COLUMN_LEVEL warnings
    assert not any(iss["severity"] == "error" for iss in sm1["validation_issues"])
    assert not any(iss["code"] == "UNKNOWN_COLUMN_LEVEL" for iss in sm1["validation_issues"])

    # Zero auto-assigned supports above foundation elevation (2.80 m)
    supported_nodes = [n for n in sm1["nodes"] if n["is_support"] or n["support_type"] != "NONE"]
    assert len(supported_nodes) == 9  # 4 at Basement (0.0 m) + 5 stepped at 1F (2.8 m)
    assert all(n["y_m"] <= 2.85 for n in supported_nodes)

    # Manually add a diagonal beam on 2F between non-orthogonal nodes (0, 5.8, 0) and (4, 5.8, 4)
    f2_nodes = [n for n in sm1["nodes"] if abs(n["y_m"] - 5.8) <= 0.1]
    n_diag_a = next(n for n in f2_nodes if abs(n["x_m"] - 0.0) < 0.25 and abs(n["z_m"] - 0.0) < 0.25)
    n_diag_b = next(n for n in f2_nodes if abs(n["x_m"] - 4.0) < 0.25 and abs(n["z_m"] - 4.0) < 0.25)
    add_diag = client.post(
        f"/api/projects/{pid}/geometry/members",
        json={
            "start_node": n_diag_a["node_id"],
            "end_node": n_diag_b["node_id"],
            "member_type": "primary_beam",
            "floor_level": lvl_2f_id,
            "grid_ref": "DIAG-A1-B2",
        },
    )
    assert add_diag.status_code == 200, add_diag.text

    # Re-run geometry generation a SECOND time -> manual diagonal beam MUST survive!
    gen2 = client.post(
        f"/api/projects/{pid}/geometry/generate",
        json={"default_support_type": "FIXED", "include_base_tie_beams": True},
    )
    assert gen2.status_code == 200, gen2.text
    sm2 = gen2.json()
    node_map_2 = {n["node_id"]: n for n in sm2["nodes"]}

    def _is_our_diagonal(m: dict) -> bool:
        if m["member_type"] == "column":
            return False
        na = node_map_2[m["start_node"]]
        nb = node_map_2[m["end_node"]]
        dx = abs(na["x_m"] - nb["x_m"])
        dz = abs(na["z_m"] - nb["z_m"])
        return abs(na["y_m"] - 5.8) <= 0.1 and dx > 3.5 and dz > 3.5

    surviving_diags = [m for m in sm2["members"] if _is_our_diagonal(m)]
    assert len(surviving_diags) == 1, "Manual diagonal beam must persist after clicking Generate 3D Frame!"
    diag_member_id = surviving_diags[0]["member_id"]

    # Approve Phase 3 geometry
    appr_geom = client.post(f"/api/projects/{pid}/geometry/approve")
    assert appr_geom.status_code == 200, appr_geom.text
    assert appr_geom.json()["engineer_approved"] is True

    # =========================================================================
    # PHASE 4: Preliminary Member Sizing, Bulk Override & Approval
    # =========================================================================
    sz_res = client.post(f"/api/projects/{pid}/sizing/auto-size")
    assert sz_res.status_code == 200, sz_res.text
    sz = sz_res.json()

    all_mids = {m["member_id"] for m in sm2["members"]}
    assigned_mids = {a["member_id"] for a in sz["member_assignments"]}
    assert assigned_mids == all_mids
    assert diag_member_id in assigned_mids
    assert sz["material"]["ec_kn_m2"] == pytest.approx(4700.0 * (28.0 ** 0.5) * 1000.0, rel=1e-4)
    sec_by_id = {s["section_id"]: s for s in sz["sections"]}
    for a in sz["member_assignments"]:
        sec = sec_by_id[a["section_id"]]
        assert sec["width_mm"] > 0 and sec["depth_mm"] > 0

    # Bulk override: set beta_angle_deg=90 on first column member
    first_col_mid = next(m["member_id"] for m in sm2["members"] if m["member_type"] == "column")
    first_col_sec = next(
        a["section_id"] for a in sz["member_assignments"] if a["member_id"] == first_col_mid
    )
    ovr_res = client.post(
        f"/api/projects/{pid}/sizing/assign",
        json={"member_ids": [first_col_mid], "section_id": first_col_sec, "beta_angle_deg": 90.0},
    )
    assert ovr_res.status_code == 200, ovr_res.text
    ovr_asgn = next(
        a for a in ovr_res.json()["member_assignments"] if a["member_id"] == first_col_mid
    )
    assert ovr_asgn["beta_angle_deg"] == pytest.approx(90.0)

    appr_sz = client.post(f"/api/projects/{pid}/sizing/approve")
    assert appr_sz.status_code == 200, appr_sz.text
    assert appr_sz.json()["engineer_approved"] is True

    # =========================================================================
    # PHASE 5: NSCP 2015 Load Generation, Approval & STAAD.Pro .std Download + Lint
    # =========================================================================
    lg_res = client.post(f"/api/projects/{pid}/loads/generate")
    assert lg_res.status_code == 200, lg_res.text
    lg = lg_res.json()

    ss = lg["seismic_summary"]
    assert ss is not None and ss["v_design_x_kn"] > 0
    assert sum(st["fx_kn"] for st in ss["stories"]) == pytest.approx(ss["v_design_x_kn"], rel=0.01)

    lrfd_combos = [c for c in lg["combinations"] if c["design_basis"] == "LRFD" and c["combo_id"] >= 101]
    asd_combos = [c for c in lg["combinations"] if c["design_basis"] == "ASD" and c["combo_id"] >= 201]
    assert len(lrfd_combos) > 0 and len(asd_combos) > 0

    appr_lg = client.post(f"/api/projects/{pid}/loads/approve")
    assert appr_lg.status_code == 200, appr_lg.text
    assert appr_lg.json()["engineer_approved"] is True

    std_resp = client.get(f"/api/projects/{pid}/staad/download")
    assert std_resp.status_code == 200
    std_text = std_resp.text
    assert len(std_text) > 500

    # Lint exported STAAD .std grammar & referential integrity
    lint_errors = lint_staad_text(std_text)
    assert lint_errors == [], f"STAAD linter reported errors: {lint_errors}"
    # Also confirm mass-modeling Reference Load -> Floor Diaphragm order passes cleanly
    assert lint_staad_text(std_text, require_ref_load_before_diaphragm=True) == []

    # =========================================================================
    # PHASE 6: 3D DSM Frame Analysis, Drift Checks, RC Design & CSV Export
    # =========================================================================
    des_res = client.post(f"/api/projects/{pid}/design/run")
    assert des_res.status_code == 200, des_res.text
    ad = des_res.json()
    assert len(ad["story_drifts"]) == 3  # 1F, 2F, ROOF DECK above Basement
    assert len(ad["beam_designs"]) > 0
    assert len(ad["column_designs"]) > 0
    assert len(ad["footing_designs"]) == 9

    # Static Vertical Equilibrium Check under Dead Load (DL) within 5%
    proj_for_eq = ProjectState.model_validate(client.get(f"/api/projects/{pid}").json())
    dsm_results = solve_frame_dsm(proj_for_eq)
    dl_applied = dsm_results.primary_vertical_applied_kn["DL"]
    dl_reacted = dsm_results.primary_vertical_reactions_kn["DL"]
    assert dl_applied > 0.0
    assert dl_reacted == pytest.approx(dl_applied, rel=0.05)

    # Export reinforcement schedules CSV and validate headers & data rows
    csv_resp = client.get(f"/api/projects/{pid}/design/export-csv")
    assert csv_resp.status_code == 200
    csv_rows = list(csv.reader(io.StringIO(csv_resp.text)))
    csv_flat = "\n".join(",".join(r) for r in csv_rows)
    assert "=== BEAM REINFORCEMENT SCHEDULE ===" in csv_flat
    assert "=== COLUMN REINFORCEMENT SCHEDULE ===" in csv_flat
    assert "=== ISOLATED FOOTING SCHEDULE ===" in csv_flat
    assert "Top Start Bars" in csv_flat
    assert "Main Bars" in csv_flat
    assert "Punching DCR" in csv_flat
    # 3 section banners + 3 column headers + 2 blank separator rows = 8 overhead rows
    expected_csv_rows = (
        8 + len(ad["beam_designs"]) + len(ad["column_designs"]) + len(ad["footing_designs"])
    )
    assert len(csv_rows) == expected_csv_rows

    # =========================================================================
    # PERSISTENCE ROUND-TRIP CHECK (Strict ProjectState extra="forbid")
    # =========================================================================
    persisted_json = data_root / "projects" / f"{pid}.json"
    assert persisted_json.exists()
    raw_disk = json.loads(persisted_json.read_text(encoding="utf-8"))
    reloaded = ProjectState.model_validate(raw_disk)

    assert reloaded.project_info.project_id == pid
    assert len(reloaded.levels) == 4
    assert len(reloaded.columns) == 22  # 4 Basement + 9 1F + 9 2F
    assert len(reloaded.openings) == 1
    assert reloaded.structural_model.engineer_approved is True
    assert len(reloaded.structural_model.manual_beams) == 1
    assert reloaded.member_sizing.engineer_approved is True
    assert reloaded.load_generation.engineer_approved is True
    assert reloaded.analysis_design.generated_at is not None
    assert len(reloaded.analysis_design.beam_designs) == len(ad["beam_designs"])
    assert len(reloaded.analysis_design.column_designs) == len(ad["column_designs"])
    assert len(reloaded.analysis_design.footing_designs) == len(ad["footing_designs"])
