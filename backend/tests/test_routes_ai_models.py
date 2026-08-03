import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.routes_ai_models import router as ai_models_router
from backend.config.settings import LLMProvider


@pytest.fixture(autouse=True)
def _reset_model_manager(tmp_path, monkeypatch):
    """Give every test a fresh ModelManager backed by a throwaway state file,
    so switching/routing changes in one test never leak into another."""
    from backend.planner import model_manager as mm_module

    monkeypatch.setattr(mm_module, "STATE_PATH", tmp_path / "ai_model_manager.json")
    monkeypatch.setattr(mm_module, "model_manager", mm_module.ModelManager())

    import backend.api.routes_ai_models as routes_module

    monkeypatch.setattr(routes_module, "model_manager", mm_module.model_manager)
    yield


@pytest_asyncio.fixture()
async def client():
    app = FastAPI()
    app.include_router(ai_models_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_get_ai_models_returns_full_view(client):
    r = await client.get("/api/ai-models")
    assert r.status_code == 200
    body = r.json()
    assert body["current_provider"] == "anthropic"
    assert body["routing_mode"] in ("manual", "auto")
    assert len(body["providers"]) == len(LLMProvider)


@pytest.mark.asyncio
async def test_switch_provider_via_api(client):
    r = await client.post("/api/ai-models/switch", json={"provider": "groq", "model": "llama-x"})
    assert r.status_code == 200
    body = r.json()
    assert body["current_provider"] == "groq"
    assert body["current_model"] == "llama-x"


@pytest.mark.asyncio
async def test_switch_provider_accepts_free_text_alias(client):
    r = await client.post("/api/ai-models/switch", json={"provider": "claude"})
    assert r.status_code == 200
    assert r.json()["current_provider"] == "anthropic"


@pytest.mark.asyncio
async def test_switch_unknown_provider_returns_400(client):
    r = await client.post("/api/ai-models/switch", json={"provider": "not-a-real-provider"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_routing_mode_toggle(client):
    r = await client.post("/api/ai-models/routing-mode", json={"mode": "auto"})
    assert r.status_code == 200
    assert r.json()["routing_mode"] == "auto"

    r = await client.post("/api/ai-models/routing-mode", json={"mode": "bogus"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_routing_rules_bulk_update(client):
    r = await client.put(
        "/api/ai-models/routing-rules",
        json={"rules": {"coding": "anthropic", "fast_response": "groq"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["coding"] == "anthropic"
    assert body["fast_response"] == "groq"


@pytest.mark.asyncio
async def test_routing_rules_bulk_update_rejects_unknown_task_type(client):
    r = await client.put("/api/ai-models/routing-rules", json={"rules": {"bogus_task": "groq"}})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_single_routing_rule_update(client):
    r = await client.post(
        "/api/ai-models/routing-rules/one", json={"task_type": "vision", "provider": "gemini"}
    )
    assert r.status_code == 200
    assert r.json() == {"task_type": "vision", "provider": "gemini"}


@pytest.mark.asyncio
async def test_fallback_and_priority_and_enable_disable(client):
    r = await client.post("/api/ai-models/fallback", json={"provider": "openrouter"})
    assert r.status_code == 200
    assert r.json()["fallback_provider"] == "openrouter"

    r = await client.post("/api/ai-models/priority", json={"providers": ["groq", "openai"]})
    assert r.status_code == 200
    assert r.json()["provider_priority"] == ["groq", "openai"]

    r = await client.post("/api/ai-models/disable", json={"provider": "gemini"})
    assert r.status_code == 200
    assert "gemini" in r.json()["disabled_providers"]

    r = await client.post("/api/ai-models/enable", json={"provider": "gemini"})
    assert r.status_code == 200
    assert "gemini" not in r.json()["disabled_providers"]


@pytest.mark.asyncio
async def test_temporary_override_set_and_clear(client):
    r = await client.post("/api/ai-models/override", json={"provider": "gemini", "reason": "one-off"})
    assert r.status_code == 200
    assert r.json()["temporary_override"]["provider"] == "gemini"

    r = await client.delete("/api/ai-models/override")
    assert r.status_code == 200
    assert r.json()["temporary_override"] is None


@pytest.mark.asyncio
async def test_test_connection_without_api_key_reports_failure(client):
    r = await client.post("/api/ai-models/test/groq")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "provider" in body


@pytest.mark.asyncio
async def test_health_endpoint_returns_snapshot_for_every_provider(client):
    r = await client.get("/api/ai-models/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {p.value for p in LLMProvider}
