"""
Browser MCP connector.

Read-only web access via `httpx.AsyncClient`, following the same
request-style used by `backend/planner/llm_client.py` (async client used as
a plain awaited call rather than kept open across requests, except here the
client genuinely is long-lived for connection pooling since this connector
issues many small fetches over its lifetime).

This is deliberately *not* a second Playwright instance -- the agent
already owns one live browser session (`backend/browser/engine.py`) and
spinning up a parallel one would double resource usage and risk the two
stepping on each other. Instead, an optional `engine_provider` callable can
be wired in (by MCPManager/main.py) so `current_page_snapshot` can read the
*existing* live session instead of duplicating it. Without an
`engine_provider`, every tool here still works except that one.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

import httpx

from backend.mcp.base import ConnectorHealth, ConnectorStatus, MCPConnector, MCPTool, MCPToolError

logger = logging.getLogger("nexus.mcp.browser")

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_TEXT_CHARS = 20_000
DEFAULT_MAX_LINKS = 200

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_LINK_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


class BrowserMCPConnector(MCPConnector):
    name = "browser"
    version = "1.0.0"
    description = "Read-only web access: fetch a URL's text/links, or snapshot the agent's live page."
    tags = ["web", "url", "page", "fetch", "navigate", "scrape"]

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        engine_provider: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        super().__init__(config)
        self._timeout: float = float(self.config.get("timeout", DEFAULT_TIMEOUT))
        self._client: Optional[httpx.AsyncClient] = None
        # Callable[[], Optional[BrowserEngine]] -- resolved lazily each call
        # so it always reflects whichever engine is currently live, not
        # whichever one existed when the connector was constructed.
        self._engine_provider: Optional[Callable[[], Optional[Any]]] = (
            engine_provider or self.config.get("engine_provider")
        )

    def set_engine_provider(self, provider: Optional[Callable[[], Optional[Any]]]) -> None:
        """Lets MCPManager/main.py wire the live BrowserEngine in after
        construction, since the engine itself may not exist yet at the
        point connectors are built."""
        self._engine_provider = provider

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": "Nexus-Agent-MCP/1.0"},
        )
        await super().connect()

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await super().disconnect()

    async def health_check(self) -> ConnectorHealth:
        if self.status != ConnectorStatus.CONNECTED:
            return await super().health_check()
        detail = "http client ready"
        if self._engine_provider is not None:
            detail += "; live engine wired"
        return ConnectorHealth(ConnectorStatus.CONNECTED, detail)

    def list_tools(self) -> list[MCPTool]:
        tools = [
            MCPTool(
                name="fetch_url",
                description="Fetch a URL and return its readable text (HTML tags stripped).",
                input_schema={
                    "url": "string",
                    "extract_text": "boolean (optional, default true; false returns raw HTML truncated)",
                },
                keywords=["fetch url", "fetch page", "get url", "download page", "read website", "open link"],
            ),
            MCPTool(
                name="get_page_links",
                description="Fetch a URL and return the links (href + text) found on the page.",
                input_schema={"url": "string"},
                keywords=["page links", "list links", "find links", "extract links"],
            ),
            MCPTool(
                name="current_page_snapshot",
                description="Snapshot of the agent's own current live browser page (visible text + interactive elements), if a task is running.",
                input_schema={},
                keywords=["current page", "what's on screen", "live page", "browser snapshot", "what am I looking at"],
            ),
        ]
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name == "fetch_url":
            return await self._fetch_url(arguments.get("url", ""), bool(arguments.get("extract_text", True)))
        if tool_name == "get_page_links":
            return await self._get_page_links(arguments.get("url", ""))
        if tool_name == "current_page_snapshot":
            return await self._current_page_snapshot()
        raise MCPToolError(f"unknown tool '{tool_name}'")

    # ---- Operations -----------------------------------------------------
    async def _get(self, url: str) -> httpx.Response:
        if not url or not url.strip():
            raise MCPToolError("url is required")
        if self._client is None:
            raise MCPToolError("browser connector is not connected")
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise MCPToolError(f"failed to fetch '{url}': {exc}") from exc
        return resp

    async def _fetch_url(self, url: str, extract_text: bool) -> dict[str, Any]:
        resp = await self._get(url)
        body = resp.text
        if not extract_text:
            return {
                "url": str(resp.url),
                "status_code": resp.status_code,
                "content": body[:DEFAULT_MAX_TEXT_CHARS],
                "truncated": len(body) > DEFAULT_MAX_TEXT_CHARS,
            }
        text = _html_to_text(body)
        return {
            "url": str(resp.url),
            "status_code": resp.status_code,
            "text": text[:DEFAULT_MAX_TEXT_CHARS],
            "truncated": len(text) > DEFAULT_MAX_TEXT_CHARS,
        }

    async def _get_page_links(self, url: str) -> dict[str, Any]:
        resp = await self._get(url)
        links = []
        for match in _LINK_RE.finditer(resp.text):
            href = match.group(1).strip()
            link_text = _ANY_TAG_RE.sub("", match.group(2)).strip()
            if not href:
                continue
            links.append({"href": href, "text": link_text[:200]})
            if len(links) >= DEFAULT_MAX_LINKS:
                break
        return {"url": str(resp.url), "links": links}

    async def _current_page_snapshot(self) -> dict[str, Any]:
        if self._engine_provider is None:
            raise MCPToolError(
                "no live browser engine is wired to this connector (no task currently running an engine)"
            )
        engine = self._engine_provider()
        if engine is None:
            raise MCPToolError("no live browser session is currently active")
        snapshot = await engine.snapshot(name_hint="mcp_snapshot")
        return {
            "url": snapshot.url,
            "title": snapshot.title,
            "visible_text": snapshot.visible_text,
            "interactive_elements": snapshot.interactive_elements,
        }


def _html_to_text(html: str) -> str:
    without_scripts = _TAG_RE.sub(" ", html)
    text = _ANY_TAG_RE.sub(" ", without_scripts)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()
