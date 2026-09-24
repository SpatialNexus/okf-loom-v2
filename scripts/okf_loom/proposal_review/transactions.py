"""Transport-neutral Proposal Review apply/undo transaction coordinator.

The coordinator owns state transitions and persisted authority.  Canonical
filesystem syscalls remain exclusively in :mod:`publication`.
"""
from __future__ import annotations

import dataclasses
import datetime
import base64
import hashlib
import threading
import time
import sys
from typing import Any, Mapping

from . import projectors, schemas
from .ids import validate_idempotency_key
from .canonical import canonical_digest, compute_checksum
from .errors import (IdempotencyConflictError, InvalidStateError,
                     PreviewAuthorityError, RepositoryCorruptError,
                     RepositoryError, SchemaError, StorageSafetyError)
from .publication import (DirectoryIdentity, FileIdentity, TargetIdentity, PreparedTarget,
                          TrustedPublicationAdapter)
from .read_model import ReadModelInputs, build_read_model
from .service_contracts import (ApplyInput, ErrorBody, ErrorEnvelope, PersistedErrorResponse,
                                UndoInput)
from .staging import stage_operations
from .validation import build_validation
from .transaction_intent import TransactionFinalizationIntent, substitute


APPLY_SCOPE = "POST /__proposal/v1/proposals/{id}/revisions/{rev}/apply"
UNDO_SCOPE = "POST /__proposal/v1/proposals/{id}/revisions/{rev}/undo"
WAIT_SECONDS = 30.0


class TransactionTerminalError(RepositoryError):
    """A terminal transaction error whose exact public response is durable."""

    def __init__(self, response: PersistedErrorResponse) -> None:
        envelope = ErrorEnvelope.from_mapping(response.envelope).to_mapping()
        error = envelope["error"]
        self.persisted_error = PersistedErrorResponse(response.status, envelope)
        super().__init__(error["message"], code=error["code"], details=error["details"])


def _plus_days(value: str, days: int) -> str:
    parsed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    parsed += datetime.timedelta(days=days)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def _plus_ms(value: str, milliseconds: int) -> str:
    parsed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    parsed += datetime.timedelta(milliseconds=milliseconds)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def _key_digest(key: str) -> str:
    key = validate_idempotency_key(key)
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _fingerprint(scope: str, repository_id: str, request: Mapping[str, Any],
                 runtime: schemas.Snapshot | None = None) -> str:
    body: dict[str, Any] = {"protocol": 1, "scope": scope,
                            "repository_id": repository_id, "request": dict(request)}
    if runtime is not None:
        body["runtime"] = {"validator_id": runtime.validator_id,
                           "serializer_id": runtime.serializer_id,
                           "operation_runtime_id": runtime.operation_runtime_id}
    return canonical_digest(body)


