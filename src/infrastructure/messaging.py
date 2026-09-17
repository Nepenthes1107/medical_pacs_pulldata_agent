"""Message-bus adapter used by service and workers."""
from typing import Any

from src.core.settings import settings
from src.infrastructure.rabbitmq import (
    publish_agent_run,
    publish_download_task,
    publish_repull_failure,
)


class RabbitPublisher:
    """Stable application-facing publisher facade.

    Queue names, durable delivery and connection lifecycle stay in the legacy
    adapter until the worker migration is complete.
    """

    def agent_run(self, run_id: str) -> None:
        publish_agent_run(run_id)

    def download_task(self, message: dict[str, Any]) -> None:
        publish_download_task(message)

    def repull_failure(self, task_id: str, failure_stage: str) -> None:
        publish_repull_failure(task_id, failure_stage)


publisher = RabbitPublisher()


def inspect_queue(queue_name: str) -> dict[str, Any]:
    """Read queue depth/consumer count without creating or publishing messages."""
    import pika

    connection = None
    try:
        connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq.url))
        channel = connection.channel()
        method = channel.queue_declare(queue=queue_name, durable=True, passive=True)
        return {
            "queue_name": queue_name,
            "message_count": method.method.message_count,
            "consumer_count": method.method.consumer_count,
        }
    finally:
        if connection and connection.is_open:
            connection.close()
