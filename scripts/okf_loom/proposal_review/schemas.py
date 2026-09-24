"""Closed contract records for Proposal / Diff Review v1 (Foundation A).

Immutable typed closed records + validators for the complete SPEC §§5-9
persisted and wire schema families. Every record:

* is a frozen dataclass with explicit fields (no silent unknown-field
  acceptance);
* exposes ``from_mapping`` (closed-schema parse: rejects unknown / missing /
  wrong-type, checks SPEC section 4 bounds, verifies checksums / digests);
* exposes ``from_json`` (canonical-JSON parse via :func:`loads_canonical` so
  duplicate-key / depth / UTF-8 / NaN / negative-zero faults map to
  ``CanonicalJsonError``);
* exposes ``to_mapping`` / ``canonical_bytes`` / ``canonical_digest`` for
  deterministic canonical serialization;
* preserves namespaced ``extensions`` verbatim but derives no authority from
  them.

Booleans are not integers: integer fields reject ``bool`` explicitly.
Canonical serialized size limits are enforced at the relevant schema
boundaries (preview 36 MiB, checkpoint 64 MiB, concept before/after 2 MiB).

This module imports only stdlib + sibling :mod:`canonical`/:mod:`ids`/
:mod:`errors` modules. It does NOT import server / studio / cli / render /
viewer / mutation-handler / production-Bundle code.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from . import ids
from .canonical import (
    canonical_digest,
    compute_checksum,
    dumps_canonical,
    loads_canonical,
)
from .errors import (
    IntegrityError,
    LimitError,
    PrimitiveError,
    SchemaError,
)


class FrozenList(list):
    """Immutable list subclass that rejects all mutation.

    ``isinstance(x, list)`` remains True so validators and ``json.dumps``
    work unchanged.  ``x[i] = v``, ``x.append()``, etc. raise ``TypeError``.
    """

    def __init__(self, data: Any = ()) -> None:
        super().__init__(_freeze_value(v) for v in data)

    @staticmethod
    def _readonly(*args: Any, **kwargs: Any) -> "FrozenList":
        raise TypeError("FrozenList is immutable")

    __setitem__ = _readonly  # type: ignore[assignment]
    __delitem__ = _readonly  # type: ignore[assignment]
    append = _readonly  # type: ignore[assignment]
    extend = _readonly  # type: ignore[assignment]
    insert = _readonly  # type: ignore[assignment]
    remove = _readonly  # type: ignore[assignment]
    pop = _readonly  # type: ignore[assignment]
    clear = _readonly  # type: ignore[assignment]
    sort = _readonly  # type: ignore[assignment]
    reverse = _readonly  # type: ignore[assignment]
    __iadd__ = _readonly  # type: ignore[assignment]
    __imul__ = _readonly  # type: ignore[assignment]


class FrozenDict(dict):
    """Immutable dict subclass that rejects all mutation.

    ``isinstance(x, dict)`` remains True so existing validators, parsers, and
    ``json.dumps`` work unchanged.  But ``x[k] = v``, ``x.pop()``, ``x.clear()``
    etc. raise ``TypeError``.  Nested dicts/lists are recursively frozen at
    construction time, so ``x["nested"]["key"] = v`` also raises.
    """

    def __init__(self, data: Any = None, **kwargs: Any) -> None:
        if data is None:
            data = {}
        frozen: dict[str, Any] = {}
        for k, v in dict(data).items():
            frozen[k] = _freeze_value(v)
        super().__init__(frozen)

    @staticmethod
    def _readonly(*args: Any, **kwargs: Any) -> "FrozenDict":
        raise TypeError("FrozenDict is immutable")

    __setitem__ = _readonly  # type: ignore[assignment]
    __delitem__ = _readonly  # type: ignore[assignment]
    pop = _readonly  # type: ignore[assignment]
    popitem = _readonly  # type: ignore[assignment]
    clear = _readonly  # type: ignore[assignment]
    update = _readonly  # type: ignore[assignment]
    setdefault = _readonly  # type: ignore[assignment]
    __ior__ = _readonly  # type: ignore[assignment]


def _freeze_value(obj: Any) -> Any:
    """Recursively freeze a JSON value: dicts→FrozenDict, lists→FrozenList."""
    if isinstance(obj, dict):
        return FrozenDict(obj)
    if isinstance(obj, list):
        return FrozenList(obj)
    if isinstance(obj, tuple):
        return tuple(_freeze_value(v) for v in obj)
    return obj


def _freeze_json(obj: Any) -> Any:
    """Recursively wrap mutable JSON values for frozen record fields.

    Returns ``FrozenDict`` for dicts, tuples of frozen values for lists/tuples,
    and the scalar as-is.
    """
    return _freeze_value(obj)


def _thaw_dict(fd: dict) -> dict[str, Any]:
    """Recursively convert a FrozenDict (or plain dict) back to plain mutable dicts/lists."""
    out: dict[str, Any] = {}
    for k, v in fd.items():
        if isinstance(v, _ClosedRecord):
            out[k] = v.to_mapping()
        elif isinstance(v, dict):
            out[k] = _thaw_dict(v)
        elif isinstance(v, (list, tuple)):
            out[k] = [_thaw_nested(i) for i in v]
        else:
            out[k] = v
    return out


def _thaw_nested(v: Any) -> Any:
    """Thaw a single value that may be a ClosedRecord, FrozenDict, or scalar."""
    if isinstance(v, _ClosedRecord):
        return v.to_mapping()
    if isinstance(v, dict):
        return _thaw_dict(v)
    return v

# --- SPEC section 4 static bounds (subset relevant to closed-record validation)

MAX_SUMMARY_CODEPOINTS = 240
MAX_RATIONALE_CODEPOINTS = 8_192
MAX_PROVENANCE_ENTRIES = 16
MAX_PROVENANCE_VALUE_CODEPOINTS = 2_048
MAX_OPERATIONS = 128
MAX_DEPENDENCY_EDGES = 512
MAX_AFFECTED_CONCEPTS = 256
MAX_SNAPSHOT_CONCEPTS = 10_000
MAX_READS_OR_WRITES_PER_OP = 64
MAX_DECISION_REASON_CODEPOINTS = 1_024
MAX_WITHDRAWAL_REASON_CODEPOINTS = 1_024
MAX_CONCEPT_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_BYTES = 36 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024
MAX_VALIDATION_FINDINGS = 10_000
MAX_DIFF_LINES = 150_000
MAX_DIFF_BYTES = 6 * 1024 * 1024
MAX_PROVENANCE_KINDS = frozenset({"comment", "plan", "workflow", "external"})
MAX_CURSOR_KEYS = 3
MIN_CURSOR_KEYS = 1

WAL_PHASES: frozenset[str] = frozenset({
    "prepared", "commit_decided", "publishing", "content_published",
    "finalizing", "committed", "rolling_back", "rolled_back", "indeterminate",
})
WAL_OPERATION_PREFIXES: dict[str, str] = {"apply": "txn_", "undo": "undo_"}

AGGREGATE_KINDS: frozenset[str] = frozenset({
    "proposals", "decisions", "transactions", "endpoint_results",
    "outbox", "tombstones", "corruption",
})
CHECKPOINT_KINDS: frozenset[str] = AGGREGATE_KINDS
WRAPPER_KINDS: frozenset[str] = frozenset({
    "proposal", "withdrawal", "decision", "transaction", "endpoint_result",
    "outbox", "tombstone", "corruption", "repair",
})
AUTHORITY_KINDS: frozenset[str] = frozenset({
    "repository", "aggregate", "checkpoint", "preview", "endpoint_result",
    "transaction_file",
})
TRANSACTION_FILE_COMPONENTS: frozenset[str] = frozenset({
    "wal", "history", "stage_leaf", "backup_leaf",
})
CORRUPTION_REASONS: frozenset[str] = frozenset({
    "missing", "malformed", "unsupported_schema", "checksum_mismatch",
    "digest_mismatch", "sequence_gap", "duplicate_identity", "type_mismatch",
    "owner_mismatch", "mode_mismatch", "link_count_mismatch", "path_substitution",
    "authority_contradiction",
})
NULL_DIGEST_REASONS: frozenset[str] = frozenset({
    "missing", "type_mismatch", "owner_mismatch", "mode_mismatch",
    "link_count_mismatch", "path_substitution",
})
DECISION_VALUES: frozenset[str] = frozenset({"accepted", "rejected"})
CONFLICT_CODES: frozenset[str] = frozenset({
    "snapshot_changed", "read_revision_changed", "write_revision_changed",
    "target_missing", "target_type_changed", "target_identity_changed", "capability_changed",
    "config_changed", "preview_expired", "decision_stale",
    "dependency_unsatisfied", "validation_stale",
})
NOTIFICATION_EVENTS: frozenset[str] = frozenset({
    "created", "superseded", "decision_changed", "withdrawn",
    "conflict_changed", "applied", "undo_changed", "expired",
})
AUDIT_STATUS: frozenset[str] = frozenset({
    "applied", "undone", "rejected", "conflicted", "expired", "indeterminate",
})
VALIDATION_STATUS: frozenset[str] = frozenset({"pass", "fail", "not_run"})
ENDPOINT_RESULT_STATES: frozenset[str] = frozenset({"prepared", "terminal", "indeterminate"})
TOMBSTONE_OUTCOMES: frozenset[str] = frozenset({"terminal_compacted", "indeterminate"})
OUTBOX_STATES: frozenset[str] = frozenset({"pending", "appended"})
OUTBOX_AGGREGATE_KINDS: frozenset[str] = frozenset({
    "proposal", "decision", "transaction", "withdrawal",
})
REPAIR_ACTIONS: frozenset[str] = frozenset({"replace_authority", "classify_indeterminate"})
REPAIR_SOURCE_KINDS: frozenset[str] = frozenset({"validated_backup", "operator_supplied"})
PROGRESS_STATUSES: frozenset[str] = frozenset({
    "preparing", "rollback_pending", "forward_committed", "publishing",
    "finalizing", "recovering", "indeterminate",
})
# Conflicts whose op_id/concept may both be null (proposal-scoped).
_PROPOSAL_LEVEL_CONFLICT_CODES: frozenset[str] = frozenset({
    "snapshot_changed", "capability_changed", "config_changed",
    "preview_expired", "validation_stale",
})


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _reject_unknown(obj: Mapping[str, Any], allowed: frozenset[str], *, path: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise SchemaError(f"unknown field(s): {sorted(extra)}", path=path)


def _require(obj: Mapping[str, Any], name: str, *, path: str) -> Any:
    if name not in obj:
        raise SchemaError(f"missing required field: {name}", path=f"{path}.{name}")
    return obj[name]


def _expect_str(v: Any, name: str, *, path: str) -> str:
    if not isinstance(v, str):
        raise SchemaError(f"{name} must be a string", path=path)
    return v


def _expect_bool(v: Any, name: str, *, path: str) -> bool:
    if not isinstance(v, bool):
        raise SchemaError(f"{name} must be a boolean", path=path)
    return v


def _expect_int(v: Any, name: str, *, path: str) -> int:
    if isinstance(v, bool):
        # SPEC section 3: "Booleans are not integers." This is a primitive-type
        # rule, so it raises PrimitiveError (not the structural SchemaError).
        raise PrimitiveError(f"{name} must be an integer (booleans are not integers)", path=path)
    if not isinstance(v, int):
        raise SchemaError(f"{name} must be an integer", path=path)
    return v


def _expect_list(v: Any, name: str, *, path: str) -> list:
    if not isinstance(v, list):
        raise SchemaError(f"{name} must be an array", path=path)
    return v


def _expect_mapping(v: Any, name: str, *, path: str) -> dict:
    if not isinstance(v, dict):
        raise SchemaError(f"{name} must be an object", path=path)
    return v


def _bounded_str(v: Any, name: str, *, lo: int, hi: int, path: str) -> str:
    s = _expect_str(v, name, path=path)
    n = len(s)
    if n < lo or n > hi:
        raise LimitError(f"{name} must be {lo}..{hi} code points (got {n})", path=path)
    return s


def _schema_int(v: Any, *, expected: int, path: str, name: str = "schema") -> int:
    i = _expect_int(v, name, path=path)
    if i != expected:
        raise SchemaError(f"unsupported {name}: {i} (expected {expected})", path=path)
    return i


def _sorted_unique_strings(v: Any, name: str, *, path: str) -> list[str]:
    lst = _expect_list(v, name, path=path)
    out: list[str] = []
    for item in lst:
        if not isinstance(item, str):
            raise SchemaError(f"{name} entries must be strings", path=path)
        out.append(item)
    if out != sorted(out):
        raise SchemaError(f"{name} must be sorted ascending", path=path)
    if len(set(out)) != len(out):
        raise SchemaError(f"{name} must be unique", path=path)
    return out


# ---------------------------------------------------------------------------
# Base record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClosedRecord:
    """Base for canonical-serializable closed records.

    Every nested mutable JSON structure (dict/list) in a field value is wrapped
    in :class:`FrozenJSON` at construction time so that post-parse mutation
    (``record.args[...] = ...``, ``record.payload[...] = ...``) raises
    ``TypeError`` rather than silently changing canonical bytes/digests.
    Source mutation is also defeated because the source dict/list is never
    stored by reference — ``FrozenJSON`` recursively copies on construction.
    """

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):
            object.__setattr__(self, f.name, _freeze_json(getattr(self, f.name)))

    def _thaw_field(self, value: Any) -> Any:
        if isinstance(value, _ClosedRecord):
            return value.to_mapping()
        if isinstance(value, FrozenDict):
            return _thaw_dict(value)
        if isinstance(value, tuple):
            return [self._thaw_field(v) for v in value]
        return value

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            out[f.name] = self._thaw_field(getattr(self, f.name))
        return out

    def canonical_bytes(self) -> bytes:
        return dumps_canonical(self.to_mapping())

    def canonical_digest(self) -> str:
        return canonical_digest(self.to_mapping())

    @classmethod
    def from_json(cls, text: str | bytes) -> "_ClosedRecord":
        return cls.from_mapping(loads_canonical(text))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# section 5.3 Snapshot + supporting sub-records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConceptIdentity(_ClosedRecord):
    parent_device: int
    parent_inode: int
    leaf_device: int
    leaf_inode: int
    ctime_ns: int
    mode: int
    size: int
    digest: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({"parent_device", "parent_inode",
        "leaf_device", "leaf_inode", "ctime_ns", "mode", "size", "digest"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "concept_identity") -> "ConceptIdentity":
        _reject_unknown(obj, cls.FIELDS, path=path)
        values = {name: _expect_int(_require(obj, name, path=path), name,
                                    path=f"{path}.{name}") for name in
                  ("parent_device", "parent_inode", "leaf_device", "leaf_inode",
                   "ctime_ns", "mode")}
        if any(value < 0 for value in values.values()):
            raise SchemaError("concept identity integers must be non-negative", path=path)
        size = ids.validate_stored_byte_size(_require(obj, "size", path=path), name=f"{path}.size")
        digest = ids.validate_content_revision(_require(obj, "digest", path=path), name=f"{path}.digest")
        return cls(**values, size=size, digest=digest)


@dataclass(frozen=True)
class InventoryEntry(_ClosedRecord):
    concept: str
    revision: str
    identity: ConceptIdentity

    FIELDS: ClassVar[frozenset[str]] = frozenset({"concept", "revision", "identity"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "concept") -> "InventoryEntry":
        _reject_unknown(obj, cls.FIELDS, path=path)
        concept = ids.validate_concept_id(_require(obj, "concept", path=path), name=f"{path}.concept")
        revision = ids.validate_content_revision(_require(obj, "revision", path=path), name=f"{path}.revision")
        identity = ConceptIdentity.from_mapping(_require(obj, "identity", path=path),
                                                path=f"{path}.identity")
        if identity.digest != revision:
            raise SchemaError("concept identity digest must equal revision", path=f"{path}.identity.digest")
        return cls(concept=concept, revision=revision, identity=identity)


@dataclass(frozen=True)
class Snapshot(_ClosedRecord):
    schema: int
    captured_at: str
    concepts: tuple[InventoryEntry, ...]
    capabilities: tuple[str, ...]
    config_digest: str
    validation_context_digest: str
    validator_id: str
    serializer_id: str
    operation_runtime_id: str
    snapshot_digest: str

    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "captured_at", "concepts", "capabilities", "config_digest", "validation_context_digest",
        "validator_id", "serializer_id", "operation_runtime_id", "snapshot_digest",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "snapshot") -> "Snapshot":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        captured_at = ids.validate_timestamp(_require(obj, "captured_at", path=path), name=f"{path}.captured_at")
        raw_concepts = _expect_list(_require(obj, "concepts", path=path), "concepts", path=f"{path}.concepts")
        if len(raw_concepts) < 1 or len(raw_concepts) > MAX_SNAPSHOT_CONCEPTS:
            raise LimitError(f"concepts must be 1..{MAX_SNAPSHOT_CONCEPTS} entries", path=f"{path}.concepts")
        concepts = tuple(
            InventoryEntry.from_mapping(c, path=f"{path}.concepts[{i}]") for i, c in enumerate(raw_concepts)
        )
        ids_sorted = [c.concept for c in concepts]
        if ids_sorted != sorted(ids_sorted):
            raise SchemaError("concepts must be sorted by concept id", path=f"{path}.concepts")
        if len(set(ids_sorted)) != len(ids_sorted):
            raise SchemaError("concepts must be unique", path=f"{path}.concepts")
        capabilities = tuple(_sorted_unique_strings(
            _require(obj, "capabilities", path=path), "capabilities", path=f"{path}.capabilities"
        ))
        config_digest = ids.validate_digest(_require(obj, "config_digest", path=path), name=f"{path}.config_digest")
        validation_context_digest = ids.validate_digest(
            _require(obj, "validation_context_digest", path=path), name=f"{path}.validation_context_digest"
        )
        validator_id = ids.validate_runtime_identity(_require(obj, "validator_id", path=path), name=f"{path}.validator_id")
        serializer_id = ids.validate_runtime_identity(_require(obj, "serializer_id", path=path), name=f"{path}.serializer_id")
        operation_runtime_id = ids.validate_runtime_identity(
            _require(obj, "operation_runtime_id", path=path), name=f"{path}.operation_runtime_id"
        )
        snapshot_digest = ids.validate_digest(_require(obj, "snapshot_digest", path=path), name=f"{path}.snapshot_digest")
        body = {
            "schema": schema,
            "concepts": [c.to_mapping() for c in concepts],
            "capabilities": list(capabilities),
            "config_digest": config_digest,
            "validation_context_digest": validation_context_digest,
            "validator_id": validator_id,
            "serializer_id": serializer_id,
            "operation_runtime_id": operation_runtime_id,
        }
        expected = canonical_digest(body)
        if snapshot_digest != expected:
            raise IntegrityError(
                f"snapshot_digest mismatch: expected {expected}, got {snapshot_digest}",
                path=f"{path}.snapshot_digest",
            )
        return cls(schema=schema, captured_at=captured_at, concepts=concepts,
                     capabilities=capabilities, config_digest=config_digest,
                     validation_context_digest=validation_context_digest,
                   validator_id=validator_id, serializer_id=serializer_id,
                   operation_runtime_id=operation_runtime_id, snapshot_digest=snapshot_digest)

    def concepts_as_mapping(self) -> list[dict[str, Any]]:
        return [c.to_mapping() for c in self.concepts]


# ---------------------------------------------------------------------------
# section 5.2 Operation discriminated union
# ---------------------------------------------------------------------------

_OPERATION_KINDS = frozenset({
    "set_frontmatter", "link_add", "entity_add", "replace_text", "update_section",
})
_OPERATION_COMMON_FIELDS: frozenset[str] = frozenset({
    "schema", "op_id", "kind", "target", "args", "reads", "writes",
    "expected_revisions", "depends_on", "extensions",
})


def _validate_set_frontmatter_args(args: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    _reject_unknown(args, frozenset({"key", "value"}), path=path)
    key = ids.validate_frontmatter_key(_require(args, "key", path=path))
    value = _require(args, "value", path=path)
    if not (value is None or isinstance(value, (bool, int, float, str, list, dict))):
        raise SchemaError("set_frontmatter value must be a JSON scalar/list/object", path=f"{path}.value")
    payload = dumps_canonical({"value": value})
    if len(payload) > 64 * 1024 + 16:
        raise LimitError("set_frontmatter value exceeds 64 KiB canonical JSON", path=f"{path}.value")
    return {"key": key, "value": value}


def _validate_link_add_args(args: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    _reject_unknown(args, frozenset({"destination", "label", "relation", "detail", "allow_forward_reference"}), path=path)
    destination = ids.validate_concept_id(_require(args, "destination", path=path), name=f"{path}.destination")
    label = _bounded_str(_require(args, "label", path=path), "label", lo=1, hi=240, path=f"{path}.label")
    relation = ids.validate_relation(_require(args, "relation", path=path))
    detail_v = _require(args, "detail", path=path)
    detail: str | None = None if detail_v is None else _bounded_str(detail_v, "detail", lo=1, hi=1024, path=f"{path}.detail")
    afr = _expect_bool(_require(args, "allow_forward_reference", path=path),
                       "allow_forward_reference", path=f"{path}.allow_forward_reference")
    if afr is not False:
        raise SchemaError("allow_forward_reference MUST be false in v1", path=f"{path}.allow_forward_reference")
    return {"destination": destination, "label": label, "relation": relation,
            "detail": detail, "allow_forward_reference": afr}


def _validate_entity_add_args(args: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    _reject_unknown(args, frozenset({"label", "kind", "entity_id"}), path=path)
    label = _bounded_str(_require(args, "label", path=path), "label", lo=1, hi=240, path=f"{path}.label")
    kind = _bounded_str(_require(args, "kind", path=path), "kind", lo=1, hi=128, path=f"{path}.kind")
    eid_v = _require(args, "entity_id", path=path)
    entity_id: str | None = None if eid_v is None else _bounded_str(eid_v, "entity_id", lo=1, hi=240, path=f"{path}.entity_id")
    return {"label": label, "kind": kind, "entity_id": entity_id}


def _validate_replace_text_args(args: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    _reject_unknown(args, frozenset({"old", "new", "occurrence"}), path=path)
    old = _bounded_str(_require(args, "old", path=path), "old", lo=1, hi=524_288, path=f"{path}.old")
    new = _bounded_str(_require(args, "new", path=path), "new", lo=0, hi=524_288, path=f"{path}.new")
    occ = _require(args, "occurrence", path=path)
    if isinstance(occ, bool):
        raise SchemaError("occurrence must be an integer 1..10000 or the string 'only'", path=f"{path}.occurrence")
    if isinstance(occ, int):
        if occ < 1 or occ > 10_000:
            raise SchemaError("occurrence integer must be in 1..10000", path=f"{path}.occurrence")
    elif isinstance(occ, str):
        if occ != "only":
            raise SchemaError("occurrence string must be exactly 'only'", path=f"{path}.occurrence")
    else:
        raise SchemaError("occurrence must be an integer 1..10000 or the string 'only'", path=f"{path}.occurrence")
    return {"old": old, "new": new, "occurrence": occ}


def _validate_update_section_args(args: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    _reject_unknown(args, frozenset({"heading", "body", "mode"}), path=path)
    heading = _bounded_str(_require(args, "heading", path=path), "heading", lo=1, hi=512, path=f"{path}.heading")
    body_v = _require(args, "body", path=path)
    if not isinstance(body_v, str):
        raise SchemaError("body must be a string", path=f"{path}.body")
    if len(body_v) > 524_288:
        raise LimitError("body must be 0..524288 code points", path=f"{path}.body")
    mode = _expect_str(_require(args, "mode", path=path), "mode", path=f"{path}.mode")
    if mode not in ("replace", "append"):
        raise SchemaError("mode must be 'replace' or 'append'", path=f"{path}.mode")
    return {"heading": heading, "body": body_v, "mode": mode}


_ARG_VALIDATORS = {
    "set_frontmatter": _validate_set_frontmatter_args,
    "link_add": _validate_link_add_args,
    "entity_add": _validate_entity_add_args,
    "replace_text": _validate_replace_text_args,
    "update_section": _validate_update_section_args,
}


def _derived_read_write(kind: str, target: str, args: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    if kind == "link_add":
        return sorted({target, args["destination"]}), [target]
    return [target], [target]


@dataclass(frozen=True)
class Operation(_ClosedRecord):
    schema: int
    op_id: str
    kind: str
    target: str
    args: dict[str, Any]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    expected_revisions: dict[str, str]
    depends_on: tuple[str, ...]
    extensions: dict[str, Any]

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "operation") -> "Operation":
        _reject_unknown(obj, _OPERATION_COMMON_FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        op_id = ids.validate_op_id(_require(obj, "op_id", path=path))
        kind = _expect_str(_require(obj, "kind", path=path), "kind", path=f"{path}.kind")
        if kind not in _OPERATION_KINDS:
            raise SchemaError(
                f"kind must be one of {sorted(_OPERATION_KINDS)}; no command/execution channel is admitted",
                path=f"{path}.kind",
            )
        target = ids.validate_concept_id(_require(obj, "target", path=path), name=f"{path}.target")
        raw_args = _expect_mapping(_require(obj, "args", path=path), "args", path=f"{path}.args")
        args = _ARG_VALIDATORS[kind](raw_args, path=f"{path}.args")
        reads = _sorted_unique_strings(_require(obj, "reads", path=path), "reads", path=f"{path}.reads")
        if not reads:
            raise SchemaError("reads must be non-empty", path=f"{path}.reads")
        if len(reads) > MAX_READS_OR_WRITES_PER_OP:
            raise LimitError(f"reads must be <= {MAX_READS_OR_WRITES_PER_OP} per operation", path=f"{path}.reads")
        writes = _sorted_unique_strings(_require(obj, "writes", path=path), "writes", path=f"{path}.writes")
        if not writes:
            raise SchemaError("writes must be non-empty", path=f"{path}.writes")
        if len(writes) > MAX_READS_OR_WRITES_PER_OP:
            raise LimitError(f"writes must be <= {MAX_READS_OR_WRITES_PER_OP} per operation", path=f"{path}.writes")
        if target not in writes:
            raise SchemaError("target MUST occur in writes", path=f"{path}.target")
        exp_reads, exp_writes = _derived_read_write(kind, target, args)
        if reads != exp_reads:
            raise SchemaError(f"reads {reads} do not match derived rule {exp_reads} for kind {kind}", path=f"{path}.reads")
        if writes != exp_writes:
            raise SchemaError(f"writes {writes} do not match derived rule {exp_writes} for kind {kind}", path=f"{path}.writes")
        raw_revs = _expect_mapping(_require(obj, "expected_revisions", path=path), "expected_revisions", path=f"{path}.expected_revisions")
        expected_keys = sorted(set(reads) | set(writes))
        if sorted(raw_revs.keys()) != expected_keys:
            raise SchemaError("expected_revisions keys must equal the union of reads and writes", path=f"{path}.expected_revisions")
        expected_revisions: dict[str, str] = {}
        for k in expected_keys:
            expected_revisions[k] = ids.validate_operation_content_revision(raw_revs[k], name=f"{path}.expected_revisions[{k}]")
        depends = _sorted_unique_strings(_require(obj, "depends_on", path=path), "depends_on", path=f"{path}.depends_on")
        for d in depends:
            ids.validate_op_id(d)
        if op_id in depends:
            raise SchemaError("depends_on cannot name self", path=f"{path}.depends_on")
        extensions = ids.validate_extensions(_require(obj, "extensions", path=path))
        return cls(schema=schema, op_id=op_id, kind=kind, target=target, args=args,
                   reads=tuple(reads), writes=tuple(writes),
                   expected_revisions=expected_revisions, depends_on=tuple(depends),
                   extensions=extensions)


def _validate_operation_graph(ops: list[Operation], *, path: str) -> None:
    """Validate depends_on existence and DAG-ness (SPEC section 5.2)."""
    op_index = {o.op_id: o for o in ops}
    edge_count = 0
    for o in ops:
        for dep in o.depends_on:
            edge_count += 1
            if dep not in op_index:
                raise SchemaError(f"depends_on references unknown op_id {dep!r}", path=path)
    if edge_count > MAX_DEPENDENCY_EDGES:
        raise LimitError(f"dependency edges must be <= {MAX_DEPENDENCY_EDGES}", path=path)
    incoming = {o.op_id: 0 for o in ops}
    adj: dict[str, list[str]] = {o.op_id: [] for o in ops}
    for o in ops:
        for dep in o.depends_on:
            adj[dep].append(o.op_id)
            incoming[o.op_id] += 1
    ready = [oid for oid, c in incoming.items() if c == 0]
    visited = 0
    while ready:
        ready.sort()  # ascending bytewise among ready (matches Kahn spec)
        n = ready.pop(0)
        visited += 1
        for m in adj[n]:
            incoming[m] -= 1
            if incoming[m] == 0:
                ready.append(m)
    if visited != len(ops):
        raise SchemaError("depends_on must form a DAG (cycle detected)", path=path)


# ---------------------------------------------------------------------------
# section 5.1 Provenance, PreviewRef, ProposalRevision, Withdrawal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvenanceEntry(_ClosedRecord):
    kind: str
    value: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "value"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "provenance") -> "ProvenanceEntry":
        _reject_unknown(obj, cls.FIELDS, path=path)
        kind = _expect_str(_require(obj, "kind", path=path), "kind", path=f"{path}.kind")
        if kind not in MAX_PROVENANCE_KINDS:
            raise SchemaError(f"provenance kind must be one of {sorted(MAX_PROVENANCE_KINDS)}", path=f"{path}.kind")
        value = _bounded_str(_require(obj, "value", path=path), "value", lo=1, hi=MAX_PROVENANCE_VALUE_CODEPOINTS, path=f"{path}.value")
        return cls(kind=kind, value=value)


@dataclass(frozen=True)
class PreviewRef(_ClosedRecord):
    schema: int
    preview_digest: str
    artifact_bytes: int
    FIELDS: ClassVar[frozenset[str]] = frozenset({"schema", "preview_digest", "artifact_bytes"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "preview_ref") -> "PreviewRef":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        preview_digest = ids.validate_digest(_require(obj, "preview_digest", path=path), name=f"{path}.preview_digest")
        artifact_bytes = ids.validate_stored_byte_size(_require(obj, "artifact_bytes", path=path), name=f"{path}.artifact_bytes")
        return cls(schema=schema, preview_digest=preview_digest, artifact_bytes=artifact_bytes)


@dataclass(frozen=True)
class ProposalRevision(_ClosedRecord):
    schema: int
    record_id: str
    creation_result_record_id: str
    creation_request_fingerprint: str
    proposal_id: str
    proposal_revision: int
    supersedes_revision: int | None
    mode: str
    author: str
    created_at: str
    summary: str
    rationale: str
    affected_concepts: tuple[str, ...]
    expected_snapshot: Snapshot
    operations: tuple[Operation, ...]
    preview_ref: PreviewRef
    provenance: tuple[ProvenanceEntry, ...]
    expires_at: str
    extensions: dict[str, Any]

    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "record_id", "creation_result_record_id",
        "creation_request_fingerprint", "proposal_id", "proposal_revision",
        "supersedes_revision", "mode", "author", "created_at", "summary",
        "rationale", "affected_concepts", "expected_snapshot", "operations",
        "preview_ref", "provenance", "expires_at", "extensions",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "proposal") -> "ProposalRevision":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        record_id = ids.validate_rec_id(_require(obj, "record_id", path=path))
        creation_result_record_id = ids.validate_rec_id(_require(obj, "creation_result_record_id", path=path))
        creation_request_fingerprint = ids.validate_digest(_require(obj, "creation_request_fingerprint", path=path), name=f"{path}.creation_request_fingerprint")
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        proposal_revision = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        supersedes_v = _require(obj, "supersedes_revision", path=path)
        if supersedes_v is None:
            supersedes_revision: int | None = None
            if proposal_revision != 1:
                raise SchemaError("supersedes_revision must be null only for revision 1", path=f"{path}.supersedes_revision")
        else:
            supersedes_revision = ids.validate_revision(supersedes_v, name=f"{path}.supersedes_revision")
            if supersedes_revision != proposal_revision - 1:
                raise SchemaError("supersedes_revision must be exactly proposal_revision-1", path=f"{path}.supersedes_revision")
        mode = _expect_str(_require(obj, "mode", path=path), "mode", path=f"{path}.mode")
        if mode != "proposal":
            raise SchemaError("mode must be the string 'proposal'", path=f"{path}.mode")
        author = ids.validate_actor(_require(obj, "author", path=path), name=f"{path}.author")
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        summary = _bounded_str(_require(obj, "summary", path=path), "summary", lo=1, hi=MAX_SUMMARY_CODEPOINTS, path=f"{path}.summary")
        rationale = _bounded_str(_require(obj, "rationale", path=path), "rationale", lo=1, hi=MAX_RATIONALE_CODEPOINTS, path=f"{path}.rationale")
        affected = _sorted_unique_strings(_require(obj, "affected_concepts", path=path), "affected_concepts", path=f"{path}.affected_concepts")
        for c in affected:
            ids.validate_concept_id(c, name=f"{path}.affected_concepts")
        if len(affected) > MAX_AFFECTED_CONCEPTS:
            raise LimitError(f"affected_concepts must be <= {MAX_AFFECTED_CONCEPTS}", path=f"{path}.affected_concepts")
        snapshot = Snapshot.from_mapping(_require(obj, "expected_snapshot", path=path), path=f"{path}.expected_snapshot")
        raw_ops = _expect_list(_require(obj, "operations", path=path), "operations", path=f"{path}.operations")
        if not raw_ops:
            raise SchemaError("operations must be non-empty", path=f"{path}.operations")
        if len(raw_ops) > MAX_OPERATIONS:
            raise LimitError(f"operations must be <= {MAX_OPERATIONS}", path=f"{path}.operations")
        parsed_ops = [Operation.from_mapping(o, path=f"{path}.operations[{i}]") for i, o in enumerate(raw_ops)]
        op_ids = [o.op_id for o in parsed_ops]
        if len(set(op_ids)) != len(op_ids):
            raise SchemaError("op_id must be unique within a revision", path=f"{path}.operations")
        _validate_operation_graph(parsed_ops, path=f"{path}.operations")
        sorted_ops = sorted(parsed_ops, key=lambda o: o.op_id)
        union_concepts = sorted({c for o in parsed_ops for c in (set(o.reads) | set(o.writes))})
        if sorted(affected) != union_concepts:
            raise SchemaError("affected_concepts must equal the union of all operation read/write sets", path=f"{path}.affected_concepts")
        preview_ref = PreviewRef.from_mapping(_require(obj, "preview_ref", path=path), path=f"{path}.preview_ref")
        raw_prov = _expect_list(_require(obj, "provenance", path=path), "provenance", path=f"{path}.provenance")
        if len(raw_prov) > MAX_PROVENANCE_ENTRIES:
            raise LimitError(f"provenance must be <= {MAX_PROVENANCE_ENTRIES} entries", path=f"{path}.provenance")
        provenance = tuple(ProvenanceEntry.from_mapping(p, path=f"{path}.provenance[{i}]") for i, p in enumerate(raw_prov))
        expires_at = ids.validate_timestamp(_require(obj, "expires_at", path=path), name=f"{path}.expires_at")
        extensions = ids.validate_extensions(_require(obj, "extensions", path=path))
        return cls(schema=schema, record_id=record_id, creation_result_record_id=creation_result_record_id,
                   creation_request_fingerprint=creation_request_fingerprint, proposal_id=proposal_id,
                   proposal_revision=proposal_revision, supersedes_revision=supersedes_revision, mode=mode,
                   author=author, created_at=created_at, summary=summary, rationale=rationale,
                   affected_concepts=tuple(affected), expected_snapshot=snapshot,
                   operations=tuple(sorted_ops), preview_ref=preview_ref, provenance=provenance,
                   expires_at=expires_at, extensions=extensions)


@dataclass(frozen=True)
class Withdrawal(_ClosedRecord):
    schema: int
    record_id: str
    withdrawal_result_record_id: str
    withdrawal_request_fingerprint: str
    proposal_id: str
    proposal_revision: int
    actor: str
    reason: str
    read_model_token: str
    withdrawn_at: str
    checksum: str

    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "record_id", "withdrawal_result_record_id",
        "withdrawal_request_fingerprint", "proposal_id", "proposal_revision",
        "actor", "reason", "read_model_token", "withdrawn_at", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "withdrawal", verify_checksum: bool = True) -> "Withdrawal":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        record_id = ids.validate_rec_id(_require(obj, "record_id", path=path))
        wrr = ids.validate_rec_id(_require(obj, "withdrawal_result_record_id", path=path))
        wrf = ids.validate_digest(_require(obj, "withdrawal_request_fingerprint", path=path), name=f"{path}.withdrawal_request_fingerprint")
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        rev = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        actor = ids.validate_actor(_require(obj, "actor", path=path), name=f"{path}.actor")
        reason = _bounded_str(_require(obj, "reason", path=path), "reason", lo=1, hi=MAX_WITHDRAWAL_REASON_CODEPOINTS, path=f"{path}.reason")
        rmt = ids.validate_digest(_require(obj, "read_model_token", path=path), name=f"{path}.read_model_token")
        withdrawn_at = ids.validate_timestamp(_require(obj, "withdrawn_at", path=path), name=f"{path}.withdrawn_at")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(schema=schema, record_id=record_id, withdrawal_result_record_id=wrr,
                   withdrawal_request_fingerprint=wrf, proposal_id=proposal_id, proposal_revision=rev,
                   actor=actor, reason=reason, read_model_token=rmt, withdrawn_at=withdrawn_at, checksum=checksum)


# ---------------------------------------------------------------------------
# section 5.3 Preview + Diff + Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConceptChange(_ClosedRecord):
    concept: str
    before_revision: str
    after_revision: str
    before_identity: ConceptIdentity
    before_base64: str
    after_base64: str
    contributing_ops: tuple[str, ...]
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "concept", "before_revision", "after_revision",
        "before_identity", "before_base64", "after_base64", "contributing_ops",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "concept_change") -> "ConceptChange":
        _reject_unknown(obj, cls.FIELDS, path=path)
        concept = ids.validate_concept_id(_require(obj, "concept", path=path), name=f"{path}.concept")
        before_revision = ids.validate_content_revision(_require(obj, "before_revision", path=path), name=f"{path}.before_revision")
        after_revision = ids.validate_content_revision(_require(obj, "after_revision", path=path), name=f"{path}.after_revision")
        before_identity = ConceptIdentity.from_mapping(
            _require(obj, "before_identity", path=path), path=f"{path}.before_identity")
        if before_identity.digest != before_revision:
            raise SchemaError("before_identity digest must equal before_revision",
                              path=f"{path}.before_identity.digest")
        before_b64 = _expect_str(_require(obj, "before_base64", path=path), "before_base64", path=f"{path}.before_base64")
        after_b64 = _expect_str(_require(obj, "after_base64", path=path), "after_base64", path=f"{path}.after_base64")
        for label, b64 in (("before_base64", before_b64), ("after_base64", after_b64)):
            decoded_len = _base64_decoded_len(b64, name=label, path=f"{path}.{label}")
            if decoded_len > MAX_CONCEPT_BYTES:
                raise LimitError(f"{label} decoded bytes exceed {MAX_CONCEPT_BYTES}", path=f"{path}.{label}")
        contributing = _sorted_unique_strings(_require(obj, "contributing_ops", path=path), "contributing_ops", path=f"{path}.contributing_ops")
        for c in contributing:
            ids.validate_op_id(c)
        return cls(concept=concept, before_revision=before_revision, after_revision=after_revision,
                   before_identity=before_identity, before_base64=before_b64,
                   after_base64=after_b64, contributing_ops=tuple(contributing))


def _base64_decoded_len(s: str, *, name: str, path: str) -> int:
    import base64
    if not s:
        return 0
    if len(s) % 4 != 0:
        raise PrimitiveError(f"{name} must be standard padded base64", path=path)
    try:
        decoded = base64.b64decode(s, validate=True)
    except Exception as exc:
        raise PrimitiveError(f"{name} is not valid base64: {exc}", path=path) from exc
    return len(decoded)


@dataclass(frozen=True)
class Diff(_ClosedRecord):
    schema: int
    format: str
    context_lines: int
    text: str
    digest: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({"schema", "format", "context_lines", "text", "digest"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "diff") -> "Diff":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        fmt = _expect_str(_require(obj, "format", path=path), "format", path=f"{path}.format")
        if fmt != "unified":
            raise SchemaError("format must be 'unified'", path=f"{path}.format")
        context_lines = _expect_int(_require(obj, "context_lines", path=path), "context_lines", path=f"{path}.context_lines")
        if context_lines != 3:
            raise SchemaError("context_lines must be 3", path=f"{path}.context_lines")
        text = _expect_str(_require(obj, "text", path=path), "text", path=f"{path}.text")
        if len(text.encode("utf-8")) > MAX_DIFF_BYTES:
            raise LimitError(f"text exceeds {MAX_DIFF_BYTES} canonical UTF-8 bytes", path=f"{path}.text")
        line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        if line_count > MAX_DIFF_LINES:
            raise LimitError(f"text exceeds {MAX_DIFF_LINES} lines", path=f"{path}.text")
        digest = ids.validate_digest(_require(obj, "digest", path=path), name=f"{path}.digest")
        body = {"schema": schema, "format": fmt, "context_lines": context_lines, "text": text}
        expected = canonical_digest(body)
        if digest != expected:
            raise IntegrityError(f"diff digest mismatch: expected {expected}, got {digest}", path=f"{path}.digest")
        return cls(schema=schema, format=fmt, context_lines=context_lines, text=text, digest=digest)


@dataclass(frozen=True)
class ValidationFinding(_ClosedRecord):
    code: str
    severity: str
    concept: str | None
    line: int
    message: str
    fingerprint: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({"code", "severity", "concept", "line", "message", "fingerprint"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "finding") -> "ValidationFinding":
        _reject_unknown(obj, cls.FIELDS, path=path)
        code = _expect_str(_require(obj, "code", path=path), "code", path=f"{path}.code")
        severity = _expect_str(_require(obj, "severity", path=path), "severity", path=f"{path}.severity")
        concept_value = _require(obj, "concept", path=path)
        concept = None if concept_value is None else ids.validate_concept_id(concept_value, name=f"{path}.concept")
        line = _expect_int(_require(obj, "line", path=path), "line", path=f"{path}.line")
        if line < 0:
            raise SchemaError("line must be non-negative", path=f"{path}.line")
        message = _expect_str(_require(obj, "message", path=path), "message", path=f"{path}.message")
        fingerprint = _expect_str(_require(obj, "fingerprint", path=path), "fingerprint", path=f"{path}.fingerprint")
        return cls(code=code, severity=severity, concept=concept, line=line, message=message, fingerprint=fingerprint)


def _parse_finding_array(obj: Mapping[str, Any], name: str, *, path: str, counter: list[int]) -> tuple[ValidationFinding, ...]:
    raw = _expect_list(_require(obj, name, path=path), name, path=f"{path}.{name}")
    counter[0] += len(raw)
    findings = tuple(ValidationFinding.from_mapping(f, path=f"{path}.{name}[{i}]") for i, f in enumerate(raw))
    key = lambda f: (0 if f.concept is None else 1, f.concept or "", f.line, f.severity, f.code, f.fingerprint)
    if list(findings) != sorted(findings, key=key):
        raise SchemaError(
            f"{name} must use null-safe bundle-first finding order",
            path=f"{path}.{name}",
        )
    return findings


@dataclass(frozen=True)
class Validation(_ClosedRecord):
    schema: int
    profile: str
    validation_context_digest: str
    before_findings: tuple[ValidationFinding, ...]
    after_findings: tuple[ValidationFinding, ...]
    added: tuple[ValidationFinding, ...]
    removed: tuple[ValidationFinding, ...]
    unchanged: tuple[ValidationFinding, ...]
    status: str
    digest: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "profile", "validation_context_digest", "before_findings", "after_findings",
        "added", "removed", "unchanged", "status", "digest",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "validation") -> "Validation":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        profile = _expect_str(_require(obj, "profile", path=path), "profile", path=f"{path}.profile")
        if not profile:
            raise SchemaError("profile must be the explicitly requested profile (no silent fallback)", path=f"{path}.profile")
        validation_context_digest = ids.validate_digest(
            _require(obj, "validation_context_digest", path=path), name=f"{path}.validation_context_digest"
        )
        counter = [0]
        before = _parse_finding_array(obj, "before_findings", path=path, counter=counter)
        after = _parse_finding_array(obj, "after_findings", path=path, counter=counter)
        added = _parse_finding_array(obj, "added", path=path, counter=counter)
        removed = _parse_finding_array(obj, "removed", path=path, counter=counter)
        unchanged = _parse_finding_array(obj, "unchanged", path=path, counter=counter)
        if counter[0] > MAX_VALIDATION_FINDINGS:
            raise LimitError(f"validation findings exceed {MAX_VALIDATION_FINDINGS}", path=path)
        status = _expect_str(_require(obj, "status", path=path), "status", path=f"{path}.status")
        if status not in VALIDATION_STATUS:
            raise SchemaError(f"status must be one of {sorted(VALIDATION_STATUS)}", path=f"{path}.status")
        digest = ids.validate_digest(_require(obj, "digest", path=path), name=f"{path}.digest")
        body = {
            "schema": schema, "profile": profile, "validation_context_digest": validation_context_digest,
            "before_findings": [f.to_mapping() for f in before],
            "after_findings": [f.to_mapping() for f in after],
            "added": [f.to_mapping() for f in added],
            "removed": [f.to_mapping() for f in removed],
            "unchanged": [f.to_mapping() for f in unchanged],
            "status": status,
        }
        expected = canonical_digest(body)
        if digest != expected:
            raise IntegrityError(f"validation digest mismatch: expected {expected}, got {digest}", path=f"{path}.digest")
        return cls(schema=schema, profile=profile, validation_context_digest=validation_context_digest,
                   before_findings=before, after_findings=after,
                   added=added, removed=removed, unchanged=unchanged, status=status, digest=digest)


@dataclass(frozen=True)
class Preview(_ClosedRecord):
    schema: int
    proposal_id: str
    proposal_revision: int
    snapshot_digest: str
    validation_context_digest: str
    validator_id: str
    serializer_id: str
    operation_runtime_id: str
    operation_order: tuple[str, ...]
    concept_changes: tuple[ConceptChange, ...]
    diff: Diff
    validation: Validation
    generated_at: str
    expires_at: str
    preview_digest: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "proposal_id", "proposal_revision", "snapshot_digest", "validation_context_digest",
        "validator_id", "serializer_id", "operation_runtime_id",
        "operation_order", "concept_changes", "diff", "validation",
        "generated_at", "expires_at", "preview_digest",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "preview") -> "Preview":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        proposal_revision = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        snapshot_digest = ids.validate_digest(_require(obj, "snapshot_digest", path=path), name=f"{path}.snapshot_digest")
        validation_context_digest = ids.validate_digest(
            _require(obj, "validation_context_digest", path=path), name=f"{path}.validation_context_digest"
        )
        validator_id = ids.validate_runtime_identity(_require(obj, "validator_id", path=path), name=f"{path}.validator_id")
        serializer_id = ids.validate_runtime_identity(_require(obj, "serializer_id", path=path), name=f"{path}.serializer_id")
        operation_runtime_id = ids.validate_runtime_identity(_require(obj, "operation_runtime_id", path=path), name=f"{path}.operation_runtime_id")
        # A canonical Kahn order is not necessarily globally lexical (a node
        # which becomes ready may sort before a node already emitted).  The
        # preview schema therefore preserves the producer's order and only
        # proves type/uniqueness here; staging owns graph/order verification.
        raw_order = _expect_list(_require(obj, "operation_order", path=path), "operation_order", path=f"{path}.operation_order")
        operation_order_list: list[str] = []
        seen_order: set[str] = set()
        for i, value in enumerate(raw_order):
            oid = _expect_str(value, "operation_order entry", path=f"{path}.operation_order[{i}]")
            ids.validate_op_id(oid)
            if oid in seen_order:
                raise SchemaError("operation_order must contain unique op ids", path=f"{path}.operation_order")
            seen_order.add(oid)
            operation_order_list.append(oid)
        operation_order = tuple(operation_order_list)
        raw_changes = _expect_list(_require(obj, "concept_changes", path=path), "concept_changes", path=f"{path}.concept_changes")
        changes = tuple(ConceptChange.from_mapping(c, path=f"{path}.concept_changes[{i}]") for i, c in enumerate(raw_changes))
        diff = Diff.from_mapping(_require(obj, "diff", path=path), path=f"{path}.diff")
        validation = Validation.from_mapping(_require(obj, "validation", path=path), path=f"{path}.validation")
        if validation.validation_context_digest != validation_context_digest:
            raise IntegrityError(
                "preview and validation validation_context_digest values differ",
                path=f"{path}.validation.validation_context_digest",
            )
        generated_at = ids.validate_timestamp(_require(obj, "generated_at", path=path), name=f"{path}.generated_at")
        expires_at = ids.validate_timestamp(_require(obj, "expires_at", path=path), name=f"{path}.expires_at")
        preview_digest = ids.validate_digest(_require(obj, "preview_digest", path=path), name=f"{path}.preview_digest")
        body = {k: v for k, v in dict(obj).items() if k != "preview_digest"}
        expected = canonical_digest(body)
        if preview_digest != expected:
            raise IntegrityError(f"preview_digest mismatch: expected {expected}, got {preview_digest}", path=f"{path}.preview_digest")
        rec = cls(schema=schema, proposal_id=proposal_id, proposal_revision=proposal_revision,
                   snapshot_digest=snapshot_digest, validation_context_digest=validation_context_digest,
                   validator_id=validator_id, serializer_id=serializer_id,
                  operation_runtime_id=operation_runtime_id, operation_order=operation_order,
                  concept_changes=changes, diff=diff, validation=validation,
                  generated_at=generated_at, expires_at=expires_at, preview_digest=preview_digest)
        if len(rec.canonical_bytes()) > MAX_PREVIEW_BYTES:
            raise LimitError(f"preview artifact exceeds {MAX_PREVIEW_BYTES} canonical bytes", path=path)
        return rec


# ---------------------------------------------------------------------------
# section 5.4 Decision row + decision-set token
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionRow(_ClosedRecord):
    schema: int
    record_id: str
    decision_result_record_id: str
    decision_request_fingerprint: str
    proposal_id: str
    proposal_revision: int
    op_id: str
    decision: str
    reason: str
    actor: str
    decided_at: str
    previous_row_token: str | None
    row_token: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "record_id", "decision_result_record_id",
        "decision_request_fingerprint", "proposal_id", "proposal_revision",
        "op_id", "decision", "reason", "actor", "decided_at",
        "previous_row_token", "row_token",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "decision") -> "DecisionRow":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        record_id = ids.validate_rec_id(_require(obj, "record_id", path=path))
        drr = ids.validate_rec_id(_require(obj, "decision_result_record_id", path=path))
        drf = ids.validate_digest(_require(obj, "decision_request_fingerprint", path=path), name=f"{path}.decision_request_fingerprint")
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        rev = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        op_id = ids.validate_op_id(_require(obj, "op_id", path=path))
        decision = _expect_str(_require(obj, "decision", path=path), "decision", path=f"{path}.decision")
        if decision not in DECISION_VALUES:
            raise SchemaError(f"decision must be one of {sorted(DECISION_VALUES)}", path=f"{path}.decision")
        reason = _expect_str(_require(obj, "reason", path=path), "reason", path=f"{path}.reason")
        if len(reason) > MAX_DECISION_REASON_CODEPOINTS:
            raise LimitError(f"reason must be 0..{MAX_DECISION_REASON_CODEPOINTS} code points", path=f"{path}.reason")
        actor = ids.validate_actor(_require(obj, "actor", path=path), name=f"{path}.actor")
        decided_at = ids.validate_timestamp(_require(obj, "decided_at", path=path), name=f"{path}.decided_at")
        prev_v = _require(obj, "previous_row_token", path=path)
        previous_row_token: str | None = None if prev_v is None else ids.validate_row_token(prev_v, name=f"{path}.previous_row_token")
        row_token = ids.validate_row_token(_require(obj, "row_token", path=path), name=f"{path}.row_token")
        return cls(schema=schema, record_id=record_id, decision_result_record_id=drr,
                   decision_request_fingerprint=drf, proposal_id=proposal_id, proposal_revision=rev,
                   op_id=op_id, decision=decision, reason=reason, actor=actor, decided_at=decided_at,
                   previous_row_token=previous_row_token, row_token=row_token)


def decision_set_token(op_id_to_row_token: list[tuple[str, str | None]]) -> str:
    """SHA-256 digest over canonical sorted tuples ``[op_id, row_token-or-null]``."""
    items = sorted(op_id_to_row_token, key=lambda t: t[0])
    for op_id, _ in items:
        ids.validate_op_id(op_id)
    canonical_items = [[op_id, tok] for op_id, tok in items]
    return canonical_digest(canonical_items)


# ---------------------------------------------------------------------------
# section 5.5 Conflict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Conflict(_ClosedRecord):
    schema: int
    code: str
    proposal_id: str
    proposal_revision: int
    op_id: str | None
    concept: str | None
    expected: str
    actual: str
    message: str
    detected_at: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "code", "proposal_id", "proposal_revision", "op_id",
        "concept", "expected", "actual", "message", "detected_at",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "conflict") -> "Conflict":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        code = _expect_str(_require(obj, "code", path=path), "code", path=f"{path}.code")
        if code not in CONFLICT_CODES:
            raise SchemaError(f"code must be one of {sorted(CONFLICT_CODES)}", path=f"{path}.code")
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        rev = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        op_v = _require(obj, "op_id", path=path)
        concept_v = _require(obj, "concept", path=path)
        if op_v is None and code not in _PROPOSAL_LEVEL_CONFLICT_CODES:
            raise SchemaError("op_id may be null only for proposal-level conflicts", path=f"{path}.op_id")
        if concept_v is None and code not in _PROPOSAL_LEVEL_CONFLICT_CODES:
            raise SchemaError("concept may be null only for proposal-level conflicts", path=f"{path}.concept")
        op_id = None if op_v is None else ids.validate_op_id(op_v)
        concept = None if concept_v is None else ids.validate_concept_id(concept_v, name=f"{path}.concept")
        # ``expected`` and ``actual`` are code-specific string authorities.  The
        # SPEC section-5.5 conflict object does not restrict them to a single
        # primitive: digest-valued codes (snapshot/capability/config_changed)
        # carry digests, revision-valued codes (read/write_revision_changed)
        # carry 64-hex content revisions, ``target_missing`` carries a revision
        # and the literal ``absent`` sentinel, ``preview_expired`` carries
        # timestamps, ``validation_stale`` carries runtime identities, and the
        # remaining codes carry bounded explanatory strings.  Accept any
        # non-empty string so projector outputs round-trip exactly.
        expected = _expect_str(_require(obj, "expected", path=path), "expected", path=f"{path}.expected")
        if not expected:
            raise SchemaError("expected must be a non-empty string", path=f"{path}.expected")
        actual = _expect_str(_require(obj, "actual", path=path), "actual", path=f"{path}.actual")
        if not actual:
            raise SchemaError("actual must be a non-empty string", path=f"{path}.actual")
        message = _expect_str(_require(obj, "message", path=path), "message", path=f"{path}.message")
        detected_at = ids.validate_timestamp(_require(obj, "detected_at", path=path), name=f"{path}.detected_at")
        return cls(schema=schema, code=code, proposal_id=proposal_id, proposal_revision=rev,
                   op_id=op_id, concept=concept, expected=expected, actual=actual,
                   message=message, detected_at=detected_at)


def sort_conflicts(conflicts: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Sort conflicts by ``(op_id-or-empty, concept-or-empty, code)`` (SPEC section 5.5)."""
    def key(c: Mapping[str, Any]) -> tuple[str, str, str]:
        return (c.get("op_id") or "", c.get("concept") or "", c.get("code") or "")
    return sorted((dict(c) for c in conflicts), key=key)


