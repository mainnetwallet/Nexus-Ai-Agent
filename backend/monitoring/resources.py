"""
Resource Monitor.

Point-in-time process/system resource snapshot: CPU%, RAM (process +
system), an estimate of browser (Chromium) memory usage, current task
queue depth, and active task count. Uses psutil when available; degrades
gracefully (returns None for a metric) if psutil or a given process
handle isn't available, so this never breaks the dashboard/health page.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

try:
    import psutil
except ImportError:  # pragma: no cover - exercised only when psutil is absent
    psutil = None  # type: ignore[assignment]


@dataclass
class ResourceSnapshot:
    taken_at: float
    cpu_percent: Optional[float]
    process_rss_mb: Optional[float]
    system_memory_percent: Optional[float]
    system_memory_available_mb: Optional[float]
    browser_memory_mb: Optional[float]
    queue_size: int
    active_tasks: int
    psutil_available: bool

    def to_dict(self) -> dict:
        return asdict(self)


class ResourceMonitor:
    def __init__(self, app_state: Any) -> None:
        self.state = app_state
        self._process = psutil.Process(os.getpid()) if psutil else None

    def snapshot(self) -> ResourceSnapshot:
        cpu_percent = None
        process_rss_mb = None
        system_memory_percent = None
        system_memory_available_mb = None

        if psutil and self._process:
            try:
                cpu_percent = psutil.cpu_percent(interval=None)
                process_rss_mb = round(self._process.memory_info().rss / (1024 * 1024), 2)
                vm = psutil.virtual_memory()
                system_memory_percent = vm.percent
                system_memory_available_mb = round(vm.available / (1024 * 1024), 2)
            except Exception:  # noqa: BLE001 - resource reads are best-effort
                pass

        browser_memory_mb = self._estimate_browser_memory()
        queue_size, active_tasks = self._queue_stats()

        return ResourceSnapshot(
            taken_at=time.time(),
            cpu_percent=cpu_percent,
            process_rss_mb=process_rss_mb,
            system_memory_percent=system_memory_percent,
            system_memory_available_mb=system_memory_available_mb,
            browser_memory_mb=browser_memory_mb,
            queue_size=queue_size,
            active_tasks=active_tasks,
            psutil_available=psutil is not None,
        )

    # ------------------------------------------------------------------ #
    def _estimate_browser_memory(self) -> Optional[float]:
        """
        Sums RSS of any chrome/chromium child processes spawned by
        Playwright under this process, if psutil is available. Returns
        None (rather than 0) when there's no active browser, so callers
        can distinguish "no browser" from "measured 0MB".
        """
        if not psutil or not self._process:
            return None
        try:
            total = 0.0
            found = False
            for child in self._process.children(recursive=True):
                try:
                    name = child.name().lower()
                except Exception:  # noqa: BLE001
                    continue
                if "chrome" in name or "chromium" in name or "playwright" in name:
                    found = True
                    total += child.memory_info().rss / (1024 * 1024)
            return round(total, 2) if found else None
        except Exception:  # noqa: BLE001
            return None

    def _queue_stats(self) -> tuple[int, int]:
        """
        Returns (queue_size, active_tasks). queue_size here is a cheap
        in-memory signal (paused task count); use async_snapshot() for the
        accurate, DB-backed queue_size (count of QUEUED tasks).
        """
        queue = getattr(self.state, "queue", None)
        if queue is None:
            return 0, 0
        try:
            status = queue.queue_status()
            queue_size = len(status.get("paused_task_ids", [])) if isinstance(status, dict) else 0
            active_tasks = 1 if getattr(queue, "current_task_id", None) else 0
            return queue_size, active_tasks
        except Exception:  # noqa: BLE001
            return 0, 0

    async def async_snapshot(self) -> ResourceSnapshot:
        """Same as snapshot(), but with an accurate DB-backed queue_size (count of QUEUED tasks)."""
        snap = self.snapshot()
        try:
            from sqlalchemy import func, select

            from backend.database.models import Task, TaskStatus
            from backend.database.session import get_session

            async with get_session() as session:
                result = await session.execute(
                    select(func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED)
                )
                snap.queue_size = int(result.scalar_one())
        except Exception:  # noqa: BLE001
            pass
        return snap
