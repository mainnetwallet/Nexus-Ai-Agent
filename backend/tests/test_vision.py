import pytest

from backend.vision.ocr import OCRResult
from backend.vision.vision_engine import VisionAnalyzer, VisionPerception


class FakeOCR:
    def __init__(self, text="canvas text here"):
        self._text = text

    async def extract_text(self, image_path, max_chars=None):
        return OCRResult(text=self._text, available=True)


class FakeLLM:
    def __init__(self, response=None, raise_exc=False):
        self._response = response or {
            "page_summary": "A canvas-based sign-up widget",
            "elements": [{"text": "Continue", "kind": "button", "approx_location": "center"}],
        }
        self._raise_exc = raise_exc

    async def complete_json_with_image(self, system_prompt, user_prompt, image_path, max_tokens=1200):
        if self._raise_exc:
            raise ValueError("model refused")
        return self._response


def test_should_trigger_below_threshold(monkeypatch):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "vision_enabled", True)
    monkeypatch.setattr(settings_module.settings, "vision_min_elements_threshold", 3)

    analyzer = VisionAnalyzer(llm=FakeLLM(), ocr=FakeOCR())
    assert analyzer.should_trigger([]) is True
    assert analyzer.should_trigger([{"text": "a"}, {"text": "b"}]) is True
    assert analyzer.should_trigger([{"text": "a"}, {"text": "b"}, {"text": "c"}]) is False


def test_should_trigger_respects_disabled_flag(monkeypatch):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "vision_enabled", False)

    analyzer = VisionAnalyzer(llm=FakeLLM(), ocr=FakeOCR())
    assert analyzer.should_trigger([]) is False


@pytest.mark.asyncio
async def test_analyze_merges_ocr_and_vision_elements(monkeypatch, tmp_path):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "vision_enabled", True)

    analyzer = VisionAnalyzer(llm=FakeLLM(), ocr=FakeOCR(text="Total: $42"))
    perception = await analyzer.analyze(str(tmp_path / "shot.png"), goal="buy the item")

    assert perception.triggered is True
    assert perception.page_summary == "A canvas-based sign-up widget"
    assert perception.ocr_text == "Total: $42"
    assert len(perception.vision_elements) == 1
    assert perception.vision_elements[0]["text"] == "Continue"


@pytest.mark.asyncio
async def test_analyze_handles_llm_failure_gracefully(monkeypatch, tmp_path):
    from backend.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "vision_enabled", True)

    analyzer = VisionAnalyzer(llm=FakeLLM(raise_exc=True), ocr=FakeOCR(text="fallback text"))
    perception = await analyzer.analyze(str(tmp_path / "shot.png"), goal="buy the item")

    assert perception.triggered is True
    assert perception.vision_elements == []
    assert perception.ocr_text == "fallback text"
    assert perception.error


def test_merge_into_elements_tags_vision_source():
    dom_elements = [{"tag": "button", "text": "Login"}]
    perception = VisionPerception(
        triggered=True,
        vision_elements=[{"text": "Play Now", "kind": "button", "approx_location": "center"}],
    )

    merged = VisionAnalyzer.merge_into_elements(dom_elements, perception)

    assert len(merged) == 2
    assert merged[0]["text"] == "Login"
    assert merged[1]["text"] == "Play Now"
    assert merged[1]["source"] == "vision"
