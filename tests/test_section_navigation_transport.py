"""R1/R2 section-destination transport: source proof, boundaries, parity.

Covers the narrow durable seam behind the "Open matching section" reading
journey:

* ``navigation.build_section_navigation`` — the bounded OPTIONAL presentation
  metadata (strict schema-v1 passage/corpus records stay untouched); entries
  bind passage id -> {concept, fragment, heading, source_revision} with the
  source/page revision owner kept distinct from the corpus binding;
* the canonical bootstrap script (static/spa/single-file) and its
  absent-metadata ``null`` fallback;
* the live HTML-path search adapter, including textual-evidence eligibility,
  explicit concept identity, and stale-revision fail-closed behavior;
* Python/JS parity for the once-per-snapshot canonical parser, the
  per-result destination lookup, and the intent label hash in
  ``portable-retrieval.js`` (Node runtime, same harness as test_render).

Browser journeys for the same feature live in
``tests/test_section_journey_browser.py``; duplicated-title, fence, Unicode,
CRLF and offset ambiguity source cases are owned by
``tests/test_navigation_sections.py`` and are reused rather than repeated.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.annotations import concept_content_rev
from okf_loom.navigation import (
    MAX_SECTION_NAVIGATION_FRAGMENT_CHARS,
    _fragment_is_transport_safe,
    _RenderedHeadingIds,
    build_section_navigation,
    section_intent_hash,
)
from okf_loom.paths import concept_id_from_str, concept_id_to_str
from okf_loom.retrieval import RetrievalService, SearchRequest, bundle_revision
from okf_loom.render import (
    _browser_passage_corpus,
    _render_search_page,
    build_graph_data,
    build_site,
    render_single_file,
)

from conftest import TOOLKIT_ROOT


def _build_bundle_with(tmp_path: Path, concept_files: dict[str, str]) -> Path:
    for relpath, content in concept_files.items():
        p = tmp_path / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


JOURNEY_BUNDLE = {
    "index.md": "# Bundle\n",
    "services/checkout.md": (
        "---\ntype: Service\ntitle: Checkout Service\n---\n"
        "# Overview\n\ncheckout validates the cart and appends order rows.\n"
        "# Endpoints\n\nPOST /v2/orders creates an order.\n"
    ),
    "tables/orders.md": (
        "---\ntype: Table\ntitle: Orders\nrelations:\n"
        "  - target: services/checkout\n    type: written_by\n---\n"
        "# Schema\n\nColumns are typed and documented.\n"
    ),
}


def _canonical_transport(payload) -> str:
    """The exact producer serialization the client's parse insists on."""
    return json.dumps(payload, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).replace("<", "\\u003c")


