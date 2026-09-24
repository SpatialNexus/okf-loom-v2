"""Pure, deterministic projectors for Proposal / Diff Review v1 (Foundation B).

Every function in this module is side-effect-free: it derives state from
explicit immutable inputs and never touches the clock (an explicit ``now`` is
supplied), the filesystem, the environment, the network, or any
server/Studio/CLI/render/viewer/Bundle code.  Impossible combinations raise
bounded :mod:`errors` domain errors rather than fabricating state.

The projectors own (SPEC section 7):

* explicit-decision selection (latest decision row per operation),
* local operation validity (structural consistency with the expected snapshot),
* transitive blockers with deterministic shortest ``op_id`` paths,
* conflict projection (read/write/environment mismatch),
* the closed effective-state precedence,
* proposal lifecycle precedence and transition guards,
* expiry boundaries,
* nonterminal WAL -> transaction-progress mapping,
* the apply-enable tuple,
* the read-model token,
* the audit projection, and
* preview/runtime freshness.

They do **not** capture snapshots, publish previews, run handlers, mutate
storage, emit events, or render anything.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from . import schemas
from . import ports as _ports
from .canonical import canonical_digest
from .errors import InvalidStateError, ProjectionError

# Pure modules type against the structural ``BundleView`` protocol; tests
# provide their own immutable fake/value.  No production Bundle adapter is
# imported by this pure layer (Req #7).
BundleView = _ports.BundleView

# ---------------------------------------------------------------------------
# Closed enums / precedence (SPEC section 7.2 / 7.3)
# ---------------------------------------------------------------------------

#: Per-operation local validity (SPEC 7.3).
LOCAL_VALIDITY_VALUES: frozenset[str] = frozenset({"valid", "invalid"})

#: Explicit decision values; ``undecided`` is the derived absence of a row.
EXPLICIT_DECISION_VALUES: frozenset[str] = frozenset({"undecided", "accepted", "rejected"})

#: Closed effective-state enum (SPEC 7.3).
EFFECTIVE_STATES: frozenset[str] = frozenset({
    "invalid", "conflicted", "rejected", "blocked", "accepted", "undecided",
})

#: Effective states that are intrinsic root causes of blocking (not accepted,
#: not themselves blocked).  Used to terminate blocker-path traversal.
_ROOT_BLOCKER_STATES: frozenset[str] = frozenset({
    "invalid", "conflicted", "rejected", "undecided",
})

#: Closed persisted/read-model lifecycle enum (SPEC 7.2).
LIFECYCLE_STATES: frozenset[str] = frozenset({
    "in_review", "ready", "conflicted", "applied", "withdrawn",
    "superseded", "expired", "audit_expired",
})

#: Terminal revision states (SPEC 7.2).
TERMINAL_STATES: frozenset[str] = frozenset({
    "applied", "withdrawn", "superseded", "expired", "audit_expired",
})

#: Live review states that may be superseded / withdrawn / decided (SPEC 7.2).
REVIEW_STATES: frozenset[str] = frozenset({"in_review", "ready", "conflicted"})

#: Lifecycle precedence, first match wins (SPEC 7.2 line 597).
LIFECYCLE_PRECEDENCE: tuple[str, ...] = (
    "audit_expired", "applied", "withdrawn", "superseded", "expired",
    "conflicted", "ready", "in_review",
)

#: Proposal-level conflict codes (op_id and concept may both be null).
_PROPOSAL_LEVEL_CODES: frozenset[str] = frozenset({
    "snapshot_changed", "capability_changed", "config_changed",
    "preview_expired", "validation_stale",
})

#: Mutation events guarded by lifecycle transition rules (SPEC 7.2).
MUTATION_EVENTS: frozenset[str] = frozenset({
    "decide", "withdraw", "supersede", "apply", "undo",
})

#: Nonterminal WAL phases that map to a transaction-progress projection.
_NONTERMINAL_PHASES: frozenset[str] = frozenset({
    "prepared", "commit_decided", "publishing", "content_published",
    "finalizing", "rolling_back", "indeterminate",
})

#: ``retry_after_ms`` for every nonterminal non-indeterminate projection.
PROGRESS_RETRY_AFTER_MS: int = 500


# ---------------------------------------------------------------------------
# Operation projection record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationState:
    """One row of ``operation_states`` in the canonical read model (SPEC 5.7)."""

    op_id: str
    local_validity: str
    explicit_decision: str
    blockers: tuple[tuple[str, ...], ...]
    conflicts: tuple[dict[str, Any], ...]
    effective_state: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "local_validity": self.local_validity,
            "explicit_decision": self.explicit_decision,
            "blockers": [list(p) for p in self.blockers],
            "conflicts": [dict(c) for c in self.conflicts],
            "effective_state": self.effective_state,
        }


# ---------------------------------------------------------------------------
# Explicit decisions (SPEC 5.4 / 7.1 STATE-03)
# ---------------------------------------------------------------------------


def latest_decision_rows(
    decision_rows: Sequence[schemas.DecisionRow | Mapping[str, Any]],
    proposal_id: str,
    proposal_revision: int,
) -> dict[str, schemas.DecisionRow]:
    """Select the latest decision row per ``op_id`` for one immutable revision.

    Decision rows are append-only full records (SPEC 5.4); the latest row for
    an operation owns the explicit decision.  Rows are coalesced by ``op_id``
    keeping the highest wrapper ``sequence`` when wrappers are supplied, else
    the last occurrence.  Rows for other proposals/revisions are ignored.
    """
    selected: dict[str, tuple[int, schemas.DecisionRow]] = {}
    seq = 0
    for row in decision_rows:
        if isinstance(row, schemas.DecisionRow):
            parsed = row
            seq += 1
        else:
            parsed = schemas.DecisionRow.from_mapping(row, path="decision")
            seq = int(row.get("sequence", seq + 1)) if isinstance(row, Mapping) else seq + 1
        if parsed.proposal_id != proposal_id or parsed.proposal_revision != proposal_revision:
            continue
        cur = selected.get(parsed.op_id)
        if cur is None or seq >= cur[0]:
            selected[parsed.op_id] = (seq, parsed)
    return {op_id: entry[1] for op_id, entry in selected.items()}


def explicit_decision_map(
    latest_rows: Mapping[str, schemas.DecisionRow],
) -> dict[str, str]:
    """Map ``op_id`` -> explicit decision value; absence is ``undecided``."""
    return {op_id: row.decision for op_id, row in latest_rows.items()}


def decision_set_tuples(
    operations: Sequence[schemas.Operation | Mapping[str, Any]],
    latest_rows: Mapping[str, schemas.DecisionRow],
) -> list[list[str | None]]:
    """Sorted ``[op_id, current_row_token-or-null]`` tuples (SPEC 5.4)."""
    rows_by_op = {op_id: row for op_id, row in latest_rows.items()}
    tuples: list[list[str | None]] = []
    for op in operations:
        op_id = op.op_id if isinstance(op, schemas.Operation) else op["op_id"]
        row = rows_by_op.get(op_id)
        tuples.append([op_id, row.row_token if row is not None else None])
    return sorted(tuples, key=lambda t: t[0])


# ---------------------------------------------------------------------------
# Local validity (SPEC 7.3 / STATE-04)
# ---------------------------------------------------------------------------


def derive_local_validity(
    operation: schemas.Operation | Mapping[str, Any],
    expected_snapshot: schemas.Snapshot | Mapping[str, Any],
) -> str:
    """Structural local validity of one operation against its expected snapshot.

    An operation is locally ``valid`` only when every declared read/write
    concept exists in the expected snapshot and the operation's
    ``expected_revisions`` exactly equal the snapshot's content revisions for
    those concepts.  This is a pure, deterministic check over immutable
    proposal authority; it never consults the *current* bundle (that signal is
    a conflict, not local validity).  Any inconsistency is ``invalid``.
    """
    op = operation if isinstance(operation, schemas.Operation) else schemas.Operation.from_mapping(operation)
    snap = (expected_snapshot if isinstance(expected_snapshot, schemas.Snapshot)
            else schemas.Snapshot.from_mapping(expected_snapshot))
    snap_revs = {entry.concept: entry.revision for entry in snap.concepts}
    concepts = sorted(set(op.reads) | set(op.writes))
    for concept in concepts:
        if concept not in snap_revs:
            return "invalid"
        if op.expected_revisions.get(concept) != snap_revisions_value(snap_revs[concept]):
            return "invalid"
    return "valid"


def snap_revisions_value(revision: str) -> str:
    """Normalize a snapshot inventory revision for comparison."""
    return revision


# ---------------------------------------------------------------------------
# Effective-state precedence (SPEC 7.3 / DEP-01..06)
# ---------------------------------------------------------------------------


def effective_state(
    local_validity: str,
    explicit_decision: str,
    blockers: Sequence[Sequence[str]] = (),
    conflicts: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Closed effective-state precedence (SPEC 7.3 table).

    ``invalid`` if locally invalid; otherwise ``conflicted`` if any conflict;
    otherwise ``rejected`` if explicitly rejected; otherwise ``blocked`` if any
    blocker; otherwise ``accepted`` if explicitly accepted; otherwise
    ``undecided``.  This is the single source of truth for DEP-01..06.
    """
    if local_validity == "invalid":
        return "invalid"
    if conflicts:
        return "conflicted"
    if explicit_decision == "rejected":
        return "rejected"
    if blockers:
        return "blocked"
    if explicit_decision == "accepted":
        return "accepted"
    return "undecided"


