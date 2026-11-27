from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator
from ..models import ProjectState

LengthUnit = Literal["mm", "cm", "m", "ft", "in"]
PageClassification = Literal["vector", "scanned", "hybrid"]
SheetType = Literal[
    "cover_toc",
    "lot_site",
    "floor_plan",
    "roof_plan",
    "elevation",
    "section",
    "schedule_detail",
    "unknown",
]
OpeningCategory = Literal["stairwell", "airwell", "shaft_other"]
DetectionMethod = Literal["auto", "vector", "raster", "vlm"]
CandidateSource = Literal["vector", "raster", "manual", "vlm", "cad"]
ColumnFillMode = Literal["shaded_or_hatched", "gray_shaded_only", "any_filled"]


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


UNIT_TO_METERS: dict[str, float] = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "ft": 0.3048,
    "in": 0.0254,
}


class PointPx(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x_px: float
    y_px: float

    @model_validator(mode="before")
    @classmethod
    def accept_xy_aliases(cls, value: Any):
        if isinstance(value, dict) and "x" in value and "y" in value and "x_px" not in value and "y_px" not in value:
            mapped = dict(value)
            mapped["x_px"] = mapped.pop("x")
            mapped["y_px"] = mapped.pop("y")
            return mapped
        return value


Point2D = PointPx


class PageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    width_pt: float = Field(gt=0)
    height_pt: float = Field(gt=0)
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    dpi: int = Field(gt=0)
    classification: PageClassification
    drawing_count: int = Field(ge=0, default=0)
    image_count: int = Field(ge=0, default=0)
    text_word_count: int = Field(ge=0, default=0)
    image_coverage: float = Field(ge=0.0, le=1.0, default=0.0)
    image_filename: str
    sheet_number: str = ""
    sheet_title: str = ""
    sheet_type: SheetType = "unknown"
    viewport_count: int = Field(ge=1, default=1)


PageMetadata = PageInfo


class PlanRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region_id: str
    page_number: int = Field(ge=1)
    name: str = Field(min_length=1)
    floor_level: str = ""
    x_px: float
    y_px: float
    width_px: float = Field(gt=0)
    height_px: float = Field(gt=0)
    origin_px: PointPx | None = None
    auto_detected: bool = True


class RegionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    name: str = Field(min_length=1)
    floor_level: str = ""
    x_px: float
    y_px: float
    width_px: float = Field(gt=0)
    height_px: float = Field(gt=0)
    origin_px: PointPx | None = None


class RegionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    floor_level: str | None = None
    origin_px: PointPx | None = None
    x_px: float | None = None
    y_px: float | None = None
    width_px: float | None = Field(default=None, gt=0)
    height_px: float | None = Field(default=None, gt=0)


class PageUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet_type: SheetType | None = None
    sheet_number: str | None = None
    sheet_title: str | None = None


class ScaleCalibrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p1: PointPx
    p2: PointPx
    known_distance: float = Field(gt=0)
    unit: LengthUnit
    origin_px: PointPx | None = None
    confirmed: bool = False
    notes: str = ""


class ScaleCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    p1: PointPx
    p2: PointPx
    origin_px: PointPx
    known_distance: float = Field(gt=0)
    unit: LengthUnit
    pixel_distance: float = Field(gt=0)
    real_distance_m: float = Field(gt=0)
    meters_per_px: float = Field(gt=0)
    pixels_per_m: float = Field(gt=0)
    confirmed: bool = False
    calibrated_at: str
    confirmed_at: str | None = None
    notes: str = ""


class GridCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    page_number: int = Field(ge=1)
    region_id: str | None = None
    axis: Literal["x", "y"]
    label: str = Field(min_length=1)
    x1_px: float
    y1_px: float
    x2_px: float
    y2_px: float
    coord_px: float
    coord_m: float | None = None
    source: CandidateSource
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    status: ReviewStatus = ReviewStatus.PENDING
    engineer_approved: bool = False
    transferred: bool = False
    notes: str = ""


class ColumnCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    page_number: int = Field(ge=1)
    region_id: str | None = None
    column_id: str = Field(min_length=1)
    grid: str = ""
    x_px: float
    y_px: float
    width_px: float = Field(gt=0, default=20.0)
    height_px: float = Field(gt=0, default=20.0)
    x_m: float | None = None
    y_m: float | None = None
    width_mm: float = Field(gt=0, default=400.0)
    depth_mm: float = Field(gt=0, default=400.0)
    source: CandidateSource
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    status: ReviewStatus = ReviewStatus.PENDING
    engineer_approved: bool = False
    transferred: bool = False
    notes: str = ""


class DetectedLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level_id: str
    name: str
    elevation_m: float
    story_height_m: float | None = None
    page_number: int = Field(ge=1)
    y_px: float
    confidence: float = Field(ge=0.0, le=1.0, default=0.75)
    review_status: ReviewStatus = ReviewStatus.PENDING
    engineer_approved: bool = False
    warnings: list[str] = Field(default_factory=list)
    transferred: bool = False


class LevelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    elevation_m: float
    y_px: float = 0.0
    engineer_approved: bool = True


class LevelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    elevation_m: float | None = None
    story_height_m: float | None = None
    review_status: ReviewStatus | None = None
    engineer_approved: bool | None = None


class MasterGridLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1)
    offset_m: float


class MasterGridDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_region_id: str
    source_page_number: int = Field(ge=1)
    source_pixels_per_meter: float = Field(gt=0)
    grid_x_m: list[MasterGridLine] = Field(default_factory=list)
    grid_y_m: list[MasterGridLine] = Field(default_factory=list)


class LockMasterGridRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_region_id: str


class StampMasterGridRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor_px: Point2D
    clear_existing_grids: bool = True


class DetectedOpening(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opening_id: str
    page_number: int = Field(ge=1)
    region_id: str | None = None
    floor_level: str = ""
    category: OpeningCategory
    label: str = Field(min_length=1)
    polygon_px: list[Point2D] = Field(min_length=3)
    review_status: ReviewStatus = ReviewStatus.APPROVED
    engineer_approved: bool = True
    transferred: bool = False


class OpeningCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    region_id: str | None = None
    floor_level: str = ""
    category: OpeningCategory
    label: str = Field(min_length=1)
    polygon_px: list[Point2D] = Field(min_length=3)
    review_status: ReviewStatus = ReviewStatus.APPROVED
    engineer_approved: bool = True


class CopyOpeningsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_region_id: str


class ExtractionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    border_margin_ratio: float = Field(default=0.04, ge=0.0, lt=0.45)
    min_span_ratio: float = Field(default=0.28, gt=0.0, le=1.0)
    cluster_tolerance_px: float = Field(default=8.0, gt=0.0, le=100.0)
    merge_dashed_segments: bool = True
    detect_grid_labels: bool = True
    require_grid_label: bool = True
    detect_grids: bool = True
    detect_columns: bool = True
    column_fill_mode: ColumnFillMode = "shaded_or_hatched"
    min_column_size_m: float = Field(default=0.18, gt=0.0, le=5.0)
    max_column_size_m: float = Field(default=1.10, gt=0.0, le=10.0)
    column_max_aspect_ratio: float = Field(default=2.5, ge=1.0, le=10.0)
    column_min_rectangularity: float = Field(default=0.78, gt=0.0, le=1.0)
    column_min_fill_ratio: float = Field(default=0.35, ge=0.0, le=1.0)
    require_near_grid: bool = False
    column_probe_radius_px: float = Field(default=24.0, gt=0.0, le=200.0)
    snap_columns_to_grids: bool = True


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: DetectionMethod = "auto"
    replace_unreviewed: bool = True
    region_id: str | None = None
    options: ExtractionOptions | None = None
    border_margin_ratio: float | None = Field(default=None, ge=0.0, lt=0.45)
    min_span_ratio: float | None = Field(default=None, gt=0.0, le=1.0)
    cluster_tolerance_px: float | None = Field(default=None, gt=0.0, le=100.0)
    merge_dashed_segments: bool | None = None
    detect_grid_labels: bool | None = None
    require_grid_label: bool | None = None
    detect_grids: bool | None = None
    detect_columns: bool | None = None
    column_fill_mode: ColumnFillMode | None = None
    min_column_size_m: float | None = Field(default=None, gt=0.0, le=5.0)
    max_column_size_m: float | None = Field(default=None, gt=0.0, le=10.0)
    column_max_aspect_ratio: float | None = Field(default=None, ge=1.0, le=10.0)
    column_min_rectangularity: float | None = Field(default=None, gt=0.0, le=1.0)
    column_min_fill_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    require_near_grid: bool | None = None
    column_probe_radius_px: float | None = Field(default=None, gt=0.0, le=200.0)
    snap_columns_to_grids: bool | None = None

    def resolved_options(self) -> ExtractionOptions:
        base = self.options.model_dump() if self.options is not None else ExtractionOptions().model_dump()
        for field_name in (
            "border_margin_ratio",
            "min_span_ratio",
            "cluster_tolerance_px",
            "merge_dashed_segments",
            "detect_grid_labels",
            "require_grid_label",
            "detect_grids",
            "detect_columns",
            "column_fill_mode",
            "min_column_size_m",
            "max_column_size_m",
            "column_max_aspect_ratio",
            "column_min_rectangularity",
            "column_min_fill_ratio",
            "require_near_grid",
            "column_probe_radius_px",
            "snap_columns_to_grids",
        ):
            val = getattr(self, field_name)
            if val is not None:
                base[field_name] = val
        return ExtractionOptions.model_validate(base)


class ManualColumnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    region_id: str | None = None
    x_px: float
    y_px: float
    column_id: str | None = None
    grid: str = ""
    width_mm: float = Field(gt=0, default=400.0)
    depth_mm: float = Field(gt=0, default=400.0)
    status: ReviewStatus = ReviewStatus.APPROVED
    notes: str = ""


class ManualGridRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    region_id: str | None = None
    axis: Literal["x", "y"]
    label: str = Field(min_length=1)
    coord_px: float
    status: ReviewStatus = ReviewStatus.APPROVED
    notes: str = ""


class ManualGridTraceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    region_id: str | None = None
    element_type: Literal["grid_x", "grid_y"]
    label: str = Field(min_length=1)
    p1: Point2D
    p2: Point2D


class CandidateReviewUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ReviewStatus | None = None
    label: str | None = None
    column_id: str | None = None
    grid: str | None = None
    width_mm: float | None = Field(default=None, gt=0)
    depth_mm: float | None = Field(default=None, gt=0)
    notes: str | None = None


class BulkReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_ids: list[str] = Field(min_length=1)
    status: ReviewStatus


class ApplyMasterGridRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_region_id: str


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int | None = Field(default=None, ge=1)
    region_id: str | None = None
    merge_mode: Literal["merge", "replace_geometry"] = "merge"
    auto_add_roof_level: bool = False
    roof_framing_style: Literal["full_grid", "perimeter_only"] = "full_grid"
    default_roof_height_m: float = Field(default=3.0, gt=0.5, le=10.0)


class TransferLevelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int | None = Field(default=None, ge=1)
    replace_existing: bool = True


class TransferRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transferred_at: str
    page_numbers: list[int]
    merge_mode: str
    x_grids_count: int = 0
    y_grids_count: int = 0
    columns_count: int = 0
    levels_count: int = 0
    openings_count: int = 0


class ImportDocumentState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str
    project_id: str
    filename: str
    source_format: Literal["PDF", "CAD"] = "PDF"
    sha256: str
    file_size_bytes: int = Field(gt=0)
    created_at: str
    updated_at: str
    page_count: int = Field(ge=1)
    pages: list[PageInfo]
    regions: list[PlanRegion] = Field(default_factory=list)
    master_grid: MasterGridDefinition | None = None
    calibrations: dict[str, ScaleCalibration] = Field(default_factory=dict)
    grid_candidates: list[GridCandidate] = Field(default_factory=list)
    column_candidates: list[ColumnCandidate] = Field(default_factory=list)
    detected_levels: list[DetectedLevel] = Field(default_factory=list)
    openings: list[DetectedOpening] = Field(default_factory=list)
    transfers: list[TransferRecord] = Field(default_factory=list)


PdfDocumentRecord = ImportDocumentState


class ImportDocumentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str
    project_id: str
    filename: str
    source_format: Literal["PDF", "CAD"] = "PDF"
    page_count: int
    created_at: str
    updated_at: str
    calibrated_pages: int
    confirmed_pages: int
    approved_grids: int
    approved_columns: int
    approved_levels: int = 0
    approved_openings: int = 0
    transfers_count: int


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: ImportDocumentState
    page_number: int
    method_requested: DetectionMethod
    method_used: str
    grids_detected: int
    columns_detected: int
    notice: str | None = None


class LevelExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: ImportDocumentState
    page_number: int
    levels_detected: int
    warnings: list[str] = Field(default_factory=list)
    notice: str | None = None


class ApplyMasterGridResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: ImportDocumentState
    source_region_id: str
    target_region_id: str
    copied_grids: int
    probed_columns: int
    notice: str


class CopyOpeningsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: ImportDocumentState
    source_region_id: str
    target_region_id: str
    copied_openings: int
    notice: str


class TransferResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    doc_id: str
    transferred_x_grids: int
    transferred_y_grids: int
    transferred_columns: int
    transferred_openings: int = 0
    total_project_columns: int
    total_project_openings: int = 0
    notice: str
    project: ProjectState
    document: ImportDocumentState


class TransferLevelsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    doc_id: str
    transferred_levels: int
    total_project_levels: int
    notice: str
    project: ProjectState
    document: ImportDocumentState


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: str
    event: str
    project_id: str
    doc_id: str
    details: dict[str, Any] = Field(default_factory=dict)
