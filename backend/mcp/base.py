"""
Base types for the MCP Core.

Design rules (mirrors backend/plugins/base.py -- do not weaken):
- A connector is a single class implementing `MCPConnector`. It declares
  the tools it exposes via `list_tools()` and executes them via
  `call_tool()`. Nothing about routing, persistence, or lifecycle lives on
  the connector itself -- that's MCPRegistry/MCPManager's job.
- `call_tool()` must raise `MCPToolError` (not a bare exception) for
  expected failure modes (bad args, path outside sandbox, disabled
  connector, upstream API error) so callers can distinguish "the tool
  correctly rejected this" from "the connector code itself is broken".
  Unexpected exceptions still propagate and are caught/isolated by
  MCPManager, exactly like a plugin hook raising is isolated by
  PluginRegistry.
- A connector must never receive private keys or seed phrases, same
  constraint as plugins (see backend/plugins/base.py). None of the
  built-in connectors touch backend/wallet/import_utils.py or wallet key
  material at all.
- Every connector's `connect()`/`disconnect()` must be idempotent and
  cheap to call repeatedly -- MCPRegistry may call them on every
  enable/disable toggle and at process startup.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ConnectorStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass
class MCPTool:
    """
    Describes a single callable operation a connector exposes. `keywords`
    drives MCPToolRouter's free-text matching (backend/mcp/router.py) --
    short, distinctive phrases a user's request would plausibly contain,
    not a restatement of the tool name.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    # Marks a tool as mutating (writes/executes/deletes) vs. read-only, so
    # callers (chat/agent) can decide whether to require confirmation.
    destructive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "keywords": self.keywords,
            "destructive": self.destructive,
        }


@dataclass
class ConnectorHealth:
    status: ConnectorStatus
    detail: str = ""
    latency_ms: Optional[float] = None
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
        }


@dataclass
class ToolCallResult:
    """Uniform result shape returned by MCPClient/MCPManager for every tool call."""

    ok: bool
    connector: str
    tool: str
    output: Any = None
    error: Optional[str] = None
    latency_ms: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "connector": self.connector,
            "tool": self.tool,
            "output": self.output,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "meta": self.meta,
        }


class MCPToolError(Exception):
    """Raised by a connector's call_tool() for an expected/handled failure."""


class MCPConnector:
    """Base class every MCP connector subclasses. Override what you need."""

    #: Unique connector id, e.g. "filesystem". Defaults to the class name.
    name: str = ""
    version: str = "1.0.0"
    description: str = ""
    #: Short topical tags, used by the router as a coarse fallback signal
    #: (e.g. an explicit "use github" hint in a chat message).
    tags: list[str] = []

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        if not self.name:
            self.name = type(self).__name__
        self.config: dict[str, Any] = dict(config or {})
        self.status: ConnectorStatus = ConnectorStatus.DISCONNECTED
        self.last_error: Optional[str] = None

    # ---- Lifecycle ----------------------------------------------------
    async def connect(self) -> None:
        """Establish whatever the connector needs (open a client, validate
        config). Default: no-op, just marks CONNECTED. Subclasses that need
        real setup should call `super().connect()` last on success."""
        self.status = ConnectorStatus.CONNECTED
        self.last_error = None

    async def disconnect(self) -> None:
        """Release resources. Default: no-op, just marks DISCONNECTED."""
        self.status = ConnectorStatus.DISCONNECTED

    async def health_check(self) -> ConnectorHealth:
        """Cheap liveness check. Subclasses can override for a real probe
        (e.g. a HEAD request); default just reports the current status."""
        if self.status == ConnectorStatus.CONNECTED:
            return ConnectorHealth(ConnectorStatus.CONNECTED, "ok")
        if self.status == ConnectorStatus.DISABLED:
            return ConnectorHealth(ConnectorStatus.DISABLED, "disabled")
        detail = self.last_error or "not connected"
        return ConnectorHealth(self.status, detail)

    # ---- Tools ----------------------------------------------------------
    def list_tools(self) -> list[MCPTool]:
        raise NotImplementedError

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute `tool_name` with `arguments`. Must raise MCPToolError for
        expected failures (unknown tool, bad args, sandbox violation)."""
        raise NotImplementedError

    def get_tool(self, tool_name: str) -> Optional[MCPTool]:
        for tool in self.list_tools():
            if tool.name == tool_name:
                return tool
        return None