def _portable_node(artifact: dict, program: str) -> dict:
    """Run the dependency-free portable runtime in Node's WebCrypto runtime."""
    script = f"""
const fs=require('fs'), crypto=require('crypto');
global.window={{crypto:crypto.webcrypto,TextEncoder:global.TextEncoder}};
eval(fs.readFileSync({json.dumps(str(TOOLKIT_ROOT / 'scripts' / 'okf_loom' / 'viewer' / 'static' / 'portable-retrieval.js'))},'utf8'));
const artifact=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{{{program}}})().catch(e=>{{console.error(e.stack||e);process.exit(1);}});
"""
    result = subprocess.run(
        ["node", "-e", script], input=json.dumps(artifact), text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _artifact_with_navigation(bundle: Bundle) -> tuple[dict, dict]:
    """Corpus artifact plus the section navigation it travels with."""
    artifact = _browser_passage_corpus(bundle, build_graph_data(bundle)["edges"])
    artifact["navigation"] = build_section_navigation(bundle)
    return artifact, artifact["navigation"]


# --- build_section_navigation ------------------------------------------------


def _concept_source_revision(bundle: Bundle, cid: str) -> str:
    """The per-concept normalized-source revision an entry must carry."""
    concept = bundle.concepts.get(concept_id_from_str(cid))
    assert concept is not None
    return concept_content_rev(concept.raw_text.replace("\r\n", "\n"))


def test_section_navigation_maps_only_proven_passages(tmp_path: Path) -> None:
    root = _build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n",
        "services/checkout.md": (
            "---\ntype: Service\ntitle: Checkout Service\n---\n"
            "# Overview\n\ncheckout validates the cart.\n\n"
            "## Notes\n\ndup one\n\n## Notes\n\ndup two\n"
        ),
        "tables/preamble.md": (
            "---\ntype: Table\ntitle: Preamble\n---\n"
            "A preamble paragraph before any heading.\n"
        ),
    })
    bundle = Bundle.load(root)
    nav = build_section_navigation(bundle)
    assert nav is not None and nav["schema"] == 2
    assert nav["bundle_revision"] == RetrievalService(bundle).revision
    fragments = nav["fragments"]
    # The duplicate titles map because their actual rendered ids stay unique.
    assert sorted(
        entry["fragment"] for pid, entry in fragments.items()
        if pid.startswith("services/checkout:")
    ) == ["notes", "notes-1", "overview"]
    # A headingless (preamble) concept contributes nothing at all.
    assert not any(pid.startswith("tables/preamble:") for pid in fragments)
    # Every entry matches the revision-gated proof for that passage and
    # binds concept identity, renderer-owned label, and that concept's
    # per-document normalized-source revision explicitly — never the corpus
    # revision, which only the payload top level owns.
    service = RetrievalService(bundle)
    revision = service.revision
    for passage in service.passages:
        expected = service.verified_section_fragment(
            concept_id_to_str(passage.concept_id),
            passage.passage_id, revision=revision)
        entry = fragments.get(passage.passage_id)
        if expected is None:
            assert entry is None
            continue
        assert entry == {
            "concept": concept_id_to_str(passage.concept_id),
            "fragment": expected,
            "heading": "Overview" if expected == "overview" else entry["heading"],
            "source_revision": _concept_source_revision(
                bundle, concept_id_to_str(passage.concept_id)),
        }
    assert (fragments and
            all(entry["source_revision"] != nav["bundle_revision"]
                for entry in fragments.values()))


def test_section_navigation_is_deterministic_and_revision_bound(
    tmp_path: Path,
) -> None:
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    first = build_section_navigation(Bundle.load(root))
    second = build_section_navigation(Bundle.load(root))
    assert first == second
    changed_root = _build_bundle_with(tmp_path / "changed", JOURNEY_BUNDLE)
    checkout = changed_root / "services" / "checkout.md"
    checkout.write_text(
        checkout.read_text(encoding="utf-8") + "\n# Extra\n\nmore words\n",
        encoding="utf-8")
    third = build_section_navigation(Bundle.load(changed_root))
    assert third is not None
    assert third["bundle_revision"] != first["bundle_revision"]
    # The per-entry revision is PER-CONCEPT: editing services/checkout moves
    # checkout's own revision only, and an unrelated concept's entries keep
    # theirs — an unrelated edit cannot manufacture or invalidate a target's
    # source revision.
    checkout_pid = sorted(
        pid for pid in third["fragments"] if pid.startswith("services/checkout:"))[0]
    orders_pid = sorted(
        pid for pid in third["fragments"] if pid.startswith("tables/orders:"))[0]
    changed_bundle = Bundle.load(changed_root)
    assert (third["fragments"][checkout_pid]["source_revision"]
            == _concept_source_revision(changed_bundle, "services/checkout"))
    assert (third["fragments"][checkout_pid]["source_revision"]
            != first["fragments"][checkout_pid]["source_revision"])
    assert (third["fragments"][orders_pid]["source_revision"]
            == first["fragments"][orders_pid]["source_revision"]
            == _concept_source_revision(Bundle.load(root), "tables/orders"))
    assert (third["fragments"][checkout_pid]["source_revision"]
            != third["bundle_revision"])


