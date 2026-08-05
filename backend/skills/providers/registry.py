"""
Provider Registry.

Auto-routes any URL to the first provider that can handle it.
New providers (GitLab, Bitbucket, local folder, ZIP, etc.) only need
to be registered here -- all upstream code calls ``get_registry()``
and then ``registry.fetch(url)``.
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.skills.providers.base import BaseSkillProvider, SourceContext
from backend.skills.providers.github_provider import GitHubSkillProvider

logger = logging.getLogger("nexus.skills.provider_registry")


class ProviderRegistry:
    """
    Holds all registered ``BaseSkillProvider`` instances and delegates
    ``fetch()`` to whichever one claims the URL.
    """

    def __init__(self) -> None:
        self._providers: list[BaseSkillProvider] = []

    def register(self, provider: BaseSkillProvider) -> None:
        self._providers.append(provider)
        logger.debug("Registered skill provider: %s", provider.provider_name())

    def find_provider(self, url: str) -> Optional[BaseSkillProvider]:
        for p in self._providers:
            if p.can_handle(url):
                return p
        return None

    def can_handle(self, url: str) -> bool:
        return self.find_provider(url) is not None

    async def fetch(self, url: str) -> SourceContext:
        provider = self.find_provider(url)
        if provider is None:
            raise ValueError(
                f"No skill provider can handle URL: {url}. "
                f"Registered providers: {[p.provider_name() for p in self._providers]}"
            )
        logger.info("Routing URL to provider '%s': %s", provider.provider_name(), url)
        return await provider.fetch(url)


# ------------------------------------------------------------------ #
# Singleton
# ------------------------------------------------------------------ #
_registry: Optional[ProviderRegistry] = None


def get_registry() -> ProviderRegistry:
    """Return the global provider registry, creating it on first call."""
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
        # Register built-in providers
        _registry.register(GitHubSkillProvider())
        logger.info("Provider registry initialized with: GitHub")
    return _registry
