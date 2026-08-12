"""ChromaDB 向量库封装（spec 11.1/11.2）。

单文件持久化，零运维。写入时附 metadata（category/stage/dicom_operation/symptom），
检索时用 where 做元数据过滤 → 向量召回 Top-K。
Embedding 通过 DashScope；无 key 时 store 仍可导入，实际 add/query 才触发 key 校验。
"""
import logging
import os
from functools import lru_cache
from typing import Dict, List, Optional

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

from app.agent.rag.embeddings import get_embeddings
from app.core.config import resolve_project_path, settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "pacs_knowledge"


class _DashScopeEmbeddingFunction(EmbeddingFunction):
    """把 LangChain OpenAIEmbeddings 适配为 ChromaDB EmbeddingFunction。

    chromadb 1.x 用 __call__ 编码文档、embed_query 编码查询；两者都要实现。
    """

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 (chroma 接口签名)
        return get_embeddings().embed_documents(list(input))

    def embed_query(self, input) -> Embeddings:  # noqa: A002
        texts = [input] if isinstance(input, str) else list(input)
        return get_embeddings().embed_documents(texts)

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
        metadata={"hnsw:space": "cosine"},
    )


def reset_collection():
    """重建 collection（初始化脚本用）。"""
    client = _client()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    get_collection.cache_clear()
    return get_collection()


def add_atoms(atoms: List[Dict]) -> int:
    """写入知识原子。atoms: [{"id","content","metadata"}]。返回写入条数。"""
    if not atoms:
        return 0
    collection = get_collection()
    collection.upsert(
        ids=[a["id"] for a in atoms],
        documents=[a["content"] for a in atoms],
        metadatas=[_normalize_metadata(a.get("metadata", {})) for a in atoms],
    )
    return len(atoms)


def _normalize_metadata(md: Dict) -> Dict:
    """ChromaDB metadata 只接受标量值；None 转空串，list 转逗号串。"""
    out = {}
    for k, v in (md or {}).items():
        if v is None:
            out[k] = ""
        elif isinstance(v, (list, tuple)):
            out[k] = ",".join(str(x) for x in v)
        elif isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def query(
    text: str,
    k: int = 10,
    where: Optional[Dict] = None,
) -> List[Dict]:
    """元数据过滤 + 向量召回 Top-K。返回 [{"id","content","metadata","distance"}]。"""
    collection = get_collection()
    kwargs = {"query_texts": [text], "n_results": k}
    if where:
        kwargs["where"] = where
    res = collection.query(**kwargs)
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    ids = res.get("ids", [[]])[0]
    dists = res.get("distances", [[]])[0]
    return [
        {"id": ids[i], "content": docs[i], "metadata": metas[i], "distance": dists[i]}
        for i in range(len(docs))
    ]


def count() -> int:
    return get_collection().count()