def test_section_navigation_source_revision_is_normalized_per_concept(
    tmp_path: Path,
) -> None:
    """Identical content shares a revision (so destination admission must
    match concept identity too), and the revision binds the LF-normalized
    canonical source, never served CRLF bytes."""
    body = (
        "---\ntype: Service\ntitle: Twin\n---\n"
        "# Overview\n\nidentical body text.\n"
    )
    root = _build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n",
        "notes/twin-a.md": body,
        "notes/twin-b.md": body,
    })
    bundle = Bundle.load(root)
    assert (_concept_source_revision(bundle, "notes/twin-a")
            == _concept_source_revision(bundle, "notes/twin-b"))
    crlf_root = _build_bundle_with(tmp_path / "crlf", {
        "index.md": "# Bundle\n",
        "notes/twin-a.md": body.replace("\n", "\r\n"),
    })
    assert (_concept_source_revision(Bundle.load(crlf_root), "notes/twin-a")
            == _concept_source_revision(bundle, "notes/twin-a"))


def test_section_navigation_misaligned_body_skips_not_raises(
    tmp_path: Path,
) -> None:
    """An in-memory source/body divergence makes the concept unprovable:
    build_section_navigation skips it (or returns None) — never an unpack
    exception."""
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    bundle = Bundle.load(root)
    concept = bundle.concepts.get(concept_id_from_str("services/checkout"))
    assert concept is not None
    concept.body = "# Diverged\n\nin-memory body no longer matches source.\n"
    nav = build_section_navigation(bundle)  # must not raise
    assert nav is not None
    assert not any(pid.startswith("services/checkout:") for pid in nav["fragments"])


def test_rendered_heading_labels_ignore_void_tags(tmp_path: Path) -> None:
    """Void tags inside headings never extend label capture across the
    heading's end tag, so following headings keep their own labels."""
    parser = _RenderedHeadingIds()
    parser.feed(
        '<h2 id="a">Alpha<br></h2>\n'
        '<h3 id="b">Beta</h3>\n'
        '<h4 id="c"><img src="i.png" alt="pic"> Gamma</h4>\n'
        '<h5 id="d">Delta<br/></h5>'
    )
    assert parser.labels == {"a": "Alpha", "b": "Beta",
                             "c": "Gamma", "d": "Delta"}


def test_section_navigation_entry_and_byte_caps_return_none_not_partial(
    tmp_path: Path, monkeypatch,
) -> None:
    from okf_loom import navigation
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    bundle = Bundle.load(root)
    monkeypatch.setattr(navigation, "MAX_SECTION_NAVIGATION_ENTRIES", 1)
    assert build_section_navigation(bundle) is None
    monkeypatch.setattr(navigation, "MAX_SECTION_NAVIGATION_ENTRIES", 20000)
    monkeypatch.setattr(navigation, "MAX_SECTION_NAVIGATION_BYTES", 64)
    assert build_section_navigation(bundle) is None


def test_section_navigation_absent_for_headingless_bundle(tmp_path: Path) -> None:
    root = _build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n",
        "tables/preamble.md": "---\ntype: Table\ntitle: Preamble\n---\nJust prose.\n",
    })
    assert build_section_navigation(Bundle.load(root)) is None


def test_fragment_transport_safety_rejects_hostile_ids() -> None:
    assert _fragment_is_transport_safe("overview")
    assert _fragment_is_transport_safe("über-schritt-1")
    assert not _fragment_is_transport_safe("")
    assert not _fragment_is_transport_safe("a\u0007b")
    assert not _fragment_is_transport_safe("a\u202eb")
    assert not _fragment_is_transport_safe(
        "x" * (MAX_SECTION_NAVIGATION_FRAGMENT_CHARS + 1))


def test_section_intent_hash_is_deterministic_hex() -> None:
    assert section_intent_hash("Overview") == section_intent_hash("Overview")
    assert section_intent_hash("Überschrift 1") != section_intent_hash("Overview")
    assert len(section_intent_hash("Overview")) == 16
    int(section_intent_hash("Overview"), 16)


# --- exported bootstrap + single-file embed ----------------------------------


