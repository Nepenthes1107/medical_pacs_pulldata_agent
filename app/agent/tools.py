import logging
from datetime import datetime
from typing import List, Optional

import pika
from langchain_core.tools import StructuredTool

from app.agent.tool_schemas import (
    ComputeIntegrityInput,
    ComputeIntegrityOutput,
    KnowledgeItem,
    MissingInstancesInput,
    MissingInstancesOutput,
    PacsHierarchyInput,
    PacsHierarchyOutput,
    PacsTargetInput,
    PacsTargetOutput,
    ReceiveStatusInput,
    ReceiveStatusOutput,
    SearchKnowledgeInput,
    SearchKnowledgeOutput,
    SeriesNode,
    TaskContextInput,
    TaskContextOutput,
    TaskHistoryInput,
    TaskHistoryOutput,
    TaskSummary,
    WorkerHealthInput,
    WorkerHealthOutput,
    derive_repull_level,
)
from app.core.config import settings
from app.core.database import session_scope
from app.core.enums import DownloadStatus
from app.core.messaging import publish_download_task
from app.core.models import ArchiveModel, DownloadTask, StoreScpImage
from app.dicom.pacs_client import PacsClient
from app.dicom.scanner import scan_local_dicom

# 重复失败阈值：历史重试次数达此值仍未成功 → repeatedly_failing（供 LLM 判断该 escalate）。
_REPEATED_FAILURE_THRESHOLD = 3


logger = logging.getLogger(__name__)


# ==========================================================================
# 9 个只读 LangChain 工具（plan §2.2）
# 每个工具复用现有 PacsClient/scanner/DB 能力，不重写业务；
# 异常一律收敛为 success=false + error，瞬时错误标 retryable=true，不抛裸异常。
# ==========================================================================

_TRANSIENT_HINTS = ("timed out", "timeout", "connection", "refused", "unreachable", "reset")


def _is_transient(message: str) -> bool:
    low = (message or "").lower()
    return any(hint in low for hint in _TRANSIENT_HINTS)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def query_pacs_target(
    source_id: str,
    study_instance_uid: Optional[str] = None,
    series_instance_uid: Optional[str] = None,
) -> PacsTargetOutput:
    """C-ECHO + C-FIND：检查 PACS 连通性并查询目标 Study/Series 元数据。"""
    client = PacsClient()
    echo = client.echo(source_id)
    out = PacsTargetOutput(success=True, pacs_reachable=bool(echo.ok))
    out.dicom_status_summary = echo.message
    if not echo.ok:
        # 不可达是可信的业务事实：success=True 但 pacs_reachable=False（spec 9.0）。
        out.retryable = _is_transient(echo.message)
        return out
    try:
        if series_instance_uid:
            if not study_instance_uid:
                return PacsTargetOutput(success=False, error="series query requires study_instance_uid")
            series = client.find_series(source_id, study_instance_uid)
            match = next((s for s in series if s.series_instance_uid == series_instance_uid), None)
            out.study_exists = True
            out.series_exists = match is not None
            out.parent_study_uid = study_instance_uid
            if match:
                out.expected_instance_count = match.number_of_series_related_instances
                out.modality = match.modality
        elif study_instance_uid:
            studies = client.find_study(source_id, study_instance_uid=study_instance_uid)
            match = next((s for s in studies if s.study_instance_uid == study_instance_uid), None)
            out.study_exists = match is not None
            if match:
                out.expected_instance_count = match.number_of_study_related_instances
                out.modality = match.modality
        return out
    except Exception as exc:  # C-FIND 失败
        msg = str(exc)
        return PacsTargetOutput(
            success=False, error="C-FIND failed: %s" % msg, retryable=_is_transient(msg),
            pacs_reachable=True, dicom_status_summary=echo.message,
        )


