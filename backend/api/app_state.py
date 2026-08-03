from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from backend.memory.store import MemoryStore
from backend.planner.task_queue import TaskQueueService
from backend.wallet.manager import WalletManager
from backend.wallet.registry import WalletRegistry

if TYPE_CHECKING:
    from backend.browser.live_session import LiveSessionManager
    from backend.plugins.registry import PluginRegistry
    from backend.planner.agent_runtime import AgentRuntime
    from backend.planner.chat_engine import ChatEngine
    from backend.skills.library import SkillService
    from backend.skills.teach import TeachModeManager
    from backend.mcp.manager import MCPManager
    from backend.identity.registry import ProfileRegistry
    from backend.identity.manager import ProfileManager
    from backend.wallet.tx_batch import TxBatchManager
    from backend.identity.pending_profile import PendingProfileManager


class AppState:
    memory: Optional[MemoryStore] = None
    wallet: Optional[WalletManager] = None
    wallet_registry: Optional[WalletRegistry] = None
    queue: Optional[TaskQueueService] = None
    live_session: Optional["LiveSessionManager"] = None
    plugins: Optional["PluginRegistry"] = None
    agent: Optional["AgentRuntime"] = None
    chat: Optional["ChatEngine"] = None
    skills: Optional["SkillService"] = None
    teach: Optional["TeachModeManager"] = None
    tx_batch: Optional["TxBatchManager"] = None
    mcp: Optional["MCPManager"] = None
    profile_registry: Optional["ProfileRegistry"] = None
    profiles: Optional["ProfileManager"] = None
    pending_profile: Optional["PendingProfileManager"] = None


state = AppState()