def test_export_workbench_carries_canonical_bootstrap_and_concept_pages_do_not(
    tmp_path: Path,
) -> None:
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    static_out = tmp_path / "site-static"
    build_site(Bundle.load(root), static_out, target="static")
    search_html = (static_out / "__search.html").read_text(encoding="utf-8")
    assert 'id="okf-section-bootstrap"' in search_html
    raw = search_html.split('id="okf-section-bootstrap">', 1)[1].split(
        "</script>", 1)[0]
    payload = json.loads(raw)
    assert payload["schema"] == 2
    nav = build_section_navigation(Bundle.load(root))
    assert raw == _canonical_transport(nav)
    # Concept pages carry the destination-side rendered-outline proof, not
    # the search transport.
    concept_html = (static_out / "services" / "checkout.html").read_text(
        encoding="utf-8")
    graph_html = (static_out / "__graph.html").read_text(encoding="utf-8")
    assert 'id="okf-section-bootstrap"' not in concept_html
    assert 'id="okf-section-bootstrap"' not in graph_html
    outline = json.loads(concept_html.split(
        'id="okf-rendered-outline">', 1)[1].split("</script>", 1)[0])
    assert outline["schema"] == 1
    assert outline["concept"] == "services/checkout"
    assert outline["headings"] == {"overview": "Overview", "endpoints": "Endpoints"}
    assert outline["source_revision"] == _concept_source_revision(
        Bundle.load(root), "services/checkout")
    assert outline["source_revision"] != bundle_revision(Bundle.load(root))


def test_spa_bootstrap_present_and_serve_page_omits_it(tmp_path: Path) -> None:
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    spa_out = tmp_path / "site-spa"
    build_site(Bundle.load(root), spa_out, target="spa")
    spa_search = (spa_out / "__search.html").read_text(encoding="utf-8")
    assert 'id="okf-section-bootstrap"' in spa_search
    bundle = Bundle.load(root)
    # serve pages are rendered per request; the bootstrap is export-only.
    html = _render_search_page(bundle, mode="serve", name=bundle.name,
                               config={}, query="checkout", results=[])
    assert 'id="okf-section-bootstrap"' not in html


def test_bootstrap_absent_metadata_falls_back_to_null(
    tmp_path: Path, monkeypatch,
) -> None:
    from okf_loom import render
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    monkeypatch.setattr(render, "build_section_navigation", lambda bundle: None)
    static_out = tmp_path / "site-fallback"
    build_site(Bundle.load(root), static_out, target="static")
    search_html = (static_out / "__search.html").read_text(encoding="utf-8")
    # The canonical script always ships; `null` is the honest absent-metadata
    # shape the client's parser refuses into the root fallback.
    assert 'id="okf-section-bootstrap">null</script>' in search_html
    single = tmp_path / "single" / "index.html"
    single.parent.mkdir(parents=True)
    render_single_file(Bundle.load(root), single)
    assert 'id="okf-section-bootstrap">null</script>' in single.read_text(
        encoding="utf-8")


def test_single_file_carries_canonical_bootstrap_beside_corpus(
    tmp_path: Path,
) -> None:
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    bundle = Bundle.load(root)
    out = tmp_path / "single" / "index.html"
    out.parent.mkdir(parents=True)
    render_single_file(bundle, out)
    content = out.read_text(encoding="utf-8")
    assert '"retrieval_corpus"' in content
    # The transport is a canonical sibling script, not a BUNDLE key.
    assert '"section_navigation"' not in content
    raw = content.split('id="okf-section-bootstrap">', 1)[1].split(
        "</script>", 1)[0]
    payload = json.loads(raw)
    assert payload["schema"] == 2
    assert raw == _canonical_transport(build_section_navigation(bundle))
    corpus = _browser_passage_corpus(bundle, build_graph_data(bundle)["edges"])
    assert payload["bundle_revision"] == corpus["manifest"]["bundle_revision"]
    # The independent rendered-source binding travels as its own canonical
    # sibling: concept identity -> per-concept normalized-source revision
    # computed from the SAME bundle source the bodies were rendered from.
    rev_raw = content.split('id="okf-single-source-revisions">', 1)[1].split(
        "</script>", 1)[0]
    rev_payload = json.loads(rev_raw)
    assert rev_payload == {
        "schema": 1,
        "revisions": {
            concept_id_to_str(cid): concept_content_rev(
                concept.raw_text.replace("\r\n", "\n"))
            for cid, concept in sorted(bundle.concepts.items())
        },
    }
    assert rev_raw == _canonical_transport(rev_payload)
    assert (rev_payload["revisions"]["services/checkout"]
            != payload["bundle_revision"])


