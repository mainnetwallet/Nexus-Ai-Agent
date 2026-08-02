"""
Live Browser Session.

Lets a client "watch" whichever website the agent is currently driving, in
(near) real time -- without touching how BrowserEngine or TaskQueueService
work internally. It only *observes* whatever BrowserEngine the task queue
currently has active (`TaskQueueService.current_engine`); it never creates,
owns, or controls a browser itself.

Two ways to consume it (both wired up in backend/api/routes_browser.py):
- Poll `GET /api/browser/status` and `GET /api/browser/screenshot` for a
  simple request/response integration.
- Open `WS /api/browser/ws/live` for push-based streaming: the manager
  broadcasts a JSON frame (base64 JPEG + url/title/task metadata) to every
  connected client on a fixed interval, and again immediately on any
  meaningful page change caught by the polling loop.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Callable, Optional

from fastapi import WebSocket

from backend.browser.engine import BrowserEngine, BrowserEngineError
from backend.config.settings import settings

logger = logging.getLogger("nexus.live_session")


class LiveSessionManager:
    """
    Singleton-ish helper (one instance lives on `backend.api.app_state.state`)
    that periodically screenshots whatever BrowserEngine is currently active
    and fans the result out to connected WebSocket clients.
    """

    def __init__(
        self,
        engine_provider: Callable[[], Optional[BrowserEngine]],
        task_id_provider: Callable[[], Optional[str]],
        interval_ms: Optional[int] = None,
        jpeg_quality: Optional[int] = None,
    ) -> None:
        self._engine_provider = engine_provider
        self._task_id_provider = task_id_provider
        self._interval_ms = interval_ms if interval_ms is not None else settings.live_session_interval_ms
        self._jpeg_quality = jpeg_quality if jpeg_quality is not None else settings.live_session_jpeg_quality

        self._clients: set[WebSocket] = set()
        self._poll_task: Optional[asyncio.Task] = None

        self._latest_frame_b64: Optional[str] = None
        self._latest_frame_bytes: Optional[bytes] = None
        self._latest_url: str = ""
        self._latest_title: str = ""
        self._latest_task_id: Optional[str] = None
        self._latest_captured_at: float = 0.0
        self._frame_count: int = 0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if not settings.live_session_enabled:
            logger.info("Live browser session disabled via settings, not starting poll loop")
            return
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("Live browser session poll loop started (interval=%dms)", self._interval_ms)

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("Poll loop raised on shutdown (%s)", exc)
            self._poll_task = None
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception as exc:
                logger.debug("Error closing live session websocket (%s)", exc)
        self._clients.clear()

    # ------------------------------------------------------------------ #
    # WebSocket client management
    # ------------------------------------------------------------------ #
    async def register(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        logger.info("Live session client connected (total=%d)", len(self._clients))
        # Send whatever we already have so the client isn't staring at a
        # blank screen until the next poll tick.
        snapshot = self._current_frame_payload()
        if snapshot:
            try:
                await websocket.send_text(snapshot)
            except Exception:
                self._clients.discard(websocket)

    def unregister(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)
        logger.info("Live session client disconnected (total=%d)", len(self._clients))

    # ------------------------------------------------------------------ #
    # Status / snapshot API
    # ------------------------------------------------------------------ #
    def is_active(self) -> bool:
        return self._engine_provider() is not None

    def status(self) -> dict[str, Any]:
        active = self.is_active()
        return {
            "active": active,
            "task_id": self._task_id_provider() if active else None,
            "url": self._latest_url if active else "",
            "title": self._latest_title if active else "",
            "connected_clients": len(self._clients),
            "frame_count": self._frame_count,
            "last_frame_at": self._latest_captured_at or None,
            "stream_interval_ms": self._interval_ms,
            "jpeg_quality": self._jpeg_quality,
            "last_error": self._last_error,
        }

    def latest_screenshot_bytes(self) -> Optional[bytes]:
        return self._latest_frame_bytes

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _current_frame_payload(self) -> Optional[str]:
        if self._latest_frame_b64 is None:
            return None
        return json.dumps(
            {
                "type": "frame",
                "task_id": self._latest_task_id,
                "url": self._latest_url,
                "title": self._latest_title,
                "captured_at": self._latest_captured_at,
                "mime_type": "image/jpeg",
                "image_base64": self._latest_frame_b64,
            }
        )

    async def _poll_loop(self) -> None:
        was_active = False
        while True:
            try:
                engine = self._engine_provider()
                if engine is not None:
                    await self._capture(engine)
                    was_active = True
                elif was_active:
                    # The active task just ended -- tell clients the session
                    # went idle so a UI can show a "no active browser" state.
                    was_active = False
                    await self._broadcast(json.dumps({"type": "idle"}))
                await asyncio.sleep(self._interval_ms / 1000)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Live session poll loop error")
                await asyncio.sleep(self._interval_ms / 1000)

    async def _capture(self, engine: BrowserEngine) -> None:
        try:
            page = engine.page
        except BrowserEngineError:
            return

        try:
            frame_bytes = await page.screenshot(type="jpeg", quality=self._jpeg_quality)
        except Exception as exc:
            # Common during navigation transitions -- not a real failure.
            self._last_error = str(exc)
            return

        self._last_error = None
        self._latest_frame_bytes = frame_bytes
        self._latest_frame_b64 = base64.b64encode(frame_bytes).decode("ascii")
        self._latest_url = page.url
        try:
            self._latest_title = await page.title()
        except Exception as exc:
            logger.debug("Failed to read page title for live frame (%s)", exc)
        self._latest_task_id = self._task_id_provider()
        self._latest_captured_at = time.time()
        self._frame_count += 1

        payload = self._current_frame_payload()
        if payload:
            await self._broadcast(payload)

    async def _broadcast(self, payload: str) -> None:
        dead = set()
        for ws in self._clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._clients.difference_update(dead)
