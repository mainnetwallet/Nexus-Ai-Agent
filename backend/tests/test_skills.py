"""
Tests for the Skill Learning System: backend/skills/{library,matcher,teach}.py
and backend/api/routes_skills.py.

SkillService talks to a real ChromaDB PersistentClient for semantic indexing,
but every call site wraps that in try/except and degrades gracefully (see
library.py's _reindex/semantic_search), so no network/embedding mocking is
required for CRUD correctness. We still stub the collection with an in-memory
fake to keep these tests fast and fully offline.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.database.models import Skill, SkillVersion
from backend.database.session import get_session, init_db
from backend.skills.library import SkillService
from backend.skills.matcher import SkillMatcher
from backend.skills.teach import TeachModeManager


class FakeCollection:
    """Stands in for the chromadb collection so tests never touch the
    network / try to download the embedding model."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    def upsert(self, ids, documents, metadatas):
        for i, doc, meta in zip(ids, documents, metadatas):
            self.docs[i] = {"document": doc, "metadata": meta}

    def delete(self, ids):
        for i in ids:
            self.docs.pop(i, None)

    def query(self, query_texts, n_results=5):
        ids = list(self.docs.keys())[:n_results]
        return {"ids": [ids], "distances": [[0.1 for _ in ids]]}


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with get_session() as session:
        await session.execute(delete(SkillVersion))
        await session.execute(delete(Skill))
    yield
    async with get_session() as session:
        await session.execute(delete(SkillVersion))
        await session.execute(delete(Skill))


@pytest.fixture
def library() -> SkillService:
    svc = SkillService()
    svc._collection = FakeCollection()
    return svc


# ---------------------------------------------------------------- #
# SkillService: CRUD
# ---------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_create_skill_persists_and_versions(library: SkillService):
    skill = await library.create(
        name="Check gas price",
        description="Reads the gwei value from Etherscan.",
        trigger="check the gas price\nwhat's gwei right now",
        website_hint="etherscan.io",
        workflow=[{"action": "navigate", "target": "https://etherscan.io/gastracker", "value": "", "description": ""}],
    )

    assert skill["name"] == "Check gas price"
    assert skill["version"] == 1
    assert skill["enabled"] is True

    versions = await library.versions(skill["id"])
    assert len(versions) == 1
    assert versions[0]["change_note"].startswith("created via")


@pytest.mark.asyncio
async def test_create_defaults_untitled_name_when_blank(library: SkillService):
    skill = await library.create(name="   ")
    assert skill["name"] == "Untitled skill"


@pytest.mark.asyncio
async def test_get_returns_none_for_missing_skill(library: SkillService):
    assert await library.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_list_filters_by_category_enabled_and_search(library: SkillService):
    a = await library.create(name="Swap tokens", category="defi", trigger="swap eth for usdc")
    b = await library.create(name="Post a tweet", category="social", trigger="tweet something")
    await library.set_enabled(b["id"], False)

    defi_only = await library.list(category="defi")
    assert [s["id"] for s in defi_only] == [a["id"]]

    enabled_only = await library.list(enabled_only=True)
    assert b["id"] not in [s["id"] for s in enabled_only]

    searched = await library.list(search="tweet")
    assert [s["id"] for s in searched] == [b["id"]]


@pytest.mark.asyncio
async def test_update_bumps_version_and_snapshots(library: SkillService):
    skill = await library.create(name="Original", description="v1")
    updated = await library.update(skill["id"], {"description": "v2"}, change_note="tweaked description")

    assert updated["version"] == 2
    assert updated["description"] == "v2"

    versions = await library.versions(skill["id"])
    assert len(versions) == 2
    # versions() orders newest-first
    assert versions[0]["change_note"] == "tweaked description"
    assert versions[0]["version"] == 2


@pytest.mark.asyncio
async def test_update_missing_skill_returns_none(library: SkillService):
    assert await library.update("nope", {"description": "x"}) is None


@pytest.mark.asyncio
async def test_delete_removes_skill_and_cascades_versions(library: SkillService):
    skill = await library.create(name="Temp")
    ok = await library.delete(skill["id"])
    assert ok is True
    assert await library.get(skill["id"]) is None

    async with get_session() as session:
        remaining = await session.get(SkillVersion, skill["id"])
        assert remaining is None


@pytest.mark.asyncio
async def test_delete_missing_skill_returns_false(library: SkillService):
    assert await library.delete("nope") is False


