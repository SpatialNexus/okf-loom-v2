"""F16 real-browser proof: mixed-backtick inline code pins exact anchors.

Six real-``serve`` scenarios with shipped assets only:

1. The canonical ``demo/showcase`` page — copied into the test's own tmp
   bundle, never written in place: the saved `` ` ```mermaid ` `` /
   `` ` ```math ` `` inline examples and ordinary prose pin exact canonical
   positions, and the saved anchors re-mark through the projection after a
   fresh canonical load.
2. A synthetic known-revision owned fixture: general regression for the
   whole pipeline on a minimal three-paragraph page — served ``studio.js``
   digest equals the checkout source, the served ``/__data/doc`` revision
   equals the recomputed canonical revision and the projection header, the
   served page's own validator admits the projection, and both ordinary
   prose and an isolated inline code span save exact anchors. It never
   visits ``/reference/capabilities``, so it is evidence about the
   pipeline, not about that page.
3. The real ``/reference/capabilities`` route from a copied docs-bundle:
   the served page correlates route, concept id, full canonical source,
   recomputed revision, projection revision, and served ``studio.js`` bytes
   with the checkout; the served full validator (including HTML) admits the
   projection; unique ordinary prose and a unique isolated inline-code span
   save exact same-block source positions that resolve at the original
   position against the matching revision; and both saved anchors re-mark
   exactly after a fresh load. Only this route-specific journey speaks
   about the original capabilities observation.
4. The four real docs pages whose list items carry inline code spans /
   fenced-looking runs across continuation boundaries: each served page's
   own HTML-aware validator admits its projection, and the served doc
   correlates route, canonical source, revision, and projection revision.
5. An actual problematic multiline-code list owner (the embedding-guide
   exception-behavior item) supports a UI-posted exact selection wholly
   inside one contiguous source run, persisted as schema-2 position evidence
   against the current revision, re-marked exactly after reload; plus one
   narrow CRLF case: a zero-indent continuation stays source-contiguous
   across the expanded newline (exact CRLF positions), while a selection
   crossing removed indentation falls back to a concept-level comment and
   never fabricates a guessed exact span.
6. Validation cost on a large synthetic page (X4): with two exact
   annotations already marked, posting a third runs the full projection
   check at most once per fetched ``/__data/doc`` and reaches "Comment
   posted." in the ordinary wait; a fresh, uncached check of the same page
   still admits the genuine projection and rejects tampered text or offsets.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

import pytest

from browser_support import copy_bundle, expect, live_server, launch_chromium, sync_playwright
from scripts.okf_loom.annotations import concept_content_rev, extract_blocks

pytestmark = pytest.mark.browser

# R3: the composer names an exact selection as one; it is never left unsaid.
EXACT_NOTE = ("Exact annotation: pinned to this passage and, once sent, highlighted on the page. It is re-checked "
              "when the page changes.")

PROBE = (
    "---\ntype: Reference\ntitle: Capability probe\n---\n\n"
    "# Capability probe\n\n"
    "Ordinary prose sentence alpha for selection.\n\n"
    "A fenced ` ```math ` block renders display math:\n\n"
    "Isolated inline `code span beta` for exact selection.\n"
)

# X4: about 62k code points and several thousand mapped segments (code
# spans, an entity, emphasis, a soft break per paragraph). Before X4 each
# full projection check re-split the whole source once per segment, so one
# check here took seconds and one post ran about ten of them.
LARGE = "---\ntype: Reference\ntitle: Large page\n---\n\n# Large page\n\n" + "".join(
    f"Paragraph {i:04d} marker-{i:04d}-alpha has `code {i:04d}` &amp; *emphasis {i:04d}*\n"
    f"and a continued line {i:04d}.\n\n" for i in range(600))


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        pg = context.new_page()
        yield pg
        context.close()
        browser.close()


def _concept_ready(page, url):
    # The showcase lazy-loads CDN modules (mermaid, highlight, KaTeX); fail
    # them so the run is offline, and wait for each owner's terminal signal.
    page.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
    page.goto(url, wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    settled = page.wait_for_function(
        "() => { const s = window.okfLoomStudio.state.commentsStatus;"
        " return ['ready', 'error'].includes(s) ? s : false; }").json_value()
    assert settled == "ready"
    page.wait_for_function("() => window.okfLoomStudio.state.selectionAffordanceReady === true")
    page.wait_for_function("() => !['', 'loading', undefined].includes(document.body.dataset.okfRenderersReady)")
    page.wait_for_function("() => !!window.okfLoomStudio.state.doc")


def _select_words(page, selector, start, end=None):
    """Select from `start` to the end of `end` (or of `start`) under one root."""
    page.locator(selector).first.evaluate(
        """(root, [start, end]) => {
          const nodes = []; let text = "", node;
          const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
          while ((node = walker.nextNode())) { nodes.push({node, at: text.length}); text += node.nodeValue; }
          const from = text.indexOf(start), to = end ? text.indexOf(end) + end.length : from + start.length;
          if (from < 0 || to <= from) throw new Error("selection text is absent");
          const point = (offset) => { for (const n of nodes) if (offset <= n.at + n.node.nodeValue.length) return [n.node, offset - n.at]; };
          const range = document.createRange();
          range.setStart(...point(from)); range.setEnd(...point(to));
          const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        }""", [start, end])


def _saved_card(page, body):
    card = page.locator(".okf-comment", has_text=body).filter(has_not=page.locator(".okf-comment__posting")).first
    expect(card).to_be_visible()
    return card


def _comment_on_selection(page, body):
    action = page.locator(".okf-comment-afford")
    expect(action).to_be_visible()
    # The affordance is a role="status" region; under the default Borders Off
    # appearance it must not paint a generated "Status" label beside Comment.
    assert page.locator("html").get_attribute("data-okf-border") == "off"
    assert action.evaluate("el => getComputedStyle(el, '::before').content") in ("none", "normal")
    assert "Status" not in action.inner_text()
    action.get_by_role("button").first.click()
    box = page.get_by_role("textbox", name="Comment for agent")
    expect(box).to_be_focused()
    note = page.locator(".okf-composer__anchor-note")
    shown = note.inner_text() if note.is_visible() else ""
    box.fill(body)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator("#okf-annotation-status")).to_have_text("Comment posted.")
    _saved_card(page, body)
    return shown


def _rows(page, base):
    data = page.request.get(f"{base}/__comments").json()
    return data["comments"] if isinstance(data, dict) else data


def _validator_null(page):
    """The served page's own (served-asset) validator admits its doc."""
    return page.evaluate(
        "() => { const d = window.okfLoomStudio.state.doc;"
        " return window.okfLoomStudio._projectionError("
        "d.annotation_projection, d.concept_content_rev, d.annotation_source, d.html); }")


