import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent import context
from app.agent.nodes import loop


def _call(name="compute_integrity", args=None):
    return {"name": name, "args": args or {
        "expected": 5, "local_unique_sop": 2, "level": "study",
    }}


def test_pending_call_creates_standard_ai_tool_request():
    patch = loop._pending_with_message([_call()])

    request = patch["messages"][0]
    assert isinstance(request, AIMessage)
    assert request.tool_calls[0]["name"] == "compute_integrity"
    assert request.tool_calls[0]["id"] == patch["pending_tool_calls"][0]["tool_call_id"]


def test_reason_consumes_native_ai_message_tool_calls(monkeypatch):
    class Model:
        def invoke(self, prompt):
            return AIMessage(content="", tool_calls=[{
                "name": "compute_integrity",
                "args": {"expected": 5, "local_unique_sop": 2, "level": "study"},
                "id": "native-call-1",
                "type": "tool_call",
            }])

    monkeypatch.setattr("app.agent.llm.llm_available", lambda: True)
    monkeypatch.setattr("app.agent.llm.get_reasoning_model", lambda: Model())
    patch = loop.reason({
        "messages": [HumanMessage(content="诊断")],
        "source_id": "orthanc-local",
        "diagnostic_level": "study",
        "rule_findings": [],
        "reflection": {},
    })

    assert patch["pending_tool_calls"][0]["tool_call_id"] == "native-call-1"
    assert patch["messages"][0].tool_calls[0]["id"] == "native-call-1"


def test_act_returns_tool_message_matching_request(monkeypatch):
    monkeypatch.setattr("app.agent.audit.record_tool_evidence", lambda evidence: None)
    pending = loop._pending_with_message([_call()])
    state = {
        **pending,
        "run_id": "run-1",
        "thread_id": "thread-1",
        "tool_results": {},
        "tool_call_history": [],
        "evidence": [],
        "action_observations": [],
        "iteration": 0,
    }

    patch = loop.act(state)

    result = patch["messages"][0]
    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == pending["pending_tool_calls"][0]["tool_call_id"]
    assert result.name == "compute_integrity"
    assert json.loads(result.content)["missing"] == 3


def test_rejected_call_is_still_returned_as_tool_message(monkeypatch):
    monkeypatch.setattr("app.agent.audit.record_tool_evidence", lambda evidence: None)
    pending = loop._pending_with_message([_call("not_a_tool", {})])
    patch = loop.act({
        **pending,
        "run_id": "run-1", "thread_id": "thread-1", "tool_results": {},
        "tool_call_history": [], "evidence": [], "action_observations": [], "iteration": 0,
    })

    output = json.loads(patch["messages"][0].content)
    assert patch["messages"][0].tool_call_id == pending["pending_tool_calls"][0]["tool_call_id"]
    assert output["error_code"] == "tool_not_allowed"
    assert output["correction"]


def test_reasoning_prompt_uses_messages_not_evidence_digest():
    pending = loop._pending_with_message([_call()])
    result = loop._tool_result_message(
        pending["pending_tool_calls"][0], {"success": False, "error_code": "invalid_arguments"}
    )
    prompt = __import__("app.agent.llm", fromlist=["reasoning_prompt"]).reasoning_prompt(
        "rules", {"messages": [HumanMessage(content="diagnose"), pending["messages"][0], result],
                  "evidence": [{"output": {"secret": "must not be rendered"}}]}, "task"
    )

    assert [message.type for message in prompt] == ["system", "human", "ai", "tool", "human"]
    assert prompt[0].content == "rules"
    assert prompt[-1].content == "task"
    assert "must not be rendered" not in " ".join(str(message.content) for message in prompt)


def test_context_does_not_split_tool_request_and_result(monkeypatch):
    monkeypatch.setattr(context, "settings", type("Settings", (), {
        "agent": type("Agent", (), {
            "context_token_budget": 20, "context_summary_budget": 20, "recent_message_count": 2,
        })(),
    })())
    monkeypatch.setattr(context, "_summarize", lambda previous, older: "summary")
    pending = loop._pending_with_message([_call()])
    tool_result = loop._tool_result_message(pending["pending_tool_calls"][0], {"success": True})
    messages = [HumanMessage(content="old" * 100, id="user-old"), pending["messages"][0], tool_result]

    assert context._recent_start(messages, 1) == 1
    patch = context.manage_context({"messages": messages, "context_summary": ""})

    assert "user-old" in [message.id for message in patch["messages"]]
