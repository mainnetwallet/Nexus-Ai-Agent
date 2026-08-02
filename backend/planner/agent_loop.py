"""
The generic planning loop.

Given (website, goal, wallet_label, notes) this NEVER contains site-specific
logic. It perceives the page via BrowserEngine, asks the LLM to reason about
what to do next, executes one action, verifies the result, and repeats until
the goal is met, the plan stalls, or max_steps is hit.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from backend.browser.engine import BrowserEngine
from backend.memory.store import MemoryStore
from backend.planner.decision_engine import SYSTEM_PROMPT, DecisionEngine
from backend.planner.llm_client import LLMClient
from backend.vision.vision_engine import VisionAnalyzer
from backend.wallet.manager import WalletManager

logger = logging.getLogger("nexus.planner")

# SYSTEM_PROMPT now lives in backend.planner.decision_engine; re-exported
# here for backward compatibility with any existing `from
# backend.planner.agent_loop import SYSTEM_PROMPT` callers.


class StepAction(str, Enum):
    CLICK = "click"
    TYPE = "type"
    NAVIGATE = "navigate"
    SCROLL = "scroll"
    WAIT = "wait"
    UPLOAD = "upload"
    FINISH = "finish"
    BLOCKED = "blocked"
    WALLET_POPUP = "wallet_popup"


@dataclass
class StepResult:
    index: int
    action: str
    target: str
    value: str
    reasoning: str
    success: bool
    screenshot_path: str
    note: str = ""


@dataclass
class TaskOutcome:
    status: str  # succeeded | failed | blocked
    steps: list[StepResult] = field(default_factory=list)
    summary: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0


class AgentLoop:
    def __init__(
        self,
        engine: BrowserEngine,
        memory: MemoryStore,
        wallet: Optional[WalletManager] = None,
        llm: Optional[LLMClient] = None,
        max_steps: int = 40,
        on_step: Optional[Any] = None,
        vision: Optional[VisionAnalyzer] = None,
        should_cancel: Optional[Any] = None,
        wait_if_paused: Optional[Any] = None,
        task_id: Optional[str] = None,
        plugin_registry: Optional[Any] = None,
    ) -> None:
        self.engine = engine
        self.memory = memory
        self.wallet = wallet
        self.llm = llm or LLMClient()
        self.max_steps = max_steps
        self.on_step = on_step  # optional async callback(StepResult) for live streaming (e.g. Telegram)
        self.vision = vision or VisionAnalyzer(llm=self.llm)
        self.should_cancel = should_cancel  # optional sync callable() -> bool, checked once per step
        self.wait_if_paused = wait_if_paused  # optional async callable() -> None; awaited once per step, before any work
        self.task_id = task_id  # optional, used only to tag plugin hook dispatch and wallet-popup veto lookups
        self.plugin_registry = plugin_registry  # optional PluginRegistry; hooks are no-ops if None

        # Dedicated reasoning module (see backend/planner/decision_engine.py):
        # owns perception-fallback, LLM decision, verification, and recovery
        # hints. Reuses the same llm/vision instances so callers that pass
        # their own (e.g. tests with a FakeLLM) get identical behavior to
        # before this was extracted.
        self.decision_engine = DecisionEngine(llm=self.llm, vision=self.vision)

    async def run(self, website: str, goal: str, wallet_label: str | None = None, notes: str = "") -> TaskOutcome:
        outcome = TaskOutcome(status="failed")

        if self.plugin_registry is not None and self.task_id is not None:
            await self.plugin_registry.dispatch_task_start(self.task_id, website, goal)

        await self.engine.navigate(website)

        similar = await self.memory.recall_similar_workflows(website=website, goal=goal, top_k=3)
        prior_context = self._format_prior_context(similar)

        stall_count = 0
        last_url = None
        recovery_context = ""

        for step_index in range(self.max_steps):
            if self.wait_if_paused is not None:
                await self.wait_if_paused()

            if self.should_cancel is not None and self.should_cancel():
                outcome.status = "cancelled"
                outcome.summary = "Task was cancelled."
                break

            url_before = self.engine.page.url
            snapshot = await self.engine.snapshot(name_hint=f"step{step_index}")
            snapshot, _perception = await self.decision_engine.perceive(snapshot, goal)

            popup_id = await self.engine.detect_popup_or_dialog(timeout_ms=300)
            if popup_id:
                logger.info("Popup detected mid-task (likely wallet or auth) tab=%s", popup_id)

            decision = await self.decision_engine.decide(
                goal, wallet_label, notes, snapshot, prior_context, recovery_context
            )
            action = decision.action
            target = decision.target
            value = decision.value
            reasoning = decision.reasoning

            if action == StepAction.FINISH.value:
                outcome.status = "succeeded"
                outcome.summary = reasoning or "Goal reported complete by planner."
                break

            if action == StepAction.BLOCKED.value:
                outcome.status = "blocked"
                outcome.summary = reasoning or "Planner reported it is blocked."
                break

            if action == StepAction.WALLET_POPUP.value:
                if self.wallet is None:
                    outcome.status = "blocked"
                    outcome.summary = "Wallet popup detected but no WalletManager configured."
                    break
                await self.wallet.handle_pending_popup(self.engine, wallet_label, task_id=self.task_id)
                continue

            success = await self._execute_action(action, target, value)
            shot = await self.engine.screenshot(name_hint=f"post_step{step_index}")

            step_result = StepResult(
                index=step_index,
                action=action,
                target=target,
                value=value,
                reasoning=reasoning,
                success=success,
                screenshot_path=shot,
            )
            outcome.steps.append(step_result)
            if self.on_step:
                await self.on_step(step_result)
            if self.plugin_registry is not None and self.task_id is not None:
                await self.plugin_registry.dispatch_step(self.task_id, step_result)

            url_after = self.engine.page.url
            self.decision_engine.verify(url_before, url_after, action, success)

            if url_after == last_url:
                stall_count += 1
            else:
                stall_count = 0
            last_url = url_after

            # Advisory-only: folded into the next decide() call's prompt so
            # the planner sees what went wrong, without changing control flow.
            recovery_context = self.decision_engine.recovery_hint(action, target, success, stall_count)

            if stall_count >= 4:
                outcome.status = "failed"
                outcome.summary = "Agent stalled: page state stopped changing after repeated actions."
                break
        else:
            outcome.status = "failed"
            outcome.summary = f"Max steps ({self.max_steps}) reached without completion."

        outcome.finished_at = time.time()
        await self.memory.save_workflow_outcome(website=website, goal=goal, outcome=outcome)
        if self.plugin_registry is not None and self.task_id is not None:
            await self.plugin_registry.dispatch_task_finish(self.task_id, outcome.status, outcome.summary)
        return outcome

    async def _execute_action(self, action: str, target: str, value: str) -> bool:
        try:
            if action == StepAction.CLICK.value:
                return await self.engine.smart_click(target)
            if action == StepAction.TYPE.value:
                return await self.engine.smart_type(target, value)
            if action == StepAction.NAVIGATE.value:
                await self.engine.navigate(value or target)
                return True
            if action == StepAction.SCROLL.value:
                await self.engine.smart_scroll(direction=value or "down")
                return True
            if action == StepAction.WAIT.value:
                await self.engine.smart_wait()
                return True
            if action == StepAction.UPLOAD.value:
                return await self.engine.upload_file(target, value)
            logger.warning("Unknown action from planner: %s", action)
            return False
        except Exception:
            logger.exception("Action execution failed: action=%s target=%s", action, target)
            return False

    @staticmethod
    def _format_prior_context(similar: list[dict[str, Any]]) -> str:
        if not similar:
            return "No prior memory of similar tasks."
        lines = ["PRIOR RELEVANT EXPERIENCE (from memory, may or may not still apply):"]
        for item in similar:
            lines.append(f"- {item.get('summary', '')} (confidence={item.get('confidence', 0):.2f})")
        return "\n".join(lines)
