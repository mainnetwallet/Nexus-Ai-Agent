import pytest

from backend.mcp.base import ToolCallResult
from backend.planner.agent_loop import AgentLoop, StepAction


class FakeMemory:
    async def recall_similar_workflows(self, website, goal, top_k=3):
        return []

    async def save_workflow_outcome(self, website, goal, outcome):
        pass


class FakeEngine:
    pass


class FakeMCP:
    def __init__(self, call_result=None, route_result=None):
        self._call_result = call_result
        self._route_result = route_result
        self.call_args = None
        self.route_args = None

    async def call(self, connector, tool, arguments):
        self.call_args = (connector, tool, arguments)
        return self._call_result

    async def route_and_call(self, request_text, arguments=None):
        self.route_args = (request_text, arguments)
        return self._route_result


def _loop(mcp):
    return AgentLoop(engine=FakeEngine(), memory=FakeMemory(), mcp=mcp, max_steps=5)


@pytest.mark.asyncio
async def test_explicit_connector_dot_tool_target_calls_mcp_call_directly():
    result = ToolCallResult(ok=True, connector="filesystem", tool="read_file", output={"content": "hi"})
    mcp = FakeMCP(call_result=result)
    loop = _loop(mcp)

    success, note = await loop._execute_mcp_tool("filesystem.read_file", '{"path": "notes.txt"}')

    assert mcp.call_args == ("filesystem", "read_file", {"path": "notes.txt"})
    assert mcp.route_args is None
    assert success is True
    assert note == "mcp[filesystem.read_file]: {'content': 'hi'}"


@pytest.mark.asyncio
async def test_free_text_target_calls_route_and_call():
    result = ToolCallResult(ok=True, connector="github", tool="list_issues", output=[])
    mcp = FakeMCP(route_result=result)
    loop = _loop(mcp)

    success, note = await loop._execute_mcp_tool("list open issues on this repo", "{}")

    assert mcp.route_args == ("list open issues on this repo", {})
    assert mcp.call_args is None
    assert success is True
    assert note == "mcp[github.list_issues]: []"


@pytest.mark.asyncio
async def test_free_text_target_no_match_returns_failure():
    mcp = FakeMCP(route_result=None)
    loop = _loop(mcp)

    success, note = await loop._execute_mcp_tool("do something vague", "")
    assert success is False
    assert "no MCP tool matched request" in note


@pytest.mark.asyncio
async def test_no_mcp_manager_configured_returns_failure():
    loop = _loop(mcp=None)
    success, note = await loop._execute_mcp_tool("filesystem.read_file", "{}")
    assert success is False
    assert "no MCPManager is configured" in note


@pytest.mark.asyncio
async def test_failed_tool_call_note_includes_error():
    result = ToolCallResult(ok=False, connector="terminal", tool="run_command", error="not allow-listed")
    mcp = FakeMCP(call_result=result)
    loop = _loop(mcp)

    success, note = await loop._execute_mcp_tool("terminal.run_command", '{"command": "rm -rf /"}')
    assert success is False
    assert note == "mcp[terminal.run_command]: not allow-listed"


@pytest.mark.asyncio
async def test_invalid_json_value_falls_back_to_empty_arguments():
    result = ToolCallResult(ok=True, connector="filesystem", tool="list_directory", output={})
    mcp = FakeMCP(call_result=result)
    loop = _loop(mcp)

    await loop._execute_mcp_tool("filesystem.list_directory", "not valid json{{{")
    assert mcp.call_args == ("filesystem", "list_directory", {})


@pytest.mark.asyncio
async def test_step_action_dispatch_routes_mcp_tool_through_execute_action():
    result = ToolCallResult(ok=True, connector="filesystem", tool="read_file", output={"content": "hi"})
    mcp = FakeMCP(call_result=result)
    loop = _loop(mcp)

    success, note = await loop._execute_action(
        StepAction.MCP_TOOL.value, "filesystem.read_file", '{"path": "notes.txt"}'
    )
    assert success is True
    assert "filesystem.read_file" in note
