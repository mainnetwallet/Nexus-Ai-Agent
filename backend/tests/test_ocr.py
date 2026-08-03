import sys
import types

import pytest

from backend.vision.ocr import OCREngine


def _install_fake_pytesseract(monkeypatch, text="Hello World", raise_on_version=False):
    fake_module = types.ModuleType("pytesseract")

    class FakeOutput:
        DICT = "dict"

    def get_tesseract_version():
        if raise_on_version:
            raise EnvironmentError("tesseract is not installed or it's not in your PATH")
        return "5.3.0"

    def image_to_string(img, lang="eng"):
        return text

    def image_to_data(img, lang="eng", output_type=None):
        return {
            "text": text.split(),
            "left": [0] * len(text.split()),
            "top": [0] * len(text.split()),
            "width": [10] * len(text.split()),
            "height": [10] * len(text.split()),
            "conf": ["95"] * len(text.split()),
        }

    fake_module.get_tesseract_version = get_tesseract_version
    fake_module.image_to_string = image_to_string
    fake_module.image_to_data = image_to_data
    fake_module.Output = FakeOutput
    monkeypatch.setitem(sys.modules, "pytesseract", fake_module)


def _install_fake_pil(monkeypatch):
    fake_pil = types.ModuleType("PIL")
    fake_image_module = types.ModuleType("PIL.Image")

    class FakeImage:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open(path):
        return FakeImage()

    fake_image_module.open = open
    fake_pil.Image = fake_image_module
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_image_module)


@pytest.mark.asyncio
async def test_ocr_extracts_text_when_available(monkeypatch, tmp_path):
    _install_fake_pytesseract(monkeypatch, text="Sign Up Now")
    _install_fake_pil(monkeypatch)

    img_path = tmp_path / "shot.png"
    img_path.write_bytes(b"fake-png-bytes")

    engine = OCREngine(lang="eng")
    result = await engine.extract_text(str(img_path))

    assert result.available is True
    assert result.text == "Sign Up Now"
    assert len(result.word_boxes) == 3
    assert result.word_boxes[0]["text"] == "Sign"


@pytest.mark.asyncio
async def test_ocr_degrades_gracefully_when_tesseract_missing(monkeypatch, tmp_path):
    _install_fake_pytesseract(monkeypatch, raise_on_version=True)
    _install_fake_pil(monkeypatch)

    img_path = tmp_path / "shot.png"
    img_path.write_bytes(b"fake-png-bytes")

    engine = OCREngine()
    result = await engine.extract_text(str(img_path))

    assert result.available is False
    assert result.text == ""
    assert "tesseract" in result.error.lower()


@pytest.mark.asyncio
async def test_ocr_missing_file_returns_unavailable(tmp_path):
    engine = OCREngine()
    result = await engine.extract_text(str(tmp_path / "nonexistent.png"))

    assert result.available is False
    assert "not found" in result.error