# ---------------------------------------------------------------------------
# section 5.6 Apply / undo requests, transaction result, WAL
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyRequest(_ClosedRecord):
    schema: int
    proposal_id: str
    proposal_revision: int
    actor: str
    snapshot_digest: str
    validation_context_digest: str
    decision_set_token: str
    read_model_token: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "proposal_id", "proposal_revision", "actor",
        "snapshot_digest", "validation_context_digest", "decision_set_token", "read_model_token",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "apply_request") -> "ApplyRequest":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        rev = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        actor = ids.validate_actor(_require(obj, "actor", path=path), name=f"{path}.actor")
        snapshot_digest = ids.validate_digest(_require(obj, "snapshot_digest", path=path), name=f"{path}.snapshot_digest")
        validation_context_digest = ids.validate_digest(
            _require(obj, "validation_context_digest", path=path), name=f"{path}.validation_context_digest"
        )
        dst = ids.validate_digest(_require(obj, "decision_set_token", path=path), name=f"{path}.decision_set_token")
        rmt = ids.validate_digest(_require(obj, "read_model_token", path=path), name=f"{path}.read_model_token")
        return cls(schema=schema, proposal_id=proposal_id, proposal_revision=rev, actor=actor,
                   snapshot_digest=snapshot_digest, validation_context_digest=validation_context_digest,
                   decision_set_token=dst, read_model_token=rmt)


