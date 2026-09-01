from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    Index,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)

from app.core.database import Base


class TimestampMixin(object):
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class StudyModel(Base, TimestampMixin):
    __tablename__ = "study"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    study_instance_uid = Column(String(128), nullable=False, unique=True, index=True)
    accession_number = Column(String(64))
    study_id = Column(String(64))
    study_date = Column(Date)
    study_time = Column(Time)
    study_description = Column(String(1024))
    modalities_in_study = Column(String(256))
    institution_name = Column(String(128))
    number_of_study_related_instances = Column(Integer)
    source_id = Column(String(64), nullable=False, default="orthanc-local", index=True)
    find_source = Column(JSON)
    move_source = Column(JSON)


class SeriesModel(Base, TimestampMixin):
    __tablename__ = "series"
    __table_args__ = (UniqueConstraint("study_instance_uid", "series_instance_uid", name="uk_study_series_uid"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    study_instance_uid = Column(String(128), nullable=False, index=True)
    series_instance_uid = Column(String(128), nullable=False, index=True)
    modality = Column(String(32))
    series_number = Column(Integer)
    series_date = Column(Date)
    series_time = Column(Time)
    series_description = Column(String(255))
    protocol_name = Column(String(128))
    body_part_examined = Column(String(64))
    manufacturer = Column(String(128))
    number_of_series_related_instances = Column(Integer)
    series_folder_path = Column(String(512))
    source_id = Column(String(64), nullable=False, default="orthanc-local", index=True)
    find_source = Column(JSON)


class DownloadTask(Base, TimestampMixin):
    __tablename__ = "download_task"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(String(64), nullable=False, unique=True, index=True)
    study_instance_uid = Column(String(128), nullable=False, index=True)
    series_instance_uid = Column(String(128), index=True)
    modality = Column(String(32))
    expected_image_number = Column(Integer)
    level = Column(String(32), nullable=False, default="study_level")
    status = Column(String(32), nullable=False, default="in_queue", index=True)
    queued_at = Column(DateTime)
    download_started_at = Column(DateTime)
    move_finished_at = Column(DateTime)
    checked_at = Column(DateTime)
    failed_at = Column(DateTime)
    priority = Column(Integer, nullable=False, default=5)
    source_id = Column(String(64), nullable=False, default="orthanc-local", index=True)
    task_body = Column(JSON)
    move_result = Column(JSON)
    task_retry_times = Column(Integer, nullable=False, default=0)
    last_error = Column(Text)


class StoreScpImage(Base, TimestampMixin):
    __tablename__ = "storescp_image"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    image_name = Column(String(255), nullable=False, unique=True)
    study_instance_uid = Column(String(128), index=True)
    series_instance_uid = Column(String(128), nullable=False, index=True)
    series_path = Column(String(512))
    file_path = Column(String(512))
    received_at = Column(DateTime, nullable=False, server_default=func.now())


class ArchiveModel(Base, TimestampMixin):
    __tablename__ = "archive"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    data_id = Column(String(255), nullable=False, unique=True, index=True)
    study_instance_uid = Column(String(128), nullable=False, index=True)
    series_instance_uid = Column(String(128), index=True)
    level = Column(String(32), nullable=False, default="study_level")
    status = Column(String(32), nullable=False, default="unknown", index=True)
    task_id = Column(String(64), index=True)
    expected_image_count = Column(Integer)
    archived_image_count = Column(Integer, nullable=False, default=0)
    checked = Column(Boolean, nullable=False, default=False)
    last_error = Column(Text)


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_run"
    __table_args__ = (
        Index("idx_agent_run_thread_status_created", "thread_id", "status", "created_at"),
        Index("idx_agent_run_task_status_created", "task_id", "status", "created_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=False, unique=True, index=True)
    route = Column(String(32))
    intent = Column(String(32))  # 显式意图（first_pull 等），最高优先于规则/LLM
    thread_id = Column(String(64), index=True)  # 会话标识，决定「谁和谁串行」（需求 2）
    user_id = Column(String(64), index=True)
    source_id = Column(String(64), nullable=False, default="orthanc-local", index=True)
    status = Column(String(32), nullable=False, default="running", index=True)
    message = Column(Text)
    task_id = Column(String(64), index=True)
    study_instance_uid = Column(String(128))
    series_instance_uid = Column(String(128))
    diagnostic_level = Column(String(16))
    diagnosis = Column(JSON)
    proposed_action = Column(JSON)
    approval_status = Column(String(16))
    operator = Column(String(64))
    error = Column(Text)


class AgentActionAudit(Base):
    """每个幂等写操作一行；只允许 processing 占位收敛为最终结果。"""

    __tablename__ = "agent_action_audit"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=False, index=True)
    task_id = Column(String(64), nullable=False, index=True)
    action = Column(String(32), nullable=False)
    operator = Column(String(64))
    idempotency_key = Column(String(128), nullable=False, unique=True)
    result = Column(String(32), nullable=False)
    detail = Column(JSON)
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)


class AgentExecutionLog(Base):
    """LangGraph 轻量执行轨迹；完整工具内容由 AgentToolEvidence 独占保存。"""

    __tablename__ = "agent_execution_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=False, index=True)
    thread_id = Column(String(64), nullable=False, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    node_name = Column(String(64))
    payload = Column(JSON)  # 状态摘要与 tool_id 引用，不保存完整工具参数或输出
    error = Column(Text)
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)


class AgentToolEvidence(Base):
    """每次只读工具调用的完整、不可变证据；引用通过 tool_id 精确定位。"""

    __tablename__ = "agent_tool_evidence"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tool_id = Column(String(36), nullable=False, unique=True, index=True)
    run_id = Column(String(64), nullable=False, index=True)
    thread_id = Column(String(64), nullable=False, index=True)
    tool_name = Column(String(64), nullable=False, index=True)
    tool_args = Column(JSON, nullable=False)
    output = Column(JSON, nullable=False)
    success = Column(Boolean, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
