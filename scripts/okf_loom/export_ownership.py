"""Opt-in managed ownership for static/SPA build output (roadmap X7).

``build --output-policy managed`` publishes a closed, versioned
``.okf-export.json`` manifest at the destination root listing every file and
directory okf-loom published. A later managed build replaces the destination
only when every entry present is one the manifest claims and is byte-identical
to that claim. An unmanaged nonempty destination is refused unless
``--adopt-output`` is given and the tree equals a fresh staging byte for byte.

Three checks run: a write-free preflight before any build write, an
authoritative recheck while the publisher holds the destination-parent lock,
and a metadata recheck after the old tree is quarantined (rolled back on any
change). Inspection never follows symlinks and never opens special files.

A hash only proves bytes are unchanged since the manifest was written; the
authority to replace comes from the operator's explicit flag, filesystem write
permission, and proof that replacement destroys nothing okf-loom did not
publish. The legacy ``replace`` policy never reads or writes the manifest.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

MANIFEST_NAME = ".okf-export.json"
MANIFEST_SCHEMA = "okf-loom/export-ownership"
MANIFEST_GENERATOR = "okf-loom"
MANIFEST_VERSION = 1
# Same bound as MAX_PORTABLE_MANIFEST_BYTES in render.py (16 MiB).
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_ENTRIES = 200_000
MAX_FINDINGS = 20
_MAX_SAFE_INT = 2 ** 53 - 1
_TOP_KEYS = frozenset({"bundle_revision", "entries", "generator", "loom_version",
                       "schema", "target", "version"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC

OUTPUT_POLICIES = ("replace", "managed")
MANAGED_TARGETS = ("static", "spa")

UNMANAGED_NONEMPTY = "export.ownership.unmanaged_nonempty"
ADOPT_MISMATCH = "export.ownership.adopt_mismatch"
MANIFEST_MALFORMED = "export.ownership.manifest_malformed"
MANIFEST_UNSUPPORTED = "export.ownership.manifest_unsupported"
ENTRY_UNKNOWN = "export.ownership.entry_unknown"
ENTRY_MODIFIED = "export.ownership.entry_modified"
ENTRY_SYMLINK = "export.ownership.entry_symlink"
ENTRY_SPECIAL = "export.ownership.entry_special"


class ExportOwnershipError(FileExistsError):
    """A managed build refused to replace output it cannot prove it owns."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)

    def __str__(self) -> str:
        return self.args[0] if self.args else self.code


class _ManifestError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True)
class Entry:
    kind: str                 # "file" | "dir"
    size: int | None = None   # files only
    sha256: str | None = None  # files only, when hashed
    meta: tuple = ()          # (dev, ino, size, mtime_ns, ctime_ns) for L2


@dataclass
class _Walk:
    entries: dict[str, Entry] = field(default_factory=dict)
    findings: list[tuple[str, str]] = field(default_factory=list)  # (path, code)
    root_identity: tuple[int, int] | None = None


# --------------------------------------------------------------------------
# Manifest codec


