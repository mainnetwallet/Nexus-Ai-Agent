from __future__ import annotations

import pytest
from eth_account import Account

from backend.config.settings import settings
from backend.wallet.hot_signer import (
    HotSigner,
    HotSignerDisabled,
    HotSignerError,
    HotSignerPersistError,
    get_hot_signer_address,
    persist_hot_signer_secret,
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


# ---------------------------------------------------------------------- #
# persist_hot_signer_secret
# ---------------------------------------------------------------------- #

TEST_MNEMONIC = "test test test test test test test test test test test junk"


TEST_PASSPHRASE = "correct-horse-battery-staple"


@pytest.fixture
def _isolated_keystore(tmp_path, monkeypatch):
    """Points the module's keystore at a scratch file so these tests never
    touch a real keystore on disk, then restores it after."""
    import backend.wallet.hot_signer as hot_signer_module
    from backend.wallet.keystore import Keystore

    scratch = tmp_path / "hot_signer.keystore"
    monkeypatch.setattr(hot_signer_module, "KEYSTORE_PATH", scratch)
    monkeypatch.setattr(hot_signer_module, "_keystore", Keystore(scratch))
    return scratch


def test_persist_requires_exactly_one_secret(_isolated_keystore):
    with pytest.raises(HotSignerPersistError):
        persist_hot_signer_secret(passphrase=TEST_PASSPHRASE)
    with pytest.raises(HotSignerPersistError):
        persist_hot_signer_secret(
            private_key=TEST_PRIVATE_KEY, seed_phrase=TEST_MNEMONIC, passphrase=TEST_PASSPHRASE
        )


def test_persist_without_passphrase_raises_instead_of_prompting(_isolated_keystore, monkeypatch):
    # No explicit passphrase, no KEYSTORE_PASSPHRASE env var, no settings
    # value -- this must raise immediately (never block on stdin), since
    # the real caller is a request handler.
    monkeypatch.delenv("KEYSTORE_PASSPHRASE", raising=False)
    monkeypatch.setattr(settings, "hot_signer_keystore_passphrase", "")
    with pytest.raises(HotSignerPersistError):
        persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY)


def test_persist_from_private_key_encrypts_keystore_and_updates_settings(_isolated_keystore, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")

    address = persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY, passphrase=TEST_PASSPHRASE)

    assert address == TEST_ADDRESS
    assert settings.hot_signer_enabled is True
    assert settings.hot_signer_private_key.lower() == TEST_PRIVATE_KEY.lower()

    # File on disk must exist and must NOT contain the plaintext key or
    # its 0x-hex form anywhere -- only ciphertext bytes.
    assert _isolated_keystore.exists()
    raw = _isolated_keystore.read_bytes()
    assert TEST_PRIVATE_KEY.encode() not in raw
    assert TEST_PRIVATE_KEY[2:].encode() not in raw  # without 0x prefix too

    # And it must actually decrypt back to the right key under the right
    # passphrase, and refuse under a wrong one.
    from backend.wallet.keystore import Keystore, KeystoreLocked

    ks = Keystore(_isolated_keystore)
    assert ks.load(TEST_PASSPHRASE).lower() == TEST_PRIVATE_KEY.lower()
    with pytest.raises(KeystoreLocked):
        ks.load("wrong-passphrase")


def test_persist_from_seed_phrase_derives_first_account(_isolated_keystore, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")

    address = persist_hot_signer_secret(seed_phrase=TEST_MNEMONIC, passphrase=TEST_PASSPHRASE)

    expected = Account.from_mnemonic(TEST_MNEMONIC, account_path="m/44'/60'/0'/0/0").address
    assert address == expected
    assert settings.hot_signer_enabled is True
    assert settings.hot_signer_private_key.startswith("0x")


def test_persist_rejects_invalid_seed_phrase(_isolated_keystore):
    with pytest.raises(HotSignerPersistError):
        persist_hot_signer_secret(seed_phrase="not a real seed phrase at all", passphrase=TEST_PASSPHRASE)


def test_persist_rejects_invalid_private_key(_isolated_keystore):
    with pytest.raises(HotSignerPersistError):
        persist_hot_signer_secret(private_key="not-a-key", passphrase=TEST_PASSPHRASE)


def test_unlock_hot_signer_round_trips_through_keystore(_isolated_keystore, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")
    persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY, passphrase=TEST_PASSPHRASE)

    # Simulate a fresh process: clear the in-memory settings, then unlock.
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")

    from backend.wallet.hot_signer import unlock_hot_signer

    address = unlock_hot_signer(passphrase=TEST_PASSPHRASE)
    assert address == TEST_ADDRESS
    assert settings.hot_signer_private_key.lower() == TEST_PRIVATE_KEY.lower()
    assert settings.hot_signer_enabled is True


def test_unlock_hot_signer_wrong_passphrase_raises(_isolated_keystore, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")
    persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY, passphrase=TEST_PASSPHRASE)

    from backend.wallet.hot_signer import HotSignerDisabled, unlock_hot_signer

    with pytest.raises(HotSignerDisabled):
        unlock_hot_signer(passphrase="wrong-passphrase")
