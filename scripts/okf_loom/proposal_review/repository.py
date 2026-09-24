"""Proposal Review v1 repository authority and startup recovery.

This module owns only proposal repository bytes.  It does not publish bundle
concepts and deliberately has no dependency on server, Studio, CLI, rendering,
viewer, mutation, or Bundle code.
"""
from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import os
import errno
import re
import time
import contextlib
import threading
from types import MappingProxyType
from dataclasses import dataclass, field
from typing import Any, Mapping

from .canonical import canonical_digest, compute_checksum, dumps_canonical, loads_canonical
from .errors import (
    IdempotencyConflictError,
    IntegrityError,
    LockUnavailableError,
    RepositoryCorruptError,
    RepositoryError,
    RepositoryQuotaError,
    SchemaError,
)
from .ids import validate_digest, validate_timestamp, validate_proposal_id, validate_out_id
from .ports import ActivityLinkVerifier, Clock, Entropy, FilesystemRoot, SystemClock, SystemEntropy
from . import schemas


REPOSITORY_DIR = "proposal-review-v1"
ACTIVE_LOG_BYTES = 8 * 1024 * 1024
MAX_GENERATIONS = 8
REPOSITORY_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
CHECKPOINT_QUOTA_BYTES = 64 * 1024 * 1024
TOMBSTONE_ROWS = 100_000
TOMBSTONE_BYTES = 64 * 1024 * 1024
QUARANTINE_BYTES = 256 * 1024 * 1024
AUDIT_ROWS = 1_000_000
AUDIT_BYTES = 512 * 1024 * 1024

AGGREGATES = tuple(sorted(schemas.AGGREGATE_KINDS))
_DISK_NAME = {"endpoint_results": "endpoint-results", **{k: k for k in AGGREGATES if k != "endpoint_results"}}
_KIND_AGGREGATE = {
    "proposal": "proposals", "withdrawal": "proposals",
    "decision": "decisions", "transaction": "transactions",
    "endpoint_result": "endpoint_results", "outbox": "outbox",
    "tombstone": "tombstones", "corruption": "corruption", "repair": "corruption",
}
_PAYLOAD_PARSER = {
    "proposal": schemas.ProposalRevision,
    "withdrawal": schemas.Withdrawal,
    "decision": schemas.DecisionRow,
    "transaction": schemas.TransactionAudit,
    "endpoint_result": schemas.EndpointResult,
    "outbox": schemas.OutboxReceipt,
    "tombstone": schemas.Tombstone,
    "corruption": schemas.CorruptionMarker,
    "repair": schemas.Repair,
}
_GEN_RE = re.compile(r"\A([a-z-]+)-(\d{20})\.jsonl\Z")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _linux_process_identity(pid: int) -> tuple[str, str | None]:
    """Return (alive/dead/unknown, Linux start-time token).

    The kernel start-time field prevents PID reuse from inheriting a lease.
    Unknown is deliberately fail-closed: callers retain expired authority.
    """
    try:
        fd = os.open(f"/proc/{pid}/stat", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return "dead", None
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESRCH}:
            return "dead", None
        return "unknown", None
    try:
        raw = os.read(fd, 4096)
        if os.read(fd, 1):
            return "unknown", None
    except OSError:
        return "unknown", None
    finally:
        os.close(fd)
    try:
        text = raw.decode("ascii")
        close = text.rfind(")")
        if close < 1:
            return "unknown", None
        fields = text[close + 2:].split()
        token = fields[19]  # proc(5) field 22; fields begins at field 3.
        if not token.isdigit():
            return "unknown", None
        return "alive", token
    except (UnicodeDecodeError, IndexError):
        return "unknown", None


def _plus_years(timestamp: str, years: int) -> str:
    value = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        value = value.replace(year=value.year + years)
    except ValueError:
        value = value.replace(month=2, day=28, year=value.year + years)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _plus_days(timestamp: str, days: int) -> str:
    value = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    value += datetime.timedelta(days=days)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _plus_seconds(timestamp: str, seconds: int) -> str:
    value = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    value += datetime.timedelta(seconds=seconds)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _id(prefix: str, entropy: Entropy) -> str:
    number = int.from_bytes(entropy.token_bytes(17), "big") & ((1 << 130) - 1)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[number & 31])
        number >>= 5
    return prefix + "".join(reversed(chars))


@dataclass(frozen=True)
class RepositoryLimits:
    repository_quota_bytes: int = REPOSITORY_QUOTA_BYTES
    checkpoint_bytes: int = CHECKPOINT_QUOTA_BYTES
    tombstone_rows: int = TOMBSTONE_ROWS
    tombstone_bytes: int = TOMBSTONE_BYTES
    quarantine_bytes: int = QUARANTINE_BYTES
    audit_rows: int = AUDIT_ROWS
    audit_bytes: int = AUDIT_BYTES
    active_proposals_per_session: int = 500
    revisions_per_lineage: int = 8
    lineages_per_session: int = 10_000
    create_rate_per_minute: int = 10
    create_rate_burst: int = 3
    preview_workers: int = 2
    preview_queue: int = 8
    generation_count: int = MAX_GENERATIONS
    active_log_bytes: int = ACTIVE_LOG_BYTES
    service_wal_nonterminal_files: int = 1_024
    service_wal_retained_files: int = 100_000
    service_wal_bytes: int = 512 * 1024 * 1024
    service_wal_file_bytes: int = 41 * 1024 * 1024

    def __post_init__(self) -> None:
        maxima = {
            "repository_quota_bytes": REPOSITORY_QUOTA_BYTES,
            "checkpoint_bytes": CHECKPOINT_QUOTA_BYTES,
            "tombstone_rows": TOMBSTONE_ROWS,
            "tombstone_bytes": TOMBSTONE_BYTES,
            "quarantine_bytes": QUARANTINE_BYTES,
            "audit_rows": AUDIT_ROWS,
            "audit_bytes": AUDIT_BYTES,
            "active_proposals_per_session": 500,
            "revisions_per_lineage": 8,
            "lineages_per_session": 10_000,
            "create_rate_per_minute": 10,
            "create_rate_burst": 3,
            "preview_workers": 2,
            "preview_queue": 8,
            "generation_count": MAX_GENERATIONS,
              "active_log_bytes": ACTIVE_LOG_BYTES,
            "service_wal_nonterminal_files": 1_024,
            "service_wal_retained_files": 100_000,
            "service_wal_bytes": 512 * 1024 * 1024,
            "service_wal_file_bytes": 41 * 1024 * 1024,
        }
        for item in dataclasses.fields(self):
            value = getattr(self, item.name)
            maximum = maxima[item.name]
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value <= 0 or value > maximum):
                raise RepositoryError(
                    f"repository limit {item.name} must be an integer in 1..{maximum}")

    def to_mapping(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_mapping())


@dataclass(frozen=True)
class AggregateHighWater:
    generation: int
    sequence: int


@dataclass(frozen=True)
class ProposalRepositorySnapshot:
    repository_id: str
    high_waters: Mapping[str, AggregateHighWater]
    records: Mapping[str, tuple[schemas.Wrapper, ...]]
    token: str
    service_intents: tuple[schemas.ServiceMutationIntent, ...] = ()

    def aggregate(self, kind: str) -> tuple[schemas.Wrapper, ...]:
        return self.records[kind]


@dataclass(frozen=True)
class RecoveryItem:
    transaction_id: str
    phase: str
    classification: str


@dataclass(frozen=True)
class RecoveryReport:
    items: tuple[RecoveryItem, ...]

    @property
    def serving_safe(self) -> bool:
        return not any(i.classification in {
            "rollback_required", "rollforward_required", "indeterminate",
            "orphan_history", "unreferenced_private",
        } for i in self.items)


@dataclass(frozen=True)
class Reconciliation:
    state: str
    endpoint_result: schemas.EndpointResult | None = None
    tombstone: schemas.Tombstone | None = None
    transaction_id: str | None = None


@dataclass(frozen=True)
class EndpointPreparation:
    """Durable endpoint prepare authority plus server-owned row identity."""

    reconciliation: Reconciliation
    result_record_id: str
    proposal_id: str | None = None
    proposal_revision: int | None = None


@dataclass
class TransactionAdmissionFence:
    """Process-local state changed only while the repository lock is held."""

    state: str = "open"


_LOCK_RANKS = {
    "repository": 0,
    "bundle": 1,
    "concept": 2,
    "lineage": 3,
    "endpoint": 4,
    "transaction": 5,
    "history": 6,
    "activity_outbox": 7,
}
_RANK_STATE = threading.local()


@dataclass
class RepositoryTransactionSession:
    """One repository lease plus explicit monotonic lock-rank enforcement.

    The coordinator acquires the repository lease first and uses ``ranked``
    around external bundle/concept/Studio locks.  A rank may be acquired once;
    neither inversion nor re-acquisition is accepted.
    """

    repository: "ProposalRepositoryV1"
    _entered: bool = False
    _held: Any = None

    def __enter__(self) -> "RepositoryTransactionSession":
        if getattr(_RANK_STATE, "active", False):
            raise RepositoryError("repository transaction session cannot be nested", code="lock_order_violation")
        # A distinct repository-wide lease is held for the complete content
        # transaction.  Short append/rotation critical sections remain free to
        # use ``.append.lock`` without recursively flocking the same open-file
        # description.
        self._held = self.repository._fs.lock(".transaction-session.lock").hold(
            self.repository._lock_timeout)
        self._held.__enter__()
        _RANK_STATE.active = True
        _RANK_STATE.highest = 0
        _RANK_STATE.seen = {0}
        self._entered = True
        return self

    def __exit__(self, typ: Any, value: Any, tb: Any) -> None:
        try:
            if self._held is not None:
                self._held.__exit__(typ, value, tb)
        finally:
            _RANK_STATE.active = False
            _RANK_STATE.highest = -1
            _RANK_STATE.seen = set()
            self._entered = False

    @contextlib.contextmanager
    def ranked(self, name: str):
        if not self._entered or not getattr(_RANK_STATE, "active", False):
            raise RepositoryError("ranked lock requires an active repository transaction session",
                                  code="lock_order_violation")
        if name not in _LOCK_RANKS or name == "repository":
            raise RepositoryError("unknown or reserved transaction lock rank", code="lock_order_violation")
        rank = _LOCK_RANKS[name]
        seen = _RANK_STATE.seen
        if rank <= _RANK_STATE.highest or rank in seen:
            raise RepositoryError("transaction lock rank inversion or re-acquisition",
                                  code="lock_order_violation")
        _RANK_STATE.highest = rank
        seen.add(rank)
        yield

    @contextlib.contextmanager
    def locked(self, name: str, identity: str):
        """Acquire one cross-process lock at the declared next rank."""
        digest = __import__("hashlib").sha256(identity.encode("utf-8")).hexdigest()
        with self.ranked(name):
            with self.repository._fs.lock(f".transaction-locks/{name}-{digest}.lock").hold(
                    self.repository._lock_timeout):
                yield


@dataclass
class _AggregateState:
    generation: int = 0
    sequence: int = 0
    records: list[schemas.Wrapper] = field(default_factory=list)
    blocked: bool = False


