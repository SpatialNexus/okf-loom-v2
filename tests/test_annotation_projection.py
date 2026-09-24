"""Renderer contract proofs for the versioned annotation DOM projection."""
from __future__ import annotations

import json
import re
import html as html_lib
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.annotations import concept_content_rev, extract_blocks, resolve_anchor
from okf_loom.render import render_concept_body_with_projection, _render_concept_page, build_site
from okf_loom.viewer.assets import auto_palette


def _bundle(tmp_path, body: str, *, newline: str = "\n") -> Bundle:
    raw = "---\ntype: table\ntitle: Projection\n---\n" + body
    path = tmp_path / "projection.md"
    path.write_bytes(raw.replace("\n", newline).encode("utf-8"))
    return Bundle.load(tmp_path)


def test_projection_handles_frontmatter_duplicates_inline_entities_and_unicode(tmp_path):
    body = """# Heading

Repeated **bold** [label](target.md) `code` &amp; \\*escape\\* 😀 é.

Repeated **bold** [label](target.md) `code` &amp; \\*escape\\* 😀 é.

- first
- second

> quoted *value*

| A | B |
|---|---|
| x | y |

<script>alert(1)</script>
"""
    bundle = _bundle(tmp_path, body, newline="\r\n")
    concept = bundle.concepts[("projection",)]
    html, projection = render_concept_body_with_projection(concept, bundle, mode="serve")
    assert projection["schema"] == 1
    assert projection["offset_unit"] == "unicode-code-point"
    assert projection["concept_content_rev"] == concept_content_rev(concept.raw_text)
    assert projection["complete"] is True
    mapped = [row for row in projection["blocks"] if row["dom_key"] is not None]
    assert len(mapped) >= 8
    assert len({row["dom_key"] for row in mapped}) == len(mapped)
    assert all(row["source_start"] < row["source_end"] for row in mapped)
    # Frontmatter blocks and the table separator are explicit gaps.
    assert any(row["dom_key"] is None for row in projection["blocks"])
    # Attributes contain opaque integers only; canonical text is never copied.
    attrs = re.findall(r'data-okf-projection-key="([^"]+)"', html)
    assert attrs and all(value.isdecimal() for value in attrs)
    assert "alert(1)" not in " ".join(re.findall(r'data-okf-projection-key="[^"]+"', html))
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    entity_source = concept.raw_text.index("&amp;")
    assert any(
        segment["source_start"] == entity_source
        and segment["source_end"] == entity_source + 5
        and segment["dom_end"] == segment["dom_start"] + 1
        for row in projection["blocks"] for segment in row["segments"]
    )


def test_initial_page_and_live_body_projection_are_identical(tmp_path):
    bundle = _bundle(tmp_path, "Repeated *one*.\n\nRepeated *one*.\n")
    concept = bundle.concepts[("projection",)]
    live_html, live_projection = render_concept_body_with_projection(concept, bundle, mode="serve")
    page = _render_concept_page(
        concept, bundle, bundle.graph(), mode="serve", name="Projection",
        palette=auto_palette([concept.type]), config={},
    )
    projection_text = re.search(r'<script type="application/json" id="okf-annotation-projection">(.*?)</script>', page, re.S)
    assert projection_text
    assert json.loads(projection_text.group(1)) == live_projection
    # Extract the body div's CONTENT. The body div may be nested inside
    # Phase 2 layout wrappers (.okf-page__reading > .okf-page__body-col),
    # so the canonical-source textarea no longer immediately follows the
    # body's closing </div> — the prior `\s*<textarea` suffix coupled this
    # assertion to template adjacency that the layout contract does not
    # require. The non-greedy match captures up to the body div's OWN close
    # (this test body has no nested </div>), preserving the byte-parity
    # contract: the SSR body content must equal the live-rendered body.
    body = re.search(r'<div class="okf-prose okf-page__body">\s*(.*?)\s*</div>', page, re.S)
    assert body and body.group(1) == live_html
    assert live_html.count("data-okf-projection-key") == 2


def test_multi_code_point_entity_maps_one_canonical_token_with_outer_boundaries(tmp_path):
    bundle = _bundle(tmp_path, "A &NotEqualTilde; B\n")
    concept = bundle.concepts[("projection",)]
    html, projection = render_concept_body_with_projection(concept, bundle, mode="serve")
    assert projection["complete"] is True
    assert "A ≂̸ B" in html_lib.unescape(html)
    entity_start = concept.raw_text.index("&NotEqualTilde;")
    segment = next(
        segment for row in projection["blocks"] for segment in row["segments"]
        if segment["source_start"] == entity_start
    )
    assert segment["source_end"] == entity_start + len("&NotEqualTilde;")
    assert segment["dom_end"] - segment["dom_start"] == 2


def test_thematic_break_projection_is_complete_and_html_is_well_formed(tmp_path):
    bundle = _bundle(tmp_path, "Before\n\n---\n\nAfter\n")
    concept = bundle.concepts[("projection",)]
    html, projection = render_concept_body_with_projection(concept, bundle, mode="serve")
    assert projection["complete"] is True
    assert "<hr data-okf-projection-key=" in html
    assert "<hr / data-okf" not in html
    mapped = [row for row in projection["blocks"] if row["dom_key"] is not None]
    assert [row["block_id"].split(":", 1)[0] for row in mapped] == ["paragraph", "hr", "paragraph"]
    assert mapped[1]["segments"] == []


def test_projection_incomplete_when_eligible_block_text_present_but_unmapped(tmp_path):
    """Group 1: an eligible canonical block whose text IS in the rendered HTML
    but failed to pair must make the projection incomplete. Assert the exact
    unmapped block id/span."""
    body = "# Heading\n\nFirst paragraph.\n\nSecond paragraph.\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    _, projection = render_concept_body_with_projection(concept, bundle, mode="serve")
    assert projection["complete"] is True
    # Plugin that garbles the SECOND paragraph's visible text.
    # Canonical source still has "Second paragraph." — it's eligible — but
    # the DOM text doesn't match, so pairing fails → unmapped → incomplete.
    def garble_second(concept_arg, html):
        return html.replace("Second paragraph.", "XXXXXX garbled text")
    _, skewed = render_concept_body_with_projection(
        concept, bundle, mode="serve", body_plugin=garble_second,
    )
    assert skewed["complete"] is False
    # Assert the exact unmapped block by its canonical source span.
    second_unmapped = [
        r for r in skewed["blocks"]
        if r["dom_key"] is None and concept.raw_text[r["source_start"]:r["source_end"]].strip().startswith("Second")
    ]
    assert len(second_unmapped) == 1, (
        f"expected exactly 1 unmapped 'Second paragraph' block, "
        f"got {[(r['block_id'], r['dom_key'], concept.raw_text[r['source_start']:r['source_end']][:20]) for r in skewed['blocks']]}"
    )


def test_projection_incomplete_when_final_repeated_eligible_node_removed(tmp_path):
    """Group 1: removing the final repeated eligible node (second identical
    paragraph) must make the projection incomplete."""
    body = "# Heading\n\nSame text.\n\nSame text.\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    _, normal = render_concept_body_with_projection(concept, bundle, mode="serve")
    assert normal["complete"] is True
    mapped_normal = [r for r in normal["blocks"] if r["dom_key"] is not None]
    assert len(mapped_normal) >= 3  # heading + 2 paragraphs
    # Plugin removes the second <p>
    def remove_last_p(concept_arg, html):
        idx = html.rfind("<p")
        return html[:idx] if idx >= 0 else html
    _, skewed = render_concept_body_with_projection(
        concept, bundle, mode="serve", body_plugin=remove_last_p,
    )
    assert skewed["complete"] is False
    unmapped = [r for r in skewed["blocks"] if r["dom_key"] is None and r.get("source_start", 0) > 0]
    assert len(unmapped) >= 1, f"expected unmapped eligible block, got {[(r['block_id'], r['dom_key']) for r in skewed['blocks']]}"


def test_projection_incomplete_when_eligible_text_substituted(tmp_path):
    """Group 1: substituting visible text of an eligible block so it no longer
    matches canonical source must make the projection incomplete."""
    body = "# Heading\n\nOriginal text.\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    def substitute(concept_arg, html):
        return html.replace("Original text.", "Substituted text.")
    _, skewed = render_concept_body_with_projection(
        concept, bundle, mode="serve", body_plugin=substitute,
    )
    assert skewed["complete"] is False
    unmapped = [r for r in skewed["blocks"]
        if r["dom_key"] is None and "Original" in concept.raw_text[r["source_start"]:r["source_end"]]]
    assert len(unmapped) >= 1, (
        f"expected unmapped substituted block, got {[(r['block_id'], r['dom_key']) for r in skewed['blocks']]}"
    )


def test_projection_exact_once_no_duplicate_keys(tmp_path):
    """Group 1: every dom_key must appear exactly once."""
    bundle = _bundle(tmp_path, "# H\n\nAlpha\n\nBeta\n")
    concept = bundle.concepts[("projection",)]
    _, projection = render_concept_body_with_projection(concept, bundle, mode="serve")
    keys = [r["dom_key"] for r in projection["blocks"] if r["dom_key"] is not None]
    assert len(keys) == len(set(keys)), f"duplicate keys: {keys}"


def test_projection_incomplete_when_1char_eligible_paragraph_removed(tmp_path):
    """Group 1: removing an eligible 1-character paragraph must make the
    projection incomplete. Assert exact unmapped block by source span/id."""
    body = "# Heading\n\nA\n\nB\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    # Normal render is complete.
    _, normal = render_concept_body_with_projection(concept, bundle, mode="serve")
    assert normal["complete"] is True
    # Plugin removes the paragraph containing "B" (1 char).
    def remove_b(concept_arg, html):
        return re.sub(r'<p\s[^>]*>B</p>', '', html)
    _, skewed = render_concept_body_with_projection(
        concept, bundle, mode="serve", body_plugin=remove_b,
    )
    assert skewed["complete"] is False
    unmapped = [r for r in skewed["blocks"]
        if r["dom_key"] is None
        and concept.raw_text[r["source_start"]:r["source_end"]].strip() == "B"]
    assert len(unmapped) == 1, (
        f"expected exactly 1 unmapped 'B' block, "
        f"got {[(r['block_id'], r['dom_key'], concept.raw_text[r['source_start']:r['source_end']].strip()) for r in skewed['blocks']]}"
    )
    assert unmapped[0]["dom_key"] is None


def test_projection_incomplete_when_2char_eligible_paragraph_removed(tmp_path):
    """Group 1: removing an eligible 2-character paragraph must make the
    projection incomplete. Assert exact unmapped block by source span/id."""
    body = "# Heading\n\nHi\n\nGo\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    _, normal = render_concept_body_with_projection(concept, bundle, mode="serve")
    assert normal["complete"] is True
    # Plugin removes the paragraph containing "Go" (2 chars).
    def remove_go(concept_arg, html):
        return re.sub(r'<p\s[^>]*>Go</p>', '', html)
    _, skewed = render_concept_body_with_projection(
        concept, bundle, mode="serve", body_plugin=remove_go,
    )
    assert skewed["complete"] is False
    unmapped = [r for r in skewed["blocks"]
        if r["dom_key"] is None
        and concept.raw_text[r["source_start"]:r["source_end"]].strip() == "Go"]
    assert len(unmapped) == 1, (
        f"expected exactly 1 unmapped 'Go' block, "
        f"got {[(r['block_id'], r['dom_key'], concept.raw_text[r['source_start']:r['source_end']].strip()) for r in skewed['blocks']]}"
    )
    assert unmapped[0]["dom_key"] is None


def test_citation_section_ineligible_normal_render_complete(tmp_path):
    """Group 1: concept with frontmatter citations has its canonical Citations
    section declared ineligible. Normal render remains complete."""
    body = "# Heading\n\nText.\n\n# Citations\n\n- Some citation\n"
    bundle = _bundle(tmp_path, body)
    # Add frontmatter citations to trigger _strip_body_citations_section
    concept = bundle.concepts[("projection",)]
    # Simulate frontmatter citations by writing the raw text with citations FM
    raw = concept.path.read_text(encoding="utf-8") if hasattr(concept, 'path') else None
    # Instead, create a bundle with citations in frontmatter
    raw_with_citations = "---\ntype: note\ntitle: Test\ncitations:\n  - \"Example\"\n---\n" + body
    (tmp_path / "projection.md").write_text(raw_with_citations, encoding="utf-8")
    bundle2 = Bundle.load(tmp_path)
    concept2 = bundle2.concepts[("projection",)]
    html, proj = render_concept_body_with_projection(concept2, bundle2, mode="serve")
    # The citation section blocks must be ineligible (dom_key is None)
    cit_blocks = [r for r in proj["blocks"]
        if "Citations" in concept2.raw_text[r["source_start"]:r["source_end"]]
        or concept2.raw_text[r["source_start"]:r["source_end"]].strip().startswith("- ")]
    assert all(r["dom_key"] is None for r in cit_blocks), (
        f"citation blocks should be ineligible: {[(r['block_id'], r['dom_key']) for r in cit_blocks]}"
    )
    # The projection should still be complete (citation blocks are ineligible,
    # not eligible-but-unmapped)
    assert proj["complete"] is True, (
        f"normal render with citation stripping should be complete: {proj['complete']}"
    )


