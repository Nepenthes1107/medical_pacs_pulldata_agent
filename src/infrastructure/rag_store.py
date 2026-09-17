"""Chroma knowledge-store adapter."""
import os
from functools import lru_cache
from typing import Any

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

from src.core.settings import resolve_project_path, settings
from src.infrastructure.rag_embeddings import get_embeddings

COLLECTION_NAME = "pacs_knowledge"
HNSW_CONFIGURATION = {
    "space": "cosine",
    "ef_construction": 100,
    "ef_search": 100,
    "max_neighbors": 16,
}


class DashScopeEmbeddingFunction(EmbeddingFunction):
    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002
        texts = list(input)
        embedding = get_embeddings()
        result = []
        for start in range(0, len(texts), 10):
            result.extend(embedding.embed_documents(texts[start:start + 10]))
        return result

    def embed_query(self, input) -> Embeddings:  # noqa: A002
        texts = [input] if isinstance(input, str) else list(input)
        return self(texts)

    @staticmethod
    def name() -> str:
        return "dashscope-text-embedding-v4"


def _client():
    import chromadb

    persist_dir = resolve_project_path(settings.rag.persist_dir)
    os.makedirs(persist_dir, exist_ok=True)
    return chromadb.PersistentClient(path=persist_dir)


@lru_cache
def get_collection():
    return _client().get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=DashScopeEmbeddingFunction(),
        configuration={"hnsw": HNSW_CONFIGURATION},
    )


def reset_collection():
    client = _client()
    if any(collection.name == COLLECTION_NAME for collection in client.list_collections()):
        client.delete_collection(COLLECTION_NAME)
    get_collection.cache_clear()
    return get_collection()


def _normalize_metadata(metadata: dict) -> dict:
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        if value is None:
            result[key] = ""
        elif isinstance(value, (list, tuple)):
            result[key] = ",".join(str(item) for item in value)
        elif isinstance(value, (str, int, float, bool)):
            result[key] = value
        else:
            raise TypeError("unsupported Chroma metadata type for %s" % key)
    return result


def add_atoms(atoms: list[dict]) -> int:
    if not atoms:
        return 0
    collection = get_collection()
    for start in range(0, len(atoms), 64):
        batch = atoms[start:start + 64]
        collection.upsert(
            ids=[item["id"] for item in batch],
            documents=[item["content"] for item in batch],
            metadatas=[_normalize_metadata(item["metadata"]) for item in batch],
        )
    return len(atoms)


def delete_ids(ids: list[str]) -> int:
    if not ids:
        return 0
    collection = get_collection()
    for start in range(0, len(ids), 256):
        collection.delete(ids=ids[start:start + 256])
    return len(ids)


def list_atoms() -> list[dict]:
    collection = get_collection()
    if not collection.count():
        return []
    result = collection.get(include=["documents", "metadatas"])
    return [
        {"id": result["ids"][index], "content": result["documents"][index],
         "metadata": result["metadatas"][index]}
        for index in range(len(result["ids"]))
    ]


def query(text: str, k: int = 10, where: dict | None = None) -> list[dict]:
    collection = get_collection()
    collection_size = collection.count()
    if not collection_size:
        return []
    kwargs = {"query_texts": [text], "n_results": min(k, collection_size)}
    if where:
        kwargs["where"] = where
    result = collection.query(**kwargs)
    documents = result["documents"][0]
    metadatas = result["metadatas"][0]
    ids = result["ids"][0]
    distances = result["distances"][0]
    return [
        {"id": ids[index], "content": documents[index], "metadata": metadatas[index],
         "distance": distances[index]}
        for index in range(len(documents))
    ]


def count() -> int:
    return get_collection().count()


# Compatibility name retained for the old Chroma embedding class.
_DashScopeEmbeddingFunction = DashScopeEmbeddingFunction
