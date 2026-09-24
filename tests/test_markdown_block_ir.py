"""Focused architecture proof for the canonical block IR.

Tests verify:
1. IR parser block structure and source spans
2. Grammar parity: extractor ↔ renderer for all declared constructs
3. Projection identities/segments/completeness
4. Plugin ownership marker isolation
5. LF/CRLF fixture matrix
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from okf_loom.markdown_blocks import (
    CANONICAL_BLOCK_IR_VERSION,
    extract_blocks,
    block_hash,
    block_visible_segments,
    generate_owner_nonce,
    inject_owner_markers,
    find_owner_markers,
    verify_owner_markers,
)
from okf_loom.viewer.markdown import markdown_to_html


# ---------------------------------------------------------------------------
# IR version
# ---------------------------------------------------------------------------

def test_ir_version_is_1():
    assert CANONICAL_BLOCK_IR_VERSION == 1


def test_projection_includes_ir_version(tmp_path):
    """Projection metadata includes block_ir_version."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\nHello.\n"
    (tmp_path / "x.md").write_text(raw)
    b = Bundle.load(tmp_path)
    _, proj = render_concept_body_with_projection(b.concepts[("x",)], b, mode="serve")
    assert proj.get("block_ir_version") == 1


# ---------------------------------------------------------------------------
# Grammar parity: extractor ↔ renderer
# ---------------------------------------------------------------------------

def test_atx_heading():
    blocks = extract_blocks("# H1\n\nText.\n")
    headings = [b for b in blocks if b.kind == "heading"]
    assert len(headings) == 1
    assert "H1" in headings[0].text
    html = markdown_to_html("# H1\n\nText.\n")
    assert "<h1" in html


def test_setext_h1():
    blocks = extract_blocks("Title\n=====\n\nText.\n")
    headings = [b for b in blocks if b.kind == "heading"]
    assert len(headings) == 1
    assert "Title" in headings[0].text
    html = markdown_to_html("Title\n=====\n\nText.\n")
    assert "<h1" in html


def test_setext_h2():
    blocks = extract_blocks("Title\n-----\n\nText.\n")
    headings = [b for b in blocks if b.kind == "heading"]
    assert len(headings) == 1
    assert "Title" in headings[0].text
    html = markdown_to_html("Title\n-----\nText.\n")
    assert "<h2" in html


def test_thematic_break():
    blocks = extract_blocks("Before\n\n---\n\nAfter\n")
    # --- is thematic break (not setext, because preceded by blank line)
    kinds = [b.kind for b in blocks]
    assert "paragraph" in kinds  # Before, After


def test_ordered_dot_list():
    blocks = extract_blocks("1. First\n2. Second\n")
    items = [b for b in blocks if b.kind == "list-item"]
    assert len(items) == 2
    html = markdown_to_html("1. First\n2. Second\n")
    assert "<li" in html
    assert "<ol" in html


def test_ordered_paren_list():
    blocks = extract_blocks("1) First\n2) Second\n")
    items = [b for b in blocks if b.kind == "list-item"]
    assert len(items) == 2
    html = markdown_to_html("1) First\n2) Second\n")
    assert "<li" in html


def test_nested_list():
    blocks = extract_blocks("- Outer\n  - Inner\n")
    items = [b for b in blocks if b.kind == "list-item"]
    assert len(items) >= 1
    html = markdown_to_html("- Outer\n  - Inner\n")
    assert "<li" in html


def test_lazy_blockquote():
    blocks = extract_blocks("> Quoted\nlazy continuation\n\nAfter.\n")
    quotes = [b for b in blocks if b.kind == "block-quote"]
    assert len(quotes) >= 1
    assert "lazy" in quotes[0].text


def test_pipe_prose_not_table():
    """Prose containing pipes is classified as paragraph for projection,
    even though the extractor may label it 'table-row'."""
    from okf_loom.render import _projection_tags_for_block
    blocks = extract_blocks("The `x` points at [[a|B]].\n")
    assert len(blocks) >= 1
    # The projection tag mapper must map pipe prose to p tags.
    tags = _projection_tags_for_block(blocks[0].kind, blocks[0].text)
    assert "p" in tags, f"pipe prose should map to p tags, got {tags}"


def test_table_with_separator():
    blocks = extract_blocks("| A | B |\n|---|---|\n| 1 | 2 |\n")
    rows = [b for b in blocks if b.kind == "table-row"]
    assert len(rows) >= 2  # header + data (separator may be filtered)


def test_fenced_code_backtick():
    blocks = extract_blocks("```\ncode\n```\n")
    code_blocks = [b for b in blocks if b.kind == "fenced-code"]
    assert len(code_blocks) == 1
    assert "code" in code_blocks[0].text


def test_fenced_code_tilde():
    blocks = extract_blocks("~~~\ncode\n~~~\n")
    code_blocks = [b for b in blocks if b.kind == "fenced-code"]
    assert len(code_blocks) == 1


def test_indented_code():
    blocks = extract_blocks("    code line\n")
    code_blocks = [b for b in blocks if b.kind == "indented-code"]
    assert len(code_blocks) == 1


def test_soft_wrapped_paragraph():
    blocks = extract_blocks("First line\nsecond line\n")
    paras = [b for b in blocks if b.kind == "paragraph"]
    assert len(paras) == 1
    assert "second" in paras[0].text


# ---------------------------------------------------------------------------
# LF/CRLF parity
# ---------------------------------------------------------------------------

def test_crlf_same_block_count():
    body = "# Heading\n\nParagraph one.\n\nParagraph two.\n"
    lf_blocks = extract_blocks(body)
    crlf_blocks = extract_blocks(body.replace("\n", "\r\n"))
    assert len(lf_blocks) == len(crlf_blocks)
    assert [b.kind for b in lf_blocks] == [b.kind for b in crlf_blocks]


def test_crlf_same_block_hashes():
    body = "# Heading\n\nParagraph.\n"
    lf_blocks = extract_blocks(body)
    crlf_blocks = extract_blocks(body.replace("\n", "\r\n"))
    for lf_b, crlf_b in zip(lf_blocks, crlf_blocks):
        assert lf_b.hash == crlf_b.hash, "CRLF→LF normalization must produce same hash"


def test_crlf_entity_and_emoji():
    body = "# H\n\nText &amp; 😀 é.\n"
    lf_blocks = extract_blocks(body)
    crlf_blocks = extract_blocks(body.replace("\n", "\r\n"))
    assert len(lf_blocks) == len(crlf_blocks)


# ---------------------------------------------------------------------------
# Projection completeness
# ---------------------------------------------------------------------------

def test_demo_bundle_projection_complete(tmp_path):
    import shutil
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    src = Path("samples/demo_bundle")
    dst = tmp_path / "bundle"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".okf-loom"))
    b = Bundle.load(dst)
    c = b.concepts[("tables", "orders")]
    _, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True
    assert proj["block_ir_version"] == 1


def test_abc_ac_reproducer_fails():
    """The critical reproducer: source 'abc' → DOM 'ac' must be incomplete."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    html, owners = render_blocks_with_owners("abc")
    html_bad = html.replace(">abc<", ">ac<")  # drop 'b'
    _, proj = build_annotation_projection("abc", "abc", html_bad, owners=owners)
    assert proj["complete"] is False


def test_abc_abc_complete():
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    html, owners = render_blocks_with_owners("abc")
    _, proj = build_annotation_projection("abc", "abc", html, owners=owners)
    assert proj["complete"] is True


# ---------------------------------------------------------------------------
# Plugin ownership markers
# ---------------------------------------------------------------------------

def test_owner_nonce_is_32_hex():
    nonce = generate_owner_nonce()
    assert len(nonce) == 32
    assert re.match(r"^[0-9a-f]{32}$", nonce)


def test_owner_nonce_unique():
    nonces = {generate_owner_nonce() for _ in range(100)}
    assert len(nonces) == 100


def test_inject_and_find_markers():
    # Inject into proper start tags — structural parser only finds owner
    # attributes on start tags, not closing tags or text content.
    n1, n2 = generate_owner_nonce(), generate_owner_nonce()
    marked = f'<p data-okf-proj-owner="{n1}">text</p><h2 data-okf-proj-owner="{n2}">heading</h2>'
    markers = find_owner_markers(marked)
    assert len(markers) == 2  # p and h2


def test_verify_markers_all_present():
    n1 = generate_owner_nonce()
    marked = f'<p data-okf-proj-owner="{n1}">text</p>'
    pre_nonces = [m["nonce"] for m in find_owner_markers(marked)]
    valid, errors = verify_owner_markers(pre_nonces, marked)
    assert not errors
    assert len(valid) == 1


def test_verify_markers_detect_removal():
    n1 = generate_owner_nonce()
    marked = f'<p data-okf-proj-owner="{n1}">text</p>'
    pre_nonces = [m["nonce"] for m in find_owner_markers(marked)]
    post_plugin = '<div>nothing</div>'
    valid, errors = verify_owner_markers(pre_nonces, post_plugin)
    assert len(errors) > 0
    assert any("removed" in e for e in errors)


def test_verify_markers_detect_duplication():
    n1 = generate_owner_nonce()
    marked = f'<p data-okf-proj-owner="{n1}">text</p>'
    pre_nonces = [m["nonce"] for m in find_owner_markers(marked)]
    post_plugin = marked + marked
    valid, errors = verify_owner_markers(pre_nonces, post_plugin)
    assert len(errors) > 0
    assert any("duplicated" in e for e in errors)


def test_verify_markers_detect_forgery():
    n1 = generate_owner_nonce()
    marked = f'<p data-okf-proj-owner="{n1}">text</p>'
    pre_nonces = [m["nonce"] for m in find_owner_markers(marked)]
    forged = generate_owner_nonce()
    post_plugin = marked.replace("</p>", f'<p data-okf-proj-owner="{forged}">forged</p></p>')
    valid, errors = verify_owner_markers(pre_nonces, post_plugin)
    assert len(errors) > 0
    assert any("forged" in e or "invented" in e for e in errors)


def test_forged_numeric_key_does_not_survive():
    """A plugin-injected data-okf-projection-key must not survive in
    final projection blocks."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    html, owners = render_blocks_with_owners("hello")
    # Plugin injects a forged numeric key.
    html_forged = html + '<p data-okf-projection-key="999">forged</p>'
    _, proj = build_annotation_projection("hello", "hello", html_forged, owners=owners)
    for block in proj["blocks"]:
        if block.get("dom_key") is not None:
            assert block["dom_key"] != 999


