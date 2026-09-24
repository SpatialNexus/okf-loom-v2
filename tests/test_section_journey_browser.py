"""R1/R2 reading journey in a real browser across all four supported modes.

One focused journey per surface — live ``serve``, exported ``static``,
exported ``spa`` on a supported clean-URL host, and ``single-file``:

* search results carry the distinct, accessible-name-exact
  ``Open matching section`` action only for rows whose actual evidence
  passage both textually matches the query and has a revision-bound,
  source-owned rendered heading destination (graph-only rows keep plain
  ``Open``);
* the section href carries a bounded navigation INTENT (never the raw
  fragment), so the browser has nothing to native-scroll to until the
  displayed page admits the destination — unique real H1-H6 heading inside
  the displayed body, page source revision equal to the intent, renderer-
  owned heading text — and publishes the real hash itself with replaceState
  (exactly ONE history entry);
* stale search into a re-rendered document, missing, chrome, duplicate, and
  plugin-modified targets all fail closed to document-root reading with the
  unsafe fragment/intent cleared WITHOUT a history push;
* a RELOADED owned single-file section entry re-admits against the loaded
  artifact's destination revision map — the same independent proof initial
  activation requires. A rebuilt snapshot whose checkout source moved (the
  heading id and renderer label unchanged) and a snapshot whose map is
  absent both refuse: the owned fragment is cleared without a history push,
  the restored selection keeps root reading, and the rejected intent is
  never routed through the ordinary legacy-hash focus;
* the browser Back key is the one-step return everywhere: server-rendered
  and exported workbenches restore the search URL/list/query/mode natively,
  and the single-file workbench records its panel state (query, mode,
  filters) on the previous entry so Back reopens it exactly as left.

The spa export's root-absolute form/Clear targets (``/__search``) need a
clean-URL host, so this module owns one: a tiny extensionless-to-``.html``
mapping server — the same rewrite every supported static SPA host performs.
It is a finite, thread-owned fixture, not a product route. Ordinary Open
links are exercised by real clicks; duplicate-title, fence, Unicode, and
CRLF source cases are reused from the source suites instead of a Cartesian
browser explosion. Rows are located by rendered identity (concept id on
Python-rendered serve rows; destination href/text on JS-rendered rows).
``reduced_motion="reduce"`` makes ``scrollIntoView`` land instantly and
exercises the reduced-motion path.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from browser_support import (expect, launch_chromium, copy_bundle,
                             free_port, live_server, managed_server,
                             sync_playwright)
from conftest import okf_module_argv, okf_subprocess_env

pytestmark = pytest.mark.browser

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
DEMO_BUNDLE = TOOLKIT_ROOT / "samples" / "demo_bundle"

SECTION_ACTION = "Open matching section"
CHECKOUT_ROW = '[data-concept-id="services/checkout"]'
ORDERS_ROW = '[data-concept-id="tables/orders"]'
CHECKOUT_FRAGMENT = "overview"


class _CleanURLHandler(SimpleHTTPRequestHandler):
    """Map extensionless SPA routes to the exported ``.html`` pages."""

    def translate_path(self, path):
        resolved = super().translate_path(path)
        if Path(resolved).is_file():
            return resolved
        clean = urllib.parse.urlsplit(path).path
        if not clean or clean.endswith("/") or "." in Path(clean).name:
            return resolved
        return super().translate_path(clean + ".html")

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


def _build_export(tmp_path_factory, target: str, *,
                  bundle: Path = DEMO_BUNDLE) -> Path:
    out = tmp_path_factory.mktemp(f"journey-{target}")
    result = subprocess.run(
        okf_module_argv("build", str(bundle), "--target", target,
                        "--out", str(out)),
        cwd=str(TOOLKIT_ROOT), env=okf_subprocess_env(),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return out


@pytest.fixture(scope="session")
def journey_server_bundle(tmp_path_factory):
    return copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp("journey") / "bundle")


@pytest.fixture(scope="session")
def journey_server_url(journey_server_bundle):
    with live_server(journey_server_bundle, "--no-watch") as base:
        yield base


@pytest.fixture(scope="session")
def journey_static_dir(tmp_path_factory):
    return _build_export(tmp_path_factory, "static")


@pytest.fixture(scope="session")
def journey_static_url(journey_static_dir):
    out = journey_static_dir
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log = out.parent / f"static-{port}.log"
    argv = ["python", "-m", "http.server", str(port), "--bind", "127.0.0.1",
            "--directory", str(out)]
    with managed_server(argv, base, log):
        yield base


@pytest.fixture(scope="session")
def journey_spa_url(tmp_path_factory):
    """A supported clean-URL host for the exported spa surface."""
    out = _build_export(tmp_path_factory, "spa")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    handler = partial(_CleanURLHandler, directory=str(out))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/", timeout=5) as response:
            assert response.status == 200
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def journey_single_dir(tmp_path_factory):
    return _build_export(tmp_path_factory, "single-file")


@pytest.fixture(scope="session")
def journey_single_url(journey_single_dir):
    out = journey_single_dir
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log = out.parent / f"single-{port}.log"
    argv = ["python", "-m", "http.server", str(port), "--bind", "127.0.0.1",
            "--directory", str(out)]
    with managed_server(argv, base, log):
        yield base


@pytest.fixture(scope="session")
def journey_single_redeploy_dir(tmp_path_factory):
    """A genuinely REBUILT single-file snapshot whose checkout source moved.

    One added paragraph under ``# Overview`` moves the per-concept
    normalized-source revision while the heading id ``overview`` and its
    renderer label stay identical — the exact redeploy shape a stale owned
    history entry must refuse on reload.
    """
    bundle = copy_bundle(
        DEMO_BUNDLE, tmp_path_factory.mktemp("single-redeploy") / "bundle")
    source = bundle / "services" / "checkout.md"
    text = source.read_text(encoding="utf-8")
    marker = "and appends a row to the [orders table](/tables/orders.md).\n"
    assert marker in text
    source.write_text(
        text.replace(marker, marker
                     + "\nRedeployment proof: retry budgets moved to the\n"
                       "shared gateway between this search and this reload.\n",
                     1),
        encoding="utf-8")
    return _build_export(tmp_path_factory, "single-file", bundle=bundle)


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            context = browser.new_context(reduced_motion="reduce")
            page = context.new_page()
            console_errors: list[str] = []
            page.on("pageerror", lambda error: console_errors.append(str(error)))
            page.console_errors = console_errors
            yield page
            context.close()
        finally:
            browser.close()


def _wait_hydrated(page) -> None:
    page.wait_for_function(
        "() => window.okfPortableProvider"
        " && document.body.dataset.okfCaptureReady === 'ready'"
        " && !document.querySelector('.okf-search-error')"
    )


def _assert_no_console_errors(page) -> None:
    assert not page.console_errors, page.console_errors


def _chrome_bottom(page) -> float:
    """Measured sticky chrome floor (topbar plus document viewbar)."""
    return page.evaluate("""() => {
      let bottom = 0;
      document.querySelectorAll('.okf-topbar, .okf-concept-viewbar').forEach(node => {
        if (node.getClientRects().length) {
          bottom = Math.max(bottom, node.getBoundingClientRect().bottom);
        }
      });
      return bottom;
    }""")


def _assert_admitted_landing(page, expected_path: str, fragment: str) -> None:
    """The published hash, real heading node focused, below measured chrome."""
    heading = page.locator(f".okf-page__body [id='{fragment}']").first
    expect(heading).to_be_visible()
    page.wait_for_function(
        "([fragment, expectedPath]) => location.pathname === expectedPath"
        " && location.hash === '#' + encodeURIComponent(fragment)"
        " && document.activeElement"
        " && document.activeElement.id === fragment",
        arg=[fragment, expected_path],
    )
    identity = page.evaluate(
        """([fragment, expectedPath]) => {
             const bodyNode = document.querySelector('.okf-page__body');
             const owned = bodyNode
               ? bodyNode.querySelector('#' + CSS.escape(fragment)) : null;
             return {activeTag: document.activeElement
               && document.activeElement.tagName,
               sameNode: document.activeElement === owned,
               hash: location.hash, path: location.pathname,
               search: location.search}}""",
        [fragment, expected_path])
    assert identity["path"] == expected_path, identity
    assert identity["search"] == "", identity
    assert identity["hash"] == f"#{fragment}", identity
    assert identity["sameNode"], (
        "focus must be the exact displayed heading DOM node, got "
        f"{identity}")
    assert identity["activeTag"] in {f"H{i}" for i in range(1, 7)}
    target = heading.evaluate(
        "el => {const r = el.getBoundingClientRect();"
        "  return {top: r.top, height: r.height,"
        "           inViewport: r.bottom > 0 && r.top < window.innerHeight};}")
    chrome = _chrome_bottom(page)
    assert target["top"] >= chrome - 1, (
        f"heading top {target['top']} must clear sticky chrome floor {chrome}")
    assert target["inViewport"], target


def _assert_root_landing(page, expected_path: str) -> None:
    """Document-root reading: intent/fragment cleared, no scroll, no focus."""
    identity = page.evaluate(
        """expectedPath => ({
             url: location.pathname + location.search + location.hash,
             path: location.pathname, scrollY: window.scrollY,
             active: document.activeElement && document.activeElement.tagName,
             intent: location.search.indexOf('okf-section') >= 0})""",
        expected_path)
    assert identity["path"] == expected_path, identity
    assert not identity["intent"] and identity["url"] == expected_path, identity
    assert identity["scrollY"] == 0, identity
    assert identity["active"] == "BODY", identity


def _normalized_heading_label(page, fragment: str) -> str | None:
    """The renderer-owned label of a displayed heading, anchors stripped."""
    return page.evaluate(
        """fragment => {
             const body = document.getElementById('detail-body');
             const heading = body
               && body.querySelector('[id="' + CSS.escape(fragment) + '"]');
             if (!heading) return null;
             const copy = heading.cloneNode(true);
             copy.querySelectorAll('.okf-heading-anchor')
               .forEach(a => a.remove());
             return (copy.textContent || '')
               .replace(/\\s+/g, ' ').trim(); }""",
        fragment)


def _assert_search_restored(page, base: str, query: str, *,
                            hydrated: bool) -> None:
    """One Back: same search URL, rows, query and mode intact."""
    assert page.url == (
        f"{base}/__search?q={urllib.parse.quote(query)}&mode=lexical"
        "&type=Service&tag=checkout")
    if hydrated:
        _wait_hydrated(page)
    # Direct children only: each result row may embed nested lists.
    assert page.locator(".okf-search-results-list > li").count() == 1
    expect(page.locator("#okf-search-query")).to_have_value(query)
    expect(page.locator("#okf-search-type")).to_have_value("Service")
    expect(page.locator("#okf-search-tag")).to_have_value("checkout")
    checked = page.locator('#okf-search-mode input[name="mode"]:checked')
    assert checked.get_attribute("value") == "lexical"


def _go_to_search(page, base: str, query: str = "checkout",
                  mode: str = "lexical", *, static: bool = False,
                  filtered: bool = False) -> None:
    page.goto(
        f"{base}/{'__search.html' if static else '__search'}"
        f"?q={urllib.parse.quote(query)}&mode={mode}"
        + ("&type=Service&tag=checkout" if filtered else ""),
        wait_until="domcontentloaded")


# --- live serve ---------------------------------------------------------------


def test_serve_section_intent_journey_back_and_open_root(
    journey_server_url, page,
) -> None:
    base = journey_server_url
    _go_to_search(page, base, filtered=True)
    assert page.locator(".okf-search-results-list > li").count() == 1
    # Ordinary Open stays document-root; the new action is distinct, exact,
    # and carries a bounded navigation INTENT — never the raw fragment.
    open_link = page.locator(f"{CHECKOUT_ROW} a.okf-search-open")
    assert open_link.get_attribute("href") == "/services/checkout"
    section = page.locator(f"{CHECKOUT_ROW} a.okf-search-open-section")
    expect(section).to_have_accessible_name(SECTION_ACTION)
    href = section.get_attribute("href")
    assert href is not None and href.startswith("/services/checkout?")
    assert "okf-section=overview" in href
    assert "okf-rev=" in href and "okf-h=" in href
    assert "#" not in href, "the raw fragment must not be published pre-admission"

    history_before = page.evaluate("history.length")
    with page.expect_navigation(wait_until="domcontentloaded"):
        section.click()
    # Admission publishes the real hash via replaceState — still ONE entry.
    _assert_admitted_landing(page, "/services/checkout", CHECKOUT_FRAGMENT)
    assert page.evaluate("history.length") == history_before + 1, (
        "the section action must add exactly one history entry")
    _assert_no_console_errors(page)

    page.go_back(wait_until="domcontentloaded")
    _assert_search_restored(page, base, "checkout", hydrated=False)
    assert page.evaluate("history.length") == history_before + 1, (
        "Back must restore without duplicating entries")

    # Ordinary Open is actually clicked: plain document-root reading.
    _go_to_search(page, base)
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.locator(f"{CHECKOUT_ROW} a.okf-search-open").click()
    assert page.url == f"{base}/services/checkout"
    _assert_root_landing(page, "/services/checkout")
    expect(page.locator(".okf-page__title")).to_have_text("Checkout Service")

    # format=json keeps the strict public wire: no section metadata anywhere.
    _go_to_search(page, base)
    payload = page.evaluate(
        "async () => (await (await fetch('/__search?q=checkout&mode=lexical"
        "&format=json')).json())")
    assert isinstance(payload, list) and payload
    for row in payload:
        assert not any("section" in key for key in row)

    # Graph-only evidence never claims a section: in relation mode the
    # Orders row's attached Schema passage has no textual query term, so the
    # same page contrasts an Open-only row against Checkout's section link.
    _go_to_search(page, base, mode="relation")
    assert page.locator(f"{ORDERS_ROW}").count() == 1
    assert page.locator(f"{ORDERS_ROW} a.okf-search-open-section").count() == 0
    assert page.locator(f"{ORDERS_ROW} a.okf-search-open").count() == 1
    assert page.locator(
        f"{CHECKOUT_ROW} a.okf-search-open-section").count() == 1


def test_static_stale_search_into_redeployed_document_fails_closed(
    journey_static_dir, journey_static_url, page,
) -> None:
    """The destination artifact changed between search and click: same id,
    same label, different stamped source revision — the exact case native
    fragment navigation cannot refuse. The intent is refused and cleared."""
    base = journey_static_url
    _go_to_search(page, base, static=True)
    section = page.locator(
        'a.okf-search-open-section[href*="services/checkout"]')
    expect(section).to_be_visible()
    original = section.get_attribute("href")
    intent_revision = urllib.parse.parse_qs(
        urllib.parse.urlparse("?" + original.split("?", 1)[1]).query
    )["okf-rev"][0]
    # Redeploy the destination document (a fresh build of changed source):
    # its per-concept source revision moves while the heading id and label
    # stay identical. The export is shared by the whole session — restore it
    # afterwards.
    deployed = journey_static_dir / "services" / "checkout.html"
    html = deployed.read_text(encoding="utf-8")
    marker = f'"source_revision":"{intent_revision}"'
    assert marker in html
    deployed.write_text(
        html.replace(marker, '"source_revision":"' + "f" * 64 + '"', 1),
        encoding="utf-8")
    try:
        history_before = page.evaluate("history.length")
        with page.expect_navigation(wait_until="domcontentloaded"):
            section.click()
        identity = page.evaluate("""() => ({
            path: location.pathname, search: location.search,
            hash: location.hash, scrollY: window.scrollY,
            active: document.activeElement && document.activeElement.tagName,
            headingText: (() => { const h = document.querySelector(
                '.okf-page__body #overview'); if (!h) return null;
              const c = h.cloneNode(true);
              c.querySelectorAll('.okf-heading-anchor').forEach(a => a.remove());
              return (c.textContent || '').replace(/\\s+/g, ' ').trim(); })()})""")
    finally:
        deployed.write_text(html, encoding="utf-8")
    assert identity["path"] == "/services/checkout.html"
    assert identity["search"] == "" and identity["hash"] == "", identity
    assert identity["scrollY"] == 0
    assert identity["active"] == "BODY"
    # The heading id AND renderer label are unchanged in the redeployed
    # artifact — but focus and the published hash must never follow an
    # unadmitted destination (the revision binding refuses it here).
    assert identity["headingText"] == "Overview"
    assert page.evaluate("history.length") == history_before + 1
    expect(page.locator(".okf-page__title")).to_have_text("Checkout Service")


def _outline_proof(page) -> dict:
    """The destination page's own rendered-outline admission proof."""
    proof = page.evaluate("""() => {
      const el = document.getElementById('okf-rendered-outline');
      return el ? JSON.parse(el.textContent) : null; }""")
    assert proof and proof["source_revision"] and proof["concept"]
    return proof


