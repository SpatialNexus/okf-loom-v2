"""Composite read-model proofs for Proposal Review Foundation B.

Verifies the closed canonical proposal read model (SPEC section 5.7): closed
field set, sole-preview-authority verification before embedding, expired
preview projection, the 40 MiB canonical size cap, read-model token
completeness, and the import boundary.  All inputs are explicit immutable
fixtures; no filesystem/clock/network is consulted.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
import pytest

from okf_loom.proposal_review import canonical, projectors, read_model, schemas
from okf_loom.proposal_review.errors import (
    PreviewAuthorityError, ReadModelTooLargeError,
)
from okf_loom.proposal_review.read_model import ReadModelInputs, build_read_model


@dataclass(frozen=True)
class ImmutableBundleView:
    snapshot_digest: str
    config_digest: str
    validation_context_digest: str
    capabilities: tuple[str, ...]
    concept_revisions: object
    validator_id: str
    serializer_id: str
    operation_runtime_id: str

    def __post_init__(self):
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "concept_revisions", MappingProxyType(dict(self.concept_revisions)))

    def environment_digest(self):
        return canonical.canonical_digest({
              "config_digest": self.config_digest,
              "validation_context_digest": self.validation_context_digest,
            "capabilities_digest": canonical.canonical_digest(list(self.capabilities)),
            "validator_id": self.validator_id,
            "serializer_id": self.serializer_id,
            "operation_runtime_id": self.operation_runtime_id,
        })

ULID = "01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
HEX64 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
HEX64B = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
DIGEST = "sha256:" + HEX64
DIGEST_B = "sha256:" + HEX64B
ROW_TOKEN = "A" * 43
RUNTIME = "okf-validator@1.0+sha256:" + HEX64
RUNTIME_SER = "okf-roundtrip@1.0+sha256:" + HEX64
RUNTIME_OPS = "proposal-ops@1.0+sha256:" + HEX64
TS = "2026-07-14T12:34:56.789Z"
TS_EXPIRE = "2026-07-22T00:00:00.000Z"
TS_AFTER = "2026-07-22T00:00:00.001Z"


def _snapshot_mapping():
    body = {
        "schema": 1,
        "concepts": [{"concept": "tables/customers", "revision": HEX64,
                      "identity": {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
                      "leaf_inode": 3, "ctime_ns": 4, "mode": 0o100600,
                      "size": 1, "digest": HEX64}},
                     {"concept": "tables/orders", "revision": HEX64B,
                      "identity": {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
                      "leaf_inode": 4, "ctime_ns": 5, "mode": 0o100600,
                      "size": 1, "digest": HEX64B}}],
        "capabilities": ["okf.cap.typed_relations"],
        "config_digest": DIGEST, "validator_id": RUNTIME,
        "validation_context_digest": DIGEST_B,
        "serializer_id": RUNTIME_SER, "operation_runtime_id": RUNTIME_OPS,
    }
    return {
        "schema": 1, "captured_at": TS,
        "concepts": body["concepts"], "capabilities": body["capabilities"],
        "config_digest": DIGEST, "validator_id": RUNTIME,
        "validation_context_digest": DIGEST_B,
        "serializer_id": RUNTIME_SER, "operation_runtime_id": RUNTIME_OPS,
        "snapshot_digest": canonical.canonical_digest(body),
    }


def _op(op_id="op_" + ULID):
    return {
        "schema": 1, "op_id": op_id, "kind": "link_add", "target": "tables/orders",
        "args": {"destination": "tables/customers", "label": "customers",
                 "relation": "depends_on", "detail": None, "allow_forward_reference": False},
        "reads": ["tables/customers", "tables/orders"], "writes": ["tables/orders"],
        "expected_revisions": {"tables/customers": HEX64, "tables/orders": HEX64B},
        "depends_on": [], "extensions": {},
    }


def _preview_body(proposal, *, snapshot_digest, expires_at, diff_text=""):
    diff_body = {"schema": 1, "format": "unified", "context_lines": 3, "text": diff_text}
    validation_body = {"schema": 1, "profile": "producer", "validation_context_digest": DIGEST_B, "before_findings": [],
                       "after_findings": [], "added": [], "removed": [],
                       "unchanged": [], "status": "pass"}
    return {
        "schema": 1, "proposal_id": proposal_id_for(proposal),
        "proposal_revision": 1, "snapshot_digest": snapshot_digest,
        "validation_context_digest": DIGEST_B,
        "validator_id": RUNTIME, "serializer_id": RUNTIME_SER,
        "operation_runtime_id": RUNTIME_OPS, "operation_order": [],
        "concept_changes": [],
        "diff": {**diff_body, "digest": canonical.canonical_digest(diff_body)},
        "validation": {**validation_body,
                       "digest": canonical.canonical_digest(validation_body)},
        "generated_at": TS, "expires_at": expires_at,
    }


def proposal_id_for(p) -> str:
    return ULID


def _make_pair(*, expires_at=TS_EXPIRE, snapshot_digest=None, preview_digest=None,
               diff_text=""):
    """Build a coherent (proposal, preview) pair with matching preview_ref."""
    snap = _snapshot_mapping()
    sd = snapshot_digest or snap["snapshot_digest"]
    placeholder = _proposal(expires_at=expires_at, snapshot_digest=sd,
                            preview_digest=DIGEST, artifact_bytes=4096)
    body = _preview_body(placeholder, snapshot_digest=sd, expires_at=expires_at,
                         diff_text=diff_text)
    real_digest = preview_digest if preview_digest else canonical.canonical_digest(body)
    body_with_digest = {**body, "preview_digest": real_digest}
    preview = schemas.Preview.from_mapping(body_with_digest)
    artifact_bytes = len(preview.canonical_bytes())
    proposal = _proposal(expires_at=expires_at, snapshot_digest=sd,
                         preview_digest=real_digest, artifact_bytes=artifact_bytes)
    return proposal, preview


def _proposal(expires_at=TS_EXPIRE, snapshot_digest=None, preview_digest=DIGEST,
              artifact_bytes=4096):
    snap = _snapshot_mapping()
    if snapshot_digest:
        snap = {**snap, "snapshot_digest": snapshot_digest}
    return schemas.ProposalRevision.from_mapping({
        "schema": 1, "record_id": "rec_" + ULID,
        "creation_result_record_id": "rec_" + ("B" * 26),
        "creation_request_fingerprint": DIGEST, "proposal_id": ULID,
        "proposal_revision": 1, "supersedes_revision": None,
        "mode": "proposal", "author": "agent-local", "created_at": TS,
        "summary": "Add the customer relation",
        "rationale": "Make the dependency explicit without changing direct mode.",
        "affected_concepts": ["tables/customers", "tables/orders"],
        "expected_snapshot": snap, "operations": [_op()],
        "preview_ref": {"schema": 1, "preview_digest": preview_digest,
                        "artifact_bytes": artifact_bytes},
        "provenance": [{"kind": "comment", "value": "01J2Y8N5Z9"}],
        "expires_at": expires_at, "extensions": {},
    })


def _bundle():
    return ImmutableBundleView(
          snapshot_digest=_snapshot_mapping()["snapshot_digest"],
          config_digest=DIGEST, validation_context_digest=DIGEST_B,
          capabilities=("okf.cap.typed_relations",),
        concept_revisions={"tables/customers": HEX64, "tables/orders": HEX64B},
        validator_id=RUNTIME, serializer_id=RUNTIME_SER, operation_runtime_id=RUNTIME_OPS,
    )


class _Sentinel:
    pass


sentinel_preview = _Sentinel()


def _inputs(proposal=None, *, now=TS, preview=sentinel_preview, **kw):
    if proposal is None:
        proposal, default_preview = _make_pair()
        if preview is sentinel_preview:
            preview = default_preview
    elif preview is sentinel_preview:
        preview = _make_pair(expires_at=proposal.expires_at)[1]
    return ReadModelInputs(
        proposal=proposal, current_bundle=_bundle(), now=now, preview=preview, **kw,
    )


def test_read_model_closed_object_and_embedding():
    """The read model is a closed object that embeds a verified preview."""
    proposal, _ = _make_pair()
    model = build_read_model(_inputs(proposal=proposal))
    assert set(model.to_mapping()) == {
        "schema", "proposal", "state", "operation_states", "latest_decisions",
        "decision_set_token", "conflicts", "transaction_attempts",
        "read_model_token", "apply_result", "undo_result", "retention",
    }
    # Preview is embedded for transport because bytes are retained and verified.
    assert model.proposal["preview"]["schema"] == 1
    assert model.proposal["preview"]["preview_digest"] == proposal.preview_ref.preview_digest
    # preview_ref remains on the immutable revision.
    assert model.proposal["preview_ref"]["preview_digest"] == proposal.preview_ref.preview_digest
    # Operation states sorted by op id; undecided by default.
    assert len(model.operation_states) == 1
    assert model.operation_states[0].effective_state == "undecided"
    # Deterministic: identical inputs => identical canonical bytes + token.
    model2 = build_read_model(_inputs(proposal=proposal))
    assert model.canonical_bytes() == model2.canonical_bytes()
    assert model.read_model_token == model2.read_model_token


def test_read_model_preview_authority_verification():
    """Missing/mismatched/oversized preview authority fails closed (503)."""
    proposal, preview = _make_pair()
    # Missing preview while bytes retained -> unavailable.
    with pytest.raises(PreviewAuthorityError):
        build_read_model(_inputs(proposal=proposal, preview=None))
    # Digest mismatch: cross a proposal with a different preview -> unavailable.
    proposal_b, preview_b = _make_pair(expires_at=proposal.expires_at, diff_text="drift")
    assert proposal_b.preview_ref.preview_digest != proposal.preview_ref.preview_digest
    with pytest.raises(PreviewAuthorityError):
        build_read_model(_inputs(proposal=proposal, preview=preview_b))
    # Byte-length mismatch (artifact_bytes) -> unavailable.
    real_preview = preview
    mismatched_bytes_proposal = _proposal(
        expires_at=proposal.expires_at,
        preview_digest=proposal.preview_ref.preview_digest,
        artifact_bytes=len(real_preview.canonical_bytes()) + 1,
    )
    with pytest.raises(PreviewAuthorityError):
        build_read_model(_inputs(proposal=mismatched_bytes_proposal, preview=real_preview))


def test_read_model_expired_preview_projection():
    """After byte expiry the preview projects expired while preview_ref remains."""
    proposal = _proposal()
    model = build_read_model(_inputs(proposal=proposal, now=TS_AFTER, preview=None))
    assert model.proposal["preview"] == {"schema": 1, "expired": True, "expired_at": TS_AFTER}
    assert model.proposal["preview_ref"]["preview_digest"] == DIGEST
    # Expired derives the terminal expired lifecycle state.
    assert model.state == "expired"


def test_read_model_token_authority_completeness():
    """The read-model token changes when any authority input changes."""
    base = build_read_model(_inputs())
    base_token = base.read_model_token
    # A changed current bundle (concept revision drift) changes the token.
    drifted_bundle = ImmutableBundleView(
          snapshot_digest=_bundle().snapshot_digest, config_digest=DIGEST,
          validation_context_digest=DIGEST_B,
        capabilities=("okf.cap.typed_relations",),
        concept_revisions={"tables/customers": HEX64B, "tables/orders": HEX64B},
        validator_id=RUNTIME, serializer_id=RUNTIME_SER, operation_runtime_id=RUNTIME_OPS,
    )
    inputs2 = _inputs()
    object.__setattr__(inputs2, "current_bundle", drifted_bundle)
    drifted = build_read_model(inputs2)
    assert drifted.read_model_token != base_token
    context_bundle = ImmutableBundleView(
        snapshot_digest=_bundle().snapshot_digest, config_digest=DIGEST,
        validation_context_digest=DIGEST,
        capabilities=("okf.cap.typed_relations",),
        concept_revisions={"tables/customers": HEX64, "tables/orders": HEX64B},
        validator_id=RUNTIME, serializer_id=RUNTIME_SER,
        operation_runtime_id=RUNTIME_OPS,
    )
    context_inputs = _inputs()
    object.__setattr__(context_inputs, "current_bundle", context_bundle)
    assert build_read_model(context_inputs).read_model_token != base_token
    # A changed decision row changes the token.
    decision = schemas.DecisionRow.from_mapping({
        "schema": 1, "record_id": "rec_" + ULID,
        "decision_result_record_id": "rec_" + ("D" * 26),
        "decision_request_fingerprint": DIGEST, "proposal_id": ULID,
        "proposal_revision": 1, "op_id": "op_" + ULID, "decision": "accepted",
        "reason": "", "actor": "local", "decided_at": TS,
        "previous_row_token": None, "row_token": ROW_TOKEN,
    })
    inputs3 = _inputs()
    object.__setattr__(inputs3, "decision_rows", (decision,))
    decided = build_read_model(inputs3)
    assert decided.read_model_token != base_token
    assert decided.operation_states[0].effective_state == "accepted"
    assert decided.state == "ready"


def test_read_model_size_cap_rejects_oversized(monkeypatch):
    """A read model exceeding 40 MiB canonical bytes is rejected before persist."""
    inputs = _inputs()
    # Force the canonical serialization to report an oversized length.
    real_build = read_model.ProposalReadModel.canonical_bytes

    def fake_bytes(self):
        data = real_build(self)
        return data + b"x" * (read_model.MAX_READ_MODEL_BYTES + 1)

    monkeypatch.setattr(read_model.ProposalReadModel, "canonical_bytes", fake_bytes)
    with pytest.raises(ReadModelTooLargeError):
        build_read_model(inputs)


def test_read_model_transaction_attempts():
    """Nonterminal WALs project as sorted transaction-progress attempts."""
    def _target(concept):
        before = {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
                  "leaf_inode": 3, "ctime_ns": 4, "mode": 0o600,
                  "size": 1, "digest": DIGEST}
        staged = {**before, "leaf_inode": 4, "ctime_ns": 5, "digest": DIGEST_B}
        return {
            "concept": concept, "before_digest": DIGEST,
            "staged_digest": DIGEST_B, "backup_digest": DIGEST, "restore_digest": DIGEST,
            "before_identity": before, "staged_identity": staged,
            "resulting_identity": None,
            "stage_leaf": {"relative_name": "s", "device": 1, "inode": 4, "mode": 0o600,
                           "size": 1, "digest": DIGEST},
            "backup_leaf": {"relative_name": "b", "device": 1, "inode": 5, "mode": 0o600,
                            "size": 1, "digest": DIGEST},
        }
    # publishing requires a non-empty proper prefix -> 2 sorted targets.
    wal_value = {
        "schema": 1, "operation": "apply", "transaction_id": "txn_" + ULID,
        "proposal_id": ULID, "proposal_revision": 1, "idempotency_scope": "/route",
        "idempotency_key_digest": DIGEST, "request_fingerprint": DIGEST,
        "transaction_intent_digest": DIGEST,
        "decision_set_token": DIGEST, "read_model_token": DIGEST,
        "preview_digest": DIGEST, "snapshot_digest": DIGEST,
        "validation_context_digest": DIGEST_B,
        "validator_id": RUNTIME, "serializer_id": RUNTIME_SER,
        "operation_runtime_id": RUNTIME_OPS, "accepted_op_ids": ["op_" + ULID],
        "targets": [_target("tables/customers"), _target("tables/orders")],
        "stage_dir_identity": {"relative_name": "stage", "device": 1, "inode": 6, "mode": 0o700},
        "backup_dir_identity": {"relative_name": "backup", "device": 1, "inode": 7, "mode": 0o700},
        "phase": "publishing", "commit_decided": True,
        "publish_bitmap": [True, False],
        "rollback_bitmap": [False, False],
        "recovery_attempts": 0, "created_at": TS, "updated_at": TS,
    }
    wal_value["targets"][0]["resulting_identity"] = {
        **wal_value["targets"][0]["staged_identity"], "parent_device": 1,
        "parent_inode": 2, "ctime_ns": 6}
    wal_value["checksum"] = canonical.compute_checksum(wal_value)
    wal = schemas.WAL.from_mapping(wal_value)
    inputs = _inputs()
    object.__setattr__(inputs, "transaction_wals", (wal,))
    model = build_read_model(inputs)
    assert len(model.transaction_attempts) == 1
    assert model.transaction_attempts[0].status == "publishing"
    assert model.transaction_attempts[0].completed_targets == 1
    assert model.transaction_attempts[0].total_targets == 2


def test_read_model_import_boundary():
    """read_model imports no server/studio/cli/render/viewer/Bundle/filesystem."""
    import inspect
    from okf_loom.proposal_review import read_model as rm
    src = inspect.getsource(rm)
    forbidden = ("import os", "open(", "requests", "socket",
                 "from okf_loom.server", "from okf_loom.studio", "from okf_loom.cli",
                 "from okf_loom.render", "from okf_loom.viewer")
    for token in forbidden:
        assert token not in src, f"read_model must not reference {token!r}"