def test_showcase_inline_example_and_prose_pin_exact_canonical_positions(tmp_path, page):
    root = copy_bundle(Path("docs-bundle"), tmp_path / "docs-copy")
    source = (root / "demo" / "showcase.md").read_text(encoding="utf-8")
    mermaid_at = source.index("` ```mermaid `") + 2
    math_at = source.index("` ```math `") + 2
    with live_server(root) as base:
        _concept_ready(page, f"{base}/demo/showcase")
        assert _validator_null(page) is None
        for body, start, exact, block in (
            ("f16 mermaid example", mermaid_at, "```mermaid", "paragraph:"),
            ("f16 math example", math_at, "```math", "paragraph:"),
            ("f16 ordinary prose", source.index("Sequence diagrams too:"), "Sequence diagrams too:", "paragraph:"),
            # The GFM task item whose checkbox segment decode was the last
            # latent validator gap the completed page exposed.
            ("f16 task item", source.index("GFM task lists render as real checkboxes"),
             "GFM task lists render as real checkboxes", "list-item:"),
        ):
            _concept_ready(page, f"{base}/demo/showcase")
            _select_words(page, ".okf-page__body", exact)
            shown = _comment_on_selection(page, body)
            assert shown == EXACT_NOTE, body
            row = next(r for r in _rows(page, base) if r["body"] == body)
            anchor = row["anchor"]
            assert (anchor["schema"], anchor["kind"], anchor["exact"]) == (2, "text-quote", exact), body
            assert anchor["block"]["id"].startswith(block), body
            assert anchor["position"] == {"start": start, "end": start + len(exact)}, body
        # Saved anchors re-mark the exact inline spans after a fresh load.
        _concept_ready(page, f"{base}/demo/showcase")
        expect(page.locator(".okf-page__body mark.okf-comment-mark")).to_have_text(
            ["GFM task lists render as real checkboxes", "```mermaid",
             "Sequence diagrams too:", "```math"])


