from __future__ import annotations

import base64

import pytest

from okf_loom.proposal_review.canonical import canonical_digest, loads_canonical, sha256_hex
from okf_loom.proposal_review.errors import IntegrityError, PreviewLimitError
from okf_loom.proposal_review.ports import FrozenValidationReport
from okf_loom.proposal_review.preview import _unified, build_preview
from okf_loom.proposal_review.schemas import Operation, Preview, Snapshot
from okf_loom.proposal_review.staging import stage_operations


class Validator:
    def installed_profiles(self): return ("producer",)
    def validate(self, snapshot, *, profile): return FrozenValidationReport((), True)


def fixture():
    files = {"docs/a": b"---\r\ntype: Note\r\n---\r\n\r\nold"}
    concepts = [{"concept": "docs/a", "revision": sha256_hex(files["docs/a"]), "identity": {
        "parent_device": 1, "parent_inode": 2, "leaf_device": 1,
        "leaf_inode": 3, "ctime_ns": 4, "mode": 0o600,
        "size": len(files["docs/a"]), "digest": sha256_hex(files["docs/a"])}}]
    sb = {"schema": 1, "concepts": concepts, "capabilities": [], "config_digest": "sha256:"+"0"*64,
        "validation_context_digest": "sha256:"+"4"*64,
        "validator_id": "v@1.0+sha256:"+"1"*64, "serializer_id": "s@1.0+sha256:"+"2"*64,
        "operation_runtime_id": "o@1.0+sha256:"+"3"*64}
    snap = Snapshot.from_mapping({**sb, "captured_at": "2026-07-14T00:00:00.000Z", "snapshot_digest": canonical_digest(sb)})
    op = Operation.from_mapping({"schema": 1, "op_id": "op_"+"A"*26, "kind": "replace_text", "target": "docs/a",
        "args": {"old": "old", "new": "new", "occurrence": "only"}, "reads": ["docs/a"], "writes": ["docs/a"],
        "expected_revisions": {"docs/a": sha256_hex(files["docs/a"])}, "depends_on": [], "extensions": {}})
    return files, snap, op


def test_preview_is_deterministic_canonical_and_uses_padded_base64_and_exact_paths():
    files, snap, op = fixture(); staged = stage_operations(snap, files, (op,))
    kwargs = dict(proposal_id="01J2Y8N5Z9Q3A7K6M4T1V0WXYZ", proposal_revision=1, snapshot=snap,
        operations=(op,), staged=staged, complete_before=files, validator=Validator(),
        generated_at="2026-07-14T00:00:00.000Z", expires_at="2026-08-14T00:00:00.000Z")
    preview, artifact = build_preview(**kwargs)
    assert build_preview(**kwargs)[1] == artifact == preview.canonical_bytes()
    assert "--- a/docs/a.md\n+++ b/docs/a.md\n" in preview.diff.text
    assert preview.validation_context_digest == snap.validation_context_digest
    assert preview.validation.validation_context_digest == snap.validation_context_digest
    assert preview.diff.text.count("\\ No newline at end of file\n") == 1
    assert base64.b64decode(preview.concept_changes[0].before_base64, validate=True) == files["docs/a"]
    tampered = loads_canonical(artifact); tampered["diff"]["text"] += "x"
    with pytest.raises(IntegrityError): Preview.from_mapping(tampered)


def test_read_model_limit_is_checked_before_return():
    files, snap, op = fixture(); staged = stage_operations(snap, files, (op,))
    with pytest.raises(PreviewLimitError):
        build_preview(proposal_id="01J2Y8N5Z9Q3A7K6M4T1V0WXYZ", proposal_revision=1, snapshot=snap,
            operations=(op,), staged=staged, complete_before=files, validator=Validator(),
            generated_at="2026-07-14T00:00:00.000Z", expires_at="2026-08-14T00:00:00.000Z",
            read_model_overhead_bytes=40*1024*1024)


def test_no_final_newline_markers_do_not_confuse_header_like_content():
    text = _unified("docs/a", b"--- old", b"+++ new")
    assert text.startswith("--- a/docs/a.md\n+++ b/docs/a.md\n")
    assert "---- old\n\\ No newline at end of file\n" in text
    assert "++++ new\n\\ No newline at end of file\n" in text
    assert text.count("\\ No newline at end of file\n") == 2


def test_pure_modules_expose_no_live_filesystem_adapter():
    import okf_loom.proposal_review.operations as operations
    import okf_loom.proposal_review.staging as staging
    assert "Bundle" not in operations.__dict__ and "FilesystemRoot" not in staging.__dict__
