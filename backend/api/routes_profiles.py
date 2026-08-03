from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.app_state import state
from backend.api.auth import require_auth
from backend.identity.detector import SUPPORTED_SERVICES
from backend.identity.registry import ProfileError, ProfileNotFoundError

router = APIRouter(prefix="/api/profiles", tags=["profiles"], dependencies=[Depends(require_auth)])


def _registry():
    if state.profile_registry is None:
        raise HTTPException(status_code=503, detail="Profile registry not initialized")
    return state.profile_registry


def _manager():
    if state.profiles is None:
        raise HTTPException(status_code=503, detail="Profile manager not initialized")
    return state.profiles


def _engine():
    # Mirrors routes_wallet.py's _engine(): the browser engine currently
    # driving a task, if any. Manual "check sessions now" needs a real page
    # to navigate a probe tab in; without an active task there's nothing to
    # check against, so that endpoint degrades gracefully instead of failing.
    return state.queue.current_engine if state.queue else None


# ---------------------------------------------------------------------- #
# Request/response models
# ---------------------------------------------------------------------- #

class CreateProfileRequest(BaseModel):
    name: str
    wallet_label: Optional[str] = None
    gmail_account: Optional[str] = None
    x_account: Optional[str] = None
    discord_account: Optional[str] = None
    extensions: list[str] = []
    notes: Optional[str] = None
    tags: list[str] = []


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    wallet_label: Optional[str] = None
    gmail_account: Optional[str] = None
    x_account: Optional[str] = None
    discord_account: Optional[str] = None
    extensions: Optional[list[str]] = None
    notes: Optional[str] = None
    tags: Optional[list[str]] = None
    status: Optional[str] = None


class CloneProfileRequest(BaseModel):
    new_name: str


class RenameProfileRequest(BaseModel):
    new_name: str


class ImportProfileRequest(BaseModel):
    name: str
    wallet_label: Optional[str] = None
    gmail_account: Optional[str] = None
    x_account: Optional[str] = None
    discord_account: Optional[str] = None
    extensions: list[str] = []
    notes: Optional[str] = None
    tags: list[str] = []


# ---------------------------------------------------------------------- #
# Profiles: CRUD
# ---------------------------------------------------------------------- #

@router.get("")
async def list_profiles(search: Optional[str] = None, tag: Optional[str] = None, enabled_only: bool = False):
    return await _registry().list_profiles(search=search, tag=tag, enabled_only=enabled_only)


@router.post("")
async def create_profile(req: CreateProfileRequest):
    try:
        return await _registry().create_profile(
            name=req.name,
            wallet_label=req.wallet_label,
            gmail_account=req.gmail_account,
            x_account=req.x_account,
            discord_account=req.discord_account,
            extensions=req.extensions,
            notes=req.notes,
            tags=req.tags,
        )
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/active")
async def get_active_profile():
    return await _registry().get_active_profile()


@router.get("/activity")
async def get_all_activity(limit: int = 50):
    return await _registry().get_activity(limit=limit)


@router.post("/import")
async def import_profile(req: ImportProfileRequest):
    try:
        return await _registry().import_profile(req.model_dump())
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{profile_id}")
async def get_profile(profile_id: str):
    try:
        profile = await _registry().get_profile(profile_id)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    return {
        "id": profile.id,
        "name": profile.name,
        "chrome_profile_dir": profile.chrome_profile_dir,
        "wallet_label": profile.wallet_label,
        "gmail_account": profile.gmail_account,
        "x_account": profile.x_account,
        "discord_account": profile.discord_account,
        "extensions": profile.extensions or [],
        "notes": profile.notes,
        "tags": profile.tags or [],
        "status": profile.status.value if hasattr(profile.status, "value") else profile.status,
        "enabled": profile.enabled,
        "is_active": profile.is_active,
        "sessions": {
            "gmail": profile.gmail_authenticated,
            "x": profile.x_authenticated,
            "discord": profile.discord_authenticated,
        },
        "last_session_check_at": profile.last_session_check_at.isoformat() if profile.last_session_check_at else None,
        "last_used_at": profile.last_used_at.isoformat() if profile.last_used_at else None,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
    }


@router.get("/{profile_id}/export")
async def export_profile(profile_id: str):
    try:
        return await _registry().export_profile(profile_id)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc


@router.patch("/{profile_id}")
async def update_profile(profile_id: str, req: UpdateProfileRequest):
    try:
        return await _registry().update_profile(profile_id, **req.model_dump(exclude_unset=True))
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{profile_id}/rename")
async def rename_profile(profile_id: str, req: RenameProfileRequest):
    try:
        return await _registry().rename_profile(profile_id, req.new_name)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{profile_id}")
async def delete_profile(profile_id: str):
    try:
        await _registry().delete_profile(profile_id)
        return {"ok": True}
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc


@router.post("/{profile_id}/clone")
async def clone_profile(profile_id: str, req: CloneProfileRequest):
    try:
        return await _registry().clone_profile(profile_id, req.new_name)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------- #
# Enable / Disable / Select active
# ---------------------------------------------------------------------- #

@router.post("/{profile_id}/enable")
async def enable_profile(profile_id: str):
    try:
        return await _registry().set_enabled(profile_id, True)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc


@router.post("/{profile_id}/disable")
async def disable_profile(profile_id: str):
    try:
        return await _registry().set_enabled(profile_id, False)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc


@router.post("/{profile_id}/select")
async def select_active_profile(profile_id: str):
    try:
        return await _registry().select_active_profile(profile_id)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------- #
# Sessions / filesystem / activity
# ---------------------------------------------------------------------- #

@router.get("/{profile_id}/sessions")
async def get_session_status(profile_id: str):
    """Last-known Gmail/X/Discord authentication status (from the most
    recent check -- either automatic, at the start of a task run against
    this profile, or manual via POST .../sessions/check)."""
    try:
        profile = await _registry().get_profile(profile_id)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    return {
        "gmail": profile.gmail_authenticated,
        "x": profile.x_authenticated,
        "discord": profile.discord_authenticated,
        "last_session_check_at": profile.last_session_check_at.isoformat() if profile.last_session_check_at else None,
    }


@router.post("/{profile_id}/sessions/check")
async def check_sessions_now(profile_id: str):
    """Manually re-run Gmail/X/Discord login detection. Requires a task to
    currently be running (so there's a live browser to check with) --
    otherwise returns 409, mirroring how routes_wallet.py's network-switch
    endpoints handle "no active browser session"."""
    engine = _engine()
    if engine is None:
        raise HTTPException(status_code=409, detail="No active browser session -- run a task with this profile first")
    try:
        loaded = await _manager().load_for_task(profile_id)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _manager().check_sessions(loaded, engine, notify_fn=None)


@router.get("/{profile_id}/filesystem")
async def inspect_filesystem(profile_id: str):
    """Read-only summary of what's on disk in this profile's Chrome
    directory -- cookies file presence/size, local/session storage
    presence, installed extension ids. Never reads the actual contents."""
    try:
        return await _registry().inspect_filesystem(profile_id)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc


@router.get("/{profile_id}/activity")
async def get_profile_activity(profile_id: str, limit: int = 50):
    return await _registry().get_activity(profile_id=profile_id, limit=limit)


@router.get("/meta/supported-services")
async def supported_services():
    return {"services": list(SUPPORTED_SERVICES)}
