from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.app_state import state
from backend.api.routes_agent import router as agent_router
from backend.api.routes_browser import router as browser_router
from backend.api.routes_logs import WebSocketLogBroadcastHandler
from backend.api.routes_logs import router as logs_router
from backend.api.routes_memory import router as memory_router
from backend.api.routes_plugins import router as plugins_router
from backend.api.routes_reports import router as reports_router
from backend.api.routes_settings import router as settings_router
from backend.api.routes_tasks import router as tasks_router
from backend.api.routes_wallet import router as wallet_router
from backend.browser.live_session import LiveSessionManager
from backend.config.settings import LOG_DIR, settings
from backend.database.session import init_db
from backend.memory.store import MemoryStore
from backend.planner.agent_runtime import AgentRuntime
from backend.planner.task_queue import TaskQueueService
from backend.plugins.registry import PluginRegistry
from backend.wallet.manager import WalletManager
from backend.wallet.registry import WalletRegistry

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "nexus.log"),
        logging.StreamHandler(),
    ],
)
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
    state.wallet = WalletManager()
    state.wallet_registry = WalletRegistry()

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

    state.queue = TaskQueueService(
        memory=state.memory, wallet=state.wallet, notify_fn=_broadcast_notify, plugin_registry=state.plugins
    )

    state.agent = AgentRuntime(queue=state.queue, on_activity_broadcast=_broadcast_agent_activity)
    await state.agent.start()  # background execution: recovers interrupted tasks, then starts the worker loop

    state.live_session = LiveSessionManager(
        engine_provider=lambda: state.queue.current_engine if state.queue else None,
        task_id_provider=lambda: state.queue.current_task_id if state.queue else None,
    )
    state.live_session.start()

    global _telegram_app
    if settings.telegram_bot_token:
        from backend.telegram.bot import NexusTelegramBot

        bot = NexusTelegramBot(queue=state.queue)
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

    if state.plugins:
        await state.plugins.unload_all()

    if _ws_log_handler is not None:
        logging.getLogger().removeHandler(_ws_log_handler)
        _ws_log_handler = None


async def _broadcast_notify(message: str) -> None:
    from backend.api.routes_tasks import broadcast

    await broadcast(message)
    logger.info("notify: %s", message)


async def _broadcast_plugin_event(payload: str) -> None:
    from backend.api.routes_plugins import broadcast

    await broadcast(payload)


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
app.include_router(memory_router)
app.include_router(reports_router)
app.include_router(wallet_router)
app.include_router(browser_router)
app.include_router(logs_router)
app.include_router(settings_router)
app.include_router(plugins_router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "app": settings.app_name}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=settings.api_host, port=settings.api_port, reload=settings.debug)
