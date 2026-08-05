"""
HotSigner: direct RPC signing for native-token transfers.

This is a deliberately separate, opt-in path from WalletManager
(backend/wallet/manager.py) and WalletRegistry (backend/wallet/registry.py).
Those two modules never touch a private key -- signing there always happens
inside the user's own browser-extension wallet, with a human (or an
explicit allowlist+cap policy) approving every popup.

HotSigner is the opposite tradeoff: the backend process holds a private key
in memory (from HOT_SIGNER_PRIVATE_KEY, an env var -- never written to the
DB, a log line, or the wallet activity table) and signs + broadcasts a raw
transaction itself via JSON-RPC. There is no approval step. This only makes
sense for a burner/bot wallet with funds you can afford to lose to a bug --
do not point this at a wallet holding real value.

Hard rules for this module:
- Disabled unless settings.hot_signer_enabled is True.
- The private key never leaves this module as a return value, log message,
  or activity-log entry. Only the derived address and tx hash are ever
  surfaced.
- Every send is recorded via WalletRegistry.record_activity (address/amount/
  chain/tx hash only) so there's an audit trail even without an approval
  step.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from eth_account import Account
from eth_account.hdaccount import Mnemonic

from backend.config.settings import BASE_DIR, settings
from backend.wallet.chain_resolver import get_rpc_candidates, resolve_chain, rpc_post_with_fallback
from backend.wallet.chains import ChainInfo, chain_by_key
from backend.wallet.keystore import (
    LEGACY_ENTRY_ID,
    Keystore,
    KeystoreError,
    KeystoreLocked,
    get_passphrase_interactive,
    get_passphrase_noninteractive,
)

logger = logging.getLogger("nexus.wallet.hot_signer")

KEYSTORE_PATH = BASE_DIR / "hot_signer.keystore"
_keystore = Keystore(KEYSTORE_PATH)

Account.enable_unaudited_hdwallet_features()


class HotSignerError(Exception):
    pass


class HotSignerDisabled(HotSignerError):
    pass


class HotSignerPersistError(HotSignerError):
    pass


@dataclass
class NativeTransferResult:
    tx_hash: str
    chain: str
    from_address: str
    to_address: str
    amount_native: float
    amount_wei: int


@dataclass
class TokenTransferResult:
    tx_hash: str
    chain: str
    token_address: str
    from_address: str
    to_address: str
    amount_tokens: float
    amount_raw: int
    decimals: int


@dataclass
class BatchLegResult:
    """Outcome of one from/to pair within a batch send."""
    from_address: str
    to_address: str
    ok: bool
    tx_hash: Optional[str] = None
    error: Optional[str] = None


@dataclass
class BatchTransferResult:
    chain: str
    legs: list[BatchLegResult]

    @property
    def succeeded(self) -> int:
        return sum(1 for leg in self.legs if leg.ok)

    @property
    def failed(self) -> int:
        return sum(1 for leg in self.legs if not leg.ok)


def _pair_addresses(from_addresses: list[str], to_addresses: list[str]) -> list[tuple[str, str]]:
    """
    Turn a from-list and to-list into (from, to) pairs, covering all four
    shapes the hot signer's batch sends support:
      - 1 from  , 1 to   -> single pair (ordinary send)
      - 1 from  , N to   -> broadcast: that one wallet pays out to every to-address
      - N from  , 1 to   -> collect: every from-wallet sends to that one to-address
      - N from  , N to   -> paired in order (from[i] -> to[i]); counts must match
    Raises HotSignerError for any other shape (e.g. 3 from vs 5 to), since
    there's no unambiguous way to match them.
    """
    if not from_addresses or not to_addresses:
        raise HotSignerError("Need at least one sender and one recipient address for a batch send.")
    if len(from_addresses) == 1:
        return [(from_addresses[0], to) for to in to_addresses]
    if len(to_addresses) == 1:
        return [(frm, to_addresses[0]) for frm in from_addresses]
    if len(from_addresses) == len(to_addresses):
        return list(zip(from_addresses, to_addresses))
    raise HotSignerError(
        f"Can't match {len(from_addresses)} sender(s) to {len(to_addresses)} recipient(s) -- "
        "use one sender, one recipient, or equal counts of each (paired in the order given)."
    )


# --- Minimal ERC20 ABI encoding, no web3.py Contract dependency needed --- #
_ERC20_TRANSFER_SELECTOR = "a9059cbb"    # transfer(address,uint256)
_ERC20_DECIMALS_SELECTOR = "313ce567"    # decimals()
_ERC20_BALANCE_OF_SELECTOR = "70a08231"  # balanceOf(address)

GAS_BUMP_PCT = 0.05  # +5% margin on both gas limit and gas price for every hot-signer tx


def _bump(value: int) -> int:
    """Apply the +5% gas margin, rounding up (ceil) so small values still get bumped."""
    return value + math.ceil(value * GAS_BUMP_PCT)


def _encode_address_param(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _encode_uint256_param(value: int) -> str:
    if value < 0:
        raise HotSignerError("Cannot encode a negative uint256")
    return format(value, "x").rjust(64, "0")


def _erc20_transfer_calldata(to_address: str, amount_raw: int) -> str:
    return "0x" + _ERC20_TRANSFER_SELECTOR + _encode_address_param(to_address) + _encode_uint256_param(amount_raw)


def _erc20_balance_of_calldata(owner_address: str) -> str:
    return "0x" + _ERC20_BALANCE_OF_SELECTOR + _encode_address_param(owner_address)


_INSUFFICIENT_FUNDS_RE = re.compile(
    r"insufficient funds[^,}]*?have\s+(\d+)[^,}]*?want\s+(\d+)", re.IGNORECASE
)


def _friendly_insufficient_funds_message(exc: Exception) -> Optional[str]:
    """
    If `exc` came from a JSON-RPC "insufficient funds ... have X want Y" error
    (X/Y in wei), return a clean human-readable message with amounts
    converted to native-token units. Returns None for any other error, so
    callers fall back to the raw RPC error text unchanged.
    """
    match = _INSUFFICIENT_FUNDS_RE.search(str(exc))
    if not match:
        return None
    have_wei, want_wei = int(match.group(1)), int(match.group(2))
    have = have_wei / 1e18
    want = want_wei / 1e18
    return (
        f"Insufficient funds: have {have:.9f}, need {want:.9f} "
        "(transfer amount + gas). Top up the wallet and try again."
    )


def _find_loaded_key(address: str) -> Optional[str]:
    """Case-insensitive lookup of a key already loaded into
    settings.hot_signer_keys for this process."""
    target = address.lower()
    for addr, key_hex in settings.hot_signer_keys.items():
        if addr.lower() == target:
            return key_hex
    return None


def _canonical_loaded_address(address: str) -> Optional[str]:
    """Returns the address exactly as stored in settings.hot_signer_keys
    (correct checksum casing) for a case-insensitive match, or None."""
    target = address.lower()
    for addr in settings.hot_signer_keys:
        if addr.lower() == target:
            return addr
    return None


def _require_enabled(from_address: Optional[str] = None) -> str:
    if not settings.hot_signer_enabled:
        raise HotSignerDisabled(
            "Hot signer is disabled. Set HOT_SIGNER_ENABLED=true and HOT_SIGNER_PRIVATE_KEY "
            "in the environment (or import+save a key) to enable direct RPC sends "
            "(burner wallets only)."
        )
    if from_address:
        key = _find_loaded_key(from_address)
        if key is None:
            raise HotSignerError(
                f"No hot signer key loaded for address {from_address!r}. "
                "Import it with save_as_hot_signer, or unlock the keystore, first."
            )
        return key

    # No from_address given. Every imported/unlocked key is auto-active --
    # there's no single "the" hot signer anymore, so with more than one key
    # loaded we can't silently guess which one to sign with; the caller
    # must say which address to send from.
    if len(settings.hot_signer_keys) > 1:
        addrs = ", ".join(settings.hot_signer_keys)
        raise HotSignerError(
            f"{len(settings.hot_signer_keys)} hot signer keys are active -- specify from_address "
            f"(one of: {addrs})."
        )
    if len(settings.hot_signer_keys) == 1:
        return next(iter(settings.hot_signer_keys.values()))

    # Nothing loaded via the keystore/multi-key path -- fall back to a
    # directly-set HOT_SIGNER_PRIVATE_KEY (e.g. set straight in .env/tests).
    key = settings.hot_signer_private_key.strip()
    if not key:
        raise HotSignerDisabled("HOT_SIGNER_PRIVATE_KEY is not set.")
    return key


def get_hot_signer_address() -> Optional[str]:
    """Returns the address of the default hot signer (used when a send
    omits from_address and only one key is loaded), or None if
    disabled/unset. Use list_hot_signers() to see every key currently
    loaded and active -- with more than one key loaded, ALL of them are
    active, not just this one."""
    key = settings.hot_signer_private_key.strip()
    if not key:
        return None
    try:
        return Account.from_key(key).address
    except Exception:
        return None


def list_hot_signers() -> list[dict]:
    """Lists every hot signer key currently loaded into this process
    (from unlock_hot_signer at startup and/or persist_hot_signer_secret
    calls since), without ever exposing the private keys themselves.
    EVERY imported/unlocked key is active -- importing another one never
    deactivates the ones already loaded. `is_default` marks the one used
    when a send omits from_address (only meaningful when exactly one key
    is loaded; with several, from_address is required on every send)."""
    default_addr = settings.hot_signer_active_address
    return [
        {
            "address": addr,
            "label": settings.hot_signer_labels.get(addr),
            "active": True,
            "is_default": addr.lower() == default_addr.lower() if default_addr else False,
        }
        for addr in settings.hot_signer_keys
    ]


def set_active_hot_signer(address: str) -> str:
    """
    Sets which already-loaded hot signer key is the DEFAULT for a send
    that omits from_address. Every loaded key is already active/usable via
    from_address regardless of this setting -- this only matters when the
    caller doesn't specify which one to use. Does not touch the keystore
    file or drop any other key. Returns the canonical (checksummed)
    address. Raises HotSignerError if that address isn't currently loaded.
    """
    canonical = _canonical_loaded_address(address)
    if canonical is None:
        raise HotSignerError(
            f"No hot signer key loaded for address {address!r}. "
            "Import/unlock it before setting it as default."
        )
    settings.hot_signer_active_address = canonical
    settings.hot_signer_private_key = settings.hot_signer_keys[canonical]
    settings.hot_signer_enabled = True
    logger.info("Hot signer default address switched to %s", canonical)
    return canonical


def remove_hot_signer(address: str, passphrase: Optional[str] = None) -> bool:
    """
    Deletes one hot signer key from the encrypted keystore file AND from
    this process's in-memory settings. If the removed key was the active
    one, falls back to another loaded key (arbitrary pick) or, if none
    remain, disables the hot signer entirely. Returns False if the address
    wasn't loaded/found.
    """
    canonical = _canonical_loaded_address(address)
    if canonical is None:
        return False

    pass_ = passphrase or settings.hot_signer_keystore_passphrase or None
    if not pass_:
        try:
            pass_ = get_passphrase_noninteractive()
        except KeystoreError as exc:
            raise HotSignerPersistError(
                "Set KEYSTORE_PASSPHRASE in the environment (or pass a passphrase "
                "explicitly) before removing a hot signer key."
            ) from exc

    try:
        _keystore.remove_key(canonical, pass_)
    except KeystoreLocked as exc:
        raise HotSignerPersistError(str(exc)) from exc

    del settings.hot_signer_keys[canonical]
    settings.hot_signer_labels.pop(canonical, None)

    if settings.hot_signer_active_address.lower() == canonical.lower():
        remaining = next(iter(settings.hot_signer_keys), None)
        if remaining:
            settings.hot_signer_active_address = remaining
            settings.hot_signer_private_key = settings.hot_signer_keys[remaining]
        else:
            settings.hot_signer_active_address = ""
            settings.hot_signer_private_key = ""
            settings.hot_signer_enabled = False

    logger.info("Hot signer key removed (address=%s)", canonical)
    return True


def persist_hot_signer_secret(
    private_key: Optional[str] = None,
    seed_phrase: Optional[str] = None,
    derivation_path: str = "m/44'/60'/0'/0/0",
    passphrase: Optional[str] = None,
    label: Optional[str] = None,
    make_active: bool = True,
) -> str:
    """
    Opt-in escape hatch, deliberately separate from
    backend/wallet/import_utils.py's derive-then-discard rule: takes a
    private key OR seed phrase, derives its address, and encrypts the raw
    private key hex into a local keystore file (backend/wallet/keystore.py)
    instead of writing it to .env in plaintext, then updates the in-memory
    `settings` object so the hot signer is usable immediately, without a
    process restart.

    MULTI-KEY: this ADDS the derived address as one entry alongside
    whatever other hot signer keys are already loaded/stored -- it never
    overwrites or drops a previously saved key the way the old single-key
    keystore did. `make_active` (default True, matching the old
    always-becomes-the-signer behavior) selects this newly saved key as
    the one HotSigner.send_native() uses by default; pass False to add a
    second/third signer without switching away from whichever is currently
    active. Use set_active_hot_signer()/list_hot_signers() to manage
    multiple saved keys afterward.

    This function exists ONLY to back an explicit "save as hot signer"
    opt-in on the wallet-import flow (REST: ImportWalletRequest.
    save_as_hot_signer; chat: wallet_save_as_hot_signer). It must never be
    called implicitly on a plain import. Persisting a key at all -- even
    encrypted -- is exactly the tradeoff hot_signer.py's module docstring
    already describes: burner/bot wallets only, never a wallet holding real
    value.

    `passphrase` unlocks the keystore file; if not given, falls back to the
    KEYSTORE_PASSPHRASE env var (settings.hot_signer_keystore_passphrase).
    This function is reachable from an API route and from the chat engine,
    both of which may be running inside a live request -- so it NEVER
    prompts interactively. If no passphrase is available either way, it
    raises HotSignerPersistError immediately rather than blocking on stdin.
    The passphrase is not itself the wallet secret -- losing it just means
    the keystore file can't be decrypted, it doesn't expose the key.

    Returns only the derived address. The key itself is never logged,
    returned, or passed to WalletRegistry -- callers should log the
    resulting address via WalletRegistry.record_activity, not this
    function's input.
    """
    if bool(private_key) == bool(seed_phrase):
        raise HotSignerPersistError("Provide exactly one of private_key or seed_phrase.")

    try:
        if private_key:
            key_hex = private_key.strip()
            account = Account.from_key(key_hex)
        else:
            phrase = (seed_phrase or "").strip()
            if not Mnemonic("english").is_mnemonic_valid(phrase):
                raise HotSignerPersistError("That does not look like a valid seed phrase.")
            account = Account.from_mnemonic(phrase, account_path=derivation_path)
            key_hex = account.key.hex()
            if not key_hex.startswith("0x"):
                key_hex = "0x" + key_hex
    except HotSignerPersistError:
        raise
    except Exception as exc:
        raise HotSignerPersistError("Could not derive an address from that secret.") from exc

    address = account.address

    try:
        pass_ = passphrase or settings.hot_signer_keystore_passphrase or None
        if not pass_:
            try:
                pass_ = get_passphrase_noninteractive()
            except KeystoreError as exc:
                raise HotSignerPersistError(
                    "Set KEYSTORE_PASSPHRASE in the environment (or pass a passphrase "
                    "explicitly) before saving a hot signer key -- this call never prompts "
                    "interactively since it may be running inside a request."
                ) from exc
        # add_key() only touches this one entry -- every other key already
        # in the keystore file is preserved untouched.
        _keystore.add_key(address, key_hex, pass_, label=label)

        # Update the live settings object so this takes effect immediately,
        # without waiting for a process restart to re-read the keystore.
        settings.hot_signer_keys[address] = key_hex
        if label:
            settings.hot_signer_labels[address] = label
        if make_active or not settings.hot_signer_active_address:
            settings.hot_signer_active_address = address
            settings.hot_signer_private_key = key_hex
        settings.hot_signer_enabled = True
    except HotSignerPersistError:
        raise
    except OSError as exc:
        raise HotSignerPersistError(f"Could not write to {KEYSTORE_PATH}: {exc}") from exc
    finally:
        del key_hex
        del pass_

    logger.info(
        "Hot signer secret encrypted and saved to keystore (address=%s); key itself never logged.",
        address,
    )
    return address


def hot_signer_keystore_exists() -> bool:
    """Whether a hot-signer keystore file has been saved previously."""
    return _keystore.exists()


def unlock_hot_signer(passphrase: Optional[str] = None, interactive: bool = False) -> str:
    """
    Loads EVERY encrypted key from the keystore file into settings at
    process start (or on demand), so all previously-saved hot signers are
    usable without holding the passphrase in .env. Call this once at
    startup instead of relying on HOT_SIGNER_PRIVATE_KEY being set
    directly. Returns the ACTIVE address (settings.hot_signer_active_address
    if it was already set and is among the loaded keys, otherwise whichever
    key happens to load first) -- use list_hot_signers() to see all of them.

    A keystore file written before multi-key support (a single raw secret,
    no address indexing) is auto-migrated in place on this call: the key is
    re-derived to find its real address, re-saved under that address, and
    the old unindexed entry is removed -- one-time, transparent, no
    behavior change for a single-key deployment.

    `interactive=True` is only safe from a script you run yourself in a
    terminal (e.g. a startup entrypoint before the server starts accepting
    requests) -- it will fall back to a stdin prompt if KEYSTORE_PASSPHRASE
    isn't set. Leave it False (the default) anywhere that might run inside
    a live request; it will raise instead of blocking.
    """
    if not _keystore.exists():
        raise HotSignerDisabled(f"No hot signer keystore found at {KEYSTORE_PATH}.")

    pass_ = passphrase or settings.hot_signer_keystore_passphrase or None
    if not pass_:
        try:
            if interactive:
                pass_ = get_passphrase_interactive("Unlock hot signer keystore: ")
            else:
                pass_ = get_passphrase_noninteractive()
        except KeystoreError as exc:
            raise HotSignerDisabled(str(exc)) from exc
    try:
        entries = _keystore.load_keys(pass_)
    except KeystoreLocked as exc:
        raise HotSignerDisabled(str(exc)) from exc

    if not entries:
        raise HotSignerDisabled(f"Keystore at {KEYSTORE_PATH} contains no keys.")

    if LEGACY_ENTRY_ID in entries:
        legacy = entries.pop(LEGACY_ENTRY_ID)
        legacy_key_hex = legacy["private_key"]
        try:
            real_address = Account.from_key(legacy_key_hex).address
        finally:
            pass
        entries[real_address] = {"private_key": legacy_key_hex, "label": legacy.get("label")}
        _keystore.replace_all(entries, pass_)
        logger.info("Migrated legacy single-key keystore to multi-key format (address=%s)", real_address)
        del legacy_key_hex

    loaded_keys: dict[str, str] = {}
    loaded_labels: dict[str, str] = {}
    try:
        for addr, entry in entries.items():
            key_hex = entry["private_key"]
            # Re-derive to confirm the stored address actually matches its
            # key rather than trusting the dict key blindly.
            derived = Account.from_key(key_hex).address
            loaded_keys[derived] = key_hex
            if entry.get("label"):
                loaded_labels[derived] = entry["label"]

        settings.hot_signer_keys = loaded_keys
        settings.hot_signer_labels = loaded_labels

        active = settings.hot_signer_active_address
        if not active or not _canonical_loaded_address(active):
            active = next(iter(loaded_keys))
        else:
            active = _canonical_loaded_address(active)

        settings.hot_signer_active_address = active
        settings.hot_signer_private_key = loaded_keys[active]
        settings.hot_signer_enabled = True
    finally:
        del entries

    logger.info(
        "Hot signer keystore unlocked for this session (%d key(s), active=%s)",
        len(loaded_keys), active,
    )
    return active


class HotSigner:
    """
    Signs and broadcasts native-token transfers directly, without a browser
    or an approval popup. See module docstring for the security tradeoff.
    """

    def __init__(self, wallet_registry: Any = None) -> None:
        # Optional: if given a WalletRegistry, sends get logged to its
        # activity table (best-effort, never blocks the send on failure).
        self._registry = wallet_registry

    async def send_native(
        self,
        chain_key: str,
        to_address: str,
        amount_native: float,
        wallet_id: Optional[str] = None,
        from_address: Optional[str] = None,
    ) -> NativeTransferResult:
        """
        Send `amount_native` of the chain's native currency to `to_address`
        on `chain_key` (e.g. "base", "ethereum", "polygon" -- see
        backend/wallet/chains.py for the supported set).

        `from_address` picks which loaded hot signer key to sign with (see
        list_hot_signers()); omit it to use whichever key is currently
        active (settings.hot_signer_active_address / set_active_hot_signer()).
        """
        private_key = _require_enabled(from_address)

        chain = chain_by_key(chain_key)
        rpc_candidates: list[str]
        if chain is not None:
            rpc_candidates = get_rpc_candidates(chain)
        else:
            # Not one of the hardcoded chains -- try to resolve it (by name
            # or numeric chain id) against the chain registry before giving
            # up. Covers "send on avalanche" etc. without us having to
            # hand-wire every EVM chain in chains.py.
            chain, rpc_candidates = await resolve_chain(chain_key)
            if chain is None:
                raise HotSignerError(
                    f"Unsupported chain: {chain_key!r} "
                    "(not hardcoded and not found in the chain registry)"
                )

        if not rpc_candidates:
            raise HotSignerError(f"No usable RPC endpoint found for {chain.key!r}")

        if not to_address or not to_address.lower().startswith("0x") or len(to_address) != 42:
            raise HotSignerError(f"Invalid destination address: {to_address!r}")

        if amount_native <= 0:
            raise HotSignerError("Transfer amount must be positive")

        cap = settings.hot_signer_max_native_value
        if cap and amount_native > cap:
            raise HotSignerError(
                f"Transfer of {amount_native} exceeds HOT_SIGNER_MAX_NATIVE_VALUE cap ({cap})"
            )

        account = Account.from_key(private_key)
        from_address = account.address
        amount_wei = round(amount_native * 1e18)

        nonce = await self._rpc_call(
            rpc_candidates, "eth_getTransactionCount", [from_address, "pending"]
        )
        gas_price = _bump(int(await self._rpc_call(rpc_candidates, "eth_gasPrice", []), 16))
        gas_limit = _bump(await self._estimate_native_gas_with_fallback(
            rpc_candidates, from_address, to_address, amount_wei
        ))

        tx = {
            "chainId": chain.chain_id_int,
            "nonce": int(nonce, 16),
            "to": to_address,
            "value": amount_wei,
            "gas": gas_limit,
            "gasPrice": gas_price,
        }

        signed = account.sign_transaction(tx)
        tx_hash = await self._rpc_call(
            rpc_candidates, "eth_sendRawTransaction", [signed.raw_transaction.hex()]
        )

        logger.info(
            "Hot signer sent native transfer: chain=%s to=%s amount=%s tx=%s",
            chain.key, to_address, amount_native, tx_hash,
        )

        if self._registry is not None:
            try:
                await self._registry.record_activity(
                    wallet_id or from_address,
                    "hot_signer_native_send",
                    f"Sent {amount_native} {chain.display_name} native token to {to_address}",
                    metadata={
                        "chain": chain.key,
                        "to": to_address,
                        "amount_native": amount_native,
                        "tx_hash": tx_hash,
                    },
                )
            except Exception:
                logger.exception("Failed to record hot signer activity (send itself succeeded)")

        return NativeTransferResult(
            tx_hash=tx_hash,
            chain=chain.key,
            from_address=from_address,
            to_address=to_address,
            amount_native=amount_native,
            amount_wei=amount_wei,
        )

    async def send_native_batch(
        self,
        chain_key: str,
        from_addresses: list[str],
        to_addresses: list[str],
        amount_native: float,
        wallet_id: Optional[str] = None,
    ) -> BatchTransferResult:
        """
        Send `amount_native` across multiple (from, to) pairs -- see
        _pair_addresses() for the four shapes supported (1->N, N->1, 1->1,
        N->N paired). Legs run sequentially (never concurrently: two sends
        from the same from-address at once would race on the same nonce),
        and one leg failing does not stop the rest -- every pair gets a
        BatchLegResult either way.
        """
        pairs = _pair_addresses(from_addresses, to_addresses)
        legs: list[BatchLegResult] = []
        for from_addr, to_addr in pairs:
            try:
                result = await self.send_native(
                    chain_key, to_addr, amount_native, wallet_id=wallet_id, from_address=from_addr,
                )
                legs.append(BatchLegResult(from_addr, to_addr, ok=True, tx_hash=result.tx_hash))
            except HotSignerError as exc:
                legs.append(BatchLegResult(from_addr, to_addr, ok=False, error=str(exc)))
            except Exception as exc:  # noqa: BLE001 -- one bad leg must not kill the batch
                logger.exception("Unexpected error on batch leg %s -> %s", from_addr, to_addr)
                legs.append(BatchLegResult(from_addr, to_addr, ok=False, error=str(exc)))
        return BatchTransferResult(chain=chain_key, legs=legs)

    async def send_token_batch(
        self,
        chain_key: str,
        token_address: str,
        from_addresses: list[str],
        to_addresses: list[str],
        amount_tokens: float,
        decimals: Optional[int] = None,
        wallet_id: Optional[str] = None,
    ) -> BatchTransferResult:
        """ERC20 counterpart of send_native_batch -- same pairing rules, same
        sequential/one-bad-leg-does-not-stop-the-rest behavior."""
        pairs = _pair_addresses(from_addresses, to_addresses)
        legs: list[BatchLegResult] = []
        for from_addr, to_addr in pairs:
            try:
                result = await self.send_token(
                    chain_key, token_address, to_addr, amount_tokens,
                    decimals=decimals, wallet_id=wallet_id, from_address=from_addr,
                )
                legs.append(BatchLegResult(from_addr, to_addr, ok=True, tx_hash=result.tx_hash))
            except HotSignerError as exc:
                legs.append(BatchLegResult(from_addr, to_addr, ok=False, error=str(exc)))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected error on batch leg %s -> %s", from_addr, to_addr)
                legs.append(BatchLegResult(from_addr, to_addr, ok=False, error=str(exc)))
        return BatchTransferResult(chain=chain_key, legs=legs)

    async def send_token(
        self,
        chain_key: str,
        token_address: str,
        to_address: str,
        amount_tokens: float,
        decimals: Optional[int] = None,
        wallet_id: Optional[str] = None,
        from_address: Optional[str] = None,
    ) -> TokenTransferResult:
        """
        Send `amount_tokens` of an ERC20 token at `token_address` to
        `to_address` on `chain_key`. Same signing/broadcast path as
        send_native (direct RPC, no approval popup), just with a
        transfer(address,uint256) call instead of a plain value transfer.

        `decimals` is auto-read from the token contract (decimals()) if not
        given -- most tokens implement it, but a few non-standard ones
        don't, in which case pass it explicitly.

        Note: hot_signer_max_native_value only caps native-currency sends.
        There's no per-token USD cap here (no price oracle wired up), so
        double-check the amount and token address before sending -- this
        still has no human approval step.
        """
        private_key = _require_enabled(from_address)

        chain = chain_by_key(chain_key)
        rpc_candidates: list[str]
        if chain is not None:
            rpc_candidates = get_rpc_candidates(chain)
        else:
            chain, rpc_candidates = await resolve_chain(chain_key)
            if chain is None:
                raise HotSignerError(
                    f"Unsupported chain: {chain_key!r} "
                    "(not hardcoded and not found in the chain registry)"
                )

        if not rpc_candidates:
            raise HotSignerError(f"No usable RPC endpoint found for {chain.key!r}")

        if not token_address or not token_address.lower().startswith("0x") or len(token_address) != 42:
            raise HotSignerError(f"Invalid token contract address: {token_address!r}")

        if not to_address or not to_address.lower().startswith("0x") or len(to_address) != 42:
            raise HotSignerError(f"Invalid destination address: {to_address!r}")

        if amount_tokens <= 0:
            raise HotSignerError("Transfer amount must be positive")

        account = Account.from_key(private_key)
        from_address = account.address

        if decimals is None:
            decimals = await self._erc20_decimals(rpc_candidates, token_address)

        amount_raw = round(amount_tokens * (10 ** decimals))
        if amount_raw <= 0:
            raise HotSignerError(
                f"Amount {amount_tokens} rounds to 0 raw units at {decimals} decimals -- too small to send"
            )

        # Pre-flight balance check -- catches an obviously-doomed send (wrong
        # token, wrong chain, empty wallet) before burning gas on a revert.
        balance_hex = await self._rpc_call(
            rpc_candidates, "eth_call",
            [{"to": token_address, "data": _erc20_balance_of_calldata(from_address)}, "latest"],
        )
        try:
            token_balance_raw = int(balance_hex, 16)
        except (TypeError, ValueError):
            token_balance_raw = None
        if token_balance_raw is not None and token_balance_raw < amount_raw:
            have = token_balance_raw / (10 ** decimals)
            raise HotSignerError(
                f"Insufficient token balance: have {have}, need {amount_tokens} "
                f"(token {token_address} on {chain.key})"
            )

        calldata = _erc20_transfer_calldata(to_address, amount_raw)

        nonce = await self._rpc_call(
            rpc_candidates, "eth_getTransactionCount", [from_address, "pending"]
        )
        gas_price = _bump(int(await self._rpc_call(rpc_candidates, "eth_gasPrice", []), 16))
        gas_limit = _bump(await self._estimate_gas_with_fallback(
            rpc_candidates, from_address, token_address, calldata
        ))

        tx = {
            "chainId": chain.chain_id_int,
            "nonce": int(nonce, 16),
            "to": token_address,
            "value": 0,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "data": calldata,
        }

        signed = account.sign_transaction(tx)
        tx_hash = await self._rpc_call(
            rpc_candidates, "eth_sendRawTransaction", [signed.raw_transaction.hex()]
        )

        logger.info(
            "Hot signer sent ERC20 transfer: chain=%s token=%s to=%s amount=%s tx=%s",
            chain.key, token_address, to_address, amount_tokens, tx_hash,
        )

        if self._registry is not None:
            try:
                await self._registry.record_activity(
                    wallet_id or from_address,
                    "hot_signer_token_send",
                    f"Sent {amount_tokens} of token {token_address} to {to_address} on {chain.display_name}",
                    metadata={
                        "chain": chain.key,
                        "token_address": token_address,
                        "to": to_address,
                        "amount_tokens": amount_tokens,
                        "decimals": decimals,
                        "tx_hash": tx_hash,
                    },
                )
            except Exception:
                logger.exception("Failed to record hot signer activity (send itself succeeded)")

        return TokenTransferResult(
            tx_hash=tx_hash,
            chain=chain.key,
            token_address=token_address,
            from_address=from_address,
            to_address=to_address,
            amount_tokens=amount_tokens,
            amount_raw=amount_raw,
            decimals=decimals,
        )

    async def _erc20_decimals(self, rpc_candidates: list[str], token_address: str) -> int:
        result = await self._rpc_call(
            rpc_candidates, "eth_call",
            [{"to": token_address, "data": "0x" + _ERC20_DECIMALS_SELECTOR}, "latest"],
        )
        try:
            return int(result, 16)
        except (TypeError, ValueError) as exc:
            raise HotSignerError(
                f"Could not read decimals() from token {token_address!r} -- "
                "pass decimals explicitly if this isn't a standard ERC20"
            ) from exc

    async def _estimate_native_gas_with_fallback(
        self, rpc_candidates: list[str], from_address: str, to_address: str, value_wei: int,
    ) -> int:
        """eth_estimateGas for a plain native-currency transfer (no calldata).
        Floors at 21000 (the protocol minimum for a value transfer) and falls
        back to it if the node can't/won't estimate."""
        try:
            estimate_hex = await self._rpc_call(
                rpc_candidates, "eth_estimateGas",
                [{"from": from_address, "to": to_address, "value": hex(value_wei)}],
            )
            return max(int(estimate_hex, 16), 21000)
        except Exception:
            logger.warning("eth_estimateGas failed for native transfer, using protocol floor of 21000")
            return 21000

    async def _estimate_gas_with_fallback(
        self, rpc_candidates: list[str], from_address: str, to_address: str, calldata: str,
        fallback: int = 100_000,
    ) -> int:
        """eth_estimateGas with a generous safety margin, falling back to a
        flat default if the node can't/won't estimate (some public RPCs
        restrict or flake on this call)."""
        try:
            estimate_hex = await self._rpc_call(
                rpc_candidates, "eth_estimateGas",
                [{"from": from_address, "to": to_address, "data": calldata}],
            )
            estimate = int(estimate_hex, 16)
            return max(int(estimate * 1.2), estimate + 10_000)
        except Exception:
            logger.warning("eth_estimateGas failed for token transfer, using flat fallback of %d", fallback)
            return fallback

    @staticmethod
    async def _rpc_call(rpc_candidates: list[str], method: str, params: list) -> str:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            data = await rpc_post_with_fallback(rpc_candidates, payload)
        except Exception as exc:
            friendly = _friendly_insufficient_funds_message(exc)
            if friendly:
                raise HotSignerError(friendly) from exc
            raise HotSignerError(f"RPC error on {method} (tried {len(rpc_candidates)} endpoint(s)): {exc}") from exc
        return data["result"]
