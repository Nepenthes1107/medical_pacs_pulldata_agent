from app.agent.nodes import loop


def _state(output, *, observations=None, retries=None, iteration=1):
    args = {"source_id": "orthanc-local", "study_instance_uid": "1.2.3.4"}
    return {
        "last_tool_ids": ["tool-1"],
        "evidence": [{
            "tool_id": "tool-1",
            "tool": "query_pacs_target",
            "args": args,
            "output": output,
        }],
        "action_observations": observations or [{
            "fingerprint": loop._fingerprint("query_pacs_target", args),
            "observation_signature": loop._observation_signature(output),
        }],
        "tool_retry_counts": retries or {},
        "rule_findings": [],
        "iteration": iteration,
        "max_iterations": 8,
    }


def test_retryable_tool_failure_is_retried_once():
    state = _state({"success": False, "retryable": True, "error": "timeout"})

    patch = loop.observe(state)

    assert len(patch["pending_tool_calls"]) == 1
    assert patch["pending_tool_calls"][0]["name"] == "query_pacs_target"
    assert patch["pending_tool_calls"][0]["args"] == {
        "source_id": "orthanc-local", "study_instance_uid": "1.2.3.4",
    }
    assert patch["pending_tool_calls"][0]["tool_call_id"].startswith("call_")
    assert patch["messages"][0].tool_calls[0]["id"] == patch["pending_tool_calls"][0]["tool_call_id"]
    assert loop.route_after_observe({**state, **patch}) == "act"


def test_non_retryable_failure_returns_to_reason_without_claiming_absence():
    state = _state({"success": False, "retryable": False, "error": "invalid argument"})

    patch = loop.observe(state)

    assert patch["pending_tool_calls"] == []
    assert "不能解释为目标不存在" in patch["rule_findings"][-1]
    assert loop.route_after_observe({**state, **patch}) == "reason"


def test_same_action_with_changed_observation_can_continue():
    state = _state({"success": True, "study_exists": True})
    state["action_observations"].insert(0, {
        "fingerprint": state["action_observations"][0]["fingerprint"],
        "observation_signature": loop._observation_signature({"success": False, "error": "timeout"}),
    })

    patch = loop.observe(state)

    assert patch.get("stop_reason") is None
    assert loop.route_after_observe({**state, **patch}) == "reason"


def test_invalid_arguments_is_not_retried():
    # 参数非法时重试同一调用必然再失败，必须交回 Reason 修正而不是程序化重试。
    state = _state({
        "success": False, "result_status": "error", "retryable": True,
        "error": "invalid arguments", "error_code": "invalid_arguments",
        "correction": "补充 study_instance_uid",
    })

    patch = loop.observe(state)

    assert patch["pending_tool_calls"] == []
    assert patch["tool_retry_counts"] == {}
    assert "补充 study_instance_uid" in patch["rule_findings"][-1]
    assert loop.route_after_observe({**state, **patch}) == "reason"


def test_unknown_tool_rejection_is_not_retried():
    state = _state({
        "success": False, "result_status": "error", "retryable": False,
        "error": "tool not allowed: query_everything", "error_code": "tool_not_allowed",
        "correction": "改用已注册工具名",
    })

    patch = loop.observe(state)

    assert patch["pending_tool_calls"] == []
    assert "tool_not_allowed" in patch["rule_findings"][-1]


def test_unavailable_dependency_is_still_retried_once():
    state = _state({
        "success": False, "result_status": "unavailable", "retryable": True,
        "error": "task query failed (OperationalError)",
        "error_code": "dependency_unavailable",
    })

    patch = loop.observe(state)

    assert len(patch["pending_tool_calls"]) == 1
    assert loop.route_after_observe({**state, **patch}) == "act"


def test_empty_result_is_not_treated_as_failure():
    state = _state({"success": True, "result_status": "empty", "tasks": []})

    patch = loop.observe(state)

    assert patch["pending_tool_calls"] == []
    assert patch["rule_findings"] == []


def test_same_action_and_observation_stops_as_no_progress():
    state = _state({"success": True, "study_exists": True})
    state["action_observations"] = state["action_observations"] * 2

    patch = loop.observe(state)

    assert patch["stop_reason"] == "no_progress"
    assert loop.route_after_observe({**state, **patch}) == "diagnose"
