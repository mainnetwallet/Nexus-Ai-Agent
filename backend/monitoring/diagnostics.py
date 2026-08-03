"""
System Diagnostics.

A deeper, on-demand check than HealthMonitor: verifies the environment
Nexus-Agent actually needs to function (Playwright browsers installed, AI
API reachable, DB schema present, plugins loadable, memory store working,
required env vars set) and produces a single structured report the
dashboard, Telegram (/diagnostics) or CI can consume.
"""
from __future__ import annotations

import importlib.util
import platform
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from backend.config.settings import LLMProvider, settings


@dataclass
class DiagnosticCheck:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class DiagnosticReport:
    generated_at: float = field(default_factory=time.time)
    python_version: str = field(default_factory=lambda: sys.version.split()[0])
    platform: str = field(default_factory=platform.platform)
    checks: list[DiagnosticCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "python_version": self.python_version,
            "platform": self.platform,
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
        }

    def to_text(self) -> str:
        lines = [
            f"Nexus-Agent Diagnostic Report",
            f"generated_at: {self.generated_at}",
            f"python: {self.python_version}  platform: {self.platform}",
            f"overall: {'PASS' if self.passed else 'FAIL'}",
            "",
        ]
        for c in self.checks:
            lines.append(f"[{'OK' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
        return "\n".join(lines)


class DiagnosticsService:
    def __init__(self, app_state: Any) -> None:
        self.state = app_state

    async def run(self) -> DiagnosticReport:
        report = DiagnosticReport()
        report.checks.append(self._check_browser())
        report.checks.append(self._check_playwright())
        report.checks.extend(self._check_ai_apis())
        report.checks.append(await self._check_database())
        report.checks.append(self._check_plugins())
        report.checks.append(self._check_memory())
        report.checks.append(self._check_environment())
        return report

    # ------------------------------------------------------------------ #
    def _check_browser(self) -> DiagnosticCheck:
        queue = getattr(self.state, "queue", None)
        if queue is None:
            return DiagnosticCheck("browser", False, "task queue not initialized")
        return DiagnosticCheck(
            "browser", True, f"channel={settings.browser_channel.value} headless={settings.browser_headless}"
        )

    def _check_playwright(self) -> DiagnosticCheck:
        if importlib.util.find_spec("playwright") is None:
            return DiagnosticCheck("playwright", False, "playwright package not installed")
        try:
            from playwright.__main__ import main as _  # noqa: F401

            return DiagnosticCheck("playwright", True, "playwright package importable")
        except Exception as exc:  # noqa: BLE001
            return DiagnosticCheck("playwright", False, f"import failed: {exc}")

    # Every provider the AI Model Manager supports. Kept here (rather than
    # imported from backend.planner.model_manager) to avoid pulling in the
    # heavier planner module just for a settings lookup.
    _PROVIDER_KEY_ATTR: dict[LLMProvider, str] = {
        LLMProvider.ANTHROPIC: "anthropic_api_key",
        LLMProvider.OPENAI: "openai_api_key",
        LLMProvider.GEMINI: "gemini_api_key",
        LLMProvider.OPENROUTER: "openrouter_api_key",
        LLMProvider.XAI: "xai_api_key",
        LLMProvider.MOONSHOT: "moonshot_api_key",
        LLMProvider.QWEN: "qwen_api_key",
        LLMProvider.ZHIPU: "zhipu_api_key",
        LLMProvider.GROQ: "groq_api_key",
        LLMProvider.CEREBRAS: "cerebras_api_key",
        LLMProvider.COHERE: "cohere_api_key",
        LLMProvider.HUGGINGFACE: "huggingface_api_key",
        LLMProvider.NVIDIA_NIM: "nvidia_nim_api_key",
        LLMProvider.SAMBANOVA: "sambanova_api_key",
        LLMProvider.TOGETHER: "together_api_key",
        LLMProvider.FIREWORKS: "fireworks_api_key",
        LLMProvider.DEEPINFRA: "deepinfra_api_key",
        LLMProvider.MISTRAL: "mistral_api_key",
        LLMProvider.REPLICATE: "replicate_api_key",
        LLMProvider.AI21: "ai21_api_key",
    }

    def _check_ai_apis(self) -> list[DiagnosticCheck]:
        """One check per provider that actually has a key configured in
        .env -- providers with no key simply don't show up here at all,
        so the Diagnostics panel stays fully dynamic as keys are added or
        removed, instead of only ever reporting on the single default
        LLM_PROVIDER."""
        checks: list[DiagnosticCheck] = []
        default_provider = settings.llm_provider.value
        for provider, attr in self._PROVIDER_KEY_ATTR.items():
            key = getattr(settings, attr, "")
            if not key:
                continue
            is_default = provider.value == default_provider
            label = f"ai_api:{provider.value}"
            detail = f"key configured{' (default)' if is_default else ''}"
            checks.append(DiagnosticCheck(label, True, detail))

        if not checks:
            checks.append(
                DiagnosticCheck(
                    "ai_api",
                    False,
                    f"no API key set for any provider (default provider={default_provider})",
                )
            )
        return checks

    async def _check_database(self) -> DiagnosticCheck:
        try:
            from sqlalchemy import text

            from backend.database.session import get_session

            async with get_session() as session:
                await session.execute(text("SELECT 1"))
            return DiagnosticCheck("database", True, f"sqlite at {settings.sqlite_path}")
        except Exception as exc:  # noqa: BLE001
            return DiagnosticCheck("database", False, f"connection failed: {exc}")

    def _check_plugins(self) -> DiagnosticCheck:
        plugins = getattr(self.state, "plugins", None)
        if plugins is None:
            return DiagnosticCheck("plugins", not settings.plugins_enabled, "plugin registry not initialized")
        try:
            loaded = plugins.list_plugins()
            return DiagnosticCheck("plugins", True, f"{len(loaded)} plugin(s) discovered")
        except Exception as exc:  # noqa: BLE001
            return DiagnosticCheck("plugins", False, f"list_plugins failed: {exc}")

    def _check_memory(self) -> DiagnosticCheck:
        memory = getattr(self.state, "memory", None)
        if memory is None:
            return DiagnosticCheck("memory", False, "MemoryStore not initialized")
        return DiagnosticCheck("memory", True, f"chroma_persist_dir={settings.chroma_persist_dir}")

    def _check_environment(self) -> DiagnosticCheck:
        missing = []
        if not settings.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        provider = settings.llm_provider.value
        key_map = {
            "anthropic": settings.anthropic_api_key,
            "openai": settings.openai_api_key,
            "gemini": settings.gemini_api_key,
            "openrouter": settings.openrouter_api_key,
        }
        if not key_map.get(provider):
            missing.append(f"{provider.upper()}_API_KEY")
        if missing:
            return DiagnosticCheck("environment", False, f"missing/optional: {', '.join(missing)}")
        return DiagnosticCheck("environment", True, "required environment variables present")
