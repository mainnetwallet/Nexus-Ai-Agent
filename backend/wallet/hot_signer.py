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
from typing import Any, Optional

import httpx
from eth_account import Account

from backend.config.settings import settings
from backend.wallet.chains import ChainInfo, chain_by_key

logger = logging.getLogger("nexus.wallet.hot_signer")


class HotSignerError(Exception):
    pass


class HotSignerDisabled(HotSignerError):
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
