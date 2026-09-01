import logging
import time
from datetime import datetime
from typing import Optional

from app.core.config import settings
from app.core.database import session_scope
from app.core.enums import ArchiveStatus, DataLevel, DownloadStatus
from app.core.models import AgentRun, ArchiveModel, DownloadTask, SeriesModel, StudyModel
from app.dicom.scanner import scan_local_dicom
from app.workers.agent_worker import sweep_awaiting_repull_timeouts


logger = logging.getLogger(__name__)
UNVERIFIED_MESSAGE = "expected count missing, cannot strictly verify"


def run_once() -> None:
    # Checker 是完整性唯一裁判：success/unverified 在本事务内直接结束关联 AgentRun，不发消息；
    # 只有超时仍不完整才记入失败事件，事务提交后发布（外部副作用不进 DB 事务）。
    completed_runs = []  # [{run_id, proposed_action, outcome}]
    failure_events = []  # [(task_id, failure_stage)]
    with session_scope() as db:
        # 只处理 Downloader 已完成 C-MOVE、等待数据补齐的任务。
        # 终态（success/fail/unverified/cancel）都会把 status 移出 downloaded，
        # 所以单看 status 就已排除已终结的任务，不需要再叠 checked_at IS NULL。
        tasks = (
            db.query(DownloadTask)
            .filter(DownloadTask.status == DownloadStatus.DOWNLOADED.value)
            .all()
        )
        for task in tasks:
            _, local_count = _scan_for_task(task)
            expected_count = _expected_count(db, task)
            archive = db.query(ArchiveModel).filter(ArchiveModel.task_id == task.task_id).first()

            if expected_count is None:
                # expected 缺失：只能说明链路通，无法严格核对 → 终结为 unverified，不再重复扫描。
                _mark_unverified(task, archive, local_count)
                completed = _complete_awaiting_run(db, task, "unverified", expected_count, local_count)
                if completed:
                    completed_runs.append(completed)
                continue

            if local_count >= expected_count:
                _mark_success(task, archive, local_count)
                completed = _complete_awaiting_run(db, task, "success", expected_count, local_count)
                if completed:
                    completed_runs.append(completed)
            elif _is_timeout(task):
                message = "local sop count %s is less than expected %s" % (local_count, expected_count)
                task.status = DownloadStatus.FAIL.value
                task.last_error = message
                task.checked_at = datetime.utcnow()
                task.failed_at = datetime.utcnow()
                if archive:
                    archive.status = ArchiveStatus.FAIL.value
                    archive.archived_image_count = local_count
                    archive.checked = True
                    archive.last_error = message
                # 超时仍不完整：不在此处结束 Run（留 awaiting_repull），只记失败事件，
                # 由 Agent Worker 收到事件后重新诊断并可能改状态。
                failure_events.append((task.task_id, "integrity"))
            # 未超时且未达标：保持 DOWNLOADED、checked_at 仍为 NULL，下一轮run_once()继续扫描等待补齐。

    # 事务已提交，Task/Archive/AgentRun 终态已落库。以下是外部副作用（best-effort）。
    _finish_completed_runs(completed_runs)
    _emit_repull_failures(failure_events)


def _complete_awaiting_run(
    db,
    task: DownloadTask,
    outcome: str,
    expected_count: Optional[int],
    local_count: int,
) -> Optional[dict]:
    """在当前事务内结束该任务关联的 awaiting_repull AgentRun（Checker 是唯一完整性裁判）。

    找不到关联 Run 返回 None（普通非 Agent 下载任务，Checker 仍正常更新 Task/Archive）。
    不在本函数内访问 Redis/RabbitMQ/Embedding——外部副作用交调用侧在事务提交后处理。
    """
    query = db.query(AgentRun).filter(
        AgentRun.task_id == task.task_id, AgentRun.status == "awaiting_repull",
    ).order_by(AgentRun.created_at.desc())
    try:
        query = query.with_for_update()
    except Exception:  # noqa: BLE001  # SQLite 等不支持行锁的后端直接跳过
        pass
    run = query.first()
    if not run:
        return None

    diagnosis = dict(run.diagnosis or {})
    if outcome == "success":
        diagnosis.update({
            "route": "checker",
            "summary": "Checker 验收通过（expected=%s, local=%s）。" % (expected_count, local_count),
            "integrity_result": "complete",
            "expected": expected_count,
            "local": local_count,
            "missing": max((expected_count or 0) - local_count, 0),
            "recommendation": "无需再补拉。",
            "confidence": "confirmed",
            "checker_terminal_status": "success",
        })
    else:  # unverified
        diagnosis.update({
            "route": "checker",
            "summary": "缺少期望数量，Checker 无法严格验证完整性。",
            "integrity_result": "unverified",
            "expected": None,
            "local": local_count,
            "missing": None,
            "recommendation": "请人工检查 PACS 缓存或任务元数据。",
            "confidence": "uncertain",
            "checker_terminal_status": "unverified",
        })

    proposed_action = run.proposed_action
    run.diagnosis = diagnosis
    run.status = "completed"
    return {"run_id": run.run_id, "proposed_action": proposed_action, "outcome": outcome}


def _finish_completed_runs(completed_runs) -> None:
    """事务提交后结束 Checker 已直接收尾 Run 的 SSE。"""
    if not completed_runs:
        return
    from app.agent import events as ev

    for item in completed_runs:
        try:
            ev.mark_done(item["run_id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("mark_done 失败（不影响已提交的 Run 终态）: %s", exc)


def _emit_repull_failures(events) -> None:
    """把补拉失败事件发到 repull_events 队列（best-effort，失败不阻断 Checker）。"""
    if not events:
        return
    try:
        from app.core.messaging import publish_repull_failure

        for task_id, failure_stage in events:
            publish_repull_failure(task_id, failure_stage)
    except Exception as exc:  # noqa: BLE001
        logger.warning("补拉失败事件发布失败（不阻断 checker）: %s", exc)


def _task_targets(task: DownloadTask):
    """任务的定向目标列表 [(series_uid, set(sop_uid))]；空列表表示整 Study 口径。"""
    series_list = (task.task_body or {}).get("series_list") or []
    targets = []
    for item in series_list:
        series_uid = item.get("series_instance_uid")
        if series_uid:
            targets.append((series_uid, set(item.get("sop_instance_uid_list") or [])))
    if not targets and task.level == DataLevel.SERIES.value and task.series_instance_uid:
        targets.append((task.series_instance_uid, set()))
    return targets


def _scan_for_task(task: DownloadTask):
    """按任务真实范围统计本地唯一 SOP 数，返回 (scan_summary, local_count)。

    多 series/sop 任务不能只看单值 series_instance_uid（那只算了第一个 Series，
    完整性会被误判）：一次扫全 Study，再在内存里按各目标过滤求和，避免多次磁盘扫描。
    """
    targets = _task_targets(task)
    if task.level == DataLevel.STUDY.value or not targets:
        summary = scan_local_dicom(task.study_instance_uid, None)
        return summary, summary["total_sop_count"]

    if len(targets) == 1 and not targets[0][1]:
        # 单 Series 且不限 SOP：沿用原有的按 Series 扫描口径。
        summary = scan_local_dicom(task.study_instance_uid, targets[0][0])
        return summary, summary["total_sop_count"]

    summary = scan_local_dicom(task.study_instance_uid, None)
    wanted = {series_uid: sops for series_uid, sops in targets}
    matched = set()
    for inst in summary.get("instances") or []:
        sops = wanted.get(inst["series_instance_uid"])
        if sops is None:
            continue
        # sops 为空 → 该 Series 全部算入；非空 → 只算点名的那几张。
        if not sops or inst["sop_instance_uid"] in sops:
            matched.add(inst["sop_instance_uid"])
    return summary, len(matched)


def _expected_count(db, task: DownloadTask) -> Optional[int]:
    if task.expected_image_number is not None:
        return task.expected_image_number
    if task.level == DataLevel.SOP.value:
        # sop 级任务的期望数只能来自任务自身范围；DB 里的 Series 总数不是它的口径。
        counts = [len(i.get("sop_instance_uid_list") or [])
                  for i in ((task.task_body or {}).get("series_list") or [])]
        return sum(counts) or None
    if task.level == DataLevel.SERIES.value:
        # 多 Series 定向任务：期望数是各目标 Series 期望数之和；任一缺失则整体不可核对
        # （回退到 Study 总数会把「只补两个 Series」误判成「要补全整个 Study」）。
        targets = _task_targets(task)
        if targets:
            total = 0
            for series_uid, _ in targets:
                series = db.query(SeriesModel).filter(
                    SeriesModel.study_instance_uid == task.study_instance_uid,
                    SeriesModel.series_instance_uid == series_uid,
                ).first()
                if not series or series.number_of_series_related_instances is None:
                    return None
                total += series.number_of_series_related_instances
            return total
        return None
    study = db.query(StudyModel).filter(StudyModel.study_instance_uid == task.study_instance_uid).first()
    if study and study.number_of_study_related_instances is not None:
        return study.number_of_study_related_instances
    return None


def _mark_success(task: DownloadTask, archive: Optional[ArchiveModel], local_count: int) -> None:
    task.status = DownloadStatus.SUCCESS.value
    task.last_error = None
    task.checked_at = datetime.utcnow()
    if archive:
        archive.status = ArchiveStatus.FINISHED.value
        archive.archived_image_count = local_count
        archive.checked = True
        archive.last_error = None


def _mark_unverified(task: DownloadTask, archive: Optional[ArchiveModel], local_count: int) -> None:
    # unverified 是 Checker 的独立终结态：期望数缺失 → 链路通但无法严格核对完整性。
    # 不复用 downloaded：那会与「等待补齐中」同值，只能靠 last_error 字符串分辨，
    # 且会被 run_once 反复扫描。checked_at 记录校验时点（可观测性）。
    task.status = DownloadStatus.UNVERIFIED.value
    task.last_error = UNVERIFIED_MESSAGE
    task.checked_at = datetime.utcnow()
    if archive:
        archive.status = ArchiveStatus.UNVERIFIED.value
        archive.archived_image_count = local_count
        archive.checked = True
        archive.last_error = UNVERIFIED_MESSAGE


def _is_timeout(task: DownloadTask) -> bool:
    # 超时基准优先用 C-MOVE 完成时间（move_finished_at）——这是任务进入“等待补齐”阶段的起点；
    # 缺失时回退到 updated_at（兼容旧数据）。
    # 用 datetime.utcnow() 做纯 UTC 差值：阶段时间字段均以 naive UTC 写入，
    # 不能用 naive.timestamp()（它按本地时区解释，非 UTC 机器上会产生时区偏移误判）。
    reference = task.move_finished_at or task.updated_at
    if not reference:
        return False
    return (datetime.utcnow() - reference).total_seconds() >= settings.checker.fail_after_seconds


def start_checker() -> None:
    logging.basicConfig(level=getattr(logging, settings.app.log_level.upper(), logging.INFO))
    logger.info("checker started, interval=%s", settings.checker.interval_seconds)
    while True:
        try:
            run_once()
            # 兜底扫描 awaiting_repull 超时未收到失败事件的 Run，防 thread 永久卡死。
            swept = sweep_awaiting_repull_timeouts()
            if swept:
                logger.warning("awaiting_repull timeout swept %s run(s)", swept)
        except Exception as exc:
            logger.exception("checker failed: %s", exc)
        time.sleep(settings.checker.interval_seconds)


if __name__ == "__main__":
    start_checker()
