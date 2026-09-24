"""Pinned filesystem authority for mutable Studio session state.

External roots are admitted beneath a trusted system temporary directory
(``/tmp`` or ``/var/tmp``), or beneath an explicit caller-owned private
ancestor.  The trusted ancestor itself may have the normal sticky 01777 mode;
every existing component below it must be owned by the effective uid with
mode exactly 0700 or 02700. Missing suffixes are requested as 0700, preserving
inherited directory setgid without chmod, while
walking descriptor-relatively.  All subsequent authority I/O is rooted at the
pinned descriptor, never at the display pathname.
"""
from __future__ import annotations

import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
              | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))


class _DescriptorPath(type(Path())):
    """Path whose authority root traverses the procfs descriptor, not a child.

    Linux rejects opening the procfs magic-link leaf itself with ``O_NOFOLLOW``.
    A trailing slash makes the kernel traverse that link as a directory before
    applying final-component no-follow semantics.  ``pathlib`` strips trailing
    slashes, so preserve it only when materializing the exact descriptor root;
    descendants remain ordinary paths with their own no-follow leaf.
    """

    def _is_descriptor_root(self) -> bool:
        parts = self.parts
        return (len(parts) == 5 and parts[:4] == ("/", "proc", "self", "fd")
                and parts[4].isdecimal())

    def __str__(self) -> str:
        value = super().__str__()
        return value + "/" if self._is_descriptor_root() else value

    def __fspath__(self) -> str:
        return str(self)

    @property
    def parent(self) -> Path:
        # Path-only adapters commonly split ``parent``/``name`` and then use
        # openat.  Keep that split descriptor-bound at the authority root.
        return self if self._is_descriptor_root() else super().parent

    @property
    def name(self) -> str:
        return "." if self._is_descriptor_root() else super().name


def _descriptor_path(fd: int) -> Path:
    return _DescriptorPath(f"/proc/self/fd/{fd}")


@dataclass(frozen=True)
class CleanupResult:
    """Typed result of descriptor-owned authority cleanup."""

    disposition: str
    device: int
    inode: int
    reachable_path: Path | None = None


