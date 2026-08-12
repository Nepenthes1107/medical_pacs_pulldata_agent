"""检索评估（spec 11.5）：Recall@3、MRR@5、延迟、Token。

固定问题集 → 对每个 query 检索，比对期望命中的知识 id（或 category）。
对照三档：仅向量召回 vs 召回+重排（无 RAG 作为基线仅记录“不检索”）。
需要真实 embedding key 才能跑；无 key 时脚本报错退出（评估本身需要向量）。

用法：python -m app.agent.rag.evaluate
"""
import json
import logging
import os
import time
from typing import Dict, List

from app.agent.rag import retriever

logger = logging.getLogger(__name__)

EVAL_SET_PATH = os.path.join(os.path.dirname(__file__), "eval_set.json")


def load_eval_set() -> List[Dict]:
    if not os.path.exists(EVAL_SET_PATH):
        return []
    with open(EVAL_SET_PATH, "r", encoding="utf-8") as fp:
        return json.load(fp)


def _hit_ids(items: List[Dict], relevant: Dict) -> List[int]:
    """返回命中位置（1-based）列表。relevant 支持按 id 或 category 命中。"""
    rel_ids = set(relevant.get("ids", []))
    rel_cats = set(relevant.get("categories", []))
    hits = []
    for rank, item in enumerate(items, start=1):
        md = item.get("metadata", {})
        if item["id"] in rel_ids or md.get("category") in rel_cats:
            hits.append(rank)
    return hits


def evaluate(enable_rerank: bool = False, k: int = 10) -> Dict:
    """跑固定问题集，返回 Recall@3 / MRR@5 / 平均延迟。"""
    eval_set = load_eval_set()
    if not eval_set:
        return {"error": "eval_set 为空"}

    recall_at_3 = 0.0
    mrr_at_5 = 0.0
    latencies = []

    for q in eval_set:
        start = time.time()
        items = retriever.retrieve(
            q["query"], where=q.get("where"), k=k, top_n=k, enable_rerank=enable_rerank
        )
        latencies.append((time.time() - start) * 1000)
        hits = _hit_ids(items, q.get("relevant", {}))
        # Recall@3：前 3 条中是否有命中。
        if any(h <= 3 for h in hits):
            recall_at_3 += 1
        # MRR@5：前 5 条中第一个命中的倒数排名。
        first_hit_5 = next((h for h in hits if h <= 5), None)
        if first_hit_5:
            mrr_at_5 += 1.0 / first_hit_5

    n = len(eval_set)
    return {
        "n_queries": n,
        "enable_rerank": enable_rerank,
        "recall_at_3": round(recall_at_3 / n, 4),
        "mrr_at_5": round(mrr_at_5 / n, 4),
        "avg_latency_ms": round(sum(latencies) / n, 2),
    }


def compare() -> Dict:
    """对照：仅向量召回 vs 召回+重排。据结果决定是否默认启用 rerank。"""
    vector_only = evaluate(enable_rerank=False)
    with_rerank = evaluate(enable_rerank=True)
    recommendation = "rerank" if (
        with_rerank.get("recall_at_3", 0) > vector_only.get("recall_at_3", 0)
    ) else "vector_only"
    return {
        "vector_only": vector_only,
        "with_rerank": with_rerank,
        "recommendation": recommendation,
        "note": "仅当 rerank 提升 Recall@3 且额外延迟可接受时才默认启用（spec 11.5）",
    }


def main():
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(compare(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
