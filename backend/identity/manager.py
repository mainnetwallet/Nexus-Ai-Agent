"""
ProfileManager -- the single facade TaskQueueService (and Chat/Telegram, via
it) calls at task start to implement the mission's load sequence:

    1. Load the selected profile.
    2. Load the associated wallet.        (resolves wallet_label; actual
                                             wallet interaction is unchanged,
                                             handled by WalletManager as before)
    3. Load Chrome profile.               (chrome_profile_dir -> BrowserEngine's
                                             user_data_dir, so Playwright launches
                                             *this* persistent profile)
    4-6. Detect Gmail/X/Discord login.    (backend/identity/detector.py, only for
                                             services this profile has an account on file for)
    7. Reuse all existing authenticated sessions. (the persistent Chrome profile
                                             dir already carries cookies/local
                                             storage/session storage/extensions --
                                             nothing further to do)
    8. If a service is not authenticated, notify the user instead of assuming
       credentials.                        (notify_fn, never touches a login form)

Composes ProfileRegistry + SessionDetector -- it does not reimplement either,
matching how MCPManager (backend/mcp/manager.py) composes MCPRegistry/
MCPToolDiscovery/MCPToolRouter instead of owning their logic itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from backend.browser.engine import BrowserEngine
from backend.identity.detector import SessionDetector
from backend.identity.registry import ProfileBusyError, ProfileNotFoundError, ProfileRegistry

logger = logging.getLogger("nexus.identity.manager")

NotifyFn = Callable[[str], Awaitable[None]]


@dataclass
class LoadedProfile:
    id: str
    name: str
    chrome_profile_dir: str
    wallet_label: Optional[str]
    configured_services: dict[str, Optional[str]]  # service -> account label, only if set


class ProfileManager:
    def __init__(self, registry: ProfileRegistry, detector: Optional[SessionDetector] = None) -> None:
        self.registry = registry
        self.detector = detector or SessionDetector()

    # ------------------------------------------------------------------ #
    # Steps 1-3: resolve profile -> wallet label + Chrome profile dir
    # ------------------------------------------------------------------ #
    async def load_for_task(self, profile_ref: str) -> LoadedProfile:
        """Steps 1-3 of the mission's load sequence. Raises ProfileNotFoundError
        if profile_ref doesn't match any profile by id or name (case-insensitive),
        ProfileError (via the registry) if it's disabled, and ProfileBusyError
        if another concurrently-running task already has this exact profile
        loaded (each Chrome profile directory can only be driven by one live
        browser at a time -- different profiles are free to run at once, this
        just prevents the same one from double-booking)."""
        profile = await self.registry.resolve(profile_ref)
        if profile is None:
            raise ProfileNotFoundError(profile_ref)
        if not profile.enabled:
            from backend.identity.registry import ProfileError

            raise ProfileError(f"Profile {profile.name!r} is disabled -- enable it before running a task with it.")

        acquired = await self.registry.try_lock(profile.id)
        if not acquired:
            raise ProfileBusyError(
                f"Profile {profile.name!r} is currently in use by another running task."
            )
        return LoadedProfile(
            id=profile.id,
            name=profile.name,
            chrome_profile_dir=profile.chrome_profile_dir,
            wallet_label=profile.wallet_label,
            configured_services={
                "gmail": profile.gmail_account,
                "x": profile.x_account,
                "discord": profile.discord_account,
            },
        )

    # ------------------------------------------------------------------ #
    # Steps 4-8: detect + reuse + notify
    # ------------------------------------------------------------------ #
    async def check_sessions(
        self,
        loaded: LoadedProfile,
        engine: BrowserEngine,
        notify_fn: Optional[NotifyFn] = None,
    ) -> dict[str, Any]:
        """
        Runs Gmail/X/Discord login detection only for the services this
        profile has an account configured for (an unconfigured service is
        simply skipped -- not every identity needs all three). Records the
        result on the profile row and, for any configured-but-unauthenticated
        service, calls notify_fn with a heads-up instead of ever attempting
        to fill in a login form.
        """
        services_to_check = [svc for svc, account in loaded.configured_services.items() if account]
        if not services_to_check:
            return {}

        checks = await self.detector.detect_all(engine, services_to_check)
        results = {svc: check.authenticated for svc, check in checks.items()}
        await self.registry.record_session_check(loaded.id, results)

        for svc, check in checks.items():
            if check.authenticated is False and notify_fn is not None:
                account = loaded.configured_services.get(svc) or "?"
                try:
                    await notify_fn(
                        f"[{loaded.name}] {svc.capitalize()} account ({account}) is not authenticated in this "
                        f"profile's Chrome session. Please log in manually -- I won't enter credentials for you."
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("check_sessions notify_fn failed for %s/%s", loaded.name, svc)

        return {svc: {"authenticated": c.authenticated, "detail": c.detail} for svc, c in checks.items()}

    # ------------------------------------------------------------------ #
    # Auto-selection: "if no profile is specified, automatically choose
    # the best available profile" -- used by chat/Telegram (see
    # ChatEngine._enqueue_with_profile) so the agent never has to stop and
    # ask which profile to use when more than one exists.
    # ------------------------------------------------------------------ #
    @staticmethod
    def choose_best_profile(available: list[dict[str, Any]], website: Optional[str] = None) -> Optional[dict[str, Any]]:
        """
        Picks one profile from `available` (already filtered to enabled
        profiles by the caller) without asking the user. Preference order:

          1. Never a profile that's currently IN_USE or NEEDS_LOGIN over one
             that's READY -- a task shouldn't get parked behind a busy or
             half-authenticated identity when a clean one exists.
          2. If `website` is given, prefer a profile whose gmail/x/discord
             account is actually on that domain (e.g. an X task naturally
             wants the profile with an x_account set) -- it's more likely to
             already be authenticated for the task at hand.
          3. Among remaining ties, the most recently used profile wins: it's
             likely still warm/authenticated, whereas an untouched profile is
             more likely to hit a login wall.
          4. Final tiebreak: profile name, for determinism.

        Returns None only if `available` is empty.
        """
        if not available:
            return None

        def status_rank(p: dict[str, Any]) -> int:
            status = p.get("status")
            if status == "ready":
                return 0
            if status == "needs_login":
                return 1
            if status == "in_use":
                return 2
            return 3  # disabled/error shouldn't reach here, but don't crash if they do

        def domain_match(p: dict[str, Any]) -> int:
            if not website:
                return 1
            host = website.lower()
            accounts = {
                "gmail": p.get("gmail_account"),
                "x": p.get("x_account"),
                "discord": p.get("discord_account"),
            }
            hints = {
                "gmail": ("gmail.com", "mail.google.com", "google.com"),
                "x": ("x.com", "twitter.com"),
                "discord": ("discord.com", "discord.gg"),
            }
            for service, account in accounts.items():
                if account and any(h in host for h in hints[service]):
                    return 0
            return 1

        def recency_score(p: dict[str, Any]) -> float:
            # Higher = more recently used = preferred. Never-used profiles
            # get 0 so any real timestamp outranks them.
            raw = p.get("last_used_at")
            if not raw:
                return 0.0
            try:
                from datetime import datetime

                return datetime.fromisoformat(raw).timestamp()
            except (ValueError, TypeError):
                return 0.0

        ranked = sorted(
            available,
            key=lambda p: (status_rank(p), domain_match(p), -recency_score(p), p.get("name") or ""),
        )
        return ranked[0]

    async def release(self, profile_id: str) -> None:
        """Called when a task finishes (success, failure, or cancel) so the
        profile doesn't stay stuck showing IN_USE."""
        try:
            profile = await self.registry.get_profile(profile_id)
        except ProfileNotFoundError:
            return
        if profile.status.value == "in_use":  # type: ignore[union-attr]
            await self.registry.update_profile(
                profile_id, status="needs_login" if profile.gmail_authenticated is False
                or profile.x_authenticated is False or profile.discord_authenticated is False else "ready"
            )
