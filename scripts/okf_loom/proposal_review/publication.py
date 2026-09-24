"""Trusted exact-byte publication adapter for Proposal Review transactions.

All canonical filesystem mutation is concentrated here.  The coordinator only
handles immutable values and WAL phases; it never calls ``Path.write_*``,
``replace`` or ``fsync`` itself.
"""
from __future__ import annotations

import hashlib
import os
import stat
import contextlib
import ctypes
import errno
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import RepositoryCorruptError, StorageSafetyError
from .ids import validate_concept_id, validate_record_id


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


_RENAME_EXCHANGE = 2
MAX_CONCEPT_BYTES = 2 * 1024 * 1024


def _rename_exchange(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
    """Linux descriptor-relative atomic exchange; there is deliberately no fallback."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise StorageSafetyError("Linux renameat2(RENAME_EXCHANGE) is unavailable") from exc
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    result = renameat2(src_dir_fd, os.fsencode(src), dst_dir_fd, os.fsencode(dst),
                       _RENAME_EXCHANGE)
    if result != 0:
        code = ctypes.get_errno()
        if code in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise StorageSafetyError("Linux renameat2(RENAME_EXCHANGE) is unavailable")
        raise StorageSafetyError(f"descriptor-relative publication exchange failed: errno {code}")


@dataclass(frozen=True)
class FileIdentity:
    relative_name: str
    device: int
    inode: int
    mode: int
    size: int
    digest: str

    def to_mapping(self) -> dict[str, object]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class TargetIdentity:
    parent_device: int
    parent_inode: int
    leaf_device: int
    leaf_inode: int
    ctime_ns: int
    mode: int
    size: int
    digest: str

    def to_mapping(self) -> dict[str, object]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DirectoryIdentity:
    relative_name: str
    device: int
    inode: int
    mode: int

    def to_mapping(self) -> dict[str, object]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class PreparedTarget:
    concept: str
    before_digest: str
    staged_digest: str
    backup_digest: str
    restore_digest: str
    before_identity: TargetIdentity
    staged_identity: TargetIdentity
    resulting_identity: TargetIdentity | None
    stage_leaf: FileIdentity
    backup_leaf: FileIdentity


@dataclass(frozen=True)
class PreparedPublication:
    transaction_id: str
    stage_dir_identity: DirectoryIdentity
    backup_dir_identity: DirectoryIdentity
    targets: tuple[PreparedTarget, ...]


class TrustedPublicationAdapter:
    """Pinned bundle/repository adapter using descriptor-relative no-follow I/O."""

    def __init__(self, bundle_root: str | Path, repository_root: str | Path,
                  *, fault_injector: object | None = None,
                  repository_dir_fd: int | None = None) -> None:
        self._bundle_path = Path(bundle_root).resolve(strict=True)
        self._repository_path = (Path(repository_root).resolve(strict=True)
                                 if repository_dir_fd is None else Path(repository_root))
        if Path(bundle_root).is_symlink() or (repository_dir_fd is None and Path(repository_root).is_symlink()):
            raise StorageSafetyError("publication roots must not be symlinks")
        self._bundle_fd = self._open_dir_path(self._bundle_path, private=False)
        try:
            if repository_dir_fd is None:
                self._repository_fd = self._open_dir_path(self._repository_path, private=True)
            else:
                self._repository_fd = os.dup(repository_dir_fd)
                info = os.fstat(self._repository_fd)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) not in (0o700, 0o2700)):
                    raise StorageSafetyError("publication repository descriptor is unsafe")
        except BaseException:
            repository_fd = getattr(self, "_repository_fd", -1)
            if repository_fd >= 0:
                os.close(repository_fd)
                self._repository_fd = -1
            os.close(self._bundle_fd)
            self._bundle_fd = -1
            raise
        self._fault_injector = fault_injector

    def _fault(self, boundary: str, evidence: Mapping[str, object]) -> None:
        if self._fault_injector is not None:
            self._fault_injector(boundary, evidence)  # type: ignore[operator]

    @staticmethod
    def _open_dir_path(path: Path, *, private: bool) -> int:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            os.close(fd); raise StorageSafetyError("publication root identity is unsafe")
        mode = stat.S_IMODE(st.st_mode)
        if (private and mode not in (0o700, 0o2700)) or (not private and mode & 0o022):
            os.close(fd); raise StorageSafetyError("publication root mode is unsafe")
        return fd

    def close(self) -> None:
        for name in ("_bundle_fd", "_repository_fd"):
            fd = getattr(self, name, -1)
            if fd >= 0:
                os.close(fd); setattr(self, name, -1)
        authority = getattr(self, "_session_authority", None)
        if authority is not None:
            authority.close(); self._session_authority = None

    def __enter__(self) -> "TrustedPublicationAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort descriptor hygiene
        try:
            self.close()
        except Exception:
            pass

    @contextlib.contextmanager
    def exclusive(self, timeout_seconds: float = 15.0):
        """Hold the existing starvation-safe bundle transaction lock."""
        from ..io_utils import bundle_transaction_lock
        with bundle_transaction_lock(self._bundle_path, "exclusive",
                                     timeout_seconds=timeout_seconds):
            yield

    @contextlib.contextmanager
    def concept_locks(self, concepts: object):
        """Hold unchanged direct-mode path locks in canonical concept order."""
        from ..io_utils import path_locks
        paths = [self._bundle_path.joinpath(*str(c).split("/")).with_suffix(".md")
                 for c in sorted(set(concepts))]
        with path_locks(paths):
            yield

    @staticmethod
    def _dir_identity(fd: int, name: str) -> DirectoryIdentity:
        st = os.fstat(fd)
        return DirectoryIdentity(name, st.st_dev, st.st_ino, stat.S_IMODE(st.st_mode))

    @staticmethod
    def _verify_dir(fd: int, identity: DirectoryIdentity) -> None:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino, stat.S_IMODE(st.st_mode)) != (
                identity.device, identity.inode, identity.mode):
            raise StorageSafetyError("pinned publication directory identity changed")

    @staticmethod
    def _open_child_dir(parent: int, name: str, *, create: bool = False) -> int:
        if create:
            try: os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError: pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     dir_fd=parent)
        st = os.fstat(fd)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
                or stat.S_IMODE(st.st_mode) not in (0o700, 0o2700)):
            os.close(fd); raise StorageSafetyError("private transaction directory is unsafe")
        return fd

    def _concept_parent(self, concept: str) -> tuple[int, str]:
        validate_concept_id(concept)
        parts = concept.split("/")
        parent = os.dup(self._bundle_fd)
        try:
            for component in parts[:-1]:
                nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                              dir_fd=parent)
                os.close(parent); parent = nxt
            return parent, parts[-1] + ".md"
        except Exception:
            os.close(parent); raise

    @staticmethod
    def _read_regular(parent: int, leaf: str) -> tuple[bytes, os.stat_result]:
        fd = os.open(leaf, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise StorageSafetyError("canonical concept leaf is not a singly-linked regular file")
            if st.st_size > MAX_CONCEPT_BYTES:
                raise StorageSafetyError("canonical concept exceeds the publication read limit")
            data = b""
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block: break
                data += block
                if len(data) > MAX_CONCEPT_BYTES:
                    raise StorageSafetyError("canonical concept exceeds the publication read limit")
            end = os.fstat(fd)
            if (st.st_dev, st.st_ino, st.st_ctime_ns, st.st_size, st.st_mode, st.st_nlink) != (
                    end.st_dev, end.st_ino, end.st_ctime_ns, end.st_size, end.st_mode, end.st_nlink):
                raise StorageSafetyError("canonical concept changed during read")
            return data, st
        finally:
            os.close(fd)

    @staticmethod
    def _exclusive_file(parent: int, leaf: str, data: bytes, mode: int) -> FileIdentity:
        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                     mode, dir_fd=parent)
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view: view = view[os.write(fd, view):]
            os.fsync(fd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise StorageSafetyError("private transaction leaf is unsafe")
            return FileIdentity(leaf, st.st_dev, st.st_ino, stat.S_IMODE(st.st_mode),
                                st.st_size, _digest(data))
        finally:
            os.close(fd)

    def prepare(self, transaction_id: str, staged: Mapping[str, bytes], *,
                restore_digests: Mapping[str, str] | None = None) -> PreparedPublication:
        prefix = "undo_" if transaction_id.startswith("undo_") else "txn_"
        validate_record_id(transaction_id, prefix)
        tx_root = self._open_child_dir(self._repository_fd, "transactions", create=True)
        try:
            tx = self._open_child_dir(tx_root, transaction_id, create=True)
        finally:
            os.close(tx_root)
        try:
            stage_fd = self._open_child_dir(tx, "stage", create=True)
            backup_fd = self._open_child_dir(tx, "backup", create=True)
            stage_identity = self._dir_identity(stage_fd, "stage")
            backup_identity = self._dir_identity(backup_fd, "backup")
            targets: list[PreparedTarget] = []
            try:
                for index, concept in enumerate(sorted(staged)):
                    parent, leaf = self._concept_parent(concept)
                    try:
                        before, current = self._read_regular(parent, leaf)
                        mode = stat.S_IMODE(current.st_mode)
                        safe_leaf = f"{index:06d}.bin"
                        stage_leaf = self._exclusive_file(stage_fd, safe_leaf, bytes(staged[concept]), mode)
                        backup_leaf = self._exclusive_file(backup_fd, safe_leaf, before, mode)
                        stage_stat = os.stat(safe_leaf, dir_fd=stage_fd, follow_symlinks=False)
                        parent_stat = os.fstat(parent)
                        before_identity = TargetIdentity(
                            parent_stat.st_dev, parent_stat.st_ino, current.st_dev,
                            current.st_ino, current.st_ctime_ns, mode, current.st_size,
                            _digest(before))
                        staged_identity = TargetIdentity(
                            os.fstat(stage_fd).st_dev, os.fstat(stage_fd).st_ino,
                            stage_stat.st_dev, stage_stat.st_ino, stage_stat.st_ctime_ns,
                            stat.S_IMODE(stage_stat.st_mode), stage_stat.st_size,
                            _digest(bytes(staged[concept])))
                        targets.append(PreparedTarget(
                            concept, _digest(before), _digest(bytes(staged[concept])),
                            _digest(before), (restore_digests or {}).get(concept, _digest(before)),
                            before_identity, staged_identity, None, stage_leaf, backup_leaf))
                    finally:
                        os.close(parent)
                os.fsync(stage_fd); os.fsync(backup_fd); os.fsync(tx)
            finally:
                os.close(stage_fd); os.close(backup_fd)
            return PreparedPublication(transaction_id, stage_identity, backup_identity, tuple(targets))
        finally:
            os.close(tx)

    @staticmethod
    def _identity(parent: int, data: bytes, st: os.stat_result) -> TargetIdentity:
        pst = os.fstat(parent)
        return TargetIdentity(pst.st_dev, pst.st_ino, st.st_dev, st.st_ino,
                              st.st_ctime_ns, stat.S_IMODE(st.st_mode), st.st_size,
                              _digest(data))

    def canonical_identity(self, concept: str) -> TargetIdentity:
        parent, leaf = self._concept_parent(concept)
        try:
            data, st = self._read_regular(parent, leaf)
            return self._identity(parent, data, st)
        finally:
            os.close(parent)

    def classify(self, target: PreparedTarget) -> str:
        parent, leaf = self._concept_parent(target.concept)
        try:
            data, st = self._read_regular(parent, leaf)
            identity = self._identity(parent, data, st)
            if identity == target.before_identity: return "before"
            if target.resulting_identity is not None and identity == target.resulting_identity:
                return "staged"
            staged = target.staged_identity
            if (identity.parent_device, identity.parent_inode, identity.leaf_device,
                    identity.leaf_inode, identity.mode, identity.size, identity.digest) == (
                    target.before_identity.parent_device, target.before_identity.parent_inode,
                    staged.leaf_device, staged.leaf_inode, staged.mode, staged.size,
                    staged.digest):
                return "staged"
            return "foreign"
        finally:
            os.close(parent)

    def canonical_digest(self, concept: str) -> str:
        parent, leaf = self._concept_parent(concept)
        try:
            data, _ = self._read_regular(parent, leaf)
            return _digest(data)
        finally:
            os.close(parent)

    def read_backup(self, transaction_id: str, identity: object) -> bytes:
        """Read one exact identity-bound retained backup without following links."""
        relative_name = str(getattr(identity, "relative_name"))
        tx_root = self._open_child_dir(self._repository_fd, "transactions")
        tx = self._open_child_dir(tx_root, transaction_id)
        backup = self._open_child_dir(tx, "backup")
        try:
            data, st = self._read_regular(backup, relative_name)
            expected = (getattr(identity, "device"), getattr(identity, "inode"),
                        getattr(identity, "mode"), getattr(identity, "size"),
                        getattr(identity, "digest"))
            actual = (st.st_dev, st.st_ino, stat.S_IMODE(st.st_mode), st.st_size, _digest(data))
            if actual != expected:
                raise StorageSafetyError("retained backup identity changed")
            return data
        finally:
            os.close(backup); os.close(tx); os.close(tx_root)

    def publish(self, transaction_id: str, target: PreparedTarget) -> TargetIdentity:
        """Publish one identity-bound source and return exact destination identity."""
        tx_root = self._open_child_dir(self._repository_fd, "transactions")
        tx = self._open_child_dir(tx_root, transaction_id)
        stage_fd = self._open_child_dir(tx, "stage")
        parent, leaf = self._concept_parent(target.concept)
        try:
            current, current_st = self._read_regular(parent, leaf)
            current_identity = self._identity(parent, current, current_st)
            try:
                staged_data, staged = self._read_regular(stage_fd, target.stage_leaf.relative_name)
            except FileNotFoundError:
                raise StorageSafetyError("exchange evidence is missing its displaced canonical leaf")
            actual_staged = self._identity(stage_fd, staged_data, staged)
            expected_stage = target.staged_identity
            canonical_is_staged = (
                current_identity.parent_device, current_identity.parent_inode,
                current_identity.leaf_device, current_identity.leaf_inode,
                current_identity.mode, current_identity.size, current_identity.digest) == (
                target.before_identity.parent_device, target.before_identity.parent_inode,
                expected_stage.leaf_device, expected_stage.leaf_inode,
                expected_stage.mode, expected_stage.size, expected_stage.digest)
            displaced_is_before = (
                actual_staged.leaf_device, actual_staged.leaf_inode,
                actual_staged.mode, actual_staged.size, actual_staged.digest) == (
                target.before_identity.leaf_device, target.before_identity.leaf_inode,
                target.before_identity.mode, target.before_identity.size,
                target.before_identity.digest)
            if canonical_is_staged and displaced_is_before:
                if target.resulting_identity is not None and current_identity != target.resulting_identity:
                    raise StorageSafetyError("completed publication resulting identity changed")
                return current_identity
            if actual_staged != target.staged_identity:
                raise StorageSafetyError("staged leaf substituted")
            if current_identity != target.before_identity:
                raise StorageSafetyError("canonical before identity changed")
            self._fault("before_rename", {"transaction_id": transaction_id,
                                          "concept": target.concept})
            # Re-open both names after the synchronized fault seam.  A same-byte
            # inode swap at this exact window is hostile, not completion.
            current2, current2_st = self._read_regular(parent, leaf)
            staged2, staged2_st = self._read_regular(stage_fd, target.stage_leaf.relative_name)
            if (self._identity(parent, current2, current2_st) != target.before_identity
                    or self._identity(stage_fd, staged2, staged2_st) != target.staged_identity):
                raise StorageSafetyError("publication identity changed at check/exchange seam")
            visible_parent, visible_leaf = self._concept_parent(target.concept)
            try:
                visible, visible_st = self._read_regular(visible_parent, visible_leaf)
                if (os.fstat(visible_parent).st_dev, os.fstat(visible_parent).st_ino) != (
                        os.fstat(parent).st_dev, os.fstat(parent).st_ino):
                    raise StorageSafetyError("canonical parent changed at check/exchange seam")
                if self._identity(visible_parent, visible, visible_st) != target.before_identity:
                    raise StorageSafetyError("visible canonical identity changed at exchange seam")
            finally:
                os.close(visible_parent)
            self._fault("before_exchange_syscall", {"transaction_id": transaction_id,
                                                     "concept": target.concept})
            _rename_exchange(stage_fd, target.stage_leaf.relative_name, parent, leaf)
            self._fault("after_rename_before_source_verify", {"transaction_id": transaction_id,
                                                               "concept": target.concept})
            result_data, result_st = self._read_regular(parent, leaf)
            resulting = self._identity(parent, result_data, result_st)
            displaced_data, displaced_st = self._read_regular(
                stage_fd, target.stage_leaf.relative_name)
            displaced = self._identity(stage_fd, displaced_data, displaced_st)
            expected = target.staged_identity
            if (resulting.leaf_device, resulting.leaf_inode, resulting.mode,
                    resulting.size, resulting.digest) != (
                    expected.leaf_device, expected.leaf_inode, expected.mode,
                    expected.size, expected.digest):
                raise StorageSafetyError("published destination identity is not staged authority")
            if (displaced.leaf_device, displaced.leaf_inode, displaced.mode,
                    displaced.size, displaced.digest) != (
                    target.before_identity.leaf_device, target.before_identity.leaf_inode,
                    target.before_identity.mode, target.before_identity.size,
                    target.before_identity.digest):
                raise StorageSafetyError("exchange displaced leaf is not prior canonical authority")
            self._fault("source_verified", {"transaction_id": transaction_id,
                                             "concept": target.concept})
            self._fault("destination_verified", {"transaction_id": transaction_id,
                                                  "concept": target.concept})
            self._fault("rename_before_parent_fsync", {"transaction_id": transaction_id,
                                                       "concept": target.concept})
            os.fsync(parent)
            os.fsync(stage_fd)
            self._fault("parent_fsynced", {"transaction_id": transaction_id,
                                           "concept": target.concept})
            if self.canonical_identity(target.concept) != resulting:
                raise RepositoryCorruptError("canonical identity changed after parent fsync")
            displaced_after, displaced_after_st = self._read_regular(
                stage_fd, target.stage_leaf.relative_name)
            if self._identity(stage_fd, displaced_after, displaced_after_st) != displaced:
                raise RepositoryCorruptError("displaced exchange evidence changed after fsync")
            self._fault("post_fsync_verified", {"transaction_id": transaction_id,
                                                 "concept": target.concept})
            return resulting
        finally:
            os.close(parent); os.close(stage_fd); os.close(tx); os.close(tx_root)


__all__ = ["FileIdentity", "TargetIdentity", "DirectoryIdentity", "PreparedTarget", "PreparedPublication",
           "TrustedPublicationAdapter"]
