from __future__ import annotations

import pytest

from okf_loom.proposal_review.canonical import canonical_digest, sha256_hex
from okf_loom.proposal_review.errors import RevisionMismatchError, UndeclaredReadError
from okf_loom.proposal_review.operations import DeclaredReads
from okf_loom.proposal_review.schemas import Operation, Snapshot
from okf_loom.proposal_review.staging import canonical_operation_order, stage_operations


def snapshot(files):
    concepts = [{"concept": key, "revision": sha256_hex(value), "identity": {
        "parent_device": 1, "parent_inode": 2, "leaf_device": 1,
        "leaf_inode": index + 3, "ctime_ns": 4, "mode": 0o600,
        "size": len(value), "digest": sha256_hex(value)}}
        for index, (key, value) in enumerate(sorted(files.items()))]
    base = {"schema": 1, "concepts": concepts, "capabilities": [], "config_digest": "sha256:" + "0"*64,
        "validation_context_digest": "sha256:" + "4"*64,
        "validator_id": "v@1.0+sha256:" + "1"*64, "serializer_id": "s@1.0+sha256:" + "2"*64,
        "operation_runtime_id": "o@1.0+sha256:" + "3"*64}
    return Snapshot.from_mapping({**base, "captured_at": "2026-07-14T00:00:00.000Z", "snapshot_digest": canonical_digest(base)})


def op(letter, files, *, depends=(), key="title", value="x"):
    return Operation.from_mapping({"schema": 1, "op_id": "op_" + letter*26, "kind": "set_frontmatter",
        "target": "docs/a", "args": {"key": key, "value": value}, "reads": ["docs/a"], "writes": ["docs/a"],
        "expected_revisions": {"docs/a": sha256_hex(files["docs/a"])}, "depends_on": list(depends), "extensions": {}})


def test_canonical_kahn_order_is_not_global_sort_and_chained_writes_see_latest():
    files = {"docs/a": b"---\ntype: Note\n---\n\nBody\n"}
    b = op("B", files, key="title", value="B")
    a = op("A", files, depends=(b.op_id,), key="description", value="A")
    c = op("C", files, depends=(a.op_id,), key="tags", value=["C"])
    assert canonical_operation_order((a, b, c)) == (b.op_id, a.op_id, c.op_id)
    result = stage_operations(snapshot(files), files, (a, b, c))
    assert b"title: B" in result.after["docs/a"] and b"description: A" in result.after["docs/a"]
    assert result.contributors["docs/a"] == tuple(sorted((a.op_id, b.op_id, c.op_id)))


def test_revision_and_undeclared_access_fail_closed():
    files = {"docs/a": b"---\ntype: Note\n---\n"}
    operation = op("A", files)
    with pytest.raises(RevisionMismatchError):
        stage_operations(snapshot(files), {"docs/a": files["docs/a"] + b"tamper"}, (operation,))
    with pytest.raises(UndeclaredReadError):
        DeclaredReads(files, ("docs/a",), operation.op_id)["docs/secret"]
