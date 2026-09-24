"""SPEC §7 ``log.md`` append.

ALWAYS append-only; history is never edited or reordered. New entries land
under a today-dated ``## YYYY-MM-DD`` heading; newest first (SPEC §7).

Existing file bytes are preserved verbatim except for the inserted block (no
reformatting of prior entries). Writes are atomic via
``okf_loom.io_utils.atomic_write_text``.
"""
from __future__ import annotations

import difflib
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .io_utils import atomic_write_text
from .model import Bundle

_DATE_HEADING_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})", re.MULTILINE)
_LOG_HEADER = "# Update Log\n\n"


@contextmanager
def _cross_process_log_lock(path: Path):
    """Serialize the log read/modify/replace cycle across processes."""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def append_log_entry(
    bundle: Bundle,
    *,
    kind: str,
    entry: str,
    date_str: str | None = None,
    log_rel: str = "log.md",
    dry_run: bool = False,
) -> str:
    """Append one entry to a SPEC §7 log.md.

    Args:
        bundle: a loaded ``Bundle``.
        kind: bold-tag kind, e.g. ``"Update"`` / ``"Creation"`` /
            ``"Deprecation"`` / ``"Initialization"`` or a custom label.
        entry: the entry text.
        date_str: ISO date ``YYYY-MM-DD``; defaults to today (UTC).
        log_rel: log file path relative to the bundle root. MUST stay
            inside the bundle (validated; rejects ``..`` escapes).
        dry_run: when True, do not write; return a unified-diff preview instead.

    Returns:
        A human-readable summary. In dry_run, a unified-diff-style string of
        what would be written.

    Raises:
        ValueError: if ``log_rel`` escapes the bundle root.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).date().isoformat()
    # Validate log_rel is contained within the bundle root (defence in depth
    # against operator foot-guns; without this, ``--log-path ../escape.md``
    # would write outside the bundle).
    log_path = _resolve_within_bundle(bundle.root, log_rel)
    with bundle.mutation() as transaction:
        with _cross_process_log_lock(log_path):
            new_line = f"* **{kind}**: {entry}"

            existed = log_path.exists()
            raw = log_path.read_text(encoding="utf-8") if existed else ""
            new_text = _insert_entry(raw, date_str, new_line)

            if dry_run:
                return _unified_diff(raw, new_text, log_rel)

            transaction.commit_log(
                log_path,
                new_text,
                lambda path, content: atomic_write_text(path, content),
            )
            action = "appended" if existed and raw else "created"
            return f"{action}: {log_rel} (## {date_str})"


def _resolve_within_bundle(bundle_root: Path, rel: str) -> Path:
    """Resolve ``rel`` against ``bundle_root`` and ensure it stays inside."""
    root = Path(bundle_root).resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as e:
        raise ValueError(
            f"log_rel escapes bundle root: {rel!r}"
        ) from e
    return candidate


# ---------------------------------------------------------------------------
# Insertion logic
# ---------------------------------------------------------------------------


def _insert_entry(raw: str, date_str: str, new_line: str) -> str:
    """Insert ``new_line`` under ``## <date_str>``, prepending the heading
    block if absent. Newest first. Existing bytes preserved verbatim except
    for the inserted block."""
    if not raw:
        return f"{_LOG_HEADER}## {date_str}\n{new_line}\n"

    # 1) Today's heading already present -> insert the new line right under it.
    today_re = re.compile(
        rf"^##\s+{re.escape(date_str)}\b.*$", re.MULTILINE
    )
    m = today_re.search(raw)
    if m:
        # m.end() sits just before the heading line's trailing newline.
        nl_at = m.end() if m.end() < len(raw) and raw[m.end()] == "\n" else m.end()
        insert_at = nl_at + 1 if nl_at < len(raw) and raw[nl_at] == "\n" else nl_at
        return raw[:insert_at] + new_line + "\n" + raw[insert_at:]

    # 2) No today heading -> prepend a new block above the newest existing one.
    first = _DATE_HEADING_RE.search(raw)
    block = f"## {date_str}\n{new_line}\n\n"
    if first:
        prefix = raw[: first.start()]
        # Ensure a blank line separates the new block from the next heading.
        if not prefix.endswith("\n\n"):
            prefix = prefix.rstrip("\n") + "\n\n"
        return prefix + block + raw[first.start():]
    # No date headings at all -> append after existing content.
    return raw.rstrip("\n") + "\n\n" + f"## {date_str}\n{new_line}\n"


# ---------------------------------------------------------------------------
# I/O + diff
# ---------------------------------------------------------------------------


# Atomic writes are provided by ``okf_loom.io_utils.atomic_write_text``.
# The old module-private ``_atomic_write`` helper has been removed in
# favour of the shared, mkstemp-based implementation that is safe under
# concurrent writers and cleans up tmp files on exception.


def _unified_diff(old: str, new: str, label: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"{label} (current)",
        tofile=f"{label} (after)",
        n=3,
    )
    out = "".join(diff)
    if not out:
        return "(no changes)"
    return out
