"""Compatibility adapters for the current implementations.

Only this module knows how the legacy modules are named. New application code can
depend on the ports from ``src.infrastructure.ports`` and tests can inject fakes.
"""
from typing import Any

from src.infrastructure import events
from src.infrastructure.db.database import SessionLocal
from src.infrastructure.db.models import AgentRun, DownloadTask
from src.infrastructure.pacs.client import PacsClient
from src.infrastructure.rag_pipeline import search_knowledge


class LegacyPacsClientAdapter:
    def __init__(self) -> None:
        self.client = PacsClient()

    def query_target(self, source_id: str, study_instance_uid: str | None = None,
                     series_instance_uid: str | None = None) -> Any:
        from src.agents.pacs.tools import query_pacs_target
        return query_pacs_target(source_id, study_instance_uid, series_instance_uid)


class SqlAlchemyRunRepository:
    def get(self, run_id: str) -> AgentRun | None:
        with SessionLocal() as db:
            return db.query(AgentRun).filter(AgentRun.run_id == run_id).first()


class SqlAlchemyTaskRepository:
    def get(self, task_id: str) -> DownloadTask | None:
        with SessionLocal() as db:
            return db.query(DownloadTask).filter(DownloadTask.task_id == task_id).first()


class RedisEventPublisher:
    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        events.publish_event(run_id, event)


class LegacyKnowledgeStore:
    def search(self, query: str, *, limit: int = 5):
        result = search_knowledge(query, top_n=limit)
        return result.hits
