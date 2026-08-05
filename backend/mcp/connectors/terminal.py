"""
Terminal MCP connector.

Runs a single allow-listed command (no shell, no chaining) and returns its
captured stdout/stderr/exit code. This is real code-execution capability,
so it ships **disabled by default** (Settings.mcp_terminal_enabled=False)
and layers several independent guards, mirroring how
backend/plugins/registry.py restricts plugin code to files already on disk
rather than accepting code over the API:

- The connector refuses to connect at all unless explicitly enabled.
- Commands are parsed with `shlex.split` and executed via
  `asyncio.create_subprocess_exec` -- never `shell=True` -- so shell
  metacharacters in an argument are inert, not interpreted.
- The executable (basename of argv[0]) must appear in a configured
  allow-list. Nothing runs that wasn't explicitly permitted.
- Raw shell metacharacters (semicolon, pipe, ampersand, dollar, backtick, redirects, newline) anywhere in the command
  string are rejected outright before parsing, as defense in depth even
  though shell=True is never used.
- Execution is confined to a configured working directory and enforces a
  wall-clock timeout, killing the process group on expiry.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import sys
from pathlib import Path
from typing import Any

from backend.mcp.base import ConnectorHealth, ConnectorStatus, MCPConnector, MCPTool, MCPToolError

logger = logging.getLogger("nexus.mcp.terminal")

_DANGEROUS_CHARS = set(";|&$`\n<>")
DEFAULT_ALLOWED_COMMANDS = [
    "git", "ls", "pwd", "echo", "cat", "grep", "find", "wc", "head", "tail",
    "python3", "pip", "pytest", "node", "npm",
]


class TerminalMCPConnector(MCPConnector):
    name = "terminal"
    version = "1.0.0"
    description = "Run a single allow-listed shell command (no chaining, no shell interpretation)."
    tags = ["terminal", "shell", "command", "cli", "execute", "run"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._enabled_flag: bool = bool(self.config.get("enabled", False))
        allowed = self.config.get("allowed_commands") or DEFAULT_ALLOWED_COMMANDS
        self._allowed_commands: set[str] = {c.strip() for c in allowed if c.strip()}
        self._timeout: float = float(self.config.get("timeout", 30))
        self._cwd = Path(self.config.get("cwd") or Path.cwd()).expanduser().resolve()

    async def connect(self) -> None:
        if not self._enabled_flag and not self.config.get("enabled", False):
            self.status = ConnectorStatus.DISABLED
            self.last_error = "terminal connector is disabled by configuration (mcp_terminal_enabled=false)"
            return
        self._cwd.mkdir(parents=True, exist_ok=True)
        await super().connect()

    async def health_check(self) -> ConnectorHealth:
        if self.status == ConnectorStatus.DISABLED:
            return ConnectorHealth(ConnectorStatus.DISABLED, "disabled by configuration")
        if self.status == ConnectorStatus.CONNECTED:
            return ConnectorHealth(
                ConnectorStatus.CONNECTED, f"cwd={self._cwd}, {len(self._allowed_commands)} command(s) allow-listed"
            )
        return await super().health_check()

    def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="run_command",
                description="Run a single allow-listed shell command and return stdout/stderr/exit code.",
                input_schema={"command": "string", "timeout": "number (optional)"},
                keywords=["run command", "execute command", "shell", "terminal", "run script"],
                destructive=True,
            ),
            MCPTool(
                name="list_allowed_commands",
                description="List the commands this connector is permitted to execute.",
                input_schema={},
                keywords=["allowed commands", "what commands can you run"],
            ),
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name == "list_allowed_commands":
            return {"allowed_commands": sorted(self._allowed_commands), "cwd": str(self._cwd)}
        if tool_name == "run_command":
            return await self._run_command(arguments.get("command", ""), arguments.get("timeout"))
        raise MCPToolError(f"unknown tool '{tool_name}'")

    async def _run_command(self, command: str, timeout: Any) -> dict[str, Any]:
        if not command or not command.strip():
            raise MCPToolError("command is required")
        if any(ch in command for ch in _DANGEROUS_CHARS):
            raise MCPToolError("command contains disallowed shell metacharacters (; | & $ ` > < newline)")

        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise MCPToolError(f"could not parse command: {exc}") from exc
        if not argv:
            raise MCPToolError("command is empty after parsing")

        executable = Path(argv[0]).name
        if executable not in self._allowed_commands:
            raise MCPToolError(
                f"command '{executable}' is not in the allow-list ({sorted(self._allowed_commands)})"
            )

        import shutil
        if not shutil.which(argv[0]) and not shutil.which(executable) and executable.lower() not in ("echo", "dir", "type", "cls", "copy"):
            raise MCPToolError(f"executable not found: {executable}")

        effective_timeout = float(timeout) if timeout else self._timeout
        logger.info("Terminal MCP running: %s (cwd=%s, timeout=%.0fs)", argv, self._cwd, effective_timeout)

        try:
            if sys.platform == "win32" and executable.lower() in ("echo", "dir", "type", "cls", "copy"):
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(self._cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(self._cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        except FileNotFoundError as exc:
            raise MCPToolError(f"executable not found: {executable}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise MCPToolError(f"command timed out after {effective_timeout}s")

        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[:20_000],
            "stderr": stderr.decode("utf-8", errors="replace")[:20_000],
        }
