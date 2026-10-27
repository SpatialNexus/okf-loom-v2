"""Live graph transport cache; portable graph serialization is not involved.

Keep at most one final byte string per view and Bundle, including negative
size results. Identical reads coalesce per view, outside the source lock.
"""
from __future__ import annotations

from bisect import bisect_right
import hashlib
import threading

from . import render
from .metadata import parse_instant
from .paths import concept_id_to_str
from .retrieval import canonical_json_bytes
from .viewer import metadata as metadata_view

# The compact live view is shared by every reader and cached once per source
# generation. Full/tool JSON keeps its existing 4 MiB cap.
MAX_STRUCTURE_BYTES = 16 * 1024 * 1024


def _record_floor(keys):
    # Braces, separators, quoted ASCII keys, colon and a one-byte value.
    return 2 + sum(len(key) + 4 for key in keys) + max(0, len(keys) - 1)


_NODE_KEYS = (
    "id", "label", "type", "description", "resource", "tags", "color", "icon",
    "size", "degree", "unresolved_count", "timestamp", "body_chars", "aliases",
    "entities", "provenance", "citations", "relations", "metadata", "okf_metadata",
    "metadata_html", "generated_at", "graph_cluster", "source_system",
)
_RELATION_KEYS = (
    "id", "label", "type", "kind", "direction", "source", "target", "target_raw",
    "authored_identity", "origin", "source_id",
)


def graph_size_floor(bundle, *, structure=False):
    """Conservative wire lower bound, without rendering or JSON encoding.

    Count only mandatory fields and verbatim strings. Never infer a rendered
    body's size from Markdown: markup can shrink. Duplicate primary relations
    remain part of the full contract; structure inherits endpoints/direction
    and references its primary relation by index.
    """
    total = 0
    for concept in bundle.concepts.values():
        total += _record_floor(("id", "label", "type") if structure else _NODE_KEYS)
        total += sum(len(value) for value in (
            concept_id_to_str(concept.id), concept.title or concept_id_to_str(concept.id),
            concept.type or "concept", "" if structure else concept.description,
        ) if isinstance(value, str))
    ids = set(bundle.concepts)
    pairs = set()
    for link in bundle.graph().edges:
        if link.source not in ids or link.target not in ids:
            continue
        total += _record_floor(("id", "label", "type", "kind", "origin", "source_id") if structure else _RELATION_KEYS)
        if not structure:
            total += len(concept_id_to_str(link.source)) + len(concept_id_to_str(link.target))
        pair = (link.source, link.target)
        if pair not in pairs:
            pairs.add(pair)
            total += _record_floor(("id", "source", "target", "relations", "primary_relation_index") if structure
                                   else ("id", "source", "target", "relations", "multiplicity", "primary_relation"))
            if not structure:
                total += _record_floor(_RELATION_KEYS)
    return total


def _json_size_floor(value):
    """Lower bound before encoding; escaped/non-ASCII text only costs more."""
    if isinstance(value, str):
        return len(value) + 2
    if isinstance(value, dict):
        return 2 + max(0, len(value) - 1) + sum(
            len(key) + 3 + _json_size_floor(child) for key, child in value.items())
    if isinstance(value, list):
        return 2 + max(0, len(value) - 1) + sum(map(_json_size_floor, value))
    return 1


def graph_response(bundle, *, name, comment_summary, structure, limit):
    """Return (bytes, strong ETag), or None for an over-cap projection.

    The cache key has no request headers, identities or cache-buster strings.
    Expiry boundaries, rather than a time bucket, preserve exact stale_after
    transitions (including clock rollback). Only two bounded entries survive.
    """
    with bundle._derived_lock:
        locks = getattr(bundle, "_graph_response_locks", None)
        if locks is None:
            locks = bundle._graph_response_locks = (threading.Lock(), threading.Lock())
    with locks[int(structure)]:
        with bundle._derived_lock:
            generation = bundle._source_generation
            facts = getattr(bundle, "_graph_response_facts", None)
            if facts is None or facts[0] != generation:
                expiries = sorted(instant for concept in bundle.concepts.values()
                                  if (instant := parse_instant(concept.metadata.get("stale_after"))) is not None)
                facts = bundle._graph_response_facts = (generation, expiries,
                    (graph_size_floor(bundle), graph_size_floor(bundle, structure=True)))
            now = metadata_view.datetime.now(metadata_view.timezone.utc)
            palette = tuple(sorted(render.resolve_palette(bundle).items()))
            key = (generation, name, palette, limit,
                   None if comment_summary is None else comment_summary["summary_revision"],
                   bisect_right(facts[1], now))
            cache = getattr(bundle, "_graph_response_cache", {})
            entry = cache.get(structure)
            if entry is not None and entry[0] == key:
                return entry[1]

        result = None
        if facts[2][int(structure)] <= limit:
            # A structural refusal under the SAME limit also proves the full
            # view cannot fit (normal HTTP caps differ, and are part of key).
            slim = cache.get(True)
            if structure or slim is None or slim[0] != key or slim[1] is not None:
                data = render.build_graph_data(
                    bundle, name=name, comment_summary=comment_summary,
                    cache_bodies=True, include_bodies=not structure, metadata_now=now, structure=structure,
                )
                if structure:
                    del data["bodies"]
                    del data["documents"]
                if _json_size_floor(data) <= limit:
                    body = canonical_json_bytes(data)
                    if len(body) <= limit:
                        result = (body, '"' + hashlib.sha256(body).hexdigest() + '"')
        with bundle._derived_lock:
            # A retained reload may finish during rendering. Never publish its
            # predecessor over the new generation's derived cache.
            if bundle._source_generation == generation:
                cache = dict(getattr(bundle, "_graph_response_cache", {}))
                cache[structure] = (key, result)
                bundle._graph_response_cache = cache
        return result
