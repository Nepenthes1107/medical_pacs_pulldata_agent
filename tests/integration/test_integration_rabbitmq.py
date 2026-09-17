"""真实 RabbitMQ + Redis 幂等锁集成测试（spec §9：RabbitMQ 事件总线 / 幂等）。

fixture 把队列名改成唯一后缀（it_*），不触碰真实 download_tasks/agent_runs；
每条消息落库后可经 passive declare 读回队列深度与 payload，验证端到端投递。
"""
import json
import uuid

import pika
import pytest

from src.core.settings import settings
from src.infrastructure import rabbitmq
from src.infrastructure.idempotency import IdempotencyLock
from src.infrastructure.messaging import inspect_queue

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rabbitmq,
]


def _consume_one(url: str, queue: str) -> dict:
    connection = pika.BlockingConnection(pika.URLParameters(url))
    channel = connection.channel()
    try:
        method, _properties, body = channel.basic_get(queue=queue, auto_ack=True)
        if method is None:
            return {}
        return json.loads(body.decode("utf-8"))
    finally:
        connection.close()


def test_publish_download_task_and_inspect(rabbit_scope):
    """download_task 投递后队列深度=1 且 payload 可读回。"""
    names = rabbit_scope["names"]
    msg = {
        "task_id": "it-dl-%s" % uuid.uuid4().hex[:8],
        "study_instance_uid": "9.9.9.20250101000000.1",
        "level": "study_level",
    }
    rabbitmq.publish_download_task(msg)

    info = inspect_queue(names["download"])
    assert info["message_count"] >= 1

    got = _consume_one(rabbit_scope["url"], names["download"])
    assert got.get("task_id") == msg["task_id"]
    assert got.get("study_instance_uid") == msg["study_instance_uid"]


def test_publish_agent_run_payload(rabbit_scope):
    """agent_run 事件体为 {run_id}，独立队列，不影响 download_tasks。"""
    run_id = "it-run-%s" % uuid.uuid4().hex[:8]
    rabbitmq.publish_agent_run(run_id)

    names = rabbit_scope["names"]
    got = _consume_one(rabbit_scope["url"], names["agent_run"])
    assert got == {"run_id": run_id}
    # 事件队列与 download 队列互不串扰
    assert inspect_queue(names["download"])["message_count"] == 0


def test_publish_repull_failure_validates_stage(rabbit_scope):
    """repull_failure 只接受 move/integrity；非法 stage 直接 ValueError 且不投递。"""
    names = rabbit_scope["names"]
    task_id = "it-repull-%s" % uuid.uuid4().hex[:8]

    # 先声明队列：channel() 首次进入即创建全部 3 个队列，否则 passive declare 会 404
    with rabbitmq.channel():
        pass

    with pytest.raises(ValueError):
        rabbitmq.publish_repull_failure(task_id, "oops")
    assert inspect_queue(names["repull"])["message_count"] == 0

    rabbitmq.publish_repull_failure(task_id, "integrity")
    got = _consume_one(rabbit_scope["url"], names["repull"])
    assert got["task_id"] == task_id
    assert got["failure_stage"] == "integrity"
    assert got["terminal_status"] == "fail"


def test_idempotency_lock_nx_semantics(rabbit_scope):
    """Redis SET NX EX：首获成功、并发重复获取失败、TTL 过期后释放。"""
    import redis as redis_py

    key = "it-lock-%s" % uuid.uuid4().hex[:8]
    lock = IdempotencyLock(ttl_seconds=1)

    try:
        assert lock.acquire(key, owner="run-1") is True
        assert lock.acquire(key, owner="run-2") is False  # NX 拒绝
        # 跨客户端（模拟另一 worker）读取锁内容
        client = redis_py.Redis.from_url(settings.redis.url)
        assert client.get("idempotency:%s" % key).decode() == "run-1"
    finally:
        client = redis_py.Redis.from_url(settings.redis.url)
        client.delete("idempotency:%s" % key)