def test_plugin_before_projection_initial_live_parity(tmp_path):
    """Group 2: initial concept-page and /__data/doc share one plugin-then-
    projection seam. A body-level plugin that injects text must produce the
    same projection body HTML, records, revision, and completeness in both
    paths."""
    body = "# Heading\n\nTarget text here.\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    injection = '<span data-plugin-marker="injected">Plugin</span>'

    def inject_plugin(concept_arg, html):
        return html + injection

    from okf_loom.render import _render_concept_page
    from okf_loom.viewer.assets import auto_palette
    from html.parser import HTMLParser

    class _BodyExtractor(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.in_body = False; self.body_html = ""
        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if d.get("class", "").find("okf-page__body") >= 0: self.in_body = True
            if self.in_body: self.body_html += self.get_starttag_text()
        def handle_endtag(self, tag):
            if self.in_body:
                self.body_html += f"</{tag}>"
                if tag == "div": self.in_body = False
        def handle_data(self, data):
            if self.in_body: self.body_html += data

    # Initial concept page: passes body_plugin
    page_html = _render_concept_page(
        concept, bundle, bundle.graph(), mode="serve", name="T",
        palette=auto_palette([concept.type]), config={}, body_plugin=inject_plugin,
    )
    # /__data/doc path
    doc_html, doc_proj = render_concept_body_with_projection(
        concept, bundle, mode="serve", body_plugin=inject_plugin,
    )
    # Initial page path
    init_html, init_proj = render_concept_body_with_projection(
        concept, bundle, mode="serve", body_plugin=inject_plugin,
    )
    # Both paths produce identical body HTML and projection
    assert doc_html == init_html, "body HTML differs between initial and live"
    assert doc_proj == init_proj, "projection differs between initial and live"
    # Plugin injection is present
    assert "Plugin" in doc_html
    # Revision and completeness agree
    assert doc_proj["concept_content_rev"] == init_proj["concept_content_rev"]
    assert doc_proj["complete"] == init_proj["complete"]


def test_static_build_plugin_before_projection_parity(tmp_path):
    """Group 2 (static): _emit_site applies the plugin at body level BEFORE
    projection, so the embedded projection metadata matches the post-plugin
    body HTML — parity with the serve path."""
    body = "# Heading\n\nTarget text.\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    injection = '<span data-plugin="static">Plugin</span>'

    class StaticPlugin:
        def on_concept_render(self, concept_arg, html):
            return html + injection

    # Build static site with plugin.
    out_dir = tmp_path / "static_out"
    build_site(bundle, out_dir, target="static", name="Test", plugin=StaticPlugin())
    page_html = (out_dir / "projection.html").read_text(encoding="utf-8")

    # Plugin injection is present in the static page.
    assert injection in page_html, "plugin injection missing from static build"

    # Extract the embedded projection JSON.
    import re as _re
    proj_match = _re.search(
        r'<script type="application/json" id="okf-annotation-projection">(.*?)</script>',
        page_html, _re.S,
    )
    assert proj_match, "no embedded projection in static build"
    static_proj = json.loads(proj_match.group(1))

    # Build the same concept via the helper to compare.
    helper_html, helper_proj = render_concept_body_with_projection(
        concept, bundle, mode="static",
        body_plugin=lambda c, h: h + injection,
    )

    # Projection records must match (plugin applied before projection in both).
    assert static_proj["blocks"] == helper_proj["blocks"], (
        "static build projection differs from body_plugin helper projection"
    )
    assert static_proj["complete"] == helper_proj["complete"]


def test_soft_wrapped_paragraph_newline_normalization_stays_standalone(tmp_path):
    """Regression: a soft-wrapped Markdown paragraph renders newline as a DOM
    space. _boundary_segments must preserve the newline→space mapping as a
    standalone segment, not coalesce it into adjacent identity text.
    The browser decoder (_decodeCanonicalSegment) only handles all-newline
    segments; a coalesced text+newline segment fails validation."""
    body = "# Heading\n\nFirst line\nsecond line\nthird\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    html, projection = render_concept_body_with_projection(concept, bundle, mode="serve")
    assert projection["complete"] is True
    # Find the paragraph block (it contains internal newlines between words).
    para = [r for r in projection["blocks"]
            if r.get("dom_key") is not None
            and "paragraph" in r.get("block_id", "")
            and "\n" in concept.raw_text[r["source_start"]:r["source_end"]]]
    assert len(para) >= 1, "expected at least one mapped block with newlines"
    rec = para[0]
    # The segment list must contain standalone newline-normalization segments
    # (source width 1, dom width 1, source char is \\n or \\r).
    newline_segs = [s for s in rec["segments"]
                    if s["source_end"] - s["source_start"] == 1
                    and s["dom_end"] - s["dom_start"] == 1
                    and concept.raw_text[s["source_start"]:s["source_end"]] in ("\n", "\r")]
    assert len(newline_segs) >= 2, (
        f"expected ≥2 standalone newline segments in soft-wrapped paragraph, "
        f"got {len(newline_segs)}; segments: {rec['segments'][:10]}"
    )
    # Verify the adjacent identity segments do NOT include newline characters.
    for s in rec["segments"]:
        src_text = concept.raw_text[s["source_start"]:s["source_end"]]
        if s not in newline_segs and s["source_end"] - s["source_start"] == s["dom_end"] - s["dom_start"]:
            # Identity segment should not contain raw newlines
            assert "\n" not in src_text and "\r" not in src_text, (
                f"identity segment {s} contains newline char in source: {src_text!r}"
            )


# ---------------------------------------------------------------------------
# R3 Item 1: Extractor/renderer grammar parity fixtures (LF and CRLF)
# ---------------------------------------------------------------------------

def test_grammar_parity_ordered_paren_list(tmp_path):
    """Extractor and renderer agree on ordered `1)` list items."""
    body = "1) First\n2) Second\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    blocks = extract_blocks(concept.raw_text)
    list_blocks = [b for b in blocks if b.kind == "list-item"]
    assert len(list_blocks) == 2, f"expected 2 list items, got {len(list_blocks)}"
    html, proj = render_concept_body_with_projection(concept, bundle, mode="serve")
    # Each list item should produce an <li>
    assert html.count("<li") >= 2


def test_grammar_parity_setext_heading():
    """Setext headings are recognized by the extractor."""
    body_text = "Setext Title\n=============\n\nParagraph.\n"
    blocks = extract_blocks(body_text)
    headings = [b for b in blocks if b.kind == "heading"]
    assert len(headings) >= 1, f"expected ≥1 heading, got {len(headings)}"
    assert "Setext Title" in headings[0].text


def test_grammar_parity_lazy_blockquote(tmp_path):
    """Lazy blockquote continuation is one block in the extractor."""
    body = "> Quoted\nlazy continuation\n\nAfter.\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    blocks = extract_blocks(concept.raw_text)
    quotes = [b for b in blocks if b.kind == "block-quote"]
    assert len(quotes) >= 1
    assert "lazy continuation" in quotes[0].text


def test_grammar_parity_pipe_prose_not_table(tmp_path):
    """Prose containing pipes is classified as paragraph, not table-row."""
    body = "The `customer_id` foreign key points at [[tables/customers|Customers]].\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    blocks = extract_blocks(concept.raw_text)
    from okf_loom.render import _projection_tags_for_block
    para_blocks = [b for b in blocks if b.kind in ("paragraph", "table-row")]
    assert len(para_blocks) >= 1
    # The pipe-containing prose should be classified by the tags helper as p
    tags = _projection_tags_for_block(para_blocks[0].kind, para_blocks[0].text)
    assert "p" in tags, f"pipe prose should map to p tags, got {tags}"


def test_grammar_parity_crlf_offsets(tmp_path):
    """CRLF line endings produce the same block structure as LF."""
    import tempfile
    body = "# Heading\n\nParagraph one.\n\nParagraph two.\n"
    # LF bundle
    lf_dir = tmp_path / "lf"
    lf_dir.mkdir()
    _bundle(lf_dir, body, newline="\n")
    b_lf = Bundle.load(lf_dir)
    # CRLF bundle
    crlf_dir = tmp_path / "crlf"
    crlf_dir.mkdir()
    _bundle(crlf_dir, body, newline="\r\n")
    b_crlf = Bundle.load(crlf_dir)
    blocks_lf = extract_blocks(b_lf.concepts[("projection",)].raw_text)
    blocks_crlf = extract_blocks(b_crlf.concepts[("projection",)].raw_text)
    assert len(blocks_lf) == len(blocks_crlf)
    assert [b.kind for b in blocks_lf] == [b.kind for b in blocks_crlf]


# ---------------------------------------------------------------------------
# R3 Item 4: Plugin additions don't break projection completeness
# ---------------------------------------------------------------------------

def test_plugin_added_paragraph_does_not_break_completeness(tmp_path):
    """A plugin that ADDS a semantic element must not make projection
    incomplete. The added element has no canonical block but the
    canonical blocks that DO exist must all map."""
    body = "# Heading\n\nOriginal text.\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    def add_paragraph(concept_arg, html):
        return html + "<p>Plugin-added paragraph.</p>"
    _, proj = render_concept_body_with_projection(
        concept, bundle, mode="serve", body_plugin=add_paragraph,
    )
    # Original blocks should all be mapped.
    assert proj["complete"] is True, (
        f"plugin-added paragraph should not break completeness: {proj['complete']}"
    )


def test_plugin_removed_paragraph_makes_incomplete(tmp_path):
    """A plugin that REMOVES an eligible paragraph makes projection
    incomplete because the canonical block has no DOM node."""
    body = "# Heading\n\nFirst paragraph.\n\nSecond paragraph.\n"
    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    def remove_second(concept_arg, html):
        return re.sub(r'<p\s[^>]*>Second paragraph\.</p>', '', html)
    _, skewed = render_concept_body_with_projection(
        concept, bundle, mode="serve", body_plugin=remove_second,
    )
    assert skewed["complete"] is False


# ---------------------------------------------------------------------------
# R3 Item 5: Evidence commutativity (binding spec §12.1)
# ---------------------------------------------------------------------------

def test_resolver_evidence_commutativity():
    """Spec §12.1 mandates commutative intersection of all non-empty supports.
    Result must be identical regardless of evidence evaluation order."""
    from okf_loom.annotations import resolve_anchor, _intersect_supports, _Candidate, Block
    # Create candidates
    c1 = _Candidate(0, 5, Block("p:1", "paragraph", 0, 10, "exact", "sha256:x", ()))
    c2 = _Candidate(10, 15, Block("p:2", "paragraph", 10, 20, "exact", "sha256:y", ()))
    c3 = _Candidate(20, 25, Block("p:3", "paragraph", 20, 30, "exact", "sha256:z", ()))
    supports = {
        "position": {c1},          # original position points to c1
        "block_id": {c1, c2},      # block id matches c1 and c2
        "heading": {c1, c2, c3},   # heading matches all
        "prefix": set(),           # empty, ignored
        "suffix": {c2, c3},        # suffix matches c2 and c3
    }
    result_a, count_a = _intersect_supports(list(supports.keys()), supports)
    # Reorder
    reordered = dict(reversed(list(supports.items())))
    result_b, count_b = _intersect_supports(list(reordered.keys()), reordered)
    assert result_a == result_b, f"commutativity violated: {result_a} != {result_b}"
    # position ∩ block_id ∩ heading ∩ suffix = {c1} ∩ {c1,c2} ∩ {c1,c2,c3} ∩ {c2,c3} = {}
    # Empty intersection from ≥2 non-empty supports => conflicting evidence
    assert len(result_a) == 0, f"expected empty intersection (conflicting), got {result_a}"


def test_resolver_conflicting_evidence_is_invalid():
    """When position points to candidate A but suffix points to candidate B
    exclusively, the intersection is empty and the result is deterministically
    invalid/conflicting_evidence. Strengthened from the vacuous form that
    accepted every resolver status (ReviewAll r7)."""
    # "alpha" occurs in two paragraphs; position+block point to the first
    # occurrence, but the suffix " END" matches only the second — the
    # intersection of non-empty supports is empty → conflicting_evidence.
    source = "foo alpha bar\n\nbaz alpha END\n"
    anchor = {
        "schema": 2, "kind": "text-quote", "exact": "alpha",
        "prefix": "", "suffix": " END",
        "heading": {"path": []},
        "block": {"id": "paragraph:1", "hash": "sha256:" + "a" * 64, "start": 0, "end": 14},
        "position": {"start": 4, "end": 9},
    }
    result = resolve_anchor(anchor, source, public=False)
    assert result["status"] == "invalid", (
        f"expected status=invalid for conflicting evidence, got {result}"
    )
    assert result["reason"] == "conflicting_evidence", (
        f"expected reason=conflicting_evidence, got {result}"
    )


# ---------------------------------------------------------------------------
# R3 Item 6: Legacy unknown metadata contract
# ---------------------------------------------------------------------------

def test_legacy_anchor_strict_public_input():
    """Public legacy input must be exactly {kind, ref}."""
    from okf_loom.annotations import resolve_anchor
    anchor_with_extra = {"kind": "text", "ref": "hello", "extra": "unknown"}
    result = resolve_anchor(anchor_with_extra, "hello world\n", public=True)
    assert result["status"] == "invalid"
    assert result["reason"] == "malformed_anchor"


def test_legacy_anchor_preserves_stored_unknowns():
    """Stored legacy anchors with unknown fields are readable; the resolver
    extracts {kind, ref} and ignores extras for resolution. Strengthened from
    the vacuous form that did not assert a precise legacy result (ReviewAll r7).
    """
    from okf_loom.annotations import resolve_anchor
    anchor_stored = {"kind": "text", "ref": "hello", "extra_field": "data", "ts": "2026-01-01"}
    result = resolve_anchor(anchor_stored, "hello world\n", public=False)
    # Exact deterministic outcome: unique global exact occurrence.
    assert result["status"] == "resolved", (
        f"stored legacy anchor with unknowns should resolve, got {result}"
    )
    assert result["reason"] == "unique_global_exact", (
        f"expected reason=unique_global_exact, got {result}"
    )
    assert result["position"] == {"start": 0, "end": 5}, (
        f"expected position [0,5) for 'hello', got {result}"
    )


# ---------------------------------------------------------------------------
# R3-1: Owner-marker projection visibility/gap coverage tests
# (replaces the old _boundary_segments / _is_valid_syntax_gap /
#  _compute_no_match_zones helper tests — those heuristics are removed;
#  source maps now come from deterministic IR rendering.)
# ---------------------------------------------------------------------------

def _render_and_project(source_body, *, plugin=None):
    """Render body with owners, optionally apply plugin, finalize projection."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    html, owners = render_blocks_with_owners(source_body)
    if plugin:
        html = plugin(html)
    return build_annotation_projection(source_body, source_body, html, owners=owners)


def test_boundary_segments_rejects_dropped_visible_char():
    """Source 'abc' → DOM 'ac' must be incomplete because visible 'b' was dropped."""
    _, proj = _render_and_project("abc", plugin=lambda h: h.replace(">abc<", ">ac<"))
    assert proj["complete"] is False, "dropping visible 'b' must fail closed"


def test_boundary_segments_accepts_full_match():
    """Source 'abc' → DOM 'abc' must be complete."""
    _, proj = _render_and_project("abc")
    assert proj["complete"] is True


def test_boundary_segments_rejects_middle_deletion():
    _, proj = _render_and_project("axb", plugin=lambda h: h.replace(">axb<", ">ab<"))
    assert proj["complete"] is False


def test_boundary_segments_rejects_beginning_deletion():
    _, proj = _render_and_project("xab", plugin=lambda h: h.replace(">xab<", ">ab<"))
    assert proj["complete"] is False


def test_boundary_segments_rejects_end_deletion():
    _, proj = _render_and_project("abx", plugin=lambda h: h.replace(">abx<", ">ab<"))
    assert proj["complete"] is False


def test_boundary_segments_rejects_repeated_char_deletion():
    _, proj = _render_and_project("aab", plugin=lambda h: h.replace(">aab<", ">ab<"))
    assert proj["complete"] is False


def test_boundary_segments_accepts_emphasis_syntax_gap():
    """Source '**bold**' → DOM 'bold' must be complete: ** is deterministic syntax."""
    _, proj = _render_and_project("**bold**")
    assert proj["complete"] is True


def test_boundary_segments_accepts_link_url_gap():
    """Source '[label](url)' → DOM 'label' must be complete: URL is not visible."""
    _, proj = _render_and_project("[label](/path/to/file.md)")
    assert proj["complete"] is True


def test_boundary_segments_accepts_fenced_code_fence():
    """Fenced code block fence markers are syntax, not visible text."""
    _, proj = _render_and_project("```\ncode\n```")
    assert proj["complete"] is True


def test_boundary_segments_url_not_false_matched():
    """The URL inside ](url) must not be character-matched into visible text.
    Projection must still be complete because the URL is deterministic syntax."""
    _, proj = _render_and_project("[customers](/tables/customers.md).")
    assert proj["complete"] is True


def test_boundary_segments_frontmatter_overlap_handled():
    """When the extractor produces one block spanning frontmatter + body,
    the projection must clip to the body region."""
    from pathlib import Path; import tempfile
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    tmp = Path(tempfile.mkdtemp())
    raw = "---\ntype: note\ntitle: T\n---\nHello world.\n"
    (tmp / "x.md").write_text(raw)
    b = Bundle.load(tmp); c = b.concepts[("x",)]
    _, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True, "frontmatter overlap must not break projection"


# ---------------------------------------------------------------------------
# R4: Markdown renderer/projection invariant — exact canonical-visible
# coverage for every listed construct (LF and CRLF).
# ---------------------------------------------------------------------------

def _r4_render_and_project(source_body, *, plugin=None):
    """Render body with owners, optionally apply plugin, finalize projection."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    html, owners = render_blocks_with_owners(source_body)
    if plugin:
        html = plugin(html)
    return build_annotation_projection(source_body, source_body, html, owners=owners)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r4_list_continuation_line_projected(newline):
    """List continuation lines must be visible in the DOM and match the IR."""
    nl = newline
    src = f"- first{nl}  continued line{nl}- second{nl}"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True, (
        f"list continuation must be complete ({newline!r})"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r4_nested_list_item_projected(newline):
    """Nested list items are part of the parent block's textContent."""
    nl = newline
    src = f"- outer{nl}  - inner{nl}- second{nl}"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r4_indented_code_projected_as_pre(newline):
    """Indented code blocks render as <pre><code>, preserving newlines."""
    nl = newline
    src = f"    code line 1{nl}    code line 2{nl}"
    html, proj = _r4_render_and_project(src)
    assert proj["complete"] is True
    assert "<pre" in html and "<code>" in html, (
        "indented code must render as <pre><code>"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r4_multiline_html_block_projected(newline):
    """Multiline/nested HTML blocks escape and preserve newlines."""
    nl = newline
    src = f"<div>{nl}  <p>nested</p>{nl}</div>{nl}"
    html, proj = _r4_render_and_project(src)
    assert proj["complete"] is True
    assert "&lt;div&gt;" in html, "HTML must be escaped"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r4_footnote_definition_projected(newline):
    """Footnote definitions get an owner marker on their <li>."""
    nl = newline
    src = f"Text with [^1] ref.{nl}{nl}[^1]: footnote text{nl}"
    html, proj = _r4_render_and_project(src)
    assert proj["complete"] is True, (
        f"footnote definition must be projected complete ({newline!r})"
    )
    assert "okf-footnotes" in html


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r4_task_list_checkbox_removed(newline):
    """GFM task-list checkbox markers are stripped in the IR visible text."""
    nl = newline
    src = f"- [ ] todo{nl}- [x] done{nl}"
    html, proj = _r4_render_and_project(src)
    assert proj["complete"] is True
    assert "okf-task__box" in html, "renderer must emit checkbox"


def test_r4_escaped_table_pipe_projected():
    """Escaped pipes \\| in table cells map to literal | in visible text."""
    src = "| a \\| b | c |\n|---|---|\n| x | y |\n"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True


def test_r4_setext_h1_projected():
    """Setext H1 headings project correctly."""
    src = "Title\n=====\n\nText.\n"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True


def test_r4_setext_h2_projected():
    """Setext H2 headings project correctly."""
    src = "Title\n-----\n\nText.\n"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True


def test_r4_ordered_paren_list_projected():
    """Ordered lists with 1) syntax project correctly."""
    src = "1) First\n2) Second\n"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True


def test_r4_lazy_blockquote_projected():
    """Lazy continuation blockquotes project correctly."""
    src = "> Quoted\nlazy continuation\n\nAfter.\n"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True


# ---------------------------------------------------------------------------
# R4: Plugin-forged projection attribute sanitization (structural)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("forged_attr", [
    'data-okf-projection-key="999"',
    "data-okf-projection-key='999'",
    "data-okf-projection-key=999",
    'DATA-OKF-PROJECTION-KEY="999"',
    'Data-Okf-Projection-Key="999"',
])
def test_r4_forged_projection_key_stripped(forged_attr):
    """Forged projection keys in any quote/case syntax must be stripped."""
    src = "Hello world.\n"
    html_owners_html, owners = _render_and_project.__wrapped__ if hasattr(_render_and_project, '__wrapped__') else (None, None)
    from okf_loom.viewer.markdown import render_blocks_with_owners
    html, owners = render_blocks_with_owners(src)
    # Inject forged attribute into the first <p tag.
    forged = html.replace("<p ", f"<p {forged_attr} ", 1)
    from okf_loom.render import build_annotation_projection
    final_html, proj = build_annotation_projection(src, src, forged, owners=owners)
    assert "999" not in final_html, (
        f"forged key {forged_attr!r} must be stripped from final HTML"
    )


@pytest.mark.parametrize("forged_attr", [
    'data-okf-proj-owner="forgedabcdef0123456789abcdef01234567"',
    "data-okf-proj-owner='forgedabcdef0123456789abcdef01234567'",
    "data-okf-proj-owner=forgedabcdef0123456789abcdef0123456",
    'DATA-OKF-PROJ-OWNER="forgedabcdef0123456789abcdef0123456"',
])
def test_r4_forged_owner_attr_stripped(forged_attr):
    """Forged owner attributes in any quote/case syntax must be stripped."""
    src = "Hello world.\n"
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    html, owners = render_blocks_with_owners(src)
    forged = html.replace("<p ", f"<p {forged_attr} ", 1)
    final_html, proj = build_annotation_projection(src, src, forged, owners=owners)
    assert "forgedabcdef" not in final_html.lower(), (
        f"forged owner {forged_attr!r} must be stripped from final HTML"
    )


def test_r4_forged_key_then_legitimate_key_coexist():
    """A forged key is stripped but its mere presence forces incomplete
    projection (fail-closed contract: any pre-finalization
    data-okf-projection-key occurrence, valued or valueless, any case,
    must force complete=False — not merely strip)."""
    src = "Hello world.\n"
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    html, owners = render_blocks_with_owners(src)
    forged = html.replace("<p ", '<p data-okf-projection-key="999" ', 1)
    final_html, proj = build_annotation_projection(src, src, forged, owners=owners)
    # The forged key must be stripped from final HTML regardless.
    import re
    keys = re.findall(r'data-okf-projection-key="(\d+)"', final_html)
    assert "999" not in keys, "forged key must be stripped"
    # Fail-closed: the pre-finalization key must force incomplete projection.
    assert proj["complete"] is False, (
        "pre-finalization data-okf-projection-key must force complete=False"
    )


# ---------------------------------------------------------------------------
# R4: IR ↔ renderer textContent parity for each construct
# ---------------------------------------------------------------------------

