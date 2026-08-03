"""
SkillMatcher -- "before planning any task: search the Skill Library, execute
a matching skill if found, otherwise create a new plan."

Two passes, fast one first:
1. Keyword/trigger pass: any enabled skill whose `trigger` (one phrase per
   line) appears as a substring of the goal, or vice versa, is an immediate
   high-confidence match -- this is what lets an exact taught trigger phrase
   fire deterministically without depending on embedding quality.
2. Semantic pass: falls back to SkillService.semantic_search() over
   name+category+trigger+description, gated by settings.skills_match_min_score
   so a low-confidence "kind of similar" skill never silently hijacks a
   task the user actually wanted freshly planned.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.config.settings import settings
from backend.skills.library import SkillService

logger = logging.getLogger("nexus.skills.matcher")


class SkillMatcher:
    def __init__(self, library: SkillService) -> None:
        self.library = library

    async def find_match(
        self, goal: str, website: Optional[str] = None, min_score: Optional[float] = None
    ) -> Optional[dict[str, Any]]:
        if not goal or not goal.strip():
            return None

        enabled_skills = await self.library.list(enabled_only=True)
        if not enabled_skills:
            return None

        keyword_hit = self._keyword_match(goal, website, enabled_skills)
        if keyword_hit is not None:
            logger.info("Skill matched by trigger keyword: %s", keyword_hit["name"])
            return keyword_hit

        threshold = settings.skills_match_min_score if min_score is None else min_score
        candidates = await self.library.semantic_search(goal, top_k=5)
        by_id = {s["id"]: s for s in enabled_skills}
        for candidate in candidates:
            skill = by_id.get(candidate["skill_id"])
            if skill is None:
                continue  # index has a stale/disabled skill
            if candidate["score"] < threshold:
                continue
            if not self._website_compatible(skill, website):
                continue
            logger.info("Skill matched semantically: %s (score=%.2f)", skill["name"], candidate["score"])
            return skill
        return None

    @staticmethod
    def _keyword_match(goal: str, website: Optional[str], skills: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        needle = goal.strip().lower()
        best = None
        for skill in skills:
            if not SkillMatcher._website_compatible(skill, website):
                continue
            phrases = [p.strip().lower() for p in (skill.get("trigger") or "").split("\n") if p.strip()]
            for phrase in phrases:
                if phrase and (phrase in needle or needle in phrase):
                    # Prefer the longest/most-specific trigger phrase match.
                    if best is None or len(phrase) > best[1]:
                        best = (skill, len(phrase))
        return best[0] if best else None

    @staticmethod
    def _website_compatible(skill: dict[str, Any], website: Optional[str]) -> bool:
        hint = skill.get("website_hint")
        if not hint or not website:
            return True
        return hint.strip().lower() in website.strip().lower() or website.strip().lower() in hint.strip().lower()
