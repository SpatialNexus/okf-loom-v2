"""Pinned-directory authority for the Studio directive journal.

All authority I/O after transaction entry is relative to one retained session
directory descriptor.  This is intentionally directive-specific; generic
pathname callers must not accidentally acquire a weaker version of this
contract.
"""
from __future__ import annotations

import json
import errno
import os
import re
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


class DirectiveStoreError(OSError):
    """Directive authority is absent, unsafe, corrupt, or unsupported."""


_ACTIVE = "directives.jsonl"
_CHECKPOINT = "directives-checkpoint.json"
_LOCK = "..directives-transaction.okf-lock"
_SEQUENCE = ".directives.jsonl.rotation-seq"
_ROTATION = re.compile(r"directives-(\d{8})-(\d+)\.jsonl\Z")
_HELD = threading.local()
_fsync = os.fsync
_unlink = os.unlink


@dataclass
class _HeldTransaction:
    mode: str
    depth: int
    transaction: "DirectiveTransaction | None" = None


def _validate_authority_row(row: Any) -> dict[str, Any]:
    if (not isinstance(row, dict)
            or not isinstance(row.get("id"), str) or not row["id"]
            or not isinstance(row.get("concept"), str)
            or (not row["concept"] and 'document' not in row)
            or row.get("state") not in {"open", "claimed", "resolved", "dismissed", "archived"}
            or type(row.get("archived", False)) is not bool):
        raise DirectiveStoreError("corrupt directive row")
    if not row['concept']:
        from .documents import document_concept
        try:
            if document_concept(row['document']):
                raise ValueError('inconsistent target')
        except (TypeError, ValueError) as exc:
            raise DirectiveStoreError('corrupt directive row') from exc
    return row


def _capable() -> bool:
    required = ("O_NOFOLLOW", "O_DIRECTORY")
    if any(not hasattr(os, name) for name in required):
        return False
    supports = os.supports_dir_fd
    # CPython exposes replace's dir-fd arguments through the same rename
    # primitive but does not list the alias in supports_dir_fd.
    return all(fn in supports for fn in (os.open, os.stat, os.rename, os.unlink))


