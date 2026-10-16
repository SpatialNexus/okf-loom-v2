"""Q3 search metacharacter robustness through the real search controls.

Search has no user-facing regular-expression feature (spec: lexical, tag,
semantic, hybrid, entity and relation modes only), so this is not a
"regex error" contract. It proves that text which would be meaningful to a
regular-expression engine — an unclosed class, an unbalanced group, a
wildcard and a lone backslash — is ordinary query text everywhere a person
can type it:

* the live shell search form (typed suggestions, then Enter),
* the live search-page box (its visible Search button), and
* the static build's client-side search-page box.

For each query the text round-trips into the controls, the page settles with
no page error, unhandled rejection or console error, highlighting renders
without throwing, and an ordinary follow-up query through the same control
still returns results.
"""
from __future__ import annotations

import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

from browser_support import (
    copy_bundle, expect, free_port, launch_chromium, live_server, managed_server, sync_playwright,
)
from conftest import okf_module_argv, okf_subprocess_env

pytestmark = pytest.mark.browser

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
DEMO_BUNDLE = TOOLKIT_ROOT / "samples" / "demo_bundle"
QUERIES = ("orders[", "a(b", ".*", "\\")
QUERY_IDS = ("unclosed-class", "unbalanced-group", "wildcard", "backslash")
FOLLOW_UP = "orders"

_REJECTION_PROBE = """
  window.__okfRejections = [];
  window.addEventListener('unhandledrejection', event => {
    const reason = event.reason;
    window.__okfRejections.push(String(reason && reason.message || reason));
  });
"""


@pytest.fixture(scope="module")
def live_url(tmp_path_factory) -> str:
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp("search-robustness") / "bundle")
    with live_server(bundle, "--no-watch") as base:
        yield base


# A passage whose Markdown syntax, link targets and hostile inline HTML sit
# before and around the only occurrence of a unique term, far enough in that
# the first 140 source characters never reach it.
MARKDOWN_TERM = "zebrafinch"
MARKDOWN_CONCEPT = """---
type: Reference
title: Ledger conventions
description: Every `ledger-sync` and **settled** payment.
---

# Ledger conventions

Every **settled** payment is copied to [the orders table](../tables/orders.md) once the
nightly reconciliation has compared the processor export with the warehouse
snapshot, and [checkout](/services/checkout.md "Checkout service") runs
`ledger-sync --zebrafinch` with *care* and ~~legacy~~ __strict__ rules.
<img src="/__okf-xss-probe.png" onerror="window.__okfXss = 1"> Keep snake_case_names.
"""


@pytest.fixture(scope="module")
def markdown_url(tmp_path_factory) -> str:
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp("search-markdown") / "bundle")
    (bundle / "references" / "ledger_conventions.md").write_text(MARKDOWN_CONCEPT, encoding="utf-8")
    with live_server(bundle, "--no-watch") as base:
        yield base


@pytest.fixture(scope="module")
def static_url(tmp_path_factory) -> str:
    root = tmp_path_factory.mktemp("search-robustness-static")
    out = root / "site"
    result = subprocess.run(
        okf_module_argv("build", str(DEMO_BUNDLE), "--target", "static", "--out", str(out)),
        cwd=str(TOOLKIT_ROOT), env=okf_subprocess_env(), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with managed_server(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(out)],
        base, root / "http.log",
    ):
        yield base


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_init_script(_REJECTION_PROBE)
            pg = context.new_page()
            problems: list[str] = []
            pg.on("pageerror", lambda error: problems.append(f"pageerror: {error}"))
            pg.on("console", lambda message: problems.append(f"console {message.type}: {message.text}")
                  if message.type == "error" else None)
            pg.on("response", lambda response: problems.append(f"http {response.status}: {response.url}")
                  if response.status >= 400 else None)
            pg.on("requestfailed", lambda request: problems.append(f"requestfailed: {request.url} {request.failure}")
                  if not request.url.split("?")[0].endswith(("/__events", "/__presence")) else None)
            pg.okf_problems = problems
            yield pg
            context.close()
        finally:
            browser.close()


