"""LangGraph AgentState（plan §4.1 / spec §7）。

自主循环版：State 只存循环需要的东西，不堆历史对话。
- 循环控制字段服务护栏 5（预算 / 无进展熔断）。
- evidence 为结构化证据集，每条记录 {tool, fields, values}，是护栏 2 引用校验的真相源。
上下文截断约束：evidence ≤MAX_EVIDENCE 条，retrieved_knowledge ≤3 条。
"""
from typing import Dict, List, Optional, TypedDict

# 上下文截断常量（spec §7）
MAX_EVIDENCE = 20
MAX_RETRIEVED_KNOWLEDGE = 3

# 循环预算默认值（护栏 5，可被 run_agent 覆盖）
DEFAULT_MAX_ITERATIONS = 8


class AgentState(TypedDict, total=False):
    # ── 运行标识 ──
    run_id: str
    status: str

    # ── 输入与路由 ──
    message: str
    intent: Optional[str]  # 显式意图（first_pull|diagnosis|knowledge_qa），最高优先
    route: str  # "first_pull" | "diagnosis" | "knowledge_qa" | "clarification"
    parse_source: str  # "explicit" | "rule" | "llm"
    task_id: Optional[str]
    source_id: str
    diagnostic_level: str  # "study" | "series" | "unknown"
    study_instance_uid: Optional[str]
    series_instance_uid: Optional[str]

    # ── 循环控制（护栏 5）──
    iteration: int  # 当前循环轮次
    max_iterations: int  # 预算上限
    tool_call_history: List[str]  # 已执行的 (工具,参数) 指纹列表，供无进展熔断
    pending_tool_calls: List[Dict]  # reason 决定的下一批 tool_call（name+args）
    converged: bool  # reason 判定证据足够
    stop_reason: Optional[str]  # "converged" | "budget" | "no_progress"

    # ── 证据集（护栏 2 的真相源）──
    # 每条：{"tool": str, "args": dict, "output": dict, "success": bool}
    evidence: List[Dict]
    tool_results: Dict  # {tool_name: 最近一次结构化输出}，供引用校验按工具名索引

    # ── 推理载体 ──
    hypothesis: Optional[str]  # LLM 当前根因假设（随轮次更新）

    # ── RAG 检索结果（knowledge_qa 出口用）──
    retrieved_knowledge: List[str]

    # ── 诊断与反思输出 ──
    diagnosis: Optional[Dict]  # 结构化根因，含 evidence_refs
    diagnose_attempts: int  # 护栏 2：引用打回重述次数
    citation_ok: bool  # 引用接地校验是否通过
    citation_violations: List[str]  # 最近一次校验的失败原因（供 diagnose 修正）
    reflection: Optional[Dict]  # reflect 自检结果

    # ── 补拉计划与审批 ──
    repull_plan: Optional[Dict]  # plan_repull 产出的参数化计划
    approval_status: Optional[str]
    operator: Optional[str]  # 审批人（interrupt resume 时带入）
    action_result: Optional[Dict]

    # ── 澄清/冲突 ──
    clarification: Optional[str]

    # ── 错误收集 ──
    errors: List[str]


def truncate_evidence(evidence: List[Dict]) -> List[Dict]:
    """按 spec §7 截断结构化证据：保留最近 MAX_EVIDENCE 条。"""
    return evidence[-MAX_EVIDENCE:]
