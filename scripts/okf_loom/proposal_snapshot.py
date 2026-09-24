"""Production live-bundle capture and filesystem-free Proposal Review adapters.

This module is deliberately not imported by the pure staging/preview modules.
It is the one production seam which turns a live Linux directory into frozen,
immutable authority and then releases bundle ownership before pure work starts.
"""
from __future__ import annotations

import base64
import datetime as _datetime
import os
import posixpath
import stat
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .config import CONFIG_FILENAME, OkfConfig, OkfConfigError
from .ignore import GITIGNORE_FILENAME, VisibilityRulePolicy, compile_rules
from .io_utils import BundleBusyError, bundle_transaction_lock
from .model import Bundle, LoadWarning
from .paths import ConceptIdError, concept_id_from_str, concept_id_to_str
from .proposal_runtime_identity import (OPERATION_RUNTIME_ID, SERIALIZER_ID,
                                        VALIDATOR_ID, VALIDATOR_RULE_CODES)
from .validate import (VALIDATOR_RULE_REGISTRY, ValidationFileFacts,
                       validate_bundle)
from .proposal_review.canonical import canonical_digest, dumps_canonical, sha256_hex
from .proposal_review.errors import PreviewPipelineError, SchemaError, ValidationProfileError
from .proposal_review.operations import RoundTripDocumentCodec
from .proposal_review.ports import (FrozenValidationReport, FrozenValidatorFinding,
                    RuntimeIdentities)
from .proposal_review.preview import build_preview
from .proposal_review.schemas import MAX_CONCEPT_BYTES, Operation, Snapshot
from .proposal_review.staging import StagedResult, stage_operations


MAX_CAPTURE_FILES = 100_000
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_CONTROL_BYTES = 1024 * 1024
MAX_NON_CONCEPT_BYTES = 16 * 1024 * 1024
CAPTURE_RETRIES = 1
MAX_TOTAL_ENTRIES = 100_000
MAX_DIRECTORY_ENTRIES = 10_000
MAX_CAPTURE_DEPTH = 64
MAX_COMPONENT_BYTES = 255
MAX_RELATIVE_PATH_BYTES = 4096


def _is_implementation_artifact(name: str) -> bool:
    return name == ".okf-loom-bundle.lock" or name.endswith(".okf-lock")


