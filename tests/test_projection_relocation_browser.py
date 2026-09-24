"""Chromium proof for HTML5 relocations that projection admission refuses.

A plugin can keep every genuine owner nonce and its exact lexical text while
HTML5 tree construction moves that text out of the owner: a block start tag
closes an open paragraph in button scope even behind inline elements;
non-cell content or text inside a table row (including resolved character
references and U+00A0) is foster-parented out of the table; a heading start
pops an open heading; ruby annotation and nested button starts generate
implied end tags through the owner; a line feed right after <pre> is dropped;
and a table part inside a cell closes the cell. A </noscript hidden in an
attribute value or comment ends noscript raw text early, so a textarea the
lexical view never sees swallows the owners; so does one behind "<!-->",
which HTML5 ends as a whole comment. Table parts without their table are
dropped. A caption, colgroup, col or tfoot start in a cell closes it too, so an
owner later in that cell lands before the table. HTML5 also tokenizes owner text differently from Python's parser:
a comment ended early leaks its rest as text, references Python drops (C0
controls, DEL, noncharacters) are code points, a literal NUL is dropped and a
literal CR becomes LF. The fast-lane refusals live in
tests/test_markdown_block_ir.py; this file executes the premise in a real
browser instead of assuming it. It also records that Chromium keeps an owner
inside a balanced select wrapper, which admission refuses anyway, and keeps
the references admission accepts.
"""
from __future__ import annotations

import re

import pytest

from browser_support import launch_chromium, sync_playwright
from okf_loom import Bundle
from okf_loom.render import _OwnerTextScanner, render_concept_body_with_projection

pytestmark = pytest.mark.browser

_PARAGRAPH = "# H\n\nText\n"
_TABLE = "# H\n\n| A | B |\n|---|---|\n| c1 | c2 |\n"
_NBSP_TABLE = "# H\n\n| A | B |\n|---|---|\n| &nbsp; | &nbsp; |\n"
_LEADING_LF_FENCE = "# H\n\n```\n\nfoo\n```\n"
_CR_REFERENCE = "# H\n\nA&#13;B\n"
_NUL = "# H\n\nA\u0000B\n"


def _in_owned_p(inner):
    return lambda html: re.sub(r"(<p [^>]*>)Text</p>", r"\g<1>" + inner + "</p>", html)


def _around_owned_p(before, inner, after):
    return lambda html: re.sub(r"(<p [^>]*>)Text</p>",
                               before + r"\g<1>" + inner + "</p>" + after, html)


def _nbsp_cells(replacement):
    return lambda html: html.replace("<td>&nbsp;</td><td>&nbsp;</td>", replacement)


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            context = browser.new_context()
            yield context.new_page()
            context.close()
        finally:
            browser.close()


def _refused_owner(tmp_path, page, body, plugin, text):
    """Render through the plugin seam, require admission to refuse, and load
    the returned HTML in Chromium; return the owner the lexical view credits
    with *text*."""
    (tmp_path / "x.md").write_text("---\ntype: note\ntitle: T\n---\n" + body, encoding="utf-8")
    bundle = Bundle.load(tmp_path)
    returned = {}

    def relocate(concept, html):
        returned["html"] = plugin(html)
        assert returned["html"] != html, "fixture transformation did not apply"
        return returned["html"]

    _, projection = render_concept_body_with_projection(
        bundle.concepts[("x",)], bundle, mode="serve", body_plugin=relocate)
    assert projection["complete"] is False

    # The lexical view still credits the owner with its exact text.
    scanner = _OwnerTextScanner()
    scanner.feed(returned["html"])
    scanner.close()
    [nonce] = [n for n, value in scanner.text.items() if value == text]

    page.set_content("<!DOCTYPE html><html><body><article>" + returned["html"]
                     + "</article></body></html>")
    return page.locator(f'[data-okf-proj-owner="{nonce}"]')


