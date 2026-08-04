"""
Regression coverage for backend/wallet/import_utils.py.

Includes a regression test for a latent bug found while building the hot
signer persistence feature: Mnemonic.is_mnemonic_valid is an instance
method on the installed eth-account version, so derive_from_seed_phrase's
old `Mnemonic.is_mnemonic_valid(phrase)` call (unbound, missing self) raised
TypeError for every seed-phrase import. Fixed to `Mnemonic("english")
.is_mnemonic_valid(phrase)`.
"""
from __future__ import annotations

import pytest
from eth_account import Account

from backend.wallet.import_utils import (
    WalletImportError,
    derive_from_private_key,
    derive_from_seed_phrase,
)

TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_MNEMONIC = "test test test test test test test test test test test junk"


def test_derive_from_private_key():
    derived = derive_from_private_key(TEST_PRIVATE_KEY)
    assert derived.address == Account.from_key(TEST_PRIVATE_KEY).address
    assert derived.method == "private_key"


def test_derive_from_private_key_rejects_garbage():
    with pytest.raises(WalletImportError):
        derive_from_private_key("not-a-key")


def test_derive_from_seed_phrase_does_not_raise_type_error():
    # Regression: this used to raise TypeError (missing 'mnemonic' arg)
    # instead of either succeeding or raising WalletImportError.
    derived = derive_from_seed_phrase(TEST_MNEMONIC)
    expected = Account.from_mnemonic(TEST_MNEMONIC, account_path="m/44'/60'/0'/0/0").address
    assert derived.address == expected
    assert derived.method == "seed_phrase"


def test_derive_from_seed_phrase_rejects_invalid_phrase():
    with pytest.raises(WalletImportError):
        derive_from_seed_phrase("not a real seed phrase at all")
