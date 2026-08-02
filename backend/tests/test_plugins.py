import pytest

from backend.plugins.base import NexusPlugin, PluginContext
from backend.plugins.registry import PluginRegistry
from backend.wallet.manager import WalletManager


GOOD_PLUGIN = '''
from backend.plugins.base import NexusPlugin

class GoodPlugin(NexusPlugin):
    name = "good"
    version = "1.2.3"
    description = "a well-behaved test plugin"

    def __init__(self):
        super().__init__()
        self.loaded = False
        self.events = []

    async def on_load(self, ctx):
        self.loaded = True

    async def on_unload(self):
        self.loaded = False

    async def on_task_start(self, task_id, website, goal):
        self.events.append(("start", task_id))

    async def on_step(self, task_id, step):
        self.events.append(("step", task_id))

    async def on_task_finish(self, task_id, status, summary):
        self.events.append(("finish", task_id, status))
'''

BROKEN_PLUGIN = '''
from backend.plugins.base import NexusPlugin

class BrokenPlugin(NexusPlugin):
    name = "broken"

    async def on_task_start(self, task_id, website, goal):
        raise RuntimeError("boom")
'''

VETO_PLUGIN = '''
from backend.plugins.base import NexusPlugin

class VetoPlugin(NexusPlugin):
    name = "veto"

    async def on_wallet_popup(self, task_id, contract_address, estimated_value, approve):
        return False
'''

NO_SUBCLASS_MODULE = '''
x = 1
'''

TWO_SUBCLASSES_MODULE = '''
from backend.plugins.base import NexusPlugin

class A(NexusPlugin):
    name = "a"

class B(NexusPlugin):
    name = "b"
'''


def _write(tmp_path, filename, content):
    path = tmp_path / filename
    path.write_text(content)
    return path


@pytest.mark.asyncio
async def test_discover_and_enable_loads_plugin(tmp_path):
    _write(tmp_path, "good.py", GOOD_PLUGIN)
    registry = PluginRegistry(plugins_dir=tmp_path)

    discovered = registry.discover()
    assert discovered == ["good"]
    listed = registry.list_plugins()
    assert listed == [{"name": "good", "version": "1.2.3", "description": "a well-behaved test plugin", "enabled": False, "error": None}]

    assert await registry.enable("good") is True
    assert registry.list_plugins()[0]["enabled"] is True


@pytest.mark.asyncio
async def test_load_all_autoenables_discovered_plugins(tmp_path):
    _write(tmp_path, "good.py", GOOD_PLUGIN)
    registry = PluginRegistry(plugins_dir=tmp_path)
    await registry.load_all()
    assert registry.list_plugins()[0]["enabled"] is True


@pytest.mark.asyncio
async def test_disable_calls_on_unload(tmp_path):
    _write(tmp_path, "good.py", GOOD_PLUGIN)
    registry = PluginRegistry(plugins_dir=tmp_path)
    await registry.load_all()
    instance = registry._records["good"].instance
    assert instance.loaded is True

    assert await registry.disable("good") is True
    assert instance.loaded is False
    assert registry.list_plugins()[0]["enabled"] is False


@pytest.mark.asyncio
async def test_enable_unknown_plugin_returns_false(tmp_path):
    registry = PluginRegistry(plugins_dir=tmp_path)
    assert await registry.enable("nope") is False
    assert await registry.disable("nope") is False


@pytest.mark.asyncio
async def test_dispatch_reaches_enabled_plugin_only(tmp_path):
    _write(tmp_path, "good.py", GOOD_PLUGIN)
    registry = PluginRegistry(plugins_dir=tmp_path)
    registry.discover()
    instance = registry._records["good"].instance

    # not yet enabled -- dispatch should be a no-op
    await registry.dispatch_task_start("t1", "https://example.com", "goal")
    assert instance.events == []

    await registry.enable("good")
    await registry.dispatch_task_start("t1", "https://example.com", "goal")
    await registry.dispatch_step("t1", object())
    await registry.dispatch_task_finish("t1", "succeeded", "done")
    assert [e[0] for e in instance.events] == ["start", "step", "finish"]