def _topological_order(
    operations: Sequence[schemas.Operation],
) -> list[schemas.Operation]:
    """Kahn topological order with ascending bytewise ``op_id`` among ready."""
    op_index = {op.op_id: op for op in operations}
    incoming = {op.op_id: 0 for op in operations}
    adj: dict[str, list[str]] = {op.op_id: [] for op in operations}
    for op in operations:
        for dep in op.depends_on:
            if dep in op_index:
                adj[dep].append(op.op_id)
                incoming[op.op_id] += 1
    ready = sorted(oid for oid, c in incoming.items() if c == 0)
    order: list[schemas.Operation] = []
    while ready:
        ready.sort()
        n = ready.pop(0)
        order.append(op_index[n])
        for m in adj[n]:
            incoming[m] -= 1
            if incoming[m] == 0:
                ready.append(m)
    if len(order) != len(operations):
        raise ProjectionError("operation depends_on must form a DAG")
    return order


def _blocker_paths(
    op_id: str,
    op_index: Mapping[str, schemas.Operation],
    effective_states: Mapping[str, str],
) -> tuple[tuple[str, ...], ...]:
    """Shortest, lexicographically-sorted ``op_id`` paths to root blockers.

    Traverses ``depends_on`` edges only through non-accepted operations and
    stops at root blockers (effective state in :data:`_ROOT_BLOCKER_STATES`).
    For each reachable root blocker the shortest path (ties broken
    lexicographically) is kept; the collection is returned sorted.
    """
    best: dict[str, tuple[str, ...]] = {}
    expanded: set[str] = set()
    queue: deque[tuple[str, tuple[str, ...]]] = deque()
    for dep in op_index[op_id].depends_on:
        if effective_states.get(dep) != "accepted":
            queue.append((dep, (op_id, dep)))
    while queue:
        node, path = queue.popleft()
        state = effective_states.get(node)
        if state in _ROOT_BLOCKER_STATES:
            cur = best.get(node)
            if cur is None or (len(path), path) < (len(cur), cur):
                best[node] = path
            continue
        # Non-accepted, non-root => must be "blocked"; expand through its deps.
        if node in expanded:
            continue
        expanded.add(node)
        for dep in op_index[node].depends_on:
            if effective_states.get(dep) != "accepted":
                queue.append((dep, path + (dep,)))
    return tuple(sorted(best.values()))


