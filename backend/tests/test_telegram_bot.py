import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.api import app_state
from backend.database.models import AgentRuntimeState, Report, Task, TaskStatus
from backend.database.session import get_session, init_db
from backend.planner.agent_runtime import AgentRuntime
from backend.planner.task_queue import TaskQueueService
from backend.telegram.bot import NexusTelegramBot


class FakeMemory:
    async def recall_similar_workflows(self, website, goal, top_k=3):
        return []

    async def save_workflow_outcome(self, website, goal, outcome):
        pass


class FakeLiveSession:
    def __init__(self, active: bool = False):
        self._active = active

    def status(self) -> dict:
        if not self._active:
            return {"active": False}
        return {"active": True, "url": "https://example.com", "title": "Example"}


@pytest_asyncio.fixture
async def bot_and_state():
    await init_db()
    async with get_session() as session:
        await session.execute(delete(Report))
        await session.execute(delete(Task))
        await session.execute(delete(AgentRuntimeState))

    queue = TaskQueueService(memory=FakeMemory(), wallet=None)
    app_state.state.queue = queue
    app_state.state.agent = AgentRuntime(queue=queue)
    app_state.state.memory = FakeMemory()
    app_state.state.plugins = None
    app_state.state.live_session = FakeLiveSession()
    app_state.state.wallet_registry = None

    bot = NexusTelegramBot(queue=queue, app_state=app_state.state)
    yield bot, app_state.state

    worker_task = queue._worker_task
    if worker_task is not None and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    async with get_session() as session:
        await session.execute(delete(Report))
        await session.execute(delete(Task))
        await session.execute(delete(AgentRuntimeState))
    app_state.state.queue = None
    app_state.state.agent = None
    app_state.state.memory = None
    app_state.state.live_session = None


@pytest.mark.asyncio
async def test_text_status_reflects_agent_runtime(bot_and_state):
    bot, state = bot_and_state
    await state.agent.start()
    text = await bot._text_status()
    assert "status: running" in text
    assert "tasks_completed" in text


@pytest.mark.asyncio
async def test_text_tasks_lists_queued_task(bot_and_state):
    bot, state = bot_and_state
    await state.queue.enqueue("https://example.com", "buy a widget", None, notes="", priority=1)
    text = await bot._text_tasks()
    assert "example.com" in text
    assert "buy a widget" in text


@pytest.mark.asyncio
async def test_text_tasks_empty(bot_and_state):
    bot, _ = bot_and_state
    text = await bot._text_tasks()
    assert "No tasks yet" in text


@pytest.mark.asyncio
async def test_text_browser_idle(bot_and_state):
    bot, _ = bot_and_state
    text = await bot._text_browser()
    assert "idle" in text.lower()


@pytest.mark.asyncio
async def test_text_browser_active(bot_and_state):
    bot, state = bot_and_state
    state.live_session = FakeLiveSession(active=True)
    text = await bot._text_browser()
    assert "example.com" in text


@pytest.mark.asyncio
async def test_text_health_reports_overall(bot_and_state):
    bot, _ = bot_and_state
    text = await bot._text_health()
    assert "overall:" in text
    assert "database:" in text


@pytest.mark.asyncio
async def test_text_diagnostics_reports_pass_fail(bot_and_state):
    bot, _ = bot_and_state
    text = await bot._text_diagnostics()
    assert "Nexus-Agent Diagnostic Report" in text


@pytest.mark.asyncio
async def test_text_resources_reports_queue_size(bot_and_state):
    bot, _ = bot_and_state
    text = await bot._text_resources()
    assert "queue_size:" in text
    assert "cpu:" in text


@pytest.mark.asyncio
async def test_no_app_state_falls_back_gracefully():
    queue = TaskQueueService(memory=FakeMemory(), wallet=None)
    bot = NexusTelegramBot(queue=queue, app_state=None)
    assert "requires the full app state" in await bot._text_health()
    assert "requires the full app state" in await bot._text_diagnostics()
    assert "requires the full app state" in await bot._text_resources()
    assert "Use /tasks" in await bot._text_status()
    assert "use /task to start" in await bot._text_browser()


def _make_update(text: str):
    message = MagicMock()
    message.reply_text = AsyncMock()
    message.text = text
    update = MagicMock()
    update.message = message
    update.effective_user = SimpleNamespace(id=1)
    return update, message


