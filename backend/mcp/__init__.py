"""
MCP Core -- Model Context Protocol connector framework for Nexus-Agent.

Gives the agent a uniform way to reach outside its browser-automation core
into filesystem, terminal, live web, and GitHub tools, using the same
discover -> enable/disable -> dispatch shape as `backend/plugins/registry.py`
(see that module's docstring for the design rationale this mirrors).

Public surface:
    MCPManager    -- top-level facade (backend/mcp/manager.py). One instance
                     lives at `state.mcp` (backend/api/app_state.py).
    MCPRegistry   -- discovers/persists connector configuration, owns
                     connector instances and their connect/disconnect
                     lifecycle (backend/mcp/registry.py).
    MCPToolDiscovery -- aggregates tool schemas from every connected
                     connector (backend/mcp/discovery.py).
    MCPToolRouter -- picks the right connector+tool for a free-form request
                     (backend/mcp/router.py), the same "keyword pass, then
                     scored pass" shape as backend/skills/matcher.py.
    MCPConnector, MCPTool, ToolCallResult, MCPToolError -- base types
                     (backend/mcp/base.py).

Built-in connectors live under backend/mcp/connectors/: filesystem,
terminal, browser, github. Nothing here talks to the network at import
time -- connectors only connect when explicitly enabled.
"""
from backend.mcp.base import (
    ConnectorHealth,
    ConnectorStatus,
    MCPConnector,
    MCPTool,
    MCPToolError,
    ToolCallResult,
)
from backend.mcp.manager import MCPManager
from backend.mcp.registry import MCPRegistry

__all__ = [
    "MCPManager",
    "MCPRegistry",
    "MCPConnector",
    "MCPTool",
    "MCPToolError",
    "ToolCallResult",
    "ConnectorHealth",
    "ConnectorStatus",
]
