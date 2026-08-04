import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from backend.database.models import ProfileActivity, ProfileRecord, Report, Task, TaskStatus
from backend.database.session import get_session, init_db
from backend.identity.manager import ProfileManager
from backend.identity.registry import ProfileRegistry
from backend.planner import task_queue as task_queue_module
from backend.planner.agent_loop import TaskOutcome
from backend.planner.task_queue import TaskQueueService


class FakeMemory:
    async def recall_similar_workflows(self, website, goal, top_k=3):
        return []

    async def save_workflow_outcome(self, website, goal, outcome):
        pass


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    yield
    async with get_session() as session:
        await session.execute(delete(Report))
        await session.execute(delete(Task))
        await session.execute(delete(ProfileActivity))
        await session.execute(delete(ProfileRecord))


def make_manager(tmp_path):
    registry = ProfileRegistry(data_dir=tmp_path)
    return ProfileManager(registry), registry


def make_queue(profiles=None) -> TaskQueueService:
    return TaskQueueService(memory=FakeMemory(), wallet=None, profiles=profiles)


class FakeAgentLoop:
    """Stand-in for backend.planner.task_queue.AgentLoop -- captures the
    wallet_label argument .run() was called with instead of doing any real
    planning/browser work."""

    last_wallet_label = None
    last_website = None

    def __init__(self, **kwargs):
        pass

    async def run(self, website, goal, wallet_label, notes):
        FakeAgentLoop.last_website = website
        FakeAgentLoop.last_wallet_label = wallet_label
        return TaskOutcome(status="succeeded", steps=[])


@pytest.mark.asyncio
async def test_enqueue_persists_profile_label():
    queue = make_queue()
    task_id = await queue.enqueue("https://example.com", "sign up", None, "", profile_label="Profile-01")

    async with get_session() as session:
        task = await session.get(Task, task_id)
        assert task.profile_label == "Profile-01"


@pytest.mark.asyncio
async def test_run_task_uses_profile_wallet_label_when_task_wallet_label_unset(tmp_path, monkeypatch):
    manager, registry = make_manager(tmp_path)
    await registry.create_profile("Profile-01", wallet_label="profile-wallet")

    monkeypatch.setattr(task_queue_module.BrowserEngine, "start", _noop_start)
    monkeypatch.setattr(task_queue_module, "AgentLoop", FakeAgentLoop)

    queue = make_queue(profiles=manager)
    task_id = await queue.enqueue(
        "https://example.com", "goal", None, "", profile_label="Profile-01"
    )
    async with get_session() as session:
        task = await session.get(Task, task_id)

    await queue._run_task(task)

    assert FakeAgentLoop.last_wallet_label == "profile-wallet"


@pytest.mark.asyncio
async def test_run_task_task_wallet_label_wins_over_profile_wallet_label(tmp_path, monkeypatch):
    manager, registry = make_manager(tmp_path)
    await registry.create_profile("Profile-01", wallet_label="profile-wallet")

    monkeypatch.setattr(task_queue_module.BrowserEngine, "start", _noop_start)
    monkeypatch.setattr(task_queue_module, "AgentLoop", FakeAgentLoop)

    queue = make_queue(profiles=manager)
    task_id = await queue.enqueue(
        "https://example.com", "goal", "explicit-wallet", "", profile_label="Profile-01"
    )
    async with get_session() as session:
        task = await session.get(Task, task_id)

    await queue._run_task(task)

    assert FakeAgentLoop.last_wallet_label == "explicit-wallet"


@pytest.mark.asyncio
async def test_run_task_profile_not_found_fails_task_before_browser_start(tmp_path, monkeypatch):
    manager, registry = make_manager(tmp_path)

    async def _raise_if_called(self):
        raise AssertionError("BrowserEngine.start should not be called when profile load fails")

    monkeypatch.setattr(task_queue_module.BrowserEngine, "start", _raise_if_called)

    queue = make_queue(profiles=manager)
    task_id = await queue.enqueue(
        "https://example.com", "goal", None, "", profile_label="does-not-exist"
    )
    async with get_session() as session:
        task = await session.get(Task, task_id)

    await queue._run_task(task)

    async with get_session() as session:
        db_task = await session.get(Task, task_id)
        assert db_task.status == TaskStatus.FAILED

        report = (
            await session.execute(select(Report).where(Report.task_id == task_id))
        ).scalar_one_or_none()
        assert report is not None
        assert report.status == TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_run_task_profile_label_ignored_when_no_profile_manager_configured(monkeypatch):
    """Backward compatibility: profiles=None must behave exactly as before
    this feature existed -- profile_label is accepted but ignored, and the
    task still runs normally through BrowserEngine/AgentLoop."""
    monkeypatch.setattr(task_queue_module.BrowserEngine, "start", _noop_start)
    monkeypatch.setattr(task_queue_module, "AgentLoop", FakeAgentLoop)

    queue = make_queue(profiles=None)
    task_id = await queue.enqueue(
        "https://example.com", "goal", "explicit-wallet", "", profile_label="some-profile"
    )
    async with get_session() as session:
        task = await session.get(Task, task_id)

    await queue._run_task(task)

    async with get_session() as session:
        db_task = await session.get(Task, task_id)
        assert db_task.status == TaskStatus.SUCCEEDED

    assert FakeAgentLoop.last_wallet_label == "explicit-wallet"


async def _noop_start(self) -> None:
    return None


