"""PACS domain DTOs owned by the new application package."""
from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.core.enums import ArchiveStatus, DataLevel, DownloadStatus


class ChatRequest(BaseModel):
    message: str
    intent: str | None = None
    thread_id: str | None = None
    user_id: str | None = None
    task_id: str | None = None
    study_instance_uid: str | None = None
    study_instance_uid_list: list[str] = Field(default_factory=list)
    series_instance_uid: str | None = None
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
    route: str | None = None
    diagnosis: dict[str, Any] | None = None
    proposed_action: dict[str, Any] | None = None
    approval_status: str | None = None
    batch_summary: dict[str, Any] | None = None
    study_results: dict[str, Any] | None = None


class ActionRequest(BaseModel):
    action: str
    operator: str | None = None


class CancelRequest(BaseModel):
    operator: str | None = None


class AbortRequest(BaseModel):
    operator: str | None = None
    reason: str | None = None


class AbortResponse(BaseModel):
    run_id: str
    status: str
    task_id: str | None = None
    flag_set: bool
    task_previous_status: str | None = None
    already_received: int = 0
    may_still_arrive: bool = False
    note: str | None = None


class ActionResponse(BaseModel):
    run_id: str
    status: str


class SeriesPullItem(BaseModel):
    series_instance_uid: str
    sop_instance_uid_list: list[str] = Field(default_factory=list)
    expected_image_number: int | None = None


class PullTaskRequest(BaseModel):
    study_instance_uid: str
    source_id: str = "orthanc-local"
    level: DataLevel = DataLevel.STUDY
    series_instance_uid: str | None = None
    series_list: list[SeriesPullItem] = Field(default_factory=list)
    priority: int = 5
    modality: str | None = None
    expected_image_number: int | None = None
    target_name: str | None = None
    skip_pacs_find: bool = False


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str


class PullTaskResponse(BaseModel):
    task_id: str
    status: DownloadStatus
    message: str


class PacsEchoResult(BaseModel):
    ok: bool
    source_id: str
    status_code: int | None = None
    latency_ms: int = 0
    message: str


class StudyResult(BaseModel):
    study_instance_uid: str
    study_date: str | None = None
    modality: str | None = None
    number_of_study_related_instances: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class SeriesResult(BaseModel):
    study_instance_uid: str
    series_instance_uid: str
    modality: str | None = None
    number_of_series_related_instances: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class InstanceResult(BaseModel):
    study_instance_uid: str
    series_instance_uid: str
    sop_instance_uid: str
    instance_number: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class MoveResult(BaseModel):
    ok: bool
    source_id: str
    level: str
    study_instance_uid: str
    series_instance_uid: str | None = None
    sop_instance_uid: str | None = None
    status_code: int | None = None
    completed: int = 0
    failed: int = 0
    warning: int = 0
    latency_ms: int = 0
    message: str

__all__ = [
    "AbortRequest", "AbortResponse", "ActionRequest", "ActionResponse", "CancelRequest",
    "ChatRequest", "ChatResponse", "RunStatusResponse", "DataLevel", "DownloadStatus",
    "ArchiveStatus", "PullTaskRequest", "SeriesPullItem", "HealthResponse",
    "PullTaskResponse", "PacsEchoResult", "StudyResult", "SeriesResult",
    "InstanceResult", "MoveResult",
]