def project_operations(
    operations: Sequence[schemas.Operation | Mapping[str, Any]],
    *,
    local_validities: Mapping[str, str],
    explicit_decisions: Mapping[str, str],
    proposal_conflicts: Sequence[Mapping[str, Any]] = (),
    op_conflicts: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, OperationState]:
    """Project the closed per-operation state table (SPEC 7.3 / STATE-04).

    Computes local validity is taken from ``local_validities`` (callers may use
    :func:`derive_local_validity`), explicit decisions from
    ``explicit_decisions``, transitive blockers via deterministic shortest
    paths, and the closed effective-state precedence.  Returns a ``op_id`` ->
    :class:`OperationState` map.  Proposal-level conflicts apply to every
    operation; op-scoped conflicts apply to the named operation only.
    """
    parsed = [
        op if isinstance(op, schemas.Operation) else schemas.Operation.from_mapping(op)
        for op in operations
    ]
    if len({op.op_id for op in parsed}) != len(parsed):
        raise ProjectionError("operation op_id must be unique within a revision")
    op_index = {op.op_id: op for op in parsed}
    op_conflicts = op_conflicts or {}
    prop_conflicts = tuple(schemas.sort_conflicts(list(proposal_conflicts)))
    effective_states: dict[str, str] = {}
    blockers_by_op: dict[str, tuple[tuple[str, ...], ...]] = {}

    for op in _topological_order(parsed):
        lv = local_validities.get(op.op_id, "valid")
        if lv not in LOCAL_VALIDITY_VALUES:
            raise ProjectionError(f"local_validity must be one of {sorted(LOCAL_VALIDITY_VALUES)}")
        decision = explicit_decisions.get(op.op_id, "undecided")
        if decision not in EXPLICIT_DECISION_VALUES:
            raise ProjectionError(
                f"explicit_decision must be one of {sorted(EXPLICIT_DECISION_VALUES)}"
            )
        conflicts_for_op = prop_conflicts + tuple(
            schemas.sort_conflicts(list(op_conflicts.get(op.op_id, ())))
        )
        # Blockers are only meaningful below the rejected precedence level.
        if lv == "invalid" or conflicts_for_op or decision == "rejected":
            blockers: tuple[tuple[str, ...], ...] = ()
        else:
            blockers = _blocker_paths(op.op_id, op_index, effective_states)
        state = effective_state(lv, decision, blockers, conflicts_for_op)
        effective_states[op.op_id] = state
        blockers_by_op[op.op_id] = blockers

    return {
        op.op_id: OperationState(
            op_id=op.op_id,
            local_validity=local_validities.get(op.op_id, "valid"),
            explicit_decision=explicit_decisions.get(op.op_id, "undecided"),
            blockers=blockers_by_op[op.op_id],
            conflicts=tuple(schemas.sort_conflicts(
                list(prop_conflicts) + list(op_conflicts.get(op.op_id, ()))
            )),
            effective_state=effective_states[op.op_id],
        )
        for op in parsed
    }


