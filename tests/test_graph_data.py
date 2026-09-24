"""Browser-free proof that ``build_graph_data`` produces render-ready JSON.

The full render proof lives in ``tests/test_viewer_browser.py`` and is gated on
the optional browser-proof dependencies + a Chromium binary, so it is SKIPPED in the
default ``pytest`` run. That left a gap: a regression that dropped graph nodes,
left labels blank, or skipped colour assignment could ship green because the
only test that would catch it never ran without the extra.

This module closes that gap with a BROWSER-FREE data-layer proof that runs in
every suite. It asserts the invariants the Cytoscape view needs to render
anything meaningful:

  * exactly one node per concept (no drops, no duplicates);
  * every node carries a non-empty label and a colour; and
  * the set of node ids equals the set of concept ids.

These run on the always-shipped ``samples/demo_bundle`` so they never skip.
"""
from __future__ import annotations

from pathlib import Path

from okf_loom.model import Bundle
from okf_loom.paths import concept_id_to_str
from okf_loom.render import build_graph_data

DEMO = "samples/demo_bundle"
# Canonical docs bundle: its concepts link to reserved quadrant index files
# (explanation/index, how-to/index, reference/index, tutorials/index) which
# are parsed as ConceptIds but held in bundle.indexes, not bundle.concepts.
# This is the real-world fixture that triggers the dangling-endpoint defect.
DOCS = "docs-bundle"


def test_inline_code_link_labels_reach_graph_json_and_nojs(tmp_path: Path) -> None:
    import json
    from okf_loom.render import _render_graph_page

    (tmp_path / "source.md").write_text(
        "---\ntype: Reference\n---\n"
        "[`/__comment-reanchor` HTTP route](/target.md)\n"
        "[`<safe>&`](/target.md)\n"
        "`[fake](/ghost.md)`\n", encoding="utf-8",
    )
    (tmp_path / "target.md").write_text("---\ntype: Reference\n---\n", encoding="utf-8")
    bundle = Bundle.load(tmp_path)
    data = json.loads(json.dumps(build_graph_data(bundle)))
    assert len(data["edges"]) == 1
    labels = [relation["label"] for relation in data["edges"][0]["data"]["relations"]]
    assert labels == ["/__comment-reanchor HTTP route", "<safe>&"]
    page = _render_graph_page(bundle, mode="serve", name="Test", config={"cdn": False})
    assert "<li>/__comment-reanchor HTTP route — link: source → target</li>" in page
    assert "<li>&lt;safe&gt;&amp; — link: source → target</li>" in page


def test_graph_data_node_count_equals_concept_count() -> None:
    """``build_graph_data`` emits exactly one node per concept."""
    b = Bundle.load(DEMO)
    data = build_graph_data(b)
    nodes = data["nodes"]
    assert len(nodes) == len(b.concepts), (
        f"node count {len(nodes)} != concept count {len(b.concepts)}; "
        f"a concept was dropped or duplicated in the graph payload"
    )


def test_graph_data_every_node_has_label_and_color() -> None:
    """Every node carries a non-empty label and a colour.

    These are the two fields Cytoscape needs to render a visible, labelled
    node (``background-color: data(color)`` and ``label: data(label)``); a
    missing/blank value would render an invisible or unlabelled node.
    """
    b = Bundle.load(DEMO)
    data = build_graph_data(b)
    assert data["nodes"], "graph data has no nodes"
    for node in data["nodes"]:
        d = node["data"]
        assert d.get("label"), (
            f"node {d.get('id')!r} is missing/empty label (renders unlabelled)"
        )
        assert d.get("color"), (
            f"node {d.get('id')!r} is missing/empty color (renders invisible)"
        )


