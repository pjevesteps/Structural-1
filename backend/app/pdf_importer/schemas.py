from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict
from ..models import ProjectState

LengthUnit = Literal["mm", "cm", "m", "ft", "in"]
PageClassification = Literal["vector", "scanned", "hybrid"]
ReviewStatus = Literal["pending", "approved", "rejected"]
DetectionMethod = Literal["auto", "vector", "raster", "vlm"]
CandidateSource = Literal["vector", "raster", "manual", "vlm"]

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
    status: ReviewStatus = "pending"
    transferred: bool = False
    notes: str = ""


class ColumnCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    page_number: int = Field(ge=1)
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
    status: ReviewStatus = "pending"
    transferred: bool = False
    notes: str = ""


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: DetectionMethod = "auto"
    replace_unreviewed: bool = True


class ManualColumnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    x_px: float
    y_px: float
    column_id: str | None = None
    grid: str = ""
    width_mm: float = Field(gt=0, default=400.0)
    depth_mm: float = Field(gt=0, default=400.0)
    status: ReviewStatus = "approved"
    notes: str = ""


class ManualGridRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    axis: Literal["x", "y"]
    label: str = Field(min_length=1)
    coord_px: float
    status: ReviewStatus = "approved"
    notes: str = ""


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


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int | None = Field(default=None, ge=1)
    merge_mode: Literal["merge", "replace_geometry"] = "merge"


class TransferRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transferred_at: str
    page_numbers: list[int]
    merge_mode: str
    x_grids_count: int
    y_grids_count: int
    columns_count: int


class ImportDocumentState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str
    project_id: str
    filename: str
    sha256: str
    file_size_bytes: int = Field(gt=0)
    created_at: str
    updated_at: str
    page_count: int = Field(ge=1)
    pages: list[PageInfo]
    calibrations: dict[str, ScaleCalibration] = Field(default_factory=dict)
    grid_candidates: list[GridCandidate] = Field(default_factory=list)
    column_candidates: list[ColumnCandidate] = Field(default_factory=list)
    transfers: list[TransferRecord] = Field(default_factory=list)


class ImportDocumentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str
    project_id: str
    filename: str
    page_count: int
    created_at: str
    updated_at: str
    calibrated_pages: int
    confirmed_pages: int
    approved_grids: int
    approved_columns: int
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


class TransferResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    doc_id: str
    transferred_x_grids: int
    transferred_y_grids: int
    transferred_columns: int
    total_project_columns: int
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
