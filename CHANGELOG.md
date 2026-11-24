# Changelog

All notable changes to **AI Structural Engineering Office** are documented in this file.

## [Unreleased]

### Added
- `CHANGELOG.md` to track all project phases, features, bug fixes, and schema updates.
- Updated `README.md` with Phase 2 PDF Plan Importer architecture, storage layout, API endpoints, and test instructions.

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
