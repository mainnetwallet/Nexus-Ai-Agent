import pytest
import pytest_asyncio

from backend.mcp.base import ConnectorStatus, MCPToolError
from backend.mcp.connectors.filesystem import FilesystemMCPConnector


@pytest_asyncio.fixture
async def connector(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    conn = FilesystemMCPConnector(config={"roots": [str(root)]})
    await conn.connect()
    return conn, root


@pytest.mark.asyncio
async def test_connect_creates_root_and_marks_connected(tmp_path):
    root = tmp_path / "new_root"
    conn = FilesystemMCPConnector(config={"roots": [str(root)]})
    assert not root.exists()
    await conn.connect()
    assert root.exists()
    assert conn.status == ConnectorStatus.CONNECTED


@pytest.mark.asyncio
async def test_path_traversal_outside_roots_is_rejected(connector):
    conn, root = connector
    with pytest.raises(MCPToolError, match="outside the allowed roots"):
        await conn.call_tool("read_file", {"path": "../../etc/passwd"})


@pytest.mark.asyncio
async def test_absolute_path_outside_roots_is_rejected(connector):
    conn, root = connector
    with pytest.raises(MCPToolError, match="outside the allowed roots"):
        await conn.call_tool("read_file", {"path": "/etc/passwd"})


@pytest.mark.asyncio
async def test_write_read_search_delete_round_trip(connector):
    conn, root = connector

    write_result = await conn.call_tool(
        "write_file", {"path": "notes/todo.txt", "content": "buy milk"}
    )
    assert write_result["bytes_written"] == len("buy milk".encode("utf-8"))
    assert (root / "notes" / "todo.txt").read_text() == "buy milk"

    read_result = await conn.call_tool("read_file", {"path": "notes/todo.txt"})
    assert read_result["content"] == "buy milk"
    assert read_result["truncated"] is False

    search_result = await conn.call_tool(
        "search_files", {"root": ".", "pattern": "*.txt"}
    )
    assert any(m.endswith("todo.txt") for m in search_result["matches"])

    list_result = await conn.call_tool("list_directory", {"path": ".", "recursive": True})
    names = [e["name"] for e in list_result["entries"]]
    assert any("todo.txt" in n for n in names)

    delete_result = await conn.call_tool("delete_file", {"path": "notes/todo.txt"})
    assert delete_result["deleted"] is True
    assert not (root / "notes" / "todo.txt").exists()


@pytest.mark.asyncio
async def test_append_mode_appends_rather_than_overwrites(connector):
    conn, root = connector
    await conn.call_tool("write_file", {"path": "log.txt", "content": "line1\n"})
    await conn.call_tool("write_file", {"path": "log.txt", "content": "line2\n", "append": True})
    assert (root / "log.txt").read_text() == "line1\nline2\n"


@pytest.mark.asyncio
async def test_read_nonexistent_file_raises_tool_error(connector):
    conn, root = connector
    with pytest.raises(MCPToolError, match="does not exist"):
        await conn.call_tool("read_file", {"path": "missing.txt"})


@pytest.mark.asyncio
async def test_delete_directory_raises_tool_error(connector):
    conn, root = connector
    (root / "subdir").mkdir()
    with pytest.raises(MCPToolError, match="cannot delete a directory"):
        await conn.call_tool("delete_file", {"path": "subdir"})


@pytest.mark.asyncio
async def test_unknown_tool_raises_tool_error(connector):
    conn, root = connector
    with pytest.raises(MCPToolError, match="unknown tool"):
        await conn.call_tool("not_a_real_tool", {})
