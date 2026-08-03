"""
Filesystem MCP connector.

Exposes read/write/search access to the local disk, sandboxed to a
configured list of allowed root directories -- every path argument is
resolved and checked against those roots before any I/O happens, the same
"never trust a path, always resolve+contain it" discipline used elsewhere
in this codebase (e.g. ConfigManager.restore()'s backup-dir containment
check). Default root is the project's own BASE_DIR unless overridden via
Settings.mcp_filesystem_roots or MCPManager.configure("filesystem", ...).
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
from pathlib import Path
from typing import Any

from backend.mcp.base import ConnectorHealth, ConnectorStatus, MCPConnector, MCPTool, MCPToolError

logger = logging.getLogger("nexus.mcp.filesystem")

DEFAULT_MAX_READ_BYTES = 500_000
DEFAULT_MAX_SEARCH_RESULTS = 200


class FilesystemMCPConnector(MCPConnector):
    name = "filesystem"
    version = "1.0.0"
    description = "Read, write, and search files within sandboxed root directories."
    tags = ["files", "filesystem", "disk", "directory", "read", "write"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        roots = self.config.get("roots") or [str(Path.cwd())]
        self._roots: list[Path] = [Path(r).expanduser().resolve() for r in roots]

    async def connect(self) -> None:
        for root in self._roots:
            root.mkdir(parents=True, exist_ok=True)
        await super().connect()

    async def health_check(self) -> ConnectorHealth:
        if self.status != ConnectorStatus.CONNECTED:
            return await super().health_check()
        ok_roots = [str(r) for r in self._roots if r.exists()]
        if not ok_roots:
            return ConnectorHealth(ConnectorStatus.ERROR, "no configured root directories exist")
        return ConnectorHealth(ConnectorStatus.CONNECTED, f"{len(ok_roots)} root(s) accessible: {ok_roots}")

    def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="list_directory",
                description="List files and subdirectories at a path.",
                input_schema={"path": "string", "recursive": "boolean (optional, default false)"},
                keywords=["list files", "list directory", "show files", "what's in", "directory contents"],
            ),
            MCPTool(
                name="read_file",
                description="Read a text file's contents.",
                input_schema={"path": "string", "max_bytes": "integer (optional)"},
                keywords=["read file", "open file", "show contents", "cat file", "file contents"],
            ),
            MCPTool(
                name="write_file",
                description="Write (or append to) a text file.",
                input_schema={"path": "string", "content": "string", "append": "boolean (optional)"},
                keywords=["write file", "save file", "create file", "append to file"],
                destructive=True,
            ),
            MCPTool(
                name="edit_file",
                description=(
                    "Find-and-replace a piece of text within an existing file, without rewriting the "
                    "whole file. old_text must match exactly (including whitespace) and, by default, "
                    "must appear exactly once -- pass expected_occurrences to allow/require a different "
                    "count. Safer than write_file for changing part of a large or important file."
                ),
                input_schema={
                    "path": "string",
                    "old_text": "string",
                    "new_text": "string",
                    "expected_occurrences": "integer (optional, default 1)",
                },
                keywords=["edit file", "replace text", "find and replace", "modify file", "change text in file"],
                destructive=True,
            ),
            MCPTool(
                name="delete_file",
                description="Delete a file.",
                input_schema={"path": "string"},
                keywords=["delete file", "remove file"],
                destructive=True,
            ),
            MCPTool(
                name="search_files",
                description="Search for files by glob pattern under a root.",
                input_schema={"root": "string (optional)", "pattern": "string", "max_results": "integer (optional)"},
                keywords=["find file", "search files", "locate file", "glob"],
            ),
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name == "list_directory":
            return await asyncio.to_thread(self._list_directory, arguments.get("path", "."), bool(arguments.get("recursive", False)))
        if tool_name == "read_file":
            return await asyncio.to_thread(self._read_file, arguments["path"], int(arguments.get("max_bytes", DEFAULT_MAX_READ_BYTES)))
        if tool_name == "write_file":
            return await asyncio.to_thread(
                self._write_file, arguments["path"], arguments.get("content", ""), bool(arguments.get("append", False))
            )
        if tool_name == "edit_file":
            return await asyncio.to_thread(
                self._edit_file,
                arguments["path"],
                arguments.get("old_text", ""),
                arguments.get("new_text", ""),
                arguments.get("expected_occurrences", 1),
            )
        if tool_name == "delete_file":
            return await asyncio.to_thread(self._delete_file, arguments["path"])
        if tool_name == "search_files":
            return await asyncio.to_thread(
                self._search_files,
                arguments.get("root", "."),
                arguments.get("pattern", "*"),
                int(arguments.get("max_results", DEFAULT_MAX_SEARCH_RESULTS)),
            )
        raise MCPToolError(f"unknown tool '{tool_name}'")

    # ---- Sandbox -----------------------------------------------------
    def _resolve(self, raw_path: str) -> Path:
        if not raw_path:
            raise MCPToolError("path is required")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            # Relative paths resolve against the first configured root.
            candidate = self._roots[0] / candidate
        resolved = candidate.resolve()
        for root in self._roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        raise MCPToolError(f"path '{raw_path}' is outside the allowed roots {[str(r) for r in self._roots]}")

    # ---- Operations -----------------------------------------------------
    def _list_directory(self, raw_path: str, recursive: bool) -> dict[str, Any]:
        target = self._resolve(raw_path)
        if not target.exists():
            raise MCPToolError(f"path does not exist: {target}")
        if not target.is_dir():
            raise MCPToolError(f"not a directory: {target}")
        entries = []
        iterator = target.rglob("*") if recursive else target.iterdir()
        for child in sorted(iterator):
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": str(child.relative_to(target)) if recursive else child.name,
                    "type": "directory" if child.is_dir() else "file",
                    "size": stat.st_size,
                }
            )
        return {"path": str(target), "entries": entries}

    def _read_file(self, raw_path: str, max_bytes: int) -> dict[str, Any]:
        target = self._resolve(raw_path)
        if not target.exists() or not target.is_file():
            raise MCPToolError(f"file does not exist: {target}")
        data = target.read_bytes()[: max(0, max_bytes)]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"path": str(target), "binary": True, "size": target.stat().st_size}
        return {"path": str(target), "content": text, "truncated": target.stat().st_size > max_bytes}

    def _write_file(self, raw_path: str, content: str, append: bool) -> dict[str, Any]:
        target = self._resolve(raw_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as fh:
            fh.write(content)
        return {"path": str(target), "bytes_written": len(content.encode("utf-8")), "append": append}

    def _edit_file(self, raw_path: str, old_text: str, new_text: str, expected_occurrences: Any) -> dict[str, Any]:
        target = self._resolve(raw_path)
        if not target.exists() or not target.is_file():
            raise MCPToolError(f"file does not exist: {target}")
        if not old_text:
            raise MCPToolError("old_text is required and cannot be empty")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise MCPToolError(f"file is not valid UTF-8 text, cannot edit: {target}") from exc

        count = content.count(old_text)
        if count == 0:
            raise MCPToolError(f"old_text not found in {target}")
        expected = 1 if expected_occurrences in (None, "") else int(expected_occurrences)
        if count != expected:
            raise MCPToolError(
                f"old_text appears {count} time(s) in {target}, expected {expected} -- "
                "make old_text more specific (include surrounding context) or pass the actual "
                "expected_occurrences if multiple replacements are intentional"
            )

        new_content = content.replace(old_text, new_text)
        target.write_text(new_content, encoding="utf-8")
        return {
            "path": str(target),
            "occurrences_replaced": count,
            "bytes_written": len(new_content.encode("utf-8")),
        }

    def _delete_file(self, raw_path: str) -> dict[str, Any]:
        target = self._resolve(raw_path)
        if not target.exists():
            raise MCPToolError(f"file does not exist: {target}")
        if target.is_dir():
            raise MCPToolError("delete_file cannot delete a directory")
        target.unlink()
        return {"path": str(target), "deleted": True}

    def _search_files(self, raw_root: str, pattern: str, max_results: int) -> dict[str, Any]:
        root = self._resolve(raw_root)
        if not root.exists():
            raise MCPToolError(f"root does not exist: {root}")
        matches = []
        for child in root.rglob("*"):
            if child.is_file() and fnmatch.fnmatch(child.name, pattern):
                matches.append(str(child))
                if len(matches) >= max_results:
                    break
        return {"root": str(root), "pattern": pattern, "matches": matches}