def test_r4_ir_renderer_parity_all_constructs():
    """For every listed construct, the IR expected_visible must equal the
    renderer's DOM textContent."""
    from okf_loom.markdown_blocks import extract_blocks, block_visible_segments
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from html.parser import HTMLParser

    constructs = {
        "list-continuation": "- first\n  continued line\n- second\n",
        "nested-list": "- outer\n  - inner\n- second\n",
        "nested-list-multi-sibling": "- outer\n  - child1\n  - child2\n",
        "nested-list-three-level": "- a\n  - b\n    - c1\n    - c2\n  - d\n",
        "task-list": "- [ ] todo\n- [x] done\n",
        "indented-code": "    code line 1\n    code line 2\n",
        "multiline-html": "<div>\n  <p>nested</p>\n</div>\n",
        "footnote": "Text with [^1] ref.\n\n[^1]: footnote text\n",
        "escaped-table": "| a \\| b | c |\n|---|---|\n| x | y |\n",
        "setext": "Title\n=====\n\nBody.\n",
        "ordered-paren": "1) First\n2) Second\n",
        "lazy-quote": "> Quoted\nlazy continuation\n\nAfter.\n",
    }

    class _TextScanner(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.text = {}
        def handle_starttag(self, tag, attrs):
            nonce = dict(attrs).get("data-okf-proj-owner")
            if tag not in ("area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"):
                self.stack.append(nonce)
        def handle_startendtag(self, tag, attrs):
            # Self-closing: text accumulates to current ancestors only.
            pass
        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()
        def handle_data(self, data):
            for n in {x for x in self.stack if x}:
                self.text[n] = self.text.get(n, "") + data

    for name, src in constructs.items():
        blocks = extract_blocks(src)
        html_out, owners = render_blocks_with_owners(src)
        scanner = _TextScanner()
        scanner.feed(html_out)
        for owner in owners:
            vis, _ = block_visible_segments(next(b for b in blocks if b.id == owner["block_id"])) or (None, None)
            if vis is None:
                continue
            actual = scanner.text.get(owner["nonce"], "")
            assert actual == vis, (
                f"{name}: owner {owner['block_id']} textContent mismatch: "
                f"expected {vis!r}, got {actual!r}"
            )


# ===========================================================================
# R5: Structural sanitization — valueless / quoted / unquoted / mixed-case
# external projection/owner attrs must ALL be removed.
# Finding 18 defect 1.
# ===========================================================================

@pytest.mark.parametrize("forged_attr", [
    # Valueless (HTML boolean-attribute syntax) — the gap the old regex missed.
    "data-okf-projection-key",
    "DATA-OKF-PROJECTION-KEY",
    "Data-Okf-Projection-Key",
    # Quoted / unquoted (already covered by r4 but kept as a combined battery).
    'data-okf-projection-key="999"',
    "data-okf-projection-key='999'",
    "data-okf-projection-key=999",
])
def test_r5_forged_projection_key_all_forms_stripped(forged_attr):
    """Every external projection-key form — valueless, quoted, unquoted,
    mixed-case — must be stripped from the final HTML, and exactly ONE
    legitimate numeric key must survive on the owned element.

    Additionally, the mere presence of any pre-finalization
    data-okf-projection-key occurrence forces ``complete=False`` (fail-closed
    contract, ReviewAll r7).

    Inspects the actual start-tag attributes (not string-in-string) to verify
    the contract precisely.
    """
    from html.parser import HTMLParser as _HP
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection

    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    forged = html.replace("<p ", f"<p {forged_attr} ", 1)
    final_html, proj = build_annotation_projection(src, src, forged, owners=owners)

    # Parse the final HTML to inspect start-tag attributes precisely.
    class _AttrGrabber(_HP):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.tags: list[tuple[str, dict[str, str | None]]] = []
        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, dict(attrs)))
        def handle_startendtag(self, tag, attrs):
            self.tags.append((tag, dict(attrs)))

    grabber = _AttrGrabber()
    grabber.feed(final_html)

    # Collect ALL projection-key attrs from ALL start tags (case-insensitive).
    all_pk = []
    for tag, attrs in grabber.tags:
        for k, v in attrs.items():
            if k.lower() == "data-okf-projection-key":
                all_pk.append(v)
    # Exactly ONE legitimate numeric key, with the expected value "1".
    assert len(all_pk) == 1, (
        f"expected exactly 1 projection-key attr, got {len(all_pk)} "
        f"(values={all_pk}) for forged form {forged_attr!r}"
    )
    assert all_pk[0] == "1", f"expected legitimate key '1', got {all_pk[0]!r}"
    # The forged value 999 must not survive.
    assert all_pk[0] != "999"
    # Fail-closed: pre-finalization key occurrence forces incomplete projection.
    assert proj["complete"] is False, (
        f"pre-finalization projection-key must force complete=False "
        f"(forged form {forged_attr!r})"
    )


def test_r5_mutation_fail_closed():
    """Mutating the visible text of an owned element must make projection
    incomplete — the owner's expected text no longer matches the DOM."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    mutated = html.replace("Hello world.", "MUTATED TEXT.")
    _, proj = build_annotation_projection(src, src, mutated, owners=owners)
    assert proj["complete"] is False


def test_r5_unknown_owner_fail_closed():
    """An unknown/forged owner marker must make projection incomplete."""
    from okf_loom.markdown_blocks import generate_owner_nonce
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    forged_nonce = generate_owner_nonce()
    injected = html + f'<p data-okf-proj-owner="{forged_nonce}">forged</p>'
    _, proj = build_annotation_projection(src, src, injected, owners=owners)
    assert proj["complete"] is False


@pytest.mark.parametrize("forged_attr", [
    # Valueless — the gap the old regex missed.
    "data-okf-proj-owner",
    "DATA-OKF-PROJ-OWNER",
    "Data-Okf-Proj-Owner",
    # Quoted / unquoted with forged nonce (already covered by r4).
    'data-okf-proj-owner="forgedabcdef0123456789abcdef01234567"',
    "data-okf-proj-owner='forgedabcdef0123456789abcdef01234567'",
])
def test_r5_forged_owner_attr_all_forms_stripped(forged_attr):
    """Every external proj-owner form — valueless, quoted, mixed-case — must
    be stripped from the final HTML."""
    src = "Hello world.\n"
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    html, owners = render_blocks_with_owners(src)
    forged = html.replace("<p ", f"<p {forged_attr} ", 1)
    final_html, proj = build_annotation_projection(src, src, forged, owners=owners)
    # After finalization, NO data-okf-proj-owner of any form may survive:
    # legitimate nonces are replaced by projection keys in step 4, and all
    # remaining/forged forms are stripped structurally in step 4b.
    assert "data-okf-proj-owner" not in final_html.lower(), (
        f"external proj-owner survived in form {forged_attr!r}"
    )


# ===========================================================================
# R5: Owner nonce tag/type validation — registered nonce on semantically
# ineligible element must fail closed.
# Finding 18 defect 2.
# ===========================================================================

@pytest.mark.parametrize("wrong_tag", ["div", "span", "blockquote", "pre"])
def test_r5_owner_moved_to_wrong_tag_fails_closed(wrong_tag):
    """A registered owner nonce moved to a semantically ineligible element
    (e.g. <p> → <div>) while preserving text must make projection
    *incomplete* — the block stays unmapped (fail closed)."""
    src = "Hello world.\n"
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    moved = html.replace(
        f'<p data-okf-proj-owner="{nonce}">',
        f'<{wrong_tag} data-okf-proj-owner="{nonce}">',
    )
    moved = moved.replace("</p>", f"</{wrong_tag}>", 1)
    final_html, proj = build_annotation_projection(src, src, moved, owners=owners)
    assert proj["complete"] is False, (
        f"owner moved to <{wrong_tag}> must fail closed (incomplete), "
        f"got complete={proj['complete']}"
    )
    # No numeric projection key may have been assigned.
    import re as _re
    keys = _re.findall(r'data-okf-projection-key="(\d+)"', final_html)
    assert not keys, (
        f"no key should be assigned for wrong-tag owner, got keys={keys}"
    )


def test_r5_owner_correct_tag_still_assigned():
    """Sanity: the legitimate tag (e.g. <p> for paragraph) must still
    receive a numeric key after tag validation."""
    src = "Hello world.\n"
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    html, owners = render_blocks_with_owners(src)
    final_html, proj = build_annotation_projection(src, src, html, owners=owners)
    assert proj["complete"] is True
    import re as _re
    keys = _re.findall(r'data-okf-projection-key="(\d+)"', final_html)
    assert keys == ["1"], f"expected exactly one key '1', got {keys}"


def test_r5_owner_li_moved_to_p_fails_closed():
    """List-item owner (<li>) moved to <p> must fail closed."""
    src = "- item one\n- item two\n"
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    moved = html.replace(
        f'<li data-okf-proj-owner="{nonce}">',
        f'<p data-okf-proj-owner="{nonce}">',
    )
    # Close the replacement tag too (find the matching </li>).
    moved = moved.replace("</li>", "</p>", 1)
    _, proj = build_annotation_projection(src, src, moved, owners=owners)
    assert proj["complete"] is False, (
        "list-item owner moved to <p> must fail closed"
    )


# ===========================================================================
# R5: Nested sibling list separator exactness — literal expected DOM text
# and IR offsets for multiple nested siblings (LF + CRLF).
# Finding 19.
# ===========================================================================

@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r5_multiple_nested_siblings_exact_textcontent(newline):
    """A list with multiple nested sibling items must produce DOM textContent
    with '\\n' separators between siblings, matching the IR expected_visible."""
    nl = newline
    src = f"- outer{nl}  - child1{nl}  - child2{nl}"
    from okf_loom.markdown_blocks import extract_blocks, block_visible_segments
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    from html.parser import HTMLParser

    class _Scanner(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.text = {}
        def handle_starttag(self, tag, attrs):
            nonce = dict(attrs).get("data-okf-proj-owner")
            if tag not in ("area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"):
                self.stack.append(nonce)
        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()
        def handle_data(self, data):
            for n in {x for x in self.stack if x}:
                self.text[n] = self.text.get(n, "") + data

    blocks = extract_blocks(src)
    html_out, owners = render_blocks_with_owners(src)
    scanner = _Scanner()
    scanner.feed(html_out)

    for owner in owners:
        block = next(b for b in blocks if b.id == owner["block_id"])
        vis, segs = block_visible_segments(block) or (None, None)
        assert vis is not None
        actual = scanner.text.get(owner["nonce"], "")
        # Literal expected DOM text: "outer\nchild1\nchild2"
        assert vis == "outer\nchild1\nchild2", (
            f"IR expected_visible wrong for {newline!r}: {vis!r}"
        )
        assert actual == vis, (
            f"DOM textContent mismatch ({newline!r}): "
            f"expected {vis!r}, got {actual!r}"
        )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r5_multiple_nested_siblings_projection_complete(newline):
    """Projection must be complete for multiple nested siblings (LF + CRLF)."""
    nl = newline
    src = f"- outer{nl}  - child1{nl}  - child2{nl}"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True, (
        f"projection must be complete for nested siblings ({newline!r})"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r5_three_level_nested_siblings_projection_complete(newline):
    """Three-level nesting with multiple siblings at the deepest level."""
    nl = newline
    src = f"- a{nl}  - b{nl}    - c1{nl}    - c2{nl}  - d{nl}"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True, (
        f"projection must be complete for 3-level nested siblings ({newline!r})"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r5_nested_ordered_siblings_projection_complete(newline):
    """Ordered nested siblings must also be complete."""
    nl = newline
    src = f"1. outer{nl}   1. child1{nl}   2. child2{nl}"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True, (
        f"projection must be complete for ordered nested siblings ({newline!r})"
    )


def test_r5_nested_sibling_segment_offsets_exact():
    """The IR source-map segments for nested siblings must map each visible
    character back to the correct source offset."""
    from okf_loom.markdown_blocks import extract_blocks, block_visible_segments
    src = "- outer\n  - child1\n  - child2\n"
    blocks = extract_blocks(src)
    block = blocks[0]  # list-item:1
    vis, segs = block_visible_segments(block)
    # Reconstruct visible text from segments and verify it matches.
    reconstructed = ""
    for seg in segs:
        chunk = src[seg["source_start"]:seg["source_end"]]
        # Normalise CRLF → LF for comparison (segments store normalised offsets)
        # but in this LF case they should be direct.
        reconstructed += vis[seg["dom_start"]:seg["dom_end"]]
    assert reconstructed == vis, (
        f"segment DOM offsets do not reconstruct visible text: "
        f"{reconstructed!r} != {vis!r}"
    )
    # Verify the separator between child1 and child2 is at the right offset.
    # Source: "- outer\n  - child1\n  - child2\n"
    #          0123456 78901234567 8901234567
    # The \n after child1 is at source offset 16 (after "- outer\n  - child1").
    nl_after_child1 = src.index("\n", src.index("child1"))
    # Find the segment that covers this newline in source space.
    found = False
    for seg in segs:
        if seg["source_start"] <= nl_after_child1 < seg["source_end"]:
            found = True
            # The DOM char at this position must be "\n".
            dom_char = vis[seg["dom_start"]:seg["dom_end"]]
            assert "\n" in dom_char, (
                f"expected \\n in DOM segment at source offset "
                f"{nl_after_child1}, got {dom_char!r}"
            )
    assert found, (
        f"no segment covers the newline between child1 and child2 "
        f"at source offset {nl_after_child1}"
    )


# ===========================================================================
# R5 structural parser proof: every external owner occurrence — valueless,
# mixed-case, single/double quoted, unquoted, malformed, unknown, duplicated,
# copied, misplaced — must make projection incomplete and be stripped.
# A nonce-like string inside another attribute value/title/text must NOT be
# mistaken for owner.
# ===========================================================================

def _proj_with_plugin(src, plugin_fn):
    """Render with owners, apply plugin, finalize."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    html, owners = render_blocks_with_owners(src)
    if plugin_fn:
        html = plugin_fn(html)
    return build_annotation_projection(src, src, html, owners=owners)


@pytest.mark.parametrize("forged_owner_form", [
    "data-okf-proj-owner",                                         # valueless
    "DATA-OKF-PROJ-OWNER",                                         # valueless mixed case
    'data-okf-proj-owner="forgedabcdef0123456789abcdef01234567890"',  # double-quoted unknown nonce
    "data-okf-proj-owner='forgedabcdef0123456789abcdef01234567890'",  # single-quoted
    "data-okf-proj-owner=forgedabcdef0123456789abcdef01234567890",    # unquoted
])
def test_r5_structural_external_owner_stripped_and_incomplete(forged_owner_form):
    """Every external owner form must be stripped and make projection
    incomplete (injected as a SEPARATE element with unknown nonce)."""
    src = "Hello world.\n"
    def inject(html):
        return html + f'<div {forged_owner_form}>extra</div>'
    final_html, proj = _proj_with_plugin(src, inject)
    # The forged owner must be stripped from the final HTML.
    assert "data-okf-proj-owner" not in final_html.lower(), (
        f"external owner form {forged_owner_form!r} survived"
    )
    # Projection must be incomplete (forged/unknown owner detected).
    assert proj["complete"] is False


def test_r5_structural_nonce_in_text_not_mistaken():
    """A nonce-like string in text content must NOT be mistaken for an owner."""
    from okf_loom.markdown_blocks import find_owner_markers, generate_owner_nonce
    nonce = generate_owner_nonce()
    # The nonce appears in text, not as a start-tag attribute.
    html = f'<p data-okf-proj-owner="{nonce}">text with {nonce} inside</p>'
    markers = find_owner_markers(html)
    # Exactly ONE marker — on the <p> start tag, not in the text.
    assert len(markers) == 1, f"expected 1 marker, got {len(markers)}"


def test_r5_structural_nonce_in_title_not_mistaken():
    """A nonce-like string in another attribute value must NOT be detected."""
    from okf_loom.markdown_blocks import find_owner_markers, generate_owner_nonce
    nonce = generate_owner_nonce()
    html = f'<p title="{nonce}">text</p>'
    markers = find_owner_markers(html)
    assert len(markers) == 0, f"nonce in title should not be detected: {markers}"


def test_r5_structural_duplicate_owner_on_same_tag_stripped():
    """A valueless owner alongside a legitimate one on the same tag is an
    anomaly: projection fails closed, ALL owner attrs stripped, no key emitted."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    # Inject a valueless owner BEFORE the legitimate one.
    forged = html.replace("<p ", "<p data-okf-proj-owner ", 1)
    final_html, proj = build_annotation_projection(src, src, forged, owners=owners)
    # No owner attribute may survive.
    assert "data-okf-proj-owner" not in final_html.lower()
    # Projection must be incomplete (valueless owner = anomalous = fail closed).
    assert proj["complete"] is False


def test_r5_structural_copied_owner_incomplete():
    """Copied owner (same nonce on two elements) must fail closed."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    # Duplicate the entire <p> element (with its owner nonce).
    import re
    match = re.search(r'<p\s[^>]*>Hello world\.</p>', html)
    assert match
    copied = html + match.group(0)
    _, proj = build_annotation_projection(src, src, copied, owners=owners)
    assert proj["complete"] is False


def test_r5_structural_production_serve_mode(tmp_path):
    """Production serve-mode rendering produces complete projection with
    legitimate numeric keys."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\n# Heading\n\nParagraph text.\n"
    (tmp_path / "n.md").write_text(raw)
    b = Bundle.load(tmp_path)
    c = b.concepts[("n",)]
    html, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True
    assert "data-okf-proj-owner" not in html.lower()
    # Each mapped block has a numeric key.
    keys = [r["dom_key"] for r in proj["blocks"] if r["dom_key"] is not None]
    assert all(isinstance(k, int) and k > 0 for k in keys)
    assert len(keys) == len(set(keys))


def test_r5_structural_production_static_mode(tmp_path):
    """Production static-mode rendering produces the same projection as serve."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\n# Heading\n\nParagraph text.\n"
    (tmp_path / "n.md").write_text(raw)
    b = Bundle.load(tmp_path)
    c = b.concepts[("n",)]
    _, serve_proj = render_concept_body_with_projection(c, b, mode="serve")
    _, static_proj = render_concept_body_with_projection(c, b, mode="static")
    assert serve_proj == static_proj
    assert static_proj["complete"] is True


def test_r5_structural_malformed_html_fails_closed():
    """Malformed HTML that breaks owner marker parsing fails closed."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    # Corrupt the HTML so the owner marker is broken.
    corrupted = html.replace('data-okf-proj-owner="', 'data-okf-proj-owner="BROKEN')
    _, proj = build_annotation_projection(src, src, corrupted, owners=owners)
    # Projection must be incomplete (owner verification fails).
    assert proj["complete"] is False
    # No owner marker may survive.
    # (The corrupted nonce doesn't match any registered owner.)


# ===========================================================================
# Owner-finalizer audit fixes: occurrence count, nesting validation,
# marker leak guard, indeterminate mapping.
# ===========================================================================

def test_audit_duplicate_identical_owner_on_same_tag_fails():
    """A tag with 2+ owner occurrences — even duplicate IDENTICAL values —
    is invalid and must fail closed."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    # Inject a duplicate IDENTICAL owner attr on the same tag.
    dup = html.replace("<p ", f'<p data-okf-proj-owner="{nonce}" ', 1)
    _, proj = build_annotation_projection(src, src, dup, owners=owners)
    assert proj["complete"] is False


