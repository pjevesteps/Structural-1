from math import hypot, sqrt
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import storage
from ..models import (
    AnalyticalMember,
    AnalyticalNode,
    GeometryGenerationOptions,
    ManualBeamOverride,
    StructuralModelState,
)
from .generator import _is_roof_level_id, generate_structural_model, is_perimeter_beam_pair
from .validator import validate_structural_model


def _ensure_all_members_sized(project) -> bool:
    from ..sizing.engine import ensure_all_members_sized

    return ensure_all_members_sized(project)

router = APIRouter(
    prefix="/api/projects/{project_id}/geometry",
    tags=["structural-geometry"],
)


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_node: int | None = Field(default=None, ge=1)
    end_node: int | None = Field(default=None, ge=1)
    end_xyz_m: list[float] | None = Field(default=None, min_length=3, max_length=3)
    member_type: Literal[
        "column",
        "primary_beam",
        "secondary_beam",
        "tie_beam",
        "trimmer_beam",
        "cantilever_beam",
        "edge_beam",
    ] = "primary_beam"
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


class DeleteDuplicateMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary_member_id: int | None = Field(default=None, ge=1)
    duplicate_member_id: int = Field(ge=1)


class MergeDuplicateNodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary_node_id: int = Field(ge=1)
    duplicate_node_id: int = Field(ge=1)


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


def _split_member_for_node(
    model: StructuralModelState,
    member_id: int,
    ratio: float,
    project=None,
) -> int:
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
    if project is not None and project.member_sizing.member_assignments:
        existing_asgn = next(
            (a for a in project.member_sizing.member_assignments if a.member_id == target.member_id),
            None,
        )
        if existing_asgn is not None and not any(
            a.member_id == next_mid for a in project.member_sizing.member_assignments
        ):
            project.member_sizing.member_assignments.append(
                existing_asgn.model_copy(update={"member_id": next_mid})
            )
    return new_nid


