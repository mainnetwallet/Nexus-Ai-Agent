from backend.identity.fs import ProfileFilesystem


def test_create_makes_directory(tmp_path):
    fs = ProfileFilesystem(tmp_path)
    path = fs.create("profile-1")
    assert (tmp_path / "profile-1").exists()
    assert path == str(tmp_path / "profile-1")


def test_delete_removes_directory_and_is_noop_if_missing(tmp_path):
    fs = ProfileFilesystem(tmp_path)
    fs.create("profile-1")
    fs.delete("profile-1")
    assert not (tmp_path / "profile-1").exists()

    # deleting again should not raise
    fs.delete("profile-1")


def test_clone_copies_full_directory_tree(tmp_path):
    fs = ProfileFilesystem(tmp_path)
    fs.create("source")
    source_default = tmp_path / "source" / "Default"
    source_default.mkdir(parents=True)
    (source_default / "Cookies").write_text("cookie-bytes")

    fs.clone("source", "target")

    target_cookies = tmp_path / "target" / "Default" / "Cookies"
    assert target_cookies.exists()
    assert target_cookies.read_text() == "cookie-bytes"


def test_clone_overwrites_existing_target(tmp_path):
    fs = ProfileFilesystem(tmp_path)
    fs.create("source")
    (tmp_path / "source" / "marker.txt").write_text("from-source")
    fs.create("target")
    (tmp_path / "target" / "stale.txt").write_text("stale")

    fs.clone("source", "target")

    assert (tmp_path / "target" / "marker.txt").read_text() == "from-source"
    assert not (tmp_path / "target" / "stale.txt").exists()


def test_clone_creates_empty_target_when_source_missing(tmp_path):
    fs = ProfileFilesystem(tmp_path)
    fs.clone("does-not-exist", "target")
    assert (tmp_path / "target").exists()


def test_inspect_missing_directory_reports_not_exists(tmp_path):
    fs = ProfileFilesystem(tmp_path)
    result = fs.inspect(str(tmp_path / "no-such-dir"))
    assert result["exists"] is False
    assert result["cookies"]["present"] is False
    assert result["local_storage"]["present"] is False
    assert result["session_storage"]["present"] is False
    assert result["extensions"] == []


def test_inspect_populated_directory_under_default_subdir(tmp_path):
    fs = ProfileFilesystem(tmp_path)
    root = fs.dir_for("profile-1")
    default_dir = root / "Default"
    (default_dir / "Local Storage" / "leveldb").mkdir(parents=True)
    (default_dir / "Session Storage").mkdir(parents=True)
    extensions_dir = default_dir / "Extensions"
    (extensions_dir / "ext-abc").mkdir(parents=True)
    (extensions_dir / "ext-xyz").mkdir(parents=True)
    cookies_file = default_dir / "Cookies"
    cookies_file.write_bytes(b"0123456789")

    result = fs.inspect(str(root))

    assert result["exists"] is True
    assert result["cookies"]["present"] is True
    assert result["cookies"]["size_bytes"] == 10
    assert result["local_storage"]["present"] is True
    assert result["session_storage"]["present"] is True
    assert result["extensions"] == ["ext-abc", "ext-xyz"]


def test_inspect_falls_back_to_root_when_no_default_subdir(tmp_path):
    fs = ProfileFilesystem(tmp_path)
    root = fs.dir_for("profile-1")
    root.mkdir(parents=True)
    (root / "Cookies").write_text("x")

    result = fs.inspect(str(root))

    assert result["exists"] is True
    assert result["cookies"]["present"] is True
    assert result["local_storage"]["present"] is False
