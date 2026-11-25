# AI Structural Engineering Office

Local-first structural engineering workspace: React/Vite frontend + FastAPI backend + strict Pydantic JSON contract (`ProjectState`, `extra="forbid"`).

Repository: https://github.com/pjevesteps/Structural-1

## Safety
This is software infrastructure, not validated structural analysis or code-compliant design. The `.std` output is geometry-only. Simulation returns a `NOT_ANALYZED` status with no fabricated forces or drift. Experimental VLM detection is a placeholder interface only and never fabricates elements. Engineer review, confirmed scale calibration, and independent verification are mandatory.

## Run on Windows
Requirements: Python 3.10+, Node.js 20+, npm.

Backend (PowerShell, from project root):
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend (second PowerShell):
```powershell
cd frontend
npm install
npm run dev -- --host 0.0.0.0
```
Open the Vite URL printed in the terminal. API docs: http://localhost:8000/docs.

## Run Tests
```powershell
.\.venv\Scripts\pytest.exe -v
```
Includes unit & API tests for Phases 1–6 plus the full Phase 1 → Phase 6 end-to-end pipeline & STAAD.Pro grammar linter (`backend/tests/test_e2e_pipeline.py`, isolated in pytest `tmp_path`).

## Tablet Access
Connect tablet and PC to the same trusted Wi-Fi. Run `ipconfig` on Windows, then open `http://PC-IP:5173` on the tablet. If prompted, allow the app only on a private network. Do not expose the development server to the public internet.

## Included

### Phase 1 — Core Workspace & Geometry Foundation
- Strict `ProjectState` Pydantic schema validation (`extra="forbid"`) and reference checks
- JSON project storage in `data/projects/{project_id}.json`
- CAD plan X/Y + elevation -> STAAD global X/Y-up/Z coordinate mapping (`backend/app/geometry.py`)
- Geometry-only `.std` preview export (`backend/app/staad.py`)
- Explicit placeholder simulation endpoint (`NOT_ANALYZED`)
- Responsive dark-theme dashboard (`frontend/src/App.jsx`)

### Phase 2 — PDF Plan Importer
- Multi-page PDF upload with file size and `%PDF-` header validation
- Immutable import storage under `data/imports/{project_id}/{doc_id}/`:
  - `original.pdf` (immutable uploaded source)
  - `pages/page_NNN.png` (PyMuPDF rendered page previews)
  - `state.json` (staged calibrations, candidates, and transfer history)
  - `audit.jsonl` (append-only audit trail)
- Page classification (`vector`, `scanned`, `hybrid`) via PyMuPDF
- Interactive 2-point scale calibration (`mm`, `cm`, `m`, `ft`, `in`) converting pixel coordinates to plan meters (`x_m`, `y_m`)
- Vector grid/column detection via PyMuPDF with OpenCV (`cv2`) raster fallback, dedicated hybrid shaded/hatched square & rectangular column scanning (**Scan Columns Only** preserving stamped Master Grids, rejecting hollow shapes, circles, walls, pitch-black icons, and earth-backfill hatches), plus an experimental VLM placeholder interface
- Interactive SVG overlay (`frontend/src/PdfImporter.jsx`) for calibration, detected grids/columns, click-to-place manual columns, and accept/reject review
- Controlled transfer: only engineer-approved elements are merged/transferred into `ProjectState`, and only after scale calibration is explicitly confirmed

### Phase 3 — Structural Geometry Generation (Module 03)
- Analytical 3D wireframe generation (`backend/app/geometry/generator.py`) from approved `ProjectState` grids, floor levels, columns, and floor openings:
  - STAAD Y-up deduplicated nodes (`AnalyticalNode`), stepped-base column stacks, automatic lowest-node `FIXED` / `PINNED` supports
  - Primary framing beams, base tie beams, and automatic skipping of beams crossing floor openings
  - Rectangular slab panel detection (`SlabPanel`) with Sutherland-Hodgman polygon opening deduction and `one_way` / `two_way` / `void` classification
