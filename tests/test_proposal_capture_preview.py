from __future__ import annotations

from pathlib import Path

from okf_loom.proposal_review.schemas import Operation, Preview
from okf_loom.proposal_snapshot import capture_bundle, capture_stage_validate_preview


NOW = "2026-07-14T00:00:00.000Z"


def test_capture_composition_builds_deterministic_schema_valid_preview(tmp_path: Path) -> None:
    source = b"---\ntype: Note\ntitle: A\ndescription: d\ntags: [x]\ntimestamp: '2026-01-01'\n---\n\nold\n"
    (tmp_path / "a.md").write_bytes(source)
    captured = capture_bundle(tmp_path, captured_at=NOW)
    op = Operation.from_mapping({
        "schema": 1, "op_id": "op_" + "A" * 26, "kind": "replace_text", "target": "a",
        "args": {"old": "old", "new": "new", "occurrence": "only"},
        "reads": ["a"], "writes": ["a"],
        "expected_revisions": {"a": captured.concept_revisions["a"]},
        "depends_on": [], "extensions": {},
    })
    kwargs = dict(root=tmp_path, proposal_id="01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
                  proposal_revision=1, operations=(op,), generated_at=NOW,
                  expires_at="2026-08-14T00:00:00.000Z", captured_at=NOW)
    first = capture_stage_validate_preview(**kwargs)
    second = capture_stage_validate_preview(**kwargs)
    assert first.artifact_bytes == second.artifact_bytes
    assert Preview.from_json(first.artifact_bytes) == first.preview
    assert first.preview.validation_context_digest == first.captured.validation_context_digest