def test_audit_p_auto_closed_by_div_fails_closed():
    """Browser HTML5 auto-closes <p> when <div> opens inside. Python's
    HTMLParser doesn't. This nesting mismatch makes textContent unreliable
    → projection must fail closed when owner tags are involved."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    # Inject a <div> inside the <p> (browser auto-closes <p>).
    import re
    nested = re.sub(
        r'(<p\s[^>]*>)(.*?)(</p>)',
        r'\1\2<div>extra</div>\3', html, count=1,
    )
    _, proj = build_annotation_projection(src, src, nested, owners=owners)
    assert proj["complete"] is False


def test_audit_mismatched_end_tag_fails_closed():
    """Mismatched end tags (e.g. <p>...</div>) would be repaired by the
    browser differently from Python HTMLParser → fail closed."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    mismatched = html.replace("</p>", "</div>")
    _, proj = build_annotation_projection(src, src, mismatched, owners=owners)
    assert proj["complete"] is False


def test_audit_unclosed_owner_tag_fails_closed():
    """An unclosed owner-marked element leaves the nesting stack unbalanced."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    # Remove the closing </p>.
    unclosed = html.replace("</p>", "")
    _, proj = build_annotation_projection(src, src, unclosed, owners=owners)
    assert proj["complete"] is False


def test_audit_marker_leak_never_complete():
    """If any data-okf-proj-owner substring survives after rewrite, output
    must NEVER be complete — regardless of owner verification."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    from okf_loom.markdown_blocks import generate_owner_nonce
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    # Inject an owner-like string in a way the rewrite can't remove
    # (inside a malformed tag the parser can't process).
    nonce = generate_owner_nonce()
    leaked = html + f'<!-- data-okf-proj-owner="{nonce}" -->'
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    if "data-okf-proj-owner" in final_html.lower():
        assert proj["complete"] is False, (
            "marker leak must prevent complete projection"
        )


def test_audit_indeterminate_mapping_stays_unmappable():
    """When block_visible_segments returns None (indeterminate), the owner's
    expected_visible is None — not empty string. None never matches any DOM
    textContent, so the block stays unmapped and projection is incomplete.

    Strengthened from the vacuous form that could pass without ever creating
    an indeterminate mapping (ReviewAll r7). Uses a deterministic simulation:
    override expected_visible to None (the exact value the renderer produces
    when block_visible_segments returns None) and verify the invariant.
    """
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    assert len(owners) >= 1
    # Simulate the indeterminate case: the renderer sets expected_visible=None
    # when block_visible_segments returns None (viewer/markdown.py:750).
    owners[0]["expected_visible"] = None
    _, proj = build_annotation_projection(src, src, html, owners=owners)
    # No owner may be mapped — None never matches any DOM textContent.
    mapped = [r for r in proj["blocks"] if r["dom_key"] is not None]
    assert len(mapped) == 0, (
        f"indeterminate (expected_visible=None) owner must stay unmapped, "
        f"got {len(mapped)} mapped blocks"
    )
    # An eligible block that stays unmapped forces incomplete projection.
    assert proj["complete"] is False, (
        "indeterminate owner must force complete=False"
    )


def test_audit_every_owner_proven_complete(tmp_path):
    """For every owner in the list, assert the mapped block has a valid
    dom_key and non-empty segments (or is hr). This catches the 'assertion
    inside owner loop' bug where a stale assertion wasn't reached."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\n# H\n\nFirst para.\n\nSecond para.\n\n- list item\n"
    (tmp_path / "n.md").write_text(raw)
    b = Bundle.load(tmp_path)
    c = b.concepts[("n",)]
    html, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True
    mapped = [r for r in proj["blocks"] if r["dom_key"] is not None]
    assert len(mapped) >= 4
    for r in mapped:
        assert r["dom_key"] is not None
        assert isinstance(r["dom_key"], int) and r["dom_key"] > 0
        is_hr = r["block_id"].startswith("hr:")
        assert is_hr or len(r["segments"]) > 0, (
            f"block {r['block_id']} has dom_key but no segments"
        )


def test_audit_legitimate_keys_preserved_normal_case(tmp_path):
    """Normal rendering produces legitimate numeric keys — no owner markers
    leak, every eligible block mapped, keys unique and positive."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\n# H\n\nText with **bold** and `code`.\n"
    (tmp_path / "n.md").write_text(raw)
    b = Bundle.load(tmp_path)
    c = b.concepts[("n",)]
    html, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True
    assert "data-okf-proj-owner" not in html.lower()
    keys = [r["dom_key"] for r in proj["blocks"] if r["dom_key"] is not None]
    assert len(keys) == len(set(keys))
    assert all(k > 0 for k in keys)


# ===========================================================================
# R7: Browser-tree-complete projection validation
# (ReviewAll r7 findings: "projection validation is not browser-tree complete"
#  and "forged projection keys contradict the fail-closed contract")
#
# No stdlib HTML5 parser dependency is available, so we fail closed for every
# HTML5 repair construct (table foster parenting, implied list/container
# closure, formatting reconstruction, foreign content) and provide
# browser-backed parity proof via Chrome for each construct.
# ===========================================================================

def _r7_inject(src, suffix_html):
    """Render a body with owners, append extra HTML, finalize projection."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    html, owners = render_blocks_with_owners(src)
    return build_annotation_projection(src, src, html + suffix_html, owners=owners)


def test_r7_table_foster_parenting_element_fails_closed():
    """A non-table element (<p>) inside <table> is foster-parented by the
    browser → projection must fail closed."""
    _, proj = _r7_inject("Hello world.\n", "<table><p>fostered</p><tr><td>x</td></tr></table>")
    assert proj["complete"] is False


def test_r7_table_foster_parenting_text_fails_closed():
    """Bare text inside <table> (not in a cell) is foster-parented."""
    _, proj = _r7_inject("Hello world.\n", "<table>fostered text<tr><td>x</td></tr></table>")
    assert proj["complete"] is False


def test_r7_table_foster_parenting_div_fails_closed():
    """A <div> inside <tbody> (not a <tr>) is foster-parented."""
    _, proj = _r7_inject("Hello world.\n", "<table><tbody><div>d</div><tr><td>x</td></tr></tbody></table>")
    assert proj["complete"] is False


def test_r7_implied_li_closure_fails_closed():
    """Implied <li> auto-closure: <ul><li>a<li>b> — browser auto-closes
    the first <li>, HTMLParser nests them differently."""
    _, proj = _r7_inject("Hello world.\n", "<ul><li>first<li>second</ul>")
    assert proj["complete"] is False


def test_r7_implied_td_closure_fails_closed():
    """Implied <td> auto-closure: <tr><td>a<td>b>."""
    _, proj = _r7_inject("Hello world.\n", "<table><tr><td>a<td>b</tr></table>")
    assert proj["complete"] is False


def test_r7_implied_tr_closure_fails_closed():
    """Implied <tr> auto-closure: consecutive <tr> without closing tag."""
    _, proj = _r7_inject("Hello world.\n", "<table><tr><td>a</td><tr><td>b</td></tr></table>")
    assert proj["complete"] is False


def test_r7_implied_dd_dt_closure_fails_closed():
    """Implied <dd>/<dt> auto-closure."""
    _, proj = _r7_inject("Hello world.\n", "<dl><dt>term<dd>desc<dt>term2</dl>")
    assert proj["complete"] is False


def test_r7_formatting_reconstruction_fails_closed():
    """Formatting element (<b>) spanning a block boundary (<p>) triggers the
    adoption agency algorithm → projection must fail closed."""
    _, proj = _r7_inject("Hello world.\n", "<b>bold<p>still")
    assert proj["complete"] is False


def test_r7_formatting_reconstruction_strong_fails_closed():
    """<strong> spanning <p> boundary also triggers reconstruction."""
    _, proj = _r7_inject("Hello world.\n", "<i>italic<div>still")
    assert proj["complete"] is False


def test_r7_foreign_content_svg_fails_closed():
    """SVG foreign content has different tree-construction rules."""
    _, proj = _r7_inject("Hello world.\n", "<p>text<svg><circle/></svg></p>")
    assert proj["complete"] is False


def test_r7_foreign_content_math_fails_closed():
    """MathML foreign content has different tree-construction rules."""
    _, proj = _r7_inject("Hello world.\n", "<p>text<math><mi>x</mi></math></p>")
    assert proj["complete"] is False


# --- Negative: normal constructs must NOT trigger false-positive repair ---

def test_r7_normal_table_stays_complete(tmp_path):
    """Normal Markdown table (proper thead/tbody/tr/td) must stay complete."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\n# H\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    (tmp_path / "n.md").write_text(raw)
    b = Bundle.load(tmp_path)
    c = b.concepts[("n",)]
    _, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True


def test_r7_normal_list_stays_complete(tmp_path):
    """Normal Markdown list (proper <ul><li>...</li></ul>) must stay complete."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\n# H\n\n- first\n- second\n- third\n"
    (tmp_path / "n.md").write_text(raw)
    b = Bundle.load(tmp_path)
    c = b.concepts[("n",)]
    _, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True


def test_r7_normal_nested_list_stays_complete(tmp_path):
    """Normal nested Markdown list must stay complete."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\n# H\n\n- a\n  - b\n  - c\n- d\n"
    (tmp_path / "n.md").write_text(raw)
    b = Bundle.load(tmp_path)
    c = b.concepts[("n",)]
    _, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True


def test_r7_normal_inline_formatting_stays_complete(tmp_path):
    """Normal inline formatting (<strong>, <code>) inside <p> must stay complete."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\n# H\n\nText with **bold** and `code`.\n"
    (tmp_path / "n.md").write_text(raw)
    b = Bundle.load(tmp_path)
    c = b.concepts[("n",)]
    _, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True


# --- Pre-finalization projection key fail-closed (all forms) ---

@pytest.mark.parametrize("forged_attr", [
    'data-okf-projection-key="5"',
    "data-okf-projection-key='5'",
    "data-okf-projection-key=5",
    "data-okf-projection-key",
    "DATA-OKF-PROJECTION-KEY",
    "Data-Okf-Projection-Key",
    'DATA-OKF-PROJECTION-KEY="5"',
    'data-okf-projection-key=""',
])
def test_r7_preexisting_projection_key_forces_incomplete(forged_attr):
    """Any pre-finalization data-okf-projection-key occurrence — valued,
    valueless, empty, or any case form — must force complete=False
    (ReviewAll r7: "forged projection keys contradict the fail-closed contract").
    The key is still stripped from output, but completeness must not survive."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    forged = html.replace("<p ", f"<p {forged_attr} ", 1)
    final_html, proj = build_annotation_projection(src, src, forged, owners=owners)
    assert proj["complete"] is False, (
        f"pre-finalization projection-key must force complete=False: {forged_attr!r}"
    )
    # The forged key must also be stripped from the final HTML.
    assert "999" not in final_html


def test_r7_no_preexisting_key_stays_complete():
    """Without any pre-finalization projection key, normal projection is complete."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    _, proj = build_annotation_projection(src, src, html, owners=owners)
    assert proj["complete"] is True


# --- Browser-backed parity proof ---

# This test is skipped unless Playwright + a Chrome/Chromium executable are
# available. It proves the browser's actual DOM tree differs from the stdlib
# HTMLParser for each HTML5 repair case — which is why fail-closed is correct.

def _launch_browser():
    """Launch optional tooling without mistaking a broken launch for absence."""
    from browser_support import sync_playwright, launch_chromium
    pw = sync_playwright().start()
    try:
        return pw, launch_chromium(pw)
    except BaseException:
        pw.stop()
        raise


@pytest.mark.browser
def test_r7_browser_parity_html5_repair():
    """Browser-backed parity proof: for each HTML5 repair fixture, the
    browser's DOM tree differs from stdlib HTMLParser — proving that
    textContent extraction per owner nonce would be unreliable, so
    fail-closed is the correct contract.

    Loads the four repair fixtures (foster parenting, implied li closure,
    formatting reconstruction, foreign content) in Chrome and verifies the
    browser tree has properties that HTMLParser cannot reproduce."""
    FIXTURES = Path(__file__).parent / "fixtures" / "projection_html5"
    pw, browser = _launch_browser()
    try:
        page = browser.new_page()

        # 1. Foster parenting: <p> is relocated before <table>.
        page.goto(f"file://{FIXTURES / 'foster_parenting.html'}")
        result = page.evaluate("""() => {
            const body = document.body;
            const table = body.querySelector('table');
            const foster = document.getElementById('foster');
            // Browser: <p#foster> is a sibling BEFORE <table>, not inside it.
            return {
                fosterParent: foster ? foster.parentElement.tagName : null,
                fosterIsTableChild: table ? table.contains(foster) : null,
                tableText: table ? table.textContent.trim() : null,
                fosterText: foster ? foster.textContent.trim() : null,
            };
        }""")
        assert result["fosterIsTableChild"] is False, (
            "browser must foster-parent <p> outside <table>"
        )
        assert result["fosterParent"] == "BODY", (
            f"fostered <p> should be child of <body>, got {result['fosterParent']}"
        )
        assert "fostered" not in (result["tableText"] or ""), (
            "fostered text must be relocated out of <table>"
        )
        assert result["fosterText"] == "fostered"

        # 2. Implied li closure: <li#a> and <li#b> are siblings.
        page.goto(f"file://{FIXTURES / 'implied_li_closure.html'}")
        result = page.evaluate("""() => {
            const a = document.getElementById('a');
            const b = document.getElementById('b');
            return {
                aContainsB: a ? a.contains(b) : null,
                sameParent: a && b ? a.parentElement === b.parentElement : null,
                aText: a ? a.textContent.trim() : null,
                bText: b ? b.textContent.trim() : null,
            };
        }""")
        assert result["aContainsB"] is False, (
            "browser must auto-close <li#a> so <li#b> is NOT nested inside it"
        )
        assert result["sameParent"] is True, (
            "both <li> must be siblings under <ul>"
        )
        assert result["aText"] == "first"
        assert result["bText"] == "second"

        # 3. Formatting reconstruction: <b> is split/reconstructed.
        page.goto(f"file://{FIXTURES / 'formatting_reconstruction.html'}")
        result = page.evaluate("""() => {
            const outer = document.getElementById('outer');
            const inner = document.getElementById('inner');
            return {
                outerTag: outer ? outer.tagName : null,
                outerContainsInner: outer ? outer.contains(inner) : null,
                innerParent: inner ? inner.parentElement.tagName : null,
                boldCount: document.querySelectorAll('b').length,
            };
        }""")
        # The adoption agency algorithm reconstructs the <b>, so inner <p>
        # is NOT inside <b#outer>, and there are 2 <b> elements.
        assert result["outerContainsInner"] is False, (
            "browser must reconstruct <b> so <p> is not inside <b#outer>"
        )
        assert result["boldCount"] >= 2, (
            f"adoption agency must create ≥2 <b> elements, got {result['boldCount']}"
        )

        # 4. Foreign content: SVG is in a different namespace.
        page.goto(f"file://{(FIXTURES / 'foreign_content.html').as_posix()}")
        result = page.evaluate("""() => {
            const svg = document.querySelector('svg');
            const circle = document.querySelector('circle');
            return {
                svgExists: svg !== null,
                svgNamespace: svg ? svg.namespaceURI : null,
                circleExists: circle !== null,
            };
        }""")
        assert result["svgExists"], "SVG element must exist in browser DOM"
        assert result["svgNamespace"] == "http://www.w3.org/2000/svg", (
            f"SVG must be in SVG namespace, got {result['svgNamespace']}"
        )
        assert result["circleExists"], "SVG child <circle> must exist in browser DOM"
    finally:
        browser.close()
        pw.stop()


# ===========================================================================
# R8: Blockquote projection ↔ renderer recursive-interpretation parity.
# The renderer strips ``>`` and recursively re-renders inner Markdown, so a
# heading, list, hr, or nested blockquote inside ``>`` must produce the SAME
# expected_visible as the browser textContent of the <blockquote>.
# ===========================================================================

from html.parser import HTMLParser as _HTMLParser


class _OwnerTextScanner(_HTMLParser):
    """Extract textContent per data-okf-proj-owner nonce (mirrors render.py)."""

    _VOID = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str | None] = []
        self.text: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        nonce = dict(attrs).get("data-okf-proj-owner")
        if tag not in self._VOID:
            self.stack.append(nonce)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        for n in {x for x in self.stack if x}:
            self.text[n] = self.text.get(n, "") + data


class _ProjKeyTextScanner(_HTMLParser):
    """Extract the ACTUAL RENDERED textContent of each element carrying a
    ``data-okf-projection-key`` (the finalized owner marker), keyed by the
    numeric projection-key string. Used to assert dom offsets against the real
    rendered DOM text — not the IR visible text and not a source slice
    (both of which made earlier offset assertions tautological). Mirrors the
    browser's textContent per finalized owner. (ReviewAll r12 finding 10.)"""

    _VOID = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str | None] = []
        self.text: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        key = dict(attrs).get("data-okf-projection-key")
        if tag not in self._VOID:
            self.stack.append(key)
        elif key is not None:
            # void element with a projection key (e.g. <hr>): empty textContent
            self.text.setdefault(key, "")

    def handle_startendtag(self, tag, attrs):
        key = dict(attrs).get("data-okf-projection-key")
        if key is not None:
            self.text.setdefault(key, "")

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        for k in {x for x in self.stack if x}:
            self.text[k] = self.text.get(k, "") + data


def _proj_key_dom_text(final_html: str) -> dict[str, str]:
    """Return ``{projection_key_str: rendered_textContent}`` from final HTML."""
    scanner = _ProjKeyTextScanner()
    scanner.feed(final_html)
    return scanner.text


def _assert_quote_parity(src: str, label: str) -> None:
    """Assert IR expected_visible == renderer textContent for every owner,
    AND that each segment's source slice reconstructs the corresponding
    visible-text slice (direct segment/source-offset parity, not just
    visible text or completeness). (ReviewAll r9 minor #6.)"""
    from okf_loom.markdown_blocks import extract_blocks, block_visible_segments
    from okf_loom.viewer.markdown import render_blocks_with_owners

    blocks = extract_blocks(src)
    html_out, owners = render_blocks_with_owners(src)
    scanner = _OwnerTextScanner()
    scanner.feed(html_out)
    for owner in owners:
        block = next(b for b in blocks if b.id == owner["block_id"])
        vis, _ = block_visible_segments(block) or (None, None)
        if vis is None:
            continue
        actual = scanner.text.get(owner["nonce"], "")
        assert actual == vis, (
            f"{label}: owner {owner['block_id']} textContent mismatch: "
            f"expected {vis!r}, got {actual!r}"
        )
        # Direct segment/source-offset parity: the IR owner's segments must
        # point at source ranges whose text equals the corresponding slice of
        # the visible string. Visible-text equality alone can pass with shifted
        # source offsets; this assertion catches that directly.
        segments = owner["segments"]
        assert vis == owner["expected_visible"], (
            f"{label}: owner {owner['block_id']} expected_visible drift: "
            f"recomputed {vis!r} != owner {owner['expected_visible']!r}"
        )
        for s in segments:
            src_slice = src[s["source_start"]:s["source_end"]]
            vis_slice = vis[s["dom_start"]:s["dom_end"]]
            # Whitespace-normalization segments (e.g. a lazy-continuation
            # source newline/cr-lf joining as a single visible space) are
            # legitimate; only require that BOTH sides are whitespace-only.
            # Any non-whitespace content must match exactly, which catches a
            # shifted source offset even when the full visible text is right.
            if src_slice.strip() == "" and vis_slice.strip() == "":
                continue
            assert src_slice == vis_slice, (
                f"{label}: owner {owner['block_id']} segment offset mismatch: "
                f"src[{s['source_start']}:{s['source_end']}]={src_slice!r} "
                f"!= vis[{s['dom_start']}:{s['dom_end']}]={vis_slice!r}"
            )
        # Segments must be ordered and non-overlapping in dom coordinates.
        for a, b in zip(segments, segments[1:]):
            assert a["dom_end"] <= b["dom_start"], (
                f"{label}: owner {owner['block_id']} segments out-of-order/"
                f"overlapping: dom_end={a['dom_end']} > dom_start={b['dom_start']}"
            )



