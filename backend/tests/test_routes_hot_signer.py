import pytest
import pytest_asyncio
from eth_account import Account
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api import app_state
from backend.api.routes_wallet import router as wallet_router
from backend.config.settings import settings
from backend.wallet.hot_signer import HotSigner
from backend.wallet.registry import WalletRegistry

TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDRESS = Account.from_key(TEST_PRIVATE_KEY).address


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")
    monkeypatch.setattr(settings, "hot_signer_max_native_value", 0.0)

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
async def test_status_disabled_by_default(client):
    resp = await client.get("/api/wallets/hot-signer/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["address"] is None


@pytest.mark.asyncio
async def test_status_enabled_shows_address(client, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)
    resp = await client.get("/api/wallets/hot-signer/status")
    body = resp.json()
    assert body["enabled"] is True
    assert body["address"] == TEST_ADDRESS


@pytest.mark.asyncio
async def test_send_disabled_returns_403(client):
    resp = await client.post(
        "/api/wallets/hot-signer/send",
        json={"chain": "base", "to_address": "0x" + "1" * 40, "amount": 0.01},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_send_success(client, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)

    async def fake_rpc_call(client_, rpc_url, method, params):
        if method == "eth_getTransactionCount":
            return "0x1"
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        return "0xabc123"

    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(fake_rpc_call))

    resp = await client.post(
        "/api/wallets/hot-signer/send",
        json={"chain": "base", "to_address": "0x" + "2" * 40, "amount": 0.001},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tx_hash"] == "0xabc123"
    assert body["from_address"] == TEST_ADDRESS
