"""OUTBOX-01..04 stored-payload authority proofs."""
from __future__ import annotations

import inspect
import multiprocessing
import os
from pathlib import Path

import pytest

from okf_loom.proposal_review import canonical, schemas
from okf_loom.proposal_review.errors import SchemaError
from okf_loom.proposal_review.outbox import OutboxReconciler
from okf_loom.proposal_review.repository import ProposalRepositoryV1
from okf_loom.proposal_review.ports import SystemEntropy
from test_proposal_service import Clock
from test_proposal_service_recovery import _adapter, _base_for_mutation

U = "01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
D = "sha256:" + "0" * 64
TS = "2026-07-14T12:34:56.789Z"


def _receipt():
    payload = {"schema": 1, "proposal_id": U, "proposal_revision": 2,
               "event": "superseded", "read_model_token": D, "transaction_id": None}
    body = {"schema": 1, "outbox_id": "out_" + U, "aggregate_kind": "proposal",
            "aggregate_id": U, "proposal_id": U, "proposal_revision": 2,
            "event_type": "superseded", "mutation_intent_id": "svc_" + U,
            "result_record_id": "rec_" + U, "payload": payload,
            "payload_digest": canonical.canonical_digest(payload), "state": "pending",
            "event_cursor": None, "attempts": 0, "created_at": TS, "updated_at": TS}
    body["checksum"] = canonical.compute_checksum(body)
    return body


def test_outbox_01_stored_payload(tmp_path) -> None:
    row = schemas.OutboxReceipt.from_mapping(_receipt())
    assert row.payload_digest == canonical.canonical_digest(row.payload)
    bad = _receipt(); bad["payload"] = {**bad["payload"], "proposal_revision": 1}
    bad["checksum"] = canonical.compute_checksum(bad)
    with pytest.raises(SchemaError):
        schemas.OutboxReceipt.from_mapping(bad)
    root = tmp_path / "real-payload"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    _base_for_mutation(root)
    repository = ProposalRepositoryV1.bootstrap(
        _adapter(root), clock=Clock(), entropy=SystemEntropy())
    receipt = repository.pending_outbox()[0]
    assert receipt.payload_digest == canonical.canonical_digest(receipt.payload)
    intent = repository.snapshot().service_intents[0]
    assert receipt.payload == intent.notification_payload == intent.outbox.payload


def test_outbox_02_authority_binding(tmp_path) -> None:
    row = schemas.OutboxReceipt.from_mapping(_receipt())
    assert (row.proposal_revision, row.mutation_intent_id, row.result_record_id) == (
        2, "svc_" + U, "rec_" + U)
    root = tmp_path / "real-binding"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    _base_for_mutation(root)
    repository = ProposalRepositoryV1.bootstrap(
        _adapter(root), clock=Clock(), entropy=SystemEntropy())
    receipt = repository.pending_outbox()[0]
    intent = repository.snapshot().service_intents[0]
    assert receipt.mutation_intent_id == intent.intent_id
    assert receipt.result_record_id == intent.assigned["result_record_id"]
    assert receipt.proposal_revision == intent.proposal_revision


def test_outbox_03_no_reconstruction() -> None:
    assert tuple(inspect.signature(OutboxReconciler).parameters) == ("repository", "appender")
    source = inspect.getsource(OutboxReconciler.reconcile_pending)
    for forbidden in ("projector", "read_model", "resolve", "current_state", "snapshot"):
        assert forbidden not in source
    assert "receipt.payload" in source and "payload_digest" in source


def test_outbox_batch_limit_is_exact_1_to_100() -> None:
    class Repo:
        def pending_outbox(self): return ()
    class Appender:
        def append_proposal_event(self, outbox_id, payload): return 1
    reconciler = OutboxReconciler(Repo(), Appender())
    assert reconciler.reconcile_pending(limit=100) == 0
    with pytest.raises(ValueError): reconciler.reconcile_pending(limit=0)
    with pytest.raises(ValueError): reconciler.reconcile_pending(limit=101)


class _ProcessEventAppender:
    def __init__(self, path: str, crash: bool): self.path, self.crash = Path(path), crash
    def append_proposal_event(self, outbox_id, payload):
        if self.path.exists():
            stored = canonical.loads_canonical(self.path.read_bytes())
            assert stored["outbox_id"] == outbox_id and stored["payload"] == dict(payload)
            return 1
        self.path.write_bytes(canonical.dumps_canonical(
            {"outbox_id": outbox_id, "payload": dict(payload)}))
        if self.crash: os._exit(79)
        return 1


def _outbox_process(root: str, event_path: str, crash: bool) -> None:
    repository = ProposalRepositoryV1.bootstrap(_adapter(Path(root)), clock=Clock(), entropy=SystemEntropy())
    OutboxReconciler(repository, _ProcessEventAppender(event_path, crash)).reconcile_pending(limit=100)


def test_outbox_04_append_before_mark(tmp_path) -> None:
    root = tmp_path / "outbox-process"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    _base_for_mutation(root); event_path = str(tmp_path / "event.json")
    context = multiprocessing.get_context("spawn")
    crashed = context.Process(target=_outbox_process, args=(str(root), event_path, True))
    crashed.start(); crashed.join(20); assert crashed.exitcode == 79
    recovered = context.Process(target=_outbox_process, args=(str(root), event_path, False))
    recovered.start(); recovered.join(20); assert recovered.exitcode == 0
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    assert repository.pending_outbox() == ()
