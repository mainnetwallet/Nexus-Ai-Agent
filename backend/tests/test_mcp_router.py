import pytest

from backend.mcp.base import ConnectorStatus, MCPConnector, MCPTool
from backend.mcp.discovery import MCPToolDiscovery
from backend.mcp.router import MCPToolRouter


class _FakeInstance:
    def __init__(self, name, tools, status=ConnectorStatus.CONNECTED):
        self.name = name
        self._tools = tools
        self.status = status
        self.tags = ["files"] if name == "filesystem" else ["github", "repo"]

    def list_tools(self):
        return self._tools


class _FakeRecord:
    def __init__(self, name, instance):
        self.name = name
        self.instance = instance


class _FakeRegistry:
    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records

    def get_record(self, name):
        for r in self._records:
            if r.name == name:
                return r
        return None


def _build_registry():
    fs_tools = [
        MCPTool(name="read_file", description="Read a text file's contents.", keywords=["read file", "open file"]),
        MCPTool(name="list_directory", description="List files and subdirectories.", keywords=["list files", "show files"]),
    ]
    gh_tools = [
        MCPTool(name="list_issues", description="List issues on a repository.", keywords=["list issues", "github issues"]),
        MCPTool(name="create_issue", description="Create a new issue.", keywords=["create issue", "file issue"]),
    ]
    fs_instance = _FakeInstance("filesystem", fs_tools)
    gh_instance = _FakeInstance("github", gh_tools)
    records = [_FakeRecord("filesystem", fs_instance), _FakeRecord("github", gh_instance)]
    return _FakeRegistry(records)


@pytest.fixture
def router():
    registry = _build_registry()
    discovery = MCPToolDiscovery(registry)
    return MCPToolRouter(discovery)


def test_explicit_connector_and_tool_bypasses_scoring(router):
    routed = router.route("this text is totally irrelevant", connector_hint="filesystem", tool_hint="read_file")
    assert routed is not None
    assert routed.connector == "filesystem"
    assert routed.tool_name == "read_file"
    assert routed.score == 999.0
    assert routed.reason == "explicit connector+tool"


def test_explicit_hint_with_unknown_tool_returns_none(router):
    routed = router.route("anything", connector_hint="filesystem", tool_hint="does_not_exist")
    assert routed is None


def test_keyword_pass_fires_correctly(router):
    routed = router.route("please read file notes.txt for me")
    assert routed is not None
    assert routed.connector == "filesystem"
    assert routed.tool_name == "read_file"
    assert routed.reason == "keyword match"


def test_connector_hint_without_tool_scores_within_connector(router):
    routed = router.route("please list issues on this repo", connector_hint="github")
    assert routed is not None
    assert routed.connector == "github"
    assert routed.tool_name == "list_issues"
    assert routed.reason == "explicit connector, scored tool"


def test_connector_hint_unknown_connector_returns_none(router):
    routed = router.route("anything", connector_hint="does-not-exist")
    assert routed is None


def test_no_match_below_min_score_returns_none(router):
    routed = router.route("completely unrelated gibberish about nothing at all", min_score=1.0)
    assert routed is None


def test_no_candidates_at_all_returns_none():
    empty_registry = _FakeRegistry([])
    discovery = MCPToolDiscovery(empty_registry)
    router = MCPToolRouter(discovery)
    assert router.route("read file notes.txt") is None
