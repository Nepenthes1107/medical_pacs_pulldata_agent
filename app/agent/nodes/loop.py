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
from hashlib import sha256
from typing import Dict, List
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage

from app.agent.state import AgentState, truncate_evidence

# act 只能执行注册表内工具（护栏 3/4）。注册表与绑定给模型的工具是同一份，
# 不再单独维护执行表——两份表分叉时「模型可见」与「工程允许」会不一致。
from app.agent.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)


def _fingerprint(name: str, args: Dict) -> str:
    """(工具, 参数) 指纹——用于无进展熔断（护栏 5）。"""
    return name + ":" + json.dumps(args or {}, sort_keys=True, ensure_ascii=False)


def _observation_signature(output: Dict) -> str:
    """对结构化 Observation 做稳定摘要，用于判断重复调用是否产生新信息。"""
    raw = json.dumps(output or {}, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _evidence_digest(state: AgentState) -> str:
    """把已采集的结构化证据压成给 LLM 阅读的摘要（唯一真相来源，护栏 3）。"""
    lines = []
    for ev in state.get("evidence", []):
        out = ev.get("output", {})
        ok = out.get("success")
        # 去掉冗长字段，保留关键结构化信号。
        compact = {k: v for k, v in out.items()
                   if k not in ("error",) and v not in (None, [], {}, "")}
        # 失败调用额外附错误码与修正方向，Reason 据此改下一步调用而不是重复犯错。
        tail = ""
        if not ok:
            tail = " error_code=%s correction=%s" % (out.get("error_code"), out.get("correction"))
        lines.append("- tool_id=%s %s(%s) success=%s result_status=%s → %s%s"
                     % (ev.get("tool_id"), ev.get("tool"),
                        json.dumps(ev.get("args", {}), ensure_ascii=False),
                        ok, out.get("result_status"),
                        json.dumps(compact, ensure_ascii=False)[:300], tail))
    return "\n".join(lines) if lines else "（尚无证据）"


def _protocol_tool_calls(calls: List[Dict]) -> List[Dict]:
    """给待执行调用分配协议 ID，供 AIMessage 与 ToolMessage 严格配对。"""
    return [{**call, "tool_call_id": call.get("tool_call_id") or "call_" + str(uuid4())}
            for call in calls]


def _tool_result_message(call: Dict, output: Dict) -> ToolMessage:
    """所有工具 Observation（含拒绝/失败）均通过标准 ToolMessage 回传。"""
    return ToolMessage(
        content=json.dumps(output, ensure_ascii=False, default=str),
        tool_call_id=call["tool_call_id"],
        name=call.get("name"),
        additional_kwargs={"agent_message_scope": "reason"},
    )


def _programmatic_tool_request_message(calls: List[Dict]) -> AIMessage:
    """为无 LLM 降级/程序化重试建立合法的工具请求前置消息。"""
    return AIMessage(content="", tool_calls=[{
        "name": call["name"], "args": call.get("args", {}) or {},
        "id": call["tool_call_id"], "type": "tool_call",
    } for call in calls], additional_kwargs={"agent_message_scope": "reason"})


def _pending_with_message(calls: List[Dict]) -> Dict:
    """为非 LLM 产生的工具调用补齐标准 AIMessage/ToolMessage 配对。"""
    calls = _protocol_tool_calls(calls)
    return {
        "pending_tool_calls": calls,
        "messages": [_programmatic_tool_request_message(calls)] if calls else [],
    }


def _structured_result(response):
    """解包 include_raw 结构化输出，同时保留模型原始 AIMessage。"""
    if not isinstance(response, dict) or "parsed" not in response:
        return response, []
    if response.get("parsing_error"):
        raise response["parsing_error"]
    parsed = response.get("parsed")
    if parsed is None:
        raise ValueError("structured LLM response has no parsed value")
    raw = response.get("raw")
    return parsed, [raw] if isinstance(raw, AIMessage) else []


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
    from app.agent.llm import get_reasoning_model, llm_available, reasoning_prompt

    if not llm_available():
        return _reason_degraded(state)

    try:
        from app.agent.prompts import get_system_prompt, xml_blocks
        model = get_reasoning_model()
        prompt = reasoning_prompt(
            get_system_prompt("reason"),
            state,
            xml_blocks([
                ("current_task", _target_block(state)),
                ("rule_findings", state.get("rule_findings", [])[-5:]),
                ("reflection_feedback", (state.get("reflection") or {}).get("reason") or "（无）"),
            ]),
        )
        message = model.invoke(prompt)
    except Exception as exc:  # LLM 调用失败：退回强信号规则，不阻断循环
        logger.warning("reason LLM 调用失败，退回受限规则推理: %s", exc)
        errors = list(state.get("errors", [])) + ["reason LLM 失败: %s" % str(exc)[:80]]
        patch = _reason_degraded(state)
        patch["errors"] = errors
        return patch

    if not isinstance(message, AIMessage):
        raise TypeError("reason model must return AIMessage")
    calls = _protocol_tool_calls([{
        "name": c.get("name"), "args": c.get("args", {}) or {},
        "tool_call_id": c.get("id"),
    } for c in (message.tool_calls or [])])
    message.tool_calls = [{
        "name": call["name"], "args": call["args"],
        "id": call["tool_call_id"], "type": "tool_call",
    } for call in calls]
    if not calls:
        return {"converged": True, "messages": [message]}
    return {"pending_tool_calls": calls, "messages": [message]}


def _reason_degraded(state: AgentState) -> Dict:
    """无 LLM 降级：只跑强信号最小探查序列，并标注受限（plan §3.5，不伪装）。

    仅采集最小证据集（连通性/任务/接收/完整性）后即收敛，不做多信号自主归因。
    """
    tr = state.get("tool_results", {})
    source_id = state.get("source_id", "orthanc-local")
    study = state.get("study_instance_uid")
    series = state.get("series_instance_uid")
    level = state.get("diagnostic_level", "study")

    # compute_integrity 的 level 是受约束枚举；路由未定级时（初始 "unknown"）退到 study，
    # 否则降级路径会自己造出被参数校验拒绝的调用。
    if level not in ("study", "series", "sop"):
        level = "study"

    pacs = tr.get("query_pacs_target")
    # 强信号：PACS 不可达 → 直接收敛。
    if pacs and pacs.get("success") and not pacs.get("pacs_reachable"):
        return {"converged": True}

    if "query_pacs_target" not in tr and study:
        args = {"source_id": source_id, "study_instance_uid": study}
        if series:
            args["series_instance_uid"] = series
        return _pending_with_message([{"name": "query_pacs_target", "args": args}])
    if "query_task_context" not in tr and (study or state.get("task_id")):
        args = {}
        if state.get("task_id"):
            args["task_id"] = state["task_id"]
        if study:
            args["study_instance_uid"] = study
        return _pending_with_message([{"name": "query_task_context", "args": args}])
    if "query_receive_status" not in tr and study:
        args = {"study_instance_uid": study}
        if series:
            args["series_instance_uid"] = series
        return _pending_with_message([{"name": "query_receive_status", "args": args}])
    if "compute_integrity" not in tr and study:
        expected = pacs.get("expected_instance_count") if pacs and pacs.get("success") else None
        receive = tr.get("query_receive_status", {})
        local = receive.get("local_unique_sop_count", 0) if receive.get("success") else 0
        return _pending_with_message([
            {"name": "compute_integrity",
             "args": {"expected": expected, "local_unique_sop": local, "level": level}}
        ])
    return {"converged": True}


# ==========================================================================
# act：执行 reason 选定的 tool_call（参数过 Pydantic，异常收敛为 success=false）
# ==========================================================================

def _rejected(error: str, code: str, correction: str) -> Dict:
    """未执行工具时的标准化 Observation（result_status=error，一律不可重试）。"""
    return {"success": False, "result_status": "error", "error": error,
            "error_code": code, "correction": correction, "retryable": False}


def _arg_violations(exc) -> str:
    """只回显字段名与约束类型，不回显输入值——避免把模型编造的内容再灌回上下文。"""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append("%s:%s" % (loc, err.get("type", "invalid")))
    return "; ".join(parts[:6])


def _run_tool_call(call: Dict) -> Dict:
    """执行单个 tool_call：先过注册表 + 参数 Schema，再真正调用。供线程池并发调度。

    执行前校验的意义：参数非法时不接触 PACS / MySQL / RabbitMQ，也不消耗外部配额；
    返回的是结构化、可纠正的 Observation（error_code + correction），而不是裸异常。
    只读工具无共享可变状态，线程安全；写工具不在注册表，一律拒绝（护栏 3/4）。
    """
    from pydantic import ValidationError

    name = call.get("name")
    args = call.get("args", {}) or {}
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        # 注册表外调用（含模型编造的工具名、写工具）一律拒绝，绝不执行。
        return _rejected("tool not allowed: %s" % name, "tool_not_allowed",
                         "改用已注册工具名：" + "、".join(sorted(TOOL_REGISTRY)))
    if not isinstance(args, dict):
        return _rejected("args must be an object", "invalid_arguments", "args 必须是键值对象")

    schema = tool.args_schema
    try:
        validated = schema.model_validate(args)
    except ValidationError as exc:
        # extra="forbid" 会在这里拦下模型附加的未知参数；缺失/越界同样在此拦截。
        return _rejected("invalid arguments for %s: %s" % (name, _arg_violations(exc)),
                         "invalid_arguments", "按 Schema 修正参数后重新调用")

    try:
        result = tool.func(**validated.model_dump())
    except Exception as exc:
        # 工具内部已收敛绝大多数异常；走到这里是未预期错误，只留安全摘要。
        logger.warning("工具 %s 执行异常: %s", name, exc, exc_info=True)
        return _rejected("%s execution failed (%s)" % (name, type(exc).__name__),
                         "tool_execution_failed", "该调用失败，不能据此断言目标不存在")

    from app.agent.tool_schemas import BaseToolOutput

    if not isinstance(result, BaseToolOutput):
        # 返回值不符合输出 Schema：不让异常结构混进证据链。
        logger.error("工具 %s 返回值类型非法: %r", name, type(result))
        return _rejected("%s returned invalid output" % name, "invalid_tool_output",
                         "该调用结果不可用，请换用其它工具采证")
    return result.model_dump()


def act(state: AgentState) -> Dict:
    """执行 pending_tool_calls，把结构化结果并入 tool_results，记录指纹。

    多个只读工具用线程池并发执行（IO 密集：PACS/DB 查询）；但结果按原 call 顺序回填，
    保持 tool_call_history/evidence 顺序确定——否则 should_continue 的无进展熔断判断会失准。
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.core.config import settings

    calls = _protocol_tool_calls(state.get("pending_tool_calls", []) or [])
    # 诊断/首拉/澄清意图下禁止 search_knowledge（RAG 仅 knowledge_qa 可用）：
    # 不抛异常让整张图崩溃，而是收敛为 tool_not_allowed 结构化拒绝，
    # 让 ReAct 收到「该工具不可用」后改用其它只读工具继续采证。
    rag_gated = state.get("use_rag") is not True

    def _run(call: Dict) -> Dict:
        if rag_gated and call.get("name") == "search_knowledge":
            return _rejected("search_knowledge requires knowledge_qa intent",
                             "tool_not_allowed", "诊断阶段请改用其它只读工具采证，勿调用知识库检索")
        return _run_tool_call(call)
    tool_results = dict(state.get("tool_results", {}))
    history = list(state.get("tool_call_history", []))
    new_evidence: List[Dict] = []
    action_observations = list(state.get("action_observations", []))

    # 指纹按 call 原始顺序追加（熔断判断依赖此顺序，不能被并发打乱）。
    for call in calls:
        history.append(_fingerprint(call.get("name"), call.get("args", {}) or {}))

    # 工具执行并发化：单调用直接跑，避免线程池开销；多调用走线程池。结果按索引回填保序。
    if len(calls) <= 1:
        outputs = [_run(call) for call in calls]
    else:
        workers = min(len(calls), max(1, settings.agent.act_max_workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outputs = list(pool.map(_run, calls))

    tool_messages = []
    for call, out in zip(calls, outputs):
        name = call.get("name")
        args = call.get("args", {}) or {}
        tool_id = str(uuid4())
        tool_results[name] = {**out, "tool_id": tool_id}
        new_evidence.append({
            "tool_id": tool_id,
            "run_id": state["run_id"],
            "thread_id": state["thread_id"],
            "tool": name,
            "args": args,
            "output": out,
            "success": bool(out.get("success")),
        })
        action_observations.append({
            "fingerprint": _fingerprint(name, args),
            "observation_signature": _observation_signature(out),
        })
        tool_messages.append(_tool_result_message(call, out))

    # MySQL 保存完整证据链；State 只保留近期窗口。持久化失败应使节点失败，避免产生
    # 无法审计、也无法在窗口外校验的诊断结果。
    from app.agent.audit import record_tool_evidence

    record_tool_evidence(new_evidence)

    evidence = list(state.get("evidence", [])) + new_evidence
    return {
        "tool_results": tool_results,
        "last_tool_ids": [item["tool_id"] for item in new_evidence],
        "tool_call_history": history,
        "action_observations": action_observations[-20:],
        "evidence": truncate_evidence(evidence),
        "messages": tool_messages,
        "pending_tool_calls": [],
        "iteration": state.get("iteration", 0) + 1,
    }


# ==========================================================================
# observe / Rule Validator：确定性处理工具结果、瞬时重试和无进展熔断。
# ==========================================================================

# 这些错误码代表调用本身非法，重试同一调用必然再次失败，直接交回 Reason 修正。
_NO_RETRY_CODES = {"invalid_arguments", "tool_not_allowed", "invalid_tool_output"}


def observe(state: AgentState) -> Dict:
    """校验最新 Observation；明确工具异常不调用 Reflection。

    retryable=true 的相同 Action+Input 由程序最多重试一次；查询失败始终保留为失败事实，
    不会被转换成 study_exists=false 等业务结论。连续相同调用且输出摘要不变则停止。
    """
    from app.core.config import settings

    last_ids = set(state.get("last_tool_ids", []) or [])
    current = [item for item in state.get("evidence", []) if item.get("tool_id") in last_ids]
    retries = dict(state.get("tool_retry_counts", {}))
    findings = list(state.get("rule_findings", []))
    retry_calls = []

    for item in current:
        output = item.get("output", {}) or {}
        if output.get("success"):
            continue
        name = item.get("tool", "unknown")
        args = item.get("args", {}) or {}
        fingerprint = _fingerprint(name, args)
        error = str(output.get("error") or "unknown error")[:160]
        # 参数错误、未知工具、非法输出不重试——重试同样的非法调用没有任何进展。
        if output.get("error_code") in _NO_RETRY_CODES:
            findings.append("%s 调用被拒绝（%s），不重试；修正方向：%s" % (
                name, output.get("error_code"), output.get("correction") or "按 Schema 修正参数",
            ))
            continue
        if output.get("retryable") and retries.get(fingerprint, 0) < settings.agent.tool_max_retries:
            retries[fingerprint] = retries.get(fingerprint, 0) + 1
            retry_calls.append({"name": name, "args": args})
            findings.append("%s 瞬时失败，程序化重试 %d/%d：%s" % (
                name, retries[fingerprint], settings.agent.tool_max_retries, error,
            ))
        else:
            kind = "重试耗尽" if output.get("retryable") else "不可重试"
            findings.append("%s %s；该失败不能解释为目标不存在：%s" % (name, kind, error))

    records = state.get("action_observations", []) or []
    current_count = len(last_ids)
    unchanged = _current_observations_unchanged(records, current_count)
    if unchanged:
        return {
            "pending_tool_calls": [],
            "tool_retry_counts": retries,
            "rule_findings": (findings + ["相同 Action + Input 的 Observation 无变化，停止重复执行"])[-20:],
            "converged": False,
            "stop_reason": "no_progress",
        }

    if state.get("iteration", 0) >= state.get("max_iterations", settings.agent.max_agent_steps):
        retry_calls = []
    patch = _pending_with_message(retry_calls)
    patch.update({
        "tool_retry_counts": retries,
        "rule_findings": findings[-20:],
    })
    return patch


def _current_observations_unchanged(records: List[Dict], current_count: int) -> bool:
    """连续两个批次的 Action+Input 与 Observation 都相同时视为无进展。"""
    if current_count <= 0 or len(records) < current_count * 2:
        return False
    previous = records[-current_count * 2:-current_count]
    current = records[-current_count:]
    return all(
        old.get("fingerprint") == item.get("fingerprint")
        and old.get("observation_signature") == item.get("observation_signature")
        for old, item in zip(previous, current)
    )


def route_after_observe(state: AgentState) -> str:
    """程序重试直回 act；无进展/预算耗尽收束；其余交回 ReAct Reason。"""
    if state.get("stop_reason") == "no_progress":
        return "diagnose"
    if state.get("iteration", 0) >= state.get("max_iterations", 8):
        return "diagnose"
    if state.get("pending_tool_calls"):
        return "act"
    return "reason"


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
    pending = state.get("pending_tool_calls", []) or []
    if not pending:
        # reason 既没收敛也没提工具 → 无可推进，收束。
        return "diagnose"
    return "act"


def _stop_reason(state: AgentState) -> str:
    if state.get("stop_reason") == "no_progress":
        return "no_progress"
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
        from app.agent.llm import get_structured_model, reasoning_prompt
        from app.agent.prompts import diagnosis_few_shot, get_system_prompt, xml_blocks
        from app.agent.tool_schemas import GroundedDiagnosis

        model = get_structured_model(GroundedDiagnosis, include_raw=True)
        prompt = reasoning_prompt(
            get_system_prompt("diagnosis"),
            state,
            xml_blocks([
                ("diagnostic_target", _target_block(state)),
                ("evidence", state.get("evidence", [])),
                ("citation_violations", state.get("citation_violations", [])),
            ]) + "\n\n" + diagnosis_few_shot(),
        )
        out, raw_messages = _structured_result(model.invoke(prompt))
        out.diagnostic_level = level  # level 由代码定，防漂移
        diagnosis = out.model_dump()
        diagnosis["integrity_result"] = result
        diagnosis["stop_reason"] = stop
        return {"diagnosis": diagnosis, "status": "completed", "stop_reason": stop,
                "messages": raw_messages}
    except Exception as exc:
        logger.warning("diagnose LLM 调用失败，退回受限诊断: %s", exc)
        diagnosis = _diagnose_degraded(state, result, level, stop)
        diagnosis["summary"] = "[受限:LLM异常] " + diagnosis["summary"]
        return {"diagnosis": diagnosis, "status": "completed", "stop_reason": stop,
                "errors": list(state.get("errors", [])) + ["diagnose LLM 失败: %s" % str(exc)[:80]]}


MAX_DIAGNOSE_ATTEMPTS = 2  # 护栏 2：引用不过打回重述最多一次（首次 + 一次修正）


def verify_diagnosis(state: AgentState) -> Dict:
    """护栏 2：只校验引用真实性，不评价证据充分性或诊断推理。

    - 引用逐条核对（harness.verify_citations，确定性代码）。
    - 全部接地 → 通过。
    - 有不接地且还没修正过 → 记违规、attempts+1，路由回 diagnose 重述一次。
    - 修正后仍不过 → 降级 uncertain、剔除无法接地的 claims（弃权比幻觉安全）。
    """
    from app.agent.harness import filter_grounded_claims, verify_citations

    diagnosis = dict(state.get("diagnosis") or {})
    attempts = state.get("diagnose_attempts", 0)

    grounded, violations = verify_citations(
        diagnosis,
        state.get("evidence", []),
        state["thread_id"],
    )

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
    diagnosis["claims"] = filter_grounded_claims(
        diagnosis,
        state.get("evidence", []),
        state["thread_id"],
    )
    diagnosis["summary"] = "[弃权] 引用未通过接地校验，降为不确定。" + diagnosis.get("summary", "")
    return {"diagnosis": diagnosis, "citation_ok": True,
            "errors": list(state.get("errors", [])) + violations}


def route_after_verify(state: AgentState) -> str:
    """引用不过且可修正 → 回 diagnose 重述；否则进 reflect。"""
    return "diagnose" if not state.get("citation_ok") else "reflect"


def reflect(state: AgentState) -> Dict:
    """护栏 7：统一判断证据充分性、推理有效性与结论强度。

    reflect 不改写 claims、数字或 root_cause，只输出三态推理判定并调整 confidence。
    未完成 reflect 验证的非 uncertain 诊断不得进入补拉规划。
    """
    from app.agent.llm import llm_available
    from app.core.config import settings

    diagnosis = dict(state.get("diagnosis") or {})
    if not llm_available():
        diagnosis["confidence"] = "uncertain"
        return {
            "diagnosis": diagnosis,
            "reflection_attempts": settings.agent.max_reflection_revisions + 1,
            "reflection": {
                "inference_status": "unsupported",
                "reason": "LLM 不可用，未完成诊断推理验证",
            },
        }
    try:
        from app.agent.harness import build_citation_facts
        from app.agent.llm import contextual_prompt, get_structured_model
        from app.agent.prompts import get_system_prompt, xml_blocks
        from app.agent.tool_schemas import Reflection

        citation_facts = build_citation_facts(
            diagnosis,
            state.get("evidence", []),
            state["thread_id"],
        )
        model = get_structured_model(Reflection, include_raw=True)
        prompt = contextual_prompt(
            get_system_prompt("reflect"),
            state,
            xml_blocks([
                ("diagnosis", diagnosis),
                ("verified_citation_facts", citation_facts),
                ("evidence", state.get("evidence", [])),
            ]),
        )
        r, raw_messages = _structured_result(model.invoke(prompt))
        reflection = r.model_dump()
        if r.inference_status == "overstated":
            diagnosis["confidence"] = {
                "confirmed": "high",
                "high": "uncertain",
                "uncertain": "uncertain",
            }.get(diagnosis.get("confidence"), "uncertain")
        elif r.inference_status == "unsupported":
            attempts = state.get("reflection_attempts", 0) + 1
            can_revise = (
                attempts <= settings.agent.max_reflection_revisions
                and state.get("iteration", 0) < state.get("max_iterations", settings.agent.max_agent_steps)
            )
            if can_revise:
                # 推理不成立但工具链仍有预算：把原因交回 Reason，只允许有限次数重新采证。
                return {
                    "diagnosis": diagnosis,
                    "reflection": reflection,
                    "reflection_attempts": attempts,
                    "converged": False,
                    "pending_tool_calls": [],
                    "status": "running",
                    "stop_reason": None,
                    "messages": raw_messages,
                }
            diagnosis["confidence"] = "uncertain"
            diagnosis["missing_evidence"] = list(diagnosis.get("missing_evidence", [])) + [
                "推理验证未通过：%s" % r.reason
            ]
            return {
                "diagnosis": diagnosis,
                "reflection": reflection,
                "reflection_attempts": attempts,
                "messages": raw_messages,
            }
        return {"diagnosis": diagnosis, "reflection": reflection, "messages": raw_messages}
    except Exception as exc:
        logger.warning("reflect 失败，诊断降为 uncertain: %s", exc)
        diagnosis["confidence"] = "uncertain"
        return {
            "diagnosis": diagnosis,
            # 调用异常不是业务证据不足，不再触发额外 LLM 循环。
            "reflection_attempts": settings.agent.max_reflection_revisions + 1,
            "reflection": {
                "inference_status": "unsupported",
                "reason": "推理验证失败: %s" % str(exc)[:80],
            },
        }


def route_after_reflect(state: AgentState) -> str:
    """首次业务性 unsupported 回 Reason 补证；其余结果进入现有规划门禁。"""
    reflection = state.get("reflection") or {}
    if reflection.get("inference_status") != "unsupported":
        return "plan_repull"
    from app.core.config import settings

    attempts = state.get("reflection_attempts", 0)
    if (
        0 < attempts <= settings.agent.max_reflection_revisions
        and state.get("iteration", 0) < state.get("max_iterations", settings.agent.max_agent_steps)
        and state.get("status") == "running"
    ):
        return "reason"
    return "plan_repull"


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
        from app.agent.llm import contextual_prompt, get_structured_model
        from app.agent.prompts import get_system_prompt, xml_blocks
        from app.agent.tool_schemas import RepullPlan

        model = get_structured_model(RepullPlan, include_raw=True)
        prompt = contextual_prompt(
            get_system_prompt("plan_repull"),
            state,
            xml_blocks([
                ("diagnosis", diagnosis),
                ("evidence", state.get("evidence", [])),
            ]),
        )
        plan_result, raw_messages = _structured_result(model.invoke(prompt))
        plan = plan_result.model_dump()
        from app.agent.harness import verify_citations

        grounded, violations = verify_citations(
            {"claims": [{"claim": "repull_plan", "evidence_refs": plan["evidence_refs"]}]},
            state.get("evidence", []),
            state["thread_id"],
        )
        if not grounded:
            return {
                "repull_plan": None,
                "errors": list(state.get("errors", []))
                + ["补拉计划引用校验失败: %s" % v for v in violations],
                "messages": raw_messages,
            }
        # 只产出计划，不设 status——是否暂停审批由下游 human_approval 节点的 interrupt 决定
        # （awaiting_approval 状态由 stream 层检测到 __interrupt__ 时设置）。
        return {"repull_plan": plan, "messages": raw_messages}
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
