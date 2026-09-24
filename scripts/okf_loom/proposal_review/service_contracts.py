"""Transport-neutral caller contracts and envelopes for Proposal Review v1.

These objects are deliberately narrower than the persisted schemas.  In
particular callers cannot choose record ids, proposal revisions, timestamps,
preview references, result links, or expiry authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import ids, schemas
from .canonical import loads_canonical
from .errors import SchemaError
from .pagination import PAGE_LIMIT_DEFAULT, PAGE_LIMIT_MAX, PAGE_LIMIT_MIN


def _object(value: Mapping[str, Any] | bytes | str, name: str, *, max_bytes: int) -> dict[str, Any]:
    if isinstance(value, (bytes, str)):
        size = len(value) if isinstance(value, bytes) else len(value.encode("utf-8"))
        if size > max_bytes:
            raise SchemaError(f"{name} body exceeds {max_bytes} bytes", code="body_too_large", path=name)
        value = loads_canonical(value)
    if not isinstance(value, Mapping):
        raise SchemaError(f"{name} must be an object", path=name)
    return dict(value)


def _closed(obj: Mapping[str, Any], fields: frozenset[str], name: str) -> None:
    extra = set(obj) - fields
    missing = fields - set(obj)
    if extra or missing:
        problem = f"unknown fields {sorted(extra)}" if extra else f"missing fields {sorted(missing)}"
        raise SchemaError(f"{name} has {problem}", path=name)


@dataclass(frozen=True)
class CreateProposalInput:
    schema: int
    mode: str
    author: str
    summary: str
    rationale: str
    expected_snapshot: schemas.Snapshot
    operations: tuple[schemas.Operation, ...]
    provenance: tuple[schemas.ProvenanceEntry, ...]
    extensions: Mapping[str, Any]
    proposal_id: str | None = None
    supersedes_revision: int | None = None
    legacy_identity: schemas.LegacyIdentity | None = None

    BASE = frozenset({"schema", "mode", "author", "summary", "rationale",
                      "expected_snapshot", "operations", "provenance", "extensions"})
    SUPERSEDE = BASE | frozenset({"proposal_id", "supersedes_revision"})
    LEGACY = BASE | frozenset({"legacy_identity"})

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | bytes | str) -> "CreateProposalInput":
        obj = _object(value, "create", max_bytes=1_048_576)
        has_supersede = "proposal_id" in obj or "supersedes_revision" in obj
        has_legacy = "legacy_identity" in obj
        if has_supersede and has_legacy:
            raise SchemaError("legacy regeneration cannot supersede a v1 proposal", path="create")
        fields = cls.SUPERSEDE if has_supersede else cls.LEGACY if has_legacy else cls.BASE
        _closed(obj, fields, "create")
        if obj["schema"] != 1 or obj["mode"] != "proposal":
            raise SchemaError("create requires schema 1 and mode 'proposal'", path="create")
        author = ids.validate_actor(obj["author"], name="create.author")
        summary, rationale = obj["summary"], obj["rationale"]
        if not isinstance(summary, str) or not 1 <= len(summary) <= schemas.MAX_SUMMARY_CODEPOINTS:
            raise SchemaError("summary is outside its closed bound", path="create.summary")
        if not isinstance(rationale, str) or not 1 <= len(rationale) <= schemas.MAX_RATIONALE_CODEPOINTS:
            raise SchemaError("rationale is outside its closed bound", path="create.rationale")
        snapshot = schemas.Snapshot.from_mapping(obj["expected_snapshot"], path="create.expected_snapshot")
        raw_ops = obj["operations"]
        if not isinstance(raw_ops, list) or not raw_ops or len(raw_ops) > schemas.MAX_OPERATIONS:
            raise SchemaError("operations must be a non-empty bounded array", path="create.operations")
        operations = tuple(schemas.Operation.from_mapping(op, path=f"create.operations[{i}]")
                           for i, op in enumerate(raw_ops))
        # Reuse ProposalRevision's graph/affected-set validation without ever
        # accepting caller-owned persistence fields.
        op_ids = [op.op_id for op in operations]
        if len(op_ids) != len(set(op_ids)):
            raise SchemaError("operation ids must be unique", path="create.operations")
        schemas._validate_operation_graph(list(operations), path="create.operations")
        raw_prov = obj["provenance"]
        if not isinstance(raw_prov, list) or len(raw_prov) > schemas.MAX_PROVENANCE_ENTRIES:
            raise SchemaError("provenance must be a bounded array", path="create.provenance")
        provenance = tuple(schemas.ProvenanceEntry.from_mapping(p, path=f"create.provenance[{i}]")
                           for i, p in enumerate(raw_prov))
        extensions = ids.validate_extensions(obj["extensions"])
        proposal_id = None
        supersedes = None
        if fields == cls.SUPERSEDE:
            proposal_id = ids.validate_proposal_id(obj["proposal_id"])
            supersedes = ids.validate_revision(obj["supersedes_revision"], name="supersedes_revision")
        legacy_identity = (schemas.LegacyIdentity.from_mapping(
            obj["legacy_identity"], path="create.legacy_identity")
            if fields == cls.LEGACY else None)
        return cls(1, "proposal", author, summary, rationale, snapshot,
                     tuple(sorted(operations, key=lambda op: op.op_id)), provenance,
                     extensions, proposal_id, supersedes, legacy_identity)

    def fingerprint_mapping(self) -> dict[str, Any]:
        value = {
            "schema": 1, "mode": self.mode, "author": self.author,
            "summary": self.summary, "rationale": self.rationale,
            "expected_snapshot": self.expected_snapshot.to_mapping(),
            "operations": [op.to_mapping() for op in self.operations],
            "provenance": [p.to_mapping() for p in self.provenance],
            "extensions": dict(self.extensions),
        }
        if self.proposal_id is not None:
            value.update(proposal_id=self.proposal_id,
                           supersedes_revision=self.supersedes_revision)
        if self.legacy_identity is not None:
            value["legacy_identity"] = self.legacy_identity.to_mapping()
        return value


@dataclass(frozen=True)
class DecisionInput:
    schema: int
    decision: str
    reason: str
    previous_row_token: str | None
    read_model_token: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | bytes | str) -> "DecisionInput":
        obj = _object(value, "decision", max_bytes=65_536)
        _closed(obj, frozenset({"schema", "decision", "reason", "previous_row_token", "read_model_token"}), "decision")
        if obj["schema"] != 1 or obj["decision"] not in schemas.DECISION_VALUES:
            raise SchemaError("decision requires schema 1 and accepted/rejected", path="decision")
        reason = obj["reason"]
        if not isinstance(reason, str) or len(reason) > schemas.MAX_DECISION_REASON_CODEPOINTS:
            raise SchemaError("decision reason is outside its closed bound", path="decision.reason")
        previous = obj["previous_row_token"]
        if previous is not None:
            previous = ids.validate_row_token(previous, name="decision.previous_row_token")
        return cls(1, obj["decision"], reason, previous,
                   ids.validate_digest(obj["read_model_token"], name="decision.read_model_token"))


@dataclass(frozen=True)
class WithdrawalInput:
    schema: int
    read_model_token: str
    reason: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | bytes | str) -> "WithdrawalInput":
        obj = _object(value, "withdrawal", max_bytes=65_536)
        _closed(obj, frozenset({"schema", "read_model_token", "reason"}), "withdrawal")
        if obj["schema"] != 1:
            raise SchemaError("withdrawal schema must be 1", path="withdrawal.schema")
        reason = obj["reason"]
        if not isinstance(reason, str) or not 1 <= len(reason) <= schemas.MAX_WITHDRAWAL_REASON_CODEPOINTS:
            raise SchemaError("withdrawal reason is outside its closed bound", path="withdrawal.reason")
        return cls(1, ids.validate_digest(obj["read_model_token"], name="withdrawal.read_model_token"), reason)


@dataclass(frozen=True)
class ApplyInput:
    """Closed transport-neutral apply authority; idempotency is out-of-body."""

    schema: int
    proposal_id: str
    proposal_revision: int
    actor: str
    snapshot_digest: str
    validation_context_digest: str
    decision_set_token: str
    read_model_token: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | bytes | str) -> "ApplyInput":
        obj = _object(value, "apply", max_bytes=65_536)
        _closed(obj, frozenset({"schema", "proposal_id", "proposal_revision", "actor",
                                "snapshot_digest", "validation_context_digest",
                                "decision_set_token", "read_model_token"}), "apply")
        if obj["schema"] != 1:
            raise SchemaError("apply schema must be 1", path="apply.schema")
        return cls(1, ids.validate_proposal_id(obj["proposal_id"]),
                   ids.validate_revision(obj["proposal_revision"], name="apply.proposal_revision"),
                   ids.validate_actor(obj["actor"], name="apply.actor"),
                   ids.validate_digest(obj["snapshot_digest"], name="apply.snapshot_digest"),
                   ids.validate_digest(obj["validation_context_digest"], name="apply.validation_context_digest"),
                   ids.validate_digest(obj["decision_set_token"], name="apply.decision_set_token"),
                   ids.validate_digest(obj["read_model_token"], name="apply.read_model_token"))

    def fingerprint_mapping(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class UndoInput:
    """Closed transport-neutral proposal-linked undo authority."""

    schema: int
    proposal_id: str
    proposal_revision: int
    actor: str
    transaction_id: str
    undo_group_id: str
    read_model_token: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | bytes | str) -> "UndoInput":
        obj = _object(value, "undo", max_bytes=65_536)
        _closed(obj, frozenset({"schema", "proposal_id", "proposal_revision", "actor",
                                "transaction_id", "undo_group_id", "read_model_token"}), "undo")
        if obj["schema"] != 1:
            raise SchemaError("undo schema must be 1", path="undo.schema")
        return cls(1, ids.validate_proposal_id(obj["proposal_id"]),
                   ids.validate_revision(obj["proposal_revision"], name="undo.proposal_revision"),
                   ids.validate_actor(obj["actor"], name="undo.actor"),
                   ids.validate_txn_id(obj["transaction_id"]),
                   ids.validate_undo_id(obj["undo_group_id"]),
                   ids.validate_digest(obj["read_model_token"], name="undo.read_model_token"))

    def fingerprint_mapping(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class StrictListQuery:
    state: str | None = None
    limit: int = PAGE_LIMIT_DEFAULT
    cursor: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StrictListQuery":
        obj = dict(value)
        unknown = set(obj) - {"state", "limit", "cursor"}
        if unknown:
            raise SchemaError(f"list query has unknown fields {sorted(unknown)}", path="query")
        state = obj.get("state")
        # Importing the pure ListQuery is the single enum authority.
        from .pagination import ListQuery
        ListQuery(state)
        limit = obj.get("limit", PAGE_LIMIT_DEFAULT)
        if isinstance(limit, bool) or not isinstance(limit, int) or not PAGE_LIMIT_MIN <= limit <= PAGE_LIMIT_MAX:
            raise SchemaError(f"limit must be {PAGE_LIMIT_MIN}..{PAGE_LIMIT_MAX}", path="query.limit")
        cursor = obj.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise SchemaError("cursor must be a non-empty string", path="query.cursor")
        return cls(state, limit, cursor)


@dataclass(frozen=True)
class SuccessEnvelope:
    data: Mapping[str, Any]
    schema: int = 1

    def to_mapping(self) -> dict[str, Any]:
        return {"schema": 1, "data": dict(self.data)}


@dataclass(frozen=True)
class ErrorBody:
    code: str
    message: str
    request_id: str
    retryable: bool = False
    resync_required: bool = False
    details: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ErrorBody":
        required = {"code", "message", "request_id", "retryable",
                    "resync_required", "details"}
        if set(value) != required:
            raise SchemaError("error has an invalid shape", path="error")
        if any(not isinstance(value[name], str) or not value[name]
               for name in ("code", "message", "request_id")):
            raise SchemaError("error code, message, and request_id must be non-empty strings",
                              path="error")
        if not isinstance(value["retryable"], bool) or not isinstance(value["resync_required"], bool):
            raise SchemaError("error disposition flags must be booleans", path="error")
        details = value["details"]
        if not isinstance(details, list) or any(not isinstance(item, Mapping) for item in details):
            raise SchemaError("error details must be a list of objects", path="error.details")
        return cls(value["code"], value["message"], value["request_id"],
                   value["retryable"], value["resync_required"],
                   tuple(dict(item) for item in details))


@dataclass(frozen=True)
class ErrorEnvelope:
    error: ErrorBody
    schema: int = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ErrorEnvelope":
        if set(value) != {"schema", "error"} or value.get("schema") != 1:
            raise SchemaError("error envelope has an invalid shape", path="envelope")
        raw_error = value.get("error")
        if not isinstance(raw_error, Mapping):
            raise SchemaError("error envelope error must be an object", path="error")
        return cls(ErrorBody.from_mapping(raw_error))

    def to_mapping(self) -> dict[str, Any]:
        e = self.error
        return {"schema": 1, "error": {"code": e.code, "message": e.message,
                "request_id": e.request_id, "retryable": e.retryable,
                "resync_required": e.resync_required,
                "details": [dict(item) for item in e.details]}}


@dataclass(frozen=True)
class TransportDisposition:
    http_status: int
    cli_exit: int
    retryable: bool = False
    resync_required: bool = False


@dataclass(frozen=True)
class PersistedErrorResponse:
    """The immutable wire error stored for a terminal transaction failure."""

    status: int
    envelope: Mapping[str, Any]

    def to_mapping(self) -> dict[str, Any]:
        return ErrorEnvelope.from_mapping(self.envelope).to_mapping()


# Successful terminal responses use exit 0; asynchronous mutation progress is
# successful transport but not completion and therefore has its own disposition.
CLI_NONTERMINAL = 7


def success_cli_exit(data: Mapping[str, Any]) -> int:
    """Map a successful service result to its CLI completion disposition."""
    return CLI_NONTERMINAL if data.get("status") == "transaction_in_progress" else 0


_HTTP_STATUS = {
    "invalid_host": 400, "invalid_json": 400, "invalid_cursor": 400,
    "invalid_request_target": 400, "request_target_too_large": 414,
    "invalid_session_token": 401, "invalid_proposal_token": 401,
    "origin_forbidden": 403, "read_only": 403,
    "proposal_mutation_loopback_only": 403,
    "not_found": 404, "capability_unavailable": 404, "proposal_not_found": 404,
    "method_not_allowed": 405, "preview_expired": 410,
    "body_too_large": 413, "read_model_too_large": 413,
    "unsupported_media_type": 415,
    "invalid_request": 422, "validation_failed": 422, "invalid_document": 422,
    "operation_invalid": 422, "undeclared_read": 422,
    "preview_limit_exceeded": 422, "validation_profile_unavailable": 422,
    "create_rate_limited": 429, "rate_limited": 429,
    "preview_concurrency_limited": 429,
    "idempotency_conflict": 409, "indeterminate_outcome": 409,
    "cursor_expired": 409, "invalid_state": 409, "stale_read_model": 409,
    "proposal_conflict": 409, "decision_conflict": 409,
    "snapshot_changed": 409, "snapshot_unstable": 409,
    "legacy_identity_conflict": 409,
    "repository_quota_exceeded": 507, "checkpoint_quota_exceeded": 507,
    "tombstone_quota_exceeded": 507, "quarantine_quota_exceeded": 507,
    "audit_quota_exceeded": 507,
    "bundle_busy": 503, "lock_unavailable": 503, "storage_corrupt": 503,
    "storage_unsafe": 503, "preview_authority_unavailable": 503,
    "preview_timeout": 503, "operation_timeout": 503,
    "service_mutation_in_progress": 503,
    "transaction_recovering": 503,
    "apply_rejected": 409,
    "internal_error": 500, "contract_parity_failure": 500,
}
_CLI_VALIDATION = {"validation_failed"}
_CLI_UNAVAILABLE = {"capability_unavailable", "proposal_not_found", "preview_expired", "not_found"}
_CLI_CONFLICT = {"stale_read_model", "decision_conflict", "idempotency_conflict",
                 "indeterminate_outcome", "invalid_state", "proposal_conflict",
                 "cursor_expired", "snapshot_changed", "snapshot_unstable"}
_CLI_CONFLICT.add("legacy_identity_conflict")
_RESYNC = {"stale_read_model", "indeterminate_outcome", "cursor_expired",
           "snapshot_changed", "snapshot_unstable"}
TRANSPORT_CODES = frozenset(_HTTP_STATUS)


def transport_disposition(code: str) -> TransportDisposition:
    status = _HTTP_STATUS.get(code, 500)
    if code in _CLI_VALIDATION:
        cli_exit = 3
    elif code in _CLI_UNAVAILABLE:
        cli_exit = 4
    elif code in _CLI_CONFLICT:
        cli_exit = 5
    elif status in {400, 401, 403, 405, 413, 414, 415, 422}:
        cli_exit = 2
    else:
        cli_exit = 6
    return TransportDisposition(status, cli_exit, status in {429, 503}, code in _RESYNC)


def bounded_error_details(exc: BaseException) -> tuple[Mapping[str, Any], ...]:
    values: list[Mapping[str, Any]] = []
    path = getattr(exc, "path", None)
    if isinstance(path, str) and path:
        values.append({"path": path[:256]})
    raw = getattr(exc, "details", None)
    if isinstance(raw, (list, tuple)):
        for item in raw[:31]:
            if isinstance(item, Mapping):
                clean = {str(k)[:64]: v for k, v in list(item.items())[:16]
                         if isinstance(v, (str, int, bool, type(None)))}
                values.append(clean)
    return tuple(values)


__all__ = ["CreateProposalInput", "DecisionInput", "WithdrawalInput", "ApplyInput", "UndoInput",
           "StrictListQuery", "SuccessEnvelope", "ErrorBody", "ErrorEnvelope",
            "TransportDisposition", "PersistedErrorResponse", "TRANSPORT_CODES", "CLI_NONTERMINAL", "success_cli_exit",
           "transport_disposition",
           "bounded_error_details"]
