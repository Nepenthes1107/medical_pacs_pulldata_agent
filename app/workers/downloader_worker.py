import json
import logging
from datetime import datetime

import pika

from app.core.config import settings
from app.core.database import session_scope
from app.core.enums import DataLevel, DownloadStatus
from app.core.models import DownloadTask
from app.dicom.pacs_client import PacsClient
from app.services import abort


logger = logging.getLogger(__name__)


def process_message(body: bytes) -> None:
    message = json.loads(body.decode("utf-8"))
    task_id = message["task_id"]

    # 短事务 1：进入 C-MOVE 前先把 downloading 与 download_started_at 独立提交，
    # 让状态在耗时的 C-MOVE 期间对外可见（避免长事务导致 downloading 不可观测）。
    move_params = _begin_downloading(task_id)
    if move_params is None:
        return  # 任务不存在（已在 _begin_downloading 抛错）或已取消

    # C-MOVE 在任何 DB 会话之外执行——这是最耗时的一步，不能占着事务/连接。
    try:
        ok, move_result, error = _run_move(move_params)
    except Exception as exc:
        move_result = None
        ok = False
        error = str(exc)

    # 短事务 2：写回最终状态与 move_finished_at / failed_at。
    _finish_move(task_id, ok, move_result, error)


def _begin_downloading(task_id: str):
    """短事务：标记 downloading + download_started_at，返回 C-MOVE 所需参数。"""
    with session_scope() as db:
        task = (db.query(DownloadTask)
                .filter(DownloadTask.task_id == task_id)
                .with_for_update()
                .first())
        if not task:
            raise RuntimeError("task not found: %s" % task_id)
        # RabbitMQ 至少一次投递：只有 in_queue 能领取，其他状态说明已被领取或已终结。
        if task.status != DownloadStatus.IN_QUEUE.value:
            logger.info("skip duplicate/stale task message: %s status=%s", task_id, task.status)
            return None
        # L1 止损闸门：C-MOVE 尚未发起，此时止损 100% 有效（一张影像都不会被推过来）。
        if abort.is_task_aborted(task_id):
            logger.info("止损生效，任务未发起 C-MOVE: %s", task_id)
            task.status = DownloadStatus.CANCEL.value
            task.last_error = "aborted by operator before C-MOVE"
            task.failed_at = datetime.utcnow()
            return None
        task.status = DownloadStatus.DOWNLOADING.value
        task.download_started_at = datetime.utcnow()
        task.last_error = None
        return {
            "task_id": task.task_id,
            "level": task.level,
            "source_id": task.source_id,
            "study_instance_uid": task.study_instance_uid,
            "series_instance_uid": task.series_instance_uid,
            # 多目标定向补拉的范围来源：task_body.series_list（一任务可绑多 series/多 sop）。
            "series_list": (task.task_body or {}).get("series_list") or [],
        }


def _finish_move(task_id: str, ok: bool, move_result, error) -> None:
    """短事务：写回 C-MOVE 最终结果。"""
    move_failed =  False
    with session_scope() as db:
        task = db.query(DownloadTask).filter(DownloadTask.task_id == task_id).first()
        if not task:
            raise RuntimeError("task not found: %s" % task_id)
        if task.status == DownloadStatus.CANCEL.value:
            return  # C-MOVE 期间被取消，不覆盖终态
        task.move_result = move_result
        task.move_finished_at = datetime.utcnow()
        if ok:
            task.status = DownloadStatus.DOWNLOADED.value
            task.last_error = None
        elif abort.is_task_aborted(task_id):
            # 止损中止：落 CANCEL 而非 FAIL，且不发补拉事件——这不是 PACS 故障，
            # 不该被 Agent 当成「补拉失败需再诊断」，是人工主动叫停的终态。
            task.status = DownloadStatus.CANCEL.value
            task.last_error = error or "aborted by operator"
            task.failed_at = datetime.utcnow()
            task.checked_at = datetime.utcnow()  # 终结，Checker 不再扫
        else:
            task.status = DownloadStatus.FAIL.value
            task.last_error = error
            task.failed_at = datetime.utcnow()
            move_failed = True

    # C-MOVE 协议/网络失败（非人工止损）→ 发失败事件，触发 Agent 重新诊断。
    if move_failed:
        try:
            from app.core.messaging import publish_repull_failure

            publish_repull_failure(task_id, "move")
        except Exception as exc:  # noqa: BLE001
            logger.warning("补拉失败事件发布失败（不阻断 downloader）: %s", exc)


