"""Study-list orchestration around the existing single-study LangGraph flow."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

from app.agent.graph import run_agent_streaming, resume_after_approval
from app.core.config import settings


def normalize_study_uids(values: List[str], single: str = None) -> List[str]:
    values = [str(v).strip() for v in (values or []) if str(v).strip()]
    if single and single.strip() and single.strip() not in values:
        values.insert(0, single.strip())
    if len(values) > settings.agent.batch_max_studies:
        raise ValueError("study_instance_uid_list supports at most %d studies" % settings.agent.batch_max_studies)
    return list(dict.fromkeys(values))


def _is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(x in text for x in ("timeout", "timed out", "connection", "temporarily", "unavailable", "rabbitmq"))


def run_batch(message: str, study_uids: List[str], source_id: str, run_id: str,
              intent: str = None, checkpointer=None) -> Dict:
    """Run existing graph once per Study with bounded concurrency and isolated checkpoints."""
    results = {}

    def one(uid):
        retries = 0
        last_error = None
        while True:
            child_thread = "%s:study:%s" % (run_id, uid)
            try:
                state = run_agent_streaming(
                    message=message, study_instance_uid=uid, source_id=source_id,
                    run_id=run_id, thread_id=child_thread, intent=intent,
                    checkpointer=checkpointer,
                    mark_done=False,
                )
                status = state.get("status", "completed")
                return uid, {
                    "study_instance_uid": uid, "status": status,
                    "task_id": state.get("task_id"), "diagnosis": state.get("diagnosis"),
                    "repull_plan": state.get("repull_plan"),
                    "approval_status": state.get("approval_status"),
                    "retry_count": retries, "thread_id": child_thread,
                    "error": (state.get("errors") or [None])[-1],
                }
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if retries >= settings.agent.batch_transient_retries or not _is_transient(exc):
                    return uid, {"study_instance_uid": uid, "status": "failed",
                                 "retry_count": retries, "thread_id": child_thread,
                                 "error_code": "network_error" if _is_transient(exc) else "execution_error",
                                 "error": last_error}
                retries += 1

    workers = min(max(1, settings.agent.batch_concurrency), len(study_uids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, uid) for uid in study_uids]
        for future in as_completed(futures):
            uid, result = future.result()
            results[uid] = result

    return aggregate_batch(results, study_uids)


def aggregate_batch(results: Dict, study_uids: List[str]) -> Dict:
    statuses = [results.get(uid, {}).get("status", "failed") for uid in study_uids]
    summary = {
        "total": len(study_uids),
        "completed": sum(s == "completed" for s in statuses),
        "failed": sum(s == "failed" for s in statuses),
        "awaiting_approval": sum(s == "awaiting_approval" for s in statuses),
        "awaiting_repull": sum(s == "awaiting_repull" for s in statuses),
        "retried": sum((results.get(uid, {}).get("retry_count") or 0) > 0 for uid in study_uids),
        "partial_failure": any(s == "failed" for s in statuses) and any(s != "failed" for s in statuses),
    }
    if summary["awaiting_approval"]:
        status = "awaiting_approval"
    elif summary["awaiting_repull"]:
        status = "awaiting_repull"
    elif summary["failed"] == summary["total"]:
        status = "failed"
    elif summary["failed"]:
        status = "partial_failed"
    else:
        status = "completed"
    return {"status": status, "batch_summary": summary, "study_results": results}


def resume_batch_after_approval(run_id: str, batch: Dict, action: str, operator: str = None) -> Dict:
    """Resume each interrupted Study graph; failures remain isolated."""
    results = dict(batch.get("study_results") or {})
    for uid, item in results.items():
        if item.get("status") != "awaiting_approval":
            continue
        try:
            state = resume_after_approval(run_id, item["thread_id"], action, operator)
            item.update({"status": state.get("status", "completed"),
                         "task_id": state.get("task_id") or item.get("task_id"),
                         "diagnosis": state.get("diagnosis"),
                         "action_result": state.get("action_result"),
                         "approval_status": state.get("approval_status")})
        except Exception as exc:  # noqa: BLE001
            item.update({"status": "failed", "error_code": "approval_execution_error", "error": str(exc)})
    return aggregate_batch(results, list(results))
