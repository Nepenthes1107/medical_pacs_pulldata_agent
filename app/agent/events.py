"""过程事件流（需求 4）：把每轮循环的语义节点作为过程事件推给前端。

承载用 Redis list per run（带 TTL），SSE 端点轮询该列表推给前端。
非 token 级——事件粒度是「Agent 在查什么、发现什么」的推理轨迹（plan-autonomous-v2 §4.2），
不破坏结构化输出与护栏。Redis 不可用时降级为 no-op（流不可用不阻断诊断主流程）。
"""
import json
import logging
from typing import Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

_EVENT_TTL_SECONDS = 3600  # Run 结束后事件留存 1 小时供回看
_DONE = "__done__"


def _key(run_id: str) -> str:
    return "run_events:%s" % run_id


def _client():
    import redis

    return redis.Redis.from_url(settings.redis.url)


def publish_event(run_id: str, event: Dict) -> None:
    """追加一条过程事件到该 run 的 Redis list（best-effort）。"""
    try:
        client = _client()
        key = _key(run_id)
        client.rpush(key, json.dumps(event, ensure_ascii=False))
        client.expire(key, _EVENT_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("过程事件发布失败（流降级，不阻断）: %s", exc)


def mark_done(run_id: str) -> None:
    """标记事件流结束，SSE 端点据此收尾。"""
    publish_event(run_id, {"type": _DONE})


def read_events(run_id: str, offset: int = 0) -> List[Dict]:
    """读取 offset 之后的事件（SSE 端点轮询用）。Redis 不可用返回空。"""
    try:
        client = _client()
        raw = client.lrange(_key(run_id), offset, -1)
        return [json.loads(r) for r in raw]
    except Exception as exc:  # noqa: BLE001
        logger.debug("过程事件读取失败: %s", exc)
        return []


def is_done_event(event: Dict) -> bool:
    return event.get("type") == _DONE


def node_to_event(node_name: str, update: Dict, degraded: bool = False) -> Optional[Dict]:
    """把一个 LangGraph 节点增量转成过程事件（plan §4.2 事件粒度）。

    返回 None 表示该节点无需推送。degraded=True 时标注「受限」轨迹（plan §4.5，不伪装）。
    """
    ev: Dict = {"type": "node", "node": node_name, "degraded": degraded}
    if node_name == "reason":
        ev["hypothesis"] = update.get("hypothesis")
        calls = update.get("pending_tool_calls") or []
        ev["next_tools"] = [c.get("name") for c in calls]
        ev["converged"] = update.get("converged", False)
    elif node_name == "act":
        tr = update.get("tool_results") or {}
        ev["observed"] = {name: {"success": out.get("success")} for name, out in tr.items()}
    elif node_name == "diagnose":
        diag = update.get("diagnosis") or {}
        ev["summary"] = diag.get("summary")
        ev["confidence"] = diag.get("confidence")
    elif node_name == "reflect":
        ev["reflection"] = update.get("reflection")
    elif node_name == "plan_repull":
        plan = update.get("repull_plan")
        ev["repull_plan"] = plan
        ev["status"] = update.get("status")
    elif node_name in ("first_pull", "retrieve_and_answer", "present_clarification"):
        diag = update.get("diagnosis") or {}
        ev["summary"] = diag.get("summary")
    elif node_name == "execute":
        ar = update.get("action_result") or {}
        ev["success"] = ar.get("success")
        ev["strategy"] = ar.get("strategy")
        ev["submitted"] = ar.get("submitted")
        ev["error"] = ar.get("error")
    else:
        return None
    return ev
