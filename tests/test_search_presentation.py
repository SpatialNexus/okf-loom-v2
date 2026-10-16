"""Public Phase 2 index and Search presentation contracts."""
from __future__ import annotations

import contextlib
import html as _html
import re
import sys
from pathlib import Path

import pytest

from browser_support import expect, free_port, launch_chromium, managed_server, sync_playwright
from okf_loom import Bundle
from okf_loom.render import (
    _render_graph_page,
    _render_index_page,
    _render_search_page,
    build_site,
    render_single_file,
)


def _bundle(tmp_path: Path, count: int = 5) -> Bundle:
    root = tmp_path / "bundle"
    root.mkdir(parents=True)
    (root / "index.md").write_text("# Bundle\n\nStart here.\n", encoding="utf-8")
    for index in range(count):
        (root / f"guide-{index}.md").write_text(
            "---\n"
            f"type: {'Guide' if index % 2 else 'Reference'}\n"
            f"title: Guide {index}\n"
            f"description: Find needle {index}.\n"
            f"timestamp: \"2026-08-{index + 1:02}T00:00:00Z\"\n"
            "---\n\n"
            f"## Answer {index}\n\nThe exact needle passage for result {index}.\n",
            encoding="utf-8",
        )
    return Bundle.load(root)


def _result(index: int, *, passage: bool = True) -> dict:
    result = {
        "concept_id": f"guide-{index}",
        "title": f"Guide {index}",
        "type": "Guide",
        "score": 1 / (index + 1),
        "backends": ["lexical"],
        "reasons": ["title contains needle", "passage contains needle"],
        "detail": {"field": "body"},
    }
    if passage:
        result["evidence"] = {"passage": {
            "excerpt": f"The exact needle passage for result {index}.",
            "citation": f"[guide-{index}.md#L9-L9]",
            "path": f"guide-{index}.md",
            "heading": f"Answer {index}",
        }}
    else:
        result["source_backend"] = "relation"
        result["reasons"] = ["relation target"]
        result["evidence"] = {"relation": {"source": "guide-1", "target": f"guide-{index}"}}
    return result


