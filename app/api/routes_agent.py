"""Agent API（spec 13.1）：异步 Run 模型。

POST /agent/chat            → 创建 agent_run + 投递 agent_runs 队列，202 + run_id
GET  /agent/runs/{run_id}   → 读 agent_run 状态；awaiting_approval 同时返回 diagnosis + proposed_action
POST /agent/runs/{run_id}/action → approve/reject；approve 触发 enqueue_retry（写操作必经此审批）
POST /agent/runs/{run_id}/abort  → 紧急止损：叫停正在进行的 C-MOVE 与影像接收
POST /agent/runs/{run_id}/cancel → 只释放 thread，已投递的补拉照常跑完

已移除旧的同步 /diagnose、/retry（spec 13 明确取消）。
"""
import json
import logging
import time
from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from fastapi.responses import StreamingResponse
from app.agent import events as ev
from app.agent.graph import resume_after_approval

from app.core.database import get_db
from app.core.enums import DataLevel, DownloadStatus
from app.core.messaging import publish_agent_run
from app.core.models import AgentRun, DownloadTask, StoreScpImage
from app.core.schemas import (
    AbortRequest,
    AbortResponse,
    ActionRequest,
    ActionResponse,
    CancelRequest,
    ChatRequest,
    ChatResponse,
    RunStatusResponse,
)
from app.services import abort as abort_service


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])

# 「活跃」状态集：这些状态下 thread 被占用，同 thread 新请求需拒绝。
ACTIVE_STATUSES = ("running", "awaiting_approval", "awaiting_repull")
THREAD_ID_CONFLICT_DETAIL = "thread_id 已被占用，请使用新的 thread_id 重试"


@router.post("/chat", response_model=ChatResponse, status_code=status.HTTP_202_ACCEPTED)
def agent_chat(request: ChatRequest, db: Session = Depends(get_db)):
    """创建 Run 并异步执行，立即返回 202 + run_id。

    需求 2：同一 thread 同一时刻只允许一个活跃 Run。已有活跃 Run → 409，客户端必须
    生成新的 thread_id 后重试，不能等待后继续复用冲突 ID。
    查-建之间用 Redis 短锁防并发同 thread 各建一个（复用 execute_repull_plan 的锁思路）。
    """
    if not request.message and not (
        request.task_id or request.study_instance_uid or request.study_instance_uid_list or request.series_instance_uid
    ):
        raise HTTPException(status_code=400, detail="message is required")

    # 未显式传入时创建新会话；调用方后续复用响应中的 thread_id 即可恢复 State。
    thread_id = request.thread_id or str(uuid4())
    lock_token = None
    if thread_id:
        # 1) 先查已有活跃 Run（快速拒绝，不必先拿锁）。
        active = _active_run_for_thread(db, thread_id)
        if active:
            raise HTTPException(status_code=409, detail=THREAD_ID_CONFLICT_DETAIL)
        # 2) 拿 Redis 短锁，防查-建竞态；拿不到说明并发请求正在建 → 409。
        lock_token = _acquire_thread_lock(thread_id)
        if lock_token is None:
            raise HTTPException(status_code=409, detail=THREAD_ID_CONFLICT_DETAIL)

    try:
        # 3) 锁内二次确认（双检，防第一步查空后另一请求已建）。
        if thread_id:
            active = _active_run_for_thread(db, thread_id)
            if active:
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
            publish_agent_run(run_id)
        except Exception as exc:
            run.status = "failed"
            run.error = "agent_runs publish failed: %s" % exc
            db.commit()
            raise HTTPException(status_code=503, detail=run.error)
        return ChatResponse(run_id=run_id, thread_id=thread_id, status="running")
    finally:
        # Run 已落库，活跃判定接手串行控制，短锁可提前释放（活跃判定基于 status，天然释放）。
        if lock_token is not None:
            _release_thread_lock(thread_id, lock_token)


def _active_run_for_thread(db: Session, thread_id: str):
    """返回该 thread 最近一个活跃 Run（running/awaiting_approval/awaiting_repull），无则 None。"""
    return (db.query(AgentRun)
            .filter(AgentRun.thread_id == thread_id, AgentRun.status.in_(ACTIVE_STATUSES))
            .order_by(AgentRun.created_at.desc())
            .first())