def query_task_context(
    task_id: Optional[str] = None,
    study_instance_uid: Optional[str] = None,
    series_instance_uid: Optional[str] = None,
) -> TaskContextOutput:
    """查询 download_task 及其阶段时间线，映射为 TaskSummary。"""
    if not any([task_id, study_instance_uid, series_instance_uid]):
        return TaskContextOutput(success=False, error="at least one of task_id/study/series uid is required")
    try:
        with session_scope() as db:
            q = db.query(DownloadTask)
            if task_id:
                q = q.filter(DownloadTask.task_id == task_id)
            if study_instance_uid:
                q = q.filter(DownloadTask.study_instance_uid == study_instance_uid)
            if series_instance_uid:
                q = q.filter(DownloadTask.series_instance_uid == series_instance_uid)
            rows = q.order_by(DownloadTask.created_at.desc()).all()
            summaries = [
                TaskSummary(
                    task_id=t.task_id, level=t.level, status=t.status,
                    expected_count=t.expected_image_number,
                    created_at=_iso(t.created_at), queued_at=_iso(t.queued_at),
                    download_started_at=_iso(t.download_started_at),
                    move_finished_at=_iso(t.move_finished_at), checked_at=_iso(t.checked_at),
                    last_error=t.last_error, can_retry=(t.status == DownloadStatus.FAIL.value),
                )
                for t in rows
            ]
        return TaskContextOutput(success=True, tasks=summaries)
    except Exception as exc:
        return TaskContextOutput(success=False, error="task query failed: %s" % exc)


def query_receive_status(
    study_instance_uid: str,
    series_instance_uid: Optional[str] = None,
) -> ReceiveStatusOutput:
    """storescp 登记数 + 本地扫描：一次调用获得接收端全貌。

    计数口径（spec 9.4）：唯一 SOP 权威来源是 local_dicom_file（local_unique_sop_count），
    storescp_received_count 仅作“接收端是否有活动”的信号。
    """
    try:
        with session_scope() as db:
            q = db.query(StoreScpImage).filter(StoreScpImage.study_instance_uid == study_instance_uid)
            if series_instance_uid:
                q = q.filter(StoreScpImage.series_instance_uid == series_instance_uid)
            rows = q.all()
            received_count = len(rows)
            received_times = sorted(r.received_at for r in rows if r.received_at)
        # 本地扫描（scanner 已支持 series 过滤）——唯一 SOP 的权威口径。
        summary = scan_local_dicom(study_instance_uid, series_instance_uid)
        return ReceiveStatusOutput(
            success=True,
            storescp_received_count=received_count,
            local_parsed_count=summary["total_files"],
            local_unique_sop_count=summary["total_sop_count"],
            first_received_at=_iso(received_times[0]) if received_times else None,
            last_received_at=_iso(received_times[-1]) if received_times else None,
            series_distribution=summary.get("series", {}),
        )
    except Exception as exc:
        return ReceiveStatusOutput(success=False, error="receive status query failed: %s" % exc)


# ==========================================================================
# 自主循环新增只读工具（plan §2.2）：hierarchy / task_history / worker_health
# / compute_integrity / search_knowledge。粒度更细，给 LLM 真实决策空间。
# ==========================================================================

def query_pacs_hierarchy(source_id: str, study_instance_uid: str) -> PacsHierarchyOutput:
    """C-FIND SERIES 级：返回 Study 下各 Series/Instance 期望数，支撑 LLM 下钻。"""
    try:
        series = PacsClient().find_series(source_id, study_instance_uid)
    except Exception as exc:
        msg = str(exc)
        return PacsHierarchyOutput(
            success=False, error="C-FIND series failed: %s" % msg,
            retryable=_is_transient(msg), study_instance_uid=study_instance_uid,
        )
    nodes = [
        SeriesNode(
            series_instance_uid=s.series_instance_uid,
            modality=s.modality,
            instance_count=s.number_of_series_related_instances,
        )
        for s in series
    ]
    return PacsHierarchyOutput(
        success=True, study_instance_uid=study_instance_uid,
        series_count=len(nodes), series=nodes,
    )


