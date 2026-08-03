"""
Pending Profile Selection.

Every browser task launched from AI Chat needs a persistent Chrome Profile
(backend/identity/) so its cookies/local storage/session state/extensions
have somewhere durable to live -- see backend/identity/manager.py's
ProfileManager docstring for the full load sequence. Rather than silently
launching a throwaway, non-persistent browser context when the user's
message didn't name a profile, ChatEngine parks the task here and asks
which Chrome Profile to use.

Mirrors the same "session intercepts every message until resolved" pattern
already used by backend/skills/teach.py's TeachModeManager and
backend/wallet/tx_batch.py's TxBatchManager, so a follow-up like "use
Profile-01" or a plain profile name is understood as answering this
question rather than being reclassified as a new, unrelated message.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class PendingTask:
    website: str
    goal: str
    wallet_label: Optional[str]
    notes: str = ""
    priority: int = 1


class PendingProfileManager:
    """In-memory only, same lifetime/scope as TxBatchManager/TeachModeManager
    -- a pending selection doesn't need to survive a process restart, and if
    it doesn't the worst case is just asking the user to re-send the task."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingTask] = {}

    def start(self, session_id: str, task: PendingTask) -> None:
        self._pending[session_id] = task

    def is_active(self, session_id: str) -> bool:
        return session_id in self._pending

    def get(self, session_id: str) -> Optional[PendingTask]:
        return self._pending.get(session_id)

    def cancel(self, session_id: str) -> None:
        self._pending.pop(session_id, None)
