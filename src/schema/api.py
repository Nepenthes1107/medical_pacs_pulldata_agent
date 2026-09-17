from typing import Any

from pydantic import BaseModel, Field


class AgentInfo(BaseModel):
    key: str
    description: str
    capabilities: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str


class AgentInvokeRequest(BaseModel):
    message: str = ""
    thread_id: str | None = None
    user_id: str | None = None
    task_id: str | None = None
    study_instance_uid: str | None = None
    series_instance_uid: str | None = None
    source_id: str = "orthanc-local"
    intent: str | None = None


class AgentInvokeResponse(BaseModel):
    agent_id: str
    thread_id: str
    state: dict[str, Any]


class ThreadRequest(BaseModel):
    thread_id: str
    user_id: str | None = None


class ThreadSummary(BaseModel):
    thread_id: str
    updated_at: str | None = None
    title: str | None = None
