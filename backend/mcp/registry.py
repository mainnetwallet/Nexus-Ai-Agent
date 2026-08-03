"""
MCPRegistry -- discovers available connector classes, persists which ones
are enabled and their configuration, and owns connector instance lifecycle
(construct/connect/disconnect). Same shape as backend/plugins/registry.py's
PluginRegistry, adapted for a fixed set of built-in connector classes
instead of dynamically imported files on disk (MCP connectors are trusted
first-party code shipped with the app, not user-supplied modules -- there
is deliberately no "install a connector from a string/upload" path here,
matching the plugin registry's own no-remote-code-execution stance).

Persistence: a small JSON file under DATA_DIR/mcp_connectors.json holding
`{connector_name: {"enabled": bool, "config": {...}}}`. Secrets that also
exist as environment/Settings values (e.g. a GitHub token) are not
duplicated into this file by default -- connectors fall back to Settings
when a config key is absent, and only get an explicit override written here
when configured via `configure()`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from backend.mcp.base import ConnectorHealth, ConnectorStatus, MCPConnector
from backend.mcp.connectors import BUILTIN_CONNECTORS

logger = logging.getLogger("nexus.mcp.registry")


@dataclass
class ConnectorRecord:
    name: str
    connector_cls: type[MCPConnector]
    enabled: bool = False
    config: dict[str, Any] = field(default_factory=dict)
    instance: Optional[MCPConnector] = field(default=None, repr=False)
    error: Optional[str] = None


class MCPRegistry:
    def __init__(
        self,
        data_dir: Path,
        connector_classes: Optional[dict[str, type[MCPConnector]]] = None,
        default_enabled: Optional[dict[str, bool]] = None,
        default_config: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "mcp_connectors.json"
        self._classes = dict(connector_classes or BUILTIN_CONNECTORS)
        self._default_enabled = default_enabled or {}
        self._default_config = default_config or {}
        self._records: dict[str, ConnectorRecord] = {}
        self._load()

    # ---- Persistence ----------------------------------------------------
    def _load(self) -> None:
        persisted: dict[str, Any] = {}
        if self.config_path.exists():
            try:
                persisted = json.loads(self.config_path.read_text())
            except Exception:
                logger.exception("Failed to read %s; starting from defaults", self.config_path)
                persisted = {}

        for name, cls in self._classes.items():
            saved = persisted.get(name, {})
            enabled = saved.get("enabled", self._default_enabled.get(name, False))
            config = {**self._default_config.get(name, {}), **saved.get("config", {})}
            self._records[name] = ConnectorRecord(name=name, connector_cls=cls, enabled=enabled, config=config)

    def _save(self) -> None:
        payload = {
            name: {"enabled": rec.enabled, "config": rec.config} for name, rec in self._records.items()
        }
        try:
            self.config_path.write_text(json.dumps(payload, indent=2))
        except Exception:
            logger.exception("Failed to persist %s", self.config_path)

    # ---- Records ----------------------------------------------------------
    def records(self) -> list[ConnectorRecord]:
        return sorted(self._records.values(), key=lambda r: r.name)

    def get_record(self, name: str) -> Optional[ConnectorRecord]:
        return self._records.get(name)

    def _get_or_create_instance(self, record: ConnectorRecord) -> MCPConnector:
        if record.instance is None:
            record.instance = record.connector_cls(config=dict(record.config))
        return record.instance

    # ---- Lifecycle --------------------------------------------------------
    async def enable(self, name: str) -> bool:
        record = self._records.get(name)
        if record is None:
            return False
        instance = self._get_or_create_instance(record)
        try:
            await instance.connect()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Connector %s failed to connect", name)
            instance.status = ConnectorStatus.ERROR
            instance.last_error = str(exc)
            record.error = str(exc)
            record.enabled = False
            self._save()
            return False
        record.enabled = True
        record.error = None
        self._save()
        logger.info("MCP connector enabled: %s", name)
        return True

    async def disable(self, name: str) -> bool:
        record = self._records.get(name)
        if record is None or record.instance is None:
            if record is not None:
                record.enabled = False
                self._save()
            return record is not None
        try:
            await record.instance.disconnect()
        except Exception:
            logger.exception("Connector %s raised during disconnect (disabling anyway)", name)
        record.instance.status = ConnectorStatus.DISABLED
        record.enabled = False
        self._save()
        logger.info("MCP connector disabled: %s", name)
        return True

    async def configure(self, name: str, config: dict[str, Any]) -> bool:
        """Merge `config` into this connector's persisted config. If it's
        currently enabled, reconnect so the new config takes effect immediately."""
        record = self._records.get(name)
        if record is None:
            return False
        record.config.update(config)
        if record.instance is not None:
            record.instance.config.update(config)
        self._save()
        if record.enabled:
            return await self.enable(name)
        return True

    async def start_enabled(self) -> None:
        """Connect every connector marked enabled in persisted config.
        Isolated per-connector -- one failing to connect never blocks the rest."""
        for record in self.records():
            if record.enabled:
                await self.enable(record.name)

    async def stop_all(self) -> None:
        for record in self.records():
            if record.instance is not None and record.instance.status == ConnectorStatus.CONNECTED:
                try:
                    await record.instance.disconnect()
                except Exception:
                    logger.exception("Connector %s raised during shutdown disconnect", record.name)

    # ---- Views --------------------------------------------------------
    def list_connectors(self) -> list[dict[str, Any]]:
        out = []
        for record in self.records():
            instance = record.instance
            out.append(
                {
                    "name": record.name,
                    "version": instance.version if instance else record.connector_cls.version,
                    "description": instance.description if instance else record.connector_cls.description,
                    "tags": instance.tags if instance else list(record.connector_cls.tags),
                    "enabled": record.enabled,
                    "status": instance.status.value if instance else ConnectorStatus.DISCONNECTED.value,
                    "error": record.error,
                    "config": _redact(record.config),
                    "tool_count": len(instance.list_tools()) if instance else 0,
                }
            )
        return out

    async def health(self) -> dict[str, ConnectorHealth]:
        out: dict[str, ConnectorHealth] = {}
        for record in self.records():
            if record.instance is None:
                out[record.name] = ConnectorHealth(ConnectorStatus.DISABLED, "not enabled")
                continue
            try:
                out[record.name] = await record.instance.health_check()
            except Exception as exc:  # noqa: BLE001
                out[record.name] = ConnectorHealth(ConnectorStatus.ERROR, f"health check raised: {exc}")
        return out


_SECRET_KEY_HINTS = ("token", "key", "secret", "password", "auth")


def _redact(config: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in config.items():
        if isinstance(v, str) and any(hint in k.lower() for hint in _SECRET_KEY_HINTS) and v:
            out[k] = "***"
        else:
            out[k] = v
    return out