def _move_targets(params: dict):
    """按 level 展开本次要执行的 C-MOVE 目标列表。

    study_level  → 一个整 Study 目标
    series_level → task_body.series_list 里每个 Series 一个目标（缺省回退到单值字段）
    sop_level    → 每个 Series 下每张点名的 SOP 一个目标（真 IMAGE 级定向）
    """
    level = params["level"]
    study_uid = params["study_instance_uid"]
    series_list = params.get("series_list") or []

    if level == DataLevel.STUDY.value:
        return [{"level": "STUDY", "study": study_uid}]

    if level == DataLevel.SERIES.value:
        uids = [i.get("series_instance_uid") for i in series_list if i.get("series_instance_uid")]
        if not uids and params.get("series_instance_uid"):
            uids = [params["series_instance_uid"]]
        if not uids:
            raise ValueError("series_instance_uid is required for series level C-MOVE")
        return [{"level": "SERIES", "study": study_uid, "series": uid} for uid in uids]

    if level == DataLevel.SOP.value:
        targets = []
        for item in series_list:
            series_uid = item.get("series_instance_uid")
            if not series_uid:
                continue
            for sop_uid in item.get("sop_instance_uid_list") or []:
                targets.append({"level": "IMAGE", "study": study_uid, "series": series_uid, "sop": sop_uid})
        if not targets:
            raise ValueError("sop level C-MOVE requires series_list with sop_instance_uid_list")
        return targets

    raise ValueError("unsupported task level: %s" % level)


def _run_move(params: dict):
    """执行全部目标并聚合结果，返回 (ok, move_result, error)。

    全部目标成功才算成功；部分失败 → 整体失败并列出失败目标（不谎报成功）。

    L2 止损闸门在目标间：sop 级任务会被 _move_targets 展开成「每张 SOP 一次 C-MOVE」，
    误批一个 500 张的定向补拉就是 500 次串行 C-MOVE，在目标间检查能砍掉绝大部分剩余量。
    范围越大这一层越有效，是大范围误批的主要止损收益。
    """
    client = PacsClient()
    source_id = params["source_id"]
    # task_id 由 _begin_downloading 提供；缺失时 is_task_aborted 返回 False（不止损），
    # 不因闸门缺参把正常补拉打挂。
    task_id = params.get("task_id")
    targets = _move_targets(params)
    # L3 用的回调：C-MOVE 进行中每个 Pending 都会问一次（跨进程读 Redis 标记）。
    should_abort = lambda: abort.is_task_aborted(task_id)  # noqa: E731

    results = []
    failures = []
    skipped = 0
    for index, target in enumerate(targets):
        # L2：每个目标发起前检查，已止损则剩余目标全部不再发起。
        if abort.is_task_aborted(task_id):
            skipped = len(targets) - index
            logger.warning("止损生效，跳过剩余 %s/%s 个 C-MOVE 目标 task_id=%s",
                           skipped, len(targets), task_id)
            break
        if target["level"] == "STUDY":
            result = client.move_study(source_id, target["study"], should_abort=should_abort)
        elif target["level"] == "SERIES":
            result = client.move_series(source_id, target["study"], target["series"],
                                        should_abort=should_abort)
        else:
            result = client.move_instance(source_id, target["study"], target["series"], target["sop"],
                                          should_abort=should_abort)
        results.append(result.model_dump())
        if not result.ok:
            failures.append("%s %s: %s" % (target["level"],
                                           target.get("sop") or target.get("series") or target["study"],
                                           result.message))

    # 被止损：不算成功（数据不全），错误信息如实说明是人工中止而非 PACS 故障。
    ok = not failures and not skipped
    move_result = {
        "level": params["level"],
        "target_count": len(targets),
        "failed_count": len(failures),
        "aborted_target_count": skipped,
        "completed": sum(r.get("completed") or 0 for r in results),
        "failed": sum(r.get("failed") or 0 for r in results),
        "warning": sum(r.get("warning") or 0 for r in results),
        "targets": results,
    }
    if ok:
        error = None
    elif skipped:
        error = "aborted by operator: skipped %d/%d target(s)" % (skipped, len(targets))
    else:
        error = "C-MOVE failed on %d/%d target(s): %s" % (
            len(failures), len(targets), "; ".join(failures[:5]))
    return ok, move_result, error


def start_worker() -> None:
    logging.basicConfig(level=getattr(logging, settings.app.log_level.upper(), logging.INFO))
    parameters = pika.URLParameters(settings.rabbitmq.url)
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue=settings.rabbitmq.download_queue, durable=True)
    channel.basic_qos(prefetch_count=settings.downloader.max_workers)

    def callback(ch, method, properties, body):
        try:
            process_message(body)
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as exc:
            logger.exception("download task failed: %s", exc)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    channel.basic_consume(queue=settings.rabbitmq.download_queue, on_message_callback=callback)
    logger.info("download worker started, queue=%s", settings.rabbitmq.download_queue)
    channel.start_consuming()


if __name__ == "__main__":
    start_worker()