def test_serve_unsafe_intents_fail_closed_to_root(journey_server_url, page) -> None:
    base = journey_server_url
    # A real concept page supplies its own outline proof: per-concept source
    # revision plus canonical concept identity.
    page.goto(f"{base}/services/checkout", wait_until="domcontentloaded")
    proof = _outline_proof(page)
    revision = proof["source_revision"]
    concept = urllib.parse.quote(proof["concept"], safe="")

    def intent(fragment: str, rev: str | None = None,
               hash_: str | None = None, cid: str | None = None) -> str:
        return (f"/services/checkout?okf-section={urllib.parse.quote(fragment)}"
                f"&okf-rev={urllib.parse.quote(rev or revision, safe='')}"
                f"&okf-c={cid or concept}"
                f"&okf-h={hash_ or '0' * 16}")

    cases = [
        # Missing destination id.
        ("missing", intent("nonexistent-heading")),
        # Chrome identity: real element, but not a heading in the body.
        ("chrome", intent("okf-document-details")),
        # Wrong revision binding (stale intent into a re-rendered document).
        ("stale", intent("overview", rev="r" * 24)),
        # Malformed label hash.
        ("badhash", intent("overview", hash_="zzz")),
        # Duplicate intent keys: the whole intent is rejected, never
        # 'first value wins'.
        ("duplicate", "/services/checkout?okf-section=overview&okf-section=overview"
                      f"&okf-rev={urllib.parse.quote(revision, safe='')}&okf-c={concept}"
                      f"&okf-h={'0' * 16}"),
        # Identity mismatch: an intent minted for another concept never
        # admits on this page, even with valid-looking revision and hash.
        ("otherdoc", intent("overview", cid="tables%2Forders")),
    ]
    for name, path in cases:
        page.goto(base + path, wait_until="domcontentloaded")
        page.wait_for_timeout(200)
        _assert_root_landing(page, "/services/checkout")
        expect(page.locator(".okf-page__title")).to_have_text("Checkout Service")