- Structural integrity validator (`backend/app/geometry/validator.py`) catching duplicate nodes/members, zero-length members, missing supports, orphan nodes, transfer/floating columns, and long beam spans
- Interactive Module 03 UI (`frontend/src/GeometryBuilder.jsx`): HTML5 Canvas 3D orbit/zoom wireframe, 2D SVG floor framing editor (`Inspect`, `Add Beam`, `Delete Member`, `Toggle Support`), integrity checklist, approval gate, and live `.std` preview drawer
- Upgraded geometry-only STAAD.Pro `.std` exporter (`backend/app/staad.py`) emitting `JOINT COORDINATES`, `MEMBER INCIDENCES`, `START GROUP DEFINITION` (`_COLUMNS`, `_BEAMS`, `_TIE_BEAMS`, per-level groups), and `SUPPORTS`

### Phase 4 — Preliminary Member Sizing & Property Assignment (Module 04)
- Automated NSCP 2015 preliminary sizing (`backend/app/sizing/engine.py`):
  - Story-tiered square column sections (`C1`, `C2`, `C3` with `0.70 Ig` cracked modifiers), span-bucketed primary/tie/secondary beam sections (`B1..`, `TB1..`, `SB1..` with `0.35 Ig` and optional `START/END MY MZ` releases), and one-way/two-way/void slab thicknesses
  - Automatic concrete modulus calculation (`Ec = 4700 * sqrt(f'c)` in `kN/m²`)
- NSCP 2015 SMRF (Zone 4) validator (`backend/app/sizing/validator.py`) checking assignment completeness, column min dimension (`>= 300 mm`), beam min width (`>= 250 mm`, `b/h >= 0.30`), beam-vs-column width, and slab min thickness
- Full STAAD.Pro `.std` property exporter (`backend/app/staad.py`) emitting `DEFINE MATERIAL START`, `MEMBER PROPERTY` (`PRIS YD/ZD`), `CONSTANTS` (`MATERIAL` + `BETA`), `MEMBER CRACKED`, `MEMBER RELEASE`, `_SEC_*` groups, and 72-char `-` line continuations
- Interactive Module 04 UI (`frontend/src/MemberSizing.jsx`): color-coded 3D property viewer, Section Catalog CRUD, filterable Member Schedule with Bulk Assign, Slab Thickness Schedule, live concrete volume (`m³`), and NSCP 2015 check sidebar

### Phase 5 — NSCP 2015 Load Generation & Full STAAD.Pro Export (Module 05)
- NSCP 2015 gravity, seismic, wind & load combination engine (`backend/app/loads/engine.py`):
  - One-way (`UNI`) and two-way (`TRAP`) tributary slab-to-beam loads honoring traced floor opening deductions plus perimeter/interior CHB wall line loads
  - NSCP 2015 Section 208 static seismic calculation (`Na`/`Nv` Tables 208-5 & 208-6 interpolation, `Ca`/`Cv` Tables 208-7 & 208-8, story seismic weights `W_i`, `V_max`/`V_min`/`V_min_z4` bounds, top force `Ft`, cumulative shears)
  - NSCP 2015 Section 207 MWFRS directional wind loads (`Kz` Table 207B.3-1, `qz`, story lateral forces `Fx`/`Fz`)
  - NSCP 2015 Sec. 203.3.1 LRFD (`101+`) and Sec. 203.4.2 ASD (`201+`) load combinations with vertical seismic `Ev = 0.5 * Ca * I`
- Full analysis-ready STAAD.Pro `.std` exporter (`backend/app/staad.py`) appending `DEFINE REFERENCE LOADS` (`R1`/`R2`), `FLOOR DIAPHRAGM` (unsupported nodes), `DEFINE UBC LOAD`, Seismic-before-Gravity primary cases (`1–11`), `LOAD COMB` / `REPEAT LOAD`, `PERFORM ANALYSIS PRINT STATICS CHECK`, and ACI `START CONCRETE DESIGN` block, plus downloadable `<project_id>.std`
- Interactive Module 05 UI (`frontend/src/LoadGenerator.jsx`): regional quick presets, 4-tab Gravity/Seismic/Wind/Combinations workspace, story force charts, NSCP check sidebar, live `.std` preview drawer, and one-click download

