"""Agent Worker：消费 agent_runs 与 repull_events 两个队列。

- agent_runs：流式执行自主诊断循环图（reason↔act↔observe），推过程事件（需求 4），
  把业务结果写入 agent_run 表。循环收束后 plan_repull 若产出补拉计划 → status=awaiting_approval，
  API 侧 /action 用 Command(resume) 恢复图执行，由图内 execute 节点落写操作（必经审批，护栏 4）。
- repull_events：补拉失败事件（Downloader move 失败 / Checker 超时不完整）→ 找 awaiting_repull
  的 Run → 重新运行正常诊断图，产出新的补拉提议（仍须人工审批，禁止自动执行）。完整性判定已由
  Checker 唯一裁定，本次不再重新证明「完整性够不够」。事件丢失由 sweep_awaiting_repull_timeouts 兜底。
"""
import json
import logging
from datetime import datetime

import pika

from src.agents.pacs.graph import get_checkpointer, run_agent_streaming
from src.core.settings import settings
from src.infrastructure.db.models import AgentRun
from src.infrastructure.db.repositories import RunRepository
from src.infrastructure.db.session import session_scope

logger = logging.getLogger(__name__)


def process_run(run_id: str) -> None:
    """执行一个 Agent Run 并把结果写回 agent_run 表。"""
    with session_scope() as db:
        run = RunRepository(db).get(run_id)
        if not run:
            raise RuntimeError("agent_run not found: %s" % run_id)
        if run.status not in ("running", None):
            logger.info("skip run %s in status %s", run_id, run.status)
            return
        message = run.message or ""
        intent = run.intent
        task_id = run.task_id
        study_uid = run.study_instance_uid
        series_uid = run.series_instance_uid
        source_id = _source_id_of(run)
        thread_id = run.thread_id or run.run_id
        study_uids = list(run.study_instance_uid_list or [])

    if study_uids:
        from src.application.batch import run_batch
        try:
            checkpointer = get_checkpointer()
            batch = run_batch(message, study_uids, source_id, run_id, intent=intent,
                              checkpointer=checkpointer)
            with session_scope() as db:
                current = RunRepository(db).get(run_id)
                if current:
                    current.route = "diagnosis"
                    current.batch_summary = batch["batch_summary"]
                    current.study_results = batch["study_results"]
                    current.diagnosis = {"route": "diagnosis", "batch_summary": batch["batch_summary"]}
                    current.proposed_action = {
                        "study_results": batch["study_results"],
                        "batch_summary": batch["batch_summary"],
                    } if batch["status"] == "awaiting_approval" else None
                    current.status = batch["status"]
                    current.approval_status = "pending" if batch["status"] == "awaiting_approval" else None
            if batch["status"] != "awaiting_approval":
                from src.infrastructure import events as ev
                ev.mark_done(run_id)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("batch agent run failed: %s", run_id)
            with session_scope() as db:
                current = RunRepository(db).get(run_id)
                if current:
                    current.status = "failed"
                    current.error = str(exc)
            return

    # checkpointer 是 HITL 审批 interrupt/replay 的前提（护栏 4）。初始化失败不静默降级——
    # 否则审批闸门失效，直接把 Run 标 failed 并注明（降级不伪装）。
    try:
        checkpointer = get_checkpointer()
    except Exception as exc:  # noqa: BLE001
        logger.exception("checkpointer 初始化失败，审批闸门不可用: %s", run_id)
        with session_scope() as db:
            run = RunRepository(db).get(run_id)
            if run:
                run.status = "failed"
                run.error = "审批 checkpoint 不可用，无法保证写操作经人工闸门: %s" % exc
        return

    # 图执行在 DB 会话之外（可能耗时：工具采集、可选 LLM）。流式执行以推送过程事件（需求 4）。
    try:
        state = run_agent_streaming(
            message=message, task_id=task_id, study_instance_uid=study_uid,
            series_instance_uid=series_uid, source_id=source_id, run_id=run_id,
            thread_id=thread_id, intent=intent, checkpointer=checkpointer,
        )
    except Exception as exc:
        logger.exception("agent run failed: %s", run_id)
        from src.infrastructure.db.audit import record_exception

        record_exception(run_id, thread_id, exc)
        with session_scope() as db:
            run = RunRepository(db).get(run_id)
            if run:
                run.status = "failed"
                run.error = str(exc)
        return

    _persist_result(run_id, state)


def _source_id_of(run: AgentRun) -> str:
    return run.source_id or "orthanc-local"


