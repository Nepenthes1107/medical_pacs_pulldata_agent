"""Persistence adapter for execution logs and immutable tool evidence."""
from typing import Any

from src.infrastructure.db.models import AgentExecutionLog, AgentToolEvidence
from src.infrastructure.db.session import session_scope


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def build_execution_summary(node_name: str, update: dict) -> dict:
    """Build a bounded execution summary; never duplicate tool payloads."""
    summary: dict[str, Any] = {}
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
            {"tool_id": tool_id, **latest_by_id.get(tool_id, {})} for tool_id in tool_ids
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


def record_execution_event(run_id: str, thread_id: str, node_name: str, update: dict) -> None:
    event_type = "tool_call" if node_name == "act" else "node"
    with session_scope() as db:
        db.add(AgentExecutionLog(
            run_id=run_id,
            thread_id=thread_id,
            event_type=event_type,
            node_name=node_name,
            payload=build_execution_summary(node_name, update),
        ))


def record_exception(run_id: str, thread_id: str, error: Exception) -> None:
    with session_scope() as db:
        db.add(AgentExecutionLog(
            run_id=run_id,
            thread_id=thread_id,
            event_type="exception",
            error=str(error),
        ))


def record_tool_evidence(evidence: list[dict]) -> None:
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


def load_tool_evidence(tool_ids: list[str], thread_id: str) -> dict[str, dict]:
    if not tool_ids:
        return {}
    with session_scope() as db:
        rows = (
            db.query(AgentToolEvidence)
            .filter(
                AgentToolEvidence.tool_id.in_(tool_ids),
                AgentToolEvidence.thread_id == thread_id,
            )
            .all()
        )
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
