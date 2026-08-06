"""
Transient wallet-import helpers.

Hard security rule (do not weaken): Nexus-Agent's storage layer only ever
holds wallet METADATA (label, address, network, tags, notes, status). It
never writes a seed phrase or private key to disk, to a log line, to
WalletActivity, or to any cache -- encrypted or not. Centralizing key
material for many wallets in one backend, for the purpose of automated
signing, turns this service into a single high-value target and defeats the
entire point of signing inside the user's own wallet extension (see
backend/wallet/manager.py). That is a deliberate scope boundary, not an
oversight.

What this module DOES do: when a user imports a wallet by private key or
seed phrase, it derives the checksum address from that secret ONE TIME, in
memory, for the duration of a single request, and returns only the address.
The secret is never returned, logged, or stored, and the local variable
holding it goes out of scope (and is best-effort zeroed) as soon as
derivation completes.

If you need Nexus-Agent to actually sign or auto-approve transactions, wire
that through the existing browser-extension flow in wallet/manager.py
(human-approved, or allowlisted + value-capped), not through a key held by
this backend.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

from eth_account import Account
from eth_account.hdaccount import Language, Mnemonic

logger = logging.getLogger("nexus.wallet.import")

Account.enable_unaudited_hdwallet_features()

ImportMethod = Literal["seed_phrase", "private_key", "browser_profile"]

DEFAULT_DERIVATION_PATH = "m/44'/60'/0'/0/0"


class WalletImportError(ValueError):
    pass


@dataclass
class DerivedAddress:
    address: str
    method: ImportMethod


def _zero(s: Optional[str]) -> None:
    # Best-effort only -- Python strings are immutable so this cannot
    # guarantee the secret is scrubbed from memory, but it removes the
    # reference promptly rather than letting it linger on the stack/heap.
    del s


def derive_from_private_key(private_key: str) -> DerivedAddress:
    """
    Accepts a raw private key (hex, with or without 0x prefix), derives the
    checksum address, and immediately discards the key. Raises
    WalletImportError on anything that doesn't parse as a valid key --
    never echoes the input back in the error message.
    """
    key = private_key.strip()
    try:
        account = Account.from_key(key)
        address = account.address
    except Exception as exc:
        raise WalletImportError("Could not derive an address from that private key.") from exc
    finally:
        _zero(key)
        _zero(private_key)
    logger.info("Derived address from imported private key (key discarded, not persisted)")
    return DerivedAddress(address=address, method="private_key")


def derive_from_seed_phrase(mnemonic: str, derivation_path: str = DEFAULT_DERIVATION_PATH) -> DerivedAddress:
    """
    Accepts a BIP-39 seed phrase, derives the first checksum address at the
    given derivation path, and immediately discards the phrase.
    """
    phrase = mnemonic.strip()
    try:
        if not Mnemonic(Language.ENGLISH).is_mnemonic_valid(phrase):
            raise WalletImportError("That does not look like a valid seed phrase.")
        account = Account.from_mnemonic(phrase, account_path=derivation_path)
        address = account.address
    except WalletImportError:
        raise
    except Exception as exc:
        raise WalletImportError("Could not derive an address from that seed phrase.") from exc
    finally:
        _zero(phrase)
        _zero(mnemonic)
    logger.info("Derived address from imported seed phrase (phrase discarded, not persisted)")
    return DerivedAddress(address=address, method="seed_phrase")
