"""
Discord MCP connector.

Same shared-session pattern as backend/mcp/connectors/x_connector.py and
backend/mcp/connectors/gmail_connector.py -- see social_base.py. Drives
Discord's web client (discord.com) over the profile's live authenticated
BrowserEngine session rather than a bot token, since the mission calls for
reusing the *user's own* browser session/identity, not a separate bot
application.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.mcp.base import MCPTool, MCPToolError
from backend.mcp.connectors.social_base import SESSION_CONNECTED, SocialMCPConnector, require_confirm

logger = logging.getLogger("nexus.mcp.discord")

APP_URL = "https://discord.com/channels/@me"
MESSAGE_BOX_CANDIDATES = [
    'div[role="textbox"][aria-label*="Message"]',
    'div[data-slate-editor="true"]',
]
UPLOAD_INPUT_CANDIDATES = ['input[type="file"]']


class DiscordMCPConnector(SocialMCPConnector):
    name = "discord"
    version = "1.0.0"
    description = "Discord: detect servers/channels, read messages, send/reply, and upload files -- via the live authenticated browser session."
    tags = ["discord", "chat", "server", "channel", "message"]
    service = "discord"

    def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="detect_login_state",
                description="Check whether Discord is currently logged in on the active browser profile's session.",
                input_schema={},
                keywords=["discord login", "is discord connected", "discord session status"],
            ),
            MCPTool(
                name="list_servers",
                description="List Discord servers (guilds) visible in the sidebar of the live session.",
                input_schema={},
                keywords=["discord servers", "list guilds", "my discord servers"],
            ),
            MCPTool(
                name="list_channels",
                description="List channels for a given server id/name visible in the live session.",
                input_schema={"server": "string (server name or id, as shown in list_servers)"},
                keywords=["discord channels", "list channels", "channels in server"],
            ),
            MCPTool(
                name="read_channel",
                description="Read the most recent visible messages in a channel (by channel URL).",
                input_schema={"channel_url": "string", "limit": "integer (optional, default 30)"},
                keywords=["read discord channel", "discord messages", "channel history"],
            ),
            MCPTool(
                name="send_message",
                description="Send a message to a channel (by channel URL). Requires confirm=true -- show the user the exact text, and only pass confirm=true once they've explicitly approved it.",
                input_schema={"channel_url": "string", "text": "string", "confirm": "boolean (must be true)"},
                keywords=["send discord message", "post to channel", "message discord channel"],
                destructive=True,
            ),
            MCPTool(
                name="reply",
                description="Reply to the most recent message in a channel with the given text (best-effort: uses Discord's inline reply on the last visible message). Requires confirm=true -- same as send_message.",
                input_schema={"channel_url": "string", "text": "string", "confirm": "boolean (must be true)"},
                keywords=["reply on discord", "reply to message", "discord reply"],
                destructive=True,
            ),
            MCPTool(
                name="upload_file",
                description="Upload a file to a channel, optionally with a caption. Requires confirm=true -- same as send_message.",
                input_schema={"channel_url": "string", "file_path": "string", "caption": "string (optional)", "confirm": "boolean (must be true)"},
                keywords=["upload file discord", "send file", "attach file discord"],
                destructive=True,
            ),
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        self._record_call()
        if tool_name == "detect_login_state":
            return await self._detect_login_state()
        if tool_name == "list_servers":
            return await self._list_servers()
        if tool_name == "list_channels":
            return await self._list_channels(arguments.get("server", ""))
        if tool_name == "read_channel":
            return await self._read_channel(arguments.get("channel_url", ""), int(arguments.get("limit", 30) or 30))
        if tool_name == "send_message":
            return await self._send_message(arguments)
        if tool_name == "reply":
            return await self._reply(arguments)
        if tool_name == "upload_file":
            return await self._upload_file(arguments)
        raise MCPToolError(f"unknown tool '{tool_name}'")

    # ---- Operations -------------------------------------------------------
    async def _detect_login_state(self) -> dict[str, Any]:
        state = await self._detect_state()
        return {"service": "discord", "session_status": state, "authenticated": state == SESSION_CONNECTED}

    async def _list_servers(self) -> dict[str, Any]:
        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(APP_URL)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            elements = await engine.extract_interactive_elements(limit=150)
        except Exception as exc:  # noqa: BLE001
            raise MCPToolError(f"discord: failed to list servers: {exc}") from exc
        finally:
            if page_id:
                await engine.close_tab(page_id)
        servers = [
            {"name": el.get("text", ""), "id": el.get("id", "")}
            for el in elements
            if el.get("role") in ("button", "link") and el.get("text")
        ]
        return {"url": APP_URL, "servers": servers}

    async def _list_channels(self, server: str) -> dict[str, Any]:
        if not server:
            raise MCPToolError("server is required (name or id from list_servers)")
        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(APP_URL)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            clicked = await engine.smart_click(server)
            if not clicked:
                raise MCPToolError(f"discord: could not find server '{server}' in the sidebar")
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            elements = await engine.extract_interactive_elements(limit=150)
        except MCPToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MCPToolError(f"discord: failed to list channels: {exc}") from exc
        finally:
            if page_id:
                await engine.close_tab(page_id)
        channels = [
            {"name": el.get("text", ""), "id": el.get("id", "")}
            for el in elements
            if el.get("role") == "link" and el.get("text")
        ]
        return {"server": server, "channels": channels}

    async def _read_channel(self, channel_url: str, limit: int) -> dict[str, Any]:
        if not channel_url:
            raise MCPToolError("channel_url is required")
        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(channel_url)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            text = await engine.extract_visible_text(max_chars=8_000)
        except Exception as exc:  # noqa: BLE001
            raise MCPToolError(f"discord: failed to read channel: {exc}") from exc
        finally:
            if page_id:
                await engine.close_tab(page_id)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return {"channel_url": channel_url, "messages": lines[-max(1, limit):]}

    async def _send_message(self, arguments: dict[str, Any]) -> dict[str, Any]:
        channel_url = arguments.get("channel_url", "")
        text = arguments.get("text", "")
        if not channel_url:
            raise MCPToolError("channel_url is required")
        if not text or not text.strip():
            raise MCPToolError("text is required")
        require_confirm(arguments, "send_message")
        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(channel_url)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            typed = await self._type_any(engine, MESSAGE_BOX_CANDIDATES, text)
            if not typed:
                raise MCPToolError("discord: could not find the message box for this channel")
            await engine.page.keyboard.press("Enter")
            await engine.smart_wait("networkidle", timeout_ms=5_000)
        finally:
            if page_id:
                await engine.close_tab(page_id)
        return {"sent": True, "channel_url": channel_url, "text": text}

    async def _reply(self, arguments: dict[str, Any]) -> dict[str, Any]:
        channel_url = arguments.get("channel_url", "")
        text = arguments.get("text", "")
        if not channel_url:
            raise MCPToolError("channel_url is required")
        if not text or not text.strip():
            raise MCPToolError("text is required")
        require_confirm(arguments, "reply")
        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(channel_url)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            # Best-effort: hover/reply affordance on Discord requires a message
            # context menu; falling back to a plain send in-channel (which
            # Discord visually threads under the active reply target if the
            # user set one) keeps this connector resilient to markup changes.
            await engine.smart_click("Reply")
            typed = await self._type_any(engine, MESSAGE_BOX_CANDIDATES, text)
            if not typed:
                raise MCPToolError("discord: could not find the message box to reply in this channel")
            await engine.page.keyboard.press("Enter")
            await engine.smart_wait("networkidle", timeout_ms=5_000)
        finally:
            if page_id:
                await engine.close_tab(page_id)
        return {"replied": True, "channel_url": channel_url, "text": text}

    async def _upload_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        channel_url = arguments.get("channel_url", "")
        file_path = arguments.get("file_path", "")
        caption = arguments.get("caption", "")
        if not channel_url:
            raise MCPToolError("channel_url is required")
        if not file_path:
            raise MCPToolError("file_path is required")
        require_confirm(arguments, "upload_file")
        engine = await self._ensure_session()
        page_id = None
        try:
            page_id = await engine.new_tab(channel_url)
            engine.switch_tab(page_id)
            await engine.smart_wait("networkidle", timeout_ms=8_000)
            uploaded = False
            for candidate in UPLOAD_INPUT_CANDIDATES:
                if await engine.upload_file(candidate, file_path):
                    uploaded = True
                    break
            if not uploaded:
                raise MCPToolError("discord: could not find a file upload input in this channel")
            if caption:
                await self._type_any(engine, MESSAGE_BOX_CANDIDATES, caption)
            await engine.page.keyboard.press("Enter")
            await engine.smart_wait("networkidle", timeout_ms=8_000)
        finally:
            if page_id:
                await engine.close_tab(page_id)
        return {"uploaded": True, "channel_url": channel_url, "file_path": file_path, "caption": caption}

    @staticmethod
    async def _type_any(engine: Any, candidates: list[str], text: str) -> bool:
        for candidate in candidates:
            if await engine.smart_type(candidate, text):
                return True
        return False
