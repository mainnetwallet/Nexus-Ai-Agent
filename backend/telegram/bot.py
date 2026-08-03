"""
Telegram remote-control bot for Nexus-Agent.

Supports the fixed command set plus free-form natural language, which is
routed through the LLM to extract an intent (start task / pause / resume /
report / etc.) so the user can type things like "pause the browser" or
"complete all tasks on this site" directly.
"""
from __future__ import annotations

import functools
import html as _html
import logging
from pathlib import Path
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from backend.config.settings import settings
from backend.planner.chat_engine import ChatEngine
from backend.planner.llm_client import LLMClient
from backend.planner.model_manager import TaskType, model_manager
from backend.planner.task_queue import TaskQueueService

logger = logging.getLogger("nexus.telegram")

# Free-form messages are routed through the LLM into one of these intents so
# the bot behaves as a full conversational interface rather than a fixed
# command list -- "how's the browser doing", "give me a report", "pause
# everything" all resolve to the same handlers the slash commands use.
INTENT_SYSTEM_PROMPT = """You route free-form Telegram messages sent to an autonomous browser-automation \
agent into a structured intent. Respond with STRICT JSON only, no prose, no markdown fences:
{
  "intent": "start_task | pause | resume | pause_task | resume_task | cancel_task | stop | restart | \
status | tasks | report | logs | screenshot | health | diagnostics | resources | browser_status | chat | \
unknown",
  "website": "url if mentioned, else empty",
  "goal": "goal description if this is a start_task intent, else empty",
  "wallet_label": "wallet label if mentioned, else empty",
  "profile_label": "browser profile name or id if mentioned, else empty",
  "task_id": "the specific task id mentioned, only for pause_task/resume_task/cancel_task, else empty"
}

Guidance:
- "how are you doing / is everything healthy / any errors" -> health
- "run diagnostics / check the environment / is playwright working" -> diagnostics
- "cpu / memory / ram / how many tasks are queued" -> resources
- "what's the browser doing / is it on a page right now" -> browser_status
- "give me a summary / report on the last task" -> report
- "restart / reboot the agent" -> restart
- "what tasks do you have / show me the queue" -> tasks
- "pause" / "pause everything" (the whole agent/worker, no specific task named) -> pause
- "resume" / "resume everything" (the whole agent/worker, no specific task named) -> resume
- "pause task" / "pause this task" / "pause task <id>" (one specific task) -> pause_task \
task_id=<id if given, else empty>
- "resume task" / "resume this task" / "resume task <id>" (one specific task) -> resume_task \
task_id=<id if given, else empty>
- "cancel task" / "cancel this task" / "cancel it" / "cancel task <id>" -> cancel_task \
task_id=<id if given, else empty>
- Greetings, general questions, small talk, or anything that isn't an action on the \
agent/queue/browser (e.g. "hi", "what can you do?", "explain what a diamond proxy contract is", \
"what's up") -> chat
- Only use "unknown" if the message is truly gibberish/empty; prefer "chat" for anything conversational.
- "run this with Profile-01" / "use my Profile-01 profile" (naming a browser identity/profile, \
not a wallet) alongside a start_task message -> also fill in profile_label"""


def _esc(value: Any) -> str:
    """HTML-escape dynamic values before interpolating into an HTML-parse-mode message."""
    return _html.escape(str(value))


HELP_TEXT = (
    "<b>🤖 Nexus-Agent — Command Center</b>\n\n"
    "<b>📋 Task</b>\n"
    "/task &lt;website&gt; | &lt;goal&gt; | &lt;wallet&gt;\n\n"
    "<b>⚙️ Control</b>\n"
    "/status  /pause  /resume  /cancel  /stop  /restart\n\n"
    "<b>📊 Monitoring</b>\n"
    "/report  /logs  /screenshot  /memory  /tasks  /browser\n\n"
    "<b>🩺 Diagnostics</b>\n"
    "/health  /diagnostics  /resources\n\n"
    "<b>🧠 Skills</b>\n"
    "/skills [list | learn &lt;desc&gt; | enable | disable | delete | pending | confirm | discard]\n"
    "/teach [start [name] | &lt;step&gt; | undo | done | cancel]\n\n"
    "<b>🔌 Connectors</b>\n"
    "/mcp [list | enable | disable | &lt;query&gt;]\n\n"
    "<i>Or just type naturally</i> — e.g. \"complete all tasks on https://example.com using Wallet-01\", "
    "\"how's everything doing?\", \"give me a report\". Anything else gets a normal chat reply.\n\n"
    "👇 Or tap a quick action:"
)

