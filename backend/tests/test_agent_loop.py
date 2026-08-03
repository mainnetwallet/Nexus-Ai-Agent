import pytest

from backend.planner.agent_loop import AgentLoop


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    async def complete_json(self, system_prompt, user_prompt, max_tokens=1500, task_type=None):
        return self._responses.pop(0)


class FakeMemory:
    async def recall_similar_workflows(self, website, goal, top_k=3):
        return []

    async def save_workflow_outcome(self, website, goal, outcome):
        self.saved = outcome


class FakePage:
    url = "https://example.com"

    async def title(self):
        return "Example"


class FakeEngine:
    def __init__(self):
        self.page = FakePage()
        self._clicked = []

    async def navigate(self, url):
        self.page.url = url

    async def snapshot(self, name_hint="s"):
        from backend.browser.engine import PageSnapshot
        return PageSnapshot(url=self.page.url, title="Example", visible_text="Welcome", interactive_elements=[], screenshot_path="")

    async def detect_popup_or_dialog(self, timeout_ms=300):
        return None

    async def smart_click(self, target):
        self._clicked.append(target)
        return True

    async def screenshot(self, name_hint="s"):
        return "/tmp/fake.png"


@pytest.mark.asyncio
async def test_agent_loop_finishes_on_finish_action():
    llm = FakeLLM([
        {"action": "click", "target": "Sign Up", "value": "", "reasoning": "start signup"},
        {"action": "finish", "target": "", "value": "", "reasoning": "signup complete"},
    ])
    engine = FakeEngine()
    memory = FakeMemory()

    loop = AgentLoop(engine=engine, memory=memory, llm=llm, max_steps=10)
    outcome = await loop.run(website="https://example.com", goal="create account")

    assert outcome.status == "succeeded"
    assert engine._clicked == ["Sign Up"]
    assert len(outcome.steps) == 1


@pytest.mark.asyncio
async def test_agent_loop_stops_immediately_when_cancelled():
    # Would run several more steps if not cancelled -- should_cancel must be
    # honored before the first snapshot/decision of the loop.
    llm = FakeLLM([
        {"action": "click", "target": "Sign Up", "value": "", "reasoning": "start signup"},
        {"action": "click", "target": "Continue", "value": "", "reasoning": "should never be reached"},
    ])
    engine = FakeEngine()
    memory = FakeMemory()

    loop = AgentLoop(engine=engine, memory=memory, llm=llm, max_steps=10, should_cancel=lambda: True)
    outcome = await loop.run(website="https://example.com", goal="create account")

    assert outcome.status == "cancelled"
    assert engine._clicked == []
    assert len(outcome.steps) == 0


@pytest.mark.asyncio
async def test_agent_loop_awaits_wait_if_paused_before_each_step():
    llm = FakeLLM([
        {"action": "click", "target": "Sign Up", "value": "", "reasoning": "start signup"},
        {"action": "finish", "target": "", "value": "", "reasoning": "done"},
    ])
    engine = FakeEngine()
    memory = FakeMemory()
    calls = []

    async def wait_if_paused():
        calls.append(1)

    loop = AgentLoop(engine=engine, memory=memory, llm=llm, max_steps=10, wait_if_paused=wait_if_paused)
    outcome = await loop.run(website="https://example.com", goal="create account")

    assert outcome.status == "succeeded"
    # Once per step (2 steps: click, finish).
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_agent_loop_cancels_after_resuming_from_pause():
    # A task paused and then cancelled should stop as soon as it resumes,
    # without executing another action.
    llm = FakeLLM([
        {"action": "click", "target": "should never run", "value": "", "reasoning": ""},
    ])
    engine = FakeEngine()
    memory = FakeMemory()
    cancelled = {"flag": False}

    async def wait_if_paused():
        cancelled["flag"] = True  # simulate: got cancelled while paused, then resumed

    loop = AgentLoop(
        engine=engine,
        memory=memory,
        llm=llm,
        max_steps=10,
        wait_if_paused=wait_if_paused,
        should_cancel=lambda: cancelled["flag"],
    )
    outcome = await loop.run(website="https://example.com", goal="create account")

    assert outcome.status == "cancelled"
    assert engine._clicked == []