@pytest.mark.asyncio
async def test_rename_and_duplicate(library: SkillService):
    skill = await library.create(name="Original", trigger="do the thing")
    renamed = await library.rename(skill["id"], "Renamed")
    assert renamed["name"] == "Renamed"

    dup = await library.duplicate(skill["id"], "Renamed copy")
    assert dup["id"] != skill["id"]
    assert dup["name"] == "Renamed copy"
    assert dup["trigger"] == "do the thing"


@pytest.mark.asyncio
async def test_set_enabled_toggles_flag(library: SkillService):
    skill = await library.create(name="Togglable")
    disabled = await library.set_enabled(skill["id"], False)
    assert disabled["enabled"] is False
    enabled = await library.set_enabled(skill["id"], True)
    assert enabled["enabled"] is True


@pytest.mark.asyncio
async def test_record_usage_updates_success_rate_and_count(library: SkillService):
    skill = await library.create(name="Tracked")
    await library.record_usage(skill["id"], True)
    await library.record_usage(skill["id"], False)

    refreshed = await library.get(skill["id"])
    assert refreshed["usage_count"] == 2
    assert 0.0 <= refreshed["success_rate"] <= 1.0


# ---------------------------------------------------------------- #
# SkillService: version rollback
# ---------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rollback_restores_previous_snapshot(library: SkillService):
    skill = await library.create(name="Original", description="v1", trigger="v1 trigger")
    await library.update(skill["id"], {"description": "v2", "trigger": "v2 trigger"})

    rolled_back = await library.rollback(skill["id"], 1)

    assert rolled_back["description"] == "v1"
    assert rolled_back["trigger"] == "v1 trigger"
    # rollback itself is a new version, not a rewrite of history
    assert rolled_back["version"] == 3


@pytest.mark.asyncio
async def test_rollback_missing_version_returns_none(library: SkillService):
    skill = await library.create(name="Original")
    assert await library.rollback(skill["id"], 99) is None


# ---------------------------------------------------------------- #
# SkillService: export / share / import
# ---------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_export_then_import_round_trips_portable_fields(library: SkillService):
    skill = await library.create(
        name="Exportable",
        description="desc",
        category="research",
        trigger="look something up",
        workflow=[{"action": "navigate", "target": "https://example.com", "value": "", "description": ""}],
    )
    payload = await library.export_skill(skill["id"])
    assert payload is not None

    imported = await library.import_skill(payload)
    assert imported["id"] != skill["id"]
    assert imported["name"] == "Exportable"
    assert imported["workflow"] == skill["workflow"]


@pytest.mark.asyncio
async def test_share_code_round_trips_through_import_from_code(library: SkillService):
    skill = await library.create(name="Shareable", trigger="share me")
    code = await library.share_code(skill["id"])
    assert code

    imported = await library.import_from_code(code)
    assert imported["name"] == "Shareable"
    assert imported["id"] != skill["id"]


@pytest.mark.asyncio
async def test_import_from_code_rejects_garbage():
    library = SkillService()
    library._collection = FakeCollection()
    with pytest.raises(ValueError):
        await library.import_from_code("not-valid-base64-json!!")


@pytest.mark.asyncio
async def test_import_recorded_workflow_creates_skill_from_plain_steps(library: SkillService):
    skill = await library.import_recorded_workflow(
        name="Recorded flow",
        steps=[{"action": "click", "target": "#submit", "value": "", "description": "click submit"}],
        description="from a recording",
        category="general",
        trigger="run the recorded flow",
    )
    assert skill["name"] == "Recorded flow"
    assert skill["source"] == "recorded_workflow"
    assert len(skill["workflow"]) == 1


# ---------------------------------------------------------------- #
# SkillService: pending "save as skill?" suggestions
# ---------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pending_lifecycle_register_confirm(library: SkillService):
    library.register_pending(
        "task-1",
        {"name": "Learned flow", "description": "auto", "category": "general", "trigger": "goal text", "workflow": []},
    )
    pending = library.list_pending()
    assert len(pending) == 1
    assert pending[0]["task_id"] == "task-1"

    skill = await library.confirm_pending("task-1")
    assert skill["name"] == "Learned flow"
    assert skill["source"] == "task_outcome"
    assert library.list_pending() == []


@pytest.mark.asyncio
async def test_pending_discard_removes_without_creating_skill(library: SkillService):
    library.register_pending("task-2", {"name": "Discard me", "workflow": []})
    ok = library.discard_pending("task-2")
    assert ok is True
    assert library.list_pending() == []


