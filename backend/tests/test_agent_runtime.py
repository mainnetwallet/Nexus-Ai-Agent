import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.database.models import AgentRuntimeState, Report, Task, TaskStatus
from backend.database.session import get_session, init_db
from backend.planner.agent_runtime import AgentRuntime
from backend.planner.task_queue import TaskQueueService


class FakeMemory:
    async def recall_similar_workflows(self, website, goal, top_k=3):
        return []

    async def save_workflow_outcome(self, website, goal, outcome):
        pass


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with get_session() as session:
        await session.execute(delete(Report))
        await session.execute(delete(Task))
        await session.execute(delete(AgentRuntimeState))
    yield
    async with get_session() as session:
        await session.execute(delete(Report))
        await session.execute(delete(Task))
        await session.execute(delete(AgentRuntimeState))


_live_runtimes: list[AgentRuntime] = []


@pytest_asyncio.fixture(autouse=True)
async def _stop_worker_loops():
    """
    AgentRuntime.start() starts TaskQueueService's real background worker
    loop. That's exactly what we want to unit-test for orchestration (status
    transitions, recovery), but it also means any QUEUED task left over from
    a test would get picked up and drive a real BrowserEngine/LLM call.
    Tests that need to observe start()'s effect on a queued task stub
    `start_worker` instead; this fixture just guarantees any worker task that
    *did* get spawned is cancelled at teardown so nothing leaks between tests.
    """
    _live_runtimes.clear()
    yield
    for runtime in _live_runtimes:
        task = runtime.queue._worker_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _live_runtimes.clear()


def make_runtime() -> AgentRuntime:
    queue = TaskQueueService(memory=FakeMemory(), wallet=None)
    runtime = AgentRuntime(queue=queue)
    _live_runtimes.append(runtime)
    return runtime


@pytest.mark.asyncio
async def test_status_before_start_defaults_to_stopped():
    runtime = make_runtime()
    status = await runtime.status()
    assert status["status"] == "stopped"
    assert status["current_task_id"] is None
    assert status["tasks_completed"] == 0


@pytest.mark.asyncio
async def test_start_sets_running_and_starts_worker():
    runtime = make_runtime()
    status = await runtime.start()
    assert status["status"] == "running"
    assert status["started_at"] is not None
    assert runtime.queue._worker_task is not None
    assert not runtime.queue._worker_task.done()


@pytest.mark.asyncio
async def test_stop_sets_stopped_and_pauses_queue():
    runtime = make_runtime()
    await runtime.start()
    status = await runtime.stop()
    assert status["status"] == "stopped"
    assert status["stopped_at"] is not None
    assert status["queue"]["worker_paused"] is True


@pytest.mark.asyncio
async def test_pause_then_resume_round_trips_status():
    runtime = make_runtime()
    await runtime.start()

    paused = await runtime.pause()
    assert paused["status"] == "paused"
    assert paused["queue"]["worker_paused"] is True

    resumed = await runtime.resume()
    assert resumed["status"] == "running"
    assert resumed["queue"]["worker_paused"] is False


@pytest.mark.asyncio
async def test_pause_resume_also_toggle_the_in_flight_task():
    runtime = make_runtime()
    queue = runtime.queue
    task_id = await queue.enqueue("https://example.com", "goal", None, "")

    # Simulate a task actively running under this queue.
    pause_event = asyncio.Event()
    pause_event.set()
    queue._task_pause_events[task_id] = pause_event
    queue.current_task_id = task_id

    await runtime.pause()
    assert not pause_event.is_set()

    await runtime.resume()
    assert pause_event.is_set()


@pytest.mark.asyncio
async def test_stop_cancels_the_in_flight_task():
    runtime = make_runtime()
    queue = runtime.queue
    task_id = await queue.enqueue("https://example.com", "goal", None, "")
    queue.current_task_id = task_id

    await runtime.stop()

    # No live pause event was set up for this task (it was never actually
    # picked up by _run_task), so cancel() resolves it directly in the DB
    # instead of leaving it stuck behind a flag nothing will ever check.
    async with get_session() as session:
        db_task = await session.get(Task, task_id)
        assert db_task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_recover_interrupted_tasks_requeues_stuck_statuses():
    runtime = make_runtime()

    stuck_ids = []
    for status in (TaskStatus.RUNNING, TaskStatus.PLANNING, TaskStatus.PAUSED):
        task_id = await runtime.queue.enqueue("https://example.com", "goal", None, "")
        async with get_session() as session:
            task = await session.get(Task, task_id)
            task.status = status
        stuck_ids.append(task_id)

    recovered = await runtime._recover_interrupted_tasks()
    assert recovered == 3

    async with get_session() as session:
        for task_id in stuck_ids:
            task = await session.get(Task, task_id)
            assert task.status == TaskStatus.QUEUED

    status = await runtime.status()
    assert status["recoveries_performed"] == 3


