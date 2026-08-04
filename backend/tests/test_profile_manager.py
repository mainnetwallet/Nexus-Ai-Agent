import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.database.models import ProfileActivity, ProfileRecord
from backend.database.session import get_session, init_db
from backend.identity.detector import ServiceCheck
from backend.identity.manager import ProfileManager
from backend.identity.registry import ProfileBusyError, ProfileError, ProfileNotFoundError, ProfileRegistry


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    yield
    async with get_session() as session:
        await session.execute(delete(ProfileActivity))
        await session.execute(delete(ProfileRecord))


class FakeSessionDetector:
    """Returns pre-programmed results instead of launching a real browser."""

    def __init__(self, results: dict[str, bool | None]):
        self.results = results
        self.calls: list[list[str]] = []

    async def detect_all(self, engine, services):
        self.calls.append(list(services))
        return {
            svc: ServiceCheck(svc, self.results.get(svc), "fake") for svc in services
        }


class FakeBrowserEngine:
    """Stand-in for backend.browser.engine.BrowserEngine -- never launches
    a real browser; check_sessions only needs *something* to pass through."""


def make_manager(tmp_path, results: dict[str, bool | None] | None = None):
    registry = ProfileRegistry(data_dir=tmp_path)
    detector = FakeSessionDetector(results or {})
    return ProfileManager(registry, detector=detector), registry, detector


@pytest.mark.asyncio
async def test_load_for_task_resolves_by_id_and_by_name(tmp_path):
    manager, registry, _ = make_manager(tmp_path)
    created = await registry.create_profile("Profile-01", wallet_label="wallet-1")

    by_id = await manager.load_for_task(created["id"])
    assert by_id.name == "Profile-01"
    assert by_id.wallet_label == "wallet-1"
    await manager.release(created["id"])  # simulates the first task finishing

    by_name = await manager.load_for_task("profile-01")
    assert by_name.id == created["id"]


@pytest.mark.asyncio
async def test_load_for_task_raises_busy_when_already_in_use(tmp_path):
    """Multi-Profile Browser Management: the same Chrome Profile can't be
    claimed by two tasks at once (different profiles can still run
    concurrently -- see test_task_queue_profile.py), even though it stays
    freely available to whichever task grabs it next once released."""
    manager, registry, _ = make_manager(tmp_path)
    created = await registry.create_profile("Profile-01")

    await manager.load_for_task(created["id"])
    with pytest.raises(ProfileBusyError):
        await manager.load_for_task(created["id"])

    await manager.release(created["id"])
    # Freed up again -- a second task can now claim it without error.
    reloaded = await manager.load_for_task(created["id"])
    assert reloaded.id == created["id"]


@pytest.mark.asyncio
async def test_load_for_task_rejects_disabled_profile(tmp_path):
    manager, registry, _ = make_manager(tmp_path)
    created = await registry.create_profile("Profile-01")
    await registry.set_enabled(created["id"], False)

    with pytest.raises(ProfileError):
        await manager.load_for_task(created["id"])


@pytest.mark.asyncio
async def test_load_for_task_raises_for_unknown_profile(tmp_path):
    manager, _, _ = make_manager(tmp_path)
    with pytest.raises(ProfileNotFoundError):
        await manager.load_for_task("does-not-exist")


@pytest.mark.asyncio
async def test_check_sessions_only_checks_configured_services(tmp_path):
    manager, registry, detector = make_manager(tmp_path, {"gmail": True})
    created = await registry.create_profile("Profile-01", gmail_account="a@gmail.com")
    loaded = await manager.load_for_task(created["id"])

    result = await manager.check_sessions(loaded, FakeBrowserEngine())

    assert detector.calls == [["gmail"]]  # x/discord not configured -> skipped
    assert result == {"gmail": {"authenticated": True, "detail": "fake"}}


@pytest.mark.asyncio
async def test_check_sessions_returns_empty_when_nothing_configured(tmp_path):
    manager, registry, detector = make_manager(tmp_path)
    created = await registry.create_profile("Profile-01")
    loaded = await manager.load_for_task(created["id"])

    result = await manager.check_sessions(loaded, FakeBrowserEngine())

    assert result == {}
    assert detector.calls == []


@pytest.mark.asyncio
async def test_check_sessions_notifies_for_each_unauthenticated_service(tmp_path):
    manager, registry, _ = make_manager(tmp_path, {"gmail": True, "x": False})
    created = await registry.create_profile("Profile-01", gmail_account="a@gmail.com", x_account="a")
    loaded = await manager.load_for_task(created["id"])

    notified: list[str] = []

    async def notify_fn(message: str):
        notified.append(message)

    await manager.check_sessions(loaded, FakeBrowserEngine(), notify_fn=notify_fn)

    assert len(notified) == 1
    assert "X" in notified[0] or "x" in notified[0].lower()


@pytest.mark.asyncio
async def test_check_sessions_records_result_on_registry(tmp_path):
    manager, registry, _ = make_manager(tmp_path, {"gmail": False})
    created = await registry.create_profile("Profile-01", gmail_account="a@gmail.com")
    loaded = await manager.load_for_task(created["id"])

    await manager.check_sessions(loaded, FakeBrowserEngine())

    profile = await registry.get_profile(created["id"])
    assert profile.gmail_authenticated is False
