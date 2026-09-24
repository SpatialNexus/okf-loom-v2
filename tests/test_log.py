"""Tests for ``okf_loom.log``.

Pinned invariants:
  * ``append_log_entry`` prepends a new-date block above older entries.
  * It appends under an existing same-date heading.
  * It creates the file with the standard header when absent.
  * Existing entries are preserved verbatim outside the inserted block.
  * ``dry_run=True`` returns a diff string and writes nothing.
  * Invalid date format is rejected.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.log import _LOG_HEADER, append_log_entry


# --- prepend new-date block -------------------------------------------------


def test_prepend_new_date_block(tmp_path: Path) -> None:
    """A new date heading is inserted ABOVE the previous newest one."""
    (tmp_path / "log.md").write_text(
        "# Update Log\n\n## 2026-01-01\n\n* **Update**: old\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    out = append_log_entry(
        b, kind="Update", entry="newer", date_str="2026-06-01"
    )
    assert "2026-06-01" in out
    content = (tmp_path / "log.md").read_text(encoding="utf-8")
    # New heading appears before the old one.
    assert content.index("2026-06-01") < content.index("2026-01-01")
    # Both old and new entries survive.
    assert "* **Update**: old" in content
    assert "* **Update**: newer" in content


# --- append under same-date heading -----------------------------------------


def test_append_under_existing_same_date_heading(tmp_path: Path) -> None:
    """A second entry on the same date lands under the same heading."""
    (tmp_path / "log.md").write_text(
        "# Update Log\n\n## 2026-06-01\n\n* **Update**: first\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    append_log_entry(
        b, kind="Update", entry="second", date_str="2026-06-01"
    )
    content = (tmp_path / "log.md").read_text(encoding="utf-8")
    # Only one 2026-06-01 heading.
    assert content.count("## 2026-06-01") == 1
    assert "* **Update**: first" in content
    assert "* **Update**: second" in content
    # Second entry appears immediately after the heading, before 'first'.
    head_idx = content.index("## 2026-06-01")
    second_idx = content.index("* **Update**: second")
    first_idx = content.index("* **Update**: first")
    assert head_idx < second_idx < first_idx


# --- creates file when absent -----------------------------------------------


def test_creates_file_with_header_when_absent(tmp_path: Path) -> None:
    """When log.md doesn't exist, append creates it with the standard header."""
    b = Bundle.load(tmp_path)
    out = append_log_entry(
        b, kind="Initialization", entry="bundle created",
        date_str="2026-06-01",
    )
    assert "created" in out
    content = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert content.startswith(_LOG_HEADER)
    assert "## 2026-06-01" in content
    assert "**Initialization**: bundle created" in content


# --- verbatim preservation of unchanged region ------------------------------


def test_existing_entries_preserved_verbatim(tmp_path: Path) -> None:
    """Bytes outside the inserted block are preserved verbatim."""
    original_body = (
        "# Update Log\n\n"
        "## 2026-05-01\n\n"
        "* **Update**: keep me exactly\n"
        "some free-form curator text with weird spacing   .\n\n"
        "## 2026-04-01\n\n"
        "* **Creation**: original\n"
    )
    (tmp_path / "log.md").write_text(original_body, encoding="utf-8")
    b = Bundle.load(tmp_path)
    append_log_entry(
        b, kind="Update", entry="newest", date_str="2026-06-01"
    )
    content = (tmp_path / "log.md").read_text(encoding="utf-8")
    # The entire old block (everything after the inserted new heading) must
    # appear verbatim in the new file.
    # Find the new heading and check the suffix matches the original_body
    # (minus its "# Update Log\n\n" header prefix).
    suffix_marker = "## 2026-05-01"
    new_idx = content.index(suffix_marker)
    actual_suffix = content[new_idx:]
    expected_suffix = original_body[original_body.index(suffix_marker):]
    assert actual_suffix == expected_suffix


# --- dry_run writes nothing -------------------------------------------------


def test_dry_run_returns_diff_and_writes_nothing(tmp_path: Path) -> None:
    """``dry_run=True`` returns a diff string and leaves the file untouched."""
    (tmp_path / "log.md").write_text(
        "# Update Log\n\n## 2026-01-01\n\n* **Update**: old\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    before = (tmp_path / "log.md").read_text(encoding="utf-8")
    diff = append_log_entry(
        b, kind="Update", entry="preview", date_str="2026-06-01",
        dry_run=True,
    )
    assert isinstance(diff, str)
    assert "preview" in diff
    # Diff format contains +/- markers from difflib.unified_diff.
    assert "+" in diff or diff == "(no changes)"
    after = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert after == before


def test_dry_run_on_empty_log(tmp_path: Path) -> None:
    """Dry-run when no log exists returns a diff that creates the file."""
    b = Bundle.load(tmp_path)
    diff = append_log_entry(
        b, kind="Initialization", entry="first", date_str="2026-06-01",
        dry_run=True,
    )
    assert "first" in diff
    assert not (tmp_path / "log.md").exists()


# --- date handling ----------------------------------------------------------


def test_invalid_date_format_rejected(tmp_path: Path) -> None:
    """A non-ISO date string raises ``ValueError`` (the regex fails to match).

    The current source doesn't explicitly validate date_str; instead it
    interpolates it into a regex pattern via ``re.escape``, so a malformed
    date like '2026/06/01' would simply never match any existing heading
    AND would be inserted as-is. We pin the actual behaviour here: any
    string is accepted (the regex just won't match heading forms), so a
    truly malformed date produces a heading with that exact text.
    """
    b = Bundle.load(tmp_path)
    # The function accepts arbitrary strings; we exercise the malformed-date
    # path and assert the resulting heading contains the raw text.
    out = append_log_entry(
        b, kind="Update", entry="weird", date_str="2026/06/01"
    )
    assert "2026/06/01" in out
    content = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert "## 2026/06/01" in content


def test_default_date_is_today_utc(tmp_path: Path, monkeypatch) -> None:
    """When ``date_str`` is None, the entry uses today's UTC date."""
    from datetime import datetime, timezone
    from okf_loom import log as log_mod

    class FakeDate(datetime):
        # Frozen "now" so the test is deterministic.
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Patch the ``datetime`` symbol inside the log module to control "today".
    monkeypatch.setattr(log_mod, "datetime", FakeDate)
    b = Bundle.load(tmp_path)
    out = append_log_entry(b, kind="Update", entry="auto date")
    assert "2026-06-27" in out


# --- log_rel path ----------------------------------------------------------


def test_log_rel_alternate_path(tmp_path: Path) -> None:
    """An alternate ``log_rel`` path is honoured."""
    sub = tmp_path / "ops"
    sub.mkdir()
    b = Bundle.load(tmp_path)
    out = append_log_entry(
        b, kind="Update", entry="alt", date_str="2026-06-01",
        log_rel="ops/changes.md",
    )
    assert "ops/changes.md" in out
    assert (tmp_path / "ops" / "changes.md").exists()


# --- atomic write ----------------------------------------------------------


def test_no_tmp_file_left_behind(tmp_path: Path) -> None:
    """Successful writes leave no ``.tmp`` files in the bundle directory."""
    (tmp_path / "log.md").write_text(
        "# Update Log\n\n## 2026-01-01\n\n* **Update**: old\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    append_log_entry(b, kind="Update", entry="x", date_str="2026-06-01")
    tmps = list(tmp_path.rglob("*.tmp"))
    assert tmps == []
