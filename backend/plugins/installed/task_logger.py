"""
Example plugin, enabled by default: appends a one-line JSON record per task
lifecycle event to `data/plugin_task_log.jsonl`. Serves as a reference
implementation for plugin authors and as a smoke-test fixture for
`backend/tests/test_plugins.py`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from backend.config.settings import DATA_DIR
from backend.plugins.base import NexusPlugin, PluginContext


class TaskLoggerPlugin(NexusPlugin):
    name = "task_logger"
    version = "1.0.0"
    description = "Appends a JSON line per task-lifecycle event to data/plugin_task_log.jsonl"

    def __init__(self) -> None:
        super().__init__()
        self._log_path: Optional[Path] = None

    async def on_load(self, ctx: PluginContext) -> None:
        self._log_path = Path(ctx.config.get("log_path", DATA_DIR / "plugin_task_log.jsonl"))

    async def on_unload(self) -> None:
        self._log_path = None

    def _write(self, record: dict) -> None:
        if self._log_path is None:
            return
        record["ts"] = time.time()
        with self._log_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    async def on_task_start(self, task_id: str, website: str, goal: str) -> None:
        self._write({"event": "task_start", "task_id": task_id, "website": website, "goal": goal})

    async def on_step(self, task_id: str, step) -> None:
        self._write(
            {
                "event": "step",
                "task_id": task_id,
                "index": getattr(step, "index", None),
                "action": getattr(step, "action", None),
                "success": getattr(step, "success", None),
            }
        )

    async def on_task_finish(self, task_id: str, status: str, summary: str) -> None:
        self._write({"event": "task_finish", "task_id": task_id, "status": status, "summary": summary})
