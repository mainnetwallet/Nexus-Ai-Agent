"""
WalletRegistry: the "Multi Wallet Manager" -- DB-backed metadata store and
read-only chain lookups for many user-owned wallets.

Scope boundary (see backend/wallet/import_utils.py for the full rationale):
this registry stores labels, addresses, groups, tags, notes, network, status,
and an activity log. It never stores a seed phrase or private key, encrypted
or otherwise. Signing/approving transactions is handled entirely by
WalletManager (backend/wallet/manager.py) against the user's own wallet
extension, with a human in the loop unless a narrow allowlist+cap policy
says otherwise. This registry only decides which wallet is "active" (i.e.
which label a task should reference) -- it never signs anything itself.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select

from backend.browser.engine import BrowserEngine
from backend.config.settings import settings
from backend.database.models import WalletActivity, WalletGroup, WalletRecord, WalletStatus
from backend.database.session import get_session
from backend.wallet.chains import chain_by_key, chain_from_hex
from backend.wallet.import_utils import (
    DerivedAddress,
    WalletImportError,
    derive_from_private_key,
    derive_from_seed_phrase,
)

logger = logging.getLogger("nexus.wallet.registry")


class WalletNotFoundError(LookupError):
    pass


def _wallet_to_dict(w: WalletRecord) -> dict[str, Any]:
    return {
        "id": w.id,
        "label": w.label,
        "address": w.address,
        "wallet_type": w.wallet_type,
        "network": w.network,
        "status": w.status.value if isinstance(w.status, WalletStatus) else w.status,
        "tags": w.tags or [],
        "notes": w.notes,
        "group_id": w.group_id,
        "is_active": w.is_active,
        "enabled": w.enabled if w.enabled is not None else True,
        "last_used_at": w.last_used_at.isoformat() if w.last_used_at else None,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


class WalletRegistry:
    # ------------------------------------------------------------------ #
    # Import / CRUD
    # ------------------------------------------------------------------ #
    async def import_wallet(
        self,
        label: str,
        method: str,
        *,
        address: Optional[str] = None,
        private_key: Optional[str] = None,
        seed_phrase: Optional[str] = None,
        wallet_type: str = "metamask",
        network: Optional[str] = None,
        tags: Optional[list[str]] = None,
        notes: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        method: "seed_phrase" | "private_key" | "browser_profile" | "address"

        For "seed_phrase"/"private_key", the secret is used ONCE in-memory to
        derive the address (backend/wallet/import_utils.py) and is never
        stored. For "browser_profile"/"address", the caller (or the browser
        engine, via get_injected_wallet_state) supplies the address directly
        -- there is no secret to handle at all.
        """
        resolved_address = address
        if method == "private_key":
            if not private_key:
                raise WalletImportError("private_key is required for method=private_key")
            derived: DerivedAddress = derive_from_private_key(private_key)
            resolved_address = derived.address
        elif method == "seed_phrase":
            if not seed_phrase:
                raise WalletImportError("seed_phrase is required for method=seed_phrase")
            derived = derive_from_seed_phrase(seed_phrase)
            resolved_address = derived.address
        elif method in ("browser_profile", "address"):
            if not resolved_address:
                raise WalletImportError(
                    "address is required when importing from an existing browser profile "
                    "(read it from the wallet extension, e.g. via BrowserEngine.get_injected_wallet_state)."
                )
        else:
            raise WalletImportError(f"Unknown import method: {method!r}")

        async with get_session() as session:
            existing = await session.scalar(select(WalletRecord).where(WalletRecord.label == label))
            if existing:
                raise WalletImportError(f"A wallet labeled {label!r} already exists.")

            wallet = WalletRecord(
                label=label,
                address=resolved_address,
                wallet_type=wallet_type,
                provider=wallet_type,
                network=network,
                status=WalletStatus.UNKNOWN,
                tags=tags or [],
                notes=notes,
                group_id=group_id,
            )
            session.add(wallet)
            await session.flush()
            session.add(
                WalletActivity(
                    wallet_id=wallet.id,
                    event_type="imported",
                    description=f"Wallet imported via {method}",
                    metadata_json={"method": method},
                )
            )
            await session.flush()
            result = _wallet_to_dict(wallet)

        logger.info("Wallet imported: label=%s method=%s address=%s", label, method, resolved_address)
        return result

    async def list_wallets(
        self,
        *,
        search: Optional[str] = None,
        group_id: Optional[str] = None,
        status: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        async with get_session() as session:
            stmt = select(WalletRecord)
            if group_id:
                stmt = stmt.where(WalletRecord.group_id == group_id)
            if status:
                stmt = stmt.where(WalletRecord.status == status)
            result = await session.execute(stmt)
            wallets = list(result.scalars().all())

        if search:
            needle = search.lower()
            wallets = [
                w for w in wallets
                if needle in (w.label or "").lower() or needle in (w.address or "").lower()
            ]
        if tag:
            wallets = [w for w in wallets if tag in (w.tags or [])]

        return [_wallet_to_dict(w) for w in wallets]

    async def get_wallet(self, wallet_id: str) -> WalletRecord:
        async with get_session() as session:
            wallet = await session.get(WalletRecord, wallet_id)
            if not wallet:
                raise WalletNotFoundError(wallet_id)
            # Detach-safe snapshot fields already loaded; return the ORM
            # object's dict form to the caller layer instead of the live
            # (session-bound) instance.
            return wallet

    async def update_wallet(self, wallet_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"label", "network", "tags", "notes", "status", "group_id", "wallet_type", "enabled"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "status" in updates and not isinstance(updates["status"], WalletStatus):
            try:
                updates["status"] = WalletStatus(updates["status"])
            except ValueError:
                raise WalletImportError(f"Invalid status: {updates['status']!r}")
        async with get_session() as session:
            wallet = await session.get(WalletRecord, wallet_id)
            if not wallet:
                raise WalletNotFoundError(wallet_id)
            for k, v in updates.items():
                setattr(wallet, k, v)
            await session.flush()
            session.add(
                WalletActivity(
                    wallet_id=wallet.id,
                    event_type="metadata_updated",
                    description=f"Updated fields: {', '.join(updates.keys()) or '(none)'}",
                    metadata_json=updates,
                )
            )
            await session.flush()
            return _wallet_to_dict(wallet)

    async def remove_wallet(self, wallet_id: str) -> None:
        async with get_session() as session:
            wallet = await session.get(WalletRecord, wallet_id)
            if not wallet:
                raise WalletNotFoundError(wallet_id)
            await session.delete(wallet)
        logger.info("Wallet removed: id=%s", wallet_id)

    async def export_metadata(self, wallet_ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """
        Metadata-only export (label/address/type/network/tags/notes/status).
        There is nothing sensitive to redact -- no secret is ever stored.
        """
        wallets = await self.list_wallets()
        if wallet_ids:
            wallets = [w for w in wallets if w["id"] in wallet_ids]
        return wallets

    # ------------------------------------------------------------------ #
    # Groups
    # ------------------------------------------------------------------ #
    async def create_group(self, name: str, description: Optional[str] = None) -> dict[str, Any]:
        async with get_session() as session:
            existing = await session.scalar(select(WalletGroup).where(WalletGroup.name == name))
            if existing:
                raise ValueError(f"A group named {name!r} already exists.")
            group = WalletGroup(name=name, description=description)
            session.add(group)
            await session.flush()
            return {"id": group.id, "name": group.name, "description": group.description}

    async def list_groups(self) -> list[dict[str, Any]]:
        async with get_session() as session:
            result = await session.execute(select(WalletGroup))
            return [
                {"id": g.id, "name": g.name, "description": g.description}
                for g in result.scalars().all()
            ]

    async def delete_group(self, group_id: str) -> None:
        async with get_session() as session:
            group = await session.get(WalletGroup, group_id)
            if not group:
                raise ValueError(f"Group not found: {group_id}")
            # Un-assign member wallets rather than cascading deletes.
            result = await session.execute(select(WalletRecord).where(WalletRecord.group_id == group_id))
            for wallet in result.scalars().all():
                wallet.group_id = None
            await session.delete(group)

    # ------------------------------------------------------------------ #
    # Active wallet selection
    # ------------------------------------------------------------------ #
    async def select_active_wallet(self, wallet_id: str) -> dict[str, Any]:
        async with get_session() as session:
            target = await session.get(WalletRecord, wallet_id)
            if not target:
                raise WalletNotFoundError(wallet_id)

            result = await session.execute(select(WalletRecord).where(WalletRecord.is_active.is_(True)))
            for w in result.scalars().all():
                if w.id != wallet_id:
                    w.is_active = False

            target.is_active = True
            target.last_used_at = datetime.now(timezone.utc)
            await session.flush()
            session.add(
                WalletActivity(
                    wallet_id=target.id,
                    event_type="selected",
                    description=f"Wallet {target.label!r} set as active",
                )
            )
            await session.flush()
            return _wallet_to_dict(target)

    async def set_wallet_enabled(self, wallet_id: str, enabled: bool) -> dict[str, Any]:
        """
        Independent per-wallet on/off toggle. Unlike select_active_wallet,
        this never touches any other wallet -- any number of wallets can be
        enabled at the same time. Every wallet starts enabled on import.
        """
        async with get_session() as session:
            target = await session.get(WalletRecord, wallet_id)
            if not target:
                raise WalletNotFoundError(wallet_id)
            target.enabled = enabled
            await session.flush()
            session.add(
                WalletActivity(
                    wallet_id=target.id,
                    event_type="enabled" if enabled else "disabled",
                    description=f"Wallet {target.label!r} {'enabled' if enabled else 'disabled'}",
                )
            )
            await session.flush()
            return _wallet_to_dict(target)

    async def get_active_wallet(self) -> Optional[dict[str, Any]]:
        async with get_session() as session:
            wallet = await session.scalar(select(WalletRecord).where(WalletRecord.is_active.is_(True)))
            return _wallet_to_dict(wallet) if wallet else None

    # ------------------------------------------------------------------ #
    # Live status: connection / lock / network (via the browser engine)
    # ------------------------------------------------------------------ #
    async def get_wallet_status(self, wallet_id: str, engine: Optional[BrowserEngine] = None) -> dict[str, Any]:
        wallet = await self.get_wallet(wallet_id)
        status: dict[str, Any] = _wallet_to_dict(wallet)
        status["live"] = {"connected": None, "locked": None, "network": wallet.network}

        if engine is None:
            return status

        provider_state = await engine.get_injected_wallet_state()
        if not provider_state.get("present"):
            status["live"] = {"connected": False, "locked": None, "network": None, "reason": "no injected provider on this page"}
            return status

        connected = bool(provider_state.get("isConnected")) and bool(provider_state.get("selectedAddress"))
        # A present-but-not-connected provider with no selected address
        # usually means the extension is locked (or simply not connected to
        # this site) -- we can't fully distinguish those from the dApp side,
        # so we report both possibilities rather than guessing.
        locked_or_disconnected = provider_state.get("present") and not provider_state.get("selectedAddress")
        chain = chain_from_hex(provider_state.get("chainId"))

        status["live"] = {
            "connected": connected,
            "locked_or_disconnected": locked_or_disconnected,
            "network": chain.key if chain else provider_state.get("chainId"),
            "selected_address": provider_state.get("selectedAddress"),
        }

        new_status = WalletStatus.ACTIVE if connected else (WalletStatus.LOCKED if locked_or_disconnected else WalletStatus.INACTIVE)
        if new_status.value != wallet.status:
            await self.update_wallet(wallet_id, status=new_status.value)
            status["status"] = new_status.value

        return status

    async def detect_network(self, engine: BrowserEngine) -> Optional[str]:
        provider_state = await engine.get_injected_wallet_state()
        chain = chain_from_hex(provider_state.get("chainId"))
        return chain.key if chain else provider_state.get("chainId")

    async def switch_network(self, engine: BrowserEngine, wallet_id: str, target_network: str) -> dict[str, Any]:
        """
        Requests a network switch through the standard EIP-3326
        `wallet_switchEthereumChain` call on the injected provider -- this is
        the same request any dApp "Switch Network" button makes. MetaMask/
        Rabby will show their own confirmation UI; we don't bypass that.
        """
        chain = chain_by_key(target_network)
        if not chain:
            raise ValueError(f"Unsupported network: {target_network!r}")

        js = f"""
        async () => {{
            if (!window.ethereum) return {{ ok: false, error: 'no injected provider' }};
            try {{
                await window.ethereum.request({{
                    method: 'wallet_switchEthereumChain',
                    params: [{{ chainId: '{chain.chain_id_hex}' }}],
                }});
                return {{ ok: true }};
            }} catch (err) {{
                return {{ ok: false, error: String(err && err.message || err) }};
            }}
        }}
        """
        result = await engine.eval_js(js, default={"ok": False, "error": "eval failed"})
        if result.get("ok"):
            await self.update_wallet(wallet_id, network=chain.key)
            await self.record_activity(wallet_id, "network_switched", f"Switched to {chain.display_name}")
        else:
            await self.record_activity(
                wallet_id, "network_switch_failed", f"Failed to switch to {chain.display_name}: {result.get('error')}"
            )
        return result

    # ------------------------------------------------------------------ #
    # Balance (read-only RPC, no wallet/browser involvement required)
    # ------------------------------------------------------------------ #
    async def get_balance(self, address: str, network: str) -> dict[str, Any]:
        rpc_url = settings.rpc_endpoints.get(network.lower())
        if not rpc_url:
            raise ValueError(f"No RPC endpoint configured for network {network!r}")

        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(rpc_url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")

        wei = int(data["result"], 16)
        return {"address": address, "network": network, "wei": wei, "native": wei / 1e18}

    # ------------------------------------------------------------------ #
    # Popup / transaction / signature request detection
    # ------------------------------------------------------------------ #
    async def classify_pending_request(self, engine: BrowserEngine) -> dict[str, Any]:
        """
        Best-effort classification of a currently-open wallet popup as a
        connection request, a transaction request, or a signature request,
        purely from the popup's visible text. Does not click anything --
        approval/rejection stays with WalletManager.handle_pending_popup and
        its human-in-the-loop / allowlist policy.
        """
        popup_id = await engine.detect_popup_or_dialog(timeout_ms=500)
        if not popup_id:
            return {"pending": False}

        engine.switch_tab(popup_id)
        text = await engine.extract_visible_text(max_chars=2000)
        lowered = text.lower()

        if any(k in lowered for k in ("signature request", "sign this message", "sign message")):
            kind = "signature"
        elif any(k in lowered for k in ("confirm transaction", "transaction request", "gas fee", "estimated fee")):
            kind = "transaction"
        elif any(k in lowered for k in ("connect", "connect with", "select an account")):
            kind = "connection"
        else:
            kind = "unknown"

        return {"pending": True, "type": kind, "popup_id": popup_id, "snippet": text[:300]}

    # ------------------------------------------------------------------ #
    # Activity history
    # ------------------------------------------------------------------ #
    async def record_activity(self, wallet_id: str, event_type: str, description: str, metadata: Optional[dict] = None) -> None:
        async with get_session() as session:
            session.add(
                WalletActivity(
                    wallet_id=wallet_id,
                    event_type=event_type,
                    description=description,
                    metadata_json=metadata or {},
                )
            )

    async def get_activity(self, wallet_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        async with get_session() as session:
            stmt = select(WalletActivity).order_by(WalletActivity.created_at.desc()).limit(limit)
            if wallet_id:
                stmt = stmt.where(WalletActivity.wallet_id == wallet_id)
            result = await session.execute(stmt)
            return [
                {
                    "id": a.id,
                    "wallet_id": a.wallet_id,
                    "event_type": a.event_type,
                    "description": a.description,
                    "metadata": a.metadata_json,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in result.scalars().all()
            ]
