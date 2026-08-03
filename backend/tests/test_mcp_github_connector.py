import pytest

from backend.mcp.base import ConnectorStatus, MCPToolError
from backend.mcp.connectors.github import GitHubMCPConnector


class RecordingConnector(GitHubMCPConnector):
    """Subclass that records _request() calls instead of hitting the network."""

    def __init__(self, config=None):
        super().__init__(config)
        self.requests = []
        self._fake_response = {"ok": True}

    async def _request(self, method, path, *, params=None, json_body=None):
        self.requests.append({"method": method, "path": path, "params": params, "json_body": json_body})
        return self._fake_response


@pytest.mark.asyncio
async def test_get_repository_builds_correct_path():
    conn = RecordingConnector(config={"default_owner": "acme", "default_repo": "widgets"})
    await conn.call_tool("get_repository", {})
    assert conn.requests == [{"method": "GET", "path": "/repos/acme/widgets", "params": None, "json_body": None}]


@pytest.mark.asyncio
async def test_get_repository_explicit_args_override_defaults():
    conn = RecordingConnector(config={"default_owner": "acme", "default_repo": "widgets"})
    await conn.call_tool("get_repository", {"owner": "other", "repo": "thing"})
    assert conn.requests[0]["path"] == "/repos/other/thing"


@pytest.mark.asyncio
async def test_list_issues_builds_path_and_state_param():
    conn = RecordingConnector(config={"default_owner": "acme", "default_repo": "widgets"})
    await conn.call_tool("list_issues", {"state": "closed"})
    assert conn.requests == [
        {"method": "GET", "path": "/repos/acme/widgets/issues", "params": {"state": "closed"}, "json_body": None}
    ]


@pytest.mark.asyncio
async def test_list_issues_defaults_state_to_open():
    conn = RecordingConnector(config={"default_owner": "acme", "default_repo": "widgets"})
    await conn.call_tool("list_issues", {})
    assert conn.requests[0]["params"] == {"state": "open"}


@pytest.mark.asyncio
async def test_create_issue_builds_path_and_json_body():
    conn = RecordingConnector(config={"default_owner": "acme", "default_repo": "widgets"})
    await conn.call_tool("create_issue", {"title": "Bug found", "body": "steps to reproduce..."})
    assert conn.requests == [
        {
            "method": "POST",
            "path": "/repos/acme/widgets/issues",
            "params": None,
            "json_body": {"title": "Bug found", "body": "steps to reproduce..."},
        }
    ]


@pytest.mark.asyncio
async def test_create_issue_requires_title():
    conn = RecordingConnector(config={"default_owner": "acme", "default_repo": "widgets"})
    with pytest.raises(MCPToolError, match="title is required"):
        await conn.call_tool("create_issue", {"body": "no title here"})


@pytest.mark.asyncio
async def test_list_pull_requests_builds_path_and_state_param():
    conn = RecordingConnector(config={"default_owner": "acme", "default_repo": "widgets"})
    await conn.call_tool("list_pull_requests", {"state": "all"})
    assert conn.requests == [
        {"method": "GET", "path": "/repos/acme/widgets/pulls", "params": {"state": "all"}, "json_body": None}
    ]


@pytest.mark.asyncio
async def test_get_file_contents_builds_path_with_ref_param():
    conn = RecordingConnector(config={"default_owner": "acme", "default_repo": "widgets"})
    await conn.call_tool("get_file_contents", {"path": "README.md", "ref": "main"})
    assert conn.requests == [
        {
            "method": "GET",
            "path": "/repos/acme/widgets/contents/README.md",
            "params": {"ref": "main"},
            "json_body": None,
        }
    ]


@pytest.mark.asyncio
async def test_get_file_contents_without_ref_has_no_params():
    conn = RecordingConnector(config={"default_owner": "acme", "default_repo": "widgets"})
    await conn.call_tool("get_file_contents", {"path": "README.md"})
    assert conn.requests[0]["params"] is None


@pytest.mark.asyncio
async def test_get_file_contents_requires_path():
    conn = RecordingConnector(config={"default_owner": "acme", "default_repo": "widgets"})
    with pytest.raises(MCPToolError, match="path is required"):
        await conn.call_tool("get_file_contents", {})


@pytest.mark.asyncio
async def test_missing_owner_and_repo_raises_tool_error():
    conn = RecordingConnector()
    with pytest.raises(MCPToolError, match="owner and repo are required"):
        await conn.call_tool("get_repository", {})


@pytest.mark.asyncio
async def test_health_check_reports_degraded_without_token():
    conn = GitHubMCPConnector(config={})
    await conn.connect()
    health = await conn.health_check()
    assert health.status == ConnectorStatus.CONNECTED
    assert "degraded" in health.detail
    assert "no token configured" in health.detail


@pytest.mark.asyncio
async def test_health_check_reports_ok_with_token():
    conn = GitHubMCPConnector(config={"token": "ghp_fake_token"})
    await conn.connect()
    health = await conn.health_check()
    assert health.status == ConnectorStatus.CONNECTED
    assert health.detail == "ok: token configured"
