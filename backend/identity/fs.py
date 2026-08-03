"""
On-disk management for Browser Profile Chrome user-data directories.

Design rule: this module never duplicates cookies/local storage/session
storage/extensions into the database. Chrome (via Playwright's
`launch_persistent_context`, see backend/browser/engine.py) already
persists all of that natively inside a profile's own directory -- that IS
the reuse mechanism the mission requires ("Every profile launches its own
Chrome profile. Reuse: Cookies / Sessions / Extensions / Login State").
This module only creates/deletes/clones those directories and reports a
lightweight, read-only summary of what's inside one, for the dashboard.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger("nexus.identity.fs")


class ProfileFilesystem:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def dir_for(self, profile_id: str) -> Path:
        return self.base_dir / profile_id

    def create(self, profile_id: str) -> str:
        path = self.dir_for(profile_id)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def delete(self, profile_id: str) -> None:
        path = self.dir_for(profile_id)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            logger.info("Deleted Chrome profile directory: %s", path)

    def clone(self, source_profile_id: str, target_profile_id: str) -> str:
        """
        Copies the full Chrome profile directory tree -- this is what makes
        a cloned profile carry over the source's cookies/local storage/
        session storage/extensions rather than starting logged out.
        """
        source = self.dir_for(source_profile_id)
        target = self.dir_for(target_profile_id)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if source.exists():
            shutil.copytree(source, target)
        else:
            target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def inspect(self, chrome_profile_dir: str) -> dict[str, Any]:
        """
        Best-effort, read-only summary of a Chrome profile directory's
        on-disk state, for the Profile Manager dashboard. Chrome nests the
        actual data a level down (the "Default" profile dir); this checks
        both that path and the root, since a directory that Chrome hasn't
        launched yet won't have "Default" populated.
        """
        root = Path(chrome_profile_dir)
        if not root.exists():
            return {
                "exists": False,
                "cookies": {"present": False},
                "local_storage": {"present": False},
                "session_storage": {"present": False},
                "extensions": [],
            }

        default_dir = root / "Default"
        search_root = default_dir if default_dir.exists() else root

        cookies_file = search_root / "Cookies"
        local_storage_dir = search_root / "Local Storage" / "leveldb"
        session_storage_dir = search_root / "Session Storage"
        extensions_dir = search_root / "Extensions"

        extensions: list[str] = []
        if extensions_dir.exists():
            try:
                extensions = sorted(p.name for p in extensions_dir.iterdir() if p.is_dir())
            except OSError:
                logger.debug("inspect: failed to list extensions dir %s", extensions_dir)

        return {
            "exists": True,
            "cookies": {
                "present": cookies_file.exists(),
                "size_bytes": cookies_file.stat().st_size if cookies_file.exists() else 0,
            },
            "local_storage": {"present": local_storage_dir.exists()},
            "session_storage": {"present": session_storage_dir.exists()},
            "extensions": extensions,
        }
