"""
Gmail MCP connector.

Same shared-session pattern as x_connector.py/discord_connector.py -- see
social_base.py. Drives Gmail's web client (mail.google.com) over the
profile's live authenticated BrowserEngine session; no Gmail API OAuth
client is used, matching the mission's "reuse existing authenticated
browser sessions" requirement and keeping this connector credential-free.

`send_email` is gated behind `require_confirm()`; `reply` is also gated
since it sends mail on the user's behalf, same reasoning as X's publish_post.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.mcp.base import MCPTool, MCPToolError
from backend.mcp.connectors.social_base import SESSION_CONNECTED, SocialMCPConnector, require_confirm

logger = logging.getLogger("nexus.mcp.gmail")

INBOX_URL = "https://mail.google.com/mail/u/0/#inbox"
SEARCH_URL_TMPL = "https://mail.google.com/mail/u/0/#search/{query}"
COMPOSE_BUTTON_CANDIDATES = ['[gh="cm"]', "Compose"]
TO_FIELD_CANDIDATES = ['textarea[name="to"]', 'input[aria-label="To recipients"]', "To"]
SUBJECT_FIELD_CANDIDATES = ['input[name="subjectbox"]', "Subject"]
BODY_FIELD_CANDIDATES = ['div[aria-label="Message Body"]', 'div[role="textbox"][g_editable="true"]']
SEND_BUTTON_CANDIDATES = ['div[aria-label*="Send"]', "Send"]
REPLY_BUTTON_CANDIDATES = ['span[data-tooltip="Reply"]', "Reply"]


class GmailMCPConnector(SocialMCPConnector):
    name = "gmail"
    version = "1.0.0"
    description = "Gmail: read/search inbox, draft/reply/send email -- via the live authenticated browser session."
    tags = ["gmail", "email", "mail", "inbox"]
    service = "gmail"

    def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="detect_login_state",
                description="Check whether Gmail is currently logged in on the active browser profile's session.",
                input_schema={},
                keywords=["gmail login", "is gmail connected", "email session status"],
            ),
            MCPTool(
                name="read_inbox",
                description="Read the most recent visible messages in the inbox.",
                input_schema={"limit": "integer (optional, default 20)"},
                keywords=["read inbox", "check email", "gmail inbox", "new emails"],
            ),
            MCPTool(
                name="search_emails",
                description="Search Gmail using a Gmail search query string (e.g. 'from:boss subject:invoice').",
                input_schema={"query": "string", "limit": "integer (optional, default 20)"},
                keywords=["search email", "search gmail", "find email"],
            ),
            MCPTool(
                name="draft_email",
                description="Prepare an email's subject/body for review WITHOUT sending it.",
                input_schema={"to": "string", "subject": "string", "body": "string"},
                keywords=["draft email", "compose email", "write an email"],
            ),
            MCPTool(
                name="send_email",
                description=(
                    "Send a new email. Requires confirm=true -- call draft_email first, show the user "
                    "the exact to/subject/body, and only pass confirm=true once they've explicitly approved it."
                ),
                input_schema={"to": "string", "subject": "string", "body": "string", "confirm": "boolean (must be true)"},
                keywords=["send email", "email this", "send this message"],
                destructive=True,
            ),
            MCPTool(
                name="reply",
                description=(
                    "Reply to an email thread (by URL). Requires confirm=true and prior explicit user "
                    "authorization, same as send_email."
                ),
                input_schema={"thread_url": "string", "body": "string", "confirm": "boolean (must be true)"},
                keywords=["reply to email", "respond to email", "reply gmail"],
                destructive=True,
            ),
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        self._record_call()
        if tool_name == "detect_login_state":
            return await self._detect_login_state()
        if tool_name == "read_inbox":
            return await self._read_inbox(int(arguments.get("limit", 20) or 20))
        if tool_name == "search_emails":
            return await self._search_emails(arguments.get("query", ""), int(arguments.get("limit", 20) or 20))
        if tool_name == "draft_email":
            return self._draft_email(arguments.get("to", ""), arguments.get("subject", ""), arguments.get("body", ""))
        if tool_name == "send_email":
            return await self._send_email(arguments)
        if tool_name == "reply":
            return await self._reply(arguments)
        raise MCPToolError(f"unknown tool '{tool_name}'")

    # ---- Operations -------------------------------------------------------
    async def _detect_login_state(self) -> dict[str, Any]:
        state = await self._detect_state()
        return {"service": "gmail", "session_status": state, "authenticated": state == SESSION_CONNECTED}

    async def _read_inbox(self, limit: int) -> dict[str, Any]:
        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(INBOX_URL)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            text = await engine.extract_visible_text(max_chars=8_000)
        except Exception as exc:  # noqa: BLE001
            raise MCPToolError(f"gmail: failed to read inbox: {exc}") from exc
        finally:
            if page_id:
                await engine.close_tab(page_id)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return {"url": INBOX_URL, "items": lines[: max(1, limit)]}

    async def _search_emails(self, query: str, limit: int) -> dict[str, Any]:
        if not query or not query.strip():
            raise MCPToolError("query is required")
        engine = await self._ensure_session()
        page_id = None
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        try:
            page_id = await engine.new_tab(url)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            text = await engine.extract_visible_text(max_chars=8_000)
        except Exception as exc:  # noqa: BLE001
            raise MCPToolError(f"gmail: search failed: {exc}") from exc
        finally:
            if page_id:
                await engine.close_tab(page_id)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return {"query": query, "url": url, "items": lines[: max(1, limit)]}

    def _draft_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        if not to or not to.strip():
            raise MCPToolError("to is required to draft an email")
        return {
            "draft": {"to": to, "subject": subject, "body": body},
            "status": "drafted -- not sent; call send_email with confirm=true to send",
        }

    async def _send_email(self, arguments: dict[str, Any]) -> dict[str, Any]:
        to = arguments.get("to", "")
        subject = arguments.get("subject", "")
        body = arguments.get("body", "")
        if not to or not to.strip():
            raise MCPToolError("to is required")
        require_confirm(arguments, "send_email")

        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(INBOX_URL)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            if not await self._click_any(engine, COMPOSE_BUTTON_CANDIDATES):
                raise MCPToolError("gmail: could not find the Compose button")
            await engine.smart_wait("networkidle", timeout_ms=5_000)
            if not await self._type_any(engine, TO_FIELD_CANDIDATES, to):
                raise MCPToolError("gmail: could not find the To field")
            if subject and not await self._type_any(engine, SUBJECT_FIELD_CANDIDATES, subject):
                raise MCPToolError("gmail: could not find the Subject field")
            if body and not await self._type_any(engine, BODY_FIELD_CANDIDATES, body):
                raise MCPToolError("gmail: could not find the message body field")
            if not await self._click_any(engine, SEND_BUTTON_CANDIDATES):
                raise MCPToolError("gmail: could not find the Send button")
            await engine.smart_wait("networkidle", timeout_ms=8_000)
        finally:
            if page_id:
                await engine.close_tab(page_id)
        return {"sent": True, "to": to, "subject": subject, "body": body}

    async def _reply(self, arguments: dict[str, Any]) -> dict[str, Any]:
        thread_url = arguments.get("thread_url", "")
        body = arguments.get("body", "")
        if not thread_url:
            raise MCPToolError("thread_url is required")
        if not body or not body.strip():
            raise MCPToolError("body is required")
        require_confirm(arguments, "reply")

        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(thread_url)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            if not await self._click_any(engine, REPLY_BUTTON_CANDIDATES):
                raise MCPToolError("gmail: could not find the Reply button on this thread")
            await engine.smart_wait("networkidle", timeout_ms=5_000)
            if not await self._type_any(engine, BODY_FIELD_CANDIDATES, body):
                raise MCPToolError("gmail: could not find the reply body field")
            if not await self._click_any(engine, SEND_BUTTON_CANDIDATES):
                raise MCPToolError("gmail: could not find the Send button")
            await engine.smart_wait("networkidle", timeout_ms=8_000)
        finally:
            if page_id:
                await engine.close_tab(page_id)
        return {"replied": True, "thread_url": thread_url, "body": body}

    @staticmethod
    async def _click_any(engine: Any, candidates: list[str]) -> bool:
        for candidate in candidates:
            if await engine.smart_click(candidate):
                return True
        return False

    @staticmethod
    async def _type_any(engine: Any, candidates: list[str], text: str) -> bool:
        for candidate in candidates:
            if await engine.smart_type(candidate, text):
                return True
        return False
