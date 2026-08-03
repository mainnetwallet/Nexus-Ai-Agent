import asyncio
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.database.models import Report, Task, TaskStatus
from backend.database.session import get_session, init_db
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


def make_queue() -> TaskQueueService:
    return TaskQueueService(memory=FakeMemory(), wallet=None)


@pytest.mark.asyncio
async def test_enqueue_persists_scheduled_for():
    queue = make_queue()
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    task_id = await queue.enqueue("https://example.com", "sign up", None, "", scheduled_for=future)

    async with get_session() as session:
        task = await session.get(Task, task_id)
        assert task.scheduled_for is not None
        assert task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_pop_next_skips_future_scheduled_task():
    queue = make_queue()
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    await queue.enqueue("https://future.example.com", "goal", None, "", scheduled_for=future)
    due_id = await queue.enqueue("https://due.example.com", "goal", None, "")

    picked = await queue._pop_next()

    assert picked is not None
    assert picked.id == due_id


@pytest.mark.asyncio
async def test_pop_next_picks_due_scheduled_task():
    queue = make_queue()
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    task_id = await queue.enqueue("https://example.com", "goal", None, "", scheduled_for=past)

    picked = await queue._pop_next()

    assert picked is not None
    assert picked.id == task_id


@pytest.mark.asyncio
async def test_pause_and_resume_unblock_a_running_task():
    queue = make_queue()
    events: list[str] = []

    async def fake_wait_if_paused_owner(task_id: str, pause_event: asyncio.Event):
        if not pause_event.is_set():
            events.append("paused")
            await pause_event.wait()
            events.append("resumed")

    # Simulate what _run_task wires up, without a real browser/LLM.
    task_id = await queue.enqueue("https://example.com", "goal", None, "")
    pause_event = asyncio.Event()
    pause_event.set()
    queue._task_pause_events[task_id] = pause_event

    assert queue.pause_task(task_id) is True
    assert queue.queue_status()["paused_task_ids"] == [task_id]

    waiter = asyncio.create_task(fake_wait_if_paused_owner(task_id, pause_event))
    await asyncio.sleep(0.05)
    assert events == ["paused"]

    assert await queue.resume_task(task_id) is True
    await asyncio.wait_for(waiter, timeout=1)
    assert events == ["paused", "resumed"]
    assert queue.queue_status()["paused_task_ids"] == []


@pytest.mark.asyncio
async def test_pause_resume_are_noop_for_unknown_task():
    queue = make_queue()
    assert queue.pause_task("does-not-exist") is False
    assert await queue.resume_task("does-not-exist") is False


@pytest.mark.asyncio
async def test_cancel_unblocks_a_paused_task():
    queue = make_queue()
    task_id = await queue.enqueue("https://example.com", "goal", None, "")
    pause_event = asyncio.Event()
    pause_event.clear()  # already paused
    queue._task_pause_events[task_id] = pause_event

    await queue.cancel(task_id)

    assert pause_event.is_set()
    assert task_id in queue._cancelled_ids


@pytest.mark.asyncio
async def test_retry_requeues_failed_task_and_resets_retry_count():
    queue = make_queue()
    task_id = await queue.enqueue("https://example.com", "goal", None, "")
    async with get_session() as session:
        task = await session.get(Task, task_id)
        task.status = TaskStatus.FAILED
        task.retry_count = 2

    ok = await queue.retry(task_id)

    assert ok is True
    async with get_session() as session:
        task = await session.get(Task, task_id)
        assert task.status == TaskStatus.QUEUED
        assert task.retry_count == 0


@pytest.mark.asyncio
async def test_retry_rejects_active_task():
    queue = make_queue()
    task_id = await queue.enqueue("https://example.com", "goal", None, "")  # starts QUEUED

    ok = await queue.retry(task_id)

    assert ok is False


@pytest.mark.asyncio
async def test_retry_rejects_unknown_task():
    queue = make_queue()
    assert await queue.retry("does-not-exist") is False
