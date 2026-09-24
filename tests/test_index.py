"""Tests for ``okf_loom.index``.

Pinned invariants:
  * ``regenerate_indexes`` creates a ``generated: true`` (root) /
    marker-wrapped (non-root) ``index.md`` for directories lacking one.
  * It SKIPS hand-authored ``index.md`` (no marker, no ``generated: true``).
  * It respects the ``<!-- okf:generated:index begin/end -->`` markers and
    rewrites ONLY inside that region.
  * ``frozen=True`` raises ``OKFIOError`` on hand-authored indexes.
  * ``plan_index_regeneration`` matches actual behaviour.
  * Idempotent: a second run writes nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from okf_loom import Bundle, OKFIOError
from okf_loom.index import (
    _BEGIN,
    _END,
    plan_index_regeneration,
    regenerate_indexes,
)


# --- create when absent -----------------------------------------------------


def test_regenerate_creates_root_index_with_generated_marker(
    tmp_path: Path,
) -> None:
    """A bundle with no root index.md gets one with ``generated: true``."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    written = regenerate_indexes(b)
    assert (tmp_path / "index.md") in [Path(p) for p in written]
    content = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "generated: true" in content


def test_regenerate_creates_non_root_index_with_markers(tmp_path: Path) -> None:
    """A non-root directory gets a marker-wrapped index.md (no frontmatter)."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text(
        "---\ntype: T\ntitle: AAA\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    written = regenerate_indexes(b)
    sub_index = sub / "index.md"
    assert sub_index in [Path(p) for p in written]
    content = sub_index.read_text(encoding="utf-8")
    assert _BEGIN in content
    assert _END in content
    # No frontmatter for non-root (SPEC §6 reserves it for the root).
    assert not content.startswith("---")


def test_regenerate_skips_hand_authored_index(tmp_path: Path) -> None:
    """Hand-authored index.md (no markers, no generated: true) is skipped."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    hand = "# Hand-authored\n\nNo markers here.\n"
    (sub / "index.md").write_text(hand, encoding="utf-8")
    b = Bundle.load(tmp_path)
    written = regenerate_indexes(b)
    assert (sub / "index.md") not in [Path(p) for p in written]
    # Content is byte-identical.
    assert (sub / "index.md").read_text(encoding="utf-8") == hand


# --- marker region: rewrite inside, preserve outside -----------------------


def test_regenerate_rewrites_inside_markers_only(tmp_path: Path) -> None:
    """Only the marker region is rewritten; prose outside is preserved."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text(
        "---\ntype: T\ntitle: Fresh\n---\nbody\n", encoding="utf-8"
    )
    hand_intro = "# Subdirectory\n\nCurator-written intro kept verbatim.\n\n"
    marker_block = f"{_BEGIN}\n(stale)\n{_END}\n"
    hand_outro = "\nMore curator prose after the marker.\n"
    (sub / "index.md").write_text(hand_intro + marker_block + hand_outro, encoding="utf-8")
    b = Bundle.load(tmp_path)
    written = regenerate_indexes(b)
    assert (sub / "index.md") in [Path(p) for p in written]
    content = (sub / "index.md").read_text(encoding="utf-8")
    assert "Curator-written intro kept verbatim." in content
    assert "More curator prose after the marker." in content
    # Stale content is gone; the new entry title appears inside the markers.
    assert "(stale)" not in content
    assert "Fresh" in content


def test_generated_marker_only_no_markers_regenerates_body(
    tmp_path: Path,
) -> None:
    """A non-root index.md with NO markers but somehow flagged generated
    (only possible at root in practice) regenerates its whole body."""
    # This path is exercised via the root index with generated: true.
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A1\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: B1\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "index.md").write_text(
        "---\ngenerated: true\n---\n(stale body)\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    written = regenerate_indexes(b)
    assert (tmp_path / "index.md") in [Path(p) for p in written]
    content = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "(stale body)" not in content
    assert "A1" in content
    assert "B1" in content


# --- frozen=True raises -----------------------------------------------------


def test_frozen_raises_on_hand_authored(tmp_path: Path) -> None:
    """``frozen=True`` raises ``OKFIOError`` on a hand-authored index.md."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    (sub / "index.md").write_text("# Hand-authored\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    with pytest.raises(OKFIOError, match="hand-authored"):
        regenerate_indexes(b, frozen=True)


