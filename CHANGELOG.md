# Changelog

All notable changes to **AI Structural Engineering Office** are documented in this file.

## [Unreleased]

### Added
- **Precision Zoom/Pan Tracing, Master Grid Stamp, Polygon Floor Openings & Floor-to-Floor Levels**:
  - `FloorOpening` model and `openings: list[FloorOpening]` on `ProjectState` (`backend/app/models.py`), backward-compatible with existing `data/projects/*.json` files under `extra="forbid"`.
  - `require_grid_label: bool = True` on `ExtractionOptions` supporting prime grid labels (`1'`) and deduplicating identical-label grids per region without synthesizing fallback labels for unlabeled walls/dimension chains.
  - Manual grid tracing (`POST /elements/manual-grid`), element deletion (`DELETE /elements/{element_id}`), and level deletion (`DELETE /levels/{level_id}`).
  - Document-level `MasterGridDefinition` (`POST /master-grid` to lock meter offsets from a calibrated region, `POST /regions/{target_region_id}/stamp-master-grid` to stamp clean approved grids onto any floor region at a clicked `anchor_px` without auto-line scanning).
  - Polygon floor openings (`DetectedOpening`, `POST /openings`, `DELETE /openings/{opening_id}`, `POST /regions/{target_region_id}/copy-openings`, and shoelace `area_sqm` calculation on transfer into `ProjectState.openings`).
  - Frontend precision canvas in `frontend/src/PdfImporter.jsx`: cursor-centered mouse-wheel zoom (`0.5x–8x`), `- / 100% / + / Fit` controls, drag-to-pan (`Inspect` drag, `Space+drag`, middle-mouse drag), inverse-zoom SVG overlays, auto-incrementing manual grid tracing, **Lock as Master Grid** & **Stamp Master Grid**, 18px intersection-snapped column placement (`Shift` for off-grid), polygon opening tracing (`Stairwell`, `Airwell / Air Vent`, `Shaft / Other`) + **Copy Openings From...**, editable floor-to-floor `ΔH` levels panel, and compact Delete (`×`) controls.
  - Added 3 unit tests in `backend/tests/test_pdf_importer.py` (`test_labeled_grids_only_drops_unlabeled_walls`, `test_floor_to_floor_elevation_filtering_only`, `test_manual_grid_master_grid_stamp_and_polygon_openings`).

### Changed
- Restricted dual-plan center viewport divider detection in `inspector.py` to `46%–56%` page width spanning `>= 80%` of active drawing height.
- Restricted `extract_levels_from_elevation_or_section` in `detector.py` strictly to structural floor/roof framing levels (filtering out ceiling, firewall, parapet, NGL, curbs/micro-steps `< 1.50 m`) with floor-to-floor `story_height_m`.

### Fixed
- **Real-World AutoCAD Architectural PDF Support (`inspector.py`, `detector.py`)**:
  - Transformed PyMuPDF text and vector drawing coordinates through `page.rotation_matrix` so rotated landscape sheets (e.g. `rotation=270`) align pixel-for-pixel with rendered PNGs.
  - Added spaced-letter and newline-split AutoCAD text normalization (e.g., `"B A S E M E N T F L O O R P L A N"` -> `"BASEMENT FLOOR PLAN"`, `"C\nR\nO\nS\nS\n \nS\nE\nC\nT\nI\nO\nN"` -> `"CROSS SECTION"`).
  - Restricted `sheet_number` extraction strictly to the bottom-right title block (`x >= 80%`, `y >= 84%`) so interior section-cut callouts (e.g. `"A12"`) are never mistaken for the sheet number.
  - Added pixel-based + vector dual-plan center viewport divider detection (42%–58% width, >= 60% dark vertical coverage above the bottom 12% title block) and smart SHX text-clue fallbacks.
  - Excluded scale strings (`SCALE: 1:100M`) and ceiling-line text from elevation floor-level extraction.

### Added
- `PATCH /api/projects/{project_id}/pdf-imports/{doc_id}/pages/{page_number}` for overriding `sheet_type`, plus `POST /regions` and `DELETE /regions/{region_id}` for managing manual/auto `PlanRegion` boxes.
- Interactive **Page Type dropdown**, **Draw Region** and **Set Origin (0,0)** toolbar buttons, live dashed region drawing preview, and a **Plan Regions panel** in `frontend/src/PdfImporter.jsx`.
- **Multi-Page Architectural PDF Set Support**:
  - Automatic per-page sheet classification (`cover_toc`, `lot_site`, `floor_plan`, `roof_plan`, `elevation`, `section`, `schedule_detail`, `unknown`) and bottom-right title-block `sheet_number` / `sheet_title` extraction strictly from each page's own embedded text (`PageInfo` / `PageMetadata`).
  - Dual-plan viewport detection (`viewport_count`) and automatic `PlanRegion` creation (Left/Right floor plan viewports excluding bottom 12% title block).
  - Elevation & Section floor-level extractor (`extract_levels_from_elevation_or_section` + `DetectedLevel`), duplicate tag & story-height consistency warnings, inline review/edit (`PATCH /levels/{level_id}`), and sorted transfer into `ProjectState.levels` (`POST /transfer-levels`).
  - Master Grid propagation across floor regions (`POST /regions/{target_region_id}/apply-master-grid`) with origin shift `(target.origin_px - source.origin_px)` and intersection-gated column probing inside the target viewport, plus `PATCH /regions/{region_id}` for setting region origins.
  - Architectural Set UI in `frontend/src/PdfImporter.jsx`: color-coded Page Navigator strip (`FLOOR PLAN (2)`, `ELEVATION`, `SECTION`, `SKIP: COVER`, `SKIP: SCHEDULE`), auto-region SVG overlays, **Set Region Origin**, **Copy Master Grid to This Region**, **Extract Floor Levels**, SVG datum overlays, inline level editing, and **Transfer Approved Levels to Project**.
  - Added unit tests `test_architectural_sheet_classification_and_master_grid` and `test_elevation_level_extraction_and_transfer` in `backend/tests/test_pdf_importer.py`.
