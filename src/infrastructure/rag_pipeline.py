"""唯一公开的在线 RAG Pipeline：预处理、混合召回、RRF 与重排。"""
import re
import unicodedata

import dashscope

from src.agents.pacs.schemas import KnowledgeItem, SearchKnowledgeInput, SearchKnowledgeOutput
from src.core.settings import settings

_ALIASES = (
    (re.compile(r"\bC[ _-]?MOVE\b", re.I), "C-MOVE"),
    (re.compile(r"\bC[ _-]?FIND\b", re.I), "C-FIND"),
    (re.compile(r"\bC[ _-]?GET\b", re.I), "C-GET"),
    (re.compile(r"\bC[ _-]?STORE\b", re.I), "C-STORE"),
    (re.compile(r"\bC[ _-]?ECHO\b", re.I), "C-ECHO"),
    (re.compile(r"\bAE[ _-]?TITLE\b", re.I), "AE Title"),
)


def _normalize_query(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, replacement in _ALIASES:
        text = pattern.sub(replacement, text)
    return text


def _message_text(message) -> str:
    content = message.content
    if not isinstance(content, str):
        raise TypeError("query rewrite response content must be text")
    return content


def _rewrite_query(
    query: str,
    context: str = "",
    recent_user_messages: list[str] | None = None,
    task_id: str | None = None,
    study_instance_uid: str | None = None,
    series_instance_uid: str | None = None,
) -> tuple[str, bool]:
    """用最小会话子集重写检索问题，不查询或验证请求携带的标识。"""
    # 延迟导入只为避免 llm -> tools -> pipeline 的模块初始化环。
    from src.agents.pacs.prompts import get_system_prompt, xml_blocks
    from src.core.llm import get_chat_model, stable_prompt

    body = xml_blocks([
        ("current_user_query", query),
        ("recent_user_messages", (recent_user_messages or [])[-3:]),
        ("request_identifiers", {
            "task_id": task_id,
            "study_instance_uid": study_instance_uid,
            "series_instance_uid": series_instance_uid,
        }),
        ("conversation_summary", context),
    ])
    rewritten = _normalize_query(_message_text(get_chat_model().invoke(
        stable_prompt(get_system_prompt("query_rewrite"), body)
    )))
    if not rewritten or len(rewritten) > 500:
        raise ValueError("rewritten query must contain 1..500 characters")
    return rewritten, rewritten != query


def _dense_retrieve(query: str, category: str | None, k: int) -> list[dict]:
    from src.infrastructure import rag_store as store

    rows = store.query(query, k=k, where={"category": category} if category else None)
    return [
        {
            "id": row["id"],
            "content": row["content"],
            "metadata": row["metadata"],
            "vector_distance": (
                float(row["distance"]) if row["distance"] is not None else None
            ),
        }
        for row in rows
    ]


def _bm25_retrieve(query: str, category: str | None, k: int) -> list[dict]:
    from src.infrastructure import rag_lexical as lexical

    return lexical._query(query, k=k, category=category)


def _rrf_fuse(dense: list[dict], lexical: list[dict], k: int) -> list[dict]:
    """按 chunk ID 去重；只融合名次，不混合两种原始分数。"""
    constant = settings.rag.rrf_constant
    fused: dict[str, dict] = {}
    for source_name, rows in (("dense", dense), ("bm25", lexical)):
        for rank, row in enumerate(rows, start=1):
            chunk_id = str(row["id"])
            if not chunk_id:
                raise ValueError("retrieval result has no chunk id")
            item = fused.setdefault(chunk_id, {
                "id": chunk_id,
                "content": row["content"],
                "metadata": row["metadata"],
                "vector_distance": None,
                "bm25_score": None,
                "rrf_score": 0.0,
                "best_rank": rank,
            })
            item["rrf_score"] += 1.0 / (constant + rank)
            item["best_rank"] = min(item["best_rank"], rank)
            if source_name == "dense":
                item["vector_distance"] = row["vector_distance"]
            else:
                item["bm25_score"] = row["bm25_score"]
    return sorted(
        fused.values(),
        key=lambda item: (-item["rrf_score"], item["best_rank"], item["id"]),
    )[:k]


def _rerank(
    query: str,
    candidates: list[dict],
    top_n: int,
) -> list[dict]:
    if not candidates:
        return []
    if not settings.llm.api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required for rerank")
    response = dashscope.TextReRank.call(
        model=settings.rag.rerank_model,
        query=query,
        documents=[item["content"] for item in candidates],
        top_n=min(top_n, len(candidates)),
        return_documents=False,
        api_key=settings.llm.api_key,
    )
    if int(response.status_code) != 200:
        raise RuntimeError("rerank request status=%s" % response.status_code)
    results = response.output["results"]
    if not results:
        raise ValueError("rerank returned no results")
    reranked = []
    for result in results:
        index = int(result["index"])
        if not 0 <= index < len(candidates):
            raise ValueError("rerank returned invalid document index")
        item = dict(candidates[index])
        item["rerank_score"] = float(result["relevance_score"])
        reranked.append(item)
    return reranked[:top_n]


def _optional_int(value) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _knowledge_item(row: dict) -> KnowledgeItem:
    metadata = row["metadata"]
    return KnowledgeItem(
        chunk_id=str(row["id"]),
        content=row["content"],
        category=metadata.get("category", "uncategorized"),
        source=metadata["source_id"],
        title=metadata["title"],
        version=str(metadata["version"]),
        section=metadata.get("section") or None,
        page=_optional_int(metadata.get("page_start")),
        url=metadata["source_url"] or None,
        vector_distance=row.get("vector_distance"),
        bm25_score=row.get("bm25_score"),
        rrf_score=row.get("rrf_score"),
        rerank_score=row.get("rerank_score"),
    )


def search_knowledge(
    query: str,
    context: str = "",
    recent_user_messages: list[str] | None = None,
    task_id: str | None = None,
    study_instance_uid: str | None = None,
    series_instance_uid: str | None = None,
    category: str | None = None,
    top_n: int | None = None,
) -> SearchKnowledgeOutput:
    """执行完整 RAG Pipeline；任一启用阶段失败时直接抛出异常。"""
    request = SearchKnowledgeInput(
        query=query,
        context=context,
        recent_user_messages=recent_user_messages or [],
        task_id=task_id,
        study_instance_uid=study_instance_uid,
        series_instance_uid=series_instance_uid,
        category=category,
        top_n=top_n,
    )
    original_query = _normalize_query(request.query)
    effective_query, rewrite_used = _rewrite_query(
        original_query,
        request.context,
        request.recent_user_messages,
        request.task_id,
        request.study_instance_uid,
        request.series_instance_uid,
    )
    requested_top_n = request.top_n or settings.rag.top_k
    dense_rows = _dense_retrieve(effective_query, request.category, settings.rag.candidate_k)
    bm25_rows = _bm25_retrieve(effective_query, request.category, settings.rag.candidate_k)
    fused = _rrf_fuse(dense_rows, bm25_rows, settings.rag.candidate_k)
    final_rows = _rerank(effective_query, fused, requested_top_n)
    hits = [_knowledge_item(row) for row in final_rows]
    return SearchKnowledgeOutput(
        success=True,
        result_status="ok" if hits else "empty",
        original_query=original_query,
        effective_query=effective_query,
        rewrite_used=rewrite_used,
        hits=hits,
    )
