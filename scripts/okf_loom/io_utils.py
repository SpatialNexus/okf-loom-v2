"""Atomic-write helpers shared across okf-loom.

All mutating operations (``update``, ``index``, ``log``, ``render``) go
through these helpers. The pattern is:

    1. Create a tmp file via ``tempfile.mkstemp`` in the *same directory*
       as the target (so ``os.replace`` is atomic on POSIX — same
       filesystem). The tmp file is created mode 0600 by ``mkstemp``, so
       it is private even on shared hosts.
    2. Write the content via ``os.fdopen``.
    3. ``os.replace(tmp, path)`` to swap into place atomically.
    4. On ANY exception during write or replace, ``os.unlink`` the tmp
       file in a ``finally`` block — no orphans left behind.

This addresses:
    - Symlink-race attacks (predictable tmp names → victim file clobber).
    - World-readable tmp files (default umask on Linux is often 0644).
    - Tmp-file orphaning on disk-full / permission errors mid-write.
"""
from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal


SELF_IGNORE = """# Auto-written by okf-loom: this directory holds ephemeral working
# state (live-session feed, undo history, per-session auth token, or
# derived caches) and must never be committed. The `*` makes the
# directory self-ignoring in ANY repository, with no parent .gitignore
# cooperation required. Safe to edit; okf-loom never overwrites it.
*
"""

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_HELD = threading.local()

BUNDLE_LOCK_FILENAME = ".okf-loom-bundle.lock"
BundleLockMode = Literal["shared", "exclusive"]


