"""离线对比 BM25、Dense、RRF、RRF+Rerank 的 Recall@5/MRR@5/延迟。"""
import json
import os
import time

from src.core.settings import settings
from src.infrastructure.rag_pipeline import (
    _bm25_retrieve,
    _dense_retrieve,
    _rerank,
    _rrf_fuse,
)

EVAL_SET_PATH = os.path.join(os.path.dirname(__file__), "eval_set.json")
STAGES = ("bm25", "dense", "rrf", "rrf_rerank")


def load_eval_set() -> list[dict]:
    if not os.path.exists(EVAL_SET_PATH):
        return []
    with open(EVAL_SET_PATH, encoding="utf-8") as fp:
        return json.load(fp)


def _retrieve_stage(query: str, category: str | None, stage: str) -> list[dict]:
    candidate_k = settings.rag.candidate_k
    if stage == "bm25":
        return _bm25_retrieve(query, category, candidate_k)[:5]
    if stage == "dense":
        return _dense_retrieve(query, category, candidate_k)[:5]
    dense = _dense_retrieve(query, category, candidate_k)
    bm25 = _bm25_retrieve(query, category, candidate_k)
    fused = _rrf_fuse(dense, bm25, candidate_k)
    if stage == "rrf":
        return fused[:5]
    return _rerank(query, fused, 5)


def _is_relevant(item: dict, relevant: dict) -> bool:
    metadata = item.get("metadata") or {}
    return (
        item.get("id") in set(relevant.get("ids", []))
        or metadata.get("category") in set(relevant.get("categories", []))
    )


def evaluate(stage: str) -> dict:
    if stage not in STAGES:
        raise ValueError("unknown stage: %s" % stage)
    cases = load_eval_set()
    if not cases:
        raise ValueError("eval_set 为空")
    recall = mrr = 0.0
    latencies = []
    for case in cases:
        started = time.perf_counter()
        where = case.get("where") or {}
        items = _retrieve_stage(case["query"], where.get("category"), stage)
        latencies.append((time.perf_counter() - started) * 1000)
        ranks = [
            rank for rank, item in enumerate(items[:5], start=1)
            if _is_relevant(item, case.get("relevant", {}))
        ]
        if ranks:
            recall += 1.0
            mrr += 1.0 / ranks[0]
    count = len(cases)
    return {
        "stage": stage,
        "n_queries": count,
        "recall_at_5": round(recall / count, 4),
        "mrr_at_5": round(mrr / count, 4),
        "avg_latency_ms": round(sum(latencies) / count, 2),
    }


def compare() -> dict:
    return {stage: evaluate(stage) for stage in STAGES}


def main():
    print(json.dumps(compare(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