# ---------------------------------------------------------------------- #
# Multi-Profile Browser Management
# ---------------------------------------------------------------------- #

class SlowFakeAgentLoop:
    """Like FakeAgentLoop, but blocks on an asyncio.Event so a test can
    prove two _run_task() calls are genuinely in flight at the same time
    (both engines started) before either finishes."""

    started = asyncio.Event()
    finish = asyncio.Event()
    concurrent_starts = 0

    def __init__(self, **kwargs):
        pass

    async def run(self, website, goal, wallet_label, notes):
        SlowFakeAgentLoop.concurrent_starts += 1
        SlowFakeAgentLoop.started.set()
        await SlowFakeAgentLoop.finish.wait()
        return TaskOutcome(status="succeeded", steps=[])


@pytest.mark.asyncio
async def test_two_different_profiles_run_concurrently(tmp_path, monkeypatch):
    """Core Multi-Profile Browser Management guarantee: two tasks against
    two *different* Chrome Profiles both get a live BrowserEngine and run
    at the same time, instead of the second waiting for the first to
    finish (the old one-task-at-a-time worker loop)."""
    manager, registry = make_manager(tmp_path)
    await registry.create_profile("Profile-01")
    await registry.create_profile("Profile-02")

    monkeypatch.setattr(task_queue_module.BrowserEngine, "start", _noop_start)
    monkeypatch.setattr(task_queue_module.BrowserEngine, "stop", _noop_start)
    monkeypatch.setattr(task_queue_module, "AgentLoop", SlowFakeAgentLoop)
    SlowFakeAgentLoop.started = asyncio.Event()
    SlowFakeAgentLoop.finish = asyncio.Event()
    SlowFakeAgentLoop.concurrent_starts = 0

    queue = make_queue(profiles=manager)
    id1 = await queue.enqueue("https://a.example.com", "goal", None, "", profile_label="Profile-01")
    id2 = await queue.enqueue("https://b.example.com", "goal", None, "", profile_label="Profile-02")

    async with get_session() as session:
        task1 = await session.get(Task, id1)
        task2 = await session.get(Task, id2)

    run1 = asyncio.create_task(queue._run_task(task1))
    run2 = asyncio.create_task(queue._run_task(task2))

    # Both should be able to reach the "running" (blocked-in-agent-loop)
    # point without either having to wait for the other to release its
    # profile first -- proves they're concurrent, not sequential.
    await asyncio.wait_for(SlowFakeAgentLoop.started.wait(), timeout=2)
    await asyncio.sleep(0.05)  # let the second one catch up too
    assert len(queue.running) == 2
    assert {info["profile_id"] for info in queue.running.values()} == {
        (await registry.get_profile((await registry.resolve("Profile-01")).id)).id,
        (await registry.get_profile((await registry.resolve("Profile-02")).id)).id,
    }

    SlowFakeAgentLoop.finish.set()
    await asyncio.wait_for(asyncio.gather(run1, run2), timeout=2)

    assert queue.running == {}
    async with get_session() as session:
        assert (await session.get(Task, id1)).status == TaskStatus.SUCCEEDED
        assert (await session.get(Task, id2)).status == TaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_second_task_on_same_profile_requeues_instead_of_failing(tmp_path, monkeypatch):
    """The same Chrome Profile still can't run two tasks at once (Chrome
    only allows one process per profile directory) -- but hitting that
    should requeue the second task for a later attempt, not permanently
    fail it."""
    manager, registry = make_manager(tmp_path)
    created = await registry.create_profile("Profile-01")

    monkeypatch.setattr(task_queue_module.BrowserEngine, "start", _noop_start)
    monkeypatch.setattr(task_queue_module.BrowserEngine, "stop", _noop_start)
    monkeypatch.setattr(task_queue_module, "AgentLoop", FakeAgentLoop)

    queue = make_queue(profiles=manager)
    # Simulate the profile already being claimed by another running task.
    await manager.load_for_task(created["id"])

    task_id = await queue.enqueue("https://example.com", "goal", None, "", profile_label="Profile-01")
    async with get_session() as session:
        task = await session.get(Task, task_id)

    await queue._run_task(task)

    async with get_session() as session:
        db_task = await session.get(Task, task_id)
        assert db_task.status == TaskStatus.QUEUED
        assert db_task.retry_count == 0  # not counted as a failed attempt


@pytest.mark.asyncio
async def test_pop_next_skips_locked_profile_for_next_eligible_task(tmp_path):
    manager, registry = make_manager(tmp_path)
    p1 = await registry.create_profile("Profile-01")
    p2 = await registry.create_profile("Profile-02")

    queue = make_queue(profiles=manager)
    busy_id = await queue.enqueue("https://a.example.com", "goal", None, "", profile_label="Profile-01")
    free_id = await queue.enqueue("https://b.example.com", "goal", None, "", profile_label="Profile-02")

    picked = await queue._pop_next(locked_profile_ids={p1["id"]})
    assert picked.id == free_id
    assert picked.id != busy_id


@pytest.mark.asyncio
async def test_enqueue_auto_selects_profile_when_none_specified(tmp_path):
    manager, registry = make_manager(tmp_path)
    await registry.create_profile("Profile-01")

    queue = make_queue(profiles=manager)
    task_id = await queue.enqueue("https://example.com", "goal", None, "")

    async with get_session() as session:
        task = await session.get(Task, task_id)
        assert task.profile_label == "Profile-01"
