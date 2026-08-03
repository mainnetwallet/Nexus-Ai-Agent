import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.database.models import ChatMessage, ChatSession, Report, Task
from backend.database.session import get_session, init_db
from backend.planner.agent_runtime import AgentRuntime
from backend.planner.chat_engine import ChatEngine
from backend.planner.task_queue import TaskQueueService


class FakeMemory:
    async def recall_similar_workflows(self, website, goal, top_k=3):
        return []

    async def save_workflow_outcome(self, website, goal, outcome):
        pass


class FakeLiveSession:
    def __init__(self, active: bool = False, url: str = "", title: str = ""):
        self._active = active
        self._url = url
        self._title = title

    def status(self) -> dict:
        return {"active": self._active, "url": self._url, "title": self._title}

    def latest_screenshot_bytes(self):
        return b"fake-jpeg" if self._active else None


class FakeAppState:
    def __init__(self, agent=None, live_session=None):
        self.agent = agent
        self.live_session = live_session or FakeLiveSession()


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with get_session() as session:
        await session.execute(delete(ChatMessage))
        await session.execute(delete(ChatSession))
        await session.execute(delete(Report))
        await session.execute(delete(Task))
    yield
    async with get_session() as session:
        await session.execute(delete(ChatMessage))
        await session.execute(delete(ChatSession))
        await session.execute(delete(Report))
        await session.execute(delete(Task))


@pytest_asyncio.fixture
async def engine():
    queue = TaskQueueService(memory=FakeMemory(), wallet=None)
    agent = AgentRuntime(queue=queue)
    app_state = FakeAppState(agent=agent)
    chat = ChatEngine(queue=queue, app_state=app_state)
    chat.llm.complete_json = AsyncMock()
    chat.llm.complete_text = AsyncMock(return_value="Hi there!")
    yield chat, queue, agent

    worker_task = queue._worker_task
    if worker_task is not None and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_session_created_and_persisted(engine):
    chat, _, _ = engine
    session = await chat.get_or_create_session(None, channel="dashboard")
    assert session.channel == "dashboard"
    again = await chat.get_or_create_session(session.id)
    assert again.id == session.id


@pytest.mark.asyncio
async def test_conversation_message_uses_llm_and_is_persisted(engine):
    chat, _, _ = engine
    chat.llm.complete_json.return_value = {"category": "conversation"}
    result = await chat.send_message("s1", "hello there")
    assert result["category"] == "conversation"
    assert result["reply"] == "Hi there!"

    history = await chat.get_history("s1")
    assert [m.content for m in history] == ["hello there", "Hi there!"]
    assert history[0].role.value == "user"
    assert history[1].role.value == "assistant"


@pytest.mark.asyncio
async def test_task_message_enqueues_task(engine):
    chat, queue, _ = engine
    chat.llm.complete_json.return_value = {
        "category": "task",
        "website": "https://example.com",
        "goal": "buy a widget",
    }
    result = await chat.send_message("s2", "go buy a widget on https://example.com")
    assert result["category"] == "task"
    assert "task_id" in result["meta"]

    tasks = queue.queue_status()  # smoke: queue still responds
    assert tasks["worker_paused"] is False

    session = await chat.get_or_create_session("s2")
    assert session.last_task_id == result["meta"]["task_id"]


@pytest.mark.asyncio
async def test_agent_command_pause_and_resume(engine):
    chat, queue, agent = engine
    await agent.start()

    chat.llm.complete_json.return_value = {"category": "agent_command", "action": "pause"}
    result = await chat.send_message("s3", "pause")
    assert result["reply"] == "Paused."
    status = await agent.status()
    assert status["status"] == "paused"

    chat.llm.complete_json.return_value = {"category": "agent_command", "action": "resume"}
    result = await chat.send_message("s3", "resume")
    assert result["reply"] == "Resumed."
    status = await agent.status()
    assert status["status"] == "running"


@pytest.mark.asyncio
async def test_browser_command_screenshot_reports_availability(engine):
    chat, _, _ = engine
    chat.app_state.live_session = FakeLiveSession(active=False)
    chat.llm.complete_json.return_value = {"category": "browser_command", "action": "screenshot"}
    result = await chat.send_message("s4", "take a screenshot")
    assert "No screenshot available" in result["reply"]

    chat.app_state.live_session = FakeLiveSession(active=True, url="https://example.com", title="Example")
    result = await chat.send_message("s4", "take a screenshot")
    assert result["meta"].get("has_screenshot") is True


@pytest.mark.asyncio
async def test_browser_command_search_enqueues_task(engine):
    chat, _, _ = engine
    chat.llm.complete_json.return_value = {"category": "browser_command", "action": "search", "query": "nexus wallet"}
    result = await chat.send_message("s5", "search for nexus wallet")
    assert "nexus wallet" in result["reply"]
    assert "task_id" in result["meta"]


@pytest.mark.asyncio
async def test_system_request_today_summary_empty(engine):
    chat, _, _ = engine
    chat.llm.complete_json.return_value = {"category": "system_request", "action": "today_summary"}
    result = await chat.send_message("s6", "what happened today?")
    assert "Nothing has run today" in result["reply"]


@pytest.mark.asyncio
async def test_clear_history_resets_session(engine):
    chat, _, _ = engine
    chat.llm.complete_json.return_value = {"category": "conversation"}
    await chat.send_message("s7", "hi")
    assert len(await chat.get_history("s7")) == 2

    await chat.clear_history("s7")
    assert len(await chat.get_history("s7")) == 0


@pytest.mark.asyncio
async def test_classifier_failure_falls_back_to_conversation(engine):
    chat, _, _ = engine
    chat.llm.complete_json.side_effect = RuntimeError("boom")
    result = await chat.send_message("s8", "asdkjaskjd")
    assert result["category"] == "conversation"
    assert result["reply"] == "Hi there!"
