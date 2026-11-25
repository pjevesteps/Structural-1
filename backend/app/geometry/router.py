from math import hypot, sqrt
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import storage
from ..models import (
    AnalyticalMember,
    AnalyticalNode,
    GeometryGenerationOptions,
    ManualBeamOverride,
    StructuralModelState,
)
from .generator import generate_structural_model
from .validator import validate_structural_model

router = APIRouter(
    prefix="/api/projects/{project_id}/geometry",
    tags=["structural-geometry"],
)


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_node: int | None = Field(default=None, ge=1)
    end_node: int | None = Field(default=None, ge=1)
    member_type: Literal["column", "primary_beam", "secondary_beam", "tie_beam", "trimmer_beam"] = "primary_beam"
    floor_level: str = ""
    grid_ref: str = ""
    replicate_all_floors: bool = False
    # Optional intermediate node creation by splitting an existing member (for secondary/trimmer beams)
    split_start_member_id: int | None = Field(default=None, ge=1)
    split_start_ratio: float = Field(default=0.5, gt=0.05, lt=0.95)
    split_end_member_id: int | None = Field(default=None, ge=1)
    split_end_ratio: float = Field(default=0.5, gt=0.05, lt=0.95)


class SupportUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    support_type: Literal["NONE", "FIXED", "PINNED"]


def _load_or_404(project_id: str):
    try:
        return storage.load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _dist_3d(a: AnalyticalNode, b: AnalyticalNode) -> float:
    return round(sqrt((a.x_m - b.x_m) ** 2 + (a.y_m - b.y_m) ** 2 + (a.z_m - b.z_m) ** 2), 4)


def _xyz_close(p: list[float], q: list[float], xz_tol: float = 0.25, y_tol: float = 0.10) -> bool:
    return hypot(p[0] - q[0], p[2] - q[2]) <= xz_tol and abs(p[1] - q[1]) <= y_tol


def _override_matches_endpoints(
    ov: ManualBeamOverride,
    start_xyz: list[float],
    end_xyz: list[float],
    xz_tol: float = 0.25,
    y_tol: float = 0.10,
) -> bool:
    return (
        (_xyz_close(ov.start_xyz_m, start_xyz, xz_tol, y_tol) and _xyz_close(ov.end_xyz_m, end_xyz, xz_tol, y_tol))
        or (_xyz_close(ov.start_xyz_m, end_xyz, xz_tol, y_tol) and _xyz_close(ov.end_xyz_m, start_xyz, xz_tol, y_tol))
    )


def _record_manual_beam_override(
    model: StructuralModelState,
    na: AnalyticalNode,
    nb: AnalyticalNode,
    member_type: str,
    floor_level: str,
    grid_ref: str,
) -> None:
    if member_type == "column":
        return
    s_xyz = [round(na.x_m, 4), round(na.y_m, 4), round(na.z_m, 4)]
    e_xyz = [round(nb.x_m, 4), round(nb.y_m, 4), round(nb.z_m, 4)]
    if any(_override_matches_endpoints(ov, s_xyz, e_xyz) for ov in model.manual_beams):
        return
    model.manual_beams.append(
        ManualBeamOverride(
            override_id=f"mb-{uuid4().hex[:8]}",
            floor_level=floor_level,
            start_xyz_m=s_xyz,
            end_xyz_m=e_xyz,
            member_type=member_type,
            grid_ref=grid_ref or "MANUAL",
        )
    )


def _revalidate_preserving_info(model: StructuralModelState) -> None:
    prev_infos = [iss for iss in model.validation_issues if iss.severity == "info"]
    model.validation_issues = validate_structural_model(model) + prev_infos
    if any(iss.severity == "error" for iss in model.validation_issues):
        model.engineer_approved = False


