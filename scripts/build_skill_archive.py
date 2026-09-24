#!/usr/bin/env python3
"""Build and verify a deterministic skill archive from an immutable Git commit.

The release authority is the recursively enumerated tree of one captured commit,
never the mutable checkout.  Only regular tracked blobs are admitted.  The
builder reads every byte by object ID and the verifier independently compares
archive paths, bytes, modes, ordering, timestamps, and compression with that
same commit-derived inventory.

Exclusions identify concrete local-state locations and credential/key basenames,
not semantic substrings in code/documentation names or dependency lockfiles.
Unhandled credential-shaped data filenames block the release for local review.
The content check detects bounded PEM private-key blocks, not arbitrary secrets;
neither a tracked commit nor a successful build establishes confidentiality.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import hashlib
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import BinaryIO, NamedTuple

__all__ = [
    "SecretScrubError",
    "ReleaseEntry",
    "is_path_denied",
    "name_matches_denylist",
    "release_inventory",
    "build_skill_archive",
    "verify_zip",
    "scan_archive_contents",
    "main",
]


class SecretScrubError(Exception):
    """Raised when the source or resulting release archive violates policy."""


_DENY_DIR_NAMES = frozenset(
    {
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
        ".venv", "venv", "env", ".env", ".eggs", ".idea", ".vscode",
        ".git", ".hg", ".svn", ".cache", ".config", "node_modules",
        "build", "dist", "_site", "okf-site", "htmlcov", ".aic",
        ".playwright-mcp", ".okf-loom", ".aws", ".kube", ".gnupg",
        ".docker", ".ansible", ".terraform.d", ".ssh",
    }
)
_DENY_DIR_NAMES_LOWER = frozenset(value.lower() for value in _DENY_DIR_NAMES)
_DENY_NAME_GLOBS = (
    "*.pyc", "*.pyo", "*.pyd", "*.swp", "*.swo", "*~", ".ds_store",
    "thumbs.db", ".coverage", ".coverage.*", "htmlcov", "*.egg-info",
    "*.egg", ".env", ".env.*", ".envrc", "*.pem", "*.key", "*.p12",
    "*.pfx", "*.keystore", "*.jks", "*.gpg", "*.asc", "id_rsa",
    "id_rsa.*", "id_dsa", "id_dsa.*", "id_ecdsa", "id_ecdsa.*",
    "id_ed25519", "id_ed25519.*", ".netrc", ".npmrc", ".pypirc",
    ".git-credentials", ".htpasswd", "*.htpasswd", ".kubeconfig",
    "*.kubeconfig", "*.kdbx", "*.ppk", "*.ovpn", "secrets.yml",
    "secrets.yaml", "service_account*.json", "*serviceaccount*.json", ".token",
)
# These are review signals, not silent exclusions or a general secret detector.
# Whole delimited words in data-like basenames catch e.g. access-token.json;
# tokenize.py, private_api.md and private/ namespaces do not trigger this signal.
# No extension is proof that the content is safe to publish.
_SENSITIVE_ARTIFACT_WORD_RE = re.compile(
    r"(?:^|[._-])(?:secrets?|credentials?|tokens?|passwords?|passwd|apikey|api_key|private)"
    r"(?:$|[._-])"
)
_SENSITIVE_DATA_SUFFIXES = frozenset(
    {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
     ".txt", ".csv", ".tsv", ".db", ".sqlite", ".sqlite3", ".bak", ".backup", ".log"}
)
_SENSITIVE_STORAGE_COMPONENTS = frozenset(
    {"secrets", ".secrets", "credentials", ".credentials", "passwords", ".passwords"}
)
# Historical/reference inputs are useful in a checkout but are not skill
# release artifacts.  Components make this apply at any nesting depth.
_RELEASE_EXCLUDED_COMPONENTS = frozenset(
    {"incoming-v1-theme", "ui-overhaul-final"}
)
_RELEASE_EXCLUDED_ROOT_PATHS = frozenset({".gitignore"})
# README showcase media (screenshots, GIFs and MP4 clips, about 13 MB) illustrate
# the repository page; a loaded skill never reads them.  Anchored at the
# repository root so a nested bundle's own docs/media/ still ships.
_RELEASE_EXCLUDED_ROOT_DIRS = ("docs/media/",)
_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_SCAN_CHUNK_BYTES = 64 * 1024
_SCAN_OVERLAP_BYTES = 16 * 1024
_PEM_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"
    rb"[A-Za-z0-9+/=\s]{8,8000}"
    rb"-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----"
)
_PUBLICATION_LOCKS_GUARD = threading.Lock()
_PUBLICATION_LOCKS: dict[str, threading.Lock] = {}
_MAX_RECOVERY_BACKUPS = 8


class ReleaseEntry(NamedTuple):
    """One regular tracked blob admitted to the release."""

    path: str
    oid: str
    mode: int
    data: bytes


def name_matches_denylist(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pattern) for pattern in _DENY_NAME_GLOBS)


def _is_sensitive_artifact_path(path: str) -> bool:
    """Recognize unhandled data-like credential names without judging contents."""
    parts = PurePosixPath(path.lower()).parts
    if any(part in _SENSITIVE_STORAGE_COMPONENTS for part in parts[:-1]):
        return True
    name = parts[-1]
    suffix = PurePosixPath(name).suffix
    return (not suffix or suffix in _SENSITIVE_DATA_SUFFIXES) and bool(
        _SENSITIVE_ARTIFACT_WORD_RE.search(name)
    )


def _safe_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(path) and not path.startswith(("/", "\\")) and "\\" not in path and all(
        part not in {"", ".", ".."} for part in pure.parts
    )


def is_path_denied(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    return (
        not _safe_path(normalized)
        or any(part.lower() in _DENY_DIR_NAMES_LOWER for part in parts)
        or any(part in _RELEASE_EXCLUDED_COMPONENTS for part in parts)
        or any(name_matches_denylist(part) for part in parts)
    )


def _git(source: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "-C", str(source), *args], input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except OSError as exc:
        raise SecretScrubError(f"cannot execute git: {exc}") from exc
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise SecretScrubError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout


def capture_commit(source: Path | str, revision: str = "HEAD") -> str:
    source = Path(source).resolve()
    if not source.is_dir():
        raise SecretScrubError(f"source directory not found: {source}")
    root = Path(_git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if root != source:
        raise SecretScrubError(f"source must be the Git worktree root: {root}")
    commit = _git(source, "rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise SecretScrubError(f"Git returned an invalid commit object ID: {commit!r}")
    return commit


def release_inventory(source: Path | str, revision: str = "HEAD") -> tuple[str, list[ReleaseEntry]]:
    """Return the canonical release inventory and the exact captured commit."""
    source = Path(source).resolve()
    commit = capture_commit(source, revision)
    raw = _git(source, "ls-tree", "-rz", "--full-tree", commit)
    entries: list[ReleaseEntry] = []
    seen: set[str] = set()
    for tree_entry, record in enumerate(raw.split(b"\0"), start=1):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode_text, kind, oid_bytes = metadata.split(b" ", 2)
            path = path_bytes.decode("utf-8")
            oid = oid_bytes.decode("ascii")
            mode = int(mode_text, 8)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SecretScrubError("malformed or non-UTF-8 path in Git tree") from exc
        if path in seen or not _safe_path(path):
            raise SecretScrubError(f"unsafe or duplicate Git tree path: {path!r}")
        seen.add(path)
        if (
            path in _RELEASE_EXCLUDED_ROOT_PATHS
            or path.startswith(_RELEASE_EXCLUDED_ROOT_DIRS)
            or is_path_denied(path)
        ):
            continue
        if _is_sensitive_artifact_path(path):
            # A filename can itself contain a secret. Give a deterministic local
            # inventory locator, never echo the path or credential-like bytes.
            raise SecretScrubError(
                f"suspicious sensitive artifact at Git tree entry {tree_entry} "
                f"(path redacted); inspect commit {commit} locally with "
                f"git ls-tree -rz --full-tree {commit} (1-based NUL-separated records). "
                "Remove sensitive material from the release commit, or refine the "
                "concrete path policy only after review; no automatic inclusion."
            )
        if kind != b"blob" or mode not in {0o100644, 0o100755}:
            raise SecretScrubError(
                f"unsupported tracked entry {path!r}: type={kind.decode('ascii', 'replace')} mode={mode:o}"
            )
        data = _git(source, "cat-file", "blob", oid)
        entries.append(ReleaseEntry(path, oid, mode, data))
    entries.sort(key=lambda entry: entry.path)
    return commit, entries


def _archive_name(prefix: str, path: str) -> str:
    return f"{prefix}/{path}"


def _write_canonical_zip(
    out: Path,
    inventory: list[ReleaseEntry],
    prefix: str,
    fixed_time: tuple[int, int, int, int, int, int],
) -> None:
    """Write the one complete byte representation allowed by release policy."""
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in inventory:
            info = zipfile.ZipInfo(_archive_name(prefix, entry.path), fixed_time)
            info.create_system = 3
            info.external_attr = entry.mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, entry.data)


def _private_sibling(out: Path, purpose: str) -> Path:
    """Create one mode-0600 temporary on the destination filesystem."""
    fd, name = tempfile.mkstemp(prefix=f".{out.name}.{purpose}-", dir=out.parent)
    os.close(fd)
    return Path(name)


def _fsync_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publication_lock(out: Path) -> threading.Lock:
    key = os.fspath(out)
    with _PUBLICATION_LOCKS_GUARD:
        return _PUBLICATION_LOCKS.setdefault(key, threading.Lock())


def _preserve_prior_output(out: Path) -> Path | None:
    """Durably hard-link the current output into a bounded recovery slot."""
    if not out.exists():
        return None
    for slot in range(1, _MAX_RECOVERY_BACKUPS + 1):
        backup = out.parent / f".{out.name}.recovery-{slot:02d}"
        try:
            os.link(out, backup)
        except FileExistsError:
            continue
        try:
            _fsync_dir(out.parent)
        except OSError:
            backup.unlink(missing_ok=True)
            try:
                _fsync_dir(out.parent)
            except OSError:
                pass
            raise
        return backup
    pattern = out.parent / f".{out.name}.recovery-NN"
    raise SecretScrubError(
        f"archive publication blocked: all {_MAX_RECOVERY_BACKUPS} recovery slots "
        f"are occupied near {pattern}; inspect and remove retained backups after recovery"
    )


def _restore_prior_output(out: Path, backup: Path | None) -> None:
    if backup is None:
        out.unlink(missing_ok=True)
        _fsync_dir(out.parent)
        return

    rollback = _private_sibling(out, "rollback")
    rollback.unlink()
    try:
        os.link(backup, rollback)
        os.replace(rollback, out)
        _fsync_dir(out.parent)
    finally:
        rollback.unlink(missing_ok=True)


def _error_diagnostic(exc: OSError) -> str:
    """Describe an OS failure without echoing paths or payload-like messages."""
    errno = f" errno={exc.errno}" if exc.errno is not None else ""
    return f"{type(exc).__name__}{errno}"


def _recovery_sync_diagnostic(parent: Path) -> str:
    try:
        _fsync_dir(parent)
    except OSError as exc:
        return f"failed ({_error_diagnostic(exc)})"
    return "succeeded"


def build_skill_archive(
    source: Path | str,
    out: Path | str,
    *,
    revision: str = "HEAD",
    fixed_time: tuple[int, int, int, int, int, int] = _FIXED_TIMESTAMP,
) -> list[str]:
    """Build from one captured commit; workspace dirt and movement are irrelevant."""
    source = Path(source).resolve()
    out = Path(out).resolve()
    commit, inventory = release_inventory(source, revision)
    prefix = source.name
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = _private_sibling(out, "build")
    manifest: list[str]
    try:
        _write_canonical_zip(tmp_out, inventory, prefix, fixed_time)
        manifest = verify_zip(
            tmp_out, source=source, revision=commit, fixed_time=fixed_time,
            verification="reproducible",
        )
        try:
            _fsync_file(tmp_out)
            with _publication_lock(out):
                backup = _preserve_prior_output(out)
                published = False
                backup_removable = False
                publication_error: OSError | None = None
                try:
                    os.replace(tmp_out, out)
                    published = True
                    _fsync_file(out)
                    _fsync_dir(out.parent)
                    backup_removable = True
                except OSError as exc:
                    publication_error = exc
                    if published:
                        try:
                            _restore_prior_output(out, backup)
                        except OSError as rollback_exc:
                            if backup is not None:
                                recovery_sync = _recovery_sync_diagnostic(out.parent)
                                raise SecretScrubError(
                                    "archive publication failed after atomic replacement; "
                                    "rollback also failed and output durability is uncertain; "
                                    f"prior archive retained at recovery path {backup}; "
                                    f"publication={_error_diagnostic(exc)}; "
                                    f"rollback={_error_diagnostic(rollback_exc)}; "
                                    f"recovery parent fsync {recovery_sync}"
                                ) from exc
                            raise SecretScrubError(
                                "archive publication failed after atomic replacement and "
                                "rollback also failed; no prior archive existed and output "
                                "state or durability is uncertain; "
                                f"publication={_error_diagnostic(exc)}; "
                                f"rollback={_error_diagnostic(rollback_exc)}"
                            ) from exc
                    backup_removable = True
                if backup is not None and backup_removable:
                    try:
                        backup.unlink()
                    except OSError as cleanup_exc:
                        raise SecretScrubError(
                            "archive output state is durable but recovery cleanup failed; "
                            f"retained backup at recovery path {backup}; "
                            f"cleanup={_error_diagnostic(cleanup_exc)}"
                        ) from cleanup_exc
                if publication_error is not None:
                    raise SecretScrubError(
                        f"archive publication failed: {publication_error}"
                    ) from publication_error
        except OSError as exc:
            raise SecretScrubError(f"archive publication failed: {exc}") from exc
    finally:
        tmp_out.unlink(missing_ok=True)
    return manifest


def _stream_contains_private_key(stream: BinaryIO) -> bool:
    tail = b""
    while True:
        chunk = stream.read(_SCAN_CHUNK_BYTES)
        if not chunk:
            return False
        window = tail + chunk
        if _PEM_PRIVATE_KEY_RE.search(window):
            return True
        tail = window[-_SCAN_OVERLAP_BYTES:]


def _scan_raw_archive(out: Path | str) -> bool:
    try:
        with Path(out).open("rb") as raw:
            return _stream_contains_private_key(raw)
    except OSError as exc:
        raise SecretScrubError(f"cannot scan archive {out}: {exc}") from exc


def _scan_archive_payloads(out: Path | str) -> list[str]:
    offenders: list[str] = []
    with zipfile.ZipFile(out, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        if duplicates:
            raise RuntimeError(f"duplicate entries cannot be scanned safely: {duplicates}")
        for info in infos:
            # Not ``info.is_dir()``: on Python 3.11 it indexes filename[-1] and
            # raises IndexError for an empty (NUL-truncated) tampered name.
            if info.filename.endswith("/"):
                continue
            with archive.open(info) as stream:
                if _stream_contains_private_key(stream):
                    offenders.append(info.filename)
    return offenders


def scan_archive_contents(out: Path | str) -> list[str]:
    """Scan raw archive bytes and every decompressed member with bounded memory."""
    offenders = ["<raw archive bytes>"] if _scan_raw_archive(out) else []
    offenders.extend(_scan_archive_payloads(out))
    return sorted(set(offenders))


def _signature_offsets(path: Path, signature: bytes) -> list[int]:
    offsets: list[int] = []
    overlap = len(signature) - 1
    tail = b""
    consumed = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_SCAN_CHUNK_BYTES):
            window = tail + chunk
            start = 0
            while True:
                found = window.find(signature, start)
                if found < 0:
                    break
                offsets.append(consumed - len(tail) + found)
                start = found + 1
            consumed += len(chunk)
            tail = window[-overlap:] if overlap else b""
    return offsets


def _zip_layout_failures(
    out: Path,
    infos: list[zipfile.ZipInfo],
    expected_infos: list[zipfile.ZipInfo],
) -> list[str]:
    """Return diagnostics for ZIP structures that high-level parsers normalize."""
    failures: list[str] = []
    expected_by_name = {info.filename: info for info in expected_infos}
    if infos and expected_infos and infos[0].header_offset != expected_infos[0].header_offset:
        failures.append(
            f"prepended bytes/local-header offset: {infos[0].header_offset} != "
            f"{expected_infos[0].header_offset}"
        )
    with out.open("rb") as stream:
        for info in infos:
            expected = expected_by_name.get(info.filename)
            if info.comment:
                failures.append(f"entry comment is not canonical for {info.filename}")
            if info.extra:
                failures.append(f"central-directory extra field is not canonical for {info.filename}")
            if info.flag_bits & 0x1:
                failures.append(f"encrypted entry is forbidden: {info.filename}")
            if info.flag_bits & 0x8:
                failures.append(f"data descriptor is not canonical: {info.filename}")
            if info.flag_bits & ~0x800:
                failures.append(
                    f"unsupported general-purpose flags for {info.filename}: 0x{info.flag_bits:04x}"
                )
            if expected is not None and info.flag_bits != expected.flag_bits:
                failures.append(
                    f"general-purpose flags differ for {info.filename}: "
                    f"0x{info.flag_bits:04x} != 0x{expected.flag_bits:04x}"
                )
            try:
                stream.seek(info.header_offset)
                header = stream.read(30)
                fields = struct.unpack("<4s5H3L2H", header)
                if fields[0] != b"PK\x03\x04":
                    raise ValueError("local header signature missing")
                local_flags, name_len, extra_len = fields[2], fields[-2], fields[-1]
                stream.seek(name_len, os.SEEK_CUR)
                if extra_len:
                    failures.append(f"local-header extra field is not canonical for {info.filename}")
                if local_flags != info.flag_bits:
                    failures.append(
                        f"local/central flags disagree for {info.filename}: "
                        f"0x{local_flags:04x} != 0x{info.flag_bits:04x}"
                    )
            except (OSError, struct.error, ValueError) as exc:
                failures.append(f"malformed local header for {info.filename}: {exc}")

    actual_eocd = _signature_offsets(out, b"PK\x05\x06")
    if actual_eocd:
        eocd_at = actual_eocd[-1]
        try:
            with out.open("rb") as stream:
                stream.seek(eocd_at)
                eocd = struct.unpack("<4s4H2LH", stream.read(22))
            disk, central_disk, disk_entries, total_entries = eocd[1:5]
            central_size, central_offset, comment_len = eocd[5:8]
            archive_size = out.stat().st_size
            if eocd_at + 22 + comment_len != archive_size:
                failures.append(
                    f"trailing bytes/EOCD extent mismatch: EOCD ends at "
                    f"{eocd_at + 22 + comment_len}, archive ends at {archive_size}"
                )
            prefix_len = eocd_at - central_size - central_offset
            if prefix_len:
                failures.append(f"prepended bytes/central-directory offset: {prefix_len}")
            if disk or central_disk or disk_entries != total_entries or total_entries != len(infos):
                failures.append("EOCD disk or entry counts are not canonical")
            central_start = eocd_at - central_size
            cursor = central_start
            with out.open("rb") as stream:
                for _ in range(total_entries):
                    stream.seek(cursor)
                    fields = struct.unpack("<4s6H3L5H2L", stream.read(46))
                    if fields[0] != b"PK\x01\x02":
                        raise ValueError("central-directory signature missing")
                    cursor += 46 + fields[10] + fields[11] + fields[12]
            if cursor != eocd_at:
                failures.append(
                    f"central-directory extent differs from EOCD: {cursor} != {eocd_at}"
                )
        except (OSError, struct.error, ValueError) as exc:
            failures.append(f"malformed EOCD/central directory: {exc}")
    else:
        failures.append("EOCD record is missing")
    return failures


def _compare_complete_bytes(actual: Path, expected: Path) -> str | None:
    """Compare complete artifacts with bounded memory and return a diagnostic."""
    actual_hash = hashlib.sha256()
    expected_hash = hashlib.sha256()
    first_difference: int | None = None
    offset = 0
    with actual.open("rb") as left, expected.open("rb") as right:
        while True:
            left_chunk = left.read(_SCAN_CHUNK_BYTES)
            right_chunk = right.read(_SCAN_CHUNK_BYTES)
            actual_hash.update(left_chunk)
            expected_hash.update(right_chunk)
            if first_difference is None and left_chunk != right_chunk:
                common = min(len(left_chunk), len(right_chunk))
                relative = next(
                    (index for index in range(common) if left_chunk[index] != right_chunk[index]),
                    common,
                )
                first_difference = offset + relative
            if not left_chunk and not right_chunk:
                break
            offset += min(len(left_chunk), len(right_chunk))
    if first_difference is None:
        return None
    return (
        f"complete archive bytes differ at offset {first_difference}; "
        f"sizes actual={actual.stat().st_size}, expected={expected.stat().st_size}; "
        f"sha256 {actual_hash.hexdigest()} != {expected_hash.hexdigest()}"
    )


def _verify_zip_representation(
    out: Path, canonical: Path, inventory: list[ReleaseEntry],
) -> None:
    """Account for every byte in the admitted non-ZIP64, descriptor-free profile.

    Compare raw headers, not ZipInfo's normalized view. Only compressed sizes
    and the offsets/sizes derived from them may differ from canonical headers.
    Walk contiguous local records and central records to the final, comment-free
    EOCD; signatures inside admitted payloads have no structural significance.
    """
    def reject(reason: str) -> None:
        raise SecretScrubError(f"post-build release parity failed: {reason}")

    with out.open("rb") as actual, canonical.open("rb") as expected:
        actual.seek(0, os.SEEK_END)
        end = actual.tell() - 22
        if end < 0:
            reject("missing final EOCD")
        actual.seek(end)
        eocd = struct.unpack("<4s4H2LH", actual.read(22))
        expected.seek(-22, os.SEEK_END)
        expected_eocd = struct.unpack("<4s4H2LH", expected.read(22))
        if eocd[:5] != expected_eocd[:5] or eocd[7] != 0:
            reject("noncanonical final EOCD (trailing bytes, disks, counts or comment)")
        central_size, central_at = eocd[5:7]
        if central_at + central_size != end:
            reject("central directory/EOCD coverage mismatch")
        cursor = 0
        central_cursor = central_at
        expected_cursor = expected_eocd[6]
        for entry in inventory:
            actual.seek(central_cursor)
            expected.seek(expected_cursor)
            fields = list(struct.unpack("<4s6H3L5H2L", actual.read(46)))
            reference = list(struct.unpack("<4s6H3L5H2L", expected.read(46)))
            compressed_size, local_at = fields[8], fields[16]
            # ZIP64 sentinels/extras and every other metadata field are denied.
            if compressed_size == 0xffffffff or local_at != cursor:
                reject("local record coverage mismatch (gap, overlap or duplicate)")
            fields[8], fields[16] = reference[8], reference[16]
            if fields != reference or reference[11] or reference[12]:
                reject("noncanonical raw central header")
            name = expected.read(reference[10])
            if actual.read(fields[10]) != name:
                reject("noncanonical raw central name")
            central_cursor += 46 + len(name)
            expected_cursor += 46 + len(name)
            if central_cursor > end:
                reject("central record exceeds directory bounds")

            actual.seek(cursor)
            expected.seek(reference[16])
            local = list(struct.unpack("<4s5H3L2H", actual.read(30)))
            reference_local = list(struct.unpack("<4s5H3L2H", expected.read(30)))
            if local[7] != compressed_size:
                reject("local/central compressed sizes disagree")
            local[7] = reference_local[7]
            if local != reference_local or reference_local[10]:
                reject("noncanonical raw local header")
            if actual.read(local[9]) != name:
                reject("local/central names disagree")
            cursor += 30 + len(name) + compressed_size
            if cursor > central_at:
                reject("compressed region overlaps central directory")

            # zipfile.read() tolerates unused compressed bytes and can stop at
            # the advertised output size. Independently require exactly one
            # complete raw DEFLATE stream and exact Git-bound output instead.
            decoder = zlib.decompressobj(-15)
            remaining = compressed_size
            produced = 0
            while remaining:
                chunk = actual.read(min(remaining, _SCAN_CHUNK_BYTES))
                if not chunk:
                    reject("truncated compressed region")
                remaining -= len(chunk)
                payload = decoder.decompress(chunk, len(entry.data) - produced + 1)
                if decoder.unused_data or decoder.unconsumed_tail:
                    reject("unconsumed or trailing compressed data")
                if payload != entry.data[produced:produced + len(payload)]:
                    reject("DEFLATE payload differs from selected Git blob")
                produced += len(payload)
            if not decoder.eof or produced != len(entry.data):
                reject("incomplete DEFLATE stream or Git blob size mismatch")
        if cursor != central_at or central_cursor != end:
            reject("unaccounted bytes between local records, directory and EOCD")


def verify_zip(
    out: Path | str,
    *,
    source: Path | str | None = None,
    revision: str = "HEAD",
    scan_contents: bool = True,
    fixed_time: tuple[int, int, int, int, int, int] = _FIXED_TIMESTAMP,
    verification: str = "reproducible",
) -> list[str]:
    """Verify Git-bound ZIP parity; integrity permits only DEFLATE re-encoding.

    Reproducible (the default and mandatory build mode) also requires complete
    canonical byte equality. Integrity always includes the secret-content scan.
    """
    if verification not in ("reproducible", "integrity"):
        raise SecretScrubError("unsupported archive verification mode")
    if verification == "integrity" and not scan_contents:
        raise SecretScrubError("integrity verification requires content scanning")
    source_path = Path(source).resolve() if source is not None else Path(__file__).resolve().parent.parent
    commit, inventory = release_inventory(source_path, revision)
    prefix = source_path.name
    expected = {_archive_name(prefix, entry.path): entry for entry in inventory}
    out = Path(out)
    failures: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="okf-archive-verify-") as temp_dir:
            expected_out = Path(temp_dir) / f"{commit}.zip"
            _write_canonical_zip(expected_out, inventory, prefix, fixed_time)
            with zipfile.ZipFile(expected_out, "r") as canonical:
                expected_infos = canonical.infolist()
            if verification == "reproducible":
                actual_eocd = _signature_offsets(out, b"PK\x05\x06")
                expected_eocd = _signature_offsets(expected_out, b"PK\x05\x06")
                if len(actual_eocd) != len(expected_eocd):
                    failures.append(
                        "EOCD signature count differs from canonical: "
                        f"{len(actual_eocd)} != {len(expected_eocd)}"
                    )
            with zipfile.ZipFile(out, "r") as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
                unsafe = sorted(name for name in names if not _safe_path(name) or is_path_denied(name))
                missing = sorted(set(expected) - set(names))
                extra = sorted(set(names) - set(expected))
                if archive.comment:
                    failures.append("archive comment is not canonical")
                if duplicates:
                    failures.append(f"duplicate entries: {duplicates}")
                if unsafe:
                    failures.append(f"unsafe/forbidden entries: {unsafe}")
                if missing:
                    failures.append(f"missing entries: {missing}")
                if extra:
                    failures.append(f"extra entries: {extra}")
                if names != sorted(expected):
                    failures.append("entry order differs from canonical inventory")
                failures.extend(_zip_layout_failures(out, infos, expected_infos))
                for info in (() if duplicates else infos):
                    entry = expected.get(info.filename)
                    if entry is None:
                        continue
                    archive_mode = (info.external_attr >> 16) & 0o177777
                    if archive_mode != entry.mode:
                        failures.append(
                            f"mode mismatch for {info.filename}: {archive_mode:o} != {entry.mode:o}"
                        )
                    if (
                        info.create_system != 3
                        or info.date_time != fixed_time
                        or info.compress_type != zipfile.ZIP_DEFLATED
                    ):
                        failures.append(f"metadata mismatch for {info.filename}")
                    if info.file_size != len(entry.data):
                        failures.append(f"uncompressed size mismatch for {info.filename}")
                    elif not info.flag_bits & 0x9 and archive.read(info) != entry.data:
                        failures.append(f"byte mismatch for {info.filename}")
            try:
                _verify_zip_representation(out, expected_out, inventory)
            except SecretScrubError as exc:
                failures.append(str(exc))
            if verification == "reproducible":
                complete_difference = _compare_complete_bytes(out, expected_out)
                if complete_difference:
                    failures.append(complete_difference)
    except (OSError, zipfile.BadZipFile, RuntimeError, struct.error, zlib.error) as exc:
        failures.append(f"cannot parse archive: {exc}")
        if scan_contents:
            try:
                if _scan_raw_archive(out):
                    failures.append(
                        "post-build content scan failed; private-key material present:\n  "
                        "<raw archive bytes>"
                    )
            except SecretScrubError as scan_exc:
                failures.append(str(scan_exc))
            failures.append(
                "decompressed payload scan incomplete: archive parser rejected artifact"
            )
        raise SecretScrubError(
            "post-build release parity failed:\n  " + "\n  ".join(failures)
        ) from exc
    if scan_contents:
        offenders = ["<raw archive bytes>"] if _scan_raw_archive(out) else []
        try:
            offenders.extend(_scan_archive_payloads(out))
        except (OSError, zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
            failures.append(f"decompressed payload scan incomplete: {exc}")
        if offenders:
            failures.append(
                "post-build content scan failed; private-key material present:\n  "
                + "\n  ".join(offenders)
            )
    if failures:
        raise SecretScrubError("post-build release parity failed:\n  " + "\n  ".join(failures))
    return names


def _build_arg_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Build or verify a commit-authoritative okf-loom skill archive. Builds always verify reproducibility."
    )
    parser.add_argument("--source", type=Path, default=repo_root)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--out", type=Path, default=repo_root.parent / "okf-loom.zip")
    operation.add_argument("--verify", type=Path, metavar="ZIP", help="verify an existing archive without publishing or modifying it")
    parser.add_argument(
        "--verification", choices=("reproducible", "integrity"),
        help="requires --verify; default reproducible; integrity permits only valid DEFLATE re-encoding, not metadata/content changes",
    )
    parser.add_argument("--revision", default="HEAD", help="commit-ish to capture (default: HEAD)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.verification is not None and args.verify is None:
        parser.error("--verification requires --verify; builds always verify reproducibility")
    source = args.source.resolve()
    out = (args.verify if args.verify is not None else args.out).resolve()
    verification = args.verification or "reproducible"
    try:
        commit = capture_commit(source, args.revision)
        if args.verify is not None:
            manifest = verify_zip(out, source=source, revision=commit, verification=verification)
        else:
            manifest = build_skill_archive(source, out, revision=commit)
    except SecretScrubError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("# okf-loom skill archive")
    print(f"#   source:   {source}")
    print(f"#   revision: {commit}")
    print(f"#   out:      {out}")
    print(f"#   files:    {len(manifest)}")
    for name in manifest:
        print(f"  {name}")
    action = f"verified ({verification})" if args.verify is not None else "wrote"
    print(f"\n# {action} {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