@pytest.mark.asyncio
async def test_on_free_text_routes_health_intent(bot_and_state, monkeypatch):
    bot, _ = bot_and_state
    bot.llm.complete_json = AsyncMock(return_value={"intent": "health"})
    update, message = _make_update("how's everything doing?")
    await bot.on_free_text(update, context=MagicMock())
    message.reply_text.assert_awaited_once()
    assert "overall:" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_on_free_text_routes_start_task_intent(bot_and_state):
    bot, _ = bot_and_state
    bot.llm.complete_json = AsyncMock(
        return_value={"intent": "start_task", "website": "https://example.com", "goal": "buy widget", "wallet_label": ""}
    )
    update, message = _make_update("go buy a widget on https://example.com")
    await bot.on_free_text(update, context=MagicMock())
    message.reply_text.assert_awaited_once()
    assert "Queued task" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_on_free_text_unknown_intent_falls_back_to_help_hint(bot_and_state):
    bot, _ = bot_and_state
    bot.llm.complete_json = AsyncMock(return_value={"intent": "unknown"})
    update, message = _make_update("asdkjaskjd")
    await bot.on_free_text(update, context=MagicMock())
    message.reply_text.assert_awaited_once_with("Not sure what you want — try /help.")


@pytest.mark.asyncio
async def test_on_free_text_llm_failure_suggests_help(bot_and_state):
    bot, _ = bot_and_state
    bot.llm.complete_json = AsyncMock(side_effect=RuntimeError("boom"))
    update, message = _make_update("???")
    await bot.on_free_text(update, context=MagicMock())
    message.reply_text.assert_awaited_once()
    assert "try /help" in message.reply_text.await_args.args[0]


# ---------------------------------------------------------------------- #
# Single Task Control via Telegram: /pause, /resume, /cancel with an
# optional task_id argument, plus the equivalent free-form phrasing.
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cmd_pause_no_args_pauses_global_worker(bot_and_state):
    bot, state = bot_and_state
    await state.agent.start()
    update, message = _make_update("/pause")
    await bot.cmd_pause(update, context=SimpleNamespace(args=[]))
    message.reply_text.assert_awaited_once_with("Paused.")
    status = await state.agent.status()
    assert status["status"] == "paused"


@pytest.mark.asyncio
async def test_cmd_pause_with_task_id_scopes_to_task(bot_and_state):
    bot, state = bot_and_state
    task_id = await bot.queue.enqueue("https://example.com", "goal", None, "")
    bot.queue._task_pause_events[task_id] = asyncio.Event()
    bot.queue._task_pause_events[task_id].set()
    bot.chat_engine.llm.complete_json = AsyncMock(
        return_value={"category": "agent_command", "action": "pause_task", "task_id": task_id}
    )
    update, message = _make_update(f"/pause {task_id}")
    await bot.cmd_pause(update, context=SimpleNamespace(args=[task_id]))
    message.reply_text.assert_awaited_once_with(f"Paused task {task_id}.")
    assert bot.queue._task_pause_events[task_id].is_set() is False


@pytest.mark.asyncio
async def test_cmd_cancel_with_task_id(bot_and_state):
    bot, state = bot_and_state
    task_id = await bot.queue.enqueue("https://example.com", "goal", None, "")
    bot.chat_engine.llm.complete_json = AsyncMock(
        return_value={"category": "agent_command", "action": "cancel_task", "task_id": task_id}
    )
    update, message = _make_update(f"/cancel {task_id}")
    await bot.cmd_cancel(update, context=SimpleNamespace(args=[task_id]))
    message.reply_text.assert_awaited_once_with(f"Cancelling task {task_id}.")
    # No live pause event for this task -> cancelled directly in the DB.
    async with get_session() as session:
        db_task = await session.get(Task, task_id)
        assert db_task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_on_free_text_routes_cancel_task_intent(bot_and_state):
    bot, state = bot_and_state
    task_id = await bot.queue.enqueue("https://example.com", "goal", None, "")
    # bot.llm and bot.chat_engine.llm are the same shared model_manager
    # singleton, so a single mock with two sequential responses stands in
    # for: (1) the top-level Telegram intent classification, then (2) the
    # ChatEngine agent_command classification triggered by _handle_chat_text.
    bot.llm.complete_json = AsyncMock(
        side_effect=[
            {"intent": "cancel_task", "task_id": task_id},
            {"category": "agent_command", "action": "cancel_task", "task_id": task_id},
        ]
    )
    update, message = _make_update(f"cancel task {task_id}")
    await bot.on_free_text(update, context=MagicMock())
    message.reply_text.assert_awaited_once_with(f"Cancelling task {task_id}.")
