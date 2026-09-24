"""Outbox pending -> event -> receipt and crash-window proofs."""
from __future__ import annotations

from okf_loom.proposal_review import canonical, schemas
from okf_loom.proposal_review.outbox import OutboxReconciler

U = "01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
TS = "2026-07-14T12:34:56.789Z"
D = "sha256:" + "0" * 64


def receipt():
    event = {"schema": 1, "proposal_id": U, "proposal_revision": 1,
             "event": "created", "read_model_token": D, "transaction_id": None}
    body = {"schema": 1, "outbox_id": "out_" + U, "aggregate_kind": "proposal",
            "aggregate_id": U, "proposal_id": U, "proposal_revision": 1,
            "event_type": "created", "mutation_intent_id": "svc_" + U,
            "result_record_id": "rec_" + U, "payload": event,
            "payload_digest": canonical.canonical_digest(event), "state": "pending",
            "event_cursor": None, "attempts": 0, "created_at": TS, "updated_at": TS}
    body["checksum"] = canonical.compute_checksum(body)
    return schemas.OutboxReceipt.from_mapping(body), event


class Repo:
    def __init__(self, row): self.row = row
    def pending_outbox(self): return (self.row,) if self.row.state == "pending" else ()
    def mark_outbox_appended(self, outbox_id, cursor):
        body = self.row.to_mapping(); body.update(state="appended", event_cursor=cursor, attempts=1)
        body["checksum"] = canonical.compute_checksum(body)
        self.row = schemas.OutboxReceipt.from_mapping(body); return self.row


class Resolver:
    def __init__(self, event): self.event = event
    def notification_for(self, receipt): return self.event


class Appender:
    def __init__(self): self.by_id = {}; self.calls = 0
    def append_proposal_event(self, outbox_id, payload):
        self.calls += 1
        return self.by_id.setdefault(outbox_id, len(self.by_id) + 1)


def test_pending_event_receipt_and_duplicate_window_are_idempotent() -> None:
    row, event = receipt(); repository = Repo(row); appender = Appender()
    reconciler = OutboxReconciler(repository, appender)
    assert reconciler.reconcile_pending() == 1
    assert repository.row.state == "appended" and repository.row.event_cursor == 1
    assert reconciler.reconcile_pending() == 0
    # Simulate event append succeeding while receipt update was lost.
    repository.row = row
    assert reconciler.reconcile_pending() == 1
    assert appender.calls == 2 and repository.row.event_cursor == 1


def test_state_17_outbox_authority() -> None:
    row, event = receipt(); repository = Repo(row); appender = Appender()
    reconciler = OutboxReconciler(repository, appender)
    reconciler.reconcile_pending()
    repository.row = row  # crash after event append, before receipt advance
    reconciler.reconcile_pending()
    assert repository.row.state == "appended"
    assert repository.row.payload == event and repository.row.payload_digest == canonical.canonical_digest(event)
    assert len(appender.by_id) == 1
