"""
Shared pytest fixtures for backend/tests.

Every route test in this suite (test_routes_agent.py, test_routes_tasks.py,
test_system_routes.py, test_skills.py, the new test_routes_profiles.py, ...)
calls endpoints without an Authorization header, on the assumption that
require_auth (backend/api/auth.py) is running in its open/no-token
development mode. The repo's own .env sets a real API_AUTH_TOKEN for actual
deployment, though -- so without this fixture, loading Settings from .env
during the test session makes every one of those calls come back 401
instead of exercising the route logic the test actually cares about.

This autouse fixture forces settings.api_auth_token empty for the whole
test session, independent of whatever .env happens to contain.
"""
from __future__ import annotations

import pytest

from backend.config.settings import settings


@pytest.fixture(autouse=True)
def _no_auth_token_in_tests(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "")
