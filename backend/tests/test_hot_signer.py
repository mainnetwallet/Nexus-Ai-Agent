from __future__ import annotations

import pytest
from eth_account import Account

from backend.config.settings import settings
from backend.wallet.hot_signer import (
    HotSigner,
    HotSignerDisabled,
    HotSignerError,
    get_hot_signer_address,
)

# Well-known throwaway test key (Hardhat/Anvil default account #0). Never used
# on a real chain -- fine to hardcode in a test file.
TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDRESS = Account.from_key(TEST_PRIVATE_KEY).address


@pytest.fixture(autouse=True)
def _reset_hot_signer_settings(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")
    monkeypatch.setattr(settings, "hot_signer_max_native_value", 0.0)


def test_get_hot_signer_address_empty_when_unset():
    assert get_hot_signer_address() is None


def test_get_hot_signer_address_derives_from_key(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)
    assert get_hot_signer_address() == TEST_ADDRESS


@pytest.mark.asyncio
async def test_send_native_disabled_raises():
    signer = HotSigner()
    with pytest.raises(HotSignerDisabled):
        await signer.send_native("base", "0x" + "1" * 40, 0.01)


@pytest.mark.asyncio
async def test_send_native_unsupported_chain(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)
    signer = HotSigner()
    with pytest.raises(HotSignerError, match="Unsupported chain"):
        await signer.send_native("not-a-chain", "0x" + "1" * 40, 0.01)


@pytest.mark.asyncio
async def test_send_native_invalid_address(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)
    signer = HotSigner()
    with pytest.raises(HotSignerError, match="Invalid destination address"):
        await signer.send_native("base", "not-an-address", 0.01)


@pytest.mark.asyncio
async def test_send_native_over_cap_rejected(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)
    monkeypatch.setattr(settings, "hot_signer_max_native_value", 0.01)
    signer = HotSigner()
    with pytest.raises(HotSignerError, match="exceeds"):
        await signer.send_native("base", "0x" + "1" * 40, 1.0)


@pytest.mark.asyncio
async def test_send_native_success(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)

    calls = []

    async def fake_rpc_call(client, rpc_url, method, params):
        calls.append(method)
        if method == "eth_getTransactionCount":
            return "0x5"
        if method == "eth_gasPrice":
            return "0x3b9aca00"  # 1 gwei
        if method == "eth_sendRawTransaction":
            return "0xdeadbeef"
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(fake_rpc_call))

    recorded = {}

    class FakeRegistry:
        async def record_activity(self, wallet_id, event_type, description, metadata=None):
            recorded["wallet_id"] = wallet_id
            recorded["event_type"] = event_type
            recorded["metadata"] = metadata

    signer = HotSigner(wallet_registry=FakeRegistry())
    to_addr = "0x" + "2" * 40
    result = await signer.send_native("base", to_addr, 0.001)

    assert result.tx_hash == "0xdeadbeef"
    assert result.chain == "base"
    assert result.from_address == TEST_ADDRESS
    assert result.to_address == to_addr
    assert result.amount_native == 0.001
    assert result.amount_wei == int(0.001 * 1e18)
    assert calls == ["eth_getTransactionCount", "eth_gasPrice", "eth_sendRawTransaction"]
    assert recorded["event_type"] == "hot_signer_native_send"
    assert recorded["metadata"]["tx_hash"] == "0xdeadbeef"
