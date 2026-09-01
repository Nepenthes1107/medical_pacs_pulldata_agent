from contextlib import contextmanager
from types import SimpleNamespace

from app.agent.tools import execute_repull_plan
from app.core.enums import ArchiveStatus, DownloadStatus
from app.core.models import AgentActionAudit, AgentRun
from app.workers import agent_worker, checker_worker, downloader_worker


def test_agent_worker_uses_persisted_source_id():
    run = SimpleNamespace(source_id="pacs-secondary")

    assert agent_worker._source_id_of(run) == "pacs-secondary"


def test_required_database_guards_are_declared():
    assert AgentRun.__table__.c.source_id.nullable is False
    assert AgentActionAudit.__table__.c.idempotency_key.nullable is False
    assert AgentActionAudit.__table__.c.idempotency_key.unique is True


def test_downloader_claims_only_in_queue_task(monkeypatch):
    task = SimpleNamespace(
        task_id="task-1",
        status=DownloadStatus.IN_QUEUE.value,
        level="study_level",
        source_id="orthanc-local",
        study_instance_uid="study-1",
        series_instance_uid=None,
        task_body={},
        download_started_at=None,
        last_error=None,
    )

    class Query:
        def filter(self, *args):
            return self

        def with_for_update(self):
            return self

        def first(self):
            return task

    class Db:
        def query(self, model):
            return Query()

    @contextmanager
    def fake_session_scope():
        yield Db()

    monkeypatch.setattr(downloader_worker, "session_scope", fake_session_scope)
    monkeypatch.setattr(downloader_worker.abort, "is_task_aborted", lambda task_id: False)

    first = downloader_worker._begin_downloading("task-1")
    second = downloader_worker._begin_downloading("task-1")

    assert first["task_id"] == "task-1"
    assert second is None
    assert task.status == DownloadStatus.DOWNLOADING.value


def test_empty_idempotency_key_is_rejected_before_redis():
    result = execute_repull_plan(run_id="run-1", idempotency_key="")

    assert result.success is False
    assert result.error == "idempotency_key is required"


def test_unverified_task_finishes_archive_state():
    task = SimpleNamespace(status=None, last_error=None, checked_at=None)
    archive = SimpleNamespace(
        status=ArchiveStatus.ARCHIVING.value,
        archived_image_count=0,
        checked=False,
        last_error=None,
    )

    checker_worker._mark_unverified(task, archive, 7)

    assert task.status == DownloadStatus.UNVERIFIED.value
    assert archive.status == ArchiveStatus.UNVERIFIED.value
    assert archive.checked is True
