from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# Playwright launches the browser as a subprocess, which asyncio's default
# SelectorEventLoop on Windows does not support (raises NotImplementedError
# from asyncio.create_subprocess_exec). Some dependency imported below sets
# WindowsSelectorEventLoopPolicy as a side effect, so this must run before
# any of those imports.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.app_state import state
from backend.api.routes_agent import router as agent_router
from backend.api.routes_ai_models import router as ai_models_router
from backend.api.routes_browser import router as browser_router
from backend.api.routes_chat import router as chat_router
from backend.api.routes_logs import WebSocketLogBroadcastHandler
from backend.api.routes_logs import router as logs_router
from backend.api.routes_mcp import router as mcp_router
from backend.api.routes_memory import router as memory_router
from backend.api.routes_plugins import router as plugins_router
from backend.api.routes_profiles import router as profiles_router
from backend.api.routes_reports import router as reports_router
from backend.api.routes_settings import router as settings_router
from backend.api.routes_skills import router as skills_router
from backend.api.routes_system import router as system_router
from backend.api.routes_tasks import router as tasks_router
from backend.api.routes_wallet import router as wallet_router
from backend.browser.live_session import LiveSessionManager
from backend.config.settings import DATA_DIR, LOG_DIR, settings
from backend.database.session import init_db
from backend.identity.manager import ProfileManager
from backend.identity.registry import ProfileRegistry
from backend.identity.pending_profile import PendingProfileManager
from backend.mcp.manager import MCPManager
from backend.memory.store import MemoryStore
from backend.planner.agent_runtime import AgentRuntime
from backend.planner.chat_engine import ChatEngine
from backend.planner.task_queue import TaskQueueService
from backend.plugins.registry import PluginRegistry
from backend.skills.library import SkillService
from backend.skills.teach import TeachModeManager
from backend.wallet.chain_confirm import ChainConfirmationManager
from backend.wallet.hot_signer import (
    HotSigner,
    HotSignerDisabled,
    hot_signer_keystore_exists,
    unlock_hot_signer,
)
from backend.wallet.manager import WalletManager
from backend.wallet.registry import WalletRegistry
from backend.wallet.tx_batch import TxBatchManager

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "nexus.log"),
        logging.StreamHandler(),
    ],
)

# These libraries emit a DEBUG line for every single HTTP request/response
# or SQL statement (headers, body chunks, connection open/close, every
# query executed twice as "executing"/"operation ... completed") -- this
# floods the log (and the dashboard's live Logs panel, which tails
# nexus.log) even in normal DEBUG mode. Keep them at WARNING regardless of
# settings.debug; app-level DEBUG logs are unaffected.
for _noisy_logger in ("httpx", "httpcore", "telegram", "aiosqlite", "chromadb"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

# chromadb 0.5.x has a known bug where it tries to send telemetry via
# posthog even with anonymized_telemetry=False, and the installed posthog
# version's capture() signature doesn't match what chromadb calls with
# ("capture() takes 1 positional argument but 3 were given"). chromadb
# catches this itself and logs it as ERROR, but it's harmless -- silence
# this specific sub-logger only (WARNING above wouldn't hide ERROR).
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)

logger = logging.getLogger("nexus.main")