@pytest.mark.parametrize("proof_fault", ["absent", "unknown-key", "oversize"])
def test_serve_missing_outline_proof_rejects_valid_hash_intent(
    journey_server_url, page, proof_fault,
) -> None:
    """A removed destination proof refuses the intent even when the caller
    supplies a perfectly valid revision, identity, and label hash: the hash
    is a drift binding, never authorization, and the allowlist is mandatory."""
    base = journey_server_url
    page.goto(f"{base}/__search?q=checkout&mode=lexical",
              wait_until="domcontentloaded")
    section = page.locator(f"{CHECKOUT_ROW} a.okf-search-open-section")
    href = section.get_attribute("href")
    assert href and "okf-section=" in href
    # Strip the outline proof from every subsequent document before its
    # parser runs (init scripts execute before page scripts).
    page.add_init_script("const proofFault = " + json.dumps(proof_fault) + ";" + """
    (() => {
      const obs = new MutationObserver(() => {
        const el = document.getElementById('okf-rendered-outline');
        if (!el || el.dataset.tampered) return;
        if (proofFault === 'absent') el.remove();
        else {
          const proof = JSON.parse(el.textContent);
          el.dataset.tampered = 'true';
          if (proofFault === 'unknown-key') proof.extra = true;
          else proof.headings.extra = 'x'.repeat(128 * 1024);
          const canonical = value => value && typeof value === 'object'
            ? '{' + Object.keys(value).sort().map(key =>
                JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
            : JSON.stringify(value);
          el.textContent = canonical(proof);
        }
      });
      obs.observe(document, {childList: true, subtree: true});
    })();""")
    page.goto(base + href, wait_until="domcontentloaded")
    page.wait_for_timeout(200)
    _assert_root_landing(page, "/services/checkout")
    if proof_fault == "absent":
        assert page.evaluate(
            "document.getElementById('okf-rendered-outline')") is None


