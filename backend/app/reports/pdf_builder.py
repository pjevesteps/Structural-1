from __future__ import annotations

import base64
from datetime import datetime, timezone
import io

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    HRFlowable,
    Image as RLImage,
    LongTable,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from ..models import ProjectState
from ..staad import build_staad_text
from .bom_engine import compute_bom_summary

NAVY = colors.HexColor("#1B365D")
SLATE = colors.HexColor("#334155")
MUTED = colors.HexColor("#64748B")
ZEBRA = colors.HexColor("#F1F5F9")
TOTAL_BG = colors.HexColor("#E2E8F0")
BORDER = colors.HexColor("#CBD5E1")
PASS_BG = colors.HexColor("#DCFCE7")
PASS_FG = colors.HexColor("#047857")
FAIL_BG = colors.HexColor("#FEE2E2")
FAIL_FG = colors.HexColor("#B91C1C")
INK = colors.HexColor("#0F172A")


def _safe_pdf_text(text: str) -> str:
    """Keep text strictly Latin-1 safe for built-in ReportLab Helvetica fonts."""
    return (
        str(text)
        .replace("\u20b1", "PHP ")
        .replace("\u2014", " - ")
        .replace("\u2013", "-")
        .replace("\u2022", "-")
        .replace("\u2192", "->")
        .replace("\u221a", "sqrt")
        .replace("\u0394", "Delta ")
        .replace("\u03c1", "rho")
        .replace("\u03b3", "gamma")
        .replace("\u00d8", "D")
        .replace("\u00f8", "d")
    )


def _make_numbered_canvas_class(header_left: str, footer_left: str):
    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            num_pages = len(self._saved_page_states)
            for state in self._saved_page_states:
                self.__dict__.update(state)
                self._draw_chrome(num_pages)
                super().showPage()
            super().save()

        def _draw_chrome(self, total_pages: int):
            page_num = self._pageNumber
            w, h = A4
            self.saveState()
            if page_num > 1:
                # Top running header
                self.setFont("Helvetica", 7.5)
                self.setFillColor(MUTED)
                self.drawString(16 * mm, h - 12 * mm, _safe_pdf_text(header_left))
                self.drawRightString(w - 16 * mm, h - 12 * mm, "Structural Calculation Report & BOM")
                self.setStrokeColor(BORDER)
                self.setLineWidth(0.5)
                self.line(16 * mm, h - 14 * mm, w - 16 * mm, h - 14 * mm)
            # Bottom running footer
            self.setStrokeColor(BORDER)
            self.setLineWidth(0.5)
            self.line(16 * mm, 13 * mm, w - 16 * mm, 13 * mm)
            self.setFont("Helvetica", 7.5)
            self.setFillColor(MUTED)
            self.drawString(16 * mm, 9 * mm, _safe_pdf_text(footer_left))
            self.drawRightString(w - 16 * mm, 9 * mm, f"Page {page_num} of {total_pages}")
            self.restoreState()

    return NumberedCanvas


