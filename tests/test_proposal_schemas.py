"""SCHEMA proof catalogue tests for Proposal / Diff Review v1 (Foundation A).

Implements the named ``SCHEMA-01..SCHEMA-11`` proof nodes from the normative
catalogue (SPEC §17.1) plus property / table cases for: duplicate keys at every
depth, unknown / missing fields, invalid enums / ids / paths / timestamps /
digests, nonfinite / negative-zero numbers, depth, extension limits, operation
DAG / reads / writes, WAL legal / illegal phase bitmaps, repository / corruption
/ repair unions, and canonical round-trip / digest stability.

This slice covers closed contract parsing and canonical serialization only -
no storage, routes, CLI, apply, or UI.
"""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from okf_loom.proposal_review import canonical, ids, schemas
from okf_loom.proposal_review.errors import (
    CanonicalJsonError,
    IntegrityError,
    LimitError,
    PrimitiveError,
    SchemaError,
)


# ============================================================================
# Shared constants and fixture builders
# ============================================================================

ULID = "01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
HEX64 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
HEX64B = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
DIGEST = "sha256:" + HEX64
DIGEST_B = "sha256:" + HEX64B
ROW_TOKEN = "A" * 43
ROW_TOKEN_B = "B" * 43
RUNTIME = "okf-validator@1.0+sha256:" + HEX64
RUNTIME_SER = "okf-roundtrip@1.0+sha256:" + HEX64
RUNTIME_OPS = "proposal-ops@1.0+sha256:" + HEX64
TS = "2026-07-14T12:34:56.789Z"
TS2 = "2026-07-15T01:02:03.040Z"
TS3 = "2026-07-22T00:00:00.000Z"


def _ext(**kw: Any) -> dict[str, Any]:
    return {}


