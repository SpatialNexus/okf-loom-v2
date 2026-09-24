"""Narrow caller-assigned, idempotent Proposal Review activity bridge.

The transaction audit chooses ``act_`` identities before finalization.  Studio
is only the append/verification store; it cannot silently substitute a new id.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .activity import validate_activity_candidate
from .canonical import canonical_digest
from .errors import RepositoryCorruptError
from .ids import validate_record_id


def _activity_id(value: str) -> str:
    return validate_record_id(value, "act_")


@dataclass(frozen=True)
class StudioActivityAdapter:
    studio: Any

    def append_activity(self, activity_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        activity_id = _activity_id(activity_id)
        body = dict(payload)
        body["id"] = activity_id
        validate_activity_candidate(body, expected_id=activity_id)
        digest = canonical_digest(body)
        matches = [row for row in self.studio.read_events(limit=100_000)
                   if row.get("id") == activity_id]
        if matches:
            if len(matches) != 1 or canonical_digest({k: v for k, v in matches[0].items()
                                                       if k not in {"event_id", "event_cursor", "seq", "ts", "rev"}}) != digest:
                raise RepositoryCorruptError("proposal activity identity has contradictory content")
            return matches[0]
        written = self.studio.events_append(body, publish=False)
        if written.get("id") != activity_id:
            raise RepositoryCorruptError("Studio replaced caller-assigned activity id")
        return written

    def verify_activity_ids(self, transaction_id: str, activity_ids: tuple[str, ...]) -> bool:
        rows = {row.get("id"): row for row in self.studio.read_events(limit=100_000)
                if row.get("type") == "activity"}
        for activity_id in activity_ids:
            _activity_id(activity_id)
            row = rows.get(activity_id)
            if row is None or row.get("detail", {}).get("transaction_id") != transaction_id:
                return False
        return bool(activity_ids)


__all__ = ["StudioActivityAdapter", "validate_activity_candidate"]
