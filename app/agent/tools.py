"""Compatibility facade for PACS tools.

The implementation lives in ``src.agents.pacs.tools``. The facade mirrors the
legacy module globals so existing tests and integrations can still patch the
request-scoped session boundary during migration.
"""
from src.agents.pacs import tools as _tools
from src.agents.pacs.tools import *  # noqa: F403
from src.infrastructure import pacs_tools as _implementation
from src.infrastructure.pacs_tools import search_knowledge_tool as _search_knowledge_tool
from src.infrastructure.rag_pipeline import search_knowledge as _canonical_search_knowledge

# During migration the legacy callable remains the patchable compatibility
# identity used by older integrations; its implementation delegates to src.
search_knowledge = _canonical_search_knowledge
_search_knowledge_tool.func = search_knowledge

session_scope = _tools.session_scope


def query_task_context(*args, **kwargs):
    _tools.session_scope = session_scope
    _implementation.session_scope = session_scope
    return _tools.query_task_context(*args, **kwargs)


def query_task_history(*args, **kwargs):
    _tools.session_scope = session_scope
    _implementation.session_scope = session_scope
    return _tools.query_task_history(*args, **kwargs)