def _with_checksum(body: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(body); value["checksum"] = compute_checksum(value); return value


def _proposal_rows(snapshot: Any) -> tuple[schemas.ProposalRevision, ...]:
    return tuple(schemas.ProposalRevision.from_mapping(w.payload)
                 for w in snapshot.aggregate("proposals") if w.kind == "proposal")


def _decision_rows(snapshot: Any, proposal_id: str, revision: int) -> tuple[schemas.DecisionRow, ...]:
    return tuple(row for row in (schemas.DecisionRow.from_mapping(w.payload)
                                 for w in snapshot.aggregate("decisions") if w.kind == "decision")
                 if row.proposal_id == proposal_id and row.proposal_revision == revision)


def _prepared_target(target: schemas.WalTarget) -> PreparedTarget:
    def leaf(value: schemas.LeafIdentity) -> FileIdentity:
        return FileIdentity(value.relative_name, value.device, value.inode, value.mode,
                            value.size, value.digest)
    def identity(value: schemas.TargetIdentity | None) -> TargetIdentity | None:
        return None if value is None else TargetIdentity(**value.to_mapping())
    return PreparedTarget(target.concept, target.before_digest, target.staged_digest,
                          target.backup_digest, target.restore_digest,
                          identity(target.before_identity), identity(target.staged_identity),
                          identity(target.resulting_identity), leaf(target.stage_leaf),
                          leaf(target.backup_leaf))  # type: ignore[arg-type]


class ProposalTransactionCoordinator:
    """Synchronous 30-second facade over durable recovery-owned completion."""

    def __init__(self, repository: Any, capture_port: Any,
                 publication: TrustedPublicationAdapter, *, clock: Any,
                 activity: Any | None = None, outbox: Any | None = None,
                 fault_injector: Any | None = None,
                 wait_seconds: float = WAIT_SECONDS) -> None:
        self.repository = repository
        self.capture_port = capture_port
        self.publication = publication
        self.clock = clock
        self.activity = activity
        self.outbox = outbox
        self.fault_injector = fault_injector
        self.wait_seconds = wait_seconds
        self._waiters: dict[str, threading.Event] = {}
        self._waiters_lock = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()
        self._closed = threading.Event()

    def close(self) -> None:
        """Stop admission, await owned workers, and release publication dirfds."""
        self._closed.set()
        while True:
            with self._workers_lock:
                workers = tuple(self._workers)
            if not workers:
                break
            for worker in workers:
                if worker is not threading.current_thread():
                    worker.join()
        self.publication.close()

    def __enter__(self) -> "ProposalTransactionCoordinator":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _fault(self, boundary: str, **evidence: Any) -> None:
        """Test seam invoked only after the named production side effect."""
        if self.fault_injector is not None:
            self.fault_injector(boundary, evidence)

    def _proposal(self, proposal_id: str, revision: int) -> schemas.ProposalRevision:
        matches = [p for p in _proposal_rows(self.repository.snapshot())
                   if p.proposal_id == proposal_id and p.proposal_revision == revision]
        if len(matches) != 1:
            raise InvalidStateError("proposal revision is missing or ambiguous")
        return matches[0]

    def _replay(self, reconciliation: Any, fingerprint: str, *, recover: bool = True
                ) -> schemas.TransactionResult | schemas.TransactionProgress | None:
        result = reconciliation.endpoint_result
        if result is not None and result.request_fingerprint != fingerprint:
            raise IdempotencyConflictError("same idempotency key has a different request fingerprint")
        if reconciliation.state == "terminal_result" and result is not None:
            if result.transaction_id is not None:
                with self._waiters_lock:
                    waiter = self._waiters.pop(result.transaction_id, None)
                    if waiter: waiter.set()
            if result.state == "indeterminate":
                raise RepositoryError("transaction outcome is indeterminate", code="indeterminate_outcome")
            body = dict(result.response or {})
            if result.transaction_id is not None:
                audits = [schemas.TransactionAudit.from_mapping(row.payload)
                          for row in self.repository.snapshot().aggregate("transactions")
                          if row.kind == "transaction"
                          and row.payload.get("transaction_id") == result.transaction_id]
                if len(audits) != 1:
                    raise RepositoryCorruptError("terminal result lacks one transaction audit")
                self.repository.verify_audit_transaction_chain(audits[0])
            if "error" in body:
                response = PersistedErrorResponse(result.http_status or 500, body)
                raise TransactionTerminalError(response)
            if body.get("schema") == 1 and "data" in body:
                body = dict(body["data"])
            body["replayed"] = True
            return schemas.TransactionResult.from_mapping(body)
        if reconciliation.state == "nonterminal_wal" and reconciliation.transaction_id:
            if not recover:
                return None
            self._recover_one(reconciliation.transaction_id)
            return self.observe(reconciliation.transaction_id)
        if reconciliation.state in {"indeterminate", "terminal_compacted"}:
            raise RepositoryError("idempotency authority is no longer replayable",
                                  code="indeterminate_outcome")
        return None

    def apply(self, request: ApplyInput | Mapping[str, Any] | bytes | str,
              idempotency_key: str) -> schemas.TransactionResult | schemas.TransactionProgress:
        value = request if isinstance(request, ApplyInput) else ApplyInput.from_mapping(request)
        key_digest = _key_digest(idempotency_key)
        preliminary = _fingerprint(APPLY_SCOPE, self.repository.repository_id,
                                   value.fingerprint_mapping())
        replay = self._replay(self.repository.reconcile_endpoint(
            APPLY_SCOPE, key_digest, preliminary), preliminary, recover=False)
        if replay is not None: return replay
        admission = self.repository.new_transaction_admission_fence()
        return self._run_with_deadline(
            lambda: self._apply_sync(value, key_digest, preliminary, admission),
            APPLY_SCOPE, key_digest, preliminary, admission)

    def _run_with_deadline(self, work: Any, scope: str, key_digest: str,
                           fingerprint: str, admission: Any) -> Any:
        if self._closed.is_set():
            raise RepositoryError("transaction coordinator is closed", code="capability_unavailable")
        completed = threading.Event(); outcome: dict[str, Any] = {}
        def runner() -> None:
            try: outcome["result"] = work()
            except BaseException as exc: outcome["error"] = exc
            finally:
                try:
                    transaction_id = self.repository.reconcile_endpoint(
                        scope, key_digest, fingerprint).transaction_id
                    if transaction_id:
                        with self._waiters_lock: self._waiters.pop(transaction_id, None)
                except (OSError, TimeoutError) as exc:
                    print(f"proposal transaction outbox delivery deferred ({type(exc).__name__})",
                          file=sys.stderr)
                with self._workers_lock:
                    self._workers.discard(threading.current_thread())
                completed.set()
        worker = threading.Thread(target=runner, name="okf-proposal-transaction", daemon=True)
        with self._workers_lock:
            if self._closed.is_set():
                raise RepositoryError("transaction coordinator is closed", code="capability_unavailable")
            self._workers.add(worker)
            try:
                worker.start()
            except BaseException:
                self._workers.discard(worker)
                raise
        if completed.wait(self.wait_seconds):
            if "error" in outcome: raise outcome["error"]
            return outcome["result"]
        admission_state = self.repository.cancel_transaction_admission(admission)
        if admission_state == "cancelled":
            raise RepositoryError(
                "transaction timed out before durable authority admission",
                code="operation_timeout")
        reconciliation = self.repository.reconcile_endpoint(scope, key_digest, fingerprint)
        if reconciliation.state == "nonterminal_wal" and reconciliation.transaction_id:
            return self.observe(reconciliation.transaction_id)
        if reconciliation.state == "prepared" and reconciliation.transaction_id:
            try: return self.observe(reconciliation.transaction_id)
            except Exception as exc:
                raise RepositoryError("transaction preparation continues after caller deadline",
                                      code="transaction_recovering") from exc
        # No durable transaction authority exists yet. The server worker still
        # owns completion, but no 202 may be invented before a WAL exists.
        raise RepositoryError("transaction admission won before the caller deadline fence",
                               code="transaction_recovering")

    def _apply_sync(self, value: ApplyInput, key_digest: str, preliminary: str,
                    admission: Any
                    ) -> schemas.TransactionResult | schemas.TransactionProgress:

        with self.repository.transaction_session() as session:
            proposal = self._proposal(value.proposal_id, value.proposal_revision)
            targets = sorted({concept for op in proposal.operations for concept in op.writes})
            with session.ranked("bundle"):
                with self.publication.exclusive():
                    with session.ranked("concept"):
                        with self.publication.concept_locks(targets):
                            lineage = f"{proposal.proposal_id}:{proposal.proposal_revision}"
                            with session.locked("lineage", lineage):
                                with session.locked("endpoint", APPLY_SCOPE + ":" + key_digest):
                                    reconciliation = self.repository.reconcile_endpoint(
                                        APPLY_SCOPE, key_digest, preliminary)
                                    replay = self._replay(reconciliation, preliminary, recover=False)
                                    if replay is not None: return replay
                                    if (reconciliation.state == "nonterminal_wal"
                                            and reconciliation.transaction_id):
                                        transaction_id = reconciliation.transaction_id
                                        with session.locked("transaction", transaction_id):
                                            with session.locked("history", transaction_id):
                                                with session.locked("activity_outbox", lineage):
                                                    self._recover_one_under_locks(transaction_id)
                                                    return self.observe(transaction_id)
                                    with session.locked("transaction", lineage):
                                        with session.locked("history", lineage):
                                            with session.locked("activity_outbox", lineage):
                                                 return self._apply_under_locks(
                                                     value, key_digest, preliminary, proposal,
                                                     admission)

    def _apply_under_locks(self, value: ApplyInput, key_digest: str,
                           preliminary: str, proposal: schemas.ProposalRevision,
                           admission: Any
                           ) -> schemas.TransactionResult | schemas.TransactionProgress:
        now = self.clock.now()
        captured = self.capture_port.capture(captured_at=now, proposal_capability_active=True)
        expected = proposal.expected_snapshot
        expected_entries = {entry.concept: entry for entry in expected.concepts}
        current_entries = {entry.concept: entry for entry in captured.snapshot.concepts}
        identity_fields = ("parent_device", "parent_inode", "leaf_device", "leaf_inode",
                           "ctime_ns", "mode", "size", "digest")
        concept_op_ids = {concept: sorted(op.op_id for op in proposal.operations
                                          if concept in (*op.reads, *op.writes))
                          for concept in expected_entries}
        for concept in sorted(set(expected_entries) & set(current_entries)):
            reviewed, current = expected_entries[concept], current_entries[concept]
            if (reviewed.revision == current.revision
                    and any(getattr(reviewed.identity, name) != getattr(current.identity, name)
                            for name in identity_fields)):
                expected_identity = reviewed.identity.to_mapping()
                actual_identity = current.identity.to_mapping()
                detail = {"schema": 1, "code": "target_identity_changed",
                    "proposal_id": proposal.proposal_id,
                    "proposal_revision": proposal.proposal_revision,
                    "op_id": concept_op_ids[concept][0], "concept": concept,
                    "expected": canonical_digest(expected_identity),
                    "actual": canonical_digest(actual_identity),
                    "message": "Canonical target identity differs from reviewed authority.",
                    "detected_at": now}
                schemas.Conflict.from_mapping(detail)
                raise RepositoryError("reviewed target identity changed",
                                      code="proposal_conflict", details=[detail])
        if captured.snapshot.validation_context_digest != expected.validation_context_digest:
            detail = {"schema": 1, "code": "validation_stale",
                "proposal_id": proposal.proposal_id,
                "proposal_revision": proposal.proposal_revision,
                "op_id": None, "concept": None,
                "expected": expected.validation_context_digest,
                "actual": captured.snapshot.validation_context_digest,
                "message": "Validation context differs from the reviewed authority.",
                "detected_at": now}
            # Parse through the closed public conflict schema before exposing
            # the detail so transport adapters cannot widen it accidentally.
            schemas.Conflict.from_mapping(detail)
            raise RepositoryError("current validation context differs from review",
                                  code="proposal_conflict", details=[detail])
        if (captured.snapshot.snapshot_digest != expected.snapshot_digest
                or captured.snapshot.capabilities != expected.capabilities
                or captured.snapshot.config_digest != expected.config_digest
                or captured.snapshot.validator_id != expected.validator_id
                or captured.snapshot.serializer_id != expected.serializer_id
                or captured.snapshot.operation_runtime_id != expected.operation_runtime_id):
            raise RepositoryError("current validation/runtime authority differs from review",
                                  code="proposal_conflict")
        fingerprint = _fingerprint(APPLY_SCOPE, self.repository.repository_id,
                                   value.fingerprint_mapping(), proposal.expected_snapshot)
        if fingerprint != preliminary:
            # Runtime identity is already bound by snapshot/request fields; keep
            # reconciliation identity stable rather than split one key.
            fingerprint = preliminary
        preview = self.repository.get_preview(proposal.preview_ref.preview_digest)
        repository_snapshot = self.repository.snapshot()
        decisions = _decision_rows(repository_snapshot, proposal.proposal_id,
                                   proposal.proposal_revision)
        service_authority = tuple(sorted((
            {"intent_id": intent.intent_id,
             "result_record_id": intent.assigned["result_record_id"],
             "record_id": intent.assigned["record_id"], "operation": intent.operation}
            for intent in repository_snapshot.service_intents
            if intent.proposal_id == proposal.proposal_id
            and intent.proposal_revision == proposal.proposal_revision),
            key=lambda row: row["intent_id"]))
        model = build_read_model(ReadModelInputs(proposal, captured.current_bundle, now,
                                                  decision_rows=decisions, preview=preview,
                                                  service_authority=service_authority))
        if model.read_model_token != value.read_model_token or model.decision_set_token != value.decision_set_token:
            raise RepositoryError("proposal read authority changed", code="stale_read_model")
        if (proposal.expected_snapshot.snapshot_digest != value.snapshot_digest
                or proposal.expected_snapshot.validation_context_digest != value.validation_context_digest):
            raise RepositoryError("proposal snapshot or validation context changed", code="proposal_conflict")
        accepted = projectors.accepted_apply_set({s.op_id: s for s in model.operation_states})
        if not accepted:
            raise InvalidStateError("accepted operation set is empty")
        by_id = {op.op_id: op for op in proposal.operations}
        for op_id in accepted:
            if any(dep not in accepted for dep in by_id[op_id].depends_on):
                raise RepositoryError("accepted operation set is not dependency-closed",
                                      code="proposal_conflict")
        selected = tuple(by_id[op_id] for op_id in accepted)
        expected_revisions = {entry.concept: "sha256:" + entry.revision
                              for entry in proposal.expected_snapshot.concepts}
        touched = sorted({concept for operation in selected
                          for concept in (*operation.reads, *operation.writes)})
        for concept in touched:
            reviewed = expected_entries.get(concept)
            actual_identity = self.publication.canonical_identity(concept)
            expected_identity = None if reviewed is None else {
                "parent_device": reviewed.identity.parent_device,
                "parent_inode": reviewed.identity.parent_inode,
                "leaf_device": reviewed.identity.leaf_device,
                "leaf_inode": reviewed.identity.leaf_inode,
                "ctime_ns": reviewed.identity.ctime_ns,
                "mode": reviewed.identity.mode & 0o7777,
                "size": reviewed.identity.size, "digest": "sha256:" + reviewed.revision}
            if expected_identity is None or actual_identity.to_mapping() != expected_identity:
                detail = {"schema": 1, "code": "target_identity_changed",
                    "proposal_id": proposal.proposal_id,
                    "proposal_revision": proposal.proposal_revision,
                    "op_id": concept_op_ids[concept][0], "concept": concept,
                    "expected": canonical_digest(expected_identity),
                    "actual": canonical_digest(actual_identity.to_mapping()),
                    "message": "Canonical target identity changed under final locks.",
                    "detected_at": now}
                schemas.Conflict.from_mapping(detail)
                raise RepositoryError("accepted target identity changed under final locks",
                                      code="proposal_conflict", details=[detail])
            if (concept not in expected_revisions
                    or actual_identity.digest != expected_revisions[concept]):
                raise RepositoryError("accepted target revision changed under final locks",
                                      code="proposal_conflict")
        staged = stage_operations(proposal.expected_snapshot, captured.concept_bytes,
                                  selected, codec=captured.codec)
        validation = build_validation(staged.before, staged.after, captured.validator,
                                      validation_context_digest=value.validation_context_digest)
        if not validation.status == "pass":
            raise RepositoryError("complete staged bundle validation failed", code="validation_failed")
        reviewed = {change.concept: change for change in preview.concept_changes}
        changed = {c: staged.after[c] for c in staged.contributors}
        for concept, after in changed.items():
            change = reviewed.get(concept)
            if change is None or base64.b64decode(change.after_base64, validate=True) != after:
                raise RepositoryError("restaged bytes differ from reviewed preview",
                                      code="contract_parity_failure")
        return self._start("apply", value, key_digest, fingerprint, proposal, decisions,
                            tuple(sorted(accepted)), changed, validation, preview, admission)

    def _start(self, operation: str, request: Any, key_digest: str, fingerprint: str,
               proposal: schemas.ProposalRevision, decisions: tuple[schemas.DecisionRow, ...],
               accepted_op_ids: tuple[str, ...],
               changed: Mapping[str, bytes], validation: schemas.Validation,
               preview: schemas.Preview, admission: Any) -> schemas.TransactionResult | schemas.TransactionProgress:
        started_at = self.clock.now()
        transaction_id = self.repository.new_id("txn_" if operation == "apply" else "undo_")
        with self._waiters_lock: self._waiters[transaction_id] = threading.Event()
        self._fault("transaction_allocated", transaction_id=transaction_id)
        restore_digests = ({concept: "sha256:" + hashlib.sha256(data).hexdigest()
                            for concept, data in changed.items()} if operation == "undo" else None)
        prepared = self.publication.prepare(transaction_id, changed,
                                            restore_digests=restore_digests)
        self._fault("private_files_fsynced", transaction_id=transaction_id)
        targets = [dict(concept=t.concept, before_digest=t.before_digest, staged_digest=t.staged_digest,
                        backup_digest=t.backup_digest, restore_digest=t.restore_digest,
                        before_identity=t.before_identity.to_mapping(),
                        staged_identity=t.staged_identity.to_mapping(), resulting_identity=None,
                        stage_leaf=t.stage_leaf.to_mapping(), backup_leaf=t.backup_leaf.to_mapping())
                   for t in prepared.targets]
        parent_transaction_id = getattr(request, "transaction_id", None) if operation == "undo" else None
        undo_group_id = (getattr(request, "undo_group_id", None)
                         if operation == "undo" else self.repository.new_id("undo_"))
        undo_expires = _plus_days(_plus_ms(started_at, 2), 30) if operation == "apply" else None
        history = schemas.HistoryManifest.from_mapping(_with_checksum({
            "schema": 1, "transaction_id": transaction_id, "operation": operation,
            "parent_transaction_id": parent_transaction_id, "proposal_id": proposal.proposal_id,
            "proposal_revision": proposal.proposal_revision, "undo_group_id": undo_group_id,
            "targets": [{"concept": t.concept, "before_digest": t.before_digest,
                         "after_digest": t.staged_digest,
                         "before_identity": t.before_identity.to_mapping(),
                         "staged_identity": t.staged_identity.to_mapping(),
                         "backup_identity": t.backup_leaf.to_mapping()} for t in prepared.targets],
            "created_at": started_at,
            "expires_at": undo_expires or _plus_days(_plus_ms(started_at, 2), 30)}))
        intent = self._build_intent(operation, request, key_digest, fingerprint, proposal,
                                    decisions, accepted_op_ids, validation, prepared, history, undo_group_id,
                                    started_at, undo_expires)
        self._fault("before_intent_admission", transaction_id=transaction_id)
        intent = self.repository.publish_transaction_intent_if_admitted(admission, intent)
        if intent is None:
            if not self.repository.cleanup_unreferenced_transaction(transaction_id):
                raise RepositoryError("cancelled transaction private cleanup failed",
                                      code="storage_corrupt")
            with self._waiters_lock:
                self._waiters.pop(transaction_id, None)
            raise RepositoryError("transaction admission was cancelled before authority",
                                  code="operation_timeout")
        self._fault("intent_published", transaction_id=transaction_id)
        history = self.repository.publish_history(history)
        self._fault("history_published", transaction_id=transaction_id)
        wal_body = {"schema": 1, "operation": operation, "transaction_id": transaction_id,
                    "proposal_id": proposal.proposal_id, "proposal_revision": proposal.proposal_revision,
                    "idempotency_scope": APPLY_SCOPE if operation == "apply" else UNDO_SCOPE,
                    "idempotency_key_digest": key_digest, "request_fingerprint": fingerprint,
                    "transaction_intent_digest": intent.canonical_digest(),
                    "decision_set_token": request.decision_set_token if operation == "apply" else None,
                    "read_model_token": request.read_model_token,
                    "preview_digest": preview.preview_digest if operation == "apply" else None,
                    "snapshot_digest": proposal.expected_snapshot.snapshot_digest,
                    "validation_context_digest": proposal.expected_snapshot.validation_context_digest,
                    "validator_id": proposal.expected_snapshot.validator_id,
                    "serializer_id": proposal.expected_snapshot.serializer_id,
                    "operation_runtime_id": proposal.expected_snapshot.operation_runtime_id,
                    "accepted_op_ids": list(accepted_op_ids) if operation == "apply" else [],
                    "targets": targets,
                    "stage_dir_identity": prepared.stage_dir_identity.to_mapping(),
                    "backup_dir_identity": prepared.backup_dir_identity.to_mapping(),
                    "phase": "prepared", "commit_decided": False,
                    "publish_bitmap": [False] * len(targets), "rollback_bitmap": [False] * len(targets),
                    "recovery_attempts": 0, "created_at": started_at, "updated_at": started_at}
        wal = self.repository.publish_wal(_with_checksum(wal_body))
        self._fault("wal_prepared", transaction_id=transaction_id)
        endpoint_prepared = schemas.EndpointResult.from_mapping(_with_checksum({
            "schema": 1, "record_id": intent.value["endpoint"]["result_record_id"],
            "scope": intent.value["endpoint"]["scope"], "key_digest": key_digest,
            "request_fingerprint": fingerprint, "state": "prepared", "http_status": None,
            "response_digest": None, "response": None, "transaction_id": transaction_id,
            "created_at": started_at, "expires_at": intent.value["timestamps"]["replay_expires_at"]}))
        self._append_exact("endpoint_results", "endpoint_result", endpoint_prepared.to_mapping(),
                           "record_id")
        self._fault("endpoint_prepared", transaction_id=transaction_id)
        self._fault("before_commit_decided", transaction_id=transaction_id)
        wal = self._advance(wal, "commit_decided", commit_decided=True)
        self._fault("commit_decided", transaction_id=transaction_id)
        try:
            for index, target in enumerate(wal.targets):
                resulting_identity = self.publication.publish(
                    wal.transaction_id, _prepared_target(target))
                self._fault("target_parent_fsynced", transaction_id=transaction_id, target=index)
                bitmap = list(wal.publish_bitmap); bitmap[index] = True
                target_rows = [item.to_mapping() for item in wal.targets]
                target_rows[index]["resulting_identity"] = resulting_identity.to_mapping()
                phase = "content_published" if all(bitmap) else "publishing"
                wal = self._advance(wal, phase, publish_bitmap=bitmap, targets=target_rows)
                self._fault("publish_bitmap_advanced", transaction_id=transaction_id, target=index)
            wal = self._advance(wal, "finalizing")
            self._fault("wal_finalizing", transaction_id=transaction_id)
            return self._finalize(wal)
        except (StorageSafetyError, RepositoryCorruptError) as exc:
            self._mark_indeterminate(self.repository.read_wal(transaction_id), exc)
            raise RepositoryError("post-decision publication failed closed",
                                  code="indeterminate_outcome") from exc

    def _build_intent(self, operation: str, request: Any, key_digest: str, fingerprint: str,
                      proposal: schemas.ProposalRevision, decisions: tuple[schemas.DecisionRow, ...],
                      accepted_op_ids: tuple[str, ...],
                      validation: schemas.Validation, prepared: Any,
                      history: schemas.HistoryManifest, undo_group_id: str,
                      started_at: str, undo_expires: str | None) -> TransactionFinalizationIntent:
        transaction_id = prepared.transaction_id
        commit_at, complete_at, rollback_at = (_plus_ms(started_at, 1),
                                                _plus_ms(started_at, 2), _plus_ms(started_at, 3))
        replay_expires, audit_expires = _plus_days(complete_at, 7), _plus_days(complete_at, 180)
        result_id, audit_id = self.repository.new_id("rec_"), self.repository.new_id("rec_")
        rollback_audit_id = self.repository.new_id("rec_")
        activity_id, outbox_id = self.repository.new_id("act_"), self.repository.new_id("out_")
        revisions = {t.concept: t.staged_digest.removeprefix("sha256:") for t in prepared.targets}
        result_concrete = {"schema": 1, "operation": operation, "transaction_id": transaction_id,
            "proposal_id": proposal.proposal_id, "proposal_revision": proposal.proposal_revision,
            "status": "applied" if operation == "apply" else "undone", "replayed": False,
            "decision_set_token": (request.decision_set_token if operation == "apply"
                                   else canonical_digest([])),
            "validation_context_digest": proposal.expected_snapshot.validation_context_digest,
            "validation_digest": validation.digest, "concept_revisions": revisions,
            "activity_ids": [activity_id], "undo_group_id": undo_group_id,
            "completed_at": complete_at, "undo_expires_at": undo_expires, "error": None}
        response_digest = canonical_digest({"schema": 1, "data": result_concrete})
        notification = {"schema": 1, "proposal_id": proposal.proposal_id,
            "proposal_revision": proposal.proposal_revision,
            "event": "applied" if operation == "apply" else "undo_changed",
            "read_model_token": request.read_model_token, "transaction_id": transaction_id}
        endpoint_scope = APPLY_SCOPE if operation == "apply" else UNDO_SCOPE
        placeholder = lambda name: {"$intent_placeholder": name}
        common_audit = {"schema": 1, "operation": operation, "transaction_id": transaction_id,
            "parent_transaction_id": getattr(request, "transaction_id", None) if operation == "undo" else None,
            "proposal_id": proposal.proposal_id, "proposal_revision": proposal.proposal_revision,
            "actor": request.actor, "endpoint_scope": endpoint_scope,
            "idempotency_key_digest": key_digest, "request_fingerprint": fingerprint,
            "transaction_intent_digest": placeholder("transaction_intent_digest"),
            "accepted_op_ids": list(accepted_op_ids) if operation == "apply" else [],
            "decision_record_ids": sorted(d.record_id for d in decisions),
            "decision_rows_digest": canonical_digest([d.to_mapping() for d in decisions]),
            "decision_set_token": request.decision_set_token if operation == "apply" else canonical_digest([]),
            "preview_digest": proposal.preview_ref.preview_digest if operation == "apply" else proposal.expected_snapshot.snapshot_digest,
            "snapshot_digest": proposal.expected_snapshot.snapshot_digest,
            "validation_context_digest": proposal.expected_snapshot.validation_context_digest,
            "validator_id": proposal.expected_snapshot.validator_id,
            "serializer_id": proposal.expected_snapshot.serializer_id,
            "operation_runtime_id": proposal.expected_snapshot.operation_runtime_id,
            "validation_profile": validation.profile, "validation_digest": validation.digest,
            "validation_status": validation.status,
            "history_manifest_digest": history.canonical_digest(),
            "target_before_revisions": placeholder("target_before_revisions"),
            "target_after_revisions": placeholder("target_after_revisions"),
            "target_before_identities": placeholder("target_before_identities"),
            "target_resulting_identities": placeholder("target_resulting_identities"),
            "target_backup_identities": placeholder("target_backup_identities"),
            "resulting_revisions": placeholder("resulting_revisions"),
            "undo_group_id": undo_group_id, "started_at": started_at}
        commit_audit = {**common_audit, "record_id": audit_id, "phase": "committed",
            "status": "applied" if operation == "apply" else "undone",
            "activity_ids": [activity_id], "outbox_ids": [outbox_id],
            "commit_decided_at": commit_at, "completed_at": complete_at,
            "error_digest": None, "checksum": placeholder("candidate_checksum")}
        rollback_error = ErrorEnvelope(ErrorBody(
            "apply_rejected", "Transaction rolled back before commit decision.", transaction_id,
            details=({"transaction_id": transaction_id},))).to_mapping()
        rollback_audit = {**common_audit, "record_id": rollback_audit_id,
            "phase": "rolled_back", "status": "rejected", "activity_ids": [], "outbox_ids": [],
            "commit_decided_at": None, "completed_at": rollback_at,
            "error_digest": canonical_digest(rollback_error), "checksum": placeholder("candidate_checksum")}
        activity = {"type": "activity", "id": activity_id, "actor": request.actor,
            "origin": "proposal", "action": operation, "ids": sorted(revisions),
            "summary": f"Proposal {operation}", "undoable": operation == "apply",
            "detail": {"transaction_id": transaction_id, "proposal_id": proposal.proposal_id,
                       "proposal_revision": proposal.proposal_revision, "undo_group_id": undo_group_id}}
        outbox = _with_checksum({"schema": 1, "outbox_id": outbox_id,
            "aggregate_kind": "transaction", "aggregate_id": proposal.proposal_id,
            "proposal_id": proposal.proposal_id, "proposal_revision": proposal.proposal_revision,
            "event_type": notification["event"], "mutation_intent_id": transaction_id,
            "result_record_id": result_id, "payload": notification,
            "payload_digest": canonical_digest(notification), "state": "pending", "event_cursor": None,
            "attempts": 0, "created_at": complete_at, "updated_at": complete_at})
        endpoint = {"schema": 1, "record_id": result_id, "scope": endpoint_scope,
            "key_digest": key_digest, "request_fingerprint": fingerprint, "state": "terminal",
            "http_status": 200, "response_digest": response_digest,
            "response": {"schema": 1, "data": {**result_concrete,
                "concept_revisions": placeholder("concept_revisions")}},
            "transaction_id": transaction_id, "created_at": complete_at,
            "expires_at": replay_expires, "checksum": placeholder("candidate_checksum")}
        rollback_endpoint = {"schema": 1, "record_id": result_id, "scope": endpoint_scope,
            "key_digest": key_digest, "request_fingerprint": fingerprint, "state": "terminal",
            "http_status": 409, "response_digest": canonical_digest(rollback_error),
            "response": rollback_error, "transaction_id": transaction_id,
            "created_at": rollback_at, "expires_at": replay_expires,
            "checksum": placeholder("candidate_checksum")}
        accepted_ids = set(accepted_op_ids)
        read_concepts = sorted({concept for op in proposal.operations
                                if operation == "apply" and op.op_id in accepted_ids
                                for concept in op.reads} or revisions)
        snapshot_revisions = {entry.concept: entry.revision for entry in proposal.expected_snapshot.concepts}
        read_revisions = {concept: "sha256:" + snapshot_revisions[concept]
                          for concept in read_concepts if concept in snapshot_revisions}
        body = {"schema": 1, "transaction_id": transaction_id, "operation": operation,
            "parent_transaction_id": getattr(request, "transaction_id", None) if operation == "undo" else None,
            "proposal_id": proposal.proposal_id, "proposal_revision": proposal.proposal_revision,
            "actor": request.actor, "endpoint": {"scope": endpoint_scope, "key_digest": key_digest,
                "request_fingerprint": fingerprint, "result_record_id": result_id},
            "accepted_authority": {"read_model_token": request.read_model_token,
                "read_revisions": read_revisions, "snapshot_digest": proposal.expected_snapshot.snapshot_digest,
                "decision_set_token": request.decision_set_token if operation == "apply" else None,
                "preview_digest": proposal.preview_ref.preview_digest if operation == "apply" else None,
                "validation_context_digest": proposal.expected_snapshot.validation_context_digest,
                "validator_id": proposal.expected_snapshot.validator_id,
                "serializer_id": proposal.expected_snapshot.serializer_id,
                "operation_runtime_id": proposal.expected_snapshot.operation_runtime_id},
            "accepted_op_ids": list(accepted_op_ids) if operation == "apply" else [],
            "decision_record_ids": sorted(d.record_id for d in decisions) if operation == "apply" else [],
            "decision_rows_digest": canonical_digest([d.to_mapping() for d in decisions]) if operation == "apply" else None,
            "validation": {"profile": validation.profile, "digest": validation.digest, "status": validation.status},
            "candidate_ids": {"commit": {"audit_record_id": audit_id, "activity_ids": [activity_id],
                "outbox_ids": [outbox_id], "result_record_id": result_id},
                "rollback": {"audit_record_id": rollback_audit_id, "activity_ids": [],
                    "outbox_ids": [], "result_record_id": result_id}},
            "candidates": {"commit": {"audit": commit_audit, "activities": [activity],
                "outbox": [outbox], "result": endpoint},
                "rollback": {"audit": rollback_audit, "activities": [], "outbox": [],
                    "result": rollback_endpoint}},
            "undo_group_id": undo_group_id,
            "timestamps": {"started_at": started_at, "commit_decided_at": commit_at,
                "commit_completed_at": complete_at, "rollback_completed_at": rollback_at,
                "undo_expires_at": undo_expires, "audit_expires_at": audit_expires,
                "replay_expires_at": replay_expires},
            "retention": {"audit_until": audit_expires, "replay_until": replay_expires,
                "undo_until": undo_expires},
            "history": {"relative_path": f"transactions/{transaction_id}/history.json",
                "manifest_digest": history.canonical_digest()},
            "targets": [{"concept": t.concept, "before_digest": t.before_digest,
                "after_digest": t.staged_digest,
                "before_identity": t.before_identity.to_mapping(),
                "staged_identity": t.staged_identity.to_mapping(),
                "resulting_identity": placeholder("target_resulting_identities"),
                "backup_identity": t.backup_leaf.to_mapping()}
                for t in prepared.targets],
            "allowed_placeholders": ["candidate_checksum", "concept_revisions",
                "resulting_revisions", "target_after_revisions", "target_backup_identities", "target_before_identities",
                "target_before_revisions", "target_resulting_identities", "transaction_intent_digest"]}
        return TransactionFinalizationIntent.from_mapping(_with_checksum(body))

    def _advance(self, wal: schemas.WAL, phase: str, **changes: Any) -> schemas.WAL:
        body = wal.to_mapping()
        body.update({key: list(value) if key in {"publish_bitmap", "rollback_bitmap"}
                     and isinstance(value, tuple) else value for key, value in changes.items()})
        body.update(phase=phase, updated_at=self.clock.now())
        body["checksum"] = compute_checksum(body)
        return self.repository.publish_wal(body)

    def _append_exact(self, aggregate: str, kind: str, payload: Mapping[str, Any], identity: str) -> None:
        matches = [row.payload for row in self.repository.snapshot().aggregate(aggregate)
                   if row.kind == kind and row.payload.get(identity) == payload.get(identity)]
        if kind == "endpoint_result" and matches:
            terminal = [row for row in matches if row.get("state") in {"terminal", "indeterminate"}]
            if terminal:
                if len(terminal) != 1 or canonical_digest(terminal[0]) != canonical_digest(payload):
                    raise RepositoryCorruptError("endpoint result terminal candidate is contradictory")
                return
            prepared = [row for row in matches if row.get("state") == "prepared"]
            if len(prepared) != 1 or any(prepared[0].get(name) != payload.get(name) for name in
                    ("record_id", "scope", "key_digest", "request_fingerprint", "transaction_id")):
                raise RepositoryCorruptError("endpoint prepared authority is contradictory")
            self.repository.append(aggregate, kind, payload)
            return
        if matches:
            if len(matches) != 1 or canonical_digest(matches[0]) != canonical_digest(payload):
                raise RepositoryCorruptError(f"{kind} candidate identity has contradictory bytes")
            return
        self.repository.append(aggregate, kind, payload)

    def _finalize(self, wal: schemas.WAL, *, branch: str = "commit",
                  raise_terminal_error: bool = True) -> schemas.TransactionResult | None:
        """Persist a terminal branch before optionally returning its error.

        Foreground rollback callers retain the durable terminal error. Recovery
        consumes that verified terminal result instead of treating it as a
        failed recovery.
        """
        intent, _ = self.repository.verify_transaction_association(wal)
        history = self.repository.finalize_history(wal, branch=branch)
        candidate = substitute(intent, wal, branch)
        audit = schemas.TransactionAudit.from_mapping(candidate["audit"])
        self._append_exact("transactions", "transaction", audit.to_mapping(), "record_id")
        self._fault("audit_appended", transaction_id=wal.transaction_id)
        if candidate["activities"] and self.activity is None:
            raise RepositoryError("activity finalization adapter is unavailable", code="transaction_recovering")
        for activity in candidate["activities"]:
            self.activity.append_activity(activity["id"], activity)
            self._fault("activity_appended", transaction_id=wal.transaction_id)
        for outbox in candidate["outbox"]:
            self._append_exact("outbox", "outbox", outbox, "outbox_id")
            self._fault("outbox_appended", transaction_id=wal.transaction_id)
        endpoint = schemas.EndpointResult.from_mapping(candidate["result"])
        self._append_exact("endpoint_results", "endpoint_result", endpoint.to_mapping(), "record_id")
        self._fault("endpoint_terminal", transaction_id=wal.transaction_id)
        wal = self._advance(wal, "committed" if branch == "commit" else "rolled_back")
        self._fault("wal_terminal", transaction_id=wal.transaction_id)
        if self.outbox is not None:
            for outbox in candidate["outbox"]:
                try:
                    cursor = self.outbox.append_proposal_event(outbox["outbox_id"], outbox["payload"])
                    self._fault("event_appended", transaction_id=wal.transaction_id)
                    self.repository.mark_outbox_appended(outbox["outbox_id"], cursor)
                    self._fault("receipt_updated", transaction_id=wal.transaction_id)
                except Exception: pass
        with self._waiters_lock:
            waiter = self._waiters.pop(wal.transaction_id, None)
            if waiter: waiter.set()
        if branch != "commit" and raise_terminal_error:
            raise TransactionTerminalError(PersistedErrorResponse(
                endpoint.http_status or 500, endpoint.response or {}))
        if branch != "commit":
            return None
        return schemas.TransactionResult.from_mapping(dict(endpoint.response or {})["data"])

    def observe(self, transaction_id: str) -> schemas.TransactionResult | schemas.TransactionProgress:
        snap = self.repository.snapshot()
        results = [schemas.EndpointResult.from_mapping(w.payload)
                   for w in snap.aggregate("endpoint_results")
                   if w.kind == "endpoint_result" and w.payload.get("transaction_id") == transaction_id
                   and w.payload.get("state") == "terminal"]
        if results:
            envelope = dict(results[-1].response or {})
            if "error" in envelope:
                raise TransactionTerminalError(PersistedErrorResponse(
                    results[-1].http_status or 500, envelope))
            body = envelope.get("data", {})
            return schemas.TransactionResult.from_mapping(body)
        wal = self.repository.read_wal(transaction_id)
        progress = projectors.map_wal_to_progress(wal)
        if progress is None:
            raise RepositoryCorruptError("terminal WAL has no terminal endpoint result")
        return schemas.TransactionProgress.from_mapping(progress.to_mapping())

    def _recover_one(self, transaction_id: str) -> None:
        """Recover under the complete global lock order in every entry path."""
        initial = self.repository.read_wal(transaction_id)
        concepts = [target.concept for target in initial.targets]
        lineage = f"{initial.proposal_id}:{initial.proposal_revision}"
        endpoint = initial.idempotency_scope + ":" + initial.idempotency_key_digest
        with self.repository.transaction_session() as session:
            with session.ranked("bundle"):
                with self.publication.exclusive():
                    with session.ranked("concept"):
                        with self.publication.concept_locks(concepts):
                            with session.locked("lineage", lineage):
                                with session.locked("endpoint", endpoint):
                                    with session.locked("transaction", transaction_id):
                                        with session.locked("history", transaction_id):
                                            with session.locked("activity_outbox", lineage):
                                                self._recover_one_under_locks(transaction_id)

    def _recover_one_under_locks(self, transaction_id: str) -> None:
        wal = self.repository.read_wal(transaction_id)
        if wal.phase in {"committed", "rolled_back"}:
            self._repair_terminal(wal)
            return
        if wal.phase == "indeterminate": return
        try:
            self.repository.verify_transaction_association(wal)
            for target in wal.targets:
                backup = self.publication.read_backup(wal.transaction_id, target.backup_leaf)
                if "sha256:" + hashlib.sha256(backup).hexdigest() != target.backup_digest:
                    raise RepositoryCorruptError("transaction backup digest mismatch")
        except Exception as exc:
            if wal.commit_decided:
                self._mark_indeterminate(wal, exc)
                return
            # No canonical rename is permitted before the decision. Preserve
            # ambiguous private authority and block serving rather than invent
            # a rollback candidate from a missing intent/history pair.
            raise RepositoryCorruptError(
                "pre-decision transaction association is unsafe; evidence preserved") from exc
        attempts = wal.recovery_attempts + 1
        if attempts > 100:
            self._mark_indeterminate(wal, RepositoryCorruptError("recovery attempt limit exceeded"))
            return
        wal = self._advance(wal, wal.phase, recovery_attempts=attempts)
        if not wal.commit_decided:
            if wal.phase in {"prepared", "rolling_back"}:
                bitmap = list(wal.rollback_bitmap)
                for index in range(len(wal.targets) - 1, -1, -1):
                    if bitmap[index]:
                        continue
                    state = self.publication.classify(_prepared_target(wal.targets[index]))
                    if state != "before":
                        self._mark_indeterminate(wal, RepositoryCorruptError(
                            "pre-decision canonical bytes are not exact before bytes"))
                        return
                    bitmap[index] = True
                    wal = self._advance(wal, "rolling_back", rollback_bitmap=bitmap)
                    self._fault("rollback_bitmap_advanced", transaction_id=wal.transaction_id,
                                target=index)
                self._finalize(wal, branch="rollback", raise_terminal_error=False)
            return
        try:
            if wal.phase in {"commit_decided", "publishing"}:
                for index, target in enumerate(wal.targets):
                    if wal.publish_bitmap[index]:
                        if self.publication.classify(_prepared_target(target)) != "staged":
                            raise RepositoryCorruptError("published canonical identity changed")
                        continue
                    resulting_identity = self.publication.publish(
                        wal.transaction_id, _prepared_target(target))
                    bitmap = list(wal.publish_bitmap); bitmap[index] = True
                    target_rows = [item.to_mapping() for item in wal.targets]
                    target_rows[index]["resulting_identity"] = resulting_identity.to_mapping()
                    phase = "content_published" if all(bitmap) else "publishing"
                    wal = self._advance(wal, phase, publish_bitmap=bitmap, targets=target_rows)
            if wal.phase == "content_published": wal = self._advance(wal, "finalizing")
            if wal.phase == "finalizing": self._finalize(wal)
        except Exception as exc:
            self._mark_indeterminate(wal, exc)

    def _repair_terminal(self, wal: schemas.WAL) -> None:
        """Validate or reconstruct the complete intent-derived terminal chain."""
        branch = "commit" if wal.phase == "committed" else "rollback"
        intent, history = self.repository.verify_transaction_association(wal)
        candidate = substitute(intent, wal, branch)
        audit = schemas.TransactionAudit.from_mapping(candidate["audit"])
        self._append_exact("transactions", "transaction", audit.to_mapping(), "record_id")
        if candidate["activities"] and self.activity is None:
            raise RepositoryCorruptError("terminal transaction activity adapter is unavailable")
        for activity in candidate["activities"]:
            self.activity.append_activity(activity["id"], activity)
        for outbox in candidate["outbox"]:
            rows = [row.payload for row in self.repository.snapshot().aggregate("outbox")
                    if row.kind == "outbox" and row.payload.get("outbox_id") == outbox["outbox_id"]]
            if rows:
                if not any(canonical_digest(row) == canonical_digest(outbox) for row in rows):
                    raise RepositoryCorruptError("terminal outbox lacks exact intent candidate")
            else:
                self._append_exact("outbox", "outbox", outbox, "outbox_id")
        endpoint = schemas.EndpointResult.from_mapping(candidate["result"])
        endpoint_rows = [row.payload for row in self.repository.snapshot().aggregate("endpoint_results")
                         if row.kind == "endpoint_result"
                         and row.payload.get("record_id") == endpoint.record_id
                         and row.payload.get("state") == "terminal"]
        if not endpoint_rows:
            tombstones = [schemas.Tombstone.from_mapping(row.payload)
                          for row in self.repository.snapshot().aggregate("tombstones")
                          if row.kind == "tombstone"
                          and row.payload.get("transaction_id") == wal.transaction_id]
            exact_tombstone = any((row.scope, row.key_digest, row.request_fingerprint,
                                   row.response_digest) ==
                                  (endpoint.scope, endpoint.key_digest,
                                   endpoint.request_fingerprint, endpoint.response_digest)
                                  for row in tombstones)
            if not exact_tombstone:
                self._append_exact("endpoint_results", "endpoint_result",
                                   endpoint.to_mapping(), "record_id")
        else:
            self._append_exact("endpoint_results", "endpoint_result",
                               endpoint.to_mapping(), "record_id")
        self.repository.verify_audit_transaction_chain(audit)
        if self.outbox is not None:
            for outbox in candidate["outbox"]:
                pending = {row.outbox_id for row in self.repository.pending_outbox()}
                if outbox["outbox_id"] not in pending:
                    continue
                try:
                    cursor = self.outbox.append_proposal_event(
                        outbox["outbox_id"], outbox["payload"])
                    self.repository.mark_outbox_appended(outbox["outbox_id"], cursor)
                except (OSError, TimeoutError) as exc:
                    # Delivery remains durable pending authority; startup is
                    # allowed to serve and a later recovery/reconciler retries.
                    print(f"proposal transaction outbox delivery deferred ({type(exc).__name__})",
                          file=sys.stderr)

    def _mark_indeterminate(self, wal: schemas.WAL, cause: Exception) -> None:
        """Persist typed uncertain authority without deleting any evidence."""
        terminal = [row.payload for row in self.repository.snapshot().aggregate("endpoint_results")
                    if row.kind == "endpoint_result"
                    and row.payload.get("transaction_id") == wal.transaction_id
                    and row.payload.get("state") == "terminal"]
        if terminal:
            # A competing recovery may have completed finalization while this
            # worker was between append and WAL replacement.  Accept only the
            # exact immutable intent candidate; anything else remains corrupt
            # without overwriting the certain terminal authority.
            branch = "rollback" if terminal[0].get("http_status") == 409 else "commit"
            try:
                intent, _ = self.repository.verify_transaction_association(wal)
                expected = schemas.EndpointResult.from_mapping(
                    substitute(intent, wal, branch)["result"]).to_mapping()
                if len(terminal) == 1 and canonical_digest(terminal[0]) == canonical_digest(expected):
                    terminal_phase = "rolled_back" if branch == "rollback" else "committed"
                    if wal.phase != terminal_phase:
                        self._advance(wal, terminal_phase)
                    return
            except Exception:
                pass
            self.repository.mark_transaction_corrupt(
                wal.transaction_id, "authority_contradiction",
                str(cause).encode("utf-8")[:4096])
            return
        if wal.phase != "indeterminate":
            wal = self._advance(wal, "indeterminate")
        # Intent/history may be the authority that is corrupt or missing.
        # Derive endpoint identity only from the independently persisted,
        # exact prepared endpoint authority; never reconstruct it from current
        # bytes, a clock, or a corrupt intent.
        prepared = [schemas.EndpointResult.from_mapping(row.payload)
                    for row in self.repository.snapshot().aggregate("endpoint_results")
                    if row.kind == "endpoint_result"
                    and row.payload.get("transaction_id") == wal.transaction_id
                    and row.payload.get("state") == "prepared"
                    and row.payload.get("scope") == wal.idempotency_scope
                    and row.payload.get("key_digest") == wal.idempotency_key_digest
                    and row.payload.get("request_fingerprint") == wal.request_fingerprint]
        if len(prepared) != 1:
            raise RepositoryCorruptError(
                "indeterminate transaction lacks one verified prepared endpoint")
        prepared_endpoint = prepared[0]
        result_id = prepared_endpoint.record_id
        expires_at = prepared_endpoint.expires_at
        self.repository.mark_transaction_corrupt(wal.transaction_id, "authority_contradiction",
                                                 str(cause).encode("utf-8")[:4096])
        if result_id is not None:
            response = {"schema": 1, "error": {"code": "indeterminate_outcome",
                "message": "Transaction outcome is indeterminate.",
                "details": {"transaction_id": wal.transaction_id}}}
            endpoint = _with_checksum({"schema": 1, "record_id": result_id,
                "scope": wal.idempotency_scope, "key_digest": wal.idempotency_key_digest,
                "request_fingerprint": wal.request_fingerprint, "state": "indeterminate",
                "http_status": 409, "response_digest": canonical_digest(response), "response": response,
                "transaction_id": wal.transaction_id, "created_at": self.clock.now(),
                "expires_at": expires_at})
            self._append_exact("endpoint_results", "endpoint_result", endpoint, "record_id")

    def recover_all(self) -> tuple[str, ...]:
        recovered: list[str] = []
        # One process-wide recovery ownership interval covers discovery through
        # serving release. Creators, retries, direct writers, and shared capture
        # cannot enter between the final authority check and resume.
        with self.repository.transaction_session() as session:
            with session.ranked("bundle"):
                with self.publication.exclusive():
                    initial = self.repository.refresh_recovery_report()
                    concepts: set[str] = set()
                    transaction_ids = sorted({item.transaction_id for item in initial.items})
                    for transaction_id in transaction_ids:
                        try:
                            concepts.update(target.concept for target in
                                            self.repository.read_wal(transaction_id).targets)
                        except Exception:
                            # Pre-WAL private evidence has no canonical publish
                            # authority and therefore contributes no concept lock.
                            pass
                    with session.ranked("concept"):
                        with self.publication.concept_locks(concepts):
                            recovery_identity = canonical_digest(transaction_ids)
                            with session.locked("lineage", "recovery:" + recovery_identity):
                                with session.locked("endpoint", "recovery:" + recovery_identity):
                                    with session.locked("transaction", "recovery:" + recovery_identity):
                                        with session.locked("history", "recovery:" + recovery_identity):
                                            with session.locked("activity_outbox", "recovery:" + recovery_identity):
                                                # Revalidate classification only
                                                # after the entire lock envelope.
                                                report = self.repository.refresh_recovery_report()
                                                for item in report.items:
                                                    if item.classification in {
                                                            "rollback_required", "rollforward_required",
                                                            "terminal"}:
                                                        try:
                                                            self._recover_one_under_locks(item.transaction_id)
                                                        except Exception as exc:
                                                            if item.classification == "terminal":
                                                                self.repository.mark_transaction_corrupt(
                                                                    item.transaction_id,
                                                                    "authority_contradiction",
                                                                    str(exc).encode("utf-8")[:4096])
                                                            raise
                                                        recovered.append(item.transaction_id)
                                                    elif item.classification in {
                                                            "unreferenced_private", "orphan_history"}:
                                                        if not self.repository.cleanup_unreferenced_transaction(
                                                                item.transaction_id):
                                                            raise RepositoryError(
                                                                "pre-WAL transaction evidence is ambiguous",
                                                                code="storage_corrupt")
                                                        recovered.append(item.transaction_id)
                                                    elif item.classification == "indeterminate":
                                                        self._mark_indeterminate(
                                                            self.repository.read_wal(item.transaction_id),
                                                            RepositoryCorruptError(
                                                                "startup transaction authority is indeterminate"))
                                                        recovered.append(item.transaction_id)
                                                self.repository.refresh_recovery_report()
                                                if not self.repository.recovery_report.serving_safe:
                                                    raise RepositoryError(
                                                        "transaction recovery did not reach a serving-safe state",
                                                        code="transaction_recovering")
                                                self.repository.resume_after_recovery()
        return tuple(recovered)

    def undo(self, request: UndoInput | Mapping[str, Any] | bytes | str,
             idempotency_key: str) -> schemas.TransactionResult | schemas.TransactionProgress:
        # The complete restore path is deliberately reached only from verified
        # apply audit/history authority.  It is not exposed by service/transport.
        value = request if isinstance(request, UndoInput) else UndoInput.from_mapping(request)
        key_digest = _key_digest(idempotency_key)
        fingerprint = _fingerprint(UNDO_SCOPE, self.repository.repository_id,
                                   value.fingerprint_mapping())
        replay = self._replay(self.repository.reconcile_endpoint(
            UNDO_SCOPE, key_digest, fingerprint), fingerprint, recover=False)
        if replay is not None: return replay
        admission = self.repository.new_transaction_admission_fence()
        return self._run_with_deadline(
            lambda: self._undo_sync(value, key_digest, fingerprint, admission),
            UNDO_SCOPE, key_digest, fingerprint, admission)

    def _undo_sync(self, value: UndoInput, key_digest: str,
                   fingerprint: str, admission: Any
                   ) -> schemas.TransactionResult | schemas.TransactionProgress:
        # Read only enough authority to derive the sorted lock set.  Every
        # authorization check is repeated after all transaction locks are held.
        snap = self.repository.snapshot()
        audits = [schemas.TransactionAudit.from_mapping(w.payload)
                  for w in snap.aggregate("transactions") if w.kind == "transaction"
                  and w.payload.get("transaction_id") == value.transaction_id]
        if len(audits) != 1 or audits[0].operation != "apply":
            raise InvalidStateError("undo requires one committed apply audit")
        _, initial_history = self.repository.verify_audit_transaction_chain(audits[0])
        targets = [target.concept for target in initial_history.targets]
        lineage = f"{value.proposal_id}:{value.proposal_revision}"
        with self.repository.transaction_session() as session:
            with session.ranked("bundle"):
                with self.publication.exclusive():
                    with session.ranked("concept"):
                        with self.publication.concept_locks(targets):
                            with session.locked("lineage", lineage):
                                with session.locked("endpoint", UNDO_SCOPE + ":" + key_digest):
                                    reconciliation = self.repository.reconcile_endpoint(
                                        UNDO_SCOPE, key_digest, fingerprint)
                                    replay = self._replay(reconciliation, fingerprint, recover=False)
                                    if replay is not None: return replay
                                    if (reconciliation.state == "nonterminal_wal"
                                            and reconciliation.transaction_id):
                                        transaction_id = reconciliation.transaction_id
                                        with session.locked("transaction", transaction_id):
                                            with session.locked("history", transaction_id):
                                                with session.locked("activity_outbox", lineage):
                                                    self._recover_one_under_locks(transaction_id)
                                                    return self.observe(transaction_id)
                                    with session.locked("transaction", value.transaction_id):
                                        with session.locked("history", value.transaction_id):
                                            with session.locked("activity_outbox", lineage):
                                                return self._undo_under_locks(
                                                    value, key_digest, fingerprint, admission)

    def _undo_under_locks(self, value: UndoInput, key_digest: str,
                          fingerprint: str, admission: Any
                          ) -> schemas.TransactionResult | schemas.TransactionProgress:
        snap = self.repository.snapshot()
        audits = [schemas.TransactionAudit.from_mapping(w.payload)
                  for w in snap.aggregate("transactions") if w.kind == "transaction"
                  and w.payload.get("transaction_id") == value.transaction_id]
        if len(audits) != 1 or audits[0].operation != "apply":
            raise InvalidStateError("undo requires one committed apply audit")
        audit = audits[0]
        if audit.proposal_id != value.proposal_id or audit.proposal_revision != value.proposal_revision:
            raise RepositoryError("undo parent proposal does not match", code="proposal_conflict")
        if audit.undo_group_id != value.undo_group_id:
            raise RepositoryError("undo group does not match apply audit", code="proposal_conflict")
        if any(w.kind == "transaction" and w.payload.get("operation") == "undo"
               and w.payload.get("parent_transaction_id") == value.transaction_id
               and w.payload.get("status") == "undone" for w in snap.aggregate("transactions")):
            raise InvalidStateError("apply transaction already has a committed undo")
        _, history = self.repository.verify_audit_transaction_chain(audit)
        now = self.clock.now()
        if now >= history.expires_at:
            raise RepositoryError("undo history expired", code="preview_expired")
        restore: dict[str, bytes] = {}
        # Publication owns backup identity reads; an adapter without this
        # optional method cannot safely authorize undo.
        reader = getattr(self.publication, "read_backup", None)
        if reader is None:
            raise RepositoryError("verified backup reader is unavailable", code="storage_corrupt")
        for target in history.targets:
            resulting_identity = audit.target_resulting_identities.get(target.concept)
            if (resulting_identity is None
                    or self.publication.canonical_identity(target.concept).to_mapping()
                    != resulting_identity.to_mapping()):
                raise RepositoryError("current target no longer equals apply result", code="proposal_conflict")
            restore[target.concept] = reader(value.transaction_id, target.backup_identity)
        proposal = self._proposal(value.proposal_id, value.proposal_revision)
        captured = self.capture_port.capture(captured_at=now, proposal_capability_active=True)
        complete_after = dict(captured.concept_bytes); complete_after.update(restore)
        validation = build_validation(captured.concept_bytes, complete_after, captured.validator,
                                      validation_context_digest=captured.snapshot.validation_context_digest)
        if validation.status != "pass":
            raise RepositoryError("complete restored bundle validation failed", code="validation_failed")
        preview = self.repository.get_preview(proposal.preview_ref.preview_digest)
        return self._start("undo", value, key_digest, fingerprint, proposal, (), (), restore,
                           validation, preview, admission)


__all__ = ["APPLY_SCOPE", "UNDO_SCOPE", "WAIT_SECONDS", "ProposalTransactionCoordinator"]
