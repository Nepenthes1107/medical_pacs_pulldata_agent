"""Generic invoke/stream/info routes for registered LangGraph agents.

This is deliberately an adapter around the existing graph. PACS-specific run,
approval and abort routes stay in ``app.api.routes_agent`` during migration.
"""
import asyncio
import json
import threading
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from src.agents.pacs.graph import get_checkpointer
from src.agents.registry import AGENTS, get_agent, get_all_agent_info
from src.schema.api import (
    AgentInfo,
    AgentInvokeRequest,
    AgentInvokeResponse,
    ThreadRequest,
    ThreadSummary,
)

router = APIRouter(prefix="/agents", tags=["agents"])


def _require_agent(agent_id: str):
    """Resolve an agent through the single registry and normalize 404s."""
    try:
        return get_agent(agent_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/info", response_model=list[AgentInfo])
def agent_info() -> list[AgentInfo]:
    return [AgentInfo(**item) for item in get_all_agent_info()]


def _invoke(agent_id: str, request: AgentInvokeRequest) -> tuple[str, dict[str, Any]]:
    thread_id = request.thread_id or str(uuid4())
    try:
        checkpointer = get_checkpointer()
        try:
            graph = get_agent(agent_id, checkpointer=checkpointer)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        from src.agents.pacs.graph import initial_state

        initial = initial_state(
            request.message,
            request.task_id,
            request.study_instance_uid,
            request.series_instance_uid,
            request.source_id,
            str(uuid4()),
            thread_id,
            None,
            request.intent,
        )
        state = graph.invoke(
            initial,
            config={
                "configurable": {"thread_id": thread_id},
                "metadata": {"user_id": request.user_id} if request.user_id else {},
            },
        )
        return thread_id, dict(state)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{agent_id}/invoke", response_model=AgentInvokeResponse)
async def invoke(request: AgentInvokeRequest, agent_id: str = "pacs-diagnostician") -> AgentInvokeResponse:
    # Graph execution is synchronous because the PACS/DIMSE adapters are blocking.
    # Keep the HTTP boundary async, and move the blocking graph call off the event loop.
    thread_id, state = await asyncio.to_thread(_invoke, agent_id, request)
    return AgentInvokeResponse(agent_id=agent_id, thread_id=thread_id, state=state)


@router.post("/{agent_id}/stream")
async def stream(request: AgentInvokeRequest, agent_id: str = "pacs-diagnostician") -> StreamingResponse:
    thread_id = request.thread_id or str(uuid4())
    # Validate before returning a streaming response so unknown agents produce a
    # normal HTTP 404 rather than an SSE error event with status 200.
    _require_agent(agent_id)
    definition = AGENTS[agent_id]
    if definition.stream_factory is None:
        raise HTTPException(status_code=501, detail=f"agent {agent_id} does not support streaming")

    async def events():
        # Adapt the synchronous LangGraph iterator to an async SSE stream without
        # buffering the complete run. The graph remains the sole producer of events.
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()
        loop = asyncio.get_running_loop()

        def produce() -> None:
            factory = definition.stream_factory
            if factory is None:  # 外层已 501；此处防御性退出，避免闭包内 None 可调用
                return
            try:
                for update in factory(
                    message=request.message, task_id=request.task_id,
                    study_instance_uid=request.study_instance_uid,
                    series_instance_uid=request.series_instance_uid,
                    source_id=request.source_id, run_id=str(uuid4()), thread_id=thread_id,
                    intent=request.intent, user_id=request.user_id,
                ):
                    asyncio.run_coroutine_threadsafe(queue.put(update), loop).result()
            except Exception as exc:  # pragma: no cover - transport boundary
                asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(sentinel), loop).result()

        worker = threading.Thread(target=produce, daemon=True)
        worker.start()
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                if isinstance(item, Exception):
                    yield f"event: error\ndata: {json.dumps({'detail': str(item)}, ensure_ascii=False)}\n\n"
                    break
                yield f"data: {json.dumps(item, default=str, ensure_ascii=False)}\n\n"
        finally:
            yield "event: done\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/{agent_id}/history")
def history(request: ThreadRequest, agent_id: str = "pacs-diagnostician") -> dict[str, Any]:
    """Read messages from the LangGraph checkpoint for one thread."""
    _require_agent(agent_id)
    try:
        checkpoint = get_checkpointer().get_tuple(
            {"configurable": {"thread_id": request.thread_id}}
        )
    except Exception as exc:  # do not silently fall back to process memory
        raise HTTPException(status_code=503, detail="checkpoint store unavailable") from exc
    if checkpoint is None:
        return {"agent_id": agent_id, "thread_id": request.thread_id, "messages": []}
    if request.user_id:
        owner = (checkpoint.metadata or {}).get("user_id")
        if owner != request.user_id:
            raise HTTPException(status_code=404, detail="thread not found")
    messages = []
    for message in (checkpoint.checkpoint.get("channel_values") or {}).get("messages", []):
        messages.append(message.model_dump() if hasattr(message, "model_dump") else str(message))
    return {"agent_id": agent_id, "thread_id": request.thread_id, "messages": messages}


@router.get("/{agent_id}/threads", response_model=list[ThreadSummary])
def threads(agent_id: str = "pacs-diagnostician", user_id: str | None = None, limit: int = 50) -> list[ThreadSummary]:
    """List checkpoint heads without maintaining a second thread table."""
    _require_agent(agent_id)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    try:
        rows = list(get_checkpointer().list(None, limit=limit))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="checkpoint store unavailable") from exc
    seen: set[str] = set()
    result: list[ThreadSummary] = []
    for row in rows:
        thread_id = row.config.get("configurable", {}).get("thread_id")
        if not thread_id or thread_id in seen:
            continue
        if user_id and (row.metadata or {}).get("user_id") != user_id:
            continue
        seen.add(thread_id)
        messages = (row.checkpoint.get("channel_values") or {}).get("messages", [])
        first = next((item for item in messages if getattr(item, "type", None) == "human"), None)
        content = getattr(first, "content", None)
        result.append(
            ThreadSummary(
                thread_id=thread_id,
                updated_at=row.checkpoint.get("ts"),
                title=str(content)[:60] if content else None,
            )
        )
    return result
