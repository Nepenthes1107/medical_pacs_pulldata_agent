"""Batch Application Service around the single-study PACS graph."""
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.agents.pacs.graph import resume_after_approval, run_agent_streaming
from src.core.settings import settings


def normalize_study_uids(values: list[str], single: str | None = None) -> list[str]:
    values = [str(value).strip() for value in (values or []) if str(value).strip()]
    if single and single.strip() and single.strip() not in values:
        values.insert(0, single.strip())
    if len(values) > settings.agent.batch_max_studies:
        raise ValueError(
            "study_instance_uid_list supports at most %d studies" % settings.agent.batch_max_studies
        )
    return list(dict.fromkeys(values))


def _is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(value in text for value in (
        "timeout", "timed out", "connection", "temporarily", "unavailable", "rabbitmq"
    ))


def run_batch(message: str, study_uids: list[str], source_id: str, run_id: str,
              intent: str | None = None, checkpointer=None) -> dict:
    results = {}

    def one(uid: str):
        retries = 0
        while True:
            child_thread = "%s:study:%s" % (run_id, uid)
            try:
                state = run_agent_streaming(
                    message=message, study_instance_uid=uid, source_id=source_id,
                    run_id=run_id, thread_id=child_thread, intent=intent,
                    checkpointer=checkpointer, mark_done=False,
                )
                return uid, {
                    "study_instance_uid": uid, "status": state.get("status", "completed"),
                    "task_id": state.get("task_id"), "diagnosis": state.get("diagnosis"),
                    "repull_plan": state.get("repull_plan"),
                    "approval_status": state.get("approval_status"), "retry_count": retries,
                    "thread_id": child_thread, "error": (state.get("errors") or [None])[-1],
                }
            except Exception as exc:  # noqa: BLE001
                transient = _is_transient(exc)
                if retries >= settings.agent.batch_transient_retries or not transient:
                    return uid, {
                        "study_instance_uid": uid, "status": "failed", "retry_count": retries,
                        "thread_id": child_thread,
                        "error_code": "network_error" if transient else "execution_error",
                        "error": str(exc),
                    }
                retries += 1

    workers = min(max(1, settings.agent.batch_concurrency), len(study_uids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, uid) for uid in study_uids]
        for future in as_completed(futures):
            uid, result = future.result()
            results[uid] = result
    return aggregate_batch(results, study_uids)


def aggregate_batch(results: dict, study_uids: list[str]) -> dict:
    statuses = [results.get(uid, {}).get("status", "failed") for uid in study_uids]
    summary = {
        "total": len(study_uids),
        "completed": sum(status == "completed" for status in statuses),
        "failed": sum(status == "failed" for status in statuses),
        "awaiting_approval": sum(status == "awaiting_approval" for status in statuses),
        "awaiting_repull": sum(status == "awaiting_repull" for status in statuses),
        "retried": sum((results.get(uid, {}).get("retry_count") or 0) > 0 for uid in study_uids),
        "partial_failure": any(status == "failed" for status in statuses)
        and any(status != "failed" for status in statuses),
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


def resume_batch_after_approval(run_id: str, batch: dict, action: str,
                                operator: str | None = None) -> dict:
    results = dict(batch.get("study_results") or {})
    for uid, item in results.items():
        if item.get("status") != "awaiting_approval":
            continue
        try:
            state = resume_after_approval(run_id, item["thread_id"], action, operator)
            item.update({
                "status": state.get("status", "completed"),
                "task_id": state.get("task_id") or item.get("task_id"),
                "diagnosis": state.get("diagnosis"),
                "action_result": state.get("action_result"),
                "approval_status": state.get("approval_status"),
            })
        except Exception as exc:  # noqa: BLE001
            item.update({"status": "failed", "error_code": "approval_execution_error", "error": str(exc)})
    return aggregate_batch(results, list(results))