def _find_or_split_host_beam_for_xyz(
    model: StructuralModelState,
    tx: float,
    ty: float,
    tz: float,
    floor_level: str,
    project=None,
) -> int | None:
    """
    Search same-floor non-column beams for the closest XZ projection of (tx, ty, tz).
    If within tol and 0.03 < t < 0.97, splits the host beam and returns the new T-junction node_id.
    If near either end (t <= 0.03 or t >= 0.97), reuses that endpoint node_id.
    """
    node_by_id = {n.node_id: n for n in model.nodes}
    host_tol = max(model.options.snap_tolerance_m, 0.15)
    best_hit: tuple[float, int, float, int, int] | None = None
    for m in model.members:
        if m.member_type == "column":
            continue
        u = node_by_id.get(m.start_node)
        v = node_by_id.get(m.end_node)
        if u is None or v is None:
            continue
        if abs(u.y_m - ty) > 0.10 or abs(v.y_m - ty) > 0.10:
            continue
        if floor_level and m.floor_level and m.floor_level != floor_level:
            continue
        dx = v.x_m - u.x_m
        dz = v.z_m - u.z_m
        len_sq = dx * dx + dz * dz
        if len_sq < 1e-6:
            continue
        t_raw = ((tx - u.x_m) * dx + (tz - u.z_m) * dz) / len_sq
        t_clamped = max(0.0, min(1.0, t_raw))
        proj_x = u.x_m + t_clamped * dx
        proj_z = u.z_m + t_clamped * dz
        perp_dist = hypot(tx - proj_x, tz - proj_z)
        if perp_dist <= host_tol:
            if best_hit is None or perp_dist < best_hit[0]:
                best_hit = (perp_dist, m.member_id, t_clamped, u.node_id, v.node_id)

    if best_hit is None:
        return None
    _, host_mid, t_val, u_nid, v_nid = best_hit
    if t_val <= 0.03:
        return u_nid
    if t_val >= 0.97:
        return v_nid
    return _split_member_for_node(model, host_mid, t_val, project=project)


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
        start_nid = _split_member_for_node(
            model, payload.split_start_member_id, payload.split_start_ratio, project=project
        )
    if end_nid is None and payload.split_end_member_id is not None:
        end_nid = _split_member_for_node(
            model, payload.split_end_member_id, payload.split_end_ratio, project=project
        )

    # Support T-junction host-beam splitting OR cantilever free-end beam creation from start_node to end_xyz_m
    if start_nid is not None and end_nid is None and payload.end_xyz_m is not None:
        anchor = next((n for n in model.nodes if n.node_id == start_nid), None)
        if anchor is None:
            raise HTTPException(status_code=404, detail="Start node not found in structural model.")
        tx, ty, tz = payload.end_xyz_m
        snap_tol = max(model.options.snap_tolerance_m, 0.05)
        existing_tip = next(
            (
                n
                for n in model.nodes
                if n.floor_level == anchor.floor_level
                and hypot(n.x_m - tx, n.z_m - tz) <= snap_tol
            ),
            None,
        )
        if existing_tip is not None:
            end_nid = existing_tip.node_id
        elif payload.member_type != "cantilever_beam":
            end_nid = _find_or_split_host_beam_for_xyz(
                model, tx, anchor.y_m, tz, anchor.floor_level, project=project
            )
            if end_nid is None:
                raise HTTPException(
                    status_code=400,
                    detail="Target end_xyz_m does not match an existing node or host beam on this floor.",
                )
        else:
            overhang = hypot(tx - anchor.x_m, tz - anchor.z_m)
            if overhang < 0.05 or overhang > 6.0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cantilever overhang length ({overhang:.2f} m) must be between 0.05 m and 6.0 m.",
                )
            new_nid = max((n.node_id for n in model.nodes), default=0) + 1
            model.nodes.append(
                AnalyticalNode(
                    node_id=new_nid,
                    x_m=round(tx, 4),
                    y_m=round(anchor.y_m, 4),
                    z_m=round(tz, 4),
                    floor_level=anchor.floor_level,
                    grid_ref="",
                    is_support=False,
                    support_type="NONE",
                )
            )
            end_nid = new_nid

    if start_nid is None or end_nid is None:
        raise HTTPException(
            status_code=400,
            detail="Provide start_node and end_node (or end_xyz_m / split_start_member_id / split_end_member_id).",
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
            if oa is not None and ob is None and payload.member_type == "cantilever_beam":
                new_nid = max((n.node_id for n in model.nodes), default=0) + 1
                ob = AnalyticalNode(
                    node_id=new_nid,
                    x_m=round(oa.x_m + (nb.x_m - na.x_m), 4),
                    y_m=round(oa.y_m, 4),
                    z_m=round(oa.z_m + (nb.z_m - na.z_m), 4),
                    floor_level=other_floor,
                    grid_ref="",
                    is_support=False,
                    support_type="NONE",
                )
                model.nodes.append(ob)
                floor_nodes.append(ob)
            elif oa is not None and ob is None:
                split_nid = _find_or_split_host_beam_for_xyz(
                    model,
                    oa.x_m + (nb.x_m - na.x_m),
                    oa.y_m,
                    oa.z_m + (nb.z_m - na.z_m),
                    other_floor,
                    project=project,
                )
                if split_nid is not None:
                    ob = next((n for n in model.nodes if n.node_id == split_nid), None)
                    if ob is not None:
                        floor_nodes.append(ob)
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
    if project.member_sizing.generated_at is not None or project.member_sizing.sections:
        _ensure_all_members_sized(project)
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
    if project.member_sizing.generated_at is not None or project.member_sizing.sections:
        _ensure_all_members_sized(project)
    storage.save_project(project)
    return model


@router.post("/clear-interior-roof-beams", response_model=StructuralModelState)
def clear_interior_roof_beams(
    project_id: str,
    floor_level: str | None = Query(None),
):
    """
    Strip non-perimeter (interior) beams on the roof level while preserving all
    outer perimeter tie-beams, columns, and analytical nodes.
    """
    project = _load_or_404(project_id)
    model = project.structural_model
    if not model.nodes:
        raise HTTPException(status_code=400, detail="Generate the 3D frame first.")

    sorted_levels = sorted(project.levels, key=lambda lv: lv.elevation_m)
    target_level = floor_level
    if not target_level:
        roof_lvl = next((lv.id for lv in reversed(sorted_levels) if _is_roof_level_id(lv.id)), None)
        target_level = roof_lvl or (sorted_levels[-1].id if sorted_levels else None)
    if not target_level:
        raise HTTPException(status_code=400, detail="No roof level found in project.")

    node_by_id = {n.node_id: n for n in model.nodes}
    lv_nodes = [n for n in model.nodes if n.floor_level == target_level]
    lv_node_ids = {n.node_id for n in lv_nodes}
    tol = max(model.options.snap_tolerance_m, 0.15)

    kept_members: list[AnalyticalMember] = []
    removed_ids: set[int] = set()
    for m in model.members:
        is_beam_on_target = m.member_type != "column" and (
            m.floor_level == target_level
            or (m.start_node in lv_node_ids and m.end_node in lv_node_ids)
        )
        if not is_beam_on_target:
            kept_members.append(m)
            continue
        na = node_by_id.get(m.start_node)
        nb = node_by_id.get(m.end_node)
        if na is not None and nb is not None and is_perimeter_beam_pair(na, nb, lv_nodes, tol=tol):
            kept_members.append(m)
        else:
            removed_ids.add(m.member_id)

    model.members = kept_members
    if removed_ids:
        model.engineer_approved = False
    _revalidate_preserving_info(model)
    if project.member_sizing.generated_at is not None or project.member_sizing.sections:
        _ensure_all_members_sized(project)
    storage.save_project(project)
    return model


def _prune_stale_member_refs(project, removed_member_ids: set[int]) -> None:
    if not removed_member_ids:
        return
    if project.member_sizing.member_assignments:
        project.member_sizing.member_assignments = [
            a for a in project.member_sizing.member_assignments if a.member_id not in removed_member_ids
        ]
    if project.load_generation.beam_loads:
        project.load_generation.beam_loads = [
            b for b in project.load_generation.beam_loads if b.member_id not in removed_member_ids
        ]


def _merge_duplicate_node_in_place(project, primary_nid: int, duplicate_nid: int) -> set[int]:
    """
    Merge duplicate_nid into primary_nid:
      - Promote stronger support (FIXED > PINNED > NONE) onto primary_nid
      - Rewire all members from duplicate_nid -> primary_nid
      - Drop zero-length / self-loop members and any newly duplicated node-pair members
      - Remove duplicate_nid from model.nodes
    Returns the set of removed member_ids.
    """
    model = project.structural_model
    node_by_id = {n.node_id: n for n in model.nodes}
    primary = node_by_id.get(primary_nid)
    duplicate = node_by_id.get(duplicate_nid)
    if primary is None or duplicate is None:
        raise HTTPException(
            status_code=404,
            detail=f"Node N{primary_nid} or N{duplicate_nid} not found in structural model.",
        )
    if primary_nid == duplicate_nid:
        raise HTTPException(status_code=400, detail="primary_node_id and duplicate_node_id must differ.")

    rank = {"NONE": 0, "PINNED": 1, "FIXED": 2}
    if rank.get(duplicate.support_type, 0) > rank.get(primary.support_type, 0):
        primary.support_type = duplicate.support_type
        primary.is_support = duplicate.support_type != "NONE"

    # Rewire members and drop self-loops / zero-length / duplicate pairs
    seen_pairs: set[tuple[int, int]] = set()
    kept_members: list[AnalyticalMember] = []
    removed_member_ids: set[int] = set()

    for m in model.members:
        if m.start_node == duplicate_nid:
            m.start_node = primary_nid
        if m.end_node == duplicate_nid:
            m.end_node = primary_nid
        if m.start_node == m.end_node:
            removed_member_ids.add(m.member_id)
            continue
        na = node_by_id.get(m.start_node)
        nb = node_by_id.get(m.end_node)
        if na is None or nb is None:
            removed_member_ids.add(m.member_id)
            continue
        new_len = _dist_3d(na, nb)
        if new_len < 0.05:
            removed_member_ids.add(m.member_id)
            continue
        pair = (min(m.start_node, m.end_node), max(m.start_node, m.end_node))
        if pair in seen_pairs:
            removed_member_ids.add(m.member_id)
            continue
        seen_pairs.add(pair)
        m.length_m = new_len
        kept_members.append(m)

    model.members = kept_members
    model.nodes = [n for n in model.nodes if n.node_id != duplicate_nid]
    _prune_stale_member_refs(project, removed_member_ids)
    return removed_member_ids


@router.post("/delete-duplicate-member", response_model=StructuralModelState)
def delete_duplicate_member(project_id: str, payload: DeleteDuplicateMemberRequest):
    project = _load_or_404(project_id)
    model = project.structural_model
    dup_id = payload.duplicate_member_id
    target = next((m for m in model.members if m.member_id == dup_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Duplicate member M{dup_id} not found.")
    if payload.primary_member_id is not None and payload.primary_member_id == dup_id:
        raise HTTPException(status_code=400, detail="primary_member_id and duplicate_member_id must differ.")

    model.members = [m for m in model.members if m.member_id != dup_id]
    _prune_stale_member_refs(project, {dup_id})
    model.engineer_approved = False
    _revalidate_preserving_info(model)
    storage.save_project(project)
    return model


@router.post("/merge-duplicate-node", response_model=StructuralModelState)
def merge_duplicate_node(project_id: str, payload: MergeDuplicateNodeRequest):
    project = _load_or_404(project_id)
    model = project.structural_model
    _merge_duplicate_node_in_place(project, payload.primary_node_id, payload.duplicate_node_id)
    model.engineer_approved = False
    _revalidate_preserving_info(model)
    storage.save_project(project)
    return model


@router.post("/delete-orphan-nodes", response_model=StructuralModelState)
def delete_orphan_nodes(project_id: str):
    project = _load_or_404(project_id)
    model = project.structural_model
    connected: set[int] = set()
    for m in model.members:
        connected.add(m.start_node)
        connected.add(m.end_node)
    before = len(model.nodes)
    model.nodes = [n for n in model.nodes if n.node_id in connected]
    if len(model.nodes) != before:
        model.engineer_approved = False
    _revalidate_preserving_info(model)
    storage.save_project(project)
    return model


@router.post("/clean-all-duplicates", response_model=StructuralModelState)
def clean_all_duplicates(project_id: str):
    """
    Batch-resolve all duplicate coincident nodes (within snap tolerance) and all
    duplicate members (order-independent {start_node, end_node}) in one safe pass.
    """
    project = _load_or_404(project_id)
    model = project.structural_model
    tol = max(min(model.options.snap_tolerance_m, 0.05), 1e-4)
    changed = False

    # 1. Merge duplicate nodes iteratively (always merging higher node_id into lower node_id)
    while True:
        nodes_sorted = sorted(model.nodes, key=lambda n: n.node_id)
        found_pair: tuple[int, int] | None = None
        for i in range(len(nodes_sorted)):
            na = nodes_sorted[i]
            for j in range(i + 1, len(nodes_sorted)):
                nb = nodes_sorted[j]
                if _dist_3d(na, nb) < tol:
                    found_pair = (na.node_id, nb.node_id)
                    break
            if found_pair is not None:
                break
        if found_pair is None:
            break
        _merge_duplicate_node_in_place(project, found_pair[0], found_pair[1])
        changed = True

    # 2. Remove remaining duplicate members (order-independent {start_node, end_node})
    seen_pairs: set[tuple[int, int]] = set()
    kept_members: list[AnalyticalMember] = []
    removed_mids: set[int] = set()
    for m in sorted(model.members, key=lambda x: x.member_id):
        pair = (min(m.start_node, m.end_node), max(m.start_node, m.end_node))
        if pair in seen_pairs:
            removed_mids.add(m.member_id)
            changed = True
        else:
            seen_pairs.add(pair)
            kept_members.append(m)
    if removed_mids:
        model.members = kept_members
        _prune_stale_member_refs(project, removed_mids)

    if changed:
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
    if project.member_sizing.generated_at is not None or project.member_sizing.sections:
        _ensure_all_members_sized(project)
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