def test_known_revision_fixture_pins_prose_and_isolated_inline_code(tmp_path, page):
    root = tmp_path / "caps-probe"
    root.mkdir()
    (root / "index.md").write_text("# Home\n\nOverview.\n", encoding="utf-8")
    (root / "note.md").write_text(PROBE, encoding="utf-8")
    expected_rev = concept_content_rev(PROBE)
    with live_server(root) as base:
        _concept_ready(page, f"{base}/note")
        # Served assets are the exact checkout client, not a stale service.
        served_js = page.request.get(f"{base}/__static/studio.js").text()
        checkout_js = Path("scripts/okf_loom/viewer/static/studio.js").read_text(encoding="utf-8")
        assert hashlib.sha256(served_js.encode()).hexdigest() == \
            hashlib.sha256(checkout_js.encode()).hexdigest()
        # Route/concept/source-rev/projection-rev correlation for this load.
        doc = page.request.get(f"{base}/__data/doc?id=note").json()
        assert doc["id"] == "note"
        assert doc["annotation_source"] == PROBE
        assert doc["concept_content_rev"] == expected_rev
        assert doc["annotation_projection"]["complete"] is True
        assert doc["annotation_projection"]["concept_content_rev"] == expected_rev
        assert _validator_null(page) is None
        # Exact selection through the real browser: isolated inline code.
        _select_words(page, ".okf-page__body", "code span beta")
        assert _comment_on_selection(page, "probe inline code") == EXACT_NOTE
        row = next(r for r in _rows(page, base) if r["body"] == "probe inline code")
        anchor = row["anchor"]
        at = PROBE.index("`code span beta`") + 1
        assert (anchor["kind"], anchor["exact"]) == ("text-quote", "code span beta")
        assert anchor["position"] == {"start": at, "end": at + len("code span beta")}
        # Ordinary prose on the same page, same revision.
        _concept_ready(page, f"{base}/note")
        _select_words(page, ".okf-page__body", "Ordinary prose sentence alpha")
        assert _comment_on_selection(page, "probe prose") == EXACT_NOTE
        row = next(r for r in _rows(page, base) if r["body"] == "probe prose")
        anchor = row["anchor"]
        at = PROBE.index("Ordinary prose sentence alpha")
        assert (anchor["kind"], anchor["exact"]) == ("text-quote", "Ordinary prose sentence alpha")
        assert anchor["position"] == {"start": at, "end": at + len("Ordinary prose sentence alpha")}


def test_reference_capabilities_page_pins_exact_selections_and_marks(tmp_path, page):
    # Route-specific F16 acceptance: the real docs-bundle capabilities page,
    # never the synthetic /note fixture. Both ranges below are unique in the
    # canonical source (prose in paragraph:3, one isolated single-backtick
    # inline-code span in table-row:51), so an exact position match cannot be
    # an accidental cross-block or wrong-occurrence mapping.
    root = copy_bundle(Path("docs-bundle"), tmp_path / "docs-copy")
    source = (root / "reference" / "capabilities.md").read_text(encoding="utf-8")
    expected_rev = concept_content_rev(source)

    def containing(exact):
        at = source.index(exact)
        assert source.count(exact) == 1, exact
        hits = [b for b in extract_blocks(source) if b.start <= at and at + len(exact) <= b.end]
        assert len(hits) == 1, exact
        return at, hits[0]

    plans = (
        ("caps real prose", "forward-compatibility mechanism"),
        ("caps real inline code", "okf.cap.jsonld_export"),
    )
    expected = {}
    for body, exact in plans:
        at, block = containing(exact)
        expected[body] = (at, exact, block.public())
    with live_server(root) as base:
        url = f"{base}/reference/capabilities"
        _concept_ready(page, url)
        assert page.url.endswith("/reference/capabilities")
        # Served assets are the exact checkout client, not a stale service.
        served_js = page.request.get(f"{base}/__static/studio.js").text()
        checkout_js = Path("scripts/okf_loom/viewer/static/studio.js").read_text(encoding="utf-8")
        assert hashlib.sha256(served_js.encode()).hexdigest() == \
            hashlib.sha256(checkout_js.encode()).hexdigest()
        # Route/concept/full-canonical-source/revision/projection correlation.
        doc = page.request.get(f"{base}/__data/doc?id=reference/capabilities").json()
        assert doc["id"] == "reference/capabilities"
        assert doc["annotation_source"] == source
        assert doc["concept_content_rev"] == expected_rev
        assert doc["annotation_projection"]["complete"] is True
        assert doc["annotation_projection"]["concept_content_rev"] == expected_rev
        # The served page's own full validator — with HTML, not metadata only.
        assert _validator_null(page) is None
        # Exact selection through the real browser: ordinary prose and one
        # isolated inline-code span, each proven same-block at a unique
        # source position.
        for body, exact in plans:
            at, block = expected[body][0], expected[body][2]
            _concept_ready(page, url)
            _select_words(page, ".okf-page__body", exact)
            assert page.evaluate("() => getSelection().toString()") == exact, body
            assert _comment_on_selection(page, body) == EXACT_NOTE, body
            row = next(r for r in _rows(page, base) if r["body"] == body)
            assert row["concept"] == "reference/capabilities", body
            anchor = row["anchor"]
            assert (anchor["schema"], anchor["kind"], anchor["exact"]) == (2, "text-quote", exact), body
            assert anchor["block"] == block, body
            assert anchor["position"] == {"start": at, "end": at + len(exact)}, body
            resolved = row["anchor_resolution"]
            assert resolved["status"] == "resolved", (body, resolved)
            assert resolved["reason"] == "original_position", (body, resolved)
            assert resolved["changes"] == [] and resolved["concept_content_rev"] == expected_rev, (body, resolved)
        # Saved anchors re-mark the exact spans after a fresh load.
        _concept_ready(page, url)
        expect(page.locator(".okf-page__body mark.okf-comment-mark")).to_have_text(
            ["forward-compatibility mechanism", "okf.cap.jsonld_export"])


