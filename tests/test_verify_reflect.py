import pytest

from app.agent.harness import build_citation_facts, verify_citations
from app.agent.nodes import loop


TOOL_ID = "11111111-1111-1111-1111-111111111111"


def _evidence(tool_id=TOOL_ID, success=True):
    return {
        "tool_id": tool_id,
        "thread_id": "thread-1",
        "tool": "compute_integrity",
        "args": {"expected": 10, "local_unique_sop": 8, "level": "study"},
        "output": {"success": success, "missing": 2, "result": "incomplete"},
        "success": success,
    }


def _diagnosis(confidence="confirmed", tool_id=TOOL_ID, field="missing", value=2):
    return {
        "summary": "本地缺少 2 个实例",
        "root_cause": "传输不完整",
        "confidence": confidence,
        "claims": [{
            "claim": "完整性缺口为 2",
            "evidence_refs": [{"tool_id": tool_id, "field": field, "value": value}],
        }],
        "missing_evidence": [],
    }


def test_verify_does_not_require_pacs_key_evidence():
    state = {
        "diagnosis": _diagnosis(),
        "evidence": [_evidence()],
        "thread_id": "thread-1",
        "diagnose_attempts": 0,
        "errors": [],
    }

    patch = loop.verify_diagnosis(state)

    assert patch["citation_ok"] is True
    assert patch["diagnosis"]["confidence"] == "confirmed"


@pytest.mark.parametrize(
    ("evidence", "field", "value"),
    [
        ([], "missing", 2),
        ([_evidence(success=False)], "missing", 2),
        ([_evidence()], "unknown_field", 2),
        ([_evidence()], "missing", 3),
    ],
)
def test_verify_rejects_false_citations(monkeypatch, evidence, field, value):
    monkeypatch.setattr("app.agent.audit.load_tool_evidence", lambda tool_ids, thread_id: {})

    grounded, violations = verify_citations(
        _diagnosis(field=field, value=value), evidence, "thread-1"
    )

    assert grounded is False
    assert violations


def test_verify_rejects_recent_evidence_from_another_thread(monkeypatch):
    foreign_evidence = {**_evidence(), "thread_id": "thread-2"}
    monkeypatch.setattr("app.agent.audit.load_tool_evidence", lambda tool_ids, thread_id: {})

    grounded, violations = verify_citations(
        _diagnosis(), [foreign_evidence], "thread-1"
    )

    assert grounded is False
    assert "不属于当前会话" in violations[0]


def test_verify_second_failure_removes_only_ungrounded_claims(monkeypatch):
    bad_tool_id = "22222222-2222-2222-2222-222222222222"
    diagnosis = _diagnosis()
    diagnosis["claims"].append({
        "claim": "不存在的工具声称 PACS 不可达",
        "evidence_refs": [{"tool_id": bad_tool_id, "field": "pacs_reachable", "value": False}],
    })
    monkeypatch.setattr("app.agent.audit.load_tool_evidence", lambda tool_ids, thread_id: {})
    state = {
        "diagnosis": diagnosis,
        "evidence": [_evidence()],
        "thread_id": "thread-1",
        "diagnose_attempts": 1,
        "errors": [],
    }

    patch = loop.verify_diagnosis(state)

    assert patch["citation_ok"] is True
    assert patch["diagnosis"]["confidence"] == "uncertain"
    assert patch["diagnosis"]["claims"] == [_diagnosis()["claims"][0]]


def test_citation_facts_load_evicted_evidence_from_mysql(monkeypatch):
    monkeypatch.setattr(
        "app.agent.audit.load_tool_evidence",
        lambda tool_ids, thread_id: {TOOL_ID: _evidence()} if thread_id == "thread-1" else {},
    )

    facts = build_citation_facts(_diagnosis(), [], "thread-1")

    assert facts == [{
        "claim": "完整性缺口为 2",
        "tool_id": TOOL_ID,
        "tool": "compute_integrity",
        "args": {"expected": 10, "local_unique_sop": 8, "level": "study"},
        "field": "missing",
        "value": 2,
    }]


