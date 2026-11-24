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

## Documentation & History
See [CHANGELOG.md](CHANGELOG.md) for a chronological record of changes.

Next: DXF import, reviewed load assignment, validated STAAD/OpenSTAAD adapter, result extraction, independent design checks, report and drawing generation.
