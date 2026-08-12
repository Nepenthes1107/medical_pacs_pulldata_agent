import json
from contextlib import contextmanager
from typing import Dict

import pika

from app.core.config import settings


@contextmanager
def rabbitmq_channel():
    parameters = pika.URLParameters(settings.rabbitmq.url)
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    try:
        # 现在支持两个队列：下载任务队列 + Agent Run 队列。
        channel.queue_declare(queue=settings.rabbitmq.download_queue, durable=True)
        channel.queue_declare(queue=settings.agent.agent_runs_queue, durable=True)
        channel.queue_declare(queue=settings.agent.repull_events_queue, durable=True)
        yield channel
    finally:
        connection.close()


def _publish(routing_key: str, message: Dict) -> None:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    with rabbitmq_channel() as channel:
        channel.basic_publish(
            exchange="",
            routing_key=routing_key,
            body=body,
            properties=pika.BasicProperties(delivery_mode=2),
        )


def publish_download_task(message: Dict) -> None:
    _publish(settings.rabbitmq.download_queue, message)


def publish_agent_run(run_id: str) -> None:
    """向 agent_runs 队列投递 run_id（Agent Worker 消费执行诊断图）。"""
    _publish(settings.agent.agent_runs_queue, {"run_id": run_id})

_FAILURE_STAGES = {"move", "integrity"}


def publish_repull_failure(task_id: str, failure_stage: str) -> None:
    """补拉失败事件（Checker 单一裁判改造后）：只有失败才发消息，success/unverified 不入队。

    failure_stage 只能是 move（Downloader C-MOVE 失败）或 integrity（Checker 超时仍不完整）。
    不携带期望数/本地数/错误详情——这些以数据库中的 DownloadTask/ArchiveModel 为唯一事实来源。
    发事件失败不阻断 Worker 主流程（best-effort）。
    """
    if failure_stage not in _FAILURE_STAGES:
        raise ValueError("invalid failure_stage: %s" % failure_stage)
    _publish(settings.agent.repull_events_queue,
             {"task_id": task_id, "terminal_status": "fail", "failure_stage": failure_stage})