def _acquire_thread_lock(thread_id: str) -> Optional[str]:
    """SET NX EX 短锁，返回 token；拿不到返回 None。Redis 不可用时放行（降级不阻断创建）。"""
    from app.core.config import settings

    token = str(uuid4())
    try:
        import redis

        client = redis.Redis.from_url(settings.redis.url)
        ok = client.set("thread:%s:active" % thread_id, token, nx=True, ex=10)
        return token if ok else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("thread 锁不可用，降级放行（依赖活跃判定串行）: %s", exc)
        return token


def _release_thread_lock(thread_id: str, token: str) -> None:
    from app.core.config import settings

    try:
        import redis

        client = redis.Redis.from_url(settings.redis.url)
        # 只释放自己持有的锁（token 匹配才删）。
        if client.get("thread:%s:active" % thread_id) == token.encode():
            client.delete("thread:%s:active" % thread_id)
    except Exception:  # noqa: BLE001
        pass


# 终态集：SSE 据此结束推送。含 cancelled/aborted/rejected——这些状态下 worker 不会再发
# done 事件，若不算终态，SSE 连接会一直挂着等一个永不到来的事件。
_SSE_TERMINAL_STATUSES = {"completed", "partial_failed", "failed", "cancelled", "aborted", "rejected"}
_SSE_POLL_INTERVAL = 0.5
_SSE_HEARTBEAT_INTERVAL = 15


