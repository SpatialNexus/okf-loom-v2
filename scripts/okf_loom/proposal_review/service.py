"""Transport-neutral Proposal Review Bundle 3 domain service.

No method in this module publishes bundle content.  The only mutation ports
are :class:`ProposalRepositoryV1` (review authority) and the optional outbox
reconciler (notifications).
"""
from __future__ import annotations

import datetime
import hashlib
import concurrent.futures
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from . import pagination, projectors, schemas
from .canonical import canonical_digest, compute_checksum, dumps_canonical
from .errors import (CursorExpiredError, InvalidStateError, ProposalReviewError, RepositoryError,
                     SchemaError, StaleReadModelError)
from . import ports as _ports
from .preview import build_preview
from .read_model import ProposalReadModel, ReadModelInputs, build_read_model
from .repository import AGGREGATES, EndpointPreparation, ProposalRepositorySnapshot, ProposalRepositoryV1
from .service_contracts import (CreateProposalInput, DecisionInput, StrictListQuery,
                                 ApplyInput, SuccessEnvelope, UndoInput,
                                 WithdrawalInput)
from .staging import stage_operations


PROPOSAL_CAPABILITY = "okf.cap.experimental.proposal_diff_review.v1"
CREATE_SCOPE = "/__proposal/v1/proposals"
PREVIEW_HEARTBEAT_INTERVAL_SECONDS = 20.0


@dataclass(frozen=True)
class CapturedServiceBundle:
    snapshot: schemas.Snapshot
    concept_bytes: Mapping[str, bytes]
    current_bundle: _ports.BundleView
    codec: _ports.DocumentCodec
    validator: _ports.FrozenSnapshotValidator


@runtime_checkable
class BundleCapturePort(Protocol):
    def capture(self, *, captured_at: str,
                  proposal_capability_active: bool) -> CapturedServiceBundle: ...


@runtime_checkable
class LegacyReaderPort(Protocol):
    def verify(self, identity: schemas.LegacyIdentity) -> schemas.LegacyProjection: ...


def _plus_seconds(value: str, seconds: int) -> str:
    dt = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    dt += datetime.timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _key_digest(key: str) -> str:
    from .ids import validate_idempotency_key
    key = validate_idempotency_key(key)
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _endpoint(state: str, *, record_id: str, scope: str, key_digest: str, fingerprint: str,
              now: str, expires_at: str, status: int | None = None,
              response: Mapping[str, Any] | None = None) -> schemas.EndpointResult:
    body: dict[str, Any] = {
        "schema": 1, "record_id": record_id, "scope": scope, "key_digest": key_digest,
        "request_fingerprint": fingerprint, "state": state,
        "http_status": status, "response_digest": None if response is None else canonical_digest(response),
        "response": None if response is None else dict(response), "transaction_id": None,
        "created_at": now, "expires_at": expires_at,
    }
    body["checksum"] = compute_checksum(body)
    return schemas.EndpointResult.from_mapping(body)


def _outbox(outbox_id: str, event: Mapping[str, Any], *, now: str,
            intent_id: str, result_record_id: str) -> schemas.OutboxReceipt:
    body = {"schema": 1, "outbox_id": outbox_id, "aggregate_kind": "proposal",
            "aggregate_id": event["proposal_id"], "proposal_id": event["proposal_id"],
            "proposal_revision": event["proposal_revision"], "event_type": event["event"],
            "mutation_intent_id": intent_id, "result_record_id": result_record_id,
            "payload": dict(event),
            "payload_digest": canonical_digest(event), "state": "pending",
            "event_cursor": None, "attempts": 0, "created_at": now, "updated_at": now}
    body["checksum"] = compute_checksum(body)
    return schemas.OutboxReceipt.from_mapping(body)


def _stored_success(response: Mapping[str, Any]) -> SuccessEnvelope:
    if set(response) != {"schema", "data"} or response.get("schema") != 1 or not isinstance(response.get("data"), Mapping):
        raise RepositoryError("stored service result is not a success envelope",
                              code="storage_corrupt")
    return SuccessEnvelope(response["data"])