def _query_of(url: str) -> list[str] | None:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True).get("q")


def _assert_clean(page) -> None:
    """The page settled without errors, rejections or failed requests."""
    page.wait_for_function("() => document.readyState === 'complete'")
    rejections = page.evaluate("() => window.__okfRejections || []")
    assert rejections == [], rejections
    assert page.okf_problems == [], page.okf_problems


def _assert_marks_are_query_terms(page, container: str) -> None:
    """Highlighting wraps only word text, never a metacharacter."""
    marks = page.locator(f"{container} mark").all_inner_texts()
    assert all(text.strip() and not re.search(r"[\[\]().*\\]", text) for text in marks), marks


def _submit_live_shell(page, query: str) -> None:
    shell = page.locator(".okf-shell__search-form input[name='q']")
    listbox = page.get_by_role("listbox", name="Search suggestions")
    if len(query.strip()) >= 2:
        # A typed query of two or more characters fetches live suggestions;
        # the suggestion renderer (with its highlighter) must settle cleanly.
        with page.expect_response(lambda r: "/__search?" in r.url and "format=json" in r.url) as suggested:
            shell.fill(query)
        assert suggested.value.ok, suggested.value.status
        status = page.locator(".okf-shell__search-form [role='status']")
        expect(status).to_have_text(re.compile(r"suggestions?\b", re.I))
        expect(status).not_to_contain_text("Suggestions unavailable")
        if FOLLOW_UP in query:
            expect(listbox).to_be_visible()
            expect(listbox.locator("mark").first).to_be_visible()
        _assert_marks_are_query_terms(page, ".okf-shell__search-form .okf-search-live")
    else:
        shell.fill(query)
    shell.press("Enter")
    page.wait_for_url(lambda url: "/__search" in url and _query_of(url) == [query])


@pytest.mark.parametrize("query", QUERIES, ids=QUERY_IDS)
def test_live_shell_search_form_treats_metacharacters_as_text(live_url: str, page, query: str) -> None:
    page.goto(live_url + "/")
    _submit_live_shell(page, query)
    expect(page.locator(".okf-shell__search-form input[name='q']")).to_have_value(query)
    expect(page.locator("#okf-search-query")).to_have_value(query)
    expect(page.locator("#okf-results-heading")).to_be_visible()
    expect(page.locator("#okf-search-query")).not_to_have_attribute("aria-invalid", "true")
    _assert_marks_are_query_terms(page, ".okf-search__results")
    _assert_clean(page)

    _submit_live_shell(page, FOLLOW_UP)
    expect(page.locator(".okf-search-result").first).to_be_visible()
    expect(page.locator(".okf-shell__search-form input[name='q']")).to_have_value(FOLLOW_UP)
    _assert_clean(page)


def test_live_suggestion_evidence_reads_as_plain_text_around_the_highlighted_match(
    markdown_url: str, page,
) -> None:
    """The suggestion's evidence line shows the passage as a reader sees it.

    The JSON passage excerpt stays the exact Markdown source (citations need
    it); the dropdown must not print its syntax, must keep the highlighted
    match in view even when it sits deep in the passage, and must never turn
    passage HTML into elements.
    """
    page.goto(markdown_url + "/")
    shell = page.locator(".okf-shell__search-form input[name='q']")
    with page.expect_response(lambda r: "/__search?" in r.url and "format=json" in r.url) as suggested:
        shell.fill(MARKDOWN_TERM)
    excerpt = next(item for item in suggested.value.json()
                   if item["concept_id"] == "references/ledger_conventions")["evidence"]["passage"]["excerpt"]
    assert "**settled**" in excerpt and "`ledger-sync --zebrafinch`" in excerpt, excerpt  # exact source kept

    option = page.get_by_role("listbox", name="Search suggestions").get_by_role(
        "option", name=re.compile(r"^Ledger conventions, Reference"))
    evidence = option.locator(".okf-search-live__evidence")
    expect(evidence).to_be_visible()
    text = evidence.inner_text()
    for syntax in ("**", "](", "`", "~~", "__strict", "<img", "onerror", ".md"):
        assert syntax not in text, (syntax, text)
    assert text.startswith("\u2026"), text
    assert "checkout runs ledger-sync --zebrafinch with care and legacy strict rules." in text, text
    assert evidence.locator("mark").all_inner_texts() == [MARKDOWN_TERM], text
    expect(evidence.locator("mark")).to_be_in_viewport()
    assert page.locator(".okf-search-live img, .okf-search-live script").count() == 0
    assert page.evaluate("() => window.__okfXss") is None
    _assert_clean(page)


