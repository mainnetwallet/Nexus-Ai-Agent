import pytest

from backend.mcp.base import ConnectorStatus, MCPToolError
from backend.mcp.connectors.terminal import DEFAULT_ALLOWED_COMMANDS, TerminalMCPConnector


@pytest.mark.asyncio
async def test_disabled_by_default_even_without_enabled_key(tmp_path):
    conn = TerminalMCPConnector(config={"cwd": str(tmp_path)})
    await conn.connect()
    assert conn.status == ConnectorStatus.DISABLED
    assert "disabled" in conn.last_error


@pytest.mark.asyncio
async def test_enabled_false_explicitly_still_disabled(tmp_path):
    conn = TerminalMCPConnector(config={"cwd": str(tmp_path), "enabled": False})
    await conn.connect()
    assert conn.status == ConnectorStatus.DISABLED


@pytest.mark.asyncio
async def test_config_enabled_true_at_construction_connects(tmp_path):
    conn = TerminalMCPConnector(config={"cwd": str(tmp_path), "enabled": True})
    await conn.connect()
    assert conn.status == ConnectorStatus.CONNECTED


@pytest.mark.asyncio
async def test_default_allow_list_used_when_none_configured(tmp_path):
    conn = TerminalMCPConnector(config={"cwd": str(tmp_path), "enabled": True})
    await conn.connect()
    result = await conn.call_tool("list_allowed_commands", {})
    assert set(result["allowed_commands"]) == set(DEFAULT_ALLOWED_COMMANDS)


@pytest.mark.asyncio
async def test_allow_list_rejection(tmp_path):
    conn = TerminalMCPConnector(
        config={"cwd": str(tmp_path), "enabled": True, "allowed_commands": ["echo"]}
    )
    await conn.connect()
    with pytest.raises(MCPToolError, match="not in the allow-list"):
        await conn.call_tool("run_command", {"command": "ls -la"})


@pytest.mark.asyncio
async def test_shell_metacharacter_rejection(tmp_path):
    conn = TerminalMCPConnector(
        config={"cwd": str(tmp_path), "enabled": True, "allowed_commands": ["echo"]}
    )
    await conn.connect()
    for dangerous in ["echo hi; rm -rf /", "echo hi | cat", "echo `whoami`", "echo $(whoami)", "echo hi &"]:
        with pytest.raises(MCPToolError, match="disallowed shell metacharacters"):
            await conn.call_tool("run_command", {"command": dangerous})


@pytest.mark.asyncio
async def test_empty_command_raises_tool_error(tmp_path):
    conn = TerminalMCPConnector(config={"cwd": str(tmp_path), "enabled": True})
    await conn.connect()
    with pytest.raises(MCPToolError, match="command is required"):
        await conn.call_tool("run_command", {"command": ""})


@pytest.mark.asyncio
async def test_allow_listed_command_runs_and_returns_exit_code_0(tmp_path):
    conn = TerminalMCPConnector(
        config={"cwd": str(tmp_path), "enabled": True, "allowed_commands": ["echo"]}
    )
    await conn.connect()
    result = await conn.call_tool("run_command", {"command": "echo hi"})
    assert result["exit_code"] == 0
    assert "hi" in result["stdout"]


@pytest.mark.asyncio
async def test_unknown_executable_raises_tool_error(tmp_path):
    conn = TerminalMCPConnector(
        config={"cwd": str(tmp_path), "enabled": True, "allowed_commands": ["totally-not-a-real-binary"]}
    )
    await conn.connect()
    with pytest.raises(MCPToolError, match="executable not found"):
        await conn.call_tool("run_command", {"command": "totally-not-a-real-binary --version"})
