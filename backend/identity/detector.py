"""
Best-effort detection of whether Gmail/X/Discord is already logged in on a
profile's Chrome session, purely by inspecting the URL/visible text a
service's own "am I logged in" page redirects to -- the same read-only
technique WalletRegistry.classify_pending_request (backend/wallet/
registry.py) uses for wallet popups. Never touches or requests a password;
if a service isn't authenticated, the only action taken is reporting that
back so the caller can notify the user, per the mission's "do not assume
credentials" rule.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from backend.browser.engine import BrowserEngine

logger = logging.getLogger("nexus.identity.detector")


@dataclass
class ServiceCheck:
    service: str
    authenticated: Optional[bool]  # None = couldn't determine
    detail: str


# Each service: a URL that only stays put if already authenticated (and
# redirects to a login page otherwise), plus the URL/text fragments that
# indicate "not logged in" if seen after navigating there.
_SERVICE_PROBES: dict[str, dict[str, object]] = {
    "gmail": {
        "url": "https://mail.google.com/mail/u/0/#inbox",
        "logged_out_url_markers": ["accounts.google.com/", "ServiceLogin"],
        "logged_out_text_markers": ["sign in", "create account"],
    },
    "x": {
        "url": "https://x.com/home",
        "logged_out_url_markers": ["x.com/login", "x.com/i/flow/login"],
        "logged_out_text_markers": ["sign in to x", "log in"],
    },
    "discord": {
        "url": "https://discord.com/channels/@me",
        "logged_out_url_markers": ["discord.com/login"],
        "logged_out_text_markers": ["welcome back!", "login with qr code"],
    },
}

SUPPORTED_SERVICES = tuple(_SERVICE_PROBES.keys())


class SessionDetector:
    """Stateless -- safe to construct once and share (mirrors ChatEngine)."""

    async def detect(self, engine: BrowserEngine, service: str) -> ServiceCheck:
        probe = _SERVICE_PROBES.get(service)
        if probe is None:
            return ServiceCheck(service, None, f"unknown service {service!r}")

        page_id: Optional[str] = None
        try:
            page_id = await engine.new_tab(str(probe["url"]))
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            current_url = engine.page.url.lower()
            text = (await engine.extract_visible_text(max_chars=2000)).lower()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Session detection failed for %s: %s", service, exc)
            return ServiceCheck(service, None, f"detection failed: {exc}")
        finally:
            if page_id:
                try:
                    await engine.close_tab(page_id)
                except Exception:
                    logger.debug("detect: failed to close probe tab for %s", service)

        url_says_logged_out = any(marker in current_url for marker in probe["logged_out_url_markers"])  # type: ignore[arg-type]
        text_says_logged_out = any(marker in text for marker in probe["logged_out_text_markers"])  # type: ignore[arg-type]

        if url_says_logged_out or text_says_logged_out:
            return ServiceCheck(service, False, "not authenticated -- redirected to login")
        return ServiceCheck(service, True, "authenticated session detected")

    async def detect_all(self, engine: BrowserEngine, services: list[str]) -> dict[str, ServiceCheck]:
        results: dict[str, ServiceCheck] = {}
        for service in services:
            results[service] = await self.detect(engine, service)
        return results