def build_calculation_report_pdf(project: ProjectState) -> bytes:
    """Build permit-ready Structural Calculation Report & BOM as A4 PDF bytes."""
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

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        title=f"{pi.project_name} - Structural Calculation Report",
        author=sig.engineer_name or "Structural AI Office",
    )
    avail_w = A4[0] - 32 * mm

    base = getSampleStyleSheet()
    st_kicker = ParagraphStyle(
        "kicker", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=9, textColor=MUTED, spaceAfter=4
    )
    st_title = ParagraphStyle(
        "coverTitle", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=NAVY, spaceAfter=6
    )
    st_sub = ParagraphStyle(
        "coverSub", parent=base["Normal"], fontName="Helvetica", fontSize=10, textColor=SLATE, spaceAfter=14
    )
    st_h1 = ParagraphStyle(
        "h1", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=NAVY, spaceBefore=12, spaceAfter=4, keepWithNext=True
    )
    st_h2 = ParagraphStyle(
        "h2", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=SLATE, spaceBefore=8, spaceAfter=3, keepWithNext=True
    )
    st_body = ParagraphStyle(
        "body", parent=base["Normal"], fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=INK, spaceAfter=5
    )
    st_note = ParagraphStyle(
        "note", parent=base["Normal"], fontName="Helvetica-Oblique", fontSize=8, leading=10.5, textColor=MUTED, spaceAfter=5
    )
    st_cell = ParagraphStyle(
        "cell", parent=base["Normal"], fontName="Helvetica", fontSize=7.4, leading=9.2, textColor=INK
    )
    st_cell_r = ParagraphStyle("cellR", parent=st_cell, alignment=TA_RIGHT)
    st_cell_b = ParagraphStyle("cellB", parent=st_cell, fontName="Helvetica-Bold")
    st_cell_br = ParagraphStyle("cellBR", parent=st_cell_b, alignment=TA_RIGHT)
    st_hdr = ParagraphStyle(
        "hdr", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=7.5, leading=9.2, textColor=colors.white
    )
    st_hdr_r = ParagraphStyle("hdrR", parent=st_hdr, alignment=TA_RIGHT)
    st_pass = ParagraphStyle(
        "pass", parent=st_cell_b, alignment=TA_CENTER, textColor=PASS_FG
    )
    st_fail = ParagraphStyle(
        "fail", parent=st_cell_b, alignment=TA_CENTER, textColor=FAIL_FG
    )
    st_code = ParagraphStyle(
        "code", parent=base["Code"], fontName="Courier", fontSize=6.8, leading=8.5, textColor=INK
    )

    story: list = []

    def add_section(title: str) -> None:
        story.append(Paragraph(_safe_pdf_text(title), st_h1))
        story.append(HRFlowable(width="100%", thickness=1.0, color=NAVY, spaceBefore=0, spaceAfter=6))

    def add_sub(title: str) -> None:
        story.append(Paragraph(_safe_pdf_text(title), st_h2))

    def add_table(
        headers: list[str],
        rows: list[list[str]],
        col_weights: list[float] | None = None,
        right_cols: set[int] | None = None,
        status_col: int | None = None,
        bold_last: bool = False,
    ) -> None:
        rc = right_cols or set()
        n_cols = len(headers)
        if col_weights and len(col_weights) == n_cols:
            tot_w = sum(col_weights)
            col_widths = [avail_w * (w / tot_w) for w in col_weights]
        else:
            col_widths = [avail_w / n_cols] * n_cols

        data = [
            [
                Paragraph(_safe_pdf_text(h), st_hdr_r if j in rc else st_hdr)
                for j, h in enumerate(headers)
            ]
        ]
        style_cmds: list = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, NAVY),
            ("LINEBELOW", (0, 1), (-1, -1), 0.35, BORDER),
            ("LINEBELOW", (0, -1), (-1, -1), 0.9, NAVY),
        ]

        last_idx = len(rows) - 1
        for i, row_vals in enumerate(rows):
            r_idx = i + 1
            is_tot = bold_last and i == last_idx
            if is_tot:
                style_cmds.append(("BACKGROUND", (0, r_idx), (-1, r_idx), TOTAL_BG))
            elif i % 2 == 1:
                style_cmds.append(("BACKGROUND", (0, r_idx), (-1, r_idx), ZEBRA))

            rendered_row = []
            for j, val in enumerate(row_vals):
                txt = _safe_pdf_text(val)
                if status_col is not None and j == status_col:
                    up = txt.strip().upper()
                    if up == "PASS":
                        style_cmds.append(("BACKGROUND", (j, r_idx), (j, r_idx), PASS_BG))
                        rendered_row.append(Paragraph("PASS", st_pass))
                        continue
                    if up == "FAIL":
                        style_cmds.append(("BACKGROUND", (j, r_idx), (j, r_idx), FAIL_BG))
                        rendered_row.append(Paragraph("FAIL", st_fail))
                        continue
                if is_tot:
                    rendered_row.append(Paragraph(txt, st_cell_br if j in rc else st_cell_b))
                else:
                    rendered_row.append(Paragraph(txt, st_cell_r if j in rc else st_cell))
            data.append(rendered_row)

        tbl = LongTable(data, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle(style_cmds))
        story.append(tbl)
        story.append(Spacer(1, 5))

    # =========================================================================
    # COVER PAGE
    # =========================================================================
    story.append(Spacer(1, 18 * mm))
    story.append(Paragraph("STRUCTURAL DESIGN CALCULATIONS &amp; BILL OF MATERIALS", st_kicker))
    story.append(Paragraph(_safe_pdf_text(pi.project_name), st_title))
    story.append(
        Paragraph(
            _safe_pdf_text(f"Project Code: {pi.project_id}  |  {opts.report_revision}"),
            st_sub,
        )
    )
    story.append(HRFlowable(width="100%", thickness=1.5, color=NAVY, spaceAfter=12))

    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    add_table(
        ["Project Attribute", "Specification"],
        [
            ["Project Name", pi.project_name],
            ["Project Code", pi.project_id],
            ["Project Owner / Client", sig.project_owner or "To be filled by engineer"],
            ["Project Location / Address", sig.project_address or pi.location or "Philippines"],
            ["Applicable Design Codes", "NSCP 2015 Vol. 1 (7th Ed.), ACI 318, NBCP (PD 1096), PNS 49:2020"],
            ["Document Revision", opts.report_revision],
            ["Date Issued", date_str],
        ],
        col_weights=[32, 68],
    )

    add_sub("Prepared, Signed & Sealed By")
    add_table(
        ["Credential", "Engineer Details"],
        [
            ["Engineer Name", sig.engineer_name or "______________________________"],
            ["Title / Designation", sig.title],
            ["PRC License No. / Validity", f"{sig.prc_license_no or 'TBD'} (Valid: {sig.prc_validity or 'TBD'})"],
            ["PTR No. / Date / Place", f"{sig.ptr_no or 'TBD'} / {sig.ptr_date or 'TBD'} / {sig.ptr_place or 'TBD'}"],
            ["Tax Identification No. (TIN)", sig.tin or "TBD"],
        ],
        col_weights=[32, 68],
    )

    if rep.readiness_warnings:
        add_sub("OBO Permit Readiness Notices")
        for w_msg in rep.readiness_warnings:
            story.append(Paragraph(_safe_pdf_text(f"- {w_msg}"), st_note))

    story.append(PageBreak())

    # =========================================================================
    # SECTION 1: DESIGN CRITERIA & MATERIALS
    # =========================================================================
    add_section("Section 1 - Design Criteria & Material Specifications")
    story.append(
        Paragraph(
            "Structural analysis and reinforced concrete member design conform to the National Structural "
            "Code of the Philippines (NSCP 2015, Volume 1, 7th Edition), ACI 318 strength design provisions, "
            "and the National Building Code of the Philippines (PD 1096, Section 302-5).",
            st_body,
        )
    )
    add_table(
        ["Design Parameter", "Specified Value"],
        [
            ["Concrete Compressive Strength (f'c)", f"{mat.fc_mpa:.1f} MPa"],
            ["Concrete Elastic Modulus (Ec = 4700*sqrt(f'c))", f"{mat.ec_kn_m2:,.1f} kN/m² ({mat.ec_kn_m2 / 1000.0:,.1f} MPa)"],
            ["Reinforced Concrete Density", f"{mat.density_kn_m3:.2f} kN/m³"],
            ["Main Rebar Yield Strength (PNS 49 Grade 60, fy)", f"{mat.fy_main_mpa:.1f} MPa (D16mm and larger)"],
            ["Ties / Stirrups Yield Strength (PNS 49 Grade 40, fyt)", f"{mat.fy_tie_mpa:.1f} MPa (D10-D12mm)"],
            ["Clear Covers (Beam / Column / Footing)", f"{dp.clear_cover_beam_mm:.0f} mm / {dp.clear_cover_column_mm:.0f} mm / {dp.clear_cover_footing_mm:.0f} mm"],
            ["Allowable Soil Bearing Capacity (qa)", f"{dp.soil_bearing_qa_kpa:.1f} kPa (soil unit wt = {dp.soil_unit_weight_kn_m3:.1f} kN/m³)"],
            ["Footing Embedment / Pedestal Drop", f"{dp.footing_embedment_m:.2f} m embedment (Model pedestal drop = {sm.options.footing_depth_m:.2f} m)"],
            ["Geotechnical Report Reference (NBCP 302-5)", sig.geotech_report_ref],
        ],
        col_weights=[40, 60],
    )

    # =========================================================================
    # SECTION 2: GEOMETRY & SECTION CATALOG
    # =========================================================================
    add_section("Section 2 - Structural Geometry & Member Section Catalog")
    n_cols = sum(1 for m in sm.members if m.member_type == "column" and not m.is_pedestal)
    n_peds = sum(1 for m in sm.members if m.member_type == "column" and m.is_pedestal)
    n_beams = sum(1 for m in sm.members if m.member_type != "column")
    n_slabs = sum(1 for s in sm.slab_panels if s.slab_action != "void")
    n_supports = sum(1 for n in sm.nodes if n.is_support)

    add_table(
        ["Analytical Model Metric", "Summary"],
        [
            ["Analytical Nodes / Supports", f"{len(sm.nodes)} nodes ({n_supports} {sm.options.default_support_type} supports)"],
            ["Columns / Pedestals", f"{n_cols} columns, {n_peds} pedestals"],
            ["Beams & Tie Beams", f"{n_beams} frame beam members"],
            ["RC Slab Panels", f"{n_slabs} active panels ({sum(s.net_area_sqm for s in sm.slab_panels if s.slab_action != 'void'):.2f} m² net)"],
        ],
        col_weights=[36, 64],
    )

    add_sub("2.1 Story Elevations")
    sorted_lvls = sorted(project.levels, key=lambda lv: lv.elevation_m)
    lvl_rows = [
        [
            lv.id,
            f"{lv.elevation_m:.2f}",
            f"{(lv.elevation_m - sorted_lvls[idx - 1].elevation_m):.2f}" if idx > 0 else "Base",
        ]
        for idx, lv in enumerate(sorted_lvls)
    ]
    add_table(["Floor Level", "Elevation (m)", "Story Height (m)"], lvl_rows, col_weights=[50, 25, 25], right_cols={1, 2})

    add_sub("2.2 Preliminary Section Catalog & Cracked Modifiers")
    sec_rows = [
        [
            s.section_id,
            s.label,
            s.category.replace("_", " ").title(),
            f"{s.width_mm:.0f} x {s.depth_mm:.0f}",
            f"{s.cracked_ix:.2f} / {s.cracked_iy:.2f} / {s.cracked_iz:.2f}",
        ]
        for s in ms.sections
    ]
    add_table(
        ["Tag", "Label", "Category", "b x h (mm)", "Cracked Ix / Iy / Iz"],
        sec_rows,
        col_weights=[10, 32, 22, 16, 20],
        right_cols={3, 4},
    )

    # =========================================================================
    # SECTION 3: NSCP 2015 LOADS
    # =========================================================================
    add_section("Section 3 - NSCP 2015 Gravity, Seismic (Sec. 208) & Wind (Sec. 207) Loads")

    if lg.floor_loads:
        add_sub("3.1 Floor Gravity Loads (kPa / kN/m)")
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
        add_table(
            ["Floor", "Occupancy", "Finish", "MEP", "Part.", "Total SDL", "LL", "Ext. Wall"],
            fl_rows,
            col_weights=[20, 18, 9, 9, 9, 12, 10, 13],
            right_cols={2, 3, 4, 5, 6, 7},
        )

    ss = lg.seismic_summary
    if ss is not None:
        add_sub("3.2 Seismic Base Shear & Story Distribution (NSCP Sec. 208.5)")
        sp = lg.seismic
        add_table(
            ["Seismic Parameter", "Computed Value"],
            [
                ["Zone / Soil Profile", f"Zone {sp.zone} (Z = {ss.z_factor:.2f}) / Soil {sp.soil_profile}"],
                ["Near-Source Factors (Na, Nv)", f"Source {sp.seismic_source_type} @ {sp.fault_distance_km:.1f} km -> Na = {ss.na:.2f}, Nv = {ss.nv:.2f}"],
                ["Seismic Coefficients (Ca, Cv)", f"Ca = {ss.ca:.3f}, Cv = {ss.cv:.3f}"],
                ["R / I / rho", f"Rx = {sp.r_factor_x:.1f}, Rz = {sp.r_factor_z:.1f}, I = {sp.importance_factor_i:.2f}, rho = {sp.redundancy_rho:.2f}"],
                ["Height hn / Period T", f"hn = {ss.building_height_m:.2f} m, T = {ss.period_t_sec:.3f} s"],
                ["Total Seismic Weight (W)", f"{ss.total_seismic_weight_kn:,.2f} kN"],
                ["Design Base Shear (Vx / Vz)", f"Vx = {ss.v_design_x_kn:,.2f} kN ({ss.base_shear_ratio_x * 100:.2f}% W) | Vz = {ss.v_design_z_kn:,.2f} kN ({ss.base_shear_ratio_z * 100:.2f}% W)"],
            ],
            col_weights=[36, 64],
        )
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
            add_table(
                ["Story", "Elev (m)", "Wi (kN)", "Fx (kN)", "Fz (kN)", "Cum Vx (kN)", "Cum Vz (kN)"],
                st_rows,
                col_weights=[22, 11, 14, 13, 13, 13.5, 13.5],
                right_cols={1, 2, 3, 4, 5, 6},
            )

    ws = lg.wind_summary
    if ws is not None:
        add_sub("3.3 Wind Load Distribution (NSCP Sec. 207)")
        wp = lg.wind
        story.append(
            Paragraph(
                _safe_pdf_text(
                    f"V = {wp.basic_wind_speed_kph:.0f} kph | Exposure {wp.exposure_category} | "
                    f"Kzt = {wp.topographic_kzt:.2f} | Kd = {wp.directionality_kd:.2f} | G = {wp.gust_factor_g:.2f} | "
                    f"Wind Base Shear: Wx = {ws.base_shear_x_kn:.2f} kN, Wz = {ws.base_shear_z_kn:.2f} kN."
                ),
                st_body,
            )
        )
        if ws.stories:
            w_rows = [
                [st.floor_level, f"{st.elevation_m:.2f}", f"{st.kz:.3f}", f"{st.qz_kpa:.3f}", f"{st.fx_kn:.2f}", f"{st.fz_kn:.2f}"]
                for st in ws.stories
            ]
            add_table(
                ["Story", "Elev (m)", "Kz", "qz (kPa)", "Wind Fx (kN)", "Wind Fz (kN)"],
                w_rows,
                col_weights=[26, 14, 12, 14, 17, 17],
                right_cols={1, 2, 3, 4, 5},
            )

    if opts.include_load_combinations and lg.combinations:
        add_sub("3.4 LRFD & ASD Load Combinations (NSCP Sec. 203)")
        c_rows = [
            [str(c.combo_id), c.design_basis, c.name, ", ".join(f"{k}:{v:+.2f}" for k, v in c.factors.items())]
            for c in lg.combinations
        ]
        add_table(["ID", "Basis", "Combination Name", "Factors"], c_rows, col_weights=[8, 10, 38, 44])

    # =========================================================================
    # SECTION 4: ANALYSIS, DRIFT & REACTIONS
    # =========================================================================
    add_section("Section 4 - Analysis Verification, Drift Checks & Support Reactions")
    src_label = (
        f"STAAD.Pro .anl Output ({ad.source_filename})"
        if ad.source == "staad_anl"
        else "Built-in 3D Direct Stiffness Method (DSM) Elastic Solver"
    )
    story.append(Paragraph(_safe_pdf_text(f"Analysis Source: {src_label}"), st_body))

    if ad.story_drifts:
        add_sub("4.1 Story Drift & Soft-Story Verification (NSCP Sec. 208.6.4)")
        dr_rows = [
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
        add_table(
            ["Story", "h (m)", "dS X/Z (mm)", "dM X/Z (mm)", "Allow (mm)", "Ratio", "Soft", "Status"],
            dr_rows,
            col_weights=[18, 9, 16, 16, 12, 10, 8, 11],
            right_cols={1, 2, 3, 4, 5},
            status_col=7,
        )

    if ad.support_reactions:
        add_sub("4.2 Support Reaction Envelopes (ASD & LRFD)")
        rx_rows = [
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
        add_table(
            ["Node", "Grid", "Level", "ASD Max (kN)", "ASD Min (kN)", "LRFD Pu", "LRFD Mu", "LRFD V"],
            rx_rows,
            col_weights=[9, 10, 18, 13, 13, 13, 12, 12],
            right_cols={3, 4, 5, 6, 7},
        )

    if ad.pinned_design_views:
        add_sub("4.3 Visual Design Verification & Utilization Maps")
        for snap in ad.pinned_design_views:
            title_txt = snap.get("title") or "Pinned Design View"
            caption_txt = snap.get("caption") or ""
            data_url = snap.get("image_data_url") or ""
            story.append(Paragraph(_safe_pdf_text(f"<b>{title_txt}</b>"), st_body))
            if "," in data_url:
                try:
                    raw_img = base64.b64decode(data_url.split(",", 1)[1])
                    if raw_img:
                        img = RLImage(io.BytesIO(raw_img))
                        max_w = avail_w * 0.92
                        max_h = 95 * mm
                        iw, ih = float(img.imageWidth or 1.0), float(img.imageHeight or 1.0)
                        scale = min(max_w / max(iw, 1.0), max_h / max(ih, 1.0))
                        img.drawWidth = iw * scale
                        img.drawHeight = ih * scale
                        story.append(img)
                except Exception:
                    pass
            if caption_txt:
                story.append(Paragraph(_safe_pdf_text(caption_txt), st_note))
            story.append(Spacer(1, 4))

    # =========================================================================
    # SECTION 5: RC MEMBER DESIGN SCHEDULES
    # =========================================================================
    add_section("Section 5 - Reinforced Concrete Member Design Schedules")
    beam_pass = sum(1 for b in ad.beam_designs if b.status == "PASS")
    col_pass = sum(1 for c in ad.column_designs if c.status == "PASS")
    ftg_pass = sum(1 for f in ad.footing_designs if f.status == "PASS")
    add_table(
        ["Element Group", "Pass / Total", "Max Governing DCR", "Status"],
        [
            ["Beams & Tie Beams", f"{beam_pass} / {len(ad.beam_designs)}", f"{max((b.governing_dcr for b in ad.beam_designs), default=0.0):.3f}", "PASS" if beam_pass == len(ad.beam_designs) else "FAIL"],
            ["Columns & Pedestals", f"{col_pass} / {len(ad.column_designs)}", f"{max((c.governing_dcr for c in ad.column_designs), default=0.0):.3f}", "PASS" if col_pass == len(ad.column_designs) else "FAIL"],
            ["Isolated Footings", f"{ftg_pass} / {len(ad.footing_designs)}", f"{max((f.governing_dcr for f in ad.footing_designs), default=0.0):.3f}", "PASS" if ftg_pass == len(ad.footing_designs) else "FAIL"],
        ],
        col_weights=[38, 22, 24, 16],
        right_cols={1, 2},
        status_col=3,
    )

    if opts.include_full_member_schedules:
        if ad.beam_designs:
            add_sub("5.1 Beam Reinforcement Schedule")
            b_rows = [
                [
                    f"M{b.member_id}",
                    b.section_id,
                    b.floor_level,
                    f"{b.width_mm:.0f}x{b.depth_mm:.0f}",
                    f"{b.mu_neg_start_knm:.1f}/{b.mu_pos_mid_knm:.1f}",
                    b.bars_top_start,
                    b.bars_bot_mid,
                    b.stirrups_support,
                    f"{b.governing_dcr:.2f}",
                    b.status,
                ]
                for b in ad.beam_designs
            ]
            add_table(
                ["ID", "Sec", "Floor", "bxh", "Mu-/+", "Top", "Bot", "Stirrups", "DCR", "Status"],
                b_rows,
                col_weights=[7, 7, 13, 10, 12, 10, 10, 15, 7, 9],
                right_cols={4, 8},
                status_col=9,
            )

        if ad.column_designs:
            add_sub("5.2 Column & Pedestal Reinforcement Schedule")
            c_rows = [
                [
                    f"M{c.member_id}",
                    c.section_id,
                    c.floor_level,
                    f"{c.width_mm:.0f}x{c.depth_mm:.0f}",
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
            add_table(
                ["ID", "Sec", "Floor", "bxh", "Pu", "Mu", "Main Bars", "rho", "Ties", "DCR", "Status"],
                c_rows,
                col_weights=[7, 6, 12, 9, 9, 8, 10, 8, 15, 7, 9],
                right_cols={4, 5, 7, 9},
                status_col=10,
            )

        if ad.footing_designs:
            add_sub("5.3 Isolated Footing Schedule")
            f_rows = [
                [
                    f.footing_tag,
                    f"N{f.node_id} ({f.grid_ref or '-'})",
                    f"{f.b_m:.2f}x{f.l_m:.2f}x{f.thickness_mm:.0f}",
                    f"{f.p_service_kn:.1f}",
                    f"{f.q_actual_kpa:.1f}/{f.q_allow_kpa:.0f}",
                    f"{f.punching_shear_dcr:.2f}",
                    f.bottom_bars_x,
                    f"{f.governing_dcr:.2f}",
                    f.status,
                ]
                for f in ad.footing_designs
            ]
            add_table(
                ["Tag", "Node", "BxLxt", "Pserv", "q/qa", "Punch", "Bottom Bars", "DCR", "Status"],
                f_rows,
                col_weights=[6, 13, 15, 10, 12, 8, 20, 7, 9],
                right_cols={3, 4, 5, 7},
                status_col=8,
            )

        if ad.smrf_joint_checks:
            add_sub("5.4 NSCP Sec. 418 SMRF Joint & SCWB Summary")
            scwb_pass = sum(1 for j in ad.smrf_joint_checks if j.scwb_status == "PASS")
            js_pass = sum(1 for j in ad.smrf_joint_checks if j.joint_shear_status == "PASS")
            story.append(
                Paragraph(
                    _safe_pdf_text(
                        f"Evaluated {len(ad.smrf_joint_checks)} beam-column joint(s): "
                        f"{scwb_pass}/{len(ad.smrf_joint_checks)} PASS SCWB (>= 1.20, NSCP Sec. 418.7.3) | "
                        f"{js_pass}/{len(ad.smrf_joint_checks)} PASS Joint Shear (NSCP Sec. 418.8.4)."
                    ),
                    st_note,
                )
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
            add_table(
                ["Node", "Level", "Grid", "Sum Mnc", "Sum Mnb", "SCWB", "Shear DCR", "Status"],
                j_rows,
                col_weights=[9, 14, 13, 13, 13, 12, 14, 12],
                right_cols={3, 4, 5, 6},
                status_col=7,
            )

        if (
            ad.retaining_wall_designs
            or ad.slab_designs
            or ad.stair_designs
            or ad.strap_footing_designs
            or ad.roof_truss_designs
        ):
            add_sub("5.5 Secondary Elements & Retaining Walls")
            if ad.retaining_wall_designs:
                rw_rows = [
                    [
                        r.wall_id,
                        f"{r.retained_height_m:.2f}x{r.total_length_m:.1f}m",
                        f"{r.ka:.2f}/{r.k_ae_seismic:.2f}",
                        f"{r.fs_overturning:.2f}/{r.fs_sliding:.2f}",
                        f"{r.max_toe_bearing_kpa:.0f}",
                        r.stem_vertical_bars,
                        r.base_heel_toe_bars,
                        r.status,
                    ]
                    for r in ad.retaining_wall_designs
                ]
                add_table(
                    ["Wall", "H x L", "Ka/Kae", "FS OT/Sl", "q_toe", "Stem Vert", "Base Bars", "Status"],
                    rw_rows,
                    col_weights=[9, 13, 11, 12, 9, 18, 18, 10],
                    right_cols={2, 3, 4},
                    status_col=7,
                )
            if ad.slab_designs:
                sl_rows = [
                    [
                        s.panel_id,
                        s.floor_level,
                        f"{s.lx_m:.2f}x{s.ly_m:.2f}",
                        f"{s.thickness_mm:.0f}",
                        s.short_span_cont_neg,
                        s.short_span_pos,
                        s.long_span_pos,
                        s.status,
                    ]
                    for s in ad.slab_designs[:24]
                ]
                add_table(
                    ["Panel", "Floor", "Lx x Ly", "t", "Short (-)", "Short (+)", "Long (+)", "Status"],
                    sl_rows,
                    col_weights=[11, 10, 12, 7, 17, 17, 16, 10],
                    right_cols={3},
                    status_col=7,
                )
            if ad.stair_designs:
                st_rows = [
                    [
                        s.stair_id,
                        f"{s.flight_span_m:.2f}m",
                        f"{s.riser_mm:.0f}/{s.tread_mm:.0f}",
                        f"{s.waist_thickness_mm:.0f}mm",
                        f"{s.mu_knm_per_m:.1f}",
                        s.main_bars,
                        s.dist_bars,
                        s.deflection_status,
                    ]
                    for s in ad.stair_designs
                ]
                add_table(
                    ["Stair", "Span", "R/T", "Waist", "Mu", "Main Bars", "Dist Bars", "Defl"],
                    st_rows,
                    col_weights=[9, 10, 11, 10, 10, 20, 20, 10],
                    right_cols={4},
                    status_col=7,
                )
            if ad.roof_truss_designs:
                tr_rows = [
                    [
                        t.truss_id,
                        f"{t.truss_type} {t.span_m:.1f}m",
                        t.top_chord_section,
                        t.bottom_chord_section,
                        t.web_section,
                        f"{t.max_chord_dcr:.2f}/{t.max_web_dcr:.2f}",
                        f"{t.estimated_steel_kg:.0f}kg",
                        t.status,
                    ]
                    for t in ad.roof_truss_designs
                ]
                add_table(
                    ["Truss", "Type/Span", "Top", "Bot", "Web", "DCR C/W", "Steel", "Status"],
                    tr_rows,
                    col_weights=[9, 14, 14, 14, 14, 13, 12, 10],
                    right_cols={5, 6},
                    status_col=7,
                )
            if ad.strap_footing_designs:
                sf_rows = [
                    [
                        s.strap_id,
                        f"N{s.exterior_node_id}->N{s.interior_node_id}",
                        f"{s.ext_pad_b_m:.2f}x{s.ext_pad_l_m:.2f}",
                        f"{s.int_pad_b_m:.2f}x{s.int_pad_l_m:.2f}",
                        f"{s.strap_b_mm:.0f}x{s.strap_h_mm:.0f}",
                        s.strap_top_bars,
                        s.strap_stirrups,
                        s.status,
                    ]
                    for s in ad.strap_footing_designs
                ]
                add_table(
                    ["Strap", "Nodes", "Ext Pad", "Int Pad", "Beam", "Top Bars", "Stirrups", "Status"],
                    sf_rows,
                    col_weights=[9, 13, 13, 13, 13, 13, 16, 10],
                    status_col=7,
                )

    # =========================================================================
    # SECTION 6: BOM & COST ESTIMATE
    # =========================================================================
    if opts.include_bom_cost_estimate:
        add_section("Section 6 - Structural Bill of Materials & Cost Estimate (NBCP Sec. 302-5f)")
        story.append(
            Paragraph(
                _safe_pdf_text(
                    "Rebar mass follows PNS 49:2020 (d²/162 kg/m). Commercial bar pieces rounded up to "
                    f"{rep.bom_params.commercial_bar_length_m:g} m standard stock lengths."
                ),
                st_note,
            )
        )

        add_sub("6.1 Concrete & Formwork Takeoff")
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
        add_table(
            ["Element Group", "Members", "Net Vol (m³)", f"Gross (+{rep.bom_params.concrete_waste_pct:.0f}%)", "Formwork (m²)"],
            conc_rows,
            col_weights=[34, 14, 17, 18, 17],
            right_cols={1, 2, 3, 4},
            bold_last=True,
        )

        add_sub("6.2 PNS 49:2020 Reinforcing Steel Schedule")
        rb_rows = [
            [
                f"D{r.diameter_mm} mm",
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
        add_table(
            ["Bar Size", "Grade", "kg/m", "Net (m)", "Gross (m)", "Weight (kg)", f"Pcs ({rep.bom_params.commercial_bar_length_m:g}m)"],
            rb_rows,
            col_weights=[11, 23, 10, 14, 14, 14, 14],
            right_cols={2, 3, 4, 5, 6},
            bold_last=True,
        )

        add_sub("6.3 Structural Cost Estimate Summary (PHP)")
        cost_rows = [
            [
                c.item_no,
                c.description,
                f"{c.quantity:,.2f}",
                c.unit,
                f"PHP {c.unit_cost_php:,.2f}",
                f"PHP {c.material_cost_php:,.2f}",
                f"PHP {c.labor_cost_php:,.2f}",
                f"PHP {c.total_cost_php:,.2f}",
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
                f"PHP {bom.total_material_cost_php:,.2f}",
                f"PHP {bom.total_labor_cost_php:,.2f}",
                f"PHP {bom.grand_total_cost_php:,.2f}",
            ]
        )
        add_table(
            ["Item", "Description", "Qty", "Unit", "Unit Rate", "Material", "Labor", "Total"],
            cost_rows,
            col_weights=[6, 30, 9, 6, 12, 12, 12, 13],
            right_cols={2, 4, 5, 6, 7},
            bold_last=True,
        )

    if opts.include_staad_snippet:
        add_section("Appendix A - STAAD.Pro Input Excerpt")
        std_lines = build_staad_text(project).splitlines()
        snippet = "\n".join(std_lines[:60])
        story.append(Preformatted(_safe_pdf_text(snippet), st_code))

    # =========================================================================
    # SECTION 7: PROFESSIONAL CERTIFICATION & DRY SEAL BLOCK
    # =========================================================================
    add_section("Section 7 - Professional Certification & Dry Seal Block (NBCP PD 1096)")
    story.append(
        Paragraph(
            "I hereby certify that the structural analysis, design calculations, reinforcement schedules, "
            "and structural bill of materials contained herein have been prepared in accordance with the "
            "National Structural Code of the Philippines (NSCP 2015, Volume 1, 7th Edition) and the "
            "National Building Code of the Philippines (PD 1096), under my direct professional supervision "
            "and engineering verification.",
            st_body,
        )
    )
    add_table(
        ["Certification Field", "Engineer Attestation & Credentials"],
        [
            ["Signature over Printed Name", f"____________________________________   {sig.engineer_name or '[Engineer Name]'} ({sig.title})"],
            ["PRC License No. / Validity", f"{sig.prc_license_no or 'TBD'}  (Valid: {sig.prc_validity or 'TBD'})"],
            ["PTR No. / Date / Place", f"{sig.ptr_no or 'TBD'} / {sig.ptr_date or 'TBD'} / {sig.ptr_place or 'TBD'}"],
            ["Tax Identification No. (TIN)", sig.tin or "TBD"],
            ["Professional Dry Seal", "[ AFFIX DRY SEAL HERE ]"],
        ],
        col_weights=[32, 68],
    )

    canvas_cls = _make_numbered_canvas_class(
        header_left=f"{pi.project_name} ({pi.project_id})",
        footer_left=opts.report_revision,
    )
    doc.build(story, canvasmaker=canvas_cls)
    return buf.getvalue()
