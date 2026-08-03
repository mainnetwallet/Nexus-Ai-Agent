import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api import routes_logs


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    log_file = tmp_path / "nexus.log"
    monkeypatch.setattr(routes_logs, "_LOG_FILE", log_file)

    app = FastAPI()
    app.include_router(routes_logs.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, log_file


@pytest.mark.asyncio
async def test_tail_logs_returns_empty_when_file_missing(client):
    c, _ = client
    resp = await c.get("/api/logs")
    assert resp.status_code == 200
    assert resp.json()["lines"] == []


@pytest.mark.asyncio
async def test_tail_logs_returns_last_n_lines(client):
    c, log_file = client
    log_file.write_text("\n".join(f"line {i}" for i in range(1, 6)) + "\n")

    resp = await c.get("/api/logs?lines=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["lines"] == ["line 4", "line 5"]
    assert body["total_lines"] == 5


@pytest.mark.asyncio
async def test_clear_logs_truncates_existing_file(client):
    c, log_file = client
    log_file.write_text("line 1\nline 2\n")

    resp = await c.delete("/api/logs")
    assert resp.status_code == 200
    assert resp.json() == {"cleared": True, "file": str(log_file)}

    assert log_file.exists()
    assert log_file.read_text() == ""


@pytest.mark.asyncio
async def test_clear_logs_is_a_noop_when_file_missing(client):
    c, log_file = client
    assert not log_file.exists()

    resp = await c.delete("/api/logs")
    assert resp.status_code == 200
    assert resp.json()["cleared"] is True
    # Clearing a nonexistent log file must not create one.
    assert not log_file.exists()


@pytest.mark.asyncio
async def test_tail_reflects_cleared_state(client):
    c, log_file = client
    log_file.write_text("old line\n")

    await c.delete("/api/logs")
    resp = await c.get("/api/logs")
    assert resp.json()["lines"] == []
