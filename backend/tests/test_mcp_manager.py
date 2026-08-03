from types import SimpleNamespace

import pytest

from backend.mcp.base import MCPConnector, MCPTool, MCPToolError
from backend.mcp.manager import MCPManager
from backend.mcp.registry import MCPRegistry


class EchoConnector(MCPConnector):
    name = "echo"
    version = "1.0.0"
    description = "echoes its arguments back"
    tags = ["echo", "test"]

    def list_tools(self):
        return [
            MCPTool(
                name="echo_tool",
                description="Echo back whatever was sent.",
                keywords=["echo this", "say back"],
            ),
            MCPTool(
                name="boom_tool",
                description="Always raises an MCPToolError.",
                keywords=["boom"],
            ),
        ]

    async def call_tool(self, tool_name, arguments):
        if tool_name == "echo_tool":
            return {"echoed": arguments}
        if tool_name == "boom_tool":
            raise MCPToolError("boom failed on purpose")
        raise MCPToolError(f"unknown tool '{tool_name}'")


CLASSES = {"echo": EchoConnector}


@pytest.mark.asyncio
async def test_route_and_call_end_to_end(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    mgr = MCPManager(registry=registry)
    await mgr.enable("echo")

    result = await mgr.route_and_call("please echo this back to me", arguments={"value": 42})
    assert result is not None
    assert result.ok is True
    assert result.connector == "echo"
    assert result.tool == "echo_tool"
    assert result.output == {"echoed": {"value": 42}}


@pytest.mark.asyncio
async def test_route_and_call_no_match_returns_none(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    mgr = MCPManager(registry=registry)
    await mgr.enable("echo")

    result = await mgr.route_and_call("completely unrelated request about nothing")
    assert result is None


@pytest.mark.asyncio
async def test_call_against_disabled_connector_returns_ok_false_not_exception(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    mgr = MCPManager(registry=registry)
    # Never enabled -- no exception should propagate.
    result = await mgr.call("echo", "echo_tool", {"value": 1})
    assert result.ok is False
    assert "not enabled" in result.error


@pytest.mark.asyncio
async def test_call_tool_error_is_isolated_as_failed_result(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    mgr = MCPManager(registry=registry)
    await mgr.enable("echo")

    result = await mgr.call("echo", "boom_tool", {})
    assert result.ok is False
    assert "boom failed on purpose" in result.error


@pytest.mark.asyncio
async def test_on_call_fires_exactly_once_with_expected_args(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    mgr = MCPManager(registry=registry)
    await mgr.enable("echo")

    calls = []

    async def on_call(connector, tool, arguments, result):
        calls.append((connector, tool, arguments, result))

    mgr.on_call = on_call
    result = await mgr.call("echo", "echo_tool", {"value": 7})

    assert len(calls) == 1
    connector, tool, arguments, recorded_result = calls[0]
    assert connector == "echo"
    assert tool == "echo_tool"
    assert arguments == {"value": 7}
    assert recorded_result is result


@pytest.mark.asyncio
async def test_on_call_via_route_and_call_fires_exactly_once(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    mgr = MCPManager(registry=registry)
    await mgr.enable("echo")

    calls = []

    async def on_call(connector, tool, arguments, result):
        calls.append((connector, tool))

    mgr.on_call = on_call
    await mgr.route_and_call("please echo this back to me", arguments={"value": 1})

    assert calls == [("echo", "echo_tool")]


@pytest.mark.asyncio
async def test_on_call_exception_does_not_propagate_out_of_call(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    mgr = MCPManager(registry=registry)
    await mgr.enable("echo")

    async def bad_on_call(connector, tool, arguments, result):
        raise RuntimeError("callback exploded")

    mgr.on_call = bad_on_call
    # Should not raise, despite on_call raising internally.
    result = await mgr.call("echo", "echo_tool", {"value": 1})
    assert result.ok is True


def _fake_settings(**overrides):
    defaults = dict(
        mcp_enabled=True,
        mcp_filesystem_enabled=True,
        mcp_terminal_enabled=False,
        mcp_browser_enabled=True,
        mcp_github_enabled=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_mcp_enabled_false_gates_start(tmp_path):
    """settings.mcp_enabled=False must actually prevent connectors from
    being started -- regression test for the master-switch no-op where
    _enabled_gate was never set by from_settings()."""
    mgr = MCPManager.from_settings(_fake_settings(mcp_enabled=False), data_dir=tmp_path)

    await mgr.start()

    # state.mcp remains a real object (per the chosen fix) so the /mcp API
    # still responds, but nothing should actually be connected.
    statuses = [c["status"] for c in mgr.registry.list_connectors()]
    assert all(status == "disconnected" for status in statuses)


@pytest.mark.asyncio
async def test_mcp_enabled_true_still_starts_connectors(tmp_path):
    """The default (mcp_enabled=True, unset) must keep starting connectors
    exactly as before this fix."""
    mgr = MCPManager.from_settings(_fake_settings(mcp_enabled=True), data_dir=tmp_path)

    await mgr.start()

    statuses = {c["name"]: c["status"] for c in mgr.registry.list_connectors()}
    # filesystem/browser/github default to enabled in from_settings(); they
    # should have been connected by start().
    assert statuses["filesystem"] == "connected"


def test_mcp_enabled_defaults_true_when_unset():
    """getattr(settings, "mcp_enabled", True) fallback: a settings object
    with no mcp_enabled attribute at all should still enable the gate."""
    settings_without_flag = SimpleNamespace(
        mcp_filesystem_enabled=True,
        mcp_terminal_enabled=False,
        mcp_browser_enabled=True,
        mcp_github_enabled=True,
    )
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        mgr = MCPManager.from_settings(settings_without_flag, data_dir=Path(d))
    assert mgr._enabled_gate is True
