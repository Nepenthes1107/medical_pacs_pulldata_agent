"""自主诊断主干图（plan §3 / spec §3）：reason ↔ act ↔ observe 循环 + 收束。

拓扑：
    START → route_request ─(knowledge_qa)→ retrieve_and_answer → END
                          ─(clarification)→ present_clarification → END
                          ─(diagnosis)────→ resolve_target ─(clarification)→ present_clarification
                                                            └(ok)→ reason ⇄ act → observe → reason
                                                                       │(should_continue=diagnose)
                                                                       ▼
                                                                   diagnose → END

Stage B：reason/diagnose 为规则占位，仅跑通拓扑与终止条件。
后续阶段接 reflect / plan_repull / human_approval（Stage D/E）。
"""
import logging
import os
from typing import Optional

from app.agent.nodes.approval import execute, human_approval, route_after_approval
from app.agent.nodes.first_pull import first_pull
from app.agent.nodes.loop import (
    act,
    diagnose,
    observe,
    plan_repull,
    reason,
    reflect,
    route_after_verify,
    should_continue,
    verify_diagnosis,
)
from app.agent.nodes.routing import (
    present_clarification,
    resolve_target,
    retrieve_and_answer,
    route_request,
)
from app.agent.state import DEFAULT_MAX_ITERATIONS, AgentState
from app.core.config import resolve_project_path, settings

logger = logging.getLogger(__name__)


def _route_after_request(state: AgentState) -> str:
    route = state.get("route")
    if route == "first_pull":
        return "first_pull"
    if route == "knowledge_qa":
        return "retrieve_and_answer"
    if route == "clarification":
        return "present_clarification"
    return "resolve_target"


def _route_after_resolve(state: AgentState) -> str:
    # resolve_target 可能因参数冲突/未找到把 route 改成 clarification。
    if state.get("route") == "clarification":
        return "present_clarification"
    return "reason"


def build_graph(checkpointer=None):
    """构造并编译自主诊断循环图。checkpointer 可注入（默认 SqliteSaver 单实例）。"""
    from langgraph.graph import END, START, StateGraph

    # 配置 LangSmith tracing 开关（可观测层）；不可用不影响主流程（护栏 7 旁路）。
    try:
        from app.agent.llm import configure_langsmith

        configure_langsmith()
    except Exception:  # noqa: BLE001
        pass

    graph = StateGraph(AgentState)
    graph.add_node("route_request", route_request)
    graph.add_node("resolve_target", resolve_target)
    graph.add_node("first_pull", first_pull)
    graph.add_node("present_clarification", present_clarification)
    graph.add_node("retrieve_and_answer", retrieve_and_answer)
    graph.add_node("reason", reason)
    graph.add_node("act", act)
    graph.add_node("observe", observe)
    graph.add_node("diagnose", diagnose)
    graph.add_node("verify_diagnosis", verify_diagnosis)
    graph.add_node("reflect", reflect)
    graph.add_node("plan_repull", plan_repull)
    graph.add_node("human_approval", human_approval)  # 护栏 4：interrupt 暂停等人工审批
    graph.add_node("execute", execute)  # 审批通过后执行写操作（固定节点，调图外写工具）

    graph.add_edge(START, "route_request")
    graph.add_conditional_edges(
        "route_request",
        _route_after_request,
        {
            "resolve_target": "resolve_target",
            "first_pull": "first_pull",
            "retrieve_and_answer": "retrieve_and_answer",
            "present_clarification": "present_clarification",
        },
    )
    graph.add_conditional_edges(
        "resolve_target",
        _route_after_resolve,
        {"reason": "reason", "present_clarification": "present_clarification"},
    )
    # 循环核心：reason ─(should_continue)→ act / diagnose；act → observe → reason。
    graph.add_conditional_edges(
        "reason",
        should_continue,
        {"act": "act", "diagnose": "diagnose"},
    )
    graph.add_edge("act", "observe")
    graph.add_edge("observe", "reason")

    # 终局收束：diagnose → 引用接地校验（护栏 2）→ 打回重述 or 反思（护栏 7）→ END。
    graph.add_edge("diagnose", "verify_diagnosis")
    graph.add_conditional_edges(
        "verify_diagnosis",
        route_after_verify,
        {"diagnose": "diagnose", "reflect": "reflect"},
    )
    graph.add_edge("reflect", "plan_repull")
    # 补拉计划 → 人工审批闸门（interrupt 暂停）→ approved 执行写 / 否则收尾（护栏 4）。
    graph.add_edge("plan_repull", "human_approval")
    graph.add_conditional_edges(
        "human_approval",
        route_after_approval,
        {"execute": "execute", "__end__": END},
    )
    graph.add_edge("execute", END)
    graph.add_edge("first_pull", END)  # 首拉直达出口，不进循环（plan-v2 §1.3）
    graph.add_edge("present_clarification", END)
    graph.add_edge("retrieve_and_answer", END)

    return graph.compile(checkpointer=checkpointer)


