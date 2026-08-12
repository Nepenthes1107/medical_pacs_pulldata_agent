"""检索管道（spec 11.2）：元数据过滤 → 向量召回 Top-K → 可选重排 → Top-N 注入。

可选重排默认关闭（settings.rag.enable_rerank），是否启用由离线评估（spec 11.5）决定。
retrieved_knowledge 最多 3 条（spec 7 截断约束）。
"""
import logging
from typing import Dict, List, Optional

from app.agent.rag import store
from app.core.config import settings

logger = logging.getLogger(__name__)


def _rerank(query_text: str, candidates: List[Dict], top_n: int) -> List[Dict]:
    """可选重排（qwen3-rerank）。失败或候选过少时退回原序，绝不阻断主流程。"""
    if not candidates:
        return []
    # 候选很少或 Top-3 已足够时直接返回，避免额外模型调用（spec 11.2）。
    if len(candidates) <= top_n:
        return candidates[:top_n]
    try:
        import dashscope  # 仅在启用重排时才需要

        docs = [c["content"] for c in candidates]
        resp = dashscope.TextReRank.call(
            model=settings.rag.rerank_model,
            query=query_text,
            documents=docs,
            top_n=top_n,
            api_key=settings.llm.api_key,
        )
        order = [item["index"] for item in resp.output["results"]]
        return [candidates[i] for i in order][:top_n]
    except Exception as exc:  # noqa: BLE001
        logger.warning("rerank 失败，退回向量召回顺序: %s", exc)
        return candidates[:top_n]


def retrieve(
    query_text: str,
    where: Optional[Dict] = None,
    k: int = 10,
    top_n: Optional[int] = None,
    enable_rerank: Optional[bool] = None,
) -> List[Dict]:
    """完整检索管道。返回最终注入的知识条目（≤top_n）。"""
    top_n = top_n if top_n is not None else settings.rag.top_k
    use_rerank = settings.rag.enable_rerank if enable_rerank is None else enable_rerank

    candidates = store.query(query_text, k=k, where=where)
    if use_rerank:
        return _rerank(query_text, candidates, top_n)
    return candidates[:top_n]


def retrieve_texts(query_text: str, where: Optional[Dict] = None, top_n: Optional[int] = None) -> List[str]:
    """检索并返回文本列表（每条截断到 300 字，spec 7）。"""
    items = retrieve(query_text, where=where, top_n=top_n)
    return [item["content"][:300] for item in items]


def build_query_text(message: str, tool_results: Dict) -> str:
    """把用户 question + 工具返回摘要拼为查询文本（spec 11.2 向量召回输入）。"""
    parts = [message or ""]
    for name, result in (tool_results or {}).items():
        if not result:
            continue
        if result.get("success"):
            parts.append("%s: %s" % (name, result.get("dicom_status_summary") or "ok"))
        else:
            parts.append("%s error: %s" % (name, result.get("error", "")))
    return " ".join(p for p in parts if p)[:1000]