# --- live search adapter ------------------------------------------------------


def test_search_section_fragments_require_textual_evidence_and_identity(
    tmp_path: Path,
) -> None:
    from okf_loom.server import _search_section_fragments
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    bundle = Bundle.load(root)
    service = RetrievalService(bundle)
    revision = service.revision
    lexical_rows = [row.as_dict() for row in service.search(
        SearchRequest(query="checkout", mode="lexical", limit=20))]
    sections = _search_section_fragments(
        lexical_rows, service, revision, "checkout")
    # A textual hit plus source proof earns exactly the proven section.
    assert set(sections) == {"services/checkout"}
    assert sections["services/checkout"]["concept"] == "services/checkout"
    assert sections["services/checkout"]["fragment"] == "overview"
    assert sections["services/checkout"]["heading"] == "Overview"
    assert (sections["services/checkout"]["source_revision"]
            == _concept_source_revision(bundle, "services/checkout"))
    assert (sections["services/checkout"]["source_revision"]
            != bundle_revision(bundle))
    # Cross-concept passage/row identity is explicit: a row whose evidence
    # passage claims another concept never earns a section.
    tampered = json.loads(json.dumps(lexical_rows))
    for row in tampered:
        passage = (row.get("evidence") or {}).get("passage")
        if passage and row["concept_id"] == "services/checkout":
            passage["concept_id"] = "tables/orders"
    assert _search_section_fragments(
        tampered, service, revision, "checkout") == {}
    # Relation evidence is graph-only here: the Orders row's attached Schema
    # passage contains no query term, so it never claims a section.
    relation_rows = [row.as_dict() for row in service.search(
        SearchRequest(query="checkout", mode="relation", limit=20))]
    assert [row["concept_id"] for row in relation_rows] == ["tables/orders"]
    assert _search_section_fragments(
        relation_rows, service, revision, "checkout") == {}
    # Stale revision: fail closed to no destinations at all.
    stale = "section-retrieval-v1:" + "0" * 64
    assert _search_section_fragments(lexical_rows, service, stale, "checkout") == {}
    # No query terms (tag/entity-style browsing): no section claims.
    assert _search_section_fragments(lexical_rows, service, revision, "") == {}
    # Stopword-only queries never manufacture textual evidence.
    assert _search_section_fragments(lexical_rows, service, revision, "the and of") == {}


def test_public_search_wire_rows_carry_no_section_metadata(tmp_path: Path) -> None:
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    bundle = Bundle.load(root)
    rows = [row.as_dict() for row in RetrievalService(bundle).search(
        SearchRequest(query="checkout", mode="lexical", limit=20))]
    assert rows
    for row in rows:
        assert not any("section" in key for key in row)
        passage = (row.get("evidence") or {}).get("passage") or {}
        assert not any("section" in key for key in passage)


# --- portable JS parity -------------------------------------------------------


