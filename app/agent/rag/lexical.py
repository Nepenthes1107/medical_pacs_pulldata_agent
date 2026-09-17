"""Compatibility exports for the infrastructure BM25 adapter."""
from src.core.settings import settings
from src.infrastructure.rag_lexical import (
    _query,
    build_index,
    load_index,
    query,
    tokenize_text,
)

__all__ = ["tokenize_text", "build_index", "load_index", "query", "_query", "settings"]