### Phase 6 — Analysis Bridge, NSCP 2015 Drift Checks & RC Member Design (Module 06)
- Built-in 3D linear elastic Direct Stiffness Method solver (`backend/app/design/solver.py`, `numpy`) with cracked section modifiers, `beta_angle_deg`, exact static condensation for pinned beam releases, consistent Gauss-Legendre distributed loads, and LRFD/ASD combination envelopes
- STAAD.Pro `.anl` output parser (`backend/app/design/anl_parser.py`) extracting `JOINT DISPLACEMENT`, `SUPPORT REACTIONS`, and `BEAM END FORCES` into the unified NSCP 2015 design pipeline
- NSCP 2015 / ACI 318 design checks (`backend/app/design/rc_design.py`): story drift (`ΔM = 0.7 R Δs`) & soft-story irregularity, RC beam flexure & SMRF stirrups, RC column biaxial P-M interaction & confinement ties, isolated footing bearing/one-way/punching shear sizing (`F1`, `F2`, ...), and CSV reinforcement schedule export
- Interactive Module 06 UI (`frontend/src/DesignReview.jsx`): 3D Utilization View (DCR color-coded with click-to-inspect), Story Drift & Reactions, Beam/Column/Footing schedules, STAAD `.anl` uploader, CSV export, and Design Health sidebar

## API

### Phase 1 Endpoints
- `GET /api/health`
- `GET /api/projects`
- `POST /api/projects`
- `GET /api/projects/{project_id}`
- `POST /api/projects/{project_id}/validate`
- `POST /api/projects/{project_id}/staad-preview`
- `POST /api/projects/{project_id}/simulate`

### Phase 2 PDF Importer Endpoints (`/api/projects/{project_id}/pdf-imports`)
- `POST /api/projects/{project_id}/pdf-imports` — Upload multi-page PDF plan
- `GET /api/projects/{project_id}/pdf-imports` — List PDF imports for project
- `GET /api/projects/{project_id}/pdf-imports/{doc_id}` — Get staged import state
- `GET /api/projects/{project_id}/pdf-imports/{doc_id}/audit` — Read audit trail (`audit.jsonl`)
- `GET /api/projects/{project_id}/pdf-imports/{doc_id}/pages/{page_number}/image` — Rendered page PNG
- `PATCH /api/projects/{project_id}/pdf-imports/{doc_id}/pages/{page_number}` — Override page `sheet_type`, `sheet_number`, or `sheet_title`
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/regions` — Manually create a `PlanRegion` box
- `DELETE /api/projects/{project_id}/pdf-imports/{doc_id}/regions/{region_id}` — Delete a `PlanRegion` box
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/pages/{page_number}/calibrate` — Set 2-point scale calibration
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/pages/{page_number}/confirm-calibration` — Confirm scale calibration
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/pages/{page_number}/extract` — Run vector, raster, auto, or VLM placeholder detection (with optional `ExtractionOptions` and `region_id`)
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/pages/{page_number}/extract-levels` — Extract floor levels & story heights from elevation/section sheets
- `PATCH /api/projects/{project_id}/pdf-imports/{doc_id}/levels/{level_id}` — Edit or approve/reject a detected floor level
- `DELETE /api/projects/{project_id}/pdf-imports/{doc_id}/levels/{level_id}` — Remove an unwanted floor level
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/transfer-levels` — Transfer engineer-approved floor levels into `ProjectState.levels`
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/elements/manual-grid` — Manually trace a snapped vertical (`grid_x`) or horizontal (`grid_y`) grid line
- `DELETE /api/projects/{project_id}/pdf-imports/{doc_id}/elements/{element_id}` — Delete a staged grid or column
- `DELETE /api/projects/{project_id}/pdf-imports/{doc_id}/regions/{region_id}/grids` — Atomically clear all grid lines in a region while keeping columns, openings, and other regions intact
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/master-grid` — Lock approved region grids & `(0,0)` origin as the document Master Grid in meters
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/regions/{target_region_id}/stamp-master-grid` — Stamp the locked Master Grid onto a target floor region at `anchor_px`
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/openings` — Create a polygon floor opening (`stairwell`, `airwell`, `shaft_other`)
- `DELETE /api/projects/{project_id}/pdf-imports/{doc_id}/openings/{opening_id}` — Delete a staged floor opening
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/regions/{target_region_id}/copy-openings` — Copy openings across floor regions aligned by `(0,0)` origin
- `PATCH /api/projects/{project_id}/pdf-imports/{doc_id}/regions/{region_id}` — Update floor plan region metadata or anchor `origin_px`
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/regions/{target_region_id}/apply-master-grid` — Copy approved master grids into a target floor region and probe intersection columns
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/candidates/columns` — Manually place a column candidate
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/candidates/grids` — Manually place a grid candidate
- `PATCH /api/projects/{project_id}/pdf-imports/{doc_id}/candidates/{candidate_id}` — Accept, reject, or edit a candidate
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/candidates/bulk-review` — Bulk accept/reject candidates
- `POST /api/projects/{project_id}/pdf-imports/{doc_id}/transfer` — Transfer approved elements to `ProjectState` (requires confirmed scale)

### Phase 3 Structural Geometry Endpoints (`/api/projects/{project_id}/geometry`)
- `POST /api/projects/{project_id}/geometry/generate` — Generate analytical nodes, members, slab panels, and run integrity checks
- `POST /api/projects/{project_id}/geometry/validate` — Re-run structural geometry integrity validation
- `POST /api/projects/{project_id}/geometry/members` — Manually add a beam/column between existing nodes or split existing members for secondary/trimmer beams
- `DELETE /api/projects/{project_id}/geometry/members/{member_id}` — Delete an analytical member and re-validate
- `PATCH /api/projects/{project_id}/geometry/nodes/{node_id}/support` — Toggle node support (`NONE`, `FIXED`, `PINNED`) and re-validate
- `POST /api/projects/{project_id}/geometry/approve` — Mark geometry approved for downstream load/STAAD modules (blocked if any `error` issues exist)

### Phase 4 Member Sizing & Property Endpoints (`/api/projects/{project_id}/sizing`)
- `POST /api/projects/{project_id}/sizing/auto-size` — Generate preliminary NSCP 2015 sections, member assignments, and slab thicknesses
- `PATCH /api/projects/{project_id}/sizing/material` — Update concrete/rebar material (`f'c`, `fy`) & sizing options, recalculate `Ec`, and re-validate
- `POST /api/projects/{project_id}/sizing/sections` — Create or update a `SectionDefinition` and re-validate
- `DELETE /api/projects/{project_id}/sizing/sections/{section_id}` — Delete an unused section (400 if still assigned)
- `POST /api/projects/{project_id}/sizing/assign` — Bulk assign `member_ids` to a section with optional `beta_angle_deg` and `MY/MZ` releases
- `PATCH /api/projects/{project_id}/sizing/slabs/{panel_id}` — Update slab panel thickness or tag and re-validate
- `POST /api/projects/{project_id}/sizing/approve` — Approve preliminary properties (blocked if any `error` issues remain)

