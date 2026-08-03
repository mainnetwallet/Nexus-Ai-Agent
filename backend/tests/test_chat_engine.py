import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.database.models import ChatMessage, ChatSession, Report, Task, TaskStatus
from backend.database.session import get_session, init_db
from backend.planner.agent_runtime import AgentRuntime
from backend.planner.chat_engine import ChatEngine
from backend.planner.task_queue import TaskQueueService
from backend.wallet.tx_batch import TxBatchManager


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
    def __init__(self, agent=None, live_session=None, tx_batch=None):
        self.agent = agent
        self.live_session = live_session or FakeLiveSession()
        self.tx_batch = tx_batch if tx_batch is not None else TxBatchManager()


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
async def test_wallet_batch_queues_one_task_per_turn(engine):
    chat, queue, _ = engine

    chat.llm.complete_json.return_value = {
        "category": "wallet",
        "wallet_action": "batch_start",
        "tx_count": 3,
        "wallet_label": "burner-01",
    }
    start_result = await chat.send_message("s-batch", "queue 3 transactions using burner-01")
    assert start_result["category"] == "wallet"
    assert "3" in start_result["reply"]

    # Once a batch is active, follow-up turns must NOT go back through the
    # intent classifier -- they're destinations, handled deterministically.
    chat.llm.complete_json.reset_mock()
    chat.llm.complete_json.return_value = {"website": "https://example.com/a", "goal": "send 0.01 ETH"}
    r1 = await chat.send_message("s-batch", "0.01 ETH to 0xabc on example.com/a")
    assert chat.llm.complete_json.await_count == 1  # only the target-extraction call, not the classifier
    assert "1/3" in r1["reply"]

    chat.llm.complete_json.return_value = {"website": "https://example.com/b", "goal": "send 0.02 ETH"}
    r2 = await chat.send_message("s-batch", "0.02 ETH to 0xdef on example.com/b")
    assert "2/3" in r2["reply"]

    chat.llm.complete_json.return_value = {"website": "https://example.com/c", "goal": "send 0.03 ETH"}
    r3 = await chat.send_message("s-batch", "0.03 ETH to 0x123 on example.com/c")
    assert "3/3" in r3["reply"]
    assert "complete" in r3["reply"].lower()

    tasks = queue.queue_status()
    assert tasks["worker_paused"] is False  # smoke: queue still responds after 3 enqueues

    # Batch is retired -- the next message goes back through normal classification.
    chat.llm.complete_json.return_value = {"category": "conversation"}
    r4 = await chat.send_message("s-batch", "thanks")
    assert r4["category"] == "conversation"


@pytest.mark.asyncio
async def test_wallet_batch_can_be_cancelled_midway(engine):
    chat, _, _ = engine
    chat.llm.complete_json.return_value = {"category": "wallet", "wallet_action": "batch_start", "tx_count": 5}
    await chat.send_message("s-cancel", "queue 5 transactions")

    result = await chat.send_message("s-cancel", "cancel")
    assert "cancel" in result["reply"].lower()

    # Cancelling ends the batch -- the next message is classified normally.
    chat.llm.complete_json.return_value = {"category": "conversation"}
    followup = await chat.send_message("s-cancel", "hi")
    assert followup["category"] == "conversation"


@pytest.mark.asyncio
async def test_classifier_receives_conversation_context_for_followup(engine):
    """Reproduces: user says "Build an HTML page", agent asks a clarifying
    question, user replies "An AI dashboard" -- the classifier call for the
    follow-up must be given the prior exchange, not just the three-word
    reply on its own, so it can resolve what the user is continuing."""
    chat, _, _ = engine

    chat.llm.complete_json.return_value = {"category": "conversation"}
    chat.llm.complete_text.return_value = "What type of HTML page did you have in mind?"
    await chat.send_message("s-followup", "Build an HTML page")

    chat.llm.complete_json.return_value = {
        "category": "task",
        "goal": "Build an AI dashboard HTML page",
    }
    chat.llm.complete_text.return_value = "On it."
    await chat.send_message("s-followup", "An AI dashboard")

    # Second classifier call (index 1) is for the follow-up message.
    assert chat.llm.complete_json.await_count == 2
    _, followup_prompt = chat.llm.complete_json.await_args_list[1].args[:2]
    assert "Build an HTML page" in followup_prompt
    assert "What type of HTML page" in followup_prompt
    assert "New message to classify: An AI dashboard" in followup_prompt

    # The very first classifier call (no prior turns yet) must NOT carry a
    # context block -- backward-compatible with a plain first message.
    _, first_prompt = chat.llm.complete_json.await_args_list[0].args[:2]
    assert first_prompt == "Build an HTML page"


