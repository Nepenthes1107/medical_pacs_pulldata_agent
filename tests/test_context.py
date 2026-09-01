from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from app.agent import context


def _settings(token_budget=80, summary_budget=30, recent_count=2):
    return SimpleNamespace(agent=SimpleNamespace(
        context_token_budget=token_budget,
        context_summary_budget=summary_budget,
        recent_message_count=recent_count,
    ))


def test_context_keeps_recent_raw_messages_and_summarizes_older(monkeypatch):
    monkeypatch.setattr(context, "settings", _settings())
    monkeypatch.setattr(context, "_summarize", lambda previous, older: "压缩后的历史摘要")
    messages = [
        HumanMessage(content="旧消息" * 80, id="m1"),
        AIMessage(content="旧回复" * 80, id="m2"),
        HumanMessage(content="最近问题", id="m3"),
        AIMessage(content="最近回答", id="m4"),
    ]

    patch = context.manage_context({"messages": messages, "context_summary": ""})

    assert patch["context_summary"] == "压缩后的历史摘要"
    assert [m.id for m in patch["messages"] if isinstance(m, RemoveMessage)] == ["m1", "m2"]
    assert "最近问题" in patch["conversation_context"]
    assert "最近回答" in patch["conversation_context"]
    assert "旧消息" not in patch["conversation_context"]


def test_sliding_window_is_only_used_after_summary(monkeypatch):
    monkeypatch.setattr(context, "settings", _settings(token_budget=20, recent_count=3))
    monkeypatch.setattr(context, "_summarize", lambda previous, older: "摘要" * 20)
    messages = [HumanMessage(content=str(i) * 100, id="m%s" % i) for i in range(5)]

    patch = context.manage_context({"messages": messages, "context_summary": ""})

    removed = [m.id for m in patch["messages"] if isinstance(m, RemoveMessage)]
    assert removed[:2] == ["m0", "m1"]  # older messages are summarized first
    assert len(removed) >= 3  # still over budget, so the raw window then slides


def test_capture_response_appends_assistant_message():
    patch = context.capture_response({"diagnosis": {"summary": "最终诊断"}})
    assert isinstance(patch["messages"][0], AIMessage)
    assert patch["messages"][0].content == "最终诊断"
