import httpx
import pytest

from backend.config.settings import LLMProvider
from backend.planner.llm_client import LLMClient


def test_anthropic_text_request_shape(monkeypatch):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "anthropic_api_key", "sk-test")
    client = LLMClient(provider=LLMProvider.ANTHROPIC, model="claude-x")

    url, headers, body = client._build_anthropic("claude-x", "sys", "hello", 100, None)

    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-test"
    assert body["messages"][0]["content"] == "hello"
    assert body["system"] == "sys"


def test_anthropic_vision_request_embeds_image():
    client = LLMClient(provider=LLMProvider.ANTHROPIC, model="claude-x")
    _url, _headers, body = client._build_anthropic("claude-x", "sys", "look", 100, ("b64data", "image/png"))

    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["data"] == "b64data"
    assert content[1]["text"] == "look"


def test_openai_vs_openrouter_use_different_urls_and_keys(monkeypatch):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "openai_api_key", "openai-key")
    monkeypatch.setattr(settings_module.settings, "openrouter_api_key", "or-key")

    openai_client = LLMClient(provider=LLMProvider.OPENAI, model="gpt-x")
    url, headers, _ = openai_client._build_openai_style("gpt-x", "sys", "hi", 100, None)
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer openai-key"

    or_client = LLMClient(provider=LLMProvider.OPENROUTER, model="anthropic/claude")
    url, headers, _ = or_client._build_openai_style("anthropic/claude", "sys", "hi", 100, None)
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer or-key"


def test_gemini_request_puts_key_in_url_not_body(monkeypatch):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "gemini_api_key", "gem-key")
    client = LLMClient(provider=LLMProvider.GEMINI, model="gemini-x")

    url, _headers, body = client._build_gemini("gemini-x", "sys", "hi", 100, None)

    assert "key=gem-key" in url
    assert "gem-key" not in str(body)
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"


def test_groq_uses_openai_compatible_shape(monkeypatch):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "groq_api_key", "groq-key")
    client = LLMClient(provider=LLMProvider.GROQ, model="llama-3.3-70b-versatile")

    url, headers, body = client._build_openai_compatible("llama-3.3-70b-versatile", "sys", "hi", 100, None)

    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer groq-key"
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_openai_compatible_providers_each_have_a_default_model():
    from backend.planner.llm_client import DEFAULT_MODELS, OPENAI_COMPATIBLE_PROVIDERS

    for provider in OPENAI_COMPATIBLE_PROVIDERS:
        assert provider in DEFAULT_MODELS, f"{provider} is missing a DEFAULT_MODELS entry"


@pytest.mark.asyncio
async def test_dispatch_routes_generic_provider_through_openai_compatible_builder(monkeypatch):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "mistral_api_key", "mistral-key")
    client = LLMClient(provider=LLMProvider.MISTRAL, model="mistral-large-latest")

    async def fake_post(url, headers, json_body):
        assert url == "https://api.mistral.ai/v1/chat/completions"
        return {"choices": [{"message": {"content": "hello from mistral"}}]}

    monkeypatch.setattr(LLMClient, "_post", staticmethod(fake_post))

    result = await client._dispatch("mistral-large-latest", "sys", "hi", 100, None)

    assert result == "hello from mistral"


@pytest.mark.asyncio
async def test_complete_json_parses_markdown_fenced_response(monkeypatch):
    client = LLMClient(provider=LLMProvider.ANTHROPIC, model="claude-x")

    async def fake_complete(system_prompt, user_prompt, max_tokens, image_path, model):
        return '```json\n{"action": "finish"}\n```'

    monkeypatch.setattr(client, "_complete", fake_complete)
    result = await client.complete_json("sys", "user")

    assert result == {"action": "finish"}


def _rate_limit_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test")
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


@pytest.mark.asyncio
async def test_rate_limited_primary_model_falls_back_and_succeeds(monkeypatch):
    from backend.planner import llm_client as llm_client_module

    monkeypatch.setattr(llm_client_module, "RATE_LIMIT_RETRY_DELAYS", [])
    monkeypatch.setattr(
        llm_client_module,
        "FALLBACK_MODELS",
        {LLMProvider.GEMINI: ["gemini-flash-backup"]},
    )

    client = LLMClient(provider=LLMProvider.GEMINI, model="gemini-primary")
    attempted_models: list[str] = []

    async def fake_dispatch(model, system_prompt, user_prompt, max_tokens, image):
        attempted_models.append(model)
        if model == "gemini-primary":
            raise _rate_limit_error()
        return "ok from backup"

    monkeypatch.setattr(client, "_dispatch", fake_dispatch)

    result = await client._complete("sys", "user", 100, image_path=None, model="gemini-primary")

    assert result == "ok from backup"
    assert attempted_models == ["gemini-primary", "gemini-flash-backup"]


@pytest.mark.asyncio
async def test_all_models_rate_limited_raises_original_error(monkeypatch):
    from backend.planner import llm_client as llm_client_module

    monkeypatch.setattr(llm_client_module, "RATE_LIMIT_RETRY_DELAYS", [])
    monkeypatch.setattr(
        llm_client_module,
        "FALLBACK_MODELS",
        {LLMProvider.GEMINI: ["gemini-flash-backup"]},
    )

    client = LLMClient(provider=LLMProvider.GEMINI, model="gemini-primary")

    async def always_rate_limited(model, system_prompt, user_prompt, max_tokens, image):
        raise _rate_limit_error()

    monkeypatch.setattr(client, "_dispatch", always_rate_limited)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client._complete("sys", "user", 100, image_path=None, model="gemini-primary")

    assert exc_info.value.response.status_code == 429


@pytest.mark.asyncio
async def test_non_rate_limit_error_raises_immediately_without_fallback(monkeypatch):
    from backend.planner import llm_client as llm_client_module

    monkeypatch.setattr(llm_client_module, "RATE_LIMIT_RETRY_DELAYS", [])
    monkeypatch.setattr(
        llm_client_module,
        "FALLBACK_MODELS",
        {LLMProvider.GEMINI: ["gemini-flash-backup"]},
    )

    client = LLMClient(provider=LLMProvider.GEMINI, model="gemini-primary")
    attempted_models: list[str] = []

    async def fake_dispatch(model, system_prompt, user_prompt, max_tokens, image):
        attempted_models.append(model)
        request = httpx.Request("POST", "https://example.test")
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(client, "_dispatch", fake_dispatch)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client._complete("sys", "user", 100, image_path=None, model="gemini-primary")

    assert exc_info.value.response.status_code == 400
    # Must not have tried the fallback model for a non-429 error.
    assert attempted_models == ["gemini-primary"]
