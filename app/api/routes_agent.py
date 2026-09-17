"""Deprecated `/agent/*` compatibility routes.

The default service is registered from ``src.service``.  These routes retain
the old URL contract only when ``LEGACY_AGENT_API_ENABLED=true`` and delegate
Run commands to the canonical application service.
"""
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from src.application.run_commands import (
    abort_run,
    approve_run,
    cancel_run,
    create_run,
    get_run_or_404,
    read_run,
)
from src.infrastructure import events as ev
from src.infrastructure.db.session import get_db
from src.schema.pacs import (
    AbortRequest,
    AbortResponse,
    ActionRequest,
    ActionResponse,
    CancelRequest,
    ChatRequest,
    ChatResponse,
    RunStatusResponse,
)

router = APIRouter(prefix="/agent", tags=["agent"], deprecated=True)
_TERMINAL = {"completed", "partial_failed", "failed", "cancelled", "aborted", "rejected"}


@router.post("/chat", response_model=ChatResponse, status_code=status.HTTP_202_ACCEPTED)
def agent_chat(request: ChatRequest, db: Session = Depends(get_db)):
    return create_run(request, db)


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
def get_run(run_id: str, db: Session = Depends(get_db)):
    return read_run(run_id, db)


@router.post("/runs/{run_id}/action", response_model=ActionResponse)
def run_action(run_id: str, request: ActionRequest, db: Session = Depends(get_db)):
    return approve_run(run_id, request, db)


@router.post("/runs/{run_id}/abort", response_model=AbortResponse)
def abort_legacy(run_id: str, request: AbortRequest = AbortRequest(), db: Session = Depends(get_db)):
    return abort_run(run_id, request, db)


@router.post("/runs/{run_id}/cancel", response_model=ActionResponse)
def cancel_legacy(run_id: str, request: CancelRequest = CancelRequest(), db: Session = Depends(get_db)):
    return cancel_run(run_id, request, db)


@router.get("/runs/{run_id}/stream")
def stream_run(request: Request, run_id: str, user_id: str | None = None,
               db: Session = Depends(get_db)):
    """Deprecated SSE stream; use `/agents/{agent_id}/stream`."""
    run = get_run_or_404(db, run_id)
    if run.user_id and run.user_id != user_id:
        raise HTTPException(status_code=403, detail="run does not belong to this user")
    try:
        offset = int(request.headers.get("last-event-id", "-1")) + 1
    except ValueError:
        offset = 0

    def events():
        nonlocal offset
        while True:
            batch = ev.read_events(run_id, offset)
            for event in batch:
                if ev.is_done_event(event):
                    yield "event: done\ndata: {}\n\n"
                    return
                yield "id: %d\ndata: %s\n\n" % (offset, json.dumps(event, ensure_ascii=False))
                offset += 1
            if not batch:
                db.refresh(run)
                if run.status in _TERMINAL:
                    yield "event: done\ndata: {}\n\n"
                    return
                yield ": heartbeat\n\n"
            time.sleep(0.5)

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        "Deprecation": "true", "X-Agent-Service-Replacement": "/agents/pacs-diagnostician/stream",
    })


# Historical names retained for integrations that imported route callables.
abort_run_legacy = abort_legacy
cancel_run_legacy = cancel_legacy

__all__ = ["abort_legacy", "agent_chat", "cancel_legacy", "get_run", "router", "run_action", "stream_run"]