def accepted_apply_set(states: Mapping[str, OperationState]) -> tuple[str, ...]:
    """The dependency-closed accepted operation set eligible for apply.

    Only operations whose effective state is exactly ``accepted`` participate
    (SPEC 7.3 / DEP-05).  Returned in canonical ascending ``op_id`` order.
    """
    return tuple(sorted(oid for oid, st in states.items() if st.effective_state == "accepted"))


# ---------------------------------------------------------------------------
# Conflict projection (SPEC 5.5 / 7.4 / CONFLICT-02..04)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConflictProjection:
    """Sorted conflict list plus per-op and proposal-level partitions."""

    conflicts: tuple[dict[str, Any], ...]
    proposal_conflicts: tuple[dict[str, Any], ...]
    op_conflicts: Mapping[str, tuple[dict[str, Any], ...]]

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)


def _conflict(
    *,
    code: str,
    proposal_id: str,
    proposal_revision: int,
    detected_at: str,
    expected: str,
    actual: str,
    message: str,
    op_id: str | None = None,
    concept: str | None = None,
) -> dict[str, Any]:
    return schemas.sort_conflicts([{
        "schema": 1,
        "code": code,
        "proposal_id": proposal_id,
        "proposal_revision": proposal_revision,
        "op_id": op_id,
        "concept": concept,
        "expected": expected,
        "actual": actual,
        "message": message,
        "detected_at": detected_at,
    }])[0]


