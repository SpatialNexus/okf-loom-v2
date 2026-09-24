"""Bounded, read-only projector for pre-v1 suggestion JSONL authority."""
from __future__ import annotations

from dataclasses import dataclass

from .canonical import loads_canonical
from .errors import LimitError, RepositoryError, SchemaError
from .ports import FilesystemRoot
from .schemas import LegacyProjection


@dataclass(frozen=True)
class LegacyCorruption:
    source_generation: int
    line: int
    reason: str


@dataclass(frozen=True)
class LegacyReadResult:
    projections: tuple[LegacyProjection, ...]
    corruptions: tuple[LegacyCorruption, ...]


class LegacySuggestionReader:
    """Never writes, upgrades, or marks a legacy suggestion applicable."""

    def __init__(self, root: FilesystemRoot, *, max_bytes: int = 64 * 1024 * 1024,
                 max_rows: int = 100_000) -> None:
        if max_bytes <= 0 or max_rows <= 0:
            raise LimitError("legacy reader bounds must be positive")
        self._root = root
        self._max_bytes = max_bytes
        self._max_rows = max_rows

    def read(self, path: str = "proposals.jsonl", *, source_generation: int = 0) -> LegacyReadResult:
        if source_generation < 0:
            raise SchemaError("source_generation must be non-negative")
        raw = self._root.read_bytes(path, max_bytes=self._max_bytes)
        complete = raw.split(b"\n")
        if raw and not raw.endswith(b"\n"):
            complete = complete[:-1]
        projections: list[LegacyProjection] = []
        corruptions: list[LegacyCorruption] = []
        seen: set[str] = set()
        row_count = 0
        for index, line in enumerate(complete, 1):
            if not line:
                continue
            row_count += 1
            if row_count > self._max_rows:
                raise LimitError("legacy suggestion row bound exceeded")
            try:
                obj = loads_canonical(line)
                if not isinstance(obj, dict):
                    raise SchemaError("legacy row must be an object")
                old_fields = {"id", "legacy_id", "summary", "author", "created_at"}
                studio_fields = {"id", "ts", "concept", "action", "args", "summary",
                                 "actor", "state", "group_id"}
                if set(obj).issubset(old_fields):
                    author = obj.get("author")
                    created_at = obj.get("created_at")
                elif set(obj) == studio_fields:
                    author = obj.get("actor")
                    created_at = obj.get("ts")
                else:
                    raise SchemaError("legacy row contains unknown or missing fields")
                legacy_id = obj.get("legacy_id", obj.get("id"))
                if not isinstance(legacy_id, str) or not legacy_id:
                    raise SchemaError("legacy row id is missing")
                if "id" in obj and "legacy_id" in obj and obj["id"] != obj["legacy_id"]:
                    raise SchemaError("legacy row has contradictory identities")
                if legacy_id in seen:
                    raise SchemaError("duplicate legacy identity in one source generation")
                projection = LegacyProjection.from_mapping({
                    "schema": 1, "kind": "legacy-suggestion",
                    "legacy_identity": {"legacy_id": legacy_id,
                                        "source_generation": source_generation},
                    "summary": obj["summary"], "author": author,
                    "created_at": created_at,
                    "regeneration_required": True, "applicable": False,
                })
                # Only mark as seen after successful projection (Fix #15): a
                # row that fails validation must not poison the dedup set.
                seen.add(legacy_id)
                projections.append(projection)
            except Exception as exc:
                # Retain bounded useful corruption detail (Fix #15): include
                # the exception class name and a short reason (no raw body).
                reason = type(exc).__name__
                msg = str(exc)
                if msg and len(msg) > 200:
                    msg = msg[:200]
                corruptions.append(LegacyCorruption(
                    source_generation, index, reason if not msg else f"{reason}: {msg}"))
        return LegacyReadResult(tuple(projections), tuple(corruptions))

    def verify(self, identity, *, path: str = "proposals.jsonl") -> LegacyProjection:
        """Return one exact retained identity or fail closed without writing."""
        result = self.read(path, source_generation=identity.source_generation)
        if result.corruptions:
            duplicate = any("duplicate legacy identity" in row.reason
                            for row in result.corruptions)
            raise RepositoryError(
                "legacy suggestion generation is malformed or duplicated",
                code="legacy_identity_conflict" if duplicate else "storage_corrupt")
        matches = [row for row in result.projections
                   if row.legacy_identity == identity]
        if len(matches) != 1:
            raise RepositoryError("legacy suggestion identity is stale or missing",
                                  code="legacy_identity_conflict")
        return matches[0]


__all__ = ["LegacyCorruption", "LegacyReadResult", "LegacySuggestionReader"]
