from pathlib import Path
import re
from .models import BeamDistributedLoad, ProjectState
from .geometry import build_node_map

_SOIL_STYP = {"SA": 1, "SB": 2, "SC": 3, "SD": 4, "SE": 5}

_COMBO_CASE_ID = {
    "EX+": 1,
    "EX-": 2,
    "EZ+": 3,
    "EZ-": 4,
    "WX+": 5,
    "WX-": 6,
    "WZ+": 7,
    "WZ-": 8,
    "DL": 9,
    "LL": 10,
    "LR": 11,
}


def _safe_group_name(prefix: str, label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label.upper()).strip("_")
    return f"{prefix}{slug or 'GROUP'}"


def _wrap_staad_ids(
    ids: list[int],
    prefix: str = "",
    suffix: str = "",
    max_len: int = 72,
) -> list[str]:
    """
    Format a STAAD.Pro member/joint ID command with '-' line continuations
    whenever a line would exceed max_len (72) characters.
    """
    tokens = [str(x) for x in ids]
    if not tokens:
        return []

    lines: list[str] = []
    cur = prefix.rstrip()

    for tok in tokens:
        candidate = f"{cur} {tok}" if cur else tok
        # Leave 2 chars room for trailing ' -' when more tokens or suffix may follow
        if cur and len(candidate) > max_len - 2:
            lines.append(f"{cur} -")
            cur = tok
        else:
            cur = candidate

    if suffix:
        cand_with_suffix = f"{cur} {suffix}" if cur else suffix
        if cur and len(cand_with_suffix) > max_len:
            lines.append(f"{cur} -")
            lines.append(suffix)
        else:
            lines.append(cand_with_suffix)
    elif cur:
        lines.append(cur)

    return lines


def _wrap_staad_tokens(tokens: list[str], max_len: int = 72) -> list[str]:
    """Wrap arbitrary STAAD token list onto lines <= max_len using '-' continuations."""
    if not tokens:
        return []
    lines: list[str] = []
    cur = ""
    for tok in tokens:
        cand = f"{cur} {tok}" if cur else tok
        if cur and len(cand) > max_len - 2:
            lines.append(f"{cur} -")
            cur = tok
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def _format_member_loads_block(
    loads: list[BeamDistributedLoad],
    scale: float = 1.0,
) -> list[str]:
    """Group identical UNI / TRAP member loads cleanly with 72-char wrapping."""
    if not loads or abs(scale) <= 1e-6:
        return []
    grouped: dict[str, list[int]] = {}
    for ld in loads:
        w1 = ld.w1_kn_m * scale
        w2 = ld.w2_kn_m * scale
        if ld.load_shape == "UNI":
            if abs(ld.d1_m) <= 1e-4 and abs(ld.d2_m) <= 1e-4:
                spec = f"UNI GY {w1:.3f}"
            else:
                spec = f"UNI GY {w1:.3f} {ld.d1_m:.3f} {ld.d2_m:.3f}"
        else:
            spec = f"TRAP GY {w1:.3f} {w2:.3f} {ld.d1_m:.3f} {ld.d2_m:.3f}"
        grouped.setdefault(spec, []).append(ld.member_id)

    out: list[str] = ["MEMBER LOAD"]
    for spec, mids in grouped.items():
        # Preserve multiplicity if a member receives identical loads from adjacent panels
        counts: dict[int, int] = {}
        for mid in mids:
            counts[mid] = counts.get(mid, 0) + 1
        max_mult = max(counts.values())
        for layer in range(1, max_mult + 1):
            layer_mids = sorted(mid for mid, c in counts.items() if c >= layer)
            out.extend(_wrap_staad_ids(layer_mids, suffix=spec))
    return out