@pytest.mark.asyncio
async def test_confirm_pending_with_no_argument_uses_latest():
    library = SkillService()
    library._collection = FakeCollection()
    library.register_pending("task-a", {"name": "Older", "workflow": []})
    library.register_pending("task-b", {"name": "Newer", "workflow": []})

    skill = await library.confirm_pending()
    assert skill["name"] == "Newer"


# ---------------------------------------------------------------- #
# SkillService: semantic search degrades gracefully
# ---------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_semantic_search_returns_scored_matches(library: SkillService):
    skill = await library.create(name="Findable", trigger="find this one")
    results = await library.semantic_search("find this one")
    assert any(r["skill_id"] == skill["id"] for r in results)


@pytest.mark.asyncio
async def test_semantic_search_swallows_backend_errors():
    library = SkillService()

    class BrokenCollection:
        def query(self, *a, **kw):
            raise RuntimeError("embedding model unavailable")

    library._collection = BrokenCollection()
    results = await library.semantic_search("anything")
    assert results == []


# ---------------------------------------------------------------- #
# SkillMatcher
# ---------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_matcher_keyword_match_wins_over_semantic(library: SkillService):
    await library.create(name="Gas checker", trigger="check the gas price", website_hint="etherscan.io")
    matcher = SkillMatcher(library)

    match = await matcher.find_match("please check the gas price for me", website="https://etherscan.io/gastracker")
    assert match is not None
    assert match["name"] == "Gas checker"


@pytest.mark.asyncio
async def test_matcher_respects_website_hint_incompatibility(library: SkillService):
    await library.create(name="Gas checker", trigger="check the gas price", website_hint="etherscan.io")
    matcher = SkillMatcher(library)

    match = await matcher.find_match("check the gas price", website="https://opensea.io")
    assert match is None


@pytest.mark.asyncio
async def test_matcher_ignores_disabled_skills(library: SkillService):
    skill = await library.create(name="Disabled skill", trigger="do the disabled thing")
    await library.set_enabled(skill["id"], False)
    matcher = SkillMatcher(library)

    match = await matcher.find_match("do the disabled thing")
    assert match is None


@pytest.mark.asyncio
async def test_matcher_returns_none_for_empty_goal(library: SkillService):
    matcher = SkillMatcher(library)
    assert await matcher.find_match("") is None
    assert await matcher.find_match("   ") is None


@pytest.mark.asyncio
async def test_matcher_returns_none_when_no_skills_exist(library: SkillService):
    matcher = SkillMatcher(library)
    assert await matcher.find_match("do anything at all") is None


# ---------------------------------------------------------------- #
# TeachModeManager
# ---------------------------------------------------------------- #
@pytest.fixture
def teach() -> TeachModeManager:
    mgr = TeachModeManager()
    mgr.llm.complete_json = AsyncMock()
    return mgr


def test_teach_start_and_is_active(teach: TeachModeManager):
    assert teach.is_active("s1") is False
    draft = teach.start("s1", name="My skill", website_hint="example.com")
    assert teach.is_active("s1") is True
    assert draft.name == "My skill"


def test_teach_cancel_clears_draft(teach: TeachModeManager):
    teach.start("s1")
    assert teach.cancel("s1") is True
    assert teach.is_active("s1") is False
    assert teach.cancel("s1") is False


def test_teach_add_step_raw_and_undo(teach: TeachModeManager):
    teach.start("s1")
    teach.add_step_raw("s1", action="click", target="#go", value="", description="click go")
    draft = teach.get_draft("s1")
    assert len(draft.steps) == 1

    assert teach.undo_last_step("s1") is True
    assert len(teach.get_draft("s1").steps) == 0
    assert teach.undo_last_step("s1") is False


@pytest.mark.asyncio
async def test_teach_add_step_from_text_uses_llm(teach: TeachModeManager):
    teach.start("s1")
    teach.llm.complete_json.return_value = {
        "action": "type",
        "target": "#email",
        "value": "test@example.com",
        "description": "fill email",
    }
    step = await teach.add_step_from_text("s1", "type my email in the email field")
    assert step["action"] == "type"
    assert teach.get_draft("s1").steps == [step]


@pytest.mark.asyncio
async def test_teach_add_step_from_text_no_active_session_returns_none(teach: TeachModeManager):
    assert await teach.add_step_from_text("nope", "click something") is None


