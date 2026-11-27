from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .. import storage
from ..models import ProjectState
from ..profile import EngineerProfile, load_engineer_profile, resolve_signatory


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_project_report_dir(project_id: str) -> Path:
    """Per-user isolated artifact directory under get_active_data_dir()/exports/reports/<project_id>/."""
    safe_pid = storage.validate_project_id(project_id)
    base = (storage.get_active_data_dir() / "exports" / "reports").resolve()
    target = (base / safe_pid).resolve()
    if not target.is_relative_to(base):
        raise ValueError("Report artifact directory escapes active data root.")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _metadata_path(project_id: str) -> Path:
    return get_project_report_dir(project_id) / "metadata.json"


def load_deliverables_metadata(project_id: str) -> dict[str, Any]:
    path = _metadata_path(project_id)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def save_deliverables_metadata(project_id: str, meta: dict[str, Any]) -> None:
    path = _metadata_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")


def compute_report_fingerprint(
    project: ProjectState,
    profile: EngineerProfile | None = None,
) -> str:
    """
    Deterministic SHA-256 fingerprint for the Structural Analysis & Design Report.
    Hashes geometry, member sizing, load/analysis/design outputs, report options, and
    resolved signatory/EngineerProfile fields. Intentionally excludes BOM rate settings
    so BOM edits/re-runs never invalidate an existing Structural Report.
    """
    sig = resolve_signatory(project.reporting.signatory, profile)
    sm = project.structural_model
    ms = project.member_sizing
    lg = project.load_generation
    ad = project.analysis_design
    payload = {
        "project_info": project.project_info.model_dump(),
        "levels": [lv.model_dump() for lv in project.levels],
        "soil_investigation": project.soil_investigation.model_dump(),
        "foundation_system": project.foundation_system,
        "structural_model": {
            "options": sm.options.model_dump(),
            "nodes": [n.model_dump() for n in sm.nodes],
            "members": [m.model_dump() for m in sm.members],
            "slab_panels": [p.model_dump() for p in sm.slab_panels],
        },
        "member_sizing": {
            "material": ms.material.model_dump(),
            "options": ms.options.model_dump(),
            "sections": [s.model_dump() for s in ms.sections],
            "member_assignments": [a.model_dump() for a in ms.member_assignments],
            "slab_assignments": [s.model_dump() for s in ms.slab_assignments],
        },
        "load_generation": {
            "floor_loads": [fl.model_dump() for fl in lg.floor_loads],
            "seismic": lg.seismic.model_dump(),
            "seismic_summary": lg.seismic_summary.model_dump() if lg.seismic_summary else None,
            "wind": lg.wind.model_dump(),
            "wind_summary": lg.wind_summary.model_dump() if lg.wind_summary else None,
            "combinations": [c.model_dump() for c in lg.combinations],
        },
        "analysis_design": {
            "source": ad.source,
            "source_filename": ad.source_filename,
            "params": ad.params.model_dump(),
            "story_drifts": [d.model_dump() for d in ad.story_drifts],
            "support_reactions": [r.model_dump() for r in ad.support_reactions],
            "beam_designs": [b.model_dump() for b in ad.beam_designs],
            "column_designs": [c.model_dump() for c in ad.column_designs],
            "footing_designs": [f.model_dump() for f in ad.footing_designs],
            "combined_footing_designs": [c.model_dump() for c in ad.combined_footing_designs],
            "mat_foundation_design": ad.mat_foundation_design.model_dump() if ad.mat_foundation_design else None,
            "smrf_joint_checks": [j.model_dump() for j in ad.smrf_joint_checks],
            "retaining_wall_designs": [r.model_dump() for r in ad.retaining_wall_designs],
            "slab_designs": [s.model_dump() for s in ad.slab_designs],
            "stair_designs": [s.model_dump() for s in ad.stair_designs],
            "strap_footing_designs": [s.model_dump() for s in ad.strap_footing_designs],
            "roof_truss_designs": [t.model_dump() for t in ad.roof_truss_designs],
            "pinned_design_views": ad.pinned_design_views,
        },
        "report_options": project.reporting.options.model_dump(),
        "signatory": sig,
    }
    return _canonical_hash(payload)


