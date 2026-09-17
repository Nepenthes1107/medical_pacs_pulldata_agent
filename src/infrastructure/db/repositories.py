"""Transactional repositories for the legacy SQLAlchemy models.

Repositories receive the request/worker session from the composition root. They do
not create or commit sessions, which keeps transaction ownership explicit while the
domain code is migrated away from ORM queries.
"""
from collections.abc import Sequence

from sqlalchemy.orm import Session

from src.core.enums import DataLevel
from src.infrastructure.db.models import (
    AgentActionAudit,
    AgentRun,
    ArchiveModel,
    DownloadTask,
    SeriesModel,
    StoreScpImage,
    StudyModel,
)


class RunRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, run_id: str) -> AgentRun | None:
        return self.db.query(AgentRun).filter(AgentRun.run_id == run_id).first()

    def active_for_thread(self, thread_id: str, statuses: Sequence[str]) -> AgentRun | None:
        return (
            self.db.query(AgentRun)
            .filter(AgentRun.thread_id == thread_id, AgentRun.status.in_(tuple(statuses)))
            .order_by(AgentRun.created_at.desc())
            .first()
        )

    def awaiting_repull_for_task(self, task_id: str) -> AgentRun | None:
        return (
            self.db.query(AgentRun)
            .filter(AgentRun.task_id == task_id, AgentRun.status == "awaiting_repull")
            .order_by(AgentRun.created_at.desc())
            .with_for_update()
            .first()
        )

    def awaiting_repull_batches(self) -> list[AgentRun]:
        return (
            self.db.query(AgentRun)
            .filter(AgentRun.batch_mode.is_(True), AgentRun.status == "awaiting_repull")
            .order_by(AgentRun.created_at.desc())
            .all()
        )

    def all_awaiting_repull(self) -> list[AgentRun]:
        return self.db.query(AgentRun).filter(AgentRun.status == "awaiting_repull").all()

    def batch_runs(self, statuses: Sequence[str]) -> list[AgentRun]:
        return (
            self.db.query(AgentRun)
            .filter(AgentRun.batch_mode.is_(True), AgentRun.status.in_(tuple(statuses)))
            .order_by(AgentRun.created_at.desc())
            .all()
        )


class TaskRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, task_id: str) -> DownloadTask | None:
        return self.db.query(DownloadTask).filter(DownloadTask.task_id == task_id).first()

    def locked_get(self, task_id: str) -> DownloadTask | None:
        return (
            self.db.query(DownloadTask)
            .filter(DownloadTask.task_id == task_id)
            .with_for_update()
            .first()
        )

    def find(self, *, task_id: str | None = None,
             study_instance_uid: str | None = None,
             series_instance_uid: str | None = None) -> list[DownloadTask]:
        query = self.db.query(DownloadTask)
        if task_id:
            query = query.filter(DownloadTask.task_id == task_id)
        if study_instance_uid:
            query = query.filter(DownloadTask.study_instance_uid == study_instance_uid)
        if series_instance_uid:
            query = query.filter(DownloadTask.series_instance_uid == series_instance_uid)
        return query.order_by(DownloadTask.created_at.desc()).all()

    def by_status(self, status: str) -> list[DownloadTask]:
        return self.db.query(DownloadTask).filter(DownloadTask.status == status).all()

    def received_images(self, study_instance_uid: str, series_instance_uid: str | None = None):
        query = self.db.query(StoreScpImage).filter(
            StoreScpImage.study_instance_uid == study_instance_uid
        )
        if series_instance_uid:
            query = query.filter(StoreScpImage.series_instance_uid == series_instance_uid)
        return query.all()

    def archive_for(self, task_id: str) -> ArchiveModel | None:
        return self.db.query(ArchiveModel).filter(ArchiveModel.task_id == task_id).first()

    def abort_scope(self, task_id: str) -> tuple[list[str], str | None]:
        task = self.get(task_id)
        if not task:
            return [], None
        series_list = (task.task_body or {}).get("series_list") or []
        series_uids = [item.get("series_instance_uid") for item in series_list
                       if item.get("series_instance_uid")]
        if not series_uids and task.level != DataLevel.STUDY.value and task.series_instance_uid:
            series_uids = [task.series_instance_uid]
        return series_uids, task.status


class ActionAuditRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_key(self, idempotency_key: str) -> AgentActionAudit | None:
        return (
            self.db.query(AgentActionAudit)
            .filter(AgentActionAudit.idempotency_key == idempotency_key)
            .first()
        )

    def require_by_key(self, idempotency_key: str) -> AgentActionAudit:
        return (
            self.db.query(AgentActionAudit)
            .filter(AgentActionAudit.idempotency_key == idempotency_key)
            .one()
        )


class StudySeriesRepository:
    def __init__(self, db: Session):
        self.db = db

    def find_by_series_uid(self, series_instance_uid: str) -> SeriesModel | None:
        return (
            self.db.query(SeriesModel)
            .filter(SeriesModel.series_instance_uid == series_instance_uid)
            .first()
        )

    def find_for_study(self, study_instance_uid: str, series_instance_uid: str) -> SeriesModel | None:
        return (
            self.db.query(SeriesModel)
            .filter(
                SeriesModel.study_instance_uid == study_instance_uid,
                SeriesModel.series_instance_uid == series_instance_uid,
            )
            .first()
        )

    def study(self, study_instance_uid: str) -> StudyModel | None:
        return (
            self.db.query(StudyModel)
            .filter(StudyModel.study_instance_uid == study_instance_uid)
            .first()
        )

    def received_count(self, study_uid: str | None, series_uids: Sequence[str]) -> int:
        if not study_uid:
            return 0
        query = self.db.query(StoreScpImage).filter(StoreScpImage.study_instance_uid == study_uid)
        if series_uids:
            query = query.filter(StoreScpImage.series_instance_uid.in_(series_uids))
        return query.count()
