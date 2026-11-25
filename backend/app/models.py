import math
from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator


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
