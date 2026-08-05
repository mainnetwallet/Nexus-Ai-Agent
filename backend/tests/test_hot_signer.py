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
    monkeypatch.setattr(settings, "hot_signer_keys", {})
    monkeypatch.setattr(settings, "hot_signer_labels", {})
    monkeypatch.setattr(settings, "hot_signer_active_address", "")


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

    async def fake_rpc_call(rpc_candidates, method, params):
        calls.append(method)
        if method == "eth_getTransactionCount":
            return "0x5"
        if method == "eth_gasPrice":
            return "0x3b9aca00"  # 1 gwei
        if method == "eth_estimateGas":
            return "0x5208"  # 21000
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
    assert calls == ["eth_getTransactionCount", "eth_gasPrice", "eth_estimateGas", "eth_sendRawTransaction"]
    assert recorded["event_type"] == "hot_signer_native_send"
    assert recorded["metadata"]["tx_hash"] == "0xdeadbeef"


# ---------------------------------------------------------------------- #
# Batch sends (1->many / many->1 / many->many)
# ---------------------------------------------------------------------- #

SECOND_PRIVATE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690"
SECOND_ADDRESS = Account.from_key(SECOND_PRIVATE_KEY).address


def _fake_rpc_call_factory():
    """Fake _rpc_call answering nonce/gasPrice/estimateGas/send generically,
    tracking nonces per from-address so sequential legs from the same
    sender see incrementing nonces like a real 'pending' RPC would."""
    nonces: dict[str, int] = {}

    async def fake_rpc_call(rpc_candidates, method, params):
        if method == "eth_getTransactionCount":
            addr = params[0]
            n = nonces.get(addr, 0)
            nonces[addr] = n + 1
            return hex(n)
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        if method == "eth_estimateGas":
            return "0x5208"
        if method == "eth_sendRawTransaction":
            return "0xdeadbeef"
        raise AssertionError(f"unexpected method {method}")

    return fake_rpc_call


@pytest.mark.asyncio
async def test_send_native_batch_one_to_many(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_keys", {TEST_ADDRESS: TEST_PRIVATE_KEY})
    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(_fake_rpc_call_factory()))

    signer = HotSigner(wallet_registry=None)
    to_addrs = ["0x" + "2" * 40, "0x" + "3" * 40, "0x" + "4" * 40]
    result = await signer.send_native_batch("base", [TEST_ADDRESS], to_addrs, 0.000001)

    assert result.chain == "base"
    assert result.succeeded == 3
    assert result.failed == 0
    assert [leg.to_address for leg in result.legs] == to_addrs
    assert all(leg.from_address == TEST_ADDRESS for leg in result.legs)
    assert all(leg.tx_hash == "0xdeadbeef" for leg in result.legs)


@pytest.mark.asyncio
async def test_send_native_batch_many_to_one(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(
        settings, "hot_signer_keys", {TEST_ADDRESS: TEST_PRIVATE_KEY, SECOND_ADDRESS: SECOND_PRIVATE_KEY}
    )
    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(_fake_rpc_call_factory()))

    signer = HotSigner(wallet_registry=None)
    to_addr = "0x" + "9" * 40
    result = await signer.send_native_batch("base", [TEST_ADDRESS, SECOND_ADDRESS], [to_addr], 0.000001)

    assert result.succeeded == 2
    assert result.failed == 0
    assert [leg.from_address for leg in result.legs] == [TEST_ADDRESS, SECOND_ADDRESS]
    assert all(leg.to_address == to_addr for leg in result.legs)


@pytest.mark.asyncio
async def test_send_native_batch_many_to_many_paired(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(
        settings, "hot_signer_keys", {TEST_ADDRESS: TEST_PRIVATE_KEY, SECOND_ADDRESS: SECOND_PRIVATE_KEY}
    )
    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(_fake_rpc_call_factory()))

    signer = HotSigner(wallet_registry=None)
    to_addrs = ["0x" + "9" * 40, "0x" + "8" * 40]
    result = await signer.send_native_batch("base", [TEST_ADDRESS, SECOND_ADDRESS], to_addrs, 0.000001)

    assert result.succeeded == 2
    assert [(leg.from_address, leg.to_address) for leg in result.legs] == [
        (TEST_ADDRESS, to_addrs[0]),
        (SECOND_ADDRESS, to_addrs[1]),
    ]