def _append_phase5_loads(lines: list[str], project: ProjectState) -> None:
    """Append Phase 5 NSCP 2015 reference mass loads, diaphragms, UBC/explicit seismic, wind, gravity, and combos."""
    model = project.structural_model
    lg = project.load_generation
    seis = lg.seismic
    seis_sum = lg.seismic_summary
    wind_sum = lg.wind_summary
    opts = lg.options

    levels = sorted(project.levels, key=lambda lv: lv.elevation_m)
    base_elev = levels[0].elevation_m if levels else min(n.y_m for n in model.nodes)
    elevated_levels = [lv for lv in levels if lv.elevation_m > base_elev + 1e-4]
    valid_mids = {m.member_id for m in model.members}

    dl_loads = [b for b in lg.beam_loads if b.load_case == "DL" and b.member_id in valid_mids]
    ll_loads = [b for b in lg.beam_loads if b.load_case == "LL" and b.member_id in valid_mids]
    lr_loads = [b for b in lg.beam_loads if b.load_case == "LR" and b.member_id in valid_mids]

    # 1. DEFINE REFERENCE LOADS (must precede FLOOR DIAPHRAGM & DEFINE UBC LOAD for mass modeling)
    lines.append("DEFINE REFERENCE LOADS")
    lines.append("LOAD R1 LOADTYPE Dead TITLE REF DEAD MASS (DL)")
    lines.append("SELFWEIGHT Y -1.0")
    lines.extend(_format_member_loads_block(dl_loads))
    if seis.live_load_seismic_fraction > 1e-6 and ll_loads:
        lines.append("LOAD R2 LOADTYPE Live TITLE REF LIVE MASS (LL)")
        lines.extend(_format_member_loads_block(ll_loads, scale=seis.live_load_seismic_fraction))
    lines.append("END DEFINE REFERENCE LOADS")

    # 2. Rigid Floor Diaphragms (strictly AFTER END DEFINE REFERENCE LOADS, excluding supported nodes)
    if opts.include_rigid_diaphragm and elevated_levels:
        dia_lines: list[str] = []
        dia_idx = 1
        for lv in elevated_levels:
            lvl_nodes = [n for n in model.nodes if abs(n.y_m - lv.elevation_m) <= 0.05]
            unsupported_ids = sorted(
                n.node_id for n in lvl_nodes if not n.is_support and n.support_type == "NONE"
            )
            if len(unsupported_ids) < 2:
                continue
            dia_lines.extend(
                _wrap_staad_ids(
                    unsupported_ids,
                    prefix=f"DIA {dia_idx} TYPE RIG HEI {lv.elevation_m:.3f} JOI",
                )
            )
            dia_idx += 1
        if dia_lines:
            lines.append("FLOOR DIAPHRAGM")
            lines.extend(dia_lines)

    # 3. DEFINE UBC LOAD (parameters only; seismic mass comes from Reference Load R1/R2 to avoid double-counting)
    if seis.staad_seismic_method == "ubc_auto" and seis_sum is not None:
        styp = _SOIL_STYP.get(seis.soil_profile, 4)
        period_t = max(seis_sum.period_t_sec, 0.05)
        lines.append("DEFINE UBC LOAD")
        lines.append(
            f"ZONE {seis_sum.z_factor:.2f} I {seis.importance_factor_i:.2f} "
            f"RWX {seis.r_factor_x:.2f} RWZ {seis.r_factor_z:.2f} STYP {styp} -"
        )
        lines.append(
            f"NA {seis_sum.na:.3f} NV {seis_sum.nv:.3f} CT {seis.ct_coeff:.4f} "
            f"PX {period_t:.3f} PZ {period_t:.3f}"
        )

    # 4. Primary Load Cases — SEISMIC FIRST (STAAD-safe ordering)
    seismic_specs = [
        (1, "SEISMIC +X (NSCP 2015)", "X", 1.0),
        (2, "SEISMIC -X (NSCP 2015)", "X", -1.0),
        (3, "SEISMIC +Z (NSCP 2015)", "Z", 1.0),
        (4, "SEISMIC -Z (NSCP 2015)", "Z", -1.0),
    ]
    for cid, title, axis, sign in seismic_specs:
        lines.append(f"LOAD {cid} LOADTYPE Seismic TITLE {title}")
        if seis.staad_seismic_method == "ubc_auto":
            lines.append(f"UBC LOAD {axis} {sign:.1f}")
            lines.append("PERFORM ANALYSIS")
            lines.append("CHANGE")
        else:
            if seis_sum is not None and seis_sum.stories:
                joint_lines: list[str] = []
                for st in seis_sum.stories:
                    story_nodes = sorted(
                        n.node_id for n in model.nodes if abs(n.y_m - st.elevation_m) <= 0.15
                    )
                    if not story_nodes:
                        continue
                    total_f = (st.fx_kn if axis == "X" else st.fz_kn) * sign
                    if abs(total_f) <= 1e-6:
                        continue
                    f_per = total_f / len(story_nodes)
                    joint_lines.extend(
                        _wrap_staad_ids(story_nodes, suffix=f"F{axis} {f_per:.3f}")
                    )
                if joint_lines:
                    lines.append("JOINT LOAD")
                    lines.extend(joint_lines)

    # 5. Wind Primary Load Cases (5..8) — distributed to windward perimeter nodes per story
    wind_specs = [
        (5, "WIND +X (NSCP 2015)", "X", 1.0),
        (6, "WIND -X (NSCP 2015)", "X", -1.0),
        (7, "WIND +Z (NSCP 2015)", "Z", 1.0),
        (8, "WIND -Z (NSCP 2015)", "Z", -1.0),
    ]
    for cid, title, axis, sign in wind_specs:
        lines.append(f"LOAD {cid} LOADTYPE Wind TITLE {title}")
        if wind_sum is not None and wind_sum.stories:
            joint_lines = []
            for st in wind_sum.stories:
                story_nodes = [n for n in model.nodes if abs(n.y_m - st.elevation_m) <= 0.15]
                if not story_nodes:
                    continue
                total_f = (st.fx_kn if axis == "X" else st.fz_kn) * sign
                if abs(total_f) <= 1e-6:
                    continue
                if axis == "X":
                    target_coord = (
                        min(n.x_m for n in story_nodes)
                        if sign > 0
                        else max(n.x_m for n in story_nodes)
                    )
                    face_nids = sorted(
                        n.node_id for n in story_nodes if abs(n.x_m - target_coord) <= 0.25
                    )
                else:
                    target_coord = (
                        min(n.z_m for n in story_nodes)
                        if sign > 0
                        else max(n.z_m for n in story_nodes)
                    )
                    face_nids = sorted(
                        n.node_id for n in story_nodes if abs(n.z_m - target_coord) <= 0.25
                    )
                if not face_nids:
                    face_nids = sorted(n.node_id for n in story_nodes)
                f_per = total_f / len(face_nids)
                joint_lines.extend(_wrap_staad_ids(face_nids, suffix=f"F{axis} {f_per:.3f}"))
            if joint_lines:
                lines.append("JOINT LOAD")
                lines.extend(joint_lines)

    # 6. Gravity Primary Load Cases (9: DL via Reference Load R1, 10: LL, 11: LR)
    lines.append("LOAD 9 LOADTYPE Dead TITLE DEAD LOAD (DL)")
    lines.append("REFERENCE LOAD")
    lines.append("R1 1.0")

    lines.append("LOAD 10 LOADTYPE Live TITLE FLOOR LIVE LOAD (LL)")
    if ll_loads:
        lines.extend(_format_member_loads_block(ll_loads))
    else:
        lines.append("* No floor live loads assigned")

    lines.append("LOAD 11 LOADTYPE Roof Live TITLE ROOF LIVE LOAD (LR)")
    if lr_loads:
        lines.extend(_format_member_loads_block(lr_loads))
    else:
        lines.append("* No roof live loads assigned")

    # 6. Load Combinations (LRFD 101+ & ASD 201+)
    for combo in lg.combinations:
        pair_tokens: list[str] = []
        for key, factor in combo.factors.items():
            ref_id = _COMBO_CASE_ID.get(key)
            if ref_id is not None and abs(factor) > 1e-6:
                pair_tokens.append(f"{ref_id} {factor:.3f}")
        if not pair_tokens:
            continue
        if opts.use_repeat_load_combos:
            lines.append(f"LOAD {combo.combo_id} LOADTYPE None TITLE {combo.name}")
            lines.append("REPEAT LOAD")
            lines.extend(_wrap_staad_tokens(pair_tokens))
        else:
            lines.append(f"LOAD COMB {combo.combo_id} {combo.name}")
            lines.extend(_wrap_staad_tokens(pair_tokens))

    # 7. Analysis Trailer
    lines.append("PERFORM ANALYSIS PRINT STATICS CHECK")


