from types import SimpleNamespace

import pytest

from backend.mcp.base import ToolCallResult
from backend.planner.chat_engine import ChatEngine


class FakeMCP:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def route_and_call(self, request_text, connector_hint=None, arguments=None, min_score=None):
        self.calls.append(
            {"request_text": request_text, "connector_hint": connector_hint, "arguments": arguments}
        )
        return self._result


@pytest.mark.asyncio
async def test_mcp_category_dispatches_to_route_and_call_on_success():
    result = ToolCallResult(ok=True, connector="filesystem", tool="read_file", output={"content": "hi"})
    fake_mcp = FakeMCP(result)
    app_state = SimpleNamespace(mcp=fake_mcp)
    chat = ChatEngine(queue=None, app_state=app_state)

    intent = {"mcp_query": "read notes.txt", "mcp_connector": "filesystem"}
    reply, meta = await chat._handle_mcp_command(intent, "please read notes.txt")

    assert fake_mcp.calls == [
        {"request_text": "read notes.txt", "connector_hint": "filesystem", "arguments": None}
    ]
    assert meta == {"connector": "filesystem", "tool": "read_file", "ok": True}
    assert "[filesystem.read_file]" in reply
    assert "hi" in reply


@pytest.mark.asyncio
async def test_mcp_category_falls_back_to_raw_text_when_no_mcp_query():
    result = ToolCallResult(ok=True, connector="github", tool="list_issues", output=[])
    fake_mcp = FakeMCP(result)
    app_state = SimpleNamespace(mcp=fake_mcp)
    chat = ChatEngine(queue=None, app_state=app_state)

    await chat._handle_mcp_command({}, "list issues on this repo")
    assert fake_mcp.calls[0]["request_text"] == "list issues on this repo"
    assert fake_mcp.calls[0]["connector_hint"] is None


@pytest.mark.asyncio
async def test_mcp_category_reports_failed_tool_call():
    result = ToolCallResult(ok=False, connector="terminal", tool="run_command", error="not in the allow-list")
    fake_mcp = FakeMCP(result)
    app_state = SimpleNamespace(mcp=fake_mcp)
    chat = ChatEngine(queue=None, app_state=app_state)

    reply, meta = await chat._handle_mcp_command({"mcp_connector": "terminal"}, "run rm -rf /")
    assert meta["ok"] is False
    assert "failed" in reply
    assert "not in the allow-list" in reply


@pytest.mark.asyncio
async def test_mcp_category_no_route_match_gives_helpful_message():
    fake_mcp = FakeMCP(None)
    app_state = SimpleNamespace(mcp=fake_mcp)
    chat = ChatEngine(queue=None, app_state=app_state)

    reply, meta = await chat._handle_mcp_command({}, "do something vague")
    assert meta == {}
    assert "couldn't figure out which tool" in reply.lower()


@pytest.mark.asyncio
async def test_mcp_core_not_enabled_fallback_when_app_state_mcp_is_none():
    app_state = SimpleNamespace(mcp=None)
    chat = ChatEngine(queue=None, app_state=app_state)

    reply, meta = await chat._handle_mcp_command({}, "read a file")
    assert reply == "MCP Core isn't enabled in this deployment."
    assert meta == {}


@pytest.mark.asyncio
async def test_mcp_core_not_enabled_fallback_when_app_state_is_none():
    chat = ChatEngine(queue=None, app_state=None)
    reply, meta = await chat._handle_mcp_command({}, "read a file")
    assert reply == "MCP Core isn't enabled in this deployment."
    assert meta == {}