def project_conflicts(
    proposal: schemas.ProposalRevision | Mapping[str, Any],
    current_bundle: BundleView,
    *,
    now: str,
    latest_decisions: Mapping[str, schemas.DecisionRow] | None = None,
) -> ConflictProjection:
    """Derive the sorted conflict list from immutable proposal + bundle facts.

    Compares the proposal's expected snapshot against the supplied current
    bundle view and the accepted operations' expected revisions against current
    concept revisions (SPEC 7.4).  Proposal-level codes
    (``snapshot_changed``/``capability_changed``/``config_changed``/
    ``preview_expired``) attach to every operation; op-scoped codes
    (``read_revision_changed``/``write_revision_changed``/``target_missing``)
    attach to the specific operation/concept.  Pure: never reads disk/clock.
    """
    rev = (proposal if isinstance(proposal, schemas.ProposalRevision)
           else schemas.ProposalRevision.from_mapping(proposal))
    latest_decisions = latest_decisions or {}
    snap = rev.expected_snapshot
    pid = rev.proposal_id
    prev = rev.proposal_revision
    proposal_level: list[dict[str, Any]] = []
    op_scoped: dict[str, list[dict[str, Any]]] = {}

    def add_proposal(code: str, expected: str, actual: str, message: str) -> None:
        proposal_level.append(_conflict(
            code=code, proposal_id=pid, proposal_revision=prev, detected_at=now,
            expected=expected, actual=actual, message=message,
        ))

    # Proposal-level environment mismatches (SPEC 7.4 CONFLICT-01/04).
    if snap.snapshot_digest != current_bundle.snapshot_digest:
        add_proposal(
            "snapshot_changed", snap.snapshot_digest, current_bundle.snapshot_digest,
            "Bundle snapshot changed since proposal creation.",
        )
    if tuple(snap.capabilities) != tuple(current_bundle.capabilities):
        add_proposal(
            "capability_changed",
            canonical_digest(list(snap.capabilities)),
            canonical_digest(list(current_bundle.capabilities)),
            "Active capability set changed since proposal creation.",
        )
    if snap.config_digest != current_bundle.config_digest:
        add_proposal(
            "config_changed", snap.config_digest, current_bundle.config_digest,
            "Effective configuration changed since proposal creation.",
        )
    if is_expired(rev.expires_at, now):
        add_proposal(
            "preview_expired", rev.expires_at, now,
            "Preview bytes have passed their exact expiry instant.",
        )

    # Runtime-identity drift invalidates preview/apply authority (STATE-15).
    for label, expected_id, actual_id in (
        ("validator_id", snap.validator_id, current_bundle.validator_id),
        ("serializer_id", snap.serializer_id, current_bundle.serializer_id),
        ("operation_runtime_id", snap.operation_runtime_id, current_bundle.operation_runtime_id),
    ):
        if expected_id != actual_id:
            add_proposal(
                "validation_stale", expected_id, actual_id,
                f"Runtime identity {label} changed since proposal creation.",
            )

    # Op-scoped read/write revision and target-missing checks for accepted ops.
    current_revs = dict(current_bundle.concept_revisions)
    for op in rev.operations:
        if op.op_id not in latest_decisions:
            continue
        if latest_decisions[op.op_id].decision != "accepted":
            continue
        for concept in op.reads:
            expected = op.expected_revisions.get(concept)
            actual = current_revs.get(concept)
            if concept not in current_revs:
                op_scoped.setdefault(op.op_id, []).append(_conflict(
                    code="target_missing", proposal_id=pid, proposal_revision=prev,
                    detected_at=now, expected=expected or "", actual="absent",
                    message=f"Read concept {concept} is missing from the current bundle.",
                    op_id=op.op_id, concept=concept,
                ))
            elif actual != expected:
                op_scoped.setdefault(op.op_id, []).append(_conflict(
                    code="read_revision_changed", proposal_id=pid, proposal_revision=prev,
                    detected_at=now, expected=expected or "", actual=actual or "",
                    message=f"Read revision for {concept} changed since proposal creation.",
                    op_id=op.op_id, concept=concept,
                ))
        for concept in op.writes:
            expected = op.expected_revisions.get(concept)
            actual = current_revs.get(concept)
            if concept not in current_revs:
                op_scoped.setdefault(op.op_id, []).append(_conflict(
                    code="target_missing", proposal_id=pid, proposal_revision=prev,
                    detected_at=now, expected=expected or "", actual="absent",
                    message=f"Write target {concept} is missing from the current bundle.",
                    op_id=op.op_id, concept=concept,
                ))
            elif actual != expected:
                op_scoped.setdefault(op.op_id, []).append(_conflict(
                    code="write_revision_changed", proposal_id=pid, proposal_revision=prev,
                    detected_at=now, expected=expected or "", actual=actual or "",
                    message=f"Write revision for {concept} changed since proposal creation.",
                    op_id=op.op_id, concept=concept,
                ))

    all_conflicts = list(proposal_level)
    for items in op_scoped.values():
        all_conflicts.extend(items)
    sorted_all = tuple(schemas.sort_conflicts(all_conflicts))
    sorted_proposal = tuple(schemas.sort_conflicts(proposal_level))
    sorted_op = {
        oid: tuple(schemas.sort_conflicts(items)) for oid, items in op_scoped.items()
    }
    return ConflictProjection(
        conflicts=sorted_all,
        proposal_conflicts=sorted_proposal,
        op_conflicts=MappingProxyType(sorted_op),
    )


# ---------------------------------------------------------------------------
# Proposal lifecycle (SPEC 7.2 / STATE-05 / LIFE-02..09)
# ---------------------------------------------------------------------------


def is_expired(expires_at: str, now: str) -> bool:
    """Exact expiry boundary: expired once ``now >= expires_at`` (SPEC 7.2)."""
    return now >= expires_at


def proposal_lifecycle_state(
    *,
    applied: bool = False,
    withdrawn: bool = False,
    superseded: bool = False,
    audit_expired: bool = False,
    expired: bool = False,
    has_conflict: bool = False,
    ready: bool = False,
) -> str:
    """First-matching lifecycle precedence (SPEC 7.2 line 597 / STATE-05)."""
    signals: list[tuple[str, bool]] = [
        ("audit_expired", audit_expired),
        ("applied", applied),
        ("withdrawn", withdrawn),
        ("superseded", superseded),
        ("expired", expired),
        ("conflicted", has_conflict),
        ("ready", ready),
        ("in_review", True),
    ]
    for state, flag in signals:
        if flag:
            if state not in LIFECYCLE_STATES:
                raise ProjectionError(f"unknown lifecycle state {state!r}")
            return state
    return "in_review"


#: Allowed source states for each mutation event (SPEC 7.2 table).
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "decide": REVIEW_STATES,
    "withdraw": REVIEW_STATES,
    "supersede": REVIEW_STATES,
    "apply": frozenset({"ready"}),
    "undo": frozenset({"applied"}),
}


