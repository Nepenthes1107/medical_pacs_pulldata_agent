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
    # 期望数缺失、完整性无法严格核对的终结态（Checker 裁定）。
    # 独立成状态而非「downloaded + checked_at 非空」的隐式编码：后者要靠 last_error
    # 字符串匹配才能分辨，是无法验证而不是验证通过，语义上必须与 success/fail 并列。
    UNVERIFIED = "unverified"


class ArchiveStatus(str, Enum):
    UNKNOWN = "unknown"
    ARCHIVING = "archiving"
    FINISHED = "finished"
    FAIL = "fail"
    UNVERIFIED = "unverified"


