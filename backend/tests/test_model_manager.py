import httpx
import pytest

from backend.config.settings import LLMProvider
from backend.planner.model_manager import ModelManager, TaskType, parse_provider_name, parse_task_type


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    from backend.planner import model_manager as mm_module

    monkeypatch.setattr(mm_module, "STATE_PATH", tmp_path / "ai_model_manager.json")
    return ModelManager()


def _rate_limit_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test")
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


# ---------------------------------------------------------------------- #
# Manual switching
# ---------------------------------------------------------------------- #
def test_switch_provider_updates_settings(manager, monkeypatch):
    from backend.config import settings as settings_module

    manager.switch_provider(LLMProvider.GROQ, "llama-x")

    assert settings_module.settings.llm_provider == LLMProvider.GROQ
    assert settings_module.settings.llm_model_override == "llama-x"
    assert manager.current_provider == LLMProvider.GROQ
    assert manager.current_model == "llama-x"


def test_switch_provider_clears_active_override(manager):
    manager.use_temporarily(LLMProvider.OPENAI)
    assert manager.has_active_override

    manager.switch_provider(LLMProvider.GEMINI)
    assert not manager.has_active_override


# ---------------------------------------------------------------------- #
# Smart routing resolution
# ---------------------------------------------------------------------- #
def test_resolve_manual_mode_ignores_task_type(manager):
    manager.routing_mode = "manual"
    manager.switch_provider(LLMProvider.ANTHROPIC)

    provider, model = manager.resolve(TaskType.FAST_RESPONSE)

    assert provider == LLMProvider.ANTHROPIC


def test_resolve_auto_mode_uses_routing_rule(manager):
    manager.enable_auto_routing(True)
    manager.set_routing_rule(TaskType.FAST_RESPONSE, LLMProvider.GROQ)

    provider, _model = manager.resolve(TaskType.FAST_RESPONSE)

    assert provider == LLMProvider.GROQ


def test_resolve_prefers_temporary_override_over_auto_routing(manager):
    manager.enable_auto_routing(True)
    manager.set_routing_rule(TaskType.CODING, LLMProvider.ANTHROPIC)
    manager.use_temporarily(LLMProvider.GEMINI, reason="one-off")

    provider, _model = manager.resolve(TaskType.CODING)

    assert provider == LLMProvider.GEMINI


# ---------------------------------------------------------------------- #
# Fallback chain
# ---------------------------------------------------------------------- #
def test_fallback_chain_skips_disabled_and_keyless_providers(manager, monkeypatch):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings_module.settings, "groq_api_key", "")  # no key -> excluded

    manager.set_fallback_provider(LLMProvider.OPENROUTER)
    manager.disable_provider(LLMProvider.GEMINI)

    chain = manager.fallback_chain(LLMProvider.ANTHROPIC)

    assert chain[0] == LLMProvider.ANTHROPIC  # primary always included even without a key
    assert LLMProvider.OPENROUTER in chain
    assert LLMProvider.GEMINI not in chain
    assert LLMProvider.GROQ not in chain


def test_is_available_respects_rate_limit_window(manager, monkeypatch):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "groq_api_key", "groq-key")
    assert manager.is_available(LLMProvider.GROQ)

    manager.record_failure(LLMProvider.GROQ, _rate_limit_error())
    assert not manager.is_available(LLMProvider.GROQ)


# ---------------------------------------------------------------------- #
# Health tracking
# ---------------------------------------------------------------------- #
def test_record_success_and_failure_update_health(manager):
    manager.record_success(LLMProvider.ANTHROPIC, 123.4)
    h = manager.health[LLMProvider.ANTHROPIC]
    assert h.status == "healthy"
    assert h.total_requests == 1
    assert h.availability == 1.0

    manager.record_failure(LLMProvider.ANTHROPIC, ValueError("bad json"))
    h = manager.health[LLMProvider.ANTHROPIC]
    assert h.total_requests == 2
    assert h.total_failures == 1
    assert h.last_error == "bad json"


# ---------------------------------------------------------------------- #
# complete_* fallback across providers
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_complete_text_falls_back_to_next_provider_on_failure(manager, monkeypatch):
    from backend.config import settings as settings_module
    from backend.planner import llm_client as llm_client_module

    monkeypatch.setattr(settings_module.settings, "openrouter_api_key", "or-key")
    manager.switch_provider(LLMProvider.ANTHROPIC)
    manager.set_fallback_provider(LLMProvider.OPENROUTER)

    calls = []

    async def fake_complete_text(self, system_prompt, user_prompt, max_tokens=800):
        calls.append(self.provider)
        if self.provider == LLMProvider.ANTHROPIC:
            raise _rate_limit_error()
        return "ok from openrouter"

    monkeypatch.setattr(llm_client_module.LLMClient, "complete_text", fake_complete_text)

    result = await manager.complete_text("sys", "hi")

    assert result == "ok from openrouter"
    assert calls == [LLMProvider.ANTHROPIC, LLMProvider.OPENROUTER]
    assert manager.health[LLMProvider.ANTHROPIC].total_failures == 1
    assert manager.health[LLMProvider.OPENROUTER].total_requests == 1


@pytest.mark.asyncio
async def test_complete_text_clears_override_after_use(manager, monkeypatch):
    from backend.planner import llm_client as llm_client_module

    async def fake_complete_text(self, system_prompt, user_prompt, max_tokens=800):
        return "ok"

    monkeypatch.setattr(llm_client_module.LLMClient, "complete_text", fake_complete_text)

    manager.use_temporarily(LLMProvider.GEMINI, reason="test")
    assert manager.has_active_override

    result = await manager.complete_text("sys", "hi")

    assert result == "ok"
    assert not manager.has_active_override


# ---------------------------------------------------------------------- #
# Free-text parsing (chat commands)
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("switch to Claude", LLMProvider.ANTHROPIC),
        ("use GPT for this", LLMProvider.OPENAI),
        ("switch to gemini", LLMProvider.GEMINI),
        ("use groq for fast responses", LLMProvider.GROQ),
        ("set openrouter as default", LLMProvider.OPENROUTER),
        ("use cohere", LLMProvider.COHERE),
        ("switch to hugging face", LLMProvider.HUGGINGFACE),
        ("use grok", LLMProvider.XAI),
        ("switch to kimi", LLMProvider.MOONSHOT),
    ],
)
def test_parse_provider_name(text, expected):
    assert parse_provider_name(text) == expected


def test_parse_provider_name_no_match():
    assert parse_provider_name("do the laundry") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("use Claude for coding", TaskType.CODING),
        ("route browser tasks to Gemini", TaskType.BROWSER_AUTOMATION),
        ("use Groq for fast responses", TaskType.FAST_RESPONSE),
        ("cheapest option", TaskType.LOW_COST),
    ],
)
def test_parse_task_type(text, expected):
    assert parse_task_type(text) == expected
