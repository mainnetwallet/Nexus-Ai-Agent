"""
PluginRegistry: discovers plugin modules on disk, manages their lifecycle
(load/enable/disable/reload), and dispatches lifecycle hooks to every
enabled plugin with per-plugin error isolation.

Discovery is intentionally narrow and does not accept code over the network
or the REST API -- only `.py` files already present under `plugins_dir`
(default `backend/plugins/installed/`) are ever imported. Enabling code
execution via an API call would turn the wallet-approval and task-execution
surfaces into a remote-code-execution target, which is exactly the kind of
scope creep `backend/wallet/import_utils.py` and `backend/wallet/manager.py`
were written to avoid for key material. The plugin API endpoints
(`backend/api/routes_plugins.py`) can only list/enable/disable/reload what's
already on disk.

Dispatch semantics:
- `on_load` / `on_unload`: awaited, once, when a plugin is enabled/disabled.
- `on_task_start` / `on_step` / `on_task_finish`: fire-and-isolated -- each
  enabled plugin's hook is awaited in turn (so ordering is deterministic for
  tests and logs), but a raised exception is caught, logged, and the plugin
  is *not* disabled -- one broken hook must never take down the task loop or
  other plugins.
- `on_wallet_popup`: also isolated the same way, but the return value is
  meaningful: any enabled plugin returning `False` flips the final decision
  to "do not approve". No plugin can flip a reject into an approve.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from backend.plugins.base import NexusPlugin, PluginContext

logger = logging.getLogger("nexus.plugins")


@dataclass
class PluginRecord:
    name: str
    version: str
    description: str
    module_path: str
    enabled: bool = False
    error: Optional[str] = None
    instance: Optional[NexusPlugin] = field(default=None, repr=False)


class PluginRegistry:
    def __init__(
        self,
        plugins_dir: Path,
        memory: Any = None,
        notify_fn: Any = None,
        config: Optional[dict] = None,
        event_fn: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self.plugins_dir = Path(plugins_dir)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.memory = memory
        self.notify_fn = notify_fn
        self._config = config or {}  # {plugin_name: {...}}
        self._records: dict[str, PluginRecord] = {}
        # Optional async callable(json_str) -> None, used to fan lifecycle
        # and dispatch events out to backend/api/routes_plugins.py's
        # `WS /api/plugins/ws/live`. None (the default) means "no live
        # event stream" -- every existing call site that doesn't pass this
        # keyword sees identical behavior to before it existed.
        self._event_fn = event_fn

    async def _emit(self, event_type: str, **fields: Any) -> None:
        if self._event_fn is None:
            return
        payload = {"type": event_type, "at": time.time(), **fields}
        try:
            await self._event_fn(json.dumps(payload))
        except Exception:
            logger.exception("Plugin event broadcast failed for event %s", event_type)

    # ---- Discovery ----------------------------------------------------
    def discover(self) -> list[str]:
        """
        Scan `plugins_dir` for `*.py` files (excluding `_`-prefixed / dunder
        files), import each as a module, and register the single
        `NexusPlugin` subclass it defines. Does not enable anything.
        Returns the list of newly discovered plugin names.
        """
        discovered: list[str] = []
        for path in sorted(self.plugins_dir.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            try:
                plugin_cls = self._import_plugin_class(path)
            except Exception as exc:
                logger.exception("Failed to import plugin module %s", path)
                self._records[path.stem] = PluginRecord(
                    name=path.stem,
                    version="unknown",
                    description="",
                    module_path=str(path),
                    enabled=False,
                    error=f"import failed: {exc}",
                )
                continue

            instance = plugin_cls()
            record = PluginRecord(
                name=instance.name,
                version=instance.version,
                description=instance.description,
                module_path=str(path),
                enabled=False,
                instance=instance,
            )
            self._records[instance.name] = record
            discovered.append(instance.name)
        return discovered

    @staticmethod
    def _import_plugin_class(path: Path) -> type[NexusPlugin]:
        # Compile the source directly rather than going through
        # importlib.util's default SourceFileLoader: that loader caches
        # compiled bytecode in __pycache__ keyed by the source file's mtime,
        # and on fast successive writes (e.g. a plugin author editing and
        # immediately calling /api/plugins/{name}/reload) two writes can
        # land within the same mtime tick and silently serve stale bytecode.
        # Reading + compiling fresh every time makes reload() always see
        # exactly what's on disk right now.
        source = path.read_text()
        module_name = f"nexus_plugin_{path.stem}"
        spec = importlib.util.spec_from_loader(module_name, loader=None, origin=str(path))
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(path)
        sys.modules[module_name] = module
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)

        candidates = [
            obj
            for obj in vars(module).values()
            if isinstance(obj, type) and issubclass(obj, NexusPlugin) and obj is not NexusPlugin
        ]
        if not candidates:
            raise ImportError(f"{path} does not define a NexusPlugin subclass")
        if len(candidates) > 1:
            raise ImportError(
                f"{path} defines {len(candidates)} NexusPlugin subclasses; a plugin file must define exactly one"
            )
        return candidates[0]

    # ---- Lifecycle ------------------------------------------------------
    async def load_all(self, *, enable: bool = True) -> None:
        """Discover every plugin on disk and, if `enable`, turn each one on."""
        self.discover()
        if enable:
            for name in list(self._records):
                if self._records[name].error is None:
                    await self.enable(name)

    async def enable(self, name: str) -> bool:
        record = self._records.get(name)
        if record is None or record.instance is None:
            return False
        if record.enabled:
            return True
        ctx = PluginContext(memory=self.memory, notify_fn=self.notify_fn, config=self._config.get(name, {}))
        try:
            await record.instance.on_load(ctx)
        except Exception as exc:
            logger.exception("Plugin %s failed on_load", name)
            record.error = f"on_load failed: {exc}"
            record.enabled = False
            return False
        record.enabled = True
        record.error = None
        logger.info("Plugin enabled: %s v%s", record.name, record.version)
        await self._emit("plugin_enabled", name=record.name, version=record.version)
        return True

    async def disable(self, name: str) -> bool:
        record = self._records.get(name)
        if record is None or record.instance is None or not record.enabled:
            return False
        try:
            await record.instance.on_unload()
        except Exception:
            logger.exception("Plugin %s raised during on_unload (disabling anyway)", name)
        record.enabled = False
        logger.info("Plugin disabled: %s", name)
        await self._emit("plugin_disabled", name=name)
        return True

    async def reload(self, name: str) -> bool:
        """Unload (if enabled), re-import the module fresh, and re-enable."""
        record = self._records.get(name)
        if record is None:
            return False
        was_enabled = record.enabled
        if record.enabled:
            await self.disable(name)
        try:
            plugin_cls = self._import_plugin_class(Path(record.module_path))
        except Exception as exc:
            logger.exception("Plugin %s failed to reload", name)
            record.error = f"reload failed: {exc}"
            await self._emit("plugin_reload_failed", name=name, error=str(exc))
            return False
        instance = plugin_cls()
        record.instance = instance
        record.version = instance.version
        record.description = instance.description
        record.error = None
        ok = await self.enable(name) if was_enabled else True
        await self._emit("plugin_reloaded", name=name, version=instance.version, re_enabled=was_enabled)
        return ok

    async def unload_all(self) -> None:
        for name in list(self._records):
            if self._records[name].enabled:
                await self.disable(name)

    def list_plugins(self) -> list[dict]:
        return [
            {
                "name": r.name,
                "version": r.version,
                "description": r.description,
                "enabled": r.enabled,
                "error": r.error,
            }
            for r in sorted(self._records.values(), key=lambda r: r.name)
        ]

    def _enabled_instances(self) -> list[NexusPlugin]:
        return [r.instance for r in self._records.values() if r.enabled and r.instance is not None]

    # ---- Dispatch --------------------------------------------------------
    async def dispatch_task_start(self, task_id: str, website: str, goal: str) -> None:
        for plugin in self._enabled_instances():
            await self._isolated(plugin, "on_task_start", task_id, website, goal)
        await self._emit("task_start", task_id=task_id, website=website, goal=goal)

    async def dispatch_step(self, task_id: str, step: Any) -> None:
        for plugin in self._enabled_instances():
            await self._isolated(plugin, "on_step", task_id, step)
        await self._emit(
            "task_step",
            task_id=task_id,
            index=getattr(step, "index", None),
            action=getattr(step, "action", None),
            target=getattr(step, "target", None),
            success=getattr(step, "success", None),
        )

    async def dispatch_task_finish(self, task_id: str, status: str, summary: str) -> None:
        for plugin in self._enabled_instances():
            await self._isolated(plugin, "on_task_finish", task_id, status, summary)
        await self._emit("task_finish", task_id=task_id, status=status, summary=summary)

    async def dispatch_wallet_popup(
        self, task_id: str, contract_address: Optional[str], estimated_value: Optional[float], approve: bool
    ) -> bool:
        """Runs on_wallet_popup on every enabled plugin; any False vetoes approval."""
        final = approve
        for plugin in self._enabled_instances():
            result = await self._isolated(
                plugin, "on_wallet_popup", task_id, contract_address, estimated_value, final
            )
            if result is False:
                final = False
        await self._emit(
            "wallet_popup",
            task_id=task_id,
            contract_address=contract_address,
            estimated_value=estimated_value,
            initial_decision=approve,
            final_decision=final,
        )
        return final

    async def _isolated(self, plugin: NexusPlugin, hook_name: str, *args: Any) -> Any:
        try:
            hook = getattr(plugin, hook_name)
            return await hook(*args)
        except Exception:
            logger.exception("Plugin %s raised in %s (isolated, continuing)", plugin.name, hook_name)
            return None
