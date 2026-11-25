from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from ..loads.engine import generate_loads
from ..models import AnalysisDesignState, ProjectState
from ..sizing.validator import validate_member_sizing
from .rc_design import run_analysis_and_design


class AutoResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_iterations: int = Field(default=6, ge=1, le=15)
    upsize_beams: bool = True
    upsize_columns: bool = True
    upsize_footings: bool = True
    control_story_drift: bool = True
    respect_locked_sections: bool = True


class AutoResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    converged: bool
    iterations_run: int
    history: list[dict] = Field(default_factory=list)
    analysis_design: AnalysisDesignState


ExtraCheckHook = Callable[[ProjectState, AnalysisDesignState], dict]


def _locked_section_ids(project: ProjectState) -> set[str]:
    """Return section IDs where ALL assigned members are explicitly marked manual_override=True."""
    asgns = project.member_sizing.member_assignments
    by_sec: dict[str, list[bool]] = {}
    for a in asgns:
        by_sec.setdefault(a.section_id, []).append(a.manual_override)
    return {sid for sid, flags in by_sec.items() if flags and all(flags)}


def run_auto_resolve(
    project: ProjectState,
    req: AutoResolveRequest,
    extra_check_hooks: list[ExtraCheckHook] | None = None,
) -> AutoResolveResponse:
    """
    Iteratively runs Analysis & RC Design, upsizes failing beam/column/drift/footing sections
    in +50 mm increments, refreshes Phase 5 gravity self-weight & seismic weights, and re-runs
    Phase 6 until 0 failures remain or `max_iterations` is reached.

    Modular `extra_check_hooks` allow Sprint C checks (e.g., SMRF SCWB) to flag additional
    section IDs for upsizing without modifying this loop.
    """
    hooks = extra_check_hooks or []
    history: list[dict] = []
    converged = False
    sec_by_id = {s.section_id: s for s in project.member_sizing.sections}
    member_by_id = {m.member_id: m for m in project.structural_model.members}
    asgn_by_mid = {a.member_id: a for a in project.member_sizing.member_assignments}
    locked_secs = _locked_section_ids(project) if req.respect_locked_sections else set()

    for iteration in range(1, req.max_iterations + 1):
        ad_state, _ = run_analysis_and_design(project)
        project.analysis_design = ad_state

        fail_beams = [b for b in ad_state.beam_designs if b.status == "FAIL"]
        fail_cols = [c for c in ad_state.column_designs if c.status == "FAIL"]
        fail_ftgs = [f for f in ad_state.footing_designs if f.status == "FAIL"]
        fail_drifts = [d for d in ad_state.story_drifts if d.status == "FAIL"]

        extra_beam_secs: set[str] = set()
        extra_col_secs: set[str] = set()
        extra_fail_cnt = 0
        hook_notes: list[str] = []
        for hook in hooks:
            res = hook(project, ad_state) or {}
            extra_beam_secs.update(res.get("fail_beam_sections") or [])
            extra_col_secs.update(res.get("fail_col_sections") or [])
            extra_fail_cnt += int(res.get("extra_fail_count") or 0)
            hook_notes.extend(res.get("notes") or [])

        total_fails = (
            len(fail_beams) + len(fail_cols) + len(fail_ftgs) + len(fail_drifts) + extra_fail_cnt
        )
        max_b_dcr = max((b.governing_dcr for b in ad_state.beam_designs), default=0.0)
        max_c_dcr = max((c.governing_dcr for c in ad_state.column_designs), default=0.0)
        max_dr = max(
            (max(d.drift_ratio_x, d.drift_ratio_z) for d in ad_state.story_drifts),
            default=0.0,
        )

        iter_entry = {
            "iteration": iteration,
            "fail_beams": len(fail_beams),
            "fail_columns": len(fail_cols),
            "fail_footings": len(fail_ftgs),
            "fail_drifts": len(fail_drifts),
            "extra_failures": extra_fail_cnt,
            "max_beam_dcr": round(max_b_dcr, 3),
            "max_col_dcr": round(max_c_dcr, 3),
            "max_drift_ratio": round(max_dr, 3),
            "actions": list(hook_notes),
        }
        history.append(iter_entry)

        if total_fails == 0:
            converged = True
            break

        if iteration >= req.max_iterations:
            break

        beam_secs_to_bump: set[str] = set(extra_beam_secs)
        col_secs_to_bump: set[str] = set(extra_col_secs)
        shear_beam_secs: set[str] = set()

        if req.upsize_beams:
            for b in fail_beams:
                beam_secs_to_bump.add(b.section_id)
                if b.shear_dcr > 1.0:
                    shear_beam_secs.add(b.section_id)

        if req.upsize_columns:
            for c in fail_cols:
                col_secs_to_bump.add(c.section_id)

        if req.control_story_drift and fail_drifts:
            # Stiffen columns and beams at failing stories (and supporting stories below)
            fail_elevs = {d.elevation_m for d in fail_drifts}
            max_fail_elev = max(fail_elevs)
            lvl_elev = {lv.id: lv.elevation_m for lv in project.levels}
            for c in ad_state.column_designs:
                if lvl_elev.get(c.floor_level, 0.0) <= max_fail_elev + 0.25:
                    col_secs_to_bump.add(c.section_id)
            for b in ad_state.beam_designs:
                if lvl_elev.get(b.floor_level, 0.0) in fail_elevs or any(
                    abs(lvl_elev.get(b.floor_level, -999.0) - fe) <= 0.25 for fe in fail_elevs
                ):
                    beam_secs_to_bump.add(b.section_id)

        if req.upsize_footings and fail_ftgs:
            # Upsize column/pedestal sections landing on failing footings to increase punching perimeter bo
            fail_nids = {f.node_id for f in fail_ftgs}
            for m in project.structural_model.members:
                if m.member_type == "column" and (m.start_node in fail_nids or m.end_node in fail_nids):
                    asgn = asgn_by_mid.get(m.member_id)
                    if asgn:
                        col_secs_to_bump.add(asgn.section_id)

        resized_any = False
        for sid in sorted(beam_secs_to_bump):
            if sid in locked_secs or sid not in sec_by_id:
                continue
            sec = sec_by_id[sid]
            old_w, old_h = sec.width_mm, sec.depth_mm
            sec.depth_mm = min(1500.0, sec.depth_mm + 50.0)
            if sid in shear_beam_secs or (sec.depth_mm / max(sec.width_mm, 1.0) > 2.2):
                sec.width_mm = min(900.0, sec.width_mm + 50.0)
            if sec.width_mm != old_w or sec.depth_mm != old_h:
                sec.label = f"{sec.section_id} ({sec.width_mm:.0f}x{sec.depth_mm:.0f})"
                iter_entry["actions"].append(
                    f"Upsized beam {sid}: {old_w:.0f}x{old_h:.0f} -> {sec.width_mm:.0f}x{sec.depth_mm:.0f} mm"
                )
                resized_any = True

        for sid in sorted(col_secs_to_bump):
            if sid in locked_secs or sid not in sec_by_id:
                continue
            sec = sec_by_id[sid]
            old_w, old_h = sec.width_mm, sec.depth_mm
            sec.width_mm = min(1500.0, sec.width_mm + 50.0)
            sec.depth_mm = min(1500.0, sec.depth_mm + 50.0)
            if sec.width_mm != old_w or sec.depth_mm != old_h:
                sec.label = f"{sec.section_id} ({sec.width_mm:.0f}x{sec.depth_mm:.0f})"
                iter_entry["actions"].append(
                    f"Upsized column {sid}: {old_w:.0f}x{old_h:.0f} -> {sec.width_mm:.0f}x{sec.depth_mm:.0f} mm"
                )
                resized_any = True

        if not resized_any:
            iter_entry["actions"].append("No eligible unlocked sections left to upsize.")
            break

        # Re-validate Phase 4 sizing & refresh Phase 5 gravity self-weight & seismic weights
        project.member_sizing.validation_issues = validate_member_sizing(project)
        project.load_generation = generate_loads(project)

    return AutoResolveResponse(
        converged=converged,
        iterations_run=len(history),
        history=history,
        analysis_design=project.analysis_design,
    )
