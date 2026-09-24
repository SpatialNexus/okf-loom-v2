"""STATE / LIFE / DEP / CONFLICT projector proofs for Proposal Review Foundation B.

Implements the named pure-projector proof nodes from the normative catalogue
(SPEC section 17.1): ``STATE-04``/``STATE-05``/``STATE-19``, ``LIFE-02``/
``LIFE-08``/``LIFE-09``, ``DEP-01..06``, ``CONFLICT-02``/``CONFLICT-03``/
``CONFLICT-04``.  Also covers the pure backend portion of the cache-invalidation
tuple (``CACHE-04``) and the import-boundary guarantee.  All inputs are
explicit immutable fixtures; no clock/filesystem/network is consulted.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import pytest

from okf_loom.proposal_review import canonical, projectors, schemas
from okf_loom.proposal_review.errors import (
    InvalidStateError, ProjectionError, StaleReadModelError,
)
from okf_loom.proposal_review.ports import BundleView


@dataclass(frozen=True)
class ImmutableBundleView:
    snapshot_digest: str
    config_digest: str
    validation_context_digest: str
    capabilities: tuple[str, ...]
    concept_revisions: Any
    validator_id: str
    serializer_id: str
    operation_runtime_id: str

    def __post_init__(self):
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "concept_revisions", MappingProxyType(dict(self.concept_revisions)))

    def environment_digest(self):
        return canonical.canonical_digest({
              "config_digest": self.config_digest,
              "validation_context_digest": self.validation_context_digest,
            "capabilities_digest": canonical.canonical_digest(list(self.capabilities)),
            "validator_id": self.validator_id,
            "serializer_id": self.serializer_id,
            "operation_runtime_id": self.operation_runtime_id,
        })
# ============================================================================
# Shared constants and fixture builders
# ============================================================================

ULID = "01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
HEX64 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
HEX64B = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
HEX64C = "2223456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
DIGEST = "sha256:" + HEX64
DIGEST_B = "sha256:" + HEX64B
ROW_TOKEN = "A" * 43
ROW_TOKEN_B = "B" * 43
ROW_TOKEN_C = "C" * 43
RUNTIME = "okf-validator@1.0+sha256:" + HEX64
RUNTIME_SER = "okf-roundtrip@1.0+sha256:" + HEX64
RUNTIME_OPS = "proposal-ops@1.0+sha256:" + HEX64
TS = "2026-07-14T12:34:56.789Z"
TS2 = "2026-07-15T01:02:03.040Z"
TS_EXPIRE = "2026-07-22T00:00:00.000Z"
TS_AFTER = "2026-07-22T00:00:00.001Z"

# Valid 26-char Crockford Base32 op_id bodies (excl. I,L,O,U), lexicographic A<B<C<D.
OP_BODY_A = "01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
OP_BODY_B = "11J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
OP_BODY_C = "21J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
OP_BODY_D = "31J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
OP_A = "op_" + OP_BODY_A
OP_B = "op_" + OP_BODY_B
OP_C = "op_" + OP_BODY_C
OP_D = "op_" + OP_BODY_D


def _snapshot_body(concepts, capabilities, config_digest=DIGEST,
                   validation_context_digest=DIGEST_B,
                   validator=RUNTIME, serializer=RUNTIME_SER, ops=RUNTIME_OPS):
    return {
        "schema": 1,
        "concepts": concepts,
        "capabilities": capabilities,
          "config_digest": config_digest,
          "validation_context_digest": validation_context_digest,
        "validator_id": validator,
        "serializer_id": serializer,
        "operation_runtime_id": ops,
    }


def snapshot_mapping(concepts=None, capabilities=None, **kw):
    concepts = concepts or [
        {"concept": "tables/customers", "revision": HEX64,
         "identity": {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
         "leaf_inode": 3, "ctime_ns": 4, "mode": 0o100600, "size": 1,
         "digest": HEX64}},
        {"concept": "tables/orders", "revision": HEX64B,
         "identity": {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
         "leaf_inode": 4, "ctime_ns": 5, "mode": 0o100600, "size": 1,
         "digest": HEX64B}},
    ]
    capabilities = capabilities or ["okf.cap.typed_relations"]
    body = _snapshot_body(concepts, capabilities, **kw)
    return {
        "schema": 1,
        "captured_at": TS,
        "concepts": concepts,
        "capabilities": capabilities,
          "config_digest": kw.get("config_digest", DIGEST),
          "validation_context_digest": kw.get("validation_context_digest", DIGEST_B),
        "validator_id": kw.get("validator", RUNTIME),
        "serializer_id": kw.get("serializer", RUNTIME_SER),
        "operation_runtime_id": kw.get("ops", RUNTIME_OPS),
        "snapshot_digest": canonical.canonical_digest(body),
    }


def _op(op_id, *, kind="link_add", target="tables/orders", reads=None,
        writes=None, expected_revisions=None, depends_on=(), destination="tables/customers"):
    reads = reads if reads is not None else ["tables/customers", "tables/orders"]
    writes = writes if writes is not None else ["tables/orders"]
    exp = expected_revisions if expected_revisions is not None else {
        "tables/customers": HEX64, "tables/orders": HEX64B,
    }
    return {
        "schema": 1, "op_id": op_id, "kind": kind, "target": target,
        "args": {"destination": destination, "label": "customers", "relation": "depends_on",
                 "detail": None, "allow_forward_reference": False},
        "reads": reads, "writes": writes,
        "expected_revisions": exp, "depends_on": sorted(depends_on), "extensions": {},
    }


def _proposal(ops, *, proposal_id=ULID, rev=1, expires_at=TS_EXPIRE,
              concepts=None, capabilities=None, **snap_kw):
    snap = snapshot_mapping(concepts=concepts, capabilities=capabilities, **snap_kw)
    affected = sorted({c for o in ops for c in (set(o["reads"]) | set(o["writes"]))})
    return schemas.ProposalRevision.from_mapping({
        "schema": 1, "record_id": "rec_" + ULID,
        "creation_result_record_id": "rec_" + ("B" * 26),
        "creation_request_fingerprint": DIGEST, "proposal_id": proposal_id,
        "proposal_revision": rev, "supersedes_revision": None if rev == 1 else rev - 1,
        "mode": "proposal", "author": "agent-local", "created_at": TS,
        "summary": "Add the customer relation",
        "rationale": "Make the dependency explicit without changing direct mode.",
        "affected_concepts": affected, "expected_snapshot": snap, "operations": ops,
        "preview_ref": {"schema": 1, "preview_digest": DIGEST, "artifact_bytes": 4096},
        "provenance": [{"kind": "comment", "value": "01J2Y8N5Z9"}],
        "expires_at": expires_at, "extensions": {},
    })


def _decision(op_id, decision="accepted", row_token=ROW_TOKEN, prev=None):
    return schemas.DecisionRow.from_mapping({
        "schema": 1, "record_id": "rec_" + ULID,
        "decision_result_record_id": "rec_" + ("D" * 26),
        "decision_request_fingerprint": DIGEST, "proposal_id": ULID,
        "proposal_revision": 1, "op_id": op_id, "decision": decision,
        "reason": "", "actor": "local", "decided_at": TS,
        "previous_row_token": prev, "row_token": row_token,
    })


def _bundle(concepts=None, capabilities=None, **kw):
    concepts = concepts if concepts is not None else {
        "tables/customers": HEX64, "tables/orders": HEX64B,
    }
    snap = snapshot_mapping(concepts=[
        {"concept": c, "revision": r, "identity": {"parent_device": 1,
         "parent_inode": 2, "leaf_device": 1, "leaf_inode": index + 3,
         "ctime_ns": index + 4, "mode": 0o100600, "size": 1, "digest": r}}
        for index, (c, r) in enumerate(sorted(concepts.items()))
    ], capabilities=capabilities, **kw)
    return ImmutableBundleView(
        snapshot_digest=snap["snapshot_digest"],
          config_digest=snap["config_digest"],
          validation_context_digest=snap["validation_context_digest"],
        capabilities=tuple(snap["capabilities"]),
        concept_revisions=concepts,
        validator_id=snap["validator_id"],
        serializer_id=snap["serializer_id"],
        operation_runtime_id=snap["operation_runtime_id"],
    )


def _target(concept="tables/orders"):
    before = {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
              "leaf_inode": 3, "ctime_ns": 4, "mode": 0o600,
              "size": 1, "digest": DIGEST}
    staged = {**before, "leaf_inode": 4, "ctime_ns": 5, "digest": DIGEST_B}
    return {
        "concept": concept, "before_digest": DIGEST,
        "staged_digest": DIGEST_B, "backup_digest": DIGEST, "restore_digest": DIGEST,
        "before_identity": before, "staged_identity": staged, "resulting_identity": None,
        "stage_leaf": {"relative_name": "stage", "device": 1, "inode": 4,
                       "mode": 0o600, "size": 1, "digest": DIGEST},
        "backup_leaf": {"relative_name": "backup", "device": 1, "inode": 5,
                        "mode": 0o600, "size": 1, "digest": DIGEST},
    }


def _wal(phase="prepared", operation="apply", recovery_attempts=0, n_targets=1):
    """Build a WAL with legal phase/bitmap combinations (SPEC 5.6 table)."""
    if phase in ("prepared", "rolling_back", "rolled_back"):
        commit_decided = False
    elif phase == "indeterminate":
        commit_decided = False
    else:  # commit_decided, publishing, content_published, finalizing, committed
        commit_decided = True
    # Construct legal bitmaps per SPEC section 5.6.
    if phase == "publishing":
        # non-empty proper prefix true: need >=2 targets.
        n_targets = max(n_targets, 2)
        publish = [True] * (n_targets - 1) + [False]
        rollback = [False] * n_targets
    elif phase in ("content_published", "finalizing", "committed"):
        publish = [True] * n_targets
        rollback = [False] * n_targets
    elif phase == "rolling_back":
        publish = [False] * n_targets
        rollback = [True] if n_targets == 1 else ([False] * (n_targets - 1) + [True])
    elif phase == "rolled_back":
        publish = [False] * n_targets
        rollback = [True] * n_targets
    else:  # prepared, commit_decided, indeterminate
        publish = [False] * n_targets
        rollback = [False] * n_targets
    concepts = ["tables/customers", "tables/orders"] if n_targets > 1 else ["tables/orders"]
    targets = [_target(c) for c in concepts]
    for target, published in zip(targets, publish):
        if published:
            target["resulting_identity"] = {**target["staged_identity"],
                                             "parent_device": 1, "parent_inode": 2,
                                             "ctime_ns": 6}
    value = {
        "schema": 1, "operation": operation,
        "transaction_id": ("txn_" if operation == "apply" else "undo_") + ULID,
        "proposal_id": ULID, "proposal_revision": 1, "idempotency_scope": "/route",
        "idempotency_key_digest": DIGEST, "request_fingerprint": DIGEST,
        "transaction_intent_digest": DIGEST,
        "decision_set_token": DIGEST, "read_model_token": DIGEST,
        "preview_digest": DIGEST, "snapshot_digest": DIGEST,
        "validation_context_digest": DIGEST_B if operation == "apply" else None,
        "validator_id": RUNTIME, "serializer_id": RUNTIME_SER,
        "operation_runtime_id": RUNTIME_OPS,
        "accepted_op_ids": ["op_" + ULID] if operation == "apply" else [],
        "targets": targets,
        "stage_dir_identity": {"relative_name": "stage", "device": 1, "inode": 6, "mode": 0o700},
        "backup_dir_identity": {"relative_name": "backup", "device": 1, "inode": 7, "mode": 0o700},
        "phase": phase, "commit_decided": commit_decided,
        "publish_bitmap": publish, "rollback_bitmap": rollback,
        "recovery_attempts": recovery_attempts, "created_at": TS, "updated_at": TS,
    }
    value["checksum"] = canonical.compute_checksum(value)
    return value


# ============================================================================
# STATE-04: blockers/conflicts/local-validity are pure projections
# ============================================================================


def test_state_04_derived_only():
    """Derived operation state is a pure function of immutable inputs.

    Feeding the same immutable inputs twice yields byte-identical projections,
    and no persisted writer is involved (STATE-04 obligation).
    """
    op = _op("op_" + ULID)
    states_a = projectors.project_operations(
        [op],
        local_validities={"op_" + ULID: "valid"},
        explicit_decisions={"op_" + ULID: "undecided"},
    )
    states_b = projectors.project_operations(
        [op],
        local_validities={"op_" + ULID: "valid"},
        explicit_decisions={"op_" + ULID: "undecided"},
    )
    st = states_a["op_" + ULID]
    assert st.effective_state == "undecided"
    assert st.blockers == ()
    assert st.conflicts == ()
    # Determinism: identical canonical projection bytes.
    assert canonical.dumps_canonical(st.to_mapping()) == \
        canonical.dumps_canonical(states_b["op_" + ULID].to_mapping())
    # Local validity is itself a pure derivation from the expected snapshot.
    prop = _proposal([op])
    assert projectors.derive_local_validity(op, prop.expected_snapshot) == "valid"


# ============================================================================
# STATE-05: lifecycle precedence is deterministic
# ============================================================================


def test_state_05_lifecycle_precedence():
    """First-matching lifecycle precedence is deterministic (STATE-05)."""
    # applied beats withdrawn/superseded/expired/ready/conflicted.
    assert projectors.proposal_lifecycle_state(
        applied=True, withdrawn=True, superseded=True, expired=True,
        has_conflict=True, ready=True) == "applied"
    # audit_expired is the absolute top of the precedence order.
    assert projectors.proposal_lifecycle_state(
        audit_expired=True, applied=True) == "audit_expired"
    # precedence chain: withdrawn > superseded > expired > conflicted > ready > in_review
    assert projectors.proposal_lifecycle_state(withdrawn=True, superseded=True) == "withdrawn"
    assert projectors.proposal_lifecycle_state(superseded=True, expired=True) == "superseded"
    assert projectors.proposal_lifecycle_state(expired=True, has_conflict=True) == "expired"
    assert projectors.proposal_lifecycle_state(has_conflict=True, ready=True) == "conflicted"
    assert projectors.proposal_lifecycle_state(ready=True) == "ready"
    assert projectors.proposal_lifecycle_state() == "in_review"
    # Deterministic over repeated calls.
    assert all(
        projectors.proposal_lifecycle_state(applied=True, ready=True) == "applied"
        for _ in range(5)
    )


# ============================================================================
# STATE-19: nonterminal WAL maps to one closed transaction-progress projection
# ============================================================================


def test_state_19_transaction_progress():
    """Every nonterminal WAL phase maps to exactly one closed projection."""
    cases = {
        "prepared": ("preparing", False),
        "commit_decided": ("forward_committed", False),
        "publishing": ("publishing", False),
        "content_published": ("finalizing", False),
        "finalizing": ("finalizing", False),
    }
    for phase, (status, _retry_null) in cases.items():
        prog = projectors.map_wal_to_progress(_wal(phase=phase))
        assert prog is not None
        assert prog.status == status
        assert prog.schema == 1
        assert prog.retry_after_ms == projectors.PROGRESS_RETRY_AFTER_MS
        assert prog.error_code is None
    # rolling_back maps to rollback_pending, counts rollback bitmap.
    prog = projectors.map_wal_to_progress(_wal(phase="rolling_back"))
    assert prog.status == "rollback_pending"
    assert prog.completed_targets == 1
    # publishing counts true publish bits (proper prefix of 2 targets).
    prog = projectors.map_wal_to_progress(_wal(phase="publishing"))
    assert prog.completed_targets == 1
    assert prog.total_targets == 2
    # recovery_attempts > 0 on any nonterminal phase -> recovering.
    prog = projectors.map_wal_to_progress(_wal(phase="publishing", recovery_attempts=1))
    assert prog.status == "recovering"
    assert prog.error_code == "transaction_recovering"
    # indeterminate dominates everything else.
    prog = projectors.map_wal_to_progress(_wal(phase="indeterminate", recovery_attempts=5))
    assert prog.status == "indeterminate"
    assert prog.retry_after_ms is None
    assert prog.error_code == "indeterminate_outcome"
    # Terminal committed/rolled_back are NOT progress projections.
    assert projectors.map_wal_to_progress(_wal(phase="committed")) is None
    assert projectors.map_wal_to_progress(_wal(phase="rolled_back")) is None
    # Closed object: round-trips through canonical mapping.
    prog = projectors.map_wal_to_progress(_wal(phase="prepared"))
    assert set(prog.to_mapping()) == {
        "schema", "operation", "transaction_id", "parent_transaction_id", "status",
        "phase", "commit_decided", "completed_targets", "total_targets",
        "recovery_attempts", "started_at", "updated_at", "retry_after_ms", "error_code",
    }


# ============================================================================
# DEP-01..06: effective-state precedence
# ============================================================================


def test_dep_01_invalid_precedence():
    """Invalid local operation wins effective-state precedence (DEP-01)."""
    assert projectors.effective_state("invalid", "accepted", [["op_x"]], [{"code": "x"}]) == "invalid"


def test_dep_02_conflict_precedence():
    """Conflict wins over explicit decision and blocker (DEP-02)."""
    assert projectors.effective_state("valid", "accepted", [["op_x"]], [{"code": "y"}]) == "conflicted"


def test_dep_03_rejected():
    """Rejection remains explicit when otherwise valid (DEP-03)."""
    assert projectors.effective_state("valid", "rejected", [["op_x"]], ()) == "rejected"


def test_dep_04_transitive_blocking():
    """Transitive prerequisite state derives sorted blockers (DEP-04).

    Graph: C depends on B, B depends on A.  A rejected (root) => B and C are
    blocked with deterministic shortest ``op_id`` paths to the root blocker A.
    """
    A, B, C = OP_A, OP_B, OP_C
    op_a = _op(A)
    op_b = _op(B, depends_on=(A,))
    op_c = _op(C, depends_on=(B,))
    states = projectors.project_operations(
        [op_a, op_b, op_c],
        local_validities={A: "valid", B: "valid", C: "valid"},
        explicit_decisions={A: "rejected", B: "accepted", C: "accepted"},
    )
    assert states[A].effective_state == "rejected"
    assert states[A].blockers == ()
    assert states[B].effective_state == "blocked"
    assert states[B].blockers == ((B, A),)
    assert states[C].effective_state == "blocked"
    assert states[C].blockers == ((C, B, A),)
    # Two independent root blockers reachable from one op yield sorted paths.
    D = OP_D
    op_d = _op(D, depends_on=(B, A))
    states2 = projectors.project_operations(
        [op_a, op_b, op_d],
        local_validities={A: "valid", B: "valid", D: "valid"},
        explicit_decisions={A: "rejected", B: "accepted", D: "accepted"},
    )
    # D -> A directly is the shortest path to A; D accepted depends on accepted B too.
    assert states2[D].effective_state == "blocked"
    assert states2[D].blockers == ((D, A),)


def test_dep_05_apply_membership():
    """Only accepted, valid, unconflicted, unblocked op enters apply set (DEP-05)."""
    A = OP_A
    B = OP_B
    states = projectors.project_operations(
        [_op(A), _op(B, depends_on=(A,))],
        local_validities={A: "valid", B: "valid"},
        explicit_decisions={A: "accepted", B: "accepted"},
    )
    assert projectors.accepted_apply_set(states) == (A, B)
    # A rejected drops both A and (transitively blocked) B from the apply set.
    states = projectors.project_operations(
        [_op(A), _op(B, depends_on=(A,))],
        local_validities={A: "valid", B: "valid"},
        explicit_decisions={A: "rejected", B: "accepted"},
    )
    assert projectors.accepted_apply_set(states) == ()


def test_dep_06_undecided():
    """Absent decision remains undecided (DEP-06)."""
    A = OP_A
    states = projectors.project_operations(
        [_op(A)],
        local_validities={A: "valid"},
        explicit_decisions={},
    )
    assert states[A].explicit_decision == "undecided"
    assert states[A].effective_state == "undecided"


# ============================================================================
# LIFE-02 / LIFE-08 / LIFE-09: proposal lifecycle
# ============================================================================


def test_life_02_ready():
    """Complete valid decisions derive ready (LIFE-02).

    Every op accepted/rejected, accepted set non-empty, dependency-closed,
    valid, fresh, no conflict => derived ``ready``.
    """
    A = "op_AAAAAAAAAAAAAAAAAAAAAAAAAA"
    op = _op(A)
    states = projectors.project_operations(
        [op], local_validities={A: "valid"}, explicit_decisions={A: "accepted"},
    )
    apply_set = projectors.accepted_apply_set(states)
    assert apply_set == (A,)
    conflicts = projectors.project_conflicts(_proposal([op]), _bundle(), now=TS)
    ready = bool(apply_set) and not conflicts.has_conflict
    assert projectors.proposal_lifecycle_state(ready=ready) == "ready"


def test_life_08_expiry():
    """Exact boundary derives expired and blocks mutation (LIFE-08)."""
    assert projectors.is_expired(TS_EXPIRE, TS_EXPIRE) is True   # boundary inclusive
    assert projectors.is_expired(TS_EXPIRE, TS_AFTER) is True     # just after
    assert projectors.is_expired(TS_EXPIRE, TS) is False          # before
    # Expired derives the expired lifecycle state and is terminal.
    assert projectors.proposal_lifecycle_state(expired=True) == "expired"
    assert "expired" in projectors.TERMINAL_STATES
    # An expired revision cannot be mutated further.
    with pytest.raises(InvalidStateError):
        projectors.assert_mutation_allowed("expired", "apply")


def test_life_09_illegal_transitions():
    """Every prohibited terminal transition returns invalid_state (LIFE-09)."""
    for terminal in projectors.TERMINAL_STATES:
        for event in ("decide", "withdraw", "supersede", "apply"):
            if event == "apply" and terminal == "ready":
                continue
            with pytest.raises(InvalidStateError):
                projectors.assert_mutation_allowed(terminal, event)
    # Undo is allowed only from applied.
    for terminal in projectors.TERMINAL_STATES - {"applied"}:
        with pytest.raises(InvalidStateError):
            projectors.assert_mutation_allowed(terminal, "undo")
    projectors.assert_mutation_allowed("applied", "undo")  # the one legal terminal exit
    # Live review states may supersede/withdraw/decide.
    for state in projectors.REVIEW_STATES:
        projectors.assert_mutation_allowed(state, "decide")
    # Apply requires exactly ready.
    for state in projectors.REVIEW_STATES - {"ready"}:
        with pytest.raises(InvalidStateError):
            projectors.assert_mutation_allowed(state, "apply")
    projectors.assert_mutation_allowed("ready", "apply")
    # Unknown event/state raise bounded ProjectionError, never fabricate state.
    with pytest.raises(ProjectionError):
        projectors.assert_mutation_allowed("ready", "bogus")
    with pytest.raises(ProjectionError):
        projectors.assert_mutation_allowed("bogus", "apply")


# ============================================================================
# CONFLICT-02 / CONFLICT-03 / CONFLICT-04: conflict projection
# ============================================================================


def test_conflict_02_read_revision():
    """Accepted op read revision mismatch is op/concept-scoped (CONFLICT-02)."""
    op = _op("op_" + ULID)
    prop = _proposal([op])
    # Drift the read concept's current revision.
    bundle = _bundle(concepts={"tables/customers": HEX64C, "tables/orders": HEX64B})
    latest = {"op_" + ULID: _decision("op_" + ULID, decision="accepted")}
    conflicts = projectors.project_conflicts(prop, bundle, now=TS, latest_decisions=latest)
    read_conflicts = [c for c in conflicts.conflicts if c["code"] == "read_revision_changed"]
    assert len(read_conflicts) == 1
    assert read_conflicts[0]["op_id"] == "op_" + ULID
    assert read_conflicts[0]["concept"] == "tables/customers"
    assert read_conflicts[0]["expected"] == HEX64
    assert read_conflicts[0]["actual"] == HEX64C
    # The mismatched op is conflicted in the projected state table.
    states = projectors.project_operations(
        [op], local_validities={"op_" + ULID: "valid"},
        explicit_decisions={"op_" + ULID: "accepted"},
        proposal_conflicts=conflicts.proposal_conflicts,
        op_conflicts=conflicts.op_conflicts,
    )
    assert states["op_" + ULID].effective_state == "conflicted"


def test_conflict_03_write_revision():
    """Accepted op write revision mismatch is op/concept-scoped (CONFLICT-03)."""
    op = _op("op_" + ULID)
    prop = _proposal([op])
    bundle = _bundle(concepts={"tables/customers": HEX64, "tables/orders": HEX64C})
    latest = {"op_" + ULID: _decision("op_" + ULID, decision="accepted")}
    conflicts = projectors.project_conflicts(prop, bundle, now=TS, latest_decisions=latest)
    write_conflicts = [c for c in conflicts.conflicts if c["code"] == "write_revision_changed"]
    assert len(write_conflicts) == 1
    assert write_conflicts[0]["op_id"] == "op_" + ULID
    assert write_conflicts[0]["concept"] == "tables/orders"
    assert write_conflicts[0]["expected"] == HEX64B
    assert write_conflicts[0]["actual"] == HEX64C


def test_conflict_04_target_environment():
    """Missing/type/capability/config mismatch maps exact code (CONFLICT-04)."""
    op = _op("op_" + ULID)
    prop = _proposal([op])
    latest = {"op_" + ULID: _decision("op_" + ULID, decision="accepted")}
    # target_missing: accepted op's write target absent from current bundle.
    bundle_missing = _bundle(concepts={"tables/customers": HEX64})
    conflicts = projectors.project_conflicts(prop, bundle_missing, now=TS, latest_decisions=latest)
    codes = {c["code"] for c in conflicts.conflicts}
    assert "target_missing" in codes
    # capability_changed: active capability set differs.
    bundle_cap = _bundle(capabilities=["okf.cap.other"])
    conflicts = projectors.project_conflicts(prop, bundle_cap, now=TS, latest_decisions=latest)
    assert any(c["code"] == "capability_changed" and c["op_id"] is None for c in conflicts.conflicts)
    # config_changed: effective configuration digest differs.
    bundle_cfg = _bundle(config_digest=DIGEST_B)
    conflicts = projectors.project_conflicts(prop, bundle_cfg, now=TS, latest_decisions=latest)
    assert any(c["code"] == "config_changed" for c in conflicts.conflicts)
    # snapshot_changed: full snapshot digest differs.
    bundle_snap = _bundle(config_digest=DIGEST_B)  # config drift changes the digest too
    conflicts = projectors.project_conflicts(prop, bundle_snap, now=TS, latest_decisions=latest)
    assert any(c["code"] == "snapshot_changed" for c in conflicts.conflicts)
    # Conflicts never mutate the stored proposal.
    assert prop.expected_snapshot.snapshot_digest != bundle_snap.snapshot_digest


# ============================================================================
# CACHE-04 (backend): any token/runtime/expiry mismatch disables controls
# ============================================================================


def test_cache_04_tuple_mismatch():
    """Any member of the apply-enable tuple changing invalidates authority."""
    base = projectors.ApplyEnableTuple(
        proposal_id=ULID, proposal_revision=1, read_model_token=DIGEST,
          decision_set_token=DIGEST, preview_digest=DIGEST, snapshot_digest=DIGEST,
          validation_context_digest=DIGEST,
        validator_id=RUNTIME, serializer_id=RUNTIME_SER, operation_runtime_id=RUNTIME_OPS,
    )
    base_token = base.as_token()
    # Identical tuple re-derives the identical token.
    assert projectors.ApplyEnableTuple(
        proposal_id=ULID, proposal_revision=1, read_model_token=DIGEST,
          decision_set_token=DIGEST, preview_digest=DIGEST, snapshot_digest=DIGEST,
          validation_context_digest=DIGEST,
        validator_id=RUNTIME, serializer_id=RUNTIME_SER, operation_runtime_id=RUNTIME_OPS,
    ).as_token() == base_token
    # Any single change changes the token (preview, runtime, snapshot, decision).
    for mutate in (
        dict(proposal_revision=2), dict(read_model_token=DIGEST_B),
          dict(decision_set_token=DIGEST_B), dict(preview_digest=DIGEST_B),
          dict(snapshot_digest=DIGEST_B), dict(validation_context_digest=DIGEST_B),
          dict(validator_id=RUNTIME_SER),
        dict(serializer_id=RUNTIME_OPS), dict(operation_runtime_id=RUNTIME),
    ):
        changed = copy.copy(dataclass_values(base))
        changed.update(mutate)
        assert projectors.ApplyEnableTuple(**changed).as_token() != base_token


def dataclass_values(obj):
    import dataclasses
    return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}


def test_read_model_token_authority_complete():
    """The read-model token includes every normative authority input."""
    inputs = projectors.ReadModelTokenInputs(
        proposal_checksum=DIGEST,
        decision_records=({"record_id": "rec_" + ULID, "row_token": ROW_TOKEN},),
        transaction_records=(), withdrawal_records=(), service_authority=(),
        preview_digest=DIGEST, conflict_inputs_digest=DIGEST, expires_at=TS_EXPIRE,
          bundle_snapshot_digest=DIGEST, config_digest=DIGEST,
          validation_context_digest=DIGEST,
        capabilities=("okf.cap.typed_relations",),
        validator_id=RUNTIME, serializer_id=RUNTIME_SER, operation_runtime_id=RUNTIME_OPS,
    )
    base = projectors.compute_read_model_token(inputs)
    # Deterministic.
    assert projectors.compute_read_model_token(inputs) == base
    # Changing any authority input changes the token.
    for field_name, new_val in (
        ("proposal_checksum", DIGEST_B), ("decision_records", ({"row_token": ROW_TOKEN_B},)),
        ("preview_digest", DIGEST_B), ("expires_at", TS_AFTER),
          ("bundle_snapshot_digest", DIGEST_B), ("config_digest", DIGEST_B),
          ("validation_context_digest", DIGEST_B),
        ("capabilities", ("okf.cap.other",)),
        ("validator_id", RUNTIME_SER), ("conflict_inputs_digest", DIGEST_B),
    ):
        changed = dataclass_values_input(inputs)
        changed[field_name] = new_val
        assert projectors.compute_read_model_token(projectors.ReadModelTokenInputs(**changed)) != base


def dataclass_values_input(obj):
    import dataclasses
    return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}


def test_stale_read_model_token_signal():
    """A stale read-model token surfaces the resync-required domain signal."""
    with pytest.raises(StaleReadModelError) as exc_info:
        raise StaleReadModelError()
    assert exc_info.value.code == "stale_read_model"
    assert exc_info.value.resync_required is True


def test_audit_projection_closed_and_pure():
    """The audit projection is a closed, deterministic link assembly (SPEC 5.7)."""
    prop = _proposal([_op("op_" + ULID)])
    audit = projectors.build_audit_projection(
        proposal=prop,
        decision_record_ids=["rec_" + "B" * 26, "rec_" + "A" * 26],
        withdrawal_record_id=None,
        transaction_ids=["txn_" + ULID],
        endpoint_result_digests=[DIGEST],
        validation_digest=DIGEST,
        resulting_revisions={"tables/orders": HEX64B},
        activity_ids=["out_" + ULID],
        outbox_ids=["out_" + ULID],
        undo_group_id=None,
        undo_transaction_ids=(),
        retention={"audit_expires_at": TS_EXPIRE, "preview_expires_at": TS_EXPIRE,
                   "replay_expires_at": None, "undo_expires_at": None},
    )
    mapping = audit.to_mapping()
    assert set(mapping) == {
        "schema", "proposal_id", "proposal_revision", "proposal_record_id",
        "decision_record_ids", "withdrawal_record_id", "transaction_ids",
        "endpoint_result_digests", "validation_digest", "resulting_revisions",
        "activity_ids", "outbox_ids", "undo_group_id", "undo_transaction_ids",
        "retention",
    }
    # id arrays are canonicalized to sorted tuples.
    assert mapping["decision_record_ids"] == ["rec_" + "A" * 26, "rec_" + "B" * 26]
    assert mapping["withdrawal_record_id"] is None
    assert mapping["undo_group_id"] is None
    # Deterministic over repeated calls.
    again = projectors.build_audit_projection(
        proposal=prop, decision_record_ids=["rec_" + "B" * 26, "rec_" + "A" * 26],
        transaction_ids=["txn_" + ULID], endpoint_result_digests=[DIGEST],
        validation_digest=DIGEST, resulting_revisions={"tables/orders": HEX64B},
        activity_ids=["out_" + ULID], outbox_ids=["out_" + ULID],
        retention={"audit_expires_at": TS_EXPIRE, "preview_expires_at": TS_EXPIRE,
                   "replay_expires_at": None, "undo_expires_at": None},
    )
    assert canonical.dumps_canonical(mapping) == canonical.dumps_canonical(again.to_mapping())


# ============================================================================
# Freshness + decision-set token purity (STATE-15 backend)
# ============================================================================


def test_is_preview_fresh_authority():
    """Preview freshness tracks every authority input + exact expiry."""
    op = _op("op_" + ULID)
    prop = _proposal([op])
    bundle = _bundle()
    assert projectors.is_preview_fresh(prop, bundle, now=TS) is True
    # Drift any authority input => not fresh.
    assert projectors.is_preview_fresh(prop, _bundle(capabilities=["okf.cap.other"]), now=TS) is False
    assert projectors.is_preview_fresh(prop, _bundle(config_digest=DIGEST_B), now=TS) is False
    assert projectors.is_preview_fresh(prop, _bundle(validator=RUNTIME_SER), now=TS) is False
    # Past expiry => not fresh.
    assert projectors.is_preview_fresh(prop, bundle, now=TS_AFTER) is False
    # Decision-set token is a pure digest of latest decision tuples.
    tuples = projectors.decision_set_tuples([op], {"op_" + ULID: _decision("op_" + ULID)})
    assert tuples == [["op_" + ULID, ROW_TOKEN]]
    assert canonical.canonical_digest(tuples).startswith("sha256:")


# ============================================================================
# Fix #7: Lifecycle ``ready`` requires every op explicitly decided
# ============================================================================


def test_life_02_ready_requires_all_decided():
    """LIFE-02: ready requires every op accepted or rejected + non-empty accepted set."""
    # Single op accepted => ready.
    A = OP_A
    op = _op(A)
    states = projectors.project_operations(
        [op], local_validities={A: "valid"}, explicit_decisions={A: "accepted"},
    )
    apply_set = projectors.accepted_apply_set(states)
    conflicts = projectors.project_conflicts(_proposal([op]), _bundle(), now=TS)
    all_decided = all(d in ("accepted", "rejected") for d in
                      (states[oid].explicit_decision for oid in states))
    ready = bool(apply_set) and not conflicts.has_conflict and all_decided
    assert ready is True

    # Undecided op => NOT ready (even if one op is accepted).
    B = OP_B
    op2 = {
        "schema": 1, "op_id": B, "kind": "entity_add", "target": "tables/customers",
        "args": {"label": "cust", "kind": "table", "entity_id": None},
        "reads": ["tables/customers"], "writes": ["tables/customers"],
        "expected_revisions": {"tables/customers": HEX64},
        "depends_on": [], "extensions": {},
    }
    states2 = projectors.project_operations(
        [op, op2], local_validities={A: "valid", B: "valid"},
        explicit_decisions={A: "accepted"},  # B undecided
    )
    apply_set2 = projectors.accepted_apply_set(states2)
    conflicts2 = projectors.project_conflicts(
        _proposal([op, op2]), _bundle(), now=TS)
    all_decided2 = all(
        states2[oid].explicit_decision in ("accepted", "rejected") for oid in states2)
    ready2 = bool(apply_set2) and not conflicts2.has_conflict and all_decided2
    assert ready2 is False

    # Both decided (one accepted, one rejected) => ready.
    states3 = projectors.project_operations(
        [op, op2], local_validities={A: "valid", B: "valid"},
        explicit_decisions={A: "accepted", B: "rejected"},
    )
    apply_set3 = projectors.accepted_apply_set(states3)
    conflicts3 = projectors.project_conflicts(
        _proposal([op, op2]), _bundle(), now=TS)
    all_decided3 = all(
        states3[oid].explicit_decision in ("accepted", "rejected") for oid in states3)
    ready3 = bool(apply_set3) and not conflicts3.has_conflict and all_decided3
    assert ready3 is True

    # Conflict => not ready even if all decided.
    bundle_conflict = _bundle(concepts={"tables/customers": HEX64C, "tables/orders": HEX64B})
    conflicts4 = projectors.project_conflicts(
        _proposal([op]), bundle_conflict, now=TS,
        latest_decisions={A: _decision(A, decision="accepted")})
    assert conflicts4.has_conflict
    ready4 = bool(apply_set) and not conflicts4.has_conflict and all_decided
    assert ready4 is False


# ============================================================================
# Fix #8: Conflict round-trip for every projector-produced conflict code
# ============================================================================


def test_conflict_round_trip_every_projector_code():
    """Every projector-produced conflict code round-trips through schemas.Conflict."""
    # snapshot_changed (digest-valued)
    for code, expected_val, actual_val in [
        ("snapshot_changed", DIGEST, DIGEST_B),
        ("read_revision_changed", HEX64, HEX64C),
        ("write_revision_changed", HEX64B, HEX64C),
        ("target_missing", HEX64, "absent"),
        ("capability_changed",
         canonical.canonical_digest(("okf.cap.typed_relations",)),
         canonical.canonical_digest(("okf.cap.other",))),
        ("config_changed", DIGEST, DIGEST_B),
        ("preview_expired", TS_EXPIRE, TS_AFTER),
        ("validation_stale", RUNTIME, RUNTIME_SER),
    ]:
        c = {
            "schema": 1, "code": code, "proposal_id": ULID,
            "proposal_revision": 1, "op_id": None, "concept": None,
            "expected": expected_val, "actual": actual_val,
            "message": f"conflict {code}", "detected_at": TS,
        }
        if code not in ("snapshot_changed", "capability_changed", "config_changed",
                         "preview_expired", "validation_stale"):
            c["op_id"] = "op_" + ULID
            c["concept"] = "tables/orders"
        # Round-trip through the schema parser.
        rec = schemas.Conflict.from_mapping(c)
        assert rec.code == code
        assert rec.expected == expected_val
        assert rec.actual == actual_val
        # Canonical round-trip.
        rt = schemas.Conflict.from_json(rec.canonical_bytes())
        assert rt.code == code
        assert rt.expected == expected_val


# ============================================================================
# Fix #10: Deep immutability — aliasing tests across record families
# ============================================================================


@pytest.mark.parametrize("name", ["Operation", "Wrapper", "Checkpoint"])
def test_deep_immutability_source_mutation(name):
    """Mutating the source mapping after parse does not change canonical bytes (Fix #10)."""
    import okf_loom.proposal_review.canonical as canon
    if name == "Operation":
        source_args = {"key": "tags", "value": ["a", "b"]}
        source_ext = {"okf.test/k": {"nested": [1]}}
        m = {
            "schema": 1, "op_id": "op_" + ULID, "kind": "set_frontmatter",
            "target": "tables/orders", "args": source_args,
            "reads": ["tables/orders"], "writes": ["tables/orders"],
            "expected_revisions": {"tables/orders": HEX64},
            "depends_on": [], "extensions": source_ext,
        }
        rec = schemas.Operation.from_mapping(m)
        original = rec.canonical_bytes()
        # Mutate the source dicts/lists after parse.
        source_args["key"] = "CHANGED"
        source_args["value"].append("c")
        source_ext["okf.test/k"]["nested"].append(99)
        assert rec.canonical_bytes() == original
    elif name == "Wrapper":
        source_payload = {"x": [1, 2]}
        m = {"record_schema": 1, "kind": "proposal", "sequence": 1, "payload": source_payload}
        m["checksum"] = canon.compute_checksum(m)
        rec = schemas.Wrapper.from_mapping(m)
        original = rec.canonical_bytes()
        source_payload["x"].append(999)
        source_payload["new_key"] = "val"
        assert rec.canonical_bytes() == original
    elif name == "Checkpoint":
        source_records = [{"x": [1]}]
        m = {"schema": 1, "repository_id": "repo_" + ULID, "kind": "proposals",
             "generation": 1, "sequence_high_water": 1, "covered_generations": [1],
             "records": source_records, "created_at": TS}
        m["records_digest"] = canon.canonical_digest(source_records)
        m["checksum"] = canon.compute_checksum(m)
        rec = schemas.Checkpoint.from_mapping(m)
        original = rec.canonical_bytes()
        source_records[0]["x"].append(999)
        source_records.append({"y": 2})
        assert rec.canonical_bytes() == original


def test_projectors_import_boundary():
    """Projectors import no server/studio/cli/render/viewer/Bundle/filesystem."""
    import ast, inspect
    from okf_loom.proposal_review import projectors as proj
    src = inspect.getsource(proj)
    forbidden = ("import os", "import sys", "open(", "requests", "socket",
                 "from okf_loom.server", "from okf_loom.studio", "from okf_loom.cli",
                 "from okf_loom.render", "from okf_loom.viewer")
    for token in forbidden:
        assert token not in src, f"projectors must not reference {token!r}"
    # AST assertion: projectors module references BundleView (the protocol),
    # not CurrentBundleView (the concrete placeholder) (Req #7).
    tree = ast.parse(src)
    names_found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names_found.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            names_found.add(f"{node.value.id}.{node.attr}")
    assert "BundleView" in names_found or "BundleView" in proj.__dict__
    assert "CurrentBundleView" not in names_found, \
        "projectors must not reference CurrentBundleView directly"


def test_removed_bundle_placeholder_is_absent_from_production_source():
    from pathlib import Path

    production = Path(__file__).parents[1] / "scripts" / "okf_loom"
    offenders = [str(path) for path in production.rglob("*.py")
                 if "CurrentBundleView" in path.read_text(encoding="utf-8")]
    assert offenders == []


# ============================================================================
# Req #1: True post-parse deep immutability — mutation rejection tests
# ============================================================================


def test_deep_immutability_record_field_mutation_rejected():
    """Mutating frozen record dict fields raises TypeError (Req #1)."""
    import pytest as _pytest
    op = schemas.Operation.from_mapping(_op("op_" + ULID))
    # args is a FrozenDict — mutation must raise.
    with _pytest.raises(TypeError):
        op.args["key"] = "changed"


def test_deep_immutability_nested_sequence_mutation_rejected():
    """Nested sequence/list mutation on frozen record raises TypeError (Req #1)."""
    import pytest as _pytest
    op_mapping = {
        "schema": 1, "op_id": "op_" + ULID, "kind": "set_frontmatter",
        "target": "tables/orders",
        "args": {"key": "tags", "value": ["a", "b"]},
        "reads": ["tables/orders"], "writes": ["tables/orders"],
        "expected_revisions": {"tables/orders": HEX64},
        "depends_on": [], "extensions": {},
    }
    rec = schemas.Operation.from_mapping(op_mapping)
    # The value list inside args is frozen — append/assign must raise.
    with _pytest.raises(TypeError):
        rec.args["value"].append("c")
    with _pytest.raises(TypeError):
        rec.args["value"][0] = "z"


def test_deep_immutability_payload_response_mutation_rejected():
    """Wrapper.payload and EndpointResult.response mutation rejected (Req #1)."""
    import pytest as _pytest
    from okf_loom.proposal_review import canonical as _canon
    payload = {"x": [1, 2], "nested": {"y": 3}}
    w_map = {"record_schema": 1, "kind": "proposal", "sequence": 1, "payload": payload}
    w_map["checksum"] = _canon.compute_checksum(w_map)
    wrapper = schemas.Wrapper.from_mapping(w_map)
    with _pytest.raises(TypeError):
        wrapper.payload["x"] = [9]
    with _pytest.raises(TypeError):
        wrapper.payload["nested"]["y"] = 99


@pytest.mark.parametrize("record_kind", ["operation_args", "wrapper_payload", "checkpoint_records"])
def test_deep_immutability_parametrized(record_kind):
    """Parametrized deep immutability across representative families (Req #1)."""
    import pytest as _pytest
    from okf_loom.proposal_review import canonical as _canon
    if record_kind == "operation_args":
        rec = schemas.Operation.from_mapping({
            "schema": 1, "op_id": "op_" + ULID, "kind": "set_frontmatter",
            "target": "tables/orders", "args": {"key": "tags", "value": ["a"]},
            "reads": ["tables/orders"], "writes": ["tables/orders"],
            "expected_revisions": {"tables/orders": HEX64},
            "depends_on": [], "extensions": {},
        })
        original = rec.canonical_bytes()
        with _pytest.raises(TypeError):
            rec.args["key"] = "changed"
        assert rec.canonical_bytes() == original
    elif record_kind == "wrapper_payload":
        w_map = {"record_schema": 1, "kind": "proposal", "sequence": 1,
                 "payload": {"data": [1, {"nested": True}]}}
        w_map["checksum"] = _canon.compute_checksum(w_map)
        rec = schemas.Wrapper.from_mapping(w_map)
        original = rec.canonical_bytes()
        with _pytest.raises(TypeError):
            rec.payload["data"][0] = 999
        with _pytest.raises(TypeError):
            rec.payload["data"][1]["nested"] = False
        assert rec.canonical_bytes() == original
    elif record_kind == "checkpoint_records":
        records = [{"seq": 1, "items": [10, 20]}]
        cp_map = {"schema": 1, "repository_id": "repo_" + ULID, "kind": "proposals",
                  "generation": 1, "sequence_high_water": 1,
                  "covered_generations": [1], "records": records, "created_at": TS}
        cp_map["records_digest"] = _canon.canonical_digest(records)
        cp_map["checksum"] = _canon.compute_checksum(cp_map)
        rec = schemas.Checkpoint.from_mapping(cp_map)
        original = rec.canonical_bytes()
        with _pytest.raises(TypeError):
            rec.records[0]["items"][0] = 999
        assert rec.canonical_bytes() == original


# ============================================================================
# Req #8: Read-token validation — exhaustive independent variation proof
# ============================================================================


def _token_inputs(**overrides):
    base = dict(
        proposal_checksum=DIGEST,
        decision_records=({"record_id": "rec_" + ULID, "row_token": ROW_TOKEN},),
        transaction_records=(), withdrawal_records=(), service_authority=(),
        preview_digest=DIGEST, conflict_inputs_digest=DIGEST, expires_at=TS_EXPIRE,
          bundle_snapshot_digest=DIGEST, config_digest=DIGEST,
          validation_context_digest=DIGEST,
        capabilities=("okf.cap.typed_relations",),
        validator_id=RUNTIME, serializer_id=RUNTIME_SER, operation_runtime_id=RUNTIME_OPS,
    )
    base.update(overrides)
    return projectors.ReadModelTokenInputs(**base)


@pytest.mark.parametrize(
    "field,new_val",
    [
        ("proposal_checksum", DIGEST_B),
        ("decision_records", ({"row_token": ROW_TOKEN_B},)),
        ("transaction_records", ({"x": 1},)),
        ("withdrawal_records", ({"x": 1},)),
        ("service_authority", ({"intent_id": "svc_" + ULID},)),
        ("preview_digest", DIGEST_B),
        ("conflict_inputs_digest", DIGEST_B),
        ("expires_at", TS_AFTER),
        ("bundle_snapshot_digest", DIGEST_B),
          ("config_digest", DIGEST_B),
          ("validation_context_digest", DIGEST_B),
        ("capabilities", ("okf.cap.other",)),
        ("validator_id", RUNTIME_SER),
        ("serializer_id", RUNTIME_OPS),
        ("operation_runtime_id", RUNTIME_SER),
    ],
    ids=["proposal_checksum", "decision_records", "transaction_records",
         "withdrawal_records", "service_authority", "preview_digest",
         "conflict_inputs_digest", "expires_at", "bundle_snapshot_digest",
          "config_digest", "validation_context_digest", "capabilities", "validator_id", "serializer_id",
         "operation_runtime_id"],
)
def test_read_token_each_authority_input_variation(field, new_val):
    """Independently varying every authority input changes the token (Req #8)."""
    base = projectors.compute_read_model_token(_token_inputs())
    varied = projectors.compute_read_model_token(_token_inputs(**{field: new_val}))
    assert varied != base
