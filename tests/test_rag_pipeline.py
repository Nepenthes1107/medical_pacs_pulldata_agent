import sys
import types

import pytest

from app.agent.rag import pipeline


def _row(chunk_id, *, category="dicom_standard", distance=None, bm25=None):
    row = {
        "id": chunk_id,
        "content": "content " + chunk_id,
        "metadata": {
            "category": category,
            "source_id": "test-source",
            "title": "DICOM",
            "version": "test",
            "source_url": "https://example.test",
            "page_start": 7,
        },
    }
    if distance is not None:
        row["vector_distance"] = distance
    if bm25 is not None:
        row["bm25_score"] = bm25
    return row


def test_normalize_preserves_exact_values_and_standardizes_aliases():
    query = pipeline._normalize_query(
        "  C_MOVE 失败 0xA801，Tag (0008,0052)，UID 1.2.840.10008.1.2  "
    )
    assert query.startswith("C-MOVE 失败 0xA801")
    assert "(0008,0052)" in query
    assert "1.2.840.10008.1.2" in query


def test_bm25_tokenizer_keeps_dicom_exact_tokens():
    pytest.importorskip("jieba")
    from app.agent.rag.lexical import tokenize_text

    tokens = tokenize_text("C-MOVE 0xA801 (0008,0052) 1.2.840.10008.1.2 中文查询")
    assert "c_move" in tokens
    assert "0xa801" in tokens
    assert "tag_0008_0052" in tokens
    assert "1_2_840_10008_1_2" in tokens
    assert "查询" in tokens


def test_first_turn_uses_rewrite_model(monkeypatch):
    import app.agent.llm as llm

    class Response:
        content = "请解释 DICOM C-MOVE 的用途"

    class Model:
        def invoke(self, _):
            return Response()

    monkeypatch.setattr(llm, "get_chat_model", lambda: Model())
    monkeypatch.setattr(pipeline, "_dense_retrieve", lambda *_: [_row("a", distance=0.1)])
    monkeypatch.setattr(pipeline, "_bm25_retrieve", lambda *_: [])
    monkeypatch.setattr(pipeline, "_rerank", lambda _query, rows, top_n: rows[:top_n])

    result = pipeline.search_knowledge("C MOVE 是什么", context="", top_n=1)

    assert result.success is True
    assert result.rewrite_used is True
    assert result.effective_query == "请解释 DICOM C-MOVE 的用途"
    assert [hit.chunk_id for hit in result.hits] == ["a"]


def test_rewrite_failure_is_not_hidden(monkeypatch):
    import app.agent.llm as llm

    class BrokenModel:
        def invoke(self, _):
            raise TimeoutError("timeout")

    monkeypatch.setattr(llm, "get_chat_model", lambda: BrokenModel())
    with pytest.raises(TimeoutError):
        pipeline._rewrite_query("C-MOVE 为什么失败", "前文摘要")


def test_context_query_is_rewritten_to_standalone_question(monkeypatch):
    import app.agent.llm as llm

    class Response:
        content = " C_FIND 的失败状态 0xA801 是什么？ "

    class Model:
        def invoke(self, _):
            return Response()

    monkeypatch.setattr(llm, "get_chat_model", lambda: Model())
    effective, used = pipeline._rewrite_query("这个错误呢？", "上文讨论 C-FIND 0xA801")
    assert effective == "C-FIND 的失败状态 0xA801 是什么?"
    assert used is True


def test_rewrite_receives_recent_messages_and_unverified_identifiers(monkeypatch):
    import app.agent.llm as llm

    observed = {}

    class Response:
        content = "任务 T-123 的 PACS 补拉失败常见原因"

    class Model:
        def invoke(self, messages):
            observed["body"] = messages[-1].content
            return Response()

    monkeypatch.setattr(llm, "get_chat_model", lambda: Model())
    effective, used = pipeline._rewrite_query(
        "这个为什么失败？",
        context="用户正在排查补拉问题",
        recent_user_messages=["先帮我看 T-123", "这个为什么失败？"],
        task_id="T-123",
        study_instance_uid="1.2.840.1",
    )

    assert used is True
    assert effective == "任务 T-123 的 PACS 补拉失败常见原因"
    assert "<recent_user_messages>" in observed["body"]
    assert "先帮我看 T-123" in observed["body"]
    assert "<request_identifiers>" in observed["body"]
    assert '"task_id": "T-123"' in observed["body"]
    assert '"study_instance_uid": "1.2.840.1"' in observed["body"]


