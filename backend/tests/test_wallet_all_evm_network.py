"""
Tests for the "All EVM chains" (network="all_evm") wallet tag: it's a valid
network value to import/label a wallet with (same address works on every EVM
chain), but balance lookups need one concrete chain -- see
routes_wallet.py::get_wallet_balance and
ChatEngine._handle_wallet_crud's balance branch.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.api import app_state
from backend.api.routes_wallet import router as wallet_router
from backend.database.models import WalletActivity, WalletRecord
from backend.database.session import get_session, init_db
from backend.wallet.registry import WalletRegistry


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    yield
    async with get_session() as session:
        await session.execute(delete(WalletActivity))
        await session.execute(delete(WalletRecord))


@pytest_asyncio.fixture
async def client():
    app_state.state.wallet_registry = WalletRegistry()
    app = FastAPI()
    app.include_router(wallet_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app_state.state.wallet_registry = None


@pytest.mark.asyncio
async def test_import_accepts_all_evm_as_network(client):
    resp = await client.post(
        "/api/wallets/import",
        json={"label": "multi-chain-1", "method": "address", "address": "0x" + "4" * 40, "network": "all_evm"},
    )
    assert resp.status_code == 200
    assert resp.json()["network"] == "all_evm"


@pytest.mark.asyncio
async def test_balance_rejects_all_evm_with_helpful_message(client):
    imported = await client.post(
        "/api/wallets/import",
        json={"label": "multi-chain-2", "method": "address", "address": "0x" + "5" * 40, "network": "all_evm"},
    )
    wallet_id = imported.json()["id"]

    resp = await client.get(f"/api/wallets/{wallet_id}/balance")
    assert resp.status_code == 400
    assert "all_evm" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_balance_works_when_explicit_chain_overrides_all_evm(client, monkeypatch):
    imported = await client.post(
        "/api/wallets/import",
        json={"label": "multi-chain-3", "method": "address", "address": "0x" + "6" * 40, "network": "all_evm"},
    )
    wallet_id = imported.json()["id"]

    async def fake_get_balance(self, address, network):
        return {"address": address, "network": network, "wei": 0, "native": 0.0}

    monkeypatch.setattr(WalletRegistry, "get_balance", fake_get_balance)

    resp = await client.get(f"/api/wallets/{wallet_id}/balance", params={"network": "base"})
    assert resp.status_code == 200
    assert resp.json()["network"] == "base"
