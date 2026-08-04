from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.app_state import state
from backend.api.auth import require_auth
from backend.config.settings import settings
from backend.wallet.hot_signer import (
    HotSignerDisabled,
    HotSignerError,
    HotSignerPersistError,
    get_hot_signer_address,
    persist_hot_signer_secret,
)
from backend.wallet.import_utils import WalletImportError
from backend.wallet.registry import WalletNotFoundError

router = APIRouter(prefix="/api/wallets", tags=["wallets"], dependencies=[Depends(require_auth)])


def _registry():
    if state.wallet_registry is None:
        raise HTTPException(status_code=503, detail="Wallet registry not initialized")
    return state.wallet_registry


def _engine():
    # The browser engine currently driving a task, if any. Several endpoints
    # (live status, network detect/switch, pending-request classification)
    # need a real page with a wallet extension injected; without an active
    # task there's nothing to read from, so those endpoints degrade
    # gracefully rather than failing outright.
    return state.queue.current_engine if state.queue else None


# ---------------------------------------------------------------------- #
# Request/response models
# ---------------------------------------------------------------------- #

class ImportWalletRequest(BaseModel):
    label: str
    method: str = Field(description="seed_phrase | private_key | browser_profile | address")
    address: Optional[str] = None
    private_key: Optional[str] = None
    seed_phrase: Optional[str] = None
    wallet_type: str = "metamask"
    network: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    notes: Optional[str] = None
    group_id: Optional[str] = None
    save_as_hot_signer: Optional[bool] = Field(
        default=None,
        description="Per-import override. If true and method is private_key/seed_phrase with a "
        "secret provided, the secret is ALSO persisted (encrypted) to the hot signer keystore "
        "(see backend/wallet/hot_signer.py) so Chat/REST can immediately send native transfers "
        "with no approval popup. If false, this import is never persisted even when "
        "HOT_SIGNER_AUTO_SAVE_ON_IMPORT is on. If omitted (null), falls back to the "
        "HOT_SIGNER_AUTO_SAVE_ON_IMPORT server setting. Burner/bot wallets only -- never a "
        "wallet holding real value.",
    )


class UpdateWalletRequest(BaseModel):
    label: Optional[str] = None
    network: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    group_id: Optional[str] = None
    wallet_type: Optional[str] = None


class CreateGroupRequest(BaseModel):
    name: str
    description: Optional[str] = None


class SwitchNetworkRequest(BaseModel):
    network: str


class SendNativeRequest(BaseModel):
    chain: str
    to_address: str
    amount: float
    wallet_id: Optional[str] = None


# Legacy request shape kept for backward compatibility with existing callers
# (registers metadata only -- no import method / no secret involved).
class RegisterWalletRequest(BaseModel):
    label: str
    address: str | None = None
    provider: str = "metamask"
    network: str | None = None


# ---------------------------------------------------------------------- #
# Wallets: CRUD + import
# ---------------------------------------------------------------------- #

@router.get("")
async def list_wallets(search: Optional[str] = None, group_id: Optional[str] = None, status: Optional[str] = None, tag: Optional[str] = None):
    return await _registry().list_wallets(search=search, group_id=group_id, status=status, tag=tag)


@router.post("")
async def register_wallet(req: RegisterWalletRequest):
    """
    Legacy metadata-only registration (address supplied directly by the
    caller). Prefer POST /api/wallets/import for the full import flow.
    """
    try:
        return await _registry().import_wallet(
            label=req.label, method="address", address=req.address, wallet_type=req.provider, network=req.network,
        )
    except WalletImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/import")
async def import_wallet(req: ImportWalletRequest):
    """
    Import a wallet by seed phrase, private key, browser profile, or address.
    Seed phrases/private keys are used only to derive the checksum address
    (backend/wallet/import_utils.py) and are never written to storage, a log,
    or the activity history -- UNLESS this import ends up persisted to the hot
    signer keystore, which happens when save_as_hot_signer=True is explicitly
    set on this call, OR (if save_as_hot_signer is omitted) when the server's
    HOT_SIGNER_AUTO_SAVE_ON_IMPORT setting is on. Passing save_as_hot_signer
    =False always skips persistence for this one import, regardless of the
    server setting.
    """
    try:
        result = await _registry().import_wallet(
            label=req.label,
            method=req.method,
            address=req.address,
            private_key=req.private_key,
            seed_phrase=req.seed_phrase,
            wallet_type=req.wallet_type,
            network=req.network,
            tags=req.tags,
            notes=req.notes,
            group_id=req.group_id,
        )
    except WalletImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    should_save_as_hot_signer = (
        req.save_as_hot_signer
        if req.save_as_hot_signer is not None
        else settings.hot_signer_auto_save_on_import
    )

    if should_save_as_hot_signer and req.method in ("private_key", "seed_phrase") and (req.private_key or req.seed_phrase):
        try:
            hot_signer_address = persist_hot_signer_secret(
                private_key=req.private_key, seed_phrase=req.seed_phrase
            )
        except HotSignerPersistError as exc:
            raise HTTPException(status_code=400, detail=f"Wallet imported, but hot signer setup failed: {exc}") from exc
        try:
            await _registry().record_activity(
                result.get("id") or hot_signer_address,
                "hot_signer_configured",
                f"Wallet '{req.label}' saved as hot signer ({hot_signer_address})",
                metadata={"address": hot_signer_address},
            )
        except Exception:
            pass  # best-effort audit log; hot signer setup itself already succeeded
        result["hot_signer_address"] = hot_signer_address

    return result