# ===========================================================================
# List-item multiline inline tokenization (actual pages + CRLF/gap case).
#
# The renderer joins a list item's continuation lines into one logical item
# text and renders it through a single inline parse; the projection
# tokenizes the same logical text with an explicit source position map.
# These journeys prove the four affected REAL pages admit their projections
# through the served HTML-aware validator, and that an actual problematic
# multiline-code list owner supports exact UI selection, persistence, and
# re-marking — while selections crossing removed indentation still fall
# back instead of guessing.
# ===========================================================================

_LIST_PARITY_ROUTES = (
    "explanation/architecture",
    "explanation/research",
    "reference/architecture",
    "reference/embedding_guide",
)


def test_multiline_list_code_pages_admit_html_aware_projection(tmp_path, page):
    """All four actual affected pages: the served page's own validator —
    evaluated WITH the rendered HTML, not metadata-only — admits the
    projection, and the served doc correlates route, full canonical source,
    canonical revision, and projection revision with the copied checkout."""
    root = copy_bundle(Path("docs-bundle"), tmp_path / "docs-copy")
    with live_server(root) as base:
        for route in _LIST_PARITY_ROUTES:
            source = root.joinpath(*route.split("/")).with_suffix(".md") \
                .read_text(encoding="utf-8")
            expected_rev = concept_content_rev(source)
            _concept_ready(page, f"{base}/{route}")
            assert page.url.endswith(f"/{route}"), route
            doc = page.request.get(f"{base}/__data/doc?id={route}").json()
            assert doc["id"] == route, route
            assert doc["annotation_source"] == source, route
            assert doc["concept_content_rev"] == expected_rev, route
            projection = doc["annotation_projection"]
            assert projection["complete"] is True, route
            assert projection["concept_content_rev"] == expected_rev, route
            # HTML-aware admission by the served client itself.
            assert _validator_null(page) is None, route


def test_actual_multiline_code_owner_pins_exact_selection_and_remarks(tmp_path, page):
    """The actual embedding-guide exception-behavior item (whose fenced-looking
    continuation renders as one multiline inline-code span) supports a
    UI-posted exact selection wholly inside one contiguous source run:
    schema-2 text-quote anchor, same-block evidence at the exact source
    position, resolved at the original position against the current revision,
    and the saved anchor re-marks the exact span after a fresh load."""
    root = copy_bundle(Path("docs-bundle"), tmp_path / "docs-copy")
    source = (root / "reference" / "embedding_guide.md").read_text(encoding="utf-8")
    expected_rev = concept_content_rev(source)
    exact = "2>>/var/log/okf-callbacks.log"
    assert source.count(exact) == 1, exact
    at = source.index(exact)
    hits = [b for b in extract_blocks(source) if b.start <= at and at + len(exact) <= b.end]
    assert len(hits) == 1, exact
    block = hits[0]
    assert block.id.startswith("list-item:"), block.id
    with live_server(root) as base:
        url = f"{base}/reference/embedding_guide"
        _concept_ready(page, url)
        assert _validator_null(page) is None
        _select_words(page, ".okf-page__body", exact)
        assert page.evaluate("() => getSelection().toString()") == exact
        assert _comment_on_selection(page, "multiline code exact") == EXACT_NOTE
        row = next(r for r in _rows(page, base) if r["body"] == "multiline code exact")
        assert row["concept"] == "reference/embedding_guide"
        anchor = row["anchor"]
        assert (anchor["schema"], anchor["kind"], anchor["exact"]) == \
            (2, "text-quote", exact)
        assert anchor["block"] == block.public()
        assert anchor["position"] == {"start": at, "end": at + len(exact)}
        resolved = row["anchor_resolution"]
        assert resolved["status"] == "resolved", resolved
        assert resolved["reason"] == "original_position", resolved
        assert resolved["changes"] == [] and resolved["concept_content_rev"] == expected_rev, resolved
        # The saved exact anchor re-marks the same span after a fresh load.
        _concept_ready(page, url)
        mark = page.locator(".okf-page__body mark.okf-comment-mark")
        expect(mark).to_have_count(1)
        assert mark.evaluate("el => el.textContent") == exact


