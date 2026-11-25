import math
import re

from ..models import ProjectState, SupportReactionEnvelope
from .solver import (
    AnalysisResults,
    BeamForceEnvelope,
    ColumnComboDemand,
    ColumnForceEnvelope,
)

_FLOAT_RE = r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_ROW_JOINT_FULL = re.compile(
    rf"^\s*(\d+)\s+(\d+)\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})"
)
_ROW_JOINT_CONT = re.compile(
    rf"^\s+(\d+)\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})"
)
_ROW_BEAM_FULL = re.compile(
    rf"^\s*(\d+)\s+(\d+)\s+(\d+)\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})"
)
_ROW_BEAM_CONT = re.compile(
    rf"^\s+(\d+)\s+(\d+)\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})"
)
_ROW_BEAM_END2 = re.compile(
    rf"^\s+(\d+)\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})\s+({_FLOAT_RE})"
)


def parse_staad_anl(anl_text: str, project: ProjectState) -> AnalysisResults:
    """
    Parse a STAAD.Pro .anl text output file for:
      - JOINT DISPLACEMENT
      - SUPPORT REACTIONS
      - BEAM END FORCES
    and map results into the unified AnalysisResults envelope structure.
    """
    lines = anl_text.splitlines()
    has_staad_error = any("** ERROR" in ln or "*** ERROR" in ln for ln in lines)

    section: str | None = None
    disp_scale_to_mm = 1.0  # default assume mm unless METER/M header detected
    cur_node: int | None = None
    cur_beam: int | None = None
    cur_lc: int | None = None

    # Raw parsed tables
    # (node_id, lc_id) -> (dx_mm, dy_mm, dz_mm)
    joint_disps: dict[tuple[int, int], tuple[float, float, float]] = {}
    # (node_id, lc_id) -> (fx, fy, fz, mx, my, mz)
    support_rxns: dict[tuple[int, int], tuple[float, float, float, float, float, float]] = {}
    # (beam_id, lc_id) -> list[(node_id, fx, fy, fz, mx, my, mz)]
    beam_forces: dict[tuple[int, int], list[tuple[int, float, float, float, float, float, float]]] = {}

    for ln in lines:
        up = ln.upper()
        if "JOINT DISPLACEMENT" in up:
            section = "DISP"
            cur_node = None
            disp_scale_to_mm = 1000.0 if ("METER" in up or "METRE" in up) and "MM" not in up else 1.0
            continue
        if "SUPPORT REACTION" in up:
            section = "RXN"
            cur_node = None
            continue
        if "BEAM END FORCE" in up or "MEMBER END FORCE" in up:
            section = "BEAM"
            cur_beam = None
            cur_lc = None
            continue
        if section == "DISP" and ("METER" in up or "METRE" in up) and "MM" not in up and "TRANS" not in up:
            disp_scale_to_mm = 1000.0
            continue
        if section and any(
            stopper in up
            for stopper in (
                "END OF LATEST ANALYSIS",
                "FINISH",
                "SECTION DISPLACEMENT",
                "CONCRETE DESIGN",
                "STATICS CHECK",
                "PROBLEM STATISTICS",
            )
        ):
            section = None
            cur_node = None
            cur_beam = None
            cur_lc = None
            continue

        if section == "DISP":
            m_full = _ROW_JOINT_FULL.match(ln)
            if m_full:
                nid, lc = int(m_full.group(1)), int(m_full.group(2))
                cur_node = nid
                joint_disps[(nid, lc)] = (
                    float(m_full.group(3)) * disp_scale_to_mm,
                    float(m_full.group(4)) * disp_scale_to_mm,
                    float(m_full.group(5)) * disp_scale_to_mm,
                )
                continue
            m_cont = _ROW_JOINT_CONT.match(ln)
            if m_cont and cur_node is not None:
                lc = int(m_cont.group(1))
                joint_disps[(cur_node, lc)] = (
                    float(m_cont.group(2)) * disp_scale_to_mm,
                    float(m_cont.group(3)) * disp_scale_to_mm,
                    float(m_cont.group(4)) * disp_scale_to_mm,
                )
                continue

        elif section == "RXN":
            m_full = _ROW_JOINT_FULL.match(ln)
            if m_full:
                nid, lc = int(m_full.group(1)), int(m_full.group(2))
                cur_node = nid
                support_rxns[(nid, lc)] = tuple(float(m_full.group(i)) for i in range(3, 9))
                continue
            m_cont = _ROW_JOINT_CONT.match(ln)
            if m_cont and cur_node is not None:
                lc = int(m_cont.group(1))
                support_rxns[(cur_node, lc)] = tuple(float(m_cont.group(i)) for i in range(2, 8))
                continue

        elif section == "BEAM":
            m_full = _ROW_BEAM_FULL.match(ln)
            if m_full:
                bid, lc, nid = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
                cur_beam, cur_lc = bid, lc
                beam_forces.setdefault((bid, lc), []).append(
                    (nid, *(float(m_full.group(i)) for i in range(4, 10)))
                )
                continue
            m_cont = _ROW_BEAM_CONT.match(ln)
            if m_cont and cur_beam is not None:
                lc, nid = int(m_cont.group(1)), int(m_cont.group(2))
                cur_lc = lc
                beam_forces.setdefault((cur_beam, lc), []).append(
                    (nid, *(float(m_cont.group(i)) for i in range(3, 9)))
                )
                continue
            m_end2 = _ROW_BEAM_END2.match(ln)
            if m_end2 and cur_beam is not None and cur_lc is not None:
                nid = int(m_end2.group(1))
                beam_forces.setdefault((cur_beam, cur_lc), []).append(
                    (nid, *(float(m_end2.group(i)) for i in range(2, 8)))
                )
                continue

    if not joint_disps and not support_rxns and not beam_forces:
        if has_staad_error:
            raise ValueError(
                "Uploaded STAAD.Pro .anl file terminated with ** ERROR and contains no analysis results. "
                "Resolve STAAD errors and re-run analysis before uploading."
            )
        raise ValueError(
            "No JOINT DISPLACEMENT, SUPPORT REACTIONS, or BEAM END FORCES tables were found in the uploaded .anl file."
        )

    model = project.structural_model
    lg = project.load_generation
    node_by_id = {n.node_id: n for n in model.nodes}
    member_by_id = {m.member_id: m for m in model.members}
    combo_by_id = {c.combo_id: c for c in lg.combinations}

    def _case_basis(lc_id: int) -> str:
        if lc_id in combo_by_id:
            return combo_by_id[lc_id].design_basis
        if 100 <= lc_id < 200:
            return "LRFD"
        if lc_id >= 200:
            return "ASD"
        return "PRIMARY"

    def _case_name(lc_id: int) -> str:
        if lc_id in combo_by_id:
            return combo_by_id[lc_id].name
        return f"LC {lc_id}"

    # 1. Seismic Displacements (EX+ = LC 1, EZ+ = LC 3)
    seismic_disps: dict[str, dict[int, tuple[float, float, float]]] = {"EX+": {}, "EZ+": {}}
    for (nid, lc), xyz in joint_disps.items():
        if lc == 1:
            seismic_disps["EX+"][nid] = xyz
        elif lc == 3:
            seismic_disps["EZ+"][nid] = xyz

    # 2. Support Reaction Envelopes
    rxns_by_node: dict[int, list[tuple[int, tuple[float, float, float, float, float, float]]]] = {}
    for (nid, lc), vals in support_rxns.items():
        rxns_by_node.setdefault(nid, []).append((lc, vals))

    support_envelopes: list[SupportReactionEnvelope] = []
    for nid, entries in sorted(rxns_by_node.items()):
        node = node_by_id.get(nid)
        asd_entries = [(lc, v) for lc, v in entries if _case_basis(lc) == "ASD"]
        lrfd_entries = [(lc, v) for lc, v in entries if _case_basis(lc) == "LRFD"]
        if not asd_entries:
            asd_entries = entries
        if not lrfd_entries:
            lrfd_entries = entries

        max_asd_fy = max(v[1] for _, v in asd_entries)
        min_asd_fy = min(v[1] for _, v in asd_entries)
        gov_asd_lc = max(asd_entries, key=lambda x: x[1][1])[0]

        max_lrfd_pu = max(v[1] for _, v in lrfd_entries)
        gov_lrfd_lc = max(lrfd_entries, key=lambda x: x[1][1])[0]
        max_lrfd_mx = max(abs(v[3]) for _, v in lrfd_entries)
        max_lrfd_mz = max(abs(v[5]) for _, v in lrfd_entries)
        max_lrfd_v = max(math.hypot(v[0], v[2]) for _, v in lrfd_entries)

        support_envelopes.append(
            SupportReactionEnvelope(
                node_id=nid,
                grid_ref=node.grid_ref if node else "",
                floor_level=node.floor_level if node else "",
                max_asd_fy_kn=round(max_asd_fy, 2),
                min_asd_fy_kn=round(min_asd_fy, 2),
                max_lrfd_pu_kn=round(max_lrfd_pu, 2),
                max_lrfd_mux_knm=round(max_lrfd_mx, 2),
                max_lrfd_muz_knm=round(max_lrfd_mz, 2),
                max_lrfd_v_kn=round(max_lrfd_v, 2),
                governing_asd_combo=_case_name(gov_asd_lc),
                governing_lrfd_combo=_case_name(gov_lrfd_lc),
            )
        )

    # 3. Beam & Column Envelopes
    forces_by_member: dict[int, list[tuple[int, list[tuple[int, float, float, float, float, float, float]]]]] = {}
    for (bid, lc), rows in beam_forces.items():
        forces_by_member.setdefault(bid, []).append((lc, rows))

    beam_envelopes: dict[int, BeamForceEnvelope] = {}
    column_envelopes: dict[int, ColumnForceEnvelope] = {}

    for mid, lc_list in forces_by_member.items():
        m = member_by_id.get(mid)
        lrfd_lcs = [(lc, rows) for lc, rows in lc_list if _case_basis(lc) == "LRFD"]
        active = lrfd_lcs if lrfd_lcs else lc_list

        if m is not None and m.member_type == "column":
            env_c = ColumnForceEnvelope(member_id=mid, pu_min_kn=1e18)
            best_score = -1.0
            for lc, rows in active:
                pu = max(r[1] for r in rows)
                mux = max(abs(r[6]) for r in rows)
                muy = max(abs(r[5]) for r in rows)
                vu = max(math.hypot(r[2], r[3]) for r in rows)
                cname = _case_name(lc)
                env_c.demands.append(
                    ColumnComboDemand(
                        combo_name=cname,
                        pu_kn=round(pu, 3),
                        mux_knm=round(mux, 3),
                        muy_knm=round(muy, 3),
                        vu_kn=round(vu, 3),
                    )
                )
                env_c.pu_max_kn = max(env_c.pu_max_kn, pu)
                env_c.pu_min_kn = min(env_c.pu_min_kn, pu)
                env_c.mux_max_knm = max(env_c.mux_max_knm, mux)
                env_c.muy_max_knm = max(env_c.muy_max_knm, muy)
                env_c.vu_max_kn = max(env_c.vu_max_kn, vu)
                score = abs(pu) * 0.05 + mux + muy
                if score > best_score:
                    best_score = score
                    env_c.governing_combo = cname
            if env_c.pu_min_kn > 1e17:
                env_c.pu_min_kn = 0.0
            column_envelopes[mid] = env_c
        else:
            env_b = BeamForceEnvelope(member_id=mid)
            best_m = -1.0
            length_m = m.length_m if m else 4.0
            for lc, rows in active:
                r_start = rows[0]
                r_end = rows[-1] if len(rows) > 1 else rows[0]
                mu_start = abs(r_start[6])
                mu_end = abs(r_end[6])
                vu = max(abs(r_start[2]), abs(r_end[2]))
                # Conservative midspan estimate from end shear & end hogging moments when STAAD prints end forces only
                mu_pos = max(0.0, 0.25 * vu * length_m - 0.5 * (mu_start + mu_end))
                env_b.mu_neg_start_knm = max(env_b.mu_neg_start_knm, mu_start)
                env_b.mu_neg_end_knm = max(env_b.mu_neg_end_knm, mu_end)
                env_b.mu_pos_mid_knm = max(env_b.mu_pos_mid_knm, mu_pos)
                env_b.vu_max_kn = max(env_b.vu_max_kn, vu)
                peak = max(mu_start, mu_end, mu_pos)
                if peak > best_m:
                    best_m = peak
                    env_b.governing_combo = _case_name(lc)
            beam_envelopes[mid] = env_b

    return AnalysisResults(
        seismic_displacements_mm=seismic_disps,
        support_reactions=support_envelopes,
        beam_envelopes=beam_envelopes,
        column_envelopes=column_envelopes,
    )
