from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import EngineerSignatory
from .storage import get_active_data_dir

logger = logging.getLogger(__name__)

PROFILE_FILENAME = "engineer_profile.json"
_PLACEHOLDERS = frozenset({"", "tbd", "n/a", "na", "-"})


class EngineerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str = Field(default="", max_length=200)
    prc_license_no: str = Field(default="", max_length=200)
    prc_validity: str = Field(default="", max_length=200)
    ptr_no: str = Field(default="", max_length=200)
    ptr_date_issued: str = Field(default="", max_length=200)
    ptr_place_issued: str = Field(default="", max_length=200)
    tin: str = Field(default="", max_length=200)
    firm_name: str = Field(default="", max_length=200)
    office_address: str = Field(default="", max_length=400)
    contact_info: str = Field(default="", max_length=200)

    @field_validator("*", mode="before")
    @classmethod
    def _coerce_none_to_empty(cls, value: Any) -> Any:
        if value is None:
            return ""
        return value


def _profile_path() -> Path:
    return get_active_data_dir() / PROFILE_FILENAME


def load_engineer_profile() -> EngineerProfile:
    """Load the active user's EngineerProfile, returning defaults if missing or corrupted."""
    path = _profile_path()
    if not path.is_file():
        return EngineerProfile()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return EngineerProfile.model_validate(raw)
    except Exception as exc:
        logger.warning("Failed to load engineer profile from %s: %s", path, exc)
        return EngineerProfile()


def save_engineer_profile(profile: EngineerProfile) -> EngineerProfile:
    """Atomically save the active user's EngineerProfile as UTF-8 JSON."""
    validated = EngineerProfile.model_validate(profile.model_dump())
    target_path = _profile_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target_path.parent),
            prefix=".engineer_profile_",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            fh.write(validated.model_dump_json(indent=2))
            fh.flush()
        os.replace(str(tmp_path), str(target_path))
        tmp_path = None
        return validated
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _flatten_line(value: str) -> str:
    return " ".join(str(value or "").splitlines()).strip()


def _is_placeholder(value: str) -> bool:
    return _flatten_line(value).lower() in _PLACEHOLDERS


def _pick(project_val: str, profile_val: str) -> str:
    """
    Prefer explicit non-placeholder project value.
    If project value is empty/placeholder, fall back to profile_val ONLY if profile_val is non-empty;
    otherwise preserve original project_val so existing defaults stay untouched.
    """
    proj_clean = _flatten_line(project_val)
    if not _is_placeholder(proj_clean):
        return proj_clean
    prof_clean = _flatten_line(profile_val)
    if prof_clean:
        return prof_clean
    return proj_clean


def resolve_signatory(
    project_metadata: EngineerSignatory | dict[str, Any] | None,
    profile: EngineerProfile | None = None,
) -> dict[str, str]:
    """
    Merge project-level EngineerSignatory metadata with the active user's EngineerProfile.
    Returns flattened single-line strings ready for DXF title blocks and OBO PDF/DOCX reports.
    """
    if isinstance(project_metadata, EngineerSignatory):
        meta = project_metadata.model_dump()
    elif isinstance(project_metadata, dict):
        meta = dict(project_metadata)
    else:
        meta = EngineerSignatory().model_dump()

    prof = profile if profile is not None else EngineerProfile()

    return {
        "engineer_name": _pick(meta.get("engineer_name", ""), prof.full_name),
        "title": _flatten_line(meta.get("title", "") or "Civil / Structural Engineer"),
        "prc_license_no": _pick(meta.get("prc_license_no", ""), prof.prc_license_no),
        "prc_validity": _pick(meta.get("prc_validity", ""), prof.prc_validity),
        "ptr_no": _pick(meta.get("ptr_no", ""), prof.ptr_no),
        "ptr_date": _pick(meta.get("ptr_date", ""), prof.ptr_date_issued),
        "ptr_place": _pick(meta.get("ptr_place", ""), prof.ptr_place_issued),
        "tin": _pick(meta.get("tin", ""), prof.tin),
        "project_owner": _flatten_line(meta.get("project_owner", "")),
        "project_address": _flatten_line(meta.get("project_address", "")),
        "geotech_report_ref": _flatten_line(meta.get("geotech_report_ref", "")),
        "firm_name": _flatten_line(prof.firm_name),
        "office_address": _flatten_line(prof.office_address),
        "contact_info": _flatten_line(prof.contact_info),
    }