@pytest.mark.asyncio
async def test_send_native_batch_mismatched_counts_raises(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(
        settings, "hot_signer_keys",
        {TEST_ADDRESS: TEST_PRIVATE_KEY, SECOND_ADDRESS: SECOND_PRIVATE_KEY},
    )
    signer = HotSigner(wallet_registry=None)
    to_addrs = ["0x" + "9" * 40, "0x" + "8" * 40, "0x" + "7" * 40]

    with pytest.raises(HotSignerError, match="Can't match"):
        await signer.send_native_batch("base", [TEST_ADDRESS, SECOND_ADDRESS], to_addrs, 0.000001)


@pytest.mark.asyncio
async def test_send_native_batch_one_bad_leg_does_not_stop_others(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_keys", {TEST_ADDRESS: TEST_PRIVATE_KEY})

    call_count = {"n": 0}

    async def flaky_rpc_call(rpc_candidates, method, params):
        if method == "eth_getTransactionCount":
            return hex(call_count["n"])
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        if method == "eth_estimateGas":
            return "0x5208"
        if method == "eth_sendRawTransaction":
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise HotSignerError("RPC error on eth_sendRawTransaction: boom")
            return "0xdeadbeef"
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(flaky_rpc_call))

    signer = HotSigner(wallet_registry=None)
    to_addrs = ["0x" + "2" * 40, "0x" + "3" * 40, "0x" + "4" * 40]
    result = await signer.send_native_batch("base", [TEST_ADDRESS], to_addrs, 0.000001)

    assert result.succeeded == 2
    assert result.failed == 1
    assert result.legs[1].ok is False
    assert "boom" in result.legs[1].error
    assert result.legs[0].ok is True
    assert result.legs[2].ok is True


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


def test_persist_without_passphrase_auto_generates_local_passphrase(_isolated_keystore, monkeypatch, tmp_path):
    # No explicit passphrase and no KEYSTORE_PASSPHRASE env var -- this must
    # NOT block on stdin (the real caller is a request handler). Per
    # keystore.get_passphrase_noninteractive(), it now falls back to a
    # local auto-generated passphrase file instead of raising.
    monkeypatch.delenv("KEYSTORE_PASSPHRASE", raising=False)
    monkeypatch.setattr(settings, "hot_signer_keystore_passphrase", "")

    import backend.config.settings as settings_module
    monkeypatch.setattr(settings_module, "BASE_DIR", tmp_path)

    address = persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY)

    assert address == TEST_ADDRESS
    passphrase_file = tmp_path / ".keystore_passphrase"
    assert passphrase_file.exists()

    # And the key must actually be recoverable using that auto-generated
    # passphrase, matching whatever the keystore file was encrypted with.
    from backend.wallet.keystore import Keystore

    auto_passphrase = passphrase_file.read_text().strip()
    ks = Keystore(_isolated_keystore)
    entries = ks.load_keys(auto_passphrase)
    assert entries[TEST_ADDRESS]["private_key"].lower() == TEST_PRIVATE_KEY.lower()


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
    entries = ks.load_keys(TEST_PASSPHRASE)
    assert entries[TEST_ADDRESS]["private_key"].lower() == TEST_PRIVATE_KEY.lower()
    with pytest.raises(KeystoreLocked):
        ks.load_keys("wrong-passphrase")


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


# ---------------------------------------------------------------------- #
# Multi-key keystore: a second saved key must NOT evict the first
# ---------------------------------------------------------------------- #

TEST_PRIVATE_KEY_2 = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690"
TEST_ADDRESS_2 = Account.from_key(TEST_PRIVATE_KEY_2).address


def test_second_persisted_key_does_not_evict_first(_isolated_keystore, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")

    addr1 = persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY, passphrase=TEST_PASSPHRASE, label="key1")
    addr2 = persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY_2, passphrase=TEST_PASSPHRASE, label="key2")

    assert addr1 == TEST_ADDRESS
    assert addr2 == TEST_ADDRESS_2

    # Both keys must be loaded in memory -- saving key2 must not wipe key1.
    assert set(settings.hot_signer_keys.keys()) == {TEST_ADDRESS, TEST_ADDRESS_2}
    assert settings.hot_signer_keys[TEST_ADDRESS].lower() == TEST_PRIVATE_KEY.lower()
    assert settings.hot_signer_keys[TEST_ADDRESS_2].lower() == TEST_PRIVATE_KEY_2.lower()

    # And both must be recoverable from disk, not just from settings.
    from backend.wallet.keystore import Keystore

    ks = Keystore(_isolated_keystore)
    entries = ks.load_keys(TEST_PASSPHRASE)
    assert set(entries.keys()) == {TEST_ADDRESS, TEST_ADDRESS_2}

    # The most recently saved key becomes active by default (matches the
    # old single-key behavior for anyone not explicitly managing multiple).
    assert settings.hot_signer_active_address == TEST_ADDRESS_2
    assert settings.hot_signer_private_key.lower() == TEST_PRIVATE_KEY_2.lower()


