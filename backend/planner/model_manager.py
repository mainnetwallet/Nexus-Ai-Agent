"""
AI Model Manager.

Sits above LLMClient (backend/planner/llm_client.py) and owns everything
about *which* provider/model handles a given call:

  - Manual switching (settings.llm_provider / llm_model_override, same
    fields LLMClient already reads -- this module is additive, not a
    replacement).
  - Smart routing: task-type -> provider rules, used when routing_mode is
    "auto" instead of a fixed manual provider.
  - Temporary override: "use Claude for this task only", auto-restored
    after one call.
  - Provider health: status/latency/last-success/last-error per provider.
  - Automatic fallback: if the resolved provider's call fails (timeout,
    HTTP error, rate limit, bad/unparsable response), retry through a
    fallback chain (explicit fallback provider, then configured priority
    order), skipping disabled providers and providers with no API key.

Existing call sites keep working unmodified: LLMClient(provider=None) still
reads settings.llm_provider directly. Callers that want routing/fallback go
through ModelManager.complete_text/complete_json/complete_json_with_image
instead, which pick a provider via `resolve()` and then delegate to
LLMClient for the actual HTTP call.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

import httpx

from backend.config.settings import DATA_DIR, LLMProvider, settings
from backend.planner.llm_client import DEFAULT_MODELS, LLMClient

logger = logging.getLogger("nexus.ai_model_manager")

STATE_PATH = DATA_DIR / "ai_model_manager.json"


class TaskType(str, Enum):
    CODING = "coding"
    BROWSER_AUTOMATION = "browser_automation"
    PLANNING = "planning"
    VISION = "vision"
    LONG_CONTEXT = "long_context"
    FAST_RESPONSE = "fast_response"
    GENERAL_CHAT = "general_chat"
    RESEARCH = "research"
    REASONING = "reasoning"
    LOW_COST = "low_cost"


# Default smart-routing table (mission spec). Overridable at runtime via
# ModelManager.set_routing_rule() / the /api/ai-models/routing-rules API,
# and persisted to STATE_PATH so overrides survive a restart.
DEFAULT_ROUTING_RULES: dict[TaskType, LLMProvider] = {
    TaskType.CODING: LLMProvider.ANTHROPIC,
    TaskType.BROWSER_AUTOMATION: LLMProvider.GEMINI,
    TaskType.PLANNING: LLMProvider.ANTHROPIC,
    TaskType.VISION: LLMProvider.GEMINI,
    TaskType.LONG_CONTEXT: LLMProvider.GEMINI,
    TaskType.FAST_RESPONSE: LLMProvider.GROQ,
    TaskType.GENERAL_CHAT: LLMProvider.OPENAI,
    TaskType.RESEARCH: LLMProvider.OPENROUTER,
    TaskType.REASONING: LLMProvider.ANTHROPIC,
    TaskType.LOW_COST: LLMProvider.OPENROUTER,
}

# Failures that justify trying the fallback chain. Anything else (bad
# request, auth failure with a *fixed* payload, programmer error) would
# fail identically on the fallback provider, so it's raised immediately.
_FALLBACK_TRIGGERING_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.HTTPStatusError,
    ValueError,  # unparsable / invalid JSON response
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _provider_api_key(provider: LLMProvider) -> str:
    attr = {
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
    }.get(provider, "")
    return getattr(settings, attr, "") if attr else ""


@dataclass
class ProviderHealth:
    status: str = "unknown"  # unknown | healthy | degraded | down
    connection_status: str = "untested"  # untested | connected | failed
    latency_ms: Optional[float] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None
    total_requests: int = 0
    total_failures: int = 0
    rate_limited_until: Optional[str] = None

    @property
    def availability(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return round((self.total_requests - self.total_failures) / self.total_requests, 4)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["availability"] = self.availability
        return d


@dataclass
class TemporaryOverride:
    provider: LLMProvider
    model: Optional[str] = None
    reason: str = ""
    created_at: str = field(default_factory=lambda: _now().isoformat())


class ModelManager:
    """
    Process-wide singleton (see `model_manager` at the bottom of this
    module). Holds routing/fallback configuration in memory and mirrors it
    to STATE_PATH so it survives a backend restart -- the same pattern as
    backend/config/config_manager.py uses for settings snapshots.
    """

    def __init__(self) -> None:
        self.routing_mode: str = "auto" if settings.ai_smart_routing_enabled else "manual"
        self.routing_rules: dict[TaskType, LLMProvider] = dict(DEFAULT_ROUTING_RULES)
        self.fallback_provider: LLMProvider = settings.ai_fallback_provider
        self.provider_priority: list[LLMProvider] = self._parse_priority(settings.ai_provider_priority_list)
        self.disabled_providers: set[LLMProvider] = self._parse_providers(settings.ai_disabled_providers_set)
        self.health: dict[LLMProvider, ProviderHealth] = {p: ProviderHealth() for p in LLMProvider}
        self._override: Optional[TemporaryOverride] = None
        self._load_state()

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_priority(raw: list[str]) -> list[LLMProvider]:
        out = []
        for item in raw:
            try:
                out.append(LLMProvider(item))
            except ValueError:
                logger.warning("Ignoring unknown provider in ai_provider_priority: %s", item)
        return out

    @staticmethod
    def _parse_providers(raw: set[str]) -> set[LLMProvider]:
        out = set()
        for item in raw:
            try:
                out.add(LLMProvider(item))
            except ValueError:
                logger.warning("Ignoring unknown provider in ai_disabled_providers: %s", item)
        return out

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load_state(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            data = json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to load AI Model Manager state, starting fresh")
            return

        self.routing_mode = data.get("routing_mode", self.routing_mode)
        if data.get("fallback_provider"):
            try:
                self.fallback_provider = LLMProvider(data["fallback_provider"])
            except ValueError:
                pass
        if data.get("provider_priority"):
            self.provider_priority = self._parse_priority(data["provider_priority"])
        if "disabled_providers" in data:
            self.disabled_providers = self._parse_providers(set(data["disabled_providers"]))
        for task_key, provider_val in (data.get("routing_rules") or {}).items():
            try:
                self.routing_rules[TaskType(task_key)] = LLMProvider(provider_val)
            except ValueError:
                continue

    def _save_state(self) -> None:
        payload = {
            "routing_mode": self.routing_mode,
            "fallback_provider": self.fallback_provider.value,
            "provider_priority": [p.value for p in self.provider_priority],
            "disabled_providers": [p.value for p in self.disabled_providers],
            "routing_rules": {k.value: v.value for k, v in self.routing_rules.items()},
            "saved_at": _now().isoformat(),
        }
        try:
            STATE_PATH.write_text(json.dumps(payload, indent=2))
        except OSError:
            logger.exception("Failed to persist AI Model Manager state")

    # ------------------------------------------------------------------ #
    # Manual switching / default provider
    # ------------------------------------------------------------------ #
    def switch_provider(self, provider: LLMProvider, model: Optional[str] = None) -> None:
        """Manually switch the active provider (and clear any routing override)."""
        settings.llm_provider = provider
        settings.llm_model_override = model or ""
        self._override = None
        logger.info("AI Model Manager: switched to provider=%s model=%s", provider.value, model or "(default)")

    def set_default_provider(self, provider: LLMProvider, model: Optional[str] = None) -> None:
        self.switch_provider(provider, model)

    @property
    def current_provider(self) -> LLMProvider:
        return settings.llm_provider

    @property
    def current_model(self) -> str:
        return settings.llm_model_override or DEFAULT_MODELS.get(settings.llm_provider, "")

    # ------------------------------------------------------------------ #
    # Smart routing
    # ------------------------------------------------------------------ #
    def enable_auto_routing(self, enabled: bool = True) -> None:
        self.routing_mode = "auto" if enabled else "manual"
        self._save_state()

    def set_routing_rule(self, task_type: TaskType, provider: LLMProvider) -> None:
        self.routing_rules[task_type] = provider
        self._save_state()

    def set_routing_rules(self, rules: dict[TaskType, LLMProvider]) -> None:
        self.routing_rules.update(rules)
        self._save_state()

    # ------------------------------------------------------------------ #
    # Temporary override
    # ------------------------------------------------------------------ #
    def use_temporarily(self, provider: LLMProvider, model: Optional[str] = None, reason: str = "") -> None:
        """
        'Use Claude for this task only.' Takes precedence over both manual
        and auto-routing for exactly the next resolved call, then clears
        itself (see resolve()).
        """
        self._override = TemporaryOverride(provider=provider, model=model, reason=reason)
        logger.info("AI Model Manager: temporary override -> %s (%s)", provider.value, reason or "one-off request")

    def clear_override(self) -> None:
        self._override = None

    @property
    def has_active_override(self) -> bool:
        return self._override is not None

    # ------------------------------------------------------------------ #
    # Fallback / priority / enable-disable
    # ------------------------------------------------------------------ #
    def set_fallback_provider(self, provider: LLMProvider) -> None:
        self.fallback_provider = provider
        self._save_state()

    def set_provider_priority(self, providers: list[LLMProvider]) -> None:
        self.provider_priority = providers
        self._save_state()

    def enable_provider(self, provider: LLMProvider) -> None:
        self.disabled_providers.discard(provider)
        self._save_state()

    def disable_provider(self, provider: LLMProvider) -> None:
        self.disabled_providers.add(provider)
        self._save_state()

    def is_available(self, provider: LLMProvider) -> bool:
        """Enabled, has an API key configured, and not currently rate-limited."""
        if provider in self.disabled_providers:
            return False
        if not _provider_api_key(provider):
            return False
        health = self.health[provider]
        if health.rate_limited_until:
            until = dt.datetime.fromisoformat(health.rate_limited_until)
            if _now() < until:
                return False
        return True

    def fallback_chain(self, primary: LLMProvider) -> list[LLMProvider]:
        """Ordered, de-duplicated list: primary, explicit fallback, priority list,
        then any other available provider -- filtered to available ones."""
        ordered = [primary, self.fallback_provider, *self.provider_priority, *list(LLMProvider)]
        seen: set[LLMProvider] = set()
        chain: list[LLMProvider] = []
        for p in ordered:
            if p in seen:
                continue
            seen.add(p)
            if p == primary or self.is_available(p):
                chain.append(p)
        return chain

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #
    def resolve(self, task_type: Optional[TaskType] = None) -> tuple[LLMProvider, Optional[str]]:
        """
        Decides which (provider, model_override) a call should use, in
        priority order: active temporary override > smart routing (if
        enabled and a task_type is given) > manual default provider.
        """
        if self._override is not None:
            return self._override.provider, self._override.model
        if self.routing_mode == "auto" and task_type is not None:
            provider = self.routing_rules.get(task_type, self.current_provider)
            return provider, None
        return self.current_provider, (settings.llm_model_override or None)

    # ------------------------------------------------------------------ #
    # Health tracking
    # ------------------------------------------------------------------ #
    def record_success(self, provider: LLMProvider, latency_ms: float) -> None:
        h = self.health[provider]
        h.total_requests += 1
        h.status = "healthy"
        h.connection_status = "connected"
        h.latency_ms = round(latency_ms, 1)
        h.last_success_at = _now().isoformat()
        h.rate_limited_until = None

    def record_failure(self, provider: LLMProvider, error: Exception) -> None:
        h = self.health[provider]
        h.total_requests += 1
        h.total_failures += 1
        h.last_error = str(error)[:500]
        h.last_error_at = _now().isoformat()
        h.connection_status = "failed"

        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429:
            h.status = "degraded"
            h.rate_limited_until = (_now() + dt.timedelta(seconds=60)).isoformat()
        elif h.availability < 0.5:
            h.status = "down"
        else:
            h.status = "degraded"

    def health_snapshot(self) -> dict[str, Any]:
        return {p.value: h.as_dict() for p, h in self.health.items()}

    # ------------------------------------------------------------------ #
    # High-level calls: resolve provider, dispatch via LLMClient, fall
    # back across providers on failure, track health, auto-clear a
    # temporary override after it's been consumed once.
    # ------------------------------------------------------------------ #
    async def _run_with_fallback(self, method_name: str, task_type: Optional[TaskType], args: tuple, kwargs: dict) -> Any:
        provider, model_override = self.resolve(task_type)
        had_override = self._override is not None
        chain = self.fallback_chain(provider)

        last_exc: Optional[Exception] = None
        for attempt_provider in chain:
            model = model_override if attempt_provider == provider else None
            client = LLMClient(provider=attempt_provider, model=model)
            start = time.monotonic()
            try:
                result = await getattr(client, method_name)(*args, **kwargs)
            except _FALLBACK_TRIGGERING_EXCEPTIONS as exc:
                self.record_failure(attempt_provider, exc)
                last_exc = exc
                logger.warning(
                    "AI Model Manager: provider=%s failed (%s), trying next in fallback chain",
                    attempt_provider.value,
                    exc,
                )
                continue
            else:
                self.record_success(attempt_provider, (time.monotonic() - start) * 1000)
                if had_override:
                    self.clear_override()
                return result

        if had_override:
            self.clear_override()
        assert last_exc is not None
        raise last_exc

    async def complete_text(self, system_prompt: str, user_prompt: str, max_tokens: int = 800, task_type: Optional[TaskType] = None) -> str:
        return await self._run_with_fallback("complete_text", task_type, (system_prompt, user_prompt, max_tokens), {})

    async def complete_json(self, system_prompt: str, user_prompt: str, max_tokens: int = 1500, task_type: Optional[TaskType] = None) -> dict[str, Any]:
        return await self._run_with_fallback("complete_json", task_type, (system_prompt, user_prompt, max_tokens), {})

    async def complete_json_with_image(
        self, system_prompt: str, user_prompt: str, image_path: str, max_tokens: int = 1200
    ) -> dict[str, Any]:
        return await self._run_with_fallback(
            "complete_json_with_image", TaskType.VISION, (system_prompt, user_prompt, image_path, max_tokens), {}
        )

    # ------------------------------------------------------------------ #
    # Connection testing (Settings "Test Provider Connection")
    # ------------------------------------------------------------------ #
    async def test_connection(self, provider: LLMProvider) -> dict[str, Any]:
        if not _provider_api_key(provider):
            return {"provider": provider.value, "ok": False, "error": "No API key configured for this provider"}

        client = LLMClient(provider=provider)
        start = time.monotonic()
        try:
            await client.complete_text("You are a connectivity check.", "Reply with the single word: ok", max_tokens=10)
        except Exception as exc:  # noqa: BLE001
            self.record_failure(provider, exc)
            return {"provider": provider.value, "ok": False, "error": str(exc)[:500]}
        latency_ms = (time.monotonic() - start) * 1000
        self.record_success(provider, latency_ms)
        return {"provider": provider.value, "ok": True, "latency_ms": round(latency_ms, 1)}

    # ------------------------------------------------------------------ #
    # Dashboard/API view
    # ------------------------------------------------------------------ #
    def available_providers(self) -> list[dict[str, Any]]:
        return [
            {
                "provider": p.value,
                "default_model": DEFAULT_MODELS.get(p, ""),
                "has_api_key": bool(_provider_api_key(p)),
                "enabled": p not in self.disabled_providers,
                "health": self.health[p].as_dict(),
            }
            for p in LLMProvider
        ]

    def to_view(self) -> dict[str, Any]:
        return {
            "current_provider": self.current_provider.value,
            "current_model": self.current_model,
            "routing_mode": self.routing_mode,
            "fallback_provider": self.fallback_provider.value,
            "provider_priority": [p.value for p in self.provider_priority],
            "disabled_providers": [p.value for p in self.disabled_providers],
            "routing_rules": {k.value: v.value for k, v in self.routing_rules.items()},
            "temporary_override": (
                {"provider": self._override.provider.value, "model": self._override.model, "reason": self._override.reason}
                if self._override
                else None
            ),
            "providers": self.available_providers(),
        }


# Free-text aliases -> LLMProvider, used by ChatEngine's natural-language
# "switch to Claude" / "use Gemini for browser tasks" command handling.
PROVIDER_ALIASES: dict[str, LLMProvider] = {
    "claude": LLMProvider.ANTHROPIC,
    "anthropic": LLMProvider.ANTHROPIC,
    "gpt": LLMProvider.OPENAI,
    "chatgpt": LLMProvider.OPENAI,
    "openai": LLMProvider.OPENAI,
    "gemini": LLMProvider.GEMINI,
    "google": LLMProvider.GEMINI,
    "grok": LLMProvider.XAI,
    "xai": LLMProvider.XAI,
    "kimi": LLMProvider.MOONSHOT,
    "moonshot": LLMProvider.MOONSHOT,
    "qwen": LLMProvider.QWEN,
    "alibaba": LLMProvider.QWEN,
    "glm": LLMProvider.ZHIPU,
    "zhipu": LLMProvider.ZHIPU,
    "openrouter": LLMProvider.OPENROUTER,
    "groq": LLMProvider.GROQ,
    "cerebras": LLMProvider.CEREBRAS,
    "cohere": LLMProvider.COHERE,
    "huggingface": LLMProvider.HUGGINGFACE,
    "hugging face": LLMProvider.HUGGINGFACE,
    "hf": LLMProvider.HUGGINGFACE,
    "nvidia": LLMProvider.NVIDIA_NIM,
    "nim": LLMProvider.NVIDIA_NIM,
    "sambanova": LLMProvider.SAMBANOVA,
    "together": LLMProvider.TOGETHER,
    "fireworks": LLMProvider.FIREWORKS,
    "deepinfra": LLMProvider.DEEPINFRA,
    "mistral": LLMProvider.MISTRAL,
    "replicate": LLMProvider.REPLICATE,
    "ai21": LLMProvider.AI21,
}


def parse_provider_name(text: str) -> Optional[LLMProvider]:
    """Best-effort free-text -> LLMProvider match, e.g. 'switch to claude' -> ANTHROPIC."""
    lowered = text.lower()
    for enum_value in LLMProvider:
        if enum_value.value.replace("_", " ") in lowered or enum_value.value in lowered:
            return enum_value
    for alias, provider in PROVIDER_ALIASES.items():
        if alias in lowered:
            return provider
    return None


_TASK_TYPE_ALIASES: dict[str, TaskType] = {
    "coding": TaskType.CODING,
    "code": TaskType.CODING,
    "browser": TaskType.BROWSER_AUTOMATION,
    "browsing": TaskType.BROWSER_AUTOMATION,
    "automation": TaskType.BROWSER_AUTOMATION,
    "planning": TaskType.PLANNING,
    "plan": TaskType.PLANNING,
    "vision": TaskType.VISION,
    "image": TaskType.VISION,
    "long context": TaskType.LONG_CONTEXT,
    "long-context": TaskType.LONG_CONTEXT,
    "fast": TaskType.FAST_RESPONSE,
    "speed": TaskType.FAST_RESPONSE,
    "chat": TaskType.GENERAL_CHAT,
    "general": TaskType.GENERAL_CHAT,
    "research": TaskType.RESEARCH,
    "reasoning": TaskType.REASONING,
    "reason": TaskType.REASONING,
    "low cost": TaskType.LOW_COST,
    "cheap": TaskType.LOW_COST,
    "cost": TaskType.LOW_COST,
}


def parse_task_type(text: str) -> Optional[TaskType]:
    lowered = text.lower()
    for alias, task_type in _TASK_TYPE_ALIASES.items():
        if alias in lowered:
            return task_type
    return None


# Process-wide singleton, mirroring the `settings = Settings()` pattern in
# backend/config/settings.py.
model_manager = ModelManager()
