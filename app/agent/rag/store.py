"""Compatibility exports for the infrastructure Chroma adapter."""
from src.infrastructure import rag_store as _canonical
from src.infrastructure.rag_store import (
    COLLECTION_NAME,
    HNSW_CONFIGURATION,
    DashScopeEmbeddingFunction,
    _DashScopeEmbeddingFunction,
    add_atoms,
    count,
    delete_ids,
    list_atoms,
    query,
    reset_collection,
)

_canonical_client = _canonical._client


def get_collection():
    _canonical._client = _client
    return _canonical.get_collection()


def _client():
    return _canonical_client()


get_collection.cache_clear = _canonical.get_collection.cache_clear

__all__ = ["COLLECTION_NAME", "HNSW_CONFIGURATION", "DashScopeEmbeddingFunction",
           "_DashScopeEmbeddingFunction", "add_atoms", "count", "delete_ids",
           "get_collection", "list_atoms", "query", "reset_collection"]