def test_set_active_hot_signer_switches_without_dropping_keys(_isolated_keystore, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")

    from backend.wallet.hot_signer import list_hot_signers, set_active_hot_signer

    persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY, passphrase=TEST_PASSPHRASE, label="key1")
    persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY_2, passphrase=TEST_PASSPHRASE, label="key2")
    assert settings.hot_signer_active_address == TEST_ADDRESS_2

    switched = set_active_hot_signer(TEST_ADDRESS)
    assert switched == TEST_ADDRESS
    assert settings.hot_signer_active_address == TEST_ADDRESS
    assert settings.hot_signer_private_key.lower() == TEST_PRIVATE_KEY.lower()

    signers = list_hot_signers()
    assert {s["address"] for s in signers} == {TEST_ADDRESS, TEST_ADDRESS_2}
    # Every loaded key is active -- switching the default never deactivates
    # the others; only is_default moves.
    assert all(s["active"] is True for s in signers)
    default_flags = {s["address"]: s["is_default"] for s in signers}
    assert default_flags[TEST_ADDRESS] is True
    assert default_flags[TEST_ADDRESS_2] is False


def test_unlock_hot_signer_migrates_legacy_single_key_file(_isolated_keystore, monkeypatch):
    """A keystore file written by the OLD single-secret Keystore.save()
    (raw key as the whole plaintext, no address indexing) must still
    unlock cleanly and end up address-indexed on disk afterward."""
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")

    from backend.wallet.hot_signer import unlock_hot_signer
    from backend.wallet.keystore import Keystore, _derive_fernet
    import os

    # Hand-write a legacy-format file: salt + Fernet(raw key string), no JSON.
    salt = os.urandom(16)
    fernet = _derive_fernet(TEST_PASSPHRASE, salt)
    token = fernet.encrypt(TEST_PRIVATE_KEY.encode("utf-8"))
    _isolated_keystore.write_bytes(salt + token)

    address = unlock_hot_signer(passphrase=TEST_PASSPHRASE)
    assert address == TEST_ADDRESS
    assert settings.hot_signer_keys[TEST_ADDRESS].lower() == TEST_PRIVATE_KEY.lower()

    # File on disk should now be migrated to the address-indexed format.
    ks = Keystore(_isolated_keystore)
    entries = ks.load_keys(TEST_PASSPHRASE)
    assert set(entries.keys()) == {TEST_ADDRESS}


# ---------------------------------------------------------------------- #
# Every imported hot signer stays active -- a send with no from_address
# only works implicitly while exactly one key is loaded.
# ---------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_send_native_with_single_loaded_key_needs_no_from_address(_isolated_keystore, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")

    persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY, passphrase=TEST_PASSPHRASE)

    async def fake_rpc_call(rpc_candidates, method, params):
        if method == "eth_getTransactionCount":
            return "0x0"
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        return "0xabc"

    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(fake_rpc_call))

    signer = HotSigner()
    result = await signer.send_native("base", "0x" + "2" * 40, 0.001)
    assert result.from_address == TEST_ADDRESS


@pytest.mark.asyncio
async def test_send_native_with_multiple_active_keys_requires_from_address(_isolated_keystore, monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", False)
    monkeypatch.setattr(settings, "hot_signer_private_key", "")

    persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY, passphrase=TEST_PASSPHRASE)
    persist_hot_signer_secret(private_key=TEST_PRIVATE_KEY_2, passphrase=TEST_PASSPHRASE)

    signer = HotSigner()
    with pytest.raises(HotSignerError, match="specify from_address"):
        await signer.send_native("base", "0x" + "2" * 40, 0.001)

    # But an explicit from_address among the active keys still works.
    async def fake_rpc_call(rpc_candidates, method, params):
        if method == "eth_getTransactionCount":
            return "0x0"
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        return "0xabc"

    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(fake_rpc_call))
    result = await signer.send_native("base", "0x" + "2" * 40, 0.001, from_address=TEST_ADDRESS)
    assert result.from_address == TEST_ADDRESS


