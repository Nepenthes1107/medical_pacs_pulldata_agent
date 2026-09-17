"""PACS business-command routes and legacy URL compatibility."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.application.run_commands import (
    abort_run as abort_run_command,
)
from src.application.run_commands import (
    approve_run,
    read_run,
)
from src.application.run_commands import (
    cancel_run as cancel_run_command,
)
from src.application.run_commands import (
    create_run as create_run_command,
)
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

# Loaded only by the legacy feature flag in app.api.main; the default PACS
# command router must not import the duplicate /agent protocol implementation.
legacy_router = None
router = APIRouter(prefix="/pacs", tags=["pacs-runs"])


@router.post("/runs", response_model=ChatResponse, status_code=202)
def create_run(request: ChatRequest, db: Session = Depends(get_db)):
    return create_run_command(request, db)


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
def get_run(run_id: str, db: Session = Depends(get_db)):
    return read_run(run_id, db)


@router.post("/runs/{run_id}/approval", response_model=ActionResponse)
def approval(run_id: str, request: ActionRequest, db: Session = Depends(get_db)):
    return approve_run(run_id, request, db)


@router.post("/runs/{run_id}/abort", response_model=AbortResponse)
def abort(run_id: str, request: AbortRequest = AbortRequest(), db: Session = Depends(get_db)):
    return abort_run_command(run_id, request, db)


@router.post("/runs/{run_id}/cancel", response_model=ActionResponse)
def cancel(run_id: str, request: CancelRequest = CancelRequest(), db: Session = Depends(get_db)):
    return cancel_run_command(run_id, request, db)


__all__ = ["legacy_router", "router"]
