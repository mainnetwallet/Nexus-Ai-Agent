"""
Log access API.

Read-only tailing of the backend's rotating log file (backend/config/settings.py
-> LOG_DIR / "nexus.log"). No write/delete surface here on purpose.

Also exposes a live WebSocket stream (`WS /api/logs/ws/live`) that pushes
each formatted log line to connected clients as it's emitted, fed by a
`logging.Handler` (`WebSocketLogBroadcastHandler`, attached in
backend/main.py's lifespan) rather than by polling the file -- so a viewer
sees log lines the moment they're written, from any logger in the process
(planner, task queue, plugins, wallet, etc.), not just this router's own.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from backend.api.auth import require_auth
from backend.config.settings import LOG_DIR

router = APIRouter(prefix="/api/logs", tags=["logs"], dependencies=[Depends(require_auth)])

_LOG_FILE = LOG_DIR / "nexus.log"

_ws_clients: set[WebSocket] = set()


@router.get("")
async def tail_logs(lines: int = 200):
    """Return the last `lines` lines of the backend log file."""
    if not _LOG_FILE.exists():
        return {"lines": [], "file": str(_LOG_FILE)}

    with _LOG_FILE.open("r", errors="replace") as f:
        content = f.readlines()

    tail = [line.rstrip("\n") for line in content[-lines:]]
    return {"lines": tail, "file": str(_LOG_FILE), "total_lines": len(content)}


async def broadcast_log_line(line: str) -> None:
    """Fan a single formatted log line out to every connected WS client."""
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(line)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


@router.websocket("/ws/live")
async def live_logs(websocket: WebSocket):
    """
    Push-based live log stream. On connect, immediately sends the last 50
    lines already on disk (so a viewer isn't staring at a blank pane), then
    streams every new formatted log line as it's emitted anywhere in the
    backend process.
    """
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        if _LOG_FILE.exists():
            with _LOG_FILE.open("r", errors="replace") as f:
                backlog = f.readlines()[-50:]
            for line in backlog:
                await websocket.send_text(line.rstrip("\n"))
        while True:
            await websocket.receive_text()  # keepalive / ignored
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)


class WebSocketLogBroadcastHandler(logging.Handler):
    """
    Bridges stdlib `logging` (sync, called from any thread -- including
    chromadb's/playwright's worker threads) to the async `broadcast_log_line`
    above. Attached to the root logger in backend/main.py's lifespan so
    every logger in the process (nexus.planner, nexus.decision_engine,
    nexus.queue, nexus.plugins, ...) feeds the live stream, on top of the
    existing FileHandler/StreamHandler set up at import time -- this is
    purely additive, nothing about existing logging configuration changes.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        try:
            self._loop.call_soon_threadsafe(self._schedule_broadcast, msg)
        except RuntimeError:
            pass  # loop already closed (e.g. during shutdown) -- drop silently

    @staticmethod
    def _schedule_broadcast(msg: str) -> None:
        asyncio.ensure_future(broadcast_log_line(msg))
