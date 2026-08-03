"""
Settings API.

Exposes the subset of runtime configuration that is safe to show and edit
from the dashboard. Secrets (API keys, auth token, Telegram token) are never
returned or accepted here -- those stay in .env / the process environment.

Updates ARE persisted to the project's .env file (see _persist_to_env_file)
in addition to being applied in-memory for the current process -- otherwise
every value edited from this page reverted the moment the backend was
restarted (e.g. re-running the terminal command), which was confusing since
the dashboard gave no indication the change was temporary.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.auth import require_auth
from backend.config.settings import BASE_DIR, settings

logger = logging.getLogger("nexus.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_auth)])


class SettingsView(BaseModel):
    app_name: str
    environment: str
    debug: bool
    llm_provider: str
    llm_model_override: str
    # AI Model Manager basics -- full control surface (routing rules,
    # fallback, per-provider health, connection testing) lives at
    # /api/ai-models; these three mirror it here so the existing Settings
    # page can show/edit them without a second round trip.
    ai_smart_routing_enabled: bool
    ai_fallback_provider: str
    browser_channel: str
    browser_headless: bool
    browser_slow_mo_ms: int
    browser_default_timeout_ms: int
    wallet_require_manual_approval: bool
    wallet_max_auto_approve_value_usd: float
    wallet_allowlisted_contracts: str
    vision_enabled: bool
    vision_min_elements_threshold: int
    ocr_enabled: bool
    ocr_lang: str
    live_session_enabled: bool
    live_session_interval_ms: int
    live_session_jpeg_quality: int


class SettingsUpdateRequest(BaseModel):
    llm_provider: str | None = None
    llm_model_override: str | None = None
    ai_smart_routing_enabled: bool | None = None
    ai_fallback_provider: str | None = None
    browser_headless: bool | None = None
    browser_slow_mo_ms: int | None = None
    browser_default_timeout_ms: int | None = None
    wallet_require_manual_approval: bool | None = None
    wallet_max_auto_approve_value_usd: float | None = None
    wallet_allowlisted_contracts: str | None = None
    vision_enabled: bool | None = None
    vision_min_elements_threshold: int | None = None
    ocr_enabled: bool | None = None
    ocr_lang: str | None = None
    live_session_enabled: bool | None = None
    live_session_interval_ms: int | None = None
    live_session_jpeg_quality: int | None = None


def _to_view() -> SettingsView:
    from backend.planner.model_manager import model_manager

    return SettingsView(
        app_name=settings.app_name,
        environment=settings.environment,
        debug=settings.debug,
        llm_provider=settings.llm_provider.value,
        llm_model_override=settings.llm_model_override,
        ai_smart_routing_enabled=model_manager.routing_mode == "auto",
        ai_fallback_provider=model_manager.fallback_provider.value,
        browser_channel=settings.browser_channel.value,
        browser_headless=settings.browser_headless,
        browser_slow_mo_ms=settings.browser_slow_mo_ms,
        browser_default_timeout_ms=settings.browser_default_timeout_ms,
        wallet_require_manual_approval=settings.wallet_require_manual_approval,
        wallet_max_auto_approve_value_usd=settings.wallet_max_auto_approve_value_usd,
        wallet_allowlisted_contracts=settings.wallet_allowlisted_contracts,
        vision_enabled=settings.vision_enabled,
        vision_min_elements_threshold=settings.vision_min_elements_threshold,
        ocr_enabled=settings.ocr_enabled,
        ocr_lang=settings.ocr_lang,
        live_session_enabled=settings.live_session_enabled,
        live_session_interval_ms=settings.live_session_interval_ms,
        live_session_jpeg_quality=settings.live_session_jpeg_quality,
    )


@router.get("", response_model=SettingsView)
async def get_settings():
    return _to_view()


def _persist_to_env_file(updates: dict) -> None:
    """
    Writes each updated field to the project's .env file (KEY=VALUE, one per
    line, matching Settings' case_sensitive=False / uppercase env var
    convention) so the change survives the next `python -m backend.main` /
    terminal restart instead of silently reverting to whatever .env already
    had. Only touches keys that were actually updated -- every other line in
    the file (API keys, tokens, unrelated config) is left exactly as-is.
    Best-effort: a write failure is logged, not raised, since the in-memory
    setting for the *current* process has already been applied successfully
    regardless.
    """
    if not updates:
        return
    env_path = BASE_DIR / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    except OSError:
        logger.exception("Failed to read .env for settings persistence")
        return

    remaining = {key.upper(): value for key, value in updates.items()}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        env_key = stripped.split("=", 1)[0].strip()
        if env_key in remaining:
            lines[i] = f"{env_key}={remaining.pop(env_key)}"

    for env_key, value in remaining.items():
        lines.append(f"{env_key}={value}")

    try:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        logger.exception("Failed to write .env for settings persistence")


@router.patch("", response_model=SettingsView)
async def update_settings(req: SettingsUpdateRequest):
    """
    Applies settings in-memory for this process AND persists them to .env
    (see _persist_to_env_file) so a later backend restart picks up the same
    values instead of falling back to whatever .env had before. Editing
    secrets/infra values (ports, DB paths, API keys) from the dashboard
    stays out of scope -- only the fields in SettingsUpdateRequest are ever
    touched.
    """
    from backend.config.settings import LLMProvider
    from backend.planner.model_manager import model_manager

    updates = req.model_dump(exclude_unset=True, exclude_none=True)
    env_updates = dict(updates)

    if "llm_provider" in updates or "llm_model_override" in updates:
        provider = LLMProvider(updates.pop("llm_provider")) if "llm_provider" in updates else settings.llm_provider
        model = updates.pop("llm_model_override", None)
        model_manager.switch_provider(provider, model or None)

    if "ai_smart_routing_enabled" in updates:
        model_manager.enable_auto_routing(updates.pop("ai_smart_routing_enabled"))

    if "ai_fallback_provider" in updates:
        model_manager.set_fallback_provider(LLMProvider(updates.pop("ai_fallback_provider")))

    for key, value in updates.items():
        setattr(settings, key, value)

    _persist_to_env_file(env_updates)
    return _to_view()
