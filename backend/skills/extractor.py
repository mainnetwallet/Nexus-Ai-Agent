"""
Smart Skill Extractor.

Takes a ``SourceContext`` (produced by any provider) and uses the LLM to
extract structured, reusable skills. Does NOT memorize code line-by-line;
instead distills:
  - reusable workflows & automation strategies
  - API / integration patterns
  - command sequences (build/deploy/test)
  - prompt templates & agent interaction patterns
  - troubleshooting & error-handling recipes
  - project conventions & architecture knowledge

Each extraction call returns a list of skill dicts ready for
``SkillService.create()``.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from backend.planner.llm_client import LLMClient
from backend.planner.model_manager import TaskType, model_manager
from backend.skills.providers.base import SourceContext

logger = logging.getLogger("nexus.skills.extractor")

# ------------------------------------------------------------------ #
# LLM Prompt
# ------------------------------------------------------------------ #
_EXTRACTION_SYSTEM_PROMPT = """\
You are an expert AI skill extractor. You analyze source code repositories and documentation to extract \
structured, reusable skills for an autonomous browser-automation agent.

You will receive:
1. Repository metadata (name, language, dependencies, architecture)
2. README / documentation content
3. Key source files

Your job: extract REUSABLE SKILLS -- NOT line-by-line code memorization.

Extract these categories of skills:
- **workflow**: Step-by-step procedures, browser/API automation flows
- **api**: API usage patterns, function signatures, client initialization
- **command**: Build, setup, deploy, test CLI command sequences
- **pattern**: Coding patterns, architectural patterns, best practices
- **prompt**: Reusable prompts, agent interaction templates
- **troubleshooting**: Error handling, edge cases, recovery procedures

Respond with STRICT JSON only, no prose, no markdown fences:
{
  "skills": [
    {
      "name": "short descriptive skill name",
      "description": "clear 1-3 sentence description of what this skill does and when to use it",
      "category": "workflow|api|command|pattern|prompt|troubleshooting",
      "trigger": "natural-language phrases that should activate this skill, one per line",
      "tags": ["tag1", "tag2"],
      "language": "python|javascript|etc",
      "file_source": "path/to/relevant/file.py or empty",
      "workflow": [
        {"action": "navigate|click|type|scroll|wait|execute|api_call", "target": "what to act on", \
"value": "parameter value if needed", "description": "what this step does"}
      ],
      "variables": [{"name": "var", "description": "what it's for", "default": ""}],
      "dependencies": ["dep1", "dep2"],
      "example_usage": "short example of how to use this skill",
      "confidence_score": 0.95
    }
  ]
}

