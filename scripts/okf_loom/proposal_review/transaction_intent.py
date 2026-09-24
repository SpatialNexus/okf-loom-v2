"""Closed immutable authority for crash-safe transaction finalization."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import ids, schemas
from .canonical import canonical_digest, compute_checksum, dumps_canonical, loads_canonical
from .errors import IntegrityError, SchemaError
from .activity import validate_activity_candidate

FIELDS = frozenset({"schema", "transaction_id", "operation", "parent_transaction_id",
    "proposal_id", "proposal_revision", "actor", "endpoint", "accepted_authority",
    "accepted_op_ids", "decision_record_ids", "decision_rows_digest", "validation",
    "candidate_ids", "candidates", "undo_group_id", "timestamps", "retention",
    "history", "targets", "allowed_placeholders", "checksum"})
PLACEHOLDERS = frozenset({"target_before_revisions", "target_after_revisions",
    "target_before_identities", "target_resulting_identities", "target_backup_identities",
    "resulting_revisions", "concept_revisions",
    "transaction_intent_digest", "candidate_checksum"})


def _map(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping): raise SchemaError(f"{name} must be an object")
    return dict(value)


def _exact(value: Mapping[str, Any], fields: frozenset[str], name: str) -> None:
    if set(value) != fields: raise SchemaError(f"{name} has non-closed fields")


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SchemaError(f"{name} must be an array of strings")
    if value != sorted(set(value)): raise SchemaError(f"{name} must be sorted and unique")
    return tuple(value)


def _placeholders(value: Any, path: tuple[str, ...] = ()) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        if "$intent_placeholder" in value:
            if set(value) != {"$intent_placeholder"} or not isinstance(value["$intent_placeholder"], str):
                raise SchemaError("intent placeholder must be a whole single-member object")
            name = value["$intent_placeholder"]
            if name not in PLACEHOLDERS: raise SchemaError("unknown intent placeholder")
            parent = path[-1] if path else ""
            if name == "transaction_intent_digest":
                allowed = parent == name and "audit" in path
            elif name == "candidate_checksum":
                allowed = parent == "checksum" and any(part in {"audit", "outbox", "result"} for part in path)
            elif name == "target_resulting_identities":
                allowed = parent in {name, "resulting_identity"}
            else:
                allowed = parent == name
            if not allowed: raise SchemaError("intent placeholder position is forbidden")
            return [name]
        for key, child in value.items(): found.extend(_placeholders(child, path + (str(key),)))
    elif isinstance(value, list):
        for index, child in enumerate(value): found.extend(_placeholders(child, path + (str(index),)))
    return found


@dataclass(frozen=True)
class TransactionFinalizationIntent:
    value: dict[str, Any]

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any], *, verify_checksum: bool = True
                     ) -> "TransactionFinalizationIntent":
        obj = _map(loads_canonical(dumps_canonical(dict(source))), "intent")
        _exact(obj, FIELDS, "intent")
        if obj["schema"] != 1 or obj["operation"] not in {"apply", "undo"}:
            raise SchemaError("invalid transaction intent discriminator")
        operation = obj["operation"]
        transaction_id = ids.validate_transaction_id_any(obj["transaction_id"])
        if not transaction_id.startswith("txn_" if operation == "apply" else "undo_"):
            raise SchemaError("transaction intent id/operation mismatch")
        if operation == "apply":
            if obj["parent_transaction_id"] is not None: raise SchemaError("apply parent must be null")
        else: ids.validate_txn_id(obj["parent_transaction_id"])
        ids.validate_proposal_id(obj["proposal_id"]); ids.validate_revision(obj["proposal_revision"])
        ids.validate_actor(obj["actor"]); ids.validate_undo_id(obj["undo_group_id"])
        endpoint = _map(obj["endpoint"], "endpoint")
        _exact(endpoint, frozenset({"scope", "key_digest", "request_fingerprint", "result_record_id"}), "endpoint")
        if not isinstance(endpoint["scope"], str) or not endpoint["scope"]: raise SchemaError("endpoint scope required")
        ids.validate_digest(endpoint["key_digest"]); ids.validate_digest(endpoint["request_fingerprint"])
        ids.validate_rec_id(endpoint["result_record_id"])
        authority = _map(obj["accepted_authority"], "accepted authority")
        _exact(authority, frozenset({"read_model_token", "read_revisions", "snapshot_digest",
            "decision_set_token", "preview_digest", "validation_context_digest", "validator_id",
            "serializer_id", "operation_runtime_id"}), "accepted authority")
        for name in ("read_model_token", "snapshot_digest", "validation_context_digest"):
            ids.validate_digest(authority[name])
        for concept, digest in _map(authority["read_revisions"], "read revisions").items():
            ids.validate_concept_id(concept); ids.validate_digest(digest)
        for name in ("validator_id", "serializer_id", "operation_runtime_id"):
            ids.validate_runtime_identity(authority[name])
        for name in ("decision_set_token", "preview_digest"):
            if operation == "apply": ids.validate_digest(authority[name])
            elif authority[name] is not None: raise SchemaError(f"undo {name} must be null")
        accepted = _strings(obj["accepted_op_ids"], "accepted op ids")
        decisions = _strings(obj["decision_record_ids"], "decision record ids")
        for item in accepted: ids.validate_op_id(item)
        for item in decisions: ids.validate_rec_id(item)
        if operation == "apply":
            if not accepted: raise SchemaError("apply intent requires accepted operations")
            ids.validate_digest(obj["decision_rows_digest"])
        elif accepted or decisions or obj["decision_rows_digest"] is not None:
            raise SchemaError("undo decision authority must be empty/null")
        validation = _map(obj["validation"], "validation")
        _exact(validation, frozenset({"profile", "digest", "status"}), "validation")
        if not isinstance(validation["profile"], str) or validation["status"] != "pass":
            raise SchemaError("intent requires passing validation")
        ids.validate_digest(validation["digest"])
        ids_map = _map(obj["candidate_ids"], "candidate ids")
        candidates = _map(obj["candidates"], "candidates")
        _exact(ids_map, frozenset({"commit", "rollback"}), "candidate ids")
        _exact(candidates, frozenset({"commit", "rollback"}), "candidates")
        for branch in ("commit", "rollback"):
            assigned = _map(ids_map[branch], "candidate branch ids")
            _exact(assigned, frozenset({"audit_record_id", "activity_ids", "outbox_ids", "result_record_id"}), "candidate branch ids")
            ids.validate_rec_id(assigned["audit_record_id"]); ids.validate_rec_id(assigned["result_record_id"])
            if assigned["result_record_id"] != endpoint["result_record_id"]: raise SchemaError("candidate endpoint id mismatch")
            activity_ids = _strings(assigned["activity_ids"], "activity ids")
            outbox_ids = _strings(assigned["outbox_ids"], "outbox ids")
            for item in activity_ids: ids.validate_record_id(item, "act_")
            for item in outbox_ids: ids.validate_out_id(item)
            candidate = _map(candidates[branch], "candidate")
            _exact(candidate, frozenset({"audit", "activities", "outbox", "result"}), "candidate")
            if candidate["audit"].get("record_id") != assigned["audit_record_id"]: raise SchemaError("candidate audit id mismatch")
            if candidate["result"].get("record_id") != assigned["result_record_id"]: raise SchemaError("candidate result id mismatch")
            if not isinstance(candidate["activities"], list): raise SchemaError("candidate activities must be an array")
            for activity in candidate["activities"]:
                validate_activity_candidate(activity)
            if sorted(row.get("id") for row in candidate["activities"]) != list(activity_ids): raise SchemaError("candidate activity ids mismatch")
            if sorted(row.get("outbox_id") for row in candidate["outbox"]) != list(outbox_ids): raise SchemaError("candidate outbox ids mismatch")
        timestamps = _map(obj["timestamps"], "timestamps")
        _exact(timestamps, frozenset({"started_at", "commit_decided_at", "commit_completed_at",
            "rollback_completed_at", "undo_expires_at", "audit_expires_at", "replay_expires_at"}), "timestamps")
        for value in timestamps.values():
            if value is not None: ids.validate_timestamp(value)
        if obj["retention"] != {"audit_until": timestamps["audit_expires_at"],
                "replay_until": timestamps["replay_expires_at"], "undo_until": timestamps["undo_expires_at"]}:
            raise SchemaError("intent retention does not repeat timestamps")
        history = _map(obj["history"], "history")
        _exact(history, frozenset({"relative_path", "manifest_digest"}), "history")
        if history["relative_path"] != f"transactions/{transaction_id}/history.json": raise SchemaError("intent history path mismatch")
        ids.validate_digest(history["manifest_digest"])
        if not isinstance(obj["targets"], list) or not obj["targets"]: raise SchemaError("intent targets required")
        concepts = []
        for item in obj["targets"]:
            target = _map(item, "intent target")
            _exact(target, frozenset({"concept", "before_digest", "after_digest", "before_identity",
                "staged_identity", "resulting_identity", "backup_identity"}), "intent target")
            concepts.append(ids.validate_concept_id(target["concept"]))
            ids.validate_digest(target["before_digest"]); ids.validate_digest(target["after_digest"])
            before_identity = schemas.TargetIdentity.from_mapping(target["before_identity"])
            staged_identity = schemas.TargetIdentity.from_mapping(target["staged_identity"])
            if before_identity.digest != target["before_digest"] or staged_identity.digest != target["after_digest"]:
                raise SchemaError("intent target identity linkage mismatch")
            if target["resulting_identity"] != {"$intent_placeholder": "target_resulting_identities"}:
                raise SchemaError("intent resulting identity must be the normative placeholder")
            schemas.LeafIdentity.from_mapping(target["backup_identity"])
        if concepts != sorted(set(concepts)): raise SchemaError("intent targets must be canonical unique")
        declared = _strings(obj["allowed_placeholders"], "allowed placeholders")
        placeholder_graph = {"candidates": candidates, "targets": obj["targets"]}
        if declared != tuple(sorted(set(_placeholders(placeholder_graph)))):
            raise SchemaError("intent placeholder declaration mismatch")
        ids.validate_digest(obj["checksum"])
        if verify_checksum and obj["checksum"] != compute_checksum(obj): raise IntegrityError("intent checksum mismatch")
        return cls(obj)

    @classmethod
    def from_json(cls, data: bytes | str) -> "TransactionFinalizationIntent":
        value = loads_canonical(data)
        if not isinstance(value, Mapping): raise SchemaError("intent must be an object")
        return cls.from_mapping(value)

    def to_mapping(self) -> dict[str, Any]: return _map(loads_canonical(self.canonical_bytes()), "intent")
    def canonical_bytes(self) -> bytes: return dumps_canonical(self.value)
    def canonical_digest(self) -> str: return canonical_digest(self.value)


def substitute(intent: TransactionFinalizationIntent, wal: schemas.WAL, branch: str) -> dict[str, Any]:
    """Perform the normative digest→maps→candidate-checksum substitution order."""
    if branch not in {"commit", "rollback"}: raise SchemaError("invalid candidate branch")
    value = intent.to_mapping(); digest = intent.canonical_digest()
    if wal.transaction_intent_digest != digest: raise IntegrityError("WAL/intent digest mismatch")
    if (wal.transaction_id, wal.operation, wal.proposal_id, wal.proposal_revision) != (
            value["transaction_id"], value["operation"], value["proposal_id"], value["proposal_revision"]):
        raise IntegrityError("WAL/intent identity mismatch")
    linked = {row["concept"]: row for row in value["targets"]}
    if [t.concept for t in wal.targets] != list(linked): raise IntegrityError("WAL/intent target mismatch")
    for target in wal.targets:
        row = linked[target.concept]
        if (row["before_digest"], row["after_digest"], row["before_identity"],
                row["staged_identity"], row["backup_identity"]) != (
                target.before_digest, target.staged_digest, target.before_identity.to_mapping(),
                target.staged_identity.to_mapping(), target.backup_leaf.to_mapping()):
            raise IntegrityError("WAL/intent target linkage mismatch")
    before = {t.concept: t.before_digest.removeprefix("sha256:") for t in wal.targets}
    after = {t.concept: t.staged_digest.removeprefix("sha256:") for t in wal.targets}
    resulting = (before if branch == "rollback" else after) if wal.operation == "apply" else (
        before if branch == "rollback" else {
            t.concept: t.restore_digest.removeprefix("sha256:") for t in wal.targets})
    before_identities = {t.concept: t.before_identity.to_mapping() for t in wal.targets}
    resulting_identities = (before_identities if branch == "rollback" else
        {t.concept: t.resulting_identity.to_mapping() for t in wal.targets
         if t.resulting_identity is not None})
    if branch == "commit" and len(resulting_identities) != len(wal.targets):
        raise IntegrityError("commit candidate requires every resulting identity")
    replacements = {"target_before_revisions": before, "target_after_revisions": after,
        "target_before_identities": before_identities,
        "target_resulting_identities": resulting_identities,
        "target_backup_identities": {t.concept: t.backup_leaf.to_mapping() for t in wal.targets},
        "resulting_revisions": resulting, "concept_revisions": resulting,
        "transaction_intent_digest": digest}
    def walk(item: Any) -> Any:
        if isinstance(item, Mapping) and set(item) == {"$intent_placeholder"}:
            name = item["$intent_placeholder"]
            return dict(item) if name == "candidate_checksum" else replacements[name]
        if isinstance(item, Mapping): return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list): return [walk(child) for child in item]
        return item
    candidate = walk(value["candidates"][branch])
    rows = [candidate["audit"], *candidate["outbox"], candidate["result"]]
    for row in rows:
        if isinstance(row.get("checksum"), Mapping): row["checksum"] = compute_checksum(row)
    schemas.TransactionAudit.from_mapping(candidate["audit"])
    for row in candidate["outbox"]: schemas.OutboxReceipt.from_mapping(row)
    schemas.EndpointResult.from_mapping(candidate["result"])
    return candidate


__all__ = ["TransactionFinalizationIntent", "substitute"]