@dataclass(frozen=True)
class UndoRequest(_ClosedRecord):
    schema: int
    proposal_id: str
    proposal_revision: int
    actor: str
    transaction_id: str
    undo_group_id: str
    read_model_token: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "proposal_id", "proposal_revision", "actor",
        "transaction_id", "undo_group_id", "read_model_token",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "undo_request") -> "UndoRequest":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        rev = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        actor = ids.validate_actor(_require(obj, "actor", path=path), name=f"{path}.actor")
        transaction_id = ids.validate_transaction_id_any(_require(obj, "transaction_id", path=path))
        undo_group_id = ids.validate_undo_id(_require(obj, "undo_group_id", path=path))
        rmt = ids.validate_digest(_require(obj, "read_model_token", path=path), name=f"{path}.read_model_token")
        return cls(schema=schema, proposal_id=proposal_id, proposal_revision=rev, actor=actor,
                   transaction_id=transaction_id, undo_group_id=undo_group_id, read_model_token=rmt)


@dataclass(frozen=True)
class TransactionResult(_ClosedRecord):
    schema: int
    operation: str
    transaction_id: str
    proposal_id: str
    proposal_revision: int
    status: str
    replayed: bool
    decision_set_token: str
    validation_context_digest: str
    validation_digest: str
    concept_revisions: dict[str, str]
    activity_ids: tuple[str, ...]
    undo_group_id: str | None
    completed_at: str
    undo_expires_at: str | None
    error: dict[str, Any] | None
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "operation", "transaction_id", "proposal_id",
        "proposal_revision", "status", "replayed", "decision_set_token",
        "validation_context_digest", "validation_digest", "concept_revisions", "activity_ids",
        "undo_group_id", "completed_at", "undo_expires_at", "error",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "transaction_result") -> "TransactionResult":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        operation = _expect_str(_require(obj, "operation", path=path), "operation", path=f"{path}.operation")
        if operation not in ("apply", "undo"):
            raise SchemaError("operation must be 'apply' or 'undo'", path=f"{path}.operation")
        transaction_id = _require(obj, "transaction_id", path=path)
        prefix = WAL_OPERATION_PREFIXES[operation]
        if not (isinstance(transaction_id, str) and transaction_id.startswith(prefix)):
            raise SchemaError(f"transaction_id must have prefix {prefix!r} for operation {operation!r}", path=f"{path}.transaction_id")
        ids.validate_record_id(transaction_id, prefix)
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        rev = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        status = _expect_str(_require(obj, "status", path=path), "status", path=f"{path}.status")
        expected_status = "applied" if operation == "apply" else "undone"
        if status != expected_status:
            raise SchemaError(f"status must be {expected_status!r} for operation {operation!r}", path=f"{path}.status")
        replayed = _expect_bool(_require(obj, "replayed", path=path), "replayed", path=f"{path}.replayed")
        dst = ids.validate_digest(_require(obj, "decision_set_token", path=path), name=f"{path}.decision_set_token")
        vcd = ids.validate_digest(_require(obj, "validation_context_digest", path=path), name=f"{path}.validation_context_digest")
        vd = ids.validate_digest(_require(obj, "validation_digest", path=path), name=f"{path}.validation_digest")
        raw_cr = _expect_mapping(_require(obj, "concept_revisions", path=path), "concept_revisions", path=f"{path}.concept_revisions")
        if not raw_cr:
            raise SchemaError("concept_revisions must be non-empty on success", path=f"{path}.concept_revisions")
        concept_revisions: dict[str, str] = {}
        for k, v in raw_cr.items():
            concept_revisions[ids.validate_concept_id(k, name=f"{path}.concept_revisions")] = ids.validate_content_revision(v, name=f"{path}.concept_revisions")
        activity_ids = tuple(_sorted_unique_strings(_require(obj, "activity_ids", path=path), "activity_ids", path=f"{path}.activity_ids"))
        if not activity_ids:
            raise SchemaError("activity_ids must be non-empty on success", path=f"{path}.activity_ids")
        for activity_id in activity_ids:
            ids.validate_record_id(activity_id, "act_")
        raw_ug = _require(obj, "undo_group_id", path=path)
        undo_group_id = None if raw_ug is None else ids.validate_undo_id(raw_ug)
        completed_at = ids.validate_timestamp(_require(obj, "completed_at", path=path), name=f"{path}.completed_at")
        raw_ue = _require(obj, "undo_expires_at", path=path)
        undo_expires_at = None if raw_ue is None else ids.validate_timestamp(raw_ue, name=f"{path}.undo_expires_at")
        raw_err = _require(obj, "error", path=path)
        if raw_err is not None:
            raise SchemaError("error must be null on success", path=f"{path}.error")
        return cls(schema=schema, operation=operation, transaction_id=transaction_id, proposal_id=proposal_id,
                   proposal_revision=rev, status=status, replayed=replayed, decision_set_token=dst,
                   validation_context_digest=vcd, validation_digest=vd,
                   concept_revisions=concept_revisions, activity_ids=activity_ids,
                   undo_group_id=undo_group_id, completed_at=completed_at, undo_expires_at=undo_expires_at, error=None)


