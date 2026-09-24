"""Injected authority ports and the Linux trusted-dirfd implementation.

The production filesystem seam starts from a descriptor that the caller has
already contained.  It never accepts an absolute repository path.  Every path
operation is relative, no-follow, owner checked, and regular authority leaves
must be private and singly linked.
"""
from __future__ import annotations

import contextlib
import hashlib
import errno
import fcntl
import os
import secrets
import stat
import time
import datetime
from dataclasses import dataclass
from typing import Any, ContextManager, Iterator, Mapping, Protocol, runtime_checkable

from .errors import LockUnavailableError, StorageSafetyError
from .ids import validate_relative_path


@runtime_checkable
class Clock(Protocol):
    def now(self) -> str: ...


@runtime_checkable
class Entropy(Protocol):
    def token_bytes(self, count: int) -> bytes: ...


@runtime_checkable
class RepositoryLock(Protocol):
    def hold(self, timeout_seconds: float = 15.0) -> ContextManager[None]: ...


@runtime_checkable
class FilesystemRoot(Protocol):
    """Deep repository filesystem port; paths are always root-relative."""

    def subroot(self, path: str, *, create: bool = False) -> "FilesystemRoot": ...
    def mkdirs(self, path: str) -> None: ...
    def exists(self, path: str) -> bool: ...
    def read_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes: ...
    def write_atomic(self, path: str, data: bytes) -> None: ...
    def append_fsync(self, path: str, data: bytes) -> None: ...
    def truncate_fsync(self, path: str, size: int) -> None: ...
    def replace(self, source: str, destination: str) -> None: ...
    def unlink(self, path: str, *, missing_ok: bool = False) -> None: ...
    def rmdir(self, path: str, *, missing_ok: bool = False) -> None: ...
    def list_dir(self, path: str = ".") -> tuple[str, ...]: ...
    def file_size(self, path: str) -> int: ...
    def file_identity(self, path: str) -> Mapping[str, Any]: ...
    def walk_files(self, path: str = ".") -> tuple[str, ...]: ...
    def lock(self, path: str = ".append.lock") -> RepositoryLock: ...


@dataclass(frozen=True)
class RuntimeIdentities:
    validator_id: str
    serializer_id: str
    operation_runtime_id: str


@runtime_checkable
class BundleView(Protocol):
    """Structural protocol for immutable bundle facts supplied to pure projectors.

    A production Bundle adapter would capture these under the bundle transaction
    read lock and hand the frozen value to the side-effect-free
    projectors/read model/pagination layer.  Tests own contract fakes that
    implement this protocol; the pure layer never imports a production Bundle
    adapter (Fix #12).

    ``concept_revisions`` is the complete visible concept inventory mapped to
    the current 64-hex content revision (SPEC section 6).  ``capabilities`` is
    the sorted active capability-id set and ``config_digest`` hashes the
    effective parsed configuration.  The three runtime identities are the
    authority-bearing inputs to snapshot/preview/read-model/apply freshness
    digests (SPEC section 3).
    """

    snapshot_digest: str
    config_digest: str
    validation_context_digest: str
    capabilities: tuple[str, ...]
    concept_revisions: Mapping[str, str]
    validator_id: str
    serializer_id: str
    operation_runtime_id: str

    def environment_digest(self) -> str: ...


@dataclass(frozen=True)
class DecodedDocument:
    """Pure parsed document value passed between an operation and its codec."""

    frontmatter: Mapping[str, Any]
    body: str


@runtime_checkable
class DocumentCodec(Protocol):
    """Exact-byte document codec; implementations must perform no I/O."""

    def decode(self, data: bytes) -> DecodedDocument: ...
    def encode(self, original: bytes, document: DecodedDocument) -> bytes: ...


@dataclass(frozen=True)
class FrozenValidatorFinding:
    code: str
    severity: str
    concept: str | None
    line: int
    message: str
    scope: str = "concept"
    fingerprint: str | None = None


@dataclass(frozen=True)
class FrozenValidationReport:
    findings: tuple[FrozenValidatorFinding, ...]
    passed: bool


@runtime_checkable
class FrozenSnapshotValidator(Protocol):
    """Injected validator over a complete immutable concept-byte mapping."""

    def installed_profiles(self) -> tuple[str, ...]: ...
    def validate(self, snapshot: Mapping[str, bytes], *, profile: str) -> FrozenValidationReport: ...


