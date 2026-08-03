import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.api import app_state
from backend.api.routes_profiles import router as profiles_router
from backend.database.models import ProfileActivity, ProfileRecord
from backend.database.session import get_session, init_db
from backend.identity.manager import ProfileManager
from backend.identity.registry import ProfileRegistry


@pytest_asyncio.fixture
async def client(tmp_path):
    await init_db()
    async with get_session() as session:
        await session.execute(delete(ProfileActivity))
        await session.execute(delete(ProfileRecord))

    registry = ProfileRegistry(data_dir=tmp_path)
    app_state.state.profile_registry = registry
    app_state.state.profiles = ProfileManager(registry)
    app_state.state.queue = None  # no active browser -> sessions/check returns 409

    app = FastAPI()
    app.include_router(profiles_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    async with get_session() as session:
        await session.execute(delete(ProfileActivity))
        await session.execute(delete(ProfileRecord))
    app_state.state.profile_registry = None
    app_state.state.profiles = None


async def _create(client, name="Profile-01", **kwargs):
    payload = {"name": name, **kwargs}
    r = await client.post("/api/profiles", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_list_profiles_empty_then_with_entries(client):
    r = await client.get("/api/profiles")
    assert r.status_code == 200
    assert r.json() == []

    await _create(client)

    r = await client.get("/api/profiles")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "Profile-01"


@pytest.mark.asyncio
async def test_create_profile_duplicate_name_rejected(client):
    await _create(client, name="Dup")
    r = await client.post("/api/profiles", json={"name": "Dup"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_get_profile_returns_single_resolved_profile_not_the_list(client):
    created = await _create(client)
    profile_id = created["id"]

    r = await client.get(f"/api/profiles/{profile_id}")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert body["id"] == profile_id
    assert "sessions" in body
    assert body["sessions"] == {"gmail": None, "x": None, "discord": None}


@pytest.mark.asyncio
async def test_get_profile_unknown_id_is_404(client):
    r = await client.get("/api/profiles/does-not-exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_update_rename_delete_roundtrip(client):
    created = await _create(client)
    profile_id = created["id"]

    r = await client.patch(f"/api/profiles/{profile_id}", json={"notes": "updated notes"})
    assert r.status_code == 200
    assert r.json()["notes"] == "updated notes"

    r = await client.post(f"/api/profiles/{profile_id}/rename", json={"new_name": "Profile-Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Profile-Renamed"

    r = await client.delete(f"/api/profiles/{profile_id}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r = await client.get(f"/api/profiles/{profile_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_clone_then_export_import_roundtrip(client):
    created = await _create(client, name="Original", wallet_label="wallet-1")
    profile_id = created["id"]

    r = await client.post(f"/api/profiles/{profile_id}/clone", json={"new_name": "Cloned"})
    assert r.status_code == 200
    assert r.json()["name"] == "Cloned"

    r = await client.get(f"/api/profiles/{profile_id}/export")
    assert r.status_code == 200
    exported = r.json()
    assert "id" not in exported
    assert "chrome_profile_dir" not in exported
    assert exported["name"] == "Original"

    exported["name"] = "Imported"
    r = await client.post("/api/profiles/import", json=exported)
    assert r.status_code == 200
    imported = r.json()
    assert imported["name"] == "Imported"
    assert imported["wallet_label"] == "wallet-1"


@pytest.mark.asyncio
async def test_enable_disable_select(client):
    created = await _create(client)
    profile_id = created["id"]

    r = await client.post(f"/api/profiles/{profile_id}/disable")
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    r = await client.post(f"/api/profiles/{profile_id}/enable")
    assert r.status_code == 200
    assert r.json()["enabled"] is True

    r = await client.post(f"/api/profiles/{profile_id}/select")
    assert r.status_code == 200
    assert r.json()["is_active"] is True


@pytest.mark.asyncio
async def test_sessions_get_and_check_without_active_browser_is_409(client):
    created = await _create(client)
    profile_id = created["id"]

    r = await client.get(f"/api/profiles/{profile_id}/sessions")
    assert r.status_code == 200
    assert r.json() == {
        "gmail": None,
        "x": None,
        "discord": None,
        "last_session_check_at": None,
    }

    r = await client.post(f"/api/profiles/{profile_id}/sessions/check")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_filesystem_activity_and_supported_services(client):
    created = await _create(client)
    profile_id = created["id"]

    r = await client.get(f"/api/profiles/{profile_id}/filesystem")
    assert r.status_code == 200
    assert "exists" in r.json()

    r = await client.get(f"/api/profiles/{profile_id}/activity")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r = await client.get("/api/profiles/activity")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r = await client.get("/api/profiles/meta/supported-services")
    assert r.status_code == 200
    assert "services" in r.json()
    assert isinstance(r.json()["services"], list)
