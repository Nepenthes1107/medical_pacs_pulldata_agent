import os
from pathlib import Path


def test_domain_package_has_no_transport_or_storage_client_imports():
    root = Path(__file__).parents[1] / "src" / "agents" / "pacs"
    forbidden = ("fastapi", "pika", "redis", "sqlalchemy", "chromadb")
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(
            f"import {name}" in text or f"from {name}" in text for name in forbidden
        ), path


def test_platform_has_one_registered_agent_and_one_llm_factory():
    from src.agents.registry import get_all_agent_info
    from src.core import llm

    assert [item["key"] for item in get_all_agent_info()] == ["pacs-diagnostician"]
    assert callable(llm.get_chat_model)


def test_settings_resolve_paths_and_keep_secret_out_of_yaml(tmp_path, monkeypatch):
    config = tmp_path / "settings.yml"
    config.write_text(
        """app:\n  name: test\ndatabase:\n  url: sqlite://\nredis:\n  url: redis://\nrabbitmq:\n  url: amqp://\nstorage:\n  fileserver_root: data/files\nstorescp: {}\npacs_sources: []\nchecker: {}\ndownloader: {}\n""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret-from-env")
    from src.core.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings(str(config))
    assert os.path.isabs(settings.storage.fileserver_root)
    assert settings.llm.api_key == "secret-from-env"
    get_settings.cache_clear()


def test_pacs_nodes_do_not_own_database_transactions():
    root = Path(__file__).parents[1] / "src" / "agents" / "pacs" / "nodes"
    forbidden = ("session_scope", "SessionLocal", "sqlalchemy", "app.core.models")
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), path


def test_pacs_api_schema_has_one_canonical_definition():
    from app.core.schemas import ChatRequest as LegacyChatRequest
    from src.schema.pacs import ChatRequest

    assert LegacyChatRequest is ChatRequest


def test_rag_implementation_has_one_canonical_callable():
    from app.agent.rag.pipeline import search_knowledge as legacy_search
    from src.infrastructure.rag_lexical import query as lexical_query
    from src.infrastructure.rag_pipeline import search_knowledge
    from src.infrastructure.rag_store import query as dense_query

    assert legacy_search is search_knowledge
    assert callable(dense_query)
    assert callable(lexical_query)
