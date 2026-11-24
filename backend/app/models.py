from pydantic import BaseModel, Field, ConfigDict, model_validator

class MaterialProperties(BaseModel):
    fc_mpa: float = Field(gt=0)
    fy_mpa: float = Field(gt=0)
    concrete_density_kn_m3: float = Field(gt=0)

class ProjectInfo(BaseModel):
    project_id: str = Field(min_length=1, max_length=80)
    project_name: str = Field(min_length=1, max_length=200)
    location: str = ""
    design_code: str = "To be confirmed by engineer"
    materials: MaterialProperties

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

class ProjectState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_info: ProjectInfo
    levels: list[Level]
    grids: Grids = Field(default_factory=Grids)
    columns: list[Column]
    beams: list[Beam] = Field(default_factory=list)

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
