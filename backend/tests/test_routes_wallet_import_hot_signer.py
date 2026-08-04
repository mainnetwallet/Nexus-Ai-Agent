"""
Tests for POST /api/wallets/import's save_as_hot_signer handling (see
backend/wallet/hot_signer.py::persist_hot_signer_secret and
backend/api/routes_wallet.py's import_wallet route). Persistence defaults to
ON (settings.hot_signer_auto_save_on_import is forced True) -- these tests
cover both that default and the explicit opt-out.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from eth_account import Account
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.api import app_state
from backend.api.routes_wallet import router as wallet_router
from backend.config.settings import settings
from backend.database.models import WalletActivity, WalletRecord
from backend.database.session import get_session, init_db
from backend.wallet.hot_signer import HotSigner
from backend.wallet.registry import WalletRegistry

TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDRESS = Account.from_key(TEST_PRIVATE_KEY).address


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    yield
    async with get_session() as session:
        await session.execute(delete(WalletActivity))
        await session.execute(delete(WalletRecord))


@pytest_asyncio.fixture
async def client(monkeypatch, tmp_path):
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

    app_state.state.wallet_registry = WalletRegistry()
    app_state.state.hot_signer = HotSigner(wallet_registry=app_state.state.wallet_registry)

    app = FastAPI()
    app.include_router(wallet_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app_state.state.wallet_registry = None
    app_state.state.hot_signer = None


@pytest.mark.asyncio
async def test_import_without_flag_persists_via_auto_save_default(client):
    # save_as_hot_signer isn't passed on the request, so this falls back to
    # settings.hot_signer_auto_save_on_import -- forced True (see
    # settings.py's _force_hot_signer_always_on validator) -- so the import
    # still persists to the hot signer. See test_explicit_false_overrides_
    # auto_save_setting below for how to actually opt out.
    resp = await client.post(
        "/api/wallets/import",
        json={
            "label": "burner-1",
            "method": "private_key",
            "private_key": TEST_PRIVATE_KEY,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hot_signer_address"] == TEST_ADDRESS
    assert settings.hot_signer_enabled is True
    assert settings.hot_signer_private_key.lower() == TEST_PRIVATE_KEY.lower()


@pytest.mark.asyncio
async def test_import_with_flag_persists_hot_signer(client):
    resp = await client.post(
        "/api/wallets/import",
        json={
            "label": "burner-2",
            "method": "private_key",
            "private_key": TEST_PRIVATE_KEY,
            "save_as_hot_signer": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hot_signer_address"] == TEST_ADDRESS
    assert settings.hot_signer_enabled is True
    assert settings.hot_signer_private_key.lower() == TEST_PRIVATE_KEY.lower()

    status_resp = await client.get("/api/wallets/hot-signer/status")
    assert status_resp.json()["enabled"] is True
    assert status_resp.json()["address"] == TEST_ADDRESS

    activity = await client.get("/api/wallets/activity")
    events = [a["event_type"] for a in activity.json()]
    assert "hot_signer_configured" in events


@pytest.mark.asyncio
async def test_import_with_flag_ignored_for_address_method(client):
    resp = await client.post(
        "/api/wallets/import",
        json={
            "label": "cold-1",
            "method": "address",
            "address": "0x" + "3" * 40,
            "save_as_hot_signer": True,
        },
    )
    assert resp.status_code == 200
    assert "hot_signer_address" not in resp.json()
    assert settings.hot_signer_enabled is False


@pytest.mark.asyncio
async def test_auto_save_on_import_persists_without_explicit_flag(client, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_auto_save_on_import", True)

    resp = await client.post(
        "/api/wallets/import",
        json={"label": "auto-1", "method": "private_key", "private_key": TEST_PRIVATE_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hot_signer_address"] == TEST_ADDRESS
    assert settings.hot_signer_enabled is True
    assert settings.hot_signer_private_key.lower() == TEST_PRIVATE_KEY.lower()


@pytest.mark.asyncio
async def test_explicit_false_overrides_auto_save_setting(client, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_auto_save_on_import", True)

    resp = await client.post(
        "/api/wallets/import",
        json={
            "label": "auto-2",
            "method": "private_key",
            "private_key": TEST_PRIVATE_KEY,
            "save_as_hot_signer": False,
        },
    )
    assert resp.status_code == 200
    assert "hot_signer_address" not in resp.json()
    assert settings.hot_signer_enabled is False
