import asyncio

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.api import app_state
from backend.api.routes_agent import router as agent_router
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
    app_state.state.live_session = None
    app_state.state.wallet_registry = None

    app = FastAPI()
    app.include_router(agent_router)
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


@pytest.mark.asyncio
async def test_status_before_start_is_stopped(client):
    r = await client.get("/api/agent/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "stopped"
    assert body["browser"] == {"active": False, "url": "", "title": ""}
    assert body["active_wallet"] is None


@pytest.mark.asyncio
async def test_start_then_status_reports_running(client):
    r = await client.post("/api/agent/start")
    assert r.status_code == 200
    assert r.json()["status"] == "running"

    r = await client.get("/api/agent/status")
    assert r.json()["status"] == "running"


@pytest.mark.asyncio
async def test_stop_reports_stopped(client):
    await client.post("/api/agent/start")
    r = await client.post("/api/agent/stop")
    assert r.status_code == 200
    assert r.json()["status"] == "stopped"


@pytest.mark.asyncio
async def test_pause_then_resume_roundtrip(client):
    await client.post("/api/agent/start")

    r = await client.post("/api/agent/pause")
    assert r.json()["status"] == "paused"
    assert r.json()["queue"]["worker_paused"] is True

    r = await client.post("/api/agent/resume")
    assert r.json()["status"] == "running"
    assert r.json()["queue"]["worker_paused"] is False


@pytest.mark.asyncio
async def test_endpoints_report_error_when_runtime_not_initialized(client):
    app_state.state.agent = None
    for path in ("/api/agent/start", "/api/agent/stop", "/api/agent/pause", "/api/agent/resume"):
        r = await client.post(path)
        assert r.json() == {"error": "agent runtime not initialized"}
    r = await client.get("/api/agent/status")
    assert r.json() == {"error": "agent runtime not initialized"}