# ---------------------------------------------------------------------- #
# ERC20 token transfers (send_token)
# ---------------------------------------------------------------------- #

TOKEN_ADDRESS = "0x" + "9" * 40


@pytest.mark.asyncio
async def test_send_token_success_reads_decimals_and_checks_balance(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)

    calls = []

    async def fake_rpc_call(rpc_candidates, method, params):
        calls.append((method, params))
        if method == "eth_call":
            data = params[0]["data"]
            if data == "0x313ce567":  # decimals()
                return hex(6)  # e.g. USDC-style 6 decimals
            if data.startswith("0x70a08231"):  # balanceOf(...)
                return hex(10 * 10**6)  # plenty of balance
            raise AssertionError(f"unexpected eth_call data {data}")
        if method == "eth_getTransactionCount":
            return "0x2"
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        if method == "eth_estimateGas":
            return hex(52000)
        if method == "eth_sendRawTransaction":
            return "0xtokentxhash"
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(fake_rpc_call))

    signer = HotSigner()
    to_addr = "0x" + "2" * 40
    result = await signer.send_token("base", TOKEN_ADDRESS, to_addr, 2.5)

    assert result.tx_hash == "0xtokentxhash"
    assert result.chain == "base"
    assert result.token_address == TOKEN_ADDRESS
    assert result.from_address == TEST_ADDRESS
    assert result.to_address == to_addr
    assert result.decimals == 6
    assert result.amount_raw == 2_500_000  # 2.5 * 10**6
    methods_called = [m for m, _ in calls]
    assert methods_called == [
        "eth_call", "eth_call", "eth_getTransactionCount", "eth_gasPrice", "eth_estimateGas", "eth_sendRawTransaction",
    ]


@pytest.mark.asyncio
async def test_send_token_explicit_decimals_skips_decimals_call(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)

    calls = []

    async def fake_rpc_call(rpc_candidates, method, params):
        calls.append(method)
        if method == "eth_call":
            return hex(10**24)  # huge balance, plenty
        if method == "eth_getTransactionCount":
            return "0x0"
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        if method == "eth_estimateGas":
            return hex(52000)
        if method == "eth_sendRawTransaction":
            return "0xtokentxhash"
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(fake_rpc_call))

    signer = HotSigner()
    result = await signer.send_token("base", TOKEN_ADDRESS, "0x" + "2" * 40, 1.0, decimals=18)
    assert result.decimals == 18
    assert result.amount_raw == 10**18
    # Only ONE eth_call (balanceOf) -- decimals() must be skipped since it was given explicitly.
    assert calls.count("eth_call") == 1


@pytest.mark.asyncio
async def test_send_token_insufficient_balance_rejected(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)

    async def fake_rpc_call(rpc_candidates, method, params):
        if method == "eth_call":
            data = params[0]["data"]
            if data == "0x313ce567":
                return hex(18)
            return hex(0)  # zero balance
        raise AssertionError(f"unexpected method {method} before balance check should have stopped it")

    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(fake_rpc_call))

    signer = HotSigner()
    with pytest.raises(HotSignerError, match="Insufficient token balance"):
        await signer.send_token("base", TOKEN_ADDRESS, "0x" + "2" * 40, 5.0)


@pytest.mark.asyncio
async def test_send_token_invalid_token_address_rejected(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)

    signer = HotSigner()
    with pytest.raises(HotSignerError, match="Invalid token contract address"):
        await signer.send_token("base", "not-a-token-address", "0x" + "2" * 40, 1.0)


@pytest.mark.asyncio
async def test_send_token_estimate_gas_failure_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "hot_signer_enabled", True)
    monkeypatch.setattr(settings, "hot_signer_private_key", TEST_PRIVATE_KEY)

    seen_tx = {}

    async def fake_rpc_call(rpc_candidates, method, params):
        if method == "eth_call":
            data = params[0]["data"]
            return hex(18) if data == "0x313ce567" else hex(10**24)
        if method == "eth_getTransactionCount":
            return "0x0"
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        if method == "eth_estimateGas":
            raise RuntimeError("this node refuses estimateGas")
        if method == "eth_sendRawTransaction":
            return "0xtokentxhash"
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(HotSigner, "_rpc_call", staticmethod(fake_rpc_call))

    signer = HotSigner()
    result = await signer.send_token("base", TOKEN_ADDRESS, "0x" + "2" * 40, 1.0)
    assert result.tx_hash == "0xtokentxhash"  # send still succeeds via the flat gas fallback