- `ExtractionOptions` Pydantic model for `POST /api/projects/{project_id}/pdf-imports/{doc_id}/pages/{page_number}/extract` (`border_margin_ratio=0.04`, `min_span_ratio=0.28`, `cluster_tolerance_px=8.0`, `merge_dashed_segments=True`, `detect_grid_labels=True`).
- Collapsible **Detection Settings** bar in `frontend/src/PdfImporter.jsx` (Min Grid Span %, Border Margin %, Cluster Tolerance px, Merge dashed, Detect labels) wired to **2. Detect Grids & Columns**.
- Unit test `test_dashed_grids_labels_and_border_exclusion` covering outer sheet border exclusion, dashed vector & scanned grid merging, and endpoint grid label matching.
- `CHANGELOG.md` to track all project phases, features, bug fixes, and schema updates.
- Updated `README.md` with Phase 2 PDF Plan Importer architecture, storage layout, API endpoints, and test instructions.

### Changed
- Tuned `detect_vector_elements` and `detect_raster_elements` in `backend/app/pdf_importer/detector.py` to exclude segments in the outer border margin, merge colinear dashed segments within `cluster_tolerance_px`, filter by `min_span_ratio`, and match 1–3 character alphanumeric grid labels near line endpoints via `page.get_text("words")`.
- Positioned SVG grid bubbles cleanly at the top (X-grid) and left (Y-grid) ends of grid lines on the overlay.

## [0.2.0] - 2026-11-24 — Phase 2: PDF Plan Importer

### Added
- **Backend PDF Importer (`backend/app/pdf_importer/`)**:
  - `schemas.py`: Strict Pydantic models (`extra="forbid"`) for pages, 2-point scale calibrations (`mm`, `cm`, `m`, `ft`, `in`), staged grid/column candidates, review requests, transfer records, and audit entries.
  - `inspector.py`: File size and `%PDF-` header validation, PyMuPDF page classification (`vector`, `scanned`, `hybrid`), PNG rendering, and pixel-to-plan-meter (`x_m`, `y_m`) conversion matching `backend/app/geometry.py`.
  - `detector.py`: Vector grid/column detection via PyMuPDF with grid-bubble label matching, OpenCV (`cv2`) raster fallback for scanned pages, and a non-fabricating `ExperimentalVLMDetector` placeholder interface.
  - `service.py`: Immutable storage layout in `data/imports/{project_id}/{doc_id}/` (`original.pdf`, `pages/`, `state.json`, `audit.jsonl`), manual placement, candidate review, and gated transfer to `ProjectState` requiring confirmed scale calibration and engineer approval.
  - `router.py` mounted in `backend/app/main.py` at `/api/projects/{project_id}/pdf-imports`.
- **Frontend PDF Importer (`frontend/src/`)**:
  - `PdfImporter.jsx`: Multi-page PDF upload & switcher, page navigation, interactive 2-point scale calibration overlay, vector/raster/VLM detection controls, SVG overlay for grids and columns, manual column placement, accept/reject review panel, and **Transfer Approved to Project**.
  - Updated `App.jsx` with **Import PDF** action on each project card and updated Module `02` description.
  - Appended responsive dark-theme PDF importer styles to `style.css`.
- **Testing & Repository Setup**:
  - Added end-to-end test suite `backend/tests/test_pdf_importer.py` using synthetic multi-page vector + scanned PDFs.
  - Added `.gitignore` and initialized GitHub repository backup at `https://github.com/pjevesteps/Structural-1.git`.

### Fixed
- Fixed literal `"\\n"` newline bug in `backend/app/staad.py` so `.std` geometry preview exports write real line breaks.

## [0.1.0] - Phase 1: Starter Workspace

### Added
- FastAPI backend with `ProjectState` (`extra="forbid"`) schema validation and reference checks (`backend/app/models.py`).
- Local JSON project storage in `data/projects/` (`backend/app/storage.py`).
- Plan X/Y + elevation to STAAD X/Y-up/Z coordinate mapping (`backend/app/geometry.py`).
- Geometry-only `.std` preview export and explicit `NOT_ANALYZED` simulation placeholder (`backend/app/staad.py`).
- React/Vite responsive dark-theme dashboard (`frontend/src/App.jsx`, `frontend/src/style.css`) and demonstration project (`DEMO-001`).
