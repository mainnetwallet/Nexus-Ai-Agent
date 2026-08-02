"""
Plugin management API.

Only lists/enables/disables/reloads plugins already discovered from disk
(`backend/plugins/installed/` by default) -- there is deliberately no
upload/install-from-string endpoint here. See
`backend/plugins/registry.py` module docstring for why.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from backend.api.app_state import state
from backend.api.auth import require_auth

router = APIRouter(prefix="/api/plugins", tags=["plugins"], dependencies=[Depends(require_auth)])

_ws_clients: set[WebSocket] = set()


def _registry():
    if state.plugins is None:
        raise HTTPException(status_code=503, detail="Plugin registry not initialized")
    return state.plugins


@router.get("")
async def list_plugins():
    return {"plugins": _registry().list_plugins()}


@router.post("/rescan")
async def rescan_plugins():
    """Re-scan plugins_dir for new/changed files without touching already-enabled plugins."""
    newly = _registry().discover()
    return {"discovered": newly, "plugins": _registry().list_plugins()}


@router.post("/{name}/enable")
async def enable_plugin(name: str):
    ok = await _registry().enable(name)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not enable plugin '{name}' (unknown or failed on_load)")
    return {"name": name, "enabled": True}


@router.post("/{name}/disable")
async def disable_plugin(name: str):
    ok = await _registry().disable(name)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not disable plugin '{name}' (unknown or not enabled)")
    return {"name": name, "enabled": False}


@router.post("/{name}/reload")
async def reload_plugin(name: str):
    ok = await _registry().reload(name)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not reload plugin '{name}'")
    return {"plugins": _registry().list_plugins()}


async def broadcast(message: str) -> None:
    """Fan a JSON-encoded plugin event out to every connected WS client.
    Wired up as PluginRegistry(event_fn=...) in backend/main.py."""
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


@router.websocket("/ws/live")
async def live_plugin_events(websocket: WebSocket):
    """
    Push-based stream of plugin lifecycle and hook-dispatch events: enable,
    disable, reload, and each task_start/step/task_finish/wallet_popup
    dispatch (see backend/plugins/registry.py's `event_fn` calls). Each
    message is a JSON object with at least a "type" field.
    """
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / ignored
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)
