"""
Built-in MCP connectors registry. backend/mcp/registry.py imports
`BUILTIN_CONNECTORS` from here as its default connector-class mapping.
"""
from __future__ import annotations

from backend.mcp.connectors.browser import BrowserMCPConnector
from backend.mcp.connectors.discord_connector import DiscordMCPConnector
from backend.mcp.connectors.filesystem import FilesystemMCPConnector
from backend.mcp.connectors.github import GitHubMCPConnector
from backend.mcp.connectors.gmail_connector import GmailMCPConnector
from backend.mcp.connectors.terminal import TerminalMCPConnector
from backend.mcp.connectors.x_connector import XMCPConnector

BUILTIN_CONNECTORS = {
    "filesystem": FilesystemMCPConnector,
    "terminal": TerminalMCPConnector,
    "browser": BrowserMCPConnector,
    "github": GitHubMCPConnector,
    "x": XMCPConnector,
    "discord": DiscordMCPConnector,
    "gmail": GmailMCPConnector,
}

# Connectors whose tools automate a live authenticated browser session
# (backend/mcp/connectors/social_base.py) rather than a first-party API --
# MCPManager uses this to know which connectors need the shared
# BrowserEngine provider wired in (see wire_browser_engine_provider()).
SOCIAL_CONNECTOR_NAMES = ("x", "discord", "gmail")

__all__ = [
    "BUILTIN_CONNECTORS",
    "SOCIAL_CONNECTOR_NAMES",
    "FilesystemMCPConnector",
    "TerminalMCPConnector",
    "BrowserMCPConnector",
    "GitHubMCPConnector",
    "XMCPConnector",
    "DiscordMCPConnector",
    "GmailMCPConnector",
]
