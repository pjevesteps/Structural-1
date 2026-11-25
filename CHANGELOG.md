# Changelog

All notable changes to **AI Structural Engineering Office** are documented in this file.

## [Unreleased]

### Added
- **Coordinate-Anchored Manual & Diagonal Beam Persistence (`backend/app/models.py`, `schemas.py`, `geometry/`, `frontend/src/GeometryBuilder.jsx`)**:
  - Added backward-compatible `ManualBeamOverride` model and `StructuralModelState.manual_beams` list (default empty, compatible with `extra="forbid"`).
  - Updated `POST /api/projects/{project_id}/geometry/members` to persist coordinate-anchored `(x_m, y_m, z_m)` overrides for any manual/diagonal/non-orthogonal beam, added `replicate_all_floors: bool = False` to replicate skewed perimeter beams across all matching framing levels, and updated `DELETE /members/{member_id}` to clean up matching `manual_beams` entries.
  - Updated `generate_structural_model` to preserve and replay all saved `manual_beams` onto regenerated nodes (within `max(snap_tolerance_m, 0.25)` m in XZ and `0.10` m in Y) so clicking **Generate 3D Frame** never wipes user-added diagonal or secondary beams.
  - Updated `GeometryBuilder.jsx` to render manual/diagonal beams distinctly in bright violet (`#c084fc`) in both 2D and 3D views (never hidden by story filters), and added a **Replicate to matching floors** checkbox in `Add Beam` mode.
  - Added `test_diagonal_manual_beam_replication_persistence_and_staad_export` in `backend/tests/test_geometry_generator.py`.
- **Level Dropdown & Flexible Level Matching (`frontend/src/PdfImporter.jsx`, `backend/app/geometry/generator.py`)**:
  - Replaced free-text region `Level` inputs in `PdfImporter.jsx` with `<select>` dropdowns populated directly from `project.levels` (`"{id} ({elevation_m} m)"`), refreshed automatically after transferring levels, with a fallback prompt (`"Extract levels on Elevation/Section first"`) when no levels exist yet.
  - Added `_resolve_level_index` in `backend/app/geometry/generator.py` supporting case-insensitive `id`/`name` matching, common floor aliases (`B1`/`BASEMENT`, `L1`/`1F`/`FIRST`, `L2`/`2F`/`SECOND`, `L3`/`3F`/`THIRD`, `L4`/`4F`/`FOURTH`, `RD`/`ROOF DECK`), and numeric elevation matching (`±0.05 m`, e.g. `"14"` -> `14.0 m`), plus actionable available-level lists in `UNKNOWN_COLUMN_LEVEL` warnings.
  - Added unit test `test_flexible_column_level_and_numeric_elevation_matching` in `backend/tests/test_geometry_generator.py`.
- **Manual Grid Undo/Redo & Clear Region Grids (`backend/app/pdf_importer/`, `frontend/src/PdfImporter.jsx`)**:
  - Added `DELETE /api/projects/{project_id}/pdf-imports/{doc_id}/regions/{region_id}/grids` (`clear_region_grids` in `service.py` + `router.py`) to atomically delete all `grid_x` / `grid_y` candidates in a region while preserving columns, openings, and other regions, logged as `region_grids_cleared` in `audit.jsonl`.
  - Added per-session `undoStack` / `redoStack` in `frontend/src/PdfImporter.jsx` tracking manual grid additions and deletions (`add_grid` / `delete_grid`), rolling `Next Grid Label` back/forward automatically, with compact **Undo** (`Ctrl+Z`) and **Redo** (`Ctrl+Y` / `Ctrl+Shift+Z`) controls and global keyboard shortcuts.
  - Added a one-click **Clear Grids** button (with lightweight confirmation) in the Plan Regions bar and Staged Review panel.
  - Added unit test `test_manual_grid_undo_redo_and_clear_region_grids` in `backend/tests/test_pdf_importer.py`.

### Fixed
- **Phase 2 Shaded/Hatched Column Scanner — Zero Text/Dimension False Positives (`backend/app/pdf_importer/detector.py`)**:
  - Removed the dark-pixel morphological closing pass that fused dimension numbers and room labels (`9.75`, `3.60`, `STORAGE`, `PARKING`, `BASEMENT`, `ALLEY`) into fake hatched boxes.
  - Added embedded PDF text masking via `page.get_text("words")` (rejecting any candidate whose center falls inside a word box or whose padded overlap exceeds 25%) and bottom-12% title-block exclusion.
  - Rebuilt the vector path to detect solid non-white/non-pure-black rectangular fills plus true closed 4-corner vector rectangles containing `>= 4` interior hatch strokes (never fusing free text strokes).
  - Rebuilt the raster path strictly on mid-gray pixels (`60 <= gray <= 215`) with light `3×3` opening (no black pixels, no heavy closing) and uniform gray interior verification (`mid_gray >= 0.70`, `black <= 0.20`, `std_dev <= 35.0`, `solidity >= 0.88`, `rectangularity >= 0.78`), verified on real architectural Page 4 (`8` Basement + `24` First Floor columns, `0` text overlaps).

