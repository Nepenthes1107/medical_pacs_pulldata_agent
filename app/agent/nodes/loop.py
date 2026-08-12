"""自主推理循环节点（plan §3 / spec §3）：reason ↔ act ↔ observe → diagnose。

Stage B：reason/diagnose 为规则版占位，跑通图拓扑与三个终止条件；
         Stage C 把 reason/diagnose 换成真 LLM（自主决策 + 结构化根因）。

控制权分层（plan §10.6）：
- reason 决定「下一步查什么 / 够了没」——自主（Stage C 交 LLM）。
- act 执行工具、observe 回灌证据——机械。
- 循环「何时必须停」——确定性代码，写在 should_continue 条件边，绝不交给模型。
"""
import json
import logging
from typing import Dict, List

from app.agent.state import AgentState, truncate_evidence
from app.agent.tools import (
    compute_integrity,
    query_missing_instances,
    query_pacs_hierarchy,
    query_pacs_target,
    query_receive_status,
    query_task_context,
    query_task_history,
    query_worker_health,
    search_knowledge,
)

logger = logging.getLogger(__name__)

# 只读工具执行表：act 只能执行白名单内工具（护栏 3/4，写工具不在此）。
_TOOL_FUNCS = {
    "query_pacs_target": query_pacs_target,
    "query_pacs_hierarchy": query_pacs_hierarchy,
    "query_task_context": query_task_context,
    "query_task_history": query_task_history,
    "query_worker_health": query_worker_health,
    "query_receive_status": query_receive_status,
    "query_missing_instances": query_missing_instances,
    "compute_integrity": compute_integrity,
    "search_knowledge": search_knowledge,
}


def _fingerprint(name: str, args: Dict) -> str:
    """(工具, 参数) 指纹——用于无进展熔断（护栏 5）。"""
    return name + ":" + json.dumps(args or {}, sort_keys=True, ensure_ascii=False)


def _evidence_digest(state: AgentState) -> str:
    """把已采集的结构化证据压成给 LLM 阅读的摘要（唯一真相来源，护栏 3）。"""
    lines = []
    for ev in state.get("evidence", []):
        out = ev.get("output", {})
        ok = out.get("success")
        # 去掉冗长字段，保留关键结构化信号。
        compact = {k: v for k, v in out.items()
                   if k not in ("error",) and v not in (None, [], {}, "")}
        lines.append("- %s(%s) success=%s → %s"
                     % (ev.get("tool"), json.dumps(ev.get("args", {}), ensure_ascii=False),
                        ok, json.dumps(compact, ensure_ascii=False)[:300]))
    return "\n".join(lines) if lines else "（尚无证据）"


def _target_block(state: AgentState) -> str:
    return ("诊断层级: %s\nsource_id: %s\ntask_id: %s\nStudyInstanceUID: %s\nSeriesInstanceUID: %s"
            % (state.get("diagnostic_level"), state.get("source_id"), state.get("task_id"),
               state.get("study_instance_uid"), state.get("series_instance_uid")))


# ==========================================================================
# reason：LLM 自主决策下一步（护栏 3/5）；LLM 不可用时降级为强信号规则（plan §3.5）
# ==========================================================================

def reason(state: AgentState) -> Dict:
    """基于当前证据决定下一步 tool_call，或判定 converged。

    LLM 可用：模型自主决定查什么/够没够（真实自主性）。
    LLM 不可用：只跑强信号规则并标注受限——不伪装成完整自主诊断（plan §3.5 / spec §10）。
    """
    from app.agent.llm import get_reasoning_model, llm_available

    if not llm_available():
        return _reason_degraded(state)

    try:
        from app.agent.prompts import REASON_SYSTEM_PROMPT
        from app.agent.tool_schemas import ReasonDecision

        model = get_reasoning_model().with_structured_output(ReasonDecision)
        prompt = (
            REASON_SYSTEM_PROMPT
            + "\n\n## 诊断对象\n" + _target_block(state)
            + "\n\n## 已收集证据（唯一真相来源）\n" + _evidence_digest(state)
            + "\n\n## 当前假设\n" + (state.get("hypothesis") or "（无）")
            + "\n\n请给出 ReasonDecision。"
        )
        decision = model.invoke(prompt)
    except Exception as exc:  # LLM 调用失败：退回强信号规则，不阻断循环
        logger.warning("reason LLM 调用失败，退回受限规则推理: %s", exc)
        errors = list(state.get("errors", [])) + ["reason LLM 失败: %s" % str(exc)[:80]]
        patch = _reason_degraded(state)
        patch["errors"] = errors
        return patch

    if decision.converged:
        return {"converged": True, "hypothesis": decision.hypothesis or state.get("hypothesis", "")}
    calls = [{"name": c.name, "args": c.args or {}} for c in decision.tool_calls]
    return {"pending_tool_calls": calls, "hypothesis": decision.hypothesis or state.get("hypothesis", "")}