def test_multiline_code_cross_newline_exact_and_indent_gap_falls_back(tmp_path, page):
    """One narrow cross-line code-span case served end-to-end: a ZERO-indent
    continuation stays source-contiguous across the expanded newline, so a
    selection spanning the newline pins exact positions slicing the raw
    newline bytes; a selection crossing the REMOVED INDENTATION of the
    indented item falls back to a concept-level comment and never fabricates
    a guessed exact span. (CRLF sources cannot be served verbatim: the
    bundle loader reads with universal newlines, so canonical source is
    always LF; CRLF tokenizer parity is proven at the producer level by the
    parametrized IR tests.)"""
    root = tmp_path / "newline-probe"
    root.mkdir()
    (root / "index.md").write_text("# Home\n\nOverview.\n", encoding="utf-8")
    body = "- `alpha\nbeta`\n\n- `one\n  two`\n"
    (root / "note.md").write_text(
        "---\ntype: Reference\ntitle: newline probe\n---\n\n" + body,
        encoding="utf-8")
    source = (root / "note.md").read_text(encoding="utf-8")
    expected_rev = concept_content_rev(source)
    with live_server(root) as base:
        url = f"{base}/note"
        _concept_ready(page, url)
        assert _validator_null(page) is None
        doc = page.request.get(f"{base}/__data/doc?id=note").json()
        assert doc["annotation_source"] == source
        assert doc["concept_content_rev"] == expected_rev
        assert doc["annotation_projection"]["complete"] is True
        # Exact across the expanded newline (source-contiguous run).
        # Selection.toString() renders the newline as a space (rendered
        # layout); the anchor evidence is Range-based, so exactness is
        # asserted on the saved anchor below.
        _select_words(page, ".okf-page__body", "alpha\nbeta")
        selected = page.evaluate("() => getSelection().toString()")
        assert "alpha" in selected and "beta" in selected, selected
        assert _comment_on_selection(page, "cross-newline contiguous exact") == EXACT_NOTE
        row = next(r for r in _rows(page, base) if r["body"] == "cross-newline contiguous exact")
        anchor = row["anchor"]
        at = source.index("alpha")
        assert (anchor["schema"], anchor["kind"]) == (2, "text-quote")
        assert anchor["exact"] == "alpha\nbeta"
        assert anchor["position"] == {"start": at, "end": at + len("alpha\nbeta")}
        assert anchor["block"]["id"].startswith("list-item:")
        # Crossing the removed indentation falls back — no guessed span.
        _concept_ready(page, url)
        _select_words(page, ".okf-page__body", "one\ntwo")
        selected = page.evaluate("() => getSelection().toString()")
        assert "one" in selected and "two" in selected, selected
        assert _comment_on_selection(page, "indent gap fallback") != ""
        row = next(r for r in _rows(page, base) if r["body"] == "indent gap fallback")
        anchor = row["anchor"]
        assert anchor["kind"] == "concept", anchor
        assert "exact" not in anchor and "position" not in anchor, anchor