def test_graph_data_node_ids_match_concept_ids() -> None:
    """The set of node ids equals the set of concept ids (no drops/dupes)."""
    b = Bundle.load(DEMO)
    data = build_graph_data(b)
    node_ids = {n["data"]["id"] for n in data["nodes"]}
    concept_ids = {concept_id_to_str(c.id) for c in b.concepts.values()}
    assert node_ids == concept_ids, (
        f"node ids != concept ids: missing={sorted(concept_ids - node_ids)} "
        f"extra={sorted(node_ids - concept_ids)}"
    )


def test_demo_graph_preserves_all_23_relationship_instances_in_bundles() -> None:
    """The real checkout fixture has a 3-instance parallel edge; none vanish."""
    data = build_graph_data(Bundle.load(DEMO))
    assert data["raw_relationship_count"] == 23
    assert sum(edge["data"]["multiplicity"] for edge in data["edges"]) == 23
    edge = next(
        edge["data"] for edge in data["edges"]
        if edge["data"]["source"] == "services/checkout"
        and edge["data"]["target"] == "tables/orders"
    )
    assert edge["multiplicity"] == 3
    assert [relation["type"] for relation in edge["relations"]] == [
        "link", "depends_on", "writes_to",
    ]
    assert [relation["label"] for relation in edge["relations"]] == [
        "orders table", "depends_on", "writes_to",
    ]
    assert edge["primary_relation"] == edge["relations"][1]
    assert edge["label"] == "depends_on"
    assert len({relation["id"] for relation in edge["relations"]}) == 3
    assert all(relation["direction"] == "outgoing" for relation in edge["relations"])


def test_bundled_edge_ids_are_stable_across_rebuilds() -> None:
    first = build_graph_data(Bundle.load(DEMO))["edges"]
    second = build_graph_data(Bundle.load(DEMO))["edges"]
    assert first == second


def test_relation_permutation_produces_exact_same_grouped_edge_payload(tmp_path: Path) -> None:
    """Authored list order cannot change grouped edge ordering, IDs, or primary."""
    def write_bundle(root: Path, relations: list[tuple[str, str]]) -> None:
        root.mkdir()
        rows = "".join(
            f"  - target: b\n    type: {kind}\n    detail: {detail}\n"
            for kind, detail in relations
        )
        (root / "a.md").write_text(
            "---\ntype: T\ntitle: A\nrelations:\n" + rows + "---\n[Body label](b.md)\n",
            encoding="utf-8",
        )
        (root / "b.md").write_text("---\ntype: T\ntitle: B\n---\nbody\n", encoding="utf-8")

    first, second = tmp_path / "first", tmp_path / "second"
    write_bundle(first, [("zeta", "last"), ("alpha", "first"), ("alpha", "second")])
    write_bundle(second, [("alpha", "second"), ("zeta", "last"), ("alpha", "first")])
    left = build_graph_data(Bundle.load(first))
    right = build_graph_data(Bundle.load(second))
    assert left["edges"] == right["edges"]
    edge = left["edges"][0]["data"]
    assert edge["multiplicity"] == 4
    assert [item["type"] for item in edge["relations"]] == ["link", "alpha", "alpha", "zeta"]
    assert [item.get("detail", "") for item in edge["relations"]] == ["", "first", "second", "last"]
    assert edge["primary_relation"]["type"] == "alpha"


def test_exact_duplicate_relations_get_distinct_deterministic_occurrence_ids(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\nrelations:\n"
        "  - {target: b, type: repeats, detail: same}\n"
        "  - {target: b, type: repeats, detail: same}\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\ntitle: B\n---\n", encoding="utf-8")
    edge = build_graph_data(Bundle.load(tmp_path))["edges"][0]["data"]
    assert edge["multiplicity"] == 2
    assert len({item["id"] for item in edge["relations"]}) == 2
    assert edge["primary_relation"] == edge["relations"][0]