def _reason_degraded(state: AgentState) -> Dict:
    """无 LLM 降级：只跑强信号最小探查序列，并标注受限（plan §3.5，不伪装）。

    仅采集最小证据集（连通性/任务/接收/完整性）后即收敛，不做多信号自主归因。
    """
    tr = state.get("tool_results", {})
    source_id = state.get("source_id", "orthanc-local")
    study = state.get("study_instance_uid")
    series = state.get("series_instance_uid")
    level = state.get("diagnostic_level", "study")

    pacs = tr.get("query_pacs_target")
    # 强信号：PACS 不可达 → 直接收敛。
    if pacs and pacs.get("success") and not pacs.get("pacs_reachable"):
        return {"converged": True, "hypothesis": "[受限] PACS 不可达，链路无法开始"}

    if "query_pacs_target" not in tr and study:
        args = {"source_id": source_id, "study_instance_uid": study}
        if series:
            args["series_instance_uid"] = series
        return {"pending_tool_calls": [{"name": "query_pacs_target", "args": args}]}
    if "query_task_context" not in tr and (study or state.get("task_id")):
        args = {}
        if state.get("task_id"):
            args["task_id"] = state["task_id"]
        if study:
            args["study_instance_uid"] = study
        return {"pending_tool_calls": [{"name": "query_task_context", "args": args}]}
    if "query_receive_status" not in tr and study:
        args = {"study_instance_uid": study}
        if series:
            args["series_instance_uid"] = series
        return {"pending_tool_calls": [{"name": "query_receive_status", "args": args}]}
    if "compute_integrity" not in tr and study:
        expected = pacs.get("expected_instance_count") if pacs and pacs.get("success") else None
        receive = tr.get("query_receive_status", {})
        local = receive.get("local_unique_sop_count", 0) if receive.get("success") else 0
        return {"pending_tool_calls": [
            {"name": "compute_integrity",
             "args": {"expected": expected, "local_unique_sop": local, "level": level}}
        ]}
    return {"converged": True, "hypothesis": "[受限] LLM 不可用，仅采集最小证据集"}


# ==========================================================================
# act：执行 reason 选定的 tool_call（参数过 Pydantic，异常收敛为 success=false）
# ==========================================================================

def _run_tool_call(call: Dict) -> Dict:
    """执行单个 tool_call，异常收敛为结构化失败（不崩循环）。供线程池并发调度。

    只读工具（白名单内）本身无共享可变状态，线程安全；写工具不在白名单，一律拒绝（护栏 3/4）。
    """
    name = call.get("name")
    args = call.get("args", {}) or {}
    func = _TOOL_FUNCS.get(name)
    if func is None:
        # 白名单外调用（含写工具）一律拒绝——护栏 3/4。
        return {"success": False, "error": "tool not allowed: %s" % name}
    try:
        return func(**args).model_dump()
    except Exception as exc:  # 参数校验等异常收敛为结构化失败，不崩循环
        return {"success": False, "error": "%s failed: %s" % (name, exc)}


