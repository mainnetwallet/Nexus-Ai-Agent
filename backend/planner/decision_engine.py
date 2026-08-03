"""
AI Decision Engine.

A dedicated reasoning module, separate from AgentLoop's orchestration
(browser lifecycle, pause/cancel, plugin dispatch, persistence). It owns:

  - perceive(): read the current browser state -- DOM snapshot, and the
    vision/OCR screenshot fallback when the DOM comes back too sparse to
    act on (canvas UIs, image-only pages).
  - decide(): ask the LLM for the single next action given that state.
  - verify(): after the action executes, check whether it actually had an
    observable effect (URL change, or an explicit failure report).
  - recovery_hint(): when an action failed or the page has stalled, produce
    a short advisory note that gets folded into the next decide() call's
    context -- never a hard behavior change, just better-informed context.
  - Every decision and verification is logged via the standard `logging`
    module (logger "nexus.decision_engine"), which is what feeds the live
    logs endpoint/WebSocket (backend/api/routes_logs.py) -- no new storage
    surface needed for "logs reasoning".

This is the same reasoning that used to live inline in
`agent_loop._decide_next_action` / the vision-fallback block at the top of
`AgentLoop.run`, extracted so it can be tested and reasoned about on its
own. It changes no external behavior: `AgentLoop.llm` and `AgentLoop.vision`
still exist with the same meaning, `StepResult`/`TaskOutcome` are untouched,
and every existing test in `backend/tests/test_agent_loop.py` still
exercises the same decision shape through `AgentLoop(llm=...)`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.browser.engine import PageSnapshot
from backend.planner.llm_client import LLMClient
from backend.planner.model_manager import TaskType, model_manager
from backend.vision.vision_engine import VisionAnalyzer, VisionPerception

logger = logging.getLogger("nexus.decision_engine")

SYSTEM_PROMPT = """You are the planning brain of an autonomous browser agent called Nexus-Agent.
You are given the CURRENT state of a webpage (visible text + a list of interactive elements)
and a user's GOAL. You must decide the single next best action to move toward the goal.

Rules:
- You have NEVER seen this website before and must reason purely from what is visible now.
- Prefer the most specific, unambiguous element description available (visible button/link text).
- If the goal appears already complete, return action "finish".
- If you are blocked (e.g. captcha, login wall, missing wallet connection you cannot perform),
  return action "blocked" and explain why in "reasoning".
- If a wallet-signing / transaction-approval popup seems to be open, return action "wallet_popup".
- If the goal requires something the page itself cannot do -- reading/writing a local file,
  running an allow-listed shell command, checking/creating a GitHub issue or PR, or fetching a
  URL that is not the current page -- return action "mcp_tool" instead of trying to fake it with
  click/type/navigate. Set "target" to "connector.tool" if you know exactly which one applies
  (connectors: filesystem, terminal, github, browser), or to a short free-text description of
  what you need if you don't (it will be routed automatically). Set "value" to a JSON object
  string of the tool's arguments, e.g. "{\\"path\\": \\"notes.txt\\", \\"content\\": \\"...\\"}"
  (empty object "{}" if none needed).
- If RECOVERY context is present below, take it into account -- it describes what went wrong
  on the previous attempt so you don't repeat the same failing action blindly.