def _default_checkpointer():
    """SqliteSaver（单实例简历项目）。失败时返回 None。

    跨进程共享：API 进程创建 Run、worker 进程 interrupt 暂停、API 进程 resume，
    三处靠同一 sqlite 文件 + check_same_thread=False 共享 checkpoint 状态。
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = resolve_project_path(settings.agent.checkpoint_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import sqlite3

    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn)


_CHECKPOINTER = None


def get_checkpointer():
    """进程内 checkpointer 单例（HITL interrupt/replay 必需）。

    审批闸门用 interrupt() 暂停、Command(resume) 恢复，必须有 checkpointer 持久化暂停态。
    初始化失败不静默降级为无 checkpoint——那样 interrupt 无法恢复、审批闸门失效（护栏 4），
    调用侧应据此把 Run 标 failed 并注明，不伪装（降级不伪装纪律）。
    """
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        _CHECKPOINTER = _default_checkpointer()
    return _CHECKPOINTER


def run_agent(
    message: str = "",
    task_id: Optional[str] = None,
    study_instance_uid: Optional[str] = None,
    series_instance_uid: Optional[str] = None,
    source_id: str = "orthanc-local",
    run_id: str = "adhoc",
    checkpointer=None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    intent: Optional[str] = None,
) -> dict:
    """执行自主诊断循环，返回最终 state。"""
    app = build_graph(checkpointer=checkpointer)
    initial = _initial_state(message, task_id, study_instance_uid, series_instance_uid,
                             source_id, run_id, max_iterations, intent)
    config = {"configurable": {"thread_id": run_id}} if checkpointer else {}
    return app.invoke(initial, config=config)


def _initial_state(message, task_id, study_instance_uid, series_instance_uid,
                   source_id, run_id, max_iterations, intent) -> AgentState:
    return {
        "run_id": run_id,
        "status": "running",
        "message": message,
        "intent": intent,
        "task_id": task_id,
        "source_id": source_id,
        "study_instance_uid": study_instance_uid,
        "series_instance_uid": series_instance_uid,
        "diagnostic_level": "unknown",
        "iteration": 0,
        "max_iterations": max_iterations,
        "tool_call_history": [],
        "pending_tool_calls": [],
        "converged": False,
        "evidence": [],
        "tool_results": {},
        "diagnose_attempts": 0,
        "citation_ok": False,
        "errors": [],
        # 同一 run_id 复用同一 checkpoint thread：显式清空上一轮周期状态，
        # 否则未在本次 input 中出现的旧字段会沿用 checkpoint 里的历史值（状态泄漏）。
        "diagnosis": None,
        "reflection": None,
        "repull_plan": None,
        "approval_status": None,
        "operator": None,
        "action_result": None,
        "stop_reason": None,
        "hypothesis": None,
        "citation_violations": [],
    }


def run_agent_streaming(
    message: str = "",
    task_id: Optional[str] = None,
    study_instance_uid: Optional[str] = None,
    series_instance_uid: Optional[str] = None,
    source_id: str = "orthanc-local",
    run_id: str = "adhoc",
    checkpointer=None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    intent: Optional[str] = None,
) -> dict:
    """流式执行：逐节点拿 state 增量 → 发过程事件 → 聚合出最终 state。

    LangGraph 原生 stream_mode="updates"，不改图结构。事件承载在 app.agent.events（Redis）。
    LLM 不可用时事件标 degraded（受限轨迹，不伪装，plan §4.5）。
    """
    from app.agent import events as ev
    from app.agent.llm import llm_available

    app = build_graph(checkpointer=checkpointer)
    initial = _initial_state(message, task_id, study_instance_uid, series_instance_uid,
                             source_id, run_id, max_iterations, intent)
    config = {"configurable": {"thread_id": run_id}}
    degraded = not llm_available()

    merged: dict = dict(initial)
    try:
        for chunk in app.stream(initial, config=config, stream_mode="updates"):
            # chunk: {node_name: state_update}；interrupt 时为 {"__interrupt__": (Interrupt,...)}。
            for node_name, update in (chunk or {}).items():
                if node_name == "__interrupt__":
                    # 图在 human_approval 暂停等人工（护栏 4）。checkpoint 已存暂停态，
                    # 等 API 侧 Command(resume) 恢复。此处标 awaiting_approval 供落库。
                    merged["status"] = "awaiting_approval"
                    ev.publish_event(run_id, {"type": "node", "node": "human_approval",
                                              "awaiting_approval": True, "degraded": degraded})
                    continue
                if isinstance(update, dict):
                    merged.update(update)
                    event = ev.node_to_event(node_name, update, degraded=degraded)
                    if event:
                        ev.publish_event(run_id, event)
    finally:
        # awaiting_approval 是 interrupt 暂停，不是终态——mark_done 会让 SSE 提前挂 done，
        # 断掉后续 execute/awaiting_repull 这段用户还要看的过程。真正终态才收尾。
        if merged.get("status") != "awaiting_approval":
            ev.mark_done(run_id)
    return merged


def resume_after_approval(run_id: str, action: str, operator: Optional[str] = None) -> dict:
    """审批恢复（HITL replay）：用 Command(resume) 从 human_approval 暂停处继续图执行。

    approve → 图内 execute 节点执行写操作后到 END；reject → human_approval 直接到 END。
    必须用与暂停时同一个 checkpointer + thread_id=run_id 才能恢复暂停态。
    返回恢复后跑完的最终 state（含 approval_status / action_result / status）。

    补发过程事件（同一条 SSE 流延续，不需要前端知道要重连）：审批结果本身 + execute
    节点执行结果。awaiting_repull（补拉已提交，等 C-MOVE 终态）不 mark_done——由 Checker
    成功/unverified 收尾，或失败事件触发下一轮诊断图收尾；其余（rejected/completed/failed）
    已到终态，这里收尾。
    """
    from langgraph.types import Command

    from app.agent import events as ev

    checkpointer = get_checkpointer()
    app = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": run_id}}
    result = app.invoke(Command(resume={"action": action, "operator": operator}), config=config)

    ev.publish_event(run_id, {"type": "node", "node": "human_approval", "resumed": True,
                              "action": action, "approval_status": result.get("approval_status")})
    action_result = result.get("action_result")
    if action_result:
        event = ev.node_to_event("execute", {"action_result": action_result})
        if event:
            ev.publish_event(run_id, event)

    if result.get("status") != "awaiting_repull":
        ev.mark_done(run_id)
    return result