@pytest.mark.parametrize("body, plugin, text", [
    pytest.param(_PARAGRAPH, _in_owned_p("<span><div>Text</div></span>"), "Text", id="p-span-div"),
    pytest.param(_PARAGRAPH, _in_owned_p("<span><hr/>Text</span>"), "Text",
                 id="p-span-hr-self-closing"),
    pytest.param(_TABLE, lambda html: html.replace("<td>c1</td>", "<span>c1</span>"), "c1c2",
                 id="tr-td-and-span"),
    pytest.param(_TABLE, lambda html: html.replace(
        "<td>c1</td><td>c2</td>", "&#99;&#49;&#99;&#50;"), "c1c2", id="tr-charref-text"),
    pytest.param(_NBSP_TABLE, _nbsp_cells("\u00a0\u00a0"), "\u00a0\u00a0", id="tr-literal-nbsp"),
    pytest.param(_NBSP_TABLE, _nbsp_cells("&nbsp;&nbsp;"), "\u00a0\u00a0",
                 id="tr-nbsp-reference"),
    pytest.param(_PARAGRAPH, lambda html: re.sub(
        r"(<h2 [^>]*>)H</h2>", r"\g<1><h3>H</h3></h2>", html), "H", id="heading-in-heading"),
    pytest.param(_PARAGRAPH, _around_owned_p("<ruby>", "<rt>Text</rt>", "</ruby>"), "Text",
                 id="ruby-rt-in-p"),
    pytest.param(_PARAGRAPH, _around_owned_p("<ruby>", "<rp>Text</rp>", "</ruby>"), "Text",
                 id="ruby-rp-in-p"),
    pytest.param(_PARAGRAPH, _around_owned_p("<ruby>", "<rb>Text</rb>", "</ruby>"), "Text",
                 id="ruby-rb-in-p"),
    pytest.param(_PARAGRAPH, _around_owned_p("<ruby>", "<rtc>Text</rtc>", "</ruby>"), "Text",
                 id="ruby-rtc-in-p"),
    pytest.param(_PARAGRAPH, _around_owned_p("<button>", "<button>Text</button>", "</button>"),
                 "Text", id="button-in-p-in-button"),
    pytest.param(_LEADING_LF_FENCE, lambda html: re.sub(
        r"(<pre [^>]*>)<code>(.*?)</code></pre>", r"\g<1>\2</pre>", html, flags=re.S),
        "\nfoo", id="pre-leading-lf"),
    pytest.param(_LEADING_LF_FENCE, lambda html: re.sub(
        r"(<pre [^>]*>)<code>\n(.*?)</code></pre>", r"\g<1>&#10;\2</pre>", html, flags=re.S),
        "\nfoo", id="pre-leading-lf-reference"),
    pytest.param(_PARAGRAPH, _around_owned_p(
        "<table><tr><td>", "Te<tbody><tr><td>xt</td></tr></tbody>", "</td></tr></table>"),
        "Text", id="tbody-in-p-in-cell"),
])
def test_chromium_moves_owner_text_that_admission_refuses(tmp_path, page, body, plugin, text):
    owner = _refused_owner(tmp_path, page, body, plugin, text)
    assert owner.count() == 1
    # Chromium moved the text out of the owner but kept it in the document.
    assert owner.text_content() != text
    document_text = page.locator("article").text_content()
    parts = re.findall(r"[a-zA-Z]+\d*|\u00a0+", text)
    assert parts
    for part in parts:
        assert part in document_text


@pytest.mark.parametrize("plugin", [
    pytest.param(lambda html: '<noscript><i title="</noscript><textarea>"></i></noscript>' + html,
                 id="noscript-end-in-attribute"),
    pytest.param(lambda html: "<noscript><!-- </noscript><textarea> --></noscript>" + html,
                 id="noscript-end-in-comment"),
    # "<!-->" is a whole comment in HTML5, and the last comment ends the
    # textarea before the rest of the page.
    pytest.param(_around_owned_p("<!--><textarea>-->", "Text", "<!--</textarea>-->"),
                 id="comment-abrupt-close"),
])
def test_chromium_swallows_owners_in_textarea_the_lexical_view_hides(tmp_path, page, plugin):
    """noscript raw text ends at the first </noscript in a scripting browser,
    and "<!-->" ends a comment, so a textarea the lexical view reads as
    attribute or comment text is real and swallows the owner as its value."""
    owner = _refused_owner(tmp_path, page, _PARAGRAPH, plugin, "Text")
    assert owner.count() == 0
    assert "Text" in page.locator("textarea").input_value()


def test_chromium_drops_owner_rows_whose_table_is_missing(tmp_path, page):
    """In the article (not a template), the body parser ignores table parts
    outside a table: no keyed row remains, only the cell text."""
    owner = _refused_owner(tmp_path, page, _TABLE,
                           lambda html: re.sub(r"<table[^>]*>|</table>", "", html), "c1c2")
    assert owner.count() == 0
    assert page.locator("article tr, article td, article tbody").count() == 0
    document_text = page.locator("article").text_content()
    assert "c1" in document_text and "c2" in document_text


_TABLE_STARTS_IN_CELL = [
    pytest.param("<caption></caption>", id="caption"),
    pytest.param("<colgroup></colgroup>", id="colgroup"),
    pytest.param("<col>", id="col"),
    pytest.param("<tfoot></tfoot>", id="tfoot"),
]


@pytest.mark.parametrize("start", _TABLE_STARTS_IN_CELL)
def test_chromium_fosters_owner_after_table_start_in_cell_out_of_the_table(tmp_path, page, start):
    """A caption, colgroup, col or tfoot start closes the cell, so the owner
    the lexical view keeps in that cell lands before the table."""
    owner = _refused_owner(tmp_path, page, _PARAGRAPH, _around_owned_p(
        "<table><tr><td><div>" + start + "</div>", "Text", "</td></tr></table>"), "Text")
    assert owner.count() == 1
    assert owner.text_content() == "Text"
    assert owner.evaluate("e => e.closest('table') === null")
    assert owner.evaluate("e => !!(e.compareDocumentPosition(document.querySelector('article table'))"
                          " & Node.DOCUMENT_POSITION_FOLLOWING)")


