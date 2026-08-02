"""
Thin, unified client over multiple LLM providers so the planner can switch
models from Settings without touching business logic.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx

from backend.config.settings import settings, LLMProvider

logger = logging.getLogger("nexus.llm")

DEFAULT_MODELS = {
    LLMProvider.ANTHROPIC: "claude-sonnet-4-6",
    LLMProvider.OPENAI: "gpt-4.1",
    LLMProvider.GEMINI: "gemini-2.5-pro",
    LLMProvider.OPENROUTER: "anthropic/claude-sonnet-4.6",
}

# All current default models above are already vision-capable, so the same
# model id is reused for image calls unless vision_model_override is set.
DEFAULT_VISION_MODELS = dict(DEFAULT_MODELS)


def _image_to_data_url(image_path: str) -> tuple[str, str]:
    """Returns (base64_data, mime_type) for a local image file."""
    mime_type = mimetypes.guess_type(image_path)[0] or "image/png"
    data = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    return data, mime_type


class LLMClient:
    """
    Unified client. Each provider is implemented as one "build request"
    method (returns url/headers/json body, aware of whether an image is
    attached) plus one "extract text" method for its response shape. Both
    complete_json() and complete_json_with_image() share the same dispatch
    -> post -> extract -> parse-JSON pipeline, so adding or fixing a
    provider only touches its one build/extract pair instead of four
    near-duplicate call methods.
    """

    def __init__(self, provider: LLMProvider | None = None, model: str | None = None) -> None:
        self.provider = provider or settings.llm_provider
        self.model = model or settings.llm_model_override or DEFAULT_MODELS[self.provider]
        self.vision_model = settings.vision_model_override or DEFAULT_VISION_MODELS[self.provider]

    # ------------------------------------------------------------------ #
    # Public API (unchanged signatures -- callers are unaffected)
    # ------------------------------------------------------------------ #
    async def complete_json(self, system_prompt: str, user_prompt: str, max_tokens: int = 1500) -> dict[str, Any]:
        """
        Sends a prompt that requests a strict JSON response and parses it.
        Raises ValueError if the provider returns something unparsable.
        """
        raw = await self._complete(system_prompt, user_prompt, max_tokens, image_path=None, model=self.model)
        return self._parse_json(raw, context="Planner")

    async def complete_json_with_image(
        self,
        system_prompt: str,
        user_prompt: str,
        image_path: str,
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        raw = await self._complete(system_prompt, user_prompt, max_tokens, image_path=image_path, model=self.vision_model)
        return self._parse_json(raw, context="Vision")

    # ------------------------------------------------------------------ #
    # Shared pipeline
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_json(raw: str, context: str) -> dict[str, Any]:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error("%s LLM returned non-JSON: %s", context, raw[:500])
            raise ValueError(f"{context} LLM did not return valid JSON: {exc}") from exc

    async def _complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        image_path: Optional[str],
        model: str,
    ) -> str:
        image = _image_to_data_url(image_path) if image_path else None

        if self.provider == LLMProvider.ANTHROPIC:
            url, headers, body = self._build_anthropic(model, system_prompt, user_prompt, max_tokens, image)
            payload = await self._post(url, headers, body)
            return "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")

        if self.provider in (LLMProvider.OPENAI, LLMProvider.OPENROUTER):
            url, headers, body = self._build_openai_style(model, system_prompt, user_prompt, max_tokens, image)
            payload = await self._post(url, headers, body)
            return payload["choices"][0]["message"]["content"]

        if self.provider == LLMProvider.GEMINI:
            url, headers, body = self._build_gemini(model, system_prompt, user_prompt, max_tokens, image)
            payload = await self._post(url, headers, body)
            return payload["candidates"][0]["content"]["parts"][0]["text"]

        raise ValueError(f"Unsupported provider {self.provider}")

    @staticmethod
    async def _post(url: str, headers: dict[str, str], json_body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=json_body)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------ #
    # Per-provider request builders (text and vision share these -- the
    # only difference is whether `image` is None)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_anthropic(
        model: str, system_prompt: str, user_prompt: str, max_tokens: int, image: Optional[tuple[str, str]]
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        if image:
            data, mime_type = image
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": data}},
                {"type": "text", "text": user_prompt},
            ]
        else:
            content = user_prompt
        return (
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": content}],
            },
        )

    def _build_openai_style(
        self, model: str, system_prompt: str, user_prompt: str, max_tokens: int, image: Optional[tuple[str, str]]
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Shared by OpenAI and OpenRouter -- both use the OpenAI chat/completions shape."""
        if image:
            data, mime_type = image
            user_content: Any = [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}},
            ]
        else:
            user_content = user_prompt

        if self.provider == LLMProvider.OPENROUTER:
            url = "https://openrouter.ai/api/v1/chat/completions"
            api_key = settings.openrouter_api_key
        else:
            url = "https://api.openai.com/v1/chat/completions"
            api_key = settings.openai_api_key

        return (
            url,
            {"Authorization": f"Bearer {api_key}"},
            {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            },
        )

    @staticmethod
    def _build_gemini(
        model: str, system_prompt: str, user_prompt: str, max_tokens: int, image: Optional[tuple[str, str]]
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        parts: list[dict[str, Any]] = [{"text": user_prompt}]
        if image:
            data, mime_type = image
            parts.append({"inline_data": {"mime_type": mime_type, "data": data}})
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={quote(settings.gemini_api_key)}",
            {},
            {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"parts": parts}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
        )
