from app.agent.batch_orchestrator import aggregate_batch, normalize_study_uids


def test_batch_aggregation_reports_partial_failure():
    result = aggregate_batch({
        "s1": {"status": "completed", "retry_count": 0},
        "s2": {"status": "failed", "retry_count": 2},
    }, ["s1", "s2"])
    assert result["status"] == "partial_failed"
    assert result["batch_summary"] == {
        "total": 2, "completed": 1, "failed": 1,
        "awaiting_approval": 0, "awaiting_repull": 0,
        "retried": 1, "partial_failure": True,
    }


def test_normalize_study_uids_deduplicates_and_preserves_order():
    assert normalize_study_uids(["s1", "s2", "s1"]) == ["s1", "s2"]