def test_serve_duplicate_and_plugin_modified_headings_fail_closed(
    journey_server_url, page,
) -> None:
    base = journey_server_url
    page.goto(f"{base}/services/checkout", wait_until="domcontentloaded")
    proof = _outline_proof(page)
    revision = proof["source_revision"]
    concept = urllib.parse.quote(proof["concept"], safe="")
    real_hash = page.evaluate("""() => {
      const h = document.querySelector('.okf-page__body #overview');
      const label = () => { const c = h.cloneNode(true);
        c.querySelectorAll('.okf-heading-anchor').forEach(a => a.remove());
        return (c.textContent || '').replace(/\\s+/g, ' ').trim(); };
      let d = 0xcbf29ce484222325n;
      for (const b of new TextEncoder().encode(label())) {
        d ^= BigInt(b); d = (d * 0x100000001b3n) & 0xffffffffffffffffn; }
      return d.toString(16).padStart(16, '0'); }""")
    assert real_hash

    def intent(fragment_id: str) -> str:
        return (f"/services/checkout?okf-section={urllib.parse.quote(fragment_id)}"
                f"&okf-rev={urllib.parse.quote(revision, safe='')}"
                f"&okf-c={concept}&okf-h={real_hash}")

    # Control first, before any tamper scripts are registered: with an
    # untouched document the same real binding admits.
    page.goto(base + intent("overview"), wait_until="domcontentloaded")
    _assert_admitted_landing(page, "/services/checkout", CHECKOUT_FRAGMENT)

    # Duplicate: a client script keeps a same-id clone of the heading alive
    # before admission — the id is no longer uniquely addressable. The tamper
    # re-applies after any body re-render so it is genuinely present when the
    # destination is admitted.
    page.add_init_script("""
    (() => {
      let rounds = 0;
      const obs = new MutationObserver(() => {
        if (rounds++ > 80) { obs.disconnect(); return; }
        const body = document.querySelector('.okf-page__body');
        const h = body && body.querySelector('#overview');
        if (!h) return;
        if (body.querySelectorAll('#overview').length < 2) {
          const clone = h.cloneNode(true);
          clone.removeAttribute('data-okf-dup');
          clone.dataset.okfDup = '1';
          h.parentNode.insertBefore(clone, h.nextSibling);
        }
      });
      obs.observe(document, {childList: true, subtree: true});
    })();""")
    page.goto(base + intent("overview"), wait_until="domcontentloaded")
    page.wait_for_timeout(200)
    _assert_root_landing(page, "/services/checkout")
    assert page.evaluate(
        "document.querySelectorAll('.okf-page__body #overview').length") == 2

    # Plugin-modified: a client script keeps the heading text renamed after
    # render — the renderer-owned label binding no longer matches the DOM.
    page.add_init_script("""
    (() => {
      let rounds = 0;
      const obs = new MutationObserver(() => {
        if (rounds++ > 80) { obs.disconnect(); return; }
        const h = document.querySelector('.okf-page__body #overview');
        if (!h) return;
        if (h.textContent.indexOf('edited by plugin') === -1) {
          h.textContent = 'Overview (edited by plugin)';
        }
      });
      obs.observe(document, {childList: true, subtree: true});
    })();""")
    page.goto(base + intent("overview"), wait_until="domcontentloaded")
    page.wait_for_timeout(200)
    _assert_root_landing(page, "/services/checkout")
    assert page.evaluate(
        "document.querySelector('.okf-page__body #overview').textContent"
    ).strip().startswith("Overview (edited by plugin)")


