import json

import pytest

from backend.mcp.base import ConnectorStatus, MCPConnector, MCPTool
from backend.mcp.registry import MCPRegistry


class GoodConnector(MCPConnector):
    name = "good"
    version = "1.0.0"
    description = "always connects"
    tags = ["good"]

    def list_tools(self):
        return [MCPTool(name="ping", description="ping", keywords=["ping"])]

    async def call_tool(self, tool_name, arguments):
        return {"pong": True}


class FlakyConnector(MCPConnector):
    """Connects normally unless config['should_fail'] is truthy."""

    name = "flaky"
    version = "1.0.0"
    description = "fails on connect when configured to"
    tags = ["flaky"]

    async def connect(self) -> None:
        if self.config.get("should_fail"):
            raise RuntimeError("simulated connect failure")
        await super().connect()

    def list_tools(self):
        return []

    async def call_tool(self, tool_name, arguments):
        raise NotImplementedError


CLASSES = {"good": GoodConnector, "flaky": FlakyConnector}


@pytest.mark.asyncio
async def test_enable_persists_and_connects(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    ok = await registry.enable("good")
    assert ok is True
    record = registry.get_record("good")
    assert record.enabled is True
    assert record.instance.status == ConnectorStatus.CONNECTED

    persisted = json.loads((tmp_path / "mcp_connectors.json").read_text())
    assert persisted["good"]["enabled"] is True


@pytest.mark.asyncio
async def test_disable_persists_and_disconnects(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    await registry.enable("good")
    ok = await registry.disable("good")
    assert ok is True
    record = registry.get_record("good")
    assert record.enabled is False
    assert record.instance.status == ConnectorStatus.DISABLED

    persisted = json.loads((tmp_path / "mcp_connectors.json").read_text())
    assert persisted["good"]["enabled"] is False


@pytest.mark.asyncio
async def test_enable_unknown_connector_returns_false(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    assert await registry.enable("does-not-exist") is False


@pytest.mark.asyncio
async def test_configure_merges_config_and_persists(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    ok = await registry.configure("good", {"some_key": "some_value"})
    assert ok is True
    record = registry.get_record("good")
    assert record.config["some_key"] == "some_value"

    persisted = json.loads((tmp_path / "mcp_connectors.json").read_text())
    assert persisted["good"]["config"]["some_key"] == "some_value"


@pytest.mark.asyncio
async def test_configure_reconnects_when_already_enabled(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    await registry.enable("good")
    ok = await registry.configure("good", {"another_key": "v"})
    assert ok is True
    record = registry.get_record("good")
    # configure() on an already-enabled connector reconnects it so the new
    # config takes effect immediately.
    assert record.instance.status == ConnectorStatus.CONNECTED
    assert record.instance.config["another_key"] == "v"


@pytest.mark.asyncio
async def test_isolated_failure_on_connect_does_not_raise(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    ok = await registry.enable("flaky")
    assert ok is True  # default config connects fine

    # Reconfigure to force a connect failure -- this reconnects since
    # the connector is already enabled.
    ok2 = await registry.configure("flaky", {"should_fail": True})
    assert ok2 is False
    record = registry.get_record("flaky")
    assert record.enabled is False
    assert record.instance.status == ConnectorStatus.ERROR
    assert record.error is not None


@pytest.mark.asyncio
async def test_start_enabled_isolates_per_connector_failure(tmp_path):
    config_path = tmp_path / "mcp_connectors.json"
    config_path.write_text(
        json.dumps(
            {
                "good": {"enabled": True, "config": {}},
                "flaky": {"enabled": True, "config": {"should_fail": True}},
            }
        )
    )
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    await registry.start_enabled()

    good_record = registry.get_record("good")
    flaky_record = registry.get_record("flaky")
    assert good_record.instance.status == ConnectorStatus.CONNECTED
    assert flaky_record.enabled is False
    assert flaky_record.instance.status == ConnectorStatus.ERROR


@pytest.mark.asyncio
async def test_json_reload_restores_enabled_state_and_config(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    await registry.enable("good")
    await registry.configure("good", {"custom": "value"})

    # Simulate a process restart: a brand new registry pointed at the same
    # data_dir should load the persisted enabled/config state, without
    # having reconnected anything yet.
    reloaded = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    record = reloaded.get_record("good")
    assert record.enabled is True
    assert record.config["custom"] == "value"
    assert record.instance is None


@pytest.mark.asyncio
async def test_list_connectors_redacts_secret_looking_keys(tmp_path):
    registry = MCPRegistry(data_dir=tmp_path, connector_classes=CLASSES)
    await registry.configure("good", {"api_token": "super-secret", "plain": "visible"})
    listed = {c["name"]: c for c in registry.list_connectors()}
    assert listed["good"]["config"]["api_token"] == "***"
    assert listed["good"]["config"]["plain"] == "visible"
