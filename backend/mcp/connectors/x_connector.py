"""
X (Twitter) MCP connector.

Drives the shared live BrowserEngine's already-authenticated X session --
see backend/mcp/connectors/social_base.py for the shared engine/session
plumbing this builds on. No X API keys are used or required; everything
here is DOM automation over the existing web session, matching the
mission's "reuse existing authenticated browser sessions" requirement.

`publish_post` and `reply` are gated behind `require_confirm()` -- they
will not touch the page at all until the caller passes `confirm=true`,
which callers (Chat/Agent Runtime) are expected to only do after showing
the user the exact draft text.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.mcp.base import MCPTool, MCPToolError
from backend.mcp.connectors.social_base import SESSION_CONNECTED, SocialMCPConnector, require_confirm

logger = logging.getLogger("nexus.mcp.x")

HOME_URL = "https://x.com/home"
NOTIFICATIONS_URL = "https://x.com/notifications"
COMPOSE_SELECTOR_CANDIDATES = [
    '[data-testid="tweetTextarea_0"]',
    'div[aria-label="Post text"]',
    'div[aria-label="Tweet text"]',
]
POST_BUTTON_CANDIDATES = [
    '[data-testid="tweetButton"]',
    '[data-testid="tweetButtonInline"]',
    "Post",
]
REPLY_BUTTON_CANDIDATES = ['[data-testid="reply"]', "Reply"]


class XMCPConnector(SocialMCPConnector):
    name = "x"
    version = "1.0.0"
    description = "X (Twitter): read profile/notifications, draft/publish posts, and reply -- via the live authenticated browser session."
    tags = ["x", "twitter", "social", "post", "tweet", "notifications"]
    service = "x"

    def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="detect_login_state",
                description="Check whether X is currently logged in on the active browser profile's session.",
                input_schema={},
                keywords=["x login", "twitter login", "is x connected", "x session status"],
            ),
            MCPTool(
                name="read_profile",
                description="Read the current user's own X profile summary (handle, display name, bio) from the live session.",
                input_schema={},
                keywords=["my x profile", "my twitter profile", "x account info", "who am i on x"],
            ),
            MCPTool(
                name="read_notifications",
                description="Read the current X notifications feed (best-effort text extraction).",
                input_schema={"limit": "integer (optional, default 20, max visible items)"},
                keywords=["x notifications", "twitter notifications", "check x mentions", "x alerts"],
            ),
            MCPTool(
                name="draft_post",
                description="Prepare a post's text for review WITHOUT publishing it. Returns the text back for the user to confirm.",
                input_schema={"text": "string"},
                keywords=["draft x post", "draft tweet", "compose tweet", "write a post"],
            ),
            MCPTool(
                name="publish_post",
                description=(
                    "Publish a post to X. Requires confirm=true -- call draft_post first, show the user "
                    "the exact text, and only pass confirm=true once they've explicitly approved it."
                ),
                input_schema={"text": "string", "confirm": "boolean (must be true)"},
                keywords=["publish tweet", "post to x", "send tweet", "tweet this"],
                destructive=True,
            ),
            MCPTool(
                name="reply",
                description=(
                    "Reply to a specific post (by URL). Requires confirm=true and prior explicit user authorization, "
                    "same as publish_post."
                ),
                input_schema={"post_url": "string", "text": "string", "confirm": "boolean (must be true)"},
                keywords=["reply to tweet", "reply on x", "respond to post"],
                destructive=True,
            ),
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        self._record_call()
        if tool_name == "detect_login_state":
            return await self._detect_login_state()
        if tool_name == "read_profile":
            return await self._read_profile()
        if tool_name == "read_notifications":
            return await self._read_notifications(int(arguments.get("limit", 20) or 20))
        if tool_name == "draft_post":
            return self._draft_post(arguments.get("text", ""))
        if tool_name == "publish_post":
            return await self._publish_post(arguments)
        if tool_name == "reply":
            return await self._reply(arguments)
        raise MCPToolError(f"unknown tool '{tool_name}'")

    # ---- Operations -------------------------------------------------------
    async def _detect_login_state(self) -> dict[str, Any]:
        state = await self._detect_state()
        return {"service": "x", "session_status": state, "authenticated": state == SESSION_CONNECTED}

    async def _read_profile(self) -> dict[str, Any]:
        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(HOME_URL)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            text = await engine.extract_visible_text(max_chars=4_000)
        except Exception as exc:  # noqa: BLE001
            raise MCPToolError(f"x: failed to read profile: {exc}") from exc
        finally:
            if page_id:
                await engine.close_tab(page_id)
        return {"url": HOME_URL, "summary_text": text[:2_000]}

    async def _read_notifications(self, limit: int) -> dict[str, Any]:
        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(NOTIFICATIONS_URL)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            text = await engine.extract_visible_text(max_chars=8_000)
        except Exception as exc:  # noqa: BLE001
            raise MCPToolError(f"x: failed to read notifications: {exc}") from exc
        finally:
            if page_id:
                await engine.close_tab(page_id)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return {"url": NOTIFICATIONS_URL, "items": lines[: max(1, limit)]}

    def _draft_post(self, text: str) -> dict[str, Any]:
        if not text or not text.strip():
            raise MCPToolError("text is required to draft a post")
        if len(text) > 280:
            raise MCPToolError(f"draft exceeds X's 280 character limit ({len(text)} chars)")
        return {"draft": text, "char_count": len(text), "status": "drafted -- not published; call publish_post with confirm=true to send"}

    async def _publish_post(self, arguments: dict[str, Any]) -> dict[str, Any]:
        text = arguments.get("text", "")
        if not text or not text.strip():
            raise MCPToolError("text is required")
        if len(text) > 280:
            raise MCPToolError(f"post exceeds X's 280 character limit ({len(text)} chars)")
        require_confirm(arguments, "publish_post")

        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(HOME_URL)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            typed = await self._smart_type_any(engine, COMPOSE_SELECTOR_CANDIDATES, text)
            if not typed:
                raise MCPToolError("x: could not find the post composer on the page")
            clicked = await self._smart_click_any(engine, POST_BUTTON_CANDIDATES)
            if not clicked:
                raise MCPToolError("x: could not find the Post button")
            await engine.smart_wait("networkidle", timeout_ms=8_000)
        finally:
            if page_id:
                await engine.close_tab(page_id)
        return {"published": True, "text": text}

    async def _reply(self, arguments: dict[str, Any]) -> dict[str, Any]:
        post_url = arguments.get("post_url", "")
        text = arguments.get("text", "")
        if not post_url:
            raise MCPToolError("post_url is required")
        if not text or not text.strip():
            raise MCPToolError("text is required")
        require_confirm(arguments, "reply")

        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(post_url)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            clicked_reply = await self._smart_click_any(engine, REPLY_BUTTON_CANDIDATES)
            if not clicked_reply:
                raise MCPToolError("x: could not find the reply button on the target post")
            typed = await self._smart_type_any(engine, COMPOSE_SELECTOR_CANDIDATES, text)
            if not typed:
                raise MCPToolError("x: could not find the reply composer")
            clicked_send = await self._smart_click_any(engine, POST_BUTTON_CANDIDATES)
            if not clicked_send:
                raise MCPToolError("x: could not find the Reply/Post button")
            await engine.smart_wait("networkidle", timeout_ms=8_000)
        finally:
            if page_id:
                await engine.close_tab(page_id)
        return {"replied": True, "post_url": post_url, "text": text}

    # ---- Small DOM helpers (shared shape across candidates) ---------------
    @staticmethod
    async def _smart_click_any(engine: Any, candidates: list[str]) -> bool:
        for candidate in candidates:
            if await engine.smart_click(candidate):
                return True
        return False

    @staticmethod
    async def _smart_type_any(engine: Any, candidates: list[str], text: str) -> bool:
        for candidate in candidates:
            if await engine.smart_type(candidate, text):
                return True
        return False