# ===========================================================================
# Structural gate tests — assert old heuristics are ABSENT and owner functions
# have PRODUCTION callers. These MUST fail against the prior fake implementation.
# ===========================================================================

def test_old_projection_helpers_absent_from_render():
    """Gate #5: old post-hoc heuristics must not exist in render.py."""
    import inspect
    from okf_loom import render as render_mod
    source = inspect.getsource(render_mod)
    removed = [
        "_ProjectionHTMLScanner",
        "_MD_SYNTAX_GAP_RE",
        "_is_valid_syntax_gap",
        "_compute_no_match_zones",
        "_boundary_segments",
        "_inject_projection_key",
        "consumed_blocks",
    ]
    for name in removed:
        # Check for the name as a definition or call (word boundary).
        assert re.search(r'\b' + re.escape(name) + r'\b', source) is None, \
            f"old heuristic {name!r} still present in render.py"


def test_old_independent_split_blocks_parsing_absent():
    """Gate #3: _split_blocks must not independently parse headings/lists/
    quotes/tables/fences — it delegates to parse_markdown_blocks."""
    import inspect
    from okf_loom.viewer import markdown as md_mod
    src = inspect.getsource(md_mod._split_blocks)
    assert "parse_markdown_blocks" in src, "_split_blocks must call parse_markdown_blocks"
    # Must NOT contain its own line-by-line block-starting scanner loop.
    assert "while i < n:" not in src, "_split_blocks must not have a line-by-line scanner"


def test_markdown_blocks_does_not_import_annotations():
    """Gate #1: markdown_blocks must not import annotations (no cycle)."""
    import inspect
    from okf_loom import markdown_blocks as mb_mod
    src = inspect.getsource(mb_mod)
    assert "from .annotations import" not in src, \
        "markdown_blocks must not import from annotations (would create a cycle)"
    assert "from .annotations " not in src, \
        "markdown_blocks must not import from annotations (would create a cycle)"


def test_owner_functions_have_production_callers():
    """Gate #4: owner-marker functions must be called in production code, not
    only in tests."""
    import inspect
    from okf_loom import render as render_mod
    from okf_loom.viewer import markdown as md_mod
    render_src = inspect.getsource(render_mod)
    md_src = inspect.getsource(md_mod)
    # generate_owner_nonce must be called in the renderer (production).
    assert "generate_owner_nonce" in md_src, \
        "generate_owner_nonce must have a production caller in viewer/markdown.py"
    # find_owner_markers must be called in render.py (production finalizer).
    assert "find_owner_markers" in render_src, \
        "find_owner_markers must have a production caller in render.py"
    # block_visible_segments must be called in the renderer (production).
    assert "block_visible_segments" in md_src, \
        "block_visible_segments must have a production caller in viewer/markdown.py"


# ===========================================================================
# Gate #7: Plugin production-path tests across initial/live/static.
# ===========================================================================

def _plugin_bundle(tmp_path, body):
    raw = "---\ntype: note\ntitle: T\n---\n" + body
    (tmp_path / "x.md").write_text(raw)
    from okf_loom import Bundle
    return Bundle.load(tmp_path)


def test_plugin_prepend_same_tag_keeps_complete(tmp_path):
    """Prepending a <p> does not break completeness (plugin-added semantics
    are ignored)."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nAlpha\n\nBeta\n")
    c = b.concepts[("x",)]
    def prepend(concept, html):
        return "<p>prepended</p>" + html
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=prepend)
    assert proj["complete"] is True


def test_plugin_append_same_text_keeps_complete(tmp_path):
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nAlpha\n")
    c = b.concepts[("x",)]
    def append_same(concept, html):
        return html + "<p>Alpha</p>"
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=append_same)
    assert proj["complete"] is True


def test_plugin_interleave_same_tag_keeps_complete(tmp_path):
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nFirst\n\nSecond\n")
    c = b.concepts[("x",)]
    def interleave(concept, html):
        import re
        # Insert between the two paragraphs.
        return re.sub(r"(<p\s[^>]*>Second</p>)", r'<p>middle</p>\1', html)
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=interleave)
    assert proj["complete"] is True


def test_plugin_movement_keeps_complete(tmp_path):
    """Moving a paragraph (cut + paste elsewhere) keeps completeness as long
    as the owner survives exactly once with exact text."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nMove me\n\nStay\n")
    c = b.concepts[("x",)]
    def move(concept, html):
        import re
        moved = re.sub(r'<p\s[^>]*>Move me</p>\s*', '', html)
        return moved + re.search(r'<p\s[^>]*>Move me</p>', html).group(0) \
            if re.search(r'<p\s[^>]*>Move me</p>', html) else moved
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=move)
    assert proj["complete"] is True


def test_plugin_replacement_text_makes_incomplete(tmp_path):
    """Replacing visible text of an owned block makes it incomplete."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nOriginal\n")
    c = b.concepts[("x",)]
    def replace(concept, html):
        return html.replace(">Original<", ">Changed<")
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=replace)
    assert proj["complete"] is False


def test_plugin_repeated_deletion_makes_incomplete(tmp_path):
    """Deleting (removing) one of two same-text blocks makes it incomplete."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nDup\n\nDup\n")
    c = b.concepts[("x",)]
    def delete_one(concept, html):
        import re
        # Remove the FIRST <p>Dup</p>.
        return re.sub(r'<p\s[^>]*>Dup</p>', '', html, count=1)
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=delete_one)
    assert proj["complete"] is False


def test_plugin_copied_owner_fails_closed(tmp_path):
    """A plugin that COPIES an owned element (duplicating the owner marker)
    must fail closed."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nUnique\n")
    c = b.concepts[("x",)]
    def copy_owner(concept, html):
        import re
        match = re.search(r'<p\s[^>]*>Unique</p>', html)
        if match:
            return html + match.group(0)  # duplicate the element WITH its owner
        return html
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=copy_owner)
    assert proj["complete"] is False


def test_plugin_invented_owner_fails_closed(tmp_path):
    """A plugin that INJECTS a new owner marker fails closed."""
    from okf_loom.render import render_concept_body_with_projection
    from okf_loom.markdown_blocks import generate_owner_nonce
    b = _plugin_bundle(tmp_path, "# H\n\nText\n")
    c = b.concepts[("x",)]
    forged = generate_owner_nonce()
    def invent(concept, html):
        return html + f'<p data-okf-proj-owner="{forged}">invented</p>'
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=invent)
    assert proj["complete"] is False


def test_plugin_forged_numeric_key_stripped(tmp_path):
    """A plugin that injects a forged data-okf-projection-key has it stripped,
    and the fail-closed contract forces complete=False (any pre-finalization
    projection-key occurrence must force incomplete, ReviewAll r7)."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nText\n")
    c = b.concepts[("x",)]
    def forge_key(concept, html):
        return html + '<p data-okf-projection-key="888">forged key</p>'
    html, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=forge_key)
    assert proj["complete"] is False, (
        "pre-finalization data-okf-projection-key must force complete=False"
    )
    assert 'data-okf-projection-key="888"' not in html


def test_plugin_malformed_html_keeps_complete_if_owners_intact(tmp_path):
    """Malformed plugin HTML that does not touch owned elements does not break
    completeness."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nText\n")
    c = b.concepts[("x",)]
    def malformed(concept, html):
        return html + '<div><span>unclosed'
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=malformed)
    assert proj["complete"] is True


def test_plugin_closed_noscript_beside_owners_keeps_complete(tmp_path):
    """A plugin-added noscript fallback that holds no owned text is ignored."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nText\n")
    c = b.concepts[("x",)]
    def fallback(concept, html):
        return html + "<noscript><p>Enable JavaScript</p></noscript>"
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=fallback)
    assert proj["complete"] is True


