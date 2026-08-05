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
from backend.config.settings import settings
from backend.database.models import Report, Task, TaskStatus, TaskStep
from backend.database.session import get_session
from backend.identity.manager import ProfileManager
from backend.identity.registry import ProfileBusyError, ProfileError, ProfileNotFoundError
from backend.memory.store import MemoryStore
from backend.planner.agent_loop import AgentLoop
from backend.skills.library import SkillService
from backend.skills.matcher import SkillMatcher
from backend.skills.runner import SkillRunner
from backend.wallet.manager import WalletManager

logger = logging.getLogger("nexus.queue")


class TaskQueueService:
    def __init__(
        self,
        memory: MemoryStore,
        wallet: WalletManager,
        notify_fn=None,
        plugin_registry=None,
        activity_fn=None,
        skills: Optional[SkillService] = None,
        mcp=None,
        profiles: Optional[ProfileManager] = None,
        max_concurrent_tasks: Optional[int] = None,
    ) -> None:
        self.memory = memory
        self.wallet = wallet
        # Identity & Profile Manager (backend/identity/): optional. When
        # provided, a task enqueued with profile_label set gets its
        # BrowserEngine launched against that profile's own persistent
        # Chrome profile directory (reusing cookies/sessions/extensions/
        # login state) and a Gmail/X/Discord session check before the
        # AgentLoop starts. None (the default) fully restores prior
        # behavior -- a profile_label is accepted but ignored.
        self.profiles = profiles
        self.notify_fn = notify_fn  # optional async callable(str) for live progress
        self.plugin_registry = plugin_registry  # optional PluginRegistry, threaded into each task's AgentLoop
        # Optional MCPManager (backend/mcp/manager.py). Threaded into both
        # AgentLoop and SkillRunner so a plan step / taught skill step can
        # invoke an MCP tool (filesystem/terminal/browser/github). None (the
        # default) fully restores prior behavior -- no mcp_tool action available.
        self.mcp = mcp
        # Optional async callable(dict) fed structured task/step events (event,
        # task_id, website, action, target, reasoning, success, status, ...).
        # Used by AgentRuntime (backend/planner/agent_runtime.py) to maintain a
        # persisted "current action" view for the dashboard, without this class
        # needing to know anything about AgentRuntime or the database row it owns.
        self.activity_fn = activity_fn
        # Skill Learning System (backend/skills/): optional. When provided,
        # every task first goes through SkillMatcher.find_match() before any
        # planning happens -- a matching enabled skill is replayed
        # deterministically via SkillRunner, falling back to normal AgentLoop
        # planning only if there's no match or the replay fails partway
        # through. Also used to register "save this as a skill?" suggestions
        # after a task succeeds. None (the default) fully restores prior
        # behavior -- pure AgentLoop planning, no skill matching/suggestions.
        self.skills = skills
        self.skill_matcher = SkillMatcher(skills) if skills else None
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
        # if any. None whenever no task is actively running a browser. Kept
        # as the *most recently started* running task for backward
        # compatibility with existing Single Task Control (pause/resume/
        # cancel) call sites -- those still target "the" current task.
        self.current_engine: Optional[BrowserEngine] = None
        self.current_task_id: Optional[str] = None

        # Multi-Profile Browser Management: every task the worker loop has
        # actually started gets an entry here for as long as it's driving a
        # browser, keyed by task_id. This is what lets several tasks --
        # each against its own Chrome Profile -- run truly concurrently
        # instead of one at a time. profile_id is None for tasks that ran
        # without an Identity & Profile Manager profile (throwaway context).
        self.running: dict[str, dict] = {}
        # How many tasks the worker loop will drive with live BrowserEngines
        # at once. A given Chrome Profile can still only be claimed by one
        # of them at a time (see ProfileRegistry.try_lock) -- this cap is
        # about total concurrent browser instances, not a per-profile limit.
        self.max_concurrent_tasks = max_concurrent_tasks or settings.max_concurrent_profile_tasks
        self._active_workers: dict[str, asyncio.Task] = {}

    async def enqueue(
        self,
        website: str,
        goal: str,
        wallet_label: str | None,
        notes: str,
        priority: int = 0,
        scheduled_for: dt.datetime | None = None,
        profile_label: str | None = None,
    ) -> str:
        # Multi-Profile Browser Management: this is the single choke point
        # every entry point (chat, Telegram /task, REST API) goes through,
        # so auto-selecting a profile here -- rather than duplicating the
        # choice in each caller -- covers all of them. ChatEngine already
        # resolves its own choice before calling enqueue() (it needs the
        # picked name to tell the user), so this is a no-op there since
        # profile_label is already set by the time it arrives.
        if not profile_label and self.profiles is not None:
            available = await self.profiles.registry.list_profiles(enabled_only=True)
            best = ProfileManager.choose_best_profile(available, website=website)
            if best is not None:
                profile_label = best["name"]

        async with get_session() as session:
            task = Task(
                website=website,
                goal=goal,
                wallet_label=wallet_label,
                profile_label=profile_label,
                notes=notes,
                priority=priority,
                scheduled_for=scheduled_for,
            )
            session.add(task)
            await session.flush()
            return task.id

    async def _save_report(
        self,
        session,
        task_id: str,
        status: str,
        summary: str,
        execution_seconds: float,
        tx_hashes: list,
        screenshots: list,
    ) -> None:
        """Upsert the Report row for a task. Report.task_id is unique (one
        row per task by design), but a task can run through multiple
        attempts via the retry path (a failed/crashed attempt with
        retry_count < max_retries goes back to QUEUED and runs again with
        the *same* task.id). Each attempt used to unconditionally INSERT a
        new Report, so the second attempt's insert violated the unique
        constraint and crashed the task permanently instead of retrying it
        (sqlite3.IntegrityError: UNIQUE constraint failed: reports.task_id).
        Updating the existing row in place instead means the report always
        reflects the most recent attempt, without ever double-inserting."""
        existing = await session.scalar(select(Report).where(Report.task_id == task_id))
        if existing is not None:
            existing.status = status
            existing.summary = summary
            existing.execution_seconds = execution_seconds
            existing.tx_hashes = tx_hashes
            existing.screenshots = screenshots
            existing.created_at = dt.datetime.now(dt.timezone.utc)
        else:
            session.add(
                Report(
                    task_id=task_id,
                    status=status,
                    summary=summary,
                    execution_seconds=execution_seconds,
                    tx_hashes=tx_hashes,
                    screenshots=screenshots,
                )
            )

    def start_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    async def cancel(self, task_id: str) -> bool:
        """
        Cancel a task. If it's actively running/paused in *this* process,
        flip the in-memory flag (and unblock it if paused) so the AgentLoop
        notices on its next should_cancel() check -- the DB row gets set to
        CANCELLED by _run_task once it unwinds.

        If there's no live event for it -- either it's still QUEUED and
        hasn't been picked up yet, or it's an orphaned row left in
        PLANNING/RUNNING/PAUSED by a worker that's no longer around (e.g. a
        dead coroutine, or a process that restarted before the startup
        recovery pass ran) -- nothing will ever observe the flag, so cancel
        it directly in the DB instead of silently no-op'ing.
        """
        self._cancelled_ids.add(task_id)
        event = self._task_pause_events.get(task_id)
        if event is not None:
            event.set()
            return True
        async with get_session() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return False
            if task.status in (
                TaskStatus.QUEUED,
                TaskStatus.PLANNING,
                TaskStatus.RUNNING,
                TaskStatus.PAUSED,
            ):
                task.status = TaskStatus.CANCELLED
        self._cancelled_ids.discard(task_id)
        return True

    def pause_task(self, task_id: str) -> bool:
        """Pause a single in-flight task. Returns False if it isn't running."""
        event = self._task_pause_events.get(task_id)
        if event is None:
            return False
        event.clear()
        return True

    async def resume_task(self, task_id: str) -> bool:
        """
        Resume a single previously-paused task. If it's still actively
        waiting in this process, unblock its wait_if_paused() the normal
        way. If no live event exists for it (an orphaned PAUSED row -- same
        causes as in cancel() above), requeue it directly so the worker
        loop picks it back up as a fresh attempt instead of leaving it
        stuck forever.
        """
        event = self._task_pause_events.get(task_id)
        if event is not None:
            event.set()
            return True
        async with get_session() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status != TaskStatus.PAUSED:
                return False
            task.status = TaskStatus.QUEUED
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
            # Full multi-profile picture: every task currently driving a
            # live BrowserEngine, not just the single "current" one above.
            "running_tasks": [
                {"task_id": tid, "profile_id": info.get("profile_id"), "website": info.get("website")}
                for tid, info in self.running.items()
            ],
            "concurrency": {"active": len(self.running), "max": self.max_concurrent_tasks},
        }

    def get_engine_for_profile(self, profile_id: str) -> Optional[BrowserEngine]:
        """Looks up the live BrowserEngine currently driving a task loaded
        against this Chrome Profile, if any -- used by routes_profiles.py
        so 'check sessions now' / the manual-open guard work correctly even
        when several profiles are running at once (not just whichever
        happens to be self.current_engine)."""
        for info in self.running.values():
            if info.get("profile_id") == profile_id:
                return info.get("engine")
        return None

    def get_engine_for_task(self, task_id: str) -> Optional[BrowserEngine]:
        info = self.running.get(task_id)
        return info.get("engine") if info else None

    async def _worker_loop(self) -> None:
        while True:
            await self._paused.wait()
            # Reap any finished per-task worker coroutines so their slot
            # counts as free for the concurrency check below.
            for tid in [t for t, w in self._active_workers.items() if w.done()]:
                self._active_workers.pop(tid, None)

            if len(self._active_workers) >= self.max_concurrent_tasks:
                await asyncio.sleep(1)
                continue

            locked_profile_ids = {info["profile_id"] for info in self.running.values() if info.get("profile_id")}
            task = await self._pop_next(locked_profile_ids)
            if task is None:
                await asyncio.sleep(2)
                continue

            worker = asyncio.create_task(self._run_task(task))
            self._active_workers[task.id] = worker

    async def _pop_next(self, locked_profile_ids: set[str] = frozenset()) -> Optional[Task]:
        """
        Picks the next eligible QUEUED task -- highest priority, then oldest
        first -- skipping over any task whose profile_label currently
        resolves to a profile another running task already has locked, so
        the worker loop doesn't try to double-book a Chrome Profile. Tasks
        with no profile_label (or whose profile isn't currently locked) are
        always eligible. A whole batch is fetched (not just one row) so a
        busy-profile task at the front of the queue doesn't block eligible
        tasks behind it from starting in other profiles.
        """
        now = dt.datetime.now(dt.timezone.utc)
        async with get_session() as session:
            result = await session.execute(
                select(Task)
                .where(
                    Task.status == TaskStatus.QUEUED,
                    (Task.scheduled_for.is_(None)) | (Task.scheduled_for <= now),
                )
                .order_by(Task.priority.desc(), Task.created_at.asc())
                .limit(50)
            )
            candidates = list(result.scalars().all())

            chosen: Optional[Task] = None
            if locked_profile_ids and self.profiles is not None:
                for candidate in candidates:
                    if not candidate.profile_label:
                        chosen = candidate
                        break
                    resolved = await self.profiles.registry.resolve(candidate.profile_label)
                    if resolved is None or resolved.id not in locked_profile_ids:
                        chosen = candidate
                        break
            elif candidates:
                chosen = candidates[0]

            if chosen:
                chosen.status = TaskStatus.PLANNING
            return chosen

    async def _run_task(self, task: Task) -> None:
        started = time.time()

        # --- Identity & Profile Manager: steps 1-3 of the load sequence ---
        # (load profile -> resolve wallet label -> resolve Chrome profile
        # dir), done *before* launching the browser so a bad profile
        # reference fails fast instead of wasting a browser launch.
        loaded_profile = None
        effective_wallet_label = task.wallet_label
        if task.profile_label and self.profiles is not None:
            try:
                loaded_profile = await self.profiles.load_for_task(task.profile_label)
                effective_wallet_label = task.wallet_label or loaded_profile.wallet_label
            except ProfileBusyError as exc:
                # Race with another task that grabbed this profile between
                # _pop_next's check and this load -- not a failure, just bad
                # timing. Put it back to QUEUED (no retry_count charged) so
                # the worker loop picks it up again once the profile frees.
                logger.info("Profile busy for task %s (%s), requeueing: %s", task.id, task.profile_label, exc)
                async with get_session() as session:
                    db_task = await session.get(Task, task.id)
                    db_task.status = TaskStatus.QUEUED
                return
            except (ProfileNotFoundError, ProfileError) as exc:
                logger.warning("Profile load failed for task %s (%s): %s", task.id, task.profile_label, exc)
                if self.notify_fn:
                    await self.notify_fn(f"Couldn't load profile {task.profile_label!r}: {exc}")
                async with get_session() as session:
                    db_task = await session.get(Task, task.id)
                    db_task.status = TaskStatus.FAILED
                    await self._save_report(
                        session,
                        task.id,
                        status=TaskStatus.FAILED.value,
                        summary=f"Profile load failed: {exc}",
                        execution_seconds=time.time() - started,
                        tx_hashes=[],
                        screenshots=[],
                    )
                return
        elif task.profile_label and self.profiles is None:
            logger.warning("Task %s references profile_label=%s but no ProfileManager is configured -- ignoring", task.id, task.profile_label)

        engine = BrowserEngine(user_data_dir=loaded_profile.chrome_profile_dir if loaded_profile else None)
        await engine.start()
        self.current_engine = engine
        self.current_task_id = task.id
        self.running[task.id] = {
            "engine": engine,
            "profile_id": loaded_profile.id if loaded_profile else None,
            "website": task.website,
        }

        # --- Identity & Profile Manager: steps 4-8 (detect Gmail/X/Discord ---
        # --- login, notify if any configured service isn't authenticated) ---
        if loaded_profile is not None:
            try:
                await self.profiles.check_sessions(loaded_profile, engine, notify_fn=self.notify_fn)
            except Exception:
                logger.exception("Session check failed for profile %s on task %s", loaded_profile.name, task.id)

        pause_event = asyncio.Event()
        pause_event.set()  # start unpaused
        self._task_pause_events[task.id] = pause_event

        async def on_step(step_result):
            if self.notify_fn:
                status_str = 'ok' if step_result.success else 'FAILED'
                msg = (
                    f"[{task.website}] step {step_result.index}: {step_result.action} "
                    f"'{step_result.target}' -> {status_str}"
                )
                await self.notify_fn(msg)
                # Send detailed error notification so user can reply with fix advice
                if not step_result.success and step_result.note:
                    await self.notify_fn(
                        f"⚠️ Error detail: {step_result.note}. "
                        f"Reply in chat with fix advice to retry (e.g. 'click Join button instead' or 'scroll down')."
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
            # Durable per-step record (TaskStep was previously defined but
            # never written to). Used by the Skill Learning System to turn a
            # successful task's steps into a skill workflow -- both for the
            # post-task "save as skill?" suggestion and for "record this as
            # a skill" pulled from a live/finished task on demand.
            try:
                async with get_session() as session:
                    session.add(
                        TaskStep(
                            task_id=task.id,
                            index=step_result.index,
                            action=step_result.action,
                            target_description=step_result.target,
                            value=step_result.value,
                            reasoning=step_result.reasoning,
                            result=step_result.note or None,
                            success=step_result.success,
                            screenshot_path=step_result.screenshot_path or None,
                        )
                    )
            except Exception:
                logger.exception("Failed to persist TaskStep for task %s step %s", task.id, step_result.index)

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

            # --- Skill Learning System: search the Skill Library before ---
            # --- planning any task. Execute a matching skill if found,   ---
            # --- otherwise fall through to normal AgentLoop planning.    ---
            matched_skill = None
            if self.skill_matcher is not None:
                try:
                    matched_skill = await self.skill_matcher.find_match(task.goal, task.website)
                except Exception:
                    logger.exception("Skill matching failed for task %s; planning normally", task.id)

            outcome = None
            used_skill_id: Optional[str] = None
            if matched_skill is not None:
                if self.notify_fn:
                    await self.notify_fn(
                        f"Matched learned skill '{matched_skill['name']}' for this task -- replaying it."
                    )
                skill_outcome = await SkillRunner(engine, mcp=self.mcp).run(matched_skill, task.website, on_step=on_step)
                used_skill_id = matched_skill["id"]
                if self.skills is not None:
                    await self.skills.record_usage(matched_skill["id"], success=skill_outcome.status == "succeeded")
                if skill_outcome.status == "succeeded":
                    outcome = skill_outcome
                elif self.notify_fn:
                    await self.notify_fn(
                        f"Skill '{matched_skill['name']}' didn't complete the task -- falling back to normal planning."
                    )

            if outcome is None:
                loop = AgentLoop(
                    engine=engine,
                    memory=self.memory,
                    wallet=self.wallet,
                    on_step=on_step,
                    should_cancel=lambda: task.id in self._cancelled_ids,
                    wait_if_paused=wait_if_paused,
                    task_id=task.id,
                    plugin_registry=self.plugin_registry,
                    mcp=self.mcp,
                )
                outcome = await loop.run(task.website, task.goal, effective_wallet_label, task.notes or "")

            # A successful run that *wasn't* already a skill replay is a
            # candidate to learn from -- register it so the user (chat,
            # Telegram, or the Skills dashboard page) can be asked whether
            # to save it as a reusable skill. See backend/skills/library.py
            # SkillService.register_pending/confirm_pending.
            if self.skills is not None and used_skill_id is None and outcome.status == "succeeded" and outcome.steps:
                self.skills.register_pending(
                    task.id,
                    {
                        "name": task.goal[:80],
                        "description": f"Learned from a successful task on {task.website}: {task.goal}",
                        "category": "general",
                        "trigger": task.goal,
                        "website_hint": task.website,
                        "workflow": [
                            {
                                "action": s.action,
                                "target": s.target,
                                "value": s.value,
                                "description": s.reasoning,
                            }
                            for s in outcome.steps
                        ],
                    },
                )
                if self.notify_fn:
                    await self.notify_fn(
                        "That completed successfully -- I can save it as a reusable skill for next time. "
                        "Say 'save as skill' if you'd like, or 'discard' to skip."
                    )

            async with get_session() as session:
                db_task = await session.get(Task, task.id)
                if outcome.status == "succeeded":
                    db_task.status = TaskStatus.SUCCEEDED
                elif outcome.status == "paused":
                    db_task.status = TaskStatus.PAUSED
                elif outcome.status == "cancelled" or task.id in self._cancelled_ids:
                    db_task.status = TaskStatus.CANCELLED
                    self._cancelled_ids.discard(task.id)
                else:
                    if db_task.retry_count < db_task.max_retries:
                        db_task.retry_count += 1
                        db_task.status = TaskStatus.QUEUED
                    else:
                        db_task.status = TaskStatus.FAILED

                await self._save_report(
                    session,
                    task.id,
                    status=db_task.status.value,
                    summary=outcome.summary,
                    execution_seconds=time.time() - started,
                    tx_hashes=[],
                    screenshots=[s.screenshot_path for s in outcome.steps],
                )

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
                await self._save_report(
                    session,
                    task.id,
                    status=db_task.status.value,
                    summary=f"Crashed: {exc}",
                    execution_seconds=time.time() - started,
                    tx_hashes=[],
                    screenshots=[],
                )
            if self.notify_fn:
                await self.notify_fn(f"Task on {task.website} crashed: {exc}")
            if self.activity_fn:
                await self.activity_fn({"event": "task_crash", "task_id": task.id, "error": str(exc)})
        finally:
            self._task_pause_events.pop(task.id, None)
            self.running.pop(task.id, None)
            if self.current_engine is engine:
                # Fall back to another still-running task, if any, so
                # Single Task Control (pause/resume/cancel) keeps pointing
                # at a live task instead of going stale while other
                # profiles' tasks are still running.
                other = next(iter(self.running.items()), None)
                self.current_engine = other[1]["engine"] if other else None
                self.current_task_id = other[0] if other else None
            await engine.stop()
            if loaded_profile is not None and self.profiles is not None:
                try:
                    await self.profiles.release(loaded_profile.id)
                except Exception:
                    logger.exception("Failed to release profile %s after task %s", loaded_profile.name, task.id)