class ProposalRepositoryV1:
    """Sole owner of every path below ``proposal-review-v1``."""

    def __init__(
        self,
        root: FilesystemRoot,
        repository_root: schemas.RepositoryRoot,
        *,
        clock: Clock,
        entropy: Entropy,
        limits: RepositoryLimits,
        rotation_bytes: int,
        lock_timeout_seconds: float,
        activity_link_verifier: ActivityLinkVerifier | None = None,
        fault_injector: Any | None = None,
    ) -> None:
        self._fs = root
        self.root_authority = repository_root
        self._clock = clock
        self._entropy = entropy
        self._limits = limits
        self._rotation_bytes = rotation_bytes
        self._lock_timeout = lock_timeout_seconds
        self._lock = root.lock()
        self._states = {k: _AggregateState() for k in AGGREGATES}
        self._recovery = RecoveryReport(())
        self._stopped = False
        self._activity_link_verifier = activity_link_verifier
        self._fault_injector = fault_injector
        self._preview_owner_pid = os.getpid()
        status, token = _linux_process_identity(self._preview_owner_pid)
        if status != "alive" or token is None:
            raise RepositoryError("safe process liveness identity is unavailable",
                                  code="storage_unsafe")
        self._preview_owner_start_token = token

    def close(self) -> None:
        """Release this repository's owned pinned filesystem root."""
        close = getattr(self._fs, "close", None)
        if close is not None:
            close()

    @classmethod
    def bootstrap(
        cls,
        session_root: FilesystemRoot,
        *,
        clock: Clock | None = None,
        entropy: Entropy | None = None,
        limits: RepositoryLimits | None = None,
        rotation_bytes: int = ACTIVE_LOG_BYTES,
        lock_timeout_seconds: float = 15.0,
        activity_link_verifier: ActivityLinkVerifier | None = None,
        fault_injector: Any | None = None,
    ) -> "ProposalRepositoryV1":
        """Create or open under a caller-contained session-root adapter."""
        clock = clock or SystemClock()
        entropy = entropy or SystemEntropy()
        limits = limits or RepositoryLimits()
        if rotation_bytes <= 0 or rotation_bytes > limits.active_log_bytes:
            raise RepositoryError(
                f"rotation threshold must be in 1..{limits.active_log_bytes} bytes")
        if lock_timeout_seconds < 0 or lock_timeout_seconds > 15:
            raise RepositoryError("lock timeout must be in 0..15 seconds")
        root = session_root.subroot(REPOSITORY_DIR, create=True)
        try:
            for directory in ("active", "checkpoints", "generations", "previews", "previews/sha256", "transactions", "quarantine", "service-wal", "preview-staging"):
                root.mkdirs(directory)
            if not root.exists(".gitignore"):
                root.write_atomic(".gitignore", b"*\n")
            with root.lock().hold(lock_timeout_seconds):
                initialized = root.exists("repository.json")
                if initialized:
                    authority = cls._read_root(root, limits, clock)
                else:
                    authority = cls._create_root(root, limits, clock, entropy)
                for aggregate in AGGREGATES:
                    path = cls._active_path(aggregate)
                    if not root.exists(path):
                        if initialized:
                            raise RepositoryCorruptError(
                                f"initialized repository is missing active aggregate {aggregate}")
                        root.write_atomic(path, b"")
            repo = cls(root, authority, clock=clock, entropy=entropy, limits=limits,
                         rotation_bytes=rotation_bytes, lock_timeout_seconds=lock_timeout_seconds,
                         activity_link_verifier=activity_link_verifier, fault_injector=fault_injector)
            repo._startup_recovery()
            return repo
        except BaseException:
            close = getattr(root, "close", None)
            if close is not None:
                close()
            raise

    @classmethod
    def open(cls, session_root: FilesystemRoot, **kwargs: Any) -> "ProposalRepositoryV1":
        repo_root = session_root.subroot(REPOSITORY_DIR, create=False)
        if not repo_root.exists("repository.json"):
            raise RepositoryCorruptError("repository root authority is missing")
        return cls.bootstrap(session_root, **kwargs)

    @staticmethod
    def _create_root(root: FilesystemRoot, limits: RepositoryLimits, clock: Clock, entropy: Entropy) -> schemas.RepositoryRoot:
        now = validate_timestamp(clock.now())
        key_id = _id("cur_", entropy)
        secret = base64.urlsafe_b64encode(entropy.token_bytes(32)).rstrip(b"=").decode("ascii")
        obj: dict[str, Any] = {
            "schema": 1,
            "repository_id": _id("repo_", entropy),
            "created_at": now,
            "aggregate_versions": {k: 1 for k in AGGREGATES},
            "cursor_keys": [{"key_id": key_id, "secret_base64url": secret,
                             "created_at": now, "not_before": now,
                             "expires_at": _plus_years(now, 10)}],
            "active_cursor_key_id": key_id,
            "limits_digest": limits.digest,
        }
        obj["checksum"] = compute_checksum(obj)
        authority = schemas.RepositoryRoot.from_mapping(obj)
        root.write_atomic("repository.json", authority.canonical_bytes())
        return schemas.RepositoryRoot.from_json(root.read_bytes("repository.json"))  # type: ignore[return-value]

    @staticmethod
    def _read_root(root: FilesystemRoot, limits: RepositoryLimits, clock: Clock) -> schemas.RepositoryRoot:
        try:
            authority = schemas.RepositoryRoot.from_json(root.read_bytes("repository.json", max_bytes=1024 * 1024))
        except Exception as exc:
            raise RepositoryCorruptError("repository root authority failed validation") from exc
        if authority.limits_digest != limits.digest:
            raise RepositoryCorruptError("repository limits digest mismatch")
        now = validate_timestamp(clock.now())
        active = [k for k in authority.cursor_keys if k.key_id == authority.active_cursor_key_id]
        if len(active) != 1 or not (active[0].not_before <= now < active[0].expires_at):
            raise RepositoryCorruptError("active cursor key is not currently valid")
        ids = [k.key_id for k in authority.cursor_keys]
        if len(ids) != len(set(ids)):
            raise RepositoryCorruptError("duplicate cursor key identity")
        return authority

    @staticmethod
    def _active_path(aggregate: str) -> str:
        return f"active/{_DISK_NAME[aggregate]}.jsonl"

    @staticmethod
    def _generation_path(aggregate: str, generation: int) -> str:
        return f"generations/{_DISK_NAME[aggregate]}-{generation:020d}.jsonl"

    @staticmethod
    def _checkpoint_path(aggregate: str) -> str:
        return f"checkpoints/{_DISK_NAME[aggregate]}.json"

    @property
    def repository_id(self) -> str:
        return self.root_authority.repository_id

    @property
    def limits(self) -> RepositoryLimits:
        """Read-only view of the root-digest-bound immutable limits."""
        return self._limits

    @property
    def recovery_report(self) -> RecoveryReport:
        return self._recovery

    def transaction_session(self) -> RepositoryTransactionSession:
        """Return the mandatory first-rank repository transaction lease."""
        return RepositoryTransactionSession(self)

    def new_transaction_admission_fence(self) -> TransactionAdmissionFence:
        return TransactionAdmissionFence()

    def cancel_transaction_admission(self, fence: TransactionAdmissionFence) -> str:
        """Atomically cancel an unadmitted worker or observe durable admission."""
        with self._lock.hold(self._lock_timeout):
            if fence.state == "open":
                fence.state = "cancelled"
            if fence.state not in {"cancelled", "admitted"}:
                raise RepositoryCorruptError("invalid transaction admission fence state")
            return fence.state

    def refresh_recovery_report(self) -> RecoveryReport:
        with self._lock.hold(self._lock_timeout):
            wal = self._classify_wals_locked()
            associations = self._verify_transaction_associations_locked()
            self._recovery = RecoveryReport(tuple(sorted(
                wal.items + associations,
                key=lambda item: (item.transaction_id, item.classification),
            )))
            return self._recovery

    def mark_transaction_corrupt(self, transaction_id: str, reason: str,
                                 observed: bytes = b"") -> schemas.Wrapper:
        """Append a transaction-file corruption marker and block mutation."""
        from .ids import validate_transaction_id_any
        validate_transaction_id_any(transaction_id)
        if reason not in schemas.CORRUPTION_REASONS:
            reason = "authority_contradiction"
        with self._lock.hold(self._lock_timeout):
            return self._mark_transaction_corrupt_locked(transaction_id, reason, observed)

    def _mark_transaction_corrupt_locked(self, transaction_id: str, reason: str,
                                         observed: bytes = b"") -> schemas.Wrapper:
        state, _faults = self._scan_aggregate_locked("corruption")
        existing = [row for row in state.records if row.kind == "corruption"
                    and row.payload.get("authority_kind") == "transaction_file"
                    and (row.payload.get("authority_identity") or {}).get("transaction_id")
                    == transaction_id and row.payload.get("repaired_at") is None]
        if existing:
            if len(existing) != 1:
                raise RepositoryCorruptError("duplicate active transaction corruption markers")
            marker = existing[0]
        else:
            body: dict[str, Any] = {"schema": 1, "corruption_id": self.new_id("cor_"),
                "repository_id": self.repository_id, "authority_kind": "transaction_file",
                "authority_identity": {"kind": "transaction_file", "transaction_id": transaction_id,
                    "component": "wal", "relative_path": f"transactions/{transaction_id}/wal.json"},
                "reason": reason, "observed_digest": ("sha256:" + hashlib.sha256(observed).hexdigest()) if observed else None,
                "diagnostic_copy": None, "detected_at": self._clock.now(), "repaired_at": None,
                "repair_record_id": None}
            body["checksum"] = compute_checksum(body)
            marker = self._append_locked("corruption", "corruption", body, permit_blocked=True)
        retained = [item for item in self._recovery.items
                    if item.transaction_id != transaction_id]
        retained.append(RecoveryItem(transaction_id, "indeterminate", "indeterminate"))
        self._recovery = RecoveryReport(tuple(sorted(
            retained, key=lambda item: (item.transaction_id, item.classification))))
        return marker

    def cleanup_unreferenced_transaction(self, transaction_id: str) -> bool:
        """Delete only a provably authority-free pre-WAL transaction's files."""
        if (not getattr(_RANK_STATE, "active", False)
                or getattr(_RANK_STATE, "highest", -1) < _LOCK_RANKS["activity_outbox"]):
            raise RepositoryError("pre-WAL cleanup requires the complete recovery lock envelope",
                                  code="lock_order_violation")
        from .ids import validate_transaction_id_any
        validate_transaction_id_any(transaction_id)
        with self._lock.hold(self._lock_timeout):
            wal_path = f"transactions/{transaction_id}/wal.json"
            if self._fs.exists(wal_path): return False
            for aggregate in ("transactions", "endpoint_results", "tombstones", "outbox"):
                state, faults = self._scan_aggregate_locked(aggregate)
                if faults: return False
                if any(row.payload.get("transaction_id") == transaction_id
                       or row.payload.get("mutation_intent_id") == transaction_id
                       for row in state.records): return False
            prefix = f"transactions/{transaction_id}/"
            root = f"transactions/{transaction_id}"
            try:
                top = set(self._fs.list_dir(root))
                if not top <= {"stage", "backup", "intent.json", "history.json"}:
                    return False
                files = tuple(p for p in self._fs.walk_files(root) if p.startswith(prefix))
            except Exception:
                return False
            allowed_files: set[str] = set()
            for directory in ("stage", "backup"):
                if directory not in top:
                    continue
                try:
                    names = self._fs.list_dir(f"{root}/{directory}")
                except Exception:
                    return False
                for name in names:
                    if not re.fullmatch(r"[0-9]{6}\.bin", name):
                        return False
                    allowed_files.add(f"{root}/{directory}/{name}")
            allowed_files.update(f"{root}/{name}" for name in ("intent.json", "history.json")
                                 if name in top)
            if set(files) != allowed_files:
                return False
            # Once intent exists it is immutable prepare identity. Every known
            # private leaf must still be the exact inode/bytes assigned there.
            if "intent.json" in top:
                try:
                    from .transaction_intent import TransactionFinalizationIntent
                    intent = TransactionFinalizationIntent.from_json(
                        self._fs.read_bytes(f"{root}/intent.json",
                                            max_bytes=self.limits.checkpoint_bytes))
                    expected: dict[str, Mapping[str, Any]] = {}
                    for index, target in enumerate(intent.value["targets"]):
                        staged = target["staged_identity"]
                        expected[f"{root}/stage/{index:06d}.bin"] = {
                            "device": staged["leaf_device"], "inode": staged["leaf_inode"],
                            "mode": staged["mode"], "size": staged["size"],
                            "digest": staged["digest"]}
                        expected[f"{root}/backup/{target['backup_identity']['relative_name']}"] = target["backup_identity"]
                    if not set(expected) <= set(files):
                        return False
                    for path, identity in expected.items():
                        actual = self._fs.file_identity(path)
                        wanted = {key: identity[key] for key in
                                  ("device", "inode", "mode", "size", "digest")}
                        observed = {key: actual[key] for key in wanted}
                        if observed != wanted:
                            return False
                except Exception:
                    return False
            for path in sorted(files, reverse=True):
                self._fs.unlink(path)
            for directory in (f"{root}/stage", f"{root}/backup", root):
                if self._fs.exists(directory):
                    self._fs.rmdir(directory)
            return True

    def _startup_recovery(self) -> None:
        faults: list[tuple[str, int | None, int | None, str, str, bytes]] = []
        with self._lock.hold(self._lock_timeout):
            for aggregate in AGGREGATES:
                try:
                    state, aggregate_faults = self._scan_aggregate_locked(aggregate)
                    self._states[aggregate] = state
                    faults.extend(aggregate_faults)
                except RepositoryCorruptError:
                    raise
            for aggregate in AGGREGATES:
                checkpoint_fault = self._verify_checkpoint_locked(aggregate)
                if checkpoint_fault is not None:
                    faults.append(checkpoint_fault)
            existing_markers = self._current_corruption_markers()
            for aggregate, generation, sequence, path, reason, observed in faults:
                identity_key = (aggregate, generation, sequence, path, reason)
                if identity_key not in existing_markers:
                    self._append_corruption_locked(aggregate, generation, sequence, path, reason, observed)
                self._states[aggregate].blocked = True
            self._closed_repair_classifications_locked()
            self._recovery = self._classify_wals_locked()
            association_recovery = self._verify_transaction_associations_locked()
            self._recovery = RecoveryReport(tuple(sorted(
                self._recovery.items + association_recovery,
                key=lambda item: (item.transaction_id, item.classification),
            )))
            self._recover_service_intents_locked()
            self._prune_service_wal_retention_locked()

    def _verify_checkpoint_locked(self, aggregate: str) -> tuple[str, int | None, int | None, str, str, bytes] | None:
        path = self._checkpoint_path(aggregate)
        generations = self._generation_files(aggregate)
        if not self._fs.exists(path):
            if generations:
                return (aggregate, None, None, path, "missing", b"")
            return None
        raw = b""
        try:
            raw = self._fs.read_bytes(path, max_bytes=self.limits.checkpoint_bytes)
            checkpoint = schemas.Checkpoint.from_json(raw)
            if checkpoint.repository_id != self.repository_id or checkpoint.kind != aggregate:
                raise IntegrityError("checkpoint repository/kind mismatch")
            state = self._states[aggregate]
            if checkpoint.sequence_high_water > state.sequence:
                raise IntegrityError("checkpoint high-water exceeds aggregate authority")
            covered = list(checkpoint.covered_generations)
            if covered and covered != list(range(covered[0], covered[-1] + 1)):
                raise IntegrityError("checkpoint covered generations are not contiguous")
            if covered and covered[-1] > state.generation:
                raise IntegrityError("checkpoint covers a future aggregate generation")
            # Validate every checkpoint-contained wrapper exactly like JSONL:
            # wrapper checksum, aggregate-kind topology, closed payload parser,
            # and contiguous sequence before trusting it as a seed (Fix #2).
            checkpoint_wrappers: list[schemas.Wrapper] = []
            cp_expected = 1
            for r in checkpoint.records:
                cp_mapping = r if isinstance(r, dict) else dict(r)
                cp_wrapper = schemas.Wrapper.from_mapping(cp_mapping)
                if _KIND_AGGREGATE.get(cp_wrapper.kind) != aggregate:
                    raise IntegrityError("checkpoint wrapper topology mismatch")
                if cp_wrapper.sequence != cp_expected:
                    raise IntegrityError("checkpoint sequence is not contiguous")
                _PAYLOAD_PARSER[cp_wrapper.kind].from_mapping(cp_wrapper.payload)
                checkpoint_wrappers.append(cp_wrapper)
                cp_expected += 1
            expected = [w for w in state.records if w.sequence <= checkpoint.sequence_high_water]
            if checkpoint_wrappers != expected:
                raise IntegrityError("checkpoint records do not match exact high-water prefix")
        except Exception:
            return (aggregate, None, None, path, "checksum_mismatch", raw)
        return None

    def _generation_files(self, aggregate: str) -> list[tuple[int, str]]:
        wanted = _DISK_NAME[aggregate]
        out: list[tuple[int, str]] = []
        for name in self._fs.list_dir("generations"):
            match = _GEN_RE.fullmatch(name)
            if match and match.group(1) == wanted:
                out.append((int(match.group(2)), f"generations/{name}"))
        out.sort()
        return out

    def _scan_aggregate_locked(self, aggregate: str) -> tuple[_AggregateState, list[tuple[str, int | None, int | None, str, str, bytes]]]:
        files = self._generation_files(aggregate)
        generations = [g for g, _ in files]
        faults: list[tuple[str, int | None, int | None, str, str, bytes]] = []
        if generations and generations != list(range(generations[0], generations[-1] + 1)):
            faults.append((aggregate, None, None, "generations", "sequence_gap", dumps_canonical(generations)))
        ordered = files + [(None, self._active_path(aggregate))]  # type: ignore[list-item]
        expected = 1
        records: list[schemas.Wrapper] = []
        # Track which sequences were seeded from the checkpoint (vs JSONL).
        cp_seeded: set[int] = set()
        checkpoint_path = self._checkpoint_path(aggregate)
        cp_raw: bytes | None = None
        if self._fs.exists(checkpoint_path):
            try:
                cp_raw = self._fs.read_bytes(
                    checkpoint_path, max_bytes=self.limits.checkpoint_bytes + 1
                )
                checkpoint = schemas.Checkpoint.from_json(cp_raw)
                if checkpoint.repository_id == self.repository_id and checkpoint.kind == aggregate:
                    # Validate every checkpoint-contained wrapper exactly like
                    # JSONL: wrapper checksum, aggregate-kind topology, closed
                    # payload parser, contiguous sequence, duplicate/identity
                    # contradictions (SPEC section 5.7 / 9 checkpoint trust).
                    cp_records: list[schemas.Wrapper] = []
                    cp_expected = 1
                    for r in checkpoint.records:
                        cp_mapping = r if isinstance(r, dict) else dict(r)
                        cp_line = dumps_canonical(cp_mapping)
                        cp_wrapper = schemas.Wrapper.from_mapping(cp_mapping)
                        if dumps_canonical(cp_mapping) != cp_line:
                            raise IntegrityError("checkpoint wrapper is not canonical")
                        if cp_wrapper.sequence != cp_expected:
                            raise IntegrityError("checkpoint sequence is not contiguous")
                        if _KIND_AGGREGATE.get(cp_wrapper.kind) != aggregate:
                            raise IntegrityError("checkpoint wrapper topology mismatch")
                        _PAYLOAD_PARSER[cp_wrapper.kind].from_mapping(cp_wrapper.payload)
                        cp_records.append(cp_wrapper)
                        cp_expected += 1
                    records = cp_records
                    if len(cp_records) != checkpoint.sequence_high_water:
                        raise IntegrityError("checkpoint is not the exact high-water prefix")
                    for w in cp_records:
                        cp_seeded.add(w.sequence)
                    expected = checkpoint.sequence_high_water + 1
            except Exception:
                # Checkpoint is corrupt: fall back to JSONL-only reconstruction.
                records = []
                expected = 1
        for generation, path in ordered:
            raw = self._fs.read_bytes(path, max_bytes=self.limits.repository_quota_bytes)
            if raw and not raw.endswith(b"\n"):
                if generation is None:
                    cut = raw.rfind(b"\n") + 1
                    self._fs.truncate_fsync(path, cut)
                    raw = raw[:cut]
                else:
                    faults.append((aggregate, generation, None, path, "malformed", raw))
                    continue
            for line in raw.splitlines():
                if not line:
                    faults.append((aggregate, generation, expected, path, "malformed", line))
                    continue
                try:
                    mapping = loads_canonical(line)
                    wrapper = schemas.Wrapper.from_mapping(mapping)
                    if dumps_canonical(mapping) != line:
                        raise SchemaError("JSONL row is not canonical")
                    if wrapper.sequence != expected:
                        if wrapper.sequence < expected:
                            if wrapper.sequence in cp_seeded:
                                # Checkpoint/generation overlap: the generation
                                # file contains the same sequences as the
                                # validated checkpoint.  Accept byte-identical
                                # overlap; fault any contradiction.
                                existing = records[wrapper.sequence - 1]
                                if existing != wrapper:
                                    faults.append((aggregate, generation,
                                                   wrapper.sequence, path,
                                                   "authority_contradiction", line))
                                continue
                            # Per normative spec, any duplicate sequence — even
                            # byte-identical — within the JSONL logs creates
                            # corruption; do not silently dedupe (SPEC section 9).
                            reason = "duplicate_identity"
                        else:
                            reason = "sequence_gap"
                        faults.append((aggregate, generation, wrapper.sequence, path,
                                       reason, line))
                        continue
                    if _KIND_AGGREGATE[wrapper.kind] != aggregate:
                        faults.append((aggregate, generation, wrapper.sequence, path, "type_mismatch", line))
                        continue
                    _PAYLOAD_PARSER[wrapper.kind].from_mapping(wrapper.payload)
                except IntegrityError:
                    faults.append((aggregate, generation, expected, path, "checksum_mismatch", line))
                    continue
                except Exception:
                    faults.append((aggregate, generation, expected, path, "malformed", line))
                    continue
                records.append(wrapper)
                expected += 1
        state = _AggregateState(generation=generations[-1] if generations else 0,
                                sequence=expected - 1, records=records, blocked=bool(faults))
        return state, faults

    def _current_corruption_markers(self) -> set[tuple[Any, ...]]:
        markers: set[tuple[Any, ...]] = set()
        for wrapper in self._states["corruption"].records:
            if wrapper.kind != "corruption":
                continue
            marker = schemas.CorruptionMarker.from_mapping(wrapper.payload)
            identity = marker.authority_identity
            if getattr(identity, "kind", None) == "aggregate":
                markers.add((identity.aggregate, identity.generation, identity.sequence,
                             identity.relative_path, marker.reason))
            elif getattr(identity, "kind", None) == "checkpoint":
                markers.add((identity.aggregate, None, None, identity.relative_path,
                             marker.reason))
        return markers

    def _append_corruption_locked(self, aggregate: str, generation: int | None, sequence: int | None,
                                  path: str, reason: str, observed: bytes) -> schemas.Wrapper:
        if aggregate == "corruption" and self._states["corruption"].blocked:
            raise RepositoryCorruptError("corruption authority itself is corrupt")
        if path.startswith("checkpoints/"):
            authority_kind = "checkpoint"
            authority_identity: dict[str, Any] = {
                "kind": "checkpoint", "aggregate": aggregate,
                "generation": max(0, self._states[aggregate].generation),
                "relative_path": path,
            }
        else:
            authority_kind = "aggregate"
            authority_identity = {"kind": "aggregate", "aggregate": aggregate,
                                  "generation": generation,
                                  "sequence": sequence if generation is not None else None,
                                  "relative_path": path}
        payload: dict[str, Any] = {
            "schema": 1, "corruption_id": _id("cor_", self._entropy),
            "repository_id": self.repository_id, "authority_kind": authority_kind,
            "authority_identity": authority_identity,
            "reason": reason,
            "observed_digest": None,
            "diagnostic_copy": self._quarantine_copy_locked(observed),
            "detected_at": self._clock.now(), "repaired_at": None,
            "repair_record_id": None,
        }
        # observed_digest hashes the exact bytes read, not a JSON projection.
        if observed or reason not in schemas.NULL_DIGEST_REASONS:
            import hashlib
            payload["observed_digest"] = "sha256:" + hashlib.sha256(observed).hexdigest()
        payload["checksum"] = compute_checksum(payload)
        return self._append_locked("corruption", "corruption", payload, permit_blocked=True)

    def _quarantine_copy_locked(self, observed: bytes) -> dict[str, Any] | None:
        if not observed:
            return None
        import hashlib
        digest = "sha256:" + hashlib.sha256(observed).hexdigest()
        path = f"quarantine/{digest[7:]}.bin"
        if not self._fs.exists(path):
            used = sum(self._fs.file_size(p) for p in self._fs.walk_files("quarantine"))
            if used + len(observed) > self.limits.quarantine_bytes:
                return None
            self._ensure_replacement_quota_locked(path, observed)
            self._fs.write_atomic(path, observed)
        return {"relative_path": path, "digest": digest, "size": len(observed)}

    def _classify_wals_locked(self) -> RecoveryReport:
        items: list[RecoveryItem] = []
        marked = {row.payload.get("authority_identity", {}).get("transaction_id")
                  for row in self._states["corruption"].records
                  if row.kind == "corruption" and row.payload.get("repaired_at") is None
                  and row.payload.get("authority_kind") == "transaction_file"}
        for name in self._fs.list_dir("transactions"):
            path = f"transactions/{name}/wal.json"
            if not self._fs.exists(path):
                continue
            try:
                wal = schemas.WAL.from_json(self._fs.read_bytes(path, max_bytes=64 * 1024 * 1024))
            except Exception as exc:
                raise RepositoryCorruptError("transaction WAL failed startup validation") from exc
            if wal.transaction_id != name:
                raise RepositoryCorruptError("transaction directory/WAL identity mismatch")
            if wal.transaction_id in marked:
                classification = "indeterminate"
            elif wal.phase in {"prepared", "rolling_back"}:
                classification = "rollback_required"
            elif wal.phase in {"commit_decided", "publishing", "content_published", "finalizing"}:
                classification = "rollforward_required"
            elif wal.phase == "indeterminate":
                classification = "indeterminate"
            else:
                classification = "terminal"
            items.append(RecoveryItem(wal.transaction_id, wal.phase, classification))
        return RecoveryReport(tuple(sorted(items, key=lambda i: i.transaction_id)))

    def _verify_transaction_associations_locked(self) -> tuple[RecoveryItem, ...]:
        audits = [
            schemas.TransactionAudit.from_mapping(w.payload)
            for w in self._states["transactions"].records
            if w.kind == "transaction"
        ]
        by_id: dict[str, list[schemas.TransactionAudit]] = {}
        for audit in audits:
            by_id.setdefault(audit.transaction_id, []).append(audit)
        for transaction_id, rows in by_id.items():
            if len(rows) != 1:
                raise RepositoryCorruptError("duplicate transaction audit identity")
            path = f"transactions/{transaction_id}/history.json"
            if not self._fs.exists(path) and rows[0].status in {"applied", "undone"}:
                raise RepositoryCorruptError("transaction audit history association is missing")
            if self._fs.exists(path):
                self.verify_history_link(rows[0])
        recovery: list[RecoveryItem] = []
        for name in self._fs.list_dir("transactions"):
            history_path = f"transactions/{name}/history.json"
            wal_exists = self._fs.exists(f"transactions/{name}/wal.json")
            if not self._fs.exists(history_path):
                if name not in by_id and not wal_exists:
                    if self._fs.walk_files(f"transactions/{name}"):
                        recovery.append(RecoveryItem(name, "pre_wal", "unreferenced_private"))
                continue
            try:
                history = schemas.HistoryManifest.from_json(
                    self._fs.read_bytes(history_path, max_bytes=self.limits.checkpoint_bytes))
            except Exception as exc:
                if wal_exists:
                    self._mark_transaction_corrupt_locked(
                        name, "authority_contradiction", str(exc).encode("utf-8")[:4096])
                    recovery.append(RecoveryItem(name, "indeterminate", "indeterminate"))
                    continue
                raise RepositoryCorruptError("history manifest failed startup validation") from exc
            if history.transaction_id != name:
                if wal_exists:
                    self._mark_transaction_corrupt_locked(
                        name, "authority_contradiction", b"history identity mismatch")
                    recovery.append(RecoveryItem(name, "indeterminate", "indeterminate"))
                    continue
                raise RepositoryCorruptError("transaction directory/history identity mismatch")
            if name not in by_id and not wal_exists:
                recovery.append(RecoveryItem(name, "pre_wal", "orphan_history"))
        return tuple(recovery)

    def _service_wrapper_bytes(self, kind: str, payload: Mapping[str, Any]) -> int:
        body = {"record_schema": 1, "kind": kind, "sequence": (1 << 53) - 1,
                "payload": dict(payload)}
        body["checksum"] = compute_checksum(body)
        return len(dumps_canonical(body)) + 1

    def _service_remaining_bytes(self, intent: schemas.ServiceMutationIntent) -> int:
        if intent.phase in {"committed", "indeterminate"}:
            return 0
        permanent = 0
        framing = 0
        if intent.preview is not None:
            digest = intent.preview["promoted_ref"]["preview_digest"][7:]
            promoted = f"previews/sha256/{digest[:2]}/{digest[2:]}.json"
            if not self._fs.exists(promoted):
                permanent += intent.preview["promoted_ref"]["artifact_bytes"]
        if intent.phase == "prepared":
            prepared = {"schema": 1, "record_id": intent.assigned["result_record_id"],
                "scope": intent.scope, "key_digest": intent.key_digest,
                "request_fingerprint": intent.request_fingerprint, "state": "prepared",
                "http_status": None, "response_digest": None, "response": None,
                "transaction_id": None, "created_at": intent.assigned["created_at"],
                "expires_at": intent.assigned["expires_at"]}
            prepared["checksum"] = compute_checksum(prepared)
            size = self._service_wrapper_bytes("endpoint_result", prepared)
            permanent += size; framing += size
        if intent.phase == "prepared":
            size = self._service_wrapper_bytes(intent.domain["kind"], intent.domain["row"])
            permanent += size; framing += size
        if intent.phase in {"prepared", "domain_appended"}:
            size = self._service_wrapper_bytes("outbox", intent.outbox.to_mapping())
            permanent += size; framing += size
        if intent.phase in {"prepared", "domain_appended", "outbox_appended"}:
            terminal = {"schema": 1, "record_id": intent.assigned["result_record_id"],
                "scope": intent.scope, "key_digest": intent.key_digest,
                "request_fingerprint": intent.request_fingerprint, "state": "terminal",
                "http_status": intent.terminal_http_status,
                "response_digest": intent.terminal_response_digest,
                "response": dict(intent.terminal_response), "transaction_id": None,
                "created_at": intent.assigned["created_at"], "expires_at": intent.assigned["expires_at"]}
            terminal["checksum"] = compute_checksum(terminal)
            size = self._service_wrapper_bytes("endpoint_result", terminal)
            permanent += size; framing += size
        # Atomic replacement needs one complete temporary WAL copy. Rotation or
        # checkpoint publication needs one complete framed copy of all rows
        # still owed; only the larger temporary peak can coexist with permanent
        # final bytes at a single boundary.
        temporary_peak = max(len(intent.canonical_bytes()), framing + 4096)
        return permanent + temporary_peak

    def _reserved_service_bytes_locked(self) -> int:
        return sum(self._service_remaining_bytes(intent)
                   for intent in self._read_service_intents_locked())

    def _ensure_quota_locked(self, additional: int, *, consume_service_reservation: bool = False) -> None:
        used = sum(self._fs.file_size(path) for path in self._fs.walk_files())
        reserved = 0 if consume_service_reservation else self._reserved_service_bytes_locked()
        if used + additional + reserved > self.limits.repository_quota_bytes:
            raise RepositoryQuotaError("proposal repository aggregate quota exceeded")

    def _fault(self, boundary: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(boundary)

    @staticmethod
    def _service_intent_path(intent_id: str) -> str:
        return f"service-wal/{intent_id}.json"

    def _read_service_intents_locked(self) -> list[schemas.ServiceMutationIntent]:
        names = self._fs.list_dir("service-wal")
        if len(names) > self.limits.service_wal_retained_files:
            raise RepositoryQuotaError("service WAL retained-file quota exceeded")
        total = 0
        intents: list[schemas.ServiceMutationIntent] = []
        for name in names:
            if not name.startswith("svc_") or not name.endswith(".json"):
                raise RepositoryCorruptError("service WAL contains a noncanonical filename")
            path = f"service-wal/{name}"
            size = self._fs.file_size(path); total += size
            if size > self.limits.service_wal_file_bytes or total > self.limits.service_wal_bytes:
                raise RepositoryQuotaError("service WAL byte quota exceeded")
            try:
                intent = schemas.ServiceMutationIntent.from_json(
                    self._fs.read_bytes(path, max_bytes=self.limits.service_wal_file_bytes))
            except Exception as exc:
                raise RepositoryCorruptError("service WAL intent failed validation") from exc
            if path != self._service_intent_path(intent.intent_id):
                raise RepositoryCorruptError("service WAL path/identity mismatch")
            intents.append(intent)
        if len({i.intent_id for i in intents}) != len(intents) or len({(i.scope, i.key_digest) for i in intents}) != len(intents):
            raise RepositoryCorruptError("duplicate service WAL identity")
        return intents

    def _write_service_intent_locked(self, intent: schemas.ServiceMutationIntent,
                                     previous: schemas.ServiceMutationIntent | None = None) -> schemas.ServiceMutationIntent:
        if previous is not None:
            order = {name: i for i, name in enumerate(schemas.SERVICE_INTENT_PHASES)}
            if intent.intent_id != previous.intent_id or order[intent.phase] < order[previous.phase]:
                raise RepositoryCorruptError("service intent phase regressed")
            mutable = {"phase", "recovery_attempts", "updated_at", "checksum"}
            if any(getattr(intent, name) != getattr(previous, name)
                   for name in schemas.ServiceMutationIntent.FIELDS - mutable):
                raise RepositoryCorruptError("service intent immutable authority changed")
        data = intent.canonical_bytes()
        if len(data) > self.limits.service_wal_file_bytes:
            raise RepositoryQuotaError("service WAL intent exceeds per-file quota")
        path = self._service_intent_path(intent.intent_id)
        self._ensure_replacement_quota_locked(path, data, consume_service_reservation=True)
        self._fs.write_atomic(path, data)
        self._fault(f"service_intent_{intent.phase}")
        reread = schemas.ServiceMutationIntent.from_json(
            self._fs.read_bytes(path, max_bytes=self.limits.service_wal_file_bytes))
        if reread != intent:
            raise RepositoryCorruptError("service intent replacement verification failed")
        return reread

    def _advance_service_intent_locked(self, intent: schemas.ServiceMutationIntent,
                                       phase: str) -> schemas.ServiceMutationIntent:
        body = intent.to_mapping(); body["phase"] = phase
        body["updated_at"] = validate_timestamp(self._clock.now())
        body["checksum"] = compute_checksum(body)
        return self._write_service_intent_locked(
            schemas.ServiceMutationIntent.from_mapping(body), intent)

    def stage_service_preview(self, intent_id: str, preview: schemas.Preview) -> Mapping[str, Any]:
        from .ids import validate_svc_id
        validate_svc_id(intent_id)
        data = preview.canonical_bytes(); path = f"preview-staging/{intent_id}/preview.json"
        with self._lock.hold(self._lock_timeout):
            self._fs.mkdirs(f"preview-staging/{intent_id}")
            self._ensure_replacement_quota_locked(path, data)
            self._fs.write_atomic(path, data); self._fault("service_preview_staged")
            owner = {"schema": 1, "intent_id": intent_id,
                     "expires_at": _plus_seconds(validate_timestamp(self._clock.now()), 60)}
            owner["checksum"] = compute_checksum(owner)
            self._fs.write_atomic(f"preview-staging/{intent_id}/owner.json",
                                  dumps_canonical(owner))
            verified = schemas.Preview.from_json(self._fs.read_bytes(path, max_bytes=schemas.MAX_PREVIEW_BYTES))
            if verified.preview_digest != preview.preview_digest:
                raise RepositoryCorruptError("staged service preview digest mismatch")
            return MappingProxyType({"relative_path": path, "preview_digest": preview.preview_digest,
                                     "artifact_bytes": len(data)})

    def _ensure_replacement_quota_locked(self, path: str, data: bytes, *,
                                         consume_service_reservation: bool = False) -> None:
        """Apply the total quota to the positive byte delta of an atomic replace."""
        old_size = self._fs.file_size(path) if self._fs.exists(path) else 0
        self._ensure_quota_locked(max(0, len(data) - old_size),
                                  consume_service_reservation=consume_service_reservation)

    def _enforce_aggregate_quotas_locked(self, aggregate: str, additional_bytes: int) -> None:
        """Enforce repository-owned aggregate-specific quotas under lock (Fix #6).

        Tombstone rows/bytes and audit (transaction) rows/bytes are repository-
        owned.  Service-owned create-rate and preview-concurrency limits are
        explicitly deferred outside the repository (they require server-layer
        timing and worker accounting that this pure authority layer does not
        own).
        """
        state = self._states[aggregate]
        new_rows = len(state.records) + 1
        # Compute aggregate bytes from the active log + generation files.
        agg_bytes = self._fs.file_size(self._active_path(aggregate))
        for _gen, gen_path in self._generation_files(aggregate):
            agg_bytes += self._fs.file_size(gen_path)
        new_bytes = agg_bytes + additional_bytes
        if aggregate == "tombstones":
            if new_rows > self.limits.tombstone_rows:
                raise RepositoryQuotaError(
                    "tombstone row quota exceeded", code="tombstone_quota_exceeded")
            if new_bytes > self.limits.tombstone_bytes:
                raise RepositoryQuotaError(
                    "tombstone byte quota exceeded", code="tombstone_quota_exceeded")
        elif aggregate == "transactions":
            if new_rows > self.limits.audit_rows:
                raise RepositoryQuotaError(
                    "audit row quota exceeded", code="audit_quota_exceeded")
            if new_bytes > self.limits.audit_bytes:
                raise RepositoryQuotaError(
                    "audit byte quota exceeded", code="audit_quota_exceeded")

    def append(self, aggregate: str, kind: str, payload: Mapping[str, Any] | Any) -> schemas.Wrapper:
        if aggregate not in AGGREGATES or _KIND_AGGREGATE.get(kind) != aggregate:
            raise SchemaError("record kind does not belong to the requested aggregate")
        mapping = (
            loads_canonical(payload.canonical_bytes())
            if hasattr(payload, "canonical_bytes")
            else dict(payload)
        )
        _PAYLOAD_PARSER[kind].from_mapping(mapping)
        with self._lock.hold(self._lock_timeout):
            return self._append_locked(aggregate, kind, mapping)

    def _append_locked(self, aggregate: str, kind: str, payload: dict[str, Any], *,
                       permit_blocked: bool = False,
                       consume_service_reservation: bool = False) -> schemas.Wrapper:
        # Under the cross-process append/rotation lock, reload and validate
        # current disk authority before assigning a sequence (Fix #1).
        # Two already-open repository instances/processes must append unique
        # contiguous sequences without corruption.  We re-scan the aggregate
        # from disk (checkpoint + generations + active log) to discover any
        # appends or rotations made by another process while we waited for
        # the lock.
        if aggregate == "corruption":
            # Internal marker/repair appends must also observe appends made by a
            # second long-lived instance before assigning their sequence.
            state, faults = self._scan_aggregate_locked("corruption")
            if faults:
                self._states["corruption"] = state
                raise RepositoryCorruptError("corruption authority itself is corrupt")
            self._states["corruption"] = state
        elif not permit_blocked:
            state, faults = self._scan_aggregate_locked(aggregate)
            if faults:
                for fault in faults:
                    identity_key = (fault[0], fault[1], fault[2], fault[3], fault[4])
                    existing = self._current_corruption_markers()
                    if identity_key not in existing:
                        self._append_corruption_locked(*fault)
                state.blocked = True
            # A valid fallback scan does not clear an unresolved corruption
            # marker (notably a rejected checkpoint).  Only an explicit repair
            # successor may restore mutation authority.
            latest_markers: dict[str, schemas.CorruptionMarker] = {}
            for wrapper in self._states["corruption"].records:
                if wrapper.kind == "corruption":
                    marker = schemas.CorruptionMarker.from_mapping(wrapper.payload)
                    latest_markers[marker.corruption_id] = marker
            if any(marker.repaired_at is None
                   and getattr(marker.authority_identity, "aggregate", None) == aggregate
                   for marker in latest_markers.values()):
                state.blocked = True
            self._states[aggregate] = state
        state = self._states[aggregate]
        if state.blocked and not permit_blocked:
            raise RepositoryCorruptError(f"aggregate {aggregate} is blocked by corruption")
        obj = {"record_schema": 1, "kind": kind, "sequence": state.sequence + 1,
               "payload": payload}
        obj["checksum"] = compute_checksum(obj)
        wrapper = schemas.Wrapper.from_mapping(obj)
        row = wrapper.canonical_bytes() + b"\n"
        self._ensure_quota_locked(len(row),
                                  consume_service_reservation=consume_service_reservation)
        self._enforce_aggregate_quotas_locked(aggregate, len(row))
        active = self._active_path(aggregate)
        if self._fs.file_size(active) and self._fs.file_size(active) + len(row) > self._rotation_bytes:
            self._rotate_locked(aggregate)
            state = self._states[aggregate]
        self._fs.append_fsync(active, row)
        state.sequence = wrapper.sequence
        state.records.append(wrapper)
        return wrapper

    def _rotate_locked(self, aggregate: str) -> None:
        state = self._states[aggregate]
        generation = state.generation + 1
        destination = self._generation_path(aggregate, generation)
        if self._fs.exists(destination):
            raise RepositoryCorruptError("rotation generation already exists")
        self._fs.replace(self._active_path(aggregate), destination)
        self._fs.write_atomic(self._active_path(aggregate), b"")
        state.generation = generation
        self._write_checkpoint_locked(aggregate)
        self._prune_locked(aggregate)

    def checkpoint(self, aggregate: str) -> schemas.Checkpoint:
        with self._lock.hold(self._lock_timeout):
            return self._write_checkpoint_locked(aggregate)

    def _write_checkpoint_locked(self, aggregate: str) -> schemas.Checkpoint:
        state = self._states[aggregate]
        records = [w.to_mapping() for w in state.records]
        old_generation = 0
        path = self._checkpoint_path(aggregate)
        if self._fs.exists(path):
            old_generation = schemas.Checkpoint.from_json(self._fs.read_bytes(path)).generation
        obj: dict[str, Any] = {
            "schema": 1, "repository_id": self.repository_id, "kind": aggregate,
            "generation": old_generation + 1,
            "sequence_high_water": state.sequence,
            "covered_generations": [g for g, _ in self._generation_files(aggregate)],
            "records_digest": canonical_digest(records), "records": records,
            "created_at": self._clock.now(),
        }
        obj["checksum"] = compute_checksum(obj)
        checkpoint = schemas.Checkpoint.from_mapping(obj)
        data = checkpoint.canonical_bytes()
        if len(data) > self.limits.checkpoint_bytes:
            raise RepositoryQuotaError("checkpoint quota exceeded", code="checkpoint_quota_exceeded")
        self._ensure_replacement_quota_locked(path, data)
        self._fs.write_atomic(path, data)
        verified = schemas.Checkpoint.from_json(self._fs.read_bytes(path))
        if verified != checkpoint:
            raise RepositoryCorruptError("checkpoint reread verification failed")
        return checkpoint

    def _prune_locked(self, aggregate: str) -> None:
        files = self._generation_files(aggregate)
        if len(files) <= self.limits.generation_count:
            return
        checkpoint = schemas.Checkpoint.from_json(self._fs.read_bytes(self._checkpoint_path(aggregate)))
        protected = self._protected_generations_locked(aggregate)
        for generation, path in files[: max(0, len(files) - self.limits.generation_count)]:
            if generation not in checkpoint.covered_generations or generation in protected:
                continue
            self._fs.unlink(path)

    def _protected_generations_locked(self, aggregate: str) -> set[int]:
        protected: set[int] = set()
        for wrapper in self._states["corruption"].records:
            if wrapper.kind == "corruption":
                marker = schemas.CorruptionMarker.from_mapping(wrapper.payload)
                ai = marker.authority_identity
                if marker.repaired_at is None and getattr(ai, "kind", None) == "aggregate" and ai.aggregate == aggregate and ai.generation is not None:
                    protected.add(ai.generation)
        # Nonterminal WALs conservatively pin all generations; repository-level
        # code cannot infer which proposal/audit generation a coordinator needs.
        if not self._recovery.serving_safe:
            protected.update(g for g, _ in self._generation_files(aggregate))
        return protected

    def begin_service_intent(self, intent: schemas.ServiceMutationIntent,
                             read_precondition: Any | None = None,
                             deadline_monotonic: float | None = None) -> schemas.ServiceMutationIntent:
        """Admit/reconcile and roll forward one exact metadata mutation intent."""
        if intent.repository_id != self.repository_id or intent.phase != "prepared":
            raise SchemaError("service intent must be a local prepared intent")
        with self._lock.hold(self._lock_timeout):
            intents = self._read_service_intents_locked()
            matches = [i for i in intents if i.scope == intent.scope and i.key_digest == intent.key_digest]
            if matches:
                if matches[-1].request_fingerprint != intent.request_fingerprint:
                    raise IdempotencyConflictError("same scope/key has a different request fingerprint")
                return self._recover_one_service_intent_locked(matches[-1])
            nonterminal = [i for i in intents if i.phase not in {"committed", "indeterminate"}]
            if len(nonterminal) >= self.limits.service_wal_nonterminal_files:
                raise RepositoryQuotaError("service WAL nonterminal-file quota exceeded")
            if len(intents) >= self.limits.service_wal_retained_files:
                raise RepositoryQuotaError("service WAL retained-file quota exceeded")
            for aggregate in AGGREGATES:
                refreshed, faults = self._scan_aggregate_locked(aggregate)
                if faults:
                    raise RepositoryCorruptError(f"{aggregate} authority scan failed")
                self._states[aggregate] = refreshed
            state = self._states["proposals"]
            current_waters = {k: AggregateHighWater(v.generation, v.sequence)
                              for k, v in self._states.items()}
            current_token = canonical_digest({"repository_id": self.repository_id,
                "high_waters": {k: {"generation": v.generation, "sequence": v.sequence}
                                for k, v in current_waters.items()}})
            current_snapshot = ProposalRepositorySnapshot(
                self.repository_id, MappingProxyType(current_waters),
                MappingProxyType({k: tuple(schemas.Wrapper.from_json(w.canonical_bytes())
                                         for w in v.records) for k, v in self._states.items()}),
                current_token, tuple(intents))
            proposals = [schemas.ProposalRevision.from_mapping(w.payload) for w in state.records if w.kind == "proposal"]
            latest_by_lineage: dict[str, schemas.ProposalRevision] = {}
            for proposal in proposals:
                current = latest_by_lineage.get(proposal.proposal_id)
                if current is None or proposal.proposal_revision > current.proposal_revision:
                    latest_by_lineage[proposal.proposal_id] = proposal
            now = validate_timestamp(self._clock.now())
            active = [p for p in latest_by_lineage.values()
                      if now < p.expires_at and not any(w.kind == "withdrawal" and
                      w.payload.get("proposal_id") == p.proposal_id and
                      w.payload.get("proposal_revision") == p.proposal_revision for w in state.records)]
            if intent.operation == "create":
                if len({p.proposal_id for p in proposals}) >= self.limits.lineages_per_session:
                    raise RepositoryQuotaError("proposal lineage limit exceeded")
                if len(active) >= self.limits.active_proposals_per_session:
                    raise RepositoryQuotaError("active proposal session limit exceeded")
            if intent.operation == "supersede":
                lineage = [p for p in proposals if p.proposal_id == intent.proposal_id]
                if (not lineage or max(p.proposal_revision for p in lineage) != intent.base_proposal_revision
                        or intent.proposal_revision != intent.base_proposal_revision + 1
                        or any(p.proposal_revision == intent.proposal_revision for p in lineage)
                        or now >= max(lineage, key=lambda p: p.proposal_revision).expires_at
                        or any(w.kind == "withdrawal" and
                               w.payload.get("proposal_id") == intent.proposal_id and
                               w.payload.get("proposal_revision") == intent.base_proposal_revision
                               for w in state.records)):
                    raise RepositoryError("supersession base is not uniquely current", code="invalid_state")
                if len(lineage) >= self.limits.revisions_per_lineage:
                    raise RepositoryQuotaError("proposal lineage revision limit exceeded")
                if any(i.operation == "supersede" and i.proposal_id == intent.proposal_id
                       and i.base_proposal_revision == intent.base_proposal_revision
                       and i.phase != "indeterminate" for i in intents):
                    raise RepositoryError("successor intent already exists", code="invalid_state")
            if intent.operation == "create" and any(
                    p.proposal_id == intent.proposal_id and p.proposal_revision == intent.proposal_revision
                    for p in proposals):
                raise RepositoryError("proposal identity already exists", code="invalid_state")
            if intent.operation in {"decision", "withdraw"}:
                addressed = [p for p in proposals if p.proposal_id == intent.proposal_id
                             and p.proposal_revision == intent.proposal_revision]
                if (len(addressed) != 1
                        or any(p.proposal_id == intent.proposal_id and
                               p.proposal_revision > intent.proposal_revision for p in proposals)
                        or any(w.kind == "withdrawal" and
                               w.payload.get("proposal_id") == intent.proposal_id and
                               w.payload.get("proposal_revision") == intent.proposal_revision
                               for w in state.records)
                        or now >= addressed[0].expires_at):
                    raise RepositoryError("proposal revision is terminal", code="invalid_state")
            if intent.operation == "decision":
                decision_state, decision_faults = self._scan_aggregate_locked("decisions")
                if decision_faults:
                    raise RepositoryCorruptError("decision authority scan failed")
                self._states["decisions"] = decision_state
                rows = [schemas.DecisionRow.from_mapping(w.payload) for w in decision_state.records
                        if w.kind == "decision" and w.payload.get("proposal_id") == intent.proposal_id
                        and w.payload.get("proposal_revision") == intent.proposal_revision
                        and w.payload.get("op_id") == intent.op_id]
                current_token = rows[-1].row_token if rows else None
                if intent.domain["row"].get("previous_row_token") != current_token:
                    raise RepositoryError("decision row token changed", code="decision_conflict")
            if intent.operation == "withdraw" and any(
                    w.kind == "withdrawal" and w.payload.get("proposal_id") == intent.proposal_id
                    and w.payload.get("proposal_revision") == intent.proposal_revision for w in state.records):
                raise RepositoryError("proposal revision already withdrawn", code="invalid_state")
            # The global repository token is evidence of the accepted read.
            # When unrelated aggregates/proposals advanced, the pure
            # proposal-specific precondition decides whether authority is
            # still identical; unrelated work therefore cannot spuriously
            # invalidate this mutation.
            if current_token != intent.accepted_repository_snapshot_token:
                # Create/supersede authority is the lineage/current-revision and
                # successor uniqueness revalidated above. A global token delta
                # caused solely by unrelated proposals is admissible.
                if (intent.operation in {"decision", "withdraw"}
                        and (read_precondition is None or not read_precondition(current_snapshot))):
                    from .errors import StaleReadModelError
                    raise StaleReadModelError()
            elif read_precondition is not None and not read_precondition(current_snapshot):
                from .errors import StaleReadModelError
                raise StaleReadModelError()
            # Admission reserves headroom for a required indeterminate
            # replacement/result even if the next durable step fails.
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise RepositoryError("service mutation deadline elapsed before intent admission",
                                      code="operation_timeout")
            required_bytes = len(intent.canonical_bytes()) + self._service_remaining_bytes(intent)
            try:
                self._ensure_quota_locked(required_bytes)
            except RepositoryQuotaError:
                self._cleanup_unreferenced_staging_locked({intent.intent_id})
                self._ensure_quota_locked(required_bytes)
            written = self._write_service_intent_locked(intent)
            prepared = {"schema": 1, "record_id": intent.assigned["result_record_id"],
                "scope": intent.scope, "key_digest": intent.key_digest,
                "request_fingerprint": intent.request_fingerprint, "state": "prepared",
                "http_status": None, "response_digest": None, "response": None,
                "transaction_id": None, "created_at": intent.assigned["created_at"],
                "expires_at": intent.assigned["expires_at"]}
            prepared["checksum"] = compute_checksum(prepared)
            self._append_locked("endpoint_results", "endpoint_result", prepared,
                                consume_service_reservation=True)
            self._fault("service_endpoint_prepared")
            return self._recover_one_service_intent_locked(written)

    def reconcile_service_intent(self, scope: str, key_digest: str,
                                 request_fingerprint: str) -> schemas.ServiceMutationIntent | None:
        validate_digest(key_digest); validate_digest(request_fingerprint)
        with self._lock.hold(self._lock_timeout):
            matches = [i for i in self._read_service_intents_locked()
                       if i.scope == scope and i.key_digest == key_digest]
            if not matches:
                return None
            if matches[-1].request_fingerprint != request_fingerprint:
                raise IdempotencyConflictError("same scope/key has a different request fingerprint")
            return self._recover_one_service_intent_locked(matches[-1])

    def observe_service_intent_nonblocking(self, scope: str, key_digest: str,
                                           request_fingerprint: str) -> tuple[bool, schemas.ServiceMutationIntent | None]:
        """Observe durable intent authority without waiting for its lock."""
        validate_digest(key_digest); validate_digest(request_fingerprint)
        try:
            with self._lock.hold(0):
                matches = [i for i in self._read_service_intents_locked()
                           if i.scope == scope and i.key_digest == key_digest]
                if matches and matches[-1].request_fingerprint != request_fingerprint:
                    raise IdempotencyConflictError(
                        "same scope/key has a different request fingerprint")
                return True, matches[-1] if matches else None
        except LockUnavailableError:
            return False, None

    def _find_identity_locked(self, aggregate: str, kind: str, field: str, value: str) -> list[schemas.Wrapper]:
        state, faults = self._scan_aggregate_locked(aggregate)
        if faults:
            raise RepositoryCorruptError(f"{aggregate} authority scan failed")
        self._states[aggregate] = state
        return [w for w in state.records if w.kind == kind and w.payload.get(field) == value]

    def _publish_preview_locked(self, preview: schemas.Preview) -> None:
        data = preview.canonical_bytes(); value = preview.preview_digest[7:]
        path = f"previews/sha256/{value[:2]}/{value[2:]}.json"
        self._ensure_replacement_quota_locked(path, data, consume_service_reservation=True)
        self._fs.mkdirs(f"previews/sha256/{value[:2]}")
        if self._fs.exists(path):
            if schemas.Preview.from_json(self._fs.read_bytes(path, max_bytes=schemas.MAX_PREVIEW_BYTES)).canonical_bytes() != data:
                raise RepositoryCorruptError("preview digest authority contradiction")
            return
        self._fs.write_atomic(path, data); self._fault("service_preview_promoted")

    def _recover_one_service_intent_locked(self, intent: schemas.ServiceMutationIntent) -> schemas.ServiceMutationIntent:
        if intent.phase in {"committed", "indeterminate"}:
            return intent
        endpoint_rows = self._find_identity_locked(
            "endpoint_results", "endpoint_result", "record_id",
            intent.assigned["result_record_id"])
        if not endpoint_rows:
            prepared = {"schema": 1, "record_id": intent.assigned["result_record_id"],
                "scope": intent.scope, "key_digest": intent.key_digest,
                "request_fingerprint": intent.request_fingerprint, "state": "prepared",
                "http_status": None, "response_digest": None, "response": None,
                "transaction_id": None, "created_at": intent.assigned["created_at"],
                "expires_at": intent.assigned["expires_at"]}
            prepared["checksum"] = compute_checksum(prepared)
            self._append_locked("endpoint_results", "endpoint_result", prepared,
                                consume_service_reservation=True)
            self._fault("service_endpoint_prepared")
        domain = intent.domain
        existing = self._find_identity_locked(domain["aggregate"], domain["kind"], "record_id",
                                              intent.assigned["record_id"])
        if existing and canonical_digest(dict(existing[-1].payload)) != domain["row_digest"]:
            raise RepositoryCorruptError("service domain identity contradiction")
        if not existing:
            if intent.preview is not None:
                path = intent.preview["staged_ref"]["relative_path"]
                preview = schemas.Preview.from_json(self._fs.read_bytes(path, max_bytes=schemas.MAX_PREVIEW_BYTES))
                self._publish_preview_locked(preview)
            self._append_locked(domain["aggregate"], domain["kind"], dict(domain["row"]),
                                consume_service_reservation=True)
            self._fault("service_domain_appended")
        if intent.phase == "prepared":
            intent = self._advance_service_intent_locked(intent, "domain_appended")
        out = self._find_identity_locked("outbox", "outbox", "outbox_id", intent.outbox.outbox_id)
        if out:
            existing_receipt = schemas.OutboxReceipt.from_mapping(out[-1].payload)
            mutable = {"state", "event_cursor", "attempts", "updated_at", "checksum"}
            if any(getattr(existing_receipt, name) != getattr(intent.outbox, name)
                   for name in schemas.OutboxReceipt.FIELDS - mutable):
                raise RepositoryCorruptError("service outbox identity contradiction")
        if not out:
            self._append_locked("outbox", "outbox", intent.outbox.to_mapping(),
                                consume_service_reservation=True); self._fault("service_outbox_appended")
        if intent.phase == "domain_appended":
            intent = self._advance_service_intent_locked(intent, "outbox_appended")
        results = self._find_identity_locked("endpoint_results", "endpoint_result", "record_id",
                                            intent.assigned["result_record_id"])
        terminal = {"schema": 1, "record_id": intent.assigned["result_record_id"],
            "scope": intent.scope, "key_digest": intent.key_digest,
            "request_fingerprint": intent.request_fingerprint, "state": "terminal",
            "http_status": intent.terminal_http_status, "response_digest": intent.terminal_response_digest,
            "response": dict(intent.terminal_response), "transaction_id": None,
            "created_at": intent.assigned["created_at"], "expires_at": intent.assigned["expires_at"]}
        terminal["checksum"] = compute_checksum(terminal)
        terminals = [w for w in results if w.payload.get("state") == "terminal"]
        if terminals and dict(terminals[-1].payload) != terminal:
            raise RepositoryCorruptError("service terminal result contradiction")
        if not terminals:
            self._append_locked("endpoint_results", "endpoint_result", terminal,
                                consume_service_reservation=True); self._fault("service_terminal_appended")
        if intent.phase == "outbox_appended":
            intent = self._advance_service_intent_locked(intent, "terminal")
        if intent.phase == "terminal":
            intent = self._advance_service_intent_locked(intent, "committed")
        if intent.phase == "committed" and intent.preview is not None:
            staged_path = intent.preview["staged_ref"]["relative_path"]
            self._fs.unlink(staged_path, missing_ok=True)
            self._fs.unlink(f"preview-staging/{intent.intent_id}/owner.json", missing_ok=True)
        return intent

    def _recover_service_intents_locked(self) -> None:
        intents = self._read_service_intents_locked()
        for intent in intents:
            try:
                self._recover_one_service_intent_locked(intent)
            except (RepositoryCorruptError, RepositoryQuotaError, OSError) as exc:
                attempts = min(100, intent.recovery_attempts + 1)
                body = intent.to_mapping(); body["recovery_attempts"] = attempts
                body["updated_at"] = validate_timestamp(self._clock.now())
                if attempts >= 100:
                    body["phase"] = "indeterminate"
                body["checksum"] = compute_checksum(body)
                failed = schemas.ServiceMutationIntent.from_mapping(body)
                self._write_service_intent_locked(failed, intent)
                if attempts >= 100:
                    response = {"schema": 1, "error": {"code": "indeterminate_outcome",
                        "message": "Service intent recovery exhausted its bounded attempts.",
                        "request_id": "req_00000000000000000000000000",
                        "retryable": False, "resync_required": True, "details": []}}
                    result = {"schema": 1, "record_id": intent.assigned["result_record_id"],
                        "scope": intent.scope, "key_digest": intent.key_digest,
                        "request_fingerprint": intent.request_fingerprint,
                        "state": "indeterminate", "http_status": 409,
                        "response_digest": canonical_digest(response), "response": response,
                        "transaction_id": None, "created_at": intent.assigned["created_at"],
                        "expires_at": intent.assigned["expires_at"]}
                    result["checksum"] = compute_checksum(result)
                    existing = self._find_identity_locked("endpoint_results", "endpoint_result",
                                                         "record_id", intent.assigned["result_record_id"])
                    if not any(w.payload.get("state") == "indeterminate" for w in existing):
                        self._append_locked("endpoint_results", "endpoint_result", result,
                                            consume_service_reservation=True)
        state, faults = self._scan_aggregate_locked("endpoint_results")
        if faults:
            raise RepositoryCorruptError("endpoint authority scan failed during service recovery")
        self._states["endpoint_results"] = state
        coherent = {(i.scope, i.key_digest, i.assigned["result_record_id"]) for i in intents}
        for wrapper in tuple(state.records):
            if wrapper.kind != "endpoint_result" or wrapper.payload.get("state") != "prepared":
                continue
            row = schemas.EndpointResult.from_mapping(wrapper.payload)
            # Apply/undo prepared results are owned by transaction
            # intent/WAL recovery, never by the service-intent reconciler.
            if row.transaction_id is not None:
                continue
            if (row.scope, row.key_digest, row.record_id) in coherent:
                continue
            response = {"schema": 1, "error": {"code": "indeterminate_outcome",
                "message": "Prepared endpoint authority has no coherent service intent.",
                "request_id": "req_00000000000000000000000000", "retryable": False,
                "resync_required": True, "details": []}}
            body = row.to_mapping(); body.update(state="indeterminate", http_status=409,
                response=response, response_digest=canonical_digest(response)); body["checksum"] = compute_checksum(body)
            self._append_locked("endpoint_results", "endpoint_result", body)
        referenced = {i.intent_id for i in intents if i.phase not in {"committed", "indeterminate"}}
        self._cleanup_unreferenced_staging_locked(referenced)

    def _cleanup_unreferenced_staging_locked(self, referenced: set[str]) -> None:
        for name in self._fs.list_dir("preview-staging"):
            if name in referenced:
                continue
            owner_path = f"preview-staging/{name}/owner.json"
            if self._fs.exists(owner_path):
                owner = loads_canonical(self._fs.read_bytes(owner_path, max_bytes=4096))
                if (set(owner) == {"schema", "intent_id", "expires_at", "checksum"}
                        and owner.get("schema") == 1 and owner.get("intent_id") == name
                        and owner.get("checksum") == compute_checksum(owner)
                        and validate_timestamp(self._clock.now()) < validate_timestamp(owner["expires_at"])):
                    continue
                self._fs.unlink(owner_path)
            path = f"preview-staging/{name}/preview.json"
            if not self._fs.exists(path):
                continue
            try:
                preview = schemas.Preview.from_json(
                    self._fs.read_bytes(path, max_bytes=schemas.MAX_PREVIEW_BYTES))
            except Exception as exc:
                raise RepositoryCorruptError("abandoned staged preview is malformed") from exc
            self._fs.unlink(path)

    def _prune_service_wal_retention_locked(self) -> int:
        """Prune only certain committed intents after the 180-day audit horizon."""
        now = validate_timestamp(self._clock.now()); removed = 0
        for intent in self._read_service_intents_locked():
            if (intent.phase == "committed"
                    and now > _plus_days(intent.created_at, 180)):
                self._fs.unlink(self._service_intent_path(intent.intent_id))
                removed += 1
        return removed

    def prune_service_retention(self) -> int:
        with self._lock.hold(self._lock_timeout):
            return self._prune_service_wal_retention_locked()

    def snapshot(self) -> ProposalRepositorySnapshot:
        with self._lock.hold(self._lock_timeout):
            for aggregate in AGGREGATES:
                state, faults = self._scan_aggregate_locked(aggregate)
                if faults:
                    if not self._states[aggregate].blocked:
                        raise RepositoryCorruptError(f"{aggregate} authority scan failed")
                    state.blocked = True
                self._states[aggregate] = state
            waters = {k: AggregateHighWater(v.generation, v.sequence) for k, v in self._states.items()}
            # Reparse canonical bytes so a consumer can never mutate repository
            # in-memory authority through a nested payload reference.
            records = {
                k: tuple(schemas.Wrapper.from_json(w.canonical_bytes()) for w in v.records)
                for k, v in self._states.items()
            }
            token_body = {k: {"generation": w.generation, "sequence": w.sequence} for k, w in waters.items()}
            token = canonical_digest({"repository_id": self.repository_id, "high_waters": token_body})
            intents = tuple(schemas.ServiceMutationIntent.from_json(i.canonical_bytes())
                            for i in self._read_service_intents_locked())
            return ProposalRepositorySnapshot(
                self.repository_id, MappingProxyType(waters),
                MappingProxyType(records), token, intents,
              )

    def snapshot_at(self, high_waters: Mapping[str, Mapping[str, int]]) -> ProposalRepositorySnapshot:
        """Reconstruct a historical immutable aggregate prefix for a cursor."""
        if set(high_waters) != set(AGGREGATES):
            raise SchemaError("historical snapshot requires every aggregate high water")
        with self._lock.hold(self._lock_timeout):
            waters: dict[str, AggregateHighWater] = {}
            records: dict[str, tuple[schemas.Wrapper, ...]] = {}
            for aggregate in AGGREGATES:
                requested = high_waters[aggregate]
                if set(requested) != {"generation", "sequence"}:
                    raise SchemaError("historical high water must contain generation and sequence")
                state, faults = self._scan_aggregate_locked(aggregate)
                if faults:
                    raise RepositoryCorruptError("historical cursor authority is corrupt")
                generation, sequence = requested["generation"], requested["sequence"]
                available = {state.generation, *(g for g, _ in self._generation_files(aggregate))}
                if generation not in available or sequence > state.sequence:
                    from .errors import CursorExpiredError
                    raise CursorExpiredError("cursor aggregate generation/prefix was pruned")
                prefix = tuple(schemas.Wrapper.from_json(w.canonical_bytes())
                               for w in state.records if w.sequence <= sequence)
                if len(prefix) != sequence:
                    from .errors import CursorExpiredError
                    raise CursorExpiredError("cursor aggregate prefix is no longer reconstructable")
                waters[aggregate] = AggregateHighWater(generation, sequence)
                records[aggregate] = prefix
            body = {k: {"generation": w.generation, "sequence": w.sequence} for k, w in waters.items()}
            token = canonical_digest({"repository_id": self.repository_id, "high_waters": body})
            intents = tuple(schemas.ServiceMutationIntent.from_json(i.canonical_bytes())
                            for i in self._read_service_intents_locked())
            return ProposalRepositorySnapshot(self.repository_id, MappingProxyType(waters),
                                              MappingProxyType(records), token, intents)

    def new_id(self, prefix: str) -> str:
        """Allocate a canonical opaque identity from repository entropy.

        Identity allocation is not persistence authority; callers must still
        include the id in one of the locked append sequences below.
        """
        if prefix not in {"", "rec_", "out_", "req_", "svc_", "txn_", "undo_", "act_", "cor_"}:
            raise SchemaError("unsupported repository identity prefix")
        return _id(prefix, self._entropy)

    def new_row_token(self) -> str:
        return base64.urlsafe_b64encode(self._entropy.token_bytes(32)).rstrip(b"=").decode("ascii")

    def _read_admission_locked(self, now: str) -> dict[str, Any]:
        path = "service-admission.json"
        if not self._fs.exists(path):
            body = {"schema": 1, "tokens": float(self.limits.create_rate_burst),
                    "updated_at": now, "preview_leases": [], "preview_queue": []}
            body["checksum"] = compute_checksum(body)
            self._fs.write_atomic(path, dumps_canonical(body))
            return body
        obj = loads_canonical(self._fs.read_bytes(path, max_bytes=256 * 1024))
        if set(obj) != {"schema", "tokens", "updated_at", "preview_leases", "preview_queue", "checksum"}:
            raise RepositoryCorruptError("service admission authority is not closed")
        if obj["schema"] != 1 or obj["checksum"] != compute_checksum(obj):
            raise RepositoryCorruptError("service admission authority checksum mismatch")
        validate_timestamp(obj["updated_at"])
        if (isinstance(obj["tokens"], bool) or not isinstance(obj["tokens"], (int, float))
                or not 0 <= obj["tokens"] <= self.limits.create_rate_burst):
            raise RepositoryCorruptError("service admission token authority is invalid")
        for collection in ("preview_leases", "preview_queue"):
            if not isinstance(obj[collection], list):
                raise RepositoryCorruptError("service admission leases must be arrays")
            for lease in obj[collection]:
                if set(lease) != {"lease_id", "owner_pid", "owner_start_token",
                                  "heartbeat_expires_at"}:
                    raise RepositoryCorruptError("service admission lease is not closed")
                from .ids import validate_svc_id
                validate_svc_id(lease["lease_id"])
                if (isinstance(lease["owner_pid"], bool)
                        or not isinstance(lease["owner_pid"], int)
                        or lease["owner_pid"] <= 0
                        or not isinstance(lease["owner_start_token"], str)
                        or not lease["owner_start_token"].isdigit()
                        or len(lease["owner_start_token"]) > 32):
                    raise RepositoryCorruptError("service admission owner identity is invalid")
                validate_timestamp(lease["heartbeat_expires_at"])
        def retained(lease: Mapping[str, Any]) -> bool:
            if now < lease["heartbeat_expires_at"]:
                return True
            status, token = _linux_process_identity(lease["owner_pid"])
            if status == "dead" or (status == "alive" and token != lease["owner_start_token"]):
                return False
            return True  # live matching owner or liveness cannot safely be disproved
        obj["preview_leases"] = [l for l in obj["preview_leases"] if retained(l)]
        obj["preview_queue"] = [l for l in obj["preview_queue"] if retained(l)]
        return obj

    @property
    def preview_owner_process_id(self) -> tuple[int, str]:
        return self._preview_owner_pid, self._preview_owner_start_token

    def _write_admission_locked(self, body: Mapping[str, Any]) -> None:
        value = dict(body); value["checksum"] = compute_checksum(value)
        data = dumps_canonical(value)
        self._ensure_replacement_quota_locked("service-admission.json", data)
        self._fs.write_atomic("service-admission.json", data)

    def admit_create(self, now: str) -> None:
        now = validate_timestamp(now)
        with self._lock.hold(self._lock_timeout):
            body = self._read_admission_locked(now)
            prior = datetime.datetime.strptime(body["updated_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
            current = datetime.datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ")
            elapsed = max(0.0, (current - prior).total_seconds())
            tokens = min(float(self.limits.create_rate_burst),
                         float(body["tokens"]) + elapsed * self.limits.create_rate_per_minute / 60.0)
            if tokens < 1.0:
                raise RepositoryError("create/supersede rate limit exceeded",
                                      code="create_rate_limited")
            body["tokens"] = tokens - 1.0; body["updated_at"] = now
            self._write_admission_locked(body)

    def acquire_preview_lease(self, now: str, *, timeout_seconds: float = 60.0) -> str:
        now = validate_timestamp(now)
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise RepositoryError("preview deadline must be in 0..60 seconds")
        lease_id = _id("svc_", self._entropy)
        expires_at = _plus_seconds(now, int(timeout_seconds))
        deadline = time.monotonic() + timeout_seconds
        queued = False
        while True:
            with self._lock.hold(self._lock_timeout):
                current_now = validate_timestamp(self._clock.now())
                body = self._read_admission_locked(current_now)
                queue_head = body["preview_queue"][0]["lease_id"] if body["preview_queue"] else None
                may_enter = (not body["preview_queue"] and not queued) or queue_head == lease_id
                if len(body["preview_leases"]) < self.limits.preview_workers and may_enter:
                    body["preview_queue"] = [l for l in body["preview_queue"]
                                             if l["lease_id"] != lease_id]
                    body["preview_leases"].append({"lease_id": lease_id,
                        "owner_pid": self._preview_owner_pid,
                        "owner_start_token": self._preview_owner_start_token,
                        "heartbeat_expires_at": _plus_seconds(current_now, 60)})
                    self._write_admission_locked(body)
                    return lease_id
                if not queued:
                    if len(body["preview_queue"]) >= self.limits.preview_queue:
                        raise RepositoryError("preview queue is full",
                                              code="preview_concurrency_limited")
                    body["preview_queue"].append({"lease_id": lease_id,
                        "owner_pid": self._preview_owner_pid,
                        "owner_start_token": self._preview_owner_start_token,
                        "heartbeat_expires_at": expires_at})
                    self._write_admission_locked(body); queued = True
            if time.monotonic() >= deadline:
                self.release_preview_lease(lease_id)
                raise RepositoryError("preview worker deadline exceeded", code="preview_timeout")
            time.sleep(0.01)

    def release_preview_lease(self, lease_id: str) -> None:
        from .ids import validate_svc_id
        validate_svc_id(lease_id)
        with self._lock.hold(self._lock_timeout):
            now = validate_timestamp(self._clock.now()); body = self._read_admission_locked(now)
            body["preview_leases"] = [l for l in body["preview_leases"]
                if l["lease_id"] != lease_id or
                l["owner_pid"] != self._preview_owner_pid or
                l["owner_start_token"] != self._preview_owner_start_token]
            body["preview_queue"] = [l for l in body["preview_queue"]
                if l["lease_id"] != lease_id or
                l["owner_pid"] != self._preview_owner_pid or
                l["owner_start_token"] != self._preview_owner_start_token]
            self._write_admission_locked(body)

    def renew_preview_lease(self, lease_id: str, *, seconds: int = 60) -> bool:
        from .ids import validate_svc_id
        validate_svc_id(lease_id)
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 60:
            raise RepositoryError("preview heartbeat duration must be 1..60 seconds")
        with self._lock.hold(self._lock_timeout):
            now = validate_timestamp(self._clock.now()); body = self._read_admission_locked(now)
            matched = False
            for lease in body["preview_leases"]:
                if (lease["lease_id"] == lease_id
                        and lease["owner_pid"] == self._preview_owner_pid
                        and lease["owner_start_token"] == self._preview_owner_start_token):
                    lease["heartbeat_expires_at"] = _plus_seconds(now, seconds)
                    matched = True
            if matched:
                self._write_admission_locked(body)
            return matched

    def cursor_signer(self):
        """Return a pure signer backed by this repository's immutable key ring."""
        from .pagination import CursorKeyMaterial, HmacCursorSigner
        keys = []
        for key in self.root_authority.cursor_keys:
            padding = "=" * (-len(key.secret_base64url) % 4)
            keys.append(CursorKeyMaterial(
                key.key_id, base64.urlsafe_b64decode(key.secret_base64url + padding),
                key.not_before, key.expires_at))
        return HmacCursorSigner.from_keys(keys, self.root_authority.active_cursor_key_id)

    def available_generations(self) -> Mapping[str, frozenset[int]]:
        """Return reconstructable generations for cursor validation."""
        with self._lock.hold(self._lock_timeout):
            result: dict[str, frozenset[int]] = {}
            for aggregate in AGGREGATES:
                state, faults = self._scan_aggregate_locked(aggregate)
                if faults:
                    raise RepositoryCorruptError("pagination generation authority is corrupt")
                generations = {state.generation}
                generations.update(g for g, _ in self._generation_files(aggregate))
                result[aggregate] = frozenset(generations)
            return MappingProxyType(result)

    def prepare_endpoint(
        self, scope: str, key_digest: str, request_fingerprint: str, *,
        expires_at: str, proposal_id: str | None = None,
        supersedes_revision: int | None = None,
    ) -> EndpointPreparation:
        """Create/replay endpoint prepare authority under the repository lock.

        For proposal creation this also allocates the lineage identity/revision
        while observing current proposal authority.  It publishes no preview
        and no domain row.
        """
        raise RepositoryError("standalone endpoint preparation is forbidden; a service intent is required",
                              code="service_intent_required")
        if not isinstance(scope, str) or not scope or len(scope) > 512:
            raise SchemaError("endpoint scope must be a bounded non-empty string")
        validate_digest(key_digest); validate_digest(request_fingerprint)
        validate_timestamp(expires_at)
        with self._lock.hold(self._lock_timeout):
            for aggregate in ("endpoint_results", "tombstones", "proposals"):
                state, faults = self._scan_aggregate_locked(aggregate)
                if faults:
                    raise RepositoryCorruptError(f"{aggregate} authority scan failed")
                self._states[aggregate] = state
            results = [schemas.EndpointResult.from_mapping(w.payload)
                       for w in self._states["endpoint_results"].records
                       if w.kind == "endpoint_result" and w.payload.get("scope") == scope
                       and w.payload.get("key_digest") == key_digest]
            tombstones = [schemas.Tombstone.from_mapping(w.payload)
                          for w in self._states["tombstones"].records
                          if w.kind == "tombstone" and w.payload.get("scope") == scope
                          and w.payload.get("key_digest") == key_digest]
            if any(row.request_fingerprint != request_fingerprint for row in results + tombstones):
                raise IdempotencyConflictError("same scope/key has a different request fingerprint")
            terminal = [row for row in results if row.state in {"terminal", "indeterminate"}]
            if terminal:
                return EndpointPreparation(Reconciliation("terminal_result", terminal[-1]), "")
            if tombstones:
                state = "terminal_compacted" if tombstones[-1].outcome == "terminal_compacted" else "indeterminate"
                return EndpointPreparation(Reconciliation(state, tombstone=tombstones[-1]), "")
            prepared = [row for row in results if row.state == "prepared"]
            proposals = [schemas.ProposalRevision.from_mapping(w.payload)
                         for w in self._states["proposals"].records if w.kind == "proposal"]
            matching = [p for p in proposals if p.creation_request_fingerprint == request_fingerprint]
            if prepared:
                # A crash after proposal append but before terminal result is
                # recoverable only through the exact linked row.
                if matching:
                    p = matching[-1]
                    return EndpointPreparation(Reconciliation("prepared", prepared[-1]),
                                               p.creation_result_record_id,
                                               p.proposal_id, p.proposal_revision)
                return EndpointPreparation(Reconciliation("prepared", prepared[-1]), "")

            if proposal_id is None:
                proposal_id = _id("", self._entropy)
                revision = 1
            else:
                validate_proposal_id(proposal_id)
                if supersedes_revision is None:
                    raise SchemaError("supersedes_revision is required for an existing lineage")
                lineage = [p for p in proposals if p.proposal_id == proposal_id]
                if not lineage or max(p.proposal_revision for p in lineage) != supersedes_revision:
                    raise RepositoryError("supersession base is not current", code="invalid_state")
                revision = supersedes_revision + 1
            record_id = _id("rec_", self._entropy)
            now = validate_timestamp(self._clock.now())
            body = {"schema": 1, "record_id": record_id, "scope": scope, "key_digest": key_digest,
                    "request_fingerprint": request_fingerprint, "state": "prepared",
                    "http_status": None, "response_digest": None, "response": None,
                    "transaction_id": None, "created_at": now, "expires_at": expires_at}
            body["checksum"] = compute_checksum(body)
            row = schemas.EndpointResult.from_mapping(body)
            self._append_locked("endpoint_results", "endpoint_result", row.to_mapping())
            return EndpointPreparation(Reconciliation("prepared", row), record_id,
                                       proposal_id, revision)

    def append_service_sequence(
        self, *, scope: str, key_digest: str, request_fingerprint: str,
        domain_rows: tuple[tuple[str, str, Any], ...],
        outbox: schemas.OutboxReceipt, terminal: schemas.EndpointResult,
        expected_snapshot_token: str | None = None,
    ) -> schemas.EndpointResult:
        """Append one service mutation's rows/outbox/result under one lock.

        Every row is idempotently recognized by its schema-owned record or
        outbox identity.  A changed duplicate is corruption, never overwrite.
        """
        raise RepositoryError("service rows may only publish through service-intent recovery",
                              code="service_intent_required")
        with self._lock.hold(self._lock_timeout):
            for aggregate in AGGREGATES:
                state, faults = self._scan_aggregate_locked(aggregate)
                if faults:
                    raise RepositoryCorruptError(f"{aggregate} authority scan failed")
                self._states[aggregate] = state
            if expected_snapshot_token is not None:
                waters = {k: {"generation": v.generation, "sequence": v.sequence}
                          for k, v in self._states.items()}
                actual = canonical_digest({"repository_id": self.repository_id,
                                           "high_waters": waters})
                if actual != expected_snapshot_token:
                    from .errors import StaleReadModelError
                    raise StaleReadModelError()
            endpoint = [schemas.EndpointResult.from_mapping(w.payload)
                        for w in self._states["endpoint_results"].records
                        if w.kind == "endpoint_result" and w.payload.get("scope") == scope
                        and w.payload.get("key_digest") == key_digest]
            if any(r.request_fingerprint != request_fingerprint for r in endpoint):
                raise IdempotencyConflictError("same scope/key has a different request fingerprint")
            old_terminal = [r for r in endpoint if r.state in {"terminal", "indeterminate"}]
            if old_terminal:
                if old_terminal[-1].to_mapping() != terminal.to_mapping():
                    raise RepositoryCorruptError("terminal service result contradicts existing authority")
                return old_terminal[-1]
            if not any(r.state == "prepared" for r in endpoint):
                raise RepositoryCorruptError("service mutation lacks prepared endpoint authority")

            for aggregate, kind, raw in domain_rows:
                parsed = _PAYLOAD_PARSER[kind].from_mapping(
                    loads_canonical(raw.canonical_bytes()) if hasattr(raw, "canonical_bytes") else dict(raw))
                mapping = parsed.to_mapping()
                identity_field = "record_id"
                identity = mapping.get(identity_field)
                existing = [w for w in self._states[aggregate].records
                            if w.kind == kind and w.payload.get(identity_field) == identity]
                if existing:
                    if dict(existing[-1].payload) != mapping:
                        raise RepositoryCorruptError("domain row identity has contradictory content")
                else:
                    self._append_locked(aggregate, kind, mapping)
            existing_out = [w for w in self._states["outbox"].records
                            if w.kind == "outbox" and w.payload.get("outbox_id") == outbox.outbox_id]
            if existing_out:
                if schemas.OutboxReceipt.from_mapping(existing_out[-1].payload).payload_digest != outbox.payload_digest:
                    raise RepositoryCorruptError("outbox identity has contradictory payload")
            else:
                self._append_locked("outbox", "outbox", outbox.to_mapping())
            self._append_locked("endpoint_results", "endpoint_result", terminal.to_mapping())
            return terminal

    def pending_outbox(self) -> tuple[schemas.OutboxReceipt, ...]:
        snap = self.snapshot()
        latest: dict[str, schemas.OutboxReceipt] = {}
        for wrapper in snap.aggregate("outbox"):
            if wrapper.kind == "outbox":
                row = schemas.OutboxReceipt.from_mapping(wrapper.payload)
                latest[row.outbox_id] = row
        return tuple(sorted((row for row in latest.values() if row.state == "pending"),
                            key=lambda row: (row.created_at, row.outbox_id)))

    def mark_outbox_appended(self, outbox_id: str, event_cursor: int) -> schemas.OutboxReceipt:
        validate_out_id(outbox_id)
        if isinstance(event_cursor, bool) or not isinstance(event_cursor, int) or event_cursor < 0:
            raise SchemaError("event cursor must be a non-negative integer")
        with self._lock.hold(self._lock_timeout):
            state, faults = self._scan_aggregate_locked("outbox")
            if faults:
                raise RepositoryCorruptError("outbox authority scan failed")
            self._states["outbox"] = state
            rows = [schemas.OutboxReceipt.from_mapping(w.payload) for w in state.records
                    if w.kind == "outbox" and w.payload.get("outbox_id") == outbox_id]
            if not rows:
                raise RepositoryCorruptError("outbox receipt is missing")
            latest = rows[-1]
            if latest.state == "appended":
                if latest.event_cursor != event_cursor:
                    raise RepositoryCorruptError("outbox id maps to a different event cursor")
                return latest
            now = validate_timestamp(self._clock.now())
            body = latest.to_mapping()
            body.update(state="appended", event_cursor=event_cursor,
                        attempts=latest.attempts + 1, updated_at=now)
            body["checksum"] = compute_checksum(body)
            advanced = schemas.OutboxReceipt.from_mapping(body)
            self._append_locked("outbox", "outbox", advanced.to_mapping())
            return advanced

    def publish_preview(self, preview: schemas.Preview | Mapping[str, Any] | bytes) -> schemas.Preview:
        if isinstance(preview, bytes):
            parsed = schemas.Preview.from_json(preview)
        elif isinstance(preview, schemas.Preview):
            parsed = preview
        else:
            parsed = schemas.Preview.from_mapping(preview)
        data = parsed.canonical_bytes()
        digest_hex = parsed.preview_digest.removeprefix("sha256:")
        path = f"previews/sha256/{digest_hex[:2]}/{digest_hex[2:]}.json"
        with self._lock.hold(self._lock_timeout):
            self._ensure_replacement_quota_locked(path, data)
            self._fs.mkdirs(f"previews/sha256/{digest_hex[:2]}")
            if self._fs.exists(path):
                existing = schemas.Preview.from_json(self._fs.read_bytes(path, max_bytes=schemas.MAX_PREVIEW_BYTES))
                if existing.preview_digest != parsed.preview_digest or existing.canonical_bytes() != data:
                    raise RepositoryCorruptError("preview content-address authority contradicts digest")
                return existing
            self._fs.write_atomic(path, data)
            return self.get_preview(parsed.preview_digest)

    def get_preview(self, digest: str) -> schemas.Preview:
        validate_digest(digest)
        value = digest[7:]
        path = f"previews/sha256/{value[:2]}/{value[2:]}.json"
        if not self._fs.exists(path):
            raise RepositoryCorruptError("referenced preview authority is missing")
        try:
            preview = schemas.Preview.from_json(self._fs.read_bytes(path, max_bytes=schemas.MAX_PREVIEW_BYTES))
        except Exception as exc:
            raise RepositoryCorruptError("preview authority failed validation") from exc
        if preview.preview_digest != digest:
            raise RepositoryCorruptError("preview authority digest mismatch")
        return preview

    def reconcile_endpoint(self, scope: str, key_digest: str, request_fingerprint: str) -> Reconciliation:
        validate_digest(key_digest); validate_digest(request_fingerprint)
        with self._lock.hold(self._lock_timeout):
            # Rescan endpoint-results, tombstones, and transaction/WAL authority
            # from disk under the lock so two open instances see current
            # authority (Req #3).
            ep_state, ep_faults = self._scan_aggregate_locked("endpoint_results")
            self._states["endpoint_results"] = ep_state
            tb_state, tb_faults = self._scan_aggregate_locked("tombstones")
            self._states["tombstones"] = tb_state
            tx_state, tx_faults = self._scan_aggregate_locked("transactions")
            self._states["transactions"] = tx_state
            corruption_state, corruption_faults = self._scan_aggregate_locked("corruption")
            self._states["corruption"] = corruption_state
            if ep_faults or tb_faults or tx_faults or corruption_faults:
                for fault in ep_faults + tb_faults + tx_faults + corruption_faults:
                    self._append_corruption_locked(*fault)
                    self._states[fault[0]].blocked = True
                raise RepositoryCorruptError("endpoint reconciliation authority scan failed")
            repaired = [item for item in self._closed_repair_classifications_locked()
                        if item.scope == scope and item.key_digest == key_digest]
            if repaired:
                identities = {(item.scope, item.key_digest, item.transaction_id,
                               item.outcome) for item in repaired}
                if len(identities) != 1:
                    raise RepositoryCorruptError(
                        "contradictory closed repair classifications for scope/key")
                return Reconciliation("indeterminate",
                                      transaction_id=repaired[-1].transaction_id)
            now = validate_timestamp(self._clock.now())
            all_results = [schemas.EndpointResult.from_mapping(w.payload) for w in ep_state.records
                           if w.kind == "endpoint_result" and w.payload.get("scope") == scope
                           and w.payload.get("key_digest") == key_digest]
            had_terminal = any(row.state in {"terminal", "indeterminate"}
                               for row in all_results)
            retained_terminal = any(row.state in {"terminal", "indeterminate"}
                                    and now <= _plus_days(row.created_at, 180)
                                    for row in all_results)
            results = [row for row in all_results
                       if (row.state == "prepared" and (not had_terminal or retained_terminal))
                       or (row.state != "prepared" and
                           now <= _plus_days(row.created_at, 180))]
            tombstones = [row for row in
                          (schemas.Tombstone.from_mapping(w.payload) for w in tb_state.records
                           if w.kind == "tombstone" and w.payload.get("scope") == scope
                           and w.payload.get("key_digest") == key_digest)
                          if now <= row.expires_at]
            authorities = results + tombstones
            audits = [schemas.TransactionAudit.from_mapping(w.payload) for w in tx_state.records
                      if w.kind == "transaction" and w.payload.get("endpoint_scope") == scope
                      and w.payload.get("idempotency_key_digest") == key_digest]

            tx_dirs = self._fs.list_dir("transactions")
            if len(tx_dirs) > self.limits.audit_rows:
                raise RepositoryQuotaError("transaction directory count exceeds audit cap")
            aggregate_wal_bytes = 0
            matching_wals: list[schemas.WAL] = []
            for name in tx_dirs:
                wal_path = f"transactions/{name}/wal.json"
                if not self._fs.exists(wal_path):
                    continue
                try:
                    size = self._fs.file_size(wal_path)
                    aggregate_wal_bytes += size
                    if size > self.limits.checkpoint_bytes or aggregate_wal_bytes > self.limits.audit_bytes:
                        raise RepositoryQuotaError("transaction WAL scan cap exceeded")
                    wal = schemas.WAL.from_json(
                        self._fs.read_bytes(wal_path, max_bytes=self.limits.checkpoint_bytes))
                    if wal.transaction_id != name:
                        raise RepositoryCorruptError("transaction directory/WAL identity mismatch")
                except RepositoryQuotaError:
                    raise
                except Exception as exc:
                    raise RepositoryCorruptError("transaction WAL failed endpoint reconciliation validation") from exc
                if wal.idempotency_scope == scope and wal.idempotency_key_digest == key_digest:
                    matching_wals.append(wal)
            self._recovery = self._classify_wals_locked()

            if len(matching_wals) > 1:
                self._append_corruption_locked("endpoint_results", None, None,
                    "active/endpoint-results.jsonl", "authority_contradiction", b"")
                self._states["endpoint_results"].blocked = True
                raise RepositoryCorruptError("multiple WALs claim one endpoint idempotency identity")
            if len(audits) > 1:
                self._append_corruption_locked("transactions", None, None,
                    "active/transactions.jsonl", "authority_contradiction", b"")
                self._states["transactions"].blocked = True
                raise RepositoryCorruptError("multiple audits claim one endpoint idempotency identity")
            # Require one fingerprint/coherent transaction lineage (Req #3).
            if any(a.request_fingerprint != request_fingerprint for a in authorities + audits + matching_wals):
                raise IdempotencyConflictError("same scope/key has a different request fingerprint")
            transaction_ids = {a.transaction_id for a in authorities + audits + matching_wals
                               if a.transaction_id is not None}
            if len(transaction_ids) > 1:
                self._append_corruption_locked("endpoint_results", None, None,
                    "active/endpoint-results.jsonl", "authority_contradiction", b"")
                self._states["endpoint_results"].blocked = True
                raise RepositoryCorruptError("endpoint authority has multiple transaction lineages")
            # All terminal rows must be byte-equivalent (inspect complete set).
            terminal = [r for r in results if r.state in {"terminal", "indeterminate"}]
            if terminal:
                first_terminal = terminal[0].to_mapping()
                if any(r.to_mapping() != first_terminal for r in terminal[1:]):
                    self._append_corruption_locked(
                        "endpoint_results", None, None, "active/endpoint-results.jsonl",
                        "authority_contradiction", b"")
                    self._states["endpoint_results"].blocked = True
                    raise RepositoryCorruptError("contradictory terminal endpoint results")
            # Prepared rows must be coherent.
            prepared = [r for r in results if r.state == "prepared"]
            if terminal and prepared:
                # Endpoint state advances append-only from prepared to one
                # terminal row.  Every immutable identity field must agree.
                if not scope.startswith(("/__proposal/v1/", "POST /__proposal/v1/")):
                    self._append_corruption_locked("endpoint_results", None, None,
                        "active/endpoint-results.jsonl", "authority_contradiction", b"")
                    self._states["endpoint_results"].blocked = True
                    raise RepositoryCorruptError("prepared and terminal endpoint authorities coexist")
                immutable = ("record_id", "scope", "key_digest", "request_fingerprint",
                             "transaction_id", "expires_at")
                if any(any(getattr(p, name) != getattr(terminal[-1], name)
                           for name in immutable) for p in prepared):
                    self._append_corruption_locked("endpoint_results", None, None,
                        "active/endpoint-results.jsonl", "authority_contradiction", b"")
                    self._states["endpoint_results"].blocked = True
                    raise RepositoryCorruptError("prepared and terminal endpoint authorities disagree")
            # A terminal result may durably precede the final WAL terminal
            # replacement (CRASH-11). Recovery verifies the intent candidate
            # and advances that same transaction; coexistence is not itself
            # contradictory.
            # Audit is intentionally appended before the terminal endpoint.
            # Prepared endpoint + exact same-transaction audit is therefore a
            # recoverable finalization window while a nonterminal WAL exists.
            if prepared:
                first_prep = prepared[0].to_mapping()
                if any(r.to_mapping() != first_prep for r in prepared[1:]):
                    self._append_corruption_locked(
                        "endpoint_results", None, None, "active/endpoint-results.jsonl",
                        "authority_contradiction", b"")
                    self._states["endpoint_results"].blocked = True
                    raise RepositoryCorruptError("contradictory prepared endpoint results")
            # Every matching tombstone remains durable authority.  Check the
            # complete set before preferring a live terminal row or the
            # latest tombstone so a coherent successor cannot hide an
            # earlier contradictory classification.
            if tombstones:
                latest = tombstones[-1]
                coherent_fields = ("result_record_id", "scope", "key_digest", "request_fingerprint",
                                   "outcome", "transaction_id", "response_digest")
                if any(any(getattr(t, name) != getattr(latest, name)
                           for name in coherent_fields)
                       for t in tombstones[:-1]):
                    self._append_corruption_locked(
                        "tombstones", None, None, "active/tombstones.jsonl",
                        "authority_contradiction", b"")
                    self._states["tombstones"].blocked = True
                    raise RepositoryCorruptError("contradictory tombstones for scope/key")
            if terminal:
                term = terminal[-1]
                now = validate_timestamp(self._clock.now())
                if now >= term.expires_at and not tombstones:
                    tombstone_body = {"schema": 1, "result_record_id": term.record_id,
                        "scope": term.scope, "key_digest": term.key_digest,
                        "request_fingerprint": term.request_fingerprint,
                        "outcome": "terminal_compacted", "response_digest": term.response_digest,
                        "transaction_id": term.transaction_id, "created_at": now,
                        "expires_at": _plus_days(term.created_at, 180)}
                    tombstone_body["checksum"] = compute_checksum(tombstone_body)
                    tombstone = schemas.Tombstone.from_mapping(tombstone_body)
                    self._append_locked("tombstones", "tombstone", tombstone.to_mapping())
                    return Reconciliation("terminal_compacted", tombstone=tombstone,
                                          transaction_id=term.transaction_id)
                if tombstones:
                    latest = tombstones[-1]
                    term = terminal[-1]
                    if (latest.result_record_id != term.record_id
                            or latest.scope != term.scope
                            or latest.key_digest != term.key_digest
                            or latest.request_fingerprint != term.request_fingerprint
                            or latest.transaction_id != term.transaction_id
                            or latest.response_digest != term.response_digest
                            or latest.outcome != "terminal_compacted"):
                        self._append_corruption_locked("endpoint_results", None, None,
                            "active/endpoint-results.jsonl", "authority_contradiction", b"")
                        self._states["endpoint_results"].blocked = True
                        self._states["tombstones"].blocked = True
                        raise RepositoryCorruptError("terminal endpoint and tombstone disagree")
                return Reconciliation("terminal_result", endpoint_result=terminal[-1], transaction_id=terminal[-1].transaction_id)
            if tombstones:
                latest = tombstones[-1]
                # Cross-authority: terminal endpoint result transaction must
                # match tombstone transaction.
                state = "terminal_compacted" if latest.outcome == "terminal_compacted" else "indeterminate"
                return Reconciliation(state, tombstone=latest, transaction_id=latest.transaction_id)
            if matching_wals:
                return Reconciliation("nonterminal_wal", transaction_id=matching_wals[0].transaction_id)
            if prepared:
                return Reconciliation("prepared", endpoint_result=prepared[-1], transaction_id=prepared[-1].transaction_id)
            return Reconciliation("new")

    # Immutable WAL identity fields (SPEC section 5.6): these fields define the
    # transaction identity and MUST NOT change across WAL updates.
    _WAL_IMMUTABLE_FIELDS: tuple[str, ...] = (
        "schema", "operation", "transaction_id", "proposal_id",
        "proposal_revision", "idempotency_scope", "idempotency_key_digest",
        "request_fingerprint", "transaction_intent_digest", "read_model_token", "snapshot_digest",
        "validation_context_digest", "validator_id", "serializer_id",
        "operation_runtime_id", "accepted_op_ids",
        "stage_dir_identity", "backup_dir_identity", "created_at",
        "decision_set_token", "preview_digest",
    )
    # Legal monotonic mutable fields: these may advance under normative
    # transitions.  ``decision_set_token``/``preview_digest`` are nullable only
    # for undo and advance from null under the normative phase machine.
    _WAL_MUTABLE_FIELDS: tuple[str, ...] = (
        "phase", "commit_decided",
        "publish_bitmap", "rollback_bitmap", "recovery_attempts",
        "updated_at", "checksum",
    )
    # Complete legal forward transition graph (SPEC section 5.6 / 7.4).
    # Each entry: old_phase -> set of legal next phases.
    _WAL_TRANSITIONS: dict[str, frozenset[str]] = {
        "prepared": frozenset({"commit_decided", "rolling_back", "indeterminate"}),
        "commit_decided": frozenset({"publishing", "content_published", "indeterminate"}),
        "publishing": frozenset({"content_published", "indeterminate"}),
        "content_published": frozenset({"finalizing", "indeterminate"}),
        "finalizing": frozenset({"committed", "indeterminate"}),
        "committed": frozenset(),  # terminal
        "rolling_back": frozenset({"rolled_back", "indeterminate"}),
        "rolled_back": frozenset(),  # terminal
        "indeterminate": frozenset({"indeterminate"}),
    }

    def publish_wal(self, wal: schemas.WAL | Mapping[str, Any]) -> schemas.WAL:
        parsed = wal if isinstance(wal, schemas.WAL) else schemas.WAL.from_mapping(wal)
        path = f"transactions/{parsed.transaction_id}/wal.json"
        with self._lock.hold(self._lock_timeout):
            # A changed transaction id must not evade immutable-field checking
            # by selecting a new path while retaining the prepare-time
            # idempotency identity.
            for name in self._fs.list_dir("transactions"):
                other_path = f"transactions/{name}/wal.json"
                if name == parsed.transaction_id or not self._fs.exists(other_path):
                    continue
                other = schemas.WAL.from_json(
                    self._fs.read_bytes(other_path, max_bytes=self.limits.checkpoint_bytes))
                if (other.idempotency_scope == parsed.idempotency_scope
                        and other.idempotency_key_digest == parsed.idempotency_key_digest):
                    raise RepositoryCorruptError(
                        "WAL idempotency identity cannot be rebound to another transaction")
            data = parsed.canonical_bytes()
            self._ensure_replacement_quota_locked(path, data)
            self._fs.mkdirs(f"transactions/{parsed.transaction_id}")
            if self._fs.exists(path):
                old = schemas.WAL.from_json(
                    self._fs.read_bytes(path, max_bytes=self.limits.checkpoint_bytes))
                if len(old.targets) != len(parsed.targets):
                    raise RepositoryCorruptError("WAL target cardinality changed")
                for old_target, new_target in zip(old.targets, parsed.targets):
                    old_body, new_body = old_target.to_mapping(), new_target.to_mapping()
                    old_result = old_body.pop("resulting_identity")
                    new_result = new_body.pop("resulting_identity")
                    if old_body != new_body or (old_result is not None and old_result != new_result):
                        raise RepositoryCorruptError("WAL immutable target authority changed")
                # Reject rebinding of immutable identity fields (Req #4).
                for fname in self._WAL_IMMUTABLE_FIELDS:
                    old_val = getattr(old, fname)
                    new_val = getattr(parsed, fname)
                    if old_val != new_val:
                        raise RepositoryCorruptError(
                            f"WAL immutable field {fname!r} changed: "
                            f"was {old_val!r}, now {new_val!r}")
                # Enforce the complete legal transition graph (Req #4).
                if old.phase != parsed.phase:
                    legal_next = self._WAL_TRANSITIONS.get(old.phase, frozenset())
                    if parsed.phase not in legal_next:
                        raise RepositoryCorruptError(
                            f"WAL illegal phase transition: "
                            f"{old.phase!r} -> {parsed.phase!r}")
                if (old.phase == "commit_decided" and parsed.phase == "content_published"
                        and len(parsed.targets) != 1):
                    raise RepositoryCorruptError(
                        "multi-target WAL cannot skip the publishing phase")
                # created_at is immutable.
                if old.created_at != parsed.created_at:
                    raise RepositoryCorruptError(
                        "WAL created_at is immutable")
                # updated_at must be nondecreasing.
                if parsed.updated_at < old.updated_at:
                    raise RepositoryCorruptError(
                        "WAL updated_at must be nondecreasing")
                # recovery_attempts must be nondecreasing.
                if parsed.recovery_attempts < old.recovery_attempts:
                    raise RepositoryCorruptError(
                        "WAL recovery_attempts must be nondecreasing")
                # Bitmap monotonicity is pointwise: moving a true bit to a
                # different position is a regression even when the count is
                # unchanged.
                if (old.phase in {"publishing", "content_published", "finalizing", "committed"}
                        and parsed.phase in {"publishing", "content_published", "finalizing", "committed"}):
                    if any(before and not after for before, after in
                           zip(old.publish_bitmap, parsed.publish_bitmap)):
                        raise RepositoryCorruptError(
                            "WAL publish bitmap must be pointwise monotonic")
                # Monotonic rollback bitmap within rollback phases.
                if (old.phase in {"rolling_back", "rolled_back"}
                        and parsed.phase in {"rolling_back", "rolled_back"}):
                    if any(before and not after for before, after in
                           zip(old.rollback_bitmap, parsed.rollback_bitmap)):
                        raise RepositoryCorruptError(
                            "WAL rollback bitmap must be pointwise monotonic")
                # commit_decided constraints: once true, cannot go false (except
                # indeterminate which is special).
                if old.commit_decided and not parsed.commit_decided:
                      raise RepositoryCorruptError(
                          "WAL commit_decided cannot regress from true")
                if old.phase == parsed.phase:
                    if any(before and not after for before, after in
                           zip(old.publish_bitmap, parsed.publish_bitmap)):
                        raise RepositoryCorruptError("same-phase publish bitmap regressed")
                    if any(before and not after for before, after in
                           zip(old.rollback_bitmap, parsed.rollback_bitmap)):
                        raise RepositoryCorruptError("same-phase rollback bitmap regressed")
                # No terminal rewrites.
                if old.phase in {"committed", "rolled_back"} and old.phase != parsed.phase:
                    raise RepositoryCorruptError(
                        f"WAL cannot leave terminal {old.phase!r} phase")
            elif parsed.phase != "prepared":
                raise RepositoryCorruptError(
                    "first WAL publication must be in prepared phase")
            self._fs.write_atomic(path, data)
            verified = schemas.WAL.from_json(self._fs.read_bytes(path))
            self._recovery = self._classify_wals_locked()
            return verified

    def read_wal(self, transaction_id: str) -> schemas.WAL:
        return schemas.WAL.from_json(self._fs.read_bytes(
            f"transactions/{transaction_id}/wal.json", max_bytes=self.limits.checkpoint_bytes))

    def publish_transaction_intent(self, intent: Any) -> Any:
        from .transaction_intent import TransactionFinalizationIntent
        parsed = intent if isinstance(intent, TransactionFinalizationIntent) else TransactionFinalizationIntent.from_mapping(intent)
        path = f"transactions/{parsed.value['transaction_id']}/intent.json"
        with self._lock.hold(self._lock_timeout):
            data = parsed.canonical_bytes(); self._ensure_replacement_quota_locked(path, data)
            self._fs.mkdirs(f"transactions/{parsed.value['transaction_id']}")
            if self._fs.exists(path):
                old = TransactionFinalizationIntent.from_json(self._fs.read_bytes(path, max_bytes=self.limits.checkpoint_bytes))
                if old.canonical_bytes() != data: raise RepositoryCorruptError("transaction intent identity contradiction")
                return old
            self._fs.write_atomic(path, data)
            verified = TransactionFinalizationIntent.from_json(self._fs.read_bytes(path, max_bytes=self.limits.checkpoint_bytes))
            if verified.canonical_digest() != parsed.canonical_digest():
                raise RepositoryCorruptError("transaction intent publication verification failed")
            return verified

    def publish_transaction_intent_if_admitted(
            self, fence: TransactionAdmissionFence, intent: Any) -> Any | None:
        """Make cancellation-vs-durable-intent admission one repository-lock decision."""
        from .transaction_intent import TransactionFinalizationIntent
        parsed = (intent if isinstance(intent, TransactionFinalizationIntent)
                  else TransactionFinalizationIntent.from_mapping(intent))
        path = f"transactions/{parsed.value['transaction_id']}/intent.json"
        with self._lock.hold(self._lock_timeout):
            if fence.state == "cancelled":
                return None
            if fence.state != "open":
                raise RepositoryCorruptError("transaction admission fence was reused")
            data = parsed.canonical_bytes(); self._ensure_replacement_quota_locked(path, data)
            self._fs.mkdirs(f"transactions/{parsed.value['transaction_id']}")
            if self._fs.exists(path):
                raise RepositoryCorruptError("fresh admission found an existing transaction intent")
            self._fs.write_atomic(path, data)
            verified = TransactionFinalizationIntent.from_json(
                self._fs.read_bytes(path, max_bytes=self.limits.checkpoint_bytes))
            if verified.canonical_digest() != parsed.canonical_digest():
                raise RepositoryCorruptError("transaction intent publication verification failed")
            fence.state = "admitted"
            return verified

    def read_transaction_intent(self, transaction_id: str) -> Any:
        from .transaction_intent import TransactionFinalizationIntent
        return TransactionFinalizationIntent.from_json(self._fs.read_bytes(
            f"transactions/{transaction_id}/intent.json", max_bytes=self.limits.checkpoint_bytes))

    def verify_transaction_association(self, wal: schemas.WAL) -> tuple[Any, schemas.HistoryManifest]:
        intent = self.read_transaction_intent(wal.transaction_id)
        if intent.canonical_digest() != wal.transaction_intent_digest:
            raise RepositoryCorruptError("transaction intent/WAL digest mismatch")
        value = intent.value
        if (value["transaction_id"], value["operation"], value["proposal_id"], value["proposal_revision"]) != (
                wal.transaction_id, wal.operation, wal.proposal_id, wal.proposal_revision):
            raise RepositoryCorruptError("transaction intent/WAL identity mismatch")
        history = schemas.HistoryManifest.from_json(self._fs.read_bytes(
            f"transactions/{wal.transaction_id}/history.json", max_bytes=self.limits.checkpoint_bytes))
        if history.canonical_digest() != value["history"]["manifest_digest"]:
            raise RepositoryCorruptError("transaction intent/history digest mismatch")
        intent_targets = {row["concept"]: row for row in value["targets"]}
        history_targets = {row.concept: row for row in history.targets}
        if set(intent_targets) != {row.concept for row in wal.targets} or set(intent_targets) != set(history_targets):
            raise RepositoryCorruptError("transaction association target mismatch")
        for target in wal.targets:
            linked, historic = intent_targets[target.concept], history_targets[target.concept]
            if (linked["before_digest"], linked["after_digest"], linked["before_identity"],
                    linked["staged_identity"], linked["backup_identity"]) != (
                    target.before_digest, target.staged_digest, target.before_identity.to_mapping(),
                    target.staged_identity.to_mapping(), target.backup_leaf.to_mapping()):
                raise RepositoryCorruptError("intent target linkage mismatch")
            if (historic.before_digest, historic.after_digest, historic.before_identity.to_mapping(),
                    historic.staged_identity.to_mapping(), historic.backup_identity.to_mapping()) != (
                    target.before_digest, target.staged_digest, target.before_identity.to_mapping(),
                    target.staged_identity.to_mapping(), target.backup_leaf.to_mapping()):
                raise RepositoryCorruptError("history target linkage mismatch")
        return intent, history

    def verify_audit_transaction_chain(self, audit: schemas.TransactionAudit) -> tuple[Any, schemas.HistoryManifest]:
        intent = self.read_transaction_intent(audit.transaction_id)
        if intent.canonical_digest() != audit.transaction_intent_digest:
            raise RepositoryCorruptError("transaction audit/intent digest mismatch")
        value = intent.value
        if value["endpoint"]["scope"] != audit.endpoint_scope or value["endpoint"]["key_digest"] != audit.idempotency_key_digest:
            raise RepositoryCorruptError("transaction audit/intent endpoint mismatch")
        if tuple(value["accepted_op_ids"]) != audit.accepted_op_ids:
            raise RepositoryCorruptError("transaction audit/intent accepted operation mismatch")
        history = self.verify_history_link(audit)
        # Intent binds the prepared history digest; the audit binds the
        # intent-derived terminal history digest after resulting identities
        # are known.  Semantic linkage is checked by verify_history_link.
        return intent, history

    def publish_history(self, manifest: schemas.HistoryManifest | Mapping[str, Any]) -> schemas.HistoryManifest:
        parsed = manifest if isinstance(manifest, schemas.HistoryManifest) else schemas.HistoryManifest.from_mapping(manifest)
        path = f"transactions/{parsed.transaction_id}/history.json"
        with self._lock.hold(self._lock_timeout):
            data = parsed.canonical_bytes()
            self._ensure_replacement_quota_locked(path, data)
            self._fs.mkdirs(f"transactions/{parsed.transaction_id}")
            if self._fs.exists(path):
                old = schemas.HistoryManifest.from_json(
                    self._fs.read_bytes(path, max_bytes=self.limits.checkpoint_bytes))
                if old.canonical_bytes() != parsed.canonical_bytes():
                    raise RepositoryCorruptError("history manifest identity contradiction")
                return old
            self._fs.write_atomic(path, data)
            verified = schemas.HistoryManifest.from_json(self._fs.read_bytes(path))
            if verified.canonical_digest() != parsed.canonical_digest():
                raise RepositoryCorruptError("history manifest digest verification failed")
            return verified

    def finalize_history(self, wal: schemas.WAL, *, branch: str) -> schemas.HistoryManifest:
        """Verify and return the one immutable history manifest."""
        if branch not in {"commit", "rollback"}:
            raise RepositoryCorruptError("invalid terminal history branch")
        path = f"transactions/{wal.transaction_id}/history.json"
        with self._lock.hold(self._lock_timeout):
            history = schemas.HistoryManifest.from_json(
                self._fs.read_bytes(path, max_bytes=self.limits.checkpoint_bytes))
            intent = self.read_transaction_intent(wal.transaction_id)
            if history.canonical_digest() != intent.value["history"]["manifest_digest"]:
                raise RepositoryCorruptError("immutable history digest contradiction")
            return history

    def verify_history_link(self, audit: schemas.TransactionAudit | Mapping[str, Any]) -> schemas.HistoryManifest:
        parsed = (
            audit
            if isinstance(audit, schemas.TransactionAudit)
            else schemas.TransactionAudit.from_mapping(loads_canonical(dumps_canonical(dict(audit))))
        )
        path = f"transactions/{parsed.transaction_id}/history.json"
        if not self._fs.exists(path):
            raise RepositoryCorruptError("transaction audit references missing history manifest")
        history = schemas.HistoryManifest.from_json(
            self._fs.read_bytes(path, max_bytes=self.limits.checkpoint_bytes))
        # Compare transaction, operation, parent, proposal/revision, undo group,
        # targets/before/after/backup identities, audit digest (Fix #5).  Any
        # mismatch is corruption.
        if history.canonical_digest() != parsed.history_manifest_digest:
            raise RepositoryCorruptError("history manifest/audit digest linkage mismatch")
        if history.transaction_id != parsed.transaction_id:
            raise RepositoryCorruptError("history manifest/audit transaction_id mismatch")
        if history.operation != parsed.operation:
            raise RepositoryCorruptError("history manifest/audit operation mismatch")
        if history.parent_transaction_id != parsed.parent_transaction_id:
            raise RepositoryCorruptError("history manifest/audit parent_transaction_id mismatch")
        if history.proposal_id != parsed.proposal_id:
            raise RepositoryCorruptError("history manifest/audit proposal_id mismatch")
        if history.proposal_revision != parsed.proposal_revision:
            raise RepositoryCorruptError("history manifest/audit proposal_revision mismatch")
        if history.undo_group_id != parsed.undo_group_id:
            raise RepositoryCorruptError("history manifest/audit undo_group_id mismatch")
        expected_backups = {t.concept: t.backup_identity.to_mapping() for t in history.targets}
        actual_backups = {k: v.to_mapping() for k, v in parsed.target_backup_identities.items()}
        if expected_backups != actual_backups:
            raise RepositoryCorruptError("history manifest/audit backup identity linkage mismatch")
        expected_before = {t.concept: t.before_identity.to_mapping() for t in history.targets}
        actual_before = {k: v.to_mapping() for k, v in parsed.target_before_identities.items()}
        if expected_before != actual_before:
            raise RepositoryCorruptError("history manifest/audit canonical identity linkage mismatch")
        # History manifest target concepts must match audit target maps.
        hist_concepts = {t.concept for t in history.targets}
        if hist_concepts != set(parsed.target_before_revisions):
            raise RepositoryCorruptError("history manifest/audit target concept mismatch")
        return history

    def can_retire_wal(self, transaction_id: str) -> bool:
        with self._lock.hold(self._lock_timeout):
            return self._can_retire_wal_locked(transaction_id)

    def _can_retire_wal_locked(self, transaction_id: str) -> bool:
        # Retirement is a single-lock authority decision.  Never trust the
        # instance's startup snapshot: another process may have appended any
        # participating aggregate since this instance opened.
        for aggregate in ("transactions", "endpoint_results", "tombstones", "outbox"):
            try:
                state, faults = self._scan_aggregate_locked(aggregate)
            except Exception:
                return False
            self._states[aggregate] = state
            if faults:
                state.blocked = True
                return False
        try:
            wal = self.read_wal(transaction_id)
        except Exception:
            return False
        if wal.phase not in {"committed", "rolled_back"}:
            return False
        audits = [schemas.TransactionAudit.from_mapping(w.payload) for w in self._states["transactions"].records
                  if w.kind == "transaction" and w.payload.get("transaction_id") == transaction_id]
        if len(audits) != 1:
            return False
        audit = audits[0]
        try:
            intent, history = self.verify_audit_transaction_chain(audit)
        except Exception:
            return False
        if wal.transaction_intent_digest != intent.canonical_digest():
            return False
        # Compare WAL/audit authority: transaction, operation, proposal/revision,
        # snapshot, preview, runtime identities, undo group (Fix #5).
        if audit.phase != wal.phase:
            return False
        if audit.operation != wal.operation:
            return False
        if audit.proposal_id != wal.proposal_id or audit.proposal_revision != wal.proposal_revision:
            return False
        if audit.endpoint_scope != wal.idempotency_scope:
            return False
        if audit.idempotency_key_digest != wal.idempotency_key_digest:
            return False
        if audit.request_fingerprint != wal.request_fingerprint:
            return False
        if wal.decision_set_token is not None and audit.decision_set_token != wal.decision_set_token:
            return False
        if audit.validator_id != wal.validator_id or audit.serializer_id != wal.serializer_id:
            return False
        if audit.operation_runtime_id != wal.operation_runtime_id:
            return False
        if audit.snapshot_digest != wal.snapshot_digest:
            return False
        # Preview digest comparison: apply WAL only has preview_digest;
        # undo WAL may have null preview_digest.  Audit always stores a
        # non-null digest; when WAL preview is null, compare against the
        # snapshot digest (undo restages from snapshot) (Req #5).
        wal_preview = wal.preview_digest if wal.preview_digest is not None else ""
        if wal_preview:
            if audit.preview_digest != wal_preview:
                return False
        # For undo with null preview_digest, audit.preview_digest must equal
        # snapshot_digest (undo restages from snapshot bytes).
        elif audit.preview_digest != wal.snapshot_digest:
            return False
        wal_targets = {target.concept: target for target in wal.targets}
        if set(wal_targets) != {target.concept for target in history.targets}:
            return False
        for target in history.targets:
            wal_target = wal_targets[target.concept]
            before = audit.target_before_revisions.get(target.concept)
            after = audit.target_after_revisions.get(target.concept)
            # Audits store raw content revisions while WAL/history authority
            # stores sha256 digests.  Normalize exactly once before comparison.
            if before is None or "sha256:" + before != wal_target.before_digest:
                return False
            if after is None or "sha256:" + after != wal_target.staged_digest:
                return False
            if target.before_digest != wal_target.before_digest or target.after_digest != wal_target.staged_digest:
                return False
            if target.backup_identity != audit.target_backup_identities.get(target.concept):
                return False
        results = [schemas.EndpointResult.from_mapping(w.payload) for w in self._states["endpoint_results"].records
                   if w.kind == "endpoint_result" and w.payload.get("transaction_id") == transaction_id
                   and w.payload.get("state") in {"terminal", "indeterminate"}]
        tombstones = [schemas.Tombstone.from_mapping(w.payload) for w in self._states["tombstones"].records
                      if w.kind == "tombstone" and w.payload.get("transaction_id") == transaction_id]
        if len(results) + len(tombstones) != 1:
            return False
        # Terminal/tombstone association exact: the endpoint result scope/key/
        # fingerprint/transaction must match the audit authority (Fix #5).
        endpoint = results[0] if results else tombstones[0]
        if endpoint.scope != audit.endpoint_scope or endpoint.key_digest != audit.idempotency_key_digest:
            return False
        if endpoint.request_fingerprint != audit.request_fingerprint or endpoint.transaction_id != transaction_id:
            return False
        receipt_rows: dict[str, list[schemas.OutboxReceipt]] = {
            outbox_id: [] for outbox_id in audit.outbox_ids
        }
        for wrapper in self._states["outbox"].records:
            if wrapper.kind == "outbox" and wrapper.payload.get("outbox_id") in receipt_rows:
                receipt = schemas.OutboxReceipt.from_mapping(wrapper.payload)
                receipt_rows[receipt.outbox_id].append(receipt)
        for rows in receipt_rows.values():
            if not rows or any(row.aggregate_id != audit.proposal_id
                               or row.mutation_intent_id != transaction_id for row in rows):
                return False
            seen_appended = False
            attempts = -1
            for row in rows:
                if row.state not in {"pending", "appended"} or row.attempts < attempts:
                    return False
                if seen_appended and row.state != "appended":
                    return False
                seen_appended = seen_appended or row.state == "appended"
                attempts = row.attempts
        if self._activity_link_verifier is None:
            return False
        try:
            if not self._activity_link_verifier.verify_activity_ids(transaction_id, audit.activity_ids):
                return False
        except Exception:
            return False
        return True

    def retire_wal(self, transaction_id: str) -> None:
        with self._lock.hold(self._lock_timeout):
            if not self._can_retire_wal_locked(transaction_id):
                raise RepositoryError("WAL retirement authority is incomplete")
            self._fs.unlink(f"transactions/{transaction_id}/wal.json")
            self._recovery = self._classify_wals_locked()

    def stop_for_repair(self) -> None:
        self._stopped = True

    def resume_after_recovery(self) -> None:
        self._startup_recovery()
        if any(s.blocked for k, s in self._states.items() if k != "corruption"):
            raise RepositoryCorruptError("repository remains blocked after repair")
        self._stopped = False

    def apply_repair(
        self,
        repair: schemas.Repair | Mapping[str, Any],
        successor: schemas.CorruptionMarker | Mapping[str, Any],
        *,
        replacement_bytes: bytes | None = None,
    ) -> None:
        """Append explicit repair + successor marker while repository is stopped.

        Operator authorization and replacement-byte selection stay outside this
        package.  This primitive enforces stopped state, repository identity,
        current marker identity, and successor immutability.
        """
        if not self._stopped:
            raise RepositoryError("repository must be stopped before repair")
        rep = repair if isinstance(repair, schemas.Repair) else schemas.Repair.from_mapping(repair)
        suc = successor if isinstance(successor, schemas.CorruptionMarker) else schemas.CorruptionMarker.from_mapping(successor)
        if rep.repository_id != self.repository_id or suc.repository_id != self.repository_id:
            raise RepositoryError("repair repository identity mismatch")
        if rep.corruption_id != suc.corruption_id or suc.repair_record_id != rep.repair_record_id or suc.repaired_at is None:
            raise RepositoryError("repair successor linkage mismatch")
        with self._lock.hold(self._lock_timeout):
            current = [schemas.CorruptionMarker.from_mapping(w.payload) for w in self._states["corruption"].records
                       if w.kind == "corruption" and w.payload.get("corruption_id") == rep.corruption_id]
            if not current or current[-1].repaired_at is not None:
                raise RepositoryError("original corruption marker is not current")
            before = current[-1]
            immutable = ("corruption_id", "repository_id", "authority_kind", "authority_identity",
                         "reason", "observed_digest", "diagnostic_copy", "detected_at")
            if any(getattr(before, n) != getattr(suc, n) for n in immutable):
                raise RepositoryError("successor marker changed immutable corruption evidence")
            if rep.action == "classify_indeterminate":
                classification = rep.terminal_classification
                if classification is None:
                    raise RepositoryError("classification repair is missing terminal authority")
                identity = before.authority_identity
                if getattr(identity, "kind", None) == "endpoint_result":
                    if (identity.scope != classification.scope
                            or identity.key_digest != classification.key_digest):
                        raise RepositoryError(
                            "classification does not match repaired endpoint marker")
                elif getattr(identity, "kind", None) == "transaction_file":
                    if identity.transaction_id != classification.transaction_id:
                        raise RepositoryError(
                            "classification does not match repaired transaction marker")
                else:
                    raise RepositoryError(
                        "classification repair requires endpoint or transaction authority")
            if rep.action == "replace_authority":
                if replacement_bytes is None or rep.replacement_authority is None:
                    raise RepositoryError("replacement repair requires supplied authority bytes")
                import hashlib
                digest = "sha256:" + hashlib.sha256(replacement_bytes).hexdigest()
                replacement = rep.replacement_authority
                if replacement.size != len(replacement_bytes):
                    raise RepositoryError("replacement authority size mismatch")
                if replacement.digest != digest:
                    raise RepositoryError("replacement authority digest mismatch")
                if replacement.source_provenance.source_digest != digest:
                    raise RepositoryError("replacement source provenance digest mismatch")
                marker_path = getattr(before.authority_identity, "relative_path", None)
                if marker_path != replacement.relative_path:
                    raise RepositoryError("replacement path does not match corruption authority")
                self._validate_replacement_bytes(before, replacement_bytes)
                self._ensure_replacement_quota_locked(replacement.relative_path, replacement_bytes)
                self._fs.write_atomic(replacement.relative_path, replacement_bytes)
                reread = self._fs.read_bytes(replacement.relative_path)
                if "sha256:" + hashlib.sha256(reread).hexdigest() != digest:
                    raise RepositoryCorruptError("replacement authority reread verification failed")
            elif replacement_bytes is not None:
                raise RepositoryError("classification repair cannot supply replacement bytes")
            self._append_locked("corruption", "repair", rep.to_mapping(), permit_blocked=True)
            self._append_locked("corruption", "corruption", suc.to_mapping(), permit_blocked=True)
            self._write_checkpoint_locked("corruption")

    def _closed_repair_classifications_locked(
        self,
    ) -> tuple[schemas.TerminalClassification, ...]:
        """Validate repair chains and project closed classification authority."""
        originals: dict[str, schemas.CorruptionMarker] = {}
        pending: dict[str, schemas.Repair] = {}
        closed: set[str] = set()
        classifications: list[schemas.TerminalClassification] = []
        immutable = ("corruption_id", "repository_id", "authority_kind",
                     "authority_identity", "reason", "observed_digest",
                     "diagnostic_copy", "detected_at")
        for wrapper in self._states["corruption"].records:
            if wrapper.kind == "repair":
                repair = schemas.Repair.from_mapping(wrapper.payload)
                original = originals.get(repair.corruption_id)
                if (original is None or repair.corruption_id in pending
                        or repair.corruption_id in closed
                        or repair.repository_id != self.repository_id):
                    raise RepositoryCorruptError("invalid or duplicate repair chain")
                pending[repair.corruption_id] = repair
                continue
            marker = schemas.CorruptionMarker.from_mapping(wrapper.payload)
            if marker.repository_id != self.repository_id:
                raise RepositoryCorruptError("corruption marker repository mismatch")
            if marker.repaired_at is None:
                if marker.corruption_id in originals:
                    raise RepositoryCorruptError("duplicate open corruption marker")
                originals[marker.corruption_id] = marker
                continue
            original = originals.get(marker.corruption_id)
            repair = pending.get(marker.corruption_id)
            if (original is None or repair is None
                    or marker.repair_record_id != repair.repair_record_id
                    or any(getattr(original, name) != getattr(marker, name)
                           for name in immutable)):
                raise RepositoryCorruptError("invalid repair successor chain")
            if repair.action == "classify_indeterminate":
                classification = repair.terminal_classification
                if classification is None:
                    raise RepositoryCorruptError("classification repair is incomplete")
                identity = original.authority_identity
                if (getattr(identity, "kind", None) == "endpoint_result"
                        and (identity.scope != classification.scope
                             or identity.key_digest != classification.key_digest)):
                    raise RepositoryCorruptError("classification/marker scope identity mismatch")
                if (getattr(identity, "kind", None) == "transaction_file"
                        and identity.transaction_id != classification.transaction_id):
                    raise RepositoryCorruptError("classification/marker transaction mismatch")
                if getattr(identity, "kind", None) not in {"endpoint_result", "transaction_file"}:
                    raise RepositoryCorruptError("classification repair has unbound authority")
                classifications.append(classification)
            closed.add(marker.corruption_id)
            pending.pop(marker.corruption_id)
        if pending:
            raise RepositoryCorruptError("repair record has no successor marker")
        return tuple(classifications)

    def _validate_replacement_bytes(
        self, marker: schemas.CorruptionMarker, data: bytes
    ) -> None:
        identity = marker.authority_identity
        try:
            if marker.authority_kind == "aggregate":
                if data and not data.endswith(b"\n"):
                    raise IntegrityError("replacement JSONL has an incomplete tail")
                previous: int | None = None
                for line in data.splitlines():
                    mapping = loads_canonical(line)
                    if dumps_canonical(mapping) != line:
                        raise IntegrityError("replacement JSONL is not canonical")
                    wrapper = schemas.Wrapper.from_mapping(mapping)
                    if _KIND_AGGREGATE[wrapper.kind] != identity.aggregate:
                        raise IntegrityError("replacement wrapper topology mismatch")
                    if previous is not None and wrapper.sequence != previous + 1:
                        raise IntegrityError("replacement JSONL sequence gap")
                    _PAYLOAD_PARSER[wrapper.kind].from_mapping(wrapper.payload)
                    previous = wrapper.sequence
            elif marker.authority_kind == "checkpoint":
                checkpoint = schemas.Checkpoint.from_json(data)
                if checkpoint.repository_id != self.repository_id or checkpoint.kind != identity.aggregate:
                    raise IntegrityError("replacement checkpoint identity mismatch")
            elif marker.authority_kind == "preview":
                preview = schemas.Preview.from_json(data)
                if preview.preview_digest != identity.preview_digest:
                    raise IntegrityError("replacement preview identity mismatch")
            elif marker.authority_kind == "endpoint_result":
                result = schemas.EndpointResult.from_json(data)
                if result.scope != identity.scope or result.key_digest != identity.key_digest:
                    raise IntegrityError("replacement endpoint-result identity mismatch")
            elif marker.authority_kind == "repository":
                root = schemas.RepositoryRoot.from_json(data)
                if root.repository_id != self.repository_id:
                    raise IntegrityError("replacement repository identity mismatch")
            elif marker.authority_kind == "transaction_file":
                if identity.component == "wal":
                    schemas.WAL.from_json(data)
                elif identity.component == "history":
                    schemas.HistoryManifest.from_json(data)
                else:
                    # Stage/backup leaves are arbitrary content bytes whose
                    # digest and size are the complete authority validation.
                    pass
        except Exception as exc:
            if isinstance(exc, RepositoryError):
                raise
            raise RepositoryError("replacement authority failed closed validation") from exc


__all__ = [
    "REPOSITORY_DIR", "ACTIVE_LOG_BYTES", "MAX_GENERATIONS",
    "RepositoryLimits", "AggregateHighWater", "ProposalRepositorySnapshot",
    "RecoveryItem", "RecoveryReport", "Reconciliation", "ProposalRepositoryV1",
]
