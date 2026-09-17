# 实现已迁至 src.agents.pacs（app.agent.graph 为兼容 facade，不 re-export 下划线私有 helper）
from src.agents.pacs import graph


def test_initial_state_uses_thread_id_and_appends_user_message():
    state = graph._initial_state(
        "继续上次诊断", None, None, None, "orthanc-local", "run-2", "thread-1", 8, None
    )
    assert state["run_id"] == "run-2"
    assert state["thread_id"] == "thread-1"
    assert state["messages"][0].content == "继续上次诊断"
    assert "task_id" not in state  # 不用 None 覆盖 checkpoint 中的上一轮目标


def test_explicit_new_target_replaces_previous_target_scope():
    state = graph._initial_state(
        "诊断新任务", "task-new", None, None, "orthanc-local", "run-3", "thread-1", 8, None
    )
    assert state["task_id"] == "task-new"
    assert state["study_instance_uid"] is None
    assert state["series_instance_uid"] is None


def test_redis_checkpointer_uses_configured_ttl(monkeypatch):
    calls = {}

    class Saver:
        def setup(self):
            calls["setup"] = True

    class RedisSaver:
        @classmethod
        def from_conn_string(cls, url, ttl):
            calls.update(url=url, ttl=ttl)
            return Saver()

    monkeypatch.setattr(graph.settings.redis, "url", "redis://redis:6379/0")
    monkeypatch.setattr(graph.settings.agent, "checkpoint_ttl_minutes", 90)
    monkeypatch.setitem(__import__("sys").modules, "langgraph.checkpoint.redis",
                        type("Module", (), {"RedisSaver": RedisSaver}))

    saver = graph._default_checkpointer()

    assert isinstance(saver, Saver)
    assert calls == {
        "url": "redis://redis:6379/0",
        "ttl": {"default_ttl": 90, "refresh_on_read": True},
        "setup": True,
    }


def test_chat_request_accepts_and_validates_study_list():
    from app.core.schemas import ChatRequest

    request = ChatRequest(message="诊断", study_instance_uid_list=[" 1.2.3 ", "1.2.4"])
    assert request.study_instance_uid_list == ["1.2.3", "1.2.4"]


def test_chat_request_rejects_duplicate_study_list():
    import pytest

    from app.core.schemas import ChatRequest

    with pytest.raises(ValueError):
        ChatRequest(message="诊断", study_instance_uid_list=["1.2.3", "1.2.3"])