def test_portable_js_parse_destination_and_intent_href(tmp_path: Path) -> None:
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    bundle = Bundle.load(root)
    artifact, nav = _artifact_with_navigation(bundle)
    artifact = dict(artifact, canonicalNavigation=_canonical_transport(nav))
    result = _portable_node(artifact, r"""
const nav=artifact.navigation;
const v=await window.okfPortableRetrieval.validate(artifact.manifest,artifact.chunks);
const p=window.okfPortableRetrieval.create(artifact.manifest,v.passages,v.edges);
const rev=p.manifest.bundle_revision;
const out={};
const canonical=artifact.canonicalNavigation;
out.parsed=!!window.okfPortableRetrieval.parseSectionNavigation(canonical);
const snapshot=window.okfPortableRetrieval.parseSectionNavigation(canonical);
const rows=p.search({query:'checkout',mode:'lexical',limit:20}).rows;
const hit=rows.find(r=>r.passage&&r.passage.concept_id==='services/checkout');
const other=rows.find(r=>r.passage&&r.passage.concept_id==='tables/orders');
const dest=window.okfPortableRetrieval.sectionDestination(snapshot,rev,hit,'checkout');
out.dest=dest;
out.href=window.okfPortableRetrieval.sectionIntentHref('/services/checkout',dest);
out.staleRev=window.okfPortableRetrieval.sectionDestination(snapshot,'section-retrieval-v1:0',hit,'checkout');
out.noQuery=window.okfPortableRetrieval.sectionDestination(snapshot,rev,hit,'');
out.stopwords=window.okfPortableRetrieval.sectionDestination(snapshot,rev,hit,'the and of');
out.noTextualHit=window.okfPortableRetrieval.sectionDestination(snapshot,rev,other,'checkout');
out.graphOnly=window.okfPortableRetrieval.sectionDestination(snapshot,rev,{concept:hit.concept},'checkout');
out.absentNav=window.okfPortableRetrieval.sectionDestination(null,rev,hit,'checkout');
out.hashOverview=window.okfPortableRetrieval.sectionIntentHash('Overview');
out.hashUnicode=window.okfPortableRetrieval.sectionIntentHash('Überschrift');
out.sourceRevisions=window.okfPortableRetrieval.parseSourceRevisions(
  '{"revisions":{"services/checkout":"'+'a'.repeat(64)+'"},"schema":1}');
out.sourceRevisionsNull=window.okfPortableRetrieval.parseSourceRevisions('null');
out.sourceRevisionsTampered=window.okfPortableRetrieval.parseSourceRevisions(
  '{"revisions":{"services/checkout":"'+'a'.repeat(64)+'"},"schema":1,"x":1}');
console.log(JSON.stringify(out));
""")
    source_revision = _concept_source_revision(bundle, "services/checkout")
    assert result["parsed"] is True
    assert result["dest"] == {
        "concept": "services/checkout", "fragment": "overview",
        "heading": "Overview", "sourceRevision": source_revision,
    }
    href = result["href"]
    assert href.startswith(
        "/services/checkout?okf-section=overview&okf-rev=")
    assert "okf-c=services%2Fcheckout" in href
    assert href.index("okf-rev=") < href.index("okf-c=") < href.index("okf-h=")
    assert "okf-h=" + section_intent_hash("Overview") in href
    assert result["staleRev"] is None
    assert result["noQuery"] is None
    assert result["stopwords"] is None
    assert result["noTextualHit"] is None
    assert result["graphOnly"] is None
    assert result["absentNav"] is None
    assert result["hashOverview"] == section_intent_hash("Overview")
    assert result["hashUnicode"] == section_intent_hash("Überschrift")
    assert result["sourceRevisions"] == {
        "services/checkout": "a" * 64}
    assert result["sourceRevisionsNull"] is None
    assert result["sourceRevisionsTampered"] is None


