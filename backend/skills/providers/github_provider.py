"""
GitHub Skill Provider.

Handles all flavors of GitHub URL:
  - https://github.com/owner/repo
  - https://github.com/owner/repo/tree/branch/path
  - https://github.com/owner/repo/blob/branch/path/file.ext
  - https://raw.githubusercontent.com/owner/repo/branch/path/file.ext

Clones (shallow, depth 1) to a temporary directory, scans recursively,
filters out junk, detects the project language, reads useful files, and
returns a SourceContext the Extractor can consume.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from backend.skills.providers.base import (
    BaseSkillProvider,
    SourceContext,
    SourceFile,
    UrlKind,
)

logger = logging.getLogger("nexus.skills.github_provider")

# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #
# Directories to always skip when scanning a cloned repo.
_SKIP_DIRS: set[str] = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "out", "coverage", ".cache",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "vendor",
    "target", ".idea", ".vscode", ".DS_Store", "eggs", "*.egg-info",
    ".gradle", ".mvn", "bower_components",
}

# File extensions to skip (binary / non-useful).
_SKIP_EXTENSIONS: set[str] = {
    ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".lib",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".bmp", ".webp",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm", ".flv",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pyc", ".pyo", ".class", ".jar",
    ".db", ".sqlite", ".sqlite3",
    ".lock",  # package-lock.json etc. are huge and not useful
    ".min.js", ".min.css",  # minified assets
    ".map",  # source maps
}

# Extensions we *do* want to read.
_USEFUL_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".rb", ".java", ".kt", ".scala", ".cs", ".cpp", ".c", ".h",
    ".php", ".lua", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".md", ".mdx", ".rst", ".txt", ".adoc",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".env.example",
    ".html", ".css", ".scss", ".less",
    ".sol",  # Solidity
    ".dockerfile", ".tf", ".hcl",
    ".graphql", ".gql", ".proto",
    ".sql",
    ".r", ".R",
}

# Max file size we'll read (256 KB).  Larger files are almost always
# generated / vendored and not useful for skill extraction.
_MAX_FILE_BYTES = 256 * 1024

# Max total characters across all files sent to the extractor.
_MAX_TOTAL_CHARS = 500_000

# Language detection: extension -> language name
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".cs": "csharp", ".cpp": "cpp", ".c": "c", ".h": "c",
    ".php": "php", ".lua": "lua",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell",
    ".md": "markdown", ".mdx": "markdown", ".rst": "restructuredtext",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini",
    ".html": "html", ".css": "css", ".scss": "scss",
    ".sol": "solidity",
    ".sql": "sql",
    ".dockerfile": "dockerfile",
    ".tf": "terraform",
    ".graphql": "graphql", ".proto": "protobuf",
    ".r": "r", ".R": "r",
}

# Dependency file -> language hint
_DEP_FILES: dict[str, str] = {
    "requirements.txt": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "pyproject.toml": "python",
    "Pipfile": "python",
    "package.json": "javascript",
    "yarn.lock": "javascript",
    "pnpm-lock.yaml": "javascript",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "Gemfile": "ruby",
    "build.gradle": "java",
    "pom.xml": "java",
    "composer.json": "php",
    "mix.exs": "elixir",
}

# ------------------------------------------------------------------ #
# URL Parsing
# ------------------------------------------------------------------ #
# Standard GitHub URL patterns
_REPO_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)
_TREE_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/tree/(?P<branch>[^/]+)(?:/(?P<path>.+))?$"
)
_BLOB_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/blob/(?P<branch>[^/]+)/(?P<path>.+)$"
)
_RAW_RE = re.compile(
    r"^https?://raw\.githubusercontent\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<branch>[^/]+)/(?P<path>.+)$"
)


def _parse_github_url(url: str) -> Optional[dict]:
    """
    Returns a dict with keys: owner, repo, branch, path, kind
    or None if the URL isn't a recognized GitHub URL.
    """
    url = url.strip()

    m = _REPO_RE.match(url)
    if m:
        return {**m.groupdict(), "branch": "HEAD", "path": None, "kind": UrlKind.REPOSITORY}

    m = _TREE_RE.match(url)
    if m:
        return {**m.groupdict(), "kind": UrlKind.FOLDER}

    m = _BLOB_RE.match(url)
    if m:
        return {**m.groupdict(), "kind": UrlKind.FILE}

    m = _RAW_RE.match(url)
    if m:
        return {**m.groupdict(), "kind": UrlKind.RAW_FILE}

    return None


# ------------------------------------------------------------------ #
# File scanning helpers
# ------------------------------------------------------------------ #
def _should_skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.startswith(".")


def _should_skip_file(path: Path) -> bool:
    name = path.name
    suffix = path.suffix.lower()

    # Skip hidden files
    if name.startswith(".") and name not in {".env.example", ".eslintrc", ".prettierrc"}:
        return True

    # Skip known binary / useless extensions
    if suffix in _SKIP_EXTENSIONS:
        return True

    # Skip files with no extension that are very large (likely binaries)
    if not suffix and path.stat().st_size > 50_000:
        return True

    return False


def _detect_language(suffix: str) -> str:
    return _EXT_TO_LANG.get(suffix.lower(), "text")


def _scan_directory(root: Path, subfolder: Optional[str] = None) -> list[SourceFile]:
    """
    Recursively scan *root* (optionally limited to *subfolder*), returning
    a list of SourceFile objects for every useful file found.
    """
    scan_root = root / subfolder if subfolder else root
    if not scan_root.is_dir():
        return []

    files: list[SourceFile] = []
    total_chars = 0

    for dirpath, dirnames, filenames in os.walk(scan_root):
        # Prune skipped directories in-place so os.walk doesn't descend.
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if _should_skip_file(fpath):
                continue

            suffix = fpath.suffix.lower()
            # Only read files we recognise or small text files
            if suffix not in _USEFUL_EXTENSIONS and fpath.stat().st_size > 10_000:
                continue

            size = fpath.stat().st_size
            if size > _MAX_FILE_BYTES:
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if total_chars + len(content) > _MAX_TOTAL_CHARS:
                # Truncate this file to fit
                remaining = _MAX_TOTAL_CHARS - total_chars
                if remaining < 200:
                    break
                content = content[:remaining] + "\n... (truncated)"

            rel = str(fpath.relative_to(root))
            files.append(SourceFile(
                relative_path=rel.replace("\\", "/"),
                language=_detect_language(suffix),
                content=content,
                size_bytes=size,
            ))
            total_chars += len(content)

            if total_chars >= _MAX_TOTAL_CHARS:
                break

        if total_chars >= _MAX_TOTAL_CHARS:
            break

    return files


def _detect_primary_language(files: list[SourceFile]) -> tuple[str, list[str]]:
    """Count code file extensions and return (primary, [all]) languages."""
    counts: dict[str, int] = {}
    for f in files:
        lang = f.language
        if lang in ("markdown", "text", "json", "yaml", "toml", "ini"):
            continue  # config/doc files don't count for primary language
        counts[lang] = counts.get(lang, 0) + f.size_bytes

    if not counts:
        return "unknown", []

    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return ranked[0][0], [lang for lang, _ in ranked]


def _extract_dependencies(root: Path) -> list[str]:
    """Pull dependency names from common manifest files."""
    deps: list[str] = []

    # Python: requirements.txt
    req = root / "requirements.txt"
    if req.is_file():
        for line in req.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                name = re.split(r"[><=!~\[]", line)[0].strip()
                if name:
                    deps.append(name)

    # Node: package.json
    pkg = root / "package.json"
    if pkg.is_file():
        import json
        try:
            data = json.loads(pkg.read_text(errors="replace"))
            for key in ("dependencies", "devDependencies"):
                if key in data and isinstance(data[key], dict):
                    deps.extend(data[key].keys())
        except Exception:
            pass

    # Rust: Cargo.toml
    cargo = root / "Cargo.toml"
    if cargo.is_file():
        for line in cargo.read_text(errors="replace").splitlines():
            m = re.match(r'^(\w[\w-]*)\s*=', line)
            if m:
                deps.append(m.group(1))

    # Go: go.mod
    gomod = root / "go.mod"
    if gomod.is_file():
        for line in gomod.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("module") and not line.startswith("go ") and not line.startswith("//"):
                parts = line.split()
                if parts:
                    deps.append(parts[0])

    return deps[:100]  # cap at 100


def _read_readme(root: Path) -> str:
    """Find and read the README file."""
    for name in ("README.md", "readme.md", "Readme.md", "README.rst", "README.txt", "README"):
        p = root / name
        if p.is_file():
            content = p.read_text(encoding="utf-8", errors="replace")
            return content[:20_000]  # cap at 20k chars
    return ""


def _build_architecture_summary(files: list[SourceFile]) -> str:
    """Build a quick directory-tree style summary of the repo structure."""
    dirs: set[str] = set()
    for f in files:
        parts = f.relative_path.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]) + "/")

    tree_lines: list[str] = sorted(dirs)[:80]  # cap
    file_lines = [f.relative_path for f in files[:80]]

    summary_parts: list[str] = []
    if tree_lines:
        summary_parts.append("Directories:\n" + "\n".join(f"  {d}" for d in tree_lines[:40]))
    if file_lines:
        summary_parts.append(f"Files ({len(files)} total, showing first 80):\n" + "\n".join(f"  {f}" for f in file_lines))

    return "\n\n".join(summary_parts)


def _content_hash(files: list[SourceFile]) -> str:
    """SHA-256 hash of all file contents for deduplication."""
    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: x.relative_path):
        h.update(f.relative_path.encode())
        h.update(f.content.encode())
    return h.hexdigest()


# ------------------------------------------------------------------ #
# GitHubSkillProvider
# ------------------------------------------------------------------ #
class GitHubSkillProvider(BaseSkillProvider):
    """
    Fetches and parses any public GitHub repository, folder, or file URL
    into a SourceContext the Skill Extractor can consume.
    """

    def provider_name(self) -> str:
        return "github"

    def can_handle(self, url: str) -> bool:
        return _parse_github_url(url) is not None

    async def fetch(self, url: str) -> SourceContext:
        parsed = _parse_github_url(url)
        if parsed is None:
            raise ValueError(f"Not a recognized GitHub URL: {url}")

        owner = parsed["owner"]
        repo = parsed["repo"]
        branch = parsed["branch"]
        path = parsed.get("path")
        kind: UrlKind = parsed["kind"]

        logger.info("GitHub fetch: owner=%s repo=%s branch=%s kind=%s path=%s",
                     owner, repo, branch, kind.value, path)

        # Clone the repo into a temp directory
        clone_url = f"https://github.com/{owner}/{repo}.git"
        tmp_dir = tempfile.mkdtemp(prefix="nexus_gh_")

        try:
            # Shallow clone
            clone_args = ["git", "clone", "--depth", "1"]
            if branch != "HEAD":
                clone_args.extend(["--branch", branch])
            clone_args.extend([clone_url, tmp_dir])

            proc = await asyncio.create_subprocess_exec(
                *clone_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip()
                raise ValueError(f"git clone failed for {clone_url}: {err_msg}")

            # Get commit SHA
            sha_proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                cwd=tmp_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            sha_out, _ = await sha_proc.communicate()
            commit_sha = sha_out.decode().strip() if sha_proc.returncode == 0 else None

            root = Path(tmp_dir)

            # Determine subfolder for folder/file URLs
            subfolder = path if kind in (UrlKind.FOLDER,) else None

            # If it's a single file URL, only read that file
            if kind in (UrlKind.FILE, UrlKind.RAW_FILE) and path:
                target = root / path
                if target.is_file():
                    try:
                        content = target.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_BYTES]
                    except Exception:
                        content = ""
                    files = [SourceFile(
                        relative_path=path,
                        language=_detect_language(target.suffix.lower()),
                        content=content,
                        size_bytes=target.stat().st_size,
                    )]
                else:
                    files = []
            else:
                files = _scan_directory(root, subfolder)

            primary_lang, all_langs = _detect_primary_language(files)
            deps = _extract_dependencies(root)
            readme = _read_readme(root)
            arch_summary = _build_architecture_summary(files)
            c_hash = _content_hash(files)

            logger.info("GitHub fetch complete: %d files, primary=%s, SHA=%s",
                        len(files), primary_lang, commit_sha)

            return SourceContext(
                url=url,
                url_kind=kind,
                owner=owner,
                repo=repo,
                branch=branch if branch != "HEAD" else "main",
                subfolder=subfolder,
                commit_sha=commit_sha,
                files=files,
                primary_language=primary_lang,
                languages=all_langs,
                dependencies=deps,
                readme_content=readme,
                architecture_summary=arch_summary,
                content_hash=c_hash,
            )
        finally:
            # Cleanup temp directory
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
