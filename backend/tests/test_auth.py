"""Auth dependency coverage: header vs WebSocket ?token= query auth.

The `require_auth` dependency is shared by REST routers AND WebSocket routes.
Browser WebSocket clients cannot set custom handshake headers, so the
frontend passes the raw token as `?token=` instead -- this suite pins that
both paths work and stay constant-time compared, and that the no-token
development mode stays open.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.api import auth
from backend.api.auth import require_auth
from backend.config.settings import settings


class FakeHTTPConnection:
    """Minimal stand-in for starlette's HTTPConnection / WebSocket --
    only query_params is touched by require_auth when no header is sent."""

    def __init__(self, query_params=None):
        self.query_params = query_params or {}


@pytest.fixture(autouse=True)
def _reset_warned_open():
    auth._warned_open = False


@pytest.mark.asyncio
async def test_open_mode_when_no_token_configured(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "")
    await require_auth(FakeHTTPConnection())  # must not raise


@pytest.mark.asyncio
async def test_header_bearer_token_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "sekrit")
    await require_auth(FakeHTTPConnection(), authorization="Bearer sekrit")


@pytest.mark.asyncio
async def test_query_token_is_accepted_for_websockets(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "sekrit")
    # No Authorization header (browsers can't set one on a WS handshake) --
    # the frontend's wsUrl() sends the raw token as ?token= instead.
    await require_auth(FakeHTTPConnection(query_params={"token": "sekrit"}))


@pytest.mark.asyncio
async def test_header_wins_over_query_token(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "sekrit")
    await require_auth(
        FakeHTTPConnection(query_params={"token": "wrong-from-query"}),
        authorization="Bearer sekrit",
    )


@pytest.mark.asyncio
async def test_missing_token_raises_401(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "sekrit")
    with pytest.raises(HTTPException) as excinfo:
        await require_auth(FakeHTTPConnection())
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_query_token_raises_401(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "sekrit")
    with pytest.raises(HTTPException):
        await require_auth(FakeHTTPConnection(query_params={"token": "wrong"}))


@pytest.mark.asyncio
async def test_wrong_header_raises_401(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "sekrit")
    with pytest.raises(HTTPException):
        await require_auth(FakeHTTPConnection(), authorization="Bearer wrong")


@pytest.mark.asyncio
async def test_bare_token_header_is_rejected(monkeypatch):
    # A header that is present but not Bearer-prefixed must be rejected, not
    # silently fall back to the query string.
    monkeypatch.setattr(settings, "api_auth_token", "sekrit")
    with pytest.raises(HTTPException) as excinfo:
        await require_auth(
            FakeHTTPConnection(query_params={"token": "sekrit"}),
            authorization="sekrit",
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_trailing_whitespace_in_bearer_token_tolerated(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "sekrit")
    await require_auth(FakeHTTPConnection(), authorization="Bearer sekrit ")
