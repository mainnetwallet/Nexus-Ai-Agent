"""
Generic, website-agnostic browser engine built on Playwright.

This module never contains logic specific to any individual site. It exposes
primitives (navigate, smart_click, smart_type, extract_page, screenshot, ...)
that the planner (backend/planner) composes into a plan for whatever website
the user supplies at runtime.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    Page,
    Playwright,
    async_playwright,
)

from backend.config.settings import settings, SCREENSHOT_DIR

logger = logging.getLogger("nexus.browser")


@dataclass
class PageSnapshot:
    url: str
    title: str
    visible_text: str
    interactive_elements: list[dict[str, Any]]
    screenshot_path: str
    captured_at: float = field(default_factory=time.time)


class BrowserEngineError(RuntimeError):
    pass


class BrowserEngine:
    """
    Wraps a single Playwright browser + persistent context. One instance
    manages one logical "session" (which may contain multiple tabs/pages).
    """

    def __init__(
        self,
        headless: bool | None = None,
        user_data_dir: str | None = None,
        channel: str | None = None,
        slow_mo_ms: int | None = None,
    ) -> None:
        self._headless = settings.browser_headless if headless is None else headless
        self._user_data_dir = user_data_dir or settings.browser_user_data_dir
        self._channel = channel or settings.browser_channel.value
        self._slow_mo_ms = settings.browser_slow_mo_ms if slow_mo_ms is None else slow_mo_ms

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._pages: dict[str, Page] = {}
        self._active_page_id: Optional[str] = None
        self._downloads: list[Path] = []

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._playwright = await async_playwright().start()

        launch_kwargs: dict[str, Any] = {
            "headless": self._headless,
            "slow_mo": self._slow_mo_ms,
            "channel": self._channel if self._channel != "chromium" else None,
        }
        launch_kwargs = {k: v for k, v in launch_kwargs.items() if v is not None}

        if self._user_data_dir:
            # Persistent profile: browser + context are the same object.
            self._context = await self._playwright.chromium.launch_persistent_context(
                self._user_data_dir, **launch_kwargs
            )
            self._browser = self._context.browser
        else:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            self._context = await self._browser.new_context()

        self._context.on("page", self._on_new_page)
        page = await self._context.new_page()
        self._register_page(page)
        logger.info("Browser engine started (channel=%s headless=%s)", self._channel, self._headless)

    async def stop(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser engine stopped")

    def _register_page(self, page: Page) -> str:
        page_id = str(uuid.uuid4())[:8]
        self._pages[page_id] = page
        self._active_page_id = page_id
        page.on("download", self._on_download)
        return page_id

    def _on_new_page(self, page: Page) -> None:
        # Handles popups (e.g. wallet-connect popups) automatically.
        self._register_page(page)
        logger.info("New tab/popup detected: %s", page.url)

    def _on_download(self, download: Download) -> None:
        async def _save() -> None:
            target = SCREENSHOT_DIR.parent / "downloads" / download.suggested_filename
            target.parent.mkdir(parents=True, exist_ok=True)
            await download.save_as(str(target))
            self._downloads.append(target)
            logger.info("Download saved: %s", target)

        asyncio.create_task(_save())

    @property
    def page(self) -> Page:
        if not self._active_page_id or self._active_page_id not in self._pages:
            raise BrowserEngineError("No active page")
        return self._pages[self._active_page_id]

    def switch_tab(self, page_id: str) -> None:
        if page_id not in self._pages:
            raise BrowserEngineError(f"Unknown page id {page_id}")
        self._active_page_id = page_id

    def list_tabs(self) -> list[dict[str, str]]:
        return [{"id": pid, "url": p.url, "title": ""} for pid, p in self._pages.items()]

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        await self.page.goto(url, timeout=settings.browser_default_timeout_ms, wait_until=wait_until)
        await self._settle()

    async def go_back(self) -> None:
        await self.page.go_back()
        await self._settle()

    async def _settle(self, ms: int = 400) -> None:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception as exc:
            logger.debug("_settle: networkidle wait skipped (%s)", exc)
        await asyncio.sleep(ms / 1000)

    # ------------------------------------------------------------------ #
    # Smart primitives
    # ------------------------------------------------------------------ #
    async def smart_click(self, selector_or_text: str, exact: bool = False, timeout_ms: int | None = None) -> bool:
        """
        Attempts several strategies to click an element described by a CSS
        selector, role, or visible text -- in that order of preference.
        """
        timeout_ms = timeout_ms or settings.browser_default_timeout_ms
        strategies = [
            lambda: self.page.locator(selector_or_text),
            lambda: self.page.get_by_text(selector_or_text, exact=exact),
            lambda: self.page.get_by_role("button", name=selector_or_text, exact=exact),
            lambda: self.page.get_by_role("link", name=selector_or_text, exact=exact),
            lambda: self.page.get_by_label(selector_or_text, exact=exact),
        ]
        for build_locator in strategies:
            try:
                locator = build_locator().first
                await locator.wait_for(state="visible", timeout=timeout_ms // len(strategies))
                await locator.scroll_into_view_if_needed()
                await locator.click(timeout=timeout_ms // len(strategies))
                await self._settle()
                return True
            except Exception as exc:
                logger.debug("smart_click strategy failed for %r (%s)", selector_or_text, exc)
                continue
        logger.warning("smart_click failed for %r", selector_or_text)
        return False

    async def smart_type(self, selector_or_label: str, text: str, clear_first: bool = True) -> bool:
        strategies = [
            lambda: self.page.locator(selector_or_label),
            lambda: self.page.get_by_label(selector_or_label),
            lambda: self.page.get_by_placeholder(selector_or_label),
        ]
        for build_locator in strategies:
            try:
                locator = build_locator().first
                await locator.wait_for(state="visible", timeout=5_000)
                if clear_first:
                    await locator.fill("")
                await locator.fill(text)
                return True
            except Exception as exc:
                logger.debug("smart_type strategy failed for %r (%s)", selector_or_label, exc)
                continue
        logger.warning("smart_type failed for %r", selector_or_label)
        return False

    async def smart_wait(self, condition: str = "networkidle", timeout_ms: int = 10_000) -> None:
        if condition in ("load", "domcontentloaded", "networkidle"):
            await self.page.wait_for_load_state(condition, timeout=timeout_ms)
        else:
            await self.page.wait_for_timeout(timeout_ms)

    async def smart_scroll(self, direction: str = "down", amount_px: int = 800) -> None:
        delta = amount_px if direction == "down" else -amount_px
        await self.page.mouse.wheel(0, delta)
        await asyncio.sleep(0.2)

    async def upload_file(self, selector: str, file_path: str) -> bool:
        try:
            await self.page.locator(selector).set_input_files(file_path)
            return True
        except Exception:
            logger.warning("upload_file failed for selector=%s", selector)
            return False

    # ------------------------------------------------------------------ #
    # Perception
    # ------------------------------------------------------------------ #
    async def extract_interactive_elements(self, limit: int = 150) -> list[dict[str, Any]]:
        """
        Extracts a compact list of clickable/typeable elements with their
        visible text, role, and a stable selector -- fed to the planner LLM
        so it can decide what to do without ever seeing raw hardcoded HTML.
        """
        js = """
        () => {
            const out = [];
            const isVisible = (el) => {
                const r = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const selectors = 'a, button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [onclick]';
            const nodes = Array.from(document.querySelectorAll(selectors));
            for (const el of nodes) {
                if (!isVisible(el)) continue;
                const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.placeholder || '').trim().slice(0, 120);
                if (!text && el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') continue;
                out.push({
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    text: text,
                    type: el.getAttribute('type') || '',
                    name: el.getAttribute('name') || '',
                    id: el.id || '',
                });
                if (out.length >= %d) break;
            }
            return out;
        }
        """ % limit
        try:
            return await self.page.evaluate(js)
        except Exception:
            logger.exception("extract_interactive_elements failed")
            return []

    async def extract_visible_text(self, max_chars: int = 6000) -> str:
        try:
            text = await self.page.evaluate("() => document.body ? document.body.innerText : ''")
            return text[:max_chars]
        except Exception:
            return ""

    async def screenshot(self, name_hint: str = "step") -> str:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{name_hint}_{int(time.time() * 1000)}.png"
        await self.page.screenshot(path=str(path), full_page=False)
        return str(path)

    async def snapshot(self, name_hint: str = "snapshot") -> PageSnapshot:
        return PageSnapshot(
            url=self.page.url,
            title=await self.page.title(),
            visible_text=await self.extract_visible_text(),
            interactive_elements=await self.extract_interactive_elements(),
            screenshot_path=await self.screenshot(name_hint),
        )

    async def eval_js(self, expression: str, default: Any = None) -> Any:
        """
        Generic escape hatch to run a small JS expression/function in the
        active page and return its (JSON-serializable) result. Read-only use
        only -- this must never be used to read or exfiltrate anything from
        a wallet extension's storage; the injected `window.ethereum`
        provider only ever exposes what the dApp-facing API exposes (chain
        id, connected accounts' addresses), never keys or seed phrases.
        """
        try:
            return await self.page.evaluate(expression)
        except Exception:
            logger.debug("eval_js failed for expression=%r", expression[:120])
            return default

    async def get_injected_wallet_state(self) -> dict[str, Any]:
        """
        Reads the dApp-facing window.ethereum provider (what MetaMask/Rabby
        inject into every page) to report connection state. This is the same
        surface any website already has access to -- no elevated access, no
        key material.
        """
        js = """
        () => {
            const eth = window.ethereum;
            if (!eth) return { present: false };
            return {
                present: true,
                isConnected: (typeof eth.isConnected === 'function') ? eth.isConnected() : null,
                chainId: eth.chainId || null,
                selectedAddress: eth.selectedAddress || null,
                isMetaMask: !!eth.isMetaMask,
            };
        }
        """
        return await self.eval_js(js, default={"present": False})

    async def detect_popup_or_dialog(self, timeout_ms: int = 2_000) -> Optional[str]:
        """
        Best-effort detection of an unexpected extra tab (commonly a wallet
        connect popup) that appeared since the last check.
        """
        await asyncio.sleep(timeout_ms / 1000)
        if len(self._pages) > 1:
            newest_id = list(self._pages.keys())[-1]
            return newest_id
        return None
