import logging


logger = logging.getLogger(__name__)


def start_scheduler() -> None:
    """第一阶段预留入口：后续可按 StudyDate 定时 C-FIND 并创建补拉任务。"""
    logger.info("scheduler is not enabled in phase 1")


if __name__ == "__main__":
    start_scheduler()
