"""
MCPToolDiscovery -- aggregates the tool schemas exposed by every connected
connector into one flat, queryable list. Purely a read-side view over
MCPRegistry; it owns no state of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.mcp.base import ConnectorStatus, MCPTool


@dataclass
class DiscoveredTool:
    connector: str
    tool: MCPTool

    def to_dict(self) -> dict[str, Any]:
        return {"connector": self.connector, **self.tool.to_dict()}


class MCPToolDiscovery:
    def __init__(self, registry: Any) -> None:
        self.registry = registry  # MCPRegistry

    def list_all(self, *, connected_only: bool = True) -> list[DiscoveredTool]:
        """Every tool across every connector. `connected_only=True` (the
        default) hides tools from connectors that are disabled or failed to
        connect, since routing to them would just fail anyway."""
        out: list[DiscoveredTool] = []
        for record in self.registry.records():
            if connected_only and record.instance is None:
                continue
            if connected_only and record.instance.status != ConnectorStatus.CONNECTED:
                continue
            if record.instance is None:
                continue
            for tool in record.instance.list_tools():
                out.append(DiscoveredTool(connector=record.name, tool=tool))
        return out

    def list_for_connector(self, connector_name: str) -> list[MCPTool]:
        record = self.registry.get_record(connector_name)
        if record is None or record.instance is None:
            return []
        return record.instance.list_tools()

    def find(self, connector_name: str, tool_name: str) -> DiscoveredTool | None:
        for tool in self.list_for_connector(connector_name):
            if tool.name == tool_name:
                return DiscoveredTool(connector=connector_name, tool=tool)
        return None