def assert_mutation_allowed(current_state: str, event: str) -> None:
    """Guard a lifecycle mutation; raise :class:`InvalidStateError` if barred.

    Implements LIFE-09: terminal revisions cannot be superseded, withdrawn,
    decided, applied, or undone except that only an applied revision may enter
    the separately governed undo transaction (SPEC 7.2).
    """
    if event not in MUTATION_EVENTS:
        raise ProjectionError(f"unknown mutation event {event!r}")
    if current_state not in LIFECYCLE_STATES:
        raise ProjectionError(f"unknown lifecycle state {current_state!r}")
    allowed = _ALLOWED_TRANSITIONS[event]
    if current_state not in allowed:
        raise InvalidStateError(
            f"mutation {event!r} is not allowed from state {current_state!r}"
        )


# ---------------------------------------------------------------------------
# Nonterminal WAL -> transaction-progress projection (SPEC 5.7 / STATE-19)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransactionProgress:
    """One closed ``transaction_attempts`` projection (SPEC 5.7)."""

    schema: int
    operation: str
    transaction_id: str
    parent_transaction_id: str | None
    status: str
    phase: str
    commit_decided: bool
    completed_targets: int
    total_targets: int
    recovery_attempts: int
    started_at: str
    updated_at: str
    retry_after_ms: int | None
    error_code: str | None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "operation": self.operation,
            "transaction_id": self.transaction_id,
            "parent_transaction_id": self.parent_transaction_id,
            "status": self.status,
            "phase": self.phase,
            "commit_decided": self.commit_decided,
            "completed_targets": self.completed_targets,
            "total_targets": self.total_targets,
            "recovery_attempts": self.recovery_attempts,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "retry_after_ms": self.retry_after_ms,
            "error_code": self.error_code,
        }


def map_wal_to_progress(
    wal: schemas.WAL | Mapping[str, Any],
    *,
    parent_transaction_id: str | None = None,
) -> TransactionProgress | None:
    """Map a nonterminal WAL to one closed progress projection (SPEC 5.7).

    Terminal ``committed``/``rolled_back`` WALs are **not** progress
    projections and return ``None``.  Projection precedence: WAL
    ``indeterminate`` -> ``indeterminate``; otherwise ``recovery_attempts > 0``
    -> ``recovering``; otherwise the phase maps to its client-visible status.
    """
    parsed = wal if isinstance(wal, schemas.WAL) else schemas.WAL.from_mapping(wal)
    phase = parsed.phase
    if phase in ("committed", "rolled_back"):
        return None
    if phase not in _NONTERMINAL_PHASES:
        raise ProjectionError(f"WAL phase {phase!r} is not a progress projection source")

    total_targets = len(parsed.targets)
    if phase == "rolling_back":
        completed = sum(1 for b in parsed.rollback_bitmap if b)
    else:
        completed = sum(1 for b in parsed.publish_bitmap if b)

    if phase == "indeterminate":
        status = "indeterminate"
    elif parsed.recovery_attempts > 0:
        status = "recovering"
    elif phase == "prepared":
        status = "preparing"
    elif phase == "rolling_back":
        status = "rollback_pending"
    elif phase == "commit_decided":
        status = "forward_committed"
    elif phase == "publishing":
        status = "publishing"
    else:  # content_published | finalizing
        status = "finalizing"

    retry_after_ms: int | None = None if status == "indeterminate" else PROGRESS_RETRY_AFTER_MS
    error_code: str | None = None
    if status == "indeterminate":
        error_code = "indeterminate_outcome"
    elif status == "recovering":
        error_code = "transaction_recovering"

    return TransactionProgress(
        schema=1,
        operation=parsed.operation,
        transaction_id=parsed.transaction_id,
        parent_transaction_id=parent_transaction_id,
        status=status,
        phase=phase,
        commit_decided=parsed.commit_decided,
        completed_targets=completed,
        total_targets=total_targets,
        recovery_attempts=parsed.recovery_attempts,
        started_at=parsed.created_at,
        updated_at=parsed.updated_at,
        retry_after_ms=retry_after_ms,
        error_code=error_code,
    )


def sort_transaction_attempts(
    attempts: Iterable[TransactionProgress],
) -> tuple[TransactionProgress, ...]:
    """Sort progress projections by ``(started_at, transaction_id)`` descending."""
    return tuple(sorted(
        attempts,
        key=lambda a: (a.started_at, a.transaction_id),
        reverse=True,
    ))


# ---------------------------------------------------------------------------
# Apply-enable tuple (SPEC 7.1 STATE-16)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyEnableTuple:
    """The complete apply-enable authority tuple (SPEC 7.1 / 12 STATE-16).

    Any member change invalidates browser controls and requires a fresh
    canonical read.  This is display/cache authority only; it MUST NOT
    authorize proposal mutation by itself.
    """

    proposal_id: str
    proposal_revision: int
    read_model_token: str
    decision_set_token: str
    preview_digest: str
    snapshot_digest: str
    validation_context_digest: str
    validator_id: str
    serializer_id: str
    operation_runtime_id: str

    def as_token(self) -> str:
        """Canonical digest over the complete tuple (CACHE-04 mismatch proof)."""
        return canonical_digest({
            "proposal_id": self.proposal_id,
            "proposal_revision": self.proposal_revision,
            "read_model_token": self.read_model_token,
            "decision_set_token": self.decision_set_token,
            "preview_digest": self.preview_digest,
            "snapshot_digest": self.snapshot_digest,
            "validation_context_digest": self.validation_context_digest,
            "validator_id": self.validator_id,
            "serializer_id": self.serializer_id,
            "operation_runtime_id": self.operation_runtime_id,
        })