# --- exported static ----------------------------------------------------------


def test_static_search_section_journey_and_back(journey_static_url, page) -> None:
    base = journey_static_url
    _go_to_search(page, base, static=True, filtered=True)
    _wait_hydrated(page)
    section = page.locator(
        'a.okf-search-open-section[href*="services/checkout"]')
    expect(section).to_have_accessible_name(SECTION_ACTION)
    href = section.get_attribute("href")
    assert href is not None
    assert href.startswith("services/checkout.html?okf-section=overview")
    assert "okf-rev=" in href and "okf-h=" in href and "#" not in href
    assert page.locator(
        'a.okf-search-open-section[href*="tables/orders"]').count() == 0
    history_before = page.evaluate("history.length")
    with page.expect_navigation(wait_until="domcontentloaded"):
        section.click()
    _assert_admitted_landing(page, "/services/checkout.html", CHECKOUT_FRAGMENT)
    assert page.evaluate("history.length") == history_before + 1
    _assert_no_console_errors(page)
    page.go_back(wait_until="domcontentloaded")
    assert page.url == (f"{base}/__search.html?q=checkout&mode=lexical"
                        "&type=Service&tag=checkout")
    _wait_hydrated(page)
    assert page.locator(".okf-search-results-list > li").count() == 1
    expect(page.locator("#okf-search-query")).to_have_value("checkout")
    expect(page.locator("#okf-search-type")).to_have_value("Service")
    expect(page.locator("#okf-search-tag")).to_have_value("checkout")
    expect(page.locator('#okf-search-mode input[value="lexical"]')).to_be_checked()


# --- exported spa on a supported clean-URL host -------------------------------


