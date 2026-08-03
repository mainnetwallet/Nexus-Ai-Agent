"""
Live Browser Session API.

Read-only observability into whatever website the agent is currently
operating on -- status polling, a single-shot screenshot, and a WebSocket
stream of screenshots as they're captured. Does not expose any control
surface over the browser itself (no click/type/navigate here); that stays
entirely inside the planner/agent_loop.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response, WebSocket, WebSocketDisconnect

from backend.api.app_state import state
from backend.api.auth import require_auth

router = APIRouter(prefix="/api/browser", tags=["browser"], dependencies=[Depends(require_auth)])


@router.get("/status")
async def browser_status():
    """Current live-session status: whether a browser is active, which task
    owns it, the page it's on, and how many clients are watching."""
    if state.live_session is None:
        return {"active": False, "error": "live session not initialized"}
    return state.live_session.status()


@router.get("/screenshot")
async def browser_screenshot():
    """Latest captured screenshot as a raw JPEG. 404-equivalent (empty 204)
    if nothing has been captured yet (e.g. no task has run since startup)."""
    if state.live_session is None:
        return Response(status_code=503, content=b"live session not initialized")
    frame = state.live_session.latest_screenshot_bytes()
    if frame is None:
        return Response(status_code=204)
    return Response(content=frame, media_type="image/jpeg")


@router.websocket("/ws/live")
async def browser_live_stream(websocket: WebSocket):
    """Push-based live view: emits a JSON frame (base64 JPEG + url/title/
    task metadata) every time the live session captures the page, plus an
    `{"type": "idle"}` message whenever the active task ends."""
    if state.live_session is None:
        await websocket.close(code=1013)  # try again later
        return

    await state.live_session.register(websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / ignored
    except WebSocketDisconnect:
        state.live_session.unregister(websocket)