@dataclass(frozen=True)
class DirIdentity(_ClosedRecord):
    relative_name: str
    device: int
    inode: int
    mode: int
    FIELDS: ClassVar[frozenset[str]] = frozenset({"relative_name", "device", "inode", "mode"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "dir_identity") -> "DirIdentity":
        _reject_unknown(obj, cls.FIELDS, path=path)
        relative_name = ids.validate_relative_path(_require(obj, "relative_name", path=path), name=f"{path}.relative_name")
        device = _expect_int(_require(obj, "device", path=path), "device", path=f"{path}.device")
        inode = _expect_int(_require(obj, "inode", path=path), "inode", path=f"{path}.inode")
        mode = _expect_int(_require(obj, "mode", path=path), "mode", path=f"{path}.mode")
        if mode < 0:
            raise SchemaError("mode must be non-negative", path=f"{path}.mode")
        return cls(relative_name=relative_name, device=device, inode=inode, mode=mode)


@dataclass(frozen=True)
class LeafIdentity(_ClosedRecord):
    relative_name: str
    device: int
    inode: int
    mode: int
    size: int
    digest: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({"relative_name", "device", "inode", "mode", "size", "digest"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "leaf_identity") -> "LeafIdentity":
        _reject_unknown(obj, cls.FIELDS, path=path)
        relative_name = ids.validate_relative_path(_require(obj, "relative_name", path=path), name=f"{path}.relative_name")
        device = _expect_int(_require(obj, "device", path=path), "device", path=f"{path}.device")
        inode = _expect_int(_require(obj, "inode", path=path), "inode", path=f"{path}.inode")
        mode = _expect_int(_require(obj, "mode", path=path), "mode", path=f"{path}.mode")
        if mode < 0:
            raise SchemaError("mode must be non-negative", path=f"{path}.mode")
        size = ids.validate_stored_byte_size(_require(obj, "size", path=path), name=f"{path}.size")
        digest = ids.validate_digest(_require(obj, "digest", path=path), name=f"{path}.digest")
        return cls(relative_name=relative_name, device=device, inode=inode, mode=mode, size=size, digest=digest)


@dataclass(frozen=True)
class TargetIdentity(_ClosedRecord):
    parent_device: int
    parent_inode: int
    leaf_device: int
    leaf_inode: int
    ctime_ns: int
    mode: int
    size: int
    digest: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "parent_device", "parent_inode", "leaf_device", "leaf_inode",
        "ctime_ns", "mode", "size", "digest",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "target_identity") -> "TargetIdentity":
        _reject_unknown(obj, cls.FIELDS, path=path)
        values = {name: _expect_int(_require(obj, name, path=path), name,
                                    path=f"{path}.{name}")
                  for name in ("parent_device", "parent_inode", "leaf_device",
                               "leaf_inode", "ctime_ns", "mode")}
        if any(value < 0 for value in values.values()):
            raise SchemaError("target identity integers must be non-negative", path=path)
        size = ids.validate_stored_byte_size(_require(obj, "size", path=path),
                                             name=f"{path}.size")
        digest = ids.validate_digest(_require(obj, "digest", path=path),
                                     name=f"{path}.digest")
        return cls(**values, size=size, digest=digest)


@dataclass(frozen=True)
class WalTarget(_ClosedRecord):
    concept: str
    before_digest: str
    staged_digest: str
    backup_digest: str
    restore_digest: str
    before_identity: TargetIdentity
    staged_identity: TargetIdentity
    resulting_identity: TargetIdentity | None
    stage_leaf: LeafIdentity
    backup_leaf: LeafIdentity
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "concept", "before_digest", "staged_digest", "backup_digest", "restore_digest",
        "before_identity", "staged_identity", "resulting_identity",
        "stage_leaf", "backup_leaf",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "target") -> "WalTarget":
        _reject_unknown(obj, cls.FIELDS, path=path)
        concept = ids.validate_concept_id(_require(obj, "concept", path=path), name=f"{path}.concept")
        before_digest = ids.validate_digest(_require(obj, "before_digest", path=path), name=f"{path}.before_digest")
        staged_digest = ids.validate_digest(_require(obj, "staged_digest", path=path), name=f"{path}.staged_digest")
        backup_digest = ids.validate_digest(_require(obj, "backup_digest", path=path), name=f"{path}.backup_digest")
        restore_digest = ids.validate_digest(_require(obj, "restore_digest", path=path), name=f"{path}.restore_digest")
        before_identity = TargetIdentity.from_mapping(
            _require(obj, "before_identity", path=path), path=f"{path}.before_identity")
        staged_identity = TargetIdentity.from_mapping(
            _require(obj, "staged_identity", path=path), path=f"{path}.staged_identity")
        raw_resulting = _require(obj, "resulting_identity", path=path)
        resulting_identity = None if raw_resulting is None else TargetIdentity.from_mapping(
            raw_resulting, path=f"{path}.resulting_identity")
        stage_leaf = LeafIdentity.from_mapping(_require(obj, "stage_leaf", path=path), path=f"{path}.stage_leaf")
        backup_leaf = LeafIdentity.from_mapping(_require(obj, "backup_leaf", path=path), path=f"{path}.backup_leaf")
        if before_identity.digest != before_digest or staged_identity.digest != staged_digest:
            raise SchemaError("target identity digest linkage mismatch", path=path)
        return cls(concept=concept, before_digest=before_digest, staged_digest=staged_digest,
                   backup_digest=backup_digest, restore_digest=restore_digest,
                   before_identity=before_identity, staged_identity=staged_identity,
                   resulting_identity=resulting_identity, stage_leaf=stage_leaf,
                   backup_leaf=backup_leaf)


def _is_bool_prefix(xs: list[bool], *, proper: bool = False, nonempty: bool = False) -> bool:
    if not all(isinstance(x, bool) for x in xs):
        return False
    n = len(xs)
    k = sum(1 for x in xs if x)
    if nonempty and k == 0:
        return False
    if proper and k >= n:
        return False
    return all(xs[:k]) and not any(xs[k:])


def _is_bool_suffix(xs: list[bool], *, nonempty: bool = False) -> bool:
    if not all(isinstance(x, bool) for x in xs):
        return False
    n = len(xs)
    k = sum(1 for x in xs if x)
    if nonempty and k == 0:
        return False
    return (not any(xs[: n - k])) and all(xs[n - k:])


def is_legal_wal_phase_bitmap(phase: str, commit_decided: bool, publish_bitmap: list[bool], rollback_bitmap: list[bool]) -> bool:
    """Closed phase/bitmap legality (SPEC section 5.6 table)."""
    if phase not in WAL_PHASES:
        return False
    n = len(publish_bitmap)
    if len(rollback_bitmap) != n:
        return False
    if phase == "prepared":
        return (not commit_decided) and not any(publish_bitmap) and not any(rollback_bitmap)
    if phase == "commit_decided":
        return commit_decided and not any(publish_bitmap) and not any(rollback_bitmap)
    if phase == "publishing":
        return (commit_decided and _is_bool_prefix(publish_bitmap, proper=True, nonempty=True) and not any(rollback_bitmap))
    if phase in ("content_published", "finalizing", "committed"):
        return commit_decided and bool(n > 0) and all(publish_bitmap) and not any(rollback_bitmap)
    if phase == "rolling_back":
        return (not commit_decided) and not any(publish_bitmap) and _is_bool_suffix(rollback_bitmap, nonempty=True)
    if phase == "rolled_back":
        return (not commit_decided) and bool(n > 0) and not any(publish_bitmap) and all(rollback_bitmap)
    # indeterminate: publish prefix, rollback suffix, disjoint
    if not _is_bool_prefix(publish_bitmap):
        return False
    if not _is_bool_suffix(rollback_bitmap):
        return False
    if any(p and r for p, r in zip(publish_bitmap, rollback_bitmap)):
        return False
    return True


@dataclass(frozen=True)
class WAL(_ClosedRecord):
    schema: int
    operation: str
    transaction_id: str
    proposal_id: str
    proposal_revision: int
    idempotency_scope: str
    idempotency_key_digest: str
    request_fingerprint: str
    transaction_intent_digest: str
    decision_set_token: str | None
    read_model_token: str
    preview_digest: str | None
    snapshot_digest: str
    validation_context_digest: str | None
    validator_id: str
    serializer_id: str
    operation_runtime_id: str
    accepted_op_ids: tuple[str, ...]
    targets: tuple[WalTarget, ...]
    stage_dir_identity: DirIdentity
    backup_dir_identity: DirIdentity
    phase: str
    commit_decided: bool
    publish_bitmap: tuple[bool, ...]
    rollback_bitmap: tuple[bool, ...]
    recovery_attempts: int
    created_at: str
    updated_at: str
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "operation", "transaction_id", "proposal_id", "proposal_revision",
        "idempotency_scope", "idempotency_key_digest", "request_fingerprint", "transaction_intent_digest",
        "decision_set_token", "read_model_token", "preview_digest", "snapshot_digest", "validation_context_digest",
        "validator_id", "serializer_id", "operation_runtime_id", "accepted_op_ids",
        "targets", "stage_dir_identity", "backup_dir_identity", "phase",
        "commit_decided", "publish_bitmap", "rollback_bitmap", "recovery_attempts",
        "created_at", "updated_at", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "wal", verify_checksum: bool = True) -> "WAL":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        operation = _expect_str(_require(obj, "operation", path=path), "operation", path=f"{path}.operation")
        if operation not in ("apply", "undo"):
            raise SchemaError("operation must be 'apply' or 'undo'", path=f"{path}.operation")
        transaction_id = _require(obj, "transaction_id", path=path)
        prefix = WAL_OPERATION_PREFIXES[operation]
        if not (isinstance(transaction_id, str) and transaction_id.startswith(prefix)):
            raise SchemaError(f"transaction_id must have prefix {prefix!r} for operation {operation!r}", path=f"{path}.transaction_id")
        ids.validate_record_id(transaction_id, prefix)
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        rev = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        idempotency_scope = _expect_str(_require(obj, "idempotency_scope", path=path), "idempotency_scope", path=f"{path}.idempotency_scope")
        ikd = ids.validate_digest(_require(obj, "idempotency_key_digest", path=path), name=f"{path}.idempotency_key_digest")
        rf = ids.validate_digest(_require(obj, "request_fingerprint", path=path), name=f"{path}.request_fingerprint")
        intent_digest = ids.validate_digest(_require(obj, "transaction_intent_digest", path=path),
                                            name=f"{path}.transaction_intent_digest")
        raw_dst = _require(obj, "decision_set_token", path=path)
        decision_set_token = None if raw_dst is None else ids.validate_digest(raw_dst, name=f"{path}.decision_set_token")
        rmt = ids.validate_digest(_require(obj, "read_model_token", path=path), name=f"{path}.read_model_token")
        raw_pd = _require(obj, "preview_digest", path=path)
        preview_digest = None if raw_pd is None else ids.validate_digest(raw_pd, name=f"{path}.preview_digest")
        snapshot_digest = ids.validate_digest(_require(obj, "snapshot_digest", path=path), name=f"{path}.snapshot_digest")
        raw_vcd = _require(obj, "validation_context_digest", path=path)
        validation_context_digest = None if raw_vcd is None else ids.validate_digest(
            raw_vcd, name=f"{path}.validation_context_digest"
        )
        if operation == "apply" and validation_context_digest is None:
            raise SchemaError("validation_context_digest is required for apply", path=f"{path}.validation_context_digest")
        if operation == "apply" and (decision_set_token is None or preview_digest is None):
            raise SchemaError("apply requires decision_set_token and preview_digest", path=path)
        if operation == "undo" and (decision_set_token is not None or preview_digest is not None):
            raise SchemaError("undo requires null decision_set_token and preview_digest", path=path)
        if operation == "undo" and validation_context_digest is None:
            raise SchemaError("validation_context_digest is required for undo validation", path=f"{path}.validation_context_digest")
        validator_id = ids.validate_runtime_identity(_require(obj, "validator_id", path=path), name=f"{path}.validator_id")
        serializer_id = ids.validate_runtime_identity(_require(obj, "serializer_id", path=path), name=f"{path}.serializer_id")
        operation_runtime_id = ids.validate_runtime_identity(_require(obj, "operation_runtime_id", path=path), name=f"{path}.operation_runtime_id")
        accepted_op_ids = tuple(_sorted_unique_strings(_require(obj, "accepted_op_ids", path=path), "accepted_op_ids", path=f"{path}.accepted_op_ids"))
        if operation == "apply" and not accepted_op_ids:
            raise SchemaError("accepted_op_ids must be non-empty for apply", path=f"{path}.accepted_op_ids")
        if operation == "undo" and accepted_op_ids:
            raise SchemaError("accepted_op_ids must be empty for undo", path=f"{path}.accepted_op_ids")
        for o in accepted_op_ids:
            ids.validate_op_id(o)
        raw_targets = _expect_list(_require(obj, "targets", path=path), "targets", path=f"{path}.targets")
        if not raw_targets:
            raise SchemaError("targets must be non-empty", path=f"{path}.targets")
        targets = tuple(WalTarget.from_mapping(t, path=f"{path}.targets[{i}]") for i, t in enumerate(raw_targets))
        if [t.concept for t in targets] != sorted(t.concept for t in targets):
            raise SchemaError("targets must be in canonical concept-id order", path=f"{path}.targets")
        if len({t.concept for t in targets}) != len(targets):
            raise SchemaError("target concepts must be unique", path=f"{path}.targets")
        if operation == "apply" and any(t.restore_digest != t.before_digest for t in targets):
            raise SchemaError("apply restore_digest must equal before_digest", path=f"{path}.targets")
        if operation == "undo" and any(t.restore_digest != t.staged_digest for t in targets):
            raise SchemaError("undo restore_digest must equal staged_digest", path=f"{path}.targets")
        stage_dir_identity = DirIdentity.from_mapping(_require(obj, "stage_dir_identity", path=path), path=f"{path}.stage_dir_identity")
        backup_dir_identity = DirIdentity.from_mapping(_require(obj, "backup_dir_identity", path=path), path=f"{path}.backup_dir_identity")
        if stage_dir_identity.relative_name != "stage" or backup_dir_identity.relative_name != "backup":
            raise SchemaError("transaction private directory names must be exactly stage and backup", path=path)
        phase = _expect_str(_require(obj, "phase", path=path), "phase", path=f"{path}.phase")
        commit_decided = _expect_bool(_require(obj, "commit_decided", path=path), "commit_decided", path=f"{path}.commit_decided")
        raw_pub = _expect_list(_require(obj, "publish_bitmap", path=path), "publish_bitmap", path=f"{path}.publish_bitmap")
        raw_rb = _expect_list(_require(obj, "rollback_bitmap", path=path), "rollback_bitmap", path=f"{path}.rollback_bitmap")
        publish_bitmap: list[bool] = []
        for i, b in enumerate(raw_pub):
            if not isinstance(b, bool):
                raise SchemaError("publish_bitmap entries must be booleans", path=f"{path}.publish_bitmap[{i}]")
            publish_bitmap.append(b)
        rollback_bitmap: list[bool] = []
        for i, b in enumerate(raw_rb):
            if not isinstance(b, bool):
                raise SchemaError("rollback_bitmap entries must be booleans", path=f"{path}.rollback_bitmap[{i}]")
            rollback_bitmap.append(b)
        if len(publish_bitmap) != len(targets):
            raise SchemaError("publish_bitmap length must equal target count", path=f"{path}.publish_bitmap")
        if len(rollback_bitmap) != len(targets):
            raise SchemaError("rollback_bitmap length must equal target count", path=f"{path}.rollback_bitmap")
        for index, (target, published) in enumerate(zip(targets, publish_bitmap)):
            if published != (target.resulting_identity is not None):
                raise IntegrityError("WAL resulting identity/publish bit mismatch",
                                     path=f"{path}.targets[{index}].resulting_identity")
        if not is_legal_wal_phase_bitmap(phase, commit_decided, publish_bitmap, rollback_bitmap):
            raise IntegrityError(f"illegal WAL phase/bitmap combination: phase={phase} cd={commit_decided} pub={publish_bitmap} rb={rollback_bitmap}", path=path)
        recovery_attempts = _expect_int(_require(obj, "recovery_attempts", path=path), "recovery_attempts", path=f"{path}.recovery_attempts")
        if recovery_attempts < 0 or recovery_attempts > 100:
            raise SchemaError("recovery_attempts must be in 0..100", path=f"{path}.recovery_attempts")
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        updated_at = ids.validate_timestamp(_require(obj, "updated_at", path=path), name=f"{path}.updated_at")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"WAL checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(schema=schema, operation=operation, transaction_id=transaction_id, proposal_id=proposal_id,
                   proposal_revision=rev, idempotency_scope=idempotency_scope, idempotency_key_digest=ikd,
                   request_fingerprint=rf, transaction_intent_digest=intent_digest,
                   decision_set_token=decision_set_token, read_model_token=rmt,
                   preview_digest=preview_digest, snapshot_digest=snapshot_digest,
                   validation_context_digest=validation_context_digest, validator_id=validator_id,
                   serializer_id=serializer_id, operation_runtime_id=operation_runtime_id,
                   accepted_op_ids=accepted_op_ids, targets=targets, stage_dir_identity=stage_dir_identity,
                   backup_dir_identity=backup_dir_identity, phase=phase, commit_decided=commit_decided,
                   publish_bitmap=tuple(publish_bitmap), rollback_bitmap=tuple(rollback_bitmap),
                   recovery_attempts=recovery_attempts, created_at=created_at, updated_at=updated_at, checksum=checksum)


