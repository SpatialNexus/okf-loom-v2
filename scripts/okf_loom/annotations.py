"""Pure Annotations v2 capture and resolution primitives.

The block grammar lives in :mod:`okf_loom.markdown_blocks` (the single shared
IR). This module adapts that IR for the annotation extractor and implements the
closed-schema anchor validator and the commutative candidate-set resolver.

Extractor-v2 is intentionally deterministic rather than renderer-dependent. In
source order it emits ATX/setext headings, paragraphs, complete list items
(including indented/nested continuation), block quotes (including lazy
continuation), fenced/indented code, individual table rows, and HTML blocks.
HTML blocks include paired containers, standalone void/self-closing tags,
comments, declarations, processing instructions, and CDATA.  Spans retain
exact source characters and line endings, exclude trailing separating blank
lines, and hashes normalize CRLF to LF only.

Offsets are Python string offsets (Unicode code points).  This module neither
normalises source text nor mutates stored anchor dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterable, Mapping

from .markdown_blocks import MarkdownBlock as Block, parse_markdown_blocks

RESOLVER_VERSION = 2
MAX_EXACT = 16_384
MAX_CONTEXT = 256
_ANCHOR_KEYS = {"schema", "kind", "exact", "prefix", "suffix", "heading", "block", "position"}
_BLOCK_KEYS = {"id", "hash", "start", "end"}
_POSITION_KEYS = {"start", "end"}
_HEADING_KEYS = {"path"}
_BLOCK_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,255}$")
_BLOCK_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class _Candidate:
    start: int
    end: int
    block: Block


class AnchorError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def concept_content_rev(source: str) -> str:
    """Return the SHA-256 revision of the exact UTF-8 canonical source bytes."""
    if not isinstance(source, str):
        raise TypeError("source must be a string")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def extract_blocks(source: str) -> tuple[Block, ...]:
    """Extract deterministic v2 containing blocks in canonical source order.

    Thin adapter over :func:`okf_loom.markdown_blocks.parse_markdown_blocks`,
    which owns the single shared block grammar.
    """
    return parse_markdown_blocks(source)


def _closed(obj: Any, keys: set[str]) -> bool:
    return isinstance(obj, Mapping) and set(obj) == keys


def parse_v2_anchor(anchor: Any, *, public: bool = True) -> Mapping[str, Any]:
    """Validate a v2 anchor; public input uses the closed schema, stored input is open."""
    if not isinstance(anchor, Mapping):
        raise AnchorError("malformed_anchor")
    if anchor.get("schema") != 2:
        raise AnchorError("unsupported_schema" if "schema" in anchor else "malformed_anchor")
    if public and not _closed(anchor, _ANCHOR_KEYS):
        raise AnchorError("malformed_anchor")
    if not _ANCHOR_KEYS.issubset(anchor) or anchor.get("kind") != "text-quote":
        raise AnchorError("malformed_anchor")
    exact, prefix, suffix = anchor["exact"], anchor["prefix"], anchor["suffix"]
    if not all(isinstance(x, str) for x in (exact, prefix, suffix)):
        raise AnchorError("malformed_anchor")
    if not exact or len(exact) > MAX_EXACT or len(prefix) > MAX_CONTEXT or len(suffix) > MAX_CONTEXT:
        raise AnchorError("oversized_anchor" if len(exact) > MAX_EXACT or len(prefix) > MAX_CONTEXT or len(suffix) > MAX_CONTEXT else "malformed_anchor")
    heading, block, position = anchor["heading"], anchor["block"], anchor["position"]
    if public and (not _closed(heading, _HEADING_KEYS) or not _closed(block, _BLOCK_KEYS) or not _closed(position, _POSITION_KEYS)):
        raise AnchorError("malformed_anchor")
    if not isinstance(heading, Mapping) or not isinstance(block, Mapping) or not isinstance(position, Mapping):
        raise AnchorError("malformed_anchor")
    if not _HEADING_KEYS.issubset(heading) or not _BLOCK_KEYS.issubset(block) or not _POSITION_KEYS.issubset(position):
        raise AnchorError("malformed_anchor")
    path = heading["path"]
    if not isinstance(path, list) or len(path) > 16 or any(not isinstance(x, str) or not x or len(x) > 512 for x in path) or sum(map(len, path)) > 2048:
        raise AnchorError("oversized_anchor" if isinstance(path, list) and (len(path) > 16 or sum(len(x) for x in path if isinstance(x, str)) > 2048) else "malformed_anchor")
    block_id, block_hash_val = block["id"], block["hash"]
    if not isinstance(block_id, str) or not _BLOCK_ID.fullmatch(block_id) or not isinstance(block_hash_val, str) or not _BLOCK_HASH.fullmatch(block_hash_val):
        raise AnchorError("malformed_anchor")
    values = (block["start"], block["end"], position["start"], position["end"])
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in values):
        raise AnchorError("invalid_offsets")
    bs, be, ps, pe = values
    if bs >= be or ps >= pe or ps < bs or pe > be or pe - ps != len(exact):
        raise AnchorError("invalid_offsets")
    return anchor


def capture_anchor(source: str, start: int, end: int) -> dict[str, Any]:
    """Capture one exact canonical source span, failing closed outside one block."""
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool) or start < 0 or start >= end or end > len(source):
        raise AnchorError("source_mapping_failed")
    containing = [block for block in extract_blocks(source) if block.start <= start and end <= block.end]
    if len(containing) != 1:
        raise AnchorError("source_mapping_failed")
    block = containing[0]
    return {
        "schema": 2, "kind": "text-quote", "exact": source[start:end],
        "prefix": source[max(block.start, start - MAX_CONTEXT):start],
        "suffix": source[end:min(block.end, end + MAX_CONTEXT)],
        "heading": {"path": list(block.heading_path)}, "block": block.public(),
        "position": {"start": start, "end": end},
    }


def _occurrences(source: str, exact: str) -> Iterable[int]:
    for index in range(0, len(source) - len(exact) + 1):
        if source.startswith(exact, index):
            yield index


def _result(status: str, reason: str, source: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "concept_content_rev": concept_content_rev(source), "resolver_version": 2, "changes": extra.pop("changes", []), **extra}


def _intersect_supports(candidates: Iterable[_Candidate], supports: Mapping[str, set[_Candidate]], order: Iterable[str] | None = None) -> tuple[set[_Candidate], int]:
    """Intersect all non-empty evidence supports; ``order`` exists for parity proof."""
    names = tuple(order) if order is not None else tuple(supports)
    nonempty = [supports[name] for name in names if supports[name]]
    return (set(candidates) if not nonempty else set.intersection(*nonempty), len(nonempty))


def resolve_anchor(anchor: Any, source: str, *, public: bool = False) -> dict[str, Any]:
    """Resolve v2 or legacy stored anchors without throwing at the public boundary."""
    if not isinstance(source, str):
        return _result("invalid", "source_mapping_failed", "")
    if isinstance(anchor, Mapping) and anchor.get("kind") == "text" and "schema" not in anchor:
        if public and set(anchor) != {"kind", "ref"}:
            return _result("invalid", "malformed_anchor", source)
        ref = anchor.get("ref")
        if not isinstance(ref, str) or not ref:
            return _result("invalid", "malformed_anchor", source)
        if public and len(ref) > MAX_EXACT:
            return _result("invalid", "oversized_anchor", source)
        # Legacy resolution is unique-only.  Once a second occurrence exists,
        # neither its position nor a complete count can affect the result.
        starts = []
        for start in _occurrences(source, ref):
            starts.append(start)
            if len(starts) == 2:
                break
        if not starts:
            return _result("stale", "exact_not_found", source, candidate_count=0)
        if len(starts) > 1:
            return _result("ambiguous", "multiple_exact_candidates", source, candidate_count=len(starts))
        start, end = starts[0], starts[0] + len(ref)
        block = next((item for item in extract_blocks(source)
                      if item.start <= start and end <= item.end), None)
        if block is None:
            return _result("stale", "no_compatible_candidate", source, candidate_count=1)
        return _result("resolved", "unique_global_exact", source,
                       position={"start": start, "end": end}, block=block.public())
    try:
        parsed = parse_v2_anchor(anchor, public=public)
    except (AnchorError, TypeError, ValueError) as exc:
        return _result("invalid", exc.reason if isinstance(exc, AnchorError) else "malformed_anchor", source)
    occurrence_starts = list(_occurrences(source, parsed["exact"]))
    if not occurrence_starts:
        return _result("stale", "exact_not_found", source, candidate_count=0)
    blocks = extract_blocks(source)
    candidates = [_Candidate(pos, pos + len(parsed["exact"]), block) for pos in occurrence_starts for block in blocks if block.start <= pos and pos + len(parsed["exact"]) <= block.end]
    if not candidates:
        return _result("stale", "no_compatible_candidate", source, candidate_count=len(occurrence_starts))
    pos = parsed["position"]; old_block = parsed["block"]; path = tuple(parsed["heading"]["path"])
    supports = {
        "position": {c for c in candidates if (c.start, c.end) == (pos["start"], pos["end"])},
        "block": {c for c in candidates if (c.block.start, c.block.end, c.block.id) == (old_block["start"], old_block["end"], old_block["id"])},
        "hash": {c for c in candidates if c.block.hash == old_block["hash"]},
        "heading": {c for c in candidates if c.block.heading_path == path},
        "prefix": {c for c in candidates if (c.start == c.block.start if not parsed["prefix"] else source[max(c.block.start, c.start - len(parsed["prefix"])):c.start] == parsed["prefix"])},
        "suffix": {c for c in candidates if (c.end == c.block.end if not parsed["suffix"] else source[c.end:min(c.block.end, c.end + len(parsed["suffix"]))] == parsed["suffix"])},
    }
    effective, nonempty_count = _intersect_supports(candidates, supports)
    if not effective:
        return _result("invalid", "conflicting_evidence", source)
    if len(effective) > 1:
        reason = "insufficient_context" if not nonempty_count else "multiple_exact_candidates"
        return _result("ambiguous", reason, source, candidate_count=len(effective))
    selected = next(iter(effective))
    changes = []
    if (selected.start, selected.end) != (pos["start"], pos["end"]): changes.append("position_moved")
    if (selected.block.start, selected.block.end, selected.block.id) != (old_block["start"], old_block["end"], old_block["id"]): changes.append("block_moved")
    if selected.block.hash != old_block["hash"]: changes.append("block_changed")
    if selected.block.heading_path != path: changes.append("heading_changed")
    if selected not in supports["prefix"]: changes.append("prefix_changed")
    if selected not in supports["suffix"]: changes.append("suffix_changed")
    original = not changes and selected in supports["position"]
    reason = "original_position" if original else "unique_context" if nonempty_count else "unique_global_exact"
    return _result("resolved", reason, source, changes=changes, position={"start": selected.start, "end": selected.end}, block=selected.block.public())
