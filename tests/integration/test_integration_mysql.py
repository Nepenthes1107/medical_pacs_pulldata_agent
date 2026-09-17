"""真实 MySQL 集成测试（spec §9：MySQL 事务与幂等审计）。

conftest 用 root 建一次性库 + models create_all（以 models 为权威 schema），
覆盖：并发活跃 Run 冲突、`with_for_update` 行锁、幂等唯一约束、
tool evidence / execution log 落库与按 thread_id 的读取隔离。
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.infrastructure.db.models import (
    AgentActionAudit,
    AgentExecutionLog,
    AgentRun,
    DownloadTask,
)
from src.infrastructure.db.repositories import (
    ActionAuditRepository,
    RunRepository,
    TaskRepository,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.mysql,
]


def _uid(prefix: str) -> str:
    return "it_%s_%s" % (prefix, uuid.uuid4().hex[:12])


# ---------------------------------------------------------------- repositories


def test_run_repository_active_conflict(mysql_session):
    """同一 thread 有并发活跃 run 时取最新；终态 run 不参与活跃冲突。"""
    from datetime import datetime

    thread = _uid("thread")
    repo = RunRepository(mysql_session)
    base = datetime(2026, 1, 1, 0, 0, 0)
    mysql_session.add(AgentRun(run_id=_uid("r1"), thread_id=thread, status="awaiting_repull",
                               created_at=base))
    mysql_session.add(AgentRun(run_id=_uid("r2"), thread_id=thread, status="running",
                               created_at=datetime(2026, 1, 1, 0, 0, 2)))
    mysql_session.add(AgentRun(run_id=_uid("r3"), thread_id=thread, status="completed",
                               created_at=datetime(2026, 1, 1, 0, 0, 4)))
    mysql_session.commit()

    active = repo.active_for_thread(thread, ["running", "awaiting_repull"])
    assert active is not None and active.run_id.startswith("it_r2_")


def test_task_repository_locked_get(mysql_session):
    """locked_get 走 with_for_update，本事务内可读改自己的锁行。"""
    task_id = _uid("task")
    mysql_session.add(DownloadTask(task_id=task_id, study_instance_uid="1.2.3", status="in_queue"))
    mysql_session.commit()

    repo = TaskRepository(mysql_session)
    locked = repo.locked_get(task_id)
    assert locked is not None and locked.task_id == task_id
    locked.status = "downloading"
    mysql_session.commit()

    assert repo.get(task_id).status == "downloading"


def test_action_audit_unique_idempotency_key(mysql_session):
    """idempotency_key 唯一约束：重复写同一 key 抛 IntegrityError。"""
    key = _uid("key")
    repo = ActionAuditRepository(mysql_session)
    mysql_session.add(AgentActionAudit(
        run_id=_uid("run"), task_id=_uid("task"),
        action="approve", idempotency_key=key, result="ok",
    ))
    mysql_session.commit()
    assert repo.get_by_key(key) is not None

    mysql_session.add(AgentActionAudit(
        run_id=_uid("run"), task_id=_uid("task"),
        action="approve", idempotency_key=key, result="ok",
    ))
    with pytest.raises(IntegrityError):
        mysql_session.commit()
    mysql_session.rollback()


# ---------------------------------------------------------------- audit helpers


def test_record_and_load_tool_evidence_thread_isolation(mysql_audit_scope):
    """record_tool_evidence → load_tool_evidence 全链路落 MySQL，且按 thread 隔离。"""
    thread_a, thread_b = _uid("ta"), _uid("tb")
    run_a, run_b = _uid("ra"), _uid("rb")
    id_a, id_b = _uid("tid"), _uid("tid")

    audit = mysql_audit_scope
    audit.record_tool_evidence([
        {"tool_id": id_a, "run_id": run_a, "thread_id": thread_a,
         "tool": "query_study", "args": {"uid": "1.2.3"},
         "output": {"hits": 1}, "success": True},
        {"tool_id": id_b, "run_id": run_b, "thread_id": thread_b,
         "tool": "query_study", "args": {"uid": "9.9.9"},
         "output": {"hits": 2}, "success": True},
    ])

    # A 线程只读到自己的证据
    got_a = audit.load_tool_evidence([id_a, id_b], thread_a)
    assert set(got_a) == {id_a}
    assert got_a[id_a]["output"] == {"hits": 1}

    # B 线程读到自己的；用 A 的 id 去 B 线程查为空 → 隔离生效
    got_b = audit.load_tool_evidence([id_a], thread_b)
    assert got_b == {}


def test_record_execution_event_and_exception(mysql_audit_scope, mysql_engine):
    """execution log / exception 两条路径经 patch 后的 session_scope 落库。"""
    audit = mysql_audit_scope
    run_id, thread_id = _uid("run"), _uid("thread")
    audit.record_execution_event(run_id, thread_id, "diagnose", {
        "status": "ok", "diagnosis": {"confidence": 0.9, "claims": []},
    })
    audit.record_exception(run_id, thread_id, RuntimeError("boom"))

    # 用独立连接读回，验证确实写入一次性测试库
    factory = sessionmaker(bind=mysql_engine, future=True)
    db = factory()
    try:
        rows = (
            db.query(AgentExecutionLog)
            .filter(AgentExecutionLog.run_id == run_id, AgentExecutionLog.thread_id == thread_id)
            .order_by(AgentExecutionLog.id)
            .all()
        )
    finally:
        db.close()

    assert len(rows) == 2
    assert {row.event_type for row in rows} == {"node", "exception"}
    exc_row = next(row for row in rows if row.event_type == "exception")
    assert exc_row.error == "boom"
