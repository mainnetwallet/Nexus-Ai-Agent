"""
Conversational AI Chat engine.

Turns Nexus-Agent from a task-only agent into something you can just talk
to. This module owns exactly one thing: classify a free-form message into
one of seven categories, then dispatch it to whichever existing module
already owns that behavior. It never reimplements task execution, agent
lifecycle, browser observation, or reporting -- it only composes
TaskQueueService, AgentRuntime, LiveSessionManager and the Report/Task
tables, all of which already exist.

Used by both the dashboard's AI Chat page (backend/api/routes_chat.py) and
the Telegram bot's natural-language fallback (backend/telegram/bot.py), so
"chat with the agent" behaves identically everywhere and there is exactly
one place that owns intent classification for conversation.

Categories (see CLASSIFIER_SYSTEM_PROMPT):
  conversation     - small talk, greetings, "what can you do"
  question         - answerable from context/knowledge, no action needed
  browser_command  - open/search/summarize/screenshot/show current browser
  agent_command     - pause/resume/stop/start/continue the agent or a task
  task             - a new goal-driven task to queue
  settings         - read current configuration
  system_request   - status/history/explain-failure/explain-last-action
  skill            - learn/list/enable/disable/delete a skill, Teach Mode,
                     confirming/discarding a "save as skill?" suggestion, or
                     correcting a skill's learned workflow (see
                     backend/skills/ and ChatEngine._handle_skill_command)
  mcp              - off-page-web/filesystem/terminal/github requests routed
                     through an MCP connector (see backend/mcp/ and
                     ChatEngine._handle_mcp_command)
  ai_model         - switch/default/auto-route/override the active LLM
                     provider, or ask about current provider/model/health
                     (see backend/planner/model_manager.py and
                     ChatEngine._handle_ai_model_command)
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from sqlalchemy import select

from backend.database.models import ChatMessage, ChatRole, ChatSession, Report, SkillSource, Task, TaskStatus
from backend.database.session import get_session
from backend.planner.llm_client import LLMClient
from backend.planner.model_manager import model_manager as _default_model_manager

logger = logging.getLogger("nexus.chat")

CLASSIFIER_SYSTEM_PROMPT = """You classify a message sent to Nexus-Agent, an autonomous browser-automation \
agent, into a structured intent. Respond with STRICT JSON only, no prose, no markdown fences:
{
  "category": "conversation | question | browser_command | agent_command | task | settings | system_request | skill | mcp",
  "action": "short action keyword, see guidance below",
  "website": "url if one is mentioned or implied, else empty",
  "goal": "goal description if this describes work to perform, else empty",
  "query": "search text or free-form subject, if relevant, else empty",
  "wallet_label": "wallet label if mentioned, else empty",
  "profile_label": "browser profile name or id if one is mentioned, else empty",
  "skill_action": "learn | confirm | discard | teach_start | teach_finish | teach_cancel | teach_undo | \
list | enable | disable | delete | correct -- only set when category=skill",
  "skill_name": "name (or partial name) of an existing skill this message refers to, else empty",
  "skill_text": "the free-form skill description to learn, teach-mode step text, or correction instruction, \
else empty",
  "mcp_query": "the raw request text describing the filesystem/terminal/github/fetch-URL work to perform, \
only set when category=mcp, else empty",
  "mcp_connector": "filesystem | terminal | browser | github -- only set when the message clearly names \
which connector to use, else empty",
  "ai_action": "switch | set_default | enable_auto_routing | disable_auto_routing | set_routing_rule | \
temporary_use | show_provider | show_model | show_providers | show_health | show_routing -- only set \
when category=ai_model",
  "ai_provider": "the AI provider named in the message (e.g. claude, gpt, gemini, groq, openrouter, cohere, \
huggingface, mistral, grok, kimi, qwen, glm...), only set when category=ai_model and a provider is named",
  "ai_task_type": "coding | browser_automation | planning | vision | long_context | fast_response | \
general_chat | research | reasoning | low_cost -- only set when category=ai_model action=set_routing_rule"
}

