from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, status

from backend.config.settings import settings

logger = logging.getLogger("nexus.auth")
_warned_open = False


async def require_auth(authorization: str = Header(default="")) -> None:
    if not settings.api_auth_token:
        global _warned_open
        if not settings.debug and not _warned_open:
            logger.warning(
                "API_AUTH_TOKEN is not set -- all REST/WebSocket endpoints are open with no auth. "
                "Set API_AUTH_TOKEN before exposing this beyond localhost."
            )
            _warned_open = True
        return  # no token configured -> open (development mode only)

    expected = f"Bearer {settings.api_auth_token}"
    # Constant-time comparison: a naive `!=` leaks timing information that
    # can be used to brute-force the token one byte at a time.
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing token")