HELP_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("📊 Status", callback_data="status"), InlineKeyboardButton("📋 Tasks", callback_data="tasks")],
        [InlineKeyboardButton("📈 Report", callback_data="report"), InlineKeyboardButton("🌐 Browser", callback_data="browser")],
        [InlineKeyboardButton("🩺 Health", callback_data="health"), InlineKeyboardButton("🔧 Diagnostics", callback_data="diagnostics")],
        [InlineKeyboardButton("🖥 Resources", callback_data="resources"), InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
)

# Persistent reply keyboard shown below the message box at all times (once
# sent with any message, Telegram keeps it visible for the rest of the
# chat until replaced). Button taps arrive as plain text messages, so
# on_free_text() matches them against MAIN_KEYBOARD_ACTIONS below and
# dispatches directly -- no LLM intent round trip needed for these.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["⚡ Status", "📋 Tasks"],
        ["📈 Report", "🌐 Browser"],
        ["🩺 Health", "🖥 Resources"],
        ["❓ Help"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


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
            elif update.callback_query:
                await update.callback_query.answer("Not authorized.", show_alert=True)
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
        self.llm = model_manager
        self.app: Optional[Application] = None
        # Conversational fallback ("chat"/"unknown" intents, i.e. anything
        # that isn't a fast structured command above) is delegated to the
        # same ChatEngine the dashboard's AI Chat page uses, keyed by
        # "telegram:<chat_id>" so each Telegram conversation gets its own
        # persistent, DB-backed history (survives restarts) and full access
        # to the task/agent-command/browser-command/system-request taxonomy
        # -- not just small talk. Reuses app_state.chat if the full app is
        # wired up; otherwise builds its own bound to just this bot's queue.
        chat_from_state = getattr(app_state, "chat", None) if app_state else None
        self.chat_engine: ChatEngine = chat_from_state or ChatEngine(queue=queue, app_state=app_state, llm=self.llm)

    def build(self) -> Application:
        app = Application.builder().token(settings.telegram_bot_token).build()

        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("task", self.cmd_task))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("stop", self.cmd_stop))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
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
        app.add_handler(CommandHandler("skills", self.cmd_skills))
        app.add_handler(CommandHandler("teach", self.cmd_teach))
        app.add_handler(CommandHandler("mcp", self.cmd_mcp))
        app.add_handler(CallbackQueryHandler(self.on_button))
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
            "👋 <b>Nexus-Agent online.</b>\n"
            "Send /task &lt;website&gt; | &lt;goal&gt; | &lt;wallet&gt;, describe a task in plain words, "
            "or just chat with me — ask questions, say hi, whatever.\n\n"
            "Type /help to see everything I can do.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD,
        )

    @auth_required
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=HELP_KEYBOARD)
        await update.message.reply_text("Quick actions are also pinned below the message box. 👇", reply_markup=MAIN_KEYBOARD)

    @auth_required
    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handles taps on the inline keyboard attached to /help (and any
        other message reusing HELP_KEYBOARD) -- each button re-runs the
        matching read-only status/report command so the user doesn't have
        to type it."""
        query = update.callback_query
        await query.answer()
        action = query.data
        text_producers = {
            "status": self._text_status,
            "tasks": self._text_tasks,
            "report": self._text_report,
            "browser": self._text_browser,
            "health": self._text_health,
            "diagnostics": self._text_diagnostics,
            "resources": self._text_resources,
        }
        if action == "help":
            await query.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=HELP_KEYBOARD)
            return
        producer = text_producers.get(action)
        if producer:
            await query.message.reply_text(await producer(), parse_mode=ParseMode.HTML)

    @auth_required
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_status(), parse_mode=ParseMode.HTML)

    @auth_required
    async def cmd_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args)
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2:
            await update.message.reply_text("Format: /task <website> | <goal> | <wallet optional> | <profile optional>")
            return
        website, goal = parts[0], parts[1]
        wallet_label = parts[2] if len(parts) > 2 and parts[2] else None
        profile_label = parts[3] if len(parts) > 3 and parts[3] else None
        task_id = await self.queue.enqueue(website, goal, wallet_label, notes="", priority=1, profile_label=profile_label)
        await update.message.reply_text(f"Queued task {task_id} on {website}.")

    @auth_required
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # "/pause" (no args) pauses the whole worker, unchanged. "/pause
        # <task_id>" scopes to that one task instead -- delegated to
        # ChatEngine so Telegram/Chat/Dashboard/REST API share the same
        # single-task pause logic (backend/planner/task_queue.py pause_task).
        task_id = context.args[0] if context and context.args else ""
        if task_id:
            await self._handle_chat_text(update, f"pause task {task_id}")
            return
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if agent:
            await agent.pause()
        else:
            self.queue.pause()
        await update.message.reply_text("Paused.")

    @auth_required
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        task_id = context.args[0] if context and context.args else ""
        if task_id:
            await self._handle_chat_text(update, f"resume task {task_id}")
            return
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if agent:
            await agent.resume()
        else:
            self.queue.resume()
        await update.message.reply_text("Resumed.")

    @auth_required
    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Cancels a single task: "/cancel <task_id>", or "/cancel" to cancel
        whichever task is currently running. Delegated to ChatEngine's
        agent_command/cancel_task handling (same code path as Chat/Dashboard/
        REST API) rather than reimplemented here."""
        task_id = context.args[0] if context and context.args else ""
        text = f"cancel task {task_id}" if task_id else "cancel task"
        await self._handle_chat_text(update, text)

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
        await update.message.reply_text(await self._text_report(), parse_mode=ParseMode.HTML)

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
        await update.message.reply_text(await self._text_tasks(), parse_mode=ParseMode.HTML)

    @auth_required
    async def cmd_browser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_browser(), parse_mode=ParseMode.HTML)

    @auth_required
    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_health(), parse_mode=ParseMode.HTML)

    @auth_required
    async def cmd_diagnostics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_diagnostics(), parse_mode=ParseMode.HTML)

    @auth_required
    async def cmd_resources(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self._text_resources(), parse_mode=ParseMode.HTML)

    @auth_required
    async def cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Delegates to ChatEngine's "skill" intent handling (backend/planner/
        chat_engine.py ChatEngine._handle_skill_command) via a synthetic
        chat message, so /skills behaves identically to typing the
        equivalent sentence -- one place owns the Skill Library dispatch
        logic. With no args, lists the skills."""
        text = " ".join(context.args) if context.args else "list my skills"
        await self._handle_chat_text(update, text)

    @auth_required
    async def cmd_teach(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Delegates to ChatEngine, which intercepts every message for this
        chat session as a Teach Mode turn once a draft is active (see
        ChatEngine.send_message / _handle_teach_turn) -- so "/teach start
        <name>" begins a session and subsequent plain messages (or further
        "/teach <step>" calls) are steps, until "/teach done"/"/teach cancel"."""
        text = " ".join(context.args) if context.args else "teach me a skill"
        if text.lower().startswith("start"):
            text = "teach me a skill" + (f" called {text[5:].strip()}" if text[5:].strip() else "")
        await self._handle_chat_text(update, text)

    @auth_required
    async def cmd_mcp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Delegates to ChatEngine's "mcp" intent handling (backend/planner/
        chat_engine.py ChatEngine._handle_mcp_command) via a synthetic chat
        message, so /mcp behaves identically to typing the equivalent
        sentence -- one place owns the MCP Core dispatch logic. With no
        args, asks for connector status."""
        text = " ".join(context.args) if context.args else "list my mcp connectors"
        await self._handle_chat_text(update, text)

    async def _reply_with_optional_file(self, update: Update, result: dict) -> None:
        """Sends ChatEngine's text reply, then -- if meta carries a
        file_path (set by ChatEngine._handle_mcp_command after a
        filesystem.write_file call) -- also sends that file to the chat as
        a downloadable document, so "make me an HTML file" actually
        delivers the file instead of just a text confirmation."""
        await update.message.reply_text(result["reply"])
        file_path = (result.get("meta") or {}).get("file_path")
        if not file_path:
            return
        path = Path(file_path)
        try:
            if not path.is_file():
                raise FileNotFoundError(str(path))
            with path.open("rb") as fh:
                await update.message.reply_document(document=fh, filename=path.name)
        except Exception:
            logger.exception("Failed to send generated file %s to Telegram", file_path)
            await update.message.reply_text(f"⚠️ File was created at {_esc(file_path)} but couldn't be sent here.")

    async def _handle_chat_text(self, update: Update, text: str) -> None:
        chat_id = update.effective_chat.id if update.effective_chat else 0
        try:
            result = await self.chat_engine.send_message(f"telegram:{chat_id}", text, channel="telegram")
        except Exception:
            logger.exception("Chat engine failed to handle skill/teach command")
            await update.message.reply_text("Couldn't process that — try /help for direct commands.")
            return
        await self._reply_with_optional_file(update, result)

    # ---------------------------------------------------------------- #
    # Natural language routing
    # ---------------------------------------------------------------- #
    @auth_required
    async def on_free_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Taps on the persistent reply keyboard (MAIN_KEYBOARD) arrive here
        # as plain text matching the button label exactly -- handle those
        # directly, skipping the LLM intent round trip entirely.
        keyboard_actions = {
            "⚡ Status": self._text_status,
            "📋 Tasks": self._text_tasks,
            "📈 Report": self._text_report,
            "🌐 Browser": self._text_browser,
            "🩺 Health": self._text_health,
            "🖥 Resources": self._text_resources,
        }
        label = (update.message.text or "").strip()
        if label == "❓ Help":
            await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=HELP_KEYBOARD)
            return
        producer = keyboard_actions.get(label)
        if producer:
            await update.message.reply_text(await producer(), parse_mode=ParseMode.HTML)
            return

        try:
            intent = await self.llm.complete_json(
                INTENT_SYSTEM_PROMPT, update.message.text, task_type=TaskType.FAST_RESPONSE
            )
        except Exception:
            await update.message.reply_text("Couldn't parse that — try /help for commands.")
            return

        kind = intent.get("intent", "unknown")
        if kind == "start_task" and intent.get("website"):
            task_id = await self.queue.enqueue(
                intent["website"], intent.get("goal", "complete available tasks"),
                intent.get("wallet_label") or None, notes="", priority=1,
                profile_label=intent.get("profile_label") or None,
            )
            await update.message.reply_text(f"Queued task {task_id} on {intent['website']}.")
        elif kind == "pause":
            await self.cmd_pause(update, context)
        elif kind == "resume":
            await self.cmd_resume(update, context)
        elif kind == "stop":
            await self.cmd_stop(update, context)
        elif kind in ("pause_task", "resume_task", "cancel_task"):
            verb = kind.split("_")[0]
            task_id = intent.get("task_id") or ""
            text = f"{verb} task {task_id}".strip()
            await self._handle_chat_text(update, text)
        elif kind == "restart":
            await self.cmd_restart(update, context)
        elif kind == "status":
            await update.message.reply_text(await self._text_status(), parse_mode=ParseMode.HTML)
        elif kind == "tasks":
            await update.message.reply_text(await self._text_tasks(), parse_mode=ParseMode.HTML)
        elif kind == "report":
            await update.message.reply_text(await self._text_report(), parse_mode=ParseMode.HTML)
        elif kind == "logs":
            await self.cmd_logs(update, context)
        elif kind == "screenshot":
            await self.cmd_screenshot(update, context)
        elif kind == "health":
            await update.message.reply_text(await self._text_health(), parse_mode=ParseMode.HTML)
        elif kind == "diagnostics":
            await update.message.reply_text(await self._text_diagnostics(), parse_mode=ParseMode.HTML)
        elif kind == "resources":
            await update.message.reply_text(await self._text_resources(), parse_mode=ParseMode.HTML)
        elif kind == "browser_status":
            await update.message.reply_text(await self._text_browser(), parse_mode=ParseMode.HTML)
        elif kind == "chat":
            await self._handle_chat(update)
        else:
            # "unknown" (truly gibberish/empty, per INTENT_SYSTEM_PROMPT) and
            # any unrecognized intent value both get the plain help hint --
            # no LLM-backed ChatEngine round trip for input that isn't
            # actually conversational.
            await update.message.reply_text("Not sure what you want — try /help.")

    async def _handle_chat(self, update: Update) -> None:
        """
        General conversational fallback -- lets the user just talk to the
        agent instead of every message needing to match a fixed slash
        command or the fast structured-intent branches above. Delegated to
        ChatEngine (backend/planner/chat_engine.py), which gives this its
        own persistent history per chat (survives restarts) and can itself
        start tasks, pause/resume/continue the agent, run browser commands
        (open/search/summarize/screenshot), and answer system requests
        ("what happened today", "explain why you failed") -- not just chat.
        """
        chat_id = update.effective_chat.id if update.effective_chat else 0
        try:
            result = await self.chat_engine.send_message(
                f"telegram:{chat_id}", update.message.text, channel="telegram"
            )
        except Exception:
            logger.exception("Chat engine failed to handle free-text message")
            await update.message.reply_text("Couldn't process that — try /help for direct commands.")
            return

        await self._reply_with_optional_file(update, result)

    # ---------------------------------------------------------------- #
    # Text-formatting helpers, shared by slash commands and NL routing
    # ---------------------------------------------------------------- #
    async def _text_status(self) -> str:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if not agent:
            return "⚙️ Queue worker running. Use /tasks to see current queue."
        s = await agent.status()
        dot = {"running": "🟢", "paused": "⏸️", "idle": "⚪", "error": "🔴", "stopped": "🔴"}.get(
            str(s.get("status", "")).lower(), "🔵"
        )
        lines = [
            "<b>📊 Agent Status</b>",
            f"{dot} status: <b>{_esc(s.get('status'))}</b>",
            f"🧩 current_task: {_esc(s.get('current_task_id') or 'none')}",
            f"⚡ current_action: {_esc(s.get('current_action') or '-')}",
            f"✅ completed: {s.get('tasks_completed', 0)}   ❌ failed: {s.get('tasks_failed', 0)}",
            f"⏱ uptime: {int(s.get('uptime_seconds', 0))}s",
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
                return "📋 No tasks yet. Use /task <website> | <goal> to queue one."
            lines = ["<b>📋 Recent Tasks</b>"]
            status_dot = {
                "completed": "✅", "done": "✅", "failed": "❌", "error": "❌",
                "running": "🟢", "in_progress": "🟢", "paused": "⏸️", "queued": "🕓", "pending": "🕓",
            }
            for t in tasks:
                status_val = t.status.value if hasattr(t.status, "value") else t.status
                dot = status_dot.get(str(status_val).lower(), "⚪")
                lines.append(
                    f"{dot} <code>{_esc(t.id[:8])}</code> [{_esc(status_val)}] "
                    f"{_esc(t.website)} :: {_esc(t.goal[:60])}"
                )
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ Couldn't load tasks: {_esc(exc)}"

    async def _text_report(self) -> str:
        try:
            from backend.database.models import Report
            from backend.database.session import list_all

            reports = await list_all(Report, order_by=Report.created_at.desc(), limit=5)
            if not reports:
                return "📈 No reports yet."
            lines = ["<b>📈 Recent Reports</b>"]
            for r in reports:
                dot = "✅" if str(r.status).lower() in ("completed", "done", "success") else (
                    "❌" if str(r.status).lower() in ("failed", "error") else "⚪"
                )
                lines.append(f"{dot} <code>{_esc(r.task_id[:8])}</code> [{_esc(r.status)}] {_esc((r.summary or '')[:80])}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ Couldn't load reports: {_esc(exc)}"

    async def _text_browser(self) -> str:
        live_session = getattr(self.app_state, "live_session", None) if self.app_state else None
        if not live_session:
            return "🌐 Browser control: use /task to start a goal-driven session."
        browser = live_session.status()
        if not browser.get("active"):
            return "🌐 Browser idle (no active session)."
        return f"🌐 <b>Browser active</b>\n{_esc(browser.get('title', ''))} — {_esc(browser.get('url', ''))}"

    async def _text_health(self) -> str:
        if not self.app_state:
            return "🩺 Health monitor requires the full app state; not available in this deployment."
        from backend.monitoring.health import HealthMonitor

        report = await HealthMonitor(self.app_state).check_all()
        overall_dot = "🟢" if str(report.overall).upper() in ("OK", "PASS", "HEALTHY") else "🔴"
        lines = [f"<b>🩺 Health Report</b>", f"{overall_dot} overall: <b>{_esc(report.overall)}</b>"]
        for c in report.components:
            dot = "🟢" if str(c.status).upper() in ("OK", "PASS", "HEALTHY") else "🔴"
            lines.append(f"{dot} {_esc(c.name)}: {_esc(c.status)} ({_esc(c.detail)})")
        return "\n".join(lines)

    async def _text_diagnostics(self) -> str:
        if not self.app_state:
            return "🔧 Diagnostics requires the full app state; not available in this deployment."
        from backend.monitoring.diagnostics import DiagnosticsService

        report = await DiagnosticsService(self.app_state).run()
        return f"<b>🔧 Diagnostics</b>\n<pre>{_esc(report.to_text())}</pre>"

    async def _text_resources(self) -> str:
        if not self.app_state:
            return "🖥 Resource monitor requires the full app state; not available in this deployment."
        from backend.monitoring.resources import ResourceMonitor

        snap = await ResourceMonitor(self.app_state).async_snapshot()
        lines = [
            "<b>🖥 Resource Usage</b>",
            f"🧮 cpu: {snap.cpu_percent if snap.cpu_percent is not None else 'n/a'}%",
            f"💾 process_ram: {snap.process_rss_mb if snap.process_rss_mb is not None else 'n/a'} MB",
            f"💾 system_ram: {snap.system_memory_percent if snap.system_memory_percent is not None else 'n/a'}%",
            f"🌐 browser_ram: {snap.browser_memory_mb if snap.browser_memory_mb is not None else 'n/a'} MB",
            f"🕓 queue_size: {snap.queue_size}   ▶️ active_tasks: {snap.active_tasks}",
        ]
        return "\n".join(lines)