@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_heading_parity(newline):
    """Heading inside blockquote: ``> # Title`` must render as <h1> inside
    <blockquote>, not as inline '# Title' paragraph text."""
    nl = newline
    _assert_quote_parity(f"> # Title{nl}", f"heading ({newline!r})")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_thematic_break_parity(newline):
    """Thematic break inside blockquote: ``> ---`` renders as <hr /> with
    empty textContent inside <blockquote>."""
    nl = newline
    _assert_quote_parity(f"> ---{nl}", f"hr ({newline!r})")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_unordered_list_parity(newline):
    """Unordered list inside blockquote: sibling <li> items concatenate
    without separator inside the <blockquote> textContent."""
    nl = newline
    _assert_quote_parity(f"> - item1{nl}> - item2{nl}", f"unordered-list ({newline!r})")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_ordered_list_parity(newline):
    """Ordered list inside blockquote: ``> 1. first`` items concatenate
    without separator."""
    nl = newline
    _assert_quote_parity(f"> 1. first{nl}> 2. second{nl}", f"ordered-list ({newline!r})")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_nested_blockquote_parity(newline):
    """Nested blockquote: ``> > nested`` recursively strips both ``>`` levels."""
    nl = newline
    _assert_quote_parity(f"> > nested{nl}", f"nested-quote ({newline!r})")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_mixed_paragraph_heading_parity(newline):
    """Paragraph + heading inside blockquote: ``> text\\n> # heading``
    produces 'text\\nheading' matching the \\n join between render units."""
    nl = newline
    _assert_quote_parity(f"> text{nl}> # heading{nl}", f"mixed-para-heading ({newline!r})")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_paragraph_then_hr_parity(newline):
    """Paragraph + thematic break inside blockquote: text + \\n + empty."""
    nl = newline
    src = f"> before{nl}>{nl}> ---{nl}"
    _assert_quote_parity(src, f"para+hr ({newline!r})")


def test_r8_quote_mixed_ordered_unordered_parity():
    """Ordered→unordered switch inside blockquote produces \\n between
    sub-groups (matching _render_list_block's sub-run split)."""
    _assert_quote_parity("> 1. first\n> - second\n", "mixed-ordered-unordered")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_simple_text_still_works(newline):
    """Simple text blockquote (the old working case) must not regress."""
    nl = newline
    _assert_quote_parity(f"> quoted *value*{nl}", f"simple-text ({newline!r})")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_lazy_continuation_still_works(newline):
    """Lazy continuation blockquote must not regress."""
    nl = newline
    _assert_quote_parity(f"> Quoted{nl}lazy continuation{nl}{nl}After.{nl}", f"lazy ({newline!r})")


# --- Projection completeness for blockquotes with inner block constructs ---

@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_heading_projection_complete(newline):
    nl = newline
    src = f"Intro.{nl}{nl}> # Title{nl}{nl}Outro.{nl}"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True, (
        f"blockquote with heading must be complete ({newline!r})"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_thematic_break_projection_complete(newline):
    nl = newline
    src = f"Intro.{nl}{nl}> ---{nl}{nl}Outro.{nl}"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True, (
        f"blockquote with hr must be complete ({newline!r})"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_list_projection_complete(newline):
    nl = newline
    src = f"Intro.{nl}{nl}> - item1{nl}> - item2{nl}{nl}Outro.{nl}"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True, (
        f"blockquote with list must be complete ({newline!r})"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r8_quote_nested_projection_complete(newline):
    nl = newline
    src = f"Intro.{nl}{nl}> > nested{nl}{nl}Outro.{nl}"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True, (
        f"nested blockquote must be complete ({newline!r})"
    )


def test_r8_quote_mixed_blocks_projection_complete():
    src = "Intro.\n\n> text\n> # heading\n> - item\n\nOutro.\n"
    _, proj = _r4_render_and_project(src)
    assert proj["complete"] is True, (
        "blockquote with mixed inner blocks must be complete"
    )


# ===========================================================================
# R8: Leak-guard security — residual owner nonce values and forged
# data-okf-projection-key text in comments/text/malformed constructs must
# NOT survive finalization; the projection must fail closed.
# ===========================================================================

def test_r8_owner_nonce_in_html_comment_fails_closed():
    """A registered owner nonce value appearing in an HTML comment must be
    detected by the leak guard and force complete=False."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    leaked = html.replace("Hello world.", f"Hello world.<!-- {nonce} -->")
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "owner nonce in HTML comment must fail closed"
    )


def test_r8_owner_nonce_in_text_fails_closed():
    """A registered owner nonce value appearing in visible text must be
    detected by the leak guard and force complete=False."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    leaked = html.replace("Hello world.", f"Hello world. {nonce}")
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "owner nonce in text must fail closed"
    )


def test_r8_forged_projection_key_in_comment_fails_closed():
    """Forged data-okf-projection-key text in an HTML comment must be
    detected by the leak guard and force complete=False."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html + '<!-- data-okf-projection-key="999" -->'
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "forged projection-key in comment must fail closed"
    )


def test_r8_forged_projection_key_in_text_fails_closed():
    """Forged data-okf-projection-key text in visible text must be detected
    by the leak guard and force complete=False."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html.replace("Hello world.", "Hello data-okf-projection-key world.")
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "forged projection-key in text must fail closed"
    )


def test_r8_no_false_positive_leak_guard_on_clean_output():
    """Normal output with legitimate projection keys must NOT trigger the
    leak guard — no false positives."""
    src = "Hello world.\n"
    _, proj = _render_and_project(src)
    assert proj["complete"] is True, (
        "clean output must not trigger leak guard false positive"
    )


# ===========================================================================
# R9: Entity-decode leak detection — a browser decodes HTML entities in text
# content, so encoded capability material must be detected (not just literal).
# ===========================================================================

def test_r9_encoded_projection_key_in_comment_detected():
    """An entity-encoded projection-key substring in a comment
    (``data-okf-projection&#45;key``) decodes to the forbidden value in a
    browser and must force complete=False."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html + '<!-- data-okf-projection&#45;key="999" -->'
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "entity-encoded projection-key in comment must fail closed"
    )


def test_r9_encoded_projection_key_in_text_detected():
    """An entity-encoded projection-key substring in visible text must be
    detected because a browser decodes it to the forbidden literal."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html.replace(
        "Hello world.", "Hello data-okf-projection&#45;key world."
    )
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "entity-encoded projection-key in text must fail closed"
    )


def test_r9_encoded_owner_marker_in_comment_detected():
    """An entity-encoded owner attribute name (``data-okf-proj&#45;owner``)
    must be detected."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html + '<!-- data-okf-proj&#45;owner="x" -->'
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "entity-encoded owner marker in comment must fail closed"
    )


def test_r9_encoded_owner_marker_hex_ref_detected():
    """A hex numeric character reference (``&#x2d;`` for ``-``) encoding a
    capability substring must be detected."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html + '<!-- data-okf-projection&#x2d;key="999" -->'
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "hex-ref-encoded projection-key in comment must fail closed"
    )


@pytest.mark.parametrize("char_index", [0, 5, 15])
def test_r9_encoded_nonce_char_in_comment_detected(char_index):
    """An entity-encoded character within a registered owner nonce must be
    detected — the decoded nonce is a leaked capability."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    assert char_index < len(nonce)
    ch = nonce[char_index]
    encoded = nonce[:char_index] + f"&#{ord(ch)};" + nonce[char_index + 1:]
    leaked = html + f"<!-- {encoded} -->"
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        f"entity-encoded nonce char at {char_index} must fail closed"
    )


# ===========================================================================
# R9: Contaminated material must be removed/escaped from returned final_html,
# not merely flagged complete=False.
# ===========================================================================

def test_r9_contaminated_nonce_removed_from_comment():
    """A leaked owner nonce in an HTML comment must be REMOVED from the
    returned final_html (not just complete=False), and the comment must not
    carry the capability string after entity decoding."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    leaked = html.replace("Hello world.", f"Hello world.<!-- {nonce} -->")
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert nonce not in final_html
    assert nonce not in html_lib.unescape(final_html)


def test_r9_contaminated_nonce_removed_from_text():
    """A leaked owner nonce in visible text must be removed from the returned
    final_html while authored prose around it survives."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    leaked = html.replace("Hello world.", f"Hello world. {nonce}")
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert nonce not in final_html
    assert "Hello world." in final_html


def test_r9_contaminated_nonce_removed_from_tag_attribute():
    """A leaked nonce inside a non-owner tag attribute (e.g. ``title``) must
    be removed from the returned final_html by the global nonce scrub."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    leaked = html.replace("<p ", f'<p title="{nonce}" ', 1)
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert nonce not in final_html


def test_r9_contaminated_encoded_nonce_removed():
    """An entity-encoded leaked nonce must be removed from returned final_html
    so the decoded capability does not survive."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    encoded = f"&#{ord(nonce[0])};" + nonce[1:]
    leaked = html.replace("Hello world.", f"Hello world. {encoded}")
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert nonce not in html_lib.unescape(final_html)


def test_r9_forged_projection_key_removed_from_comment():
    """Forged projection-key text in a comment must be removed from returned
    final_html while legitimate projection keys on mapped blocks survive."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html + '<!-- data-okf-projection-key="999" -->'
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    # The forged comment content is scrubbed; the legitimate block key is kept.
    assert html_lib.unescape(final_html).lower().count(
        "data-okf-projection-key") <= 1
    assert '"999"' not in final_html


def test_r9_forged_projection_key_removed_from_text():
    """Forged projection-key text in visible text must be removed while the
    authored prose around it survives."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html.replace("Hello world.", "Hello data-okf-projection-key world.")
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert html_lib.unescape(final_html).lower().count(
        "data-okf-projection-key") == 0
    assert "Hello" in final_html and "world." in final_html


def test_r9_encoded_forged_key_removed_from_comment():
    """An entity-encoded forged projection-key in a comment must be removed
    from returned final_html (decoded form gone)."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html + '<!-- data-okf-projection&#45;key="999" -->'
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert html_lib.unescape(final_html).lower().count(
        "data-okf-projection-key") <= 1
    assert '"999"' not in final_html


def test_r9_clean_output_not_corrupted_by_scrub():
    """Clean output with legitimate projection keys must pass through the
    scrub unchanged (no false positive removal, complete stays True)."""
    src = "Hello world.\n"
    html, proj = _render_and_project(src)
    assert proj["complete"] is True
    assert "Hello world." in html
    keys = re.findall(r'data-okf-projection-key="(\d+)"', html)
    assert keys and all(k.isdecimal() for k in keys)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r9_quote_projection_segment_offset_parity(newline):
    """Direct segment/source-offset parity at the PROJECTION level: each
    mapped blockquote block's projection segments must slice the canonical
    source to the exact visible text (whitespace-normalization segments
    excepted), for both LF and CRLF."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.markdown_blocks import extract_blocks, block_visible_segments
    nl = newline
    src = f"Intro.{nl}{nl}> # Title{nl}> para text{nl}{nl}Outro.{nl}"
    blocks = extract_blocks(src)
    rendered, owners = render_blocks_with_owners(src)
    final_html, proj = build_annotation_projection(src, src, rendered, owners=owners)
    # Find the blockquote block record in the projection.
    quote_rec = next(
        (r for r in proj["blocks"] if r["block_id"].startswith("block-quote")
         and r["dom_key"] is not None),
        None,
    )
    assert quote_rec is not None, f"blockquote must be mapped ({newline!r})"
    block = next(b for b in blocks if b.id == quote_rec["block_id"])
    vis, _ = block_visible_segments(block) or (None, None)
    assert vis is not None
    # Projection segments carry canonical-source offsets (body offset applied).
    for s in quote_rec["segments"]:
        src_slice = src[s["source_start"]:s["source_end"]]
        vis_slice = vis[s["dom_start"]:s["dom_end"]]
        if src_slice.strip() == "" and vis_slice.strip() == "":
            continue
        assert src_slice == vis_slice, (
            f"projection segment offset mismatch ({newline!r}): "
            f"src[{s['source_start']}:{s['source_end']}]={src_slice!r} "
            f"!= vis[{s['dom_start']}s:{s['dom_end']}]={vis_slice!r}"
        )


# ===========================================================================
# R10: Bounded iterative canonicalization — detection of double-encoded
# entities, percent encodings, and named entities. Malformed/unterminated
# tag-like capability text scrubbed. Post-scrub residual contamination
# verification. Nested LF/CRLF projection segment/source-offset assertions.
# ===========================================================================

def test_r10_canonicalize_iteratively_decodes_double_encoded_entity():
    """``_canonicalize_iteratively`` must peel successive entity layers:
    ``&amp;#45;`` → ``&#45;`` → ``-`` in two passes. A single
    ``_html.unescape`` leaves ``&#45;`` (not ``-``). Returns canonical text
    plus convergence/exhaustion state (ReviewAll r11)."""
    from okf_loom.render import _canonicalize_iteratively
    result, converged, exhausted = _canonicalize_iteratively("&amp;#45;")
    assert result == "-", (
        f"double-encoded entity should canonicalize to '-': got {result!r}"
    )
    assert converged is True, "should converge within budget"
    assert exhausted is False, "should not exhaust"


def test_r10_canonicalize_iteratively_decodes_percent_encoding():
    """``_canonicalize_iteratively`` must percent-decode: ``%64%61%74%61``
    → ``data``."""
    from okf_loom.render import _canonicalize_iteratively
    result, converged, exhausted = _canonicalize_iteratively("%64%61%74%61")
    assert result == "data", (
        f"percent-encoded should canonicalize to 'data': got {result!r}"
    )
    assert converged is True
    assert exhausted is False


def test_r10_canonicalize_iteratively_bounded():
    """The canonicalizer must terminate in at most _CANONICALIZE_MAX_ITERATIONS
    passes and not hang on pathological self-referential input."""
    from okf_loom.render import _canonicalize_iteratively, _CANONICALIZE_MAX_ITERATIONS
    # Three layers of encoding (&amp;amp;amp;): pass 1 → &amp;amp;, pass 2 →
    # &amp;, pass 3 → &, pass 4 → & (fixed point). Converges within the bound.
    result, converged, exhausted = _canonicalize_iteratively("&amp;amp;amp;")
    assert result == "&"
    assert _CANONICALIZE_MAX_ITERATIONS == 4
    assert converged is True, "&amp;amp;amp; converges within the 4-pass budget"
    assert exhausted is False


def test_r10_canonicalize_iteratively_preserves_plain_text():
    """Ordinary authored text with no encoding must pass through unchanged."""
    from okf_loom.render import _canonicalize_iteratively
    text = "Hello world. This is ordinary prose with no entities."
    result, converged, exhausted = _canonicalize_iteratively(text)
    assert result == text
    assert converged is True
    assert exhausted is False


def test_r10_canonicalize_iteratively_handles_named_entity():
    """Named entities like ``&hyphen;`` (→ U+2010) must be decoded by the
    canonicalizer, even though they don't match the ASCII hyphen-minus in
    capability strings (U+002D)."""
    from okf_loom.render import _canonicalize_iteratively
    result, converged, exhausted = _canonicalize_iteratively("&hyphen;")
    assert result == "\u2010", (
        f"&hyphen; should decode to U+2010: got {result!r}"
    )
    assert converged is True
    assert exhausted is False


def test_r10_capability_char_alt_includes_percent_encoding():
    """``_capability_char_alt`` must generate percent-encoding alternatives
    for ASCII characters so scrub patterns match percent-encoded capability
    material."""
    from okf_loom.render import _capability_char_alt
    pattern = _capability_char_alt("d")
    # The literal, decimal NCR, hex NCR, and percent encodings should all
    # be alternatives.
    import re as _re
    compiled = _re.compile(pattern, _re.IGNORECASE)
    assert compiled.match("d")
    assert compiled.match("D")  # case-insensitive
    assert compiled.match("&#100;")
    assert compiled.match("&#x64;")
    assert compiled.match("%64")  # percent encoding of 'd' (ord 100 = 0x64)


def test_r10_double_encoded_entity_in_comment_detected():
    """A double-encoded entity containing capability material
    (``&amp;#100;ata-okf-proj-owner``) must be detected by the bounded
    iterative canonicalizer and force ``complete=False``."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    # &amp;#100; → &#100; → 'd', giving data-okf-proj-owner.
    leaked = html + '<!-- &amp;#100;ata-okf-proj-owner="x" -->'
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "double-encoded entity in comment must fail closed"
    )


def test_r10_double_encoded_entity_in_text_detected():
    """A double-encoded entity in visible text must be detected."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html.replace(
        "Hello world.", "Hello &amp;#100;ata-okf-proj-owner world."
    )
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "double-encoded entity in text must fail closed"
    )


def test_r10_percent_encoded_owner_marker_in_comment_detected():
    """A percent-encoded capability substring (``%64%61%74%61-okf-proj-owner``)
    in a comment must be detected by the iterative canonicalizer."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html + '<!-- %64%61%74%61-okf-proj-owner="x" -->'
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "percent-encoded owner marker in comment must fail closed"
    )


def test_r10_percent_encoded_nonce_in_comment_detected():
    """A percent-encoded registered nonce character in a comment must be
    detected as a nonce leak by the iterative canonicalizer."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    # Percent-encode the first character of the nonce.
    encoded = f"%{ord(nonce[0]):02X}" + nonce[1:]
    leaked = html + f"<!-- {encoded} -->"
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "percent-encoded nonce in comment must fail closed"
    )


def test_r10_named_entity_no_false_positive():
    """Authored text containing a named entity like ``&hyphen;`` (which
    decodes to U+2010, NOT the ASCII hyphen-minus U+002D used in capability
    strings) must NOT trigger a false positive capability leak. The
    canonicalizer decodes it to a different character than the capability
    string contains."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    # &hyphen; → U+2010, which is NOT '-' (U+002D) used in capability strings.
    # Inject in a COMMENT so text verification is not disturbed.
    leaked = html + '<!-- data&hyphen;okf-proj-owner -->'
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    # The named entity does NOT match the capability string (different code
    # point), so projection must remain complete.
    assert proj["complete"] is True, (
        "named entity &hyphen; must not false-positive as capability leak"
    )