def test_search_suggestion_empty_and_recent_states(live_url: str, page) -> None:
    page.goto(live_url + "/tables/orders")
    search = page.locator(".okf-shell__search-form input[name='q']")
    search.focus()
    live = page.get_by_role("listbox", name="Search suggestions")
    expect(live).to_be_hidden()  # the only visit is the current page
    page.goto(live_url + "/")
    search = page.locator(".okf-shell__search-form input[name='q']")
    search.focus()
    live = page.get_by_role("listbox", name="Search suggestions")
    expect(live).to_be_visible()
    expect(live.get_by_role("option")).to_have_count(1)
    assert live.get_by_role("option").get_attribute("data-href") == "/tables/orders"
    search.fill("zzqxv")
    expect(live.locator(".okf-search-live__empty")).to_have_text("No matching pages")
    expect(live.get_by_role("option")).to_have_count(1)
    expect(live.get_by_role("option")).to_have_text("View all results")
    assert live.get_by_role("option").evaluate("n => getComputedStyle(n).borderBottomWidth") == "0px"


def test_search_preview_and_concept_summary_are_readable_but_copy_keeps_source(markdown_url: str, page) -> None:
    page.goto(markdown_url + "/__search?q=" + MARKDOWN_TERM)
    result = page.locator('.okf-search-result[data-concept-id="references/ledger_conventions"]')
    preview = result.locator(".okf-search-snippet").first
    expect(preview).to_be_visible()
    assert "ledger-sync --zebrafinch with care and legacy strict rules" in preview.inner_text()
    for syntax in ("**", "](", "`", "~~", "__strict", "<img", "onerror", ".md"):
        assert syntax not in preview.inner_text()
    source = result.locator("[data-copy-passage]").get_attribute("data-excerpt")
    assert "**settled**" in source and "`ledger-sync --zebrafinch`" in source
    result.locator(".okf-search-disclosure > summary").click()
    raw = result.locator(".okf-search-source")
    expect(raw).not_to_be_visible()
    result.get_by_text("Exact Markdown source", exact=True).click()
    expect(raw).to_have_text(source)
    assert result.locator("img, script").count() == 0
    page.goto(markdown_url + "/")
    expect(page.locator('.okf-card[data-concept-id="references/ledger_conventions"] .okf-card__desc')).to_have_text(
        "Every ledger-sync and settled payment.")
    _assert_clean(page)


@pytest.mark.parametrize("theme", ["bone", "slate"])
@pytest.mark.parametrize("forced", [False, True], ids=["normal", "forced-colors"])
def test_search_suggestion_selection_keeps_every_row_height(live_url: str, page, theme: str, forced: bool) -> None:
    page.add_init_script(f"localStorage.setItem('okf-loom.appearance', '{theme}')")
    if forced:
        page.emulate_media(forced_colors="active")
    page.goto(live_url + "/")
    search = page.locator(".okf-shell__search-form input[name='q']")
    search.fill("orders")
    live = page.get_by_role("listbox", name="Search suggestions")
    expect(live).to_be_visible()
    options = live.get_by_role("option")
    heights = options.evaluate_all("nodes => nodes.map(n => n.getBoundingClientRect().height)")
    assert len(heights) > 1
    for index in range(len(heights)):
        search.press("ArrowDown")
        expect(options.nth(index)).to_have_attribute("aria-selected", "true")
        expect(search).to_have_attribute("aria-activedescendant", options.nth(index).get_attribute("id"))
        assert options.evaluate_all("nodes => nodes.map(n => n.getBoundingClientRect().height)") == heights
        assert options.nth(index).evaluate("n => getComputedStyle(n, '::before').content") in ("none", "normal")