def test_authored_projection_key_actual_spec_exact_selection_persists_and_remarks(tmp_path, page):
    """Actual unchanged /reference/spec, not a synthetic substitute for it."""
    root = copy_bundle(Path("docs-bundle"), tmp_path / "docs-copy")
    source = (root / "reference" / "spec.md").read_text(encoding="utf-8")
    assert source == Path("docs-bundle/reference/spec.md").read_text(encoding="utf-8")
    exact = "data-okf-projection-key"
    assert source.count(exact) == 1
    at = source.index(exact)
    rev = concept_content_rev(source)
    block, = [b for b in extract_blocks(source) if b.start <= at < at + len(exact) <= b.end]
    with live_server(root) as base:
        url = f"{base}/reference/spec"
        _concept_ready(page, url)
        assert page.url.endswith("/reference/spec")
        doc = page.request.get(f"{base}/__data/doc?id=reference/spec").json()
        assert doc["id"] == "reference/spec"
        assert doc["annotation_source"] == source
        assert doc["concept_content_rev"] == rev
        projection = doc["annotation_projection"]
        assert projection["complete"] is True
        assert projection["concept_content_rev"] == rev
        served = page.request.get(url).text()
        embedded = re.search(
            r'<script type="application/json" id="okf-annotation-projection">(.*?)</script>', served, re.S)
        assert embedded and json.loads(embedded.group(1)) == projection
        assert doc["html"] in served
        assert "data-okf-proj-owner" not in doc["html"]
        assert page.evaluate("() => window.okfLoomStudio.state.doc.annotation_source") == source
        assert page.evaluate("() => window.okfLoomStudio.state.doc.html") == doc["html"]
        assert _validator_null(page) is None  # full served HTML-aware admission
        keys = page.locator(".okf-page__body [data-okf-projection-key]").evaluate_all(
            "nodes => nodes.map(n => n.getAttribute('data-okf-projection-key'))")
        assert keys and all(k.isdecimal() for k in keys)
        assert len(keys) == len(set(keys))
        assert {int(k) for k in keys} == {
            r["dom_key"] for r in projection["blocks"] if r["dom_key"] is not None}
        record, = [r for r in projection["blocks"] if r["block_id"] == block.id]
        owner = f'.okf-page__body [data-okf-projection-key="{record["dom_key"]}"]'
        literal = page.locator(owner + " code", has_text=exact)
        expect(literal).to_have_count(1)
        literal.scroll_into_view_if_needed()
        expect(literal).to_be_visible()
        expect(literal).to_have_text(exact)
        _select_words(page, owner, exact)
        assert page.evaluate("() => getSelection().toString()") == exact
        assert _comment_on_selection(page, "authored reserved spelling") == EXACT_NOTE
        row = next(r for r in _rows(page, base) if r["body"] == "authored reserved spelling")
        assert row["concept"] == "reference/spec"
        anchor = row["anchor"]
        assert (anchor["schema"], anchor["kind"], anchor["exact"]) == (2, "text-quote", exact)
        assert anchor["position"] == {"start": at, "end": at + len(exact)}
        assert anchor["block"] == block.public()
        resolved = row["anchor_resolution"]
        assert (resolved["status"], resolved["reason"], resolved["concept_content_rev"]) == \
            ("resolved", "original_position", rev)
        assert resolved["changes"] == []
        _concept_ready(page, url)
        assert _validator_null(page) is None
        mark = page.locator(owner + " code mark.okf-comment-mark")
        expect(mark).to_have_count(1)
        expect(mark).to_have_text(exact)
        assert page.locator(".okf-page__body mark.okf-comment-mark").count() == 1
        saved = next(r for r in _rows(page, base) if r["id"] == row["id"])
        assert saved["anchor"] == anchor
        assert saved["anchor_resolution"]["concept_content_rev"] == rev


