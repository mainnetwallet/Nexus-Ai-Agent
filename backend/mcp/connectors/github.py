"""
GitHub MCP connector.

Thin wrapper over the public GitHub REST API (https://api.github.com) via
`httpx.AsyncClient`. The actual HTTP call is isolated in `_request()` so
tests can monkeypatch it and exercise every tool without real network
access, matching `backend/tests/test_llm_client.py`'s pattern for
`LLMClient` (build/parse methods kept separate and individually testable).

Auth: `config["token"]` if configured via `MCPManager.configure("github",
...)`, otherwise falls back to `Settings.mcp_github_token`. Unauthenticated
use still works for public-repo reads (GitHub's public rate limit just
applies), so `health_check()` reports "degraded" rather than "error" when
no token is present -- the connector is still usable, just rate-limited.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from backend.config.settings import settings as global_settings
from backend.mcp.base import ConnectorHealth, ConnectorStatus, MCPConnector, MCPTool, MCPToolError

logger = logging.getLogger("nexus.mcp.github")

API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 20.0


class GitHubMCPConnector(MCPConnector):
    name = "github"
    version = "1.0.0"
    description = "Read repositories/issues/PRs/files and create issues via the GitHub REST API."
    tags = ["github", "git", "repo", "repository", "issue", "pull request", "code"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._timeout: float = float(self.config.get("timeout", DEFAULT_TIMEOUT))
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def _token(self) -> str:
        return self.config.get("token") or getattr(global_settings, "mcp_github_token", "") or ""

    @property
    def _default_owner(self) -> str:
        return self.config.get("default_owner") or getattr(global_settings, "mcp_github_default_owner", "") or ""

    @property
    def _default_repo(self) -> str:
        return self.config.get("default_repo") or getattr(global_settings, "mcp_github_default_repo", "") or ""

    async def connect(self) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._client = httpx.AsyncClient(base_url=API_BASE, timeout=self._timeout, headers=headers)
        await super().connect()

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await super().disconnect()

    async def health_check(self) -> ConnectorHealth:
        if self.status != ConnectorStatus.CONNECTED:
            return await super().health_check()
        if not self._token:
            return ConnectorHealth(
                ConnectorStatus.CONNECTED, "degraded: no token configured, public reads only (rate-limited)"
            )
        return ConnectorHealth(ConnectorStatus.CONNECTED, "ok: token configured")

    def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="get_repository",
                description="Get metadata for a repository.",
                input_schema={"owner": "string (optional, defaults to configured owner)", "repo": "string (optional, defaults to configured repo)"},
                keywords=["repository info", "repo details", "get repo", "github repository"],
            ),
            MCPTool(
                name="list_issues",
                description="List issues on a repository.",
                input_schema={"owner": "string (optional)", "repo": "string (optional)", "state": "string (optional: open|closed|all, default open)"},
                keywords=["list issues", "github issues", "check issues", "open issues"],
            ),
            MCPTool(
                name="create_issue",
                description="Create a new issue on a repository.",
                input_schema={"owner": "string (optional)", "repo": "string (optional)", "title": "string", "body": "string (optional)"},
                keywords=["create issue", "file issue", "open issue", "report bug on github"],
                destructive=True,
            ),
            MCPTool(
                name="list_pull_requests",
                description="List pull requests on a repository.",
                input_schema={"owner": "string (optional)", "repo": "string (optional)", "state": "string (optional: open|closed|all, default open)"},
                keywords=["list pull requests", "list prs", "github pull requests", "open prs"],
            ),
            MCPTool(
                name="get_file_contents",
                description="Get the contents of a file in a repository.",
                input_schema={"owner": "string (optional)", "repo": "string (optional)", "path": "string", "ref": "string (optional, branch/commit/tag)"},
                keywords=["file contents", "read file from github", "github file", "get file"],
            ),
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        owner = arguments.get("owner") or self._default_owner
        repo = arguments.get("repo") or self._default_repo
        if tool_name == "get_repository":
            self._require(owner, repo)
            return await self._get_repository(owner, repo)
        if tool_name == "list_issues":
            self._require(owner, repo)
            return await self._list_issues(owner, repo, arguments.get("state", "open"))
        if tool_name == "create_issue":
            self._require(owner, repo)
            if not arguments.get("title"):
                raise MCPToolError("title is required")
            return await self._create_issue(owner, repo, arguments["title"], arguments.get("body", ""))
        if tool_name == "list_pull_requests":
            self._require(owner, repo)
            return await self._list_pull_requests(owner, repo, arguments.get("state", "open"))
        if tool_name == "get_file_contents":
            self._require(owner, repo)
            if not arguments.get("path"):
                raise MCPToolError("path is required")
            return await self._get_file_contents(owner, repo, arguments["path"], arguments.get("ref"))
        raise MCPToolError(f"unknown tool '{tool_name}'")

    @staticmethod
    def _require(owner: str, repo: str) -> None:
        if not owner or not repo:
            raise MCPToolError("owner and repo are required (pass explicitly or configure default_owner/default_repo)")

    # ---- HTTP -------------------------------------------------------
    async def _request(self, method: str, path: str, *, params: Optional[dict[str, Any]] = None, json_body: Optional[dict[str, Any]] = None) -> Any:
        """Isolated so tests can monkeypatch this single method instead of
        mocking the network, matching test_llm_client.py's approach."""
        if self._client is None:
            raise MCPToolError("github connector is not connected")
        try:
            resp = await self._client.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise MCPToolError(f"github request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise MCPToolError(f"github API error {resp.status_code} for {method} {path}: {resp.text[:500]}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ---- Operations ---------------------------------------------------
    async def _get_repository(self, owner: str, repo: str) -> Any:
        return await self._request("GET", f"/repos/{owner}/{repo}")

    async def _list_issues(self, owner: str, repo: str, state: str) -> Any:
        return await self._request("GET", f"/repos/{owner}/{repo}/issues", params={"state": state or "open"})

    async def _create_issue(self, owner: str, repo: str, title: str, body: str) -> Any:
        return await self._request("POST", f"/repos/{owner}/{repo}/issues", json_body={"title": title, "body": body})

    async def _list_pull_requests(self, owner: str, repo: str, state: str) -> Any:
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls", params={"state": state or "open"})

    async def _get_file_contents(self, owner: str, repo: str, path: str, ref: Optional[str]) -> Any:
        params = {"ref": ref} if ref else None
        return await self._request("GET", f"/repos/{owner}/{repo}/contents/{path}", params=params)
