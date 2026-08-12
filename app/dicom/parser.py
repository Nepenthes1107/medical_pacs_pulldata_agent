import os
import re
from datetime import date
from typing import Any, Optional


SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z_.-]+")


def safe_segment(value: Any, default: str = "unknown") -> str:
    text = str(value or default).strip() or default
    return SAFE_NAME_RE.sub("_", text)[:180]


def get_text(dataset: Any, name: str, default: Optional[str] = None) -> Optional[str]:
    value = getattr(dataset, name, default)
    if value is None:
        return default
    return str(value)


def get_int(dataset: Any, name: str) -> Optional[int]:
    value = getattr(dataset, name, None)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_date(dataset: Any, name: str) -> Optional[date]:
    value = getattr(dataset, name, None)
    if not value:
        return None
    text = str(value)
    if len(text) != 8:
        return None
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def build_dicom_file_path(root: str, dataset: Any) -> str:
    study_uid = safe_segment(get_text(dataset, "StudyInstanceUID"))
    series_uid = safe_segment(get_text(dataset, "SeriesInstanceUID"))
    sop_uid = safe_segment(get_text(dataset, "SOPInstanceUID"))
    return os.path.join(root, study_uid, series_uid, "%s.dcm" % sop_uid)
