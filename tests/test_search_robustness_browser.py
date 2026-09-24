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
