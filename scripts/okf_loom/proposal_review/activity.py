"""Closed Proposal Review activity validation shared by domain and adapters."""
from __future__ import annotations

from typing import Any, Mapping

from . import ids
from .errors import SchemaError
from .ids import validate_record_id


def _activity_id(value: str) -> str:
    return validate_record_id(value, "act_")


def validate_activity_candidate(value: Mapping[str, Any], *, expected_id: str | None = None
                                ) -> dict[str, Any]:
    """Validate the one closed activity shape used by intents and adapters."""
    if not isinstance(value, Mapping):
        raise SchemaError("proposal activity candidate must be an object")
    body = dict(value)
    if set(body) != {"type", "id", "actor", "origin", "action", "ids", "summary",
                     "undoable", "detail"}:
        raise SchemaError("proposal activity candidate has non-closed fields")
    if body["type"] != "activity":
        raise SchemaError("proposal activity payload type must be activity")
    activity_id = _activity_id(body["id"])
    if expected_id is not None and activity_id != _activity_id(expected_id):
        raise SchemaError("proposal activity payload id mismatch")
    ids.validate_actor(body["actor"])
    if body["origin"] != "proposal" or body["action"] not in {"apply", "undo"}:
        raise SchemaError("invalid proposal activity origin/action")
    if not isinstance(body["ids"], list):
        raise SchemaError("proposal activity ids must be an array")
    concepts = [ids.validate_concept_id(item) for item in body["ids"]]
    if concepts != sorted(set(concepts)):
        raise SchemaError("proposal activity ids must be canonical unique")
    if not isinstance(body["summary"], str) or not body["summary"]:
        raise SchemaError("proposal activity summary required")
    if not isinstance(body["undoable"], bool):
        raise SchemaError("proposal activity undoable must be boolean")
    detail = body["detail"]
    if not isinstance(detail, Mapping):
        raise SchemaError("proposal activity detail must be an object")
    detail = dict(detail)
    if set(detail) != {"transaction_id", "proposal_id", "proposal_revision", "undo_group_id"}:
        raise SchemaError("proposal activity detail has non-closed fields")
    ids.validate_transaction_id_any(detail["transaction_id"])
    ids.validate_proposal_id(detail["proposal_id"])
    ids.validate_revision(detail["proposal_revision"])
    ids.validate_undo_id(detail["undo_group_id"])
    return body


__all__ = ["validate_activity_candidate"]
