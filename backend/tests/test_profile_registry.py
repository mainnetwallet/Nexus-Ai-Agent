import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.database.models import ProfileActivity, ProfileRecord, ProfileStatus
from backend.database.session import get_session, init_db
from backend.identity.registry import ProfileError, ProfileNotFoundError, ProfileRegistry


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await init_db()
    yield
    async with get_session() as session:
        await session.execute(delete(ProfileActivity))
        await session.execute(delete(ProfileRecord))


@pytest.fixture
def registry(tmp_path):
    return ProfileRegistry(data_dir=tmp_path)


# ---------------------------------------------------------------------- #
# CRUD
# ---------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_create_profile_persists_and_creates_dir(registry, tmp_path):
    created = await registry.create_profile(
        "Profile-01", wallet_label="wallet-1", gmail_account="a@gmail.com", tags=["work"]
    )
    assert created["name"] == "Profile-01"
    assert created["wallet_label"] == "wallet-1"
    assert created["tags"] == ["work"]
    assert created["status"] == ProfileStatus.READY.value
    assert created["enabled"] is True
    assert created["is_active"] is False
    profile_dir = tmp_path / "browser_profiles" / created["id"]
    assert profile_dir.exists()


@pytest.mark.asyncio
async def test_create_profile_rejects_duplicate_name(registry):
    await registry.create_profile("Profile-01")
    with pytest.raises(ProfileError):
        await registry.create_profile("Profile-01")


@pytest.mark.asyncio
async def test_list_profiles_filters_by_search_tag_and_enabled(registry):
    a = await registry.create_profile("Alpha", gmail_account="alpha@gmail.com", tags=["team-a"])
    b = await registry.create_profile("Beta", tags=["team-b"])
    await registry.set_enabled(b["id"], False)

    all_profiles = await registry.list_profiles()
    assert {p["name"] for p in all_profiles} == {"Alpha", "Beta"}

    by_search = await registry.list_profiles(search="alpha")
    assert [p["id"] for p in by_search] == [a["id"]]

    by_tag = await registry.list_profiles(tag="team-b")
    assert [p["id"] for p in by_tag] == [b["id"]]

    enabled_only = await registry.list_profiles(enabled_only=True)
    assert [p["id"] for p in enabled_only] == [a["id"]]


@pytest.mark.asyncio
async def test_get_profile_returns_record_and_raises_for_missing(registry):
    created = await registry.create_profile("Profile-01")
    profile = await registry.get_profile(created["id"])
    assert profile.name == "Profile-01"

    with pytest.raises(ProfileNotFoundError):
        await registry.get_profile("does-not-exist")


@pytest.mark.asyncio
async def test_get_by_name_is_case_insensitive(registry):
    created = await registry.create_profile("Profile-01")
    found = await registry.get_by_name("profile-01")
    assert found is not None
    assert found.id == created["id"]
    assert await registry.get_by_name("no-such-profile") is None


@pytest.mark.asyncio
async def test_resolve_accepts_id_or_name(registry):
    created = await registry.create_profile("Profile-01")
    by_id = await registry.resolve(created["id"])
    by_name = await registry.resolve("profile-01")
    assert by_id.id == created["id"]
    assert by_name.id == created["id"]
    assert await registry.resolve("nope") is None


@pytest.mark.asyncio
async def test_update_profile_applies_allowed_fields_and_logs_activity(registry):
    created = await registry.create_profile("Profile-01")
    updated = await registry.update_profile(created["id"], notes="updated notes", tags=["x"])
    assert updated["notes"] == "updated notes"
    assert updated["tags"] == ["x"]

    activity = await registry.get_activity(profile_id=created["id"])
    assert any(a["event_type"] == "updated" for a in activity)


@pytest.mark.asyncio
async def test_update_profile_rejects_name_clash(registry):
    await registry.create_profile("Alpha")
    beta = await registry.create_profile("Beta")
    with pytest.raises(ProfileError):
        await registry.update_profile(beta["id"], name="Alpha")


@pytest.mark.asyncio
async def test_update_profile_raises_for_missing(registry):
    with pytest.raises(ProfileNotFoundError):
        await registry.update_profile("does-not-exist", notes="x")


@pytest.mark.asyncio
async def test_rename_profile_updates_name(registry):
    created = await registry.create_profile("Profile-01")
    renamed = await registry.rename_profile(created["id"], "Profile-01-Renamed")
    assert renamed["name"] == "Profile-01-Renamed"


@pytest.mark.asyncio
async def test_delete_profile_removes_row_and_directory(registry, tmp_path):
    created = await registry.create_profile("Profile-01")
    profile_dir = tmp_path / "browser_profiles" / created["id"]
    assert profile_dir.exists()

    await registry.delete_profile(created["id"])

    with pytest.raises(ProfileNotFoundError):
        await registry.get_profile(created["id"])
    assert not profile_dir.exists()


@pytest.mark.asyncio
async def test_delete_profile_raises_for_missing(registry):
    with pytest.raises(ProfileNotFoundError):
        await registry.delete_profile("does-not-exist")


# ---------------------------------------------------------------------- #
# Clone / Import / Export
# ---------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_clone_profile_copies_metadata_and_directory(registry, tmp_path):
    source = await registry.create_profile(
        "Source", wallet_label="wallet-1", gmail_account="a@gmail.com", tags=["t"]
    )
    source_dir = tmp_path / "browser_profiles" / source["id"]
    (source_dir / "Cookies").write_text("fake-cookie-data")

    clone = await registry.clone_profile(source["id"], "Clone")
    assert clone["name"] == "Clone"
    assert clone["wallet_label"] == "wallet-1"
    assert clone["gmail_account"] == "a@gmail.com"
    assert clone["tags"] == ["t"]

    clone_dir = tmp_path / "browser_profiles" / clone["id"]
    assert (clone_dir / "Cookies").read_text() == "fake-cookie-data"

    activity = await registry.get_activity(profile_id=clone["id"])
    assert any(a["event_type"] == "cloned" for a in activity)


