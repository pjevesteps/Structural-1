from __future__ import annotations

import base64
from datetime import datetime, timezone
import io

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Pt, RGBColor

from ..models import ProjectState
from ..staad import build_staad_text
from .bom_engine import compute_bom_summary

NAVY_HEX = "1B365D"
SLATE_HEX = "334155"
ZEBRA_HEX = "F1F5F9"
TOTAL_HEX = "E2E8F0"
BORDER_HEX = "CBD5E1"
PASS_BG = "DCFCE7"
FAIL_BG = "FEE2E2"

NAVY_RGB = RGBColor(0x1B, 0x36, 0x5D)
SLATE_RGB = RGBColor(0x33, 0x41, 0x55)
MUTED_RGB = RGBColor(0x64, 0x74, 0x8B)
PASS_RGB = RGBColor(0x04, 0x78, 0x57)
FAIL_RGB = RGBColor(0xB9, 0x1C, 0x1C)


def _set_cell_shading(cell, fill_hex: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    tcPr.append(shd)


def _set_cell_margins(cell, top=60, bottom=60, left=90, right=90) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = OxmlElement("w:tcMar")
    for side, val in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        node = OxmlElement(f"w:{side}")
        node.set(qn("w:w"), str(val))
        node.set(qn("w:type"), "dxa")
        tcMar.append(node)
    tcPr.append(tcMar)


def _set_table_borders(table, color_hex: str = BORDER_HEX) -> None:
    tblPr = table._tbl.tblPr
    borders = parse_xml(
        f'<w:tblBorders {nsdecls("w")}>'
        f'<w:top w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
        f'<w:bottom w:val="single" w:sz="6" w:space="0" w:color="{NAVY_HEX}"/>'
        f'<w:insideH w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
        f'<w:left w:val="none"/>'
        f'<w:right w:val="none"/>'
        f'<w:insideV w:val="none"/>'
        f"</w:tblBorders>"
    )
    tblPr.append(borders)


def _set_row_repeat_header(row) -> None:
    trPr = row._tr.get_or_add_trPr()
    trPr.append(OxmlElement("w:tblHeader"))
    trPr.append(OxmlElement("w:cantSplit"))


def _set_row_cant_split(row) -> None:
    trPr = row._tr.get_or_add_trPr()
    trPr.append(OxmlElement("w:cantSplit"))


def _add_page_number_fields(paragraph) -> None:
    run_pre = paragraph.add_run("Page ")
    run_pre.font.size = Pt(8.5)
    run_pre.font.color.rgb = MUTED_RGB

    for field_name in ("PAGE", "NUMPAGES"):
        if field_name == "NUMPAGES":
            run_mid = paragraph.add_run(" of ")
            run_mid.font.size = Pt(8.5)
            run_mid.font.color.rgb = MUTED_RGB
        r = paragraph.add_run()
        r.font.size = Pt(8.5)
        r.font.color.rgb = MUTED_RGB
        fld_begin = OxmlElement("w:fldChar")
        fld_begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = f" {field_name} "
        fld_sep = OxmlElement("w:fldChar")
        fld_sep.set(qn("w:fldCharType"), "separate")
        fld_end = OxmlElement("w:fldChar")
        fld_end.set(qn("w:fldCharType"), "end")
        r._r.append(fld_begin)
        r._r.append(instr)
        r._r.append(fld_sep)
        r._r.append(fld_end)


def _add_section_heading(doc: Document, title: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.keep_with_next = True
    pPr = p._p.get_or_add_pPr()
    pBdr = parse_xml(
        f'<w:pBdr {nsdecls("w")}><w:bottom w:val="single" w:sz="10" w:space="3" w:color="{NAVY_HEX}"/></w:pBdr>'
    )
    pPr.append(pBdr)
    run = p.add_run(title)
    run.bold = True
    run.font.size = Pt(12.5)
    run.font.color.rgb = NAVY_RGB


def _add_subheading(doc: Document, title: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(9)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.keep_with_next = True
    run = p.add_run(title)
    run.bold = True
    run.font.size = Pt(10)
    run.font.color.rgb = SLATE_RGB


def _add_body(doc: Document, text: str, italic: bool = False) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(4)
    run = p.add_run(text)
    run.font.size = Pt(9)
    run.italic = italic
    run.font.color.rgb = MUTED_RGB if italic else RGBColor(0x1E, 0x29, 0x3B)


def _add_styled_table(
    doc: Document,
    headers: list[str],
    rows: list[list[str]],
    right_align_cols: set[int] | None = None,
    status_col: int | None = None,
    bold_last_row: bool = False,
    font_size: float = 8.0,
) -> None:
    right_cols = right_align_cols or set()
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    _set_table_borders(table)

    # Header row
    hdr_row = table.rows[0]
    _set_row_repeat_header(hdr_row)
    for j, h_text in enumerate(headers):
        cell = hdr_row.cells[j]
        _set_cell_shading(cell, NAVY_HEX)
        _set_cell_margins(cell, top=65, bottom=65, left=70, right=70)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        p = cell.paragraphs[0]
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        if j in right_cols:
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        r = p.add_run(h_text)
        r.bold = True
        r.font.size = Pt(font_size)
        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    # Data rows
    last_idx = len(rows) - 1
    for i, row_vals in enumerate(rows):
        row = table.rows[i + 1]
        _set_row_cant_split(row)
        is_total = bold_last_row and i == last_idx
        bg = TOTAL_HEX if is_total else (ZEBRA_HEX if i % 2 == 1 else None)
        for j, val in enumerate(row_vals):
            cell = row.cells[j]
            _set_cell_margins(cell, top=48, bottom=48, left=70, right=70)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            cell_bg = bg
            txt_color = RGBColor(0x1E, 0x29, 0x3B)
            is_bold = is_total
            if status_col is not None and j == status_col:
                if val.strip().upper() == "PASS":
                    cell_bg = PASS_BG
                    txt_color = PASS_RGB
                    is_bold = True
                elif val.strip().upper() == "FAIL":
                    cell_bg = FAIL_BG
                    txt_color = FAIL_RGB
                    is_bold = True
            if cell_bg:
                _set_cell_shading(cell, cell_bg)
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            if j in right_cols:
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            elif status_col is not None and j == status_col:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(str(val))
            r.bold = is_bold
            r.font.size = Pt(font_size)
            r.font.color.rgb = txt_color

    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_before = Pt(0)
    spacer.paragraph_format.space_after = Pt(3)


def build_calculation_report_docx(project: ProjectState) -> bytes:
    """Build permit-ready Structural Calculation Report & BOM as DOCX bytes."""
    rep = project.reporting
    bom = rep.bom if rep.bom is not None else compute_bom_summary(project, rep.bom_params)
    sig = rep.signatory
    opts = rep.options
    pi = project.project_info
    sm = project.structural_model
    ms = project.member_sizing
    lg = project.load_generation
    ad = project.analysis_design
    mat = ms.material
    dp = ad.params

    doc = Document()

    # Configure A4 portrait with clean margins
    section = doc.sections[0]
    section.page_width = Inches(8.27)
    section.page_height = Inches(11.69)
    section.top_margin = Inches(0.75)
    section.bottom_margin = Inches(0.75)
    section.left_margin = Inches(0.65)
    section.right_margin = Inches(0.65)
    section.different_first_page_header_footer = True

    normal_style = doc.styles["Normal"]
    normal_style.font.name = "Calibri"
    normal_style.font.size = Pt(9.5)

    # Running Header & Footer (pages 2+)
    hdr = section.header
    hp = hdr.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    hrun = hp.add_run(
        f"{pi.project_name} ({pi.project_id})  |  Structural Design Calculations & BOM"
    )
    hrun.font.size = Pt(8)
    hrun.font.color.rgb = MUTED_RGB

    ftr = section.footer
    fp = ftr.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    frun = fp.add_run(f"{opts.report_revision}   |   ")
    frun.font.size = Pt(8.5)
    frun.font.color.rgb = MUTED_RGB
    _add_page_number_fields(fp)

    # =========================================================================
    # COVER PAGE
    # =========================================================================
    p_top = doc.add_paragraph()
    p_top.paragraph_format.space_before = Pt(36)
    p_top.paragraph_format.space_after = Pt(6)
    r_kicker = p_top.add_run("STRUCTURAL DESIGN CALCULATIONS & BILL OF MATERIALS")
    r_kicker.bold = True
    r_kicker.font.size = Pt(10.5)
    r_kicker.font.color.rgb = MUTED_RGB

    p_title = doc.add_paragraph()
    p_title.paragraph_format.space_after = Pt(6)
    r_title = p_title.add_run(pi.project_name)
    r_title.bold = True
    r_title.font.size = Pt(24)
    r_title.font.color.rgb = NAVY_RGB

    p_sub = doc.add_paragraph()
    p_sub.paragraph_format.space_after = Pt(20)
    r_sub = p_sub.add_run(
        f"Project Code: {pi.project_id}   •   {opts.report_revision}"
    )
    r_sub.font.size = Pt(11)
    r_sub.font.color.rgb = SLATE_RGB

    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    cover_rows = [
        ["Project Name", pi.project_name],
        ["Project Code", pi.project_id],
        ["Project Owner / Client", sig.project_owner or "To be filled by engineer"],
        ["Project Location / Address", sig.project_address or pi.location or "Philippines"],
        ["Applicable Codes & Standards", "NSCP 2015 Vol. 1 (7th Ed.), ACI 318, NBCP (PD 1096), PNS 49:2020"],
        ["Document Revision", opts.report_revision],
        ["Date Issued", date_str],
    ]
    _add_styled_table(doc, ["Project Attribute", "Specification"], cover_rows, font_size=9.0)

    _add_subheading(doc, "Prepared, Signed & Sealed By")
    sign_rows = [
        ["Engineer Name", sig.engineer_name or "______________________________"],
        ["Title / Designation", sig.title],
        ["PRC License No. / Validity", f"{sig.prc_license_no or 'TBD'}  (Valid until: {sig.prc_validity or 'TBD'})"],
        ["PTR No. / Date / Place", f"{sig.ptr_no or 'TBD'}  /  {sig.ptr_date or 'TBD'}  /  {sig.ptr_place or 'TBD'}"],
        ["Tax Identification No. (TIN)", sig.tin or "TBD"],
    ]
    _add_styled_table(doc, ["Credential", "Details"], sign_rows, font_size=8.8)

    if rep.readiness_warnings:
        _add_subheading(doc, "OBO Permit Readiness Notices")
        for w_msg in rep.readiness_warnings:
            _add_body(doc, f"• {w_msg}", italic=True)

    doc.add_page_break()

    # =========================================================================
    # SECTION 1: DESIGN CRITERIA & MATERIALS
    # =========================================================================
    _add_section_heading(doc, "Section 1 · Design Criteria & Material Specifications")
    _add_body(
        doc,
        "Structural analysis and reinforced concrete member design conform to the National Structural "
        "Code of the Philippines (NSCP 2015, Volume 1, 7th Edition), ACI 318 strength design provisions, "
        "and the National Building Code of the Philippines (PD 1096, Section 302-5).",
    )
    criteria_rows = [
        ["Concrete Compressive Strength (f'c)", f"{mat.fc_mpa:.1f} MPa"],
        ["Concrete Elastic Modulus (Ec = 4700√f'c)", f"{mat.ec_kn_m2:,.1f} kN/m² ({mat.ec_kn_m2 / 1000.0:,.1f} MPa)"],
        ["Unit Weight of Reinforced Concrete", f"{mat.density_kn_m3:.2f} kN/m³"],
        ["Main Reinforcing Steel (PNS 49 Grade 60, fy)", f"{mat.fy_main_mpa:.1f} MPa (Ø16 mm and larger)"],
        ["Transverse Ties / Stirrups (PNS 49 Grade 40, fyt)", f"{mat.fy_tie_mpa:.1f} MPa (Ø10–12 mm)"],
        ["Clear Concrete Covers (Beam / Column / Footing)", f"{dp.clear_cover_beam_mm:.0f} mm / {dp.clear_cover_column_mm:.0f} mm / {dp.clear_cover_footing_mm:.0f} mm"],
        ["Allowable Soil Bearing Capacity (qa)", f"{dp.soil_bearing_qa_kpa:.1f} kPa (Unit weight γs = {dp.soil_unit_weight_kn_m3:.1f} kN/m³)"],
        ["Footing Embedment / Pedestal Drop", f"{dp.footing_embedment_m:.2f} m embedment (Model pedestal drop = {sm.options.footing_depth_m:.2f} m)"],
        ["Geotechnical Report Reference (NBCP 302-5)", sig.geotech_report_ref],
    ]
    _add_styled_table(doc, ["Design Parameter", "Specified Value"], criteria_rows, font_size=8.5)

    # =========================================================================
    # SECTION 2: STRUCTURAL GEOMETRY & MEMBER CATALOG
    # =========================================================================
    _add_section_heading(doc, "Section 2 · Structural Geometry & Preliminary Section Catalog")
    n_cols = sum(1 for m in sm.members if m.member_type == "column" and not m.is_pedestal)
    n_peds = sum(1 for m in sm.members if m.member_type == "column" and m.is_pedestal)
    n_beams = sum(1 for m in sm.members if m.member_type != "column")
    n_slabs = sum(1 for s in sm.slab_panels if s.slab_action != "void")
    n_supports = sum(1 for n in sm.nodes if n.is_support)

    geom_kpi_rows = [
        ["Analytical Nodes / Supports", f"{len(sm.nodes)} nodes ({n_supports} {sm.options.default_support_type} supports)"],
        ["Columns / Pedestals", f"{n_cols} structural columns, {n_peds} foundation pedestals"],
        ["Beams & Tie Beams", f"{n_beams} frame beam elements"],
        ["RC Slab Panels", f"{n_slabs} active panels ({sum(s.net_area_sqm for s in sm.slab_panels if s.slab_action != 'void'):.2f} m² net area)"],
    ]
    _add_styled_table(doc, ["Analytical Model Metric", "Count / Summary"], geom_kpi_rows, font_size=8.5)

    _add_subheading(doc, "Story Elevations")
    sorted_lvls = sorted(project.levels, key=lambda lv: lv.elevation_m)
    lvl_rows = []
    for idx, lv in enumerate(sorted_lvls):
        sh = (lv.elevation_m - sorted_lvls[idx - 1].elevation_m) if idx > 0 else 0.0
        lvl_rows.append([lv.id, f"{lv.elevation_m:.2f} m", f"{sh:.2f} m" if idx > 0 else "Base"])
    _add_styled_table(doc, ["Floor Level", "Elevation (m)", "Story Height (m)"], lvl_rows, right_align_cols={1, 2})

    _add_subheading(doc, "Section Property Catalog & Cracked Stiffness Modifiers (NSCP 406.6.3.1)")
    sec_rows = [
        [
            s.section_id,
            s.label,
            s.category.replace("_", " ").title(),
            f"{s.width_mm:.0f} × {s.depth_mm:.0f}",
            f"{s.cracked_ix:.2f} / {s.cracked_iy:.2f} / {s.cracked_iz:.2f}",
        ]
        for s in ms.sections
    ]
    _add_styled_table(
        doc,
        ["Tag", "Description", "Category", "Size b×h (mm)", "Cracked Ix / Iy / Iz"],
        sec_rows,
        right_align_cols={3, 4},
    )

    # =========================================================================
    # SECTION 3: NSCP 2015 GRAVITY, SEISMIC & WIND LOADS
    # =========================================================================
    _add_section_heading(doc, "Section 3 · NSCP 2015 Gravity, Seismic (Sec. 208) & Wind (Sec. 207) Loads")

    if lg.floor_loads:
        _add_subheading(doc, "3.1 Floor Gravity Loads (NSCP 2015 Tables 204-1 & 205-1)")
        fl_rows = [
            [
                fl.floor_level,
                fl.occupancy_preset.replace("_", " ").title(),
                f"{fl.sdl_finish_kpa:.2f}",
                f"{fl.sdl_ceiling_mep_kpa:.2f}",
                f"{fl.sdl_partitions_kpa:.2f}",
                f"{fl.sdl_finish_kpa + fl.sdl_ceiling_mep_kpa + fl.sdl_partitions_kpa:.2f}",
                f"{fl.live_load_kpa:.2f}",
                f"{fl.exterior_wall_line_kn_m:.2f}" if fl.apply_exterior_wall_perimeter else "0.00",
            ]
            for fl in lg.floor_loads
        ]
        _add_styled_table(
            doc,
            ["Floor", "Occupancy", "Finish", "MEP", "Part.", "Total SDL (kPa)", "LL (kPa)", "Ext. Wall (kN/m)"],
            fl_rows,
            right_align_cols={2, 3, 4, 5, 6, 7},
        )

    ss = lg.seismic_summary
    if ss is not None:
        _add_subheading(doc, "3.2 Seismic Parameters & Equivalent Static Base Shear (NSCP Sec. 208.5)")
        sp = lg.seismic
        seis_param_rows = [
            ["Seismic Zone / Soil Profile", f"Zone {sp.zone} (Z = {ss.z_factor:.2f})  /  Soil {sp.soil_profile}"],
            ["Near-Source Factors (Na, Nv)", f"Source Type {sp.seismic_source_type} @ {sp.fault_distance_km:.1f} km  →  Na = {ss.na:.2f}, Nv = {ss.nv:.2f}"],
            ["Seismic Coefficients (Ca, Cv)", f"Ca = {ss.ca:.3f}, Cv = {ss.cv:.3f}"],
            ["System Factors (R, I, ρ)", f"Rx = {sp.r_factor_x:.1f}, Rz = {sp.r_factor_z:.1f}, I = {sp.importance_factor_i:.2f}, ρ = {sp.redundancy_rho:.2f}"],
            ["Building Height & Period (hn, T)", f"hn = {ss.building_height_m:.2f} m, T = {ss.period_t_sec:.3f} sec"],
            ["Total Seismic Dead Weight (W)", f"{ss.total_seismic_weight_kn:,.2f} kN"],
            ["Design Base Shear (Vx / Vz)", f"Vx = {ss.v_design_x_kn:,.2f} kN ({ss.base_shear_ratio_x * 100:.2f}% W)  |  Vz = {ss.v_design_z_kn:,.2f} kN ({ss.base_shear_ratio_z * 100:.2f}% W)"],
        ]
        _add_styled_table(doc, ["Seismic Parameter", "Computed Value"], seis_param_rows, font_size=8.3)

        if ss.stories:
            st_rows = [
                [
                    st.floor_level,
                    f"{st.elevation_m:.2f}",
                    f"{st.weight_kn:,.1f}",
                    f"{st.fx_kn:,.2f}",
                    f"{st.fz_kn:,.2f}",
                    f"{st.cumulative_shear_x_kn:,.2f}",
                    f"{st.cumulative_shear_z_kn:,.2f}",
                ]
                for st in ss.stories
            ]
            _add_styled_table(
                doc,
                ["Story", "Elev (m)", "Weight Wi (kN)", "Fx (kN)", "Fz (kN)", "Cum. Vx (kN)", "Cum. Vz (kN)"],
                st_rows,
                right_align_cols={1, 2, 3, 4, 5, 6},
            )

    ws = lg.wind_summary
    if ws is not None:
        _add_subheading(doc, "3.3 Wind Load Parameters & Story Forces (NSCP Sec. 207)")
        wp = lg.wind
        _add_body(
            doc,
            f"Basic Wind Speed V = {wp.basic_wind_speed_kph:.0f} kph | Exposure {wp.exposure_category} | "
            f"Kzt = {wp.topographic_kzt:.2f} | Kd = {wp.directionality_kd:.2f} | G = {wp.gust_factor_g:.2f} | "
            f"Total Wind Base Shear: Wx = {ws.base_shear_x_kn:.2f} kN, Wz = {ws.base_shear_z_kn:.2f} kN.",
        )
        if ws.stories:
            w_rows = [
                [
                    st.floor_level,
                    f"{st.elevation_m:.2f}",
                    f"{st.kz:.3f}",
                    f"{st.qz_kpa:.3f}",
                    f"{st.fx_kn:.2f}",
                    f"{st.fz_kn:.2f}",
                ]
                for st in ws.stories
            ]
            _add_styled_table(
                doc,
                ["Story", "Elev (m)", "Kz", "qz (kPa)", "Wind Fx (kN)", "Wind Fz (kN)"],
                w_rows,
                right_align_cols={1, 2, 3, 4, 5},
            )

    if opts.include_load_combinations and lg.combinations:
        _add_subheading(doc, "3.4 Factored (LRFD) & Service (ASD) Load Combinations (NSCP Sec. 203)")
        combo_rows = [
            [
                str(c.combo_id),
                c.design_basis,
                c.name,
                ", ".join(f"{k}:{v:+.2f}" for k, v in c.factors.items()),
            ]
            for c in lg.combinations
        ]
        _add_styled_table(doc, ["ID", "Basis", "Combination Name", "Load Factors"], combo_rows, font_size=7.6)

    # =========================================================================
    # SECTION 4: ANALYSIS VERIFICATION, DRIFT & SUPPORT REACTIONS
    # =========================================================================
    _add_section_heading(doc, "Section 4 · Structural Analysis, Drift Verification & Support Reactions")
    src_label = (
        f"STAAD.Pro Analysis Output ({ad.source_filename})"
        if ad.source == "staad_anl"
        else "Built-in 3D Direct Stiffness Method (DSM) Elastic Frame Solver"
    )
    _add_body(doc, f"Analysis Source: {src_label}  |  Generated: {ad.generated_at or 'N/A'}")

    if ad.story_drifts:
        _add_subheading(doc, "4.1 NSCP 2015 Section 208.6.4 Story Drift & Soft-Story Check")
        drift_rows = [
            [
                d.floor_level,
                f"{d.story_height_m:.2f}",
                f"{d.delta_s_x_mm:.2f} / {d.delta_s_z_mm:.2f}",
                f"{d.delta_m_x_mm:.2f} / {d.delta_m_z_mm:.2f}",
                f"{d.allowable_drift_mm:.1f}",
                f"{max(d.drift_ratio_x, d.drift_ratio_z):.3f}",
                "YES" if d.soft_story_flag else "No",
                d.status,
            ]
            for d in ad.story_drifts
        ]
        _add_styled_table(
            doc,
            ["Story", "h (m)", "ΔS X/Z (mm)", "ΔM X/Z (mm)", "Allow (mm)", "Max Ratio", "Soft Story", "Status"],
            drift_rows,
            right_align_cols={1, 2, 3, 4, 5},
            status_col=7,
            font_size=7.8,
        )

    if ad.support_reactions:
        _add_subheading(doc, "4.2 Foundation Support Reaction Envelopes (ASD & LRFD)")
        rxn_rows = [
            [
                f"N{r.node_id}",
                r.grid_ref or "-",
                r.floor_level,
                f"{r.max_asd_fy_kn:.1f}",
                f"{r.min_asd_fy_kn:.1f}",
                f"{r.max_lrfd_pu_kn:.1f}",
                f"{max(r.max_lrfd_mux_knm, r.max_lrfd_muz_knm):.1f}",
                f"{r.max_lrfd_v_kn:.1f}",
            ]
            for r in ad.support_reactions
        ]
        _add_styled_table(
            doc,
            ["Node", "Grid", "Base Level", "ASD Fy Max (kN)", "ASD Fy Min (kN)", "LRFD Pu (kN)", "LRFD Mu (kNm)", "LRFD V (kN)"],
            rxn_rows,
            right_align_cols={3, 4, 5, 6, 7},
            font_size=7.8,
        )

    if ad.pinned_design_views:
        _add_subheading(doc, "4.3 Visual Design Verification & Utilization Maps")
        for snap in ad.pinned_design_views:
            title_txt = snap.get("title") or "Pinned Design View"
            caption_txt = snap.get("caption") or ""
            data_url = snap.get("image_data_url") or ""
            p_t = doc.add_paragraph()
            p_t.paragraph_format.space_before = Pt(4)
            p_t.paragraph_format.space_after = Pt(2)
            rt = p_t.add_run(title_txt)
            rt.bold = True
            rt.font.size = Pt(9.0)
            rt.font.color.rgb = NAVY_RGB
            if "," in data_url:
                try:
                    raw_img = base64.b64decode(data_url.split(",", 1)[1])
                    if raw_img:
                        doc.add_picture(io.BytesIO(raw_img), width=Inches(6.2))
                except Exception:
                    pass
            if caption_txt:
                p_c = doc.add_paragraph()
                p_c.paragraph_format.space_after = Pt(4)
                rc = p_c.add_run(caption_txt)
                rc.italic = True
                rc.font.size = Pt(8.0)
                rc.font.color.rgb = MUTED_RGB

    # =========================================================================
    # SECTION 5: RC MEMBER DESIGN SCHEDULES
    # =========================================================================
    _add_section_heading(doc, "Section 5 · Reinforced Concrete Member Design Schedules")
    beam_pass = sum(1 for b in ad.beam_designs if b.status == "PASS")
    col_pass = sum(1 for c in ad.column_designs if c.status == "PASS")
    ftg_pass = sum(1 for f in ad.footing_designs if f.status == "PASS")
    max_b_dcr = max((b.governing_dcr for b in ad.beam_designs), default=0.0)
    max_c_dcr = max((c.governing_dcr for c in ad.column_designs), default=0.0)
    max_f_dcr = max((f.governing_dcr for f in ad.footing_designs), default=0.0)

    kpi_rows = [
        ["Beams & Tie Beams", f"{beam_pass} / {len(ad.beam_designs)} PASS", f"{max_b_dcr:.3f}", "PASS" if beam_pass == len(ad.beam_designs) else "FAIL"],
        ["Columns & Pedestals", f"{col_pass} / {len(ad.column_designs)} PASS", f"{max_c_dcr:.3f}", "PASS" if col_pass == len(ad.column_designs) else "FAIL"],
        ["Isolated Footings", f"{ftg_pass} / {len(ad.footing_designs)} PASS", f"{max_f_dcr:.3f}", "PASS" if ftg_pass == len(ad.footing_designs) else "FAIL"],
    ]
    _add_styled_table(
        doc,
        ["Element Group", "Pass Count", "Max Governing DCR", "Overall"],
        kpi_rows,
        right_align_cols={1, 2},
        status_col=3,
        font_size=8.5,
    )

    if opts.include_full_member_schedules:
        if ad.beam_designs:
            _add_subheading(doc, "5.1 Beam Reinforcement Schedule")
            b_rows = [
                [
                    f"M{b.member_id}",
                    b.section_id,
                    b.floor_level,
                    f"{b.width_mm:.0f}×{b.depth_mm:.0f}",
                    f"{b.mu_neg_start_knm:.1f}/{b.mu_pos_mid_knm:.1f}",
                    b.bars_top_start,
                    b.bars_bot_mid,
                    b.stirrups_support,
                    f"{b.governing_dcr:.2f}",
                    b.status,
                ]
                for b in ad.beam_designs
            ]
            _add_styled_table(
                doc,
                ["ID", "Sec", "Floor", "b×h (mm)", "Mu-/+ (kNm)", "Top Bars", "Bot Bars", "Stirrups (Sup)", "DCR", "Status"],
                b_rows,
                right_align_cols={4, 8},
                status_col=9,
                font_size=7.2,
            )

        if ad.column_designs:
            _add_subheading(doc, "5.2 Column & Pedestal Reinforcement Schedule")
            c_rows = [
                [
                    f"M{c.member_id}",
                    c.section_id,
                    c.floor_level,
                    f"{c.width_mm:.0f}×{c.depth_mm:.0f}",
                    f"{c.pu_max_kn:.1f}",
                    f"{max(c.mux_max_knm, c.muy_max_knm):.1f}",
                    c.main_bars,
                    f"{c.rho_provided * 100:.2f}%",
                    c.ties_confined,
                    f"{c.governing_dcr:.2f}",
                    c.status,
                ]
                for c in ad.column_designs
            ]
            _add_styled_table(
                doc,
                ["ID", "Sec", "Floor", "b×h (mm)", "Pu (kN)", "Mu (kNm)", "Main Bars", "ρ", "Confined Ties", "DCR", "Status"],
                c_rows,
                right_align_cols={4, 5, 7, 9},
                status_col=10,
                font_size=7.2,
            )

        if ad.footing_designs:
            _add_subheading(doc, "5.3 Isolated Spread Footing Schedule")
            f_rows = [
                [
                    f.footing_tag,
                    f"N{f.node_id} ({f.grid_ref or '-'})",
                    f"{f.b_m:.2f} × {f.l_m:.2f} × {f.thickness_mm:.0f}",
                    f"{f.p_service_kn:.1f}",
                    f"{f.q_actual_kpa:.1f} / {f.q_allow_kpa:.0f}",
                    f"{f.punching_shear_dcr:.2f}",
                    f.bottom_bars_x,
                    f"{f.governing_dcr:.2f}",
                    f.status,
                ]
                for f in ad.footing_designs
            ]
            _add_styled_table(
                doc,
                ["Tag", "Node (Grid)", "B × L × t (m/mm)", "Pserv (kN)", "q / qa (kPa)", "Punch DCR", "Bottom Bars (Both Ways)", "DCR", "Status"],
                f_rows,
                right_align_cols={3, 4, 5, 7},
                status_col=8,
                font_size=7.3,
            )

        if ad.smrf_joint_checks:
            _add_subheading(doc, "5.4 NSCP Sec. 418 SMRF Joint & Strong-Column/Weak-Beam (SCWB) Summary")
            scwb_pass = sum(1 for j in ad.smrf_joint_checks if j.scwb_status == "PASS")
            js_pass = sum(1 for j in ad.smrf_joint_checks if j.joint_shear_status == "PASS")
            _add_body(
                doc,
                f"Evaluated {len(ad.smrf_joint_checks)} elevated beam-column joint(s): "
                f"{scwb_pass}/{len(ad.smrf_joint_checks)} PASS SCWB (Sum Mnc / Sum Mnb >= 1.20, NSCP Sec. 418.7.3) | "
                f"{js_pass}/{len(ad.smrf_joint_checks)} PASS Joint Shear (Vu <= phi Vn, NSCP Sec. 418.8.4).",
            )
            j_rows = [
                [
                    f"N{j.joint_node_id}",
                    j.floor_level,
                    j.grid_ref or "-",
                    f"{j.sum_mnc_knm:.1f}",
                    f"{j.sum_mnb_knm:.1f}",
                    f"{j.scwb_ratio:.2f}",
                    f"{j.joint_shear_dcr:.2f}",
                    "PASS" if (j.scwb_status == "PASS" and j.joint_shear_status == "PASS") else "FAIL",
                ]
                for j in ad.smrf_joint_checks
            ]
            _add_styled_table(
                doc,
                ["Node", "Level", "Grid", "Sum Mnc (kNm)", "Sum Mnb (kNm)", "SCWB (>=1.20)", "Joint Shear DCR", "Status"],
                j_rows,
                right_align_cols={3, 4, 5, 6},
                status_col=7,
                font_size=7.3,
            )

        if (
            ad.retaining_wall_designs
            or ad.slab_designs
            or ad.stair_designs
            or ad.strap_footing_designs
            or ad.roof_truss_designs
        ):
            _add_subheading(doc, "5.5 Secondary Elements & Retaining Walls")
            if ad.retaining_wall_designs:
                rw_rows = [
                    [
                        r.wall_id,
                        f"{r.retained_height_m:.2f}m x {r.total_length_m:.1f}m",
                        f"{r.ka:.2f} / {r.k_ae_seismic:.2f}",
                        f"{r.fs_overturning:.2f} / {r.fs_sliding:.2f}",
                        f"{r.max_toe_bearing_kpa:.1f}",
                        r.stem_vertical_bars,
                        r.base_heel_toe_bars,
                        r.status,
                    ]
                    for r in ad.retaining_wall_designs
                ]
                _add_styled_table(
                    doc,
                    ["Wall", "H x L", "Ka / Kae", "FS OT/Sl", "q_toe (kPa)", "Stem Vert", "Base Bars", "Status"],
                    rw_rows,
                    right_align_cols={2, 3, 4},
                    status_col=7,
                    font_size=7.2,
                )
            if ad.slab_designs:
                sl_rows = [
                    [
                        s.panel_id,
                        s.floor_level,
                        f"{s.lx_m:.2f} x {s.ly_m:.2f}",
                        f"{s.thickness_mm:.0f}",
                        s.short_span_cont_neg,
                        s.short_span_pos,
                        s.long_span_pos,
                        s.status,
                    ]
                    for s in ad.slab_designs[:24]
                ]
                _add_styled_table(
                    doc,
                    ["Panel", "Floor", "Lx x Ly (m)", "t (mm)", "Short Cont (-)", "Short Pos (+)", "Long Pos (+)", "Status"],
                    sl_rows,
                    right_align_cols={2, 3},
                    status_col=7,
                    font_size=7.1,
                )
            if ad.stair_designs:
                st_rows = [
                    [
                        s.stair_id,
                        f"{s.flight_span_m:.2f} m",
                        f"{s.riser_mm:.0f} / {s.tread_mm:.0f}",
                        f"{s.waist_thickness_mm:.0f} mm",
                        f"{s.mu_knm_per_m:.1f}",
                        s.main_bars,
                        s.dist_bars,
                        s.deflection_status,
                    ]
                    for s in ad.stair_designs
                ]
                _add_styled_table(
                    doc,
                    ["Stair", "Span", "R/T (mm)", "Waist", "Mu (kNm/m)", "Main Bars", "Dist Bars", "Defl"],
                    st_rows,
                    right_align_cols={1, 4},
                    status_col=7,
                    font_size=7.2,
                )
            if ad.roof_truss_designs:
                tr_rows = [
                    [
                        t.truss_id,
                        f"{t.truss_type} ({t.span_m:.1f}m @ {t.pitch_deg:.0f}°)",
                        t.top_chord_section,
                        t.bottom_chord_section,
                        t.web_section,
                        f"{t.max_chord_dcr:.2f} / {t.max_web_dcr:.2f}",
                        f"{t.estimated_steel_kg:.1f} kg",
                        t.status,
                    ]
                    for t in ad.roof_truss_designs
                ]
                _add_styled_table(
                    doc,
                    ["Truss", "Type / Span", "Top Chord", "Bot Chord", "Webs", "DCR (C/W)", "Est. Steel", "Status"],
                    tr_rows,
                    right_align_cols={5, 6},
                    status_col=7,
                    font_size=7.2,
                )
            if ad.strap_footing_designs:
                sf_rows = [
                    [
                        s.strap_id,
                        f"N{s.exterior_node_id} -> N{s.interior_node_id}",
                        f"{s.ext_pad_b_m:.2f}x{s.ext_pad_l_m:.2f}",
                        f"{s.int_pad_b_m:.2f}x{s.int_pad_l_m:.2f}",
                        f"{s.strap_b_mm:.0f}x{s.strap_h_mm:.0f}",
                        s.strap_top_bars,
                        s.strap_stirrups,
                        s.status,
                    ]
                    for s in ad.strap_footing_designs
                ]
                _add_styled_table(
                    doc,
                    ["Strap", "Ext -> Int", "Ext Pad (m)", "Int Pad (m)", "Beam (mm)", "Top Bars", "Stirrups", "Status"],
                    sf_rows,
                    status_col=7,
                    font_size=7.2,
                )

    # =========================================================================
    # SECTION 6: BILL OF MATERIALS & COST ESTIMATE
    # =========================================================================
    if opts.include_bom_cost_estimate:
        _add_section_heading(doc, "Section 6 · Structural Bill of Materials & Cost Estimate (NBCP Sec. 302-5f)")
        _add_body(
            doc,
            "Note: Beam concrete volume is based on full rectangular stem dimensions; reinforcing steel "
            "unit mass follows PNS 49:2020 nominal linear mass (d²/162 kg/m). Commercial bar counts "
            f"are rounded up to {rep.bom_params.commercial_bar_length_m:g} m standard lengths.",
            italic=True,
        )

        _add_subheading(doc, "6.1 Concrete & Formwork Takeoff")
        conc_rows = [
            [
                r.element_group,
                str(r.member_count),
                f"{r.net_volume_m3:.3f}",
                f"{r.gross_volume_m3:.3f}",
                f"{r.formwork_area_m2:.2f}",
            ]
            for r in bom.concrete_rows
        ]
        conc_rows.append(
            [
                "TOTAL CONCRETE & FORMWORK",
                str(sum(r.member_count for r in bom.concrete_rows)),
                f"{bom.total_concrete_net_m3:.3f}",
                f"{bom.total_concrete_gross_m3:.3f}",
                f"{bom.total_formwork_m2:.2f}",
            ]
        )
        _add_styled_table(
            doc,
            ["Element Group", "Members", "Net Vol. (m³)", f"Gross Vol. (+{rep.bom_params.concrete_waste_pct:.0f}%)", "Formwork (m²)"],
            conc_rows,
            right_align_cols={1, 2, 3, 4},
            bold_last_row=True,
        )

        _add_subheading(doc, "6.2 PNS 49:2020 Reinforcing Steel Schedule")
        rb_rows = [
            [
                f"Ø{r.diameter_mm} mm",
                r.grade_label,
                f"{r.unit_mass_kg_m:.4f}",
                f"{r.net_length_m:,.2f}",
                f"{r.gross_length_m:,.2f}",
                f"{r.total_weight_kg:,.2f}",
                f"{r.commercial_pieces:,} pcs",
            ]
            for r in bom.rebar_rows
        ]
        rb_rows.append(
            [
                "TOTAL REBAR",
                f"Steel Ratio: {bom.rebar_ratio_kg_per_m3:.1f} kg/m³",
                "",
                f"{sum(r.net_length_m for r in bom.rebar_rows):,.2f}",
                f"{sum(r.gross_length_m for r in bom.rebar_rows):,.2f}",
                f"{bom.total_rebar_kg:,.2f}",
                f"{sum(r.commercial_pieces for r in bom.rebar_rows):,} pcs",
            ]
        )
        _add_styled_table(
            doc,
            ["Bar Size", "Grade", "kg/m", "Net Length (m)", "Gross Length (m)", "Weight (kg)", f"Pcs ({rep.bom_params.commercial_bar_length_m:g}m)"],
            rb_rows,
            right_align_cols={2, 3, 4, 5, 6},
            bold_last_row=True,
        )

        _add_subheading(doc, "6.3 Structural Cost Estimate Summary (Philippine Peso, PHP)")
        cost_rows = [
            [
                c.item_no,
                c.description,
                f"{c.quantity:,.2f}",
                c.unit,
                f"₱{c.unit_cost_php:,.2f}",
                f"₱{c.material_cost_php:,.2f}",
                f"₱{c.labor_cost_php:,.2f}",
                f"₱{c.total_cost_php:,.2f}",
            ]
            for c in bom.cost_lines
        ]
        cost_rows.append(
            [
                "TOTAL",
                f"Grand Total Structural Cost (Labor = {rep.bom_params.labor_pct_of_materials:.1f}% of Materials)",
                "",
                "",
                "",
                f"₱{bom.total_material_cost_php:,.2f}",
                f"₱{bom.total_labor_cost_php:,.2f}",
                f"₱{bom.grand_total_cost_php:,.2f}",
            ]
        )
        _add_styled_table(
            doc,
            ["Item", "Scope of Work", "Qty", "Unit", "Unit Rate", "Material (PHP)", "Labor (PHP)", "Total (PHP)"],
            cost_rows,
            right_align_cols={2, 4, 5, 6, 7},
            bold_last_row=True,
            font_size=7.6,
        )

    # Optional STAAD Snippet Appendix
    if opts.include_staad_snippet:
        _add_section_heading(doc, "Appendix A · STAAD.Pro Input File Excerpt")
        std_lines = build_staad_text(project).splitlines()
        snippet = "\n".join(std_lines[:80])
        if len(std_lines) > 80:
            snippet += f"\n* ... ({len(std_lines) - 80} additional lines omitted; export full .std in Module 05)"
        p_code = doc.add_paragraph()
        r_code = p_code.add_run(snippet)
        r_code.font.name = "Consolas"
        r_code.font.size = Pt(7.5)

    # =========================================================================
    # SECTION 7: PROFESSIONAL CERTIFICATION & DRY SEAL BLOCK
    # =========================================================================
    _add_section_heading(doc, "Section 7 · Professional Certification & Dry Seal Block (NBCP PD 1096)")
    _add_body(
        doc,
        "I hereby certify that the structural analysis, design calculations, reinforcement schedules, "
        "and structural bill of materials contained herein have been prepared in accordance with the "
        "National Structural Code of the Philippines (NSCP 2015, Volume 1, 7th Edition) and the "
        "National Building Code of the Philippines (PD 1096), under my direct professional supervision "
        "and engineering verification.",
    )
    cert_rows = [
        ["Signature over Printed Name", f"\n\n________________________________________\n{sig.engineer_name or '[Engineer Name]'}\n{sig.title}"],
        ["PRC Registration No.", sig.prc_license_no or "______________________"],
        ["PRC Validity Date", sig.prc_validity or "______________________"],
        ["PTR No. / Date / Place Issued", f"{sig.ptr_no or '________'} / {sig.ptr_date or '________'} / {sig.ptr_place or '________'}"],
        ["Tax Identification No. (TIN)", sig.tin or "______________________"],
        ["Dry Seal Space", "\n\n[  AFFIX PROFESSIONAL DRY SEAL HERE  ]\n"],
    ]
    _add_styled_table(doc, ["Certification Field", "Engineer Attestation & Credentials"], cert_rows, font_size=8.8)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