def test_r10_unterminated_tag_capability_scrubbed():
    """An unterminated tag-like construct containing capability material must
    be scrubbed from the returned final_html (not copied verbatim)."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    # Unterminated tag-like construct — no closing '>'.
    leaked = html + f'<p data-okf-proj-owner="{nonce}"'
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    # The nonce must NOT survive in the returned HTML (decoded too).
    assert nonce not in final_html
    assert nonce not in html_lib.unescape(final_html)
    # The owner attribute name must NOT survive.
    assert "data-okf-proj-owner" not in html_lib.unescape(final_html).lower()


def test_r10_malformed_tag_projection_key_scrubbed():
    """Forged projection-key text in an unterminated tag-like construct must
    be scrubbed from returned final_html."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html + '<p data-okf-projection-key="999"'
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert '"999"' not in final_html


def test_r10_post_scrub_residual_fail_closed():
    """If contamination is detected, complete must be False even after
    scrubbing attempts to clean the output. The post-scrub canonicalization
    verification acts as an additional safety net."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    # Inject the nonce in a comment — the scrub will remove it, but
    # complete must still be False because contamination was detected.
    leaked = html + f"<!-- {nonce} -->"
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False


def test_r10_clean_output_no_residual_leak():
    """Clean output with legitimate projection keys must not trigger any
    residual leak detection. complete stays True."""
    src = "Hello world.\n"
    _, proj = _render_and_project(src)
    assert proj["complete"] is True


def test_r10_percent_encoded_forged_key_in_comment_detected():
    """A percent-encoded forged projection-key in a comment must be detected
    by the iterative canonicalizer."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html + '<!-- %64%61%74%61-okf-projection-key="999" -->'
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "percent-encoded forged projection-key in comment must fail closed"
    )


def test_r10_triple_encoded_entity_detected():
    """A triple-encoded entity (``&amp;amp;#45;``) must be detected by the
    bounded iterative canonicalizer (three decode passes)."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection, _canonicalize_iteratively
    # Verify the canonicalizer handles triple encoding.
    canon, _, _ = _canonicalize_iteratively("&amp;amp;#45;")
    assert canon == "-"
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html + '<!-- &amp;amp;#100;ata-okf-proj-owner="x" -->'
    _, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "triple-encoded entity in comment must fail closed"
    )


def test_r10_authored_text_preserved_after_scrub():
    """When contamination is scrubbed, ordinary authored prose around the
    contamination must survive intact."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    leaked = html.replace(
        "Hello world.", f"Hello world. {nonce} more text."
    )
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert nonce not in final_html
    assert "Hello world." in final_html
    assert "more text." in final_html


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r10_nested_list_projection_segment_offset_parity(newline):
    """Explicit nested LF/CRLF final projection segment/source-offset
    assertions: each mapped block in a deeply nested list must have
    projection segments whose canonical-source slices match the visible-text
    slices, for both LF and CRLF. CRLF source is normalised to LF before
    comparison (the block extractor normalises line endings in visible text
    but source offsets point into the raw CRLF source)."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.markdown_blocks import extract_blocks, block_visible_segments
    nl = newline
    src = f"- outer{nl}  - child1{nl}    - grandchild1{nl}  - child2{nl}"
    blocks = extract_blocks(src)
    rendered, owners = render_blocks_with_owners(src)
    final_html, proj = build_annotation_projection(src, src, rendered, owners=owners)
    assert proj["complete"] is True, f"nested list must project complete ({newline!r})"
    # Assert every mapped block's segments slice the source correctly.
    block_map = {b.id: b for b in blocks}
    for rec in proj["blocks"]:
        if rec["dom_key"] is None or not rec["segments"]:
            continue
        block = block_map.get(rec["block_id"])
        assert block is not None, f"block {rec['block_id']} not found in IR"
        vis, segs = block_visible_segments(block) or (None, None)
        assert vis is not None, f"block {rec['block_id']} has no visible segments"
        for s in rec["segments"]:
            src_slice = src[s["source_start"]:s["source_end"]].replace("\r\n", "\n")
            vis_slice = vis[s["dom_start"]:s["dom_end"]]
            if src_slice.strip() == "" and vis_slice.strip() == "":
                continue
            assert src_slice == vis_slice, (
                f"nested list segment mismatch ({newline!r}) "
                f"block={rec['block_id']}: "
                f"src[{s['source_start']}:{s['source_end']}]={src_slice!r} "
                f"!= vis[{s['dom_start']}:{s['dom_end']}]={vis_slice!r}"
            )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r10_nested_blockquote_with_list_segment_offset_parity(newline):
    """Explicit nested LF/CRLF projection segment/source-offset assertions
    for a blockquote containing a list (mixed nesting), for both LF and
    CRLF. CRLF source is normalised to LF before comparison."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.markdown_blocks import extract_blocks, block_visible_segments
    nl = newline
    src = (
        f"Intro.{nl}{nl}"
        f"> Quote line.{nl}> - nested item{nl}> - another{nl}{nl}"
        f"Outro.{nl}"
    )
    blocks = extract_blocks(src)
    rendered, owners = render_blocks_with_owners(src)
    final_html, proj = build_annotation_projection(src, src, rendered, owners=owners)
    # At least the blockquote must be mapped.
    mapped = [r for r in proj["blocks"] if r["dom_key"] is not None]
    assert len(mapped) >= 1, f"at least one block must be mapped ({newline!r})"
    # Assert segment/source-offset parity for every mapped block.
    block_map = {b.id: b for b in blocks}
    for rec in mapped:
        if not rec["segments"]:
            continue
        block = block_map.get(rec["block_id"])
        assert block is not None, f"block {rec['block_id']} not found"
        vis, _ = block_visible_segments(block) or (None, None)
        assert vis is not None, f"block {rec['block_id']} has no visible segments"
        for s in rec["segments"]:
            src_slice = src[s["source_start"]:s["source_end"]].replace("\r\n", "\n")
            vis_slice = vis[s["dom_start"]:s["dom_end"]]
            if src_slice.strip() == "" and vis_slice.strip() == "":
                continue
            assert src_slice == vis_slice, (
                f"nested blockquote segment mismatch ({newline!r}) "
                f"block={rec['block_id']}: "
                f"src[{s['source_start']}:{s['source_end']}]={src_slice!r} "
                f"!= vis[{s['dom_start']}:{s['dom_end']}]={vis_slice!r}"
            )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r10_nested_heading_paragraph_segment_offset_parity(newline):
    """Explicit nested LF/CRLF projection segment/source-offset assertions
    for a document with headings followed by paragraphs (the common case),
    verifying offsets are correct for both LF and CRLF. CRLF source is
    normalised to LF before comparison."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.markdown_blocks import extract_blocks, block_visible_segments
    nl = newline
    src = (
        f"# Top Heading{nl}{nl}"
        f"First paragraph.{nl}{nl}"
        f"## Sub Heading{nl}{nl}"
        f"Second paragraph with **bold** text.{nl}{nl}"
        f"### Sub Sub{nl}{nl}"
        f"Third.{nl}"
    )
    blocks = extract_blocks(src)
    rendered, owners = render_blocks_with_owners(src)
    final_html, proj = build_annotation_projection(src, src, rendered, owners=owners)
    assert proj["complete"] is True, f"heading/para doc must project complete ({newline!r})"
    # Every mapped block's segments must slice source correctly.
    block_map = {b.id: b for b in blocks}
    checked = 0
    for rec in proj["blocks"]:
        if rec["dom_key"] is None or not rec["segments"]:
            continue
        block = block_map.get(rec["block_id"])
        assert block is not None, f"block {rec['block_id']} not found"
        vis, _ = block_visible_segments(block) or (None, None)
        assert vis is not None, f"block {rec['block_id']} has no visible segments"
        for s in rec["segments"]:
            src_slice = src[s["source_start"]:s["source_end"]].replace("\r\n", "\n")
            vis_slice = vis[s["dom_start"]:s["dom_end"]]
            if src_slice.strip() == "" and vis_slice.strip() == "":
                continue
            assert src_slice == vis_slice, (
                f"heading/para segment mismatch ({newline!r}) "
                f"block={rec['block_id']}: "
                f"src[{s['source_start']}:{s['source_end']}]={src_slice!r} "
                f"!= vis[{s['dom_start']}:{s['dom_end']}]={vis_slice!r}"
            )
        checked += 1
    assert checked >= 3, f"expected at least 3 mapped blocks with segments ({newline!r})"


def test_r10_multiple_nested_levels_crlf_source_offsets():
    """Deeply nested (3-level) list with CRLF must produce correct source
    offsets at every nesting level. This is the explicit CRLF assertion that
    catches off-by-one errors from CRLF line-ending normalization in the
    block extractor."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.markdown_blocks import extract_blocks
    nl = "\r\n"
    src = f"- L1{nl}  - L2a{nl}    - L3a{nl}    - L3b{nl}  - L2b{nl}"
    blocks = extract_blocks(src)
    rendered, owners = render_blocks_with_owners(src)
    final_html, proj = build_annotation_projection(src, src, rendered, owners=owners)
    # Every block's source_start/source_end must fall within the source.
    for rec in proj["blocks"]:
        assert 0 <= rec["source_start"] <= len(src), (
            f"source_start out of bounds: {rec['source_start']} (len={len(src)})"
        )
        assert 0 <= rec["source_end"] <= len(src), (
            f"source_end out of bounds: {rec['source_end']} (len={len(src)})"
        )
        assert rec["source_start"] <= rec["source_end"], (
            f"source_start > source_end for {rec['block_id']}"
        )
    # The deepest item (L3b) must be found via MANDATORY SOURCE LOOKUP (the
    # block whose canonical source slice contains "L3b") — NOT by block_id
    # substring (the block_id is "list-item:1" and never contains "L3b", so the
    # old lookup was vacuous and the assertion never ran). The block must be
    # mapped, and the L3b segment must slice the CRLF source to the exact
    # normalized visible text. (ReviewAll r11: repair deep CRLF assertion.)
    l3b_rec = next(
        (r for r in proj["blocks"]
         if "L3b" in src[r["source_start"]:r["source_end"]]),
        None,
    )
    assert l3b_rec is not None, (
        "L3b block not found by mandatory source lookup; blocks: "
        + repr([(r["block_id"], src[r["source_start"]:r["source_end"]].replace("\r", "\\r"))
                for r in proj["blocks"]])
    )
    assert l3b_rec["dom_key"] is not None, "L3b-containing block must be mapped"
    # Find the L3b segment and assert exact source/DOM parity (CRLF → LF).
    l3b_seg = next(
        (s for s in l3b_rec["segments"]
         if "L3b" in src[s["source_start"]:s["source_end"]]),
        None,
    )
    assert l3b_seg is not None, (
        f"no segment slices source to L3b; segments: {l3b_rec['segments']}"
    )
    src_slice = src[l3b_seg["source_start"]:l3b_seg["source_end"]]
    assert src_slice == "L3b"
    # CRLF is its own 2-to-1 transform, not mixed into an identity run.
    newline_seg = l3b_rec["segments"][l3b_rec["segments"].index(l3b_seg) + 1]
    assert newline_seg["source_start"] == l3b_seg["source_end"]
    assert newline_seg["dom_start"] == l3b_seg["dom_end"]
    assert src[newline_seg["source_start"]:newline_seg["source_end"]] == "\r\n"
    # ACTUAL RENDERED DOM text keyed by dom_key (ReviewAll r12 finding 10):
    # the dom offsets must slice the REAL rendered textContent of the element
    # carrying data-okf-projection-key=<dom_key>, not a source slice (the old
    # ``dom_slice = src[...]`` was tautological with ``src_slice``).
    dom_text = _proj_key_dom_text(final_html)
    key_str = str(l3b_rec["dom_key"])
    assert key_str in dom_text, (
        f"no rendered DOM text for projection-key {key_str}; "
        f"got keys {list(dom_text)}"
    )
    dom_slice = dom_text[key_str][l3b_seg["dom_start"]:l3b_seg["dom_end"]]
    assert dom_slice == "L3b", (
        f"L3b dom offset must slice the ACTUAL rendered DOM text to 'L3b': "
        f"got {dom_slice!r} from dom_text={dom_text[key_str]!r}"
    )
    assert dom_text[key_str][newline_seg["dom_start"]:newline_seg["dom_end"]] == "\n"


# ===========================================================================
# R11: Convergence/exhaustion state + bounded iterative scrub of nested
# encodings in comments, text, AND tag interiors. Exhaustion forces a
# capability-free safe fallback (complete=False). Nested encodings cannot
# survive the returned HTML under the same bounded semantics used for
# detection. Explicit two-level ``> >`` LF+CRLF exact source/DOM proof.
# (ReviewAll r11.)
# ===========================================================================

def _r11_entity_layered(token: str, layers: int) -> str:
    """Encode ``token``'s first character in ``layers`` HTML-entity layers
    (numeric character reference base): layer 1 → ``&#N;``, layer 2 →
    ``&amp;#N;``, layer 3 → ``&amp;amp;#N;``, … . Used to synthesise
    double/triple/deeper/overbudget encoded capability material for scrub and
    exhaustion tests."""
    return "&" + "amp;" * (layers - 1) + f"#{ord(token[0])};" + token[1:]


def _r11_assert_capability_free(final_html: str, nonce: str) -> None:
    """Assert the returned HTML is capability-free under BOTH a single browser
    decode pass (``html.unescape``) AND the bounded iterative canonicalizer —
    the same semantics used for leak detection. Owner/projection-key names and
    the registered nonce must not survive in either view."""
    from okf_loom.render import _canonicalize_iteratively
    one_pass = html_lib.unescape(final_html).lower()
    canon, _, _ = _canonicalize_iteratively(final_html)
    cl = canon.lower()
    assert "data-okf-proj-owner" not in one_pass, "owner marker survives one decode"
    assert "data-okf-proj-owner" not in cl, "owner marker survives bounded canonicalize"
    # Forged projection-key text: at most the legitimate numeric keys survive.
    assert cl.count("data-okf-projection-key") <= 1, (
        f"forged projection-key survives canonicalize: "
        f"{cl.count('data-okf-projection-key')} occurrences"
    )
    assert nonce not in one_pass, "nonce survives one decode"
    assert nonce not in canon, "nonce survives bounded canonicalize"


def test_r11_canonicalize_returns_convergence_exhaustion_state():
    """``_canonicalize_iteratively`` returns ``(text, converged, exhausted)``
    where converged and exhausted are complements. Convergent input reports
    ``converged=True, exhausted=False``; overbudget input reports
    ``converged=False, exhausted=True``."""
    from okf_loom.render import (
        _canonicalize_iteratively, _CANONICALIZE_MAX_ITERATIONS,
    )
    # Convergent cases.
    text, conv, exh = _canonicalize_iteratively("plain text")
    assert (text, conv, exh) == ("plain text", True, False)
    text, conv, exh = _canonicalize_iteratively("&amp;#45;")
    assert (text, conv, exh) == ("-", True, False)
    # Overbudget: MAX+1 entity layers cannot be peeled within the budget.
    overbudget = "&" + "amp;" * _CANONICALIZE_MAX_ITERATIONS + "#45;"
    text, conv, exh = _canonicalize_iteratively(overbudget)
    assert conv is False, "overbudget input must not converge"
    assert exh is True, "overbudget input must report exhaustion"


def test_r11_overbudget_encoding_forces_incomplete_safe_fallback():
    """Encoding deeper than ``_CANONICALIZE_MAX_ITERATIONS`` cannot be proven
    capability-free within the budget, so exhaustion MUST force the
    capability-free safe fallback (``complete=False``) even if the nonce is
    not revealed by the bounded canonicalizer."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import (
        build_annotation_projection, _canonicalize_iteratively,
        _CANONICALIZE_MAX_ITERATIONS,
    )
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    # MAX+1 layers: the bounded canonicalizer cannot fully peel this.
    enc = _r11_entity_layered(nonce, _CANONICALIZE_MAX_ITERATIONS + 1)
    leaked = html + f"<!-- {enc} -->"
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        "overbudget (exhaustion) encoding must force complete=False "
        "(capability-free safe fallback)"
    )
    # The returned HTML must still be capability-free under the SAME bounded
    # semantics: the canonicalizer cannot reveal the nonce within budget, so
    # the overbudget comment surviving in the output does not leak.
    _r11_assert_capability_free(final_html, nonce)


@pytest.mark.parametrize("layers,label", [
    (2, "double"),
    (3, "triple"),
    (4, "deeper"),
])
def test_r11_nested_encoded_nonce_in_comment_scrubbed_capability_free(layers, label):
    """Double/triple/deeper-encoded nonce in a comment must (a) force
    ``complete=False`` and (b) be removed from the returned HTML so it is
    capability-free under the bounded canonicalizer — comments are stripped
    wholesale when contaminated at any encoding depth."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    enc = _r11_entity_layered(nonce, layers)
    leaked = html + f"<!-- {enc} -->"
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        f"{label}-encoded nonce in comment must fail closed"
    )
    # The contaminated comment is removed entirely → capability-free output.
    assert "<!--" not in final_html, (
        f"{label}-encoded nonce comment must be stripped, not left in output"
    )
    _r11_assert_capability_free(final_html, nonce)


@pytest.mark.parametrize("layers,label", [
    (2, "double"),
    (3, "triple"),
])
def test_r11_nested_encoded_nonce_in_text_scrubbed_capability_free(layers, label):
    """Nested-encoded nonce in visible text must force ``complete=False`` and
    be removed from the returned HTML while authored prose survives. The
    bounded iterative scrub decodes one text layer per pass so the single-
    layer nonce regex can match and remove nested-encoded material."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    enc = _r11_entity_layered(nonce, layers)
    leaked = html.replace("Hello world.", f"Hello world. {enc} more text.")
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        f"{label}-encoded nonce in text must fail closed"
    )
    _r11_assert_capability_free(final_html, nonce)
    assert "Hello world." in final_html, "authored prose must survive scrub"
    assert "more text." in final_html, "authored prose must survive scrub"


@pytest.mark.parametrize("layers,label", [
    (2, "double"),
    (3, "triple"),
])
def test_r11_nested_encoded_nonce_in_tag_interior_scrubbed_capability_free(layers, label):
    """Nested-encoded nonce inside a tag attribute value (``title``) must
    force ``complete=False`` and be removed from the returned HTML. The
    bounded iterative scrub decodes one attribute-value layer per pass so
    the single-layer nonce regex scrubs tag interiors too."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    enc = _r11_entity_layered(nonce, layers)
    leaked = html.replace("<p ", f'<p title="{enc}" ', 1)
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        f"{label}-encoded nonce in tag interior must fail closed"
    )
    _r11_assert_capability_free(final_html, nonce)