def test_authored_projection_key_injected_plugin_scrubbed_browser_rejects(tmp_path, page, monkeypatch):
    """Controlled plugin through production discovery and both consent gates."""
    root = tmp_path / "plugin-bundle"
    root.mkdir()
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    source = "---\ntype: Reference\n---\n\nAuthored `data-okf-projection-key`.\n\nStable prose.\n"
    (root / "note.md").write_text(source, encoding="utf-8")
    (root / "okf-loom.config.yaml").write_text("viewer:\n  allow_active_code: true\n", encoding="utf-8")
    # A tiny disposable entry-point distribution, not an installed dependency
    # or a monkeypatch of the projection/admission path.
    plugins = tmp_path / "controlled-plugin"
    dist = plugins / "authored_key_probe-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: authored-key-probe\nVersion: 1.0\n", encoding="utf-8")
    (dist / "entry_points.txt").write_text(
        "[okf_loom.viewer_plugins]\nprobe = authored_key_probe:Probe\n", encoding="utf-8")
    (plugins / "authored_key_probe.py").write_text(
        'class Probe:\n'
        '    def on_concept_render(self, concept, html):\n'
        '        return html + \'<aside id="injected-key">data-okf-projection-key</aside>\'\n',
        encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(plugins) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    with live_server(root, "--allow-active-code") as base:
        _concept_ready(page, f"{base}/note")
        doc = page.request.get(f"{base}/__data/doc?id=note").json()
        assert doc["annotation_source"] == source
        assert doc["concept_content_rev"] == concept_content_rev(source)
        assert doc["annotation_projection"]["complete"] is False
        assert _validator_null(page) is not None
        expect(page.locator("#injected-key")).to_have_count(1)  # plugin really ran
        expect(page.locator("#injected-key")).to_have_text("")
        assert "data-okf-projection-key" not in page.locator(".okf-page__body").inner_text()
        assert "data-okf-proj-owner" not in doc["html"]
        assert "data-okf-projection-key" not in re.sub(
            r' data-okf-projection-key="[0-9]+"', "", doc["html"])
        _select_words(page, ".okf-page__body", "Stable prose.")
        assert _comment_on_selection(page, "injected-key page fallback") != ""
        row = next(r for r in _rows(page, base) if r["body"] == "injected-key page fallback")
        assert row["anchor"]["kind"] == "concept"
        assert "position" not in row["anchor"]


def _large_page_bundle(tmp_path):
    root = tmp_path / "large-page"
    root.mkdir()
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "large.md").write_text(LARGE, encoding="utf-8")
    return root


def _validation_count(page):
    return page.evaluate("() => window.okfLoomStudio._projectionValidationCount()")


def test_large_page_post_checks_the_projection_once_per_fetched_document(tmp_path, page):
    """X4: a post no longer re-runs the full projection check per caller.

    The send gate, admission, the staged tuple, every existing exact mark and
    the composer each asked for the same verdict about the same document.
    """
    with live_server(_large_page_bundle(tmp_path)) as base:
        url = f"{base}/large"
        for body, exact in (("x4 first", "marker-0100-alpha"), ("x4 second", "marker-0300-alpha")):
            _concept_ready(page, url)
            _select_words(page, ".okf-page__body", exact)
            assert _comment_on_selection(page, body) == EXACT_NOTE, body
        _concept_ready(page, url)
        marks = page.locator(".okf-page__body mark.okf-comment-mark")
        expect(marks).to_have_text(["marker-0100-alpha", "marker-0300-alpha"])
        _select_words(page, ".okf-page__body", "marker-0500-alpha")
        page.locator(".okf-comment-afford").get_by_role("button").first.click()
        box = page.get_by_role("textbox", name="Comment for agent")
        expect(box).to_be_focused()
        expect(page.locator(".okf-composer__anchor-note")).to_have_text(EXACT_NOTE)  # exact, not a page comment
        box.fill("x4 measured")
        fetched = []
        page.on("response", lambda response: fetched.append(response.url) if "/__data/doc" in response.url else None)
        before = _validation_count(page)
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.locator("#okf-annotation-status")).to_have_text("Comment posted.")
        validations = _validation_count(page) - before
        assert 1 <= validations <= len(fetched), (validations, fetched)
        _saved_card(page, "x4 measured")
        expect(marks).to_have_text(["marker-0100-alpha", "marker-0300-alpha", "marker-0500-alpha"])
        row = next(r for r in _rows(page, base) if r["body"] == "x4 measured")
        assert (row["anchor"]["kind"], row["anchor"]["exact"]) == ("text-quote", "marker-0500-alpha")


