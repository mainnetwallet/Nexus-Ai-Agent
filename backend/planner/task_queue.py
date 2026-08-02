"""
In-process priority task queue backed by the SQLite Task table, driving the
AgentLoop for each task while supporting pause/resume/retry/cancel.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Optional

from sqlalchemy import select

from backend.browser.engine import BrowserEngine
from backend.database.models import Report, Task, TaskStatus
from backend.database.session import get_session
from backend.memory.store import MemoryStore
from backend.planner.agent_loop import AgentLoop
from backend.wallet.manager import WalletManager

logger = logging.getLogger("nexus.queue")


class TaskQueueService:
    def __init__(
        self, memory: MemoryStore, wallet: WalletManager, notify_fn=None, plugin_registry=None, activity_fn=None
    ) -> None:
        self.memory = memory
        self.wallet = wallet
        self.notify_fn = notify_fn  # optional async callable(str) for live progress
        self.plugin_registry = plugin_registry  # optional PluginRegistry, threaded into each task's AgentLoop
        # Optional async callable(dict) fed structured task/step events (event,
        # task_id, website, action, target, reasoning, success, status, ...).
        # Used by AgentRuntime (backend/planner/agent_runtime.py) to maintain a
        # persisted "current action" view for the dashboard, without this class
        # needing to know anything about AgentRuntime or the database row it owns.
        self.activity_fn = activity_fn
        self._paused = asyncio.Event()
        self._paused.set()  # start unpaused (global worker pause)
        self._cancelled_ids: set[str] = set()
        self._worker_task: Optional[asyncio.Task] = None

        # Per-task pause: each running task gets its own asyncio.Event (set =
        # running, cleared = paused). Only populated while a task is actually
        # executing; looked up by id so pause/resume calls for a task that
        # isn't currently running are simply no-ops.
        self._task_pause_events: dict[str, asyncio.Event] = {}

        # Exposed for the live browser session (backend/browser/live_session.py):
        # the BrowserEngine + task id currently being driven by the agent loop,
        # if any. None whenever no task is actively running a browser.
        self.current_engine: Optional[BrowserEngine] = None
        self.current_task_id: Optional[str] = None

    async def enqueue(
        self,
        website: str,
        goal: str,
        wallet_label: str | None,
        notes: str,
        priority: int = 0,
        scheduled_for: dt.datetime | None = None,
    ) -> str:
        async with get_session() as session:
            task = Task(
                website=website,
                goal=goal,
                wallet_label=wallet_label,
                notes=notes,
                priority=priority,
                scheduled_for=scheduled_for,
            )
            session.add(task)
            await session.flush()
            return task.id

    def start_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    def cancel(self, task_id: str) -> None:
        self._cancelled_ids.add(task_id)
        # If the task is currently paused, unblock it so the loop can observe
        # should_cancel() and stop, instead of hanging forever waiting to resume.
        event = self._task_pause_events.get(task_id)
        if event is not None:
            event.set()

    def pause_task(self, task_id: str) -> bool:
        """Pause a single in-flight task. Returns False if it isn't running."""
        event = self._task_pause_events.get(task_id)
        if event is None:
            return False
        event.clear()
        return True

    def resume_task(self, task_id: str) -> bool:
        """Resume a single previously-paused task. Returns False if it isn't running."""
        event = self._task_pause_events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    async def retry(self, task_id: str) -> bool:
        """
        Re-queue a FAILED or CANCELLED task for another run, resetting its
        retry counter. Returns False if the task doesn't exist or is still
        active (queued/planning/running/paused).
        """
        async with get_session() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                return False
            task.status = TaskStatus.QUEUED
            task.retry_count = 0
            self._cancelled_ids.discard(task_id)
            return True

    def queue_status(self) -> dict:
        return {
            "worker_paused": not self._paused.is_set(),
            "active_task_id": self.current_task_id,
            "paused_task_ids": [tid for tid, ev in self._task_pause_events.items() if not ev.is_set()],
        }

    async def _worker_loop(self) -> None:
        while True:
            await self._paused.wait()
            task = await self._pop_next()
            if task is None:
                await asyncio.sleep(2)
                continue
            await self._run_task(task)

    async def _pop_next(self) -> Optional[Task]:
        now = dt.datetime.now(dt.timezone.utc)
        async with get_session() as session:
            result = await session.execute(
                select(Task)
                .where(
                    Task.status == TaskStatus.QUEUED,
                    (Task.scheduled_for.is_(None)) | (Task.scheduled_for <= now),
                )
                .order_by(Task.priority.desc(), Task.created_at.asc())
                .limit(1)
            )
            task = result.scalar_one_or_none()
            if task:
                task.status = TaskStatus.PLANNING
            return task

    async def _run_task(self, task: Task) -> None:
        started = time.time()
        engine = BrowserEngine()
        await engine.start()
        self.current_engine = engine
        self.current_task_id = task.id

        pause_event = asyncio.Event()
        pause_event.set()  # start unpaused
        self._task_pause_events[task.id] = pause_event

        async def on_step(step_result):
            if self.notify_fn:
                await self.notify_fn(
                    f"[{task.website}] step {step_result.index}: {step_result.action} "
                    f"'{step_result.target}' -> {'ok' if step_result.success else 'FAILED'}"
                )
            if self.activity_fn:
                await self.activity_fn(
                    {
                        "event": "step",
                        "task_id": task.id,
                        "website": task.website,
                        "index": step_result.index,
                        "action": step_result.action,
                        "target": step_result.target,
                        "reasoning": step_result.reasoning,
                        "success": step_result.success,
                    }
                )

        async def wait_if_paused():
            if pause_event.is_set():
                return
            async with get_session() as session:
                db_task = await session.get(Task, task.id)
                db_task.status = TaskStatus.PAUSED
            if self.notify_fn:
                await self.notify_fn(f"Task on {task.website} paused.")
            await pause_event.wait()
            if task.id in self._cancelled_ids:
                return  # cancelled while paused; let the loop's should_cancel check handle it
            async with get_session() as session:
                db_task = await session.get(Task, task.id)
                db_task.status = TaskStatus.RUNNING
            if self.notify_fn:
                await self.notify_fn(f"Task on {task.website} resumed.")

        try:
            async with get_session() as session:
                db_task = await session.get(Task, task.id)
                db_task.status = TaskStatus.RUNNING

            if self.activity_fn:
                await self.activity_fn(
                    {"event": "task_start", "task_id": task.id, "website": task.website, "goal": task.goal}
                )

            loop = AgentLoop(
                engine=engine,
                memory=self.memory,
                wallet=self.wallet,
                on_step=on_step,
                should_cancel=lambda: task.id in self._cancelled_ids,
                wait_if_paused=wait_if_paused,
                task_id=task.id,
                plugin_registry=self.plugin_registry,
            )
            outcome = await loop.run(task.website, task.goal, task.wallet_label, task.notes or "")

            async with get_session() as session:
                db_task = await session.get(Task, task.id)
                if outcome.status == "succeeded":
                    db_task.status = TaskStatus.SUCCEEDED
                elif outcome.status == "cancelled" or task.id in self._cancelled_ids:
                    db_task.status = TaskStatus.CANCELLED
                    self._cancelled_ids.discard(task.id)
                else:
                    if db_task.retry_count < db_task.max_retries:
                        db_task.retry_count += 1
                        db_task.status = TaskStatus.QUEUED
                    else:
                        db_task.status = TaskStatus.FAILED

                report = Report(
                    task_id=task.id,
                    status=db_task.status.value,
                    summary=outcome.summary,
                    execution_seconds=time.time() - started,
                    tx_hashes=[],
                    screenshots=[s.screenshot_path for s in outcome.steps],
                )
                session.add(report)

            if self.notify_fn:
                await self.notify_fn(f"Task on {task.website} finished: {outcome.status} - {outcome.summary}")
            if self.activity_fn:
                await self.activity_fn(
                    {"event": "task_finish", "task_id": task.id, "status": outcome.status, "summary": outcome.summary}
                )

        except Exception as exc:
            # Covers browser crashes (Playwright TargetClosedError and similar)
            # as well as any other unexpected failure mid-task. Recovered the
            # same way a normal failed outcome is: retried up to max_retries
            # before being marked FAILED, instead of always giving up after
            # one crash. A fresh BrowserEngine is launched for the retry (see
            # top of this method / `finally` below), so a crashed browser
            # doesn't take future attempts down with it.
            logger.exception("Task %s crashed", task.id)
            async with get_session() as session:
                db_task = await session.get(Task, task.id)
                if db_task.retry_count < db_task.max_retries:
                    db_task.retry_count += 1
                    db_task.status = TaskStatus.QUEUED
                else:
                    db_task.status = TaskStatus.FAILED
                report = Report(
                    task_id=task.id,
                    status=db_task.status.value,
                    summary=f"Crashed: {exc}",
                    execution_seconds=time.time() - started,
                    tx_hashes=[],
                    screenshots=[],
                )
                session.add(report)
            if self.notify_fn:
                await self.notify_fn(f"Task on {task.website} crashed: {exc}")
            if self.activity_fn:
                await self.activity_fn({"event": "task_crash", "task_id": task.id, "error": str(exc)})
        finally:
            self._task_pause_events.pop(task.id, None)
            if self.current_engine is engine:
                self.current_engine = None
                self.current_task_id = None
            await engine.stop()