@pytest.mark.parametrize("start", _TABLE_STARTS_IN_CELL[:3])
def test_chromium_keeps_owner_row_after_table_start_in_earlier_cell(tmp_path, page, start):
    """Recorded, not assumed: an owner row after the closed cell stays keyed
    with its text in the table. The refusal stands because the start is a
    table part outside its required parent."""
    owner = _refused_owner(tmp_path, page, _TABLE, lambda html: html.replace(
        "<tbody>", "<tbody><tr><td><div>" + start + "</div></td></tr>", 1), "c1c2")
    assert owner.count() == 1
    assert owner.text_content() == "c1c2"
    assert owner.evaluate("e => e.closest('table') !== null")


def test_chromium_keeps_owner_rows_of_admitted_caption_and_colgroup(tmp_path, page):
    """Recorded, not assumed: a caption and column group directly in the
    table leave every keyed row with the text admission credits."""
    (tmp_path / "x.md").write_text("---\ntype: note\ntitle: T\n---\n" + _TABLE, encoding="utf-8")
    bundle = Bundle.load(tmp_path)
    html, projection = render_concept_body_with_projection(
        bundle.concepts[("x",)], bundle, mode="serve", body_plugin=lambda concept, html: re.sub(
            r"(<table[^>]*>)", r"\1<caption>Cap</caption><colgroup><col><col/></colgroup><col>",
            html, count=1))
    assert projection["complete"] is True
    page.set_content("<!DOCTYPE html><html><body><article>" + html + "</article></body></html>")
    rows = page.locator("article table tr[data-okf-projection-key]")
    assert rows.evaluate_all("rows => rows.map(r => r.textContent)") == ["AB", "c1c2"]


def _code_points(locator):
    return locator.evaluate("e => [...e.textContent].map(c => c.codePointAt(0))")


@pytest.mark.parametrize("body, plugin, text, browser_text", [
    pytest.param(_PARAGRAPH, _in_owned_p("<!-->x-->Text"), "Text", "x-->Text",
                 id="comment-abrupt-close"),
    pytest.param(_PARAGRAPH, _in_owned_p("<!--->x-->Text"), "Text", "x-->Text",
                 id="comment-abrupt-dash-close"),
    pytest.param(_PARAGRAPH, _in_owned_p("Te&#1;xt"), "Text", "Te\u0001xt",
                 id="control-reference"),
    pytest.param(_PARAGRAPH, _in_owned_p("Te&#x7F;xt"), "Text", "Te\u007fxt",
                 id="delete-reference"),
    pytest.param(_PARAGRAPH, _in_owned_p("Te&#xFDD0;xt"), "Text", "Te\ufdd0xt",
                 id="noncharacter-reference"),
    pytest.param(_CR_REFERENCE, lambda html: html.replace("&#13;", "\r"), "A\rB", "A\nB",
                 id="literal-cr"),
])
def test_chromium_tokenizes_owner_text_that_admission_refuses(
        tmp_path, page, body, plugin, text, browser_text):
    owner = _refused_owner(tmp_path, page, body, plugin, text)
    assert owner.count() == 1
    assert _code_points(owner) == [ord(c) for c in browser_text]


def _served(tmp_path, page, body):
    """Render without a plugin, load the returned HTML in the article and
    return its keyed paragraph and the projection."""
    (tmp_path / "x.md").write_text("---\ntype: note\ntitle: T\n---\n" + body, encoding="utf-8")
    bundle = Bundle.load(tmp_path)
    html, projection = render_concept_body_with_projection(
        bundle.concepts[("x",)], bundle, mode="serve")
    page.set_content("<!DOCTYPE html><html><body><article>" + html + "</article></body></html>")
    return page.locator("article p[data-okf-projection-key]"), projection


def test_chromium_drops_nul_from_renderer_output_that_admission_refuses(tmp_path, page):
    """The renderer's own output keeps a source NUL that HTML5 drops."""
    owner, projection = _served(tmp_path, page, _NUL)
    assert projection["complete"] is False
    assert owner.count() == 1
    assert _code_points(owner) == [ord("A"), ord("B")]


@pytest.mark.parametrize("body, text", [
    pytest.param(_CR_REFERENCE, "A\rB", id="cr-reference"),
    pytest.param("# H\n\nA&#x81;B\n", "A\u0081B", id="unmapped-c1-reference"),
    pytest.param("# H\n\nA&#x80;B\n", "A\u20acB", id="mapped-c1-reference"),
])
def test_chromium_keeps_reference_text_that_admission_accepts(tmp_path, page, body, text):
    """Recorded, not assumed: references Python decodes as HTML5 does keep
    the admitted text in Chromium."""
    owner, projection = _served(tmp_path, page, body)
    assert projection["complete"] is True
    assert _code_points(owner) == [ord(c) for c in text]


def test_chromium_keeps_select_wrapped_owner_that_admission_refuses(tmp_path, page):
    """Recorded, not assumed: this Chromium's select parser keeps a balanced
    select wrapper's owner and its text (older parsers dropped the p). The
    refusal stands because in-select tree construction is not modelled."""
    owner = _refused_owner(tmp_path, page, _PARAGRAPH,
                           _around_owned_p("<select>", "Text", "</select>"), "Text")
    assert owner.count() == 1
    assert owner.text_content() == "Text"