def test_portable_js_parse_rejects_meaningful_negatives(
    tmp_path: Path,
) -> None:
    """Duplicate keys, noncanonical bytes, prototype keys, hostile values,
    structural drift, and over-bound payloads are refused whole."""
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    bundle = Bundle.load(root)
    artifact, nav = _artifact_with_navigation(bundle)
    assert nav is not None
    canonical = _canonical_transport(nav)
    revision = nav["bundle_revision"]
    source_revision = _concept_source_revision(bundle, "services/checkout")
    entry = {"concept": "services/checkout", "fragment": "overview",
             "heading": "Overview", "source_revision": source_revision}
    fragment_pid = "services/checkout:L12-L12"

    def with_fragments(fragments) -> dict:
        return {"schema": 2, "bundle_revision": revision,
                "fragments": fragments}

    negatives = {
        "duplicateKey": (
            '{"schema":2,"bundle_revision":"%s","fragments":{},'
            '"bundle_revision":"%s"}' % (revision, revision)),
        "noncanonicalWhitespace": json.dumps(
            nav, ensure_ascii=True, sort_keys=True),
        "nonCanonicalKeyOrder": json.dumps(
            {"fragments": nav["fragments"], "schema": 2,
             "bundle_revision": revision},
            ensure_ascii=True, separators=(",", ":")),
        "protoKey": _canonical_transport(
            {"schema": 2, "bundle_revision": revision,
             "fragments": {"__proto__": entry}}),
        "bidiFragment": _canonical_transport(with_fragments(
            {fragment_pid: dict(entry, fragment="a\u202eb")})),
        "controlFragment": _canonical_transport(with_fragments(
            {fragment_pid: dict(entry, fragment="a\u0007b")})),
        "overlongFragment": _canonical_transport(with_fragments(
            {fragment_pid: dict(entry, fragment="x" * 513)})),
        "emptyFragment": _canonical_transport(with_fragments(
            {fragment_pid: dict(entry, fragment="")})),
        "missingHeading": _canonical_transport(with_fragments(
            {fragment_pid: {"concept": "services/checkout",
                            "fragment": "overview",
                            "source_revision": source_revision}})),
        "emptyHeading": _canonical_transport(with_fragments(
            {fragment_pid: dict(entry, heading="")})),
        "bidiHeading": _canonical_transport(with_fragments(
            {fragment_pid: dict(entry, heading="Over\u202eview")})),
        "wrongSchema": _canonical_transport(
            dict(nav, schema=1)),
        "extraTopKey": _canonical_transport(
            dict(nav, extra=1)),
        "conceptMismatchShape": _canonical_transport(with_fragments(
            {fragment_pid: dict(entry, concept="no spaces allowed")})),
        "missingSourceRevision": _canonical_transport(with_fragments(
            {fragment_pid: {"concept": "services/checkout",
                            "fragment": "overview",
                            "heading": "Overview"}})),
        "emptyRaw": "",
        "notAnObject": _canonical_transport([1, 2]),
    }
    huge = with_fragments({})
    for i in range(20001):
        huge["fragments"][f"services/checkout:L{i + 1}-L{i + 1}"] = dict(
            entry, fragment=f"s{i}")
    negatives["overCap"] = _canonical_transport(huge)
    artifact = dict(artifact, negatives=negatives)
    result = _portable_node(artifact, r"""
const out={};
for(const [name,raw] of Object.entries(artifact.negatives)){
  out[name]=window.okfPortableRetrieval.parseSectionNavigation(raw);
}
console.log(JSON.stringify(out));
""")
    for name in negatives:
        assert result[name] in (None, False), name
    assert result["overCap"] is None


def test_portable_js_section_destination_eligibility_matches_python(
    tmp_path: Path,
) -> None:
    """Graph-only evidence never earns a section; the JS rule mirrors Python."""
    root = _build_bundle_with(tmp_path, JOURNEY_BUNDLE)
    bundle = Bundle.load(root)
    artifact, _nav = _artifact_with_navigation(bundle)
    artifact = dict(artifact, canonicalNavigation=_canonical_transport(_nav))
    from okf_loom.server import _search_section_fragments
    service = RetrievalService(bundle)
    revision = service.revision
    rows = [row.as_dict() for row in service.search(
        SearchRequest(query="schema", mode="relation", limit=20))]
    python_sections = _search_section_fragments(rows, service, revision, "schema")
    result = _portable_node(artifact, r"""
const v=await window.okfPortableRetrieval.validate(artifact.manifest,artifact.chunks);
const p=window.okfPortableRetrieval.create(artifact.manifest,v.passages,v.edges);
const snapshot=window.okfPortableRetrieval.parseSectionNavigation(artifact.canonicalNavigation);
const rev=p.manifest.bundle_revision;
const rows=p.search({query:'schema',mode:'relation',limit:20}).rows;
const out={};
rows.forEach(r=>{const cid=r.passage?r.passage.concept_id:r.concept.id;
  const d=window.okfPortableRetrieval.sectionDestination(snapshot,rev,r,'schema');
  if(d)out[cid]=d.fragment;});
console.log(JSON.stringify(out));
""")
    assert result == {cid: entry["fragment"]
                      for cid, entry in python_sections.items()}