# ---------------------------------------------------------------------------
# section 5.7 History manifest, Endpoint result, Tombstone, Outbox receipt,
#        Transaction audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryTarget(_ClosedRecord):
    concept: str
    before_digest: str
    after_digest: str
    before_identity: TargetIdentity
    staged_identity: TargetIdentity
    backup_identity: LeafIdentity
    FIELDS: ClassVar[frozenset[str]] = frozenset({"concept", "before_digest", "after_digest",
        "before_identity", "staged_identity", "backup_identity"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "history_target") -> "HistoryTarget":
        _reject_unknown(obj, cls.FIELDS, path=path)
        concept = ids.validate_concept_id(_require(obj, "concept", path=path), name=f"{path}.concept")
        before_digest = ids.validate_digest(_require(obj, "before_digest", path=path), name=f"{path}.before_digest")
        after_digest = ids.validate_digest(_require(obj, "after_digest", path=path), name=f"{path}.after_digest")
        before_identity = TargetIdentity.from_mapping(_require(obj, "before_identity", path=path),
                                                      path=f"{path}.before_identity")
        staged_identity = TargetIdentity.from_mapping(_require(obj, "staged_identity", path=path),
                                                      path=f"{path}.staged_identity")
        backup_identity = LeafIdentity.from_mapping(_require(obj, "backup_identity", path=path), path=f"{path}.backup_identity")
        if before_identity.digest != before_digest or staged_identity.digest != after_digest:
            raise SchemaError("history identity digest linkage mismatch", path=path)
        return cls(concept=concept, before_digest=before_digest, after_digest=after_digest,
                   before_identity=before_identity, staged_identity=staged_identity,
                   backup_identity=backup_identity)


def _check_txn_prefix(transaction_id: Any, operation: str, *, path: str) -> str:
    prefix = WAL_OPERATION_PREFIXES[operation]
    if not (isinstance(transaction_id, str) and transaction_id.startswith(prefix)):
        raise SchemaError(f"transaction_id must have prefix {prefix!r}", path=f"{path}.transaction_id")
    return ids.validate_record_id(transaction_id, prefix)


@dataclass(frozen=True)
class HistoryManifest(_ClosedRecord):
    schema: int
    transaction_id: str
    operation: str
    parent_transaction_id: str | None
    proposal_id: str
    proposal_revision: int
    undo_group_id: str
    targets: tuple[HistoryTarget, ...]
    created_at: str
    expires_at: str
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "transaction_id", "operation", "parent_transaction_id",
        "proposal_id", "proposal_revision", "undo_group_id", "targets",
        "created_at", "expires_at", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "history_manifest", verify_checksum: bool = True) -> "HistoryManifest":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        operation = _expect_str(_require(obj, "operation", path=path), "operation", path=f"{path}.operation")
        if operation not in ("apply", "undo"):
            raise SchemaError("operation must be 'apply' or 'undo'", path=f"{path}.operation")
        transaction_id = _check_txn_prefix(_require(obj, "transaction_id", path=path), operation, path=path)
        raw_pt = _require(obj, "parent_transaction_id", path=path)
        parent_transaction_id = None if raw_pt is None else ids.validate_txn_id(raw_pt)
        if operation == "apply" and parent_transaction_id is not None:
            raise SchemaError("apply history parent_transaction_id must be null", path=f"{path}.parent_transaction_id")
        if operation == "undo" and parent_transaction_id is None:
            raise SchemaError("undo history requires parent_transaction_id", path=f"{path}.parent_transaction_id")
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        rev = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        undo_group_id = ids.validate_undo_id(_require(obj, "undo_group_id", path=path))
        raw_targets = _expect_list(_require(obj, "targets", path=path), "targets", path=f"{path}.targets")
        targets = tuple(HistoryTarget.from_mapping(t, path=f"{path}.targets[{i}]") for i, t in enumerate(raw_targets))
        if not targets:
            raise SchemaError("history targets must be non-empty", path=f"{path}.targets")
        if [t.concept for t in targets] != sorted(t.concept for t in targets):
            raise SchemaError("targets must be in canonical concept-id order", path=f"{path}.targets")
        if len({t.concept for t in targets}) != len(targets):
            raise SchemaError("history target concepts must be unique", path=f"{path}.targets")
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        expires_at = ids.validate_timestamp(_require(obj, "expires_at", path=path), name=f"{path}.expires_at")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"history manifest checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(schema=schema, transaction_id=transaction_id, operation=operation, parent_transaction_id=parent_transaction_id,
                   proposal_id=proposal_id, proposal_revision=rev, undo_group_id=undo_group_id, targets=targets,
                   created_at=created_at, expires_at=expires_at, checksum=checksum)


@dataclass(frozen=True)
class EndpointResult(_ClosedRecord):
    schema: int
    record_id: str
    scope: str
    key_digest: str
    request_fingerprint: str
    state: str
    http_status: int | None
    response_digest: str | None
    response: dict[str, Any] | None
    transaction_id: str | None
    created_at: str
    expires_at: str
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "record_id", "scope", "key_digest", "request_fingerprint", "state",
        "http_status", "response_digest", "response", "transaction_id",
        "created_at", "expires_at", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "endpoint_result", verify_checksum: bool = True) -> "EndpointResult":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        record_id = ids.validate_rec_id(_require(obj, "record_id", path=path))
        scope = _expect_str(_require(obj, "scope", path=path), "scope", path=f"{path}.scope")
        key_digest = ids.validate_digest(_require(obj, "key_digest", path=path), name=f"{path}.key_digest")
        rf = ids.validate_digest(_require(obj, "request_fingerprint", path=path), name=f"{path}.request_fingerprint")
        state = _expect_str(_require(obj, "state", path=path), "state", path=f"{path}.state")
        if state not in ENDPOINT_RESULT_STATES:
            raise SchemaError(f"state must be one of {sorted(ENDPOINT_RESULT_STATES)}", path=f"{path}.state")
        raw_status = _require(obj, "http_status", path=path)
        raw_rd = _require(obj, "response_digest", path=path)
        raw_resp = _require(obj, "response", path=path)
        if state == "prepared":
            if raw_status is not None or raw_rd is not None or raw_resp is not None:
                raise SchemaError("prepared state requires null status/digest/response", path=path)
        elif state == "terminal":
            if not isinstance(raw_status, int) or isinstance(raw_status, bool):
                raise SchemaError("terminal state requires an integer http_status", path=f"{path}.http_status")
            if raw_resp is None:
                raise SchemaError("terminal state requires a response object", path=f"{path}.response")
            if not isinstance(raw_rd, str):
                raise SchemaError("terminal state requires a response_digest", path=f"{path}.response_digest")
        elif state == "indeterminate":
            if raw_status != 409:
                raise SchemaError("indeterminate state requires http_status 409", path=f"{path}.http_status")
            if not isinstance(raw_resp, dict):
                raise SchemaError("indeterminate state requires a response object", path=f"{path}.response")
            if not isinstance(raw_rd, str):
                raise SchemaError("indeterminate state requires a response_digest", path=f"{path}.response_digest")
        http_status = None if raw_status is None else (raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else None)
        response_digest = None if raw_rd is None else ids.validate_digest(raw_rd, name=f"{path}.response_digest")
        response = None if raw_resp is None else _expect_mapping(raw_resp, "response", path=f"{path}.response")
        if response is not None and response_digest != canonical_digest(response):
            raise IntegrityError(
                "endpoint result response_digest does not match response",
                path=f"{path}.response_digest",
            )
        raw_tid = _require(obj, "transaction_id", path=path)
        transaction_id = None if raw_tid is None else ids.validate_transaction_id_any(raw_tid)
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        expires_at = ids.validate_timestamp(_require(obj, "expires_at", path=path), name=f"{path}.expires_at")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"endpoint result checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(schema=schema, record_id=record_id, scope=scope, key_digest=key_digest, request_fingerprint=rf, state=state,
                   http_status=http_status, response_digest=response_digest, response=response,
                   transaction_id=transaction_id, created_at=created_at, expires_at=expires_at, checksum=checksum)


@dataclass(frozen=True)
class Tombstone(_ClosedRecord):
    schema: int
    result_record_id: str
    scope: str
    key_digest: str
    request_fingerprint: str
    outcome: str
    response_digest: str | None
    transaction_id: str | None
    created_at: str
    expires_at: str
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "result_record_id", "scope", "key_digest", "request_fingerprint", "outcome",
        "response_digest", "transaction_id", "created_at", "expires_at", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "tombstone", verify_checksum: bool = True) -> "Tombstone":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        result_record_id = ids.validate_rec_id(_require(obj, "result_record_id", path=path))
        scope = _expect_str(_require(obj, "scope", path=path), "scope", path=f"{path}.scope")
        key_digest = ids.validate_digest(_require(obj, "key_digest", path=path), name=f"{path}.key_digest")
        rf = ids.validate_digest(_require(obj, "request_fingerprint", path=path), name=f"{path}.request_fingerprint")
        outcome = _expect_str(_require(obj, "outcome", path=path), "outcome", path=f"{path}.outcome")
        if outcome not in TOMBSTONE_OUTCOMES:
            raise SchemaError(f"outcome must be one of {sorted(TOMBSTONE_OUTCOMES)}", path=f"{path}.outcome")
        raw_rd = _require(obj, "response_digest", path=path)
        if outcome == "terminal_compacted" and not isinstance(raw_rd, str):
            raise SchemaError("terminal_compacted outcome requires a response_digest", path=f"{path}.response_digest")
        response_digest = None if raw_rd is None else ids.validate_digest(raw_rd, name=f"{path}.response_digest")
        raw_tid = _require(obj, "transaction_id", path=path)
        transaction_id = None if raw_tid is None else ids.validate_transaction_id_any(raw_tid)
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        expires_at = ids.validate_timestamp(_require(obj, "expires_at", path=path), name=f"{path}.expires_at")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"tombstone checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(schema=schema, result_record_id=result_record_id, scope=scope, key_digest=key_digest, request_fingerprint=rf, outcome=outcome,
                   response_digest=response_digest, transaction_id=transaction_id, created_at=created_at,
                   expires_at=expires_at, checksum=checksum)


@dataclass(frozen=True)
class OutboxReceipt(_ClosedRecord):
    schema: int
    outbox_id: str
    aggregate_kind: str
    aggregate_id: str
    proposal_id: str
    proposal_revision: int
    event_type: str
    mutation_intent_id: str
    result_record_id: str
    payload: dict[str, Any]
    payload_digest: str
    state: str
    event_cursor: int | None
    attempts: int
    created_at: str
    updated_at: str
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "outbox_id", "aggregate_kind", "aggregate_id", "proposal_id",
        "proposal_revision", "event_type", "mutation_intent_id", "result_record_id", "payload",
        "payload_digest", "state", "event_cursor", "attempts", "created_at",
        "updated_at", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "outbox_receipt", verify_checksum: bool = True) -> "OutboxReceipt":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        outbox_id = ids.validate_out_id(_require(obj, "outbox_id", path=path))
        aggregate_kind = _expect_str(_require(obj, "aggregate_kind", path=path), "aggregate_kind", path=f"{path}.aggregate_kind")
        if aggregate_kind not in OUTBOX_AGGREGATE_KINDS:
            raise SchemaError(f"aggregate_kind must be one of {sorted(OUTBOX_AGGREGATE_KINDS)}", path=f"{path}.aggregate_kind")
        aggregate_id = _expect_str(_require(obj, "aggregate_id", path=path), "aggregate_id", path=f"{path}.aggregate_id")
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        proposal_revision = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        event_type = _expect_str(_require(obj, "event_type", path=path), "event_type", path=f"{path}.event_type")
        if event_type not in NOTIFICATION_EVENTS:
            raise SchemaError("unsupported proposal notification event", path=f"{path}.event_type")
        mutation_intent_id = ids.validate_record_id(_require(obj, "mutation_intent_id", path=path),
                                                   "svc_" if str(obj.get("mutation_intent_id", "")).startswith("svc_") else
                                                   ("undo_" if str(obj.get("mutation_intent_id", "")).startswith("undo_") else "txn_"))
        result_record_id = ids.validate_rec_id(_require(obj, "result_record_id", path=path))
        payload = _expect_mapping(_require(obj, "payload", path=path), "payload", path=f"{path}.payload")
        expected_payload_fields = {"schema", "proposal_id", "proposal_revision", "event", "read_model_token", "transaction_id"}
        if set(payload) != expected_payload_fields or payload.get("schema") != 1:
            raise SchemaError("outbox payload must be the closed proposal notification", path=f"{path}.payload")
        if (ids.validate_proposal_id(payload["proposal_id"]) != proposal_id
                or ids.validate_revision(payload["proposal_revision"]) != proposal_revision):
            raise SchemaError("outbox payload proposal identity mismatch", path=f"{path}.payload")
        ids.validate_digest(payload["read_model_token"], name=f"{path}.payload.read_model_token")
        if payload["transaction_id"] is not None:
            ids.validate_transaction_id_any(payload["transaction_id"])
        if aggregate_id != proposal_id or payload["event"] != event_type:
            raise SchemaError("outbox aggregate/event binding mismatch", path=path)
        payload_digest = ids.validate_digest(_require(obj, "payload_digest", path=path), name=f"{path}.payload_digest")
        if payload_digest != canonical_digest(payload):
            raise IntegrityError("outbox payload digest mismatch", path=f"{path}.payload_digest")
        state = _expect_str(_require(obj, "state", path=path), "state", path=f"{path}.state")
        if state not in OUTBOX_STATES:
            raise SchemaError(f"state must be one of {sorted(OUTBOX_STATES)}", path=f"{path}.state")
        raw_cursor = _require(obj, "event_cursor", path=path)
        if state == "appended":
            if not (isinstance(raw_cursor, int) and not isinstance(raw_cursor, bool)):
                raise SchemaError("appended state requires a non-null event_cursor", path=f"{path}.event_cursor")
        event_cursor = None if raw_cursor is None else _expect_int(raw_cursor, "event_cursor", path=f"{path}.event_cursor")
        attempts = _expect_int(_require(obj, "attempts", path=path), "attempts", path=f"{path}.attempts")
        if attempts < 0:
            raise SchemaError("attempts must be non-negative", path=f"{path}.attempts")
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        updated_at = ids.validate_timestamp(_require(obj, "updated_at", path=path), name=f"{path}.updated_at")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"outbox receipt checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(schema=schema, outbox_id=outbox_id, aggregate_kind=aggregate_kind, aggregate_id=aggregate_id,
                   proposal_id=proposal_id, proposal_revision=proposal_revision, event_type=event_type,
                   mutation_intent_id=mutation_intent_id, result_record_id=result_record_id, payload=payload,
                   payload_digest=payload_digest, state=state, event_cursor=event_cursor,
                   attempts=attempts, created_at=created_at, updated_at=updated_at, checksum=checksum)


SERVICE_INTENT_PHASES = ("prepared", "domain_appended", "outbox_appended",
                         "terminal", "committed", "indeterminate")
MAX_SERVICE_INTENT_BYTES = 41 * 1024 * 1024