@pytest.mark.asyncio
async def test_conversational_reply_also_receives_history_context(engine):
    chat, _, _ = engine
    chat.llm.complete_json.return_value = {"category": "conversation"}

    await chat.send_message("s-ctx", "My favorite color is blue")
    await chat.send_message("s-ctx", "What did I just tell you?")

    _, second_user_prompt = chat.llm.complete_text.await_args_list[1].args[:2]
    assert "My favorite color is blue" in second_user_prompt
    assert "What did I just tell you?" in second_user_prompt


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


# ---------------------------------------------------------------------- #
# Single Task Control -- pause/resume/cancel one specific task, distinct
# from the global worker pause/resume covered above.
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pause_task_by_explicit_id(engine):
    chat, queue, _ = engine
    task_id = await queue.enqueue("https://example.com", "goal", None, "")
    queue._task_pause_events[task_id] = asyncio.Event()
    queue._task_pause_events[task_id].set()  # running

    chat.llm.complete_json.return_value = {
        "category": "agent_command",
        "action": "pause_task",
        "task_id": task_id,
    }
    result = await chat.send_message("s9", f"pause task {task_id}")
    assert result["reply"] == f"Paused task {task_id}."
    assert result["meta"]["task_id"] == task_id
    assert queue._task_pause_events[task_id].is_set() is False


@pytest.mark.asyncio
async def test_pause_task_defaults_to_currently_running_task(engine):
    chat, queue, _ = engine
    task_id = await queue.enqueue("https://example.com", "goal", None, "")
    queue.current_task_id = task_id
    queue._task_pause_events[task_id] = asyncio.Event()
    queue._task_pause_events[task_id].set()

    chat.llm.complete_json.return_value = {"category": "agent_command", "action": "pause_task", "task_id": ""}
    result = await chat.send_message("s10", "pause this task")
    assert result["reply"] == f"Paused task {task_id}."


@pytest.mark.asyncio
async def test_pause_task_no_running_task(engine):
    chat, _, _ = engine
    chat.llm.complete_json.return_value = {"category": "agent_command", "action": "pause_task", "task_id": ""}
    result = await chat.send_message("s11", "pause this task")
    assert result["reply"] == "No task is currently running to pause."


@pytest.mark.asyncio
async def test_resume_task_by_explicit_id(engine):
    chat, queue, _ = engine
    task_id = await queue.enqueue("https://example.com", "goal", None, "")
    queue._task_pause_events[task_id] = asyncio.Event()  # cleared = paused

    chat.llm.complete_json.return_value = {
        "category": "agent_command",
        "action": "resume_task",
        "task_id": task_id,
    }
    result = await chat.send_message("s12", f"resume task {task_id}")
    assert result["reply"] == f"Resumed task {task_id}."
    assert queue._task_pause_events[task_id].is_set() is True


@pytest.mark.asyncio
async def test_cancel_task_by_explicit_id(engine):
    chat, queue, _ = engine
    task_id = await queue.enqueue("https://example.com", "goal", None, "")

    chat.llm.complete_json.return_value = {
        "category": "agent_command",
        "action": "cancel_task",
        "task_id": task_id,
    }
    result = await chat.send_message("s13", f"cancel task {task_id}")
    assert result["reply"] == f"Cancelling task {task_id}."
    # No live pause event for this task -> cancelled directly in the DB.
    async with get_session() as session:
        db_task = await session.get(Task, task_id)
        assert db_task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_task_unknown_id(engine):
    chat, _, _ = engine
    chat.llm.complete_json.return_value = {
        "category": "agent_command",
        "action": "cancel_task",
        "task_id": "does-not-exist",
    }
    result = await chat.send_message("s14", "cancel task does-not-exist")
    assert result["reply"] == "No task found with id does-not-exist."


@pytest.mark.asyncio
async def test_bare_pause_still_targets_global_worker_not_a_task(engine):
    """Backward compatibility: plain 'pause'/'resume' (no task named) must
    keep controlling the global agent/worker, not silently start scoping to
    whichever task happens to be running."""
    chat, queue, agent = engine
    await agent.start()
    task_id = await queue.enqueue("https://example.com", "goal", None, "")
    queue.current_task_id = task_id

    chat.llm.complete_json.return_value = {"category": "agent_command", "action": "pause"}
    result = await chat.send_message("s15", "pause")
    assert result["reply"] == "Paused."
    status = await agent.status()
    assert status["status"] == "paused"
