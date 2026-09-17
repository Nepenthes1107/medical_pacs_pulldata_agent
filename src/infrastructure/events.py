"""Redis-backed process-event adapter for legacy run streams."""
import json
import logging

from src.core.settings import settings

logger = logging.getLogger(__name__)

_EVENT_TTL_SECONDS = 3600
_DONE = "__done__"


def _key(run_id: str) -> str:
    return "run_events:%s" % run_id


def _client():
    import redis

    return redis.Redis.from_url(settings.redis.url)


def publish_event(run_id: str, event: dict) -> None:
    """Append a best-effort compatibility event to the run's Redis list."""
    try:
        client = _client()
        key = _key(run_id)
        client.rpush(key, json.dumps(event, ensure_ascii=False))
        client.expire(key, _EVENT_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("process event publish failed: %s", exc)


def record_execution_event(run_id: str, thread_id: str, node_name: str, update: dict) -> None:
    """Persist a bounded node audit through the event adapter boundary."""
    from src.infrastructure.db.audit import record_execution_event as _record
    _record(run_id, thread_id, node_name, update)


def mark_done(run_id: str) -> None:
    publish_event(run_id, {"type": _DONE})


def read_events(run_id: str, offset: int = 0) -> list[dict]:
    try:
        raw = _client().lrange(_key(run_id), offset, -1)
        return [json.loads(item) for item in raw]
    except Exception as exc:  # noqa: BLE001
        logger.debug("process event read failed: %s", exc)
        return []


def is_done_event(event: dict) -> bool:
    return event.get("type") == _DONE


def node_to_event(node_name: str, update: dict, degraded: bool = False) -> dict | None:
    event: dict = {"type": "node", "node": node_name, "degraded": degraded}
    if node_name == "reason":
        calls = update.get("pending_tool_calls") or []
        event["next_tools"] = [call.get("name") for call in calls]
        event["converged"] = update.get("converged", False)
    elif node_name == "act":
        results = update.get("tool_results") or {}
        event["observed"] = {
            name: {"success": output.get("success")} for name, output in results.items()
        }
    elif node_name == "observe":
        event["retry_tools"] = [
            call.get("name") for call in (update.get("pending_tool_calls") or [])
        ]
        event["rule_findings"] = (update.get("rule_findings") or [])[-3:]
        event["stop_reason"] = update.get("stop_reason")
    elif node_name == "diagnose":
        diagnosis = update.get("diagnosis") or {}
        event["summary"] = diagnosis.get("summary")
        event["confidence"] = diagnosis.get("confidence")
    elif node_name == "reflect":
        event["reflection"] = update.get("reflection")
        event["revision_attempts"] = update.get("reflection_attempts")
    elif node_name == "plan_repull":
        event["repull_plan"] = update.get("repull_plan")
        event["status"] = update.get("status")
    elif node_name in ("first_pull", "retrieve_and_answer", "present_clarification"):
        event["summary"] = (update.get("diagnosis") or {}).get("summary")
    elif node_name == "execute":
        action_result = update.get("action_result") or {}
        event["success"] = action_result.get("success")
        event["strategy"] = action_result.get("strategy")
        event["submitted"] = action_result.get("submitted")
        event["error"] = action_result.get("error")
    else:
        return None
    return event
