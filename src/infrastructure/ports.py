"""Dependency inversion ports for the PACS application layer.

These protocols are intentionally small. Existing ``app`` implementations are
adapted in later phases; defining the ports now prevents new graph nodes from
coupling directly to SQLAlchemy, RabbitMQ, Redis, or Chroma.
"""
from collections.abc import Sequence
from typing import Any, Protocol


class PacsClientPort(Protocol):
    def query_target(self, source_id: str, study_instance_uid: str | None = None,
                     series_instance_uid: str | None = None) -> Any: ...

    def move_study(self, source_id: str, study_instance_uid: str, **kwargs: Any) -> Any: ...


class RunRepositoryPort(Protocol):
    def get(self, run_id: str) -> Any: ...
    def save(self, run: Any) -> None: ...


class TaskRepositoryPort(Protocol):
    def get(self, task_id: str) -> Any: ...
    def save(self, task: Any) -> None: ...


class EventPublisherPort(Protocol):
    def publish(self, run_id: str, event: dict[str, Any]) -> None: ...


class KnowledgeStorePort(Protocol):
    def search(self, query: str, *, limit: int = 5) -> Sequence[Any]: ...
