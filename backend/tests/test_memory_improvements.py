import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.config.settings import settings
from backend.database.models import MemoryEntry
from backend.database.session import get_session, init_db
from backend.memory.store import (
    MemoryStore,
    compute_base_importance,
    content_hash,
    effective_importance,
    infer_category,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with get_session() as session:
        await session.execute(delete(MemoryEntry))
    yield
    async with get_session() as session:
        await session.execute(delete(MemoryEntry))


class _FakeEmbeddingFunction:
    """Deterministic, network-free stand-in for chromadb's default ONNX
    embedding function (see test_memory_store_mcp.py for the original)."""

    def __call__(self, input):  # noqa: A002
        return [[float((hash(text) % 1000)) / 1000.0] * 8 for text in input]

    def name(self):
        return "fake-test-embedding"


@pytest.fixture
def memory_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "chroma_persist_dir", str(tmp_path / "chroma"))
    return MemoryStore(embedding_function=_FakeEmbeddingFunction())


# ------------------------------------------------------------------ #
# Pure helpers
# ------------------------------------------------------------------ #
def test_infer_category_defaults_by_kind():
    assert infer_category("preference", None) == "profiles"
    assert infer_category("mcp_call", None) == "coding"
    assert infer_category("workflow", None) == "browser"
    assert infer_category("unknown_kind", None) == "general"


def test_infer_category_explicit_override_wins():
    assert infer_category("workflow", {"category": "tasks"}) == "tasks"


def test_content_hash_normalizes_whitespace_and_case():
    assert content_hash("Hello   World") == content_hash("hello world")
    assert content_hash("a") != content_hash("b")


def test_compute_base_importance_bounded_and_status_sensitive():
    succeeded = compute_base_importance("workflow", 0.8, {"status": "succeeded"})
    failed = compute_base_importance("workflow", 0.2, {"status": "failed"})
    assert 0.0 <= succeeded <= 1.0
    assert 0.0 <= failed <= 1.0
    assert succeeded > failed


def test_effective_importance_decays_with_age_and_boosts_with_access():
    now = dt.datetime.now(dt.timezone.utc)
    fresh = effective_importance(
        importance=0.6, access_count=0, created_at=now, last_accessed_at=None, now=now
    )
    old = effective_importance(
        importance=0.6,
        access_count=0,
        created_at=now - dt.timedelta(days=180),
        last_accessed_at=None,
        now=now,
    )
    frequently_used = effective_importance(
        importance=0.6, access_count=20, created_at=now, last_accessed_at=now, now=now
    )
    assert old < fresh
    assert frequently_used > fresh


# ------------------------------------------------------------------ #
# Write path: category + importance are stamped automatically
# ------------------------------------------------------------------ #
@pytest.mark.asyncio
async def test_save_preference_is_categorized_and_scored(memory_store):
    await memory_store.save_preference("theme", "dark")

    memories = await memory_store.list_memories()
    assert len(memories) == 1
    entry = memories[0]
    assert entry["category"] == "profiles"
    assert 0.0 <= entry["importance"] <= 1.0
    assert entry["access_count"] == 0
    assert entry["archived"] is False


# ------------------------------------------------------------------ #
# Duplicate Memory Detection
# ------------------------------------------------------------------ #
@pytest.mark.asyncio
async def test_duplicate_preference_folds_into_existing_row(memory_store):
    await memory_store.save_preference("theme", "dark")
    await memory_store.save_preference("theme", "dark")  # exact duplicate write

    memories = await memory_store.list_memories()
    assert len(memories) == 1
    assert memories[0]["access_count"] == 1  # bumped, not duplicated
    assert memories[0]["merged_count"] == 1


