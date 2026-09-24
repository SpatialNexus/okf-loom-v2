"""Shared provenance inventory and manifest validation for capture and tests.

Both producer (capture_annotations_v2.py) and verifier
(test_annotations_capture_manifest.py) import from here so inventory roots,
exclusions, hash construction, and manifest schema are guaranteed identical.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from types import MappingProxyType
from pathlib import Path
from collections.abc import Mapping
from typing import Any, NamedTuple

# Roots whose contents are consumed by the capture workflow.
INVENTORY_ROOTS: list[str] = [
    "scripts",
    "tests",
    "samples/demo_bundle",
]

_EXCLUDED_SEGMENTS: frozenset[str] = frozenset({
    "__pycache__", ".okf-loom", ".git", ".aic", "node_modules",
    ".pytest_cache", "sessions",
})

# A capture is produced from one committed source tree into the git-ignored
# evidence directory; the published tree never carries the screenshots.  To
# verify a capture later, a separately prepared evidence checkout records it
# as one commit containing only the evidence directory (added explicitly,
# because the directory is ignored).  The manifest is written during the
# first phase, so its evidence commit cannot be self-referential.  The
# validator proves the boundary instead: HEAD must be the single evidence
# commit whose parent is ``source_revision``.
PROVENANCE_MODEL = "source-vs-evidence-v1"
EVIDENCE_DIRECTORY = "captures/annotations-v2"
EMPTY_WORKTREE_DIGEST = hashlib.sha256(b"").hexdigest()


def _is_excluded(p: Path, root: Path) -> bool:
    parts = p.relative_to(root).parts
    if any(seg in _EXCLUDED_SEGMENTS for seg in parts):
        return True
    if p.name == "Bn":
        return True
    # Runtime mutation locks contain no capture source bytes and exist only
    # transiently while a bundle is being edited.  Binding them would make an
    # otherwise identical capture depend on lock timing (and previously put
    # zero-byte .okf-lock files in release provenance).
    if p.name.endswith(".okf-lock"):
        return True
    return False


class GitProvenanceError(RuntimeError):
    """Raised when a Git provenance operation required for release validation fails.

    Release validation must fail closed on Git errors; this exception propagates
    from inventory computation so that a Git failure can never silently produce
    a valid-looking but unverified inventory.
    """


# Supported tracked-file Git modes (from `git ls-files --stage`).
# 100644 = normal blob, 100755 = executable blob, 120000 = symlink (blob is
# the link target).  Any other mode (e.g. 160000 gitlink/submodule) is rejected
# by the strict stage parser because the inventory cannot bind it to on-disk
# bytes.
_SUPPORTED_GIT_MODES: frozenset[str] = frozenset({"100644", "100755", "120000"})
_HEXDIGITS: frozenset[str] = frozenset("0123456789abcdef")
# Inventory status classification for consumed files.
_STATUS_TRACKED = "tracked"
_STATUS_UNTRACKED = "untracked"
_STATUS_IGNORED = "ignored"


def _canonical_inventory_bytes(entries: list[dict[str, Any]]) -> bytes:
    """Canonical deterministic byte serialization of inventory entries.

    Every entry field — path, type, tracked, status, mode, object_sha,
    sha256, and symlink_target — is bound into the digest so that tampering
    with any single field (including mode, Git-tracked status, the index
    stage-0 object ID, or a symlink's target) is detected by both the
    aggregate digest and the detailed-entry comparison.  Entries must
    already be sorted by path.

    A regular file's ``symlink_target`` is ``None`` and serializes as a
    fixed ``\\x00NULL`` sentinel so a symlink whose target is the empty
    string cannot collide with a regular file's absent target.  An
    untracked file's ``object_sha`` is ``None`` and serializes with the
    same sentinel so a tracked file whose index object ID is the empty
    string cannot collide with an untracked file's absent object ID.
    """
    parts: list[bytes] = []
    for e in entries:
        target = e.get("symlink_target")
        target_bytes = target.encode("utf-8") if target is not None else b"\x00NULL"
        object_sha = e.get("object_sha")
        object_sha_bytes = (
            object_sha.encode("ascii") if object_sha is not None else b"\x00NULL"
        )
        parts.append(
            e["path"].encode("utf-8") + b"\0"
            + e["type"].encode("utf-8") + b"\0"
            + str(e["tracked"]).encode("ascii") + b"\0"
            + e["status"].encode("ascii") + b"\0"
            + e["mode"].encode("ascii") + b"\0"
            + object_sha_bytes + b"\0"
            + e["sha256"].encode("ascii") + b"\0"
            + target_bytes + b"\n"
        )
    return b"".join(parts)


def _canonical_inventory_json_bytes(entries: list[dict[str, Any]]) -> bytes:
    """Return the canonical JSON serialization of an inventory.

    The newline-delimited byte serializer above is the historical aggregate
    digest authority.  This second digest binds the JSON-native inventory
    crossing into the manifest, so a producer and verifier cannot silently
    disagree about the serialized consumed-input list.
    """
    return json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _inventory_json_digest(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical_inventory_json_bytes(entries)).hexdigest()


def inventory_serialized_digest(entries: list[dict[str, Any]]) -> str:
    """Return the SHA-256 of the canonical JSON inventory serialization."""
    return _inventory_json_digest(entries)


def compute_inventory_entries(root: Path) -> list[dict[str, Any]]:
    """Return a sorted list of per-file inventory records.

    Each record: ``{path, type, tracked, status, mode, object_sha, sha256,
    symlink_target}``.

    - ``type``: ``"regular"`` or ``"symlink"`` (from ``lstat()``, never
      following symlinks, so the on-disk entry itself is classified).
    - ``tracked``: ``True`` if the file is Git-tracked (present in the index).
    - ``status``: one of ``"tracked"``, ``"untracked"``, or ``"ignored"``.
      Ignored files are detected via ``git check-ignore`` and recorded with
      exact semantics (they are real on-disk consumed inputs, distinguished
      from plain untracked files).  Bound into the digest.
    - ``mode``: a *Git* mode string (``"100644"``, ``"100755"``, or
      ``"120000"``), never raw filesystem permission bits.

      * Tracked entries derive ``mode`` authoritatively from a fail-closed,
        strict ``git ls-files --stage -z`` query of the index.
      * Untracked/ignored entries use explicit, stable semantics: ``120000``
        for a symlink; for a regular file ``100755`` if any executable bit
        (``st_mode & 0o111``) is set, otherwise ``100644``.

    - ``object_sha``: for tracked entries, the Git index stage-0 object ID
      (40-char lowercase hex) from ``git ls-files --stage``, bound into the
      digest so that an index-only mutation (``git update-index
      --cacheinfo`` changing the staged blob without touching working-tree
      bytes) is detected even when the file's ``sha256`` is unchanged.
      ``None`` for untracked/ignored entries (not in the Git index).
    - ``sha256``: hex digest of file bytes (regular) or the link-target
      string (symlink), so the digest binds the *actual current bytes* on
      disk.
    - ``symlink_target``: the link-target string for a symlink, or ``None``
      for a regular file (bound into the digest).

    Enumeration is two-pass so ignored classification needs a single
    ``git check-ignore`` call over all untracked candidates (not per-file).
    Pass 1 enumerates + type-validates every non-excluded object (raising
    ``ValueError`` on FIFO/socket/device — never silently omitted).  Pass 2
    classifies status and builds entries.  All Git operations are
    fail-closed (``GitProvenanceError``).
    """
    inventory_roots = [root / r for r in INVENTORY_ROOTS]
    index = _git_index_stage(root)
    tracked_set = frozenset(index.keys())

    # Pass 1: enumerate candidate files without ever resolving an inventory
    # component through a pathname.  Each directory is opened relative to its
    # pinned parent with O_NOFOLLOW and its identity is checked before and
    # after enumeration.  The identity chain is retained for pass two.
    candidates: list[tuple[str, os.stat_result, tuple[tuple[int, int, int], ...]]] = []
    seen: set[str] = set()
    root_fd = os.open(str(root), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                      | getattr(os, "O_NOFOLLOW", 0))

    def ident(st: os.stat_result) -> tuple[int, int, int]:
        return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))

    def walk(parent_fd: int, parts: tuple[str, ...],
             chain: tuple[tuple[int, int, int], ...]) -> None:
        name = parts[-1]
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISDIR(opened.st_mode):
                raise ValueError(f"inventory component is not a directory: {'/'.join(parts)}")
            current_chain = chain + (ident(opened),)
            with os.scandir(fd) as scan:
                rows = sorted(list(scan), key=lambda row: row.name)
            for row in rows:
                rel = "/".join(parts + (row.name,))
                p = root / rel
                if _is_excluded(p, root):
                    continue
                st = os.stat(row.name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    walk(fd, parts + (row.name,), current_chain)
                    continue
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    raise ValueError(
                        f"unsupported special file in inventory root: {rel} "
                        f"(st_mode={oct(st.st_mode)}); refusing to omit unrecorded object")
                if rel not in seen:
                    seen.add(rel)
                    candidates.append((rel, st, current_chain))
            if ident(os.fstat(fd)) != ident(opened):
                raise ValueError(f"inventory directory identity changed while enumerating: {'/'.join(parts)}")
        finally:
            os.close(fd)

    try:
        for inventory_root in INVENTORY_ROOTS:
            components = tuple(Path(inventory_root).parts)
            parent_fd = root_fd
            opened_fds: list[int] = []
            chain: tuple[tuple[int, int, int], ...] = ()
            try:
                try:
                    for component in components[:-1]:
                        fd = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                     | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        opened_fds.append(fd)
                        chain += (ident(os.fstat(fd)),)
                        parent_fd = fd
                    walk(parent_fd, components, chain)
                except FileNotFoundError:
                    # Inventory roots are optional in minimal test/consumer
                    # checkouts, matching the previous rglob semantics.
                    pass
            finally:
                for fd in reversed(opened_fds):
                    os.close(fd)
    finally:
        os.close(root_fd)

    # Classify ignored files (single check-ignore over untracked candidates).
    untracked_candidates = [rel for rel, _, _ in candidates if rel not in tracked_set]
    ignored_set = _git_ignored_set(root, untracked_candidates)

    # Open beneath the already-selected repository directory.  Every regular
    # file is opened descriptor-relative with O_NOFOLLOW and matched to the
    # lstat identity from pass one; this prevents a pathname symlink swap from
    # redirecting provenance hashing outside the inventory root.
    root_fd = os.open(str(root), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                      | getattr(os, "O_NOFOLLOW", 0))
    entries: list[dict[str, Any]] = []
    try:
        for rel, st, expected_chain in candidates:
            git_info = index.get(rel)
            tracked = rel in tracked_set
            status = (_STATUS_TRACKED if tracked else
                      _STATUS_IGNORED if rel in ignored_set else _STATUS_UNTRACKED)
            object_sha = git_info["object_sha"] if git_info else None
            parts = tuple(Path(rel).parts)
            parent_fd = root_fd
            opened_dirs: list[int] = []
            try:
                actual_chain: list[tuple[int, int, int]] = []
                for component in parts[:-1]:
                    dfd = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                  | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                    opened_dirs.append(dfd)
                    dst = os.fstat(dfd)
                    actual_chain.append(ident(dst))
                    parent_fd = dfd
                if tuple(actual_chain) != expected_chain:
                    raise ValueError(f"inventory parent identity changed before read: {rel}")
                before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                if ident(before) != ident(st):
                    raise ValueError(f"inventory entry identity changed before read: {rel}")
                if stat.S_ISLNK(st.st_mode):
                    if not hasattr(os, "O_PATH"):
                        raise ValueError(
                            f"inventory symlink target cannot be coherently pinned: {rel}")
                    link_fd = os.open(parts[-1], os.O_PATH | getattr(os, "O_NOFOLLOW", 0),
                                      dir_fd=parent_fd)
                    try:
                        opened = os.fstat(link_fd)
                        if not stat.S_ISLNK(opened.st_mode) or ident(opened) != ident(st):
                            raise ValueError(f"inventory symlink identity changed: {rel}")
                        try:
                            target = os.readlink("", dir_fd=link_fd)
                        except OSError as exc:
                            raise ValueError(
                                f"inventory symlink target cannot be coherently pinned: {rel}") from exc
                        if ident(os.fstat(link_fd)) != ident(opened):
                            raise ValueError(f"inventory symlink changed while read: {rel}")
                    finally:
                        os.close(link_fd)
                    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
                    ftype = "symlink"
                    git_mode = git_info["mode"] if git_info else "120000"
                    symlink_target: str | None = target
                else:
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    try:
                        fd = os.open(parts[-1], flags, dir_fd=parent_fd)
                    except OSError as exc:
                        raise ValueError(
                            f"inventory file changed before secure open: {rel}: {exc}") from exc
                    try:
                        opened = os.fstat(fd)
                        identity = (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))
                        opened_identity = (opened.st_dev, opened.st_ino,
                                           stat.S_IFMT(opened.st_mode))
                        if not stat.S_ISREG(opened.st_mode) or opened_identity != identity:
                            raise ValueError(
                                f"inventory file identity/type changed before read: {rel}")
                        hasher = hashlib.sha256()
                        while chunk := os.read(fd, 1024 * 1024):
                            hasher.update(chunk)
                        after = os.fstat(fd)
                        after_identity = (after.st_dev, after.st_ino,
                                          stat.S_IFMT(after.st_mode))
                        if (after_identity != opened_identity
                                or after.st_size != opened.st_size
                                or after.st_mtime_ns != opened.st_mtime_ns):
                            raise ValueError(f"inventory file changed while being read: {rel}")
                        digest = hasher.hexdigest()
                    finally:
                        os.close(fd)
                    ftype = "regular"
                    git_mode = (git_info["mode"] if git_info else
                                "100755" if st.st_mode & 0o111 else "100644")
                    symlink_target = None
            finally:
                for dfd in reversed(opened_dirs):
                    os.close(dfd)
            entries.append({
                "path": rel, "type": ftype, "tracked": tracked,
                "status": status, "mode": git_mode, "object_sha": object_sha,
                "sha256": digest, "symlink_target": symlink_target,
            })
    finally:
        os.close(root_fd)
    expected_identities = sorted(
        (rel, st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))
        for rel, st, _ in candidates)
    if _inventory_identity_snapshot(root) != expected_identities:
        raise ValueError("inventory membership or identity changed during collection")
    entries.sort(key=lambda e: e["path"])
    return entries


def _inventory_identity_snapshot(root: Path) -> list[tuple[str, int, int, int]]:
    """Repeat enumeration and bind exact membership plus leaf identity."""
    rows: list[tuple[str, int, int, int]] = []
    for inventory_root in INVENTORY_ROOTS:
        base = root / inventory_root
        if not base.exists():
            continue
        for current, dirs, files, dirfd in os.fwalk(base, follow_symlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(d for d in dirs if not _is_excluded(current_path / d, root))
            for name in sorted(files):
                path = current_path / name
                if _is_excluded(path, root):
                    continue
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    raise ValueError(f"unsupported special file in inventory root: {path.relative_to(root)}")
                rows.append((path.relative_to(root).as_posix(), st.st_dev, st.st_ino,
                             stat.S_IFMT(st.st_mode)))
    return sorted(rows)


def compute_inventory(root: Path) -> tuple[str, int]:
    """Return ``(sha256_hexdigest, file_count)`` for the deterministic
    filesystem inventory.

    The digest canonically binds every entry field: path, type, tracked,
    status, mode, sha256, and symlink_target.  This is the aggregate digest
    form used in the manifest; the validator additionally requires exact
    detailed-entry equality.
    """
    entries = compute_inventory_entries(root)
    return hashlib.sha256(_canonical_inventory_bytes(entries)).hexdigest(), len(entries)


class ProvenanceSnapshot(NamedTuple):
    """Immutable point-in-time provenance snapshot of a repo.

    All fields come from a SINGLE inventory scan plus single Git queries, so a
    snapshot is internally consistent (no double scan).  Two snapshots compare
    equal iff HEAD, worktree bytes, the capture script, and every consumed
    inventory entry are byte-identical — the contract used to prove source
    bytes did not drift between the pre-capture and post-capture snapshots.
    """
    head: str
    worktree_digest: str
    script_sha256: str
    inventory_entries: tuple[MappingProxyType, ...]
    inventory_digest: str
    inventory_count: int


def snapshot_inventory_for_manifest(snapshot: ProvenanceSnapshot) -> list[dict[str, Any]]:
    """Return a canonical JSON-native deep copy of snapshot inventory entries.

    ``ProvenanceSnapshot`` deliberately retains mapping proxies so callers
    cannot mutate the captured authority.  Manifest construction crosses a
    serialization boundary, so it must explicitly thaw that authority into a
    detached tree containing only JSON-native containers/scalars.  No string
    coercion is permitted: unsupported values fail visibly in ``json.dumps``.
    """
    def thaw(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: thaw(child) for key, child in value.items()}
        if isinstance(value, tuple):
            return [thaw(child) for child in value]
        if isinstance(value, list):
            return [thaw(child) for child in value]
        return value

    return [thaw(entry) for entry in snapshot.inventory_entries]


def capture_provenance_snapshot(root: Path, *, require_clean: bool = True) -> ProvenanceSnapshot:
    """Build an immutable provenance snapshot in a SINGLE scan.

    Returns HEAD, the binary-worktree digest, the capture-script sha256, the
    detailed inventory entries (as an immutable tuple), and the digest/count
    values derived from those SAME entries.  A production source snapshot
    requires an attached branch and a clean index/worktree.  ``require_clean``
    exists only for deterministic unit seams that need to inspect a dirty
    tree; the release capture path retains the default fail-closed behavior.

    Fail-closed: any Git or read error raises (``GitProvenanceError`` for Git
    failures) so a snapshot can never be silently incomplete.
    """
    entries = compute_inventory_entries(root)
    digest = hashlib.sha256(_canonical_inventory_bytes(entries)).hexdigest()
    _git_attached_branch(root)
    head = _git_head(root)
    status = _git_status_porcelain(root)
    if require_clean and status:
        raise GitProvenanceError(
            "source snapshot requires a clean index/worktree; "
            f"git status is not clean ({len(status)} bytes of status output)"
        )
    worktree = _git_worktree_binary(root)
    script_entry = next((entry for entry in entries
                         if entry["path"] == "scripts/capture_annotations_v2.py"), None)
    if script_entry is None or script_entry["type"] != "regular":
        raise ValueError("capture script missing from secure inventory authority")
    script_hash = script_entry["sha256"]
    return ProvenanceSnapshot(
        head=head,
        worktree_digest=hashlib.sha256(worktree).hexdigest(),
        script_sha256=script_hash,
        inventory_entries=tuple(MappingProxyType(dict(entry)) for entry in entries),
        inventory_digest=digest,
        inventory_count=len(entries),
    )


def _git_head(root: Path) -> str:
    """Return ``git rev-parse HEAD`` (fail-closed → GitProvenanceError)."""
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), text=True, timeout=5).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitProvenanceError(f"git rev-parse HEAD failed: {exc}") from exc
    if not _is_git_revision(head):
        raise GitProvenanceError(f"git rev-parse HEAD returned an invalid revision: {head!r}")
    return head


def _is_git_revision(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in _HEXDIGITS for char in value)
    )


def _git_attached_branch(root: Path) -> str:
    """Return the current branch, rejecting detached/ambiguous HEAD state."""
    try:
        branch = subprocess.check_output(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=str(root), text=True, timeout=5,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitProvenanceError(
            "git HEAD is detached or branch identity is unavailable"
        ) from exc
    if not branch or "\n" in branch or "\x00" in branch:
        raise GitProvenanceError("git HEAD has an empty or ambiguous branch identity")
    return branch


def _git_status_porcelain(root: Path) -> bytes:
    """Return NUL-framed Git status, failing closed on Git errors."""
    try:
        return subprocess.check_output(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(root), timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitProvenanceError(f"git status failed (release-fatal): {exc}") from exc


def _git_worktree_binary(root: Path) -> bytes:
    """Return ``git diff --binary HEAD`` bytes (fail-closed)."""
    try:
        return subprocess.check_output(
            ["git", "diff", "--binary", "HEAD"],
            cwd=str(root), timeout=5)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitProvenanceError(f"git diff --binary HEAD failed: {exc}") from exc


def _git_commit_parents(root: Path, revision: str) -> tuple[str, ...]:
    """Return validated parents for one commit, or raise GitProvenanceError."""
    if not _is_git_revision(revision):
        raise GitProvenanceError(f"invalid commit revision: {revision!r}")
    try:
        output = subprocess.check_output(
            ["git", "rev-list", "--parents", "-n", "1", revision],
            cwd=str(root), text=True, timeout=5,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitProvenanceError(
            f"git commit parent lookup failed for {revision}: {exc}"
        ) from exc
    parts = output.split()
    if not parts or parts[0] != revision or any(not _is_git_revision(p) for p in parts):
        raise GitProvenanceError(
            f"git commit parent output is invalid or ambiguous: {output!r}"
        )
    return tuple(parts[1:])


def _git_evidence_changes(root: Path, source_revision: str,
                           evidence_revision: str) -> list[tuple[str, str]]:
    """Return ``(status, path)`` changes between source and evidence commits."""
    if not _is_git_revision(source_revision) or not _is_git_revision(evidence_revision):
        raise GitProvenanceError("cannot diff invalid source/evidence revision")
    try:
        output = subprocess.check_output(
            ["git", "diff", "--name-status", "--no-renames", "-z",
             source_revision, evidence_revision],
            cwd=str(root), timeout=5,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitProvenanceError(
            "git evidence publication diff failed (release-fatal): "
            f"{exc}"
        ) from exc
    if not output:
        return []
    if not output.endswith(b"\0"):
        raise GitProvenanceError("git evidence diff is not NUL-terminated")
    fields = output[:-1].split(b"\0")
    if len(fields) % 2:
        raise GitProvenanceError("git evidence diff has an incomplete status/path pair")
    changes: list[tuple[str, str]] = []
    for index in range(0, len(fields), 2):
        status = fields[index].decode("ascii", "replace")
        path = fields[index + 1].decode("utf-8", "surrogateescape")
        if not status or not path or len(status) != 1:
            raise GitProvenanceError(
                f"git evidence diff contains an invalid status/path pair: {status!r}, {path!r}"
            )
        changes.append((status, path))
    return changes


def _git_index_stage(root: Path) -> dict[str, dict[str, str]]:
    """Return repo-relative POSIX path → ``{mode, object_sha, stage}`` from
    the Git index, fail-closed and STRICT.

    Uses ``git ls-files --stage -z`` (NUL-delimited) so tracked-file Git
    modes (``100644`` normal, ``100755`` executable, ``120000`` symlink) and
    symlink semantics are bound authoritatively from the index rather than
    from filesystem permission bits.

    Every record is validated.  Any uncertainty raises ``GitProvenanceError``
    rather than being silently skipped:

    * the ``\\t`` delimiter between metadata and path is required;
    * the metadata must be exactly ``<mode> <sha> <stage>`` (three fields);
    * the mode must be one of the supported tracked modes;
    * the object id must be a 40-character lowercase hex string;
    * the stage must be ``0`` (a non-zero stage indicates an unmerged conflict
      path);
    * a path may appear at most once (a duplicate/conflict path is rejected).

    Git itself errors (unavailable, non-repo, timeout) also raise
    ``GitProvenanceError`` so a Git failure can never silently produce an
    empty (all-untracked) index.
    """
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "--stage", "-z"],
            cwd=str(root), timeout=10, stderr=subprocess.PIPE,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitProvenanceError(f"git ls-files --stage failed: {exc}") from exc
    index: dict[str, dict[str, str]] = {}
    if out and not out.endswith(b"\0"):
        raise GitProvenanceError("git stage output is not NUL-terminated")
    records = out[:-1].split(b"\0") if out else []
    if any(not record for record in records):
        raise GitProvenanceError("git stage output contains an empty record")
    for record in records:
        # Format: "<mode> <sha> <stage>\t<path>"
        meta, tab, path_bytes = record.partition(b"\t")
        if not tab:
            raise GitProvenanceError(
                f"git stage record missing tab delimiter: {record!r}")
        parts = meta.split(b" ")
        if len(parts) != 3:
            raise GitProvenanceError(
                f"git stage record must have exactly mode/sha/stage: {record!r}")
        mode = parts[0].decode("ascii", "replace")
        sha = parts[1].decode("ascii", "replace")
        stage = parts[2].decode("ascii", "replace")
        path = path_bytes.decode("utf-8", "surrogateescape")
        if not path:
            raise GitProvenanceError("git stage record contains an empty path")
        if mode not in _SUPPORTED_GIT_MODES:
            raise GitProvenanceError(
                f"unsupported git mode {mode!r} for {path!r} "
                f"(need one of {sorted(_SUPPORTED_GIT_MODES)})")
        if len(sha) != 40 or any(c not in _HEXDIGITS for c in sha):
            raise GitProvenanceError(
                f"invalid git object id {sha!r} for {path!r}")
        if stage != "0":
            raise GitProvenanceError(
                f"non-stage-0 (unmerged conflict) index entry for {path!r}: "
                f"stage {stage}")
        if path in index:
            raise GitProvenanceError(
                f"duplicate/conflicting index path: {path!r}")
        index[path] = {"mode": mode, "object_sha": sha, "stage": stage}
    return index


def _git_ignored_set(root: Path, candidates: list[str]) -> frozenset[str]:
    """Return the subset of *candidates* that Git ignores, via
    ``git check-ignore``.  Fail-closed: real Git errors raise
    ``GitProvenanceError``; ``check-ignore`` exit code 1 (no matches) yields
    an empty set.

    Only untracked candidates should be passed (tracked files are never
    ignored), so the tracked/untracked/ignored classification is unambiguous.
    """
    if not candidates:
        return frozenset()
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-z", "--stdin"],
          input=("\0".join(candidates) + "\0").encode("utf-8", "surrogateescape"),
            cwd=str(root), capture_output=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitProvenanceError(f"git check-ignore failed: {exc}") from exc
    rc = result.returncode
    # check-ignore exits 0 when >=1 path is ignored, 1 when none are ignored.
    if rc == 0 or rc == 1:
        out = result.stdout
        if out and not out.endswith(b"\0"):
            raise GitProvenanceError("git check-ignore output is not NUL-terminated")
        records = out[:-1].split(b"\0") if out else []
        if any(not record for record in records):
            raise GitProvenanceError("git check-ignore output contains an empty record")
        decoded = [p.decode("utf-8", "surrogateescape") for p in records]
        if rc == 0 and not decoded:
            raise GitProvenanceError("git check-ignore rc=0 returned no paths")
        if len(decoded) != len(set(decoded)):
            raise GitProvenanceError("git check-ignore output contains a duplicate path")
        foreign = set(decoded) - set(candidates)
        if foreign:
            raise GitProvenanceError(
                f"git check-ignore returned paths not submitted: {sorted(foreign)!r}")
        if rc == 1 and decoded:
            raise GitProvenanceError("git check-ignore rc=1 returned ignored paths")
        return frozenset(decoded)
    raise GitProvenanceError(
        f"git check-ignore failed (rc={rc}): "
        f"{result.stderr.decode('utf-8', 'replace')}"
    )


def _git_tracked_set(root: Path) -> frozenset[str]:
    """Return the set of repo-relative POSIX paths that Git tracks.

    Derives from the fail-closed ``_git_index_stage`` query (single source of
    truth for tracked presence and Git mode).  Raises ``GitProvenanceError``
    on Git errors rather than returning an empty set, so a Git failure can
    never silently classify every file as untracked.
    """
    return frozenset(_git_index_stage(root).keys())


# ---------------------------------------------------------------------------
# Per-image expected viewport/media
# ---------------------------------------------------------------------------

EXPECTED_VIEWPORTS: dict[str, dict[str, int]] = {
    "resolved.png": {"width": 1280, "height": 900},
    "stale-after-mutation.png": {"width": 1280, "height": 900},
    "selecting.png": {"width": 1280, "height": 900},
    "conflict.png": {"width": 1280, "height": 900},
    "save-failure.png": {"width": 1280, "height": 900},
    "focus-visible.png": {"width": 1280, "height": 900},
    "mobile-320.png": {"width": 320, "height": 900},
    "forced-colors.png": {"width": 1280, "height": 900},
    "no-js.png": {"width": 1280, "height": 900},
}

EXPECTED_MEDIA: dict[str, dict[str, str]] = {
    "resolved.png": {"reduced_motion": "reduce", "forced_colors": "none"},
    "stale-after-mutation.png": {"reduced_motion": "reduce", "forced_colors": "none"},
    "selecting.png": {"reduced_motion": "reduce", "forced_colors": "none"},
    "conflict.png": {"reduced_motion": "reduce", "forced_colors": "none"},
    "save-failure.png": {"reduced_motion": "reduce", "forced_colors": "none"},
    "focus-visible.png": {"reduced_motion": "reduce", "forced_colors": "none"},
    "mobile-320.png": {"reduced_motion": "reduce", "forced_colors": "none"},
    "forced-colors.png": {"reduced_motion": "reduce", "forced_colors": "active"},
    "no-js.png": {"reduced_motion": "none", "forced_colors": "none"},
}


# ---------------------------------------------------------------------------
# Truthful scenario classifications
# ---------------------------------------------------------------------------

SCENARIO_CLASSIFICATIONS: dict[str, str] = {
    "resolved.png": "public-api-server-flow",
    "stale-after-mutation.png": "public-api-server-flow",
    "selecting.png": "public-api-browser-flow",
    "conflict.png": "browser-handling-of-injected-http-envelope",
    "save-failure.png": "browser-handling-of-injected-http-envelope",
    "focus-visible.png": "browser-handling-of-injected-http-envelope",
    "mobile-320.png": "browser-handling-of-injected-http-envelope",
    "forced-colors.png": "browser-handling-of-injected-http-envelope",
    "no-js.png": "public-api-server-flow",
}


# ---------------------------------------------------------------------------
# Shared manifest validator (used by both capture finalization and tests)
# ---------------------------------------------------------------------------

def expected_evidence_paths(expected_artifacts: Mapping[str, str]) -> list[str]:
    """Return the exact final evidence paths allowed by publication."""
    return sorted(
        {f"{EVIDENCE_DIRECTORY}/{name}" for name in expected_artifacts}
        | {f"{EVIDENCE_DIRECTORY}/manifest.json"}
    )


def validate_manifest(manifest: dict[str, Any], root: Path,
                      expected_artifacts: dict[str, str],
                      check_files: bool = True, *,
                      phase: str = "final") -> list[str]:
    """Validate a manifest dict against the expected schema and artifacts.

    Returns a list of error strings (empty if valid).  When *check_files*
    is True, also verifies per-file SHA-256 against the actual files on disk
    under ``root / EVIDENCE_DIRECTORY``.

    ``phase="capture"`` validates a staged manifest against the source tree;
    ``phase="final"`` (the release-authority default) additionally requires
    an attached, clean checkout whose one-parent HEAD is the evidence commit
    immediately following ``source_revision``.  The evidence commit may
    change only allowed evidence paths and must update ``manifest.json``; the
    final on-disk directory still must contain the complete exact image set.

    Git provenance failures are release-fatal: if Git is unavailable or
    errors during revision, status, publication-diff, or tracked-file
    enumeration, the validator appends an error rather than silently passing.

    This function uses explicit ``if`` checks, never ``assert``, so it
    is safe under ``python -O``.
    """
    if phase not in {"capture", "final"}:
        return [f"unknown manifest validation phase: {phase!r}"]
    if not isinstance(manifest, dict):
        return ["manifest is not a JSON object"]
    errors: list[str] = []
    output = root / EVIDENCE_DIRECTORY

    # Schema.
    if manifest.get("schema") != 1:
        errors.append(f"schema mismatch: {manifest.get('schema')}")
    if manifest.get("provenance_model") != PROVENANCE_MODEL:
        errors.append(
            f"provenance model mismatch: {manifest.get('provenance_model')!r}"
        )
    if not str(manifest.get("captured_at", "")).endswith("+00:00"):
        errors.append(f"timestamp not UTC ISO: {manifest.get('captured_at')}")
    if not manifest.get("browser"):
        errors.append("missing browser version")

    expected_paths = expected_evidence_paths(expected_artifacts)
    if manifest.get("evidence_paths") != expected_paths:
        errors.append(
            "evidence publication path set mismatch: "
            f"{manifest.get('evidence_paths')} != {expected_paths}"
        )
    if not manifest.get("evidence_parent_verified"):
        errors.append("evidence_parent_verified flag missing or false")

    source_revision = manifest.get("source_revision")
    evidence_parent = manifest.get("evidence_parent_revision")
    if not _is_git_revision(source_revision):
        errors.append(f"source_revision is not a full Git revision: {source_revision!r}")
    if not _is_git_revision(evidence_parent):
        errors.append(
            f"evidence_parent_revision is not a full Git revision: {evidence_parent!r}"
        )
    if source_revision != evidence_parent:
        errors.append(
            "source/evidence parent revision mismatch: "
            f"{source_revision!r} != {evidence_parent!r}"
        )
    if not manifest.get("source_revision_verified"):
        errors.append("source_revision_verified flag missing or false")
    head: str | None = None
    try:
        head = _git_head(root)
    except GitProvenanceError as exc:
        errors.append(str(exc))

    # Inventory — aggregate digest, count, and exact detailed-entry equality.
    # Git failures during tracked-file enumeration are release-fatal.
    try:
        expected_entries = compute_inventory_entries(root)
    except (GitProvenanceError, ValueError) as exc:
        errors.append(f"inventory computation failed (release-fatal): {exc}")
        expected_entries = []
    expected_inv = hashlib.sha256(_canonical_inventory_bytes(expected_entries)).hexdigest()
    script_entries = [entry for entry in expected_entries
                      if entry["path"] == "scripts/capture_annotations_v2.py"]
    if len(script_entries) != 1 or script_entries[0].get("type") != "regular":
        errors.append("inventory must contain exactly one regular authoritative capture script")
    elif manifest.get("capture_script_sha256") != script_entries[0]["sha256"]:
        errors.append("capture script hash mismatch")
    if manifest.get("capture_input_inventory_sha256") != expected_inv:
        errors.append("capture input inventory hash mismatch")
    expected_serialized_inv = _inventory_json_digest(expected_entries)
    if manifest.get("capture_input_inventory_serialized_sha256") != expected_serialized_inv:
        errors.append("serialized capture input inventory hash mismatch")
    if manifest.get("capture_input_file_count") != len(expected_entries):
        errors.append(f"inventory file count mismatch: {manifest.get('capture_input_file_count')} != {len(expected_entries)}")
    # Exact detailed-inventory equality: recorded entries must match the freshly
    # computed entries field-for-field (path, type, tracked, status, mode,
    # sha256, and symlink_target).  This binds Git mode, tracked status, and
    # symlink target individually — not just via the aggregate digest — so a
    # tamper of any single field is rejected even if the digest were forged.
    recorded_entries = manifest.get("capture_input_inventory")
    if not isinstance(recorded_entries, list):
        errors.append("capture_input_inventory missing or not a list")
    elif recorded_entries != expected_entries:
        errors.append(
            "capture_input_inventory detailed entries mismatch: recorded "
            "entries do not exactly match freshly computed inventory"
        )

    # Source/evidence worktree digests — fail closed on Git errors.  The
    # capture phase compares both fields with the live source-side snapshot;
    # final validation requires a clean source capture and compares the
    # evidence-side field with the final checkout.
    try:
        worktree = _git_worktree_binary(root)
        live_worktree_digest = hashlib.sha256(worktree).hexdigest()
        if phase == "capture":
            if manifest.get("source_worktree_digest") != live_worktree_digest:
                errors.append("source worktree digest mismatch")
            if manifest.get("evidence_worktree_digest") != live_worktree_digest:
                errors.append("capture/evidence worktree digest mismatch")
        else:
            if manifest.get("source_worktree_digest") != EMPTY_WORKTREE_DIGEST:
                errors.append(
                    "source worktree was not clean at capture: "
                    f"{manifest.get('source_worktree_digest')!r} != {EMPTY_WORKTREE_DIGEST}"
                )
            if manifest.get("evidence_worktree_digest") != live_worktree_digest:
                errors.append("evidence worktree digest mismatch")
    except GitProvenanceError as exc:
        errors.append(str(exc))

    if phase == "capture":
        if head is not None and source_revision != head:
            errors.append(f"source_revision mismatch: {source_revision} != {head}")
    else:
        # The final authority must be attached and clean.  ``git diff`` alone
        # omits untracked files, so status is an independent stale/tamper
        # barrier rather than a redundant convenience check.
        try:
            _git_attached_branch(root)
        except GitProvenanceError as exc:
            errors.append(str(exc))
        try:
            if _git_status_porcelain(root):
                errors.append(
                    "final evidence tree is not clean (source or artifact "
                    "mutation detected)"
                )
        except GitProvenanceError as exc:
            errors.append(str(exc))

        if head is not None and _is_git_revision(source_revision):
            try:
                parents = _git_commit_parents(root, head)
                if len(parents) != 1:
                    errors.append(
                        "evidence HEAD must have exactly one parent; "
                        f"got {len(parents)} (detached/merge provenance is ambiguous)"
                    )
                elif parents[0] != source_revision:
                    errors.append(
                        "evidence commit parent does not equal source_revision: "
                        f"{parents[0]} != {source_revision}"
                    )
                if len(parents) == 1 and parents[0] == source_revision:
                    changes = _git_evidence_changes(root, source_revision, head)
                    actual_paths = [path for _, path in changes]
                    if len(actual_paths) != len(set(actual_paths)):
                        errors.append("evidence publication diff contains duplicate paths")
                    actual_path_set = set(actual_paths)
                    expected_path_set = set(expected_paths)
                    if not actual_path_set.issubset(expected_path_set):
                        errors.append(
                            "evidence publication commit changed paths outside the "
                            "allowed artifact set: "
                            f"actual={sorted(actual_path_set)} expected={expected_paths}"
                        )
                    if f"{EVIDENCE_DIRECTORY}/manifest.json" not in actual_path_set:
                        errors.append(
                            "evidence publication commit did not update manifest.json"
                        )
                    if any(status not in {"A", "M"} for status, _ in changes):
                        errors.append(
                            "evidence publication commit contains non-add/modify "
                            f"status: {changes}"
                        )
            except GitProvenanceError as exc:
                errors.append(str(exc))

    # Artifact set.
    images_value = manifest.get("images", [])
    if not isinstance(images_value, list):
        errors.append("images missing or not a list")
        images: list[Any] = []
    else:
        images = images_value
    file_names: list[Any] = []
    for img in images:
        if not isinstance(img, dict):
            errors.append(f"image row is not an object: {img!r}")
            file_names.append("")
        else:
            file_names.append(img.get("file", ""))
    try:
        file_name_set = set(file_names)
    except TypeError:
        file_name_set = set()
        errors.append(f"image file names contain an unhashable value: {file_names!r}")
    if file_name_set != set(expected_artifacts):
        missing = set(expected_artifacts) - file_name_set
        extra = file_name_set - set(expected_artifacts)
        errors.append(f"artifact set mismatch: missing={missing} extra={extra}")
    if len(file_names) != len(file_name_set):
        errors.append(f"duplicate image rows: {file_names}")

    output_fd: int | None = None
    if check_files:
        try:
            output_fd = os.open(str(output), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            errors.append(f"output directory secure open failed: {exc}")

    # Per-image checks.
    for img in images:
        if not isinstance(img, dict):
            continue
        fname = img.get("file", "")
        if not isinstance(fname, str) or fname not in expected_artifacts:
            continue
        # Scenario assertions identity.
        expected_scenario = expected_artifacts[fname]
        assertions = img.get("scenario_assertions", [])
        if not isinstance(assertions, list) or any(not isinstance(a, str) for a in assertions):
            errors.append(f"{fname} scenario_assertions is not a string list")
            actual = ""
        else:
            actual = "; ".join(assertions)
        if actual != expected_scenario:
            errors.append(f"scenario mismatch for {fname}: expected={expected_scenario!r} actual={actual!r}")
        if img.get("release_proof") is not True:
            errors.append(f"{fname} release_proof is not True")
        if not img.get("scenario_assertions"):
            errors.append(f"{fname} has no scenario_assertions")
        # Per-image classification — exact enforcement against the truthful
        # classification table.  Missing, unknown, or mismatched values
        # are rejected so an injected-envelope image cannot be relabelled
        # as production-server proof.
        expected_cls = SCENARIO_CLASSIFICATIONS.get(fname)
        if expected_cls is not None:
            actual_cls = img.get("classification")
            if actual_cls != expected_cls:
                errors.append(
                    f"{fname} classification mismatch: expected={expected_cls!r} "
                    f"actual={actual_cls!r}"
                )
        elif "classification" not in img:
            errors.append(f"{fname} missing classification")
        # Per-image viewport.
        expected_vp = EXPECTED_VIEWPORTS.get(fname)
        if expected_vp:
            actual_vp = img.get("viewport", {})
            if actual_vp != expected_vp:
                errors.append(f"{fname} viewport mismatch: {actual_vp} != {expected_vp}")
        # Per-image media.
        expected_md = EXPECTED_MEDIA.get(fname)
        if expected_md:
            actual_md = img.get("media", {})
            if actual_md != expected_md:
                errors.append(f"{fname} media mismatch: {actual_md} != {expected_md}")
        # On-disk artifact checks (lstat-based; reject symlinks/specials).
        if check_files and output_fd is not None:
            try:
                actual_hash, _ = _hash_regular_at(output_fd, fname)
                if actual_hash != img.get("sha256"):
                    errors.append(f"{fname} hash mismatch on disk")
            except (OSError, ValueError) as exc:
                if isinstance(exc, OSError) and exc.errno == getattr(__import__('errno'), 'ELOOP'):
                    errors.append(f"{fname} is a symlink, not a regular file")
                else:
                    errors.append(f"{fname} secure read failed: {exc}")

    if check_files:
        # Exact output-directory set: the directory must contain exactly the
        # expected artifacts + manifest.json — no extra files, directories,
        # or symlinks.  (Staging/published exact-set contract.)
        expected_names = set(expected_artifacts) | {"manifest.json"}
        actual_names = ({row.name for row in os.scandir(output_fd)}
                        if output_fd is not None else set())
        extras = actual_names - expected_names
        missing = expected_names - actual_names
        if extras:
            errors.append(
                f"output directory has extra entries (files/dirs/symlinks): "
                f"{sorted(extras)}")
        if missing:
            errors.append(f"output directory missing entries: {sorted(missing)}")
        # manifest.json must be a regular file too (not a symlink/special).
        if output_fd is not None:
            try:
                _, manifest_bytes = _hash_regular_at(output_fd, "manifest.json")
                import json
                if json.loads(manifest_bytes) != manifest:
                    errors.append("manifest.json bytes do not match validated manifest")
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                errors.append(f"manifest.json secure read failed: {exc}")

    if output_fd is not None:
        os.close(output_fd)

    return errors


def _hash_regular_at(dir_fd: int, name: str) -> tuple[str, bytes]:
    """Open one leaf no-follow beneath a pinned directory and hash that fd."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("invalid output leaf name")
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not a regular file")
        chunks: list[bytes] = []
        hasher = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
            hasher.update(chunk)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError("file changed while being hashed")
        return hasher.hexdigest(), b"".join(chunks)
    finally:
        os.close(fd)


def _lstat_regular_check(path: Path, label: str) -> str:
    """Return an error string if *path* is missing or is not a regular file
    (symlinks and special files are rejected — never followed).  Returns an
    empty string when *path* is a regular file."""
    if not path.exists() and not path.is_symlink():
        return f"{label} missing on disk"
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        return f"{label} is a symlink, not a regular file"
    if not stat.S_ISREG(st.st_mode):
        return f"{label} is not a regular file (st_mode={oct(st.st_mode)})"
    return ""