@pytest.mark.parametrize("plugin", [
    # An element left open around the owners: the browser nests them in it.
    pytest.param(lambda html: "<div>" + html, id="unclosed-wrapper"),
    # An open select drops the owners' tags in a browser (in-select mode).
    pytest.param(lambda html: "<select>" + html, id="unclosed-select"),
    # A truncated trailing start tag is parser recovery, never trusted.
    pytest.param(lambda html: html + "<div", id="truncated-tag"),
    # noscript content is unrendered raw text in a scripting browser, so a
    # wrapped body leaves zero keyed elements in the DOM.
    pytest.param(lambda html: "<noscript>" + html + "</noscript>",
                 id="noscript-wrapped-owners"),
    pytest.param(lambda html: re.sub(r">Text</p>", "><noscript>Text</noscript></p>", html),
                 id="noscript-inside-owner"),
    # noscript raw text ends at the first </noscript, even inside an
    # attribute value or comment: the textarea after it swallows the owners.
    pytest.param(lambda html: '<noscript><i title="</noscript><textarea>"></i></noscript>' + html,
                 id="noscript-end-in-attribute"),
    pytest.param(lambda html: "<noscript><!-- </noscript><textarea> --></noscript>" + html,
                 id="noscript-end-in-comment"),
])
def test_plugin_owner_wrapping_constructs_make_incomplete(tmp_path, plugin):
    """Open or raw-text constructs that hold owned elements fail closed even
    though every owner survives once with its exact text."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nText\n")
    c = b.concepts[("x",)]
    _, proj = render_concept_body_with_projection(
        c, b, mode="serve", body_plugin=lambda concept, html: plugin(html))
    assert proj["complete"] is False


_PARAGRAPH_BODY = "# H\n\nText\n"
_TABLE_BODY = "# H\n\n| A | B |\n|---|---|\n| c1 | c2 |\n"
_NBSP_TABLE_BODY = "# H\n\n| A | B |\n|---|---|\n| &nbsp; | &nbsp; |\n"
# A fenced block whose code starts with a blank line: the owner text starts
# with a line feed.
_LEADING_LF_FENCE_BODY = "# H\n\n```\n\nfoo\n```\n"


def _in_owned_p(inner):
    return lambda html: re.sub(r"(<p [^>]*>)Text</p>", r"\g<1>" + inner + "</p>", html)


def _around_owned_p(before, inner, after):
    return lambda html: re.sub(r"(<p [^>]*>)Text</p>",
                               before + r"\g<1>" + inner + "</p>" + after, html)


def _nbsp_cells(replacement):
    return lambda html: html.replace("<td>&nbsp;</td><td>&nbsp;</td>", replacement)


# Balanced replacements that keep every genuine owner nonce and its exact
# lexical text, while HTML5 tree construction moves that text out of the
# owner. Chromium proof for each: tests/test_projection_relocation_browser.py.
_RELOCATIONS = [
    # HTML5 closes an open p in button scope at a block start tag, even
    # through intervening inline elements; the text lands after the p.
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("<span><div>Text</div></span>"), id="p-span-div"),
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("<em><span><div>Text</div></span></em>"),
                 id="p-em-span-div"),
    # The slash is ignored: a self-closing hr still closes the paragraph.
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("<span><hr/>Text</span>"), id="p-span-hr-self-closing"),
    # Anything but a cell directly inside a row is foster-parented out of
    # the table, taking its text with it.
    pytest.param(_TABLE_BODY, lambda html: html.replace(
        "<td>c1</td><td>c2</td>", "<span>c1</span><span>c2</span>"), id="tr-spans"),
    pytest.param(_TABLE_BODY, lambda html: html.replace("<td>c1</td>", "<span>c1</span>"),
                 id="tr-td-and-span"),
    # Character references are text too: resolved, they are foster-parented.
    pytest.param(_TABLE_BODY, lambda html: html.replace(
        "<td>c1</td><td>c2</td>", "&#99;&#49;&#99;&#50;"), id="tr-charref-text"),
    # U+00A0 is not HTML whitespace, whether literal or referenced.
    pytest.param(_NBSP_TABLE_BODY, _nbsp_cells("\u00a0\u00a0"), id="tr-literal-nbsp"),
    pytest.param(_NBSP_TABLE_BODY, _nbsp_cells("&nbsp;&nbsp;"), id="tr-nbsp-reference"),
    # A heading start pops the open heading: the inner one becomes a sibling.
    pytest.param(_PARAGRAPH_BODY, lambda html: re.sub(
        r"(<h2 [^>]*>)H</h2>", r"\g<1><h3>H</h3></h2>", html), id="heading-in-heading"),
    # Ruby annotation starts with ruby in scope generate implied end tags,
    # popping the paragraph.
    pytest.param(_PARAGRAPH_BODY, _around_owned_p("<ruby>", "<rt>Text</rt>", "</ruby>"),
                 id="ruby-rt-in-p"),
    pytest.param(_PARAGRAPH_BODY, _around_owned_p("<ruby>", "<rp>Text</rp>", "</ruby>"),
                 id="ruby-rp-in-p"),
    pytest.param(_PARAGRAPH_BODY, _around_owned_p("<ruby>", "<rb>Text</rb>", "</ruby>"),
                 id="ruby-rb-in-p"),
    pytest.param(_PARAGRAPH_BODY, _around_owned_p("<ruby>", "<rtc>Text</rtc>", "</ruby>"),
                 id="ruby-rtc-in-p"),
    # A button start with a button in scope closes everything up to it.
    pytest.param(_PARAGRAPH_BODY, _around_owned_p("<button>", "<button>Text</button>", "</button>"),
                 id="button-in-p-in-button"),
    # The parser drops a line feed directly after a pre start tag.
    pytest.param(_LEADING_LF_FENCE_BODY, lambda html: re.sub(
        r"(<pre [^>]*>)<code>(.*?)</code></pre>", r"\g<1>\2</pre>", html, flags=re.S),
        id="pre-leading-lf"),
    pytest.param(_LEADING_LF_FENCE_BODY, lambda html: re.sub(
        r"(<pre [^>]*>)<code>\n(.*?)</code></pre>", r"\g<1>&#10;\2</pre>", html, flags=re.S),
        id="pre-leading-lf-reference"),
    # A table part inside a cell closes the cell and whatever is open in it.
    pytest.param(_PARAGRAPH_BODY, _around_owned_p(
        "<table><tr><td>", "Te<tbody><tr><td>xt</td></tr></tbody>", "</td></tr></table>"),
        id="tbody-in-p-in-cell"),
    # Without their table, the body parser drops the owner rows' table parts
    # (the table part is the owner's ancestor, not the owner or inside it).
    pytest.param(_TABLE_BODY, lambda html: re.sub(r"<table[^>]*>|</table>", "", html),
                 id="table-parts-without-table"),
]


@pytest.mark.parametrize("body, plugin", _RELOCATIONS)
def test_plugin_html5_relocation_out_of_owner_makes_incomplete(tmp_path, body, plugin):
    """Balanced plugin HTML that keeps every genuine owner nonce and its exact
    lexical text still fails closed when HTML5 tree construction moves that
    text out of the owner (browser proof: test_projection_relocation_browser)."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, body)
    c = b.concepts[("x",)]
    _, clean = render_concept_body_with_projection(c, b, mode="serve")
    assert clean["complete"] is True
    def relocate(concept, html):
        changed = plugin(html)
        assert changed != html, "fixture transformation did not apply"
        return changed
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=relocate)
    assert proj["complete"] is False


# Table starts that are not allowlisted but, inside a cell, close it like the
# allowlisted table parts do.
_TABLE_STARTS_IN_CELL = [
    pytest.param("<caption></caption>", id="caption"),
    pytest.param("<colgroup></colgroup>", id="colgroup"),
    pytest.param("<col>", id="col"),
    pytest.param("<tfoot></tfoot>", id="tfoot"),
]


def _owned_p_after_table_start_in_cell(start):
    return _around_owned_p("<table><tr><td><div>" + start + "</div>", "Text", "</td></tr></table>")


def _owner_row_after_table_start_in_cell(start):
    return lambda html: html.replace("<tbody>", "<tbody><tr><td><div>" + start + "</div></td></tr>", 1)


@pytest.mark.parametrize("start", _TABLE_STARTS_IN_CELL)
@pytest.mark.parametrize("body, placement", [
    # The start closes the cell, so the rest of it, the owned paragraph
    # included, is foster-parented out of the table.
    pytest.param(_PARAGRAPH_BODY, _owned_p_after_table_start_in_cell, id="owner-later-in-cell"),
    # Chromium keeps a later owner row keyed with its text; the start is
    # still a table part outside its required parent.
    pytest.param(_TABLE_BODY, _owner_row_after_table_start_in_cell, id="owner-row-after-cell"),
])
def test_plugin_table_start_in_cell_makes_incomplete(tmp_path, body, placement, start):
    """A caption, colgroup, col or tfoot start must start in its required
    parent (the table, or a colgroup for col) anywhere in the document
    (browser proof: test_projection_relocation_browser)."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, body)
    c = b.concepts[("x",)]
    plugin = placement(start)
    def relocate(concept, html):
        changed = plugin(html)
        assert changed != html, "fixture transformation did not apply"
        return changed
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=relocate)
    assert proj["complete"] is False


# The source reference &#13; is a CR in both Python and HTML5.
_CR_REFERENCE_BODY = "# H\n\nA&#13;B\n"

# Balanced plugin HTML that keeps every genuine owner nonce and the text
# Python's parser credits to it, while HTML5 tokenization gives the owner
# different text or none. Chromium proof: test_projection_relocation_browser.
_TOKENIZATION_DIFFERENCES = [
    # A comment that is not well-formed HTML5 syntax ends elsewhere in a
    # browser: "<!-->" and "<!--->" are whole comments there.
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("<!-->x-->Text"), id="comment-abrupt-close"),
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("<!--->x-->Text"), id="comment-abrupt-dash-close"),
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("<!--x--!>y-->Text"), id="comment-bang-close"),
    # The hidden opener precedes the owner: the browser's textarea swallows it.
    pytest.param(_PARAGRAPH_BODY, _around_owned_p(
        "<!--><textarea>-->", "Text", "<!--</textarea>-->"), id="comment-hides-textarea"),
    # Python drops these references; HTML5 emits the code point.
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("Te&#1;xt"), id="control-reference"),
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("Te&#x7F;xt"), id="delete-reference"),
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("Te&#xFDD0;xt"), id="noncharacter-reference"),
    # HTML5 turns a literal CR into LF; the reference stays a CR.
    pytest.param(_CR_REFERENCE_BODY, lambda html: html.replace("&#13;", "\r"), id="literal-cr"),
]


@pytest.mark.parametrize("body, plugin", _TOKENIZATION_DIFFERENCES)
def test_plugin_html5_tokenization_difference_makes_incomplete(tmp_path, body, plugin):
    """Admitted owner text must be the text HTML5 produces, and only
    well-formed HTML5 comments are admitted anywhere in the document."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, body)
    c = b.concepts[("x",)]
    _, clean = render_concept_body_with_projection(c, b, mode="serve")
    assert clean["complete"] is True
    def tokenize(concept, html):
        changed = plugin(html)
        assert changed != html, "fixture transformation did not apply"
        return changed
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=tokenize)
    assert proj["complete"] is False


