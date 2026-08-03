from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from backend.config.settings import settings
from backend.database.models import MemoryEntry
from backend.database.session import get_session, init_db
from backend.mcp.base import ToolCallResult
from backend.memory.store import MemoryStore


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with get_session() as session:
        await session.execute(delete(MemoryEntry).where(MemoryEntry.kind == "mcp_call"))
    yield
    async with get_session() as session:
        await session.execute(delete(MemoryEntry).where(MemoryEntry.kind == "mcp_call"))


class _FakeEmbeddingFunction:
    """Deterministic, network-free stand-in for chromadb's default ONNX
    embedding function. Test environments (and any deployment without
    egress to chroma's model bucket) shouldn't have to download a model
    just to upsert a row; only chromadb's presence/shape of the embedding
    matters here, not its semantic quality."""

    def __call__(self, input):  # noqa: A002 - chromadb's own param name
        return [[float((hash(text) % 1000)) / 1000.0] * 8 for text in input]

    def name(self):
        return "fake-test-embedding"


@pytest.fixture
def memory_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "chroma_persist_dir", str(tmp_path / "chroma"))
    return MemoryStore(embedding_function=_FakeEmbeddingFunction())


@pytest.mark.asyncio
async def test_save_tool_call_writes_mcp_call_entry(memory_store):
    result = ToolCallResult(ok=True, connector="filesystem", tool="read_file", output={"content": "hi"})
    await memory_store.save_tool_call("filesystem", "read_file", {"path": "notes.txt"}, result)

    async with get_session() as session:
        rows = (await session.execute(select(MemoryEntry).where(MemoryEntry.kind == "mcp_call"))).scalars().all()

    assert len(rows) == 1
    entry = rows[0]
    assert entry.kind == "mcp_call"
    assert entry.metadata_json["connector"] == "filesystem"
    assert entry.metadata_json["tool"] == "read_file"
    assert entry.metadata_json["arguments"] == {"path": "notes.txt"}
    assert entry.metadata_json["ok"] is True
    assert "filesystem.read_file" in entry.content


@pytest.mark.asyncio
async def test_save_tool_call_records_failure_details(memory_store):
    result = ToolCallResult(ok=False, connector="terminal", tool="run_command", error="not allow-listed")
    await memory_store.save_tool_call("terminal", "run_command", {"command": "rm -rf /"}, result)

    async with get_session() as session:
        rows = (await session.execute(select(MemoryEntry).where(MemoryEntry.kind == "mcp_call"))).scalars().all()

    assert len(rows) == 1
    entry = rows[0]
    assert entry.metadata_json["ok"] is False
    assert "not allow-listed" in entry.content


@pytest.mark.asyncio
async def test_save_tool_call_handles_result_without_standard_attrs(memory_store):
    # `result` is documented as Any -- a plain object without ok/output/error
    # attributes should still be recorded gracefully rather than raising.
    result = SimpleNamespace()
    await memory_store.save_tool_call("browser", "fetch_url", {"url": "https://example.com"}, result)

    async with get_session() as session:
        rows = (await session.execute(select(MemoryEntry).where(MemoryEntry.kind == "mcp_call"))).scalars().all()

    assert len(rows) == 1
    assert rows[0].metadata_json["ok"] is None