@pytest.mark.asyncio
async def test_start_recovers_interrupted_tasks_on_boot():
    runtime = make_runtime()
    runtime.queue.start_worker = lambda: None  # isolate recovery from the real dispatch loop
    task_id = await runtime.queue.enqueue("https://example.com", "goal", None, "")
    async with get_session() as session:
        task = await session.get(Task, task_id)
        task.status = TaskStatus.RUNNING

    await runtime.start()

    async with get_session() as session:
        task = await session.get(Task, task_id)
        assert task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_recover_clears_stale_current_task_pointer():
    """
    An unclean shutdown can leave AgentRuntimeState.current_task_id pointing
    at a task from before the crash -- that field is only ever updated by
    live task_start/task_finish events, so a fresh process still shows it as
    the dashboard's "current task" forever, even after the underlying task
    is requeued or deleted. Recovery should clear it since nothing is
    genuinely in flight right after boot.
    """
    runtime = make_runtime()
    task_id = await runtime.queue.enqueue("https://example.com", "goal", None, "")
    async with get_session() as session:
        task = await session.get(Task, task_id)
        task.status = TaskStatus.RUNNING

    await runtime._update(current_task_id=task_id, current_website="https://example.com", current_action="click")

    await runtime._recover_interrupted_tasks()

    status = await runtime.status()
    assert status["current_task_id"] is None
    assert status["current_website"] is None
    assert status["current_action"] is None


@pytest.mark.asyncio
async def test_recover_keeps_current_task_pointer_if_actually_live():
    """If self.queue.current_task_id still matches (a genuinely in-flight
    task in this same process, e.g. a redundant Start click), recovery must
    not blank out the dashboard's view of it."""
    runtime = make_runtime()
    task_id = await runtime.queue.enqueue("https://example.com", "goal", None, "")
    runtime.queue.current_task_id = task_id
    await runtime._update(current_task_id=task_id, current_website="https://example.com", current_action="click")

    await runtime._recover_interrupted_tasks()

    status = await runtime.status()
    assert status["current_task_id"] == task_id


@pytest.mark.asyncio
async def test_activity_step_event_updates_current_action():
    runtime = make_runtime()
    await runtime.start()

    await runtime._on_activity(
        {
            "event": "step",
            "task_id": "t1",
            "action": "click",
            "target": "Sign up button",
            "reasoning": "Need to start signup flow",
        }
    )

    status = await runtime.status()
    assert status["current_task_id"] == "t1"
    assert status["current_action"] == "click"
    assert status["current_target"] == "Sign up button"
    assert status["current_reasoning"] == "Need to start signup flow"
    assert status["steps_executed"] == 1


@pytest.mark.asyncio
async def test_activity_task_finish_succeeded_increments_completed_and_clears_current():
    runtime = make_runtime()
    await runtime.start()
    await runtime._on_activity({"event": "task_start", "task_id": "t1", "website": "https://example.com"})
    await runtime._on_activity({"event": "task_finish", "task_id": "t1", "status": "succeeded", "summary": "done"})

    status = await runtime.status()
    assert status["tasks_completed"] == 1
    assert status["tasks_failed"] == 0
    assert status["current_task_id"] is None


@pytest.mark.asyncio
async def test_activity_task_finish_failed_increments_failed():
    runtime = make_runtime()
    await runtime.start()
    await runtime._on_activity({"event": "task_finish", "task_id": "t1", "status": "failed", "summary": "nope"})

    status = await runtime.status()
    assert status["tasks_failed"] == 1
    assert status["tasks_completed"] == 0


@pytest.mark.asyncio
async def test_activity_task_crash_increments_failed_and_sets_action():
    runtime = make_runtime()
    await runtime.start()
    await runtime._on_activity({"event": "task_crash", "task_id": "t1", "error": "browser crashed"})

    status = await runtime.status()
    assert status["tasks_failed"] == 1
    assert status["current_action"] == "crashed"
    assert status["current_reasoning"] == "browser crashed"


@pytest.mark.asyncio
async def test_activity_broadcast_callback_is_invoked():
    received = []

    async def on_broadcast(event):
        received.append(event)

    queue = TaskQueueService(memory=FakeMemory(), wallet=None)
    runtime = AgentRuntime(queue=queue, on_activity_broadcast=on_broadcast)
    await runtime.start()

    await runtime._on_activity({"event": "step", "task_id": "t1", "action": "wait", "target": "", "reasoning": ""})

    assert len(received) == 1
    assert received[0]["event"] == "step"
