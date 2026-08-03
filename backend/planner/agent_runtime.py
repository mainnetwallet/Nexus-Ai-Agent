"""
Autonomous Agent Runtime.

A thin orchestration layer on top of TaskQueueService (backend/planner/task_queue.py)
that gives the dashboard/API a single, named lifecycle for "the agent" as a whole --
distinct from per-task pause/resume, which TaskQueueService already handles.

This module never re-implements what TaskQueueService, BrowserEngine, AgentLoop,
or LiveSessionManager already do -- it only composes them and adds the pieces
they don't own:
  - a single Start / Stop / Pause / Resume surface for "the agent" (not one task)
  - persistence of that status across process restarts (AgentRuntimeState row)
  - startup recovery of tasks left mid-flight by an unclean shutdown
  - a rolling "current action / reasoning" view for live monitoring, fed by
    TaskQueueService's activity_fn hook (browser/live URL comes from the
    existing LiveSessionManager -- not duplicated here)
  - runtime statistics (tasks completed/failed, steps executed, recoveries)

Background execution: TaskQueueService already runs its worker loop as a
free-running asyncio.Task (`start_worker`), so once `AgentRuntime.start()`
returns, the agent keeps working through the queue without blocking any
caller -- this class just supervises that loop's lifecycle and status.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Callable, Optional

from sqlalchemy import select

from backend.database.models import AgentRuntimeState, AgentRuntimeStatus, Task, TaskStatus
from backend.database.session import get_session
from backend.planner.task_queue import TaskQueueService

logger = logging.getLogger("nexus.agent_runtime")

_SINGLETON_ID = "singleton"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class AgentRuntime:
    def __init__(
        self,
        queue: TaskQueueService,
        on_activity_broadcast: Optional[Callable[[dict], Any]] = None,
    ) -> None:
        self.queue = queue
        # Optional async callable(dict) -> None, used to fan raw activity events
        # out to a WebSocket (see backend/api/routes_agent.py). Never required.
        self._on_activity_broadcast = on_activity_broadcast
        self._process_started_at: Optional[float] = None
        queue.activity_fn = self._on_activity

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> dict:
        """
        Begin continuous autonomous operation: recover any work left stuck by
        an unclean shutdown, then start (or resume) the background worker
        loop that drains the task queue forever until stopped.
        """
        recovered = await self._recover_interrupted_tasks()
        self.queue.start_worker()  # no-op if already running
        self.queue.resume()  # lift any global pause left over from before
        if self._process_started_at is None:
            self._process_started_at = time.time()
        await self._update(status=AgentRuntimeStatus.RUNNING, started_at=_now(), stopped_at=None)
        logger.info("Agent runtime started (recovered=%d interrupted task(s))", recovered)
        return await self.status()

    async def stop(self) -> dict:
        """
        Stop autonomous operation: cancel whatever task is currently in
        flight (if any) and pause the worker loop so it stops picking up new
        work. The worker coroutine itself keeps running idle (cheap to leave
        alive) so `start()` can resume instantly without recreating it.
        """
        await self._update(status=AgentRuntimeStatus.STOPPING)
        current = self.queue.current_task_id
        if current:
            self.queue.cancel(current)
        self.queue.pause()
        self._process_started_at = None
        await self._update(status=AgentRuntimeStatus.STOPPED, stopped_at=_now())
        logger.info("Agent runtime stopped")
        return await self.status()

    async def pause(self) -> dict:
        """Pause the worker loop and the in-flight task (if any), without cancelling it."""
        self.queue.pause()
        current = self.queue.current_task_id
        if current:
            self.queue.pause_task(current)
        await self._update(status=AgentRuntimeStatus.PAUSED)
        logger.info("Agent runtime paused")
        return await self.status()

    async def resume(self) -> dict:
        """Resume the worker loop and the in-flight task (if any)."""
        self.queue.resume()
        current = self.queue.current_task_id
        if current:
            self.queue.resume_task(current)
        await self._update(status=AgentRuntimeStatus.RUNNING)
        logger.info("Agent runtime resumed")
        return await self.status()

    # ------------------------------------------------------------------ #
    # Recovery
    # ------------------------------------------------------------------ #
    async def _recover_interrupted_tasks(self) -> int:
        """
        Tasks left in PLANNING/RUNNING/PAUSED status are artifacts of an
        unclean shutdown (process killed, crashed, redeployed) -- there is no
        live BrowserEngine or asyncio task backing them anymore in a fresh
        process, so they can never progress on their own. Requeue them so the
        worker loop picks them up as new attempts instead of leaving the
        dashboard showing phantom "running" tasks forever.
        """
        recovered = 0
        async with get_session() as session:
            result = await session.execute(
                select(Task).where(Task.status.in_([TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.PAUSED]))
            )
            stuck = list(result.scalars().all())
            for task in stuck:
                task.status = TaskStatus.QUEUED
                recovered += 1

        if recovered:
            logger.warning("Recovered %d task(s) interrupted by unclean shutdown -> requeued", recovered)
            async with get_session() as session:
                row = await session.get(AgentRuntimeState, _SINGLETON_ID)
                if row is None:
                    row = AgentRuntimeState(id=_SINGLETON_ID)
                    session.add(row)
                    await session.flush()
                row.recoveries_performed = (row.recoveries_performed or 0) + recovered
        return recovered

    # ------------------------------------------------------------------ #
    # Activity feed (structured events pushed from TaskQueueService)
    # ------------------------------------------------------------------ #
    async def _on_activity(self, event: dict) -> None:
        kind = event.get("event")
        fields: dict[str, Any] = {"last_heartbeat_at": _now()}

        if kind == "task_start":
            fields.update(
                current_task_id=event.get("task_id"),
                current_website=event.get("website"),
                current_action="starting",
                current_target=None,
                current_reasoning=None,
            )
        elif kind == "step":
            fields.update(
                current_task_id=event.get("task_id"),
                current_action=event.get("action"),
                current_target=event.get("target"),
                current_reasoning=event.get("reasoning"),
            )
            await self._increment("steps_executed")
        elif kind == "task_finish":
            status = event.get("status")
            if status == "succeeded":
                await self._increment("tasks_completed")
            elif status in ("failed", "cancelled", "blocked"):
                await self._increment("tasks_failed")
            fields.update(current_task_id=None, current_website=None, current_action=None, current_target=None,
                          current_reasoning=event.get("summary"))
        elif kind == "task_crash":
            await self._increment("tasks_failed")
            fields.update(current_action="crashed", current_reasoning=event.get("error", ""))

        await self._update(**fields)
        if self._on_activity_broadcast:
            await self._on_activity_broadcast(event)

    # ------------------------------------------------------------------ #
    # Persistence helpers
    # ------------------------------------------------------------------ #
    async def _increment(self, field: str) -> None:
        async with get_session() as session:
            row = await session.get(AgentRuntimeState, _SINGLETON_ID)
            if row is None:
                row = AgentRuntimeState(id=_SINGLETON_ID)
                session.add(row)
                await session.flush()
            setattr(row, field, (getattr(row, field) or 0) + 1)

    async def _update(self, **fields: Any) -> None:
        async with get_session() as session:
            row = await session.get(AgentRuntimeState, _SINGLETON_ID)
            if row is None:
                row = AgentRuntimeState(id=_SINGLETON_ID)
                session.add(row)
                await session.flush()
            for key, value in fields.items():
                setattr(row, key, value)

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    async def status(self) -> dict:
        async with get_session() as session:
            row = await session.get(AgentRuntimeState, _SINGLETON_ID)
            if row is None:
                row = AgentRuntimeState(id=_SINGLETON_ID)
                session.add(row)
                await session.flush()

            data = {
                "status": row.status.value if row.status else AgentRuntimeStatus.STOPPED.value,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "stopped_at": row.stopped_at.isoformat() if row.stopped_at else None,
                "current_task_id": row.current_task_id,
                "current_website": row.current_website,
                "current_action": row.current_action,
                "current_target": row.current_target,
                "current_reasoning": row.current_reasoning,
                "tasks_completed": row.tasks_completed,
                "tasks_failed": row.tasks_failed,
                "steps_executed": row.steps_executed,
                "recoveries_performed": row.recoveries_performed,
                "last_heartbeat_at": row.last_heartbeat_at.isoformat() if row.last_heartbeat_at else None,
            }

        data["uptime_seconds"] = (time.time() - self._process_started_at) if self._process_started_at else 0
        data["queue"] = self.queue.queue_status()
        return data
