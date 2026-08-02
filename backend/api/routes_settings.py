"""
Settings API.

Exposes the subset of runtime configuration that is safe to show and edit
from the dashboard. Secrets (API keys, auth token, Telegram token) are never
returned or accepted here -- those stay in .env / the process environment.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.auth import require_auth
from backend.config.settings import settings

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_auth)])


class SettingsView(BaseModel):
    app_name: str
    environment: str
    debug: bool
    llm_provider: str
    llm_model_override: str
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
    return SettingsView(
        app_name=settings.app_name,
        environment=settings.environment,
        debug=settings.debug,
        llm_provider=settings.llm_provider.value,
        llm_model_override=settings.llm_model_override,
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


@router.patch("", response_model=SettingsView)
async def update_settings(req: SettingsUpdateRequest):
    """
    Updates in-memory settings for this process only (not persisted to .env).
    Restart the backend to fall back to .env values. This is intentional:
    editing secrets/infra values (ports, DB paths, API keys) from the
    dashboard is out of scope.
    """
    updates = req.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in updates.items():
        setattr(settings, key, value)
    return _to_view()
