from __future__ import annotations

import pytest

from okf_loom.parse import parse_document
from okf_loom.proposal_review.errors import OperationExecutionError
from okf_loom.proposal_review.operations import execute_operation
from okf_loom.proposal_review.schemas import Operation

OID = "op_" + "A" * 26


def operation(kind, args, *, reads=("docs/a",), target="docs/a"):
    writes = [target]
    return Operation.from_mapping({"schema": 1, "op_id": OID, "kind": kind, "target": target,
        "args": args, "reads": list(reads), "writes": writes,
        "expected_revisions": {name: "0" * 64 for name in set(reads) | set(writes)},
        "depends_on": [], "extensions": {}})


def test_frontmatter_roundtrip_preserves_comments_unknown_keys_and_noop_bytes():
    raw = b'---\ntype: Note\n# keep me\nunknown: "quoted"\n---\n\nBody without final newline'
    op = operation("set_frontmatter", {"key": "tags", "value": ["one", "two"]})
    result = execute_operation(op, {"docs/a": raw})
    assert b"# keep me" in result and b'unknown: "quoted"' in result
    noop = operation("set_frontmatter", {"key": "unknown", "value": "quoted"})
    assert execute_operation(noop, {"docs/a": raw}) == raw


def test_set_frontmatter_applies_exact_todo_placeholder_value():
    raw = b"---\ntype: Note\ndescription: Hand authored\n---\n\nBody\n"
    op = operation("set_frontmatter", {"key": "description", "value": "TODO: replace later"})
    result = execute_operation(op, {"docs/a": raw})
    # Both supported YAML backends must preserve the exact scalar, not one quote style.
    frontmatter, _ = parse_document(result.decode("utf-8"))
    assert frontmatter == {"type": "Note", "description": "TODO: replace later"}
    assert result.endswith(b"---\n\nBody\n")


def test_link_relation_and_entity_are_idempotent():
    raw = b"---\ntype: Note\n---\n\nBody\n"
    link = operation("link_add", {"destination": "docs/b", "label": "B", "relation": "uses",
        "detail": None, "allow_forward_reference": False}, reads=("docs/a", "docs/b"))
    once = execute_operation(link, {"docs/a": raw, "docs/b": b"---\ntype: Note\n---\n"})
    assert execute_operation(link, {"docs/a": once, "docs/b": b"---\ntype: Note\n---\n"}) == once
    entity = operation("entity_add", {"label": "Ada", "kind": "person", "entity_id": None})
    entity_once = execute_operation(entity, {"docs/a": raw})
    assert execute_operation(entity, {"docs/a": entity_once}) == entity_once


def test_replace_nth_only_and_fenced_duplicate_sections_fail_closed():
    raw = b"---\ntype: Note\n---\n\nx x x\n"
    nth = operation("replace_text", {"old": "x", "new": "y", "occurrence": 2})
    assert b"x y x" in execute_operation(nth, {"docs/a": raw})
    only = operation("replace_text", {"old": "x", "new": "y", "occurrence": "only"})
    with pytest.raises(OperationExecutionError):
        execute_operation(only, {"docs/a": raw})
    fenced = b"---\ntype: Note\n---\n\n```\n## Target\n```\n## Target\nold\n"
    section = operation("update_section", {"heading": "## Target", "body": "new", "mode": "replace"})
    assert b"```\n## Target\n```\n## Target\n\nnew\n" in execute_operation(section, {"docs/a": fenced})
    duplicate = fenced + b"## Target\nagain\n"
    with pytest.raises(OperationExecutionError):
        execute_operation(section, {"docs/a": duplicate})