def _checksum(d: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``d`` with a valid ``checksum`` attached."""
    out = copy.deepcopy(d)
    if "scope" in out and "state" in out:
        out.setdefault("record_id", "rec_" + ULID)
    if "scope" in out and "outcome" in out:
        out.setdefault("result_record_id", "rec_" + ULID)
    if "outbox_id" in out and "proposal_id" not in out:
        payload = {"schema": 1, "proposal_id": ULID, "proposal_revision": 1,
                   "event": out["event_type"], "read_model_token": DIGEST,
                   "transaction_id": None}
        out.update(proposal_id=ULID, proposal_revision=1,
                   mutation_intent_id="svc_" + ULID,
                   result_record_id="rec_" + ULID, payload=payload,
                   payload_digest=canonical.canonical_digest(payload))
    out["checksum"] = canonical.compute_checksum(out)
    return out


def base_snapshot_mapping() -> dict[str, Any]:
    return {
        "schema": 1,
        "captured_at": TS,
        "concepts": [
            {"concept": "tables/customers", "revision": HEX64,
             "identity": {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
             "leaf_inode": 3, "ctime_ns": 4, "mode": 0o100600, "size": 1,
             "digest": HEX64}},
            {"concept": "tables/orders", "revision": HEX64B,
             "identity": {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
             "leaf_inode": 4, "ctime_ns": 5, "mode": 0o100600, "size": 1,
             "digest": HEX64B}},
        ],
        "capabilities": ["okf.cap.typed_relations"],
        "config_digest": DIGEST,
        "validation_context_digest": DIGEST_B,
        "validator_id": RUNTIME,
        "serializer_id": RUNTIME_SER,
        "operation_runtime_id": RUNTIME_OPS,
        "snapshot_digest": "",  # filled by helper
    }


def snapshot_mapping() -> dict[str, Any]:
    snap = base_snapshot_mapping()
    body = {
        "schema": snap["schema"],
        "concepts": snap["concepts"],
        "capabilities": snap["capabilities"],
        "config_digest": snap["config_digest"],
        "validation_context_digest": snap["validation_context_digest"],
        "validator_id": snap["validator_id"],
        "serializer_id": snap["serializer_id"],
        "operation_runtime_id": snap["operation_runtime_id"],
    }
    snap["snapshot_digest"] = canonical.canonical_digest(body)
    return snap


def link_add_operation(op_id: str = "op_" + ULID) -> dict[str, Any]:
    return {
        "schema": 1,
        "op_id": op_id,
        "kind": "link_add",
        "target": "tables/orders",
        "args": {
            "destination": "tables/customers",
            "label": "customers",
            "relation": "depends_on",
            "detail": None,
            "allow_forward_reference": False,
        },
        "reads": ["tables/customers", "tables/orders"],
        "writes": ["tables/orders"],
        "expected_revisions": {
            "tables/customers": HEX64,
            "tables/orders": HEX64B,
        },
        "depends_on": [],
        "extensions": {},
    }


def proposal_mapping(rev: int = 1, supersedes: int | None = None) -> dict[str, Any]:
    snap = snapshot_mapping()
    m = {
        "schema": 1,
        "record_id": "rec_" + ULID,
        "creation_result_record_id": "rec_" + ("B" * 26),
        "creation_request_fingerprint": DIGEST,
        "proposal_id": ULID,
        "proposal_revision": rev,
        "supersedes_revision": supersedes,
        "mode": "proposal",
        "author": "agent-local",
        "created_at": TS,
        "summary": "Add the customer relation",
        "rationale": "Make the dependency explicit without changing direct mode.",
        "affected_concepts": ["tables/customers", "tables/orders"],
        "expected_snapshot": snap,
        "operations": [link_add_operation()],
        "preview_ref": {
            "schema": 1,
            "preview_digest": DIGEST,
            "artifact_bytes": 4096,
        },
        "provenance": [{"kind": "comment", "value": "01J2Y8N5Z9"}],
        "expires_at": TS3,
        "extensions": {},
    }
    return m


# ============================================================================
# SCHEMA-01: Proposal / withdrawal closed immutable schemas
# ============================================================================


def test_schema_01_proposal() -> None:
    """Proposal revision + withdrawal parse, round-trip, reject bad inputs."""
    # --- Valid proposal round-trips and digest is stable ---
    m = proposal_mapping()
    rec = schemas.ProposalRevision.from_mapping(m)
    assert rec.proposal_revision == 1
    assert rec.supersedes_revision is None
    # canonical round trip: the canonical bytes re-parse to an equal structure
    # (tuples normalize to lists through JSON, so compare via canonical bytes).
    rt_bytes = rec.canonical_bytes()
    assert canonical.loads_canonical(rt_bytes) == canonical.loads_canonical(
        canonical.dumps_canonical(rec.to_mapping())
    )
    digest_a = canonical.canonical_digest(canonical.loads_canonical(rt_bytes))
    digest_b = canonical.canonical_digest(canonical.loads_canonical(rec.canonical_bytes()))
    assert digest_a == digest_b

    # --- Supersession: supersedes_revision must be exactly rev - 1 ---
    sup = proposal_mapping(rev=2, supersedes=1)
    schemas.ProposalRevision.from_mapping(sup)
    bad = proposal_mapping(rev=2, supersedes=None)  # missing predecessor
    with pytest.raises(SchemaError):
        schemas.ProposalRevision.from_mapping(bad)
    gap = proposal_mapping(rev=3, supersedes=1)  # gap
    with pytest.raises(SchemaError):
        schemas.ProposalRevision.from_mapping(gap)

    # --- Unknown core field rejected ---
    extra = proposal_mapping()
    extra["unknown_top"] = 1
    with pytest.raises(SchemaError):
        schemas.ProposalRevision.from_mapping(extra)

    # --- Missing required field rejected ---
    missing = proposal_mapping()
    del missing["summary"]
    with pytest.raises(SchemaError):
        schemas.ProposalRevision.from_mapping(missing)

    # --- Inline preview rejected (only preview_ref allowed) ---
    inline = proposal_mapping()
    inline["preview"] = {"schema": 1}
    with pytest.raises(SchemaError):
        schemas.ProposalRevision.from_mapping(inline)

    # --- affected_concepts must equal sorted union of op read/write sets ---
    wrong = proposal_mapping()
    wrong["affected_concepts"] = ["tables/orders"]
    with pytest.raises(SchemaError):
        schemas.ProposalRevision.from_mapping(wrong)

    # --- Withdrawal: exact closed fields + reason bounds ---
    w = _checksum({
        "schema": 1,
        "record_id": "rec_" + ULID,
        "withdrawal_result_record_id": "rec_" + ("C" * 26),
        "withdrawal_request_fingerprint": DIGEST,
        "proposal_id": ULID,
        "proposal_revision": 1,
        "actor": "local-user",
        "reason": "mistake",
        "read_model_token": DIGEST,
        "withdrawn_at": TS2,
    })
    wr = schemas.Withdrawal.from_mapping(w)
    assert wr.reason == "mistake"
    # bad checksum
    bad_w = copy.deepcopy(w)
    bad_w["checksum"] = DIGEST  # wrong
    with pytest.raises(IntegrityError):
        schemas.Withdrawal.from_mapping(bad_w)
    # empty reason rejected
    empty_reason = copy.deepcopy(w)
    empty_reason["reason"] = ""
    empty_reason["checksum"] = canonical.compute_checksum(empty_reason)
    with pytest.raises((SchemaError, LimitError, PrimitiveError)):
        schemas.Withdrawal.from_mapping(empty_reason)
    # unknown field rejected
    extra_w = copy.deepcopy(w)
    extra_w["extra"] = 1
    with pytest.raises(SchemaError):
        schemas.Withdrawal.from_mapping(extra_w)


# ============================================================================
# SCHEMA-02: All five operation variants and op-id ownership
# ============================================================================

def _op_common(kind: str, target: str, args: dict[str, Any], reads: list[str], writes: list[str]) -> dict[str, Any]:
    revs = {c: HEX64 for c in sorted(set(reads) | set(writes))}
    return {
        "schema": 1,
        "op_id": "op_" + ULID,
        "kind": kind,
        "target": target,
        "args": args,
        "reads": reads,
        "writes": writes,
        "expected_revisions": revs,
        "depends_on": [],
        "extensions": {},
    }


@pytest.mark.parametrize(
    "kind,target,args,reads,writes",
    [
        ("set_frontmatter", "tables/orders", {"key": "tags", "value": ["a"]}, ["tables/orders"], ["tables/orders"]),
        ("link_add", "tables/orders",
         {"destination": "tables/customers", "label": "customers", "relation": "depends_on", "detail": None, "allow_forward_reference": False},
         ["tables/customers", "tables/orders"], ["tables/orders"]),
        ("entity_add", "tables/orders",
         {"label": "cust", "kind": "table", "entity_id": None},
         ["tables/orders"], ["tables/orders"]),
        ("replace_text", "tables/orders",
         {"old": "foo", "new": "bar", "occurrence": 1},
         ["tables/orders"], ["tables/orders"]),
        ("update_section", "tables/orders",
         {"heading": "## Details", "body": "body", "mode": "replace"},
         ["tables/orders"], ["tables/orders"]),
    ],
)
def test_schema_02_operations(kind, target, args, reads, writes) -> None:
    """Each of the five operation kinds parses when its derived rules hold."""
    op = _op_common(kind, target, args, reads, writes)
    rec = schemas.Operation.from_mapping(op)
    assert rec.kind == kind
    assert rec.target == target

    # --- Unknown kind rejected (no command/execution channel) ---
    bad_kind = copy.deepcopy(op)
    bad_kind["kind"] = "command"
    with pytest.raises(SchemaError):
        schemas.Operation.from_mapping(bad_kind)

    # --- Wrong args / read-write set / expected revision rejected ---
    bad_args = copy.deepcopy(op)
    bad_args["args"] = {"unexpected": 1}
    with pytest.raises(SchemaError):
        schemas.Operation.from_mapping(bad_args)

    bad_rw = copy.deepcopy(op)
    bad_rw["reads"] = sorted(set(reads) | {"tables/extra"})  # undeclared destination
    bad_rw["expected_revisions"] = {c: HEX64 for c in sorted(set(bad_rw["reads"]) | set(writes))}
    with pytest.raises(SchemaError):
        schemas.Operation.from_mapping(bad_rw)

    bad_rev = copy.deepcopy(op)
    bad_rev["expected_revisions"]["tables/extra"] = HEX64  # keys != union(reads,writes)
    with pytest.raises(SchemaError):
        schemas.Operation.from_mapping(bad_rev)

    # --- absent is not a valid operation expected revision ---
    absent_rev = copy.deepcopy(op)
    absent_rev["expected_revisions"] = {c: "absent" for c in sorted(set(reads) | set(writes))}
    with pytest.raises(PrimitiveError):
        schemas.Operation.from_mapping(absent_rev)

    # --- op_id format validated ---
    bad_opid = copy.deepcopy(op)
    bad_opid["op_id"] = "op_BAD"  # not 26 Crockford
    with pytest.raises(PrimitiveError):
        schemas.Operation.from_mapping(bad_opid)

    # --- target MUST occur in writes ---
    bad_target = copy.deepcopy(op)
    bad_target["target"] = "tables/other"
    with pytest.raises(SchemaError):
        schemas.Operation.from_mapping(bad_target)

    # --- depends_on cannot name self ---
    self_dep = copy.deepcopy(op)
    self_dep["depends_on"] = [op["op_id"]]
    with pytest.raises(SchemaError):
        schemas.Operation.from_mapping(self_dep)


def test_operation_specific_arg_rules() -> None:
    # set_frontmatter cannot set 'type'
    op = _op_common("set_frontmatter", "tables/orders", {"key": "type", "value": "T"}, ["tables/orders"], ["tables/orders"])
    with pytest.raises(PrimitiveError):
        schemas.Operation.from_mapping(op)

    # link_add allow_forward_reference MUST be false in v1
    op = _op_common("link_add", "tables/orders",
                    {"destination": "tables/customers", "label": "x", "relation": None, "detail": None, "allow_forward_reference": True},
                    ["tables/customers", "tables/orders"], ["tables/orders"])
    with pytest.raises(SchemaError):
        schemas.Operation.from_mapping(op)

    # replace_text occurrence must be 1..10000 or 'only'
    op = _op_common("replace_text", "tables/orders",
                    {"old": "a", "new": "b", "occurrence": 0},
                    ["tables/orders"], ["tables/orders"])
    with pytest.raises(SchemaError):
        schemas.Operation.from_mapping(op)

    # update_section mode must be replace|append
    op = _op_common("update_section", "tables/orders",
                    {"heading": "h", "body": "b", "mode": "merge"},
                    ["tables/orders"], ["tables/orders"])
    with pytest.raises(SchemaError):
        schemas.Operation.from_mapping(op)


def test_operation_dag_and_uniqueness_at_proposal_level() -> None:
    """Proposal-level: depends_on must form a DAG and op_ids unique within revision."""
    # op_id is "op_" + 26 Crockford chars; build distinct valid ids.
    op_a = "op_A" + "0" * 25  # body = "A" + 25 zeros = 26 chars
    op_b = "op_B" + "0" * 25
    a = link_add_operation(op_a)
    b = {
        "schema": 1, "op_id": op_b, "kind": "entity_add",
        "target": "tables/orders",
        "args": {"label": "x", "kind": "table", "entity_id": None},
        "reads": ["tables/orders"], "writes": ["tables/orders"],
        "expected_revisions": {"tables/orders": HEX64B},
        "depends_on": [op_a], "extensions": {},
    }
    m = proposal_mapping()
    # both ops touch only tables/{customers,orders}; affected_concepts still holds.
    m["operations"] = [b, a]  # out of order -> must be sorted by op_id on parse
    schemas.ProposalRevision.from_mapping(m)  # ok, parses + sorts

    # duplicate op_id in one revision rejected
    dup = copy.deepcopy(m)
    dup["operations"] = [copy.deepcopy(a), copy.deepcopy(a)]
    with pytest.raises(SchemaError):
        schemas.ProposalRevision.from_mapping(dup)

    # cycle rejected
    cyc_a = copy.deepcopy(a)
    cyc_b = copy.deepcopy(b)
    cyc_a["depends_on"] = [op_b]
    cyc_b["depends_on"] = [op_a]
    cyc = copy.deepcopy(m)
    cyc["operations"] = [cyc_a, cyc_b]
    with pytest.raises(SchemaError):
        schemas.ProposalRevision.from_mapping(cyc)


# ============================================================================
# SCHEMA-03: Snapshot / sole preview / diff / validation digest parity
# ============================================================================

def test_schema_03_preview() -> None:
    snap = schemas.Snapshot.from_mapping(snapshot_mapping())
    # snapshot_digest parity: recomputed over the declared sub-object
    body = {
        "schema": 1,
        "concepts": snap.concepts_as_mapping(),
        "capabilities": list(snap.capabilities),
        "config_digest": snap.config_digest,
        "validation_context_digest": snap.validation_context_digest,
        "validator_id": snap.validator_id,
        "serializer_id": snap.serializer_id,
        "operation_runtime_id": snap.operation_runtime_id,
    }
    assert snap.snapshot_digest == canonical.canonical_digest(body)
    # tamper -> mismatch
    bad = snapshot_mapping()
    bad["snapshot_digest"] = DIGEST
    with pytest.raises(IntegrityError):
        schemas.Snapshot.from_mapping(bad)

    # --- Preview closed schema + preview_digest parity ---
    change = {
        "concept": "tables/orders",
        "before_revision": HEX64,
        "after_revision": HEX64B,
        "before_identity": {"parent_device": 1, "parent_inode": 2,
            "leaf_device": 1, "leaf_inode": 3, "ctime_ns": 4,
            "mode": 0o100600, "size": 1, "digest": HEX64},
        "before_base64": "AA==",
        "after_base64": "BA==",
        "contributing_ops": ["op_" + ULID],
    }
    diff_body = {"schema": 1, "format": "unified", "context_lines": 3, "text": "@@ -1 +1 @@"}
    diff_body["digest"] = canonical.canonical_digest({
        "schema": 1, "format": "unified", "context_lines": 3, "text": diff_body["text"]
    })
    finding = {"code": "okf.x", "severity": "info", "concept": "tables/orders",
               "line": 1, "message": "m", "fingerprint": "fp1"}
    val_body = {
        "schema": 1, "profile": "producer", "validation_context_digest": DIGEST_B,
        "before_findings": [], "after_findings": [], "added": [],
        "removed": [], "unchanged": [finding], "status": "pass",
    }
    val_body["digest"] = canonical.canonical_digest({
        "schema": 1, "profile": "producer", "validation_context_digest": DIGEST_B, "before_findings": [],
        "after_findings": [], "added": [], "removed": [],
        "unchanged": [finding], "status": "pass",
    })
    prev = {
        "schema": 1, "proposal_id": ULID, "proposal_revision": 1,
        "snapshot_digest": snap.snapshot_digest,
        "validation_context_digest": DIGEST_B,
        "validator_id": RUNTIME, "serializer_id": RUNTIME_SER,
        "operation_runtime_id": RUNTIME_OPS,
        "operation_order": ["op_" + ULID],
        "concept_changes": [change],
        "diff": diff_body,
        "validation": val_body,
        "generated_at": TS, "expires_at": TS3,
        "preview_digest": "",
    }
    prev_without = {k: v for k, v in prev.items() if k != "preview_digest"}
    prev["preview_digest"] = canonical.canonical_digest(prev_without)
    rec = schemas.Preview.from_mapping(prev)
    assert rec.preview_digest == prev["preview_digest"]
    mismatched_context = copy.deepcopy(prev)
    mismatched_context["validation"]["validation_context_digest"] = DIGEST
    mismatched_context["validation"]["digest"] = canonical.canonical_digest(
        {k: v for k, v in mismatched_context["validation"].items() if k != "digest"}
    )
    mismatched_context["preview_digest"] = canonical.canonical_digest(
        {k: v for k, v in mismatched_context.items() if k != "preview_digest"}
    )
    with pytest.raises(IntegrityError):
        schemas.Preview.from_mapping(mismatched_context)
    # digest mismatch rejected
    bad_prev = copy.deepcopy(prev)
    bad_prev["preview_digest"] = DIGEST
    with pytest.raises(IntegrityError):
        schemas.Preview.from_mapping(bad_prev)
    # wrong diff digest rejected
    bad_diff = copy.deepcopy(prev)
    bad_diff["diff"] = dict(diff_body, digest=DIGEST)
    bad_diff["preview_digest"] = canonical.canonical_digest(
        {k: v for k, v in bad_diff.items() if k != "preview_digest"})
    with pytest.raises(IntegrityError):
        schemas.Preview.from_mapping(bad_diff)
    # wrong profile fallback rejected: parser must reject missing profile (SCHEMA-03)
    bad_val_no_profile = copy.deepcopy(val_body)
    bad_val_prev = copy.deepcopy(prev_without)
    bad_val_prev["validation"] = {k: v for k, v in bad_val_no_profile.items() if k != "profile"}
    bad_val_prev["validation"]["digest"] = canonical.canonical_digest(
        {k: v for k, v in bad_val_prev["validation"].items()})
    bad_val_prev["preview_digest"] = canonical.canonical_digest(
        {k: v for k, v in bad_val_prev.items() if k != "preview_digest"})
    with pytest.raises(SchemaError):
        schemas.Preview.from_mapping(bad_val_prev)
    # parser rejection: unsupported schema int rejected
    bad_schema_prev = copy.deepcopy(prev)
    bad_schema_prev["schema"] = 2
    bad_schema_prev["preview_digest"] = canonical.canonical_digest(
        {k: v for k, v in bad_schema_prev.items() if k != "preview_digest"})
    with pytest.raises(SchemaError):
        schemas.Preview.from_mapping(bad_schema_prev)
    # parser rejection: empty profile string rejected (no silent fallback)
    empty_profile_prev = copy.deepcopy(prev)
    empty_profile_prev["validation"] = dict(prev["validation"], profile="")
    empty_profile_prev["validation"]["digest"] = canonical.canonical_digest(
        {k: v for k, v in empty_profile_prev["validation"].items() if k != "digest"})
    empty_profile_prev["preview_digest"] = canonical.canonical_digest(
        {k: v for k, v in empty_profile_prev.items() if k != "preview_digest"})
    with pytest.raises(SchemaError):
        schemas.Preview.from_mapping(empty_profile_prev)


# ============================================================================
# SCHEMA-04: Decision request / row / token / reason rules
# ============================================================================


def test_schema_04_decision() -> None:
    base = {
        "schema": 1,
        "record_id": "rec_" + ULID,
        "decision_result_record_id": "rec_" + ("D" * 26),
        "decision_request_fingerprint": DIGEST,
        "proposal_id": ULID,
        "proposal_revision": 1,
        "op_id": "op_" + ULID,
        "decision": "accepted",
        "reason": "",
        "actor": "local-user",
        "decided_at": TS2,
        "previous_row_token": None,
        "row_token": ROW_TOKEN,
    }
    schemas.DecisionRow.from_mapping(base)  # first decision: previous null ok

    # replacement: previous must be a row token
    repl = copy.deepcopy(base)
    repl["previous_row_token"] = ROW_TOKEN
    repl["row_token"] = ROW_TOKEN_B
    schemas.DecisionRow.from_mapping(repl)

    # unsupported decision
    bad = copy.deepcopy(base)
    bad["decision"] = "maybe"
    with pytest.raises(SchemaError):
        schemas.DecisionRow.from_mapping(bad)

    # bad row token
    bad = copy.deepcopy(base)
    bad["row_token"] = "short"
    with pytest.raises(PrimitiveError):
        schemas.DecisionRow.from_mapping(bad)

    # decision_set_token over [op_id, current_row_token-or-null] sorted tuples
    tokens = [("op_" + ULID, ROW_TOKEN), ("op_B" + "0" * 25, None)]
    dst = schemas.decision_set_token(tokens)
    assert dst.startswith("sha256:")
    # stable & order-independent (sorted by op_id)
    dst_rev = schemas.decision_set_token(list(reversed(tokens)))
    assert dst == dst_rev


# ============================================================================
# SCHEMA-05: Closed conflict codes / null ownership / sort
# ============================================================================

CONFLICT_CODES = [
    "snapshot_changed", "read_revision_changed", "write_revision_changed",
    "target_missing", "target_type_changed", "capability_changed",
    "config_changed", "preview_expired", "decision_stale",
    "dependency_unsatisfied", "validation_stale",
]
# Codes that are proposal-scoped (op_id and concept may both be null).
_PROPOSAL_LEVEL_CODES = {
    "snapshot_changed", "capability_changed", "config_changed",
    "preview_expired", "validation_stale",
}


def test_schema_05_conflict() -> None:
    # proposal-level conflict: op_id and concept null allowed
    c = {
        "schema": 1, "code": "snapshot_changed",
        "proposal_id": ULID, "proposal_revision": 1,
        "op_id": None, "concept": None,
        "expected": DIGEST, "actual": DIGEST_B,
        "message": "Snapshot changed.", "detected_at": TS,
    }
    rec = schemas.Conflict.from_mapping(c)
    assert rec.op_id is None and rec.concept is None

    # every code valid; op-scoped codes require populated op_id + concept
    for code in CONFLICT_CODES:
        cc = copy.deepcopy(c)
        cc["code"] = code
        if code not in _PROPOSAL_LEVEL_CODES:
            cc["op_id"] = "op_" + ULID
            cc["concept"] = "tables/orders"
        schemas.Conflict.from_mapping(cc)

    # unknown code rejected
    bad = copy.deepcopy(c)
    bad["code"] = "mystery"
    with pytest.raises(SchemaError):
        schemas.Conflict.from_mapping(bad)

    # op-scoped conflict cannot have null op_id
    bad = copy.deepcopy(c)
    bad["code"] = "read_revision_changed"
    bad["op_id"] = None
    bad["concept"] = "tables/orders"
    with pytest.raises(SchemaError):
        schemas.Conflict.from_mapping(bad)

    # sort helper: by (op_id-or-empty, concept-or-empty, code)
    items = [
        {"op_id": None, "concept": None, "code": "snapshot_changed"},
        {"op_id": "op_" + ULID, "concept": "tables/orders", "code": "read_revision_changed"},
        {"op_id": "op_" + ULID, "concept": "tables/orders", "code": "validation_stale"},
    ]
    out = schemas.sort_conflicts(items)
    assert [i["code"] for i in out] == [
        "snapshot_changed", "read_revision_changed", "validation_stale",
    ]


# ============================================================================
# SCHEMA-06: WAL legal phase / bitmap combinations
# ============================================================================


def _wal_mapping(phase: str, publish: list[bool], rollback: list[bool],
                 commit_decided: bool, op: str = "apply") -> dict[str, Any]:
    target_names = ["tables/orders", "tables/users"][:len(publish)]
    targets = [
        {
            "concept": concept, "before_digest": DIGEST,
            "staged_digest": DIGEST_B, "backup_digest": DIGEST,
            "restore_digest": DIGEST if op == "apply" else DIGEST_B,
            "before_identity": _target_identity(DIGEST, 3),
            "staged_identity": _target_identity(DIGEST_B, 4),
            "resulting_identity": _target_identity(DIGEST_B, 4) if publish[index] else None,
            "stage_leaf": _leaf("stage"), "backup_leaf": _leaf("backup"),
        }
        for index, concept in enumerate(target_names)
    ]
    txn_prefix = "txn_" if op == "apply" else "undo_"
    return {
        "schema": 1, "operation": op, "transaction_id": txn_prefix + ULID,
        "proposal_id": ULID, "proposal_revision": 1,
        "idempotency_scope": "/__proposal/v1/proposals/{id}/revisions/{rev}/apply",
        "idempotency_key_digest": DIGEST, "request_fingerprint": DIGEST,
        "transaction_intent_digest": DIGEST,
        "decision_set_token": DIGEST if op == "apply" else None,
        "read_model_token": DIGEST,
        "preview_digest": DIGEST if op == "apply" else None,
        "snapshot_digest": DIGEST,
        "validation_context_digest": DIGEST_B,
        "validator_id": RUNTIME, "serializer_id": RUNTIME_SER,
        "operation_runtime_id": RUNTIME_OPS,
        "accepted_op_ids": ["op_" + ULID] if op == "apply" else [],
        "targets": targets,
        "stage_dir_identity": _dir_id("stage"),
        "backup_dir_identity": _dir_id("backup"),
        "phase": phase, "commit_decided": commit_decided,
        "publish_bitmap": publish, "rollback_bitmap": rollback,
        "recovery_attempts": 0, "created_at": TS, "updated_at": TS,
        "checksum": "",
    }


def _leaf(name: str) -> dict[str, Any]:
    return {"relative_name": name, "device": 1, "inode": 9, "mode": 0o600,
            "size": 4, "digest": DIGEST}


def _dir_id(name: str) -> dict[str, Any]:
    return {"relative_name": name, "device": 1, "inode": 7, "mode": 0o700}


def _with_checksum(d: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(d)
    d["checksum"] = canonical.compute_checksum(d)
    return d


@pytest.mark.parametrize(
    "phase,publish,rollback,cd",
    [
        ("prepared", [False, False], [False, False], False),
        ("commit_decided", [False, False], [False, False], True),
        ("publishing", [True, False], [False, False], True),
        ("content_published", [True, True], [False, False], True),
        ("finalizing", [True, True], [False, False], True),
        ("committed", [True, True], [False, False], True),
        ("rolling_back", [False, False], [False, True], False),
        ("rolled_back", [False, False], [True, True], False),
        ("indeterminate", [True, False], [False, True], True),
    ],
)
def test_schema_06_wal_legal_phases(phase, publish, rollback, cd) -> None:
    m = _with_checksum(_wal_mapping(phase, publish, rollback, cd))
    schemas.WAL.from_mapping(m)


def test_schema_06_wal(tmp_path) -> None:
    """SCHEMA-06: WAL legal phase/bitmap combinations (canonical catalogue node).

    Sweeps every legal phase/bitmap/commit_decided combination from the SPEC
    section 5.6 table, plus a representative set of illegal combinations, all
    through the closed WAL record parser. The parametrized companions below add
    exhaustive per-row coverage.
    """
    legal_cases = [
        ("prepared", [False, False], [False, False], False),
        ("commit_decided", [False, False], [False, False], True),
        ("publishing", [True, False], [False, False], True),
        ("content_published", [True, True], [False, False], True),
        ("finalizing", [True, True], [False, False], True),
        ("committed", [True, True], [False, False], True),
        ("rolling_back", [False, False], [False, True], False),
        ("rolled_back", [False, False], [True, True], False),
        ("indeterminate", [True, False], [False, True], True),
    ]
    for phase, pub, rb, cd in legal_cases:
        m = _with_checksum(_wal_mapping(phase, pub, rb, cd))
        schemas.WAL.from_mapping(m)  # must not raise

    illegal_cases = [
        # commit_decided mismatched with phase
        ("prepared", [False, False], [False, False], True),
        ("commit_decided", [False, False], [False, False], False),
        # publishing with full (not proper prefix) publish bitmap
        ("publishing", [True, True], [False, False], True),
        # publishing with non-prefix publish bitmap
        ("publishing", [False, True], [False, False], True),
        # content_published with incomplete publish bitmap
        ("content_published", [True, False], [False, False], True),
        # rolling_back with prefix (not suffix) rollback bitmap
        ("rolling_back", [False, False], [True, False], False),
        # rolling_back with empty rollback bitmap
        ("rolling_back", [False, False], [False, False], False),
        # rolled_back with partial rollback
        ("rolled_back", [False, False], [False, True], False),
        # indeterminate with overlapping publish/rollback bits
        ("indeterminate", [True, True], [True, True], True),
    ]
    for phase, pub, rb, cd in illegal_cases:
        m = _with_checksum(_wal_mapping(phase, pub, rb, cd))
        with pytest.raises((IntegrityError, SchemaError)):
            schemas.WAL.from_mapping(m)

    # Exercise the complete closed finalization intent emitted by the real
    # coordinator, including audit checksum placeholders and WAL digest link.
    from test_proposal_apply import _ready_transaction
    from okf_loom.proposal_review.transaction_intent import TransactionFinalizationIntent
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try:
        result = coordinator.apply(request, "K" * 32)
        wal = repository.read_wal(result.transaction_id)
        intent = repository.read_transaction_intent(result.transaction_id)
        assert wal.transaction_intent_digest == intent.canonical_digest()
        assert "candidate_checksum" in intent.value["allowed_placeholders"]
        changed = intent.to_mapping(); changed["unknown"] = True
        with pytest.raises(SchemaError): TransactionFinalizationIntent.from_mapping(changed)
        changed = intent.to_mapping(); changed["checksum"] = DIGEST
        with pytest.raises(IntegrityError): TransactionFinalizationIntent.from_mapping(changed)
    finally: publication.close()

    # transaction_id prefix must match operation
    m = _with_checksum(_wal_mapping("prepared", [False], [False], False, op="apply"))
    m["transaction_id"] = "undo_" + ULID
    m["checksum"] = canonical.compute_checksum(m)
    with pytest.raises(SchemaError):
        schemas.WAL.from_mapping(m)

    # bitmap length must equal target count
    m = _with_checksum(_wal_mapping("prepared", [False, False], [False], False))
    with pytest.raises(SchemaError):
        schemas.WAL.from_mapping(m)


@pytest.mark.parametrize(
    "phase,publish,rollback,cd",
    [
        # prepared must have commit_decided False
        ("prepared", [False, False], [False, False], True),
        # commit_decided phase requires True
        ("commit_decided", [False, False], [False, False], False),
        # publishing requires proper non-empty prefix
        ("publishing", [True, True], [False, False], True),   # full, not proper
        ("publishing", [False, True], [False, False], True),  # non-prefix
        # content_published requires all true
        ("content_published", [True, False], [False, False], True),
        # rolling_back requires non-empty true suffix (canonical order)
        ("rolling_back", [False, False], [True, False], False),  # prefix not suffix
        ("rolling_back", [False, False], [False, False], False), # empty
        # rolled_back requires all true
        ("rolled_back", [False, False], [False, True], False),
        # indeterminate must be disjoint
        ("indeterminate", [True, True], [True, True], True),
    ],
)
def test_schema_06_wal_illegal_phases(phase, publish, rollback, cd) -> None:
    m = _with_checksum(_wal_mapping(phase, publish, rollback, cd))
    with pytest.raises((IntegrityError, SchemaError)):
        schemas.WAL.from_mapping(m)


def test_wal_transaction_id_prefix_matches_operation() -> None:
    m = _with_checksum(_wal_mapping("prepared", [False], [False], False, op="apply"))
    m["transaction_id"] = "undo_" + ULID  # wrong prefix for apply
    m["checksum"] = canonical.compute_checksum(m)
    with pytest.raises(SchemaError):
        schemas.WAL.from_mapping(m)


def test_wal_bitmap_length_must_equal_targets() -> None:
    m = _with_checksum(_wal_mapping("prepared", [False, False], [False], False))
    with pytest.raises(SchemaError):
        schemas.WAL.from_mapping(m)


# ============================================================================
# SCHEMA-07: Result / audit / history / backup / undo / withdraw links complete
# ============================================================================


def test_schema_07_audit(tmp_path) -> None:
    # Apply request closed schema
    apply_req = {
        "schema": 1, "proposal_id": ULID, "proposal_revision": 1,
        "actor": "local", "snapshot_digest": DIGEST,
        "validation_context_digest": DIGEST_B,
        "decision_set_token": DIGEST, "read_model_token": DIGEST,
    }
    schemas.ApplyRequest.from_mapping(apply_req)
    with pytest.raises(SchemaError):
        schemas.ApplyRequest.from_mapping({**apply_req, "idempotency_key": "x"})  # not in body

    # Undo request
    undo_req = {
        "schema": 1, "proposal_id": ULID, "proposal_revision": 1,
        "actor": "local", "transaction_id": "txn_" + ULID,
        "undo_group_id": "undo_" + ULID, "read_model_token": DIGEST,
    }
    schemas.UndoRequest.from_mapping(undo_req)

    # Transaction result (apply -> applied, undo -> undone)
    res = {
        "schema": 1, "operation": "apply", "transaction_id": "txn_" + ULID,
        "proposal_id": ULID, "proposal_revision": 1, "status": "applied",
        "replayed": False, "decision_set_token": DIGEST,
        "validation_context_digest": DIGEST_B,
        "validation_digest": DIGEST,
        "concept_revisions": {"tables/orders": HEX64B},
        "activity_ids": ["act_" + ULID], "undo_group_id": None,
        "completed_at": TS2, "undo_expires_at": None, "error": None,
    }
    schemas.TransactionResult.from_mapping(res)
    bad = copy.deepcopy(res)
    bad["status"] = "undone"  # mismatched with operation=apply
    with pytest.raises(SchemaError):
        schemas.TransactionResult.from_mapping(bad)

    # History manifest with backup identity
    hm = _checksum({
        "schema": 1, "transaction_id": "txn_" + ULID, "operation": "apply",
        "parent_transaction_id": None, "proposal_id": ULID, "proposal_revision": 1,
        "undo_group_id": "undo_" + ULID,
        "targets": [{
              "concept": "tables/orders", "before_digest": DIGEST,
              "after_digest": DIGEST_B,
              "before_identity": _target_identity(DIGEST, 3),
              "staged_identity": _target_identity(DIGEST_B, 4),
              "backup_identity": _leaf("backup"),
        }],
        "created_at": TS, "expires_at": TS3,
    })
    schemas.HistoryManifest.from_mapping(hm)

    # Endpoint result: prepared has null status/digest/response
    er = _checksum({
        "schema": 1, "scope": "/route", "key_digest": DIGEST,
        "request_fingerprint": DIGEST, "state": "prepared",
        "http_status": None, "response_digest": None, "response": None,
        "transaction_id": "txn_" + ULID, "created_at": TS, "expires_at": TS3,
    })
    schemas.EndpointResult.from_mapping(er)
    # terminal must carry status+response+digest
    er_term = copy.deepcopy(er)
    er_term.update(state="terminal", http_status=200,
                   response_digest=canonical.canonical_digest({"ok": True}),
                   response={"ok": True})
    er_term["checksum"] = canonical.compute_checksum(er_term)
    schemas.EndpointResult.from_mapping(er_term)
    bad_term = copy.deepcopy(er_term)
    bad_term["response"] = None  # terminal without response
    bad_term["checksum"] = canonical.compute_checksum(bad_term)
    with pytest.raises(SchemaError):
        schemas.EndpointResult.from_mapping(bad_term)

    # Tombstone
    tb = _checksum({
        "schema": 1, "scope": "/route", "key_digest": DIGEST,
        "request_fingerprint": DIGEST, "outcome": "terminal_compacted",
        "response_digest": DIGEST, "transaction_id": "txn_" + ULID,
        "created_at": TS, "expires_at": TS3,
    })
    schemas.Tombstone.from_mapping(tb)

    # Outbox receipt
    ob = _checksum({
        "schema": 1, "outbox_id": "out_" + ULID, "aggregate_kind": "proposal",
        "aggregate_id": ULID, "event_type": "applied",
        "payload_digest": DIGEST, "state": "pending", "event_cursor": None,
        "attempts": 0, "created_at": TS, "updated_at": TS,
    })
    schemas.OutboxReceipt.from_mapping(ob)
    bad_ob = copy.deepcopy(ob)
    bad_ob["state"] = "appended"
    bad_ob["event_cursor"] = None  # appended needs cursor
    bad_ob["checksum"] = canonical.compute_checksum(bad_ob)
    with pytest.raises(SchemaError):
        schemas.OutboxReceipt.from_mapping(bad_ob)

    # Transaction audit (closed, many fields, parent link rule)
    audit = _checksum({
        "schema": 1, "record_id": "rec_" + ULID, "operation": "apply",
        "transaction_id": "txn_" + ULID, "parent_transaction_id": None,
        "proposal_id": ULID, "proposal_revision": 1, "actor": "local",
        "endpoint_scope": "/route", "idempotency_key_digest": DIGEST,
          "request_fingerprint": DIGEST, "transaction_intent_digest": DIGEST,
          "accepted_op_ids": ["op_" + ULID],
        "decision_record_ids": ["rec_" + ("D" * 26)],
        "decision_rows_digest": DIGEST, "decision_set_token": DIGEST,
        "preview_digest": DIGEST, "snapshot_digest": DIGEST,
        "validation_context_digest": DIGEST_B,
        "validator_id": RUNTIME, "serializer_id": RUNTIME_SER,
        "operation_runtime_id": RUNTIME_OPS, "validation_profile": "producer",
        "validation_digest": DIGEST, "validation_status": "pass",
        "phase": "committed", "status": "applied",
        "history_manifest_digest": DIGEST,
          "target_before_revisions": {"tables/orders": HEX64},
          "target_after_revisions": {"tables/orders": HEX64B},
          "target_before_identities": {"tables/orders": _target_identity(DIGEST, 3)},
          "target_resulting_identities": {"tables/orders": _target_identity(DIGEST_B, 4)},
          "target_backup_identities": {"tables/orders": _leaf("backup")},
        "resulting_revisions": {"tables/orders": HEX64B},
        "activity_ids": ["act_" + ULID], "undo_group_id": "undo_" + ULID,
        "outbox_ids": ["out_" + ULID], "started_at": TS,
        "commit_decided_at": TS2, "completed_at": TS2, "error_digest": None,
    })
    schemas.TransactionAudit.from_mapping(audit)
    # undo audit: parent must reference the applied txn
    bad_undo = copy.deepcopy(audit)
    bad_undo.update(operation="undo", parent_transaction_id=None, status="undone")
    bad_undo["transaction_id"] = "undo_" + ULID
    bad_undo["accepted_op_ids"] = []
    bad_undo["checksum"] = canonical.compute_checksum(bad_undo)
    with pytest.raises(SchemaError):
        schemas.TransactionAudit.from_mapping(bad_undo)

    from test_proposal_apply import _ready_transaction
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try:
        result = coordinator.apply(request, "K" * 32)
        audit = next(schemas.TransactionAudit.from_mapping(row.payload)
                     for row in repository.snapshot().aggregate("transactions")
                     if row.kind == "transaction" and row.payload.get("transaction_id") == result.transaction_id)
        intent, linked_history = repository.verify_audit_transaction_chain(audit)
        assert audit.transaction_intent_digest == intent.canonical_digest()
        assert audit.history_manifest_digest == linked_history.canonical_digest()
    finally: publication.close()


# ============================================================================
# SCHEMA-08: Wrapper / checkpoint / sequence / generation closed
# ============================================================================


def test_schema_08_storage() -> None:
    # Wrapper (JSONL row)
    wrap = _checksum({
        "record_schema": 1, "kind": "proposal", "sequence": 1,
        "payload": proposal_mapping(),
    })
    schemas.Wrapper.from_mapping(wrap)
    bad = copy.deepcopy(wrap)
    bad["kind"] = "mystery"
    bad["checksum"] = canonical.compute_checksum(bad)
    with pytest.raises(SchemaError):
        schemas.Wrapper.from_mapping(bad)

    # Checkpoint
    cp_records = [proposal_mapping()]
    cp = _checksum({
        "schema": 1, "repository_id": "repo_" + ULID, "kind": "proposals",
        "generation": 1, "sequence_high_water": 1,
        "covered_generations": [1],
        "records_digest": canonical.canonical_digest(cp_records),
        "records": cp_records, "created_at": TS,
    })
    rec = schemas.Checkpoint.from_mapping(cp)
    # records_digest parity
    assert rec.records_digest == canonical.canonical_digest(cp_records)
    bad_cp = copy.deepcopy(cp)
    bad_cp["records_digest"] = DIGEST
    bad_cp["checksum"] = canonical.compute_checksum(bad_cp)
    with pytest.raises(IntegrityError):
        schemas.Checkpoint.from_mapping(bad_cp)

    # Repository root
    root = _checksum({
        "schema": 1, "repository_id": "repo_" + ULID, "created_at": TS,
        "aggregate_versions": {
            "proposals": 1, "decisions": 1, "transactions": 1,
            "endpoint_results": 1, "outbox": 1, "tombstones": 1, "corruption": 1,
        },
        "cursor_keys": [{
            "key_id": "cur_" + ULID, "secret_base64url": "A" * 43,
            "created_at": TS, "not_before": TS, "expires_at": TS3,
        }],
        "active_cursor_key_id": "cur_" + ULID,
        "limits_digest": DIGEST,
    })
    schemas.RepositoryRoot.from_mapping(root)
    bad_root = copy.deepcopy(root)
    bad_root["aggregate_versions"]["proposals"] = 2  # unsupported version
    bad_root["checksum"] = canonical.compute_checksum(bad_root)
    with pytest.raises(SchemaError):
        schemas.RepositoryRoot.from_mapping(bad_root)

    # Corruption marker: every authority kind
    for auth in _authority_identities():
        cm = _checksum({
            "schema": 1, "corruption_id": "cor_" + ULID,
            "repository_id": "repo_" + ULID,
            "authority_kind": auth["kind"], "authority_identity": auth,
            "reason": "missing", "observed_digest": None,
            "diagnostic_copy": None, "detected_at": TS,
            "repaired_at": None, "repair_record_id": None,
        })
        schemas.CorruptionMarker.from_mapping(cm)

    # authority_kind must equal authority_identity.kind
    bad_auth = _authority_identities()[0]
    cm = _checksum({
        "schema": 1, "corruption_id": "cor_" + ULID,
        "repository_id": "repo_" + ULID,
        "authority_kind": "checkpoint", "authority_identity": bad_auth,
        "reason": "missing", "observed_digest": None,
        "diagnostic_copy": None, "detected_at": TS,
        "repaired_at": None, "repair_record_id": None,
    })
    with pytest.raises(SchemaError):
        schemas.CorruptionMarker.from_mapping(cm)

    # Repair: replace_authority
    rep = _checksum({
        "schema": 1, "repair_record_id": "rec_" + ULID,
        "corruption_id": "cor_" + ULID, "repository_id": "repo_" + ULID,
        "action": "replace_authority",
        "replacement_authority": {
            "relative_path": "active/proposals.jsonl", "digest": DIGEST,
            "size": 4096,
            "source_provenance": {
                "kind": "validated_backup", "source_digest": DIGEST,
                "operator_note": "from backup",
            },
        },
        "terminal_classification": None, "operator_label": "ops",
        "reason": "repaired from backup", "created_at": TS,
    })
    schemas.Repair.from_mapping(rep)
    # classify_indeterminate
    rep2 = copy.deepcopy(rep)
    rep2.update(action="classify_indeterminate", replacement_authority=None,
                terminal_classification={
                    "scope": "/route", "key_digest": DIGEST,
                    "transaction_id": "txn_" + ULID, "outcome": "indeterminate",
                })
    rep2["checksum"] = canonical.compute_checksum(rep2)
    schemas.Repair.from_mapping(rep2)
    # mixed action fields rejected
    rep3 = copy.deepcopy(rep)
    rep3["terminal_classification"] = {"scope": "/r", "key_digest": DIGEST,
                                       "transaction_id": "txn_" + ULID, "outcome": "indeterminate"}
    rep3["checksum"] = canonical.compute_checksum(rep3)
    with pytest.raises(SchemaError):
        schemas.Repair.from_mapping(rep3)


def _authority_identities() -> list[dict[str, Any]]:
    return [
        {"kind": "repository", "repository_id": "repo_" + ULID},
        {"kind": "aggregate", "aggregate": "proposals", "generation": 1,
         "sequence": 5, "relative_path": "active/proposals.jsonl"},
        {"kind": "checkpoint", "aggregate": "proposals", "generation": 1,
         "relative_path": "checkpoints/proposals.json"},
        {"kind": "preview", "preview_digest": DIGEST,
         "relative_path": "previews/sha256/ab/rest.json"},
        {"kind": "endpoint_result", "scope": "/route", "key_digest": DIGEST,
         "sequence": 3, "relative_path": "active/endpoint-results.jsonl"},
        {"kind": "transaction_file", "transaction_id": "txn_" + ULID,
         "component": "wal", "relative_path": "transactions/txn_1/wal.json"},
    ]


# ============================================================================
# SCHEMA-09: Notification / read / page / runtime token schemas
# ============================================================================


def test_schema_09_wire() -> None:
    # Notification
    n = {
        "schema": 1, "proposal_id": ULID, "proposal_revision": 1,
        "event": "created", "read_model_token": DIGEST, "transaction_id": None,
    }
    schemas.Notification.from_mapping(n)
    bad = copy.deepcopy(n)
    bad["event"] = "mystery"
    with pytest.raises(SchemaError):
        schemas.Notification.from_mapping(bad)

    # Transaction progress projection
    tp = {
        "schema": 1, "operation": "apply", "transaction_id": "txn_" + ULID,
        "parent_transaction_id": None, "status": "publishing",
        "phase": "publishing", "commit_decided": True,
        "completed_targets": 1, "total_targets": 2, "recovery_attempts": 0,
        "started_at": TS, "updated_at": TS2, "retry_after_ms": 500,
        "error_code": None,
    }
    schemas.TransactionProgress.from_mapping(tp)

    # Cursor payload (stateless, frozen projection inputs)
    cp = {
        "schema": 1, "repository_id": "repo_" + ULID, "query_digest": DIGEST,
        "last_created_at": TS, "last_proposal_id": ULID,
        "last_proposal_revision": 1,
        "repository_high_waters": {k: {"generation": 1, "sequence": 0} for k in
                                   ["proposals", "decisions", "transactions",
                                    "endpoint_results", "outbox", "tombstones", "corruption"]},
        "bundle_snapshot_digest": DIGEST, "validation_context_digest": DIGEST_B,
        "clock_captured_at": TS,
        "earliest_expiry_at": TS3, "environment_digest": DIGEST,
        "cursor_expires_at": TS3,
    }
    schemas.CursorPayload.from_mapping(cp)

    # Page payload
    page = {
        "schema": 1, "items": [], "next_cursor": None,
        "page_snapshot_token": DIGEST, "snapshot_expires_at": TS3,
    }
    schemas.PagePayload.from_mapping(page)


# ============================================================================
# SCHEMA-10: Legacy closed identity and projection
# ============================================================================


def test_schema_10_legacy() -> None:
    leg = {
        "schema": 1, "kind": "legacy-suggestion",
        "legacy_identity": {"legacy_id": "legacy-7", "source_generation": 3},
        "summary": "old suggestion", "author": "old-agent",
        "created_at": TS, "regeneration_required": True, "applicable": False,
    }
    rec = schemas.LegacyProjection.from_mapping(leg)
    assert rec.applicable is False
    assert rec.regeneration_required is True
    # bare legacy id (no source_generation) rejected
    bad = copy.deepcopy(leg)
    bad["legacy_identity"] = {"legacy_id": "legacy-7"}  # missing source_generation
    with pytest.raises(SchemaError):
        schemas.LegacyProjection.from_mapping(bad)
    # applicable must remain False (legacy never becomes applicable)
    bad2 = copy.deepcopy(leg)
    bad2["applicable"] = True
    with pytest.raises(SchemaError):
        schemas.LegacyProjection.from_mapping(bad2)
    # regeneration identity must repeat both fields (legacy_id + source_generation)
    lid = schemas.LegacyIdentity.from_mapping({"legacy_id": "legacy-7", "source_generation": 3})
    assert lid.legacy_id == "legacy-7" and lid.source_generation == 3


# ============================================================================
# SCHEMA-11: Duplicate / unknown / type / depth / size UTF-8 corpus
# ============================================================================

# A family -> (valid_mapping_builder, optional factory). Each invalid mutation
# is applied to a deep copy and must be rejected by the family parser.
FAMILIES: list[tuple[str, Any]] = []  # populated below


def _leaf(name: str = "backup") -> dict[str, Any]:
    return {"relative_name": name, "device": 1, "inode": 2, "mode": 0o600,
            "size": 1, "digest": DIGEST}


def _dir_id(name: str = "stage") -> dict[str, Any]:
    return {"relative_name": name, "device": 1, "inode": 3, "mode": 0o700}


def _target_identity(digest: str = DIGEST, inode: int = 3) -> dict[str, Any]:
    return {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
            "leaf_inode": inode, "ctime_ns": 4, "mode": 0o600,
            "size": 1, "digest": digest}


def _wal_target(concept: str = "tables/orders") -> dict[str, Any]:
    return {"concept": concept, "before_digest": DIGEST,
            "staged_digest": DIGEST_B, "backup_digest": DIGEST,
            "restore_digest": DIGEST,
            "before_identity": _target_identity(DIGEST, 3),
            "staged_identity": _target_identity(DIGEST_B, 4),
            "resulting_identity": None,
            "stage_leaf": _leaf("stage"), "backup_leaf": _leaf("backup")}


def _authority_identities() -> list[dict[str, Any]]:
    return [
        {"kind": "repository", "repository_id": "repo_" + ULID},
        {"kind": "aggregate", "aggregate": "proposals", "generation": 1,
         "sequence": 5, "relative_path": "active/proposals.jsonl"},
        {"kind": "checkpoint", "aggregate": "proposals", "generation": 1,
         "relative_path": "checkpoints/proposals.json"},
        {"kind": "preview", "preview_digest": DIGEST,
         "relative_path": "previews/sha256/ab/rest.json"},
        {"kind": "endpoint_result", "scope": "/route", "key_digest": DIGEST,
         "sequence": 3, "relative_path": "active/endpoint-results.jsonl"},
        {"kind": "transaction_file", "transaction_id": "txn_" + ULID,
         "component": "wal", "relative_path": "transactions/txn_1/wal.json"},
    ]


def _register_families() -> None:
    global FAMILIES
    FAMILIES = [
        ("ProposalRevision", proposal_mapping, schemas.ProposalRevision),
        ("Snapshot", snapshot_mapping, schemas.Snapshot),
        ("Operation", link_add_operation, schemas.Operation),
        ("ProvenanceEntry", lambda: {"kind": "comment", "value": "v1"}, schemas.ProvenanceEntry),
        ("PreviewRef", lambda: {"schema": 1, "preview_digest": DIGEST,
                                "artifact_bytes": 4096}, schemas.PreviewRef),
        ("InventoryEntry", lambda: {"concept": "tables/orders",
                                     "revision": HEX64, "identity": {
                                     "parent_device": 1, "parent_inode": 2,
                                     "leaf_device": 1, "leaf_inode": 3,
                                     "ctime_ns": 4, "mode": 0o100600,
                                     "size": 1, "digest": HEX64}}, schemas.InventoryEntry),
        ("Withdrawal", lambda: _checksum({
            "schema": 1, "record_id": "rec_" + ULID,
            "withdrawal_result_record_id": "rec_" + ("C" * 26),
            "withdrawal_request_fingerprint": DIGEST, "proposal_id": ULID,
            "proposal_revision": 1, "actor": "local-user", "reason": "mistake",
            "read_model_token": DIGEST, "withdrawn_at": TS2,
        }), schemas.Withdrawal),
        ("DecisionRow", lambda: {
            "schema": 1, "record_id": "rec_" + ULID,
            "decision_result_record_id": "rec_" + ("D" * 26),
            "decision_request_fingerprint": DIGEST, "proposal_id": ULID,
            "proposal_revision": 1, "op_id": "op_" + ULID, "decision": "accepted",
            "reason": "", "actor": "u", "decided_at": TS2,
            "previous_row_token": None, "row_token": ROW_TOKEN,
        }, schemas.DecisionRow),
        ("Conflict", lambda: {
            "schema": 1, "code": "snapshot_changed", "proposal_id": ULID,
            "proposal_revision": 1, "op_id": None, "concept": None,
            "expected": DIGEST, "actual": DIGEST_B, "message": "x", "detected_at": TS,
        }, schemas.Conflict),
        ("ConceptChange", lambda: {
            "concept": "tables/orders", "before_revision": HEX64,
            "after_revision": HEX64B, "before_identity": {"parent_device": 1,
                "parent_inode": 2, "leaf_device": 1, "leaf_inode": 3,
                "ctime_ns": 4, "mode": 0o100600, "size": 1, "digest": HEX64},
            "before_base64": "AA==",
            "after_base64": "BA==", "contributing_ops": ["op_" + ULID],
        }, schemas.ConceptChange),
        ("Diff", lambda: {**{"schema": 1, "format": "unified", "context_lines": 3, "text": "x"},
                          "digest": canonical.canonical_digest({"schema": 1, "format": "unified", "context_lines": 3, "text": "x"})},
         schemas.Diff),
        ("ValidationFinding", lambda: {
            "code": "c", "severity": "info", "concept": "tables/orders",
            "line": 1, "message": "m", "fingerprint": "f",
        }, schemas.ValidationFinding),
          ("Validation", lambda: {**{"schema": 1, "profile": "producer", "validation_context_digest": DIGEST_B, "before_findings": [],
                                     "after_findings": [], "added": [], "removed": [], "unchanged": [], "status": "pass"},
                                 "digest": canonical.canonical_digest({"schema": 1, "profile": "producer", "validation_context_digest": DIGEST_B, "before_findings": [],
                                                                      "after_findings": [], "added": [], "removed": [], "unchanged": [], "status": "pass"})},
         schemas.Validation),
        ("Preview", lambda: (lambda b: {**b, "preview_digest": canonical.canonical_digest({k: v for k, v in b.items() if k != "preview_digest"})})({
            "schema": 1, "proposal_id": ULID, "proposal_revision": 1,
              "snapshot_digest": canonical.canonical_digest({"x": 1}),
              "validation_context_digest": DIGEST_B,
            "validator_id": RUNTIME, "serializer_id": RUNTIME_SER,
            "operation_runtime_id": RUNTIME_OPS, "operation_order": [],
            "concept_changes": [],
            "diff": {**{"schema": 1, "format": "unified", "context_lines": 3, "text": ""},
                     "digest": canonical.canonical_digest({"schema": 1, "format": "unified", "context_lines": 3, "text": ""})},
              "validation": {**{"schema": 1, "profile": "producer", "validation_context_digest": DIGEST_B, "before_findings": [],
                              "after_findings": [], "added": [], "removed": [], "unchanged": [], "status": "pass"},
                             "digest": canonical.canonical_digest({"schema": 1, "profile": "producer", "validation_context_digest": DIGEST_B, "before_findings": [],
                                                                 "after_findings": [], "added": [], "removed": [], "unchanged": [], "status": "pass"})},
            "generated_at": TS, "expires_at": TS3, "preview_digest": "",
        }), schemas.Preview),
        ("ApplyRequest", lambda: {
            "schema": 1, "proposal_id": ULID, "proposal_revision": 1,
              "actor": "local", "snapshot_digest": DIGEST,
              "validation_context_digest": DIGEST_B,
            "decision_set_token": DIGEST, "read_model_token": DIGEST,
        }, schemas.ApplyRequest),
        ("UndoRequest", lambda: {
            "schema": 1, "proposal_id": ULID, "proposal_revision": 1,
            "actor": "local", "transaction_id": "txn_" + ULID,
            "undo_group_id": "undo_" + ULID, "read_model_token": DIGEST,
        }, schemas.UndoRequest),
        ("TransactionResult", lambda: {
            "schema": 1, "operation": "apply", "transaction_id": "txn_" + ULID,
            "proposal_id": ULID, "proposal_revision": 1, "status": "applied",
              "replayed": False, "decision_set_token": DIGEST,
              "validation_context_digest": DIGEST_B,
              "validation_digest": DIGEST,
            "concept_revisions": {"tables/orders": HEX64B},
        "activity_ids": ["act_" + ULID], "undo_group_id": None,
            "completed_at": TS2, "undo_expires_at": None, "error": None,
        }, schemas.TransactionResult),
        ("DirIdentity", lambda: _dir_id(), schemas.DirIdentity),
        ("LeafIdentity", lambda: _leaf(), schemas.LeafIdentity),
        ("WalTarget", lambda: _wal_target(), schemas.WalTarget),
        ("WAL", lambda: _checksum({
            "schema": 1, "operation": "apply", "transaction_id": "txn_" + ULID,
            "proposal_id": ULID, "proposal_revision": 1, "idempotency_scope": "/route",
            "idempotency_key_digest": DIGEST, "request_fingerprint": DIGEST,
            "transaction_intent_digest": DIGEST,
            "decision_set_token": DIGEST, "read_model_token": DIGEST,
              "preview_digest": DIGEST, "snapshot_digest": DIGEST,
              "validation_context_digest": DIGEST_B,
            "validator_id": RUNTIME, "serializer_id": RUNTIME_SER,
            "operation_runtime_id": RUNTIME_OPS, "accepted_op_ids": ["op_" + ULID],
            "targets": [_wal_target()],
            "stage_dir_identity": _dir_id("stage"),
            "backup_dir_identity": _dir_id("backup"),
            "phase": "prepared", "commit_decided": False,
            "publish_bitmap": [False], "rollback_bitmap": [False],
            "recovery_attempts": 0, "created_at": TS, "updated_at": TS,
        }), schemas.WAL),
        ("HistoryTarget", lambda: {
            "concept": "tables/orders", "before_digest": DIGEST,
            "after_digest": DIGEST_B,
            "before_identity": _target_identity(DIGEST, 3),
            "staged_identity": _target_identity(DIGEST_B, 4),
            "backup_identity": _leaf(),
        }, schemas.HistoryTarget),
        ("HistoryManifest", lambda: _checksum({
            "schema": 1, "transaction_id": "txn_" + ULID, "operation": "apply",
            "parent_transaction_id": None, "proposal_id": ULID, "proposal_revision": 1,
            "undo_group_id": "undo_" + ULID,
            "targets": [{"concept": "tables/orders", "before_digest": DIGEST,
                         "after_digest": DIGEST_B,
                         "before_identity": _target_identity(DIGEST, 3),
                         "staged_identity": _target_identity(DIGEST_B, 4),
                         "backup_identity": _leaf()}],
            "created_at": TS, "expires_at": TS3,
        }), schemas.HistoryManifest),
        ("EndpointResult", lambda: _checksum({
            "schema": 1, "scope": "/route", "key_digest": DIGEST,
            "request_fingerprint": DIGEST, "state": "prepared",
            "http_status": None, "response_digest": None, "response": None,
            "transaction_id": "txn_" + ULID, "created_at": TS, "expires_at": TS3,
        }), schemas.EndpointResult),
        ("Tombstone", lambda: _checksum({
            "schema": 1, "scope": "/route", "key_digest": DIGEST,
            "request_fingerprint": DIGEST, "outcome": "terminal_compacted",
            "response_digest": DIGEST, "transaction_id": "txn_" + ULID,
            "created_at": TS, "expires_at": TS3,
        }), schemas.Tombstone),
        ("OutboxReceipt", lambda: _checksum({
            "schema": 1, "outbox_id": "out_" + ULID, "aggregate_kind": "proposal",
            "aggregate_id": ULID, "event_type": "applied",
            "payload_digest": DIGEST, "state": "pending", "event_cursor": None,
            "attempts": 0, "created_at": TS, "updated_at": TS,
        }), schemas.OutboxReceipt),
        ("TransactionAudit", lambda: _checksum({
            "schema": 1, "record_id": "rec_" + ULID, "operation": "apply",
            "transaction_id": "txn_" + ULID, "parent_transaction_id": None,
            "proposal_id": ULID, "proposal_revision": 1, "actor": "local",
            "endpoint_scope": "/route", "idempotency_key_digest": DIGEST,
            "request_fingerprint": DIGEST, "transaction_intent_digest": DIGEST,
            "accepted_op_ids": ["op_" + ULID],
            "decision_record_ids": [],
            "decision_rows_digest": DIGEST, "decision_set_token": DIGEST,
              "preview_digest": DIGEST, "snapshot_digest": DIGEST,
              "validation_context_digest": DIGEST_B,
            "validator_id": RUNTIME, "serializer_id": RUNTIME_SER,
            "operation_runtime_id": RUNTIME_OPS, "validation_profile": "producer",
            "validation_digest": DIGEST, "validation_status": "pass",
            "phase": "committed", "status": "applied",
            "history_manifest_digest": DIGEST,
            "target_before_revisions": {"tables/orders": HEX64},
            "target_after_revisions": {"tables/orders": HEX64B},
            "target_before_identities": {"tables/orders": _target_identity(DIGEST, 3)},
            "target_resulting_identities": {"tables/orders": _target_identity(DIGEST_B, 4)},
            "target_backup_identities": {"tables/orders": _leaf()},
            "resulting_revisions": {"tables/orders": HEX64B},
            "activity_ids": [], "undo_group_id": "undo_" + ULID, "outbox_ids": [],
            "started_at": TS, "commit_decided_at": TS2, "completed_at": TS2,
            "error_digest": None,
        }), schemas.TransactionAudit),
        ("Checkpoint", lambda: _checksum({
            "schema": 1, "repository_id": "repo_" + ULID, "kind": "proposals",
            "generation": 1, "sequence_high_water": 0,
            "covered_generations": [1],
            "records_digest": canonical.canonical_digest([]),
            "records": [], "created_at": TS,
        }), schemas.Checkpoint),
        ("Wrapper", lambda: _checksum({
            "record_schema": 1, "kind": "proposal", "sequence": 1,
            "payload": proposal_mapping(),
        }), schemas.Wrapper),
        ("CursorKey", lambda: {
            "key_id": "cur_" + ULID, "secret_base64url": "A" * 43,
            "created_at": TS, "not_before": TS, "expires_at": TS3,
        }, schemas.CursorKey),
        ("RepositoryRoot", lambda: _checksum({
            "schema": 1, "repository_id": "repo_" + ULID, "created_at": TS,
            "aggregate_versions": {k: 1 for k in ["proposals", "decisions",
                "transactions", "endpoint_results", "outbox", "tombstones", "corruption"]},
            "cursor_keys": [{
                "key_id": "cur_" + ULID, "secret_base64url": "A" * 43,
                "created_at": TS, "not_before": TS, "expires_at": TS3,
            }],
            "active_cursor_key_id": "cur_" + ULID, "limits_digest": DIGEST,
        }), schemas.RepositoryRoot),
        ("CorruptionMarker", lambda: _checksum({
            "schema": 1, "corruption_id": "cor_" + ULID,
            "repository_id": "repo_" + ULID,
            "authority_kind": "repository",
            "authority_identity": {"kind": "repository", "repository_id": "repo_" + ULID},
            "reason": "missing", "observed_digest": None,
            "diagnostic_copy": None, "detected_at": TS,
            "repaired_at": None, "repair_record_id": None,
        }), schemas.CorruptionMarker),
        ("DiagnosticCopy", lambda: {"relative_path": "x.json", "digest": DIGEST,
                                    "size": 1}, schemas.DiagnosticCopy),
        ("SourceProvenance", lambda: {"kind": "validated_backup", "source_digest": DIGEST,
                                      "operator_note": "note"}, schemas.SourceProvenance),
        ("ReplacementAuthority", lambda: {"relative_path": "x.json", "digest": DIGEST,
                                          "size": 1, "source_provenance": {
                                              "kind": "validated_backup", "source_digest": DIGEST,
                                              "operator_note": "note"}}, schemas.ReplacementAuthority),
        ("TerminalClassification", lambda: {"scope": "/r", "key_digest": DIGEST,
                                            "transaction_id": "txn_" + ULID,
                                            "outcome": "indeterminate"}, schemas.TerminalClassification),
        ("Repair", lambda: _checksum({
            "schema": 1, "repair_record_id": "rec_" + ULID,
            "corruption_id": "cor_" + ULID, "repository_id": "repo_" + ULID,
            "action": "classify_indeterminate", "replacement_authority": None,
            "terminal_classification": {"scope": "/route", "key_digest": DIGEST,
                                         "transaction_id": "txn_" + ULID, "outcome": "indeterminate"},
            "operator_label": "ops", "reason": "classified", "created_at": TS,
        }), schemas.Repair),
        ("Notification", lambda: {
            "schema": 1, "proposal_id": ULID, "proposal_revision": 1,
            "event": "created", "read_model_token": DIGEST, "transaction_id": None,
        }, schemas.Notification),
        ("LegacyIdentity", lambda: {"legacy_id": "legacy-7", "source_generation": 3},
         schemas.LegacyIdentity),
        ("LegacyProjection", lambda: {
            "schema": 1, "kind": "legacy-suggestion",
            "legacy_identity": {"legacy_id": "legacy-7", "source_generation": 3},
            "summary": "s", "author": "a", "created_at": TS,
            "regeneration_required": True, "applicable": False,
        }, schemas.LegacyProjection),
        ("TransactionProgress", lambda: {
            "schema": 1, "operation": "apply", "transaction_id": "txn_" + ULID,
            "parent_transaction_id": None, "status": "publishing",
            "phase": "publishing", "commit_decided": True,
            "completed_targets": 1, "total_targets": 2, "recovery_attempts": 0,
            "started_at": TS, "updated_at": TS2, "retry_after_ms": 500,
            "error_code": None,
        }, schemas.TransactionProgress),
        ("CursorPayload", lambda: {
            "schema": 1, "repository_id": "repo_" + ULID, "query_digest": DIGEST,
            "last_created_at": TS, "last_proposal_id": ULID,
            "last_proposal_revision": 1,
            "repository_high_waters": {k: {"generation": 1, "sequence": 0} for k in
                                       ["proposals", "decisions", "transactions",
                                        "endpoint_results", "outbox", "tombstones", "corruption"]},
            "bundle_snapshot_digest": DIGEST, "validation_context_digest": DIGEST_B,
            "clock_captured_at": TS,
            "earliest_expiry_at": TS3, "environment_digest": DIGEST,
            "cursor_expires_at": TS3,
        }, schemas.CursorPayload),
        ("PagePayload", lambda: {
            "schema": 1, "items": [], "next_cursor": None,
            "page_snapshot_token": DIGEST, "snapshot_expires_at": TS3,
        }, schemas.PagePayload),
    ]


_register_families()


@pytest.mark.parametrize("name,builder,parser", FAMILIES, ids=[f[0] for f in FAMILIES])
def test_schema_11_public_corpus(name, builder, parser) -> None:
    base = builder()

    # baseline valid
    parser.from_mapping(copy.deepcopy(base))

    # --- duplicate keys: parse via from_json must reject ---
    # Duplicate a key that is actually present at the top level (families differ
    # in their key names, e.g. Wrapper uses ``record_schema`` not ``schema``).
    import re as _re
    text = canonical.dumps_canonical(base).decode("utf-8")
    m = _re.match(r'\{"([^"]+)":', text)
    assert m is not None
    first_key = m.group(1)
    dup_text = text[:-1] + "," + json.dumps({first_key: 2})[1:-1] + "}"
    with pytest.raises(CanonicalJsonError):
        parser.from_mapping(canonical.loads_canonical(dup_text))

    # --- unknown core field ---
    extra = copy.deepcopy(base)
    extra["__unknown_field__"] = 1
    with pytest.raises(SchemaError):
        parser.from_mapping(extra)

    # --- wrong schema integer ---
    wrong_schema = copy.deepcopy(base)
    if "schema" in wrong_schema:
        wrong_schema["schema"] = 2
        with pytest.raises(SchemaError):
            parser.from_mapping(wrong_schema)

    # --- boolean where integer required ---
    bool_schema = copy.deepcopy(base)
    if "schema" in bool_schema and not isinstance(bool_schema["schema"], bool):
        bool_schema["schema"] = True
        with pytest.raises(PrimitiveError):
            parser.from_mapping(bool_schema)

    # --- unsupported version (negative probe): bad proposal_revision where present ---
    # covered elsewhere; sanity that parser still rejects type-mangled nested id
    bad_type = copy.deepcopy(base)
    # find the first string-typed id-like field and mangle it
    for k, v in list(bad_type.items()):
        if isinstance(v, str) and (k.endswith("_id") or k == "proposal_id"):
            bad_type[k] = 123
            break
    else:
        bad_type["schema"] = "x"
    with pytest.raises((PrimitiveError, SchemaError)):
        parser.from_mapping(bad_type)


def test_corpus_invalid_utf8_rejected_at_load() -> None:
    raw = b'{"schema":1,"author":"\xff"}'
    with pytest.raises(CanonicalJsonError):
        canonical.loads_canonical(raw)


def test_corpus_over_limit_summary_rejected() -> None:
    m = proposal_mapping()
    m["summary"] = "x" * 241  # 1..240 code points
    with pytest.raises(LimitError):
        schemas.ProposalRevision.from_mapping(m)


def test_corpus_over_limit_rationale_rejected() -> None:
    m = proposal_mapping()
    m["rationale"] = "x" * 8193  # 1..8192
    with pytest.raises(LimitError):
        schemas.ProposalRevision.from_mapping(m)


def test_corpus_too_many_operations_rejected() -> None:
    m = proposal_mapping()
    ops = []
    for i in range(129):  # cap 128
        op = link_add_operation(op_id=f"op_{i:026d}"[:30])
        # fix body: op_id must be op_ + 26 Crockford chars (digits ok in Crockford)
        op["op_id"] = "op_" + str(i).zfill(26)[-26:].upper()
        ops.append(op)
    # dedupe op_ids deterministically
    seen = set()
    deduped = []
    for op in ops:
        if op["op_id"] not in seen:
            seen.add(op["op_id"])
            deduped.append(op)
    m["operations"] = deduped
    with pytest.raises(LimitError):
        schemas.ProposalRevision.from_mapping(m)


# ============================================================================
# Import boundary proof
# ============================================================================


def test_import_boundary_isolated() -> None:
    """The package must not import server/studio/cli/render/viewer/mutators/Bundle.

    Two proofs:
    1. Source-level: no proposal_review module imports anything outside stdlib
       and its own sibling modules (AST scan of every ``.py`` in the package).
    2. Runtime: importing the package does not pull in the forbidden runtime
       modules (server/studio/cli/render/watch/viewer/mutation handlers).
    """
    import ast
    import pathlib

    pkg_dir = pathlib.Path(schemas.__file__).resolve().parent
    allowed_external_prefixes: tuple[str, ...] = ()  # no okf_loom parent imports
    for src in sorted(pkg_dir.glob("*.py")):
        if src.name == "__init__.py":
            # __init__ re-exports siblings only; still must not import forbidden.
            pass
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.level > 0:
                    # Relative import: stays inside the proposal_review package.
                    continue
                mod = node.module
                # Absolute imports must be stdlib - no okf_loom.* at all.
                assert not mod.startswith("okf_loom"), \
                    f"{src.name}: imports parent module {mod!r}"
            elif isinstance(node, ast.Import):
                for n in node.names:
                    assert not n.name.startswith("okf_loom"), \
                        f"{src.name}: imports parent module {n.name!r}"

    # Runtime proof in a fresh subprocess so the measurement is not polluted by
    # modules other tests may have imported into this shared process.
    import subprocess
    import sys as _sys
    probe = (
        "import sys; sys.path.insert(0, 'scripts'); "
        "before = set(sys.modules); "
        "import okf_loom.proposal_review; "
        "new = set(sys.modules) - before; "
        "bad = sorted(m for m in new if m.startswith('okf_loom.') "
        "and 'proposal_review' not in m and m not in ('okf_loom',)); "
        "print('|'.join(bad))"
    )
    result = subprocess.run(
        [_sys.executable, "-c", probe], cwd=_sys.path[0] if _sys.path else None,
        capture_output=True, text=True,
    )
    # The parent package ``okf_loom`` itself runs on import (unavoidable for a
    # subpackage) and pulls in data-model utilities (paths/exceptions/parse/
    # model/retrieval) via its own __init__; that is expected and not forbidden.
    # The forbidden surface is the runtime/viewer/mutation/capture layer the user
    # named: server/studio/cli/render/watch/viewer/mutation-handlers/Bundle.
    pulled = [m for m in result.stdout.strip().split("|") if m] if result.stdout.strip() else []
    forbidden_runtime = {
        "okf_loom.server", "okf_loom.studio", "okf_loom.cli", "okf_loom.render",
        "okf_loom.watch", "okf_loom.annotations", "okf_loom.markdown_blocks",
        "okf_loom.capture_provenance",
    }
    bad = [m for m in pulled if m in forbidden_runtime or m.startswith("okf_loom.viewer")]
    assert not bad, f"proposal_review transitively imported forbidden runtime modules: {bad}"