def test_index_has_one_inventory_with_progressive_order_and_filter_controls(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    html = _render_index_page(
        bundle, mode="serve", name="Bundle", palette={}, config={}, sub="", index_file=None,
    )
    assert html.count('class="okf-card"') == len(bundle.concepts)
    assert 'data-index-browse-controls' in html
    assert '<option value="recommended" selected>Recommended</option>' in html
    assert '<option value="recent">Recently changed</option>' in html
    assert '<option value="Guide">Guide</option>' in html
    assert 'data-index-clear' in html
    assert 'data-index-no-match' in html and 'data-index-empty' in html
    assert 'data-index-browse.js' not in html
    assert 'index-browse.js' in html


def test_search_uses_canonical_labels_compact_disclosure_and_action_ids(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    html = _render_search_page(
        bundle, mode="serve", name="Bundle", config={}, query="needle",
        results=[_result(index) for index in range(5)],
    )
    for value, label in (
        ("hybrid", "Best overall"), ("lexical", "Exact wording"),
        ("semantic", "Similar meaning"), ("tag", "Tagged with"),
        ("entity", "Mentions entity"), ("relation", "Connected by relation"),
    ):
        assert re.search(rf'name="mode" value="{value}"[^>]*>\s*<span>{label}<small>', html)
    article = re.search(r'<article class="okf-search-result".*?</article>', html, re.DOTALL).group(0)
    # Ledger: one primary action per page (the search submit); each result's
    # Open is a secondary outlined button.
    assert '<a class="okf-btn okf-search-open"' in article
    assert 'okf-btn--primary' not in article
    assert article.count("Evidence and context") == 1
    assert '<details class="okf-search-disclosure">' in article
    assert '<details class="okf-search-disclosure" open>' not in article
    assert 'data-search-action="copy-passage"' in article and '>Copy passage</button>' in article
    assert 'data-search-action="copy-concept-context"' in article and '>Copy concept context</button>' in article
    assert "Source:" in article and "Scope:" in article and "Estimated size:" in article
    disclosure = article.split('<details class="okf-search-disclosure">', 1)[1]
    assert "[guide-0.md#L9-L9]" in disclosure
    assert "Exact canonical source passage" in disclosure
    assert "title contains needle" in disclosure


def _meta_line(html: str, cid: str) -> tuple[str, str, str]:
    """Return (heading html, meta-line html, meta-line text after the id)."""
    article = re.search(
        rf'<article class="okf-search-result"[^>]*data-concept-id="{re.escape(cid)}">(.*?)</article>',
        html, re.DOTALL).group(1)
    heading = re.search(r"<h3>(.*?)</h3>", article, re.DOTALL).group(1)
    meta = re.search(r'<div class="okf-search-result__meta okf-muted">(.*?)</div>', article, re.DOTALL).group(1)
    words = " ".join(_html.unescape(re.sub(r"<[^>]+>", "", meta)).split())
    return heading, meta, words.split(cid, 1)[1]


def test_search_advisory_lists_only_non_default_states(tmp_path: Path) -> None:
    """U4: one muted meta line per result; the advisory group names only
    trust/lifecycle/freshness states that differ from the OKF defaults, with
    staleness evaluated at the page's single render instant."""
    from datetime import datetime, timezone

    now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    defaults = {"trust_tier": "unverified", "status": "stable",
                "generated_at": None, "generated_at_source": None}
    long_status = "a" * 39 + "\U0001F600" + "overflow"  # 40th code point is astral
    cases = {
        "defaults": (defaults, None),
        "legacy": ({}, None),
        "reviewed": ({**defaults, "trust_tier": "human-reviewed", "status": "deprecated",
                      "stale_after": "2026-09-08T12:00:00+00:00"},  # equality is stale
                     " · Advisory · Trust: human-reviewed · Lifecycle: deprecated · Stale"),
        "future": ({**defaults, "trust_tier": "machine-confirmed", "status": "draft",
                    "stale_after": "2026-09-08T12:00:01+00:00"},
                   " · Advisory · Trust: machine-confirmed · Lifecycle: draft"),
        "fresh-default": ({**defaults, "stale_after": "2099-01-01T00:00:00+00:00"}, None),
        "offset-stale": ({**defaults, "stale_after": "2026-09-08T13:00:00+01:00"},
                         " · Advisory · Stale"),
        "invalid-expiry": ({**defaults, "stale_after": "2026-02-30T00:00:00+00:00"},
                           " · Advisory · Freshness unknown (invalid expiry)"),
        "null-expiry": ({**defaults, "stale_after": None},
                        " · Advisory · Freshness unknown (invalid expiry)"),
        "unrecognized": ({**defaults, "status": "Archived"},
                         " · Advisory · Lifecycle: Archived (unrecognized)"),
        "overlong": ({**defaults, "status": long_status},
                     " · Advisory · Lifecycle: " + "a" * 39 + "\U0001F600… (unrecognized)"),
        "non-string": ({**defaults, "status": 3}, " · Advisory · Lifecycle: invalid value"),
        "null-status": ({**defaults, "status": None}, " · Advisory · Lifecycle: invalid value"),
        "hostile": ({**defaults, "status": "<script>window.OKF_XSS=1</script>"},
                    " · Advisory · Lifecycle: <script>window.OKF_XSS=1</script> (unrecognized)"),
    }
    results = []
    for index, (cid, (metadata, _expected)) in enumerate(cases.items()):
        row = _result(index % 5)
        row.update({"concept_id": cid, "title": f"Result {cid}"})
        if metadata:
            row["metadata"] = metadata
        results.append(row)
    html = _render_search_page(
        _bundle(tmp_path), mode="serve", name="Bundle", config={}, query="needle",
        results=results, now=now,
    )
    for cid, (_metadata, expected) in cases.items():
        heading, meta, trailing = _meta_line(html, cid)
        assert "Advisory" not in heading and "okf-search-advisory" not in heading, cid
        if expected is None:
            assert "okf-search-advisory" not in meta, cid
            assert trailing == "", (cid, trailing)
        else:
            assert meta.count('<span class="okf-search-advisory">') == 1, cid
            assert trailing == expected, (cid, trailing)
        # Trust is informational; lifecycle and freshness carry the attention
        # marker the stylesheet sets heavier (never colour alone).
        assert '<span data-okf-advisory="trust" data-okf-advisory-attention' not in meta, cid
        for kind in ("lifecycle", "freshness"):
            if f'data-okf-advisory="{kind}"' in meta:
                assert f'data-okf-advisory="{kind}" data-okf-advisory-attention>' in meta, (cid, kind)
    assert "<script>window.OKF_XSS" not in html
    # The concept page's live clock owns data-okf-stale-at; search results are
    # evaluated once and never re-labelled "Not stale" by renderers.js.
    assert "data-okf-stale-at" not in html
    assert "Trust: unverified" not in html and "Lifecycle: stable" not in html
    # Without an explicit instant the page evaluates freshness at render time.
    live = _render_search_page(
        _bundle(tmp_path / "live"), mode="serve", name="Bundle", config={}, query="needle",
        results=[results[list(cases).index("fresh-default")]],
    )
    assert "okf-search-advisory" not in live


def test_search_limit_notice_and_relevant_empty_actions(tmp_path: Path) -> None:
    from okf_loom.retrieval import SearchRequest
    bundle = _bundle(tmp_path)
    limited = _render_search_page(bundle, mode="serve", name="Bundle", config={}, query="needle",
                                  results=[_result(0)], request=SearchRequest(limit=1))
    assert 'class="okf-search-limit-notice"' in limited
    assert "Result limit reached (1). Increase Result limit in Filters" in limited
    empty = _render_search_page(bundle, mode="serve", name="Bundle", config={}, query="zzqxv", results=[])
    assert "Clear filters</a>" not in empty and "Try Best overall</a>" in empty
    assert "with the current filters" not in empty
    filtered = _render_search_page(bundle, mode="serve", name="Bundle", config={}, query="zzqxv", results=[],
                                   request=SearchRequest(mode="semantic", tag="needle"))
    assert "Clear filters</a>" in filtered and "with the current filters" in filtered
    assert '?q=zzqxv&amp;mode=semantic&amp;limit=20' in filtered
    assert 'class="okf-search-empty__actions"' in filtered


def test_search_marks_only_complete_non_stopword_terms(tmp_path: Path) -> None:
    row = _result(0)
    row["evidence"]["passage"]["excerpt"] = "Special specification inspection spec SPEC and command the themes café cafés snake_case"
    html = _render_search_page(_bundle(tmp_path), mode="serve", name="Bundle", config={},
                               query="spec and the café snake_case", results=[row])
    preview = re.search(r'<blockquote class="okf-search-snippet okf-search-snippet--compact">(.*?)</blockquote>', html).group(1)
    assert re.findall(r"<mark>(.*?)</mark>", preview) == ["spec", "SPEC", "café", "snake_case"]


def test_passageless_result_is_honest_and_has_no_passage_action(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    html = _render_search_page(
        bundle, mode="serve", name="Bundle", config={}, query="needle", results=[_result(0, passage=False)],
    )
    article = re.search(r'<article class="okf-search-result".*?</article>', html, re.DOTALL).group(0)
    assert "Graph evidence only" in article
    assert "No exact passage is available" in article
    assert "Copy passage" not in article and 'data-copy-passage' not in article
    assert "Copy concept context" in article


# Wraps the shared portable renderer as soon as portable-retrieval.js
# publishes it, before either adapter can call it, and remembers every node it
# returned plus the last row it rendered.
_SHARED_RENDER_SPY = """
(() => {
  let published;
  window.__okfSharedNodes = new WeakSet();
  Object.defineProperty(window, "okfPortableRetrieval", {
    configurable: true,
    get() { return published; },
    set(value) {
      if (value && typeof value.renderResult === "function") {
        const shared = value.renderResult;
        value.renderResult = function (row, options) {
          const node = shared.apply(this, arguments);
          window.__okfSharedNodes.add(node);
          window.__okfLastRender = {row, options};
          return node;
        };
      }
      published = value;
    },
  });
})();
"""

_MODE_LABELS = ("Best overall", "Exact wording", "Similar meaning", "Tagged with",
                "Mentions entity", "Connected by relation")


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


@pytest.mark.browser
@pytest.mark.parametrize("surface", ["static", "single-file"])
def test_portable_adapters_render_shared_results_with_capability_actions(
    tmp_path: Path, page, surface: str,
) -> None:
    """Static and single-file search render every result through the shared
    portable renderer; only the adapters add capability-bound copy actions."""
    bundle = _bundle(tmp_path / "input")
    page.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
    page.add_init_script(_SHARED_RENDER_SPY)
    with contextlib.ExitStack() as stack:
        if surface == "static":
            site = tmp_path / "site"
            build_site(bundle, site, target="static")
            port = free_port()
            base = stack.enter_context(managed_server(
                [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
                 "--directory", str(site)],
                f"http://127.0.0.1:{port}", tmp_path / f"static-{port}.log"))
            page.goto(f"{base}/__search.html?q=needle&mode=lexical", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
            scope = page.locator("body")
            results = page.locator(".okf-search-results-list > li")
        else:
            out = tmp_path / "single.html"
            render_single_file(bundle, out)
            page.goto(out.as_uri(), wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.body.dataset.okfSearchCaptureReady === 'ready'"
                " && !document.getElementById('okf-single-search-toggle').disabled")
            page.locator("#okf-single-search-toggle").click()
            scope = page.locator("#okf-single-search")
            scope.get_by_role("radio", name="Exact wording").check()
            page.fill("#okf-single-query", "needle")
            scope.locator("form button[type='submit']").click()
            results = page.locator("[data-single-results] > li")
            expect(scope.get_by_role("button", name="Build context pack")).to_have_count(1)
        for label in _MODE_LABELS:
            expect(scope.get_by_role("radio", name=label)).to_have_count(1)

        expect(results).to_have_count(5)
        first_meta = results.first.locator(".okf-search-result__meta")
        expect(first_meta.locator(".okf-search-result__type svg")).to_have_count(1)
        assert ".md" not in first_meta.inner_text()
        if surface == "static":
            expect(page.locator('.okf-topbar input[name="q"]')).to_have_value("needle")
            expect(results.first.locator(".okf-copy-scope code").first).to_have_text(re.compile(r"guide-\d\.md"))
            expect(page.locator(".okf-page-context > details > summary")).to_have_text("Build context pack for first 5 results")
        # Every displayed result is a node the shared renderer returned.
        assert results.evaluate_all(
            "items => items.every(item => window.__okfSharedNodes.has(item))")
        expect(results.get_by_text("Why this result?")).to_have_count(0)
        for index in range(5):
            item = results.nth(index)
            disclosure = item.locator("details.okf-search-disclosure")
            expect(disclosure).to_have_count(1)
            summary = disclosure.locator(":scope > summary")
            expect(summary).to_have_text("Evidence and context")
            assert disclosure.evaluate("details => details.open") is False
            # R11: one filled primary per view (the Search submit); every
            # row's Open stays an outlined button.
            open_action = item.get_by_role("link", name="Open", exact=True)
            expect(open_action).to_have_count(1)
            open_markup = open_action.evaluate("a => a.outerHTML")
            assert "okf-btn" in open_markup and "okf-btn--primary" not in open_markup, open_markup
            passage = item.get_by_role("button", name="Copy passage", exact=True)
            expect(passage).to_have_attribute("data-search-action", "copy-passage")
            summary.click()
            context = disclosure.get_by_role("button", name="Copy concept context", exact=True)
            expect(context).to_be_visible()
            expect(context).to_have_attribute("data-search-action", "copy-concept-context")

        # Without an adapter's actions the shared renderer adds no controls.
        bare = page.evaluate("""() => {
          const {row, options} = window.__okfLastRender;
          const item = window.okfPortableRetrieval.renderResult(row, options, "#probe", () => {}, null);
          return {
            summaries: [...item.querySelectorAll("details.okf-search-disclosure > summary")]
              .map(summary => summary.textContent),
            buttons: item.querySelectorAll("button").length,
          };
        }""")
        assert bare == {"summaries": ["Evidence and context"], "buttons": 0}


def test_docs_index_is_concise_role_based_start_here() -> None:
    root = Path(__file__).resolve().parents[1]
    body = (root / "docs-bundle/index.md").read_text(encoding="utf-8")
    assert "# Start here" in body
    for label in ("Use from a clone", "Open the live Studio", "Find a command", "Understand the format"):
        assert label in body
    assert "# Four kinds of documentation" not in body
    assert body.count("\n* [") <= 5


def test_empty_index_distinguishes_bundle_empty_from_active_filter_mismatch(tmp_path: Path) -> None:
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    (empty_root / "index.md").write_text("# Empty\n", encoding="utf-8")
    empty_html = _render_index_page(
        Bundle.load(empty_root), mode="serve", name="Empty", palette={}, config={}, sub="", index_file=None,
    )
    assert "No concepts are available yet" in empty_html
    assert 'data-index-no-match hidden' in empty_html

    populated_html = _render_index_page(
        _bundle(tmp_path / "populated", count=1), mode="serve", name="Bundle", palette={}, config={}, sub="", index_file=None,
    )
    assert 'data-index-empty hidden' in populated_html
    assert 'data-index-no-match hidden' in populated_html


# ---------------------------------------------------------------------------
# F7 — recency surfaces read the OKF 0.2 metadata owner, not the raw legacy key
# ---------------------------------------------------------------------------

def _recency_bundle(root: Path) -> Bundle:
    """Fixture pair-plus covering every F7 acceptance shape (all valid YAML).

    Expected effective-instant order (newest first), keyed by concept id:
      b_offset_late  generated.at 2026-09-01T23:00-05:00  = 2026-09-02T04:00Z (newest;
                     authored string sorts BELOW c_utc — lexical compare would flip it)
      c_utc          generated.at 2026-09-02T00:00+00:00
      a_gen_new      generated.at 2026-09-01T12:00+02:00 = 10:00Z, no legacy key
      e_equiv        same instant as a_gen_new in a different offset (stable id tie)
      d_conflict     generated.at 2026-08-30 WINS over its newer legacy timestamp
      f_empty_by     generated present, by empty, newer legacy timestamp → UNKNOWN
                     (no fallback), despite the newest legacy date in the bundle
      g_invalid      generated.at invalid, legacy timestamp present → UNKNOWN
      h_undated      no dates at all → UNKNOWN
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text("# Recency\n\nStart here.\n", encoding="utf-8")
    concepts = {
        "a_gen_new": 'type: Note\ntitle: A gen new\ngenerated: {by: agent/v2, at: "2026-09-01T12:00:00+02:00"}',
        "b_offset_late": 'type: Note\ntitle: B offset late\ngenerated: {by: agent/v2, at: "2026-09-01T23:00:00-05:00"}',
        "c_utc": 'type: Note\ntitle: C utc\ngenerated: {by: agent/v2, at: "2026-09-02T00:00:00+00:00"}',
        "d_conflict": ('type: Note\ntitle: D conflict\ngenerated: {by: agent/v2, at: "2026-08-30T00:00:00Z"}\n'
                       'timestamp: "2026-09-09T00:00:00Z"'),
        "e_equiv": 'type: Note\ntitle: E equiv\ngenerated: {by: agent/v2, at: "2026-09-01T10:00:00Z"}',
        "f_empty_by": 'type: Note\ntitle: F empty by\ngenerated: {by: "", at: "2026-09-10T00:00:00Z"}\ntimestamp: "2026-09-05T00:00:00Z"',
        "g_invalid": 'type: Note\ntitle: G invalid\ngenerated: {by: agent/v2, at: not-a-date}\ntimestamp: "2026-09-06T00:00:00Z"',
        "h_undated": "type: Note\ntitle: H undated",
    }
    for stem, frontmatter in concepts.items():
        (root / f"{stem}.md").write_text(
            f"---\n{frontmatter}\n---\n\nBody of {stem}.\n", encoding="utf-8",
        )
    return Bundle.load(root)


def _recent_titles_order(html: str) -> list[str]:
    return re.findall(r'<li><a href="[^"]*">([^<]*)</a>', html)


_EXPECTED_RECENCY_ORDER = [
    "B offset late", "C utc", "A gen new", "E equiv", "D conflict",
    "F empty by", "G invalid", "H undated",
]


def test_index_cards_carry_effective_instant_not_legacy_key(tmp_path: Path) -> None:
    """F7 surface 1: the card attribute is the canonical UTC effective instant;
    unknown-time cards stay empty and never inherit a legacy date."""
    bundle = _recency_bundle(tmp_path / "f7")
    html = _render_index_page(
        bundle, mode="serve", name="F7", palette={}, config={}, sub="", index_file=None,
    )
    assert 'data-index-instant="2026-09-01T10:00:00+00:00"' in html, (
        "generated.at must be canonicalized to UTC for the client sort"
    )
    assert 'data-index-instant=""' in html, "unknown-time cards must stay empty"
    assert "data-index-timestamp" not in html


def test_empty_query_suggestions_order_by_effective_instant(tmp_path: Path) -> None:
    """F7 surface 2: the empty-search 'Recent concepts' head ranks real
    instants (mixed offsets compared as instants), generated over conflicting
    legacy. The head keeps its existing top-5 contract; the FULL ordering
    (unknown-time last, stable concept-id ties) is asserted on the shared
    helper both suggestion lists consume."""
    from okf_loom.render import _recent_concepts_by_instant
    bundle = _recency_bundle(tmp_path / "f7")
    html = _render_search_page(
        bundle, mode="serve", name="F7", config={}, query="", results=[],
    )
    assert _recent_titles_order(html) == _EXPECTED_RECENCY_ORDER[:5]
    full = [c.title for c in _recent_concepts_by_instant(bundle, limit=99)]
    assert full == _EXPECTED_RECENCY_ORDER


def test_nojs_graph_suggestions_match_search_recency(tmp_path: Path) -> None:
    """F7 surface 3: the no-JS graph 'Recent concepts' list agrees with the
    empty-search head — one recency semantic across server suggestion lists."""
    bundle = _recency_bundle(tmp_path / "f7")
    html = _render_graph_page(
        bundle, mode="serve", name="F7", config={},
    )
    recent_section = re.search(
        r'okf-graph-nojs__recent.*?<ul>(.*?)</ul>', html, re.S,
    ).group(1)
    assert _recent_titles_order(recent_section) == _EXPECTED_RECENCY_ORDER[:5]


# ---------------------------------------------------------------------------
# F14 — the index recency rail speaks reader words with a real separator
# ---------------------------------------------------------------------------

def _render_index_with_events(tmp_path: Path, events: list[dict]) -> str:
    bundle = _bundle(tmp_path / "rail", count=2)
    return _render_index_page(
        bundle, mode="serve", name="Rail", palette={}, config={}, sub="", index_file=None,
        engagement_events=events,
    )


def test_recent_rail_maps_disk_changed_to_reader_words_with_separator(tmp_path: Path) -> None:
    """F14: 'disk changed' never reaches an accessible name; the action phrase
    and the time are separated; raw audit fields stay on the row."""
    html = _render_index_with_events(tmp_path, [
        {"id": "evt-1", "seq": 1, "type": "changed", "ids": ["guide-0"],
         "origin": "disk", "ts": "2026-09-08T10:00:00.123Z"},
    ])
    assert "disk changed" not in html
    assert "<strong>Guide 0</strong> — edited on disk" in html
    # Separator between the phrase and the time is screen-reader-only (a
    # visible bare " · " became a stray grid item); raw ts stays machine-readable.
    assert '</span><span class="sr-only"> · </span><time' in html
    assert "</span> · <time" not in html
    assert 'datetime="2026-09-08T10:00:00.123Z"' in html
    assert "2026-09-08 10:00 UTC" in html
    # The audit record remains on the row element, untouched.
    assert 'data-event-action="changed"' in html
    assert 'data-event-actor="disk"' in html


def test_recent_rail_keeps_actor_identity_and_truthful_fallbacks(tmp_path: Path) -> None:
    """F14: real actors keep their audit identity, reader verbs apply where
    known, and an unparseable timestamp shows its raw record (no invention)."""
    html = _render_index_with_events(tmp_path, [
        {"id": "evt-2", "seq": 2, "type": "activity", "ids": ["guide-1"],
         "actor": "agent:test/1", "action": "update-section", "origin": "mutator",
         "ts": "2026-09-08T11:30:00Z", "summary": "edit"},
        {"id": "evt-3", "seq": 3, "type": "created", "ids": ["guide-0"],
         "origin": "mutator", "ts": "not-a-ts"},
    ])
    assert "<strong>Guide 1</strong> — agent:test/1 update-section" in html
    assert "<strong>Guide 0</strong> — created via mutator" in html
    assert "2026-09-08 11:30 UTC" in html
    assert "not-a-ts</time>" in html
    assert "disk changed" not in html
