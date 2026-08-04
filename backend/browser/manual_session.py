"""
Manual "Open in Chrome" session.

The agent drives profiles through Playwright's `launch_persistent_context`
(see BrowserEngine, backend/browser/engine.py). This module is the other
side of the same coin: it launches the *real*, separately-installed Chrome
binary as an ordinary, detached OS process pointed at that exact profile's
`chrome_profile_dir`, so a person can open it up and click around manually
-- log in, check something, poke at a page -- exactly like using any other
Chrome profile on their machine.

Because it's a real `--user-data-dir`, all of the isolation Chrome already
guarantees between profiles applies here too: cookies/local storage/session
storage/extensions are read from and written back to that profile's own
directory, and nothing here touches any other profile.

This process is fully independent of the backend: it isn't a Playwright
object we own, so restarting/crashing the backend doesn't close the window,
and closing the window doesn't affect the backend. The one thing that *does*
conflict is Chrome's own profile lock -- a profile directory can only be
open in one Chrome process at a time, whether that's this manual session or
the agent's own Playwright-driven BrowserEngine. Route-level code (see
routes_profiles.py) checks whether the agent currently has this exact
profile loaded before calling into here.
"""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("nexus.browser.manual_session")


class ManualChromeSessionError(RuntimeError):
    pass


# Common install locations per OS, used only if the executable isn't found
# on PATH. `browser_executable_path` in settings always wins over both.
_CANDIDATE_PATHS: dict[str, list[str]] = {
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ],
    "Linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ],
}

_PATH_NAMES = ("google-chrome", "google-chrome-stable", "chrome", "chromium-browser", "chromium")


def resolve_chrome_executable(override: Optional[str] = None) -> str:
    """Finds a real Chrome/Chromium binary on this machine: `override` (e.g.
    settings.browser_executable_path) wins if set, then PATH, then a short
    list of the usual per-OS install locations."""
    if override:
        if not Path(override).exists():
            raise ManualChromeSessionError(f"Configured browser_executable_path does not exist: {override}")
        return override

    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found

    for path in _CANDIDATE_PATHS.get(platform.system(), []):
        if Path(path).exists():
            return path

    raise ManualChromeSessionError(
        "Couldn't find a Chrome/Chromium executable on this machine. "
        "Set browser_executable_path (BROWSER_EXECUTABLE_PATH) to its full path."
    )


def open_profile_in_chrome(chrome_profile_dir: str, executable_override: Optional[str] = None) -> int:
    """Launches a real, detached Chrome process against this profile's
    user-data-dir and returns its PID. Safe to call repeatedly: if Chrome is
    already open on this exact directory, Chrome's own SingletonLock just
    focuses the existing window instead of erroring."""
    exe = resolve_chrome_executable(executable_override)
    Path(chrome_profile_dir).mkdir(parents=True, exist_ok=True)

    args = [exe, f"--user-data-dir={chrome_profile_dir}", "--no-first-run", "--no-default-browser-check"]
    popen_kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if platform.system() == "Windows":
        popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(args, **popen_kwargs)
    except OSError as exc:
        raise ManualChromeSessionError(f"Failed to launch Chrome: {exc}") from exc

    logger.info("Opened Chrome profile manually: dir=%s pid=%s", chrome_profile_dir, proc.pid)
    return proc.pid