def compute_bom_fingerprint(
    project: ProjectState,
    profile: EngineerProfile | None = None,
) -> str:
    """
    Deterministic SHA-256 fingerprint for the Bill of Materials & Cost Estimate.
    Hashes sizing/takeoff geometry, RC design outputs used in rebar/footing takeoff,
    BOM unit-rate parameters, and resolved signatory/EngineerProfile fields.
    """
    sig = resolve_signatory(project.reporting.signatory, profile)
    sm = project.structural_model
    ms = project.member_sizing
    ad = project.analysis_design
    payload = {
        "project_info": project.project_info.model_dump(),
        "levels": [lv.model_dump() for lv in project.levels],
        "structural_model": {
            "members": [m.model_dump() for m in sm.members],
            "slab_panels": [p.model_dump() for p in sm.slab_panels],
        },
        "member_sizing": {
            "material": ms.material.model_dump(),
            "sections": [s.model_dump() for s in ms.sections],
            "member_assignments": [a.model_dump() for a in ms.member_assignments],
            "slab_assignments": [s.model_dump() for s in ms.slab_assignments],
        },
        "analysis_design": {
            "params": ad.params.model_dump(),
            "beam_designs": [b.model_dump() for b in ad.beam_designs],
            "column_designs": [c.model_dump() for c in ad.column_designs],
            "footing_designs": [f.model_dump() for f in ad.footing_designs],
            "combined_footing_designs": [c.model_dump() for c in ad.combined_footing_designs],
            "mat_foundation_design": ad.mat_foundation_design.model_dump() if ad.mat_foundation_design else None,
            "retaining_wall_inputs": [r.model_dump() for r in ad.retaining_wall_inputs],
            "stair_inputs": [s.model_dump() for s in ad.stair_inputs],
            "strap_footing_designs": [s.model_dump() for s in ad.strap_footing_designs],
            "roof_truss_designs": [t.model_dump() for t in ad.roof_truss_designs],
        },
        "bom_params": project.reporting.bom_params.model_dump(),
        "signatory": sig,
    }
    return _canonical_hash(payload)


def check_report_prerequisites(project: ProjectState) -> list[str]:
    missing: list[str] = []
    if not project.structural_model.nodes or not project.structural_model.members:
        missing.append("Module 03 (3D Structural Geometry) has not been generated yet.")
    if not project.member_sizing.member_assignments:
        missing.append("Module 04 (Member Sizing) has not been completed yet.")
    if (
        not project.analysis_design.generated_at
        and not project.analysis_design.beam_designs
        and not project.analysis_design.column_designs
    ):
        missing.append("Module 06 (Analysis & RC Design) has not been run yet.")
    return missing


def check_bom_prerequisites(project: ProjectState) -> list[str]:
    missing: list[str] = []
    if not project.structural_model.members:
        missing.append("Module 03 (3D Structural Geometry) has not been generated yet.")
    if not project.member_sizing.member_assignments:
        missing.append("Module 04 (Member Sizing) has not been completed yet.")
    if (
        not project.analysis_design.generated_at
        and not project.analysis_design.beam_designs
        and not project.analysis_design.column_designs
    ):
        missing.append("Module 06 (Analysis & RC Design) is required for rebar and footing takeoff.")
    return missing


def report_artifact_paths(project_id: str) -> tuple[Path, Path]:
    safe_pid = storage.validate_project_id(project_id)
    d = get_project_report_dir(safe_pid)
    return (
        d / f"{safe_pid}_Structural_Calculation_Report.docx",
        d / f"{safe_pid}_Structural_Calculation_Report.pdf",
    )


def bom_artifact_path(project_id: str) -> Path:
    safe_pid = storage.validate_project_id(project_id)
    d = get_project_report_dir(safe_pid)
    return d / f"{safe_pid}_Structural_BOM.csv"


def persist_structural_report_artifacts(
    project: ProjectState,
    docx_bytes: bytes,
    pdf_bytes: bytes,
    profile: EngineerProfile | None = None,
) -> dict[str, Any]:
    """Write DOCX & PDF artifacts and update ONLY structural_report metadata (never touches BOM)."""
    pid = project.project_info.project_id
    docx_path, pdf_path = report_artifact_paths(pid)
    docx_path.write_bytes(docx_bytes)
    pdf_path.write_bytes(pdf_bytes)

    meta = load_deliverables_metadata(pid)
    now = _utc_now()
    fp = compute_report_fingerprint(project, profile)
    meta["structural_report"] = {
        "generated_at": now,
        "fingerprint": fp,
        "docx_filename": docx_path.name,
        "pdf_filename": pdf_path.name,
        "docx_size_bytes": len(docx_bytes),
        "pdf_size_bytes": len(pdf_bytes),
    }
    save_deliverables_metadata(pid, meta)
    return meta["structural_report"]