### Phase 5 NSCP 2015 Load Generation & STAAD Export Endpoints (`/api/projects/{project_id}`)
- `POST /api/projects/{project_id}/loads/generate` — Generate NSCP 2015 gravity tributary beam loads, seismic, wind, LRFD/ASD combinations, and validation checks
- `PATCH /api/projects/{project_id}/loads/config` — Update floor loads, seismic, wind, or export options and recalculate cleanly
- `POST /api/projects/{project_id}/loads/approve` — Record engineer approval for generated loads (blocked if any `error` issues remain)
- `GET /api/projects/{project_id}/staad/download` — Download the full STAAD.Pro input file as `<project_id>.std`

### Phase 6 Analysis Bridge & RC Design Endpoints (`/api/projects/{project_id}/design`)
- `POST /api/projects/{project_id}/design/run` — Run internal 3D DSM frame solver, NSCP 2015 drift/soft-story checks, and RC beam/column/footing design
- `POST /api/projects/{project_id}/design/upload-anl` — Upload a STAAD.Pro `.anl` output file, parse envelopes, and run NSCP 2015 drift & RC design
- `POST /api/projects/{project_id}/design/approve` — Record engineer approval (blocked while failing checks remain)
- `GET /api/projects/{project_id}/design/export-csv` — Download Beam, Column, and Footing reinforcement schedules as CSV

## Documentation & History
See [CHANGELOG.md](CHANGELOG.md) for a chronological record of changes.

Next: DXF import, reviewed load assignment, validated STAAD/OpenSTAAD adapter, result extraction, independent design checks, report and drawing generation.
