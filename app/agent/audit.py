"""LangGraph 轻量执行轨迹与完整工具证据的 MySQL 持久化。"""
from typing import Any, Dict, Optional

from app.core.database import session_scope
from app.core.models import AgentExecutionLog, AgentToolEvidence


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def record_execution_event(
    run_id: str,
    thread_id: str,
    node_name: str,
    update: Dict,
) -> None:
    """追加节点轨迹；payload 只保存状态摘要和 tool_id 引用。"""
    event_type = "tool_call" if node_name == "act" else "node"
    payload = build_execution_summary(node_name, update)
    with session_scope() as db:
        db.add(AgentExecutionLog(
            run_id=run_id,
            thread_id=thread_id,
            event_type=event_type,
            node_name=node_name,
            payload=payload,
        ))


def build_execution_summary(node_name: str, update: Dict) -> Dict:
    """把节点增量压缩为可查询摘要，禁止复制完整工具参数与输出。"""
    summary: Dict[str, Any] = {}
    scalar_fields = (
        "status", "route", "parse_source", "diagnostic_level", "iteration",
        "converged", "stop_reason", "citation_ok", "diagnose_attempts",
        "approval_status",
    )
    for field in scalar_fields:
        value = update.get(field)
        if value is not None and isinstance(value, (str, int, float, bool)):
            summary[field] = value

    if node_name == "reason":
        calls = update.get("pending_tool_calls") or []
        summary["planned_tools"] = [call.get("name") for call in calls if call.get("name")]
    elif node_name == "act":
        tool_ids = update.get("last_tool_ids") or []
        summary["tool_ids"] = list(tool_ids)
        latest_by_id = {
            result.get("tool_id"): {"tool": name, "success": bool(result.get("success"))}
            for name, result in (update.get("tool_results") or {}).items()
            if result.get("tool_id") in tool_ids
        }
        summary["tool_status"] = [
            {"tool_id": tool_id, **latest_by_id.get(tool_id, {})}
            for tool_id in tool_ids
        ]
    elif node_name == "observe":
        summary["retry_tools"] = [
            call.get("name") for call in (update.get("pending_tool_calls") or []) if call.get("name")
        ]
        summary["rule_findings"] = (update.get("rule_findings") or [])[-3:]
    elif node_name == "diagnose":
        diagnosis = update.get("diagnosis") or {}
        summary["confidence"] = diagnosis.get("confidence")
        summary["claim_count"] = len(diagnosis.get("claims") or [])
    elif node_name == "reflect":
        reflection = update.get("reflection") or {}
        diagnosis = update.get("diagnosis") or {}
        summary["inference_status"] = reflection.get("inference_status")
        summary["confidence"] = diagnosis.get("confidence")
        summary["reflection_attempts"] = update.get("reflection_attempts")
    elif node_name == "plan_repull":
        plan = update.get("repull_plan")
        summary["plan_generated"] = plan is not None
        if plan:
            summary["strategy"] = plan.get("strategy")
    elif node_name == "execute":
        result = update.get("action_result") or {}
        summary["success"] = result.get("success")
        summary["strategy"] = result.get("strategy")
        summary["submitted"] = result.get("submitted")
    elif node_name == "human_approval":
        summary["action"] = update.get("action")

    return _json_safe(summary)


def record_exception(run_id: str, thread_id: str, error: Exception) -> None:
    with session_scope() as db:
        db.add(AgentExecutionLog(
            run_id=run_id,
            thread_id=thread_id,
            event_type="exception",
            error=str(error),
        ))


def record_tool_evidence(evidence: list[Dict]) -> None:
    """在工具节点返回 State 前，批量持久化本轮完整证据。"""
    if not evidence:
        return
    with session_scope() as db:
        db.add_all([
            AgentToolEvidence(
                tool_id=item["tool_id"],
                run_id=item["run_id"],
                thread_id=item["thread_id"],
                tool_name=item["tool"],
                tool_args=_json_safe(item["args"]),
                output=_json_safe(item["output"]),
                success=item["success"],
            )
            for item in evidence
        ])


def load_tool_evidence(tool_ids: list[str], thread_id: str) -> Dict[str, Dict]:
    """按 tool_id 批量回查已离开 State 窗口的证据，并校验会话隔离。"""
    if not tool_ids:
        return {}
    with session_scope() as db:
        rows = (db.query(AgentToolEvidence)
                .filter(AgentToolEvidence.tool_id.in_(tool_ids),
                        AgentToolEvidence.thread_id == thread_id)
                .all())
        return {
            row.tool_id: {
                "tool_id": row.tool_id,
                "tool": row.tool_name,
                "args": row.tool_args,
                "output": row.output,
                "success": row.success,
            }
            for row in rows
        }
