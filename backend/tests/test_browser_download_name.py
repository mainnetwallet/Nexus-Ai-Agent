"""Download-filename sanitization (backend/browser/engine.py).

A browser-initiated download's `suggested_filename` is supplied by the
remote site (Content-Disposition) and can contain ``../``, absolute paths,
or Windows-style separators -- even on POSIX hosts. `_safe_download_name`
must reduce it to a bare basename so a download can never escape the
downloads directory, and must fall back to a timestamped name when nothing
usable remains.
"""
import re

from backend.browser.engine import _safe_download_name


def test_plain_name_is_kept():
    assert _safe_download_name("report.pdf") == "report.pdf"


def test_posix_parent_traversal_is_stripped():
    assert _safe_download_name("../../etc/passwd") == "passwd"


def test_absolute_path_is_reduced_to_basename():
    assert _safe_download_name("/etc/shadow") == "shadow"


def test_windows_separators_are_handled():
    assert _safe_download_name("..\\..\\evil.exe") == "evil.exe"


def test_mixed_traversal_is_stripped():
    assert _safe_download_name("a/b/../c.txt") == "c.txt"


def test_degenerate_names_fall_back_to_timestamp():
    for bad in ("..", ".", "", "   ", "../..", "..\\..", "/"):
        name = _safe_download_name(bad)
        assert re.fullmatch(r"download-\d+\.bin", name), f"{bad!r} -> {name!r}"
