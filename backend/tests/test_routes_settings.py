import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.routes_settings import router as settings_router


@pytest.fixture(autouse=True)
def _fresh_model_manager(tmp_path, monkeypatch):
    from backend.planner import model_manager as mm_module

    monkeypatch.setattr(mm_module, "STATE_PATH", tmp_path / "ai_model_manager.json")
    monkeypatch.setattr(mm_module, "model_manager", mm_module.ModelManager())
    yield


@pytest_asyncio.fixture
async def client():
    app = FastAPI()
    app.include_router(settings_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_get_settings_includes_ai_model_manager_fields(client):
    r = await client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert "ai_smart_routing_enabled" in body
    assert "ai_fallback_provider" in body
    assert body["llm_provider"] == "anthropic"


@pytest.mark.asyncio
async def test_patch_settings_switches_provider_through_model_manager(client):
    from backend.planner.model_manager import model_manager

    r = await client.patch("/api/settings", json={"llm_provider": "groq", "llm_model_override": "llama-x"})
    assert r.status_code == 200
    body = r.json()
    assert body["llm_provider"] == "groq"
    assert body["llm_model_override"] == "llama-x"
    # Confirms the update went through ModelManager, not a bare setattr.
    assert model_manager.current_provider.value == "groq"


@pytest.mark.asyncio
async def test_patch_settings_toggles_smart_routing_and_fallback(client):
    from backend.planner.model_manager import model_manager

    r = await client.patch(
        "/api/settings", json={"ai_smart_routing_enabled": True, "ai_fallback_provider": "openrouter"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ai_smart_routing_enabled"] is True
    assert body["ai_fallback_provider"] == "openrouter"
    assert model_manager.routing_mode == "auto"
    assert model_manager.fallback_provider.value == "openrouter"


@pytest.mark.asyncio
async def test_patch_settings_never_accepts_secrets(client):
    r = await client.patch("/api/settings", json={"anthropic_api_key": "sk-leak"})
    assert r.status_code == 200
    # Unknown/disallowed field is silently ignored by pydantic's model, not applied.
    body = r.json()
    assert "anthropic_api_key" not in body
