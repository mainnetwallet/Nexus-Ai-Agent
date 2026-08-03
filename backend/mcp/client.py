"""
MCPClient -- thin per-connector wrapper that turns a raw `call_tool()` into
a uniform, timed, never-raising `ToolCallResult`.

This is the single choke point every tool invocation passes through, so
timeout enforcement, error normalization, and call bookkeeping (last call
time/error, for the dashboard) live in exactly one place rather than being
duplicated across chat/agent/skills/telegram call sites.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from backend.mcp.base import ConnectorStatus, MCPConnector, MCPToolError, ToolCallResult

logger = logging.getLogger("nexus.mcp.client")


@dataclass
class ClientStats:
    calls: int = 0
    errors: int = 0
    last_call_at: Optional[float] = None
    last_error: Optional[str] = None
    last_tool: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "errors": self.errors,
            "last_call_at": self.last_call_at,
            "last_error": self.last_error,
            "last_tool": self.last_tool,
        }


class MCPClient:
    """Wraps a single connected `MCPConnector` instance."""

    def __init__(self, connector: MCPConnector, default_timeout: float = 30.0) -> None:
        self.connector = connector
        self.default_timeout = default_timeout
        self.stats = ClientStats()

    async def call(
        self, tool_name: str, arguments: Optional[dict[str, Any]] = None, timeout: Optional[float] = None
    ) -> ToolCallResult:
        arguments = arguments or {}
        start = time.perf_counter()
        self.stats.calls += 1
        self.stats.last_call_at = time.time()
        self.stats.last_tool = tool_name

        if self.connector.status != ConnectorStatus.CONNECTED:
            error = f"connector '{self.connector.name}' is not connected (status={self.connector.status.value})"
            self.stats.errors += 1
            self.stats.last_error = error
            return ToolCallResult(ok=False, connector=self.connector.name, tool=tool_name, error=error)

        tool = self.connector.get_tool(tool_name)
        if tool is None:
            error = f"unknown tool '{tool_name}' on connector '{self.connector.name}'"
            self.stats.errors += 1
            self.stats.last_error = error
            return ToolCallResult(ok=False, connector=self.connector.name, tool=tool_name, error=error)

        try:
            output = await asyncio.wait_for(
                self.connector.call_tool(tool_name, arguments), timeout=timeout or self.default_timeout
            )
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            return ToolCallResult(
                ok=True, connector=self.connector.name, tool=tool_name, output=output, latency_ms=latency_ms
            )
        except asyncio.TimeoutError:
            error = f"tool '{tool_name}' timed out after {timeout or self.default_timeout}s"
            logger.warning("MCP tool call timed out: connector=%s tool=%s", self.connector.name, tool_name)
        except MCPToolError as exc:
            error = str(exc)
            logger.info("MCP tool call rejected: connector=%s tool=%s error=%s", self.connector.name, tool_name, error)
        except Exception as exc:  # noqa: BLE001 - isolate: one bad tool call must never crash the caller
            error = f"unexpected error: {exc}"
            logger.exception("MCP tool call raised unexpectedly: connector=%s tool=%s", self.connector.name, tool_name)

        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        self.stats.errors += 1
        self.stats.last_error = error
        return ToolCallResult(ok=False, connector=self.connector.name, tool=tool_name, error=error, latency_ms=latency_ms)
