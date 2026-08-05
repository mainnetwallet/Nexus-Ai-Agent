"""
Multi-source Skill Provider architecture.

Lets the agent learn skills from multiple external sources (GitHub repos,
GitLab, local folders, ZIP archives, etc.) through a pluggable provider
interface. Each provider knows how to fetch, parse, and present source
files from its own kind of URL/path.
"""
from backend.skills.providers.base import BaseSkillProvider, SourceContext, SourceFile
from backend.skills.providers.github_provider import GitHubSkillProvider
from backend.skills.providers.registry import ProviderRegistry, get_registry

__all__ = [
    "BaseSkillProvider",
    "SourceContext",
    "SourceFile",
    "GitHubSkillProvider",
    "ProviderRegistry",
    "get_registry",
]
