import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.database.models import ChatMessage, ChatSession, Report, Task
from backend.database.session import get_session, init_db
from backend.planner.chat_engine import ChatEngine
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


@pytest.fixture(autouse=True)
def _fresh_model_manager(tmp_path, monkeypatch):
    """Each test gets its own ModelManager instance/state file so a switch
    or routing-rule change in one test never bleeds into the next."""
    from backend.planner import model_manager as mm_module

    monkeypatch.setattr(mm_module, "STATE_PATH", tmp_path / "ai_model_manager.json")
    fresh = mm_module.ModelManager()
    monkeypatch.setattr(mm_module, "model_manager", fresh)
    yield fresh


@pytest_asyncio.fixture
async def engine():
    queue = TaskQueueService(memory=FakeMemory(), wallet=None)
    chat = ChatEngine(queue=queue)
    chat.llm.complete_json = AsyncMock()
    chat.llm.complete_text = AsyncMock(return_value="Hi there!")
    yield chat, queue

    worker_task = queue._worker_task
    if worker_task is not None and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_switch_provider_via_chat(engine, _fresh_model_manager):
    chat, _ = engine
    chat.llm.complete_json.return_value = {
        "category": "ai_model",
        "ai_action": "switch",
        "ai_provider": "claude",
    }
    result = await chat.send_message("s1", "switch to Claude")
    assert result["category"] == "ai_model"
    assert "Switched to anthropic" in result["reply"]
    assert _fresh_model_manager.current_provider.value == "anthropic"


@pytest.mark.asyncio
async def test_switch_without_named_provider_asks_for_clarification(engine):
    chat, _ = engine
    chat.llm.complete_json.return_value = {"category": "ai_model", "ai_action": "switch"}
    result = await chat.send_message("s2", "switch providers")
    assert "Which provider" in result["reply"]


@pytest.mark.asyncio
async def test_enable_and_disable_auto_routing_via_chat(engine, _fresh_model_manager):
    chat, _ = engine
    chat.llm.complete_json.return_value = {"category": "ai_model", "ai_action": "enable_auto_routing"}
    result = await chat.send_message("s3", "use automatic routing")
    assert "automatic" in result["reply"].lower() or "on" in result["reply"].lower()
    assert _fresh_model_manager.routing_mode == "auto"

    chat.llm.complete_json.return_value = {"category": "ai_model", "ai_action": "disable_auto_routing"}
    result = await chat.send_message("s3", "turn off smart routing")
    assert _fresh_model_manager.routing_mode == "manual"


@pytest.mark.asyncio
async def test_set_routing_rule_via_chat(engine, _fresh_model_manager):
    chat, _ = engine
    chat.llm.complete_json.return_value = {
        "category": "ai_model",
        "ai_action": "set_routing_rule",
        "ai_provider": "groq",
        "ai_task_type": "fast response",
    }
    result = await chat.send_message("s4", "use Groq for fast responses")
    assert "fast_response -> groq" in result["reply"]

    from backend.planner.model_manager import TaskType, LLMProvider  # noqa: F401 (import check)

    assert _fresh_model_manager.routing_rules[TaskType.FAST_RESPONSE].value == "groq"


@pytest.mark.asyncio
async def test_temporary_use_via_chat(engine, _fresh_model_manager):
    chat, _ = engine
    chat.llm.complete_json.return_value = {
        "category": "ai_model",
        "ai_action": "temporary_use",
        "ai_provider": "gemini",
    }
    result = await chat.send_message("s5", "use Gemini for this task only")
    assert "next request only" in result["reply"]
    assert _fresh_model_manager.has_active_override
    assert _fresh_model_manager._override.provider.value == "gemini"


@pytest.mark.asyncio
async def test_show_provider_and_model_via_chat(engine, _fresh_model_manager):
    chat, _ = engine
    chat.llm.complete_json.return_value = {"category": "ai_model", "ai_action": "show_provider"}
    result = await chat.send_message("s6", "show current provider")
    assert "anthropic" in result["reply"]

    chat.llm.complete_json.return_value = {"category": "ai_model", "ai_action": "show_model"}
    result = await chat.send_message("s6", "show current model")
    assert "Current model" in result["reply"]


@pytest.mark.asyncio
async def test_show_providers_via_chat(engine):
    chat, _ = engine
    chat.llm.complete_json.return_value = {"category": "ai_model", "ai_action": "show_providers"}
    result = await chat.send_message("s7", "show available providers")
    assert "anthropic" in result["reply"] and "groq" in result["reply"]