def query_task_history(
    task_id: Optional[str] = None,
    study_instance_uid: Optional[str] = None,
    series_instance_uid: Optional[str] = None,
) -> TaskHistoryOutput:
    """返回目标的累计重试次数、去重错误演变，支撑「反复失败该上报」判断。"""
    if not any([task_id, study_instance_uid, series_instance_uid]):
        return TaskHistoryOutput(success=False, error="at least one of task_id/study/series uid is required")
    try:
        with session_scope() as db:
            q = db.query(DownloadTask)
            if task_id:
                q = q.filter(DownloadTask.task_id == task_id)
            if study_instance_uid:
                q = q.filter(DownloadTask.study_instance_uid == study_instance_uid)
            if series_instance_uid:
                q = q.filter(DownloadTask.series_instance_uid == series_instance_uid)
            rows = q.order_by(DownloadTask.created_at.desc()).all()
            if not rows:
                return TaskHistoryOutput(success=True, task_id=task_id or "", current_status="")
            latest = rows[0]
            total_retry = max((t.task_retry_times or 0) for t in rows)
            # 去重历史错误（保序）。
            seen, distinct = set(), []
            for t in rows:
                err = (t.last_error or "").strip()
                if err and err not in seen:
                    seen.add(err)
                    distinct.append(err)
            repeatedly = (
                total_retry >= _REPEATED_FAILURE_THRESHOLD
                and latest.status != DownloadStatus.SUCCESS.value
            )
            return TaskHistoryOutput(
                success=True, task_id=latest.task_id, total_retry_times=total_retry,
                current_status=latest.status, distinct_errors=distinct,
                last_error=latest.last_error, repeatedly_failing=repeatedly,
            )
    except Exception as exc:
        return TaskHistoryOutput(success=False, error="task history query failed: %s" % exc)


def query_worker_health(queue_name: Optional[str] = None) -> WorkerHealthOutput:
    """查询下载队列的 message/consumer 数，合成 Worker 存活/积压一体信号。"""
    name = queue_name or settings.rabbitmq.download_queue
    connection = None
    try:
        params = pika.URLParameters(settings.rabbitmq.url)
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        # passive=True：只查询已存在队列的状态，不创建/修改队列。
        method = channel.queue_declare(queue=name, durable=True, passive=True)
        msg_count = method.method.message_count
        consumer_count = method.method.consumer_count
        return WorkerHealthOutput(
            success=True, queue_accessible=True, queue_name=name,
            message_count=msg_count, consumer_count=consumer_count,
            worker_alive=(consumer_count > 0),
            backlog_likely=(msg_count > 0 and consumer_count == 0),
        )
    except Exception as exc:
        msg = str(exc)
        return WorkerHealthOutput(
            success=False, error="worker health query failed: %s" % msg,
            retryable=_is_transient(msg), queue_name=name, queue_accessible=False,
        )
    finally:
        try:
            if connection and connection.is_open:
                connection.close()
        except Exception:
            pass


def compute_integrity(
    expected: Optional[int] = None,
    local_unique_sop: int = 0,
    level: str = "study",
) -> ComputeIntegrityOutput:
    """护栏 1：完整性数量比对做成 LLM 必调工具，算术归代码。

    只做算术，不做任何根因判断（根因是 LLM 的活）。
    missing = max(expected - local_unique_sop, 0)；expected 缺失 → unverified。
    """
    if expected is None:
        return ComputeIntegrityOutput(
            success=True, level=level, expected=None,
            local_unique_sop=local_unique_sop, missing=None, result="unverified",
        )
    missing = max(expected - local_unique_sop, 0)
    return ComputeIntegrityOutput(
        success=True, level=level, expected=expected,
        local_unique_sop=local_unique_sop, missing=missing,
        result="complete" if missing == 0 else "incomplete",
    )


def query_missing_instances(
    source_id: str,
    study_instance_uid: str,
    series_instance_uid: str,
) -> MissingInstancesOutput:
    """IMAGE 级差集：PACS 侧 SOP 集合 − 本地 SOP 集合 = 缺失的具体 SOP 列表。

    差集算术归代码（同护栏 1），LLM 只消费结果并据此填 missing_targets——
    SOP UID 绝不能由模型编造，必须来自本工具输出。
    """
    try:
        instances = PacsClient().find_instances(source_id, study_instance_uid, series_instance_uid)
    except Exception as exc:
        msg = str(exc)
        return MissingInstancesOutput(
            success=False, error="C-FIND image failed: %s" % msg, retryable=_is_transient(msg),
            study_instance_uid=study_instance_uid, series_instance_uid=series_instance_uid,
        )
    try:
        summary = scan_local_dicom(study_instance_uid, series_instance_uid)
    except Exception as exc:
        return MissingInstancesOutput(
            success=False, error="local scan failed: %s" % exc,
            study_instance_uid=study_instance_uid, series_instance_uid=series_instance_uid,
        )

    pacs_sops = {i.sop_instance_uid for i in instances if i.sop_instance_uid}
    local_sops = set(summary.get("sop_instance_uid_list") or [])
    missing = sorted(pacs_sops - local_sops)
    return MissingInstancesOutput(
        success=True, study_instance_uid=study_instance_uid, series_instance_uid=series_instance_uid,
        pacs_sop_count=len(pacs_sops), local_sop_count=len(local_sops),
        missing_count=len(missing), missing_sop_instance_uids=missing,
    )


