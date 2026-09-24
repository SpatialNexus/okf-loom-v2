"""SPEC §6 ``index.md`` regeneration.

CRITICAL contract (research.md §C.5 "Auto-update without destroying
hand-curated content"):

    "synthesize by default, defer to hand-authored, touch only marked regions."

A directory's ``index.md`` is one of:

    1. **absent**        -> create it, tool-managed (generated marker).
    2. **hand-authored** -> NEVER overwrite. ``frozen=True`` raises
                            ``OKFIOError``; otherwise it is skipped silently.
    3. **tool-managed**  -> regenerate. If the body carries
                            ``<!-- okf:generated:index begin --> ... end -->``
                            markers, ONLY that region is rewritten; anything
                            outside the markers is preserved verbatim. If the
                            file instead signals management via a
                            ``generated: true`` frontmatter key (and no
                            markers), the whole body is regenerated while the
                            frontmatter is preserved.

SPEC §6 reserves frontmatter for the bundle-ROOT ``index.md`` only. We
therefore use comment markers (not frontmatter) for non-root generated indexes
so that we stay SPEC-conformant.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import GENERATED_MARKER_KEY, OKF_VERSION_KEY
from .exceptions import OKFIOError
from .io_utils import atomic_write_text
from .model import Bundle
from .parse import serialize_document
from .studio import rev_of
from .paths import ConceptIdError, concept_id_from_path, concept_id_to_str

_INDEX_FILE = "index.md"
_BEGIN = "<!-- okf:generated:index begin -->"
_END = "<!-- okf:generated:index end -->"
_MARKER_REGION_RE = re.compile(
    re.escape(_BEGIN) + r".*?" + re.escape(_END), re.DOTALL
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def regenerate_indexes(bundle: Bundle, *, frozen: bool = False,
                       studio: Any = None,
                       group_id: str | None = None,
                       actor: str = "agent",
                       origin: str = "auto-repair") -> list[Path]:
    """Regenerate tool-managed ``index.md`` files; never touch hand-authored.

    Args:
        bundle: a loaded ``Bundle``.
        frozen: CI mode. When True, encountering a hand-authored index.md that
            regeneration *would* need to touch raises ``OKFIOError`` instead of
            silently skipping. (Hand-authored files are skipped anyway; this
            only matters if a caller expects a directory to be writable.)
        studio: optional :class:`okf_loom.studio.Studio` for an active
            session. When set, each index write is routed through
            ``studio.save_concept(action="refresh_index", origin=origin,
            undoable=False, group_id=group_id)`` so the mechanical regen
            appears in the change list with attribution, emits a ``changed``
            SSE event for live patching, and shares one group-undo per repair
            pass (AGENTS.md hard rule #7 / §11 / P2-2 / ARCH2-003). When None
            (no session, or callers that don't want attribution), writes go
            through the compatibility ``atomic_write_text`` path with no attribution.
        group_id: optional group id stamped on every studio-routed write so
            the whole repair pass reverts in one click (§12.5). Ignored when
            ``studio`` is None.
        actor: activity actor for studio-routed writes (default ``agent``).
            Ignored when ``studio`` is None.
        origin: activity origin for studio-routed writes (default
            ``auto-repair``). Ignored when ``studio`` is None.

    Returns:
        The list of absolute paths written.

    Two-pass implementation (fail-closed in frozen mode): we plan EVERY
    directory first, raising on any hand-authored file in frozen mode
    BEFORE writing anything to disk. Only then do we apply the planned
    writes.

    Reserved-filename note (AGENTS.md hard rule #5): ``index.md`` is a
    reserved filename — but the concept_id we hand to ``save_concept`` is
    the *path-derived* id (``"tables/index"`` or ``"index"``), not the
    filename, and the file we are writing IS the directory index (not a
    concept document). Routing through ``save_concept`` is what gets the
    refresh attributed + broadcast; the file itself is still the SPEC §6
    directory index. We pass ``path=index_path`` explicitly so the funnel
    writes the canonical index path regardless of concept_id path
    derivation.
    """
    with bundle.mutation() as transaction:
        # Pass 1: plan everything against one retained snapshot; raise on
        # frozen conflicts before any write.
        planned: list[tuple[Path, str]] = []
        expected: dict[Path, bytes | None] = {}
        write_results: dict[Path, Any] = {}
        for directory in _directories_with_concepts(bundle):
            action, content, index_path = _plan_one(bundle, directory, frozen=frozen)
            if action == "write" and content is not None:
                planned.append((index_path, content))
                retained = bundle.indexes.get(index_path.relative_to(bundle.root))
                expected[index_path] = (
                    retained.raw_text.encode("utf-8") if retained is not None else None
                )

        def persist(index_path: Path, content: str) -> None:
            if studio is not None:
                rel_no_suffix = index_path.relative_to(bundle.root).with_suffix("")
                concept_id = str(rel_no_suffix)
                result = studio.save_concept(
                    concept_id=concept_id, raw=content,
                    actor=actor, action="refresh_index", origin=origin,
                    group_id=group_id, undoable=False,
                    summary=f"refresh_index on {concept_id}",
                    path=index_path,
                    expected_rev=(rev_of(expected[index_path])
                                  if expected[index_path] is not None else ""),
                    publish=False,
                    defer_events=True,
                )
                if not result.ok:
                    raise OKFIOError(
                        result.error or f"index write rejected for {concept_id}"
                    )
                write_results[index_path] = result
                return
            try:
                current = index_path.read_bytes()
            except OSError:
                current = None
            if current != expected[index_path]:
                raise OKFIOError(f"index write conflict: {index_path}")
            atomic_write_text(index_path, content)

        staged_events: list[dict[str, Any]] = []

        def finalize_studio() -> None:
            if studio is None:
                return
            from .io_utils import atomic_write_bytes
            events_before = studio.events_path.read_bytes() if studio.events_path.is_file() else None
            logged_before = studio.last_logged_path.read_bytes() if studio.last_logged_path.is_file() else None
            logged_map_before = dict(studio._last_logged_rev)
            history_before = {
                path: path.read_bytes()
                for path in studio.history_dir.rglob("*") if path.is_file()
            }
            try:
              if group_id:
                studio.snapshot_many_for_undo(
                    [
                        (
                            str(path.relative_to(bundle.root).with_suffix("")),
                            write_results[path].prior_bytes
                            if write_results[path].prior_bytes is not None else b"",
                        )
                        for path, _content in planned
                    ],
                    group_id=group_id,
                )
              for index_path, content in planned:
                rel_no_suffix = index_path.relative_to(bundle.root).with_suffix("")
                concept_id = str(rel_no_suffix)
                studio.mark_logged(concept_id, content)
                studio.record_activity(
                    actor=actor, action="refresh_index", ids=[concept_id],
                    summary=f"refresh_index on {concept_id}", origin=origin,
                    undoable=False, group_id=group_id, publish=False,
                    collector=staged_events,
                )
                staged_events.append(studio.emit_change(
                    kind="changed", ids=[concept_id], origin=origin, publish=False
                ))
            except BaseException:
                for path in sorted(
                    (p for p in studio.history_dir.rglob("*") if p.is_file()),
                    key=lambda p: len(p.parts), reverse=True,
                ):
                    if path not in history_before:
                        path.unlink(missing_ok=True)
                for path, raw in history_before.items():
                    atomic_write_bytes(path, raw)
                if events_before is None:
                    studio.events_path.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(studio.events_path, events_before)
                if logged_before is None:
                    studio.last_logged_path.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(studio.last_logged_path, logged_before)
                studio._last_logged_rev = logged_map_before
                raise
            from .studio import _event_dedup_key
            for event in staged_events:
                studio._remember_published(_event_dedup_key(event))

        def commit() -> None:
            if studio is not None:
                concepts = [
                    str(path.relative_to(bundle.root).with_suffix(""))
                    for path, _content in planned
                ]
                error = studio.preflight_group_members(group_id, concepts)
                if error:
                    raise OKFIOError(error)
            transaction.commit_indexes(planned, persist, finalize=finalize_studio)
        if studio is not None:
            studio.run_group_transaction(
                group_id, commit, content_paths=[path for path, _raw in planned]
            )
        else:
            commit()
        if studio is not None:
            for event in staged_events:
                studio.bus.publish(event)
        return [path for path, _content in planned]


def plan_index_regeneration(bundle: Bundle) -> dict:
    """Describe what ``regenerate_indexes`` would do, WITHOUT writing.

    Returns a dict with keys:
        - ``would_write``: rel paths that would be created or regenerated.
        - ``would_skip_hand_authored``: rel paths protected from overwrite.
        - ``would_skip_unchanged``: tool-managed rel paths already current.
    """
    would_write: list[str] = []
    would_skip_hand_authored: list[str] = []
    would_skip_unchanged: list[str] = []
    for directory in _directories_with_concepts(bundle):
        action, _content, index_path = _plan_one(bundle, directory, frozen=False)
        rel = str(index_path.relative_to(bundle.root))
        if action == "write":
            would_write.append(rel)
        elif action == "skip_hand":
            would_skip_hand_authored.append(rel)
        elif action == "skip_unchanged":
            would_skip_unchanged.append(rel)
    return {
        "would_write": would_write,
        "would_skip_hand_authored": would_skip_hand_authored,
        "would_skip_unchanged": would_skip_unchanged,
    }


# ---------------------------------------------------------------------------
# Per-directory planning (shared by regenerate + plan)
# ---------------------------------------------------------------------------


def _plan_one(
    bundle: Bundle, directory: Path, *, frozen: bool
) -> tuple[str, str | None, Path]:
    """Decide what to do for one directory's index.md.

    Returns ``(action, content_or_None, index_path)`` where action is one of
    ``write`` | ``skip_hand`` | ``skip_unchanged`` | ``skip_empty``.
    """
    rel_dir = directory.relative_to(bundle.root)
    index_rel = rel_dir / _INDEX_FILE
    index_path = bundle.root / index_rel
    entries = _dir_entries(bundle, directory)
    if not entries:
        return "skip_empty", None, index_path
    new_body = _build_index_text(entries)

    existing = bundle.indexes.get(index_rel)
    is_root = rel_dir == Path(".")

    if existing is None:
        # Case 1: absent -> create tool-managed.
        return "write", _compose_new(is_root, bundle, new_body), index_path

    has_markers = bool(_MARKER_REGION_RE.search(existing.body))
    is_gen_fm = existing.frontmatter.get(GENERATED_MARKER_KEY) is True

    if not has_markers and not is_gen_fm:
        # Case 2: hand-authored -> protected.
        if frozen:
            raise OKFIOError(
                f"Refusing to overwrite hand-authored index.md: {index_path}"
            )
        return "skip_hand", None, index_path

    # Case 3: tool-managed.
    if has_markers:
        new_body_region = _replace_marker_region(existing.body, new_body)
        content = serialize_document(existing.frontmatter, new_body_region)
    else:
        # generated:true frontmatter, no markers -> regenerate whole body,
        # preserving frontmatter.
        content = serialize_document(existing.frontmatter, new_body)

    if content == existing.raw_text:
        return "skip_unchanged", None, index_path
    return "write", content, index_path


def _compose_new(is_root: bool, bundle: Bundle, body: str) -> str:
    """Compose content for a freshly created index.md."""
    if is_root:
        # SPEC §11 allows root index.md frontmatter. Use the generated marker.
        fm: dict = {GENERATED_MARKER_KEY: True}
        if bundle.okf_version:
            # Keep version first (SPEC convention), then okf-loom marker.
            fm = {OKF_VERSION_KEY: bundle.okf_version, GENERATED_MARKER_KEY: True}
        return serialize_document(fm, body)
    # Non-root: SPEC §6 forbids frontmatter -> use comment markers only.
    marked = f"{_BEGIN}\n{body.rstrip()}\n{_END}\n"
    return serialize_document({}, marked)


def _replace_marker_region(body: str, new_inner: str) -> str:
    replacement = f"{_BEGIN}\n{new_inner.rstrip()}\n{_END}"
    return _MARKER_REGION_RE.sub(replacement, body, count=1)


# ---------------------------------------------------------------------------
# Directory + entry computation (mirrors upstream _build_index_text)
# ---------------------------------------------------------------------------


def _directories_with_concepts(bundle: Bundle) -> list[Path]:
    """All ancestor directories of any concept (root-inclusive), shallow-first."""
    root = bundle.root
    dirs: set[Path] = set()
    for c in bundle.concepts.values():
        cur = c.path.parent
        while True:
            dirs.add(cur)
            if cur == root:
                break
            if cur.parent == cur:  # filesystem root guard
                break
            cur = cur.parent
    return sorted(dirs, key=lambda p: str(p.relative_to(root)))


def _dir_has_concepts_recursive(dirs_with: set[Path], d: Path) -> bool:
    return d in dirs_with


def _dir_entries(bundle: Bundle, directory: Path) -> list[dict]:
    """Build the entry list for a directory's index.

    Each entry is ``{type, title, link, description}``. Child concept files
    appear under their real ``type``; subdirectories (that recursively contain
    concepts) appear under the synthetic ``Subdirectories`` type, mirroring the
    upstream reference agent.
    """
    dirs_with = _directories_with_concepts(bundle)
    dirs_set = set(dirs_with)
    entries: list[dict] = []
    try:
        children = sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return entries
    for child in children:
        if child.name == _INDEX_FILE:
            continue
        if child.is_file() and child.suffix == ".md":
            try:
                cid = concept_id_from_path(bundle.root, child)
            except (ConceptIdError, ValueError):
                continue
            concept = bundle.concepts.get(cid)
            if concept is None:
                continue
            entries.append(
                {
                    "type": concept.type or "Other",
                    "title": concept.title or child.stem,
                    "link": "/" + child.relative_to(bundle.root).as_posix(),
                    "description": concept.description or "",
                }
            )
        elif child.is_dir():
            if not _dir_has_concepts_recursive(dirs_set, child):
                continue
            entries.append(
                {
                    "type": "Subdirectories",
                    "title": child.name,
                    "link": "/" + (child.relative_to(bundle.root) / _INDEX_FILE).as_posix(),
                    "description": _subdir_description(bundle, child),
                }
            )
    return entries


def _subdir_description(bundle: Bundle, subdir: Path) -> str:
    """Synthesize a description for a subdirectory from its index.md.

    Uses the description of the first listed entry in the subdirectory's
    index.md, or "" if none.
    """
    rel = subdir.relative_to(bundle.root) / _INDEX_FILE
    idx = bundle.indexes.get(rel)
    if idx is None:
        return ""
    for (_label, _target, desc) in idx.entries():
        if desc:
            return desc
    return ""


def _build_index_text(entries: list[dict]) -> str:
    """Render entries grouped by type, sections sorted, entries alphabetical.

    Mirrors ``workspace/upstream/okf/src/reference_agent/bundle/index.py``::
        ``_build_index_text``.
    """
    grouped: dict[str, list[dict]] = {}
    for e in entries:
        grouped.setdefault(e["type"] or "Other", []).append(e)
    sections: list[str] = []
    for typ in sorted(grouped):
        lines = [f"# {typ}", ""]
        for e in sorted(grouped[typ], key=lambda x: x["title"].lower()):
            suffix = f" - {e['description']}" if e["description"] else ""
            lines.append(f"* [{e['title']}]({e['link']}){suffix}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------
#
# Atomic writes are provided by ``okf_loom.io_utils.atomic_write_text``.
# The old module-private ``_atomic_write`` helper has been removed in
# favour of the shared, mkstemp-based implementation that is safe under
# concurrent writers and cleans up tmp files on exception.


# ---------------------------------------------------------------------------
# Derived JSON artifacts (current spec §8)
# ---------------------------------------------------------------------------
#
# ``okf index --emit-json`` writes two portable JSON files under
# ``<bundle>/.okf-loom/index/`` for external tools and the §4.5/§4.6 providers:
#
#     content.json  — the ContentIndex serialised (concepts + by_type / by_tag)
#     graph.json    — the bundle.graph() serialised, same shape as cmd_graph
#
# Hard rules (current spec §8 acceptance):
#   * NO absolute host paths in either file (use bundle-relative paths).
#   * Both files carry ``"generated_by": "okf-loom", "format_version": 1``.
#   * Byte-stable across runs: reloading the bundle and re-emitting produces
#     identical bytes (deterministic JSON: sorted keys, sorted concept lists).
#   * Atomic writes via ``io_utils.atomic_write_text``.
#   * Safe to delete and regenerate.

# Subdirectory under ``<bundle>/.okf-loom/`` for derived index artifacts.
DERIVED_INDEX_SUBDIR: Path = Path(".okf-loom") / "index"

# Provenance marker merged into every emitted JSON document (current spec §8).
_DERIVED_MARKER: dict[str, Any] = {
    "generated_by": "okf-loom",
    "format_version": 1,
}


def _serialise_json(obj: dict[str, Any]) -> str:
    """Deterministic JSON text for an emitted artifact.

    Sorted keys + sorted concept lists + trailing newline so that re-emitting
    a bundle produces byte-identical output across runs and processes.
    """
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _build_portable_content_json(bundle: Bundle) -> dict[str, Any]:
    """Build the portable ``content.json`` dict (ContentIndex serialised).

    No absolute paths: the ``path`` field is the concept's bundle-relative
    path (``Concept.rel_path``). The corpus_text field of ContentIndex is
    deliberately omitted — it is large and is a search-backend concern, not
    a portable-metadata concern.
    """
    concepts_sorted = sorted(bundle.concepts.values(), key=lambda c: c.id)
    concepts = [
        {
            "id": concept_id_to_str(c.id),
            "type": c.type,
            "title": c.title,
            "description": c.description,
            "tags": list(c.tags),
            "resource": c.resource,
            "timestamp": c.timestamp,
            "path": str(c.rel_path),
        }
        for c in concepts_sorted
    ]
    by_type: dict[str, list[str]] = {}
    by_tag: dict[str, list[str]] = {}
    for c in concepts_sorted:
        cid = concept_id_to_str(c.id)
        by_type.setdefault(c.type or "<untyped>", []).append(cid)
        for t in c.tags:
            by_tag.setdefault(t, []).append(cid)
    # Sort derived index entries so re-emit is byte-stable regardless of
    # dict insertion order.
    by_type = {k: sorted(v) for k, v in sorted(by_type.items())}
    by_tag = {k: sorted(v) for k, v in sorted(by_tag.items())}
    return {
        **_DERIVED_MARKER,
        "name": bundle.name,
        "concepts": concepts,
        "by_type": by_type,
        "by_tag": by_tag,
    }


def _build_portable_graph_json(bundle: Bundle) -> dict[str, Any]:
    """Build the portable ``graph.json`` dict (mirrors cmd_graph's output).

    Same fields as :func:`okf_loom.cli.cmd_graph` JSON output so external
    tools can consume either source identically.
    """
    graph = bundle.graph()
    # ConceptIds that are loaded concepts (the node set). Reserved index/log
    # files live in bundle.indexes/bundle.logs, not bundle.concepts, so a
    # link that resolves to e.g. ("govern", "index") is NOT a current node.
    concept_ids = set(bundle.concepts.keys())
    nodes = [
        {
            "id": concept_id_to_str(c.id),
            "type": c.type,
            "title": c.title,
            "tags": list(c.tags),
            "path": str(c.rel_path),
        }
        for c in sorted(bundle.concepts.values(), key=lambda c: c.id)
    ]
    edges = [
        {
            "source": concept_id_to_str(e.source),
            # Dangling-endpoint invariant (parity with build_graph_data): a
            # serialised graph target must reference a current node or be
            # None. Reserved index/log navigation (Graph.navigation) is
            # serialised alongside concept edges as a target-null diagnostic
            # record (source/target_raw/form/label retained) so the portable
            # graph keeps reserved-document navigation discoverable without
            # ever asserting a false node reference; tolerated broken/missing
            # links null the same way. Valid navigation is never an unresolved
            # count (current spec: Graph.navigation is separate from concept
            # edges and unresolved counts).
            "target": (
                concept_id_to_str(e.target)
                if e.target and e.target in concept_ids
                else None
            ),
            "target_raw": e.target_raw,
            "form": e.form,
            "label": e.label,
        }
        # iter2 P2-2: sort edges explicitly (current spec §3/§8 determinism). Iter-1
        # sorted nodes but missed these sibling lists; Bundle.load's sorted
        # rglob masked the bug for disk-loaded bundles.
        for e in sorted(
            [*graph.edges, *graph.navigation],
            key=lambda e: (
                e.source,
                e.target if e.target is not None else (),
                e.target_raw,
            ),
        )
    ]
    external = [
        {
            "source": concept_id_to_str(e.source),
            "target_raw": e.target_raw,
            "label": e.label,
        }
        for e in sorted(graph.external, key=lambda e: (e.source, e.target_raw))
    ]
    return {
        **_DERIVED_MARKER,
        "nodes": nodes,
        "edges": edges,
        "external": external,
    }


def emit_derived_json(bundle: Bundle) -> list[Path]:
    """Write ``content.json`` + ``graph.json`` under ``<bundle>/.okf-loom/index/``.

    Returns the absolute paths written, in the order written. The output is
    byte-stable across runs (deterministic JSON, no host-absolute paths),
    and writes are atomic (tmp + ``os.replace`` via
    :func:`okf_loom.io_utils.atomic_write_text`).

    The output directory is safe to delete and regenerate; current spec §8
    recommends (but does not auto-write) gitignoring ``.okf-loom/index/``.
    """
    content_path = bundle.root / DERIVED_INDEX_SUBDIR / "content.json"
    graph_path = bundle.root / DERIVED_INDEX_SUBDIR / "graph.json"
    # atomic_write_text creates parent dirs (mkstemp in target dir → parents
    # must exist); make them once up front for both files.
    content_path.parent.mkdir(parents=True, exist_ok=True)
    # Derived caches must not get committed: self-ignore the cache dir.
    from .io_utils import ensure_self_ignored
    ensure_self_ignored(content_path.parent)
    atomic_write_text(content_path, _serialise_json(_build_portable_content_json(bundle)))
    atomic_write_text(graph_path, _serialise_json(_build_portable_graph_json(bundle)))
    return [content_path, graph_path]
