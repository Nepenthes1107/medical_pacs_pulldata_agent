"""Compatibility schema module.

PACS request/response and DICOM result DTOs are owned by ``src.schema.pacs``.
This module only preserves the historical import path for downstream callers.
"""


from pydantic import BaseModel

from src.core.enums import ArchiveStatus, DataLevel, DownloadStatus
from src.schema.pacs import (
    AbortRequest,
    AbortResponse,
    ActionRequest,
    ActionResponse,
    CancelRequest,
    ChatRequest,
    ChatResponse,
    HealthResponse,
    InstanceResult,
    MoveResult,
    PacsEchoResult,
    PullTaskRequest,
    PullTaskResponse,
    RunStatusResponse,
    SeriesPullItem,
    SeriesResult,
    StudyResult,
)


class AgentDiagnoseRequest(BaseModel):
    task_id: str | None = None
    study_instance_uid: str | None = None
    source_id: str = "orthanc-local"


class AgentRetryRequest(BaseModel):
    task_id: str
    force: bool = False


class AgentChatRequest(BaseModel):
    question: str
    source_id: str = "orthanc-local"


__all__ = [
    "AbortRequest", "AbortResponse", "ActionRequest", "ActionResponse", "CancelRequest",
    "ChatRequest", "ChatResponse", "HealthResponse", "InstanceResult", "MoveResult",
    "PacsEchoResult", "PullTaskRequest", "PullTaskResponse", "RunStatusResponse",
    "SeriesPullItem", "SeriesResult", "StudyResult", "AgentDiagnoseRequest",
    "AgentRetryRequest", "AgentChatRequest", "ArchiveStatus", "DataLevel", "DownloadStatus",
]
