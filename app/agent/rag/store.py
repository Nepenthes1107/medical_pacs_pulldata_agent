"""ChromaDB 向量库封装（spec 11.1/11.2）。

单文件持久化，零运维。写入时附 metadata（category/stage/dicom_operation/symptom），
检索时用 where 做元数据过滤 → 向量召回 Top-K。
Embedding 通过 DashScope；无 key 时 store 仍可导入，实际 add/query 才触发 key 校验。
"""
import os
from functools import lru_cache
from typing import Dict, List, Optional

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

from app.agent.rag.embeddings import get_embeddings
from app.core.config import resolve_project_path, settings

COLLECTION_NAME = "pacs_knowledge"
HNSW_CONFIGURATION = {
    "space": "cosine",
    "ef_construction": 100,
    "ef_search": 100,
    "max_neighbors": 16,
}


class _DashScopeEmbeddingFunction(EmbeddingFunction):
    """把 LangChain OpenAIEmbeddings 适配为 ChromaDB EmbeddingFunction。

    chromadb 1.x 用 __call__ 编码文档、embed_query 编码查询；两者都要实现。
    """

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 (chroma 接口签名)
        texts = list(input)
        emb = get_embeddings()
        result = []
        # DashScope text-embedding-v4 单次请求最多 10 条，超限返回 400；按 10 分批兜底。
        for start in range(0, len(texts), 10):
            result.extend(emb.embed_documents(texts[start:start + 10]))
        return result

    def embed_query(self, input) -> Embeddings:  # noqa: A002
        texts = [input] if isinstance(input, str) else list(input)
        return self.__call__(texts)

    @staticmethod
    def name() -> str:
        return "dashscope-text-embedding-v4"


def _client():
    import chromadb

    persist_dir = resolve_project_path(settings.rag.persist_dir)
    os.makedirs(persist_dir, exist_ok=True)
    return chromadb.PersistentClient(path=persist_dir)


@lru_cache()
def get_collection():
    """获取/创建知识库 collection（余弦相似度）。"""
    client = _client()
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=_DashScopeEmbeddingFunction(),
        configuration={"hnsw": HNSW_CONFIGURATION},
    )


def reset_collection():
    """重建 collection（初始化脚本用）。"""
    client = _client()
    if any(collection.name == COLLECTION_NAME for collection in client.list_collections()):
        client.delete_collection(COLLECTION_NAME)
    get_collection.cache_clear()
    return get_collection()


def add_atoms(atoms: List[Dict]) -> int:
    """写入知识原子。atoms: [{"id","content","metadata"}]。返回写入条数。"""
    if not atoms:
        return 0
    collection = get_collection()
    # 官方文档切分后可能有数千 chunks，分批避免单次 embedding/Chroma payload 过大。
    for start in range(0, len(atoms), 64):
        batch = atoms[start:start + 64]
        collection.upsert(
            ids=[a["id"] for a in batch],
            documents=[a["content"] for a in batch],
            metadatas=[_normalize_metadata(a["metadata"]) for a in batch],
        )
    return len(atoms)


def delete_ids(ids: List[str]) -> int:
    """删除已移除或已变更文档的旧 chunks。"""
    if not ids:
        return 0
    collection = get_collection()
    for start in range(0, len(ids), 256):
        collection.delete(ids=ids[start:start + 256])
    return len(ids)


def list_atoms() -> List[Dict]:
    """读取当前 Chroma corpus，供增量快照和 BM25 重建共用。"""
    collection = get_collection()
    total = collection.count()
    if not total:
        return []
    result = collection.get(include=["documents", "metadatas"])
    return [
        {"id": result["ids"][index],
         "content": result["documents"][index],
         "metadata": result["metadatas"][index]}
        for index in range(len(result["ids"]))
    ]


def _normalize_metadata(md: Dict) -> Dict:
    """ChromaDB metadata 只接受标量值；None 转空串，list 转逗号串。"""
    out = {}
    for k, v in md.items():
        if v is None:
            out[k] = ""
        elif isinstance(v, (list, tuple)):
            out[k] = ",".join(str(x) for x in v)
        elif isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            raise TypeError("unsupported Chroma metadata type for %s" % k)
    return out


def query(
    text: str,
    k: int = 10,
    where: Optional[Dict] = None,
) -> List[Dict]:
    """元数据过滤 + 向量召回 Top-K。返回 [{"id","content","metadata","distance"}]。"""
    collection = get_collection()
    collection_size = collection.count()
    if collection_size == 0:
        return []
    kwargs = {"query_texts": [text], "n_results": min(k, collection_size)}
    if where:
        kwargs["where"] = where
    res = collection.query(**kwargs)
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    ids = res["ids"][0]
    dists = res["distances"][0]
    return [
        {"id": ids[i], "content": docs[i], "metadata": metas[i], "distance": dists[i]}
        for i in range(len(docs))
    ]


def count() -> int:
    return get_collection().count()
