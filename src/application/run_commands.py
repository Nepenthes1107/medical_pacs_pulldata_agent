"""PACS Run command application boundary.

This module owns the persistence and queueing contract for creating and reading
Runs.  HTTP routes only translate requests and responses; the legacy URL module
continues to expose the same functions during the compatibility window.
"""
import logging
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.agents.pacs.graph import resume_after_approval
from src.application import abort as abort_service
from src.core.enums import DownloadStatus
from src.core.settings import settings
from src.infrastructure.db.models import AgentRun
from src.infrastructure.db.repositories import (
    RunRepository,
    StudySeriesRepository,
    TaskRepository,
)
from src.infrastructure.db.session import SessionLocal
from src.infrastructure.messaging import publisher
from src.schema.pacs import (
    AbortRequest,
    AbortResponse,
    ActionRequest,
    ActionResponse,
    ChatRequest,
    ChatResponse,
    RunStatusResponse,
)

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("running", "awaiting_approval", "awaiting_repull")
THREAD_ID_CONFLICT_DETAIL = "thread_id 已被占用，请使用新的 thread_id 重试"


def get_run_or_404(db: Session, run_id: str) -> AgentRun:
    run = RunRepository(db).get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


def create_run(request: ChatRequest, db: Session) -> ChatResponse:
    if not request.message and not (
        request.task_id or request.study_instance_uid or request.study_instance_uid_list
        or request.series_instance_uid
    ):
        raise HTTPException(status_code=400, detail="message is required")
    thread_id = request.thread_id or str(uuid4())
    if RunRepository(db).active_for_thread(thread_id, ACTIVE_STATUSES):
        raise HTTPException(status_code=409, detail=THREAD_ID_CONFLICT_DETAIL)
    lock_token = _acquire_thread_lock(thread_id)
    if lock_token is None:
        raise HTTPException(status_code=409, detail=THREAD_ID_CONFLICT_DETAIL)
    try:
        if RunRepository(db).active_for_thread(thread_id, ACTIVE_STATUSES):
            raise HTTPException(status_code=409, detail=THREAD_ID_CONFLICT_DETAIL)
        run_id = str(uuid4())
        run = AgentRun(
            run_id=run_id, status="running", message=request.message, intent=request.intent,
            thread_id=thread_id, user_id=request.user_id, source_id=request.source_id,
            task_id=request.task_id, study_instance_uid=request.study_instance_uid,
            series_instance_uid=request.series_instance_uid,
            study_instance_uid_list=request.study_instance_uid_list or None,
            batch_mode=bool(request.study_instance_uid_list),
        )
        db.add(run)
        db.commit()
        try:
            publisher.agent_run(run_id)
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.error = "agent_runs publish failed: %s" % exc
            db.commit()
            raise HTTPException(status_code=503, detail=run.error) from exc
        return ChatResponse(run_id=run_id, thread_id=thread_id, status="running")
    finally:
        _release_thread_lock(thread_id, lock_token)


def _acquire_thread_lock(thread_id: str) -> str | None:
    token = str(uuid4())
    try:
        import redis
        client = redis.Redis.from_url(settings.redis.url)
        return token if client.set("thread:%s:active" % thread_id, token, nx=True, ex=10) else None
    except Exception as exc:  # noqa: BLE001
        # Database active-state checks remain authoritative if Redis is unavailable.
        logger.warning("thread lock unavailable; using database active-state check: %s", exc)
        return token


def _release_thread_lock(thread_id: str, token: str) -> None:
    try:
        import redis
        client = redis.Redis.from_url(settings.redis.url)
        if client.get("thread:%s:active" % thread_id) == token.encode():
            client.delete("thread:%s:active" % thread_id)
    except Exception:
        pass


def read_run(run_id: str, db: Session) -> RunStatusResponse:
    run = get_run_or_404(db, run_id)
    response = RunStatusResponse(
        run_id=run.run_id, status=run.status, route=run.route,
        approval_status=run.approval_status, batch_summary=run.batch_summary,
        study_results=run.study_results,
    )
    if run.status != "running":
        response.diagnosis = run.diagnosis
        response.proposed_action = run.proposed_action
    return response


def approve_run(run_id: str, request: ActionRequest, db: Session) -> ActionResponse:
    run = get_run_or_404(db, run_id)
    if run.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="run is not awaiting approval (status=%s)" % run.status)
    action = request.action.lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action must be approve or reject")
    if run.batch_mode:
        try:
            from src.application.batch import resume_batch_after_approval
            batch = resume_batch_after_approval(run_id, {"study_results": run.study_results or {}}, action, request.operator)
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.error = str(exc)
            db.commit()
            raise HTTPException(status_code=500, detail="batch approval failed: %s" % exc) from exc
        run.study_results = batch["study_results"]
        run.batch_summary = batch["batch_summary"]
        run.approval_status = "approved" if action == "approve" else "rejected"
        run.operator = request.operator
        run.status = "rejected" if action == "reject" else batch["status"]
        db.commit()
        return ActionResponse(run_id=run_id, status=run.status)
    try:
        result_state = resume_after_approval(run_id, run.thread_id or run_id, action, request.operator)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="approval resume failed: %s" % exc) from exc
    action_result = result_state.get("action_result") or {}
    run.approval_status = result_state.get("approval_status")
    run.operator = request.operator
    if action == "reject":
        run.status = "rejected"
        db.commit()
        return ActionResponse(run_id=run_id, status="rejected")
    if not action_result.get("success"):
        run.status = "failed"
        run.error = action_result.get("error")
        db.commit()
        raise HTTPException(status_code=409, detail=action_result.get("error") or "execute failed")
    if action_result.get("created_task") and action_result.get("task_id"):
        run.task_id = action_result["task_id"]
    next_status = result_state.get("status")
    if next_status is not None:
        run.status = next_status
    db.commit()
    return ActionResponse(run_id=run_id, status=run.status)


