import json

import pytest

from backend.plugins.registry import PluginRegistry

GOOD_PLUGIN = '''
from backend.plugins.base import NexusPlugin

class GoodPlugin(NexusPlugin):
    name = "good"
    version = "1.0.0"
    description = "well-behaved"
'''


def _write(tmp_path, filename, content):
    (tmp_path / filename).write_text(content)


class EventCollector:
    def __init__(self):
        self.events = []

    async def __call__(self, payload: str) -> None:
        self.events.append(json.loads(payload))


@pytest.mark.asyncio
async def test_enable_and_disable_emit_events(tmp_path):
    _write(tmp_path, "good.py", GOOD_PLUGIN)
    collector = EventCollector()
    registry = PluginRegistry(plugins_dir=tmp_path, event_fn=collector)
    registry.discover()

    await registry.enable("good")
    await registry.disable("good")

    types = [e["type"] for e in collector.events]
    assert types == ["plugin_enabled", "plugin_disabled"]
    assert collector.events[0]["name"] == "good"


@pytest.mark.asyncio
async def test_reload_emits_event(tmp_path):
    _write(tmp_path, "good.py", GOOD_PLUGIN)
    collector = EventCollector()
    registry = PluginRegistry(plugins_dir=tmp_path, event_fn=collector)
    await registry.load_all()

    collector.events.clear()
    ok = await registry.reload("good")

    assert ok is True
    types = [e["type"] for e in collector.events]
    assert "plugin_reloaded" in types


@pytest.mark.asyncio
async def test_task_dispatch_hooks_emit_events(tmp_path):
    collector = EventCollector()
    registry = PluginRegistry(plugins_dir=tmp_path, event_fn=collector)

    await registry.dispatch_task_start("t1", "https://example.com", "sign up")
    await registry.dispatch_task_finish("t1", "succeeded", "done")
    await registry.dispatch_wallet_popup("t1", "0xabc", 5.0, True)

    types = [e["type"] for e in collector.events]
    assert types == ["task_start", "task_finish", "wallet_popup"]
    assert collector.events[2]["final_decision"] is True


@pytest.mark.asyncio
async def test_no_event_fn_is_a_safe_no_op(tmp_path):
    """Default construction (no event_fn) must behave exactly as before -- no
    broadcast attempted, nothing raised."""
    _write(tmp_path, "good.py", GOOD_PLUGIN)
    registry = PluginRegistry(plugins_dir=tmp_path)
    registry.discover()

    assert await registry.enable("good") is True
    assert await registry.disable("good") is True


@pytest.mark.asyncio
async def test_broken_event_fn_never_breaks_dispatch(tmp_path):
    """A raising event_fn must be isolated, same guarantee as plugin hooks."""

    async def broken(payload):
        raise RuntimeError("boom")

    _write(tmp_path, "good.py", GOOD_PLUGIN)
    registry = PluginRegistry(plugins_dir=tmp_path, event_fn=broken)
    registry.discover()

    assert await registry.enable("good") is True
