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


@pytest.mark.asyncio
async def test_complete_json_parses_markdown_fenced_response(monkeypatch):
    client = LLMClient(provider=LLMProvider.ANTHROPIC, model="claude-x")

    async def fake_complete(system_prompt, user_prompt, max_tokens, image_path, model):
        return '```json\n{"action": "finish"}\n```'

    monkeypatch.setattr(client, "_complete", fake_complete)
    result = await client.complete_json("sys", "user")

    assert result == {"action": "finish"}
