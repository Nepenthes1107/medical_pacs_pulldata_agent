"""Small Agent Registry compatible with agent-service-toolkit conventions.

The registry owns discovery only. Domain graph construction stays in ``app.agent``
until the incremental migration is complete, so this module is intentionally thin.
"""
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class AgentDefinition:
    description: str
    graph_factory: Callable[..., Any]
    stream_factory: Callable[..., Any] | None = None
    capabilities: frozenset[str] = frozenset()


@lru_cache(maxsize=1)
def _build_registry() -> dict[str, AgentDefinition]:
    from src.agents.pacs.graph import build_graph, stream_updates

    return {
        "pacs-diagnostician": AgentDefinition(
            description="基于证据、支持人工审批和 PACS 外部工具的诊断 Agent。",
            graph_factory=build_graph,
            stream_factory=stream_updates,
            capabilities=frozenset({"diagnosis", "knowledge_qa", "human_approval"}),
        )
    }


# A concrete mapping matches the toolkit's public registry contract. Construction is
# still lazy through ``_build_registry`` so importing settings or schemas does not
# initialize LangGraph until the service actually needs agent discovery.
class _LazyRegistry(dict[str, AgentDefinition]):
    def _load(self) -> dict[str, AgentDefinition]:
        return _build_registry()

    def __getitem__(self, key: str) -> AgentDefinition:
        return self._load()[key]

    def __iter__(self):
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())

    def items(self):
        return self._load().items()

    def keys(self):
        return self._load().keys()

    def values(self):
        return self._load().values()


AGENTS: dict[str, AgentDefinition] = _LazyRegistry()


def get_agent(agent_id: str = "pacs-diagnostician", *, checkpointer: Any | None = None) -> Any:
    try:
        definition = _build_registry()[agent_id]
        if checkpointer is None:
            from src.agents.pacs.graph import get_checkpointer

            checkpointer = get_checkpointer()
        return definition.graph_factory(checkpointer=checkpointer)
    except KeyError as exc:
        raise KeyError(f"unknown agent: {agent_id}") from exc


def get_all_agent_info() -> list[dict[str, Any]]:
    return [
        {
            "key": agent_id,
            "description": definition.description,
            "capabilities": sorted(definition.capabilities),
        }
        for agent_id, definition in _build_registry().items()
    ]