def test_spa_clean_url_deep_route_section_journey_apply_clear_and_back(
    journey_spa_url, page,
) -> None:
    base = journey_spa_url
    # Deep route: a clean-URL concept page must load and read on its own.
    page.goto(f"{base}/services/checkout", wait_until="domcontentloaded")
    expect(page.locator(".okf-page__body [id='overview']")).to_be_visible()

    _go_to_search(page, base, filtered=True)
    _wait_hydrated(page)
    section = page.locator(
        'a.okf-search-open-section[href*="services/checkout"]')
    expect(section).to_have_accessible_name(SECTION_ACTION)
    href = section.get_attribute("href")
    assert href is not None and href.startswith("/services/checkout?")
    assert "okf-section=overview" in href and "okf-rev=" in href
    assert "okf-h=" in href and "#" not in href
    assert page.locator(
        'a.okf-search-open-section[href*="tables/orders"]').count() == 0
    history_before = page.evaluate("history.length")
    with page.expect_navigation(wait_until="domcontentloaded"):
        section.click()
    _assert_admitted_landing(page, "/services/checkout", CHECKOUT_FRAGMENT)
    assert page.evaluate("history.length") == history_before + 1
    _assert_no_console_errors(page)
    page.go_back(wait_until="domcontentloaded")
    _assert_search_restored(page, base, "checkout", hydrated=True)

    # U8 on the same supported host: Apply carries filters through a real
    # clean-URL navigation, and Clear returns the honest zero state.
    page.goto(f"{base}/__search", wait_until="domcontentloaded")
    _wait_hydrated(page)
    summary = page.locator("details.okf-search-filters > summary")
    assert summary.inner_text().strip() == "Filters"
    if not page.locator("details.okf-search-filters").evaluate("el => el.open"):
        summary.click()
    page.fill("#okf-search-type", "Service")
    page.fill("#okf-search-tag", "checkout")
    page.fill("#okf-search-query", "checkout")
    with page.expect_navigation(wait_until="domcontentloaded") as nav:
        page.get_by_role("button", name="Apply", exact=True).click()
    assert nav.value.ok
    assert "type=Service" in page.url and "tag=checkout" in page.url, page.url
    _wait_hydrated(page)
    assert summary.inner_text().strip() == "Filters, 2 active"
    assert page.locator(".okf-search-results-list > li").count() == 1
    assert "Checkout Service" in page.locator(
        ".okf-search-results-list > li").first.inner_text()
    with page.expect_navigation(wait_until="domcontentloaded") as nav:
        page.locator(".okf-search-filter-actions").get_by_role(
            "link", name="Clear").click()
    assert nav.value.ok and page.url.endswith("/__search")
    _wait_hydrated(page)
    assert summary.inner_text().strip() == "Filters"
    assert page.locator(".okf-search-results-list > li").count() == 0


# --- single-file ---------------------------------------------------------------


def test_single_file_section_selection_one_entry_back_and_forward(
    journey_single_url, page,
) -> None:
    base = journey_single_url
    page.goto(base + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.body.dataset.okfSearchCaptureReady === 'ready'"
        " && !document.getElementById('okf-single-search-toggle').disabled")
    toggle = page.locator("#okf-single-search-toggle")
    toggle.click()
    page.fill("#okf-single-query", "checkout")
    page.locator(
        "#okf-single-search .okf-search-filters > summary").click()
    page.fill("#okf-single-search input[name='type']", "Service")
    submit = page.locator("#okf-single-search form button[type='submit']")
    submit.click()
    # Mode-independent invariant: graph-only evidence (no exact passage,
    # rendered as .okf-search-concept-rationale) never claims a section.
    page.locator(
        "#okf-single-search input[name='mode'][value='relation']").check()
    submit.click()
    graph_only = page.locator(
        "[data-single-results] > li").filter(
        has=page.locator(".okf-search-concept-rationale"))
    for i in range(graph_only.count()):
        assert graph_only.nth(i).locator(
            "a.okf-search-open-section").count() == 0
    page.locator(
        "#okf-single-search input[name='mode'][value='lexical']").check()
    submit.click()
    # In the single-file workbench the section href is a valid viewer URL
    # owned by the existing selected state — never '#detail-content#...'.
    checkout_row = page.locator("[data-single-results] > li").filter(
        has=page.locator('a[data-graph-concept="services/checkout"]'))
    assert checkout_row.count() == 1
    section = checkout_row.first.locator("a.okf-search-open-section")
    expect(section).to_have_accessible_name(SECTION_ACTION)
    assert section.get_attribute("href") == (
        "?selected=services%2Fcheckout#overview")
    history_before = page.evaluate("history.length")
    section.click()
    # Exactly one entry; the panel closed; the real heading NODE inside the
    # displayed document owns focus, below the measured sticky chrome.
    expect(page.locator("#okf-single-search")).to_be_hidden()
    body_node = page.locator(
        "#detail-body[data-concept-id='services/checkout']")
    expect(body_node).to_be_visible()
    expect(body_node.locator(f"[id='{CHECKOUT_FRAGMENT}']")).to_be_visible()
    page.wait_for_function(
        "() => location.search === '?selected='"
        " + encodeURIComponent('services/checkout')"
        " && location.hash === '#overview'")
    identity = page.evaluate("""() => {
      const bodyNode = document.getElementById('detail-body');
      const owned = bodyNode.querySelector('#overview');
      let bottom = 0;
      document.querySelectorAll('.okf-topbar').forEach(node => {
        bottom = Math.max(bottom, node.getBoundingClientRect().bottom); });
      const r = owned.getBoundingClientRect();
      return {sameNode: document.activeElement === owned,
              tag: owned.tagName, top: r.top, chrome: bottom,
              entries: history.length};}""")
    # In the single-file detail panel the concept's "Overview" heading is an
    # H2 section heading below the (visually hidden) H1 document title.
    assert identity["sameNode"] and identity["tag"] == "H2", identity
    assert identity["top"] >= identity["chrome"] - 1, identity
    assert page.evaluate("history.length") == history_before + 1

    # The browser Back key is the one-step return: the panel reopens with
    # its ACTUAL query, type filter, and mode, and the owned list matches.
    page.go_back(wait_until="domcontentloaded")
    expect(page.locator("#okf-single-search")).to_be_visible()
    assert page.locator("#okf-single-query").input_value() == "checkout"
    assert page.locator(
        "#okf-single-search input[name='type']").input_value() == "Service"
    checked = page.locator("#okf-single-search input[name='mode']:checked")
    assert checked.get_attribute("value") == "lexical"
    assert page.locator("[data-single-results] > li").count() == 1
    assert page.evaluate("history.length") == history_before + 1
    _assert_no_console_errors(page)

    # Forward re-enters the section destination through the URL owner: the
    # selection is restored and the fragment is re-admitted.
    page.go_forward(wait_until="domcontentloaded")
    expect(page.locator("#okf-single-search")).to_be_hidden()
    page.wait_for_function(
        "() => location.hash === '#overview'"
        " && document.activeElement"
        " && document.activeElement.id === 'overview'")