def search_knowledge(
    query: str,
    category: Optional[str] = None,
    top_n: Optional[int] = None,
) -> SearchKnowledgeOutput:
    """Agentic RAG：LLM 自己决定何时查知识，而非固定检索节点。"""
    try:
        from app.agent.rag import retriever
        from app.agent.rag.embeddings import embeddings_available

        if not embeddings_available():
            return SearchKnowledgeOutput(
                success=True, query=query, available=False,
                error="embedding/knowledge base unavailable",
            )
        where = {"category": category} if category else None
        items = retriever.retrieve(query, where=where, top_n=top_n)
        hits = [
            KnowledgeItem(
                content=(it.get("content") or "")[:300],
                category=(it.get("metadata") or {}).get("category"),
                source=(it.get("metadata") or {}).get("source"),
            )
            for it in items
        ]
        return SearchKnowledgeOutput(success=True, query=query, hits=hits, available=True)
    except Exception as exc:
        return SearchKnowledgeOutput(success=False, query=query, error="knowledge search failed: %s" % exc)


# StructuredTool 封装：暴露 Pydantic args_schema 给 bind_tools，参数非法由 Pydantic 拦截。
query_pacs_target_tool = StructuredTool.from_function(
    func=query_pacs_target,
    name="query_pacs_target",
    description="检查 PACS 连通性并查询目标 Study/Series 元数据（一次调用完成 C-ECHO + C-FIND）。",
    args_schema=PacsTargetInput,
)
query_pacs_hierarchy_tool = StructuredTool.from_function(
    func=query_pacs_hierarchy,
    name="query_pacs_hierarchy",
    description="C-FIND SERIES 级下钻：列出 Study 下各 Series 及其期望 Instance 数。用于定位到底缺哪个 Series。",
    args_schema=PacsHierarchyInput,
)
query_task_context_tool = StructuredTool.from_function(
    func=query_task_context,
    name="query_task_context",
    description="查询补拉任务当前状态与阶段时间线（queued/download_started/move_finished/checked 等）。",
    args_schema=TaskContextInput,
)
query_task_history_tool = StructuredTool.from_function(
    func=query_task_history,
    name="query_task_history",
    description="查询目标历史累计重试次数与错误演变，判断是否反复失败（该上报而非再重试）。",
    args_schema=TaskHistoryInput,
)
query_worker_health_tool = StructuredTool.from_function(
    func=query_worker_health,
    name="query_worker_health",
    description="查询下载队列消费者数与积压，判断 Worker 是否存活/队列是否堵住。任务 in_queue 停滞时调用。",
    args_schema=WorkerHealthInput,
)
query_receive_status_tool = StructuredTool.from_function(
    func=query_receive_status,
    name="query_receive_status",
    description="查询 storescp 接收登记数与本地扫描结果。唯一 SOP 以本地 local_dicom_file 为权威口径。",
    args_schema=ReceiveStatusInput,
)
compute_integrity_tool = StructuredTool.from_function(
    func=compute_integrity,
    name="compute_integrity",
    description="完整性算术：给定 expected 与 local_unique_sop，返回缺口 missing 与 result。数量必须调此工具，不许自行计算。",
    args_schema=ComputeIntegrityInput,
)
query_missing_instances_tool = StructuredTool.from_function(
    func=query_missing_instances,
    name="query_missing_instances",
    description="IMAGE 级差集：列出某 Series 下 PACS 有、本地缺的具体 SOPInstanceUID。"
                "要做 sop 粒度定向补拉时必须先调此工具取目标，不得自行编造 SOP UID。",
    args_schema=MissingInstancesInput,
)
search_knowledge_tool = StructuredTool.from_function(
    func=search_knowledge,
    name="search_knowledge",
    description="按需检索领域知识/故障 SOP/历史经验。仅在需要外部知识判断时调用（非每次必调）。",
    args_schema=SearchKnowledgeInput,
)