class _ReflectionResult:
    def __init__(self, status):
        self.inference_status = status
        self.reason = "test"

    def model_dump(self):
        return {"inference_status": self.inference_status, "reason": self.reason}


class _ReflectionModel:
    def __init__(self, status=None, error=None):
        self.status = status
        self.error = error

    def invoke(self, prompt):
        if self.error:
            raise self.error
        return _ReflectionResult(self.status)


@pytest.mark.parametrize(
    ("status", "before", "after"),
    [
        ("supported", "confirmed", "confirmed"),
        ("overstated", "confirmed", "high"),
        ("overstated", "high", "uncertain"),
    ],
)
def test_reflect_maps_inference_status_to_confidence(monkeypatch, status, before, after):
    monkeypatch.setattr("app.agent.llm.llm_available", lambda: True)
    monkeypatch.setattr(
        "app.agent.llm.get_structured_model", lambda schema, **kwargs: _ReflectionModel(status=status)
    )
    state = {
        "diagnosis": _diagnosis(confidence=before),
        "evidence": [_evidence()],
        "thread_id": "thread-1",
    }

    patch = loop.reflect(state)

    assert patch["reflection"]["inference_status"] == status
    assert patch["diagnosis"]["confidence"] == after


def test_first_unsupported_reflection_requests_one_evidence_revision(monkeypatch):
    monkeypatch.setattr("app.agent.llm.llm_available", lambda: True)
    monkeypatch.setattr(
        "app.agent.llm.get_structured_model", lambda schema, **kwargs: _ReflectionModel(status="unsupported")
    )
    state = {
        "diagnosis": _diagnosis(confidence="confirmed"),
        "evidence": [_evidence()],
        "thread_id": "thread-1",
        "iteration": 2,
        "max_iterations": 8,
        "reflection_attempts": 0,
    }

    patch = loop.reflect(state)

    assert patch["diagnosis"]["confidence"] == "confirmed"
    assert patch["reflection_attempts"] == 1
    assert patch["status"] == "running"
    assert loop.route_after_reflect({**state, **patch}) == "reason"


def test_second_unsupported_reflection_stops_as_uncertain(monkeypatch):
    monkeypatch.setattr("app.agent.llm.llm_available", lambda: True)
    monkeypatch.setattr(
        "app.agent.llm.get_structured_model", lambda schema, **kwargs: _ReflectionModel(status="unsupported")
    )
    state = {
        "diagnosis": _diagnosis(confidence="confirmed"),
        "evidence": [_evidence()],
        "thread_id": "thread-1",
        "iteration": 3,
        "max_iterations": 8,
        "reflection_attempts": 1,
    }

    patch = loop.reflect(state)

    assert patch["diagnosis"]["confidence"] == "uncertain"
    assert patch["reflection_attempts"] == 2
    assert loop.route_after_reflect({**state, **patch}) == "plan_repull"


def test_reflect_failure_blocks_repull(monkeypatch):
    monkeypatch.setattr("app.agent.llm.llm_available", lambda: True)
    monkeypatch.setattr(
        "app.agent.llm.get_structured_model",
        lambda schema, **kwargs: _ReflectionModel(error=RuntimeError("model unavailable")),
    )
    state = {
        "diagnosis": _diagnosis(confidence="confirmed"),
        "evidence": [_evidence()],
        "thread_id": "thread-1",
    }

    reflected = loop.reflect(state)
    plan = loop.plan_repull({**state, **reflected})

    assert reflected["diagnosis"]["confidence"] == "uncertain"
    assert plan["repull_plan"] is None


def test_reflect_unavailable_keeps_uncertain_diagnosis_restricted(monkeypatch):
    monkeypatch.setattr("app.agent.llm.llm_available", lambda: False)
    state = {
        "diagnosis": _diagnosis(confidence="uncertain"),
        "evidence": [_evidence()],
        "thread_id": "thread-1",
    }

    patch = loop.reflect(state)

    assert patch["diagnosis"]["confidence"] == "uncertain"
    assert patch["reflection"]["inference_status"] == "unsupported"