@runtime_checkable
class ActivityLinkVerifier(Protocol):
    """External activity-store authority used before WAL retirement."""

    def verify_activity_ids(self, transaction_id: str, activity_ids: tuple[str, ...]) -> bool: ...


@runtime_checkable
class CursorSigner(Protocol):
    """Signs and verifies stateless pagination cursors.

    The concrete pure :class:`~okf_loom.proposal_review.pagination.HmacCursorSigner`
    implements the exact SPEC section-10 HMAC-SHA-256 algorithm with explicit
    key material; this protocol lets a production seam substitute a key-store
    backed signer without the pure layer depending on it.
    """

    def active_key_id(self) -> str:
        """Return the currently active signing key id."""
        ...

    def key_not_before(self, key_id: str) -> str | None:
        """Return ``not_before`` for ``key_id`` or ``None`` when unknown."""
        ...

    def key_expires_at(self, key_id: str) -> str | None:
        """Return ``expires_at`` for ``key_id`` or ``None`` when unknown."""
        ...

    def sign(self, repository_id: str, payload: bytes) -> tuple[str, str]:
        """Return ``(key_id, tag_base64url)`` for canonical payload ``payload``."""
        ...

    def verify(self, repository_id: str, key_id: str, payload: bytes, tag_base64url: str) -> bool:
        """Constant-time verify; ``False`` on unknown key or tag mismatch."""
        ...


class SystemEntropy:
    def token_bytes(self, count: int) -> bytes:
        return secrets.token_bytes(count)


class SystemClock:
    def now(self) -> str:
        value = datetime.datetime.now(datetime.timezone.utc)
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parts(path: str) -> tuple[str, ...]:
    if path == ".":
        return ()
    validate_relative_path(path)
    return tuple(path.split("/"))


def _safety(message: str) -> StorageSafetyError:
    return StorageSafetyError(message)


