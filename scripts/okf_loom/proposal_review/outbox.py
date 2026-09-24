"""At-least-once Proposal Review notification outbox reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from . import ids, schemas
from .canonical import canonical_digest
from .errors import RepositoryCorruptError


@runtime_checkable
class EventAppender(Protocol):
    """Existing event-store seam; append is idempotent by ``outbox_id``."""

    def append_proposal_event(self, outbox_id: str, payload: Mapping[str, Any]) -> int: ...


def validate_notification(value: Mapping[str, Any]) -> dict[str, Any]:
    obj = dict(value)
    required = {"schema", "proposal_id", "proposal_revision", "event",
                "read_model_token", "transaction_id"}
    if set(obj) != required or obj["schema"] != 1:
        raise RepositoryCorruptError("outbox notification is not the closed schema")
    ids.validate_proposal_id(obj["proposal_id"])
    ids.validate_revision(obj["proposal_revision"], name="proposal_revision")
    if obj["event"] not in {"created", "superseded", "decision_changed", "withdrawn",
                            "conflict_changed", "applied", "undo_changed", "expired"}:
        raise RepositoryCorruptError("outbox notification has an unsupported event")
    ids.validate_digest(obj["read_model_token"], name="read_model_token")
    if obj["transaction_id"] is not None:
        ids.validate_transaction_id_any(obj["transaction_id"])
    return obj


@dataclass(frozen=True)
class OutboxReconciler:
    repository: Any
    appender: EventAppender

    def reconcile_pending(self, *, limit: int = 100) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("outbox reconciliation limit must be 1..100")
        completed = 0
        for receipt in self.repository.pending_outbox()[:limit]:
            payload = validate_notification(receipt.payload)
            if canonical_digest(payload) != receipt.payload_digest:
                raise RepositoryCorruptError("outbox notification payload digest mismatch")
            # A crash after this idempotent append and before receipt update may
            # call it again. Delivery is consequently at least once, never a
            # canonical-state transfer.
            cursor = self.appender.append_proposal_event(receipt.outbox_id, payload)
            self.repository.mark_outbox_appended(receipt.outbox_id, cursor)
            completed += 1
        return completed


__all__ = ["EventAppender", "OutboxReconciler",
           "validate_notification"]
