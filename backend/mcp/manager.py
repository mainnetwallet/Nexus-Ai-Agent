"""
MCPManager -- the single facade every other subsystem (Chat, Planner, Agent
Runtime, AI Decision Engine, Skill Learning, Memory, Dashboard, Telegram)
talks to. Composes MCPRegistry (lifecycle+config), MCPToolDiscovery
(tool listing), and MCPToolRouter (auto-selection) -- it does not
re-implement any of their logic, matching how AgentRuntime composes
TaskQueueService rather than owning task execution itself.

Lives at `state.mcp` (backend/api/app_state.py), constructed once in
backend/main.py's lifespan and started/stopped alongside the rest of the
app's singletons.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from backend.mcp.base import ToolCallResult
from backend.mcp.client import MCPClient
from backend.mcp.connectors import SOCIAL_CONNECTOR_NAMES
from backend.mcp.discovery import MCPToolDiscovery
from backend.mcp.registry import MCPRegistry
from backend.mcp.router import DEFAULT_MIN_SCORE, MCPToolRouter

logger = logging.getLogger("nexus.mcp.manager")


class MCPManager:
    def __init__(
        self,
        registry: MCPRegistry,
        tool_call_timeout: float = 30.0,
        router_min_score: float = DEFAULT_MIN_SCORE,
    ) -> None:
        self.registry = registry
        self.discovery = MCPToolDiscovery(registry)
        self.router = MCPToolRouter(self.discovery)
        self.tool_call_timeout = tool_call_timeout
        self.router_min_score = router_min_score
        self._clients: dict[str, MCPClient] = {}
        # Optional callback(connector: str, tool: str, arguments: dict, result: ToolCallResult) -> Awaitable[None],
        # invoked after every call()/route_and_call(). Kept as a settable attribute (not a constructor arg used
        # by from_settings) so this package stays dependency-free of memory/chat/telegram -- backend/main.py wires
        # it to a thin wrapper around state.memory.save_tool_call, mirroring notify_fn/activity_fn on TaskQueueService.
        self.on_call: Optional[Any] = None
        # Master switch (settings.mcp_enabled). Defaults to enabled so tests
        # that construct MCPManager directly (bypassing from_settings) keep
        # their existing behavior; from_settings() below is the only place
        # that ever sets this to False.
        self._enabled_gate: bool = True

    @classmethod
    def from_settings(cls, settings: Any, data_dir: Path) -> "MCPManager":
        """Builds a registry pre-seeded from Settings (enabled-by-default
        flags + connector config), then wraps it in a manager. This is the
        constructor backend/main.py uses; tests construct MCPRegistry/
        MCPManager directly with their own tmp_path + fakes instead."""
        default_enabled = {
            "filesystem": bool(getattr(settings, "mcp_filesystem_enabled", True)),
            "terminal": bool(getattr(settings, "mcp_terminal_enabled", False)),
            "browser": bool(getattr(settings, "mcp_browser_enabled", True)),
            "github": bool(getattr(settings, "mcp_github_enabled", True)),
            "x": bool(getattr(settings, "mcp_x_enabled", True)),
            "discord": bool(getattr(settings, "mcp_discord_enabled", True)),
            "gmail": bool(getattr(settings, "mcp_gmail_enabled", True)),
        }
        fs_roots = list(getattr(settings, "mcp_filesystem_roots_list", []) or [])
        terminal_allowlist = list(getattr(settings, "mcp_terminal_commands_allowlist_set", []) or [])
        default_config = {
            "filesystem": {"roots": fs_roots or [str(data_dir)]},
            "terminal": {
                # Empty allow-list from Settings means "use the connector's
                # own built-in default", not "allow nothing" -- pass None so
                # TerminalMCPConnector.__init__ falls back to DEFAULT_ALLOWED_COMMANDS.
                "allowed_commands": terminal_allowlist or None,
                "timeout": getattr(settings, "mcp_terminal_timeout_seconds", 30),
                "cwd": getattr(settings, "mcp_terminal_working_dir", str(data_dir)),
            },
            "browser": {"timeout": getattr(settings, "mcp_browser_timeout_seconds", 20)},
            "github": {
                "token": getattr(settings, "mcp_github_token", ""),
                "default_owner": getattr(settings, "mcp_github_default_owner", ""),
                "default_repo": getattr(settings, "mcp_github_default_repo", ""),
            },
            "x": {"account": getattr(settings, "mcp_x_account", "") or None},
            "discord": {"account": getattr(settings, "mcp_discord_account", "") or None},
            "gmail": {"account": getattr(settings, "mcp_gmail_account", "") or None},
        }
        registry = MCPRegistry(
            data_dir=data_dir, default_enabled=default_enabled, default_config=default_config
        )
        manager = cls(
            registry=registry,
            tool_call_timeout=getattr(settings, "mcp_tool_call_timeout_seconds", 30.0),
            router_min_score=getattr(settings, "mcp_router_min_score", DEFAULT_MIN_SCORE),
        )
        manager._enabled_gate = bool(getattr(settings, "mcp_enabled", True))
        return manager

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if not getattr(self, "_enabled_gate", True):
            logger.info("MCP manager start() skipped: mcp_enabled is False")
            return
        await self.registry.start_enabled()
        logger.info(
            "MCP manager started: %s",
            [r["name"] for r in self.registry.list_connectors() if r["enabled"]],
        )

    async def stop(self) -> None:
        await self.registry.stop_all()

    def wire_browser_engine_provider(self, provider: Any) -> None:
        """Lets main.py hand the browser connector (and every social
        connector -- X/Discord/Gmail, see backend/mcp/connectors/
        social_base.py) a `Callable[[], Optional[BrowserEngine]]` once the
        live engine/queue exists (which happens after this manager is
        constructed), so they read the agent's real live session instead of
        duplicating a second Playwright instance. No-op per-connector if
        that connector was never constructed (e.g. disabled)."""
        for name in ("browser", *SOCIAL_CONNECTOR_NAMES):
            record = self.registry.get_record(name)
            if record is None:
                continue
            instance = record.instance
            if instance is not None and hasattr(instance, "set_engine_provider"):
                instance.set_engine_provider(provider)

    # ------------------------------------------------------------------ #
    # Connector management (dashboard-facing)
    # ------------------------------------------------------------------ #
    def list_connectors(self) -> list[dict[str, Any]]:
        return self.registry.list_connectors()

    async def enable(self, name: str) -> bool:
        return await self.registry.enable(name)

    async def disable(self, name: str) -> bool:
        return await self.registry.disable(name)

    async def configure(self, name: str, config: dict[str, Any]) -> bool:
        return await self.registry.configure(name, config)

    async def health(self) -> dict[str, Any]:
        health_by_connector = await self.registry.health()
        return {name: h.to_dict() for name, h in health_by_connector.items()}

    async def social_status(self) -> dict[str, Any]:
        """Connection Status + Session Status + Account Information + Last
        Used for X/Discord/Gmail in one call -- backs GET
        /api/mcp/social-status (backend/api/routes_mcp.py), used by the
        dashboard's social connectors panel."""
        out: dict[str, Any] = {}
        for name in SOCIAL_CONNECTOR_NAMES:
            record = self.registry.get_record(name)
            if record is None:
                continue
            instance = record.instance
            if instance is None or not hasattr(instance, "status_snapshot"):
                out[name] = {
                    "connector": name,
                    "service": name,
                    "connection_status": "disconnected",
                    "session_status": "unknown",
                    "account": (record.config or {}).get("account"),
                    "last_used_at": None,
                }
                continue
            out[name] = await instance.status_snapshot()
        return out

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #
    def list_tools(self, connector: Optional[str] = None) -> list[dict[str, Any]]:
        if connector:
            return [t.to_dict() for t in self.discovery.list_for_connector(connector)]
        return [t.to_dict() for t in self.discovery.list_all(connected_only=True)]

    def _client_for(self, connector_name: str) -> Optional[MCPClient]:
        record = self.registry.get_record(connector_name)
        if record is None or record.instance is None:
            return None
        client = self._clients.get(connector_name)
        if client is None or client.connector is not record.instance:
            client = MCPClient(record.instance, default_timeout=self.tool_call_timeout)
            self._clients[connector_name] = client
        return client

    async def call(
        self, connector: str, tool: str, arguments: Optional[dict[str, Any]] = None, timeout: Optional[float] = None
    ) -> ToolCallResult:
        """Explicit call: caller already knows exactly which connector+tool."""
        client = self._client_for(connector)
        if client is None:
            result = ToolCallResult(
                ok=False, connector=connector, tool=tool, error=f"connector '{connector}' is not enabled"
            )
        else:
            result = await client.call(tool, arguments, timeout=timeout)
        if self.on_call is not None:
            try:
                await self.on_call(connector, tool, arguments or {}, result)
            except Exception:  # noqa: BLE001
                logger.exception("on_call callback failed for %s.%s", connector, tool)
        return result

    async def route_and_call(
        self,
        request_text: str,
        connector_hint: Optional[str] = None,
        arguments: Optional[dict[str, Any]] = None,
        min_score: Optional[float] = None,
    ) -> Optional[ToolCallResult]:
        """
        The 'AI automatically chooses the correct MCP connector' entry point.
        Used by ChatEngine, AgentLoop's mcp_tool action, SkillRunner, and the
        Telegram bot's free-text fallback -- one routing implementation
        shared everywhere, exactly like SkillMatcher is shared by chat and
        the task queue. Returns None (not a failed ToolCallResult) when
        nothing matched confidently enough, so callers can distinguish "no
        tool applies" from "the tool call failed".
        """
        routed = self.router.route(
            request_text, connector_hint=connector_hint, min_score=min_score if min_score is not None else self.router_min_score
        )
        if routed is None:
            return None
        return await self.call(routed.connector, routed.tool_name, arguments or {})
