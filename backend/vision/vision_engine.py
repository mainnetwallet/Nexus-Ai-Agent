"""
Vision fallback for the planner.

The primary perception path is DOM extraction (backend/browser/engine.py::
extract_interactive_elements). It's fast, cheap, and accurate for ordinary
HTML pages. It fails on:
  - canvas-rendered UIs (games, some wallet widgets, drawing/annotation tools)
  - image-only content (scanned docs, some captchas, flattened marketing pages)
  - heavily obfuscated/shadow-DOM widgets that don't expose clean ARIA roles

This module is the fallback: it sends the page screenshot to a vision-capable
LLM and asks it to describe actionable elements in the SAME shape the planner
already expects (see backend/planner/agent_loop.py's element dicts), so no
downstream code needs to know whether an element came from the DOM or from
vision. It never encodes logic about any specific website.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.config.settings import settings
from backend.planner.llm_client import LLMClient
from backend.vision.ocr import OCREngine, OCRResult

logger = logging.getLogger("nexus.vision")

VISION_SYSTEM_PROMPT = """You are the visual-perception module of an autonomous browser agent.
You are given a screenshot of the CURRENT state of a webpage. The normal DOM-based element
reader found too little to act on (likely a canvas-rendered UI, an image-only page, or a
heavily obfuscated widget), so you are being asked to read the pixels directly.

Rules:
- Describe only what is visibly present in the screenshot. Never invent elements.
- Do not reason about any specific known website -- treat this as a page you've never seen.
- Respond with STRICT JSON only, no prose, no markdown fences, matching this schema:
{
  "page_summary": "one or two sentence description of what this page shows",
  "elements": [
    {"text": "visible label or description of the clickable/typeable element", "kind": "button|link|input|tab|other", "approx_location": "top-left|top|top-right|center|bottom-left|bottom|bottom-right"}
  ]
}
List at most 20 of the most relevant elements toward typical page goals (forms, primary
buttons, navigation)."""


@dataclass
class VisionPerception:
    triggered: bool
    page_summary: str = ""
    vision_elements: list[dict[str, Any]] = field(default_factory=list)
    ocr_text: str = ""
    ocr_available: bool = False
    error: str = ""


class VisionAnalyzer:
    """
    Combines OCR (fast, local, always attempted first) with an optional
    vision-LLM read (slower, costs a model call) to enrich a page snapshot
    when DOM extraction comes back too sparse to act on.
    """

    def __init__(self, llm: Optional[LLMClient] = None, ocr: Optional[OCREngine] = None) -> None:
        self.llm = llm or LLMClient()
        self.ocr = ocr or OCREngine()

    def should_trigger(self, interactive_elements: list[dict[str, Any]]) -> bool:
        if not settings.vision_enabled:
            return False
        return len(interactive_elements) < settings.vision_min_elements_threshold

    async def analyze(self, screenshot_path: str, goal: str, force: bool = False) -> VisionPerception:
        """
        Runs OCR unconditionally (cheap) and, if vision is enabled, asks the
        vision-LLM to describe actionable elements from the screenshot.
        `force=True` skips the threshold check (used by callers that already
        decided a fallback is warranted, e.g. after a failed action).
        """
        ocr_result: OCRResult = await self.ocr.extract_text(screenshot_path)

        if not settings.vision_enabled:
            return VisionPerception(
                triggered=False,
                ocr_text=ocr_result.text,
                ocr_available=ocr_result.available,
            )

        try:
            user_prompt = f"GOAL the agent is trying to accomplish: {goal or 'unspecified'}\n\nAnalyze the attached screenshot."
            decision = await self.llm.complete_json_with_image(
                VISION_SYSTEM_PROMPT, user_prompt, screenshot_path, max_tokens=1200
            )
            elements = decision.get("elements", []) or []
            return VisionPerception(
                triggered=True,
                page_summary=decision.get("page_summary", ""),
                vision_elements=elements[:20],
                ocr_text=ocr_result.text,
                ocr_available=ocr_result.available,
            )
        except Exception as exc:
            logger.exception("Vision fallback analysis failed")
            return VisionPerception(
                triggered=True,
                ocr_text=ocr_result.text,
                ocr_available=ocr_result.available,
                error=str(exc),
            )

    @staticmethod
    def merge_into_elements(
        dom_elements: list[dict[str, Any]], perception: VisionPerception
    ) -> list[dict[str, Any]]:
        """
        Produces a single element list the planner can reason over, tagging
        vision-sourced elements so the planner (or a human reading logs) can
        tell where each one came from.
        """
        merged = list(dom_elements)
        for el in perception.vision_elements:
            merged.append(
                {
                    "tag": el.get("kind", "other"),
                    "role": "",
                    "text": el.get("text", "")[:120],
                    "type": "",
                    "name": "",
                    "id": "",
                    "source": "vision",
                    "approx_location": el.get("approx_location", ""),
                }
            )
        return merged
