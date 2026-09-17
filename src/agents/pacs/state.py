"""LangGraph AgentState（plan §4.1 / spec §7）。

自主循环版：State 保存循环工作集，以及经预算管理后的会话消息与摘要。
- 循环控制字段服务护栏 5（预算 / 无进展熔断）。
- evidence 为近期结构化证据集；完整证据链按 tool_id 保存在 MySQL。
上下文截断约束：evidence ≤MAX_EVIDENCE 条，retrieved_knowledge ≤5 条。
"""
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

# 上下文截断常量（spec §7）
MAX_EVIDENCE = 20
MAX_RETRIEVED_KNOWLEDGE = 5

class AgentState(TypedDict, total=False):
    # ── 运行标识 ──
    run_id: str
    thread_id: str
    status: str

    # ── 会话上下文（Redis Checkpointer 按 thread_id 持久化）──
    messages: Annotated[list[AnyMessage], add_messages]
    context_summary: str
    conversation_context: str
    context_token_count: int

    # ── 输入与路由 ──
    message: str
    intent: str | None  # 显式意图（first_pull|diagnosis|knowledge_qa），最高优先
    route: str  # "first_pull" | "diagnosis" | "knowledge_qa" | "clarification"
    use_rag: bool  # 仅 knowledge_qa 意图进入知识检索
    parse_source: str  # "explicit" | "rule" | "llm"
    task_id: str | None
    source_id: str
    diagnostic_level: str  # "study" | "series" | "unknown"
    study_instance_uid: str | None
    study_instance_uid_list: list[str]
    series_instance_uid: str | None

    # ── 循环控制（护栏 5）──
    iteration: int  # 当前循环轮次
    max_iterations: int  # 预算上限
    tool_call_history: list[str]  # 已执行的 (工具,参数) 指纹列表，供无进展熔断
    pending_tool_calls: list[dict]  # reason 决定的下一批 tool_call（name+args）
    tool_retry_counts: dict[str, int]  # Action+Input 指纹对应的程序化重试次数
    action_observations: list[dict]  # 近期 {fingerprint, observation_signature}，供无进展判断
    rule_findings: list[str]  # Observe 的确定性校验摘要，供下一轮 Reason 使用
    converged: bool  # reason 判定证据足够
    stop_reason: str | None  # "converged" | "budget" | "no_progress"

    # ── 证据集（护栏 2 的真相源）──
    # 每条近期证据：{"tool_id", "run_id", "thread_id", "tool", "args", "output", "success"}；完整链在 MySQL。
    evidence: list[dict]
    tool_results: dict  # {tool_name: 最近一次结构化输出}，只供推理/路由便利读取，不作为引用真相源
    last_tool_ids: list[str]  # 最近一次 act 产生的 ID，仅供轻量执行轨迹建立证据引用

    # ── RAG 检索结果（knowledge_qa 出口用）──
    retrieved_knowledge: list[dict]

    # ── 诊断与反思输出 ──
    diagnosis: dict | None  # 结构化根因，含 evidence_refs
    diagnose_attempts: int  # 护栏 2：引用打回重述次数
    citation_ok: bool  # 引用接地校验是否通过
    citation_violations: list[str]  # 最近一次校验的失败原因（供 diagnose 修正）
    reflection: dict | None  # {inference_status, reason}，负责证据充分性与推理有效性
    reflection_attempts: int  # unsupported 后已使用的重新采证次数

    # ── 补拉计划与审批 ──
    repull_plan: dict | None  # plan_repull 产出的参数化计划
    approval_status: str | None
    operator: str | None  # 审批人（interrupt resume 时带入）
    action_result: dict | None

    # ── 澄清/冲突 ──
    clarification: str | None

    # ── 错误收集 ──
    errors: list[str]


def truncate_evidence(evidence: list[dict]) -> list[dict]:
    """按 spec §7 截断结构化证据：保留最近 MAX_EVIDENCE 条。"""
    return evidence[-MAX_EVIDENCE:]