@pytest.mark.asyncio
async def test_clone_profile_rejects_duplicate_name_and_missing_source(registry):
    source = await registry.create_profile("Source")
    await registry.create_profile("Existing")
    with pytest.raises(ProfileError):
        await registry.clone_profile(source["id"], "Existing")
    with pytest.raises(ProfileNotFoundError):
        await registry.clone_profile("does-not-exist", "New")


@pytest.mark.asyncio
async def test_export_then_import_round_trip(registry):
    created = await registry.create_profile(
        "Profile-01", wallet_label="wallet-1", gmail_account="a@gmail.com", tags=["t"], notes="hi"
    )
    exported = await registry.export_profile(created["id"])
    assert "id" not in exported
    assert "chrome_profile_dir" not in exported
    assert "is_active" not in exported

    exported["name"] = "Imported-Profile"
    imported = await registry.import_profile(exported)
    assert imported["name"] == "Imported-Profile"
    assert imported["wallet_label"] == "wallet-1"
    assert imported["gmail_account"] == "a@gmail.com"
    assert imported["tags"] == ["t"]
    assert imported["notes"] == "hi"
    assert imported["id"] != created["id"]


# ---------------------------------------------------------------------- #
# Enable / Disable / Select active
# ---------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_set_enabled_false_disables_and_sets_status(registry):
    created = await registry.create_profile("Profile-01")
    disabled = await registry.set_enabled(created["id"], False)
    assert disabled["enabled"] is False
    assert disabled["status"] == ProfileStatus.DISABLED.value

    enabled = await registry.set_enabled(created["id"], True)
    assert enabled["enabled"] is True
    assert enabled["status"] == ProfileStatus.READY.value


@pytest.mark.asyncio
async def test_select_active_profile_rejects_disabled_profile(registry):
    created = await registry.create_profile("Profile-01")
    await registry.set_enabled(created["id"], False)
    with pytest.raises(ProfileError):
        await registry.select_active_profile(created["id"])


@pytest.mark.asyncio
async def test_select_active_profile_enforces_single_active_invariant(registry):
    a = await registry.create_profile("Alpha")
    b = await registry.create_profile("Beta")

    await registry.select_active_profile(a["id"])
    active = await registry.get_active_profile()
    assert active["id"] == a["id"]

    await registry.select_active_profile(b["id"])
    active = await registry.get_active_profile()
    assert active["id"] == b["id"]

    all_profiles = await registry.list_profiles()
    active_ids = [p["id"] for p in all_profiles if p["is_active"]]
    assert active_ids == [b["id"]]


@pytest.mark.asyncio
async def test_select_active_profile_raises_for_missing(registry):
    with pytest.raises(ProfileNotFoundError):
        await registry.select_active_profile("does-not-exist")


@pytest.mark.asyncio
async def test_get_active_profile_none_when_nothing_selected(registry):
    await registry.create_profile("Profile-01")
    assert await registry.get_active_profile() is None


# ---------------------------------------------------------------------- #
# Session status
# ---------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_record_session_check_all_authenticated_sets_ready(registry):
    created = await registry.create_profile("Profile-01", gmail_account="a@gmail.com", x_account="a")
    result = await registry.record_session_check(created["id"], {"gmail": True, "x": True})
    assert result["status"] == ProfileStatus.READY.value
    assert result["sessions"]["gmail"] is True
    assert result["sessions"]["x"] is True
    assert result["last_session_check_at"] is not None


@pytest.mark.asyncio
async def test_record_session_check_any_unauthenticated_sets_needs_login(registry):
    created = await registry.create_profile("Profile-01", gmail_account="a@gmail.com", x_account="a")
    result = await registry.record_session_check(created["id"], {"gmail": True, "x": False})
    assert result["status"] == ProfileStatus.NEEDS_LOGIN.value
    assert result["sessions"]["x"] is False


@pytest.mark.asyncio
async def test_record_session_check_logs_activity_and_raises_for_missing(registry):
    created = await registry.create_profile("Profile-01", gmail_account="a@gmail.com")
    await registry.record_session_check(created["id"], {"gmail": True})
    activity = await registry.get_activity(profile_id=created["id"])
    assert any(a["event_type"] == "session_checked" for a in activity)

    with pytest.raises(ProfileNotFoundError):
        await registry.record_session_check("does-not-exist", {"gmail": True})


# ---------------------------------------------------------------------- #
# Activity history
# ---------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_activity_orders_newest_first_and_respects_limit(registry):
    created = await registry.create_profile("Profile-01")
    await registry.update_profile(created["id"], notes="one")
    await registry.update_profile(created["id"], notes="two")

    activity = await registry.get_activity(profile_id=created["id"], limit=1)
    assert len(activity) == 1
    assert activity[0]["description"].startswith("Updated fields")


@pytest.mark.asyncio
async def test_get_activity_without_profile_id_returns_all(registry):
    a = await registry.create_profile("Alpha")
    b = await registry.create_profile("Beta")
    activity = await registry.get_activity()
    profile_ids = {a["id"], b["id"]}
    assert any(entry["profile_id"] in profile_ids for entry in activity)