@pytest.mark.parametrize("plugin", [
    pytest.param(None, id="renderer-output"),
    pytest.param(lambda html: html.replace("A\x00B", "<i>A\x00B</i>"), id="plugin-output"),
])
def test_literal_nul_makes_incomplete(tmp_path, plugin):
    """HTML5 drops a literal NUL from body text (and replaces it elsewhere)
    while Python keeps it, in the renderer's own output as in a plugin's."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, "# H\n\nA\x00B\n")
    c = b.concepts[("x",)]
    def wrap(concept, html):
        changed = plugin(html)
        assert changed != html, "fixture transformation did not apply"
        return changed
    html, proj = render_concept_body_with_projection(
        c, b, mode="serve", body_plugin=wrap if plugin else None)
    assert "A\x00B" in html
    assert proj["complete"] is False


@pytest.mark.parametrize("body, plugin", [
    # Nested "<!--" and a trailing "<!-" end where Python ends them, but are
    # not HTML5 comment syntax either.
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("<!--a<!--b-->Text"), id="comment-nested-open"),
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("<!--a<!--->Text"), id="comment-ends-bang-dash"),
    # The hidden opener need not touch an owner.
    pytest.param(_PARAGRAPH_BODY, lambda html: html + "<!-->", id="comment-abrupt-close-after"),
    # Unclosed at the end of the document.
    pytest.param(_PARAGRAPH_BODY, lambda html: html + "<!-- open", id="comment-unclosed"),
])
def test_plugin_malformed_comment_anywhere_makes_incomplete(tmp_path, body, plugin):
    """Only well-formed HTML5 comments are admitted, even where Python and a
    browser happen to agree or no owner is near."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, body)
    c = b.concepts[("x",)]
    def comment(concept, html):
        changed = plugin(html)
        assert changed != html, "fixture transformation did not apply"
        return changed
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=comment)
    assert proj["complete"] is False


@pytest.mark.parametrize("plugin", [
    # Chromium's select parser keeps this owner and its text, but in-select
    # tree construction varies by browser and is not modelled.
    pytest.param(_around_owned_p("<select>", "Text", "</select>"), id="select-around-p"),
    pytest.param(_in_owned_p("<select><option>Text</option></select>"), id="option-in-p"),
    pytest.param(_around_owned_p("<form>", "Text", "</form>"), id="form-around-p"),
    pytest.param(_in_owned_p("<nobr>Text</nobr>"), id="nobr-in-p"),
    pytest.param(_around_owned_p("<ruby>", "Text", "</ruby>"), id="ruby-around-p"),
    pytest.param(_in_owned_p("<okf-widget>Text</okf-widget>"), id="custom-element-in-p"),
    pytest.param(_around_owned_p("<okf-widget>", "Text", "</okf-widget>"),
                 id="custom-element-around-p"),
    # An anchor start closes an open anchor (adoption agency).
    pytest.param(_in_owned_p("<a href='#x'>Te<a href='#y'>xt</a></a>"),
                 id="anchor-in-anchor-in-p"),
])
def test_plugin_unmodelled_element_around_or_in_owner_makes_incomplete(tmp_path, plugin):
    """Only elements whose tree construction admission models may enclose or
    sit inside a keyed owner, nested the way the parser keeps them; anything
    else fails closed even when a browser happens to keep the owner text in
    place."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, _PARAGRAPH_BODY)
    c = b.concepts[("x",)]
    def wrap(concept, html):
        changed = plugin(html)
        assert changed != html, "fixture transformation did not apply"
        return changed
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=wrap)
    assert proj["complete"] is False


@pytest.mark.parametrize("body, plugin", [
    pytest.param(_PARAGRAPH_BODY, _around_owned_p(
        "<article><header>", "<mark>Te</mark><b>x</b><kbd>t</kbd>", "</header></article>"),
        id="containers-and-phrasing"),
    pytest.param(_PARAGRAPH_BODY, _around_owned_p(
        "<figure>", "<a href='#x'><small>Text</small></a>", "<figcaption>c</figcaption></figure>"),
        id="figure-and-link"),
    pytest.param(_PARAGRAPH_BODY, _around_owned_p("<dl><dt>t</dt><dd>", "Text", "</dd></dl>"),
                 id="definition-list"),
    pytest.param(_TABLE_BODY, lambda html: html.replace(
        "<td>c1</td>", "<td><i>c</i>1</td>").replace("<table", "<section><table")
        .replace("</table>", "</table></section>"), id="table-cell-phrasing"),
    # HTML ASCII whitespace directly in a table section stays in place.
    pytest.param(_TABLE_BODY, lambda html: html.replace("<tbody>", "<tbody>\t\f\r &#32;&#10;"),
                 id="table-ascii-whitespace"),
    # Well-formed HTML5 comments, in and around owners.
    pytest.param(_PARAGRAPH_BODY, _around_owned_p(
        "<!---->", "<!-- a -- b - -->Te<!--a->b-->xt", "<!--<!-->"), id="well-formed-comments"),
    # A complete table with a footer elsewhere keeps its table parts.
    pytest.param(_PARAGRAPH_BODY, lambda html: html + (
        "<table><tfoot><tr><td>f</td></tr></tfoot></table>"), id="table-footer-elsewhere"),
    # Captions and column groups directly in the owner rows' table; HTML5
    # implies the colgroup of a col directly in a table.
    pytest.param(_TABLE_BODY, lambda html: re.sub(
        r"(<table[^>]*>)", r"\1<caption>Cap</caption><colgroup><col><col/></colgroup>", html, count=1),
        id="table-caption-and-colgroup"),
    pytest.param(_TABLE_BODY, lambda html: re.sub(r"(<table[^>]*>)", r"\1<col><col>", html, count=1),
                 id="table-cols"),
    # A literal CR outside owners changes no owner text.
    pytest.param(_PARAGRAPH_BODY, lambda html: html.replace("\n", "\r\n"), id="crlf-between-owners"),
    # References Python decodes exactly as HTML5 does.
    pytest.param(_PARAGRAPH_BODY, _in_owned_p("&#84;&#x65;xt"), id="equal-references"),
    pytest.param("# H\n\nA&#x81;B\n", lambda html: html.replace("&#x81;", "&#129;"),
                 id="unmapped-c1-reference"),
    pytest.param(_CR_REFERENCE_BODY, lambda html: html.replace("&#13;", "&#x0D;"),
                 id="cr-reference"),
])
def test_plugin_modelled_wrappers_and_phrasing_keep_complete(tmp_path, body, plugin):
    """Allowlisted containers around an owner and plain phrasing inside it
    keep the map complete while the owner text is unchanged."""
    from okf_loom.render import render_concept_body_with_projection
    b = _plugin_bundle(tmp_path, body)
    c = b.concepts[("x",)]
    def wrap(concept, html):
        changed = plugin(html)
        assert changed != html, "fixture transformation did not apply"
        return changed
    _, proj = render_concept_body_with_projection(c, b, mode="serve", body_plugin=wrap)
    assert proj["complete"] is True


def test_plugin_parity_initial_live_static(tmp_path):
    """Gate #7: initial-page, live /__data/doc, and static build share the
    same projection for the same plugin."""
    from okf_loom.render import render_concept_body_with_projection, build_site
    from okf_loom.viewer.assets import auto_palette
    import json as _json
    b = _plugin_bundle(tmp_path, "# H\n\nTarget.\n")
    c = b.concepts[("x",)]
    injection = '<span data-plugin="x">P</span>'
    def plugin_fn(concept, html):
        return html + injection
    # Live path.
    live_html, live_proj = render_concept_body_with_projection(
        c, b, mode="serve", body_plugin=plugin_fn)
    # Static path.
    static_html, static_proj = render_concept_body_with_projection(
        c, b, mode="static", body_plugin=plugin_fn)
    assert live_proj == static_proj
    assert live_proj["complete"] is True


# ---------------------------------------------------------------------------
# R4: IR visible-text correctness for every listed construct
# ---------------------------------------------------------------------------

def test_r4_ir_list_continuation_line_visible():
    """Continuation lines in list items produce visible text."""
    from okf_loom.markdown_blocks import block_visible_segments
    blocks = extract_blocks("- first\n  continued line\n- second\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "first\ncontinued line", f"expected continuation visible, got {vis!r}"


def test_r4_ir_task_list_checkbox_removed():
    """GFM task-list checkbox markers are stripped from visible text."""
    from okf_loom.markdown_blocks import block_visible_segments
    blocks = extract_blocks("- [ ] todo\n- [x] done\n")
    vis0, _ = block_visible_segments(blocks[0])
    vis1, _ = block_visible_segments(blocks[1])
    assert vis0 == " todo", f"expected ' todo', got {vis0!r}"
    assert vis1 == " done", f"expected ' done', got {vis1!r}"


def test_r4_ir_indented_code_no_trailing_newline():
    """Indented code visible text has no trailing newline."""
    from okf_loom.markdown_blocks import block_visible_segments
    blocks = extract_blocks("    code line 1\n    code line 2\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "code line 1\ncode line 2", f"got {vis!r}"
    assert not vis.endswith("\n"), "no trailing newline"


def test_r4_ir_html_block_preserves_newlines():
    """Multiline HTML visible text preserves newlines."""
    from okf_loom.markdown_blocks import block_visible_segments
    blocks = extract_blocks("<div>\n  <p>nested</p>\n</div>\n")
    vis, _ = block_visible_segments(blocks[0])
    assert "\n" in vis, f"newlines must be preserved, got {vis!r}"


def test_r4_ir_footnote_visible_matches_li_textcontent():
    """Footnote definition visible text matches the rendered <li> textContent."""
    from okf_loom.markdown_blocks import block_visible_segments
    blocks = extract_blocks("Text.\n\n[^1]: footnote text\n")
    fn_block = [b for b in blocks if "[^1]:" in b.text][0]
    vis, _ = block_visible_segments(fn_block)
    # Renderer produces <li>...footnote text ↩</li>, textContent is "footnote text ↩"
    assert vis.endswith("\u21a9"), f"must end with back-arrow, got {vis!r}"
    assert "footnote text" in vis


def test_r4_ir_escaped_table_pipe_correct_offsets():
    """Escaped pipe \\| maps to a 2-source-char non-identity segment."""
    from okf_loom.markdown_blocks import block_visible_segments
    blocks = extract_blocks("| a \\| b | c |\n|---|---|\n| x | y |\n")
    header = blocks[0]
    vis, segs = block_visible_segments(header)
    assert "| b" in vis, f"escaped pipe must be visible as |, got {vis!r}"
    # The escaped pipe source span must be 2 chars wide (\|).
    pipe_segs = [s for s in segs if s["source_end"] - s["source_start"] == 2
                 and s["dom_end"] - s["dom_start"] == 1]
    assert len(pipe_segs) >= 1, f"expected a 2-src-char→1-dom-char segment for \\|, got {segs}"


def test_r4_ir_setext_heading_visible():
    """Setext headings produce correct visible text."""
    from okf_loom.markdown_blocks import block_visible_segments
    blocks = extract_blocks("Title\n=====\n\nBody.\n")
    heading = [b for b in blocks if b.kind == "heading"][0]
    vis, _ = block_visible_segments(heading)
    assert vis == "Title", f"got {vis!r}"


# ===========================================================================
# Depth-aware nested same-name HTML container tests (review reproducer).
# ===========================================================================

def test_nested_same_name_html_one_block():
    """Quality reviewer reproducer: <div>\n<div>x</div>\ny</div> must yield
    exactly ONE html block — the outer </div> closes, not the inner."""
    source = "<div>\n<div>x</div>\ny</div>\n"
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1, (
        f"expected 1 html block for nested same-name, got {len(html_blocks)}: "
        f"{[(b.start, b.end, b.text[:30]) for b in html_blocks]}"
    )
    # Text after the inner close stays in the same block.
    assert "y" in html_blocks[0].text
    assert html_blocks[0].text.rstrip().endswith("</div>")


def test_nested_same_name_html_stable_hash_and_heading():
    source = "# Section\n\n<div>\n<div>x</div>\ny</div>\n"
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1
    assert html_blocks[0].heading_path == ("Section",)
    # Hash is deterministic.
    assert html_blocks[0].hash.startswith("sha256:")
    h2 = extract_blocks(source)[len(blocks) - 1].hash
    assert html_blocks[0].hash == h2


def test_nested_same_name_html_crlf_parity():
    """LF and CRLF produce the same block count, kinds, and hashes."""
    body = "<div>\n<div>x</div>\ny</div>\n"
    lf = extract_blocks(body)
    crlf = extract_blocks(body.replace("\n", "\r\n"))
    assert len(lf) == len(crlf)
    assert [b.kind for b in lf] == [b.kind for b in crlf]
    for a, b in zip(lf, crlf):
        assert a.hash == b.hash, "CRLF→LF hash normalisation must match"


def test_nested_same_name_html_multiple_tags_one_line():
    """<div><div>x</div></div> on one line → one block."""
    blocks = extract_blocks("<div><div>x</div></div>\n")
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1


def test_nested_same_name_html_attribute_fake_close():
    """Quoted attribute value containing > must not trigger early close."""
    source = '<div title="a>b">\ncontent\n</div>\n'
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1
    assert "content" in html_blocks[0].text


def test_nested_same_name_html_mixed_case():
    """Mixed-case tags <DIV>...</div> are depth-balanced."""
    source = "<DIV>\nstuff\n</div>\n"
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1


def test_nested_same_name_html_void_tag_no_depth_change():
    """Void tags (<br>) inside a paired container do not affect depth."""
    source = "<div>\n<br>\nstuff\n</div>\n"
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1


def test_nested_same_name_html_self_closing_no_depth_change():
    """Self-closing same-name <div/> does not increment depth."""
    source = "<div>\n<div/>\nstuff\n</div>\n"
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1


def test_nested_same_name_html_capture_resolve_projection(tmp_path):
    """Capture, resolve, and projection are complete for nested HTML blocks."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\n<div>\n<div>x</div>\ny\n</div>\n"
    (tmp_path / "n.md").write_text(raw)
    b = Bundle.load(tmp_path)
    c = b.concepts[("n",)]
    # Projection must be complete.
    html, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True, (
        f"projection incomplete for nested HTML: {[(r['block_id'], r['dom_key']) for r in proj['blocks']]}"
    )


