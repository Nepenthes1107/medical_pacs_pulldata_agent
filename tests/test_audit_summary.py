from app.agent.audit import build_execution_summary


def test_act_audit_summary_keeps_only_tool_references_and_status():
    update = {
        "iteration": 2,
        "last_tool_ids": ["tool-1"],
        "tool_results": {
            "compute_integrity": {
                "tool_id": "tool-1",
                "success": True,
                "missing": 2,
                "private_output": "must-not-be-copied",
            }
        },
        "evidence": [{
            "tool_id": "tool-1",
            "args": {"expected": 10},
            "output": {"missing": 2},
        }],
    }

    summary = build_execution_summary("act", update)

    assert summary == {
        "iteration": 2,
        "tool_ids": ["tool-1"],
        "tool_status": [{
            "tool_id": "tool-1",
            "tool": "compute_integrity",
            "success": True,
        }],
    }
    serialized = str(summary)
    assert "private_output" not in serialized
    assert "expected" not in serialized
    assert "missing" not in serialized


def test_non_tool_node_audit_summary_does_not_copy_full_diagnosis():
    summary = build_execution_summary("diagnose", {
        "status": "completed",
        "diagnosis": {
            "confidence": "high",
            "root_cause": "large root cause text",
            "claims": [{"claim": "fact", "evidence_refs": []}],
        },
    })

    assert summary == {
        "status": "completed",
        "confidence": "high",
        "claim_count": 1,
    }