class LinuxDirFdFilesystem:
    """Strong Linux stdlib adapter rooted at a pinned, already-contained fd.

    Use :meth:`adopt` with a caller-open directory descriptor, or
    :meth:`open_at` with a relative child of one.  The adapter duplicates its
    descriptor and owns that duplicate.  Unsupported platforms fail closed.
    """

    def __init__(self, fd: int) -> None:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise _safety("trusted dirfd/no-follow storage is unsupported")
        self._fd = os.dup(fd)
        try:
            self._validate_dir_fd(self._fd, private=False)
        except BaseException:
            os.close(self._fd)
            self._fd = -1
            raise

    @classmethod
    def adopt(cls, already_contained_dir_fd: int) -> "LinuxDirFdFilesystem":
        return cls(already_contained_dir_fd)

    @classmethod
    def open_at(
        cls, parent_dir_fd: int, relative_path: str, *, create: bool = False
    ) -> "LinuxDirFdFilesystem":
        parent = cls(parent_dir_fd)
        try:
            return parent.subroot(relative_path, create=create)  # type: ignore[return-value]
        finally:
            parent.close()

    def close(self) -> None:
        fd = getattr(self, "_fd", -1)
        if fd >= 0:
            os.close(fd)
            self._fd = -1

    def __del__(self) -> None:  # pragma: no cover - best-effort descriptor hygiene
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _validate_dir_fd(fd: int, *, private: bool) -> os.stat_result:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise _safety("trusted root component is not a directory")
        if st.st_uid != os.geteuid():
            raise _safety("trusted root component has the wrong owner")
        mode = stat.S_IMODE(st.st_mode)
        # Directory setgid inherits a group, never group access. Keep an exact
        # allowlist: no group/other permissions, sticky or setuid bits.
        if private and mode not in (0o700, 0o2700):
            raise _safety("private repository directory mode is not 0700 or 02700")
        if not private and mode & 0o022:
            raise _safety("trusted root component is group/other writable")
        return st

    @staticmethod
    def _validate_file(fd: int) -> os.stat_result:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _safety("authority leaf is not a regular file")
        if st.st_uid != os.geteuid():
            raise _safety("authority leaf has the wrong owner")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise _safety("authority leaf mode is not 0600")
        if st.st_nlink != 1:
            raise _safety("authority leaf link count is not one")
        return st

    def _open_dir(self, parts: tuple[str, ...], *, create: bool = False) -> int:
        current = os.dup(self._fd)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
                nxt = os.open(part, flags, dir_fd=current)
                try:
                    self._validate_dir_fd(nxt, private=True)
                    before = os.stat(part, dir_fd=current, follow_symlinks=False)
                    opened = os.fstat(nxt)
                    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise _safety("directory component substituted during open")
                except BaseException:
                    os.close(nxt)
                    raise
                os.close(current)
                current = nxt
            return current
        except Exception as exc:
            os.close(current)
            if isinstance(exc, OSError) and exc.errno in {
                errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM
            }:
                raise _safety("unsafe or inaccessible directory component") from exc
            raise

    @contextlib.contextmanager
    def _parent(self, path: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = _parts(path)
        if not parts:
            raise _safety("operation requires a leaf path")
        fd = self._open_dir(parts[:-1], create=create)
        try:
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def subroot(self, path: str, *, create: bool = False) -> "LinuxDirFdFilesystem":
        fd = self._open_dir(_parts(path), create=create)
        try:
            return LinuxDirFdFilesystem(fd)
        finally:
            os.close(fd)

    def mkdirs(self, path: str) -> None:
        fd = self._open_dir(_parts(path), create=True)
        os.close(fd)

    def exists(self, path: str) -> bool:
        try:
            with self._parent(path) as (parent, leaf):
                st = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    raise _safety("symlink substitution detected")
                return True
        except FileNotFoundError:
            return False

    def _open_file(self, path: str, flags: int) -> int:
        try:
            with self._parent(path) as (parent, leaf):
                fd = os.open(leaf, flags | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                raise _safety("unsafe authority leaf substitution") from exc
            raise
        try:
            self._validate_file(fd)
            return fd
        except Exception:
            os.close(fd)
            raise

    def read_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes:
        fd = self._open_file(path, os.O_RDONLY)
        try:
            st = self._validate_file(fd)
            if max_bytes is not None and st.st_size > max_bytes:
                raise _safety("authority leaf exceeds bounded read limit")
            chunks: list[bytes] = []
            remaining = st.st_size
            while remaining:
                block = os.read(fd, min(remaining, 1024 * 1024))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            if self._validate_file(fd).st_size != st.st_size:
                raise _safety("authority leaf changed during read")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def write_atomic(self, path: str, data: bytes) -> None:
        with self._parent(path, create=True) as (parent, leaf):
            try:
                existing = os.open(
                    leaf, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent,
                )
            except FileNotFoundError:
                existing = -1
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                    raise _safety("unsafe atomic-write destination substitution") from exc
                raise
            if existing >= 0:
                try:
                    self._validate_file(existing)
                finally:
                    os.close(existing)
            tmp = f".{leaf}.tmp-{secrets.token_hex(12)}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
            fd = os.open(tmp, flags, 0o600, dir_fd=parent)
            try:
                self._validate_file(fd)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
                self._validate_file(fd)
            except Exception:
                try:
                    os.unlink(tmp, dir_fd=parent)
                except OSError:
                    pass
                raise
            finally:
                os.close(fd)
            os.replace(tmp, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            verify = os.open(leaf, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
            try:
                self._validate_file(verify)
            finally:
                os.close(verify)
            os.fsync(parent)

    def append_fsync(self, path: str, data: bytes) -> None:
        with self._parent(path, create=True) as (parent, leaf):
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
            fd = os.open(leaf, flags, 0o600, dir_fd=parent)
        try:
            self._validate_file(fd)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            self._validate_file(fd)
        finally:
            os.close(fd)

    def truncate_fsync(self, path: str, size: int) -> None:
        fd = self._open_file(path, os.O_WRONLY)
        try:
            os.ftruncate(fd, size)
            os.fsync(fd)
        finally:
            os.close(fd)

    def replace(self, source: str, destination: str) -> None:
        with self._parent(source) as (sp, sl), self._parent(destination, create=True) as (dp, dl):
            src_st = os.stat(sl, dir_fd=sp, follow_symlinks=False)
            if not stat.S_ISREG(src_st.st_mode) or src_st.st_nlink != 1:
                raise _safety("replace source is not a singly-linked regular file")
            os.replace(sl, dl, src_dir_fd=sp, dst_dir_fd=dp)
            os.fsync(dp)
            if sp != dp:
                os.fsync(sp)

    def unlink(self, path: str, *, missing_ok: bool = False) -> None:
        try:
            with self._parent(path) as (parent, leaf):
                fd = os.open(leaf, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    self._validate_file(fd)
                finally:
                    os.close(fd)
                os.unlink(leaf, dir_fd=parent)
                os.fsync(parent)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def rmdir(self, path: str, *, missing_ok: bool = False) -> None:
        """Remove one exact empty owned directory without following links."""
        try:
            with self._parent(path) as (parent, leaf):
                st = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
                        or stat.S_IMODE(st.st_mode) not in (0o700, 0o2700)):
                    raise _safety("rmdir target is not an exact private directory")
                os.rmdir(leaf, dir_fd=parent)
                os.fsync(parent)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def list_dir(self, path: str = ".") -> tuple[str, ...]:
        fd = self._open_dir(_parts(path))
        try:
            return tuple(sorted(os.listdir(fd)))
        finally:
            os.close(fd)

    def file_size(self, path: str) -> int:
        fd = self._open_file(path, os.O_RDONLY)
        try:
            return self._validate_file(fd).st_size
        finally:
            os.close(fd)

    def file_identity(self, path: str) -> Mapping[str, Any]:
        fd = self._open_file(path, os.O_RDONLY)
        try:
            st = self._validate_file(fd)
            data = b""
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                data += block
            end = os.fstat(fd)
            if (st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mode, st.st_size) != (
                    end.st_dev, end.st_ino, end.st_ctime_ns, end.st_mode, end.st_size):
                raise _safety("repository file changed while identifying")
            return {"device": st.st_dev, "inode": st.st_ino,
                    "ctime_ns": st.st_ctime_ns, "mode": stat.S_IMODE(st.st_mode),
                    "size": st.st_size,
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
        finally:
            os.close(fd)

    def walk_files(self, path: str = ".") -> tuple[str, ...]:
        base_parts = _parts(path)
        found: list[str] = []

        def visit(fd: int, prefix: tuple[str, ...]) -> None:
            for name in sorted(os.listdir(fd)):
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    raise _safety("symlink encountered while accounting repository quota")
                rel = "/".join(prefix + (name,))
                if stat.S_ISDIR(st.st_mode):
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                    try:
                        self._validate_dir_fd(child, private=True)
                        visit(child, prefix + (name,))
                    finally:
                        os.close(child)
                elif stat.S_ISREG(st.st_mode):
                    leaf = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        self._validate_file(leaf)
                    finally:
                        os.close(leaf)
                    found.append(rel)
                else:
                    raise _safety("non-regular object encountered in repository")

        root = self._open_dir(base_parts)
        try:
            visit(root, base_parts)
        finally:
            os.close(root)
        return tuple(found)

    def lock(self, path: str = ".append.lock") -> RepositoryLock:
        return _FcntlLock(self, path)


class _FcntlLock:
    def __init__(self, root: LinuxDirFdFilesystem, path: str) -> None:
        self._root = root
        self._path = path

    @contextlib.contextmanager
    def hold(self, timeout_seconds: float = 15.0) -> Iterator[None]:
        if timeout_seconds < 0 or timeout_seconds > 15.0:
            raise LockUnavailableError("repository lock timeout must be in 0..15 seconds")
        with self._root._parent(self._path, create=True) as (parent, leaf):
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
            fd = os.open(leaf, flags, 0o600, dir_fd=parent)
        try:
            self._root._validate_file(fd)
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockUnavailableError("repository append/rotation lock unavailable")
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


__all__ = [
    "Clock", "Entropy", "RepositoryLock", "FilesystemRoot",
    "RuntimeIdentities", "BundleView", "DocumentCodec", "DecodedDocument",
    "FrozenSnapshotValidator", "FrozenValidatorFinding", "FrozenValidationReport",
    "ActivityLinkVerifier", "CursorSigner",
    "SystemEntropy", "SystemClock", "LinuxDirFdFilesystem",
]
