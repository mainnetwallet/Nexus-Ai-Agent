"""
AI Chat API.

Persistent conversational sessions backed by ChatEngine (backend/planner/
chat_engine.py). One session = one continuous conversation the dashboard's
AI Chat page (or a Telegram chat) can send messages into; history survives
process restarts because it's stored in the same SQLite database as
everything else.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.app_state import state
from backend.api.auth import require_auth

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[Depends(require_auth)])


class SendMessageRequest(BaseModel):
    text: str


class CreateSessionRequest(BaseModel):
    channel: str = "dashboard"


def _session_dict(s) -> dict:
    return {
        "id": s.id,
        "channel": s.channel,
        "title": s.title,
        "last_task_id": s.last_task_id,
        "last_error": s.last_error,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _message_dict(m) -> dict:
    return {
        "id": m.id,
        "role": m.role.value if hasattr(m.role, "value") else m.role,
        "content": m.content,
        "category": m.category,
        "meta": m.meta_json,
        "created_at": m.created_at.isoformat(),
    }


@router.get("/sessions")
async def list_sessions():
    if state.chat is None:
        return {"error": "chat engine not initialized"}
    sessions = await state.chat.list_sessions()
    return [_session_dict(s) for s in sessions]


@router.post("/sessions")
async def create_session(req: CreateSessionRequest):
    if state.chat is None:
        return {"error": "chat engine not initialized"}
    session = await state.chat.get_or_create_session(None, channel=req.channel)
    return _session_dict(session)


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    if state.chat is None:
        return {"error": "chat engine not initialized"}
    messages = await state.chat.get_history(session_id)
    return [_message_dict(m) for m in messages]


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, req: SendMessageRequest):
    if state.chat is None:
        return {"error": "chat engine not initialized"}
    return await state.chat.send_message(session_id, req.text, channel="dashboard")


@router.delete("/sessions/{session_id}/messages")
async def clear_messages(session_id: str):
    if state.chat is None:
        return {"error": "chat engine not initialized"}
    await state.chat.clear_history(session_id)
    return {"ok": True}


@router.get("/sessions/{session_id}/export")
async def export_messages(session_id: str):
    if state.chat is None:
        return {"error": "chat engine not initialized"}
    messages = await state.chat.get_history(session_id)
    return {"session_id": session_id, "messages": [_message_dict(m) for m in messages]}
