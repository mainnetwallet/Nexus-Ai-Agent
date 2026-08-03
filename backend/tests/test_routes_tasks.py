import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.api import app_state
from backend.api.routes_tasks import router as tasks_router
from backend.database.models import Report, Task
from backend.database.session import get_session, init_db
from backend.planner.task_queue import TaskQueueService


class FakeMemory:
    async def recall_similar_workflows(self, website, goal, top_k=3):
        return []

    async def save_workflow_outcome(self, website, goal, outcome):
        pass


@pytest_asyncio.fixture
async def client():
    await init_db()
    app_state.state.queue = TaskQueueService(memory=FakeMemory(), wallet=None)
    app = FastAPI()
    app.include_router(tasks_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    async with get_session() as session:
        await session.execute(delete(Report))
        await session.execute(delete(Task))
    app_state.state.queue = None


@pytest.mark.asyncio
async def test_create_then_get_task_returns_steps_field(client):
    r = await client.post("/api/tasks", json={"website": "https://example.com", "goal": "sign up", "notes": ""})
    assert r.status_code == 200
    task_id = r.json()["id"]

    r = await client.get(f"/api/tasks/{task_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == task_id
    assert body["status"] == "queued"
    assert body["steps"] == []  # previously raised MissingGreenlet instead of returning


@pytest.mark.asyncio
async def test_get_unknown_task_returns_error_not_500(client):
    r = await client.get("/api/tasks/does-not-exist")
    assert r.status_code == 200
    assert r.json() == {"error": "not found"}


@pytest.mark.asyncio
async def test_list_tasks_includes_scheduled_for(client):
    await client.post("/api/tasks", json={"website": "https://example.com", "goal": "g", "notes": ""})
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    assert "scheduled_for" in r.json()[0]


@pytest.mark.asyncio
async def test_cancel_unknown_task_reports_not_found(client):
    r = await client.post("/api/tasks/does-not-exist/cancel")
    assert r.json() == {"error": "not found"}


@pytest.mark.asyncio
async def test_cancel_known_task_requests_cancellation(client):
    r = await client.post("/api/tasks", json={"website": "https://example.com", "goal": "g", "notes": ""})
    task_id = r.json()["id"]

    r = await client.post(f"/api/tasks/{task_id}/cancel")
    assert r.json() == {"id": task_id, "cancel_requested": True}
    assert task_id in app_state.state.queue._cancelled_ids


@pytest.mark.asyncio
async def test_pause_and_resume_report_not_running_for_queued_task(client):
    r = await client.post("/api/tasks", json={"website": "https://example.com", "goal": "g", "notes": ""})
    task_id = r.json()["id"]

    r = await client.post(f"/api/tasks/{task_id}/pause")
    assert r.json() == {"error": "task is not currently running"}

    r = await client.post(f"/api/tasks/{task_id}/resume")
    assert r.json() == {"error": "task is not currently paused"}


@pytest.mark.asyncio
async def test_retry_rejects_a_still_queued_task(client):
    r = await client.post("/api/tasks", json={"website": "https://example.com", "goal": "g", "notes": ""})
    task_id = r.json()["id"]

    r = await client.post(f"/api/tasks/{task_id}/retry")
    body = r.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_queue_status_and_pause_resume_roundtrip(client):
    r = await client.get("/api/tasks/queue/status")
    assert r.json() == {"worker_paused": False, "active_task_id": None, "paused_task_ids": []}

    r = await client.post("/api/tasks/queue/pause")
    assert r.json() == {"worker_paused": True}

    r = await client.get("/api/tasks/queue/status")
    assert r.json()["worker_paused"] is True

    r = await client.post("/api/tasks/queue/resume")
    assert r.json() == {"worker_paused": False}


@pytest.mark.asyncio
async def test_scheduled_for_round_trips_through_create_and_list(client):
    scheduled = "2099-01-01T00:00:00+00:00"
    r = await client.post(
        "/api/tasks",
        json={"website": "https://example.com", "goal": "g", "notes": "", "scheduled_for": scheduled},
    )
    task_id = r.json()["id"]

    r = await client.get("/api/tasks")
    entry = next(t for t in r.json() if t["id"] == task_id)
    assert entry["scheduled_for"] is not None
