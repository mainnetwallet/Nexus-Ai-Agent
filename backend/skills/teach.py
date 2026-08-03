"""
Teach Mode + natural-language skill authoring + correction parsing.

Three distinct "learn from ..." entry points from the task description live
here, all LLM-assisted but each producing the same shape of output (a
workflow step, or a full skill draft) that backend.skills.library.SkillService
persists:

- Teach Mode: a short multi-turn chat flow (start -> repeated "then click
  the button" style step -> done) driven by TeachModeManager below.
- Natural language: one message describing an entire skill end-to-end,
  parsed in a single LLM call by parse_skill_from_text().
- User corrections: "no, actually type it into the email field", parsed by
  parse_correction() into a replacement/insertion step for an existing
  skill's workflow.

TeachModeManager itself holds only small, process-lifetime, per-chat-session
drafts in memory -- exactly like backend.skills.library.SkillService's
`_pending` suggestions -- committed to the database only on finish().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.planner.llm_client import LLMClient
from backend.planner.model_manager import TaskType, model_manager

logger = logging.getLogger("nexus.skills.teach")

SKILL_FROM_TEXT_PROMPT = """You convert a user's free-form description of a repeatable browser task into a \
structured Skill for an autonomous browser-automation agent. Respond with STRICT JSON only, no prose, no \
markdown fences:
{
  "name": "short skill name",
  "description": "one or two sentence description",
  "category": "short category label, e.g. defi, wallet, research, forms, social",
  "trigger": "one or more natural-language phrases that should activate this skill, one per line",
  "website_hint": "the primary website/domain this skill runs on, if mentioned, else empty",
  "variables": [{"name": "var_name", "description": "what it's for", "default": ""}],
  "workflow": [
    {"action": "navigate|click|type|scroll|wait|upload", "target": "selector/text/url", "value": "text to type or \
url, empty if not needed", "description": "what this step does"}
  ]
}
Guidance:
- Use {{variable_name}} inside target/value wherever the user implies something that will change between runs \
(an amount, a search term, a recipient) and declare it in "variables".
- Keep workflow steps minimal and in the order the user described them.
- If the user's description is too vague to produce concrete steps, return an empty workflow array rather than \
inventing actions."""

CORRECTION_PROMPT = """You convert a user's correction about a browser-automation step into a structured \
replacement step. Respond with STRICT JSON only, no prose, no markdown fences:
{
  "action": "navigate|click|type|scroll|wait|upload",
  "target": "selector/text/url",
  "value": "text to type or url, empty if not needed",
  "description": "what this corrected step does"
}
The user is telling you what a specific step in a learned skill should have done instead. Produce only the \
corrected step, not the whole skill."""

STEP_FROM_TEXT_PROMPT = """You convert one sentence describing a single browser action, spoken during an \
interactive teaching session, into a structured step. Respond with STRICT JSON only, no prose, no markdown fences:
{
  "action": "navigate|click|type|scroll|wait|upload",
  "target": "selector/text/url",
  "value": "text to type or url, empty if not needed",
  "description": "short restatement of the step"
}"""


@dataclass
class TeachDraft:
    name: str = ""
    description: str = ""
    category: str = "general"
    trigger: str = ""
    website_hint: str = ""
    variables: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)


class TeachModeManager:
    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self.llm = llm or model_manager
        self._drafts: dict[str, TeachDraft] = {}

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #
    def is_active(self, session_id: str) -> bool:
        return session_id in self._drafts

    def start(self, session_id: str, name: str = "", trigger: str = "", website_hint: str = "") -> TeachDraft:
        draft = TeachDraft(name=name, trigger=trigger, website_hint=website_hint)
        self._drafts[session_id] = draft
        return draft

    def cancel(self, session_id: str) -> bool:
        return self._drafts.pop(session_id, None) is not None

    def get_draft(self, session_id: str) -> Optional[TeachDraft]:
        return self._drafts.get(session_id)

    def undo_last_step(self, session_id: str) -> bool:
        draft = self._drafts.get(session_id)
        if not draft or not draft.steps:
            return False
        draft.steps.pop()
        return True

    def add_step_raw(self, session_id: str, action: str, target: str, value: str = "", description: str = "") -> bool:
        draft = self._drafts.get(session_id)
        if not draft:
            return False
        draft.steps.append({"action": action, "target": target, "value": value, "description": description})
        return True

    async def add_step_from_text(self, session_id: str, text: str) -> Optional[dict[str, Any]]:
        draft = self._drafts.get(session_id)
        if not draft:
            return None
        try:
            step = await self.llm.complete_json(STEP_FROM_TEXT_PROMPT, text, task_type=TaskType.BROWSER_AUTOMATION)
        except Exception:
            logger.exception("Teach Mode step parse failed")
            return None
        step = {
            "action": (step.get("action") or "click").lower(),
            "target": step.get("target", ""),
            "value": step.get("value", ""),
            "description": step.get("description", text),
        }
        draft.steps.append(step)
        return step

    def finish(self, session_id: str) -> Optional[TeachDraft]:
        return self._drafts.pop(session_id, None)

    # ------------------------------------------------------------------ #
    # Whole-skill natural language authoring
    # ------------------------------------------------------------------ #
    async def parse_skill_from_text(self, text: str) -> dict[str, Any]:
        try:
            parsed = await self.llm.complete_json(SKILL_FROM_TEXT_PROMPT, text, task_type=TaskType.PLANNING)
        except Exception:
            logger.exception("Natural-language skill parse failed")
            return {"name": "", "workflow": []}
        return {
            "name": parsed.get("name") or "",
            "description": parsed.get("description", ""),
            "category": parsed.get("category") or "general",
            "trigger": parsed.get("trigger", ""),
            "website_hint": parsed.get("website_hint") or None,
            "variables": parsed.get("variables") or [],
            "workflow": parsed.get("workflow") or [],
        }

    # ------------------------------------------------------------------ #
    # Corrections
    # ------------------------------------------------------------------ #
    async def parse_correction(self, instruction: str) -> dict[str, Any]:
        try:
            step = await self.llm.complete_json(CORRECTION_PROMPT, instruction, task_type=TaskType.BROWSER_AUTOMATION)
        except Exception:
            logger.exception("Correction parse failed")
            return {"action": "click", "target": "", "value": "", "description": instruction}
        return {
            "action": (step.get("action") or "click").lower(),
            "target": step.get("target", ""),
            "value": step.get("value", ""),
            "description": step.get("description", instruction),
        }