def _persist_result(run_id: str, state: dict) -> None:
    """把图最终 state 落到 agent_run（对外可查询业务结果）。"""
    diagnosis = state.get("diagnosis")
    graph_status = state.get("status", "completed")
    route = state.get("route")
    repull_plan = state.get("repull_plan")

    with session_scope() as db:
        run = RunRepository(db).get(run_id)
        if not run:
            return
        run.route = route
        run.diagnostic_level = state.get("diagnostic_level")
        run.study_instance_uid = state.get("study_instance_uid")
        run.series_instance_uid = state.get("series_instance_uid")
        run.task_id = state.get("task_id") or run.task_id
        run.diagnosis = diagnosis
        # awaiting_approval 时 proposed_action 承载参数化补拉计划（plan §4.4）。
        if graph_status == "awaiting_approval" and repull_plan:
            from src.agents.pacs.schemas import derive_repull_level

            run.status = "awaiting_approval"
            run.approval_status = "pending"
            scope = repull_plan.get("scope") if isinstance(repull_plan.get("scope"), dict) else {}
            # level 现算后落进 proposed_action：审批人 GET /runs/{id} 要凭它判断动多大范围。
            run.proposed_action = {"task_id": run.task_id,
                                   "level": derive_repull_level(scope.get("missing_targets") or []),
                                   **repull_plan}
        else:
            run.status = "completed"


# ==========================================================================
# 补拉失败事件 → 找 awaiting_repull 的 Run → 重新运行正常诊断图，产出新的补拉提议。
# 完整性已由 Checker 唯一裁定，这里不再重新证明「完整性够不够」。
# ==========================================================================

def process_repull_failure(task_id: str, terminal_status: str, failure_stage: str = "") -> None:
    """收到补拉失败事件（Downloader move 失败 / Checker 超时不完整）：
    找该 task 的 awaiting_repull Run，重新运行正常诊断图产出新的补拉提议。

    只接受 terminal_status="fail"；其他值记录告警并安全忽略（幂等，不阻断队列）。
    幂等：找不到关联 Run（非本项目触发/已处理过/已取消）直接返回，事件照常 ack。
    禁止在此直接调用 execute_repull_plan——新计划仍须走 human_approval 闸门。
    """
    if terminal_status != "fail":
        logger.warning("忽略非 fail 的补拉事件 task_id=%s terminal_status=%s", task_id, terminal_status)
        return

    with session_scope() as db:
        run = RunRepository(db).awaiting_repull_for_task(task_id)
        batch_uid = None
        if not run:
            for candidate in RunRepository(db).awaiting_repull_batches():
                for uid, item in (candidate.study_results or {}).items():
                    if item.get("task_id") == task_id and item.get("status") == "awaiting_repull":
                        run, batch_uid = candidate, uid
                        break
                if run:
                    break
        if not run:
            logger.info("repull failure event for task %s has no awaiting_repull run, skip", task_id)
            return
        run_id = run.run_id
        study_uid = run.study_instance_uid
        series_uid = run.series_instance_uid
        source_id = _source_id_of(run)
        thread_id = run.thread_id or run.run_id
        # 状态先移出 awaiting_repull，防止重复失败消息或并发取消互相覆盖（幂等要求）。
        if batch_uid:
            item = dict((run.study_results or {}).get(batch_uid) or {})
            item["status"] = "running"
            item["failure_stage"] = failure_stage
            run.study_results = {**(run.study_results or {}), batch_uid: item}
            child_thread = item.get("thread_id") or "%s:study:%s" % (run.run_id, batch_uid)
            run.status = "running"
            batch_run_id = run.run_id
            batch_source = _source_id_of(run)
            batch_thread = child_thread
            batch_parent = run
        else:
            batch_parent = None
        run.status = "running"

    if batch_uid and batch_parent is not None:
        try:
            checkpointer = get_checkpointer()
            state = run_agent_streaming(
                message=("已批准执行的批量补拉任务 Study=%s 在 %s 阶段失败，请重新诊断。"
                         % (batch_uid, failure_stage or "unknown")),
                study_instance_uid=batch_uid, source_id=batch_source,
                run_id=batch_run_id, thread_id=batch_thread, intent="diagnosis",
                checkpointer=checkpointer,
            )
            with session_scope() as db:
                current = RunRepository(db).get(batch_run_id)
                if current:
                    results = dict(current.study_results or {})
                    item = dict(results.get(batch_uid) or {})
                    item.update({"status": state.get("status", "completed"),
                                 "task_id": state.get("task_id") or item.get("task_id"),
                                 "diagnosis": state.get("diagnosis"),
                                 "repull_plan": state.get("repull_plan"),
                                 "approval_status": state.get("approval_status")})
                    results[batch_uid] = item
                    from src.application.batch import aggregate_batch
                    aggregate = aggregate_batch(results, list(results))
                    current.study_results = aggregate["study_results"]
                    current.batch_summary = aggregate["batch_summary"]
                    current.status = aggregate["status"]
                    current.proposed_action = {"study_results": aggregate["study_results"],
                                               "batch_summary": aggregate["batch_summary"]}
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("batch study re-diagnosis failed: %s", batch_uid)
            with session_scope() as db:
                current = RunRepository(db).get(batch_run_id)
                if current:
                    current.status = "partial_failed"
                    current.error = str(exc)
            return
        run.proposed_action = None
        run.approval_status = None
        run.operator = None
        run.error = None

    message = (
        "已批准执行的补拉任务 task_id=%s 在 %s 阶段失败。"
        "请查询该任务最新状态和错误证据，重新诊断并提出新的补拉计划；不得自动执行。"
        % (task_id, failure_stage or "unknown")
    )

    # checkpointer 是 HITL 审批闸门的前提，与 process_run 保持一致的降级纪律。
    try:
        checkpointer = get_checkpointer()
    except Exception as exc:  # noqa: BLE001
        logger.exception("checkpointer 初始化失败，审批闸门不可用: %s", run_id)
        with session_scope() as db:
            run = RunRepository(db).get(run_id)
            if run:
                run.status = "failed"
                run.error = "审批 checkpoint 不可用，无法保证写操作经人工闸门: %s" % exc
        from src.infrastructure import events as ev

        ev.mark_done(run_id)
        return

    try:
        state = run_agent_streaming(
            message=message, task_id=task_id, study_instance_uid=study_uid,
            series_instance_uid=series_uid, source_id=source_id, run_id=run_id,
            thread_id=thread_id, intent="diagnosis", checkpointer=checkpointer,
        )
    except Exception as exc:
        logger.exception("重新诊断失败: %s", run_id)
        from src.infrastructure.db.audit import record_exception

        record_exception(run_id, thread_id, exc)
        with session_scope() as db:
            run = RunRepository(db).get(run_id)
            if run:
                run.status = "failed"
                run.error = str(exc)
        from src.infrastructure import events as ev

        ev.mark_done(run_id)
        return

    _persist_result(run_id, state)


