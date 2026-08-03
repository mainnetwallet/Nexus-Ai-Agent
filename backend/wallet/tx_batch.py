"""
TxBatchManager: multi-turn "queue N transactions" chat flow.

Mirrors backend.skills.teach.TeachModeManager's shape -- small,
process-lifetime, per-chat-session drafts held in memory. A user says
something like "queue 10 transactions" (category=wallet, see
ChatEngine.CLASSIFIER_SYSTEM_PROMPT), then names one destination per
follow-up message ("0.01 ETH to 0xabc... on uniswap.org"); each is queued
as a normal task via the existing TaskQueueService, exactly like any task
enqueued through category="task".

Scope boundary (deliberately narrow): this module only decides *what
tasks to queue*. It never touches wallet approval policy and never grants
extra trust to a wallet. Whether a queued task's on-chain confirmation
still needs a human click is governed entirely, and unchanged, by
backend/wallet/manager.py's existing allowlist+value-cap policy
(settings.wallet_require_manual_approval / wallet_allowlisted_contracts /
wallet_max_auto_approve_value_usd) -- configured by the human via
Settings, never via a chat message. That boundary is intentional: chat
also drives the same browser-automation agent that reads arbitrary
webpages, so a chat-reachable way to loosen approval policy would be a
chat-reachable way for a malicious page to loosen it too. Queuing 10
tasks back-to-back from chat does not change how each one gets approved.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("nexus.wallet.tx_batch")


@dataclass
class TxBatchDraft:
    session_id: str
    wallet_label: Optional[str]
    total: int
    queued: list[str] = field(default_factory=list)  # task_ids queued so far, in order

    @property
    def remaining(self) -> int:
        return max(self.total - len(self.queued), 0)


class TxBatchManager:
    def __init__(self) -> None:
        self._drafts: dict[str, TxBatchDraft] = {}

    def is_active(self, session_id: str) -> bool:
        draft = self._drafts.get(session_id)
        return draft is not None and draft.remaining > 0

    def start(self, session_id: str, total: int, wallet_label: Optional[str] = None) -> TxBatchDraft:
        draft = TxBatchDraft(session_id=session_id, wallet_label=wallet_label or None, total=max(int(total), 1))
        self._drafts[session_id] = draft
        logger.info("Tx batch started: session=%s total=%d wallet=%s", session_id, draft.total, wallet_label)
        return draft

    def get_draft(self, session_id: str) -> Optional[TxBatchDraft]:
        return self._drafts.get(session_id)

    def record_queued(self, session_id: str, task_id: str) -> Optional[TxBatchDraft]:
        """Appends `task_id` to the draft's queued list. Once the draft's
        target count is reached the draft is retired (popped from the
        in-memory map) but the same, now-complete object is still returned
        so the caller can report the final tally."""
        draft = self._drafts.get(session_id)
        if draft is None:
            return None
        draft.queued.append(task_id)
        if draft.remaining <= 0:
            self._drafts.pop(session_id, None)
            logger.info("Tx batch complete: session=%s queued=%d", session_id, len(draft.queued))
        return draft

    def cancel(self, session_id: str) -> bool:
        return self._drafts.pop(session_id, None) is not None