def test_empty_single_and_self_loop_bundles_preserve_topology(tmp_path: Path) -> None:
    empty = tmp_path / "empty"; empty.mkdir()
    assert build_graph_data(Bundle.load(empty))["edges"] == []
    single = tmp_path / "single"; single.mkdir()
    (single / "a.md").write_text(
        "---\ntype: T\ntitle: A\nrelations:\n  - {target: a, type: validates_self}\n---\n",
        encoding="utf-8",
    )
    data = build_graph_data(Bundle.load(single))
    assert len(data["nodes"]) == 1 and len(data["edges"]) == 1
    edge = data["edges"][0]["data"]
    assert edge["source"] == edge["target"] == "a"
    assert edge["multiplicity"] == data["raw_relationship_count"] == 1
    assert data["nodes"][0]["data"]["degree"] == 2


def test_edge_ids_are_unambiguous_for_double_underscore_endpoint_collisions(tmp_path: Path) -> None:
    for name, body in {
        "a__b.md": "[c](c.md)", "a.md": "[b__c](b__c.md)",
        "b__c.md": "body", "c.md": "body",
    }.items():
        (tmp_path / name).write_text(f"---\ntype: T\ntitle: {name}\n---\n{body}\n", encoding="utf-8")
    edges = build_graph_data(Bundle.load(tmp_path))["edges"]
    by_pair = {(e["data"]["source"], e["data"]["target"]): e["data"]["id"] for e in edges}
    assert set(by_pair) == {("a__b", "c"), ("a", "b__c")}
    assert len(set(by_pair.values())) == 2
    assert all(edge_id.startswith("edge-") and len(edge_id.split("-", 2)[2]) == 64 for edge_id in by_pair.values())


def test_graph_data_exposes_allowlisted_grouping_metadata(tmp_path) -> None:
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: Runbook\n"
        "title: Deploy\n"
        "graph_cluster: ops/deploy\n"
        "source_system: local\n"
        "secret_owner: do-not-ship\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    data = build_graph_data(Bundle.load(tmp_path))
    node = data["nodes"][0]["data"]

    assert node["graph_cluster"] == "ops/deploy"
    assert node["source_system"] == "local"
    assert node["metadata"]["graph_cluster"] == "ops/deploy"
    assert "secret_owner" not in node["metadata"]


def test_graph_data_exposes_alias_object_labels(tmp_path) -> None:
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: Design\n"
        "title: Design System\n"
        "aliases:\n"
        "  - label: Architecture\n"
        "    discoverable: false\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    data = build_graph_data(Bundle.load(tmp_path))
    node = data["nodes"][0]["data"]

    assert node["aliases"] == ["Architecture"]


# ---------------------------------------------------------------------------
# Dangling-endpoint invariant (layout-graph).
#
# graph.js ``graphCandidate`` REJECTS any payload where an edge source/target
# or a backlinks/outgoing key/value does not reference a current node id. The
# visual producer must therefore never emit such endpoints. Reserved quadrant
# index files (e.g. govern/index.md) are parsed as ConceptIds but held in
# bundle.indexes (not bundle.concepts), and tolerated broken/missing links
# (SPEC §5.3) resolve to ConceptIds that are not loaded concepts — both
# appear in graph.edges with a non-None target but must be omitted from the
# visual payload. They remain model diagnostics in bundle.graph().unresolved.
# These tests mirror the client validator's exact invariant so they run in
# every suite (the browser proof in test_viewer_browser.py is skipped without
# Chromium) — they are the durable graph-init regression.
# ---------------------------------------------------------------------------


def _assert_no_dangling_endpoints(data) -> None:
    """Mirror graph.js ``graphCandidate``: every edge/backlink endpoint ∈ nodes."""
    node_ids = {n["data"]["id"] for n in data["nodes"]}
    for edge in data["edges"]:
        d = edge["data"]
        assert d["source"] in node_ids, (
            f"edge source {d['source']!r} is not a current node (dangling endpoint)"
        )
        assert d["target"] in node_ids, (
            f"edge target {d['target']!r} is not a current node (dangling endpoint)"
        )
    for key, sources in data["backlinks"].items():
        assert key in node_ids, (
            f"backlinks key {key!r} is not a current node (dangling endpoint)"
        )
        for src in sources:
            assert src in node_ids, (
                f"backlinks source {src!r} under {key!r} is not a current node "
                f"(dangling endpoint)"
            )


