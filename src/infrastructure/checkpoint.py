"""LangGraph checkpoint adapter.

Redis/RedisJSON details stay outside the PACS graph. The adapter preserves the
existing setup and TTL semantics required for interrupt/resume approvals.
"""
from typing import Any

from src.core.settings import settings


def create_checkpointer() -> Any:
    from langgraph.checkpoint.redis import RedisSaver

    ttl = {
        "default_ttl": settings.agent.checkpoint_ttl_minutes,
        "refresh_on_read": True,
    }
    try:
        saver: Any = RedisSaver(redis_url=settings.redis.url, ttl=ttl)
    except TypeError:
        # 老版 langgraph-checkpoint-redis 的 from_conn_string 签名不同（构造/迭代器），
        # 此处仅做版本自适应兜底，类型以 Any 表达、不绑定具体返回形态。
        saver = RedisSaver.from_conn_string(settings.redis.url, ttl=ttl)
    saver.setup()
    return saver