# ===========================================================================
# Multi-line comment/CDATA/PI lexical state carries across lines (review
# reproducer: fake opens/closes inside multi-line constructs do not affect
# depth).
# ===========================================================================

def test_multiline_comment_fake_close_one_block():
    """Reproducer: <div>\\n<!--\\n</div>\\n-->\\ny</div> must be ONE html block."""
    source = "<div>\n<!--\n</div>\n-->\ny</div>\n"
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1, (
        f"expected 1 block, got {len(html_blocks)}: "
        f"{[(b.start, b.end, repr(b.text[:40])) for b in html_blocks]}"
    )
    assert "y" in html_blocks[0].text
    assert html_blocks[0].text.rstrip().endswith("</div>")


def test_multiline_comment_fake_open_close_one_block():
    """Fake <div></div> pair inside multi-line comment does not affect depth."""
    source = "<div>\n<!--\n<div></div>\n-->\nreal\n</div>\n"
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1
    assert "real" in html_blocks[0].text


def test_multiline_cdata_fake_tags_one_block():
    """Fake <div></div> inside multi-line CDATA does not affect depth."""
    source = "<div>\n<![CDATA[\n<div></div>\n]]>\nreal\n</div>\n"
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1
    assert "real" in html_blocks[0].text


def test_multiline_pi_fake_close_one_block():
    """Fake </div> inside multi-line PI does not affect depth."""
    source = "<div>\n<?\n</div>\n?>\nreal\n</div>\n"
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1
    assert "real" in html_blocks[0].text


def test_multiline_comment_crlf_parity():
    """LF and CRLF produce same block count, kinds, and hashes."""
    body = "<div>\n<!--\n</div>\n-->\ny</div>\n"
    lf = extract_blocks(body)
    crlf = extract_blocks(body.replace("\n", "\r\n"))
    assert len(lf) == len(crlf)
    assert [b.kind for b in lf] == [b.kind for b in crlf]
    for a, b in zip(lf, crlf):
        assert a.hash == b.hash


def test_multiline_comment_stable_span():
    """The block span covers the entire construct including the real close."""
    source = "<div>\n<!--\n</div>\n-->\ny</div>\n"
    blocks = extract_blocks(source)
    html_blocks = [b for b in blocks if b.kind == "html"]
    assert len(html_blocks) == 1
    blk = html_blocks[0]
    # Span must include the closing </div> at the end.
    assert source[blk.start:blk.end].rstrip().endswith("</div>")
    # Span must include 'y' after the comment terminator.
    assert "y" in source[blk.start:blk.end]


