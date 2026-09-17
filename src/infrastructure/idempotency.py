"""Redis idempotency lock adapter for approved write actions."""

import redis

from src.core.settings import settings


class IdempotencyLock:
    def __init__(self, ttl_seconds: int | None = None):
        self.ttl_seconds = ttl_seconds or settings.agent.approval_lock_ttl_seconds

    def acquire(self, key: str, owner: str) -> bool:
        client = redis.Redis.from_url(settings.redis.url)
        return bool(client.set("idempotency:%s" % key, owner, nx=True, ex=self.ttl_seconds))


idempotency_lock = IdempotencyLock()
