"""Live collaborative studio logic layer (current spec §10-§13).

This module is the **reusable logic layer** the HTTP server (``server.py``),
the headless watcher (``watch.py``), and embedding harnesses all share. It
is deliberately framework-free: no ``http.server`` import, no asyncio. The
HTTP layer in ``server.py`` stays thin and calls into here.

Responsibilities (one implementation each, §17):
  * :class:`EventBus` — thread-safe fan-out for SSE clients (bounded per-client
    queues; a slow client gets deltas dropped + one ``resync`` enqueued so the
    watcher never blocks on a browser, §7.1).
  * :func:`rev_of` — per-doc content hash ``sha1(raw)[:12]`` for live-update
    coherence (§9.3 / D3).
  * :func:`new_id` — sortable ULID-style ids for events / comments (§10.5).
  * :class:`Studio` — session state + the append-only feeds under
    ``<bundle>/.okf-loom/session/`` (``events.jsonl``, ``directives.jsonl``,
    ``presence.json``, ``history/``) and the comment / presence / undo /
    activity API (current spec §11-§13).

Concurrency: append-only JSONL writes are serialized with an advisory file
lock (``fcntl.flock`` POSIX / ``msvcrt.locking`` Windows; O_APPEND fallback)
so multiple writers — the serve process, a separate ``okf watch`` process,
and direct CLI calls — never interleave a record (§10.5 "who writes it").
An in-process :class:`threading.Lock` orders same-process appends on top.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
import math
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterator, TypedDict

from .io_utils import (atomic_write_bytes, atomic_write_text,
                       bundle_transaction_lock, durable_atomic_write_text,
                       path_lock, path_locks)
from .annotations import capture_anchor, concept_content_rev, parse_v2_anchor
from .directive_store import DirectiveStore, DirectiveStoreError, DirectiveTransaction
from .paths import validate_segment
from .presence import PresenceStore, PresenceUnavailable
from .session_authority import (
    SessionAuthority,
    resolve_bundle_root_authority,
    resolve_session_authority,
)


class CommentMutationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class UncertainJSONLTail(ValueError):
    pass


class CommentSummaryUnavailable(RuntimeError):
    """The durable comment authority could not produce a trustworthy summary."""


class CommentSummaryV1(TypedDict):
    """Sanitized portable aggregate; no comment identity or content fields."""

    schema: int
    semantics: str
    counts: dict[str, int]
    summary_revision: str


def _read_regular_json_nofollow(path: Path) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("not a regular file")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                return json.load(handle)
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CommentMutationError("unavailable") from exc


def _read_regular_json_beneath_nofollow(parent: Path, name: str) -> Any:
    """Read a regular JSON leaf relative to one pinned directory authority."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise CommentMutationError("unavailable")
    parent_fd = -1
    fd = -1
    try:
        parent_fd = os.open(
            parent,
            os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
        parent_info = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_info.st_mode):
            raise OSError("not a directory")
        current = os.stat(parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (parent_info.st_dev, parent_info.st_ino):
            raise OSError("session directory was substituted")
        fd = os.open(
            name,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CommentMutationError("unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _read_regular_bytes_nofollow(path: Path) -> bytes:
    """Read one existing regular file without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def canonical_comment_summary_revision(counts: dict[str, int]) -> str:
    """Digest the canonical public CommentSummaryV1 fields."""
    revision_input = json.dumps(
        {"schema": 1, "semantics": "active-comment-row", "counts": counts},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(revision_input.encode("utf-8")).hexdigest()


@contextmanager
def _existing_directive_lock(path: Path) -> Iterator[None]:
    """Acquire an existing writer lock without creating or following it."""
    import fcntl

    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular directive lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _require_safe_regular_destination(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CommentMutationError("unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CommentMutationError("unavailable")


@contextmanager
def _pinned_receipt_directory(path: Path, *, create: bool = False,
                              missing_ok: bool = False) -> Iterator[int | None]:
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    parent_fd = -1
    fd = -1
    try:
        if path.parent == Path("/proc/self/fd") and path.name.isdecimal():
            fd = os.open(os.fspath(path) + "/", flags)
            yield fd
            return
        # The session directory is the trusted ancestor.  Pin it before either
        # creating or opening the private receipt directory so a concurrent
        # pathname substitution cannot redirect receipt I/O elsewhere.
        parent_fd = os.open(path.parent, flags)
        if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            raise OSError("not a directory")
        if create:
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        try:
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if missing_ok and not create:
                yield None
                return
            raise
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("not a directory")
        yield fd
    except (OSError, UnicodeError) as exc:
        raise CommentMutationError("unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


@contextmanager
def _pinned_receipt_parent(
        path: Path) -> Iterator[tuple[int, Callable[[bool], int | None]]]:
    """Pin the trusted session parent and open/create its receipt child from it."""
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    parent_fd = -1
    child_fd = -1
    try:
        if path.parent == Path("/proc/self/fd") and path.name.isdecimal():
            parent_fd = os.open(os.fspath(path) + "/", flags)

            def open_root(_create: bool = False) -> int | None:
                return parent_fd

            yield parent_fd, open_root
            return
        parent_fd = os.open(path.parent, flags)
        if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            raise OSError("not a directory")

        def open_child(create: bool = False) -> int | None:
            nonlocal child_fd
            if child_fd >= 0:
                return child_fd
            if create:
                try:
                    os.mkdir(path.name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            try:
                child_fd = os.open(path.name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                raise OSError("not a directory")
            return child_fd

        yield parent_fd, open_child
    except (OSError, UnicodeError) as exc:
        raise CommentMutationError("unavailable") from exc
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _read_receipt_at(directory_fd: int, name: str) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CommentMutationError("unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _write_receipt_at(directory_fd: int, name: str, value: Any) -> None:
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = -1
    try:
        fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(json.dumps(value, separators=(",", ":")).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise CommentMutationError("unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass

# ---------------------------------------------------------------------------
# Cross-platform advisory file lock for append-only JSONL writes.
# ---------------------------------------------------------------------------

try:  # POSIX
    import fcntl as _fcntl

    def _lock_handle(fh: Any) -> None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)

    def _unlock_handle(fh: Any) -> None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)

    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - Windows
    try:
        import msvcrt as _msvcrt

        def _lock_handle(fh: Any) -> None:
            _msvcrt.locking(fh.fileno(), _msvcrt.LK_LOCK, 1)

        def _unlock_handle(fh: Any) -> None:
            try:
                _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass

        _HAVE_FLOCK = True
    except ImportError:  # pragma: no cover
        _HAVE_FLOCK = False

        def _lock_handle(fh: Any) -> None:
            pass

        def _unlock_handle(fh: Any) -> None:
            pass


# ---------------------------------------------------------------------------
# rev + ids
# ---------------------------------------------------------------------------

def rev_of(raw: str | bytes) -> str:
    """Per-doc content hash ``sha1(raw)[:12]`` (§9.3 / D3).

    Used for live-update coherence and to detect the one real collision case
    (a concurrent on-disk edit while the agent writes, §9.4). The agent is the
    sole writer of ``.md`` files through the studio, so this is mainly to keep
    live updates coherent, not to merge concurrent edits.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


# ULID-style sortable id: 10 base32-ish chars of ms timestamp + 16 random.
# Monotonic enough for tail/sort within a session and cheap (no extra dep).
_TS_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32_ms(ms: int) -> str:
    out = []
    for _ in range(10):
        ms, rem = divmod(ms, 32)
        out.append(_TS_ALPHABET[rem])
    return "".join(reversed(out))


def new_id() -> str:
    """A sortable, unique id (ULID-style) for events and comments (§10.5)."""
    ts = _b32_ms(int(time.time() * 1000))
    rand = secrets.token_hex(8).upper()
    return f"{ts}{rand}"


def _normalize_comment_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a comment record for readers (archive-track split).

    Earlier records stored archive as a ``state == "archived"`` value. The
    current model keeps ``archived`` as a separate boolean and preserves the
    underlying lifecycle state (open/resolved/dismissed). On read, we map
    any older ``state == "archived"`` to ``archived=True, state="open"``
    (best-effort: the prior state was most often open) and default
    ``archived`` to ``False`` for new records that lack the field.

    Also ensures ``updated_at`` falls back to ``ts`` for older rows that
    predate the separate updated_at field.

    Also auto-derives ``request_summary`` from ``body`` when not
    set. The studio's collapsed preview shows BOTH a short of what was
    asked (this field) AND a short of what the agent did (the explicit
    ``summary`` field set at resolve time). The ask-short is computed
    once on first read and cached on the record so it is stable across
    reads. Agents can override it explicitly via
    ``okf comment-claim --summary`` (which writes ``request_summary``,
    NOT ``summary`` — the latter is reserved for the resolve-time
    "done" tag).
    """
    if not isinstance(row, dict):
        return row
    out = dict(row)
    if out.get("state") == "archived":
        out["state"] = "open"
        out["archived"] = True
    out.setdefault("archived", False)
    # updated_at fallback: older rows use ts for both creation and modification.
    if not out.get("updated_at"):
        out["updated_at"] = out.get("ts")
    # request_summary: short version of the comment body (the "ask").
    # Auto-derived once on first read so the collapsed preview can show a
    # useful short even when the agent never set one explicitly. The
    # heuristic is intentionally simple: first sentence boundary or first
    # 100 chars, whichever is shorter. Trailing whitespace + ellipsis.
    if not out.get("request_summary"):
        body = (out.get("body") or "").strip()
        if body:
            # Split on sentence-ending punctuation or newline; take the
            # first non-empty chunk.
            import re as _re
            parts = _re.split(r"[.!?\n]", body, maxsplit=1)
            first = (parts[0] if parts else body).strip()
            if not first:
                first = body
            if len(first) > 100:
                first = first[:100].rstrip() + "\u2026"
            out["request_summary"] = first
        else:
            out["request_summary"] = ""
    return out


def _now_iso() -> str:
    """UTC instant in ISO-8601 with milliseconds + ``Z`` (datetime skill).

    Single clock read (F14): ``time.gmtime`` and the millisecond fraction both
    derive from ONE ``time.time()`` so a ms boundary crossing cannot make the
    ``.mmm`` field inconsistent with the seconds field (e.g. ``...:59.000``
    when the two reads straddled a whole-second rollover).
    """
    now = time.time()
    t = time.gmtime(now)
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + f".{int((now * 1000) % 1000):03d}Z"


# ---------------------------------------------------------------------------
# Safe path-component validation (§9.5 path containment invariant)
# ---------------------------------------------------------------------------

# Allow the same characters as a concept-id segment (paths.validate_segment)
# plus colon (group ids may carry ULID-style suffixes) — but reject any path
# separator, leading dot, or percent-encoding that could escape a directory.
_SAFE_PATH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


def _assert_safe_path_token(name: str, value: str) -> None:
    """Reject path-traversal-shaped values before they reach filesystem ops.

    The studio writes undo snapshots under ``history/<hex>/<rev>.md`` and
    group manifests under ``history/_groups/<group_id>/manifest.json``. The
    ``rev`` and ``group_id`` values come from HTTP/CLI callers, so they must
    pass the same containment guard as ``concept_id`` (§9.5: "path
    containment on every path-like id"). Hex-encoded ids and content-hash
    revs already pass; this guards against a caller that bypasses those
    encodings (defense-in-depth, §15.5).
    """
    if not isinstance(value, str) or not _SAFE_PATH_TOKEN_RE.match(value):
        raise ValueError(f"unsafe {name} (rejected by §9.5 path containment): {value!r}")


# Activity actions that change the graph structure (§7.2 ``graph`` event).
# After a successful write of one of these, the studio also emits a
# ``graph`` event so the graph view invalidates its cached layout (P1-6).
_GRAPH_ACTIONS: frozenset[str] = frozenset({
    "add_link", "remove_link",
    "add_relation", "remove_relation",
    "add_entity", "remove_entity",
    "write_concept", "remove_concept",
    "repair",
})


# ---------------------------------------------------------------------------
# §12.3 / §15.5: per-kind args schema for the public /__apply HTTP surface.
# ---------------------------------------------------------------------------
# Each entry declares the (required, optional) args for one UpdateOp ``kind``
# the studio's /__apply endpoint accepts. Required entries MUST be present
# and well-typed (the ``_AnyType`` sentinel means "any
# non-None JSON value"); optional entries are checked when supplied, without
# coercion or default injection. Null is accepted only by a nullable type.
# Any key NOT in (required ∪ optional) is rejected with 400 so the public
# HTTP surface is fail-closed — the library handlers (``update._h_*``) stay
# lenient for in-process / CLI callers that may carry extra context (e.g.
# ``allow_forward_reference`` is a CLI-only knob the HTTP surface also
# allows so existing programmatic agents don't break).
#
# The schemas intentionally match the underlying handler signatures in
# ``update._HANDLERS`` (target existence is the handler's call; we only
# validate arg shape here). ``AnyType`` means "required, any JSON type".
class _AnyType:
    """Sentinel type for "required, any JSON type" schema entries."""

    @classmethod
    def _check(cls, value: Any) -> bool:
        return True


_APPLY_KIND_SCHEMAS: dict[str, dict[str, dict[str, type]]] = {
    "add_link": {
        "required": {"target_concept_id": str},
        "optional": {
            "label": str,
            "anchor_text": str,
            "section": (str, type(None)),
            "allow_forward_reference": bool,
        },
    },
    "add_relation": {
        "required": {"target_concept_id": str, "relation_type": str},
        "optional": {"detail": str},
    },
    "add_entity": {
        "required": {"label": str},
        "optional": {"kind": str, "entity_id": str, "aliases": list},
    },
    "set_frontmatter": {
        # ``value`` may be any JSON type (str/int/float/bool/list/dict).
        "required": {"key": str, "value": _AnyType},
        "optional": {},
    },
    "append_body_section": {
        "required": {"heading": str, "body": str},
        "optional": {},
    },
    "update_section": {
        "required": {"heading": str, "body": str},
        "optional": {"mode": str, "create_if_missing": bool},
    },
    "replace_text": {
        "required": {"old": str, "new": str},
        "optional": {"all": bool},
    },
    "set_tag": {
        "required": {"tag": str},
        "optional": {},
    },
    "add_tag": {
        "required": {"tag": str},
        "optional": {},
    },
    "remove_link": {
        "required": {"target_concept_id": str},
        "optional": {},
    },
}


def validate_apply_args(kind: str, args: dict) -> tuple[bool, str]:
    """Fail-closed validation of /__apply args per ``_APPLY_KIND_SCHEMAS``.

    Returns ``(True, "")`` when ``args`` satisfies the schema for ``kind``;
    otherwise ``(False, "<human-readable reason>")``. Reasons use stable
    machine prefixes (``unknown_kind:``, ``missing_arg:``, ``bad_type:``,
    ``unknown_arg:``) so callers can classify responses.

    The library handlers in :mod:`okf_loom.update` ALSO validate per-kind
    args (raising ``KeyError`` → ``bad_args:<key>``); this validator runs
    earlier so the HTTP surface returns 400 (not a 200 with ``applied:
    False``) on bad input, and so unknown args are rejected even when the
    handler would silently ignore them.
    """
    if not isinstance(args, dict):
        return False, "bad_type:args must be a JSON object"
    schema = _APPLY_KIND_SCHEMAS.get(kind)
    if schema is None:
        return False, f"unknown_kind:{kind}"
    required = schema.get("required", {})
    optional = schema.get("optional", {})
    allowed = set(required) | set(optional)
    for key, expected in {**required, **optional}.items():
        if key not in args:
            if key in required:
                return False, f"missing_arg:{kind}.{key}"
            continue
        value = args[key]
        if expected is _AnyType:
            if value is None:
                return False, f"bad_type:{kind}.{key} must not be null"
            continue
        if not isinstance(value, expected):
            return False, (
                f"bad_type:{kind}.{key} must be "
                f"{' or '.join(t.__name__ for t in expected) if isinstance(expected, tuple) else expected.__name__}"
            )
    for key in args:
        if key not in allowed:
            return False, f"unknown_arg:{kind}.{key}"
    return True, ""




# ---------------------------------------------------------------------------
# EventBus — thread-safe fan-out for SSE clients
# ---------------------------------------------------------------------------

class EventBus:
    """Thread-safe pub/sub for live push (§7.1).

    Each subscriber owns a bounded :class:`queue.Queue`. :meth:`publish`
    fans an event out to every queue with ``put_nowait``; a full queue (slow
    client) gets the event dropped and a single ``resync`` sentinel enqueued
    instead, so a stalled browser never blocks the watcher or other clients.
    """

    def __init__(self, *, max_queue: int = 64) -> None:
        self._max_queue = max_queue
        self._subs: list[queue.Queue] = []
        # Callback subscribers (§17 in-process embedding API). Each entry is
        # (callback, stop_event). A daemon thread per callback drains its own
        # Queue and invokes the callback per event.
        self._cb_subs: list[_CallbackSub] = []
        self._lock = threading.Lock()
        self._closed = False

    def subscribe(self, callback: Callable[[dict[str, Any]], None] | None = None
                  ) -> "queue.Queue | _CallbackSub":
        """Register a subscriber. Returns the subscriber handle.

        - Without a callback (the SSE-client default): returns a bounded
          :class:`queue.Queue` the caller drains itself. ``publish`` pushes
          events into it (drop+resync on full).
        - With a ``callback`` (§17 in-process embedding API for a harness
          running the server in a daemon thread): returns a
          :class:`_CallbackSub` whose own daemon thread invokes ``callback``
          per event. :meth:`unsubscribe` stops the thread.

        A slow callback is treated like a slow SSE client: pending events
        are drained and replaced with one ``resync`` sentinel, so the
        callback is never blocked indefinitely and the publish path never
        stalls on a stuck subscriber (§7.1 / §13.8).
        """
        if callback is None:
            q: queue.Queue = queue.Queue(maxsize=self._max_queue)
            with self._lock:
                if self._closed:
                    q.put_nowait({"__stop__": True})
                    return q
                self._subs.append(q)
            return q
        sub = _CallbackSub(callback=callback, max_queue=self._max_queue)
        with self._lock:
            if self._closed:
                sub.stop()
                return sub
            self._cb_subs.append(sub)
        sub.start()
        return sub

    def unsubscribe(self, handle: "queue.Queue | _CallbackSub") -> None:
        # ARCH2-004 fix: snapshot under the lock, then release BEFORE calling
        # handle.stop() (which joins the worker thread). Calling stop() inside
        # self._lock deadlocks for ~2s if the callback re-enters the bus
        # (e.g. via publish()) — the worker thread blocks waiting for the
        # lock we're holding.
        with self._lock:
            if isinstance(handle, _CallbackSub):
                if handle in self._cb_subs:
                    self._cb_subs.remove(handle)
                needs_stop = True
            else:
                needs_stop = False
                if handle in self._subs:
                    self._subs.remove(handle)
        if needs_stop:
            handle.stop()

    def client_count(self) -> int:
        with self._lock:
            return len(self._subs) + len(self._cb_subs)

    def publish(self, event: dict[str, Any]) -> None:
        """Fan ``event`` out to every subscriber; drop+resync slow clients."""
        with self._lock:
            if self._closed:
                return
            subs = list(self._subs)
            cb_subs = list(self._cb_subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Slow client: drain stale deltas and enqueue ONE resync so
                # the client knows it fell behind and re-fetches current state.
                self._replace_with_resync(q)
        for cb_sub in cb_subs:
            cb_sub.deliver(event)

    def close(self) -> None:
        """Fence publication and stop every subscriber worker idempotently."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            queues = tuple(self._subs)
            callbacks = tuple(self._cb_subs)
            self._subs.clear()
            self._cb_subs.clear()
        for q in queues:
            try:
                q.put_nowait({"__stop__": True})
            except queue.Full:
                self._replace_with_stop(q)
        # Joining outside the bus lock allows callbacks to finish a final
        # re-entrant publish, which is fenced above rather than deadlocked.
        for callback in callbacks:
            callback.stop()

    @staticmethod
    def _replace_with_stop(q: queue.Queue) -> None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break
        q.put_nowait({"__stop__": True})

    @staticmethod
    def _replace_with_resync(q: queue.Queue) -> None:
        dropped = 0
        while True:
            try:
                q.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        try:
            q.put_nowait({"type": "resync"})
        except queue.Full:
            pass


class _CallbackSub:
    """In-process callback subscriber (§17 ``EventBus.subscribe(callback)``).

    A daemon worker thread drains a bounded :class:`queue.Queue` and invokes
    the callback once per event. A full queue is treated like a slow SSE
    client: pending events are dropped and replaced with one ``resync``
    sentinel (§7.1), so a stuck callback never blocks ``publish`` and never
    accumulates unbounded memory. ``stop()`` signals shutdown and joins.
    """

    def __init__(self, *, callback: Callable[[dict[str, Any]], None],
                 max_queue: int = 64) -> None:
        self._callback = callback
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="okf-eventbus-cb", daemon=True
        )
        self._thread.start()

    def deliver(self, event: dict[str, Any]) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            # Mirror EventBus slow-client policy: drop deltas + one resync.
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            try:
                self._q.put_nowait({"type": "resync"})
            except queue.Full:
                pass

    def stop(self) -> None:
        self._stop.set()
        # Push a sentinel so a blocking get returns immediately.
        try:
            self._q.put_nowait({"__stop__": True})
        except queue.Full:
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._q.put_nowait({"__stop__": True})
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if event.get("__stop__"):
                break
            try:
                self._callback(event)
            except Exception as e:
                # A buggy / malicious callback must never kill the worker
                # thread (§13.8 robustness — a dropped agent process degrades
                # gracefully; the studio stays usable). The exception is
                # swallowed by design so one bad subscriber cannot take the
                # event bus down. Surface it on stderr with a unique prefix
                # so it isn't silent — see docs/embedding-guide.md
                # §"EventBus.subscribe(callback) trust boundary" (P3-12 /
                # SEC2-007) for why this is a trust boundary and how to
                # escalate (e.g. redirect stderr to your log collector).
                try:
                    import traceback
                    print(
                        f"[okf-callback] subscriber raised "
                        f"{type(e).__name__}: {e}",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                except Exception:
                    # Last-resort: never let the logger itself kill the worker.
                    pass


# ---------------------------------------------------------------------------
# Session feeds (append-only JSONL)
# ---------------------------------------------------------------------------

def _repair_torn_active_tail(path: Path) -> None:
    """Durably truncate an unacknowledged partial (non-newline-terminated)
    suffix from the active JSONL tail so the next append never merges into it.

    A crash between ``os.write`` and the terminating ``\\n`` can leave the
    active file ending in a partial record.  If the next append blindly
    ``O_APPEND``s after those bytes, the fresh JSON object merges into the
    partial line, producing a phantom/corrupt row that sibling readers skip
    (active torn tail) but later rotation archives permanently.  This helper
    truncates ONLY the partial suffix back to the final complete line boundary
    (or zero when no complete line exists).

    Complete newline-terminated rows are never truncated: if one is corrupt
    the repair fails closed (raises ``ValueError``) exactly like the reader,
    rather than hiding the damage behind a torn-tail truncation.  Must be
    called inside the advisory lock so a concurrent writer cannot observe a
    half-repaired file.
    """
    if not path.is_file():
        return
    raw = path.read_bytes()
    if not raw or raw.endswith((b"\n", b"\r")):
        return  # clean tail — nothing to repair
    boundary = max(raw.rfind(b"\n"), raw.rfind(b"\r"))
    truncate_len = boundary + 1 if boundary >= 0 else 0
    # Fail closed on corrupt *complete* rows — only the unacknowledged
    # partial suffix is eligible for truncation, never a full line.
    for raw_line in raw[:truncate_len].splitlines(keepends=True):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"corrupt JSONL row in {path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"non-object JSONL row in {path}")
    fd = os.open(path, os.O_WRONLY, 0o600)
    try:
        os.ftruncate(fd, truncate_len)
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_jsonl(path: Path, obj: dict[str, Any], *, proc_lock: threading.Lock,
                  max_bytes: int | None = None, keep: int = 7,
                  before_prune: Callable[[], None] | None = None) -> None:
    """Append one JSON record to ``path`` under a cross-process advisory lock.

    One compact JSON object per line, terminated by ``\\n``. The in-process
    ``proc_lock`` orders same-process writers; the advisory file lock orders
    writers across processes (serve vs. a separate ``okf watch``). The final
    fallback (no fcntl/msvcrt) is a single ``O_APPEND`` write, which is atomic
    for small records on POSIX (§10.5).

    Before every append the active tail is repaired
    (:func:`_repair_torn_active_tail`): a crash-mid-append partial suffix is
    durably truncated back to the last complete line boundary so the fresh
    record never merges into a phantom line and a later rotation never
    archives the corruption.  Both directives and events funnel through this
    helper, so the guarantee is uniform.

    When ``max_bytes`` is set and the active file is at or above the cap, the
    file is rotated to ``<stem>-YYYYMMDD-NN.<ext>`` and a fresh active file
    starts (§10.5). The most recent ``keep`` rotated files are retained; the
    rest are pruned. Rotation runs under the same advisory lock so concurrent
    writers never split a record across files.
    """
    line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
    data = line.encode("utf-8")
    if keep < 0:
        raise ValueError("JSONL rotation keep must be non-negative")
    path.parent.mkdir(parents=True, exist_ok=True)
    with proc_lock:
        # Lock a stable sibling instead of the active JSONL itself. Locking the
        # active file and then renaming it leaves the fd attached to the archive;
        # the first post-rotation record would be appended to the rotated file.
        # The stable lock keeps the cross-process critical section while we
        # rotate first, then open the fresh active path for the actual append.
        lock_path = path.with_name(f".{path.name}.lock")
        lock_fh = open(lock_path, "a+b")
        try:
            if _HAVE_FLOCK:
                _lock_handle(lock_fh)
            try:
                # Repair a torn active tail before rotation/append so the
                # next record never merges into a partial predecessor and a
                # later rotation never archives the corruption.
                _repair_torn_active_tail(path)
                # §10.5 rotation: cap the active file before the append.
                # Runs INSIDE the advisory lock (QUA2-003) so a concurrent
                # writer cannot observe a half-rotated state or race on the
                # rename target.
                rotated: Path | None = None
                if max_bytes is not None and max_bytes > 0 and path.is_file() and path.stat().st_size >= max_bytes:
                    rotated = _rotate_jsonl(path, keep=keep, prune=False)
                existed = path.exists()
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("short JSONL append")
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                # A newly created/recreated active pathname is not durable until
                # the containing directory is synced.
                if not existed:
                    _fsync_directory(path.parent)
                # Pruning is post-commit housekeeping only: old generations are
                # retained until both rename and replacement append are durable.
                if rotated is not None:
                    if before_prune is not None:
                        before_prune()
                    _prune_jsonl_rotations(path, keep=keep)
            finally:
                if _HAVE_FLOCK:
                    _unlock_handle(lock_fh)
        finally:
            lock_fh.close()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rotation_sort_key(path: Path, stem: str, suffix: str) -> tuple[int, str]:
    match = re.fullmatch(rf"{re.escape(stem)}-(\d{{8}})-(\d+){re.escape(suffix)}", path.name)
    # The durable generation is authoritative.  The date is decorative only:
    # wall-clock regression must never make the newest generation look oldest.
    return (int(match.group(2)), match.group(1)) if match else (0, path.name)


def _prune_jsonl_rotations(active: Path, *, keep: int) -> None:
    stem, suffix, parent = active.stem, active.suffix, active.parent
    rotated = sorted(parent.glob(f"{stem}-*-*{suffix}"),
                     key=lambda value: _rotation_sort_key(value, stem, suffix))
    stale_rows = rotated if keep == 0 else rotated[:-keep]
    for stale in stale_rows:
        try:
            stale.unlink()
            _fsync_directory(parent)
        except OSError:
            # Retaining an extra durable generation is safe; pruning never
            # changes the acknowledged append outcome.
            continue


def _rotate_jsonl(active: Path, *, keep: int, prune: bool = True) -> Path | None:
    """Rename ``active`` to a timestamped sibling and prune old rotations.

    Returns the rotated path (or ``None`` if rotation did not occur — e.g.
    the active file does not exist). Called inside the advisory lock from
    :func:`_append_jsonl`. The naming scheme is ``<stem>-YYYYMMDD-NN.<ext>``
    so lexicographic sort == chronological order within a day (§10.5).
    """
    if not active.is_file():
        return None
    stem = active.stem
    suffix = active.suffix
    parent = active.parent
    day = time.strftime("%Y%m%d", time.gmtime())
    counter_path = parent / f".{active.name}.rotation-seq"
    persisted = 0
    if counter_path.is_file():
        try:
            persisted = int(counter_path.read_text(encoding="ascii"))
        except (OSError, ValueError) as exc:
            raise OSError("invalid JSONL rotation sequence") from exc
    observed = max((number for number, _date in
                    (_rotation_sort_key(path, stem, suffix)
                     for path in parent.glob(f"{stem}-*-*{suffix}"))), default=0)
    nn = max(persisted, observed) + 1
    durable_atomic_write_text(counter_path, str(nn), encoding="ascii")
    candidate = parent / f"{stem}-{day}-{nn:012d}{suffix}"
    if candidate.exists():
        raise OSError("JSONL rotation generation collision")
    os.rename(active, candidate)
    _fsync_directory(parent)
    if prune:
        _prune_jsonl_rotations(active, keep=keep)
    return candidate


def _read_jsonl(path: Path, *, active: bool = False,
                fail_on_torn: bool = False) -> Iterator[dict[str, Any]]:
    try:
        raw = _read_regular_bytes_nofollow(path)
    except FileNotFoundError:
        return
    lines = raw.splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        complete = raw_line.endswith((b"\n", b"\r"))
        stripped = raw_line.strip()
        if not stripped:
            continue
        if active and index == len(lines) - 1 and not complete:
            if fail_on_torn:
                raise UncertainJSONLTail(f"uncertain final JSONL row in {path}")
            return
        try:
            value = json.loads(stripped.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"corrupt JSONL row in {path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"non-object JSONL row in {path}")
        yield value


def _read_jsonl_with_rotations(active: Path, *, fail_on_torn: bool = False) -> Iterator[dict[str, Any]]:
    """Yield events across rotated + active JSONL files in oldest-first order.

    Used by :meth:`Studio.read_events` and :meth:`Studio.list_comments` so a
    caller does not need to know about rotation. Rotated files match the
    pattern ``<stem>-YYYYMMDD-NN.<ext>`` and sort lexicographically into
    chronological order within a day; the active file is read last.
    """
    stem = active.stem
    suffix = active.suffix
    parent = active.parent
    rotated = sorted(parent.glob(f"{stem}-*-*{suffix}"),
                     key=lambda value: _rotation_sort_key(value, stem, suffix))
    for r in rotated:
        yield from _read_jsonl(r)
    yield from _read_jsonl(active, active=True, fail_on_torn=fail_on_torn)


def read_event_snapshot(
    session_dir: str | Path, *, limit: int = 500,
) -> list[dict[str, Any]] | None:
    """Read a newest-first event snapshot without creating or recovering state.

    A missing authority is distinguishable from an authoritative empty file.
    The existing sidecar lock is opened no-follow and never created.
    """
    session = Path(session_dir)
    events = session / "events.jsonl"
    lock = session / "..sidecar-transaction.okf-lock"
    rotated = sorted(session.glob("events-*-*.jsonl"))
    if not events.exists() and not rotated:
        return None
    if not lock.exists():
        raise CommentSummaryUnavailable("event snapshot authority unavailable")
    if events.is_symlink() or lock.is_symlink() or any(path.is_symlink() for path in rotated):
        raise CommentSummaryUnavailable("event snapshot authority unavailable")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(lock, flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular event lock")
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_SH)
        rows = list(_read_jsonl_with_rotations(events, fail_on_torn=True))
        rows.reverse()
        return rows[:limit]
    except (OSError, ValueError, UncertainJSONLTail) as exc:
        raise CommentSummaryUnavailable("event snapshot authority unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def read_comment_summary(
    session_dir: str | Path,
    *,
    graph_concepts: set[str] | frozenset[str],
) -> CommentSummaryV1:
    """Read the locked V2 latest-row authority into a sanitized aggregate.

    The checkpoint is read first, followed by rotated JSONL generations and
    the active JSONL file, so assignment into ``latest`` implements durable
    last-write-wins order.  This reader never repairs, checkpoints, rotates,
    or otherwise mutates comment state.  A malformed or unreadable authority
    raises :class:`CommentSummaryUnavailable`; it is never represented as an
    empty/zero summary.
    """
    if (not isinstance(graph_concepts, (set, frozenset))
            or any(not isinstance(value, str) or not value for value in graph_concepts)):
        raise ValueError("graph_concepts must be a set of non-empty concept ids")
    latest: dict[str, dict[str, Any]] = {}
    try:
        with DirectiveStore(session_dir).reader() as transaction:
            rows = transaction.rows(fail_on_torn=True, migrate=False)
            for row in rows:
                comment_id = row.get("id")
                concept = row.get("concept")
                state = row.get("state")
                archived = row.get("archived", False)
                if (not isinstance(comment_id, str) or not comment_id
                        or not isinstance(concept, str) or not concept
                        or state not in {"open", "claimed", "resolved", "dismissed", "archived"}
                        or type(archived) is not bool):
                    raise ValueError("corrupt directive row")
                latest[comment_id] = row
    except (OSError, ValueError, DirectiveStoreError) as exc:
        raise CommentSummaryUnavailable("comment summary authority unavailable") from exc

    counts: dict[str, int] = {}
    for row in latest.values():
        # Legacy archive rows predate the separate ``archived`` boolean.
        legacy_archived = row["state"] == "archived"
        if legacy_archived or row.get("archived", False):
            continue
        if row["state"] not in {"open", "claimed"}:
            continue
        concept = row["concept"]
        if concept not in graph_concepts:
            continue
        counts[concept] = counts.get(concept, 0) + 1
    counts = dict(sorted(counts.items()))
    return {
        "schema": 1,
        "semantics": "active-comment-row",
        "counts": counts,
        "summary_revision": canonical_comment_summary_revision(counts),
    }


def _event_dedup_key(event: dict[str, Any]) -> str | None:
    """Unique per-append key for cross-process broadcast dedup.

    ``event_id`` is the right identity: :meth:`Studio.append_event` stamps
    a fresh ULID on EVERY append, unconditionally. Neither of the other
    candidates is unique:

    * ``id`` — comment lifecycle events reuse the COMMENT id as the event
      id (the SSE payload contract; the client's upsertComment keys on
      it), so an id collides across a comment's open→claimed→resolved
      transitions.
    * ``seq`` — a per-instance in-memory counter initialised from the
      feed at construction, so two processes whose Studio instances both
      predate the other's writes stamp colliding seqs.

    The ``id|seq`` composite fallback covers rows written before
    ``event_id`` existed (their combination is unique in all but
    pathological multi-writer interleavings).
    """
    eid = event.get("event_id")
    if eid:
        return f"eid:{eid}"
    rid = event.get("id")
    if rid is None:
        return None
    return f"id:{rid}|seq:{event.get('seq')}"


# ---------------------------------------------------------------------------
# Studio — session state + the comment / presence / undo / activity API
# ---------------------------------------------------------------------------


def _configured_session_root(bundle_root: str | Path, session_rel: str) -> Path | SessionAuthority:
    """Resolve the optional process-owned mutable Studio authority."""
    root = Path(bundle_root).resolve(strict=True)
    raw = os.environ.get("OKF_LOOM_SESSION_ROOT")
    if raw is None:
        return root / session_rel
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("OKF_LOOM_SESSION_ROOT must be an absolute directory")
    candidate = Path(os.path.normpath(os.fspath(candidate)))
    immutable_raw = os.environ.get("OKF_LOOM_IMMUTABLE_ROOT")
    try:
        immutable = Path(immutable_raw).resolve(strict=True) if immutable_raw else root
    except OSError as exc:
        raise ValueError("OKF_LOOM_IMMUTABLE_ROOT is unavailable") from exc

    # Lexical rejection is deliberately before any filesystem mutation.  A
    # protected missing leaf must not cause its first parent to be created.
    for protected in (root, immutable):
        if candidate == protected or candidate.is_relative_to(protected):
            raise ValueError("OKF_LOOM_SESSION_ROOT must be outside immutable bundle/runtime roots")

    try:
        approved_raw = os.environ.get("OKF_LOOM_SESSION_TRUSTED_ROOT")
        approved = Path(approved_raw) if approved_raw else None
        authority = resolve_session_authority(candidate, approved_root=approved)
        for protected in (root, immutable):
            if authority.display_path == protected or authority.display_path.is_relative_to(protected):
                authority.close()
                raise OSError("session root is inside a protected root")
        return authority
    except (OSError, ValueError) as exc:
        raise ValueError(
            "OKF_LOOM_SESSION_ROOT must be a private caller-owned directory"
        ) from exc


@dataclass
class WriteResult:
    """Outcome of :meth:`Studio.save_concept` (the single write funnel).

    A successful write: ``ok=True``, ``rev`` is the new content-hash,
    ``activity_id`` is the appended activity event id (for change-list
    Undo lookup), and ``snap_rev`` is the prior-bytes rev (or ``None``
    when the write was not undoable / created a new file with empty prior).

    A §9.3/§9.4 collision: ``ok=False, conflict=True``, ``current_rev`` is
    the on-disk rev that mismatched ``expected_rev``; the write did NOT
    happen. The caller surfaces this (the studio conflict UX, §9.4) — the
    studio never silent-clobbers.

    Other failures (forbidden_paths, max_edits_per_action, malformed id):
    ``ok=False, conflict=False, error=<msg>``.
    """
    ok: bool
    conflict: bool = False
    rev: str | None = None
    expected_rev: str | None = None
    current_rev: str | None = None
    concept_id: str | None = None
    activity_id: str | None = None
    snap_rev: str | None = None
    # Internal transaction hand-off. Deferred writes return the exact bytes
    # observed by the CAS check so their owner can publish undo history only
    # after every physical write has committed.
    prior_bytes: bytes | None = field(default=None, repr=False)
    error: str | None = None

@dataclass
class Studio:
    """Session state for one running server (§6 ``server.state``).

    Holds the :class:`EventBus`, the bundle + session paths, the in-process
    append lock, and a monotonic ``seq`` counter. The HTTP layer, the watcher,
    and embedding harnesses call the methods here; the actual ``.md`` writes
    happen through :meth:`save_concept` — the **single internal write path**
    (§17) that the CLI mutators, the HTTP `/__apply`/`/__undo` endpoints, and
    embedding harnesses all share. ``save_concept`` validates, atomically
    writes, snapshots for undo, records an attributed activity event, marks
    the rev as logged so the watcher doesn't double-log it, and broadcasts
    `changed` (+ `graph` for graph-affecting ops) over SSE.
    """

    bundle_root: Path
    session_dir: Path
    session_authority: SessionAuthority | None = field(default=None, repr=False)
    _bundle_authority: SessionAuthority | None = field(default=None, repr=False)
    _bundle_session_relative: str | None = field(default=None, repr=False)
    _session_descendants: dict[str, SessionAuthority] = field(
        default_factory=dict, init=False, repr=False)
    bus: EventBus = field(default_factory=EventBus)
    log_edits: bool = True
    bundle_name: str = ""
    # §10.5 events.jsonl rotation caps (None = no rotation; spec default 8 MiB
    # / 7 rotated files, set via :meth:`for_bundle` config).
    events_max_bytes: int | None = 8 * 1024 * 1024
    events_keep: int = 7
    TOKEN_MAX_BYTES: ClassVar[int] = 128
    SERVER_STATE_MAX_BYTES: ClassVar[int] = 64 * 1024
    # Mutable session counters (guarded by _lock). On startup these are
    # seeded from the existing events.jsonl so a new process joining an
    # existing session continues the monotonic sequence (P1-2
    # cross-process dedup).
    seq: int = 0
    bundle_rev: int = 0
    presence: dict[str, Any] = field(default_factory=lambda: {"actor": "agent", "state": "idle"})
    # INTENT2-008: presence + claim staleness TTLs (§13.8 robust liveness).
    # A dropped agent process degrades gracefully: presence → idle after
    # presence_ttl_s (default 300s); stale claims → open after
    # claim_ttl_s (default 600s). The sweep runs on watcher tick.
    _presence_ts: float = field(default_factory=lambda: time.time(), repr=False)
    presence_ttl_s: float = 300.0
    claim_ttl_s: float = 600.0
    # P1-4 / QUA2-011: RLock (re-entrant) so idempotency's lookup→post_comment→
    # store critical section can hold _lock across post_comment (which itself
    # takes _lock for its append). A plain Lock deadlocks here.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _session_init_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _session_initialized: bool = field(default=False, repr=False)
    # Lock order for presence is publish -> cache (_lock) -> pinned authority.
    # Cache is released before EventBus or sidecar append work. The outer
    # publication lock preserves commit/revision publication order without
    # ever holding the cache lock while acquiring the sidecar lock.
    _presence_publish_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # F3 (§10.5 disk-event dedup): concept_id → most recent content-hash rev a
    # mutator/undo already logged. The watcher's disk-origin ``emit_change``
    # skips an id whose current on-disk rev matches this value, so a write
    # that already logged itself is NOT re-logged when the watcher notices the
    # mtime change (no double-logging). Guarded by ``_lock`` AND persisted to
    # ``.last-logged.json`` so a SEPARATE process (serve + a concurrent
    # ``okf watch``) shares the same dedup state (P1-2 cross-process drift).
    _last_logged_rev: dict[str, str] = field(default_factory=dict, repr=False)
    # Cross-process SSE tail (INTENT2-001 / Bundle H finding): bounded set of
    # event ids THIS process published in-process via ``append_event`` so the
    # watcher's tail-and-rebroadcast path doesn't double-broadcast them.
    # Bounded at 256 entries (ring); entries are evicted oldest-first.
    _recently_published_events: set = field(default_factory=set, repr=False)
    _recently_published_order: list = field(default_factory=list, repr=False)
    _event_tail_cursor_key: str | None = field(default=None, repr=False)
    _event_tail_seen_keys: set[str] = field(default_factory=set, repr=False)
    # §12.3 opt-in constraints (default empty = fully free agent, §1.1/D8).
    constraints: dict[str, Any] = field(default_factory=dict)

    # --- lifecycle -------------------------------------------------------

    @classmethod
    def for_bundle(cls, bundle_root: str | Path, *, session_rel: str = ".okf-loom/session",
                   log_edits: bool = True, bundle_name: str = "",
                   max_queue: int = 64,
                   events_max_bytes: int | None = 8 * 1024 * 1024,
                   events_keep: int = 7,
                   constraints: dict[str, Any] | None = None,
                   session_authority: SessionAuthority | None = None) -> "Studio":
        root = Path(bundle_root)
        session_path = Path(session_rel)
        bundle_relative: str | None = None
        bundle_authority: SessionAuthority | None = None
        if session_authority is None and not session_path.is_absolute():
            bundle_relative = str(session_path)
            try:
                bundle_authority = resolve_bundle_root_authority(root)
                session_authority = bundle_authority.subdirectory(
                    bundle_relative, create=False)
            except FileNotFoundError:
                pass
            except BaseException:
                if bundle_authority is not None:
                    bundle_authority.close()
                raise
        studio = cls(
            bundle_root=root,
            session_dir=(session_authority.display_path if session_authority is not None else
                         (session_path if session_path.is_absolute() else root / session_path)),
            session_authority=session_authority,
            _bundle_authority=bundle_authority,
            _bundle_session_relative=bundle_relative,
            bus=EventBus(max_queue=max_queue),
            log_edits=log_edits,
            bundle_name=bundle_name,
            events_max_bytes=events_max_bytes,
            events_keep=events_keep,
            constraints=dict(constraints) if constraints else {},
        )
        # P1-2: resume monotonic seq/bundle_rev + cross-process dedup map
        # from any existing session state so a new process joining an
        # existing session does not reset the counters or re-log writes a
        # sibling process already logged.
        try:
            studio._resume_from_existing_session()
            studio._recover_comment_notifications()
            return studio
        except BaseException:
            studio.close()
            raise

    @classmethod
    def for_configured_bundle(cls, bundle_root: str | Path, *, bundle_name: str = "",
                              max_queue: int = 64) -> "Studio":
        """Create a Studio using ``studio:`` config for the bundle.

        ``serve`` already honored ``studio.session_dir``; CLI and watch paths
        must use the same resolver so session state has one authoritative
        location instead of splitting token/comment/presence files between the
        configured directory and the default ``.okf-loom/session``.
        """
        from .config import OkfConfig

        root = Path(bundle_root)
        sc = OkfConfig.load(root).studio
        session_root = _configured_session_root(root, sc.session_path)
        authority = session_root if isinstance(session_root, SessionAuthority) else None
        return cls.for_bundle(
            root,
            session_rel=(str(authority.path) if authority is not None else sc.session_path),
            log_edits=sc.log_edits,
            bundle_name=bundle_name,
            max_queue=max_queue,
            events_max_bytes=sc.events_max_bytes,
            events_keep=sc.events_keep,
            constraints=dict(sc.constraints) if sc.constraints else None,
            session_authority=authority,
        )

    def close(self) -> None:
        """Release every descriptor owned by an external session authority."""
        self.bus.close()
        for authority in self._session_descendants.values():
            authority.close()
        self._session_descendants.clear()
        if self.session_authority is not None:
            self.session_authority.close()
            self.session_authority = None
        if self._bundle_authority is not None:
            self._bundle_authority.close()
            self._bundle_authority = None

    def __enter__(self) -> "Studio":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - descriptor hygiene
        try:
            self.close()
        except Exception:
            pass

    def _session_directory(self, relative: str, *, create: bool = True) -> Path:
        """Return a descriptor-pinned private session descendant."""
        if self.session_authority is None:
            if not create:
                raise FileNotFoundError(relative)
            self._pin_bundle_session(create=True)
        assert self.session_authority is not None
        authority = self._session_descendants.get(relative)
        if authority is None:
            authority = self.session_authority.subdirectory(relative, create=create)
            self._session_descendants[relative] = authority
        return Path(str(authority.path).rstrip("/"))

    @property
    def _session_io_dir(self) -> Path:
        """Descriptor-bound root used by all runtime session-state I/O."""
        if self.session_authority is None:
            self._pin_bundle_session(create=False)
        assert self.session_authority is not None
        return self.session_authority.path

    def _pin_bundle_session(self, *, create: bool) -> None:
        if self.session_authority is not None:
            return
        if self._bundle_session_relative is None:
            raise FileNotFoundError(self.session_dir)
        if self._bundle_authority is None:
            raise RuntimeError("bundle session authority is closed")
        self.session_authority = self._bundle_authority.subdirectory(
            self._bundle_session_relative, create=create)

    def _adopt_existing_session(self) -> bool:
        """Lazily pin state created after this Studio was constructed."""
        if self.session_authority is not None:
            return True
        try:
            self._pin_bundle_session(create=False)
        except FileNotFoundError:
            return False
        return True

    def _resume_from_existing_session(self) -> None:
        """Adopt the highest seq/rev + the dedup map from an existing session.

        Called once from :meth:`for_bundle`. If no events.jsonl exists, the
        counters stay at 0 and the dedup map stays empty (fresh session).
        """
        if self.session_authority is None:
            return
        try:
            max_seq = 0
            max_rev = 0
            rows = self.run_sidecar_transaction(
                lambda: list(_read_jsonl_with_rotations(self.events_path))
            )
            for ev in rows:
                seq = ev.get("seq")
                if isinstance(seq, int) and seq > max_seq:
                    max_seq = seq
                rev = ev.get("rev")
                if isinstance(rev, int) and rev > max_rev:
                    max_rev = rev
            if self.event_sequence_path.is_file():
                state = json.loads((self._session_io_dir / ".event-sequence.json").read_text(encoding="utf-8"))
                if (not isinstance(state, dict) or not isinstance(state.get("seq"), int)
                        or not isinstance(state.get("bundle_rev"), int)
                        or state["seq"] < 0 or state["bundle_rev"] < 0):
                    raise ValueError("invalid durable event sequence")
                max_seq = max(max_seq, state["seq"])
                max_rev = max(max_rev, state["bundle_rev"])
            with self._lock:
                self.seq = max_seq
                self.bundle_rev = max_rev
            # Restore the cross-process dedup map.
            self._load_last_logged()
        except OSError:
            pass

    @property
    def events_path(self) -> Path:
        return ((self._session_io_dir if self.session_authority is not None else
                 self.session_dir) / "events.jsonl")

    @property
    def event_sequence_path(self) -> Path:
        return self._session_io_dir / ".event-sequence.json"

    @property
    def directives_path(self) -> Path:
        return self._session_io_dir / "directives.jsonl"

    @property
    def directives_transaction_path(self) -> Path:
        return self._session_io_dir / ".directives-transaction"

    @property
    def directives_checkpoint_path(self) -> Path:
        return self._session_io_dir / "directives-checkpoint.json"

    DIRECTIVES_MAX_BYTES: int = 2 * 1024 * 1024
    DIRECTIVES_KEEP: int = 3

    @staticmethod
    def _read_directive_rows_locked(
            transaction: DirectiveTransaction, *, fail_on_torn: bool = False
    ) -> Iterator[dict[str, Any]]:
        """Read only through an explicit pinned directive capability."""
        yield from transaction.rows(fail_on_torn=fail_on_torn)

    @staticmethod
    def _directive_transaction_rows_locked(
            transaction: DirectiveTransaction, field: str, txid: Any
    ) -> list[dict[str, Any]]:
        return transaction.physical_rows(field, txid)

    @property
    def directive_transaction_results_dir(self) -> Path:
        return self._session_directory(".directive-transaction-results")

    def _transaction_result_path(self, txid: str) -> Path:
        if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", txid):
            raise CommentMutationError("unavailable")
        return self.directive_transaction_results_dir / f"{txid}.json"

    def _write_transaction_result_locked(self, record: dict[str, Any]) -> None:
        self._validate_transaction_result(record)
        path = self._transaction_result_path(record["transaction_id"])
        if path.is_file():
            if self._read_transaction_result_locked(record["transaction_id"]) != record:
                raise CommentMutationError("idempotency_conflict")
            return
        durable_atomic_write_text(path, json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    def _read_transaction_result_locked(self, txid: str) -> dict[str, Any] | None:
        path = self._transaction_result_path(txid)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommentMutationError("unavailable") from exc
        self._validate_transaction_result(value)
        return value

    @staticmethod
    def _validate_transaction_result(record: Any) -> None:
        if (not isinstance(record, dict)
                or record.get("operation") not in {"create", "reanchor"}
                or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", str(record.get("transaction_id", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("fingerprint", "")))
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", str(record.get("key", "")))
                or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", str(record.get("comment_id", "")))
                or not re.fullmatch(r"[!-~]{1,128}", str(record.get("row_token", "")))
                or not isinstance(record.get("status"), int)
                or isinstance(record.get("status"), bool)
                or not isinstance(record.get("response"), dict)
                or record["response"].get("ok") is not True):
            raise CommentMutationError("unavailable")
        comment = record["response"].get("comment")
        if (not isinstance(comment, dict) or comment.get("id") != record["comment_id"]
                or comment.get("row_token") != record["row_token"]):
            raise CommentMutationError("unavailable")

    def _checkpoint_row_transaction_result_locked(self, row: dict[str, Any]) -> None:
        create_txid = row.get("comment_create_transaction_id")
        if isinstance(create_txid, str):
            self._write_transaction_result_locked({
                "operation": "create", "transaction_id": create_txid,
                "fingerprint": row.get("comment_create_request_fingerprint"),
                "key": row.get("comment_create_idempotency_key"),
                "comment_id": row.get("id"), "row_token": row.get("row_token"),
                "status": 201, "response": {"ok": True, "comment": dict(row)},
            })
        reanchor_txid = row.get("reanchor_transaction_id")
        if isinstance(reanchor_txid, str):
            self._write_transaction_result_locked({
                "operation": "reanchor", "transaction_id": reanchor_txid,
                "fingerprint": row.get("reanchor_request_fingerprint"),
                "key": row.get("reanchor_idempotency_key"),
                "comment_id": row.get("id"), "row_token": row.get("row_token"),
                "status": 200,
                  "response": self._reanchor_response(
                      row, str(row.get("reanchor_concept_content_rev", ""))),
                  "concept": row.get("concept", ""),
                  "notification_event": self._comment_event(
                      row, transaction_id=reanchor_txid, operation="reanchor"),
              })

    def _append_directive_locked(
            self, transaction: DirectiveTransaction, row: dict[str, Any]) -> None:
        """One bounded persistence funnel for every full directive writer."""
        transaction.append(row, max_bytes=self.DIRECTIVES_MAX_BYTES,
                           keep=self.DIRECTIVES_KEEP)

    @property
    def presence_path(self) -> Path:
        # PresenceStore receives this compatibility path but performs every
        # actual operation through _run_presence_transaction's pinned dirfd.
        return self.session_dir / "presence.json"

    @property
    def token_path(self) -> Path:
        """Per-session CSRF token file (current spec §14).

        ``serve`` writes the per-session token here (0o600) so an external
        agent can read it via ``okf token <bundle>`` or the library, then
        drive ``/__apply`` / ``/__presence`` / ``/__comment`` over HTTP.
        """
        return self._session_io_dir / ".token"

    def read_token(self) -> str:
        """Read the session token while retaining its filesystem authority."""
        return self._read_session_text(".token", max_bytes=self.TOKEN_MAX_BYTES)

    def _read_session_text(self, relative: str, *, max_bytes: int) -> str:
        """Read one bounded regular session file through the live authority."""
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        fd = -1
        try:
            try:
                if self.session_authority is None:
                    raise FileNotFoundError(self.session_dir)
                fd = self.session_authority.open(relative, flags)
            except OSError as exc:
                if exc.errno in {errno.ENXIO, errno.ENODEV, errno.EOPNOTSUPP}:
                    raise OSError(
                        errno.EINVAL,
                        f"{relative} is not a supported regular file",
                    ) from exc
                raise
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(
                    errno.EINVAL,
                    f"{relative} is not a supported regular file",
                )
            if info.st_size > max_bytes:
                raise OSError(
                    errno.EFBIG,
                    f"{relative} exceeds the {max_bytes}-byte limit",
                )
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > max_bytes:
                raise OSError(
                    errno.EFBIG,
                    f"{relative} exceeds the {max_bytes}-byte limit",
                )
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OSError(
                    errno.EILSEQ, f"{relative} is not valid UTF-8") from exc
        finally:
            if fd >= 0:
                os.close(fd)

    def write_session_text(self, relative: str, value: str) -> None:
        """Atomically write one runtime leaf through pinned session authority."""
        self.ensure_session()
        assert self.session_authority is not None
        temporary = f".{relative.lstrip('.')}.{secrets.token_hex(8)}.tmp"
        fd = -1
        try:
            fd = self.session_authority.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            raw = value.encode("utf-8")
            offset = 0
            while offset < len(raw):
                offset += os.write(fd, raw[offset:])
            os.fsync(fd)
            os.close(fd)
            fd = -1
            self.session_authority.replace(temporary, relative)
            os.fsync(self.session_authority.fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.session_authority.fd)
            except FileNotFoundError:
                pass

    def unlink_session_file(self, relative: str, *, missing_ok: bool = False) -> None:
        """Unlink one runtime leaf through pinned session authority."""
        if self.session_authority is None:
            if missing_ok:
                return
            raise FileNotFoundError(relative)
        try:
            os.unlink(relative, dir_fd=self.session_authority.fd)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def read_server_state(self) -> str:
        """Read ``server.json`` without following a substituted leaf."""
        return self._read_session_text(
            "server.json", max_bytes=self.SERVER_STATE_MAX_BYTES)

    @property
    def server_state_path(self) -> Path:
        """Live-server state file (``server.json``).

        ``serve`` writes ``{host, port, pid, started, url, tunnel_url}``
        here at startup (and refreshes it when a tunnel attaches/detaches)
        so session-aware CLI verbs — ``okf tunnel``, and anything else that
        must reach the RUNNING server over HTTP rather than the session
        files — can find the port without scanning processes. Removed on
        clean shutdown; a stale file (crash) is detected by the connect
        failing.
        """
        return self._session_io_dir / "server.json"

    @property
    def last_logged_path(self) -> Path:
        return self._session_io_dir / ".last-logged.json"

    @property
    def history_dir(self) -> Path:
        return self._session_directory("history")

    # --- §9 idempotency cache (P1-11 / QUA idempotency-retry) -----------
    #
    # ``POST /__comment`` and ``POST /__undo`` accept an optional
    # ``Idempotency-Key`` header (RFC-style). Within a 10-minute window, a
    # re-POST with the same key + body returns the ORIGINAL response — no
    # duplicate directive, no double-apply. Records live in
    # ``.okf-loom/session/.idempotency/<key>.json`` and are written atomically
    # (tmp + rename) so a crash mid-write never produces a half record that
    # pins the key forever. The 10-minute TTL is intentionally short: it
    # covers the realistic network-retry window without becoming a permanent
    # duplicate-suppression table.
    IDEMPOTENCY_TTL_SECONDS: int = 600  # 10 minutes.
    REANCHOR_FINALIZED_TTL_SECONDS: int = 86_400
    REANCHOR_PREPARED_GRACE_SECONDS: int = 300
    REANCHOR_WAL_CAP: int = 10_000
    REANCHOR_TOMBSTONE_TTL_SECONDS: int = 7 * 86_400
    REANCHOR_TOMBSTONE_CAP: int = 10_000
    REANCHOR_FILTER_BITS: int = 262_144
    COMMENT_CREATE_WAL_TTL_SECONDS: int = 86_400
    COMMENT_CREATE_WAL_CAP: int = 10_000
    COMMENT_CREATE_PREPARED_GRACE_SECONDS: int = 300
    COMMENT_CREATE_TOMBSTONE_CAP: int = 10_000
    COMMENT_CREATE_TOMBSTONE_TTL_SECONDS: int = 7 * 86_400
    NOTIFICATION_RECEIPT_CAP: int = 10_000

    @property
    def idempotency_dir(self) -> Path:
        return self._session_directory(".idempotency")

    def _idempotency_path(self, key: str) -> Path:
        """Resolve the on-disk path for one idempotency record.

        The key is validated against the same segment regex as a concept-id
        segment (paths.validate_segment) plus colon + slash so common
        idempotency-key schemes (UUID v4 with dashes, ULIDs, ``<scope>:<id>``)
        are accepted without escaping. Path-traversal-shaped keys are
        rejected before they reach the path join (§9.5 path containment).
        """
        if not isinstance(key, str) or not key:
            raise ValueError("idempotency key must be a non-empty string")
        # Allow common idempotency-key characters: UUID dashes, colons,
        # underscores, dots. Reject path separators + leading dots.
        if not re.match(r"^[A-Za-z0-9._:-]{1,256}$", key) or key.startswith(".."):
            raise ValueError(f"unsafe idempotency key: {key!r}")
        return self.idempotency_dir / f"{key}.json"

    def idempotency_lookup(self, key: str, *, body: dict) -> dict | None:
        """Return the cached response for ``(key, body)`` if a fresh record
        exists, else ``None``.

        ``body`` is included in the cache key (via content-hash) so a key
        reused with DIFFERENT intent does not silently return the prior
        response (RFC 9110 idempotency-key semantics: same key + same
        intent = replay; same key + different intent = conflict, but we
        treat it as a fresh call to stay permissive).
        """
        try:
            path = self._idempotency_path(key)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        # TTL: drop records older than the window so a long-lived session
        # does not pin keys indefinitely.
        ts = record.get("ts")
        if not isinstance(ts, (int, float)) or time.time() - ts > self.IDEMPOTENCY_TTL_SECONDS:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        # Body fingerprint: same key + same body = replay.
        if record.get("body_fingerprint") != self._body_fingerprint(body):
            return None
        return record.get("response")

    def idempotency_store(self, key: str, *, body: dict, response: dict) -> None:
        """Atomically persist ``(key, body) → response`` for replay.

        SEC2-001: also opportunistically sweeps expired (TTL > 10 min) and
        over-cap (default 10000 files) cache entries so a buggy/authorized
        attacker can't exhaust inodes by spamming unique keys.
        """
        try:
            path = self._idempotency_path(key)
        except ValueError:
            return  # unsafe key: skip caching (the request itself still runs)
        self.idempotency_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "key": key,
            "ts": time.time(),
            "body_fingerprint": self._body_fingerprint(body),
            "response": response,
        }
        atomic_write_text(
            path,
            json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str),
        )
        # SEC2-001: opportunistic sweep — cheap because it only reads mtime +
        # only fires on store (write-rate, not read-rate). Best-effort; an
        # OSError here just means we tried to sweep during a concurrent
        # cleanup.
        try:
            self._idempotency_sweep()
        except OSError:
            pass

    # SEC2-001 caps. 10-min TTL matches the lookup window; the file-count
    # cap bounds inode usage by a buggy or malicious in-loopback client.
    _IDEMPOTENCY_TTL_S: float = 600.0
    _IDEMPOTENCY_MAX_FILES: int = 10_000

    def _idempotency_sweep(self) -> None:
        """Delete expired + over-cap idempotency cache files (best-effort)."""
        try:
            files = list(self.idempotency_dir.glob("*.json"))
        except OSError:
            return
        if not files:
            return
        now = time.time()
        # Pass 1: drop expired.
        for f in files:
            try:
                if (now - f.stat().st_mtime) > self._IDEMPOTENCY_TTL_S:
                    f.unlink(missing_ok=True)
            except OSError:
                pass
        # Pass 2: if still over cap, drop oldest by mtime.
        try:
            files = list(self.idempotency_dir.glob("*.json"))
        except OSError:
            return
        if len(files) <= self._IDEMPOTENCY_MAX_FILES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for stale in files[:len(files) - self._IDEMPOTENCY_MAX_FILES]:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _body_fingerprint(body: dict) -> str:
        """Stable content-hash of a request body for idempotency matching.

        Uses sha1 of the canonical JSON encoding so two semantically-
        identical bodies (key-order differences) fingerprint the same."""
        canon = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]


    def ensure_session(self) -> None:
        """Create the session directory tree (idempotent).

        Also drops a self-contained .gitignore inside the bundle's
        .okf-loom/ workdir so session state (incl. the auth token) can
        never be committed, whatever repo the bundle lives in.

        This startup/write boundary also initializes the directive authority's
        lock.  Read-only consumers deliberately never create authority, so a
        fresh server must establish the empty authority before serving graph
        summaries.
        """
        with self._session_init_lock:
            if self._session_initialized:
                return
            self._pin_bundle_session(create=True)
            directive_store = DirectiveStore(self._session_io_dir)
            # Event publication can be part of a directive mutation.  In that
            # case the current thread already owns the valid pinned authority;
            # attempting to initialize it again would violate the general
            # nested-transaction ban.  The ownership query grants no nested
            # access and fails closed if the session pathname was substituted.
            if directive_store.writer_owned_by_current_thread():
                # Initialization includes the supported legacy checkpoint
                # migration; writer ownership must not bypass that work.
                directive_store._initialize_owned_writer()
            else:
                with directive_store.initialize():
                    pass
            self._session_directory("history")
            from .io_utils import ensure_self_ignored
            ensure_self_ignored(self._session_io_dir)
            self._session_initialized = True

    def _next_seq(self) -> int:
        with self._lock:
            self.seq += 1
            return self.seq

    def _next_bundle_rev(self) -> int:
        with self._lock:
            self.bundle_rev += 1
            return self.bundle_rev

    def current_rev(self) -> int:
        with self._lock:
            return self.bundle_rev

    # --- cross-process dedup map (P1-2) ---------------------------------

    def _load_last_logged(self) -> None:
        """Load the cross-process dedup map from disk (best-effort)."""
        try:
            if self.last_logged_path.is_file():
                data = json.loads(self.last_logged_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    with self._lock:
                        # Merge: in-memory wins for entries we have already
                        # mutated this process (we don't lose our own writes).
                        merged = {str(k): str(v) for k, v in data.items()
                                  if isinstance(v, (str, int))}
                        merged.update(self._last_logged_rev)
                        self._last_logged_rev = merged
        except (OSError, json.JSONDecodeError):
            pass

    def _save_last_logged(self) -> None:
        """Persist the dedup map atomically (best-effort, advisory-locked).

        QUA2-006 fix: MERGE with the on-disk map rather than overwrite, so a
        sibling process's entries (e.g. a concurrent ``okf watch``) are not
        lost when this process writes its own. Without the merge, cross-
        process dedup drifts and disk events get double-logged.
        """
        try:
            self.ensure_session()
            # Read the current on-disk map (best-effort), merge our in-memory
            # entries on top, then atomically write the merged result. The
            # advisory flock on events.jsonl is NOT taken here — the file is
            # tiny and the merge window is short; atomic_write_text prevents
            # tearing. A truly atomic cross-process merge would need a dedicated
            # flock on .last-logged.json, but the loss-of-entries failure mode
            # is the one we're fixing, and the merge handles it.
            existing: dict[str, str] = {}
            try:
                if self.last_logged_path.is_file():
                    data = json.loads(self.last_logged_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        existing = {
                            str(k): str(v) for k, v in data.items()
                            if isinstance(v, (str, int))
                        }
            except (OSError, json.JSONDecodeError):
                pass
            with self._lock:
                merged = dict(existing)
                merged.update(self._last_logged_rev)
                snapshot = dict(merged)
            atomic_write_text(
                self.last_logged_path,
                json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False),
            )
        except OSError:
            pass

    # --- events feed (§10.5) --------------------------------------------

    @property
    def sidecar_lock_path(self) -> Path:
        return self._session_io_dir / ".sidecar-transaction"

    def run_sidecar_transaction(self, callback: Callable[[], Any]) -> Any:
        from .io_utils import path_lock
        with path_lock(self.sidecar_lock_path):
            return callback()

    def _run_presence_transaction(self, callback: Callable[[], Any]) -> Any:
        """Pin every directory and perform lock/authority I/O via dirfd."""
        import fcntl
        fds=[]; lock_fd=-1
        try:
            self.ensure_session()
            assert self.session_authority is not None
            current = os.dup(self.session_authority.fd)
            fds.append(current)
            lock_flags=os.O_RDWR|os.O_CREAT|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
            lock_fd=os.open(".presence.lock",lock_flags,0o600,dir_fd=current)
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode): raise PresenceUnavailable("unsafe presence lock")
            fcntl.flock(lock_fd,fcntl.LOCK_EX)
            return callback(current)
        except (OSError,ValueError) as exc:
            if isinstance(exc,PresenceUnavailable): raise
            raise PresenceUnavailable("unsafe session authority") from exc
        finally:
            if lock_fd>=0:
                try: fcntl.flock(lock_fd,fcntl.LOCK_UN)
                except OSError: pass
                os.close(lock_fd)
            for fd in reversed(fds): os.close(fd)

    def run_group_transaction(
        self, group_id: str | None, callback: Callable[[], Any],
        *, content_paths: list[Path] | None = None,
    ) -> Any:
        from .io_utils import path_locks
        self.ensure_session()
        owned = [self.sidecar_lock_path, *(content_paths or [])]
        if group_id:
            _assert_safe_path_token("group_id", group_id)
            owned.append(self.history_dir / "_groups" / group_id / "manifest.json")
        if content_paths:
            with bundle_transaction_lock(self.bundle_root, "exclusive"):
                with path_locks(owned):
                    return callback()
        with path_locks(owned):
            return callback()

    def append_event(self, event: dict[str, Any], *, publish: bool = True) -> dict[str, Any]:
        self.ensure_session()
        return self.run_sidecar_transaction(
            lambda: self._append_event_locked(event, publish=publish)
        )

    def append_outbox_event(self, outbox_id: str, event: dict[str, Any]) -> int:
        """Append one immutable proposal notification, idempotently.

        The retained event log is the dedup authority.  Lookup and append share
        the sidecar transaction so concurrent reconcilers cannot create two
        retained rows for one outbox receipt.
        """
        if (not isinstance(outbox_id, str) or not outbox_id
                or not isinstance(event, dict)):
            raise ValueError("invalid proposal outbox event")
        self.ensure_session()

        def append() -> int:
            from .proposal_review.canonical import canonical_digest
            from .proposal_review.errors import RepositoryCorruptError
            self.ensure_session()
            candidates = [self.events_path, *sorted(
                self.events_path.parent.glob(f"{self.events_path.stem}-*-*{self.events_path.suffix}"))]
            for path in candidates:
                if not path.exists():
                    continue
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1):
                    raise RepositoryCorruptError("proposal event authority is unsafe")
                mode = stat.S_IMODE(info.st_mode)
                if mode != 0o600:
                    try:
                        path.chmod(0o600, follow_symlinks=False)
                    except OSError as exc:
                        raise RepositoryCorruptError(
                            "proposal event authority mode cannot be normalized") from exc
                    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
                        raise RepositoryCorruptError("proposal event authority is not private")
            expected_digest = canonical_digest(event)
            for row in _read_jsonl_with_rotations(self.events_path):
                if row.get("outbox_id") == outbox_id:
                    if (row.get("type") != "proposal" or row.get("data") != event
                            or row.get("payload_digest") != expected_digest):
                        raise RepositoryCorruptError(
                            "retained proposal outbox identity conflicts with payload")
                    cursor = row.get("event_cursor")
                    if isinstance(cursor, int) and not isinstance(cursor, bool):
                        return cursor
                    raise RepositoryCorruptError("retained proposal event has no valid cursor")
            row = self._append_event_locked(
                {"type": "proposal", "outbox_id": outbox_id, "data": dict(event),
                 "payload_digest": expected_digest})
            return int(row["event_cursor"])

        return self.run_sidecar_transaction(append)

    def _append_event_locked(self, event: dict[str, Any], *, publish: bool = True,
                             receipt_directory_fd: int | None = None) -> dict[str, Any]:
        """Stamp, persist, and (optionally) broadcast a session event.

        Fills ``id``/``ts``/``seq`` if absent, appends to ``events.jsonl``
        under the advisory lock (with §10.5 rotation when configured), then
        publishes to the bus. Returns the fully attributed event (the dict
        callers hand to SSE clients).

        (§17 library API alias: ``events_append``.)
        """
        self.ensure_session()
        ev = dict(event)
        ev.setdefault("id", new_id())
        # Unconditional per-append identity for cross-process dedup. ``id``
        # can NOT serve: comment lifecycle events reuse the comment id (the
        # SSE contract), and ``seq`` is a per-instance counter that collides
        # across processes. See _event_dedup_key.
        ev["event_id"] = new_id()
        ev.setdefault("ts", _now_iso())
        # The cursor is allocated from durable history while the sidecar
        # cross-process lock is held.  Per-process counters can collide when
        # two long-lived Studio instances append concurrently.
        durable_max = max(
            (row.get("seq", 0) for row in _read_jsonl_with_rotations(self.events_path)
             if isinstance(row.get("seq"), int) and not isinstance(row.get("seq"), bool)),
            default=0,
        )
        durable_rev = max(
            (row.get("rev", 0) for row in _read_jsonl_with_rotations(self.events_path)
             if isinstance(row.get("rev"), int) and not isinstance(row.get("rev"), bool)),
            default=0,
        )
        if self.event_sequence_path.is_file():
            state = json.loads(self.event_sequence_path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or not isinstance(state.get("seq"), int)
                    or not isinstance(state.get("bundle_rev"), int)):
                raise ValueError("invalid durable event sequence")
            durable_max = max(durable_max, state["seq"])
            durable_rev = max(durable_rev, state["bundle_rev"])
        with self._lock:
            self.seq = max(self.seq, durable_max) + 1
            self.bundle_rev = max(self.bundle_rev, durable_rev) + 1
            ev["seq"] = self.seq
        ev["event_cursor"] = ev["seq"]
        ev.setdefault("rev", self.bundle_rev)
        durable_atomic_write_text(
            self.event_sequence_path,
            json.dumps({"seq": self.seq, "bundle_rev": self.bundle_rev}, separators=(",", ":")),
        )
        _append_jsonl(
            self.events_path, ev, proc_lock=self._lock,
            max_bytes=self.events_max_bytes, keep=self.events_keep,
            before_prune=lambda: self._checkpoint_notification_events_locked(
                receipt_directory_fd=receipt_directory_fd),
        )
        if publish:
            self.bus.publish(ev)
            # Track the event so the cross-process tail doesn't re-broadcast it.
            self._remember_published(_event_dedup_key(ev))
        return ev

    def _remember_published(self, key: str | None) -> None:
        """Track an event dedup key as published by THIS process.

        Keys come from :func:`_event_dedup_key` — ``event_id``-based, NOT
        ``id``-based. Comment lifecycle events deliberately reuse the
        COMMENT id as the event ``id`` (the SSE contract: the client's
        upsertComment keys on it), so an id-based set poisoned itself:
        once the creation event was published in-process, every later
        claim/resolve event for that comment carried the same id and was
        skipped by the tail forever (CLI state flips never reached open
        tabs). ``event_id`` is a fresh ULID stamped on every append.
        """
        if not key:
            return
        with self._lock:
            self._recently_published_events.add(key)
            self._recently_published_order.append(key)
            self._event_tail_seen_keys.add(key)
            # Bound the ring at 256 entries.
            while len(self._recently_published_order) > 256:
                old = self._recently_published_order.pop(0)
                self._recently_published_events.discard(old)

    def tail_and_broadcast_cross_process_events(self) -> list[dict[str, Any]]:
        return self.run_sidecar_transaction(
            self._tail_and_broadcast_cross_process_events_locked
        )

    def _tail_and_broadcast_cross_process_events_locked(self) -> list[dict[str, Any]]:
        """Tail events.jsonl for entries written by ANOTHER process and
        broadcast them to THIS process's SSE subscribers.

        Cross-process SSE gap (INTENT2-001 / Bundle H finding): when a CLI
        mutator in a separate process calls save_concept, the activity /
        changed / graph events land in events.jsonl but the CLI's bus has no
        subscribers and this process's bus never saw the append. The watcher's
        disk-origin emit_change correctly DEDUPES the .md write (via
        .last-logged.json), but that leaves the browser with NO signal at
        all. This method tails events.jsonl past the highest id we've seen,
        skips ids we published ourselves (``_recently_published_events``),
        and broadcasts the rest. Returns the broadcast entries for inspection.

        Idempotent + bounded: reads at most the last 500 events from the
        feed (sufficient for any realistic burst), filters, publishes. Called
        from the watcher's ``_reload`` path when the bundle changed AND from
        its every-tick ``_tick`` path when events.jsonl's mtime moved
        (ARCH4-001 — session-only writes like a CLI claim/resolve/presence
        never touch a ``.md`` file).

        Dedup is by :func:`_event_dedup_key` (``event_id``-based). It was
        id-based, which silently dropped every comment LIFECYCLE event:
        comment events reuse the comment id as the event id (the SSE
        contract), so after the creation event was published in-process,
        the claim/resolve rows written by a CLI process carried an
        already-seen id and were never broadcast — an open tab never saw
        the state flip.
        """
        broadcast: list[dict[str, Any]] = []
        try:
            rows = list(_read_jsonl_with_rotations(self.events_path))
        except OSError:
            return broadcast
        with self._lock:
            current_keys = {
                key for row in rows if (key := _event_dedup_key(row)) is not None
            }
            self._event_tail_seen_keys.intersection_update(current_keys)
            seen = set(self._recently_published_events) | set(self._event_tail_seen_keys)
        start = 0
        if self._event_tail_cursor_key is not None:
            cursor_found = False
            for index, row in enumerate(rows):
                if _event_dedup_key(row) == self._event_tail_cursor_key:
                    start = index + 1
                    cursor_found = True
                    break
            if not cursor_found and rows:
                resync = {"type": "resync", "reason": "retention_gap"}
                self.bus.publish(resync)
                self._event_tail_cursor_key = _event_dedup_key(rows[-1])
                return [resync]
        # Process a bounded oldest-first batch. The cursor advances for every
        # examined record, including locally-published records skipped by
        # canonical dedup, so bursts larger than one batch are drained across
        # calls without dropping unseen rows.
        batch = rows[start:start + 256]
        for row in batch:
            key = _event_dedup_key(row)
            if key is not None:
                self._event_tail_cursor_key = key
            if key is None:
                continue
            if key in seen:
                continue
            broadcast.append(row)
            seen.add(key)  # don't re-broadcast within this call
            with self._lock:
                self._event_tail_seen_keys.add(key)
        for row in broadcast:
            try:
                self.bus.publish(row)
                self._remember_published(_event_dedup_key(row))
            except Exception:
                pass
        return broadcast

    def preflight_group_members(
        self, group_id: str | None, concept_ids: list[str]
    ) -> str | None:
        """Validate and reserve the complete logical member set under its lock.

        The caller must keep :meth:`run_group_transaction` ownership through
        content persistence and manifest publication. This method performs no
        write; that full-lock envelope is the reservation.
        """
        if group_id is None:
            return None
        members = self._read_group_manifest_locked(group_id) or []
        existing = {row["concept"] for row in members}
        requested = set(concept_ids)
        cap = self.constraints.get("max_edits_per_action")
        if isinstance(cap, int) and cap > 0:
            total = len(existing | requested)
            if total > cap:
                return f"max_edits_per_action ({cap}) reached for group {group_id!r}"
        return None

    # §17 library API alias.
    def events_append(self, event: dict[str, Any], *, publish: bool = True
                      ) -> dict[str, Any]:
        return self.append_event(event, publish=publish)

    def record_activity(self, *, actor: str, action: str, ids: list[str],
                        summary: str, origin: str = "mutator",
                        undoable: bool = False, group_id: str | None = None,
                        detail: dict[str, Any] | None = None,
                         emit_graph: bool | None = None,
                         publish: bool = True,
                         collector: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Record an agent/CLI activity entry (§7.2 ``activity`` / §12.2).

        When ``action`` is in :data:`_GRAPH_ACTIONS` and ``emit_graph`` is
        ``None`` (default), a follow-on ``graph`` event is emitted so the
        graph view invalidates its cached layout (P1-6). Set ``emit_graph``
        explicitly to override.
        """
        ev: dict[str, Any] = {
            "type": "activity",
            "actor": actor,
            "origin": origin,
            "action": action,
            "ids": list(ids),
            "summary": summary,
            "undoable": bool(undoable),
        }
        if group_id:
            _assert_safe_path_token("group_id", group_id)
            ev["group_id"] = group_id
        if detail:
            ev["detail"] = detail
        out = self.append_event(ev, publish=publish)
        if collector is not None:
            collector.append(out)
        # §7.2 graph event: after a graph-affecting activity, emit ``graph``
        # so the graph view patches (P1-6). One graph event per activity; the
        # graph view coalesces if several arrive close together.
        if emit_graph is True or (emit_graph is None and action in _GRAPH_ACTIONS):
            graph_event = self.append_event(
                {"type": "graph", "ids": list(ids), "origin": origin},
                publish=publish,
            )
            if collector is not None:
                collector.append(graph_event)
        return out

    def read_events(self, *, since: str | None = None, limit: int = 200,
                    actor: str | None = None, concept: str | None = None,
                    order: str = "asc") -> list[dict[str, Any]]:
        """Read + filter the events feed (§10.5 / §12.2 change-list source).

        Reads across rotated + active files (§10.5) so rotation does not
        truncate the visible history.

        ``order``:
          * ``"asc"`` (default): oldest-first, after the ``since`` cursor.
            Used by ``okf watch --since`` for replay (oldest unplayed → newest).
          * ``"desc"``: newest-first. Used by the change-list panel which
            must surface RECENT activity; on a busy session an asc limit
            returns the OLDEST N rows, hiding the agent's latest pass at the
            top (Bundle H finding). When ``order="desc"`` the ``since``
            cursor is ignored (it is an oldest-first concept).
        """
        rows = self.run_sidecar_transaction(
            lambda: list(_read_jsonl_with_rotations(self.events_path))
        )
        if order == "desc":
            # Newest-first: reverse the chronological order, then filter,
            # then take the leading limit. The since cursor is ignored
            # because it's anchored at "give me everything AFTER this id",
            # which is an oldest-first concept.
            rows.reverse()
            out: list[dict[str, Any]] = []
            for row in rows:
                if actor and row.get("actor") != actor:
                    continue
                if concept and concept not in (row.get("ids") or []):
                    continue
                out.append(row)
                if len(out) >= limit:
                    break
            return out
        # asc (default): oldest-first after the since cursor.
        out = []
        skipping = since is not None
        for row in rows:
            if skipping:
                if row.get("id") == since or str(row.get("event_cursor", "")) == since:
                    skipping = False
                continue
            if actor and row.get("actor") != actor:
                continue
            if concept and concept not in (row.get("ids") or []):
                continue
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def read_event_snapshot(self) -> list[dict[str, Any]]:
        """Return one stable retained oldest-first event snapshot."""
        return self.run_sidecar_transaction(
            lambda: list(_read_jsonl_with_rotations(self.events_path))
        )

    def emit_change(self, *, kind: str, ids: list[str], origin: str,
                    graph: bool = False, publish: bool = True) -> dict[str, Any]:
        """Emit a ``changed``/``created``/``removed``/``graph`` event (§7.2).

        Used by the watcher change-diff (origin ``disk``) and by mutator /
        undo callers (origin ``mutator`` / ``undo``). Per §10.5, a disk event
        whose concept content rev matches the most recent rev a mutator/undo
        already logged for that concept is de-duped (skipped) — so an agent or
        undo write that already logged itself is NOT re-logged when the
        watcher sees the mtime change.

        Mutator/undo callers populate the dedup map via :meth:`mark_logged`
        (called from :meth:`save_concept` and :meth:`restore_snapshot`);
        this method never dedupes a non-disk origin. If every id of a disk
        event is deduped, nothing is appended or published and a
        ``{"deduped": True}`` marker dict is returned (no caller inspects the
        return value today).

        Dedup is **cross-process** (P1-2): the dedup map is reloaded from
        ``.last-logged.json`` at the top of every disk emit so a separate
        ``okf watch`` process sees writes a sibling ``serve`` process logged.
        """
        # P1-2: refresh from disk so a separate process's writes are honored.
        if origin == "disk":
            self._load_last_logged()
        emit_ids = list(ids)
        if origin == "disk" and kind in ("changed", "created", "removed"):
            kept: list[str] = []
            for cid in emit_ids:
                with self._lock:
                    logged = self._last_logged_rev.get(cid)
                # Skip only when the id was logged by a mutator AND the
                # current on-disk rev still matches it (same content). A
                # missing/unreadable file → unknown rev → keep (log it).
                if logged is not None and self._current_disk_rev(cid) == logged:
                    continue
                kept.append(cid)
            emit_ids = kept
        if not emit_ids and not graph:
            return {"type": kind, "ids": [], "origin": origin, "graph": graph,
                    "deduped": True}
        return self.append_event(
            {"type": kind, "ids": emit_ids, "origin": origin, "graph": graph},
            publish=publish,
        )

    def mark_logged(self, concept_id: str, raw: str | bytes) -> None:
        """Record that ``concept_id`` was just written with content ``raw``.

        The watcher's disk-origin :meth:`emit_change` de-dupes an id whose
        current on-disk rev equals the value stored here (§10.5), so a write
        that already logged itself (a :meth:`save_concept` write or an
        :meth:`restore_snapshot` undo) is not re-logged when the watcher
        notices the mtime change.

        The map is also persisted atomically to ``.last-logged.json`` so a
        SEPARATE process (``serve`` + a concurrent ``okf watch``) shares the
        same dedup state — closing the P1-2 cross-process double-logging gap.
        """
        def update() -> None:
            with self._lock:
                self._last_logged_rev[concept_id] = rev_of(raw)
            self._save_last_logged()
        self.run_sidecar_transaction(update)

    def _current_disk_rev(self, concept_id: str) -> str | None:
        """Content-hash rev of ``concept_id``'s CURRENT on-disk bytes, or None.

        Used by :meth:`emit_change` to decide whether a disk event duplicates
        a write a mutator/undo already logged. Reads
        ``<bundle_root>/<concept_id>.md`` (SPEC §2 concept-id → path
        convention: the id is the file path with ``.md`` stripped). Returns
        ``None`` if the file is missing or unreadable so the caller falls
        through to logging — a missing/unreadable file must never suppress a
        real change event.
        """
        try:
            data = (self.bundle_root / f"{concept_id}.md").read_bytes()
        except OSError:
            return None
        return rev_of(data)

    # --- single internal write path (current spec §10/§13 ``save_concept``) -------

    def save_concept(self, *, concept_id: str, raw: str | bytes,
                     actor: str = "agent", action: str = "write_concept",
                     origin: str = "mutator", group_id: str | None = None,
                     expected_rev: str | None = None, summary: str | None = None,
                     detail: dict[str, Any] | None = None, undoable: bool = True,
                     emit_graph: bool | None = None, path: Path | None = None,
                     publish: bool = True, defer_events: bool = False) -> "WriteResult":
        from .paths import concept_id_from_str, concept_id_to_path, ConceptIdError
        try:
            cid = concept_id_from_str(concept_id)
        except ConceptIdError as exc:
            return WriteResult(ok=False, error=f"bad concept id: {exc}")
        target_path = path if path is not None else concept_id_to_path(self.bundle_root, cid)
        from .io_utils import path_locks
        self.ensure_session()
        owned_paths = [target_path, self.sidecar_lock_path]
        if group_id:
            owned_paths.append(self.history_dir / "_groups" / group_id / "manifest.json")
        with bundle_transaction_lock(self.bundle_root, "exclusive"):
            with path_locks(owned_paths):
                return self._save_concept_unlocked(
                    concept_id=concept_id, raw=raw, actor=actor, action=action,
                    origin=origin, group_id=group_id, expected_rev=expected_rev,
                    summary=summary, detail=detail, undoable=undoable,
                    emit_graph=emit_graph, path=target_path, publish=publish,
                    defer_events=defer_events,
                )

    def _save_concept_unlocked(self, *, concept_id: str, raw: str | bytes,
                     actor: str = "agent", action: str = "write_concept",
                     origin: str = "mutator",
                     group_id: str | None = None,
                     expected_rev: str | None = None,
                     summary: str | None = None,
                     detail: dict[str, Any] | None = None,
                     undoable: bool = True,
                      emit_graph: bool | None = None,
                      path: Path | None = None,
                      publish: bool = True,
                       defer_events: bool = False) -> "WriteResult":
        """The single internal atomic write path for the studio (current spec §10/§13).

        Every studio-aware write — CLI mutators (``okf link-add`` /
        ``entity-add`` / ``update`` / ``repair`` / ``write-concept`` /
        ``set-frontmatter``), the HTTP ``/__apply`` endpoint, and the
        ``/__undo`` restore — routes through this method so that:

        * the write is atomic (tmp + rename, AGENTS.md hard rule #8);
        * the prior bytes are snapshotted for one-click Undo (§12.5);
        * a group of writes (shared ``group_id``) is undoable as one pass;
        * an attributed ``activity`` event is appended to ``events.jsonl``
          with actor/origin/action/ids/summary (so the change list shows
          *who* did *what*, not just *something changed*);
        * the new content-hash rev is marked logged so the watcher's disk
          emit_change de-dupes it (no double-logging);
        * a `changed` event (and `graph` for graph-affecting ops) is
          broadcast over SSE so every open tab patches in place with no
          refresh.

        Args:
            concept_id: canonical concept id (e.g. ``tables/orders``). Used
                for path resolution when ``path`` is None, and for activity
                attribution.
            raw: the new file bytes (str or bytes).
            actor: who is doing the write (default ``agent``). Used in the
                activity event.
            action: short op name for the activity event (e.g.
                ``add_link``, ``add_entity``, ``write_concept``,
                ``undo_restore``). Drives the auto-emit-``graph`` heuristic
                when ``emit_graph`` is None.
            origin: where the write came from (``mutator`` / ``http-apply``
                / ``undo`` / ``auto-repair``). Used in the activity event
                and the change event.
            group_id: optional group manifest id. When set, the snapshot is
                added to ``history/_groups/<group_id>/manifest.json`` so
                the whole pass reverts in one click (§12.5).
            expected_rev: when set, the on-disk content-hash rev is compared
                to this value BEFORE the write. A mismatch means someone
                (usually the user, editing on disk) changed the file
                between read-time and write-time — the §9.3/§9.4 collision
                case. The function returns ``WriteResult(conflict=True)``
                WITHOUT writing, so the caller can surface the conflict
                instead of silent-clobbering (§9 acceptance: "no silent
                clobber").
            summary: human-readable one-liner for the activity event
                (default: ``f"{action} on {concept_id}"``).
            detail: op-specific detail dict for the activity event
                (target concept, link type, args, etc.).
            undoable: when True (default), snapshot the prior bytes for
                Undo. Set False for ops that shouldn't get an Undo button
                (e.g. ``repair`` that touches many files atomically — the
                caller may prefer a single group-undo instead).
            emit_graph: override the auto-emit-``graph`` heuristic. None
                means "auto: emit ``graph`` iff ``action in _GRAPH_ACTIONS``".
            path: optional explicit file path. When None, the path is
                derived from ``bundle_root / f"{concept_id}.md"``.
            publish: when False, skip the SSE broadcast (used by callers
                that batch many writes and broadcast once at the end).
            defer_events: internal transaction mode. Persist and snapshot, but
                defer mark-logged/activity/graph/changed publication until the
                owning multi-write transaction has durably committed. This
                prevents append-only success events surviving a later rollback.

        Returns:
            A :class:`WriteResult`. On conflict, ``conflict=True`` and
            ``current_rev`` is the on-disk rev that mismatched; the write
            did NOT happen. On success, ``ok=True``, ``rev`` is the new
            content-hash, ``activity_id`` is the appended event id, and
            ``snap_rev`` is the prior-bytes rev (for explicit Undo lookup).
        """
        from .paths import concept_id_from_str, concept_id_to_path, ConceptIdError  # late import

        # Validate the concept_id lexically BEFORE any filesystem op so a
        # malformed id (HTTP-supplied) cannot reach the path join. The path is
        # built from the validated tuple, never the raw string: a raw id like
        # "/etc/x" survives validation (empty segments are dropped) but would
        # make a raw-string join absolute and escape the bundle.
        try:
            cid = concept_id_from_str(concept_id)
        except ConceptIdError as e:
            return WriteResult(ok=False, conflict=False, error=f"bad concept id: {e}")
        target_path = path if path is not None else concept_id_to_path(self.bundle_root, cid)
        if isinstance(raw, str):
            raw_bytes = raw.encode("utf-8")
        else:
            raw_bytes = raw

        # Read prior bytes (None if file doesn't exist yet — created case).
        prior_bytes: bytes | None = None
        if target_path.is_file():
            try:
                prior_bytes = target_path.read_bytes()
            except OSError:
                prior_bytes = None

        # §9.3/§9.4 collision guard. expected_rev is the content-hash the
        # caller saw at read-time; if the on-disk rev has moved, someone
        # (the user, in their editor) edited the file concurrently. We do
        # NOT clobber; we return conflict and let the caller surface it.
        if expected_rev is not None:
            current_rev = rev_of(prior_bytes) if prior_bytes is not None else None
            expected_absent = expected_rev == ""
            if (expected_absent and prior_bytes is not None) or (
                not expected_absent and current_rev != expected_rev
            ):
                return WriteResult(
                    ok=False, conflict=True,
                    expected_rev=expected_rev, current_rev=current_rev,
                    concept_id=concept_id,
                )

        # §12.3 opt-in forbidden_paths constraint. The user opt-in only;
        # default empty (fully free agent, §1.1/D8).
        forbidden = self.constraints.get("forbidden_paths") or []
        if isinstance(forbidden, list) and forbidden:
            import fnmatch
            for pat in forbidden:
                if fnmatch.fnmatch(concept_id, str(pat)):
                    return WriteResult(
                        ok=False, conflict=False,
                        error=f"forbidden_paths constraint blocked {concept_id!r}",
                        concept_id=concept_id,
                    )

        # §12.3 opt-in max_edits_per_action constraint. Counts writes that
        # share a group_id; the (N+1)th is refused. Per-action only; not a
        # global cap. The user opt-in; default absent (no cap).
        # The public save funnel holds the canonical group lock through this
        # check and snapshot publication. Multi-target transactions additionally
        # preflight their complete unique member set before their first write.
        if group_id is not None:
            cap = self.constraints.get("max_edits_per_action")
            if isinstance(cap, int) and cap > 0:
                with self._lock:
                    members = self._read_group_manifest_locked(group_id) or []
                    existing = {member["concept"] for member in members}
                    if concept_id not in existing and len(existing) >= cap:
                        return WriteResult(
                            ok=False, conflict=False,
                            error=f"max_edits_per_action ({cap}) reached for group {group_id!r}",
                            concept_id=concept_id,
                        )

        # Snapshot prior bytes for Undo BEFORE the write (§12.5). Deferred
        # transaction writes stage only the observed bytes; their owner creates
        # snapshots/group membership after the whole disk transaction commits.
        snap_rev: str | None = None
        if undoable and not defer_events and prior_bytes is not None:
            snap_rev = self.snapshot_for_undo(
                concept_id=concept_id, raw=prior_bytes, group_id=group_id,
            )
        elif undoable and not defer_events and prior_bytes is None:
            # Created case: snapshot an empty placeholder so Undo can remove
            # the file by restoring empty + the caller can decide to delete.
            snap_rev = self.snapshot_for_undo(
                concept_id=concept_id, raw=b"", group_id=group_id,
            )

        # Atomic write (AGENTS.md hard rule #8).
        atomic_write_bytes(target_path, raw_bytes)

        # Attribute + log + broadcast (the heart of the funnel).
        new_rev = rev_of(raw_bytes)
        if defer_events:
            return WriteResult(
                ok=True, conflict=False, rev=new_rev,
                concept_id=concept_id, snap_rev=None, prior_bytes=prior_bytes,
            )
        self.mark_logged(concept_id, raw_bytes)
        activity_summary = summary or f"{action} on {concept_id}"
        activity_detail = dict(detail or {})
        if snap_rev is not None:
            activity_detail.setdefault("before", snap_rev)
            activity_detail.setdefault("before_concept", concept_id)
        activity = self.record_activity(
            actor=actor, action=action, ids=[concept_id],
            summary=activity_summary, origin=origin,
            undoable=undoable, group_id=group_id, detail=activity_detail,
            emit_graph=emit_graph,
        )
        if publish:
            self.emit_change(kind="changed", ids=[concept_id], origin=origin)
            # The watcher's disk emit for THIS rev will dedupe (mark_logged
            # above). For non-disk origins the change event is the canonical
            # broadcast.
        return WriteResult(
            ok=True, conflict=False, rev=new_rev, concept_id=concept_id,
            activity_id=activity.get("id"), snap_rev=snap_rev,
        )

    # --- undo (§12.5) ---------------------------------------------------

    def restore_snapshot(self, *, concept_id: str, rev: str,
                         actor: str = "agent", origin: str = "undo",
                         group_id: str | None = None,
                         publish: bool = True) -> "WriteResult":
        """Restore a prior snapshot through :meth:`save_concept` (§12.5).

        The restore is itself an attributed, undoable write — undoing a
        change is itself a change that appears in the change list (so the
        user can redo by undoing the undo). Reads the prior snapshot bytes
        and routes them through ``save_concept(action="undo_restore")``.
        """
        raw = self.undo_snapshot(concept_id=concept_id, rev=rev)
        if raw is None:
            return WriteResult(
                ok=False, conflict=False,
                error=f"no snapshot for {concept_id} @ {rev}",
                concept_id=concept_id,
            )
        return self.save_concept(
            concept_id=concept_id, raw=raw, actor=actor,
            action="undo_restore", origin=origin,
            group_id=group_id, undoable=True,
            summary=f"undo {concept_id} @ {rev}", publish=publish,
        )

    # --- comments / directives (§9) -------------------------------------

    def audit_public_denial(self, *, route: str, method: str) -> None:
        """Bounded private denial audit: no URL queries, headers, bodies or tokens."""
        from .io_utils import durable_atomic_write_text
        def write():
            path = self._session_io_dir / 'public-denials.json'
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                rows = []
            else:
                try:
                    import stat
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError('unsafe denial audit')
                    with os.fdopen(fd, 'rb', closefd=False) as stream:
                        raw = stream.read(131073)
                    if len(raw) > 131072:
                        raise ValueError('oversized denial audit')
                    rows = json.loads(raw)
                finally:
                    os.close(fd)
            if not isinstance(rows, list):
                raise ValueError('invalid denial audit')
            rows = rows[-255:] + [{'ts': _now_iso(), 'route': route, 'method': method,
                                   'reason': 'forbidden', 'ingress': 'public'}]
            durable_atomic_write_text(path, json.dumps(rows, separators=(',', ':')))
        self.run_sidecar_transaction(write)

    def post_comment(self, *, concept: str, body: str,
                     document: str | None = None,
                     anchor: dict[str, Any] | None = None,
                     actor: str = "user", detail: dict[str, Any] | None = None,
                       parent_id: str | None = None,
                       state: str = "open",
                        transaction_meta: dict[str, str] | None = None,
                        fault: str | None = None,
                        portable_import: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append a user comment/ask as an ``open`` directive (§9.1).

        ``portable_import`` is the server-owned provenance marker for a
        locally imported portable feedback note (X2). Only the local import
        route passes it; no HTTP body, CLI flag or event can supply it. It is
        stored as a top-level row field and deliberately NOT projected into
        the closed comment event/receipt schema.

        ``parent_id`` (optional) creates a threaded reply to a parent
        comment. The comment is anchored to the same concept as the parent.

        When replying to an archived parent, the parent is
        auto-unarchived (archive is a soft hide, not a closed state). The
        new reply itself is never archived.

        ``state`` may be ``"open"`` (default — an ask that is work for the
        agent) or ``"resolved"`` (a statement that needs no follow-up).
        Agent-authored replies posted via ``okf comment-reply`` use
        ``"resolved"`` so ``wait --for comment`` never hands the agent its
        own reply back as new work, and the thread-archive gate (every
        comment resolved) stays satisfiable without the agent resolving
        its own remarks.
        """
        if state not in ("open", "resolved"):
            raise ValueError(f"comment state must be open|resolved, got {state!r}")
        self.ensure_session()
        if parent_id and not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", parent_id):
            raise ValueError("invalid parent comment id")
        from .documents import document_path, document_concept, comment_document
        if parent_id:
            parent = self.get_comment(parent_id)
            if parent is None:
                raise ValueError("parent comment not found")
            if document is not None and document != comment_document(parent):
                raise ValueError('reply target differs from parent')
            concept = parent.get("concept", "")
            document = parent.get("document", document)
        if document is not None:
            document = document_path(document)
            if document_concept(document) != concept:
                raise ValueError("inconsistent document target")
        cid = new_id()
        now = _now_iso()
        # Auto-derive request_summary from body (short version of
        # the ask) so the collapsed preview has a useful short from the
        # moment the comment is posted, before the agent claims/resolves.
        import re as _re
        _body_trim = body.strip()
        _req_sum = ""
        if _body_trim:
            _parts = _re.split(r"[.!?\n]", _body_trim, maxsplit=1)
            _first = (_parts[0] if _parts else _body_trim).strip() or _body_trim
            if len(_first) > 100:
                _first = _first[:100].rstrip() + "\u2026"
            _req_sum = _first
        row_token = "r2:" + new_id()
        directive = {
            "id": cid,
            "ts": now,
            "updated_at": now,
            "concept": concept,
            "anchor": anchor or {},
            "actor": actor,
            "body": body,
            "state": state,
            "claimed_by": actor if state == "resolved" else None,
            "resolved_activity": [],
            "detail": detail or {},
            "archived": False,
            "request_summary": _req_sum,
            "row_token": row_token,
        }
        if document is not None:
            directive["document"] = document
        if parent_id:
            directive["parent_id"] = parent_id
        if portable_import is not None:
            if not isinstance(portable_import, dict) or parent_id:
                raise ValueError("invalid portable import marker")
            # Deterministic deep copy: a retry after a crash writes the same row.
            directive["portable_import"] = json.loads(json.dumps(
                portable_import, sort_keys=True, ensure_ascii=False))
        txid = ((transaction_meta or {}).get("comment_create_transaction_id")
                or row_token.removeprefix("r2:"))
        if transaction_meta:
            directive.update(transaction_meta)
        else:
            directive["notification_transaction_id"] = txid
        # SEC2-002: rotate directives.jsonl too (default 2 MiB / 3 files) so
        # comment spam can't fill the disk. directives are kept longer than
        # events because comment state is the source of truth for the UI's
        # comment panel — but rotation preserves the most recent records.
        comment_locks = [self._comment_lock_path(cid)]
        if parent_id:
            comment_locks.append(self._comment_lock_path(parent_id))
        event = self._comment_event(directive, transaction_id=txid)
        # The immutable full payload is durable before the directive can become
        # visible.  On retry, an existing pending receipt owns all canonical row
        # identity fields, so a crash in this interval cannot create a second
        # row identity or reconstruct the create event from mutable latest state.
        reserved, authoritative_event = self.run_sidecar_transaction(
            lambda: self._reserve_notification_identity(txid, "create", event))
        for name in ("id", "concept", "anchor", "state", "claimed_by", "actor",
                     "body", "resolved_activity", "reply", "archived", "summary",
                     "request_summary", "updated_at", "ts", "row_token", "parent_id", "document"):
            if name in authoritative_event:
                directive[name] = authoritative_event[name]
            elif name == "parent_id":
                directive.pop(name, None)
        if fault == "after_notification_reservation":
            raise OSError("injected after notification reservation before directive")
        with path_locks(comment_locks):
            with DirectiveStore(self._session_io_dir).writer() as directive_tx:
                rows = list(self._read_directive_rows_locked(directive_tx))
                if any(row.get("id") == cid or row.get("row_token") == row_token for row in rows):
                    raise CommentMutationError("unavailable")
                if parent_id:
                    parent = next((row for row in reversed(rows) if row.get("id") == parent_id), None)
                    if parent is None:
                        raise ValueError("parent comment not found")
                    directive["concept"] = parent.get("concept", concept)
                    if _normalize_comment_row(parent).get("archived"):
                        self._update_comment_locked(directive_tx, parent_id, archived=False,
                                                    mutation_actor=actor)
                self._append_directive_locked(directive_tx, directive)
        # Broadcast a comment event so the UI + agent see it live. Carries
        # ``actor`` + ``parent_id`` so a tab that did NOT post the comment
        # still threads a reply under its parent (before this, only the
        # posting tab knew — the SSE payload lacked parent_id, so other
        # open tabs rendered replies as roots until a reload).
        event = self._comment_event(directive, transaction_id=txid)
        try:
            self._deliver_notification_event_once(txid, "create", event, fault=fault)
        except Exception:
            if transaction_meta or fault:
                raise
        if not transaction_meta:
            self._cleanup_transaction_sidecars_locked()
        return directive

    @staticmethod
    def _comment_event(directive: dict[str, Any], *, transaction_id: str | None = None,
                       operation: str = "create", mutation_actor: str | None = None) -> dict[str, Any]:
        """Build the one canonical full payload for every comment mutation."""
        event = {
            "type": "comment", "id": directive["id"], "concept": directive.get("concept", ""),
            "anchor": directive.get("anchor", {}), "state": directive.get("state", "open"),
            "claimed_by": directive.get("claimed_by"), "actor": directive.get("actor"),
            "body": directive.get("body", ""),
            "resolved_activity": directive.get("resolved_activity", []),
            "reply": directive.get("reply"), "archived": directive.get("archived", False),
            "summary": directive.get("summary"),
            "request_summary": directive.get("request_summary"),
            "updated_at": directive.get("updated_at"), "ts": directive.get("ts"),
            "row_token": directive.get("row_token"),
        }
        if directive.get("parent_id"):
            event["parent_id"] = directive["parent_id"]
        if "document" in directive:
            event["document"] = directive["document"]
        if operation == "lifecycle":
            # A lifecycle notification describes this mutation, not authorship
            # of the original comment. Only the caller's explicit evidence can
            # identify its actor; an inherited claim is not proof of who edited
            # or archived a row. Keep create/re-anchor receipt payloads stable.
            event.update(operation="lifecycle", actor=mutation_actor,
                         ts=directive.get("updated_at"))
        txid = (transaction_id or directive.get("comment_create_transaction_id")
                or directive.get("notification_transaction_id"))
        if txid and operation in {"create", "reanchor"}:
            event["comment_create_transaction_id" if operation == "create"
                  else "reanchor_transaction_id"] = txid
        return event

    @staticmethod
    def _validate_notification_event(event: Any, txid: str, operation: str) -> dict[str, Any]:
        """Fail closed unless an outbox receipt contains one full canonical event."""
        required = {"type", "id", "concept", "anchor", "state", "claimed_by", "actor",
                    "body", "resolved_activity", "reply", "archived", "summary",
                    "request_summary", "updated_at", "ts", "row_token"}
        tx_field = "comment_create_transaction_id" if operation == "create" else "reanchor_transaction_id"
        timestamp = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
        nullable_text = ("reply", "summary", "request_summary")
        if (not isinstance(event, dict)
                or not required | {tx_field} <= set(event)
                or not set(event) <= required | {tx_field, "parent_id", "document"}
                or event.get("type") != "comment" or event.get(tx_field) != txid
                or not isinstance(event.get("id"), str)
                or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", event["id"])
                or not isinstance(event.get("concept"), str)
                or not isinstance(event.get("anchor"), dict)
                or event.get("state") not in {"open", "claimed", "resolved", "dismissed"}
                # Pre-v2 rows have no actor; re-anchor preserves that legacy
                # absence while all newly authored rows use the closed enum.
                or event.get("actor") not in {None, "user", "agent"}
                or (operation == "create" and event.get("actor") not in {"user", "agent"})
                or (operation == "create" and event.get("state") not in {"open", "resolved"})
                or (event.get("claimed_by") is not None and (
                    not isinstance(event.get("claimed_by"), str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", event["claimed_by"])))
                or (event.get("state") == "claimed" and event.get("claimed_by") is None)
                or (operation == "create" and event.get("state") == "open"
                    and event.get("claimed_by") is not None)
                or (operation == "create" and event.get("state") == "resolved"
                    and event.get("claimed_by") != event.get("actor"))
                or not isinstance(event.get("body"), str)
                or not isinstance(event.get("resolved_activity"), list)
                or any(not isinstance(item, str)
                       or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", item)
                       for item in event.get("resolved_activity", []))
                or type(event.get("archived")) is not bool
                or any(event.get(name) is not None and not isinstance(event.get(name), str)
                       for name in nullable_text)
                or not isinstance(event.get("row_token"), str)
                or not re.fullmatch(r"r2:[0-9A-HJKMNP-TV-Z]{10,26}", event["row_token"])
                or not isinstance(event.get("ts"), str) or not timestamp.fullmatch(event["ts"])
                or not isinstance(event.get("updated_at"), str)
                or not timestamp.fullmatch(event["updated_at"])
                or event.get("updated_at", "") < event.get("ts", "")
                or ("parent_id" in event and (not isinstance(event["parent_id"], str)
                    or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", event["parent_id"])))):
            raise CommentMutationError("unavailable")
        if "document" in event:
            from .documents import document_concept
            try:
                if document_concept(event["document"]) != event["concept"]:
                    raise ValueError("target mismatch")
            except (TypeError, ValueError) as exc:
                raise CommentMutationError("unavailable") from exc
        return json.loads(json.dumps(event, ensure_ascii=False))

    def _recover_comment_notifications(self) -> None:
        if self.session_authority is None:
            return
        # Pending receipts are the immutable WAL authority even when a later
        # lifecycle row has intentionally shed the create/re-anchor metadata.
        with _pinned_receipt_parent(
                self.notification_receipts_dir) as (_parent_fd, open_receipt_child):
            # Pin both the trusted parent and an existing receipt child before
            # directive enumeration.  Enumeration may rotate/checkpoint and
            # invoke fault/test callbacks; none of those may let a pathname
            # substitution redirect subsequent receipt reads or cleanup.
            directory_fd = open_receipt_child(False)
            with DirectiveStore(self._session_io_dir).writer() as directive_tx:
                visible_rows = {(row.get("id"), row.get("row_token"))
                                for row in self._read_directive_rows_locked(directive_tx)}
            if directory_fd is not None:
                for name in self._notification_receipt_names_at(directory_fd):
                    txid = name.removesuffix(".json")
                    receipt = _read_receipt_at(directory_fd, name)
                    if (not isinstance(receipt, dict) or receipt.get("state") != "pending"
                            or receipt.get("transaction_id") != txid
                            or receipt.get("operation") not in {"create", "reanchor"}):
                        continue
                    event = self._validate_notification_event(
                        receipt.get("event"), txid, receipt["operation"])
                    # A create receipt is intentionally reserved before its directive.
                    # A crash at that boundary must not publish a phantom comment; the
                    # idempotent retry first adopts this immutable identity and exposes
                    # the directive, after which normal recovery may deliver it.
                    if receipt["operation"] == "create" and (
                            event["id"], event["row_token"]) not in visible_rows:
                        continue
                    self._deliver_notification_event_once(
                        txid, receipt["operation"], event,
                        receipt_directory_fd=directory_fd)
            for directive in self.list_comments():
                txid = (directive.get("comment_create_transaction_id")
                        or directive.get("notification_transaction_id"))
                if isinstance(txid, str) and re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", txid):
                    if directory_fd is None:
                        directory_fd = open_receipt_child(True)
                        assert directory_fd is not None
                    self._deliver_notification_event_once(
                        txid, "create", self._comment_event(directive, transaction_id=txid),
                        receipt_directory_fd=directory_fd)

    @property
    def notification_receipts_dir(self) -> Path:
        return self._session_directory(".notification-receipts")

    @property
    def keyless_notification_filter_path(self) -> Path:
        return self._session_io_dir / ".keyless-notification-filter.json"

    KEYLESS_NOTIFICATION_FILTER_CAP = 10_000
    KEYLESS_NOTIFICATION_FILTER_BITS = 1 << 20
    KEYLESS_NOTIFICATION_FILTER_HASHES = 7

    def _notification_receipt_names_at(self, directory_fd: int) -> list[str]:
        try:
            names = os.listdir(directory_fd)
            receipt_names: list[str] = []
            for name in names:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise CommentMutationError("unavailable")
                # Atomic writes can leave only this nonce-bearing name after
                # process death. Remove it through the same pinned authority.
                if re.fullmatch(
                        r"\.[0-9A-HJKMNP-TV-Z]{10,26}\.json\.[0-9a-f]{16}\.tmp",
                        name):
                    os.unlink(name, dir_fd=directory_fd)
                    continue
                if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}\.json", name):
                    raise CommentMutationError("unavailable")
                receipt_names.append(name)
            os.fsync(directory_fd)
            return sorted(receipt_names)
        except OSError as exc:
            raise CommentMutationError("unavailable") from exc

    def _keyless_notification_filter_state(self) -> tuple[set[str], bytearray]:
        if not self.keyless_notification_filter_path.exists():
            return set(), bytearray(self.KEYLESS_NOTIFICATION_FILTER_BITS // 8)
        value = _read_regular_json_nofollow(self.keyless_notification_filter_path)
        if (not isinstance(value, dict) or value.get("version") != 3
                or not isinstance(value.get("transactions"), list)
                or len(value["transactions"]) > self.KEYLESS_NOTIFICATION_FILTER_CAP
                or any(not isinstance(item, str)
                       or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", item)
                       for item in value["transactions"])):
            raise CommentMutationError("unavailable")
        bits = bytearray(self.KEYLESS_NOTIFICATION_FILTER_BITS // 8)
        encoded = value.get("bloom", "")
        if (not isinstance(encoded, str) or len(encoded) != len(bits) * 2
                  or value.get("bloom_bits") != self.KEYLESS_NOTIFICATION_FILTER_BITS
                  or value.get("bloom_hashes") != self.KEYLESS_NOTIFICATION_FILTER_HASHES):
            raise CommentMutationError("unavailable")
        try:
            bits = bytearray.fromhex(encoded)
        except ValueError as exc:
            raise CommentMutationError("unavailable") from exc
        return set(value["transactions"]), bits

    def _keyless_notification_filter(self) -> set[str]:
        """Return the bounded exact checkpoint (legacy diagnostic contract)."""
        return self._keyless_notification_filter_state()[0]

    def _notification_bloom_indexes(self, txid: str) -> Iterator[int]:
        digest = hashlib.sha256(txid.encode("ascii")).digest()
        left = int.from_bytes(digest[:16], "big")
        right = int.from_bytes(digest[16:], "big") | 1
        for number in range(self.KEYLESS_NOTIFICATION_FILTER_HASHES):
            yield (left + number * right) % self.KEYLESS_NOTIFICATION_FILTER_BITS

    def _keyless_notification_filter_contains(self, txid: str) -> bool:
        exact, bits = self._keyless_notification_filter_state()
        return txid in exact or all(bits[index // 8] & (1 << (index % 8))
                                    for index in self._notification_bloom_indexes(txid))

    def _reserve_notification_identity(self, txid: str, operation: str,
                                        event: dict[str, Any], *,
                                        receipt_directory_fd: int | None = None
                                        ) -> tuple[bool, dict[str, Any]]:
        if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", txid):
            raise CommentMutationError("unavailable")
        name = f"{txid}.json"
        if receipt_directory_fd is None:
            with _pinned_receipt_directory(self.notification_receipts_dir, create=True) as directory_fd:
                assert directory_fd is not None
                return self._reserve_notification_identity(
                    txid, operation, event, receipt_directory_fd=directory_fd)
        directory_fd = receipt_directory_fd
        if directory_fd is not None:
            try:
                value = _read_receipt_at(directory_fd, name)
            except CommentMutationError:
                try:
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    value = None
                else:
                    raise
            if value is None and self._keyless_notification_filter_contains(txid):
                return False, event
            if value is not None:
                if (not isinstance(value, dict) or value.get("version") != 1
                    or value.get("transaction_id") != txid
                    or value.get("operation") != operation
                    or value.get("state", "committed") not in {"pending", "committed"}):
                    raise CommentMutationError("unavailable")
                stored_event = self._validate_notification_event(value.get("event"), txid, operation)
                if value.get("state", "committed") == "committed":
                    return False, stored_event
            # A crash may leave the reservation pending. Reconcile against all
            # retained event generations: an append proves delivery; absence
            # permits the serialized caller to retry the one append.
                field = ("comment_create_transaction_id" if operation == "create"
                         else "reanchor_transaction_id")
                if any(row.get(field) == txid
                       for row in _read_jsonl_with_rotations(self.events_path)):
                    self._commit_notification_identity(
                        txid, operation, stored_event, receipt_directory_fd=directory_fd)
                    return False, stored_event
                return True, stored_event
            event = self._validate_notification_event(event, txid, operation)
            _write_receipt_at(directory_fd, name,
                {"version": 1, "transaction_id": txid, "operation": operation,
                 "state": "pending", "event": event})
            return True, event

    def _commit_notification_identity(self, txid: str, operation: str,
                                       event: dict[str, Any], *,
                                       receipt_directory_fd: int | None = None) -> None:
        """Durably record that one transaction notification was appended."""
        if operation not in {"create", "reanchor"}:
            raise CommentMutationError("unavailable")
        event = self._validate_notification_event(event, txid, operation)
        if receipt_directory_fd is None:
            with _pinned_receipt_directory(self.notification_receipts_dir, create=True) as directory_fd:
                assert directory_fd is not None
                self._commit_notification_identity(
                    txid, operation, event, receipt_directory_fd=directory_fd)
            return
        _write_receipt_at(receipt_directory_fd, f"{txid}.json", {
            "version": 1, "transaction_id": txid, "operation": operation,
            "state": "committed", "event": event})

    def _deliver_notification_event_once(self, txid: str, operation: str,
                                           event: dict[str, Any], *,
                                           fault: str | None = None,
                                           receipt_directory_fd: int | None = None) -> None:
        """Serialize receipt reconciliation, event append, and commit."""
        def deliver() -> None:
            reserved, authoritative_event = self._reserve_notification_identity(
                txid, operation, event, receipt_directory_fd=receipt_directory_fd)
            if not reserved:
                return
            if fault == "after_notification_pending":
                raise OSError("injected after notification pending")
            self._append_event_locked(
                authoritative_event, receipt_directory_fd=receipt_directory_fd)
            if fault == "after_notification_append":
                raise OSError("injected after notification append")
            self._commit_notification_identity(
                txid, operation, authoritative_event,
                receipt_directory_fd=receipt_directory_fd)
        self.run_sidecar_transaction(deliver)

    def _checkpoint_notification_events_locked(
            self, *, receipt_directory_fd: int | None = None) -> None:
        """Checkpoint event transaction identities before rotation pruning.

        Event generations are a replay transport, not the long-lived dedupe
        authority. Before old generations are removed, copy every retained
        create/re-anchor notification identity into its durable committed
        receipt. The caller holds the sidecar transaction and JSONL append
        locks, so no event append or receipt transition can race this scan.
        """
        identities: dict[str, tuple[str, dict[str, Any]]] = {}
        for row in _read_jsonl_with_rotations(self.events_path):
            for field, operation in (
                ("comment_create_transaction_id", "create"),
                ("reanchor_transaction_id", "reanchor"),
            ):
                txid = row.get(field)
                if txid is None:
                    continue
                if not isinstance(txid, str) or not re.fullmatch(
                        r"[0-9A-HJKMNP-TV-Z]{10,26}", txid):
                    raise CommentMutationError("unavailable")
                canonical_event = {name: value for name, value in row.items()
                                   if name not in {"event_id", "seq", "event_cursor", "rev"}}
                prior = identities.setdefault(txid, (operation, canonical_event))
                if prior[0] != operation:
                    raise CommentMutationError("unavailable")
        for txid, (operation, event) in identities.items():
            self._commit_notification_identity(
                txid, operation, event,
                receipt_directory_fd=receipt_directory_fd)

    def _release_notification_identity(self, txid: str) -> None:
        try:
            with _pinned_receipt_directory(self.notification_receipts_dir) as directory_fd:
                try: os.unlink(f"{txid}.json", dir_fd=directory_fd)
                except FileNotFoundError: pass
                os.fsync(directory_fd)
        except CommentMutationError:
            if self.notification_receipts_dir.exists(): raise

    def _cleanup_transaction_sidecars_locked(self) -> None:
        """Remove exact sidecars only after WAL/tombstone retirement is durable."""
        protected: set[str] = set()
        for wal_dir, excluded in (
            (self.comment_create_wal_dir, {self.comment_create_tombstone_path, self.comment_create_filter_path}),
            (self.reanchor_wal_dir, {self.reanchor_tombstone_path, self.reanchor_filter_path}),
        ):
            for path in wal_dir.glob("*.json"):
                if path in excluded:
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise CommentMutationError("unavailable") from exc
                txid = value.get("transaction_id") if isinstance(value, dict) else None
                if isinstance(txid, str):
                    protected.add(txid)
        for values in (self._read_simple_tombstones(self.comment_create_tombstone_path),
                       self._reanchor_tombstones()):
            for value in values.values():
                txid = value.get("transaction_id") if isinstance(value, dict) else None
                if isinstance(txid, str):
                    protected.add(txid)
        for path in self.directive_transaction_results_dir.glob("*.json"):
            if path.stem not in protected:
                path.unlink(missing_ok=True)
        if self.directive_transaction_results_dir.is_dir():
            _fsync_directory(self.directive_transaction_results_dir)
        exact, bloom = self._keyless_notification_filter_state()
        changed = False
        retire: list[str] = []
        with _pinned_receipt_directory(
                self.notification_receipts_dir, missing_ok=True) as directory_fd:
            if directory_fd is None:
                return
            for name in self._notification_receipt_names_at(directory_fd):
                txid = name.removesuffix(".json")
                if txid in protected:
                    continue
                receipt = _read_receipt_at(directory_fd, name)
                if (not isinstance(receipt, dict)
                        or set(receipt) != {"version", "transaction_id", "operation", "state", "event"}
                        or receipt.get("version") != 1
                        or receipt.get("transaction_id") != txid
                        or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", txid)
                        or receipt.get("operation") not in {"create", "reanchor"}
                        or receipt.get("state") not in {"pending", "committed"}):
                    raise CommentMutationError("unavailable")
                self._validate_notification_event(receipt.get("event"), txid,
                                                  receipt.get("operation"))
                if receipt["state"] != "committed":
                    continue
                exact.add(txid)
                changed = True
                retire.append(name)
            if changed:
                cap = max(0, int(self.KEYLESS_NOTIFICATION_FILTER_CAP))
                ordered = sorted(exact)
                evicted = ordered[:-cap] if cap else ordered
                exact = set(ordered[-cap:]) if cap else set()
                for txid in evicted:
                    for index in self._notification_bloom_indexes(txid):
                        bloom[index // 8] |= 1 << (index % 8)
                _require_safe_regular_destination(self.keyless_notification_filter_path)
                durable_atomic_write_text(self.keyless_notification_filter_path, json.dumps(
                    {"version": 3, "checkpoint_cursor": self.seq,
                       "transactions": sorted(exact), "bloom_bits": self.KEYLESS_NOTIFICATION_FILTER_BITS,
                       "bloom_hashes": self.KEYLESS_NOTIFICATION_FILTER_HASHES,
                       "bloom": bloom.hex()}, separators=(",", ":")))
                for name in retire:
                    try: os.unlink(name, dir_fd=directory_fd)
                    except FileNotFoundError: pass
                os.fsync(directory_fd)

    @property
    def comment_create_wal_dir(self) -> Path:
        return self._session_directory(".comment-create-wal")

    @property
    def comment_create_tombstone_path(self) -> Path:
        return self.comment_create_wal_dir / "tombstones.json"

    @property
    def comment_create_filter_path(self) -> Path:
        return self.comment_create_wal_dir / "tombstone-filter.json"

    def post_comment_idempotent(self, *, request: dict[str, Any], idempotency_key: str,
                                document: str | None = None,
                                concept: str, body: str, anchor: dict[str, Any],
                                actor: str, detail: dict[str, Any],
                                parent_id: str | None = None,
                                fault: str | None = None,
                                portable_import: dict[str, Any] | None = None,
                                ) -> tuple[dict[str, Any], bool]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", idempotency_key):
            raise CommentMutationError("invalid_request")
        fingerprint = hashlib.sha256(json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()).hexdigest()
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        wal_path = self.comment_create_wal_dir / f"{digest}.json"
        key_lock = self._session_directory(
            ".comment-create-wal/.key-locks") / digest
        with path_lock(key_lock):
            self._sweep_comment_create_wal_locked(time.time())
            wal = None
            if wal_path.is_file():
                try:
                    wal = json.loads(wal_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise CommentMutationError("unavailable") from exc
                self._validate_comment_create_wal(wal)
                if wal.get("key") != idempotency_key:
                    raise CommentMutationError("unavailable")
                if wal["fingerprint"] != fingerprint:
                    raise CommentMutationError("idempotency_conflict")
            replayed = wal is not None
            txid = wal["transaction_id"] if wal else new_id()
            tombstone = None
            if wal is None:
                tombstones = self._read_simple_tombstones(self.comment_create_tombstone_path)
                tombstone = tombstones.get(idempotency_key)
                if tombstone:
                    if tombstone.get("fingerprint") != fingerprint:
                        raise CommentMutationError("idempotency_conflict")
                    txid = tombstone.get("transaction_id")
                elif self._simple_filter_contains(self.comment_create_filter_path, idempotency_key):
                    raise CommentMutationError("idempotency_conflict")
            with DirectiveStore(self._session_io_dir).writer() as directive_tx:
                matching = self._directive_transaction_rows_locked(
                    directive_tx, "comment_create_transaction_id", txid)
            if matching:
                if len(matching) != 1:
                    raise CommentMutationError("idempotency_conflict")
                directive = matching[0]
                if (directive.get("comment_create_idempotency_key") != idempotency_key
                        or directive.get("comment_create_request_fingerprint") != fingerprint
                        or (wal is not None and wal.get("state") == "finalized" and (
                            directive.get("id") != wal.get("comment_id")
                            or directive.get("row_token") != wal.get("row_token")))):
                    raise CommentMutationError("idempotency_conflict")
                if wal is None:
                    wal = {"transaction_id": txid, "key": idempotency_key,
                           "fingerprint": fingerprint, "state": "prepared",
                           "created_at": time.time(),
                           "expires_at": time.time() + self.COMMENT_CREATE_WAL_TTL_SECONDS}
            else:
                if tombstone is not None:
                    response = tombstone.get("response")
                    if (tombstone.get("state") != "finalized"
                            or not isinstance(response, dict)
                            or response.get("ok") is not True
                            or not isinstance(response.get("comment"), dict)):
                        raise CommentMutationError("idempotency_conflict")
                    self._deliver_notification_event_once(
                        txid, "create", self._comment_event(
                            response["comment"], transaction_id=txid))
                    return dict(response["comment"]), True
                if wal and wal.get("state") == "finalized":
                    self._deliver_create_notification_locked(wal_path, wal)
                    return dict(wal["response"]["comment"]), True
                result = self._read_transaction_result_locked(txid) if wal else None
                if result is not None:
                    if (result.get("operation") != "create"
                            or result.get("fingerprint") != fingerprint
                            or result.get("key") != idempotency_key
                            or result.get("status") != 201):
                        raise CommentMutationError("idempotency_conflict")
                    directive = dict(result["response"]["comment"])
                else:
                    if wal is None:
                        wal = {"transaction_id": txid, "key": idempotency_key,
                               "fingerprint": fingerprint, "state": "prepared",
                               "created_at": time.time(),
                               "expires_at": time.time() + self.COMMENT_CREATE_WAL_TTL_SECONDS}
                        self._write_prepared_create_wal_locked(wal_path, wal)
                        if fault == "after_prepare":
                            raise OSError("injected comment create after prepare")
                    directive = self.post_comment(
                        concept=concept, body=body, anchor=anchor, actor=actor,
                        detail=detail, parent_id=parent_id,
                        document=document,
                        transaction_meta={
                            "comment_create_transaction_id": txid,
                            "comment_create_idempotency_key": idempotency_key,
                            "comment_create_request_fingerprint": fingerprint,
                        },
                        fault=fault if fault in {"after_notification_reservation", "after_notification_pending", "after_notification_append"} else None,
                        portable_import=portable_import,
                    )
                    result_response = {"ok": True, "comment": directive}
                    self._write_transaction_result_locked({
                        "operation": "create", "transaction_id": txid,
                        "fingerprint": fingerprint, "key": idempotency_key,
                        "comment_id": directive["id"], "row_token": directive["row_token"],
                        "status": 201, "response": result_response,
                    })
                    if fault == "after_append":
                        raise OSError("injected comment create after append")
            self._finalize_comment_create_wal_locked(
                wal_path, wal, {"ok": True, "comment": directive})
            if fault == "after_finalize":
                raise OSError("injected comment create after finalize")
            self._deliver_create_notification_locked(wal_path, wal)
            return directive, replayed or bool(matching)

    def _finalize_comment_create_wal_locked(self, wal_path: Path, wal: dict[str, Any],
                                            response: dict[str, Any]) -> None:
        """Durably mark one create WAL finalized with its exact response.

        Recovery cannot infer that post_comment's event append completed.
        Keep the durable bit false and reconcile by transaction id; the
        delivery helper finds an existing event or appends exactly one.
        Callers hold the key lock or the WAL maintenance lock.
        """
        directive = response["comment"]
        wal.update(state="finalized", status=201, response=response,
                   comment_id=directive["id"], row_token=directive["row_token"],
                   notification_delivered=False)
        durable_atomic_write_text(wal_path, json.dumps(wal, separators=(",", ":"), ensure_ascii=False))

    def _reconcile_prepared_create_locked(self, wal_path: Path,
                                          wal: dict[str, Any]) -> dict[str, Any] | None:
        """Finalize one prepared create WAL from durable evidence, else ``None``.

        Evidence is the transaction result sidecar or exactly one physical
        directive row tagged with the WAL's transaction id (whose key and
        fingerprint must match). The caller holds the WAL maintenance lock;
        this takes only the directives transaction, so the key → maintenance →
        directives order stays a subsequence of the global lock order.
        Notification delivery is the caller's responsibility.
        """
        result = self._read_transaction_result_locked(wal["transaction_id"])
        with DirectiveStore(self._session_io_dir).writer() as directive_tx:
            physical = self._directive_transaction_rows_locked(
                directive_tx, "comment_create_transaction_id", wal["transaction_id"])
        if len(physical) > 1:
            raise CommentMutationError("idempotency_conflict")
        if physical:
            directive = physical[0]
            if (directive.get("comment_create_request_fingerprint") != wal["fingerprint"]
                    or directive.get("comment_create_idempotency_key") != wal["key"]):
                raise CommentMutationError("idempotency_conflict")
            if result is None:
                result = {"operation": "create", "transaction_id": wal["transaction_id"],
                          "fingerprint": wal["fingerprint"], "key": wal["key"],
                          "comment_id": directive["id"], "row_token": directive["row_token"],
                          "status": 201, "response": {"ok": True, "comment": directive}}
                self._write_transaction_result_locked(result)
        if result is None:
            return None
        if (result.get("operation") != "create"
                or result.get("fingerprint") != wal["fingerprint"]
                or result.get("key") != wal["key"]
                or result.get("status") != 201):
            raise CommentMutationError("idempotency_conflict")
        self._finalize_comment_create_wal_locked(wal_path, wal, result["response"])
        return result["response"]["comment"]

    def inspect_comment_create_key(self, *, request: dict[str, Any],
                                   idempotency_key: str) -> tuple[str, dict[str, Any] | None]:
        """Classify a create key WITHOUT side effects (no sweep, no delivery).

        Returns ``(status, comment)`` where status is ``new``, ``prepared``,
        ``finalized`` (comment is the finalized response row), ``conflict``
        (same key, different fingerprint, or an unusable retained outcome) or
        ``retired`` (only the compact tombstone filter remembers the key).
        Used by the portable-feedback preview; it takes the key lock only so a
        concurrent create cannot hand it a torn WAL.
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", idempotency_key):
            raise CommentMutationError("invalid_request")
        fingerprint = hashlib.sha256(json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()).hexdigest()
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        wal_path = self.comment_create_wal_dir / f"{digest}.json"
        key_lock = self._session_directory(
            ".comment-create-wal/.key-locks") / digest
        with path_lock(key_lock):
            if wal_path.is_file():
                try:
                    wal = json.loads(wal_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise CommentMutationError("unavailable") from exc
                self._validate_comment_create_wal(wal)
                if wal.get("key") != idempotency_key:
                    raise CommentMutationError("unavailable")
                if wal.get("fingerprint") != fingerprint:
                    return "conflict", None
                if wal.get("state") == "finalized":
                    return "finalized", dict(wal["response"]["comment"])
                return "prepared", None
            tombstone = self._read_simple_tombstones(
                self.comment_create_tombstone_path).get(idempotency_key)
            if tombstone is not None:
                response = tombstone.get("response") if isinstance(tombstone, dict) else None
                if (tombstone.get("fingerprint") != fingerprint
                        or tombstone.get("state") != "finalized"
                        or not isinstance(response, dict) or response.get("ok") is not True
                        or not isinstance(response.get("comment"), dict)):
                    return "conflict", None
                return "finalized", dict(response["comment"])
            if self._simple_filter_contains(self.comment_create_filter_path, idempotency_key):
                return "retired", None
            return "new", None

    def replay_finalized_comment_create(self, *, request: dict[str, Any],
                                        idempotency_key: str,
                                        reconcile_prepared: bool = False) -> dict[str, Any] | None:
        """Return a durable finalized create outcome without consulting its concept.

        ``None`` means the key is genuinely new or only prepared/uncertain; callers
        must then perform current-resource validation.  Key/fingerprint conflicts
        are terminal and deliberately precede resource diagnostics.

        With ``reconcile_prepared`` a prepared WAL whose row (or transaction
        result) is already durable is finalized here, under the key lock and
        then the WAL maintenance lock, and replayed — so a crash after append
        followed by target deletion replays instead of reporting a missing
        target. No concept lock is held on this path.
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", idempotency_key):
            raise CommentMutationError("invalid_request")
        fingerprint = hashlib.sha256(json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()).hexdigest()
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        wal_path = self.comment_create_wal_dir / f"{digest}.json"
        key_lock = self._session_directory(
            ".comment-create-wal/.key-locks") / digest
        with path_lock(key_lock):
            if wal_path.is_file():
                try:
                    wal = json.loads(wal_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise CommentMutationError("unavailable") from exc
                self._validate_comment_create_wal(wal)
                if wal.get("key") != idempotency_key:
                    raise CommentMutationError("unavailable")
                if wal.get("fingerprint") != fingerprint:
                    raise CommentMutationError("idempotency_conflict")
                if wal.get("state") == "finalized":
                    self._deliver_create_notification_locked(wal_path, wal)
                    return dict(wal["response"]["comment"])
                if not reconcile_prepared:
                    return None
                with path_lock(self.comment_create_wal_dir / ".maintenance"):
                    # A concurrent sweep (holding another key's lock) may have
                    # finalized or retired this record since it was read.
                    if wal_path.is_file():
                        try:
                            wal = json.loads(wal_path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError) as exc:
                            raise CommentMutationError("unavailable") from exc
                        self._validate_comment_create_wal(wal)
                        if wal.get("key") != idempotency_key or wal.get("fingerprint") != fingerprint:
                            raise CommentMutationError("unavailable")
                        if wal.get("state") == "prepared":
                            if self._reconcile_prepared_create_locked(wal_path, wal) is None:
                                return None
                        self._deliver_create_notification_locked(wal_path, wal)
                        return dict(wal["response"]["comment"])
                # Retired by that sweep: the sweep writes the tombstone before
                # unlinking the WAL, so fall through to the same tombstone /
                # filter classification a retired key gets on first read.
            tombstone = self._read_simple_tombstones(
                self.comment_create_tombstone_path).get(idempotency_key)
            if tombstone is not None:
                if tombstone.get("fingerprint") != fingerprint:
                    raise CommentMutationError("idempotency_conflict")
                response = tombstone.get("response")
                if (tombstone.get("state") != "finalized" or not isinstance(response, dict)
                        or response.get("ok") is not True
                        or not isinstance(response.get("comment"), dict)):
                    raise CommentMutationError("idempotency_conflict")
                txid = tombstone.get("transaction_id")
                if not isinstance(txid, str):
                    raise CommentMutationError("idempotency_conflict")
                self._deliver_notification_event_once(
                    txid, "create", self._comment_event(
                        response["comment"], transaction_id=txid))
                return dict(response["comment"])
            if self._simple_filter_contains(self.comment_create_filter_path, idempotency_key):
                raise CommentMutationError("idempotency_conflict")
            return None

    def _write_prepared_create_wal_locked(self, wal_path: Path, wal: dict[str, Any]) -> None:
        with path_lock(self.comment_create_wal_dir / ".maintenance"):
            live = [path for path in self.comment_create_wal_dir.glob("*.json")
                    if path not in {self.comment_create_tombstone_path,
                                    self.comment_create_filter_path}]
            if len(live) >= self.COMMENT_CREATE_WAL_CAP:
                raise CommentMutationError("unavailable")
            durable_atomic_write_text(wal_path, json.dumps(wal, separators=(",", ":")))

    def _deliver_create_notification_locked(self, wal_path: Path, wal: dict[str, Any]) -> None:
        if wal.get("notification_delivered") is True:
            return
        txid = wal["transaction_id"]
        directive = wal["response"]["comment"]
        self._deliver_notification_event_once(
            txid, "create", self._comment_event(directive, transaction_id=txid))
        wal["notification_delivered"] = True
        durable_atomic_write_text(wal_path, json.dumps(wal, separators=(",", ":"), ensure_ascii=False))

    @staticmethod
    def _validate_comment_create_wal(record: Any) -> None:
        if (not isinstance(record, dict)
                or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", str(record.get("transaction_id", "")))
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", str(record.get("key", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("fingerprint", "")))
                or record.get("state") not in {"prepared", "finalized"}
                or isinstance(record.get("created_at"), bool)
                or not isinstance(record.get("created_at"), (int, float))
                or not math.isfinite(float(record["created_at"]))
                or not isinstance(record.get("expires_at"), (int, float))
                or isinstance(record.get("expires_at"), bool)
                or not math.isfinite(float(record["expires_at"]))
                or float(record["expires_at"]) < float(record["created_at"])):
            raise CommentMutationError("unavailable")
        if record["state"] == "finalized":
            response = record.get("response")
            if (record.get("status") != 201 or not isinstance(response, dict)
                    or response.get("ok") is not True
                    or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", str(record.get("comment_id", "")))
                    or not re.fullmatch(r"[!-~]{1,128}", str(record.get("row_token", "")))
                    or not isinstance(response.get("comment"), dict)
                    or response["comment"].get("id") != record["comment_id"]
                    or response["comment"].get("row_token") != record["row_token"]):
                raise CommentMutationError("unavailable")

    @staticmethod
    def _read_simple_tombstones(path: Path) -> dict[str, dict[str, Any]]:
        if not path.is_file(): return {}
        try: value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc: raise CommentMutationError("unavailable") from exc
        if not isinstance(value, dict): raise CommentMutationError("unavailable")
        return value

    def _simple_filter_contains(self, path: Path, key: str) -> bool:
        if not path.is_file(): return False
        try: bits = bytes.fromhex(json.loads(path.read_text())["bits"])
        except Exception as exc: raise CommentMutationError("unavailable") from exc
        return all(bits[pos // 8] & (1 << (pos % 8)) for pos in self._reanchor_filter_positions(key))

    def _sweep_comment_create_wal_locked(self, now: float) -> None:
        with path_lock(self.comment_create_wal_dir / ".maintenance"):
            self.comment_create_wal_dir.mkdir(parents=True, exist_ok=True)
            records = []
            for path in self.comment_create_wal_dir.glob("*.json"):
                if path.name in {"tombstones.json", "tombstone-filter.json"}: continue
                try: row = json.loads(path.read_text())
                except Exception as exc: raise CommentMutationError("unavailable") from exc
                self._validate_comment_create_wal(row); records.append((path, row))
            for path, row in records:
                if row["state"] != "prepared":
                    continue
                if self._reconcile_prepared_create_locked(path, row) is not None:
                    self._deliver_create_notification_locked(path, row)
            finalized = sorted(((p, r) for p, r in records if r["state"] == "finalized"),
                               key=lambda item: item[1]["created_at"])
            evict = {p for p, r in finalized if now > r["expires_at"]}
            overflow = max(0, len(records) - len(evict) - self.COMMENT_CREATE_WAL_CAP)
            evict.update(p for p, _ in [(p, r) for p, r in finalized if p not in evict][:overflow])
            for p, r in records:
                if r["state"] == "prepared" and now > r["expires_at"] + self.COMMENT_CREATE_PREPARED_GRACE_SECONDS:
                    evict.add(p)
            tombstones = self._read_simple_tombstones(self.comment_create_tombstone_path)
            for p, r in records:
                if p in evict:
                    if r["state"] == "finalized" and r.get("notification_delivered") is not True:
                        self._deliver_create_notification_locked(p, r)
                    tombstones[r["key"]] = {"transaction_id": r["transaction_id"],
                                            "fingerprint": r["fingerprint"], "evicted_at": now,
                                            "state": r["state"],
                                            "status": r.get("status"),
                                            "response": r.get("response"),
                                            "notification_delivered": r.get("notification_delivered", False)}
            ordered = sorted(tombstones.items(), key=lambda item: item[1].get("evicted_at", 0))
            compact_keys = {key for key, row in ordered
                            if now - float(row.get("evicted_at", 0)) > self.COMMENT_CREATE_TOMBSTONE_TTL_SECONDS}
            survivors = [(key, row) for key, row in ordered if key not in compact_keys]
            if len(survivors) > self.COMMENT_CREATE_TOMBSTONE_CAP:
                overflow_rows = survivors if self.COMMENT_CREATE_TOMBSTONE_CAP == 0 else survivors[:-self.COMMENT_CREATE_TOMBSTONE_CAP]
                compact_keys.update(key for key, _ in overflow_rows)
            compact = [(key, tombstones[key]) for key in compact_keys]
            if compact:
                size = self.REANCHOR_FILTER_BITS // 8
                bits = bytearray(size)
                if self.comment_create_filter_path.is_file():
                    bits = bytearray(bytes.fromhex(json.loads(self.comment_create_filter_path.read_text())["bits"]))
                for key, _ in compact:
                    for pos in self._reanchor_filter_positions(key): bits[pos // 8] |= 1 << (pos % 8)
                    tombstones.pop(key, None)
                durable_atomic_write_text(self.comment_create_filter_path,
                    json.dumps({"version": 1, "bits": bytes(bits).hex()}, separators=(",", ":")))
            durable_atomic_write_text(self.comment_create_tombstone_path,
                                      json.dumps(tombstones, separators=(",", ":")))
            for path in evict: path.unlink(missing_ok=True)
            self._cleanup_transaction_sidecars_locked()
            _fsync_directory(self.comment_create_wal_dir)

    def list_comments(self, *, state: str | None = None,
                      concept: str | None = None, document: str | None = None) -> list[dict[str, Any]]:
        """List directives, filtered by state / concept (§9.1).

        ``directives.jsonl`` is append-only: a lifecycle transition appends a
        new full record for the same id. We keep only the **latest** record
        per id (last write wins), so a resolved comment does not also show as
        open.

        Compatibility migration: older records with ``state == "archived"``
        (pre-separate-track archive) are normalized to ``archived=True,
        state="open"`` on read so the UI's archive filter works uniformly.
        """
        from .documents import document_path, comment_document
        if document is not None:
            document = document_path(document)
            if concept is not None:
                raise ValueError('choose concept or document filter, not both')
        if self.session_authority is None:
            # Preserve the directive adapter's public capability diagnostic
            # before a fresh session would otherwise be reported as missing.
            from . import directive_store as directive_store_module
            if not directive_store_module._capable():
                raise DirectiveStoreError("safe directive authority is unavailable")
            return []
        latest: dict[str, dict[str, Any]] = {}
        try:
            with DirectiveStore(self._session_io_dir).writer() as directive_tx:
                for row in self._read_directive_rows_locked(directive_tx):
                    latest[row.get("id", "")] = row
        except DirectiveStoreError as exc:
            # Preserve the historical public list contract only for proven
            # malformed JSON/invalid directive rows.  Capability, pathname,
            # lock, checkpoint, and other I/O failures remain unavailable.
            if str(exc) == "corrupt directive row":
                raise ValueError("corrupt JSONL") from exc
            raise
        out: list[dict[str, Any]] = []
        for row in latest.values():
            row = _normalize_comment_row(row)
            row.setdefault("row_token", self._legacy_row_token(row))
            if state and row.get("state") != state:
                continue
            if concept and row.get("concept") != concept:
                continue
            if document and comment_document(row) != document:
                continue
            out.append(row)
        return out

    def comment_summary(
        self,
        *,
        graph_concepts: set[str] | frozenset[str],
    ) -> CommentSummaryV1:
        """Return the portable sanitized summary from this Studio's authority."""
        return read_comment_summary(self._session_io_dir, graph_concepts=graph_concepts)

    def get_comment(self, comment_id: str) -> dict[str, Any] | None:
        # Append-only feed: the latest record for the id is the current state.
        latest: dict[str, Any] | None = None
        with DirectiveStore(self._session_io_dir).writer() as directive_tx:
            for row in self._read_directive_rows_locked(directive_tx):
                if row.get("id") == comment_id:
                    latest = row
        if latest is None:
            return None
        normalized = _normalize_comment_row(latest)
        normalized.setdefault("row_token", self._legacy_row_token(latest))
        return normalized

    @staticmethod
    def _legacy_row_token(row: dict[str, Any]) -> str:
        raw = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "legacy:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    _COMMENT_FIELD_UNSET = object()

    def update_comment(self, comment_id: str, *, state: str | None = None,
                       claimed_by: Any = _COMMENT_FIELD_UNSET,
                       resolved_activity: list | None = None,
                       reply: str | None = None,
                       archived: bool | None = None,
                       summary: str | None = None,
                       request_summary: str | None = None,
                       body: str | None = None,
                       mutation_actor: str | None = None) -> dict[str, Any] | None:
        """Transition a comment's lifecycle (§9.1): claim / resolve / dismiss.

        ``directives.jsonl`` is append-only; a transition appends a new record
        for the same ``id`` carrying the new state, and :meth:`list_comments`
        / :meth:`get_comment` return the latest record per id.

        ``archived`` is a SEPARATE track from ``state``. Archiving a
        comment does NOT change its open/resolved/dismissed state — it only
        flips a boolean that the UI uses to hide the thread. Unarchiving
        restores the prior visibility without touching state. ``summary`` is
        a short agent-authored blurb (set on claim/resolve) shown in the
        collapsed preview. ``updated_at`` is bumped on every transition;
        ``ts`` is preserved as the creation timestamp.

        TWO summary fields are semantically distinct:
          * ``request_summary`` — short of what was ASKED. Set explicitly
            by ``okf comment-claim --summary`` (claim-time = "what the
            user wants"). Auto-derived from body if never set (see
            ``_normalize_comment_row``).
          * ``summary`` — short of what the agent DID. Set by
            ``okf comment-resolve --summary`` (resolve-time = "what the
            agent did"). Empty until resolve.
        The collapsed preview shows BOTH so the panel reads as a
        ask→done status board.
        """
        if not isinstance(comment_id, str) or not re.fullmatch(
                r"[0-9A-HJKMNP-TV-Z]{10,26}", comment_id):
            return None
        # A lookup for an absent id is read-only and must not manufacture the
        # session directory or its directive lock authority.
        if not self._adopt_existing_session():
            return None
        probe = self.get_comment(comment_id)
        if probe is None:
            return None
        locks = [self._comment_lock_path(comment_id)]
        parent_id = probe.get("parent_id")
        if isinstance(parent_id, str) and re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", parent_id):
            locks.append(self._comment_lock_path(parent_id))
        with path_locks(locks), DirectiveStore(self._session_io_dir).writer() as directive_tx:
            return self._update_comment_locked(
                directive_tx, comment_id, state=state, claimed_by=claimed_by,
                resolved_activity=resolved_activity, reply=reply,
                archived=archived, summary=summary,
                request_summary=request_summary, body=body,
                mutation_actor=mutation_actor,
            )

    def _comment_lock_path(self, comment_id: str) -> Path:
        if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", comment_id):
            raise ValueError("invalid comment id")
        return self._session_directory(".comment-locks") / comment_id

    def _update_comment_locked(self, directive_tx: DirectiveTransaction,
                               comment_id: str, *, state: str | None = None,
                               claimed_by: Any = _COMMENT_FIELD_UNSET,
                               resolved_activity: list | None = None,
                               reply: str | None = None,
                               archived: bool | None = None,
                               summary: str | None = None,
                               request_summary: str | None = None,
                               body: str | None = None,
                               mutation_actor: str | None = None) -> dict[str, Any] | None:
        """Merge lifecycle-owned fields after reloading under the comment lock."""
        current = next((row for row in reversed(directive_tx.rows())
                        if row.get("id") == comment_id), None)
        if current is None:
            return None
        current = _normalize_comment_row(current)
        current.setdefault("row_token", self._legacy_row_token(current))
        # Claim/resolve APIs already carry the acting label in claimed_by.
        # Do not derive it from the resulting row on other operations.
        if (mutation_actor is None and state in {"claimed", "resolved"}
                and claimed_by is not self._COMMENT_FIELD_UNSET):
            mutation_actor = claimed_by
        if state is not None and state != "resolved" and current.get("parent_id"):
            parent = next((row for row in reversed(directive_tx.rows())
                           if row.get("id") == current["parent_id"]), None)
            if parent and parent.get("archived"):
                self._update_comment_locked(directive_tx, current["parent_id"], archived=False,
                                            mutation_actor=mutation_actor)
        updated = dict(current)
        self._checkpoint_row_transaction_result_locked(current)
        # Transaction metadata identifies the exact operation result row.  A
        # later lifecycle/body/archive row is a distinct mutation and must not
        # impersonate create/re-anchor transaction identity.  The finalized
        # WAL retains the durable operation proof and exact response.
        for name in (
            "comment_create_transaction_id", "comment_create_idempotency_key",
            "comment_create_request_fingerprint", "reanchor_transaction_id",
            "reanchor_request_fingerprint", "reanchor_idempotency_key",
            "reanchor_concept_content_rev",
        ):
            updated.pop(name, None)
        if state is not None:
            updated["state"] = state
        if claimed_by is not self._COMMENT_FIELD_UNSET:
            updated["claimed_by"] = claimed_by
        previous_owner = current.get("claimed_by")
        next_owner = updated.get("claimed_by")
        next_state = updated.get("state")
        if next_state == "claimed":
            if current.get("state") != "claimed" or next_owner != previous_owner:
                updated["claimed_at"] = _now_iso()
            elif not isinstance(updated.get("claimed_at"), str):
                raise CommentMutationError("unavailable")
        else:
            updated.pop("claimed_at", None)
        if resolved_activity is not None:
            updated["resolved_activity"] = list(resolved_activity)
        if reply is not None:
            updated["reply"] = reply
        if archived is not None:
            updated["archived"] = bool(archived)
        if summary is not None:
            updated["summary"] = summary
        if request_summary is not None:
            updated["request_summary"] = request_summary
        if body is not None and body != updated.get("body"):
            # User-side comment edit (the enter-too-soon fix). The edit
            # bumps updated_at, so an agent blocked in ``wait`` re-receives
            # the still-open comment with the corrected body. Re-derive
            # request_summary from the new body unless this call also sets
            # it explicitly — the stored short must describe the CURRENT
            # ask, not the pre-edit one.
            updated["body"] = body
            if request_summary is None:
                updated["request_summary"] = ""
        # Preserve creation ts (including legacy absence); only this mutation's
        # time is known. Never manufacture historical authorship or timestamps.
        updated["updated_at"] = _now_iso()
        updated["row_token"] = "r2:" + new_id()
        # Ensure request_summary is present (auto-derive on write so the
        # stored record is self-contained; the read-side normalize is the
        # backstop for older rows).
        if not updated.get("request_summary"):
            body = (updated.get("body") or "").strip()
            if body:
                import re as _re
                parts = _re.split(r"[.!?\n]", body, maxsplit=1)
                first = (parts[0] if parts else body).strip() or body
                if len(first) > 100:
                    first = first[:100].rstrip() + "\u2026"
                updated["request_summary"] = first
            else:
                updated["request_summary"] = ""
        self._append_directive_locked(directive_tx, updated)
        self.append_event(self._comment_event(
            updated, operation="lifecycle", mutation_actor=mutation_actor))
        return updated

    def update_comment_external(self, comment_id: str, *, expected_row_token: str,
                                state: str | None = None, archived: bool | None = None,
                                body: str | None = None) -> tuple[dict[str, Any] | None, str | None, list[str]]:
        """CAS mutation for public lifecycle/body/archive requests.

        All state-dependent guards and the append execute in one comment ->
        directives transaction. Trusted CLI/library callers continue using
        :meth:`update_comment` for latest-regardless semantics.
        """
        if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", comment_id):
            return None, "invalid_id", []
        probe = self.get_comment(comment_id)
        if probe is None:
            return None, "not_found", []
        locks = [self._comment_lock_path(comment_id)]
        parent_id = probe.get("parent_id")
        if isinstance(parent_id, str) and re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", parent_id):
            locks.append(self._comment_lock_path(parent_id))
        with path_locks(locks), DirectiveStore(self._session_io_dir).writer() as directive_tx:
            latest: dict[str, dict[str, Any]] = {}
            for row in self._read_directive_rows_locked(directive_tx):
                latest[row.get("id", "")] = _normalize_comment_row(row)
            current = latest.get(comment_id)
            if current is None:
                return None, "not_found", []
            current.setdefault("row_token", self._legacy_row_token(current))
            if current.get("row_token") != expected_row_token:
                return None, "stale", []
            if state is not None:
                transition = (current.get("state", "open"), state)
                if transition not in {("open", "dismissed"), ("open", "claimed"),
                                      ("dismissed", "open")}:
                    return None, "invalid_transition", []
            if body is not None and current.get("state") in {"resolved", "dismissed"}:
                return None, "body_state", []
            if state == "dismissed" and current.get("claimed_by"):
                return None, "claimed", []
            unresolved: list[str] = []
            if archived is True:
                if current.get("parent_id"):
                    return None, "archive_reply", []
                thread = [row for row in latest.values()
                          if row.get("id") == comment_id or row.get("parent_id") == comment_id]
                unresolved = [row.get("id", "") for row in thread if row.get("state") != "resolved"]
                if unresolved:
                    return None, "archive_unresolved", unresolved
            updated = self._update_comment_locked(
                directive_tx, comment_id, state=state,
                claimed_by=None if state == "open" else self._COMMENT_FIELD_UNSET,
                archived=archived, body=body, mutation_actor="user"
            )
            return updated, None, []

    @property
    def reanchor_wal_dir(self) -> Path:
        return self._session_directory(".comment-reanchor-wal")

    def reanchor_comment(self, *, comment_id: str, expected_row_token: str,
                         expected_concept_content_rev: str, anchor: dict[str, Any],
                         idempotency_key: str, canonical_source: str,
                         expected_concept: str | None = None,
                         fault: str | None = None) -> tuple[dict[str, Any], bool, int]:
        """Crash-recoverable append of one anchor-owned full comment row."""
        fingerprint = hashlib.sha256(json.dumps({
            "comment_id": comment_id, "expected_row_token": expected_row_token,
            "expected_concept_content_rev": expected_concept_content_rev,
            "anchor": anchor,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        wal_path = self.reanchor_wal_dir / (hashlib.sha256(idempotency_key.encode()).hexdigest() + ".json")
        key_lock = self._session_directory(
            ".comment-reanchor-wal/.key-locks") / hashlib.sha256(
                idempotency_key.encode()).hexdigest()
        # Fixed global order: endpoint key -> comment -> directives snapshot.
        with path_lock(key_lock), path_lock(self._comment_lock_path(comment_id)):
            self._sweep_reanchor_wal_locked(now=time.time())
            wal = None
            if wal_path.is_file():
                try:
                    wal = json.loads(wal_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise CommentMutationError("unavailable") from exc
                self._validate_reanchor_wal(wal)
                if wal.get("key") != idempotency_key or wal.get("fingerprint") != fingerprint:
                    raise CommentMutationError("idempotency_conflict")
                if wal.get("state") == "finalized":
                    self._deliver_reanchor_notification_locked(wal_path, wal)
                    return dict(wal["response"]), True, int(wal.get("status", 200))
                txid = wal.get("transaction_id")
                with DirectiveStore(self._session_io_dir).writer() as directive_tx:
                    matching = self._directive_transaction_rows_locked(
                        directive_tx, "reanchor_transaction_id", txid)
                if matching:
                    if len(matching) != 1:
                        raise CommentMutationError("idempotency_conflict")
                    row = matching[-1]
                    if (row.get("reanchor_request_fingerprint") != fingerprint
                            or row.get("reanchor_idempotency_key") != idempotency_key
                            or row.get("id") != comment_id):
                        raise CommentMutationError("idempotency_conflict")
                    response = self._reanchor_response(
                        row, row.get("reanchor_concept_content_rev", expected_concept_content_rev)
                    )
                    notification_event = self._comment_event(
                        row, transaction_id=txid, operation="reanchor")
                    wal.update(state="finalized", response=response,
                               resulting_row_token=row.get("row_token"), status=200,
                               concept=row.get("concept", ""),
                               notification_event=notification_event,
                               notification_delivered=False)
                    durable_atomic_write_text(wal_path, json.dumps(wal, separators=(",", ":"), ensure_ascii=False))
                    self._deliver_reanchor_notification_locked(wal_path, wal)
                    return response, True, int(wal.get("status", 200))
                result = self._read_transaction_result_locked(txid)
                if result is not None:
                    if (result.get("operation") != "reanchor"
                            or result.get("fingerprint") != fingerprint
                            or result.get("key") != idempotency_key
                            or result.get("comment_id") != comment_id
                            or result.get("status") != 200):
                        raise CommentMutationError("idempotency_conflict")
                    response = dict(result["response"])
                    notification_event = self._validate_notification_event(
                        result.get("notification_event"), txid, "reanchor")
                    wal.update(state="finalized", response=response,
                               resulting_row_token=result["row_token"], status=200,
                               concept=result.get("concept", ""),
                               notification_event=notification_event,
                               notification_delivered=False)
                    durable_atomic_write_text(wal_path, json.dumps(
                        wal, separators=(",", ":"), ensure_ascii=False))
                    self._deliver_reanchor_notification_locked(wal_path, wal)
                    return response, True, 200
            else:
                tombstone = self._reanchor_tombstones().get(idempotency_key)
                if tombstone is not None:
                    if (tombstone.get("fingerprint") != fingerprint
                            or tombstone.get("comment_id") != comment_id):
                        raise CommentMutationError("idempotency_conflict")
                    response = tombstone.get("response")
                    if not isinstance(response, dict) or response.get("ok") is not True:
                        # Prepared/legacy tombstones represent uncertain
                        # outcomes and remain deliberately fail closed.
                        raise CommentMutationError("idempotency_conflict")
                    self._deliver_reanchor_tombstone_notification_locked(idempotency_key, tombstone)
                    return dict(response), True, int(tombstone.get("status", 200))
                if self._reanchor_filter_contains(idempotency_key):
                    raise CommentMutationError("idempotency_conflict")
            current = self.get_comment(comment_id)
            if current is None:
                raise CommentMutationError("comment_not_found")
            if current.get("row_token") != expected_row_token:
                raise CommentMutationError("comment_conflict")
            if expected_concept is not None and current.get("concept") != expected_concept:
                raise CommentMutationError("comment_conflict")
            if concept_content_rev(canonical_source) != expected_concept_content_rev:
                raise CommentMutationError("concept_conflict")
            parse_v2_anchor(anchor, public=True)
            pos = anchor["position"]
            if canonical_source[pos["start"]:pos["end"]] != anchor["exact"]:
                raise CommentMutationError("invalid_anchor")
            try:
                canonical = capture_anchor(canonical_source, pos["start"], pos["end"])
            except Exception as exc:
                raise CommentMutationError("invalid_anchor") from exc
            if any(canonical[name] != anchor[name] for name in canonical):
                raise CommentMutationError("invalid_anchor")
            txid = wal.get("transaction_id") if wal else new_id()
            if wal is None:
                if fault == "before_wal":
                    raise OSError("injected before wal")
                now = time.time()
                wal = {"transaction_id": txid, "key": idempotency_key,
                       "fingerprint": fingerprint, "comment_id": comment_id,
                       "expected_row_token": expected_row_token,
                       "expected_concept_content_rev": expected_concept_content_rev,
                       "state": "prepared", "created_at": now, "expires_at": now + 86400}
                self._write_prepared_reanchor_wal_locked(wal_path, wal)
            if fault == "after_prepare":
                raise OSError("injected after prepare")
            # Preserve the create result before this distinct transaction row
            # stops carrying create identity. Unknown prefix extensions remain.
            self._checkpoint_row_transaction_result_locked(current)
            updated = dict(current)
            for name in (
                "comment_create_transaction_id", "comment_create_idempotency_key",
                "comment_create_request_fingerprint", "reanchor_transaction_id",
                "reanchor_request_fingerprint", "reanchor_idempotency_key",
                "reanchor_concept_content_rev",
            ):
                updated.pop(name, None)
            prior_anchor = current.get("anchor") if isinstance(current.get("anchor"), dict) else {}
            merged_anchor = json.loads(json.dumps(prior_anchor))
            # Legacy identity fields are replaced by the v2 schema rather than
            # retained as misleading extensions.
            merged_anchor.pop("ref", None)
            if merged_anchor.get("kind") == "text":
                merged_anchor.pop("kind", None)
            for name, value in anchor.items():
                if isinstance(value, dict) and isinstance(merged_anchor.get(name), dict):
                    merged_anchor[name].update(value)
                else:
                    merged_anchor[name] = value
            updated.update(anchor=merged_anchor, row_token="r2:" + new_id(),
                           ts=current.get("ts") or _now_iso(), updated_at=_now_iso(),
                           reanchor_transaction_id=txid,
                           reanchor_request_fingerprint=fingerprint,
                           reanchor_idempotency_key=idempotency_key,
                           reanchor_concept_content_rev=expected_concept_content_rev)
            with DirectiveStore(self._session_io_dir).writer() as directive_tx:
                self._append_directive_locked(directive_tx, updated)
            response = self._reanchor_response(updated, expected_concept_content_rev)
            notification_event = self._comment_event(
                updated, transaction_id=txid, operation="reanchor")
            self._write_transaction_result_locked({
                "operation": "reanchor", "transaction_id": txid,
                "fingerprint": fingerprint, "key": idempotency_key,
                "comment_id": comment_id, "row_token": updated["row_token"],
                "status": 200, "response": response,
                "concept": updated.get("concept", ""),
                "notification_event": notification_event,
            })
            if fault == "after_append":
                raise OSError("injected after append")
            wal.update(state="finalized", response=response, status=200,
                       resulting_row_token=updated["row_token"],
                       concept=updated.get("concept", ""),
                       notification_event=notification_event,
                       notification_delivered=False)
            durable_atomic_write_text(wal_path, json.dumps(wal, separators=(",", ":"), ensure_ascii=False))
            if fault == "after_finalize":
                raise OSError("injected after finalize")
            self._deliver_reanchor_notification_locked(wal_path, wal, fault=fault)
            return response, False, 200

    def replay_finalized_reanchor(self, *, comment_id: str, expected_row_token: str,
                                  expected_concept_content_rev: str,
                                  anchor: dict[str, Any], idempotency_key: str
                                  ) -> tuple[dict[str, Any], int] | None:
        """Reconcile a finalized re-anchor before current concept access."""
        fingerprint = hashlib.sha256(json.dumps({
            "comment_id": comment_id, "expected_row_token": expected_row_token,
            "expected_concept_content_rev": expected_concept_content_rev,
            "anchor": anchor,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        wal_path = self.reanchor_wal_dir / f"{digest}.json"
        key_lock = self._session_directory(
            ".comment-reanchor-wal/.key-locks") / digest
        with path_lock(key_lock):
            if wal_path.is_file():
                try:
                    wal = json.loads(wal_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise CommentMutationError("unavailable") from exc
                self._validate_reanchor_wal(wal)
                if wal.get("key") != idempotency_key or wal.get("fingerprint") != fingerprint:
                    raise CommentMutationError("idempotency_conflict")
                if wal.get("state") == "finalized":
                    self._deliver_reanchor_notification_locked(wal_path, wal)
                    return dict(wal["response"]), int(wal.get("status", 200))
                return None
            tombstone = self._reanchor_tombstones().get(idempotency_key)
            if tombstone is not None:
                if (tombstone.get("fingerprint") != fingerprint
                        or tombstone.get("comment_id") != comment_id):
                    raise CommentMutationError("idempotency_conflict")
                response = tombstone.get("response")
                if not isinstance(response, dict) or response.get("ok") is not True:
                    raise CommentMutationError("idempotency_conflict")
                self._deliver_reanchor_tombstone_notification_locked(idempotency_key, tombstone)
                return dict(response), int(tombstone.get("status", 200))
            if self._reanchor_filter_contains(idempotency_key):
                raise CommentMutationError("idempotency_conflict")
            return None

    def _write_prepared_reanchor_wal_locked(self, wal_path: Path, wal: dict[str, Any]) -> None:
        with path_lock(self.reanchor_wal_dir / ".maintenance"):
            live = [path for path in self.reanchor_wal_dir.glob("*.json")
                    if path not in {self.reanchor_tombstone_path, self.reanchor_filter_path}]
            if len(live) >= self.REANCHOR_WAL_CAP:
                raise CommentMutationError("unavailable")
            durable_atomic_write_text(wal_path, json.dumps(wal, separators=(",", ":"), ensure_ascii=False))

    def _deliver_reanchor_notification_locked(self, wal_path: Path, wal: dict[str, Any],
                                               fault: str | None = None) -> None:
        if wal.get("notification_delivered") is True:
            return
        txid = wal.get("transaction_id")
        if not isinstance(txid, str):
            raise CommentMutationError("unavailable")
        event = self._validate_notification_event(wal.get("notification_event"), txid, "reanchor")
        self._deliver_notification_event_once(txid, "reanchor", event, fault=fault)
        wal["notification_delivered"] = True
        durable_atomic_write_text(wal_path, json.dumps(wal, separators=(",", ":"), ensure_ascii=False))

    def _deliver_reanchor_tombstone_notification_locked(self, key: str, tombstone: dict[str, Any]) -> None:
        if tombstone.get("notification_delivered") is True:
            return
        response = tombstone["response"]; comment = response["comment"]
        txid = tombstone.get("transaction_id")
        if not isinstance(txid, str):
            raise CommentMutationError("unavailable")
        event = self._validate_notification_event(
            tombstone.get("notification_event"), txid, "reanchor")
        self._deliver_notification_event_once(txid, "reanchor", event)
        with path_lock(self.reanchor_wal_dir / ".maintenance"):
            values = self._reanchor_tombstones()
            if key in values:
                values[key]["notification_delivered"] = True
                durable_atomic_write_text(self.reanchor_tombstone_path,
                    json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    @property
    def reanchor_tombstone_path(self) -> Path:
        return self.reanchor_wal_dir / "tombstones.json"

    @property
    def reanchor_filter_path(self) -> Path:
        return self.reanchor_wal_dir / "tombstone-filter.json"

    def _reanchor_tombstones(self) -> dict[str, dict[str, Any]]:
        if not self.reanchor_tombstone_path.is_file():
            return {}
        try:
            value = json.loads(self.reanchor_tombstone_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommentMutationError("unavailable") from exc
        if not isinstance(value, dict):
            raise CommentMutationError("unavailable")
        return value

    @staticmethod
    def _validate_reanchor_wal(record: Any) -> None:
        required = {
            "transaction_id": str, "key": str, "fingerprint": str,
            "comment_id": str, "expected_row_token": str,
            "expected_concept_content_rev": str, "state": str,
            "created_at": (int, float), "expires_at": (int, float),
        }
        if not isinstance(record, dict) or any(
                name not in record or isinstance(record[name], bool)
                or not isinstance(record[name], kind)
                for name, kind in required.items()):
            raise CommentMutationError("unavailable")
        if record["state"] not in {"prepared", "finalized"}:
            raise CommentMutationError("unavailable")
        if record["state"] == "finalized" and (
                not isinstance(record.get("response"), dict)
                or not isinstance(record.get("status"), int)
                or isinstance(record.get("status"), bool)
                or not isinstance(record.get("resulting_row_token"), str)):
            raise CommentMutationError("unavailable")
        if (not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", record["transaction_id"])
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", record["key"])
                or not re.fullmatch(r"[0-9a-f]{64}", record["fingerprint"])
                or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", record["comment_id"])
                or not record["expected_row_token"]
                or len(record["expected_row_token"]) > 128
                or any(ord(char) < 33 or ord(char) > 126 for char in record["expected_row_token"])
                or not re.fullmatch(r"[0-9a-f]{64}", record["expected_concept_content_rev"])
                or not math.isfinite(float(record["created_at"]))
                or not math.isfinite(float(record["expires_at"]))
                or float(record["expires_at"]) < float(record["created_at"])):
            raise CommentMutationError("unavailable")
        if record["state"] == "finalized":
            response = record["response"]
            if (record["status"] != 200 or response.get("ok") is not True
                    or not re.fullmatch(r"[!-~]{1,128}", record["resulting_row_token"])
                    or not isinstance(response.get("comment"), dict)
                    or response["comment"].get("id") != record["comment_id"]
                    or response["comment"].get("row_token") != record["resulting_row_token"]
                    or response.get("concept_content_rev") != record["expected_concept_content_rev"]):
                raise CommentMutationError("unavailable")

    def _reanchor_filter(self) -> bytearray:
        size = self.REANCHOR_FILTER_BITS // 8
        if not self.reanchor_filter_path.is_file():
            return bytearray(size)
        try:
            value = json.loads(self.reanchor_filter_path.read_text(encoding="utf-8"))
            raw = bytes.fromhex(value["bits"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise CommentMutationError("unavailable") from exc
        if len(raw) != size:
            raise CommentMutationError("unavailable")
        return bytearray(raw)

    def _reanchor_filter_positions(self, key: str) -> tuple[int, int, int]:
        digest = hashlib.sha256(key.encode()).digest()
        return tuple(int.from_bytes(digest[i:i + 4], "big") % self.REANCHOR_FILTER_BITS
                     for i in (0, 4, 8))  # type: ignore[return-value]

    def _reanchor_filter_contains(self, key: str) -> bool:
        bits = self._reanchor_filter()
        return all(bits[pos // 8] & (1 << (pos % 8)) for pos in self._reanchor_filter_positions(key))

    def _sweep_reanchor_wal_locked(self, *, now: float) -> None:
        """Bound WAL files while retaining durable uncertain-outcome identities.

        Caller owns one endpoint-key lock. A maintenance lock serializes sweeps;
        lock order is key -> maintenance -> directives snapshot.
        """
        maintenance = self.reanchor_wal_dir / ".maintenance"
        with path_lock(maintenance):
            self.reanchor_wal_dir.mkdir(parents=True, exist_ok=True)
            records: list[tuple[Path, dict[str, Any]]] = []
            for path in self.reanchor_wal_dir.glob("*.json"):
                if path.name in {"tombstones.json", "tombstone-filter.json"}:
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise CommentMutationError("unavailable") from exc
                if not isinstance(record, dict):
                    raise CommentMutationError("unavailable")
                self._validate_reanchor_wal(record)
                records.append((path, record))
            for path, record in records:
                if record["state"] != "prepared":
                    continue
                result = self._read_transaction_result_locked(record["transaction_id"])
                with DirectiveStore(self._session_io_dir).writer() as directive_tx:
                    physical = self._directive_transaction_rows_locked(
                        directive_tx, "reanchor_transaction_id", record["transaction_id"])
                if len(physical) > 1:
                    raise CommentMutationError("idempotency_conflict")
                if physical:
                    directive = physical[0]
                    if (directive.get("reanchor_request_fingerprint") != record["fingerprint"]
                            or directive.get("reanchor_idempotency_key") != record["key"]
                            or directive.get("id") != record["comment_id"]):
                        raise CommentMutationError("idempotency_conflict")
                    if result is None:
                        response = self._reanchor_response(
                            directive, directive.get("reanchor_concept_content_rev",
                                                     record["expected_concept_content_rev"]))
                        result = {"operation": "reanchor", "transaction_id": record["transaction_id"],
                                  "fingerprint": record["fingerprint"], "key": record["key"],
                                  "comment_id": record["comment_id"], "row_token": directive["row_token"],
                                  "status": 200, "response": response,
                                  "concept": directive.get("concept", ""),
                                  "notification_event": self._comment_event(
                                      directive, transaction_id=record["transaction_id"],
                                      operation="reanchor")}
                        self._write_transaction_result_locked(result)
                if result is None:
                    continue
                if (result.get("operation") != "reanchor"
                        or result.get("fingerprint") != record["fingerprint"]
                        or result.get("key") != record["key"]
                        or result.get("comment_id") != record["comment_id"]):
                    raise CommentMutationError("idempotency_conflict")
                record.update(state="finalized", status=200, response=result["response"],
                              resulting_row_token=result["row_token"],
                              concept=result.get("concept", ""),
                              notification_event=self._validate_notification_event(
                                  result.get("notification_event"), record["transaction_id"], "reanchor"),
                              notification_delivered=False)
                durable_atomic_write_text(path, json.dumps(record, separators=(",", ":"), ensure_ascii=False))
                self._deliver_reanchor_notification_locked(path, record)
            finalized = sorted(
                ((path, row) for path, row in records if row.get("state") == "finalized"),
                key=lambda item: float(item[1].get("created_at", 0)),
            )
            evict = {path for path, row in finalized
                     if now > float(row.get("expires_at", 0))}
            survivors = [(path, row) for path, row in finalized if path not in evict]
            overflow = max(0, len(records) - len(evict) - self.REANCHOR_WAL_CAP)
            evict.update(path for path, _ in survivors[:overflow])
            for path, row in records:
                if (row.get("state") == "prepared"
                        and now > float(row.get("expires_at", 0)) + self.REANCHOR_PREPARED_GRACE_SECONDS):
                    evict.add(path)
            tombstones = self._reanchor_tombstones()
            for path, row in records:
                if path not in evict:
                    continue
                if row.get("state") == "finalized" and row.get("notification_delivered") is not True:
                    self._deliver_reanchor_notification_locked(path, row)
                key = row.get("key")
                if not isinstance(key, str):
                    raise CommentMutationError("unavailable")
                tombstones[key] = {
                    "transaction_id": row.get("transaction_id"),
                    "fingerprint": row.get("fingerprint"),
                    "comment_id": row.get("comment_id"),
                    "evicted_at": now,
                    "status": row.get("status", 200),
                    "state": row.get("state"),
                    "response": row.get("response"),
                    "resulting_row_token": row.get("resulting_row_token"),
                      "concept": row.get("concept", ""),
                      "notification_event": row.get("notification_event"),
                    "notification_delivered": row.get("notification_delivered", False),
                }
            compact = [key for key, row in tombstones.items()
                       if now - float(row.get("evicted_at", 0)) > self.REANCHOR_TOMBSTONE_TTL_SECONDS]
            remaining = sorted(
                ((key, row) for key, row in tombstones.items() if key not in compact),
                key=lambda item: float(item[1].get("evicted_at", 0)),
            )
            overflow = max(0, len(remaining) - self.REANCHOR_TOMBSTONE_CAP)
            compact.extend(key for key, _ in remaining[:overflow])
            if compact:
                bits = self._reanchor_filter()
                for key in compact:
                    for pos in self._reanchor_filter_positions(key):
                        bits[pos // 8] |= 1 << (pos % 8)
                    tombstones.pop(key, None)
                durable_atomic_write_text(
                    self.reanchor_filter_path,
                    json.dumps({"version": 1, "bits": bytes(bits).hex()}, separators=(",", ":")),
                )
            durable_atomic_write_text(
                self.reanchor_tombstone_path,
                json.dumps(tombstones, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            )
            for path in evict:
                path.unlink()
            self._cleanup_transaction_sidecars_locked()
            dir_fd = os.open(self.reanchor_wal_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def _emit_reanchor_notification_once(self, txid: str | None, comment_id: str,
                                         row_token: str | None, concept: str) -> None:
        if not txid:
            raise CommentMutationError("unavailable")
        def emit() -> None:
            if any(row.get("reanchor_transaction_id") == txid
                   for row in _read_jsonl_with_rotations(self.events_path)):
                return
            self._append_event_locked({
                "type": "comment", "id": comment_id, "concept": concept,
                "row_token": row_token, "reanchor_transaction_id": txid,
            })
        self.run_sidecar_transaction(emit)

    @staticmethod
    def _reanchor_response(row: dict[str, Any], content_rev: str) -> dict[str, Any]:
        return {"ok": True, "comment": {"id": row["id"],
                "row_token": row["row_token"], "anchor": row["anchor"]},
                "concept_content_rev": content_rev}


    # §17 library API alias — explicit "resolve" verb for the agent loop.
    def resolve_comment(self, comment_id: str, *,
                        reply: str | None = None,
                        activity_ids: list[str] | None = None,
                        actor: str = "agent",
                        summary: str | None = None) -> dict[str, Any] | None:
        """Mark a comment ``resolved`` with optional reply + activity links.

        §17 names this as the programmatic equivalent of the HTTP agent loop:
        ``okf comment-claim <bundle> <id>`` → agent does its scoped work →
        ``okf comment-resolve <bundle> <id> --reply … --activity <id>[,<id>]``.

        INTENT2-007 (iter-3): also back-stamps ``comment_id`` on each named
        activity event in events.jsonl so the change list can render a
        back-link ("resolved comment X"). The back-stamp is a small
        ``comment_link`` event that carries the (activity_id, comment_id)
        pair; the UI reads it to render the back-link.

        ``summary`` is a short agent-authored blurb shown in the
        collapsed thread preview. Separate from ``reply`` (which is the
        long-form resolution message) so the panel can show a quick tag
        without expanding.
        """
        updated = self.update_comment(
            comment_id, state="resolved",
            claimed_by=actor,
            resolved_activity=activity_ids,
            reply=reply,
            summary=summary,
        )
        if updated is not None and activity_ids:
            # INTENT2-007: back-stamp comment_id on each activity event so
            # the change list can link back to the originating comment.
            for aid in activity_ids:
                try:
                    self.append_event({
                        "type": "comment_link",
                        "activity_id": aid,
                        "comment_id": comment_id,
                        "concept": updated.get("concept", ""),
                        **({'document': updated['document']} if 'document' in updated else {}),
                    })
                except Exception:
                    pass  # best-effort; the resolve itself succeeded
        return updated

    # --- presence (current spec §12) --------------------------------------

    def set_presence(self, *, actor: str = "agent", state: str = "idle",
                     focus: str | None = None,
                     message: str | None = None) -> dict[str, Any]:
        """Set + broadcast agent presence (current spec §12).

        INTENT2-008 (iter-3): records a timestamp so the staleness sweep
        can revert to ``idle`` when the agent process drops (§13.8 robust
        liveness). The TTL is configurable via ``self.presence_ttl_s``
        (default 300s = 5 min).

        ``message`` is an optional short free-text progress line ("linking
        3 of 7 tables…") shown next to the state chip, so a long
        multi-concept pass reads as visible progress instead of a static
        "editing". Capped at 200 chars; re-post to update it mid-pass.
        """
        presence = {"actor": actor, "state": state}
        if focus:
            presence["focus"] = focus
        if message:
            msg = str(message).strip()
            if len(msg) > 200:
                msg = msg[:200].rstrip() + "…"
            if msg:
                presence["message"] = msg
        with self._presence_publish_lock:
            with self._lock:
                self.ensure_session()
                snapshot, applied, delta = self.presence_store.set_agent(
                    {**presence, "kind": "agent"})
                if not applied:
                    raise PresenceUnavailable("agent presence sequence was not applied")
                self.presence = presence
                self._presence_ts = time.time()
            # Never hold the cache lock while publishing or taking the
            # sidecar event lock. The publication lock keeps commit order.
            if delta:
                self.bus.publish(delta)
            self.append_event({"type": "presence", "actor": actor, "state": state,
                               **({"focus": focus} if focus else {}),
                               **({"message": presence["message"]} if presence.get("message") else {})},
                              publish=False)
        return presence

    @property
    def presence_store(self) -> PresenceStore:
        return PresenceStore(self.presence_path, self._run_presence_transaction,
                             agent_ttl=self.presence_ttl_s)

    def get_presence_snapshot(self) -> dict[str, Any]:
        return self.presence_store.snapshot()

    # §17 library API alias.
    def post_presence(self, *, actor: str = "agent", state: str = "idle",
                      focus: str | None = None,
                      message: str | None = None) -> dict[str, Any]:
        return self.set_presence(actor=actor, state=state, focus=focus,
                                 message=message)

    def get_presence(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.presence)

    def sweep_stale_presence_and_claims(self) -> dict[str, list[str]]:
        """§13.8 robust-liveness staleness sweep (INTENT2-008).

        ARCH5-001 fix (iter-5): also check presence.json's mtime as a
        cross-process freshness signal. The CLI agent's set_presence
        writes presence.json cross-process, but serve's in-process
        _presence_ts was never updated by that write — so serve would
        falsely revert a LIVE CLI agent to idle after the TTL. By also
        reading presence.json's mtime, the sweep correctly tracks the
        CLI agent's last-known-alive time.
        """
        result: dict[str, Any] = {"presence_reverted": False, "claims_reverted": []}
        try:
            with self._presence_publish_lock:
                with self._lock:
                    def publish_locked(browser_snapshot, browser_deltas):
                        agent = next((row for row in browser_snapshot["records"]
                                      if row.get("session_id") == "agent"), None)
                        cached = dict(self.presence)
                        corrective = bool(agent is not None and any(
                            cached.get(key) != agent.get(key)
                            for key in ("actor", "state", "focus", "message")))
                        if agent is not None:
                            self.presence = {key: agent[key] for key in ("actor", "state", "focus", "message") if key in agent}
                            self._presence_ts = agent.get("updated_at", self._presence_ts)
                        for delta in browser_deltas:
                            self.bus.publish(delta)
                        if corrective and not any(
                                delta.get("record", {}).get("session_id") == "agent"
                                for delta in browser_deltas):
                            self.bus.publish({"type": "presence", "presence_version": 2,
                                              "presence_rev": browser_snapshot["presence_rev"],
                                              "action": "upsert", "record": agent,
                                              "reason": "cross_process"})
                    browser_snapshot, browser_deltas = self.presence_store.sweep(
                        publish=publish_locked)
                if browser_deltas:
                    result["browser_presence_swept"] = True
                if any(delta.get("record", {}).get("session_id") == "agent"
                       and delta.get("reason") == "expired" for delta in browser_deltas):
                    result["presence_reverted"] = True
        except PresenceUnavailable:
            result["presence_unavailable"] = True
        now = time.time()
        # Claim TTL: revert stale claims to open.
        try:
            for c in self.list_comments(state="claimed"):
                ts_str = c.get("claimed_at")
                # Claim age starts at the claim transition, not creation.
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    claimed_ts = dt.timestamp()
                except (AttributeError, TypeError, ValueError, OSError):
                    result.setdefault("claims_quarantined", []).append(c.get("id"))
                    continue
                if (now - claimed_ts) > self.claim_ttl_s:
                    self.update_comment(c["id"], state="open", claimed_by=None)
                    result["claims_reverted"].append(c["id"])
        except CommentMutationError:
            result["claims_unavailable"] = True
        return result

    # --- §12.3 opt-in constraints (post_suggestion) ---------------------

    def post_suggestion(self, *, concept: str, action: str, args: dict[str, Any],
                        summary: str, group_id: str | None = None,
                        actor: str = "agent") -> dict[str, Any]:
        """Append a proposal to ``proposals.jsonl`` (§12.3 opt-in propose_only).

        When the user has opted into ``studio.constraints.propose_only: true``,
        the agent writes PlannedAction envelopes here instead of writing
        files. A ``suggestions`` SSE event fires; a review panel (built via
        the client extension API) accepts/dismisses. Accept → ``POST /__apply``
        runs the whitelisted UpdateOp via :meth:`save_concept` (§12.3).

        No-op (returns ``{}``) when ``propose_only`` is not enabled, so the
        agent's normal ``save_concept`` writes apply directly (§1.1 / D8).
        """
        if not (self.constraints.get("propose_only") is True):
            return {}
        self.ensure_session()
        proposal = {
            "id": new_id(),
            "ts": _now_iso(),
            "concept": concept,
            "action": action,
            "args": dict(args),
            "summary": summary,
            "actor": actor,
            "state": "open",
            "group_id": group_id,
        }
        path = self._session_io_dir / "proposals.jsonl"
        # SEC2-008: rotate proposals.jsonl (default 1 MiB / 2 files). Smaller
        # than directives because proposals are an opt-in (propose_only) stream.
        _append_jsonl(path, proposal, proc_lock=self._lock,
                      max_bytes=1 * 1024 * 1024, keep=2)
        self.append_event({"type": "suggestions", "proposal": proposal["id"],
                           "concept": concept})
        return proposal

    # --- undo (§12.5) ---------------------------------------------------

    def snapshot_for_undo(self, *, concept_id: str, raw: str | bytes,
                          group_id: str | None = None) -> str:
        return self.run_group_transaction(
            group_id,
            lambda: self._snapshot_for_undo_locked(
                concept_id=concept_id, raw=raw, group_id=group_id
            ),
        )

    def _snapshot_for_undo_locked(self, *, concept_id: str, raw: str | bytes,
                                  group_id: str | None = None) -> str:
        """Snapshot prior concept bytes for undo; returns the ``rev`` (§12.5).

        Stored at ``history/<safe_cid>/<rev>.md`` (capped ring per concept).
        When ``group_id`` is set, the snapshot is also listed in the group
        manifest so a whole agent pass reverts in one click.

        The snapshot write is ATOMIC (P1-4, AGENTS.md hard rule #8): a crash
        mid-write leaves no torn snapshot that could silently corrupt a
        future undo. The group manifest write is likewise atomic.
        """
        if group_id:
            # Validate existing ownership metadata before creating any snapshot.
            self._read_group_manifest_locked(group_id)
        rev = rev_of(raw)
        # F8: reversible, collision-free encoding. ``concept_id.replace("/",
        # "__")`` was non-injective — ``a__b`` and ``a/b`` mapped to the same
        # history dir. Hex of the UTF-8 bytes is injective (and mirrors
        # :meth:`undo_snapshot`), so every concept id gets a unique dir. The
        # group manifest stores the PLAIN concept id, so group undo does not
        # need the encoding.
        _assert_safe_path_token("rev", rev)
        safe = concept_id.encode("utf-8").hex() or "_root"
        d = self._session_directory(f"history/{safe}")
        # P1-4: atomic write for the snapshot itself.
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        atomic_write_bytes(d / f"{rev}.md", raw_bytes)
        # Cap the ring: keep the most recent N snapshots per concept.
        _cap_history_ring(d, limit=50)
        if group_id:
            self._record_group_member(group_id, concept_id, rev)
        return rev

    def snapshot_many_for_undo(
        self, entries: list[tuple[str, str | bytes]], *, group_id: str | None = None
    ) -> dict[str, str]:
        """Publish a transaction's undo snapshots as one rollback-safe batch."""
        return self.run_group_transaction(
            group_id,
            lambda: self._snapshot_many_for_undo_locked(entries, group_id=group_id),
        )

    def _snapshot_many_for_undo_locked(
        self, entries: list[tuple[str, str | bytes]], *, group_id: str | None = None
    ) -> dict[str, str]:
        snapshots: dict[str, str] = {}
        created: list[Path] = []
        manifest: Path | None = None
        manifest_before: bytes | None = None
        try:
            for concept_id, raw in entries:
                raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
                rev = rev_of(raw_bytes)
                _assert_safe_path_token("rev", rev)
                safe = concept_id.encode("utf-8").hex() or "_root"
                directory = self._session_directory(f"history/{safe}")
                target = directory / f"{rev}.md"
                existed = target.exists()
                atomic_write_bytes(target, raw_bytes)
                if not existed:
                    created.append(target)
                snapshots[concept_id] = rev
            if group_id:
                _assert_safe_path_token("group_id", group_id)
                with self._lock:
                    gdir = self._session_directory(f"history/_groups/{group_id}")
                    manifest = gdir / "manifest.json"
                    manifest_before = manifest.read_bytes() if manifest.is_file() else None
                    members = self._read_group_manifest_locked(group_id) or []
                    for concept_id, rev in snapshots.items():
                        entry = {"concept": concept_id, "rev": rev}
                        if entry not in members:
                            members.append(entry)
                    atomic_write_text(
                        manifest,
                        json.dumps(members, separators=(",", ":"), ensure_ascii=False),
                    )
        except BaseException:
            for target in reversed(created):
                target.unlink(missing_ok=True)
            if manifest is not None:
                if manifest_before is None:
                    manifest.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(manifest, manifest_before)
            raise
        for target in created:
            _cap_history_ring(target.parent, limit=50)
        return snapshots

    def _record_group_member(self, group_id: str, concept_id: str, rev: str) -> None:
        # QUA2-001 fix: hold self._lock for the read-modify-write of the group
        # manifest. Without this, two concurrent save_concept(group_id="G1")
        # calls both read the current manifest, both append, and the second
        # atomic_write_text overwrites the first → lost group member → broken
        # group-undo. The lock serializes same-process writers; cross-process
        # safety would need a dedicated flock on the manifest file.
        _assert_safe_path_token("group_id", group_id)
        _assert_safe_path_token("rev", rev)
        with self._lock:
            gdir = self._session_directory(f"history/_groups/{group_id}")
            manifest = gdir / "manifest.json"
            members = self._read_group_manifest_locked(group_id) or []
            entry = {"concept": concept_id, "rev": rev}
            if entry not in members:
                members.append(entry)
            # P1-4: atomic manifest write (tmp + rename) — INSIDE the lock so
            # the read-modify-write is serialized.
            atomic_write_text(
                manifest,
                json.dumps(members, separators=(",", ":"), ensure_ascii=False),
            )

    def undo_snapshot(self, *, concept_id: str, rev: str) -> str | None:
        """Read a prior snapshot's raw bytes for restore (§12.5)."""
        # P2-1: validate rev before it reaches the path join.
        _assert_safe_path_token("rev", rev)
        # F8: must mirror snapshot_for_undo's reversible hex encoding so the
        # dir written there is found here.
        safe = concept_id.encode("utf-8").hex() or "_root"
        directory = self._session_directory(f"history/{safe}")
        f = directory / f"{rev}.md"
        if not f.is_file():
            return None
        return f.read_text(encoding="utf-8")

    def undo_group(self, group_id: str) -> list[dict[str, str]] | None:
        """Return the members of a group for whole-pass undo (§12.5)."""
        _assert_safe_path_token("group_id", group_id)
        return self.run_group_transaction(
            group_id,
            lambda: self._read_group_manifest_locked(group_id),
        )

    def _read_group_manifest_locked(self, group_id: str) -> list[dict[str, str]] | None:
        """Read a group's manifest WITHOUT taking ``self._lock``.

        Caller MUST already hold ``self._lock``. Used internally by
        :meth:`save_concept`'s ``max_edits_per_action`` cap check (QUA2-004)
        so it doesn't self-deadlock against :meth:`_record_group_member`
        (which also takes the lock).
        """
        manifest = self._session_directory(
            f"history/_groups/{group_id}") / "manifest.json"
        if not manifest.is_file():
            return None
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise ValueError(f"invalid group manifest for {group_id!r}") from exc
        if not isinstance(value, list) or any(
            not isinstance(item, dict)
            or set(item) != {"concept", "rev"}
            or not isinstance(item.get("concept"), str)
            or not isinstance(item.get("rev"), str)
            for item in value
        ):
            raise ValueError(f"invalid group manifest for {group_id!r}")
        return value


def _cap_history_ring(d: Path, *, limit: int) -> None:
    """Keep only the most recent ``limit`` snapshots in a concept history dir."""
    try:
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in files[limit:]:
        try:
            stale.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Agent foreground wait primitive (current spec §12 — the agent loop)
# ---------------------------------------------------------------------------

def wait_for_work(
    bundle_root: str | Path,
    *,
    kinds: tuple[str, ...] = ("comment",),
    since: str | None = None,
    timeout: float | None = None,
    interval: float = 0.5,
) -> dict[str, Any] | None:
    """Block until there is new work for the agent, then return it (current spec §12).

    This is the **agent's foreground wait primitive**: the agent (running in
    an opencode / Claude Code / Codex session) calls this, it BLOCKS until a
    new open comment and/or a new change event appears, returns that one item,
    and exits — so the agent wakes, acts on it (edits via mutators, resolves
    the comment), then calls ``wait_for_work`` again. It MUST run in the
    agent's foreground (the agent is the processor); never background it — a
    backgrounded wait has no consumer for its output.

    **Pre-existing open comments** (current spec §12): on startup, if there are
    already open comments the agent hasn't seen, the function returns the
    FIRST one immediately (it doesn't skip them by establishing a baseline
    at the latest id). Subsequent calls process the rest, then wait for
    truly new work. This means the agent loop naturally drains any backlog
    on first run, then settles into wait-for-new mode.

    Args:
        bundle_root: path to the OKF bundle.
        kinds: what to wait for — ``"comment"`` (a new OPEN user comment) and/or
            ``"change"`` (a new changed/created/removed event). Default waits
            for a new open comment (the user's ask).
        since: only items whose feed id sorts after this are considered. If
            ``None`` (default), pre-existing open comments are returned first
            (backlog drain); once the backlog is empty, only truly new work
            is returned.
        timeout: max seconds to wait (``None`` = block indefinitely until
            work arrives). On timeout, returns ``None``.
        interval: seconds between feed polls (default 0.5).

    Returns:
        The first matching item (a comment dict for ``"comment"``, an event
        dict for ``"change"``), or ``None`` on timeout.
    """
    import time as _time

    root = Path(bundle_root)
    studio = Studio.for_configured_bundle(root)
    deadline = None if timeout is None else (_time.monotonic() + timeout)
    want_comment = "comment" in kinds
    want_change = "change" in kinds

    def _session_exists() -> bool:
        # A fresh bundle has an empty feed.  Adoption is always relative to the
        # bundle descriptor retained by Studio, never through the display path.
        return studio._adopt_existing_session()

    def _open_comments() -> list[dict[str, Any]]:
        return studio.list_comments(state="open") if _session_exists() else []

    # --- Pre-existing open comments (backlog drain) ---
    # On startup, if since is not pinned, return the FIRST open comment
    # immediately. This lets the agent pick up work that was posted before
    # it started, rather than skipping it. Subsequent calls drain the rest,
    # then the baseline logic below kicks in for truly new work.
    # The one-work-item contract stands (block once, print one item, exit),
    # but the returned comment carries a ``queue`` field — the OTHER open
    # comments still pending — so the agent can see backlog depth without
    # diffing ``comment-list`` between loop turns.
    def _with_queue(comment: dict[str, Any]) -> dict[str, Any]:
        pending = sorted(
            (c for c in _open_comments()
             if c.get("id") != comment.get("id")),
            key=lambda c: c.get("ts", ""),
        )
        return {
            "kind": "comment", **comment,
            "queue": {"pending": len(pending),
                      "ids": [c.get("id") for c in pending]},
        }

    try:
        if want_comment and since is None:
            open_comments = _open_comments()
            if open_comments:
                # Return the oldest open comment first (FIFO backlog drain).
                open_comments.sort(key=lambda c: c.get("ts", ""))
                return _with_queue(open_comments[0])

    # Establish the baseline: the latest ids currently in each feed, so we only
    # return work that is NEW since the call began (unless `since` pins it).
        def _latest_event_id() -> str | None:
            if not _session_exists():
                return None
            last = None
            for row in _read_jsonl_with_rotations(studio.events_path):
                if row.get("id"):
                    last = row["id"]
            return last

    # P2-18: for comments, also track the latest DIRECTIVE RECORD ts. A
    # comment that is open → resolved → re-opened keeps the SAME id but
    # gains a new ts (the re-open transition appends a new directives.jsonl
    # record). Filtering by id alone would miss the re-open. We track the
    # latest record ts and treat any open comment whose latest ts is newer
    # as fresh work — so a re-opened comment wakes the agent again.
    #
    # ``ts`` is the IMMUTABLE creation timestamp; ``updated_at`` is the field
    # bumped on every transition. We check both for back-compat with records
    # written before the separate updated_at field existed.
        def _latest_directive_values() -> tuple[str | None, str | None]:
            last_id = None
            last_ts = None
            if not _session_exists():
                return last_id, last_ts
            with DirectiveStore(studio._session_io_dir).writer() as directive_tx:
                rows = directive_tx.rows()
            for row in rows:
                if row.get("id"):
                    last_id = row["id"]
                ts = row.get("updated_at") or row.get("ts")
                if isinstance(ts, str):
                    last_ts = ts
            return last_id, last_ts

        latest_comment, base_comment_ts = _latest_directive_values()
        base_comment = since if (since and want_comment) else latest_comment
        base_event = since if (since and want_change) else _latest_event_id()

        while True:
            if want_comment:
                for c in _open_comments():
                # A comment is "new" if its id is newer than the baseline
                # (a fresh ask) OR its latest updated_at is newer than the
                # baseline (a re-open of a previously-resolved comment, P2-18).
                    c_ts = c.get("updated_at") or c.get("ts")
                    is_new_id = base_comment is None or c.get("id", "") > base_comment
                    is_reopened = (
                        base_comment_ts is not None
                        and isinstance(c_ts, str)
                        and c_ts > base_comment_ts
                    )
                    if is_new_id or is_reopened:
                        return _with_queue(c)
            if want_change and _session_exists():
                for e in studio.read_events(limit=500):
                    eid = e.get("id", "")
                    if e.get("type") in ("changed", "created", "removed") and (
                        base_event is None or eid > base_event
                    ):
                        return {"kind": "change", **e}
            if deadline is not None:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    return None
                _time.sleep(min(interval, remaining))
            else:
                _time.sleep(interval)
    finally:
        studio.close()
