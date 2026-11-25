import math
from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


class ProjectMaterialProperties(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fc_mpa: float = Field(gt=0)
    fy_mpa: float = Field(gt=0)
    concrete_density_kn_m3: float = Field(gt=0)


class ProjectInfo(BaseModel):
    project_id: str = Field(min_length=1, max_length=80)
    project_name: str = Field(min_length=1, max_length=200)
    location: str = ""
    design_code: str = "To be confirmed by engineer"
    materials: ProjectMaterialProperties


class Level(BaseModel):
    id: str = Field(min_length=1)
    elevation_m: float


class GridLine(BaseModel):
    label: str
    coord_m: float


class Grids(BaseModel):
    x_grids: list[GridLine] = Field(default_factory=list)
    y_grids: list[GridLine] = Field(default_factory=list)


class Column(BaseModel):
    id: str
    grid: str = ""
    floor_level: str = ""
    x_m: float
    y_m: float
    width_mm: float = Field(gt=0)
    depth_mm: float = Field(gt=0)


class Beam(BaseModel):
    id: str
    start_col: str
    end_col: str
    level: str
    width_mm: float = Field(gt=0)
    depth_mm: float = Field(gt=0)
    span_m: float | None = Field(default=None, gt=0)


class FloorOpening(BaseModel):
    id: str
    label: str
    category: Literal["stairwell", "airwell", "shaft_other"]
    floor_level: str = ""
    polygon_m: list[list[float]] = Field(min_length=3)
    area_sqm: float = Field(gt=0)


class AnalyticalNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: int = Field(ge=1)
    x_m: float
    y_m: float  # STAAD Y-up elevation
    z_m: float  # plan Y
    floor_level: str
    grid_ref: str = ""
    is_support: bool = False
    support_type: Literal["NONE", "FIXED", "PINNED"] = "NONE"


class AnalyticalMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: int = Field(ge=1)
    start_node: int = Field(ge=1)
    end_node: int = Field(ge=1)
    member_type: Literal["column", "primary_beam", "secondary_beam", "tie_beam", "trimmer_beam"]
    floor_level: str
    grid_ref: str = ""
    length_m: float = Field(ge=0.0)
    manual_override: bool = False
    is_pedestal: bool = False


class SlabPanel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    panel_id: str
    floor_level: str
    polygon_m: list[list[float]] = Field(min_length=3)
    lx_m: float = Field(gt=0)
    ly_m: float = Field(gt=0)
    gross_area_sqm: float = Field(ge=0)
    opening_deduction_sqm: float = Field(ge=0)
    net_area_sqm: float = Field(ge=0)
    slab_action: Literal["one_way", "two_way", "void"]


class GeometryCheckIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    floor_level: str = ""
    entity_ids: list[str] = Field(default_factory=list)


class GeometryGenerationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snap_tolerance_m: float = Field(default=0.05, gt=0)
    default_support_type: Literal["FIXED", "PINNED"] = "FIXED"
    include_base_tie_beams: bool = True
    max_beam_span_m: float = Field(default=12.0, gt=0)
    skip_beams_through_openings: bool = True
    auto_detect_slab_panels: bool = True
    footing_depth_m: float = Field(default=0.0, ge=0.0, le=6.0)


class ManualBeamOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    override_id: str
    floor_level: str
    start_xyz_m: list[float] = Field(min_length=3, max_length=3)
    end_xyz_m: list[float] = Field(min_length=3, max_length=3)
    member_type: Literal["primary_beam", "secondary_beam", "tie_beam", "trimmer_beam"] = "primary_beam"
    grid_ref: str = "MANUAL"


class StructuralModelState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: str | None = None
    options: GeometryGenerationOptions = Field(default_factory=GeometryGenerationOptions)
    nodes: list[AnalyticalNode] = Field(default_factory=list)
    members: list[AnalyticalMember] = Field(default_factory=list)
    manual_beams: list[ManualBeamOverride] = Field(default_factory=list)
    slab_panels: list[SlabPanel] = Field(default_factory=list)
    validation_issues: list[GeometryCheckIssue] = Field(default_factory=list)
    engineer_approved: bool = False


class MaterialProperties(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "CONCRETE"
    fc_mpa: float = Field(default=28.0, gt=0)
    fy_main_mpa: float = Field(default=415.0, gt=0)
    fy_tie_mpa: float = Field(default=275.0, gt=0)
    ec_kn_m2: float = Field(default=24870062.0, gt=0)
    poisson: float = Field(default=0.17, gt=0, lt=0.5)
    density_kn_m3: float = Field(default=23.54, gt=0)
    alpha: float = Field(default=1e-5, gt=0)
    damp: float = Field(default=0.05, ge=0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def auto_compute_ec(cls, data: Any):
        if isinstance(data, dict) and "fc_mpa" in data and "ec_kn_m2" not in data:
            try:
                fc = float(data["fc_mpa"])
                if fc > 0:
                    updated = dict(data)
                    updated["ec_kn_m2"] = round(4700.0 * math.sqrt(fc) * 1000.0, 2)
                    return updated
            except (TypeError, ValueError):
                pass
        return data


class SectionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    category: Literal["column", "primary_beam", "secondary_beam", "tie_beam", "trimmer_beam"]
    width_mm: float
    depth_mm: float
    material_name: str = "CONCRETE"
    color_hex: str = "#38bdf8"
    cracked_ix: float = Field(default=0.70, gt=0, le=1.0)
    cracked_iy: float = Field(default=0.70, gt=0, le=1.0)
    cracked_iz: float = Field(default=0.70, gt=0, le=1.0)


class MemberPropertyAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: int = Field(ge=1)
    section_id: str = Field(min_length=1)
    beta_angle_deg: float = 0.0
    release_start_moment: bool = False
    release_end_moment: bool = False
    manual_override: bool = False


class SlabPropertyAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    panel_id: str = Field(min_length=1)
    floor_level: str
    slab_tag: str = "S1"
    thickness_mm: float = Field(default=125.0, ge=0.0)
    min_code_thickness_mm: float = Field(default=100.0, ge=0.0)
    manual_override: bool = False


class SizingOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seismic_smrf_zone4: bool = True
    include_cracked_modifiers: bool = True
    pin_secondary_beams: bool = True
    beam_span_depth_ratio: float = Field(default=12.0, gt=2.0, le=30.0)
    round_increment_mm: int = Field(default=50, ge=10, le=200)


class PropertyCheckIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    entity_ids: list[str] = Field(default_factory=list)


class MemberSizingState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: str | None = None
    material: MaterialProperties = Field(default_factory=MaterialProperties)
    options: SizingOptions = Field(default_factory=SizingOptions)
    sections: list[SectionDefinition] = Field(default_factory=list)
    member_assignments: list[MemberPropertyAssignment] = Field(default_factory=list)
    slab_assignments: list[SlabPropertyAssignment] = Field(default_factory=list)
    validation_issues: list[PropertyCheckIssue] = Field(default_factory=list)
    engineer_approved: bool = False


class FloorGravityLoad(BaseModel):
    model_config = ConfigDict(extra="forbid")
    floor_level: str = Field(min_length=1)
    occupancy_preset: str = "residential"
    sdl_finish_kpa: float = Field(default=1.20, ge=0.0)
    sdl_ceiling_mep_kpa: float = Field(default=0.50, ge=0.0)
    sdl_partitions_kpa: float = Field(default=1.00, ge=0.0)
    live_load_kpa: float = Field(default=1.90, ge=0.0)
    is_roof: bool = False
    exterior_wall_line_kn_m: float = Field(default=7.50, ge=0.0)
    interior_wall_line_kn_m: float = Field(default=4.50, ge=0.0)
    apply_exterior_wall_perimeter: bool = True
    apply_interior_wall_beams: bool = False


class SeismicParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zone: Literal[2, 4] = 4
    soil_profile: Literal["SA", "SB", "SC", "SD", "SE"] = "SD"
    occupancy_category: Literal[
        "I_ESSENTIAL", "II_HAZARDOUS", "III_SPECIAL", "IV_STANDARD", "V_MISC"
    ] = "IV_STANDARD"
    importance_factor_i: float = Field(default=1.0, gt=0.0)
    seismic_source_type: Literal["A", "B", "C"] = "A"
    fault_distance_km: float = Field(default=5.0, ge=0.0)
    r_factor_x: float = Field(default=8.5, gt=0.0)
    r_factor_z: float = Field(default=8.5, gt=0.0)
    ct_coeff: float = Field(default=0.0731, gt=0.0)
    redundancy_rho: float = Field(default=1.0, ge=1.0, le=1.5)
    include_vertical_ev: bool = True
    live_load_seismic_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    staad_seismic_method: Literal["ubc_auto", "explicit_story_forces"] = "ubc_auto"


class WindParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    basic_wind_speed_kph: float = Field(default=250.0, gt=0.0)
    exposure_category: Literal["B", "C", "D"] = "B"
    topographic_kzt: float = Field(default=1.0, ge=1.0)
    directionality_kd: float = Field(default=0.85, gt=0.0, le=1.0)
    gust_factor_g: float = Field(default=0.85, gt=0.0)
    cp_windward: float = Field(default=0.80, gt=0.0)
    cp_leeward_x: float = Field(default=-0.50)
    cp_leeward_z: float = Field(default=-0.50)


class BeamDistributedLoad(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: int = Field(ge=1)
    load_case: Literal["DL", "LL", "LR"]
    load_shape: Literal["UNI", "TRAP"]
    w1_kn_m: float
    w2_kn_m: float = 0.0
    d1_m: float = 0.0
    d2_m: float = 0.0
    source: str


class StorySeismicResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    floor_level: str
    elevation_m: float
    weight_kn: float
    fx_kn: float
    fz_kn: float
    cumulative_shear_x_kn: float
    cumulative_shear_z_kn: float


class StoryWindResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    floor_level: str
    elevation_m: float
    kz: float
    qz_kpa: float
    fx_kn: float
    fz_kn: float


class SeismicSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    z_factor: float = 0.40
    na: float = 1.0
    nv: float = 1.0
    ca: float = 0.44
    cv: float = 0.64
    building_height_m: float = 0.0
    period_t_sec: float = 0.0
    total_seismic_weight_kn: float = 0.0
    v_design_x_kn: float = 0.0
    v_design_z_kn: float = 0.0
    v_max_kn: float = 0.0
    v_min_kn: float = 0.0
    v_min_z4_kn: float = 0.0
    ft_x_kn: float = 0.0
    ft_z_kn: float = 0.0
    base_shear_ratio_x: float = 0.0
    base_shear_ratio_z: float = 0.0
    stories: list[StorySeismicResult] = Field(default_factory=list)


class WindSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_shear_x_kn: float = 0.0
    base_shear_z_kn: float = 0.0
    stories: list[StoryWindResult] = Field(default_factory=list)


class LoadCombinationDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    combo_id: int = Field(ge=1)
    name: str = Field(min_length=1)
    design_basis: Literal["LRFD", "ASD"]
    factors: dict[str, float] = Field(default_factory=dict)


class LoadGenerationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_rigid_diaphragm: bool = True
    use_repeat_load_combos: bool = False
    include_lrfd_combos: bool = True
    include_asd_combos: bool = True
    f1_live_load_factor: float = Field(default=0.5, ge=0.0, le=1.0)


class LoadCheckIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    floor_level: str = ""


class LoadGenerationState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: str | None = None
    floor_loads: list[FloorGravityLoad] = Field(default_factory=list)
    seismic: SeismicParameters = Field(default_factory=SeismicParameters)
    wind: WindParameters = Field(default_factory=WindParameters)
    options: LoadGenerationOptions = Field(default_factory=LoadGenerationOptions)
    beam_loads: list[BeamDistributedLoad] = Field(default_factory=list)
    seismic_summary: SeismicSummary | None = None
    wind_summary: WindSummary | None = None
    combinations: list[LoadCombinationDef] = Field(default_factory=list)
    validation_issues: list[LoadCheckIssue] = Field(default_factory=list)
    engineer_approved: bool = False


class DesignParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clear_cover_beam_mm: float = Field(default=40.0, gt=0.0)
    clear_cover_column_mm: float = Field(default=40.0, gt=0.0)
    clear_cover_footing_mm: float = Field(default=75.0, gt=0.0)
    main_bar_dia_beam_mm: int = Field(default=16, ge=10, le=36)
    main_bar_dia_col_mm: int = Field(default=20, ge=12, le=36)
    stirrup_tie_dia_mm: int = Field(default=10, ge=8, le=16)
    soil_bearing_qa_kpa: float = Field(default=150.0, gt=0.0)
    footing_embedment_m: float = Field(default=1.50, ge=0.0)
    soil_unit_weight_kn_m3: float = Field(default=18.0, gt=0.0)
    include_staad_concrete_design: bool = True


class StoryDriftResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    floor_level: str
    elevation_m: float
    story_height_m: float
    elastic_disp_x_mm: float
    elastic_disp_z_mm: float
    delta_s_x_mm: float
    delta_s_z_mm: float
    delta_m_x_mm: float
    delta_m_z_mm: float
    allowable_drift_mm: float
    drift_ratio_x: float
    drift_ratio_z: float
    stiffness_x_kn_m: float = 0.0
    stiffness_z_kn_m: float = 0.0
    soft_story_flag: bool = False
    status: Literal["PASS", "FAIL"]


class SupportReactionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: int = Field(ge=1)
    grid_ref: str = ""
    floor_level: str
    max_asd_fy_kn: float
    min_asd_fy_kn: float
    max_lrfd_pu_kn: float
    max_lrfd_mux_knm: float
    max_lrfd_muz_knm: float
    max_lrfd_v_kn: float
    governing_asd_combo: str = ""
    governing_lrfd_combo: str = ""


class BeamDesignResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: int = Field(ge=1)
    section_id: str
    floor_level: str
    width_mm: float
    depth_mm: float
    length_m: float
    mu_neg_start_knm: float
    mu_pos_mid_knm: float
    mu_neg_end_knm: float
    vu_max_kn: float
    governing_combo: str
    as_req_top_start_mm2: float
    as_req_bot_mid_mm2: float
    as_req_top_end_mm2: float
    bars_top_start: str
    bars_bot_mid: str
    bars_top_end: str
    stirrups_support: str
    stirrups_midspan: str
    flexure_dcr: float
    shear_dcr: float
    governing_dcr: float
    status: Literal["PASS", "FAIL"]
    notes: list[str] = Field(default_factory=list)


class ColumnDesignResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: int = Field(ge=1)
    section_id: str
    floor_level: str
    width_mm: float
    depth_mm: float
    length_m: float
    pu_max_kn: float
    pu_min_kn: float
    mux_max_knm: float
    muy_max_knm: float
    vu_max_kn: float
    governing_combo: str
    rho_provided: float
    main_bars: str
    ties_confined: str
    ties_mid: str
    pm_dcr: float
    shear_dcr: float
    governing_dcr: float
    status: Literal["PASS", "FAIL"]
    notes: list[str] = Field(default_factory=list)


class FootingDesignResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    footing_tag: str
    node_id: int = Field(ge=1)
    grid_ref: str = ""
    base_elevation_m: float
    p_service_kn: float
    pu_kn: float
    b_m: float
    l_m: float
    thickness_mm: float
    q_actual_kpa: float
    q_allow_kpa: float
    bearing_dcr: float
    one_way_shear_dcr: float
    punching_shear_dcr: float
    governing_dcr: float
    bottom_bars_x: str
    bottom_bars_z: str
    status: Literal["PASS", "FAIL"]


class DesignCheckIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    entity_ids: list[str] = Field(default_factory=list)


class AnalysisDesignState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: str | None = None
    source: Literal["internal_dsm", "staad_anl"] = "internal_dsm"
    source_filename: str = ""
    params: DesignParameters = Field(default_factory=DesignParameters)
    story_drifts: list[StoryDriftResult] = Field(default_factory=list)
    support_reactions: list[SupportReactionEnvelope] = Field(default_factory=list)
    beam_designs: list[BeamDesignResult] = Field(default_factory=list)
    column_designs: list[ColumnDesignResult] = Field(default_factory=list)
    footing_designs: list[FootingDesignResult] = Field(default_factory=list)
    validation_issues: list[DesignCheckIssue] = Field(default_factory=list)
    engineer_approved: bool = False


class EngineerSignatory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engineer_name: str = ""
    title: str = "Civil / Structural Engineer"
    prc_license_no: str = ""
    prc_validity: str = ""
    ptr_no: str = ""
    ptr_date: str = ""
    ptr_place: str = ""
    tin: str = ""
    project_owner: str = ""
    project_address: str = ""
    geotech_report_ref: str = (
        "Pending geotechnical / soil boring report (required for >= 3 storeys per NBCP Sec. 302-5)"
    )


class BomCostParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region_preset: Literal["custom", "car_baguio", "ncr_manila", "cebu_visayas", "davao_mindanao"] = "car_baguio"
    concrete_waste_pct: float = Field(default=5.0, ge=0.0, le=50.0)
    rebar_waste_splice_pct: float = Field(default=12.0, ge=0.0, le=50.0)
    commercial_bar_length_m: Literal[6.0, 7.5, 9.0, 10.5, 12.0] = 6.0
    tie_wire_pct_of_rebar: float = Field(default=1.5, ge=0.0, le=10.0)
    rate_concrete_php_m3: float = Field(default=6500.0, ge=0.0)
    rate_rebar_php_kg: float = Field(default=52.0, ge=0.0)
    rate_tie_wire_php_kg: float = Field(default=95.0, ge=0.0)
    rate_formwork_php_m2: float = Field(default=650.0, ge=0.0)
    rate_chb_wall_php_m2: float = Field(default=950.0, ge=0.0)
    labor_pct_of_materials: float = Field(default=35.0, ge=0.0, le=200.0)
    labor_concrete_pct: float | None = Field(default=None, ge=0.0, le=200.0)
    labor_rebar_pct: float | None = Field(default=None, ge=0.0, le=200.0)
    labor_formwork_pct: float | None = Field(default=None, ge=0.0, le=200.0)
    include_chb_masonry: bool = True

    @field_validator("commercial_bar_length_m", mode="before")
    @classmethod
    def coerce_bar_length_float(cls, v: Any):
        if isinstance(v, (int, str)):
            try:
                return float(v)
            except (TypeError, ValueError):
                return v
        return v


class ConcreteQuantityRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    element_group: str
    member_count: int = Field(ge=0)
    net_volume_m3: float = Field(ge=0.0)
    gross_volume_m3: float = Field(ge=0.0)
    formwork_area_m2: float = Field(ge=0.0)


class RebarQuantityRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diameter_mm: int = Field(gt=0)
    grade_label: str
    unit_mass_kg_m: float = Field(gt=0.0)
    net_length_m: float = Field(ge=0.0)
    gross_length_m: float = Field(ge=0.0)
    total_weight_kg: float = Field(ge=0.0)
    commercial_pieces: int = Field(ge=0)


class BomCostLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_no: str
    description: str
    quantity: float = Field(ge=0.0)
    unit: str
    unit_cost_php: float = Field(ge=0.0)
    material_cost_php: float = Field(ge=0.0)
    labor_cost_php: float = Field(ge=0.0)
    total_cost_php: float = Field(ge=0.0)


class BomSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_concrete_net_m3: float = 0.0
    total_concrete_gross_m3: float = 0.0
    total_rebar_kg: float = 0.0
    rebar_ratio_kg_per_m3: float = 0.0
    total_tie_wire_kg: float = 0.0
    total_formwork_m2: float = 0.0
    total_chb_area_m2: float = 0.0
    total_material_cost_php: float = 0.0
    total_labor_cost_php: float = 0.0
    grand_total_cost_php: float = 0.0
    concrete_rows: list[ConcreteQuantityRow] = Field(default_factory=list)
    rebar_rows: list[RebarQuantityRow] = Field(default_factory=list)
    cost_lines: list[BomCostLine] = Field(default_factory=list)


class ReportGenerationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_load_combinations: bool = True
    include_full_member_schedules: bool = True
    include_bom_cost_estimate: bool = True
    include_staad_snippet: bool = False
    report_revision: str = "Rev. 0 — For Building Permit Submission"


class ReportState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: str | None = None
    signatory: EngineerSignatory = Field(default_factory=EngineerSignatory)
    bom_params: BomCostParameters = Field(default_factory=BomCostParameters)
    options: ReportGenerationOptions = Field(default_factory=ReportGenerationOptions)
    bom: BomSummary | None = None
    readiness_warnings: list[str] = Field(default_factory=list)


class CadSheetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet_no: str = Field(min_length=1, max_length=20)
    title: str = Field(min_length=1, max_length=160)
    plan_scale: int | None = Field(default=None, ge=20, le=500)
    items: list[str] = Field(default_factory=list)


class CadDrawingOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet_size: Literal["A1", "A2", "A3"] = "A1"
    plan_scale: int = Field(default=100, ge=20, le=500)
    detail_scale: int = Field(default=25, ge=5, le=100)
    sheet_prefix: str = Field(default="S", min_length=1, max_length=10)
    include_general_notes: bool = True
    include_foundation_plan: bool = True
    include_framing_plans: bool = True
    include_schedules: bool = True
    include_typical_details: bool = True
    preset: Literal["custom", "a1_permit_set", "a3_individual_floors"] = "a1_permit_set"
    dwg_version: Literal["ACAD2010", "ACAD2013", "ACAD2018"] = "ACAD2013"
    sheets: list[CadSheetConfig] = Field(default_factory=list)


class CadSheetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet_no: str
    title: str
    contents: list[str] = Field(default_factory=list)
    entity_count: int = Field(default=0, ge=0)
    overflow: bool = False


class CadDrawingState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: str | None = None
    options: CadDrawingOptions = Field(default_factory=CadDrawingOptions)
    sheets: list[CadSheetSummary] = Field(default_factory=list)
    total_entities: int = Field(default=0, ge=0)
    layers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    available_items: list[dict[str, str]] = Field(default_factory=list)


class ProjectRevisionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=200)
    created_at: str
    notes: str = ""
    node_count: int = Field(default=0, ge=0)
    member_count: int = Field(default=0, ge=0)
    total_concrete_m3: float = Field(default=0.0, ge=0.0)
    total_rebar_kg: float = Field(default=0.0, ge=0.0)
    grand_total_cost_php: float = Field(default=0.0, ge=0.0)
    frozen_state: dict[str, Any] = Field(default_factory=dict)


class ProjectState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_info: ProjectInfo
    levels: list[Level]
    grids: Grids = Field(default_factory=Grids)
    columns: list[Column]
    beams: list[Beam] = Field(default_factory=list)
    openings: list[FloorOpening] = Field(default_factory=list)
    structural_model: StructuralModelState = Field(default_factory=StructuralModelState)
    member_sizing: MemberSizingState = Field(default_factory=MemberSizingState)
    load_generation: LoadGenerationState = Field(default_factory=LoadGenerationState)
    analysis_design: AnalysisDesignState = Field(default_factory=AnalysisDesignState)
    reporting: ReportState = Field(default_factory=ReportState)
    cad_drawings: CadDrawingState = Field(default_factory=CadDrawingState)
    revisions: list[ProjectRevisionSnapshot] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_references(self):
        level_ids = {x.id for x in self.levels}
        col_ids = {x.id for x in self.columns}
        if len(level_ids) != len(self.levels):
            raise ValueError("Level IDs must be unique.")
        if len(col_ids) != len(self.columns):
            raise ValueError("Column IDs must be unique.")
        for beam in self.beams:
            if beam.start_col not in col_ids or beam.end_col not in col_ids:
                raise ValueError(f"{beam.id}: beam references an unknown column.")
            if beam.level not in level_ids:
                raise ValueError(f"{beam.id}: beam references an unknown level.")
            if beam.start_col == beam.end_col:
                raise ValueError(f"{beam.id}: start and end columns must differ.")
        return self