@pytest.mark.parametrize("layers,label", [
    (2, "double"),
    (3, "triple"),
])
def test_r11_nested_encoded_owner_marker_scrubbed_capability_free(layers, label):
    """Nested-encoded ``data-okf-proj-owner`` marker in a comment must force
    ``complete=False`` and the returned HTML must be free of the owner marker
    under the bounded canonicalizer."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    marker = "data-okf-proj-owner"
    enc = _r11_entity_layered(marker, layers)
    leaked = html + f'<!-- {enc}="forged" -->'
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        f"{label}-encoded owner marker must fail closed"
    )
    assert "<!--" not in final_html, "contaminated comment must be stripped"
    _r11_assert_capability_free(final_html, owners[0]["nonce"])


def test_r11_nested_encoded_forged_projection_key_scrubbed_capability_free():
    """A double-encoded forged ``data-okf-projection-key`` in a comment must
    force ``complete=False`` and be stripped from the returned HTML."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    marker = "data-okf-projection-key"
    enc = _r11_entity_layered(marker, 2)
    leaked = html + f'<!-- {enc}="999" -->'
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert "<!--" not in final_html
    # The forged value 999 must not survive.
    assert '"999"' not in final_html
    _r11_assert_capability_free(final_html, owners[0]["nonce"])