def _abort_scope(db: Session, task_id: str):
    return TaskRepository(db).abort_scope(task_id)


def _received_count(study_uid: str | None, series_uids) -> int:
    if not study_uid:
        return 0
    with SessionLocal() as db:
        return StudySeriesRepository(db).received_count(study_uid, series_uids)


def abort_run(run_id: str, request: AbortRequest, db: Session) -> AbortResponse:
    run = get_run_or_404(db, run_id)
    task_id = run.task_id
    if run.batch_mode:
        task_ids = [item.get("task_id") for item in (run.study_results or {}).values() if item.get("task_id")]
        if not task_ids:
            raise HTTPException(status_code=409, detail="batch run has no download task to abort (status=%s)" % run.status)
        flag_set = True
        total_received = 0
        may_still_arrive = False
        for child_task_id in task_ids:
            series_uids, previous_status = _abort_scope(db, child_task_id)
            child = TaskRepository(db).get(child_task_id)
            child_flag = abort_service.mark_aborted(child_task_id, child.study_instance_uid if child else None, series_uids)
            flag_set = flag_set and child_flag
            if child and child.status not in (DownloadStatus.SUCCESS.value, DownloadStatus.FAIL.value,
                                              DownloadStatus.UNVERIFIED.value, DownloadStatus.CANCEL.value):
                child.status = DownloadStatus.CANCEL.value
                child.last_error = "aborted by operator"
                child.checked_at = datetime.utcnow()
                child.failed_at = datetime.utcnow()
            total_received += _received_count(child.study_instance_uid if child else None, series_uids)
            may_still_arrive = may_still_arrive or previous_status == DownloadStatus.DOWNLOADING.value
        run.status = "aborted"
        run.operator = request.operator if request else None
        db.commit()
        return AbortResponse(run_id=run_id, status="aborted", flag_set=flag_set,
                             already_received=total_received, may_still_arrive=may_still_arrive,
                             note=None if flag_set else "部分 Study 的止损标记写入失败")
    if not task_id:
        raise HTTPException(status_code=409, detail="run has no download task to abort (status=%s)" % run.status)
    series_uids, previous_status = _abort_scope(db, task_id)
    flag_set = abort_service.mark_aborted(task_id, run.study_instance_uid, series_uids)
    task = TaskRepository(db).get(task_id)
    if task and task.status not in (DownloadStatus.SUCCESS.value, DownloadStatus.FAIL.value,
                                    DownloadStatus.UNVERIFIED.value, DownloadStatus.CANCEL.value):
        task.status = DownloadStatus.CANCEL.value
        task.last_error = "aborted by operator"
        task.checked_at = datetime.utcnow()
        task.failed_at = datetime.utcnow()
    run.status = "aborted"
    if request and request.operator:
        run.operator = request.operator
    diagnosis = dict(run.diagnosis or {})
    diagnosis["aborted"] = {"operator": request.operator if request else None,
                             "reason": request.reason if request else None, "flag_set": flag_set}
    run.diagnosis = diagnosis
    db.commit()
    already_received = _received_count(run.study_instance_uid, series_uids)
    may_still_arrive = previous_status == DownloadStatus.DOWNLOADING.value
    note = None
    if not flag_set:
        note = "止损标记写入失败（Redis 不可用），补拉未被拦截，请人工处置"
    elif may_still_arrive:
        note = "C-MOVE 已发出，已在传输的影像会被 storescp 拒收；PACS 侧会记录一批 C-STORE 失败"
    return AbortResponse(run_id=run_id, status="aborted", task_id=task_id, flag_set=flag_set,
                         task_previous_status=previous_status, already_received=already_received,
                         may_still_arrive=may_still_arrive, note=note)


def cancel_run(run_id: str, request, db: Session) -> ActionResponse:
    run = get_run_or_404(db, run_id)
    if run.status not in ACTIVE_STATUSES:
        return ActionResponse(run_id=run_id, status=run.status)
    run.status = "cancelled"
    if request and request.operator:
        run.operator = request.operator
    db.commit()
    return ActionResponse(run_id=run_id, status="cancelled")


__all__ = ["ACTIVE_STATUSES", "approve_run", "create_run", "abort_run", "cancel_run",
           "get_run_or_404", "read_run"]
