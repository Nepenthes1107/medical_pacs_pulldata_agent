"""人工审批闸门（护栏 4）：canonical LangGraph HITL interrupt/replay。

拓扑：plan_repull → human_approval →(条件边)→ {execute → END | END}

- human_approval：有补拉计划则 interrupt() 暂停，等 API 侧 Command(resume=审批结果) 恢复；
  无计划直接放行到 END（无写操作可审批）。
- execute：仅在 approved 时执行——调**图外**的 execute_repull_plan（写工具不进白名单、
  不绑定 LLM，护栏 4 铁律不变）；写操作的 Redis 幂等锁/审计/投队列全在该函数内，
  也是 replay 重复执行的天然防线。

控制权分层：LLM 只产计划（plan_repull），批不批是人（interrupt），执行是固定节点（execute）。
"""
import logging
from typing import cast

from src.agents.pacs.state import AgentState

logger = logging.getLogger(__name__)


def human_approval(state: AgentState) -> dict:
    """审批闸门：有补拉计划 → interrupt 暂停等人工；无计划 → 放行完成。

    interrupt 的 payload 是给人看的补拉计划摘要；恢复值是 {"action", "operator"}。
    resume 后把审批结论写进 state，交条件边分派到 execute 或 END。
    """
    plan = state.get("repull_plan")
    if not plan:
        # 无写操作可审批（uncertain 弃权 / 无 LLM / 无需补拉）→ 直接完成。
        return {"status": "completed", "approval_status": None}

    from langgraph.types import interrupt

    from src.agents.pacs.schemas import derive_repull_level

    plan_scope = plan.get("scope")
    scope = plan_scope if isinstance(plan_scope, dict) else {}
    # interrupt 暂停：payload 供前端/审批人查看；返回值即 Command(resume=...) 传入的审批结果。
    # level 现算——审批人要凭它判断这次动多大范围，必须与实际执行同一份推导。
    decision = interrupt({
        "kind": "repull_approval",
        "run_id": state.get("run_id"),
        "task_id": state.get("task_id"),
        "level": derive_repull_level(scope.get("missing_targets") or []),
        "repull_plan": plan,
    })
    decision = decision or {}
    action = str(decision.get("action", "")).lower()
    operator = decision.get("operator")

    if action == "approve":
        return {"approval_status": "approved", "operator": operator}
    # reject 或未知动作一律按拒绝处理（安全默认：不执行写操作）。
    return {"approval_status": "rejected", "operator": operator, "status": "rejected"}


def route_after_approval(state: AgentState) -> str:
    """approved → execute 执行写操作；否则（rejected/无计划）→ END。"""
    return "execute" if state.get("approval_status") == "approved" else "__end__"


def execute(state: AgentState) -> dict:
    """审批通过后执行补拉计划（唯一触发写操作的固定节点，护栏 4）。

    调图外 execute_repull_plan（零改动复用）：Redis 幂等锁 → 写审计 → 投递补拉队列。
    幂等键 目标:strategy:范围指纹——不含 run_id，故跨 Run（多 thread 诊断同一 task）
    对同一目标同策略同范围的重复写会被同一把锁挡住；replay 重跑本节点同样命中。
    范围进键的原因：同一 task 先补 Series A、重新诊断后再补 Series B 是两次不同的合法写操作。

    定向补拉会新建独立任务，此时必须把新 task_id 写回 state——否则 Checker 收尾与补拉失败
    事件都按 task_id 找不到 awaiting_repull 的 Run，收尾链会断。
    """
    from src.agents.pacs.tools import execute_repull_plan, scope_fingerprint

    plan = state.get("repull_plan") or {}
    run_id = state.get("run_id")
    task_id = state.get("task_id")
    strategy = plan.get("strategy", "escalate")
    plan_scope = plan.get("scope")
    scope = plan_scope if isinstance(plan_scope, dict) else {}
    missing_targets = scope.get("missing_targets", []) or []
    idempotency_key = "%s:%s:%s" % (
        task_id or state.get("study_instance_uid") or "", strategy,
        scope_fingerprint(missing_targets),
    )

    result = execute_repull_plan(
        run_id=cast(str, run_id), task_id=task_id, strategy=strategy, idempotency_key=idempotency_key,
        missing_targets=missing_targets,
        study_instance_uid=state.get("study_instance_uid"), source_id=state.get("source_id"),
        operator=state.get("operator"),
    )
    action_result = {
        "success": result.success, "strategy": result.strategy, "new_status": result.new_status,
        "submitted": result.submitted, "degraded": result.degraded,
        "created_task": result.created_task, "level": result.level, "note": result.note,
        "task_id": result.task_id,
        "audit_log_id": result.audit_log_id, "error": result.error,
    }
    # submitted（retry_task/targeted_cmove）→ awaiting_repull 等 Checker 验收；escalate → completed。
    if not result.success:
        status = "failed"
    elif result.submitted:
        status = "awaiting_repull"
    else:
        status = "completed"
    patch: dict = {"action_result": action_result, "status": status}
    # 新建定向任务 → Run 改指向新任务，Checker 收尾与失败事件据此关联。
    if result.created_task and result.task_id:
        patch["task_id"] = result.task_id
    return patch

