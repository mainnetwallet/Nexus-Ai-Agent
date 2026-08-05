import asyncio

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.api import app_state
from backend.api.routes_system import router as system_router
from backend.config.config_manager import BACKUP_DIR, ConfigManager
from backend.database.models import AgentRuntimeState, Report, Task
from backend.database.session import get_session, init_db
from backend.planner.agent_runtime import AgentRuntime
from backend.planner.task_queue import TaskQueueService


class FakeMemory:
    async def recall_similar_workflows(self, website, goal, top_k=3):
        return []

    async def save_workflow_outcome(self, website, goal, outcome):
        pass


@pytest_asyncio.fixture
async def client():
    await init_db()
    async with get_session() as session:
        await session.execute(delete(Report))
        await session.execute(delete(Task))
        await session.execute(delete(AgentRuntimeState))

    queue = TaskQueueService(memory=FakeMemory(), wallet=None)
    app_state.state.queue = queue
    app_state.state.agent = AgentRuntime(queue=queue)
    app_state.state.memory = FakeMemory()
    app_state.state.plugins = None
    app_state.state.live_session = None
    app_state.state.wallet_registry = None

    app = FastAPI()
    app.include_router(system_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    worker_task = queue._worker_task
    if worker_task is not None and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    async with get_session() as session:
        await session.execute(delete(Report))
        await session.execute(delete(Task))
        await session.execute(delete(AgentRuntimeState))
    app_state.state.queue = None
    app_state.state.agent = None
    app_state.state.memory = None


@pytest.mark.asyncio
async def test_health_endpoint_reports_components(client):
    r = await client.get("/api/system/health")
    assert r.status_code == 200
    body = r.json()
    assert body["overall"] in {"ok", "degraded", "down", "unknown"}
    names = {c["name"] for c in body["components"]}
    assert names == {"backend", "database", "browser", "memory", "ai_provider", "telegram", "websocket", "mcp"}
    assert all("latency_ms" in c for c in body["components"])


@pytest.mark.asyncio
async def test_diagnostics_endpoint_returns_checks(client):
    r = await client.get("/api/system/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert "passed" in body
    check_names = {c["name"].split(":")[0] for c in body["checks"]}
    assert check_names == {"browser", "playwright", "ai_api", "database", "plugins", "memory", "environment"}


@pytest.mark.asyncio
async def test_diagnostics_text_endpoint(client):
    r = await client.get("/api/system/diagnostics/text")
    assert r.status_code == 200
    assert "Nexus-Agent Diagnostic Report" in r.json()["report"]


@pytest.mark.asyncio
async def test_resources_endpoint_returns_snapshot(client):
    r = await client.get("/api/system/resources")
    assert r.status_code == 200
    body = r.json()
    assert "cpu_percent" in body
    assert "queue_size" in body
    assert "active_tasks" in body
    assert body["queue_size"] >= 0


@pytest.mark.asyncio
async def test_version_endpoint_returns_build_info(client):
    r = await client.get("/api/system/version")
    assert r.status_code == 200
    body = r.json()
    for key in ("version", "commit", "commit_short", "branch", "dirty", "repo"):
        assert key in body


@pytest.mark.asyncio
async def test_config_export_import_roundtrip(client):
    r = await client.get("/api/system/config/export")
    assert r.status_code == 200
    exported = r.json()
    assert "settings" in exported
    assert "api_auth_token" not in exported["settings"]
    assert "anthropic_api_key" not in exported["settings"]

    exported["settings"]["browser_slow_mo_ms"] = 250
    r = await client.post("/api/system/config/import", json={"settings": exported["settings"]})
    assert r.status_code == 200
    assert r.json()["applied"]["browser_slow_mo_ms"] == 250


@pytest.mark.asyncio
async def test_config_backup_and_restore_roundtrip(client, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.config.config_manager.BACKUP_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)

    r = await client.post("/api/system/config/backup")
    assert r.status_code == 200
    filename = r.json()["filename"]

    r = await client.get("/api/system/config/backups")
    assert r.status_code == 200
    assert any(b["filename"] == filename for b in r.json()["backups"])

    r = await client.post("/api/system/config/restore", json={"filename": filename})
    assert r.status_code == 200
    assert "applied" in r.json()


@pytest.mark.asyncio
async def test_config_restore_missing_file_returns_404(client, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.config.config_manager.BACKUP_DIR", tmp_path)
    r = await client.post("/api/system/config/restore", json={"filename": "does_not_exist.json"})
    assert r.status_code == 404


def test_config_manager_never_exports_secrets():
    exported = ConfigManager.export_settings()
    dumped_keys = set(exported["settings"].keys())
    assert "api_auth_token" not in dumped_keys
    assert "telegram_bot_token" not in dumped_keys
    assert not any(k.endswith("_api_key") for k in dumped_keys)