@pytest.mark.parametrize("query", QUERIES, ids=QUERY_IDS)
def test_live_search_page_box_treats_metacharacters_as_text(live_url: str, page, query: str) -> None:
    page.goto(live_url + "/__search")
    box = page.locator("#okf-search-query")
    expect(box).to_be_visible()
    box.fill(query)
    page.locator(".okf-search-submit").click()
    page.wait_for_url(lambda url: _query_of(url) == [query])
    expect(page.locator("#okf-search-query")).to_have_value(query)
    expect(page.locator("#okf-results-heading")).to_be_visible()
    expect(page.locator("#okf-search-query")).not_to_have_attribute("aria-invalid", "true")
    _assert_marks_are_query_terms(page, ".okf-search__results")
    _assert_clean(page)

    page.locator("#okf-search-query").fill(FOLLOW_UP)
    page.locator(".okf-search-submit").click()
    page.wait_for_url(lambda url: _query_of(url) == [FOLLOW_UP])
    expect(page.locator(".okf-search-result").first).to_be_visible()
    _assert_clean(page)


@pytest.mark.parametrize("width,height", [(768, 1024), (1024, 1366)])
def test_search_result_title_is_a_44px_coarse_target(live_url: str, page, width: int, height: int) -> None:
    context = page.context.browser.new_context(viewport={"width": width, "height": height}, has_touch=True)
    try:
        touch_page = context.new_page()
        touch_page.goto(live_url + "/__search?q=orders")
        title = touch_page.locator(".okf-search-result h3 > a").first
        expect(title).to_be_visible()
        box = title.bounding_box()
        assert box["height"] >= 44 and box["width"] >= 44, box
    finally:
        context.close()


def test_portable_search_verifies_evidence_on_plain_http_nonlocalhost(static_url: str, page) -> None:
    # Transport stays on our owned loopback server, but the browser evaluates
    # the document and its scripts as an ordinary, non-secure LAN origin.
    origin = static_url.replace("127.0.0.1", "portable.test")
    page.route(origin + "/**", lambda route: route.fulfill(
        response=route.fetch(url=route.request.url.replace(origin, static_url))))
    page.goto(origin + "/__search.html?q=orders")
    expect(page.locator("body")).to_have_attribute("data-okf-capture-ready", "ready")
    assert page.evaluate("() => isSecureContext") is False
    assert page.evaluate("() => !!crypto.subtle") is False
    expect(page.locator(".okf-search-result").first).to_be_visible()
    assert page.evaluate("() => window.okfPortableProvider.passages.length") > 0
    _assert_clean(page)


@pytest.mark.parametrize("query", QUERIES, ids=QUERY_IDS)
def test_static_search_page_box_treats_metacharacters_as_text(static_url: str, page, query: str) -> None:
    page.goto(static_url + "/__search.html")
    body = page.locator("body")
    expect(body).to_have_attribute("data-okf-capture-ready", "ready")
    box = page.locator("#okf-search-query")
    box.fill(query)
    page.locator(".okf-search-submit").click()
    page.wait_for_url(lambda url: _query_of(url) == [query])
    expect(body).to_have_attribute("data-okf-capture-ready", "ready")
    expect(page.locator("#okf-search-query")).to_have_value(query)
    expect(page.locator("#okf-search-query")).not_to_have_attribute("aria-invalid", "true")
    expect(page.locator("#okf-results-heading")).to_contain_text(f"for “{query.strip()}”")
    _assert_marks_are_query_terms(page, ".okf-search__results")
    _assert_clean(page)

    page.locator("#okf-search-query").fill(FOLLOW_UP)
    page.locator(".okf-search-submit").click()
    page.wait_for_url(lambda url: _query_of(url) == [FOLLOW_UP])
    expect(body).to_have_attribute("data-okf-capture-ready", "ready")
    expect(page.locator(".okf-search-result").first).to_be_visible()
    expect(page.locator(".okf-search__results mark").first).to_be_visible()
    _assert_marks_are_query_terms(page, ".okf-search__results")
    _assert_clean(page)
