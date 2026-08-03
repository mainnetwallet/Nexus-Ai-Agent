import pytest

from backend.skills.runner import SkillRunner


class FakeEngine:
    """Minimal stand-in for BrowserEngine covering exactly the surface
    SkillRunner touches, so these tests never launch a real browser."""

    def __init__(self, click_should_fail_on: str | None = None):
        self.click_should_fail_on = click_should_fail_on
        self.navigated_to: list[str] = []
        self.clicked: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.scrolled: list[str] = []
        self.uploaded: list[tuple[str, str]] = []
        self.waited = 0
        self.screenshots = 0

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        if url == "https://fail-nav.example.com":
            raise RuntimeError("navigation blocked")
        self.navigated_to.append(url)

    async def smart_click(self, selector_or_text: str, exact: bool = False, timeout_ms=None) -> bool:
        self.clicked.append(selector_or_text)
        return selector_or_text != self.click_should_fail_on

    async def smart_type(self, selector_or_label: str, text: str, clear_first: bool = True) -> bool:
        self.typed.append((selector_or_label, text))
        return True

    async def smart_scroll(self, direction: str = "down", amount_px: int = 800) -> None:
        self.scrolled.append(direction)

    async def smart_wait(self, condition: str = "networkidle", timeout_ms: int = 10_000) -> None:
        self.waited += 1

    async def upload_file(self, selector: str, file_path: str) -> bool:
        self.uploaded.append((selector, file_path))
        return True

    async def screenshot(self, name_hint: str = "step") -> str:
        self.screenshots += 1
        return f"/tmp/{name_hint}.png"


def make_skill(**overrides):
    skill = {
        "id": "skill-1",
        "name": "Fill contact form",
        "variables": [{"name": "email", "description": "", "default": "default@example.com"}],
        "workflow": [
            {"action": "click", "target": "#start", "value": "", "description": "click start"},
            {"action": "type", "target": "#email", "value": "{{email}}", "description": "type email"},
            {"action": "scroll", "target": "", "value": "down", "description": "scroll down"},
            {"action": "wait", "target": "", "value": "", "description": "wait for load"},
            {"action": "click", "target": "#submit", "value": "", "description": "submit"},
        ],
    }
    skill.update(overrides)
    return skill


@pytest.mark.asyncio
async def test_run_replays_all_steps_and_substitutes_variables():
    engine = FakeEngine()
    runner = SkillRunner(engine)

    outcome = await runner.run(make_skill(), website="https://example.com")

    assert outcome.status == "succeeded"
    assert len(outcome.steps) == 5
    assert all(s.success for s in outcome.steps)
    assert engine.navigated_to == ["https://example.com"]
    assert engine.typed == [("#email", "default@example.com")]
    assert engine.screenshots == 5


@pytest.mark.asyncio
async def test_run_uses_variable_override_over_default():
    engine = FakeEngine()
    runner = SkillRunner(engine)

    outcome = await runner.run(
        make_skill(), website="https://example.com", variables={"email": "override@example.com"}
    )

    assert outcome.status == "succeeded"
    assert engine.typed == [("#email", "override@example.com")]


@pytest.mark.asyncio
async def test_run_stops_and_reports_failure_at_first_failed_step():
    engine = FakeEngine(click_should_fail_on="#submit")
    runner = SkillRunner(engine)

    outcome = await runner.run(make_skill(), website="https://example.com")

    assert outcome.status == "failed"
    assert "falling back to standard planning" in outcome.summary
    # Ran through all 5 steps, but the last one is recorded as a failure --
    # no steps after the failure since the loop returns immediately.
    assert len(outcome.steps) == 5
    assert outcome.steps[-1].success is False
    assert outcome.steps[-1].action == "click"
    assert outcome.steps[-1].target == "#submit"


@pytest.mark.asyncio
async def test_run_empty_workflow_fails_without_touching_engine():
    engine = FakeEngine()
    runner = SkillRunner(engine)

    outcome = await runner.run(make_skill(workflow=[]), website="https://example.com")

    assert outcome.status == "failed"
    assert "empty workflow" in outcome.summary
    assert engine.navigated_to == []


@pytest.mark.asyncio
async def test_run_navigation_failure_short_circuits():
    engine = FakeEngine()
    runner = SkillRunner(engine)

    outcome = await runner.run(make_skill(), website="https://fail-nav.example.com")

    assert outcome.status == "failed"
    assert "could not navigate" in outcome.summary
    assert engine.clicked == []


@pytest.mark.asyncio
async def test_run_navigate_action_step_uses_value_as_url():
    engine = FakeEngine()
    runner = SkillRunner(engine)
    skill = make_skill(
        workflow=[{"action": "navigate", "target": "", "value": "https://second-page.example.com", "description": ""}]
    )

    outcome = await runner.run(skill, website="https://example.com")

    assert outcome.status == "succeeded"
    assert engine.navigated_to == ["https://example.com", "https://second-page.example.com"]


@pytest.mark.asyncio
async def test_run_unknown_action_fails_the_step():
    engine = FakeEngine()
    runner = SkillRunner(engine)
    skill = make_skill(workflow=[{"action": "teleport", "target": "", "value": "", "description": ""}])

    outcome = await runner.run(skill, website="https://example.com")

    assert outcome.status == "failed"
    assert outcome.steps[0].success is False


@pytest.mark.asyncio
async def test_run_no_website_skips_navigation():
    engine = FakeEngine()
    runner = SkillRunner(engine)
    skill = make_skill(workflow=[{"action": "click", "target": "#start", "value": "", "description": ""}])

    outcome = await runner.run(skill, website="")

    assert outcome.status == "succeeded"
    assert engine.navigated_to == []


@pytest.mark.asyncio
async def test_run_step_exception_is_caught_and_reported_as_failure():
    class ExplodingEngine(FakeEngine):
        async def smart_click(self, selector_or_text: str, exact: bool = False, timeout_ms=None) -> bool:
            raise RuntimeError("boom")

    engine = ExplodingEngine()
    runner = SkillRunner(engine)
    skill = make_skill(workflow=[{"action": "click", "target": "#start", "value": "", "description": ""}])

    outcome = await runner.run(skill, website="")

    assert outcome.status == "failed"
    assert outcome.steps[0].success is False