@pytest.mark.asyncio
async def test_find_duplicate_groups_and_merge(memory_store):
    async with get_session() as session:
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

    groups = await memory_store.find_duplicate_groups()
    assert len(groups) == 1
    assert len(groups[0]) == 2

    ids = [e["id"] for e in groups[0]]
    result = await memory_store.merge_duplicates(ids)
    assert result["kept_id"] in ids
    assert len(result["removed_ids"]) == 1

    remaining = await memory_store.list_memories()
    assert len(remaining) == 1
    assert remaining[0]["confidence"] == 0.7


@pytest.mark.asyncio
async def test_merge_duplicates_requires_two_ids(memory_store):
    with pytest.raises(ValueError):
        await memory_store.merge_duplicates(["only-one-id"])


# ------------------------------------------------------------------ #
# Archive / Forget
# ------------------------------------------------------------------ #
@pytest.mark.asyncio
async def test_archive_hides_from_default_listing_but_is_recoverable(memory_store):
    await memory_store.save_preference("theme", "dark")
    entry_id = (await memory_store.list_memories())[0]["id"]

    assert await memory_store.archive_memory(entry_id) is True
    assert await memory_store.list_memories() == []
    assert len(await memory_store.list_memories(include_archived=True)) == 1

    assert await memory_store.unarchive_memory(entry_id) is True
    assert len(await memory_store.list_memories()) == 1


@pytest.mark.asyncio
async def test_forget_permanently_deletes(memory_store):
    await memory_store.save_preference("theme", "dark")
    entry_id = (await memory_store.list_memories())[0]["id"]

    assert await memory_store.forget_memory(entry_id) is True
    assert await memory_store.get_memory(entry_id) is None
    assert await memory_store.forget_memory(entry_id) is False  # already gone


@pytest.mark.asyncio
async def test_bulk_archive_and_forget(memory_store):
    await memory_store.save_preference("a", "1")
    await memory_store.save_preference("b", "2")
    ids = [e["id"] for e in await memory_store.list_memories()]

    archived = await memory_store.bulk_archive(ids)
    assert archived == 2

    forgotten = await memory_store.bulk_forget(ids)
    assert forgotten == 2
    assert await memory_store.list_memories(include_archived=True) == []


# ------------------------------------------------------------------ #
# Expiration Policy
# ------------------------------------------------------------------ #
@pytest.mark.asyncio
async def test_expiration_sweep_archives_old_low_value_memories(memory_store, monkeypatch):
    monkeypatch.setattr(settings, "memory_expiration_days", 30)
    monkeypatch.setattr(settings, "memory_low_importance_threshold", 0.9)  # everything qualifies
    monkeypatch.setattr(settings, "memory_expire_action", "archive")

    async with get_session() as session:
        session.add(
            MemoryEntry(
                kind="mcp_call",
                content="old low value call",
                metadata_json={},
                confidence=0.1,
                category="coding",
                importance=0.1,
                created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=100),
            )
        )

    result = await memory_store.run_expiration_sweep()
    assert result["archived"] == 1
    assert await memory_store.list_memories() == []


@pytest.mark.asyncio
async def test_expiration_sweep_leaves_recent_or_high_value_memories(memory_store):
    await memory_store.save_preference("theme", "dark")  # fresh -> not eligible
    result = await memory_store.run_expiration_sweep()
    assert result == {"archived": 0, "forgotten": 0}
    assert len(await memory_store.list_memories()) == 1


# ------------------------------------------------------------------ #
# Analytics
# ------------------------------------------------------------------ #
@pytest.mark.asyncio
async def test_analytics_reports_counts_by_category(memory_store):
    await memory_store.save_preference("theme", "dark")
    await memory_store.save_workflow_outcome(
        "example.com",
        "claim reward",
        type("Outcome", (), {"status": "succeeded", "steps": [], "summary": "done"})(),
    )

    stats = await memory_store.get_analytics()
    assert stats["total"] == 2
    assert stats["active"] == 2
    assert stats["archived"] == 0
    assert stats["by_category"]["profiles"] == 1
    assert stats["by_category"]["browser"] == 1
    assert 0.0 <= stats["average_importance"] <= 1.0
    assert "growth_last_14_days" in stats
