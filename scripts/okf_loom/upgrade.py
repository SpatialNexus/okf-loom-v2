"""Read-only planning and opt-in, forward-recoverable OKF 0.1 -> 0.2.

Each concept edit is additive; legacy evidence is retained. The declaration
is written last. A killed process can leave compatible mixed metadata, never
half a file. Linux descriptor-relative exchange retains displaced evidence;
caught failures reconcile even a syscall that completed before raising. There
is no bundle-wide atomicity or hostile same-UID writer guarantee.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from . import SPEC_VERSION, _yaml_compat as yaml
from .config import CONFIG_FILENAME, OkfConfig, OkfConfigError
from .exceptions import OKFParseError
from .ignore import iter_markdown_files
from .io_utils import _tree_directory_fd, validate_tree_path
from .metadata import instant_text, metadata_issues
from .model import Bundle
from .parse import parse_document
from .roundtrip import _values_equal
from .validate import _parse_version


class UpgradeError(ValueError):
    """Content-redacted refusal; no unsupported conversion is attempted."""


@dataclass(frozen=True)
class _Entry:
    identity: tuple
    ctime_ns: int
    data: bytes | None

    def same_file(self, other: _Entry | None) -> bool:
        # rename/exchange changes ctime, not the identity/content authority.
        return other is not None and (self.identity, self.data) == (other.identity, other.data)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns)


def _directory_identity(fd):
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino, info.st_mode)


def _entry(parent: int, leaf: str, *, foreign: bool = False) -> _Entry | None:
    """Bounded no-follow read; foreign objects may be retained, never followed."""
    try:
        info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        if foreign:
            return _Entry(_identity(info), info.st_ctime_ns, None)
        raise UpgradeError("upgrade.unsafe_path: expected singly-linked regular source files")
    fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    try:
        start = os.fstat(fd)
        if _identity(start) != _identity(info) or start.st_ctime_ns != info.st_ctime_ns:
            raise UpgradeError("upgrade.rev_conflict: source identity changed during open")
        if start.st_size > 1048576:
            raise UpgradeError("upgrade.source_too_large: source exceeds 1 MiB")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, 1048577 - size))
            if not chunk:
                break
            chunks.append(chunk); size += len(chunk)
            if size > 1048576:
                raise UpgradeError("upgrade.source_too_large: source exceeds 1 MiB")
        end = os.fstat(fd)
        visible = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (_identity(end), end.st_ctime_ns) != (_identity(start), start.st_ctime_ns) or (
                _identity(visible), visible.st_ctime_ns) != (_identity(start), start.st_ctime_ns):
            raise UpgradeError("upgrade.rev_conflict: source changed during read")
        return _Entry(_identity(start), start.st_ctime_ns, b"".join(chunks))
    finally:
        os.close(fd)


def _read(root: Path, relative: str, identities: dict | None = None) -> bytes | None:
    path = root / relative
    with _tree_directory_fd(path.parent) as parent:
        entry = _entry(parent, path.name)
        if identities is not None:
            identities[relative] = (_directory_identity(parent), entry)
        return entry.data if entry is not None else None


def _snapshot(root: Path, identities: dict | None = None) -> dict[str, bytes | None]:
    # Do not use a permissive config fallback to decide what to mutate.
    _read(root, CONFIG_FILENAME)
    try:
        config = OkfConfig.load(root).bundle
    except OkfConfigError as exc:
        raise UpgradeError("upgrade.config_invalid: repair scan configuration before migration") from exc
    names = {"index.md", CONFIG_FILENAME, ".gitignore"}
    for path in iter_markdown_files(root, exclude=config.exclude, include=config.include,
                                    respect_gitignore=config.respect_gitignore):
        names.add(path.relative_to(root).as_posix())
        for parent in (path.parent, *path.parent.parents):
            if not parent.is_relative_to(root):
                break
            names.add((parent / ".gitignore").relative_to(root).as_posix())
    return {name: _read(root, name, identities) for name in sorted(names)}


def _digest(snapshot: dict[str, bytes | None]) -> str:
    rows = [(name, hashlib.sha256(raw).hexdigest() if raw is not None else None)
            for name, raw in sorted(snapshot.items())]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _header_edit(raw: str, fm: dict, *, generated: dict | None = None) -> str:
    """Change only a simple declaration token or append generation metadata.

    No serializer fallback is allowed: exotic YAML stays authored or fails
    closed. Every original header/body byte except the version token survives.
    """
    lines = raw.splitlines(keepends=True)
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    expected = dict(fm)
    if generated is not None:
        expected["generated"] = generated
        addition = yaml.safe_dump({"generated": generated}, sort_keys=False,
                                  allow_unicode=True).replace("\n", newline)
        lines.insert(end, addition)
    else:
        # Only plain top-level scalar spelling; retain quotes, spaces, comments.
        pattern = re.compile(r'^(okf_version:[ \t]*)([\'"]?)([0-9]+\.[0-9]+)(\2)([ \t]*(?:#.*)?)(\r?\n)$')
        matches = [(i, pattern.fullmatch(line)) for i, line in enumerate(lines[1:end], 1)]
        matches = [(i, match) for i, match in matches if match is not None]
        if len(matches) != 1:
            raise UpgradeError("upgrade.header_ambiguous: root declaration needs manual review")
        i, match = matches[0]
        # Always quote the new version, including a formerly numeric scalar.
        quote = match[2] or '"'
        lines[i] = match[1] + quote + SPEC_VERSION + quote + match[5] + match[6]
        expected["okf_version"] = SPEC_VERSION
    result = "".join(lines)
    actual, _ = parse_document(result)
    if not _values_equal(actual, expected):
        raise UpgradeError("upgrade.header_ambiguous: header edit cannot preserve semantics")
    return result


@dataclass
class UpgradePlan:
    root: Path
    before: dict[str, bytes | None]
    changes: dict[str, str]
    report: dict
    identities: dict


def plan_upgrade(root: str | Path, *, actor: str | None = None,
                 target: str = SPEC_VERSION) -> UpgradePlan:
    """Inventory without locks, directories, caches, log entries, or writes.

    An explicit actor asserts provenance for existing content, NOT the identity
    of whoever invokes migration. No actor is inferred from the environment.
    """
    root = validate_tree_path(root)
    if not root.is_dir():
        raise FileNotFoundError("Bundle directory not found")
    if actor is not None and (not isinstance(actor, str) or not actor.strip()
                              or any(ord(c) < 32 for c in actor)):
        raise UpgradeError("upgrade.invalid_actor: supply a non-empty single-line actor")
    identities: dict = {}
    before = _snapshot(root, identities)
    raw_index = before["index.md"]
    try:
        index_fm, _ = parse_document(raw_index.decode("utf-8")) if raw_index else ({}, "")
    except (OKFParseError, ValueError, UnicodeError) as exc:
        raise UpgradeError("index.parse_failed: cannot read root declaration") from exc
    declared = "okf_version" in index_fm
    source = _parse_version(str(index_fm["okf_version"])) if declared else _parse_version(SPEC_VERSION)
    if source is None:
        raise UpgradeError("spec.version_malformed: expected ASCII major.minor; compatibility is unknown")
    if _parse_version(target) != _parse_version(SPEC_VERSION):
        raise UpgradeError(f"unsupported target SPEC v{target}; no files changed")
    if source not in ((0, 1), (0, 2)):
        raise UpgradeError(f"unsupported conversion to SPEC v{target}; no files changed")
    changes: dict[str, str] = {}
    deferred: list[dict] = []
    # Undeclared bundles retain the current-version assumption; do not silently
    # adopt or relabel them. Declared 0.2 can finish a prior partial migration.
    if declared:
        for name, raw in before.items():
            if raw is None or not name.endswith(".md") or Path(name).name in ("index.md", "log.md"):
                continue
            try:
                text = raw.decode("utf-8")
                fm, body = parse_document(text)
            except (OKFParseError, ValueError, UnicodeError) as exc:
                raise UpgradeError("upgrade.parse_failed: concept needs manual repair") from exc
            if not isinstance(fm.get("type"), str) or not fm["type"].strip():
                raise UpgradeError("upgrade.missing_type: concept needs manual repair")
            reasons = []
            if "timestamp" in fm and "generated" not in fm:
                at = instant_text(fm["timestamp"])
                if at is None:
                    reasons.append("legacy_timestamp_invalid")
                elif actor is None:
                    reasons.append("legacy_timestamp_needs_actor")
                else:
                    try:
                        changes[name] = _header_edit(text, fm, generated={"by": actor, "at": at})
                    except (OKFParseError, ValueError):
                        reasons.append("header_needs_review")
            if any(field.startswith("generated") for _code, field, _msg in metadata_issues(fm)):
                reasons.append("existing_generated_needs_review")
            if "citations" in fm or re.search(r"^#{1,6}[ \t]+Citations\b", body, re.M | re.I):
                reasons.append("citations_preserved_needs_review")
            if reasons:
                deferred.append({"path": name, "reasons": reasons})
        if source == (0, 1):
            changes["index.md"] = _header_edit(raw_index.decode("utf-8"), index_fm)
    report = {
        "target": SPEC_VERSION, "source": ".".join(map(str, source)),
        "undeclared": not declared, "revision": _digest(before),
        "status": "partial" if deferred else "ready" if changes else "noop",
        "changes": [{"path": name, "action": "declare_version" if name == "index.md"
                     else "add_attributed_generated_retaining_timestamp"} for name in changes],
        "deferred": deferred, "applied": False,
    }
    return UpgradePlan(root, before, changes, report, identities)


def _exchange(stage_fd: int, stage_leaf: str, parent_fd: int, leaf: str) -> None:
    # Reuse the supported Linux primitive, without Proposal repository/session
    # activation and without an ordinary rename fallback. Index is a valid
    # migration target, so Proposal's concept-only coordinator is not reused.
    from .proposal_review.publication import _rename_exchange
    _rename_exchange(stage_fd, stage_leaf, parent_fd, leaf)


class _Publication:
    """One pinned target and its retained exchange slot. Not a durable journal."""

    def __init__(self, path: Path, parent: int, expected: tuple, raw: str):
        self.path, self.parent, self.expected = path, parent, expected
        self.raw = raw.encode("utf-8")
        if len(self.raw) > 1048576:
            raise UpgradeError("upgrade.source_too_large: migrated source would exceed 1 MiB")
        self.stage_name = f".okf-upgrade-{path.name}-{secrets.token_hex(12)}"
        self.stage = None
        self.stage_identity = None
        self.candidate = None
        self.attempted = False
        self.clean_entry = None

    def visible(self):
        with _tree_directory_fd(self.path.parent) as visible:
            if _directory_identity(visible) != self.expected[0]:
                raise UpgradeError("upgrade.rev_conflict: source parent substituted")

    def prepare(self):
        self.visible()
        if _directory_identity(self.parent) != self.expected[0] or _entry(self.parent, self.path.name) != self.expected[1]:
            raise UpgradeError("upgrade.rev_conflict: source identity changed")
        os.mkdir(self.stage_name, 0o700, dir_fd=self.parent)
        info = os.stat(self.stage_name, dir_fd=self.parent, follow_symlinks=False)
        self.stage_identity = (info.st_dev, info.st_ino, info.st_mode)
        self.stage = os.open(self.stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.parent)
        if (_directory_identity(self.stage) != self.stage_identity
                or stat.S_IMODE(os.fstat(self.stage).st_mode) not in (0o700, 0o2700)):
            raise UpgradeError("upgrade.unsafe_path: private exchange directory substituted")
        fd = os.open("entry", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.stage)
        try:
            data = memoryview(self.raw)
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    raise OSError("upgrade staging write made no progress")
                data = data[written:]
            os.fchmod(fd, stat.S_IMODE(self.expected[1].identity[2]))
            os.fsync(fd)
        finally:
            os.close(fd)
        self.candidate = _entry(self.stage, "entry")
        self.clean_entry = self.candidate
        os.fsync(self.stage)
        os.fsync(self.parent)

    def publish(self):
        self.visible()
        if (_entry(self.parent, self.path.name) != self.expected[1]
                or _entry(self.stage, "entry") != self.candidate):
            raise UpgradeError("upgrade.rev_conflict: source or candidate changed at publication")
        # Record the attempt BEFORE entering a fallible syscall/writer. A
        # wrapper or signal can raise after the kernel has exchanged the names.
        self.attempted = True
        self.clean_entry = None
        _exchange(self.stage, "entry", self.parent, self.path.name)
        self.verify()
        os.fsync(self.parent)
        os.fsync(self.stage)
        self.verify()

    def verify(self):
        self.visible()
        if (not self.candidate.same_file(_entry(self.parent, self.path.name))
                or not self.expected[1].same_file(_entry(self.stage, "entry", foreign=True))):
            raise UpgradeError("upgrade.rev_conflict: exchanged source is not the expected inode and bytes")

    def rollback(self):
        if not self.attempted:
            return
        saved = _entry(self.stage, "entry", foreign=True)
        if self.candidate.same_file(saved):
            # Syscall never took effect (or an interrupted rollback finished).
            self.clean_entry = self.candidate
            return
        current = _entry(self.parent, self.path.name, foreign=True)
        if saved is None or not self.candidate.same_file(current):
            raise UpgradeError("upgrade.rollback_uncertain: rollback conflict; newer bytes preserved")
        # Restore what was ACTUALLY displaced, not a stale preview copy. Thus a
        # same-byte inode swap or different bytes at the syscall seam survive.
        try:
            _exchange(self.stage, "entry", self.parent, self.path.name)
        except BaseException:
            # Infer the outcome from both names, including KeyboardInterrupt
            # after exchange; never rely on the writer returning successfully.
            restored = _entry(self.parent, self.path.name, foreign=True)
            displaced = _entry(self.stage, "entry", foreign=True)
            if not saved.same_file(restored) or not self.candidate.same_file(displaced):
                raise
        restored = _entry(self.parent, self.path.name, foreign=True)
        displaced = _entry(self.stage, "entry", foreign=True)
        if not saved.same_file(restored):
            raise UpgradeError("upgrade.rollback_uncertain: restored source changed; evidence retained")
        if not self.candidate.same_file(displaced):
            # Another canonical writer won at the reverse-exchange seam. Put
            # its displaced entry back, retaining the older evidence. A further
            # race cannot be certified; never delete either remaining entry.
            if displaced is not None:
                _exchange(self.stage, "entry", self.parent, self.path.name)
                os.fsync(self.parent); os.fsync(self.stage)
            raise UpgradeError("upgrade.rollback_uncertain: writer raced rollback; evidence retained")
        os.fsync(self.parent)
        os.fsync(self.stage)
        if (not saved.same_file(_entry(self.parent, self.path.name, foreign=True))
                or not self.candidate.same_file(_entry(self.stage, "entry", foreign=True))):
            raise UpgradeError("upgrade.rollback_uncertain: rollback changed after fsync")
        self.clean_entry = self.candidate

    def cleanup(self):
        if self.stage_identity is None:
            return
        info = os.stat(self.stage_name, dir_fd=self.parent, follow_symlinks=False)
        if (info.st_dev, info.st_ino, info.st_mode) != self.stage_identity:
            raise UpgradeError("upgrade.cleanup_uncertain: exchange directory moved; inspect recovery evidence")
        if self.stage is not None:
            entry = _entry(self.stage, "entry", foreign=True)
            if entry is not None:
                if self.clean_entry is None or not self.clean_entry.same_file(entry):
                    raise UpgradeError("upgrade.cleanup_uncertain: exchange evidence retained")
                os.unlink("entry", dir_fd=self.stage)
                os.fsync(self.stage)
        os.rmdir(self.stage_name, dir_fd=self.parent)
        os.fsync(self.parent)

    def close(self):
        if self.stage is not None:
            os.close(self.stage)
            self.stage = None


def apply_upgrade(plan: UpgradePlan, *, expected_revision: str | None = None) -> dict:
    """Preflight the visible snapshot, verify each exchange, declaration last.

    A preview revision may be supplied across CLI invocations. Runtime state is
    created only for an actual write. Descriptor-relative Linux exchange checks
    displaced authority and retains uncertain recovery evidence. The generic
    BundleMutation pathname rollback is intentionally NOT a migration writer.
    """
    if expected_revision is not None and expected_revision != plan.report["revision"]:
        raise UpgradeError("upgrade.rev_conflict: preview revision changed; no files changed")
    if not plan.changes:
        return dict(plan.report)
    for name, raw in plan.changes.items():
        parts = Path(name).parts
        if (Path(name).is_absolute() or not parts or ".." in parts
                or name not in plan.before or name not in plan.identities
                or plan.before[name] is None or not isinstance(raw, str)):
            raise UpgradeError("upgrade.invalid_plan: expected existing bundle-relative source targets")
    bundle = Bundle(plan.root)
    with bundle.mutation(), ExitStack() as stack:
        identities: dict = {}
        if _snapshot(plan.root, identities) != plan.before or identities != plan.identities:
            raise UpgradeError("upgrade.rev_conflict: bundle changed; no files changed")
        targets = []
        # Pin every source parent before any canonical publication. Index last
        # is enforced here too, not entrusted to a caller's dict ordering.
        for name in sorted(plan.changes, key=lambda name: (name == "index.md", name)):
            path = plan.root / name
            parent = stack.enter_context(_tree_directory_fd(path.parent))
            target = _Publication(path, parent, plan.identities[name], plan.changes[name])
            targets.append(target)
            stack.callback(target.close)
        try:
            for target in targets:
                target.prepare()
            for target in targets:
                target.publish()
            for target in targets:
                target.verify()
        except BaseException as original:
            uncertain = []
            for target in reversed(targets):
                try:
                    target.rollback()
                    target.cleanup()
                except BaseException:
                    uncertain.append(target.stage_name)
            if uncertain:
                raise UpgradeError("upgrade.rollback_uncertain: rollback conflict or durability failure; "
                                   "newer bytes preserved; inspect sources and remaining exchange evidence "
                                   "under the pinned source parents: "
                                   + ", ".join(uncertain)) from original
            raise
        # All source publications have settled before old evidence is removed.
        # Cleanup failure is NOT permission to undo a now-committed batch using
        # partially deleted backups. Report it distinctly for manual inspection.
        for target in targets:
            target.clean_entry = target.expected[1]
            try:
                target.cleanup()
            except BaseException as error:
                raise UpgradeError("upgrade.cleanup_uncertain: source changes published; cleanup durability "
                                   "is uncertain; inspect sources and any remaining exchange directory "
                                   + target.stage_name) from error
    return {**plan.report, "applied": True}