@dataclass
class SessionAuthority:
    """Owned directory descriptor plus immutable admission identity."""

    display_path: Path
    fd: int
    device: int
    inode: int
    uid: int
    mode: int
    parent_fd: int
    final_name: str
    parent_device: int
    parent_inode: int
    strict_private_components: bool = True

    @property
    def path(self) -> Path:
        """Descriptor-bound materialization for legacy Path-only internals."""
        if self.fd < 0:
            raise RuntimeError("session authority is closed")
        return _descriptor_path(self.fd)

    def duplicate(self) -> "SessionAuthority":
        fd = os.dup(self.fd)
        return self._from_fd(
            self.display_path, fd, os.dup(self.parent_fd), self.final_name,
            strict_private_components=self.strict_private_components,
        )

    def subdirectory(self, relative: str, *, create: bool = False) -> "SessionAuthority":
        """Pin one private descendant without consulting the display path.

        Strict private components are opened no-follow with mode 0700 or 02700.
        A missing suffix is requested as 0700, retaining inherited setgid.
        The returned authority owns independent descriptors.
        """
        self._require_open()
        parts = _parts(relative)
        current = os.dup(self.fd)
        parent = -1
        try:
            for index, part in enumerate(parts):
                if index == len(parts) - 1:
                    parent = os.dup(current)
                if create:
                    try:
                        os.mkdir(
                            part, 0o700 if self.strict_private_components else 0o777,
                            dir_fd=current,
                        )
                    except FileExistsError:
                        pass
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
                try:
                    if not self._valid_component(os.fstat(child)):
                        raise OSError("session authority child is not private")
                except BaseException:
                    os.close(child)
                    raise
                os.close(current)
                current = child
            result = self._from_fd(
                self.display_path.joinpath(*parts), current, parent, parts[-1],
                strict_private_components=self.strict_private_components,
            )
            current = -1
            parent = -1
            return result
        finally:
            if current >= 0:
                os.close(current)
            if parent >= 0:
                os.close(parent)

    @staticmethod
    def _from_fd(
        display: Path, fd: int, parent_fd: int, final_name: str, *,
        strict_private_components: bool = True,
    ) -> "SessionAuthority":
        info = os.fstat(fd)
        parent_info = os.fstat(parent_fd)
        return SessionAuthority(display, fd, info.st_dev, info.st_ino,
                                 info.st_uid, stat.S_IMODE(info.st_mode), parent_fd, final_name,
                                 parent_info.st_dev, parent_info.st_ino,
                                 strict_private_components)

    def _valid_component(self, info: os.stat_result) -> bool:
        if self.strict_private_components:
            return _private_component(info)
        return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1

    def __del__(self) -> None:  # pragma: no cover - descriptor hygiene
        try:
            self.close()
        except Exception:
            pass

    def _require_open(self) -> None:
        if self.fd < 0:
            raise RuntimeError("session authority is closed")

    @contextmanager
    def _parent(self, relative: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        """Yield a pinned parent FD and leaf for one safe relative path."""
        self._require_open()
        parts = _parts(relative)
        current = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(
                            part, 0o700 if self.strict_private_components else 0o777,
                            dir_fd=current,
                        )
                    except FileExistsError:
                        pass
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
                if not self._valid_component(os.fstat(child)):
                    os.close(child)
                    raise OSError("session authority child is not private")
                os.close(current)
                current = child
            yield current, parts[-1]
        finally:
            os.close(current)

    def mkdir(self, relative: str, mode: int = 0o700, *, parents: bool = False,
              exist_ok: bool = False) -> None:
        """Create a private directory without consulting the display path."""
        if mode != 0o700:
            raise ValueError("session authority mkdir mode must be exactly 0700")
        with self._parent(relative, create=parents) as (parent, leaf):
            try:
                os.mkdir(leaf, mode, dir_fd=parent)
            except FileExistsError:
                if not exist_ok:
                    raise
            expected = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            child = os.open(leaf, _DIR_FLAGS, dir_fd=parent)
            try:
                info = os.fstat(child)
                if _stat_identity(info) != _stat_identity(expected):
                    raise OSError("session authority directory changed before admission")
                # mkdir success does not identify the inode subsequently opened:
                # a replacement before the first stat passes both identity checks.
                # Admit safe existing state instead: 02700 only adds group
                # inheritance, not group access. Never chmod either inode.
                if not _private_component(info):
                    raise OSError("session authority directory is not private")
                current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if _stat_identity(current) != _stat_identity(info):
                    raise OSError("session authority directory changed during admission")
            finally:
                os.close(child)

    def open(self, relative: str, flags: int, mode: int = 0o600,
             *, create_parents: bool = False) -> int:
        """Open a no-follow leaf beneath the pinned root."""
        with self._parent(relative, create=create_parents) as (parent, leaf):
            return os.open(leaf, flags | getattr(os, "O_NOFOLLOW", 0)
                           | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=parent)

    def replace(self, source: str, destination: str) -> None:
        """Atomically replace between two descriptor-relative locations."""
        with self._parent(source) as (source_parent, source_leaf):
            with self._parent(destination, create=True) as (dest_parent, dest_leaf):
                os.replace(source_leaf, dest_leaf, src_dir_fd=source_parent,
                           dst_dir_fd=dest_parent)

    def list(self, relative: str = ".") -> tuple[str, ...]:
        """List a pinned directory; entries are names, never followed paths."""
        self._require_open()
        if relative == ".":
            directory = os.dup(self.fd)
        else:
            parts = _parts(relative)
            directory = os.dup(self.fd)
            try:
                for part in parts:
                    child = os.open(part, _DIR_FLAGS, dir_fd=directory)
                    if not self._valid_component(os.fstat(child)):
                        os.close(child)
                        raise OSError("session authority child is not private")
                    os.close(directory)
                    directory = child
            except BaseException:
                os.close(directory)
                raise
        try:
            return tuple(os.listdir(directory))
        finally:
            os.close(directory)

    def reachable_path(self) -> Path | None:
        """Return an identity-verified current path within the pinned parent."""
        self._require_open()
        try:
            parent_info = self.display_path.parent.stat(follow_symlinks=False)
        except OSError:
            return None
        if (parent_info.st_dev, parent_info.st_ino) != (self.parent_device, self.parent_inode):
            return None
        for name in os.listdir(self.parent_fd):
            try:
                info = os.stat(name, dir_fd=self.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (info.st_dev, info.st_ino) == (self.device, self.inode):
                return self.display_path.parent / name
        return None

    def retention_record(self) -> dict[str, object]:
        """Return durable identity metadata, with a path only when verified."""
        path = self.reachable_path()
        return {"schema": 3, "device": self.device, "inode": self.inode,
                "uid": self.uid, "originalPath": f"<redacted>/{self.final_name}",
                "reachablePath": None if path is None else str(path)}

    def dispose(self) -> CleanupResult:
        """Sanitize files in the pinned tree while retaining its directory skeleton.

        POSIX has no operation that removes a directory *by its open descriptor*.
        Consequently this method never attempts to unlink the authority root by
        a mutable parent/name pair.  The caller owns descriptor closure after it
        has durably recorded the returned, truthful retained-root disposition.
        """
        self._require_open()
        _empty_directory(self.fd)
        return CleanupResult(
            "sanitized-retained-tree", self.device, self.inode, self.reachable_path())


def _parts(relative: str) -> tuple[str, ...]:
    value = os.fspath(relative)
    path = Path(value)
    if (not value or path.is_absolute() or "\0" in value
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise ValueError("session authority operation requires a safe relative path")
    return path.parts


def _private_component(info: os.stat_result) -> bool:
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) in (0o700, 0o2700))


def _empty_directory(directory_fd: int) -> None:
    """Recursively sanitize leaves while retaining every directory entry.

    A descendant can be replaced after its pinned descriptor is sanitized.
    There is no identity-checked POSIX ``rmdir`` operation, so disposal never
    removes directory namespace entries at any depth.
    """
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child)
                if _stat_identity(opened) != _stat_identity(info):
                    raise OSError("session authority child changed before cleanup")
                _empty_directory(child)
            finally:
                os.close(child)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if _stat_identity(current) != _stat_identity(info):
                raise OSError("session authority child changed during cleanup")
        else:
            os.unlink(name, dir_fd=directory_fd)


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    """Return the full identity admitted across a stat/open boundary."""
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


