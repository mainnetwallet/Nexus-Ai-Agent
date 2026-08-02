"""
Health Monitor.

Aggregates a fast, best-effort health check across every subsystem the
dashboard/Telegram bot cares about: backend, browser, database, process
memory, AI provider, Telegram, and the WebSocket broadcast layer. Reuses
existing singletons on AppState/settings rather than re-implementing any
connectivity logic -- this module only asks "is it up" and times how long
that took.

Each check is isolated: one subsystem failing (e.g. no AI key configured)
never raises and never prevents the other checks from running.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from backend.config.settings import settings

HealthStatus = str  # "ok" | "degraded" | "down" | "unknown"


@dataclass
class ComponentHealth:
    name: str
    status: HealthStatus
    detail: str = ""
    latency_ms: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


@dataclass
class HealthReport:
    overall: HealthStatus
    components: list[ComponentHealth] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "overall": self.overall,
            "checked_at": self.checked_at,
            "components": [c.to_dict() for c in self.components],
        }


def _timed(fn: Callable[[], tuple[HealthStatus, str]]) -> tuple[HealthStatus, str, float]:
    start = time.perf_counter()
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001 - a check failing is data, not a crash
        status, detail = "down", f"check raised: {exc}"
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return status, detail, latency_ms


class HealthMonitor:
    """
    Reads live state from AppState (passed in at call time to avoid an
    import cycle with backend.api.app_state) and returns a HealthReport.
    """

    def __init__(self, app_state: Any) -> None:
        self.state = app_state

    async def check_all(self) -> HealthReport:
        components = [
            self._check_backend(),
            await self._check_database(),
            self._check_browser(),
            self._check_memory_store(),
            self._check_ai_provider(),
            self._check_telegram(),
            self._check_websocket(),
        ]
        overall = self._aggregate(components)
        return HealthReport(overall=overall, components=components)

    # ------------------------------------------------------------------ #
    # Individual checks
    # ------------------------------------------------------------------ #
    def _check_backend(self) -> ComponentHealth:
        status, detail, latency = _timed(lambda: ("ok", f"{settings.app_name} process alive"))
        return ComponentHealth("backend", status, detail, latency)

    async def _check_database(self) -> ComponentHealth:
        start = time.perf_counter()
        try:
            from sqlalchemy import text

            from backend.database.session import get_session

            async with get_session() as session:
                await session.execute(text("SELECT 1"))
            status, detail = "ok", "SQLite reachable"
        except Exception as exc:  # noqa: BLE001
            status, detail = "down", f"query failed: {exc}"
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return ComponentHealth("database", status, detail, latency_ms)

    def _check_browser(self) -> ComponentHealth:
        def _run() -> tuple[HealthStatus, str]:
            queue = getattr(self.state, "queue", None)
            if queue is None:
                return "unknown", "task queue not initialized"
            engine = getattr(queue, "current_engine", None)
            if engine is None:
                return "ok", "idle (no active browser session)"
            return "ok", "active browser session running"

        status, detail, latency = _timed(_run)
        return ComponentHealth("browser", status, detail, latency)

    def _check_memory_store(self) -> ComponentHealth:
        def _run() -> tuple[HealthStatus, str]:
            memory = getattr(self.state, "memory", None)
            if memory is None:
                return "down", "MemoryStore not initialized"
            return "ok", "vector memory store initialized"

        status, detail, latency = _timed(_run)
        return ComponentHealth("memory", status, detail, latency)

    def _check_ai_provider(self) -> ComponentHealth:
        def _run() -> tuple[HealthStatus, str]:
            provider = settings.llm_provider.value
            key_map = {
                "anthropic": settings.anthropic_api_key,
                "openai": settings.openai_api_key,
                "gemini": settings.gemini_api_key,
                "openrouter": settings.openrouter_api_key,
            }
            if key_map.get(provider):
                return "ok", f"{provider} API key configured"
            return "degraded", f"{provider} API key missing"

        status, detail, latency = _timed(_run)
        return ComponentHealth("ai_provider", status, detail, latency)

    def _check_telegram(self) -> ComponentHealth:
        def _run() -> tuple[HealthStatus, str]:
            if not settings.telegram_bot_token:
                return "degraded", "TELEGRAM_BOT_TOKEN not set -- bot disabled"
            return "ok", "bot token configured"

        status, detail, latency = _timed(_run)
        return ComponentHealth("telegram", status, detail, latency)

    def _check_websocket(self) -> ComponentHealth:
        def _run() -> tuple[HealthStatus, str]:
            # The agent/logs/plugins routers each keep their own in-process
            # WebSocket client set; the layer itself is always "up" as long
            # as the FastAPI app is serving requests (this check is reached).
            return "ok", "WebSocket broadcast layer active"

        status, detail, latency = _timed(_run)
        return ComponentHealth("websocket", status, detail, latency)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _aggregate(components: list[ComponentHealth]) -> HealthStatus:
        statuses = {c.status for c in components}
        if "down" in statuses:
            return "down"
        if "degraded" in statuses:
            return "degraded"
        if statuses == {"ok"}:
            return "ok"
        return "unknown"
