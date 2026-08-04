import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.api import app_state
from backend.api.routes_memory import router as memory_router
from backend.config.settings import settings
from backend.database.models import MemoryEntry
from backend.database.session import get_session, init_db
from backend.memory.store import MemoryStore


class _FakeEmbeddingFunction:
    def __call__(self, input):  # noqa: A002
        return [[float((hash(text) % 1000)) / 1000.0] * 8 for text in input]

    def name(self):
        return "fake-test-embedding"


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "chroma_persist_dir", str(tmp_path / "chroma"))
    await init_db()
    async with get_session() as session:
        await session.execute(delete(MemoryEntry))

    app_state.state.memory = MemoryStore(embedding_function=_FakeEmbeddingFunction())

    app = FastAPI()
    app.include_router(memory_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    async with get_session() as session:
        await session.execute(delete(MemoryEntry))
    app_state.state.memory = None


@pytest.mark.asyncio
async def test_list_memories_empty(client):
    res = await client.get("/api/memory")
    assert res.status_code == 200
    assert res.json() == {"memories": []}


@pytest.mark.asyncio
async def test_save_then_list_and_get_and_analytics(client):
    await app_state.state.memory.save_preference("theme", "dark")

    res = await client.get("/api/memory")
    assert res.status_code == 200
    memories = res.json()["memories"]
    assert len(memories) == 1
    entry_id = memories[0]["id"]
    assert memories[0]["category"] == "profiles"

    res = await client.get(f"/api/memory/{entry_id}")
    assert res.status_code == 200
    assert res.json()["id"] == entry_id

    res = await client.get("/api/memory/analytics")
    assert res.status_code == 200
    stats = res.json()
    assert stats["total"] == 1
    assert stats["by_category"]["profiles"] == 1


@pytest.mark.asyncio
async def test_archive_unarchive_and_forget_flow(client):
    await app_state.state.memory.save_preference("theme", "dark")
    memories = (await client.get("/api/memory")).json()["memories"]
    entry_id = memories[0]["id"]

    res = await client.post(f"/api/memory/{entry_id}/archive")
    assert res.status_code == 200
    assert (await client.get("/api/memory")).json()["memories"] == []

    res = await client.post(f"/api/memory/{entry_id}/unarchive")
    assert res.status_code == 200
    assert len((await client.get("/api/memory")).json()["memories"]) == 1

    res = await client.delete(f"/api/memory/{entry_id}")
    assert res.status_code == 200
    assert res.json() == {"id": entry_id, "forgotten": True}

    res = await client.get(f"/api/memory/{entry_id}")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_duplicates_and_merge_endpoints(client):
    async with get_session() as session:
        from backend.memory.store import content_hash

        session.add(
            MemoryEntry(
                kind="preference",
                content="favorite color = blue",
                metadata_json={},
                confidence=0.5,
                category="profiles",
                importance=0.5,
                content_hash=content_hash("favorite color = blue"),
            )
        )
        session.add(
            MemoryEntry(
                kind="preference",
                content="favorite color = blue",
                metadata_json={},
                confidence=0.7,
                category="profiles",
                importance=0.6,
                content_hash=content_hash("favorite color = blue"),
            )
        )

    res = await client.get("/api/memory/duplicates")
    assert res.status_code == 200
    groups = res.json()["groups"]
    assert len(groups) == 1
    ids = [e["id"] for e in groups[0]]

    res = await client.post("/api/memory/duplicates/merge", json={"ids": ids})
    assert res.status_code == 200
    body = res.json()
    assert body["kept_id"] in ids
    assert len(body["removed_ids"]) == 1

    remaining = (await client.get("/api/memory")).json()["memories"]
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_expire_run_endpoint(client):
    res = await client.post("/api/memory/expire/run")
    assert res.status_code == 200
    assert res.json() == {"archived": 0, "forgotten": 0}


@pytest.mark.asyncio
async def test_bulk_archive_and_forget_endpoints(client):
    await app_state.state.memory.save_preference("a", "1")
    await app_state.state.memory.save_preference("b", "2")
    ids = [e["id"] for e in (await client.get("/api/memory")).json()["memories"]]

    res = await client.post("/api/memory/bulk/archive", json={"ids": ids})
    assert res.status_code == 200
    assert res.json() == {"archived": 2}

    res = await client.post("/api/memory/bulk/forget", json={"ids": ids})
    assert res.status_code == 200
    assert res.json() == {"forgotten": 2}


@pytest.mark.asyncio
async def test_search_endpoint_unchanged(client):
    res = await client.get("/api/memory/search", params={"q": "anything"})
    assert res.status_code == 200
    assert "results" in res.json()
