import os
from collections import Counter
from typing import Dict, Iterable, List, Optional

import pydicom

from app.core.config import resolve_project_path, settings
from app.dicom.parser import get_date, get_int, get_text


def iter_dicom_files(root: str) -> Iterable[str]:
    if not os.path.exists(root):
        return
    for current_root, _, files in os.walk(root):
        for file_name in files:
            file_path = os.path.join(current_root, file_name)
            if file_name.lower().endswith((".dcm", ".dicom")) or os.path.isfile(file_path):
                yield file_path


def parse_dicom_file(file_path: str) -> Optional[Dict]:
    try:
        dataset = pydicom.dcmread(file_path, stop_before_pixels=True, force=True)
    except Exception:
        return None

    study_uid = get_text(dataset, "StudyInstanceUID")
    series_uid = get_text(dataset, "SeriesInstanceUID")
    sop_uid = get_text(dataset, "SOPInstanceUID")
    if not study_uid or not series_uid or not sop_uid:
        return None
    return {
        "study_instance_uid": study_uid,
        "series_instance_uid": series_uid,
        "sop_instance_uid": sop_uid,
        "study_date": get_date(dataset, "StudyDate"),
        "modality": get_text(dataset, "Modality"),
        "instance_number": get_int(dataset, "InstanceNumber"),
        "rows_count": get_int(dataset, "Rows"),
        "columns_count": get_int(dataset, "Columns"),
        "window_width": get_text(dataset, "WindowWidth"),
        "window_center": get_text(dataset, "WindowCenter"),
        "pixel_spacing": get_text(dataset, "PixelSpacing"),
        "slice_thickness": get_text(dataset, "SliceThickness"),
        "file_path": file_path,
        "file_size": os.path.getsize(file_path),
        "number_of_study_related_instances": get_int(dataset, "NumberOfStudyRelatedInstances"),
        "number_of_series_related_instances": get_int(dataset, "NumberOfSeriesRelatedInstances"),
    }


def scan_local_dicom(
    study_instance_uid: Optional[str] = None,
    series_instance_uid: Optional[str] = None,
    fileserver_root: Optional[str] = None,
) -> Dict:
    root = resolve_project_path(fileserver_root or settings.storage.fileserver_root)
    if study_instance_uid:
        scan_root = os.path.join(root, study_instance_uid)
    else:
        scan_root = root

    instances: List[Dict] = []
    for file_path in iter_dicom_files(scan_root) or []:
        metadata = parse_dicom_file(file_path)
        if not metadata:
            continue
        if study_instance_uid and metadata["study_instance_uid"] != study_instance_uid:
            continue
        if series_instance_uid and metadata["series_instance_uid"] != series_instance_uid:
            continue
        instances.append(metadata)

    series_counter = Counter(item["series_instance_uid"] for item in instances)
    return {
        "study_instance_uid": study_instance_uid,
        "series_instance_uid": series_instance_uid,
        "fileserver_root": root,
        "total_files": len(instances),
        "total_sop_count": len({item["sop_instance_uid"] for item in instances}),
        "series_count": len(series_counter),
        "series": dict(series_counter),
        "sop_instance_uid_list": sorted({item["sop_instance_uid"] for item in instances}),
        "sample_files": [item["file_path"] for item in instances[:5]],
        "instances": instances,
    }