@dataclass(frozen=True)
class ServiceMutationIntent(_ClosedRecord):
    """Exact metadata-publication WAL authority (SPEC §5.6A)."""

    schema: int
    intent_id: str
    operation: str
    scope: str
    key_digest: str
    request_fingerprint: str
    repository_id: str
    proposal_id: str
    proposal_revision: int
    op_id: str | None
    base_proposal_revision: int | None
    accepted_repository_snapshot_token: str
    accepted_read_model_token: str | None
    assigned: dict[str, Any]
    domain: dict[str, Any]
    notification_payload: dict[str, Any]
    outbox: OutboxReceipt
    terminal_http_status: int
    terminal_response: dict[str, Any]
    terminal_response_digest: str
    preview: dict[str, Any] | None
    phase: str
    recovery_attempts: int
    created_at: str
    updated_at: str
    checksum: str

    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "intent_id", "operation", "scope", "key_digest",
        "request_fingerprint", "repository_id", "proposal_id", "proposal_revision",
        "op_id", "base_proposal_revision", "accepted_repository_snapshot_token",
        "accepted_read_model_token", "assigned",
        "domain", "notification_payload", "outbox", "terminal_http_status",
        "terminal_response", "terminal_response_digest", "preview", "phase",
        "recovery_attempts", "created_at", "updated_at", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "service_intent",
                     verify_checksum: bool = True) -> "ServiceMutationIntent":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        intent_id = ids.validate_svc_id(_require(obj, "intent_id", path=path))
        operation = _expect_str(_require(obj, "operation", path=path), "operation", path=f"{path}.operation")
        if operation not in {"create", "supersede", "decision", "withdraw"}:
            raise SchemaError("unsupported service intent operation", path=f"{path}.operation")
        scope = _bounded_str(_require(obj, "scope", path=path), "scope", lo=1, hi=512, path=f"{path}.scope")
        key_digest = ids.validate_digest(_require(obj, "key_digest", path=path), name=f"{path}.key_digest")
        fingerprint = ids.validate_digest(_require(obj, "request_fingerprint", path=path), name=f"{path}.request_fingerprint")
        repository_id = ids.validate_repository_id(_require(obj, "repository_id", path=path))
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        proposal_revision = ids.validate_revision(_require(obj, "proposal_revision", path=path))
        raw_op = _require(obj, "op_id", path=path)
        op_id = None if raw_op is None else ids.validate_op_id(raw_op)
        if (operation == "decision") != (op_id is not None):
            raise SchemaError("op_id is non-null exactly for decision", path=f"{path}.op_id")
        raw_base = _require(obj, "base_proposal_revision", path=path)
        base = None if raw_base is None else ids.validate_revision(raw_base)
        if (operation == "create") != (base is None):
            raise SchemaError("base revision is null exactly for create", path=f"{path}.base_proposal_revision")
        accepted_repository_snapshot_token = ids.validate_digest(
            _require(obj, "accepted_repository_snapshot_token", path=path),
            name=f"{path}.accepted_repository_snapshot_token")
        raw_token = _require(obj, "accepted_read_model_token", path=path)
        accepted = None if raw_token is None else ids.validate_digest(raw_token)
        if (operation in {"decision", "withdraw"}) != (accepted is not None):
            raise SchemaError("accepted token is required exactly for decision/withdraw", path=f"{path}.accepted_read_model_token")

        assigned = _expect_mapping(_require(obj, "assigned", path=path), "assigned", path=f"{path}.assigned")
        _reject_unknown(assigned, frozenset({"record_id", "result_record_id", "outbox_id",
                                             "created_at", "domain_at", "expires_at"}), path=f"{path}.assigned")
        ids.validate_rec_id(_require(assigned, "record_id", path=f"{path}.assigned"))
        result_id = ids.validate_rec_id(_require(assigned, "result_record_id", path=f"{path}.assigned"))
        ids.validate_out_id(_require(assigned, "outbox_id", path=f"{path}.assigned"))
        for name in ("created_at", "domain_at", "expires_at"):
            ids.validate_timestamp(_require(assigned, name, path=f"{path}.assigned"), name=f"{path}.assigned.{name}")

        domain = _expect_mapping(_require(obj, "domain", path=path), "domain", path=f"{path}.domain")
        _reject_unknown(domain, frozenset({"aggregate", "kind", "row", "row_digest"}), path=f"{path}.domain")
        pair = (domain.get("aggregate"), domain.get("kind"))
        expected_pair = (("decisions", "decision") if operation == "decision" else
                         ("proposals", "withdrawal") if operation == "withdraw" else
                         ("proposals", "proposal"))
        if pair != expected_pair:
            raise SchemaError("service intent domain aggregate/kind mismatch", path=f"{path}.domain")
        row = _expect_mapping(_require(domain, "row", path=f"{path}.domain"), "row", path=f"{path}.domain.row")
        parser = DecisionRow if operation == "decision" else Withdrawal if operation == "withdraw" else ProposalRevision
        parsed_row = parser.from_mapping(row, path=f"{path}.domain.row")
        row_digest = ids.validate_digest(_require(domain, "row_digest", path=f"{path}.domain"))
        if row_digest != canonical_digest(row):
            raise IntegrityError("service intent domain row digest mismatch", path=f"{path}.domain.row_digest")
        if getattr(parsed_row, "record_id") != assigned["record_id"]:
            raise SchemaError("service intent assigned/domain record mismatch", path=f"{path}.domain.row")

        notification = _expect_mapping(_require(obj, "notification_payload", path=path), "notification_payload", path=f"{path}.notification_payload")
        outbox = OutboxReceipt.from_mapping(_require(obj, "outbox", path=path), path=f"{path}.outbox")
        if (dict(outbox.payload) != notification or outbox.mutation_intent_id != intent_id
                or outbox.result_record_id != result_id or outbox.proposal_id != proposal_id
                or outbox.proposal_revision != proposal_revision):
            raise SchemaError("service intent outbox binding mismatch", path=f"{path}.outbox")
        status = _expect_int(_require(obj, "terminal_http_status", path=path), "terminal_http_status", path=f"{path}.terminal_http_status")
        if not 100 <= status <= 599:
            raise SchemaError("terminal_http_status must be 100..599", path=f"{path}.terminal_http_status")
        response = _expect_mapping(_require(obj, "terminal_response", path=path), "terminal_response", path=f"{path}.terminal_response")
        response_digest = ids.validate_digest(_require(obj, "terminal_response_digest", path=path))
        if response_digest != canonical_digest(response):
            raise IntegrityError("service intent terminal response digest mismatch", path=f"{path}.terminal_response_digest")
        raw_preview = _require(obj, "preview", path=path)
        preview = None if raw_preview is None else _expect_mapping(raw_preview, "preview", path=f"{path}.preview")
        if operation in {"create", "supersede"}:
            if preview is None or set(preview) != {"staged_ref", "promoted_ref"}:
                raise SchemaError("create/supersede intent requires closed preview refs", path=f"{path}.preview")
            staged_ref = _expect_mapping(preview["staged_ref"], "staged_ref", path=f"{path}.preview.staged_ref")
            if set(staged_ref) != {"relative_path", "preview_digest", "artifact_bytes"}:
                raise SchemaError("staged_ref is not closed", path=f"{path}.preview.staged_ref")
            ids.validate_relative_path(staged_ref["relative_path"])
            ids.validate_digest(staged_ref["preview_digest"])
            ids.validate_stored_byte_size(staged_ref["artifact_bytes"], limit=MAX_PREVIEW_BYTES)
            promoted = PreviewRef.from_mapping(preview["promoted_ref"], path=f"{path}.preview.promoted_ref")
            if (promoted.preview_digest != staged_ref["preview_digest"]
                    or promoted.artifact_bytes != staged_ref["artifact_bytes"]):
                raise SchemaError("staged/promoted preview refs differ", path=f"{path}.preview")
        elif preview is not None:
            raise SchemaError("decision/withdraw preview must be null", path=f"{path}.preview")
        phase = _expect_str(_require(obj, "phase", path=path), "phase", path=f"{path}.phase")
        if phase not in SERVICE_INTENT_PHASES:
            raise SchemaError("unsupported service intent phase", path=f"{path}.phase")
        attempts = _expect_int(_require(obj, "recovery_attempts", path=path), "recovery_attempts", path=f"{path}.recovery_attempts")
        if not 0 <= attempts <= 100:
            raise SchemaError("recovery_attempts must be 0..100", path=f"{path}.recovery_attempts")
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path))
        updated_at = ids.validate_timestamp(_require(obj, "updated_at", path=path))
        if created_at != assigned["created_at"] or updated_at < created_at:
            raise SchemaError("service intent timestamps are incoherent", path=path)
        checksum = ids.validate_digest(_require(obj, "checksum", path=path))
        if verify_checksum and checksum != compute_checksum(dict(obj)):
            raise IntegrityError("service intent checksum mismatch", path=f"{path}.checksum")
        rec = cls(schema, intent_id, operation, scope, key_digest, fingerprint, repository_id,
                  proposal_id, proposal_revision, op_id, base, accepted_repository_snapshot_token,
                  accepted, assigned, domain,
                  notification, outbox, status, response, response_digest, preview, phase,
                  attempts, created_at, updated_at, checksum)
        if len(rec.canonical_bytes()) > MAX_SERVICE_INTENT_BYTES:
            raise LimitError("service intent exceeds 41 MiB", path=path)
        return rec


def _parse_concept_revision_map(obj: Mapping[str, Any], name: str, *, path: str) -> dict[str, str]:
    raw = _expect_mapping(_require(obj, name, path=path), name, path=f"{path}.{name}")
    out: dict[str, str] = {}
    for k, v in raw.items():
        out[ids.validate_concept_id(k, name=f"{path}.{name}")] = ids.validate_content_revision(v, name=f"{path}.{name}")
    return out


@dataclass(frozen=True)
class TransactionAudit(_ClosedRecord):
    schema: int
    record_id: str
    operation: str
    transaction_id: str
    parent_transaction_id: str | None
    proposal_id: str
    proposal_revision: int
    actor: str
    endpoint_scope: str
    idempotency_key_digest: str
    request_fingerprint: str
    transaction_intent_digest: str
    accepted_op_ids: tuple[str, ...]
    decision_record_ids: tuple[str, ...]
    decision_rows_digest: str
    decision_set_token: str
    preview_digest: str
    snapshot_digest: str
    validation_context_digest: str
    validator_id: str
    serializer_id: str
    operation_runtime_id: str
    validation_profile: str
    validation_digest: str
    validation_status: str
    phase: str
    status: str
    history_manifest_digest: str
    target_before_revisions: dict[str, str]
    target_after_revisions: dict[str, str]
    target_before_identities: dict[str, TargetIdentity]
    target_resulting_identities: dict[str, TargetIdentity]
    target_backup_identities: dict[str, LeafIdentity]
    resulting_revisions: dict[str, str]
    activity_ids: tuple[str, ...]
    undo_group_id: str | None
    outbox_ids: tuple[str, ...]
    started_at: str
    commit_decided_at: str | None
    completed_at: str
    error_digest: str | None
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "record_id", "operation", "transaction_id",
        "parent_transaction_id", "proposal_id", "proposal_revision", "actor",
          "endpoint_scope", "idempotency_key_digest", "request_fingerprint", "transaction_intent_digest",
          "accepted_op_ids", "decision_record_ids", "decision_rows_digest", "decision_set_token",
        "preview_digest", "snapshot_digest", "validation_context_digest", "validator_id", "serializer_id",
        "operation_runtime_id", "validation_profile", "validation_digest",
        "validation_status", "phase", "status", "history_manifest_digest",
          "target_before_revisions", "target_after_revisions",
          "target_before_identities", "target_resulting_identities",
          "target_backup_identities", "resulting_revisions", "activity_ids",
        "undo_group_id", "outbox_ids", "started_at", "commit_decided_at",
        "completed_at", "error_digest", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "transaction_audit", verify_checksum: bool = True) -> "TransactionAudit":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        record_id = ids.validate_rec_id(_require(obj, "record_id", path=path))
        operation = _expect_str(_require(obj, "operation", path=path), "operation", path=f"{path}.operation")
        if operation not in ("apply", "undo"):
            raise SchemaError("operation must be 'apply' or 'undo'", path=f"{path}.operation")
        transaction_id = _check_txn_prefix(_require(obj, "transaction_id", path=path), operation, path=path)
        raw_pt = _require(obj, "parent_transaction_id", path=path)
        if operation == "apply":
            if raw_pt is not None:
                raise SchemaError("parent_transaction_id must be null for apply", path=f"{path}.parent_transaction_id")
            parent_transaction_id = None
        else:
            if raw_pt is None:
                raise SchemaError("parent_transaction_id must reference the applied transaction for undo", path=f"{path}.parent_transaction_id")
            parent_transaction_id = ids.validate_txn_id(raw_pt)
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        rev = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        actor = ids.validate_actor(_require(obj, "actor", path=path), name=f"{path}.actor")
        endpoint_scope = _expect_str(_require(obj, "endpoint_scope", path=path), "endpoint_scope", path=f"{path}.endpoint_scope")
        ikd = ids.validate_digest(_require(obj, "idempotency_key_digest", path=path), name=f"{path}.idempotency_key_digest")
        rf = ids.validate_digest(_require(obj, "request_fingerprint", path=path), name=f"{path}.request_fingerprint")
        intent_digest = ids.validate_digest(_require(obj, "transaction_intent_digest", path=path),
                                            name=f"{path}.transaction_intent_digest")
        accepted_op_ids = tuple(_sorted_unique_strings(
            _require(obj, "accepted_op_ids", path=path), "accepted_op_ids",
            path=f"{path}.accepted_op_ids"))
        for op_id in accepted_op_ids:
            ids.validate_op_id(op_id)
        if operation == "apply" and not accepted_op_ids:
            raise SchemaError("apply audit requires accepted_op_ids", path=f"{path}.accepted_op_ids")
        if operation == "undo" and accepted_op_ids:
            raise SchemaError("undo audit accepted_op_ids must be empty", path=f"{path}.accepted_op_ids")
        decision_record_ids = tuple(_sorted_unique_strings(_require(obj, "decision_record_ids", path=path), "decision_record_ids", path=f"{path}.decision_record_ids"))
        for d in decision_record_ids:
            ids.validate_rec_id(d)
        if operation == "undo" and decision_record_ids:
            raise SchemaError("decision_record_ids must be empty for undo", path=f"{path}.decision_record_ids")
        drd = ids.validate_digest(_require(obj, "decision_rows_digest", path=path), name=f"{path}.decision_rows_digest")
        dst = ids.validate_digest(_require(obj, "decision_set_token", path=path), name=f"{path}.decision_set_token")
        preview_digest = ids.validate_digest(_require(obj, "preview_digest", path=path), name=f"{path}.preview_digest")
        snapshot_digest = ids.validate_digest(_require(obj, "snapshot_digest", path=path), name=f"{path}.snapshot_digest")
        validation_context_digest = ids.validate_digest(
            _require(obj, "validation_context_digest", path=path), name=f"{path}.validation_context_digest"
        )
        validator_id = ids.validate_runtime_identity(_require(obj, "validator_id", path=path), name=f"{path}.validator_id")
        serializer_id = ids.validate_runtime_identity(_require(obj, "serializer_id", path=path), name=f"{path}.serializer_id")
        operation_runtime_id = ids.validate_runtime_identity(_require(obj, "operation_runtime_id", path=path), name=f"{path}.operation_runtime_id")
        validation_profile = _expect_str(_require(obj, "validation_profile", path=path), "validation_profile", path=f"{path}.validation_profile")
        validation_digest = ids.validate_digest(_require(obj, "validation_digest", path=path), name=f"{path}.validation_digest")
        validation_status = _expect_str(_require(obj, "validation_status", path=path), "validation_status", path=f"{path}.validation_status")
        if validation_status not in VALIDATION_STATUS:
            raise SchemaError(f"validation_status must be one of {sorted(VALIDATION_STATUS)}", path=f"{path}.validation_status")
        phase = _expect_str(_require(obj, "phase", path=path), "phase", path=f"{path}.phase")
        if phase not in WAL_PHASES:
            raise SchemaError(f"phase must be one of {sorted(WAL_PHASES)}", path=f"{path}.phase")
        status = _expect_str(_require(obj, "status", path=path), "status", path=f"{path}.status")
        if status not in AUDIT_STATUS:
            raise SchemaError(f"status must be one of {sorted(AUDIT_STATUS)}", path=f"{path}.status")
        history_manifest_digest = ids.validate_digest(_require(obj, "history_manifest_digest", path=path), name=f"{path}.history_manifest_digest")
        target_before_revisions = _parse_concept_revision_map(obj, "target_before_revisions", path=path)
        target_after_revisions = _parse_concept_revision_map(obj, "target_after_revisions", path=path)
        resulting_revisions = _parse_concept_revision_map(obj, "resulting_revisions", path=path)
        def parse_target_identities(name: str) -> dict[str, TargetIdentity]:
            raw = _expect_mapping(_require(obj, name, path=path), name, path=f"{path}.{name}")
            return {ids.validate_concept_id(k, name=f"{path}.{name}"):
                    TargetIdentity.from_mapping(v, path=f"{path}.{name}[{k}]")
                    for k, v in raw.items()}
        target_before_identities = parse_target_identities("target_before_identities")
        target_resulting_identities = parse_target_identities("target_resulting_identities")
        raw_backup = _expect_mapping(_require(obj, "target_backup_identities", path=path), "target_backup_identities", path=f"{path}.target_backup_identities")
        target_backup_identities: dict[str, LeafIdentity] = {}
        for k, v in raw_backup.items():
            target_backup_identities[ids.validate_concept_id(k, name=f"{path}.target_backup_identities")] = LeafIdentity.from_mapping(v, path=f"{path}.target_backup_identities[{k}]")
        activity_ids = tuple(_sorted_unique_strings(_require(obj, "activity_ids", path=path), "activity_ids", path=f"{path}.activity_ids"))
        for activity_id in activity_ids:
            ids.validate_record_id(activity_id, "act_")
        raw_ug = _require(obj, "undo_group_id", path=path)
        undo_group_id = None if raw_ug is None else ids.validate_undo_id(raw_ug)
        outbox_ids = tuple(_sorted_unique_strings(_require(obj, "outbox_ids", path=path), "outbox_ids", path=f"{path}.outbox_ids"))
        for o in outbox_ids:
            ids.validate_out_id(o)
        started_at = ids.validate_timestamp(_require(obj, "started_at", path=path), name=f"{path}.started_at")
        raw_commit_at = _require(obj, "commit_decided_at", path=path)
        commit_decided_at = None if raw_commit_at is None else ids.validate_timestamp(
            raw_commit_at, name=f"{path}.commit_decided_at")
        if phase in {"committed", "finalizing", "content_published", "publishing", "commit_decided"} and commit_decided_at is None:
            raise SchemaError("forward transaction audit requires commit_decided_at", path=f"{path}.commit_decided_at")
        if phase in {"rolling_back", "rolled_back"} and commit_decided_at is not None:
            raise SchemaError("rollback transaction audit requires null commit_decided_at", path=f"{path}.commit_decided_at")
        completed_at = ids.validate_timestamp(_require(obj, "completed_at", path=path), name=f"{path}.completed_at")
        raw_ed = _require(obj, "error_digest", path=path)
        error_digest = None if raw_ed is None else ids.validate_digest(raw_ed, name=f"{path}.error_digest")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"transaction audit checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(schema=schema, record_id=record_id, operation=operation, transaction_id=transaction_id,
                   parent_transaction_id=parent_transaction_id, proposal_id=proposal_id, proposal_revision=rev,
                   actor=actor, endpoint_scope=endpoint_scope, idempotency_key_digest=ikd,
                     request_fingerprint=rf, transaction_intent_digest=intent_digest,
                     accepted_op_ids=accepted_op_ids,
                     decision_record_ids=decision_record_ids, decision_rows_digest=drd, decision_set_token=dst,
                   preview_digest=preview_digest, snapshot_digest=snapshot_digest,
                   validation_context_digest=validation_context_digest, validator_id=validator_id,
                   serializer_id=serializer_id, operation_runtime_id=operation_runtime_id,
                   validation_profile=validation_profile, validation_digest=validation_digest,
                   validation_status=validation_status, phase=phase, status=status,
                   history_manifest_digest=history_manifest_digest,
                     target_before_revisions=target_before_revisions,
                     target_after_revisions=target_after_revisions,
                     target_before_identities=target_before_identities,
                     target_resulting_identities=target_resulting_identities,
                     target_backup_identities=target_backup_identities,
                   resulting_revisions=resulting_revisions, activity_ids=activity_ids,
                   undo_group_id=undo_group_id, outbox_ids=outbox_ids, started_at=started_at,
                   commit_decided_at=commit_decided_at, completed_at=completed_at,
                   error_digest=error_digest, checksum=checksum)


