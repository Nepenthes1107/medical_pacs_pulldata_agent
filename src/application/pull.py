"""Application boundary for creating pull tasks."""
from src.application import pull_service as _pull
from src.infrastructure.db.session import session_scope

PullError = _pull.PullError


def create_pull_task(request, db):
    return _pull.create_pull_task(request, db)


def create_first_pull(request):
    with session_scope() as db:
        task = create_pull_task(request, db)
        return task.task_id, task.status


__all__ = ["PullError", "create_first_pull", "create_pull_task"]