def _validated_name(name: str, *, parent: str) -> str:
    if not isinstance(name, str) or name in ("", ".", "..") or "\\" in name or "\x00" in name:
        raise SnapshotCaptureError("snapshot_unsafe", "bundle entry has an unsafe name")
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SnapshotCaptureError("snapshot_unsafe", "bundle entry name is not valid UTF-8") from exc
    if len(encoded) > MAX_COMPONENT_BYTES:
        raise SnapshotCaptureError("snapshot_limit_exceeded", "bundle entry name exceeds component limit")
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        raise SnapshotCaptureError("snapshot_unsafe", "bundle entry name contains a control character")
    rel = f"{parent}/{name}" if parent else name
    if len(rel.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise SnapshotCaptureError("snapshot_limit_exceeded", "bundle-relative path exceeds byte limit")
    return rel


class SnapshotCaptureError(PreviewPipelineError):
    """Typed, transport-neutral capture failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message, code=code)
        self.retryable = retryable


@dataclass(frozen=True)
class FileFact:
    path: str
    kind: str
    size: int
    revision: str

    def to_mapping(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "size": self.size,
                "revision": self.revision}


@dataclass(frozen=True)
class CapturedWarning:
    code: str
    path: str | None
    line: int
    message: str
    fingerprint: str

    def to_mapping(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "line": self.line,
                "message": self.message, "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class FrozenBundleFacts:
    reserved_files: Mapping[str, bytes]
    visible_non_concept_files: tuple[FileFact, ...]
    scan_controls: Mapping[str, bytes]
    load_warnings: tuple[CapturedWarning, ...]
    validation_policy: Mapping[str, Any]
    config: OkfConfig

    def __post_init__(self) -> None:
        object.__setattr__(self, "reserved_files",
                           MappingProxyType({k: bytes(v) for k, v in sorted(self.reserved_files.items())}))
        object.__setattr__(self, "scan_controls",
                           MappingProxyType({k: bytes(v) for k, v in sorted(self.scan_controls.items())}))
        object.__setattr__(self, "validation_policy", _deep_freeze(dict(self.validation_policy)))

    def validation_context_projection(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "reserved_files": _byte_map_projection(self.reserved_files),
            "visible_non_concept_files": [f.to_mapping() for f in self.visible_non_concept_files],
            "scan_controls": _byte_map_projection(self.scan_controls),
            "load_warnings": [w.to_mapping() for w in self.load_warnings],
            "validation_policy": _deep_thaw(self.validation_policy),
        }


@dataclass(frozen=True)
class CapturedBundle:
    snapshot: Snapshot
    concept_bytes: Mapping[str, bytes]
    facts: FrozenBundleFacts
    snapshot_digest: str
    config_digest: str
    validation_context_digest: str
    capabilities: tuple[str, ...]
    concept_revisions: Mapping[str, str]
    validator_id: str
    serializer_id: str
    operation_runtime_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "concept_bytes", MappingProxyType(
            {k: bytes(v) for k, v in sorted(self.concept_bytes.items())}))
        object.__setattr__(self, "concept_revisions", MappingProxyType(
            dict(sorted(self.concept_revisions.items()))))

    def environment_digest(self) -> str:
        return canonical_digest({
            "config_digest": self.config_digest,
            "validation_context_digest": self.validation_context_digest,
            "capabilities_digest": canonical_digest(list(self.capabilities)),
            "validator_id": self.validator_id,
            "serializer_id": self.serializer_id,
            "operation_runtime_id": self.operation_runtime_id,
        })


@dataclass(frozen=True)
class CapturePreviewResult:
    captured: CapturedBundle
    staged: StagedResult
    preview: Any
    artifact_bytes: bytes
    read_model_size_check_deferred: bool


@dataclass(frozen=True)
class _Leaf:
    path: str
    data: bytes
    device: int
    inode: int
    parent_device: int
    parent_inode: int
    ctime_ns: int
    mode: int
    size: int

    @property
    def revision(self) -> str:
        return sha256_hex(self.data)

    def manifest(self) -> dict[str, Any]:
        return {"path": self.path, "device": self.device, "inode": self.inode,
                "parent_device": self.parent_device, "parent_inode": self.parent_inode,
                "ctime_ns": self.ctime_ns, "mode": self.mode, "size": self.size,
                "revision": self.revision}


@dataclass(frozen=True)
class _Scan:
    leaves: Mapping[str, _Leaf]
    directories: tuple[tuple[str, int, int, int], ...]
    controls: Mapping[str, bytes]
    config: OkfConfig

    def manifest(self) -> dict[str, Any]:
        return {"files": [self.leaves[k].manifest() for k in sorted(self.leaves)],
                "directories": [list(v) for v in self.directories],
                "controls": _byte_map_projection(self.controls),
                "effective_config": self.config.effective_projection()}


class _Unstable(Exception):
    pass


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _deep_thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(v) for v in value]
    return value


def _byte_projection(data: bytes) -> dict[str, Any]:
    return {"revision": sha256_hex(data), "size": len(data),
            "bytes_base64": base64.b64encode(data).decode("ascii")}


def _byte_map_projection(values: Mapping[str, bytes]) -> dict[str, Any]:
    return {k: _byte_projection(values[k]) for k in sorted(values)}


def _runtime_id(name: str, version: str, material: bytes) -> str:
    return f"{name}@{version}+sha256:{sha256_hex(material)}"


DEFAULT_RUNTIME_IDENTITIES = RuntimeIdentities(
    validator_id=VALIDATOR_ID,
    serializer_id=SERIALIZER_ID,
    operation_runtime_id=OPERATION_RUNTIME_ID,
)


def frozen_validation_policy(profile: str = "producer", *,
                             fail_on_broken_links: bool = False) -> dict[str, Any]:
    if profile not in ("spec", "producer", "loose"):
        raise ValidationProfileError(f"validation profile {profile!r} is not installed")
    if tuple(sorted(VALIDATOR_RULE_REGISTRY)) != VALIDATOR_RULE_CODES:
        raise SnapshotCaptureError(
            "runtime_identity_mismatch",
            "validator rule registry changed without a reviewed runtime identity bump",
        )
    rules = []
    for code, rule in sorted(VALIDATOR_RULE_REGISTRY.items()):
        effective = rule.effective_severity(
            profile, fail_on_broken_links=fail_on_broken_links
        ).value
        rules.append({"code": code, "base_severity": rule.base_severity.value,
                      "scope": rule.scope, "severity": effective,
                      "fails_profile": effective == "error",
                      "parameters": ({"fail_on_broken_links": True}
                                     if code in ("link.broken", "asset.missing") and fail_on_broken_links
                                     else {})})
    policy_material = canonical_digest({"profile": profile, "rules": rules}).encode("ascii")
    return {"profile": profile,
            "policy_id": _runtime_id("okf-validation-policy", "1.0", policy_material),
            "rules": rules}


def _open_root(path: str | Path) -> tuple[int, tuple[int, int]]:
    supplied = os.fspath(path)
    before = os.stat(supplied, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotCaptureError("snapshot_unsafe", "bundle root must not be a symlink")
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotCaptureError("snapshot_unsafe", "bundle root must be a directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(supplied, flags)
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        raise _Unstable("bundle root identity changed")
    return fd, (after.st_dev, after.st_ino)


def _verify_root(path: str | Path, fd: int, identity: tuple[int, int]) -> None:
    current_fd = os.fstat(fd)
    try:
        current_path = os.stat(os.fspath(path), follow_symlinks=False)
    except OSError as exc:
        raise _Unstable("bundle root was substituted") from exc
    if (current_fd.st_dev, current_fd.st_ino) != identity or (current_path.st_dev, current_path.st_ino) != identity:
        raise _Unstable("bundle root was substituted")


def _read_regular(parent_fd: int, name: str, rel: str, *, limit: int) -> _Leaf:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise SnapshotCaptureError("snapshot_unsafe", f"captured leaf is unreadable: {rel}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotCaptureError("snapshot_unsafe", f"visible symlink is not allowed: {rel}")
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotCaptureError("snapshot_unsafe", f"visible non-regular file is not allowed: {rel}")
    if before.st_size > limit:
        raise SnapshotCaptureError("snapshot_limit_exceeded", f"captured file exceeds limit: {rel}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise SnapshotCaptureError("snapshot_unsafe", f"captured leaf cannot be opened: {rel}") from exc
    try:
        opened = os.fstat(fd)
        ident = (before.st_dev, before.st_ino, before.st_ctime_ns, before.st_mode, before.st_size)
        if ident != (opened.st_dev, opened.st_ino, opened.st_ctime_ns, opened.st_mode, opened.st_size):
            raise _Unstable(f"captured leaf changed while opening: {rel}")
        chunks: list[bytes] = []
        total = 0
        while True:
            part = os.read(fd, min(65536, limit + 1 - total))
            if not part:
                break
            total += len(part)
            if total > limit:
                raise SnapshotCaptureError("snapshot_limit_exceeded", f"captured file exceeds limit: {rel}")
            chunks.append(part)
        final = os.fstat(fd)
        if ident != (final.st_dev, final.st_ino, final.st_ctime_ns, final.st_mode, final.st_size) or total != before.st_size:
            raise _Unstable(f"captured leaf changed while reading: {rel}")
        data = b"".join(chunks)
        parent = os.fstat(parent_fd)
        return _Leaf(rel, data, before.st_dev, before.st_ino, parent.st_dev,
                     parent.st_ino, before.st_ctime_ns, before.st_mode, total)
    finally:
        os.close(fd)


def _scan(root_fd: int) -> _Scan:
    # Config controls traversal and is captured before any visibility decision.
    try:
        cfg_stat = os.stat(CONFIG_FILENAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        cfg_leaf = None
        config = OkfConfig()
    else:
        if stat.S_ISLNK(cfg_stat.st_mode) or not stat.S_ISREG(cfg_stat.st_mode):
            raise SnapshotCaptureError("snapshot_unsafe", "configuration must be a no-follow regular file")
        cfg_leaf = _read_regular(root_fd, CONFIG_FILENAME, CONFIG_FILENAME, limit=MAX_CONTROL_BYTES)
        try:
            config = OkfConfig.parse_bytes(cfg_leaf.data)
        except OkfConfigError:
            config = OkfConfig()

    bundle_cfg = config.bundle
    policy = VisibilityRulePolicy.compile(exclude=bundle_cfg.exclude,
                                          include=bundle_cfg.include)
    leaves: dict[str, _Leaf] = {}
    controls: dict[str, bytes] = {}
    directories: list[tuple[str, int, int, int]] = []
    aggregate = 0

    def add(leaf: _Leaf, *, control: bool = False) -> None:
        nonlocal aggregate
        aggregate += leaf.size
        if len(leaves) >= MAX_CAPTURE_FILES or aggregate > MAX_CAPTURE_BYTES:
            raise SnapshotCaptureError("snapshot_limit_exceeded", "bundle capture exceeds file/byte limits")
        leaves[leaf.path] = leaf
        if control:
            controls[leaf.path] = leaf.data

    total_entries = 0

    def open_dir(components: tuple[str, ...]) -> int:
        current = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                          getattr(os, "O_CLOEXEC", 0), dir_fd=root_fd)
        try:
            for component in components:
                nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                              getattr(os, "O_CLOEXEC", 0), dir_fd=current)
                os.close(current)
                current = nxt
            return current
        except BaseException:
            os.close(current)
            raise

    stack: list[tuple[tuple[str, ...], str, tuple[Any, ...], bool,
                      frozenset[Any], int]] = [((), "", (), False, frozenset(), 0)]
    while stack:
        components, rel, git_rules, suppressed, masked, depth = stack.pop()
        if depth > MAX_CAPTURE_DEPTH:
            raise SnapshotCaptureError("snapshot_limit_exceeded", "bundle tree exceeds depth limit")
        fd = open_dir(components)
        try:
            info = os.fstat(fd)
            if len(directories) >= MAX_CAPTURE_FILES:
                raise SnapshotCaptureError("snapshot_limit_exceeded", "bundle capture exceeds directory limit")
            directories.append((rel, info.st_dev, info.st_ino, info.st_mode))
            names: list[str] = []
            directory_entries = 0
            try:
                with os.scandir(fd) as iterator:
                    for entry in iterator:
                        directory_entries += 1
                        total_entries += 1
                        if directory_entries > MAX_DIRECTORY_ENTRIES or total_entries > MAX_TOTAL_ENTRIES:
                            raise SnapshotCaptureError("snapshot_limit_exceeded", "bundle traversal exceeds entry limit")
                        _validated_name(entry.name, parent=rel)
                        names.append(entry.name)
            except UnicodeError as exc:
                raise SnapshotCaptureError("snapshot_unsafe", "bundle directory contains an undecodable name") from exc
            names.sort(key=lambda value: value.encode("utf-8"))
            local_git = git_rules
            if bundle_cfg.respect_gitignore and GITIGNORE_FILENAME in names:
                gi_rel = _validated_name(GITIGNORE_FILENAME, parent=rel)
                gi = _read_regular(fd, GITIGNORE_FILENAME, gi_rel, limit=MAX_CONTROL_BYTES)
                add(gi, control=True)
                try:
                    text = gi.data.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise SnapshotCaptureError("snapshot_unsafe", f"scan control is invalid UTF-8: {gi_rel}") from exc
                local_git = local_git + compile_rules(text.splitlines(), base=rel)
            children: list[tuple[tuple[str, ...], str, tuple[Any, ...], bool,
                                 frozenset[Any], int]] = []
            for name in names:
                child_rel = _validated_name(name, parent=rel)
                if _is_implementation_artifact(name):
                    continue
                if bundle_cfg.respect_gitignore and name == GITIGNORE_FILENAME:
                    continue
                try:
                    child_info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError as exc:
                    raise SnapshotCaptureError("snapshot_unsafe", f"visible entry is unreadable: {child_rel}") from exc
                if stat.S_ISDIR(child_info.st_mode):
                    revive = policy.revives(child_rel)
                    reaches = policy.dir_included(child_rel) or policy.reaches(child_rel)
                    rule_ignored = policy.ignored(local_git, child_rel, True, masked=masked)
                    marker_present = False
                    if revive or reaches or (not suppressed and not rule_ignored):
                        child_fd_probe = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            try:
                                marker = os.stat(".git", dir_fd=child_fd_probe, follow_symlinks=False)
                            except FileNotFoundError:
                                marker = None
                            if marker is not None:
                                if stat.S_ISLNK(marker.st_mode):
                                    raise SnapshotCaptureError("snapshot_unsafe", f"scan-control symlink is not allowed: {child_rel}/.git")
                                marker_type = ("directory" if stat.S_ISDIR(marker.st_mode)
                                               else "regular" if stat.S_ISREG(marker.st_mode)
                                               else "other")
                                controls[f"{child_rel}/.git#manifest"] = dumps_canonical({
                                    "schema": 1, "kind": "nested-repository-marker",
                                    "device": marker.st_dev, "inode": marker.st_ino,
                                    "mode": marker.st_mode, "type": marker_type,
                                    "size": marker.st_size,
                                })
                                marker_present = True
                        finally:
                            os.close(child_fd_probe)
                    next_suppressed = suppressed
                    next_mask = masked
                    descend = True
                    if revive:
                        next_suppressed = False
                        next_mask = policy.revival_mask(local_git, child_rel, masked)
                    elif suppressed:
                        descend = reaches
                        next_suppressed = True
                    else:
                        ignored = rule_ignored or marker_present
                        if ignored:
                            descend = reaches
                            next_suppressed = True
                    if descend:
                        if depth + 1 > MAX_CAPTURE_DEPTH:
                            raise SnapshotCaptureError("snapshot_limit_exceeded", "bundle tree exceeds depth limit")
                        children.append((components + (name,), child_rel, local_git,
                                         next_suppressed, next_mask, depth + 1))
                    continue
                included = name == CONFIG_FILENAME or policy.file_included(child_rel)
                if not included and (suppressed or policy.ignored(local_git, child_rel, False, masked=masked)):
                    continue
                limit = MAX_CONCEPT_BYTES if name.endswith(".md") else MAX_NON_CONCEPT_BYTES
                if name == CONFIG_FILENAME:
                    limit = MAX_CONTROL_BYTES
                leaf = _read_regular(fd, name, child_rel, limit=limit)
                try:
                    leaf.data.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    if name.endswith(".md") or name == CONFIG_FILENAME:
                        raise SnapshotCaptureError("snapshot_unsafe", f"captured source is invalid UTF-8: {child_rel}") from exc
                add(leaf, control=(name == CONFIG_FILENAME))
            stack.extend(reversed(children))
        finally:
            os.close(fd)
    if cfg_leaf is not None:
        scanned = leaves.get(CONFIG_FILENAME)
        if scanned is None or scanned.data != cfg_leaf.data or scanned.manifest() != cfg_leaf.manifest():
            raise _Unstable("configuration changed during scan")
    return _Scan(MappingProxyType(dict(leaves)), tuple(sorted(directories)),
                 MappingProxyType(dict(controls)), config)


def _detached_bundle(concepts: Mapping[str, bytes], reserved: Mapping[str, bytes],
                     *, extra_warnings: Sequence[LoadWarning] = ()) -> Bundle:
    root = Path("/__okf_frozen_bundle__")
    bundle = Bundle(root, name="frozen", filesystem_free=True,
                    warnings=list(extra_warnings))
    for path, data in sorted({**reserved, **{f"{k}.md": v for k, v in concepts.items()}}.items()):
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise SnapshotCaptureError("snapshot_unsafe", f"captured source is invalid UTF-8: {path}") from exc
        rel = Path(path)
        synthetic = root / rel
        if rel.name == "index.md":
            bundle._load_index(synthetic, rel, text)
        elif rel.name == "log.md":
            bundle._load_log(synthetic, rel, text)
        else:
            bundle._load_concept(synthetic, rel, text)
    return bundle


def _captured_warnings(bundle: Bundle) -> tuple[CapturedWarning, ...]:
    values: list[CapturedWarning] = []
    root = bundle.root
    for warning in bundle.warnings:
        path: str | None = None
        if warning.path is not None:
            try:
                path = warning.path.relative_to(root).as_posix()
            except ValueError:
                path = warning.path.as_posix().lstrip("/")
        line = warning.line or 0
        body = {"code": warning.code, "path": path, "line": line, "message": warning.message}
        values.append(CapturedWarning(warning.code, path, line, warning.message,
                                      canonical_digest(body)))
    return tuple(sorted(values, key=lambda w: (0 if w.path is None else 1, w.path or "", w.line, w.code, w.fingerprint)))


def _build_capture(scan: _Scan, *, captured_at: str, profile: str,
                   identities: RuntimeIdentities) -> CapturedBundle:
    concepts: dict[str, bytes] = {}
    reserved: dict[str, bytes] = {}
    nonconcept: list[FileFact] = []
    for path, leaf in sorted(scan.leaves.items()):
        name = posixpath.basename(path)
        if path.endswith(".md") and name not in ("index.md", "log.md"):
            concepts[path[:-3]] = leaf.data
        elif name in ("index.md", "log.md"):
            reserved[path] = leaf.data
        else:
            kind = "validation_input" if path in scan.controls else "asset"
            nonconcept.append(FileFact(path, kind, leaf.size, leaf.revision))
    if not concepts:
        raise SnapshotCaptureError("invalid_bundle", "snapshot requires at least one visible concept")
    bundle = _detached_bundle(concepts, reserved)
    warnings = list(_captured_warnings(bundle))
    # A malformed config is permissive for ordinary loading but frozen as a
    # config-scoped warning and scans with defaults.
    config_leaf = scan.leaves.get(CONFIG_FILENAME)
    if config_leaf is not None:
        try:
            OkfConfig.parse_bytes(config_leaf.data)
        except OkfConfigError as exc:
            message = f"{CONFIG_FILENAME} could not be loaded ({exc}); scanning with default excludes"
            body = {"code": "config.unparseable", "path": CONFIG_FILENAME, "line": 0, "message": message}
            warnings.append(CapturedWarning("config.unparseable", CONFIG_FILENAME, 0, message,
                                            canonical_digest(body)))
    warnings_tuple = tuple(sorted(warnings, key=lambda w: (0 if w.path is None else 1, w.path or "", w.line, w.code, w.fingerprint)))
    policy = frozen_validation_policy(
        profile,
        fail_on_broken_links=scan.config.validate.fail_on_broken_links,
    )
    frozen_config = replace(scan.config, raw=_deep_freeze(scan.config.raw))
    facts = FrozenBundleFacts(reserved, tuple(sorted(nonconcept, key=lambda f: f.path)),
                              scan.controls, warnings_tuple, policy, frozen_config)
    config_digest = canonical_digest(frozen_config.effective_projection())
    context_digest = canonical_digest(facts.validation_context_projection())
    capabilities = tuple(sorted(bundle.capabilities().active_ids))
    revisions = {k: sha256_hex(v) for k, v in sorted(concepts.items())}
    body = {"schema": 1,
            "concepts": [{"concept": k, "revision": revisions[k], "identity": {
                "parent_device": scan.leaves[k + ".md"].parent_device,
                "parent_inode": scan.leaves[k + ".md"].parent_inode,
                "leaf_device": scan.leaves[k + ".md"].device,
                "leaf_inode": scan.leaves[k + ".md"].inode,
                "ctime_ns": scan.leaves[k + ".md"].ctime_ns,
                "mode": scan.leaves[k + ".md"].mode,
                "size": scan.leaves[k + ".md"].size,
                "digest": revisions[k]}} for k in sorted(revisions)],
            "capabilities": list(capabilities), "config_digest": config_digest,
            "validation_context_digest": context_digest,
            "validator_id": identities.validator_id, "serializer_id": identities.serializer_id,
            "operation_runtime_id": identities.operation_runtime_id}
    digest = canonical_digest(body)
    snapshot = Snapshot.from_mapping({**body, "captured_at": captured_at,
                                      "snapshot_digest": digest})
    return CapturedBundle(snapshot, concepts, facts, digest, config_digest,
                          context_digest, capabilities, revisions,
                          identities.validator_id, identities.serializer_id,
                          identities.operation_runtime_id)


def capture_bundle(
    root: str | Path,
    *,
    captured_at: str | None = None,
    profile: str = "producer",
    lock_timeout_seconds: float = 15.0,
    identities: RuntimeIdentities = DEFAULT_RUNTIME_IDENTITIES,
) -> CapturedBundle:
    """Capture a live bundle under shared ownership and exact double scan."""
    if lock_timeout_seconds < 0 or lock_timeout_seconds > 15.0:
        raise ValueError("capture lock timeout must be in 0..15 seconds")
    if captured_at is None:
        captured_at = _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    deadline = time.monotonic() + lock_timeout_seconds
    last: Exception | None = None
    for attempt in range(CAPTURE_RETRIES + 1):
        root_fd = -1
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SnapshotCaptureError("bundle_busy", "bundle transaction lock is busy", retryable=True)
            root_fd, identity = _open_root(root)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SnapshotCaptureError("bundle_busy", "bundle transaction lock is busy", retryable=True)
            with bundle_transaction_lock(root, "shared", timeout_seconds=remaining):
                _verify_root(root, root_fd, identity)
                first = _scan(root_fd)
                _verify_root(root, root_fd, identity)
                second = _scan(root_fd)
                _verify_root(root, root_fd, identity)
                if first.manifest() != second.manifest():
                    raise _Unstable("bundle changed between exact scans")
                return _build_capture(first, captured_at=captured_at, profile=profile,
                                      identities=identities)
        except BundleBusyError as exc:
            raise SnapshotCaptureError("bundle_busy", "bundle transaction lock is busy", retryable=True) from exc
        except _Unstable as exc:
            last = exc
            if attempt >= CAPTURE_RETRIES:
                raise SnapshotCaptureError("snapshot_unstable", "bundle changed during both capture attempts", retryable=True) from exc
        except OSError as exc:
            raise SnapshotCaptureError("snapshot_unsafe", "bundle capture encountered an unreadable or substituted entry") from exc
        finally:
            if root_fd >= 0:
                os.close(root_fd)
    raise SnapshotCaptureError("snapshot_unstable", str(last or "bundle changed"), retryable=True)


class FrozenFileFacts(ValidationFileFacts):
    def __init__(self, facts: FrozenBundleFacts, concepts: Sequence[str] = ()) -> None:
        paths = {f.path for f in facts.visible_non_concept_files}
        paths.update(facts.reserved_files)
        paths.update(f"{concept}.md" for concept in concepts)
        self._paths = frozenset(paths)

    def is_file(self, relative_path: str) -> bool:
        return relative_path in self._paths

    def lookup(self, source_relative_path: Path, target: str) -> tuple[bool, bool]:
        if target.startswith("/"):
            candidate = target.lstrip("/")
        else:
            candidate = posixpath.join(source_relative_path.parent.as_posix(), target)
        normalized = posixpath.normpath(candidate)
        escaped = normalized == ".." or normalized.startswith("../") or normalized.startswith("/")
        # Remove only a semantic current-directory prefix.  ``str.lstrip`` is
        # incorrect here because it treats its argument as a character set and
        # turns a root dotfile such as ``.secret.png`` into ``secret.png``.
        lookup_path = normalized[2:] if normalized.startswith("./") else normalized
        return (not escaped and lookup_path in self._paths), escaped


class ProductionFrozenSnapshotValidator:
    """Validator over captured bytes/facts only; performs no filesystem I/O."""

    def __init__(self, facts: FrozenBundleFacts) -> None:
        self._facts = facts
        policy = _deep_thaw(facts.validation_policy)
        if set(policy) != {"profile", "policy_id", "rules"}:
            raise SchemaError("frozen validation policy is not closed")
        if policy["profile"] not in ("spec", "producer", "loose"):
            raise SchemaError("frozen validation policy profile is unsupported")
        expected_policy_id = _runtime_id(
            "okf-validation-policy", "1.0",
            canonical_digest({"profile": policy["profile"],
                              "rules": policy["rules"]}).encode("ascii"),
        )
        if policy["policy_id"] != expected_policy_id:
            raise SchemaError("frozen validation policy identity mismatch")
        rules: dict[str, Mapping[str, Any]] = {}
        expected_keys = {"code", "base_severity", "scope", "severity",
                         "fails_profile", "parameters"}
        for raw in policy["rules"]:
            if not isinstance(raw, dict) or set(raw) != expected_keys:
                raise SchemaError("frozen validation rule is not closed")
            code = raw["code"]
            if not isinstance(code, str) or not code or code in rules:
                raise SchemaError("frozen validation rules require unique codes")
            if raw["scope"] not in ("bundle", "reserved_file", "config", "capability", "concept"):
                raise SchemaError("frozen validation rule scope is unsupported")
            if raw["base_severity"] not in ("error", "warning", "info") or raw["severity"] not in ("error", "warning", "info"):
                raise SchemaError("frozen validation rule severity is unsupported")
            if not isinstance(raw["fails_profile"], bool) or not isinstance(raw["parameters"], dict):
                raise SchemaError("frozen validation rule fields are malformed")
            rules[code] = MappingProxyType(dict(raw))
        if not rules:
            raise SchemaError("frozen validation policy has no rules")
        if tuple(sorted(rules)) != VALIDATOR_RULE_CODES:
            raise SchemaError("frozen validation policy has missing or unknown rule codes")
        self._profile = policy["profile"]
        self._rules = MappingProxyType(rules)

    def installed_profiles(self) -> tuple[str, ...]:
        return ("loose", "producer", "spec")

    def validate(self, snapshot: Mapping[str, bytes], *, profile: str) -> FrozenValidationReport:
        if profile not in self.installed_profiles():
            raise ValidationProfileError(f"validation profile {profile!r} is not installed")
        if profile != self._profile:
            raise ValidationProfileError(
                f"validation profile {profile!r} does not match the frozen policy profile"
            )
        # Config warnings are immutable captured context. Source warnings are
        # recomputed from supplied bytes so staged parse/type changes are seen.
        extras = [LoadWarning(w.code, w.message,
                              Path("/__okf_frozen_bundle__") / w.path if w.path else None,
                              w.line or None)
                  for w in self._facts.load_warnings if w.code.startswith("config.")]
        bundle = _detached_bundle(snapshot, self._facts.reserved_files,
                                  extra_warnings=extras)
        files = FrozenFileFacts(self._facts, tuple(snapshot))
        report = validate_bundle(bundle, file_facts=files, _normalize_rules=False)
        values: list[FrozenValidatorFinding] = []
        failed = False
        for finding in report.findings:
            rule = self._rules.get(finding.code)
            if rule is None:
                raise SchemaError(f"raw validator emitted code absent from frozen policy: {finding.code!r}")
            scope = rule["scope"]
            concept = concept_id_to_str(finding.concept_id) if finding.concept_id else None
            if scope == "concept" and concept is None and finding.path is not None:
                try:
                    rel = finding.path.relative_to(bundle.root).as_posix()
                    if rel.endswith(".md") and posixpath.basename(rel) not in ("index.md", "log.md"):
                        concept = rel[:-3]
                except ValueError:
                    pass
            if scope == "concept" and concept is None:
                raise SchemaError(
                    f"concept-scoped finding {finding.code!r} has no canonical concept owner"
                )
            if scope == "concept":
                try:
                    canonical = concept_id_to_str(concept_id_from_str(concept))
                except ConceptIdError as exc:
                    raise SchemaError("concept-scoped finding has a noncanonical owner") from exc
                if canonical != concept or concept not in snapshot:
                    raise SchemaError("concept-scoped finding owner is absent from frozen snapshot")
                if finding.path is not None:
                    try:
                        owner_path = finding.path.relative_to(bundle.root).as_posix()
                    except ValueError as exc:
                        raise SchemaError("concept-scoped finding path escapes frozen bundle") from exc
                    if (owner_path.endswith(".md") and
                            posixpath.basename(owner_path) not in ("index.md", "log.md") and
                            owner_path[:-3] != concept):
                        raise SchemaError("concept-scoped finding path and owner disagree")
            elif concept is not None:
                raise SchemaError(f"{scope}-scoped finding must not claim a concept")
            severity = rule["severity"]
            body = {"code": finding.code, "severity": severity,
                    "concept": concept, "line": finding.line or 0,
                    "message": finding.message}
            fingerprint = canonical_digest(body)
            values.append(FrozenValidatorFinding(
                finding.code, severity, concept, finding.line or 0,
                finding.message, scope=scope, fingerprint=fingerprint))
            failed = failed or bool(rule["fails_profile"])
        values.sort(key=lambda f: (0 if f.concept is None else 1, f.concept or "",
                                   f.line, f.severity, f.code,
                                   canonical_digest({"code": f.code, "severity": f.severity,
                                                     "concept": f.concept, "line": f.line,
                                                     "message": f.message})))
        return FrozenValidationReport(tuple(values), not failed)


ProductionDocumentCodec = RoundTripDocumentCodec


def capture_stage_validate_preview(
    root: str | Path,
    *,
    proposal_id: str,
    proposal_revision: int,
    operations: Sequence[Operation],
    generated_at: str,
    expires_at: str,
    profile: str = "producer",
    captured_at: str | None = None,
    read_model_overhead_bytes: int | None = None,
) -> CapturePreviewResult:
    """Compose capture → stage → frozen validation → canonical preview.

    The complete read-model overhead is unavailable until the future
    persistence/read-model boundary.  Passing ``None`` records that check as
    explicitly deferred; a caller that already owns the exact overhead may
    provide it for immediate enforcement.
    """
    captured = capture_bundle(root, captured_at=captured_at or generated_at,
                              profile=profile)
    codec = ProductionDocumentCodec()
    staged = stage_operations(captured.snapshot, captured.concept_bytes,
                              operations, codec=codec)
    validator = ProductionFrozenSnapshotValidator(captured.facts)
    preview, artifact = build_preview(
        proposal_id=proposal_id, proposal_revision=proposal_revision,
        snapshot=captured.snapshot, operations=operations, staged=staged,
        complete_before=captured.concept_bytes, validator=validator,
        generated_at=generated_at, expires_at=expires_at, profile=profile,
        read_model_overhead_bytes=(read_model_overhead_bytes or 0))
    return CapturePreviewResult(captured, staged, preview, bytes(artifact),
                                read_model_overhead_bytes is None)


# Concise alias used by service composition without activating a service.
capture_to_preview = capture_stage_validate_preview


__all__ = [
    "MAX_CAPTURE_FILES", "MAX_CAPTURE_BYTES", "MAX_CONTROL_BYTES",
    "MAX_NON_CONCEPT_BYTES", "CAPTURE_RETRIES", "SnapshotCaptureError",
    "FileFact", "CapturedWarning", "FrozenBundleFacts", "CapturedBundle",
    "CapturePreviewResult", "DEFAULT_RUNTIME_IDENTITIES", "frozen_validation_policy",
    "capture_bundle", "FrozenFileFacts", "ProductionFrozenSnapshotValidator",
    "ProductionDocumentCodec", "capture_stage_validate_preview", "capture_to_preview",
]