def test_multiline_comment_capture_resolve_projection(tmp_path):
    """Capture, resolve, and projection are complete for HTML blocks with
    fake tags inside multi-line comments."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    raw = "---\ntype: note\ntitle: T\n---\n<div>\n<!--\n</div>\n<div></div>\n-->\ny\n</div>\n"
    (tmp_path / "n.md").write_text(raw)
    b = Bundle.load(tmp_path)
    c = b.concepts[("n",)]
    html, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True, (
        f"projection incomplete: {[(r['block_id'], r['dom_key']) for r in proj['blocks']]}"
    )


# ===========================================================================
# R8: Blockquote IR recursively interprets inner block Markdown.
# _quote_content must mirror the renderer's recursive re-rendering so that
# expected_visible matches browser textContent for headings/lists/hr/nested
# blocks, not treat inner content as inline paragraph text.
# ===========================================================================

def test_r8_quote_heading_visible_is_not_inline_hash():
    """``> # Title`` must produce visible 'Title', not '# Title'."""
    blocks = extract_blocks("> # Title\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "Title", f"expected 'Title', got {vis!r}"


def test_r8_quote_thematic_break_visible_is_empty():
    """``> ---`` must produce empty visible text (renders as <hr />)."""
    blocks = extract_blocks("> ---\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "", f"expected '', got {vis!r}"


def test_r8_quote_list_visible_concatenates_items():
    """``> - a`` + ``> - b`` must concatenate without separator."""
    blocks = extract_blocks("> - item1\n> - item2\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "item1item2", f"got {vis!r}"


def test_r8_quote_ordered_list_visible_concatenates_items():
    """``> 1. first`` + ``> 2. second`` must concatenate without separator."""
    blocks = extract_blocks("> 1. first\n> 2. second\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "firstsecond", f"got {vis!r}"


def test_r8_quote_nested_blockquote_visible_strips_all_levels():
    """``> > nested`` must produce 'nested'."""
    blocks = extract_blocks("> > nested\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "nested", f"got {vis!r}"


def test_r8_quote_mixed_paragraph_heading_visible():
    """``> text`` + ``> # heading`` produces 'text\\nheading'."""
    blocks = extract_blocks("> text\n> # heading\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "text\nheading", f"got {vis!r}"


def test_r8_quote_mixed_ordered_unordered_has_newline():
    """Ordered→unordered switch produces \\n between sub-groups."""
    blocks = extract_blocks("> 1. first\n> - second\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "first\nsecond", f"got {vis!r}"


def test_r8_quote_simple_text_still_works():
    """Simple text blockquote must not regress."""
    blocks = extract_blocks("> quoted *value*\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "quoted value", f"got {vis!r}"


def test_r8_quote_crlf_heading_visible():
    """CRLF variant of heading in blockquote."""
    blocks = extract_blocks("> # Title\r\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "Title", f"got {vis!r}"


def test_r8_quote_crlf_list_visible():
    """CRLF variant of list in blockquote."""
    blocks = extract_blocks("> - a\r\n> - b\r\n")
    vis, _ = block_visible_segments(blocks[0])
    assert vis == "ab", f"got {vis!r}"


# ===========================================================================
# R9: cross-line inline projection parity.
#
# _paragraph_content must tokenize the renderer's JOINED paragraph payload so
# that inline delimiters spanning a soft line break (e.g. ``**patches\nin
# place**``) are recognised exactly as the renderer sees them, instead of being
# split into per-line literal text. The historical defect left stray ``**`` in
# expected_visible → owner text mismatch → eligible-unmapped → complete=False,
# which globally invalidated the projection (and thus the selection composer)
# even for unrelated, correctly-mapped blocks like the showcase blockquote.
# ===========================================================================

def _owner_textcontent(html_out: str) -> dict:
    """Map each data-okf-proj-owner nonce -> the DOM textContent under it."""
    from html.parser import HTMLParser

    class _Scan(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack: list = []
            self.text: dict = {}

        def handle_starttag(self, tag, attrs):
            nonce = dict(attrs).get("data-okf-proj-owner")
            if tag not in ("area", "base", "br", "col", "embed", "hr", "img",
                           "input", "link", "meta", "param", "source", "track",
                           "wbr"):
                self.stack.append(nonce)

        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()

        def handle_data(self, data):
            for n in {x for x in self.stack if x}:
                self.text[n] = self.text.get(n, "") + data

    s = _Scan()
    s.feed(html_out)
    return s.text


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("body,expected", [
    # Cross-line **strong** — the original defect (**patches\nin place**).
    ("xx **patches\nin place** yy", "xx patches in place yy"),
    # Cross-line __strong__ (underscore form).
    ("xx __patches\nin place__ yy", "xx patches in place yy"),
    # Cross-line ~~strikethrough~~.
    ("a ~~strike\nme~~ b", "a strike me b"),
    # Cross-line single-star emphasis.
    ("a *foo\nbar* b", "a foo bar b"),
    # Cross-line link label.
    ("see [foo\nbar](u.md) now", "see foo bar now"),
    # ** opening marker at the very end of the first line.
    ("**bold\nline**", "bold line"),
])
def test_r9_cross_line_inline_visible_matches_renderer(newline, body, expected):
    """IR expected_visible must equal the rendered DOM textContent for inline
    delimiters that span a soft line break (LF + CRLF)."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    src = body.replace("\n", newline) + newline
    blocks = extract_blocks(src)
    para = next(b for b in blocks if b.kind == "paragraph")
    vis, _ = block_visible_segments(para)
    assert vis == expected, f"IR visible mismatch ({newline!r}): {vis!r}"
    html_out, owners = render_blocks_with_owners(src)
    actual = _owner_textcontent(html_out)
    for o in owners:
        if o["block_id"] == para.id:
            assert actual.get(o["nonce"], "") == vis, (
                f"DOM textContent mismatch ({newline!r}): "
                f"expected {vis!r}, got {actual.get(o['nonce'], '')!r}"
            )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r9_cross_line_strong_segment_offsets_exact(newline):
    """The source-map segments for a cross-line strong span must (a) reconstruct
    the visible text, (b) map the soft-break space to the source newline span,
    and (c) exclude the ``**`` delimiter source offsets — proving exact
    source/code-point offsets are preserved across the join."""
    src = "xx **patches\nin place** yy".replace("\n", newline) + newline
    blocks = extract_blocks(src)
    para = next(b for b in blocks if b.kind == "paragraph")
    vis, segs = block_visible_segments(para)
    assert vis == "xx patches in place yy", f"got {vis!r}"
    # (a) segments reconstruct the visible text exactly.
    reconstructed = "".join(vis[s["dom_start"]:s["dom_end"]] for s in segs)
    assert reconstructed == vis, f"segments do not reconstruct vis: {reconstructed!r}"
    # (b) the source newline span is covered by a single-space DOM segment.
    nl = src.index(newline)
    nl_seg = next((s for s in segs if s["source_start"] <= nl < s["source_end"]), None)
    assert nl_seg is not None, f"no segment covers the newline at source {nl}"
    assert vis[nl_seg["dom_start"]:nl_seg["dom_end"]] == " ", (
        f"newline must map to a single rendered space, got "
        f"{vis[nl_seg['dom_start']:nl_seg['dom_end']]!r}"
    )
    assert nl_seg["source_end"] - nl_seg["source_start"] == len(newline), (
        f"newline segment must span exactly the newline bytes ({newline!r})"
    )
    # (c) the ** delimiter offsets are excluded from every segment's source range.
    star_open = src.index("**")
    star_close = src.index("**", star_open + 2)
    for s in segs:
        assert not (s["source_start"] <= star_open < s["source_end"])
        assert not (s["source_start"] <= star_close < s["source_end"])


def test_r9_entity_not_split_across_line_keeps_strong():
    """A character entity stays on its line (entities cannot span a soft break),
    and a cross-line strong that follows still resolves — the joined tokenizer
    must not regress entity or single-line handling."""
    src = "Cost is &amp;5 for **one\nthing** here.\n"
    blocks = extract_blocks(src)
    para = next(b for b in blocks if b.kind == "paragraph")
    vis, segs = block_visible_segments(para)
    assert vis == "Cost is &5 for one thing here.", f"got {vis!r}"
    # The entity decodes to a single '&' (non-identity segment boundary).
    entity_src = src.index("&amp;")
    ent_seg = next((s for s in segs if s["source_start"] <= entity_src < s["source_end"]))
    assert vis[ent_seg["dom_start"]:ent_seg["dom_end"]] == "&"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_r9_cross_line_strong_projection_complete(newline):
    """End-to-end: a paragraph with a cross-line strong span must project
    complete (the live-demo blocker)."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    src = "Edit any concept and the page **patches\nin place** now.".replace(
        "\n", newline) + newline
    html, owners = render_blocks_with_owners(src)
    _, proj = build_annotation_projection(src, src, html, owners=owners)
    assert proj["complete"] is True, (
        f"cross-line strong must project complete ({newline!r})"
    )


def test_r9_annotations_v2_showcase_projection_complete():
    """The canonical docs-bundle showcase (contains the cross-line
    ``**patches\nin place**`` Live-updates paragraph) must project complete
    after the parity fix — this is the exact live-demo blocker."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    b = Bundle.load(Path("docs-bundle"))
    c = b.concepts[("demo", "annotations_v2_showcase")]
    _, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True, "showcase must be complete for the live demo"


def test_r9_showcase_target_fixture_mapped():
    """The fixture sentence the showcase exercises target (the 'river path'
    sentence) must carry a projection dom_key and exact source/DOM parity once
    the showcase is complete — proving the selection composer can resolve it
    after the parity fix (previously the complete=false global invalidation
    blocked it). The fixture is deliberately a plain paragraph, not a
    blockquote, so the live Studio can capture it."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    b = Bundle.load(Path("docs-bundle"))
    c = b.concepts[("demo", "annotations_v2_showcase")]
    _, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True
    raw = c.raw_text
    fixture = "The river path stays open after sunset when the amber markers are visible."
    # Locate the target by its exact content (it appears exactly once on the
    # page), not by block kind — the fixture is a plain paragraph so the live
    # Studio can capture it.
    target = next(
        r for r in proj["blocks"]
        if fixture in raw[r["source_start"]:r["source_end"]]
    )
    assert target["block_id"].startswith("paragraph:"), (
        f"fixture must be a paragraph block, got {target['block_id']!r}"
    )
    assert target["dom_key"] is not None, "target paragraph must be mapped to a DOM node"
    segs = target["segments"]
    assert segs, "target paragraph must carry source-map segments"
    assert all(s["source_start"] < s["source_end"] for s in segs)
    assert all(s["dom_start"] < s["dom_end"] for s in segs)
    # Exact source/DOM parity: each segment's source and DOM extents match, and
    # the source span reproduces the fixture sentence verbatim.
    assert all(
        s["source_end"] - s["source_start"] == s["dom_end"] - s["dom_start"]
        for s in segs
    ), "each segment's source and DOM extents must be identical"
    assert raw[segs[0]["source_start"]:segs[-1]["source_end"]] == fixture, (
        "segment source span must equal the fixture sentence verbatim"
    )


# ===========================================================================
# F16: shared inline-code delimiter grammar.
#
# The HTML inline renderer and the projection text tokenizer must agree on
# where an inline code span starts and ends, so the rendered visible text
# equals the canonical projection. The saved showcase paragraphs
# (`` ` ```mermaid ` `` / `` ` ```math ` ``) mix backtick run lengths: a
# 1-backtick opener must close at the NEXT run of exactly 1 backtick — a
# longer ``` run stays inside the span content. The historical regex treated
# every backtick individually and disagreed with the tokenizer, which left
# both showcase paragraphs eligible-but-unmapped → complete=False → the
# client refused the whole page's projection metadata.
# ===========================================================================

_SHOWCASE = Path("docs-bundle") / "demo" / "showcase.md"


def _saved_showcase_lines() -> list[str]:
    """The exact saved source paragraphs carrying mixed backtick runs."""
    lines = _SHOWCASE.read_text(encoding="utf-8").splitlines()
    return [lines[89], lines[117]]


def _code_span_case(source: str):
    """Return (paragraph block, visible, segments) for a one-paragraph doc."""
    blocks = extract_blocks(source)
    para = next(b for b in blocks if b.kind == "paragraph")
    return para, block_visible_segments(para)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_f16_saved_showcase_inline_examples_render_equals_projection(newline):
    """For each saved showcase paragraph: the rendered DOM textContent under
    the owner marker equals the IR expected visible text, and the inline
    example keeps its CommonMark visible form `` ```mermaid ``/`` ```math ``
    (one code span, longer runs inside the content)."""
    from okf_loom.viewer.markdown import render_blocks_with_owners

    saved = _saved_showcase_lines()
    assert "` ```mermaid `" in saved[0] and "` ```math `" in saved[1]
    expected_span = (" ```mermaid ", " ```math ")
    for line, span in zip(saved, expected_span):
        src = line.replace("\n", newline) + newline
        para, result = _code_span_case(src)
        assert result is not None, f"IR must map the saved example: {line!r}"
        vis, _ = result
        assert span in vis, f"code span content must stay intact ({newline!r}): {vis!r}"
        html_out, owners = render_blocks_with_owners(src)
        actual = _owner_textcontent(html_out)
        owner = next(o for o in owners if o["block_id"] == para.id)
        assert actual.get(owner["nonce"], "") == vis, (
            f"rendered/projection mismatch ({newline!r}) for {line!r}: "
            f"{actual.get(owner['nonce'], '')!r} != {vis!r}"
        )


def test_f16_saved_math_example_visible_text_and_exact_source_span():
    """The math example renders ONE code span whose visible content is
    `` ```math `` (space-padded, longer run inside) and whose source map
    covers exactly the source characters `` ```math `` — 1:1 identity."""
    line = "A fenced ` ```math ` block renders display math:"
    src = line + "\n"
    para, result = _code_span_case(src)
    vis, segs = result
    assert vis == "A fenced  ```math  block renders display math:"
    # The rendered HTML: one <code> span, delimiter backticks consumed.
    from okf_loom.viewer.markdown import markdown_to_html
    html = markdown_to_html(src)
    assert "<code> ```math </code>" in html
    # The code content's source span is exact: the visible `` ```math ``
    # slice maps 1:1 onto the same characters in the source.
    content = "```math"
    at = vis.index(content)
    dom_a, dom_b = at, at + len(content)
    covering = [s for s in segs if s["dom_start"] <= dom_a and dom_b <= s["dom_end"]]
    assert covering, f"no segment covers the code content: {segs!r}"
    seg = covering[0]
    assert seg["source_end"] - seg["source_start"] == seg["dom_end"] - seg["dom_start"]
    src_a = seg["source_start"] + (dom_a - seg["dom_start"])
    assert src[src_a:src_a + len(content)] == content


def test_f16_saved_examples_leave_whole_showcase_concept_complete():
    """End-to-end acceptance: the canonical demo/showcase concept must
    project complete with both saved example paragraphs mapped."""
    from okf_loom import Bundle
    from okf_loom.render import render_concept_body_with_projection
    b = Bundle.load(Path("docs-bundle"))
    c = b.concepts[("demo", "showcase")]
    _, proj = render_concept_body_with_projection(c, b, mode="serve")
    assert proj["complete"] is True, "showcase must admit exact anchoring"
    raw = c.raw_text
    for needle in ("` ```mermaid `", "` ```math `"):
        rec = next(r for r in proj["blocks"] if needle in raw[r["source_start"]:r["source_end"]])
        assert rec["dom_key"] is not None, f"{needle!r} paragraph must be mapped"
        assert rec["segments"], f"{needle!r} paragraph must carry segments"


@pytest.mark.parametrize("source,html,visible", [
    # Ordinary inline code.
    ("plain `code` span", "<p>plain <code>code</code> span</p>", "plain code span"),
    # Saved showcase shape: 1-opener skips the longer ``` run, closes at the
    # next 1-run; the ``` stays inside the content.
    ("a ` ```m ` b", "<p>a <code> ```m </code> b</p>", "a  ```m  b"),
    # Equal 2-run span; a 1-run stays inside the content.
    ("d ``a`b`` e", "<p>d <code>a`b</code> e</p>", "d a`b e"),
    # Unmatched opener run: the WHOLE run is literal, scanning resumes after
    # it, so the later 1-run pair still forms a span.
    ("u ```x`y` v", "<p>u ```x<code>y</code> v</p>", "u ```xy v"),
    # No closer at all: everything literal.
    ("open ` never", "<p>open ` never</p>", "open ` never"),
    # Entities inside code stay literal text (CommonMark: no decode in code
    # spans), escaped once for HTML.
    ("e `a&amp;b` f", "<p>e <code>a&amp;amp;b</code> f</p>", "e a&amp;b f"),
    # Unicode inside code stays 1:1.
    ("g `héllo😀` h", "<p>g <code>héllo😀</code> h</p>", "g héllo😀 h"),
])
def test_f16_inline_code_delimiter_matrix(source, html, visible):
    """One grammar: rendered HTML, rendered visible text, and the IR visible
    text agree for ordinary, unequal-run, unmatched, entity and Unicode
    inline code."""
    from okf_loom.viewer.markdown import markdown_to_html, render_blocks_with_owners
    src = source + "\n"
    assert markdown_to_html(src) == html, source
    para, result = _code_span_case(src)
    vis, _ = result
    assert vis == visible, source
    html_out, owners = render_blocks_with_owners(src)
    owner = next(o for o in owners if o["block_id"] == para.id)
    assert _owner_textcontent(html_out).get(owner["nonce"], "") == visible


def test_f16_inline_code_span_source_positions():
    """find_inline_code_span returns exact offsets for the delimiter runs:
    content between them, span end after the closing run."""
    from okf_loom.markdown_blocks import find_inline_code_span
    text = "a ` ```m ` b"
    # opener at 2 (run 1), content 3..9 (" ```m "), closing run 9..10.
    assert find_inline_code_span(text, 2) == (2, 3, 9, 10)
    assert find_inline_code_span("``a`b``", 0) == (0, 2, 5, 7)
    # Not a backtick: no span.
    assert find_inline_code_span("abc", 1) is None
    # Unmatched run: None (opener is literal).
    assert find_inline_code_span("a ` b", 2) is None


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_f16_inline_example_projection_complete(newline):
    """A paragraph carrying the saved math example must project complete for
    LF and CRLF sources (the exact regression that blocked /demo/showcase)."""
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    body = "A fenced ` ```math ` block renders display math.\n".replace("\n", newline)
    html, owners = render_blocks_with_owners(body)
    _, proj = build_annotation_projection(body, body, html, owners=owners)
    assert proj["complete"] is True, f"inline example must project complete ({newline!r})"


def test_f16_footnote_backlink_visible_but_unmapped():
    """The footnote li's template space + back-arrow are visible (DOM parity)
    but carry no source bytes: they must NOT become zero-width segments — the
    client validator rejects source_start == source_end — and the trailing
    unmapped DOM region keeps selecting `` ↩`` an honest page fallback."""
    src = "Para one.\n\n[^why]: Footnote body `[^id]` here.\n"
    blocks = extract_blocks(src)
    fn = next(b for b in blocks if b.text.lstrip().startswith("[^why]:"))
    vis, segs = block_visible_segments(fn)
    assert vis.endswith(" ↩"), f"back-arrow must stay visible: {vis!r}"
    assert all(s["source_start"] < s["source_end"] for s in segs), segs
    covered = max(s["dom_end"] for s in segs)
    assert vis[covered:] == " ↩", f"only the back-link may be unmapped: {vis[covered:]!r}"
    # End-to-end: the page still projects complete.
    from okf_loom.render import build_annotation_projection
    from okf_loom.viewer.markdown import render_blocks_with_owners
    html, owners = render_blocks_with_owners("Para one.\n\n[^why]: Footnote body `[^id]` here.\n")
    _, proj = build_annotation_projection(
        "Para one.\n\n[^why]: Footnote body `[^id]` here.\n",
        "Para one.\n\n[^why]: Footnote body `[^id]` here.\n", html, owners=owners)
    assert proj["complete"] is True


# ===========================================================================
# List-item multiline inline tokenization parity.
#
# The renderer joins every non-blank continuation line into the CURRENT
# item's text (``stack[-1]["node"]["text"] += "\n" + line.strip()``) and then
# renders that whole logical item text through ONE inline parse.
# ``_list_item_content`` must tokenize the same logical item text — with an
# explicit position map back to canonical source coordinates — so delimiters
# spanning a continuation boundary (e.g. an inline code span opening on the
# marker line and closing on a continuation line) produce the same visible
# text as the rendered DOM with exact original source offsets. A
# fenced-looking ``​``​`​`` run inside an item keeps rendering as multiline
# INLINE code including any language text (established rendering behaviour).
# ===========================================================================

_LIST_MULTILINE_CASES = [
    # Cross-join inline code span: rendered <code>a\nb</code>, not literal
    # backticks.
    ("- `a\n  b`\n", "a\nb"),
    # Single-line control: unchanged behaviour.
    ("- `a b`\n", "a b"),
    # Fenced-looking run inside an item: multiline inline code INCLUDING the
    # language text and trailing newline (rendered behaviour preserved).
    ("- x\n  ```py\n  a * b\n  ```\n", "x\npy\na * b\n"),
    # Plain continuation control.
    ("- x\n  a * b\n", "x\na * b"),
    # Unclosed opener run stays literal on BOTH sides (shared grammar) — no
    # delimiter is guessed.
    ("- `a\n  b\n", "`a\nb"),
    # Unicode (emoji astral + accents) across the join stays 1:1.
    ("- `héllo😀\n  wörld`\n", "héllo😀\nwörld"),
]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("body,expected", _LIST_MULTILINE_CASES)
def test_list_multiline_visible_matches_renderer(newline, body, expected):
    """IR expected_visible must equal the rendered DOM textContent for list
    items whose inline delimiters span a continuation boundary (LF + CRLF)."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    src = body.replace("\n", newline)
    blocks = extract_blocks(src)
    item = next(b for b in blocks if b.kind == "list-item")
    result = block_visible_segments(item)
    assert result is not None, f"IR must map the item ({newline!r}): {src!r}"
    vis, _ = result
    assert vis == expected, f"IR visible mismatch ({newline!r}): {vis!r}"
    html_out, owners = render_blocks_with_owners(src)
    actual = _owner_textcontent(html_out)
    owner = next(o for o in owners if o["block_id"] == item.id)
    assert actual.get(owner["nonce"], "") == vis, (
        f"DOM textContent mismatch ({newline!r}): "
        f"{actual.get(owner['nonce'], '')!r} != {vis!r}"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_list_multiline_code_span_segments_exact(newline):
    """Exact source-segment semantics for a cross-join code span: segments
    reconstruct the visible text, exclude the delimiter backticks, map the
    joined newline to its exact source span (CRLF stays a 2-source-code-point
    → 1-DOM-newline segment, never mixed into an identity run), and leave the
    stripped continuation indentation an unmapped source gap."""
    src = "- `alpha\n  beta`\n".replace("\n", newline)
    blocks = extract_blocks(src)
    item = next(b for b in blocks if b.kind == "list-item")
    vis, segs = block_visible_segments(item)
    assert vis == "alpha\nbeta"
    # (a) Segments reconstruct the visible text exactly.
    assert "".join(vis[s["dom_start"]:s["dom_end"]] for s in segs) == vis
    # (b) The delimiter backticks stay outside every segment.
    b1 = src.index("`")
    b2 = src.index("`", b1 + 1)
    # (c) The stripped continuation indentation is covered by no segment.
    indent_at = src.index(newline) + len(newline)
    assert src[indent_at:indent_at + 2] == "  "
    for s in segs:
        assert not (s["source_start"] <= b1 < s["source_end"]), segs
        assert not (s["source_start"] <= b2 < s["source_end"]), segs
        assert not (s["source_start"] <= indent_at < s["source_end"]), segs
        assert not (s["source_start"] <= indent_at + 1 < s["source_end"]), segs
    # (d) Newline boundaries: the DOM newline maps to exactly len(newline)
    # source code points (2 for CRLF), never a mixed-width token.
    dom_cursor = 0
    hits = 0
    for s in segs:
        dom_text = vis[s["dom_start"]:s["dom_end"]]
        src_text = src[s["source_start"]:s["source_end"]]
        if "\n" not in dom_text:
            assert dom_text == src_text, (dom_text, src_text)
            continue
        hits += 1
        off = dom_text.index("\n")
        assert src_text[off:off + len(newline)] == newline, (dom_text, src_text)
        rest_dom = dom_text[off + 1:]
        assert src_text[off + len(newline):] == rest_dom
        if newline == "\r\n":
            assert dom_text == "\n" and src_text == "\r\n", (dom_text, src_text)
    assert hits == 1, segs


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_list_multiline_owner_segments_split_expanded_crlf(newline):
    """Through the production owner pipeline (render_blocks_with_owners), a
    proven-identity run crossing an expanded CRLF newline is split at it: the
    published owner segments contain a standalone two-source-code-point →
    one-DOM-newline segment, and no segment mixes widths."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    src = "- `alpha\n  beta`\n".replace("\n", newline)
    blocks = extract_blocks(src)
    item = next(b for b in blocks if b.kind == "list-item")
    vis, _ = block_visible_segments(item)
    html_out, owners = render_blocks_with_owners(src)
    owner = next(o for o in owners if o["block_id"] == item.id)
    segments = owner["segments"]
    assert "".join(vis[s["dom_start"]:s["dom_end"]] for s in segments) == vis
    for s in segments:
        dom_text = vis[s["dom_start"]:s["dom_end"]]
        src_text = src[s["source_start"]:s["source_end"]]
        if newline == "\r\n" and "\n" in dom_text:
            # The expanded newline owns its exact 2→1 boundaries.
            assert src_text == "\r\n" or ("\r\n" not in src_text and dom_text == src_text), (
                dom_text, src_text)
        else:
            assert dom_text == src_text, (dom_text, src_text)
    assert all(s["source_start"] < s["source_end"] for s in segments), segments


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_list_nested_and_task_multiline_parity(newline):
    """Nested-item boundaries and task markers survive logical-item
    tokenization: a cross-join code span inside a nested item maps exactly,
    and a task marker followed by a continuation consumes the join newline
    the same way the renderer's DOTALL task match does (checkbox + template
    space + continuation text)."""
    from okf_loom.viewer.markdown import render_blocks_with_owners
    body = (
        "- outer `x\n    y` tail\n"
        "  - inner `[a\n    b]`\n"
        "- [x]\n  follows checkbox\n"
    )
    src = body.replace("\n", newline)
    blocks = extract_blocks(src)
    items = [b for b in blocks if b.kind == "list-item"]
    assert len(items) == 2
    vis_outer, segs_outer = block_visible_segments(items[0])
    assert vis_outer == "outer x\ny tail\ninner [a\nb]", vis_outer
    # Delimiters excluded; the nested-item separator newline is present.
    assert "`" not in vis_outer
    vis_task, segs_task = block_visible_segments(items[1])
    assert vis_task == " follows checkbox", vis_task
    # The task template space maps to the whole "[x]<newline>" prefix span.
    prefix_seg = segs_task[0]
    assert vis_task[prefix_seg["dom_start"]:prefix_seg["dom_end"]] == " "
    assert src[prefix_seg["source_start"]:prefix_seg["source_end"]] == \
        f"[x]{newline}"
    # Rendered DOM parity for both owners. Local scanner: the shared
    # _owner_textcontent helper treats a self-closing <input ... /> as an
    # end tag (HTMLParser default), which would pop the task <li>'s nonce
    # before its checkbox-following text; the r4 projection-module scanner
    # semantics (startendtag = no stack change) are the correct ones here.
    from html.parser import HTMLParser

    class _TaskAwareScanner(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.text = {}

        def handle_starttag(self, tag, attrs):
            nonce = dict(attrs).get("data-okf-proj-owner")
            if tag not in ("area", "base", "br", "col", "embed", "hr", "img",
                           "input", "link", "meta", "param", "source", "track", "wbr"):
                self.stack.append(nonce)

        def handle_startendtag(self, tag, attrs):
            # Self-closing (the task checkbox): no textContent, no stack change.
            pass

        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()

        def handle_data(self, data):
            for n in {x for x in self.stack if x}:
                self.text[n] = self.text.get(n, "") + data

    html_out, owners = render_blocks_with_owners(src)
    scanner = _TaskAwareScanner()
    scanner.feed(html_out)
    nonce_by_id = {o["block_id"]: o["nonce"] for o in owners}
    for item, vis in ((items[0], vis_outer), (items[1], vis_task)):
        assert scanner.text.get(nonce_by_id[item.id], "") == vis, item.id
