"""Authoritative section-aware retrieval and deterministic context packs.

The module is deliberately dependency-free.  It derives exact passages from a
loaded :class:`Bundle` snapshot and delegates ranking to the existing six-mode
search implementation; context packs and enhanced search therefore cannot
drift into separate ranking algorithms.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable
from urllib.parse import unquote_to_bytes

from .model import Bundle, Concept
from .paths import ConceptId, ConceptIdError, concept_id_from_str, concept_id_to_str
from .search import SearchMode, SearchResult, tokenize


PASSAGE_SCHEMA_VERSION = 1
CONTEXT_SCHEMA_VERSION = 1
CORPUS_SCHEMA_VERSION = 1
ALGORITHM_VERSION = "section-retrieval-v1"
MAX_QUERY_CHARS = 4096
MAX_FILTER_CHARS = 256
MAX_EXCERPT_CHARS = 2000
MAX_REASONS = 8
MAX_EVIDENCE_EDGES = 20
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# Kept below BaseHTTPRequestHandler's fixed 64 KiB request-line ceiling so
# application routes can emit the stable contract envelope before stdlib's
# built-in HTML rejection takes ownership.
MAX_REQUEST_TARGET_BYTES = 60 * 1024
MAX_BUNDLE_CONCEPTS = 100_000
MAX_BUNDLE_PASSAGES = 250_000
MAX_BUNDLE_SOURCE_CODEPOINTS = 256 * 1024 * 1024
# Import work cap: exact sum of canonical ``passages`` array bytes across all
# chunks. Per-chunk HTTP-compatible 4 MiB limits still apply independently.
MAX_CORPUS_CANONICAL_BYTES = 64 * 1024 * 1024
MAX_CORPUS_PROJECTION_NODES = 2_000_000
MAX_CONTEXT_DOCUMENTS = 100
MAX_CONTEXT_PASSAGES_PER_DOCUMENT = 4
PUBLIC_METADATA_MAX_CODEPOINTS = 4096
PUBLIC_METADATA_STRING_CODEPOINTS = 1024
PUBLIC_METADATA_COLLECTION_ITEMS = 8
PUBLIC_METADATA_KEY_CODEPOINTS = 1024
PUBLIC_METADATA_KEY_BYTES = 1024
_WORK_STATE = threading.local()
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_BIDI_OVERRIDES = {chr(code) for code in (
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069,
)}
_SAFE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")


class RetrievalContractError(ValueError):
    """Caller-safe validation failure with stable HTTP contract metadata."""

    def __init__(self, code: str, message: str, *, fields: dict[str, str] | None = None,
                 status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields or {}
        self.status = status

    def as_error(self) -> dict[str, Any]:
        return {"ok": False, "error": {
            "code": self.code, "message": self.message, "fields": self.fields,
        }}


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize one canonical UTF-8 JSON wire representation, rejecting NaN/Infinity."""
    try:
        visited = 0

        def validate(item: Any) -> None:
            nonlocal visited
            visited += 1
            if visited % 128 == 0:
                check_retrieval_cancelled()
            if item is None or isinstance(item, (str, bool, int)):
                return
            if isinstance(item, float):
                if not math.isfinite(item):
                    raise ValueError("non-finite float")
                return
            if isinstance(item, list):
                for child in item:
                    validate(child)
                return
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise TypeError("JSON object keys must be strings")
                for child in item.values():
                    validate(child)
                return
            raise TypeError(f"unsupported JSON type: {type(item).__name__}")

        check_retrieval_cancelled()
        validate(value)
        check_retrieval_cancelled()
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        check_retrieval_cancelled()
        return encoded
    except RetrievalContractError:
        raise
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RetrievalContractError(
            "invalid_json_value", "Response contains a non-canonical JSON value", status=500,
        ) from exc


def set_retrieval_cancel_event(event: threading.Event | None) -> None:
    _WORK_STATE.cancel_event = event


def check_retrieval_cancelled() -> None:
    event = getattr(_WORK_STATE, "cancel_event", None)
    if event is not None and event.is_set():
        raise RetrievalContractError("retrieval_timeout", "Retrieval deadline exceeded", status=503)


