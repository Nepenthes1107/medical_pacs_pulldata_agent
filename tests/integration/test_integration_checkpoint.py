"""真实 Redis Checkpointer 集成测试（spec §9：Redis checkpointer）。

验证点：
- 真实 `RedisSaver` + `setup()` 可初始化（RediSearch JSON 索引仅支持 db0，见 conftest）；
- 带 checkpointer 的最小 StateGraph：invoke → interrupt → `Command(resume=…)` 恢复；
- 用第二个全新连接/checkpointer 跨连接读回 checkpoint，证明真实落盘（非内存）。
"""
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src.infrastructure.checkpoint import create_checkpointer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis,
]


class _State(TypedDict, total=False):
    value: str
    resumed: str


def _node_value(state: _State) -> dict:
    return {"value": "a"}


def _node_gate(state: _State) -> dict:
    # 无 resume 传入时中断，返回的 __interrupt__ 携带 payload；
    # 之后 Command(resume=…) 恢复，interrupt() 原地返回 resume 值。
    answer = interrupt({"ask": "proceed?"})
    return {"resumed": answer}


def _node_finish(state: _State) -> dict:
    return {"value": state["value"] + "|" + state.get("resumed", "")}


def _build_graph(saver):
    graph = StateGraph(_State)
    graph.add_node("value", _node_value)
    graph.add_node("gate", _node_gate)
    graph.add_node("finish", _node_finish)
    graph.add_edge(START, "value")
    graph.add_edge("value", "gate")
    graph.add_edge("gate", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=saver)


def test_checkpoint_persist_interrupt_resume(redis_settings_url):
    """invoke → 中断 → resume → 最终状态；跨连接读回证明已持久化。"""
    thread_id = "it_ckpt_%s" % __import__("uuid").uuid4().hex[:8]
    config = {"configurable": {"thread_id": thread_id}}

    saver1 = create_checkpointer()  # settings.redis.url 已由 fixture 指向测试 Redis
    app1 = _build_graph(saver1)
    try:
        # 第一次：在 gate 节点中断
        first = app1.invoke({"value": ""}, config)
        assert "__interrupt__" in first, "期望 interrupt 返回 __interrupt__ 载荷"
        payload = first["__interrupt__"][0].value
        assert payload == {"ask": "proceed?"}

        # 第二次：新连接 + 新 saver（真实落盘的读侧）
        saver2 = create_checkpointer()
        app2 = _build_graph(saver2)
        state_before = app2.get_state(config)
        assert state_before.values.get("value") == "a"
        assert "resumed" not in state_before.values

        # resume 之后跑完 gate→finish
        resumed = app2.invoke(Command(resume="yes"), config)
        assert resumed.get("value") == "a|yes", resumed
        assert resumed.get("resumed") == "yes"

        # 跨连接读回最新状态
        state_after = app1.get_state(config)
        assert state_after.values.get("resumed") == "yes"
        assert state_after.values.get("value") == "a|yes"
    finally:
        try:
            saver1.delete_thread(thread_id)
        except Exception:  # noqa: BLE001
            pass


def test_checkpoint_two_threads_isolated(redis_settings_url):
    """不同 thread_id 的 checkpoints 互不串扰（thread 级隔离）。"""
    import uuid

    saver = create_checkpointer()
    try:
        configs = []
        for suffix in ("t1", "t2"):
            thread_id = "it_ckpt_%s" % uuid.uuid4().hex[:8]
            config = {"configurable": {"thread_id": thread_id}}
            configs.append((thread_id, config))
            app = _build_graph(saver)
            first = app.invoke({"value": ""}, config)
            assert "__interrupt__" in first

        # t1 resume，t2 保持中断 → 各自状态独立
        app = _build_graph(saver)
        app.invoke(Command(resume="for-t1"), configs[0][1])
        after_t1 = saver.list(configs[0][1], limit=1)
        assert next(after_t1) is not None

        # t2 仍未 resume
        state_t2 = app.get_state(configs[1][1])
        assert state_t2.values.get("value") == "a"
        assert "resumed" not in state_t2.values
    finally:
        for thread_id, _ in configs:
            try:
                saver.delete_thread(thread_id)
            except Exception:  # noqa: BLE001
                pass