SINGLE_SOURCE_REVISIONS_SCRIPT = (
    r'<script type="application/json" id="okf-single-source-revisions">'
    r'(.*?)</script>')


@pytest.mark.parametrize("proof", ["changed", "absent"])
def test_single_file_reload_restoration_readmits_owned_revision_proof(
    journey_single_dir, page, tmp_path, proof, request,
) -> None:
    """A reload of an OWNED single-file section entry re-admits the stamp.

    ``history.state`` (with ``okfSectionIntent``) survives a reload of the
    same URL. The restoration owner must re-admit it against the loaded
    artifact's independently parsed destination revision map — exact entry
    for the selected canonical concept, exact revision, then label re-proof
    in the displayed body — exactly like initial activation. A rebuilt
    snapshot whose checkout source moved (heading id and label still
    identical) and a snapshot whose revision map is absent both refuse: the
    owned fragment is cleared WITHOUT a history push, the restored selection
    keeps root reading, and the rejected intent never reaches the ordinary
    legacy-hash focus. The heading itself stays present, unique, and
    identically labelled, so refusal is the revision proof alone.
    """
    base_html = (journey_single_dir / "index.html").read_text(
        encoding="utf-8")
    deploy = tmp_path / "reload-deploy"
    deploy.mkdir()
    (deploy / "index.html").write_text(base_html, encoding="utf-8")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log = tmp_path / f"single-reload-{port}.log"
    argv = ["python", "-m", "http.server", str(port), "--bind", "127.0.0.1",
            "--directory", str(deploy)]
    with managed_server(argv, base, log):
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.body.dataset.okfSearchCaptureReady === 'ready'"
            " && !document.getElementById('okf-single-search-toggle').disabled")
        page.locator("#okf-single-search-toggle").click()
        page.fill("#okf-single-query", "checkout")
        page.locator(
            "#okf-single-search form button[type='submit']").click()
        checkout_row = page.locator("[data-single-results] > li").filter(
            has=page.locator('a[data-graph-concept="services/checkout"]'))
        assert checkout_row.count() == 1
        section = checkout_row.first.locator("a.okf-search-open-section")
        expect(section).to_be_visible()
        section.click()
        expect(page.locator("#okf-single-search")).to_be_hidden()
        page.wait_for_function(
            "() => location.search === '?selected='"
            " + encodeURIComponent('services/checkout')"
            " && location.hash === '#overview'"
            " && document.activeElement"
            " && document.activeElement.id === 'overview'")
        intent = page.evaluate(
            "history.state && history.state.okfSectionIntent || null")
        assert intent and intent["fragment"] == CHECKOUT_FRAGMENT, intent
        assert intent["heading"] == "Overview" and intent["revision"], intent
        # The minted stamp is bound to the artifact actually served here.
        served = re.search(SINGLE_SOURCE_REVISIONS_SCRIPT, base_html)
        assert served, "the export must carry the destination revision map"
        served_revisions = json.loads(served.group(1))["revisions"]
        assert served_revisions["services/checkout"] == intent["revision"]

        entries_before_reload = page.evaluate("history.length")
        if proof == "changed":
            redeploy_dir = request.getfixturevalue(
                "journey_single_redeploy_dir")
            replacement = (redeploy_dir / "index.html").read_text(
                encoding="utf-8")
            assert "retry budgets moved" in replacement, (
                "the rebuild must embed the changed checkout source")
            rebuilt = re.search(SINGLE_SOURCE_REVISIONS_SCRIPT, replacement)
            assert rebuilt
            assert json.loads(rebuilt.group(1))[
                "revisions"]["services/checkout"] != intent["revision"], (
                "the rebuild must move the checkout source revision while "
                "the heading id and label stay identical")
        else:
            replacement = re.sub(
                r'<script type="application/json"'
                r' id="okf-single-source-revisions">.*?</script>',
                "", base_html, flags=re.DOTALL)
            assert 'id="okf-single-source-revisions"' not in replacement
        (deploy / "index.html").write_text(replacement, encoding="utf-8")

        # Reload the SAME owned history entry: the URL (query + hash) and
        # the stamped history state are restored by the browser unchanged.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.body.dataset.okfSearchCaptureReady === 'ready'")
        # Stale or absent destination revision proof: the owned fragment is
        # cleared from the owned entry and the heading never takes focus —
        # including not through the ordinary document-local hash path.
        page.wait_for_function("() => location.hash === ''", timeout=10000)
        identity = page.evaluate("""() => ({
            hash: location.hash,
            search: location.search,
            focusedHeading: !!(document.activeElement
              && document.activeElement.id === 'overview'),
            active: document.activeElement
              && (document.activeElement.id || document.activeElement.tagName),
            bodyConcept: (() => { const b =
                document.getElementById('detail-body');
              return b ? b.getAttribute('data-concept-id') : null; })(),
            overviewCount: (() => { const b =
                document.getElementById('detail-body');
              return b ? b.querySelectorAll('#overview').length : 0; })(),
            entries: history.length})""")
        expect(page.locator(
            "#detail-body[data-concept-id='services/checkout']")
        ).to_be_visible()
        assert identity["bodyConcept"] == "services/checkout", identity
        assert identity["search"] == (
            "?selected=" + urllib.parse.quote("services/checkout", safe=""))
        assert identity["entries"] == entries_before_reload, (
            "the refusal must clear the fragment without adding history")
        # The destination is retained in the loaded artifact: one heading
        # with the unchanged renderer label — refusal is the revision
        # proof alone, never a missing, renamed, or duplicated target.
        assert identity["overviewCount"] == 1, identity
        assert _normalized_heading_label(
            page, CHECKOUT_FRAGMENT) == "Overview"
        assert identity["hash"] == "" and not identity["focusedHeading"], (
            f"stale {proof} proof must not focus or keep the owned fragment")
        _assert_no_console_errors(page)