def _utf8_length(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _projection_checkpoint(work_state: dict[str, int], depth: int) -> None:
    """Testable recursive-work checkpoint; cancellation is sampled in bounded batches."""
    if work_state["visited"] % 64 == 0:
        check_retrieval_cancelled()


def validate_retrieval_work(bundle: Bundle) -> None:
    """Apply aggregate work caps before graph/index/backend construction."""
    check_retrieval_cancelled()
    if len(bundle.concepts) > MAX_BUNDLE_CONCEPTS:
        raise RetrievalContractError("bundle_too_large", "Bundle exceeds concept work limit",
                                     status=413)
    total = 0
    for index, concept in enumerate(bundle.concepts.values()):
        if index % 256 == 0:
            check_retrieval_cancelled()
        total += len(concept.raw_text)
        if total > MAX_BUNDLE_SOURCE_CODEPOINTS:
            raise RetrievalContractError("bundle_too_large", "Bundle exceeds source work limit",
                                         status=413)


def _bounded_text(value: str | None, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise RetrievalContractError("missing_seed", "A query or concept seed is required",
                                         fields={field: "required"})
        return None
    if not isinstance(value, str):
        raise RetrievalContractError("invalid_request", f"{field} must be a string",
                                     fields={field: "invalid_type"})
    if len(value) > (MAX_QUERY_CHARS if field == "q" else MAX_FILTER_CHARS):
        raise RetrievalContractError("invalid_request", f"{field} exceeds its maximum length",
                                     fields={field: "too_long"})
    if any(unicodedata.category(ch) == "Cc" or ch in _BIDI_OVERRIDES for ch in value):
        raise RetrievalContractError("invalid_request", f"{field} contains control characters",
                                     fields={field: "control_character"})
    value = value.strip()
    if field != "q" and not value:
        raise RetrievalContractError("invalid_request", f"{field} must not be empty",
                                     fields={field: "empty"})
    if required and not value:
        raise RetrievalContractError("missing_seed", "A query or concept seed is required",
                                     fields={field: "required"})
    return value


def _bounded_selection_text(
    value: str | None, field: str, maximum: int, *, required: bool = False
) -> str | None:
    if value is None:
        if required:
            raise RetrievalContractError("invalid_selection", "selection_exact is required",
                                         fields={"selection_exact": "required"})
        return None
    if not isinstance(value, str):
        raise RetrievalContractError("invalid_selection", f"{field} must be a string",
                                     fields={field: "invalid_type"})
    if len(value) > maximum:
        raise RetrievalContractError("invalid_selection", f"{field} exceeds its maximum length",
                                     fields={field: "too_long"})
    if any(unicodedata.category(ch) == "Cc" and ch not in {"\n", "\r", "\t"}
           or ch in _BIDI_OVERRIDES for ch in value):
        raise RetrievalContractError("invalid_selection", f"{field} contains forbidden controls",
                                     fields={field: "control_character"})
    if required and not value:
        raise RetrievalContractError("invalid_selection", "selection_exact is required",
                                     fields={"selection_exact": "required"})
    return value


def _bounded_int(value: int, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RetrievalContractError("invalid_request", f"{field} must be an integer from {low} to {high}",
                                     fields={field: "out_of_range"})
    return value


def canonical_concept_id(value: str, field: str = "concept") -> str:
    """Validate a public canonical concept id without permissive path cleanup."""
    value = _bounded_text(value, field, required=True) or ""
    if (not _SAFE_ID.fullmatch(value) or value.endswith(".md")
            or any(seg in {".", "..", "index", "log", "index.md", "log.md"}
                   or seg.startswith("__") for seg in value.split("/"))):
        raise RetrievalContractError("invalid_target", "Invalid canonical concept id",
                                     fields={field: "invalid"})
    try:
        concept_id_from_str(value)
    except ConceptIdError as exc:
        raise RetrievalContractError("invalid_target", "Invalid canonical concept id",
                                     fields={field: "invalid"}) from exc
    return value


@dataclass(frozen=True)
class SearchRequest:
    query: str = ""
    mode: str = "lexical"
    type_filter: str | None = None
    tag: str | None = None
    entity: str | None = None
    relation: str | None = None
    source: str | None = None
    target: str | None = None
    limit: int = 20

    def normalized(self) -> "SearchRequest":
        if not isinstance(self.query, str):
            raise RetrievalContractError("invalid_request", "q must be a string",
                                         fields={"q": "invalid_type"})
        try:
            mode = SearchMode(self.mode).value
        except (ValueError, TypeError) as exc:
            raise RetrievalContractError("invalid_request", "Unknown search mode",
                                         fields={"mode": "unsupported"}) from exc
        query = _bounded_text(self.query, "q") or ""
        values = {
            "type_filter": _bounded_text(self.type_filter, "type"),
            "tag": _bounded_text(self.tag, "tag"),
            "entity": _bounded_text(self.entity, "entity"),
            "relation": _bounded_text(self.relation, "relation"),
            "source": _bounded_text(self.source, "source"),
            "target": _bounded_text(self.target, "target"),
        }
        if values["entity"] is not None and mode != "entity":
            raise RetrievalContractError("invalid_request", "entity is only valid in entity mode",
                                         fields={"entity": "mode_inapplicable"})
        if values["entity"] is not None and query and query != values["entity"]:
            raise RetrievalContractError("invalid_request", "Use q or entity, not both",
                                         fields={"entity": "duplicate_seed"})
        edge = any(values[k] is not None for k in ("relation", "source", "target"))
        if edge and mode != "relation":
            raise RetrievalContractError("invalid_request", "relation/source/target require relation mode",
                                         fields={"mode": "filter_mismatch"})
        if mode == "entity" and values["entity"] and not query:
            query = values["entity"] or ""
        for key in ("source", "target"):
            if values[key]:
                values[key] = canonical_concept_id(values[key] or "", key)
        return SearchRequest(query=query, mode=mode, limit=_bounded_int(self.limit, "limit", 1, 100),
                             **values)


@dataclass(frozen=True)
class ContextRequest:
    query: str = ""
    mode: str = "hybrid"
    type_filter: str | None = None
    tag: str | None = None
    entity: str | None = None
    relation: str | None = None
    source: str | None = None
    target: str | None = None
    concept: str | None = None
    selection_exact: str | None = None
    selection_prefix: str | None = None
    selection_suffix: str | None = None
    selection_heading: str | None = None
    budget_tokens: int = 6000
    neighbors: int = 1
    limit: int = 20

    def normalized(self, bundle: Bundle) -> "ContextRequest":
        if not isinstance(self.query, str):
            raise RetrievalContractError("invalid_request", "q must be a string",
                                         fields={"q": "invalid_type"})
        concept = canonical_concept_id(self.concept, "concept") if self.concept is not None else None
        has_selection = any(value is not None for value in (
            self.selection_exact, self.selection_prefix, self.selection_suffix,
            self.selection_heading,
        ))
        if has_selection and concept is None:
            raise RetrievalContractError("invalid_selection", "Selection requires a concept target",
                                         fields={"concept": "required"})
        selection_exact = _bounded_selection_text(
            self.selection_exact, "selection_exact", 4096, required=has_selection
        )
        selection_prefix = _bounded_selection_text(
            self.selection_prefix, "selection_prefix", 512
        )
        selection_suffix = _bounded_selection_text(
            self.selection_suffix, "selection_suffix", 512
        )
        selection_heading = _bounded_selection_text(
            self.selection_heading, "selection_heading", 256
        )
        if concept and concept_id_from_str(concept) not in bundle.concepts:
            raise RetrievalContractError("concept_not_found", "Concept target was not found",
                                         fields={"concept": "not_found"}, status=404)
        query = _bounded_text(self.query, "q") or ""
        internal_concept_query = not query and concept is not None
        sr = SearchRequest(query=query, mode=self.mode, type_filter=self.type_filter,
                           tag=self.tag, entity=self.entity, relation=self.relation,
                           source=self.source, target=self.target, limit=self.limit).normalized()
        has_relation_seed = sr.mode == "relation" and any(
            value is not None for value in (sr.relation, sr.source, sr.target)
        )
        if not sr.query and concept is None and not has_relation_seed:
            raise RetrievalContractError("missing_seed", "A query or concept seed is required",
                                         fields={"q": "required"})
        normalized_query = (bundle.concepts[concept_id_from_str(concept)].title
                            if internal_concept_query and concept is not None else sr.query)
        return ContextRequest(query=normalized_query, mode=sr.mode, type_filter=sr.type_filter,
                              tag=sr.tag, entity=sr.entity, relation=sr.relation,
                              source=sr.source, target=sr.target, concept=concept,
                              selection_exact=selection_exact,
                              selection_prefix=selection_prefix,
                              selection_suffix=selection_suffix,
                              selection_heading=selection_heading,
                              budget_tokens=_bounded_int(self.budget_tokens, "budget_tokens", 128, 50000),
                              neighbors=_bounded_int(self.neighbors, "neighbors", 0, 2), limit=sr.limit)


@dataclass(frozen=True)
class Passage:
    concept_id: ConceptId
    path: str
    title: str
    type: str | None
    heading: str | None
    heading_line: int | None
    line_start: int
    line_end: int
    char_start: int | None
    char_end: int | None
    excerpt: str
    tags: tuple[str, ...]
    entities: tuple[str, ...]
    relations: tuple[dict[str, str], ...]
    freshness: str | None
    provenance: Any
    citations: Any
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def passage_id(self) -> str:
        suffix = (f"@C{self.char_start}-C{self.char_end}"
                  if self.char_start is not None and self.char_end is not None else "")
        return (f"{concept_id_to_str(self.concept_id)}:"
                f"L{self.line_start}-L{self.line_end}{suffix}")

    @property
    def citation(self) -> str:
        suffix = (f"@C{self.char_start}-C{self.char_end}"
                  if self.char_start is not None and self.char_end is not None else "")
        return f"[{self.path}#L{self.line_start}-L{self.line_end}{suffix}]"

    def public_dict(self, *, include_metadata: bool = True) -> dict[str, Any]:
        result = {
            "concept_id": concept_id_to_str(self.concept_id), "path": self.path,
            "title": self.title, "type": self.type, "heading": self.heading,
            "heading_line": self.heading_line, "line_start": self.line_start,
            "line_end": self.line_end, "char_start": self.char_start,
            "char_end": self.char_end, "passage_id": self.passage_id,
            "citation": self.citation, "excerpt": self.excerpt,
            "tags": list(self.tags), "entities": list(self.entities),
            "relations": list(self.relations), "freshness": self.freshness,
            "provenance": _public_value(self.provenance),
            "citations": _public_value(self.citations),
        }
        if include_metadata:
            result["metadata"] = public_metadata(self.metadata)
        return result


def _selection_coordinates(passage: Passage, start: int, end: int) -> dict[str, int]:
    before = passage.excerpt[:start]
    through = passage.excerpt[:end]
    start_line = passage.line_start + before.count("\n")
    end_line = passage.line_start + through.count("\n")
    start_column = len(before.rsplit("\n", 1)[-1])
    end_column = len(through.rsplit("\n", 1)[-1])
    if passage.char_start is not None:
        start_column += passage.char_start
        end_column += passage.char_start
    return {"line_start": start_line, "line_end": end_line,
            "char_start": start_column, "char_end": end_column}


def _selection_citation(path: str, coordinates: dict[str, int]) -> str:
    return (f"[{path}#L{coordinates['line_start']}-L{coordinates['line_end']}"
            f"@C{coordinates['char_start']}-C{coordinates['char_end']}]")


def _resolve_exact_selection(
    passages: tuple[Passage, ...], request: ContextRequest
) -> tuple[dict[str, Any], Passage] | None:
    exact = request.selection_exact
    if exact is None:
        return None
    candidates: list[tuple[Passage, int, int]] = []
    for passage_index, passage in enumerate(passages):
        if passage_index % 64 == 0:
            check_retrieval_cancelled()
        if passage.concept_id != concept_id_from_str(request.concept or ""):
            continue
        if request.selection_heading is not None and passage.heading != request.selection_heading:
            continue
        offset = 0
        while True:
            found = passage.excerpt.find(exact, offset)
            if found < 0:
                break
            end = found + len(exact)
            prefix_ok = (request.selection_prefix is None
                         or passage.excerpt[:found].endswith(request.selection_prefix))
            suffix_ok = (request.selection_suffix is None
                         or passage.excerpt[end:].startswith(request.selection_suffix))
            if prefix_ok and suffix_ok:
                candidates.append((passage, found, end))
                if len(candidates) > 1:
                    raise RetrievalContractError(
                        "selection_ambiguous", "Selection matches multiple exact locations",
                        fields={"selection_exact": "ambiguous"}, status=409,
                    )
            offset = found + 1
    if not candidates:
        raise RetrievalContractError(
            "selection_not_found", "Selection does not match an exact representable passage",
            fields={"selection_exact": "not_found"}, status=400,
        )
    passage, start, end = candidates[0]
    coordinates = _selection_coordinates(passage, start, end)
    selection = {
        "status": "attached_exact", "exact": exact,
        "prefix": request.selection_prefix, "suffix": request.selection_suffix,
        "heading": request.selection_heading, "passage_id": passage.passage_id,
        "citation": _selection_citation(passage.path, coordinates),
        **coordinates,
    }
    return selection, passage


def _project_public_value(value: Any, *, _depth: int = 0) -> Any:
    """Bound authored-public metadata with explicit truncation markers."""
    from datetime import date, datetime
    if isinstance(value, (datetime, date)):
        value = value.isoformat()
    if _depth >= 4:
        return {"truncated": True, "reason": "max_depth"}
    if isinstance(value, float) and not math.isfinite(value):
        raise RetrievalContractError("invalid_public_metadata", "Public metadata must be finite")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= PUBLIC_METADATA_STRING_CODEPOINTS:
            return value
        return {"value": value[:PUBLIC_METADATA_STRING_CODEPOINTS], "truncated": True,
                "omitted_codepoints": len(value) - PUBLIC_METADATA_STRING_CODEPOINTS}
    if isinstance(value, (list, tuple)):
        projected = [_project_public_value(item, _depth=_depth + 1)
                     for item in value[:PUBLIC_METADATA_COLLECTION_ITEMS]]
        if len(value) > PUBLIC_METADATA_COLLECTION_ITEMS:
            return {"items": projected, "truncated": True,
                    "omitted_items": len(value) - PUBLIC_METADATA_COLLECTION_ITEMS}
        return projected
    if isinstance(value, dict):
        items = list(value.items())
        if any(not isinstance(key, str) for key, _item in items):
            raise RetrievalContractError(
                "invalid_public_metadata", "Public metadata object keys must be strings"
            )
        projected = {key: _project_public_value(item, _depth=_depth + 1)
                     for key, item in items[:PUBLIC_METADATA_COLLECTION_ITEMS]}
        if len(items) > PUBLIC_METADATA_COLLECTION_ITEMS:
            return {"entries": projected, "truncated": True,
                    "omitted_items": len(items) - PUBLIC_METADATA_COLLECTION_ITEMS}
        raw = canonical_json_bytes(projected).decode("utf-8")
        if len(raw) > PUBLIC_METADATA_MAX_CODEPOINTS:
            return {"preview": raw[:PUBLIC_METADATA_STRING_CODEPOINTS], "truncated": True,
                    "omitted_codepoints": len(raw) - PUBLIC_METADATA_STRING_CODEPOINTS}
        return projected
    raise RetrievalContractError(
        "invalid_public_metadata",
        "Public metadata contains an unsupported value type",
    )


def _public_value(value: Any) -> Any:
    projected = _project_public_value(value)
    raw = canonical_json_bytes(projected).decode("utf-8")
    if len(raw) <= PUBLIC_METADATA_MAX_CODEPOINTS:
        return projected
    return {"preview": raw[:PUBLIC_METADATA_STRING_CODEPOINTS], "truncated": True,
            "omitted_codepoints": len(raw) - PUBLIC_METADATA_STRING_CODEPOINTS}


def public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Bound each known family independently, keeping source window depth intact."""
    return {key: _public_value(value) for key, value in metadata.items()}


def _body_start(lines: list[str]) -> int:
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return i + 1
    return 0


def _heading_rows(lines: list[str], start: int) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    fence_char: str | None = None
    fence_len = 0
    for index in range(start, len(lines)):
        line = lines[index]
        fm = _FENCE.match(line)
        if fm:
            marker = fm.group(1)
            if fence_char is None:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char, fence_len = None, 0
            continue
        if fence_char is not None:
            continue
        hm = _HEADING.match(line)
        if hm:
            rows.append((index, len(hm.group(1)), hm.group(2).strip()))
    return rows


def _chunk_span(
    lines: list[str], start: int, end: int
) -> list[tuple[int, int, int | None, int | None, str]]:
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    if start >= end:
        return []
    chunks: list[tuple[int, int, int | None, int | None, str]] = []
    cursor = start
    while cursor < end:
        best = cursor
        size = 0
        i = cursor
        while i < end:
            addition = len(lines[i]) + (1 if i > cursor else 0)
            if size + addition > MAX_EXCERPT_CHARS:
                break
            size += addition
            best = i + 1
            i += 1
            if i < end and not lines[i].strip():
                best = i + 1
        if best == cursor:  # one physical line exceeds the excerpt cap
            text = lines[cursor]
            for off in range(0, len(text), MAX_EXCERPT_CHARS):
                char_end = min(len(text), off + MAX_EXCERPT_CHARS)
                chunks.append((cursor, cursor + 1, off, char_end, text[off:char_end]))
            cursor += 1
            continue
        chunk_end = best
        while chunk_end > cursor and not lines[chunk_end - 1].strip():
            chunk_end -= 1
        excerpt = "\n".join(lines[cursor:chunk_end])
        if excerpt:
            chunks.append((cursor, chunk_end, None, None, excerpt))
        cursor = best
        while cursor < end and not lines[cursor].strip():
            cursor += 1
    return chunks


def _entity_labels(concept: Concept) -> tuple[str, ...]:
    labels: list[str] = []
    value = concept.frontmatter.get("entities")
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                labels.append(item[:MAX_FILTER_CHARS])
            elif isinstance(item, dict) and item.get("label"):
                labels.append(str(item["label"])[:MAX_FILTER_CHARS])
    return tuple(labels[:MAX_EVIDENCE_EDGES])


def _relation_rows(concept: Concept) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    value = concept.frontmatter.get("relations")
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("target"):
                rows.append({"type": str(item.get("type", "relates_to"))[:MAX_FILTER_CHARS],
                             "target": str(item["target"])[:MAX_FILTER_CHARS]})
    rows.sort(key=lambda row: (row["type"], row["target"]))
    return tuple(rows[:MAX_EVIDENCE_EDGES])


def derive_passages(bundle: Bundle) -> tuple[Passage, ...]:
    validate_retrieval_work(bundle)
    passages: list[Passage] = []
    for concept_index, cid in enumerate(sorted(bundle.concepts)):
        if concept_index % 64 == 0:
            check_retrieval_cancelled()
        concept = bundle.concepts[cid]
        # Physical OKF coordinates recognize LF and CRLF only. Other Unicode
        # separators and lone CR remain content and cannot shift citations.
        lines = concept.raw_text.replace("\r\n", "\n").split("\n")
        start = _body_start(lines)
        headings = _heading_rows(lines, start)
        sections: list[tuple[int, int, str | None, int | None]] = []
        if not headings:
            sections.append((start, len(lines), None, None))
        else:
            if start < headings[0][0]:
                sections.append((start, headings[0][0], None, None))
            for pos, (line_index, _level, text) in enumerate(headings):
                next_index = headings[pos + 1][0] if pos + 1 < len(headings) else len(lines)
                sections.append((line_index + 1, next_index, text, line_index))
        metadata = concept.metadata
        common = dict(
            concept_id=cid, path=concept.rel_path.as_posix(),
            title=concept.title[:MAX_EXCERPT_CHARS],
            type=(concept.type[:MAX_FILTER_CHARS] if concept.type else None),
            tags=tuple(tag[:MAX_FILTER_CHARS] for tag in concept.tags[:MAX_EVIDENCE_EDGES]),
            entities=_entity_labels(concept),
            relations=_relation_rows(concept), freshness=metadata["generated_at"],
            provenance=concept.frontmatter.get("provenance"),
            citations=concept.frontmatter.get("citations"),
            metadata=metadata,
        )
        for sec_start, sec_end, heading, heading_index in sections:
            chunks = _chunk_span(lines, sec_start, sec_end)
            if not chunks and heading_index is not None:
                chunks = [(heading_index, heading_index + 1, None, None, lines[heading_index])]
            for chunk_start, chunk_end, char_start, char_end, excerpt in chunks:
                check_retrieval_cancelled()
                passages.append(Passage(heading=heading,
                    heading_line=(heading_index + 1 if heading_index is not None else None),
                    line_start=chunk_start + 1, line_end=chunk_end,
                    char_start=char_start, char_end=char_end, excerpt=excerpt, **common))
                if len(passages) > MAX_BUNDLE_PASSAGES:
                    raise RetrievalContractError("bundle_too_large", "Bundle exceeds passage work limit",
                                                 status=413)
    return tuple(passages)


def bundle_revision(bundle: Bundle) -> str:
    with bundle._derived_lock:
        check_retrieval_cancelled()
        cached = getattr(bundle, "_bundle_revision", None)
        if cached is not None:
            check_retrieval_cancelled()
            return cached
        validate_retrieval_work(bundle)
        digest = hashlib.sha256((ALGORITHM_VERSION + "\0").encode())
        records: list[tuple[str, str]] = []
        records.extend(("concept:" + c.rel_path.as_posix(), c.raw_text) for c in bundle.concepts.values())
        records.extend(("index:" + p.as_posix(), obj.raw_text) for p, obj in bundle.indexes.items())
        records.extend(("log:" + p.as_posix(), obj.raw_text) for p, obj in bundle.logs.items())
        check_retrieval_cancelled()
        for record_index, (key, raw) in enumerate(sorted(records)):
            if record_index % 64 == 0:
                check_retrieval_cancelled()
            digest.update(key.encode("utf-8") + b"\0")
            # Keep each non-cooperative encode/hash unit bounded even when one
            # authored document is very large.
            for offset in range(0, len(raw), 64 * 1024):
                check_retrieval_cancelled()
                digest.update(raw[offset:offset + 64 * 1024].encode("utf-8"))
            digest.update(b"\0")
        value = ALGORITHM_VERSION + ":" + digest.hexdigest()
        check_retrieval_cancelled()
        bundle._bundle_revision = value
        return value


def _passage_score(passage: Passage, query: str) -> tuple[int, int, int, str]:
    terms = tokenize(query)
    text = (passage.heading or "") + "\n" + passage.excerpt
    lower = text.lower()
    hits = sum(lower.count(term.lower()) for term in terms)
    heading_hits = sum((passage.heading or "").lower().count(term.lower()) for term in terms)
    return (-hits, -heading_hits, passage.line_start, passage.path)


def _best_passage(passages: Iterable[Passage], query: str) -> Passage | None:
    values = list(passages)
    return min(values, key=lambda p: _passage_score(p, query)) if values else None


def _passage_hit_count(passage: Passage, query: str) -> int:
    text = ((passage.heading or "") + "\n" + passage.excerpt).lower()
    return sum(text.count(term.lower()) for term in tokenize(query))


def _metadata_markdown(item: dict[str, Any]) -> str:
    """Separate normalized data from exact evidence; never synthesize footnotes.

    Public families are already bounded by ``public_metadata``. Escape fence,
    HTML and display-control characters as JSON data, without changing values
    after JSON decoding. Charge this entire block, including its labels.
    """
    if not item.get("metadata"):
        return ""
    # Context producers retain the canonical wire text so even a pack that has
    # passed through JavaScript (1.0 -> 1 in its decoded view) formats identically.
    # Older schema-v1 items without that additive field remain formattable.
    data = (item["metadata_json"] if "metadata_json" in item
            else canonical_json_bytes(item["metadata"]).decode("utf-8"))
    data = re.sub(
        r"[<>&`\u007f-\u009f\u2028-\u202e\u2066-\u2069]",
        lambda match: f"\\u{ord(match[0]):04x}", data,
    )
    return "\n".join([
        "### OKF 0.2 metadata and sources (normalized data)",
        "Trust is advisory, never authorization. Computation is metadata only, never executed.",
        "Source ids match [^id] exactly; no footnote definitions or legacy citations are synthesized.",
        "", "```json", data, "```", "", "### Exact excerpt", "", "",
    ])


def _markdown_item(item: dict[str, Any]) -> str:
    return "\n".join([
        f"## {item['citation']} {item['title']}",
        f"Type: {item['type'] or ''}",
        f"Backends: {', '.join(item['backends'])}",
        f"Reasons: {'; '.join(item['reasons']) or 'matched query'}",
        f"Relation path (undirected): {' ↔ '.join(item['relation_path'])}",
        f"Freshness: {item['freshness'] or ''}",
        f"Provenance: {canonical_json_bytes(item['provenance']).decode('utf-8')}",
        "",
        _metadata_markdown(item) + item["excerpt"],
    ])


def _selection_markdown(selection: dict[str, Any]) -> str:
    return "\n".join([
        "## Selected text",
        f"Citation: {selection['citation']}",
        f"Status: {selection['status']}",
        "",
        selection["exact"],
    ])


def _selection_token_cost(selection: dict[str, Any]) -> int:
    projection = {key: selection.get(key) for key in (
        "status", "exact", "prefix", "suffix", "heading", "passage_id", "citation",
        "line_start", "line_end", "char_start", "char_end",
    )}
    compact = json.dumps(projection, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return math.ceil(len(compact) / 4)


def governed_item_token_cost(item: dict[str, Any]) -> int:
    """Budget evidence plus the safe normalized metadata Markdown block.

    Legacy display fields remain excluded. Normalized OKF metadata is charged
    before item selection in every representation, not appended after budgeting.
    """
    projection = {key: item.get(key) for key in (
        "concept_id", "passage_id", "citation", "excerpt", "backends", "reasons",
        "graph_distance", "relation_path", "evidence",
    )}
    compact = json.dumps(projection, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return math.ceil(len(compact) / 4) + math.ceil(len(_metadata_markdown(item)) / 4)


def _context_item(
    result: SearchResult,
    passage: Passage,
    *,
    distance: int = 0,
    relation_path: list[ConceptId] | None = None,
) -> dict[str, Any]:
    """Construct one canonical schema-v1 context item for every pack producer."""
    reasons = list(result.reasons or [f"ranked by {result.source_backend}"])[:MAX_REASONS]
    base_evidence = result.evidence or {}
    metadata = public_metadata(passage.metadata)
    return {
        "passage_id": passage.passage_id, "citation": passage.citation,
        "concept_id": concept_id_to_str(result.concept_id), "path": passage.path,
        "title": passage.title, "type": passage.type, "heading": passage.heading,
        "heading_line": passage.heading_line, "line_start": passage.line_start,
        "line_end": passage.line_end, "char_start": passage.char_start,
        "char_end": passage.char_end, "excerpt": passage.excerpt,
        "score": result.score, "backends": list(result.backends or [result.source_backend]),
        "reasons": reasons, "graph_distance": distance,
        "relation_path": [concept_id_to_str(node) for node in
                          (relation_path or [result.concept_id])],
        "freshness": passage.freshness, "provenance": _public_value(passage.provenance),
        "metadata": metadata,
        # Preserve numeric spellings across JSON.parse/JSON.stringify in portable
        # consumers (e.g. 1.0, 1e-07). The decoded view remains additive metadata.
        "metadata_json": canonical_json_bytes(metadata).decode("utf-8"),
        "evidence": {"schema_version": PASSAGE_SCHEMA_VERSION,
                     "passage_id": passage.passage_id,
                     "edges": list(base_evidence.get("edges", []))[:MAX_EVIDENCE_EDGES],
                     "omitted_count": int(base_evidence.get("omitted_count", 0) or 0)},
    }


def _shortest_relation_path(bundle: Bundle, start: ConceptId, target: ConceptId,
                            max_depth: int) -> list[ConceptId] | None:
    if start == target:
        return [start]
    graph = bundle.graph()
    queue = deque([[start]])
    seen = {start}
    while queue:
        check_retrieval_cancelled()
        path = queue.popleft()
        if len(path) - 1 >= max_depth:
            continue
        node = path[-1]
        adjacent = {link.target for link in graph.out_edges.get(node, []) if link.target}
        adjacent.update(link.source for link in graph.in_edges.get(node, []))
        for neighbour in sorted(n for n in adjacent if n in graph.concept_ids):
            if neighbour in seen:
                continue
            candidate = path + [neighbour]
            if neighbour == target:
                return candidate
            seen.add(neighbour)
            queue.append(candidate)
    return None


def _bounded_graph_neighbour_paths(
    bundle: Bundle, seeds: list[ConceptId], *, max_depth: int,
    type_filter: str | None, tag: str | None, max_documents: int,
) -> dict[ConceptId, list[ConceptId]]:
    """Deterministic multi-source BFS with bounded published neighbours."""
    if max_depth <= 0 or max_documents <= 0:
        return {}
    graph = bundle.graph()
    seed_set = set(seeds)
    queue = deque([[seed] for seed in sorted(seed_set)])
    seen = set(seed_set)
    found: dict[ConceptId, list[ConceptId]] = {}
    while queue and len(found) < max_documents:
        check_retrieval_cancelled()
        path = queue.popleft()
        if len(path) - 1 >= max_depth:
            continue
        node = path[-1]
        adjacent = {link.target for link in graph.out_edges.get(node, []) if link.target}
        adjacent.update(link.source for link in graph.in_edges.get(node, []))
        for neighbour in sorted(n for n in adjacent if n in graph.concept_ids):
            if neighbour in seen:
                continue
            seen.add(neighbour)
            candidate = path + [neighbour]
            queue.append(candidate)
            concept = bundle.concepts[neighbour]
            if type_filter is not None and concept.type != type_filter:
                continue
            if tag is not None and tag.lower() not in {own.lower() for own in concept.tags}:
                continue
            found[neighbour] = candidate
            if len(found) >= max_documents:
                break
    return found


class RetrievalService:
    """Read-only retrieval over exactly one immutable Bundle snapshot."""

    def __init__(self, bundle: Bundle) -> None:
        self.bundle = bundle

    @property
    def passages(self) -> tuple[Passage, ...]:
        with self.bundle._derived_lock:
            check_retrieval_cancelled()
            cached = getattr(self.bundle, "_passage_index", None)
            if cached is None or cached[0] != PASSAGE_SCHEMA_VERSION:
                built = derive_passages(self.bundle)
                check_retrieval_cancelled()
                self.bundle._passage_index = (PASSAGE_SCHEMA_VERSION, built)
                return built
            check_retrieval_cancelled()
            return cached[1]

    @property
    def revision(self) -> str:
        return bundle_revision(self.bundle)

    def search(self, request: SearchRequest) -> list[SearchResult]:
        validate_retrieval_work(self.bundle)
        with self.bundle._derived_lock:
            return self._search_locked(request)

    def search_with_revision(self, request: SearchRequest) -> tuple[list[SearchResult], str]:
        """Return ranked rows and their revision under one generation lock."""
        validate_retrieval_work(self.bundle)
        with self.bundle._derived_lock:
            rows = self.search(request)
            check_retrieval_cancelled()
            return rows, self.revision

    def verified_section_fragment(
        self, concept_id: str, passage_id: str, *, revision: str,
    ) -> str | None:
        """Optional source-owned navigation proof; never changes schema-v1 rows.

        A stale result, cross-concept passage, or unavailable heading has no
        section destination. Consumers retain their ordinary document link and
        encode the returned DOM id as a fragment, not as Markdown or a URL.
        This does not establish a textual query match for graph-only results.
        """
        from .navigation import passage_heading_fragment

        if (not isinstance(concept_id, str) or not isinstance(passage_id, str)
                or not isinstance(revision, str)
                or len(passage_id) > MAX_QUERY_CHARS):
            return None
        try:
            cid = concept_id_from_str(canonical_concept_id(concept_id))
        except (ValueError, RetrievalContractError):
            return None
        if concept_id_to_str(cid) != concept_id:
            return None
        validate_retrieval_work(self.bundle)
        with self.bundle._derived_lock:
            if revision != self.revision or cid not in self.bundle.concepts:
                return None
            for index, passage in enumerate(self.passages):
                if index % 128 == 0:
                    check_retrieval_cancelled()
                if passage.concept_id == cid and passage.passage_id == passage_id:
                    fragment = passage_heading_fragment(self.bundle.concepts[cid], passage)
                    check_retrieval_cancelled()
                    return fragment
        return None

    def _search_locked(self, request: SearchRequest) -> list[SearchResult]:
        from .search import search_bundle
        req = request.normalized()
        for field, value in (("source", req.source), ("target", req.target)):
            if value and concept_id_from_str(value) not in self.bundle.concepts:
                raise RetrievalContractError("concept_not_found", "Concept target was not found",
                                             fields={field: "not_found"}, status=404)
        results = search_bundle(self.bundle, req.query, mode=SearchMode(req.mode),
                                type_filter=req.type_filter, tag=req.tag, limit=req.limit,
                                relation=req.relation, source=req.source, target=req.target)
        result_ids = {result.concept_id for result in results}
        best_by_concept: dict[ConceptId, Passage] = {}
        for passage_index, passage in enumerate(self.passages):
            if passage_index % 128 == 0:
                check_retrieval_cancelled()
            if passage.concept_id not in result_ids:
                continue
            current = best_by_concept.get(passage.concept_id)
            if current is None or _passage_score(passage, req.query) < _passage_score(current, req.query):
                best_by_concept[passage.concept_id] = passage
        for result in results:
            passage = best_by_concept.get(result.concept_id)
            backends = list(result.backends or [result.source_backend])
            reasons = [f"ranked by {backend}" for backend in backends][:MAX_REASONS]
            result.backends = backends
            result.reasons = reasons
            result.evidence = {
                "schema_version": PASSAGE_SCHEMA_VERSION,
                "passage": passage.public_dict() if passage else None,
                "edges": ((result.detail or {}).get("edges", [])[:MAX_EVIDENCE_EDGES]),
                "omitted_count": ((result.detail or {}).get("omitted_count", 0) +
                    max(0, len((result.detail or {}).get("edges", [])) - MAX_EVIDENCE_EDGES)),
            }
        return results

    def context(self, request: ContextRequest) -> dict[str, Any]:
        validate_retrieval_work(self.bundle)
        with self.bundle._derived_lock:
            return self._context_locked(request)

    def default_concept_contexts(
        self, concept_ids: Iterable[str] | None = None
    ) -> dict[str, str]:
        """Return canonical default concept-only Markdown keyed by concept id.

        This is the supported static-render integration seam.  It groups the
        canonical passage index in one pass without query ranking, then feeds
        target-owned passages through the same item construction, accounting,
        response caps, and serializer used by :meth:`context`.
        """
        validate_retrieval_work(self.bundle)
        with self.bundle._derived_lock:
            if concept_ids is None:
                requested = [concept_id_to_str(cid) for cid in sorted(self.bundle.concepts)]
            else:
                if isinstance(concept_ids, (str, bytes)):
                    raise RetrievalContractError(
                        "invalid_request", "concept_ids must be an iterable of concept ids",
                        fields={"concept_ids": "invalid_type"},
                    )
                requested = list(concept_ids)

            normalized: set[str] = set()
            for index, value in enumerate(requested):
                if index % 256 == 0:
                    check_retrieval_cancelled()
                if not isinstance(value, str):
                    raise RetrievalContractError(
                        "invalid_target", "Invalid canonical concept id",
                        fields={"concept": "invalid"},
                    )
                concept_id = canonical_concept_id(value)
                if concept_id in normalized:
                    raise RetrievalContractError(
                        "invalid_request", "Duplicate concept target",
                        fields={"concept": "duplicate"},
                    )
                if concept_id_from_str(concept_id) not in self.bundle.concepts:
                    raise RetrievalContractError(
                        "concept_not_found", "Concept target was not found",
                        fields={"concept": "not_found"}, status=404,
                    )
                normalized.add(concept_id)

            if not normalized:
                return {}

            target_cids = {concept_id_from_str(value) for value in normalized}
            passages_by_concept: dict[ConceptId, list[Passage]] = {}
            for passage_index, passage in enumerate(self.passages):
                if passage_index % 128 == 0:
                    check_retrieval_cancelled()
                if passage.concept_id in target_cids:
                    # derive_passages() emits sorted concept/source order, so
                    # append preserves canonical passage order in one O(P) pass.
                    passages_by_concept.setdefault(passage.concept_id, []).append(passage)

            output: dict[str, str] = {}
            for request_index, concept_id in enumerate(sorted(normalized)):
                if request_index % 32 == 0:
                    check_retrieval_cancelled()
                cid = concept_id_from_str(concept_id)
                concept = self.bundle.concepts[cid]
                req = ContextRequest(
                    query=concept.title,
                    concept=concept_id, mode="concept-only",
                    budget_tokens=6000, neighbors=0, limit=20,
                )
                result = SearchResult(
                    concept_id=cid, title=concept.title, score=1.0,
                    source_backend="concept-target", backends=["concept-target"],
                    reasons=["explicit concept target"], evidence=None,
                )
                owned_passages = passages_by_concept.get(cid, [])
                candidate_refs = [
                    (result, passage, 0, [cid]) for passage in owned_passages[:req.limit]
                ]
                pack = self._finalize_context_pack_locked(
                    req, candidate_refs, relevant_count=len(owned_passages), selection=None,
                )
                output[concept_id] = pack["representations"]["markdown"]
            return output

    def _context_locked(
        self,
        request: ContextRequest,
        *,
        normalized_request: ContextRequest | None = None,
        prefetched_results: list[SearchResult] | None = None,
        passage_subset: tuple[Passage, ...] | None = None,
    ) -> dict[str, Any]:
        req = normalized_request or request.normalized(self.bundle)
        passages = self.passages if passage_subset is None else passage_subset
        selection_resolution = _resolve_exact_selection(passages, req)
        selection = selection_resolution[0] if selection_resolution else None
        selection_passage = selection_resolution[1] if selection_resolution else None
        # An authored title is identity metadata, not public ``q`` input.  It
        # may exceed the public query cap, so use a deterministic bounded
        # ranking seed while retaining the exact target in ``concept``.
        ranking_query = req.query[:MAX_QUERY_CHARS] if req.concept and not request.query else req.query
        results = prefetched_results if prefetched_results is not None else self._search_locked(
            SearchRequest(query=ranking_query, mode=req.mode,
                type_filter=req.type_filter, tag=req.tag, entity=req.entity,
                relation=req.relation, source=req.source, target=req.target, limit=100)
        )
        if req.concept:
            target_cid = concept_id_from_str(req.concept)
            existing = next((result for result in results if result.concept_id == target_cid), None)
            if existing is not None:
                results.remove(existing)
                existing.reasons = ["explicit concept target"] + list(existing.reasons or [])
                results.insert(0, existing)
            else:
                concept = self.bundle.concepts[target_cid]
                results.insert(0, SearchResult(concept_id=target_cid, title=concept.title,
                    score=(max((r.score for r in results), default=0.0) + 1.0),
                    source_backend="concept-target", backends=["concept-target"],
                    reasons=["explicit concept target"], evidence=None))
        results = results[:MAX_CONTEXT_DOCUMENTS]
        result_by_id = {result.concept_id: result for result in results}
        neighbour_paths = _bounded_graph_neighbour_paths(
            self.bundle, [result.concept_id for result in results], max_depth=req.neighbors,
            type_filter=req.type_filter, tag=req.tag,
            max_documents=max(0, MAX_CONTEXT_DOCUMENTS - len(result_by_id)),
        )
        neighbour_paths = {cid: path for cid, path in neighbour_paths.items()
                           if cid not in result_by_id}

        # One passage-index scan. Keep only the deterministic top N passage
        # references per document; public metadata projection happens later.
        target_ids = set(result_by_id) | set(neighbour_paths)
        top_passages: dict[ConceptId, list[Passage]] = {cid: [] for cid in target_ids}
        relevant_counts: dict[ConceptId, int] = {cid: 0 for cid in target_ids}
        fallback: dict[ConceptId, Passage] = {}
        for passage_index, passage in enumerate(passages):
            if passage_index % 128 == 0:
                check_retrieval_cancelled()
            if passage.concept_id not in target_ids:
                continue
            current_fallback = fallback.get(passage.concept_id)
            if current_fallback is None or _passage_score(passage, req.query) < _passage_score(current_fallback, req.query):
                fallback[passage.concept_id] = passage
            if _passage_hit_count(passage, req.query) <= 0:
                continue
            relevant_counts[passage.concept_id] += 1
            group = top_passages[passage.concept_id]
            group.append(passage)
            group.sort(key=lambda candidate: _passage_score(candidate, req.query))
            del group[MAX_CONTEXT_PASSAGES_PER_DOCUMENT:]

        # Backend matches in title/tag/relation metadata still receive one
        # exact fallback passage when no body section contains query terms.
        for cid in result_by_id:
            if relevant_counts[cid] == 0 and cid in fallback:
                top_passages[cid] = [fallback[cid]]
                relevant_counts[cid] = 1

        ranked_groups: list[list[tuple[SearchResult, Passage, int, list[ConceptId]]]] = []
        for result in results:
            group = top_passages.get(result.concept_id, [])
            if group:
                ranked_groups.append([(result, passage, 0, [result.concept_id]) for passage in group])
        for cid, path in sorted(neighbour_paths.items(), key=lambda row: (len(row[1]), row[0])):
            group = top_passages.get(cid, [])
            if not group:  # neighbours must be query relevant; no fallback
                continue
            concept = self.bundle.concepts[cid]
            pseudo = SearchResult(concept_id=cid, title=concept.title, score=0.0,
                source_backend="graph", backends=["graph"],
                reasons=["query-relevant graph neighbour"])
            ranked_groups.append([(pseudo, passage, len(path) - 1, path) for passage in group])

        candidate_refs: list[tuple[SearchResult, Passage, int, list[ConceptId]]] = []
        seen_passages: set[str] = set()
        depth = 0
        while True:
            check_retrieval_cancelled()
            added = False
            for group in ranked_groups:
                if depth < len(group):
                    candidate = group[depth]
                    if candidate[1].passage_id not in seen_passages:
                        seen_passages.add(candidate[1].passage_id)
                        candidate_refs.append(candidate)
                    added = True
            if not added:
                break
            depth += 1

        if selection_passage is not None:
            target_result = result_by_id.get(selection_passage.concept_id)
            if target_result is None:
                concept = self.bundle.concepts[selection_passage.concept_id]
                target_result = SearchResult(
                    concept_id=selection_passage.concept_id, title=concept.title,
                    score=max((result.score for result in results), default=0.0) + 1.0,
                    source_backend="selection", backends=["selection"],
                    reasons=["contains exact selected text"],
                )
            existing_index = next((index for index, candidate in enumerate(candidate_refs)
                                   if candidate[1].passage_id == selection_passage.passage_id), None)
            if existing_index is not None:
                candidate_refs.pop(existing_index)
            elif _passage_hit_count(selection_passage, req.query) <= 0:
                relevant_counts[selection_passage.concept_id] = (
                    relevant_counts.get(selection_passage.concept_id, 0) + 1
                )
            candidate_refs.insert(0, (
                target_result, selection_passage, 0, [selection_passage.concept_id]
            ))

        return self._finalize_context_pack_locked(
            req, candidate_refs, relevant_count=sum(relevant_counts.values()),
            selection=selection,
        )

    def _finalize_context_pack_locked(
        self,
        req: ContextRequest,
        candidate_refs: list[tuple[SearchResult, Passage, int, list[ConceptId]]],
        *,
        relevant_count: int,
        selection: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Apply canonical item construction, budgets, caps, and serialization."""
        selected: list[dict[str, Any]] = []
        selection_cost = _selection_token_cost(selection) if selection is not None else 0
        estimated = selection_cost
        for result, passage, distance, path in candidate_refs[:req.limit]:
            check_retrieval_cancelled()
            item = _context_item(result, passage, distance=distance, relation_path=path)
            cost = governed_item_token_cost(item)
            if estimated + cost > req.budget_tokens:
                if selection is not None and passage.passage_id == selection["passage_id"]:
                    raise RetrievalContractError(
                        "budget_too_small", "Budget cannot include the exact selection and its passage",
                        fields={"budget_tokens": "too_small"}, status=400,
                    )
                continue
            item["estimated_tokens"] = cost
            selected.append(item)
            estimated += cost
        pack = {
            "schema_version": CONTEXT_SCHEMA_VERSION, "bundle_revision": self.revision,
            "query": req.query, "mode": req.mode,
            "filters": {"type": req.type_filter, "tag": req.tag, "entity": req.entity,
                        "relation": req.relation, "source": req.source, "target": req.target,
                        "concept": req.concept},
            "budget": {"requested_tokens": req.budget_tokens, "estimated_tokens": estimated},
            "neighbors": req.neighbors, "included": len(selected),
            "omitted": relevant_count - len(selected),
            "truncated": len(selected) < relevant_count, "items": selected,
        }
        if selection is not None:
            pack["selection"] = selection
        protected_items = 1 if selection is not None else 0
        while (len(pack["items"]) > protected_items and
             len(canonical_json_bytes(pack)) > MAX_RESPONSE_BYTES):
            check_retrieval_cancelled()
            removed = pack["items"].pop()
            pack["budget"]["estimated_tokens"] -= removed["estimated_tokens"]
            pack["included"] -= 1
            pack["omitted"] += 1
            pack["truncated"] = True
        check_retrieval_cancelled()
        if len(canonical_json_bytes(pack)) > MAX_RESPONSE_BYTES:
            raise RetrievalContractError("response_too_large", "Response cannot fit the size limit", status=413)
        while True:
            check_retrieval_cancelled()
            markdown = self.context_markdown(pack)
            check_retrieval_cancelled()
            pack["representations"] = {"markdown": markdown}
            if len(canonical_json_bytes(pack)) <= MAX_RESPONSE_BYTES:
                break
            if len(pack["items"]) <= protected_items:
                raise RetrievalContractError(
                    "response_too_large", "Response cannot fit the size limit", status=413
                )
            removed = pack["items"].pop()
            pack["budget"]["estimated_tokens"] -= removed["estimated_tokens"]
            pack["included"] -= 1
            pack["omitted"] += 1
            pack["truncated"] = True
        check_retrieval_cancelled()
        return pack

    @staticmethod
    def context_markdown(pack: dict[str, Any]) -> str:
        lines = ["# OKF Context Pack", "",
                 f"- Schema: {pack['schema_version']}",
                 f"- Bundle revision: `{pack['bundle_revision']}`",
                 f"- Query: `{pack['query']}`", f"- Mode: `{pack['mode']}`",
                 f"- Budget: {pack['budget']['estimated_tokens']}/{pack['budget']['requested_tokens']} estimated tokens",
                 f"- Included: {pack['included']}; omitted: {pack['omitted']}", ""]
        if pack.get("selection") is not None:
            lines.extend([_selection_markdown(pack["selection"]), ""])
        for item in pack["items"]:
            lines.extend([_markdown_item(item), ""])
        return "\n".join(lines).rstrip() + "\n"


def corpus_chunk_identity(
    revision: str,
    index: int,
    passages: list[dict[str, Any]] | None = None,
    *,
    content_hash: str | None = None,
) -> str:
    """Return the stable chunk identity without duplicating canonical work.

    Producers that already serialized the chunk pass its SHA-256 digest.  The
    ``passages`` form remains for compatibility and uses the cancellable
    canonical serializer.
    """
    check_retrieval_cancelled()
    if content_hash is None:
        if passages is None:
            raise ValueError("passages or content_hash is required")
        content_hash = hashlib.sha256(canonical_json_bytes(passages)).hexdigest()
    check_retrieval_cancelled()
    return f"{revision}:{index:06d}:{content_hash}"


def build_passage_corpus(bundle: Bundle, *, chunk_size: int = 250) -> dict[str, Any]:
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")
    service = RetrievalService(bundle)
    with bundle._derived_lock:
        passages = service.passages
        revision = service.revision
        rows: list[dict[str, Any]] = []
        for index, passage in enumerate(passages):
            if index % 64 == 0:
                check_retrieval_cancelled()
            # Portable schema-v1 consumers enforce an exact field set. Keep
            # that wire shape until Python and browser consumers can upgrade
            # together; live search/context metadata is an additive API.
            rows.append(passage.public_dict(include_metadata=False))
        chunks: list[dict[str, Any]] = []
        descriptors: list[dict[str, Any]] = []
        for index, offset in enumerate(range(0, len(rows), chunk_size)):
            check_retrieval_cancelled()
            data = rows[offset:offset + chunk_size]
            raw = canonical_json_bytes(data)
            content_hash = hashlib.sha256(raw).hexdigest()
            chunk_id = corpus_chunk_identity(revision, index, content_hash=content_hash)
            filename = f"passages-{index:06d}-{content_hash[:16]}.json"
            chunks.append({"schema_version": CORPUS_SCHEMA_VERSION,
                           "bundle_revision": revision, "index": index,
                           "count": len(data), "id": chunk_id, "hash": content_hash,
                           "passages": data})
            descriptors.append({"file": filename, "id": chunk_id, "hash": content_hash,
                                "index": index, "count": len(data)})
        manifest = {"schema_version": CORPUS_SCHEMA_VERSION,
                    "algorithm_version": ALGORITHM_VERSION,
                    "bundle_revision": revision, "passage_count": len(rows),
                    "chunk_count": len(chunks), "chunks": descriptors}
        check_retrieval_cancelled()
        return {"manifest": manifest, "chunks": chunks}


def load_passage_corpus(artifact: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Validate a complete artifact before publishing any passage rows."""
    check_retrieval_cancelled()
    if not isinstance(artifact, dict) or set(artifact) != {"manifest", "chunks"}:
        raise RetrievalContractError("invalid_corpus", "Corpus artifact fields are invalid")
    if not isinstance(artifact.get("manifest"), dict):
        raise RetrievalContractError("invalid_corpus", "Corpus manifest is missing")
    manifest = artifact["manifest"]
    expected_manifest_fields = {"schema_version", "algorithm_version", "bundle_revision",
                                "passage_count", "chunk_count", "chunks"}
    if set(manifest) != expected_manifest_fields:
        raise RetrievalContractError("invalid_corpus", "Corpus manifest fields are invalid")
    if (not _is_canonical_int(manifest.get("schema_version"))
            or manifest.get("schema_version") != CORPUS_SCHEMA_VERSION):
        raise RetrievalContractError("unsupported_corpus_schema", "Unsupported corpus schema")
    if manifest.get("algorithm_version") != ALGORITHM_VERSION:
        raise RetrievalContractError("unsupported_corpus_algorithm", "Unsupported corpus algorithm")
    revision = manifest.get("bundle_revision")
    descriptors = manifest.get("chunks")
    chunks = artifact.get("chunks")
    if (not isinstance(revision, str) or _utf8_length(revision) is None
            or not isinstance(descriptors, list) or not isinstance(chunks, list)):
        raise RetrievalContractError("invalid_corpus", "Corpus metadata is malformed")
    if (not _is_canonical_int(manifest.get("chunk_count"), minimum=0)
            or not _is_canonical_int(manifest.get("passage_count"), minimum=0)
            or manifest.get("chunk_count") != len(descriptors)
            or len(chunks) != len(descriptors)):
        raise RetrievalContractError("incomplete_corpus", "Corpus chunks are incomplete")
    if (manifest["chunk_count"] > MAX_BUNDLE_PASSAGES
            or manifest["passage_count"] > MAX_BUNDLE_PASSAGES):
        raise RetrievalContractError("corpus_too_large", "Corpus exceeds passage limits", status=413)
    descriptor_by_index: dict[int, dict[str, Any]] = {}
    seen_files: set[str] = set()
    seen_descriptor_ids: set[str] = set()
    expected_descriptor_fields = {"file", "id", "hash", "index", "count"}
    descriptor_total = 0
    for descriptor_index, descriptor in enumerate(descriptors):
        if descriptor_index % 64 == 0:
            check_retrieval_cancelled()
        if (not isinstance(descriptor, dict) or set(descriptor) != expected_descriptor_fields
                or not _is_canonical_int(descriptor.get("index"), minimum=0)
                or not _is_canonical_int(descriptor.get("count"), minimum=0)):
            raise RetrievalContractError("invalid_corpus", "Chunk descriptor is malformed")
        index = descriptor["index"]
        if descriptor["count"] > MAX_BUNDLE_PASSAGES:
            raise RetrievalContractError("corpus_too_large", "Corpus chunk count exceeds limit",
                                         status=413)
        descriptor_total += descriptor["count"]
        if descriptor_total > MAX_BUNDLE_PASSAGES:
            raise RetrievalContractError("corpus_too_large", "Corpus exceeds passage limits",
                                         status=413)
        if index in descriptor_by_index:
            raise RetrievalContractError("invalid_corpus", "Duplicate corpus chunk index")
        filename = descriptor.get("file")
        chunk_id = descriptor.get("id")
        if (not isinstance(filename, str) or _utf8_length(filename) is None
                or filename in seen_files):
            raise RetrievalContractError("invalid_corpus", "Duplicate or missing corpus chunk file")
        if (not isinstance(chunk_id, str) or _utf8_length(chunk_id) is None
                or _utf8_length(descriptor.get("hash")) is None
                or chunk_id in seen_descriptor_ids):
            raise RetrievalContractError("invalid_corpus", "Duplicate or missing corpus chunk id")
        seen_files.add(filename)
        seen_descriptor_ids.add(chunk_id)
        descriptor_by_index[index] = descriptor
    canonical_indices = list(range(len(descriptors)))
    if [descriptor["index"] for descriptor in descriptors] != canonical_indices:
        raise RetrievalContractError("incomplete_corpus", "Corpus chunk sequence is incomplete")
    if descriptor_total != manifest["passage_count"]:
        raise RetrievalContractError("incomplete_corpus", "Descriptor passage count is incomplete")
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    expected_chunk_fields = {"schema_version", "bundle_revision", "index", "count",
                             "id", "hash", "passages"}
    if any(not isinstance(chunk, dict) for chunk in chunks):
        raise RetrievalContractError("invalid_corpus", "Corpus chunk is malformed")
    # Validate sortable identity fields before ordering attacker-controlled
    # objects. Mixed integer/string indices must stay inside the corpus error
    # contract rather than escaping as Python's comparison TypeError.
    for chunk_position, chunk in enumerate(chunks):
        if chunk_position % 64 == 0:
            check_retrieval_cancelled()
        if set(chunk) != expected_chunk_fields:
            raise RetrievalContractError("invalid_corpus", "Corpus chunk fields are invalid")
        if (not _is_canonical_int(chunk.get("index"), minimum=0)
                or not _is_canonical_int(chunk.get("count"), minimum=0)):
            raise RetrievalContractError("invalid_corpus", "Corpus chunk index or count is invalid")
        if (not _is_canonical_int(chunk.get("schema_version"))
                or chunk.get("schema_version") != CORPUS_SCHEMA_VERSION):
            raise RetrievalContractError("unsupported_corpus_schema", "Unsupported chunk schema")
        if chunk.get("bundle_revision") != revision:
            raise RetrievalContractError("mixed_corpus_revision", "Corpus revisions do not match")
        if (_utf8_length(chunk.get("id")) is None
                or _utf8_length(chunk.get("hash")) is None):
            raise RetrievalContractError("invalid_corpus", "Corpus chunk strings are invalid")
    check_retrieval_cancelled()
    ordered_chunks = sorted(chunks, key=lambda chunk: chunk.get("index", -1))
    seen_chunk_indices: set[int] = set()
    seen_passage_ids: set[str] = set()
    projection_work = {"visited": 0, "remaining": MAX_CORPUS_PROJECTION_NODES}
    corpus_canonical_size = 0
    validated_count = 0
    for chunk_position, chunk in enumerate(ordered_chunks):
        if chunk_position % 32 == 0:
            check_retrieval_cancelled()
        if (set(chunk) != expected_chunk_fields
                or not _is_canonical_int(chunk.get("schema_version"))
                or chunk.get("schema_version") != CORPUS_SCHEMA_VERSION):
            raise RetrievalContractError("unsupported_corpus_schema", "Unsupported chunk schema")
        if chunk.get("bundle_revision") != revision:
            raise RetrievalContractError("mixed_corpus_revision", "Corpus revisions do not match")
        index = chunk.get("index")
        if not _is_canonical_int(index, minimum=0) or index in seen_chunk_indices:
            raise RetrievalContractError("invalid_corpus", "Duplicate or invalid chunk index")
        seen_chunk_indices.add(index)
        descriptor = descriptor_by_index.get(index)
        if descriptor is None or chunk.get("id") != descriptor.get("id"):
            raise RetrievalContractError("invalid_corpus", "Chunk identity does not match manifest")
        if chunk["id"] in seen_ids:
            raise RetrievalContractError("invalid_corpus", "Duplicate corpus chunk id")
        seen_ids.add(chunk["id"])
        passages = chunk.get("passages")
        if (not isinstance(passages, list)
                or not _is_canonical_int(chunk.get("count"), minimum=0)
                or chunk.get("count") != len(passages)):
            raise RetrievalContractError("invalid_corpus", "Chunk count does not match payload")
        if chunk["count"] > MAX_BUNDLE_PASSAGES:
            raise RetrievalContractError("corpus_too_large", "Corpus chunk count exceeds limit",
                                         status=413)
        validated_count += chunk["count"]
        if validated_count > manifest["passage_count"]:
            raise RetrievalContractError("incomplete_corpus", "Corpus passage count is inconsistent")
        # Validate all field/count bounds before serializing potentially
        # attacker-controlled passage payloads. Nothing is appended publicly
        # until the complete chunk passes validation and identity checks.
        chunk_rows: list[dict[str, Any]] = []
        encoded_rows: list[bytes] = []
        # Exact canonical array size: opening + closing brackets, plus commas.
        canonical_size = 2
        for passage_index, passage in enumerate(passages):
            if passage_index % 64 == 0:
                check_retrieval_cancelled()
            _validate_corpus_passage(passage, seen_passage_ids,
                                     projection_work=projection_work)
            # Serialize each already-bounded passage exactly once. This keeps
            # nested provenance/citation traversal local and lets us reject
            # aggregate overflow before allocating one monolithic chunk body.
            encoded = canonical_json_bytes(passage)
            check_retrieval_cancelled()
            next_size = canonical_size + len(encoded) + (1 if encoded_rows else 0)
            if next_size > MAX_RESPONSE_BYTES:
                raise RetrievalContractError(
                    "corpus_too_large", "Corpus chunk exceeds size limit", status=413
                )
            canonical_size = next_size
            chunk_rows.append(passage)
            encoded_rows.append(encoded)
        check_retrieval_cancelled()
        raw = b"[" + b",".join(encoded_rows) + b"]"
        if len(raw) != canonical_size:  # defensive accounting invariant
            raise RetrievalContractError("invalid_corpus", "Corpus canonical size is inconsistent")
        next_corpus_size = corpus_canonical_size + len(raw)
        if next_corpus_size > MAX_CORPUS_CANONICAL_BYTES:
            raise RetrievalContractError(
                "corpus_too_large", "Corpus exceeds aggregate canonical byte limit", status=413
            )
        corpus_canonical_size = next_corpus_size
        content_hash = hashlib.sha256(raw).hexdigest()
        if content_hash != chunk.get("hash") or content_hash != descriptor.get("hash"):
            raise RetrievalContractError("corpus_hash_mismatch", "Chunk hash does not match payload")
        expected_id = corpus_chunk_identity(revision, index, content_hash=content_hash)
        expected_file = f"passages-{index:06d}-{content_hash[:16]}.json"
        if chunk.get("id") != expected_id or descriptor.get("file") != expected_file:
            raise RetrievalContractError("corpus_hash_mismatch", "Chunk identity is not content addressed")
        if descriptor.get("count") != len(passages):
            raise RetrievalContractError("invalid_corpus", "Descriptor count does not match payload")
        check_retrieval_cancelled()
        rows.extend(chunk_rows)
    if manifest.get("passage_count") != len(rows):
        raise RetrievalContractError("incomplete_corpus", "Corpus passage count is incomplete")
    order = [_corpus_passage_order_key(passage) for passage in rows]
    if order != sorted(order):
        raise RetrievalContractError("invalid_corpus", "Corpus passages are not canonically ordered")
    check_retrieval_cancelled()
    return tuple(rows)


def _is_canonical_int(value: Any, *, minimum: int | None = None) -> bool:
    """True only for JSON integer values; booleans and floats are never integers."""
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return minimum is None or value >= minimum


_CORPUS_PASSAGE_FIELDS = {
    "concept_id", "path", "title", "type", "heading", "heading_line",
    "line_start", "line_end", "char_start", "char_end", "passage_id",
    "citation", "excerpt", "tags", "entities", "relations", "freshness",
    "provenance", "citations",
}


def _valid_public_projection(
    value: Any, *, depth: int = 0, work_state: dict[str, int] | None = None,
    size_state: dict[str, int] | None = None,
) -> bool:
    if work_state is None:
        work_state = {"visited": 0, "remaining": MAX_CORPUS_PROJECTION_NODES}
    work_state["visited"] += 1
    work_state["remaining"] -= 1
    _projection_checkpoint(work_state, depth)
    if work_state["remaining"] < 0:
        raise RetrievalContractError(
            "corpus_too_large", "Corpus projection work limit exceeded", status=413
        )
    if size_state is None:
        size_state = {"codepoints": 0}

    def add_size(amount: int) -> bool:
        size_state["codepoints"] += amount
        return size_state["codepoints"] <= PUBLIC_METADATA_MAX_CODEPOINTS
    if depth > 4:
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    if value is None or isinstance(value, (bool, int, float)):
        return add_size(len(json.dumps(value, ensure_ascii=False, allow_nan=False)))
    if isinstance(value, str):
        return (_utf8_length(value) is not None
                and len(value) <= PUBLIC_METADATA_STRING_CODEPOINTS
                and add_size(len(json.dumps(value, ensure_ascii=False))))
    if isinstance(value, list):
        return (len(value) <= PUBLIC_METADATA_COLLECTION_ITEMS
                and add_size(2 + max(0, len(value) - 1)) and all(
            _valid_public_projection(item, depth=depth + 1, work_state=work_state,
                                     size_state=size_state)
            for item in value
        ))
    if isinstance(value, dict):
        if len(value) > PUBLIC_METADATA_COLLECTION_ITEMS:
            return False
        if not add_size(2 + max(0, len(value) - 1)):
            return False
        for key, item in value.items():
            if (not isinstance(key, str)
                    or len(key) > PUBLIC_METADATA_KEY_CODEPOINTS
                    or _utf8_length(key) is None
                    or (_utf8_length(key) or 0) > PUBLIC_METADATA_KEY_BYTES):
                return False
            if not add_size(len(json.dumps(key, ensure_ascii=False)) + 1):
                return False
            work_state["visited"] += 1
            work_state["remaining"] -= 1
            _projection_checkpoint(work_state, depth + 1)
            if work_state["remaining"] < 0:
                raise RetrievalContractError(
                    "corpus_too_large", "Corpus projection work limit exceeded", status=413
                )
            if not _valid_public_projection(item, depth=depth + 1, work_state=work_state,
                                            size_state=size_state):
                return False
        return True
    return False


def _corpus_passage_order_key(passage: dict[str, Any]) -> tuple[Any, ...]:
    return (passage["concept_id"], passage["line_start"], passage["line_end"],
            -1 if passage["char_start"] is None else passage["char_start"],
            -1 if passage["char_end"] is None else passage["char_end"],
            passage["passage_id"])


def _validate_corpus_passage(
    passage: Any, seen_ids: set[str], *, projection_work: dict[str, int] | None = None
) -> None:
    if not isinstance(passage, dict) or set(passage) != _CORPUS_PASSAGE_FIELDS:
        raise RetrievalContractError("invalid_corpus_passage", "Passage fields are invalid")
    concept_id = passage["concept_id"]
    path = passage["path"]
    if (not isinstance(concept_id, str) or not isinstance(path, str)
            or _utf8_length(concept_id) is None or _utf8_length(path) is None):
        raise RetrievalContractError("invalid_corpus_passage", "Passage identity types are invalid")
    try:
        canonical_id = concept_id_to_str(concept_id_from_str(concept_id))
    except ConceptIdError as exc:
        raise RetrievalContractError("invalid_corpus_passage", "Passage concept id is invalid") from exc
    if canonical_id != concept_id:
        raise RetrievalContractError("invalid_corpus_passage", "Passage concept id is not canonical")
    pure_path = PurePosixPath(path)
    if (pure_path.is_absolute() or pure_path.suffix != ".md" or "\\" in path
            or any(part in {"", ".", ".."} for part in pure_path.parts)
            or pure_path.name in {"index.md", "log.md"}
            or pure_path.as_posix() != path or path[:-3] != concept_id):
        raise RetrievalContractError("invalid_corpus_passage", "Passage path is invalid")
    scalar_strings = (("title", MAX_EXCERPT_CHARS),)
    for field, maximum in scalar_strings:
        if (not isinstance(passage[field], str) or len(passage[field]) > maximum
                or _utf8_length(passage[field]) is None):
            raise RetrievalContractError("invalid_corpus_passage", f"Passage {field} is invalid")
    for field, maximum in (("type", MAX_FILTER_CHARS), ("heading", MAX_EXCERPT_CHARS),
                           ("freshness", MAX_FILTER_CHARS)):
        value = passage[field]
        if value is not None and (not isinstance(value, str) or len(value) > maximum
                                  or _utf8_length(value) is None):
            raise RetrievalContractError("invalid_corpus_passage", f"Passage {field} is invalid")
    for field in ("line_start", "line_end"):
        value = passage[field]
        if not _is_canonical_int(value, minimum=1):
            raise RetrievalContractError("invalid_corpus_passage", "Passage line bounds are invalid")
    if passage["line_end"] < passage["line_start"]:
        raise RetrievalContractError("invalid_corpus_passage", "Passage line bounds are reversed")
    heading_line = passage["heading_line"]
    if (passage["heading"] is None) != (heading_line is None):
        raise RetrievalContractError("invalid_corpus_passage", "Passage heading identity is incomplete")
    if heading_line is not None and (not _is_canonical_int(heading_line, minimum=1)
                                     or heading_line > passage["line_start"]):
        raise RetrievalContractError("invalid_corpus_passage", "Passage heading line is invalid")
    char_start, char_end = passage["char_start"], passage["char_end"]
    if (char_start is None) != (char_end is None):
        raise RetrievalContractError("invalid_corpus_passage", "Passage character bounds are incomplete")
    if char_start is not None:
        if (not _is_canonical_int(char_start, minimum=0)
                or not _is_canonical_int(char_end, minimum=0)
                or char_end <= char_start
                or passage["line_start"] != passage["line_end"]):
            raise RetrievalContractError("invalid_corpus_passage", "Passage character bounds are invalid")
    excerpt = passage["excerpt"]
    if (not isinstance(excerpt, str) or len(excerpt) > MAX_EXCERPT_CHARS
            or _utf8_length(excerpt) is None):
        raise RetrievalContractError("invalid_corpus_passage", "Passage excerpt is invalid")
    if char_start is not None and len(excerpt) != char_end - char_start:
        raise RetrievalContractError("invalid_corpus_passage", "Passage character span does not match excerpt")
    suffix = f"@C{char_start}-C{char_end}" if char_start is not None else ""
    expected_id = f"{concept_id}:L{passage['line_start']}-L{passage['line_end']}{suffix}"
    expected_citation = f"[{path}#L{passage['line_start']}-L{passage['line_end']}{suffix}]"
    if (_utf8_length(passage.get("passage_id")) is None
            or _utf8_length(passage.get("citation")) is None
            or passage["passage_id"] != expected_id or passage["citation"] != expected_citation):
        raise RetrievalContractError("invalid_corpus_passage", "Passage identity does not match coordinates")
    if expected_id in seen_ids:
        raise RetrievalContractError("invalid_corpus_passage", "Duplicate passage identity")
    seen_ids.add(expected_id)
    for field in ("tags", "entities"):
        value = passage[field]
        if (not isinstance(value, list) or len(value) > MAX_EVIDENCE_EDGES
              or any(not isinstance(item, str) or len(item) > MAX_FILTER_CHARS
                     or _utf8_length(item) is None for item in value)):
            raise RetrievalContractError("invalid_corpus_passage", f"Passage {field} is invalid")
    relations = passage["relations"]
    if (not isinstance(relations, list) or len(relations) > MAX_EVIDENCE_EDGES
            or any(not isinstance(row, dict) or set(row) != {"type", "target"}
                 or any(not isinstance(row[key], str) or len(row[key]) > MAX_FILTER_CHARS
                        or _utf8_length(row[key]) is None
                          for key in ("type", "target")) for row in relations)):
        raise RetrievalContractError("invalid_corpus_passage", "Passage relations are invalid")
    for field in ("provenance", "citations"):
        value = passage[field]
        if not _valid_public_projection(
            value, work_state=projection_work, size_state={"codepoints": 0}
        ):
            raise RetrievalContractError("invalid_corpus_passage", f"Passage {field} is invalid")


def bound_search_payload(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Fit a bare-array response and return deterministic tail omission count."""
    bounded = list(rows)
    omitted = 0
    while True:
        check_retrieval_cancelled()
        if len(canonical_json_bytes(bounded)) <= MAX_RESPONSE_BYTES:
            if not bounded and rows:
                raise RetrievalContractError("response_too_large",
                                             "No search result fits the response size limit",
                                             status=413)
            check_retrieval_cancelled()
            return bounded, omitted
        if not bounded:
            raise RetrievalContractError("response_too_large", "Response cannot fit the size limit", status=413)
        bounded.pop()
        omitted += 1


def strict_percent_decode(value: str, *, plus_as_space: bool = True) -> str:
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise RetrievalContractError("malformed_encoding", "Malformed percent encoding")
    try:
        encoded = value.replace("+", " ") if plus_as_space else value
        return unquote_to_bytes(encoded).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RetrievalContractError("malformed_encoding", "Malformed UTF-8 encoding") from exc


def strict_decode_path(raw_path: str) -> str:
    """Decode one request path exactly once and reject routing aliases."""
    if re.search(r"%(?:2f|5c)", raw_path, re.I):
        raise RetrievalContractError("invalid_path", "Encoded path separators are not allowed",
                                     fields={"path": "encoded_separator"})
    path = strict_percent_decode(raw_path, plus_as_space=False)
    if "\\" in path or any(
        unicodedata.category(ch) == "Cc" or ch in _BIDI_OVERRIDES for ch in path
    ):
        raise RetrievalContractError("invalid_path", "Path contains forbidden characters",
                                     fields={"path": "forbidden_character"})
    raw_segments = raw_path.split("/")
    segments = path.split("/")
    for raw_segment, segment in zip(raw_segments, segments):
        if segment in {".", ".."}:
            raise RetrievalContractError("invalid_path", "Dot traversal is not allowed",
                                         fields={"path": "traversal"})
        if "%" in raw_segment and segment.startswith("__"):
            raise RetrievalContractError("invalid_path", "Encoded reserved route aliases are not allowed",
                                         fields={"path": "reserved_alias"})
    return path


def parse_http_query(raw: str, *, allowed: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    if not raw:
        return values
    for pair in raw.split("&"):
        key_raw, sep, value_raw = pair.partition("=")
        key = strict_percent_decode(key_raw)
        if key in {"source", "target", "concept"} and re.search(r"%(?:2f|5c)", value_raw, re.I):
            raise RetrievalContractError("invalid_target", "Encoded slash or backslash is not allowed",
                                         fields={key: "invalid"})
        value = strict_percent_decode(value_raw if sep else "")
        if key not in allowed:
            raise RetrievalContractError("unknown_parameter", "Unknown query parameter",
                                         fields={key or "query": "unknown"})
        if key in values:
            raise RetrievalContractError("duplicate_parameter", "Duplicate query parameter",
                                         fields={key: "duplicate"})
        values[key] = value
    return values


def prescan_search_error_format(raw: str) -> str:
    """Bounded decoded format intent for errors before strict full parsing.

    All decoded format values must be exactly ``json``. Duplicate ``json``
    still negotiates JSON while the main parser rejects the duplicate. Any
    conflicting, invalid or unsupported format deterministically selects HTML.
    """
    pairs = raw.split("&") if raw else []
    if len(pairs) > 100:
        return "html"
    values: list[str] = []
    invalid = False
    for pair in pairs:
        key_raw, sep, value_raw = pair.partition("=")
        try:
            key = strict_percent_decode(key_raw)
        except RetrievalContractError:
            continue
        if key != "format":
            continue
        try:
            values.append(strict_percent_decode(value_raw if sep else ""))
        except RetrievalContractError:
            invalid = True
    if invalid or not values or any(value != "json" for value in values):
        return "html"
    return "json"


def parse_int_param(params: dict[str, str], key: str, default: int) -> int:
    raw = params.get(key)
    if raw is None:
        return default
    if len(raw) > 10 or not re.fullmatch(r"[0-9]+", raw):
        raise RetrievalContractError("invalid_request", f"{key} must be an integer",
                                     fields={key: "invalid_integer"})
    try:
        return int(raw)
    except (ValueError, OverflowError) as exc:
        raise RetrievalContractError("invalid_request", f"{key} must be an integer",
                                     fields={key: "invalid_integer"}) from exc


__all__ = ["SearchRequest", "ContextRequest", "Passage", "RetrievalService",
           "RetrievalContractError", "build_passage_corpus", "bundle_revision",
           "corpus_chunk_identity", "governed_item_token_cost", "load_passage_corpus",
           "parse_http_query", "parse_int_param", "strict_decode_path",
           "bound_search_payload", "canonical_json_bytes", "MAX_RESPONSE_BYTES", "MAX_REQUEST_TARGET_BYTES"]
