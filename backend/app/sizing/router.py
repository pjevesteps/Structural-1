from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import storage
from ..models import (
    MaterialProperties,
    MemberPropertyAssignment,
    MemberSizingState,
    SectionDefinition,
    SizingOptions,
)
from .engine import auto_size_members, compute_ec_kn_m2
from .validator import validate_member_sizing

router = APIRouter(
    prefix="/api/projects/{project_id}/sizing",
    tags=["member-sizing"],
)


class AutoSizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material: MaterialProperties | None = None
    options: SizingOptions | None = None
    preserve_manual_overrides: bool = False


class MaterialAndOptionsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material: MaterialProperties | None = None
    options: SizingOptions | None = None
    # Convenience flat fields for quick material updates
    fc_mpa: float | None = Field(default=None, gt=0)
    fy_main_mpa: float | None = Field(default=None, gt=0)
    fy_tie_mpa: float | None = Field(default=None, gt=0)
    poisson: float | None = Field(default=None, gt=0, lt=0.5)
    density_kn_m3: float | None = Field(default=None, gt=0)


class BulkAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_ids: list[int] = Field(min_length=1)
    section_id: str = Field(min_length=1)
    beta_angle_deg: float | None = None
    release_start_moment: bool | None = None
    release_end_moment: bool | None = None


class SlabUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thickness_mm: float | None = Field(default=None, ge=0.0)
    slab_tag: str | None = None


def _load_or_404(project_id: str):
    try:
        return storage.load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _revalidate_and_save(project) -> MemberSizingState:
    project.member_sizing.validation_issues = validate_member_sizing(project)
    if any(iss.severity == "error" for iss in project.member_sizing.validation_issues):
        project.member_sizing.engineer_approved = False
    storage.save_project(project)
    return project.member_sizing


@router.post("/auto-size", response_model=MemberSizingState)
def run_auto_size(project_id: str, payload: AutoSizeRequest | None = None):
    project = _load_or_404(project_id)
    req = payload if payload is not None else AutoSizeRequest()
    try:
        sizing = auto_size_members(
            project,
            material=req.material,
            options=req.options,
            preserve_manual_overrides=req.preserve_manual_overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    project.member_sizing = sizing
    storage.save_project(project)
    return sizing


@router.patch("/material", response_model=MemberSizingState)
def update_material_and_options(project_id: str, payload: MaterialAndOptionsUpdateRequest):
    project = _load_or_404(project_id)
    sizing = project.member_sizing

    if payload.material is not None:
        mat = payload.material.model_copy(deep=True)
        mat.ec_kn_m2 = compute_ec_kn_m2(mat.fc_mpa)
        sizing.material = mat
    else:
        mat = sizing.material
        if payload.fc_mpa is not None:
            mat.fc_mpa = payload.fc_mpa
            mat.ec_kn_m2 = compute_ec_kn_m2(payload.fc_mpa)
        if payload.fy_main_mpa is not None:
            mat.fy_main_mpa = payload.fy_main_mpa
        if payload.fy_tie_mpa is not None:
            mat.fy_tie_mpa = payload.fy_tie_mpa
        if payload.poisson is not None:
            mat.poisson = payload.poisson
        if payload.density_kn_m3 is not None:
            mat.density_kn_m3 = payload.density_kn_m3

    if payload.options is not None:
        sizing.options = payload.options.model_copy(deep=True)

    sizing.engineer_approved = False
    return _revalidate_and_save(project)


@router.post("/sections", response_model=MemberSizingState)
def upsert_section(project_id: str, payload: SectionDefinition):
    project = _load_or_404(project_id)
    sizing = project.member_sizing

    replaced = False
    for idx, existing in enumerate(sizing.sections):
        if existing.section_id == payload.section_id:
            sizing.sections[idx] = payload
            replaced = True
            break
    if not replaced:
        sizing.sections.append(payload)

    sizing.engineer_approved = False
    return _revalidate_and_save(project)


@router.delete("/sections/{section_id}", response_model=MemberSizingState)
def delete_section(project_id: str, section_id: str):
    project = _load_or_404(project_id)
    sizing = project.member_sizing

    if not any(s.section_id == section_id for s in sizing.sections):
        raise HTTPException(status_code=404, detail=f"Section '{section_id}' not found.")

    assigned_mids = [a.member_id for a in sizing.member_assignments if a.section_id == section_id]
    if assigned_mids:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot delete section '{section_id}': still assigned to {len(assigned_mids)} "
                "member(s). Reassign those members first."
            ),
        )

    sizing.sections = [s for s in sizing.sections if s.section_id != section_id]
    sizing.engineer_approved = False
    return _revalidate_and_save(project)


@router.post("/assign", response_model=MemberSizingState)
def bulk_assign_members(project_id: str, payload: BulkAssignRequest):
    project = _load_or_404(project_id)
    sizing = project.member_sizing

    if not any(s.section_id == payload.section_id for s in sizing.sections):
        raise HTTPException(status_code=404, detail=f"Section '{payload.section_id}' not found.")

    valid_mids = {m.member_id for m in project.structural_model.members}
    unknown = [mid for mid in payload.member_ids if mid not in valid_mids]
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=f"Member ID(s) not found in structural model: {unknown}",
        )

    assign_by_mid = {a.member_id: a for a in sizing.member_assignments}
    for mid in payload.member_ids:
        existing = assign_by_mid.get(mid)
        if existing is None:
            new_asgn = MemberPropertyAssignment(
                member_id=mid,
                section_id=payload.section_id,
                beta_angle_deg=payload.beta_angle_deg if payload.beta_angle_deg is not None else 0.0,
                release_start_moment=bool(payload.release_start_moment),
                release_end_moment=bool(payload.release_end_moment),
                manual_override=True,
            )
            sizing.member_assignments.append(new_asgn)
            assign_by_mid[mid] = new_asgn
        else:
            existing.section_id = payload.section_id
            if payload.beta_angle_deg is not None:
                existing.beta_angle_deg = payload.beta_angle_deg
            if payload.release_start_moment is not None:
                existing.release_start_moment = payload.release_start_moment
            if payload.release_end_moment is not None:
                existing.release_end_moment = payload.release_end_moment
            existing.manual_override = True

    sizing.member_assignments.sort(key=lambda a: a.member_id)
    sizing.engineer_approved = False
    return _revalidate_and_save(project)


@router.patch("/slabs/{panel_id}", response_model=MemberSizingState)
def update_slab_assignment(project_id: str, panel_id: str, payload: SlabUpdateRequest):
    project = _load_or_404(project_id)
    sizing = project.member_sizing
    target = next((s for s in sizing.slab_assignments if s.panel_id == panel_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Slab assignment '{panel_id}' not found.")

    if payload.thickness_mm is not None:
        target.thickness_mm = payload.thickness_mm
    if payload.slab_tag is not None and payload.slab_tag.strip():
        target.slab_tag = payload.slab_tag.strip()
    target.manual_override = True

    sizing.engineer_approved = False
    return _revalidate_and_save(project)


@router.post("/approve", response_model=MemberSizingState)
def approve_sizing(project_id: str):
    project = _load_or_404(project_id)
    sizing = project.member_sizing
    sizing.validation_issues = validate_member_sizing(project)

    errors = [iss for iss in sizing.validation_issues if iss.severity == "error"]
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve member sizing while {len(errors)} error(s) remain: {errors[0].message}",
        )

    sizing.engineer_approved = True
    storage.save_project(project)
    return sizing