@pytest.mark.asyncio
async def test_broken_plugin_hook_is_isolated(tmp_path):
    _write(tmp_path, "good.py", GOOD_PLUGIN)
    _write(tmp_path, "broken.py", BROKEN_PLUGIN)
    registry = PluginRegistry(plugins_dir=tmp_path)
    await registry.load_all()

    good_instance = registry._records["good"].instance

    # broken plugin's on_task_start raises; must not crash dispatch or affect
    # the other enabled plugin, and broken must stay enabled (isolated, not disabled)
    await registry.dispatch_task_start("t1", "https://example.com", "goal")
    assert good_instance.events == [("start", "t1")]
    assert registry.list_plugins() == sorted(registry.list_plugins(), key=lambda p: p["name"])
    broken_record = next(p for p in registry.list_plugins() if p["name"] == "broken")
    assert broken_record["enabled"] is True


@pytest.mark.asyncio
async def test_reload_picks_up_module_changes(tmp_path):
    path = _write(tmp_path, "good.py", GOOD_PLUGIN)
    registry = PluginRegistry(plugins_dir=tmp_path)
    await registry.load_all()
    assert registry.list_plugins()[0]["version"] == "1.2.3"

    path.write_text(GOOD_PLUGIN.replace('"1.2.3"', '"9.9.9"'))
    assert await registry.reload("good") is True
    assert registry.list_plugins()[0]["version"] == "9.9.9"
    assert registry.list_plugins()[0]["enabled"] is True


@pytest.mark.asyncio
async def test_module_with_no_plugin_subclass_records_error(tmp_path):
    _write(tmp_path, "bad.py", NO_SUBCLASS_MODULE)
    registry = PluginRegistry(plugins_dir=tmp_path)
    registry.discover()
    record = registry.list_plugins()[0]
    assert record["name"] == "bad"
    assert record["error"] is not None
    assert record["enabled"] is False


@pytest.mark.asyncio
async def test_module_with_two_plugin_subclasses_records_error(tmp_path):
    _write(tmp_path, "twobad.py", TWO_SUBCLASSES_MODULE)
    registry = PluginRegistry(plugins_dir=tmp_path)
    registry.discover()
    record = registry.list_plugins()[0]
    assert record["error"] is not None


@pytest.mark.asyncio
async def test_wallet_popup_plugin_can_veto_but_not_grant(tmp_path):
    _write(tmp_path, "veto.py", VETO_PLUGIN)
    registry = PluginRegistry(plugins_dir=tmp_path)
    await registry.load_all()

    # veto plugin turns an approve into a reject
    assert await registry.dispatch_wallet_popup("t1", "0xabc", 1.0, True) is False
    # it must never turn a reject into an approve
    assert await registry.dispatch_wallet_popup("t1", "0xabc", 1.0, False) is False


class _FakeEngine:
    async def detect_popup_or_dialog(self, timeout_ms=500):
        return "popup1"

    def switch_tab(self, popup_id):
        pass

    async def extract_visible_text(self, max_chars=2000):
        return "Approve this transaction from 0x1111111111111111111111111111111111111111 worth $5"

    async def smart_click(self, label):
        self.clicked = label
        return True


@pytest.mark.asyncio
async def test_wallet_manager_applies_plugin_veto(tmp_path, monkeypatch):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "wallet_require_manual_approval", False)
    monkeypatch.setattr(settings_module.settings, "wallet_max_auto_approve_value_usd", 100.0)
    monkeypatch.setattr(
        settings_module.settings, "wallet_allowlisted_contracts", "0x1111111111111111111111111111111111111111"
    )

    _write(tmp_path, "veto.py", VETO_PLUGIN)
    registry = PluginRegistry(plugins_dir=tmp_path)
    await registry.load_all()

    manager = WalletManager(plugin_registry=registry)
    engine = _FakeEngine()
    decision = await manager.handle_pending_popup(engine, "my wallet", task_id="t1")

    assert decision.approve is False
    assert decision.reason == "vetoed by plugin"
    assert engine.clicked in ("Reject", "Cancel")
