"""Application boundary for resolving a diagnostic target."""

from collections.abc import Mapping
from typing import Any

from src.core.enums import DataLevel
from src.infrastructure.db.repositories import StudySeriesRepository, TaskRepository
from src.infrastructure.db.session import session_scope


def resolve_target(state: Mapping[str, Any]) -> dict:
    errors = list(state.get("errors", []))
    task_id = state.get("task_id")
    study_uid = state.get("study_instance_uid")
    series_uid = state.get("series_instance_uid")
    with session_scope() as db:
        if task_id:
            task = TaskRepository(db).get(task_id)
            if not task:
                return {"route": "clarification", "clarification": "未找到 task_id=%s 对应的任务。" % task_id, "errors": errors}
            if (study_uid and study_uid != task.study_instance_uid) or (series_uid and series_uid != task.series_instance_uid):
                errors.append("用户提供的 Study/Series UID 与 task 记录不一致，以 task 为准")
            level = "series" if task.level == DataLevel.SERIES.value else "study"
            return {"diagnostic_level": level, "study_instance_uid": task.study_instance_uid,
                    "series_instance_uid": task.series_instance_uid if level == "series" else None,
                    "errors": errors}
        if study_uid and not series_uid:
            series = StudySeriesRepository(db).find_by_series_uid(study_uid)
            if series:
                return {"diagnostic_level": "series", "study_instance_uid": series.study_instance_uid,
                        "series_instance_uid": series.series_instance_uid, "errors": errors}
        if series_uid:
            resolved_study = study_uid
            series = StudySeriesRepository(db).find_by_series_uid(series_uid)
            if series:
                if study_uid and study_uid != series.study_instance_uid:
                    return {"route": "clarification", "clarification": "Study 与 Series 归属不一致，请确认参数。", "errors": errors}
                resolved_study = series.study_instance_uid
            return {"diagnostic_level": "series", "study_instance_uid": resolved_study,
                    "series_instance_uid": series_uid, "errors": errors}
    if study_uid:
        return {"diagnostic_level": "study", "study_instance_uid": study_uid, "errors": errors}
    return {"route": "clarification", "clarification": "缺少可定位的 Study/Series 标识。", "errors": errors}


__all__ = ["resolve_target"]