def persist_bom_artifact(
    project: ProjectState,
    csv_text: str,
    profile: EngineerProfile | None = None,
) -> dict[str, Any]:
    """Write BOM CSV artifact and update ONLY bom metadata (never touches Structural Report)."""
    pid = project.project_info.project_id
    csv_path = bom_artifact_path(pid)
    csv_bytes = csv_text.encode("utf-8")
    csv_path.write_bytes(csv_bytes)

    meta = load_deliverables_metadata(pid)
    now = _utc_now()
    fp = compute_bom_fingerprint(project, profile)
    meta["bom"] = {
        "generated_at": now,
        "fingerprint": fp,
        "csv_filename": csv_path.name,
        "csv_size_bytes": len(csv_bytes),
    }
    save_deliverables_metadata(pid, meta)
    return meta["bom"]


def get_deliverables_status(
    project: ProjectState,
    profile: EngineerProfile | None = None,
) -> dict[str, Any]:
    """
    Compute independent deliverable status ('missing' | 'ready' | 'stale'), timestamps,
    fingerprints, prerequisites, and artifact descriptors for Structural Report and BOM.
    """
    if profile is None:
        profile = load_engineer_profile()
    pid = project.project_info.project_id
    meta = load_deliverables_metadata(pid)

    # 1. Structural Report status
    sr_meta = meta.get("structural_report") or {}
    docx_path, pdf_path = report_artifact_paths(pid)
    sr_files_ok = docx_path.is_file() and pdf_path.is_file()
    sr_cur_fp = compute_report_fingerprint(project, profile)
    sr_missing_prereqs = check_report_prerequisites(project)

    if not sr_meta or not sr_files_ok:
        sr_status = "missing"
    elif sr_meta.get("fingerprint") == sr_cur_fp:
        sr_status = "ready"
    else:
        sr_status = "stale"

    # 2. BOM status
    bom_meta = meta.get("bom") or {}
    csv_path = bom_artifact_path(pid)
    bom_file_ok = csv_path.is_file()
    bom_cur_fp = compute_bom_fingerprint(project, profile)
    bom_missing_prereqs = check_bom_prerequisites(project)

    if not bom_meta or not bom_file_ok or project.reporting.bom is None:
        bom_status = "missing"
    elif bom_meta.get("fingerprint") == bom_cur_fp:
        bom_status = "ready"
    else:
        bom_status = "stale"

    return {
        "project_id": pid,
        "structural_report": {
            "status": sr_status,
            "generated_at": sr_meta.get("generated_at") if sr_files_ok else None,
            "fingerprint": sr_meta.get("fingerprint") if sr_files_ok else None,
            "current_fingerprint": sr_cur_fp,
            "prerequisites_met": len(sr_missing_prereqs) == 0,
            "missing_prerequisites": sr_missing_prereqs,
            "artifacts": {
                "docx": {
                    "exists": docx_path.is_file(),
                    "filename": docx_path.name,
                    "size_bytes": docx_path.stat().st_size if docx_path.is_file() else 0,
                    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                },
                "pdf": {
                    "exists": pdf_path.is_file(),
                    "filename": pdf_path.name,
                    "size_bytes": pdf_path.stat().st_size if pdf_path.is_file() else 0,
                    "media_type": "application/pdf",
                },
            },
        },
        "bom": {
            "status": bom_status,
            "generated_at": bom_meta.get("generated_at") if (bom_file_ok and project.reporting.bom is not None) else None,
            "fingerprint": bom_meta.get("fingerprint") if (bom_file_ok and project.reporting.bom is not None) else None,
            "current_fingerprint": bom_cur_fp,
            "prerequisites_met": len(bom_missing_prereqs) == 0,
            "missing_prerequisites": bom_missing_prereqs,
            "artifacts": {
                "csv": {
                    "exists": csv_path.is_file(),
                    "filename": csv_path.name,
                    "size_bytes": csv_path.stat().st_size if csv_path.is_file() else 0,
                    "media_type": "text/csv; charset=utf-8",
                },
            },
        },
    }