@router.get("/export")
async def export_wallets(ids: Optional[str] = None):
    wallet_ids = ids.split(",") if ids else None
    return await _registry().export_metadata(wallet_ids)


@router.get("/active")
async def get_active_wallet():
    return await _registry().get_active_wallet()


@router.get("/activity")
async def get_all_activity(limit: int = 50):
    return await _registry().get_activity(limit=limit)


@router.get("/groups")
async def list_groups():
    return await _registry().list_groups()


@router.post("/groups")
async def create_group(req: CreateGroupRequest):
    try:
        return await _registry().create_group(req.name, req.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/groups/{group_id}")
async def delete_group(group_id: str):
    try:
        await _registry().delete_group(group_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/hot-signer/status")
async def hot_signer_status():
    """Read-only: whether the hot signer is enabled and, if so, its derived
    address. Never returns the private key."""
    address = get_hot_signer_address()
    return {
        "enabled": settings.hot_signer_enabled and bool(address),
        "address": address,
        "max_native_value": settings.hot_signer_max_native_value or None,
    }


@router.post("/hot-signer/send")
async def hot_signer_send_native(req: SendNativeRequest):
    """
    Direct RPC native-token transfer -- bypasses the browser-extension
    approval flow entirely (see backend/wallet/hot_signer.py). Disabled by
    default; requires HOT_SIGNER_ENABLED + HOT_SIGNER_PRIVATE_KEY. Intended
    for burner/bot wallets only.
    """
    hot_signer = state.hot_signer
    if hot_signer is None:
        raise HTTPException(status_code=503, detail="Hot signer not initialized")
    try:
        result = await hot_signer.send_native(req.chain, req.to_address, req.amount, wallet_id=req.wallet_id)
    except HotSignerDisabled as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except HotSignerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "tx_hash": result.tx_hash,
        "chain": result.chain,
        "from_address": result.from_address,
        "to_address": result.to_address,
        "amount_native": result.amount_native,
    }


@router.get("/{wallet_id}")
async def get_wallet(wallet_id: str):
    try:
        wallet = await _registry().get_wallet(wallet_id)
    except WalletNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Wallet not found") from exc
    return {
        "id": wallet.id,
        "label": wallet.label,
        "address": wallet.address,
        "wallet_type": wallet.wallet_type,
        "network": wallet.network,
        "status": wallet.status.value if hasattr(wallet.status, "value") else wallet.status,
        "tags": wallet.tags or [],
        "notes": wallet.notes,
        "group_id": wallet.group_id,
        "is_active": wallet.is_active,
        "last_used_at": wallet.last_used_at.isoformat() if wallet.last_used_at else None,
        "created_at": wallet.created_at.isoformat() if wallet.created_at else None,
    }


@router.patch("/{wallet_id}")
async def update_wallet(wallet_id: str, req: UpdateWalletRequest):
    try:
        return await _registry().update_wallet(wallet_id, **req.model_dump(exclude_unset=True))
    except WalletNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Wallet not found") from exc


@router.delete("/{wallet_id}")
async def remove_wallet(wallet_id: str):
    try:
        await _registry().remove_wallet(wallet_id)
        return {"ok": True}
    except WalletNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Wallet not found") from exc


@router.post("/{wallet_id}/select")
async def select_active_wallet(wallet_id: str):
    """
    Marks this wallet as the one the Browser Engine should use for future
    tasks. Does not touch anything mid-task -- see the "no automatic
    switching" note on POST /api/browser tasks.
    """
    try:
        return await _registry().select_active_wallet(wallet_id)
    except WalletNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Wallet not found") from exc


@router.get("/{wallet_id}/status")
async def get_wallet_status(wallet_id: str):
    try:
        return await _registry().get_wallet_status(wallet_id, engine=_engine())
    except WalletNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Wallet not found") from exc


@router.get("/{wallet_id}/balance")
async def get_wallet_balance(wallet_id: str, network: Optional[str] = None):
    wallet = await _registry().get_wallet(wallet_id)
    if not wallet.address:
        raise HTTPException(status_code=400, detail="Wallet has no address on file")
    target_network = network or wallet.network
    if not target_network:
        raise HTTPException(status_code=400, detail="No network specified and wallet has none on file")
    if target_network == "all_evm":
        raise HTTPException(
            status_code=400,
            detail="This wallet is tagged 'all_evm' (works on every EVM chain, not tied to one) -- "
            "pass ?network=<chain> explicitly to check a balance, e.g. ?network=base.",
        )
    try:
        return await _registry().get_balance(wallet.address, target_network)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/{wallet_id}/activity")
async def get_wallet_activity(wallet_id: str, limit: int = 50):
    return await _registry().get_activity(wallet_id=wallet_id, limit=limit)


@router.get("/network/current")
async def get_connected_network():
    engine = _engine()
    if engine is None:
        return {"network": None, "reason": "no active browser session"}
    network = await _registry().detect_network(engine)
    return {"network": network}


@router.post("/{wallet_id}/network/switch")
async def switch_network(wallet_id: str, req: SwitchNetworkRequest):
    engine = _engine()
    if engine is None:
        raise HTTPException(status_code=409, detail="No active browser session to switch network on")
    result = await _registry().switch_network(engine, wallet_id, req.network)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "network switch failed"))
    return result


@router.get("/requests/pending")
async def get_pending_request():
    """
    Detects and classifies a currently-open wallet popup (connection request,
    transaction request, or signature request) without approving/rejecting
    it. Actual approve/reject flow stays in WalletManager.handle_pending_popup.
    """
    engine = _engine()
    if engine is None:
        return {"pending": False, "reason": "no active browser session"}
    return await _registry().classify_pending_request(engine)
