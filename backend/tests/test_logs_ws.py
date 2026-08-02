import asyncio
import logging

import pytest

from backend.api import routes_logs
from backend.api.routes_logs import WebSocketLogBroadcastHandler, broadcast_log_line


class FakeWebSocket:
    def __init__(self, fail=False):
        self.sent: list[str] = []
        self.fail = fail

    async def send_text(self, data: str):
        if self.fail:
            raise RuntimeError("client gone")
        self.sent.append(data)


@pytest.mark.asyncio
async def test_broadcast_log_line_fans_out_to_all_clients():
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    routes_logs._ws_clients.update({ws1, ws2})
    try:
        await broadcast_log_line("hello world")
        assert ws1.sent == ["hello world"]
        assert ws2.sent == ["hello world"]
    finally:
        routes_logs._ws_clients.clear()


@pytest.mark.asyncio
async def test_broadcast_log_line_drops_dead_clients():
    good, dead = FakeWebSocket(), FakeWebSocket(fail=True)
    routes_logs._ws_clients.update({good, dead})
    try:
        await broadcast_log_line("still alive")
        assert good.sent == ["still alive"]
        assert dead not in routes_logs._ws_clients
        assert good in routes_logs._ws_clients
    finally:
        routes_logs._ws_clients.clear()


@pytest.mark.asyncio
async def test_ws_log_handler_bridges_a_log_record_to_broadcast():
    loop = asyncio.get_running_loop()
    handler = WebSocketLogBroadcastHandler(loop)
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    ws = FakeWebSocket()
    routes_logs._ws_clients.add(ws)
    try:
        logger = logging.getLogger("nexus.test_handler")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            logger.info("hello from a test logger")
        finally:
            logger.removeHandler(handler)

        # emit() schedules the broadcast via call_soon_threadsafe -- give the
        # loop a tick to actually run the scheduled coroutine.
        await asyncio.sleep(0.05)

        assert any("hello from a test logger" in msg for msg in ws.sent)
    finally:
        routes_logs._ws_clients.discard(ws)


def test_ws_log_handler_swallows_formatting_errors():
    """A handler that can't format a record must not raise into the logger."""
    loop = asyncio.new_event_loop()
    handler = WebSocketLogBroadcastHandler(loop)

    class BrokenFormatter(logging.Formatter):
        def format(self, record):
            raise ValueError("boom")

    handler.setFormatter(BrokenFormatter())
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    handler.emit(record)  # must not raise
    loop.close()
