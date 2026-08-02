"""
Telegram remote-control bot for Nexus-Agent.

Supports the fixed command set plus free-form natural language, which is
routed through the LLM to extract an intent (start task / pause / resume /
report / etc.) so the user can type things like "pause the browser" or
"complete all tasks on this site" directly.
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from backend.config.settings import settings
from backend.planner.llm_client import LLMClient
from backend.planner.task_queue import TaskQueueService

logger = logging.getLogger("nexus.telegram")

# Free-form messages are routed through the LLM into one of these intents so
# the bot behaves as a full conversational interface rather than a fixed
# command list -- "how's the browser doing", "give me a report", "pause
# everything" all resolve to the same handlers the slash commands use.
INTENT_SYSTEM_PROMPT = """You route free-form Telegram messages sent to an autonomous browser-automation \
agent into a structured intent. Respond with STRICT JSON only, no prose, no markdown fences:
{
  "intent": "start_task | pause | resume | stop | restart | status | tasks | report | logs | screenshot | \
health | diagnostics | resources | browser_status | unknown",
  "website": "url if mentioned, else empty",
  "goal": "goal description if this is a start_task intent, else empty",
  "wallet_label": "wallet label if mentioned, else empty"
}

Guidance:
- "how are you doing / is everything healthy / any errors" -> health
- "run diagnostics / check the environment / is playwright working" -> diagnostics
- "cpu / memory / ram / how many tasks are queued" -> resources
- "what's the browser doing / is it on a page right now" -> browser_status
- "give me a summary / report on the last task" -> report
- "restart / reboot the agent" -> restart
- "what tasks do you have / show me the queue" -> tasks
- If nothing matches, use "unknown" rather than guessing."""


def _is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not settings.allowed_telegram_ids:
        return True  # no allowlist configured -> open (development mode)
    return user is not None and user.id in settings.allowed_telegram_ids


def auth_required(handler):
    """
    Wraps a command/message handler so it always checks _is_authorized()
    first. Every handler in this bot goes through this decorator -- prior to
    this, several commands (pause/resume/stop/logs/screenshot/settings/
    memory/tasks/browser/status) had NO auth check at all, so anyone who
    could message the bot could pause the queue or read logs/screenshots
    even with TELEGRAM_ALLOWED_USER_IDS configured. Do not add a new command
    method that skips this decorator.
    """

    @functools.wraps(handler)
    async def wrapped(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            if update.message:
                await update.message.reply_text("Not authorized.")
            logger.warning(
                "Unauthorized Telegram access attempt: user_id=%s handler=%s",
                update.effective_user.id if update.effective_user else "unknown",
                handler.__name__,
            )
            return
        return await handler(self, update, context)

    return wrapped


class NexusTelegramBot:
    def __init__(self, queue: TaskQueueService, app_state: Optional[Any] = None) -> None:
        self.queue = queue
        # Optional backend.api.app_state.AppState -- when provided, lets the
        # bot answer /status, /report, /browser, /tasks, /health,
        # /diagnostics and /resources with real live data instead of just
        # pointing the user at the REST API. Entirely optional so existing
        # callers that only pass `queue` keep working unchanged.
        self.app_state = app_state
        self.llm = LLMClient()
        self.app: Optional[Application] = None

    def build(self) -> Application:
        app = Application.builder().token(settings.telegram_bot_token).build()

        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("task", self.cmd_task))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("stop", self.cmd_stop))
        app.add_handler(CommandHandler("report", self.cmd_report))
        app.add_handler(CommandHandler("logs", self.cmd_logs))
        app.add_handler(CommandHandler("screenshot", self.cmd_screenshot))
        app.add_handler(CommandHandler("memory", self.cmd_memory))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CommandHandler("tasks", self.cmd_tasks))
        app.add_handler(CommandHandler("browser", self.cmd_browser))
        app.add_handler(CommandHandler("health", self.cmd_health))
        app.add_handler(CommandHandler("diagnostics", self.cmd_diagnostics))
        app.add_handler(CommandHandler("resources", self.cmd_resources))
        app.add_handler(CommandHandler("restart", self.cmd_restart))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_free_text))

        self.app = app
        return app

    async def notify(self, chat_id: int, text: str) -> None:
        if self.app:
            await self.app.bot.send_message(chat_id=chat_id, text=text)

    # ---------------------------------------------------------------- #
    # Commands
    # ---------------------------------------------------------------- #
    @auth_required
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Nexus-Agent online. Send /task <website> | <goal> | <wallet> or just describe what you want done."
        )

    @auth_required
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "/task <website> | <goal> | <wallet>\n"
            "/status /pause /resume /stop /restart\n"
            "/report /logs /screenshot /memory\n"
            "/tasks /settings /browser\n"
            "/health /diagnostics /resources\n"
            "Or just type naturally, e.g. 'complete all tasks on https://example.com using Wallet-01', "
            "'how's everything doing?', 'restart the agent', 'give me a report'."
        )

    @auth_required
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_status())

    @auth_required
    async def cmd_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args)
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2:
            await update.message.reply_text("Format: /task <website> | <goal> | <wallet optional>")
            return
        website, goal = parts[0], parts[1]
        wallet_label = parts[2] if len(parts) > 2 else None
        task_id = await self.queue.enqueue(website, goal, wallet_label, notes="", priority=1)
        await update.message.reply_text(f"Queued task {task_id} on {website}.")

    @auth_required
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if agent:
            await agent.pause()
        else:
            self.queue.pause()
        await update.message.reply_text("Paused.")

    @auth_required
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if agent:
            await agent.resume()
        else:
            self.queue.resume()
        await update.message.reply_text("Resumed.")

    @auth_required
    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if agent:
            await agent.stop()
        else:
            self.queue.pause()
        await update.message.reply_text("Stopped (in-flight step will finish, then the agent goes idle).")

    @auth_required
    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if not agent:
            await update.message.reply_text("Restart requires the full app state; not available in this deployment.")
            return
        await update.message.reply_text("Restarting agent...")
        await agent.stop()
        status = await agent.start()
        await update.message.reply_text(f"Agent restarted. status={status.get('status')}")

    @auth_required
    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_report())

    @auth_required
    async def cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from backend.config.settings import LOG_DIR

        log_file = LOG_DIR / "nexus.log"
        if log_file.exists():
            tail = "\n".join(log_file.read_text(errors="ignore").splitlines()[-30:])
            await update.message.reply_text(f"```\n{tail}\n```", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("No logs yet.")

    @auth_required
    async def cmd_screenshot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from backend.config.settings import SCREENSHOT_DIR

        shots = sorted(SCREENSHOT_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        if shots:
            await update.message.reply_photo(photo=shots[0].open("rb"))
        else:
            await update.message.reply_text("No screenshots yet.")

    @auth_required
    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Memory is queried via API: GET /api/memory/search?q=...")

    @auth_required
    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            f"provider={settings.llm_provider.value} browser={settings.browser_channel.value} "
            f"headless={settings.browser_headless}"
        )

    @auth_required
    async def cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_tasks())

    @auth_required
    async def cmd_browser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_browser())

    @auth_required
    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_health())

    @auth_required
    async def cmd_diagnostics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_diagnostics())

    @auth_required
    async def cmd_resources(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_resources())

    # ---------------------------------------------------------------- #
    # Natural language routing
    # ---------------------------------------------------------------- #
    @auth_required
    async def on_free_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            intent = await self.llm.complete_json(INTENT_SYSTEM_PROMPT, update.message.text)
        except Exception:
            await update.message.reply_text("Couldn't parse that — try /help for commands.")
            return

        kind = intent.get("intent", "unknown")
        if kind == "start_task" and intent.get("website"):
            task_id = await self.queue.enqueue(
                intent["website"], intent.get("goal", "complete available tasks"),
                intent.get("wallet_label") or None, notes="", priority=1,
            )
            await update.message.reply_text(f"Queued task {task_id} on {intent['website']}.")
        elif kind == "pause":
            await self.cmd_pause(update, context)
        elif kind == "resume":
            await self.cmd_resume(update, context)
        elif kind == "stop":
            await self.cmd_stop(update, context)
        elif kind == "restart":
            await self.cmd_restart(update, context)
        elif kind == "status":
            await update.message.reply_text(await self._text_status())
        elif kind == "tasks":
            await update.message.reply_text(await self._text_tasks())
        elif kind == "report":
            await update.message.reply_text(await self._text_report())
        elif kind == "logs":
            await self.cmd_logs(update, context)
        elif kind == "screenshot":
            await self.cmd_screenshot(update, context)
        elif kind == "health":
            await update.message.reply_text(await self._text_health())
        elif kind == "diagnostics":
            await update.message.reply_text(await self._text_diagnostics())
        elif kind == "resources":
            await update.message.reply_text(await self._text_resources())
        elif kind == "browser_status":
            await update.message.reply_text(await self._text_browser())
        else:
            await update.message.reply_text("Not sure what you want — try /help.")

    # ---------------------------------------------------------------- #
    # Text-formatting helpers, shared by slash commands and NL routing
    # ---------------------------------------------------------------- #
    async def _text_status(self) -> str:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if not agent:
            return "Queue worker running. Use /tasks to see current queue."
        s = await agent.status()
        lines = [
            f"status: {s.get('status')}",
            f"current_task: {s.get('current_task_id') or 'none'}",
            f"current_action: {s.get('current_action') or '-'}",
            f"tasks_completed: {s.get('tasks_completed', 0)}  tasks_failed: {s.get('tasks_failed', 0)}",
            f"uptime: {int(s.get('uptime_seconds', 0))}s",
        ]
        return "\n".join(lines)

    async def _text_tasks(self) -> str:
        try:
            from sqlalchemy import select

            from backend.database.models import Task
            from backend.database.session import get_session

            async with get_session() as session:
                result = await session.execute(select(Task).order_by(Task.created_at.desc()).limit(10))
                tasks = list(result.scalars().all())
            if not tasks:
                return "No tasks yet. Use /task <website> | <goal> to queue one."
            lines = ["Recent tasks:"]
            for t in tasks:
                status_val = t.status.value if hasattr(t.status, "value") else t.status
                lines.append(f"- {t.id[:8]} [{status_val}] {t.website} :: {t.goal[:60]}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"Couldn't load tasks: {exc}"

    async def _text_report(self) -> str:
        try:
            from backend.database.models import Report
            from backend.database.session import list_all

            reports = await list_all(Report, order_by=Report.created_at.desc(), limit=5)
            if not reports:
                return "No reports yet."
            lines = ["Recent reports:"]
            for r in reports:
                lines.append(f"- {r.task_id[:8]} [{r.status}] {(r.summary or '')[:80]}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"Couldn't load reports: {exc}"

    async def _text_browser(self) -> str:
        live_session = getattr(self.app_state, "live_session", None) if self.app_state else None
        if not live_session:
            return "Browser control: use /task to start a goal-driven session."
        browser = live_session.status()
        if not browser.get("active"):
            return "Browser idle (no active session)."
        return f"Browser active: {browser.get('title', '')} — {browser.get('url', '')}"

    async def _text_health(self) -> str:
        if not self.app_state:
            return "Health monitor requires the full app state; not available in this deployment."
        from backend.monitoring.health import HealthMonitor

        report = await HealthMonitor(self.app_state).check_all()
        lines = [f"overall: {report.overall}"]
        for c in report.components:
            lines.append(f"- {c.name}: {c.status} ({c.detail})")
        return "\n".join(lines)

    async def _text_diagnostics(self) -> str:
        if not self.app_state:
            return "Diagnostics requires the full app state; not available in this deployment."
        from backend.monitoring.diagnostics import DiagnosticsService

        report = await DiagnosticsService(self.app_state).run()
        return report.to_text()

    async def _text_resources(self) -> str:
        if not self.app_state:
            return "Resource monitor requires the full app state; not available in this deployment."
        from backend.monitoring.resources import ResourceMonitor

        snap = await ResourceMonitor(self.app_state).async_snapshot()
        lines = [
            f"cpu: {snap.cpu_percent if snap.cpu_percent is not None else 'n/a'}%",
            f"process_ram: {snap.process_rss_mb if snap.process_rss_mb is not None else 'n/a'} MB",
            f"system_ram: {snap.system_memory_percent if snap.system_memory_percent is not None else 'n/a'}%",
            f"browser_ram: {snap.browser_memory_mb if snap.browser_memory_mb is not None else 'n/a'} MB",
            f"queue_size: {snap.queue_size}  active_tasks: {snap.active_tasks}",
        ]
        return "\n".join(lines)