class DirectiveTransaction:
    """A locked capability rooted at one pinned session directory inode."""

    __slots__ = ("__dir_fd", "__lock_fd", "__writer", "__active")

    def __init__(self, dir_fd: int, lock_fd: int, writer: bool):
        self.__dir_fd = dir_fd
        self.__lock_fd = lock_fd
        self.__writer = writer
        self.__active = True

    def _require_active(self) -> None:
        if not self.__active:
            raise DirectiveStoreError("directive transaction is inactive")

    def _deactivate(self) -> None:
        self.__active = False
        self.__dir_fd = -1
        self.__lock_fd = -1

    def _names(self) -> list[str]:
        try:
            names = os.listdir(self.__dir_fd)
        except (OSError, TypeError) as exc:
            raise DirectiveStoreError("descriptor-relative listing unavailable") from exc
        if any(not isinstance(name, str) or "/" in name or name in {".", ".."} for name in names):
            raise DirectiveStoreError("invalid session entry")
        return names

    def _open_regular(self, name: str, flags: int, *, mode: int = 0o600) -> int:
        if not name or "/" in name or name in {".", ".."}:
            raise DirectiveStoreError("invalid directive leaf")
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         mode, dir_fd=self.__dir_fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise DirectiveStoreError("directive leaf is not regular")
            os.set_inheritable(fd, False)
            return fd
        except BaseException:
            if "fd" in locals():
                os.close(fd)
            raise

    def _read(self, name: str, *, missing_ok: bool = True) -> bytes | None:
        try:
            fd = self._open_regular(name, os.O_RDONLY)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(fd)

    @staticmethod
    def _jsonl(raw: bytes | None, *, active: bool, fail_on_torn: bool) -> list[dict[str, Any]]:
        if raw is None:
            return []
        rows: list[dict[str, Any]] = []
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            complete = line.endswith((b"\n", b"\r"))
            stripped = line.strip()
            if not stripped:
                continue
            if active and index == len(lines) - 1 and not complete:
                if fail_on_torn:
                    raise DirectiveStoreError("uncertain directive tail")
                break
            try:
                row = json.loads(stripped.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise DirectiveStoreError("corrupt directive row") from exc
            rows.append(_validate_authority_row(row))
        return rows

    def _rotations(self) -> list[tuple[int, str, str]]:
        rows = []
        for name in self._names():
            match = _ROTATION.fullmatch(name)
            if match:
                rows.append((int(match.group(2)), match.group(1), name))
        rows.sort()
        return rows

    def _checkpoint(self) -> tuple[dict[str, Any] | None, int | None]:
        raw = self._read(_CHECKPOINT)
        if raw is None:
            return None, None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DirectiveStoreError("corrupt directive checkpoint") from exc
        if (not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("rows"), list)
                or any(not isinstance(row, dict) for row in value["rows"])):
            raise DirectiveStoreError("corrupt directive checkpoint")
        for row in value["rows"]:
            _validate_authority_row(row)
        marker = value.get("through_rotation")
        if marker is not None and (type(marker) is not int or marker < 0):
            raise DirectiveStoreError("corrupt directive checkpoint")
        return value, marker

    def rows(self, *, fail_on_torn: bool = False, migrate: bool = True) -> list[dict[str, Any]]:
        self._require_active()
        checkpoint, marker = self._checkpoint()
        rotations = self._rotations()
        if checkpoint is not None and marker is None and rotations:
            if not self.__writer or not migrate:
                raise DirectiveStoreError("directive checkpoint lacks rotation watermark")
            marker = self._migrate_checkpoint(checkpoint, rotations)
        rows = list(checkpoint["rows"]) if checkpoint is not None else []
        for generation, _day, name in rotations:
            if marker is None or generation > marker:
                rows.extend(self._jsonl(self._read(name, missing_ok=False), active=False,
                                        fail_on_torn=True))
        rows.extend(self._jsonl(self._read(_ACTIVE), active=True, fail_on_torn=fail_on_torn))
        return rows

    def physical_rows(self, field: str, value: Any) -> list[dict[str, Any]]:
        self._require_active()
        rows: list[dict[str, Any]] = []
        for _generation, _day, name in self._rotations():
            rows.extend(self._jsonl(self._read(name, missing_ok=False), active=False,
                                    fail_on_torn=True))
        rows.extend(self._jsonl(self._read(_ACTIVE), active=True, fail_on_torn=True))
        found = [row for row in rows if row.get(field) == value]
        if found:
            return found
        checkpoint, _marker = self._checkpoint()
        return [] if checkpoint is None else [row for row in checkpoint["rows"] if row.get(field) == value]

    def _atomic(self, name: str, data: bytes) -> None:
        if not self.__writer:
            raise DirectiveStoreError("read transaction cannot write")
        temp = f".directive-{secrets.token_hex(16)}.tmp"
        fd = self._open_regular(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise DirectiveStoreError("short directive write")
                view = view[count:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temp, name, src_dir_fd=self.__dir_fd, dst_dir_fd=self.__dir_fd)
            check = self._open_regular(name, os.O_RDONLY)
            os.close(check)
            os.fsync(self.__dir_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp, dir_fd=self.__dir_fd)
            except FileNotFoundError:
                pass

    def _migrate_checkpoint(self, checkpoint: dict[str, Any], rotations: list[tuple[int, str, str]]) -> int:
        generation = self._prove_checkpoint_chronology(checkpoint, rotations)
        upgraded = dict(checkpoint, through_rotation=generation)
        self._atomic(_CHECKPOINT, json.dumps(upgraded, separators=(",", ":"),
                                                   ensure_ascii=False).encode())
        return generation

    def _prove_checkpoint_chronology(
            self, checkpoint: dict[str, Any],
            rotations: list[tuple[int, str, str]]) -> int:
        """Prove the legacy checkpoint boundary without mutating authority."""
        validated = [_validate_authority_row(row) for row in checkpoint["rows"]]
        expected = {row["id"]: row for row in validated}
        if len(expected) != len(validated):
            raise DirectiveStoreError("corrupt directive checkpoint")
        latest: dict[str, dict[str, Any]] = {}
        for _generation, _day, name in rotations:
            for row in self._jsonl(self._read(name, missing_ok=False), active=False, fail_on_torn=True):
                if not isinstance(row.get("id"), str) or not row["id"]:
                    raise DirectiveStoreError("corrupt directive row")
                latest[row["id"]] = row
        # A normal legacy checkpoint was written after rotation and before a
        # later active append.  That exact post-rotation boundary is valid and
        # must be tested before consuming the first newer active row.
        proved = all(expected.get(key) == value for key, value in latest.items())
        if not proved:
            for row in self._jsonl(self._read(_ACTIVE), active=True, fail_on_torn=True):
                if not isinstance(row.get("id"), str) or not row["id"]:
                    raise DirectiveStoreError("corrupt directive row")
                latest[row["id"]] = row
                if all(expected.get(key) == value for key, value in latest.items()):
                    proved = True
                    break
        if not proved:
            raise DirectiveStoreError("legacy checkpoint chronology is unprovable")
        return max(row[0] for row in rotations)

    def checkpoint(self) -> None:
        self._require_active()
        latest: dict[str, dict[str, Any]] = {}
        for row in self.rows(fail_on_torn=True):
            valid = _validate_authority_row(row)
            latest[valid["id"]] = valid
        generation = max((row[0] for row in self._rotations()), default=0)
        self._atomic(_CHECKPOINT, json.dumps({"version": 1, "through_rotation": generation,
                                             "rows": list(latest.values())},
                                            separators=(",", ":"), ensure_ascii=False).encode())

    def append(self, row: dict[str, Any], *, max_bytes: int, keep: int) -> None:
        self._require_active()
        _validate_authority_row(row)
        if not self.__writer:
            raise DirectiveStoreError("read transaction cannot append")
        if keep < 0:
            raise ValueError("JSONL rotation keep must be non-negative")
        raw = self._read(_ACTIVE)
        if raw is not None and raw and not raw.endswith((b"\n", b"\r")):
            boundary = max(raw.rfind(b"\n"), raw.rfind(b"\r")) + 1
            self._jsonl(raw[:boundary], active=False, fail_on_torn=True)
            fd = self._open_regular(_ACTIVE, os.O_WRONLY)
            try:
                os.ftruncate(fd, boundary); os.fsync(fd)
            finally:
                os.close(fd)
            raw = raw[:boundary]
        rotated = raw is not None and max_bytes > 0 and len(raw) >= max_bytes
        if rotated:
            # Validate the complete compaction input before sequence replace,
            # rotation, checkpoint replacement, or pruning changes anything.
            for existing in self.rows(fail_on_torn=True):
                _validate_authority_row(existing)
            rotations = self._rotations()
            sequence_raw = self._read(_SEQUENCE)
            try:
                persisted = 0 if sequence_raw is None else int(sequence_raw.decode("ascii"))
            except (UnicodeError, ValueError) as exc:
                raise DirectiveStoreError("invalid directive rotation sequence") from exc
            generation = max(persisted, max((r[0] for r in rotations), default=0)) + 1
            self._atomic(_SEQUENCE, str(generation).encode("ascii"))
            name = f"directives-{time.strftime('%Y%m%d', time.gmtime())}-{generation:012d}.jsonl"
            if name in self._names():
                raise DirectiveStoreError("directive rotation collision")
            os.rename(_ACTIVE, name, src_dir_fd=self.__dir_fd, dst_dir_fd=self.__dir_fd)
            os.fsync(self.__dir_fd)
        data = (json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        existed = _ACTIVE in self._names()
        fd = self._open_regular(_ACTIVE, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise DirectiveStoreError("short directive append")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            os.fsync(self.__dir_fd)
        if rotated:
            self.checkpoint()
            stale = self._rotations()
            stale = stale if keep == 0 else stale[:-keep]
            for _generation, _day, name in stale:
                try:
                    _unlink(name, dir_fd=self.__dir_fd)
                except OSError:
                    continue
                # Once deletion succeeds, durability failure is part of the
                # acknowledged mutation and must never be reported as success.
                _fsync(self.__dir_fd)


class DirectiveStore:
    """Factory for read-only and writer transactions."""

    def __init__(self, session_dir: str | Path):
        raw = os.fspath(session_dir)
        # A descriptor-root path carries a significant trailing slash: it
        # traverses procfs's magic link before final-component O_NOFOLLOW.
        self._session = (raw if str(raw).startswith("/proc/self/fd/")
                         else os.path.abspath(raw))

    @staticmethod
    def _validate_unlocked_authority(dir_fd: int) -> None:
        """Validate pre-existing authority before creating its first lock.

        This is the one migration case supported by initialization: directive
        data written by the legacy store may exist without the lock leaf.  It
        must be structurally trustworthy before initialization mutates the
        directory by adding that lock.  All inspection is relative to the
        already-pinned directory descriptor.
        """
        probe = DirectiveTransaction(dir_fd, -1, False)
        names = probe._names()
        if ".directives-transaction" in names:
            raise DirectiveStoreError("legacy directive transaction leaf is unsafe")
        checkpoint, _marker = probe._checkpoint()
        rotations = probe._rotations()
        if checkpoint is not None:
            for row in checkpoint["rows"]:
                _validate_authority_row(row)
        for _generation, _day, name in rotations:
            probe._jsonl(probe._read(name, missing_ok=False), active=False,
                         fail_on_torn=True)
        # A crash-mid-append suffix on the active journal has always been a
        # recoverable read condition.  Initialization must not turn that
        # tolerated tail into a reason to reject (or partially initialize)
        # otherwise valid legacy authority.  Rotations and checkpoints above
        # remain strict because they are immutable/complete authority.
        probe._jsonl(probe._read(_ACTIVE), active=True, fail_on_torn=False)
        if checkpoint is not None and _marker is None and rotations:
            probe._prove_checkpoint_chronology(checkpoint, rotations)
        sequence = probe._read(_SEQUENCE)
        if sequence is not None:
            try:
                value = int(sequence.decode("ascii"))
            except (UnicodeError, ValueError) as exc:
                raise DirectiveStoreError("invalid directive rotation sequence") from exc
            if value < 0:
                raise DirectiveStoreError("invalid directive rotation sequence")

    @staticmethod
    def _open_pinned_lock(dir_fd: int, *, create: bool) -> tuple[int, bool]:
        flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        created = False
        if create:
            try:
                lock_fd = os.open(_LOCK, flags | os.O_CREAT | os.O_EXCL, 0o600,
                                  dir_fd=dir_fd)
                created = True
                os.fsync(dir_fd)
            except FileExistsError:
                lock_fd = os.open(_LOCK, flags, dir_fd=dir_fd)
        else:
            lock_fd = os.open(_LOCK, flags, dir_fd=dir_fd)
        try:
            DirectiveStore._validate_canonical_lock(dir_fd, lock_fd)
            os.set_inheritable(lock_fd, False)
            return lock_fd, created
        except BaseException:
            os.close(lock_fd)
            raise

    @staticmethod
    def _validate_canonical_lock(dir_fd: int, lock_fd: int) -> None:
        lock_info = os.fstat(lock_fd)
        current = os.stat(_LOCK, dir_fd=dir_fd, follow_symlinks=False)
        if (not stat.S_ISREG(lock_info.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or lock_info.st_nlink != 1
                or current.st_nlink != 1
                or (lock_info.st_dev, lock_info.st_ino)
                != (current.st_dev, current.st_ino)):
            raise DirectiveStoreError("directive lock is not a stable regular file")

    @contextmanager
    def _transaction(self, *, writer: bool,
                     initialize: bool = False) -> Iterator[DirectiveTransaction]:
        if not _capable():
            raise DirectiveStoreError("safe directive authority is unavailable")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        dir_fd = os.open(self._session, flags)
        lock_fd = -1
        dir_locked = False
        transaction: DirectiveTransaction | None = None
        identity: tuple[int, int] | None = None
        registered = False
        try:
            pinned = os.fstat(dir_fd)
            current = os.stat(self._session, follow_symlinks=False)
            if not stat.S_ISDIR(pinned.st_mode) or (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
                raise DirectiveStoreError("session directory was substituted")
            identity = (pinned.st_dev, pinned.st_ino)
            held: dict[tuple[int, int], _HeldTransaction] = getattr(
                _HELD, "transactions", {})
            _HELD.transactions = held
            if identity in held:
                raise DirectiveStoreError("nested directive transaction is forbidden")
            held[identity] = _HeldTransaction(
                mode="writer" if writer else "reader", depth=1)
            registered = True
            import fcntl
            # The pinned directory inode is the primary cooperative authority.
            # It cannot be redirected by replacing the compatibility lock leaf.
            fcntl.flock(dir_fd, fcntl.LOCK_EX if writer else fcntl.LOCK_SH)
            dir_locked = True
            try:
                lock_stat = os.stat(_LOCK, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                lock_stat = None
            if lock_stat is not None and not stat.S_ISREG(lock_stat.st_mode):
                raise DirectiveStoreError("directive lock is not regular")
            if lock_stat is None:
                if not writer:
                    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), _LOCK)
                self._validate_unlocked_authority(dir_fd)
            lock_fd, _created = self._open_pinned_lock(
                dir_fd, create=writer and lock_stat is None)
            fcntl.flock(lock_fd, fcntl.LOCK_EX if writer else fcntl.LOCK_SH)
            # The pathname may have been replaced while flock waited.  A lock
            # on the stale inode grants no authority over the canonical leaf.
            self._validate_canonical_lock(dir_fd, lock_fd)
            transaction = DirectiveTransaction(dir_fd, lock_fd, writer)
            held[identity].transaction = transaction
            if ".directives-transaction" in transaction._names():
                raise DirectiveStoreError("legacy directive transaction leaf is unsafe")
            if initialize:
                # Revalidate under the canonical lock and perform the store's
                # supported legacy checkpoint-watermark migration if needed.
                transaction.rows(fail_on_torn=False, migrate=True)
            yield transaction
        finally:
            if transaction is not None:
                transaction._deactivate()
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            if dir_locked:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
            os.close(dir_fd)
            if registered and identity is not None:
                held = getattr(_HELD, "transactions", {})
                record = held.get(identity)
                if record is not None:
                    record.depth -= 1
                    if record.depth <= 0:
                        held.pop(identity, None)

    def reader(self):
        return self._transaction(writer=False)

    def writer(self):
        return self._transaction(writer=True)

    def initialize(self):
        """Initialize or validate an authority at an already-created session."""
        return self._transaction(writer=True, initialize=True)

    def _owned_transaction(self) -> _HeldTransaction | None:
        """Return this thread's record only for the current session inode."""
        try:
            current = os.stat(self._session, follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISDIR(current.st_mode):
            return None
        held: dict[tuple[int, int], _HeldTransaction] = getattr(
            _HELD, "transactions", {})
        return held.get((current.st_dev, current.st_ino))

    def writer_owned_by_current_thread(self) -> bool:
        """Whether this thread owns the writer pinned to this session inode.

        This is deliberately only an ownership query; it does not permit a
        nested transaction.  Re-resolving the session pathname also ensures a
        substituted directory is not mistaken for the authority already held.
        """
        record = self._owned_transaction()
        return bool(record is not None and record.mode == "writer"
                    and record.depth == 1 and record.transaction is not None)

    def _initialize_owned_writer(self) -> None:
        """Run initialization migration through this thread's owned writer."""
        record = self._owned_transaction()
        if (record is None or record.mode != "writer" or record.depth != 1
                or record.transaction is None):
            raise DirectiveStoreError("owned directive writer is unavailable")
        record.transaction.rows(fail_on_torn=False, migrate=True)