def sweep_awaiting_repull_timeouts(now: datetime | None = None) -> int:
    """事件丢失兜底：awaiting_repull 超时未收到失败事件（success/unverified 已由 Checker
    同事务收尾，不会走到这里）→ completed + 注明，释放永久卡住的 thread。

    不把未知状态伪装成成功，也不写经验。返回被兜底收尾的 Run 数。
    用 updated_at 作超时基准（进入 awaiting_repull 的时间；本次不引入新字段）。
    """
    now = now or datetime.utcnow()
    timeout = settings.agent.repull_terminal_timeout_seconds
    swept = 0
    with session_scope() as db:
        runs = RunRepository(db).all_awaiting_repull()
        for run in runs:
            ref = run.updated_at
            if ref and (now - ref).total_seconds() >= timeout:
                diag = dict(run.diagnosis or {})
                diag["pull_terminal_timeout"] = True
                diag["summary"] = "等待补拉任务终态处理超时，请人工核查任务状态。" + diag.get("summary", "")
                run.diagnosis = diag
                run.status = "completed"
                swept += 1
    return swept


def start_worker() -> None:
    logging.basicConfig(level=getattr(logging, settings.app.log_level.upper(), logging.INFO))
    parameters = pika.URLParameters(settings.rabbitmq.url)
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue=settings.agent.agent_runs_queue, durable=True)
    channel.queue_declare(queue=settings.agent.repull_events_queue, durable=True)
    channel.basic_qos(prefetch_count=1)

    def callback(ch, method, properties, body):
        try:
            payload = json.loads(body.decode("utf-8"))
            process_run(payload["run_id"])
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as exc:
            logger.exception("agent run processing failed: %s", exc)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def repull_callback(ch, method, properties, body):
        try:
            payload = json.loads(body.decode("utf-8"))
            process_repull_failure(payload["task_id"], payload.get("terminal_status", ""),
                                   payload.get("failure_stage", ""))
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as exc:
            logger.exception("repull event processing failed: %s", exc)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    channel.basic_consume(queue=settings.agent.agent_runs_queue, on_message_callback=callback)
    channel.basic_consume(queue=settings.agent.repull_events_queue, on_message_callback=repull_callback)
    logger.info("agent worker started, queues=%s,%s",
                settings.agent.agent_runs_queue, settings.agent.repull_events_queue)
    channel.start_consuming()


if __name__ == "__main__":
    start_worker()
