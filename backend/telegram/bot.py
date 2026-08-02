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
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from backend.config.settings import settings
from backend.planner.llm_client import LLMClient
from backend.planner.task_queue import TaskQueueService

logger = logging.getLogger("nexus.telegram")

INTENT_SYSTEM_PROMPT = """You route free-form Telegram messages sent to a browser automation bot into a
structured intent. Respond with STRICT JSON only:
{
  "intent": "start_task | pause | resume | stop | status | report | logs | screenshot | unknown",
  "website": "url if mentioned, else empty",
  "goal": "goal description if this is a start_task intent, else empty",
  "wallet_label": "wallet label if mentioned, else empty"
}"""


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
    def __init__(self, queue: TaskQueueService) -> None:
        self.queue = queue
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
            "/status /pause /resume /stop\n"
            "/report /logs /screenshot /memory\n"
            "/tasks /settings /browser\n"
            "Or just type naturally, e.g. 'complete all tasks on https://example.com using Wallet-01'."
        )

    @auth_required
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Queue worker running. Use /tasks to see current queue.")

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
        self.queue.pause()
        await update.message.reply_text("Paused.")

    @auth_required
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.queue.resume()
        await update.message.reply_text("Resumed.")

    @auth_required
    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.queue.pause()
        await update.message.reply_text("Stopped (paused queue; in-flight step will finish).")

    @auth_required
    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Use the dashboard /reports view for full detail (API: GET /api/reports).")

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
        await update.message.reply_text("Use API GET /api/tasks for full list (Telegram summary coming from API layer).")

    @auth_required
    async def cmd_browser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Browser control: use /task to start a goal-driven session.")

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
            self.queue.pause()
            await update.message.reply_text("Paused.")
        elif kind == "resume":
            self.queue.resume()
            await update.message.reply_text("Resumed.")
        elif kind == "stop":
            self.queue.pause()
            await update.message.reply_text("Stopped.")
        else:
            await update.message.reply_text("Not sure what you want — try /help.")