def test_single_file_duplicate_target_fails_closed_without_focus(
    journey_single_url, page,
) -> None:
    base = journey_single_url
    page.add_init_script("""
    (() => {
      const obs = new MutationObserver(() => {
        const body = document.getElementById('detail-body');
        const h = body && body.querySelector('#overview');
        if (h && !h.dataset.okfDuplicated) {
          h.dataset.okfDuplicated = '1';
          obs.disconnect();
          const clone = h.cloneNode(true);
          h.parentNode.insertBefore(clone, h.nextSibling);
        }
      });
      obs.observe(document, {childList: true, subtree: true});
    })();""")
    page.goto(base + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.body.dataset.okfSearchCaptureReady === 'ready'"
        " && !document.getElementById('okf-single-search-toggle').disabled")
    toggle = page.locator("#okf-single-search-toggle")
    toggle.click()
    page.fill("#okf-single-query", "checkout")
    page.locator(
        "#okf-single-search form button[type='submit']").click()
    checkout_row = page.locator("[data-single-results] > li").filter(
        has=page.locator('a[data-graph-concept="services/checkout"]'))
    assert checkout_row.count() == 1
    section = checkout_row.first.locator("a.okf-search-open-section")
    expect(section).to_be_visible()
    section.click()
    # The selection still happens (root reading of the same document), but
    # the unaddressable duplicate fragment is cleared and never focused.
    expect(page.locator("#okf-single-search")).to_be_hidden()
    page.wait_for_function(
        "() => location.search === '?selected='"
        " + encodeURIComponent('services/checkout')"
        " && location.hash === ''")
    identity = page.evaluate("""() => ({
        duplicates: document.getElementById('detail-body')
          .querySelectorAll('#overview').length,
        focusedHeading: document.activeElement
          && document.activeElement.id === 'overview'})""")
    assert identity["duplicates"] == 2, identity
    assert not identity["focusedHeading"], identity


def test_single_file_plain_detail_content_hash_is_preserved(
    journey_single_url, page,
) -> None:
    """The single-file concept list deep-links with a plain
    ``?selected=<id>#detail-content`` hash: an ordinary document-local
    destination owned by the graph URL owner — never a section intent, so
    the hash must survive (no strip) while the selection restores."""
    base = journey_single_url
    page.goto(base + "/?selected=services%2Fcheckout#detail-content",
              wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.body.dataset.okfSearchCaptureReady === 'ready'")
    body_node = page.locator(
        "#detail-body[data-concept-id='services/checkout']")
    expect(body_node).to_be_visible()
    identity = page.evaluate("""() => ({
        hash: location.hash,
        search: location.search,
        active: document.activeElement
          && (document.activeElement.id || document.activeElement.tagName)})""")
    assert identity["hash"] == "#detail-content", identity
    assert identity["search"] == "?selected=services%2Fcheckout", identity
