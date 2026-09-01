from langchain_core.messages import AIMessage, HumanMessage

from app.agent.nodes import routing


def test_knowledge_qa_passes_minimal_state_context_to_rewrite(monkeypatch):
    observed = {}

    class Result:
        hits = []

        def model_dump(self):
            return {"rewrite_used": True}

    def fake_search(query, **kwargs):
        observed["query"] = query
        observed.update(kwargs)
        return Result()

    monkeypatch.setattr(routing, "search_knowledge", fake_search)
    state = {
        "use_rag": True,
        "message": "这个为什么失败？",
        "messages": [
            HumanMessage(content="请看 task T-123"),
            AIMessage(content="收到"),
            HumanMessage(content="这个为什么失败？"),
        ],
        "context_summary": "上一轮讨论补拉异常",
        "task_id": "T-123",
        "study_instance_uid": "1.2.840.1",
        "series_instance_uid": "1.2.840.2",
    }

    routing.retrieve_and_answer(state)

    assert observed["query"] == "这个为什么失败？"
    assert observed["recent_user_messages"] == ["请看 task T-123", "这个为什么失败？"]
    assert observed["context"] == "上一轮讨论补拉异常"
    assert observed["task_id"] == "T-123"
    assert observed["study_instance_uid"] == "1.2.840.1"
    assert observed["series_instance_uid"] == "1.2.840.2"