def _split_member_for_node(model: StructuralModelState, member_id: int, ratio: float) -> int:
    node_by_id = {n.node_id: n for n in model.nodes}
    target = next((m for m in model.members if m.member_id == member_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found for split.")

    na = node_by_id[target.start_node]
    nb = node_by_id[target.end_node]
    nx = round(na.x_m + (nb.x_m - na.x_m) * ratio, 4)
    ny = round(na.y_m + (nb.y_m - na.y_m) * ratio, 4)
    nz = round(na.z_m + (nb.z_m - na.z_m) * ratio, 4)

    new_nid = max((n.node_id for n in model.nodes), default=0) + 1
    new_node = AnalyticalNode(
        node_id=new_nid,
        x_m=nx,
        y_m=ny,
        z_m=nz,
        floor_level=target.floor_level,
        grid_ref="",
        is_support=False,
        support_type="NONE",
    )
    model.nodes.append(new_node)

    # Split target member into two segments: na -> new_node and new_node -> nb
    orig_end = target.end_node
    target.end_node = new_nid
    target.length_m = _dist_3d(na, new_node)
    target.manual_override = True

    next_mid = max((m.member_id for m in model.members), default=0) + 1
    model.members.append(
        AnalyticalMember(
            member_id=next_mid,
            start_node=new_nid,
            end_node=orig_end,
            member_type=target.member_type,
            floor_level=target.floor_level,
            grid_ref=target.grid_ref,
            length_m=_dist_3d(new_node, nb),
            manual_override=True,
        )
    )
    return new_nid


@router.post("/generate", response_model=StructuralModelState)
def generate_geometry(
    project_id: str,
    options: GeometryGenerationOptions | None = None,
):
    project = _load_or_404(project_id)
    opts = options if options is not None else GeometryGenerationOptions()
    model = generate_structural_model(project, opts)
    project.structural_model = model
    storage.save_project(project)
    return model


@router.post("/validate", response_model=StructuralModelState)
def validate_geometry(project_id: str):
    project = _load_or_404(project_id)
    model = project.structural_model
    _revalidate_preserving_info(model)
    storage.save_project(project)
    return model


@router.post("/members", response_model=StructuralModelState)
def add_member(project_id: str, payload: AddMemberRequest):
    project = _load_or_404(project_id)
    model = project.structural_model

    start_nid = payload.start_node
    end_nid = payload.end_node

    if start_nid is None and payload.split_start_member_id is not None:
        start_nid = _split_member_for_node(model, payload.split_start_member_id, payload.split_start_ratio)
    if end_nid is None and payload.split_end_member_id is not None:
        end_nid = _split_member_for_node(model, payload.split_end_member_id, payload.split_end_ratio)

    if start_nid is None or end_nid is None:
        raise HTTPException(
            status_code=400,
            detail="Provide start_node and end_node (or split_start_member_id / split_end_member_id).",
        )
    if start_nid == end_nid:
        raise HTTPException(status_code=400, detail="Start and end nodes must differ.")

    node_by_id = {n.node_id: n for n in model.nodes}
    na = node_by_id.get(start_nid)
    nb = node_by_id.get(end_nid)
    if na is None or nb is None:
        raise HTTPException(status_code=404, detail="Start or end node not found in structural model.")

    pair = (min(start_nid, end_nid), max(start_nid, end_nid))
    if any((min(m.start_node, m.end_node), max(m.start_node, m.end_node)) == pair for m in model.members):
        raise HTTPException(status_code=400, detail=f"Member between N{start_nid} and N{end_nid} already exists.")

    length_m = _dist_3d(na, nb)
    if length_m < 0.05:
        raise HTTPException(status_code=400, detail="Member length must be at least 0.05 m.")

    next_mid = max((m.member_id for m in model.members), default=0) + 1
    fl = payload.floor_level.strip() or na.floor_level
    gref = payload.grid_ref.strip() or f"{na.grid_ref}-{nb.grid_ref}".strip("-") or "MANUAL"

    model.members.append(
        AnalyticalMember(
            member_id=next_mid,
            start_node=start_nid,
            end_node=end_nid,
            member_type=payload.member_type,
            floor_level=fl,
            grid_ref=gref,
            length_m=length_m,
            manual_override=True,
        )
    )
    _record_manual_beam_override(model, na, nb, payload.member_type, fl, gref)
    next_mid += 1

    # Optionally replicate the same (x, z) beam onto every other framing level with matching nodes
    if payload.replicate_all_floors and payload.member_type != "column" and na.floor_level == nb.floor_level:
        tol_xz = max(model.options.snap_tolerance_m, 0.25)
        nodes_by_floor: dict[str, list[AnalyticalNode]] = {}
        for n in model.nodes:
            nodes_by_floor.setdefault(n.floor_level, []).append(n)
        min_elev = min((n.y_m for n in model.nodes), default=0.0)
        lowest_floors = {n.floor_level for n in model.nodes if abs(n.y_m - min_elev) <= 0.05}
        existing_pairs = {(min(m.start_node, m.end_node), max(m.start_node, m.end_node)) for m in model.members}

        for other_floor, floor_nodes in nodes_by_floor.items():
            if other_floor == fl:
                continue
            if (
                other_floor in lowest_floors
                and fl not in lowest_floors
                and not model.options.include_base_tie_beams
            ):
                continue

            oa = min(
                (n for n in floor_nodes if hypot(n.x_m - na.x_m, n.z_m - na.z_m) <= tol_xz),
                key=lambda n: hypot(n.x_m - na.x_m, n.z_m - na.z_m),
                default=None,
            )
            ob = min(
                (n for n in floor_nodes if hypot(n.x_m - nb.x_m, n.z_m - nb.z_m) <= tol_xz),
                key=lambda n: hypot(n.x_m - nb.x_m, n.z_m - nb.z_m),
                default=None,
            )
            if oa is None or ob is None or oa.node_id == ob.node_id:
                continue
            opair = (min(oa.node_id, ob.node_id), max(oa.node_id, ob.node_id))
            if opair in existing_pairs:
                continue
            olen = _dist_3d(oa, ob)
            if olen < 0.05:
                continue

            ogref = payload.grid_ref.strip() or f"{oa.grid_ref}-{ob.grid_ref}".strip("-") or "MANUAL"
            model.members.append(
                AnalyticalMember(
                    member_id=next_mid,
                    start_node=oa.node_id,
                    end_node=ob.node_id,
                    member_type=payload.member_type,
                    floor_level=other_floor,
                    grid_ref=ogref,
                    length_m=olen,
                    manual_override=True,
                )
            )
            existing_pairs.add(opair)
            _record_manual_beam_override(model, oa, ob, payload.member_type, other_floor, ogref)
            next_mid += 1

    model.engineer_approved = False
    _revalidate_preserving_info(model)
    storage.save_project(project)
    return model


@router.delete("/members/{member_id}", response_model=StructuralModelState)
def delete_member(project_id: str, member_id: int):
    project = _load_or_404(project_id)
    model = project.structural_model
    target = next((m for m in model.members if m.member_id == member_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found.")

    if target.manual_override:
        node_by_id = {n.node_id: n for n in model.nodes}
        na = node_by_id.get(target.start_node)
        nb = node_by_id.get(target.end_node)
        if na is not None and nb is not None:
            s_xyz = [na.x_m, na.y_m, na.z_m]
            e_xyz = [nb.x_m, nb.y_m, nb.z_m]
            tol_xz = max(model.options.snap_tolerance_m, 0.25)
            model.manual_beams = [
                ov
                for ov in model.manual_beams
                if not _override_matches_endpoints(ov, s_xyz, e_xyz, xz_tol=tol_xz, y_tol=0.10)
            ]

    model.members = [m for m in model.members if m.member_id != member_id]

    model.engineer_approved = False
    _revalidate_preserving_info(model)
    storage.save_project(project)
    return model


@router.patch("/nodes/{node_id}/support", response_model=StructuralModelState)
def update_node_support(project_id: str, node_id: int, payload: SupportUpdateRequest):
    project = _load_or_404(project_id)
    model = project.structural_model
    target = next((n for n in model.nodes if n.node_id == node_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found.")

    target.support_type = payload.support_type
    target.is_support = payload.support_type != "NONE"
    model.engineer_approved = False
    _revalidate_preserving_info(model)
    storage.save_project(project)
    return model


@router.post("/approve", response_model=StructuralModelState)
def approve_geometry(project_id: str):
    project = _load_or_404(project_id)
    model = project.structural_model
    _revalidate_preserving_info(model)

    errors = [iss for iss in model.validation_issues if iss.severity == "error"]
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve geometry while {len(errors)} error(s) remain: {errors[0].message}",
        )

    model.engineer_approved = True
    storage.save_project(project)
    return model