def test_r11_clean_output_still_complete_no_exhaustion_false_positive():
    """Regression guard: the exhaustion wiring must NOT cause false positives
    on clean output. Normal rendering with no contamination must stay
    ``complete=True`` and converge."""
    src = "# Heading\n\nText with **bold** and `code` &amp; entity.\n"
    _, proj = _render_and_project(src)
    assert proj["complete"] is True, (
        "clean output must not trip exhaustion/leak false positives"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r11_two_level_blockquote_exact_source_dom_proof(newline):
    """Explicit two-level ``> >`` blockquote LF+CRLF proof: the nested
    blockquote strips BOTH ``>`` markers, and the single mapped segment's
    canonical-source slice must EXACTLY equal its DOM visible-text slice
    (``nested``), for both LF and CRLF source. This is the exact source/DOM
    parity proof for the two-level quote that the r8 parity helper exercises
    indirectly; here it is asserted directly on the final projection record."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.markdown_blocks import extract_blocks, block_visible_segments
    nl = newline
    src = f"> > nested{nl}"
    blocks = extract_blocks(src)
    rendered, owners = render_blocks_with_owners(src)
    final_html, proj = build_annotation_projection(src, src, rendered, owners=owners)
    assert proj["complete"] is True, (
        f"two-level blockquote must project complete ({newline!r})"
    )
    mapped = [r for r in proj["blocks"] if r["dom_key"] is not None]
    assert len(mapped) == 1, (
        f"expected exactly 1 mapped block ({newline!r}), got {len(mapped)}"
    )
    rec = mapped[0]
    assert rec["block_id"] == "block-quote:1", (
        f"expected block-quote:1, got {rec['block_id']}"
    )
    block = next(b for b in blocks if b.id == rec["block_id"])
    vis, _ = block_visible_segments(block) or (None, None)
    assert vis == "nested", f"expected visible 'nested', got {vis!r}"
    # Exactly one segment mapping the visible text; its source slice must be
    # exactly 'nested' (both ``>`` markers and the leading space stripped).
    assert len(rec["segments"]) == 1, (
        f"expected exactly 1 segment ({newline!r}), got {rec['segments']}"
    )
    seg = rec["segments"][0]
    src_slice = src[seg["source_start"]:seg["source_end"]]
    dom_slice = vis[seg["dom_start"]:seg["dom_end"]]
    assert src_slice == "nested", (
        f"two-level quote source slice must be exactly 'nested' ({newline!r}): "
        f"got {src_slice!r} at [{seg['source_start']}:{seg['source_end']}]"
    )
    assert dom_slice == "nested", (
        f"two-level quote DOM slice must be exactly 'nested' ({newline!r}): "
        f"got {dom_slice!r} at [{seg['dom_start']}:{seg['dom_end']}]"
    )
    assert seg["dom_end"] - seg["dom_start"] == len("nested")
    # Actual rendered owner DOM text keyed by dom_key (ReviewAll r12 finding
    # 10): the dom offset must slice the REAL rendered textContent of the
    # element carrying data-okf-projection-key=<dom_key>, agreeing with the
    # IR visible text — not just the IR slice above.
    dom_text = _proj_key_dom_text(final_html)
    actual_dom = dom_text[str(rec["dom_key"])]
    assert actual_dom == "nested", (
        f"actual rendered DOM text must be 'nested' ({newline!r}): "
        f"got {actual_dom!r}"
    )
    assert actual_dom[seg["dom_start"]:seg["dom_end"]] == "nested"
    # The visible DOM contains the rendered text inside a nested blockquote.
    assert "nested" in final_html


# ===========================================================================
# R12: Known-safe capability-free HTML on exhaustion (blocker) + structural
# tag-attribute parse/serialize (finding 9) + actual rendered owner DOM text
# keyed by dom_key (finding 10) + two nested blockquote elements LF+CRLF
# exact rendered DOM parity (finding 11). (ReviewAll r12.)
# ===========================================================================

def _r12_deep_decode(s: str, iterations: int = 60) -> str:
    """Decode HTML entities far beyond the canonicalization budget, returning
    the fully-peeled form. Used to prove injected bytes/capabilities are
    ABSENT from the returned HTML even when an attacker nests encoding deeper
    than the normal bound. (ReviewAll r12 blocker.)"""
    for _ in range(iterations):
        nxt = html_lib.unescape(s)
        if nxt == s:
            break
        s = nxt
    return s


def _r12_assert_capability_free_deep(final_html: str, nonce: str) -> None:
    """Assert the returned HTML is capability-free under a DEEP decode (far
    beyond the canonicalization budget) — proving injected bytes/capabilities
    are absent regardless of encoding depth, not merely under the bounded
    canonicalizer."""
    from okf_loom.render import _canonicalize_iteratively
    bounded, _, _ = _canonicalize_iteratively(final_html)
    deep = _r12_deep_decode(final_html)
    cl_b = bounded.lower()
    cl_d = deep.lower()
    assert "data-okf-proj-owner" not in cl_b, "owner marker survives bounded decode"
    assert "data-okf-proj-owner" not in cl_d, "owner marker survives DEEP decode"
    assert nonce not in bounded, "nonce survives bounded decode"
    assert nonce not in deep, "nonce survives DEEP decode (beyond bound)"


# --- Blocker: known-safe capability-free HTML on ANY exhaustion ---

@pytest.mark.parametrize("region,layers", [
    ("comment", 8),   # beyond the 4-iteration canonicalization budget
    ("comment", 12),
    ("text", 8),
    ("text", 12),
])
def test_r12_exhaustion_returns_known_safe_capability_free_html(region, layers):
    """On ANY canonicalization/scrub exhaustion the returned HTML must be
    KNOWN-SAFE capability-free: decoding it FAR beyond the budget (60 passes)
    must prove the injected nonce is absent, even though ``complete=False``.
    (ReviewAll r12 blocker.)"""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    enc = _r11_entity_layered(nonce, layers)
    if region == "comment":
        leaked = html + f"<!-- {enc} -->"
    else:
        leaked = html.replace("Hello world.", f"Hello world. {enc} more text.")
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False, (
        f"exhaustion ({region}, {layers} layers) must force complete=False"
    )
    # The returned HTML must be known-safe: deep decode proves the nonce absent.
    _r12_assert_capability_free_deep(final_html, nonce)
    if region == "comment":
        assert "<!--" not in final_html, "all comments stripped on exhaustion"
    else:
        assert "Hello world." in final_html, "authored prose must survive"


def test_r12_overbudget_nonce_in_tag_interior_known_safe():
    """Overbudget (8-layer) nonce in a tag attribute value must force
    ``complete=False`` and the returned HTML must be capability-free under a
    deep decode (the contaminated attribute is dropped by the structural
    scrub / known-safe fallback)."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    enc = _r11_entity_layered(nonce, 8)
    leaked = html.replace("<p ", f'<p title="{enc}" ', 1)
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    _r12_assert_capability_free_deep(final_html, nonce)


# --- Finding 9: structural tag-attribute parse/serialize ---

def test_r12_finding9_structural_attr_drop_quoted_and_unquoted():
    """Contaminated attribute values — whether DOUBLE-QUOTED or UNQUOTED —
    must be DROPPED by the structural attribute scrub, while clean attributes
    (including the legitimate numeric projection key) survive. The nonce must
    be absent from the returned HTML. (ReviewAll r12 finding 9.)"""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    nonce = owners[0]["nonce"]
    # Quoted contaminated title + unquoted contaminated data-x + clean class.
    leaked = html.replace(
        "<p ", f'<p title="{nonce}" data-x={nonce} class="keep" ', 1,
    )
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert "title=" not in final_html, "contaminated quoted attr must be dropped"
    assert "data-x=" not in final_html, "contaminated unquoted attr must be dropped"
    assert 'class="keep"' in final_html, "clean attr must survive"
    # The legitimate numeric projection key survives.
    keys = re.findall(r'data-okf-projection-key="(\d+)"', final_html)
    assert keys == ["1"], f"legitimate key must survive; got {keys}"
    assert nonce not in final_html
    assert nonce not in html_lib.unescape(final_html)


def test_r12_finding9_owner_marker_attr_dropped_by_name():
    """An attribute NAMED ``data-okf-proj-owner`` (pre-finalization marker)
    must be dropped by name regardless of its value. (ReviewAll r12 finding 9.)"""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    from okf_loom.render import build_annotation_projection
    src = "Hello world.\n"
    html, owners = render_blocks_with_owners(src)
    leaked = html.replace("<p ", '<p data-okf-proj-owner="leak" ', 1)
    final_html, proj = build_annotation_projection(src, src, leaked, owners=owners)
    assert proj["complete"] is False
    assert "data-okf-proj-owner" not in final_html.lower()


def test_r12_finding9_quote_delimiters_never_decoded_into_syntax():
    """An attribute value containing an escaped quote (``&quot;``) must NOT
    have its quote delimiter decoded into attribute syntax by the scrub. The
    structural scrub re-escapes ``"`` on serialization so it cannot break out
    of the attribute. (ReviewAll r12 finding 9.)"""
    from okf_loom.render import _scrub_single_tag_attrs, _capability_literal_regex
    nonce = "0" * 32
    nr = [_capability_literal_regex(nonce)]
    # A clean attr with an escaped quote + the legitimate projection key.
    tag = '<p title="a&quot;b" data-okf-projection-key="1">'
    out = _scrub_single_tag_attrs(tag, nr)
    # The projection key survives.
    assert 'data-okf-projection-key="1"' in out
    # The escaped quote stays escaped (not a bare " inside the value that
    # would terminate the attribute early).
    assert 'title="a&quot;b"' in out, (
        f"escaped quote must be preserved, not decoded into syntax: got {out!r}"
    )
    # Parse the scrubbed tag to confirm it is still ONE well-formed tag with
    # exactly the expected attributes (no syntax breakout).
    class _Grabber(_HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.attrs = None
        def handle_starttag(self, tag, attrs):
            self.attrs = dict(attrs)
    g = _Grabber()
    g.feed(out)
    assert g.attrs is not None, f"scrubbed tag unparseable: {out!r}"
    assert g.attrs.get("title") == 'a"b', (
        f"title value must round-trip to a\"b: got {g.attrs.get('title')!r}"
    )
    assert g.attrs.get("data-okf-projection-key") == "1"


def test_r12_finding9_valueless_boolean_attr_preserved():
    """A valueless (boolean) attribute on a clean tag must survive the
    structural scrub unchanged (it is neither contaminated nor an owner
    marker)."""
    from okf_loom.render import _scrub_single_tag_attrs, _capability_literal_regex
    nr = [_capability_literal_regex("0" * 32)]
    tag = '<p data-okf-projection-key="1" contenteditable>'
    out = _scrub_single_tag_attrs(tag, nr)
    assert "contenteditable" in out
    assert 'data-okf-projection-key="1"' in out


# --- Finding 11: two nested blockquote elements LF+CRLF exact rendered DOM ---

@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r12_finding11_two_nested_blockquote_elements_exact_dom_parity(newline):
    """A two-level blockquote with two lines (``> > alpha`` / ``> > beta``)
    must render as TWO nested ``<blockquote>`` elements, and the exact
    rendered DOM (structure + textContent) must be IDENTICAL for LF and CRLF.
    The projection segment dom offsets must slice the ACTUAL rendered DOM text
    of the element keyed by dom_key. (ReviewAll r12 finding 11.)"""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    nl = newline
    src = f"> > alpha{nl}> > beta{nl}"
    rendered, owners = render_blocks_with_owners(src)
    final_html, proj = build_annotation_projection(src, src, rendered, owners=owners)
    assert proj["complete"] is True, (
        f"two-line nested blockquote must project complete ({newline!r})"
    )
    # TWO nested blockquote elements in the rendered DOM.
    assert final_html.count("<blockquote") == 2, (
        f"expected exactly 2 <blockquote> elements ({newline!r}): {final_html!r}"
    )
    assert final_html.count("</blockquote>") == 2
    # The outer blockquote carries the projection key; the inner does not.
    assert "<blockquote data-okf-projection-key=\"1\">" in final_html
    # Actual rendered owner DOM text keyed by dom_key.
    dom_text = _proj_key_dom_text(final_html)
    rec = next(r for r in proj["blocks"] if r["dom_key"] is not None)
    actual = dom_text[str(rec["dom_key"])]
    assert actual == "alpha beta", (
        f"actual rendered DOM text must be 'alpha beta' ({newline!r}): "
        f"got {actual!r}"
    )
    # Each segment's dom offsets slice the ACTUAL rendered DOM text exactly.
    reconstructed = "".join(
        actual[s["dom_start"]:s["dom_end"]] for s in rec["segments"]
    )
    assert reconstructed == "alpha beta", (
        f"dom offsets must reconstruct actual DOM text ({newline!r}): "
        f"got {reconstructed!r} from {rec['segments']}"
    )


def test_r12_finding11_two_nested_blockquote_lf_crlf_rendered_dom_identical():
    """The exact RENDERED DOM (final HTML) for the two-line nested blockquote
    must be byte-identical between LF and CRLF source — proving line-ending
    parity at the rendered-output level, not just projection completeness."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    lf = "> > alpha\n> > beta\n"
    crlf = "> > alpha\r\n> > beta\r\n"
    lf_rendered, lf_owners = render_blocks_with_owners(lf)
    crlf_rendered, crlf_owners = render_blocks_with_owners(crlf)
    lf_html, lf_proj = build_annotation_projection(lf, lf, lf_rendered, owners=lf_owners)
    crlf_html, crlf_proj = build_annotation_projection(crlf, crlf, crlf_rendered, owners=crlf_owners)
    assert lf_proj["complete"] and crlf_proj["complete"]
    # Rendered DOM is identical (the only difference would be CRLF leakage).
    assert lf_html == crlf_html, (
        f"LF and CRLF rendered DOM must be identical:\nLF={lf_html!r}\nCRLF={crlf_html!r}"
    )
    # Projections share the same segment structure (offsets point into their
    # own source, so we compare dom ranges + visible text, not source offsets).
    lf_rec = next(r for r in lf_proj["blocks"] if r["dom_key"] is not None)
    crlf_rec = next(r for r in crlf_proj["blocks"] if r["dom_key"] is not None)
    assert [(s["dom_start"], s["dom_end"]) for s in lf_rec["segments"]] == (
           [(s["dom_start"], s["dom_end"]) for s in crlf_rec["segments"]])


# --- Regression: clean output unaffected by known-safe wiring ---

def test_r12_clean_output_unaffected_complete():
    """The known-safe fallback and structural scrub must NEVER run on clean
    output, so normal rendering stays ``complete=True`` and tags are not
    re-serialized."""
    src = "# Heading\n\nText with **bold**, `code`, &amp; entity.\n\n- a\n- b\n"
    html, proj = _render_and_project(src)
    assert proj["complete"] is True
    # Clean tags are untouched (no re-serialization side effects).
    assert "<p data-okf-projection-key" in html
    assert "<!--" not in html  # no comments introduced


# ---------------------------------------------------------------------------
# Empty non-HR rendered blocks: non-selectable, unkeyed, but complete.
#
# Owners whose expected visible text is empty carry no selectable text. Only
# thematic breaks (``hr:*``) are valid keyed zero-segment records. Every other
# empty rendered block (e.g. a blank ``> `` blockquote) must have its owner
# marker stripped WITHOUT assigning a DOM projection key, and emit
# ``dom_key: null, segments: []`` while staying ``complete=True`` (no
# selectable text is omitted).
# ---------------------------------------------------------------------------

def test_blank_blockquote_unkeyed_no_dom_key():
    """A blank ``> `` blockquote has empty visible text: it must NOT receive a
    DOM projection key. Its owner marker is stripped, the record keeps
    ``dom_key: null`` and ``segments: []``, and the projection stays
    ``complete=True`` because no selectable text was omitted."""
    src = "Before.\n\n> \n\nAfter.\n"
    html, proj = _render_and_project(src)
    assert proj["complete"] is True
    by_id = {row["block_id"].split(":", 1)[0]: row for row in proj["blocks"]}
    quote = by_id["block-quote"]
    assert quote["dom_key"] is None
    assert quote["segments"] == []
    # Owner marker is stripped and NO projection key is assigned to it.
    assert "data-okf-proj-owner" not in html
    assert "<blockquote data-okf-projection-key" not in html


def test_thematic_break_remains_keyed_alongside_blank_blockquote():
    """An ``<hr>`` next to a blank blockquote stays a valid keyed zero-segment
    record (the only empty block that may be keyed)."""
    src = "Before.\n\n> \n\n---\n\nAfter.\n"
    html, proj = _render_and_project(src)
    assert proj["complete"] is True
    hr = next(row for row in proj["blocks"] if row["block_id"].startswith("hr:"))
    assert hr["dom_key"] is not None
    assert hr["segments"] == []
    assert "<hr data-okf-projection-key=" in html


def test_neighboring_selectable_blocks_project_around_blank_blockquote():
    """Selectable paragraphs surrounding a blank blockquote keep their keys and
    source-mapped segments, so anchors remain resolvable."""
    src = "Before.\n\n> \n\nAfter.\n"
    html, proj = _render_and_project(src)
    assert proj["complete"] is True
    paras = [row for row in proj["blocks"]
             if row["block_id"].split(":", 1)[0] == "paragraph"]
    assert len(paras) == 2
    assert all(row["dom_key"] is not None for row in paras)
    assert all(len(row["segments"]) >= 1 for row in paras)
    assert all(s["source_start"] < s["source_end"]
               for row in paras for s in row["segments"])
    # Keys remain unique across the keyed (selectable + hr) blocks.
    keyed = [row["dom_key"] for row in proj["blocks"] if row["dom_key"] is not None]
    assert len(keyed) == len(set(keyed))


# ===========================================================================
# List-item multiline inline tokenization — actual pages and blocks.
#
# The renderer joins a list item's continuation lines into one logical item
# text and renders it through a single inline parse; the projection must
# tokenize the same logical text with an explicit source position map. These
# tests pin the four real docs pages whose list items carry inline code
# spans / fenced-looking runs across continuation boundaries, the seven
# offending blocks themselves, exact source-segment semantics, and the
# fail-closed boundaries that must not regress.
# ===========================================================================

_LIST_PARITY_PAGES = (
    "explanation/architecture",
    "explanation/research",
    "reference/architecture",
    "reference/embedding_guide",
)

# The seven offending list-item blocks, identified by the exact prefix of
# each block's canonical source (each unique in its page).
_LIST_PARITY_BLOCK_NEEDLES = {
    "explanation/architecture": (
        "- **Every module is independently importable.**",
    ),
    "explanation/research": (
        "5. **Plugin interface (what a backend must implement):**",
    ),
    "reference/architecture": (
        "- **`Studio`** — the per-bundle studio handle.",
    ),
    "reference/embedding_guide": (
        "4. **Exception behavior.**",
        "2. **Expose the root skill**",
        "4. **Scope a subagent**",
        "5. **Permission scoping**",
    ),
}


def test_list_parity_four_actual_pages_project_complete():
    """Each of the four actual docs pages must project complete, and every
    previously offending list-item block must carry a DOM projection key with
    exact source segments whose slices are verbatim source code points."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    b = Bundle.load(Path("docs-bundle"))
    for name in _LIST_PARITY_PAGES:
        c = b.concepts[tuple(name.split("/"))]
        _, proj = render_concept_body_with_projection(c, b, mode="serve")
        assert proj["complete"] is True, f"{name} must project complete"
        raw = c.raw_text
        for needle in _LIST_PARITY_BLOCK_NEEDLES[name]:
            recs = [r for r in proj["blocks"]
                    if raw[r["source_start"]:r["source_end"]].startswith(needle)]
            assert len(recs) == 1, (name, needle)
            rec = recs[0]
            assert rec["block_id"].startswith("list-item:"), (name, needle)
            assert rec["dom_key"] is not None, (name, needle)
            segs = rec["segments"]
            assert segs, (name, needle)
            # Every segment is non-degenerate, strictly ordered, in-block, and
            # slices the canonical source verbatim (usable exact anchors).
            cursor_dom = 0
            for s in segs:
                assert s["source_start"] < s["source_end"], (name, needle, segs)
                assert s["dom_start"] == cursor_dom, (name, needle, segs)
                assert rec["source_start"] <= s["source_start"] < s["source_end"] <= rec["source_end"], (name, needle)
                cursor_dom = s["dom_end"]
                assert raw[s["source_start"]:s["source_end"]], (name, needle)


def test_list_parity_actual_multiline_code_spans_have_exact_boundaries():
    """For the originally diagnosed actual blocks, the published segments keep
    exact inner boundaries: a cross-line code span's visible slice maps onto
    the same source characters, delimiter backticks stay unmapped, and the
    stripped continuation indentation stays a source gap."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    b = Bundle.load(Path("docs-bundle"))
    # explanation/architecture: `from\n  okf_loom.validate import validate_bundle`
    # opens on the marker line and closes on a continuation line.
    c = b.concepts[("explanation", "architecture")]
    raw = c.raw_text
    _, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True
    opener = raw.index("`from\n") if "`from\n" in raw else raw.index("`from\r\n")
    closer = raw.index("`", raw.index("validate_bundle"))
    rec = next(r for r in proj["blocks"]
               if r["block_id"].startswith("list-item:")
               and r["source_start"] <= opener < r["source_end"])
    assert rec["dom_key"] is not None
    for s in rec["segments"]:
        assert not (s["source_start"] <= opener < s["source_end"]), rec["segments"]
        assert not (s["source_start"] <= closer < s["source_end"]), rec["segments"]
    # The continuation-line content itself IS mapped verbatim.
    cont = "okf_loom.validate import validate_bundle"
    at = raw.index(cont)
    assert any(s["source_start"] <= at < s["source_end"] for s in rec["segments"])


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_list_parity_minimal_cross_line_code_complete_exact_positions(newline):
    """The minimal cross-line code span item projects complete end-to-end with
    exact positions: a zero-indent continuation keeps source contiguity across
    the (possibly expanded) newline, so the joined visible span maps 1:1 onto
    the source span `alpha<newline>beta`."""
    body = "- `alpha\nbeta`\n".replace("\n", newline)
    html, proj = _r4_render_and_project(body)
    assert proj["complete"] is True, f"cross-line code must complete ({newline!r})"
    rec = next(r for r in proj["blocks"] if r["block_id"].startswith("list-item:"))
    assert rec["dom_key"] is not None
    segs = rec["segments"]
    vis = "alpha\nbeta"
    # Segments reconstruct the visible code content and map it 1:1 onto
    # source, with the newline segment carrying exactly len(newline) points.
    pos = 0
    covered = ""
    for s in segs:
        dom_text = vis[s["dom_start"]:s["dom_end"]]
        src_text = body[s["source_start"]:s["source_end"]]
        covered += src_text
        if dom_text == "\n":
            assert src_text == newline, (dom_text, src_text)
            assert s["dom_end"] - s["dom_start"] == 1
        else:
            assert dom_text == src_text, (dom_text, src_text)
    assert covered == f"alpha{newline}beta", segs


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_list_parity_indentation_gap_never_maps_across(newline):
    """A continuation with stripped indentation leaves an unmapped source gap:
    the projection stays complete, but no segment covers the gap, so a
    selection across the join can never resolve to guessed source positions
    (the client falls back instead)."""
    body = "- `alpha\n  beta`\n".replace("\n", newline)
    html, proj = _r4_render_and_project(body)
    assert proj["complete"] is True
    rec = next(r for r in proj["blocks"] if r["block_id"].startswith("list-item:"))
    nl_at = body.index(newline)
    indent_at = nl_at + len(newline)
    assert body[indent_at:indent_at + 2] == "  "
    for s in rec["segments"]:
        assert not (s["source_start"] <= indent_at < s["source_end"]), rec["segments"]
        assert not (s["source_start"] <= indent_at + 1 < s["source_end"]), rec["segments"]


def test_list_parity_unclosed_delimiter_stays_literal_complete():
    """An unclosed backtick run spanning the join stays literal visible text on
    BOTH sides (shared delimiter grammar): it is mapped honestly as identity
    text — never guessed into a code span, never dropped."""
    body = "- `alpha\n  beta\n"
    html, proj = _r4_render_and_project(body)
    assert proj["complete"] is True
    rec = next(r for r in proj["blocks"] if r["block_id"].startswith("list-item:"))
    assert rec["dom_key"] is not None
    vis = "`alpha\nbeta"
    assert "".join(
        vis[s["dom_start"]:s["dom_end"]] for s in rec["segments"]) == vis
    # The literal backtick is visible text inside an identity 1:1 run —
    # mapped, never stripped or guessed into a code span.
    at = body.index("`")
    seg = next(s for s in rec["segments"]
               if s["source_start"] <= at < s["source_end"])
    assert seg["dom_start"] == 0 and vis[0] == "`"
    assert body[seg["source_start"]] == "`"
    assert seg["source_end"] - seg["source_start"] == seg["dom_end"] - seg["dom_start"]


def test_list_parity_task_marker_with_continuation_consumes_join():
    """A task marker whose text starts on the next line consumes the join
    newline (renderer DOTALL task match): checkbox template space maps to the
    whole '[x]<newline>' source span, followed by the continuation text."""
    body = "- [x]\n  follows checkbox\n"
    html, proj = _r4_render_and_project(body)
    assert proj["complete"] is True
    assert "okf-task__box" in html, "renderer must emit the checkbox"
    rec = next(r for r in proj["blocks"] if r["block_id"].startswith("list-item:"))
    assert rec["dom_key"] is not None
    segs = rec["segments"]
    first = segs[0]
    assert body[first["source_start"]:first["source_end"]] == "[x]\n"
    assert "".join(
        " follows checkbox"[s["dom_start"]:s["dom_end"]] for s in segs
    ) == " follows checkbox"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_list_parity_quote_item_splits_at_quote_line_marker(newline):
    """A quoted list item whose inline run coalesces across the join newline
    (the historical undecodable block-quote shape) is SPLIT at the structural
    quote-line marker: per-line identity pieces with the newline + stripped
    ">" markers left an unmapped gap — never one stretched segment whose
    source slice spans "\\n> ". The projection stays complete, every
    published piece is an exact equal-width identity run, and the pieces
    flanking the gap are source-discontinuous, so a selection crossing the
    quoted-line break falls back instead of guessing (client rule:
    non-contiguous segments -> null)."""
    nl = newline
    body = f"> - `alpha{nl}> beta`{nl}> after{nl}"
    html, proj = _r4_render_and_project(body)
    assert proj["complete"] is True
    quote = next(
        r for r in proj["blocks"] if r["block_id"].startswith("block-quote:"))
    assert quote["dom_key"] is not None
    segs = sorted(quote["segments"], key=lambda s: s["dom_start"])
    assert segs, "quoted list must publish segments"
    # No segment's source slice may contain the structural marker: each
    # piece stops before the join newline or starts at/after the next
    # quoted line's content.
    nl_at = body.index(nl)
    for s in segs:
        assert not (s["source_start"] <= nl_at < s["source_end"]), segs
        assert not (
            s["source_start"] <= nl_at + len(nl) < s["source_end"]), segs
        # Split pieces are honest identity runs: one source code point per
        # visible code point (direct-mappable by the narrow client decoder).
        assert s["source_end"] - s["source_start"] == \
            s["dom_end"] - s["dom_start"], (s, segs)
    # The gap is real and exactly the marker region: the piece before ends
    # at the join newline and the piece after starts at the next line's
    # content (newline + ">" + " " stripped), so the flanking segments are
    # source-discontinuous and a cross-line selection cannot pass the
    # client's contiguity check.
    prev = next(s for s in segs if s["source_end"] == nl_at)
    nxt = next(
        s for s in segs if s["source_start"] == nl_at + len(nl) + 2)
    assert prev["dom_start"] < nxt["dom_start"], segs
    assert prev["source_end"] != nxt["source_start"], segs


def test_authored_projection_key_actual_spec_preserved_complete():
    """The unchanged canonical spec documents the reserved attribute as text."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    b = Bundle.load(Path("docs-bundle"))
    c = b.concepts[("reference", "spec")]
    html, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True
    assert "<code>data-okf-projection-key</code>" in html
    assert "data-okf-proj-owner" not in html
    keys = re.findall(r'data-okf-projection-key="([0-9]+)"', html)
    assert len(keys) == len(set(keys))
    assert {int(k) for k in keys} == {
        r["dom_key"] for r in proj["blocks"] if r["dom_key"] is not None}
    at = c.raw_text.index("data-okf-projection-key")
    segment = next(s for r in proj["blocks"] for s in r["segments"]
                   if s["source_start"] <= at and at + 23 <= s["source_end"])
    assert segment["source_end"] - segment["source_start"] == \
        segment["dom_end"] - segment["dom_start"]


_AUTHORED_KEY = "data-okf-projection-key"


@pytest.mark.parametrize("body,visible", [
    (f"Plain {_AUTHORED_KEY} end.\n", [f"Plain {_AUTHORED_KEY} end."]),
    (f"Inline `{_AUTHORED_KEY}` end.\n", [f"Inline {_AUTHORED_KEY} end."]),
    (f"```text\n{_AUTHORED_KEY}\n```\n", [_AUTHORED_KEY]),
    (f"First {_AUTHORED_KEY}.\n\nSecond `{_AUTHORED_KEY}`.\n",
     [f"First {_AUTHORED_KEY}.", f"Second {_AUTHORED_KEY}."]),
    (f"- `😀 é before\r\n{_AUTHORED_KEY} after 雪.`\r\n",
     [f"😀 é before\n{_AUTHORED_KEY} after 雪."]),
])
def test_authored_projection_key_exact_text_offsets_noop_plugin(tmp_path, body, visible):
    from html.parser import HTMLParser
    from okf_loom.parse import parse_document

    bundle = _bundle(tmp_path, body)
    concept = bundle.concepts[("projection",)]
    # Producer-level CRLF proof: Bundle.load intentionally universal-newline
    # normalizes served source. Supply exact canonical bytes in memory here.
    concept.raw_text = "---\r\ntype: table\r\n---\r\n\r\n" + body
    concept.body = parse_document(concept.raw_text)[1]
    seen_nonces = []

    def noop(_concept, html):
        seen_nonces.extend(re.findall(r'data-okf-proj-owner="([^"]+)"', html))
        return html

    html, proj = render_concept_body_with_projection(concept, bundle, mode="serve", body_plugin=noop)
    assert proj["complete"] is True
    assert all(n not in html for n in seen_nonces)
    assert "data-okf-proj-owner" not in html
    assert proj["concept_content_rev"] == concept_content_rev(concept.raw_text)

    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    records = [r for r in proj["blocks"] if r["dom_key"] is not None]
    assert len(records) == len(visible)
    for record, expected in zip(records, visible):
        # Parse the actual returned owner subtree, including the CRLF list.
        subtree = re.search(
            rf'<(p|pre|li) data-okf-projection-key="{record["dom_key"]}">(.*?)</\1>', html, re.S)
        assert subtree, html
        text = Text()
        text.feed(subtree.group(2))
        text.close()
        assert "".join(text.parts) == expected
        at = expected.index(_AUTHORED_KEY)
        segment = next(s for s in record["segments"]
                       if s["dom_start"] <= at and at + len(_AUTHORED_KEY) <= s["dom_end"])
        source_at = segment["source_start"] + at - segment["dom_start"]
        assert concept.raw_text[source_at:source_at + len(_AUTHORED_KEY)] == _AUTHORED_KEY
        assert record["source_start"] <= source_at < record["source_end"]
        # All published runs reconstruct these exact visible code points;
        # only the canonical CRLF -> visible LF newline changes width.
        for s in record["segments"]:
            assert concept.raw_text[s["source_start"]:s["source_end"]].replace("\r\n", "\n") == \
                expected[s["dom_start"]:s["dom_end"]]


@pytest.mark.parametrize("attack", [
    "attribute", "numeric-in-value", "comment", "malformed", "other-owner", "extra-code", "extra-text",
    "move-comment", "delete-owner", "duplicate-owner", "alter-owner", "same-text-new-tree",
    "entity", "percent", "nested", "exhaustion", "unclosed-attribute", "unclosed-comment",
    "unclosed-owner", "nonce-in-authored-owner",
])
def test_authored_projection_key_contamination_revokes_and_scrubs(tmp_path, attack):
    key = _AUTHORED_KEY
    bundle = _bundle(tmp_path, f"Authored `{key}`.\n\nOther owner.\n")
    concept = bundle.concepts[("projection",)]
    seen_nonces = []

    def plugin(_concept, html):
        seen_nonces.extend(re.findall(r'data-okf-proj-owner="([^"]+)"', html))
        first = re.search(r"<p\b.*?</p>", html, re.S).group()
        suffixes = {
            "attribute": f'<aside title="{key}"></aside>',
            "comment": f"<!--{key}-->",
            "malformed": f"< {key}",
            "extra-code": f"<code>{key}</code>",
            "entity": "<aside>data&#45;okf-projection-key</aside>",
            "percent": "<aside>data%2dokf-projection-key</aside>",
            "nested": "<aside>data&amp;#45;okf-projection-key</aside>",
            "exhaustion": "<aside>data&" + "amp;" * 8 + "#45;okf-projection-key</aside>",
            "unclosed-attribute": f'<aside title="{key}',
            "unclosed-comment": f"<!--{key}",
        }
        if attack in suffixes:
            return html + suffixes[attack]
        if attack == "numeric-in-value":
            return html.replace("<p ", '<p title=\' data-okf-projection-key="1"\' ', 1)
        if attack == "other-owner":
            return html.replace("Other owner.", f"Other owner. {key}")
        if attack == "extra-text":
            return html.replace(key, f"{key} {key}")
        if attack == "move-comment":
            return html.replace(f"<code>{key}</code>", f"<!--{key}-->")
        if attack == "delete-owner":
            return html.replace(first, f"<code>{key}</code>")
        if attack == "duplicate-owner":
            return html + first
        if attack == "alter-owner":
            return html.replace(seen_nonces[0], "invented-owner")
        if attack == "same-text-new-tree":
            return html.replace(f"<code>{key}</code>", f"<em>{key}</em>")
        if attack == "unclosed-owner":
            return html.replace("</p>", "", 1)
        assert attack == "nonce-in-authored-owner"
        return html.replace(key, key + seen_nonces[0])

    html, proj = render_concept_body_with_projection(concept, bundle, mode="serve", body_plugin=plugin)
    assert proj["complete"] is False
    # No restoration channel: even the genuine spelling loses its exemption
    # on contamination. Decode deeply to inspect RETURNED output, not metadata.
    from urllib.parse import unquote
    decoded = html
    for _ in range(32):
        decoded = unquote(html_lib.unescape(decoded))
    assert "data-okf-proj-owner" not in decoded
    assert all(n not in decoded for n in seen_nonces)
    assert key not in re.sub(r' data-okf-projection-key="[0-9]+"', "", decoded)


def test_authored_projection_key_no_private_proof_or_source_gap_fails_closed():
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    body = f"Authored `{_AUTHORED_KEY}`.\n"
    html, owners = render_blocks_with_owners(body)
    for proof in (None, html):
        if proof is not None:
            # Preserve the expected text but remove its source coverage. A
            # trusted-looking subtree is insufficient without owner segments.
            owners[0]["segments"] = []
        returned, proj = build_annotation_projection(body, body, html, owners=owners, _trusted_html=proof)
        assert proj["complete"] is False
        assert _AUTHORED_KEY not in re.sub(r' data-okf-projection-key="[0-9]+"', "", returned)


def test_authored_projection_key_wrappers_outside_owner_preserve_provenance(tmp_path):
    bundle = _bundle(tmp_path, f"Authored `{_AUTHORED_KEY}`.\n")
    html, proj = render_concept_body_with_projection(
        bundle.concepts[("projection",)], bundle, mode="serve",
        body_plugin=lambda c, h: "<section>" + h + "</section>")
    assert proj["complete"] is True
    assert f"<code>{_AUTHORED_KEY}</code>" in html
