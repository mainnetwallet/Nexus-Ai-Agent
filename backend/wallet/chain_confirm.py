"""
ChainConfirmationManager: session-based confirmation manager for unknown chains.

Mirrors TxBatchManager / TeachModeManager's pattern -- holds in-memory drafts of
unverified chain parameter candidates found via web lookup while waiting for
user manual confirmation in chat.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.config.settings import settings
from backend.wallet.chain_web_lookup import WebChainCandidate

logger = logging.getLogger("nexus.wallet.chain_confirm")


@dataclass
class PendingChainConfirmation:
    session_id: str
    candidate: WebChainCandidate
    intent: dict[str, Any]
    text: str
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)


class ChainConfirmationManager:
    def __init__(self) -> None:
        self._pending: dict[str, PendingChainConfirmation] = {}

    def _expire_if_idle(self, session_id: str) -> None:
        draft = self._pending.get(session_id)
        timeout = getattr(settings, "chain_confirmation_timeout_seconds", 600)
        if draft is not None and (time.monotonic() - draft.last_activity) > timeout:
            logger.info("Chain confirmation draft expired due to inactivity: session=%s", session_id)
            self._pending.pop(session_id, None)

    def is_active(self, session_id: str) -> bool:
        self._expire_if_idle(session_id)
        return session_id in self._pending

    def start(
        self,
        session_id: str,
        candidate: WebChainCandidate,
        intent: dict[str, Any],
        text: str,
    ) -> PendingChainConfirmation:
        item = PendingChainConfirmation(
            session_id=session_id,
            candidate=candidate,
            intent=intent,
            text=text,
        )
        self._pending[session_id] = item
        logger.info(
            "Pending chain confirmation started: session=%s chain=%s (ID=%s)",
            session_id, candidate.display_name, candidate.chain_id_int,
        )
        return item

    def get_pending(self, session_id: str) -> Optional[PendingChainConfirmation]:
        self._expire_if_idle(session_id)
        return self._pending.get(session_id)

    def cancel(self, session_id: str) -> bool:
        return self._pending.pop(session_id, None) is not None

    def pop_confirmed(self, session_id: str) -> Optional[PendingChainConfirmation]:
        self._expire_if_idle(session_id)
        return self._pending.pop(session_id, None)
