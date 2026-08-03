"""
Identity & Profile Manager.

Each Browser Profile (backend.database.models.ProfileRecord) represents one
complete, isolated, reusable online identity: a name, its own persistent
Chrome profile directory (cookies/local storage/session storage/extensions
live there natively, courtesy of backend.browser.engine.BrowserEngine's
existing persistent-context support), and which wallet/Gmail/X/Discord
accounts belong to it.

- backend.identity.fs        -- Chrome profile directory create/clone/delete/inspect
- backend.identity.detector  -- read-only Gmail/X/Discord login-state detection
- backend.identity.registry  -- DB-backed CRUD/clone/import-export/enable-disable
- backend.identity.manager   -- task-startup orchestration (the facade other
                                  subsystems call; see ProfileManager)
"""
from backend.identity.manager import LoadedProfile, ProfileManager
from backend.identity.registry import ProfileError, ProfileNotFoundError, ProfileRegistry

__all__ = [
    "ProfileManager",
    "LoadedProfile",
    "ProfileRegistry",
    "ProfileNotFoundError",
    "ProfileError",
]
