"""
GitHub Integration: version / commit / build info.

Reads local git metadata (no network call, no token needed) to answer
"what build is this" for the dashboard, /diagnostics, and the Telegram
bot. Falls back gracefully to "unknown" fields when the process isn't
running from a git checkout (e.g. a built Docker image without .git),
so this never raises.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache

from backend.config.settings import BASE_DIR


@dataclass
class BuildInfo:
    version: str
    commit: str
    commit_short: str
    branch: str
    commit_date: str
    dirty: bool
    repo: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "commit": self.commit,
            "commit_short": self.commit_short,
            "branch": self.branch,
            "commit_date": self.commit_date,
            "dirty": self.dirty,
            "repo": self.repo,
        }


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=BASE_DIR, capture_output=True, text=True, timeout=5, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


@lru_cache(maxsize=1)
def get_build_info() -> BuildInfo:
    commit = _git("rev-parse", "HEAD") or "unknown"
    commit_short = commit[:7] if commit != "unknown" else "unknown"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    commit_date = _git("log", "-1", "--format=%cI") or "unknown"
    status = _git("status", "--porcelain")
    dirty = bool(status)

    remote = _git("config", "--get", "remote.origin.url") or "unknown"
    repo = remote.rstrip("/").removesuffix(".git")
    if repo.startswith("git@github.com:"):
        repo = "https://github.com/" + repo.split("git@github.com:", 1)[1]

    # "version" is the nearest tag if the repo has one, else the short commit.
    version = _git("describe", "--tags", "--always") or commit_short

    return BuildInfo(
        version=version,
        commit=commit,
        commit_short=commit_short,
        branch=branch,
        commit_date=commit_date,
        dirty=dirty,
        repo=repo,
    )


def refresh_build_info() -> BuildInfo:
    """Bypasses the cache -- use after a deploy/redeploy within the same process."""
    get_build_info.cache_clear()
    return get_build_info()