def test_graph_data_canonical_docs_bundle_has_no_dangling_endpoints() -> None:
    """The canonical docs-bundle graph payload has zero dangling endpoints.

    docs-bundle concepts link to reserved quadrant index files
    (explanation/index, how-to/index, reference/index, tutorials/index); these
    were emitted as dangling visual edges before the fix, which graph.js
    rejected, breaking the whole graph view. Every edge source/target and
    every backlinks key/source must belong to the final node ID set.
    """
    b = Bundle.load(DOCS)
    data = build_graph_data(b)
    _assert_no_dangling_endpoints(data)
    # Sanity: docs-bundle is non-trivial and actually has edges to prune, so
    # this is a real regression guard rather than a vacuous pass on an empty
    # graph.
    assert data["nodes"], "docs-bundle has no concepts"
    assert data["edges"], "docs-bundle has no concept-to-concept edges"
    # The reserved index files are NOT nodes...
    node_ids = {n["data"]["id"] for n in data["nodes"]}
    for reserved in ("explanation/index", "how-to/index", "reference/index", "tutorials/index"):
        assert reserved not in node_ids, f"{reserved!r} should not be a graph node"
    # ...yet their links remain usable navigation, not broken graph edges.
    navigation_targets = {
        concept_id_to_str(link.target)
        for link in b.graph().navigation
        if link.target is not None
    }
    assert "explanation/index" in navigation_targets, (
        "reserved-index link was lost from model navigation"
    )


