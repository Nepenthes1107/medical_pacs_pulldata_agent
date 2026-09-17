"""Domain enums shared by the application and PACS agent layers."""
from enum import Enum


class DataLevel(str, Enum):
    UNKNOWN = "unknown"
    STUDY = "study_level"
    SERIES = "series_level"
    SOP = "sop_level"


class DownloadStatus(str, Enum):
    UNKNOWN = "unknown"
    IN_QUEUE = "in_queue"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    SUCCESS = "success"
    FAIL = "fail"
    CANCEL = "cancel"
    UNVERIFIED = "unverified"


class ArchiveStatus(str, Enum):
    UNKNOWN = "unknown"
    ARCHIVING = "archiving"
    FINISHED = "finished"
    FAIL = "fail"
    UNVERIFIED = "unverified"


__all__ = ["ArchiveStatus", "DataLevel", "DownloadStatus"]
