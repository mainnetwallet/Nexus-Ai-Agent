"""
Tests for the Chat wallet-import flow's hot signer save behavior (see
ChatEngine._handle_wallet_crud's import branch and
_handle_pending_wallet_secret_turn in backend/planner/chat_engine.py).
Persistence defaults to ON (settings.hot_signer_auto_save_on_import is
forced True) unless the user explicitly declines in the same message.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from eth_account import Account
from sqlalchemy import delete

from backend.config.settings import settings
from backend.database.models import ChatMessage, ChatSession, Report, Task, WalletActivity, WalletRecord
from backend.database.session import get_session, init_db
from backend.planner.agent_runtime import AgentRuntime
from backend.planner.chat_engine import ChatEngine
from backend.planner.task_queue import TaskQueueService
from backend.wallet.hot_signer import HotSigner
from backend.wallet.registry import WalletRegistry
from backend.wallet.tx_batch import TxBatchManager

TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDRESS = Account.from_key(TEST_PRIVATE_KEY).address


class FakeLiveSession:
    def status(self) -> dict:
        return {"active": False, "url": "", "title": ""}

    def latest_screenshot_bytes(self):
        return None


class FakeMemory:
    async def recall_similar_workflows(self, website, goal, top_k=3):
        return []

    async def save_workflow_outcome(self, website, goal, outcome):
        pass


class FakeAppState:
    def __init__(self, wallet_registry, hot_signer):
        self.agent = None
        self.live_session = FakeLiveSession()
        self.tx_batch = TxBatchManager()
        self.wallet_registry = wallet_registry
        self.hot_signer = hot_signer


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with get_session() as session:
        await session.execute(delete(ChatMessage))
        await session.execute(delete(ChatSession))
        await session.execute(delete(Report))
        await session.execute(delete(Task))
        await session.execute(delete(WalletActivity))
        await session.execute(delete(WalletRecord))
    yield
    async with get_session() as session:
        await session.execute(delete(ChatMessage))
        await session.execute(delete(ChatSession))
        await session.execute(delete(Report))
        await session.execute(delete(Task))
        await session.execute(delete(WalletActivity))
        await session.execute(delete(WalletRecord))


@pytest_asyncio.fixture
async def engine(tmp_path, monkeypatch):
    import backend.wallet.hot_signer as hot_signer_module
    from backend.wallet.keystore import Keystore

    scratch_keystore = tmp_path / "hot_signer.keystore"
    monkeypatch.setattr(hot_signer_module, "KEYSTORE_PATH", scratch_keystore)
    monkeypatch.setattr(hot_signer_module, "_keystore", Keystore(scratch_keystore))
    monkeypatch.setenv("KEYSTORE_PASSPHRASE", "test-passphrase-not-a-real-secret")
    monkeypatch.setattr(settings, "hot_signer_keystore_passphrase", "test-passphrase-not-a-real-secret")
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")
    monkeypatch.setattr(settings, "hot_signer_keys", {})
    monkeypatch.setattr(settings, "hot_signer_labels", {})
    monkeypatch.setattr(settings, "hot_signer_active_address", "")

    queue = TaskQueueService(memory=FakeMemory(), wallet=None)
    wallet_registry = WalletRegistry()
    hot_signer = HotSigner(wallet_registry=wallet_registry)
    app_state = FakeAppState(wallet_registry=wallet_registry, hot_signer=hot_signer)
    chat = ChatEngine(queue=queue, app_state=app_state)
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
async def test_import_without_explicit_flag_persists_via_server_default(engine):
    # wallet_save_as_hot_signer isn't mentioned in the user's message, so
    # this falls back to settings.hot_signer_auto_save_on_import -- which is
    # forced True (see settings.py's _force_hot_signer_always_on validator),
    # so the import persists to the hot signer even without an explicit ask.
    chat, _ = engine
    chat.llm.complete_json.return_value = {
        "category": "wallet",
        "wallet_action": "import",
        "wallet_label": "burner-a",
        "wallet_import_method": "private_key",
    }
    start = await chat.send_message("s1", "import wallet burner-a with my private key")
    assert start["meta"]["pending"] == "wallet_secret"
    assert start["meta"]["save_as_hot_signer"] is True

    # The secret turn must NOT go back through the classifier.
    chat.llm.complete_json.reset_mock()
    result = await chat.send_message("s1", TEST_PRIVATE_KEY)
    assert chat.llm.complete_json.await_count == 0
    assert "Imported wallet 'burner-a'" in result["reply"]
    assert result["meta"]["hot_signer_address"] == TEST_ADDRESS
    assert settings.hot_signer_enabled is True
    assert settings.hot_signer_private_key.lower() == TEST_PRIVATE_KEY.lower()


@pytest.mark.asyncio
async def test_import_with_explicit_false_flag_skips_hot_signer(engine):
    # User explicitly declines this turn (wallet_save_as_hot_signer="false")
    # -- this must override the server-wide auto-save default.
    chat, _ = engine
    chat.llm.complete_json.return_value = {
        "category": "wallet",
        "wallet_action": "import",
        "wallet_label": "cold-a",
        "wallet_import_method": "private_key",
        "wallet_save_as_hot_signer": "false",
    }
    start = await chat.send_message("s1b", "import wallet cold-a with my private key, no hot signer")
    assert start["meta"]["save_as_hot_signer"] is False

    chat.llm.complete_json.reset_mock()
    result = await chat.send_message("s1b", TEST_PRIVATE_KEY)
    assert chat.llm.complete_json.await_count == 0
    assert "Imported wallet 'cold-a'" in result["reply"]
    assert "hot_signer_address" not in result["meta"]
    assert settings.hot_signer_enabled is False


@pytest.mark.asyncio
async def test_import_with_hot_signer_flag_persists_and_enables_send(engine):
    chat, _ = engine
    chat.llm.complete_json.return_value = {
        "category": "wallet",
        "wallet_action": "import",
        "wallet_label": "burner-b",
        "wallet_import_method": "private_key",
        "wallet_save_as_hot_signer": "true",
    }
    start = await chat.send_message("s2", "import wallet burner-b with my private key, hot signer hisebe set koro")
    assert start["meta"]["save_as_hot_signer"] is True
    assert "hot signer" in start["reply"].lower()

    chat.llm.complete_json.reset_mock()
    result = await chat.send_message("s2", TEST_PRIVATE_KEY)
    assert chat.llm.complete_json.await_count == 0
    assert result["meta"]["hot_signer_address"] == TEST_ADDRESS
    assert settings.hot_signer_enabled is True
    assert settings.hot_signer_private_key.lower() == TEST_PRIVATE_KEY.lower()

    # The already-wired HotSigner instance in app_state should now be able
    # to send without any further .env/manual setup.
    async def fake_rpc_call(rpc_candidates, method, params):
        if method == "eth_getTransactionCount":
            return "0x1"
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        return "0xfeedface"

    from unittest.mock import patch

    with patch.object(HotSigner, "_rpc_call", staticmethod(fake_rpc_call)):
        chat.llm.complete_json.return_value = {
            "category": "wallet",
            "wallet_action": "send_native",
            "send_chain": "base",
            "send_to_address": "0x" + "2" * 40,
            "send_amount": "0.001",
        }
        send_result = await chat.send_message("s2", "send 0.001 to 0x" + "2" * 40 + " on base")
    assert "0xfeedface" in send_result["reply"]


@pytest.mark.asyncio
async def test_secret_is_still_redacted_from_chat_history_when_saved_as_hot_signer(engine):
    chat, _ = engine
    chat.llm.complete_json.return_value = {
        "category": "wallet",
        "wallet_action": "import",
        "wallet_label": "burner-c",
        "wallet_import_method": "private_key",
        "wallet_save_as_hot_signer": "true",
    }
    await chat.send_message("s3", "import wallet burner-c with my private key as hot signer")
    await chat.send_message("s3", TEST_PRIVATE_KEY)

    async with get_session() as db:
        from sqlalchemy import select

        rows = (
            await db.execute(
                select(ChatMessage.content).where(ChatMessage.session_id == "s3", ChatMessage.role == "user")
            )
        ).scalars().all()
    assert all(TEST_PRIVATE_KEY not in (r or "") for r in rows)