def test_graph_data_omits_reserved_index_edges_but_model_keeps_navigation(
    tmp_path: Path,
) -> None:
    """A link to a reserved index file is omitted from the visual payload but
    retained in ``bundle.graph().navigation`` without a false broken-link badge.

    ``govern/index.md`` is a reserved filename: it is loaded into
    ``bundle.indexes`` (not ``bundle.concepts``), so a body link from
    ``govern/policy.md`` resolves to ConceptId ``("govern", "index")`` which
    is a parsed but non-current target — exactly the dangling-endpoint class.
    """
    (tmp_path / "govern").mkdir()
    # Reserved non-root index file (no frontmatter; marker-free → held in
    # bundle.indexes, not bundle.concepts).
    (tmp_path / "govern" / "index.md").write_text(
        "# Governance\n\nQuadrant index.\n", encoding="utf-8"
    )
    (tmp_path / "govern" / "policy.md").write_text(
        "---\ntype: Policy\ntitle: Policy\n---\n"
        "See the [index](index.md) and a [real peer](charter.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "govern" / "charter.md").write_text(
        "---\ntype: Policy\ntitle: Charter\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    data = build_graph_data(b)

    _assert_no_dangling_endpoints(data)
    node_ids = {n["data"]["id"] for n in data["nodes"]}
    assert "govern/index" not in node_ids
    assert {"govern/policy", "govern/charter"} <= node_ids
    # The reserved-index edge is omitted from the visual payload...
    pairs = {(e["data"]["source"], e["data"]["target"]) for e in data["edges"]}
    assert ("govern/policy", "govern/index") not in pairs
    assert "govern/index" not in data["backlinks"]
    # ...the valid current-concept edge is retained...
    assert ("govern/policy", "govern/charter") in pairs
    # ...and the reserved-index link survives as non-graph navigation.
    navigation_targets = {
        concept_id_to_str(link.target)
        for link in b.graph().navigation
        if link.target is not None
    }
    assert "govern/index" in navigation_targets
    assert not b.graph().unresolved
    assert all(node['data']['unresolved_count'] == 0 for node in data['nodes'])


def test_graph_data_retains_valid_current_concept_edges(tmp_path: Path) -> None:
    """Edges between two loaded concepts are retained (the invariant does not
    over-prune). Covers the swap/nested legitimate-edge case."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [b](b.md).\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: B\n---\nBack to [a](a.md).\n", encoding="utf-8"
    )
    data = build_graph_data(Bundle.load(tmp_path))
    _assert_no_dangling_endpoints(data)
    pairs = {(e["data"]["source"], e["data"]["target"]) for e in data["edges"]}
    assert ("a", "b") in pairs
    assert ("b", "a") in pairs
    # Backlinks are populated from the retained edges.
    assert set(data["backlinks"].get("a", [])) == {"b"}
    assert set(data["backlinks"].get("b", [])) == {"a"}


# ---------------------------------------------------------------------------
# Phase 3 graph visual language: degree-driven size + degree field +
# deterministic broken-link count (Graph.unresolved derivation).
# ---------------------------------------------------------------------------


def test_graph_data_size_is_degree_driven_and_monotonic(tmp_path: Path) -> None:
    """Phase 3: ``size`` is degree-driven (was body-length). A node with more
    in-bundle edge endpoints must end up no smaller than a node with fewer,
    and an orphan (degree 0) must get the minimum. Body length must NOT
    influence size (a long body on an isolated node must not inflate it).

    Degree here = sum of outgoing + backlinks over deduped edges (both
    directions counted separately, matching the client ``degreeOf``).
    """
    # a → b → c (linear chain with bidirectional body links) plus an
    # isolated long-body node. Degree: a=2, b=4, c=2, isolated=0.
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [b](b.md).\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: B\n---\nSee [a](a.md) and [c](c.md).\n", encoding="utf-8"
    )
    (tmp_path / "c.md").write_text(
        "---\ntype: T\ntitle: C\n---\nSee [b](b.md).\n", encoding="utf-8"
    )
    # Isolated node with a HUGE body — under the old body-length proxy it
    # would have been the largest node on the canvas; under degree-driven
    # sizing it must be the minimum (degree 0).
    (tmp_path / "isolated.md").write_text(
        "---\ntype: T\ntitle: Isolated\n---\n" + "x" * 5000 + "\n", encoding="utf-8"
    )
    data = build_graph_data(Bundle.load(tmp_path))
    by_id = {n["data"]["id"]: n["data"] for n in data["nodes"]}
    assert by_id["isolated"]["degree"] == 0
    # The chain's middle node has the highest degree (bidirectional endpoints
    # to both a and c ⇒ 4 edge endpoints).
    assert by_id["b"]["degree"] == 4
    assert by_id["a"]["degree"] == 2
    assert by_id["c"]["degree"] == 2
    # Size is monotonic non-decreasing in degree.
    assert by_id["isolated"]["size"] <= by_id["a"]["size"] == by_id["c"]["size"]
    assert by_id["a"]["size"] < by_id["b"]["size"], (
        f"middle node b must be larger than leaf a (got b={by_id['b']['size']} "
        f"a={by_id['a']['size']})"
    )
    # The long body must NOT inflate the isolated node — it stays at min size
    # (the body-length proxy would have given it ~50).
    assert by_id["isolated"]["size"] == 24, (
        f"isolated node with degree 0 must be the minimum size 24 "
        f"(got {by_id['isolated']['size']})"
    )
    # All Phase 3 nodes carry the new ``degree`` field.
    for nid, d in by_id.items():
        assert "degree" in d, f"{nid} missing 'degree' field"
        assert isinstance(d["degree"], int) and d["degree"] >= 0


def test_graph_data_degree_matches_client_unique_neighbour_count(
    tiny_good_bundle: Path,
) -> None:
    """Phase 3 contract-runtime-parity: the server-side ``degree`` field must
    equal the edge-endpoint count the client computes from the deduped edge
    list (``(outgoing[id]||[]).length + (backlinks[id]||[]).length`` — both
    directions counted separately).
    """
    data = build_graph_data(Bundle.load(tiny_good_bundle))
    outgoing: dict[str, list[str]] = {}
    backlinks: dict[str, list[str]] = {}
    for e in data["edges"]:
        s, t = e["data"]["source"], e["data"]["target"]
        outgoing.setdefault(s, [])
        if t not in outgoing[s]:
            outgoing[s].append(t)
        backlinks.setdefault(t, [])
        if s not in backlinks[t]:
            backlinks[t].append(s)
    for n in data["nodes"]:
        nid = n["data"]["id"]
        client_deg = len(outgoing.get(nid, [])) + len(backlinks.get(nid, []))
        assert n["data"]["degree"] == client_deg, (
            f"{nid}: server degree {n['data']['degree']} != client degreeOf "
            f"{client_deg} (contract drift between server size hint and client "
            f"hub-halo / orphan-shelf computations)"
        )


def test_graph_data_unresolved_count_derived_from_graph_unresolved(
    tmp_path: Path,
) -> None:
    """Phase 3: ``unresolved_count`` is a deterministic derivation from
    ``Graph.unresolved`` — the model's existing broken-link diagnostic
    (SPEC §5.3). Every unresolved link increments its source node's count.
    No new product semantics are introduced."""
    # policy links to a missing target (broken) + a real peer; charter is
    # the real peer; orphans/missing links to reserved index files count too.
    (tmp_path / "govern").mkdir()
    (tmp_path / "govern" / "index.md").write_text(
        "# Governance\n\nQuadrant index.\n", encoding="utf-8"
    )
    (tmp_path / "govern" / "policy.md").write_text(
        "---\ntype: Policy\ntitle: Policy\n---\n"
        "See the [index](index.md), a [real peer](charter.md), "
        "and a [missing](does-not-exist.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "govern" / "charter.md").write_text(
        "---\ntype: Policy\ntitle: Charter\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    data = build_graph_data(b)
    by_id = {n["data"]["id"]: n["data"] for n in data["nodes"]}

    # The sum of per-node unresolved_count must equal Graph.unresolved's
    # count (every unresolved link has a source concept).
    g = b.graph()
    expected_total = sum(1 for u in g.unresolved if u.source is not None)
    actual_total = sum(n["unresolved_count"] for n in by_id.values())
    assert actual_total == expected_total, (
        f"sum of per-node unresolved_count ({actual_total}) != "
        f"Graph.unresolved with non-None source ({expected_total})"
    )
    # Every node carries the new field (defaults to 0).
    for nid, d in by_id.items():
        assert "unresolved_count" in d, f"{nid} missing 'unresolved_count' field"
        assert isinstance(d["unresolved_count"], int) and d["unresolved_count"] >= 0
    # policy.md has at least one unresolved outgoing link (the missing target
    # and/or the reserved-index target). charter has none.
    assert by_id["govern/policy"]["unresolved_count"] >= 1, (
        "policy node should have at least one unresolved outgoing link"
    )
    assert by_id["govern/charter"]["unresolved_count"] == 0


def test_graph_data_unresolved_count_deterministic_across_calls(
    tmp_path: Path,
) -> None:
    """Phase 3 contract-runtime-parity: ``build_graph_data`` is pure — two
    calls on the same bundle return identical ``unresolved_count`` values
    per node. Static/live/single-file render paths all see the same count."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [missing](no-such.md).\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    d1 = {n["data"]["id"]: n["data"]["unresolved_count"] for n in build_graph_data(b)["nodes"]}
    d2 = {n["data"]["id"]: n["data"]["unresolved_count"] for n in build_graph_data(b)["nodes"]}
    assert d1 == d2, f"non-deterministic unresolved_count: {d1} vs {d2}"
    assert d1["a"] == 1