@dataclass(frozen=True)
class ProposalService:
    repository: ProposalRepositoryV1
    capture_port: BundleCapturePort
    clock: _ports.Clock
    runtime_identities: _ports.RuntimeIdentities
    proposal_capability_active: bool = False
    legacy_reader: LegacyReaderPort | None = None
    transaction_coordinator: Any | None = None
    transaction_publication: Any | None = None
    _lifecycle_lock: threading.Lock = field(default_factory=threading.Lock, init=False,
                                            repr=False, compare=False)
    _executors: set[concurrent.futures.ThreadPoolExecutor] = field(
        default_factory=set, init=False, repr=False, compare=False)
    _heartbeats: set[tuple[threading.Event, threading.Thread]] = field(
        default_factory=set, init=False, repr=False, compare=False)
    _closed: threading.Event = field(default_factory=threading.Event, init=False,
                                     repr=False, compare=False)

    def _ensure_open(self) -> None:
        if self._closed.is_set():
            raise RepositoryError("Proposal Review service is closed",
                                  code="capability_unavailable")

    def _register_executor(self, executor: concurrent.futures.ThreadPoolExecutor) -> None:
        with self._lifecycle_lock:
            if self._closed.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                raise RepositoryError("Proposal Review service is closed",
                                      code="capability_unavailable")
            self._executors.add(executor)

    def _executor_done(self, executor: concurrent.futures.ThreadPoolExecutor) -> None:
        with self._lifecycle_lock:
            self._executors.discard(executor)
            closing = self._closed.is_set()
        if not closing:
            executor.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        """Close all resources owned by a composed service, idempotently."""
        with self._lifecycle_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            executors = tuple(self._executors)
            heartbeats = tuple(self._heartbeats)
        for stop, _thread in heartbeats:
            stop.set()
        if self.transaction_coordinator is not None:
            self.transaction_coordinator.close()
        for executor in executors:
            executor.shutdown(wait=True, cancel_futures=True)
        for _stop, thread in heartbeats:
            if thread is not threading.current_thread():
                thread.join()
        if self.transaction_publication is not None:
            self.transaction_publication.close()
        if self.legacy_reader is not None:
            close = getattr(getattr(self.legacy_reader, "session_root", None), "close", None)
            if close is not None:
                close()
        close_repository = getattr(self.repository, "close", None)
        if close_repository is not None:
            close_repository()

    def __enter__(self) -> "ProposalService":
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _require_active(self) -> None:
        self._ensure_open()
        if not self.proposal_capability_active:
            raise RepositoryError("Proposal Review capability is unavailable",
                                  code="capability_unavailable")

    def _admit_create_rate(self, now: str) -> None:
        self.repository.admit_create(now)

    def _run_preview_job(self, work, *, lease_id: str, deadline: float):
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                         thread_name_prefix="proposal-preview")
        self._register_executor(executor)
        heartbeat_stop = threading.Event()
        def heartbeat() -> None:
            while not heartbeat_stop.wait(PREVIEW_HEARTBEAT_INTERVAL_SECONDS):
                backoff = .05
                while not heartbeat_stop.is_set():
                    try:
                        if not self.repository.renew_preview_lease(lease_id):
                            return
                        break
                    except (ProposalReviewError, OSError):
                        # A transient lock/filesystem failure must not end live
                        # ownership. Expired leases remain fail-closed while
                        # this process identity is still verifiably alive.
                        if heartbeat_stop.wait(backoff):
                            return
                        backoff = min(1.0, backoff * 2)
        heartbeat_thread = threading.Thread(target=heartbeat,
            name="proposal-preview-heartbeat", daemon=True)
        with self._lifecycle_lock:
            if self._closed.is_set():
                rejected = True
            else:
                rejected = False
                self._heartbeats.add((heartbeat_stop, heartbeat_thread))
                heartbeat_thread.start()
        if rejected:
            self._executor_done(executor)
            self.repository.release_preview_lease(lease_id)
            raise RepositoryError("Proposal Review service is closed",
                                  code="capability_unavailable")
        try:
            future = executor.submit(work)
        except BaseException:
            heartbeat_stop.set(); heartbeat_thread.join(timeout=1)
            with self._lifecycle_lock:
                self._heartbeats.discard((heartbeat_stop, heartbeat_thread))
            self._executor_done(executor)
            self.repository.release_preview_lease(lease_id)
            raise

        def release_when_worker_exits(completed) -> None:
            # This callback runs only after a running worker has actually
            # exited (or a queued future was successfully cancelled). The
            # repository lease therefore remains hard capacity authority after
            # the caller has received preview_timeout.
            try:
                heartbeat_stop.set(); heartbeat_thread.join(timeout=1)
                self.repository.release_preview_lease(lease_id)
            finally:
                with self._lifecycle_lock:
                    self._heartbeats.discard((heartbeat_stop, heartbeat_thread))
                self._executor_done(executor)
        future.add_done_callback(release_when_worker_exits)
        try:
            remaining = max(0.0, deadline - time.monotonic())
            return future.result(timeout=remaining)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RepositoryError("preview worker deadline exceeded", code="preview_timeout") from exc

    def _run_read_job(self, work, *, timeout_seconds: float):
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                         thread_name_prefix="proposal-read")
        self._register_executor(executor)
        try:
            future = executor.submit(work)
        except BaseException:
            self._executor_done(executor)
            raise
        future.add_done_callback(lambda _completed: self._executor_done(executor))
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RepositoryError("proposal operation deadline exceeded",
                                  code="operation_timeout") from exc

    def _admit_intent_with_deadline(self, intent, precondition, deadline: float, *,
                                    ambiguous_on_timeout: bool = False):
        remaining = max(0.0, deadline - time.monotonic())
        if remaining == 0:
            raise RepositoryError("service mutation deadline exceeded before intent admission",
                                  code="operation_timeout")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                         thread_name_prefix="proposal-mutation")
        self._register_executor(executor)
        try:
            future = executor.submit(self.repository.begin_service_intent, intent,
                                     precondition, deadline)
        except BaseException:
            self._executor_done(executor)
            raise
        future.add_done_callback(lambda _completed: self._executor_done(executor))
        try:
            return future.result(timeout=remaining)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            observed, retained = self.repository.observe_service_intent_nonblocking(
                intent.scope, intent.key_digest, intent.request_fingerprint)
            if ambiguous_on_timeout:
                code = "indeterminate_outcome"
            else:
                code = ("operation_timeout" if observed and retained is None
                        else "service_mutation_in_progress")
            raise RepositoryError("service mutation deadline elapsed; repository recovery owns any admitted intent",
                                  code=code) from exc

    def _service_intent(self, *, operation: str, scope: str, key_digest: str,
                        fingerprint: str, proposal_id: str, revision: int,
                        op_id: str | None, base_revision: int | None,
                        repository_snapshot_token: str,
                        accepted_token: str | None, record_id: str, result_id: str,
                        outbox_id: str, now: str, expires_at: str, domain_at: str,
                        aggregate: str, kind: str, row: Any,
                        notification: Mapping[str, Any], response: Mapping[str, Any],
                        status: int, preview: Mapping[str, Any] | None,
                        intent_id: str) -> schemas.ServiceMutationIntent:
        receipt = _outbox(outbox_id, notification, now=now, intent_id=intent_id,
                          result_record_id=result_id)
        domain_row = row.to_mapping()
        terminal_envelope = SuccessEnvelope(response).to_mapping()
        body = {"schema": 1, "intent_id": intent_id, "operation": operation,
            "scope": scope, "key_digest": key_digest, "request_fingerprint": fingerprint,
            "repository_id": self.repository.repository_id, "proposal_id": proposal_id,
            "proposal_revision": revision, "op_id": op_id,
            "base_proposal_revision": base_revision,
            "accepted_repository_snapshot_token": repository_snapshot_token,
            "accepted_read_model_token": accepted_token,
            "assigned": {"record_id": record_id, "result_record_id": result_id,
                         "outbox_id": outbox_id, "created_at": now,
                         "domain_at": domain_at, "expires_at": expires_at},
            "domain": {"aggregate": aggregate, "kind": kind, "row": domain_row,
                       "row_digest": canonical_digest(domain_row)},
            "notification_payload": dict(notification), "outbox": receipt.to_mapping(),
            "terminal_http_status": status, "terminal_response": terminal_envelope,
            "terminal_response_digest": canonical_digest(terminal_envelope), "preview": preview,
            "phase": "prepared", "recovery_attempts": 0, "created_at": now,
            "updated_at": now}
        body["checksum"] = compute_checksum(body)
        return schemas.ServiceMutationIntent.from_mapping(body)

    # ------------------------------- common frozen views
    def _capture(self, now: str) -> CapturedServiceBundle:
        captured = self.capture_port.capture(
            captured_at=now, proposal_capability_active=self.proposal_capability_active)
        caps = tuple(captured.snapshot.capabilities)
        if (PROPOSAL_CAPABILITY in caps) != self.proposal_capability_active:
            raise RepositoryError("capture capability does not match explicit service activation",
                                  code="contract_parity_failure")
        expected = self.runtime_identities
        if (captured.snapshot.validator_id != expected.validator_id
                or captured.snapshot.serializer_id != expected.serializer_id
                or captured.snapshot.operation_runtime_id != expected.operation_runtime_id):
            raise RepositoryError("capture runtime identities differ from service dependencies",
                                  code="contract_parity_failure")
        return captured

    @staticmethod
    def _records(snapshot: ProposalRepositorySnapshot, aggregate: str, kind: str, parser):
        return tuple(parser.from_mapping(w.payload) for w in snapshot.aggregate(aggregate)
                     if w.kind == kind)

    def _model(self, proposal: schemas.ProposalRevision, repo: ProposalRepositorySnapshot,
               captured: CapturedServiceBundle, now: str, *,
               extra_decision: schemas.DecisionRow | None = None,
               extra_withdrawal: schemas.Withdrawal | None = None,
               preview_override: schemas.Preview | None = None,
               extra_service_authority: Mapping[str, Any] | None = None) -> ProposalReadModel:
        decisions = list(self._records(repo, "decisions", "decision", schemas.DecisionRow))
        if extra_decision is not None:
            decisions.append(extra_decision)
        withdrawals = [row for row in self._records(repo, "proposals", "withdrawal", schemas.Withdrawal)
                       if row.proposal_id == proposal.proposal_id
                       and row.proposal_revision == proposal.proposal_revision]
        if extra_withdrawal is not None:
            withdrawals.append(extra_withdrawal)
        proposals = self._records(repo, "proposals", "proposal", schemas.ProposalRevision)
        superseded = any(p.proposal_id == proposal.proposal_id
                         and p.proposal_revision > proposal.proposal_revision for p in proposals)
        audits = [a for a in self._records(repo, "transactions", "transaction", schemas.TransactionAudit)
                  if a.proposal_id == proposal.proposal_id and a.proposal_revision == proposal.proposal_revision]
        applied = any(a.operation == "apply" and a.status == "applied" for a in audits)
        service_authority = [
            {"intent_id": i.intent_id, "result_record_id": i.assigned["result_record_id"],
             "record_id": i.assigned["record_id"], "operation": i.operation}
            for i in repo.service_intents if i.proposal_id == proposal.proposal_id
            and i.proposal_revision == proposal.proposal_revision]
        if extra_service_authority is not None:
            service_authority.append(dict(extra_service_authority))
        service_authority.sort(key=lambda value: value["intent_id"])
        preview = preview_override or self.repository.get_preview(proposal.preview_ref.preview_digest)
        return build_read_model(ReadModelInputs(
            proposal=proposal, current_bundle=captured.current_bundle, now=now,
            decision_rows=tuple(decisions), preview=preview,
            withdrawal=withdrawals[-1] if withdrawals else None,
            transaction_records=tuple(a.to_mapping() for a in audits),
            applied=applied, superseded=superseded,
            service_authority=tuple(service_authority)))

    def _find(self, repo: ProposalRepositorySnapshot, proposal_id: str,
              revision: int) -> schemas.ProposalRevision:
        matches = [p for p in self._records(repo, "proposals", "proposal", schemas.ProposalRevision)
                   if p.proposal_id == proposal_id and p.proposal_revision == revision]
        if len(matches) != 1:
            raise RepositoryError("proposal revision was not found", code="proposal_not_found")
        return matches[0]

    def _list_item(self, proposal: schemas.ProposalRevision,
                   repo: ProposalRepositorySnapshot, captured: CapturedServiceBundle,
                   now: str) -> pagination.ProposalListItem:
        """Project bounded list metadata without opening preview artifacts."""
        rows = self._records(repo, "decisions", "decision", schemas.DecisionRow)
        latest = projectors.latest_decision_rows(rows, proposal.proposal_id,
                                                 proposal.proposal_revision)
        decision_map = projectors.explicit_decision_map(latest)
        local = {op.op_id: projectors.derive_local_validity(op, proposal.expected_snapshot)
                 for op in proposal.operations}
        conflicts = projectors.project_conflicts(proposal, captured.current_bundle,
                                                 now=now, latest_decisions=latest)
        states = projectors.project_operations(
            proposal.operations, local_validities=local, explicit_decisions=decision_map,
            proposal_conflicts=conflicts.proposal_conflicts,
            op_conflicts=conflicts.op_conflicts)
        apply_set = projectors.accepted_apply_set(states)
        ready = (bool(apply_set) and not conflicts.has_conflict
                 and all(decision_map.get(op.op_id) in {"accepted", "rejected"}
                         for op in proposal.operations)
                 and all(state.local_validity == "valid" for state in states.values()))
        proposals = self._records(repo, "proposals", "proposal", schemas.ProposalRevision)
        withdrawals = self._records(repo, "proposals", "withdrawal", schemas.Withdrawal)
        audits = self._records(repo, "transactions", "transaction", schemas.TransactionAudit)
        superseded = any(p.proposal_id == proposal.proposal_id and
                         p.proposal_revision > proposal.proposal_revision for p in proposals)
        withdrawn = any(w.proposal_id == proposal.proposal_id and
                        w.proposal_revision == proposal.proposal_revision for w in withdrawals)
        applied = any(a.proposal_id == proposal.proposal_id and
                      a.proposal_revision == proposal.proposal_revision and
                      a.operation == "apply" and a.status == "applied" for a in audits)
        state = projectors.proposal_lifecycle_state(
            applied=applied, withdrawn=withdrawn, superseded=superseded,
            expired=projectors.is_expired(proposal.expires_at, now),
            has_conflict=conflicts.has_conflict, ready=ready)
        service_authority = tuple(sorted((
            {"intent_id": i.intent_id, "result_record_id": i.assigned["result_record_id"],
             "record_id": i.assigned["record_id"], "operation": i.operation}
            for i in repo.service_intents if i.proposal_id == proposal.proposal_id
            and i.proposal_revision == proposal.proposal_revision), key=lambda value: value["intent_id"]))
        bundle = captured.current_bundle
        token = projectors.compute_read_model_token(projectors.ReadModelTokenInputs(
            proposal_checksum=canonical_digest(proposal.to_mapping()),
            decision_records=tuple(latest[op_id].to_mapping() for op_id in sorted(latest)),
            transaction_records=tuple(a.to_mapping() for a in audits if
                a.proposal_id == proposal.proposal_id and
                a.proposal_revision == proposal.proposal_revision), withdrawal_records=tuple(
                w.to_mapping() for w in withdrawals if w.proposal_id == proposal.proposal_id
                and w.proposal_revision == proposal.proposal_revision),
            service_authority=service_authority, preview_digest=proposal.preview_ref.preview_digest,
            conflict_inputs_digest=projectors.conflict_inputs_digest(
                bundle_snapshot_digest=bundle.snapshot_digest, config_digest=bundle.config_digest,
                validation_context_digest=bundle.validation_context_digest,
                capabilities=bundle.capabilities, concept_revisions=bundle.concept_revisions,
                validator_id=bundle.validator_id, serializer_id=bundle.serializer_id,
                operation_runtime_id=bundle.operation_runtime_id),
            expires_at=proposal.expires_at,
            bundle_snapshot_digest=bundle.snapshot_digest, config_digest=bundle.config_digest,
            validation_context_digest=bundle.validation_context_digest,
            capabilities=bundle.capabilities, validator_id=bundle.validator_id,
            serializer_id=bundle.serializer_id, operation_runtime_id=bundle.operation_runtime_id))
        return pagination.ProposalListItem(
            proposal.created_at, proposal.proposal_id, proposal.proposal_revision,
            state, proposal.expires_at, token, proposal.summary, proposal.author)

    # ------------------------------- create / supersede
    def create(self, request: CreateProposalInput | Mapping[str, Any] | bytes | str, *,
               idempotency_key: str) -> SuccessEnvelope:
        self._require_active()
        value = request if isinstance(request, CreateProposalInput) else CreateProposalInput.from_mapping(request)
        now = self.clock.now()
        scope = CREATE_SCOPE
        key_digest = _key_digest(idempotency_key)
        fingerprint = canonical_digest({
            "protocol": 1, "scope": scope, "repository_id": self.repository.repository_id,
            "request": value.fingerprint_mapping(),
            "runtime": {"validator_id": self.runtime_identities.validator_id,
                        "serializer_id": self.runtime_identities.serializer_id,
                        "operation_runtime_id": self.runtime_identities.operation_runtime_id,
                        "proposal_capability_active": self.proposal_capability_active},
            "base": [value.proposal_id, value.supersedes_revision],
        })
        replay = self.repository.reconcile_endpoint(scope, key_digest, fingerprint)
        if replay.state == "terminal_result" and replay.endpoint_result is not None:
            if replay.endpoint_result.state == "indeterminate":
                raise RepositoryError("endpoint outcome is indeterminate", code="indeterminate_outcome")
            return _stored_success(replay.endpoint_result.response or {})
        if replay.state == "terminal_compacted":
            retained = self.repository.reconcile_service_intent(scope, key_digest, fingerprint)
            if retained is not None:
                return _stored_success(retained.terminal_response)
        retained = self.repository.reconcile_service_intent(scope, key_digest, fingerprint)
        if retained is not None:
            return _stored_success(retained.terminal_response)
        if replay.state in {"indeterminate", "terminal_compacted"}:
            raise RepositoryError("endpoint outcome is indeterminate", code="indeterminate_outcome")
        if replay.state not in {"new"}:
            raise RepositoryError("service mutation is recovering", code="indeterminate_outcome")
        legacy_projection = None
        if value.legacy_identity is not None:
            if self.legacy_reader is None:
                raise RepositoryError("legacy suggestion reader is unavailable",
                                      code="legacy_identity_conflict")
            legacy_projection = self.legacy_reader.verify(value.legacy_identity)
        self._admit_create_rate(now)
        preview_expires_at = _plus_seconds(now, 30 * 24 * 60 * 60)
        replay_expires_at = _plus_seconds(now, 7 * 24 * 60 * 60)
        intent_id = self.repository.new_id("svc_")
        proposal_id = value.proposal_id or self.repository.new_id("")
        revision = 1 if value.proposal_id is None else int(value.supersedes_revision) + 1
        record_id = self.repository.new_id("rec_")
        result_id = self.repository.new_id("rec_")
        outbox_id = self.repository.new_id("out_")
        def preview_work():
            captured_value = self._capture(now)
            if captured_value.snapshot.snapshot_digest != value.expected_snapshot.snapshot_digest:
                raise RepositoryError("current bundle differs from caller expected snapshot",
                                      code="snapshot_changed")
            if value.proposal_id is not None and value.supersedes_revision is not None:
                lineage_view = self.repository.snapshot()
                base = self._find(lineage_view, value.proposal_id, value.supersedes_revision)
                base_model = self._model(base, lineage_view, captured_value, now)
                projectors.assert_mutation_allowed(base_model.state, "supersede")
            staged_value = stage_operations(captured_value.snapshot,
                                            captured_value.concept_bytes,
                                            value.operations, codec=captured_value.codec)
            preview_value, artifact_value = build_preview(
                proposal_id=proposal_id, proposal_revision=revision,
                snapshot=captured_value.snapshot, operations=value.operations,
                staged=staged_value, complete_before=captured_value.concept_bytes,
                validator=captured_value.validator, generated_at=now,
                expires_at=preview_expires_at)
            return captured_value, preview_value, artifact_value
        preview_deadline = time.monotonic() + 60.0
        lease_id = self.repository.acquire_preview_lease(
            now, timeout_seconds=max(0.001, preview_deadline - time.monotonic()))
        captured, preview, artifact = self._run_preview_job(
            preview_work, lease_id=lease_id, deadline=preview_deadline)
        staged_ref = self.repository.stage_service_preview(intent_id, preview)
        affected = sorted({concept for op in value.operations for concept in (*op.reads, *op.writes)})
        provenance = list(value.provenance)
        if value.legacy_identity is not None:
            if len(provenance) >= schemas.MAX_PROVENANCE_ENTRIES:
                raise SchemaError("legacy provenance exceeds the bounded provenance array",
                                  path="create.provenance")
            provenance.append(schemas.ProvenanceEntry.from_mapping({
                "kind": "external",
                "value": dumps_canonical({
                    "legacy_identity": value.legacy_identity.to_mapping()
                }).decode("utf-8"),
            }, path="create.legacy_identity"))
        proposal_mapping = {
            "schema": 1, "record_id": record_id,
            "creation_result_record_id": result_id,
            "creation_request_fingerprint": fingerprint,
            "proposal_id": proposal_id, "proposal_revision": revision,
            "supersedes_revision": value.supersedes_revision, "mode": "proposal",
            "author": value.author, "created_at": now, "summary": value.summary,
            "rationale": value.rationale, "affected_concepts": affected,
            "expected_snapshot": captured.snapshot.to_mapping(),
            "operations": [op.to_mapping() for op in value.operations],
            "preview_ref": {"schema": 1, "preview_digest": preview.preview_digest,
                            "artifact_bytes": len(artifact)},
            "provenance": [p.to_mapping() for p in provenance],
            "expires_at": preview_expires_at, "extensions": dict(value.extensions),
        }
        before = self.repository.snapshot()
        proposal = schemas.ProposalRevision.from_mapping(proposal_mapping)
        event_name = "created" if value.proposal_id is None else "superseded"
        model = self._model(proposal, before, captured, now,
                            preview_override=preview,
                            extra_service_authority={"intent_id": intent_id,
                                "result_record_id": result_id, "record_id": record_id,
                                "operation": "create" if value.proposal_id is None else "supersede"})
        notification = {"schema": 1, "proposal_id": proposal.proposal_id,
                        "proposal_revision": proposal.proposal_revision, "event": event_name,
                        "read_model_token": model.read_model_token, "transaction_id": None}
        response = model.to_mapping()
        intent = self._service_intent(
            operation="create" if value.proposal_id is None else "supersede",
            scope=scope, key_digest=key_digest, fingerprint=fingerprint,
            proposal_id=proposal_id, revision=revision, op_id=None,
            base_revision=value.supersedes_revision, repository_snapshot_token=before.token,
            accepted_token=None,
            record_id=record_id, result_id=result_id, outbox_id=outbox_id,
            now=now, expires_at=replay_expires_at, domain_at=now,
            aggregate="proposals", kind="proposal", row=proposal,
            notification=notification, response=response, status=201,
            preview={"staged_ref": dict(staged_ref), "promoted_ref": proposal.preview_ref.to_mapping()},
            intent_id=intent_id)
        if value.legacy_identity is not None:
            current_legacy = self.legacy_reader.verify(value.legacy_identity)  # type: ignore[union-attr]
            if current_legacy != legacy_projection:
                raise RepositoryError("legacy suggestion changed before intent admission",
                                      code="legacy_identity_conflict")
        completed = self.repository.begin_service_intent(intent)
        return _stored_success(completed.terminal_response)

    # ------------------------------- reads
    def show(self, proposal_id: str, revision: int) -> SuccessEnvelope:
        self._require_active()
        def work():
            repo = self.repository.snapshot()
            proposal = self._find(repo, proposal_id, revision)
            now = self.clock.now(); captured = self._capture(now)
            return SuccessEnvelope(self._model(proposal, repo, captured, now).to_mapping())
        return self._run_read_job(work, timeout_seconds=10.0)

    def list(self, query: StrictListQuery | Mapping[str, Any]) -> SuccessEnvelope:
        self._require_active()
        query = query if isinstance(query, StrictListQuery) else StrictListQuery.from_mapping(query)
        return self._run_read_job(lambda: self._list_impl(query), timeout_seconds=10.0)

    def _list_impl(self, query: StrictListQuery) -> SuccessEnvelope:
        now = self.clock.now()
        signer = self.repository.cursor_signer()
        if query.cursor is not None:
            payload = pagination.decode_cursor(
                query.cursor, signer=signer, repository_id=self.repository.repository_id,
                now=now, expected_query_digest=pagination.ListQuery(query.state).digest)
            repo = self.repository.snapshot_at(payload.repository_high_waters)
            clock_captured_at = payload.clock_captured_at
        else:
            repo = self.repository.snapshot()
            clock_captured_at = now
        captured = self._capture(now)
        proposal_rows = repo.aggregate("proposals")
        max_proposal_rows = (self.repository.limits.lineages_per_session *
                             (self.repository.limits.revisions_per_lineage + 1))
        if len(proposal_rows) > max_proposal_rows:
            raise RepositoryError("proposal metadata exceeds bounded list authority",
                                  code="storage_corrupt")
        if (len(repo.aggregate("decisions")) > self.repository.limits.audit_rows
                or len(repo.aggregate("transactions")) > self.repository.limits.audit_rows):
            raise RepositoryError("proposal metadata aggregate exceeds bounded scan limit",
                                  code="storage_corrupt")
        proposals = self._records(repo, "proposals", "proposal", schemas.ProposalRevision)
        items = [self._list_item(proposal, repo, captured, clock_captured_at)
                 for proposal in proposals]
        view = pagination.FrozenPaginationView(
            repository_id=repo.repository_id, items=tuple(items),
            high_waters={a: {"generation": repo.high_waters[a].generation,
                             "sequence": repo.high_waters[a].sequence} for a in AGGREGATES},
            available_generations=self.repository.available_generations(),
            bundle_snapshot_digest=captured.current_bundle.snapshot_digest,
            config_digest=captured.current_bundle.config_digest,
            validation_context_digest=captured.current_bundle.validation_context_digest,
            capabilities=captured.current_bundle.capabilities,
            validator_id=captured.current_bundle.validator_id,
            serializer_id=captured.current_bundle.serializer_id,
            operation_runtime_id=captured.current_bundle.operation_runtime_id,
            clock_captured_at=clock_captured_at)
        page = pagination.walk_page(view, query=pagination.ListQuery(query.state),
                                    limit=query.limit, signer=signer, now=now,
                                    cursor=query.cursor)
        if len(page.canonical_bytes() if hasattr(page, "canonical_bytes")
               else dumps_canonical(page.to_mapping())) > 40 * 1024 * 1024:
            raise RepositoryError("list page exceeds 40 MiB", code="read_model_too_large")
        return SuccessEnvelope(page.to_mapping())

    # ------------------------------- decision / withdrawal
    def decide(self, proposal_id: str, revision: int, op_id: str,
               request: DecisionInput | Mapping[str, Any], *, actor: str = "local-user",
               idempotency_key: str | None = None) -> SuccessEnvelope:
        self._require_active()
        deadline = time.monotonic() + 15.0
        value = request if isinstance(request, DecisionInput) else DecisionInput.from_mapping(request)
        from .ids import validate_actor, validate_op_id
        actor = validate_actor(actor); validate_op_id(op_id)
        now = self.clock.now()
        key = idempotency_key or ("internal-" + self.repository.new_id("req_"))
        kd = _key_digest(key)
        scope = f"/__proposal/v1/proposals/{proposal_id}/revisions/{revision}/decisions/{op_id}"
        fingerprint = canonical_digest({"protocol": 1, "scope": scope,
            "repository_id": self.repository.repository_id,
            "proposal_id": proposal_id, "proposal_revision": revision, "op_id": op_id,
            "runtime": {"validator_id": self.runtime_identities.validator_id,
                        "serializer_id": self.runtime_identities.serializer_id,
                        "operation_runtime_id": self.runtime_identities.operation_runtime_id},
            "request": {"schema": 1, "decision": value.decision, "reason": value.reason,
                        "previous_row_token": value.previous_row_token}})
        replay = self.repository.reconcile_endpoint(scope, kd, fingerprint)
        if replay.state == "terminal_result" and replay.endpoint_result:
            if replay.endpoint_result.state == "indeterminate":
                raise RepositoryError("endpoint outcome is indeterminate", code="indeterminate_outcome")
            return _stored_success(replay.endpoint_result.response or {})
        if replay.state == "terminal_compacted":
            retained = self.repository.reconcile_service_intent(scope, kd, fingerprint)
            if retained is not None:
                return _stored_success(retained.terminal_response)
        retained = self.repository.reconcile_service_intent(scope, kd, fingerprint)
        if retained is not None:
            return _stored_success(retained.terminal_response)
        repo = self.repository.snapshot(); proposal = self._find(repo, proposal_id, revision)
        captured = self._capture(now); current_model = self._model(proposal, repo, captured, now)
        if current_model.read_model_token != value.read_model_token:
            raise StaleReadModelError()
        projectors.assert_mutation_allowed(current_model.state, "decide")
        op = next((candidate for candidate in proposal.operations if candidate.op_id == op_id), None)
        if op is None:
            raise RepositoryError("operation was not found", code="proposal_not_found")
        latest = projectors.latest_decision_rows(
            self._records(repo, "decisions", "decision", schemas.DecisionRow), proposal_id, revision)
        current = latest.get(op_id)
        expected_previous = None if current is None else current.row_token
        if value.previous_row_token != expected_previous:
            raise RepositoryError("decision row token changed", code="decision_conflict")
        if value.decision == "rejected" and not value.reason:
            by_id = {candidate.op_id: candidate for candidate in proposal.operations}
            def depends_transitively(candidate_id: str, seen: set[str] | None = None) -> bool:
                seen = set() if seen is None else seen
                if candidate_id in seen:
                    return False
                direct = by_id[candidate_id].depends_on
                return op_id in direct or any(
                    depends_transitively(dep, seen | {candidate_id}) for dep in direct)
            if any(candidate.op_id != op_id and depends_transitively(candidate.op_id)
                   for candidate in proposal.operations):
                raise SchemaError("rejecting an operation with dependents requires a reason")
        replay_expires_at = _plus_seconds(now, (24 * 60 * 60) if idempotency_key is None else (7 * 24 * 60 * 60))
        intent_id = self.repository.new_id("svc_"); record_id = self.repository.new_id("rec_")
        result_id = self.repository.new_id("rec_"); outbox_id = self.repository.new_id("out_")
        row = schemas.DecisionRow.from_mapping({
            "schema": 1, "record_id": record_id,
            "decision_result_record_id": result_id,
            "decision_request_fingerprint": fingerprint, "proposal_id": proposal_id,
            "proposal_revision": revision, "op_id": op_id, "decision": value.decision,
            "reason": value.reason, "actor": actor, "decided_at": now,
            "previous_row_token": expected_previous, "row_token": self.repository.new_row_token()})
        model = self._model(proposal, repo, captured, now, extra_decision=row,
                            extra_service_authority={"intent_id": intent_id,
                                "result_record_id": result_id, "record_id": record_id,
                                "operation": "decision"})
        response = {"decision": row.to_mapping(), "decision_set_token": model.decision_set_token,
                    "read_model_token": model.read_model_token}
        notification = {"schema": 1, "proposal_id": proposal_id,
                        "proposal_revision": revision, "event": "decision_changed",
                        "read_model_token": model.read_model_token, "transaction_id": None}
        intent = self._service_intent(operation="decision", scope=scope, key_digest=kd,
            fingerprint=fingerprint, proposal_id=proposal_id, revision=revision, op_id=op_id,
            base_revision=revision, repository_snapshot_token=repo.token,
            accepted_token=value.read_model_token, record_id=record_id,
            result_id=result_id, outbox_id=outbox_id, now=now, expires_at=replay_expires_at,
            domain_at=now, aggregate="decisions", kind="decision", row=row,
            notification=notification, response=response, status=200, preview=None,
            intent_id=intent_id)
        def decision_precondition(current_repo: ProposalRepositorySnapshot) -> bool:
            current_proposal = self._find(current_repo, proposal_id, revision)
            return self._model(current_proposal, current_repo, captured, now).read_model_token == value.read_model_token
        return _stored_success(self._admit_intent_with_deadline(
            intent, decision_precondition, deadline,
            ambiguous_on_timeout=idempotency_key is None).terminal_response)

    def withdraw(self, proposal_id: str, revision: int,
                 request: WithdrawalInput | Mapping[str, Any], *, idempotency_key: str,
                 actor: str = "local-user") -> SuccessEnvelope:
        self._require_active()
        deadline = time.monotonic() + 15.0
        value = request if isinstance(request, WithdrawalInput) else WithdrawalInput.from_mapping(request)
        from .ids import validate_actor
        actor = validate_actor(actor); now = self.clock.now(); kd = _key_digest(idempotency_key)
        scope = f"/__proposal/v1/proposals/{proposal_id}/revisions/{revision}/withdraw"
        fingerprint = canonical_digest({"protocol": 1, "scope": scope,
            "repository_id": self.repository.repository_id,
            "proposal_id": proposal_id, "proposal_revision": revision,
            "runtime": {"validator_id": self.runtime_identities.validator_id,
                        "serializer_id": self.runtime_identities.serializer_id,
                        "operation_runtime_id": self.runtime_identities.operation_runtime_id},
            "request": {"schema": 1, "reason": value.reason}})
        replay = self.repository.reconcile_endpoint(scope, kd, fingerprint)
        if replay.state == "terminal_result" and replay.endpoint_result:
            if replay.endpoint_result.state == "indeterminate":
                raise RepositoryError("endpoint outcome is indeterminate", code="indeterminate_outcome")
            return _stored_success(replay.endpoint_result.response or {})
        if replay.state == "terminal_compacted":
            retained = self.repository.reconcile_service_intent(scope, kd, fingerprint)
            if retained is not None:
                return _stored_success(retained.terminal_response)
        retained = self.repository.reconcile_service_intent(scope, kd, fingerprint)
        if retained is not None:
            return _stored_success(retained.terminal_response)
        repo = self.repository.snapshot(); proposal = self._find(repo, proposal_id, revision)
        captured = self._capture(now); current = self._model(proposal, repo, captured, now)
        if current.read_model_token != value.read_model_token:
            raise StaleReadModelError()
        projectors.assert_mutation_allowed(current.state, "withdraw")
        intent_id = self.repository.new_id("svc_"); record_id = self.repository.new_id("rec_")
        result_id = self.repository.new_id("rec_"); outbox_id = self.repository.new_id("out_")
        expires_at = _plus_seconds(now, 7 * 24 * 60 * 60)
        withdrawal_body = {"schema": 1, "record_id": record_id,
            "withdrawal_result_record_id": result_id,
            "withdrawal_request_fingerprint": fingerprint, "proposal_id": proposal_id,
            "proposal_revision": revision, "actor": actor, "reason": value.reason,
            "read_model_token": value.read_model_token, "withdrawn_at": now}
        withdrawal_body["checksum"] = compute_checksum(withdrawal_body)
        withdrawal = schemas.Withdrawal.from_mapping(withdrawal_body)
        model = self._model(proposal, repo, captured, now, extra_withdrawal=withdrawal,
                            extra_service_authority={"intent_id": intent_id,
                                "result_record_id": result_id, "record_id": record_id,
                                "operation": "withdraw"})
        response = model.to_mapping()
        notification = {"schema": 1, "proposal_id": proposal_id,
                        "proposal_revision": revision, "event": "withdrawn",
                        "read_model_token": model.read_model_token, "transaction_id": None}
        intent = self._service_intent(operation="withdraw", scope=scope, key_digest=kd,
            fingerprint=fingerprint, proposal_id=proposal_id, revision=revision, op_id=None,
            base_revision=revision, repository_snapshot_token=repo.token,
            accepted_token=value.read_model_token, record_id=record_id,
            result_id=result_id, outbox_id=outbox_id, now=now, expires_at=expires_at,
            domain_at=now, aggregate="proposals", kind="withdrawal", row=withdrawal,
            notification=notification, response=response, status=200, preview=None,
            intent_id=intent_id)
        def withdrawal_precondition(current_repo: ProposalRepositorySnapshot) -> bool:
            current_proposal = self._find(current_repo, proposal_id, revision)
            return self._model(current_proposal, current_repo, captured, now).read_model_token == value.read_model_token
        return _stored_success(self._admit_intent_with_deadline(
            intent, withdrawal_precondition, deadline).terminal_response)

    # ------------------------------- content transactions
    def _transaction(self, operation: str, request: ApplyInput | UndoInput,
                     idempotency_key: str) -> SuccessEnvelope:
        """Expose the composed coordinator without weakening its authority.

        The service capability gate deliberately precedes coordinator lookup,
        request parsing, idempotency reconciliation, and repository access.
        """
        self._require_active()
        if self.transaction_coordinator is None:
            raise RepositoryError("proposal transaction service is unavailable",
                                  code="capability_unavailable")
        result = getattr(self.transaction_coordinator, operation)(request, idempotency_key)
        if isinstance(result, schemas.TransactionProgress):
            return SuccessEnvelope({
                "status": "transaction_in_progress",
                "attempt": result.to_mapping(),
                "read_model_token": request.read_model_token,
            })
        return SuccessEnvelope(result.to_mapping())

    def apply(self, request: ApplyInput | Mapping[str, Any] | bytes | str, *,
              idempotency_key: str) -> SuccessEnvelope:
        self._require_active()
        value = request if isinstance(request, ApplyInput) else ApplyInput.from_mapping(request)
        return self._transaction("apply", value, idempotency_key)

    def undo(self, request: UndoInput | Mapping[str, Any] | bytes | str, *,
             idempotency_key: str) -> SuccessEnvelope:
        self._require_active()
        value = request if isinstance(request, UndoInput) else UndoInput.from_mapping(request)
        return self._transaction("undo", value, idempotency_key)


__all__ = ["PROPOSAL_CAPABILITY", "CapturedServiceBundle", "BundleCapturePort",
           "ProposalService"]