- Respond with STRICT JSON only, no prose, no markdown fences, matching this schema:
{
  "action": "click | type | navigate | scroll | wait | upload | mcp_tool | finish | blocked | wallet_popup",
  "target": "visible text or description of the element to act on (empty for navigate/scroll/wait/finish; \\"connector.tool\\" or free text for mcp_tool)",
  "value": "text to type, URL to navigate to, scroll direction, or a JSON arguments object string for mcp_tool (empty otherwise)",
  "reasoning": "one sentence why this action was chosen",
  "confidence": 0.0-1.0
}"""


@dataclass
class Decision:
    """The next action the decision engine wants AgentLoop to execute."""

    action: str
    target: str = ""
    value: str = ""
    reasoning: str = ""
    confidence: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    """Whether a just-executed action appears to have had an observable effect."""

    changed: bool
    url_before: str
    url_after: str
    note: str = ""


class DecisionEngine:
    """
    Reads browser state (DOM snapshot + vision/OCR fallback), decides the
    next action via the LLM, verifies the result of the previous action, and
    produces recovery guidance. Stateless across calls except for the
    LLM/vision clients it wraps -- AgentLoop owns all task-level state
    (step history, stall counters, cancellation).
    """

    def __init__(self, llm: Optional[LLMClient] = None, vision: Optional[VisionAnalyzer] = None) -> None:
        self.llm = llm or model_manager
        self.vision = vision or VisionAnalyzer(llm=self.llm)

    # ------------------------------------------------------------------ #
    # Perception
    # ------------------------------------------------------------------ #
    def should_use_vision(self, interactive_elements: list[dict[str, Any]]) -> bool:
        return self.vision.should_trigger(interactive_elements)

    async def perceive(self, snapshot: PageSnapshot, goal: str) -> tuple[PageSnapshot, Optional[VisionPerception]]:
        """
        Enriches `snapshot` in place with the vision/OCR fallback when the DOM
        came back too sparse to act on. Returns the snapshot and the raw
        VisionPerception (None if the fallback wasn't triggered).
        """
        if not self.should_use_vision(snapshot.interactive_elements):
            return snapshot, None

        logger.info(
            "Only %d DOM elements found; falling back to vision/OCR perception",
            len(snapshot.interactive_elements),
        )
        perception = await self.vision.analyze(snapshot.screenshot_path, goal)
        snapshot.interactive_elements = self.vision.merge_into_elements(
            snapshot.interactive_elements, perception
        )
        if perception.ocr_text:
            snapshot.visible_text = (snapshot.visible_text + "\n\n[OCR TEXT]\n" + perception.ocr_text).strip()
        return snapshot, perception

    # ------------------------------------------------------------------ #
    # Decision
    # ------------------------------------------------------------------ #
    async def decide(
        self,
        goal: str,
        wallet_label: str | None,
        notes: str,
        snapshot: PageSnapshot,
        prior_context: str,
        recovery_context: str = "",
    ) -> Decision:
        user_prompt = f"""GOAL: {goal}
WALLET: {wallet_label or "none specified"}
NOTES: {notes or "none"}

{prior_context}
{recovery_context}

CURRENT PAGE URL: {snapshot.url}
CURRENT PAGE TITLE: {snapshot.title}

VISIBLE TEXT (truncated):
{snapshot.visible_text[:3000]}

INTERACTIVE ELEMENTS (up to 150):
{snapshot.interactive_elements}
"""
        try:
            raw = await self.llm.complete_json(SYSTEM_PROMPT, user_prompt, task_type=TaskType.BROWSER_AUTOMATION)
        except Exception:
            logger.exception("Decision engine LLM call failed")
            raw = {"action": "blocked", "reasoning": "LLM planning call failed", "target": "", "value": ""}

        decision = Decision(
            action=raw.get("action", "blocked") or "blocked",
            target=raw.get("target", "") or "",
            value=raw.get("value", "") or "",
            reasoning=raw.get("reasoning", "") or "",
            confidence=float(raw.get("confidence") or 0.0),
            raw=raw,
        )
        logger.info(
            "decision goal=%r action=%s target=%r confidence=%.2f reasoning=%r",
            goal,
            decision.action,
            decision.target,
            decision.confidence,
            decision.reasoning,
        )
        return decision

    # ------------------------------------------------------------------ #
    # Verification
    # ------------------------------------------------------------------ #
    def verify(self, url_before: str, url_after: str, action: str, success: bool) -> VerificationResult:
        """
        Best-effort verification that an executed action had an observable
        effect. A same-URL result is not necessarily a failure (in-page
        state changes -- e.g. opening a dropdown -- don't change the URL),
        so this only produces a note; AgentLoop's own stall counter (which
        tracks consecutive no-change steps) is what actually drives recovery
        timing.
        """
        changed = url_after != url_before
        if not success:
            note = f"action '{action}' reported failure"
        elif changed:
            note = "url changed -- action had a visible effect"
        else:
            note = "url unchanged -- may be a valid in-page change, or a stall"
        result = VerificationResult(changed=changed, url_before=url_before, url_after=url_after, note=note)
        logger.info(
            "verify action=%s success=%s url_before=%s url_after=%s note=%s",
            action,
            success,
            url_before,
            url_after,
            note,
        )
        return result

    # ------------------------------------------------------------------ #
    # Recovery
    # ------------------------------------------------------------------ #
    def recovery_hint(self, action: str, target: str, success: bool, stall_count: int) -> str:
        """
        Produces advisory text folded into the next decide() call's prompt
        when the previous action failed or the page has stalled for
        multiple consecutive steps. Returns "" when there's nothing to add.
        This never changes control flow by itself -- it only gives the LLM
        better context for its next decision.
        """
        if not success:
            hint = (
                f"RECOVERY: the previous action ({action} '{target}') failed to execute. "
                "Consider a different, more specific element description, scrolling to reveal "
                "the element, or waiting for the page to finish loading."
            )
        elif stall_count >= 2:
            hint = (
                "RECOVERY: the page has not changed for multiple steps in a row. Consider "
                "scrolling, waiting, or reconsidering whether the goal is already complete "
                "(action 'finish') or blocked (action 'blocked')."
            )
        else:
            return ""
        logger.info(hint)
        return hint
