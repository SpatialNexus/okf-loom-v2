"""Pure composite proposal read model for Proposal / Diff Review v1 (Foundation B).

Composes the side-effect-free :mod:`projectors` into the canonical closed
proposal read model (SPEC section 5.7).  It consumes **supplied** verified
preview bytes, decision/transaction/withdrawal records, and immutable
bundle/runtime/clock facts: it never performs filesystem, clock, network, or
Bundle-adapter I/O, and it imports no server/Studio/CLI/render/viewer code.

Authority rules (SPEC section 5.3 / 6 / 10):

* the sole preview artifact is the only preview authority; before embedding it
  for transport the read model verifies ``preview_ref.preview_digest`` and
  ``artifact_bytes`` exactly, and a missing/mismatched/oversized artifact makes
  the read model unavailable (:class:`PreviewAuthorityError`);
* after preview-byte expiry the embedded ``preview`` becomes
  ``{schema:1,expired:true,expired_at}`` while ``proposal.preview_ref`` remains;
* the complete read-model canonical serialized bytes must not exceed 40 MiB or
  the proposal is rejected (:class:`ReadModelTooLargeError`);
* the read-model token includes every normative authority input.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from . import projectors, schemas
from . import ports as _ports
from .canonical import canonical_digest, dumps_canonical
from .errors import PreviewAuthorityError, ReadModelTooLargeError

# Pure frozen bundle-facts protocol — tests provide their own immutable fake;
# no production Bundle adapter is imported (Req #7).
BundleView = _ports.BundleView

#: Complete proposal read-model canonical serialized byte cap (SPEC section 4).
MAX_READ_MODEL_BYTES: int = 40 * 1024 * 1024

#: Aggregate kinds whose high-waters participate in the read-model token.
_AGGREGATES = ("proposals", "decisions", "transactions", "endpoint_results",
               "outbox", "tombstones", "corruption")


@dataclass(frozen=True)
class Retention:
    """Closed retention projection (SPEC section 5.7).

    Each timestamp is nullable only when that artifact never existed.
    """

    audit_expires_at: str | None
    preview_expires_at: str | None
    replay_expires_at: str | None
    undo_expires_at: str | None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "audit_expires_at": self.audit_expires_at,
            "preview_expires_at": self.preview_expires_at,
            "replay_expires_at": self.replay_expires_at,
            "undo_expires_at": self.undo_expires_at,
        }


@dataclass(frozen=True)
class ProposalReadModel:
    """Canonical closed proposal read model (SPEC section 5.7 line 435)."""

    schema: int
    proposal: Mapping[str, Any]
    state: str
    operation_states: tuple[projectors.OperationState, ...]
    latest_decisions: tuple[Mapping[str, Any], ...]
    decision_set_token: str
    conflicts: tuple[Mapping[str, Any], ...]
    transaction_attempts: tuple[projectors.TransactionProgress, ...]
    read_model_token: str
    apply_result: Mapping[str, Any] | None
    undo_result: Mapping[str, Any] | None
    retention: Retention

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "proposal": dict(self.proposal),
            "state": self.state,
            "operation_states": [s.to_mapping() for s in self.operation_states],
            "latest_decisions": [dict(d) for d in self.latest_decisions],
            "decision_set_token": self.decision_set_token,
            "conflicts": [dict(c) for c in self.conflicts],
            "transaction_attempts": [a.to_mapping() for a in self.transaction_attempts],
            "read_model_token": self.read_model_token,
            "apply_result": dict(self.apply_result) if self.apply_result is not None else None,
            "undo_result": dict(self.undo_result) if self.undo_result is not None else None,
            "retention": self.retention.to_mapping(),
        }

    def canonical_bytes(self) -> bytes:
        return dumps_canonical(self.to_mapping())


def _verify_and_embed_preview(
    proposal: schemas.ProposalRevision,
    preview: schemas.Preview | None,
    *,
    now: str,
) -> dict[str, Any]:
    """Return the ``proposal`` transport projection with embedded/expired preview.

    Before byte expiry the sole preview artifact is mandatory and must verify
    exactly; after expiry the projection marks the preview expired.  The
    immutable ``preview_ref`` always remains on the proposal revision.
    """
    revision_mapping = proposal.to_mapping()
    if projectors.is_expired(proposal.expires_at, now):
        return {**revision_mapping, "preview": {"schema": 1, "expired": True, "expired_at": now}}
    if preview is None:
        raise PreviewAuthorityError(
            "verified preview artifact is required while preview bytes are retained"
        )
    if preview.preview_digest != proposal.preview_ref.preview_digest:
        raise PreviewAuthorityError("preview artifact digest does not match preview_ref")
    actual_bytes = len(preview.canonical_bytes())
    if actual_bytes != proposal.preview_ref.artifact_bytes:
        raise PreviewAuthorityError(
            f"preview artifact byte length {actual_bytes} != preview_ref.artifact_bytes "
            f"{proposal.preview_ref.artifact_bytes}"
        )
    return {**revision_mapping, "preview": preview.to_mapping()}


@dataclass(frozen=True)
class ReadModelInputs:
    """Explicit immutable facts consumed by :func:`build_read_model`."""

    proposal: schemas.ProposalRevision
    current_bundle: BundleView
    now: str
    decision_rows: tuple[schemas.DecisionRow, ...] = ()
    preview: schemas.Preview | None = None
    transaction_wals: tuple[schemas.WAL, ...] = ()
    transaction_records: tuple[Mapping[str, Any], ...] = ()
    withdrawal: schemas.Withdrawal | None = None
    apply_result: Mapping[str, Any] | None = None
    undo_result: Mapping[str, Any] | None = None
    applied: bool = False
    superseded: bool = False
    audit_expired: bool = False
    audit_expires_at: str | None = None
    replay_expires_at: str | None = None
    undo_expires_at: str | None = None
    service_authority: tuple[Mapping[str, Any], ...] = ()


def build_read_model(inputs: ReadModelInputs) -> ProposalReadModel:
    """Build the canonical closed proposal read model from immutable facts.

    Pure composition: derives operation states, conflicts, lifecycle,
    transaction-progress attempts, the decision-set token, retention, and the
    read-model token.  Verifies sole preview authority before embedding and
    rejects an oversized read model before it can be persisted.
    """
    proposal = inputs.proposal
    bundle = inputs.current_bundle
    now = inputs.now

    # --- preview authority + embedding ---
    proposal_projection = _verify_and_embed_preview(proposal, inputs.preview, now=now)

    # --- explicit decisions ---
    latest_rows = projectors.latest_decision_rows(
        inputs.decision_rows, proposal.proposal_id, proposal.proposal_revision,
    )
    decision_map = projectors.explicit_decision_map(latest_rows)

    # --- local validity + conflicts ---
    local_validities = {
        op.op_id: projectors.derive_local_validity(op, proposal.expected_snapshot)
        for op in proposal.operations
    }
    conflict_projection = projectors.project_conflicts(
        proposal, bundle, now=now, latest_decisions=latest_rows,
    )

    # --- operation states ---
    operation_states = projectors.project_operations(
        proposal.operations,
        local_validities=local_validities,
        explicit_decisions=decision_map,
        proposal_conflicts=conflict_projection.proposal_conflicts,
        op_conflicts=conflict_projection.op_conflicts,
    )
    ordered_states = tuple(
        operation_states[op.op_id] for op in sorted(proposal.operations, key=lambda o: o.op_id)
    )

    # --- transaction-progress attempts (nonterminal WALs only) ---
    attempts = []
    for wal in inputs.transaction_wals:
        parent = None
        if wal.operation == "undo":
            parent = inputs.apply_result.get("transaction_id") if inputs.apply_result else None
        prog = projectors.map_wal_to_progress(wal, parent_transaction_id=parent)
        if prog is not None:
            attempts.append(prog)
    ordered_attempts = projectors.sort_transaction_attempts(attempts)

    # --- decision-set token ---
    dst = schemas.decision_set_token(
        projectors.decision_set_tuples(proposal.operations, latest_rows)
    )

    # --- lifecycle state ---
    # SPEC §7.2 LIFE-02: ``ready`` requires every operation explicitly
    # accepted or rejected, accepted set non-empty, dependency-closed, valid,
    # fresh, and no conflict (Fix #7).
    apply_set = projectors.accepted_apply_set(operation_states)
    all_ops_decided = all(
        decision_map.get(op.op_id, "undecided") in ("accepted", "rejected")
        for op in proposal.operations
    )
    no_invalid = all(
        operation_states[op.op_id].local_validity == "valid"
        for op in proposal.operations
    )
    ready = (
        bool(apply_set)
        and not conflict_projection.has_conflict
        and all_ops_decided
        and no_invalid
    )
    expired = projectors.is_expired(proposal.expires_at, now)
    state = projectors.proposal_lifecycle_state(
        applied=inputs.applied,
        withdrawn=inputs.withdrawal is not None,
        superseded=inputs.superseded,
        audit_expired=inputs.audit_expired,
        expired=expired,
        has_conflict=conflict_projection.has_conflict,
        ready=ready,
    )

    # --- retention ---
    retention = Retention(
        audit_expires_at=inputs.audit_expires_at,
        preview_expires_at=proposal.expires_at,
        replay_expires_at=inputs.replay_expires_at,
        undo_expires_at=inputs.undo_expires_at,
    )

    # --- read-model token (every normative authority input) ---
    read_model_token_inputs = projectors.ReadModelTokenInputs(
        proposal_checksum=canonical_digest(proposal.to_mapping()),
        decision_records=tuple(latest_rows[op_id].to_mapping() for op_id in sorted(latest_rows)),
        transaction_records=(tuple(inputs.transaction_records) or
                             tuple(w.to_mapping() for w in inputs.transaction_wals)),
        withdrawal_records=() if inputs.withdrawal is None else (inputs.withdrawal.to_mapping(),),
        service_authority=tuple(inputs.service_authority),
        preview_digest=proposal.preview_ref.preview_digest,
        conflict_inputs_digest=projectors.conflict_inputs_digest(
            bundle_snapshot_digest=bundle.snapshot_digest,
            config_digest=bundle.config_digest,
            validation_context_digest=bundle.validation_context_digest,
            capabilities=bundle.capabilities,
            concept_revisions=bundle.concept_revisions,
            validator_id=bundle.validator_id,
            serializer_id=bundle.serializer_id,
            operation_runtime_id=bundle.operation_runtime_id,
        ),
        expires_at=proposal.expires_at,
        bundle_snapshot_digest=bundle.snapshot_digest,
        config_digest=bundle.config_digest,
        validation_context_digest=bundle.validation_context_digest,
        capabilities=bundle.capabilities,
        validator_id=bundle.validator_id,
        serializer_id=bundle.serializer_id,
        operation_runtime_id=bundle.operation_runtime_id,
    )
    read_model_token = projectors.compute_read_model_token(read_model_token_inputs)

    model = ProposalReadModel(
        schema=1,
        proposal=MappingProxyType(proposal_projection),
        state=state,
        operation_states=ordered_states,
        latest_decisions=tuple(
            latest_rows[op.op_id].to_mapping()
            for op in sorted(proposal.operations, key=lambda o: o.op_id)
            if op.op_id in latest_rows
        ),
        decision_set_token=dst,
        conflicts=conflict_projection.conflicts,
        transaction_attempts=ordered_attempts,
        read_model_token=read_model_token,
        apply_result=inputs.apply_result,
        undo_result=inputs.undo_result,
        retention=retention,
    )

    # --- 40 MiB response cap (SPEC section 4 / 6) ---
    if len(model.canonical_bytes()) > MAX_READ_MODEL_BYTES:
        raise ReadModelTooLargeError(
            f"complete proposal read model exceeds {MAX_READ_MODEL_BYTES} canonical bytes"
        )
    return model


__all__ = [
    "MAX_READ_MODEL_BYTES",
    "Retention",
    "ProposalReadModel",
    "ReadModelInputs",
    "build_read_model",
]
