"""
SkillRunner -- deterministically replays a Skill's stored `workflow`
(a list of {action, target, value, description} steps) against a live
BrowserEngine, the same primitives AgentLoop._execute_action uses, so a
learned skill behaves identically to the equivalent hand-planned steps.

Deliberately dumb and fast: no LLM calls, no perception fallback. If a step
fails, the run stops immediately with status "failed" so the caller (see
backend/planner/task_queue.py) can fall back to normal AgentLoop planning
for that task instead of half-executing a stale skill.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from backend.browser.engine import BrowserEngine
from backend.planner.agent_loop import StepAction, StepResult, TaskOutcome

logger = logging.getLogger("nexus.skills.runner")

_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def _substitute(text: str, variables: dict[str, str]) -> str:
    if not text:
        return text

    def _replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        return str(variables.get(key, match.group(0)))

    return _VAR_PATTERN.sub(_replace, text)


class SkillRunner:
    def __init__(self, engine: BrowserEngine, mcp: Optional[Any] = None) -> None:
        self.engine = engine
        self.mcp = mcp  # optional MCPManager; StepAction.MCP_TOOL is a no-op failure if None

    async def run(
        self,
        skill: dict[str, Any],
        website: str,
        variables: Optional[dict[str, str]] = None,
        on_step: Optional[Any] = None,
    ) -> TaskOutcome:
        outcome = TaskOutcome(status="failed")
        resolved_vars = self._resolve_variables(skill, variables or {})

        workflow = skill.get("workflow") or []
        if not workflow:
            outcome.summary = "Skill has an empty workflow."
            outcome.finished_at = time.time()
            return outcome

        if website:
            try:
                await self.engine.navigate(website)
            except Exception as exc:  # noqa: BLE001
                outcome.summary = f"Skill replay could not navigate to {website}: {exc}"
                outcome.finished_at = time.time()
                return outcome

        for index, step in enumerate(workflow):
            action = (step.get("action") or "").lower()
            target = _substitute(step.get("target", ""), resolved_vars)
            value = _substitute(step.get("value", ""), resolved_vars)

            success, step_note = await self._execute_action(action, target, value)
            shot = ""
            try:
                shot = await self.engine.screenshot(name_hint=f"skill_step{index}")
            except Exception:
                pass

            step_result = StepResult(
                index=index,
                action=action,
                target=target,
                value=value,
                reasoning=step.get("description", "") or f"Replaying learned skill '{skill.get('name', '')}'",
                success=success,
                screenshot_path=shot,
                note=step_note,
            )
            outcome.steps.append(step_result)
            if on_step:
                await on_step(step_result)

            if not success:
                outcome.status = "failed"
                outcome.summary = (
                    f"Skill '{skill.get('name', '')}' failed to replay at step {index} "
                    f"({action} -> {target!r}); falling back to standard planning."
                )
                outcome.finished_at = time.time()
                return outcome

        outcome.status = "succeeded"
        outcome.summary = f"Completed via learned skill '{skill.get('name', '')}'."
        outcome.finished_at = time.time()
        return outcome

    @staticmethod
    def _resolve_variables(skill: dict[str, Any], overrides: dict[str, str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for var in skill.get("variables") or []:
            name = var.get("name")
            if not name:
                continue
            resolved[name] = str(var.get("default", ""))
        resolved.update({k: str(v) for k, v in overrides.items()})
        return resolved

    async def _execute_action(self, action: str, target: str, value: str) -> tuple[bool, str]:
        try:
            if action == StepAction.CLICK.value:
                return await self.engine.smart_click(target), ""
            if action == StepAction.TYPE.value:
                return await self.engine.smart_type(target, value), ""
            if action == StepAction.NAVIGATE.value:
                await self.engine.navigate(value or target)
                return True, ""
            if action == StepAction.SCROLL.value:
                await self.engine.smart_scroll(direction=value or "down")
                return True, ""
            if action == StepAction.WAIT.value:
                await self.engine.smart_wait()
                return True, ""
            if action == StepAction.UPLOAD.value:
                return await self.engine.upload_file(target, value), ""
            if action == StepAction.MCP_TOOL.value:
                return await self._execute_mcp_tool(target, value)
            logger.warning("Unknown action in skill workflow: %s", action)
            return False, ""
        except Exception:
            logger.exception("Skill step execution failed: action=%s target=%s", action, target)
            return False, ""

    async def _execute_mcp_tool(self, target: str, value: str) -> tuple[bool, str]:
        if self.mcp is None:
            return False, "mcp_tool step requested but no MCPManager is configured"

        try:
            arguments = json.loads(value) if value else {}
            if not isinstance(arguments, dict):
                arguments = {}
        except (json.JSONDecodeError, TypeError):
            arguments = {}

        connector, _, tool = (target or "").partition(".")
        if connector and tool:
            result = await self.mcp.call(connector.strip(), tool.strip(), arguments)
        else:
            result = await self.mcp.route_and_call(target or "", arguments=arguments)
            if result is None:
                return False, f"no MCP tool matched skill step request: {target!r}"

        note = f"mcp[{result.connector}.{result.tool}]: {result.output if result.ok else result.error}"
        return result.ok, note
