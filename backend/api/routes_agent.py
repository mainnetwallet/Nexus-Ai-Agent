"""
Autonomous Agent Runtime API.

Control surface for the agent as a whole (Start/Stop/Pause/Resume), plus a
consolidated status view for the dashboard's Agent page: runtime status,
current task/action/reasoning, browser state (via the existing live
session), active wallet, and runtime statistics. A WebSocket streams the
same activity events that update that status, for live monitoring.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from backend.api.app_state import state
from backend.api.auth import require_auth

router = APIRouter(prefix="/api/agent", tags=["agent"], dependencies=[Depends(require_auth)])

_ws_clients: set[WebSocket] = set()


@router.post("/start")
async def start_agent():
    if state.agent is None:
        return {"error": "agent runtime not initialized"}
    return await state.agent.start()


@router.post("/stop")
async def stop_agent():
    if state.agent is None:
        return {"error": "agent runtime not initialized"}
    return await state.agent.stop()


@router.post("/pause")
async def pause_agent():
    if state.agent is None:
        return {"error": "agent runtime not initialized"}
    return await state.agent.pause()


@router.post("/resume")
async def resume_agent():
    if state.agent is None:
        return {"error": "agent runtime not initialized"}
    return await state.agent.resume()


@router.get("/status")
async def agent_status():
    """
    Consolidated view for the dashboard's Agent page. Composes AgentRuntime's
    own status with the existing live browser session and active-wallet
    lookups rather than duplicating that state.
    """
    if state.agent is None:
        return {"error": "agent runtime not initialized"}

    payload = await state.agent.status()

    if state.live_session is not None:
        browser = state.live_session.status()
        payload["browser"] = {
            "active": browser.get("active", False),
            "url": browser.get("url", ""),
            "title": browser.get("title", ""),
        }
    else:
        payload["browser"] = {"active": False, "url": "", "title": ""}

    if state.wallet_registry is not None:
        active_wallet = await state.wallet_registry.get_active_wallet()
        payload["active_wallet"] = active_wallet
    else:
        payload["active_wallet"] = None

    return payload


async def broadcast(payload: dict) -> None:
    import json

    text = json.dumps(payload)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


@router.websocket("/ws/live")
async def agent_live_stream(websocket: WebSocket):
    """Pushes each structured activity event (task_start/step/task_finish/task_crash) as it happens."""
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / ignored
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)
