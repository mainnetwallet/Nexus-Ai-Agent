"""
ProfileRegistry: the Identity & Profile Manager's DB-backed metadata store.
Mirrors backend/wallet/registry.py's WalletRegistry shape deliberately --
same CRUD/activity-log/select-active conventions -- so the two feel like
one family of "manager" modules rather than a new pattern.

Scope boundary (same as WalletRegistry): this registry stores names,
Chrome profile directory paths, linked account labels (wallet/Gmail/X/
Discord), tags, notes, and status. It never stores a password, seed
phrase, or private key. Cookies/local storage/session storage/extensions
live on disk in the Chrome profile directory itself (backend/identity/
fs.py) -- this registry only tracks *where* that directory is and the
last-known authentication status per service.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

from backend.database.models import ProfileActivity, ProfileRecord, ProfileStatus
from backend.database.session import get_session
from backend.identity.fs import ProfileFilesystem

logger = logging.getLogger("nexus.identity.registry")


class ProfileNotFoundError(LookupError):
    pass


class ProfileError(ValueError):
    pass


def _profile_to_dict(p: ProfileRecord) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "chrome_profile_dir": p.chrome_profile_dir,
        "wallet_label": p.wallet_label,
        "gmail_account": p.gmail_account,
        "x_account": p.x_account,
        "discord_account": p.discord_account,
        "extensions": p.extensions or [],
        "notes": p.notes,
        "tags": p.tags or [],
        "status": p.status.value if isinstance(p.status, ProfileStatus) else p.status,
        "enabled": p.enabled,
        "is_active": p.is_active,
        "sessions": {
            "gmail": p.gmail_authenticated,
            "x": p.x_authenticated,
            "discord": p.discord_authenticated,
        },
        "last_session_check_at": p.last_session_check_at.isoformat() if p.last_session_check_at else None,
        "last_used_at": p.last_used_at.isoformat() if p.last_used_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


class ProfileRegistry:
    def __init__(self, data_dir: Path) -> None:
        self.fs = ProfileFilesystem(Path(data_dir) / "browser_profiles")

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    async def create_profile(
        self,
        name: str,
        *,
        wallet_label: Optional[str] = None,
        gmail_account: Optional[str] = None,
        x_account: Optional[str] = None,
        discord_account: Optional[str] = None,
        extensions: Optional[list[str]] = None,
        notes: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        async with get_session() as session:
            existing = await session.scalar(select(ProfileRecord).where(ProfileRecord.name == name))
            if existing:
                raise ProfileError(f"A profile named {name!r} already exists.")

            profile = ProfileRecord(
                name=name,
                chrome_profile_dir="",  # filled in below once we have the id
                wallet_label=wallet_label,
                gmail_account=gmail_account,
                x_account=x_account,
                discord_account=discord_account,
                extensions=extensions or [],
                notes=notes,
                tags=tags or [],
            )
            session.add(profile)
            await session.flush()
            profile.chrome_profile_dir = self.fs.create(profile.id)
            await session.flush()
            session.add(ProfileActivity(profile_id=profile.id, event_type="created", description=f"Profile {name!r} created"))
            await session.flush()
            result = _profile_to_dict(profile)

        logger.info("Profile created: name=%s id=%s", name, result["id"])
        return result

    async def list_profiles(
        self, *, search: Optional[str] = None, tag: Optional[str] = None, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        async with get_session() as session:
            stmt = select(ProfileRecord)
            if enabled_only:
                stmt = stmt.where(ProfileRecord.enabled.is_(True))
            result = await session.execute(stmt)
            profiles = list(result.scalars().all())

        if search:
            needle = search.lower()
            profiles = [
                p for p in profiles
                if needle in p.name.lower()
                or needle in (p.gmail_account or "").lower()
                or needle in (p.x_account or "").lower()
                or needle in (p.discord_account or "").lower()
            ]
        if tag:
            profiles = [p for p in profiles if tag in (p.tags or [])]

        return [_profile_to_dict(p) for p in profiles]

    async def get_profile(self, profile_id: str) -> ProfileRecord:
        async with get_session() as session:
            profile = await session.get(ProfileRecord, profile_id)
            if not profile:
                raise ProfileNotFoundError(profile_id)
            return profile

    async def get_by_name(self, name: str) -> Optional[ProfileRecord]:
        """Case-insensitive lookup so "Run this task with Profile-01" and
        "profile-01" resolve the same way from chat/Telegram free text."""
        async with get_session() as session:
            result = await session.execute(select(ProfileRecord))
            for profile in result.scalars().all():
                if profile.name.lower() == name.lower():
                    return profile
            return None

    async def resolve(self, profile_ref: str) -> Optional[ProfileRecord]:
        """Accepts either a profile id or a profile name."""
        async with get_session() as session:
            profile = await session.get(ProfileRecord, profile_ref)
            if profile:
                return profile
        return await self.get_by_name(profile_ref)

    async def update_profile(self, profile_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "name", "wallet_label", "gmail_account", "x_account", "discord_account",
            "extensions", "notes", "tags", "status",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "status" in updates and not isinstance(updates["status"], ProfileStatus):
            try:
                updates["status"] = ProfileStatus(updates["status"])
            except ValueError:
                raise ProfileError(f"Invalid status: {updates['status']!r}")

        async with get_session() as session:
            profile = await session.get(ProfileRecord, profile_id)
            if not profile:
                raise ProfileNotFoundError(profile_id)
            if "name" in updates and updates["name"] != profile.name:
                clash = await session.scalar(select(ProfileRecord).where(ProfileRecord.name == updates["name"]))
                if clash:
                    raise ProfileError(f"A profile named {updates['name']!r} already exists.")
            for k, v in updates.items():
                setattr(profile, k, v)
            await session.flush()
            session.add(
                ProfileActivity(
                    profile_id=profile.id,
                    event_type="updated",
                    description=f"Updated fields: {', '.join(updates.keys()) or '(none)'}",
                    metadata_json={k: v for k, v in updates.items() if k != "status"},
                )
            )
            await session.flush()
            return _profile_to_dict(profile)

    async def rename_profile(self, profile_id: str, new_name: str) -> dict[str, Any]:
        return await self.update_profile(profile_id, name=new_name)

    async def delete_profile(self, profile_id: str) -> None:
        async with get_session() as session:
            profile = await session.get(ProfileRecord, profile_id)
            if not profile:
                raise ProfileNotFoundError(profile_id)
            await session.delete(profile)
        self.fs.delete(profile_id)
        logger.info("Profile deleted: id=%s", profile_id)

    # ------------------------------------------------------------------ #
    # Clone / Import / Export
    # ------------------------------------------------------------------ #
    async def clone_profile(self, profile_id: str, new_name: str) -> dict[str, Any]:
        """
        Copies both the metadata row and the full Chrome profile directory
        (cookies/local storage/session storage/extensions included) so the
        clone starts already authenticated, exactly like the source.
        """
        async with get_session() as session:
            source = await session.get(ProfileRecord, profile_id)
            if not source:
                raise ProfileNotFoundError(profile_id)
            clash = await session.scalar(select(ProfileRecord).where(ProfileRecord.name == new_name))
            if clash:
                raise ProfileError(f"A profile named {new_name!r} already exists.")

            clone = ProfileRecord(
                name=new_name,
                chrome_profile_dir="",
                wallet_label=source.wallet_label,
                gmail_account=source.gmail_account,
                x_account=source.x_account,
                discord_account=source.discord_account,
                extensions=list(source.extensions or []),
                notes=source.notes,
                tags=list(source.tags or []),
                gmail_authenticated=source.gmail_authenticated,
                x_authenticated=source.x_authenticated,
                discord_authenticated=source.discord_authenticated,
            )
            session.add(clone)
            await session.flush()
            clone.chrome_profile_dir = self.fs.clone(source.id, clone.id)
            await session.flush()
            session.add(
                ProfileActivity(
                    profile_id=clone.id,
                    event_type="cloned",
                    description=f"Cloned from {source.name!r}",
                    metadata_json={"source_profile_id": source.id},
                )
            )
            await session.flush()
            result = _profile_to_dict(clone)

        logger.info("Profile cloned: source=%s -> new=%s", profile_id, new_name)
        return result

    async def export_profile(self, profile_id: str) -> dict[str, Any]:
        """Metadata-only export -- no cookies/local storage/session storage/
        extension binaries, no credentials. Just enough to recreate the row
        (and, separately, hand the operator the on-disk directory path)."""
        profile = await self.get_profile(profile_id)
        data = _profile_to_dict(profile)
        data.pop("id", None)
        data.pop("chrome_profile_dir", None)
        data.pop("is_active", None)
        return data

    async def import_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        return await self.create_profile(
            name=data["name"],
            wallet_label=data.get("wallet_label"),
            gmail_account=data.get("gmail_account"),
            x_account=data.get("x_account"),
            discord_account=data.get("discord_account"),
            extensions=data.get("extensions"),
            notes=data.get("notes"),
            tags=data.get("tags"),
        )

    # ------------------------------------------------------------------ #
    # Enable / Disable / Select active
    # ------------------------------------------------------------------ #
    async def set_enabled(self, profile_id: str, enabled: bool) -> dict[str, Any]:
        async with get_session() as session:
            profile = await session.get(ProfileRecord, profile_id)
            if not profile:
                raise ProfileNotFoundError(profile_id)
            profile.enabled = enabled
            profile.status = ProfileStatus.DISABLED if not enabled else ProfileStatus.READY
            await session.flush()
            session.add(
                ProfileActivity(
                    profile_id=profile.id,
                    event_type="enabled" if enabled else "disabled",
                    description=f"Profile {profile.name!r} {'enabled' if enabled else 'disabled'}",
                )
            )
            await session.flush()
            return _profile_to_dict(profile)

    async def select_active_profile(self, profile_id: str) -> dict[str, Any]:
        async with get_session() as session:
            target = await session.get(ProfileRecord, profile_id)
            if not target:
                raise ProfileNotFoundError(profile_id)
            if not target.enabled:
                raise ProfileError(f"Profile {target.name!r} is disabled -- enable it first.")

            result = await session.execute(select(ProfileRecord).where(ProfileRecord.is_active.is_(True)))
            for p in result.scalars().all():
                if p.id != profile_id:
                    p.is_active = False

            target.is_active = True
            target.last_used_at = datetime.now(timezone.utc)
            await session.flush()
            session.add(
                ProfileActivity(profile_id=target.id, event_type="selected", description=f"Profile {target.name!r} set as active")
            )
            await session.flush()
            return _profile_to_dict(target)

    async def get_active_profile(self) -> Optional[dict[str, Any]]:
        async with get_session() as session:
            profile = await session.scalar(select(ProfileRecord).where(ProfileRecord.is_active.is_(True)))
            return _profile_to_dict(profile) if profile else None

    # ------------------------------------------------------------------ #
    # Session status (written by ProfileManager after detection runs)
    # ------------------------------------------------------------------ #
    async def record_session_check(self, profile_id: str, results: dict[str, Optional[bool]]) -> dict[str, Any]:
        async with get_session() as session:
            profile = await session.get(ProfileRecord, profile_id)
            if not profile:
                raise ProfileNotFoundError(profile_id)
            if "gmail" in results:
                profile.gmail_authenticated = results["gmail"]
            if "x" in results:
                profile.x_authenticated = results["x"]
            if "discord" in results:
                profile.discord_authenticated = results["discord"]
            profile.last_session_check_at = datetime.now(timezone.utc)

            checked_values = [v for v in results.values() if v is not None]
            if checked_values and not all(checked_values):
                profile.status = ProfileStatus.NEEDS_LOGIN
            elif profile.enabled:
                profile.status = ProfileStatus.READY
            await session.flush()
            session.add(
                ProfileActivity(
                    profile_id=profile.id,
                    event_type="session_checked",
                    description=f"Session check: {results}",
                    metadata_json={k: v for k, v in results.items()},
                )
            )
            await session.flush()
            return _profile_to_dict(profile)

    async def inspect_filesystem(self, profile_id: str) -> dict[str, Any]:
        profile = await self.get_profile(profile_id)
        return self.fs.inspect(profile.chrome_profile_dir)

    # ------------------------------------------------------------------ #
    # Activity history
    # ------------------------------------------------------------------ #
    async def get_activity(self, profile_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        async with get_session() as session:
            stmt = select(ProfileActivity).order_by(ProfileActivity.created_at.desc()).limit(limit)
            if profile_id:
                stmt = stmt.where(ProfileActivity.profile_id == profile_id)
            result = await session.execute(stmt)
            return [
                {
                    "id": a.id,
                    "profile_id": a.profile_id,
                    "event_type": a.event_type,
                    "description": a.description,
                    "metadata": a.metadata_json,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in result.scalars().all()
            ]