def act(state: AgentState) -> Dict:
    """执行 pending_tool_calls，把结构化结果并入 tool_results，记录指纹。

    多个只读工具用线程池并发执行（IO 密集：PACS/DB 查询）；但结果按原 call 顺序回填，
    保持 tool_call_history/evidence 顺序确定——否则 should_continue 的无进展熔断判断会失准。
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.core.config import settings

    calls = state.get("pending_tool_calls", []) or []
    tool_results = dict(state.get("tool_results", {}))
    history = list(state.get("tool_call_history", []))
    new_evidence: List[Dict] = []

    # 指纹按 call 原始顺序追加（熔断判断依赖此顺序，不能被并发打乱）。
    for call in calls:
        history.append(_fingerprint(call.get("name"), call.get("args", {}) or {}))

    # 工具执行并发化：单调用直接跑，避免线程池开销；多调用走线程池。结果按索引回填保序。
    if len(calls) <= 1:
        outputs = [_run_tool_call(call) for call in calls]
    else:
        workers = min(len(calls), max(1, settings.agent.act_max_workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outputs = list(pool.map(_run_tool_call, calls))

    for call, out in zip(calls, outputs):
        name = call.get("name")
        args = call.get("args", {}) or {}
        tool_results[name] = out
        new_evidence.append({"tool": name, "args": args, "output": out,
                             "success": bool(out.get("success"))})

    evidence = list(state.get("evidence", [])) + new_evidence
    return {
        "tool_results": tool_results,
        "tool_call_history": history,
        "evidence": truncate_evidence(evidence),
        "pending_tool_calls": [],
        "iteration": state.get("iteration", 0) + 1,
    }


# ==========================================================================
# observe：薄节点，仅回到 reason（证据已在 act 里回灌）。保留为独立节点以贴合 ReAct 语义与 trace。
# ==========================================================================

def observe(state: AgentState) -> Dict:
    return {}


# ==========================================================================
# 终止条件（护栏 5）：全部由代码判断，绝不交给 LLM。
# ==========================================================================

def should_continue(state: AgentState) -> str:
    """reason 后的条件边：决定继续 act 还是收束到 diagnose。"""
    # 1) LLM/规则主动收敛。
    if state.get("converged"):
        return "diagnose"
    # 2) 预算耗尽。
    iteration = state.get("iteration", 0)
    if iteration >= state.get("max_iterations", 8):
        return "diagnose"
    # 3) 无进展熔断：本轮拟提的 tool_call 与历史最近一次完全相同。
    pending = state.get("pending_tool_calls", []) or []
    history = state.get("tool_call_history", []) or []
    if pending and history:
        pending_fps = [_fingerprint(c.get("name"), c.get("args", {})) for c in pending]
        # 连续两轮相同工具+参数 → 卡死。
        if all(fp in history[-len(pending):] for fp in pending_fps):
            return "diagnose"
    if not pending:
        # reason 既没收敛也没提工具 → 无可推进，收束。
        return "diagnose"
    return "act"


def _stop_reason(state: AgentState) -> str:
    if state.get("converged"):
        return "converged"
    if state.get("iteration", 0) >= state.get("max_iterations", 8):
        return "budget"
    return "no_progress"


# ==========================================================================
# diagnose（Stage B 规则占位；Stage C 换 LLM 结构化输出 + evidence_refs）
# ==========================================================================

def diagnose(state: AgentState) -> Dict:
    """产出结构化根因（Stage C）：LLM 关联多信号推断，每条断言带 evidence_refs。

    LLM 不可用：降级为受限诊断——只陈述已采集的强信号事实，confidence=uncertain，
    明确标注「LLM 不可用，本次为受限诊断」，不伪装完整归因（plan §3.5）。
    引用校验（护栏 2）在 Stage D 的独立校验器里做，diagnose 只负责产出。
    """
    from app.agent.llm import llm_available

    stop = _stop_reason(state)
    tr = state.get("tool_results", {})
    integrity = tr.get("compute_integrity", {})
    result = integrity.get("result", "unverified")
    level = state.get("diagnostic_level", "study")

    if not llm_available():
        diagnosis = _diagnose_degraded(state, result, level, stop)
        return {"diagnosis": diagnosis, "status": "completed", "stop_reason": stop}

    try:
        from app.agent.llm import get_structured_model
        from app.agent.prompts import DIAGNOSIS_SYSTEM_PROMPT
        from app.agent.tool_schemas import GroundedDiagnosis

        model = get_structured_model(GroundedDiagnosis)
        prompt = (
            DIAGNOSIS_SYSTEM_PROMPT
            + "\n\n## 诊断对象\n" + _target_block(state)
            + "\n\n## 已收集证据（唯一真相来源，引用必须来自这里）\n" + _evidence_digest(state)
            + "\n\n## 当前假设\n" + (state.get("hypothesis") or "（无）")
            + "\n\n请产出 GroundedDiagnosis，每条 claim 必须带 evidence_refs。"
        )
        out = model.invoke(prompt)
        out.diagnostic_level = level  # level 由代码定，防漂移
        diagnosis = out.model_dump()
        diagnosis["integrity_result"] = result
        diagnosis["stop_reason"] = stop
        return {"diagnosis": diagnosis, "status": "completed", "stop_reason": stop}
    except Exception as exc:
        logger.warning("diagnose LLM 调用失败，退回受限诊断: %s", exc)
        diagnosis = _diagnose_degraded(state, result, level, stop)
        diagnosis["summary"] = "[受限:LLM异常] " + diagnosis["summary"]
        return {"diagnosis": diagnosis, "status": "completed", "stop_reason": stop,
                "errors": list(state.get("errors", [])) + ["diagnose LLM 失败: %s" % str(exc)[:80]]}


MAX_DIAGNOSE_ATTEMPTS = 2  # 护栏 2：引用不过打回重述最多一次（首次 + 一次修正）


def verify_diagnosis(state: AgentState) -> Dict:
    """护栏 2/6：引用接地校验 + 弃权判定。

    - 引用逐条核对（harness.verify_citations，确定性代码）。
    - 全部接地 → 通过。
    - 有不接地且还没修正过 → 记违规、attempts+1，路由回 diagnose 重述一次。
    - 修正后仍不过 → 降级 uncertain、剔除无法接地的 claims（弃权比幻觉安全）。
    - 关键证据缺失（护栏 6）→ 直接 uncertain，不硬下结论。
    """
    from app.agent.harness import key_evidence_present, verify_citations

    diagnosis = dict(state.get("diagnosis") or {})
    tr = state.get("tool_results", {})
    attempts = state.get("diagnose_attempts", 0)

    grounded, violations = verify_citations(diagnosis, tr)

    # 护栏 6：关键证据缺失 → 弃权。
    if not key_evidence_present(tr):
        diagnosis["confidence"] = "uncertain"
        missing = list(diagnosis.get("missing_evidence", []))
        if "query_pacs_target 未成功" not in missing:
            missing.append("query_pacs_target 未成功")
        diagnosis["missing_evidence"] = missing
        return {"diagnosis": diagnosis, "citation_ok": True}

    if grounded:
        return {"diagnosis": diagnosis, "citation_ok": True}

    # 有引用不接地：还能修正 → 打回 diagnose。
    if attempts < MAX_DIAGNOSE_ATTEMPTS - 1:
        return {
            "citation_ok": False,
            "diagnose_attempts": attempts + 1,
            "citation_violations": violations,
            "errors": list(state.get("errors", [])) + violations,
        }
    # 修正后仍不过 → 弃权：剔除不接地 claims，降 uncertain。
    diagnosis["confidence"] = "uncertain"
    diagnosis["missing_evidence"] = list(diagnosis.get("missing_evidence", [])) + [
        "引用无法接地，已弃权：%s" % v for v in violations[:3]
    ]
    diagnosis["claims"] = []  # 不保留未经核实的断言
    diagnosis["summary"] = "[弃权] 引用未通过接地校验，降为不确定。" + diagnosis.get("summary", "")
    return {"diagnosis": diagnosis, "citation_ok": True,
            "errors": list(state.get("errors", [])) + violations}


def route_after_verify(state: AgentState) -> str:
    """引用不过且可修正 → 回 diagnose 重述；否则进 reflect。"""
    return "diagnose" if not state.get("citation_ok") else "reflect"


def reflect(state: AgentState) -> Dict:
    """护栏 7：终局前一次自我批判（结论能否由所引证据推出、有无过度归因）。

    LLM 可用则做语义自检；不可用则跳过（不伪装）。reflect 不改数字、不翻案根因，
    只在明显过度归因时把 confidence 往下压——软性兜底，硬约束仍在护栏 2/6。
    """
    from app.agent.llm import llm_available

    diagnosis = dict(state.get("diagnosis") or {})
    if not llm_available():
        return {"reflection": {"skipped": True, "reason": "LLM 不可用"}}
    try:
        from app.agent.llm import get_structured_model
        from app.agent.prompts import REFLECT_SYSTEM_PROMPT
        from app.agent.tool_schemas import Reflection

        model = get_structured_model(Reflection)
        prompt = (
            REFLECT_SYSTEM_PROMPT
            + "\n\n## 诊断结论\n" + json.dumps(diagnosis, ensure_ascii=False)[:1500]
            + "\n\n## 证据\n" + _evidence_digest(state)
            + "\n\n请给出 Reflection。"
        )
        r = model.invoke(prompt)
        reflection = r.model_dump()
        # 过度归因或结论不被支持 → 压置信度（不翻案，只降级，软性兜底）。
        if (r.over_attribution or not r.supported) and diagnosis.get("confidence") == "confirmed":
            diagnosis["confidence"] = "high"
        return {"diagnosis": diagnosis, "reflection": reflection}
    except Exception as exc:
        logger.warning("reflect 失败，跳过自检: %s", exc)
        return {"reflection": {"skipped": True, "reason": str(exc)[:80]}}


def plan_repull(state: AgentState) -> Dict:
    """产出参数化补拉计划（Stage E / spec §6）。策略由 LLM 自主选，代码只承载。

    护栏 6：confidence=uncertain 时不产出补拉计划，改列缺哪些证据——弃权比乱补安全。
    LLM 不可用：不臆造计划，直接完成（受限，不补拉）。
    计划落 state.repull_plan；下游 human_approval 节点据此 interrupt 暂停，交人工闸门（护栏 4）。
    """
    diagnosis = state.get("diagnosis") or {}
    # 护栏 6：不确定 → 不补拉（无计划，human_approval 节点将直接放行完成）。
    if diagnosis.get("confidence") == "uncertain":
        return {"repull_plan": None}

    from app.agent.llm import llm_available

    if not llm_available():
        return {"repull_plan": None}

    try:
        from app.agent.llm import get_structured_model
        from app.agent.prompts import PLAN_REPULL_SYSTEM_PROMPT
        from app.agent.tool_schemas import RepullPlan

        model = get_structured_model(RepullPlan)
        prompt = (
            PLAN_REPULL_SYSTEM_PROMPT
            + "\n\n## 诊断结论\n" + json.dumps(diagnosis, ensure_ascii=False)[:1500]
            + "\n\n## 证据\n" + _evidence_digest(state)
            + "\n\n请产出 RepullPlan。"
        )
        plan = model.invoke(prompt).model_dump()
        # 只产出计划，不设 status——是否暂停审批由下游 human_approval 节点的 interrupt 决定
        # （awaiting_approval 状态由 stream 层检测到 __interrupt__ 时设置）。
        return {"repull_plan": plan}
    except Exception as exc:
        logger.warning("plan_repull 失败，不产出补拉计划: %s", exc)
        return {"repull_plan": None,
                "errors": list(state.get("errors", [])) + ["plan_repull 失败: %s" % str(exc)[:80]]}


def _diagnose_degraded(state: AgentState, result: str, level: str, stop: str) -> Dict:
    """无 LLM 受限诊断：只陈述强信号事实，不做多信号归因，confidence 恒 uncertain。"""
    tr = state.get("tool_results", {})
    pacs = tr.get("query_pacs_target", {})
    facts = []
    if pacs.get("success") and not pacs.get("pacs_reachable"):
        facts.append("PACS 不可达")
    ci = tr.get("compute_integrity", {})
    if ci.get("result") == "incomplete":
        facts.append("完整性缺口 missing=%s" % ci.get("missing"))
    summary = "[受限] LLM 不可用，仅给出强信号事实：" + ("；".join(facts) if facts else "已采集最小证据集")
    return {
        "diagnostic_level": level,
        "summary": summary,
        "root_cause": "",
        "confidence": "uncertain",
        "claims": [],
        "missing_evidence": ["LLM 不可用，未做多信号自主归因"],
        "integrity_result": result,
        "stop_reason": stop,
        "degraded": True,
    }