# 9 个只读工具白名单（写工具 execute_repull_plan 不在其中——它必经审批节点）。
READ_ONLY_TOOLS: List[StructuredTool] = [
    query_pacs_target_tool,
    query_pacs_hierarchy_tool,
    query_task_context_tool,
    query_task_history_tool,
    query_worker_health_tool,
    query_receive_status_tool,
    query_missing_instances_tool,
    compute_integrity_tool,
    search_knowledge_tool,
]
READ_ONLY_TOOL_NAMES = {t.name for t in READ_ONLY_TOOLS}

# 写工具断言：绝不在只读白名单内（护栏 4）。
assert "execute_repull_plan" not in READ_ONLY_TOOL_NAMES, "写工具不得进入只读白名单"


# ==========================================================================
# 写工具 execute_repull_plan（plan §6 / spec §6）——唯一写操作。
# 绝不绑定给模型、绝不经 MCP 暴露——只能在审批通过后由固定节点调用。
# 按 strategy 分派：retry_task / targeted_cmove / escalate（护栏 4）。
# Redis 短期锁 → 写审计 → 投递 RabbitMQ（escalate 不投递），不同步等待补拉完成。
# ==========================================================================

_VALID_STRATEGIES = {"retry_task", "targeted_cmove", "escalate"}


def _redis_client():
    import redis

    return redis.Redis.from_url(settings.redis.url)


def _clean_targets(missing_targets):
    """归一化定向目标为干净的 dict 列表（丢弃无 Series UID 的项）。

    写工具也可能被 API/测试直接调用，不能假设上游一定已过 RepullScope。
    """
    targets = []
    for item in missing_targets or []:
        if not isinstance(item, dict):
            continue
        series = item.get("series_instance_uid")
        if not series:
            continue
        sops = [str(s) for s in (item.get("sop_instance_uid_list") or []) if s]
        targets.append({"series_instance_uid": series, "sop_instance_uid_list": sops})
    return targets


def scope_fingerprint(missing_targets: Optional[List[dict]]) -> str:
    """把定向范围压成稳定短指纹，作为幂等键的一部分。

    为何必须：幂等键原为 task_id:strategy。同一 task 先补 Series A、失败后重新诊断再补 Series B
    是两次不同的合法写操作，键相同会被同一把锁误挡；而 task_id 为空（手动下载场景）时
    键退化成 None:strategy，跨不同 Study 互相撞锁。范围进键两头兼顾。
    无范围时返回空串——retry_task 的键形如 "T1:retry_task:"，与原键语义等价。
    """
    import hashlib

    items = []
    for t in _clean_targets(missing_targets):
        sops = ",".join(sorted(t["sop_instance_uid_list"]))
        items.append(t["series_instance_uid"] + "|" + sops)
    if not items:
        return ""
    return hashlib.sha1(";".join(sorted(items)).encode("utf-8")).hexdigest()[:12]