# ---------------------------------------------------------------------------
# section 5.7 Checkpoint, Wrapper, RepositoryRoot, CursorKey
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Checkpoint(_ClosedRecord):
    schema: int
    repository_id: str
    kind: str
    generation: int
    sequence_high_water: int
    covered_generations: tuple[int, ...]
    records_digest: str
    records: tuple[dict[str, Any], ...]
    created_at: str
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "repository_id", "kind", "generation", "sequence_high_water",
        "covered_generations", "records_digest", "records", "created_at", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "checkpoint", verify_checksum: bool = True) -> "Checkpoint":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        repository_id = ids.validate_repository_id(_require(obj, "repository_id", path=path))
        kind = _expect_str(_require(obj, "kind", path=path), "kind", path=f"{path}.kind")
        if kind not in CHECKPOINT_KINDS:
            raise SchemaError(f"kind must be one of {sorted(CHECKPOINT_KINDS)}", path=f"{path}.kind")
        generation = _expect_int(_require(obj, "generation", path=path), "generation", path=f"{path}.generation")
        if generation < 0:
            raise SchemaError("generation must be a non-negative integer", path=f"{path}.generation")
        shw = _expect_int(_require(obj, "sequence_high_water", path=path), "sequence_high_water", path=f"{path}.sequence_high_water")
        if shw < 0:
            raise SchemaError("sequence_high_water must be non-negative", path=f"{path}.sequence_high_water")
        raw_cg = _expect_list(_require(obj, "covered_generations", path=path), "covered_generations", path=f"{path}.covered_generations")
        covered: list[int] = []
        for g in raw_cg:
            if isinstance(g, bool) or not isinstance(g, int):
                raise SchemaError("covered_generations entries must be integers", path=f"{path}.covered_generations")
            covered.append(g)
        if covered != sorted(covered):
            raise SchemaError("covered_generations must be sorted ascending", path=f"{path}.covered_generations")
        if any(g < 0 for g in covered):
            raise SchemaError("covered_generations entries must be non-negative", path=f"{path}.covered_generations")
        if len(set(covered)) != len(covered):
            raise SchemaError("covered_generations must be unique", path=f"{path}.covered_generations")
        records_digest = ids.validate_digest(_require(obj, "records_digest", path=path), name=f"{path}.records_digest")
        raw_records = _expect_list(_require(obj, "records", path=path), "records", path=f"{path}.records")
        if len(raw_records) != shw:
            raise SchemaError("records must be the exact sequence_high_water prefix", path=f"{path}.records")
        expected_rd = canonical_digest(raw_records)
        if records_digest != expected_rd:
            raise IntegrityError(f"records_digest mismatch: expected {expected_rd}, got {records_digest}", path=f"{path}.records_digest")
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"checkpoint checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        rec = cls(schema=schema, repository_id=repository_id, kind=kind, generation=generation,
                  sequence_high_water=shw, covered_generations=tuple(covered),
                  records_digest=records_digest, records=tuple(raw_records),
                  created_at=created_at, checksum=checksum)
        if len(rec.canonical_bytes()) > MAX_CHECKPOINT_BYTES:
            raise LimitError(f"checkpoint exceeds {MAX_CHECKPOINT_BYTES} canonical bytes", path=path)
        return rec


@dataclass(frozen=True)
class Wrapper(_ClosedRecord):
    record_schema: int
    kind: str
    sequence: int
    payload: dict[str, Any]
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({"record_schema", "kind", "sequence", "payload", "checksum"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "wrapper", verify_checksum: bool = True) -> "Wrapper":
        _reject_unknown(obj, cls.FIELDS, path=path)
        record_schema = _schema_int(_require(obj, "record_schema", path=path), expected=1, path=f"{path}.record_schema", name="record_schema")
        kind = _expect_str(_require(obj, "kind", path=path), "kind", path=f"{path}.kind")
        if kind not in WRAPPER_KINDS:
            raise SchemaError(f"kind must be one of {sorted(WRAPPER_KINDS)}", path=f"{path}.kind")
        sequence = _expect_int(_require(obj, "sequence", path=path), "sequence", path=f"{path}.sequence")
        if sequence < 0:
            raise SchemaError("sequence must be a non-negative integer", path=f"{path}.sequence")
        payload = _expect_mapping(_require(obj, "payload", path=path), "payload", path=f"{path}.payload")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"wrapper checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(record_schema=record_schema, kind=kind, sequence=sequence, payload=payload, checksum=checksum)


@dataclass(frozen=True)
class CursorKey(_ClosedRecord):
    key_id: str
    secret_base64url: str
    created_at: str
    not_before: str
    expires_at: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({"key_id", "secret_base64url", "created_at", "not_before", "expires_at"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "cursor_key") -> "CursorKey":
        _reject_unknown(obj, cls.FIELDS, path=path)
        key_id = ids.validate_cur_id(_require(obj, "key_id", path=path))
        secret = _expect_str(_require(obj, "secret_base64url", path=path), "secret_base64url", path=f"{path}.secret_base64url")
        import base64
        pad = "=" * (-len(secret) % 4)
        try:
            decoded = base64.urlsafe_b64decode(secret + pad)
        except Exception as exc:
            raise PrimitiveError(f"cursor key secret is not valid base64url: {exc}", path=f"{path}.secret_base64url") from exc
        if len(decoded) != 32:
            raise PrimitiveError(f"cursor key secret must decode to exactly 32 bytes (got {len(decoded)})", path=f"{path}.secret_base64url")
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        not_before = ids.validate_timestamp(_require(obj, "not_before", path=path), name=f"{path}.not_before")
        expires_at = ids.validate_timestamp(_require(obj, "expires_at", path=path), name=f"{path}.expires_at")
        if not_before >= expires_at:
            raise SchemaError("not_before must be < expires_at", path=f"{path}.not_before")
        return cls(key_id=key_id, secret_base64url=secret, created_at=created_at, not_before=not_before, expires_at=expires_at)


@dataclass(frozen=True)
class RepositoryRoot(_ClosedRecord):
    schema: int
    repository_id: str
    created_at: str
    aggregate_versions: dict[str, int]
    cursor_keys: tuple[CursorKey, ...]
    active_cursor_key_id: str
    limits_digest: str
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "repository_id", "created_at", "aggregate_versions",
        "cursor_keys", "active_cursor_key_id", "limits_digest", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "repository_root", verify_checksum: bool = True) -> "RepositoryRoot":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        repository_id = ids.validate_repository_id(_require(obj, "repository_id", path=path))
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        raw_av = _expect_mapping(_require(obj, "aggregate_versions", path=path), "aggregate_versions", path=f"{path}.aggregate_versions")
        expected_keys = sorted(AGGREGATE_KINDS)
        if sorted(raw_av.keys()) != expected_keys:
            raise SchemaError(f"aggregate_versions must have exactly the keys {expected_keys}", path=f"{path}.aggregate_versions")
        aggregate_versions: dict[str, int] = {}
        for k in expected_keys:
            v = raw_av[k]
            if isinstance(v, bool) or not isinstance(v, int):
                raise SchemaError(f"aggregate_versions[{k}] must be an integer", path=f"{path}.aggregate_versions")
            if v != 1:
                raise SchemaError(f"aggregate_versions[{k}] must be 1 in v1", path=f"{path}.aggregate_versions")
            aggregate_versions[k] = v
        raw_keys = _expect_list(_require(obj, "cursor_keys", path=path), "cursor_keys", path=f"{path}.cursor_keys")
        if len(raw_keys) < MIN_CURSOR_KEYS or len(raw_keys) > MAX_CURSOR_KEYS:
            raise SchemaError(f"cursor_keys must have {MIN_CURSOR_KEYS}..{MAX_CURSOR_KEYS} entries", path=f"{path}.cursor_keys")
        cursor_keys = tuple(CursorKey.from_mapping(k, path=f"{path}.cursor_keys[{i}]") for i, k in enumerate(raw_keys))
        active_cursor_key_id = ids.validate_cur_id(_require(obj, "active_cursor_key_id", path=path))
        if active_cursor_key_id not in {k.key_id for k in cursor_keys}:
            raise SchemaError("active_cursor_key_id must name one of the listed cursor keys", path=f"{path}.active_cursor_key_id")
        limits_digest = ids.validate_digest(_require(obj, "limits_digest", path=path), name=f"{path}.limits_digest")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"repository root checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(schema=schema, repository_id=repository_id, created_at=created_at,
                   aggregate_versions=aggregate_versions, cursor_keys=cursor_keys,
                   active_cursor_key_id=active_cursor_key_id, limits_digest=limits_digest, checksum=checksum)


# ---------------------------------------------------------------------------
# section 5.7 Corruption marker (authority identity union) + Repair
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorityIdentityRepository(_ClosedRecord):
    kind: str
    repository_id: str


@dataclass(frozen=True)
class AuthorityIdentityAggregate(_ClosedRecord):
    kind: str
    aggregate: str
    generation: int | None
    sequence: int | None
    relative_path: str


@dataclass(frozen=True)
class AuthorityIdentityCheckpoint(_ClosedRecord):
    kind: str
    aggregate: str
    generation: int
    relative_path: str


@dataclass(frozen=True)
class AuthorityIdentityPreview(_ClosedRecord):
    kind: str
    preview_digest: str
    relative_path: str


@dataclass(frozen=True)
class AuthorityIdentityEndpointResult(_ClosedRecord):
    kind: str
    scope: str
    key_digest: str
    sequence: int
    relative_path: str


@dataclass(frozen=True)
class AuthorityIdentityTransactionFile(_ClosedRecord):
    kind: str
    transaction_id: str
    component: str
    relative_path: str


def parse_authority_identity(obj: Mapping[str, Any], *, path: str = "authority_identity") -> Any:
    if not isinstance(obj, dict):
        raise SchemaError("authority_identity must be an object", path=path)
    if "kind" not in obj:
        raise SchemaError("authority_identity.kind is required", path=f"{path}.kind")
    kind = obj["kind"]
    if kind == "repository":
        _reject_unknown(obj, frozenset({"kind", "repository_id"}), path=path)
        return AuthorityIdentityRepository(kind=kind, repository_id=ids.validate_repository_id(_require(obj, "repository_id", path=path)))
    if kind == "aggregate":
        _reject_unknown(obj, frozenset({"kind", "aggregate", "generation", "sequence", "relative_path"}), path=path)
        aggregate = _expect_str(_require(obj, "aggregate", path=path), "aggregate", path=f"{path}.aggregate")
        if aggregate not in AGGREGATE_KINDS:
            raise SchemaError(f"aggregate must be one of {sorted(AGGREGATE_KINDS)}", path=f"{path}.aggregate")
        gen_v = _require(obj, "generation", path=path)
        seq_v = _require(obj, "sequence", path=path)
        if gen_v is None and seq_v is not None:
            raise SchemaError("sequence non-null with generation null is invalid", path=f"{path}.sequence")
        generation = None if gen_v is None else _expect_int(gen_v, "generation", path=f"{path}.generation")
        sequence = None if seq_v is None else _expect_int(seq_v, "sequence", path=f"{path}.sequence")
        relative_path = ids.validate_relative_path(_require(obj, "relative_path", path=path), name=f"{path}.relative_path")
        return AuthorityIdentityAggregate(kind=kind, aggregate=aggregate, generation=generation, sequence=sequence, relative_path=relative_path)
    if kind == "checkpoint":
        _reject_unknown(obj, frozenset({"kind", "aggregate", "generation", "relative_path"}), path=path)
        aggregate = _expect_str(_require(obj, "aggregate", path=path), "aggregate", path=f"{path}.aggregate")
        if aggregate not in CHECKPOINT_KINDS:
            raise SchemaError(f"aggregate must be one of {sorted(CHECKPOINT_KINDS)}", path=f"{path}.aggregate")
        generation = _expect_int(_require(obj, "generation", path=path), "generation", path=f"{path}.generation")
        if generation < 0:
            raise SchemaError("generation must be non-negative", path=f"{path}.generation")
        relative_path = ids.validate_relative_path(_require(obj, "relative_path", path=path), name=f"{path}.relative_path")
        return AuthorityIdentityCheckpoint(kind=kind, aggregate=aggregate, generation=generation, relative_path=relative_path)
    if kind == "preview":
        _reject_unknown(obj, frozenset({"kind", "preview_digest", "relative_path"}), path=path)
        return AuthorityIdentityPreview(kind=kind, preview_digest=ids.validate_digest(_require(obj, "preview_digest", path=path), name=f"{path}.preview_digest"), relative_path=ids.validate_relative_path(_require(obj, "relative_path", path=path), name=f"{path}.relative_path"))
    if kind == "endpoint_result":
        _reject_unknown(obj, frozenset({"kind", "scope", "key_digest", "sequence", "relative_path"}), path=path)
        scope = _expect_str(_require(obj, "scope", path=path), "scope", path=f"{path}.scope")
        key_digest = ids.validate_digest(_require(obj, "key_digest", path=path), name=f"{path}.key_digest")
        sequence = _expect_int(_require(obj, "sequence", path=path), "sequence", path=f"{path}.sequence")
        if sequence < 0:
            raise SchemaError("sequence must be non-negative", path=f"{path}.sequence")
        relative_path = ids.validate_relative_path(_require(obj, "relative_path", path=path), name=f"{path}.relative_path")
        return AuthorityIdentityEndpointResult(kind=kind, scope=scope, key_digest=key_digest, sequence=sequence, relative_path=relative_path)
    if kind == "transaction_file":
        _reject_unknown(obj, frozenset({"kind", "transaction_id", "component", "relative_path"}), path=path)
        transaction_id = ids.validate_transaction_id_any(_require(obj, "transaction_id", path=path))
        component = _expect_str(_require(obj, "component", path=path), "component", path=f"{path}.component")
        if component not in TRANSACTION_FILE_COMPONENTS:
            raise SchemaError(f"component must be one of {sorted(TRANSACTION_FILE_COMPONENTS)}", path=f"{path}.component")
        relative_path = ids.validate_relative_path(_require(obj, "relative_path", path=path), name=f"{path}.relative_path")
        return AuthorityIdentityTransactionFile(kind=kind, transaction_id=transaction_id, component=component, relative_path=relative_path)
    raise SchemaError(f"unknown authority_identity kind {kind!r}", path=f"{path}.kind")


@dataclass(frozen=True)
class DiagnosticCopy(_ClosedRecord):
    relative_path: str
    digest: str
    size: int
    FIELDS: ClassVar[frozenset[str]] = frozenset({"relative_path", "digest", "size"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "diagnostic_copy") -> "DiagnosticCopy":
        _reject_unknown(obj, cls.FIELDS, path=path)
        relative_path = ids.validate_relative_path(_require(obj, "relative_path", path=path), name=f"{path}.relative_path")
        digest = ids.validate_digest(_require(obj, "digest", path=path), name=f"{path}.digest")
        size = ids.validate_stored_byte_size(_require(obj, "size", path=path), name=f"{path}.size")
        return cls(relative_path=relative_path, digest=digest, size=size)


@dataclass(frozen=True)
class CorruptionMarker(_ClosedRecord):
    schema: int
    corruption_id: str
    repository_id: str
    authority_kind: str
    authority_identity: Any
    reason: str
    observed_digest: str | None
    diagnostic_copy: DiagnosticCopy | None
    detected_at: str
    repaired_at: str | None
    repair_record_id: str | None
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "corruption_id", "repository_id", "authority_kind",
        "authority_identity", "reason", "observed_digest", "diagnostic_copy",
        "detected_at", "repaired_at", "repair_record_id", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "corruption_marker", verify_checksum: bool = True) -> "CorruptionMarker":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        corruption_id = ids.validate_cor_id(_require(obj, "corruption_id", path=path))
        repository_id = ids.validate_repository_id(_require(obj, "repository_id", path=path))
        authority_kind = _expect_str(_require(obj, "authority_kind", path=path), "authority_kind", path=f"{path}.authority_kind")
        if authority_kind not in AUTHORITY_KINDS:
            raise SchemaError(f"authority_kind must be one of {sorted(AUTHORITY_KINDS)}", path=f"{path}.authority_kind")
        raw_ai = _expect_mapping(_require(obj, "authority_identity", path=path), "authority_identity", path=f"{path}.authority_identity")
        authority_identity = parse_authority_identity(raw_ai, path=f"{path}.authority_identity")
        if authority_identity.kind != authority_kind:
            raise SchemaError("authority_kind MUST equal authority_identity.kind", path=f"{path}.authority_kind")
        reason = _expect_str(_require(obj, "reason", path=path), "reason", path=f"{path}.reason")
        if reason not in CORRUPTION_REASONS:
            raise SchemaError(f"reason must be one of {sorted(CORRUPTION_REASONS)}", path=f"{path}.reason")
        raw_od = _require(obj, "observed_digest", path=path)
        if raw_od is None:
            if reason not in NULL_DIGEST_REASONS:
                raise SchemaError(f"observed_digest may be null only for reasons {sorted(NULL_DIGEST_REASONS)} before readable bytes were obtained", path=f"{path}.observed_digest")
            observed_digest = None
        else:
            observed_digest = ids.validate_digest(raw_od, name=f"{path}.observed_digest")
        raw_dc = _require(obj, "diagnostic_copy", path=path)
        diagnostic_copy = None if raw_dc is None else DiagnosticCopy.from_mapping(raw_dc, path=f"{path}.diagnostic_copy")
        detected_at = ids.validate_timestamp(_require(obj, "detected_at", path=path), name=f"{path}.detected_at")
        raw_ra = _require(obj, "repaired_at", path=path)
        raw_rid = _require(obj, "repair_record_id", path=path)
        if (raw_ra is None) != (raw_rid is None):
            raise SchemaError("repaired_at and repair_record_id must be both null or both non-null", path=f"{path}.repaired_at")
        repaired_at = None if raw_ra is None else ids.validate_timestamp(raw_ra, name=f"{path}.repaired_at")
        repair_record_id = None if raw_rid is None else ids.validate_rec_id(raw_rid)
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"corruption marker checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(schema=schema, corruption_id=corruption_id, repository_id=repository_id,
                   authority_kind=authority_kind, authority_identity=authority_identity, reason=reason,
                   observed_digest=observed_digest, diagnostic_copy=diagnostic_copy, detected_at=detected_at,
                   repaired_at=repaired_at, repair_record_id=repair_record_id, checksum=checksum)


@dataclass(frozen=True)
class SourceProvenance(_ClosedRecord):
    kind: str
    source_digest: str
    operator_note: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "source_digest", "operator_note"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "source_provenance") -> "SourceProvenance":
        _reject_unknown(obj, cls.FIELDS, path=path)
        kind = _expect_str(_require(obj, "kind", path=path), "kind", path=f"{path}.kind")
        if kind not in REPAIR_SOURCE_KINDS:
            raise SchemaError(f"source_provenance.kind must be one of {sorted(REPAIR_SOURCE_KINDS)}", path=f"{path}.kind")
        source_digest = ids.validate_digest(_require(obj, "source_digest", path=path), name=f"{path}.source_digest")
        operator_note = _bounded_str(_require(obj, "operator_note", path=path), "operator_note", lo=1, hi=1024, path=f"{path}.operator_note")
        return cls(kind=kind, source_digest=source_digest, operator_note=operator_note)


