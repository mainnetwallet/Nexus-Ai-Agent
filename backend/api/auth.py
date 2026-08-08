from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, status
from starlette.requests import HTTPConnection

from backend.config.settings import settings

logger = logging.getLogger("nexus.auth")
_warned_open = False


async def require_auth(conn: HTTPConnection, authorization: str = Header(default="")) -> None:
    if not settings.api_auth_token:
        global _warned_open
        if not settings.debug and not _warned_open:
            logger.warning(
                "API_AUTH_TOKEN is not set -- all REST/WebSocket endpoints are open with no auth. "
                "Set API_AUTH_TOKEN before exposing this beyond localhost."
            )
            _warned_open = True
        return  # no token configured -> open (development mode only)

    # REST clients send `Authorization: Bearer <token>`. Browser WebSocket
    # clients cannot set custom headers on the handshake, so the frontend
    # passes the raw token as `?token=<token>` instead (see wsUrl() in
    # frontend/src/lib/api.ts) -- honor both, with the header winning. A
    # header that is present but not Bearer-prefixed is rejected outright
    # rather than silently falling back to the query string.
    if authorization:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing token")
        token = authorization.removeprefix("Bearer ").strip()
    else:
        token = conn.query_params.get("token", "")
    # Constant-time comparison: a naive `!=` leaks timing information that
    # can be used to brute-force the token one byte at a time.
    if not hmac.compare_digest(token, settings.api_auth_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing token")
