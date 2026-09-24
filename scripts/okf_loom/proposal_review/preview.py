"""Pure construction of the immutable deterministic Preview-v1 artifact."""
from __future__ import annotations

import base64
import difflib
from typing import Mapping, Sequence

from .canonical import canonical_digest, sha256_hex
from .errors import PreviewLimitError
from .ports import FrozenSnapshotValidator
from .schemas import (MAX_CONCEPT_BYTES, MAX_DIFF_BYTES, MAX_DIFF_LINES, MAX_PREVIEW_BYTES,
                      ConceptChange, Diff, Operation, Preview, Snapshot)
from .staging import StagedResult, canonical_operation_order
from .validation import DEFAULT_PROFILE, build_validation

MAX_AGGREGATE_RAW_BYTES = 24 * 1024 * 1024
MAX_READ_MODEL_BYTES = 40 * 1024 * 1024


def _unified(concept: str, before: bytes, after: bytes) -> str:
    # Decode was already proven by the operation codec; repeat fail-closed for
    # callers constructing from a custom StagedResult.
    old = before.decode("utf-8", errors="strict").splitlines(keepends=True)
    new = after.decode("utf-8", errors="strict").splitlines(keepends=True)
    # Give every unterminated content line an explicit private terminator
    # before asking difflib to add control prefixes.  Replacing that terminator
    # afterwards avoids guessing whether an emitted '-'/'+' line is content or
    # a file header based on its prefix.
    marker = "\x00okf-no-final-newline\x00"
    while any(marker in line for line in old) or any(marker in line for line in new):
        marker += "\x00"
    old = [line if line.endswith(("\n", "\r")) else line + marker + "\n" for line in old]
    new = [line if line.endswith(("\n", "\r")) else line + marker + "\n" for line in new]
    generated = difflib.unified_diff(old, new, fromfile=f"a/{concept}.md",
                                     tofile=f"b/{concept}.md", n=3, lineterm="\n")
    return "".join(generated).replace(marker + "\n", "\n\\ No newline at end of file\n")


def build_preview(
    *,
    proposal_id: str,
    proposal_revision: int,
    snapshot: Snapshot,
    operations: Sequence[Operation],
    staged: StagedResult,
    complete_before: Mapping[str, bytes],
    validator: FrozenSnapshotValidator,
    generated_at: str,
    expires_at: str,
    profile: str = DEFAULT_PROFILE,
    read_model_overhead_bytes: int = 0,
) -> tuple[Preview, bytes]:
    expected_order = canonical_operation_order(operations)
    if staged.operation_order != expected_order:
        raise PreviewLimitError("staged operation order is not canonical Kahn order")
    if dict(complete_before) != dict(staged.before):
        raise PreviewLimitError("complete before snapshot does not match staged authority",
                                code="contract_parity_failure")
    changed_concepts = tuple(sorted(staged.contributors))
    raw_size = sum(len(staged.before[c]) + len(staged.after[c]) for c in changed_concepts)
    if raw_size > MAX_AGGREGATE_RAW_BYTES:
        raise PreviewLimitError(f"aggregate raw before+after bytes exceed {MAX_AGGREGATE_RAW_BYTES}")
    changes: list[ConceptChange] = []
    snapshot_identities = {entry.concept: entry.identity.to_mapping()
                           for entry in snapshot.concepts}
    diff_parts: list[str] = []
    complete_after = dict(complete_before)
    for concept in changed_concepts:
        before, after = staged.before[concept], staged.after[concept]
        if len(before) > MAX_CONCEPT_BYTES or len(after) > MAX_CONCEPT_BYTES:
            raise PreviewLimitError(f"concept {concept} exceeds the 2 MiB byte limit")
        changes.append(ConceptChange.from_mapping({
            "concept": concept, "before_revision": sha256_hex(before), "after_revision": sha256_hex(after),
            "before_identity": snapshot_identities[concept],
            "before_base64": base64.b64encode(before).decode("ascii"),
            "after_base64": base64.b64encode(after).decode("ascii"),
            "contributing_ops": list(staged.contributors[concept]),
        }))
        complete_after[concept] = after
        diff_parts.append(_unified(concept, before, after))
    diff_text = "".join(diff_parts)
    diff_bytes = len(diff_text.encode("utf-8"))
    diff_lines = diff_text.count("\n") + (0 if not diff_text or diff_text.endswith("\n") else 1)
    if diff_bytes > MAX_DIFF_BYTES or diff_lines > MAX_DIFF_LINES:
        raise PreviewLimitError("aggregate unified diff exceeds its byte or line limit")
    diff_body = {"schema": 1, "format": "unified", "context_lines": 3, "text": diff_text}
    diff = Diff.from_mapping({**diff_body, "digest": canonical_digest(diff_body)})
    validation = build_validation(
        complete_before, complete_after, validator,
        validation_context_digest=snapshot.validation_context_digest,
        profile=profile,
    )
    body = {
        "schema": 1, "proposal_id": proposal_id, "proposal_revision": proposal_revision,
        "snapshot_digest": snapshot.snapshot_digest,
        "validation_context_digest": snapshot.validation_context_digest,
        "validator_id": snapshot.validator_id,
        "serializer_id": snapshot.serializer_id, "operation_runtime_id": snapshot.operation_runtime_id,
        "operation_order": list(expected_order), "concept_changes": [c.to_mapping() for c in changes],
        "diff": diff.to_mapping(), "validation": validation.to_mapping(),
        "generated_at": generated_at, "expires_at": expires_at,
    }
    preview = Preview.from_mapping({**body, "preview_digest": canonical_digest(body)})
    artifact = preview.canonical_bytes()
    if len(artifact) > MAX_PREVIEW_BYTES:
        raise PreviewLimitError(f"preview artifact exceeds {MAX_PREVIEW_BYTES}")
    if read_model_overhead_bytes < 0 or len(artifact) + read_model_overhead_bytes > MAX_READ_MODEL_BYTES:
        raise PreviewLimitError(f"projected read model exceeds {MAX_READ_MODEL_BYTES}", code="read_model_too_large")
    # Canonical round-trip is part of artifact construction, not deferred to
    # persistence.  It proves nested digests and immutable record rebuilding.
    round_tripped = Preview.from_json(artifact)
    if round_tripped != preview or round_tripped.canonical_bytes() != artifact:
        raise PreviewLimitError("canonical Preview round-trip failed", code="contract_parity_failure")
    return preview, artifact


__all__ = ["MAX_AGGREGATE_RAW_BYTES", "MAX_READ_MODEL_BYTES", "build_preview"]
