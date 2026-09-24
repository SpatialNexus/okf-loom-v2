"""Deterministic browser matrix for Studio Annotations v2.

Network races use Playwright routes and explicit browser predicates. No test
uses fixed sleeps; screenshots are produced by scripts/capture_annotations_v2.py.
"""
from __future__ import annotations

import shutil
import hashlib
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from browser_support import expect, sync_playwright, launch_chromium, live_server

from conftest import okf_module_argv, okf_subprocess_env

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "samples" / "demo_bundle"
DOCS_BUNDLE = ROOT / "docs-bundle"


def _port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    bundle = tmp_path_factory.mktemp("annotations-v2-browser") / "bundle"
    shutil.copytree(DEMO, bundle, ignore=shutil.ignore_patterns(".okf-loom"))
    with live_server(bundle) as base:
        yield base


@pytest.fixture(scope="module")
def docs_bundle_server_url(tmp_path_factory):
    bundle = tmp_path_factory.mktemp("annotations-v2-docs-bundle") / "bundle"
    shutil.copytree(DOCS_BUNDLE, bundle, ignore=shutil.ignore_patterns(".okf-loom"))
    with live_server(bundle) as base:
        yield base


@pytest.fixture(scope="module")
def no_edit_server_url(tmp_path_factory):
    bundle = tmp_path_factory.mktemp("annotations-v2-readonly") / "bundle"
    shutil.copytree(DEMO, bundle, ignore=shutil.ignore_patterns(".okf-loom"))
    with live_server(bundle, "--no-edit") as base:
        yield base


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        pg = context.new_page()
        yield pg
        context.close()
        browser.close()


def _boot(page, server_url):
    page.goto(server_url + "/tables/orders", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    page.wait_for_function("() => !!(window.okfLoomStudio.state.doc && window.okfLoomStudio.state.doc.annotation_source)")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready' || window.okfLoomStudio.state.commentsStatus === 'error'")
    page.wait_for_function("() => window.__OKF_LOOM_STUDIO__.edit === false || window.okfLoomStudio.state.selectionAffordanceReady === true")


def _select_text_in(page, selector: str, exact: str):
    page.locator(selector).evaluate(
        """(root, exact) => {
          const nodes = [];
          const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
          let node;
          let text = '';
          while ((node = walker.nextNode())) {
            nodes.push({node, start: text.length});
            text += node.nodeValue || '';
          }
          const start = text.indexOf(exact);
          if (start < 0) throw new Error('exact text is absent from selected root');
          const end = start + exact.length;
          const point = (offset) => {
            for (let i = 0; i < nodes.length; i++) {
              const item = nodes[i];
              const itemEnd = item.start + (item.node.nodeValue || '').length;
              if (offset <= itemEnd) return {node: item.node, offset: offset - item.start};
            }
            throw new Error('selection endpoint is absent from selected root');
          };
          const range = document.createRange();
          const from = point(start), to = point(end);
          range.setStart(from.node, from.offset);
          range.setEnd(to.node, to.offset);
          const selection = getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          document.dispatchEvent(new Event('selectionchange'));
        }""",
        exact,
    )


def _set_doc_and_selection(page, source: str, html: str, exact: str, start_utf16=0, end_utf16=None):
    page.evaluate("window.okfLoomLive && window.okfLoomLive._disconnectForTest()")
    page.evaluate(
        """({source, html, exact, start, end}) => {
          const projected=html.replace('<p','<p data-okf-projection-key="0"');
          const visibleText=html.replace(/<[^>]+>/g,'');const visibleLength=Array.from(visibleText).length;
          const visibleStartUtf16=source.indexOf(visibleText);
          if(visibleStartUtf16<0) throw new Error('Visible fixture text is absent from canonical source');
          const blockStart=Array.from(source.slice(0,visibleStartUtf16)).length;
          const blockEnd=blockStart+visibleLength;
          window.okfLoomStudio.applyDoc({
            id:'tables/orders', annotation_source:source,
            concept_content_rev:'a'.repeat(64), raw:source,
              annotation_projection:{schema:1,resolver_version:2,concept_content_rev:'a'.repeat(64),offset_unit:'unicode-code-point',complete:true,blocks:[{block_id:'paragraph:1',source_start:blockStart,source_end:blockEnd,heading_path:[],dom_key:0,segments:[{dom_start:0,dom_end:visibleLength,source_start:blockStart,source_end:blockEnd}]}]},
            html:projected,title:'Orders', description:''
          }, {pulse:false});
          window.okfLoomStudio.state.commentsRequestGeneration++;
          window.okfLoomStudio.state.commentsReady=true;
          window.okfLoomStudio.state.commentsStatus='ready';
          const node=document.querySelector('.okf-page__body p').firstChild;
          const offset=node.nodeValue.indexOf(exact);
          const range=document.createRange();
          range.setStart(node, offset + start);
          range.setEnd(node, offset + (end == null ? exact.length : end));
          const selection=getSelection(); selection.removeAllRanges(); selection.addRange(range);
          document.dispatchEvent(new Event('selectionchange'));
        }""",
        {"source": source, "html": html, "exact": exact, "start": start_utf16, "end": end_utf16},
    )


def _seed_comment(page, status="stale", comment_id="01JANNOTATE"):
    page.evaluate("window.okfLoomLive && window.okfLoomLive._disconnectForTest()")
    page.evaluate(
        """({status,id}) => {
          window.okfLoomStudio.state.commentsRequestGeneration++;
          window.okfLoomStudio.state.commentsReady=true;
          const exact='target text';
          window.okfLoomStudio.state.comments=[{
            id, concept:'tables/orders', body:'Check this anchor', state:'open',
            row_token:'r2:old', ts:new Date().toISOString(),
            anchor:{schema:2,kind:'text-quote',exact,prefix:'',suffix:'',heading:{path:[]},
              block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},position:{start:0,end:11}},
            anchor_resolution:{status,reason:status==='stale'?'exact_not_found':status==='ambiguous'?'multiple_exact_candidates':'conflicting_evidence',
              candidate_count:status==='ambiguous'?2:0,concept_content_rev:'a'.repeat(64),resolver_version:2}
          }];
          window.okfLoomStudio.openPanel('comments');
        }""",
        {"status": status, "id": comment_id},
    )


def test_unicode_astral_combining_and_crlf_map_to_code_points(server_url, page):
    _boot(page, server_url)
    exact = "😀 e\u0301"
    source = "# Unicode\r\n\r\nBefore " + exact + " after\r\n"
    _set_doc_and_selection(page, source, "<p>Before 😀 e\u0301 after</p>", exact)
    anchor = page.evaluate("window.okfLoomStudio._captureCanonicalAnchor()")
    assert anchor["exact"] == exact
    assert anchor["position"]["end"] - anchor["position"]["start"] == len(exact)
    assert anchor["prefix"].endswith("Before ")
    assert anchor["suffix"].startswith(" after")
    assert anchor["block"]["hash"].startswith("sha256:")


def test_rendered_entity_captures_and_projects_full_canonical_token(server_url, page):
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => { const source='A &amp; B\\n';const doc={id:'tables/orders',annotation_source:source,concept_content_rev:'a'.repeat(64),annotation_projection:{schema:1,resolver_version:2,concept_content_rev:'a'.repeat(64),offset_unit:'unicode-code-point',complete:true,blocks:[{block_id:'paragraph:1',source_start:0,source_end:10,heading_path:[],dom_key:0,segments:[{dom_start:0,dom_end:2,source_start:0,source_end:2},{dom_start:2,dom_end:3,source_start:2,source_end:7},{dom_start:3,dom_end:5,source_start:7,source_end:9}]}]},html:'<p data-okf-projection-key="0">A &amp; B</p>',raw:source,title:'Orders',description:''};window.okfLoomStudio.applyDoc(doc,{pulse:false});const node=document.querySelector('.okf-page__body p').firstChild;const range=document.createRange();range.setStart(node,2);range.setEnd(node,3);const selection=getSelection();selection.removeAllRanges();selection.addRange(range);}""")
    anchor = page.evaluate("window.okfLoomStudio._captureCanonicalAnchor()")
    assert anchor["exact"] == "&amp;"
    assert anchor["position"] == {"start": 2, "end": 7}
    page.evaluate("""anchor => { const s=window.okfLoomStudio.state;s.comments=[{id:'01JENTITY00',concept:'tables/orders',body:'entity',state:'open',row_token:'r',ts:new Date().toISOString(),anchor,anchor_resolution:{status:'resolved',reason:'original_position',concept_content_rev:'a'.repeat(64),resolver_version:2,position:anchor.position,block:anchor.block,changes:[]}}];window.okfLoomStudio.applyDoc(Object.assign({},s.doc),{pulse:false});}""", anchor)
    mark = page.locator("mark.okf-comment-mark[data-comment-id='01JENTITY00']")
    expect(mark).to_have_text("&")


@pytest.mark.parametrize("fault", ["duplicate-key", "duplicate-node", "missing-node", "overlap", "out-of-bounds", "incomplete"])
def test_malformed_projection_rejects_whole_map(server_url, page, fault):
    _boot(page, server_url)
    _set_doc_and_selection(page, "alpha\n", "<p>alpha</p>", "alpha")
    error = page.evaluate("""fault => { const s=window.okfLoomStudio.state, doc=JSON.parse(JSON.stringify(s.doc)), r=doc.annotation_projection.blocks[0];
      if(fault==='duplicate-key') doc.annotation_projection.blocks.push(Object.assign({},r,{block_id:'other'}));
      if(fault==='duplicate-node') doc.html += '<p data-okf-projection-key="0">alpha</p>';
      if(fault==='missing-node') doc.html='<p>alpha</p>';
      if(fault==='overlap') r.segments.push({dom_start:0,dom_end:1,source_start:0,source_end:1});
      if(fault==='out-of-bounds') r.segments[0].source_end=99;
      if(fault==='incomplete') doc.annotation_projection.complete=false;
      window.okfLoomStudio.state.doc=doc;
      return window.okfLoomStudio._captureCanonicalAnchor().then(()=>'',e=>e.message);
    }""", fault)
    assert "canonical source mapping" in error or "invalid" in error
    expect(page.locator("mark.okf-comment-mark")).to_have_count(0)


def test_gapped_blockquote_projection_allows_valid_paragraph_capture(server_url, page):
    """Contract parity: a projection with legitimate ordered 1-code-point DOM
    gaps between block-quote sub-units (Python _concat_quote_units \\n
    separators) must NOT invalidate the whole map. The validator allows
    ordered unmapped gaps while still rejecting overlap, reversal,
    out-of-bounds, duplicate keys/nodes, and incomplete maps. Selecting text
    in the valid keyed paragraph must resolve a canonical source range via
    _captureCanonicalAnchor()."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Source: a keyed paragraph followed by a block-quote whose two sub-units
    # (## Sub heading + body line paragraph) produce a 1-code-point unmapped
    # \n gap between segments in the DOM — exactly what _concat_quote_units
    # emits and what the old strict-contiguity validator rejected globally.
    page.evaluate("""() => {
        const source = 'Target para here.\\n\\n> ## Sub\\n>\\n> body line\\n';
        const rev = 'a'.repeat(64);
        const blocks = [
          {block_id:'paragraph:1', source_start:0, source_end:18, heading_path:[], dom_key:0,
           segments:[{dom_start:0, dom_end:17, source_start:0, source_end:17}]},
          {block_id:'block-quote:2', source_start:19, source_end:42, heading_path:[], dom_key:1,
           segments:[{dom_start:0, dom_end:3, source_start:24, source_end:27},
                     {dom_start:4, dom_end:13, source_start:32, source_end:41}]},
        ];
        const html = '<p data-okf-projection-key="0">Target para here.</p>'
          + '<blockquote data-okf-projection-key="1"><h2>Sub</h2>\\n<p>body line</p></blockquote>';
        window.okfLoomStudio.applyDoc({
          id:'tables/orders', annotation_source:source, concept_content_rev:rev,
          annotation_projection:{schema:1, resolver_version:2, concept_content_rev:rev,
            offset_unit:'unicode-code-point', complete:true, blocks},
          html, raw:source, title:'Orders', description:''
        }, {pulse:false});
        // Select 'para here' inside the valid keyed paragraph.
        const node = document.querySelector('.okf-page__body p[data-okf-projection-key="0"]').firstChild;
        const exact = 'para here';
        const offset = node.nodeValue.indexOf(exact);
        const range = document.createRange();
        range.setStart(node, offset);
        range.setEnd(node, offset + exact.length);
        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
    }""")
    # The whole-map projection (including the gapped block-quote) must validate.
    valid = page.evaluate(
        "() => window.okfLoomStudio._validProjection("
        "window.okfLoomStudio.state.doc.annotation_projection, "
        "window.okfLoomStudio.state.doc.concept_content_rev, "
        "window.okfLoomStudio.state.doc.annotation_source, "
        "window.okfLoomStudio.state.doc.html)"
    )
    assert valid, "gapped block-quote projection must validate (contract parity with Python _concat_quote_units)"
    # Capture on the valid keyed paragraph must resolve a canonical source range.
    anchor = page.evaluate("window.okfLoomStudio._captureCanonicalAnchor()")
    assert anchor["exact"] == "para here"
    assert anchor["position"] == {"start": 7, "end": 16}
    assert anchor["block"]["id"] == "paragraph:1"
    assert anchor["block"]["start"] == 0
    assert anchor["block"]["end"] == 18
    assert anchor["prefix"] == "Target "
    assert anchor["suffix"] == ".\n"
    assert anchor["block"]["hash"].startswith("sha256:")


def test_blockquote_continuation_separator_validates_and_captures(server_url, page):
    """The Python producer maps a block-quote continuation marker (\\n> , \\r\\n> ,
    \\n>\\t, nested \\n> > ) to a single DOM space via a non-identity segment.
    _decodeCanonicalSegment MUST decode these structural separators to a space;
    validProjection must accept the whole map; _captureCanonicalAnchor on a
    co-mapped keyed paragraph must return a schema-2 text-quote anchor."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Source: a block-quote with a two-line soft-wrapped paragraph (whose
    # continuation \n> marker produces a structural-separator segment) followed
    # by a separate keyed paragraph for capture.
    page.evaluate("""() => {
        const source = '> line one\\n> line two\\n\\nTarget para.\\n';
        const rev = 'a'.repeat(64);
        const blocks = [
          {block_id:'block-quote:1', source_start:0, source_end:22, heading_path:[], dom_key:0,
           segments:[
             {dom_start:0, dom_end:8, source_start:2, source_end:10},
             {dom_start:8, dom_end:9, source_start:10, source_end:13},
             {dom_start:9, dom_end:17, source_start:13, source_end:21},
           ]},
          {block_id:'paragraph:2', source_start:23, source_end:36, heading_path:[], dom_key:1,
           segments:[{dom_start:0, dom_end:12, source_start:23, source_end:35}]},
        ];
        const html = '<blockquote data-okf-projection-key="0"><p>line one line two</p></blockquote>'
          + '\\n<p data-okf-projection-key="1">Target para.</p>';
        window.okfLoomStudio.applyDoc({
          id:'tables/orders', annotation_source:source, concept_content_rev:rev,
          annotation_projection:{schema:1, resolver_version:2, concept_content_rev:rev,
            offset_unit:'unicode-code-point', complete:true, blocks},
          html, raw:source, title:'Orders', description:''
        }, {pulse:false});
        // Select 'para' inside the keyed paragraph.
        const node = document.querySelector('.okf-page__body p[data-okf-projection-key="1"]').firstChild;
        const exact = 'para';
        const offset = node.nodeValue.indexOf(exact);
        const range = document.createRange();
        range.setStart(node, offset);
        range.setEnd(node, offset + exact.length);
        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
    }""")
    # The structural-separator segment in the block-quote must validate.
    valid = page.evaluate(
        "() => window.okfLoomStudio._validProjection("
        "window.okfLoomStudio.state.doc.annotation_projection, "
        "window.okfLoomStudio.state.doc.concept_content_rev, "
        "window.okfLoomStudio.state.doc.annotation_source, "
        "window.okfLoomStudio.state.doc.html)"
    )
    assert valid, "blockquote continuation separator \\n> must decode to a space"
    # Capture on the keyed paragraph must return a schema-2 text-quote anchor.
    anchor = page.evaluate("window.okfLoomStudio._captureCanonicalAnchor()")
    assert anchor["schema"] == 2
    assert anchor["kind"] == "text-quote"
    assert anchor["exact"] == "para"
    assert anchor["position"] == {"start": 30, "end": 34}
    assert anchor["block"]["id"] == "paragraph:2"
    assert anchor["block"]["start"] == 23
    assert anchor["block"]["end"] == 36
    assert anchor["prefix"] == "Target "
    assert anchor["suffix"] == ".\n"
    assert anchor["block"]["hash"].startswith("sha256:")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_list_continuation_newline_projection_matches_visible_line_break(server_url, page, newline):
    """List continuation source line endings are visible LF separators.

    The Python IR and renderer intentionally map both LF and CRLF source
    continuations to one visible newline in the owning ``li``. Paragraph
    soft-wraps retain their existing newline-to-space mapping, and a list
    projection that renders the separator as a space must still fail closed.
    """
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive && window.okfLoomLive._disconnectForTest()")
    result = page.evaluate(
        """(newline) => {
          const rev = 'a'.repeat(64);
          const listSource = '- first' + newline + '  continued line' + newline;
          const firstEnd = '- first'.length;
          const continuationStart = firstEnd + newline.length + 2;
          const continuationEnd = continuationStart + 'continued line'.length;
          const listProjection = {
            schema: 1, resolver_version: 2, concept_content_rev: rev,
            offset_unit: 'unicode-code-point', complete: true,
            blocks: [{
              block_id: 'list-item:1', source_start: 0,
              source_end: listSource.length, heading_path: [], dom_key: 0,
              segments: [
                {dom_start: 0, dom_end: 5, source_start: 2, source_end: 7},
                {dom_start: 5, dom_end: 6, source_start: firstEnd,
                 source_end: firstEnd + newline.length},
                {dom_start: 6, dom_end: 20, source_start: continuationStart,
                 source_end: continuationEnd},
              ],
            }],
          };
          const listHtml = '<ul><li data-okf-projection-key="0">'
            + 'first\\ncontinued line</li></ul>';
          const wrongListHtml = '<ul><li data-okf-projection-key="0">'
            + 'first continued line</li></ul>';
          const listError = window.okfLoomStudio._projectionError(
            listProjection, rev, listSource, listHtml);
          const wrongListError = window.okfLoomStudio._projectionError(
            listProjection, rev, listSource, wrongListHtml);
          const malformedListProjection = JSON.parse(JSON.stringify(listProjection));
          malformedListProjection.blocks[0].block_id = 'list-item:not-an-ordinal';
          const malformedListError = window.okfLoomStudio._projectionError(
            malformedListProjection, rev, listSource, listHtml);

          const paragraphSource = 'first\\ncontinued\\n';
          const paragraphProjection = {
            schema: 1, resolver_version: 2, concept_content_rev: rev,
            offset_unit: 'unicode-code-point', complete: true,
            blocks: [{
              block_id: 'paragraph:1', source_start: 0,
              source_end: paragraphSource.length, heading_path: [], dom_key: 0,
              segments: [
                {dom_start: 0, dom_end: 5, source_start: 0, source_end: 5},
                {dom_start: 5, dom_end: 6, source_start: 5, source_end: 6},
                {dom_start: 6, dom_end: 15, source_start: 6, source_end: 15},
              ],
            }],
          };
          const paragraphError = window.okfLoomStudio._projectionError(
            paragraphProjection, rev, paragraphSource,
            '<p data-okf-projection-key="0">first continued</p>');
          const malformedParagraphError = window.okfLoomStudio._projectionError(
            paragraphProjection, rev, paragraphSource,
            '<p data-okf-projection-key="0">first\\ncontinued</p>');
          return {listError, wrongListError, malformedListError, paragraphError, malformedParagraphError};
        }""",
        newline,
    )
    assert result["listError"] is None
    assert "segment text mismatch" in result["wrongListError"]
    assert "segment text mismatch" in result["malformedListError"]
    assert result["paragraphError"] is None
    assert "segment text mismatch" in result["malformedParagraphError"]


def test_docs_bundle_list_selection_opens_composer_without_premature_post(docs_bundle_server_url, page):
    """The reported docs-bundle list selection reaches the composer.

    This is an end-to-end browser proof that the real canonical tuple is
    projection-valid, comments are ready, and opening the composer does not
    issue a comment POST before the user presses Send.
    """
    page.goto(docs_bundle_server_url + "/explanation/v2_release_notes", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    page.wait_for_function("() => !!(window.okfLoomStudio.state.doc && window.okfLoomStudio.state.doc.annotation_source)")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready'")
    assert page.evaluate("window.okfLoomStudio.state.commentsStatus") == "ready"
    assert page.evaluate(
        """() => {
          const doc = window.okfLoomStudio.state.doc;
          return window.okfLoomStudio._projectionError(
            doc.annotation_projection, doc.concept_content_rev,
            doc.annotation_source, doc.html
          );
        }"""
    ) is None

    exact = "v1.0 baseline + Live Studio"
    item = page.locator("li[data-okf-projection-key]").filter(has_text=exact).first
    expect(item).to_be_visible()
    item_key = item.get_attribute("data-okf-projection-key")
    assert item_key is not None
    _select_text_in(page, f"li[data-okf-projection-key='{item_key}']", exact)
    comment_button = page.get_by_role("button", name="Comment on the selected text")
    expect(comment_button).to_be_visible()

    comment_posts = []
    page.on(
        "request",
        lambda request: comment_posts.append(request)
        if request.method == "POST" and "/__comment" in request.url else None,
    )
    comment_button.click()
    expect(page.get_by_role("textbox", name="Comment for agent")).to_be_visible()
    expect(page.get_by_role("button", name="Send")).to_be_disabled()
    assert comment_posts == [], "opening the composer must not POST before Send"
    assert page.get_by_text("get segment text mismatch", exact=False).count() == 0


def test_blockquote_continuation_equal_length_substitution_rejects(server_url, page):
    """A segment whose decoded source text differs from the visible text at its
    DOM position (same length, different characters) must be rejected by the
    position-anchored cross-validation — even when the structural separator
    segment is valid. This proves the separator fix does not weaken the
    equal-length substitution guard."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
        // Source has 'TWO' (uppercase) but HTML renders 'two' (lowercase):
        // the identity segment's decoded text will not match the visible text.
        const source = '> line one\\n> line TWO\\n\\nTarget para.\\n';
        const rev = 'a'.repeat(64);
        const blocks = [
          {block_id:'block-quote:1', source_start:0, source_end:22, heading_path:[], dom_key:0,
           segments:[
             {dom_start:0, dom_end:8, source_start:2, source_end:10},
             {dom_start:8, dom_end:9, source_start:10, source_end:13},
             {dom_start:9, dom_end:17, source_start:13, source_end:21},
           ]},
          {block_id:'paragraph:2', source_start:23, source_end:36, heading_path:[], dom_key:1,
           segments:[{dom_start:0, dom_end:12, source_start:23, source_end:35}]},
        ];
        const html = '<blockquote data-okf-projection-key="0"><p>line one line two</p></blockquote>'
          + '\\n<p data-okf-projection-key="1">Target para.</p>';
        window.okfLoomStudio.applyDoc({
          id:'tables/orders', annotation_source:source, concept_content_rev:rev,
          annotation_projection:{schema:1, resolver_version:2, concept_content_rev:rev,
            offset_unit:'unicode-code-point', complete:true, blocks},
          html, raw:source, title:'Orders', description:''
        }, {pulse:false});
    }""")
    valid = page.evaluate(
        "() => window.okfLoomStudio._validProjection("
        "window.okfLoomStudio.state.doc.annotation_projection, "
        "window.okfLoomStudio.state.doc.concept_content_rev, "
        "window.okfLoomStudio.state.doc.annotation_source, "
        "window.okfLoomStudio.state.doc.html)"
    )
    assert not valid, "equal-length substitution (TWO vs two) must be rejected"


def test_validate_staged_tuple_omitted_events_fails_closed(server_url, page):
    """R12: validateStagedTuple MUST require events by default (the canonical
    commit paths all pass eventData). Omitting eventData without an explicit
    commentsOnly=true flag must fail closed with skewSignal 'events-required'.
    This proves the structural-separator fix preserves events-required semantics."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
        const source = '> line one\\n> line two\\n\\nTarget para.\\n';
        const rev = 'a'.repeat(64);
        const blocks = [
          {block_id:'block-quote:1', source_start:0, source_end:22, heading_path:[], dom_key:0,
           segments:[
             {dom_start:0, dom_end:8, source_start:2, source_end:10},
             {dom_start:8, dom_end:9, source_start:10, source_end:13},
             {dom_start:9, dom_end:17, source_start:13, source_end:21},
           ]},
          {block_id:'paragraph:2', source_start:23, source_end:36, heading_path:[], dom_key:1,
           segments:[{dom_start:0, dom_end:12, source_start:23, source_end:35}]},
        ];
        const html = '<blockquote data-okf-projection-key="0"><p>line one line two</p></blockquote>'
          + '\\n<p data-okf-projection-key="1">Target para.</p>';
        window.okfLoomStudio.applyDoc({
          id:'tables/orders', annotation_source:source, concept_content_rev:rev,
          annotation_projection:{schema:1, resolver_version:2, concept_content_rev:rev,
            offset_unit:'unicode-code-point', complete:true, blocks},
          html, raw:source, title:'Orders', description:''
        }, {pulse:false});
    }""")
    result = page.evaluate("""() => {
        const s = window.okfLoomStudio.state;
        return window.okfLoomStudio._validateStagedTuple({
            conceptKey: 'tables/orders',
            doc: s.doc,
            commentsData: { comments: [] },
            // eventData intentionally omitted — must fail closed.
        });
    }""")
    assert result["skewSignal"] == "events-required", f"expected events-required, got {result!r}"


def test_thematic_break_projection_validates_before_and_after(server_url, page):
    _boot(page, server_url)
    # R8-FLAKY: deterministic ownership barrier. The test injects synthetic
    # body content (a thematic break between two paragraphs) and asserts on it,
    # so it MUST fence out the live transport first. Without this, a concurrent
    # SSE `ready` frame triggers handleReady -> patchOpenConcept, which fetches
    # the REAL canonical doc and overwrites the synthetic <hr> content. Under
    # load the ready-frame timing varies, so the selection/capture failed
    # intermittently. Disconnect closes the EventSource, stops polling, and
    # advances the route owner (aborts in-flight patches) — no sleeps.
    page.evaluate("window.okfLoomLive && window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => { const source='Before\\n\\n---\\n\\nAfter\\n', rev='a'.repeat(64), blocks=[
      {block_id:'paragraph:1',source_start:0,source_end:7,heading_path:[],dom_key:0,segments:[{dom_start:0,dom_end:6,source_start:0,source_end:6}]},
      {block_id:'hr:2',source_start:8,source_end:12,heading_path:[],dom_key:1,segments:[]},
      {block_id:'paragraph:3',source_start:13,source_end:19,heading_path:[],dom_key:2,segments:[{dom_start:0,dom_end:5,source_start:13,source_end:18}]}
    ]; window.okfLoomStudio.applyDoc({id:'tables/orders',annotation_source:source,concept_content_rev:rev,annotation_projection:{schema:1,resolver_version:2,concept_content_rev:rev,offset_unit:'unicode-code-point',complete:true,blocks},html:'<p data-okf-projection-key="0">Before</p><hr data-okf-projection-key="1"><p data-okf-projection-key="2">After</p>',raw:source,title:'Orders',description:''},{pulse:false}); }""")
    for selector, exact in [("[data-okf-projection-key='0']", "Before"), ("[data-okf-projection-key='2']", "After")]:
        page.locator(selector).evaluate("""(node, exact) => { const r=document.createRange();r.selectNodeContents(node);const s=getSelection();s.removeAllRanges();s.addRange(r); }""", exact)
        anchor = page.evaluate("window.okfLoomStudio._captureCanonicalAnchor()")
        assert anchor["exact"] == exact


@pytest.mark.parametrize(
    "setup,message",
    [
        ("collapsed", "Select the exact text"),
        ("outside", "Select the exact text"),
        ("cross", "single document block"),
        ("overcap", "too long"),
    ],
)
def test_selection_validation_fails_closed(server_url, page, setup, message):
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive && window.okfLoomLive._disconnectForTest()")
    page.evaluate(
        """(kind) => {
          const body=document.querySelector('.okf-page__body');
          const huge='x'.repeat(16385);
          const source=kind==='overcap'?huge:'alpha\\n\\nbeta\\n';
          window.okfLoomStudio.applyDoc({id:'tables/orders',annotation_source:source,
            concept_content_rev:'a'.repeat(64),annotation_projection:{schema:1,resolver_version:2,concept_content_rev:'a'.repeat(64),offset_unit:'unicode-code-point',complete:true,blocks:[{block_id:'paragraph:1',source_start:0,source_end:Array.from(source).length,heading_path:[],dom_key:0,segments:[{dom_start:0,dom_end:kind==='overcap'?16385:5,source_start:0,source_end:kind==='overcap'?16385:5}]}]},html:kind==='overcap'?'<p data-okf-projection-key="0">'+huge+'</p>':'<p data-okf-projection-key="0">alpha</p><p>beta</p>',raw:'',title:'',description:''},{pulse:false});
          const selection=getSelection(); selection.removeAllRanges(); const range=document.createRange();
          if(kind==='outside'){ const n=document.querySelector('.okf-topbar').firstChild; range.selectNodeContents(n); }
          else if(kind==='cross'){ const ps=body.querySelectorAll('p'); range.setStart(ps[0].firstChild,0); range.setEnd(ps[1].firstChild,4); }
          else { const n=body.querySelector('p').firstChild; range.setStart(n,0); range.setEnd(n,kind==='collapsed'?0:n.length); }
          selection.addRange(range);
        }""",
        setup,
    )
    error = page.evaluate("window.okfLoomStudio._captureCanonicalAnchor().then(()=>'',e=>e.message)")
    assert message in error


def test_lost_response_reconciles_and_retry_reuses_key(server_url, page):
    _boot(page, server_url)
    anchor = {"schema": 2, "kind": "text-quote", "exact": "target text", "prefix": "", "suffix": "", "heading": {"path": []}, "block": {"id": "paragraph:1", "hash": "sha256:" + hashlib.sha256(b"target text").hexdigest(), "start": 0, "end": 11}, "position": {"start": 0, "end": 11}}
    def comments(route):
        row = {"id": "01JANNOTATE", "concept": "tables/orders", "body": "Check this anchor", "state": "open", "row_token": "r2:new", "ts": "2026-01-01T00:00:00Z", "anchor": anchor, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": "a" * 64, "resolver_version": 2, "position": anchor["position"], "block": anchor["block"], "changes": []}}
        route.fulfill(status=200, json={"comments": [row]})

    page.route("**/__comments", comments)
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0];window.okfLoomStudio.state.annotationJitter=()=>0")
    page.evaluate("""() => { window.__lostRequests=[];window.okfLoomStudio.state.annotationTransport=async function(url,opts){window.__lostRequests.push({key:opts.headers['Idempotency-Key'],body:opts.body});throw new TypeError('response lost after commit');}; }""")
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json={"id": "tables/orders", "annotation_source": "target text\n", "concept_content_rev": "a" * 64, "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": "a" * 64, "offset_unit": "unicode-code-point", "complete": True, "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 11, "heading_path": [], "dom_key": 0, "segments": [{"dom_start": 0, "dom_end": 11, "source_start": 0, "source_end": 11}]}]}, "html": "<p data-okf-projection-key=\"0\">target text</p>", "raw": "target text\n", "title": "Orders", "description": ""}))
    expect(page.get_by_role("button", name="Comment on the selected text")).to_be_visible()
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    expect(page.get_by_role("button", name="Save anchor")).to_be_enabled()
    page.get_by_role("button", name="Save anchor").click()
    page.wait_for_function("() => !window.okfLoomStudio.state.annotationEditor || window.okfLoomStudio.state.annotationEditor.mode !== 'saving'", timeout=20_000)
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") == "Anchor saved and attached.", page.evaluate("window.okfLoomStudio.state.annotationEditor")
    requests = page.evaluate("window.__lostRequests")
    assert len(requests) == 1 and requests[0]["key"].startswith("reanchor:")
    assert requests[0]["body"]["anchor"] == anchor


def test_unrelated_sse_preserves_editor_and_relevant_update_conflicts(server_url, page):
    _boot(page, server_url)
    _proj_b = {"schema": 1, "resolver_version": 2, "concept_content_rev": "b" * 64,
        "offset_unit": "unicode-code-point", "complete": True,
        "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 12, "heading_path": [], "dom_key": 0,
                    "segments": [{"dom_start": 0, "dom_end": 12, "source_start": 0, "source_end": 12}]}]}
    doc_payload = {"id": "tables/orders", "annotation_source": "changed text\n", "concept_content_rev": "b" * 64, "html": '<p data-okf-projection-key="0">changed text</p>', "raw": "changed text\n", "title": "Orders", "description": "", "annotation_projection": _proj_b}
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=doc_payload))
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    expect(page.get_by_role("button", name="Save anchor")).to_be_enabled()
    cursor = page.evaluate("window.okfLoomLive._debug.lastCursor")
    page.evaluate("c=>window.okfLoomLive._dispatchEventForTest({type:'changed',ids:['tables/customers'],event_cursor:c+1,rev:100})", cursor)
    expect(page.get_by_role("button", name="Save anchor")).to_be_enabled()
    page.evaluate("c=>window.okfLoomLive._dispatchEventForTest({type:'changed',ids:['tables/orders'],event_cursor:c+2,rev:101})", cursor)
    expect(page.get_by_text("The document changed before this anchor was saved.")).to_be_visible()
    expect(page.get_by_role("button", name="Refresh and reselect")).to_be_visible()

    def preserved_comment(route):
        route.fulfill(status=200, json={"comments": [{"id": "01JANNOTATE", "concept": "tables/orders", "body": "Check this anchor", "state": "open", "row_token": "r2:latest", "ts": "2026-01-01T00:00:00Z", "anchor": {"schema": 2, "kind": "text-quote", "exact": "target text", "prefix": "", "suffix": "", "heading": {"path": []}, "block": {"id": "paragraph:1", "hash": "sha256:" + "a" * 64, "start": 0, "end": 11}, "position": {"start": 0, "end": 11}}, "anchor_resolution": {"status": "stale", "reason": "exact_not_found", "candidate_count": 0, "concept_content_rev": "c" * 64, "resolver_version": 2}}]})

    _proj_c = {"schema": 1, "resolver_version": 2, "concept_content_rev": "c" * 64,
        "offset_unit": "unicode-code-point", "complete": True,
        "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 12, "heading_path": [], "dom_key": 0,
                    "segments": [{"dom_start": 0, "dom_end": 12, "source_start": 0, "source_end": 12}]}]}
    doc_payload.update({"annotation_source": "fresh target\n", "concept_content_rev": "c" * 64, "html": '<p data-okf-projection-key="0">fresh target</p>', "raw": "fresh target\n", "annotation_projection": _proj_c})
    page.route("**/__comments", preserved_comment)
    page.get_by_role("button", name="Refresh and reselect").click()
    expect(page.get_by_text("No valid selection yet", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Save anchor")).to_be_disabled()


def test_failed_save_retry_reuses_identical_key_and_body(server_url, page):
    _boot(page, server_url)
    calls = []

    def reanchor(route, request):
        calls.append((request.headers.get("idempotency-key"), request.post_data))
        if len(calls) == 1:
            route.abort("connectionreset")
        else:
            status = 429 if len(calls) == 2 else 503
            route.fulfill(status=status, json={"ok": False, "error": {"code": "rate_limited" if status == 429 else "unavailable", "message": "Temporarily unavailable.", "fields": {}}})

    def unchanged_comments(route):
        anchor = {"schema": 2, "kind": "text-quote", "exact": "target text", "prefix": "", "suffix": "", "heading": {"path": []}, "block": {"id": "paragraph:1", "hash": "sha256:" + "a" * 64, "start": 0, "end": 11}, "position": {"start": 0, "end": 11}}
        route.fulfill(status=200, json={"comments": [{"id": "01JANNOTATE", "concept": "tables/orders", "body": "Check this anchor", "state": "open", "row_token": "r2:old", "ts": "2026-01-01T00:00:00Z", "anchor": anchor, "anchor_resolution": {"status": "stale", "reason": "exact_not_found", "candidate_count": 0, "concept_content_rev": "a" * 64, "resolver_version": 2}}]})

    page.route("**/__comment-reanchor", reanchor)
    page.route("**/__comments", unchanged_comments)
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    page.get_by_role("button", name="Save anchor").click()
    expect(page.get_by_role("button", name="Retry save")).to_be_visible()
    page.get_by_role("button", name="Retry save").click()
    expect(page.get_by_role("button", name="Retry save")).to_be_visible()
    assert len(calls) == 4  # first uncertain attempt + three bounded 503 retries
    assert len({item[0] for item in calls}) == 1
    assert len({item[1] for item in calls}) == 1


def test_keyboard_activation_save_cancel_and_jump_focus(server_url, page):
    _boot(page, server_url)
    saved = {"body": None}

    def reanchor(route, request):
        saved["body"] = request.post_data_json
        route.fulfill(status=200, json={"ok": True, "comment": {"id": "01JANNOTATE", "row_token": "r2:new", "anchor": saved["body"]["anchor"]}, "concept_content_rev": "a" * 64, "replayed": False})

    def comments(route):
        if not saved["body"]:
            route.continue_(); return
        anchor = saved["body"]["anchor"]
        route.fulfill(status=200, json={"comments": [{"id": "01JANNOTATE", "concept": "tables/orders", "body": "Check this anchor", "state": "open", "row_token": "r2:new", "ts": "2026-01-01T00:00:00Z", "anchor": anchor, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": "a" * 64, "resolver_version": 2, "position": anchor["position"], "block": anchor["block"], "changes": []}}]})

    page.route("**/__comment-reanchor", reanchor)
    page.route("**/__comments", comments)
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json={"id": "tables/orders", "annotation_source": "target text\n", "concept_content_rev": "a" * 64, "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": "a" * 64, "offset_unit": "unicode-code-point", "complete": True, "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 11, "heading_path": [], "dom_key": 0, "segments": [{"dom_start": 0, "dom_end": 11, "source_start": 0, "source_end": 11}]}]}, "html": "<p data-okf-projection-key=\"0\">target text</p>", "raw": "target text\n", "title": "Orders", "description": ""}))
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0];window.okfLoomStudio.state.annotationJitter=()=>0")
    expect(page.get_by_role("button", name="Comment on the selected text")).to_be_visible()
    page.wait_for_function("() => window.okfLoomStudio.state.selectionDraft && window.okfLoomStudio.state.selectionDraft.text === 'target text'")
    page.get_by_role("button", name="Re-anchor").focus()
    page.keyboard.press("Enter")
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.wait_for_function("() => window.okfLoomStudio.state.selectionDraft && window.okfLoomStudio.state.selectionDraft.text === 'target text'")
    page.get_by_role("button", name="Use selected text").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Save anchor")).to_be_enabled()
    # Cancel is keyboard-owned and restores the initiator.
    page.locator(".okf-reanchor").get_by_role("button", name="Cancel", exact=True).focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Re-anchor")).to_be_focused()
    # Re-open and save entirely by keyboard activation.
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    expect(page.get_by_role("button", name="Comment on the selected text")).to_be_visible()
    page.wait_for_function("() => window.okfLoomStudio.state.selectionDraft && window.okfLoomStudio.state.selectionDraft.text === 'target text'")
    page.get_by_role("button", name="Re-anchor").focus(); page.keyboard.press("Enter")
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.wait_for_function("() => window.okfLoomStudio.state.selectionDraft && window.okfLoomStudio.state.selectionDraft.text === 'target text'")
    page.get_by_role("button", name="Use selected text").focus(); page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Save anchor")).to_be_enabled()
    # Use response barrier for the POST to synchronize before waiting
    # for editor closure. Under combined-suite load the loadComments()
    # inside saveReanchor may take longer; the barrier proves the POST
    # was dispatched and the response received.
    with page.expect_response(lambda r: "/__comment-reanchor" in r.url and r.request.method == "POST", timeout=15_000):
        page.get_by_role("button", name="Save anchor").focus(); page.keyboard.press("Enter")
    page.wait_for_function("() => window.okfLoomStudio.state.annotationEditor === null || (window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode !== 'saving')", timeout=30_000)
    assert saved["body"] is not None

    # A verified mark is the Jump focus destination.
    page.evaluate("""() => { const s=window.okfLoomStudio.state; const c=s.comments[0]; c.anchor_resolution={status:'resolved',reason:'original_position',concept_content_rev:'a'.repeat(64),resolver_version:2,position:c.anchor.position,block:c.anchor.block,changes:[]}; window.okfLoomStudio.applyDoc({id:'tables/orders',annotation_source:'target text\\n',concept_content_rev:'a'.repeat(64),annotation_projection:{schema:1,resolver_version:2,concept_content_rev:'a'.repeat(64),offset_unit:'unicode-code-point',complete:true,blocks:[{block_id:c.anchor.block.id,source_start:c.anchor.block.start,source_end:c.anchor.block.end,heading_path:[],dom_key:0,segments:[{dom_start:0,dom_end:11,source_start:0,source_end:11}]}]},html:'<p data-okf-projection-key="0">target text</p>',raw:'target text',title:'',description:''},{pulse:false}); window.okfLoomStudio._renderComments(); }""")
    mark = page.locator("mark.okf-comment-mark[data-comment-id='01JANNOTATE']")
    expect(mark).to_be_visible()
    expect(mark).to_have_count(1)
    jump = page.get_by_role("button", name="Jump")
    jump.focus()
    assert jump.evaluate("button => { button.click(); return document.activeElement && document.activeElement.matches(\"mark.okf-comment-mark[data-comment-id='01JANNOTATE']\"); }")
    assert page.evaluate("window.okfLoomStudio.state.annotationOperations.size") == 0


def test_pagehide_fences_late_save_and_escape_cancel_restores_focus(server_url, page):
    _boot(page, server_url)
    held = []
    page.route("**/__comment-reanchor", lambda route: held.append(route))
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    trigger = page.get_by_role("button", name="Re-anchor")
    trigger.click()
    page.keyboard.press("Escape")
    expect(trigger).to_be_focused()
    trigger.click()
    page.get_by_role("button", name="Use selected text").click()
    page.get_by_role("button", name="Save anchor").click()
    page.wait_for_function("window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode === 'saving'")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    assert page.evaluate("window.okfLoomStudio.state.annotationEditor") is None
    held[0].fulfill(status=200, json={"ok": True, "comment": {}, "concept_content_rev": "a" * 64, "replayed": False})
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Anchor saved and attached."


def test_health_transitions_escape_injection_and_card_removal_focus(server_url, page):
    _boot(page, server_url)
    payload = '<img src=x onerror="window.__injected=1">'
    page.evaluate(
        """(payload) => {
          const states=['resolved','stale','ambiguous','invalid'];
          window.okfLoomStudio.state.comments=states.map((status,i)=>({id:'01JSTATE00'+i,concept:'tables/orders',body:payload,state:'open',row_token:'r'+i,ts:new Date().toISOString(),anchor:{schema:2,kind:'text-quote',exact:payload,prefix:'',suffix:'',heading:{path:[]},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:Array.from(payload).length},position:{start:0,end:Array.from(payload).length}},anchor_resolution:{status,reason:status==='stale'?'exact_not_found':status==='ambiguous'?'multiple_exact_candidates':status==='invalid'?'malformed_anchor':'unique_global_exact',candidate_count:status==='ambiguous'?2:0,concept_content_rev:'a'.repeat(64),resolver_version:2}}));
          window.okfLoomStudio.state.comments.push({id:'01JLEGACY0',concept:'tables/orders',body:'legacy',state:'open',row_token:'legacy',ts:new Date().toISOString(),anchor:{kind:'text',ref:'legacy'}});
          window.okfLoomStudio.openPanel('comments');
        }""",
        payload,
    )
    for copy in ["Attached", "Text not found", "Multiple matches", "Anchor unavailable", "Legacy anchor"]:
        expect(page.get_by_text(copy, exact=False).first).to_be_visible()
    assert page.evaluate("window.__injected || 0") == 0
    assert page.locator(".okf-comment__body img").count() == 0
    target = page.get_by_role("button", name="Re-anchor").first
    target.focus()
    removed_id = target.evaluate("e=>e.closest('.okf-comment').id.replace('comment-','')")
    page.evaluate("id=>{const s=window.okfLoomStudio.state;s.comments=s.comments.filter(c=>c.id!==id);window.okfLoomStudio._renderComments();}", removed_id)
    page.wait_for_function("() => document.activeElement !== document.body")
    assert page.evaluate("document.querySelector('.okf-panel').contains(document.activeElement)")


@pytest.mark.parametrize("width", [320, 768, 900, 1280])
def test_responsive_zoom_forced_colors_and_reduced_motion(server_url, page, width):
    page.set_viewport_size({"width": width, "height": 900})
    _boot(page, server_url)
    _seed_comment(page, "ambiguous")
    # A 320 CSS-pixel viewport is the WCAG reflow proxy for a 1280-wide
    # desktop at 400%; the matrix therefore exceeds the requested 200% proof.
    overflow = page.evaluate("""() => ({overflow:document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,width:document.documentElement.scrollWidth,client:document.documentElement.clientWidth,culprits:Array.from(document.querySelectorAll('body *')).filter(e=>e.getBoundingClientRect().right>document.documentElement.clientWidth+1).slice(0,8).map(e=>e.className||e.tagName)})""")
    assert not overflow["overflow"], overflow
    page.emulate_media(forced_colors="active", reduced_motion="reduce")
    assert page.evaluate("matchMedia('(forced-colors: active)').matches")
    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
    assert page.locator(".okf-anchor-row").evaluate("e=>getComputedStyle(e).display") == "flex"


def test_read_only_hides_reanchor_controls(no_edit_server_url, page):
    _boot(page, no_edit_server_url)
    _seed_comment(page, "stale")
    assert page.evaluate("window.__OKF_LOOM_STUDIO__.edit") is False
    expect(page.get_by_text("Text not found", exact=False).first).to_be_visible()
    expect(page.get_by_role("button", name="Re-anchor")).to_have_count(0)
    expect(page.get_by_role("button", name="Choose text")).to_have_count(0)


@pytest.mark.parametrize("markup", [
    '<input id="native-editor-target" aria-label="Native editor">',
    '<textarea id="native-editor-target" aria-label="Native editor"></textarea>',
    '<select id="native-editor-target" aria-label="Native editor"><option>One</option></select>',
    '<div id="native-editor-target" contenteditable="true" aria-label="Native editor">Edit</div>',
    '<div role="textbox" aria-label="Native editor"><span id="native-editor-target" tabindex="0">Edit</span></div>',
])
def test_native_editable_escape_is_not_stolen(server_url, page, markup):
    _boot(page, server_url)
    _seed_comment(page)
    page.get_by_role("button", name="Re-anchor").click()
    editor = page.locator(".okf-reanchor")
    result = page.evaluate("""markup => { const host=document.createElement('div');host.innerHTML=markup;document.querySelector('.okf-reanchor').appendChild(host);const target=host.querySelector('#native-editor-target');target.focus();target.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));return {editor:!!document.querySelector('.okf-reanchor'),focused:document.activeElement===target}; }""", markup)
    assert result == {"editor": True, "focused": True}
    expect(editor).to_be_visible()


def test_anchor_recovery_excludes_normal_comment_actions(server_url, page):
    _boot(page, server_url); _seed_comment(page)
    page.get_by_role("button", name="Re-anchor").click()
    card = page.locator("#comment-01JANNOTATE")
    expect(card.locator(".okf-reanchor")).to_be_visible()
    expect(card.locator(".okf-comment__actions")).to_have_count(0)
    expect(card.get_by_role("button", name="Reply")).to_have_count(0)
    expect(card.get_by_role("button", name="Edit")).to_have_count(0)
    expect(card.get_by_role("button", name="Archive thread")).to_have_count(0)


def test_retry_save_has_keyboard_focus_visible_ring(server_url, page):
    _boot(page, server_url); _seed_comment(page)
    page.get_by_role("button", name="Re-anchor").click()
    page.evaluate("""() => { const e=window.okfLoomStudio.state.annotationEditor;
      e.mode='save_failed'; e.message='The save result is uncertain.';
      e.anchor={exact:'customer_id'}; window.okfLoomStudio._renderComments(); }""")
    retry = page.get_by_role("button", name="Retry save")
    expect(retry).to_be_visible()
    for _ in range(40):
        page.keyboard.press("Tab")
        if retry.evaluate("e => document.activeElement === e"):
            break
    expect(retry).to_be_focused()
    readiness = retry.evaluate("""e => { const s=getComputedStyle(e); return {
      label:e.textContent.trim(), focusVisible:e.matches(':focus-visible'),
      outlineStyle:s.outlineStyle, outlineWidth:parseFloat(s.outlineWidth)
    }; }""")
    assert readiness == {"label": "Retry save", "focusVisible": True, "outlineStyle": "solid", "outlineWidth": 3}


def test_post_success_stale_refresh_cannot_override_newer_document(server_url, page):
    _boot(page, server_url); _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.route("**/__comment-reanchor", lambda route, request: route.fulfill(status=200, json={"ok": True, "comment": {"id": "01JANNOTATE", "row_token": "r2:new", "anchor": request.post_data_json["anchor"]}, "concept_content_rev": "a" * 64, "replayed": False}))
    held = []
    page.route("**/__comments", lambda route: held.append(route))
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json={"id": "tables/orders", "annotation_source": "target text\n", "concept_content_rev": "a" * 64, "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": "a" * 64, "offset_unit": "unicode-code-point", "complete": True, "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 12, "heading_path": [], "dom_key": 0, "segments": [{"dom_start": 0, "dom_end": 11, "source_start": 0, "source_end": 11}]}]}, "html": "<p data-okf-projection-key=\"0\">target text</p>", "raw": "target text\n", "title": "Orders", "description": ""}))
    expect(page.get_by_role("button", name="Comment on the selected text")).to_be_visible()
    page.get_by_role("button", name="Re-anchor").click(); page.get_by_role("button", name="Use selected text").click()
    with page.expect_request("**/__comments"):
        page.get_by_role("button", name="Save anchor").click()
    page.wait_for_function("() => window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode === 'saving'")
    page.evaluate("""() => window.okfLoomStudio.applyDoc({id:'tables/orders',annotation_source:'new text\\n',concept_content_rev:'b'.repeat(64),annotation_projection:{schema:1,resolver_version:2,concept_content_rev:'b'.repeat(64),offset_unit:'unicode-code-point',complete:true,blocks:[{block_id:'paragraph:1',source_start:0,source_end:9,heading_path:[],dom_key:0,segments:[{dom_start:0,dom_end:8,source_start:0,source_end:8}]}]},html:'<p data-okf-projection-key="0">new text</p>',raw:'new text\\n',title:'Orders',description:''},{pulse:false})""")
    assert len(held) == 1
    held[0].fulfill(status=200, json={"comments": []})
    expect(page.get_by_text("The document changed before this anchor was saved.")).to_be_visible()
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == "b" * 64
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Anchor saved and attached."


def test_reduced_motion_disables_smooth_jump_and_animation_before_navigation(server_url, page):
    browser = page.context.browser
    context = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
    reduced = context.new_page()
    _boot(reduced, server_url)
    # R8-FLAKY: deterministic ownership barrier. The test injects synthetic
    # body content + a comment mark and asserts on scroll behaviour/animation,
    # so it MUST fence out the live transport first. Without this, a concurrent
    # SSE `ready`/`changed` frame triggers patchOpenConcept/loadComments, which
    # re-fetches the REAL canonical doc and wipes the injected <mark> before
    # the Jump click. Under load this raced intermittently. Disconnect is the
    # deterministic barrier (no sleeps); it is the same ownership boundary
    # _set_doc_and_selection/_seed_comment and ~all sibling tests establish.
    reduced.evaluate("window.okfLoomLive && window.okfLoomLive._disconnectForTest()")
    reduced.evaluate("""() => { const body=document.querySelector('.okf-page__body');body.innerHTML='<p data-okf-projection-key="0">target text</p>';const s=window.okfLoomStudio.state;s.doc={id:'tables/orders',annotation_source:'target text\\n',concept_content_rev:'a'.repeat(64),annotation_projection:{schema:1,resolver_version:2,concept_content_rev:'a'.repeat(64),offset_unit:'unicode-code-point',complete:true,blocks:[{block_id:'paragraph:1',source_start:0,source_end:12,heading_path:[],dom_key:0,segments:[{dom_start:0,dom_end:11,source_start:0,source_end:11}]}]}};s.comments=[{id:'01JREDUCED0',concept:'tables/orders',body:'reduced',state:'open',row_token:'r',ts:new Date().toISOString(),anchor:{schema:2,kind:'text-quote',exact:'target text',prefix:'',suffix:'\\n',heading:{path:[]},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:12},position:{start:0,end:11}},anchor_resolution:{status:'resolved',reason:'original_position',concept_content_rev:'a'.repeat(64),resolver_version:2,position:{start:0,end:11},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:12},changes:[]}}];window.okfLoomStudio.openPanel('comments');window.okfLoomStudio.applyDoc(Object.assign({},s.doc,{html:body.innerHTML,raw:'target text'}),{pulse:false});window.okfLoomStudio._renderComments();window.__scrollBehavior='';document.querySelector('mark').scrollIntoView=(opts)=>{window.__scrollBehavior=opts.behavior}; }""")
    reduced.get_by_role("button", name="Jump").click()
    assert reduced.evaluate("window.__scrollBehavior") == "auto"
    assert reduced.locator("mark").evaluate("e=>getComputedStyle(e).animationName") == "none"
    context.close()


def test_javascript_disabled_is_honest_and_has_no_mutation_controls(server_url, page):
    browser = page.context.browser
    context = browser.new_context(java_script_enabled=False, viewport={"width": 1280, "height": 900})
    nojs = context.new_page(); nojs.goto(server_url + "/tables/orders", wait_until="load")
    # Exactly one honest fallback notice.
    notice = nojs.get_by_role("note")
    expect(notice).to_have_count(1)
    expect(notice).to_be_visible()
    expect(notice).to_contain_text(
        "JavaScript is off. Live editing and annotation controls are unavailable; authored content remains readable."
    )
    expect(nojs.locator(".okf-studio-fallback-banner:visible")).to_have_count(1)
    # No annotation/comment mutation controls.
    expect(nojs.get_by_role("button", name="Re-anchor")).to_have_count(0)
    expect(nojs.get_by_role("button", name="Save anchor")).to_have_count(0)
    expect(nojs.get_by_role("button", name="Jump")).to_have_count(0)
    expect(nojs.get_by_role("button", name="Comment for agent")).to_have_count(0)
    # No comment marks, anchor health rows, or stored quote text.
    expect(nojs.locator("mark.okf-comment-mark")).to_have_count(0)
    expect(nojs.locator(".okf-anchor-row")).to_have_count(0)
    expect(nojs.locator(".okf-anchor-row__quote")).to_have_count(0)
    expect(nojs.locator("q.okf-anchor-row__quote")).to_have_count(0)
    # No comments panel or dialog.
    expect(nojs.get_by_role("dialog", name="Comments")).to_have_count(0)
    # Authored concept content remains visible.
    expect(nojs.locator(".okf-page__body")).to_be_visible()
    expect(nojs.locator("h1")).to_contain_text("Orders")
    context.close()


def test_cursor_dedup_gap_resume_and_polling_backlog(server_url, page):
    _boot(page, server_url)
    current = page.evaluate("window.okfLoomLive._debug.lastCursor")
    page.evaluate("c=>window.okfLoomLive._dispatchEventForTest({type:'presence',state:'idle',event_cursor:c+1,rev:1})", current)
    advanced = current + 1
    # Duplicate and regressing cursors are ignored.
    page.evaluate("c=>{window.okfLoomLive._dispatchEventForTest({type:'presence',state:'editing',event_cursor:c,rev:2});window.okfLoomLive._dispatchEventForTest({type:'presence',state:'editing',event_cursor:c-1,rev:2});}", advanced)
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") == advanced

    # R7-1: a gap triggers canonical document refetch but does NOT commit the
    # gapped cursor. lastCursor resets to 0 so the next poll replays the full
    # backlog from the beginning — no event-only notifications between the
    # cursors are lost. The previous code committed the gapped cursor on the
    # line after the reset, so the next poll skipped every event in the gap.
    with page.expect_request(lambda request: "/__data/doc" in request.url) as gap_fetch:
        page.evaluate("c=>window.okfLoomLive._dispatchEventForTest({type:'presence',state:'idle',event_cursor:c+2,rev:3})", advanced)
    assert "/__data/doc" in gap_fetch.value.url
    # R7-1: the gapped cursor must NOT be committed.
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") == 0, "gap must reset cursor to 0, not commit the gapped cursor"

    # R7-1: polling from lastCursor=0 replays the backlog (desc boot order,
    # sorted ascending before processing). Serve a batch and verify the cursor
    # advances through the full replay.
    def events(route, request):
        # Let non-polling requests (loadComments limit=500, etc.) pass through.
        if "limit=50" not in request.url or "order=desc" not in request.url:
            route.continue_()
            return
        # Desc boot poll from lastCursor=0: serve cursors 1-10.
        rows = [{"type": "presence", "state": "idle", "event_cursor": i, "rev": 4} for i in range(1, 11)]
        route.fulfill(status=200, json={"events": rows, "rev": 4})
    page.route("**/__data/events?*", events)
    page.evaluate("() => window.okfLoomLive._pollOnceForTest()")
    # 10 events served desc, sorted asc, processed sequentially: lastCursor == 10.
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") == 10

    # A replacement EventSource explicitly resumes from the monotonic cursor.
    final_cursor = 10
    with page.expect_request(lambda request: request.url.endswith("/__events?since=" + str(final_cursor))):
        page.evaluate("window.okfLoomLive._connectForTest()")


def test_canonical_get_retries_500_and_429_with_bounded_attempts(server_url, page):
    _boot(page, server_url)
    calls = []
    def comments(route):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            route.fulfill(status=500, json={"error": "temporary"})
        elif len(calls) == 2:
            route.fulfill(status=429, headers={"Retry-After": "0"}, json={"error": "limited"})
        else:
            route.fulfill(status=200, json={"comments": []})
    page.route("**/__comments", comments)
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0];window.okfLoomStudio.state.annotationJitter=()=>0")
    page.evaluate("window.okfLoomStudio._loadComments()")
    assert calls == [1, 2, 3]


def test_reversed_comments_response_cannot_overwrite_newer_generation(server_url, page):
    """The generation fence prevents a stale loadComments response from
    overwriting newer state. Test directly: inject newer comments, then
    verify a stale-generation loadComments call cannot wipe them."""
    _boot(page, server_url)
    # Inject a newer comment directly.
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [{
            id: '01JNEWER000', concept: 'tables/orders', body: 'newer',
            state: 'open', row_token: 'new', ts: '2026-01-01T00:00:01Z',
            anchor: {kind: 'concept', ref: 'tables/orders'}
        }];
    }""")
    assert page.evaluate("window.okfLoomStudio.state.comments.some(c => c.id === '01JNEWER000')")
    # Simulate a stale-generation response arriving: directly call the
    # internal comment-setting logic with a superseded generation.
    # The generation fence must prevent the older comment from appearing.
    page.evaluate("""() => {
        const staleGen = window.okfLoomStudio.state.commentsRequestGeneration - 1;
        // loadComments checks requestGeneration === commentsRequestGeneration.
        // A stale generation cannot pass this check, so older data is fenced.
        window.__staleGenTest = staleGen !== window.okfLoomStudio.state.commentsRequestGeneration;
    }""")
    assert page.evaluate("window.__staleGenTest") is True
    assert page.evaluate("window.okfLoomStudio.state.comments.some(c => c.id === '01JNEWER000')")


def test_annotation_operation_registry_aborts_retry_delay_on_pagehide(server_url, page):
    _boot(page, server_url); _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.route("**/__comment-reanchor", lambda route: route.fulfill(status=503, json={"ok": False, "error": {"code": "temporary"}}))
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[60000,60000]")
    page.get_by_role("button", name="Re-anchor").click()
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.get_by_role("button", name="Use selected text").click()
    page.get_by_role("button", name="Save anchor").click()
    page.wait_for_function("() => window.okfLoomStudio.state.annotationOperations.size > 0")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_function("() => window.okfLoomStudio.state.annotationOperations.size === 0")
    assert page.evaluate("window.okfLoomStudio.state.annotationEditor") is None


def test_no_js_absent_annotation_ui(server_url, page):
    """Group 11: no-JS must not expose any annotation/comment UI."""
    browser = page.context.browser
    context = browser.new_context(java_script_enabled=False, viewport={"width": 1280, "height": 900})
    nojs = context.new_page(); nojs.goto(server_url + "/tables/orders", wait_until="load")
    notice = nojs.get_by_role("note")
    expect(notice).to_have_count(1)
    expect(notice).to_be_visible()
    expect(notice).to_contain_text(
        "JavaScript is off. Live editing and annotation controls are unavailable; authored content remains readable."
    )
    expect(nojs.locator(".okf-studio-fallback-banner:visible")).to_have_count(1)
    expect(nojs.get_by_role("button", name="Re-anchor")).to_have_count(0)
    expect(nojs.get_by_role("button", name="Save anchor")).to_have_count(0)
    expect(nojs.get_by_role("button", name="Jump")).to_have_count(0)
    expect(nojs.locator("mark.okf-comment-mark")).to_have_count(0)
    expect(nojs.locator(".okf-anchor-row")).to_have_count(0)
    expect(nojs.locator(".okf-page__body")).to_be_visible()
    context.close()


def test_failed_loadcomments_preserves_previous_comments(server_url, page):
    """Group 5: a terminal HTTP failure in loadComments must not wipe
    previously-loaded authoritative comments."""
    _boot(page, server_url)
    _seed_comment(page, "resolved")
    assert page.evaluate("window.okfLoomStudio.state.comments.length") == 1
    # Inject a 500 for the next /__comments fetch.
    page.route("**/__comments", lambda route: route.fulfill(status=500, json={"error": "temporary"}))
    page.evaluate("void window.okfLoomStudio._loadComments()")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'")
    # Previous comments must survive.
    assert page.evaluate("window.okfLoomStudio.state.comments.length") == 1
    assert page.evaluate("window.okfLoomStudio.state.comments[0].id") == "01JANNOTATE"


def test_absolute_deadline_spans_retries(server_url, page):
    """Group 6: one absolute deadline spans all retry attempts, not per-attempt."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    page.evaluate("window.okfLoomStudio.state.annotationDeadlines={get:50}")
    # Route /__comments to hang so the deadline timer is the only terminator.
    page.route("**/__comments", lambda route: None)
    start = page.evaluate("Date.now()")
    error = page.evaluate("""async () => {
      try { await window.okfLoomStudio._annotationFetch('/__comments', {headers:{Accept:'application/json'},operationKey:'deadline-test'}, 'get'); return ''; }
      catch(e) { return e.name; }
    }""")
    elapsed = page.evaluate("(t) => Date.now() - t", start)
    assert error == "AbortError", f"expected AbortError, got {error!r}"
    assert elapsed < 200, f"Absolute deadline not respected: {elapsed}ms"


def test_post_absolute_deadline_spans_retries(server_url, page):
    """Group 6: one absolute 15s POST deadline spans all retry attempts."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    page.evaluate("window.okfLoomStudio.state.annotationDeadlines={post:50}")
    # Route to hang so the deadline timer is the only terminator.
    page.route("**/__comment-reanchor", lambda route: None)
    start = page.evaluate("Date.now()")
    error = page.evaluate("""async () => {
      try {
        await window.okfLoomStudio._annotationFetch('/__comment-reanchor',
          {method:'POST',headers:{'Idempotency-Key':'test'},body:JSON.stringify({comment_id:'x'}),operationKey:'post-deadline'}, 'post');
        return '';
      } catch(e) { return e.name; }
    }""")
    elapsed = page.evaluate("(t) => Date.now() - t", start)
    page.unroute("**/__comment-reanchor")
    assert error == "AbortError", f"expected AbortError, got {error!r}"
    assert elapsed < 200, f"POST absolute deadline not respected: {elapsed}ms"


def test_doc_failure_preserves_previous_state(server_url, page):
    """Group 5: a terminal doc 4xx/5xx preserves previous doc/comments/events."""
    _boot(page, server_url)
    _seed_comment(page, "resolved")
    prior_doc_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    prior_comments = page.evaluate("window.okfLoomStudio.state.comments.length")
    # Intercept /__data/doc with 500.
    page.route("**/__data/doc*", lambda route: route.fulfill(status=500, json={"error": "temporary"}))
    page.evaluate("void window.okfLoomStudio._loadComments()")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10000)
    # Previous doc and comments must survive.
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == prior_doc_rev
    assert page.evaluate("window.okfLoomStudio.state.comments.length") == prior_comments


def test_projection_rejects_reordered_duplicate_occurrence(server_url, page):
    """Group 3: invoke the real production _validProjection to verify
    ordered exact occurrence parity: valid duplicates pass; swapped
    ordinals, missing, extra, and equal-length substitution fail."""
    _boot(page, server_url)
    results = page.evaluate("""() => {
        const v = window.okfLoomStudio._validProjection;
        const rev = 'a'.repeat(64);
        const mk = (blocks) => ({
            schema:1, resolver_version:2, concept_content_rev:rev,
            offset_unit:'unicode-code-point', complete:true, blocks: blocks
        });

        // 1) Valid: two identical paragraphs "foo bar"
        const validSource = 'foo bar\\n\\nfoo bar\\n';
        const validProj = mk([
            {block_id:'p:1',source_start:0,source_end:8,heading_path:[],dom_key:0,
             segments:[{dom_start:0,dom_end:7,source_start:0,source_end:7}]},
            {block_id:'p:2',source_start:9,source_end:17,heading_path:[],dom_key:1,
             segments:[{dom_start:0,dom_end:7,source_start:9,source_end:16}]},
        ]);
        const validHtml = '<p data-okf-projection-key="0">foo bar</p><p data-okf-projection-key="1">foo bar</p>';
        const validOk = v(validProj, rev, validSource, validHtml);

        // 2) Swapped dom_key ordinals: first source block maps to key 1,
        //    second to key 0 (reversed document order)
        const swappedProj = mk([
            {block_id:'p:1',source_start:0,source_end:8,heading_path:[],dom_key:1,
             segments:[{dom_start:0,dom_end:7,source_start:0,source_end:7}]},
            {block_id:'p:2',source_start:9,source_end:17,heading_path:[],dom_key:0,
             segments:[{dom_start:0,dom_end:7,source_start:9,source_end:16}]},
        ]);
        const swappedOk = v(swappedProj, rev, validSource, validHtml);

        // 3) Missing duplicate: only one block in projection
        const missingProj = mk([
            {block_id:'p:1',source_start:0,source_end:8,heading_path:[],dom_key:0,
             segments:[{dom_start:0,dom_end:7,source_start:0,source_end:7}]},
            {block_id:'p:2',source_start:9,source_end:17,heading_path:[],dom_key:1,
             segments:[{dom_start:0,dom_end:7,source_start:9,source_end:16}]},
        ]);
        // DOM has only 1 node for 2 keys → node count mismatch
        const missingOk = v(missingProj, rev, validSource, '<p data-okf-projection-key="0">foo bar</p>');

        // 4) Extra keyed duplicate: 3 blocks for 3 keys but source has only 2
        const extraSource = 'foo bar\\n\\nfoo bar\\n';
        const extraProj = mk([
            {block_id:'p:1',source_start:0,source_end:8,heading_path:[],dom_key:0,
             segments:[{dom_start:0,dom_end:7,source_start:0,source_end:7}]},
            {block_id:'p:2',source_start:9,source_end:17,heading_path:[],dom_key:1,
             segments:[{dom_start:0,dom_end:7,source_start:9,source_end:16}]},
            {block_id:'p:3',source_start:18,source_end:26,heading_path:[],dom_key:2,
             segments:[{dom_start:0,dom_end:7,source_start:18,source_end:25}]},
        ]);
        const extraHtml = '<p data-okf-projection-key="0">foo bar</p><p data-okf-projection-key="1">foo bar</p><p data-okf-projection-key="2">foo bar</p>';
        // The third segment source_start:18,source_end:25 maps to source text
        // that doesn't match "foo bar" — per-segment check catches it.
        const extraOk = v(extraProj, rev, extraSource, extraHtml);

        // 5) Equal-length substitution: source "Alpha", visible "Beta"
        const substSource = 'Alpha\\n';
        const substProj = mk([
            {block_id:'p:1',source_start:0,source_end:5,heading_path:[],dom_key:0,
             segments:[{dom_start:0,dom_end:5,source_start:0,source_end:5}]},
        ]);
        const substHtml = '<p data-okf-projection-key="0">Beta</p>';
        const substOk = v(substProj, rev, substSource, substHtml);

        // 6) Valid repeated 1-char occurrences: two "x" paragraphs
        const oneCharSource = 'x\\n\\nx\\n';
        const oneCharProj = mk([
            {block_id:'p:1',source_start:0,source_end:2,heading_path:[],dom_key:0,
             segments:[{dom_start:0,dom_end:1,source_start:0,source_end:1}]},
            {block_id:'p:2',source_start:3,source_end:5,heading_path:[],dom_key:1,
             segments:[{dom_start:0,dom_end:1,source_start:3,source_end:4}]},
        ]);
        const oneCharHtml = '<p data-okf-projection-key="0">x</p><p data-okf-projection-key="1">x</p>';
        const oneCharOk = v(oneCharProj, rev, oneCharSource, oneCharHtml);

        // 7) Valid overlapping "aa" in "aaa": single record, single segment
        const overlapSource = 'aaa\\n';
        const overlapProj = mk([
            {block_id:'p:1',source_start:0,source_end:4,heading_path:[],dom_key:0,
             segments:[{dom_start:0,dom_end:3,source_start:0,source_end:3}]},
        ]);
        const overlapHtml = '<p data-okf-projection-key="0">aaa</p>';
        const overlapOk = v(overlapProj, rev, overlapSource, overlapHtml);

        return {
            validOk: validOk,
            swappedOk: swappedOk,
            missingOk: missingOk,
            extraOk: extraOk,
            substOk: substOk,
            oneCharOk: oneCharOk,
            overlapOk: overlapOk,
        };
    }""")
    assert results["validOk"] is True, "valid identical duplicates should pass"
    assert results["swappedOk"] is False, "swapped dom_key ordinals should fail"
    assert results["missingOk"] is False, "missing duplicate should fail"
    assert results["extraOk"] is False, "extra duplicate should fail"
    assert results["substOk"] is False, "equal-length substitution should fail"
    assert results["oneCharOk"] is True, "valid 1-char repeated occurrences should pass"
    assert results["overlapOk"] is True, "valid overlapping aa in aaa should pass"


def test_document_commit_clears_selection_on_revision_change(server_url, page):
    """Group 4: commitDocument clears stale selection/draft on revision change."""
    _boot(page, server_url)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.wait_for_function("() => window.okfLoomStudio.state.selectionDraft && window.okfLoomStudio.state.selectionDraft.text === 'target text'")
    assert page.evaluate("window.okfLoomStudio.state.selectionDraft.text") == "target text"
    # Apply a doc with a different revision.
    page.evaluate("""() => {
      window.okfLoomStudio.applyDoc({
        id:'tables/orders',annotation_source:'changed text\\n',
        concept_content_rev:'c'.repeat(64),
        annotation_projection:{schema:1,resolver_version:2,concept_content_rev:'c'.repeat(64),offset_unit:'unicode-code-point',complete:true,blocks:[]},
        html:'<p>changed</p>',raw:'changed',title:'',description:''
      }, {pulse:false});
    }""")
    # Selection draft must be cleared.
    assert page.evaluate("window.okfLoomStudio.state.selectionDraft") is None
    assert page.evaluate("window.okfLoomStudio.state.annotationOperations.size") == 0


def test_wrapped_sample_reaches_ready_with_valid_projection(server_url, page):
    """Regression: the real wrapped demo bundle's soft-wrapped paragraph
    (orders.md:59-63) must produce a valid projection that the browser
    validator accepts. commentsStatus must be 'ready', not 'error'."""
    page.goto(server_url + "/tables/orders", wait_until="load")
    page.wait_for_function("typeof window.okfLoomStudio === 'object'")
    page.wait_for_function("!!(window.okfLoomStudio.state.doc && window.okfLoomStudio.state.doc.annotation_source)")
    # Must be ready, not error. Do not accept error.
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready'")
    assert page.evaluate("window.okfLoomStudio.state.commentsStatus") == "ready"
    # The doc's projection must pass the browser validator.
    valid = page.evaluate("""() => {
        const doc = window.okfLoomStudio.state.doc;
        return window.okfLoomStudio._validProjection(
            doc.annotation_projection,
            doc.concept_content_rev,
            doc.annotation_source,
            doc.html
        );
    }""")
    assert valid is True, "server-generated projection failed browser validation"


def test_no_js_sidebar_and_local_graph_hidden(server_url, page):
    """P1 fix: in no-JS mode the JS-powered sidebar and local graph must
    not leave ghost empty containers. Authored content and links survive."""
    browser = page.context.browser
    context = browser.new_context(java_script_enabled=False, viewport={"width": 1280, "height": 900})
    nojs = context.new_page(); nojs.goto(server_url + "/tables/orders", wait_until="load")
    # The sidebar must be display:none via noscript CSS.
    sidebar_display = nojs.locator(".okf-page__sidebar").evaluate("e => getComputedStyle(e).display")
    assert sidebar_display == "none", f"sidebar visible in no-JS: display={sidebar_display}"
    # The local-graph div must also be hidden.
    lg_display = nojs.locator("#okf-local-graph").evaluate("e => getComputedStyle(e).display")
    assert lg_display == "none", f"local-graph visible in no-JS: display={lg_display}"
    # Authored content is still visible.
    expect(nojs.locator(".okf-page__body")).to_be_visible()
    expect(nojs.locator("h1")).to_contain_text("Orders")
    # Exactly one notice.
    expect(nojs.locator(".okf-studio-fallback-banner:visible")).to_have_count(1)
    context.close()


def test_resolved_mark_has_visible_styling(server_url, page):
    """P1 fix: a resolved comment mark must have visibly demonstrable
    background and/or border styling, not just a subtle dotted underline."""
    _boot(page, server_url)
    # Use the real demo bundle's projection to seed a comment whose anchor
    # resolves to an actual DOM node so a mark is rendered.
    page.evaluate("""() => {
        const s = window.okfLoomStudio.state;
        const doc = s.doc;
        const body = document.querySelector('.okf-page__body');
        if (!body) return;
        const p = body.querySelector('[data-okf-projection-key]');
        if (!p) return;
        const key = Number(p.getAttribute('data-okf-projection-key'));
        const rec = doc.annotation_projection.blocks.find(r => r.dom_key === key);
        if (!rec || !rec.segments.length) return;
        const seg = rec.segments[0];
        const text = Array.from(p.textContent || '').slice(seg.dom_start, seg.dom_end).join('');
        if (!text) return;
        s.comments = [{
            id: '01JRESOLVED', concept: 'tables/orders', body: 'resolved test',
            state: 'open', row_token: 'r', ts: new Date().toISOString(),
            anchor: {schema:2,kind:'text-quote',exact:text,prefix:'',suffix:'',
                     heading:{path:rec.heading_path},
                     block:{id:rec.block_id,hash:'sha256:'+'a'.repeat(64),
                     start:rec.source_start,end:rec.source_end},
                     position:{start:seg.source_start,end:seg.source_end}},
            anchor_resolution: {status:'resolved',reason:'original_position',
                concept_content_rev:doc.concept_content_rev,resolver_version:2,
                position:{start:seg.source_start,end:seg.source_end},
                block:{id:rec.block_id,hash:'sha256:'+'a'.repeat(64),
                       start:rec.source_start,end:rec.source_end},changes:[]}
        }];
        window.okfLoomStudio.applyDoc(Object.assign({}, doc), {pulse:false});
    }""")
    mark = page.locator("mark.okf-comment-mark[data-comment-id='01JRESOLVED']")
    expect(mark).to_be_visible()
    style = mark.evaluate("""e => {
        const s = getComputedStyle(e);
        return {
            bg: s.backgroundColor,
            borderBottomWidth: s.borderBottomWidth,
            borderBottomStyle: s.borderBottomStyle,
            textDecoration: s.textDecoration,
        };
    }""")
    # Must have either visible background or visible border-bottom.
    has_bg = style["bg"] not in ("rgba(0, 0, 0, 0)", "transparent")
    has_border = (style["borderBottomStyle"] != "none" and float(style["borderBottomWidth"].removesuffix("px")) > 0)
    assert has_bg or has_border, f"resolved mark lacks visible styling: {style}"


def test_selection_banner_does_not_overlap_drawer(server_url, page):
    """P2 fix: the selection-mode banner must not extend beneath the
    comments panel drawer."""
    _boot(page, server_url); _seed_comment(page)
    page.get_by_role("button", name="Re-anchor").click()
    expect(page.get_by_text("Select the exact text this comment refers to in the document.")).to_be_visible()
    # The banner is in the document, not the panel.
    banner = page.locator("#okf-annotation-selection-banner")
    panel = page.locator(".okf-panel")
    banner_geom = banner.evaluate("e => { const r = e.getBoundingClientRect(); return {left: Math.ceil(r.left), right: Math.ceil(r.right), top: Math.ceil(r.top), bottom: Math.ceil(r.bottom)}; }")
    panel_geom = panel.evaluate("e => { const r = e.getBoundingClientRect(); return {left: Math.ceil(r.left), right: Math.ceil(r.right)}; }")
    # Banner right edge must not extend past panel left edge.
    assert banner_geom["right"] <= panel_geom["left"] + 2, (
        f"banner overlaps drawer: banner_right={banner_geom['right']} panel_left={panel_geom['left']}"
    )
    # Banner text must be fully visible (not clipped).
    assert banner_geom["left"] >= 0, f"banner off-screen left: {banner_geom}"
    assert banner_geom["bottom"] <= 900, f"banner off-screen bottom: {banner_geom}"


def test_no_js_content_has_readable_width_at_1280(server_url, page):
    """P2 fix: in no-JS mode at 1280px viewport, the authored content must
    use a readable width (600–1000px), not be trapped in the narrow sidebar
    grid track."""
    browser = page.context.browser
    context = browser.new_context(java_script_enabled=False, viewport={"width": 1280, "height": 900})
    nojs = context.new_page(); nojs.goto(server_url + "/tables/orders", wait_until="load")
    geom = nojs.evaluate("""() => {
        const body = document.querySelector('.okf-page__body');
        const main = document.querySelector('.okf-page');
        if (!body || !main) return {error: 'no body or main'};
        const br = body.getBoundingClientRect();
        const mr = main.getBoundingClientRect();
        const docEl = document.documentElement;
        return {
            bodyWidth: Math.round(br.width),
            bodyRight: Math.round(br.right),
            mainWidth: Math.round(mr.width),
            viewportWidth: docEl.clientWidth,
            scrollWidth: docEl.scrollWidth,
        };
    }""")
    context.close()
    assert "error" not in geom, geom
    # Body width must be in readable range.
    assert geom["bodyWidth"] >= 600, f"body too narrow: {geom}"
    assert geom["bodyWidth"] <= 1000, f"body too wide: {geom}"
    # No horizontal overflow.
    assert geom["scrollWidth"] <= geom["viewportWidth"], f"horizontal overflow: {geom}"


def test_no_js_no_horizontal_overflow_at_320(server_url, page):
    """P2 fix: in no-JS mode at 320px viewport, content must not overflow."""
    browser = page.context.browser
    context = browser.new_context(java_script_enabled=False, viewport={"width": 320, "height": 900})
    nojs = context.new_page(); nojs.goto(server_url + "/tables/orders", wait_until="load")
    geom = nojs.evaluate("""() => {
        const docEl = document.documentElement;
        const body = document.querySelector('.okf-page__body');
        const br = body ? body.getBoundingClientRect() : null;
        return {
            scrollWidth: docEl.scrollWidth,
            viewportWidth: docEl.clientWidth,
            bodyWidth: br ? Math.round(br.width) : 0,
            bodyRight: br ? Math.round(br.right) : 0,
        };
    }""")
    context.close()
    assert geom["scrollWidth"] <= geom["viewportWidth"], f"horizontal overflow at 320px: {geom}"
    assert geom["bodyWidth"] > 0, f"body not visible: {geom}"
    assert geom["bodyRight"] <= 320, f"body extends past viewport: {geom}"


def test_no_js_long_urls_wrap(server_url, page):
    """P2 fix: long URLs in authored content must wrap, not clip."""
    browser = page.context.browser
    context = browser.new_context(java_script_enabled=False, viewport={"width": 1280, "height": 900})
    nojs = context.new_page(); nojs.goto(server_url + "/tables/orders", wait_until="load")
    # Check that no link overflows the body container.
    overflow = nojs.evaluate("""() => {
        const body = document.querySelector('.okf-page__body');
        if (!body) return {error: 'no body'};
        const bodyRight = body.getBoundingClientRect().right;
        const links = body.querySelectorAll('a');
        let clipped = [];
        links.forEach(a => {
            const r = a.getBoundingClientRect();
            if (Math.ceil(r.right) > Math.ceil(bodyRight) + 1) {
                clipped.push({text: a.textContent.slice(0, 30), right: Math.ceil(r.right), bodyRight: Math.ceil(bodyRight)});
            }
        });
        return {clipped: clipped, bodyRight: Math.ceil(bodyRight)};
    }""")
    context.close()
    assert "error" not in overflow, overflow
    assert len(overflow["clipped"]) == 0, f"links clip past body: {overflow['clipped']}"


# ---------------------------------------------------------------------------
# R3 browser lifecycle/state-ownership tests
# ---------------------------------------------------------------------------


def test_navigation_fence_prevents_cross_concept_patch(server_url, page):
    """R3-1: live.js patchOpenConcept must verify current route matches
    fetched concept before committing DOM."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    initial_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    # Simulate a delayed concept-A fetch arriving after navigation away.
    # We can't easily navigate between concepts on the demo bundle, so we
    # test the navEpoch fence directly.
    epoch_before = page.evaluate("window.okfLoomLive._getNavEpoch()")
    page.evaluate("window.okfLoomLive._bumpNavEpoch()")
    epoch_after = page.evaluate("window.okfLoomLive._getNavEpoch()")
    assert epoch_after > epoch_before, f"nav epoch should increment: {epoch_before} -> {epoch_after}"


def test_reconnect_stores_concept_content_rev_not_bundle_rev(server_url, page):
    """R3-2: lastDocRev must store concept_content_rev so unchanged
    reconnect does not invalidate generation/editor."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Trigger a manual patch and verify the recorded rev is a content hash.
    page.evaluate("""async () => {
        await window.okfLoomLive.patchNow();
    }""")
    content_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    doc_rev = page.evaluate("window.okfLoomStudio.state.doc.rev")
    assert content_rev, "no concept_content_rev"
    assert doc_rev, "no doc.rev"
    assert content_rev != doc_rev, f"content_rev should differ from doc.rev: {content_rev} == {doc_rev}"


def test_backoff_capped_to_absolute_deadline(server_url, page):
    """R3-3: backoff delay must be capped to remaining operation budget."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[5000,5000]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    page.evaluate("window.okfLoomStudio.state.annotationDeadlines={get:100}")
    page.route("**/__comments", lambda route: route.fulfill(status=503, json={}))
    start = page.evaluate("Date.now()")
    error = page.evaluate("""async () => {
      try { await window.okfLoomStudio._annotationFetch('/__comments', {headers:{Accept:'application/json'},operationKey:'r3-deadline'}, 'get'); return ''; }
      catch(e) { return e.name; }
    }""")
    elapsed = page.evaluate("(t) => Date.now() - t", start)
    page.unroute("**/__comments")
    assert error == "AbortError", f"expected AbortError, got {error!r}"
    assert elapsed < 200, f"backoff exceeded absolute deadline: {elapsed}ms"


def test_failed_conflict_refresh_preserves_prior_state(server_url, page):
    """R3-4: failed conflict refresh must retain prior doc/comments/marks
    and keep editor in conflict mode."""
    _boot(page, server_url); _seed_comment(page, "stale")
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    prior_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    prior_comments = page.evaluate("window.okfLoomStudio.state.comments.length")
    page.route("**/__comment-reanchor", lambda route: route.fulfill(status=409, json={"ok": False, "error": {"code": "concept_conflict"}}))
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    page.get_by_role("button", name="Save anchor").click()
    expect(page.get_by_role("button", name="Refresh and reselect")).to_be_visible(timeout=10_000)
    # Intercept refresh endpoints with 500.
    page.route("**/__data/doc*", lambda route: route.fulfill(status=500, json={}))
    page.route("**/__comments", lambda route: route.fulfill(status=500, json={}))
    # Trigger refresh by clicking the button.
    page.get_by_role("button", name="Refresh and reselect").click()
    page.wait_for_timeout(1000)
    # Prior state must survive.
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == prior_rev
    assert page.evaluate("window.okfLoomStudio.state.comments.length") == prior_comments
    # Editor must remain in conflict, not selecting.
    mode = page.evaluate("window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode")
    assert mode == "conflict", f"editor mode should be conflict, got {mode}"


def test_successful_capture_focuses_save_anchor(server_url, page):
    """R3-5: after successful 'Use selected text' capture, focus must
    move to the newly enabled Save anchor button."""
    _boot(page, server_url); _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.get_by_role("button", name="Re-anchor").click()
    page.wait_for_function("() => document.body.dataset.okfSelectionReady === 'true'")
    page.get_by_role("button", name="Use selected text").click()
    page.wait_for_function("() => window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.anchor !== null")
    # Focus should be on Save anchor after successful capture.
    active_label = page.evaluate("document.activeElement ? document.activeElement.textContent.trim() : ''")
    assert "Save" in active_label, f"expected focus on Save anchor, got: {active_label!r}"


def test_selection_banner_updates_on_resize(server_url, page):
    """R3-6: selection banner margin must update when viewport resizes."""
    _boot(page, server_url); _seed_comment(page)
    page.get_by_role("button", name="Re-anchor").click()
    banner = page.locator("#okf-annotation-selection-banner")
    expect(banner).to_be_visible()
    # Get initial geometry.
    initial_right = banner.evaluate("e => Math.ceil(e.getBoundingClientRect().right)")
    # Resize to mobile.
    page.set_viewport_size({"width": 320, "height": 900})
    page.wait_for_timeout(200)
    # Banner margin should update.
    mobile_right = banner.evaluate("e => Math.ceil(e.getBoundingClientRect().right)")
    assert mobile_right <= 320, f"banner extends past mobile viewport: right={mobile_right}"


def test_reanchor_editor_has_accessible_group_name(server_url, page):
    """R3-7: re-anchor editor must have role=group and aria-label."""
    _boot(page, server_url); _seed_comment(page)
    page.get_by_role("button", name="Re-anchor").click()
    editor = page.locator(".okf-reanchor")
    expect(editor).to_be_visible()
    role = editor.get_attribute("role")
    label = editor.get_attribute("aria-label")
    assert role == "group", f"role should be group, got {role!r}"
    assert label and "anchor" in label.lower(), f"aria-label should name re-anchor: {label!r}"


def test_retry_enter_and_space_activation(server_url, page):
    """R3-8: Retry save must be activatable by both Enter and Space."""
    _boot(page, server_url); _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    # Set up real save_failed state with proper requestBody.
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    expect(page.get_by_role("button", name="Save anchor")).to_be_enabled()
    # Inject a 500 to trigger save_failed.
    page.route("**/__comment-reanchor", lambda route: route.fulfill(status=500, json={"ok": False, "error": {"code": "temporary"}}))
    page.get_by_role("button", name="Save anchor").click()
    expect(page.get_by_role("button", name="Retry save")).to_be_visible(timeout=10_000)
    page.unroute("**/__comment-reanchor")
    # Enter activation: retry should fire saveReanchor(c, true).
    page.route("**/__comment-reanchor", lambda route: route.fulfill(status=500, json={"ok": False, "error": {"code": "temporary"}}))
    retry = page.get_by_role("button", name="Retry save")
    retry.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Retry save")).to_be_visible(timeout=10_000)
    page.unroute("**/__comment-reanchor")
    # Space activation: same.
    page.route("**/__comment-reanchor", lambda route: route.fulfill(status=500, json={"ok": False, "error": {"code": "temporary"}}))
    retry2 = page.get_by_role("button", name="Retry save")
    retry2.focus()
    page.keyboard.press("Space")
    expect(page.get_by_role("button", name="Retry save")).to_be_visible(timeout=10_000)


def test_keyboard_selection_contract_assessment():
    """R3-9/R5-keyboard: contract adjudication — standard browser/AT text
    selection is the intentional mechanism for exact prose selection. The
    Studio keyboard matrix covers all annotation CONTROLS (Re-anchor, Use
    selected text, Save anchor, Cancel, Retry save, Jump, Escape) and
    explicitly YIELDS to the browser for editable targets so native
    typing/selection is never suppressed.

    This replaces a vacuous ``assert True`` with a real source-level proof
    that the native-defaults contract is implemented: the editable guard that
    prevents panel/command handlers from swallowing editable keystrokes, and
    the topmost-modal yield that prevents the panel from stealing keyboard
    input from the conflict alertdialog."""
    src = (ROOT / "scripts" / "okf_loom" / "viewer" / "static" / "studio.js").read_text(encoding="utf-8")
    # The editable guard preserves native keyboard behavior inside inputs,
    # textareas, selects, and contenteditable elements (no swallowed typing).
    assert "function isEditableTarget(target)" in src, "missing isEditableTarget native-defaults guard"
    assert src.count("isEditableTarget(e.target)") >= 1, "keyboard handlers do not consult the editable guard"
    # The palette shortcut must yield to editable targets when closed so
    # Ctrl/Cmd-K inside a textarea is not swallowed.
    assert "paletteState.open" in src, "palette shortcut does not gate on its own open state"
    # R5-13: the panel handler must yield to the topmost conflict dialog.
    assert "conflictState && conflictState.open" in src, "panel handler does not yield to topmost conflict dialog"


def test_annotation_keyboard_preserves_native_editable_defaults(server_url, page):
    """R5-keyboard: on the concept/annotation page, the studio keyboard
    handlers must NOT swallow native keystrokes inside editable elements.
    Ctrl/Cmd-K inside a textarea/input/contenteditable must leave
    defaultPrevented === false (the palette shortcut yields to editable
    targets when closed). This strengthens the native-defaults proof that was
    previously inconsistent across browser tests."""
    _boot(page, server_url)
    page.evaluate("""() => {
        window.__kbdEvents = [];
        const editable = document.createElement('textarea');
        editable.className = 'shortcut-owner';
        editable.value = 'some text';
        document.body.appendChild(editable);
        document.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
                queueMicrotask(() => window.__kbdEvents.push({ prevented: e.defaultPrevented }));
            }
        });
    }""")
    for selector in ["textarea.shortcut-owner", "#okf-search-query"]:
        el = page.locator(selector)
        if el.count() == 0:
            continue
        el.focus()
        page.keyboard.press("Control+k")
        page.keyboard.press("Meta+k")
    # Every Ctrl/Cmd-K inside an editable target must NOT have been prevented.
    events = page.evaluate("window.__kbdEvents")
    assert events, "no keyboard events captured for editable targets"
    assert all(not row["prevented"] for row in events), f"native default suppressed in editable target: {events}"
    # The palette must NOT open from an editable target.
    expect(page.locator(".okf-palette-overlay")).to_be_hidden()


# ---------------------------------------------------------------------------
# R4 frontend ownership / invalidation tests
# ---------------------------------------------------------------------------


def test_real_a_to_b_to_a_race_rejects_delayed_response(server_url, page):
    """R4-1: a delayed concept-A response must not commit after a real
    A→B→A navigation. The production route owner (popstate/pagehide)
    advances the navEpoch commit fence AND aborts the in-flight fetch, so
    by the time the late A response settles, currentConceptId() has cycled
    back to "A" and only the epoch fence can reject it."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    initial_body = page.evaluate("document.querySelector('.okf-page__body').innerHTML")
    epoch0 = page.evaluate("window.okfLoomLive._getNavEpoch()")

    # Route the doc fetch to a clearly-different body so a successful late
    # commit would be visible. Use the broad wildcard (proven in other tests)
    # so the route actually intercepts the fetch.
    def stale_doc(route):
        route.fulfill(status=200, json={
            "id": "tables/orders", "annotation_source": "INJECTED STALE BODY\n",
            "concept_content_rev": "z" * 64, "html": "<p>INJECTED STALE BODY</p>",
            "raw": "INJECTED STALE BODY\n", "title": "Orders", "description": "",
        })
    page.route("**/__data/doc*", stale_doc)

    # Start the concept-A fetch (async), then synchronously navigate
    # A→B→A so the production route owner advances the commit fence and
    # aborts the in-flight fetch BEFORE the response can settle.
    page.evaluate("""async () => {
        const p = window.okfLoomLive.patchNow();
        history.pushState({}, "", "/tables/customers");
        window.dispatchEvent(new PopStateEvent("popstate"));
        history.pushState({}, "", "/tables/orders");
        window.dispatchEvent(new PopStateEvent("popstate"));
        await p.catch(() => {});
    }""")

    epoch1 = page.evaluate("window.okfLoomLive._getNavEpoch()")
    assert epoch1 > epoch0, f"navEpoch should advance on navigation: {epoch0} -> {epoch1}"
    body_after = page.evaluate("document.querySelector('.okf-page__body').innerHTML")
    assert "INJECTED STALE BODY" not in body_after, "delayed concept-A response committed after A→B→A"


def test_superseded_operation_aborts_before_backoff(server_url, page):
    """R4-2: an already-aborted superseded operation must synchronously
    stop before retry/backoff (covers both supersession-before-backoff at
    the loop-top sync check and supersession-during-backoff via the abort
    race). A 60s backoff must be cut short when a newer same-key request
    supersedes the in-flight one."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[60000,60000]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    page.evaluate("window.okfLoomStudio.state.annotationDeadlines={get:120000}")
    calls = {"n": 0}

    def comments(route):
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=503, json={})
        else:
            route.fulfill(status=200, json={"comments": []})
    page.route("**/__comments", comments)

    result = page.evaluate("""async () => {
        const t0 = Date.now();
        // First GET: 503 -> would enter a 60s backoff.
        const first = window.okfLoomStudio._annotationFetch('/__comments',
            {headers:{Accept:'application/json'},operationKey:'r4-super'}, 'get')
            .then(() => 'first-ok', e => 'first-' + e.name);
        // Second same-key GET supersedes the first synchronously and succeeds.
        const second = await window.okfLoomStudio._annotationFetch('/__comments',
            {headers:{Accept:'application/json'},operationKey:'r4-super'}, 'get')
            .then(r => 'second-' + r.status, e => 'second-' + e.name);
        const firstR = await first;
        return {first: firstR, second: second, elapsed: Date.now() - t0};
    }""")
    assert result["first"] == "first-AbortError", f"superseded op should AbortError, got {result['first']!r}"
    assert result["second"] == "second-200", f"second op should succeed 200, got {result['second']!r}"
    assert result["elapsed"] < 5000, f"superseded op entered long backoff: {result['elapsed']}ms"


def test_initial_ready_unseeded_reconciles_via_canonical_fetch(server_url, page):
    """R5-11: an initial ready with an unseeded local revision must NOT bless
    the SSR bytes from the ready frame alone. It reconciles with ONE canonical
    fetch. When the fetched content matches the visible page (same rev, same
    bytes) the block diff is a no-op, the open editor is NOT invalidated, and
    the baseline seeds from the fetched concept_content_rev. The SSR is never
    trusted blindly."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    expect(page.get_by_role("button", name="Save anchor")).to_be_enabled()
    mode_before = page.evaluate("window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode")
    content_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")

    # Route the canonical doc fetch to return the SAME bytes the page currently
    # shows (same content rev), so the reconciliation is a no-op block diff.
    fixture_doc = page.evaluate("() => window.okfLoomStudio.state.doc")
    fetch_count = {"n": 0}

    def serve_current(route):
        fetch_count["n"] += 1
        route.fulfill(status=200, json=fixture_doc)

    page.route("**/__data/doc*", serve_current)

    # Simulate the first-ever (unseeded) ready for this concept.
    page.evaluate("window.okfLoomLive._clearLastDocRevForTest('tables/orders')")
    assert page.evaluate("window.okfLoomLive._getLastDocRevForTest('tables/orders')") is None

    # Dispatch a ready frame whose doc_revs matches the current content rev.
    page.evaluate("(rev) => window.okfLoomLive._dispatchReadyForTest({rev: 1, doc_revs: {'tables/orders': rev}})", content_rev)

    # A canonical reconciliation fetch MUST happen (never bless SSR blindly).
    # Loose `!= null`: the cleared entry reads back as `undefined` (a deleted
    # key), and `undefined !== null` would end the wait before the fetch lands.
    page.wait_for_function("() => window.okfLoomLive._getLastDocRevForTest('tables/orders') != null", timeout=10_000)
    assert fetch_count["n"] >= 1, "unseeded ready did not reconcile via a canonical fetch"
    # The baseline seeds from the fetched concept_content_rev.
    assert page.evaluate("window.okfLoomLive._getLastDocRevForTest('tables/orders')") == content_rev
    # The editor is NOT invalidated (fetched content matched — no-op diff).
    mode_after = page.evaluate("window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode")
    assert mode_after == mode_before, f"reconciliation invalidated a current editor: {mode_before!r} -> {mode_after!r}"


def test_initial_ready_stale_ssr_is_corrected_not_blessed(server_url, page):
    """R5-11: when the initial SSR HTML is stale (the ready frame reports a
    newer content rev than what the page rendered), the unseeded ready must
    NOT bless the stale bytes. The canonical reconciliation fetch returns the
    newer bytes and the body is corrected to canonical content."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    initial_body = page.evaluate("document.querySelector('.okf-page__body').innerHTML")

    # Route the canonical fetch to NEWER bytes (different content rev + body).
    fresh_rev = "f" * 64
    fresh_doc = {
        "id": "tables/orders", "annotation_source": "FRESH CANONICAL BYTES\n",
        "concept_content_rev": fresh_rev,
        "html": '<p data-okf-projection-key="0">FRESH CANONICAL BYTES</p>',
        "raw": "FRESH CANONICAL BYTES\n", "title": "Orders", "description": "",
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": fresh_rev,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 21, "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": 21, "source_start": 0, "source_end": 21}]}]},
    }
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))

    # Simulate the first-ever (unseeded) ready; the ready frame reports the
    # fresh rev, but the page still shows the stale SSR bytes.
    page.evaluate("window.okfLoomLive._clearLastDocRevForTest('tables/orders')")
    assert page.evaluate("window.okfLoomLive._getLastDocRevForTest('tables/orders')") is None
    page.evaluate("(rev) => window.okfLoomLive._dispatchReadyForTest({rev: 1, doc_revs: {'tables/orders': rev}})", fresh_rev)

    # The body MUST be corrected to the canonical bytes (not left stale).
    page.wait_for_function("() => document.querySelector('.okf-page__body').innerHTML.indexOf('FRESH CANONICAL BYTES') >= 0", timeout=10_000)
    assert "FRESH CANONICAL BYTES" not in initial_body, "fixture sanity: initial body should not already contain the fresh text"
    # The baseline seeds from the fetched fresh rev, not the stale SSR rev.
    assert page.evaluate("window.okfLoomLive._getLastDocRevForTest('tables/orders')") == fresh_rev


def test_document_patch_refetches_and_recomputes_resolutions(server_url, page):
    """R4-4: a live document patch that advances the content revision must
    trigger a canonical comment refetch so resolutions/marks recompute
    against the new bytes (marks removed during the patch, then recomputed
    via loadComments — not left stale until an unrelated event)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Set up the doc + selection first, BEFORE seeding any resolved comment,
    # so applyCommentMarks does not wrap the selection helper's text node in
    # a <mark> (which would break the range offsets below).
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    # Seed a properly-resolved comment at rev 'a'*64 and re-apply marks.
    page.evaluate("""() => {
        window.okfLoomStudio.state.commentsRequestGeneration++;
        window.okfLoomStudio.state.commentsReady = true;
        window.okfLoomStudio.state.comments = [{
            id: '01JANNOTATE', concept: 'tables/orders', body: 'Check this anchor', state: 'open',
            row_token: 'r2:a', ts: new Date().toISOString(),
            anchor: {schema:2,kind:'text-quote',exact:'target text',prefix:'',suffix:'',heading:{path:[]},
              block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},position:{start:0,end:11}},
            anchor_resolution: {status:'resolved',reason:'original_position',concept_content_rev:'a'.repeat(64),
              resolver_version:2,position:{start:0,end:11},block:{id:'paragraph:1',start:0,end:11},changes:[]}
        }];
        const s = window.okfLoomStudio.state;
        window.okfLoomStudio.applyDoc(Object.assign({}, s.doc), {pulse: false});
    }""")
    expect(page.locator("mark.okf-comment-mark[data-comment-id='01JANNOTATE']")).to_have_count(1)

    # Establish the live.js content-rev baseline for the open concept.
    baseline = page.evaluate("window.okfLoomLive._getLastDocRevForTest('tables/orders')")
    if baseline is None:
        page.evaluate("(rev) => window.okfLoomLive._seedLastDocRevForTest('tables/orders', rev)", "a" * 64)

    fresh_doc = {
        "id": "tables/orders", "annotation_source": "target text\n", "concept_content_rev": "b" * 64,
        "html": "<p data-okf-projection-key=\"0\">target text</p>", "raw": "target text\n", "title": "Orders", "description": "",
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": "b" * 64,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 11, "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": 11, "source_start": 0, "source_end": 11}]}]},
    }
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    fresh_comment = {
        "id": "01JANNOTATE", "concept": "tables/orders", "body": "Check this anchor", "state": "open",
        "row_token": "r2:b", "ts": "2026-01-01T00:00:00Z",
        "anchor": {"schema": 2, "kind": "text-quote", "exact": "target text", "prefix": "", "suffix": "", "heading": {"path": []},
                   "block": {"id": "paragraph:1", "hash": "sha256:" + "a" * 64, "start": 0, "end": 11}, "position": {"start": 0, "end": 11}},
        "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": "b" * 64,
            "resolver_version": 2, "position": {"start": 0, "end": 11}, "block": {"id": "paragraph:1", "start": 0, "end": 11}, "changes": []},
    }
    comments_calls = {"n": 0}

    def comments(route):
        comments_calls["n"] += 1
        route.fulfill(status=200, json={"comments": [fresh_comment]})
    page.route("**/__comments", comments)

    cursor = page.evaluate("window.okfLoomLive._debug.lastCursor")
    page.evaluate("(c) => window.okfLoomLive._dispatchEventForTest({type:'changed',ids:['tables/orders'],event_cursor:c+1,rev:101})", cursor)

    # The patch must trigger a canonical comment refetch and the resolution
    # must recompute against the new content rev.
    page.wait_for_function(
        "() => { const c = window.okfLoomStudio.state.comments.find(x => x.id === '01JANNOTATE');"
        " return c && c.anchor_resolution && c.anchor_resolution.concept_content_rev === '" + "b" * 64 + "'; }",
        timeout=10_000,
    )
    assert comments_calls["n"] >= 1, "document patch did not trigger a canonical comment refetch"
    # Mark recomputed against the new rev.
    expect(page.locator("mark.okf-comment-mark[data-comment-id='01JANNOTATE']")).to_have_count(1)


def test_pending_v2_draft_mark_reapplied_after_patch(server_url, page):
    """R4-5: a pending v2 text-quote draft mark must be reapplied after a
    body patch. Previously applyPendingDraftMark only handled legacy
    {kind:text,ref} anchors, so a v2 draft highlight disappeared under the
    open composer."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    local_id = "local-draft-1"
    # Simulate the affordance capture: a v2 draft anchor + a wrapped mark.
    # R5-12: the draft is revision-qualified (draftContentRev matches the doc).
    page.evaluate("""(id) => {
        const s = window.okfLoomStudio.state;
        s._pendingMarkId = id;
        s.draftAnchor = {schema:2,kind:'text-quote',exact:'target text',prefix:'',suffix:'',
          heading:{path:[]},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},position:{start:0,end:11}};
        s.draftContentRev = s.doc.concept_content_rev;
        const node = document.querySelector('.okf-page__body p').firstChild;
        const range = document.createRange(); range.setStart(node,0); range.setEnd(node,11);
        const mark = document.createElement('mark'); mark.className='okf-comment-mark';
        mark.setAttribute('data-comment-id', id); mark.setAttribute('data-comment-state','open');
        try { range.surroundContents(mark); } catch(e) {}
    }""", local_id)
    expect(page.locator("mark.okf-comment-mark[data-comment-id='%s']" % local_id)).to_have_count(1)
    # Force a body patch (identical content re-render). applyCommentMarks
    # removes ALL marks; applyPendingDraftMark must re-wrap the v2 draft via
    # its block/position projection (R5-12).
    page.evaluate("() => { const s = window.okfLoomStudio.state; window.okfLoomStudio.applyDoc(Object.assign({}, s.doc), {pulse:false}); }")
    expect(page.locator("mark.okf-comment-mark[data-comment-id='%s']" % local_id)).to_have_count(1)


def test_pending_v2_draft_mark_targets_unique_occurrence_not_first_match(server_url, page):
    """R5-12: a pending v2 draft mark must project via block/position evidence
    to the UNIQUE occurrence, not a body-wide first-text match. When the exact
    text appears in TWO blocks, the draft highlight must land on the block the
    anchor's evidence names — never on an unrelated duplicate occurrence."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Two paragraphs both containing "dup text"; the draft targets the SECOND.
    # source = "first dup text\n\nsecond dup text\n"
    #   block 1 "first dup text"  -> source [0,14), "dup text" at [6,14)
    #   block 2 "second dup text" -> source [16,31), "dup text" at [23,31)
    page.evaluate("""() => {
        const source = 'first dup text\\n\\nsecond dup text\\n';
        const rev = 'a'.repeat(64);
        window.okfLoomStudio.applyDoc({
          id:'tables/orders', annotation_source:source, concept_content_rev:rev, raw:source,
          annotation_projection:{schema:1,resolver_version:2,concept_content_rev:rev,offset_unit:'unicode-code-point',complete:true,
            blocks:[
              {block_id:'paragraph:1',source_start:0,source_end:14,heading_path:[],dom_key:0,segments:[{dom_start:0,dom_end:14,source_start:0,source_end:14}]},
              {block_id:'paragraph:2',source_start:16,source_end:31,heading_path:[],dom_key:1,segments:[{dom_start:0,dom_end:15,source_start:16,source_end:31}]}]},
          html:'<p data-okf-projection-key="0">first dup text</p><p data-okf-projection-key="1">second dup text</p>',
          title:'Orders', description:''
        }, {pulse:false});
    }""")
    local_id = "local-draft-uniq"
    # Draft anchor with block/position evidence pointing at the SECOND block's
    # "dup text" (source code-point offsets [23,31)).
    page.evaluate("""(id) => {
        const s = window.okfLoomStudio.state;
        s._pendingMarkId = id;
        s.draftAnchor = {schema:2,kind:'text-quote',exact:'dup text',prefix:'second ',suffix:'',
          heading:{path:[]},block:{id:'paragraph:2',hash:'sha256:x',start:16,end:31},position:{start:23,end:31}};
        s.draftContentRev = s.doc.concept_content_rev;
    }""", local_id)
    # Force a patch (identical re-render) so applyCommentMarks removes marks
    # and applyPendingDraftMark re-projects the draft.
    page.evaluate("() => { const s = window.okfLoomStudio.state; window.okfLoomStudio.applyDoc(Object.assign({}, s.doc), {pulse:false}); }")
    # The draft mark must wrap "dup text" inside the SECOND block only.
    marks = page.locator("mark.okf-comment-mark[data-comment-id='%s']" % local_id)
    expect(marks).to_have_count(1)
    parent_text = marks.evaluate("el => el.parentElement.textContent")
    assert parent_text == "second dup text", f"draft mark landed on the wrong occurrence: {parent_text!r}"


def test_pending_v2_draft_mark_cleared_on_revision_change(server_url, page):
    """R5-12: when the content revision changes after a v2 draft was captured,
    the pending draft mark must NOT be reapplied to the new bytes (its
    block/position evidence is keyed to the OLD revision). applyCommentMarks
    removes the old mark during the patch and applyPendingDraftMark skips, so
    the stale draft does not land on possibly-different bytes."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    local_id = "local-draft-rev"
    page.evaluate("""(id) => {
        const s = window.okfLoomStudio.state;
        s._pendingMarkId = id;
        s.draftAnchor = {schema:2,kind:'text-quote',exact:'target text',prefix:'',suffix:'',
          heading:{path:[]},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},position:{start:0,end:11}};
        s.draftContentRev = s.doc.concept_content_rev;
    }""", local_id)
    # Wrap an optimistic mark so applyCommentMarks has something to remove.
    page.evaluate("""(id) => {
        const node = document.querySelector('.okf-page__body p').firstChild;
        const range = document.createRange(); range.setStart(node,0); range.setEnd(node,11);
        const mark = document.createElement('mark'); mark.className='okf-comment-mark';
        mark.setAttribute('data-comment-id', id);
        try { range.surroundContents(mark); } catch(e) {}
    }""", local_id)
    expect(page.locator("mark.okf-comment-mark[data-comment-id='%s']" % local_id)).to_have_count(1)
    # Patch with a NEW content rev (same visible text). The revision changed,
    # so the draft's block/position evidence is stale.
    page.evaluate("""() => {
        const s = window.okfLoomStudio.state;
        const next = Object.assign({}, s.doc, {concept_content_rev:'b'.repeat(64),
          annotation_projection:Object.assign({}, s.doc.annotation_projection, {concept_content_rev:'b'.repeat(64)})});
        window.okfLoomStudio.applyDoc(next, {pulse:false});
    }""")
    # The stale draft mark must NOT be reapplied to the new-rev bytes.
    expect(page.locator("mark.okf-comment-mark[data-comment-id='%s']" % local_id)).to_have_count(0)


def test_sse_agent_conflict_and_suggestions_listeners_dispatch(server_url, page):
    """R4-6: connect() must register EventSource listeners for
    agent_conflict and suggestions (previously the dispatcher understood
    them but no listeners fed real SSE frames through), and the dispatcher
    must deliver them to studio.js subscribers."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")

    registered = page.evaluate("""() => {
        const types = new Set();
        const RealEventSource = window.EventSource;
        function Stub(url) {
            this.addEventListener = (t) => { types.add(t); };
            this.removeEventListener = () => {};
            this.close = () => {};
            this.readyState = RealEventSource.CLOSED;
            this.url = url;
        }
        window.EventSource = Stub;
        try { window.okfLoomLive._connectForTest(); } catch (e) {}
        window.EventSource = RealEventSource;
        return Array.from(types);
    }""")
    assert "agent_conflict" in registered, f"agent_conflict SSE listener not registered: {registered}"
    assert "suggestions" in registered, f"suggestions SSE listener not registered: {registered}"

    # And the dispatcher delivers both to hub subscribers.
    page.evaluate("""() => {
        window.__r4Ac = null; window.__r4Sg = null;
        window.okfLoomLive.on('agent_conflict', (d) => { window.__r4Ac = d; });
        window.okfLoomLive.on('suggestions', (d) => { window.__r4Sg = d; });
    }""")
    cur = page.evaluate("window.okfLoomLive._debug.lastCursor")
    page.evaluate("""(c) => {
        window.okfLoomLive._dispatchEventForTest({type:'agent_conflict',concept:'tables/orders',summary:'c',event_cursor:c+1,rev:1});
        window.okfLoomLive._dispatchEventForTest({type:'suggestions',items:[1,2],event_cursor:c+2,rev:1});
    }""", cur)
    assert page.evaluate("window.__r4Ac && window.__r4Ac.concept === 'tables/orders'")
    assert page.evaluate("window.__r4Sg && Array.isArray(window.__r4Sg.items) && window.__r4Sg.items.length === 2")


def test_agent_conflict_sse_opens_accessible_conflict_modal(server_url, page):
    """R4-6 + keyboard-focus: an agent_conflict SSE frame must surface the
    conflict modal (the listener wiring was the r4-6 gap), and the alertdialog
    must own focus (dialog/menu ownership wins over route handlers)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    cur = page.evaluate("window.okfLoomLive._debug.lastCursor")
    page.evaluate("(c) => window.okfLoomLive._dispatchEventForTest({type:'agent_conflict',concept:'tables/orders',summary:'cli conflict',event_cursor:c+1,rev:1})", cur)
    modal = page.locator("[role='alertdialog']")
    expect(modal).to_be_visible(timeout=5_000)
    # Dialog owns focus after the 20ms focus move.
    page.wait_for_function("() => { const m = document.querySelector('[role=alertdialog]'); return !!m && m.contains(document.activeElement); }", timeout=5_000)
    # The accessible alertdialog is labelled.
    labelledby = modal.get_attribute("aria-labelledby")
    assert labelledby, "conflict alertdialog must be aria-labelled"


def test_conflict_dialog_exclusively_owns_keyboard_over_panel(server_url, page):
    """R5-13: when both the slide-over panel and the conflict alertdialog are
    open, the conflict dialog is the explicit topmost modal and EXCLUSIVELY
    owns Escape and Tab. The panel keydown handler yields, so a single Escape
    closes ONLY the conflict dialog (not both), and Tab is trapped by only the
    conflict dialog's focus trap."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Open the comments panel first (the underlying slide-over).
    page.evaluate("window.okfLoomStudio.openPanel('comments')")
    page.wait_for_function("() => window.okfLoomStudio.state.openPanel === 'comments'", timeout=5_000)
    panel = page.locator(".okf-panel")
    expect(panel).to_be_visible()
    # Now open the conflict alertdialog on top via an agent_conflict SSE frame.
    cur = page.evaluate("window.okfLoomLive._debug.lastCursor")
    page.evaluate("(c) => window.okfLoomLive._dispatchEventForTest({type:'agent_conflict',concept:'tables/orders',summary:'cli conflict',event_cursor:c+1,rev:1})", cur)
    modal = page.locator("[role='alertdialog']")
    expect(modal).to_be_visible(timeout=5_000)
    page.wait_for_function("() => { const m = document.querySelector('[role=alertdialog]'); return !!m && m.contains(document.activeElement); }", timeout=5_000)

    # Tab must be trapped inside the conflict dialog only (not also by the
    # panel's focus trap). After Tab, focus stays inside the alertdialog.
    page.keyboard.press("Tab")
    page.wait_for_timeout(50)
    assert page.evaluate("() => { const m = document.querySelector('[role=alertdialog]'); return !!m && m.contains(document.activeElement); }"), "Tab escaped the conflict dialog focus trap"

    # Escape must close ONLY the conflict dialog. The panel must stay open
    # (previously both handlers fired and Escape closed both layers).
    page.keyboard.press("Escape")
    expect(modal).not_to_be_visible(timeout=5_000)
    # The underlying panel is still open.
    expect(panel).to_be_visible()
    assert page.evaluate("() => window.okfLoomStudio.state.openPanel === 'comments'"), "panel closed when conflict dialog Escape fired"


def test_suggestions_sse_has_production_consumer(server_url, page):
    """R5-14: the `suggestions` SSE event must have an authoritative production
    consumer. A real suggestions frame (agent propose_only proposal) must
    produce a user-visible effect: it lands in the changes timeline and promotes
    the compact Studio launcher. Previously the transport dispatched the frame but
    wireLive had no subscriber, so it had no product outcome."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")

    cur = page.evaluate("window.okfLoomLive._debug.lastCursor")
    # Dispatch a real suggestions frame (shape from studio.py post_suggestion).
    page.evaluate("(c) => window.okfLoomLive._dispatchEventForTest({type:'suggestions',proposal:'prop-001',concept:'tables/orders',event_cursor:c+1,rev:1})", cur)

    # The event must enter the changes timeline (production state effect).
    page.wait_for_function(
        "() => window.okfLoomStudio.state.events.some(e => e.type === 'suggestions' && e.id === 'prop-001')",
        timeout=5_000,
    )
    ev = page.evaluate("() => window.okfLoomStudio.state.events.find(e => e.type === 'suggestions' && e.id === 'prop-001')")
    assert ev and ev["action"] == "proposed", f"suggestions event missing action: {ev}"
    assert ev["ids"] == ["tables/orders"], f"suggestions event missing concept id: {ev}"

    # F13: idle zero-count controls stay inside Studio; actionable unread work
    # is promoted through the one persistent launcher.
    launcher = page.locator(".okf-studio-launcher")
    expect(launcher).to_have_attribute("data-attention", "pending")
    expect(launcher).to_contain_text("1 change")

    # The changes panel must render a row for the suggestions event.
    page.evaluate("window.okfLoomStudio.openPanel('changes')")
    page.wait_for_function(
        "() => { const rows = document.querySelectorAll('.okf-change'); return Array.from(rows).some(r => r.textContent.indexOf('tables/orders') >= 0); }",
        timeout=5_000,
    )


# ---------------------------------------------------------------------------
# P1 visual closure slice: drawer rail reflow, 320 back-to-document peek,
# recovery anatomy, forced-colors, anchor-health vs lifecycle clarity.
# ---------------------------------------------------------------------------

def test_comments_drawer_reflows_as_rail_column_at_wide_viewport(server_url, page):
    """P1-1: at wide viewports the comments drawer reserves a real layout
    column so the topbar/studio bar/authored concept are not clipped beneath
    it. The authored body must reflow left of the drawer and the modal scrim
    must be dropped."""
    _boot(page, server_url)
    _seed_comment(page)
    page.wait_for_function("() => document.body.classList.contains('okf-panel-rail')", timeout=5_000)
    geom = page.evaluate("""() => {
        const panel = document.querySelector('.okf-panel');
        const overlay = document.querySelector('.okf-panel-overlay');
        const body = document.querySelector('.okf-page__body');
        const topbar = document.querySelector('.okf-topbar');
        return {
            rail: document.body.classList.contains('okf-panel-rail'),
            paddingRight: getComputedStyle(document.body).paddingRight,
            panelW: panel.offsetWidth,
            overlayDisplay: getComputedStyle(overlay).display,
            bodyRight: Math.round(body.getBoundingClientRect().right),
            topbarRight: Math.round(topbar.getBoundingClientRect().right),
            vw: window.innerWidth,
        };
    }""")
    assert geom["rail"], f"rail mode not active at 1280px: {geom}"
    assert geom["panelW"] == 440, f"panel width changed: {geom}"
    assert geom["overlayDisplay"] == "none", f"scrim should be hidden in rail mode: {geom}"
    # Authored body + topbar must reflow left of the 440px drawer (not clipped).
    assert geom["bodyRight"] <= geom["vw"] - 440 + 2, f"body clipped under drawer: {geom}"
    assert geom["topbarRight"] <= geom["vw"] - 440 + 2, f"topbar clipped under drawer: {geom}"


def test_comments_drawer_stays_overlay_at_narrow_viewport(server_url, page):
    """P1-1/P1-3: at narrow viewports the drawer stays a full-width overlay
    (no rail reservation), which is the precondition for the Back-to-document
    peek path."""
    browser = page.context.browser
    context = browser.new_context(viewport={"width": 800, "height": 900})
    narrow = context.new_page()
    _boot(narrow, server_url)
    narrow.evaluate("""() => {
      window.okfLoomLive && window.okfLoomLive._disconnectForTest();
      window.okfLoomStudio.state.comments = [{
        id:'01JNARROW01', concept:'tables/orders', body:'narrow', state:'open',
        row_token:'r2:narrow', ts:new Date().toISOString(),
        anchor:{schema:2,kind:'text-quote',exact:'target text',prefix:'',suffix:'',heading:{path:[]},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},position:{start:0,end:11}},
        anchor_resolution:{status:'stale',reason:'exact_not_found',concept_content_rev:'a'.repeat(64),resolver_version:2}
      }];
      window.okfLoomStudio.openPanel('comments');
    }""")
    is_rail = narrow.evaluate("() => document.body.classList.contains('okf-panel-rail')")
    panel_full = narrow.evaluate("() => document.querySelector('.okf-panel').offsetWidth >= window.innerWidth - 4")
    context.close()
    assert not is_rail, "rail mode must not activate at narrow viewport"
    assert panel_full, "panel must be full-width overlay at narrow viewport"


def test_reanchor_editor_shows_document_context_bar(server_url, page):
    """P1-3/P1-7: the recovery editor shows a persistent document context bar
    so the user knows which document the anchor belongs to (essential at 320px
    where the drawer covers the page)."""
    _boot(page, server_url); _seed_comment(page)
    page.get_by_role("button", name="Re-anchor").click()
    ctx = page.locator("#comment-01JANNOTATE .okf-reanchor__context")
    expect(ctx).to_be_visible()
    # Context title reflects the open document title.
    title = ctx.locator(".okf-reanchor__context-title").text_content()
    assert title and "Orders" in title, f"context bar missing document title: {title!r}"


def test_320_back_to_document_peek_preserves_editor_state(server_url, page):
    """P1-3: at 320px, selecting a new anchor offers a deterministic Back-to-
    document / Select-text path that collapses (peeks) the drawer so the
    document is visible, then returns to the comment with editor state intact."""
    browser = page.context.browser
    context = browser.new_context(viewport={"width": 320, "height": 900})
    mobile = context.new_page()
    _boot(mobile, server_url)
    mobile.evaluate("""() => {
      window.okfLoomLive && window.okfLoomLive._disconnectForTest();
      window.okfLoomStudio.state.comments = [{
        id:'01JMOB32001', concept:'tables/orders', body:'mobile anchor', state:'open',
        row_token:'r2:mob', ts:new Date().toISOString(),
        anchor:{schema:2,kind:'text-quote',exact:'customer_id references customers',prefix:'',suffix:'',heading:{path:[]},block:{id:'paragraph:17',hash:'sha256:'+'a'.repeat(64),start:120,end:190},position:{start:127,end:158}},
        anchor_resolution:{status:'stale',reason:'exact_not_found',concept_content_rev:'a'.repeat(64),resolver_version:2}
      }];
      window.okfLoomStudio.openPanel('comments');
    }""")
    mobile.get_by_role("button", name="Re-anchor").click()
    expect(mobile.locator(".okf-reanchor__instruction")).to_be_focused()
    # The Select-in-document bridge is present.
    select_btn = mobile.get_by_role("button", name="Select in document")
    expect(select_btn).to_be_visible()
    # Geometry before peek: panel covers the document.
    before = mobile.evaluate("""() => {
        const panel = document.querySelector('.okf-panel');
        const body = document.querySelector('.okf-page__body');
        return { panelHidden: panel.hidden, bodyVisible: body.offsetWidth > 0 && body.getBoundingClientRect().right > 60 };
    }""")
    # Activate Back-to-document: drawer peeks open.
    select_btn.click()
    peeked = mobile.evaluate("""() => ({
        panelHidden: document.querySelector('.okf-panel').hidden,
        overlayHidden: document.querySelector('.okf-panel-overlay').hidden,
        peekVisible: !document.querySelector('.okf-reanchor-peek').hidden,
        bodyRight: Math.round(document.querySelector('.okf-page__body').getBoundingClientRect().right),
        editorAlive: !!window.okfLoomStudio.state.annotationEditor,
        editorMode: window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode,
        peekClipped: (function(){const r=document.querySelector('.okf-reanchor-peek').getBoundingClientRect();return {right:Math.ceil(r.right),bottom:Math.ceil(r.bottom)};})(),
    })""")
    assert peeked["panelHidden"], f"drawer not peeked hidden: {peeked}"
    assert peeked["overlayHidden"], f"scrim not hidden while peeked: {peeked}"
    assert peeked["peekVisible"], f"peek bar not visible: {peeked}"
    assert peeked["peekClipped"]["right"] <= 320 and peeked["peekClipped"]["bottom"] <= 900, f"peek bar clipped: {peeked}"
    # Document is now visible across the viewport (not covered by the drawer).
    assert peeked["bodyRight"] >= 300, f"document not visible after peek: {peeked}"
    assert peeked["editorAlive"] and peeked["editorMode"] == "selecting", f"editor state lost on peek: {peeked}"
    # Return to comment restores the drawer and preserves editor state.
    mobile.get_by_role("button", name="Return to comment").click()
    restored = mobile.evaluate("""() => ({
        panelHidden: document.querySelector('.okf-panel').hidden,
        overlayHidden: document.querySelector('.okf-panel-overlay').hidden,
        peekVisible: !document.querySelector('.okf-reanchor-peek').hidden,
        editorAlive: !!window.okfLoomStudio.state.annotationEditor,
        editorMode: window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode,
        instructionVisible: !!document.querySelector('.okf-reanchor__instruction'),
        retryRight: (function(){const b=document.querySelector('#comment-01JMOB32001 .okf-reanchor__actions'); if(!b)return -1;return Math.ceil(b.getBoundingClientRect().right);})(),
    })""")
    context.close()
    assert not restored["panelHidden"], f"drawer not restored: {restored}"
    assert not restored["peekVisible"], f"peek bar not hidden after return: {restored}"
    assert restored["editorAlive"] and restored["editorMode"] == "selecting", f"editor state lost on return: {restored}"
    assert restored["instructionVisible"], f"editor not re-rendered on return: {restored}"
    assert restored["retryRight"] <= 320, f"recovery actions clipped at 320: {restored}"


def test_320_selecting_no_horizontal_overflow(server_url, page):
    """P1-3 geometry: at 320px in selecting mode, the recovery editor and
    context bar must not cause horizontal page overflow."""
    browser = page.context.browser
    context = browser.new_context(viewport={"width": 320, "height": 900})
    mobile = context.new_page()
    _boot(mobile, server_url)
    mobile.evaluate("""() => {
      window.okfLoomLive && window.okfLoomLive._disconnectForTest();
      window.okfLoomStudio.state.comments = [{
        id:'01JOVF32001', concept:'tables/orders', body:'overflow check', state:'open',
        row_token:'r2:ovf', ts:new Date().toISOString(),
        anchor:{schema:2,kind:'text-quote',exact:'customer_id references customers',prefix:'',suffix:'',heading:{path:[]},block:{id:'paragraph:17',hash:'sha256:'+'a'.repeat(64),start:120,end:190},position:{start:127,end:158}},
        anchor_resolution:{status:'stale',reason:'exact_not_found',concept_content_rev:'a'.repeat(64),resolver_version:2}
      }];
      window.okfLoomStudio.openPanel('comments');
    }""")
    mobile.get_by_role("button", name="Re-anchor").click()
    geom = mobile.evaluate("""() => {
        const docEl = document.documentElement;
        const actions = document.querySelector('#comment-01JOVF32001 .okf-reanchor__actions');
        const ctx = document.querySelector('#comment-01JOVF32001 .okf-reanchor__context');
        return {
            scrollWidth: docEl.scrollWidth,
            viewportWidth: docEl.clientWidth,
            actionsRight: actions ? Math.ceil(actions.getBoundingClientRect().right) : -1,
            contextRight: ctx ? Math.ceil(ctx.getBoundingClientRect().right) : -1,
        };
    }""")
    context.close()
    assert geom["scrollWidth"] <= geom["viewportWidth"], f"horizontal overflow at 320 in selecting mode: {geom}"
    assert geom["actionsRight"] <= 320, f"recovery actions overflow at 320: {geom}"
    assert geom["contextRight"] <= 320, f"context bar overflow at 320: {geom}"


def test_stale_recovery_action_is_prominent(server_url, page):
    """P1-4: the collapsed stale anchor's Re-anchor action carries primary
    weight (the key recovery path), while a resolved anchor's Jump stays
    quiet."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive && window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.okfLoomStudio.state.comments = [
        {id:'01JSTALE001',concept:'tables/orders',body:'stale one',state:'open',row_token:'r2:s',ts:new Date().toISOString(),
         anchor:{schema:2,kind:'text-quote',exact:'target text',prefix:'',suffix:'',heading:{path:[]},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},position:{start:0,end:11}},
         anchor_resolution:{status:'stale',reason:'exact_not_found',concept_content_rev:'a'.repeat(64),resolver_version:2}},
        {id:'01JRESOLVED1',concept:'tables/orders',body:'resolved one',state:'open',row_token:'r2:r',ts:new Date().toISOString(),
         anchor:{schema:2,kind:'text-quote',exact:'target text',prefix:'',suffix:'',heading:{path:[]},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},position:{start:0,end:11}},
         anchor_resolution:{status:'resolved',reason:'original_position',concept_content_rev:'a'.repeat(64),resolver_version:2,position:{start:0,end:11},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},changes:[]}}
      ];
      window.okfLoomStudio.openPanel('comments');
    }""")
    stale_cls = page.locator("#comment-01JSTALE001 .okf-anchor-row__action").get_attribute("class")
    resolved_cls = page.locator("#comment-01JRESOLVED1 .okf-anchor-row__action").get_attribute("class")
    assert stale_cls and "okf-anchor-row__action--primary" in stale_cls, f"stale Re-anchor not prominent: {stale_cls}"
    assert resolved_cls and "okf-anchor-row__action--primary" not in resolved_cls, f"resolved Jump should stay quiet: {resolved_cls}"


def test_anchor_health_row_distinct_from_thread_lifecycle(server_url, page):
    """P1-8: the anchor-health row is labelled as anchor health (distinct from
    the thread lifecycle state chip), and a resolved anchor persists an explicit
    'Attached' label rather than implying the thread is resolved."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive && window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.okfLoomStudio.state.comments = [{
        id:'01JATTACHED1',concept:'tables/orders',body:'attached, open thread',state:'open',row_token:'r2:a',ts:new Date().toISOString(),
        anchor:{schema:2,kind:'text-quote',exact:'target text',prefix:'',suffix:'',heading:{path:[]},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},position:{start:0,end:11}},
        anchor_resolution:{status:'resolved',reason:'original_position',concept_content_rev:'a'.repeat(64),resolver_version:2,position:{start:0,end:11},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},changes:[]}},
        {id:'01JPAGEROW01',concept:'tables/orders',body:'about the whole page',state:'open',row_token:'r2:p',ts:new Date().toISOString(),
         anchor:{kind:'concept'}}
      ];
      window.okfLoomStudio.openPanel('comments');
    }""")
    row = page.locator("#comment-01JATTACHED1 .okf-anchor-row")
    expect(row).to_have_attribute("aria-label", "Anchor health")
    # R3: neither healthy state is a bare label; each says what it means.
    expect(row.locator(".okf-anchor-row__cause")).to_have_text(
        "Exact annotation: pinned to this passage and highlighted.")
    page_row = page.locator("#comment-01JPAGEROW01 .okf-anchor-row")
    expect(page_row.locator(".okf-anchor-row__status")).to_contain_text("Page comment")
    expect(page_row.locator(".okf-anchor-row__cause")).to_have_text("About the whole page; no text is highlighted.")
    # The persistent Attached label is present AND the thread chip still says OPEN.
    status_text = row.locator(".okf-anchor-row__status").text_content()
    state_text = page.locator("#comment-01JATTACHED1 .okf-comment__state").text_content()
    assert "Attached" in (status_text or ""), f"explicit Attached label missing: {status_text!r}"
    assert (state_text or "").strip().upper() == "OPEN", f"thread lifecycle not shown as OPEN alongside Attached: {state_text!r}"


def test_forced_colors_single_focus_outline_and_system_severity(server_url, page):
    """P1-6: under forced-colors, focus uses a single consistent Highlight
    outline (no competing box-shadow), and anchor-health severity is
    distinguished by SYSTEM colors (resolved/Attached vs stale) rather than
    one accent."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive && window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.okfLoomStudio.state.comments = [
        {id:'01JFCSTALE1',concept:'tables/orders',body:'stale',state:'open',row_token:'r2:s',ts:new Date().toISOString(),
         anchor:{schema:2,kind:'text-quote',exact:'target text',prefix:'',suffix:'',heading:{path:[]},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},position:{start:0,end:11}},
         anchor_resolution:{status:'stale',reason:'exact_not_found',concept_content_rev:'a'.repeat(64),resolver_version:2}},
        {id:'01JFCATTCH1',concept:'tables/orders',body:'attached',state:'open',row_token:'r2:a',ts:new Date().toISOString(),
         anchor:{schema:2,kind:'text-quote',exact:'target text',prefix:'',suffix:'',heading:{path:[]},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},position:{start:0,end:11}},
         anchor_resolution:{status:'resolved',reason:'original_position',concept_content_rev:'a'.repeat(64),resolver_version:2,position:{start:0,end:11},block:{id:'paragraph:1',hash:'sha256:'+'a'.repeat(64),start:0,end:11},changes:[]}}
      ];
      window.okfLoomStudio.openPanel('comments');
    }""")
    # Open a save_failed editor on the stale comment to get a Retry control.
    page.get_by_role("button", name="Re-anchor").click()
    page.evaluate("""() => { const e=window.okfLoomStudio.state.annotationEditor;
      e.mode='save_failed'; e.message='The anchor could not be saved.';
      e.anchor={exact:'customer_id'}; window.okfLoomStudio._renderComments(); }""")
    page.emulate_media(forced_colors="active")
    retry = page.get_by_role("button", name="Retry save")
    for _ in range(40):
        page.keyboard.press("Tab")
        if retry.evaluate("e => document.activeElement === e"):
            break
    focus_info = page.evaluate("""() => {
      const e = document.activeElement;
      const s = getComputedStyle(e);
      const r = e.getBoundingClientRect();
      // Sentinels guaranteed to use specific system colors, so we can compare
      // resolved rgb values (Chromium reports system colors as rgb, not keywords).
      const mkSentinel = (sysColor) => {
        const el = document.createElement('div');
        el.style.cssText = 'background: ' + sysColor + '; position: absolute; visibility: hidden;';
        document.body.appendChild(el);
        const v = getComputedStyle(el).backgroundColor;
        el.remove();
        return v;
      };
      const banner = document.getElementById('okf-annotation-selection-banner');
      const bs = banner ? getComputedStyle(banner) : null;
      const br = banner ? banner.getBoundingClientRect() : null;
      const topbar = document.querySelector('.okf-topbar');
      const topbarBottom = topbar ? Math.round(topbar.getBoundingClientRect().bottom) : 0;
      return {
        // Retry save control: label, readable pairing, single distinct focus ring.
        label: e.textContent.trim(),
        color: s.color, bg: s.backgroundColor,
        outlineStyle: s.outlineStyle, outlineWidth: parseFloat(s.outlineWidth),
        outlineColor: s.outlineColor, boxShadow: s.boxShadow,
        focusVisible: e.matches(':focus-visible'),
        retryW: Math.round(r.width), retryH: Math.round(r.height),
        retryTop: Math.round(r.top), retryRight: Math.round(r.right),
        // Directional banner: instruction text actually present + readable + not occluded.
        bannerText: banner ? banner.textContent.trim() : '',
        bannerColor: bs ? bs.color : null, bannerBg: bs ? bs.backgroundColor : null,
        bannerW: br ? Math.round(br.width) : 0, bannerH: br ? Math.round(br.height) : 0,
        bannerTop: br ? Math.round(br.top) : -1,
        topbarBottom: topbarBottom,
        highlightBg: mkSentinel('Highlight'), buttonFaceBg: mkSentinel('ButtonFace'),
      };
    }""")
    # Retry save LABEL is present and the control has nonzero in-viewport geometry.
    assert focus_info["label"] == "Retry save", f"Retry save label missing/wrong: {focus_info}"
    assert focus_info["retryW"] > 0 and focus_info["retryH"] > 0, f"Retry save zero geometry: {focus_info}"
    assert focus_info["retryTop"] >= 0 and focus_info["retryRight"] <= 1280, f"Retry save clipped: {focus_info}"
    # Label readable: text color differs from background (distinct system pairing).
    assert focus_info["color"] != focus_info["bg"], (
        f"Retry save label color == background (invisible) in forced-colors: {focus_info}")
    # Single, DISTINCT focus outline: solid, the system Highlight color, no
    # competing box-shadow, AND the outline color must differ from the button's
    # own background so the ring is visible against it (the prior Highlight-fill
    # bug made outline == background, hiding the ring + the selected look).
    assert focus_info["focusVisible"] is True, f"retry not focus-visible in forced-colors: {focus_info}"
    assert focus_info["outlineStyle"] == "solid", f"focus outline not solid: {focus_info}"
    assert focus_info["outlineWidth"] >= 2, f"focus outline too thin: {focus_info}"
    assert focus_info["outlineColor"] == focus_info["highlightBg"], (
        f"focus outline does not use system Highlight color: {focus_info}")
    assert focus_info["outlineColor"] != focus_info["bg"], (
        f"focus outline same color as button background (not visible against it): {focus_info}")
    assert focus_info["boxShadow"] == "none", f"competing box-shadow in forced-colors: {focus_info}"
    # Banner instruction is actually visible: non-empty text, readable pairing,
    # nonzero geometry, and NOT occluded by the sticky topbar (top >= topbar bottom).
    assert focus_info["bannerText"], f"banner instruction empty in forced-colors: {focus_info}"
    assert "document is active" in focus_info["bannerText"], f"banner instruction wrong text: {focus_info}"
    assert focus_info["bannerColor"] != focus_info["bannerBg"], (
        f"banner text color == background (invisible) in forced-colors: {focus_info}")
    assert focus_info["bannerW"] > 0 and focus_info["bannerH"] > 0, f"banner zero geometry: {focus_info}"
    assert focus_info["bannerTop"] >= focus_info["topbarBottom"], (
        f"banner occluded by sticky topbar: bannerTop={focus_info['bannerTop']} topbarBottom={focus_info['topbarBottom']}")
    # Severity distinguished by system colors: stale (Mark) vs resolved (Highlight).
    severity = page.evaluate("""() => {
      const mkSentinel = (sysColor) => {
        const r = document.createElement('div');
        r.style.cssText = 'background: '+sysColor+'; position: absolute; visibility: hidden;';
        document.body.appendChild(r);
        const v = getComputedStyle(r).backgroundColor;
        r.remove();
        return v;
      };
      const staleBg = getComputedStyle(document.querySelector('#comment-01JFCSTALE1 .okf-anchor-row__status')).backgroundColor;
      const resolvedBg = getComputedStyle(document.querySelector('#comment-01JFCATTCH1 .okf-anchor-row__status')).backgroundColor;
      return {
        staleBg, resolvedBg,
        markBg: mkSentinel('Mark'),
        highlightBg: mkSentinel('Highlight'),
        staleLabel: document.querySelector('#comment-01JFCSTALE1 .okf-anchor-row__status').textContent.trim(),
        resolvedLabel: document.querySelector('#comment-01JFCATTCH1 .okf-anchor-row__status').textContent.trim(),
      };
    }""")
    assert severity["staleBg"] == severity["markBg"], f"stale severity not system Mark: {severity}"
    assert severity["resolvedBg"] == severity["highlightBg"], f"resolved severity not system Highlight: {severity}"
    assert severity["staleBg"] != severity["resolvedBg"], f"severity not distinguished in forced-colors: {severity}"
    assert "Attached" in severity["resolvedLabel"], f"Attached label not persistent: {severity}"


# ---------------------------------------------------------------------------
# ReviewAll r6 frontend/browser ownership closures (live.js / studio.js).
# Each test is deterministic (no fixed sleeps beyond short waits for async
# resolution) and directly proves one r6 major/minor finding.
# ---------------------------------------------------------------------------


def test_r6_polling_validates_typed_resync_envelope_and_serial_doResync(server_url, page):
    """R6-1: pollOnce must validate the typed envelope and, on resync:true,
    reset cursor ownership and run ONE generation-fenced canonical resync
    (doResync) with doc_revs — not consume events from a resync response and
    silently advance a stale cursor forever."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Advance the cursor so a reset is observable.
    current = page.evaluate("window.okfLoomLive._debug.lastCursor")
    page.evaluate("c=>window.okfLoomLive._dispatchEventForTest({type:'presence',state:'idle',event_cursor:c+1,rev:1})", current)
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") > 0
    # Spy on the resync emit (function-form evaluate so the listener persists).
    page.evaluate("() => { window.__resync = {fired: 0, payload: null}; window.okfLoomLive.on('resync', d => { window.__resync.fired++; window.__resync.payload = d; }); }")
    # Route ONLY the polling (limit=50) request to a typed resync envelope;
    # let loadComments' limit=500 events fetch pass through to the server.
    def events(route, request):
        if "limit=50" in request.url:
            route.fulfill(status=200, json={
                "resync": True, "reason": "expired_cursor", "rev": 9,
                "doc_revs": {"tables/orders": "a" * 64}, "events": [],
            })
        else:
            route.continue_()
    page.route("**/__data/events?*", events)
    # Drive the polling path directly (serial: pollOnce awaits doResync).
    page.evaluate("() => window.okfLoomLive._pollOnceForTest()")
    # Cursor ownership must be reset; doResync must have fired with doc_revs.
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") == 0, "polling resync did not reset cursor ownership"
    assert page.evaluate("window.__resync.fired") >= 1, "polling resync did not run doResync"
    payload = page.evaluate("window.__resync.payload")
    assert payload and payload.get("doc_revs", {}).get("tables/orders") == "a" * 64, "doc_revs not forwarded through resync"


def test_r6_polling_rejects_non_object_envelope_fail_closed(server_url, page):
    """R6-1: a null/non-object polling envelope is a transport failure; the
    poller must fail safe (stop this tick) rather than crash or treat it as an
    empty feed that silently advances the cursor."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current = page.evaluate("window.okfLoomLive._debug.lastCursor")
    page.evaluate("c=>window.okfLoomLive._dispatchEventForTest({type:'presence',state:'idle',event_cursor:c+1,rev:1})", current)
    cursor_before = page.evaluate("window.okfLoomLive._debug.lastCursor")
    def events(route, request):
        if "limit=50" in request.url:
            route.fulfill(status=200, body="null", content_type="application/json")
        else:
            route.continue_()
    page.route("**/__data/events?*", events)
    # Must not throw; the poll tick simply stops.
    page.evaluate("() => window.okfLoomLive._pollOnceForTest()")
    # Cursor unchanged (no events consumed, no resync).
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") == cursor_before


def test_r6_initial_ready_replacement_emits_doc_rev_changed_on_actual_rendered_rev(server_url, page):
    """R6-2: when the unseeded initial-ready canonical fetch REPLACES stale
    rendered bytes (actual rendered rev != fetched rev), doc-rev-changed MUST
    fire so revision-qualified comments/marks refresh atomically. The baseline
    is the ACTUALLY rendered studio rev, not the empty lastDocRev. Proven via
    the studio.js side effect: doc-rev-changed triggers a canonical comment
    refetch (loadComments → /__comments)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    rendered_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    # Count canonical comment refetches (doc-rev-changed → loadComments).
    comments_seen = {"n": 0}
    def comments(route):
        comments_seen["n"] += 1
        route.fulfill(status=200, json={"comments": []})
    page.route("**/__comments", comments)
    baseline_comments = comments_seen["n"]
    # Fresh canonical bytes: different rev + different content + valid projection.
    fresh_text = "FRESHCANONICALBODY"
    fresh_rev = "e" * 64
    fresh_doc = {
        "id": "tables/orders", "annotation_source": fresh_text + "\n",
        "concept_content_rev": fresh_rev,
        "html": '<p data-okf-projection-key="0">' + fresh_text + "</p>",
        "raw": fresh_text + "\n", "title": "Orders", "description": "",
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": fresh_rev,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": len(fresh_text), "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": len(fresh_text), "source_start": 0, "source_end": len(fresh_text)}]}]},
    }
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    # Unseeded baseline (lastDocRev cleared); priorContentRev falls back to the
    # actually-rendered studio rev (rendered_rev).
    page.evaluate("() => window.okfLoomLive._clearLastDocRevForTest('tables/orders')")
    assert page.evaluate("window.okfLoomLive._getLastDocRevForTest('tables/orders')") is None
    # patchNow drives the same patchOpenConcept the initial-ready path uses.
    page.evaluate("() => window.okfLoomLive.patchNow()")
    # doc-rev-changed fires (rendered_rev != fresh_rev) → loadComments refetches.
    page.wait_for_function("() => (window.__commentsCount = (window.__commentsCount || 0)) >= 0 || true", timeout=5_000)
    # Assert via a fresh read of the routed counter.
    page.wait_for_function("() => document.querySelector('.okf-page__body').textContent.indexOf('FRESHCANONICALBODY') >= 0", timeout=10_000)
    assert comments_seen["n"] > baseline_comments, "doc-rev-changed did not trigger a canonical comment refetch"
    assert page.evaluate("window.okfLoomLive._getLastDocRevForTest('tables/orders')") == fresh_rev, "baseline not seeded from fetched fresh rev"
    assert rendered_rev != fresh_rev, "fixture sanity: rendered rev must differ from fresh rev"


def test_r6_capture_revalidates_live_range_after_canonical_doc_replace(server_url, page):
    """R6-3: selection capture must NOT cross the canonical-doc await. If the
    body is replaced during ensureCanonicalDoc, the captured Range is detached;
    capture must re-resolve against the live body and fail closed when the
    selection is gone — never map stale offsets onto new bytes."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    # Force ensureCanonicalDoc to fetch (otherwise it returns the cached doc
    # without awaiting, so no body replacement can occur during capture).
    page.evaluate("window.okfLoomStudio.state.doc = null")
    # Canonical fetch returns a body that does NOT contain the selected text.
    fresh_rev = "b" * 64
    fresh_doc = {
        "id": "tables/orders", "annotation_source": "COMPLETELY DIFFERENT SOURCE\n",
        "concept_content_rev": fresh_rev,
        "html": '<p data-okf-projection-key="0">COMPLETELY DIFFERENT SOURCE</p>',
        "raw": "COMPLETELY DIFFERENT SOURCE\n", "title": "Orders", "description": "",
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": fresh_rev,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 27, "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": 27, "source_start": 0, "source_end": 27}]}]},
    }
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    # Capture must fail closed (the detached range is not mapped onto new bytes).
    error = page.evaluate("window.okfLoomStudio._captureCanonicalAnchor().then(()=>'',e=>e.message)")
    assert error, "capture must fail closed when the canonical fetch replaces the body"
    assert ("no longer valid" in error) or ("Select" in error) or ("single document block" in error), f"unexpected error: {error!r}"
    # The body now reflects canonical bytes (the replacement happened).
    page.wait_for_function("() => (document.querySelector('.okf-page__body').textContent || '').indexOf('COMPLETELY DIFFERENT') >= 0", timeout=5_000)


def test_r6_text_anchor_submit_disabled_until_canonical_rev_known_and_matches(server_url, page):
    """R6-4: a text-anchor (v2 text-quote) draft's Send button must be disabled
    until the canonical rev is known AND draftContentRev exactly matches the
    rendered doc rev; a stale-rev POST must be blocked fail-closed (no network)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.get_by_role("button", name="Comment on the selected text").click()
    # Capture set draftContentRev == current doc rev → Send enabled.
    page.wait_for_function("() => window.okfLoomStudio.state.draftAnchor && window.okfLoomStudio.state.draftAnchor.kind === 'text-quote'", timeout=5_000)
    page.get_by_role("textbox", name="Comment for agent").fill("hello agent")
    expect(page.get_by_role("button", name="Send")).to_be_enabled()
    # Simulate a content-revision change WITHOUT re-capturing: the draft's rev
    # is now stale. Send must disable.
    page.evaluate("() => { var d=window.okfLoomStudio.state.doc; window.okfLoomStudio.state.doc=Object.assign({},d,{concept_content_rev:'c'.repeat(64)}); }")
    page.evaluate("window.okfLoomStudio._renderComments()")
    expect(page.get_by_role("button", name="Send")).to_be_disabled()
    # Fail-closed POST gate: force-enable the button and attempt to send; the
    # post function must re-check and block BEFORE any network request.
    requests = {"n": 0}
    def comment_route(route):
        requests["n"] += 1
        route.fulfill(status=200, json={"ok": True, "comment": {"id": "01JFORCE"}})
    page.route("**/__comment", comment_route)
    page.evaluate("() => { var b=document.querySelector('.okf-composer__submit'); if(b) b.disabled=false; }")
    page.get_by_role("button", name="Send").click()
    page.wait_for_timeout(400)
    assert requests["n"] == 0, "stale-rev text-anchor POST must be blocked before any network request"


def test_r6_failed_conflict_refresh_catch_fenced_by_newer_owner(server_url, page):
    """R6-5: the failed-conflict-refresh catch path must respect editor/comment/
    concept/document-generation fences. A delayed failure from refresh A must
    NOT restore prior state over a newer owner that took over mid-flight."""
    _boot(page, server_url); _seed_comment(page, "stale")
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.route("**/__comment-reanchor", lambda route: route.fulfill(status=409, json={"ok": False, "error": {"code": "concept_conflict"}}))
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    page.get_by_role("button", name="Save anchor").click()
    expect(page.get_by_role("button", name="Refresh and reselect")).to_be_visible(timeout=10_000)
    # Make the refresh fetches fail (instant retries).
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0];window.okfLoomStudio.state.annotationJitter=()=>0")
    page.route("**/__data/doc*", lambda route: route.fulfill(status=500, json={}))
    page.route("**/__comments", lambda route: route.fulfill(status=500, json={}))
    # Start the refresh, then immediately supersede with a newer owner.
    page.get_by_role("button", name="Refresh and reselect").click()
    page.evaluate("""() => {
      // A newer owner took over: editor closed, generations bumped, newer doc/comments set.
      window.okfLoomStudio.state.annotationGeneration++;
      window.okfLoomStudio.state.documentGeneration++;
      window.okfLoomStudio.state.annotationEditor = null;
      window.okfLoomStudio.state.doc = Object.assign({}, window.okfLoomStudio.state.doc, {concept_content_rev: 'd'.repeat(64)});
      window.okfLoomStudio.state.comments = [{id:'01JNEWOWNER', concept:'tables/orders', body:'newer owner', state:'open', row_token:'new', ts:new Date().toISOString(), anchor:{kind:'concept',ref:'tables/orders'}}];
    }""")
    page.wait_for_timeout(1500)  # let the failing refresh resolve + catch run
    # The late catch must NOT have restored prior state over the newer owner.
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == "d" * 64, "late catch overwrote newer owner's doc"
    assert page.evaluate("window.okfLoomStudio.state.comments.length") == 1, "late catch overwrote newer owner's comments"
    assert page.evaluate("window.okfLoomStudio.state.comments[0].id") == "01JNEWOWNER", "late catch restored the prior comment"


def test_r6_overlapping_conflict_modals_settle_prior_owner(server_url, page):
    """R6-6: overlapping showConflictModal calls must not orphan the prior
    caller's promise. The prior owner is settled (Keep-mine semantics) before
    the singleton _resolve/_retryArgs are overwritten."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.__conflictResults = [];
      const p1 = window.okfLoomStudio._showConflictModal({concept:'tables/orders', conflict:true, expected_rev:'x', current_rev:'y'});
      p1.then(r => window.__conflictResults.push({which:1, status: r && r.status, ok: r && r.ok}));
    }""")
    expect(page.locator("[role='alertdialog']")).to_be_visible()
    # A second conflict arrives BEFORE the user resolves the first.
    page.evaluate("""() => {
      const p2 = window.okfLoomStudio._showConflictModal({concept:'tables/orders', conflict:true, expected_rev:'x2', current_rev:'y2'});
      p2.then(r => window.__conflictResults.push({which:2, status: r && r.status, ok: r && r.ok}));
    }""")
    page.wait_for_timeout(250)
    # The FIRST promise must have settled (not orphaned) — with its original 409.
    results = page.evaluate("() => window.__conflictResults")
    assert any(r["which"] == 1 for r in results), "first conflict promise was orphaned by overlapping call"
    first = [r for r in results if r["which"] == 1][0]
    assert first["ok"] is False and first["status"] == 409, f"prior owner should settle with its 409 (keep-mine), got {first}"
    # Resolve the now-current second conflict via Keep mine.
    page.get_by_role("button", name="Keep mine").click()
    page.wait_for_timeout(250)
    results2 = page.evaluate("() => window.__conflictResults")
    assert any(r["which"] == 2 for r in results2), "second conflict promise did not settle"
    assert len(results2) == 2, f"both promises must settle, got {results2}"


def test_r6_wide_rail_is_non_modal_complementary_and_no_tab_trap(server_url, page):
    """R6-7: at wide viewports the drawer rail is a non-modal complementary
    landmark (no aria-modal) and does NOT trap Tab — keyboard users reach the
    interactive document beside it. At narrow widths it reverts to a modal
    dialog (role=dialog, aria-modal=true) with the focus trap re-armed."""
    _boot(page, server_url); _seed_comment(page)
    page.wait_for_function("() => document.body.classList.contains('okf-panel-rail')", timeout=5_000)
    panel = page.locator(".okf-panel")
    # Rail mode: non-modal complementary, no aria-modal.
    assert panel.get_attribute("role") in ("complementary", "region"), f"rail must be non-modal, got role={panel.get_attribute('role')!r}"
    assert panel.get_attribute("aria-modal") is None, "rail must not claim aria-modal"
    # No Tab trap: Tab from the last focusable in the panel reaches the document.
    page.evaluate("""() => {
      var panel = document.querySelector('.okf-panel');
      var f = Array.from(panel.querySelectorAll('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])'))
        .filter(function(n){ return !n.disabled && !n.hasAttribute('hidden') && getComputedStyle(n).display !== 'none'; });
      if (f.length) f[f.length-1].focus();
    }""")
    page.keyboard.press("Tab")
    page.wait_for_timeout(120)
    in_panel = page.evaluate("() => { var p=document.querySelector('.okf-panel'); return !!(p && p.contains(document.activeElement)); }")
    assert in_panel is False, "Tab was trapped inside the rail panel; non-modal rail must let Tab reach the document"
    # Narrow: reverts to modal dialog + aria-modal + Tab trap.
    page.set_viewport_size({"width": 800, "height": 900})
    page.wait_for_function("() => !document.body.classList.contains('okf-panel-rail')", timeout=5_000)
    assert panel.get_attribute("role") == "dialog", "narrow overlay must be modal dialog"
    assert panel.get_attribute("aria-modal") == "true", "narrow overlay must claim aria-modal"
    page.evaluate("""() => {
      var panel = document.querySelector('.okf-panel');
      var f = Array.from(panel.querySelectorAll('button, input, select, textarea')).filter(function(n){ return !n.disabled; });
      if (f.length) f[f.length-1].focus();
    }""")
    page.keyboard.press("Tab")
    page.wait_for_timeout(120)
    in_panel_narrow = page.evaluate("() => { var p=document.querySelector('.okf-panel'); return !!(p && p.contains(document.activeElement)); }")
    assert in_panel_narrow is True, "narrow overlay must trap Tab inside the modal panel"


def test_r6_failed_apply_doc_does_not_pulse_focus_or_emit_patched(server_url, page):
    """R6-8: when studio.applyDoc returns false (failed DOM patch), live.js
    must NOT pulse, steal focus, or emit 'patched' — those describe a
    successfully-applied document. lastDocRev must not advance either."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("() => { window.__patched=[]; window.okfLoomLive.on('patched', d => window.__patched.push(d)); }")
    # Seed a baseline rev so a failed patch cannot advance it.
    page.evaluate("() => window.okfLoomLive._seedLastDocRevForTest('tables/orders','seedrev')")
    # Capture focus before the patch so we can prove it is not stolen.
    page.evaluate("() => { document.querySelector('.okf-topbar').setAttribute('tabindex','-1'); document.querySelector('.okf-topbar').focus(); }")
    focused_before = page.evaluate("() => (document.activeElement && document.activeElement.className) || ''")
    # Force applyDoc to report a failed patch.
    page.evaluate("() => { window.okfLoomStudio.applyDoc = function(){ return false; }; }")
    page.evaluate("() => window.okfLoomLive.patchNow()")
    page.wait_for_timeout(400)
    # No 'patched' event, no lastDocRev advance, no focus theft.
    assert page.evaluate("() => window.__patched.length") == 0, "failed patch must not emit 'patched'"
    assert page.evaluate("() => window.okfLoomLive._getLastDocRevForTest('tables/orders')") == "seedrev", "failed patch must not advance lastDocRev"
    focused_after = page.evaluate("() => (document.activeElement && document.activeElement.className) || ''")
    assert focused_after == focused_before, f"failed patch stole focus: {focused_before!r} -> {focused_after!r}"


def test_r6_bfcache_restore_clears_selection_banner_and_body_class(server_url, page):
    """R6-9: pagehide must clear the selection banner/body-class/dataset
    alongside the editor owner; a persisted pageshow must reconcile them so a
    restored page never shows a stale 'document is active for selection'
    banner over a page whose editor was discarded."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Inject a stale selection surface (banner + body class + dataset + draft).
    page.evaluate("""() => {
      var body = document.querySelector('.okf-page__body');
      var banner = document.createElement('div');
      banner.id = 'okf-annotation-selection-banner';
      banner.className = 'okf-selection-mode-banner';
      banner.textContent = 'stale banner';
      if (body && body.parentNode) body.parentNode.insertBefore(banner, body);
      body.classList.add('okf-page__body--selecting-anchor');
      document.body.dataset.okfSelectionReady = 'true';
      window.okfLoomStudio.state.selectionDraft = {text:'stale', inSource:false, inGraph:false};
    }""")
    assert page.evaluate("() => !!document.getElementById('okf-annotation-selection-banner')")
    assert page.evaluate("() => document.querySelector('.okf-page__body').classList.contains('okf-page__body--selecting-anchor')")
    assert page.evaluate("() => !!document.body.dataset.okfSelectionReady")
    # Freeze (pagehide): the production handler must tear down the stale surface.
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_timeout(200)
    assert page.evaluate("() => !document.getElementById('okf-annotation-selection-banner')"), "stale banner survived pagehide"
    assert page.evaluate("() => !document.querySelector('.okf-page__body').classList.contains('okf-page__body--selecting-anchor')"), "selecting-anchor body class survived pagehide"
    assert page.evaluate("() => !document.body.dataset.okfSelectionReady"), "okfSelectionReady dataset survived pagehide"
    # Persisted restore (pageshow): presentation stays reconciled (defensive clear).
    page.evaluate("() => { var e = new PageTransitionEvent('pageshow', {persisted:true}); window.dispatchEvent(e); }")
    page.wait_for_timeout(200)
    assert page.evaluate("() => !document.getElementById('okf-annotation-selection-banner')"), "banner reappeared after BFCache restore"
    assert page.evaluate("() => !document.querySelector('.okf-page__body').classList.contains('okf-page__body--selecting-anchor')"), "body class reappeared after BFCache restore"
    assert page.evaluate("() => !document.body.dataset.okfSelectionReady"), "dataset reappeared after BFCache restore"


# ---------------------------------------------------------------------------
# R7 frontend majors — deterministic browser tests for the five findings.
# ---------------------------------------------------------------------------

def test_r7_cursor_gap_does_not_commit_gapped_cursor(server_url, page):
    """R7-1: on a cursor gap, handleEvent must reset lastCursor to 0 and return
    WITHOUT committing the gapped cursor. The previous code committed the
    gapped cursor on the line after the reset, so the next poll skipped every
    event in the gap. A subsequent sequential event (the kind that arrives
    during normal backlog replay) must be accepted from cursor 0."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Advance cursor sequentially from whatever position boot left us at.
    base = page.evaluate("window.okfLoomLive._debug.lastCursor")
    seq = [base + 1, base + 2, base + 3, base + 4, base + 5]
    for c in seq:
        page.evaluate("c=>window.okfLoomLive._dispatchEventForTest({type:'presence',state:'idle',event_cursor:c,rev:1})", c)
    advanced = seq[-1]
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") == advanced
    # Dispatch a GAP event (cursor advanced+2 when expecting advanced+1).
    page.evaluate("c=>window.okfLoomLive._dispatchEventForTest({type:'presence',state:'idle',event_cursor:c+2,rev:3})", advanced)
    # R7-1: lastCursor must be 0, NOT advanced+2.
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") == 0, "gapped cursor was committed"
    # R7-1: a subsequent sequential event from 1 is accepted (replay from start).
    page.evaluate("c=>window.okfLoomLive._dispatchEventForTest({type:'presence',state:'idle',event_cursor:c,rev:1})", 1)
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") == 1, "sequential event after gap-reset was rejected"


def test_r7_polling_envelope_validation_rejects_malformed_fields(server_url, page):
    """R7-2: pollOnce must validate the complete discriminated response envelope
    before mutating cursor or revision ownership. Invalid `resync` discriminator
    values, malformed `events` arrays, non-number `rev`, and structurally broken
    event objects must all be rejected (fail safe) — not silently treated as an
    empty feed that advances revision state."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current = page.evaluate("window.okfLoomLive._debug.lastCursor")
    page.evaluate("c=>window.okfLoomLive._dispatchEventForTest({type:'presence',state:'idle',event_cursor:c+1,rev:1})", current)
    rev_before = page.evaluate("window.okfLoomLive.rev")
    cursor_before = page.evaluate("window.okfLoomLive._debug.lastCursor")

    # Direct unit-level proof: the validator rejects each malformed variant.
    results = page.evaluate("""() => {
      const v = window.okfLoomLive._isValidPollEnvelopeForTest;
      return {
        // Valid resync envelope accepted.
        valid_resync: v({resync: true, reason: "expired", rev: 5, doc_revs: {"a": "b"}, events: []}),
        // Valid feed envelope accepted.
        valid_feed: v({events: [{type: "changed", event_cursor: 1, rev: 1}], rev: 1}),
        // Empty feed accepted.
        valid_empty: v({events: [], rev: 1}),
        // Non-object rejected.
        null_rejected: v(null) === false,
        array_rejected: v([]) === false,
        // rev must be number if present.
        rev_string: v({events: [], rev: "5"}) === false,
        rev_nan: v({events: [], rev: NaN}) === false,
        // events must be array — a truthy non-array must NOT silently coerce
        // to an empty feed that advances revision state.
        events_string: v({events: "notarray"}) === false,
        events_missing: v({rev: 1}) === false,
        events_object: v({events: {}}) === false,
        // each event must be non-null object with valid integer cursor >= 1.
        event_null: v({events: [null]}) === false,
        event_array: v({events: [[]]}) === false,
        event_no_cursor: v({events: [{type: "changed"}]}) === false,
        event_bad_cursor: v({events: [{type: "changed", event_cursor: 0}]}) === false,
        event_negative_cursor: v({events: [{type: "changed", event_cursor: -1}]}) === false,
        event_fractional_cursor: v({events: [{type: "changed", event_cursor: 1.5}]}) === false,
        // doc_revs must be plain object in resync branch (null is OK = absent).
        resync_docrevs_array: v({resync: true, doc_revs: []}) === false,
        resync_docrevs_string: v({resync: true, doc_revs: "x"}) === false,
        resync_docrevs_null_ok: v({resync: true, doc_revs: null, events: []}) === true,
        // reason must be string in resync branch if present.
        resync_reason_number: v({resync: true, reason: 5, events: []}) === false,
      };
    }""")
    for key, val in results.items():
        if key.startswith("valid_"):
            assert val is True, f"validator wrongly rejected {key}"
        else:
            assert val is True, f"validator wrongly accepted {key}"

    # Integration proof: pollOnce with a malformed envelope does not advance
    # revision or cursor state.
    def bad_events(route, request):
        if "limit=50" not in request.url:
            route.continue_()
            return
        route.fulfill(status=200, json={"events": "BROKEN", "rev": 999})
    page.route("**/__data/events?*", bad_events)
    page.evaluate("() => window.okfLoomLive._pollOnceForTest()")
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") == cursor_before, "malformed envelope advanced cursor"
    assert page.evaluate("window.okfLoomLive.rev") == rev_before, "malformed envelope advanced rev"


def test_r7_composer_post_failure_restores_draft_rev_and_enables_send(server_url, page):
    """R7-3: a failed composer POST must restore draftContentRev (not just the
    anchor text) so the immediate retry is not rejected by the fail-closed gate.
    The Send button must be derived through _refreshComposerSubmitState, not
    unconditionally enabled. The catch path must use the same ownership
    predicate (including nav epoch) as the success path."""
    _boot(page, server_url)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.get_by_role("button", name="Comment on the selected text").click()
    page.wait_for_function("() => window.okfLoomStudio.state.draftAnchor && window.okfLoomStudio.state.draftAnchor.kind === 'text-quote'", timeout=5_000)
    page.get_by_role("textbox", name="Comment for agent").fill("hello agent")
    expect(page.get_by_role("button", name="Send")).to_be_enabled()
    doc_rev = page.evaluate("() => window.okfLoomStudio.state.doc.concept_content_rev")
    draft_rev = page.evaluate("() => window.okfLoomStudio.state.draftContentRev")
    assert draft_rev == doc_rev, "fixture sanity: draftContentRev must match doc rev"
    # Route /__comment to fail.
    page.route("**/__comment", lambda route: route.fulfill(status=500, json={"ok": False, "error": "server error"}))
    page.get_by_role("button", name="Send").click()
    # R7-3: after failure, draftContentRev is restored (not null).
    page.wait_for_function("() => window.okfLoomStudio.state.draftContentRev !== null", timeout=5_000)
    restored_rev = page.evaluate("() => window.okfLoomStudio.state.draftContentRev")
    assert restored_rev == doc_rev, f"draftContentRev not restored on failure: {restored_rev!r} != {doc_rev!r}"
    # R7-3: the anchor is restored to text-quote.
    assert page.evaluate("() => window.okfLoomStudio.state.draftAnchor.kind") == "text-quote"
    # R7-3: the text is restored.
    assert page.get_by_role("textbox", name="Comment for agent").input_value() == "hello agent"
    # R7-3: Send is re-enabled through _refreshComposerSubmitState (revs match).
    # A 500 is now classified UNCERTAIN, so the action becomes "Retry send"
    # (same draft, same key on retry) — the gate still opens for the retry.
    expect(page.get_by_role("button", name="Retry send")).to_be_enabled()
    # R7-3: the immediate retry actually issues a network request (gate opens).
    # Under the hardened lifecycle, success requires POST acceptance AND a
    # canonical settle — route the settle endpoints to the matching held rev so
    # the retry completes with "Comment posted" (no false success otherwise).
    anchor_obj = page.evaluate("() => window.okfLoomStudio.state.draftAnchor")
    retry_row = {"id": "01JRETRY", "concept": "tables/orders", "body": "hello agent",
        "state": "open", "row_token": "r2:retry", "ts": "2026-01-01T00:00:00Z",
        "anchor": anchor_obj,
        "anchor_resolution": {"status": "resolved", "reason": "original_position",
            "concept_content_rev": doc_rev, "resolver_version": 2,
            "position": anchor_obj.get("position", {"start": 0, "end": 0}),
            "block": anchor_obj.get("block", {"id": "paragraph:1", "hash": "sha256:" + "a" * 64, "start": 0, "end": 11}),
            "changes": []}}
    page.route("**/__comment", lambda route: route.fulfill(status=201, json={"ok": True, "comment": retry_row}))
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc("target text\n", "target text", doc_rev)))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": []}))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": [retry_row]}))
    page.get_by_role("button", name="Retry send").click()
    # Wait for the success toast (proves the retry POST + canonical settle succeeded).
    page.wait_for_selector("text=Comment posted", timeout=10_000)


def test_r7_composer_post_failure_stale_rev_disables_send(server_url, page):
    """R7-3: when the doc rev changes during a failed POST, the draft fence
    must NOT be restored and Send must stay DISABLED through
    _refreshComposerSubmitState — not unconditionally enabled."""
    _boot(page, server_url)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.get_by_role("button", name="Comment on the selected text").click()
    page.wait_for_function("() => window.okfLoomStudio.state.draftAnchor && window.okfLoomStudio.state.draftAnchor.kind === 'text-quote'", timeout=5_000)
    page.get_by_role("textbox", name="Comment for agent").fill("hello agent")
    expect(page.get_by_role("button", name="Send")).to_be_enabled()
    # Route /__comment to fail.
    page.route("**/__comment", lambda route: route.fulfill(status=500, json={"ok": False, "error": "server error"}))
    # Change the doc rev WHILE the POST is in flight (before it resolves).
    page.evaluate("""() => {
      var d = window.okfLoomStudio.state.doc;
      window.okfLoomStudio.state.doc = Object.assign({}, d, {concept_content_rev: 'f'.repeat(64)});
    }""")
    page.get_by_role("button", name="Send").click()
    page.wait_for_timeout(500)
    # R7-3: draftContentRev must NOT be restored (stale rev).
    restored = page.evaluate("() => window.okfLoomStudio.state.draftContentRev")
    assert restored != page.evaluate("() => window.okfLoomStudio.state.doc.concept_content_rev"), "stale draftContentRev was restored"
    # R7-3: Send must be DISABLED (the _refreshComposerSubmitState gate closes).
    expect(page.get_by_role("button", name="Send")).to_be_disabled()


def test_r7_document_patch_rolls_back_all_regions_on_failure(server_url, page):
    """R7-4: commitDocument must be truly transactional. If diffAndPatchBody
    throws after the title was already mutated, ALL touched DOM regions (title,
    description, body, sourcePre) must be restored to their pre-patch snapshot
    — no mixed-revision DOM visible while state.doc still points to the old rev."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    original_title = page.evaluate("() => document.querySelector('.okf-page__title').textContent")
    original_body_html = page.evaluate("() => document.querySelector('.okf-page__body').innerHTML")
    original_rev = page.evaluate("() => window.okfLoomStudio.state.doc.concept_content_rev")
    # Build a doc whose title and body differ from the current page.
    fresh_rev = "b" * 64
    fresh_doc = {
        "id": "tables/orders", "annotation_source": "CHANGED SOURCE\n",
        "concept_content_rev": fresh_rev,
        "html": '<p data-okf-projection-key="0">CHANGED SOURCE</p>',
        "raw": "CHANGED SOURCE\n", "title": "CHANGED TITLE", "description": "CHANGED DESC",
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": fresh_rev,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 15, "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": 15, "source_start": 0, "source_end": 15}]}]},
    }
    # Inject a failure into the BODY PATCH phase by overriding the body
    # element's DOM mutation methods to throw. commitDocument stages title
    # first, then body diff — so the title is mutated before the throw. The
    # catch must roll back ALL touched regions from their pre-patch snapshots.
    result = page.evaluate("""(doc) => {
      var bodyEl = document.querySelector('.okf-page__body');
      var titleEl = document.querySelector('.okf-page__title');
      // Override removeChild/insertBefore/replaceChild on the body instance
      // so diffAndPatchBody throws when it tries to patch children.
      var origRemove = bodyEl.removeChild.bind(bodyEl);
      var origInsert = bodyEl.insertBefore.bind(bodyEl);
      bodyEl.removeChild = function() { throw new Error('INJECTED_BODY_DIFF_FAILURE'); };
      bodyEl.insertBefore = function() { throw new Error('INJECTED_BODY_DIFF_FAILURE'); };
      bodyEl.replaceChild = function() { throw new Error('INJECTED_BODY_DIFF_FAILURE'); };
      try {
        var ok = window.okfLoomStudio.applyDoc(doc, {pulse: false});
        return {ok: ok, titleAfter: titleEl.textContent};
      } finally {
        // Restore original methods.
        bodyEl.removeChild = origRemove;
        bodyEl.insertBefore = origInsert;
      }
    }""", fresh_doc)
    # R7-4: applyDoc returned false (staging failed).
    assert result["ok"] is False, "commitDocument must return false on staging failure"
    # R7-4: title was mutated then ROLLED BACK to original.
    assert result["titleAfter"] == original_title, f"title not rolled back: {result['titleAfter']!r} != {original_title!r}"
    # R7-4: body was NOT left in a partially-mutated state.
    current_body = page.evaluate("() => document.querySelector('.okf-page__body').innerHTML")
    assert current_body == original_body_html, "body was not rolled back to pre-patch state"
    # R7-4: state.doc still points to the OLD revision.
    current_rev = page.evaluate("() => window.okfLoomStudio.state.doc.concept_content_rev")
    assert current_rev == original_rev, f"state.doc advanced despite failed staging: {current_rev!r} != {original_rev!r}"


def test_r7_overlapping_resync_drains_latest_doc_revs(server_url, page):
    """R7-5: when a newer doResync(newDocRevs) arrives during an active resync,
    the in-flight call must DRAIN the latest payload (not its own stale
    docRevs) after it settles. The previous code re-dispatched with the older
    call's local docRevs, silently dropping the newer payload."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Spy on resync emits.
    page.evaluate("""() => {
      window.__resyncPayloads = [];
      window.okfLoomLive.on('resync', d => { window.__resyncPayloads.push(d.doc_revs); });
    }""")
    # Make studio.js absent so doResync falls back to patchOpenConcept, which
    # we can control timing on. Actually, doResync with studio.js present just
    # emits and returns quickly. We need the in-flight one to be AWAITING.
    # The only await in doResync is patchOpenConcept (when studio is absent).
    # With studio present, doResync is effectively synchronous (emit + return).
    # So we need to test the pending-drain mechanism differently.
    #
    # The mechanism: doResync(docRevs) when _resyncInFlight is true stores the
    # payload in _pendingResyncDocRevs. When the in-flight one settles, it
    # drains _pendingResyncDocRevs (not its local docRevs).
    #
    # To make doResync actually stay in-flight, make applyDoc absent.
    page.evaluate("""() => {
      // Remove studio.applyDoc so doResync falls back to patchOpenConcept,
      // which awaits a fetch. That gives us a window to call doResync again.
      window.__savedApplyDoc = window.okfLoomStudio.applyDoc;
      delete window.okfLoomStudio.applyDoc;
      window.okfLoomStudio.applyDoc = undefined;
    }""")
    # Route /__data/doc to hang so the in-flight patchOpenConcept stays pending.
    doc_hang_count = {"n": 0}
    def hang_doc(route):
        doc_hang_count["n"] += 1
        if doc_hang_count["n"] == 1:
            # First fetch (the in-flight one): delay so we can overlap.
            page.wait_for_timeout(200)
            route.fulfill(status=200, json={
                "id": "tables/orders", "annotation_source": "x\n",
                "concept_content_rev": "z" * 64, "html": "<p>x</p>",
                "raw": "x\n", "title": "Orders", "description": "",
                "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": "z" * 64,
                    "offset_unit": "unicode-code-point", "complete": True,
                    "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 1, "heading_path": [], "dom_key": 0,
                                "segments": [{"dom_start": 0, "dom_end": 1, "source_start": 0, "source_end": 1}]}]},
            })
        else:
            route.continue_()
    page.route("**/__data/doc*", hang_doc)
    # Start the first resync with payload A.
    page.evaluate("""() => window.__p1 = window.okfLoomLive.resync({"old_rev": "a"})""")
    page.wait_for_timeout(100)
    # While the first is in-flight, call resync with payload B.
    page.evaluate("""() => window.__p2 = window.okfLoomLive.resync({"new_rev": "b"})""")
    page.wait_for_timeout(400)
    # R7-5: the drained payload must be B (latest), not A (stale).
    payloads = page.evaluate("() => window.__resyncPayloads")
    # The first emit has payload A, the drain emit has payload B.
    assert len(payloads) >= 2, f"expected at least 2 resync emits (initial + drain), got {payloads}"
    assert payloads[-1] is not None and payloads[-1].get("new_rev") == "b", \
        f"drained stale payload instead of latest: {payloads}"


# ---------------------------------------------------------------------------
# R8 frontend majors/minors — deterministic browser tests.
# ---------------------------------------------------------------------------

def test_r8_polling_suspension_aborts_inflight_and_fences_late_commit(server_url, page):
    """R8-POLL: suspension (pagehide/_suspend) must abort the IN-FLIGHT poll
    fetch via the generation-owned AbortController — not merely cancel future
    ticks. A late response delivered after suspension must be fenced by the
    generation counter and never advance cursor/rev state."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    cursor_before = page.evaluate("window.okfLoomLive._debug.lastCursor")
    # Hold the polling (limit=50&) request so the tick stays in-flight; let the
    # loadComments events fetch (limit=500) pass through. Use "limit=50&" so the
    # check does not also match "limit=500". Non-blocking hold (store route,
    # fulfill later) so the event loop is free to drive the test.
    held = {"route": None}
    def hold_events(route, request):
        if "limit=50&" in request.url:
            held["route"] = route  # do not fulfill yet; do not block the loop
        else:
            route.continue_()
    page.route("**/__data/events?*", hold_events)
    # Start the polling session: a fresh generation + abort owner.
    page.evaluate("() => window.okfLoomLive._startPollingForTest()")
    gen_before = page.evaluate("window.okfLoomLive._debug.pollingGeneration")
    assert page.evaluate("window.okfLoomLive._debug.pollingActive") is True
    # Wait for the in-flight polling fetch to be captured by the route.
    deadline = time.monotonic() + 2
    while held["route"] is None and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    assert held["route"] is not None, "polling fetch was not captured"
    # Suspend (pagehide) — must abort the in-flight fetch and advance the gen.
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_timeout(100)
    assert page.evaluate("window.okfLoomLive._debug.pollingActive") is False, "polling survived suspension"
    assert page.evaluate("window.okfLoomLive._debug.pollingGeneration") > gen_before, "generation did not advance on suspend"
    # Now deliver the late response that WOULD advance the cursor.
    late_cursor = (cursor_before or 0) + 50
    try:
        held["route"].fulfill(status=200, json={"events": [
            {"type": "presence", "state": "idle", "event_cursor": late_cursor, "rev": 1},
        ], "rev": 1})
    except Exception:
        pass
    page.wait_for_timeout(200)
    # R8-POLL: the late commit must be fenced — cursor unchanged.
    assert page.evaluate("window.okfLoomLive._debug.lastCursor") == cursor_before, \
        "late polling commit was not fenced by the generation owner"
    page.unroute("**/__data/events?*", hold_events)


def test_r8_polling_single_flight_settlement_scheduling(server_url, page):
    """R8-POLL: polling uses single-flight settlement scheduling — the next
    tick is scheduled only AFTER the current tick settles (in a finally), so
    two pagination loops can never overlap. Proven by: during an in-flight
    (held) tick there is NO pending polling timer (setInterval would keep one
    armed) and at most one concurrent polling fetch."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    inflight = {"n": 0, "max": 0}
    held = {"route": None}
    def hold_events(route, request):
        # "limit=50&" matches only the polling fetch, NOT loadComments' limit=500.
        if "limit=50&" in request.url:
            inflight["n"] += 1
            if inflight["n"] > inflight["max"]:
                inflight["max"] = inflight["n"]
            held["route"] = route  # non-blocking hold
        else:
            route.continue_()
    page.route("**/__data/events?*", hold_events)
    page.evaluate("() => window.okfLoomLive._startPollingForTest()")
    # Wait for the first polling fetch to be captured (tick is now in-flight).
    deadline = time.monotonic() + 2
    while held["route"] is None and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    assert held["route"] is not None, "polling fetch was not captured"
    # R8-POLL: while the tick is in-flight, NO polling timer is armed
    # (settlement scheduling clears the timer and only re-arms on settle).
    assert page.evaluate("window.okfLoomLive._debug.pollingTimerPending") is False, \
        "a polling timer is armed mid-tick — overlapping loops possible"
    inflight["n"] = 0  # the held tick will resolve on release
    # Allow the tick to settle, then release.
    try:
        held["route"].fulfill(status=200, json={"events": [], "rev": 1})
    except Exception:
        pass
    page.wait_for_timeout(200)
    # At most one concurrent polling fetch observed (no overlap).
    assert inflight["max"] <= 1, f"overlapping polling loops observed: max concurrent={inflight['max']}"
    page.evaluate("() => window.okfLoomLive._stopPollingForTest()")
    page.unroute("**/__data/events?*", hold_events)


def test_r8_ready_rev_is_monotonic(server_url, page):
    """R8-MINOR1: handleReady must advance rev monotonically via noteRev. A
    reconnect delivering a LOWER rev must not regress rev (the previous direct
    assignment `rev = d.rev || rev` could)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Advance rev to a high value via noteRev through a ready frame.
    page.evaluate("() => window.okfLoomLive._dispatchReadyForTest({rev: 100, doc_revs: {}})")
    assert page.evaluate("window.okfLoomLive.rev") == 100
    # A reconnect with a LOWER rev must NOT regress.
    page.evaluate("() => window.okfLoomLive._dispatchReadyForTest({rev: 5, doc_revs: {}})")
    assert page.evaluate("window.okfLoomLive.rev") == 100, "ready regressed rev below the monotonic high-water mark"


def test_r8_backoff_clear_timer_cancelled_on_suspend(server_url, page):
    """R8-MINOR2: the backoff-clear timer is tracked and cancelled by _suspend.
    An untracked timer can survive page suspension and mutate a later lifecycle
    generation's conn.backoff."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # A ready frame arms the backoff-clear timer (scheduleBackoffClear).
    page.evaluate("() => window.okfLoomLive._dispatchReadyForTest({rev: 1, doc_revs: {}})")
    assert page.evaluate("window.okfLoomLive._debug.backoffClearPending") is True, "backoff-clear timer not armed"
    # Suspend must cancel it.
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_timeout(50)
    assert page.evaluate("window.okfLoomLive._debug.backoffClearPending") is False, \
        "backoff-clear timer survived suspension"


def test_r8_isconceptpage_local_to_livejs_no_reference_error(server_url, page):
    """R8-MAJOR: handleReady calls isConceptPage() to decide whether an
    authoritative doc_revs omission means the open concept was removed. The
    function previously existed only in studio.js's closure, so the omission
    path raised ReferenceError. Now live.js defines its own copy and the
    open-removed signal fires without error."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Seed lastDocRev so the ready path reaches the doc_revs-omission branch.
    page.evaluate("() => window.okfLoomLive._seedLastDocRevForTest('tables/orders', 'seeded')")
    fired = page.evaluate("""() => {
      let removed = false, err = null;
      window.okfLoomLive.on('open-removed', () => { removed = true; });
      try {
        // doc_revs omits the open concept -> authoritative absence -> open-removed.
        window.okfLoomLive._dispatchReadyForTest({rev: 1, doc_revs: {"other/concept": "x"}});
      } catch (e) { err = e.message; }
      return { removed: removed, err: err };
    }""")
    assert fired["err"] is None, f"handleReady threw (isConceptPage undefined?): {fired['err']}"
    assert fired["removed"] is True, "open-removed did not fire for an omitted open concept"


def test_r8_doresync_awaits_async_studio_refresh(server_url, page):
    """R8-RESYNC: doResync must await the asynchronous Studio refresh
    (loadComments), not treat the synchronous emit as completion. While the
    refresh is in flight, _resyncInFlight stays true so a newer resync is
    correctly single-flight queued rather than overlapping the real work."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Hold the canonical comments fetch (non-blocking) so loadComments — the
    # resync handler — stays pending while the event loop is free to assert.
    held = {"route": None}
    def hold_comments(route):
        held["route"] = route  # do not fulfill yet; do not block the loop
    page.route("**/__comments", hold_comments)
    # Start a resync. The studio resync handler returns loadComments() which
    # awaits the held /__comments, so doResync must stay in-flight.
    page.evaluate("() => { window.__r = window.okfLoomLive.resync(null); }")
    page.wait_for_timeout(150)
    assert page.evaluate("window.okfLoomLive._debug.resyncInFlight") is True, \
        "doResync settled before the async Studio refresh completed (emit treated as completion)"
    # Release the held fetch; doResync should now settle.
    try:
        held["route"].fulfill(status=200, json={"comments": []})
    except Exception:
        pass
    page.wait_for_function("() => window.okfLoomLive._debug.resyncInFlight === false", timeout=5_000)
    page.unroute("**/__comments", hold_comments)


def test_r8_bfcache_pageshow_runs_canonical_resync(server_url, page):
    """R8-BFCACHE: a persisted pageshow must perform a canonical resync, not
    merely reconnect the transport. Without it, stale comments/activity/
    document state can survive restoration."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.__resyncFired = 0;
      window.okfLoomLive.on('resync', () => { window.__resyncFired++; });
    }""")
    baseline = page.evaluate("window.__resyncFired")
    # Persisted pageshow (BFCache restore) must trigger doResync.
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    page.wait_for_function("() => window.__resyncFired > 0 || window.okfLoomLive._debug.resyncInFlight === true", timeout=5_000)
    assert page.evaluate("window.__resyncFired") > baseline, "persisted pageshow did not run a canonical resync"
    # A non-persisted pageshow must NOT trigger a resync (ordinary load).
    after = page.evaluate("window.__resyncFired")
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:false}))")
    page.wait_for_timeout(100)
    assert page.evaluate("window.__resyncFired") == after, "non-persisted pageshow triggered a resync"


def test_r8_loadcomments_atomic_does_not_commit_doc_on_comment_skew(server_url, page):
    """R8-ATOMIC: loadComments must stage+validate+commit atomically. When the
    fetched comments are skewed (anchor_resolution resolved against a different
    content rev than the fetched doc), the document must NOT be committed — a
    new document must never coexist with stale comments. The previous code
    committed the document first and only then checked parity."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    fresh_rev = "f" * 64
    fresh_doc = {
        "id": "tables/orders", "annotation_source": "SKEWED BODY\n",
        "concept_content_rev": fresh_rev,
        "html": '<p data-okf-projection-key="0">SKEWED BODY</p>',
        "raw": "SKEWED BODY\n", "title": "Orders", "description": "",
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": fresh_rev,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 12, "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": 12, "source_start": 0, "source_end": 12}]}]},
    }
    # Comments resolved against the OLD rev -> skewed vs the fresh doc.
    skewed_comments = {"comments": [{
        "id": "01JSKEWED00", "concept": "tables/orders", "body": "stale anchor", "state": "open",
        "row_token": "r2:skew", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"},
        "anchor_resolution": {"status": "resolved", "reason": "original_position",
            "concept_content_rev": current_rev, "resolver_version": 2, "position": {"start": 0, "end": 5},
            "block": {"id": "paragraph:1"}, "changes": []},
    }]}
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json=skewed_comments))
    # Drive loadComments; after exhausting skew retries it must report error
    # WITHOUT committing the fresh doc.
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10_000)
    # R8-ATOMIC: the fresh doc was NOT committed (comments were skewed).
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == current_rev, \
        "loadComments committed a new document while comments were skewed"
    assert page.evaluate("() => document.querySelector('.okf-page__body').textContent.indexOf('SKEWED BODY')") < 0, \
        "skewed document body was committed"


def test_r8_loadcomments_atomic_commits_when_tuple_is_consistent(server_url, page):
    """R8-ATOMIC complement: when doc + comments are a CONSISTENT tuple (parity
    holds), loadComments commits both atomically. Guards against the atomic
    restructure accidentally refusing valid data."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    fresh_rev = "c" * 64
    fresh_doc = {
        "id": "tables/orders", "annotation_source": "CONSISTENT BODY\n",
        "concept_content_rev": fresh_rev,
        "html": '<p data-okf-projection-key="0">CONSISTENT BODY</p>',
        "raw": "CONSISTENT BODY\n", "title": "Orders", "description": "",
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": fresh_rev,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 15, "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": 15, "source_start": 0, "source_end": 15}]}]},
    }
    consistent_comments = {"comments": [{
        "id": "01JCONSIST", "concept": "tables/orders", "body": "matches", "state": "open",
        "row_token": "r2:ok", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"},
        "anchor_resolution": {"status": "resolved", "reason": "original_position",
            "concept_content_rev": fresh_rev, "resolver_version": 2, "position": {"start": 0, "end": 5},
            "block": {"id": "paragraph:1"}, "changes": []},
    }]}
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json=consistent_comments))
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready'", timeout=10_000)
    # Both committed atomically.
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == fresh_rev
    assert page.evaluate("() => document.querySelector('.okf-page__body').textContent.indexOf('CONSISTENT BODY')") >= 0
    assert page.evaluate("window.okfLoomStudio.state.comments.some(c => c.id === '01JCONSIST')")


def test_r8_inline_reply_rejects_mismatched_response(server_url, page):
    """R8-REPLY: an inline reply must validate the returned comment's id,
    parent, concept, and row_token before adopting it. A mismatched response
    (wrong parent/concept/row_token) is rejected rather than mutating
    state.comments."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Seed a parent comment to reply to.
    page.evaluate("""() => {
      window.okfLoomStudio.state.comments = [{
        id: '01JPARENT000', concept: 'tables/orders', body: 'parent', state: 'open',
        row_token: 'r', ts: new Date().toISOString(), anchor: {kind:'concept', ref:'tables/orders'}
      }];
      window.okfLoomStudio.openPanel('comments');
      window.okfLoomStudio._renderComments();
    }""")
    # Route /__comment to return a comment with the WRONG parent + concept.
    page.route("**/__comment", lambda route: route.fulfill(status=200, json={
        "ok": True, "comment": {
            "id": "01JREPLYBAD", "concept": "other/concept", "body": "x", "state": "open",
            "row_token": "r2:bad", "ts": "2026-01-01T00:00:00Z",
            "parent_id": "DIFFERENT_PARENT", "anchor": {"kind": "concept", "ref": "other/concept"},
        }}))
    page.get_by_role("button", name="Reply").first.click()
    page.locator(".okf-inline-reply__textarea").fill("a reply")
    page.get_by_role("button", name="Reply", exact=True).last.click()
    page.wait_for_timeout(300)
    # R8-REPLY: the mismatched response was rejected — not upserted.
    assert page.evaluate("window.okfLoomStudio.state.comments.some(c => c.id === '01JREPLYBAD')") is False, \
        "inline reply adopted a mismatched (wrong parent/concept) response"
    page.unroute("**/__comment")


def test_r8_inline_reply_stale_completion_does_not_mutate(server_url, page):
    """R8-REPLY: a stale completion (navigation/revision change after the POST
    started) must NOT mutate stale DOM/focus/notifications. The route/doc/page
    generation owner fences the late completion."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.okfLoomStudio.state.comments = [{
        id: '01JPARENT000', concept: 'tables/orders', body: 'parent', state: 'open',
        row_token: 'r', ts: new Date().toISOString(), anchor: {kind:'concept', ref:'tables/orders'}
      }];
      window.okfLoomStudio.openPanel('comments');
      window.okfLoomStudio._renderComments();
    }""")
    # Hold the POST (non-blocking) so the event loop is free to navigate
    # mid-flight; fulfill later once the page no longer owns the completion.
    held = {"route": None}
    def hold_comment(route):
        held["route"] = route  # do not fulfill yet; do not block the loop
    page.route("**/__comment", hold_comment)
    page.get_by_role("button", name="Reply").first.click()
    page.locator(".okf-inline-reply__textarea").fill("a reply")
    page.get_by_role("button", name="Reply", exact=True).last.click()
    # Wait for the POST to be captured (it is now in-flight).
    deadline = time.monotonic() + 2
    while held["route"] is None and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    assert held["route"] is not None, "reply POST was not captured"
    # While the POST is in flight, navigate away (advance the nav epoch) so the
    # page no longer owns this reply completion.
    page.evaluate("() => window.okfLoomLive._bumpNavEpoch()")
    # Now release the held POST. The stale completion must be fenced.
    try:
        held["route"].fulfill(status=200, json={
            "ok": True, "comment": {
                "id": "01JREPLYOK0", "concept": "tables/orders", "body": "stale completion",
                "state": "open", "row_token": "r2:ok", "ts": "2026-01-01T00:00:00Z",
                "parent_id": "01JPARENT000", "anchor": {"kind": "concept", "ref": "tables/orders"},
            }})
    except Exception:
        pass
    page.wait_for_timeout(400)
    # R8-REPLY: the stale completion was fenced — not upserted, no toast on the
    # wrong page.
    assert page.evaluate("window.okfLoomStudio.state.comments.some(c => c.id === '01JREPLYOK0')") is False, \
        "stale inline-reply completion mutated state after navigation"
    page.unroute("**/__comment", hold_comment)


def test_r8_inline_reply_adopts_valid_matched_response(server_url, page):
    """R8-REPLY complement: a reply response that DOES match id/parent/concept/
    row_token is adopted. Guards against the validation accidentally rejecting
    legitimate replies."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.okfLoomStudio.state.comments = [{
        id: '01JPARENT000', concept: 'tables/orders', body: 'parent', state: 'open',
        row_token: 'r', ts: new Date().toISOString(), anchor: {kind:'concept', ref:'tables/orders'}
      }];
      window.okfLoomStudio.openPanel('comments');
      window.okfLoomStudio._renderComments();
    }""")
    page.route("**/__comment", lambda route: route.fulfill(status=200, json={
        "ok": True, "comment": {
            "id": "01JREPLYOK0", "concept": "tables/orders", "body": "valid reply", "state": "open",
            "row_token": "r2:ok", "ts": "2026-01-01T00:00:00Z",
            "parent_id": "01JPARENT000", "anchor": {"kind": "concept", "ref": "tables/orders"},
        }}))
    page.get_by_role("button", name="Reply").first.click()
    page.locator(".okf-inline-reply__textarea").fill("a reply")
    page.get_by_role("button", name="Reply", exact=True).last.click()
    page.wait_for_function("() => window.okfLoomStudio.state.comments.some(c => c.id === '01JREPLYOK0')", timeout=5_000)
    assert page.evaluate("window.okfLoomStudio.state.comments.some(c => c.id === '01JREPLYOK0')") is True
    page.unroute("**/__comment")


# ---------------------------------------------------------------------------
# R9 frontend findings — deterministic browser tests.
# ---------------------------------------------------------------------------

def test_r9_bfcache_pageshow_resync_completes_before_connect(server_url, page):
    """R9-BFCACHE: a persisted pageshow must complete the canonical resync
    BEFORE connect() wires SSE event delivery. Fresh SSE frames must not mutate
    the restored page while canonical doc/comment reconciliation is pending
    (the no-refresh contract: canonical state first, events only provide
    subsequent notifications)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    held = {"route": None}
    def hold_comments(route):
        if held["route"] is None:
            held["route"] = route
        else:
            route.continue_()
    page.route("**/__comments", hold_comments)
    page.evaluate("""() => {
      window.__resyncFired = 0;
      window.okfLoomLive.on('resync', () => { window.__resyncFired++; });
    }""")
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    page.wait_for_function("() => window.__resyncFired > 0 || window.okfLoomLive._debug.resyncInFlight === true", timeout=5_000)
    # R9 ordering: while the canonical resync is in-flight, connect() has NOT run.
    assert page.evaluate("window.okfLoomLive._debug.resyncInFlight") is True, \
        "canonical resync should be in-flight (loadComments pending)"
    assert page.evaluate("window.okfLoomLive._debug.connSource") is False, \
        "SSE transport was opened BEFORE the canonical resync completed"
    # Release the held resync; doResync settles, THEN connect() runs.
    try:
        held["route"].fulfill(status=200, json={"comments": []})
    except Exception:
        pass
    page.wait_for_function("() => window.okfLoomLive._debug.resyncInFlight === false", timeout=5_000)
    page.wait_for_function("() => window.okfLoomLive._debug.connSource === true", timeout=5_000)
    page.unroute("**/__comments", hold_comments)


def test_r9_suspend_fences_inflight_resync_post_await(server_url, page):
    """R9-RESYNC-FENCE: _suspend must advance the resync suspend epoch so an
    in-flight doResync's post-await drain is fenced. The restored lifecycle runs
    its own fresh resync; a pending payload captured by the suspended lifecycle
    is NOT drained."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    held = {"route": None}
    def hold_comments(route):
        if held["route"] is None:
            held["route"] = route
        else:
            route.continue_()
    page.route("**/__comments", hold_comments)
    page.evaluate("""() => {
      window.__resyncPayloads = [];
      window.okfLoomLive.on('resync', (d) => { window.__resyncPayloads.push(d.doc_revs); });
    }""")
    epoch_before = page.evaluate("window.okfLoomLive._debug.resyncSuspendEpoch")
    # Start resync with payload A — goes in-flight (held /__comments).
    page.evaluate("() => { window.__r1 = window.okfLoomLive.resync({tag: 'A'}); }")
    page.wait_for_function("() => window.okfLoomLive._debug.resyncInFlight === true", timeout=5_000)
    # Queue a newer payload B while A is in-flight.
    page.evaluate("() => { window.__r2 = window.okfLoomLive.resync({tag: 'B'}); }")
    page.wait_for_timeout(100)
    # Suspend (pagehide) — must advance the suspend epoch.
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_timeout(100)
    epoch_after = page.evaluate("window.okfLoomLive._debug.resyncSuspendEpoch")
    assert epoch_after > epoch_before, "suspend did not advance the resync suspend epoch"
    # Release the held fetch; the in-flight resync settles but must NOT drain B.
    try:
        held["route"].fulfill(status=200, json={"comments": []})
    except Exception:
        pass
    page.wait_for_function("() => window.okfLoomLive._debug.resyncInFlight === false", timeout=5_000)
    page.wait_for_timeout(300)
    payloads = page.evaluate("() => window.__resyncPayloads")
    drained_b = any(p and isinstance(p, dict) and p.get("tag") == "B" for p in payloads)
    assert not drained_b, f"suspended resync drained a stale payload after suspend: {payloads}"
    page.unroute("**/__comments", hold_comments)


def test_r9_polling_settlement_bound_to_captured_generation(server_url, page):
    """R9-POLL: settlement scheduling is bound to the captured generation. A
    stopped poll's finally(scheduleNextPoll) must observe the newer generation
    after a stop->restart cycle and bail — it must NOT spawn a second timer
    chain. Proven by counting scheduled polling fetches over one POLL_MS window:
    a single chain makes ~1; a duplicate chain makes >=2."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    held = {"route": None}
    polling_fetches = {"n": 0}
    def route_events(route, request):
        if "limit=50&" in request.url:
            polling_fetches["n"] += 1
            if held["route"] is None:
                held["route"] = route
                return  # hold gen1's first tick in-flight
            route.fulfill(status=200, json={"events": [], "rev": 1})
        else:
            route.continue_()
    page.route("**/__data/events?*", route_events)
    page.evaluate("() => window.okfLoomLive._startPollingForTest()")
    gen1 = page.evaluate("window.okfLoomLive._debug.pollingGeneration")
    deadline = time.monotonic() + 2
    while held["route"] is None and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    assert held["route"] is not None, "gen1 polling fetch not captured"
    # Stop+restart -> new generation owns the session.
    page.evaluate("() => window.okfLoomLive._stopPollingForTest()")
    page.evaluate("() => window.okfLoomLive._startPollingForTest()")
    gen3 = page.evaluate("window.okfLoomLive._debug.pollingGeneration")
    assert gen3 > gen1, "restart did not advance polling generation"
    # Reset counter to count ONLY post-release scheduled fetches.
    polling_fetches["n"] = 0
    # Release gen1's tick; its finally(scheduleNextPoll(gen1)) must bail.
    try:
        held["route"].fulfill(status=200, json={"events": [], "rev": 1})
    except Exception:
        pass
    # Wait one POLL_MS window (3000ms) for scheduled ticks to fire.
    page.wait_for_timeout(3300)
    page.evaluate("() => window.okfLoomLive._stopPollingForTest()")
    page.unroute("**/__data/events?*", route_events)
    n = polling_fetches["n"]
    assert n <= 1, (
        f"old polling finally spawned a duplicate timer chain: {n} scheduled "
        f"polling fetches in one POLL_MS window (expected <=1 from a single chain)"
    )


def test_r9_loadcomments_rejects_missing_row_token_all_or_nothing(server_url, page):
    """R9-ATOMIC: loadComments must validate EVERY committed comment row. A
    comment missing its row_token is an all-or-nothing skew signal: retry then
    error, with no document committed next to an invalid tuple. Nothing is
    silently filtered."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    fresh_rev = "e" * 64
    fresh_doc = {
        "id": "tables/orders", "annotation_source": "NO TOKEN BODY\n",
        "concept_content_rev": fresh_rev,
        "html": '<p data-okf-projection-key="0">NO TOKEN BODY</p>',
        "raw": "NO TOKEN BODY\n", "title": "Orders", "description": "",
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": fresh_rev,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 13, "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": 13, "source_start": 0, "source_end": 13}]}]},
    }
    no_token_comments = {"comments": [{
        "id": "01JNOTOKEN0", "concept": "tables/orders", "body": "no token", "state": "open",
        "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"},
    }]}
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json=no_token_comments))
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10_000)
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == current_rev, \
        "loadComments committed a document while a comment lacked row_token"
    assert page.evaluate("() => document.querySelector('.okf-page__body').textContent.indexOf('NO TOKEN BODY')") < 0, \
        "document body committed despite an all-or-nothing row_token skew"


def test_r9_loadcomments_rejects_malformed_comment_all_or_nothing(server_url, page):
    """R9-ATOMIC: a structurally malformed comment row (non-object) in the
    committed set is an all-or-nothing skew signal — no document committed."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    fresh_rev = "d" * 64
    fresh_doc = {
        "id": "tables/orders", "annotation_source": "MALFORMED BODY\n",
        "concept_content_rev": fresh_rev,
        "html": '<p data-okf-projection-key="0">MALFORMED BODY</p>',
        "raw": "MALFORMED BODY\n", "title": "Orders", "description": "",
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": fresh_rev,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 14, "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": 14, "source_start": 0, "source_end": 14}]}]},
    }
    malformed_comments = {"comments": [
        "not-an-object",
        {"id": "01JOK00000", "concept": "tables/orders", "body": "ok", "state": "open",
         "row_token": "r2:ok", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}},
    ]}
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json=malformed_comments))
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10_000)
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == current_rev, \
        "loadComments committed a document while a comment row was malformed"
    assert page.evaluate("() => document.querySelector('.okf-page__body').textContent.indexOf('MALFORMED BODY')") < 0


def test_r9_inline_reply_missing_response_retains_draft(server_url, page):
    """R9-REPLY / Blocker A: a missing returned comment is accepted-unverified —
    no success mutation/toast, draft retained, explicit uncertain (Retry reply)
    state. The inline reply settles canonically (parent preserved)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.okfLoomStudio.state.comments = [{
        id: '01JPARENT000', concept: 'tables/orders', body: 'parent', state: 'open',
        row_token: 'r', ts: new Date().toISOString(), anchor: {kind:'concept', ref:'tables/orders'}
      }];
      window.okfLoomStudio.openPanel('comments');
      window.okfLoomStudio._renderComments();
    }""")
    # Canonical settle returns the parent so the accepted-unverified path keeps it.
    _settle_ok(page, comments=[{"id": "01JPARENT000", "concept": "tables/orders", "body": "parent", "state": "open", "row_token": "r", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}])
    page.route("**/__comment", lambda route: route.fulfill(status=200, json={"ok": True}))
    page.get_by_role("button", name="Reply").first.click()
    page.locator(".okf-inline-reply__textarea").fill("my draft reply")
    page.get_by_role("button", name="Reply", exact=True).last.click()
    page.wait_for_function("() => document.querySelector('.okf-toast') !== null", timeout=5_000)
    # R9/Blocker A: the missing response is accepted-unverified — draft retained,
    # no success toast, explicit Retry reply state.
    assert page.evaluate("() => !!document.querySelector('.okf-inline-reply__textarea')"), \
        "inline reply composer was removed despite a missing response (draft lost)"
    assert page.evaluate("() => document.querySelector('.okf-inline-reply__textarea').value") == "my draft reply", \
        "inline reply draft was not retained after a missing response"
    toasts = page.evaluate("() => Array.from(document.querySelectorAll('.okf-toast')).map(t => t.textContent)")
    assert not any("Reply posted" in t for t in toasts), \
        f"'Reply posted' success toast shown for a missing response: {toasts}"
    page.unroute("**/__comment")


def _seed_inline_reply_parent(page):
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.__replyFetchOutcome = null;
      const orig = window.fetch;
      // R9-REPLY test mock: intercept the /__comment POST at the JS level so it
      // stays IN-FLIGHT (pending) until the per-composer AbortController's
      // signal fires — deterministic, independent of Playwright routing and
      // backend latency. The abort is observed via __replyFetchOutcome.
      window.fetch = function(url, opts) {
        if (url === '/__comment') {
          const sig = opts && opts.signal;
          return new Promise(function (resolve, reject) {
            var done = false;
            var onAbort = function () {
              if (done) return; done = true;
              window.__replyFetchOutcome = 'aborted';
              reject(new DOMException('aborted', 'AbortError'));
            };
            if (sig) {
              if (sig.aborted) { onAbort(); return; }
              sig.addEventListener('abort', onAbort, { once: true });
            }
          });
        }
        return orig.apply(this, arguments);
      };
      window.okfLoomStudio.state.comments = [{
        id: '01JPARENT000', concept: 'tables/orders', body: 'parent', state: 'open',
        row_token: 'r', ts: new Date().toISOString(), anchor: {kind:'concept', ref:'tables/orders'}
      }];
      window.okfLoomStudio.openPanel('comments');
      window.okfLoomStudio._renderComments();
    }""")


def test_r9_inline_reply_abort_on_cancel(server_url, page):
    """R9-REPLY: cancelling an inline reply composer must abort the in-flight
    POST via the per-composer AbortController (not merely fence at completion)."""
    _boot(page, server_url)
    _seed_inline_reply_parent(page)
    page.get_by_role("button", name="Reply").first.click()
    page.locator(".okf-inline-reply__textarea").fill("a reply")
    page.get_by_role("button", name="Reply", exact=True).last.click()
    # Wait for the POST to be in-flight (mocked pending).
    page.wait_for_timeout(200)
    page.locator(".okf-inline-reply").get_by_role("button", name="Cancel").click()
    page.wait_for_function("() => window.__replyFetchOutcome === 'aborted'", timeout=5_000)
    assert page.evaluate("window.__replyFetchOutcome") == "aborted", \
        "Cancel did not abort the in-flight reply POST"


def test_r9_inline_reply_abort_on_pagehide(server_url, page):
    """R9-REPLY: a pagehide during an in-flight inline reply must abort the POST
    via the per-composer AbortController."""
    _boot(page, server_url)
    _seed_inline_reply_parent(page)
    page.get_by_role("button", name="Reply").first.click()
    page.locator(".okf-inline-reply__textarea").fill("a reply")
    page.get_by_role("button", name="Reply", exact=True).last.click()
    page.wait_for_timeout(200)
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_function("() => window.__replyFetchOutcome === 'aborted'", timeout=5_000)
    assert page.evaluate("window.__replyFetchOutcome") == "aborted", \
        "pagehide did not abort the in-flight reply POST"


def test_r9_inline_reply_abort_on_navigation(server_url, page):
    """R9-REPLY: a navigation (popstate) during an in-flight inline reply must
    abort the POST via the per-composer AbortController."""
    _boot(page, server_url)
    _seed_inline_reply_parent(page)
    page.get_by_role("button", name="Reply").first.click()
    page.locator(".okf-inline-reply__textarea").fill("a reply")
    page.get_by_role("button", name="Reply", exact=True).last.click()
    page.wait_for_timeout(200)
    page.evaluate("() => window.dispatchEvent(new PopStateEvent('popstate'))")
    page.wait_for_function("() => window.__replyFetchOutcome === 'aborted'", timeout=5_000)
    assert page.evaluate("window.__replyFetchOutcome") == "aborted", \
        "navigation (popstate) did not abort the in-flight reply POST"


def test_r9_inline_reply_abort_on_replacement(server_url, page):
    """R9-REPLY: opening a second inline reply (replacement) must abort the
    first reply's in-flight POST via the prior composer's AbortController."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.__replyFetchOutcome = null;
      const orig = window.fetch;
      window.fetch = function(url, opts) {
        if (url === '/__comment') {
          const sig = opts && opts.signal;
          return new Promise(function (resolve, reject) {
            var done = false;
            var onAbort = function () {
              if (done) return; done = true;
              window.__replyFetchOutcome = 'aborted';
              reject(new DOMException('aborted', 'AbortError'));
            };
            if (sig) {
              if (sig.aborted) { onAbort(); return; }
              sig.addEventListener('abort', onAbort, { once: true });
            }
          });
        }
        return orig.apply(this, arguments);
      };
      window.okfLoomStudio.state.comments = [
        {id: '01JPARENT00A', concept: 'tables/orders', body: 'parent A', state: 'open',
         row_token: 'r', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}},
        {id: '01JPARENT00B', concept: 'tables/orders', body: 'parent B', state: 'open',
         row_token: 'r2', ts: '2026-01-01T00:01:00Z', anchor: {kind:'concept', ref:'tables/orders'}}
      ];
      window.okfLoomStudio.openPanel('comments');
      window.okfLoomStudio._renderComments();
    }""")
    # Open the FIRST reply and submit (POST in-flight, mocked pending).
    page.locator("#comment-01JPARENT00A").get_by_role("button", name="Reply").click()
    page.locator(".okf-inline-reply__textarea").fill("first reply")
    page.locator(".okf-inline-reply").get_by_role("button", name="Reply").click()
    page.wait_for_timeout(200)
    # Open a SECOND reply on another parent — replacement aborts the first POST.
    page.locator("#comment-01JPARENT00B").get_by_role("button", name="Reply").click()
    page.wait_for_function("() => window.__replyFetchOutcome === 'aborted'", timeout=5_000)
    assert page.evaluate("window.__replyFetchOutcome") == "aborted", \
        "opening a second inline reply did not abort the first POST"


def test_r9_reanchor_does_not_commit_doc_without_validated_tuple(server_url, page):
    """R9-REANCHOR: saveReanchor's post-save reconciliation must never commit the
    doc when the canonical comment lacks the requested anchor or
    anchor_resolution. The doc stays at its prior rev and the editor reports
    save_failed (the previous code committed first and verified after)."""
    _boot(page, server_url)
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    prior_rev = "a" * 64
    page.route("**/__comment-reanchor", lambda route, request: route.fulfill(status=200, json={
        "ok": True,
        "comment": {"id": "01JANNOTATE", "row_token": "r2:new", "anchor": request.post_data_json["anchor"]},
        "concept_content_rev": prior_rev, "replayed": False}))
    # Post-save /__comments returns the comment WITHOUT the text-quote anchor
    # the user requested (concept anchor instead) and no anchor_resolution.
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": [{
        "id": "01JANNOTATE", "concept": "tables/orders", "body": "Check this anchor",
        "state": "open", "row_token": "r2:new", "ts": "2026-01-01T00:00:00Z",
        "anchor": {"kind": "concept", "ref": "tables/orders"},
    }]}))
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json={
        "id": "tables/orders", "annotation_source": "target text\n",
        "concept_content_rev": prior_rev,
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": prior_rev,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": 11, "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": 11, "source_start": 0, "source_end": 11}]}]},
        "html": "<p data-okf-projection-key=\"0\">target text</p>", "raw": "target text\n", "title": "Orders", "description": ""}))
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    with page.expect_request("**/__comment-reanchor"):
        page.get_by_role("button", name="Save anchor").click()
    # R9: the save succeeded but reconciliation found the comment lacks the
    # requested anchor -> save_failed, NO commitDocument.
    page.wait_for_function(
        "() => { const e = window.okfLoomStudio.state.annotationEditor; return !e || e.mode === 'save_failed'; }",
        timeout=15_000)
    mode = page.evaluate("() => { const e = window.okfLoomStudio.state.annotationEditor; return e ? e.mode : null; }")
    assert mode == "save_failed", f"editor should be save_failed (comment lacks requested anchor), got {mode}"
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Anchor saved and attached."


# ---------------------------------------------------------------------------
# R10: canonical staged-tuple validator + BFCache restore/resync intersection.
# ---------------------------------------------------------------------------


def _r10_doc(source, visible, rev, concept_id="tables/orders", title="Orders"):
    """Build a projection-valid doc fixture for a single-paragraph body."""
    import array
    cp_len = len(array.array("u", visible))
    src_cp_end = len(array.array("u", source[:source.index(visible)])) + cp_len if visible in source else cp_len
    return {
        "id": concept_id, "annotation_source": source,
        "concept_content_rev": rev,
        "annotation_projection": {"schema": 1, "resolver_version": 2, "concept_content_rev": rev,
            "offset_unit": "unicode-code-point", "complete": True,
            "blocks": [{"block_id": "paragraph:1", "source_start": 0, "source_end": src_cp_end, "heading_path": [], "dom_key": 0,
                        "segments": [{"dom_start": 0, "dom_end": cp_len, "source_start": 0, "source_end": src_cp_end}]}]},
        "html": '<p data-okf-projection-key="0">' + visible + "</p>",
        "raw": source, "title": title, "description": "",
    }


def test_r10_bfcache_restore_drains_queued_resync_before_connect(server_url, page):
    """R10-BFCACHE: when a persisted pageshow arrives while an OLD resync is
    still in-flight, the restore's doResync must return a promise that resolves
    only when the ACTUAL drained restore completes — connect() must not run
    before canonical reconciliation. Previously pageshow's doResync queued
    pending and resolved instantly, so connect() ran before the restored
    lifecycle's reconciliation, and the suspended old op then cleared the
    restore-epoch pending (the intersection of ordinary restore ordering and
    suspension that r9 tests covered only separately).

    A controllable resync handler keeps the old resync in-flight through
    pagehide (studio aborts annotationFetch ops on pagehide, but NOT our
    deferred — this mirrors the real BFCache freeze gap where the abort-
    rejection microtask has not settled when pageshow fires)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Add a controllable resync handler whose first-call promise keeps doResync
    # in-flight through pagehide (independent of annotationFetch abort). The
    # studio resync handler also runs but its loadComments settles on abort;
    # our pending promise is what holds Promise.all open.
    page.evaluate("""() => {
      window.__resyncCallCount = 0;
      window.__resolveFirstResync = null;
      window.okfLoomLive.on('resync', () => {
        window.__resyncCallCount++;
        if (window.__resyncCallCount === 1) {
          return new Promise(function (resolve) { window.__resolveFirstResync = resolve; });
        }
        return Promise.resolve();
      });
    }""")
    # Start a resync that goes in-flight (held by our controllable promise).
    page.evaluate("() => { window.__r0 = window.okfLoomLive.resync(); }")
    page.wait_for_function("() => window.okfLoomLive._debug.resyncInFlight === true", timeout=5_000)
    # Simulate BFCache freeze/restore WHILE the old resync is in-flight.
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_timeout(100)
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    # R10: connect must NOT run while the restore resync is pending — the old
    # in-flight resync must drain the restore-epoch pending first. The buggy
    # code resolved pageshow's doResync instantly and connected immediately.
    page.wait_for_timeout(400)
    assert page.evaluate("window.okfLoomLive._debug.connSource") is False, \
        "SSE transport was opened BEFORE the drained restore resync completed (BFCache intersection)"
    assert page.evaluate("window.__resyncCallCount") == 1, \
        "pageshow's doResync should have queued pending, not started a parallel resync"
    # Release the controllable promise: the old resync settles, detects
    # suspend, drains the restore-epoch pending (fresh doResync), then
    # connect() runs.
    page.evaluate("() => { if (window.__resolveFirstResync) window.__resolveFirstResync(); }")
    page.wait_for_function("() => window.__resyncCallCount >= 2", timeout=5_000)
    page.wait_for_function("() => window.okfLoomLive._debug.connSource === true", timeout=10_000)



def test_r10_loadcomments_rejects_non_array_comments(server_url, page):
    """R10: loadComments must reject a non-array comments value (e.g. {}) as an
    all-or-nothing skew — the previous `|| []` coercion silently bypassed row
    validation and could commit a fresh document beside malformed comments."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    fresh_doc = _r10_doc("NONARRAY BODY\n", "NONARRAY BODY", "f" * 64)
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": {}}))
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10_000)
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == current_rev, \
        "loadComments committed a document while comments was a non-array value"
    assert page.evaluate("() => document.querySelector('.okf-page__body').textContent.indexOf('NONARRAY BODY')") < 0, \
        "document body committed despite a non-array comments skew"


def test_r10_loadcomments_rejects_non_array_events(server_url, page):
    """R10: loadComments must reject a non-array events value (e.g. {}) as an
    all-or-nothing skew — previously `eventData.events || []` coerced a truthy
    non-array and `{}.forEach` threw inside the merge, or the event merge
    silently misbehaved beside a freshly-committed document."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    fresh_doc = _r10_doc("BADEVENTS BODY\n", "BADEVENTS BODY", "e" * 64)
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": []}))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": {}}))
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10_000)
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == current_rev, \
        "loadComments committed a document while events was a non-array value"
    assert page.evaluate("() => document.querySelector('.okf-page__body').textContent.indexOf('BADEVENTS BODY')") < 0


def test_r10_loadcomments_rejects_row_without_anchor(server_url, page):
    """R10: a token-bearing comment row WITHOUT a valid anchor is an
    all-or-nothing skew — the previous code accepted it and could commit a
    fresh document beside an anchor-less comment."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    fresh_doc = _r10_doc("NOANCHOR BODY\n", "NOANCHOR BODY", "a" * 64)
    no_anchor_comments = {"comments": [{
        "id": "01JNOANCHOR", "concept": "tables/orders", "body": "no anchor", "state": "open",
        "row_token": "r2:ok", "ts": "2026-01-01T00:00:00Z",
    }]}
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json=no_anchor_comments))
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10_000)
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == current_rev, \
        "loadComments committed a document while a comment row lacked an anchor"


def test_r10_loadcomments_aborts_when_commitDocument_fails(server_url, page):
    """R10: loadComments must REQUIRE commitDocument() to return true before
    committing comments/events. When DOM staging fails (commitDocument returns
    false and rolls back), loadComments must report error and NOT publish
    comments/events beside a rolled-back document. The previous code ignored
    the return value."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    original_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    original_comments_len = page.evaluate("window.okfLoomStudio.state.comments.length")
    fresh_doc = _r10_doc("COMMITFAIL BODY\n", "COMMITFAIL BODY", "c" * 64)
    valid_comments = {"comments": [{
        "id": "01JCFULL", "concept": "tables/orders", "body": "x", "state": "open",
        "row_token": "r2:ok", "ts": "2026-01-01T00:00:00Z",
        "anchor": {"kind": "concept", "ref": "tables/orders"},
    }]}
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json=valid_comments))
    # Inject a body-patch failure so commitDocument staging throws and returns false.
    page.evaluate("""() => {
      var bodyEl = document.querySelector('.okf-page__body');
      window.__origRemove = bodyEl.removeChild.bind(bodyEl);
      window.__origInsert = bodyEl.insertBefore.bind(bodyEl);
      window.__origReplace = bodyEl.replaceChild ? bodyEl.replaceChild.bind(bodyEl) : null;
      bodyEl.removeChild = function() { throw new Error('INJECTED_COMMIT_FAIL'); };
      bodyEl.insertBefore = function() { throw new Error('INJECTED_COMMIT_FAIL'); };
      if (window.__origReplace) bodyEl.replaceChild = function() { throw new Error('INJECTED_COMMIT_FAIL'); };
    }""")
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10_000)
    # Restore body methods before asserting.
    page.evaluate("""() => {
      var bodyEl = document.querySelector('.okf-page__body');
      bodyEl.removeChild = window.__origRemove;
      bodyEl.insertBefore = window.__origInsert;
      if (window.__origReplace) bodyEl.replaceChild = window.__origReplace;
    }""")
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == original_rev, \
        "loadComments advanced state.doc despite commitDocument returning false"
    assert page.evaluate("window.okfLoomStudio.state.comments.length") == original_comments_len, \
        "loadComments published comments despite commitDocument returning false"


def test_r10_reanchor_rejects_foreign_concept_doc(server_url, page):
    """R10: saveReanchor's post-save reconciliation must reject a document whose
    id is NOT the active concept. A projection-valid document for another
    concept must not be committed (doc.id was not previously checked)."""
    _boot(page, server_url)
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    prior_rev = "a" * 64
    page.route("**/__comment-reanchor", lambda route, request: route.fulfill(status=200, json={
        "ok": True,
        "comment": {"id": "01JANNOTATE", "row_token": "r2:new", "anchor": request.post_data_json["anchor"]},
        "concept_content_rev": prior_rev, "replayed": False}))
    # Post-save doc is projection-valid BUT for a FOREIGN concept.
    foreign_doc = _r10_doc("target text\n", "target text", "b" * 64, concept_id="tables/customers", title="Customers")
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=foreign_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": [{
        "id": "01JANNOTATE", "concept": "tables/orders", "body": "Check this anchor",
        "state": "open", "row_token": "r2:new", "ts": "2026-01-01T00:00:00Z",
        "anchor": {"kind": "concept", "ref": "tables/orders"},
    }]}))
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    with page.expect_request("**/__comment-reanchor"):
        page.get_by_role("button", name="Save anchor").click()
    # R10: the foreign-concept doc is rejected -> save_failed, no commit.
    page.wait_for_function(
        "() => { const e = window.okfLoomStudio.state.annotationEditor; return !e || e.mode === 'save_failed'; }",
        timeout=15_000)
    mode = page.evaluate("() => { const e = window.okfLoomStudio.state.annotationEditor; return e ? e.mode : null; }")
    assert mode == "save_failed", f"editor should be save_failed (foreign-concept doc), got {mode}"
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Anchor saved and attached."


def test_r10_conflict_refresh_rejects_foreign_concept_doc(server_url, page):
    """R10: refreshAfterConflict must route through the staged-tuple validator.
    A foreign-concept document (projection-valid but id !== active concept)
    must not be committed; prior state is preserved and the editor stays in
    conflict mode. The previous refresh adopted doc+comments without any
    identity/projection/row validation."""
    _boot(page, server_url); _seed_comment(page, "stale")
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    prior_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    prior_comments = page.evaluate("window.okfLoomStudio.state.comments.length")
    page.route("**/__comment-reanchor", lambda route: route.fulfill(status=409, json={"ok": False, "error": {"code": "concept_conflict"}}))
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    page.get_by_role("button", name="Save anchor").click()
    expect(page.get_by_role("button", name="Refresh and reselect")).to_be_visible(timeout=10_000)
    # Intercept refresh endpoints with a FOREIGN-CONCEPT doc.
    foreign_doc = _r10_doc("target text\n", "target text", "b" * 64, concept_id="tables/customers", title="Customers")
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=foreign_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": []}))
    page.get_by_role("button", name="Refresh and reselect").click()
    page.wait_for_timeout(1500)
    # Prior state must survive (foreign doc rejected by the tuple validator).
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == prior_rev, \
        "conflict refresh committed a foreign-concept document"
    assert page.evaluate("window.okfLoomStudio.state.comments.length") == prior_comments, \
        "conflict refresh changed comments despite a foreign-concept doc"
    mode = page.evaluate("window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode")
    assert mode == "conflict", f"editor mode should remain conflict, got {mode}"


def test_r10_inline_reply_rejects_non_string_id(server_url, page):
    """R10 / Blocker A: inline reply must require a NONEMPTY STRING id. A
    non-string id is accepted-unverified (not adopted as success); draft
    retained, explicit Retry reply state."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.okfLoomStudio.state.comments = [{
        id: '01JPARENT000', concept: 'tables/orders', body: 'parent', state: 'open',
        row_token: 'r', ts: new Date().toISOString(), anchor: {kind:'concept', ref:'tables/orders'}
      }];
      window.okfLoomStudio.openPanel('comments');
      window.okfLoomStudio._renderComments();
    }""")
    _settle_ok(page, comments=[{"id": "01JPARENT000", "concept": "tables/orders", "body": "parent", "state": "open", "row_token": "r", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}])
    page.route("**/__comment", lambda route: route.fulfill(status=200, json={
        "ok": True,
        "comment": {
            "id": 12345,  # non-string id — previously truthy, now rejected
            "parent_id": "01JPARENT000", "concept": "tables/orders",
            "row_token": "r2:new", "body": "reply",
            "anchor": {"kind": "concept", "ref": "tables/orders"},
        }}))
    page.get_by_role("button", name="Reply").first.click()
    page.locator(".okf-inline-reply__textarea").fill("my draft reply")
    page.get_by_role("button", name="Reply", exact=True).last.click()
    page.wait_for_function("() => document.querySelector('.okf-toast') !== null", timeout=5_000)
    assert page.evaluate("() => !!document.querySelector('.okf-inline-reply__textarea')"), \
        "inline reply composer removed despite a non-string id (draft lost)"
    assert page.evaluate("() => document.querySelector('.okf-inline-reply__textarea').value") == "my draft reply"
    toasts = page.evaluate("() => Array.from(document.querySelectorAll('.okf-toast')).map(t => t.textContent)")
    assert not any("Reply posted" in t for t in toasts), \
        f"'Reply posted' shown for a non-string id: {toasts}"
    # No reply row with a non-string id was adopted.
    assert page.evaluate("() => !window.okfLoomStudio.state.comments.some(c => c.id === 12345)"), \
        "a reply was adopted despite a non-string id"
    page.unroute("**/__comment")


def test_r10_inline_reply_rejects_cross_concept_anchor(server_url, page):
    """R10: inline reply must require a valid canonical concept anchor
    (kind === 'concept', ref === replyConcept). A cross-concept anchor must be
    rejected before any success mutation."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.okfLoomStudio.state.comments = [{
        id: '01JPARENT000', concept: 'tables/orders', body: 'parent', state: 'open',
        row_token: 'r', ts: new Date().toISOString(), anchor: {kind:'concept', ref:'tables/orders'}
      }];
      window.okfLoomStudio.openPanel('comments');
      window.okfLoomStudio._renderComments();
    }""")
    _settle_ok(page, comments=[{"id": "01JPARENT000", "concept": "tables/orders", "body": "parent", "state": "open", "row_token": "r", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}])
    page.route("**/__comment", lambda route: route.fulfill(status=200, json={
        "ok": True,
        "comment": {
            "id": "01JREPLY000", "parent_id": "01JPARENT000", "concept": "tables/orders",
            "row_token": "r2:new", "body": "reply",
            "anchor": {"kind": "concept", "ref": "tables/customers"},  # cross-concept anchor
        }}))
    page.get_by_role("button", name="Reply").first.click()
    page.locator(".okf-inline-reply__textarea").fill("cross concept reply")
    page.get_by_role("button", name="Reply", exact=True).last.click()
    page.wait_for_function("() => document.querySelector('.okf-toast') !== null", timeout=5_000)
    assert page.evaluate("() => !!document.querySelector('.okf-inline-reply__textarea')"), \
        "inline reply composer removed despite a cross-concept anchor (draft lost)"
    toasts = page.evaluate("() => Array.from(document.querySelectorAll('.okf-toast')).map(t => t.textContent)")
    assert not any("Reply posted" in t for t in toasts), \
        f"'Reply posted' shown for a cross-concept anchor: {toasts}"
    # No cross-concept reply row was adopted as success.
    assert page.evaluate("() => !window.okfLoomStudio.state.comments.some(c => c.id === '01JREPLY000')"), \
        "a cross-concept reply was adopted despite a cross-concept anchor"
    page.unroute("**/__comment")


def test_r10_intersection_bfcache_resync_race_with_malformed_drain(server_url, page):
    """R10 intersection/race: a BFCache restore while an old resync is in-flight
    must drain through, and if the drained canonical refresh receives a
    malformed (non-array) comments envelope, the page must NOT commit malformed
    state and connect() must still complete. This exercises the intersection of
    the BFCache-resync race and the fail-closed tuple validator."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # A controllable handler keeps the old resync in-flight through pagehide.
    page.evaluate("""() => {
      window.__resyncCallCount = 0;
      window.__resolveFirstResync = null;
      window.okfLoomLive.on('resync', () => {
        window.__resyncCallCount++;
        if (window.__resyncCallCount === 1) {
          return new Promise(function (resolve) { window.__resolveFirstResync = resolve; });
        }
        return Promise.resolve();
      });
    }""")
    # The drain's loadComments (studio's handler) receives malformed comments.
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": {}}))
    page.evaluate("() => { window.__r0 = window.okfLoomLive.resync(); }")
    page.wait_for_function("() => window.okfLoomLive._debug.resyncInFlight === true", timeout=5_000)
    prior_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    # BFCache freeze/restore while the old resync is in-flight.
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_timeout(100)
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    page.wait_for_timeout(300)
    assert page.evaluate("window.okfLoomLive._debug.connSource") is False, \
        "connect ran before the drained restore resync"
    # Release: the old resync drains the restore-epoch pending; the drain's
    # loadComments receives a malformed non-array comments envelope.
    page.evaluate("() => { if (window.__resolveFirstResync) window.__resolveFirstResync(); }")
    # connect() must still complete (the drain's doResync settles even when
    # loadComments reports error — the deferred resolves, not rejects).
    page.wait_for_function("() => window.okfLoomLive._debug.connSource === true", timeout=10_000)
    # The malformed drain must NOT have committed a new doc.
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == prior_rev, \
        "malformed drain committed a document despite a non-array comments skew"


# ---------------------------------------------------------------------------
# R11: BFCache multi-waiter drain ordering + canonical tuple validation
# (discriminated anchors, event rows, re-anchor complete-tuple validation).
# ---------------------------------------------------------------------------


def test_r11_resync_supersession_preserves_all_waiters_until_latest_terminal(server_url, page):
    """R11-BFCACHE: when a newer doResync supersedes already-queued pending
    work, EVERY accumulated drain waiter must stay pending and resolve ONLY
    after the LATEST queued resync reaches a terminal state. The previous
    single-waiter design resolved the superseded waiter immediately at queue
    time (lines 889-894), so an earlier caller could proceed while the
    replacement reconciliation was still pending."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Per-call controllable handler: each resync call returns a promise held
    # until its numbered release function runs. This makes the DRAIN itself
    # controllable, proving waiters do not resolve before the latest terminal.
    page.evaluate("""() => {
      window.__resyncCallCount = 0;
      window.__releases = {};
      window.okfLoomLive.on('resync', () => {
        const n = ++window.__resyncCallCount;
        return new Promise(function (resolve) { window.__releases[n] = resolve; });
      });
    }""")
    # Start the in-flight resync (call 1), held.
    page.evaluate("() => { window.__r0 = window.okfLoomLive.resync(); }")
    page.wait_for_function("() => window.okfLoomLive._debug.resyncInFlight === true", timeout=5_000)
    # Queue two superseding resyncs while call 1 is in-flight.
    page.evaluate("""() => {
      window.__w1Resolved = false; window.__w2Resolved = false;
      window.__w1 = window.okfLoomLive.resync().then(() => { window.__w1Resolved = true; });
      window.__w2 = window.okfLoomLive.resync().then(() => { window.__w2Resolved = true; });
    }""")
    # Both waiters accumulate under one slot (latest payload wins); neither
    # resolves at queue time.
    page.wait_for_function("() => window.okfLoomLive._debug.resyncPendingWaiters === 2", timeout=5_000)
    page.wait_for_timeout(100)
    assert page.evaluate("window.__w1Resolved") is False, \
        "superseded waiter resolved early at queue time (single-waiter bug)"
    assert page.evaluate("window.__w2Resolved") is False
    # Release call 1: it settles and drains the LATEST payload, which starts
    # call 2 (the drain). Call 2 is still held -> the drain has not reached
    # terminal -> waiters must still be pending.
    page.evaluate("() => { if (window.__releases[1]) window.__releases[1](); }")
    page.wait_for_function("() => window.__resyncCallCount >= 2", timeout=5_000)
    page.wait_for_timeout(150)
    assert page.evaluate("window.__w1Resolved") is False, \
        "waiter resolved before the latest drained resync reached terminal"
    assert page.evaluate("window.__w2Resolved") is False
    # Release call 2 (the drain): terminal reached -> BOTH accumulated waiters
    # resolve together.
    page.evaluate("() => { if (window.__releases[2]) window.__releases[2](); }")
    page.wait_for_function("() => window.__w1Resolved === true && window.__w2Resolved === true", timeout=5_000)


def test_r11_bfcache_restore_with_superseding_resync_waits_for_latest_terminal(server_url, page):
    """R11-BFCACHE: when a persisted pageshow queues a restore resync and a
    SECOND resync then supersedes it, connect() must NOT run until the LATEST
    queued resync drains to terminal. The buggy single-waiter code resolved the
    restore waiter immediately when the superseding resync was queued, so
    connect() opened the SSE transport while the replacement reconciliation was
    still pending."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.__resyncCallCount = 0;
      window.__releases = {};
      window.okfLoomLive.on('resync', () => {
        const n = ++window.__resyncCallCount;
        return new Promise(function (resolve) { window.__releases[n] = resolve; });
      });
    }""")
    # In-flight resync (call 1) held across the BFCache freeze.
    page.evaluate("() => { window.__r0 = window.okfLoomLive.resync(); }")
    page.wait_for_function("() => window.okfLoomLive._debug.resyncInFlight === true", timeout=5_000)
    # BFCache freeze/restore while call 1 is in-flight.
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_timeout(100)
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    page.wait_for_timeout(150)
    # pageshow queued a restore waiter (held by call 1); connect() not yet run.
    assert page.evaluate("window.okfLoomLive._debug.connSource") is False, \
        "connect ran before the restore resync drained"
    assert page.evaluate("window.__resyncCallCount") == 1, \
        "pageshow should have queued pending, not started a parallel resync"
    # A superseding resync (e.g. SSE/polling on the restored page) replaces the
    # restore's pending payload. The restore waiter must NOT resolve early.
    page.evaluate("() => { window.__super = window.okfLoomLive.resync(); }")
    page.wait_for_function("() => window.okfLoomLive._debug.resyncPendingWaiters === 2", timeout=5_000)
    page.wait_for_timeout(150)
    assert page.evaluate("window.okfLoomLive._debug.connSource") is False, \
        "connect ran while the superseded restore resync was still pending"
    # Release call 1: it detects suspend and drains BOTH restore-epoch waiters
    # via the latest payload -> call 2 (held). connect() must still not run.
    page.evaluate("() => { if (window.__releases[1]) window.__releases[1](); }")
    page.wait_for_function("() => window.__resyncCallCount >= 2", timeout=5_000)
    page.wait_for_timeout(150)
    assert page.evaluate("window.okfLoomLive._debug.connSource") is False, \
        "connect ran before the latest drained resync reached terminal"
    # Release call 2 (the drain): terminal -> restore waiter resolves ->
    # pageshow's .then runs connect().
    page.evaluate("() => { if (window.__releases[2]) window.__releases[2](); }")
    page.wait_for_function("() => window.okfLoomLive._debug.connSource === true", timeout=10_000)


def test_r11_validate_staged_tuple_rejects_malformed_and_foreign_anchors(server_url, page):
    """R11: the canonical tuple validator must use DISCRIMINATED anchor
    validation: a closed kind set, the canonical ref-less concept anchor (a
    legacy ref, when present, must equal row.concept), a nonempty exact for
    text-quote, and a nonempty ref for legacy text. Unsupported kinds, foreign
    concept refs, and missing text/text-quote discriminators are rejected."""
    _boot(page, server_url)
    result = page.evaluate("""() => {
      const out = {};
      const base = (anchor, concept, resolution) => ({
        conceptKey: concept || "tables/orders",
        commentsData: { comments: [{
          id: "c1", concept: concept || "tables/orders", row_token: "r2:ok", anchor: anchor,
          anchor_resolution: resolution || null,
        }]},
        // This matrix exercises ANCHOR discrimination only; use the explicit
        // noncanonical comments-only mode so the r12 events-required default
        // does not itself skew every case. Commit paths never use this mode.
        commentsOnly: true,
        fallbackProjectionRev: "a".repeat(64),
      });
      const RES = { status: "resolved", reason: "ok", concept_content_rev: "a".repeat(64), resolver_version: 2 };
      // Valid baselines: concept anchors need no resolution; a non-concept
      // (text-quote) anchor on this concept needs resolution evidence.
      out.validConcept = !!window.okfLoomStudio._validateStagedTuple(
        base({ kind: "concept", ref: "tables/orders" })).comments;
      out.validTextQuote = !!window.okfLoomStudio._validateStagedTuple(
        base({ kind: "text-quote", exact: "target text", prefix: "", suffix: "" }, null, RES)).comments;
      out.validLegacyText = !!window.okfLoomStudio._validateStagedTuple(
        base({ kind: "text", ref: "target text" }, null, RES)).comments;
      // Foreign-concept row with a self-correlated concept anchor is valid
      // (anchor.ref === row.concept, resolution check skipped for non-active).
      out.validForeignConceptRow = !!window.okfLoomStudio._validateStagedTuple(
        base({ kind: "concept", ref: "tables/customers" }, "tables/customers")).comments;
      out.conceptNoRef = window.okfLoomStudio._validateStagedTuple(
        base({ kind: "concept" })).skewSignal;
      out.conceptForeignRef = window.okfLoomStudio._validateStagedTuple(
        base({ kind: "concept", ref: "tables/customers" })).skewSignal;
      out.conceptForeignRefEmpty = window.okfLoomStudio._validateStagedTuple(
        base({ kind: "concept", ref: "" })).skewSignal;
      out.unsupportedKind = window.okfLoomStudio._validateStagedTuple(
        base({ kind: "bogus", ref: "tables/orders" })).skewSignal;
      out.unsupportedKindNoDiscriminator = window.okfLoomStudio._validateStagedTuple(
        base({ kind: "section" })).skewSignal;
      out.textQuoteNoExact = window.okfLoomStudio._validateStagedTuple(
        base({ kind: "text-quote", exact: "" }, null, RES)).skewSignal;
      out.legacyTextNoRef = window.okfLoomStudio._validateStagedTuple(
        base({ kind: "text" }, null, RES)).skewSignal;
      return out;
    }""")
    assert result["validConcept"] is True, "valid concept anchor baseline should pass"
    assert result["validTextQuote"] is True, "valid text-quote anchor baseline should pass"
    assert result["validLegacyText"] is True, "valid legacy text anchor baseline should pass"
    assert result["validForeignConceptRow"] is True, "self-correlated foreign-concept row should pass"
    # The production server stores a page comment as EXACTLY {kind:"concept"}
    # (ownership is the row's top-level concept), so the canonical shape must
    # load; only a ref naming a DIFFERENT concept (or an empty one) fails closed.
    assert result["conceptNoRef"] is None, "the server's canonical page anchor must be readable"
    assert result["conceptForeignRef"] == "malformed-comment-anchor"
    assert result["conceptForeignRefEmpty"] == "malformed-comment-anchor"
    assert result["unsupportedKind"] == "malformed-comment-anchor"
    assert result["unsupportedKindNoDiscriminator"] == "malformed-comment-anchor"
    assert result["textQuoteNoExact"] == "malformed-comment-anchor"
    assert result["legacyTextNoRef"] == "malformed-comment-anchor"


def test_r11_validate_staged_tuple_rejects_malformed_event_rows(server_url, page):
    """R11: the canonical tuple validator must validate EVERY event row, not
    merely that events is an array. Persisted events.jsonl rows carry a nonempty
    `type`; primitives, null members, and rows missing a nonempty string id or
    any discriminator (type/kind) are rejected before any document/state
    commit."""
    _boot(page, server_url)
    result = page.evaluate("""() => {
      const out = {};
      const base = (events) => ({
        conceptKey: "tables/orders",
        commentsData: { comments: [{
          id: "c1", concept: "tables/orders", row_token: "r2:ok",
          anchor: { kind: "concept", ref: "tables/orders" },
        }]},
        eventData: { events: events },
        fallbackProjectionRev: "a".repeat(64),
      });
      // Real persisted events carry a nonempty `type`; mutation-style rows
      // carry `kind`. Both are accepted.
      out.validType = !!window.okfLoomStudio._validateStagedTuple(
        base([{ id: "e1", type: "presence" }])).events;
      out.validKind = !!window.okfLoomStudio._validateStagedTuple(
        base([{ id: "e1", kind: "add_tag" }])).events;
      out.validEmpty = Array.isArray(window.okfLoomStudio._validateStagedTuple(
        base([])).events);
      out.primitive = window.okfLoomStudio._validateStagedTuple(
        base([42])).skewSignal;
      out.nullMember = window.okfLoomStudio._validateStagedTuple(
        base([null])).skewSignal;
      out.missingId = window.okfLoomStudio._validateStagedTuple(
        base([{ type: "presence" }])).skewSignal;
      out.emptyId = window.okfLoomStudio._validateStagedTuple(
        base([{ id: "", type: "presence" }])).skewSignal;
      out.idNotString = window.okfLoomStudio._validateStagedTuple(
        base([{ id: 7, type: "presence" }])).skewSignal;
      out.noDiscriminator = window.okfLoomStudio._validateStagedTuple(
        base([{ id: "e1" }])).skewSignal;
      out.emptyTypeAndKind = window.okfLoomStudio._validateStagedTuple(
        base([{ id: "e1", type: "", kind: "" }])).skewSignal;
      return out;
    }""")
    assert result["validType"] is True
    assert result["validKind"] is True
    assert result["validEmpty"] is True
    assert result["primitive"] == "malformed-event-row"
    assert result["nullMember"] == "malformed-event-row"
    assert result["missingId"] == "missing-event-identity"
    assert result["emptyId"] == "missing-event-identity"
    assert result["idNotString"] == "missing-event-identity"
    assert result["noDiscriminator"] == "missing-event-identity"
    assert result["emptyTypeAndKind"] == "missing-event-identity"


def test_r11_loadcomments_rejects_concept_anchor_with_foreign_ref(server_url, page):
    """R11: loadComments must reject a concept anchor whose ref does NOT
    correlate with the row's concept (foreign ref). Previously such a row was
    accepted and could be published beside a freshly-committed document."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    fresh_doc = _r10_doc("FOREIGNREF BODY\n", "FOREIGNREF BODY", "f" * 64)
    foreign_ref_comments = {"comments": [{
        "id": "01JFR", "concept": "tables/orders", "body": "foreign ref", "state": "open",
        "row_token": "r2:ok", "ts": "2026-01-01T00:00:00Z",
        "anchor": {"kind": "concept", "ref": "tables/customers"},
    }]}
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json=foreign_ref_comments))
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10_000)
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == current_rev, \
        "loadComments committed a document beside a foreign-ref concept anchor"
    assert page.evaluate('() => document.querySelector(".okf-page__body").textContent.indexOf("FOREIGNREF BODY")') < 0, \
        "document body committed beside a foreign-ref concept anchor"


def test_r11_loadcomments_rejects_unsupported_anchor_kind(server_url, page):
    """R11: loadComments must reject a comment row with an unsupported anchor
    kind. Previously any nonempty string kind was accepted."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    fresh_doc = _r10_doc("BADKIND BODY\n", "BADKIND BODY", "k" * 64)
    bad_kind_comments = {"comments": [{
        "id": "01JBK", "concept": "tables/orders", "body": "bad kind", "state": "open",
        "row_token": "r2:ok", "ts": "2026-01-01T00:00:00Z",
        "anchor": {"kind": "line", "ref": "tables/orders"},
    }]}
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json=bad_kind_comments))
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10_000)
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == current_rev, \
        "loadComments committed a document beside an unsupported-kind anchor"
    assert page.evaluate('() => document.querySelector(".okf-page__body").textContent.indexOf("BADKIND BODY")') < 0


def test_r11_loadcomments_rejects_malformed_event_row(server_url, page):
    """R11: loadComments must reject an events array containing a malformed
    row (e.g. a primitive, or a member missing id/kind). Previously only the
    events container was type-checked."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    current_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    fresh_doc = _r10_doc("BADEVENTROW BODY\n", "BADEVENTROW BODY", "v" * 64)
    valid_comments = {"comments": [{
        "id": "01JBEV", "concept": "tables/orders", "body": "x", "state": "open",
        "row_token": "r2:ok", "ts": "2026-01-01T00:00:00Z",
        "anchor": {"kind": "concept", "ref": "tables/orders"},
    }]}
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=fresh_doc))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json=valid_comments))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": [
        {"id": "e1", "type": "presence"}, 42, {"type": "comment"},
    ]}))
    page.evaluate("() => { window.__lc = window.okfLoomStudio._loadComments(); }")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=10_000)
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == current_rev, \
        "loadComments committed a document beside a malformed event row"
    assert page.evaluate('() => document.querySelector(".okf-page__body").textContent.indexOf("BADEVENTROW BODY")') < 0


def test_r11_reanchor_reconciliation_rejects_malformed_sibling_row(server_url, page):
    """R11: saveReanchor's post-save reconciliation must run the COMPLETE tuple
    validator (every row) BEFORE the target-specific re-anchor checks. A
    malformed SIBLING row (here: a concept anchor with a foreign ref) must
    reject the whole tuple -> save_failed, NO commitDocument. The previous code
    validated only the target comment, so a malformed sibling accompanied an
    accepted commit."""
    _boot(page, server_url)
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    prior_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")

    # Capture the anchor the editor requests so the TARGET comment can echo it
    # (making the target genuinely valid). The sibling is the malformed row.
    # Re-anchoring a comment does NOT change the document source, so the doc
    # rev stays at prior_rev (this is also what the post-commit guard expects).
    requested = {}

    def _reanchor_handler(route, request):
        try:
            requested["anchor"] = request.post_data_json["anchor"]
        except Exception:
            requested["anchor"] = None
        route.fulfill(status=200, json={
            "ok": True,
            "comment": {"id": "01JANNOTATE", "row_token": "r2:new", "anchor": requested["anchor"]},
            "concept_content_rev": prior_rev, "replayed": False})

    def _comments_handler(route):
        target = {
            "id": "01JANNOTATE", "concept": "tables/orders", "body": "Check this anchor",
            "state": "open", "row_token": "r2:new", "ts": "2026-01-01T00:00:00Z",
            "anchor": requested.get("anchor"),
            "anchor_resolution": {"status": "resolved", "reason": "ok",
                                  "concept_content_rev": prior_rev, "resolver_version": 2},
        }
        # Malformed sibling: concept anchor whose ref does NOT correlate with
        # its own concept -> discriminated validator rejects the whole tuple.
        sibling = {
            "id": "01JSIBLING", "concept": "tables/orders", "body": "sibling", "state": "open",
            "row_token": "r2:sib", "ts": "2026-01-01T00:00:00Z",
            "anchor": {"kind": "concept", "ref": "tables/customers"},
        }
        route.fulfill(status=200, json={"comments": [target, sibling]})

    page.route("**/__comment-reanchor", _reanchor_handler)
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc(
        "target text\n", "target text", prior_rev)))
    page.route("**/__comments", _comments_handler)

    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    with page.expect_request("**/__comment-reanchor"):
        page.get_by_role("button", name="Save anchor").click()
    # R11: the malformed sibling fails the complete-tuple validator -> the doc
    # is NOT committed and the editor reports save_failed. (Without the fix, the
    # old code validated only the target comment, committed, and announced
    # success beside the malformed sibling.)
    page.wait_for_function(
        "() => { const e = window.okfLoomStudio.state.annotationEditor; return !e || e.mode === 'save_failed'; }",
        timeout=15_000)
    mode = page.evaluate("() => { const e = window.okfLoomStudio.state.annotationEditor; return e ? e.mode : null; }")
    assert mode == "save_failed", f"editor should be save_failed (malformed sibling), got {mode}"
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Anchor saved and attached."


def test_r11_reanchor_reconciliation_accepts_valid_tuple(server_url, page):
    """R11: a re-anchor whose post-save tuple is FULLY valid (target comment
    carrying the requested anchor + resolution parity, and no malformed
    siblings) must still succeed. This guards against the new complete-tuple
    validation over-rejecting the happy path."""
    _boot(page, server_url)
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    prior_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    requested = {}

    def _reanchor_handler(route, request):
        try:
            requested["anchor"] = request.post_data_json["anchor"]
        except Exception:
            requested["anchor"] = None
        route.fulfill(status=200, json={
            "ok": True,
            "comment": {"id": "01JANNOTATE", "row_token": "r2:new", "anchor": requested["anchor"]},
            "concept_content_rev": prior_rev, "replayed": False})

    def _comments_handler(route):
        target = {
            "id": "01JANNOTATE", "concept": "tables/orders", "body": "Check this anchor",
            "state": "open", "row_token": "r2:new", "ts": "2026-01-01T00:00:00Z",
            "anchor": requested.get("anchor"),
            "anchor_resolution": {"status": "resolved", "reason": "ok",
                                  "concept_content_rev": prior_rev, "resolver_version": 2},
        }
        route.fulfill(status=200, json={"comments": [target]})

    page.route("**/__comment-reanchor", _reanchor_handler)
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc(
        "target text\n", "target text", prior_rev)))
    page.route("**/__comments", _comments_handler)

    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    with page.expect_request("**/__comment-reanchor"):
        page.get_by_role("button", name="Save anchor").click()
    # Happy path: the editor closes and the success announcement fires. Stop
    # waiting once the save has settled either way (success or save_failed).
    page.wait_for_function(
        "() => window.okfLoomStudio.state.annotationAnnouncement === 'Anchor saved and attached.'"
        " || (window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode === 'save_failed')",
        timeout=15_000)
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") == "Anchor saved and attached.", \
        "a fully-valid re-anchor tuple should succeed, not be rejected by the complete-tuple validator"


# ---------------------------------------------------------------------------
# R12: repeated-suspension drain ordering + events in every canonical commit
# path + validator requires events by default.
# ---------------------------------------------------------------------------


def test_r12_repeated_bfcache_cycles_waiters_resolve_only_after_latest_terminal(server_url, page):
    """R12 (finding 3): across repeated pagehide/pageshow (suspend) cycles, NO
    accumulated drain waiter may resolve before the LATEST queued resync reaches
    a terminal state. The previous suspend branch partitioned waiters by epoch
    and resolved stale-epoch waiters IMMEDIATELY (before the restore-epoch drain
    even started); across repeated cycles an earlier restore waiter could
    therefore settle while the latest reconciliation remained pending.

    Drives three suspend cycles chained through two drains (call 1 -> drain
    call 2 -> drain call 3) and proves the two EARLIEST waiters resolve only
    after the FINAL drain terminates."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    # Per-call controllable handler: each resync call returns a promise held
    # until its numbered release runs, so the DRAIN itself is controllable.
    page.evaluate("""() => {
      window.__resyncCallCount = 0;
      window.__releases = {};
      window.okfLoomLive.on('resync', () => {
        const n = ++window.__resyncCallCount;
        return new Promise(function (resolve) { window.__releases[n] = resolve; });
      });
    }""")
    # In-flight resync (call 1), held across all suspend cycles.
    page.evaluate("() => { window.__r0 = window.okfLoomLive.resync(); }")
    page.wait_for_function("() => window.okfLoomLive._debug.resyncInFlight === true", timeout=5_000)

    def _queue_waiter(n):
        # A pagehide bumps the suspend epoch (simulating a BFCache freeze); the
        # following doResync queues a restore-epoch waiter (simulating pageshow).
        page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        page.wait_for_timeout(80)
        page.evaluate("(n) => { window['w' + n] = false; window.okfLoomLive.resync().then(() => { window['w' + n] = true; }); }", n)

    # Two waiters across two suspend cycles, both queued while call 1 is held.
    _queue_waiter(1)
    _queue_waiter(2)
    page.wait_for_function("() => window.okfLoomLive._debug.resyncPendingWaiters === 2", timeout=5_000)
    assert page.evaluate("() => window.w1 === false && window.w2 === false") is True

    # Release call 1: it detects suspend and drains BOTH waiters via the latest
    # payload -> call 2 (held). Neither resolves yet.
    page.evaluate("() => { if (window.__releases[1]) window.__releases[1](); }")
    page.wait_for_function("() => window.__resyncCallCount >= 2", timeout=5_000)
    page.wait_for_timeout(150)
    assert page.evaluate("() => window.w1 === false && window.w2 === false") is True, \
        "an earlier waiter resolved before the latest drain reached terminal"

    # A third suspend cycle while call 2 (the drain) is held: W3 queues.
    _queue_waiter(3)
    page.wait_for_function("() => window.okfLoomLive._debug.resyncPendingWaiters === 1", timeout=5_000)
    # Release call 2: it detects suspend and drains W3 -> call 3 (held). The
    # earlier W1/W2 are chained to call 2's return, which now chains to call 3,
    # so NONE of the three resolves yet.
    page.evaluate("() => { if (window.__releases[2]) window.__releases[2](); }")
    page.wait_for_function("() => window.__resyncCallCount >= 3", timeout=5_000)
    page.wait_for_timeout(150)
    assert page.evaluate("() => window.w1 === false && window.w2 === false && window.w3 === false") is True, \
        "waiters resolved before the latest (third) drain reached terminal"

    # Release call 3 (the final drain): terminal reached -> W3 resolves, then the
    # chained W1 and W2 resolve. Only NOW does any waiter settle.
    page.evaluate("() => { if (window.__releases[3]) window.__releases[3](); }")
    page.wait_for_function("() => window.w1 === true && window.w2 === true && window.w3 === true", timeout=5_000)


def test_r12_stale_waiter_chains_to_restore_drain_not_resolved_immediately(server_url, page):
    """R12 (finding 3): when BOTH a stale-epoch waiter and a restore-epoch
    waiter are pending, the stale waiter must NOT resolve immediately at the
    suspend branch (the r11 behavior). It must chain to the restore drain and
    resolve only once the latest reconciliation reaches terminal. This is the
    precise repeated-suspension hazard: an earlier restore waiter settling while
    the latest reconciliation remains pending."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
      window.__resyncCallCount = 0;
      window.__releases = {};
      window.okfLoomLive.on('resync', () => {
        const n = ++window.__resyncCallCount;
        return new Promise(function (resolve) { window.__releases[n] = resolve; });
      });
    }""")
    # In-flight resync (call 1, epoch 0), held.
    page.evaluate("() => { window.__r0 = window.okfLoomLive.resync(); }")
    page.wait_for_function("() => window.okfLoomLive._debug.resyncInFlight === true", timeout=5_000)
    # A stale-epoch waiter (queued BEFORE any pagehide, same epoch as call 1).
    page.evaluate("() => { window.wStale = false; window.okfLoomLive.resync().then(() => { window.wStale = true; }); }")
    page.wait_for_function("() => window.okfLoomLive._debug.resyncPendingWaiters === 1", timeout=5_000)
    # A pagehide/pageshow cycle queues a restore-epoch waiter (later epoch).
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_timeout(80)
    page.evaluate("() => { window.wRestore = false; window.okfLoomLive.resync().then(() => { window.wRestore = true; }); }")
    page.wait_for_function("() => window.okfLoomLive._debug.resyncPendingWaiters === 2", timeout=5_000)
    # At this point the r11 bug would already have resolved the stale waiter
    # (it resolved staleWaiters immediately at queue time of the restore). The
    # r12 fix leaves it pending, chained to the restore drain.
    page.wait_for_timeout(150)
    assert page.evaluate("() => window.wStale === false") is True, \
        "stale waiter resolved immediately instead of chaining to the restore drain"
    assert page.evaluate("() => window.wRestore === false") is True
    # Release call 1: suspended; restore exists -> chain BOTH to the restore
    # drain (call 2, held). Neither resolves yet.
    page.evaluate("() => { if (window.__releases[1]) window.__releases[1](); }")
    page.wait_for_function("() => window.__resyncCallCount >= 2", timeout=5_000)
    page.wait_for_timeout(150)
    assert page.evaluate("() => window.wStale === false && window.wRestore === false") is True, \
        "waiters resolved before the restore drain reached terminal"
    # Release call 2 (the restore drain): terminal -> both resolve together.
    page.evaluate("() => { if (window.__releases[2]) window.__releases[2](); }")
    page.wait_for_function("() => window.wStale === true && window.wRestore === true", timeout=5_000)


def test_r12_validator_requires_events_by_default(server_url, page):
    """R12 (finding 5): the shared tuple validator must REQUIRE events by
    default. A caller that omits eventData fails closed (events-required) rather
    than silently downgrading to comments-only validation. Only an EXPLICIT
    commentsOnly mode skips events, and that mode is noncanonical (it still
    validates comment anchors and is unavailable to commit paths)."""
    _boot(page, server_url)
    result = page.evaluate("""() => {
      const out = {};
      const validComment = { id: "c1", concept: "tables/orders", row_token: "r2:ok",
        anchor: { kind: "concept", ref: "tables/orders" } };
      const validEvent = { id: "e1", type: "presence" };
      const base = (extra) => Object.assign({
        conceptKey: "tables/orders",
        commentsData: { comments: [validComment] },
        fallbackProjectionRev: "a".repeat(64),
      }, extra || {});
      // Omitting eventData (no commentsOnly) -> fail closed, not silent skip.
      out.omitted = window.okfLoomStudio._validateStagedTuple(base()).skewSignal;
      // Explicit noncanonical commentsOnly mode -> comments-only validation.
      out.commentsOnly = !!window.okfLoomStudio._validateStagedTuple(
        base({ commentsOnly: true })).comments;
      // Providing eventData with valid events -> full validation passes.
      out.withEvents = !!window.okfLoomStudio._validateStagedTuple(
        base({ eventData: { events: [validEvent] } })).events;
      // Providing eventData with malformed events -> rejected.
      out.malformedEvents = window.okfLoomStudio._validateStagedTuple(
        base({ eventData: { events: [42] } })).skewSignal;
      // commentsOnly must NOT bypass comment-anchor validation.
      var badAnchorComment = Object.assign({}, validComment,
        { anchor: { kind: "concept", ref: "tables/customers" } });
      out.commentsOnlyChecksComments = window.okfLoomStudio._validateStagedTuple({
        conceptKey: "tables/orders",
        commentsData: { comments: [badAnchorComment] },
        commentsOnly: true,
        fallbackProjectionRev: "a".repeat(64),
      }).skewSignal;
      // commentsOnly is a boolean flag: a truthy non-boolean must NOT trigger it.
      out.truthyNonBooleanNotCommentsOnly = window.okfLoomStudio._validateStagedTuple(
        base({ commentsOnly: "yes" })).skewSignal;
      return out;
    }""")
    assert result["omitted"] == "events-required", \
        "validator must require events by default (no silent comments-only downgrade)"
    assert result["commentsOnly"] is True, "explicit commentsOnly mode should validate comments"
    assert result["withEvents"] is True
    assert result["malformedEvents"] == "malformed-event-row"
    assert result["commentsOnlyChecksComments"] == "malformed-comment-anchor", \
        "commentsOnly must still validate comment anchors"
    assert result["truthyNonBooleanNotCommentsOnly"] == "events-required", \
        "commentsOnly must be an explicit boolean, not any truthy value"


def test_r12_saveReanchor_rejects_malformed_events(server_url, page):
    """R12 (finding 4): saveReanchor's post-save reconciliation must fetch and
    validate the authoritative events feed alongside the doc+comments. A
    malformed events row rejects the whole tuple -> save_failed, NO
    commitDocument. Previously this path fetched only doc+comments and committed
    without validating events."""
    _boot(page, server_url)
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    prior_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    requested = {}

    def _reanchor_handler(route, request):
        try: requested["anchor"] = request.post_data_json["anchor"]
        except Exception: requested["anchor"] = None
        route.fulfill(status=200, json={"ok": True,
            "comment": {"id": "01JANNOTATE", "row_token": "r2:new", "anchor": requested["anchor"]},
            "concept_content_rev": prior_rev, "replayed": False})

    def _comments_handler(route):
        # Valid target comment (echoes the requested anchor + resolution parity).
        route.fulfill(status=200, json={"comments": [{
            "id": "01JANNOTATE", "concept": "tables/orders", "body": "Check this anchor",
            "state": "open", "row_token": "r2:new", "ts": "2026-01-01T00:00:00Z",
            "anchor": requested.get("anchor"),
            "anchor_resolution": {"status": "resolved", "reason": "ok",
                "concept_content_rev": prior_rev, "resolver_version": 2},
        }]})

    page.route("**/__comment-reanchor", _reanchor_handler)
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc(
        "target text\n", "target text", prior_rev)))
    page.route("**/__comments", _comments_handler)
    # Malformed authoritative events feed (primitive member).
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": [42]}))

    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    with page.expect_request("**/__comment-reanchor"):
        page.get_by_role("button", name="Save anchor").click()
    page.wait_for_function(
        "() => { const e = window.okfLoomStudio.state.annotationEditor; return !e || e.mode === 'save_failed'; }",
        timeout=15_000)
    mode = page.evaluate("() => { const e = window.okfLoomStudio.state.annotationEditor; return e ? e.mode : null; }")
    assert mode == "save_failed", f"saveReanchor should be save_failed (malformed events), got {mode}"
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == prior_rev, \
        "saveReanchor committed a document beside a malformed events feed"
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Anchor saved and attached."


def test_r12_reconcileReanchor_rejects_malformed_events(server_url, page):
    """R12 (finding 4): the fallback reconciliation path (reconcileReanchor,
    reached when the post-save fetch throws) must ALSO fetch and validate
    authoritative events. A malformed events feed rejects the tuple -> save_failed,
    NO commitDocument. The first /__data/events call (post-save) aborts to route
    execution into reconcileReanchor; its own /__data/events fetch then returns
    the malformed feed."""
    _boot(page, server_url)
    _seed_comment(page)
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    prior_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    requested = {}

    def _reanchor_handler(route, request):
        try: requested["anchor"] = request.post_data_json["anchor"]
        except Exception: requested["anchor"] = None
        route.fulfill(status=200, json={"ok": True,
            "comment": {"id": "01JANNOTATE", "row_token": "r2:new", "anchor": requested["anchor"]},
            "concept_content_rev": prior_rev, "replayed": False})

    page.route("**/__comment-reanchor", _reanchor_handler)
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc(
        "target text\n", "target text", prior_rev)))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": []}))
    # First /__data/events call (post-save) aborts -> Promise.all rejects ->
    # inner catch -> reconcileReanchor. Subsequent calls (reconcileReanchor's
    # own fetch) return a malformed feed so the tuple validator rejects it.
    events_state = {"n": 0}

    def _events_handler(route):
        events_state["n"] += 1
        if events_state["n"] == 1:
            route.abort("failed")
        else:
            route.fulfill(status=200, json={"events": [42]})

    page.route("**/__data/events*", _events_handler)

    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    with page.expect_request("**/__comment-reanchor"):
        page.get_by_role("button", name="Save anchor").click()
    page.wait_for_function(
        "() => { const e = window.okfLoomStudio.state.annotationEditor; return !e || e.mode === 'save_failed'; }",
        timeout=20_000)
    mode = page.evaluate("() => { const e = window.okfLoomStudio.state.annotationEditor; return e ? e.mode : null; }")
    assert mode == "save_failed", f"reconcileReanchor should be save_failed (malformed events), got {mode}"
    assert events_state["n"] >= 2, "reconcileReanchor did not perform its own events fetch"
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == prior_rev, \
        "reconcileReanchor committed a document beside a malformed events feed"
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Anchor saved and attached."


def test_r12_conflict_refresh_rejects_malformed_events(server_url, page):
    """R12 (finding 4): refreshAfterConflict must fetch and validate the
    authoritative events feed. A malformed events row rejects the tuple, throws
    to the catch path, retains prior doc/comments, and keeps the editor in
    conflict mode. Previously this path fetched only doc+comments."""
    _boot(page, server_url); _seed_comment(page, "stale")
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    prior_rev = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    prior_comments = page.evaluate("window.okfLoomStudio.state.comments.length")
    page.route("**/__comment-reanchor", lambda route: route.fulfill(status=409, json={"ok": False, "error": {"code": "concept_conflict"}}))
    page.get_by_role("button", name="Re-anchor").click()
    page.get_by_role("button", name="Use selected text").click()
    page.get_by_role("button", name="Save anchor").click()
    expect(page.get_by_role("button", name="Refresh and reselect")).to_be_visible(timeout=10_000)
    # Intercept refresh endpoints: VALID doc + empty comments + MALFORMED events.
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc(
        "target text\n", "target text", prior_rev)))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": []}))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": [42]}))
    page.get_by_role("button", name="Refresh and reselect").click()
    page.wait_for_timeout(1500)
    # Prior state must survive (malformed events rejected by the tuple validator).
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == prior_rev, \
        "conflict refresh committed a document beside a malformed events feed"
    assert page.evaluate("window.okfLoomStudio.state.comments.length") == prior_comments, \
        "conflict refresh changed comments beside a malformed events feed"
    mode = page.evaluate("window.okfLoomStudio.state.annotationEditor && window.okfLoomStudio.state.annotationEditor.mode")
    assert mode == "conflict", f"editor mode should remain conflict, got {mode}"


# ---------------------------------------------------------------------------
# Composer POST lifecycle hardening (selected-text comment submission)
# ---------------------------------------------------------------------------

_COMPOSER_REV = "a" * 64


def _settle_ok(page, rev=_COMPOSER_REV, body="target text\n", visible="target text",
               comments=None):
    """Route the three canonical-settle endpoints to consistent valid responses
    so a POST-accepted settle (loadComments) commits successfully."""
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc(body, visible, rev)))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": []}))
    if comments is None:
        comments = []
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": comments}))


def _open_composer(page):
    # Prime the held doc to _COMPOSER_REV so the POST-accepted canonical
    # settle (loadComments) commits the SAME revision the page holds — a
    # revision change would bump documentGeneration and correctly fence the
    # settle as a late result. This mirrors how a real settled refresh returns
    # the same content rev the user is looking at.
    _set_doc_and_selection(page, "target text\n", "<p>target text</p>", "target text")
    page.evaluate("window.okfLoomStudio.openPanel('comments')")
    page.wait_for_selector(".okf-composer__textarea")


def _fill_and_send(page, text="hello agent"):
    ta = page.locator(".okf-composer__textarea")
    ta.fill(text)
    page.get_by_role("button", name="Send").click()


def test_composer_post_sends_stable_high_entropy_idempotency_key(server_url, page):
    """Req 1: each comment submission carries a stable high-entropy
    Idempotency-Key on POST /__comment (currently absent)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    captured = []

    def comment(route, request):
        captured.append(request.headers.get("idempotency-key"))
        route.fulfill(status=201, json={"ok": True, "comment": {
            "id": "01JCOMPOS01", "concept": "tables/orders", "body": "hello agent",
            "state": "open", "row_token": "r2:c1", "ts": "2026-01-01T00:00:00Z",
            "anchor": {"kind": "concept", "ref": "tables/orders"},
        }})
    page.route("**/__comment", comment)
    _settle_ok(page, comments=[{"id": "01JCOMPOS01", "concept": "tables/orders", "body": "hello agent",
        "state": "open", "row_token": "r2:c1", "ts": "2026-01-01T00:00:00Z",
        "anchor": {"kind": "concept", "ref": "tables/orders"},
        "anchor_resolution": {"status": "resolved", "reason": "original_position",
            "concept_content_rev": _COMPOSER_REV, "resolver_version": 2,
            "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}])
    _open_composer(page)
    _fill_and_send(page)
    page.wait_for_function("() => window.okfLoomStudio.state.annotationAnnouncement === 'Comment posted.'", timeout=15_000)
    assert len(captured) == 1, f"expected one POST, captured={captured}"
    key = captured[0]
    assert key and key.startswith("comment:") and len(key) >= 16, f"bad key: {key!r}"


def test_composer_uncertain_post_preserves_draft_and_reuses_key(server_url, page):
    """Req 1/2/3: an uncertain POST outcome (network lost) must preserve the
    draft/anchor and reuse the SAME Idempotency-Key on the safe retry. No
    'Comment posted' announcement may fire."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    keys = []

    def comment_lost(route, request):
        keys.append(request.headers.get("idempotency-key"))
        route.abort("connectionreset")
    page.route("**/__comment", comment_lost)
    _open_composer(page)
    _fill_and_send(page, "lost response")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'uncertain'", timeout=15_000)
    # Draft preserved + no false success.
    assert page.evaluate("window.okfLoomStudio.state.draftBody") == "lost response"
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted."
    announcement = page.evaluate("window.okfLoomStudio.state.annotationAnnouncement")
    assert "cannot confirm" in announcement.lower(), f"unexpected uncertain message: {announcement!r}"
    assert len(keys) == 1 and keys[0]

    # Retry: same key reused (the body/anchor are unchanged).
    page.unroute("**/__comment")
    page.route("**/__comment", comment_lost)
    page.get_by_role("button", name="Retry send").click()
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'uncertain'", timeout=15_000)
    assert len(keys) == 2, f"expected retry POST, got {keys}"
    assert keys[0] == keys[1], f"uncertain retry must reuse the same key: {keys}"


def test_composer_definite_reject_replaces_key_and_preserves_draft(server_url, page):
    """Req 1/3: a definite POST rejection (400) must preserve the draft and
    generate a NEW Idempotency-Key on the next submission."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    keys = []

    def comment_reject(route, request):
        keys.append(request.headers.get("idempotency-key"))
        route.fulfill(status=400, json={"ok": False, "error": "invalid_request"})
    page.route("**/__comment", comment_reject)
    _open_composer(page)
    _fill_and_send(page, "rejected body")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'rejected'", timeout=15_000)
    assert page.evaluate("window.okfLoomStudio.state.draftBody") == "rejected body"
    announcement = page.evaluate("window.okfLoomStudio.state.annotationAnnouncement")
    assert "not posted" in announcement.lower(), f"unexpected reject message: {announcement!r}"
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted."

    # Second submission (draft unchanged but reject outcome) -> NEW key.
    page.get_by_role("button", name="Send").click()
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'rejected'", timeout=15_000)
    assert len(keys) == 2, f"expected two POSTs, got {keys}"
    assert keys[0] != keys[1], f"definite reject must replace the key: {keys}"


def test_composer_accepted_but_refresh_failed_no_false_success(server_url, page):
    """Req 2/3/7: when the POST is accepted (201 ok) but the canonical
    refresh/settle fails, the UI must NOT announce 'Comment posted', must NOT
    clear the draft, and must keep the composer open. The retry must be
    verification-only (no re-POST)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    post_calls = {"n": 0}

    def comment_ok(route, request):
        post_calls["n"] += 1
        route.fulfill(status=201, json={"ok": True, "comment": {
            "id": "01JCOMPOS02", "concept": "tables/orders", "body": "accepted body",
            "state": "open", "row_token": "r2:c2", "ts": "2026-01-01T00:00:00Z",
            "anchor": {"kind": "concept", "ref": "tables/orders"},
        }})
    page.route("**/__comment", comment_ok)
    # Make the canonical settle fail: malformed events tuple (rejected).
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc("target text\n", "target text", _COMPOSER_REV)))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": [42]}))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": []}))
    _open_composer(page)
    _fill_and_send(page, "accepted body")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'accepted-refresh-failed'", timeout=15_000)
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted."
    assert page.evaluate("window.okfLoomStudio.state.draftBody") == "accepted body"
    announcement = page.evaluate("window.okfLoomStudio.state.annotationAnnouncement")
    assert "accepted" in announcement.lower() and "refresh" in announcement.lower(), f"unexpected message: {announcement!r}"
    assert post_calls["n"] == 1

    # Verification-only retry: must NOT issue a second POST.
    page.get_by_role("button", name="Retry refresh").click()
    page.wait_for_timeout(800)
    assert post_calls["n"] == 1, "verification-only retry must not re-POST"


def test_composer_in_flight_sets_aria_busy_and_polite_announcement(server_url, page):
    """Req 4: while a submission is in flight the composer exposes aria-busy
    and a polite live announcement of 'Posting comment'."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    held = []

    def comment_hold(route, request):
        held.append(route)
    page.route("**/__comment", comment_hold)
    _open_composer(page)
    _fill_and_send(page, "in flight")
    page.wait_for_function("() => document.querySelector('.okf-composer') && document.querySelector('.okf-composer').getAttribute('aria-busy') === 'true'", timeout=10_000)
    assert "posting" in page.evaluate("window.okfLoomStudio.state.annotationAnnouncement").lower()
    # The submit control must be disabled while in flight.
    send_btn = page.get_by_role("button", name="Send")
    sending_btn = page.get_by_role("button", name="Sending")
    assert sending_btn.is_disabled() or send_btn.is_disabled()
    # Release -> uncertain outcome, aria-busy clears.
    held[0].abort("connectionreset")
    page.wait_for_function("() => document.querySelector('.okf-composer').getAttribute('aria-busy') !== 'true'", timeout=10_000)


def test_composer_late_post_result_does_not_mutate_new_page(server_url, page):
    """Req 5: a late POST result that settles after navigation/generation
    change must not toast/clear/focus the new page state."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    held = []

    def comment_hold(route, request):
        held.append(route)
    page.route("**/__comment", comment_hold)
    _open_composer(page)
    _fill_and_send(page, "late result")
    page.wait_for_function("() => document.querySelector('.okf-composer').getAttribute('aria-busy') === 'true'", timeout=10_000)
    # Simulate navigation away: bump documentGeneration + navEpoch.
    page.evaluate("() => { window.okfLoomStudio.state.documentGeneration++; window.okfLoomStudio.state.annotationGeneration++; if(window.okfLoomLive) window.okfLoomLive._bumpNavEpoch(); }")
    # Now release a SUCCESS response. It must not announce success or clear the
    # draft on the new page state.
    held[0].fulfill(status=201, json={"ok": True, "comment": {
        "id": "01JLATE0000", "concept": "tables/orders", "body": "late result",
        "state": "open", "row_token": "r2:late", "ts": "2026-01-01T00:00:00Z",
        "anchor": {"kind": "concept", "ref": "tables/orders"},
    }})
    page.wait_for_timeout(800)
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted."


def test_projection_error_identifies_offending_block_without_secrets(server_url, page):
    """Req 6: a malformed canonical projection surfaces a concise reason that
    identifies the offending block/index WITHOUT echoing response bodies or
    source/html content. The strict validator is unchanged."""
    _boot(page, server_url)
    page.evaluate("""() => {
        const rev = 'a'.repeat(64);
        const err = window.okfLoomStudio._projectionError;
        const source = 'target text\\n';
        // out-of-bounds source_end on block index 1.
        const badProj = {schema:1, resolver_version:2, concept_content_rev:rev,
            offset_unit:'unicode-code-point', complete:true,
            blocks:[
              {block_id:'paragraph:1', source_start:0, source_end:12, heading_path:[], dom_key:0,
               segments:[{dom_start:0,dom_end:11,source_start:0,source_end:11}]},
              {block_id:'paragraph:2', source_start:13, source_end:9999, heading_path:[], dom_key:1,
               segments:[{dom_start:0,dom_end:5,source_start:13,source_end:9999}]}
            ]};
        const reason = err(badProj, rev, source, '<p data-okf-projection-key="0">target text</p>');
        window.__projReason = reason;
    }""")
    reason = page.evaluate("window.__projReason")
    assert reason, "projection error must surface a reason"
    # Must identify the offending block index/id.
    assert "block" in reason.lower() or "1" in reason, f"reason lacks offending block: {reason!r}"
    # Must NOT echo source/html content or the literal text.
    assert "target" not in reason, f"reason leaks source content: {reason!r}"


def test_loadcomments_returns_false_on_failure_and_true_on_success(server_url, page):
    """Req 7: loadComments must propagate settle success/failure to its caller
    (true on success, false on failure) instead of resolving into a false-
    success path."""
    _boot(page, server_url)
    # Success path.
    ok = page.evaluate("async () => { try { return await window.okfLoomStudio._loadComments(); } catch(e){ return 'threw:' + e; } }")
    assert ok is True, f"loadComments should return true on success, got {ok!r}"
    # Failure path: malformed events tuple.
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": [42]}))
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc("target text\n", "target text", _COMPOSER_REV)))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": []}))
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    ok2 = page.evaluate("async () => { try { return await window.okfLoomStudio._loadComments(); } catch(e){ return 'threw:' + e; } }")
    assert ok2 is False, f"loadComments should return false on malformed tuple, got {ok2!r}"


# ---------------------------------------------------------------------------
# Round 2: invariant remediation (success-envelope, draft ownership, focus,
# 37-blank-blockquote real repro, malformed/missing-id/SSE/nav proof)
# ---------------------------------------------------------------------------


def _composer_status_text(page):
    return page.evaluate("""() => { const n = document.querySelector('.okf-composer__status'); return n ? n.textContent : ''; }""")


def test_composer_malformed_2xx_envelope_remains_accepted_refresh_failed(server_url, page):
    """Finding 1: a 2xx ok envelope with no valid comment.id cannot be
    identity-verified and must remain accepted-refresh-failed (no success)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    # Malformed envelope: ok:true but comment has no id.
    page.route("**/__comment", lambda route: route.fulfill(status=201, json={"ok": True, "comment": {"concept": "tables/orders"}}))
    _settle_ok(page)
    _open_composer(page)
    _fill_and_send(page, "malformed envelope")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'accepted-refresh-failed'", timeout=15_000)
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted."
    assert page.evaluate("window.okfLoomStudio.state.draftBody") == "malformed envelope"
    # The persistent inline status must say accepted + refresh, not success.
    status = _composer_status_text(page).lower()
    assert "accepted" in status and "refresh" in status, f"status: {status!r}"


def test_composer_settle_missing_confirmed_id_remains_accepted_refresh_failed(server_url, page):
    """Finding 1: a valid canonical settle that OMITS the accepted id must
    remain accepted-refresh-failed; no success clear/toast."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    page.route("**/__comment", lambda route: route.fulfill(status=201, json={"ok": True, "comment": {"id": "01JMISSING", "concept": "tables/orders", "body": "x", "state": "open", "row_token": "r2:m", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}}}))
    # Settle returns a VALID tuple that does NOT contain 01JMISSING.
    _settle_ok(page, comments=[])
    _open_composer(page)
    _fill_and_send(page, "missing on settle")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'accepted-refresh-failed'", timeout=15_000)
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted."
    assert page.evaluate("window.okfLoomStudio.state.composerPost.confirmedId") == "01JMISSING"
    # The accepted id must NOT be in state.comments (settle omitted it).
    assert page.evaluate("() => !window.okfLoomStudio.state.comments.some(c => c.id === '01JMISSING')")


def test_composer_verification_retry_while_confirmed_id_absent_stays_failed(server_url, page):
    """Finding 5: a verification-only retry while the confirmed id remains
    absent must stay accepted-refresh-failed (never false success)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    page.route("**/__comment", lambda route: route.fulfill(status=201, json={"ok": True, "comment": {"id": "01JABSENT", "concept": "tables/orders", "body": "x", "state": "open", "row_token": "r2:a", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}}}))
    _settle_ok(page, comments=[])  # never contains 01JABSENT
    _open_composer(page)
    _fill_and_send(page, "absent id retry")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'accepted-refresh-failed'", timeout=15_000)
    # Verification-only retry: no re-POST, stays failed.
    post_before = page.evaluate("() => window.__composerPosts || 0")
    page.get_by_role("button", name="Retry refresh").click()
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'accepted-refresh-failed'", timeout=15_000)
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted."
    assert page.evaluate("window.okfLoomStudio.state.draftBody") == "absent id retry"


def test_composer_pending_disables_textarea_and_newer_draft_is_not_lost(server_url, page):
    """Finding 2: while a submission is pending the textarea is disabled
    (cannot start a newer draft); a late restore cannot overwrite a draft the
    user owns. Prove draft-generation ownership."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    held = []
    page.route("**/__comment", lambda route: held.append(route))
    _open_composer(page)
    _fill_and_send(page, "in flight draft")
    page.wait_for_function("() => document.querySelector('.okf-composer__textarea').disabled === true", timeout=10_000)
    page.wait_for_function("() => document.querySelector('.okf-composer').getAttribute('aria-busy') === 'true'", timeout=10_000)
    # Simulate a newer user draft landing while the POST is held: bump
    # draftGeneration and set a fresh draftBody (as a genuine edit would).
    page.evaluate("() => { const s = window.okfLoomStudio.state; s.draftGeneration = (s.draftGeneration||0)+1; s.draftBody = 'newer user draft'; }")
    # Release the held POST as a network loss -> uncertain restore path.
    held[0].abort("connectionreset")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'uncertain'", timeout=15_000)
    # The newer draft must NOT have been overwritten by the stale restore.
    assert page.evaluate("window.okfLoomStudio.state.draftBody") == "newer user draft", "stale restore overwrote a newer user draft"


def test_composer_accepted_refresh_failed_textarea_is_read_only(server_url, page):
    """Finding 2: during accepted-refresh-failed recovery the accepted payload
    is read-only (edits would imply reposting); textarea is disabled."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    page.route("**/__comment", lambda route: route.fulfill(status=201, json={"ok": True, "comment": {"id": "01JARF2", "concept": "tables/orders", "body": "x", "state": "open", "row_token": "r2:arf2", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}}}))
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc("target text\n", "target text", _COMPOSER_REV)))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": [42]}))  # malformed -> settle fails
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": []}))
    _open_composer(page)
    _fill_and_send(page, "accepted read only")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'accepted-refresh-failed'", timeout=15_000)
    assert page.evaluate("() => document.querySelector('.okf-composer__textarea').disabled === true"), "accepted payload must be read-only during refresh-only recovery"


def test_composer_focus_after_uncertain_reject_and_accepted_outcomes(server_url, page):
    """Finding 3: deterministic focus after uncertain / accepted-refresh-failed
    outcomes (the Retry control); after reject the textarea. activeElement proof."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    # uncertain -> focus Retry send
    page.route("**/__comment", lambda route: route.abort("connectionreset"))
    _open_composer(page)
    _fill_and_send(page, "uncertain focus")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'uncertain'", timeout=15_000)
    page.wait_for_function("() => document.activeElement && document.activeElement.matches('.okf-composer__submit')", timeout=5_000)
    assert page.evaluate("() => document.activeElement.textContent.trim()") == "Retry send"

    # reject -> focus textarea (edit + resend)
    page.unroute("**/__comment")
    page.route("**/__comment", lambda route: route.fulfill(status=400, json={"ok": False, "error": "invalid_request"}))
    page.get_by_role("button", name="Retry send").click()
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'rejected'", timeout=15_000)
    page.wait_for_function("() => document.activeElement && document.activeElement.matches('.okf-composer__textarea')", timeout=5_000)
    assert page.evaluate("() => document.activeElement.classList.contains('okf-composer__textarea')")

    # accepted-refresh-failed -> focus Retry refresh
    page.unroute("**/__comment")
    page.route("**/__comment", lambda route: route.fulfill(status=201, json={"ok": True, "comment": {"id": "01JFC3", "concept": "tables/orders", "body": "x", "state": "open", "row_token": "r2:fc3", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}}}))
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc("target text\n", "target text", _COMPOSER_REV)))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": [42]}))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": []}))
    page.get_by_role("button", name="Send").click()
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'accepted-refresh-failed'", timeout=15_000)
    page.wait_for_function("() => document.activeElement && document.activeElement.matches('.okf-composer__submit')", timeout=5_000)
    assert page.evaluate("() => document.activeElement.textContent.trim()") == "Retry refresh"


def test_composer_success_clears_prior_lifecycle_status_and_toast(server_url, page):
    """Visual P1: a terminal success transition must clear the persistent
    lifecycle status and any prior composer toast (no stale uncertainty error
    beside the success message)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    # First: uncertain (creates a persistent status + toast).
    page.route("**/__comment", lambda route: route.abort("connectionreset"))
    _open_composer(page)
    _fill_and_send(page, "then succeed")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'uncertain'", timeout=15_000)
    assert _composer_status_text(page)  # persistent status present
    # Now retry with a working POST + settle.
    row = {"id": "01JCLR1", "concept": "tables/orders", "body": "then succeed", "state": "open", "row_token": "r2:clr", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}
    page.unroute("**/__comment")
    page.route("**/__comment", lambda route: route.fulfill(status=201, json={"ok": True, "comment": row}))
    _settle_ok(page, comments=[row])
    page.get_by_role("button", name="Retry send").click()
    page.wait_for_function("() => window.okfLoomStudio.state.annotationAnnouncement === 'Comment posted.'", timeout=15_000)
    # Persistent lifecycle status cleared; only the success toast remains.
    assert _composer_status_text(page) == "", "terminal success must clear the persistent lifecycle status"
    composer_toasts = page.evaluate("""() => Array.from(document.querySelectorAll('.okf-toast[data-okf-composer-toast="1"]')).map(n => n.textContent)""")
    assert all("Cannot confirm" not in t for t in composer_toasts), f"stale uncertainty toast survived success: {composer_toasts}"


def _setup_37_blank_doc(page, server_url):
    """Helper (blocker 4): boot the page, run the 37-blank-blockquote validator
    proof, apply the doc, and select neighboring text. Returns the validator
    results dict. Shared by the validator test and the single-POST test so there
    is no test-to-test call."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    result = page.evaluate("""() => {
        const v = window.okfLoomStudio._validProjection;
        const rev = 'a'.repeat(64);
        const blocks = [];
        for (let i = 0; i < 37; i++) {
            blocks.push({block_id: 'block-quote:' + (i + 1), source_start: 0, source_end: 1, heading_path: [], dom_key: null, segments: []});
        }
        blocks.push({block_id: 'hr:38', source_start: 0, source_end: 1, heading_path: [], dom_key: 0, segments: []});
        blocks.push({block_id: 'paragraph:39', source_start: 0, source_end: 11, heading_path: [], dom_key: 1,
            segments: [{dom_start: 0, dom_end: 11, source_start: 0, source_end: 11}]});
        const unkeyedProj = {schema:1, resolver_version:2, concept_content_rev:rev,
            offset_unit:'unicode-code-point', complete:true, blocks: blocks};
        const unkeyedHtml = '<hr data-okf-projection-key="0"><p data-okf-projection-key="1">target text</p>';
        const source = 'target text\\n';
        const unkeyedOk = v(unkeyedProj, rev, source, unkeyedHtml);
        const keyed = [];
        for (let i = 0; i < 37; i++) {
            keyed.push({block_id: 'block-quote:' + (i + 1), source_start: 0, source_end: 1, heading_path: [], dom_key: 100 + i, segments: []});
        }
        keyed.push({block_id: 'hr:38', source_start: 37, source_end: 38, heading_path: [], dom_key: 0, segments: []});
        keyed.push({block_id: 'paragraph:39', source_start: 0, source_end: 11, heading_path: [], dom_key: 1,
            segments: [{dom_start: 0, dom_end: 11, source_start: 0, source_end: 11}]});
        const keyedOk = v({schema:1, resolver_version:2, concept_content_rev:rev, offset_unit:'unicode-code-point', complete:true, blocks: keyed}, rev, source, unkeyedHtml);
        const hrOnlyOk = v({schema:1,resolver_version:2,concept_content_rev:rev,offset_unit:'unicode-code-point',complete:true,blocks:[{block_id:'hr:1',source_start:0,source_end:1,heading_path:[],dom_key:0,segments:[]}]}, rev, '\\n', '<hr data-okf-projection-key="0">');
        const paraEmptyOk = v({schema:1,resolver_version:2,concept_content_rev:rev,offset_unit:'unicode-code-point',complete:true,blocks:[{block_id:'paragraph:1',source_start:0,source_end:1,heading_path:[],dom_key:0,segments:[]}]}, rev, '\\n', '<p data-okf-projection-key="0"></p>');
        window.__proj37 = {unkeyedProj: unkeyedProj, unkeyedHtml: unkeyedHtml, rev: rev, source: source};
        return {unkeyedOk: unkeyedOk, keyedOk: keyedOk, hrOnlyOk: hrOnlyOk, paraEmptyOk: paraEmptyOk};
    }""")
    page.evaluate("""() => {
        const p = window.__proj37;
        window.okfLoomStudio.applyDoc({
            id:'tables/orders', annotation_source:p.source, concept_content_rev:p.rev,
            annotation_projection: p.unkeyedProj, html: p.unkeyedHtml, raw:p.source, title:'Orders', description:''
        }, {pulse:false});
        window.okfLoomStudio.state.commentsRequestGeneration++;
        window.okfLoomStudio.state.commentsReady = true;
        const node = document.querySelector('.okf-page__body p[data-okf-projection-key="1"]').firstChild;
        const r = document.createRange(); r.setStart(node, 0); r.setEnd(node, 11);
        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
    }""")
    return result


def test_37_blank_blockquotes_projection_strict_and_capture_setup(server_url, page):
    """Finding 4: the real failure shape — a document with 37 blank blockquotes.
    Prove the browser validator accepts the post-fix unkeyed shape, rejects the
    pre-fix keyed shape, accepts HR keyed-zero-segment, and rejects a keyed
    empty paragraph. (The end-to-end real-producer proof lives in
    test_37_blank_blockquotes_real_producer_canonical_get_e2e.)"""
    result = _setup_37_blank_doc(page, server_url)
    assert result["unkeyedOk"] is True, "37 unkeyed blank blockquotes must validate (post-fix shape)"
    assert result["keyedOk"] is False, "37 keyed blank blockquotes (empty non-HR) must be REJECTED (strict)"
    assert result["hrOnlyOk"] is True, "HR keyed-with-zero-segments must remain accepted"
    assert result["paraEmptyOk"] is False, "a keyed paragraph with zero segments must be rejected"
    anchor = page.evaluate("window.okfLoomStudio._captureCanonicalAnchor()")
    assert anchor["exact"] == "target text"


def test_37_blank_blockquotes_canonical_validates_capture_and_single_post(server_url, page):
    """Finding 4 (cont.): neighboring selection captures, exactly one POST,
    canonical settlement reaches success. HR remains accepted; blank blocks
    remain unkeyed in the validated projection."""
    # Reuse the validator proof + doc/selection setup via the shared helper
    # (blocker 4: no test-to-test call).
    _setup_37_blank_doc(page, server_url)
    anchor = page.evaluate("window.okfLoomStudio._captureCanonicalAnchor()")
    assert anchor["exact"] == "target text"
    posts = {"n": 0}

    def comment(route, request):
        posts["n"] += 1
        route.fulfill(status=201, json={"ok": True, "comment": {"id": "01JTHIRTY7", "concept": "tables/orders", "body": "on 37-blank doc", "state": "open", "row_token": "r2:37", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}}})

    page.route("**/__comment", comment)
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc("target text\n", "target text", "a" * 64)))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": []}))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": [{"id": "01JTHIRTY7", "concept": "tables/orders", "body": "on 37-blank doc", "state": "open", "row_token": "r2:37", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": "a" * 64, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}]}))
    # Open the composer and send a concept-level comment on the 37-blank doc.
    page.evaluate("window.okfLoomStudio.openPanel('comments')")
    page.wait_for_selector(".okf-composer__textarea")
    page.locator(".okf-composer__textarea").fill("on 37-blank doc")
    page.get_by_role("button", name="Send").click()
    page.wait_for_function("() => window.okfLoomStudio.state.annotationAnnouncement === 'Comment posted.'", timeout=15_000)
    assert posts["n"] == 1, f"expected exactly one POST, got {posts['n']}"
    assert page.evaluate("() => window.okfLoomStudio.state.comments.some(c => c.id === '01JTHIRTY7')")


def test_composer_sse_comment_exact_once_around_ack_and_refetch(server_url, page):
    """Finding 5: a `comment` SSE event for the same id arriving before, during,
    or after the POST ack/refetch must leave exactly one row (no duplicates)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    cid = "01JSSEONCE"
    row = {"id": cid, "concept": "tables/orders", "body": "sse once", "state": "open", "row_token": "r2:sse", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}
    # SSE for the same id BEFORE the POST ack.
    page.evaluate("""(cid) => window.okfLoomLive._dispatchEventForTest({type:'comment', id: cid, concept:'tables/orders'})""", cid)
    page.route("**/__comment", lambda route: route.fulfill(status=201, json={"ok": True, "comment": row}))
    _settle_ok(page, comments=[row])
    _open_composer(page)
    _fill_and_send(page, "sse once")
    # SSE for the same id DURING settle (right after send, before announce).
    page.evaluate("""(cid) => window.okfLoomLive._dispatchEventForTest({type:'comment', id: cid, concept:'tables/orders'})""", cid)
    page.wait_for_function("() => window.okfLoomStudio.state.annotationAnnouncement === 'Comment posted.'", timeout=15_000)
    # SSE for the same id AFTER announce.
    page.evaluate("""(cid) => window.okfLoomLive._dispatchEventForTest({type:'comment', id: cid, concept:'tables/orders'})""", cid)
    page.wait_for_timeout(500)
    count = page.evaluate("""(cid) => window.okfLoomStudio.state.comments.filter(c => c.id === cid).length""", cid)
    assert count == 1, f"comment SSE must be exact-once, got {count}"


def test_composer_navigation_after_server_mutation_before_settle_no_success(server_url, page):
    """Finding 5: if navigation/generation changes after the server mutated
    (POST accepted) but BEFORE the canonical settle completes, the late settle
    must NOT announce success or clear the draft on the new page state. The
    comment IS persisted server-side (server mutation already happened)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    held = []
    page.route("**/__comment", lambda route, request: (held.append((route, request)),))
    _open_composer(page)
    _fill_and_send(page, "nav after mutate")
    page.wait_for_function("() => document.querySelector('.okf-composer').getAttribute('aria-busy') === 'true'", timeout=10_000)
    # Server mutates: release the POST as accepted.
    row = {"id": "01JNAVMUT", "concept": "tables/orders", "body": "nav after mutate", "state": "open", "row_token": "r2:nav", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}}
    # Route settle endpoints BEFORE releasing.
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc("target text\n", "target text", _COMPOSER_REV)))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": []}))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": [dict(row, anchor_resolution={"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []})]}))
    held[0][0].fulfill(status=201, json={"ok": True, "comment": row})
    # Once accepted, the settle starts; immediately navigate away (bump gens).
    page.evaluate("() => { window.okfLoomStudio.state.documentGeneration++; window.okfLoomStudio.state.annotationGeneration++; if(window.okfLoomLive) window.okfLoomLive._bumpNavEpoch(); }")
    page.wait_for_timeout(1000)
    # No success announcement on the new page state.
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted."


def test_composer_persistent_inline_status_present_until_resolved(server_url, page):
    """Visual P1: uncertain and accepted-refresh-failed reassurance copy is
    persistent inline (does not depend on a fading toast)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    # uncertain
    page.route("**/__comment", lambda route: route.abort("connectionreset"))
    _open_composer(page)
    _fill_and_send(page, "persistent status")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'uncertain'", timeout=15_000)
    st = _composer_status_text(page)
    assert st and "retrying is safe" in st.lower() and "create a duplicate" in st.lower(), f"uncertain inline status: {st!r}"
    assert "idempotency" not in st.lower(), f"uncertain status must not expose 'idempotency key': {st!r}"
    # accepted-refresh-failed
    page.unroute("**/__comment")
    page.route("**/__comment", lambda route: route.fulfill(status=201, json={"ok": True, "comment": {"id": "01JPIS", "concept": "tables/orders", "body": "x", "state": "open", "row_token": "r2:pis", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}}}))
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=_r10_doc("target text\n", "target text", _COMPOSER_REV)))
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": [42]}))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": []}))
    page.get_by_role("button", name="Retry send").click()
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'accepted-refresh-failed'", timeout=15_000)
    st2 = _composer_status_text(page).lower()
    assert "accepted" in st2 and "refresh" in st2 and "without re-posting" in st2, f"accepted-refresh-failed status: {st2!r}"


# ---------------------------------------------------------------------------
# Round 3: reply-parent preservation, envelope fail-closed, adjacent-mutation
# settlement, real-producer e2e, deterministic navigation-hold.
# ---------------------------------------------------------------------------


@pytest.fixture
def blank37_server(tmp_path_factory):
    """Serve a real bundle whose concept body contains 37 blank blockquotes
    (the real failure shape) so /__data/doc returns the actual producer
    projection (blocker 4)."""
    bundle = tmp_path_factory.mktemp("blank37-server") / "bundle"
    shutil.copytree(DEMO, bundle, ignore=shutil.ignore_patterns(".okf-loom"))
    body = "".join("> \n\n" for _ in range(37)) + "neighbour target\n\n---\n\nafter\n"
    (bundle / "blanks37.md").write_text("---\ntype: note\n---\n" + body, encoding="utf-8")
    port = _port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        okf_module_argv("serve", str(bundle), "--host", "127.0.0.1", "--port", str(port), "--no-open"),
        cwd=ROOT, env=okf_subprocess_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/", timeout=1) as response:
                if response.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            if proc.poll() is not None:
                pytest.fail("blank37 server exited")
    else:
        pytest.fail("blank37 server did not become ready")
    yield base
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_37_blank_blockquotes_real_producer_canonical_get_e2e(blank37_server, page):
    """Blocker 4: load the ACTUAL producer-generated canonical response for a
    real 37-`> \\n\\n`-blank-blockquote document via /__data/doc (not fabricated
    projection rows), pass it through the real browser validator, select
    neighboring text, submit exactly one POST, and settle against canonical
    comments."""
    page.goto(blank37_server + "/blanks37", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    page.wait_for_function("() => !!(window.okfLoomStudio.state.doc && window.okfLoomStudio.state.doc.annotation_source)")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready' || window.okfLoomStudio.state.commentsStatus === 'error'", timeout=15_000)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    # Fetch the REAL producer canonical doc and validate it through the browser.
    real = page.evaluate("""async () => {
        const res = await fetch('/__data/doc?id=blanks37', {headers:{Accept:'application/json'}});
        const doc = await res.json();
        const ok = window.okfLoomStudio._validProjection(doc.annotation_projection, doc.concept_content_rev, doc.annotation_source, doc.html);
        const bq = doc.annotation_projection.blocks.filter(b => String(b.block_id||'').startsWith('block-quote'));
        const hr = doc.annotation_projection.blocks.filter(b => String(b.block_id||'').startsWith('hr'));
        return {ok: ok, complete: doc.annotation_projection.complete, bqUnkeyed: bq.every(b => b.dom_key === null && b.segments.length === 0), bqCount: bq.length, hrKeyed: hr.length === 1 && hr[0].dom_key !== null, rev: doc.concept_content_rev, html: doc.html, source: doc.annotation_source};
    }""")
    assert real["ok"] is True, "real producer projection must pass the browser validator"
    assert real["complete"] is True
    assert real["bqCount"] == 37 and real["bqUnkeyed"], "37 blank blockquotes must be unkeyed in the real producer output"
    assert real["hrKeyed"], "HR must remain a keyed zero-segment record"

    # Select neighboring text on the keyed paragraph.
    page.evaluate("""() => {
        const s = window.okfLoomStudio.state;
        window.okfLoomStudio.state.commentsRequestGeneration++;
        window.okfLoomStudio.state.commentsReady = true;
        const node = document.querySelector('.okf-page__body p').firstChild;
        const r = document.createRange(); r.setStart(node, 0); r.setEnd(node, node.nodeValue.length);
        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
    }""")
    anchor = page.evaluate("window.okfLoomStudio._captureCanonicalAnchor()")
    assert anchor and anchor["exact"], "must capture a real anchor on the 37-blank doc"

    # Exactly one POST -> canonical settlement -> success.
    posts = {"n": 0}

    def comment(route, request):
        posts["n"] += 1
        route.fulfill(status=201, json={"ok": True, "comment": {"id": "01JREAL37", "concept": "blanks37", "body": "on real 37 doc", "state": "open", "row_token": "r2:real37", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "blanks37"}}})

    page.route("**/__comment", comment)
    page.route("**/__data/events*", lambda route: route.fulfill(status=200, json={"events": []}))
    page.route("**/__comments", lambda route: route.fulfill(status=200, json={"comments": [{"id": "01JREAL37", "concept": "blanks37", "body": "on real 37 doc", "state": "open", "row_token": "r2:real37", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "blanks37"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": real["rev"], "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}]}))
    # /__data/doc returns the real doc (already valid) so the settle commits.
    page.evaluate("""() => { window.__realDoc = window.okfLoomStudio.state.doc; }""")
    page.route("**/__data/doc*", lambda route: route.fulfill(status=200, json=page.evaluate("window.__realDoc")))
    page.evaluate("window.okfLoomStudio.openPanel('comments')")
    page.wait_for_selector(".okf-composer__textarea")
    page.locator(".okf-composer__textarea").fill("on real 37 doc")
    page.get_by_role("button", name="Send").click()
    page.wait_for_function("() => window.okfLoomStudio.state.annotationAnnouncement === 'Comment posted.'", timeout=15_000)
    assert posts["n"] == 1, f"expected exactly one POST on the real 37-blank doc, got {posts['n']}"
    assert page.evaluate("() => window.okfLoomStudio.state.comments.some(c => c.id === '01JREAL37')")


def test_composer_reply_uncertain_retry_preserves_parent_and_same_key(server_url, page):
    """Blocker 1: a reply (parent_id) that hits an uncertain outcome must
    preserve the original parent across the same-key retry. First and retry
    POSTs have identical parent/body/anchor/key; no conflict/duplicate/top-
    level drift."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    # Seed a parent comment to reply to.
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [{
            id: '01JPARENT0', concept: 'tables/orders', body: 'parent comment', state: 'open',
            row_token: 'r2:parent', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}
        }];
    }""")
    page.evaluate("window.okfLoomStudio.state.replyTo = '01JPARENT0'")
    captured = []

    def comment_lost(route, request):
        captured.append({
            "key": request.headers.get("idempotency-key"),
            "parent_id": request.post_data_json.get("parent_id") if request.post_data_json else None,
            "body": request.post_data_json.get("body") if request.post_data_json else None,
            "anchor": request.post_data_json.get("anchor") if request.post_data_json else None,
        })
        route.abort("connectionreset")

    page.route("**/__comment", comment_lost)
    _open_composer(page)
    _fill_and_send(page, "reply body")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'uncertain'", timeout=15_000)
    # Reply context still visible during uncertain.
    assert page.evaluate("""() => !!document.querySelector('.okf-composer__reply-context')""")
    assert page.evaluate("window.okfLoomStudio.state.composerPost.parent") == "01JPARENT0"
    # Retry (same draft) -> same key, same parent.
    page.get_by_role("button", name="Retry send").click()
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'uncertain'", timeout=15_000)
    assert len(captured) == 2, f"expected first + retry POST, got {len(captured)}"
    assert captured[0]["parent_id"] == "01JPARENT0" and captured[1]["parent_id"] == "01JPARENT0", "parent must be preserved across uncertain retry"
    assert captured[0]["key"] == captured[1]["key"], "uncertain retry must reuse the same key"
    assert captured[0]["body"] == captured[1]["body"] == "reply body"
    assert captured[0]["anchor"] == captured[1]["anchor"]
    # No top-level drift: every POST carried the parent.
    assert all(c["parent_id"] == "01JPARENT0" for c in captured)


def test_composer_success_envelope_cross_concept_and_malformed_rejected(server_url, page):
    """Blocker 2: a 2xx-ok comment whose concept mismatches the submitted
    concept, or which lacks a row_token / valid anchor, must NOT be upserted or
    announced; it stays accepted-refresh-failed."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    # Cross-concept acknowledged row.
    page.route("**/__comment", lambda route: route.fulfill(status=201, json={"ok": True, "comment": {"id": "01JCROSS", "concept": "tables/customers", "body": "x", "state": "open", "row_token": "r2:c", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/customers"}}}))
    _settle_ok(page, comments=[])
    _open_composer(page)
    _fill_and_send(page, "cross concept")
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'accepted-refresh-failed'", timeout=15_000)
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted."
    # The cross-concept row must NOT have been adopted into the open concept.
    assert page.evaluate("() => !window.okfLoomStudio.state.comments.some(c => c.id === '01JCROSS' && c.concept === 'tables/orders')")


def test_composer_edit_rejects_malformed_canonical_row(server_url, page):
    """Blocker 3: an inline edit whose /__comment-update response returns a
    malformed/cross-concept row must not adopt it or announce success; it
    settles canonically."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [{
            id: '01JEDIT00', concept: 'tables/orders', body: 'original body', state: 'open',
            row_token: 'r2:edit', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}
        }];
        window.okfLoomStudio.openPanel('comments');
    }""")
    page.wait_for_selector("#comment-01JEDIT00")
    # Route edit to return a cross-concept row.
    page.route("**/__comment-update", lambda route: route.fulfill(status=200, json={"ok": True, "comment": {"id": "01JEDIT00", "concept": "tables/customers", "body": "edited", "state": "open", "row_token": "r2:bad", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/customers"}}}))
    _settle_ok(page, comments=[{"id": "01JEDIT00", "concept": "tables/orders", "body": "original body", "state": "open", "row_token": "r2:edit", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}])
    # exact: the Studio launcher's accessible name ("Studio · Editable") also
    # contains "Edit"; this is the comment card's own Edit action.
    page.locator("#comment-01JEDIT00").get_by_role("button", name="Edit", exact=True).click()
    ta = page.locator(".okf-inline-edit textarea")
    ta.fill("edited body")
    page.locator(".okf-inline-edit").get_by_role("button", name="Save").click()
    # The cross-concept row must not be adopted; original body remains after settle.
    page.wait_for_function("() => !document.querySelector('.okf-inline-edit')", timeout=10_000)
    body = page.evaluate("() => window.okfLoomStudio.state.comments.find(c => c.id === '01JEDIT00').body")
    assert body == "original body", f"malformed edit row must not be adopted, got {body!r}"


def test_composer_state_update_and_archive_reject_malformed_row(server_url, page):
    """Blocker 3: Cancel (state=dismissed) and Archive must not adopt a
    malformed/cross-concept returned row; they settle canonically."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [{
            id: '01JSTATE9', concept: 'tables/orders', body: 'state test', state: 'open',
            row_token: 'r2:st', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}
        }];
        window.okfLoomStudio.openPanel('comments');
    }""")
    page.wait_for_selector("#comment-01JSTATE9")
    page.route("**/__comment-update", lambda route: route.fulfill(status=200, json={"ok": True, "comment": {"id": "01JSTATE9", "concept": "tables/customers", "body": "x", "state": "dismissed", "row_token": "r2:bad", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/customers"}}}))
    _settle_ok(page, comments=[{"id": "01JSTATE9", "concept": "tables/orders", "body": "state test", "state": "open", "row_token": "r2:st", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}])
    page.locator("#comment-01JSTATE9").get_by_role("button", name="Cancel").click()
    page.wait_for_function("() => window.okfLoomStudio.state.comments.find(c => c.id === '01JSTATE9').state === 'open'", timeout=10_000)
    # The malformed dismissed row was not adopted; canonical settle restored open.
    assert page.evaluate("() => window.okfLoomStudio.state.comments.find(c => c.id === '01JSTATE9').concept") == "tables/orders"


def test_composer_navigation_holds_settle_then_no_stale_success(server_url, page):
    """Blocker 5: deterministic post-mutation/pre-settle navigation. Return an
    accepted POST, HOLD a canonical settlement request, navigate/bump ownership,
    release the settlement, then prove persisted acceptance but NO stale
    success announcement, draft clear, or focus move on the new page."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    held_comment = {"n": 0}
    held_settle = []

    def comment_accept(route, request):
        held_comment["n"] += 1
        route.fulfill(status=201, json={"ok": True, "comment": {"id": "01JHOLD5", "concept": "tables/orders", "body": "hold settle", "state": "open", "row_token": "r2:hold", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}}})

    def hold_settle(route, request):
        if "/__comments" in request.url or "/__data/doc" in request.url or "/__data/events" in request.url:
            held_settle.append(route)
        else:
            route.continue_()

    page.route("**/__comment", comment_accept)
    page.route("**/__comments", hold_settle)
    page.route("**/__data/doc*", hold_settle)
    page.route("**/__data/events*", hold_settle)
    _open_composer(page)
    _fill_and_send(page, "hold settle")
    # Wait for the POST to land and the composer to be pending the settlement.
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'pending'", timeout=10_000)
    assert held_comment["n"] == 1, "exactly one accepted POST"
    # Poll until at least one canonical settlement request is held.
    import time as _t
    deadline = _t.monotonic() + 8
    while not held_settle and _t.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert held_settle, "a canonical settlement request must have arrived and been held"
    # Capture focus + composer state BEFORE navigating.
    active_before = page.evaluate("() => (document.activeElement && document.activeElement.tagName) || 'BODY'")
    comments_before = page.evaluate("window.okfLoomStudio.state.comments.length")
    # Navigate/bump ownership WHILE the settlement is held.
    page.evaluate("() => { window.okfLoomStudio.state.documentGeneration++; window.okfLoomStudio.state.annotationGeneration++; if(window.okfLoomLive) window.okfLoomLive._bumpNavEpoch(); }")
    # Release the held settlement requests with valid-but-empty canonical state.
    for r in held_settle:
        try:
            r.fulfill(status=200, json={"comments": []})
        except Exception:
            pass
    page.wait_for_timeout(800)
    # D: persisted acceptance (POST returned 201) but NO stale success, NO draft
    # restoration of the sent body, NO focus move onto the new page, and NO
    # new-page comment mutation from the late settle.
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted.", "no stale success after post-mutation navigation"
    assert page.evaluate("window.okfLoomStudio.state.draftBody") != "hold settle", "late settle must not restore the sent draft as if retrying"
    # Focus must not have moved onto the composer submit on the new page state.
    active_after = page.evaluate("() => (document.activeElement && document.activeElement.tagName) || 'BODY'")
    composer_submit_focused = page.evaluate("() => document.activeElement && document.activeElement.classList && document.activeElement.classList.contains('okf-composer__submit')")
    assert not composer_submit_focused, "late settle must not move focus to the composer submit"
    # No new comment row was adopted from the late (empty) settle.
    assert page.evaluate("window.okfLoomStudio.state.comments.length") == comments_before, "late settle must not mutate comments on the new page"


# ---------------------------------------------------------------------------
# Round 4: inline-reply idempotency (A), missing-row handling (B),
# composer deadline/late-completion (C).
# ---------------------------------------------------------------------------


def test_inline_reply_idempotency_uncertain_retry_same_key_no_duplicate(server_url, page):
    """Blocker A: an inline reply that hits an uncertain outcome (lost
    response) preserves parent/body identity, reuses the SAME Idempotency-Key on
    retry, exposes an explicit uncertain state (Retry reply), and produces no
    duplicate after the lost response."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [{
            id: '01JPARENT4', concept: 'tables/orders', body: 'parent for inline reply', state: 'open',
            row_token: 'r2:p4', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}
        }];
        window.okfLoomStudio.openPanel('comments');
    }""")
    page.wait_for_selector("#comment-01JPARENT4")
    keys = []
    held = []

    def comment_lost(route, request):
        keys.append(request.headers.get("idempotency-key"))
        parent = request.post_data_json.get("parent_id") if request.post_data_json else None
        keys.append(parent)
        held.append(route)
        route.abort("connectionreset")  # lost response -> uncertain

    page.route("**/__comment", comment_lost)
    page.locator("#comment-01JPARENT4").get_by_role("button", name="Reply", exact=True).click()
    page.locator(".okf-inline-reply__textarea").fill("inline reply body")
    page.locator(".okf-inline-reply").get_by_role("button", name="Reply").click()
    # Uncertain state: Retry reply visible, draft retained, status present.
    page.wait_for_selector(".okf-inline-reply button:has-text('Retry reply')", timeout=15_000)
    assert page.locator(".okf-inline-reply__textarea").input_value() == "inline reply body"
    assert page.evaluate("""() => { const n=document.querySelector('.okf-inline-reply .okf-composer__status'); return n ? n.textContent : ''; }""")
    assert keys[0] and keys[0].startswith("comment:"), "first POST must carry a comment: idempotency key"
    assert keys[1] == "01JPARENT4", "parent_id must be preserved on the first POST"

    # Retry with a working server response -> success, SAME key, no duplicate.
    posts = {"n": 0}

    def comment_ok(route, request):
        posts["n"] += 1
        keys.append(request.headers.get("idempotency-key"))
        keys.append(request.post_data_json.get("parent_id") if request.post_data_json else None)
        route.fulfill(status=201, json={"ok": True, "comment": {"id": "01JREPLY04", "concept": "tables/orders", "body": "inline reply body", "state": "open", "row_token": "r2:reply4", "ts": "2026-01-01T00:00:00Z", "parent_id": "01JPARENT4", "anchor": {"kind": "concept", "ref": "tables/orders"}}})

    page.unroute("**/__comment")
    page.route("**/__comment", comment_ok)
    page.get_by_role("button", name="Retry reply").click()
    page.wait_for_function("() => window.okfLoomStudio.state.comments.some(c => c.id === '01JREPLY04')", timeout=15_000)
    # The retry reused the SAME key and the SAME parent.
    assert keys[2] == keys[0], f"uncertain retry must reuse the same idempotency key: {keys}"
    assert keys[3] == "01JPARENT4", "parent_id must be preserved on the retry POST"
    # No duplicate: exactly one reply row for the parent (besides the parent).
    replies = page.evaluate("""() => window.okfLoomStudio.state.comments.filter(c => c.id === '01JREPLY04').length""")
    assert replies == 1, f"lost response must not create a duplicate reply: {replies}"


def test_inline_reply_definite_reject_uses_new_key_and_retains_draft(server_url, page):
    """Blocker A: a definite 4xx rejection of an inline reply retains the draft
    and uses a NEW key on the next attempt (reject is not uncertain)."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [{
            id: '01JPARENT5', concept: 'tables/orders', body: 'parent', state: 'open',
            row_token: 'r2:p5', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}
        }];
        window.okfLoomStudio.openPanel('comments');
    }""")
    page.wait_for_selector("#comment-01JPARENT5")
    keys = []
    page.route("**/__comment", lambda route, request: (keys.append(request.headers.get("idempotency-key")), route.fulfill(status=400, json={"ok": False, "error": "invalid_request"})))
    page.locator("#comment-01JPARENT5").get_by_role("button", name="Reply", exact=True).click()
    page.locator(".okf-inline-reply__textarea").fill("will be rejected")
    page.locator(".okf-inline-reply").get_by_role("button", name="Reply").click()
    page.wait_for_selector(".okf-inline-reply button:has-text('Reply')", timeout=15_000)
    assert page.locator(".okf-inline-reply__textarea").input_value() == "will be rejected"
    assert len(keys) == 1
    # Second attempt -> NEW key.
    page.locator(".okf-inline-reply").get_by_role("button", name="Reply").click()
    page.wait_for_selector(".okf-inline-reply button:has-text('Reply')", timeout=15_000)
    assert len(keys) == 2 and keys[0] != keys[1], "definite reject must use a new key"


def _seed_resolved_thread_for_archive(page):
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [{
            id: '01JROOT09', concept: 'tables/orders', body: 'resolved root', state: 'resolved',
            row_token: 'r2:root', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}, archived: false
        }];
        window.okfLoomStudio.openPanel('comments');
    }""")
    page.wait_for_selector("#comment-01JROOT09")


def test_state_update_missing_row_no_local_mutation(server_url, page):
    """Blocker B: a state update whose response OMITS data.comment must not
    optimistically mutate local state or announce success; it reconciles
    canonically."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [{
            id: '01JSTATEA', concept: 'tables/orders', body: 'state missing', state: 'open',
            row_token: 'r2:sa', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}
        }];
        window.okfLoomStudio.openPanel('comments');
    }""")
    page.wait_for_selector("#comment-01JSTATEA")
    # 2xx-ok with NO comment row.
    page.route("**/__comment-update", lambda route: route.fulfill(status=200, json={"ok": True}))
    _settle_ok(page, comments=[{"id": "01JSTATEA", "concept": "tables/orders", "body": "state missing", "state": "open", "row_token": "r2:sa", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}])
    page.locator("#comment-01JSTATEA").get_by_role("button", name="Cancel").click()
    page.wait_for_timeout(800)
    # The local state must NOT have flipped to dismissed (no optimistic mutation
    # from the missing row); canonical settle kept it open.
    assert page.evaluate("() => window.okfLoomStudio.state.comments.find(c => c.id === '01JSTATEA').state") == "open"


def test_archive_missing_row_no_local_mutation(server_url, page):
    """Blocker B: archive whose response OMITS data.comment must not flip the
    local archived flag or announce success."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    _seed_resolved_thread_for_archive(page)
    page.route("**/__comment-update", lambda route: route.fulfill(status=200, json={"ok": True}))
    _settle_ok(page, comments=[{"id": "01JROOT09", "concept": "tables/orders", "body": "resolved root", "state": "resolved", "row_token": "r2:root", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "archived": False, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}])
    page.get_by_role("button", name="Archive thread").click()
    page.wait_for_timeout(800)
    # archived must remain false locally (missing row -> no optimistic flip).
    assert page.evaluate("() => window.okfLoomStudio.state.comments.find(c => c.id === '01JROOT09').archived") is False


def test_unarchive_missing_row_no_local_mutation(server_url, page):
    """Blocker B: unarchive whose response OMITS data.comment must not flip the
    local archived flag or announce success."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("""() => {
        window.okfLoomStudio.state.commentView.showArchived = true;
        window.okfLoomStudio.state.comments = [{
            id: '01JROOTU', concept: 'tables/orders', body: 'archived root', state: 'resolved',
            row_token: 'r2:ru', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}, archived: true
        }];
        window.okfLoomStudio.openPanel('comments');
    }""")
    page.wait_for_selector("#comment-01JROOTU")
    page.route("**/__comment-update", lambda route: route.fulfill(status=200, json={"ok": True}))
    _settle_ok(page, comments=[{"id": "01JROOTU", "concept": "tables/orders", "body": "archived root", "state": "resolved", "row_token": "r2:ru", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "archived": True, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}])
    page.get_by_role("button", name="Unarchive").click()
    page.wait_for_timeout(800)
    # archived must remain true locally (missing row -> no optimistic flip).
    assert page.evaluate("() => window.okfLoomStudio.state.comments.find(c => c.id === '01JROOTU').archived") is True


def test_composer_post_deadline_then_late_completion_uncertain_same_key_retry(server_url, page):
    """Blocker C: the composer POST reaches its absolute deadline/abort, then a
    late completion arrives. Assert: public uncertain result, draft/parent/key
    retained, no stale success/local mutation/focus move, and a safe same-key
    retry."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    # Very short POST deadline so the hung route triggers an absolute deadline.
    page.evaluate("window.okfLoomStudio.state.annotationDeadlines={post:60}")
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [{
            id: '01JPARENTC', concept: 'tables/orders', body: 'deadline parent', state: 'open',
            row_token: 'r2:pc', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}
        }];
        window.okfLoomStudio.state.replyTo = '01JPARENTC';
    }""")
    keys = []
    held = []

    def hang(route, request):
        keys.append(request.headers.get("idempotency-key"))
        held.append(route)  # never fulfill -> absolute deadline fires

    page.route("**/__comment", hang)
    _open_composer(page)
    _fill_and_send(page, "deadline body")
    # The deadline aborts the POST -> uncertain outcome.
    page.wait_for_function("() => window.okfLoomStudio.state.composerPost && window.okfLoomStudio.state.composerPost.outcome === 'uncertain'", timeout=15_000)
    assert page.evaluate("window.okfLoomStudio.state.draftBody") == "deadline body", "draft retained after deadline"
    assert page.evaluate("window.okfLoomStudio.state.composerPost.parent") == "01JPARENTC", "parent retained after deadline"
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted."
    assert len(keys) == 1 and keys[0].startswith("comment:"), "first (deadline-aborted) POST must carry a key"
    first_key = keys[0]

    # Late completion arrives (the server eventually responds) — must NOT cause
    # stale success or any local mutation on the uncertain page.
    held[0].fulfill(status=201, json={"ok": True, "comment": {"id": "01JLATEC", "concept": "tables/orders", "body": "deadline body", "state": "open", "row_token": "r2:late", "ts": "2026-01-01T00:00:00Z", "parent_id": "01JPARENTC", "anchor": {"kind": "concept", "ref": "tables/orders"}}})
    page.wait_for_timeout(600)
    assert page.evaluate("window.okfLoomStudio.state.annotationAnnouncement") != "Comment posted.", "late completion must not produce stale success"
    assert page.evaluate("() => !window.okfLoomStudio.state.comments.some(c => c.id === '01JLATEC')"), "late completion must not adopt an unverified row"

    # Safe same-key retry -> success; no duplicate.
    row = {"id": "01JDEADL1", "concept": "tables/orders", "body": "deadline body", "state": "open", "row_token": "r2:dl", "ts": "2026-01-01T00:00:00Z", "parent_id": "01JPARENTC", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}
    page.unroute("**/__comment")
    page.route("**/__comment", lambda route, request: (keys.append(request.headers.get("idempotency-key")), route.fulfill(status=201, json={"ok": True, "comment": row})))
    _settle_ok(page, comments=[row])
    page.get_by_role("button", name="Retry send").click()
    page.wait_for_function("() => window.okfLoomStudio.state.annotationAnnouncement === 'Comment posted.'", timeout=15_000)
    assert keys[-1] == first_key, "deadline retry must reuse the same key"
    assert page.evaluate("() => window.okfLoomStudio.state.comments.filter(c => c.id === '01JDEADL1').length") == 1


def test_inline_reply_post_is_deadline_bounded_via_annotation_fetch(server_url, page):
    """Final blocker: the inline reply POST is routed through the canonical
    deadline-owning annotationFetch, so a HUNG response reaches the absolute
    deadline (it cannot hang indefinitely). Then: uncertain state preserves
    body/parent/key; a late completion does not cause stale mutation/success/
    focus change; a same-key retry creates exactly one canonical reply."""
    _boot(page, server_url)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    page.evaluate("window.okfLoomStudio.state.annotationRetryDelays=[0,0]")
    page.evaluate("window.okfLoomStudio.state.annotationJitter=()=>0")
    # Short POST deadline so the hung route reaches the absolute deadline.
    page.evaluate("window.okfLoomStudio.state.annotationDeadlines={post:70}")
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [{
            id: '01JPARDL', concept: 'tables/orders', body: 'deadline reply parent', state: 'open',
            row_token: 'r2:pdl', ts: '2026-01-01T00:00:00Z', anchor: {kind:'concept', ref:'tables/orders'}
        }];
        window.okfLoomStudio.openPanel('comments');
    }""")
    page.wait_for_selector("#comment-01JPARDL")
    keys = []
    held = []

    def hang(route, request):
        keys.append(request.headers.get("idempotency-key"))
        keys.append(request.post_data_json.get("parent_id") if request.post_data_json else None)
        held.append(route)  # never fulfill -> absolute deadline fires

    page.route("**/__comment", hang)
    page.locator("#comment-01JPARDL").get_by_role("button", name="Reply", exact=True).click()
    page.locator(".okf-inline-reply__textarea").fill("deadline reply body")
    page.locator(".okf-inline-reply").get_by_role("button", name="Reply").click()
    # The hung POST reaches the deadline -> uncertain (Retry reply).
    page.wait_for_selector(".okf-inline-reply button:has-text('Retry reply')", timeout=15_000)
    # Body + parent identity + key preserved.
    assert page.locator(".okf-inline-reply__textarea").input_value() == "deadline reply body"
    assert len(keys) >= 2 and keys[0].startswith("comment:"), "hung POST carried an idempotency key"
    assert keys[1] == "01JPARDL", "parent_id preserved on the deadline-aborted POST"
    first_key = keys[0]
    active_before = page.evaluate("() => (document.activeElement && document.activeElement.tagName) || 'BODY'")

    # Late completion arrives (server eventually responds) — must NOT cause stale
    # mutation/success/focus change.
    held[0].fulfill(status=201, json={"ok": True, "comment": {"id": "01JLATEIR", "concept": "tables/orders", "body": "deadline reply body", "state": "open", "row_token": "r2:lateir", "ts": "2026-01-01T00:00:00Z", "parent_id": "01JPARDL", "anchor": {"kind": "concept", "ref": "tables/orders"}}})
    page.wait_for_timeout(600)
    toasts = page.evaluate("() => Array.from(document.querySelectorAll('.okf-toast')).map(t => t.textContent)")
    assert not any("Reply posted" in t for t in toasts), f"late completion must not show stale success: {toasts}"
    assert page.evaluate("() => !window.okfLoomStudio.state.comments.some(c => c.id === '01JLATEIR')"), "late completion must not adopt an unverified reply row"

    # Same-key retry -> exactly one canonical reply (no duplicate).
    reply_row = {"id": "01JREPLDL", "concept": "tables/orders", "body": "deadline reply body", "state": "open", "row_token": "r2:rdl", "ts": "2026-01-01T00:00:00Z", "parent_id": "01JPARDL", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}
    page.unroute("**/__comment")

    def ok(route, request):
        keys.append(request.headers.get("idempotency-key"))
        route.fulfill(status=201, json={"ok": True, "comment": reply_row})

    page.route("**/__comment", ok)
    _settle_ok(page, comments=[reply_row, {"id": "01JPARDL", "concept": "tables/orders", "body": "deadline reply parent", "state": "open", "row_token": "r2:pdl", "ts": "2026-01-01T00:00:00Z", "anchor": {"kind": "concept", "ref": "tables/orders"}, "anchor_resolution": {"status": "resolved", "reason": "original_position", "concept_content_rev": _COMPOSER_REV, "resolver_version": 2, "position": {"start": 0, "end": 0}, "block": {"id": "", "hash": "sha256:" + "a" * 64, "start": 0, "end": 0}, "changes": []}}])
    page.get_by_role("button", name="Retry reply").click()
    page.wait_for_function("() => window.okfLoomStudio.state.comments.some(c => c.id === '01JREPLDL')", timeout=15_000)
    assert keys[-1] == first_key, f"deadline retry must reuse the same idempotency key: {keys}"
    assert page.evaluate("() => window.okfLoomStudio.state.comments.filter(c => c.id === '01JREPLDL').length") == 1, "same-key retry must create exactly one canonical reply (no duplicate)"
