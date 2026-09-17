"""RabbitMQ transport adapter for durable application events."""
import json
from contextlib import contextmanager

import pika

from src.core.settings import settings


@contextmanager
def channel():
    connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq.url))
    ch = connection.channel()
    try:
        ch.queue_declare(queue=settings.rabbitmq.download_queue, durable=True)
        ch.queue_declare(queue=settings.agent.agent_runs_queue, durable=True)
        ch.queue_declare(queue=settings.agent.repull_events_queue, durable=True)
        yield ch
    finally:
        connection.close()


def _publish(routing_key: str, message: dict) -> None:
    with channel() as ch:
        ch.basic_publish(
            exchange="", routing_key=routing_key,
            body=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            properties=pika.BasicProperties(delivery_mode=2),
        )


def publish_download_task(message: dict) -> None:
    _publish(settings.rabbitmq.download_queue, message)


def publish_agent_run(run_id: str) -> None:
    _publish(settings.agent.agent_runs_queue, {"run_id": run_id})


def publish_repull_failure(task_id: str, failure_stage: str) -> None:
    if failure_stage not in {"move", "integrity"}:
        raise ValueError("invalid failure_stage: %s" % failure_stage)
    _publish(settings.agent.repull_events_queue, {
        "task_id": task_id, "terminal_status": "fail", "failure_stage": failure_stage,
    })
