"""
Plugin API for Nexus-Agent.

A plugin is a single Python module living under `backend/plugins/installed/`
that defines exactly one subclass of `NexusPlugin`. The registry
(`backend/plugins/registry.py`) discovers, loads, enables/disables, and
dispatches lifecycle hooks to plugin instances.

Design rules (do not weaken):
- Hooks are best-effort observers, not gatekeepers. A plugin can inspect a
  task/step/wallet-decision and act on it (log it, notify someone, write to
  its own storage), but it cannot block, mutate, or override the agent's
  behavior -- the registry only ever calls hooks with copies/reads of state,
  and return values are ignored for every hook except `on_wallet_popup`,
  which may only ever narrow an approval (turn approve->reject), never
  widen one. See `registry.PluginRegistry.dispatch_wallet_popup`.
- A plugin must never receive private keys or seed phrases -- those never
  exist as Python values outside `backend/wallet/import_utils.py` in the
  first place (see that module's docstring), so there is nothing to leak
  here structurally.
- Every hook has a no-op default so a plugin only implements what it needs.
- All hooks are async and MUST NOT be assumed to run to completion before
  the agent continues in every case -- see registry docstring for the exact
  dispatch semantics (fire-and-isolated for most hooks; awaited only for
  `on_wallet_popup`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PluginContext:
    """
    Read-only-by-convention handle passed to on_load so a plugin can reach
    shared services it needs (memory store for read queries, a notify
    function for sending a message to the user, and its own persistent
    config dict). Plugins should not reach into private attributes of these
    objects; only the documented public methods.
    """

    memory: Any = None
    notify_fn: Optional[Any] = None  # async callable(str) -> None
    config: dict = field(default_factory=dict)  # this plugin's own settings, from plugins.json


class NexusPlugin:
    """Base class every plugin subclasses. Override only the hooks you need."""

    #: Unique plugin id. Defaults to the class name if not overridden.
    name: str = ""
    #: Free-form version string, shown in the plugin list.
    version: str = "0.1.0"
    #: One-line human description, shown in the dashboard.
    description: str = ""

    def __init__(self) -> None:
        if not self.name:
            self.name = type(self).__name__

    # ---- Lifecycle -------------------------------------------------
    async def on_load(self, ctx: PluginContext) -> None:
        """Called once when the plugin is enabled (at startup or via the API)."""

    async def on_unload(self) -> None:
        """Called once when the plugin is disabled or the app is shutting down."""

    # ---- Task lifecycle ---------------------------------------------
    async def on_task_start(self, task_id: str, website: str, goal: str) -> None:
        """Called right before the agent loop starts working a task."""

    async def on_step(self, task_id: str, step: Any) -> None:
        """
        Called after each executed browser step. `step` is a
        `backend.planner.agent_loop.StepResult` (read-only: treat as
        immutable, it's shared with other plugins in dispatch order).
        """

    async def on_task_finish(self, task_id: str, status: str, summary: str) -> None:
        """Called once a task reaches a terminal status (succeeded/failed/blocked/cancelled)."""

    # ---- Wallet ------------------------------------------------------
    async def on_wallet_popup(self, task_id: str, contract_address: Optional[str], estimated_value: Optional[float], approve: bool) -> Optional[bool]:
        """
        Called after WalletManager's own allow-policy has produced a
        decision, before it's acted on. Return `False` to veto an approval
        (turn it into a reject); return `None` or `True` to leave the
        existing decision alone. A plugin can never turn a reject into an
        approve -- the registry enforces this, see
        `PluginRegistry.dispatch_wallet_popup`.
        """
        return None