def test_frozen_does_not_raise_when_no_hand_authored(tmp_path: Path) -> None:
    """``frozen=True`` is fine when there's nothing hand-authored to skip."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    # No index.md present; regeneration creates one. Frozen mode permits this.
    written = regenerate_indexes(b, frozen=True)
    assert written  # something got written


def test_frozen_allows_marker_managed_files(tmp_path: Path) -> None:
    """``frozen=True`` does NOT raise for marker-managed index.md (tool-owned)."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    (sub / "index.md").write_text(
        f"# Sub\n\n{_BEGIN}\n(stale)\n{_END}\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    written = regenerate_indexes(b, frozen=True)  # must not raise
    assert (sub / "index.md") in [Path(p) for p in written]


# --- idempotency ------------------------------------------------------------


def test_regenerate_idempotent(tmp_path: Path) -> None:
    """A second regenerate run on a freshly-regenerated bundle writes nothing."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    first = regenerate_indexes(b)
    assert first  # something was written
    # Reload and run again.
    b2 = Bundle.load(tmp_path)
    second = regenerate_indexes(b2)
    assert second == []


# --- plan matches behaviour -------------------------------------------------


def test_plan_matches_actual_behaviour(tmp_path: Path) -> None:
    """``plan_index_regeneration`` predicts what ``regenerate_indexes`` writes."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    hand_dir = tmp_path / "hand"
    hand_dir.mkdir()
    (hand_dir / "h.md").write_text(
        "---\ntype: T\ntitle: H\n---\nbody\n", encoding="utf-8"
    )
    (hand_dir / "index.md").write_text("# Hand\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    plan = plan_index_regeneration(b)
    # Run actual regeneration.
    written = regenerate_indexes(b)
    written_rel = sorted(
        str(Path(p).relative_to(tmp_path)) for p in written
    )
    assert sorted(plan["would_write"]) == written_rel
    # Hand-authored is listed under would_skip_hand_authored.
    assert "hand/index.md" in plan["would_skip_hand_authored"]


def test_plan_keys_present(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    plan = plan_index_regeneration(b)
    for key in ("would_write", "would_skip_hand_authored", "would_skip_unchanged"):
        assert key in plan
    assert isinstance(plan["would_write"], list)


# --- index content correctness ----------------------------------------------


def test_index_lists_concepts_grouped_by_type(tmp_path: Path) -> None:
    """Generated index lists concepts grouped under ``# <Type>`` headings."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text(
        "---\ntype: Table\ntitle: Alpha\n---\nbody\n", encoding="utf-8"
    )
    (sub / "b.md").write_text(
        "---\ntype: View\ntitle: Beta\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    regenerate_indexes(b)
    body = (sub / "index.md").read_text(encoding="utf-8")
    assert "# Table" in body
    assert "# View" in body
    assert "[Alpha](/sub/a.md)" in body
    assert "[Beta](/sub/b.md)" in body


def test_root_index_lists_subdirectories(tmp_path: Path) -> None:
    """The root index.md lists subdirectories that contain concepts."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    regenerate_indexes(b)
    body = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "# Subdirectories" in body
    assert "[sub](/sub/index.md)" in body


def test_no_concepts_no_write(tmp_path: Path) -> None:
    """An empty bundle produces no index writes."""
    b = Bundle.load(tmp_path)
    written = regenerate_indexes(b)
    assert written == []


# --- derived JSON artifacts (current spec §8) --------------------------------


def test_emit_derived_json_writes_both_files(tmp_path: Path) -> None:
    """``emit_derived_json`` writes content.json + graph.json under .okf-loom/index/."""
    from okf_loom.index import emit_derived_json

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    written = emit_derived_json(b)
    names = {Path(p).name for p in written}
    assert names == {"content.json", "graph.json"}
    assert (tmp_path / ".okf-loom" / "index" / "content.json").is_file()
    assert (tmp_path / ".okf-loom" / "index" / "graph.json").is_file()


def test_emit_derived_json_carries_marker(tmp_path: Path) -> None:
    """Both files carry the ``generated_by`` / ``format_version`` marker."""
    import json

    from okf_loom.index import emit_derived_json

    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    emit_derived_json(b)
    content = json.loads(
        (tmp_path / ".okf-loom" / "index" / "content.json").read_text(encoding="utf-8")
    )
    graph = json.loads(
        (tmp_path / ".okf-loom" / "index" / "graph.json").read_text(encoding="utf-8")
    )
    assert content["generated_by"] == "okf-loom"
    assert content["format_version"] == 1
    assert graph["generated_by"] == "okf-loom"
    assert graph["format_version"] == 1


def test_emit_derived_json_has_no_absolute_host_paths(tmp_path: Path) -> None:
    """Neither file leaks the absolute bundle root path (portability current spec §8)."""
    from okf_loom.index import emit_derived_json

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    emit_derived_json(b)
    abs_root = str(tmp_path.resolve())
    content_text = (tmp_path / ".okf-loom" / "index" / "content.json").read_text(encoding="utf-8")
    graph_text = (tmp_path / ".okf-loom" / "index" / "graph.json").read_text(encoding="utf-8")
    assert abs_root not in content_text
    assert abs_root not in graph_text
    # Spot-check: the path field is bundle-relative.
    import json
    content = json.loads(content_text)
    assert content["concepts"][0]["path"] == "sub/a.md"
    graph = json.loads(graph_text)
    assert graph["nodes"][0]["path"] == "sub/a.md"


def test_emit_derived_json_is_byte_stable_across_runs(tmp_path: Path) -> None:
    """Reloading the bundle and re-emitting produces byte-identical output."""
    from okf_loom.index import emit_derived_json

    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [b](b.md)\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: U\ntitle: B\ntags: [x, y]\n---\nbody\n", encoding="utf-8"
    )
    b1 = Bundle.load(tmp_path)
    emit_derived_json(b1)
    content1 = (tmp_path / ".okf-loom" / "index" / "content.json").read_bytes()
    graph1 = (tmp_path / ".okf-loom" / "index" / "graph.json").read_bytes()

    # Reload from disk and re-emit (simulates a fresh process).
    b2 = Bundle.load(tmp_path)
    emit_derived_json(b2)
    content2 = (tmp_path / ".okf-loom" / "index" / "content.json").read_bytes()
    graph2 = (tmp_path / ".okf-loom" / "index" / "graph.json").read_bytes()

    assert content1 == content2
    assert graph1 == graph2


def test_emit_derived_json_byte_stable_under_reversed_in_memory_dict(tmp_path: Path) -> None:
    """iter2 P2-2 regression: edges/external must be explicitly sorted (current spec §3/§8).

    The existing byte-stability test reloads from disk (Bundle.load uses sorted
    rglob), which MASKS the bug where edges/external are emitted in graph
    insertion order. This test constructs the SAME bundle but reverses the
    in-memory ``bundle.concepts`` dict (as an embedder using the library API
    directly would) and asserts the emitted graph.json is byte-identical.
    Catches the class of bug where a sort is missing on edges/external even
    though nodes are sorted.
    """
    from okf_loom.index import emit_derived_json, _build_portable_graph_json

    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [b](b.md) and [c](c.md)\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: U\ntitle: B\n---\nBack to [a](a.md)\n", encoding="utf-8"
    )
    (tmp_path / "c.md").write_text(
        "---\ntype: V\ntitle: C\n---\nbody\n", encoding="utf-8"
    )
    bundle = Bundle.load(tmp_path)
    g_normal = json.dumps(_build_portable_graph_json(bundle), sort_keys=True)

    # Reverse the in-memory concepts dict + invalidate caches so graph is
    # rebuilt from the reversed dict. An embedder using the library API
    # directly (not Bundle.load) could hit this insertion order.
    reversed_concepts = dict(reversed(list(bundle.concepts.items())))
    bundle._concepts = reversed_concepts
    bundle.invalidate()
    g_reversed = json.dumps(_build_portable_graph_json(bundle), sort_keys=True)

    assert g_normal == g_reversed, (
        "graph.json byte order changed when bundle.concepts insertion order "
        "was reversed — edges/external are not explicitly sorted (§3.2)"
    )


