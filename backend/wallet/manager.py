"""
WalletManager automates interaction with browser-extension wallets
(MetaMask, Rabby) purely at the UI level.

Hard security rules (do not weaken these):
- This module NEVER reads, stores, or transmits a seed phrase or private key.
- All signing happens inside the user's own wallet extension; Nexus-Agent
  only decides whether to click "Approve" or "Reject" in that extension's
  popup, based on an explicit, configurable allow-policy.
- By default every approval requires a human in the loop (see
  settings.wallet_require_manual_approval). Auto-approval is only possible
  for contracts the user has explicitly allowlisted AND under the configured
  USD value cap.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from backend.browser.engine import BrowserEngine
from backend.config.settings import settings

logger = logging.getLogger("nexus.wallet")


@dataclass
class ApprovalDecision:
    approve: bool
    reason: str
    auto: bool


class WalletManager:
    def __init__(self, notify_human_fn=None, plugin_registry=None) -> None:
        """
        notify_human_fn: optional async callable(prompt: str) -> bool
        Used to ask a human (e.g. via Telegram) whether to approve a pending
        wallet popup when it falls outside the auto-approve policy.

        plugin_registry: optional PluginRegistry. If set, every enabled
        plugin's on_wallet_popup hook runs after the policy (and any human)
        decision and may veto an approval, but can never turn a reject into
        an approve -- see backend/plugins/registry.py.
        """
        self._notify_human_fn = notify_human_fn
        self.plugin_registry = plugin_registry

    async def handle_pending_popup(
        self, engine: BrowserEngine, wallet_label: Optional[str], task_id: Optional[str] = None
    ) -> ApprovalDecision:
        popup_id = await engine.detect_popup_or_dialog(timeout_ms=500)
        if not popup_id:
            return ApprovalDecision(approve=False, reason="No popup found", auto=False)

        engine.switch_tab(popup_id)
        snapshot_text = await engine.extract_visible_text(max_chars=2000)
        contract_address = self._extract_contract_address(snapshot_text)
        estimated_value = self._extract_value_estimate(snapshot_text)

        decision = await self._evaluate_policy(contract_address, estimated_value)

        if not decision.auto and self._notify_human_fn is not None:
            prompt = (
                f"Wallet approval requested for {wallet_label or 'unknown wallet'}\n"
                f"Contract: {contract_address or 'unknown'}\n"
                f"Estimated value: {estimated_value if estimated_value is not None else 'unknown'}\n"
                f"Approve this action?"
            )
            approved = await self._notify_human_fn(prompt)
            decision = ApprovalDecision(approve=approved, reason="human decision", auto=False)

        if decision.approve and self.plugin_registry is not None:
            plugin_allows = await self.plugin_registry.dispatch_wallet_popup(
                task_id, contract_address, estimated_value, decision.approve
            )
            if not plugin_allows:
                decision = ApprovalDecision(approve=False, reason="vetoed by plugin", auto=False)

        if decision.approve:
            await engine.smart_click("Approve") or await engine.smart_click("Confirm")
        else:
            await engine.smart_click("Reject") or await engine.smart_click("Cancel")

        logger.info(
            "Wallet popup resolved: approve=%s reason=%s contract=%s value=%s",
            decision.approve, decision.reason, contract_address, estimated_value,
        )
        return decision

    async def _evaluate_policy(self, contract_address: Optional[str], estimated_value: Optional[float]) -> ApprovalDecision:
        if settings.wallet_require_manual_approval:
            return ApprovalDecision(approve=False, reason="manual approval required by policy", auto=False)

        if contract_address and contract_address.lower() not in settings.allowlisted_contracts:
            return ApprovalDecision(approve=False, reason="contract not allowlisted", auto=False)

        if estimated_value is not None and estimated_value > settings.wallet_max_auto_approve_value_usd:
            return ApprovalDecision(approve=False, reason="value exceeds auto-approve cap", auto=False)

        return ApprovalDecision(approve=True, reason="within allowlist and value cap", auto=True)

    @staticmethod
    def _extract_contract_address(text: str) -> Optional[str]:
        match = re.search(r"0x[a-fA-F0-9]{40}", text)
        return match.group(0) if match else None

    @staticmethod
    def _extract_value_estimate(text: str) -> Optional[float]:
        match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", text)
        return float(match.group(1)) if match else None
