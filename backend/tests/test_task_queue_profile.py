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