def test_emit_derived_json_graph_shape_mirrors_cmd_graph(tmp_path: Path) -> None:
    """graph.json carries the same shape as ``okf graph --format json`` output."""
    import json

    from okf_loom.index import emit_derived_json

    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [b](b.md)\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: B\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    emit_derived_json(b)
    graph = json.loads(
        (tmp_path / ".okf-loom" / "index" / "graph.json").read_text(encoding="utf-8")
    )
    # Same top-level keys as cmd_graph (plus the marker keys).
    for key in ("nodes", "edges", "external"):
        assert key in graph
    # Edge shape mirrors cmd_graph: source/target/target_raw/form/label.
    edge = graph["edges"][0]
    for k in ("source", "target", "target_raw", "form", "label"):
        assert k in edge
    # The a → b internal edge is present.
    pairs = {(e["source"], e["target"]) for e in graph["edges"]}
    assert ("a", "b") in pairs


def test_portable_graph_json_has_no_dangling_targets(tmp_path: Path) -> None:
    """Portable graph.json never asserts a ``target`` that is not a current node.

    Parity with the visual ``build_graph_data`` invariant: a link whose target
    resolves to a ConceptId that is not a loaded concept (reserved index/log
    files, tolerated broken/missing links) must serialise ``target`` as None
    rather than a dangling node id, so external tools building a graph from
    ``.okf-loom/index/graph.json`` never see edges to non-existent nodes. The
    edge record is retained (``target_raw``/``form``/``label``) as a
    diagnostic. Valid reserved-document links stay discoverable through that
    diagnostic record while the model classifies them in ``Graph.navigation``
    — never in concept edges or unresolved counts (current spec); only a
    missing document or missing rendered heading fragment remains unresolved.
    """
    from okf_loom.index import _build_portable_graph_json
    from okf_loom.paths import concept_id_to_str

    (tmp_path / "govern").mkdir()
    # Reserved non-root index file → bundle.indexes, NOT bundle.concepts.
    (tmp_path / "govern" / "index.md").write_text(
        "# Governance\n\nQuadrant index.\n", encoding="utf-8"
    )
    (tmp_path / "govern" / "policy.md").write_text(
        "---\ntype: Policy\ntitle: Policy\n---\n"
        "See the [index](index.md) and a [peer](charter.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "govern" / "charter.md").write_text(
        "---\ntype: Policy\ntitle: Charter\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    graph = _build_portable_graph_json(b)
    node_ids = {n["id"] for n in graph["nodes"]}
    assert "govern/index" not in node_ids

    # No edge asserts a non-None target that is not a current node.
    dangling = [
        e for e in graph["edges"]
        if e.get("target") is not None and e["target"] not in node_ids
    ]
    assert dangling == [], (
        "portable graph.json emitted dangling target(s): "
        + ", ".join(repr(e["target"]) for e in dangling)
    )

    # The reserved-index link is retained as a diagnostic record with a null
    # target (target_raw still shows where it pointed).
    index_edges = [
        e for e in graph["edges"]
        if e["source"] == "govern/policy" and "index" in e["target_raw"]
    ]
    assert index_edges, "reserved-index edge record was dropped entirely"
    assert all(e["target"] is None for e in index_edges), (
        "reserved-index edge should have null target, got non-node reference"
    )

    # The valid current-concept edge keeps its concrete target.
    valid = [
        e for e in graph["edges"]
        if e["source"] == "govern/policy" and e.get("target") == "govern/charter"
    ]
    assert valid, "valid current-concept edge lost its target"

    # Model classification stays exact: the valid reserved-index link is
    # navigation, not an unresolved count.
    model = b.graph()
    navigation_targets = {
        concept_id_to_str(link.target)
        for link in model.navigation if link.target is not None
    }
    unresolved_targets = {
        concept_id_to_str(link.target)
        for link in model.unresolved if link.target is not None
    }
    assert "govern/index" in navigation_targets
    assert "govern/index" not in unresolved_targets


def test_emit_derived_json_includes_by_type_and_by_tag(tmp_path: Path) -> None:
    """content.json exposes ContentIndex-derived by_type / by_tag lookups."""
    import json

    from okf_loom.index import emit_derived_json

    (tmp_path / "a.md").write_text(
        "---\ntype: Table\ntitle: A\ntags: [pii]\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: Table\ntitle: B\ntags: [pii, public]\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    emit_derived_json(b)
    content = json.loads(
        (tmp_path / ".okf-loom" / "index" / "content.json").read_text(encoding="utf-8")
    )
    assert content["by_type"]["Table"] == ["a", "b"]
    # Tag index entries are sorted (deterministic).
    assert content["by_tag"]["pii"] == ["a", "b"]
    assert content["by_tag"]["public"] == ["b"]


def test_emit_derived_json_no_tmp_files_left(tmp_path: Path) -> None:
    """Atomic writes leave no ``.okf-*`` tmp files behind."""
    from okf_loom.index import emit_derived_json

    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    emit_derived_json(b)
    index_dir = tmp_path / ".okf-loom" / "index"
    leftover = [p.name for p in index_dir.iterdir() if p.name.startswith(".okf-")]
    assert leftover == []


def test_cli_index_emit_json_flag(tmp_path: Path) -> None:
    """``okf index <bundle> --emit-json`` writes both derived JSON files."""
    import io
    from contextlib import redirect_stdout

    from okf_loom.cli import main

    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["index", str(tmp_path), "--emit-json"])
    assert rc == 0
    assert (tmp_path / ".okf-loom" / "index" / "content.json").is_file()
    assert (tmp_path / ".okf-loom" / "index" / "graph.json").is_file()
    out = buf.getvalue()
    assert "content.json" in out
    assert "graph.json" in out


def test_cli_index_without_emit_json_writes_no_derived(tmp_path: Path) -> None:
    """Plain ``okf index`` does NOT write the derived JSON artifacts."""
    import io
    from contextlib import redirect_stdout

    from okf_loom.cli import main

    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["index", str(tmp_path)])
    assert rc == 0
    assert not (tmp_path / ".okf-loom" / "index" / "content.json").exists()
    assert not (tmp_path / ".okf-loom" / "index" / "graph.json").exists()