_telegram_app = None
_ws_log_handler: logging.Handler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Bridge stdlib logging -> WS /api/logs/ws/live. Additive: the existing
    # FileHandler/StreamHandler from logging.basicConfig above are untouched.
    global _ws_log_handler
    _ws_log_handler = WebSocketLogBroadcastHandler(asyncio.get_running_loop())
    _ws_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(_ws_log_handler)

    state.memory = MemoryStore()
    await state.memory.backfill_legacy_entries()  # one-time: categorize/score pre-upgrade memories
    state.memory.start()  # background Memory Expiration Policy sweep
    state.wallet = WalletManager()
    state.wallet_registry = WalletRegistry()
    state.tx_batch = TxBatchManager()
    state.chain_confirm = ChainConfirmationManager()
    state.hot_signer = HotSigner(wallet_registry=state.wallet_registry)

    # If a hot-signer keystore file exists from a prior "save as hot signer"
    # import, unlock it now so HOT_SIGNER_* is live for this process without
    # needing a plaintext key in .env. Non-interactive: requires
    # KEYSTORE_PASSPHRASE in the environment, never prompts (this runs
    # during server startup, but keep the pattern consistent with the
    # request-handling callers in routes_wallet.py / chat_engine.py).
    # Missing/locked keystore is not fatal -- hot signer just stays
    # disabled until the user re-imports or sets the passphrase and
    # restarts.
    try:
        if hot_signer_keystore_exists():
            unlocked_address = unlock_hot_signer()
            logger.info("Hot signer keystore unlocked at startup (address=%s)", unlocked_address)
    except HotSignerDisabled as exc:
        logger.warning("Hot signer keystore present but not unlocked at startup: %s", exc)

    state.plugins = PluginRegistry(
        plugins_dir=settings.plugins_dir,
        memory=state.memory,
        notify_fn=_broadcast_notify,
        event_fn=_broadcast_plugin_event,
    )
    if settings.plugins_enabled:
        await state.plugins.load_all()
        logger.info("Plugins loaded: %s", [p["name"] for p in state.plugins.list_plugins() if p["enabled"]])
    state.wallet.plugin_registry = state.plugins

    if settings.skills_enabled:
        state.skills = SkillService()
        state.teach = TeachModeManager()

    state.mcp = MCPManager.from_settings(settings, DATA_DIR)
    state.mcp.on_call = _record_mcp_call
    await state.mcp.start()

    state.profile_registry = ProfileRegistry(DATA_DIR)
    await state.profile_registry.reset_stale_in_use_profiles()
    state.profiles = ProfileManager(state.profile_registry)
    state.pending_profile = PendingProfileManager()

    state.queue = TaskQueueService(
        memory=state.memory,
        wallet=state.wallet,
        notify_fn=_broadcast_notify,
        plugin_registry=state.plugins,
        skills=state.skills,
        mcp=state.mcp,
        profiles=state.profiles,
    )
    if state.mcp:
        state.mcp.wire_browser_engine_provider(lambda: state.queue.current_engine if state.queue else None)

    state.agent = AgentRuntime(queue=state.queue, on_activity_broadcast=_broadcast_agent_activity)
    await state.agent.start()  # background execution: recovers interrupted tasks, then starts the worker loop

    state.chat = ChatEngine(queue=state.queue, app_state=state)

    state.live_session = LiveSessionManager(
        engine_provider=lambda: state.queue.current_engine if state.queue else None,
        task_id_provider=lambda: state.queue.current_task_id if state.queue else None,
    )
    state.live_session.start()

    global _telegram_app
    if settings.telegram_bot_token:
        from backend.telegram.bot import NexusTelegramBot

        bot = NexusTelegramBot(queue=state.queue, app_state=state)
        _telegram_app = bot.build()
        await _telegram_app.initialize()
        await _telegram_app.start()
        await _telegram_app.updater.start_polling()
        logger.info("Telegram bot started")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set -- Telegram control disabled")

    logger.info("Nexus-Agent backend ready on %s:%s", settings.api_host, settings.api_port)
    yield

    if _telegram_app:
        await _telegram_app.updater.stop()
        await _telegram_app.stop()
        await _telegram_app.shutdown()

    if state.live_session:
        await state.live_session.stop()

    if state.memory:
        await state.memory.stop()

    if state.plugins:
        await state.plugins.unload_all()

    if state.mcp:
        await state.mcp.stop()

    if _ws_log_handler is not None:
        logging.getLogger().removeHandler(_ws_log_handler)
        _ws_log_handler = None


async def _broadcast_notify(message: str) -> None:
    from backend.api.routes_tasks import broadcast

    await broadcast(message)
    logger.info("notify: %s", message)

    if state.chat is not None:
        try:
            from backend.database.models import ChatRole, ChatSession
            from backend.database.session import get_session
            from sqlalchemy import select

            async with get_session() as db:
                stmt = select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(1)
                res = await db.execute(stmt)
                session = res.scalars().first()
                if session:
                    await state.chat._append(
                        session.id,
                        ChatRole.ASSISTANT,
                        message,
                        category="agent_command",
                    )
        except Exception:
            logger.exception("Failed to mirror notify message into chat session")


async def _broadcast_plugin_event(payload: str) -> None:
    from backend.api.routes_plugins import broadcast

    await broadcast(payload)


async def _record_mcp_call(connector: str, tool: str, arguments: dict, result) -> None:
    if state.memory:
        await state.memory.save_tool_call(connector, tool, arguments, result)


async def _broadcast_agent_activity(event: dict) -> None:
    from backend.api.routes_agent import broadcast

    await broadcast(event)


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tasks_router)
app.include_router(agent_router)
app.include_router(chat_router)
app.include_router(memory_router)
app.include_router(reports_router)
app.include_router(wallet_router)
app.include_router(browser_router)
app.include_router(logs_router)
app.include_router(settings_router)
app.include_router(plugins_router)
app.include_router(system_router)
app.include_router(skills_router)
app.include_router(mcp_router)
app.include_router(profiles_router)
app.include_router(ai_models_router)


@app.get("/api/health")
async def health():
    """Lightweight liveness probe (unauthenticated, for load balancers/orchestrators).
    For the full component-by-component breakdown, see GET /api/system/health."""
    return {"status": "ok", "app": settings.app_name}


if __name__ == "__main__":
    import uvicorn

    # See scripts/dev.ps1 for why --reload/reload=True is unsafe on Windows:
    # uvicorn forces WindowsSelectorEventLoopPolicy for its reload worker
    # subprocess, which breaks Playwright (asyncio subprocess support).
    _reload = settings.debug and sys.platform != "win32"
    uvicorn.run("backend.main:app", host=settings.api_host, port=settings.api_port, reload=_reload)
