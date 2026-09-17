"""Compatibility exports for the infrastructure RabbitMQ adapter."""
from src.infrastructure.rabbitmq import (
    channel as rabbitmq_channel,
)
from src.infrastructure.rabbitmq import (
    publish_agent_run,
    publish_download_task,
    publish_repull_failure,
)

__all__ = ["rabbitmq_channel", "publish_agent_run", "publish_download_task", "publish_repull_failure"]