# ---------------------------------------------------------------------------
# Preview / runtime freshness (SPEC 6 / STATE-15)
# ---------------------------------------------------------------------------


def is_preview_fresh(
    proposal: schemas.ProposalRevision | Mapping[str, Any],
    current_bundle: BundleView,
    *,
    now: str,
    preview_digest: str | None = None,
) -> bool:
    """Preview freshness: all authority inputs match and expiry not passed.

    Preview is fresh only while the current snapshot digest, capability set,
    config digest, the three runtime identities, and the preview digest all
    match, and ``expires_at`` has not passed (SPEC section 6 line 562).
    """
    rev = (proposal if isinstance(proposal, schemas.ProposalRevision)
           else schemas.ProposalRevision.from_mapping(proposal))
    snap = rev.expected_snapshot
    if is_expired(rev.expires_at, now):
        return False
    if snap.snapshot_digest != current_bundle.snapshot_digest:
        return False
    if tuple(snap.capabilities) != tuple(current_bundle.capabilities):
        return False
    if snap.config_digest != current_bundle.config_digest:
        return False
    if snap.validation_context_digest != current_bundle.validation_context_digest:
        return False
    if snap.validator_id != current_bundle.validator_id:
        return False
    if snap.serializer_id != current_bundle.serializer_id:
        return False
    if snap.operation_runtime_id != current_bundle.operation_runtime_id:
        return False
    if preview_digest is not None and rev.preview_ref.preview_digest != preview_digest:
        return False
    return True


# ---------------------------------------------------------------------------
# Read-model token (SPEC 5.7 / 10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadModelTokenInputs:
    """Every normative authority input to the read-model token (SPEC 5.7/10)."""

    proposal_checksum: str
    decision_records: tuple[Mapping[str, Any], ...]
    transaction_records: tuple[Mapping[str, Any], ...]
    withdrawal_records: tuple[Mapping[str, Any], ...]
    service_authority: tuple[Mapping[str, Any], ...]
    preview_digest: str
    conflict_inputs_digest: str
    expires_at: str
    bundle_snapshot_digest: str
    config_digest: str
    validation_context_digest: str
    capabilities: tuple[str, ...]
    validator_id: str
    serializer_id: str
    operation_runtime_id: str


def compute_read_model_token(inputs: ReadModelTokenInputs) -> str:
    """Canonical digest over every normative read-model authority input.

    Hashes proposal revision checksum, current complete decision rows,
    applicable transaction/withdrawal records, proposal-specific service
    intent/result identities, preview digest, conflict input snapshot, exact expiry instant,
    bundle snapshot, config/capability set, and all three runtime identities
    (SPEC section 10 line 870).  Any change to any authority input changes the
    token; a stale token never merges stale controls.
    """
    body = {
        "proposal_checksum": inputs.proposal_checksum,
        "decision_records": [dict(r) for r in inputs.decision_records],
        "transaction_records": [dict(r) for r in inputs.transaction_records],
        "withdrawal_records": [dict(r) for r in inputs.withdrawal_records],
        "service_authority": [dict(r) for r in inputs.service_authority],
        "preview_digest": inputs.preview_digest,
        "conflict_inputs_digest": inputs.conflict_inputs_digest,
        "expires_at": inputs.expires_at,
        "bundle_snapshot_digest": inputs.bundle_snapshot_digest,
        "config_digest": inputs.config_digest,
        "validation_context_digest": inputs.validation_context_digest,
        "capabilities": list(inputs.capabilities),
        "validator_id": inputs.validator_id,
        "serializer_id": inputs.serializer_id,
        "operation_runtime_id": inputs.operation_runtime_id,
    }
    return canonical_digest(body)


def conflict_inputs_digest(
    *,
    bundle_snapshot_digest: str,
    config_digest: str,
    validation_context_digest: str,
    capabilities: Sequence[str],
    concept_revisions: Mapping[str, str],
    validator_id: str,
    serializer_id: str,
    operation_runtime_id: str,
) -> str:
    """Digest over the conflict-determining bundle/runtime inputs (SPEC 5.7)."""
    return canonical_digest({
        "bundle_snapshot_digest": bundle_snapshot_digest,
        "config_digest": config_digest,
        "validation_context_digest": validation_context_digest,
        "capabilities_digest": canonical_digest(list(capabilities)),
        "concept_revisions": dict(concept_revisions),
        "validator_id": validator_id,
        "serializer_id": serializer_id,
        "operation_runtime_id": operation_runtime_id,
    })