def execute_repull_plan(
    run_id: str,
    task_id: Optional[str] = None,
    strategy: str = "escalate",
    idempotency_key: str = "",
    missing_targets: Optional[List[dict]] = None,
    study_instance_uid: Optional[str] = None,
    source_id: Optional[str] = None,
    operator: Optional[str] = None,
    force: bool = False,
) -> "RepullExecutionOutput":
    """经审批后执行补拉计划。按 strategy + 范围分派，返回 RepullExecutionOutput。

    分派表：
    - escalate                     → 只写审计，不投队列
    - targeted_cmove + 有定向目标   → 新建独立定向任务（原失败任务保持 FAIL 不动）
    - targeted_cmove + 无定向目标   → 原任务整体重投 + degraded（无从定向，如实标注）
    - retry_task + task 存在        → 原任务整体重投
    - 任一策略 + task 不存在        → 新建任务（用户手动下载、系统内无 task 记录的场景）
    """
    from app.agent.tool_schemas import RepullExecutionOutput

    if strategy not in _VALID_STRATEGIES:
        return RepullExecutionOutput(success=False, strategy=strategy, task_id=task_id or "",
                                     error="unknown strategy: %s" % strategy)
    targets = _clean_targets(missing_targets)
    audit_task_id = task_id or study_instance_uid or ""

    # 1) Redis 短期锁：SET idempotency:{key} {run_id} NX EX ttl，防审批接口并发重复提交。
    lock_key = "idempotency:%s" % idempotency_key
    try:
        client = _redis_client()
        acquired = client.set(lock_key, run_id, nx=True, ex=settings.agent.approval_lock_ttl_seconds)
    except Exception as exc:  # Redis 不可用不得静默放行写操作
        return RepullExecutionOutput(success=False, strategy=strategy, task_id=task_id or "",
                                     error="approval lock unavailable: %s" % exc, retryable=True)
    if not acquired:
        return RepullExecutionOutput(success=False, strategy=strategy, task_id=task_id or "",
                                     error="duplicate execution within lock TTL")

    try:
        # escalate：只写审计 + 标记上报，不投递队列（护栏 6：知道该放手时放手）。
        if strategy == "escalate":
            with session_scope() as db:
                audit = _audit(db, run_id, audit_task_id, operator, idempotency_key, "escalated", strategy,
                               {"missing_targets": targets,
                                "level": derive_repull_level(targets)})
                db.flush()
                audit_id = str(audit.id)
            return RepullExecutionOutput(success=True, strategy=strategy, task_id=task_id or "",
                                         audit_log_id=audit_id, submitted=False,
                                         note="escalated for human handling; no queue submission")

        existing = _load_task_brief(task_id) if task_id else None
        # 无既有任务（手动下载场景）或有精确定向目标 → 新建任务，不动原任务。
        if existing is None or (strategy == "targeted_cmove" and targets):
            study_uid = study_instance_uid or (existing or {}).get("study_instance_uid")
            if not study_uid:
                return RepullExecutionOutput(
                    success=False, strategy=strategy, task_id=task_id or "",
                    error="task not found and no study_instance_uid to create a repull task")
            return _create_repull_task(
                run_id=run_id, origin_task_id=task_id, strategy=strategy,
                idempotency_key=idempotency_key, targets=targets,
                study_instance_uid=study_uid,
                source_id=source_id or (existing or {}).get("source_id") or "orthanc-local",
                operator=operator, task_existed=existing is not None,
            )
        # 有既有任务且无定向目标 → 原任务整体重投。
        return _submit_repull(run_id, task_id, strategy, idempotency_key, operator, force)
    except Exception as exc:
        return RepullExecutionOutput(success=False, strategy=strategy, task_id=task_id or "",
                                     error="execute failed: %s" % exc)


def _load_task_brief(task_id: str) -> Optional[dict]:
    """读既有任务的定位信息（不持有会话），None 表示任务不存在。"""
    with session_scope() as db:
        task = db.query(DownloadTask).filter(DownloadTask.task_id == task_id).first()
        if not task:
            return None
        return {"study_instance_uid": task.study_instance_uid, "source_id": task.source_id,
                "level": task.level, "series_instance_uid": task.series_instance_uid}


def _create_repull_task(run_id, origin_task_id, strategy, idempotency_key, targets,
                        study_instance_uid, source_id, operator, task_existed):
    """新建独立定向补拉任务：原失败任务保持不动，历史与范围各自留痕。

    复用 services.pull.create_pull_task（建任务 + 投队列的单一真相），不重写业务。
    """
    from app.agent.tool_schemas import RepullExecutionOutput
    from app.core.enums import DataLevel
    from app.core.schemas import PullTaskRequest, SeriesPullItem
    from app.services import pull as pull_service

    # 粒度用共享的 derive_repull_level 现算，与审批展示/审计读到的是同一份推导。
    task_level = {"sop": DataLevel.SOP, "series": DataLevel.SERIES,
                  "study": DataLevel.STUDY}[derive_repull_level(targets)]

    series_items = [SeriesPullItem(series_instance_uid=t["series_instance_uid"],
                                  sop_instance_uid_list=t["sop_instance_uid_list"])
                    for t in targets]
    request = PullTaskRequest(
        study_instance_uid=study_instance_uid,
        source_id=source_id,
        level=task_level,
        series_instance_uid=series_items[0].series_instance_uid if len(series_items) == 1 else None,
        series_list=series_items,
    )

    try:
        with session_scope() as db:
            task = pull_service.create_pull_task(request, db)
            new_task_id = task.task_id
            new_status = task.status
    except pull_service.PullError as exc:
        with session_scope() as db:
            _audit(db, run_id, origin_task_id or study_instance_uid or "", operator, idempotency_key,
                   "failed", strategy, {"error": exc.message, "targets": targets})
        return RepullExecutionOutput(success=False, strategy=strategy, task_id=origin_task_id or "",
                                     error="create repull task failed: %s" % exc.message,
                                     retryable=exc.status_code >= 500)

    with session_scope() as db:
        audit = _audit(db, run_id, new_task_id, operator, idempotency_key, "submitted", strategy,
                       {"origin_task_id": origin_task_id, "level": task_level.value,
                        "targets": targets, "created_task": True, "task_existed": task_existed})
        db.flush()
        audit_id = str(audit.id)

    note = "created %s repull task for %d target(s)" % (task_level.value, len(targets))
    if not task_existed:
        note += "; no prior task record (manual download scenario)"
    return RepullExecutionOutput(success=True, strategy=strategy, task_id=new_task_id,
                                 new_status=new_status, audit_log_id=audit_id, submitted=True,
                                 degraded=False, created_task=True, level=task_level.value, note=note)