def write_staad_preview(project: ProjectState, destination: Path):
    """
    Write STAAD.Pro .std file with real newlines and 72-character '-' line continuations.
    - When Phase 5 load_generation is populated (with Phase 3 + Phase 4), emits complete
      analysis-ready STAAD.Pro input including FLOOR DIAPHRAGM, DEFINE UBC LOAD, Seismic/Wind/
      Gravity primary cases, LRFD/ASD LOAD COMB or REPEAT LOAD, and PERFORM ANALYSIS PRINT STATICS CHECK.
    - When Phase 4 member_sizing is populated without loads, emits geometry + material/sections/
      cracked modifiers/releases + supports.
    - When Phase 3 structural_model is populated without sizing, emits Phase 3 geometry + groups + supports.
    - Falls back cleanly to Phase 1 column/beam mapping when structural_model is empty.
    """
    model = project.structural_model
    sizing = project.member_sizing
    lg = project.load_generation
    has_sizing = bool(
        model.nodes
        and model.members
        and sizing.sections
        and sizing.member_assignments
    )
    has_loads = bool(has_sizing and lg.generated_at is not None)

    if model.nodes and model.members:
        lines = ["STAAD SPACE", "UNIT METER KN", "JOINT COORDINATES"]
        for n in sorted(model.nodes, key=lambda x: x.node_id):
            lines.append(f"{n.node_id} {n.x_m:.3f} {n.y_m:.3f} {n.z_m:.3f};")

        lines.append("MEMBER INCIDENCES")
        for m in sorted(model.members, key=lambda x: x.member_id):
            lines.append(f"{m.member_id} {m.start_node} {m.end_node};")

        # Member groups
        col_ids = sorted(m.member_id for m in model.members if m.member_type == "column")
        beam_ids = sorted(
            m.member_id
            for m in model.members
            if m.member_type in ("primary_beam", "secondary_beam", "trimmer_beam")
        )
        tie_ids = sorted(m.member_id for m in model.members if m.member_type == "tie_beam")

        level_groups: dict[str, list[int]] = {}
        for m in sorted(model.members, key=lambda x: x.member_id):
            if m.floor_level:
                level_groups.setdefault(m.floor_level, []).append(m.member_id)

        sec_by_id = {s.section_id: s for s in sizing.sections}
        valid_mids = {m.member_id for m in model.members}
        section_member_map: dict[str, list[int]] = {}
        if has_sizing:
            for asgn in sorted(sizing.member_assignments, key=lambda a: a.member_id):
                if asgn.member_id in valid_mids and asgn.section_id in sec_by_id:
                    section_member_map.setdefault(asgn.section_id, []).append(asgn.member_id)

        if col_ids or beam_ids or tie_ids or level_groups or section_member_map:
            lines.append("START GROUP DEFINITION")
            lines.append("MEMBER")
            if col_ids:
                lines.extend(_wrap_staad_ids(col_ids, prefix="_COLUMNS"))
            if beam_ids:
                lines.extend(_wrap_staad_ids(beam_ids, prefix="_BEAMS"))
            if tie_ids:
                lines.extend(_wrap_staad_ids(tie_ids, prefix="_TIE_BEAMS"))
            for lvl_name, mids in level_groups.items():
                lines.extend(_wrap_staad_ids(mids, prefix=_safe_group_name("_LVL_", lvl_name)))
            if has_sizing:
                for sec in sizing.sections:
                    mids = section_member_map.get(sec.section_id, [])
                    if mids:
                        lines.extend(_wrap_staad_ids(mids, prefix=_safe_group_name("_SEC_", sec.section_id)))
            lines.append("END GROUP DEFINITION")

        # Phase 4: Material, Section Properties, Constants/Beta, Cracked Modifiers, Releases
        if has_sizing:
            mat = sizing.material
            mat_name = re.sub(r"[^A-Za-z0-9_]+", "_", (mat.name or "CONCRETE").upper()).strip("_") or "CONCRETE"
            lines.append("DEFINE MATERIAL START")
            lines.append(f"ISOTROPIC {mat_name}")
            lines.append(f"E {mat.ec_kn_m2:.2f}")
            lines.append(f"POISSON {mat.poisson:.3f}")
            lines.append(f"DENSITY {mat.density_kn_m3:.3f}")
            lines.append(f"ALPHA {mat.alpha:.1e}")
            lines.append(f"DAMP {mat.damp:.3f}")
            lines.append("TYPE CONCRETE")
            lines.append(f"STRENGTH FCU {mat.fc_mpa * 1000.0:.1f}")
            lines.append("END DEFINE MATERIAL")

            lines.append("MEMBER PROPERTY")
            for sec in sizing.sections:
                mids = section_member_map.get(sec.section_id, [])
                if not mids:
                    continue
                yd_m = sec.depth_mm / 1000.0
                zd_m = sec.width_mm / 1000.0
                lines.append(f"* Section {sec.section_id}: {sec.width_mm:.0f}x{sec.depth_mm:.0f} mm")
                lines.extend(
                    _wrap_staad_ids(
                        mids,
                        suffix=f"PRIS YD {yd_m:.3f} ZD {zd_m:.3f}",
                    )
                )

            lines.append("CONSTANTS")
            lines.append(f"MATERIAL {mat_name} ALL")
            beta_groups: dict[float, list[int]] = {}
            for asgn in sorted(sizing.member_assignments, key=lambda a: a.member_id):
                if asgn.member_id in valid_mids and abs(asgn.beta_angle_deg) > 1e-6:
                    beta_groups.setdefault(round(asgn.beta_angle_deg, 2), []).append(asgn.member_id)
            for deg, mids in sorted(beta_groups.items()):
                deg_str = f"{deg:g}"
                lines.extend(_wrap_staad_ids(mids, prefix=f"BETA {deg_str} MEMB"))

            if sizing.options.include_cracked_modifiers:
                cracked_groups: dict[tuple[float, float, float], list[int]] = {}
                for asgn in sorted(sizing.member_assignments, key=lambda a: a.member_id):
                    if asgn.member_id not in valid_mids:
                        continue
                    sec = sec_by_id.get(asgn.section_id)
                    if sec is None:
                        continue
                    key = (round(sec.cracked_ix, 2), round(sec.cracked_iy, 2), round(sec.cracked_iz, 2))
                    cracked_groups.setdefault(key, []).append(asgn.member_id)
                if cracked_groups:
                    lines.append("MEMBER CRACKED")
                    for (ix, iy, iz), mids in cracked_groups.items():
                        lines.extend(
                            _wrap_staad_ids(
                                mids,
                                suffix=f"REDUCTION RIX {ix:.2f} RIY {iy:.2f} RIZ {iz:.2f}",
                            )
                        )

            start_rel = sorted(
                a.member_id
                for a in sizing.member_assignments
                if a.member_id in valid_mids and a.release_start_moment
            )
            end_rel = sorted(
                a.member_id
                for a in sizing.member_assignments
                if a.member_id in valid_mids and a.release_end_moment
            )
            if start_rel or end_rel:
                lines.append("MEMBER RELEASE")
                if start_rel:
                    lines.extend(_wrap_staad_ids(start_rel, suffix="START MY MZ"))
                if end_rel:
                    lines.extend(_wrap_staad_ids(end_rel, suffix="END MY MZ"))

        # Supports
        fixed_ids = sorted(n.node_id for n in model.nodes if n.is_support and n.support_type == "FIXED")
        pinned_ids = sorted(n.node_id for n in model.nodes if n.is_support and n.support_type == "PINNED")
        if fixed_ids or pinned_ids:
            lines.append("SUPPORTS")
            if fixed_ids:
                lines.extend(_wrap_staad_ids(fixed_ids, suffix="FIXED"))
            if pinned_ids:
                lines.extend(_wrap_staad_ids(pinned_ids, suffix="PINNED"))

        # Phase 5: NSCP 2015 Diaphragms, Loads, Combinations & Analysis
        if has_loads:
            _append_phase5_loads(lines, project)

        lines += ["FINISH", ""]
        content = "\n".join(lines)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

        if has_loads:
            source = "structural_model_full_analysis"
            notice = (
                "Full analysis-ready STAAD.Pro .std exported (geometry, material, cracked sections, "
                "releases, diaphragms, NSCP 2015 seismic/wind/gravity loads & LRFD/ASD combinations). "
                "Engineer review required prior to STAAD.Pro execution."
            )
        elif has_sizing:
            source = "structural_model_with_sizing"
            notice = (
                "STAAD.Pro geometry + preliminary member properties, material, cracked modifiers & releases exported; "
                "loads and design parameters required before analysis."
            )
        else:
            source = "structural_model"
            notice = "Geometry-only STAAD.Pro preview from analytical model; sections and loads required before analysis."

        return {
            "path": str(destination),
            "nodes": len(model.nodes),
            "members": len(model.members),
            "supports": len(fixed_ids) + len(pinned_ids),
            "source": source,
            "std_preview": content,
            "notice": notice,
        }

    # Phase 1 fallback when structural_model is empty
    levels, node_map, joints = build_node_map(project)
    lines = ["STAAD SPACE", "UNIT METER KN", "JOINT COORDINATES"]
    lines += [j + ";" for j in joints]
    lines.append("MEMBER INCIDENCES")
    member_id, count = 1, 0
    for lower, upper in zip(levels, levels[1:]):
        for col in project.columns:
            lines.append(f"{member_id} {node_map[(col.id, lower.id)]} {node_map[(col.id, upper.id)]};")
            member_id += 1
            count += 1
    for beam in project.beams:
        lines.append(f"{member_id} {node_map[(beam.start_col, beam.level)]} {node_map[(beam.end_col, beam.level)]};")
        member_id += 1
        count += 1
    lines += ["FINISH", ""]
    content = "\n".join(lines)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return {
        "path": str(destination),
        "nodes": len(joints),
        "members": count,
        "source": "phase1_fallback",
        "std_preview": content,
        "notice": "Geometry-only preview; not ready for engineering analysis.",
    }


def simulate_placeholder(project: ProjectState):
    return {
        "mode": "PLACEHOLDER",
        "status": "NOT_ANALYZED",
        "project_id": project.project_info.project_id,
        "message": "No solver was run. No engineering results are available.",
    }