@router.get("/runs/{run_id}/stream")
def stream_run(
    request: Request,
    run_id: str,
    user_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """SSE 过程事件流：逐条推 reason/act/diagnose 等推理轨迹，只读不触发写。

    归属校验：Run 有归属（user_id）时，调用方必须传匹配的 user_id。
    重连恢复：客户端通过 Last-Event-ID 头传上次收到的事件序号，服务端从该偏移继续推送。
    退出条件：收到 done 事件，或 Run 已进入终态（completed/failed）且事件队列排空。
    """
    run = _get_run_or_404(db, run_id)
    if run.user_id and run.user_id != user_id:
        raise HTTPException(status_code=403, detail="run does not belong to this user")

    last_event_id = request.headers.get("last-event-id", "").strip()
    try:
        offset = int(last_event_id) + 1 if last_event_id else 0
    except ValueError:
        offset = 0

    def _gen():
        nonlocal offset
        last_hb = time.monotonic()

        while True:
            batch = ev.read_events(run_id, offset)
            for event in batch:
                if ev.is_done_event(event):
                    yield "event: done\ndata: {}\n\n"
                    return
                yield "id: %d\ndata: %s\n\n" % (offset, json.dumps(event, ensure_ascii=False))
                offset += 1

            if not batch:
                db.refresh(run)
                if run.status in _SSE_TERMINAL_STATUSES:
                    yield "event: done\ndata: {}\n\n"
                    return

                now = time.monotonic()
                if now - last_hb >= _SSE_HEARTBEAT_INTERVAL:
                    yield ": heartbeat\n\n"
                    last_hb = now

            time.sleep(_SSE_POLL_INTERVAL)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
def get_run(run_id: str, db: Session = Depends(get_db)):
    """轮询 Run 状态；awaiting_approval 时必须同时返回 diagnosis + proposed_action（spec 13.3）。"""
    run = _get_run_or_404(db, run_id)
    resp = RunStatusResponse(
        run_id=run.run_id, status=run.status, route=run.route,
        approval_status=run.approval_status,
        batch_summary=run.batch_summary,
        study_results=run.study_results,
    )
    # running 可暂不返回诊断；其余状态（含 awaiting_approval）返回已生成结果。
    if run.status != "running":
        resp.diagnosis = run.diagnosis
        resp.proposed_action = run.proposed_action
    return resp


@router.post("/runs/{run_id}/action", response_model=ActionResponse)
def run_action(run_id: str, request: ActionRequest, db: Session = Depends(get_db)):
    """审批写操作。approve → enqueue_retry；reject → 不写队列。"""
    run = _get_run_or_404(db, run_id)
    if run.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="run is not awaiting approval (status=%s)" % run.status)
    action = request.action.lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action must be approve or reject")

    if run.batch_mode:
        try:
            from app.agent.batch_orchestrator import resume_batch_after_approval
            batch = resume_batch_after_approval(
                run_id, {"study_results": run.study_results or {}}, action, request.operator
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("batch approval failed: %s", run_id)
            run.status = "failed"
            run.error = str(exc)
            db.commit()
            raise HTTPException(status_code=500, detail="batch approval failed: %s" % exc)
        run.study_results = batch["study_results"]
        run.batch_summary = batch["batch_summary"]
        run.approval_status = "approved" if action == "approve" else "rejected"
        run.operator = request.operator
        run.status = "rejected" if action == "reject" else batch["status"]
        db.commit()
        return ActionResponse(run_id=run_id, status=run.status)

    # 审批 = 从图的 human_approval 暂停处 Command(resume) 恢复。
    # approve → 图内 execute 节点执行写操作；reject → 图内直接收尾。写操作只发生在图内固定节点。
    try:
        result_state = resume_after_approval(
            run_id, run.thread_id or run_id, action, request.operator
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("审批恢复失败: %s", run_id)
        raise HTTPException(status_code=500, detail="approval resume failed: %s" % exc)

    action_result = result_state.get("action_result") or {}
    graph_status = result_state.get("status")
    run.approval_status = result_state.get("approval_status")
    run.operator = request.operator

    if action == "reject":
        run.status = "rejected"
        db.commit()
        return ActionResponse(run_id=run_id, status="rejected")

    # approve：execute 节点已跑。执行失败 → failed + 409；成功按 submitted 分派状态。
    if not action_result.get("success"):
        run.status = "failed"
        run.error = action_result.get("error")
        db.commit()
        raise HTTPException(status_code=409, detail=action_result.get("error") or "execute failed")

    # 执行结果不回写 proposed_action：该字段只表达「审批前的提议」，混入结果会造成语义漂移。
    # 执行事实的唯一归宿是 agent_action_audit 表（只追加，含 operator/strategy/detail）。
    # 定向补拉新建了独立任务 → Run 改指向新任务，否则 process_repull_failure 按 task_id
    # 找不到本 Run，失败事件重诊断链会断。
    if action_result.get("created_task") and action_result.get("task_id"):
        run.task_id = action_result["task_id"]
    # graph_status 已是 awaiting_repull（submitted）或 completed（escalate），直接采用。
    run.status = graph_status
    db.commit()
    return ActionResponse(run_id=run_id, status=graph_status)


@router.post("/runs/{run_id}/abort", response_model=AbortResponse)
def abort_run(run_id: str, request: AbortRequest = AbortRequest(), db: Session = Depends(get_db)):
    """紧急止损：误批了大范围补拉，立刻叫停正在进行的 C-MOVE 与影像接收。

    与 /cancel 的分工：/cancel 只释放 thread、不动已投递的补拉（数据照常补全）；
    /abort 是反过来——主动阻止数据继续进来，用于「批错了」而非「不想跟进了」。

    四道递减确定性的闸门（详见 services.abort）：
        L1 队列前 100% 拦住 → L2 目标间砍掉剩余目标 → L3 发 C-CANCEL → L4 storescp 拒收

    诚实边界：
    - C-MOVE 数据流不经过本服务，已发出的 C-STORE 可能续到，靠 L4 拒收兜底；
    - 止损必然在磁盘上留下半个 Study（已收的留、后续的拒），already_received 交代留了多少；
    - Redis 不可用时 flag_set=False，四层闸门都不生效，调用方必须据此改走人工处置。
    """
    run = _get_run_or_404(db, run_id)
    task_id = run.task_id
    if run.batch_mode:
        task_ids = [item.get("task_id") for item in (run.study_results or {}).values()
                    if item.get("task_id")]
        if not task_ids:
            raise HTTPException(status_code=409,
                                detail="batch run has no download task to abort (status=%s)" % run.status)
        flag_set = True
        total_received = 0
        may_still_arrive = False
        for child_task_id in task_ids:
            series_uids, previous_status = _abort_scope(db, child_task_id)
            child = db.query(DownloadTask).filter(DownloadTask.task_id == child_task_id).first()
            child_flag = abort_service.mark_aborted(child_task_id,
                                                     child.study_instance_uid if child else None,
                                                     series_uids)
            flag_set = flag_set and child_flag
            if child and child.status not in (DownloadStatus.SUCCESS.value,
                                              DownloadStatus.FAIL.value,
                                              DownloadStatus.UNVERIFIED.value,
                                              DownloadStatus.CANCEL.value):
                child.status = DownloadStatus.CANCEL.value
                child.last_error = "aborted by operator"
                child.checked_at = datetime.utcnow()
                child.failed_at = datetime.utcnow()
            total_received += _received_count(child.study_instance_uid if child else None, series_uids)
            may_still_arrive = may_still_arrive or previous_status == DownloadStatus.DOWNLOADING.value
        run.status = "aborted"
        run.operator = request.operator if request else None
        db.commit()
        return AbortResponse(run_id=run_id, status="aborted", task_id=None,
                             flag_set=flag_set, already_received=total_received,
                             may_still_arrive=may_still_arrive,
                             note=None if flag_set else "部分 Study 的止损标记写入失败")
    if not task_id:
        raise HTTPException(status_code=409,
                            detail="run has no download task to abort (status=%s)" % run.status)

    # 止损范围：定向补拉只停点名的 Series，避免误伤同 Study 的其他正常数据。
    series_uids, previous_status = _abort_scope(db, task_id)
    flag_set = abort_service.mark_aborted(task_id, run.study_instance_uid, series_uids)

    # MySQL 是任务是否允许启动的权威来源；Redis 标记只负责打断已开始的热路径。
    task = db.query(DownloadTask).filter(DownloadTask.task_id == task_id).first()
    if task and task.status not in (
        DownloadStatus.SUCCESS.value,
        DownloadStatus.FAIL.value,
        DownloadStatus.UNVERIFIED.value,
        DownloadStatus.CANCEL.value,
    ):
        task.status = DownloadStatus.CANCEL.value
        task.last_error = "aborted by operator"
        task.checked_at = datetime.utcnow()
        task.failed_at = datetime.utcnow()

    # Run 落 aborted 终态：离开活跃态即释放 thread，也让补拉失败事件不再接管（同 /cancel 机制）。
    run.status = "aborted"
    if request and request.operator:
        run.operator = request.operator
    diag = dict(run.diagnosis or {})
    diag["aborted"] = {"operator": request.operator if request else None,
                       "reason": request.reason if request else None,
                       "flag_set": flag_set}
    run.diagnosis = diag
    db.commit()

    # 已落盘张数：止损后磁盘上实际留下多少（如实交代半个 Study 的规模）。
    already_received = _received_count(run.study_instance_uid, series_uids)
    may_still_arrive = previous_status == DownloadStatus.DOWNLOADING.value
    note = None
    if not flag_set:
        note = "止损标记写入失败（Redis 不可用），补拉未被拦截，请人工处置"
    elif may_still_arrive:
        note = "C-MOVE 已发出，已在传输的影像会被 storescp 拒收；PACS 侧会记录一批 C-STORE 失败"
    return AbortResponse(
        run_id=run_id, status="aborted", task_id=task_id, flag_set=flag_set,
        task_previous_status=previous_status, already_received=already_received,
        may_still_arrive=may_still_arrive, note=note,
    )


def _abort_scope(db: Session, task_id: str):
    """返回 (定向 Series UID 列表, 任务当前状态)。Series 列表为空表示按整 Study 止损。"""
    task = db.query(DownloadTask).filter(DownloadTask.task_id == task_id).first()
    if not task:
        return [], None
    series_list = (task.task_body or {}).get("series_list") or []
    uids = [i.get("series_instance_uid") for i in series_list if i.get("series_instance_uid")]
    if not uids and task.level != DataLevel.STUDY.value and task.series_instance_uid:
        uids = [task.series_instance_uid]
    return uids, task.status


def _received_count(study_uid: Optional[str], series_uids) -> int:
    """统计止损范围内已落盘的影像数（storescp_image 表按 UID 计数，不扫磁盘）。"""
    if not study_uid:
        return 0
    from app.core.database import session_scope

    with session_scope() as db:
        query = db.query(StoreScpImage).filter(StoreScpImage.study_instance_uid == study_uid)
        if series_uids:
            query = query.filter(StoreScpImage.series_instance_uid.in_(series_uids))
        return query.count()


@router.post("/runs/{run_id}/cancel", response_model=ActionResponse)
def cancel_run(run_id: str, request: CancelRequest = CancelRequest(), db: Session = Depends(get_db)):
    """取消一个活跃 Run，释放 thread（需求 3.6）。

    仅活跃态（running/awaiting_approval/awaiting_repull）可取消，终态幂等返回。
    重要：取消只改 Run 状态；已投递的补拉/下载任务由 downloader/checker 独立驱动，照常后台跑——
    取消后 process_repull_failure 只认 awaiting_repull，Run 离开该状态即不再自动重诊断。
    """
    run = _get_run_or_404(db, run_id)
    if run.status not in ACTIVE_STATUSES:
        # 已终态：幂等返回当前状态，不报错。
        return ActionResponse(run_id=run_id, status=run.status)
    run.status = "cancelled"
    if request and request.operator:
        run.operator = request.operator
    db.commit()
    return ActionResponse(run_id=run_id, status="cancelled")


def _get_run_or_404(db: Session, run_id: str) -> AgentRun:
    run = db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run
