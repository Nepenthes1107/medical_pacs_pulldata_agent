from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.database import Base


class TimestampMixin:
    # 建表由 config/schema.sql 负责（本仓库不依赖模型 create_all 落库），
    # nullable=False 与 DB 约束一致；server_default/onupdate 仅作插入默认。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class StudyModel(Base, TimestampMixin):
    __tablename__ = "study"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    accession_number: Mapped[str | None] = mapped_column(String(64))
    study_id: Mapped[str | None] = mapped_column(String(64))
    study_date: Mapped[date | None] = mapped_column(Date)
    study_time: Mapped[time | None] = mapped_column(Time)
    study_description: Mapped[str | None] = mapped_column(String(1024))
    modalities_in_study: Mapped[str | None] = mapped_column(String(256))
    institution_name: Mapped[str | None] = mapped_column(String(128))
    number_of_study_related_instances: Mapped[int | None] = mapped_column(Integer)
    source_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="orthanc-local", index=True
    )
    find_source: Mapped[Any | None] = mapped_column(JSON)
    move_source: Mapped[Any | None] = mapped_column(JSON)


class SeriesModel(Base, TimestampMixin):
    __tablename__ = "series"
    __table_args__ = (
        UniqueConstraint("study_instance_uid", "series_instance_uid", name="uk_study_series_uid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    study_instance_uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    series_instance_uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    modality: Mapped[str | None] = mapped_column(String(32))
    series_number: Mapped[int | None] = mapped_column(Integer)
    series_date: Mapped[date | None] = mapped_column(Date)
    series_time: Mapped[time | None] = mapped_column(Time)
    series_description: Mapped[str | None] = mapped_column(String(255))
    protocol_name: Mapped[str | None] = mapped_column(String(128))
    body_part_examined: Mapped[str | None] = mapped_column(String(64))
    manufacturer: Mapped[str | None] = mapped_column(String(128))
    number_of_series_related_instances: Mapped[int | None] = mapped_column(Integer)
    series_folder_path: Mapped[str | None] = mapped_column(String(512))
    source_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="orthanc-local", index=True
    )
    find_source: Mapped[Any | None] = mapped_column(JSON)


class DownloadTask(Base, TimestampMixin):
    __tablename__ = "download_task"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    study_instance_uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    series_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    modality: Mapped[str | None] = mapped_column(String(32))
    expected_image_number: Mapped[int | None] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(String(32), nullable=False, default="study_level")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="in_queue", index=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime)
    download_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    move_finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    source_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="orthanc-local", index=True
    )
    task_body: Mapped[Any | None] = mapped_column(JSON)
    move_result: Mapped[Any | None] = mapped_column(JSON)
    task_retry_times: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class StoreScpImage(Base, TimestampMixin):
    __tablename__ = "storescp_image"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    image_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    study_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    series_instance_uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    series_path: Mapped[str | None] = mapped_column(String(512))
    file_path: Mapped[str | None] = mapped_column(String(512))
    received_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class ArchiveModel(Base, TimestampMixin):
    __tablename__ = "archive"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    data_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    study_instance_uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    series_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    level: Mapped[str] = mapped_column(String(32), nullable=False, default="study_level")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    expected_image_count: Mapped[int | None] = mapped_column(Integer)
    archived_image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_run"
    __table_args__ = (
        Index("idx_agent_run_thread_status_created", "thread_id", "status", "created_at"),
        Index("idx_agent_run_task_status_created", "task_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    route: Mapped[str | None] = mapped_column(String(32))
    intent: Mapped[str | None] = mapped_column(String(32))  # 显式意图（first_pull 等），最高优先于规则/LLM
    thread_id: Mapped[str | None] = mapped_column(String(64), index=True)  # 会话标识（需求 2）
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="orthanc-local", index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running", index=True)
    message: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    study_instance_uid: Mapped[str | None] = mapped_column(String(128))
    study_instance_uid_list: Mapped[Any | None] = mapped_column(JSON)
    batch_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    batch_summary: Mapped[Any | None] = mapped_column(JSON)
    study_results: Mapped[Any | None] = mapped_column(JSON)
    series_instance_uid: Mapped[str | None] = mapped_column(String(128))
    diagnostic_level: Mapped[str | None] = mapped_column(String(16))
    diagnosis: Mapped[Any | None] = mapped_column(JSON)
    proposed_action: Mapped[Any | None] = mapped_column(JSON)
    approval_status: Mapped[str | None] = mapped_column(String(16))
    operator: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)


class AgentActionAudit(Base):
    """每个幂等写操作一行；只允许 processing 占位收敛为最终结果。"""

    __tablename__ = "agent_action_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    operator: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )


class AgentExecutionLog(Base):
    """LangGraph 轻量执行轨迹；完整工具内容由 AgentToolEvidence 独占保存。"""

    __tablename__ = "agent_execution_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    node_name: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[Any | None] = mapped_column(JSON)  # 状态摘要与 tool_id 引用，不保存完整工具参数或输出
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )


class AgentToolEvidence(Base):
    """每次只读工具调用的完整、不可变证据；引用通过 tool_id 精确定位。"""

    __tablename__ = "agent_tool_evidence"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tool_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tool_args: Mapped[Any] = mapped_column(JSON, nullable=False)
    output: Mapped[Any] = mapped_column(JSON, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )
