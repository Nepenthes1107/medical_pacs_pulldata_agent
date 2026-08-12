"""首次拉取直达节点（plan-autonomous-v2 §1.2）。

首拉本质是「建 DownloadTask → 投 download_queue」，无多信号关联推断空间，
故不进 reason↔act 循环，直接复用 services.pull.create_pull_task（与 /tasks/pull 同一份逻辑）。
首拉视为常规业务动作、不需人工审批（护栏 4 仍只管补拉等诊断结论触发的写操作）。
"""
import logging
from typing import Dict

from app.agent.state import AgentState
from app.core.database import session_scope
from app.core.enums import DataLevel
from app.core.schemas import PullTaskRequest
from app.services import pull as pull_service

logger = logging.getLogger(__name__)


def first_pull(state: AgentState) -> Dict:
    """建首拉任务并投队列，结构化结果写入 state，status=completed 直接收尾。"""
    study_uid = state.get("study_instance_uid")
    series_uid = state.get("series_instance_uid")
    source_id = state.get("source_id", "orthanc-local")

    if not study_uid and not series_uid:
        return {"status": "completed", "route": "clarification",
                "diagnosis": {"summary": "首次拉取需要 StudyInstanceUID / SeriesInstanceUID。",
                              "route": "clarification"}}

    level = DataLevel.SERIES if series_uid else DataLevel.STUDY
    request = PullTaskRequest(
        study_instance_uid=study_uid or "",
        source_id=source_id,
        level=level,
        series_instance_uid=series_uid,
    )
    try:
        with session_scope() as db:
            task = pull_service.create_pull_task(request, db)
            task_id = task.task_id
            status = task.status
    except pull_service.PullError as exc:
        logger.warning("first_pull 建任务失败: %s", exc)
        return {"status": "failed", "route": "first_pull",
                "diagnosis": {"summary": "首次拉取失败：%s" % exc.message, "route": "first_pull"},
                "errors": list(state.get("errors", [])) + [exc.message]}
    except Exception as exc:  # noqa: BLE001
        logger.exception("first_pull 异常")
        return {"status": "failed", "route": "first_pull",
                "diagnosis": {"summary": "首次拉取异常：%s" % exc, "route": "first_pull"},
                "errors": list(state.get("errors", [])) + [str(exc)]}

    return {
        "status": "completed",
        "route": "first_pull",
        "task_id": task_id,
        "diagnosis": {
            "summary": "首次拉取任务已创建并入队（task_id=%s）。" % task_id,
            "route": "first_pull",
            "task_id": task_id,
            "task_status": status,
        },
    }