Guidance:
- Greetings, small talk, "what are you doing", "what can you do" -> category=conversation
- General questions not about a specific action -> category=question
- "open chrome", "open <url>" -> category=browser_command action=open
- "search for X" -> category=browser_command action=search query=X
- "summarize this page" -> category=browser_command action=summarize
- "take a screenshot" -> category=browser_command action=screenshot
- "show browser" / "what's on screen" -> category=browser_command action=show
- "pause" -> category=agent_command action=pause
- "resume" -> category=agent_command action=resume
- "stop" -> category=agent_command action=stop
- "continue the previous task" / "keep going" -> category=agent_command action=continue
- "check my current task" / "what's the status" -> category=system_request action=current_task
- "explain why you failed" / "why did that fail" -> category=system_request action=explain_failure
- "explain your last action" / "what did you just do" -> category=system_request action=explain_last_action
- "what happened today" -> category=system_request action=today_summary
- settings/configuration questions ("what model are you using", "what wallet mode") -> category=settings
- "learn how to ...", "remember how to ...", "save this as a skill: ..." -> category=skill \
skill_action=learn skill_text=<the full description of the workflow>
- "save as skill" / "yes save it" / "keep that skill" (answering a "save this as a skill?" prompt) -> \
category=skill skill_action=confirm
- "discard" / "don't save it" / "no, skip it" (answering a "save this as a skill?" prompt) -> \
category=skill skill_action=discard
- "teach me a skill" / "let's do teach mode" / "I want to teach you something" -> category=skill \
skill_action=teach_start skill_name=<name if one was given, else empty>
- "done" / "finish" / "that's it, save it" / "done teaching" (while teaching) -> category=skill \
skill_action=teach_finish
- "cancel" / "cancel teaching" / "forget this one" (while teaching) -> category=skill skill_action=teach_cancel
- "undo" / "undo that step" / "undo the last step" (while teaching) -> category=skill skill_action=teach_undo
- "list my skills" / "what skills do you know" / "show me my skills" -> category=skill skill_action=list
- "enable the X skill" -> category=skill skill_action=enable skill_name=X
- "disable the X skill" -> category=skill skill_action=disable skill_name=X
- "delete the X skill" / "forget the X skill" -> category=skill skill_action=delete skill_name=X
- "no, actually click the confirm button instead" / a correction about a step a learned skill just took -> \
category=skill skill_action=correct skill_name=<skill if named, else empty> skill_text=<the correction>
- "list files in X" / "read file X" / "write/delete/search files in X" -> category=mcp mcp_connector=filesystem \
mcp_query=<the raw request>
- "run command X" / "execute X in the terminal" -> category=mcp mcp_connector=terminal mcp_query=<the raw request>
- "check github issues on X/Y" / "create an issue on X/Y" / "list PRs on X/Y" / "get file contents from X/Y" -> \
category=mcp mcp_connector=github mcp_query=<the raw request>
- "fetch this URL" / "get the links on this page" (off-page, not the live browser session) -> category=mcp \
mcp_connector=browser mcp_query=<the raw request>
- If the message clearly needs filesystem/terminal/github/off-page-web access but doesn't name which one, \
still use category=mcp with mcp_connector left empty so it can be auto-routed
- Anything that describes NEW work to perform on a website (e.g. "go buy a widget on \
example.com", "complete the KYC form on X") -> category=task, with website/goal filled in
- "run this task with Profile-01" / "use my Profile-01 profile" / "as Profile-01" (naming a \
browser identity/profile, not a wallet) -> category=task, with profile_label filled in alongside \
website/goal/wallet_label
- Only classify as "task" when the message actually describes work to perform. Simple \
conversation, questions, or commands about existing tasks must NOT be classified as task.
- "switch to Claude" / "switch to Gemini" / "use GPT" / "use Groq" -> category=ai_model ai_action=switch \
ai_provider=<the named provider>
- "set Gemini as default" / "make Claude my default provider" -> category=ai_model ai_action=set_default \
ai_provider=<the named provider>
- "use automatic routing" / "enable smart routing" / "turn on auto model routing" -> category=ai_model \
ai_action=enable_auto_routing
- "turn off smart routing" / "use manual mode" / "stop auto-routing" -> category=ai_model \
ai_action=disable_auto_routing
- "use Claude for coding" / "route vision tasks to Gemini" / "use Groq for fast responses" -> category=ai_model \
ai_action=set_routing_rule ai_provider=<the named provider> ai_task_type=<the matching task type>
- "use Claude for this task only" / "use Gemini just this time" / "use Groq for this request" -> \
category=ai_model ai_action=temporary_use ai_provider=<the named provider>
- "show current provider" / "which AI provider are you using" -> category=ai_model ai_action=show_provider
- "show current model" / "which model are you using" -> category=ai_model ai_action=show_model
- "show available providers" / "list AI providers" -> category=ai_model ai_action=show_providers
- "show provider health" / "check AI provider status" -> category=ai_model ai_action=show_health
- "show routing rules" / "what's the routing config" -> category=ai_model ai_action=show_routing"""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ChatEngine:
    """
    Stateless besides its DB-backed sessions -- safe to construct once and
    share between the dashboard chat routes and the Telegram bot.
    """

    def __init__(self, queue: Any, app_state: Optional[Any] = None, llm: Optional[LLMClient] = None) -> None:
        self.queue = queue  # TaskQueueService
        self.app_state = app_state  # backend.api.app_state.AppState, optional
        self.llm = llm or _default_model_manager

    # ------------------------------------------------------------------ #
    # Session management
    # ------------------------------------------------------------------ #
    async def get_or_create_session(self, session_id: Optional[str], channel: str = "dashboard") -> ChatSession:
        async with get_session() as db:
            if session_id:
                existing = await db.get(ChatSession, session_id)
                if existing:
                    return existing
                row = ChatSession(id=session_id, channel=channel)
            else:
                row = ChatSession(channel=channel)
            db.add(row)
            await db.flush()
            await db.refresh(row)
            return row

    async def list_sessions(self, channel: Optional[str] = None) -> list[ChatSession]:
        async with get_session() as db:
            stmt = select(ChatSession).order_by(ChatSession.updated_at.desc())
            if channel:
                stmt = stmt.where(ChatSession.channel == channel)
            result = await db.execute(stmt)
            return list(result.scalars().all())

    async def get_history(self, session_id: str, limit: int = 200) -> list[ChatMessage]:
        async with get_session() as db:
            result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.asc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def clear_history(self, session_id: str) -> None:
        async with get_session() as db:
            from sqlalchemy import delete

            await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
            row = await db.get(ChatSession, session_id)
            if row:
                row.last_task_id = None
                row.last_error = None

    # ------------------------------------------------------------------ #
    # Core turn
    # ------------------------------------------------------------------ #
    async def send_message(self, session_id: str, text: str, channel: str = "dashboard") -> dict:
        session = await self.get_or_create_session(session_id, channel)
        await self._append(session.id, ChatRole.USER, text)

        # Teach Mode is intercepted BEFORE intent classification: once a
        # session has an active teach draft (backend.skills.teach.
        # TeachModeManager, keyed by this chat session's id), every message
        # is a teach-mode turn (a step description, or "undo"/"done"/
        # "cancel") until it's finished or cancelled -- it never goes
        # through the LLM classifier, so a step like "type 50 into the
        # amount field" can't accidentally get reclassified as a "task".
        teach = getattr(self.app_state, "teach", None) if self.app_state else None
        if teach is not None and teach.is_active(session.id):
            try:
                reply, meta = await self._handle_teach_turn(session, teach, text)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Teach Mode turn failed")
                reply, meta = f"Something went wrong in Teach Mode: {exc}", {}
            await self._append(session.id, ChatRole.ASSISTANT, reply, category="skill", meta=meta)
            return {"session_id": session.id, "reply": reply, "category": "skill", "action": "teach_step", "meta": meta}

        try:
            intent = await self.llm.complete_json(CLASSIFIER_SYSTEM_PROMPT, text)
        except Exception:
            logger.exception("Chat classifier failed, falling back to conversation")
            intent = {"category": "conversation"}

        category = intent.get("category", "conversation")
        action = intent.get("action", "")
        meta: dict[str, Any] = {}

        try:
            if category == "task" and intent.get("website"):
                reply, meta = await self._handle_task(session, intent)
            elif category == "agent_command":
                reply, meta = await self._handle_agent_command(session, action)
            elif category == "browser_command":
                reply, meta = await self._handle_browser_command(session, action, intent)
            elif category == "system_request":
                reply, meta = await self._handle_system_request(session, action)
            elif category == "settings":
                reply = await self._handle_settings()
            elif category == "ai_model":
                reply, meta = await self._handle_ai_model_command(intent)
            elif category == "skill":
                reply, meta = await self._handle_skill_command(session, intent, text)
            elif category == "mcp":
                reply, meta = await self._handle_mcp_command(intent, text)
            else:
                reply = await self._handle_conversation(session, text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Chat dispatch failed for category=%s", category)
            reply = f"Something went wrong handling that: {exc}"

        await self._append(session.id, ChatRole.ASSISTANT, reply, category=category, meta=meta)
        return {"session_id": session.id, "reply": reply, "category": category, "action": action, "meta": meta}

    # ------------------------------------------------------------------ #
    # Category handlers
    # ------------------------------------------------------------------ #
    async def _handle_task(self, session: ChatSession, intent: dict) -> tuple[str, dict]:
        website = intent["website"]
        goal = intent.get("goal") or "Complete the available task on this site."
        wallet_label = intent.get("wallet_label") or None
        profile_label = intent.get("profile_label") or None
        task_id = await self.queue.enqueue(website, goal, wallet_label, notes="", priority=1, profile_label=profile_label)
        await self._touch_session(session.id, last_task_id=task_id)
        return f"Queued a task on {website}: {goal}\n(task_id={task_id})", {"task_id": task_id}

    async def _handle_agent_command(self, session: ChatSession, action: str) -> tuple[str, dict]:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if action == "pause":
            if agent:
                await agent.pause()
            else:
                self.queue.pause()
            return "Paused.", {}
        if action == "resume":
            if agent:
                await agent.resume()
            else:
                self.queue.resume()
            return "Resumed.", {}
        if action == "stop":
            if agent:
                await agent.stop()
            else:
                self.queue.pause()
            return "Stopped. In-flight step will finish, then the agent goes idle.", {}
        if action == "start":
            if agent:
                await agent.start()
            return "Started.", {}
        if action == "continue":
            return await self._continue_previous(session)
        return "Not sure which agent action you mean -- try pause, resume, stop, or continue.", {}

    async def _continue_previous(self, session: ChatSession) -> tuple[str, dict]:
        # Prefer a task that's actually paused right now.
        qstatus = self.queue.queue_status()
        paused_ids = qstatus.get("paused_task_ids") or []
        if paused_ids:
            task_id = paused_ids[0]
            self.queue.resume_task(task_id)
            return f"Resuming task {task_id}.", {"task_id": task_id}

        # Otherwise fall back to retrying this session's last known task, if
        # it ended in a retryable (failed/cancelled) state.
        if session.last_task_id:
            ok = await self.queue.retry(session.last_task_id)
            if ok:
                return f"Re-queued task {session.last_task_id} for another attempt.", {"task_id": session.last_task_id}

        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if agent:
            await agent.resume()
        return "Nothing specific to continue -- resumed the agent worker so it picks up the next queued task.", {}

    async def _handle_browser_command(self, session: ChatSession, action: str, intent: dict) -> tuple[str, dict]:
        live_session = getattr(self.app_state, "live_session", None) if self.app_state else None

        if action == "screenshot":
            if live_session and live_session.latest_screenshot_bytes() is not None:
                return "Here's the latest screenshot (see the Browser panel / GET /api/browser/screenshot).", {
                    "has_screenshot": True
                }
            return "No screenshot available yet -- nothing has run in the browser this session.", {}

        if action == "show":
            if not live_session:
                return "Live browser view isn't initialized in this deployment.", {}
            status = live_session.status()
            if not status.get("active"):
                return "Browser is idle -- no active session right now.", {}
            return f"Browser active: {status.get('title', '')} — {status.get('url', '')}", {}

        if action == "summarize":
            website = self._current_website()
            if not website:
                return "There's no active page to summarize right now -- start a task first.", {}
            task_id = await self.queue.enqueue(
                website, "Read the current page and summarize its contents in a few sentences.", None, notes="", priority=2
            )
            await self._touch_session(session.id, last_task_id=task_id)
            return f"Queued a summary of {website} (task_id={task_id}). I'll have it shortly.", {"task_id": task_id}

        if action == "search":
            query = intent.get("query") or ""
            if not query:
                return "What would you like me to search for?", {}
            task_id = await self.queue.enqueue(
                "https://www.google.com", f"Search for '{query}' and report the top results.", None, notes="", priority=1
            )
            await self._touch_session(session.id, last_task_id=task_id)
            return f"Searching for '{query}' (task_id={task_id}).", {"task_id": task_id}

        if action == "open":
            website = intent.get("website") or intent.get("query") or ""
            if not website:
                return "Which site should I open?", {}
            task_id = await self.queue.enqueue(
                website, "Open the page and report what's there.", None, notes="", priority=1
            )
            await self._touch_session(session.id, last_task_id=task_id)
            return f"Opening {website} (task_id={task_id}).", {"task_id": task_id}

        return "Not sure which browser action you mean.", {}

    async def _handle_system_request(self, session: ChatSession, action: str) -> tuple[str, dict]:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None

        if action in ("current_task", "status", ""):
            if not agent:
                return "Agent runtime isn't initialized in this deployment.", {}
            s = await agent.status()
            if not s.get("current_task_id"):
                return "No task currently in flight. Queue worker is " + (
                    "paused." if s.get("queue", {}).get("worker_paused") else "active."
                ), {}
            return (
                f"Working on task {s['current_task_id']} at {s.get('current_website') or '—'}: "
                f"{s.get('current_action') or 'starting'} {s.get('current_target') or ''}".strip()
            ), {}

        if action == "explain_last_action":
            if not agent:
                return "Agent runtime isn't initialized in this deployment.", {}
            s = await agent.status()
            reasoning = s.get("current_reasoning")
            if reasoning:
                return reasoning, {}
            return "No reasoning has been recorded yet -- the agent hasn't taken an action this session.", {}

        if action == "explain_failure":
            report = await self._last_failed_report(session)
            if not report:
                return "I don't see any failed tasks to explain.", {}
            await self._touch_session(session.id, last_error=report.summary)
            return f"Task {report.task_id} ended as {report.status}: {report.summary}", {"task_id": report.task_id}

        if action == "today_summary":
            return await self._today_summary()

        return await self._handle_system_request(session, "current_task")

    async def _handle_settings(self) -> str:
        from backend.config.settings import settings
        from backend.planner.model_manager import model_manager

        return (
            f"provider={settings.llm_provider.value} model={settings.llm_model_override or '(default)'} "
            f"routing={model_manager.routing_mode} "
            f"browser={settings.browser_channel.value} headless={settings.browser_headless} "
            f"wallet_manual_approval={settings.wallet_require_manual_approval}"
        )

    # ------------------------------------------------------------------ #
    # AI Model Manager
    # ------------------------------------------------------------------ #
    async def _handle_ai_model_command(self, intent: dict) -> tuple[str, dict]:
        from backend.planner.model_manager import TaskType, model_manager, parse_provider_name, parse_task_type

        action = intent.get("ai_action", "")
        provider = parse_provider_name(intent.get("ai_provider", "") or "") if intent.get("ai_provider") else None

        if action == "switch":
            if not provider:
                return "Which provider would you like to switch to? (Claude, GPT, Gemini, Groq, OpenRouter...)", {}
            model_manager.switch_provider(provider)
            return f"Switched to {provider.value}.", {"provider": provider.value}

        if action == "set_default":
            if not provider:
                return "Which provider should be the default?", {}
            model_manager.set_default_provider(provider)
            return f"{provider.value} is now the default provider.", {"provider": provider.value}

        if action == "enable_auto_routing":
            model_manager.enable_auto_routing(True)
            return "Automatic smart routing is now on -- tasks will be routed per the configured rules.", {}

        if action == "disable_auto_routing":
            model_manager.enable_auto_routing(False)
            return f"Automatic routing is off -- using the manually selected provider ({model_manager.current_provider.value}).", {}

        if action == "set_routing_rule":
            if not provider:
                return "Which provider should handle that task type?", {}
            task_type = parse_task_type(intent.get("ai_task_type", "") or "")
            if task_type is None:
                return "Which task type is this rule for (coding, browser automation, vision, fast response, etc.)?", {}
            model_manager.set_routing_rule(task_type, provider)
            return f"Routing rule updated: {task_type.value} -> {provider.value}.", {
                "task_type": task_type.value,
                "provider": provider.value,
            }

        if action == "temporary_use":
            if not provider:
                return "Which provider should I use for this one task?", {}
            model_manager.use_temporarily(provider, reason="chat: one-off override")
            return f"Using {provider.value} for the next request only, then reverting to the normal routing.", {
                "provider": provider.value
            }

        if action == "show_provider":
            return f"Current provider: {model_manager.current_provider.value}", {}

        if action == "show_model":
            return f"Current model: {model_manager.current_model}", {}

        if action == "show_providers":
            names = ", ".join(p.value for p in model_manager.health.keys())
            return f"Available providers: {names}", {}

        if action == "show_health":
            snapshot = model_manager.health_snapshot()
            lines = [
                f"{name}: {info['status']} (avail={info['availability']*100:.0f}%, latency={info['latency_ms'] or '—'}ms)"
                for name, info in snapshot.items()
                if info["total_requests"] > 0
            ]
            if not lines:
                return "No provider health data yet -- nothing has been called or tested this run.", {}
            return "Provider health:\n" + "\n".join(lines), {}

        if action == "show_routing":
            mode = model_manager.routing_mode
            rules = ", ".join(f"{t.value}->{p.value}" for t, p in model_manager.routing_rules.items())
            return f"Routing mode: {mode}. Rules: {rules}", {}

        return "Not sure which AI model action you mean -- try switch/set default/enable auto routing/show provider.", {}

    # ------------------------------------------------------------------ #
    # MCP Core
    # ------------------------------------------------------------------ #
    async def _handle_mcp_command(self, intent: dict, raw_text: str) -> tuple[str, dict]:
        """Dispatches category="mcp" messages to backend.mcp.manager.MCPManager
        (state.mcp) via route_and_call, which auto-selects the connector+tool
        from free text (optionally steered by mcp_connector as a hint). Free
        via Telegram too, since NexusTelegramBot._handle_chat_text ultimately
        calls ChatEngine.send_message."""
        mcp = getattr(self.app_state, "mcp", None) if self.app_state else None
        if mcp is None:
            return "MCP Core isn't enabled in this deployment.", {}

        query = (intent.get("mcp_query") or "").strip() or raw_text
        connector_hint = (intent.get("mcp_connector") or "").strip() or None

        result = await mcp.route_and_call(query, connector_hint=connector_hint)
        if result is None:
            return (
                "I couldn't figure out which tool that needs -- try naming the connector "
                "(filesystem/terminal/browser/github) or being more specific.",
                {},
            )

        meta = {"connector": result.connector, "tool": result.tool, "ok": result.ok}
        if not result.ok:
            return f"[{result.connector}.{result.tool}] failed: {result.error}", meta
        return f"[{result.connector}.{result.tool}] {result.output}", meta

    # ------------------------------------------------------------------ #
    # Skill Learning System
    # ------------------------------------------------------------------ #
    async def _handle_skill_command(self, session: ChatSession, intent: dict, raw_text: str) -> tuple[str, dict]:
        """Dispatches category="skill" messages to backend.skills.library.
        SkillService (state.skills) and backend.skills.teach.TeachModeManager
        (state.teach). Both are optional -- when skills_enabled=false
        neither is constructed in backend/main.py -- so this degrades to a
        plain explanation rather than an AttributeError."""
        skills = getattr(self.app_state, "skills", None) if self.app_state else None
        teach = getattr(self.app_state, "teach", None) if self.app_state else None
        if skills is None or teach is None:
            return "The Skill Library isn't enabled in this deployment.", {}

        action = (intent.get("skill_action") or intent.get("action") or "").strip().lower()
        skill_name = (intent.get("skill_name") or "").strip()
        skill_text = (intent.get("skill_text") or "").strip()

        if action == "learn":
            return await self._skill_learn_from_text(skills, teach, skill_text or raw_text)

        if action == "confirm":
            skill = await skills.confirm_pending()
            if skill is None:
                return "There's no pending skill suggestion to save.", {}
            return f"Saved '{skill['name']}' as a skill ({len(skill['workflow'])} step(s)).", {"skill_id": skill["id"]}

        if action == "discard":
            ok = skills.discard_pending()
            return ("Discarded -- I won't save that as a skill.", {}) if ok else (
                "There's no pending skill suggestion to discard.", {}
            )

        if action == "teach_start":
            draft = teach.start(session.id, name=skill_name, trigger="", website_hint="")
            name_part = f" for '{skill_name}'" if skill_name else ""
            return (
                f"Teach Mode started{name_part} -- describe one browser action at a time, e.g. "
                "\"click the Connect Wallet button\" or \"type {{amount}} into the amount field\". "
                "Say \"undo\" to remove the last step, \"done\" to save the skill, or \"cancel\" to discard it.",
                {"draft": draft.__dict__},
            )

        if action == "teach_finish":
            return await self._skill_teach_finish(session, skills, teach)

        if action == "teach_cancel":
            ok = teach.cancel(session.id)
            return ("Cancelled -- nothing was saved.", {}) if ok else ("No active Teach Mode session to cancel.", {})

        if action == "teach_undo":
            ok = teach.undo_last_step(session.id)
            return ("Removed the last step.", {}) if ok else ("No active Teach Mode session, or nothing to undo.", {})

        if action == "list":
            return await self._skill_list(skills)

        if action in ("enable", "disable"):
            target = await self._find_skill_by_name(skills, skill_name)
            if target is None:
                return f"Couldn't find a skill matching '{skill_name}'.", {}
            enabled = action == "enable"
            updated = await skills.set_enabled(target["id"], enabled)
            verb = "Enabled" if enabled else "Disabled"
            return f"{verb} '{updated['name']}'.", {"skill_id": updated["id"]}

        if action == "delete":
            target = await self._find_skill_by_name(skills, skill_name)
            if target is None:
                return f"Couldn't find a skill matching '{skill_name}'.", {}
            await skills.delete(target["id"])
            return f"Deleted '{target['name']}'.", {}

        if action == "correct":
            return await self._skill_correct(skills, teach, skill_name, skill_text or raw_text)

        return (
            "Not sure which skill action you mean -- try \"list my skills\", \"teach me a skill\", "
            "or \"learn how to ...\".",
            {},
        )

    async def _skill_learn_from_text(self, skills: Any, teach: Any, text: str) -> tuple[str, dict]:
        if not text:
            return "What should I learn? Describe the steps, e.g. \"learn how to check the gas price on etherscan\".", {}
        draft = await teach.parse_skill_from_text(text)
        if not draft.get("workflow"):
            return (
                "I couldn't pull concrete steps out of that -- try describing the exact clicks/typing "
                "involved, or say \"teach me a skill\" to walk through it step by step instead.",
                {},
            )
        skill = await skills.create(
            name=draft.get("name") or text[:60],
            description=draft.get("description", ""),
            category=draft.get("category", "general"),
            trigger=draft.get("trigger", ""),
            variables=draft.get("variables") or [],
            workflow=draft.get("workflow") or [],
            website_hint=draft.get("website_hint"),
            source=SkillSource.NATURAL_LANGUAGE,
        )
        return f"Learned a new skill: '{skill['name']}' ({len(skill['workflow'])} step(s)).", {"skill_id": skill["id"]}

    async def _skill_teach_finish(self, session: ChatSession, skills: Any, teach: Any) -> tuple[str, dict]:
        draft = teach.get_draft(session.id)
        if draft is None:
            return "No active Teach Mode session -- say \"teach me a skill\" to start one.", {}
        if not draft.steps:
            teach.cancel(session.id)
            return "No steps were taught, so there's nothing to save -- Teach Mode session ended.", {}
        teach.finish(session.id)
        skill = await skills.create(
            name=draft.name or "Taught skill",
            description=draft.description,
            category=draft.category,
            trigger=draft.trigger,
            website_hint=draft.website_hint or None,
            variables=draft.variables,
            workflow=draft.steps,
            source=SkillSource.TEACH_MODE,
        )
        return f"Saved skill '{skill['name']}' with {len(skill['workflow'])} step(s).", {"skill_id": skill["id"]}

    async def _skill_list(self, skills: Any) -> tuple[str, dict]:
        items = await skills.list()
        if not items:
            return "No skills learned yet -- try \"teach me a skill\" or \"learn how to ...\".", {}
        lines = ["Learned skills:"]
        for s in items[:20]:
            flag = "" if s["enabled"] else " (disabled)"
            lines.append(f"- {s['name']}{flag} — used {s['usage_count']}x, {int(s['success_rate'] * 100)}% success")
        if len(items) > 20:
            lines.append(f"...and {len(items) - 20} more. See the Skills page for the full list.")
        return "\n".join(lines), {"count": len(items)}

    async def _skill_correct(self, skills: Any, teach: Any, skill_name: str, instruction: str) -> tuple[str, dict]:
        if not instruction:
            return "What should that step have done instead?", {}
        target = await self._find_skill_by_name(skills, skill_name) or await self._most_recently_used_skill(skills)
        if target is None:
            return "I don't have a skill to correct yet -- mention which skill you mean.", {}
        corrected_step = await teach.parse_correction(instruction)
        workflow = list(target["workflow"])
        if workflow:
            workflow[-1] = corrected_step
            note = "corrected last step via chat"
        else:
            workflow.append(corrected_step)
            note = "appended corrected step via chat"
        updated = await skills.update(target["id"], {"workflow": workflow}, change_note=note)
        if updated is None:
            return f"Couldn't update '{target['name']}'.", {}
        return (
            f"Updated '{updated['name']}' -- {corrected_step.get('description') or 'step corrected'}.",
            {"skill_id": updated["id"]},
        )

    @staticmethod
    async def _find_skill_by_name(skills: Any, name: str) -> Optional[dict[str, Any]]:
        if not name:
            return None
        matches = await skills.list(search=name)
        if not matches:
            return None
        lname = name.strip().lower()
        for s in matches:
            if s["name"].strip().lower() == lname:
                return s
        return matches[0]

    @staticmethod
    async def _most_recently_used_skill(skills: Any) -> Optional[dict[str, Any]]:
        items = await skills.list()
        used = [s for s in items if s.get("last_used_at")]
        if not used:
            return None
        used.sort(key=lambda s: s["last_used_at"], reverse=True)
        return used[0]

    async def _handle_teach_turn(self, session: ChatSession, teach: Any, text: str) -> tuple[str, dict]:
        """One turn of an active Teach Mode session (backend.skills.teach.
        TeachModeManager). "undo"/"done"/"cancel" (and close synonyms) are
        matched directly rather than routed through the LLM classifier, so
        they behave deterministically even if a taught step happens to
        contain a similar word; anything else is parsed as a step via
        teach.add_step_from_text (which itself calls the LLM once, on the
        step text only)."""
        lowered = text.strip().lower().rstrip(".!")

        if lowered in ("cancel", "cancel teaching", "stop teaching", "forget this", "forget it", "abort"):
            teach.cancel(session.id)
            return "Cancelled -- I won't save that skill.", {}

        if lowered in ("undo", "undo that", "undo last step", "undo the last step", "undo step"):
            ok = teach.undo_last_step(session.id)
            return ("Removed the last step.", {}) if ok else ("Nothing to undo yet.", {})

        if lowered in ("done", "finish", "that's it", "thats it", "save it", "finished", "done teaching", "save"):
            skills = getattr(self.app_state, "skills", None) if self.app_state else None
            if skills is None:
                teach.cancel(session.id)
                return "The Skill Library isn't enabled in this deployment -- Teach Mode session ended.", {}
            return await self._skill_teach_finish(session, skills, teach)

        step = await teach.add_step_from_text(session.id, text)
        if step is None:
            return (
                "Couldn't parse that as a step -- try describing one browser action, e.g. "
                "\"click the Submit button\".",
                {},
            )
        draft = teach.get_draft(session.id)
        count = len(draft.steps) if draft else 0
        return (
            f"Got it: {step.get('description') or step.get('action')}. "
            f"({count} step(s) so far — say \"done\" to finish, \"undo\" to remove the last one, or keep going.)",
            {"step": step},
        )

    async def _handle_conversation(self, session: ChatSession, text: str) -> str:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        live_session = getattr(self.app_state, "live_session", None) if self.app_state else None

        status_line = "unknown"
        if agent:
            s = await agent.status()
            status_line = (
                f"status={s.get('status')} current_task={s.get('current_task_id') or 'none'} "
                f"current_website={s.get('current_website') or '—'} "
                f"tasks_completed={s.get('tasks_completed', 0)} tasks_failed={s.get('tasks_failed', 0)}"
            )
        browser_line = "unknown"
        if live_session:
            b = live_session.status()
            browser_line = f"active={b.get('active')} url={b.get('url') or '—'}"

        history = await self.get_history(session.id, limit=13)
        convo = "\n".join(f"{m.role.value}: {m.content}" for m in history[-13:-1])

        system_prompt = (
            "You are Nexus-Agent, chatting naturally with your operator. You control an autonomous "
            "browser-automation agent (Playwright-driven, non-custodial wallet approvals, task queue). "
            "Answer naturally and concisely -- a sentence or two for simple questions, more only if "
            "genuinely needed. If the user asks you to do something actionable, tell them what you're "
            "about to do; otherwise just answer.\n\n"
            f"Current agent status: {status_line}\n"
            f"Current browser: {browser_line}\n"
            f"Last known error this session: {session.last_error or 'none'}"
        )
        user_prompt = f"{convo}\nuser: {text}" if convo else text
        reply = await self.llm.complete_text(system_prompt, user_prompt)
        return reply.strip() or "..."

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _current_website(self) -> Optional[str]:
        live_session = getattr(self.app_state, "live_session", None) if self.app_state else None
        if live_session:
            status = live_session.status()
            if status.get("active"):
                return status.get("url") or None
        return None

    async def _last_failed_report(self, session: ChatSession) -> Optional[Report]:
        async with get_session() as db:
            candidate_id = session.last_task_id
            if candidate_id:
                result = await db.execute(select(Report).where(Report.task_id == candidate_id))
                report = result.scalar_one_or_none()
                if report and report.status in ("failed", "cancelled"):
                    return report
            result = await db.execute(
                select(Report).where(Report.status == "failed").order_by(Report.created_at.desc()).limit(1)
            )
            return result.scalar_one_or_none()

    async def _today_summary(self) -> tuple[str, dict]:
        start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        async with get_session() as db:
            tasks_result = await db.execute(select(Task).where(Task.created_at >= start))
            tasks = list(tasks_result.scalars().all())
            reports_result = await db.execute(select(Report).where(Report.created_at >= start))
            reports = list(reports_result.scalars().all())

        if not tasks and not reports:
            return "Nothing has run today yet.", {}

        succeeded = sum(1 for r in reports if r.status == "succeeded")
        failed = sum(1 for r in reports if r.status in ("failed", "cancelled"))
        lines = [f"Today: {len(tasks)} task(s) queued, {succeeded} succeeded, {failed} failed/blocked."]
        for t in tasks[:10]:
            status_val = t.status.value if hasattr(t.status, "value") else t.status
            lines.append(f"- [{status_val}] {t.website} :: {t.goal[:60]}")
        return "\n".join(lines), {}

    async def _touch_session(self, session_id: str, last_task_id: Optional[str] = None, last_error: Optional[str] = None) -> None:
        async with get_session() as db:
            row = await db.get(ChatSession, session_id)
            if row is None:
                return
            if last_task_id is not None:
                row.last_task_id = last_task_id
            if last_error is not None:
                row.last_error = last_error

    async def _append(
        self, session_id: str, role: ChatRole, content: str, category: Optional[str] = None, meta: Optional[dict] = None
    ) -> None:
        async with get_session() as db:
            db.add(ChatMessage(session_id=session_id, role=role, content=content, category=category, meta_json=meta or {}))
