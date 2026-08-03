import pytest

from backend.browser.engine import PageSnapshot
from backend.planner.decision_engine import DecisionEngine
from backend.vision.vision_engine import VisionAnalyzer


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts = []

    async def complete_json(self, system_prompt, user_prompt, max_tokens=1500):
        self.prompts.append(user_prompt)
        return self._responses.pop(0)


class NullVision(VisionAnalyzer):
    """Vision analyzer that never triggers -- used to isolate decide()/verify() tests."""

    def __init__(self):
        pass

    def should_trigger(self, interactive_elements):
        return False


@pytest.mark.asyncio
async def test_decide_returns_parsed_decision():
    llm = FakeLLM([{"action": "click", "target": "Sign Up", "value": "", "reasoning": "start", "confidence": 0.9}])
    engine = DecisionEngine(llm=llm, vision=NullVision())
    snapshot = PageSnapshot(url="https://x.com", title="X", visible_text="hi", interactive_elements=[], screenshot_path="")

    decision = await engine.decide("create account", None, "", snapshot, "No prior memory.")

    assert decision.action == "click"
    assert decision.target == "Sign Up"
    assert decision.confidence == 0.9


@pytest.mark.asyncio
async def test_decide_folds_recovery_context_into_prompt():
    llm = FakeLLM([{"action": "finish", "target": "", "value": "", "reasoning": "done"}])
    engine = DecisionEngine(llm=llm, vision=NullVision())
    snapshot = PageSnapshot(url="https://x.com", title="X", visible_text="hi", interactive_elements=[], screenshot_path="")

    await engine.decide("goal", None, "", snapshot, "No prior memory.", recovery_context="RECOVERY: previous action failed")

    assert "RECOVERY: previous action failed" in llm.prompts[0]


@pytest.mark.asyncio
async def test_decide_handles_llm_failure_gracefully():
    class BrokenLLM:
        async def complete_json(self, *a, **kw):
            raise RuntimeError("boom")

    engine = DecisionEngine(llm=BrokenLLM(), vision=NullVision())
    snapshot = PageSnapshot(url="https://x.com", title="X", visible_text="hi", interactive_elements=[], screenshot_path="")

    decision = await engine.decide("goal", None, "", snapshot, "No prior memory.")

    assert decision.action == "blocked"
    assert "LLM planning call failed" in decision.reasoning


def test_verify_detects_url_change():
    engine = DecisionEngine(llm=FakeLLM([]), vision=NullVision())
    result = engine.verify("https://a.com", "https://b.com", "click", success=True)
    assert result.changed is True
    assert "visible effect" in result.note


def test_verify_flags_reported_failure():
    engine = DecisionEngine(llm=FakeLLM([]), vision=NullVision())
    result = engine.verify("https://a.com", "https://a.com", "click", success=False)
    assert result.changed is False
    assert "failure" in result.note


def test_recovery_hint_empty_when_healthy():
    engine = DecisionEngine(llm=FakeLLM([]), vision=NullVision())
    assert engine.recovery_hint("click", "Sign Up", success=True, stall_count=0) == ""


def test_recovery_hint_on_action_failure():
    engine = DecisionEngine(llm=FakeLLM([]), vision=NullVision())
    hint = engine.recovery_hint("click", "Sign Up", success=False, stall_count=0)
    assert "failed to execute" in hint


def test_recovery_hint_on_stall():
    engine = DecisionEngine(llm=FakeLLM([]), vision=NullVision())
    hint = engine.recovery_hint("scroll", "", success=True, stall_count=2)
    assert "has not changed" in hint


@pytest.mark.asyncio
async def test_perceive_skips_vision_when_dom_has_enough_elements():
    engine = DecisionEngine(llm=FakeLLM([]), vision=NullVision())
    snapshot = PageSnapshot(
        url="https://x.com", title="X", visible_text="hi",
        interactive_elements=[{"text": "a"}, {"text": "b"}, {"text": "c"}], screenshot_path="",
    )
    result_snapshot, perception = await engine.perceive(snapshot, "goal")
    assert perception is None
    assert result_snapshot.interactive_elements == snapshot.interactive_elements