class BundleTransactionLockError(RuntimeError):
    """Typed failure while acquiring the bundle transaction lock."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BundleLockUpgradeError(BundleTransactionLockError):
    """A shared owner attempted the forbidden shared-to-exclusive upgrade."""

    def __init__(self) -> None:
        super().__init__(
            "bundle_lock_upgrade_forbidden",
            "cannot upgrade a shared bundle transaction lock to exclusive",
        )


class BundleBusyError(BundleTransactionLockError):
    """The bounded shared-lock acquisition deadline expired."""

    retryable = True

    def __init__(self) -> None:
        super().__init__("bundle_busy", "bundle transaction lock is busy")


class _BundleLockState:
    def __init__(self) -> None:
        self.condition = threading.Condition(threading.RLock())
        self.mode: BundleLockMode | None = None
        self.acquiring: int | None = None
        self.waiting_writers = 0
        self.owners: dict[int, list[BundleLockMode]] = {}
        self.owner_fds: dict[int, int] = {}


_BUNDLE_STATES_GUARD = threading.Lock()
_BUNDLE_STATES: dict[tuple[int, int], _BundleLockState] = {}
_BUNDLE_STATES_PID = os.getpid()
_BUNDLE_FD_GUARD = threading.Lock()
_BUNDLE_LOCK_FDS: set[int] = set()


def _before_bundle_lock_fork() -> None:
    _BUNDLE_FD_GUARD.acquire()


def _after_bundle_lock_fork_parent() -> None:
    _BUNDLE_FD_GUARD.release()


def _reset_bundle_locks_after_fork() -> None:
    """Drop inherited ownership descriptors and rebuild child-only metadata."""
    global _BUNDLE_STATES_GUARD, _BUNDLE_STATES, _BUNDLE_STATES_PID
    global _BUNDLE_FD_GUARD, _BUNDLE_LOCK_FDS
    # before= holds the old guard, so no opener can create an unregistered fd
    # while fork snapshots the process. This includes turnstile waiters that
    # have opened the leaf but have not yet published local ownership metadata.
    for fd in _BUNDLE_LOCK_FDS:
        try:
            os.close(fd)
        except OSError:
            pass
    # Locks/conditions may have been held by a vanished sibling thread at fork.
    # Replace rather than acquire them in the single-threaded child.
    _BUNDLE_STATES = {}
    _BUNDLE_STATES_GUARD = threading.Lock()
    _BUNDLE_LOCK_FDS = set()
    _BUNDLE_FD_GUARD = threading.Lock()
    _BUNDLE_STATES_PID = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_bundle_lock_fork,
        after_in_parent=_after_bundle_lock_fork_parent,
        after_in_child=_reset_bundle_locks_after_fork,
    )


def _pin_bundle_root(root: str | Path) -> tuple[Path, int, tuple[int, int]]:
    """Open and identify a canonical bundle root without following its leaf."""
    supplied = Path(root)
    initial = os.stat(supplied, follow_symlinks=False)
    if stat.S_ISLNK(initial.st_mode):
        raise OSError("bundle root must not be a symlink")
    if not stat.S_ISDIR(initial.st_mode):
        raise NotADirectoryError(str(supplied))
    canonical = supplied.resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(canonical, flags)
    info = os.fstat(root_fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(root_fd)
        raise NotADirectoryError(str(canonical))
    if (initial.st_dev, initial.st_ino) != (info.st_dev, info.st_ino):
        os.close(root_fd)
        raise OSError("bundle root identity changed while it was being pinned")
    return canonical, root_fd, (info.st_dev, info.st_ino)


def _bundle_state(identity: tuple[int, int]) -> _BundleLockState:
    global _BUNDLE_STATES_PID
    if _BUNDLE_STATES_PID != os.getpid():
        # Defensive fallback for runtimes without register_at_fork support.
        _reset_bundle_locks_after_fork()
    with _BUNDLE_STATES_GUARD:
        return _BUNDLE_STATES.setdefault(identity, _BundleLockState())


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _wait(condition: threading.Condition, deadline: float | None) -> None:
    remaining = _remaining(deadline)
    if remaining is not None and remaining <= 0:
        raise BundleBusyError()
    condition.wait(remaining)


def _open_bundle_lock(root_fd: int) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with _BUNDLE_FD_GUARD:
        fd = os.open(BUNDLE_LOCK_FILENAME, flags, 0o600, dir_fd=root_fd)
        _BUNDLE_LOCK_FDS.add(fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        _close_bundle_lock_fd(fd)
        raise OSError("bundle transaction lock leaf must be a regular file")
    return fd


def _close_bundle_lock_fd(fd: int) -> None:
    with _BUNDLE_FD_GUARD:
        _BUNDLE_LOCK_FDS.discard(fd)
        os.close(fd)


def _acquire_cross_process(fd: int, mode: BundleLockMode,
                           deadline: float | None) -> None:
    """Pass the writer-priority gate, then acquire the normative flock."""
    import fcntl

    operation = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX

    def acquire(call: object) -> None:
        if deadline is None:
            call(operation)  # type: ignore[operator]
            return
        while True:
            try:
                call(operation | fcntl.LOCK_NB)  # type: ignore[operator]
                return
            except BlockingIOError:
                remaining = _remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise BundleBusyError()
                time.sleep(min(0.01, remaining if remaining is not None else 0.01))

    # Byte zero is a writer-priority turnstile. Readers hold it only while
    # acquiring flock; a writer keeps it while waiting for existing readers,
    # so later readers cannot bypass that queued writer.
    gate = lambda op: fcntl.lockf(fd, op, 1, 0, os.SEEK_SET)
    acquire(gate)
    try:
        acquire(lambda op: fcntl.flock(fd, op))
    except BaseException:
        gate(fcntl.LOCK_UN)
        raise
    if mode == "shared":
        gate(fcntl.LOCK_UN)


def _release_cross_process(fd: int, mode: BundleLockMode) -> None:
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if mode == "exclusive":
            fcntl.lockf(fd, fcntl.LOCK_UN, 1, 0, os.SEEK_SET)


@contextmanager
def bundle_transaction_lock(root: str | Path, mode: BundleLockMode,
                            timeout_seconds: float | None = None):
    """Hold mode-aware ownership of a bundle's source/config transaction.

    Lock order is pinned root, this transaction lock, ``Bundle._derived_lock``,
    sorted ``path_locks``, then ``Studio._lock``.  Exclusive owners may nest
    either mode; shared owners may only nest shared mode.
    """
    if mode not in ("shared", "exclusive"):
        raise ValueError("bundle lock mode must be 'shared' or 'exclusive'")
    if timeout_seconds is not None and timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative")

    _canonical, root_fd, identity = _pin_bundle_root(root)
    state = _bundle_state(identity)
    acquisition_pid = os.getpid()
    tid = threading.get_ident()
    deadline = (None if timeout_seconds is None
                else time.monotonic() + timeout_seconds)
    reserved = False
    writer_wait_registered = False
    acquired = False
    try:
        with state.condition:
            stack = state.owners.get(tid)
            if stack:
                effective = "exclusive" if "exclusive" in stack else "shared"
                if effective == "shared" and mode == "exclusive":
                    raise BundleLockUpgradeError()
                stack.append(mode)
                acquired = True
            else:
                if mode == "exclusive":
                    state.waiting_writers += 1
                    writer_wait_registered = True
                while True:
                    if mode == "shared":
                        if (state.acquiring is None and state.mode in (None, "shared")
                                and state.waiting_writers == 0):
                            break
                    elif state.acquiring is None and state.mode is None:
                        break
                    _wait(state.condition, deadline)

                if writer_wait_registered:
                    state.waiting_writers -= 1
                    writer_wait_registered = False
                # Every first acquisition by a thread crosses the process
                # gate. Reusing one process-wide shared fd would let a new
                # local reader bypass a writer queued in another process.
                state.acquiring = tid
                reserved = True

        if reserved:
            fd = _open_bundle_lock(root_fd)
            try:
                _acquire_cross_process(fd, mode, deadline)
            except BaseException:
                _close_bundle_lock_fd(fd)
                raise
            with state.condition:
                state.mode = mode
                state.acquiring = None
                state.owners[tid] = [mode]
                state.owner_fds[tid] = fd
                reserved = False
                acquired = True
                state.condition.notify_all()
        yield
    finally:
        if acquisition_pid != os.getpid():
            # after_in_child already closed the inherited owner/turnstile fd
            # and discarded its metadata. Do not touch the stale parent state
            # or a descriptor number that the child may since have reused.
            try:
                os.close(root_fd)
            except OSError:
                pass
            return
        with state.condition:
            if writer_wait_registered:
                state.waiting_writers -= 1
                state.condition.notify_all()
            if reserved and state.acquiring == tid:
                state.acquiring = None
                state.condition.notify_all()
            if acquired:
                stack = state.owners.get(tid)
                if not stack or stack[-1] != mode:
                    raise RuntimeError("bundle transaction locks exited out of order")
                stack.pop()
                if not stack:
                    del state.owners[tid]
                    fd = state.owner_fds.pop(tid)
                    held_mode = state.mode
                    if held_mode is not None:
                        _release_cross_process(fd, held_mode)
                    _close_bundle_lock_fd(fd)
                if not state.owners:
                    state.mode = None
                state.condition.notify_all()
        os.close(root_fd)


def assert_bundle_transaction_owned(root: str | Path,
                                    mode: BundleLockMode = "shared") -> None:
    """Assert that the current thread owns the requested transaction mode."""
    _canonical, root_fd, identity = _pin_bundle_root(root)
    os.close(root_fd)
    state = _bundle_state(identity)
    with state.condition:
        stack = state.owners.get(threading.get_ident(), [])
        effective = "exclusive" if "exclusive" in stack else ("shared" if stack else None)
    if effective is None or (mode == "exclusive" and effective != "exclusive"):
        raise RuntimeError(f"bundle transaction lock is not held in {mode} mode")


def _canonical_lock_path(path: str | Path) -> Path:
    target = Path(path).resolve()
    return target.with_name(f".{target.name}.okf-lock")


@contextmanager
def path_locks(paths: list[str | Path]):
    """Hold canonical advisory locks for targets in deterministic order.

    The in-process RLocks serialize threads; flock serializes processes. The
    thread-local depth makes nested Studio/Bundle mutation funnels re-entrant.
    """
    lock_paths = sorted({_canonical_lock_path(path) for path in paths}, key=str)
    held: dict[str, tuple[int, int]] = getattr(_HELD, "locks", {})
    _HELD.locks = held
    acquired: list[tuple[str, threading.RLock, int | None]] = []
    try:
        for lock_path in lock_paths:
            key = str(lock_path)
            with _PATH_LOCKS_GUARD:
                local = _PATH_LOCKS.setdefault(key, threading.RLock())
            local.acquire()
            if key in held:
                fd, depth = held[key]
                held[key] = (fd, depth + 1)
                acquired.append((key, local, None))
                continue
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            held[key] = (fd, 1)
            acquired.append((key, local, fd))
        yield
    finally:
        for key, local, opened_fd in reversed(acquired):
            fd, depth = held[key]
            if depth > 1:
                held[key] = (fd, depth - 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                del held[key]
            local.release()


@contextmanager
def path_lock(path: str | Path):
    with path_locks([path]):
        yield


def ensure_self_ignored(dirpath: str | Path) -> None:
    """Make ``dirpath`` self-ignoring: write ``dirpath/.gitignore``
    containing ``*`` (idempotent; never overwrites an existing file so
    user edits stick). The standard pattern for tool-owned state dirs —
    pytest writes ``.pytest_cache/.gitignore``, Cargo writes
    ``target/.gitignore``, virtualenv self-ignores the venv dir: works
    wherever the bundle lives, and is immune to the directory being
    renamed or configured elsewhere.
    Viewer overrides are deliberately NOT self-ignored — they are
    hand-authored customization worth versioning.
    """
    dirpath = Path(dirpath)
    gi = dirpath / ".gitignore"
    if gi.exists():
        return
    try:
        dirpath.mkdir(parents=True, exist_ok=True)
        atomic_write_text(gi, SELF_IGNORE)
    except OSError:
        pass  # best-effort: a read-only checkout must not break serving


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically write ``text`` to ``path`` (tmp + os.replace).

    The tmp file is created in the same directory as ``path`` (so the
    rename is atomic on POSIX) with mode 0600 via ``mkstemp``. Parents
    are created if missing. On any exception, the tmp file is unlinked.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".okf-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        # BaseException so we also clean up on KeyboardInterrupt.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` (binary). See ``atomic_write_text``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".okf-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def durable_atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace a durability-critical file and fsync file + directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".okf-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_replace_tree(src: str | Path, dst: str | Path, *,
                        verify_previous=None) -> None:
    """Publish a staged directory, retaining prior output if rollback fails.

    Individual renames are atomic, not the pair: readers may briefly see no
    destination. Parents must exist and be caller-controlled; symlink paths
    and overlapping trees are refused. The destination parent's advisory
    directory lock serializes cooperating publishers (threads and processes)
    without creating or adopting a lock file. All rename/cleanup operations
    are relative to pinned no-follow directory descriptors.

    Only this invocation's private backup is removed. No historical recovery
    directory (including ``dst.old``) is adopted or cleaned. This is ordinary
    build publication, not crash-durable exchange or proposal publication.

    ``verify_previous(parent_fd, name, phase)`` (optional, managed exports)
    runs with phase ``"locked"`` under the lock before any recovery write,
    and with ``"quarantined"`` (``backup_fd``, ``"previous"``) between the two
    renames; raising there rolls the old tree back. ``None`` is the unchanged
    legacy path.
    """
    import fcntl
    import shutil
    import uuid

    src = validate_tree_path(src)
    dst = validate_tree_path(dst)
    if src.is_relative_to(dst) or dst.is_relative_to(src):
        raise ValueError("publication source and destination trees must not overlap")
    with _tree_directory_fd(src.parent) as src_fd, _tree_directory_fd(dst.parent) as dst_fd:
        fcntl.flock(dst_fd, fcntl.LOCK_EX)
        source_info = _tree_entry(src_fd, src.name)
        if source_info is None:
            raise FileNotFoundError(str(src))
        if source_info.st_dev != os.fstat(dst_fd).st_dev:
            raise ValueError("publication staging must be on the destination filesystem")
        previous = _tree_entry(dst_fd, dst.name)
        if verify_previous is not None:
            verify_previous(dst_fd, dst.name, "locked")
        backup_name = None
        backup_fd = None
        retain = False
        try:
            if previous is not None:
                # mkdir is exclusive: a collision refuses rather than adopting
                # an unrelated path. The old tree is a child of this 0700 dir.
                candidate = f".okf-recovery-{uuid.uuid4().hex}"
                os.mkdir(candidate, 0o700, dir_fd=dst_fd)
                backup_name = candidate
                backup_fd = os.open(backup_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=dst_fd)
            try:
                if backup_fd is not None:
                    retain = True
                    os.rename(dst.name, "previous", src_dir_fd=dst_fd, dst_dir_fd=backup_fd)
                    if verify_previous is not None:
                        verify_previous(backup_fd, "previous", "quarantined")
                os.rename(src.name, dst.name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
            except BaseException as publish_error:
                if backup_fd is not None:
                    try:
                        if _tree_entry(backup_fd, "previous") is not None:
                            # Never overwrite an intervening destination, even
                            # an empty directory. Cooperating writers hold flock.
                            if _tree_entry(dst_fd, dst.name) is not None:
                                raise FileExistsError(str(dst))
                            os.rename("previous", dst.name, src_dir_fd=backup_fd, dst_dir_fd=dst_fd)
                        retain = False
                    except BaseException as rollback_error:
                        retain = True
                        raise TreePublicationRecoveryError(
                            dst, dst.parent / backup_name / "previous",
                            publish_error, rollback_error,
                        ) from publish_error
                raise
            retain = False
        finally:
            if backup_fd is not None:
                os.close(backup_fd)
            if backup_name is not None and not retain:
                try:
                    shutil.rmtree(backup_name, dir_fd=dst_fd)
                except OSError as cleanup_error:
                    raise TreePublicationRecoveryError(
                        dst, dst.parent / backup_name, cleanup_error, None,
                    ) from cleanup_error


class TreePublicationRecoveryError(OSError):
    """Publication needs operator inspection; recovery bytes were retained."""

    def __init__(self, destination: Path, recovery_path: Path,
                 publish_error: BaseException, rollback_error: BaseException | None):
        self.destination = destination
        self.recovery_path = recovery_path
        self.publish_error = publish_error
        self.rollback_error = rollback_error
        super().__init__(f"publication requires recovery inspection for {destination}; "
                         f"retained recovery at {recovery_path}; do not delete it or blindly retry")


def validate_tree_path(path: str | Path) -> Path:
    """Read-only directory-path admission, including dangling symlinks."""
    supplied = Path(path).absolute()
    for part in reversed((supplied, *supplied.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"publication path must not use symlinks: {part}")
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(str(part))
    return Path(os.path.abspath(supplied))


@contextmanager
def _tree_directory_fd(path: Path):
    """Pin each ancestor without following symlinks, including during a swap."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _tree_entry(parent_fd: int, name: str):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"publication path must not use symlinks: {name}")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(name)
    return info


def _rmtree(path: Path) -> None:
    """Recursively delete a directory tree (symlink-safe).

    Refuses to delete a top-level symlink (which would follow the link
    target and delete its contents rather than the link itself).
    """
    # Guard: if path itself is a symlink, unlink the link, not its target.
    if path.is_symlink():
        try:
            path.unlink()
        except OSError:
            pass
        return
    # Use os.walk top-down so we don't follow symlinks; unlink files and
    # the directory itself.
    for root, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            try:
                (Path(root) / name).unlink()
            except OSError:
                pass
        for name in dirs:
            p = Path(root) / name
            if p.is_symlink():
                try:
                    p.unlink()
                except OSError:
                    pass
            else:
                try:
                    p.rmdir()
                except OSError:
                    pass
    try:
        path.rmdir()
    except OSError:
        pass