@dataclass(frozen=True)
class ReplacementAuthority(_ClosedRecord):
    relative_path: str
    digest: str
    size: int
    source_provenance: SourceProvenance
    FIELDS: ClassVar[frozenset[str]] = frozenset({"relative_path", "digest", "size", "source_provenance"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "replacement_authority") -> "ReplacementAuthority":
        _reject_unknown(obj, cls.FIELDS, path=path)
        relative_path = ids.validate_relative_path(_require(obj, "relative_path", path=path), name=f"{path}.relative_path")
        digest = ids.validate_digest(_require(obj, "digest", path=path), name=f"{path}.digest")
        size = ids.validate_stored_byte_size(_require(obj, "size", path=path), name=f"{path}.size")
        source_provenance = SourceProvenance.from_mapping(_require(obj, "source_provenance", path=path), path=f"{path}.source_provenance")
        return cls(relative_path=relative_path, digest=digest, size=size, source_provenance=source_provenance)


@dataclass(frozen=True)
class TerminalClassification(_ClosedRecord):
    scope: str
    key_digest: str
    transaction_id: str
    outcome: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({"scope", "key_digest", "transaction_id", "outcome"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "terminal_classification") -> "TerminalClassification":
        _reject_unknown(obj, cls.FIELDS, path=path)
        scope = _expect_str(_require(obj, "scope", path=path), "scope", path=f"{path}.scope")
        key_digest = ids.validate_digest(_require(obj, "key_digest", path=path), name=f"{path}.key_digest")
        transaction_id = ids.validate_transaction_id_any(_require(obj, "transaction_id", path=path))
        outcome = _expect_str(_require(obj, "outcome", path=path), "outcome", path=f"{path}.outcome")
        if outcome != "indeterminate":
            raise SchemaError("terminal_classification.outcome must be 'indeterminate'", path=f"{path}.outcome")
        return cls(scope=scope, key_digest=key_digest, transaction_id=transaction_id, outcome=outcome)


@dataclass(frozen=True)
class Repair(_ClosedRecord):
    schema: int
    repair_record_id: str
    corruption_id: str
    repository_id: str
    action: str
    replacement_authority: ReplacementAuthority | None
    terminal_classification: TerminalClassification | None
    operator_label: str
    reason: str
    created_at: str
    checksum: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "repair_record_id", "corruption_id", "repository_id", "action",
        "replacement_authority", "terminal_classification", "operator_label",
        "reason", "created_at", "checksum",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "repair", verify_checksum: bool = True) -> "Repair":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        repair_record_id = ids.validate_rec_id(_require(obj, "repair_record_id", path=path))
        corruption_id = ids.validate_cor_id(_require(obj, "corruption_id", path=path))
        repository_id = ids.validate_repository_id(_require(obj, "repository_id", path=path))
        action = _expect_str(_require(obj, "action", path=path), "action", path=f"{path}.action")
        if action not in REPAIR_ACTIONS:
            raise SchemaError(f"action must be one of {sorted(REPAIR_ACTIONS)}", path=f"{path}.action")
        raw_repl = _require(obj, "replacement_authority", path=path)
        raw_tc = _require(obj, "terminal_classification", path=path)
        if action == "replace_authority":
            if raw_repl is None or raw_tc is not None:
                raise SchemaError("replace_authority requires replacement_authority and null terminal_classification", path=path)
            replacement_authority: ReplacementAuthority | None = ReplacementAuthority.from_mapping(raw_repl, path=f"{path}.replacement_authority")
            terminal_classification: TerminalClassification | None = None
        else:  # classify_indeterminate
            if raw_tc is None or raw_repl is not None:
                raise SchemaError("classify_indeterminate requires terminal_classification and null replacement_authority", path=path)
            terminal_classification = TerminalClassification.from_mapping(raw_tc, path=f"{path}.terminal_classification")
            replacement_authority = None
        operator_label = ids.validate_actor(_require(obj, "operator_label", path=path), name=f"{path}.operator_label")
        reason = _bounded_str(_require(obj, "reason", path=path), "reason", lo=1, hi=1024, path=f"{path}.reason")
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        checksum = ids.validate_digest(_require(obj, "checksum", path=path), name=f"{path}.checksum")
        if verify_checksum:
            expected = compute_checksum(dict(obj))
            if checksum != expected:
                raise IntegrityError(f"repair checksum mismatch: expected {expected}, got {checksum}", path=f"{path}.checksum")
        return cls(schema=schema, repair_record_id=repair_record_id, corruption_id=corruption_id,
                   repository_id=repository_id, action=action, replacement_authority=replacement_authority,
                   terminal_classification=terminal_classification, operator_label=operator_label,
                   reason=reason, created_at=created_at, checksum=checksum)


# ---------------------------------------------------------------------------
# section 5.7 Notification, Legacy projection, Page/cursor wire
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Notification(_ClosedRecord):
    schema: int
    proposal_id: str
    proposal_revision: int
    event: str
    read_model_token: str
    transaction_id: str | None
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "proposal_id", "proposal_revision", "event",
        "read_model_token", "transaction_id",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "notification") -> "Notification":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        proposal_id = ids.validate_proposal_id(_require(obj, "proposal_id", path=path))
        rev = ids.validate_revision(_require(obj, "proposal_revision", path=path), name=f"{path}.proposal_revision")
        event = _expect_str(_require(obj, "event", path=path), "event", path=f"{path}.event")
        if event not in NOTIFICATION_EVENTS:
            raise SchemaError(f"event must be one of {sorted(NOTIFICATION_EVENTS)}", path=f"{path}.event")
        read_model_token = ids.validate_digest(_require(obj, "read_model_token", path=path), name=f"{path}.read_model_token")
        raw_tid = _require(obj, "transaction_id", path=path)
        transaction_id = None if raw_tid is None else ids.validate_transaction_id_any(raw_tid)
        return cls(schema=schema, proposal_id=proposal_id, proposal_revision=rev, event=event,
                   read_model_token=read_model_token, transaction_id=transaction_id)


@dataclass(frozen=True)
class LegacyIdentity(_ClosedRecord):
    legacy_id: str
    source_generation: int
    FIELDS: ClassVar[frozenset[str]] = frozenset({"legacy_id", "source_generation"})

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "legacy_identity") -> "LegacyIdentity":
        _reject_unknown(obj, cls.FIELDS, path=path)
        legacy_id = _bounded_str(_require(obj, "legacy_id", path=path), "legacy_id", lo=1, hi=240, path=f"{path}.legacy_id")
        source_generation = _expect_int(_require(obj, "source_generation", path=path), "source_generation", path=f"{path}.source_generation")
        if source_generation < 0:
            raise SchemaError("source_generation must be non-negative", path=f"{path}.source_generation")
        return cls(legacy_id=legacy_id, source_generation=source_generation)


@dataclass(frozen=True)
class LegacyProjection(_ClosedRecord):
    schema: int
    kind: str
    legacy_identity: LegacyIdentity
    summary: str
    author: str
    created_at: str
    regeneration_required: bool
    applicable: bool
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "kind", "legacy_identity", "summary", "author",
        "created_at", "regeneration_required", "applicable",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "legacy_projection") -> "LegacyProjection":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        kind = _expect_str(_require(obj, "kind", path=path), "kind", path=f"{path}.kind")
        if kind != "legacy-suggestion":
            raise SchemaError("kind must be 'legacy-suggestion'", path=f"{path}.kind")
        legacy_identity = LegacyIdentity.from_mapping(_require(obj, "legacy_identity", path=path), path=f"{path}.legacy_identity")
        summary = _bounded_str(_require(obj, "summary", path=path), "summary", lo=1, hi=MAX_SUMMARY_CODEPOINTS, path=f"{path}.summary")
        author = ids.validate_actor(_require(obj, "author", path=path), name=f"{path}.author")
        created_at = ids.validate_timestamp(_require(obj, "created_at", path=path), name=f"{path}.created_at")
        regeneration_required = _expect_bool(_require(obj, "regeneration_required", path=path), "regeneration_required", path=f"{path}.regeneration_required")
        if regeneration_required is not True:
            raise SchemaError("regeneration_required must be true for legacy projections", path=f"{path}.regeneration_required")
        applicable = _expect_bool(_require(obj, "applicable", path=path), "applicable", path=f"{path}.applicable")
        if applicable is not False:
            raise SchemaError("applicable must be false: legacy projections never become applicable", path=f"{path}.applicable")
        return cls(schema=schema, kind=kind, legacy_identity=legacy_identity, summary=summary,
                   author=author, created_at=created_at, regeneration_required=regeneration_required,
                   applicable=applicable)


@dataclass(frozen=True)
class TransactionProgress(_ClosedRecord):
    """section 5.7 transaction-progress projection (nonterminal WAL only)."""
    schema: int
    operation: str
    transaction_id: str
    parent_transaction_id: str | None
    status: str
    phase: str
    commit_decided: bool
    completed_targets: int
    total_targets: int
    recovery_attempts: int
    started_at: str
    updated_at: str
    retry_after_ms: int | None
    error_code: str | None
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "operation", "transaction_id", "parent_transaction_id",
        "status", "phase", "commit_decided", "completed_targets",
        "total_targets", "recovery_attempts", "started_at", "updated_at",
        "retry_after_ms", "error_code",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "transaction_progress") -> "TransactionProgress":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        operation = _expect_str(_require(obj, "operation", path=path), "operation", path=f"{path}.operation")
        if operation not in ("apply", "undo"):
            raise SchemaError("operation must be 'apply' or 'undo'", path=f"{path}.operation")
        transaction_id = _check_txn_prefix(_require(obj, "transaction_id", path=path), operation, path=path)
        raw_pt = _require(obj, "parent_transaction_id", path=path)
        parent_transaction_id = None if raw_pt is None else ids.validate_txn_id(raw_pt)
        status = _expect_str(_require(obj, "status", path=path), "status", path=f"{path}.status")
        if status not in PROGRESS_STATUSES:
            raise SchemaError(f"status must be one of {sorted(PROGRESS_STATUSES)}", path=f"{path}.status")
        phase = _expect_str(_require(obj, "phase", path=path), "phase", path=f"{path}.phase")
        if phase not in WAL_PHASES:
            raise SchemaError(f"phase must be one of {sorted(WAL_PHASES)}", path=f"{path}.phase")
        commit_decided = _expect_bool(_require(obj, "commit_decided", path=path), "commit_decided", path=f"{path}.commit_decided")
        completed_targets = _expect_int(_require(obj, "completed_targets", path=path), "completed_targets", path=f"{path}.completed_targets")
        if completed_targets < 0:
            raise SchemaError("completed_targets must be non-negative", path=f"{path}.completed_targets")
        total_targets = _expect_int(_require(obj, "total_targets", path=path), "total_targets", path=f"{path}.total_targets")
        if total_targets < 1 or completed_targets > total_targets:
            raise SchemaError("total_targets must be >= 1 and >= completed_targets", path=f"{path}.total_targets")
        recovery_attempts = _expect_int(_require(obj, "recovery_attempts", path=path), "recovery_attempts", path=f"{path}.recovery_attempts")
        if recovery_attempts < 0 or recovery_attempts > 100:
            raise SchemaError("recovery_attempts must be in 0..100", path=f"{path}.recovery_attempts")
        started_at = ids.validate_timestamp(_require(obj, "started_at", path=path), name=f"{path}.started_at")
        updated_at = ids.validate_timestamp(_require(obj, "updated_at", path=path), name=f"{path}.updated_at")
        raw_retry = _require(obj, "retry_after_ms", path=path)
        if status == "indeterminate":
            if raw_retry is not None:
                raise SchemaError("retry_after_ms must be null for indeterminate status", path=f"{path}.retry_after_ms")
            retry_after_ms: int | None = None
        else:
            if not (isinstance(raw_retry, int) and not isinstance(raw_retry, bool)):
                raise SchemaError("retry_after_ms must be an integer for non-indeterminate status", path=f"{path}.retry_after_ms")
            retry_after_ms = raw_retry
        raw_ec = _require(obj, "error_code", path=path)
        error_code: str | None
        if raw_ec is None:
            error_code = None
        else:
            error_code = _expect_str(raw_ec, "error_code", path=f"{path}.error_code")
        return cls(schema=schema, operation=operation, transaction_id=transaction_id,
                   parent_transaction_id=parent_transaction_id, status=status, phase=phase,
                   commit_decided=commit_decided, completed_targets=completed_targets,
                   total_targets=total_targets, recovery_attempts=recovery_attempts,
                   started_at=started_at, updated_at=updated_at, retry_after_ms=retry_after_ms,
                   error_code=error_code)


@dataclass(frozen=True)
class CursorPayload(_ClosedRecord):
    """section 10 stateless pagination cursor payload (signed by the server)."""
    schema: int
    repository_id: str
    query_digest: str
    last_created_at: str
    last_proposal_id: str
    last_proposal_revision: int
    repository_high_waters: dict[str, dict[str, int]]
    bundle_snapshot_digest: str
    validation_context_digest: str
    clock_captured_at: str
    earliest_expiry_at: str | None
    environment_digest: str
    cursor_expires_at: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "repository_id", "query_digest", "last_created_at",
        "last_proposal_id", "last_proposal_revision", "repository_high_waters",
        "bundle_snapshot_digest", "validation_context_digest", "clock_captured_at", "earliest_expiry_at",
        "environment_digest", "cursor_expires_at",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "cursor_payload") -> "CursorPayload":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        repository_id = ids.validate_repository_id(_require(obj, "repository_id", path=path))
        query_digest = ids.validate_digest(_require(obj, "query_digest", path=path), name=f"{path}.query_digest")
        last_created_at = ids.validate_timestamp(_require(obj, "last_created_at", path=path), name=f"{path}.last_created_at")
        last_proposal_id = ids.validate_proposal_id(_require(obj, "last_proposal_id", path=path))
        last_proposal_revision = ids.validate_revision(_require(obj, "last_proposal_revision", path=path), name=f"{path}.last_proposal_revision")
        raw_hw = _expect_mapping(_require(obj, "repository_high_waters", path=path), "repository_high_waters", path=f"{path}.repository_high_waters")
        if sorted(raw_hw.keys()) != sorted(AGGREGATE_KINDS):
            raise SchemaError(f"repository_high_waters must have exactly the keys {sorted(AGGREGATE_KINDS)}", path=f"{path}.repository_high_waters")
        repository_high_waters: dict[str, dict[str, int]] = {}
        for k in AGGREGATE_KINDS:
            entry = raw_hw[k]
            if not isinstance(entry, dict):
                raise SchemaError(f"repository_high_waters[{k}] must be an object", path=f"{path}.repository_high_waters")
            if sorted(entry.keys()) != ["generation", "sequence"]:
                raise SchemaError(f"repository_high_waters[{k}] must have keys generation,sequence", path=f"{path}.repository_high_waters")
            for fld in ("generation", "sequence"):
                v = entry[fld]
                if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                    raise SchemaError(f"repository_high_waters[{k}].{fld} must be a non-negative integer", path=f"{path}.repository_high_waters")
            repository_high_waters[k] = {"generation": entry["generation"], "sequence": entry["sequence"]}
        bundle_snapshot_digest = ids.validate_digest(_require(obj, "bundle_snapshot_digest", path=path), name=f"{path}.bundle_snapshot_digest")
        validation_context_digest = ids.validate_digest(
            _require(obj, "validation_context_digest", path=path), name=f"{path}.validation_context_digest"
        )
        clock_captured_at = ids.validate_timestamp(_require(obj, "clock_captured_at", path=path), name=f"{path}.clock_captured_at")
        raw_ee = _require(obj, "earliest_expiry_at", path=path)
        earliest_expiry_at = None if raw_ee is None else ids.validate_timestamp(raw_ee, name=f"{path}.earliest_expiry_at")
        environment_digest = ids.validate_digest(_require(obj, "environment_digest", path=path), name=f"{path}.environment_digest")
        cursor_expires_at = ids.validate_timestamp(_require(obj, "cursor_expires_at", path=path), name=f"{path}.cursor_expires_at")
        return cls(schema=schema, repository_id=repository_id, query_digest=query_digest,
                   last_created_at=last_created_at, last_proposal_id=last_proposal_id,
                   last_proposal_revision=last_proposal_revision,
                     repository_high_waters=repository_high_waters,
                     bundle_snapshot_digest=bundle_snapshot_digest,
                     validation_context_digest=validation_context_digest,
                   clock_captured_at=clock_captured_at, earliest_expiry_at=earliest_expiry_at,
                   environment_digest=environment_digest, cursor_expires_at=cursor_expires_at)


@dataclass(frozen=True)
class PagePayload(_ClosedRecord):
    """section 10 list-page payload."""
    schema: int
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    page_snapshot_token: str
    snapshot_expires_at: str
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema", "items", "next_cursor", "page_snapshot_token", "snapshot_expires_at",
    })

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any], *, path: str = "page_payload") -> "PagePayload":
        _reject_unknown(obj, cls.FIELDS, path=path)
        schema = _schema_int(_require(obj, "schema", path=path), expected=1, path=f"{path}.schema")
        raw_items = _expect_list(_require(obj, "items", path=path), "items", path=f"{path}.items")
        items = tuple(_expect_mapping(it, f"items[{i}]", path=f"{path}.items[{i}]") for i, it in enumerate(raw_items))
        raw_nc = _require(obj, "next_cursor", path=path)
        next_cursor = None if raw_nc is None else _expect_str(raw_nc, "next_cursor", path=f"{path}.next_cursor")
        page_snapshot_token = ids.validate_digest(_require(obj, "page_snapshot_token", path=path), name=f"{path}.page_snapshot_token")
        snapshot_expires_at = ids.validate_timestamp(_require(obj, "snapshot_expires_at", path=path), name=f"{path}.snapshot_expires_at")
        return cls(schema=schema, items=items, next_cursor=next_cursor,
                   page_snapshot_token=page_snapshot_token, snapshot_expires_at=snapshot_expires_at)


__all__ = [
    "FrozenDict", "FrozenList",
    # bounds / enums
    "MAX_SUMMARY_CODEPOINTS", "MAX_RATIONALE_CODEPOINTS", "MAX_PROVENANCE_ENTRIES",
    "MAX_PROVENANCE_VALUE_CODEPOINTS", "MAX_OPERATIONS", "MAX_DEPENDENCY_EDGES",
    "MAX_AFFECTED_CONCEPTS", "MAX_SNAPSHOT_CONCEPTS", "MAX_READS_OR_WRITES_PER_OP",
    "MAX_DECISION_REASON_CODEPOINTS", "MAX_WITHDRAWAL_REASON_CODEPOINTS",
    "MAX_CONCEPT_BYTES", "MAX_PREVIEW_BYTES", "MAX_CHECKPOINT_BYTES",
    "MAX_VALIDATION_FINDINGS", "MAX_DIFF_LINES", "MAX_DIFF_BYTES",
    "MAX_PROVENANCE_KINDS", "MAX_CURSOR_KEYS", "MIN_CURSOR_KEYS",
    "WAL_PHASES", "WAL_OPERATION_PREFIXES", "AGGREGATE_KINDS", "CHECKPOINT_KINDS",
    "WRAPPER_KINDS", "AUTHORITY_KINDS", "TRANSACTION_FILE_COMPONENTS",
    "CORRUPTION_REASONS", "NULL_DIGEST_REASONS", "DECISION_VALUES",
    "CONFLICT_CODES", "NOTIFICATION_EVENTS", "AUDIT_STATUS", "VALIDATION_STATUS",
    "ENDPOINT_RESULT_STATES", "TOMBSTONE_OUTCOMES", "OUTBOX_STATES",
      "OUTBOX_AGGREGATE_KINDS", "REPAIR_ACTIONS", "REPAIR_SOURCE_KINDS",
      "PROGRESS_STATUSES", "SERVICE_INTENT_PHASES", "MAX_SERVICE_INTENT_BYTES",
    # functions
    "is_legal_wal_phase_bitmap", "decision_set_token", "sort_conflicts",
    "parse_authority_identity",
    # records
    "ConceptIdentity", "InventoryEntry", "Snapshot", "Operation", "ProvenanceEntry", "PreviewRef",
    "ProposalRevision", "Withdrawal", "ConceptChange", "Diff", "ValidationFinding",
    "Validation", "Preview", "DecisionRow", "Conflict", "ApplyRequest",
    "UndoRequest", "TransactionResult", "DirIdentity", "LeafIdentity", "TargetIdentity", "WalTarget",
    "WAL", "HistoryTarget", "HistoryManifest", "EndpointResult", "Tombstone",
      "OutboxReceipt", "ServiceMutationIntent", "TransactionAudit", "Checkpoint", "Wrapper", "CursorKey",
    "RepositoryRoot", "AuthorityIdentityRepository", "AuthorityIdentityAggregate",
    "AuthorityIdentityCheckpoint", "AuthorityIdentityPreview",
    "AuthorityIdentityEndpointResult", "AuthorityIdentityTransactionFile",
    "DiagnosticCopy", "CorruptionMarker", "SourceProvenance", "ReplacementAuthority",
    "TerminalClassification", "Repair", "Notification", "LegacyIdentity",
    "LegacyProjection", "TransactionProgress", "CursorPayload", "PagePayload",
]