def _submit_repull(run_id, task_id, strategy, idempotency_key, operator, force):
    """retry_task / targeted_cmove 的共同执行体：重置任务状态并投递下载队列。"""
    from app.agent.tool_schemas import RepullExecutionOutput
    from app.core.enums import ArchiveStatus, DataLevel

    degraded = False
    note = ""
    with session_scope() as db:
        task = db.query(DownloadTask).filter(DownloadTask.task_id == task_id).first()
        if not task:
            _audit(db, run_id, task_id, operator, idempotency_key, "failed", strategy, {"error": "task not found"})
            return RepullExecutionOutput(success=False, strategy=strategy, task_id=task_id, error="task not found")
        if task.status != DownloadStatus.FAIL.value and not force:
            _audit(db, run_id, task_id, operator, idempotency_key, "failed", strategy,
                   {"error": "task status=%s not retryable" % task.status})
            return RepullExecutionOutput(success=False, strategy=strategy, task_id=task_id,
                                         new_status=task.status,
                                         error="only failed task can be retried unless force=true")

        # 走到这里说明 targeted_cmove 没有拿到任何定向目标（有目标的走 _create_repull_task）。
        # 无从定向 → 诚实降级为整任务重试，并在 note/audit 注明，不假装做了定向补拉。
        if strategy == "targeted_cmove":
            if task.level == DataLevel.SERIES.value and task.series_instance_uid:
                note = "targeted_cmove reuses existing series-level task (already series-scoped)"
            else:
                degraded = True
                note = "targeted_cmove degraded to full retry_task (no missing target provided)"
        task_level = task.level

        # 重置为 in_queue + 阶段时间清零。
        task.status = DownloadStatus.IN_QUEUE.value
        task.task_retry_times += 1
        task.last_error = None
        task.queued_at = datetime.utcnow()
        task.download_started_at = None
        task.move_finished_at = None
        task.checked_at = None
        task.failed_at = None
        archive = db.query(ArchiveModel).filter(ArchiveModel.task_id == task.task_id).first()
        if archive:
            archive.status = ArchiveStatus.ARCHIVING.value
            archive.checked = False
            archive.last_error = None
        # 消息体只带 task_id：downloader 按 task_id 从 DB 重查参数（与 pull.task_message 一致）。
        message = {"task_id": task.task_id}
        audit = _audit(db, run_id, task_id, operator, idempotency_key, "submitted", strategy,
                       {"retry_times": task.task_retry_times, "degraded": degraded,
                        "level": task.level})
        db.flush()
        audit_id = str(audit.id)
    # 投递 RabbitMQ（事务提交后），只返回 in_queue，不等待补拉完成。
    publish_download_task(message)
    return RepullExecutionOutput(success=True, strategy=strategy, task_id=task_id,
                                 new_status=DownloadStatus.IN_QUEUE.value, audit_log_id=audit_id,
                                 submitted=True, degraded=degraded, created_task=False,
                                 level=task_level, note=note)


def _audit(db, run_id, task_id, operator, idempotency_key, result, action, detail):
    from app.core.models import AgentActionAudit

    # action 列为 String(32)：用短前缀 "repull:<strategy>"（≤21 字符）避免 MySQL 截断。
    audit = AgentActionAudit(
        run_id=run_id, task_id=task_id, action="repull:%s" % action, operator=operator,
        idempotency_key=idempotency_key, result=result, detail=detail,
    )
    db.add(audit)
    return audit
