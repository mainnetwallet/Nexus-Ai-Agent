"""Discord connector confirm-gate coverage.

`send_message`, `reply`, and `upload_file` are outward-facing, irreversible
actions on Discord's medium of record -- they must refuse to run until the
caller passes `confirm=true`, mirroring the X and Gmail connectors. Read
tools must NOT require confirm. No live browser is needed: the gate fires
before any session work, so a missing engine surfaces only after confirm
passes.
"""
import pytest
from backend.mcp.base import MCPToolError
from backend.mcp.connectors.discord_connector import DiscordMCPConnector


@pytest.fixture
def conn() -> DiscordMCPConnector:
    return DiscordMCPConnector(config={})


@pytest.mark.asyncio
async def test_send_message_requires_confirm(conn):
    with pytest.raises(MCPToolError, match="requires explicit user confirmation"):
        await conn.call_tool("send_message", {"channel_url": "c", "text": "hi"})


@pytest.mark.asyncio
async def test_reply_requires_confirm(conn):
    with pytest.raises(MCPToolError, match="requires explicit user confirmation"):
        await conn.call_tool("reply", {"channel_url": "c", "text": "hi"})


@pytest.mark.asyncio
async def test_upload_file_requires_confirm(conn):
    with pytest.raises(MCPToolError, match="requires explicit user confirmation"):
        await conn.call_tool("upload_file", {"channel_url": "c", "file_path": "/tmp/x.txt"})


@pytest.mark.asyncio
async def test_send_message_confirm_true_passes_gate_to_session(conn):
    # confirm=true clears the gate; the next failure is the missing live
    # browser engine, NOT a confirm error -- proving the gate was passed.
    with pytest.raises(MCPToolError, match="live browser"):
        await conn.call_tool(
            "send_message", {"channel_url": "c", "text": "hi", "confirm": True}
        )


@pytest.mark.asyncio
async def test_reply_confirm_true_passes_gate_to_session(conn):
    with pytest.raises(MCPToolError, match="live browser"):
        await conn.call_tool("reply", {"channel_url": "c", "text": "hi", "confirm": True})


@pytest.mark.asyncio
async def test_upload_file_confirm_true_passes_gate_to_session(conn):
    with pytest.raises(MCPToolError, match="live browser"):
        await conn.call_tool(
            "upload_file",
            {"channel_url": "c", "file_path": "/tmp/x.txt", "confirm": True},
        )


@pytest.mark.asyncio
async def test_read_tools_do_not_require_confirm(conn):
    # detect_login_state returns a status dict even with no engine (open),
    # without ever asking for confirmation.
    result = await conn.call_tool("detect_login_state", {})
    assert result["session_status"] in ("connected", "login_required", "expired", "unknown")