@pytest.mark.asyncio
async def test_teach_add_step_from_text_llm_failure_returns_none(teach: TeachModeManager):
    teach.start("s1")
    teach.llm.complete_json.side_effect = RuntimeError("boom")
    assert await teach.add_step_from_text("s1", "click something") is None


def test_teach_finish_pops_draft(teach: TeachModeManager):
    teach.start("s1", name="Done skill")
    draft = teach.finish("s1")
    assert draft.name == "Done skill"
    assert teach.is_active("s1") is False


@pytest.mark.asyncio
async def test_parse_skill_from_text_success(teach: TeachModeManager):
    teach.llm.complete_json.return_value = {
        "name": "Parsed skill",
        "category": "general",
        "trigger": "do it",
        "workflow": [{"action": "click", "target": "#x", "value": "", "description": ""}],
    }
    draft = await teach.parse_skill_from_text("click the x button")
    assert draft["name"] == "Parsed skill"
    assert len(draft["workflow"]) == 1


@pytest.mark.asyncio
async def test_parse_skill_from_text_llm_failure_returns_empty_workflow(teach: TeachModeManager):
    teach.llm.complete_json.side_effect = RuntimeError("boom")
    draft = await teach.parse_skill_from_text("anything")
    assert draft["workflow"] == []


@pytest.mark.asyncio
async def test_parse_correction_success_and_failure(teach: TeachModeManager):
    teach.llm.complete_json.return_value = {"action": "type", "target": "#field", "value": "x", "description": "fix"}
    step = await teach.parse_correction("actually type x into the field")
    assert step["action"] == "type"

    teach.llm.complete_json.side_effect = RuntimeError("boom")
    fallback = await teach.parse_correction("some instruction")
    assert fallback["action"] == "click"
    assert fallback["description"] == "some instruction"


# ---------------------------------------------------------------- #
# API routes (backend/api/routes_skills.py) via app_state wiring
# ---------------------------------------------------------------- #
@pytest_asyncio.fixture
async def client(library: SkillService, teach: TeachModeManager):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from backend.api import app_state
    from backend.api.routes_skills import router as skills_router

    app_state.state.skills = library
    app_state.state.teach = teach
    app = FastAPI()
    app.include_router(skills_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app_state.state.skills = None
    app_state.state.teach = None


@pytest.mark.asyncio
async def test_route_create_and_list_skills(client):
    resp = await client.post("/api/skills", json={"name": "Route created", "trigger": "run route test"})
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["name"] == "Route created"

    listed = await client.get("/api/skills")
    assert listed.status_code == 200
    assert any(s["id"] == created["id"] for s in listed.json())


@pytest.mark.asyncio
async def test_route_get_missing_skill_404(client):
    resp = await client.get("/api/skills/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_route_enable_disable_missing_skill_404(client):
    resp = await client.post("/api/skills/does-not-exist/disable")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_route_pending_confirm_and_discard(client):
    library_instance = (await client.get("/api/skills")).json()
    assert library_instance == []

    from backend.api import app_state

    app_state.state.skills.register_pending("task-route", {"name": "Route pending", "workflow": []})

    pending = await client.get("/api/skills/pending")
    assert len(pending.json()) == 1

    confirmed = await client.post("/api/skills/pending/task-route/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["name"] == "Route pending"

    discard_missing = await client.post("/api/skills/pending/task-route/discard")
    assert discard_missing.json()["ok"] is False


@pytest.mark.asyncio
async def test_route_teach_lifecycle(client):
    start = await client.post("/api/skills/teach/session-x/start", json={"name": "Taught via API"})
    assert start.status_code == 200
    assert start.json()["draft"]["name"] == "Taught via API"

    status = await client.get("/api/skills/teach/session-x")
    assert status.json()["active"] is True

    cancelled = await client.post("/api/skills/teach/session-x/cancel")
    assert cancelled.json()["ok"] is True

    status_after = await client.get("/api/skills/teach/session-x")
    assert status_after.json()["active"] is False


@pytest.mark.asyncio
async def test_route_learn_from_text_no_workflow_returns_not_created(client):
    from backend.api import app_state

    app_state.state.teach.llm.complete_json.return_value = {"name": "", "workflow": []}
    resp = await client.post("/api/skills/learn", json={"text": "vague description"})
    assert resp.status_code == 200
    assert resp.json()["created"] is False
