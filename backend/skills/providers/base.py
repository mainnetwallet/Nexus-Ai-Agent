"""
Abstract base for all Skill Providers.

A provider knows how to take a URL or path, fetch the relevant source
material, and return a structured ``SourceContext`` that the Skill
Extractor can reason over.
"""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


class UrlKind(str, enum.Enum):
    """What flavour of URL we detected."""
    REPOSITORY = "repository"
    FOLDER = "folder"
    FILE = "file"
    RAW_FILE = "raw_file"
    UNKNOWN = "unknown"


@dataclass
class SourceFile:
    """One file from a provider, trimmed to its useful textual content."""
    relative_path: str          # e.g. "src/api.py"
    language: str               # e.g. "python", "javascript", "markdown"
    content: str                # full text (capped; see provider)
    size_bytes: int = 0


@dataclass
class SourceContext:
    """
    Everything the Extractor needs to know about a fetched source.

    Fields like ``primary_language``, ``dependencies``, ``architecture_summary``
    are filled in by the provider after scanning all files, so the Extractor
    doesn't have to re-derive them.
    """
    url: str                                     # original URL the user pasted
    url_kind: UrlKind = UrlKind.REPOSITORY
    owner: str = ""                               # e.g. "octocat"
    repo: str = ""                                # e.g. "Hello-World"
    branch: str = "main"
    subfolder: Optional[str] = None               # non-None for folder/file URLs
    commit_sha: Optional[str] = None              # HEAD sha at fetch time

    files: list[SourceFile] = field(default_factory=list)

    primary_language: str = "unknown"
    languages: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    readme_content: str = ""
    architecture_summary: str = ""                # short text built by the provider

    # Metadata for deduplication / continuous update
    content_hash: str = ""                        # SHA-256 of concatenated file contents


class BaseSkillProvider(ABC):
    """
    Interface every source provider must implement.

    Subclasses handle one family of URLs (GitHub, GitLab, local folder …)
    and produce a ``SourceContext`` the Extractor consumes.
    """

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return True if this provider knows how to fetch *url*."""
        ...

    @abstractmethod
    async def fetch(self, url: str) -> SourceContext:
        """
        Download / clone / read the source behind *url* and return a
        fully-populated ``SourceContext``.

        May raise ``ValueError`` for unsupported URL patterns or network
        errors.
        """
        ...

    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable label, e.g. ``'github'``."""
        ...