# ---------------------------------------------------------------------------
# Audit projection (SPEC 5.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditProjection:
    """Closed audit projection (SPEC 5.7)."""

    schema: int
    proposal_id: str
    proposal_revision: int
    proposal_record_id: str
    decision_record_ids: tuple[str, ...]
    withdrawal_record_id: str | None
    transaction_ids: tuple[str, ...]
    endpoint_result_digests: tuple[str, ...]
    validation_digest: str | None
    resulting_revisions: Mapping[str, str]
    activity_ids: tuple[str, ...]
    outbox_ids: tuple[str, ...]
    undo_group_id: str | None
    undo_transaction_ids: tuple[str, ...]
    retention: Mapping[str, str | None]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "proposal_id": self.proposal_id,
            "proposal_revision": self.proposal_revision,
            "proposal_record_id": self.proposal_record_id,
            "decision_record_ids": list(self.decision_record_ids),
            "withdrawal_record_id": self.withdrawal_record_id,
            "transaction_ids": list(self.transaction_ids),
            "endpoint_result_digests": list(self.endpoint_result_digests),
            "validation_digest": self.validation_digest,
            "resulting_revisions": dict(self.resulting_revisions),
            "activity_ids": list(self.activity_ids),
            "outbox_ids": list(self.outbox_ids),
            "undo_group_id": self.undo_group_id,
            "undo_transaction_ids": list(self.undo_transaction_ids),
            "retention": dict(self.retention),
        }


def build_audit_projection(
    *,
    proposal: schemas.ProposalRevision | Mapping[str, Any],
    decision_record_ids: Sequence[str] = (),
    withdrawal_record_id: str | None = None,
    transaction_ids: Sequence[str] = (),
    endpoint_result_digests: Sequence[str] = (),
    validation_digest: str | None = None,
    resulting_revisions: Mapping[str, str] | None = None,
    activity_ids: Sequence[str] = (),
    outbox_ids: Sequence[str] = (),
    undo_group_id: str | None = None,
    undo_transaction_ids: Sequence[str] = (),
    retention: Mapping[str, str | None] | None = None,
) -> AuditProjection:
    """Assemble the closed audit projection (SPEC section 5.7) from immutable links.

    Nullable singular ids appear only when that event never occurred.  All id
    arrays are canonicalized to sorted tuples.  Pure: no I/O or persistence.
    """
    rev = (proposal if isinstance(proposal, schemas.ProposalRevision)
           else schemas.ProposalRevision.from_mapping(proposal))
    return AuditProjection(
        schema=1,
        proposal_id=rev.proposal_id,
        proposal_revision=rev.proposal_revision,
        proposal_record_id=rev.record_id,
        decision_record_ids=tuple(sorted(decision_record_ids)),
        withdrawal_record_id=withdrawal_record_id,
        transaction_ids=tuple(sorted(transaction_ids)),
        endpoint_result_digests=tuple(endpoint_result_digests),
        validation_digest=validation_digest,
        resulting_revisions=MappingProxyType(dict(resulting_revisions or {})),
        activity_ids=tuple(sorted(activity_ids)),
        outbox_ids=tuple(sorted(outbox_ids)),
        undo_group_id=undo_group_id,
        undo_transaction_ids=tuple(sorted(undo_transaction_ids)),
        retention=MappingProxyType(dict(retention or {})),
    )


__all__ = [
    "LOCAL_VALIDITY_VALUES",
    "EXPLICIT_DECISION_VALUES",
    "EFFECTIVE_STATES",
    "LIFECYCLE_STATES",
    "TERMINAL_STATES",
    "REVIEW_STATES",
    "LIFECYCLE_PRECEDENCE",
    "MUTATION_EVENTS",
    "PROGRESS_RETRY_AFTER_MS",
    "OperationState",
    "latest_decision_rows",
    "explicit_decision_map",
    "decision_set_tuples",
    "derive_local_validity",
    "effective_state",
    "project_operations",
    "accepted_apply_set",
    "ConflictProjection",
    "project_conflicts",
    "is_expired",
    "proposal_lifecycle_state",
    "assert_mutation_allowed",
    "TransactionProgress",
    "map_wal_to_progress",
    "sort_transaction_attempts",
    "ApplyEnableTuple",
    "is_preview_fresh",
    "ReadModelTokenInputs",
    "compute_read_model_token",
    "conflict_inputs_digest",
    "AuditProjection",
    "build_audit_projection",
    "BundleView",
]
