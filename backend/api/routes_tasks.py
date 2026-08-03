from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.api.app_state import state
from backend.api.auth import require_auth
from backend.database.models import Task
from backend.database.session import get_session, list_all

router = APIRouter(prefix="/api/tasks", tags=["tasks"], dependencies=[Depends(require_auth)])

_ws_clients: set[WebSocket] = set()


class CreateTaskRequest(BaseModel):
    website: str
    goal: str
    wallet_label: Optional[str] = None
    notes: str = ""
    priority: int = 0
    scheduled_for: Optional[dt.datetime] = None


@router.post("")
async def create_task(req: CreateTaskRequest):
    task_id = await state.queue.enqueue(
        req.website, req.goal, req.wallet_label, req.notes, req.priority, req.scheduled_for
    )
    return {"id": task_id}


@router.get("")
async def list_tasks():
    tasks = await list_all(Task, order_by=Task.created_at.desc(), limit=200)
    return [
        {
            "id": t.id,
            "website": t.website,
            "goal": t.goal,
            "wallet_label": t.wallet_label,
            "status": t.status.value,
            "priority": t.priority,
            "retry_count": t.retry_count,
            "created_at": t.created_at.isoformat(),
            "scheduled_for": t.scheduled_for.isoformat() if t.scheduled_for else None,
        }
        for t in tasks
    ]


@router.get("/queue/status")
async def queue_status():
    return state.queue.queue_status()


@router.post("/queue/pause")
async def pause_queue():
    state.queue.pause()
    return {"worker_paused": True}


@router.post("/queue/resume")
async def resume_queue():
    state.queue.resume()
    return {"worker_paused": False}


@router.get("/{task_id}")
async def get_task(task_id: str):
    async with get_session() as session:
        result = await session.execute(
            select(Task).where(Task.id == task_id).options(selectinload(Task.steps))
        )
        task = result.scalar_one_or_none()
        if not task:
            return {"error": "not found"}
        return {
            "id": task.id,
            "website": task.website,
            "goal": task.goal,
            "status": task.status.value,
            "steps": [
                {"index": s.index, "action": s.action, "target": s.target_description, "success": s.success}
                for s in task.steps
            ],
        }


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    async with get_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            return {"error": "not found"}
    state.queue.cancel(task_id)
    return {"id": task_id, "cancel_requested": True}


@router.post("/{task_id}/pause")
async def pause_task(task_id: str):
    ok = state.queue.pause_task(task_id)
    if not ok:
        return {"error": "task is not currently running"}
    return {"id": task_id, "paused": True}


@router.post("/{task_id}/resume")
async def resume_task(task_id: str):
    ok = state.queue.resume_task(task_id)
    if not ok:
        return {"error": "task is not currently paused"}
    return {"id": task_id, "paused": False}


@router.post("/{task_id}/retry")
async def retry_task(task_id: str):
    ok = await state.queue.retry(task_id)
    if not ok:
        return {"error": "task not found, or not in a retryable (failed/cancelled) state"}
    return {"id": task_id, "requeued": True}


async def broadcast(message: str) -> None:
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


@router.websocket("/ws/live")
async def live_updates(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / ignored
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)