def test_large_page_fresh_projection_check_keeps_every_verdict(tmp_path, page):
    """X4: an uncached check still admits the genuine map and rejects tampering."""
    def block_id(exact):
        at = LARGE.index(exact)
        block, = [b for b in extract_blocks(LARGE) if b.start <= at < b.end]
        return block.id

    altered, shortened = block_id("marker-0250-alpha"), block_id("marker-0400-alpha")
    edited = block_id("marker-0350-alpha")
    with live_server(_large_page_bundle(tmp_path)) as base:
        _concept_ready(page, f"{base}/large")
        # Boot commits the same revision twice (the comments load and live's
        # first reconciliation), each with its own document object, and only
        # the comments load judges its document before committing it. Let
        # live settle, then commit through a comments load, so the document
        # measured below is the one that load judged.
        page.wait_for_function("() => !!window.okfLoomLive._getLastDocRevForTest('large')")
        assert page.evaluate("() => window.okfLoomStudio._loadComments()") is True
        result = page.evaluate("""(shortened) => {
          const api = window.okfLoomStudio, d = api.state.doc;
          const judge = (projection, html) => {
            const before = api._projectionValidationCount();
            const verdict = api._projectionError(projection, d.concept_content_rev, d.annotation_source, html);
            return {verdict, runs: api._projectionValidationCount() - before};
          };
          const fresh = () => JSON.parse(JSON.stringify(d.annotation_projection));
          const alteredHtml = d.html.replace("marker-0250-alpha", "marker-0250-alphZ");
          const genuine = fresh(), out = {};
          out.loaded = judge(d.annotation_projection, d.html);
          // Linear work: one full check splits the whole source once, where it
          // used to split it again for every mapped segment.
          const realFrom = Array.from, whole = d.annotation_source.length;
          let splits = 0;
          Array.from = function (value) {
            if (typeof value === "string" && value.length >= whole) splits++;
            return realFrom.apply(this, arguments);
          };
          try { out.fresh = judge(fresh(), d.html); } finally { Array.from = realFrom; }
          out.wholeSourceSplits = splits;
          out.genuine = judge(genuine, d.html);
          out.genuineAgain = judge(genuine, d.html);
          out.alteredText = judge(fresh(), alteredHtml);
          out.genuineWithAlteredText = judge(genuine, alteredHtml);
          out.genuineRestored = judge(genuine, d.html);
          // G1: the revision is checked before the remembered verdict, on
          // every call: a wrong revision never reaches (or reruns) the cache.
          const judgeRev = (projection, rev) => {
            const before = api._projectionValidationCount();
            const verdict = api._projectionError(projection, rev, d.annotation_source, d.html);
            return {verdict, runs: api._projectionValidationCount() - before};
          };
          out.wrongRevision = judgeRev(genuine, "f".repeat(64));
          out.rightRevision = judgeRev(genuine, d.concept_content_rev);
          // G2: the verdict is reused only for the exact source string. One
          // changed code point in the source (same projection object, same
          // html) is judged afresh. A separate projection object keeps the
          // genuine entry above untouched.
          const sourceProbe = fresh(), at = d.annotation_source.indexOf("marker-0350-alpha");
          const editedSource = d.annotation_source.slice(0, at) + "n" + d.annotation_source.slice(at + 1);
          out.sourceProbeGenuine = judge(sourceProbe, d.html);
          const beforeEdit = api._projectionValidationCount();
          out.editedSource = {verdict: api._projectionError(sourceProbe, d.concept_content_rev, editedSource, d.html),
                              runs: api._projectionValidationCount() - beforeEdit};
          const tampered = fresh();
          tampered.blocks.find((b) => b.block_id === shortened).segments[0].source_end -= 1;
          out.tampered = judge(tampered, d.html);
          out.tamperedAgain = judge(tampered, d.html);
          // A judged projection cannot drift from its remembered verdict.
          const segment = genuine.blocks.find((b) => b.block_id === shortened).segments[0];
          const end = segment.source_end;
          try { (() => { "use strict"; segment.source_end = end - 1; })(); out.write = "accepted"; }
          catch (error) { out.write = error instanceof TypeError ? "refused" : String(error); }
          out.afterWrite = [segment.source_end === end, judge(genuine, d.html)];
          return out;
        }""", shortened)
    ok = {"verdict": None, "runs": 1}
    assert result["loaded"] == {"verdict": None, "runs": 0}  # the comments load judged it
    assert (result["fresh"], result["wholeSourceSplits"]) == (ok, 1)
    assert result["genuine"] == ok
    assert result["genuineAgain"] == {"verdict": None, "runs": 0}
    assert result["alteredText"] == {"verdict": f"block ({altered}): segment text mismatch", "runs": 1}
    assert result["genuineWithAlteredText"] == {"verdict": f"block ({altered}): segment text mismatch", "runs": 1}
    assert result["genuineRestored"] == ok
    assert result["wrongRevision"] == {"verdict": "projection metadata invalid", "runs": 0}
    assert result["rightRevision"] == {"verdict": None, "runs": 0}
    assert result["sourceProbeGenuine"] == ok
    assert result["editedSource"] == {"verdict": f"block ({edited}): segment text mismatch", "runs": 1}
    assert result["tampered"] == {"verdict": f"block ({shortened}): segment decode failed", "runs": 1}
    assert result["tamperedAgain"] == {"verdict": f"block ({shortened}): segment decode failed", "runs": 0}
    assert result["write"] == "refused"
    assert result["afterWrite"] == [True, {"verdict": None, "runs": 0}]