def resolve_session_authority(candidate: Path, *, approved_root: Path | None = None,
                              create: bool = True) -> SessionAuthority:
    """Admit and pin ``candidate`` beneath the documented trusted ancestor."""
    if not candidate.is_absolute() or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("session root requires an absolute path and no-follow support")
    candidate = Path(os.path.normpath(os.fspath(candidate)))
    roots = ([approved_root] if approved_root is not None else
             [Path("/tmp"), Path("/var/tmp")])
    anchor = next((Path(root).resolve(strict=True) for root in roots
                   if root is not None and (candidate == Path(root)
                   or candidate.is_relative_to(Path(root)))), None)
    if anchor is None:
        raise ValueError("session root is outside a trusted temporary/private ancestor")
    if approved_root is not None:
        anchor_info = anchor.stat(follow_symlinks=False)
        if not _private_component(anchor_info):
            raise ValueError("caller-approved session ancestor is not private")

    relative_parts = candidate.relative_to(anchor).parts
    if not relative_parts:
        raise ValueError("session authority must be a private child of its trusted ancestor")

    created: list[tuple[int, str]] = []
    current = os.open(anchor, _DIR_FLAGS)
    final_parent = -1
    try:
        for index, part in enumerate(relative_parts):
            if index == len(relative_parts) - 1:
                final_parent = os.dup(current)
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                parent_copy = os.dup(current)
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                    created.append((parent_copy, part))
                except BaseException:
                    os.close(parent_copy)
                    raise
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            info = os.fstat(child)
            if not _private_component(info):
                os.close(child)
                raise OSError("session path component is not caller-owned and private")
            os.close(current)
            current = child
        info = os.fstat(current)
        parent_info = os.fstat(final_parent)
        authority = SessionAuthority(candidate, current, info.st_dev, info.st_ino,
                                      info.st_uid, stat.S_IMODE(info.st_mode),
                                      final_parent, relative_parts[-1],
                                      parent_info.st_dev, parent_info.st_ino)
        current = -1
        final_parent = -1
        return authority
    except (OSError, ValueError) as exc:
        for parent_fd, name in reversed(created):
            try: os.rmdir(name, dir_fd=parent_fd)
            except OSError: pass
        raise ValueError("session root must be caller-owned and private") from exc
    finally:
        if current >= 0: os.close(current)
        if final_parent >= 0: os.close(final_parent)
        for parent_fd, _ in created: os.close(parent_fd)


