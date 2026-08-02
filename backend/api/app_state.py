from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from backend.memory.store import MemoryStore
from backend.planner.task_queue import TaskQueueService
from backend.wallet.manager import WalletManager
from backend.wallet.registry import WalletRegistry

if TYPE_CHECKING:
    from backend.browser.live_session import LiveSessionManager
    from backend.plugins.registry import PluginRegistry


class AppState:
    memory: Optional[MemoryStore] = None
    wallet: Optional[WalletManager] = None
    wallet_registry: Optional[WalletRegistry] = None
    queue: Optional[TaskQueueService] = None
    live_session: Optional["LiveSessionManager"] = None
    plugins: Optional["PluginRegistry"] = None


state = AppState()
