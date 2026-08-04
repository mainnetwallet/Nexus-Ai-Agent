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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import set_key
from eth_account import Account
from eth_account.hdaccount import Mnemonic

from backend.config.settings import BASE_DIR, settings
from backend.wallet.chains import ChainInfo, chain_by_key

logger = logging.getLogger("nexus.wallet.hot_signer")

ENV_PATH = BASE_DIR / ".env"

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


def _require_enabled() -> str:
    if not settings.hot_signer_enabled:
        raise HotSignerDisabled(
            "Hot signer is disabled. Set HOT_SIGNER_ENABLED=true and HOT_SIGNER_PRIVATE_KEY "
            "in the environment to enable direct RPC sends (burner wallets only)."
        )
    key = settings.hot_signer_private_key.strip()
    if not key:
        raise HotSignerDisabled("HOT_SIGNER_PRIVATE_KEY is not set.")
    return key


def get_hot_signer_address() -> Optional[str]:
    """Returns the address of the configured hot signer, or None if disabled/unset."""
    key = settings.hot_signer_private_key.strip()
    if not key:
        return None
    try:
        return Account.from_key(key).address
    except Exception:
        return None


def persist_hot_signer_secret(
    private_key: Optional[str] = None,
    seed_phrase: Optional[str] = None,
    derivation_path: str = "m/44'/60'/0'/0/0",
) -> str:
    """
    Opt-in escape hatch, deliberately separate from
    backend/wallet/import_utils.py's derive-then-discard rule: takes a
    private key OR seed phrase, derives its address, and writes the raw
    private key hex into .env as HOT_SIGNER_PRIVATE_KEY (plus
    HOT_SIGNER_ENABLED=true), then updates the in-memory `settings` object
    so the hot signer is usable immediately, without a process restart.

    This function exists ONLY to back an explicit "save as hot signer"
    opt-in on the wallet-import flow (REST: ImportWalletRequest.
    save_as_hot_signer; chat: wallet_save_as_hot_signer). It must never be
    called implicitly on a plain import. Writing a plaintext key to a file
    on disk is exactly the tradeoff hot_signer.py's module docstring already
    describes -- burner/bot wallets only, never a wallet holding real value.

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
        ENV_PATH.touch(exist_ok=True)
        try:
            ENV_PATH.chmod(0o600)
        except OSError:
            # Best-effort on platforms/filesystems that don't support chmod.
            pass
        set_key(str(ENV_PATH), "HOT_SIGNER_PRIVATE_KEY", key_hex, quote_mode="never")
        set_key(str(ENV_PATH), "HOT_SIGNER_ENABLED", "true", quote_mode="never")

        # Update the live settings object so this takes effect immediately,
        # without waiting for a process restart to re-read .env.
        settings.hot_signer_private_key = key_hex
        settings.hot_signer_enabled = True
    except OSError as exc:
        raise HotSignerPersistError(f"Could not write to {ENV_PATH}: {exc}") from exc
    finally:
        del key_hex

    logger.info(
        "Hot signer secret persisted to .env (address=%s); key itself never logged.", address
    )
    return address


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
    ) -> NativeTransferResult:
        """
        Send `amount_native` of the chain's native currency to `to_address`
        on `chain_key` (e.g. "base", "ethereum", "polygon" -- see
        backend/wallet/chains.py for the supported set).
        """
        private_key = _require_enabled()

        chain = chain_by_key(chain_key)
        if chain is None:
            raise HotSignerError(f"Unsupported chain: {chain_key!r}")

        if not to_address or not to_address.lower().startswith("0x") or len(to_address) != 42:
            raise HotSignerError(f"Invalid destination address: {to_address!r}")

        if amount_native <= 0:
            raise HotSignerError("Transfer amount must be positive")

        cap = settings.hot_signer_max_native_value
        if cap and amount_native > cap:
            raise HotSignerError(
                f"Transfer of {amount_native} exceeds HOT_SIGNER_MAX_NATIVE_VALUE cap ({cap})"
            )

        rpc_url = settings.rpc_endpoints.get(chain.key)
        if not rpc_url:
            raise HotSignerError(f"No RPC endpoint configured for {chain.key!r}")

        account = Account.from_key(private_key)
        from_address = account.address
        amount_wei = round(amount_native * 1e18)

        async with httpx.AsyncClient(timeout=15.0) as client:
            nonce = await self._rpc_call(
                client, rpc_url, "eth_getTransactionCount", [from_address, "pending"]
            )
            gas_price = await self._rpc_call(client, rpc_url, "eth_gasPrice", [])

            tx = {
                "chainId": chain.chain_id_int,
                "nonce": int(nonce, 16),
                "to": to_address,
                "value": amount_wei,
                "gas": 21000,  # plain native transfer, no calldata
                "gasPrice": int(gas_price, 16),
            }

            signed = account.sign_transaction(tx)
            tx_hash = await self._rpc_call(
                client, rpc_url, "eth_sendRawTransaction", [signed.raw_transaction.hex()]
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

    @staticmethod
    async def _rpc_call(client: httpx.AsyncClient, rpc_url: str, method: str, params: list) -> str:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        resp = await client.post(rpc_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise HotSignerError(f"RPC error on {method}: {data['error']}")
        return data["result"]
