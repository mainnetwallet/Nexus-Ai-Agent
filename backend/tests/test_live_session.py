import json

import pytest

from backend.browser.engine import BrowserEngineError
from backend.browser.live_session import LiveSessionManager


class FakePage:
    def __init__(self, url="https://example.com", title="Example", raise_on_screenshot=False):
        self.url = url
        self._title = title
        self._raise_on_screenshot = raise_on_screenshot
        self.screenshot_calls = 0

    async def screenshot(self, type="jpeg", quality=60):
        self.screenshot_calls += 1
        if self._raise_on_screenshot:
            raise RuntimeError("page is navigating")
        return b"\xff\xd8fake-jpeg-bytes"

    async def title(self):
        return self._title


class FakeEngine:
    def __init__(self, page=None, no_active_page=False):
        self._page = page or FakePage()
        self._no_active_page = no_active_page

    @property
    def page(self):
        if self._no_active_page:
            raise BrowserEngineError("No active page")
        return self._page


class FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent: list[str] = []
        self.closed = False

    async def accept(self):
        self.accepted = True

    async def send_text(self, data: str):
        self.sent.append(data)

    async def close(self, code: int = 1000):
        self.closed = True


def _manager(engine=None, task_id="task-1", **kwargs) -> LiveSessionManager:
    box = {"engine": engine, "task_id": task_id if engine else None}
    return LiveSessionManager(
        engine_provider=lambda: box["engine"],
        task_id_provider=lambda: box["task_id"],
        interval_ms=kwargs.get("interval_ms", 50),
        jpeg_quality=kwargs.get("jpeg_quality", 60),
    ), box


@pytest.mark.asyncio
async def test_status_inactive_with_no_engine():
    manager, _ = _manager(engine=None)

    status = manager.status()

    assert status["active"] is False
    assert status["task_id"] is None
    assert status["connected_clients"] == 0
    assert status["last_frame_at"] is None


@pytest.mark.asyncio
async def test_capture_updates_status_and_latest_screenshot():
    page = FakePage(url="https://shop.example/cart", title="Cart")
    engine = FakeEngine(page=page)
    manager, _ = _manager(engine=engine)

    assert manager.latest_screenshot_bytes() is None

    await manager._capture(engine)

    status = manager.status()
    assert status["active"] is True
    assert status["task_id"] == "task-1"
    assert status["url"] == "https://shop.example/cart"
    assert status["title"] == "Cart"
    assert status["frame_count"] == 1
    assert status["last_frame_at"] is not None
    assert manager.latest_screenshot_bytes() == b"\xff\xd8fake-jpeg-bytes"


@pytest.mark.asyncio
async def test_capture_broadcasts_frame_to_connected_clients():
    engine = FakeEngine()
    manager, _ = _manager(engine=engine)
    ws = FakeWebSocket()
    await manager.register(ws)

    await manager._capture(engine)

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload["type"] == "frame"
    assert payload["task_id"] == "task-1"
    assert payload["mime_type"] == "image/jpeg"
    assert payload["image_base64"]  # non-empty base64 string


@pytest.mark.asyncio
async def test_register_sends_existing_frame_immediately():
    engine = FakeEngine()
    manager, _ = _manager(engine=engine)
    await manager._capture(engine)  # populate a frame before any client connects

    ws = FakeWebSocket()
    await manager.register(ws)

    assert ws.accepted is True
    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0])["type"] == "frame"


@pytest.mark.asyncio
async def test_register_with_no_frame_yet_sends_nothing():
    manager, _ = _manager(engine=None)
    ws = FakeWebSocket()

    await manager.register(ws)

    assert ws.accepted is True
    assert ws.sent == []


@pytest.mark.asyncio
async def test_capture_handles_no_active_page_gracefully():
    engine = FakeEngine(no_active_page=True)
    manager, _ = _manager(engine=engine)

    await manager._capture(engine)  # should not raise

    assert manager.latest_screenshot_bytes() is None
    assert manager.status()["frame_count"] == 0


@pytest.mark.asyncio
async def test_capture_handles_screenshot_failure_gracefully():
    page = FakePage(raise_on_screenshot=True)
    engine = FakeEngine(page=page)
    manager, _ = _manager(engine=engine)

    await manager._capture(engine)  # should not raise

    status = manager.status()
    assert status["frame_count"] == 0
    assert status["last_error"] is not None


@pytest.mark.asyncio
async def test_unregister_removes_client():
    engine = FakeEngine()
    manager, _ = _manager(engine=engine)
    ws = FakeWebSocket()
    await manager.register(ws)
    assert manager.status()["connected_clients"] == 1

    manager.unregister(ws)

    assert manager.status()["connected_clients"] == 0


@pytest.mark.asyncio
async def test_broadcast_drops_dead_clients():
    engine = FakeEngine()
    manager, _ = _manager(engine=engine)

    good = FakeWebSocket()
    await manager.register(good)

    class DeadWebSocket(FakeWebSocket):
        async def send_text(self, data: str):
            raise RuntimeError("connection closed")

    dead = DeadWebSocket()
    await manager.register(dead)
    assert manager.status()["connected_clients"] == 2

    await manager._capture(engine)

    assert manager.status()["connected_clients"] == 1
    assert len(good.sent) == 1  # no frame existed yet at register time, one sent on capture


@pytest.mark.asyncio
async def test_poll_loop_start_and_stop_lifecycle():
    manager, _ = _manager(engine=None, interval_ms=10)

    manager.start()
    assert manager._poll_task is not None
    assert not manager._poll_task.done()

    await manager.stop()

    assert manager._poll_task is None


@pytest.mark.asyncio
async def test_is_active_reflects_engine_provider():
    box = {"engine": None}
    manager = LiveSessionManager(
        engine_provider=lambda: box["engine"],
        task_id_provider=lambda: "task-1" if box["engine"] else None,
        interval_ms=50,
    )

    assert manager.is_active() is False

    box["engine"] = FakeEngine()
    assert manager.is_active() is True