### Added
- **Tuned Phase 2 Shaded & Hatched Column Scanner (`backend/app/pdf_importer/`, `frontend/src/PdfImporter.jsx`)**:
  - Extended `ExtractionOptions` and `ExtractRequest` in `schemas.py` with `detect_grids`, `detect_columns`, `column_fill_mode` (`"shaded_or_hatched"`, `"gray_shaded_only"`, `"any_filled"`), `min_column_size_m`, `max_column_size_m`, `column_max_aspect_ratio`, `column_min_rectangularity`, `column_min_fill_ratio`, `require_near_grid`, `column_probe_radius_px`, and `snap_columns_to_grids`.
  - Added dedicated hybrid vector + raster `detect_shaded_hatched_columns` pipeline in `detector.py` with scale-aware pixel bounds, gray-shaded + morphological hatch-fusion masks, `minAreaRect`/solidity/`approxPolyDP` quad filtering, strict un-morphed interior shading/Sobel-Canny hatch verification (rejecting hollow furniture/rooms, pitch-black stove icons, circles/bubbles, long thin walls, and large L-shaped earth backfills), IoU deduplication, and grid intersection snapping/confidence boosting.
  - Updated `service.py` so **Scan Columns Only** (`detect_grids=False, detect_columns=True`) preserves existing stamped/manual grids while clearing only unapproved auto-detected columns in the active region.
  - Updated `frontend/src/PdfImporter.jsx` with a **Scan Columns Only** button, compact column tuning controls inside **Detection Settings**, and exact `width_px × height_px` outlined column rectangles with center crosshairs and grid labels on the SVG overlay.
  - Added unit test `test_shaded_hatched_column_scanner_filters_non_columns_and_preserves_grids` in `backend/tests/test_pdf_importer.py`.
- **Phase 3 — Module 03 · Structural Geometry Generation**:
  - **Analytical Geometry Schema (`backend/app/models.py`, `backend/app/schemas.py`)**: Added backward-compatible Pydantic models (`AnalyticalNode`, `AnalyticalMember`, `SlabPanel`, `GeometryCheckIssue`, `GeometryGenerationOptions`, `StructuralModelState`) attached to `ProjectState.structural_model` (respecting `extra="forbid"`), added optional `Column.floor_level: str = ""` preserved during PDF Importer transfers, and added `backend/app/schemas.py` re-exporting all models.
  - **Geometry Generation Engine & Validator (`backend/app/geometry/`)**: Replaced standalone `geometry.py` with a package preserving Phase 1 `plan_to_staad`, `build_node_map`, and `infer_span` while adding:
    - `generator.py`: `generate_structural_model` building STAAD Y-up deduplicated nodes, stepped-base column stacks & automatic lowest-node supports (`FIXED`/`PINNED`), primary & tie beams, opening midpoint beam exclusion (`BEAM_SKIPPED_OPENING`), and rectangular slab panel detection with Sutherland-Hodgman opening polygon clipping (`one_way` / `two_way` / `void` + gross/deduction/net areas).
    - `validator.py`: `validate_structural_model` checking structural connectivity and modeling errors (`EMPTY_MODEL`, `DUPLICATE_NODE`, `UNKNOWN_NODE_REF`, `ZERO_LENGTH_MEMBER`, `DUPLICATE_MEMBER`, `NO_SUPPORTS`) and engineering warnings (`FLOATING_OR_TRANSFER_COLUMN`, `ORPHAN_NODE`, `LONG_BEAM_SPAN`).
    - `router.py`: Mounted at `/api/projects/{project_id}/geometry` with `POST /generate`, `POST /validate`, `POST /members` (direct node pair or splitting existing beams for secondary/trimmer beams), `DELETE /members/{member_id}`, `PATCH /nodes/{node_id}/support`, and `POST /approve` (error-gated).
  - **Upgraded STAAD.Pro `.std` Export (`backend/app/staad.py`)**: Exports directly from `project.structural_model` (`STAAD SPACE`, `UNIT METER KN`, `JOINT COORDINATES`, `MEMBER INCIDENCES`, `START GROUP DEFINITION` for `_COLUMNS`, `_BEAMS`, `_TIE_BEAMS` and per-level groups, and `SUPPORTS`) with inline `std_preview`, while preserving Phase 1 fallback when `structural_model` is empty.
  - **Module 03 Frontend UI (`frontend/src/GeometryBuilder.jsx`, `frontend/src/App.jsx`, `frontend/src/style.css`)**: Pure HTML5 Canvas 3D analytical wireframe (drag-to-orbit, wheel-to-zoom, story filter, XYZ triad, color-coded columns/beams/supports) + interactive SVG 2D floor framing editor (`Inspect`, `Add Beam`, `Delete Member`, `Toggle Support`, slab panels with `1-WAY`/`2-WAY`/`VOID` badges & net area, floor openings), live stats + Geometry Integrity Check sidebar, **Approve Geometry**, and live copyable STAAD `.std` preview drawer (zero extra npm dependencies).
  - **Tests (`backend/tests/test_geometry_generator.py`)**: Added 4 unit & API tests covering stepped-base partial basements, opening beam skipping & slab area deduction, transfer/floating column warnings, manual member add/delete, support toggling, approval gating, upgraded STAAD `.std` export, and `DEMO-001.json` backward compatibility.
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
