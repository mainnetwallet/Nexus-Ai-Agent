"""
Shared base for browser-session-driven social MCP connectors (X, Discord,
Gmail).

These three services don't get a REST/OAuth client the way
`backend/mcp/connectors/github.py` does -- per the mission they must reuse
the agent's *existing authenticated browser session* instead of a second,
disconnected credential flow. Concretely that means driving the same
`BrowserEngine` (backend/browser/engine.py) instance the active task/profile
already has open, exactly like `BrowserMCPConnector.current_page_snapshot`
does via its `engine_provider` callable (backend/mcp/connectors/browser.py)
-- this module generalizes that one pattern into a base class instead of
duplicating it three times.

Login/session detection is *not* reimplemented here either: it delegates to
`backend/identity/detector.py`'s `SessionDetector`, the same probe the
Identity/Profile Manager uses at task start. A connector built on this base
never fills in a username/password field anywhere -- if the session isn't
authenticated, every tool raises `SessionRequiredError` (a `MCPToolError`
subclass) telling the caller to log in manually, matching
`ProfileManager.check_sessions`'s "notify, never assume credentials" rule.

Selector strategy: methods here call `BrowserEngine.smart_click` /
`smart_type`, which already try several locator strategies (CSS, text,
role, label) in turn -- see engine.py. Site markup changes over time, so
every DOM interaction in the concrete connectors is wrapped and raises a
`MCPToolError` with a clear message rather than letting a raw Playwright
exception escape, matching `base.py`'s "expected failure -> MCPToolError"
contract.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from backend.browser.engine import BrowserEngine
from backend.identity.detector import SessionDetector
from backend.mcp.base import ConnectorHealth, ConnectorStatus, MCPConnector, MCPToolError

logger = logging.getLogger("nexus.mcp.social")

# Mirrors ProfileManager's session vocabulary (backend/identity/manager.py)
# plus SESSION_UNKNOWN for "couldn't determine" -- kept as plain strings
# (not an Enum) since these flow straight into dashboard JSON payloads.
SESSION_CONNECTED = "connected"
SESSION_LOGIN_REQUIRED = "login_required"
SESSION_EXPIRED = "session_expired"
SESSION_UNKNOWN = "unknown"


class SessionRequiredError(MCPToolError):
    """Raised when a tool needs an authenticated session that isn't available.

    Kept as a distinct subclass (rather than a bare MCPToolError) so
    callers -- Chat/Agent Runtime/Dashboard -- can special-case "please log
    in" versus "the request itself was invalid" if useful, without any of
    them being *required* to catch it specially (it's still a MCPToolError).
    """


def require_confirm(arguments: dict[str, Any], action: str) -> None:
    """Shared confirmation gate for irreversible, outward-facing actions
    (publishing a post, sending an email). Mirrors GitHubMCPConnector's
    `destructive=True` tool flag, but enforced in-band here (raise unless
    the caller explicitly passed confirm=true) since a social platform
    doesn't offer an undo the way a filesystem write does."""
    if arguments.get("confirm") is not True:
        raise MCPToolError(
            f"{action} requires explicit user confirmation -- show the draft to the user first, "
            f"then call again with confirm=true once they approve it."
        )


class SocialMCPConnector(MCPConnector):
    """Base class for connectors that automate an already-logged-in web
    session rather than calling a first-party API. Subclasses set `service`
    to one of SessionDetector.SUPPORTED_SERVICES ("gmail" | "x" | "discord")
    and implement their own `list_tools()`/`call_tool()`."""

    #: SessionDetector service key -- must match backend/identity/detector.py
    service: str = ""

    def __init__(
        self,
        config: Optional[dict[str, Any]] = None,
        engine_provider: Optional[Callable[[], Optional[BrowserEngine]]] = None,
        detector: Optional[SessionDetector] = None,
    ) -> None:
        super().__init__(config)
        # Callable[[], Optional[BrowserEngine]] -- resolved lazily on every
        # call so it always reflects whichever profile's session is
        # currently live, never one captured at construction time. Wired by
        # MCPManager.wire_browser_engine_provider() after main.py's
        # TaskQueueService exists (see backend/mcp/manager.py).
        self._engine_provider: Optional[Callable[[], Optional[BrowserEngine]]] = (
            engine_provider or self.config.get("engine_provider")
        )
        self._detector = detector or SessionDetector()
        self._last_used_at: Optional[float] = None
        self._last_session_status: str = SESSION_UNKNOWN
        # Account label is display-only (e.g. "alice@gmail.com" or
        # "@alice"); never a credential. Comes from connector config, which
        # MCPManager.from_settings/registry.configure() populate -- the
        # Identity/Profile Manager's own per-profile account labels
        # (ProfileRecord.gmail_account/x_account/discord_account) are the
        # source of truth and are surfaced to the dashboard separately via
        # backend/api/routes_profiles.py, not duplicated into this connector.
        self._account: Optional[str] = self.config.get("account") or None

    def set_engine_provider(self, provider: Optional[Callable[[], Optional[BrowserEngine]]]) -> None:
        self._engine_provider = provider

    async def connect(self) -> None:
        # No separate network client to open -- every tool call routes
        # through the shared BrowserEngine, resolved lazily per-call.
        await super().connect()

    async def disconnect(self) -> None:
        await super().disconnect()

    # ---- Engine / session plumbing --------------------------------------
    def _get_engine(self) -> BrowserEngine:
        if self._engine_provider is None:
            raise MCPToolError(
                f"{self.name}: no live browser engine is wired to this connector "
                f"(no task is currently running an engine)"
            )
        engine = self._engine_provider()
        if engine is None:
            raise MCPToolError(
                f"{self.name}: no live browser session is currently active -- start a task "
                f"with a profile configured for {self.service or self.name} first"
            )
        return engine

    async def _detect_state(self) -> str:
        """Best-effort session probe; never raises -- returns SESSION_UNKNOWN
        on any failure so health_check()/status_snapshot() stay cheap."""
        try:
            engine = self._get_engine()
        except MCPToolError:
            return SESSION_UNKNOWN
        try:
            check = await self._detector.detect(engine, self.service)
        except Exception:  # noqa: BLE001
            logger.debug("%s: session detection raised", self.name, exc_info=True)
            return SESSION_UNKNOWN
        if check.authenticated is True:
            return SESSION_CONNECTED
        if check.authenticated is False:
            return SESSION_LOGIN_REQUIRED
        return SESSION_UNKNOWN

    async def _ensure_session(self) -> BrowserEngine:
        """Resolves the live engine AND confirms the session is
        authenticated before a tool touches the page. Raises
        SessionRequiredError (never proceeds silently) if not."""
        engine = self._get_engine()
        check = await self._detector.detect(engine, self.service)
        if check.authenticated is True:
            self._last_session_status = SESSION_CONNECTED
            return engine
        if check.authenticated is False:
            self._last_session_status = SESSION_LOGIN_REQUIRED
            raise SessionRequiredError(
                f"{self.name}: not authenticated in the active browser profile's session. "
                f"Please log in to {self.service} manually in that profile's Chrome window -- "
                f"this connector will never enter credentials on your behalf."
            )
        self._last_session_status = SESSION_UNKNOWN
        raise SessionRequiredError(
            f"{self.name}: could not determine session state ({check.detail}). Try again in a moment."
        )

    def _record_call(self) -> None:
        self._last_used_at = time.time()

    # ---- Health / dashboard ----------------------------------------------
    async def health_check(self) -> ConnectorHealth:
        if self.status != ConnectorStatus.CONNECTED:
            return await super().health_check()
        state = await self._detect_state()
        self._last_session_status = state
        detail = f"session: {state}"
        if self._last_used_at is not None:
            detail += f"; last used {int(time.time() - self._last_used_at)}s ago"
        else:
            detail += "; never used"
        return ConnectorHealth(ConnectorStatus.CONNECTED, detail)

    async def status_snapshot(self) -> dict[str, Any]:
        """Richer dashboard payload than health_check() alone -- Connection
        Status, Session Status, Account Information, Last Used, all in one
        call, used by GET /api/mcp/social-status (backend/api/routes_mcp.py)."""
        session_status = await self._detect_state()
        self._last_session_status = session_status
        return {
            "connector": self.name,
            "service": self.service,
            "connection_status": self.status.value,
            "session_status": session_status,
            "account": self._account,
            "last_used_at": self._last_used_at,
        }