def canonical_manifest_bytes(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8") + b"\n"


def _valid_entry_path(path: object) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\x00" in path:
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    segments = path.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return False
    return path != MANIFEST_NAME


def parse_manifest(data: bytes) -> dict[str, Entry]:
    """Parse canonical manifest bytes into ``{path: Entry}``; fail closed."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise _ManifestError(MANIFEST_MALFORMED, "manifest exceeds 16 MiB")
    try:
        obj = json.loads(data.decode("utf-8"))
        canonical = canonical_manifest_bytes(obj)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise _ManifestError(MANIFEST_MALFORMED, "manifest is not canonical JSON") from None
    if canonical != data:
        raise _ManifestError(MANIFEST_MALFORMED, "manifest bytes are not canonical")
    if not isinstance(obj, dict):
        raise _ManifestError(MANIFEST_MALFORMED, "manifest is not an object")
    if obj.get("schema") != MANIFEST_SCHEMA or obj.get("generator") != MANIFEST_GENERATOR:
        raise _ManifestError(MANIFEST_MALFORMED, "manifest schema/generator mismatch")
    if "version" not in obj:
        raise _ManifestError(MANIFEST_MALFORMED, "manifest version missing")
    version = obj["version"]
    if type(version) is not int or version != MANIFEST_VERSION:
        raise _ManifestError(MANIFEST_UNSUPPORTED, "manifest version is not 1")
    if set(obj) != _TOP_KEYS:
        raise _ManifestError(MANIFEST_MALFORMED, "manifest keys are not exactly the v1 set")
    if obj["target"] not in MANAGED_TARGETS:
        raise _ManifestError(MANIFEST_MALFORMED, "manifest target invalid")
    for key, limit in (("bundle_revision", 128), ("loom_version", 64)):
        value = obj[key]
        if not isinstance(value, str) or not 1 <= len(value) <= limit:
            raise _ManifestError(MANIFEST_MALFORMED, f"manifest {key} invalid")
    raw_entries = obj["entries"]
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_MANIFEST_ENTRIES:
        raise _ManifestError(MANIFEST_MALFORMED, "manifest entries invalid")
    entries: dict[str, Entry] = {}
    previous: str | None = None
    for item in raw_entries:
        if not isinstance(item, dict):
            raise _ManifestError(MANIFEST_MALFORMED, "manifest entry is not an object")
        path = item.get("path")
        kind = item.get("type")
        if not _valid_entry_path(path):
            raise _ManifestError(MANIFEST_MALFORMED, "manifest entry path invalid")
        # Code-point order equals UTF-8 byte order for valid scalar values.
        if previous is not None and path <= previous:
            raise _ManifestError(MANIFEST_MALFORMED, "manifest entries unsorted or duplicated")
        previous = path
        if kind == "dir":
            if set(item) != {"path", "type"}:
                raise _ManifestError(MANIFEST_MALFORMED, "manifest dir entry keys invalid")
            entries[path] = Entry("dir")
        elif kind == "file":
            if set(item) != {"path", "type", "size", "sha256"}:
                raise _ManifestError(MANIFEST_MALFORMED, "manifest file entry keys invalid")
            size, digest = item["size"], item["sha256"]
            if type(size) is not int or not 0 <= size <= _MAX_SAFE_INT:
                raise _ManifestError(MANIFEST_MALFORMED, "manifest file size invalid")
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise _ManifestError(MANIFEST_MALFORMED, "manifest file sha256 invalid")
            entries[path] = Entry("file", size, digest)
        else:
            raise _ManifestError(MANIFEST_MALFORMED, "manifest entry type invalid")
    for path in entries:
        parent = path.rpartition("/")[0]
        if parent and entries.get(parent, Entry("file")).kind != "dir":
            raise _ManifestError(MANIFEST_MALFORMED, "manifest parent directory closure broken")
    return entries


def build_manifest(entries: dict[str, Entry], *, target: str, revision: str,
                   loom_version: str) -> bytes:
    items = []
    for path in sorted(entries):
        entry = entries[path]
        if entry.kind == "dir":
            items.append({"path": path, "type": "dir"})
        else:
            items.append({"path": path, "type": "file", "size": entry.size,
                          "sha256": entry.sha256})
    return canonical_manifest_bytes({
        "bundle_revision": revision, "entries": items, "generator": MANIFEST_GENERATOR,
        "loom_version": loom_version, "schema": MANIFEST_SCHEMA, "target": target,
        "version": MANIFEST_VERSION,
    })


# --------------------------------------------------------------------------
# No-follow tree walker


def _meta(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _walk(root_fd: int, *, hash_files: bool, skip_manifest: bool,
          limit: int | None = None) -> _Walk:
    """Inventory a pinned directory without following links or opening specials.

    Depth-first with immediate descent: open descriptors never exceed the tree
    depth plus one, however wide a directory is. ``limit`` defaults to
    ``MAX_MANIFEST_ENTRIES`` resolved at call time.
    """
    if limit is None:
        limit = MAX_MANIFEST_ENTRIES
    result = _Walk()
    root_info = os.fstat(root_fd)
    result.root_identity = (root_info.st_dev, root_info.st_ino)
    root = os.dup(root_fd)
    try:
        stack: list[tuple[int, str, Iterator[str]]] = [
            (root, "", iter(sorted(os.listdir(root))))]
    except BaseException:
        os.close(root)
        raise
    try:
        while stack:
            dir_fd, prefix, names = stack[-1]
            name = next(names, None)
            if name is None:
                stack.pop()
                os.close(dir_fd)
                continue
            rel = f"{prefix}{name}"
            if skip_manifest and not prefix and name == MANIFEST_NAME:
                continue
            if len(result.entries) + len(result.findings) >= limit:
                result.findings.append((rel, ENTRY_UNKNOWN))
                return result
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                result.findings.append((rel.encode("utf-8", "backslashreplace")
                                        .decode("utf-8"), ENTRY_UNKNOWN))
                continue
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue  # vanished; absence is judged by the comparison
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                result.findings.append((rel, ENTRY_SYMLINK))
            elif stat.S_ISDIR(mode):
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError as err:
                    _raise_if_resource(err)
                    result.findings.append((rel, ENTRY_MODIFIED))
                    continue
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        result.findings.append((rel, ENTRY_MODIFIED))
                        os.close(child)
                        continue
                    children = iter(sorted(os.listdir(child)))
                except BaseException:
                    os.close(child)
                    raise
                result.entries[rel] = Entry("dir", meta=(info.st_dev, info.st_ino))
                stack.append((child, rel + "/", children))
            elif stat.S_ISREG(mode):
                entry = _inspect_file(dir_fd, name, info, hash_files)
                if entry is None:
                    result.findings.append((rel, ENTRY_MODIFIED))
                else:
                    result.entries[rel] = entry
            else:
                result.findings.append((rel, ENTRY_SPECIAL))
    finally:
        for fd, _, _ in stack:
            os.close(fd)
    return result


# Exhausted descriptors or memory say nothing about the tree; report them as
# the operational failure they are instead of a false ownership finding.
_RESOURCE_ERRNOS = frozenset({errno.EMFILE, errno.ENFILE, errno.ENOMEM})


def _raise_if_resource(err: OSError) -> None:
    if err.errno in _RESOURCE_ERRNOS:
        raise err


def _inspect_file(dir_fd: int, name: str, info: os.stat_result, hash_files: bool) -> Entry | None:
    if not hash_files:
        return Entry("file", info.st_size, None, _meta(info))
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as err:
        _raise_if_resource(err)
        return None
    try:
        opened = os.fstat(fd)
        # A swap to a FIFO/device/other inode after lstat is caught without blocking.
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
            return None
        digest = _hash_fd(fd)
        return Entry("file", opened.st_size, digest, _meta(opened))
    finally:
        os.close(fd)


def _open_child_dir(parent_fd: int, name: str) -> int | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"publication path must not use symlinks: {name}")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(name)
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        os.close(fd)
        raise ValueError(f"publication path changed during inspection: {name}")
    return fd


def _read_manifest(root_fd: int) -> tuple[bytes, tuple] | tuple[str, str] | None:
    """Return ``(bytes, meta)``, None when absent, or a ``(path, code)`` finding."""
    try:
        info = os.stat(MANIFEST_NAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return (MANIFEST_NAME, ENTRY_SYMLINK)
    if stat.S_ISDIR(info.st_mode):
        return (MANIFEST_NAME, MANIFEST_MALFORMED)
    if not stat.S_ISREG(info.st_mode):
        return (MANIFEST_NAME, ENTRY_SPECIAL)
    if info.st_size > MAX_MANIFEST_BYTES:
        return (MANIFEST_NAME, MANIFEST_MALFORMED)
    try:
        fd = os.open(MANIFEST_NAME, _FILE_FLAGS, dir_fd=root_fd)
    except OSError as err:
        _raise_if_resource(err)
        return (MANIFEST_NAME, ENTRY_MODIFIED)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
            return (MANIFEST_NAME, ENTRY_MODIFIED)
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_MANIFEST_BYTES:
                return (MANIFEST_NAME, MANIFEST_MALFORMED)
            chunks.append(chunk)
        return b"".join(chunks), _meta(opened)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Classification


@dataclass
class Decision:
    state: str   # "create" | "managed" | "adopt"
    walk: _Walk | None = None


def _compare(expected: dict[str, Entry], walk: _Walk, *, missing_code: str,
             extra_code: str, differ_code: str) -> list[tuple[str, str]]:
    findings = list(walk.findings)
    flagged = {path for path, _ in findings}
    for path, entry in walk.entries.items():
        claim = expected.get(path)
        if claim is None:
            findings.append((path, extra_code))
        elif claim.kind != entry.kind or (entry.kind == "file" and (
                claim.size != entry.size or claim.sha256 != entry.sha256)):
            findings.append((path, differ_code))
    for path in expected:
        if path not in walk.entries and path not in flagged:
            findings.append((path, missing_code))
    return findings


def _deny(code: str, dst: Path, findings: list[tuple[str, str]], *, phase: str,
          hint: str = "") -> ExportOwnershipError:
    total = len(findings)
    findings = sorted(findings)[:MAX_FINDINGS]
    first = findings[0][0] if findings else "."
    if findings:
        code = findings[0][1]
    tail = "no files changed"
    if phase != "preflight":
        tail = "no files changed; destination unchanged; private staging removed"
    message = f"{code}: {dst} ({total} finding(s); first: {first!r}); {tail}"
    if hint:
        message += f"; {hint}"
    return ExportOwnershipError(code, message)


_UNMANAGED_HINT = ("use an empty or absent directory, or --adopt-output only for an "
                   "unmodified export of this bundle")


def _classify(root_fd: int | None, dst: Path, *, adopt: bool, phase: str,
              staged: dict[str, Entry] | None) -> Decision:
    """Classify the destination; ``staged`` is required to judge adoption."""
    if root_fd is None:
        return Decision("create")
    if not os.listdir(root_fd):
        return Decision("create", _Walk(root_identity=_identity(root_fd)))
    raw = _read_manifest(root_fd)
    if raw is not None and isinstance(raw[0], str):
        raise _deny(raw[1], dst, [raw], phase=phase)
    if raw is not None:
        data, manifest_meta = raw
        try:
            claimed = parse_manifest(data)
        except _ManifestError as err:
            raise _deny(err.code, dst, [(MANIFEST_NAME, err.code)], phase=phase) from None
        walk = _walk(root_fd, hash_files=True, skip_manifest=True)
        findings = _compare(claimed, walk, missing_code=ENTRY_MODIFIED,
                            extra_code=ENTRY_UNKNOWN, differ_code=ENTRY_MODIFIED)
        if findings:
            raise _deny(findings[0][1], dst, findings, phase=phase)
        # Recorded for the post-quarantine recheck, which covers the whole tree.
        walk.entries[MANIFEST_NAME] = Entry("file", manifest_meta[2], None, manifest_meta)
        return Decision("managed", walk)
    if not adopt:
        raise _deny(UNMANAGED_NONEMPTY, dst, [(".", UNMANAGED_NONEMPTY)], phase=phase,
                    hint=_UNMANAGED_HINT)
    if staged is None:
        # Preflight: structure only; content equality needs the fresh staging.
        walk = _walk(root_fd, hash_files=False, skip_manifest=False)
        if walk.findings:
            raise _deny(walk.findings[0][1], dst, walk.findings, phase=phase)
        return Decision("adopt", walk)
    walk = _walk(root_fd, hash_files=True, skip_manifest=False)
    if walk.findings:
        raise _deny(walk.findings[0][1], dst, walk.findings, phase=phase)
    findings = _compare(staged, walk, missing_code=ADOPT_MISMATCH,
                        extra_code=ADOPT_MISMATCH, differ_code=ADOPT_MISMATCH)
    if findings:
        raise _deny(ADOPT_MISMATCH, dst, findings, phase=phase)
    return Decision("adopt", walk)


def _identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino)


def preflight(out_dir: Path, *, adopt: bool) -> str:
    """Write-free admission before any build write; returns the observed state."""
    out_dir = Path(out_dir)
    from .io_utils import _tree_directory_fd

    parent = out_dir.parent
    probe = parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe != parent:
        return "create"  # a missing ancestor means the destination is absent
    with _tree_directory_fd(parent) as parent_fd:
        root_fd = _open_child_dir(parent_fd, out_dir.name)
        try:
            return _classify(root_fd, out_dir, adopt=adopt, phase="preflight",
                             staged=None).state
        finally:
            if root_fd is not None:
                os.close(root_fd)


def seal_stage(stage: Path, *, target: str, revision: str, loom_version: str) -> dict[str, Entry]:
    """Write the manifest into private staging; returns the staged inventory."""
    from .io_utils import _tree_directory_fd, atomic_write_bytes

    stage = Path(stage)
    with _tree_directory_fd(stage) as stage_fd:
        if os.path.lexists(stage / MANIFEST_NAME):
            raise RuntimeError(f"build output collides with reserved {MANIFEST_NAME}")
        walk = _walk(stage_fd, hash_files=True, skip_manifest=False,
                     limit=MAX_MANIFEST_ENTRIES + 1)
    if walk.findings or len(walk.entries) > MAX_MANIFEST_ENTRIES:
        raise RuntimeError("managed build staging holds entries outside the manifest "
                           "contract (non-regular files or over the entry limit)")
    data = build_manifest(walk.entries, target=target, revision=revision,
                          loom_version=loom_version)
    if len(data) > MAX_MANIFEST_BYTES:
        raise RuntimeError("managed build manifest exceeds 16 MiB")
    atomic_write_bytes(stage / MANIFEST_NAME, data)
    return walk.entries


class PublicationVerifier:
    """``atomic_replace_tree`` hook: L1 under the lock, L2 after quarantine."""

    def __init__(self, out_dir: Path, *, adopt: bool, staged: dict[str, Entry]) -> None:
        self.out_dir = Path(out_dir)
        self.adopt = adopt
        self.staged = staged
        self.decision: Decision | None = None

    @property
    def ownership(self) -> str:
        state = self.decision.state if self.decision else "create"
        return {"create": "created", "managed": "replaced_managed", "adopt": "adopted"}[state]

    def __call__(self, parent_fd: int, name: str, phase: str) -> None:
        if phase == "locked":
            root_fd = _open_child_dir(parent_fd, name)
            try:
                self.decision = _classify(root_fd, self.out_dir, adopt=self.adopt,
                                          phase="locked", staged=self.staged)
            finally:
                if root_fd is not None:
                    os.close(root_fd)
        elif phase == "quarantined":
            self._recheck(parent_fd, name)
        else:
            raise ValueError(f"unknown verifier phase: {phase!r}")

    def _recheck(self, parent_fd: int, name: str) -> None:
        recorded = self.decision.walk if self.decision else None
        root_fd = _open_child_dir(parent_fd, name)
        if root_fd is None or recorded is None:
            if root_fd is not None:
                os.close(root_fd)
            raise _deny(ENTRY_MODIFIED, self.out_dir, [(".", ENTRY_MODIFIED)],
                        phase="quarantined")
        try:
            if _identity(root_fd) != recorded.root_identity:
                raise _deny(ENTRY_MODIFIED, self.out_dir, [(".", ENTRY_MODIFIED)],
                            phase="quarantined")
            # The recorded walk may list the manifest besides a full manifest
            # of entries, so the cap here is one more than the manifest's.
            current = _walk(root_fd, hash_files=False, skip_manifest=False,
                            limit=MAX_MANIFEST_ENTRIES + 1)
        finally:
            os.close(root_fd)
        findings = list(current.findings)
        flagged = {path for path, _ in findings}
        for path, entry in current.entries.items():
            before = recorded.entries.get(path)
            if before is None:
                findings.append((path, ENTRY_UNKNOWN))
            elif before.kind != entry.kind or before.meta != entry.meta:
                findings.append((path, ENTRY_MODIFIED))
        for path in recorded.entries:
            # As in _compare: a path already flagged (symlink, special) keeps
            # its specific code instead of also counting as missing.
            if path not in current.entries and path not in flagged:
                findings.append((path, ENTRY_MODIFIED))
        if findings:
            raise _deny(findings[0][1], self.out_dir, findings, phase="quarantined")


def publication_verifier(out_dir: Path, *, adopt: bool,
                         staged: dict[str, Entry]) -> PublicationVerifier:
    return PublicationVerifier(out_dir, adopt=adopt, staged=staged)


VerifyPrevious = Callable[[int, str, str], None]
