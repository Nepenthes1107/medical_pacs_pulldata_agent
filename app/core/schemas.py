from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from app.core.enums import DataLevel, DownloadStatus


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str


class SeriesPullItem(BaseModel):
    series_instance_uid: str
    sop_instance_uid_list: List[str] = Field(default_factory=list)
    expected_image_number: Optional[int] = None


class PullTaskRequest(BaseModel):
    study_instance_uid: str
    source_id: str = "orthanc-local"
    level: DataLevel = DataLevel.STUDY
    series_instance_uid: Optional[str] = None
    series_list: List[SeriesPullItem] = Field(default_factory=list)
    priority: int = 5
    modality: Optional[str] = None
    expected_image_number: Optional[int] = None
    target_name: Optional[str] = None
    skip_pacs_find: bool = False


class PullTaskResponse(BaseModel):
    task_id: str
    status: DownloadStatus
    message: str


class PacsEchoResult(BaseModel):
    ok: bool
    source_id: str
    status_code: Optional[int] = None
    latency_ms: int = 0
    message: str


class StudyResult(BaseModel):
    study_instance_uid: str
    study_date: Optional[str] = None
    modality: Optional[str] = None
    number_of_study_related_instances: Optional[int] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class SeriesResult(BaseModel):
    study_instance_uid: str
    series_instance_uid: str
    modality: Optional[str] = None
    number_of_series_related_instances: Optional[int] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class InstanceResult(BaseModel):
    """IMAGE 级 C-FIND 单条结果（sop 粒度定向补拉的目标来源）。"""

    study_instance_uid: str
    series_instance_uid: str
    sop_instance_uid: str
    instance_number: Optional[int] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class MoveResult(BaseModel):
    ok: bool
    source_id: str
    level: str
    study_instance_uid: str
    series_instance_uid: Optional[str] = None
    sop_instance_uid: Optional[str] = None
    status_code: Optional[int] = None
    completed: int = 0
    failed: int = 0
    warning: int = 0
    latency_ms: int = 0
    message: str


class AgentDiagnoseRequest(BaseModel):
    task_id: Optional[str] = None
    study_instance_uid: Optional[str] = None
    source_id: str = "orthanc-local"


class AgentRetryRequest(BaseModel):
    task_id: str
    force: bool = False


class AgentChatRequest(BaseModel):
    question: str
    source_id: str = "orthanc-local"


# --- 异步 Run 模型（spec §7）---

class ChatRequest(BaseModel):
    message: str
    intent: Optional[str] = None  # 显式意图：first_pull|diagnosis|knowledge_qa（最高优先）
    thread_id: Optional[str] = None  # 会话标识（需求 2：同 thread 串行）
    user_id: Optional[str] = None
    task_id: Optional[str] = None
    study_instance_uid: Optional[str] = None
    study_instance_uid_list: List[str] = Field(default_factory=list)
    series_instance_uid: Optional[str] = None
    source_id: str = "orthanc-local"

    @model_validator(mode="after")
    def validate_study_list(self):
        values = [str(v).strip() for v in self.study_instance_uid_list if str(v).strip()]
        if len(values) != len(self.study_instance_uid_list):
            raise ValueError("study_instance_uid_list contains empty UID")
        if len(set(values)) != len(values):
            raise ValueError("study_instance_uid_list contains duplicate UID")
        if len(values) > 20:
            raise ValueError("study_instance_uid_list supports at most 20 studies")
        self.study_instance_uid_list = values
        if self.study_instance_uid and values and self.study_instance_uid not in values:
            raise ValueError("study_instance_uid must be included in study_instance_uid_list")
        return self


class ChatResponse(BaseModel):
    run_id: str
    thread_id: str
    status: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str
    route: Optional[str] = None
    diagnosis: Optional[Dict[str, Any]] = None
    proposed_action: Optional[Dict[str, Any]] = None
    approval_status: Optional[str] = None
    batch_summary: Optional[Dict[str, Any]] = None
    study_results: Optional[Dict[str, Any]] = None


class ActionRequest(BaseModel):
    action: str  # "approve" | "reject"
    operator: Optional[str] = None


class CancelRequest(BaseModel):
    """取消 Run 的可选 body（需求 3.6）：仅记录操作人，无必填字段。"""
    operator: Optional[str] = None


class AbortRequest(BaseModel):
    """紧急止损请求（误批大范围补拉）：operator/reason 进审计，事后可追溯谁停的、为什么。"""
    operator: Optional[str] = None
    reason: Optional[str] = None


class AbortResponse(BaseModel):
    """止损结果。字段刻意区分「已拦住的」与「可能仍会到达的」，不谎报停干净了。"""
    run_id: str
    status: str
    task_id: Optional[str] = None
    flag_set: bool  # 止损标记是否写入成功（False 表示 Redis 故障，止损可能不生效）
    task_previous_status: Optional[str] = None
    already_received: int = 0  # 止损前已落盘的影像数（磁盘上留下的半个 Study）
    may_still_arrive: bool = False  # C-MOVE 已发出，PACS 可能仍在推送
    note: Optional[str] = None


class ActionResponse(BaseModel):
    run_id: str
    status: str