def resolve_bundle_session_authority(
    bundle_root: Path, relative: str, *, create: bool = False,
) -> SessionAuthority:
    """Pin a bundle-owned session directory without following any component.

    Bundle-local state keeps its historical workspace permissions; unlike an
    external temporary authority, existing components are not required to be
    exactly ``0700`` or ``02700``. They must be directories owned by the effective user.
    Missing components are created with the ordinary directory mode subject to
    the process umask, matching ``Path.mkdir(parents=True)`` compatibility.
    """
    parts = _parts(relative)
    root = Path(bundle_root)
    if not root.is_absolute():
        root = Path(os.path.abspath(root))
    current = os.open(root, _DIR_FLAGS)
    parent = -1
    created: list[tuple[int, str]] = []
    try:
        root_info = os.fstat(current)
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid():
            raise OSError("bundle root is not caller-owned")
        for index, part in enumerate(parts):
            if index == len(parts) - 1:
                parent = os.dup(current)
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                parent_copy = os.dup(current)
                try:
                    try:
                        os.mkdir(part, 0o777, dir_fd=current)
                        created.append((parent_copy, part))
                    except FileExistsError:
                        os.close(parent_copy)
                except BaseException:
                    if parent_copy >= 0:
                        try:
                            os.close(parent_copy)
                        except OSError:
                            pass
                    raise
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                os.close(child)
                raise OSError("bundle session component is not caller-owned")
            os.close(current)
            current = child
        result = SessionAuthority._from_fd(
            root.joinpath(*parts), current, parent, parts[-1],
            strict_private_components=False,
        )
        current = -1
        parent = -1
        return result
    except BaseException:
        for parent_fd, name in reversed(created):
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if current >= 0:
            os.close(current)
        if parent >= 0:
            os.close(parent)
        for parent_fd, _ in created:
            os.close(parent_fd)


def resolve_bundle_root_authority(bundle_root: Path) -> SessionAuthority:
    """Pin one caller-owned bundle directory for later relative state access.

    The returned descriptor is the authority even if any pathname ancestor is
    subsequently renamed or replaced.  Bundle roots retain their historical
    workspace permissions; ownership and directory type are the admission
    boundary, rather than the 0700-or-02700 policy used by external state roots.
    """
    root = Path(bundle_root)
    if not root.is_absolute():
        root = Path(os.path.abspath(root))
    directory = os.open(root, _DIR_FLAGS)
    parent = -1
    try:
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("bundle root is not caller-owned")
        parent = os.open("..", _DIR_FLAGS, dir_fd=directory)
        result = SessionAuthority._from_fd(
            root, directory, parent, root.name, strict_private_components=False,
        )
        directory = -1
        parent = -1
        return result
    finally:
        if directory >= 0:
            os.close(directory)
        if parent >= 0:
            os.close(parent)


def temporary_session_authority(*, prefix: str, parent: Path) -> SessionAuthority:
    """Create a finalizer-free mkdtemp root and return its pinned authority."""
    candidate = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        candidate.chmod(0o700, follow_symlinks=False)
        # System temporary parents are trusted by policy even though their
        # sticky directory is intentionally shared; the new child is 0700.
        return resolve_session_authority(candidate, create=False)
    except BaseException:
        # Allocation failed before any child process could receive authority.
        # Explicit cleanup is safe here and no TemporaryDirectory finalizer exists.
        try:
            os.rmdir(candidate)
        except OSError:
            pass
        raise
