"""
Unit tests for the GitHub & Multi-Source Skill Learning System.
"""
import pytest
from backend.skills.providers.base import SourceContext, SourceFile, UrlKind
from backend.skills.providers.github_provider import (
    GitHubSkillProvider,
    _parse_github_url,
    _should_skip_dir,
    _should_skip_file,
    _detect_language,
)
from backend.skills.providers.registry import ProviderRegistry, get_registry
from backend.skills.extractor import SkillExtractor
from backend.database.models import SkillSource


def test_parse_github_url_repository():
    res = _parse_github_url("https://github.com/octocat/Hello-World")
    assert res is not None
    assert res["owner"] == "octocat"
    assert res["repo"] == "Hello-World"
    assert res["kind"] == UrlKind.REPOSITORY


def test_parse_github_url_with_git_suffix():
    res = _parse_github_url("https://github.com/owner/my-repo.git")
    assert res is not None
    assert res["owner"] == "owner"
    assert res["repo"] == "my-repo"


def test_parse_github_url_tree():
    res = _parse_github_url("https://github.com/owner/repo/tree/main/src/components")
    assert res is not None
    assert res["owner"] == "owner"
    assert res["repo"] == "repo"
    assert res["branch"] == "main"
    assert res["path"] == "src/components"
    assert res["kind"] == UrlKind.FOLDER


def test_parse_github_url_blob():
    res = _parse_github_url("https://github.com/owner/repo/blob/master/README.md")
    assert res is not None
    assert res["owner"] == "owner"
    assert res["repo"] == "repo"
    assert res["branch"] == "master"
    assert res["path"] == "README.md"
    assert res["kind"] == UrlKind.FILE


def test_parse_github_url_raw():
    res = _parse_github_url("https://raw.githubusercontent.com/owner/repo/main/script.py")
    assert res is not None
    assert res["owner"] == "owner"
    assert res["repo"] == "repo"
    assert res["path"] == "script.py"
    assert res["kind"] == UrlKind.RAW_FILE


def test_file_filtering_junk_dirs():
    assert _should_skip_dir("node_modules") is True
    assert _should_skip_dir(".git") is True
    assert _should_skip_dir("dist") is True
    assert _should_skip_dir("src") is False


def test_detect_language():
    assert _detect_language(".py") == "python"
    assert _detect_language(".ts") == "typescript"
    assert _detect_language(".js") == "javascript"
    assert _detect_language(".sol") == "solidity"
    assert _detect_language(".rs") == "rust"


def test_provider_registry():
    registry = ProviderRegistry()
    provider = GitHubSkillProvider()
    registry.register(provider)

    assert registry.can_handle("https://github.com/octocat/Hello-World") is True
    assert registry.can_handle("https://invalid.com/foo") is False

    p = registry.find_provider("https://github.com/octocat/Hello-World")
    assert p is provider


def test_global_registry_singleton():
    reg = get_registry()
    assert reg.can_handle("https://github.com/owner/repo") is True


@pytest.mark.asyncio
async def test_extractor_normalization():
    class DummyLLM:
        async def complete_json(self, system_prompt, user_prompt, task_type=None):
            return {
                "skills": [
                    {
                        "name": "Check Balance",
                        "description": "Checks the balance of an account",
                        "category": "api",
                        "trigger": "check balance\nget balance",
                        "tags": ["crypto", "web3"],
                        "language": "python",
                        "workflow": [
                            {"action": "execute", "target": "shell", "value": "python check.py", "description": "Run check script"}
                        ],
                        "variables": [{"name": "address", "description": "Wallet address", "default": ""}],
                        "confidence_score": 0.9,
                    }
                ]
            }

    extractor = SkillExtractor(llm=DummyLLM())
    ctx = SourceContext(
        url="https://github.com/test/repo",
        owner="test",
        repo="repo",
        primary_language="python",
        files=[SourceFile(relative_path="check.py", language="python", content="print('hello')")],
    )

    extracted = await extractor.extract(ctx)
    assert len(extracted) == 1
    skill = extracted[0]
    assert skill["name"] == "[test/repo] Check Balance"
    assert skill["category"] == "api"
    assert skill["confidence_score"] == 0.9
    assert skill["repository"] == "test/repo"
    assert len(skill["workflow"]) == 1


def test_skill_source_enum_has_github():
    assert SkillSource.GITHUB.value == "github"


@pytest.mark.asyncio
async def test_import_from_url_auto_detect_in_routes(monkeypatch):
    from backend.skills.library import SkillService

    class MockProvider:
        def provider_name(self):
            return "github"
        def can_handle(self, url):
            return "github.com" in url
        async def fetch(self, url):
            return SourceContext(
                url=url,
                owner="mockowner",
                repo="mockrepo",
                branch="main",
                commit_sha="1234567890abcdef",
                primary_language="python",
                files=[SourceFile(relative_path="main.py", language="python", content="print('hello world')")],
            )

    class MockExtractor:
        async def extract(self, ctx):
            return [
                {
                    "name": f"[{ctx.owner}/{ctx.repo}] Run Main Script",
                    "description": "Runs the main script",
                    "category": "workflow",
                    "trigger": "run main script",
                    "workflow": [{"action": "execute", "target": "shell", "value": "python main.py", "description": "Run script"}],
                    "confidence_score": 0.95,
                    "website_hint": ctx.url,
                }
            ]

    import backend.skills.providers.registry as reg_mod
    import backend.skills.extractor as ext_mod

    # Patch registry and extractor
    mock_reg = reg_mod.ProviderRegistry()
    mock_reg.register(MockProvider())
    monkeypatch.setattr(reg_mod, "get_registry", lambda: mock_reg)
    monkeypatch.setattr(ext_mod, "SkillExtractor", MockExtractor)

    service = SkillService()
    result = await service.import_from_url("https://github.com/mockowner/mockrepo")

    assert result["skills_created"] == 1
    assert result["repository"] == "mockowner/mockrepo"
    assert len(result["skills"]) == 1
    assert result["skills"][0]["name"] == "[mockowner/mockrepo] Run Main Script"
