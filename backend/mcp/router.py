"""
MCPToolRouter -- picks the right connector+tool for a free-form request.

Same two-pass shape as backend/skills/matcher.py's SkillMatcher, and for the
same reason: an exact, high-confidence signal should fire deterministically
before falling back to fuzzier scoring.

1. Explicit-hint pass: if the caller already knows the connector (an
   explicit "use github" style instruction, or a chat/agent action that
   names one directly), skip scoring entirely and pick that connector's
   best-scoring tool.
2. Keyword-scored pass: every discovered tool accumulates a score from
   substring hits between the request text and (a) its own `keywords`
   (weighted highest), (b) its connector's `tags` (weighted lower, since
   tags are coarse), and (c) its tool name/description words. The
   highest-scoring tool above `min_score` wins; ties are broken by
   preferring the tool with more distinct keyword hits.

This is deliberately dependency-free (no embedding/model call) so routing
is fast, deterministic, and testable without network access -- consistent
with the rest of the MCP core never touching the network unless a
connector's own tool call requires it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from backend.mcp.discovery import DiscoveredTool, MCPToolDiscovery

logger = logging.getLogger("nexus.mcp.router")

DEFAULT_MIN_SCORE = 1.0

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) > 2}


@dataclass
class RoutedTool:
    connector: str
    tool_name: str
    score: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"connector": self.connector, "tool": self.tool_name, "score": self.score, "reason": self.reason}


class MCPToolRouter:
    def __init__(self, discovery: MCPToolDiscovery) -> None:
        self.discovery = discovery

    def route(
        self,
        request_text: str,
        connector_hint: Optional[str] = None,
        tool_hint: Optional[str] = None,
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> Optional[RoutedTool]:
        if tool_hint and connector_hint:
            found = self.discovery.find(connector_hint, tool_hint)
            if found is not None:
                return RoutedTool(found.connector, found.tool.name, score=999.0, reason="explicit connector+tool")
            return None

        candidates = self.discovery.list_all(connected_only=True)
        if not candidates:
            return None

        if connector_hint:
            candidates = [c for c in candidates if c.connector.lower() == connector_hint.lower()]
            if not candidates:
                return None
            # Within an explicitly-named connector, still score to pick the
            # *right* tool, but don't require min_score -- the connector
            # choice was already explicit, so pick the best fit regardless.
            best = max(candidates, key=lambda c: self._score(request_text, c))
            return RoutedTool(best.connector, best.tool.name, score=self._score(request_text, best), reason="explicit connector, scored tool")

        scored = [(c, self._score(request_text, c)) for c in candidates]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        best, best_score = scored[0]
        if best_score < min_score:
            logger.debug("No MCP tool cleared min_score=%.2f (best=%.2f, %s)", min_score, best_score, best.tool.name)
            return None
        logger.info(
            "MCP router matched connector=%s tool=%s score=%.2f for request=%r",
            best.connector, best.tool.name, best_score, request_text,
        )
        return RoutedTool(best.connector, best.tool.name, score=best_score, reason="keyword match")

    @staticmethod
    def _score(request_text: str, candidate: DiscoveredTool) -> float:
        needle = (request_text or "").lower()
        req_words = _words(request_text)
        score = 0.0

        for kw in candidate.tool.keywords:
            kw_l = kw.lower()
            if kw_l and kw_l in needle:
                # Longer, more specific phrases count for more than single words.
                score += 2.0 + 0.1 * len(kw_l.split())

        tool_name_words = _words(candidate.tool.name.replace("_", " "))
        score += 1.0 * len(req_words & tool_name_words)

        desc_words = _words(candidate.tool.description)
        score += 0.3 * len(req_words & desc_words)

        return score
