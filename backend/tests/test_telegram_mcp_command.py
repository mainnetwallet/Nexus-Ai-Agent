import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.api import app_state
from backend.database.models import AgentRuntimeState, Report, Task
from backend.database.session import get_session, init_db
from backend.planner.agent_runtime import AgentRuntime
from backend.planner.task_queue import TaskQueueService
from backend.telegram.bot import NexusTelegramBot


class FakeMemory:
    async def recall_similar_workflows(self, website, goal, top_k=3):
        return []

    async def save_workflow_outcome(self, website, goal, outcome):
        pass


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
    app_state.state.live_session = None
    app_state.state.wallet_registry = None

    bot = NexusTelegramBot(queue=queue, app_state=app_state.state)
    bot.chat_engine.send_message = AsyncMock(return_value={"reply": "ok", "category": "mcp", "action": "", "meta": {}})
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


def _make_update(text: str = "", args=None):
    message = MagicMock()
    message.reply_text = AsyncMock()
    message.text = text
    update = MagicMock()
    update.message = message
    update.effective_user = SimpleNamespace(id=1)
    update.effective_chat = SimpleNamespace(id=42)
    context = MagicMock()
    context.args = args or []
    return update, message, context


@pytest.mark.asyncio
async def test_mcp_with_no_args_defaults_to_list_connectors(bot_and_state):
    bot, _ = bot_and_state
    update, message, context = _make_update()
    await bot.cmd_mcp(update, context)

    bot.chat_engine.send_message.assert_awaited_once_with(
        "telegram:42", "list my mcp connectors", channel="telegram"
    )
    message.reply_text.assert_awaited_once_with("ok")


@pytest.mark.asyncio
async def test_mcp_with_args_passes_joined_text_through(bot_and_state):
    bot, _ = bot_and_state
    update, message, context = _make_update(args=["read", "file", "notes.txt"])
    await bot.cmd_mcp(update, context)

    bot.chat_engine.send_message.assert_awaited_once_with(
        "telegram:42", "read file notes.txt", channel="telegram"
    )
    message.reply_text.assert_awaited_once_with("ok")


@pytest.mark.asyncio
async def test_mcp_command_requires_authorization(bot_and_state, monkeypatch):
    bot, _ = bot_and_state
    from backend.config.settings import settings

    monkeypatch.setattr(type(settings), "allowed_telegram_ids", property(lambda self: {999}))
    update, message, context = _make_update()
    await bot.cmd_mcp(update, context)

    bot.chat_engine.send_message.assert_not_awaited()
    message.reply_text.assert_awaited_once_with("Not authorized.")
