"""Deterministic in-memory Proposal-v1 staging."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from .canonical import sha256_hex
from .errors import OperationExecutionError, RevisionMismatchError
from .operations import DeclaredReads, execute_operation
from .ports import DocumentCodec
from .schemas import MAX_CONCEPT_BYTES, Operation, Snapshot


def canonical_operation_order(operations: Sequence[Operation]) -> tuple[str, ...]:
    by_id = {op.op_id: op for op in operations}
    if len(by_id) != len(operations):
        raise OperationExecutionError("operation ids must be unique")
    incoming = {oid: 0 for oid in by_id}
    outgoing: dict[str, list[str]] = {oid: [] for oid in by_id}
    for op in operations:
        for dependency in op.depends_on:
            if dependency not in by_id:
                raise OperationExecutionError(f"operation {op.op_id} depends on unknown {dependency}", op_id=op.op_id)
            incoming[op.op_id] += 1
            outgoing[dependency].append(op.op_id)
    ready = sorted(oid for oid, count in incoming.items() if count == 0)
    result: list[str] = []
    while ready:
        oid = ready.pop(0)
        result.append(oid)
        for dependent in sorted(outgoing[oid]):
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if len(result) != len(operations):
        raise OperationExecutionError("operation dependencies must form a DAG")
    return tuple(result)


@dataclass(frozen=True)
class StagedResult:
    operation_order: tuple[str, ...]
    before: Mapping[str, bytes]
    after: Mapping[str, bytes]
    contributors: Mapping[str, tuple[str, ...]]


def _ancestors(op_id: str, by_id: Mapping[str, Operation], memo: dict[str, frozenset[str]]) -> frozenset[str]:
    if op_id not in memo:
        direct = by_id[op_id].depends_on
        memo[op_id] = frozenset(direct).union(*(_ancestors(dep, by_id, memo) for dep in direct))
    return memo[op_id]


def stage_operations(
    snapshot: Snapshot,
    captured_bytes: Mapping[str, bytes],
    operations: Sequence[Operation],
    *,
    codec: DocumentCodec | None = None,
) -> StagedResult:
    """Stage operations from one immutable captured snapshot.

    Reads observe a prerequisite's latest write only.  Independent writes to
    one target are rejected rather than allowing topological tie-breaking to
    become accidental write authority.
    """
    inventory = {entry.concept: entry.revision for entry in snapshot.concepts}
    if set(captured_bytes) != set(inventory):
        raise RevisionMismatchError("captured bytes must equal the complete snapshot inventory")
    frozen_before = {concept: bytes(data) for concept, data in captured_bytes.items()}
    for concept, data in frozen_before.items():
        if len(data) > MAX_CONCEPT_BYTES:
            raise OperationExecutionError(f"concept {concept} exceeds the {MAX_CONCEPT_BYTES}-byte limit", concept=concept)
        actual = sha256_hex(data)
        if inventory[concept] != actual:
            raise RevisionMismatchError(f"snapshot revision mismatch for {concept}: expected {inventory[concept]}, got {actual}")

    by_id = {op.op_id: op for op in operations}
    order = canonical_operation_order(operations)
    ancestors: dict[str, frozenset[str]] = {}
    for op in operations:
        for concept, expected in op.expected_revisions.items():
            actual = inventory.get(concept)
            if actual != expected:
                raise RevisionMismatchError(f"expected revision mismatch for {concept}: expected {expected}, got {actual or 'absent'}")
    # Same-target mutations must explicitly form one dependency chain.
    writers: dict[str, list[str]] = {}
    for oid in order:
        for target in by_id[oid].writes:
            prior = writers.setdefault(target, [])
            if prior and prior[-1] not in _ancestors(oid, by_id, ancestors):
                raise OperationExecutionError(f"same-target writes for {target} must form an explicit dependency chain", op_id=oid, concept=target)
            prior.append(oid)

    produced: dict[str, dict[str, bytes]] = {}
    final = dict(frozen_before)
    contributors: dict[str, list[str]] = {}
    for oid in order:
        op = by_id[oid]
        ancestor_set = _ancestors(oid, by_id, ancestors)
        visible: dict[str, bytes] = {}
        for concept in op.reads:
            value = frozen_before[concept]
            for previous in order:
                if previous == oid:
                    break
                if previous in ancestor_set and concept in produced.get(previous, {}):
                    value = produced[previous][concept]
            visible[concept] = value
        guarded = DeclaredReads(MappingProxyType(visible), op.reads, oid)
        result = execute_operation(op, guarded, codec=codec)
        if len(result) > MAX_CONCEPT_BYTES:
            raise OperationExecutionError(f"operation output for {op.target} exceeds the {MAX_CONCEPT_BYTES}-byte limit",
                                          op_id=oid, concept=op.target)
        produced[oid] = {op.target: result}
        final[op.target] = result
        if result != visible[op.target]:
            contributors.setdefault(op.target, []).append(oid)

    changed = tuple(concept for concept in sorted(final) if final[concept] != frozen_before[concept])
    frozen_contributors = {concept: tuple(sorted(contributors.get(concept, ()))) for concept in changed}
    # Both views are complete snapshots.  Consumers cannot accidentally run
    # bundle validation against only the changed subset; contributors is the
    # explicit changed-concept index used by preview projection.
    complete_before = {concept: frozen_before[concept] for concept in sorted(frozen_before)}
    complete_after = {concept: final[concept] for concept in sorted(final)}
    return StagedResult(order, MappingProxyType(complete_before), MappingProxyType(complete_after),
                        MappingProxyType(frozen_contributors))


__all__ = ["StagedResult", "canonical_operation_order", "stage_operations"]
