"""
OCR fallback for canvas-heavy or screenshot-only pages.

The DOM extractor in backend/browser/engine.py sees nothing on pages that
render their UI to a <canvas> (games, some wallet widgets, PDF viewers,
certain captchas) or where text is baked into images. This module runs
Tesseract OCR over the page screenshot so the planner still gets *something*
readable, instead of an empty interactive-elements list.

Generic by design: no site-specific logic, just "read the pixels".
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config.settings import settings

logger = logging.getLogger("nexus.vision.ocr")


@dataclass
class OCRResult:
    text: str
    word_boxes: list[dict[str, Any]] = field(default_factory=list)
    available: bool = True
    error: str = ""


class OCRUnavailableError(RuntimeError):
    """Raised only if the caller explicitly requests strict mode."""


class OCREngine:
    """
    Thin async wrapper around pytesseract. Tesseract's binary is a system
    dependency (not a Python package), so this degrades gracefully -- if
    it's missing, `extract_text` returns an empty OCRResult with
    `available=False` instead of crashing the agent loop.
    """

    def __init__(self, lang: str | None = None) -> None:
        self.lang = lang or settings.ocr_lang
        self._checked = False
        self._usable = False

    def _ensure_ready(self) -> bool:
        if self._checked:
            return self._usable
        self._checked = True
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401

            pytesseract.get_tesseract_version()
            self._usable = True
        except Exception as exc:  # pytesseract raises its own error types + ImportError
            logger.warning("OCR unavailable (tesseract/pytesseract not installed): %s", exc)
            self._usable = False
        return self._usable

    async def extract_text(self, image_path: str, max_chars: int | None = None) -> OCRResult:
        if not settings.ocr_enabled:
            return OCRResult(text="", available=False, error="OCR disabled in settings")
        if not Path(image_path).exists():
            return OCRResult(text="", available=False, error=f"screenshot not found: {image_path}")

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._extract_sync, image_path, max_chars or settings.ocr_max_chars)
        except Exception:
            logger.exception("OCR extraction failed for %s", image_path)
            return OCRResult(text="", available=False, error="OCR extraction raised an exception")

    def _extract_sync(self, image_path: str, max_chars: int) -> OCRResult:
        if not self._ensure_ready():
            return OCRResult(text="", available=False, error="tesseract binary not found on PATH")

        import pytesseract
        from PIL import Image

        with Image.open(image_path) as img:
            text = pytesseract.image_to_string(img, lang=self.lang)
            boxes: list[dict[str, Any]] = []
            try:
                data = pytesseract.image_to_data(img, lang=self.lang, output_type=pytesseract.Output.DICT)
                for i, word in enumerate(data.get("text", [])):
                    word = word.strip()
                    if not word:
                        continue
                    conf_raw = data.get("conf", ["-1"])[i]
                    try:
                        conf = float(conf_raw)
                    except (TypeError, ValueError):
                        conf = -1.0
                    if conf < 40:
                        continue
                    boxes.append(
                        {
                            "text": word,
                            "x": data["left"][i],
                            "y": data["top"][i],
                            "width": data["width"][i],
                            "height": data["height"][i],
                            "confidence": conf,
                        }
                    )
            except Exception:
                logger.debug("image_to_data (word boxes) failed; text-only OCR result returned", exc_info=True)

        return OCRResult(text=text.strip()[:max_chars], word_boxes=boxes[:200], available=True)