Rules:
- Extract 3-15 skills per repository (quality over quantity).
- Each skill must be SELF-CONTAINED and REUSABLE in a different context.
- Use {{variable_name}} placeholders for anything that changes between uses.
- confidence_score: 0.0-1.0 reflecting how confident you are the skill is correct & useful.
- If a file is too small or trivial, skip it -- don't create skills for boilerplate.
- Focus on the UNIQUE value of this repository, not generic programming knowledge.
- For command-type skills, the workflow steps should use action="execute" with target="shell".
"""


# ------------------------------------------------------------------ #
# Extractor
# ------------------------------------------------------------------ #
class SkillExtractor:
    """
    Analyzes a ``SourceContext`` and uses the LLM to produce structured
    skill definitions ready for persistence.
    """

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or model_manager

    async def extract(self, ctx: SourceContext) -> list[dict[str, Any]]:
        """
        Extract skills from a fully-populated ``SourceContext``.

        Returns a list of skill dicts (same shape as CreateSkillRequest).
        """
        user_prompt = self._build_user_prompt(ctx)

        logger.info(
            "Extracting skills from %s/%s (%d files, %s)",
            ctx.owner, ctx.repo, len(ctx.files), ctx.primary_language,
        )

        try:
            result = await self.llm.complete_json(
                _EXTRACTION_SYSTEM_PROMPT,
                user_prompt,
                task_type=TaskType.PLANNING,
            )
        except Exception:
            logger.exception("LLM skill extraction failed for %s/%s", ctx.owner, ctx.repo)
            return []

        raw_skills = result.get("skills", [])
        if not isinstance(raw_skills, list):
            logger.warning("LLM returned non-list 'skills': %s", type(raw_skills))
            return []

        # Normalize and enrich each extracted skill
        skills: list[dict[str, Any]] = []
        for raw in raw_skills:
            try:
                skill = self._normalize_skill(raw, ctx)
                skills.append(skill)
            except Exception:
                logger.debug("Skipping malformed extracted skill: %s", raw)
                continue

        logger.info("Extracted %d skills from %s/%s", len(skills), ctx.owner, ctx.repo)
        return skills

    # ------------------------------------------------------------------ #
    # Prompt building
    # ------------------------------------------------------------------ #
    def _build_user_prompt(self, ctx: SourceContext) -> str:
        """Build the user message containing repo metadata + file contents."""
        parts: list[str] = []

        # Section 1: Repository Metadata
        parts.append(f"## Repository: {ctx.owner}/{ctx.repo}")
        parts.append(f"URL: {ctx.url}")
        parts.append(f"Primary Language: {ctx.primary_language}")
        if ctx.languages:
            parts.append(f"Languages: {', '.join(ctx.languages)}")
        if ctx.dependencies:
            parts.append(f"Dependencies: {', '.join(ctx.dependencies[:30])}")
        if ctx.commit_sha:
            parts.append(f"Commit: {ctx.commit_sha[:12]}")
        parts.append("")

        # Section 2: Architecture overview
        if ctx.architecture_summary:
            parts.append("## Architecture")
            # Truncate to keep prompt manageable
            arch = ctx.architecture_summary[:3000]
            parts.append(arch)
            parts.append("")

        # Section 3: README / Documentation
        if ctx.readme_content:
            parts.append("## README")
            readme = ctx.readme_content[:8000]
            parts.append(readme)
            parts.append("")

        # Section 4: Key source files
        parts.append("## Source Files")

        # Prioritize: README > config/setup files > source code
        priority_files: list[tuple[int, Any]] = []
        for f in ctx.files:
            score = 0
            name_lower = f.relative_path.lower()

            # High priority: entry points, main files, config
            if any(k in name_lower for k in ("main", "app", "index", "server", "cli", "setup", "config")):
                score = 3
            elif any(k in name_lower for k in ("api", "route", "handler", "controller", "service")):
                score = 2
            elif f.language in ("markdown", "restructuredtext"):
                score = 2 if "readme" in name_lower else 1
            elif any(k in name_lower for k in ("test", "spec", "fixture", "mock")):
                score = 0  # tests are lowest priority
            else:
                score = 1

            priority_files.append((score, f))

        # Sort by priority descending, then take the top files that fit
        priority_files.sort(key=lambda x: x[0], reverse=True)

        chars_budget = 80_000  # max chars for file contents in the prompt
        chars_used = sum(len(p) for p in parts)

        for _, f in priority_files:
            if chars_used >= chars_budget:
                break

            content = f.content
            if len(content) > 6000:
                content = content[:6000] + "\n... (truncated)"

            file_block = f"\n### {f.relative_path} ({f.language})\n```{f.language}\n{content}\n```\n"
            chars_used += len(file_block)
            if chars_used > chars_budget:
                break
            parts.append(file_block)

        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    # Normalization
    # ------------------------------------------------------------------ #
    def _normalize_skill(self, raw: dict, ctx: SourceContext) -> dict[str, Any]:
        """
        Clean up and enrich one raw LLM-extracted skill dict into the
        standard shape expected by SkillService.create().
        """
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("Skill has no name")

        # Prefix with repo name for uniqueness
        repo_prefix = f"[{ctx.owner}/{ctx.repo}]"
        if not name.startswith("["):
            name = f"{repo_prefix} {name}"

        description = str(raw.get("description", "")).strip()
        category = str(raw.get("category", "general")).strip()
        trigger = str(raw.get("trigger", "")).strip()

        # Tags
        tags = raw.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if t]

        # File source
        file_source = str(raw.get("file_source", "")).strip()

        # Language
        language = str(raw.get("language", ctx.primary_language)).strip()

        # Workflow
        workflow = raw.get("workflow", [])
        if not isinstance(workflow, list):
            workflow = []
        workflow = [
            {
                "action": str(s.get("action", "execute")).lower(),
                "target": str(s.get("target", "")),
                "value": str(s.get("value", "")),
                "description": str(s.get("description", "")),
            }
            for s in workflow
            if isinstance(s, dict)
        ]

        # Variables
        variables = raw.get("variables", [])
        if not isinstance(variables, list):
            variables = []
        variables = [
            {
                "name": str(v.get("name", "")),
                "description": str(v.get("description", "")),
                "default": str(v.get("default", "")),
            }
            for v in variables
            if isinstance(v, dict) and v.get("name")
        ]

        # Dependencies
        deps = raw.get("dependencies", [])
        if not isinstance(deps, list):
            deps = []

        # Example usage
        example_usage = str(raw.get("example_usage", "")).strip()

        # Confidence
        try:
            confidence = float(raw.get("confidence_score", 0.5))
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            confidence = 0.5

        return {
            "name": name,
            "description": description,
            "category": category,
            "trigger": trigger,
            "tags": tags,
            "language": language,
            "repository": f"{ctx.owner}/{ctx.repo}",
            "file_source": file_source,
            "workflow": workflow,
            "variables": variables,
            "dependencies": deps,
            "example_usage": example_usage,
            "confidence_score": confidence,
            "website_hint": ctx.url,
            # Provenance for deduplication
            "commit_sha": ctx.commit_sha,
            "content_hash": ctx.content_hash,
        }