def test_rrf_deduplicates_and_uses_rank_only(monkeypatch):
    monkeypatch.setattr(pipeline.settings.rag, "rrf_constant", 60)
    dense = [_row("a", distance=0.1), _row("b", distance=0.2)]
    bm25 = [_row("b", bm25=99.0), _row("c", bm25=1.0)]

    fused = pipeline._rrf_fuse(dense, bm25, 20)

    assert [item["id"] for item in fused] == ["b", "a", "c"]
    assert fused[0]["rrf_score"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused[0]["vector_distance"] == 0.2
    assert fused[0]["bm25_score"] == 99.0


def test_dense_failure_stops_pipeline(monkeypatch):
    def fail(*_):
        raise RuntimeError("dense unavailable")

    monkeypatch.setattr(pipeline, "_dense_retrieve", fail)
    monkeypatch.setattr(pipeline, "_rewrite_query", lambda query, *_args: (query, False))
    monkeypatch.setattr(
        pipeline, "_bm25_retrieve", lambda *_: pytest.fail("Dense 失败后不应继续 BM25"),
    )
    with pytest.raises(RuntimeError, match="dense unavailable"):
        pipeline.search_knowledge("状态码 0xA801", top_n=1)


def test_bm25_failure_stops_pipeline(monkeypatch):
    def fail(*_):
        raise RuntimeError("bm25 unavailable")

    monkeypatch.setattr(pipeline, "_dense_retrieve", lambda *_: [_row("dense", distance=0.2)])
    monkeypatch.setattr(pipeline, "_bm25_retrieve", fail)
    monkeypatch.setattr(pipeline, "_rewrite_query", lambda query, *_args: (query, False))
    with pytest.raises(RuntimeError, match="bm25 unavailable"):
        pipeline.search_knowledge("association", top_n=1)


def test_category_is_applied_to_both_retrievers(monkeypatch):
    seen = []
    monkeypatch.setattr(
        pipeline, "_dense_retrieve",
        lambda query, category, k: seen.append(("dense", category)) or [],
    )
    monkeypatch.setattr(
        pipeline, "_bm25_retrieve",
        lambda query, category, k: seen.append(("bm25", category)) or [],
    )
    monkeypatch.setattr(pipeline, "_rewrite_query", lambda query, *_args: (query, False))
    pipeline.search_knowledge("association", category="dicom_standard")

    assert seen == [("dense", "dicom_standard"), ("bm25", "dicom_standard")]


def test_rerank_failure_is_not_hidden(monkeypatch):
    class BrokenReranker:
        @staticmethod
        def call(**_kwargs):
            raise TimeoutError("timeout")

    monkeypatch.setattr(pipeline.dashscope, "TextReRank", BrokenReranker)
    monkeypatch.setattr(pipeline.settings.llm, "api_key", "test-key")
    candidates = [_row("first"), _row("second")]

    with pytest.raises(TimeoutError):
        pipeline._rerank("query", candidates, 2)


def test_rerank_saves_relative_score(monkeypatch):
    class Response:
        status_code = 200
        output = {"results": [
            {"index": 1, "relevance_score": 0.91},
            {"index": 0, "relevance_score": 0.40},
        ]}

    class Reranker:
        @staticmethod
        def call(**_kwargs):
            return Response()

    monkeypatch.setattr(pipeline.dashscope, "TextReRank", Reranker)
    monkeypatch.setattr(pipeline.settings.llm, "api_key", "test-key")

    rows = pipeline._rerank("query", [_row("a"), _row("b")], 2)

    assert [row["id"] for row in rows] == ["b", "a"]
    assert rows[0]["rerank_score"] == pytest.approx(0.91)


def test_agent_tool_and_mcp_bind_the_same_function(monkeypatch):
    from app.agent import tools
    from app.agent.mcp_server import build_mcp_server
    from app.agent.nodes import routing

    assert tools.search_knowledge is pipeline.search_knowledge
    assert tools.search_knowledge_tool.func is pipeline.search_knowledge
    assert routing.search_knowledge is pipeline.search_knowledge

    class FakeFastMCP:
        def __init__(self, _name):
            self.registered = []

        def tool(self):
            def register(function):
                self.registered.append(function)
                return function
            return register

    mcp_module = types.ModuleType("mcp")
    server_module = types.ModuleType("mcp.server")
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = FakeFastMCP
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    server = build_mcp_server()
    assert pipeline.search_knowledge in server.registered
    assert not any(fn.__name__ == "mcp_search_knowledge" for fn in server.registered)
