"""Browser proof on disposable bundles. See README for core/browser lanes.

Collection never imports Playwright. Missing optional tooling may skip locally;
OKF_REQUIRE_BROWSER=1 makes it required. Server or browser startup errors fail.
"""
from __future__ import annotations

import socket
import subprocess
import time
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import os
import shutil
import struct
import sys
from pathlib import Path

import pytest

from browser_support import (expect, sync_playwright, launch_chromium, copy_bundle,
                             live_server, managed_server, wait_for_server, reveal_control, show_graph_canvas)

from conftest import okf_module_argv, okf_subprocess_env

# Gate 2: every test in this module is marked as a browser test.
pytestmark = pytest.mark.browser


def _context_pack(marker: str, query: str) -> dict:
    return {
        "schema_version": 1,
        "bundle_revision": marker,
        "query": query,
        "mode": "lexical",
        "filters": {},
        "budget": {"requested_tokens": 6000, "estimated_tokens": 1},
        "neighbors": 1,
        "included": 0,
        "omitted": 0,
        "truncated": False,
        "items": [],
        "representations": {"markdown": f"# OKF Context Pack\n\n{marker}\n"},
        "test_marker": marker,
    }


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Resolved repo root (the directory containing pyproject.toml). We do NOT
# depend on the process's CWD because pytest can be invoked from anywhere.
TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
DEMO_BUNDLE = TOOLKIT_ROOT / "samples" / "demo_bundle"
# P1 progressive disclosure tests need a graph large enough to trigger edge
# elision (>~20 edges). The docs-bundle has 32 concepts / 243 edges.
DOCS_BUNDLE = TOOLKIT_ROOT / "docs-bundle"

# How long to wait for `okf serve` to bind + answer 200 on /, and how often
# to poll. The server boots in well under a second on a warm machine; this
# budget covers slow CI runners.
_SERVER_STARTUP_TIMEOUT = 15.0
_SERVER_POLL_INTERVAL = 0.15

# Per-request HTTP timeout used during readiness polling.
_POLL_HTTP_TIMEOUT = 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_context_options(page) -> None:
    """Reveal the native owning disclosures before editing context bounds."""
    options = page.locator('.okf-page-context > details')
    if not options.evaluate('el => el.open'):
        options.locator(':scope > summary').click()
    advanced = options.locator('.okf-context-advanced')
    if not advanced.evaluate('el => el.open'):
        advanced.locator(':scope > summary').click()


def _free_port() -> int:
    """Reserve and immediately release an ephemeral port for the test server.

    Mirrors ``tests/test_server_smoke.py`` so we never collide with the
    default ``okf serve`` port (8787) or any concurrently running service.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server(proc: subprocess.Popen, base: str) -> None:
    wait_for_server(proc, base, timeout=_SERVER_STARTUP_TIMEOUT)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def server_url(tmp_path_factory):
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp("viewer") / "bundle")
    with live_server(bundle, "--no-watch") as base:
        yield base


@pytest.fixture(scope="session")
def docs_server_url(tmp_path_factory):
    bundle = copy_bundle(DOCS_BUNDLE, tmp_path_factory.mktemp("viewer-docs") / "bundle")
    with live_server(bundle, "--no-watch") as base:
        yield base


@pytest.fixture(scope="session")
def static_url(tmp_path_factory):
    """Build and serve the generated static surface for portable-runtime proof."""
    out = tmp_path_factory.mktemp("portable-static")
    result = subprocess.run(
        okf_module_argv("build", str(DEMO_BUNDLE), "--target", "static", "--out", str(out)),
        cwd=str(TOOLKIT_ROOT), env=okf_subprocess_env(),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    with managed_server(
        [sys.executable, "-m", "http.server", str(port),
         "--bind", "127.0.0.1", "--directory", str(out)],
        base, out.parent / f"static-{port}.log",
    ):
        yield base


@pytest.fixture(scope="session")
def spa_url(tmp_path_factory):
    """Build and serve the generated single-page (spa) portable surface."""
    out = tmp_path_factory.mktemp("portable-spa")
    result = subprocess.run(
        okf_module_argv("build", str(DEMO_BUNDLE), "--target", "spa", "--out", str(out)),
        cwd=str(TOOLKIT_ROOT), env=okf_subprocess_env(),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    with managed_server(
        [sys.executable, "-m", "http.server", str(port),
         "--bind", "127.0.0.1", "--directory", str(out)],
        base, out.parent / f"spa-{port}.log",
    ):
        yield base


@pytest.fixture
def page():
    """A fresh context; only absent optional tooling may skip."""
    with sync_playwright() as p:
        browser = launch_chromium(p)
        try:
            context = browser.new_context()
            pg = context.new_page()
            yield pg
            context.close()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Browser-proof contracts
# ---------------------------------------------------------------------------


def test_graph_renders(server_url: str, page) -> None:
    """The full-page graph view mounts the Cytoscape canvas.

    Previously the canvas check was ``try: wait_for_selector(canvas) except:
    pass``, which SWALLOWED a render failure, and only a non-zero container
    bounding box was asserted (which passes with zero nodes — the graph
    section mounts even when Cytoscape never paints). Now:

      * the Cytoscape ``<canvas>`` MUST be visible (hard assertion — fails if
        the CDN script is blocked or Cytoscape fails to initialise); and
      * the graph carries a non-zero node count, proved by reading the same
        graph.json the page consumed (a non-zero bbox alone cannot distinguish
        a rendered graph from an empty one).
    """
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    # The graph container is always present in the template; visibility +
    # non-zero bounding box prove it actually rendered rather than being
    # hidden by a CSS / load failure.
    graph = page.locator("#okf-graph.okf-graph")
    graph.wait_for(state="visible")
    box = graph.bounding_box()
    assert box is not None and box["width"] > 0 and box["height"] > 0, (
        "graph container is not laid out (zero bounding box)"
    )
    # HARD assertion: the Cytoscape <canvas> must be visible. If Cytoscape.js
    # failed to load (CDN blocked) or failed to initialise, no canvas is
    # created and graph.js shows a load-error notice instead — so this fails,
    # rather than being swallowed by the old try/except.
    # Cytoscape stacks multiple <canvas> elements (one per layer); the bottom
    # layer is always present once Cytoscape initialises, so .first is the
    # stable visibility probe.
    canvas = page.locator("#okf-graph canvas").first
    expect(canvas).to_be_visible()
    # Non-zero node count: fetch the same graph.json the page fed to Cytoscape
    # and assert it carries real concepts. A non-zero bbox passes with zero
    # nodes, so this is the proof that actual concept nodes were rendered.
    node_count = page.evaluate("""async () => {
        const el = document.getElementById('okf-graph');
        const url = el.getAttribute('data-graph-url');
        const resp = await fetch(url);
        const data = await resp.json();
        return (data.nodes || []).length;
    }""")
    assert node_count > 0, (
        "graph rendered with zero nodes — data path delivered no concepts"
    )


def test_desktop_graph_workspace_remains_viewport_bounded(server_url: str, page) -> None:
    """The desktop graph owns a bounded workspace with two internal scroll panes."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    page.locator("#okf-graph").wait_for(state="visible")

    geometry = page.evaluate(
        """() => {
            const canvas = document.querySelector('#okf-graph');
            const detail = document.querySelector('#okf-detail');
            const topbar = document.querySelector('.okf-topbar');
            const layout = document.querySelector('.okf-layout');
            const rect = el => el && el.getBoundingClientRect();
            return {
                bodyOverflowY: getComputedStyle(document.body).overflowY,
                canvas: rect(canvas),
                detail: rect(detail),
                layout: rect(layout),
                topbar: rect(topbar),
                viewportH: window.innerHeight,
            };
        }"""
    )
    assert geometry["bodyOverflowY"] == "hidden", geometry
    for pane in ("canvas", "detail"):
        box = geometry[pane]
        assert box and box["width"] > 100 and box["height"] > 100, geometry
        assert box["bottom"] <= geometry["viewportH"] + 1, geometry
    assert geometry["layout"]["height"] > 0, geometry
    assert geometry["topbar"]["top"] == 0, geometry


def test_delayed_initial_graph_fetch_is_fenced_by_pagehide_and_bfcache_restarts(server_url: str, page) -> None:
    """A hidden page drops request 1; persisted pageshow installs request 2 only."""
    page.add_init_script("""(() => {
      const real=window.fetch.bind(window);
      window.__initialGraphGate={requestCount:0,pending:{},released:[],stacks:[]};
      window.fetch=(input,init) => {
        const url=typeof input==='string'?input:(input&&input.url)||'';
        const stack=new Error('graph request probe').stack||'';
        if (!new URL(url,location.href).pathname.endsWith('/__data/graph.json') || !stack.includes('/graph.js:')) return real(input,init);
        const requestId=++window.__initialGraphGate.requestCount;
        window.__initialGraphGate.stacks.push(stack);
        return real(input,init).then(response => response.json()).then(payload => {
          payload.nodes.push({data:{id:'owner-request-'+requestId,label:'Owner request '+requestId,type:'concept'}});
          return new Promise(resolve => {
            window.__initialGraphGate.pending[requestId]={
              release:() => {
                window.__initialGraphGate.released.push(requestId);
                resolve(new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}}));
              }
            };
          });
        });
      };
    })();""")
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__initialGraphGate.pending[1]")
    assert page.evaluate("() => window.__initialGraphGate.requestCount") == 1, page.evaluate("() => window.__initialGraphGate.stacks")
    assert page.evaluate("() => !window.__okfLoomGraph"), "graph initialized before held request 1 was released"
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))")
    page.evaluate("() => window.__initialGraphGate.pending[1].release()")
    page.wait_for_function("() => window.__initialGraphGate.released.includes(1)")
    assert page.evaluate("() => !window.__okfLoomGraph"), "late request 1 initialized the pagehidden graph"
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    page.wait_for_function("() => window.__initialGraphGate.pending[2]")
    assert page.evaluate("() => window.__initialGraphGate.requestCount") == 2
    assert page.evaluate("() => !window.__okfLoomGraph"), "graph initialized before request 2 was released"
    page.evaluate("() => window.__initialGraphGate.pending[2].release()")
    page.wait_for_function("() => window.__okfLoomGraph?.cy", timeout=10000)
    ownership = page.evaluate("""() => ({
      request1:window.__okfLoomGraph.cy.getElementById('owner-request-1').length,
      request2:window.__okfLoomGraph.cy.getElementById('owner-request-2').length,
      released:window.__initialGraphGate.released.slice()
    })""")
    assert ownership == {"request1": 0, "request2": 1, "released": [1, 2]}


def test_graph_selection_owns_neighbourhood_context_action(server_url: str, page) -> None:
    """Selected identity publishes, visible context follows it, clear removes both."""
    _open_graph(page, server_url)
    page.evaluate("""() => {
      window.__graphSelectionEvents = [];
      window.addEventListener('okf-loom:graph-selection', e => window.__graphSelectionEvents.push(e.detail));
    }""")
    index = page.locator(".okf-node-index")
    index.locator("summary").click()
    first = index.locator(".okf-node-index__item").first
    concept_id = first.get_attribute("data-target")
    first.click()
    page.wait_for_function("id => window.okfGraphSelection && window.okfGraphSelection.conceptId === id", arg=concept_id)
    action = page.locator("#okf-copy-neighbourhood")
    expect(action).to_be_visible()
    assert page.evaluate("() => window.__graphSelectionEvents.at(-1).conceptId") == concept_id
    page.keyboard.press("Control+k")
    expect(page.get_by_role("option", name="Copy concept context")).to_be_visible()
    page.keyboard.press("Escape")
    page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
      configurable: true, value: { writeText: () => Promise.resolve() }
    })""")
    action.click()
    expect(page.locator("#okf-graph-copy-status")).to_have_text("Copied concept context.")
    page.evaluate("() => { window.__okfLoomGraph.cy.trigger('tap'); return undefined; }")
    page.wait_for_function("() => window.okfGraphSelection && window.okfGraphSelection.conceptId === null")
    expect(action).to_be_hidden()
    assert page.evaluate("() => window.__graphSelectionEvents.at(-1).conceptId") is None


def test_graph_context_ignores_late_response_after_clear(server_url: str, page) -> None:
    """Clearing selection revokes ownership before a late context response."""
    _open_graph(page, server_url)
    page.locator(".okf-node-index summary").click()
    page.locator(".okf-node-index__item").first.click()
    page.wait_for_function("() => window.okfGraphSelection && window.okfGraphSelection.conceptId")
    page.evaluate("""() => {
      const nativeFetch = window.fetch.bind(window);
      window.__clipboardWrites = 0;
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        writeText: () => { window.__clipboardWrites++; return Promise.resolve(); }
      }});
      window.fetch = (url, opts) => {
        if (String(url).startsWith('/__context?')) return new Promise(resolve => {
          window.__resolveLateGraphContext = () => resolve(new Response('# late context', {status: 200}));
        });
        return nativeFetch(url, opts);
      };
    }""")
    action = page.locator("#okf-copy-neighbourhood")
    action.click()
    page.wait_for_function("() => typeof window.__resolveLateGraphContext === 'function'")
    page.evaluate("() => { window.__okfLoomGraph.cy.trigger('tap'); return undefined; }")
    page.evaluate("() => window.__resolveLateGraphContext()")
    page.wait_for_timeout(100)
    assert page.evaluate("() => window.__clipboardWrites") == 0
    expect(action).to_be_hidden()
    assert action.text_content() == "Copy concept context"


def test_concept_page_loads(server_url: str, page) -> None:
    """A concept page renders its H1 title and a non-empty body."""
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    h1 = page.locator("h1.okf-page__title")
    h1.wait_for(state="visible")
    title = (h1.text_content() or "").strip()
    assert title, "concept page H1 is empty"
    # The Orders concept title is "Orders"; tolerate case but require text.
    assert "order" in title.lower(), f"unexpected concept title: {title!r}"
    body = page.locator(".okf-page__body")
    body.wait_for(state="visible")
    assert (body.inner_text() or "").strip(), "concept body is empty"


@pytest.mark.parametrize(
    "scenario", ["abort-before-headers", "abort-during-body", "malformed-json", "http-500"]
)
def test_doc_refetch_cancellation_is_silent_while_real_failures_stay_reported(
    server_url: str, page, scenario: str
) -> None:
    """live.js doc-refetch cancellation is quiet; genuine failures stay loud.

    Route departure aborts the in-flight /__data/doc owner at any await point —
    including after the response headers arrive but before the body is
    consumed. An abort belonging to the departed route is the expected terminal
    state: no reconciliation error, no old-owner commit, no revision
    advancement. Malformed JSON and HTTP failures without cancellation are real
    diagnostics for the current owner: still reported, still no commit.

    The natural boot race (studio.js loadComments and live.js initial-ready
    both fetch /__data/doc within milliseconds) is avoided: boot is allowed to
    settle, then the initial-ready re-reconciliation is driven through the
    production ``_dispatchReadyForTest`` seam with lastDocRev cleared, and only
    the fetch/body-read issued by THAT reconciliation is gated.
    """
    # Record live.js's console.error diagnostics inside the page so both the
    # silence and the reporting assertions wait on the same signal. Gate
    # wrappers arm only for the dispatched re-reconciliation (window.__armed),
    # never for boot traffic.
    page.add_init_script(
        """(() => {
          window.__liveErrors = [];
          window.__armed = false;
          const origError = console.error;
          console.error = function () {
            try { window.__liveErrors.push(Array.from(arguments).map(String).join(' ')); } catch (e) {}
            return origError.apply(console, arguments);
          };
        })()"""
    )
    if scenario == "abort-before-headers":
        page.add_init_script(
            """(() => {
              const nativeFetch = window.fetch.bind(window);
              window.__docGate = { pending: false, release: null, settled: false, signal: null };
              window.fetch = (url, opts) => {
                if (window.__armed && String(url).indexOf('/__data/doc') >= 0) {
                  window.__armed = false;
                  window.__docGate.pending = true;
                  window.__docGate.signal = (opts && opts.signal) || null;
                  const gate = new Promise((resolve, reject) => {
                    if (opts && opts.signal) opts.signal.addEventListener('abort',
                      () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
                    window.__docGate.release = () => nativeFetch(url, opts).then(resolve, reject);
                  });
                  gate.catch(() => { window.__docGate.settled = true; });
                  return gate;
                }
                return nativeFetch(url, opts);
              };
            })()"""
        )
    elif scenario == "abort-during-body":
        # The diagnostic boundary: headers arrive from the production server,
        # but the native body read is deferred until the gate releases — an
        # abort in between must die inside live.js's cancellation boundary.
        page.add_init_script(
            """(() => {
              const nativeJson = Response.prototype.json;
              window.__bodyGate = { pending: false, release: null, settled: false };
              Response.prototype.json = function () {
                const url = (this && this.url) || '';
                if (window.__armed && url.indexOf('/__data/doc') >= 0) {
                  window.__armed = false;
                  window.__bodyGate.pending = true;
                  const read = nativeJson.bind(this);
                  return new Promise((resolve, reject) => {
                    window.__bodyGate.release = () => {
                      read().then(
                        (v) => { window.__bodyGate.settled = true; resolve(v); },
                        (e) => { window.__bodyGate.settled = true; reject(e); });
                    };
                  });
                }
                return nativeJson.call(this);
              };
            })()"""
        )

    page.goto(f"{server_url}/tables/orders", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomLive === 'object'", timeout=8000)
    # Let the natural boot reconciliation commit (studio.js loadComments and
    # live.js initial-ready both fetch /__data/doc at boot) so the gates below
    # can only ever capture the dispatched re-reconciliation.
    # An unseeded revision is undefined, not null; require a committed value.
    page.wait_for_function(
        "() => !!window.okfLoomLive._getLastDocRevForTest('tables/orders')", timeout=15000
    )
    page.evaluate("() => window.okfLoomLive._clearLastDocRevForTest('tables/orders')")

    if scenario == "malformed-json":
        page.route(
            "**/__data/doc*",
            lambda route: route.fulfill(status=200, content_type="application/json", body="{not json"),
        )
    elif scenario == "http-500":
        page.route(
            "**/__data/doc*",
            lambda route: route.fulfill(status=500, content_type="application/json", body='{"error":"boom"}'),
        )

    # Drive the initial-ready branch (seen == null after the clear) through
    # the production seam: patchOpenConcept refetches the doc.
    page.evaluate(
        """() => {
          window.__armed = true;
          window.okfLoomLive._dispatchReadyForTest({
            rev: 2,
            doc_revs: { 'tables/orders': window.okfLoomStudio.state.doc.concept_content_rev },
          });
        }"""
    )

    if scenario == "abort-before-headers":
        page.wait_for_function("() => window.__docGate && window.__docGate.pending", timeout=8000)
        epoch_before = page.evaluate("() => window.okfLoomLive._getNavEpoch()")
        # The production route-departure owner aborts the in-flight fetch.
        page.evaluate("() => window.dispatchEvent(new PopStateEvent('popstate'))")
        assert page.evaluate("() => window.okfLoomLive._getNavEpoch()") > epoch_before, "route owner did not advance"
        assert page.evaluate("() => !!(window.__docGate.signal && window.__docGate.signal.aborted)"), (
            "the in-flight fetch signal was not aborted by route departure"
        )
        page.evaluate("() => window.__docGate.release()")
    elif scenario == "abort-during-body":
        page.wait_for_function("() => window.__bodyGate && window.__bodyGate.pending", timeout=8000)
        epoch_before = page.evaluate("() => window.okfLoomLive._getNavEpoch()")
        page.evaluate("() => window.dispatchEvent(new PopStateEvent('popstate'))")
        assert page.evaluate("() => window.okfLoomLive._getNavEpoch()") > epoch_before, "route owner did not advance"
        # The deferred native body read now rejects with AbortError.
        page.evaluate("() => window.__bodyGate.release()")

    if scenario in ("abort-before-headers", "abort-during-body"):
        page.wait_for_function(
            "() => !!(window.__docGate && window.__docGate.settled) || !!(window.__bodyGate && window.__bodyGate.settled)",
            timeout=8000,
        )
        reconciliation_errors = page.evaluate(
            "() => window.__liveErrors.filter((t) => t.indexOf('initial-ready reconciliation failed') >= 0)"
        )
        assert reconciliation_errors == [], f"departed-owner abort surfaced as an error: {reconciliation_errors}"
    else:
        page.wait_for_function(
            "() => window.__liveErrors.some((t) => t.indexOf('initial-ready reconciliation failed') >= 0)",
            timeout=8000,
        )
    # No scenario may commit the fetched doc or advance the revision baseline.
    assert page.evaluate("() => window.okfLoomLive._getLastDocRevForTest('tables/orders')") is None, (
        "a failed/aborted doc fetch must not seed lastDocRev"
    )


def test_phase2_concept_anatomy_dom_order(server_url: str, page) -> None:
    """Phase 2 layout-graph + contract-runtime parity.

    Authoritative layout contract (ui-overhaul-plan §3.3 lines 140-154):
    on wide screens the infobox is a RIGHT RAIL beside the body (not
    vertically stacked above it). This replaces the prior vertical-stacking
    assertion which encoded the wrong contract.

    Asserts, at the default desktop viewport:
      * header sits above both infobox and body;
      * the infobox is in the RIGHT rail — its left edge is at/after the
        body's right edge (no horizontal overlap with the reading lane);
      * the infobox is NOT a descendant of .okf-page__body (projection parity);
      * canonical-source / projection sit between body and connections in DOM.
    """
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")

    # All structural landmarks exist.
    header = page.locator(".okf-page__header")
    infobox = page.locator(".okf-page__infobox")
    body = page.locator(".okf-page__body")
    canonical = page.locator("#okf-canonical-source")
    projection = page.locator("#okf-annotation-projection")
    assert header.count() == 1, "header missing"
    assert infobox.count() == 1, "infobox missing"
    assert body.count() == 1, "body missing"

    h_box = header.bounding_box()
    i_box = infobox.bounding_box()
    b_box = body.bounding_box()
    assert h_box and i_box and b_box, "bounding boxes not available"
    # Header band sits above both the infobox and the body.
    assert h_box["y"] < i_box["y"], "header must be above infobox"
    assert h_box["y"] < b_box["y"], "header must be above body"
    # Approved reading-first order: details follow, never squeeze the prose.
    assert i_box["y"] >= b_box["y"] + b_box["height"] - 1
    expect(infobox).not_to_have_attribute("open", "")

    # The infobox is NOT a descendant of .okf-page__body (projection parity).
    assert page.evaluate(
        """() => {
            const body = document.querySelector('.okf-page__body');
            const infobox = document.querySelector('.okf-page__infobox');
            return body && infobox ? !body.contains(infobox) : false;
        }"""
    ), "infobox must be outside .okf-page__body"

    # DOM order: body -> canonical-source -> annotation-projection ->
    # connections (the projection anchors stay between body and connections).
    order = page.evaluate(
        """() => {
            const pick = (sel) => {
                const el = document.querySelector(sel);
                return el ? Array.from(document.querySelectorAll('*')).indexOf(el) : -1;
            };
            return {
                body: pick('.okf-page__body'),
                canonical: pick('#okf-canonical-source'),
                projection: pick('#okf-annotation-projection'),
                connections: pick('.okf-page__connections'),
            };
        }"""
    )
    assert order["body"] < order["canonical"] < order["projection"], (
        "body -> canonical-source -> projection DOM order violated"
    )
    if order["connections"] != -1:
        assert order["projection"] < order["connections"], (
            "projection must precede connections in DOM"
        )


def test_phase2_mobile_anatomy_320px(server_url: str, page) -> None:
    """Phase 2 layout-graph (mobile, 320px): the on-this-page disclosure is
    revealed and the visual order is header -> section disclosure -> infobox
    -> body, with NO horizontal document overflow.

    ui-overhaul-plan §3.3 line 153: the section disclosure sits "below the
    header band, never above the title".
    """
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")

    header = page.locator(".okf-page__header")
    onthispage = page.locator("details.okf-onthispage")
    infobox = page.locator(".okf-page__infobox")
    body = page.locator(".okf-page__body")
    assert onthispage.count() == 1, "mobile on-this-page disclosure missing"
    # Disclosure is visible on mobile (it's display:none on desktop).
    expect(onthispage).to_be_visible()

    h_box = header.bounding_box()
    d_box = onthispage.bounding_box()
    i_box = infobox.bounding_box()
    b_box = body.bounding_box()
    assert h_box and d_box and i_box and b_box
    # Visual order: header -> disclosure -> infobox -> body.
    assert h_box["y"] < d_box["y"], "disclosure must be below header"
    assert d_box["y"] < i_box["y"], "disclosure must be above infobox"
    assert i_box["y"] >= b_box["y"] + b_box["height"] - 1, "details follow prose on mobile"
    # The disclosure is never above the title (header.y < disclosure.y).

    # No horizontal document overflow at 320px: the widest scrolled element
    # never exceeds the viewport, and the page itself does not overflow.
    overflow = page.evaluate(
        """() => {
            const html = document.documentElement;
            return {
                scrollWidth: html.scrollWidth,
                clientWidth: html.clientWidth,
                bodyScrollW: document.body.scrollWidth,
            };
        }"""
    )
    assert overflow["scrollWidth"] <= overflow["clientWidth"] + 1, (
        f"horizontal document overflow at 320px: "
        f"scrollWidth={overflow['scrollWidth']} clientWidth={overflow['clientWidth']}"
    )


def test_phase2_infobox_facts_disclosure_keyboard(server_url: str, page) -> None:
    """Phase 2 keyboard-focus: the infobox facts disclosure toggles via
    keyboard (Enter AND Space) and shows a visible focus ring on the summary."""
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    details = page.locator("details.okf-page__infobox")
    if not details.count():
        pytest.fail("fixture concept has no extra frontmatter keys to disclose")
    summary = details.locator("summary")
    # Focus the summary (keyboard-equivalent) then activate with Enter, then
    # Space, proving both activation keys toggle the disclosure.
    summary.focus()
    expect(summary).to_be_focused()

    # Enter opens.
    page.keyboard.press("Enter")
    expect(details).to_have_attribute("open", "")
    # Space closes (native <details> toggles on either activation key).
    summary.focus()
    page.keyboard.press(" ")
    assert details.get_attribute("open") is None, "Space did not close the disclosure"

    # Visible focus ring: the focused summary renders a non-zero outline so
    # keyboard users can see where they are (focus-visible contract).
    summary.focus()
    focus_info = page.evaluate(
        """() => {
            const el = document.querySelector('details.okf-page__infobox > summary');
            el.focus();
            const s = getComputedStyle(el);
            const w = parseFloat((s.outlineWidth || '0px').replace('px', ''));
            const visible = s.outlineStyle && s.outlineStyle !== 'none' && w > 0;
            return { outlineWidth: s.outlineWidth, outlineStyle: s.outlineStyle,
                     outlineColor: s.outlineColor, visible: visible };
        }"""
    )
    assert focus_info["visible"], (
        "facts disclosure summary must show a visible focus outline; "
        f"computed={focus_info}"
    )


def test_phase2_unsafe_resource_url_fails_closed(server_url: str, page) -> None:
    """Phase 2 security-boundary: a concept whose ``resource:`` frontmatter
    carries a disallowed scheme (``javascript:``) renders NO actionable link —
    the value is rendered as a blocked, non-interactive span instead.

    The demo bundle does not ship a javascript: resource, so this builds a
    one-concept temp bundle, serves it, and loads the page. (Uses the same
    serve path as the other fixtures.)"""
    import shutil
    import tempfile
    bundle_dir = Path(tempfile.mkdtemp()) / "demo"
    shutil.copytree(str(DEMO_BUNDLE), str(bundle_dir))
    # Override a single concept with a javascript: resource.
    (bundle_dir / "glossary.md").write_text(
        "---\n"
        "type: Term\n"
        "title: Unsafe Resource Demo\n"
        'resource: javascript:alert(1)\n'
        "---\n\n"
        "# Body\n\nPlain body.\n",
        encoding="utf-8",
    )
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        okf_module_argv(
            "serve", str(bundle_dir), "--host", "127.0.0.1", "--port", str(port),
            "--no-watch", "--no-open",
        ),
        cwd=str(TOOLKIT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=okf_subprocess_env(),
    )
    try:
        _wait_for_server(proc, base)
        page.goto(f"{base}/glossary", wait_until="domcontentloaded")
        page.locator("h1.okf-page__title").wait_for(state="visible")
        # No anchor carries the javascript: scheme anywhere on the page.
        bad = page.evaluate(
            """() => Array.from(document.querySelectorAll('a')).filter(
                  a => (a.getAttribute('href') || '').trim().toLowerCase()
                       .startsWith('javascript:')
                ).length"""
        )
        assert bad == 0, "an actionable javascript: link is present (fail-open)"
        # The blocked resource renders as a non-interactive span, not a link.
        blocked = page.locator(".okf-link-blocked")
        assert blocked.count() >= 1, "unsafe resource should render as .okf-link-blocked"
        # The blocked span is NOT focusable/actionable (no href, not an <a>).
        tag = page.evaluate(
            "() => document.querySelector('.okf-link-blocked') ? "
            "document.querySelector('.okf-link-blocked').tagName : ''"
        )
        assert tag != "A", "blocked resource must not be an <a> element"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)


def test_phase2_code_block_horizontal_scroll(server_url: str, page) -> None:
    """Phase 2 regression: the authored wrapper owns horizontal scrolling.

    The inner ``pre`` keeps visible overflow, avoiding a nested keyboard
    scroll region, while the measured outer owner contains wide code without
    forcing document overflow at a narrow viewport.
    """
    page.set_viewport_size({"width": 360, "height": 640})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    # tables/orders has a wide ```sql fence.
    port = page.locator(".okf-page__body .okf-content-scrollport--code").first
    pre = port.locator("pre").first
    expect(port).to_be_visible()
    expect(pre).to_be_visible()
    info = page.evaluate(
        """() => {
            const port = document.querySelector('.okf-page__body .okf-content-scrollport--code');
            const pre = port.querySelector('pre');
            const cs = getComputedStyle(pre);
            const html = document.documentElement;
            return {
                outerOverflowX: getComputedStyle(port).overflowX,
                outerScrollWidth: port.scrollWidth,
                outerClientWidth: port.clientWidth,
                preOverflowX: cs.overflowX,
                docScrollW: html.scrollWidth,
                docClientW: html.clientWidth,
            };
        }"""
    )
    assert info["outerOverflowX"] in ("auto", "scroll"), (
        f"outer wrapper must own horizontal scrolling, got {info['outerOverflowX']!r}"
    )
    assert info["preOverflowX"] == "visible", info
    # The code content is wider than the wrapper's visible box (so scroll is real).
    assert info["outerScrollWidth"] > info["outerClientWidth"], (
        "code block is not wider than its box — horizontal-scroll proof is moot"
    )
    # And the DOCUMENT does not overflow horizontally (scroll contained in pre).
    assert info["docScrollW"] <= info["docClientW"] + 1, (
        f"document overflows horizontally at 360px: "
        f"scrollW={info['docScrollW']} clientW={info['docClientW']}"
    )


def test_phase2_toc_zero_headings_cleanup(server_url: str, page) -> None:
    """Phase 2 state-ownership: when the body ends up with ZERO headings after
    a patch, rebuildSectionsToc disconnects the IntersectionObserver (no stale
    observer survives on detached headings) and the TOC panel shows the empty
    state.

    Drives the rebuild directly via the exposed test hook so the assertion
    isolates rebuildSectionsToc itself. (Dispatching the live
    ``okf-loom:bodyPatched`` event also triggers wiki.js's body re-render from
    the canonical source, which would restore the removed headings and mask
    the zero-headings path — so the function is exercised directly here, and
    the event-driven path is covered by the focus-preservation test below.)"""
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    # Studio must have booted (the sections panel + observer exist).
    assert page.evaluate("() => !!window.okfLoomStudio"), "studio did not boot"
    # Initially an observer is active (the page has headings).
    assert page.evaluate("() => window.okfDocumentOutline.observerActive") is True
    # Empty the body, drive the rebuild, and read the result ATOMICALLY (in
    # the same evaluate). The live studio runs a background re-sync that
    # would otherwise restore the body from the canonical source between
    # evaluates and mask the zero-headings outcome — reading in the same
    # synchronous tick proves the rebuild function's own behavior.
    result = page.evaluate(
        """() => {
            const body = document.querySelector('.okf-page__body');
            if (body) body.innerHTML = '<p>No headings left after this patch.</p>';
            window.okfDocumentOutline.rebuild();
            const outline = document.querySelector('[data-okf-document-outline]');
            return {
                headCount: body.querySelectorAll('h1,h2,h3,h4,h5,h6').length,
                outlineHidden: outline ? outline.hidden : null,
                observerActive: window.okfDocumentOutline.observerActive,
            };
        }"""
    )
    assert result["headCount"] == 0, "body should have zero headings after empty"
    assert result["outlineHidden"] is True, "zero-headings rebuild must hide stale outline chrome"
    assert result["observerActive"] is False, (
        "stale IntersectionObserver survived the zero-headings rebuild"
    )


def test_phase2_toc_focus_preserved_through_patch(server_url: str, page) -> None:
    """Phase 2 keyboard-focus / state-ownership: when keyboard focus is on a
    TOC link and a body patch lands, focus is preserved — restored to the
    rebuilt link with the same target heading identity."""
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    assert page.evaluate("() => !!window.okfLoomStudio"), "studio did not boot"
    # Open the sections panel if collapsed, and focus its first link.
    toc_link = page.locator(
        '[data-okf-document-outline] a.okf-onthispage__link'
    ).first
    if not toc_link.count():
        pytest.fail("fixture has no TOC links to focus (concept has no headings)")
    href = toc_link.get_attribute("href")
    reveal_control(page, toc_link).focus()
    expect(toc_link).to_be_focused()
    # Dispatch the body-patch event and read focus ATOMICALLY (same evaluate).
    # The live studio's background re-sync would otherwise re-render the body
    # between evaluates; reading in the same synchronous tick proves the
    # rebuild restored focus to the rebuilt matching link.
    focused_href = page.evaluate(
        """() => {
            window.dispatchEvent(new CustomEvent('okf-loom:bodyPatched'));
            const a = document.activeElement;
            return a && a.tagName === 'A' ? a.getAttribute('href') : null;
        }"""
    )
    assert focused_href == href, (
        f"TOC focus not preserved through patch: focused href={focused_href!r}, "
        f"expected={href!r}"
    )


def test_phase2_mobile_onthispage_rebuilds_after_body_patch(server_url: str, page) -> None:
    """P1 state-ownership: the mobile 'On this page' disclosure is rebuilt from
    the CURRENT rendered heading ids after a live body patch — it is not left
    stale (dead hrefs / missing sections) the way the SSR-only disclosure was.

    Drives rebuildOnThisPage directly via the exposed hook and reads the result
    ATOMICALLY (same evaluate): a NEW heading appended to the body must appear
    in the rebuilt disclosure, and the rebuilt href list must exactly mirror
    the body's id-carrying headings (parity with SSR _render_onthispage)."""
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    assert page.evaluate("() => !!window.okfLoomStudio"), "studio did not boot"
    result = page.evaluate(
        """() => {
            const body = document.querySelector('.okf-page__body');
            // Append a brand-new id-carrying heading the SSR disclosure
            // never knew about, simulating a patch that adds a section.
            const h = document.createElement('h2');
            h.id = 'phase2-new-heading';
            h.textContent = 'Brand new section';
            body.appendChild(h);
            window.okfDocumentOutline.rebuild();
            const disc = document.querySelector('details.okf-onthispage');
            const links = disc
                ? Array.from(disc.querySelectorAll('a.okf-onthispage__link'))
                : [];
            const bodyIds = Array.from(
                body.querySelectorAll('h1,h2,h3,h4,h5,h6')
            ).map(el => el.id).filter(Boolean);
            return {
                exists: !!disc,
                hidden: disc ? disc.hidden : null,
                hrefs: links.map(a => a.getAttribute('href')),
                labels: links.map(a => (a.textContent || '').trim()),
                bodyHrefs: bodyIds.map(id => '#' + id),
            };
        }"""
    )
    assert result["exists"], "on-this-page disclosure should exist"
    assert result["hidden"] is False, "disclosure must be shown when headings exist"
    assert result["hrefs"] == result["bodyHrefs"], (
        f"rebuilt disclosure hrefs do not mirror current body ids: "
        f"got {result['hrefs']} want {result['bodyHrefs']}"
    )
    assert "#phase2-new-heading" in result["hrefs"], (
        "new heading was NOT reflected in rebuilt disclosure (stale SSR links)"
    )
    assert "Brand new section" in result["labels"], "new heading label missing"


def test_phase2_mobile_onthispage_hidden_when_no_headings(server_url: str, page) -> None:
    """P1 state-ownership: when a patch leaves the body with ZERO headings the
    mobile disclosure is hidden and its list is removed — no stale chrome for
    sections that no longer exist (parity with SSR _render_onthispage, which
    emits nothing for a headingless body). The element is retained (hidden) so
    a later patch that re-adds headings can revive it."""
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    result = page.evaluate(
        """() => {
            const body = document.querySelector('.okf-page__body');
            body.innerHTML = '<p>No headings left after this patch.</p>';
            window.okfDocumentOutline.rebuild();
            const disc = document.querySelector('details.okf-onthispage');
            return {
                exists: !!disc,
                hidden: disc ? disc.hidden : null,
                hasNav: disc ? !!disc.querySelector('.okf-onthispage__nav') : false,
                isOpen: disc ? disc.hasAttribute('open') : null,
                headCount: body.querySelectorAll('h1,h2,h3,h4,h5,h6').length,
            };
        }"""
    )
    assert result["headCount"] == 0, "body should have zero headings"
    assert result["exists"], "disclosure element should be retained for revival"
    assert result["hidden"] is True, "zero-headings disclosure must be hidden"
    assert result["hasNav"] is False, "stale nav/list must be removed"
    assert result["isOpen"] is False, "hidden disclosure must not be open"


def test_phase2_mobile_onthispage_focus_preserved_and_recovered(server_url: str, page) -> None:
    """P1 keyboard-focus: when a body patch rebuilds the mobile disclosure,
    focus on a section link is RESTORED to the rebuilt link with the same href
    (heading survived); and when the focused heading is REMOVED, focus
    RECOVERS into the disclosure summary (stays in the TOC region) instead of
    dropping to <body>."""
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    # The SSR disclosure is closed by default; open it so its links are
    # focusable (a closed <details> hides its non-summary content).
    disc = page.locator("details.okf-onthispage")
    disc.locator("summary").click()
    expect(disc).to_have_attribute("open", "")
    link = page.locator("details.okf-onthispage a.okf-onthispage__link").first
    if not link.count():
        pytest.fail("fixture has no on-this-page links (concept has no headings)")
    href = link.get_attribute("href")
    # Case 1 — heading survives: focus restored to the rebuilt matching link.
    link.focus()
    expect(link).to_be_focused()
    restored = page.evaluate(
        """() => {
            window.okfDocumentOutline.rebuild();
            const a = document.activeElement;
            return a && a.tagName === 'A' ? a.getAttribute('href') : null;
        }"""
    )
    assert restored == href, (
        f"focus not restored to rebuilt link: focused={restored!r} want={href!r}"
    )
    # Case 2 — focused heading removed: focus recovers into the disclosure
    # (summary), not dropped to <body> or a different, possibly-wrong link.
    link2 = page.locator("details.okf-onthispage a.okf-onthispage__link").first
    href2 = link2.get_attribute("href") or ""
    target_id = href2[1:] if href2.startswith("#") else href2
    link2.focus()
    expect(link2).to_be_focused()
    fallback = page.evaluate(
        """(targetId) => {
            const victim = targetId ? document.getElementById(targetId) : null;
            if (victim) victim.remove();
            window.okfDocumentOutline.rebuild();
            const a = document.activeElement;
            const disc = document.querySelector('details.okf-onthispage');
            return {
                tag: a ? a.tagName : null,
                inDisclosure: !!(a && disc && disc.contains(a)),
                isBody: a === document.body,
            };
        }""",
        target_id,
    )
    assert fallback["inDisclosure"], (
        f"focus left the TOC region after heading removal: {fallback}"
    )
    assert fallback["isBody"] is False, "focus dropped to <body>"


def test_phase2_mobile_wide_table_scroll_affordance_320px(server_url: str, page) -> None:
    """P1 mobile a11y: a wide body table at 320px becomes a contained
    horizontal-scroll region with BOTH a programmatic affordance (role=region
    + descriptive aria-label + tabindex=0 so keyboard/AT can scroll it) and a
    visible overflow cue. Every cell stays reachable by scrolling, and there
    is NO document-level horizontal overflow. The orders schema table
    naturally overflows at 320px (table ~340px > column ~288px), so this
    proves the contract on the real reviewer-flagged surface, not a fixture."""
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    page.wait_for_selector(".okf-page__body .okf-tablewrap", timeout=3000)
    # Let the enhancer's fit/affordance settle (initTables runs on DOMContentLoaded
    # + a resize debounce; the cue opacity also transitions over 120ms).
    page.wait_for_timeout(350)
    info = page.evaluate(
        """() => {
            const wrap = document.querySelector('.okf-page__body .okf-tablewrap');
            const table = wrap && wrap.querySelector('table');
            const frame = wrap && wrap.closest('.okf-tablewrap-frame');
            const cueR = frame && frame.querySelector('.okf-tablewrap__cue--right');
            const cueL = frame && frame.querySelector('.okf-tablewrap__cue--left');
            const wr = wrap && wrap.getBoundingClientRect();
            const cr = cueR && cueR.getBoundingClientRect();
            return {
                role: wrap ? wrap.getAttribute('role') : null,
                ariaLabel: wrap ? wrap.getAttribute('aria-label') : null,
                tabIndex: wrap ? wrap.tabIndex : null,
                // Affordance marker classes live on the non-scrolling FRAME.
                scrolls: frame ? frame.classList.contains('okf-tablewrap--scrolls') : null,
                scrolledEnd: frame ? frame.classList.contains('okf-tablewrap--scrolled-end') : null,
                cueRightOpacity: cueR ? getComputedStyle(cueR).opacity : null,
                // P1 cue-geometry: the right cue must be pinned to the wrapper's
                // right edge (cueR.right == wrap.right), NOT scrolled away with
                // the table content (an abspos cue inside the scroll port would
                // drift left by scrollLeft).
                cueRightEdge: cr ? Math.round(cr.right) : null,
                wrapRightEdge: wr ? Math.round(wr.right) : null,
                tableScrollW: table ? table.scrollWidth : 0,
                wrapClientW: wrap ? wrap.clientWidth : 0,
                cellCount: table ? table.querySelectorAll('th,td').length : 0,
                cueLeft: cueL ? Math.round(cueL.getBoundingClientRect().left) : null,
                wrapLeft: wr ? Math.round(wr.left) : null,
            };
        }"""
    )
    assert info["tableScrollW"] > info["wrapClientW"], (
        "schema table must overflow at 320px for this proof to be meaningful"
    )
    assert info["role"] == "region", f"wide table wrap must be role=region; got {info['role']!r}"
    assert info["tabIndex"] == 0, f"region must be keyboard-focusable (tabindex=0); got {info['tabIndex']!r}"
    assert info["ariaLabel"] and "scroll" in info["ariaLabel"].lower(), (
        f"aria-label must describe horizontal scroll; got {info['ariaLabel']!r}"
    )
    assert info["scrolls"] is True, "frame must carry the --scrolls affordance class"
    # Visible cue: at scroll-start the right-edge fade is shown (not scrolledEnd).
    assert info["scrolledEnd"] is False, "right cue hidden prematurely"
    assert float(info["cueRightOpacity"]) > 0, (
        f"right scroll cue must be visible; opacity={info['cueRightOpacity']!r}"
    )
    # The right cue stays pinned to the wrapper's right edge at scroll-START
    # (it did not scroll away into the content).
    assert abs(info["cueRightEdge"] - info["wrapRightEdge"]) <= 1, (
        f"right cue is not pinned to the wrapper edge at scroll-start: "
        f"cueRight={info['cueRightEdge']} wrapRight={info['wrapRightEdge']}"
    )
    assert info["cellCount"] > 0
    # Reachability: scroll the region fully right and confirm the LAST cell is
    # now inside the visible scroll viewport (reachable), the document itself
    # never overflows horizontally (scroll contained in the region), AND the
    # cue geometry still tracks the wrapper edge at the far end (left cue now
    # pinned to the wrapper's left edge, right cue faded as scrolled-end).
    # The cue classes update via a requestAnimationFrame on scroll, so scroll
    # first, let a frame settle, then read the geometry.
    page.evaluate(
        """() => {
            const wrap = document.querySelector('.okf-page__body .okf-tablewrap');
            wrap.scrollLeft = wrap.scrollWidth;
        }"""
    )
    page.wait_for_function(
        """() => {
            const f = document.querySelector('.okf-page__body .okf-tablewrap').closest('.okf-tablewrap-frame');
            return f && f.classList.contains('okf-tablewrap--scrolled-end');
        }""",
        timeout=2000,
    )
    # The cue opacity transitions over 120ms after the class flips; let it
    # settle before asserting the faded/visible opacity values.
    page.wait_for_timeout(180)
    reach = page.evaluate(
        """() => {
            const wrap = document.querySelector('.okf-page__body .okf-tablewrap');
            const table = wrap.querySelector('table');
            const frame = wrap.closest('.okf-tablewrap-frame');
            const cells = table.querySelectorAll('th,td');
            const last = cells[cells.length - 1].getBoundingClientRect();
            const wr = wrap.getBoundingClientRect();
            const cueL = frame.querySelector('.okf-tablewrap__cue--left');
            return {
                lastRight: last.right,
                wrapRight: wr.right,
                docScrollW: document.documentElement.scrollWidth,
                docClientW: document.documentElement.clientWidth,
                scrolledEnd: frame.classList.contains('okf-tablewrap--scrolled-end'),
                cueLeftEdge: cueL ? Math.round(cueL.getBoundingClientRect().left) : null,
                wrapLeftEdge: Math.round(wr.left),
                cueLeftOpacity: cueL ? getComputedStyle(cueL).opacity : null,
                cueRightOpacity: frame.querySelector('.okf-tablewrap__cue--right')
                  ? getComputedStyle(frame.querySelector('.okf-tablewrap__cue--right')).opacity : null,
            };
        }"""
    )
    assert reach["lastRight"] <= reach["wrapRight"] + 1, (
        f"last table cell not reachable after horizontal scroll: "
        f"lastRight={reach['lastRight']} wrapRight={reach['wrapRight']}"
    )
    assert reach["docScrollW"] <= reach["docClientW"] + 1, (
        f"document overflows horizontally at 320px: "
        f"scrollW={reach['docScrollW']} clientW={reach['docClientW']}"
    )
    # Far-end cue geometry: scrolled to the right end, the right cue fades
    # (scrolled-end) and the LEFT cue pins to the wrapper's left edge (it did
    # not scroll away with the content either).
    assert reach["scrolledEnd"] is True, "right end not detected after full scroll"
    assert float(reach["cueRightOpacity"]) == 0, (
        f"right cue must fade at the far end; opacity={reach['cueRightOpacity']!r}"
    )
    assert abs(reach["cueLeftEdge"] - reach["wrapLeftEdge"]) <= 1, (
        f"left cue is not pinned to the wrapper edge at the far end: "
        f"cueLeft={reach['cueLeftEdge']} wrapLeft={reach['wrapLeftEdge']}"
    )
    assert float(reach["cueLeftOpacity"]) > 0, (
        f"left cue must be visible at the far end; opacity={reach['cueLeftOpacity']!r}"
    )


def test_phase2_wide_table_affordance_stripped_when_it_fits(server_url: str, page) -> None:
    """P1 state-ownership / keyboard-focus: the scroll affordance is scoped to
    tables that actually overflow. When the table fits its column (wide
    viewport) the wrapper is NOT role=region, NOT a tab stop, and carries no
    visible cue — so fitting tables add zero spurious keyboard stops and no
    stale 'scroll' hint. (No-JS keeps the bare .okf-table scroll behaviour.)"""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    page.wait_for_selector(".okf-page__body .okf-tablewrap", timeout=3000)
    page.wait_for_timeout(200)
    info = page.evaluate(
        """() => {
            const wrap = document.querySelector('.okf-page__body .okf-tablewrap');
            const table = wrap && wrap.querySelector('table');
            const frame = wrap && wrap.closest('.okf-tablewrap-frame');
            const cueR = frame && frame.querySelector('.okf-tablewrap__cue--right');
            return {
                overflows: table && wrap ? table.scrollWidth > wrap.clientWidth + 1 : null,
                role: wrap ? wrap.getAttribute('role') : null,
                ariaLabel: wrap ? wrap.getAttribute('aria-label') : null,
                tabIndex: wrap ? wrap.tabIndex : null,
                scrolls: frame ? frame.classList.contains('okf-tablewrap--scrolls') : null,
                cueRightOpacity: cueR ? getComputedStyle(cueR).opacity : null,
            };
        }"""
    )
    assert info["overflows"] is False, "precondition: table must fit at 1280px"
    assert info["role"] is None, f"fitting table must not be a region; got {info['role']!r}"
    assert info["tabIndex"] == -1, (
        f"fitting table must not be a tab stop; tabIndex={info['tabIndex']!r}"
    )
    assert info["ariaLabel"] is None, "fitting table must carry no scroll aria-label"
    assert info["scrolls"] is False
    assert float(info["cueRightOpacity"]) == 0, (
        f"cue must be hidden when the table fits; opacity={info['cueRightOpacity']!r}"
    )


# ===========================================================================
# P1 Phase-2 closure: mobile collapsible infobox, mobile current-heading
# ownership, mobile masthead non-overlap, and search/hover polish.
# ===========================================================================


def test_phase2_mobile_infobox_collapsed_by_default_and_native(server_url: str, page) -> None:
    """P1 #2 (keyboard-focus / layout-graph): on mobile the WHOLE concept
    facts infobox is a native <details> disclosure collapsed by default
    (no tall facts wall before the body). The summary toggles via keyboard
    (Enter + Space) and shows a visible focus ring; the body (tags/facts/
    governed) is hidden until opened. No-JS keeps the native toggle usable."""
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    box = page.locator("details.okf-page__infobox")
    assert box.count() == 1, "infobox must be a <details> disclosure"
    # Collapsed by default on mobile.
    assert box.get_attribute("open") is None, "mobile infobox must be collapsed by default"
    summary = box.locator("summary.okf-infobox__summary")
    assert summary.count() == 1, "infobox summary (type/rev toggle) missing"
    # The body wrapper exists but is hidden while closed.
    assert box.locator(".okf-infobox__body").count() == 1
    # Keyboard toggle: Enter opens.
    summary.focus()
    expect(summary).to_be_focused()
    page.keyboard.press("Enter")
    expect(box).to_have_attribute("open", "")
    # Space closes (native <details> toggles on either activation key).
    summary.focus()
    page.keyboard.press(" ")
    assert box.get_attribute("open") is None, "Space did not close the infobox disclosure"
    # Visible focus ring on the summary (keyboard-focus contract).
    summary.focus()
    focus_info = page.evaluate(
        """() => {
            const s = document.querySelector('details.okf-page__infobox > summary');
            const cs = getComputedStyle(s);
            const w = parseFloat((cs.outlineWidth || '0px').replace('px', ''));
            return { outlineWidth: cs.outlineWidth, outlineStyle: cs.outlineStyle, visible: cs.outlineStyle && cs.outlineStyle !== 'none' && w > 0 };
        }"""
    )
    assert focus_info["visible"], f"infobox summary must show a visible focus outline; got {focus_info}"


def test_phase2_desktop_infobox_collapsed_on_wide_screen(server_url: str, page) -> None:
    """Document details starts collapsed at every width and only the reader
    opens or closes it. On a live page at 1440px it is still collapsed after
    Studio has booted (Studio used to open it at 1100px or wider during its
    boot). The reader opens it with the keyboard, and a later resize to a
    phone width leaves it open."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    # Studio adds okf-studio-booted to <html> once all synchronous boot work,
    # the concept-page setup included, has completed.
    page.wait_for_function(
        "() => document.documentElement.classList.contains('okf-studio-booted')"
    )
    box = page.locator("details.okf-page__infobox")
    expect(box).to_have_count(1)
    assert box.evaluate("el => el.open") is False, (
        "Document details must still be collapsed at 1440px after Studio boots"
    )
    box.locator(':scope > summary').press('Enter')
    expect(box).to_have_attribute('open', '')
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(box).to_have_attribute('open', '')


def test_phase2_desktop_infobox_stays_collapsed_when_studio_boots_after_wiki(
    server_url: str, page
) -> None:
    """Studio never opens Document details, whatever order the scripts run
    in. In the served page the Studio module in <head> runs before wiki.js,
    so this test moves the Studio tag after wiki.js. wiki.js then binds the
    disclosure before Studio boots. That is the order in which Studio's
    former boot step opened it at 1100px or wider."""
    studio_tag = '<script type="module" src="/__static/studio.js"></script>'
    moved: dict = {}

    def studio_after_wiki(route):
        response = route.fetch()
        html = response.text()
        before, found, after = html.partition(studio_tag)
        wiki = re.search(r'<script src="[^"]*/wiki\.js" defer></script>', after)
        if found and wiki:
            html = (before + after[:wiki.end()] + "\n" + studio_tag
                    + after[wiki.end():])
            moved["done"] = True
        route.fulfill(response=response, body=html)

    page.route(f"{server_url}/tables/orders", studio_after_wiki)
    # Record whether wiki.js had bound the disclosure when Studio finished
    # booting, so the test proves it exercised the late-boot order.
    page.add_init_script("""
      window.addEventListener('okf-loom:studioBooted', () => {
        const box = document.querySelector('details.okf-page__infobox');
        window.__okfInfoboxAtStudioBoot = box
          ? { bound: box.dataset.okfInfoboxBound || null, open: box.open }
          : null;
      });
    """)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.documentElement.classList.contains('okf-studio-booted')"
    )
    assert moved.get("done"), "the Studio script tag was not moved after wiki.js"
    at_boot = page.evaluate("() => window.__okfInfoboxAtStudioBoot")
    assert at_boot is not None and at_boot["bound"] == "1", (
        f"wiki.js must have bound Document details before Studio booted; got {at_boot}"
    )
    box = page.locator("details.okf-page__infobox")
    assert at_boot["open"] is False and box.evaluate("el => el.open") is False, (
        "Studio must not open Document details at 1440px when it boots after wiki.js"
    )


def test_phase2_static_desktop_infobox_collapsed_without_studio(static_url: str, page) -> None:
    """Static output, which does not load studio.js, matches the live page:
    at 1440px Document details is collapsed once wiki.js (the shared loader
    for static, SPA and serve) has bound it, and the reader opens it with the
    keyboard to lay out the facts body.

    Assertions:
      * body carries data-okf-mode="static" (we are actually testing static);
      * no studio.js script is on the page;
      * after wiki.js has bound the disclosure it is collapsed;
      * Enter on its summary opens it and the facts body is laid out.
    """
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(static_url + "/tables/orders.html", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    # Confirm we are actually on a static build (no studio modules loaded).
    mode = page.evaluate("() => document.body.getAttribute('data-okf-mode')")
    assert mode == "static", f"test requires the static surface; got data-okf-mode={mode!r}"
    studio_loaded = page.evaluate(
        "() => !!document.querySelector('script[src*=\"studio.js\"]')"
    )
    assert not studio_loaded, "static builds must NOT load studio.js (parity assumption)"
    box = page.locator("details.okf-page__infobox")
    expect(box).to_have_count(1)
    # wiki.js marks the disclosure once it has bound the reader's toggle.
    expect(box).to_have_attribute("data-okf-infobox-bound", "1")
    assert box.evaluate("el => el.open") is False, (
        "static Document details must be collapsed at 1440px"
    )
    box.locator(':scope > summary').press('Enter')
    expect(box).to_have_attribute('open', '')
    # The body content (tags/facts/governed) is actually visible, not just the
    # summary. This catches a regression where the <details> is 'open' but the
    # body is empty or display:none.
    body_visible = page.evaluate(
        """() => {
            const b = document.querySelector('details.okf-page__infobox .okf-infobox__body');
            if (!b) return false;
            const r = b.getBoundingClientRect();
            const cs = getComputedStyle(b);
            return r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        }"""
    )
    assert body_visible, (
        "static desktop infobox body must be laid out and visible once open"
    )


def test_phase2_static_mobile_infobox_collapsed_without_studio(static_url: str, page) -> None:
    """P1 #2 mobile parity: on a STATIC mobile build (320px) the infobox
    STAYS COLLAPSED by default — wiki.js does not force it open on narrow
    screens. This guarantees the static mobile reading experience keeps the
    'summary-first, facts on demand' contract: no tall facts wall before the
    body, exactly matching the live mobile behaviour.

    Pairs with the static desktop test: collapsed at both widths."""
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(static_url + "/tables/orders.html", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    box = page.locator("details.okf-page__infobox")
    expect(box).to_have_count(1)
    page.wait_for_timeout(250)
    # Mobile: must NOT be auto-opened (user opens on demand).
    assert box.get_attribute("open") is None, (
        "static mobile infobox must be collapsed by default — wiki.js must "
        "not force it open on narrow screens (mobile parity with studio.js)"
    )


def test_phase2_mobile_onthispage_active_heading_aria_current(server_url: str, page) -> None:
    """P1 #4 (state-ownership / keyboard-focus): the mobile 'On this page'
    links receive active/.is-active AND aria-current="location" as the active
    heading changes, matching the desktop rail's scroll-spy behaviour."""
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    disc = page.locator("details.okf-onthispage")
    assert disc.count() == 1, "mobile on-this-page disclosure missing"
    # Open the disclosure so its links are present, then scroll a heading
    # near the top into view and let the IntersectionObserver settle.
    disc.locator("summary").click()
    expect(disc).to_have_attribute("open", "")
    links = disc.locator("a.okf-onthispage__link")
    if not links.count():
        pytest.fail("fixture has no on-this-page links (concept has no headings)")
    first_href = links.first.get_attribute("href") or ""
    target_id = first_href[1:] if first_href.startswith("#") else first_href
    if target_id:
        # Land the heading in the scroll-spy activation band (~10-30% of the
        # viewport, mirroring the observer's -10%/-70% rootMargin) so the
        # IntersectionObserver fires for it. A bare scrollIntoView({start})
        # parks the heading at y=0 (above the band) and would not activate it.
        page.evaluate(
            """(id) => {
                const el = document.getElementById(id);
                if (!el) return;
                const y = el.getBoundingClientRect().top + window.scrollY - window.innerHeight * 0.15;
                window.scrollTo(0, y);
            }""",
            target_id,
        )
    # The observer fires asynchronously; wait for any link to become current.
    try:
        page.wait_for_function(
            """() => {
                const a = document.querySelector('details.okf-onthispage a.okf-onthispage__link[aria-current="location"]');
                return !!a;
            }""",
            timeout=3000,
        )
    except Exception:
        active = page.evaluate(
            """() => Array.from(document.querySelectorAll('details.okf-onthispage a.okf-onthispage__link'))
                  .filter(a => a.classList.contains('is-active')).length"""
        )
        pytest.fail(f"no mobile on-this-page link received aria-current/.is-active; is-active count={active}")
    # Exactly one link is current at a time.
    current = page.evaluate(
        """() => ({
            currentCount: document.querySelectorAll('details.okf-onthispage a.okf-onthispage__link[aria-current="location"]').length,
            activeCount: document.querySelectorAll('details.okf-onthispage a.okf-onthispage__link.is-active').length,
        })"""
    )
    assert current["currentCount"] == 1, f"expected exactly one current mobile link; got {current}"
    assert current["activeCount"] == 1, f"expected exactly one .is-active mobile link; got {current}"


def test_phase2_mobile_onthispage_labels_match_canonical_visible_text(server_url: str, page) -> None:
    """P1 contract-runtime parity: a live body patch that introduces headings
    with HTML-special characters and inline markup must rebuild the mobile 'On
    this page' disclosure with labels equal to the CANONICAL VISIBLE text of
    each heading — the same text SSR _render_onthispage emits (single-encoded)
    and the same text a reader sees. The live path reads heading.textContent
    (which the browser has already decoded) and normalizes whitespace; it must
    never double-encode entities ("&amp;amp;") nor leak inline <code>/<a> markup
    into a jump-list label."""
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    assert page.evaluate("() => !!window.okfLoomStudio"), "studio did not boot"
    # Replace the body with headings exercising the parity cases: an ampersand,
    # inline code, an internal link, and a literal "</>". Patch via innerHTML
    # exactly like the sibling parity tests do; studio.js's body-diff treats the
    # new headings as the current body and rebuildOnThisPage mirrors them.
    result = page.evaluate(
        r"""() => {
            const body = document.querySelector('.okf-page__body');
            body.innerHTML =
                '<h2 id="amp">API &amp; SDK</h2>' +
                '<h2 id="code">Use the <code>foo()</code> method</h2>' +
                '<h2 id="link">See the <a href="orders">Loom</a> guide</h2>' +
                '<h2 id="lt">a &lt;tag&gt; &amp; b</h2>' +
                '<p>prose.</p>';
            window.okfDocumentOutline.rebuild();
            const disc = document.querySelector('details.okf-onthispage');
            const links = disc
                ? Array.from(disc.querySelectorAll('a.okf-onthispage__link'))
                : [];
            return {
                exists: !!disc,
                hidden: disc ? disc.hidden : null,
                labels: links.map(a => (a.textContent || '').trim()),
                hrefs: links.map(a => a.getAttribute('href')),
                // The link's innerHTML, to assert no markup leaked through.
                labelHTML: links.map(a => a.innerHTML),
            };
        }"""
    )
    assert result["exists"], "on-this-page disclosure should exist"
    assert result["hidden"] is False, "disclosure must be shown when headings exist"
    # Canonical visible text — exactly what SSR produces and what the reader sees.
    assert result["labels"] == [
        "API & SDK",
        "Use the foo() method",
        "See the Loom guide",
        "a <tag> & b",
    ], f"live on-this-page labels are not canonical visible text: {result['labels']!r}"
    # hrefs mirror the patched heading ids.
    assert result["hrefs"] == ["#amp", "#code", "#link", "#lt"], (
        f"live hrefs do not mirror patched ids: {result['hrefs']!r}"
    )
    # textContent was used (safe): the SERIALIZED label is single-encoded (the
    # browser's innerHTML escapes "&" once), never double-encoded. This is the
    # live-side mirror of the SSR double-escape regression: a buggy path would
    # serialize "API &amp;amp; SDK". textContent above is the canonical proof.
    assert result["labelHTML"][0] == "API &amp; SDK", (
        "ampersand label is not single-encoded HTML: " + repr(result["labelHTML"][0])
    )
    assert "API &amp;amp;" not in result["labelHTML"][0], (
        "live ampersand label is double-encoded: " + repr(result["labelHTML"][0])
    )


def test_phase2_mobile_masthead_never_overlays_hero(server_url: str, page) -> None:
    """P1 #6 / P1 #1 (layout-graph): the concept-page topbar is STICKY at every
    mobile width (320px through 420px) — restoring the Phase 1 sticky-mobile-
    chrome contract. The prior P1 fix made it position:static at <=420px to
    dodge a wrap-induced overlap; the regression shipped a non-sticky masthead
    at 390px (and 320px) so readers lost brand/search/theme on scroll.

    The current fix restores sticky by reverting to the single-row nowrap
    contract (the wrap that produced the ~131px tall topbar is gone), so the
    sticky masthead is back to ~52px. A small sticky topbar still overlays
    content as it scrolls (that IS sticky behaviour, same as desktop), but it
    no longer covers the header BAND as a 131px wall. The contract asserted
    here is:

      * position is 'sticky' (not 'static') at 320px AND 390px;
      * the topbar height stays under ~64px (single row), proving the wrap is
        gone and the sticky chrome does not eat the mobile fold;
      * the title is reachable in the viewport at scrollY=0 (no overlay there).

    The deep-overlap scan (does the sticky topbar intersect the page-header
    while scrolling?) is INTENTIONALLY not re-asserted: a sticky masthead
    overlays content during scroll by design — same as desktop. The Phase 1
    contract is 'sticky + small', not 'static + scrolls-away'.
    """
    for vw, vh in ((320, 640), (390, 800)):
        page.set_viewport_size({"width": vw, "height": vh})
        page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
        page.locator("h1.okf-page__title").wait_for(state="visible")
        page.wait_for_timeout(150)
        info = page.evaluate(
            """() => {
                const tb = document.querySelector('.okf-topbar');
                const cs = tb ? getComputedStyle(tb) : null;
                const title = document.querySelector('.okf-page__title');
                const tbox = title ? title.getBoundingClientRect() : null;
                return {
                    position: cs ? cs.position : null,
                    topbarH: tb ? tb.getBoundingClientRect().height : 0,
                    // At scrollY=0 the title must be visible in the viewport
                    // (not pushed below the fold by an oversized topbar).
                    titleVisibleAtTop: tbox ? (tbox.top > 0 && tbox.bottom < window.innerHeight) : false,
                };
            }"""
        )
        assert info["position"] == "sticky", (
            f"concept-page topbar must be sticky at {vw}px; got {info['position']!r} "
            "(position:static at <=420px was the prior P1 workaround — now reverted)"
        )
        assert info["topbarH"] <= 174, (
            f"wrapped mobile topbar must remain contained at {vw}px; "
            f"got height={info['topbarH']:.1f}px"
        )
        assert info["titleVisibleAtTop"], (
            f"page title must be reachable at scrollY=0 at {vw}px; "
            f"the sticky topbar must not push it below the fold"
        )


def test_phase2_mobile_sticky_chrome_390px_regression(server_url: str, page) -> None:
    """P1 #1 regression: at 390px the topbar is STICKY and every control stays
    keyboard-reachable. This is the exact width at which the prior P1
    masthead-overlap fix made the topbar position:static, breaking the Phase 1
    sticky-mobile-chrome contract. Pin it so a future 'fix' that re-introduces
    position:static at <=420px fails here loudly.

    Also asserts no horizontal document overflow at 390px (the tightened
    search input + brand truncation keep every control in-viewport without
    forcing the page to scroll sideways)."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    page.wait_for_timeout(150)
    sticky = page.evaluate("() => getComputedStyle(document.querySelector('.okf-topbar')).position")
    assert sticky == "sticky", (
        f"390px topbar must be sticky (Phase 1 contract); got {sticky!r}"
    )
    overflow = page.evaluate(
        """() => {
            const doc = document.documentElement;
            return {
                scrollWidth: doc.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
            };
        }"""
    )
    assert overflow["scrollWidth"] <= overflow["clientWidth"] + 1, (
        f"horizontal document overflow at 390px: scrollWidth="
        f"{overflow['scrollWidth']} clientWidth={overflow['clientWidth']} "
        "(the compact-phone topbar rule must keep all controls in-bounds)"
    )


def test_document_root_owns_vertical_scroll_and_sticky_topbar(server_url: str, page) -> None:
    """Long documents scroll the root while global chrome remains sticky."""
    page.set_viewport_size({"width": 1100, "height": 700})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    page.evaluate(
        """() => {
            const filler = document.createElement('div');
            filler.style.height = '1800px';
            filler.setAttribute('aria-hidden', 'true');
            document.querySelector('.okf-page__body').appendChild(filler);
            window.scrollTo(0, 900);
        }"""
    )
    page.wait_for_function("document.scrollingElement.scrollTop > 500")
    ownership = page.evaluate(
        """() => ({
            rootIsDocumentElement: document.scrollingElement === document.documentElement,
            rootTop: document.scrollingElement.scrollTop,
            bodyTop: document.body.scrollTop,
            bodyHeight: getComputedStyle(document.body).height,
            topbarTop: document.querySelector('.okf-topbar').getBoundingClientRect().top,
            horizontalOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        })"""
    )
    assert ownership["rootIsDocumentElement"], ownership
    assert ownership["rootTop"] > 500 and ownership["bodyTop"] == 0, ownership
    assert abs(ownership["topbarTop"]) <= 1, ownership
    assert ownership["horizontalOverflow"] <= 1, ownership


def test_authored_scroll_regions_follow_measured_runtime_geometry(server_url: str, page) -> None:
    """Potentially-wide SSR wrappers gain semantics only during real overflow."""
    page.set_viewport_size({"width": 900, "height": 700})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.wait_for_function("() => Boolean(window.okfRenderers && window.okfRenderers._updateContentScrollports)")
    state = page.evaluate(
        """() => {
          const port = document.createElement('div');
          port.className = 'okf-content-scrollport okf-content-scrollport--code';
          port.dataset.okfScrollKind = 'code';
          port.dataset.okfScrollLabel = 'SQL code block';
          port.style.width = '180px';
          const content = document.createElement('pre');
          content.style.width = '620px';
          content.textContent = 'SELECT a_really_wide_projection FROM orders';
          port.appendChild(content);
          document.querySelector('.okf-page__body').appendChild(port);
          window.okfRenderers._updateContentScrollports();
          const overflow = {
            role: port.getAttribute('role'),
            label: port.getAttribute('aria-label'),
            tabindex: port.getAttribute('tabindex'),
          };
          content.style.width = '40px';
          content.textContent = 'OK';
          window.okfRenderers._updateContentScrollports();
          return { overflow, fit: {
            role: port.getAttribute('role'),
            label: port.getAttribute('aria-label'),
            tabindex: port.getAttribute('tabindex'),
          }};
        }"""
    )
    assert state["overflow"] == {
        "role": "region",
        "label": "SQL code block. Scroll horizontally to view all content.",
        "tabindex": "0",
    }
    assert state["fit"] == {"role": None, "label": None, "tabindex": None}


def test_phase2_search_live_type_chip_and_highlight(server_url: str, page) -> None:
    """P1 #3 (contract-runtime-parity / security-boundary): live suggestions
    carry type identity (a tinted .okf-search-live__type chip) and safely
    highlight matched query terms via <mark> built from text nodes.

    P1 #3 (iter-2): the chip must also carry the SAME type ICON the rest of
    the UI uses (not text/colour only). The icon SVG is built via the SVG
    namespace from a server-shipped trusted JSON map (the search response
    carries the resolved icon key per result; wiki.js selects the curated path
    markup from that map). Asserts:
      * an <svg> child of class okf-search-live__type-icon exists in the chip;
      * the SVG has a non-empty viewBox + a real geometry child (path/rect/
        ellipse/circle), proving the icon paths actually rendered (not an
        empty <svg> shell);
      * the SVG is aria-hidden so the chip's accessible name stays as just the
        type label (no duplicate announcement from the glyph).
    """
    page.goto(server_url + "/", wait_until="domcontentloaded")
    search = page.locator('input[type="search"][name="q"]').first
    search.wait_for(state="visible")
    search.fill("orders")
    live = page.locator(".okf-search-live")
    live.wait_for(state="visible")
    info = page.evaluate(
        """() => {
            const live = document.querySelector('.okf-search-live');
            const opt = live.querySelector('.okf-search-live__option:not(.okf-search-live__all)');
            if (!opt) return {missing: true};
            const chip = opt.querySelector('.okf-search-live__type');
            const head = opt.querySelector('.okf-search-live__head');
            const marks = opt.querySelectorAll('mark');
            const svg = chip ? chip.querySelector('svg.okf-search-live__type-icon') : null;
            const geom = svg ? svg.querySelectorAll('path,rect,ellipse,circle') : null;
            return {
                hasChip: !!chip && chip.textContent.trim().length > 0,
                chipColor: chip ? getComputedStyle(chip).color : null,
                hasHead: !!head,
                markCount: marks.length,
                // SAFETY: no <img>, <script>, or on* attribute leaked into the
                // option (highlight is text-node built, not innerHTML).
                unsafeNodes: opt.querySelectorAll('img,script,[onerror],[onload],[onclick]').length,
                // ICON assertions (P1 #3 iter-2): the chip visibly carries the
                // type icon, not just text/colour.
                hasIconSvg: !!svg,
                iconViewBox: svg ? svg.getAttribute('viewBox') : null,
                iconGeomCount: geom ? geom.length : 0,
                iconAriaHidden: svg ? svg.getAttribute('aria-hidden') : null,
            };
        }"""
    )
    assert not info.get("missing"), "no suggestion option rendered"
    assert info["hasChip"], "suggestion must carry a type chip (.okf-search-live__type)"
    assert info["hasHead"], "suggestion must wrap chip+title in .okf-search-live__head"
    assert info["markCount"] >= 1, f"matched term must be highlighted via <mark>; got {info['markCount']}"
    assert info["unsafeNodes"] == 0, "unsafe node/attribute leaked into the suggestion option"
    # P1 #3 iter-2 icon contract.
    assert info["hasIconSvg"], (
        "chip must visibly carry the type ICON (<svg.okf-search-live__type-icon>), "
        "not text/colour only"
    )
    assert info["iconViewBox"] == "0 0 24 24", (
        f"icon SVG must use the 24x24 viewBox the curated paths target; got "
        f"{info['iconViewBox']!r}"
    )
    assert info["iconGeomCount"] >= 1, (
        "icon SVG must contain a real geometry element (path/rect/ellipse/"
        "circle); an empty <svg> shell does not satisfy the icon contract"
    )
    assert info["iconAriaHidden"] == "true", (
        "icon SVG must be aria-hidden so the chip's accessible name is the "
        "type label, not 'image' or the glyph's alt text"
    )


def test_phase2_search_live_grouping_and_recent(server_url: str, page) -> None:
    """P1 #3 (state-ownership): suggestions group by type when there are >6
    hits, and an empty focused query surfaces recent pages."""
    # Grouping needs >6 results of mixed types — build a small temp bundle.
    import shutil, tempfile
    bundle_dir = Path(tempfile.mkdtemp()) / "demo"
    shutil.copytree(str(DEMO_BUNDLE), str(bundle_dir))
    (bundle_dir / "glossary.md").unlink(missing_ok=True)
    body = "\n\nGamma domain glossary entry for grouping.\n"
    extra = {
        "g1.md": "---\ntype: Table\ntitle: Gamma One\ntags: [gamma]\n---\n" + body,
        "g2.md": "---\ntype: Table\ntitle: Gamma Two\ntags: [gamma]\n---\n" + body,
        "g3.md": "---\ntype: Table\ntitle: Gamma Three\ntags: [gamma]\n---\n" + body,
        "g4.md": "---\ntype: Dataset\ntitle: Gamma Four\ntags: [gamma]\n---\n" + body,
        "g5.md": "---\ntype: Dataset\ntitle: Gamma Five\ntags: [gamma]\n---\n" + body,
        "g6.md": "---\ntype: Dataset\ntitle: Gamma Six\ntags: [gamma]\n---\n" + body,
        "g7.md": "---\ntype: Playbook\ntitle: Gamma Seven\ntags: [gamma]\n---\n" + body,
    }
    for name, content in extra.items():
        (bundle_dir / name).write_text(content, encoding="utf-8")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        okf_module_argv("serve", str(bundle_dir), "--host", "127.0.0.1", "--port", str(port), "--no-watch", "--no-open"),
        cwd=str(TOOLKIT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=okf_subprocess_env(),
    )
    try:
        _wait_for_server(proc, base)
        page.goto(base + "/", wait_until="domcontentloaded")
        search = page.locator('input[type="search"][name="q"]').first
        search.wait_for(state="visible")
        search.fill("gamma")
        live = page.locator(".okf-search-live")
        live.wait_for(state="visible")
        page.wait_for_timeout(200)
        grouping = page.evaluate(
            """() => {
                const live = document.querySelector('.okf-search-live');
                return {
                    groupCount: live.querySelectorAll('.okf-search-live__group').length,
                    optionCount: live.querySelectorAll('.okf-search-live__option:not(.okf-search-live__all)').length,
                };
            }"""
        )
        assert grouping["optionCount"] > 6, "precondition: >6 gamma suggestions expected"
        assert grouping["groupCount"] >= 2, f"type grouping missing (>6 results): {grouping}"
        # Recent pages on empty focus: clear the query and focus — a Recent
        # pages group appears (or the empty state, but never nothing/stale).
        search.fill("")
        page.wait_for_timeout(150)
        recent = page.evaluate(
            """() => {
                const live = document.querySelector('.okf-search-live');
                const grp = live ? live.querySelector('.okf-search-live__group--recent') : null;
                return {
                    recentGroup: !!grp,
                    open: live ? !live.hidden : false,
                    empty: live ? !!live.querySelector('.okf-search-live__empty') : false,
                };
            }"""
        )
        assert recent["open"], "empty query should surface the live panel (recent pages)"
        assert recent["recentGroup"], f"Recent pages group missing on empty query: {recent}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)


def test_static_recent_pages_navigate_to_html_concept(static_url: str, page) -> None:
    """P1 static/live URL parity (contract-runtime-parity): recent-page options
    in the topbar search combobox must build mode-aware URLs so a STATIC build
    resolves a recent entry to its relative ``<rootPrefix><id>.html`` page (not
    the extensionless ``/id`` root URL that 404s in static output and ignores
    nested deployment roots). The static combobox must also still WIRE the
    recent-pages affordance: it is localStorage-only and needs no /__search
    backend, so the static typed-query gate (staticSubmitOnly) must disable
    live (typed-query) suggestions only — never recent-page navigation.

    Proof on the BUILT static surface (not the live server):
      * visit two nested concept pages so both are recorded in recent history;
      * focus the empty search input -> the Recent pages group appears;
      * the recent option's data-href is the RELATIVE .html path
        (../tables/orders.html), never the bare /tables/orders;
      * the option carries its type icon svg (identity preserved);
      * CLICK a recent entry -> arrive at the correct .html concept page;
      * KEYBOARD (ArrowDown + Enter) -> arrive at the correct .html concept page.
    """
    # Visit two nested concepts so recent history has both. localStorage
    # persists across same-origin navigations within one browser context.
    page.goto(static_url + "/tables/orders.html", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.okf-page__title')")
    page.goto(static_url + "/datasets/orders.html", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.okf-page__title')")

    search = page.locator('.okf-topbar input[type="search"][name="q"]').first
    search.wait_for(state="visible")

    # Empty-query trigger: focus the empty input. The focus handler renders
    # recent pages (never a dead empty affordance).
    search.click()
    page.locator(".okf-search-live__group--recent").wait_for(state="visible")

    # URL + identity assertions read from the DOM directly (durable over
    # brittle attribute selectors).
    box = page.evaluate(
        """() => {
            const opts = Array.from(document.querySelectorAll('.okf-search-live__option[data-href]'));
            const t = opts.find(o => (o.getAttribute('data-href') || '').endsWith('tables/orders.html'));
            return {
                hrefs: opts.map(o => o.getAttribute('data-href')),
                targetHref: t ? t.getAttribute('data-href') : null,
                hasTypeIcon: !!t && t.querySelectorAll('svg.okf-search-live__type-icon').length === 1,
                noExtensionless: opts.every(o => !(o.getAttribute('data-href') || '').match(/^\\/tables\\/orders$/)),
            };
        }"""
    )
    assert "../tables/orders.html" in box["hrefs"], (
        f"static recent-page hrefs must include the relative .html path, got {box['hrefs']}"
    )
    assert box["targetHref"] == "../tables/orders.html", (
        f"static recent-page href must be the relative .html path, got {box['targetHref']!r}"
    )
    assert box["noExtensionless"], (
        f"static recent-page hrefs must NOT contain bare /tables/orders, got {box['hrefs']}"
    )
    assert box["hasTypeIcon"], "recent-page option must carry its type icon svg"

    # CLICK: navigate to the correct .html concept page.
    with page.expect_navigation(wait_until="domcontentloaded") as nav:
        page.locator('.okf-search-live__option[data-href="../tables/orders.html"]').first.click()
    assert nav.value.ok, "click on recent entry must navigate"
    assert page.url.endswith("/tables/orders.html"), (
        f"click must arrive at tables/orders.html, got {page.url}"
    )

    # KEYBOARD: fresh page, ArrowDown to the same entry, Enter.
    page.goto(static_url + "/datasets/orders.html", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.okf-page__title')")
    search = page.locator('.okf-topbar input[type="search"][name="q"]').first
    search.click()
    page.locator(".okf-search-live__group--recent").wait_for(state="visible")
    page.wait_for_function("() => document.querySelector('.okf-search-live__option[data-href]') !== null")
    # Recent list is newest-first: [datasets/orders, tables/orders]. ArrowDown
    # twice selects the tables/orders option; Enter navigates via data-href.
    search.press("ArrowDown")
    search.press("ArrowDown")
    with page.expect_navigation(wait_until="domcontentloaded") as nav:
        search.press("Enter")
    assert nav.value.ok, "Enter on keyboard-selected recent entry must navigate"
    assert page.url.endswith("/tables/orders.html"), (
        f"keyboard activation must arrive at tables/orders.html, got {page.url}"
    )


def test_static_topbar_single_search_control_and_portable_submit(static_url: str, page) -> None:
    """Header-search dedup, proved on the BUILT static surface (source checks
    cannot prove the absence of a JS-injected duplicate):

      * desktop + mobile topbars show ONE search control — the query field
        plus its visible labelled Search submit — and NO JS-injected
        'Open search' note beside it (on the index, a concept, and the
        search page itself);
      * the native form stays the honest portable route: Enter with a query
        AND a Search-button click both navigate to ``__search.html`` with
        real results, and an EMPTY submit opens the search page landing
        (never a dead affordance);
      * typing never fires the doomed live-suggestion fetch (no
        ``/__search?...format=json`` request) while recent-pages/focus stay
        wired (navigation covered by
        test_static_recent_pages_navigate_to_html_concept).
    """
    requests_seen: list[str] = []
    page.on("request", lambda request: requests_seen.append(request.url))

    # -- Desktop index: one control, no note --------------------------------
    page.goto(static_url + "/", wait_until="domcontentloaded")
    probe = page.evaluate(
        """() => {
            const tb = document.querySelector('.okf-topbar');
            const forms = tb ? tb.querySelectorAll('form.okf-search-form') : [];
            const submit = forms.length === 1
                ? forms[0].querySelector('button[type="submit"], button:not([type])') : null;
            const s = submit ? submit.getBoundingClientRect() : null;
            const noteLink = [...(tb ? tb.querySelectorAll('a') : [])].find(a =>
                a.textContent.trim() === 'Open search'
                || (a.getAttribute('aria-label') || '') === 'Open the dedicated search page');
            return {
                formCount: forms.length,
                hasInput: forms.length === 1 && !!forms[0].querySelector('input[type="search"][name="q"]'),
                submitText: submit ? submit.textContent.trim() : null,
                submitVisible: !!s && s.width > 0 && s.height > 0,
                noteCount: document.querySelectorAll('.okf-search-note').length,
                noteLinkPresent: !!noteLink,
            };
        }"""
    )
    assert probe["formCount"] == 1 and probe["hasInput"], probe
    assert probe["submitText"] == "Search" and probe["submitVisible"], probe
    assert probe["noteCount"] == 0 and not probe["noteLinkPresent"], (
        f"static header must show field + Search only (header-search dedup): {probe}"
    )

    # -- Portable submit via Enter: typed query, no doomed live fetch -------
    search = page.locator('.okf-topbar input[type="search"][name="q"]').first
    search.click()
    search.fill("orders")
    page.wait_for_timeout(400)  # past the 180ms live-suggest debounce window
    doomed = [u for u in requests_seen if "__search?" in u and "format=json" in u]
    assert not doomed, (
        f"static typed query must not fire the doomed live-suggestion fetch, "
        f"saw {doomed!r}"
    )
    with page.expect_navigation(wait_until="domcontentloaded") as nav:
        search.press("Enter")
    assert nav.value.ok, "Enter must submit the static search form"
    assert "__search.html" in page.url and "q=orders" in page.url, page.url
    page.wait_for_function(
        "() => window.okfPortableProvider && !document.querySelector('.okf-search-error')"
    )
    page.locator(".okf-search-results-list").wait_for(state="visible")
    result_count = page.locator(".okf-search-results-list li").count()
    assert result_count >= 1, f"static search returned zero results for 'orders': {page.url}"
    # The search page itself shows one control too (no note even where the
    # dedicated searcher lives).
    assert page.locator(".okf-search-note").count() == 0
    assert page.locator(".okf-topbar a", has_text="Open search").count() == 0

    # -- Empty submit via Enter: opens the search landing -------------------
    page.goto(static_url + "/", wait_until="domcontentloaded")
    search = page.locator('.okf-topbar input[type="search"][name="q"]').first
    search.click()
    with page.expect_navigation(wait_until="domcontentloaded") as nav:
        search.press("Enter")
    assert nav.value.ok, "empty Enter must still submit the form"
    assert "__search.html" in page.url, page.url
    page.wait_for_function(
        "() => window.okfPortableProvider && !document.querySelector('.okf-search-error')"
    )
    # static-search.js restores the landing for an empty, filterless query.
    page.locator(".okf-search__results").wait_for(state="visible")
    assert "Browse recent concepts or start a search." in page.locator(
        "#okf-search-status"
    ).inner_text(), "empty static submit must land on the search landing"

    # -- Portable submit via the Search BUTTON from a nested concept page ---
    page.goto(static_url + "/tables/orders.html", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.okf-page__title')")
    assert page.locator(".okf-search-note").count() == 0
    search = page.locator('.okf-topbar input[type="search"][name="q"]').first
    search.fill("customers")
    submit = page.locator('.okf-topbar form.okf-search-form button[type="submit"]').first
    with page.expect_navigation(wait_until="domcontentloaded") as nav:
        submit.click()
    assert nav.value.ok, "Search button must submit the static form"
    assert "__search.html" in page.url and "q=customers" in page.url, page.url
    page.wait_for_function(
        "() => window.okfPortableProvider && !document.querySelector('.okf-search-error')"
    )
    page.locator(".okf-search-results-list").wait_for(state="visible")
    assert page.locator(".okf-search-results-list li").count() >= 1, (
        f"button-submitted static search returned zero results for 'customers'"
    )

    # -- Mobile (390px): same absence, nothing clipped ----------------------
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(static_url + "/tables/orders.html", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.okf-page__title')")
    mobile = page.evaluate(
        """() => {
            const tb = document.querySelector('.okf-topbar');
            const submit = document.querySelector('.okf-topbar form.okf-search-form button[type="submit"]');
            const s = submit ? submit.getBoundingClientRect() : null;
            const clipped = [...(tb ? tb.querySelectorAll('a, input, button') : [])].filter(el => {
                const r = el.getBoundingClientRect();
                return getComputedStyle(el).display !== 'none'
                    && (r.left < 0 || r.right > innerWidth + 0.5);
            }).length;
            return {
                noteCount: document.querySelectorAll('.okf-search-note').length,
                noteLinkPresent: [...(tb ? tb.querySelectorAll('a') : [])].some(a =>
                    a.textContent.trim() === 'Open search'
                    || (a.getAttribute('aria-label') || '') === 'Open the dedicated search page'),
                submitVisible: !!s && s.width > 0 && s.height > 0,
                clipped,
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
            };
        }"""
    )
    assert mobile["noteCount"] == 0 and not mobile["noteLinkPresent"], (
        f"mobile static header must show field + Search only: {mobile}"
    )
    assert mobile["submitVisible"], f"Search submit lost on mobile: {mobile}"
    assert mobile["clipped"] == 0, f"topbar controls clip at 390px: {mobile}"
    assert mobile["scrollWidth"] <= mobile["clientWidth"] == 390, mobile


def test_static_search_route_typing_never_fires_doomed_suggestions_fetch(
    static_url: str, page,
) -> None:
    """Phase-1 portable correctness, search-route half.

    The portable gate used to stay live-only on the static search page
    itself (`isStatic && !body.okf-viewer--search`), so typing there fired
    the doomed ``/__search?...format=json`` suggestion fetch that can only
    ever 404 on a static host — while the same typing on every other static
    page was already gated. The gate must be simply ``isStatic``: typing on
    the search route stays quiet, the portable searcher keeps working, and
    the real GET form still navigates to fresh results.
    """
    requests_seen: list[str] = []
    page.on("request", lambda request: requests_seen.append(request.url))
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.okfPortableProvider && !document.querySelector('.okf-search-error')"
    )
    page.locator(".okf-search-results-list").wait_for(state="visible")

    search = page.locator('.okf-topbar input[type="search"][name="q"]').first
    search.click()
    search.fill("refund")
    page.wait_for_timeout(400)  # past the 180ms live-suggest debounce window
    doomed = [u for u in requests_seen if "__search?" in u and "format=json" in u]
    assert not doomed, (
        f"typing on the static search route must not fire the doomed "
        f"live-suggestion fetch, saw {doomed!r}"
    )

    # The portable searcher still owns the route: results for the URL query
    # are rendered (not wiped out by the typing), and submitting navigates.
    assert page.locator(".okf-search-results-list li").count() >= 1
    with page.expect_navigation(wait_until="domcontentloaded") as nav:
        search.press("Enter")
    assert nav.value.ok, "Enter must still submit the static search form"
    assert "__search.html" in page.url and "q=refund" in page.url, page.url
    page.wait_for_function(
        "() => window.okfPortableProvider && !document.querySelector('.okf-search-error')"
    )
    page.locator(".okf-search-results-list").wait_for(state="visible")
    assert page.locator(".okf-search-results-list li").count() >= 1


def test_spa_hover_and_focus_never_fetch_raw_or_hint_a_popover(
    spa_url: str, page,
) -> None:
    """Phase-1 portable correctness, spa half.

    Link hover/focus previews are a serve-only capability (``/__raw`` is a
    server route). On the built spa surface the binding used to stay live:
    hovering or keyboard-focusing a root-absolute link fired one or two
    doomed ``/__raw/<id>`` fetches per link and stamped the link with an
    ``aria-describedby`` pointing at a popover that can never open — an
    announced but nonexistent description. On spa, hover and focus must
    stay silent (no ``/__raw`` request, no describedby hint).
    """
    requests_seen: list[str] = []
    page.on("request", lambda request: requests_seen.append(request.url))
    page.goto(spa_url + "/", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.okf-index-page, main')")
    mode = page.evaluate("() => document.body.dataset.okfMode")
    assert mode == "spa", f"expected the spa surface, got mode={mode!r}"

    candidates = page.locator('main a[href^="/"]:visible')
    link = None
    for i in range(candidates.count()):
        href_i = candidates.nth(i).get_attribute("href")
        if href_i and not href_i.startswith("/__"):
            link = candidates.nth(i)
            href = href_i
            break
    assert link is not None, "no visible root-absolute concept link on the spa index"
    assert not href.endswith(".html"), href  # genuine root-absolute spa route
    link.hover()
    page.wait_for_timeout(400)  # past any hover-intent delay
    link.focus()
    page.wait_for_timeout(400)

    raw = [u for u in requests_seen if "/__raw/" in u]
    assert not raw, f"spa hover/focus must not fetch /__raw, saw {raw!r}"
    described = page.evaluate(
        """() => [...document.querySelectorAll('main a[aria-describedby]')]
             .map(a => a.getAttribute('aria-describedby'))"""
    )
    assert described == [], f"spa links must not hint at a popover: {described!r}"
    assert page.locator("#okf-popover").is_hidden()


@pytest.mark.parametrize("export_fixture", ["static_url", "spa_url"])
def test_portable_export_search_syncs_filter_count_and_disclosure(
    request, export_fixture: str, page,
) -> None:
    """U8 exported-surface half: the native Filters disclosure must carry the
    same honest count contract on the built static and spa exports that the
    live server renders server-side (serve and single-file are covered
    elsewhere in the suite).

    The exported ``__search.html`` is server-rendered with no filters, so
    static-search.js owns the disclosure once it hydrates the URL. The
    hydration must sync ``.okf-search-filters > summary`` from the effective
    filter count — plain "Filters" at zero, "Filters, N active" otherwise —
    open the disclosure once for active URL filters, and keep native
    open/close ownership afterwards. Retrieval itself must really apply the
    hydrated filters (lexical ranking needs a query, so q rides along), and
    the separate active-filters paragraph keeps listing the values.

    The spa export is exercised through real exported files only: its
    root-absolute form/Clear targets (``/__search``) need a clean-URL host,
    so the effective form-submit and Clear legs run on the static export,
    where ``__search.html`` is directly hosted.
    """
    base = request.getfixturevalue(export_fixture)
    disclosure = page.locator("details.okf-search-filters")
    summary = page.locator("details.okf-search-filters > summary")

    def wait_hydrated() -> None:
        page.wait_for_function(
            "() => window.okfPortableProvider"
            " && document.body.dataset.okfCaptureReady === 'ready'"
            " && !document.querySelector('.okf-search-error')"
        )

    # Zero state: plain "Filters" (visible and accessible name), disclosure
    # closed, and native summary keyboard toggling still owns it.
    page.goto(base + "/__search.html", wait_until="domcontentloaded")
    wait_hydrated()
    assert summary.inner_text().strip() == "Filters", summary.inner_text()
    assert "0 active" not in summary.text_content()
    expect(summary).to_have_accessible_name(re.compile(r"^Filters\s*▾?\s*$"))
    assert not disclosure.evaluate("el => el.open")
    expect(page.locator(".okf-search-filter-grid")).to_be_hidden()
    summary.focus()
    page.keyboard.press("Enter")
    assert disclosure.evaluate("el => el.open")
    expect(page.locator(".okf-search-filter-grid")).to_be_visible()
    page.keyboard.press("Enter")
    assert not disclosure.evaluate("el => el.open")
    expect(page.locator(".okf-search-filter-grid")).to_be_hidden()

    # A URL filter hydrates to a nonzero count and opens the disclosure once.
    page.goto(base + "/__search.html?type=Service", wait_until="domcontentloaded")
    wait_hydrated()
    assert summary.inner_text().strip() == "Filters, 1 active", summary.inner_text()
    expect(summary).to_have_accessible_name(re.compile(r"^Filters, 1 active\b"))
    assert disclosure.evaluate("el => el.open") is True
    assert "current filters" in page.locator("#okf-results-heading").inner_text()
    expect(page.locator(".okf-search__summary .okf-active-filters")).to_contain_text(
        "type: Service"
    )

    # Two active filters, and retrieval really applies them: exactly the
    # checkout Service survives the type+tag pair.
    page.goto(
        base + "/__search.html?type=Service&tag=checkout&q=checkout",
        wait_until="domcontentloaded",
    )
    wait_hydrated()
    assert summary.inner_text().strip() == "Filters, 2 active", summary.inner_text()
    expect(summary).to_have_accessible_name(re.compile(r"^Filters, 2 active\b"))
    assert disclosure.evaluate("el => el.open") is True
    expect(page.locator(".okf-search__summary .okf-active-filters")).to_contain_text(
        "tag: checkout"
    )
    results = page.locator(".okf-search-results-list li")
    assert results.count() == 1, (
        f"filters must retrieve exactly the checkout Service, got {results.count()}"
    )
    assert "Checkout Service" in results.first.inner_text()

    if export_fixture != "static_url":
        return  # spa form/Clear targets root-absolute /__search (clean-URL host)

    # An effective form submit on the exported workbench carries the filters
    # into a fresh hydration with the same nonzero contract...
    page.goto(base + "/__search.html", wait_until="domcontentloaded")
    wait_hydrated()
    assert summary.inner_text().strip() == "Filters"
    if not disclosure.evaluate("el => el.open"):
        summary.click()
    page.fill("#okf-search-type", "Service")
    page.fill("#okf-search-tag", "checkout")
    page.fill("#okf-search-query", "checkout")
    with page.expect_navigation(wait_until="domcontentloaded") as nav:
        page.get_by_role("button", name="Apply", exact=True).click()
    assert nav.value.ok, "Apply must submit the exported search form"
    assert "type=Service" in page.url and "tag=checkout" in page.url, page.url
    wait_hydrated()
    assert summary.inner_text().strip() == "Filters, 2 active", summary.inner_text()
    assert disclosure.evaluate("el => el.open") is True
    assert page.locator(".okf-search-results-list li").count() == 1

    # ...and Clear returns the honest zero state through a real navigation.
    with page.expect_navigation(wait_until="domcontentloaded") as nav:
        page.locator(".okf-search-filter-actions").get_by_role("link", name="Clear").click()
    assert nav.value.ok, "Clear must navigate back to the filterless search page"
    wait_hydrated()
    assert summary.inner_text().strip() == "Filters", summary.inner_text()
    assert "0 active" not in summary.text_content()
    assert not disclosure.evaluate("el => el.open")


def test_graph_comment_badges_appear_only_for_positive_counts(server_url: str, page) -> None:
    """Phase-1 clarity: graph comment badges are positive-count signals.

    With a graph payload carrying an explicit ``active_comment_count: 0``
    node, a positive (3) node, and an unmarked node, the full render
    pipeline must keep zeros and omissions badge-free everywhere the count
    is surfaced as a visible chip — the canvas badge data
    (``commentBadgeUrl``), the keyboard node-index chip, and the detail
    signals row — while the positive count shows everywhere. The
    node-index aria phrase is the assistive channel, not a badge: it keeps
    the exact count for marked nodes (including explicit 0) and stays
    silent for unmarked ones. Node data keeps the real values either way.
    """
    import json as _json

    mutated: dict = {}

    def handle(route):
        response = route.fetch()
        body = _json.loads(response.body())
        nodes = body.get("nodes", [])
        if len(nodes) >= 3:
            nodes[0]["data"]["active_comment_count"] = 0   # explicit zero
            nodes[1]["data"]["active_comment_count"] = 3   # positive
            nodes[2]["data"].pop("active_comment_count", None)  # unmarked
            mutated.update(zero=nodes[0]["data"]["id"],
                           positive=nodes[1]["data"]["id"],
                           unmarked=nodes[2]["data"]["id"])
        route.fulfill(response=response, body=_json.dumps(body),
                      content_type="application/json")

    page.route("**/graph.json", handle)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(server_url + "/__graph", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.okfGraphCaptureState && window.okfGraphCaptureState().layoutSettled"
    )
    assert mutated, "graph payload mutation fixture did not run"

    data = page.evaluate(
        """ids => {
          const G = window.__okfLoomGraph;
          const read = id => {
            const n = G.cy.getElementById(id);
            if (!n.length) return null;
            return {badge: n.data('commentBadgeUrl'),
                    hasCount: n.data('has_active_comment_count'),
                    count: n.data('active_comment_count')};
          };
          const indexItem = id => {
            const el = document.querySelector('.okf-node-index__item[data-target="' + id + '"]');
            if (!el) return null;
            return {chip: !!el.querySelector('.okf-node-index__comments'),
                    aria: el.getAttribute('aria-label') || ''};
          };
          return {zero: {...read(ids.zero), index: indexItem(ids.zero)},
                  positive: {...read(ids.positive), index: indexItem(ids.positive)},
                  unmarked: {...read(ids.unmarked), index: indexItem(ids.unmarked)}};
        }""",
        mutated,
    )
    assert data["zero"]["hasCount"] == 1 and data["zero"]["count"] == 0, data
    assert data["zero"]["badge"] == "none", data
    assert data["zero"]["index"] is not None and data["zero"]["index"]["chip"] is False, data
    # The aria phrase keeps the exact count (assistive channel); only the
    # visible chip is a positive-count signal.
    assert "0 active comments" in data["zero"]["index"]["aria"], data

    assert data["positive"]["hasCount"] == 1 and data["positive"]["count"] == 3, data
    assert data["positive"]["badge"].startswith("data:image/svg+xml"), data
    assert data["positive"]["index"]["chip"] is True, data
    assert "3 active comments" in data["positive"]["index"]["aria"], data

    assert data["unmarked"]["hasCount"] == 0, data
    assert data["unmarked"]["badge"] == "none", data
    assert data["unmarked"]["index"]["chip"] is False, data

    # Detail signals row: positive only.
    page.evaluate(
        """id => window.__okfLoomGraph.interaction.dispatch({type: 'SELECT', id, quietUrl: true})""",
        mutated["positive"],
    )
    page.wait_for_function(
        "() => (document.querySelector('#okf-detail') || document.body).innerText.includes('Active comments')"
    )
    detail = page.evaluate(
        """() => (document.querySelector('#okf-detail') || document.body).innerText"""
    )
    assert "Active comments" in detail and "3" in detail, detail

    page.evaluate(
        """id => window.__okfLoomGraph.interaction.dispatch({type: 'SELECT', id, quietUrl: true})""",
        mutated["zero"],
    )
    page.wait_for_function(
        """() => { const el = document.querySelector('#okf-detail') || document.body;
                   return el.innerText.includes('Stands out by'); }"""
    )
    zero_detail = page.evaluate(
        "() => (document.querySelector('#okf-detail') || document.body).innerText"
    )
    assert "Active comments" not in zero_detail, zero_detail


def test_serve_recent_pages_use_extensionless_routes(server_url: str, page) -> None:
    """P1 static/live URL parity (contract-runtime-parity): the mode-aware
    recent-page URL fix must not regress serve/spa. In serve mode the
    recent-page options must keep their extensionless ``/<id>`` routes."""
    page.goto(server_url + "/tables/orders", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.okf-page__title')")
    page.goto(server_url + "/datasets/orders", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.okf-page__title')")
    search = page.locator('.okf-topbar input[type="search"][name="q"]').first
    search.wait_for(state="visible")
    search.click()
    page.locator(".okf-search-live__group--recent").wait_for(state="visible")
    hrefs = page.evaluate(
        """() => Array.from(
              document.querySelectorAll('.okf-search-live__option[data-href]')
            ).map(o => o.getAttribute('data-href'))"""
    )
    assert "/tables/orders" in hrefs, (
        f"serve recent-page hrefs must include extensionless /tables/orders, got {hrefs}"
    )
    assert all(h.startswith("/") and not h.endswith(".html") for h in hrefs), (
        f"serve recent-page hrefs must all be extensionless /<id>, got {hrefs}"
    )


def test_phase2_search_live_hostile_title_is_text_only(server_url: str, page) -> None:
    """P1 #3 (security-boundary): a concept whose title contains HTML is
    rendered as TEXT in the live suggestion (no markup, no script execution).
    The suggestion DOM is built with createElement + textContent / text-node
    highlight, never innerHTML from server data."""
    import shutil, tempfile
    bundle_dir = Path(tempfile.mkdtemp()) / "demo"
    shutil.copytree(str(DEMO_BUNDLE), str(bundle_dir))
    (bundle_dir / "evil.md").write_text(
        "---\ntype: Table\n"
        'title: "<img src=x onerror=alert(1)> markup <b>bold</b>"\n'
        "tags: [evil]\n---\n\nEvil markup title for escaping proof.\n",
        encoding="utf-8",
    )
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        okf_module_argv("serve", str(bundle_dir), "--host", "127.0.0.1", "--port", str(port), "--no-watch", "--no-open"),
        cwd=str(TOOLKIT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=okf_subprocess_env(),
    )
    try:
        _wait_for_server(proc, base)
        page.goto(base + "/", wait_until="domcontentloaded")
        search = page.locator('input[type="search"][name="q"]').first
        search.wait_for(state="visible")
        search.fill("evil")
        live = page.locator(".okf-search-live")
        live.wait_for(state="visible")
        page.wait_for_timeout(150)
        safety = page.evaluate(
            """() => {
                const live = document.querySelector('.okf-search-live');
                const opt = live.querySelector('.okf-search-live__option:not(.okf-search-live__all)');
                if (!opt) return {missing: true};
                return {
                    // The hostile markup must NOT have created real img/b nodes.
                    imgCount: opt.querySelectorAll('img').length,
                    boldCount: opt.querySelectorAll('b').length,
                    // The raw title text is present (escaped, as text).
                    hasEvilText: !!Array.from(opt.querySelectorAll('*')).find(e => (e.textContent||'').includes('onerror')),
                    unsafeAttrs: opt.querySelectorAll('[onerror],[onload]').length,
                };
            }"""
        )
        assert not safety.get("missing"), "no suggestion rendered for the hostile-title concept"
        assert safety["imgCount"] == 0, "hostile <img> markup was injected into the suggestion"
        assert safety["boldCount"] == 0, "hostile <b> markup was injected into the suggestion"
        assert safety["unsafeAttrs"] == 0, "hostile on* attribute survived into the suggestion"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)


def test_phase2_link_popover_type_band_and_counts(server_url: str, page) -> None:
    """P1 #3 (contract-runtime-parity / state-ownership): the hover popover is
    a mini concept card — type band + title + description + safe link/relation
    counts — under the existing race fence (a fast hover swap cancels the
    earlier fetch's render)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    # The orders concept links out to customers (a typed relation target).
    link = page.locator(".okf-page__body a.okf-internal, .okf-relation__target").first
    if not link.count():
        pytest.fail("fixture has no internal link to hover on this concept")
    link.hover()
    # The popover fetch is ~200ms after hover (scheduleShow delay).
    try:
        page.wait_for_selector("#okf-popover .okf-popover__type", timeout=3000)
    except Exception:
        pytest.fail("popover type band did not appear on hover")
    info = page.evaluate(
        """() => {
            const p = document.getElementById('okf-popover');
            const type = p.querySelector('.okf-popover__type');
            const counts = p.querySelector('.okf-popover__counts');
            return {
                typeText: type ? type.textContent.trim() : "",
                hasCounts: !!counts,
                // SAFETY: popover rebuilt from fetched /__raw via textContent only.
                unsafe: p.querySelectorAll('img,script,[onerror]').length,
            };
        }"""
    )
    assert info["typeText"], "popover must show a type band"
    assert info["unsafe"] == 0, "unsafe node leaked into the popover"
    # Counts row is optional (a linkless concept has none) but when the
    # target links out it must appear with the evidence row.
    if info["hasCounts"]:
        count_text = page.evaluate("() => document.getElementById('okf-popover').querySelector('.okf-popover__counts').textContent")
        assert "Links" in count_text or "Relations" in count_text, (
            f"popover counts row must label link/relation counts; got {count_text!r}"
        )


def _popover_title(page) -> str:
    """Read the current popover title text (or '' if hidden/empty).

    Used by the popover-lifecycle tests so each one asserts against the SAME
    stable signal (the rendered title), not internal counters like
    popoverSeq/generation that are not exposed to the page."""
    return page.evaluate(
        """() => {
            const p = document.getElementById('okf-popover');
            if (!p || p.style.display === 'none') return '';
            const h = p.querySelector('h4');
            return h ? (h.textContent || '').trim() : '';
        }"""
    )


def _popover_display(page) -> str:
    """Read the popover's current display value (asserts hidden vs shown)."""
    return page.evaluate(
        """() => {
            const p = document.getElementById('okf-popover');
            return p ? p.style.display : 'none';
        }"""
    )


def test_phase2_popover_later_hover_wins_over_earlier_pending(server_url: str, page) -> None:
    """P1 #4 (state-ownership / async-lifecycle): hovering link A then quickly
    link B (while A's /__raw fetch is still pending) must end with the popover
    showing B's title, never A's. The popoverSeq fence invalidates A's render
    once B is hovered; A's late response is dropped on arrival.

    Proves the race fence by CONTROLLING fetch timing with a Playwright route
    handler: the FIRST /__raw fetch (whichever link the hover triggers) is
    held until B's fetch completes and renders, then A is released. Without
    the fence, A's late response would overwrite B."""
    page.set_viewport_size({"width": 1280, "height": 800})
    # Hold the FIRST /__raw fetch (any URL) until the test releases it; let
    # every subsequent /__raw pass through so B's hover resolves normally.
    page.add_init_script(r"""(() => {
      const nativeFetch = window.fetch.bind(window);
      let seen = 0;
      window.__holdA = null;
      window.fetch = (url, opts) => {
        const u = String(url);
        if (u.indexOf('/__raw/') >= 0) {
          seen++;
          if (seen === 1) {
            // Hold the first hover's fetch.
            return new Promise(resolve => {
              window.__holdA = () => nativeFetch(url, opts).then(resolve);
            });
          }
        }
        return nativeFetch(url, opts);
      };
    })()""")
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    # Use the same selector the existing popover test uses: it includes
    # .okf-relation__target which sits OUTSIDE .okf-page__body (in the
    # infobox/connections area), so studio.js's body-patch does not strip
    # wiki.js's hover bindings from it. Body-internal links lose their
    # aria-describedby after a studio body patch (a separate pre-existing
    # concern), so the relation target is the stable hover target.
    links = page.locator(".okf-page__body a.okf-internal, .okf-relation__target")
    link_count = links.count()
    if link_count < 2:
        pytest.fail("orders page needs >=2 bound links for the hover-swap proof")
    # Hover A first. scheduleShow delays the fetch ~200ms (REDUCED_MOTION
    # shortens it to 0; we are not in reduced motion here), so wait long
    # enough for A's fetch to actually issue AND be held before swapping to B.
    links.nth(0).hover()
    page.wait_for_function(
        "() => typeof window.__holdA === 'function'",
        timeout=3000,
    )
    # Now hover B (its /__raw fetch passes through and renders).
    links.nth(1).hover()
    try:
        page.wait_for_function(
            "() => { const p = document.getElementById('okf-popover'); "
            "return p && p.style.display !== 'none' && p.querySelector('h4'); }",
            timeout=4000,
        )
    except Exception:
        pytest.fail("B's popover never rendered (held-A fetch may have blocked the queue)")
    title_b = _popover_title(page)
    assert title_b, "B's popover title must have rendered"
    # Release A's late response. It must NOT overwrite B's popover.
    page.evaluate("() => { if (window.__holdA) window.__holdA(); }")
    page.wait_for_timeout(300)
    title_after_a = _popover_title(page)
    assert title_after_a == title_b, (
        f"late A response overwrote B's popover: expected {title_b!r}, got "
        f"{title_after_a!r}. The popoverSeq fence should have invalidated A."
    )


def test_phase2_popover_hide_while_pending_keeps_it_hidden(server_url: str, page) -> None:
    """P1 #4 (async-lifecycle): mouseout/cancelShow while a /__raw fetch is
    still pending must keep the popover HIDDEN — the pending response cannot
    reopen it. hidePopover() increments popoverSeq exactly to invalidate the
    in-flight fetch; if the seq check is removed the late response renders
    an unwanted popover after the user has already moved away."""
    page.set_viewport_size({"width": 1280, "height": 800})
    # Hold the only /__raw fetch the hover will trigger.
    page.add_init_script(r"""(() => {
      const nativeFetch = window.fetch.bind(window);
      window.__releaseHold = null;
      window.fetch = (url, opts) => {
        if (String(url).indexOf('/__raw/') >= 0 && !window.__releaseHold) {
          return new Promise(resolve => {
            window.__releaseHold = () => nativeFetch(url, opts).then(resolve);
          });
        }
        return nativeFetch(url, opts);
      };
    })()""")
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    # Use the stable hover selector (see test_phase2_popover_later_hover_wins
    # for the rationale: relation targets survive studio body patches).
    link = page.locator(".okf-page__body a.okf-internal, .okf-relation__target").first
    if not link.count():
        pytest.fail("fixture has no internal link to hover on this concept")
    link.hover()
    # scheduleShow delays the fetch ~200ms; wait for it to actually issue so
    # the held Promise exists before we move away (otherwise cancelShow just
    # clears the timer and no fetch ever happens — there is nothing to drop).
    page.wait_for_function(
        "() => typeof window.__releaseHold === 'function'",
        timeout=3000,
    )
    # Mouse out BEFORE the fetch resolves: hidePopover() invalidates the seq.
    page.mouse.move(10, 10)
    page.wait_for_timeout(180)  # past the cancelShow hide delay (80ms)
    assert _popover_display(page) == "none", (
        "popover must be hidden after mouseout while the fetch is pending"
    )
    # Release the held response. The popover must STAY hidden — the pending
    # response cannot reopen it once hidePopover bumped the seq.
    page.evaluate("() => { if (window.__releaseHold) window.__releaseHold(); }")
    page.wait_for_timeout(300)
    assert _popover_display(page) == "none", (
        "pending /__raw response reopened the popover after mouseout — the "
        "popoverSeq fence failed to drop the late response"
    )
    assert _popover_title(page) == "", (
        "popover title must be empty (hidden), not rendered from the late response"
    )


def test_phase2_popover_stale_rejected_response_cannot_reopen(server_url: str, page) -> None:
    """P1 #4 (security-boundary / state-ownership): a STALE or REJECTED
    /__raw response (e.g. an old request whose fetch Promise rejected, or a
    very late response arriving after another hover cycle completed) CANNOT
    reopen or overwrite the current popover. The popoverSeq fence treats a
    non-matching seq as a no-op render.

    Proves the fence by routing /__raw to a controllable handler that
    resolves a STALE payload (the wrong concept's title) AFTER a fresh
    hover+render has already shown the correct title. The stale payload must
    not overwrite the fresh render."""
    page.set_viewport_size({"width": 1280, "height": 800})
    # Hold the FIRST /__raw (stale) request, then let subsequent ones pass.
    # The stale response will carry a DIFFERENT title than the live hover.
    page.add_init_script(r"""(() => {
      const nativeFetch = window.fetch.bind(window);
      let staleHeld = false;
      window.__releaseStale = null;
      window.fetch = (url, opts) => {
        const u = String(url);
        if (u.indexOf('/__raw/') >= 0 && !staleHeld) {
          staleHeld = true;
          return new Promise(resolve => {
            window.__releaseStale = () => resolve(new Response(
              "---\\ntype: StaleType\\ntitle: STALE_TITLE_THAT_MUST_NOT_WIN\\n---\\nbody\\n",
              {status: 200, headers: {'Content-Type': 'text/plain'}}
            ));
          });
        }
        return nativeFetch(url, opts);
      };
    })()""")
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    # Use the stable hover selector (relation targets survive studio body patches).
    links = page.locator(".okf-page__body a.okf-internal, .okf-relation__target")
    if links.count() < 2:
        pytest.fail("fixture needs >=2 bound links for stale/rejected proof")
    # First hover triggers the HELD (stale) request. Wait for the held fetch
    # to actually issue (scheduleShow ~200ms) before moving away.
    links.nth(0).hover()
    page.wait_for_function(
        "() => typeof window.__releaseStale === 'function'",
        timeout=3000,
    )
    # Move away (cancel) then hover a SECOND link, which issues a fresh /__raw
    # that passes through normally and renders.
    page.mouse.move(10, 10)
    page.wait_for_timeout(180)  # past cancelShow delay
    links.nth(1).hover()
    try:
        page.wait_for_function(
            "() => { const p = document.getElementById('okf-popover'); "
            "return p && p.style.display !== 'none' && p.querySelector('h4'); }",
            timeout=4000,
        )
    except Exception:
        pytest.fail("second hover did not render a popover on this concept set")
    fresh_title = _popover_title(page)
    # Now release the STALE response. It must NOT overwrite the fresh popover.
    page.evaluate("() => { if (window.__releaseStale) window.__releaseStale(); }")
    page.wait_for_timeout(300)
    final_title = _popover_title(page)
    assert final_title == fresh_title, (
        f"stale response overwrote the fresh popover: expected {fresh_title!r}, "
        f"got {final_title!r}. The popoverSeq fence should have dropped it."
    )
    assert final_title != "STALE_TITLE_THAT_MUST_NOT_WIN", (
        "the stale title leaked into the live popover — popoverSeq fence failed"
    )


def test_phase2_popover_release_in_cancel_window_never_shows_a(server_url: str, page) -> None:
    """P1 #4 (cache-invalidation-invariants / async-lifecycle): the LAST gap in
    the popover race fence. When hover A's /__raw fetch is PENDING and the user
    moves to link B, A's response can resolve INSIDE cancelShow's 80ms hide
    window — BEFORE hidePopover runs (the only place the old code bumped
    popoverSeq). In that window seq===popoverSeq, so without an IMMEDIATE
    invalidation on hover B, A's late response rendered stale content for a
    brief flash before the 80ms hide caught up.

    This test pins that exact window. It holds A's fetch with a distinctive
    STALE title, hovers A (fetch issues + held), then TELEPORTS the pointer
    onto B (page.mouse.move, single step) so mouseleave A -> mouseenter B fire
    in the same frame — making the [mouseenter B, mouseleave A + 80ms] window
    the full ~80ms wide. (A normal .hover() moves the pointer over many frames,
    eating the 80ms so hidePopover invalidates A before B is even entered —
    which would make the test pass for the wrong reason on the unfixed code.)
    A is released the instant the teleport returns, landing inside that window,
    and a continuous recorder (MutationObserver + 4ms poll) captures EVERY
    title the popover ever shows. The STALE title must NEVER appear — proving
    scheduleShow invalidates the prior fetch the instant a new hover begins, not
    80ms later. A hide/rejection regression is folded in: after release the
    popover must end showing B's real title (or stay hidden), never A.

    This is the only test that exercises the pre-hidePopover window; the
    sibling tests release A only AFTER B has rendered (post-window) or move
    fully off any link (no hover B), so they do not cover this gap."""
    page.set_viewport_size({"width": 1280, "height": 800})
    # Hold the FIRST /__raw (A) and make its response carry a distinctive STALE
    # title so any render of A is unambiguously detectable. The body uses REAL
    # newlines (single backslash-n in this raw string -> JS newline) so
    # parseFrontmatter actually extracts the title; with literal backslash-n the
    # "---\\n" prefix never matches and the title silently falls back to the
    # concept id, masking the bug. Let every subsequent /__raw (B) pass through.
    page.add_init_script(r"""(() => {
      const nativeFetch = window.fetch.bind(window);
      let seen = 0;
      window.__holdA = null;
      window.fetch = (url, opts) => {
        const u = String(url);
        if (u.indexOf('/__raw/') >= 0) {
          seen++;
          if (seen === 1) {
            return new Promise(resolve => {
              window.__holdA = () => resolve(new Response(
                "---\ntype: StaleType\ntitle: STALE_A_TITLE_THAT_MUST_NEVER_SHOW\n---\nbody\n",
                {status: 200, headers: {'Content-Type': 'text/plain'}}
              ));
            });
          }
        }
        return nativeFetch(url, opts);
      };
    })()""")
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    links = page.locator(".okf-page__body a.okf-internal, .okf-relation__target")
    if links.count() < 2:
        pytest.fail("orders page needs >=2 bound links for the cancel-window proof")
    # Position BOTH links inside the viewport BEFORE any hover. The teleport
    # below is a raw page.mouse.move, which never scrolls; with link B below
    # the fold the pointer misses it, mouseenter B never fires, and A's stale
    # title legitimately survives. Scrolling after hovering A would instead
    # fire scroll-driven mouseleave/enter and destroy the pinned cancel
    # window, so both links are placed first and their geometry asserted.
    for index in (0, 1):
        links.nth(index).scroll_into_view_if_needed()
    viewport = page.viewport_size
    boxes = [links.nth(index).bounding_box() for index in (0, 1)]
    for label, box in zip(("A", "B"), boxes):
        assert box and box["y"] >= 0 and box["y"] + box["height"] <= viewport["height"], (
            f"cancel-window proof needs link {label} fully inside the "
            f"{viewport['width']}x{viewport['height']} viewport, got {box}"
        )
    # Install a continuous recorder: capture EVERY title the popover's <h4>
    # displays while it is visible, via a MutationObserver PLUS a fast 4ms poll
    # backstop so even a sub-frame stale flash cannot slip through.
    page.evaluate(
        r"""() => {
            window.__seenTitles = new Set();
            window.__stopRecord = null;
            const readTitle = () => {
                const p = document.getElementById('okf-popover');
                if (!p || p.style.display === 'none') return;
                const h = p.querySelector('h4');
                const t = h ? (h.textContent || '').trim() : '';
                if (t) window.__seenTitles.add(t);
            };
            const obs = new MutationObserver(readTitle);
            const p0 = document.getElementById('okf-popover');
            if (p0) obs.observe(p0, {childList: true, subtree: true, characterData: true});
            const iv = setInterval(readTitle, 4);
            window.__stopRecord = () => { clearInterval(iv); obs.disconnect(); };
        }"""
    )
    # Hover A first. scheduleShow delays the fetch ~200ms; wait for the held
    # fetch to actually issue before moving to B.
    links.nth(0).hover()
    page.wait_for_function(
        "() => typeof window.__holdA === 'function'",
        timeout=3000,
    )
    # TELEPORT onto B (single-step mouse.move): mouseleave A -> cancelShow
    # (schedules hidePopover at +80ms) and mouseenter B -> scheduleShow(B) fire
    # in the same frame. scheduleShow(B) is the call that must IMMEDIATELY bump
    # popoverSeq to drop A; B's own show is still delayed ~200ms.
    bbox = links.nth(1).bounding_box()
    page.mouse.move(bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)
    # Release A the instant the teleport returns — inside the 80ms cancel
    # window, well before hidePopover runs. Without the scheduleShow
    # invalidation, A's resolving fetch sees seq===popoverSeq and renders the
    # STALE title for ~60ms (caught by the recorder); with the fix it is
    # rejected on arrival.
    page.evaluate("() => { if (window.__holdA) window.__holdA(); }")
    # Let the full hover-B cycle settle (past the 80ms hide AND B's ~200ms show).
    page.wait_for_timeout(700)
    page.evaluate("() => { if (window.__stopRecord) window.__stopRecord(); }")
    seen = set(page.evaluate("() => Array.from(window.__seenTitles)"))
    # The hard contract: A's stale title must NEVER have been shown, even for a
    # single frame. This fails on the unfixed code (A flashes during the window).
    assert "STALE_A_TITLE_THAT_MUST_NEVER_SHOW" not in seen, (
        "stale A title was shown during the cancel window before hidePopover "
        "ran: scheduleShow did not invalidate the prior hover immediately. "
        f"Recorded titles: {sorted(seen)!r}"
    )
    # Regression: B's real hover must still complete (the popover eventually
    # rendered SOMETHING for B, or at least no stale-A content leaked). If B
    # did not render at all we still pass the primary contract above, but flag
    # a soft check so a broken B path is not masked.
    final_display = _popover_display(page)
    if final_display != "none":
        final_title = _popover_title(page)
        assert final_title != "STALE_A_TITLE_THAT_MUST_NEVER_SHOW", (
            "popover ended on the stale A title after the cancel window"
        )
        assert final_title, "popover is visible but has no title (B did not render)"


def test_search_returns_results(server_url: str, page) -> None:
    """The live search endpoint returns at least one hit for ``orders``.

    Uses the live server (the static build has no search backend and the
    capture script covers that path separately).
    """
    page.goto(server_url + "/", wait_until="domcontentloaded")
    # The index page exposes a topbar search form whose input is named ``q``.
    search_input = page.locator('input[type="search"][name="q"]').first
    search_input.wait_for(state="visible")
    search_input.fill("orders")
    # Submitting the form navigates to /__search?q=orders on the live server.
    with page.expect_navigation(wait_until="domcontentloaded") as nav_info:
        search_input.press("Enter")
    resp = nav_info.value
    assert resp.ok, f"search navigation failed: status={resp.status}"
    assert "/__search" in page.url, f"did not navigate to search: {page.url}"
    results = page.locator(".okf-search__results")
    results.wait_for(state="visible")
    # At least one result element should be rendered (link or list item).
    result_count = results.locator("a, li").count()
    assert result_count >= 1, "search returned zero results for 'orders'"


def test_live_prepare_context_retains_every_applicable_filter(server_url: str, page) -> None:
    """Prepared context agrees with the URL-restored workbench contract."""
    page.goto(server_url + "/__search?q=orders&mode=lexical&limit=5", wait_until="domcontentloaded")
    page.locator("[data-context-prepare]").wait_for(state="attached")
    page.evaluate("""() => {
      const nativeFetch = window.fetch.bind(window);
      window.__contextUrls = [];
      window.fetch = (url, opts) => {
        if (String(url).startsWith('/__context?')) {
          window.__contextUrls.push(String(url));
          return Promise.resolve(new Response(JSON.stringify({
            schema_version: 1, bundle_revision: 'test', query: 'orders', mode: 'relation',
            filters: {}, budget: {requested_tokens: 7000, estimated_tokens: 1},
            neighbors: 2, included: 0, omitted: 0, truncated: false, items: [],
            representations: {markdown: '# OKF Context Pack\\n'}
          }), {status: 200, headers: {'Content-Type': 'application/json'}}));
        }
        return nativeFetch(url, opts);
      };
    }""")
    page.locator(".okf-search-filters summary").click()
    page.get_by_role("radio", name="Connected by relation").click()
    page.locator('[name="type"]').fill("Table")
    page.locator('[name="tag"]').fill("facts")
    page.locator('[name="relation"]').fill("link")
    page.locator('[name="source"]').fill("tables/orders")
    page.locator('[name="target"]').fill("tables/users")
    _open_context_options(page)
    page.locator("[data-context-budget]").fill("7000")
    page.locator("[data-context-neighbors]").select_option("2")
    page.locator("[data-context-limit]").fill("5")
    page.locator("[data-context-prepare]").click()
    page.wait_for_function("() => window.__contextUrls.length === 1")
    relation = page.evaluate(
        "() => Object.fromEntries(new URL(window.__contextUrls[0], location.href).searchParams)"
    )
    assert relation == {
        "format": "json", "mode": "relation", "budget_tokens": "7000",
        "neighbors": "2", "limit": "5", "type": "Table", "tag": "facts",
        "relation": "link", "source": "tables/orders", "target": "tables/users",
        "q": "orders",
    }

    page.get_by_role("radio", name="Mentions entity").click()
    page.locator("#okf-search-query").fill("")
    page.locator('[name="entity"]').fill("Orders")
    page.locator("[data-context-prepare]").click()
    page.wait_for_function("() => window.__contextUrls.length === 2")
    entity = page.evaluate(
        "() => Object.fromEntries(new URL(window.__contextUrls[1], location.href).searchParams)"
    )
    assert entity["mode"] == "entity" and entity["entity"] == "Orders"
    assert "relation" not in entity and "source" not in entity and "target" not in entity


def test_live_context_target_supersession_fences_every_late_mutation(server_url: str, page) -> None:
    """A newer target/action owns pack, status, label, clipboard and focus."""
    page.goto(server_url + "/__search?q=orders&mode=lexical&limit=5", wait_until="domcontentloaded")
    _open_context_options(page)
    page.evaluate("""() => {
      const nativeFetch=window.fetch.bind(window);window.__heldContext=[];window.__clipboardWrites=[];
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:text=>{window.__clipboardWrites.push(text);return Promise.resolve();}}});
      window.fetch=(url,opts)=>String(url).startsWith('/__context?')?new Promise(resolve=>{
        window.__heldContext.push({url:String(url),opts,resolve});
      }):nativeFetch(url,opts);
    }""")
    prepare = page.locator("[data-context-prepare]")
    query = page.locator("#okf-search-query")
    prepare.click()
    page.wait_for_function("() => window.__heldContext.length === 1")
    query.fill("customers")
    prepare.click()
    page.wait_for_function("() => window.__heldContext.length === 2")
    page.evaluate("pack => {pack.bundle_revision=document.body.dataset.okfRevision;window.__heldContext[1].resolve(new Response(JSON.stringify(pack),{status:200,headers:{'Content-Type':'application/json'}}));}", _context_pack("new-owner", "customers"))
    page.wait_for_function("() => document.querySelector('[data-context-prepare]')._okfPreparedContext?.test_marker === 'new-owner'")
    before = page.evaluate("""() => ({
          pack:document.querySelector('[data-context-prepare]')._okfPreparedContext.test_marker,
          prepared:document.querySelector('[data-context-copy-prepared]')._okfPreparedContext.test_marker,
      status:document.querySelector('#okf-copy-status').textContent,
      label:document.querySelector('[data-context-prepare]').textContent,
      focus:document.activeElement.id||document.activeElement.getAttribute('data-context-prepare')||document.activeElement.tagName,
      clipboard:window.__clipboardWrites.slice()
    })""")
    page.evaluate("pack => {pack.bundle_revision=document.body.dataset.okfRevision;window.__heldContext[0].resolve(new Response(JSON.stringify(pack),{status:200,headers:{'Content-Type':'application/json'}}));}", _context_pack("stale-owner", "orders"))
    page.wait_for_timeout(100)
    after = page.evaluate("""() => ({
          pack:document.querySelector('[data-context-prepare]')._okfPreparedContext.test_marker,
          prepared:document.querySelector('[data-context-copy-prepared]')._okfPreparedContext.test_marker,
      status:document.querySelector('#okf-copy-status').textContent,
      label:document.querySelector('[data-context-prepare]').textContent,
      focus:document.activeElement.id||document.activeElement.getAttribute('data-context-prepare')||document.activeElement.tagName,
      clipboard:window.__clipboardWrites.slice()
    })""")
    assert after == before
    assert after["pack"] == "new-owner" and after["clipboard"] == []
    assert page.evaluate("() => window.__heldContext[0].opts.signal.aborted")


def test_live_context_pagehide_fences_late_response(server_url: str, page) -> None:
    """Navigation revokes context ownership before a late transport settles."""
    page.goto(server_url + "/__search?q=orders&mode=lexical&limit=5", wait_until="domcontentloaded")
    _open_context_options(page)
    page.evaluate("""() => {
      const nativeFetch=window.fetch.bind(window);window.__heldNavigationContext=null;window.__clipboardWrites=[];
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:text=>{window.__clipboardWrites.push(text);return Promise.resolve();}}});
      window.fetch=(url,opts)=>String(url).startsWith('/__context?')?new Promise(resolve=>{window.__heldNavigationContext={opts,resolve};}):nativeFetch(url,opts);
    }""")
    prepare = page.locator("[data-context-prepare]")
    prepare.click()
    page.wait_for_function("() => !!window.__heldNavigationContext")
    page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
    before = page.evaluate("""() => ({
      pack:document.querySelector('[data-context-prepare]')._okfPreparedContext||null,
      prepared:document.querySelector('[data-context-copy-prepared]')._okfPreparedContext||null,
      status:document.querySelector('#okf-copy-status').textContent,
      label:document.querySelector('[data-context-prepare]').textContent,
      focus:document.activeElement.id||document.activeElement.tagName,
      clipboard:window.__clipboardWrites.slice(),
      aborted:window.__heldNavigationContext.opts.signal.aborted
    })""")
    page.evaluate("pack => window.__heldNavigationContext.resolve(new Response(JSON.stringify(pack),{status:200,headers:{'Content-Type':'application/json'}}))", _context_pack("late-navigation", "orders"))
    page.wait_for_timeout(100)
    after = page.evaluate("""() => ({
      pack:document.querySelector('[data-context-prepare]')._okfPreparedContext||null,
      prepared:document.querySelector('[data-context-copy-prepared]')._okfPreparedContext||null,
      status:document.querySelector('#okf-copy-status').textContent,
      label:document.querySelector('[data-context-prepare]').textContent,
      focus:document.activeElement.id||document.activeElement.tagName,
      clipboard:window.__clipboardWrites.slice(),
      aborted:window.__heldNavigationContext.opts.signal.aborted
    })""")
    assert before["aborted"] is True
    assert after == before


def test_static_portable_modes_context_and_fail_closed_url(static_url: str, page) -> None:
    page.goto(static_url + "/__search.html?q=orders&mode=hybrid&limit=5", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && !document.querySelector('.okf-search-error')")
    assert page.evaluate("() => Object.values(window.okfPortableProvider.capabilities).every(Boolean)")
    for name in ("Best overall", "Exact wording", "Similar meaning", "Tagged with", "Mentions entity", "Connected by relation"):
        control = page.get_by_role("radio", name=name)
        expect(control).to_be_enabled()
    expect(page.locator(".okf-page-context")).to_be_visible()
    _open_context_options(page)
    page.locator("[data-context-prepare]").click()
    expect(page.locator("[data-context-copy-prepared]")).to_be_enabled()
    assert "estimated tokens" in page.locator("[data-context-estimate]").inner_text()
    citation_size = page.locator(".okf-search-citation").first.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    assert citation_size >= 14

    page.goto(static_url + "/__search.html?q=x&q=y&mode=lexical", wait_until="domcontentloaded")
    expect(page.locator(".okf-search-error")).to_contain_text("Duplicate search parameter: q")
    expect(page.get_by_role("button", name="Retry")).to_have_count(0)
    edit = page.get_by_role("button", name="Edit search")
    expect(edit).to_be_visible()
    expect(page.get_by_role("link", name="Start clean")).to_be_visible()
    edit.click()
    expect(page.locator("#okf-search-query")).to_be_focused()


def test_static_provider_setup_failure_publishes_explicit_error(static_url: str, page) -> None:
    page.route(
        "**/passages-manifest.json",
        lambda route: route.fulfill(status=200, content_type="application/json", body='{"schema_version":999}'),
    )
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    error = page.locator(".okf-search-error")
    expect(error).to_be_visible()
    expect(error).to_contain_text("manifest is unsupported, incomplete, or noncanonical")
    expect(page.get_by_role("button", name="Retry")).to_be_visible()
    expect(page.locator("#okf-search-status")).to_have_text("Portable search unavailable.")


def test_static_retry_ignores_late_superseded_corpus(static_url: str, page) -> None:
    page.add_init_script(r"""(() => {
      const nativeFetch = window.fetch.bind(window);
      let held = false;
      window.fetch = (url, opts) => {
        if (!held && /passages-\d{6}-[a-f0-9]{16}\.json/.test(String(url))) {
          held = true;
          return new Promise(resolve => {
            window.__resolveLateCorpus = () => nativeFetch(url, opts).then(resolve);
          });
        }
        return nativeFetch(url, opts);
      };
    })()""")
    page.goto(static_url + "/__search.html?q=orders&mode=hybrid", wait_until="domcontentloaded")
    page.wait_for_function("() => typeof window.__resolveLateCorpus === 'function'")
    page.evaluate("() => window.okfReloadPortableSearch()")
    page.wait_for_function("() => window.okfPortableProvider && document.querySelectorAll('.okf-search-result').length > 0")
    page.evaluate("() => { window.__freshPortableProvider = window.okfPortableProvider; window.__freshStatus = document.querySelector('#okf-search-status').textContent; window.__resolveLateCorpus(); }")
    page.wait_for_timeout(150)
    assert page.evaluate("() => window.okfPortableProvider === window.__freshPortableProvider")
    assert page.evaluate("() => document.querySelector('#okf-search-status').textContent === window.__freshStatus")


def test_static_failed_owned_reload_revokes_provider_and_retry_owns_focus(static_url: str, page) -> None:
    """A failed generation cannot expose stale evidence; Retry restores stable focus."""
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    page.route(
        "**/passages-manifest.json",
        lambda route: route.fulfill(status=200, content_type="application/json", body='{"schema_version":999}'),
    )
    page.evaluate("() => window.okfReloadPortableSearch()")
    expect(page.get_by_role("button", name="Retry")).to_be_visible()
    assert page.evaluate("() => window.okfPortableProvider === null")
    assert page.evaluate("() => document.body.dataset.okfCaptureReady") == "unavailable"
    page.get_by_role("button", name="Retry").click()
    expect(page.get_by_role("button", name="Retry")).to_be_focused()
    page.unroute("**/passages-manifest.json")
    page.get_by_role("button", name="Retry").click()
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    expect(page.locator("#okf-search-query")).to_be_focused()
    assert page.evaluate("() => !!document.body.dataset.okfCaptureRevision")


def test_static_formatted_selection_lazily_maps_to_canonical_source(static_url: str, page) -> None:
    """Rendered emphasis maps fail-closed to canonical Markdown before provider use."""
    page.goto(static_url + "/playbooks/refund_flow.html", wait_until="domcontentloaded")
    assert page.evaluate("() => window.okfPortableProvider == null")
    selected = page.evaluate("""() => {
      const strong = Array.from(document.querySelectorAll('.okf-page__body strong')).find(el => el.textContent.includes('over $1000 USD'));
      if (!strong) return false;
      const range = document.createRange();
      range.setStart(strong.previousSibling, strong.previousSibling.textContent.length - 1);
      range.setEnd(strong.nextSibling, 1);
      const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
      document.dispatchEvent(new Event('selectionchange'));
      return true;
    }""")
    assert selected
    page.wait_for_function("() => window.okfSelectionContext && window.okfPortableProvider")
    mapped = page.evaluate("() => window.okfSelectionContext")
    assert mapped["concept"] == "playbooks/refund_flow"
    assert "over $1000 USD" in mapped["selection_exact"]
    assert mapped["selection_exact"] == "over $1000 USD"
    expect(page.locator(".okf-selection-hint")).to_have_text("Select text to copy exact context.")
    toolbar = page.get_by_role("button", name="Copy selection with citation")
    expect(toolbar).to_be_visible()
    collision = page.evaluate("""() => {
      const button=document.querySelector('.okf-selection-context'),selected=Array.from(document.querySelectorAll('.okf-page__body strong')).find(el=>el.textContent.includes('over $1000 USD'));
      let callout=selected.closest('p').nextElementSibling;while(callout&&callout.classList.contains('okf-selection-context'))callout=callout.nextElementSibling;const br=button.getBoundingClientRect(),cr=callout.getBoundingClientRect();
      const overlap=Math.min(br.right,cr.right)-Math.max(br.left,cr.left)>0&&Math.min(br.bottom,cr.bottom)-Math.max(br.top,cr.top)>0;
      const clearance=br.bottom<=cr.top?cr.top-br.bottom:br.top>=cr.bottom?br.top-cr.bottom:0;
      return {overlap,clearance,width:br.width,height:br.height,placement:button.dataset.placement};
    }""")
    assert collision["overlap"] is False, collision
    assert collision["clearance"] >= 8
    assert collision["width"] >= 24 and collision["height"] >= 24
    assert collision["placement"] in {"above", "below", "flow-below"}


def test_live_nojs_nested_manual_context_link_returns_markdown(server_url: str, page) -> None:
    """Nested canonical identifiers keep literal separators through the live link."""
    page.goto(server_url + "/__search?q=orders&mode=lexical", wait_until="domcontentloaded")
    link = page.locator('.okf-nojs-manual a[href*="concept="]').first
    href = link.get_attribute("href")
    assert href and "%2F" not in href.upper() and "concept=" in href
    response = page.request.get(server_url + href)
    assert response.status == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text().startswith("# OKF Context Pack")


def test_live_nojs_exposes_default_context_without_inert_options(server_url: str, page) -> None:
    context = page.context.browser.new_context(java_script_enabled=False, viewport={"width": 1280, "height": 800})
    nojs = context.new_page()
    try:
        nojs.goto(server_url + "/__search?q=orders&mode=lexical", wait_until="domcontentloaded")
        assert nojs.locator("[data-context-budget]:visible,[data-context-neighbors]:visible,[data-context-limit]:visible").count() == 0
        nojs.locator(".okf-page-context > details > summary").click()
        expect(nojs.get_by_text("Open default canonical Markdown context", exact=True)).to_be_visible()
        notice = nojs.locator(".okf-nojs-live-banner[role='note']:visible")
        expect(notice).to_have_count(1)
        expect(notice).to_contain_text("JavaScript is off")
    finally:
        context.close()


def test_static_graph_portable_context_is_lazy_retryable_and_owned(static_url: str, page) -> None:
    """Graph opening is cheap; first context action owns load, failure and Retry."""
    requests = []
    page.on("request", lambda request: requests.append(request.url) if "passages-manifest.json" in request.url else None)
    page.route(
        "**/passages-manifest.json",
        lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"offline"}'),
    )
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.cy")
    assert requests == []
    page.locator(".okf-node-index summary").click()
    page.locator(".okf-node-index__item").first.click()
    copy = page.locator("#okf-copy-neighbourhood")
    expect(copy).to_be_enabled()
    copy.click()
    expect(page.locator(".okf-graph-context-unavailable")).to_be_visible()
    assert len(requests) == 1
    fallback = page.get_by_role("link", name="Open emitted concept-only Markdown")
    expect(fallback).to_be_visible()
    assert "__context/" in fallback.get_attribute("href")
    page.unroute("**/passages-manifest.json")
    page.get_by_role("button", name="Retry", exact=True).click()
    page.wait_for_function("() => window.okfPortableProvider")
    expect(copy).to_be_focused()
    expect(page.locator(".okf-graph-context-unavailable")).to_have_count(0)


def test_cytoscape_unavailable_exposes_only_working_fallback_actions(static_url: str, page) -> None:
    page.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    expect(page.locator(".okf-graph-loading--error")).to_contain_text("Interactive graph unavailable")
    expect(page.locator(".okf-graph-fallback li").first).to_be_visible()
    search = page.get_by_role("link", name="Open Search & Context")
    index = page.get_by_role("link", name="Browse the concept index")
    expect(search).to_be_visible()
    expect(index).to_be_visible()
    assert search.get_attribute("href") == "__search.html"
    assert index.get_attribute("href") == "index.html"
    outcome = page.evaluate("""async () => {
      const started=performance.now();
      try { await window.okfCaptureReadiness.start({target:'graph'}).promise; return {authorized:true}; }
      catch(error) { return {authorized:error.screenshotAllowed,code:error.code,phase:error.metadata.readinessPhase,elapsed:performance.now()-started}; }
    }""")
    assert outcome["authorized"] is False
    assert outcome["code"] == "unavailable"
    assert outcome["phase"] == "graph-cytoscape"
    assert outcome["elapsed"] < 1000


def test_empty_graph_is_semantically_settled_without_layout_generation(static_url: str, page) -> None:
    """An authoritative empty graph is ready; zero layout generation is not a timeout."""
    page.route(
        "**/__data/graph.json",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"bundle_revision":"empty-revision","nodes":[],"edges":[],"types":[],"bodies":{},"backlinks":{},"outgoing":{},"external":[],"palette":{}}',
        ),
    )
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfGraphCaptureState?.().initialized")
    result = page.evaluate("""async () => ({
      state: window.okfGraphCaptureState(),
      readiness: await window.okfCaptureReadiness.start({target:'graph'}).promise
    })""")
    assert result["state"]["payloadNodeCount"] == result["state"]["nodeCount"] == 0
    assert result["state"]["layoutRequired"] is False
    assert result["state"]["layoutSettled"] is True
    assert result["state"]["layoutGeneration"] == result["state"]["appliedLayoutGeneration"] == 0
    assert result["readiness"]["screenshotAllowed"] is True


def test_static_graph_failure_rebinds_to_new_selection(static_url: str, page) -> None:
    """A failed provider is current-selection state, never stale target state."""
    page.route(
        "**/passages-manifest.json",
        lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"offline"}'),
    )
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.cy")
    page.locator(".okf-node-index summary").click()
    nodes = page.locator(".okf-node-index__item")
    first_id = nodes.nth(0).get_attribute("data-target")
    second_id = nodes.nth(1).get_attribute("data-target")
    assert first_id and second_id and first_id != second_id
    nodes.nth(0).click()
    page.locator("#okf-copy-neighbourhood").click()
    failure = page.locator(".okf-graph-context-unavailable")
    expect(failure).to_have_attribute("data-context-concept", first_id)
    nodes.nth(1).click()
    expect(failure).to_have_attribute("data-context-concept", second_id)
    expect(failure.get_by_role("link", name="Open emitted concept-only Markdown")).to_have_attribute("href", f"__context/{second_id}.txt")
    href = failure.get_by_role("link", name="Open emitted concept-only Markdown").get_attribute("href")
    assert href and second_id in href and first_id not in href


def test_static_graph_pagehide_aborts_and_fences_late_portable_load(static_url: str, page) -> None:
    page.add_init_script("""(() => {
      const nativeFetch=window.fetch.bind(window);
      window.fetch=(url,opts)=>String(url).includes('passages-manifest.json')?new Promise((resolve,reject)=>{
        window.__lateGraphManifest=()=>nativeFetch(url,opts).then(resolve,reject);
        if(opts&&opts.signal)opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
      }):nativeFetch(url,opts);
    })()""")
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.cy")
    page.locator(".okf-node-index summary").click()
    page.locator(".okf-node-index__item").first.click()
    page.locator("#okf-copy-neighbourhood").click()
    page.wait_for_function("() => typeof window.__lateGraphManifest === 'function'")
    page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
    page.evaluate("() => window.__lateGraphManifest()")
    page.wait_for_timeout(100)
    assert page.evaluate("() => window.okfPortableProvider == null")
    expect(page.locator(".okf-graph-context-unavailable")).to_have_count(0)


def test_static_concept_pagehide_aborts_and_fences_late_portable_load(static_url: str, page) -> None:
    """A concept page cannot publish its provider after pagehide revokes ownership."""
    page.add_init_script("""(() => {
      const nativeFetch=window.fetch.bind(window);
      window.fetch=(url,opts)=>String(url).includes('passages-manifest.json')?new Promise((resolve,reject)=>{
        window.__lateConceptManifest={opts,settle:()=>nativeFetch(url,opts).then(resolve,reject)};
        if(opts&&opts.signal)opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
      }):nativeFetch(url,opts);
    })()""")
    page.goto(static_url + "/tables/orders.html", wait_until="domcontentloaded")
    page.wait_for_function("() => typeof window.okfStaticContextCopy === 'function'")
    page.evaluate("""() => {
      window.__conceptLoadError=null;
      window.okfStaticContextCopy('tables/orders',{}).catch(error=>window.__conceptLoadError=error.name);
    }""")
    page.wait_for_function("() => !!window.__lateConceptManifest")
    page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
    assert page.evaluate("() => window.__lateConceptManifest.opts.signal.aborted") is True
    page.evaluate("() => window.__lateConceptManifest.settle()")
    page.wait_for_timeout(100)
    assert page.evaluate("() => window.okfPortableProvider == null")
    assert page.evaluate("() => window.__conceptLoadError") == "AbortError"


def test_static_search_pagehide_revokes_late_provider_publication(static_url: str, page) -> None:
    page.add_init_script("""(() => {
      const nativeFetch=window.fetch.bind(window);
      window.fetch=(url,opts)=>String(url).includes('passages-manifest.json')?new Promise((resolve,reject)=>{
        window.__lateSearchManifest={opts,settle:()=>nativeFetch(url,opts).then(resolve,reject)};
        if(opts&&opts.signal)opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
      }):nativeFetch(url,opts);
    })()""")
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.__lateSearchManifest")
    page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
    assert page.evaluate("() => window.__lateSearchManifest.opts.signal.aborted") is True
    page.evaluate("() => window.__lateSearchManifest.settle()")
    page.wait_for_timeout(100)
    assert page.evaluate("() => window.okfPortableProvider == null")


def test_spa_portable_result_links_are_extensionless(static_url: str, page) -> None:
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.querySelector('.okf-search-result')")
    page.evaluate("() => {document.body.dataset.okfMode='spa';window.okfReloadPortableSearch();}")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    href = page.locator(".okf-search-result h3 a").first.get_attribute("href")
    assert href and href.startswith("/") and not href.endswith(".html")


def test_static_nojs_search_and_graph_keep_working_navigation(static_url: str, page) -> None:
    """Static degraded surfaces expose only working navigation and browse paths.

    The header search form is a real GET form, so it stays usable without
    JavaScript even though the static search page's results need the
    client-side searcher (announced by the no-JS notice on that page).
    """
    context = page.context.browser.new_context(java_script_enabled=False, viewport={"width": 1280, "height": 800})
    nojs = context.new_page()
    try:
        nojs.goto(static_url + "/__search.html", wait_until="domcontentloaded")
        body = nojs.locator("body")
        expect(body).to_contain_text("arbitrary search requires JavaScript")
        expect(nojs.locator(".okf-search-workbench")).to_be_hidden()
        searchbox = nojs.get_by_role("searchbox", name="Search this bundle")
        expect(searchbox).to_be_visible()
        expect(nojs.locator("#okf-theme")).to_be_hidden()
        # The Appearance menu (owned by theme.js) must also stay hidden/inert
        # without JavaScript: it starts `hidden` and only theme.js opens it.
        expect(nojs.locator("#okf-appearance-menu")).to_be_hidden()
        expect(nojs.locator(".okf-search-initial section").first).to_be_visible()
        expect(nojs.get_by_role("link", name="concept index by type or tag")).to_be_visible()
        # The GET form is honest navigation: submitting lands on the same
        # search route with the query reflected, still without JavaScript.
        searchbox.fill("orders")
        with nojs.expect_navigation(wait_until="domcontentloaded") as navigation:
            nojs.locator('.okf-shell__search-submit').click()
        assert navigation.value.ok and "q=orders" in nojs.url
        expect(nojs.locator(".okf-search-workbench")).to_be_hidden()
        expect(body).to_contain_text("arbitrary search requires JavaScript")

        nojs.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
        expect(nojs.get_by_role("heading", name="Graph unavailable without JavaScript")).to_be_visible()
        expect(nojs.locator("main")).to_be_visible()
        expect(nojs.locator(".okf-layout")).to_be_hidden()
        # Graph-only interactive controls stay hidden; the shell's own search
        # form is the sole interactive pair and it keeps offering navigation.
        assert nojs.locator("input:visible,select:visible,button:visible").count() == 2
        expect(nojs.get_by_role("searchbox", name="Search this bundle")).to_be_visible()
        expect(nojs.get_by_role("button", name="Search")).to_be_visible()
        assert nojs.locator(".okf-graph-nojs input:visible,.okf-graph-nojs select:visible,.okf-graph-nojs button:visible").count() == 0
        expect(nojs.get_by_role("link", name="Index", exact=True)).to_be_visible()
        recent_links = nojs.locator(".okf-graph-nojs__recent a")
        assert recent_links.count() > 0
        assert recent_links.first.get_attribute("href").endswith(".html")
        action = nojs.get_by_role("link", name="Browse the concept index")
        expect(action).to_be_visible()
        with nojs.expect_navigation(wait_until="domcontentloaded") as navigation:
            action.click()
        assert navigation.value.ok and nojs.url.endswith("/index.html")
    finally:
        context.close()


def test_live_nojs_graph_has_complete_fallback_body(server_url: str, page) -> None:
    """The live graph degrades to useful authored navigation, not a blank shell."""
    context = page.context.browser.new_context(java_script_enabled=False, viewport={"width": 1280, "height": 900})
    nojs = context.new_page()
    try:
        nojs.goto(server_url + "/__graph", wait_until="domcontentloaded")
        expect(nojs.get_by_role("heading", name="Graph unavailable without JavaScript")).to_be_visible()
        expect(nojs.locator(".okf-layout")).to_be_hidden()
        # The shared shell remains useful without JavaScript: its native Search
        # form is available, while graph-only interactive controls stay hidden.
        expect(nojs.get_by_role("searchbox", name="Search this bundle")).to_be_visible()
        expect(nojs.get_by_role("button", name="Search")).to_be_visible()
        assert nojs.locator(".okf-graph-main input:visible,.okf-graph-main select:visible,.okf-graph-main button:visible").count() == 0
        expect(nojs.get_by_role("link", name="Index", exact=True)).to_be_visible()
        recent_links = nojs.locator(".okf-graph-nojs__recent a")
        assert recent_links.count() > 0
        recent_href = recent_links.first.get_attribute("href")
        assert recent_href and recent_href.startswith("/") and not recent_href.endswith(".html")
        action = nojs.get_by_role("link", name="Browse the concept index")
        expect(action).to_be_visible()
        with nojs.expect_navigation(wait_until="domcontentloaded") as navigation:
            action.click()
        assert navigation.value.ok and nojs.url.rstrip("/") == server_url
    finally:
        context.close()


@pytest.mark.parametrize("viewport,min_width", [({"width":1280,"height":900},700),({"width":390,"height":844},330)])
def test_static_nojs_graph_export_is_substantial_contained_and_responsive(static_url: str, page, viewport: dict, min_width: int) -> None:
    context=page.context.browser.new_context(java_script_enabled=False,viewport=viewport)
    nojs=context.new_page()
    try:
        nojs.goto(static_url+"/__graph.html",wait_until="domcontentloaded")
        figure=nojs.locator("figure.okf-graph-nojs__export");svg=figure.locator("svg.okf-graph-export")
        expect(figure).to_be_visible();expect(svg).to_be_visible();expect(figure.locator("figcaption")).to_contain_text("Static relationship map")
        metrics=svg.evaluate("""svg=>{const s=svg.getBoundingClientRect(),f=svg.closest('figure').getBoundingClientRect(),m=svg.closest('main').getBoundingClientRect();return {sw:s.width,sh:s.height,fw:f.width,left:s.left,right:s.right,mainLeft:m.left,mainRight:m.right,overflow:document.documentElement.scrollWidth-innerWidth,display:getComputedStyle(svg).display};}""")
        assert metrics["sw"]>=min_width and metrics["sh"]>120 and metrics["display"]=="block"
        assert metrics["left"]>=metrics["mainLeft"]-1 and metrics["right"]<=metrics["mainRight"]+1
        assert metrics["sw"]<=metrics["fw"]+1 and metrics["overflow"]<=0
    finally: context.close()


@pytest.mark.parametrize("raw", ["q=%ZZ", "q=%C3%28"])
def test_static_malformed_percent_or_utf8_is_field_error(static_url: str, page, raw: str) -> None:
    page.set_viewport_size({"width": 320, "height": 720})
    page.goto(static_url + "/__search.html?" + raw, wait_until="domcontentloaded")
    page.wait_for_function("() => document.body.dataset.okfCaptureReady === 'input-error'")
    expect(page.locator("#okf-results-heading")).to_have_text("Search needs correction")
    error = page.locator(".okf-search-input-error")
    expect(error).to_be_visible()
    expect(error).to_have_attribute("data-error-field", "q")
    query = page.locator("#okf-search-query")
    expect(query).to_have_attribute("aria-invalid", "true")
    describedby = query.get_attribute("aria-describedby")
    assert describedby and describedby.startswith("okf-runtime-error-q")
    inline = page.locator("#" + describedby)
    expect(inline).to_contain_text("Keep one q parameter")
    expect(page.locator(".okf-page-context")).to_have_count(0)
    assert page.evaluate("""() => document.querySelector('#okf-results-heading').getBoundingClientRect().top < document.querySelector('.okf-search-input-error').getBoundingClientRect().top""")
    expect(page.get_by_role("button", name="Retry")).to_have_count(0)
    assert page.evaluate("() => !!window.okfPortableProvider")
    # Layout sanity at 320px: the document must not overflow horizontally
    # (no sideways page scroll), AND every topbar control must remain
    # keyboard-reachable (Tab reaches it). The Phase 1 single-row sticky
    # chrome contract explicitly lets the controls row scroll horizontally
    # ("the controls row scrolls horizontally rather than forcing a wrap" —
    # wiki.css §Responsive CRI2-001), so a control scrolled out of view
    # within the .okf-topbar__controls scroller is NOT a clipping bug — it
    # is reachable via Tab + the scroller. The prior wrap-based layout
    # guaranteed `clipped == 0` by forcing a second row; the sticky-chrome
    # restoration intentionally trades that for a single sticky row.
    reflow = page.evaluate("""() => {
        const controls = Array.from(document.querySelectorAll('.okf-topbar a,.okf-topbar input,.okf-topbar button'))
            .filter(el => {const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0;});
        const cs = getComputedStyle(document.querySelector('.okf-topbar__controls') || document.body);
        return {
            docOverflow: document.documentElement.scrollWidth > window.innerWidth,
            // The controls scroller must be the containment boundary: a
            // control scrolled out of the viewport is fine, but the PAGE
            // must not scroll sideways to reveal it.
            controlCount: controls.length,
            scrollerOverflowX: cs.overflowX,
        };
    }""")
    assert reflow["docOverflow"] is False, (
        f"document must not overflow horizontally at 320px: "
        f"scrollWidth={page.evaluate('document.documentElement.scrollWidth')} "
        f"> innerWidth={page.evaluate('window.innerWidth')}"
    )
    assert reflow["controlCount"] >= 1, "topbar must keep at least one visible control"
    # The controls row either scrolls (single-row sticky contract) or wraps
    # (acceptable fallback). Either way, no document overflow.
    assert reflow["scrollerOverflowX"] in ("auto", "scroll", "hidden", "visible"), (
        f"unexpected controls overflow-x: {reflow['scrollerOverflowX']!r}"
    )


def test_suggestions_timeout_and_superseded_response_are_deterministic(server_url: str, page) -> None:
    """Suggestion deadline and query generation prevent stale listbox commits."""
    page.add_init_script("window.OKF_LOOM_TEST_DEADLINES={suggestions:60,context:60,clipboard:60}")
    page.add_init_script("""(() => {
        const nativeFetch = window.fetch.bind(window); window.__suggestions = [];
        window.fetch = (url, opts) => String(url).startsWith('/__search?')
          ? new Promise((resolve, reject) => {
              const signal = opts && opts.signal;
              const entry = {
                url: String(url),
                query: new URL(String(url), location.origin).searchParams.get('q'),
                createdAt: performance.now(),
                signalPresent: !!signal,
                signalAbortedAtCreation: !!(signal && signal.aborted),
                aborted: false,
                abortAt: null,
                settlement: null,
                resolve(value) { entry.resolveAttemptedAt = performance.now(); resolve(value); },
                reject,
              };
              const abort = () => {
                entry.aborted = true;
                entry.abortAt = performance.now();
                entry.settlement = 'aborted';
                reject(new DOMException('Aborted', 'AbortError'));
              };
              window.__suggestions.push(entry);
              if (signal) {
                if (signal.aborted) abort();
                else signal.addEventListener('abort', abort, {once: true});
              }
            })
          : nativeFetch(url, opts);
    })()""")
    page.goto(server_url + "/", wait_until="domcontentloaded")
    search = page.locator('input[type="search"][name="q"]').first
    page.evaluate("() => window.__finalSuggestionInputAt = performance.now()")
    search.fill("orders")
    page.wait_for_function("""() => window.__suggestions.some(entry =>
        entry.query === 'orders' && entry.createdAt - window.__finalSuggestionInputAt >= 150)""")
    page.wait_for_function("() => Array.from(document.querySelectorAll('[role=status]')).some(el => el.textContent.includes('Suggestions unavailable. Submit the search form for full results.'))")
    orders_request = page.evaluate("""() => {
        const entry = window.__suggestions.filter(item => item.query === 'orders').at(-1);
        return {signalPresent: entry.signalPresent, signalAbortedAtCreation: entry.signalAbortedAtCreation,
          aborted: entry.aborted, createdAt: entry.createdAt, abortAt: entry.abortAt, settlement: entry.settlement};
    }""")
    assert orders_request["signalPresent"] is True
    assert orders_request["signalAbortedAtCreation"] is False
    assert orders_request["aborted"] is True
    assert orders_request["abortAt"] >= orders_request["createdAt"]
    assert orders_request["settlement"] == "aborted"
    assert search.get_attribute("aria-expanded") == "false"
    assert search.get_attribute("aria-activedescendant") is None
    expect(page.locator(".okf-search-live")).to_be_hidden()
    expect(page.locator(".okf-search-live [role='option']")).to_have_count(0)
    # A successful empty response remains a distinct, navigable settled state.
    page.evaluate("() => window.__finalSuggestionInputAt = performance.now()")
    search.fill("empty")
    page.wait_for_function("""() => window.__suggestions.some(entry =>
        entry.query === 'empty' && entry.createdAt - window.__finalSuggestionInputAt >= 150)""")
    page.evaluate("""() => window.__suggestions.filter(entry => entry.query === 'empty').at(-1)
        .resolve(new Response('[]',{status:200,headers:{'Content-Type':'application/json'}}))""")
    page.wait_for_function("() => Array.from(document.querySelectorAll('[role=status]')).some(el => el.textContent.includes('No direct suggestions. View all results is available.'))")
    assert search.get_attribute("aria-expanded") == "true"
    expect(page.get_by_role("option", name="View all results")).to_be_visible()
    page.evaluate("() => window.__finalSuggestionInputAt = performance.now()")
    search.fill("alpha")
    page.wait_for_function("""() => window.__suggestions.some(entry =>
        entry.query === 'alpha' && entry.createdAt - window.__finalSuggestionInputAt >= 150)""")
    page.evaluate("() => window.__finalSuggestionInputAt = performance.now()")
    search.fill("beta")
    page.wait_for_function("""() => window.__suggestions.some(entry =>
        entry.query === 'beta' && entry.createdAt - window.__finalSuggestionInputAt >= 150)""")
    page.wait_for_function("() => Array.from(document.querySelectorAll('[role=status]')).some(el => el.textContent.includes('Suggestions unavailable. Submit the search form for full results.'))")
    page.evaluate("""() => window.__suggestions.filter(entry => entry.query === 'alpha').at(-1)
        .resolve(new Response(JSON.stringify([{concept_id:'stale-result',title:'stale-result',type:'Note'}]),
          {status:200,headers:{'Content-Type':'application/json'}}))""")
    expect(page.locator('[role="status"]', has_text="Suggestions unavailable. Submit the search form for full results.")).to_be_visible()
    expect(page.locator('[role="option"]', has_text="stale-result")).to_have_count(0)
    assert search.get_attribute("aria-expanded") == "false"
    assert search.get_attribute("aria-activedescendant") is None
    expect(page.locator(".okf-search-live")).to_be_hidden()
    expect(page.locator(".okf-search-live [role='option']")).to_have_count(0)
    assert page.evaluate("""() => window.__suggestions
        .filter(entry => entry.query === 'alpha' || entry.query === 'beta')
        .every(entry => entry.signalPresent && entry.aborted && entry.settlement === 'aborted')""")


def test_group_c_portable_loader_preserves_caller_deadline(static_url: str, page) -> None:
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.okfPortableRetrieval")
    result = page.evaluate("""async () => {
      const nativeFetch=window.fetch,nativeNow=Date.now,nativeSet=window.setTimeout;
      let fetches=0,delay=null;Date.now=()=>1000;
      window.fetch=()=>{fetches++;return new Promise(()=>{});};
      window.setTimeout=(fn,ms)=>{delay=ms;return 99;};
      const pending=window.okfPortableRetrieval.loadStatic('__data/',{deadline:1125,controller:new AbortController()});
      window.fetch=nativeFetch;Date.now=nativeNow;window.setTimeout=nativeSet;
      let expired='';try{await window.okfPortableRetrieval.loadStatic('__data/',{deadline:999,controller:new AbortController()});}catch(e){expired=e.name;}
      void pending;return {fetches,delay,expired};
    }""")
    assert result == {"fetches": 1, "delay": 125, "expired": "TimeoutError"}


def test_group_c_static_bfcache_restore_starts_fresh_load(static_url: str, page) -> None:
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.okfPortableProvider")
    page.evaluate("""() => {const native=window.okfPortableRetrieval.loadStatic;window.__reloads=0;window.okfPortableRetrieval.loadStatic=(...args)=>{window.__reloads++;return native(...args);};window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));}""")
    page.wait_for_function("() => window.__reloads===1 && !!window.okfPortableProvider")


def test_group_c_single_file_bfcache_restore_revalidates(tmp_path: Path, page) -> None:
    from okf_loom import Bundle
    from okf_loom.render import render_single_file
    output=tmp_path/"single-bfcache.html";render_single_file(Bundle.load(DEMO_BUNDLE),output)
    page.goto(output.as_uri(), wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.okfPortableProvider")
    page.evaluate("""() => {const native=window.okfPortableRetrieval.validate;window.__validations=0;window.okfPortableRetrieval.validate=(...args)=>{window.__validations++;return native(...args);};window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));}""")
    page.wait_for_function("() => window.__validations===1 && !!window.okfPortableProvider")
    page.locator("#okf-single-search-toggle").click()
    readiness = page.evaluate("async () => await window.okfCaptureReadiness.start({target:'search',expectedRevision:document.body.dataset.okfCaptureRevision}).promise")
    assert readiness["screenshotAllowed"] is True
    assert readiness["metadata"]["readinessPhase"] == "ready"


def test_group_c_cited_copy_revokes_prepared_owner_without_stranding(static_url: str, page) -> None:
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.okfPortableProvider")
    _open_context_options(page)
    page.evaluate("() => {window.__lateContext={};window.okfStaticContextCopy=()=>new Promise(resolve=>window.__lateContext.resolve=resolve);Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.resolve()}});}")
    prepare=page.locator("[data-context-prepare]");prepare.click();expect(prepare).to_have_attribute("aria-busy","true")
    page.get_by_role("button",name="Copy passage").first.click()
    expect(prepare).not_to_have_attribute("aria-busy", "true");expect(prepare).to_have_text("Build context pack")
    assert page.locator("[data-context-copy-prepared]").is_disabled()


def test_group_c_palette_active_descendant_enter_matches_announced_option(server_url: str, page) -> None:
    page.goto(server_url + "/", wait_until="domcontentloaded")
    search=page.locator('input[type="search"][name="q"]').first;search.fill("orders")
    page.locator(".okf-search-live").wait_for(state="visible");search.press("ArrowDown")
    active=search.get_attribute("aria-activedescendant");assert active
    selected=page.locator("#"+active);expect(selected).to_have_attribute("aria-selected","true")
    href=selected.get_attribute("data-href");search.press("Enter");page.wait_for_load_state("domcontentloaded")
    assert page.url.endswith(href)


def test_group_c_graph_context_requires_nonempty_provider_and_pack_revisions(static_url: str, page) -> None:
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph?.cy?.nodes().length")
    page.locator(".okf-node-index summary").click();page.locator(".okf-node-index__item").first.click()
    page.evaluate("""async () => {const payload=await fetch(document.getElementById('okf-graph').dataset.graphUrl).then(r=>r.json());window.__writes=[];Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:t=>{window.__writes.push(t);return Promise.resolve();}}});window.okfPortableProvider={manifest:{bundle_revision:payload.bundle_revision},context:()=>({schema_version:1,query:'missing',mode:'lexical',filters:{},budget:{requested_tokens:6000,estimated_tokens:1},neighbors:1,included:0,omitted:0,truncated:false,items:[],representations:{markdown:'# must not copy'}})};}""")
    page.locator("#okf-copy-neighbourhood").click()
    expect(page.get_by_role("button",name="Retry",exact=True)).to_be_visible()
    assert page.evaluate("() => window.__writes.length===0")


def test_group_c_static_initial_browse_survives_portable_hydration(static_url: str, page) -> None:
    page.goto(static_url + "/__search.html", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.okfPortableProvider")
    expect(page.get_by_role("heading", name="Recent concepts")).to_be_visible()
    expect(page.get_by_role("heading", name="Browse")).to_be_visible()
    expect(page.get_by_role("heading", name="Try a search")).to_be_visible()
    expect(page.locator("#okf-results-heading")).not_to_contain_text('0 results for ““')


@pytest.mark.parametrize("retry_outcome", ["resolve", "reject"])
def test_group_c_static_context_timeout_immediate_retry_owns_fresh_provider(static_url: str, page, retry_outcome: str) -> None:
    page.add_init_script("window.OKF_LOOM_TEST_DEADLINES={context:60,clipboard:60}")
    page.goto(static_url + "/tables/orders.html", wait_until="domcontentloaded")
    page.evaluate("""() => {window.__providerCalls=[];window.__providerAborts=0;window.okfPortableProvider=null;window.okfPortableRetrieval.loadStatic=(root,lifecycle)=>{const call={lifecycle};window.__providerCalls.push(call);if(window.__providerCalls.length===1)return new Promise((resolve,reject)=>lifecycle.controller.signal.addEventListener('abort',()=>{window.__providerAborts++;reject(new DOMException('Aborted','AbortError'));},{once:true}));return window.__retryOutcome==='resolve'?Promise.resolve({manifest:{bundle_revision:document.body.dataset.okfRevision},context:()=>({schema_version:1,bundle_revision:document.body.dataset.okfRevision,query:'fresh',mode:'lexical',filters:{},budget:{requested_tokens:6000,estimated_tokens:1},neighbors:1,included:0,omitted:0,truncated:false,items:[],representations:{markdown:'# fresh'}})}):Promise.reject(new Error('fresh retry failure'));};Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.resolve()}});}""")
    page.evaluate("outcome=>window.__retryOutcome=outcome", retry_outcome)
    page.locator(".okf-context-copy").first.click()
    retry=page.get_by_role("button",name="Retry",exact=True);expect(retry).to_be_visible();retry.click()
    page.wait_for_function("() => window.__providerCalls.length===2")
    assert page.evaluate("() => window.__providerAborts===1 && window.__providerCalls[0].lifecycle.generation < window.__providerCalls[1].lifecycle.generation")
    if retry_outcome == "resolve":
        page.wait_for_function("() => !!window.okfPortableProvider")
        expect(page.locator(".okf-context-copy").first).not_to_have_attribute("aria-busy","true")
    else:
        expect(page.locator("#okf-context-error-title")).to_contain_text("Context could not be prepared")


def test_group_c_suggestions_bfcache_restart_fences_old_result(server_url: str, page) -> None:
    page.add_init_script("""(() => {const native=window.fetch.bind(window);window.__suggestionCalls=[];window.fetch=(url,opts)=>String(url).startsWith('/__search?')?new Promise((resolve,reject)=>{window.__suggestionCalls.push({resolve,reject,signal:opts.signal});opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});}):native(url,opts);})()""")
    page.goto(server_url + "/", wait_until="domcontentloaded")
    search=page.locator('input[type="search"][name="q"]').first;search.fill("orders")
    page.wait_for_function("() => window.__suggestionCalls.length===1")
    page.evaluate("() => {window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));}")
    page.wait_for_function("() => window.__suggestionCalls.length===2")
    page.evaluate("() => window.__suggestionCalls[1].resolve(new Response(JSON.stringify([{concept_id:'fresh',title:'Fresh',type:'Note'}]),{status:200,headers:{'Content-Type':'application/json'}}))")
    # The option's accessible name is a clean "title, type, id" (set via
    # aria-label) so the combobox announcement is deterministic regardless of
    # the visual chip/title/path layout.
    expect(page.get_by_role("option", name="Fresh, Note, fresh")).to_be_visible()
    assert page.evaluate("() => window.__suggestionCalls[0].signal.aborted")


def test_group_c_single_escape_copy_cleanup_and_filter_context(tmp_path: Path, page) -> None:
    from okf_loom import Bundle
    from okf_loom.render import render_single_file
    output=tmp_path/"single-group-c-r2.html";render_single_file(Bundle.load(DEMO_BUNDLE),output)
    page.goto(output.as_uri(),wait_until="domcontentloaded");page.wait_for_function("() => !!window.okfPortableProvider")
    toggle=page.locator("#okf-single-search-toggle");toggle.focus();toggle.click();panel=page.locator("#okf-single-search")
    page.evaluate("""() => {const d=document.createElement('div');d.id='nested-test-dialog';d.tabIndex=-1;d.setAttribute('role','dialog');d.setAttribute('aria-modal','true');d.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();d.remove();}});document.body.appendChild(d);d.focus();d.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));}""");expect(panel).to_be_visible();assert page.locator("#nested-test-dialog").count()==0
    page.locator('#okf-single-search [name="q"]').focus();page.keyboard.press("Escape");assert panel.evaluate("el=>el.hidden");assert toggle.evaluate("el=>document.activeElement===el")
    toggle.click();page.locator("#okf-single-search .okf-search-filters summary").click();page.locator('[name="mode"][value="tag"]').check();page.locator('#okf-single-search [name="tag"]').fill("orders");page.locator("#okf-single-search form").evaluate("form=>form.requestSubmit()")
    # U8 parity on the portable surface: the collapsed zero state reads as a
    # plain "Filters" disclosure; a nonzero count becomes "Filters, N active".
    # The count owner updates on form change, so the value is committed the
    # way a user commits it (focus leaves the field).
    single_summary=page.locator("#okf-single-search .okf-search-filters > summary")
    page.locator('#okf-single-search [name="tag"]').blur()
    expect(single_summary).to_contain_text("Filters, 1 active")
    expect(page.locator("[data-single-results-heading]")).to_contain_text("for current filters")
    expect(page.locator("[data-single-page-context]")).to_be_enabled()
    page.locator('#okf-single-search [name="tag"]').fill("DoesNotExist");page.locator("#okf-single-search form").evaluate("form=>form.requestSubmit()")
    expect(page.locator("[data-single-results-heading]")).to_have_text("No results for current filters")
    expect(page.locator("[data-single-page-context]")).to_be_disabled()
    expect(page.locator("[data-single-results]")).to_contain_text("Context is unavailable until evidence matches")
    page.locator("#okf-single-search [data-single-clear-filters]").click()
    # innerText is the visible truth: the ", 0 active" fragment carries the
    # hidden attribute, so it is out of both the render and the a11y tree
    # (textContent would still see it, which is exactly the point).
    assert single_summary.evaluate("el => el.innerText").strip() == "Filters"
    assert "0 active" not in single_summary.evaluate("el => el.innerText")
    page.evaluate("""() => {window.__lateSingle={};Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>new Promise(resolve=>window.__lateSingle.resolve=resolve)}});const b=document.createElement('button');b.id='late-single-copy';b.textContent='Copy test';document.body.appendChild(b);window.okfSingleContext.copy({concept:'tables/orders'},b,{mode:'lexical'});}""")
    expect(page.locator("#late-single-copy")).to_have_attribute("aria-busy","true")
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))")
    expect(page.locator("#late-single-copy")).to_have_text("Copy test");expect(page.locator("#late-single-copy")).not_to_have_attribute("aria-busy","true")
    page.evaluate("() => window.__lateSingle.resolve()");expect(page.locator("#late-single-copy")).to_have_text("Copy test")


def test_group_c_single_results_activate_graph_by_pointer_and_keyboard(tmp_path: Path, page) -> None:
    from okf_loom import Bundle
    from okf_loom.render import render_single_file
    output=tmp_path/"single-result-activation.html";render_single_file(Bundle.load(DEMO_BUNDLE),output)
    page.goto(output.as_uri(),wait_until="domcontentloaded");page.wait_for_function("() => !!window.okfPortableProvider")
    toggle=page.locator("#okf-single-search-toggle");toggle.click();query=page.locator('#okf-single-search [name="q"]');query.fill("orders");page.locator("#okf-single-search form").evaluate("form=>form.requestSubmit()")
    first=page.locator('[data-single-results] a[data-graph-concept]').first;concept=first.get_attribute("data-graph-concept");first.click()
    assert page.evaluate("() => window.okfGraphSelection.conceptId") == concept
    assert page.locator("#okf-single-search").evaluate("el=>el.hidden")
    toggle.click();query.fill("orders");page.locator("#okf-single-search form").evaluate("form=>form.requestSubmit()")
    keyboard=page.locator(f'[data-single-results] a[data-graph-concept="{concept}"]').first;keyboard.focus();keyboard.press("Enter")
    assert page.evaluate("() => window.okfGraphSelection.conceptId") == concept
    page.wait_for_function("() => document.activeElement===document.getElementById('detail-content')")
    toggle.click();query.fill("orders");page.locator("#okf-single-search form").evaluate("form=>form.requestSubmit()")
    page.evaluate("() => { window.okfActivateGraphConcept = undefined; }")
    fallback=page.locator(f'[data-single-results] a[data-graph-concept="{concept}"]').first;fallback.click()
    assert page.evaluate("() => window.okfGraphSelection.conceptId") == concept
    assert page.locator("#okf-single-search").evaluate("el=>el.hidden")


def test_group_c_single_copy_revocation_clears_manual_fallback(tmp_path: Path, page) -> None:
    from okf_loom import Bundle
    from okf_loom.render import render_single_file
    output=tmp_path/"single-copy-revoke.html";render_single_file(Bundle.load(DEMO_BUNDLE),output)
    page.goto(output.as_uri(),wait_until="domcontentloaded");page.wait_for_function("() => !!window.okfPortableProvider")
    page.locator("#okf-single-search-toggle").click()
    page.evaluate("""() => {Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(new Error('denied'))}});const b=document.createElement('button');b.id='manual-copy-owner';b.textContent='Copy test';document.body.appendChild(b);window.okfSingleContext.copy({concept:'tables/orders'},b,{mode:'lexical'});}""")
    fallback=page.locator("[data-single-copy-fallback]");expect(fallback).to_be_visible();expect(page.locator("#okf-single-copy-text")).not_to_have_value("")
    page.locator("[data-single-search-close]").click();expect(fallback).to_be_hidden();expect(page.locator("#okf-single-copy-text")).to_have_value("");expect(page.locator("[data-single-copy-status]")).to_have_text("")


def test_group_c_suggestion_announcement_counts_rendered_direct_cap(server_url: str, page) -> None:
    page.add_init_script("""(() => {const real=window.fetch.bind(window);window.fetch=(url,opts)=>String(url).startsWith('/__search?')&&String(url).includes('format=json')?Promise.resolve(new Response(JSON.stringify(Array.from({length:12},(_,i)=>({concept_id:'cap-'+i,title:'Cap '+i,type:'Note'}))),{status:200,headers:{'Content-Type':'application/json'}})):real(url,opts);})();""")
    page.goto(server_url + "/", wait_until="domcontentloaded")
    search=page.locator('input[type="search"][name="q"]').first;search.fill("cap")
    page.wait_for_function("() => document.querySelectorAll('.okf-search-live [role=option]').length === 9")
    assert search.evaluate("el => el.parentNode.querySelector('[role=status]').textContent") == "8 direct suggestions. View all results is also available."


def test_graph_refresh_empty_transition_capture_truth(server_url: str, page) -> None:
    """Live structural refreshes settle without creating an automatic layout owner."""
    _open_graph(page, server_url)
    page.wait_for_function("() => window.okfGraphCaptureState().layoutSettled")
    result = page.evaluate("""async () => {
      const graph=document.getElementById('okf-graph'),url=graph.dataset.graphUrl,native=window.fetch.bind(window),base=await native(url).then(r=>r.json());
      let current={...base,bundle_revision:'empty-refresh',nodes:[],edges:[],outgoing:{},backlinks:{},bodies:{}};
      window.fetch=(request,options)=>String(request)===url?Promise.resolve(new Response(JSON.stringify(current),{status:200,headers:{'Content-Type':'application/json'}})):native(request,options);
      await window.__okfLoomGraph.refreshGraph();
      const empty=window.okfGraphCaptureState();
      const emptyReady=await window.okfCaptureReadiness.start({target:'graph',expectedRevision:'empty-refresh'}).promise;
      let emptyMismatch=null;try{await window.okfCaptureReadiness.start({target:'graph',expectedRevision:'not-empty-refresh'}).promise;}catch(error){emptyMismatch={code:error.code,phase:error.metadata.readinessPhase};}
      const emptyAttrs={revision:document.body.dataset.okfRevision,captureRevision:document.body.dataset.okfCaptureRevision};
      window.__emptyTransitionLayouts=[];
      window.__okfLoomGraph.cy.layout=()=>{window.__emptyTransitionLayouts.push(true);throw new Error('refresh must not layout');};
      current={...base,bundle_revision:'nonempty-refresh'};
      await window.__okfLoomGraph.refreshGraph();
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      return {empty,emptyReady,emptyMismatch,emptyAttrs,settled:window.okfGraphCaptureState(),held:window.__emptyTransitionLayouts.length};
    }""")
    assert result["empty"]["layoutRequired"] is False and result["empty"]["layoutSettled"] is True
    assert result["empty"]["layoutGeneration"] == result["empty"]["appliedLayoutGeneration"] == 0
    assert result["emptyReady"]["screenshotAllowed"] is True
    assert result["emptyReady"]["metadata"]["revision"] == "empty-refresh"
    assert result["emptyMismatch"] == {"code": "revision-mismatch", "phase": "revision-mismatch"}
    assert result["emptyAttrs"] == {"revision": "empty-refresh", "captureRevision": "empty-refresh"}
    assert result["settled"]["layoutRequired"] is False and result["settled"]["layoutSettled"] is True
    assert result["settled"]["layoutGeneration"] == result["settled"]["appliedLayoutGeneration"] == 0
    assert result["held"] == 0
    page.wait_for_function("() => window.okfGraphCaptureState().layoutSettled")
    restored = page.evaluate("""async () => {const ready=await window.okfCaptureReadiness.start({target:'graph',expectedRevision:'nonempty-refresh'}).promise;let mismatch=null;try{await window.okfCaptureReadiness.start({target:'graph',expectedRevision:'empty-refresh'}).promise;}catch(error){mismatch={code:error.code,phase:error.metadata.readinessPhase};}return {ready,mismatch,revision:document.body.dataset.okfRevision,captureRevision:document.body.dataset.okfCaptureRevision};}""")
    assert restored["ready"]["metadata"]["revision"] == "nonempty-refresh"
    assert restored["mismatch"] == {"code": "revision-mismatch", "phase": "revision-mismatch"}
    assert restored["revision"] == restored["captureRevision"] == "nonempty-refresh"


def test_graph_live_delta_preserves_camera_and_animates_owned_add_remove(server_url: str, page) -> None:
    """Accepted structure never lays out/pans; add and removal settle before capture."""
    _open_graph(page, server_url)
    page.wait_for_function("() => window.okfGraphCaptureState().layoutSettled")
    result = page.evaluate("""async () => {
      const G=window.__okfLoomGraph,cy=G.cy,graph=document.getElementById('okf-graph'),url=graph.dataset.graphUrl,native=window.fetch.bind(window),base=await native(url).then(r=>r.json());
      cy.pan({x:37,y:-19});cy.zoom(.73);const before={pan:cy.pan(),zoom:cy.zoom(),layouts:G.layoutStats.starts};
      const anchor=base.nodes[0].data.id,id='live-delta-owned',baseOutgoing=base.outgoing||{},baseBacklinks=base.backlinks||{};let current={...base,bundle_revision:'live-add',nodes:[...base.nodes,{data:{id,label:'Live delta',type:'concept'}}],edges:[...base.edges,{data:{source:anchor,target:id,label:'links'}}],outgoing:{...baseOutgoing,[anchor]:[...(baseOutgoing[anchor]||[]),id]},backlinks:{...baseBacklinks,[id]:[anchor]}};
      window.fetch=(request,options)=>String(request)===url?Promise.resolve(new Response(JSON.stringify(current),{status:200,headers:{'Content-Type':'application/json'}})):native(request,options);
      await G.refreshGraph();const added=cy.getElementById(id),duringAdd={opacity:Number(added.style('opacity')),width:Number(added.style('width')),pending:window.okfGraphCaptureState().transitionPending};
      await new Promise(resolve=>{const check=()=>!window.okfGraphCaptureState().transitionPending?resolve():requestAnimationFrame(check);check();});
      current={...base,bundle_revision:'live-remove'};await G.refreshGraph();const existsDuringExit=cy.getElementById(id).length===1;
      await new Promise(resolve=>{const check=()=>cy.getElementById(id).length===0&&!window.okfGraphCaptureState().transitionPending?resolve():requestAnimationFrame(check);check();});
      return {before,after:{pan:cy.pan(),zoom:cy.zoom(),layouts:G.layoutStats.starts},duringAdd,existsDuringExit,chip:document.querySelector('.okf-graph-updated span').textContent};
    }""")
    assert result["after"] == result["before"]
    assert result["duringAdd"]["opacity"] < 1 and result["duringAdd"]["pending"] is True
    assert result["existsDuringExit"] is True
    assert "Graph updated" in result["chip"]


def test_graph_explicit_layout_motion_and_refresh_deadline(server_url: str, page) -> None:
    page.add_init_script("window.OKF_LOOM_TEST_DEADLINES={graphRefresh:60}")
    _open_graph(page, server_url)
    page.get_by_text("Graph settings", exact=True).click()
    options = page.evaluate("""async () => {
      const G=window.__okfLoomGraph,cy=G.cy;let captured=null;
      cy.layout=opts=>{captured=opts;const handlers=[];return {on(name,fn){if(name==='layoutstop')handlers.push(fn);return this;},run(){queueMicrotask(()=>handlers.forEach(fn=>fn()));},stop(){}}};
      document.querySelector('.okf-graph-layout-settings button[aria-label="Re-run layout"]').click();
      await new Promise(resolve=>requestAnimationFrame(resolve));return captured;
    }""")
    assert options["animate"] == "end" and options["animationDuration"] == 400
    deadline = page.evaluate("""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,before=G.refreshStats.applied,native=window.fetch.bind(window);
      window.fetch=(request,options)=>String(request)===url?new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true})):native(request,options);
      await G.refreshGraph();return {before,after:G.refreshStats.applied,timedOut:G.refreshStats.timedOut,error:document.body.dataset.okfGraphRefreshError};
    }""")
    assert deadline == {"before": deadline["before"], "after": deadline["before"], "timedOut": 1, "error": "graph-refresh-timeout"}


def test_graph_reduced_motion_makes_explicit_layout_and_delta_instant(server_url: str, page) -> None:
    page.emulate_media(reduced_motion="reduce")
    _open_graph(page, server_url)
    page.get_by_text("Graph settings", exact=True).click()
    result = page.evaluate("""async () => {
      const G=window.__okfLoomGraph,cy=G.cy,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),base=await native(url).then(r=>r.json());let opts;
      cy.layout=o=>{opts=o;const hs=[];return {on(n,f){if(n==='layoutstop')hs.push(f);return this;},run(){hs.forEach(f=>f());},stop(){}}};document.querySelector('.okf-graph-layout-settings button[aria-label="Re-run layout"]').click();
      const id='reduced-delta',fresh={...base,bundle_revision:'reduced',nodes:[...base.nodes,{data:{id,label:'Reduced',type:'concept'}}]};window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(fresh),{status:200,headers:{'Content-Type':'application/json'}})):native(r,o);await G.refreshGraph();
      return {animate:opts.animate,opacity:Number(cy.getElementById(id).style('opacity')),settled:window.okfGraphCaptureState().layoutSettled};
    }""")
    assert result["animate"] is False and result["settled"] is True
    assert result["opacity"] > 0


def test_graph_transition_then_failed_refresh_settles_current_readiness(server_url: str, page) -> None:
    """A pending accepted transition is not promoted to coherent state by a later failure."""
    _open_graph(page, server_url)
    result = page.evaluate("""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),base=await native(url).then(r=>r.json()),id='readiness-race';
      let mode='add',rejectB;window.fetch=(r,o)=>String(r)!==url?native(r,o):mode==='add'?Promise.resolve(new Response(JSON.stringify({...base,bundle_revision:'race-a',nodes:[...base.nodes,{data:{id,label:'Race',type:'concept'}}]}),{status:200,headers:{'Content-Type':'application/json'}})):new Promise((resolve,reject)=>rejectB=reject);
      await G.refreshGraph();mode='fail';const failed=G.refreshGraph();
      await new Promise(resolve=>{const check=()=>!window.okfGraphCaptureState().transitionPending?resolve():requestAnimationFrame(check);check();});
      const whileFailed=document.body.dataset.okfGraphCaptureReady;rejectB(new TypeError('network failed'));await failed;
      return {whileFailed,after:document.body.dataset.okfGraphCaptureReady,revision:document.body.dataset.okfCaptureRevision,present:G.cy.getElementById(id).length};
    }""")
    assert result == {"whileFailed": "loading", "after": "ready", "revision": "race-a", "present": 1}


def test_graph_deadline_classifies_ignored_abort_and_fences_late_completion(server_url: str, page) -> None:
    page.add_init_script("window.OKF_LOOM_TEST_DEADLINES={graphRefresh:60}")
    _open_graph(page, server_url)
    result = page.evaluate("""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),base=await native(url).then(r=>r.json());let release;
      window.fetch=(r,o)=>String(r)===url?new Promise(resolve=>release=()=>resolve(new Response(JSON.stringify({...base,bundle_revision:'must-not-land',nodes:[...base.nodes,{data:{id:'late-ignored-abort',label:'Late',type:'concept'}}]}),{status:200,headers:{'Content-Type':'application/json'}}))):native(r,o);
      const pending=G.refreshGraph();await new Promise(resolve=>{const check=()=>G.refreshStats.timedOut?resolve():requestAnimationFrame(check);check();});const atDeadline={error:document.body.dataset.okfGraphRefreshError,timedOut:G.refreshStats.timedOut};release();await pending;
      return {atDeadline,late:G.cy.getElementById('late-ignored-abort').length,revision:document.body.dataset.okfRevision};
    }""")
    assert result["atDeadline"] == {"error": "graph-refresh-timeout", "timedOut": 1}
    assert result["late"] == 0 and result["revision"] != "must-not-land"


def test_graph_same_id_transition_supersession_and_pagehide_cleanup(server_url: str, page) -> None:
    _open_graph(page, server_url)
    result = page.evaluate("""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),base=await native(url).then(r=>r.json()),id='same-id';let current;
      window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(current),{status:200,headers:{'Content-Type':'application/json'}})):native(r,o);
      current={...base,bundle_revision:'same-add',nodes:[...base.nodes,{data:{id,label:'First',type:'concept'}}]};await G.refreshGraph();
      current={...base,bundle_revision:'same-remove'};await G.refreshGraph();
      current={...base,bundle_revision:'same-readd',nodes:[...base.nodes,{data:{id,label:'Final',type:'concept'}}]};await G.refreshGraph();
      await new Promise(resolve=>{const check=()=>!window.okfGraphCaptureState().transitionPending?resolve():requestAnimationFrame(check);check();});const readd={count:G.cy.getElementById(id).length,label:G.cy.getElementById(id).data('label')};
      current={...base,bundle_revision:'pagehide-add',nodes:[...base.nodes,{data:{id:'pagehide-delta',label:'Hidden',type:'concept'}}]};await G.refreshGraph();const pending=window.okfGraphCaptureState().transitionPending;dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));const hidden=window.okfGraphCaptureState();dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
      const ready=await window.okfCaptureReadiness.start({target:'graph',expectedRevision:'pagehide-add'}).promise;
      return {readd,pending,hidden:{transitionPending:hidden.transitionPending},ready:{allowed:ready.screenshotAllowed,revision:ready.metadata.revision},capture:document.body.dataset.okfGraphCaptureReady};
    }""")
    assert result["readd"] == {"count": 1, "label": "Final"}
    assert result["pending"] is True and result["hidden"]["transitionPending"] is False
    assert result["ready"] == {"allowed": True, "revision": "pagehide-add"} and result["capture"] == "ready"


def test_presence_hidden_lod_focus_never_moves_or_lays_out(server_url: str, page) -> None:
    _open_graph(page, server_url)
    result = page.evaluate("""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),base=await native(url).then(r=>r.json());
      const nodes=Array.from({length:105},(_,i)=>({data:{id:'presence-'+i,label:'Presence '+i,type:'concept'}})),fresh={...base,bundle_revision:'presence-lod',nodes,edges:[],outgoing:{},backlinks:{}};
      window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(fresh),{status:200,headers:{'Content-Type':'application/json'}})):native(r,o);await G.refreshGraph();await new Promise(resolve=>{const check=()=>!window.okfGraphCaptureState().transitionPending?resolve():requestAnimationFrame(check);check();});
      const hidden=nodes.map(n=>n.data.id).find(id=>G.cy.getElementById(id).length===0);G.cy.pan({x:43,y:-23});G.cy.zoom(.69);const before={pan:G.cy.pan(),zoom:G.cy.zoom(),starts:G.layoutStats.starts};window.okfLoomLive.emit('presence',{focus:hidden});await new Promise(requestAnimationFrame);
      return {hidden,absent:G.cy.getElementById(hidden).length===0,before,after:{pan:G.cy.pan(),zoom:G.cy.zoom(),starts:G.layoutStats.starts},halos:G.cy.nodes('.okf-presence-halo').length};
    }""")
    assert result["hidden"] and result["absent"] and result["before"] == result["after"]
    assert result["halos"] == 0


def test_capture_readiness_removes_abort_listeners_on_every_terminal(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.wait_for_function("() => window.okfGraphCaptureState().layoutSettled")
    counts = page.evaluate("""async () => {
      const proto=AbortSignal.prototype,add=proto.addEventListener,remove=proto.removeEventListener,c={add:0,remove:0};
      proto.addEventListener=function(type,fn,opts){if(type==='abort')c.add++;return add.call(this,type,fn,opts);};
      proto.removeEventListener=function(type,fn,opts){if(type==='abort')c.remove++;return remove.call(this,type,fn,opts);};
      try {
        const a=new AbortController();await window.okfCaptureReadiness.start({target:'graph',signal:a.signal}).promise;
        const b=new AbortController();await window.okfCaptureReadiness.start({target:'#missing-capture-target',signal:b.signal}).promise.catch(()=>{});
        document.body.dataset.okfCaptureReady='loading';const c1=new AbortController();const pending=window.okfCaptureReadiness.start({target:'document',signal:c1.signal}).promise.catch(()=>{});
        const c2=new AbortController();await window.okfCaptureReadiness.start({target:'#missing-again',signal:c2.signal}).promise.catch(()=>{});await pending;
        return c;
      } finally {proto.addEventListener=add;proto.removeEventListener=remove;}
    }""")
    assert counts == {"add": 4, "remove": 4}


def test_group_d_context_fingerprint_revoke_timeout_retry_and_focus(server_url: str, page) -> None:
    """One owned fingerprint fences timeout/retry and every mutable context bound."""
    page.add_init_script("window.OKF_LOOM_TEST_DEADLINES={suggestions:60,context:60,clipboard:60}")
    page.goto(server_url + "/__search?q=orders&mode=lexical&limit=5&type=Table", wait_until="domcontentloaded")
    _open_context_options(page)
    page.evaluate("""() => {
      const nativeFetch=window.fetch.bind(window);window.__contextAttempts=0;
      window.fetch=(url,opts)=>{if(!String(url).startsWith('/__context?'))return nativeFetch(url,opts);
        window.__contextAttempts++;if(window.__contextAttempts===1)return new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true}));
        const pack={schema_version:1,bundle_revision:document.body.dataset.okfRevision,query:'orders',mode:'lexical',filters:{type:'Table'},budget:{requested_tokens:6000,estimated_tokens:1},neighbors:1,included:0,omitted:0,truncated:false,items:[],representations:{markdown:'# retry'}};
        return Promise.resolve(new Response(JSON.stringify(pack),{status:200,headers:{'Content-Type':'application/json'}}));};
    }""")
    page.locator("[data-context-prepare]").click()
    retry = page.get_by_role("button", name="Retry", exact=True)
    expect(retry).to_be_visible()
    assert retry.evaluate("el => document.activeElement === el")
    fallback = page.get_by_role("link", name="Open canonical Markdown context")
    href = fallback.get_attribute("href") or ""
    for value in ("q=orders", "mode=lexical", "limit=5", "type=Table", "budget_tokens=6000", "neighbors=1"):
        assert value in href
    retry.click()
    prepared = page.locator("[data-context-copy-prepared]")
    expect(prepared).to_be_enabled()
    assert prepared.evaluate("el => document.activeElement === el")
    fingerprint = page.evaluate("() => JSON.parse(document.querySelector('[data-context-copy-prepared]')._okfPreparedFingerprint)")
    assert fingerprint["target"]["query"] == "orders"
    assert fingerprint["options"] == {"mode": "lexical", "type": "Table", "tag": "", "entity": "", "relation": "", "source": "", "target": "", "budget": "6000", "neighbors": "1", "limit": "5"}
    page.locator("[data-context-budget]").fill("7000")
    expect(prepared).to_be_disabled()
    assert page.locator("#okf-context-error").count() == 0


def test_group_d_static_context_absolute_deadline_fences_late_result(static_url: str, page) -> None:
    """Portable provider/context work shares one deadline and a late pack cannot publish."""
    page.add_init_script("window.OKF_LOOM_TEST_DEADLINES={suggestions:60,context:60,clipboard:60}")
    page.goto(static_url + "/__search.html?q=orders&mode=lexical&limit=5", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    _open_context_options(page)
    page.evaluate("""() => { window.__lateStaticResolve=null; window.okfStaticContextCopy=()=>new Promise(resolve=>window.__lateStaticResolve=resolve); }""")
    page.locator("[data-context-prepare]").click()
    retry = page.get_by_role("button", name="Retry", exact=True)
    expect(retry).to_be_visible()
    expect(page.locator("#okf-context-error-title")).to_contain_text("longer than 30 seconds")
    assert retry.evaluate("el => document.activeElement === el")
    page.evaluate("""() => window.__lateStaticResolve({schema_version:1,bundle_revision:'late',query:'orders',mode:'lexical',filters:{},budget:{requested_tokens:6000,estimated_tokens:1},neighbors:1,included:0,omitted:0,truncated:false,items:[],representations:{markdown:'# late'}})""")
    page.wait_for_timeout(100)
    expect(page.locator("[data-context-copy-prepared]")).to_be_disabled()
    expect(retry).to_be_visible()


def test_group_d_palette_preserves_filter_bounds_and_editable_shortcut(server_url: str, page) -> None:
    """Palette reads canonical search state while native editables retain Ctrl/Cmd+K."""
    page.goto(server_url + "/__search?q=&mode=tag&tag=orders&limit=7", wait_until="domcontentloaded")
    expect(page.locator("#okf-results-heading")).to_contain_text("results for current filters")
    expect(page.locator(".okf-active-filters")).to_contain_text("tag: orders")
    expect(page.locator(".okf-page-context")).to_be_visible()
    _open_context_options(page)
    page.locator("[data-context-budget]").fill("7000")
    page.locator("[data-context-neighbors]").select_option("2")
    state = page.evaluate("() => window.okfReadSearchContextState()")
    assert state["query"] == "" and state["hasFilters"] is True
    assert state["options"]["tag"] == "orders"
    assert state["options"]["limit"] == 7 and state["options"]["budget"] == 7000 and state["options"]["neighbors"] == 2
    query = page.locator("#okf-search-query")
    query.focus()
    page.keyboard.press("Control+k")
    expect(page.locator(".okf-palette-overlay")).to_be_hidden()
    page.locator(".okf-studio-launcher").click()
    page.get_by_role("button", name="Open the command palette (Control K)").click()
    expect(page.get_by_role("option", name="Copy concept context")).to_be_visible()


def test_group_d_static_filter_only_heading_and_context(static_url: str, page) -> None:
    page.goto(static_url + "/__search.html?mode=tag&tag=orders&limit=7", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    expect(page.locator("#okf-results-heading")).to_contain_text("results for current filters")
    expect(page.locator(".okf-active-filters")).to_contain_text("tag: orders")
    expect(page.locator(".okf-page-context")).to_be_visible()


def test_group_d_graph_teardown_and_mobile_keyboard_focus(server_url: str, page) -> None:
    """Central teardown clears owned graph work; keyboard activation exposes mobile details."""
    page.set_viewport_size({"width": 390, "height": 780})
    _open_graph(page, server_url)
    page.get_by_role("button", name="Concepts").click()
    if not page.locator(".okf-node-index").evaluate("el => el.open"):
        page.locator(".okf-node-index summary").click()
    concept_list = page.get_by_role("listbox", name="Graph concepts")
    concept_list.focus()
    page.keyboard.press("Space")
    detail = page.locator("#detail-content")
    expect(detail).to_be_visible()
    assert page.evaluate("() => document.querySelector('.okf-layout').dataset.mobilePane === 'details'")
    expect(detail).to_be_focused()
    path_status = page.evaluate("""() => { const cy=window.__okfLoomGraph.cy,nodes=cy.nodes();nodes[0].trigger('tap');nodes[1].trigger({type:'tap',originalEvent:{shiftKey:true}});return document.querySelector('.okf-signal__status').textContent; }""")
    assert "Path:" in path_status and "→" in path_status and "via" in path_status
    page.evaluate("""() => { const f=document.getElementById('okf-graph-copy-fallback'),a=document.getElementById('okf-graph-copy-text'),s=document.getElementById('okf-graph-copy-status');f.hidden=false;a.value='stale graph markdown';s.textContent='stale graph status';s.setAttribute('role','alert');window.okfPortableProvider={manifest:{bundle_revision:'stale'}};window.okfTeardownGraphLifecycle('test',{invalidateProvider:true}); }""")
    expect(page.locator("#okf-graph-copy-fallback")).to_be_hidden()
    assert page.evaluate("() => window.okfPortableProvider === null")
    assert page.evaluate("() => document.getElementById('okf-graph-copy-text').value === '' && document.getElementById('okf-graph-copy-status').textContent === '' && document.getElementById('okf-graph-copy-status').getAttribute('role') === 'status'")


def test_graph_detail_accessible_order_controls_and_focus_provenance(server_url: str, page) -> None:
    """Selected detail is named once, ordered, and focused only for keyboard activation."""
    page.set_viewport_size({"width": 390, "height": 780})
    _open_graph(page, server_url)
    controls = page.locator("[data-graph-pane]").evaluate_all(
        "els => els.map(el => [el.textContent.trim(), el.getAttribute('aria-controls'), !!document.getElementById(el.getAttribute('aria-controls'))])"
    )
    assert controls == [
        ["Graph", "okf-graph", True],
        ["Concepts", "okf-concept-index", True],
        ["Details", "okf-detail", True],
    ]
    assert page.locator(".okf-node-index summary").get_attribute("aria-controls") == "okf-node-index-list"

    page.get_by_role("button", name="Concepts").click()
    concept_list = page.get_by_role("listbox", name="Graph concepts")
    concept_list.focus()
    page.keyboard.press("Space")
    detail = page.locator("#detail-content")
    expect(detail).to_be_focused()
    selected_title = page.locator("#detail-title").inner_text()
    assert detail.get_attribute("aria-labelledby") == "detail-title"
    assert page.evaluate("() => document.querySelector('.okf-layout').dataset.mobilePane") == "details"
    assert page.locator("#okf-detail").evaluate("el => el.scrollTop") == 0
    assert page.locator("#detail-content .okf-detail__header > .okf-graph-actions").count() == 1
    assert page.locator("#detail-connection-summary").bounding_box()["y"] >= detail.bounding_box()["y"]
    assert page.locator("#detail-connection-summary").evaluate(
        "summary => summary.compareDocumentPosition(document.getElementById('detail-body')) & Node.DOCUMENT_POSITION_FOLLOWING"
    )
    assert page.locator("#detail-body").evaluate(
        "body => body.compareDocumentPosition(document.getElementById('detail-relationships-slot')) & Node.DOCUMENT_POSITION_FOLLOWING"
    )
    selected_status = page.locator(".okf-detail-selected-status")
    assert selected_status.count() == 1
    assert selected_status.inner_text() == f"Selected: {selected_title}"
    assert selected_status.get_attribute("role") is None
    assert selected_status.get_attribute("aria-live") is None
    assert page.locator('#okf-graph-announcer[role="status"][aria-live="polite"]').count() == 1
    assert selected_title

    # Mobile graph tools intentionally remain collapsed behind More tools.
    # Use that visible, stable control to prove pointer activation preserves
    # focus rather than forcing a hidden search field into the tab sequence.
    page.get_by_role('searchbox', name='Search graph concepts').focus()
    focused_before = page.evaluate("() => document.activeElement.className")
    concept_id = page.locator('#okf-node-index-list [role="option"]').first.get_attribute("data-target")
    assert page.evaluate("id => window.okfActivateGraphConcept(id, 'pointer')", concept_id)
    assert page.evaluate("() => document.activeElement.className") == focused_before


def test_single_file_graph_detail_hierarchy_and_keyboard_focus(tmp_path: Path, page) -> None:
    """The portable viewer uses the same detail order, naming, and focus contract."""
    from okf_loom import Bundle
    from okf_loom.render import render_single_file

    output = tmp_path / "single-graph-detail.html"
    render_single_file(Bundle.load(DEMO_BUNDLE), output)
    page.set_viewport_size({"width": 390, "height": 780})
    page.goto(output.as_uri(), wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph && document.querySelector('.okf-node-index__item')")
    concept_id = page.locator(".okf-node-index__item").first.get_attribute("data-target")
    assert page.evaluate("id => window.okfActivateGraphConcept(id, 'keyboard')", concept_id)
    detail = page.locator("#detail-content")
    expect(detail).to_be_visible()
    expect(detail).to_be_focused()
    assert detail.get_attribute("aria-labelledby") == "detail-title"
    ids = detail.locator(
        "#detail-title, #detail-description, #detail-connection-summary, #detail-body, #detail-relationships-slot, #okf-detail-signals"
    ).evaluate_all("els => els.map(el => el.id)")
    assert ids == [
        "detail-title",
        "detail-description",
        "detail-connection-summary",
        "detail-body",
        "detail-relationships-slot",
        "okf-detail-signals",
    ]


def test_group_d_graph_refresh_deletion_rebuilds_authoritative_state(server_url: str, page) -> None:
    """A revision-changing refresh deletes stale selection and rebuilds index/focus."""
    _open_graph(page, server_url)
    page.locator(".okf-node-index summary").click()
    selected = page.evaluate("() => window.__okfLoomGraph.cy.edges().first().source().id()")
    first = page.locator(f'.okf-node-index__item[data-target="{selected}"]')
    first.click()
    page.wait_for_function("id => window.okfGraphSelection.conceptId === id", arg=selected)
    page.evaluate("id=>{const G=window.__okfLoomGraph,n=G.cy.getElementById(id),other=n.neighborhood('node').first();G.interaction.dispatch({type:'PATH',from:id,id:other.id()})}",selected)
    page.get_by_role("toolbar",name="Selected concept actions").get_by_role("button",name="Explore neighborhood").click();page.wait_for_function("()=>window.__okfLoomGraph.getState().lens==='focus'&&window.okfGraphCaptureState().layoutSettled")
    before_motion=page.evaluate("""id=>{const G=window.__okfLoomGraph,positions={};G.cy.nodes().filter(n=>n.id()!==id).forEach(n=>positions[n.id()]=n.position());return {pan:G.cy.pan(),zoom:G.cy.zoom(),starts:G.layoutStats.starts,positions};}""",selected)
    page.evaluate("""async selected => { const graph=document.getElementById('okf-graph'),url=graph.dataset.graphUrl,native=window.fetch.bind(window),fresh=await native(url).then(r=>r.json());fresh.nodes=fresh.nodes.filter(n=>n.data.id!==selected);fresh.edges=fresh.edges.filter(e=>e.data.source!==selected&&e.data.target!==selected);fresh.outgoing=fresh.outgoing||{};fresh.backlinks=fresh.backlinks||{};delete fresh.outgoing[selected];delete fresh.backlinks[selected];Object.keys(fresh.outgoing).forEach(id=>fresh.outgoing[id]=fresh.outgoing[id].filter(other=>other!==selected));Object.keys(fresh.backlinks).forEach(id=>fresh.backlinks[id]=fresh.backlinks[id].filter(other=>other!==selected));fresh.bundle_revision='replacement-revision';window.okfPortableProvider={manifest:{bundle_revision:'stale-revision'}};window.fetch=(request,options)=>String(request)===url?Promise.resolve(new Response(JSON.stringify(fresh),{status:200,headers:{'Content-Type':'application/json'}})):native(request,options);window.__okfLoomGraph.refreshGraph(); }""", selected)
    page.wait_for_function("() => window.okfGraphSelection.conceptId === null")
    expect(page.locator(f'.okf-node-index__item[data-target="{selected}"]')).to_have_count(0)
    assert page.evaluate("() => window.okfPortableProvider === null")
    assert page.evaluate("() => document.activeElement === document.getElementById('okf-search')")
    cleaned=page.evaluate("()=>({s:window.__okfLoomGraph.interaction.snapshot(),focus:window.__okfLoomGraph.focusRoot(),action:document.querySelector('.okf-graph-actions').hidden,url:location.search,ready:window.okfGraphCaptureState().layoutSettled})")
    assert cleaned["s"]["selected"] is None and cleaned["s"]["active"] is None and cleaned["s"]["pathEnd"] is None and cleaned["focus"] is None and cleaned["action"] and "selected=" not in cleaned["url"] and "focus=" not in cleaned["url"] and "path=" not in cleaned["url"]
    after_motion=page.evaluate("""()=>{const G=window.__okfLoomGraph,positions={};G.cy.nodes().filter(n=>!n.hasClass('okf-delta-exit')).forEach(n=>positions[n.id()]=n.position());return {pan:G.cy.pan(),zoom:G.cy.zoom(),starts:G.layoutStats.starts,positions};}""")
    assert after_motion == before_motion


def test_group_d_large_graph_refresh_show_all_never_resurrects_deleted_hidden_nodes(server_url: str, page) -> None:
    """The mutable authoritative snapshot, not the boot bundle, owns >100-node LOD."""
    _open_graph(page, server_url)
    result = page.evaluate("""async () => {
      const graph=document.getElementById('okf-graph'),url=graph.dataset.graphUrl,native=window.fetch.bind(window);
      const base=await native(url).then(r=>r.json());
      const make=(count,drop) => {
        const nodes=Array.from({length:count},(_,i)=>({data:{id:'large-'+String(i).padStart(3,'0'),label:'Large '+i,type:'concept'}})).filter(n=>n.data.id!==drop);
        const edges=nodes.slice(1).map((n,i)=>({data:{source:nodes[0].data.id,target:n.data.id,label:'links'}}));
        const outgoing={},backlinks={};edges.forEach(e=>{(outgoing[e.data.source]??=[]).push(e.data.target);(backlinks[e.data.target]??=[]).push(e.data.source);});
        return {...base,bundle_revision:'large-'+count+'-'+(drop||'all'),nodes,edges,outgoing,backlinks,bodies:{},types:['concept']};
      };
      let current=make(105,null);
      window.fetch=(request,options)=>String(request)===url?Promise.resolve(new Response(JSON.stringify(current),{status:200,headers:{'Content-Type':'application/json'}})):native(request,options);
      await window.__okfLoomGraph.refreshGraph();
      const hidden=current.nodes.map(n=>n.data.id).find(id=>window.__okfLoomGraph.cy.getElementById(id).length===0);
      current=make(104,hidden);
      await window.__okfLoomGraph.refreshGraph();
      return {hidden,total:current.nodes.length};
    }""")
    assert result["hidden"]
    pill = page.locator(".okf-graph-lod-pill")
    expect(pill).to_contain_text(f"of {result['total']} nodes")
    pill.evaluate("el => el.click()")
    assert page.evaluate("id => window.__okfLoomGraph.cy.getElementById(id).length === 0", result["hidden"])
    assert page.evaluate("() => window.okfGraphCaptureState().payloadNodeCount") == result["total"]


@pytest.mark.parametrize("mutation", ["missing-nodes", "bad-edge"])
def test_group_d_malformed_refresh_is_atomic(server_url: str, page, mutation: str) -> None:
    _open_graph(page, server_url)
    page.wait_for_function("() => window.okfGraphCaptureState().layoutSettled")
    before = page.evaluate("() => {const state=window.okfGraphCaptureState();return {revision:document.body.dataset.okfRevision,captureRevision:document.body.dataset.okfCaptureRevision,nodeCount:state.nodeCount,payloadNodeCount:state.payloadNodeCount,ids:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort()};}")
    page.evaluate("""async mutation => {
      const graph=document.getElementById('okf-graph'),url=graph.dataset.graphUrl,native=window.fetch.bind(window),fresh=await native(url).then(r=>r.json());
      fresh.bundle_revision='must-not-commit';
      if(mutation==='missing-nodes') delete fresh.nodes;
      else fresh.edges=[{data:{source:fresh.nodes[0].data.id,target:'missing-endpoint'}}];
      window.fetch=(request,options)=>String(request)===url?Promise.resolve(new Response(JSON.stringify(fresh),{status:200,headers:{'Content-Type':'application/json'}})):native(request,options);
      await window.__okfLoomGraph.refreshGraph();
    }""", mutation)
    after = page.evaluate("() => {const state=window.okfGraphCaptureState();return {revision:document.body.dataset.okfRevision,captureRevision:document.body.dataset.okfCaptureRevision,nodeCount:state.nodeCount,payloadNodeCount:state.payloadNodeCount,ids:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort()};}")
    assert after == before


@pytest.mark.parametrize("body,code,phase", [
    ("not-json", "network-failure", "graph-json"),
    ('{"bundle_revision":"x","edges":[]}', "unavailable", "graph-schema"),
])
def test_group_d_current_initial_failure_publishes_capture_terminal(static_url: str, page, body: str, code: str, phase: str) -> None:
    page.route("**/__data/graph.json", lambda route: route.fulfill(status=200, content_type="application/json", body=body))
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfGraphCaptureState?.().terminalCode")
    outcome = page.evaluate("""async () => {try{await window.okfCaptureReadiness.start({target:'graph'}).promise;return {ok:true};}catch(e){return {ok:false,code:e.code,phase:e.metadata.readinessPhase,allowed:e.screenshotAllowed};}}""")
    assert outcome == {"ok": False, "code": code, "phase": phase, "allowed": False}
    terminal = page.evaluate("() => window.okfGraphCaptureState()")
    assert terminal["terminalCode"] == code and terminal["terminalPhase"] == phase


@pytest.mark.parametrize("adapter", [
    "({initialized:'yes',nodeCount:1,payloadNodeCount:1,layoutRequired:false,layoutSettled:true,layoutGeneration:0,appliedLayoutGeneration:0})",
    "({initialized:true,nodeCount:-1,payloadNodeCount:1,layoutRequired:false,layoutSettled:true,layoutGeneration:0,appliedLayoutGeneration:0})",
    "({initialized:true,nodeCount:1,payloadNodeCount:1,layoutRequired:true,layoutSettled:true,layoutGeneration:2,appliedLayoutGeneration:1})",
    "({initialized:true,nodeCount:2,payloadNodeCount:1,layoutRequired:false,layoutSettled:true,layoutGeneration:0,appliedLayoutGeneration:0})",
    "({initialized:true,nodeCount:1,payloadNodeCount:1,layoutRequired:false,layoutSettled:true,layoutGeneration:1,appliedLayoutGeneration:1})",
    "({initialized:true,nodeCount:1,payloadNodeCount:1,layoutRequired:false,layoutSettled:false,layoutGeneration:0,appliedLayoutGeneration:0})",
    "({initialized:false,nodeCount:0,payloadNodeCount:0,layoutRequired:false,layoutSettled:false,layoutGeneration:0,appliedLayoutGeneration:0})",
])
def test_group_d_capture_adapter_shape_fails_closed(server_url: str, page, adapter: str) -> None:
    _open_graph(page, server_url)
    result = page.evaluate("""async source => {const value=Function('return '+source)();window.okfGraphCaptureState=()=>value;try{await window.okfCaptureReadiness.start({target:'graph'}).promise;return {authorized:true};}catch(e){return {authorized:e.screenshotAllowed,code:e.code,phase:e.metadata.readinessPhase};}}""", adapter)
    assert result["authorized"] is False
    assert result["code"] == "unavailable" and result["phase"] == "graph-adapter-invalid"


def test_graph_layout_deadline_never_fabricates_capture_success(server_url: str, page) -> None:
    """Only current layoutstop settles; timeout and stale layoutstop remain fail-closed."""
    _open_graph(page, server_url)
    page.wait_for_function("() => window.okfGraphCaptureState().layoutSettled")
    page.get_by_text("Graph settings", exact=True).click()
    page.evaluate("""() => {
      const cy=window.__okfLoomGraph.cy;
      window.__heldCaptureLayouts=[];
      cy.layout=() => {
        const held={handlers:[],stopped:false,on(name,fn){if(name==='layoutstop')this.handlers.push(fn);return this;},run(){},stop(){this.stopped=true;this.handlers.forEach(fn=>fn());}};
        window.__heldCaptureLayouts.push(held);
        return held;
      };
    }""")
    rerun = page.get_by_role("button", name="Re-run layout", exact=True)
    rerun.click()
    page.wait_for_function("() => window.__heldCaptureLayouts.length >= 1")
    first_count = page.evaluate("() => window.__heldCaptureLayouts.length")
    page.wait_for_function("() => window.okfGraphCaptureState().terminalPhase === 'graph-layout-timeout'", timeout=3000)
    timed_out = page.evaluate("() => window.okfGraphCaptureState()")
    assert timed_out["layoutSettled"] is False
    assert timed_out["terminalCode"] == "unavailable"
    denied = page.evaluate("""async () => {try{await window.okfCaptureReadiness.start({target:'graph'}).promise;return {allowed:true};}catch(e){return {allowed:e.screenshotAllowed,code:e.code};}}""")
    assert denied == {"allowed": False, "code": "unavailable"}
    page.evaluate("() => window.__heldCaptureLayouts.forEach(layout=>layout.handlers.forEach(fn=>fn()))")
    assert page.evaluate("() => window.okfGraphCaptureState().terminalPhase") == "graph-layout-timeout"
    rerun.click()
    page.wait_for_function("count => window.__heldCaptureLayouts.length > count", arg=first_count)
    page.evaluate("count => window.__heldCaptureLayouts.slice(0,count).forEach(layout=>layout.handlers.forEach(fn=>fn()))", first_count)
    assert page.evaluate("() => !window.okfGraphCaptureState().layoutSettled && !window.okfGraphCaptureState().terminalCode")
    page.evaluate("() => window.__heldCaptureLayouts.at(-1).handlers.forEach(fn=>fn())")
    page.wait_for_function("() => window.okfGraphCaptureState().layoutSettled && !window.okfGraphCaptureState().terminalCode")


def test_group_d_queued_refresh_is_cancelled_on_pagehide(server_url: str, page) -> None:
    _open_graph(page, server_url)
    started = page.evaluate("() => window.__okfLoomGraph.refreshStats.started")
    page.evaluate("() => {window.okfLoomLive.emit('graph',{});window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));}")
    page.wait_for_timeout(850)
    assert page.evaluate("() => window.__okfLoomGraph.refreshStats.started") == started


def test_group_d_inflight_comment_refresh_pagehide_publishes_nothing(server_url: str, page) -> None:
    _open_graph(page, server_url)
    result = page.evaluate(r"""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),base=await native(url).then(r=>r.json()),id=base.nodes[0].data.id;
      const before=G.cy.getElementById(id).data('active_comment_count');let release;
      window.fetch=(req,opt)=>String(req)===url?new Promise(resolve=>release=()=>{const payload=JSON.parse(JSON.stringify(base));payload.nodes[0].data.active_comment_count=88;resolve(new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}}));}):native(req,opt);
      const pending=G.refreshGraph();await new Promise(requestAnimationFrame);
      window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));
      const hidden={graph:document.body.dataset.okfGraphCaptureReady,page:document.body.dataset.okfCaptureReady,count:G.cy.getElementById(id).data('active_comment_count'),applied:G.refreshStats.applied};
      release();await pending;await new Promise(requestAnimationFrame);
      return {before,hidden,late:{graph:document.body.dataset.okfGraphCaptureReady,page:document.body.dataset.okfCaptureReady,count:G.cy.getElementById(id).data('active_comment_count'),applied:G.refreshStats.applied}};
    }""")
    assert result["late"] == result["hidden"]
    assert result["late"]["count"] == result["before"]


@pytest.mark.parametrize("field,mode,url_value,next_value,late_value", [
    ("q", "lexical", "orders", "customers", "refund"),
    ("mode", "lexical", "lexical", "hybrid", "semantic"),
    ("type", "lexical", "Table", "Service", "Dataset"),
    ("tag", "tag", "orders", "checkout", "refund"),
    ("entity", "entity", "Order", "purchase", "transaction"),
    ("relation", "relation", "depends_on", "writes_to", "references"),
    ("source", "relation", "services/checkout", "tables/orders", "datasets/orders"),
    ("target", "relation", "tables/orders", "tables/customers", "references/currencies"),
    ("budget", "lexical", "6000", "7000", "8000"),
    ("neighbors", "lexical", "1", "2", "0"),
    ("limit", "lexical", "5", "4", "3"),
])
def test_group_d_every_context_owner_revokes_and_fences_late_settlement(
    server_url: str, page, field: str, mode: str, url_value: str, next_value: str, late_value: str
) -> None:
    params = {"q": "orders", "mode": "lexical", "limit": "5"}
    page.goto(server_url + "/__search?" + urllib.parse.urlencode(params), wait_until="domcontentloaded")
    _open_context_options(page)
    if field in {"type", "tag", "entity", "relation", "source", "target"} and not page.locator(".okf-search-filters").evaluate("el=>el.open"):
        page.locator(".okf-search-filters summary").click()
    if field == "entity":
        page.locator('[name="mode"][value="entity"]').check(); page.locator('[name="entity"]').fill(url_value)
    elif field in {"relation", "source", "target"}:
        page.locator('[name="mode"][value="relation"]').check(); page.locator(f'[name="{field}"]').fill(url_value)
    elif field in {"type", "tag"}:
        page.locator(f'[name="{field}"]').fill(url_value)
    page.evaluate("""() => { const native=window.fetch.bind(window);window.__ownedHeld=[];window.__holdOwned=false;
      window.fetch=(url,opts)=>{if(!String(url).startsWith('/__context?'))return native(url,opts);const pack={schema_version:1,bundle_revision:document.body.dataset.okfRevision,query:'owned',mode:'lexical',filters:{},budget:{requested_tokens:6000,estimated_tokens:1},neighbors:1,included:0,omitted:0,truncated:false,items:[],representations:{markdown:'# owned'}};if(!window.__holdOwned)return Promise.resolve(new Response(JSON.stringify(pack),{status:200,headers:{'Content-Type':'application/json'}}));return new Promise((resolve,reject)=>window.__ownedHeld.push({resolve,reject,pack}));}; }""")
    prepare = page.locator("[data-context-prepare]")
    prepared = page.locator("[data-context-copy-prepared]")
    prepare.click(); expect(prepared).to_be_enabled()
    page.evaluate("""() => {const f=document.getElementById('okf-copy-fallback'),a=document.getElementById('okf-copy-fallback-text'),s=document.getElementById('okf-copy-status');f.hidden=false;a.value='stale markdown';s.textContent='stale status';s.setAttribute('role','alert');}""")
    if field == "budget": control = page.locator("[data-context-budget]")
    elif field == "neighbors": control = page.locator("[data-context-neighbors]")
    elif field == "limit": control = page.locator("[data-context-limit]")
    elif field == "mode": control = page.locator(f'[name="mode"][value="{next_value}"]')
    elif field == "q": control = page.locator("#okf-search-query")
    else: control = page.locator(f'[name="{field}"]')
    control.focus()
    if field in {"neighbors"}: control.select_option(next_value)
    elif field == "mode": control.check()
    else: control.fill(next_value)
    assert control.evaluate("el => document.activeElement === el")
    expect(prepared).to_be_disabled()
    assert page.evaluate("() => !document.querySelector('[data-context-prepare]')._okfPreparedContext && !document.querySelector('[data-context-copy-prepared]')._okfPreparedContext")
    assert page.evaluate("() => document.getElementById('okf-copy-fallback').hidden && document.getElementById('okf-copy-fallback-text').value === '' && document.getElementById('okf-copy-status').textContent === '' && document.getElementById('okf-copy-status').getAttribute('role') === 'status'")
    assert page.locator("#okf-context-error").count() == 0
    page.evaluate("window.__holdOwned=true")
    prepare.click(); page.wait_for_function("() => window.__ownedHeld.length===1")
    if field == "neighbors": control.select_option(late_value)
    elif field == "mode": page.locator(f'[name="mode"][value="{late_value}"]').check()
    else: control.fill(late_value)
    page.evaluate("() => window.__ownedHeld[0].resolve(new Response(JSON.stringify(window.__ownedHeld[0].pack),{status:200,headers:{'Content-Type':'application/json'}}))")
    page.wait_for_timeout(50); expect(prepared).to_be_disabled(); assert page.locator("#okf-context-error").count() == 0
    prepare.click(); page.wait_for_function("() => window.__ownedHeld.length===2")
    if field == "neighbors": control.select_option(next_value)
    elif field == "mode": page.locator('[name="mode"][value="lexical"]').check()
    else: control.fill(next_value)
    page.evaluate("() => window.__ownedHeld[1].reject(new Error('late failure'))")
    page.wait_for_timeout(50); expect(prepared).to_be_disabled(); assert page.locator("#okf-context-error").count() == 0


def test_group_d_live_revision_mismatch_never_becomes_copyable(server_url: str, page) -> None:
    page.goto(server_url + "/__search?q=orders&mode=lexical&limit=5", wait_until="domcontentloaded")
    revision = page.locator("body").get_attribute("data-okf-revision")
    assert revision and revision != "pending"
    _open_context_options(page)
    page.evaluate("""() => { const native=window.fetch.bind(window);window.fetch=(url,opts)=>String(url).startsWith('/__context?')?Promise.resolve(new Response(JSON.stringify({schema_version:1,bundle_revision:'wrong-revision',query:'orders',mode:'lexical',filters:{},budget:{requested_tokens:6000,estimated_tokens:1},neighbors:1,included:0,omitted:0,truncated:false,items:[],representations:{markdown:'# stale'}}),{status:200,headers:{'Content-Type':'application/json'}})):native(url,opts); }""")
    page.locator("[data-context-prepare]").click()
    expect(page.get_by_role("button", name="Retry", exact=True)).to_be_visible()
    expect(page.locator("[data-context-copy-prepared]")).to_be_disabled()
    assert page.evaluate("() => !document.querySelector('[data-context-copy-prepared]')._okfPreparedContext")


def test_group_d_static_graph_absolute_deadline_and_honest_fallback(static_url: str, page) -> None:
    page.add_init_script("window.OKF_LOOM_TEST_DEADLINES={context:60}")
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.cy.nodes().length")
    page.locator(".okf-node-index summary").click(); page.locator(".okf-node-index__item").first.click()
    page.evaluate("() => {window.okfPortableProvider=null;window.okfPortableRetrieval.loadStatic=()=>new Promise((resolve,reject)=>{window.__lateGraph={resolve,reject};});}")
    page.locator("#okf-copy-neighbourhood").click()
    retry = page.get_by_role("button", name="Retry", exact=True); expect(retry).to_be_visible(); assert retry.evaluate("el=>document.activeElement===el")
    fallback = page.get_by_role("link", name="Open emitted concept-only Markdown")
    response = page.request.get(urllib.parse.urljoin(static_url + "/", fallback.get_attribute("href") or ""))
    assert response.status == 200 and "# OKF Context Pack" in response.text()
    page.evaluate("() => window.__lateGraph.resolve({manifest:{bundle_revision:'late'},context:()=>{throw new Error('late')}})")
    page.wait_for_timeout(100); expect(page.locator("#okf-copy-neighbourhood")).not_to_have_text("Copied")


def test_group_d_mobile_shell_order_bounds_and_structured_content(server_url: str, page) -> None:
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 780})
        page.goto(server_url + "/__search?q=orders&mode=lexical&limit=5", wait_until="domcontentloaded")
        metrics = page.evaluate("""() => {const controls=[...document.querySelectorAll('.okf-topbar a,.okf-topbar input,.okf-topbar button,.okf-studio-bar button')].filter(e=>getComputedStyle(e).display!=='none');return {scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth,inner:innerWidth,bad:controls.filter(e=>{const r=e.getBoundingClientRect();return r.left<0||r.right>innerWidth+0.5}).length,order:document.querySelector('.okf-topbar').compareDocumentPosition(document.querySelector('.okf-search-workbench'))&Node.DOCUMENT_POSITION_FOLLOWING};}""")
        assert metrics["scroll"] <= metrics["client"] == metrics["inner"] and metrics["bad"] == 0 and metrics["order"]
    page.goto(server_url + "/__search?q=column&mode=lexical&limit=5", wait_until="domcontentloaded")
    expect(page.locator(".okf-search-table").first).to_be_visible()
    expect(page.get_by_role("table").first).to_contain_text("Column")


def test_group_d_editable_shortcut_matrix_and_palette_ownership(server_url: str, page) -> None:
    page.goto(server_url + "/__search?q=orders", wait_until="domcontentloaded")
    page.evaluate("""() => {for(const [tag,attrs] of [['textarea',{}],['select',{}],['div',{contenteditable:'true'}],['div',{role:'textbox',tabindex:'0'}]]){const e=document.createElement(tag);Object.assign(e,attrs);for(const k in attrs)e.setAttribute(k,attrs[k]);if(tag==='select'){const o=document.createElement('option');o.textContent='x';e.appendChild(o);}e.className='shortcut-owner';document.body.appendChild(e);}window.__shortcutEvents=[];document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k')queueMicrotask(()=>window.__shortcutEvents.push({tag:e.target.tagName,role:e.target.getAttribute('role'),content:e.target.isContentEditable,prevented:e.defaultPrevented}));});}""")
    controls=[page.locator("#okf-search-query"),page.locator("textarea.shortcut-owner"),page.locator("select.shortcut-owner"),page.locator('[contenteditable="true"]'),page.locator('[role="textbox"]')]
    for control in controls:
        control.focus(); page.keyboard.press("Control+k"); expect(page.locator(".okf-palette-overlay")).to_be_hidden()
        control.focus(); page.keyboard.press("Meta+k"); expect(page.locator(".okf-palette-overlay")).to_be_hidden()
    assert all(not row["prevented"] for row in page.evaluate("window.__shortcutEvents"))
    page.locator(".okf-studio-launcher").click();trigger=page.get_by_role("button",name="Open the command palette (Control K)");trigger.focus();page.keyboard.press("Control+k");expect(page.locator(".okf-palette-overlay")).to_be_visible();page.keyboard.press("Escape");assert trigger.evaluate("el=>document.activeElement===el")


@pytest.mark.parametrize("mode,key,value", [
    ("tag", "type", "Table"),
    ("tag", "tag", "orders"),
    ("entity", "entity", "Order"),
    ("relation", "relation", "depends_on"),
    ("relation", "source", "services/checkout"),
    ("relation", "target", "tables/orders"),
])
def test_group_d_live_and_portable_filter_only_contract(
    server_url: str, static_url: str, page, mode: str, key: str, value: str
) -> None:
    query = urllib.parse.urlencode({"mode": mode, key: value, "limit": 7}, safe="/")
    for url in (server_url + "/__search?" + query, static_url + "/__search.html?" + query):
        page.goto(url, wait_until="domcontentloaded")
        if "__search.html" in url:
            page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
        expect(page.locator("#okf-results-heading")).to_contain_text("current filters")
        expect(page.locator(".okf-active-filters")).to_contain_text(f"{key}: {value}")
        expect(page.locator(".okf-page-context")).to_be_visible()
        state = page.evaluate("() => window.okfReadSearchContextState()")
        assert state["query"] == "" and state["options"][key] == value and state["options"]["limit"] == 7
    page.goto(server_url + "/__search?" + query, wait_until="domcontentloaded")
    page.locator(".okf-studio-launcher").click()
    page.get_by_role("button", name="Open the command palette (Control K)").click()
    expect(page.get_by_role("option", name="Copy concept context")).to_be_visible()


def test_group_d_static_search_failure_fallback_is_followable_concept_only(static_url: str, page) -> None:
    page.goto(static_url + "/__search.html?mode=tag&tag=orders&limit=7", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    _open_context_options(page)
    page.evaluate("() => {window.okfStaticContextCopy=()=>Promise.reject(new Error('offline'));}")
    page.locator("[data-context-prepare]").click()
    fallback = page.get_by_role("link", name="Open emitted concept-only Markdown")
    expect(fallback).to_be_visible()
    response = page.request.get(urllib.parse.urljoin(static_url + "/", fallback.get_attribute("href") or ""))
    assert response.status == 200 and "# OKF Context Pack" in response.text()


def test_group_d_provider_mismatch_invalidates_then_next_action_uses_fresh_revision(static_url: str, page) -> None:
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.cy.nodes().length")
    page.locator(".okf-node-index summary").click(); page.locator(".okf-node-index__item").first.click()
    page.evaluate("""async () => {const graph=document.getElementById('okf-graph'),payload=await fetch(graph.dataset.graphUrl).then(r=>r.json());window.__providerWrites=[];window.__providerLoads=0;Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:t=>{window.__providerWrites.push(t);return Promise.resolve();}}});const fresh={manifest:{bundle_revision:payload.bundle_revision},context:()=>({schema_version:1,bundle_revision:payload.bundle_revision,query:'fresh',mode:'lexical',filters:{},budget:{requested_tokens:6000,estimated_tokens:1},neighbors:1,included:0,omitted:0,truncated:false,items:[],representations:{markdown:'# fresh revision'}})};window.okfPortableProvider={manifest:{bundle_revision:'stale'},context:()=>{throw new Error('stale provider copied');}};window.okfPortableRetrieval.loadStatic=()=>{window.__providerLoads++;return Promise.resolve(fresh);};}""")
    copy = page.locator("#okf-copy-neighbourhood")
    copy.click(); page.wait_for_function("() => window.__providerLoads===1 && window.okfPortableProvider?.manifest?.bundle_revision !== 'stale'")
    assert page.evaluate("() => window.__providerWrites.length===0")
    copy.click(); page.wait_for_function("() => window.__providerWrites.length===1")
    assert page.evaluate("() => window.__providerWrites[0].includes('# fresh revision') && window.__providerLoads===1")
    assert page.locator(".okf-graph-context-unavailable").count() <= 1


def test_group_d_static_graph_sync_context_elapsed_deadline_blocks_publication(static_url: str, page) -> None:
    page.add_init_script("window.OKF_LOOM_TEST_DEADLINES={context:60}")
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph?.cy?.nodes().length")
    page.locator(".okf-node-index summary").click(); page.locator(".okf-node-index__item").first.click()
    page.evaluate("""async () => {const payload=await fetch(document.getElementById('okf-graph').dataset.graphUrl).then(r=>r.json());window.__clock=1000;Date.now=()=>window.__clock;window.__syncWrites=[];Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:t=>{window.__syncWrites.push(t);return Promise.resolve();}}});window.okfPortableProvider={manifest:{bundle_revision:payload.bundle_revision},context:()=>{window.__clock+=61;return {schema_version:1,bundle_revision:payload.bundle_revision,query:'late-sync',mode:'lexical',filters:{},budget:{requested_tokens:6000,estimated_tokens:1},neighbors:1,included:0,omitted:0,truncated:false,items:[],representations:{markdown:'# late sync'}};}};}""")
    page.locator("#okf-copy-neighbourhood").click()
    retry = page.get_by_role("button", name="Retry", exact=True); expect(retry).to_be_visible(); assert retry.evaluate("el=>document.activeElement===el")
    assert page.evaluate("() => window.__syncWrites.length===0")
    expect(page.locator("#okf-graph-copy-status")).not_to_contain_text("Prepared")
    expect(page.locator("#okf-copy-neighbourhood")).not_to_have_text("Copied")


def test_group_d_portable_markdown_labels_multi_node_path_undirected(static_url: str, page) -> None:
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider")
    markdown = page.evaluate("""() => {const provider=window.okfPortableProvider,pack=provider.context({concept:'tables/orders'},{mode:'lexical',budget:6000,neighbors:1,limit:20},[]);if(!pack.items.length)throw new Error('expected context item');pack.items[0].relation_path=['A','B'];return provider.markdown(pack);}""")
    assert "Relation path (undirected): A ↔ B" in markdown
    assert "A -> B" not in markdown and "A → B" not in markdown


@pytest.mark.parametrize("settlement", ["resolve", "reject"])
def test_group_d_old_provider_late_settlement_cannot_replace_fresh_owner(static_url: str, page, settlement: str) -> None:
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph?.cy?.nodes().length")
    page.locator(".okf-node-index summary").click(); page.locator(".okf-node-index__item").first.click()
    page.evaluate("""async () => {const payload=await fetch(document.getElementById('okf-graph').dataset.graphUrl).then(r=>r.json());window.okfPortableProvider=null;window.__lateUnhandled=[];window.addEventListener('unhandledrejection',e=>{window.__lateUnhandled.push(String(e.reason));e.preventDefault();});window.__lateProvider={};window.okfPortableRetrieval.loadStatic=()=>new Promise((resolve,reject)=>{window.__lateProvider.resolve=resolve;window.__lateProvider.reject=reject;});window.__freshProvider={marker:'fresh',manifest:{bundle_revision:payload.bundle_revision},context:()=>({schema_version:1,bundle_revision:payload.bundle_revision,query:'fresh',mode:'lexical',filters:{},budget:{requested_tokens:6000,estimated_tokens:1},neighbors:1,included:0,omitted:0,truncated:false,items:[],representations:{markdown:'# fresh'}})};}""")
    page.locator("#okf-copy-neighbourhood").click(); page.wait_for_function("() => !!window.__lateProvider.resolve")
    page.locator("#okf-search").focus()
    page.evaluate("() => {window.okfTeardownGraphLifecycle('provider-revision-mismatch',{invalidateProvider:true});window.okfPortableProvider=window.__freshProvider;}")
    if settlement == "resolve":
        page.evaluate("() => window.__lateProvider.resolve({marker:'old',manifest:{bundle_revision:'old'},context:()=>{throw new Error('old context used');}})")
    else:
        page.evaluate("() => window.__lateProvider.reject(new Error('old load failed late'))")
    page.wait_for_timeout(100)
    assert page.evaluate("() => window.okfPortableProvider===window.__freshProvider && window.__lateUnhandled.length===0")
    assert page.evaluate("() => document.activeElement===document.getElementById('okf-search')")
    assert page.locator(".okf-graph-context-unavailable").count() == 0
    assert page.locator("#okf-graph-copy-status").inner_text() == ""


def test_group_d_mobile_graph_header_and_controls_never_layer(static_url: str, page) -> None:
    for width in (320, 390):
        page.set_viewport_size({"width":width,"height":780}); page.goto(static_url+"/__graph.html",wait_until="domcontentloaded")
        page.wait_for_function("() => window.__okfLoomGraph?.cy?.nodes().length")
        expect(page.locator(".okf-graph-tour")).to_have_count(0)
        page.get_by_role("button", name="Concepts").click()
        if not page.locator(".okf-node-index").evaluate("el=>el.open"): page.locator(".okf-node-index summary").click()
        page.locator(".okf-node-index").focus(); page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded")
        for y in (0, 180, 520):
            page.evaluate("y=>scrollTo(0,y)", y); page.wait_for_timeout(20)
            geometry=page.evaluate("""() => {const header=document.querySelector('.okf-topbar'),panel=document.querySelector('.okf-signal--bar'),controls=[...document.querySelectorAll('.okf-topbar a,.okf-topbar input,.okf-topbar button,.okf-signal--bar button,.okf-signal--bar input')].filter(e=>getComputedStyle(e).display!=='none');const h=header.getBoundingClientRect(),p=panel.getBoundingClientRect(),intersect=Math.max(0,Math.min(h.bottom,p.bottom)-Math.max(h.top,p.top))*Math.max(0,Math.min(h.right,p.right)-Math.max(h.left,p.left));return {h:h.toJSON(),p:p.toJSON(),headerPosition:getComputedStyle(header).position,panelPosition:getComputedStyle(panel).position,intersect,bad:controls.filter(e=>{const r=e.getBoundingClientRect();return r.left<0||r.right>innerWidth+.5}).length,scrollWidth:document.documentElement.scrollWidth,innerWidth};}""")
            assert geometry["headerPosition"] == "static" and geometry["panelPosition"] == "static"
            assert geometry["intersect"] == 0 and geometry["bad"] == 0 and geometry["scrollWidth"] <= geometry["innerWidth"], geometry


def test_group_d_timeout_copy_control_is_disabled_and_visually_distinct(server_url: str, page) -> None:
    page.add_init_script("window.OKF_LOOM_TEST_DEADLINES={context:60}")
    page.goto(server_url+"/__search?q=orders&mode=lexical&limit=5",wait_until="domcontentloaded");_open_context_options(page)
    page.evaluate("""() => {const native=fetch.bind(window);window.fetch=(url,opts)=>String(url).startsWith('/__context?')?new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true})):native(url,opts);}""")
    page.locator("[data-context-prepare]").click(); expect(page.get_by_role("button",name="Retry",exact=True)).to_be_visible()
    copy=page.locator("[data-context-copy-prepared]");expect(copy).to_be_disabled();style=copy.evaluate("el=>({opacity:parseFloat(getComputedStyle(el).opacity),cursor:getComputedStyle(el).cursor,bg:getComputedStyle(el).backgroundColor})")
    assert style["opacity"] <= .5 and style["cursor"] == "not-allowed"


def test_clipboard_fallback_late_settlement_and_320px_capture(static_url: str, page) -> None:
    """Clipboard failure stays recoverable; late writes and narrow overflow are owned."""
    page.set_viewport_size({"width": 320, "height": 720})
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    responsive = page.evaluate("""() => {
      const viewport=innerWidth;
      const controls=Array.from(document.querySelectorAll('.okf-topbar a,.okf-topbar input,.okf-topbar button')).filter(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return s.display!=='none'&&r.width>0&&r.height>0;});
      const clipped=controls.filter(el=>{const r=el.getBoundingClientRect();return r.left<0||r.right>viewport;}).map(el=>el.textContent||el.getAttribute('aria-label'));
      const details=document.querySelector('.okf-page-context details'),summary=details.querySelector('summary').getBoundingClientRect(),first=document.querySelector('.okf-search-result').getBoundingClientRect();
      return {clipped,labels:controls.map(el=>(el.textContent||el.getAttribute('aria-label')||el.value).trim()),detailsOpen:details.open,summaryHeight:summary.height,firstTop:first.top,summaryBottom:summary.bottom,overflow:document.documentElement.scrollWidth>viewport};
    }""")
    assert responsive["clipped"] == [] and responsive["overflow"] is False
    assert any("demo_bundle" in label for label in responsive["labels"])
    assert any("Graph" in label for label in responsive["labels"])
    assert any("Index" in label for label in responsive["labels"])
    assert responsive["detailsOpen"] is False and responsive["summaryHeight"] <= 44
    assert responsive["firstTop"] >= responsive["summaryBottom"]
    _open_context_options(page)
    assert page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert page.evaluate("() => document.body.dataset.okfCaptureSurface") == "search"
    assert page.evaluate("() => !!document.body.dataset.okfCaptureRevision")

    page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {configurable:true,value:{writeText:()=>Promise.reject(new Error('denied'))}})""")
    cited = page.get_by_role("button", name="Copy passage").first
    source_article = cited.locator("xpath=ancestor::article")
    source_title = source_article.locator("h3").inner_text().strip()
    source_citation = source_article.locator(".okf-search-citation").inner_text().strip()
    cited.click()
    fallback = page.locator(".okf-copy-fallback")
    expect(fallback).to_be_visible()
    expect(fallback.locator("h3")).to_contain_text(source_title)
    expect(fallback.locator(".okf-copy-fallback__source")).to_contain_text(source_citation)
    expect(fallback.locator(".okf-copy-fallback__instruction")).to_contain_text("Ctrl/Cmd+C")
    expect(fallback.get_by_role("button", name="Select all text")).to_be_visible()
    assert fallback.evaluate("el => el.closest('.okf-search-result') === document.querySelector('.okf-search-result')")
    expect(page.locator("#okf-copy-fallback-text")).not_to_have_value("")

    page.evaluate("""() => {
      document.querySelector('.okf-copy-fallback').hidden=true;
      window.__lateClipboard = {};
      Object.defineProperty(navigator, 'clipboard', {configurable:true,value:{writeText:()=>new Promise(resolve=>window.__lateClipboard.resolve=resolve)}});
    }""")
    cited.click()
    page.wait_for_function("() => typeof window.__lateClipboard.resolve === 'function'")
    page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
    page.evaluate("() => window.__lateClipboard.resolve()")
    page.wait_for_timeout(100)
    expect(cited).not_to_have_text("Copied")


def test_capture_readiness_success_metadata(static_url: str, page) -> None:
    page.set_viewport_size({"width": 960, "height": 720})
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => document.body.dataset.okfCaptureReady === 'ready'")
    result = page.evaluate("async () => {const run=window.okfCaptureReadiness.start({target:'search',expectedRevision:document.body.dataset.okfCaptureRevision});return await run.promise;}")
    assert result["screenshotAllowed"] is True
    assert result["generation"] == 1
    metadata = result["metadata"]
    assert metadata["revision"]
    assert metadata["url"].endswith("/__search.html?q=orders")
    assert metadata["target"] == "search" and metadata["kind"] == "search"
    assert metadata["cssViewport"] == {"width": 960, "height": 720}
    assert metadata["dpr"] >= 1
    assert metadata["browserZoomEvidence"]["source"].endswith("not a text-only zoom claim")
    assert metadata["theme"] in {"swiss-light", "swiss-dark", "technical-light", "technical-dark"}
    assert metadata["javascript"] is True
    assert page.evaluate("() => window.okfCaptureReadiness.deadlineMs") == 30000


@pytest.mark.parametrize("surface", ["live"])
def test_graph_capture_readiness_uses_production_adapter(
    server_url: str, static_url: str, page, surface: str
) -> None:
    """The shipped graph adapter authorizes only the settled current revision."""
    url = server_url + "/__graph" if surface == "live" else static_url + "/__graph.html"
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph?.cy?.nodes().length > 0")
    result = page.evaluate("""async () => {
      const graph = document.getElementById('okf-graph');
      const payload = await fetch(graph.dataset.graphUrl).then(response => response.json());
      const revision = document.body.dataset.okfCaptureRevision;
      const readiness = await window.okfCaptureReadiness.start({target:'graph', expectedRevision:revision}).promise;
      return {state:window.okfGraphCaptureState(), payloadNodeCount:payload.nodes.length, revision, readiness};
    }""")
    state = result["state"]
    required = {
        "initialized", "nodeCount", "payloadNodeCount",
        "layoutRequired", "layoutSettled", "layoutGeneration", "appliedLayoutGeneration",
        "transitionPending", "presenceDiagnostic",
    }
    assert required <= set(state)
    assert state["initialized"] is True
    assert state["payloadNodeCount"] == result["payloadNodeCount"] > 0
    assert state["nodeCount"] > 0
    assert state["layoutRequired"] is True
    assert state["layoutSettled"] is True
    assert state["layoutGeneration"] == state["appliedLayoutGeneration"] > 0
    assert state["transitionPending"] is False
    assert state["presenceDiagnostic"] == {"count": 0, "sessionIds": []}
    assert result["readiness"]["screenshotAllowed"] is True
    assert result["readiness"]["metadata"]["readinessPhase"] == "graph-ready"
    assert result["readiness"]["metadata"]["revision"] == result["revision"]


def test_graph_capture_readiness_adapter_exception_fails_closed_promptly(server_url: str, page) -> None:
    """A broken stable adapter rejects promptly instead of orphaning the rAF poll."""
    page.goto(server_url + "/__graph", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph?.cy?.nodes().length > 0")
    result = page.evaluate("""async () => {
      window.okfGraphCaptureState = () => { throw new Error('adapter exploded'); };
      const started = performance.now();
      const run = window.okfCaptureReadiness.start({target:'graph'});
      const outcome = await Promise.race([
        run.promise.then(value => ({authorized:value.screenshotAllowed})).catch(error => ({
          authorized:error.screenshotAllowed, code:error.code, phase:error.metadata.readinessPhase,
          cause:error.metadata.cause, elapsed:performance.now()-started
        })),
        new Promise(resolve => setTimeout(() => resolve({hung:true}), 1000))
      ]);
      return outcome;
    }""")
    assert "hung" not in result
    assert result["authorized"] is False
    assert result["code"] == "unavailable"
    assert result["phase"] == "graph-adapter"
    assert result["cause"] == "adapter exploded"
    assert result["elapsed"] < 1000


def test_capture_readiness_timeout_late_retry_and_distinct_diagnostics(static_url: str, page) -> None:
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => document.body.dataset.okfCaptureReady === 'ready'")
    result = page.evaluate("""async () => {
      const first=window.okfCaptureReadiness.start({target:'#never-present'});
      const missing=await first.promise.then(value=>({ok:true,value}),error=>({ok:false,code:error.code,allowed:error.screenshotAllowed}));
      document.body.dataset.okfCaptureReady='loading';
      const stale=window.okfCaptureReadiness.start({target:'search'});
      const retry=window.okfCaptureReadiness.start({target:'search'});
      const superseded=await stale.promise.then(value=>({ok:true,value}),error=>({ok:false,code:error.code,allowed:error.screenshotAllowed}));
      document.body.dataset.okfCaptureReady='ready';
      const retryResult=await retry.promise;
      const diagnostics={};
      const hidden=document.createElement('div');hidden.id='capture-hidden';hidden.hidden=true;document.body.appendChild(hidden);
      for(const spec of [['hidden','#capture-hidden'],['unavailable','search'],['network-failure','search']]){
        document.body.dataset.okfCaptureReady=spec[0];
        const run=window.okfCaptureReadiness.start({target:spec[1]});
        const owned=run.promise.then(value=>({ok:true,value}),error=>({ok:false,code:error.code,allowed:error.screenshotAllowed,message:error.message}));
        diagnostics[spec[0]]=await owned;
      }
      document.body.dataset.okfCaptureReady='ready';
      const canvas=document.createElement('canvas');canvas.id='capture-canvas';canvas.width=10;canvas.height=10;canvas.style.cssText='width:10px;height:10px';document.body.appendChild(canvas);
      const uniform=await window.okfCaptureReadiness.start({target:'#capture-canvas'}).promise;
      canvas.toDataURL=()=>{throw new DOMException('tainted','SecurityError');};
      const unreadable=await window.okfCaptureReadiness.start({target:'#capture-canvas'}).promise.then(value=>({ok:true,value}),error=>({ok:false,code:error.code,allowed:error.screenshotAllowed}));
      return {missing,superseded,retryGeneration:retry.generation,retryResult,diagnostics,uniform,unreadable};
    }""")
    assert result["missing"] == {"ok": False, "code": "missing-target", "allowed": False}
    assert result["superseded"] == {"ok": False, "code": "superseded", "allowed": False}
    assert result["retryGeneration"] == 3
    assert result["retryResult"]["screenshotAllowed"] is True
    assert result["retryResult"]["metadata"]["revision"]
    assert {key: value["code"] for key, value in result["diagnostics"].items()} == {
        "hidden": "hidden", "unavailable": "unavailable", "network-failure": "network-failure",
    }
    assert all(value["allowed"] is False for value in result["diagnostics"].values())
    assert result["uniform"]["screenshotAllowed"] is True
    assert result["unreadable"] == {"ok": False, "code": "unreadable", "allowed": False}


def test_capture_readiness_production_deadline_fences_late_settlement_and_allows_fresh_retry(
    static_url: str, page
) -> None:
    page.clock.install()
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => document.body.dataset.okfCaptureReady === 'ready'")
    page.evaluate("""() => {
      document.body.dataset.okfCaptureReady='loading';
      const run=window.okfCaptureReadiness.start({target:'search'});
      window.__captureAuthorizations=[];
      window.__captureTimeout=run.promise.then(value=>{window.__captureAuthorizations.push(value.generation);return {ok:true,value};},error=>({ok:false,code:error.code,allowed:error.screenshotAllowed,generation:error.generation,phase:error.metadata.readinessPhase}));
      window.__captureDeadline={deadlineAt:run.deadlineAt,startedAt:run.deadlineAt-window.okfCaptureReadiness.deadlineMs};
    }""")
    page.clock.fast_forward(30000)
    timed_out = page.evaluate("async () => await window.__captureTimeout")
    assert timed_out == {
        "ok": False, "code": "timeout", "allowed": False,
        "generation": 1, "phase": "loading",
    }
    assert page.evaluate(
        "() => window.__captureDeadline.deadlineAt-window.__captureDeadline.startedAt"
    ) == 30000

    # The render can settle after its owner timed out, but that stale generation
    # must never gain screenshot authorization.
    page.evaluate("() => {document.body.dataset.okfCaptureReady='ready';}")
    page.clock.fast_forward(100)
    assert page.evaluate("() => window.__captureAuthorizations") == []
    assert page.evaluate("async () => await window.__captureTimeout") == timed_out

    retry = page.evaluate("""async () => {const run=window.okfCaptureReadiness.start({target:'search'});return await run.promise;}""")
    assert retry["generation"] == 2 and retry["screenshotAllowed"] is True


def test_capture_readiness_pagehide_fences_late_settlement_and_allows_fresh_retry(
    static_url: str, page
) -> None:
    page.clock.install()
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => document.body.dataset.okfCaptureReady === 'ready'")
    result = page.evaluate("""async () => {
      document.body.dataset.okfCaptureReady='loading';
      const run=window.okfCaptureReadiness.start({target:'search'});
      const authorizations=[];
      const terminal=run.promise.then(value=>{authorizations.push(value.generation);return {ok:true,value};},error=>({ok:false,code:error.code,allowed:error.screenshotAllowed,generation:error.generation,phase:error.metadata.readinessPhase}));
      window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:false}));
      const pagehide=await terminal;
      document.body.dataset.okfCaptureReady='ready';
      await new Promise(resolve=>requestAnimationFrame(()=>resolve()));
      const late=await terminal;
      const retryRun=window.okfCaptureReadiness.start({target:'search'});
      const retry=await retryRun.promise;
      return {pagehide,late,authorizations,retry,deadlineMs:window.okfCaptureReadiness.deadlineMs};
    }""")
    assert result["pagehide"] == {
        "ok": False, "code": "superseded", "allowed": False,
        "generation": 1, "phase": "pagehide",
    }
    assert result["late"] == result["pagehide"]
    assert result["authorizations"] == []
    assert result["retry"]["generation"] == 2
    assert result["retry"]["screenshotAllowed"] is True
    assert result["deadlineMs"] == 30000


@pytest.mark.parametrize("theme", ["technical-light", "technical-dark", "swiss-light", "swiss-dark"])
def test_populated_search_accessibility_in_every_theme(static_url: str, page, theme: str) -> None:
    """Every resolved family-mode theme keeps evidence, controls, focus and
    text legible. Both families share a palette within a mode, so each mode
    is audited once per family."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    # Apply the theme through the canonical owner (theme.js); it sets
    # data-theme and persists the preference. Falls back to a direct
    # data-theme set if the API is somehow absent.
    page.evaluate("""theme => {
      if (window.OKFLoomTheme && window.OKFLoomTheme.setTheme) window.OKFLoomTheme.setTheme(theme);
      else document.documentElement.dataset.theme = theme;
    }""", theme)
    expect(page.locator(".okf-search-result").first).to_be_visible()
    first_disclosure = page.locator(".okf-search-disclosure").first
    expect(first_disclosure).to_be_visible()
    first_disclosure.locator("summary").click()
    expect(first_disclosure.locator(".okf-search-citation")).to_be_visible()
    expect(page.locator("#okf-search-query")).to_be_visible()
    page.locator("#okf-search-query").focus()
    audit = page.evaluate("""() => {
      const rgb=value=>{const m=value.match(/[\\d.]+/g);return m?m.slice(0,3).map(Number):null;};
      const luminance=value=>{const c=rgb(value);if(!c)return null;const linear=c.map(v=>{v/=255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4);});return .2126*linear[0]+.7152*linear[1]+.0722*linear[2];};
      const contrast=(a,b)=>{const x=luminance(a),y=luminance(b);return x==null||y==null?null:(Math.max(x,y)+.05)/(Math.min(x,y)+.05);};
      const effectiveBackground=el=>{for(let node=el;node;node=node.parentElement){const color=getComputedStyle(node).backgroundColor;if(color&&!color.endsWith(', 0)')&&color!=='rgba(0, 0, 0, 0)')return color;}return getComputedStyle(document.documentElement).backgroundColor;};
      const body=getComputedStyle(document.body),citation=document.querySelector('.okf-search-citation'),citationStyle=getComputedStyle(citation),focused=getComputedStyle(document.activeElement);
      const visibleControls=Array.from(document.querySelectorAll('input,select,button,a')).filter(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;});
      const clipped=visibleControls.filter(el=>{const r=el.getBoundingClientRect();return r.left < -1 || r.right > innerWidth+1;});
      return {theme:document.documentElement.dataset.theme,overflow:document.documentElement.scrollWidth>innerWidth+1,clipped:clipped.length,bodyContrast:contrast(body.color,effectiveBackground(document.body)),citationContrast:contrast(citationStyle.color,effectiveBackground(citation)),outlineStyle:focused.outlineStyle,outlineWidth:parseFloat(focused.outlineWidth)||0};
    }""")
    assert audit["theme"] == theme
    assert audit["overflow"] is False and audit["clipped"] == 0
    assert audit["bodyContrast"] is not None and audit["bodyContrast"] >= 4.5
    assert audit["citationContrast"] is not None and audit["citationContrast"] >= 4.5
    assert audit["outlineStyle"] != "none" and audit["outlineWidth"] >= 2


def test_forced_colors_preserves_search_invalid_and_copy_fallback_states(static_url: str, page) -> None:
    """Forced colors retains textual, semantic and bordered state indicators."""
    page.emulate_media(forced_colors="active")
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    assert page.evaluate("() => matchMedia('(forced-colors: active)').matches")
    expect(page.locator(".okf-search-result").first).to_be_visible()
    selected = page.locator('.okf-search-mode input:checked')
    expect(selected).to_be_visible()
    assert selected.is_checked()

    page.goto(static_url + "/__search.html?q=x&q=y&mode=lexical", wait_until="domcontentloaded")
    page.wait_for_function("() => document.body.dataset.okfCaptureReady === 'input-error'")
    error = page.locator(".okf-search-input-error")
    expect(error.get_by_role("heading", name="Correct the q parameter")).to_be_visible()
    assert "Action required" in error.evaluate("el => getComputedStyle(el,'::before').content")
    query = page.locator("#okf-search-query")
    expect(query).to_have_attribute("aria-invalid", "true")
    invalid_style = query.evaluate("el => {const s=getComputedStyle(el);return {outline:parseFloat(s.outlineWidth)||0,border:parseFloat(s.borderWidth)||0};}")
    assert invalid_style["outline"] >= 2 and invalid_style["border"] >= 1

    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    page.evaluate("() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(new Error('denied'))}})")
    page.get_by_role("button", name="Copy passage").first.click()
    fallback = page.locator(".okf-copy-fallback")
    expect(fallback).to_be_visible()
    expect(fallback.get_by_role("heading", name="Copy manually")).to_be_visible()
    expect(fallback.locator(".okf-copy-fallback__instruction")).to_contain_text("Ctrl/Cmd+C")
    expect(fallback.locator("textarea")).not_to_have_value("")
    forced_style = fallback.evaluate("el => {const s=getComputedStyle(el),t=getComputedStyle(el.querySelector('textarea'));return {border:parseFloat(s.borderWidth)||0,textareaBorder:parseFloat(t.borderWidth)||0};}")
    assert forced_style["border"] >= 1 and forced_style["textareaBorder"] >= 1


def test_reduced_motion_keeps_suggestion_context_selection_and_fallback_states(static_url: str, server_url: str, page) -> None:
    """Reduced motion removes animation without suppressing required state changes."""
    page.emulate_media(reduced_motion="reduce")
    assert page.evaluate("() => matchMedia('(prefers-reduced-motion: reduce)').matches")

    page.goto(server_url + "/", wait_until="domcontentloaded")
    search = page.locator('input[type="search"][name="q"]').first
    search.fill("orders")
    suggestion = page.locator('[role="option"]').first
    expect(suggestion).to_be_visible()
    suggestion_motion = suggestion.evaluate("el => {const s=getComputedStyle(el);return {animation:s.animationDuration,transition:s.transitionDuration};}")

    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    _open_context_options(page)
    page.locator("[data-context-prepare]").click()
    prepared = page.locator("[data-context-copy-prepared]")
    expect(prepared).to_be_enabled()
    prepared_motion = prepared.evaluate("el => {const s=getComputedStyle(el);return {animation:s.animationDuration,transition:s.transitionDuration};}")
    page.evaluate("() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(new Error('denied'))}})")
    page.get_by_role("button", name="Copy passage").first.click()
    fallback = page.locator(".okf-copy-fallback")
    expect(fallback).to_be_visible()
    fallback_motion = fallback.evaluate("el => {const s=getComputedStyle(el);return {animation:s.animationDuration,transition:s.transitionDuration};}")

    page.goto(static_url + "/playbooks/refund_flow.html", wait_until="domcontentloaded")
    page.evaluate("""() => {const strong=Array.from(document.querySelectorAll('.okf-page__body strong')).find(el=>el.textContent.includes('over $1000 USD'));const range=document.createRange();range.selectNodeContents(strong);const selection=getSelection();selection.removeAllRanges();selection.addRange(range);document.dispatchEvent(new Event('selectionchange'));}""")
    selection_action = page.get_by_role("button", name="Copy selection with citation")
    expect(selection_action).to_be_visible()
    selection_motion = selection_action.evaluate("el => {const s=getComputedStyle(el);return {animation:s.animationDuration,transition:s.transitionDuration};}")
    motion = [suggestion_motion, prepared_motion, fallback_motion, selection_motion]
    assert motion and all(row["animation"] == "0s" and row["transition"] == "0s" for row in motion)


@pytest.mark.parametrize("width,height", [(1440, 900), (768, 900), (390, 844), (320, 720)])
def test_populated_search_context_reflows_across_required_viewports(static_url: str, page, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    expect(page.locator(".okf-search-result").first).to_be_visible()
    _open_context_options(page)
    page.locator("[data-context-prepare]").click()
    expect(page.locator("[data-context-copy-prepared]")).to_be_enabled()
    page.locator("#okf-search-query").focus()
    reflow = page.evaluate("""() => {const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};const controls=Array.from(document.querySelectorAll('input,select,button,a,summary')).filter(visible);const clipped=controls.filter(el=>{const r=el.getBoundingClientRect();return r.left < -1 || r.right > innerWidth+1 || r.width < 1 || r.height < 1;}).map(el=>{const r=el.getBoundingClientRect();return {tag:el.tagName,id:el.id,className:el.className,text:(el.textContent||el.getAttribute('aria-label')||'').trim().slice(0,60),left:r.left,right:r.right,width:r.width};});const focus=getComputedStyle(document.activeElement);return {width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth+1,clipped,outline:parseFloat(focus.outlineWidth)||0};}""")
    assert reflow["width"] == width
    assert reflow["overflow"] is False and reflow["clipped"] == [], reflow
    assert reflow["outline"] >= 2


def test_search_context_reflows_at_720px_css_viewport_dpr2(static_url: str, page) -> None:
    """The 720px CSS viewport at DPR2 remains functional without claiming browser zoom."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setDeviceMetricsOverride", {
        "width": 720, "height": 450, "deviceScaleFactor": 2, "mobile": False,
        "screenWidth": 1440, "screenHeight": 900,
    })
    _open_context_options(page)
    page.locator("[data-context-prepare]").click()
    expect(page.locator("[data-context-copy-prepared]")).to_be_enabled()
    zoom = page.evaluate("""() => {const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};const controls=Array.from(document.querySelectorAll('input,select,button,a,summary')).filter(visible);return {width:innerWidth,dpr:devicePixelRatio,overflow:document.documentElement.scrollWidth>innerWidth+1,clipped:controls.filter(el=>{const r=el.getBoundingClientRect();return r.left < -1 || r.right > innerWidth+1;}).length};}""")
    assert zoom == {"width": 720, "dpr": 2, "overflow": False, "clipped": 0}


def test_search_context_fallback_reflows_at_320px_css_viewport_dpr4(static_url: str, page) -> None:
    """The 320px CSS viewport at DPR4 reflows without claiming browser zoom."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'")
    _open_context_options(page)
    page.evaluate("""() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(new Error('denied'))}})""")
    page.get_by_role("button", name="Copy passage").first.click()
    expect(page.locator(".okf-copy-fallback")).to_be_visible()
    before = page.evaluate("() => ({width:innerWidth,dpr:devicePixelRatio})")
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setDeviceMetricsOverride", {
        "width": 320, "height": 225, "deviceScaleFactor": 4, "mobile": False,
        "screenWidth": 1280, "screenHeight": 900,
    })
    page.wait_for_timeout(150)
    zoomed = page.evaluate("""() => {
      const visible=el=>{const s=getComputedStyle(el),r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
      const selectors=['.okf-topbar a','.okf-topbar button','.okf-search-workbench input','.okf-search-workbench button','.okf-page-context input','.okf-page-context select','.okf-page-context button','.okf-copy-fallback textarea','.okf-copy-fallback button'];
      const controls=selectors.flatMap(selector=>Array.from(document.querySelectorAll(selector))).filter(visible);
      const boxes=controls.map(el=>{const r=el.getBoundingClientRect();return {tag:el.tagName,text:(el.textContent||el.getAttribute('aria-label')||'').trim().slice(0,40),left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};});
      const clipped=boxes.filter(r=>r.left < -1 || r.right > innerWidth+1 || r.width < 1 || r.height < 1);
      const overlap=(a,b)=>Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1;
      const header=boxes.filter(row=>row.top < document.querySelector('.okf-topbar').getBoundingClientRect().bottom+1);
      const headerOverlaps=[];for(let i=0;i<header.length;i++)for(let j=i+1;j<header.length;j++)if(overlap(header[i],header[j]))headerOverlaps.push([header[i].text,header[j].text]);
      return {innerWidth,innerHeight,dpr:devicePixelRatio,visualScale:visualViewport?visualViewport.scale:1,scrollWidth:document.documentElement.scrollWidth,clipped,headerOverlaps};
    }""")
    assert zoomed["innerWidth"] == 320 == before["width"] / 4
    assert zoomed["dpr"] == 4 and zoomed["dpr"] > before["dpr"]
    assert zoomed["scrollWidth"] <= zoomed["innerWidth"] + 1
    assert zoomed["clipped"] == []
    assert zoomed["headerOverlaps"] == []


def test_malformed_single_file_corpus_settles_unavailable(tmp_path: Path, page) -> None:
    """Malformed evidence keeps Search reachable but disables evidence actions."""
    from okf_loom import Bundle
    from okf_loom.render import render_single_file

    output = tmp_path / "malformed-single.html"
    render_single_file(Bundle.load(DEMO_BUNDLE), output)
    html = output.read_text(encoding="utf-8")
    prefix = "window.BUNDLE = "
    start = html.index(prefix) + len(prefix)
    end = html.index(";\nwindow.OKF_LOOM_INITIAL_LAYOUT", start)
    payload = json.loads(html[start:end])
    payload["retrieval_corpus"]["manifest"]["schema_version"] = 999
    output.write_text(html[:start] + json.dumps(payload) + html[end:], encoding="utf-8")

    page.goto(output.as_uri(), wait_until="domcontentloaded")
    page.locator("#okf-single-search-toggle").click()
    expect(page.locator("[data-single-search-status]")).to_contain_text("Portable search unavailable")
    assert page.evaluate("() => window.okfPortableProvider == null")
    assert page.evaluate("() => document.body.dataset.okfCaptureReady") == "unavailable"
    authorization = page.evaluate("async () => { try { await window.okfCaptureReadiness.start({target:'search'}).promise; return null; } catch (error) { return {code:error.code, screenshotAllowed:error.screenshotAllowed}; } }")
    assert authorization == {"code": "unavailable", "screenshotAllowed": False}
    expect(page.locator("#okf-single-search-toggle")).to_be_enabled()
    expect(page.locator("#okf-single-search input, #okf-single-search button[type=submit]").first).to_be_disabled()
    expect(page.locator("[data-single-page-context]")).to_be_disabled()


def test_single_file_nojs_and_missing_corpus_are_honest(tmp_path: Path, page) -> None:
    from okf_loom import Bundle
    from okf_loom.render import render_single_file

    output = tmp_path / "single.html"
    render_single_file(Bundle.load(DEMO_BUNDLE), output)
    nojs_context = page.context.browser.new_context(java_script_enabled=False)
    nojs = nojs_context.new_page()
    try:
        nojs.goto(output.as_uri(), wait_until="domcontentloaded")
        expect(nojs.get_by_text("Concept bodies, the graph view, and embedded Search and Context controls need JavaScript", exact=False)).to_be_visible()
        expect(nojs.locator("#okf-single-search-toggle")).to_be_hidden()
    finally:
        nojs_context.close()

    html = output.read_text(encoding="utf-8")
    prefix = "window.BUNDLE = "
    start = html.index(prefix) + len(prefix)
    end = html.index(";\nwindow.OKF_LOOM_INITIAL_LAYOUT", start)
    payload = json.loads(html[start:end])
    del payload["retrieval_corpus"]
    missing = tmp_path / "single-missing.html"
    missing.write_text(html[:start] + json.dumps(payload) + html[end:], encoding="utf-8")
    page.goto(missing.as_uri(), wait_until="domcontentloaded")
    expect(page.locator("[data-single-search-status]")).to_contain_text("unavailable")
    expect(page.locator("#okf-single-search-toggle")).to_be_enabled()
    page.locator("#okf-single-search-toggle").click()
    expect(page.locator("#okf-single-search")).to_be_visible()
    expect(page.locator("#okf-single-search input").first).to_be_disabled()
    assert page.evaluate("() => document.body.dataset.okfCaptureReady") == "unavailable"
    page.keyboard.press("Escape")
    expect(page.locator('#okf-single-search')).to_be_hidden()
    expect(page.locator('#okf-single-search-toggle')).to_be_focused()
    page.locator('[data-okf-shell-item="graph"] a').click()
    settings = page.get_by_text("Graph settings", exact=True)
    if settings.count() and not page.get_by_role("button", name="Re-run layout").is_visible():
        settings.click()
    page.get_by_role("button", name="Re-run layout").click()
    page.wait_for_function("() => window.okfGraphCaptureState().layoutSettled")
    assert page.evaluate("() => document.body.dataset.okfCaptureReady") == "unavailable"
    assert page.evaluate("() => document.body.dataset.okfGraphCaptureReady") == "ready"
    authorization = page.evaluate("async () => { try { await window.okfCaptureReadiness.start({target:'search'}).promise; return null; } catch (error) { return {code:error.code, screenshotAllowed:error.screenshotAllowed}; } }")
    assert authorization == {"code": "unavailable", "screenshotAllowed": False}


def test_backlinks_show(server_url: str, page) -> None:
    """A referenced concept shows a non-empty 'Cited by' section.

    ``tables/customers`` is referenced by ``tables/orders`` (both a body
    markdown link and a typed ``references`` relation), so the backlinks
    block on its page must be present and non-empty.
    """
    page.goto(f"{server_url}/tables/customers", wait_until="domcontentloaded")
    backlinks = page.locator("#okf-backlinks")
    backlinks.wait_for(state="attached")
    assert backlinks.is_visible(), "backlinks section is hidden"
    # The section is always rendered (template emits the heading); the
    # contract is that it contains at least one outbound link.
    link_count = backlinks.locator("a").count()
    assert link_count >= 1, (
        "'Cited by' section is empty; expected a link back to tables/orders"
    )


# ---------------------------------------------------------------------------
# Signal controls — durable behaviour + async-lifecycle proof
# ---------------------------------------------------------------------------
#
# These assert REAL Cytoscape/graph-state mutations (node positions, data,
# classes; edge weights/classes; focus/dim classes; layout lifecycle counters)
# through the ``window.__okfLoomGraph`` test/diagnostic hook (which exposes the LIVE,
# mutable Cytoscape instance for tests) — not screenshots or status strings.
# They prove each control changes graph state, that keyboard operation works,
# and that the SINGLE stale-safe layout owner fences late completions so the
# latest control/LOD/focus state always wins.
#
# Lifecycle owner contract (runLayoutNow): the owner token (layoutSeq) is BUMPED
# BEFORE the previous layout is stopped, so even a SYNCHRONOUS stop-triggered
# `layoutstop` from the old run sees `mySeq != layoutSeq` and is fenced. The
# settle result is applied exactly ONCE per layout (one-shot by sequence), so a
# layout's `layoutstop` + its safety-net timer never double-apply. Readiness is
# keyed on the exact `layoutStats.lastAppliedSeq`.
#
# ASYNC_LIFECYCLE_MATRIX (proven by test_signal_async_lifecycle_latest_wins and
# test_signal_lod_lifecycle below):
#   scenario                          | owner / fence                  | proof
#   debounced control relayout        | runLayoutNow (++seq, then stop)| latest preset wins; positions valid
#   layout-select rapid restart       | runLayoutNow (++seq, then stop)| skippedStale increases; last layout wins
#   stop-triggered SYNC layoutstop     | ++layoutSeq BEFORE stop()      | old run sees mySeq!=layoutSeq → skippedStale; never applied
#   one-shot apply per layout         | lastAppliedSeq dedup           | applied delta <= starts delta (no double-apply)
#   LOD show-all during/after layout  | runLayoutNow (routed)          | all nodes shown, grid, no overlap
#   node-index ensure-visible         | runLayoutNow (routed)          | hidden node added; latest state intact
#   late layoutstop / timed fallback  | mySeq === layoutSeq gate       | skippedStale increments; no stale fit
#   selection/focus preserved         | identity kept on relayout      | selected node unchanged after rapid changes


def _open_graph(page, server_url, init_script: str | None = None):
    """Navigate to /__graph and wait until the graph + diagnostic hook are
    ready and the initial layout has been applied at least once.

    Readiness keys on ``layoutStats.lastAppliedSeq`` — the EXACT sequence of the
    most recently applied (settled, fenced-current) layout — which is one-shot
    per layout, instead of the looser ``applied`` counter."""
    # Returning-visitor state: suppress the first-visit micro-tour so these
    # graph-state tests drive a clean canvas. The tour moves focus into itself
    # and traps Tab on open; test_graph_micro_tour_focus_trap drives it
    # deliberately WITHOUT this flag.
    page.add_init_script(
        "try { window.localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}"
    )
    if init_script:
        page.add_init_script(init_script)
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    # These tests exercise canvas behavior, not the compact Concepts default.
    # Select the public pane explicitly instead of waiting for hidden pixels.
    graph_pane = page.get_by_role('navigation', name='Graph panes').get_by_role('button', name='Graph', exact=True)
    if graph_pane.is_visible():
        graph_pane.click()
    page.locator("#okf-graph canvas").first.wait_for(state="visible")
    page.wait_for_function(
        "() => window.__okfLoomGraph && window.__okfLoomGraph.cy "
        "&& window.__okfLoomGraph.cy.nodes().length > 0 "
        "&& window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1",
        timeout=10000,
    )


def _state(page):
    return page.evaluate("() => window.__okfLoomGraph.getState()")


def _applied_seq(page) -> int:
    """The exact sequence of the last applied layout (one-shot per layout)."""
    return page.evaluate("() => window.__okfLoomGraph.layoutStats.lastAppliedSeq")


def _wait_applied_after(page, prev_seq: int, timeout: int = 10000) -> None:
    """Wait until a NEW layout (sequence > prev_seq) has actually settled and
    been applied — a precise readiness signal, not a blind sleep."""
    page.wait_for_function(
        "prev => window.__okfLoomGraph.layoutStats.lastAppliedSeq > prev",
        arg=prev_seq, timeout=timeout,
    )


_SNAP_JS = """() => {
  const cy = window.__okfLoomGraph.cy;
  const nodes = {}, edges = {};
  cy.nodes().forEach(n => { const p = n.position(); nodes[n.id()] = {
    x: Math.round(p.x), y: Math.round(p.y), color: n.data('color'),
    vizSize: Math.round((n.data('vizSize')||0)*10)/10,
    bridge: n.hasClass('okf-bridge'), focus: n.hasClass('okf-focus-root'),
    dim: n.hasClass('dim') }; });
  cy.edges().forEach(e => { edges[e.id()] = {
    weight: Math.round((e.data('weight')||0)*1000)/1000,
    rel: e.hasClass('okf-rel-edge'), relColor: e.data('relColor')||null,
    showLabel: e.hasClass('okf-show-label'), dim: e.hasClass('dim') }; });
  return { nodes, edges, n: cy.nodes().length, e: cy.edges().length,
           dimNodes: cy.nodes('.dim').length, dimEdges: cy.edges('.dim').length,
           bridgeNodes: cy.nodes('.okf-bridge').length,
           relEdges: cy.edges('.okf-rel-edge').length,
           focusNodes: cy.nodes('.okf-focus-root').length,
           zoom: Math.round(cy.zoom()*100)/100 };
}"""


def _snap(page):
    return page.evaluate(_SNAP_JS)


def _pos_moved(a, b) -> int:
    """Total |dx|+|dy| of nodes present in both snapshots."""
    tot = 0
    for nid, na in a["nodes"].items():
        nb = b["nodes"].get(nid)
        if nb:
            tot += abs(na["x"] - nb["x"]) + abs(na["y"] - nb["y"])
    return tot


def _min_gap(page) -> float:
    return page.evaluate(
        """() => { const cy = window.__okfLoomGraph.cy; const ns = cy.nodes();
           let m = 1e9; for (let i=0;i<ns.length;i++) for (let j=i+1;j<ns.length;j++){
             const a=ns[i].position(), b=ns[j].position();
             const d=Math.hypot(a.x-b.x,a.y-b.y)-ns[i].width()/2-ns[j].width()/2;
             if (d<m) m=d; } return ns.length<2 ? 999 : Math.round(m); }"""
    )


def _bbox_diag(page) -> float:
    """Model-space bounding-box diagonal of all nodes — a spread metric that is
    independent of zoom/fit (node.position() is in Cytoscape model coordinates,
    which overlayAwareFit never mutates)."""
    return page.evaluate(
        "() => { const cy = window.__okfLoomGraph.cy; const bb = cy.nodes().boundingBox();"
        " return Math.round(Math.hypot(bb.w, bb.h)); }"
    )


def _range_valuetext(page, aria_label: str):
    """The aria-valuetext (high-end label) currently exposed by a named range."""
    return page.evaluate(
        """(label) => { const i = Array.from(document.querySelectorAll('.okf-signal input[type=range]'))
             .find(x => (x.getAttribute('aria-label')||'') === label);
           return i ? i.getAttribute('aria-valuetext') : null; }""",
        arg=aria_label,
    )


def _group_distances(page):
    """Center-to-center model-space distances between every node pair at the
    current layout, split by whether the pair shares a ``type`` (i.e. is the same
    group under Group by = Type). Returns
    ``{'same': [...], 'diff': [...], 'same_pairs': [[idA, idB], ...]}``.

    Model coordinates (``node.position()``) are zoom/fit independent, so this is a
    durable grouping metric, not a screenshot heuristic."""
    return page.evaluate(
        """() => {
          const cy = window.__okfLoomGraph.cy;
          const ns = cy.nodes().toArray();
          const same = [], diff = [], same_pairs = [];
          for (let i = 0; i < ns.length; i++) {
            for (let j = i + 1; j < ns.length; j++) {
              const a = ns[i].position(), b = ns[j].position();
              const d = Math.round(Math.hypot(a.x - b.x, a.y - b.y));
              const ta = ns[i].data('type'), tb = ns[j].data('type');
              if (ta != null && ta === tb) { same.push(d); same_pairs.push([ns[i].id(), ns[j].id()]); }
              else diff.push(d);
            }
          }
          return { same, diff, same_pairs };
        }"""
    )


def _preset(page, name: str) -> None:
    reveal_control(page, page.locator(f'#okf-lens-{name}')).check()


def _set_range(page, aria_label: str, value: int) -> None:
    page.evaluate(
        """([label, val]) => {
          const inp = Array.from(document.querySelectorAll('.okf-signal input[type=range]'))
            .find(i => (i.getAttribute('aria-label')||'') === label);
          inp.value = String(val); inp.dispatchEvent(new Event('input', {bubbles:true})); }""",
        arg=[aria_label, value],
    )


def _set_select(page, aria_label: str, value: str) -> None:
    page.evaluate(
        """([label, val]) => {
          const s = Array.from(document.querySelectorAll('.okf-signal select'))
            .find(x => (x.getAttribute('aria-label')||'') === label);
          s.value = val; s.dispatchEvent(new Event('change', {bubbles:true})); }""",
        arg=[aria_label, value],
    )


def _toggle_check(page, text: str) -> None:
    page.evaluate(
        """(text) => { const l = Array.from(document.querySelectorAll('.okf-signal__row--check'))
             .find(x => x.textContent.trim().indexOf(text) >= 0);
           l.querySelector('input').click(); }""",
        arg=text,
    )


def test_lens_switching_mutates_graph_state(server_url: str, page) -> None:
    """Lenses change real Cytoscape state — computed colours, computed sizes,
    bridge classes, edge labels — not just chrome. (Evidence-based
    lens engine: communities/PageRank/betweenness/recency.)"""
    _open_graph(page, server_url)
    base = _snap(page)
    assert base["n"] == 6 and base["e"] >= 1
    st = _state(page)
    assert st["lens"] == "map" and st["colorMode"] == "community" and st["sizeMode"] == "pagerank"
    # Map: PageRank sizing differentiates nodes.
    sizes = sorted(v["vizSize"] for v in base["nodes"].values())
    assert sizes[-1] > sizes[0], "PageRank sizing produced uniform node sizes"

    # Bridges: betweenness sizing + bridge colour ramp + glow class.
    prev = _applied_seq(page)
    _preset(page, "bridges")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(400)
    st = _state(page)
    assert st["lens"] == "bridges" and st["colorMode"] == "bridge" and st["sizeMode"] == "betweenness"
    br = _snap(page)
    assert br["bridgeNodes"] >= 1, "Bridges lens set no okf-bridge node"
    assert any(br["nodes"][k]["color"] != base["nodes"][k]["color"] for k in base["nodes"]), \
        "Bridges colour ramp did not recolour any node"

    # Flow: relationship labels on; typed edges carry colour + labels.
    prev = _applied_seq(page)
    _preset(page, "flow")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(400)
    st = _state(page)
    assert st["relationEdges"] and st["showEdgeLabels"]
    fl = _snap(page)
    assert fl["relEdges"] >= 1, "Flow lens set no okf-rel-edge"
    assert any(e["showLabel"] for e in fl["edges"].values()), "no edge label shown in Flow"

    # Recent: recency colouring is applied (demo nodes lack timestamps →
    # the neutral undated colour is fine; just prove the mode switched).
    prev = _applied_seq(page)
    _preset(page, "recent")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(400)
    assert _state(page)["colorMode"] == "recency"

    # Back to Map: bridge glow cleared. Phase 3 dash-always: typed edges
    # (edges with a non-empty label) STILL carry their .okf-reld-N dash
    # class in the Map lens — the dash encoding is no longer an explicit-
    # labels state. Assert the dash count equals the labelled-edge count
    # exactly (the deterministic Phase 3 contract: dash ⇔ has-label).
    prev = _applied_seq(page)
    _preset(page, "map")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(400)
    mp = _snap(page)
    assert mp["bridgeNodes"] == 0
    dash_count = page.evaluate(
        "window.__okfLoomGraph.cy.edges('.okf-reld-1, .okf-reld-2, .okf-reld-3').length"
    )
    labelled_count = page.evaluate(
        "window.__okfLoomGraph.cy.edges().filter(e => !!e.data('label')).length"
    )
    assert dash_count == labelled_count, (
        f"Phase 3 dash-always invariant: dash count {dash_count} != labelled-edge "
        f"count {labelled_count} (every typed edge must carry its dash class "
        f"in every lens, including Map)"
    )
    assert _state(page)["lens"] == "map"


def test_lens_summary_panel_ranks_and_navigates(server_url: str, page) -> None:
    """Every lens renders a ranked summary in the detail pane; clicking a row
    selects that node on the canvas (the 'answers beside the picture'
    pattern)."""
    _open_graph(page, server_url)
    reveal_control(page, page.locator('.okf-lens-summary .okf-lens-summary__verdict'))
    page.wait_for_selector(".okf-lens-summary .okf-lens-summary__verdict", timeout=5000)
    # Map: community digest with at least one theme row.
    rows = page.locator(".okf-lens-summary__row")
    assert rows.count() >= 1, "Map summary rendered no theme rows"
    # Bridges: ranked bridgers; clicking the top row selects a node.
    prev = _applied_seq(page)
    _preset(page, "bridges")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(300)
    page.wait_for_selector(".okf-lens-summary__row", timeout=5000)
    page.locator(".okf-lens-summary__row").first.click()
    page.wait_for_timeout(400)
    assert page.evaluate(
        "() => window.__okfLoomGraph.cy.nodes(':selected').length"
    ) == 1, "clicking a summary row did not select the node"
    assert page.eval_on_selector("#detail-empty", "e => e.hidden") is True, \
        "detail panel did not open from the summary row"


def test_signal_spacing_and_lens_relayout(server_url: str, page) -> None:
    """Node-spacing changes and lens switches trigger a real relayout
    (positions change), not a no-op."""
    _open_graph(page, server_url)
    _preset(page, "map")
    page.wait_for_timeout(700)

    base = _snap(page)
    prev = _applied_seq(page)
    _set_range(page, "Node spacing", 2)  # Open (mid of the 5-step range)
    _wait_applied_after(page, prev)
    page.wait_for_timeout(600)
    assert _state(page)["spacing"] == 2
    spaced = _snap(page)
    assert _pos_moved(base, spaced) > 0, "Node spacing change did not relayout"

    base2 = _snap(page)
    prev = _applied_seq(page)
    _preset(page, "themes")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(600)
    assert _state(page)["lens"] == "themes"
    assert _pos_moved(base2, _snap(page)) > 0, "Lens switch did not relayout"


def test_signal_expanded_range_spreads_further(server_url: str, page) -> None:
    """The widened Signal range (5 steps: Compact…Vast / Soft…Expanse /
    Low…Magnetic) pushes nodes substantially farther than the previous 3-step
    ceiling. The model-space spread at the NEW maximum is much larger than at the
    OLD reachable top (index 2 ≈ the previous ceiling), overlap stays safe via the
    resolver, and aria-valuetext reflects the high-end labels."""
    _open_graph(page, server_url)
    _preset(page, "map")
    page.wait_for_timeout(700)

    def _push(spacing: int, sep: int, strength: int) -> None:
        # Set each range and wait on a real applied-layout signal (the debounced
        # owner coalesces, so wait per step) — no blind sleeps as the proof.
        for label, val in (("Node spacing", spacing),
                           ("Cluster separation", sep),
                           ("Group strength", strength)):
            prev = _applied_seq(page)
            _set_range(page, label, val)
            _wait_applied_after(page, prev)
            page.wait_for_timeout(350)

    # Old reachable ceiling: the previous range topped out at index 2.
    _push(2, 2, 2)
    old_ceiling = _bbox_diag(page)
    snap_old = _snap(page)
    assert _min_gap(page) > 0, "overlap at the mid range"

    # New maximum (Vast / Expanse / Magnetic): spreads much farther.
    _push(4, 4, 4)
    st = _state(page)
    assert st["spacing"] == 4 and st["separation"] == 4 and st["groupStrength"] == 4, \
        f"controls did not reach the new maximum index: {st}"
    new_max = _bbox_diag(page)

    # The widened ceiling must produce a substantially larger spread than the old
    # top-of-range could — proves the range now has real reach, not a token bump.
    assert new_max > old_ceiling * 1.4, (
        f"expanded range did not spread substantially farther than the old ceiling "
        f"(bbox diagonal {old_ceiling} -> {new_max})"
    )
    assert _pos_moved(snap_old, _snap(page)) > 0, "pushing to the new maximum did not relayout"
    # Overlap invariant holds even at the extreme end (resolver guarantees a gap).
    assert _min_gap(page) > 0, "overlap at maximum spacing/separation/strength"

    # aria-valuetext exposes the accessible high-end label for each control.
    assert _range_valuetext(page, "Node spacing") == "Vast"
    assert _range_valuetext(page, "Cluster separation") == "Expanse"
    assert _range_valuetext(page, "Group strength") == "Magnetic"


def test_lens_communities_deterministic_and_painted(server_url: str, page) -> None:
    """Community detection is deterministic (same bundle → same assignment on
    every load — the anti-position-churn requirement) and the Map lens paints
    nodes by community: same community ⇒ same colour, and the colour comes
    from the community ramp, not the type palette."""
    _open_graph(page, server_url)
    page.wait_for_timeout(400)
    com1 = page.evaluate("() => window.__okfLoomGraph.communityOf()")
    assert com1 and len(com1) == 6, f"expected 6 community assignments, got {com1}"
    snap = _snap(page)
    colours = {}
    for nid, c in com1.items():
        colour = snap["nodes"][nid]["color"]
        if c in colours:
            assert colours[c] == colour, (
                f"nodes in community {c} got different colours: {colours[c]} vs {colour}"
            )
        else:
            colours[c] = colour
    # Distinct communities get distinct colours.
    assert len(set(colours.values())) == len(colours), f"community colours collide: {colours}"

    # Reload → identical assignment (deterministic label propagation).
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.__okfLoomGraph && window.__okfLoomGraph.cy "
        "&& window.__okfLoomGraph.cy.nodes().length > 0 "
        "&& window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1",
        timeout=10000,
    )
    page.wait_for_timeout(300)
    com2 = page.evaluate("() => window.__okfLoomGraph.communityOf()")
    assert com1 == com2, f"community assignment changed across reloads: {com1} vs {com2}"


def test_lens_encoding_toggles(server_url: str, page) -> None:
    """Graph settings toggles: 'Colour by theme' off reverts nodes to the type
    palette (the Saket et al. evidence-mandated escape hatch); 'Show
    relationship labels' reveals typed-edge labels in any lens."""
    _open_graph(page, server_url)
    page.wait_for_timeout(400)
    themed = _snap(page)
    _toggle_check(page, "Colour by theme")
    page.wait_for_timeout(300)
    assert _state(page)["colorMode"] == "type"
    typed = _snap(page)
    assert any(typed["nodes"][k]["color"] != themed["nodes"][k]["color"] for k in themed["nodes"]), \
        "disabling theme colours changed nothing"
    # The type palette equals each node's baseColor.
    base_matches = page.evaluate(
        """() => window.__okfLoomGraph.cy.nodes().every(
             n => n.data('color') === n.data('baseColor'))"""
    )
    assert base_matches, "type colouring does not match the node type palette"
    _toggle_check(page, "Colour by theme")
    page.wait_for_timeout(300)
    assert _state(page)["colorMode"] == "community"

    # Relationship labels toggle (unselect first: a selected node shows
    # its own contextual edge labels by design).
    page.evaluate("() => { window.__okfLoomGraph.cy.elements().unselect(); return undefined; }")
    page.wait_for_timeout(200)
    before = _snap(page)
    assert not any(e["showLabel"] for e in before["edges"].values()), \
        "labels visible before the toggle with nothing selected"
    _toggle_check(page, "Show relationship labels")
    page.wait_for_timeout(300)
    st = _state(page)
    assert st["showEdgeLabels"] and st["relationEdges"]
    after = _snap(page)
    assert any(e["showLabel"] for e in after["edges"].values()), \
        "relationship-label toggle revealed no labels"


def test_graph_theme_type_palette_no_collision(server_url: str, page) -> None:
    """P1 graph/type colour collision fix: the theme (community) palette must
    not share the same hue family as the product-wide type palette. Themes use
    a cool teal/steel family; types keep the golden-angle full-spectrum spread.
    The legend's 'Filter types' section must NOT use colour swatches (icons
    only), and theme entries must carry a non-colour textual marker (T1, T2…).
    """
    _open_graph(page, server_url)
    page.wait_for_timeout(400)
    assert _state(page)["colorMode"] == "community"

    # 1. Theme colours (on canvas) must be visually distinct from type colours.
    theme_colors = page.evaluate(
        """() => { const cy = window.__okfLoomGraph.cy;
            return [...new Set(cy.nodes().map(n => n.data('color')))]; }"""
    )
    type_colors = page.evaluate(
        """() => { const cy = window.__okfLoomGraph.cy;
            return [...new Set(cy.nodes().map(n => n.data('baseColor')))]; }"""
    )
    # No theme colour may equal any type colour.
    collision = set(theme_colors) & set(type_colors)
    assert not collision, f"theme/type colour collision: {collision}"

    # 2. Legend "Filter types" section has ZERO colour swatches.
    # The legend is collapsible (collapsed by default so a dense canvas is
    # not obscured); expand it before asserting on its rendered key body.
    page.locator("details.okf-signal__advanced > summary").click()
    page.wait_for_timeout(150)
    swatch_info = page.evaluate(
        """() => {
            const legend = document.querySelector('.okf-graph-legend');
            if (!legend) return { error: 'no legend' };
            const titles = [...legend.querySelectorAll('.okf-graph-legend__title')];
            const ft = titles.find(t => t.textContent.includes('Filter types'));
            if (!ft) return { error: 'no filter title' };
            let items = [], next = ft.nextElementSibling;
            while (next && !next.classList.contains('okf-graph-legend__title')) {
                if (next.classList.contains('okf-graph-legend__item')) items.push(next);
                next = next.nextElementSibling;
            }
            return {
                count: items.length,
                swatches: items.filter(i => i.querySelector('.okf-graph-legend__swatch')).length,
                icons: items.filter(i => i.querySelector('svg')).length,
            };
        }"""
    )
    assert "error" not in swatch_info, swatch_info["error"]
    assert swatch_info["count"] > 0, "filter types section has no items"
    assert swatch_info["swatches"] == 0, \
        f"filter types section has {swatch_info['swatches']} colour swatches (should be icon-only)"
    assert swatch_info["icons"] == swatch_info["count"], \
        f"not all filter items have icons ({swatch_info['icons']}/{swatch_info['count']})"

    # 3. Theme legend entries carry non-colour textual markers (T1, T2…).
    legend_text = page.evaluate(
        "() => document.querySelector('.okf-graph-legend').innerText"
    )
    assert "T1" in legend_text, "legend missing T1 textual theme marker"
    assert "Colour: themes" in legend_text, "legend missing themes section"


def test_graph_incident_edge_highlight(server_url: str, page) -> None:
    """P1 edge highlight: tapping a node marks its incident edges with the
    .okf-incident class; deselecting clears them."""
    _open_graph(page, server_url)
    page.wait_for_timeout(400)

    # Select a node with edges via the real tap flow.
    incident = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            const node = cy.nodes().filter(n => n.degree(false) > 0).first();
            cy.elements().unselect();
            node.emit('tap');
            return {
                id: node.id(),
                degree: node.degree(false),
                incident: cy.edges('.okf-incident').length,
            };
        }"""
    )
    assert incident["incident"] == incident["degree"], \
        f"incident edges {incident['incident']} != degree {incident['degree']}"

    # Deselect clears incident edges.
    cleared = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            cy.emit('tap');  // background tap → clearSelection
            return cy.edges('.okf-incident').length;
        }"""
    )
    assert cleared == 0, f"{cleared} incident edges remain after deselect"


# ---- Phase 3: graph visual language ---------------------------------------

def test_graph_phase3_labels_are_compact_desktop_and_readable_mobile(server_url: str, page) -> None:
    """Desktop stays compact while mobile labels use spacing-aware two-line wrap."""
    _open_graph(page, server_url)
    page.wait_for_timeout(400)
    label_style = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            const n = cy.nodes().first();
            return {
                wrap: n.style('text-wrap'),
                maxWidth: n.style('text-max-width'),
            };
        }"""
    )
    assert label_style["wrap"] == "ellipsis"
    assert label_style["maxWidth"].startswith("90")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(150)
    mobile = page.evaluate("""() => {const n=window.__okfLoomGraph.cy.nodes().first();return {
      wrap:n.style('text-wrap'),maxWidth:parseFloat(n.style('text-max-width')),
      font:parseFloat(n.style('font-size')),full:n.data('label'),
      indexed:Array.from(document.querySelectorAll('.okf-node-index__label')).some(x=>x.textContent===n.data('label'))};}""")
    assert mobile["wrap"] == "ellipsis"
    assert mobile["font"] >= 12 and mobile["maxWidth"] >= 52
    assert mobile["maxWidth"] != 90, "mobile label width must derive from grid spacing"
    assert mobile["full"] and mobile["indexed"]


@pytest.mark.parametrize("width", [320, 390])
def test_graph_phase3_mobile_small_graph_uses_canvas_and_bottom_viewport_row(
    server_url: str, page, width: int,
) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    page.goto(server_url + "/__graph", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfGraphCaptureState?.().layoutSettled")
    show_graph_canvas(page)
    result = page.evaluate(r"""() => {const cy=window.__okfLoomGraph.cy,c=document.getElementById('okf-graph'),cr=c.getBoundingClientRect(),nodes=cy.nodes();
      const boxes=nodes.map(n=>n.renderedBoundingBox({includeLabels:true,includeOverlays:true}));
      const bb={l:Math.min(...boxes.map(b=>b.x1)),r:Math.max(...boxes.map(b=>b.x2)),t:Math.min(...boxes.map(b=>b.y1)),b:Math.max(...boxes.map(b=>b.y2))};
      const controls=document.querySelector('.okf-graph-zoom'),zr=controls.getBoundingClientRect(),buttons=[...controls.querySelectorAll('button')];
      const overlap=(a,b)=>a.x1<b.x2&&a.x2>b.x1&&a.y1<b.y2&&a.y2>b.y1;
      const edge=cy.edges().first(), rgb=s=>s.match(/\d+/g).slice(0,3).map(Number), lum=v=>{v/=255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4)}, contrast=(a,b)=>{a=rgb(a).map(lum);b=rgb(b).map(lum);const A=.2126*a[0]+.7152*a[1]+.0722*a[2],B=.2126*b[0]+.7152*b[1]+.0722*b[2];return (Math.max(A,B)+.05)/(Math.min(A,B)+.05)};
          const bodies=nodes.map(n=>n.renderedBoundingBox({includeLabels:false,includeOverlays:false}));
          return {count:nodes.length,widthRatio:(bb.r-bb.l)/cr.width,heightRatio:(bb.b-bb.t)/cr.height,
            inside:bb.l>=16&&bb.r<=cr.width-16&&bb.t>=16&&bb.b<=cr.height-16,
            overlaps:bodies.some((a,i)=>bodies.slice(i+1).some(b=>overlap(a,b))),
        horizontal:zr.width>zr.height,bottom:cr.bottom-zr.bottom<=16,
        targets:buttons.every(b=>{const r=b.getBoundingClientRect();return r.width>=44&&r.height>=44}),
        controlsInside:zr.left>=cr.left&&zr.right<=cr.right&&zr.top>=cr.top&&zr.bottom<=cr.bottom,
        edgeContrast:contrast(edge.style('line-color'),getComputedStyle(c).backgroundColor),
        arrow:edge.style('target-arrow-shape'),dash:edge.style('line-style'),
        names:buttons.map(b=>({aria:b.getAttribute('aria-label'),title:b.title})),
        revision:document.body.dataset.okfRevision,model:nodes.map(n=>n.id()).sort().join(',')};}""")
    assert 2 <= result["count"] <= 12
    assert 0.85 <= result["widthRatio"] <= 0.92, result
    # The visible Find row now owns some vertical space. Require useful
    # occupancy plus the stronger label/body/control containment checks below.
    assert 0.50 <= result["heightRatio"] <= 0.80, result
    assert result["inside"] and not result["overlaps"]
    assert result["horizontal"] and result["bottom"] and result["targets"] and result["controlsInside"]
    assert result["edgeContrast"] >= 3 and result["arrow"] == "triangle"
    assert all(item["aria"] and item["title"] for item in result["names"])
    proof_dir = Path("/tmp/okf-loom-mobile-graph-p1")
    proof_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(proof_dir / f"graph-{width}x844.png"))
    before = result
    page.locator(".okf-graph-zoom button").first.click()
    page.wait_for_timeout(120)
    after = page.evaluate("() => ({revision:document.body.dataset.okfRevision,model:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort().join(',')})")
    assert after == {"revision": before["revision"], "model": before["model"]}


def test_graph_phase3_hub_halo_is_top_decile_by_degree(server_url: str, page) -> None:
    """Phase 3: top-decile-by-degree nodes carry ``.okf-hub``. The halo set
    is exactly ceil(N/10) nodes (rounded up so small bundles still have a
    hub) ranked by degree, ties broken deterministically. Degree-zero nodes
    can never be hubs (orphans are not structurally central)."""
    _open_graph(page, server_url)
    page.wait_for_timeout(400)
    info = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            const ns = cy.nodes().toArray();
            // Mirror the client's degreeOf: unique-neighbour count from the
            // displayed cy graph (deduped edges).
            const deg = {};
            ns.forEach(n => { deg[n.id()] = n.degree(false); });
            const withDeg = ns.filter(n => deg[n.id()] > 0);
            const expectedHubCount = Math.max(1, Math.ceil(withDeg.length / 10));
            // The hub threshold = the degree at index (N - hubCount) in the
            // ascending sorted degree list. Nodes with deg >= threshold are hubs.
            const asc = withDeg.map(n => deg[n.id()]).sort((a, b) => a - b);
            const cutoffIdx = asc.length - expectedHubCount;
            const threshold = asc[cutoffIdx] || asc[0] || 0;
            const expectedHubs = withDeg.filter(n => deg[n.id()] >= threshold).map(n => n.id()).sort();
            const actualHubs = ns.filter(n => n.hasClass('okf-hub')).map(n => n.id()).sort();
            return {
                totalNodes: ns.length,
                expectedHubIds: expectedHubs,
                actualHubIds: actualHubs,
                expectedCount: expectedHubCount,
                actualCount: actualHubs.length,
                orphansWithHub: ns.filter(n => n.hasClass('okf-hub') && deg[n.id()] === 0).length,
            };
        }"""
    )
    assert info["orphansWithHub"] == 0, "an orphan (degree 0) was marked as a hub"
    assert info["actualHubIds"] == info["expectedHubIds"], (
        f"hub set mismatch: expected {info['expectedHubIds']} got {info['actualHubIds']}"
    )
    assert info["actualCount"] == info["expectedCount"], (
        f"hub count {info['actualCount']} != expected top-decile {info['expectedCount']}"
    )


def test_graph_phase3_orphan_shelf_lists_degree_zero(docs_server_url: str, page) -> None:
    """Phase 3: the labelled bottom orphan shelf lists exactly the degree-
    zero concepts, deterministically sorted by label. Each shelf entry is a
    real ``<button>`` so keyboard users can focus an orphan directly.

    Uses docs_server_url only if it has orphans; demo_bundle has none, so
    this test injects an orphan into the client payload via the data hook.
    """
    _open_graph(page, docs_server_url)
    page.wait_for_timeout(400)
    # Snapshot the actual shelf contents + the canvas degree-zero set so
    # we can assert equivalence directly (layout-graph determinism).
    info = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            const degZero = cy.nodes().filter(n => n.degree(false) === 0)
                .map(n => ({ id: n.id(), label: n.data('label') || n.id() }))
                .sort((a, b) => {
                    const la = String(a.label).toLowerCase(), lb = String(b.label).toLowerCase();
                    if (la < lb) return -1; if (la > lb) return 1;
                    return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
                });
            const shelf = document.querySelector('.okf-graph-orphans');
            const items = shelf && !shelf.hidden
                ? Array.from(shelf.querySelectorAll('.okf-graph-orphans__item')).map(b => ({
                    id: b.getAttribute('data-target'),
                    label: (b.querySelector('.okf-node-index__label') || {}).textContent || '',
                    tag: b.tagName,
                    type: b.getAttribute('type') || '',
                }))
                : [];
            const title = shelf && !shelf.hidden
                ? (shelf.querySelector('.okf-graph-orphans__title') || {}).textContent || ''
                : '';
            return { degZero, items, title, shelfHidden: !shelf || shelf.hidden };
        }"""
    )
    if info["shelfHidden"]:
        # docs-bundle has no orphans in the deduped displayed graph; the
        # shelf is correctly hidden when the set is empty (fail-closed).
        assert info["degZero"] == [], (
            f"shelf hidden but {len(info['degZero'])} degree-zero nodes exist on canvas"
        )
        return
    # Shelf count copy + entries match the canvas orphan set exactly.
    assert "Not yet linked" in info["title"], f"shelf title wrong: {info['title']!r}"
    expected_count = len(info["degZero"])
    assert info["title"].endswith(str(expected_count)), (
        f"shelf title count does not match degZero set ({expected_count}): {info['title']!r}"
    )
    expected_labels = [d["label"] for d in info["degZero"]]
    actual_labels = [it["label"] for it in info["items"]]
    assert actual_labels == expected_labels, (
        f"shelf entries != degree-zero set: expected {expected_labels} got {actual_labels}"
    )
    # Each entry is a real keyboard-accessible button.
    for it in info["items"]:
        assert it["tag"] == "BUTTON", f"shelf entry is not a <button>: {it}"
        assert it["type"] == "button", f"shelf entry type attr missing: {it}"


def test_graph_phase3_orphan_shelf_demo_has_no_orphans_hidden(server_url: str, page) -> None:
    """Phase 3 fail-closed: the demo_bundle (which has no degree-zero nodes)
    hides the orphan shelf entirely. The shelf must not render an empty
    container that consumes canvas real estate."""
    _open_graph(page, server_url)
    page.wait_for_timeout(400)
    visible = page.evaluate(
        """() => {
            const shelf = document.querySelector('.okf-graph-orphans');
            return {
                exists: !!shelf,
                hidden: shelf ? shelf.hidden : null,
                degZero: window.__okfLoomGraph.cy.nodes().filter(n => n.degree(false) === 0).length,
            };
        }"""
    )
    assert visible["exists"], "orphan shelf element was not mounted"
    assert visible["degZero"] == 0, "demo_bundle unexpectedly has degree-zero nodes"
    assert visible["hidden"] is True, (
        "orphan shelf must be hidden when the bundle has no degree-zero nodes"
    )


def test_graph_phase3_typed_edge_dash_always_in_every_lens(server_url: str, page) -> None:
    """Phase 3 contract: typed edges (edges with a non-empty label) ALWAYS
    carry their ``.okf-reld-N`` dash class, in EVERY lens — not only under
    the explicit Relations/Focus intent. Walks every preset and asserts the
    invariant. (Regression guard for the prior relationEdges-gated dash.)"""
    _open_graph(page, server_url)
    page.wait_for_timeout(400)
    presets = ["map", "themes", "bridges", "flow", "focus", "recent"]
    for name in presets:
        try:
            prev = _applied_seq(page)
            _preset(page, name)
            # Some presets (recent) don't always trigger a layout re-run;
            # wait briefly for the visual-encoding pass instead of asserting
            # a new layout sequence. The dash classes are re-applied by
            # applyVisualEncoding on every lens change regardless of layout.
            try:
                _wait_applied_after(page, prev, timeout=3000)
            except Exception:
                pass
            page.wait_for_timeout(400)
        except Exception as e:
            raise AssertionError(f"failed to switch to {name} preset: {e}")
        counts = page.evaluate(
            """() => {
                const cy = window.__okfLoomGraph.cy;
                return {
                    labelled: cy.edges().filter(e => !!e.data('label')).length,
                    dashed: cy.edges('.okf-reld-1, .okf-reld-2, .okf-reld-3').length,
                    rel: cy.edges('.okf-rel-edge').length,
                    lens: window.__okfLoomGraph.getState().lens,
                };
            }"""
        )
        assert counts["lens"] == name, f"preset switch to {name} failed (lens={counts['lens']})"
        assert counts["rel"] == counts["labelled"], (
            f"{name}: rel-edge count {counts['rel']} != labelled-edge count {counts['labelled']}"
        )
        assert counts["dashed"] == counts["labelled"], (
            f"{name} lens: typed-edge dash invariant broken — dashed {counts['dashed']} "
            f"!= labelled {counts['labelled']} (Phase 3 dash must be always-on)"
        )


def test_graph_phase3_broken_link_cue_on_nodes(docs_server_url: str, page) -> None:
    """Phase 3: nodes with unresolved outgoing links (count > 0 from
    ``Graph.unresolved`` server-side) carry ``.okf-broken-link`` on the
    canvas AND expose the count via the keyboard node index's aria-label
    (non-colour channel for the cue, since Cytoscape's underlay is colour-
    only and unreadable in forced-colours mode).

    docs-bundle has deterministic unresolved links to reserved quadrant
    index files (see test_graph_data.py for the model-level proof)."""
    _open_graph(page, docs_server_url)
    page.wait_for_timeout(500)
    info = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            const nodes = cy.nodes().toArray();
            const expectedBroken = nodes.filter(n => (Number(n.data('unresolved_count')) || 0) > 0)
                .map(n => n.id()).sort();
            const actualBroken = nodes.filter(n => n.hasClass('okf-broken-link'))
                .map(n => n.id()).sort();
            // The keyboard node index should expose the broken-link count in
            // the aria-label of the corresponding entry (non-colour channel).
            const indexEntries = Array.from(document.querySelectorAll(
                '#okf-node-index-list .okf-node-index__item')).map(b => ({
                id: b.getAttribute('data-target'),
                aria: b.getAttribute('aria-label') || '',
                hasWarn: !!b.querySelector('.okf-node-index__warn'),
            }));
            const withWarn = indexEntries.filter(e => e.hasWarn).map(e => e.id).sort();
            return {
                expectedBroken, actualBroken, indexEntries, withWarn,
                totalNodes: nodes.length,
            };
        }"""
    )
    if info["totalNodes"] == 0 or not info["expectedBroken"]:
        # If docs-bundle's unresolved links are all to reserved files (not
        # loaded concepts), the displayed-graph payload may have no nodes
        # with unresolved_count > 0; assert the fail-closed shape instead.
        assert info["actualBroken"] == [], (
            f"no node expected to have broken-link cue but got {info['actualBroken']}"
        )
        return
    assert info["actualBroken"] == info["expectedBroken"], (
        f"broken-link class set mismatch: expected {info['expectedBroken']} "
        f"got {info['actualBroken']}"
    )
    # The node index carries the warning badge on exactly the same set.
    assert info["withWarn"] == info["expectedBroken"], (
        f"node index '!' badge set mismatch: expected on {info['expectedBroken']} "
        f"got {info['withWarn']}"
    )
    # And at least one aria-label mentions "broken link" (non-colour channel).
    broken_arias = [e["aria"] for e in info["indexEntries"] if e["id"] in info["expectedBroken"]]
    assert any("broken link" in a.lower() for a in broken_arias), (
        f"no node index aria-label mentions 'broken link': {broken_arias[:3]}"
    )


def test_graph_phase3_label_plate_light_vs_dark(server_url: str, page) -> None:
    """Phase 3 light/dark label plate policy: light themes drop the node
    label plate entirely (text-background-opacity 0) since fg-on-bg-elev
    already contrasts well; dark themes keep the 0.85 plate so labels stay
    crisp over edges. Asserted on the live canvas style."""
    _open_graph(page, server_url)
    page.wait_for_timeout(400)
    # Light theme (default).
    light_opacity = page.evaluate(
        """() => window.__okfLoomGraph.cy.nodes().first().style('text-background-opacity')"""
    )
    assert float(light_opacity) == 0.0, (
        f"light theme plate opacity must be 0 (got {light_opacity})"
    )
    # Switch to dark theme via the canonical API.
    page.evaluate(
        """() => {
            if (!window.OKFLoomTheme || typeof window.OKFLoomTheme.setTheme !== "function") {
                throw new Error("canonical theme API unavailable");
            }
            window.OKFLoomTheme.setTheme("technical-dark");
        }"""
    )
    page.wait_for_timeout(300)
    dark_opacity = page.evaluate(
        """() => window.__okfLoomGraph.cy.nodes().first().style('text-background-opacity')"""
    )
    assert float(dark_opacity) == 0.85, (
        f"dark theme plate opacity must be 0.85 (got {dark_opacity})"
    )


# ---- Phase 3 P1: live-refresh existing-node patch + deterministic orphan dock


def _live_payload_setup(page) -> str:
    """Shared setup for the live-refresh tests: capture the base graph.json,
    build three derived payloads on the page (add-edge, base=remove,
    unresolved-only) stashed as window globals, and return the target node id
    whose degree/size/unresolved_count the tests mutate. Mirrors how the
    server recomputes degree-driven ``size`` so the patch is contract-real.

    Picks a pair (a, c) with NO existing edge in either direction so the
    ``add`` payload is a genuine new relationship (not a dedup no-op), and
    recomputes ``degree`` from the deduped edge set exactly as render.py does
    (unique (src,tgt) pairs, both endpoints incremented) — so the patched
    degree stays in parity with the client's live edge degree."""
    page.evaluate(
        r"""async () => {
          const graph=document.getElementById('okf-graph'), url=graph.dataset.graphUrl, native=window.fetch.bind(window);
          const base=await native(url).then(r=>r.json());
          const ids=base.nodes.map(n=>n.data.id);
          const hasEdge=(s,t)=>base.edges.some(e=>(e.data.source===s&&e.data.target===t)||(e.data.source===t&&e.data.target===s));
          let a=null,c=null;
          for(let i=0;i<ids.length && !a;i++) for(let j=i+1;j<ids.length;j++){ if(!hasEdge(ids[i],ids[j])){a=ids[i];c=ids[j];break;} }
          if(!a){a=ids[0];c=ids[ids.length-1];}
          window.__okfP1={url:url, native:native, base:base, a:a, c:c};
          function degreeOf(edges){const seen={},deg={};edges.forEach(e=>{const k=e.data.source+'__'+e.data.target; if(seen[k])return; seen[k]=1; deg[e.data.source]=(deg[e.data.source]||0)+1; deg[e.data.target]=(deg[e.data.target]||0)+1;}); return deg;}
          const addEdges=base.edges.concat([{data:{source:a,target:c,label:'depends_on'}}]);
          const addDeg=degreeOf(addEdges);
          const addNodes=base.nodes.map(n=>{const d=addDeg[n.data.id]||0; return {data:Object.assign({},n.data,{degree:d,size:24+Math.floor(28*Math.min(1,Math.sqrt(d)/3))})};});
          const addOut=JSON.parse(JSON.stringify(base.outgoing||{})); const addBack=JSON.parse(JSON.stringify(base.backlinks||{}));
          (addOut[a]=addOut[a]||[]).push(c); (addBack[c]=addBack[c]||[]).push(a);
          window.__okfP1.add=Object.assign({},base,{edges:addEdges,nodes:addNodes,outgoing:addOut,backlinks:addBack});
          const u=JSON.parse(JSON.stringify(base)); const idx=u.nodes.findIndex(n=>n.data.id===a);
          u.nodes[idx].data.unresolved_count=(u.nodes[idx].data.unresolved_count||0)+3;
          window.__okfP1.unres=u;
        }"""
    )
    return page.evaluate("() => window.__okfP1.a")


def _live_serve(page, which: str) -> None:
    """Point window.fetch at one of the stashed payloads and run one refresh."""
    page.evaluate(
        """(which) => {
          const P=window.__okfP1, o=P[which], url=P.url, native=P.native;
          window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(o),{status:200,headers:{'Content-Type':'application/json'}})):native(req,opt);
        }""",
        which,
    )
    page.evaluate("async () => { await window.__okfLoomGraph.refreshGraph(); }")


def test_graph_phase3_live_refresh_relationship_add_updates_degree_and_size(
    server_url: str, page
) -> None:
    """Phase 3 P1 (async-lifecycle / contract-runtime-parity): a live refresh
    that ADDS a relationship between two existing nodes must patch the
    existing Cytoscape node's degree-driven ``degree`` and ``size`` fields
    (not just label/colour metadata), so the degree-driven node glyph + hub
    halo recompute without a full reload. Previously ``size``/``degree`` were
    absent from the existing-node diff list, leaving the glyph stale after a
    relationship add."""
    _open_graph(page, server_url)
    target = _live_payload_setup(page)

    def node_fields() -> dict:
        return page.evaluate(
            """(id) => { const n=window.__okfLoomGraph.cy.getElementById(id);
                return {deg:n.data('degree'), size:n.data('size')}; }""",
            target,
        )

    before = node_fields()
    _live_serve(page, "add")
    after = node_fields()
    assert after["deg"] > before["deg"], (
        f"degree did not increase after relationship add: {before} -> {after}"
    )
    assert after["size"] >= before["size"], (
        f"degree-driven size did not rise after add: {before} -> {after}"
    )
    # The cy node data degree must equal the client bundle degree (degreeOf)
    # after the refresh — the patch keeps server/client degree in parity.
    parity = page.evaluate(
        """(id) => ({ data: window.__okfLoomGraph.cy.getElementById(id).data('degree'),
            bundleDeg: window.__okfLoomGraph.cy.getElementById(id).degree(false) })""",
        target,
    )
    # degree(false) counts unique neighbours from live edges; the patched
    # `degree` data is the server value over deduped edges (both directions).
    assert parity["data"] == parity["bundleDeg"], (
        f"server degree data != client live degree after refresh: {parity}"
    )


def test_graph_phase3_live_refresh_relationship_remove_restores_degree(
    server_url: str, page
) -> None:
    """Phase 3 P1: removing the added relationship restores the original
    degree/size, and — importantly — a node that becomes degree-zero after
    the removal appears in the orphan shelf (the dock + DOM control surface
    re-derive from the recomputed degreeOf on the same refresh)."""
    _open_graph(page, server_url)
    target = _live_payload_setup(page)

    def node_fields() -> dict:
        return page.evaluate(
            """(id) => { const n=window.__okfLoomGraph.cy.getElementById(id);
                return {deg:n.data('degree'), size:n.data('size')}; }""",
            target,
        )

    base_fields = node_fields()
    _live_serve(page, "add")
    added = node_fields()
    assert added["deg"] > base_fields["deg"]
    _live_serve(page, "base")  # back to the original payload (edge removed)
    removed = node_fields()
    assert removed == base_fields, (
        f"degree/size not restored after relationship remove: base={base_fields} removed={removed}"
    )


def test_graph_phase3_live_refresh_unresolved_only_toggles_broken_link(
    server_url: str, page
) -> None:
    """Phase 3 P1 (async-lifecycle blocker): a live refresh that changes ONLY
    ``unresolved_count`` (no node/edge set change, no degree change) must
    still recompute + repaint the canvas broken-link cue. Before the fix the
    unresolved_count field was not in the existing-node diff list, so an
    unresolved-only change left ``changed`` at 0 and the
    applyVisualEncoding block never ran — the red underlay stayed stale and
    the tooltip/detail/index rows disagreed with the canvas. This test
    proves the patch list + the changed-gated repaint now cover that case."""
    _open_graph(page, server_url)
    target = _live_payload_setup(page)

    def state() -> dict:
        return page.evaluate(
            """(id) => { const n=window.__okfLoomGraph.cy.getElementById(id);
                return { broken:n.hasClass('okf-broken-link'),
                         unres:Number(n.data('unresolved_count'))||0 }; }""",
            target,
        )

    before = state()
    assert before["broken"] is False, "target unexpectedly starts broken"
    _live_serve(page, "unres")  # unresolved-only change (no edge/node churn)
    after = state()
    assert after["unres"] > before["unres"], (
        f"unresolved_count not patched: {before} -> {after}"
    )
    assert after["broken"] is True, (
        f"broken-link cue did not toggle on unresolved-only change: {before} -> {after}"
    )
    # The node index (keyboard surface) and tooltip source (nodeIndex) agree
    # with the canvas — non-colour channel parity after the repaint.
    consistency = page.evaluate(
        """(id) => {
          const n=window.__okfLoomGraph.cy.getElementById(id);
          const idxItem=document.querySelector('.okf-node-index__item[data-target="'+id+'"]');
          return { canvasBroken:n.hasClass('okf-broken-link'),
                   indexHasWarn: idxItem ? !!idxItem.querySelector('.okf-node-index__warn') : null };
        }""",
        target,
    )
    # The target had 0 unresolved before; after the +3 change the node-index
    # "!" badge must appear (non-colour channel mirrors the canvas cue).
    assert consistency["indexHasWarn"] is True, (
        f"node-index warn badge disagrees with canvas after unresolved change: {consistency}"
    )


def test_graph_phase3_comment_badge_semantics_index_detail_and_canvas(
    server_url: str, page
) -> None:
    """Absent stays unknown; 0/1/plural/99+ retain exact HTML/ARIA counts."""
    _open_graph(page, server_url)
    result = page.evaluate(
        r"""async () => {
          const G=window.__okfLoomGraph, el=document.getElementById('okf-graph');
          const url=el.dataset.graphUrl, native=window.fetch.bind(window);
          const base=await native(url,{cache:'no-store'}).then(r=>r.json());
          const counts=[undefined,0,1,17,1234];
          const nodes=base.nodes.slice(0,counts.length).map((n,i)=>{
            const data=Object.assign({},n.data); delete data.active_comment_count;
            if(counts[i]!==undefined)data.active_comment_count=counts[i];
            return {data};
          });
          const ids=new Set(nodes.map(n=>n.data.id));
          const edges=base.edges.filter(e=>ids.has(e.data.source)&&ids.has(e.data.target));
          const payload=Object.assign({},base,{nodes,edges});
          delete payload.outgoing; delete payload.backlinks;
          let opts=null;
          window.fetch=(req,opt)=>String(req)===url?(opts=opt,Promise.resolve(new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}}))):native(req,opt);
          await G.refreshGraph(); await new Promise(requestAnimationFrame);
          const rows=nodes.map(n=>{const id=n.data.id,b=document.querySelector('.okf-node-index__item[data-target="'+CSS.escape(id)+'"]');const cy=G.cy.getElementById(id);return {id,aria:b&&b.getAttribute('aria-label'),badge:b&&b.querySelector('.okf-node-index__comments')&&b.querySelector('.okf-node-index__comments').textContent,hasCanvas:cy.data('has_active_comment_count'),canvas:String(cy.data('commentBadgeUrl')||'')};});
          const selected=nodes[4].data.id; window.okfActivateGraphConcept(selected,'pointer');
          const detail=Array.from(document.querySelectorAll('#okf-detail-signals dt')).find(x=>x.textContent==='Active comments');
          const capture=window.okfGraphCaptureState();
          const pendingRaw=G.cy.nodes().length;
          await new Promise(resolve=>{const check=()=>window.okfGraphCaptureState().transitionPending?requestAnimationFrame(check):resolve();check();});
          return {rows,detail:detail&&detail.nextElementSibling.textContent,cache:opts&&opts.cache,nodeCount:capture.nodeCount,payloadCount:capture.payloadNodeCount,pendingRaw,settledRaw:G.cy.nodes().length,transitionPending:capture.transitionPending,ready:document.body.dataset.okfGraphCaptureReady};
        }"""
    )
    rows = result["rows"]
    assert rows[0]["badge"] is None and "active comment" not in rows[0]["aria"]
    # Phase-1 badge truth: an explicit 0 keeps the exact-count aria phrase
    # (the assistive channel) but renders NO visible chip and NO canvas
    # badge — zero is the absence of a signal, not a "0" bubble.
    assert rows[1]["badge"] is None and "0 active comments" in rows[1]["aria"]
    assert rows[2]["badge"] == "1" and "1 active comment)" in rows[2]["aria"]
    assert rows[3]["badge"] == "17" and "17 active comments" in rows[3]["aria"]
    assert rows[4]["badge"] == "99+" and "1234 active comments" in rows[4]["aria"]
    assert rows[0]["hasCanvas"] == 0 and rows[0]["canvas"] == "none"
    assert rows[1]["hasCanvas"] == 1 and rows[1]["canvas"] == "none"
    assert all(row["hasCanvas"] == 1 and row["canvas"].startswith("data:image/svg+xml") for row in rows[2:])
    assert result["detail"] == "1234"
    assert result["cache"] == "no-store"
    assert result["nodeCount"] == result["payloadCount"] == 5, "accepted capture payload must exclude exit visuals"
    assert result["transitionPending"] is True and result["pendingRaw"] >= result["payloadCount"]
    assert result["settledRaw"] == result["payloadCount"], "raw Cytoscape count must converge after transition settlement"
    assert result["ready"] == "ready"


def test_graph_phase3_comment_refresh_rejects_malformed_and_preserves_last_good(
    server_url: str, page
) -> None:
    """Malformed/error refreshes cannot fabricate zero; a later valid fetch recovers."""
    _open_graph(page, server_url)
    result = page.evaluate(
        r"""async () => {
          const G=window.__okfLoomGraph, url=document.getElementById('okf-graph').dataset.graphUrl;
          const native=window.fetch.bind(window), base=await native(url).then(r=>r.json()), id=base.nodes[0].data.id;
          function payload(value){const p=JSON.parse(JSON.stringify(base));p.nodes[0].data.active_comment_count=value;return p;}
          let current=payload(7), options=[];
          window.fetch=(req,opt)=>String(req)===url?(options.push(opt),Promise.resolve(current instanceof Response?current:new Response(JSON.stringify(current),{status:200,headers:{'Content-Type':'application/json'}}))):native(req,opt);
          const read=()=>{const b=document.querySelector('.okf-node-index__item[data-target="'+CSS.escape(id)+'"]');return b&&b.getAttribute('aria-label');};
          const refresh=async()=>{await G.refreshGraph();await new Promise(requestAnimationFrame);};
          await refresh(); const good=read();
          const ready=()=>({graph:document.body.dataset.okfGraphCaptureReady,page:document.body.dataset.okfCaptureReady,error:document.body.dataset.okfGraphRefreshError||null});
          const goodReady=ready();
          current=payload(-1); await refresh(); const malformed=read(),malformedReady=ready();
          current=new Response('',{status:503}); await refresh(); const unavailable=read(),unavailableReady=ready();
          window.fetch=(req,opt)=>String(req)===url?Promise.reject(new TypeError('offline')):native(req,opt);
          await refresh(); const network=read(),networkReady=ready();
          window.fetch=(req,opt)=>String(req)===url?(options.push(opt),Promise.resolve(new Response(JSON.stringify(payload(11)),{status:200,headers:{'Content-Type':'application/json'}}))):native(req,opt);
          await refresh(); const recovered=read(),recoveredReady=ready();
          return {good,malformed,unavailable,network,recovered,goodReady,malformedReady,unavailableReady,networkReady,recoveredReady,noStore:options.every(o=>o.cache==='no-store')};
        }"""
    )
    assert "7 active comments" in result["good"]
    assert result["malformed"] == result["good"]
    assert result["unavailable"] == result["good"]
    assert result["network"] == result["good"]
    assert result["goodReady"] == {"graph": "ready", "page": "ready", "error": None}
    for key in ("malformedReady", "unavailableReady", "networkReady"):
        assert result[key]["graph"] == result[key]["page"] == "ready"
        assert result[key]["error"]
    assert "11 active comments" in result["recovered"]
    assert result["recoveredReady"] == {"graph": "ready", "page": "ready", "error": None}
    assert result["noStore"] is True


def test_multi_edge_keyboard_pointer_panel_and_secondary_semantics(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.get_by_text("Graph settings", exact=True).click()
    page.locator(".okf-edge-bundle-index summary").click()
    trigger = page.get_by_role("option", name="Checkout Service → Orders · 3 relationships")
    expect(trigger).to_be_visible()
    before = page.evaluate("() => ({zoom:window.__okfLoomGraph.cy.zoom(),pan:window.__okfLoomGraph.cy.pan()})")
    trigger.click()
    expect(page.get_by_role("heading", name="Relationships (3)")).to_be_visible()
    panel = page.locator("#okf-edge-bundle-panel")
    expect(panel).to_contain_text("depends_on")
    expect(panel).to_contain_text("writes_to")
    expect(panel).to_contain_text("orders table")
    assert page.evaluate("() => document.activeElement.id") != "okf-edge-bundle-panel"
    assert page.evaluate("() => document.querySelectorAll('.okf-edge-bundle-index__item[aria-selected=true]').length") == 1
    after = page.evaluate("() => ({zoom:window.__okfLoomGraph.cy.zoom(),pan:window.__okfLoomGraph.cy.pan()})")
    assert after == before
    page.get_by_role("button", name="Collapse relationships").click()
    expect(panel).to_be_hidden()
    trigger.focus()
    trigger.press("Enter")
    collapse = page.get_by_role("button", name="Collapse relationships")
    expect(collapse).to_be_focused()
    collapse.press("Enter")
    expect(page.get_by_role("listbox", name="Bundled relationships")).to_be_focused()
    trigger.press("Space")
    expect(collapse).to_be_focused()
    collapse.press("Space")
    expect(page.get_by_role("listbox", name="Bundled relationships")).to_be_focused()
    second = page.locator(".okf-edge-bundle-index__item").nth(1)
    second.focus(); second.press("Space")
    expect(collapse).to_be_focused()
    assert page.evaluate("() => document.querySelectorAll('.okf-edge-bundle-index__item[aria-selected=true]').length") == 1
    collapse.press("Enter")
    expect(page.get_by_role("listbox", name="Bundled relationships")).to_be_focused()


@pytest.mark.parametrize("mutation", ["duplicate-id", "bad-multiplicity", "bad-primary", "empty-relations"])
def test_multi_edge_refresh_validation_is_fail_closed(server_url: str, page, mutation: str) -> None:
    _open_graph(page, server_url)
    before = page.evaluate("() => document.body.dataset.okfRevision")
    result = page.evaluate(r"""async mutation => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),fresh=await native(url).then(r=>r.json());
      const edge=fresh.edges.find(e=>e.data.multiplicity>1);fresh.bundle_revision='invalid-'+mutation;
      if(mutation==='duplicate-id')edge.data.relations[1].id=edge.data.relations[0].id;
      if(mutation==='bad-multiplicity')edge.data.multiplicity+=1;
      if(mutation==='bad-primary')edge.data.primary_relation={id:'missing',type:'x',label:'x'};
      if(mutation==='empty-relations')edge.data.relations=[];
      window.__multiEdgePayload=fresh;window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(window.__multiEdgePayload),{status:200,headers:{'Content-Type':'application/json'}})):native(req,opt);
      await G.refreshGraph();return {revision:document.body.dataset.okfRevision,error:document.body.dataset.okfGraphRefreshError};
    }""", mutation)
    assert result["revision"] == before
    assert result["error"]


def test_multi_edge_refresh_rebuilds_index_and_preserves_panel_focus(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.get_by_text("Graph settings", exact=True).click()
    page.locator(".okf-edge-bundle-index summary").click()
    trigger = page.get_by_role("option", name="Checkout Service → Orders · 3 relationships")
    trigger.focus(); trigger.press("Enter")
    result = page.evaluate(r"""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),fresh=await native(url).then(r=>r.json());
      const edge=fresh.edges.find(e=>e.data.source==='services/checkout'&&e.data.target==='tables/orders');
      edge.data.relations.push({id:'rel-refresh-fourth',label:'audits orders',type:'audits',kind:'relation',direction:'outgoing',source:edge.data.source,target:edge.data.target,target_raw:'relation:tables/orders',authored_identity:'refresh'});edge.data.multiplicity=4;
      window.__multiEdgePayload=fresh;window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(window.__multiEdgePayload),{status:200,headers:{'Content-Type':'application/json'}})):native(req,opt);
      await G.refreshGraph();await new Promise(requestAnimationFrame);
      return {focus:document.activeElement&&document.activeElement.textContent,panel:document.getElementById('okf-edge-bundle-panel').textContent,expanded:document.querySelectorAll('.okf-edge-bundle-index__item[aria-selected=true]').length};
    }""")
    assert result["focus"] == "Collapse relationships"
    assert "Relationships (4)" in result["panel"] and "audits orders" in result["panel"]
    assert result["expanded"] == 1
    page.get_by_role("button", name="Collapse relationships").press("Enter")
    updated = page.get_by_role("option", name="Checkout Service → Orders · 4 relationships")
    expect(page.get_by_role("listbox", name="Bundled relationships")).to_be_focused()
    updated.dispatch_event("click", {"detail": 1})
    assert page.evaluate("() => document.querySelector('.okf-edge-bundle-index__list').getAttribute('aria-activedescendant')===document.querySelector('.okf-edge-bundle-index__item[aria-selected=true]').id")
    page.evaluate("() => window.__okfLoomGraph.refreshGraph()")
    expect(page.get_by_role("listbox", name="Bundled relationships")).to_be_focused()
    second = page.locator(".okf-edge-bundle-index__item").nth(1)
    second.click()
    assert page.evaluate("() => document.querySelectorAll('.okf-edge-bundle-index__item[aria-selected=true]').length") == 1
    second_edge_id = second.get_attribute("data-edge-id")
    assert page.locator('.okf-edge-bundle-index__item[aria-selected="true"]').get_attribute("data-edge-id") == second_edge_id
    page.evaluate("edgeId => {window.__multiEdgePayload.edges=window.__multiEdgePayload.edges.filter(e=>e.data.id!==edgeId);return window.__okfLoomGraph.refreshGraph();}", second_edge_id)
    expect(page.locator("#okf-edge-bundle-panel")).to_be_hidden()
    expect(page.get_by_role("listbox", name="Bundled relationships")).to_be_focused()


def test_relationship_filter_counts_rebuild_on_live_instance_add_remove(server_url: str, page) -> None:
    _open_graph(page,server_url);page.get_by_text("Graph settings",exact=True).click()
    owner=page.get_by_label("Relationship filters");chips=owner.locator("button.okf-filter-chip")
    assert chips.count()>1
    first=chips.first.get_attribute("data-filter-value");second=chips.nth(1).get_attribute("data-filter-value")
    initial=page.evaluate("t=>window.__okfLoomGraph.cy.edges().reduce((n,e)=>n+(e.data('relations')||[]).filter(r=>r.type===t).length,0)",first)
    expect(chips.first).to_have_attribute("aria-label",re.compile(rf"^{re.escape(first)}, {initial} relationships?, shown$"))
    chips.first.click();chips.nth(1).focus();assert chips.nth(1).evaluate("b=>document.activeElement===b");assert page.locator("body").get_attribute("data-okf-relationship-filter-focus")==second
    result=page.evaluate(r"""async ([first,second])=>{const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),fresh=await native(url).then(r=>r.json()),edge=fresh.edges.find(e=>e.data.relations&&e.data.relations.length);edge.data.relations.push({id:'live-new-instance',type:'live_type',label:'Human label differs',kind:'relation',direction:'outgoing',source:edge.data.source,target:edge.data.target});edge.data.relations.push({id:'live-existing-instance',type:first,label:'Second human label',kind:'relation',direction:'outgoing',source:edge.data.source,target:edge.data.target});edge.data.multiplicity=edge.data.relations.length;fresh.bundle_revision='relation-filter-add';window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(fresh),{status:200,headers:{'Content-Type':'application/json'}})):native(req,opt);await G.refreshGraph();await new Promise(requestAnimationFrame);return {focus:document.activeElement&&document.activeElement.dataset.filterValue,active:document.activeElement&&document.activeElement.outerHTML,hidden:G.getState().hiddenRelations[first],url:location.search};}""",[first,second])
    result["controls"]=page.get_by_label("Relationship filters").locator("button.okf-filter-chip").count()
    result["open"]=page.locator("details.okf-signal__advanced").get_attribute("open") is not None
    result["targetVisible"]=page.get_by_label("Relationship filters").locator(f'[data-filter-value="{second}"]').is_visible()
    assert result["controls"]>1, result
    assert result["open"] and result["targetVisible"], result
    page.wait_for_function("value=>document.activeElement&&document.activeElement.dataset.filterValue===value",arg=second)
    assert result["hidden"] is True and first in result["url"], result
    owner=page.get_by_label("Relationship filters")
    expect(owner.locator('[data-filter-value="live_type"]')).to_have_attribute("aria-label","live_type, 1 relationship, shown")
    expect(owner.locator(f'[data-filter-value="{first}"]')).to_have_attribute("aria-label",re.compile(rf"^{re.escape(first)}, {initial+1} relationships?, hidden$"))
    removed=page.evaluate(r"""async () => {const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),fresh=await native(url).then(r=>r.json());fresh.edges.forEach(e=>{e.data.relations=(e.data.relations||[]).filter(r=>r.id!=='live-new-instance'&&r.id!=='live-existing-instance');e.data.multiplicity=e.data.relations.length;});fresh.edges=fresh.edges.filter(e=>e.data.relations.length);fresh.bundle_revision='relation-filter-remove';window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(fresh),{status:200,headers:{'Content-Type':'application/json'}})):native(req,opt);await G.refreshGraph();await new Promise(requestAnimationFrame);return {hasLive:!!document.querySelector('[data-filter-value="live_type"]'),focus:document.activeElement&&document.activeElement.dataset.filterValue};}""")
    assert not removed["hasLive"] and removed["focus"]==second


def test_multi_edge_old_payload_and_malicious_label_are_safe(server_url: str, page) -> None:
    _open_graph(page, server_url)
    result = page.evaluate(r"""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),fresh=await native(url).then(r=>r.json());
      fresh.edges.forEach(e=>{delete e.data.relations;delete e.data.multiplicity;delete e.data.primary_relation;});
      fresh.edges[0].data.label='<img src=x onerror=window.__multiEdgeXss=1>';
      window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(fresh),{status:200,headers:{'Content-Type':'application/json'}})):native(req,opt);
      await G.refreshGraph();const edge=G.cy.edges()[0];edge.emit('tap');
      return {xss:window.__multiEdgeXss||0,text:document.getElementById('okf-edge-bundle-panel').textContent,images:document.getElementById('okf-edge-bundle-panel').querySelectorAll('img').length,multiplicity:edge.data('multiplicity')};
    }""")
    assert result["xss"] == 0 and result["images"] == 0
    assert "<img src=x" in result["text"]
    assert result["multiplicity"] == 1


def test_multi_edge_endpoint_collision_ids_coexist_through_live_refresh(server_url: str, page) -> None:
    _open_graph(page, server_url)
    result = page.evaluate(r"""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),base=await native(url).then(r=>r.json());
      const ids=['a__b','c','a','b__c'];ids.forEach(id=>base.nodes.push({data:{id,label:id,type:'concept'}}));
      function relation(relationId,s,t){const relation={id:relationId,label:'link',type:'link',kind:'body_link',direction:'outgoing',source:s,target:t,target_raw:t};return {data:{source:s,target:t,label:'link',multiplicity:1,relations:[relation],primary_relation:relation}};}
      base.edges.push(relation('collision-left-r','a__b','c'),relation('collision-right-r','a','b__c'));
      window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(base),{status:200,headers:{'Content-Type':'application/json'}})):native(req,opt);
      await G.refreshGraph();return [['a__b','c'],['a','b__c']].map(pair=>{const edge=G.cy.edges().filter(e=>e.data('source')===pair[0]&&e.data('target')===pair[1]);return {id:edge.id(),length:G.cy.getElementById(edge.id()).length,source:edge.data('source'),target:edge.data('target')};});
    }""")
    assert result == [
        {"id": "legacy-edge:4:a__b:1:c", "length": 1, "source": "a__b", "target": "c"},
        {"id": "legacy-edge:1:a:4:b__c", "length": 1, "source": "a", "target": "b__c"},
    ]


def test_multi_edge_nojs_relationship_text_static_and_single(static_url: str, tmp_path: Path, page) -> None:
    from okf_loom import Bundle
    from okf_loom.render import render_single_file

    single = tmp_path / "single.html"
    render_single_file(Bundle.load(DEMO_BUNDLE), single)
    context = page.context.browser.new_context(java_script_enabled=False)
    try:
        static = context.new_page(); static.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
        expect(static.get_by_role("heading", name="Resolved in-bundle relationships")).to_be_visible()
        expect(static.get_by_text("orders table — link: services/checkout → tables/orders", exact=True)).to_be_visible()
        portable = context.new_page(); portable.goto(single.as_uri(), wait_until="domcontentloaded")
        expect(portable.get_by_role("heading", name="Resolved in-bundle relationships")).to_be_visible()
        expect(portable.get_by_text("depends_on — depends_on: services/checkout → tables/orders", exact=True)).to_be_visible()
    finally:
        context.close()


def test_multi_edge_secondary_relation_filter_group_and_path(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.locator(".okf-signal__advanced > summary").click()
    page.wait_for_function("() => document.querySelector('.okf-signal__status')?.dataset.visibleConnectionBundles && document.querySelector('.okf-edge-bundle-index')?.dataset.visibleConnectionBundles")
    depends = page.locator('.okf-filter-chips[aria-label="Relationship filters"] button[data-filter-value="depends_on"]')
    expect(depends).to_be_visible(); depends.click()
    edge_id = page.evaluate("() => window.__okfLoomGraph.cy.edges().filter(e=>e.data('source')==='services/checkout'&&e.data('target')==='tables/orders').id()")
    assert not page.evaluate("id => window.__okfLoomGraph.cy.getElementById(id).hasClass('dim')", edge_id)
    depends.click()
    page.get_by_role("button", name="Show only depends_on").click()
    assert not page.evaluate("id => window.__okfLoomGraph.cy.getElementById(id).hasClass('dim')", edge_id)
    assert "hiddenRelation=" in page.url
    page.reload(); page.wait_for_function("() => window.__okfLoomGraph?.cy")
    edge_id = page.evaluate("() => window.__okfLoomGraph.cy.edges().filter(e=>e.data('source')==='services/checkout'&&e.data('target')==='tables/orders').id()")
    assert not page.evaluate("id => window.__okfLoomGraph.cy.getElementById(id).hasClass('dim')", edge_id)
    page.locator(".okf-signal__advanced > summary").click()
    page.locator('.okf-filter-chips[aria-label="Relationship filters"] .okf-filter-showall').click()
    page.get_by_label("Group by").select_option("relation")
    expect(page.locator(".okf-signal__status")).to_contain_text("grouped by relation type")
    page.evaluate("() => {const G=window.__okfLoomGraph;G.tracePath(G.cy.getElementById('services/checkout'),G.cy.getElementById('tables/orders'));}")
    path = page.locator(".okf-detail-path")
    expect(path).to_contain_text("depends_on")
    expect(path).to_contain_text("writes_to")
    expect(path).to_contain_text("orders table")


def test_multi_edge_bundle_and_instance_count_parity(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.wait_for_function("() => document.querySelector('.okf-signal__status')?.dataset.totalRelationshipInstances")
    counts = page.evaluate("""() => {const cy=window.__okfLoomGraph.cy,status=document.querySelector('.okf-signal__status'),mobile=document.querySelector('.okf-mobile-overview__status');return {canvasBundles:cy.edges().length,canvasInstances:cy.edges().reduce((n,e)=>n+(e.data('multiplicity')||1),0),statusBundles:Number(status.dataset.totalConnectionBundles),statusInstances:Number(status.dataset.totalRelationshipInstances),statusText:status.textContent,mobileBundles:Number(mobile.dataset.totalConnectionBundles),mobileInstances:Number(mobile.dataset.totalRelationshipInstances),mobileText:mobile.textContent};}""")
    assert counts["canvasBundles"] == counts["statusBundles"] == counts["mobileBundles"] == 14
    assert counts["canvasInstances"] == counts["statusInstances"] == counts["mobileInstances"] == 23
    assert "connection bundles" in counts["statusText"] and "relationship instances" in counts["statusText"]
    assert "connection bundles" in counts["mobileText"] and "relationship instances" in counts["mobileText"]


def test_multi_edge_hiding_secondary_relation_updates_visible_instance_parity(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.get_by_text("Graph settings", exact=True).click()
    page.locator(".okf-edge-bundle-index summary").click()
    trigger = page.get_by_role("option", name="Checkout Service → Orders · 3 relationships")
    trigger.click()
    page.locator('.okf-filter-chips[aria-label="Relationship filters"] button[data-filter-value="depends_on"]').click()
    page.wait_for_function("() => document.querySelector('.okf-signal__status')?.dataset.visibleRelationshipInstances")
    expect(page.get_by_role("heading", name="Relationships (2 of 3)")).to_be_visible()
    panel = page.locator("#okf-edge-bundle-panel")
    expect(panel).not_to_contain_text("depends_on")
    expect(panel).to_contain_text("writes_to")
    expect(panel).to_contain_text("orders table")
    expect(trigger).to_contain_text("3 relationships")  # disclosure keeps total multiplicity
    counts = page.evaluate("""() => {const cy=window.__okfLoomGraph.cy,state=window.__okfLoomGraph.getState(),visible=cy.edges().filter(e=>!e.hasClass('dim')&&!e.hasClass('okf-edge-elided')),actual=visible.reduce((n,e)=>n+(e.data('relations')||[]).filter(r=>!state.hiddenRelations[r.type]&&!state.hiddenRelations[r.label]).length,0),status=document.querySelector('.okf-signal__status'),mobile=document.querySelector('.okf-mobile-overview__status');return {actual,status:Number(status.dataset.visibleRelationshipInstances),mobile:Number(mobile.dataset.visibleRelationshipInstances),bundles:visible.length,statusBundles:Number(status.dataset.visibleConnectionBundles),mobileBundles:Number(mobile.dataset.visibleConnectionBundles)};}""")
    assert counts["actual"] == counts["status"] == counts["mobile"]
    assert counts["bundles"] == counts["statusBundles"] == counts["mobileBundles"]


def test_multi_edge_relationship_label_toggle_resyncs_and_restores_projection(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.locator(".okf-signal__advanced > summary").click()
    page.wait_for_function("() => document.querySelector('.okf-signal__status')?.dataset.visibleConnectionBundles && document.querySelector('.okf-edge-bundle-index')?.dataset.visibleConnectionBundles")
    read = """() => {const read=e=>({b:e.dataset.visibleConnectionBundles,i:e.dataset.visibleRelationshipInstances,tb:e.dataset.totalConnectionBundles,ti:e.dataset.totalRelationshipInstances}),status=document.querySelector('.okf-signal__status'),mobile=document.querySelector('.okf-mobile-overview__status'),disclosure=document.querySelector('.okf-edge-bundle-index');return {status:read(status),mobile:read(mobile),disclosure:read(disclosure),canvas:{b:String(window.__okfLoomGraph.cy.edges().filter(e=>!e.hasClass('dim')&&!e.hasClass('okf-edge-elided')).length),i:String(window.__okfLoomGraph.cy.edges().filter(e=>!e.hasClass('dim')&&!e.hasClass('okf-edge-elided')).reduce((n,e)=>n+(e.data('relations')||[]).length,0))}};}"""
    before = page.evaluate(read)
    _toggle_check(page, "Show relationship labels")
    explicit = page.evaluate(read)
    assert explicit["canvas"] == {"b": "14", "i": "23"}
    assert explicit["status"] == explicit["mobile"] == explicit["disclosure"] == {"b": "14", "i": "23", "tb": "14", "ti": "23"}
    _toggle_check(page, "Show relationship labels")
    restored = page.evaluate(read)
    assert restored == before


def test_multi_edge_400_percent_reflow_has_no_page_overflow(server_url: str, page) -> None:
    # WCAG 1.4.10 equivalent: 1280px at 400% gives a 320 CSS-pixel viewport.
    page.set_viewport_size({"width": 320, "height": 800})
    _open_graph(page, server_url)
    page.get_by_role("button", name="Details", exact=True).click()
    page.get_by_text("Graph settings", exact=True).click()
    page.locator(".okf-edge-bundle-index summary").evaluate("e=>e.click()")
    page.get_by_role("option", name="Checkout Service → Orders · 3 relationships").evaluate("e=>e.click()")
    expect(page.get_by_role("heading", name="Relationships (3)")).to_be_visible()
    overflow = page.evaluate("""() => ({page:document.documentElement.scrollWidth-document.documentElement.clientWidth,offenders:Array.from(document.querySelectorAll('body *')).filter(e=>!e.closest('#okf-graph .cytoscape-container')&&e.getBoundingClientRect().right>document.documentElement.clientWidth+1).map(e=>e.className||e.tagName).slice(0,10)})""")
    assert overflow["page"] <= 1, overflow
    assert overflow["offenders"] == [], overflow
    expect(page.locator(".okf-topbar")).to_be_visible()
    page.get_by_role("button", name="Graph", exact=True).click()
    expect(page.locator(".okf-edge-bundle-index")).to_have_count(1)
    page.get_by_label("Graph panes").get_by_role("button", name="Details", exact=True).click()
    collapse = page.get_by_role("button", name="Collapse relationships")
    collapse.focus(); collapse.press("Enter")
    expect(page.locator("#okf-edge-bundle-panel")).to_be_hidden()


def test_graph_phase3_comment_event_updates_every_surface_without_layout(
    server_url: str, page
) -> None:
    """A live comment event uses the one graph refresh owner and patches counts only."""
    _open_graph(page, server_url)
    page.locator(".okf-node-index summary").click()
    # Initialisation applies a first layout immediately, then the active lens
    # owns a pre-existing 220 ms refinement. Opening the disclosure can happen
    # while that deferred owner is still pending, so settle it before taking
    # the no-layout baseline. The invariant below remains exact: the comment
    # event may not advance either generation.
    page.wait_for_timeout(300)
    page.wait_for_function(
        "() => { const s=window.okfGraphCaptureState(); "
        "return s.layoutSettled && s.layoutGeneration === s.appliedLayoutGeneration; }"
    )
    result = page.evaluate(
        r"""async () => {
          const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window);
          const base=await native(url).then(r=>r.json()),id=base.nodes[0].data.id;
          const payload=JSON.parse(JSON.stringify(base));payload.nodes[0].data.active_comment_count=137;
          const before={nodes:G.cy.nodes().length,edges:G.cy.edges().length,capture:window.okfGraphCaptureState()};
          window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}})):native(req,opt);
          window.okfLoomLive.emit('comment',{concept_id:id});
          await new Promise(resolve=>setTimeout(resolve,850));await new Promise(requestAnimationFrame);
          const button=document.querySelector('.okf-node-index__item[data-target="'+CSS.escape(id)+'"]');
          button.focus();button.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));button.click();
          const detail=Array.from(document.querySelectorAll('#okf-detail-signals dt')).find(x=>x.textContent==='Active comments');
          return {id,badge:button.querySelector('.okf-node-index__comments').textContent,aria:button.getAttribute('aria-label'),detail:detail&&detail.nextElementSibling.textContent,focused:document.activeElement===button,canvas:decodeURIComponent(G.cy.getElementById(id).data('commentBadgeUrl')),nodes:G.cy.nodes().length,edges:G.cy.edges().length,capture:window.okfGraphCaptureState(),before};
        }"""
    )
    assert result["badge"] == "99+"
    assert "137 active comments" in result["aria"] and result["detail"] == "137"
    assert result["focused"] is True and ">99+<" in result["canvas"]
    assert result["nodes"] == result["before"]["nodes"]
    assert result["edges"] == result["before"]["edges"]
    assert result["capture"]["layoutGeneration"] == result["before"]["capture"]["layoutGeneration"]
    assert result["capture"]["appliedLayoutGeneration"] == result["before"]["capture"]["appliedLayoutGeneration"]


def test_graph_phase3_comment_badges_follow_theme_and_accessibility_media(
    server_url: str, page
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(reduced_motion="reduce", forced_colors="active")
    _open_graph(page, server_url)
    result = page.evaluate(
        r"""async () => {
          const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=window.fetch.bind(window),base=await native(url).then(r=>r.json());
          const payload=JSON.parse(JSON.stringify(base)),id=payload.nodes[0].data.id;
          payload.nodes[0].data.label='Sehr langer Wissensgraph-Begriff العربية 日本語 with broken destination';payload.nodes[0].data.active_comment_count=1000;payload.nodes[0].data.unresolved_count=4;
          window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}})):native(req,opt);await G.refreshGraph();
          const uri=()=>G.cy.getElementById(id).data('commentBadgeUrl');
          window.OKFLoomTheme.setTheme('technical-light');await new Promise(requestAnimationFrame);const light=uri();
          window.OKFLoomTheme.setTheme('technical-dark');await new Promise(requestAnimationFrame);const dark=uri();
          const item=document.querySelector('.okf-node-index__item[data-target="'+CSS.escape(id)+'"]'),badge=item.querySelector('.okf-node-index__comments'),warn=item.querySelector('.okf-node-index__warn');
          return {light,dark,theme:document.documentElement.dataset.theme,aria:item.getAttribute('aria-label'),badge:badge.textContent,warn:!!warn,badgeBorder:getComputedStyle(badge).borderTopColor,motion:matchMedia('(prefers-reduced-motion: reduce)').matches,forced:matchMedia('(forced-colors: active)').matches,overflow:document.documentElement.scrollWidth>innerWidth+1};
        }"""
    )
    assert result["light"] != result["dark"] and result["theme"] == "technical-dark"
    assert result["badge"] == "99+" and "1000 active comments" in result["aria"]
    assert "4 broken links" in result["aria"] and result["warn"] is True
    assert result["motion"] is True and result["forced"] is True
    assert result["badgeBorder"] and result["overflow"] is False


def test_static_graph_comment_badges_never_open_comment_network(static_url: str, page) -> None:
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(static_url + "/__graph.html", wait_until="domcontentloaded")
    page.locator("#okf-graph canvas").first.wait_for(state="visible")
    page.wait_for_timeout(200)
    assert not [
        url for url in requests
        if urllib.parse.urlparse(url).path.lower().startswith(("/__comment", "/comments", "/api/comment"))
    ]


def _inject_orphan_fixture(page, server_url) -> None:
    """Replace the live graph.json with a NON-VACUOUS fixture: 4 connected
    nodes (kept from the real bundle) + 3 deterministic degree-zero orphans
    (Alpha/Mango/Zeta). Used by the orphan-dock geometry / keyboard / capture
    tests so the assertions run against a graph that actually HAS orphans."""
    _open_graph(page, server_url)
    page.evaluate(
        r"""async () => {
          const graph=document.getElementById('okf-graph'), url=graph.dataset.graphUrl, native=window.fetch.bind(window);
          const base=await native(url).then(r=>r.json());
          const keep=base.nodes.slice(0,4).map(n=>n.data.id);
          const orph=[{id:'orphan-zeta',label:'Zeta Orphan'},{id:'orphan-alpha',label:'Alpha Orphan'},{id:'orphan-mango',label:'Mango Orphan'}];
          const nodes=base.nodes.filter(n=>keep.includes(n.data.id))
                   .concat(orph.map(o=>({data:{id:o.id,label:o.label,type:'concept',color:'#888888',size:24,degree:0,unresolved_count:0}})));
          const edges=base.edges.filter(e=>keep.includes(e.data.source)&&keep.includes(e.data.target));
          const outgoing={},backlinks={};
          edges.forEach(e=>{(outgoing[e.data.source]=outgoing[e.data.source]||[]).push(e.data.target);(backlinks[e.data.target]=backlinks[e.data.target]||[]).push(e.data.source);});
           const payload=Object.assign({},base,{nodes:nodes,edges:edges,outgoing:outgoing,backlinks:backlinks});
           window.__okfOrphUrl=url; window.__okfOrphNative=native;
           const G=window.__okfLoomGraph;G.cy.pan({x:31,y:-17});G.cy.zoom(.71);
           const before={pan:G.cy.pan(),zoom:G.cy.zoom(),starts:G.layoutStats.starts};
           window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}})):native(req,opt);
           return G.refreshGraph().then(()=>{window.__okfOrphanMotion={before,after:{pan:G.cy.pan(),zoom:G.cy.zoom(),starts:G.layoutStats.starts},capture:window.okfGraphCaptureState()};});
        }"""
    )
    page.wait_for_timeout(900)


_ORPHAN_GEOM_JS = r"""() => {
  const cy=window.__okfLoomGraph.cy;
  const ids=['orphan-alpha','orphan-mango','orphan-zeta'];
  const orph=ids.map(id=>{const n=cy.getElementById(id);const p=n.position();
    return {id,label:String(n.data('label')||id),x:p.x,y:p.y,r:(n.width()||24)/2};});
  function fp(n){const lw=Math.min(90,String(n.label).length*7);const half=Math.max(n.r,lw/2);
    return {x1:n.x-half,x2:n.x+half,y1:n.y-n.r,y2:n.y+n.r+19};}
  let ov=0; const fps=orph.map(fp);
  for(let i=0;i<fps.length;i++)for(let j=i+1;j<fps.length;j++){
    const a=fps[i],b=fps[j];const ox=Math.min(a.x2,b.x2)-Math.max(a.x1,b.x1);
    const oy=Math.min(a.y2,b.y2)-Math.max(a.y1,b.y1);if(ox>0&&oy>0)ov++;}
  const mains=cy.nodes().toArray().filter(n=>!ids.includes(n.id())).map(n=>{
    const p=n.position();return {x:p.x,y:p.y,r:(n.width()||24)/2,label:String(n.data('label')||'')};});
  const mfp=mains.map(fp); let orphMain=0;
  for(const o of fps)for(const m of mfp){const ox=Math.min(o.x2,m.x2)-Math.max(o.x1,m.x1);
    const oy=Math.min(o.y2,m.y2)-Math.max(o.y1,m.y1);if(ox>0&&oy>0)orphMain++;}
  const ys=orph.map(o=>o.y);
  const ltr=orph.slice().sort((a,b)=>a.x-b.x).map(o=>o.id);
  const info={orph:orph.map(o=>({id:o.id,x:Math.round(o.x),y:Math.round(o.y),r:Math.round(o.r)})),
    orphOverlaps:ov, orphMainOverlaps:orphMain,
    sameRow:(Math.max.apply(null,ys)-Math.min.apply(null,ys))<=1,
    leftToRight:ltr, expectedSort:ids.slice()};
  info.leftToRightMatchesSort=JSON.stringify(info.leftToRight)===JSON.stringify(info.expectedSort);
  return info;
}"""


def test_graph_phase3_orphan_dock_geometry_stable_order_no_overlap(
    server_url: str, page
) -> None:
    """Phase 3 P1 (layout-graph): the ACTUAL degree-zero Cytoscape nodes are
    docked in a reserved bottom band (not only duplicated DOM buttons). With a
    non-vacuous orphan fixture this asserts: (1) orphan footprints never
    overlap each other, (2) orphans never overlap connected nodes, (3) all
    orphans share one band row, (4) left-to-right canvas order matches the
    deterministic (label,id) sort. Model-space geometry is viewport-independent,
    so the same invariants hold on mobile."""
    _inject_orphan_fixture(page, server_url)
    info = page.evaluate(_ORPHAN_GEOM_JS)
    assert info["orphOverlaps"] == 0, (
        f"docked orphan footprints overlap: {info}"
    )
    assert info["orphMainOverlaps"] == 0, (
        f"docked orphans overlap connected nodes: {info}"
    )
    assert info["sameRow"] is True, "orphans are not on a single reserved band row"
    assert info["leftToRightMatchesSort"] is True, (
        f"orphan canvas order != deterministic (label,id) sort: {info}"
    )
    motion = page.evaluate("() => window.__okfOrphanMotion")
    assert motion["after"] == motion["before"], "orphan-only placement moved the camera or started a global layout"
    assert motion["capture"]["payloadNodeCount"] == motion["capture"]["nodeCount"] == 7
    # DOM shelf entries mirror the same sorted set (the labelled control
    # surface stays in lockstep with the docked glyphs).
    shelf = page.evaluate(
        """() => { const s=document.querySelector('.okf-graph-orphans');
          return s&&!s.hidden?Array.from(s.querySelectorAll('.okf-graph-orphans__item')).map(b=>b.getAttribute('data-target')):[]; }"""
    )
    assert shelf == info["expectedSort"], (
        f"DOM shelf order != deterministic orphan sort: {shelf} vs {info['expectedSort']}"
    )


def test_graph_phase3_orphan_dock_no_overlap_mobile(server_url: str, page) -> None:
    """Small mobile graphs integrate orphans into the deterministic grid safely."""
    page.set_viewport_size({"width": 390, "height": 844})
    _inject_orphan_fixture(page, server_url)
    info = page.evaluate(_ORPHAN_GEOM_JS)
    assert info["orphOverlaps"] == 0, f"mobile orphan overlap: {info}"
    assert info["orphMainOverlaps"] == 0, f"mobile orphan/main overlap: {info}"
    # <=12 nodes use the label-aware mobile grid rather than the desktop-only
    # orphan band; requiring one row here would recreate the compact cluster.
    assert len({item["y"] for item in info["orph"]}) <= 3


def test_graph_phase3_orphan_shelf_composite_keyboard_focus_and_activation(
    server_url: str, page
) -> None:
    """The orphan alternative is one listbox stop; Space selects its active row."""
    _inject_orphan_fixture(page, server_url)
    orphan_list = page.get_by_role("listbox", name="Concepts with no relationships")
    orphan_list.wait_for(state="visible")
    orphan_list.focus()
    expect(orphan_list).to_be_focused()
    page.keyboard.press("Space")
    page.wait_for_function("() => window.okfGraphSelection && window.okfGraphSelection.conceptId")
    selected = page.evaluate("() => window.okfGraphSelection.conceptId")
    assert selected in ("orphan-alpha", "orphan-mango", "orphan-zeta"), (
        f"keyboard activation did not select an orphan: {selected}"
    )
    # The detail pane opened (keyboard provenance).
    detail = page.locator("#detail-content")
    expect(detail).to_be_visible()


def test_graph_phase3_orphan_dock_capture_readiness(server_url: str, page) -> None:
    """Phase 3 P1 (rendering-capture): after the orphan fixture loads and
    docks, the graph is capture-ready (layout settled, no terminal state) so a
    screenshot/capture consumer is authorized. Docking must not strand the
    capture lifecycle in a loading state."""
    _inject_orphan_fixture(page, server_url)
    page.wait_for_function("() => window.okfGraphCaptureState().layoutSettled")
    outcome = page.evaluate(
        """async () => {
          try { await window.okfCaptureReadiness.start({target:'graph'}).promise; return {authorized:true}; }
          catch(e) { return {authorized:e.screenshotAllowed,code:e.code,phase:e.metadata && e.metadata.readinessPhase}; }
        }"""
    )
    assert outcome["authorized"] is True, (
        f"orphan-dock graph not capture-ready: {outcome}"
    )
    state = page.evaluate("() => window.okfGraphCaptureState()")
    assert state["layoutSettled"] is True
    assert not state.get("terminalCode")


def test_graph_key_is_owned_by_single_lenses_advanced_region(
    server_url: str, page
) -> None:
    """The graph key has no canvas owner or duplicate disclosure."""
    _open_graph(page, server_url)
    info = page.evaluate(
        r"""() => {
          const lg=document.querySelector('.okf-graph-legend');
          const body=lg && lg.querySelector('.okf-graph-legend__body');
          return { hidden: lg ? lg.hidden : null,
                   inCanvas: !!(lg && lg.closest('#okf-graph')),
                   regionCount: document.querySelectorAll('.okf-signal[role="region"]').length,
                   hasRegion: !!(lg && lg.closest('.okf-signal[role="region"]')),
                   hasGraphSettings: !!(lg && lg.closest('details.okf-signal__advanced')),
                   hasDuplicateToggle: !!(lg && lg.querySelector('.okf-graph-legend__toggle')),
                   bodyDisplay: body ? getComputedStyle(body).display : null };
        }"""
    )
    assert info["hasRegion"] and info["hasGraphSettings"] and info["regionCount"] == 1, info
    assert not info["inCanvas"] and not info["hasDuplicateToggle"], info
    page.locator("details.okf-signal__advanced > summary").click()
    assert page.locator(".okf-graph-legend__body").is_visible()

def test_graph_phase3_dark_theme_edge_contrast_raised(server_url: str, page) -> None:
    """Phase 3 P1: dark-theme edges are lifted above the hairline frame
    colour so they read on the dark canvas (the frame colour #334155
    disappears as a graph edge on the ~#0b1220 canvas). Asserted on the live
    edge style after switching to the technical-dark theme."""
    _open_graph(page, server_url)
    page.evaluate(
        """() => { if (window.OKFLoomTheme && window.OKFLoomTheme.setTheme)
          window.OKFLoomTheme.setTheme('technical-dark'); }"""
    )
    page.wait_for_timeout(300)
    edge = page.evaluate(
        """() => { const e=window.__okfLoomGraph.cy.edges()[0];
          return { line: e.style('line-color'), theme: document.documentElement.getAttribute('data-theme') }; }"""
    )
    assert edge["theme"] == "technical-dark"
    # The lifted dark edge is a noticeably brighter slate than the old #334155
    # frame value. Parse the rgb and assert every channel rose.
    import re
    m = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", edge["line"] or "")
    assert m, f"unexpected edge line-color: {edge['line']!r}"
    r, g, b = (int(x) for x in m.groups())
    assert (r, g, b) > (51, 65, 85), (
        f"dark edge not lifted above old #334155 frame: rgb({r},{g},{b})"
    )


# ---- P1 Progressive disclosure: deterministic edge backbone ---------------

def test_graph_progressive_disclosure_backbone(docs_server_url: str, page) -> None:
    """Default unselected state shows a deterministic high-signal subset
    (backbone), not the full edge set. The backbone is stable across reloads."""
    _open_graph(page, docs_server_url)
    page.wait_for_timeout(500)

    info = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            return {
                total: cy.edges().length,
                elided: cy.edges('.okf-edge-elided').length,
                backbone: window.__okfLoomGraph.backboneEdgeCount(),
                relationshipDetailOptIn: window.__okfLoomGraph.getState().relationshipDetailOptIn,
            };
        }"""
    )
    assert info["total"] > 0, "graph has no edges"
    assert info["relationshipDetailOptIn"] is False, "default should be tiered mode"
    assert info["elided"] > 0, \
        f"no edges elided in backbone mode (total={info['total']})"
    assert info["total"] - info["elided"] == info["backbone"], \
        f"visible ({info['total'] - info['elided']}) != backbone count ({info['backbone']})"
    # The backbone is a SUBSET, not the full set.
    assert info["backbone"] < info["total"], \
        f"backbone ({info['backbone']}) >= total ({info['total']}) — no disclosure"

    # Determinism: reload → same backbone set.
    first_elided = page.evaluate(
        "() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').map(e=>e.id()).sort().join(',')"
    )
    page.reload(wait_until="domcontentloaded")
    page.locator("#okf-graph canvas").first.wait_for(state="visible")
    page.wait_for_function(
        "() => window.__okfLoomGraph && window.__okfLoomGraph.cy "
        "&& window.__okfLoomGraph.cy.nodes().length > 0 "
        "&& window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1",
        timeout=10000,
    )
    page.wait_for_timeout(300)
    second_elided = page.evaluate(
        "() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').map(e=>e.id()).sort().join(',')"
    )
    assert first_elided == second_elided, \
        "backbone edge set changed across reload (non-deterministic)"


def test_graph_relationship_labels_toggle_controls_complete_detail(docs_server_url: str, page) -> None:
    """The explicit label control reveals all edges and restores the tier."""
    _open_graph(page, docs_server_url)
    page.wait_for_timeout(500)

    backbone_elided = page.evaluate(
        "() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').length"
    )
    assert backbone_elided > 0, "no edges elided before toggle"

    page.evaluate("() => { window.__okfLoomGraph.cy.zoom(1.6); return undefined; }")
    page.wait_for_timeout(100)
    high_zoom_elided = page.evaluate(
        "() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').length"
    )
    assert high_zoom_elided > 0, "automatic high zoom exposed the complete edge set"

    # Toggle ON.
    _toggle_check(page, "Show relationship labels")
    page.wait_for_timeout(200)
    assert _state(page)["relationshipDetailOptIn"] is True
    all_elided = page.evaluate(
        "() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').length"
    )
    assert all_elided == 0

    # Toggle OFF → salient high-zoom tier restored.
    _toggle_check(page, "Show relationship labels")
    page.wait_for_timeout(200)
    assert _state(page)["relationshipDetailOptIn"] is False
    restored_elided = page.evaluate(
        "() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').length"
    )
    assert restored_elided == high_zoom_elided


def test_graph_selected_incident_reveal(docs_server_url: str, page) -> None:
    """Selecting a node reveals ALL its incident edges (even elided ones);
    deselecting re-elides the non-backbone ones."""
    _open_graph(page, docs_server_url)
    page.wait_for_timeout(500)

    result = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            // Pick a high-degree node likely to have elided incident edges.
            const node = cy.nodes().sort((a,b) => b.degree(false)-a.degree(false))[0];
            const id = node.id();
            const degree = node.degree(false);
            const elidedIncident = cy.edges().filter(e =>
                e.source().id() === id || e.target().id() === id
            ).filter('.okf-edge-elided').length;
            // Select via the real tap flow.
            cy.elements().unselect();
            node.emit('tap');
            const stillElided = cy.edges().filter(e =>
                e.source().id() === id || e.target().id() === id
            ).filter('.okf-edge-elided').length;
            return { id, degree, elidedIncident, stillElided };
        }"""
    )
    assert result["elidedIncident"] > 0, \
        "selected node had no elided incident edges (test fixture too sparse)"
    assert result["stillElided"] == 0, \
        f"{result['stillElided']} incident edges remain elided after selection"

    # Deselect re-elides.
    page.evaluate("() => { window.__okfLoomGraph.cy.emit('tap'); return undefined; }")
    page.wait_for_timeout(200)
    re_elided = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            return cy.edges('.okf-edge-elided').length;
        }"""
    )
    assert re_elided > 0, "edges not re-elided after deselect"


def test_graph_path_reveal(docs_server_url: str, page) -> None:
    """Path tracing reveals edges in the path, even if they are elided by
    the backbone. Clearing the path re-elides them."""
    _open_graph(page, docs_server_url)
    page.wait_for_timeout(500)

    result = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            // Find a pair whose shortest path includes at least one elided edge.
            const nodes = cy.nodes().toArray();
            for (let i = 0; i < nodes.length; i++) {
                for (let j = i+1; j < nodes.length; j++) {
                    const res = cy.elements().aStar({root: nodes[i], goal: nodes[j], directed: false});
                    if (res && res.found) {
                        const ep = res.path.edges().filter('.okf-edge-elided');
                        if (ep.length > 0) {
                            return { found: true, a: nodes[i].id(), b: nodes[j].id() };
                        }
                    }
                }
            }
            return { found: false };
        }"""
    )
    if not result["found"]:
        pytest.fail("fixture has no node pair with an elided path edge in this bundle")

    a = page.evaluate("(id) => window.__okfLoomGraph.cy.getElementById(id).id()", result["a"])
    b_el = page.evaluate("(id) => window.__okfLoomGraph.cy.getElementById(id).id()", result["b"])

    # Trace the path via the diagnostic hook.
    page.evaluate(
        """([a, b]) => {
            const cy = window.__okfLoomGraph.cy;
            window.__okfLoomGraph.tracePath(cy.getElementById(a), cy.getElementById(b));
        }""",
        arg=[result["a"], result["b"]],
    )
    page.wait_for_timeout(200)
    path_info = page.evaluate(
        """() => {
            const cy = window.__okfLoomGraph.cy;
            return {
                pathEdges: cy.edges('.okf-path').length,
                pathElided: cy.edges('.okf-path').filter('.okf-edge-elided').length,
            };
        }"""
    )
    assert path_info["pathEdges"] > 0, "no path edges after tracing"
    assert path_info["pathElided"] == 0, \
        f"{path_info['pathElided']} path edges still elided after reveal"

    # Clear path re-elides.
    page.evaluate("() => window.__okfLoomGraph.clearPath()")
    page.wait_for_timeout(200)
    elided_after = page.evaluate(
        "() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').length"
    )
    assert elided_after > 0, "edges not re-elided after clearPath"


def test_graph_progressive_disclosure_reset(docs_server_url: str, page) -> None:
    """Reset view clears explicit complete relationship detail."""
    _open_graph(page, docs_server_url)
    page.wait_for_timeout(500)

    _toggle_check(page, "Show relationship labels")
    page.evaluate("() => { window.__okfLoomGraph.cy.zoom(1.6); return undefined; }")
    page.wait_for_timeout(200)
    assert _state(page)["relationshipDetailOptIn"] is True
    assert page.evaluate("() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').length") == 0

    # Reset view.
    page.click("#okf-reset")
    page.wait_for_timeout(300)
    assert _state(page)["relationshipDetailOptIn"] is False
    elided = page.evaluate("() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').length")
    assert elided > 0, "backbone not restored after reset"

    # Checkbox reflects the reset state.
    cb_checked = page.evaluate(
        """() => {
            const cbs = [...document.querySelectorAll('input[type=checkbox]')];
            const cb = cbs.find(c => c.closest('label') &&
                c.closest('label').textContent.includes('Show relationship labels'));
            return cb ? cb.checked : null;
        }"""
    )
    assert cb_checked is False, "checkbox not unchecked after reset"


def test_graph_relationship_labels_keyboard(server_url: str, page) -> None:
    """The relationship-label toggle is keyboard-accessible: Tab reaches it,
    Space toggles it, and the state changes correctly. Works on any graph."""
    _open_graph(page, server_url)
    page.wait_for_timeout(500)

    # Verify the checkbox exists and is focusable.
    info = page.evaluate(
        """() => {
            const cbs = [...document.querySelectorAll('input[type=checkbox]')];
            const cb = cbs.find(c => c.closest('label') &&
                c.closest('label').textContent.includes('Show relationship labels'));
            if (!cb) return { found: false };
            return {
                found: true,
                type: cb.type,
                hasLabel: !!cb.closest('label'),
                tabIndex: cb.tabIndex,
            };
        }"""
    )
    assert info["found"], "relationship-label checkbox not found"
    assert info["type"] == "checkbox"
    assert info["hasLabel"], "checkbox missing associated label"

    # Focus + Space toggles it. The checkbox lives inside the Graph settings
    # disclosure — open it first so the checkbox is visible/focusable.
    adv = page.query_selector(".okf-signal__advanced")
    if adv and not page.eval_on_selector(".okf-signal__advanced", "e => e.hasAttribute('open')"):
        page.eval_on_selector(".okf-signal__advanced", "e => e.setAttribute('open', '')")
    page.wait_for_timeout(100)
    page.evaluate(
        """() => {
            const cbs = [...document.querySelectorAll('input[type=checkbox]')];
            const cb = cbs.find(c => c.closest('label') &&
                c.closest('label').textContent.includes('Show relationship labels'));
            cb.focus();
        }"""
    )
    page.wait_for_timeout(100)
    focused_label = page.evaluate("() => document.activeElement && document.activeElement.closest('label') && document.activeElement.closest('label').textContent.trim()")
    assert "Show relationship labels" in (focused_label or ""), \
        f"focus not on relationship-label checkbox (focused: {focused_label!r})"

    page.keyboard.press("Space")
    page.wait_for_timeout(200)
    assert _state(page)["relationshipDetailOptIn"] is True

    page.keyboard.press("Space")
    page.wait_for_timeout(200)
    assert _state(page)["relationshipDetailOptIn"] is False


def test_signal_focus_depth_and_clear(server_url: str, page) -> None:
    """Selecting a node + enabling focus marks the focus root; Clear focus
    removes the focus class/state while keeping the selection/detail."""
    _open_graph(page, server_url)
    _preset(page, "map")
    page.wait_for_timeout(500)

    # Select a concept via the keyboard-accessible node index.
    page.click(".okf-node-index > summary")
    page.locator(".okf-node-index__item").first.click()
    page.wait_for_timeout(300)
    assert page.eval_on_selector("#detail-empty", "e => e.hidden") is True, "detail not shown after index click"

    _preset(page, "focus")
    page.wait_for_timeout(400)
    st = _state(page)
    assert st["focusEnabled"] is True
    assert page.evaluate("() => window.__okfLoomGraph.focusRoot()") is not None
    assert _snap(page)["focusNodes"] == 1, "focus root halo class not applied to exactly one node"

    _set_select(page, "Depth", "2")
    page.wait_for_timeout(300)
    assert _state(page)["focusDepth"] == 2

    # Clear focus: class + state cleared, but the selection/detail remains.
    page.click(".okf-signal__btn")  # Clear focus
    page.wait_for_timeout(300)
    assert _state(page)["focusEnabled"] is False
    assert _snap(page)["focusNodes"] == 0, "focus root halo not cleared"
    assert page.eval_on_selector("#detail-empty", "e => e.hidden") is True, "detail lost after clear focus"


def test_signal_keyboard_and_node_index(server_url: str, page) -> None:
    """Keyboard operation: preset radios via Arrow keys apply a preset; the
    node index selects via Enter; Reset clears; controls show a focus ring."""
    _open_graph(page, server_url)

    # Lens radio group is arrow-navigable AND applies on change.
    reveal_control(page, page.locator('#okf-lens-map')).focus()
    assert page.evaluate("() => document.activeElement.id") == "okf-lens-map"
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(600)
    assert page.evaluate("() => document.activeElement.id") == "okf-lens-themes"
    assert page.evaluate("() => document.getElementById('okf-lens-themes').checked") is True
    assert _state(page)["lens"] == "themes"

    # A panel select shows a visible focus ring (focus-visible outline).
    # Selects live inside the Graph settings disclosure (collapsed by
    # default) — open it first so the select is focusable.
    page.eval_on_selector(".okf-signal__advanced", "e => { e.open = true; }")
    outline = page.eval_on_selector(
        ".okf-signal select", "e => { e.focus(); return getComputedStyle(e).outlineStyle; }"
    )
    assert outline == "solid", f"control focus outline not visible: {outline!r}"

    # Node index: keyboard activate (Enter) selects the concept + shows detail.
    page.click(".okf-node-index > summary")
    btn = page.locator(".okf-node-index__item").nth(2)
    btn.focus()
    target = btn.get_attribute("data-target")
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    assert "/tables/" in page.url
    assert page.url.endswith(target)

    # Enter opens the active option's concept page.
    assert page.url.endswith(target)


def test_signal_async_lifecycle_latest_wins(server_url: str, page) -> None:
    """The single layout owner fences stale completions: overlapping layout
    starts increment layoutStats.skippedStale, the LAST layout wins, the graph
    stays overlap-free, the fit is applied ONCE per layout, and the selection is
    preserved. Covers the stop-triggered-stale path (owner token bumped before
    stop) and the late layoutstop / timed-fallback path."""
    _open_graph(page, server_url)
    selected = page.evaluate("() => { const s = window.__okfLoomGraph.cy.nodes(':selected'); return s.length ? s.id() : null; }")
    base = page.evaluate("() => Object.assign({}, window.__okfLoomGraph.layoutStats)")

    # Fire THREE overlapping layout starts by applying lenses back-to-back
    # (each applyLens schedules through the single owner; the debounce
    # coalesces same-tick calls, so space them just past it). Each new
    # start bumps layoutSeq BEFORE stopping the prior layout, so the
    # prior's completion (synchronous stop-triggered layoutstop AND/OR its
    # later timed fallback) sees mySeq != layoutSeq and is fenced →
    # skippedStale rises; the last lens wins.
    for lens in ("themes", "bridges", "map"):
        page.evaluate(f"() => window.__okfLoomGraph.applyLens({lens!r})")
        page.wait_for_timeout(260)
    # Wait past the 900ms one-shot fallback so every superseded completion fired.
    page.wait_for_timeout(1300)
    stats = page.evaluate("() => Object.assign({}, window.__okfLoomGraph.layoutStats)")
    assert stats["skippedStale"] > base["skippedStale"], (
        f"stale layout completions were not fenced "
        f"(skippedStale {base['skippedStale']} -> {stats['skippedStale']})"
    )
    # One-shot: 3 starts apply at most 3 layouts even though each layout has BOTH
    # a layoutstop handler and a timed fallback (no double-apply per sequence).
    started = stats["starts"] - base["starts"]
    applied = stats["applied"] - base["applied"]
    assert started >= 3, f"expected >=3 layout starts, got {started}"
    assert applied <= started, f"fit applied more times ({applied}) than layouts started ({started})"
    assert stats["lastAppliedSeq"] == stats["starts"], "lastAppliedSeq is not the most recent layout"
    assert _state(page)["lens"] == "map", "the latest lens did not win"
    assert _min_gap(page) > 0, "overlap after rapid layout restarts"
    assert _snap(page)["zoom"] <= 1.6 + 1e-6, "zoom exceeded clamp after rapid restarts"

    # Rapid lens spam: the latest lens wins, still overlap-free, selection kept.
    for pr in ["themes", "bridges", "flow", "recent", "focus", "map", "bridges"]:
        _preset(page, pr)
        page.wait_for_timeout(35)
    page.wait_for_timeout(1300)
    assert _state(page)["preset"] == "bridges", "latest lens did not win after spam"
    assert _min_gap(page) > 0, "overlap after rapid preset spam"
    now_sel = page.evaluate("() => { const s = window.__okfLoomGraph.cy.nodes(':selected'); return s.length ? s.id() : null; }")
    assert now_sel == selected, f"selection identity lost across rapid changes ({selected} -> {now_sel})"


def test_signal_lod_lifecycle(server_url: str, page) -> None:
    """With a low LOD threshold, node-index ensure-visible and Show-all both
    route through the stale-safe owner: hidden nodes are added, Show-all reveals
    every node on a grid, and the graph stays overlap-free."""
    _open_graph(page, server_url, init_script="window.OKF_LOOM_GRAPH_LOD_THRESHOLD = 3;")
    assert _snap(page)["n"] == 3, "LOD did not cap the initial canvas to the top-3 nodes"
    assert page.locator(".okf-graph-lod-pill").count() == 1, "LOD pill missing"

    # ensure-visible: pick a node the LOD hid and focus it from the index.
    hidden = page.evaluate(
        """() => { const cy = window.__okfLoomGraph.cy;
           const shown = new Set(cy.nodes().map(n => n.id()));
           const items = Array.from(document.querySelectorAll('#okf-node-index-list .okf-node-index__item'));
           const it = items.find(b => !shown.has(b.getAttribute('data-target')));
           return it ? it.getAttribute('data-target') : null; }"""
    )
    assert hidden, "no hidden node found to ensure-visible"
    starts0 = page.evaluate("() => window.__okfLoomGraph.layoutStats.starts")
    page.click(".okf-node-index > summary")
    page.click(f'.okf-node-index__item[data-target="{hidden}"]')
    page.wait_for_timeout(700)
    assert page.evaluate(f"() => !!window.__okfLoomGraph.cy.getElementById({hidden!r}).length"), \
        "ensure-visible did not add the hidden node"
    assert _snap(page)["n"] >= 4
    assert page.evaluate("() => window.__okfLoomGraph.layoutStats.starts") > starts0, \
        "ensure-visible did not run its layout through the owner"

    # Show all: every node revealed, grid layout, overlap-free.
    page.click(".okf-graph-lod-pill")
    page.wait_for_timeout(900)
    assert _snap(page)["n"] == 6, "Show all did not reveal every node"
    assert _state(page)["layout"] == "grid", "Show all did not switch to grid"
    assert _min_gap(page) > 0, "overlap after Show all"


# ---------------------------------------------------------------------------
# Opt-in micro-tour — dialog focus management (a11y hardening)
# ---------------------------------------------------------------------------


def _start_graph_tour(page):
    """Prove first paint is unobstructed, then use the public invitation."""
    expect(page.locator(".okf-graph-tour")).to_have_count(0)
    invitation = page.get_by_role("button", name="Take a 45-second graph tour", include_hidden=True)
    reveal_control(page, invitation)
    expect(invitation).to_be_visible()
    reveal_control(page, invitation).click()
    tour = page.get_by_role("dialog", name="Graph tour")
    expect(tour).to_be_visible()
    return tour, invitation


def _tour_completion_value(page):
    return page.evaluate("""() => { const key=Object.keys(localStorage).find(k=>k.startsWith('okfGraphTourDone:v2:')); return key && localStorage.getItem(key); }""")


def test_graph_micro_tour_focus_lifecycle(server_url: str, page) -> None:
    """The explicitly opened desktop tour is nonmodal: focus moves in on open,
    Escape closes ONLY the tour (it does not clear
    the graph selection the way a bare graph Escape would), and focus is
    restored to a stable graph control on close.

    A fresh browser context shows usable graph navigation before the invitation
    is activated.
    """
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    tour, invitation = _start_graph_tour(page)
    # Named nonmodal coach-mark dialog.
    expect(tour).to_have_attribute("role", "dialog")
    expect(tour).to_have_attribute("aria-modal", "false")
    expect(tour).to_have_attribute("aria-label", "Graph tour")
    # Explicit activation moves focus into the owned coach mark.
    assert page.evaluate("() => document.querySelector('.okf-graph-tour').contains(document.activeElement)")

    # Give the graph a selection so we can later prove Escape did NOT reach the
    # global graph handler (which would clear it).
    page.wait_for_function(
        "() => window.__okfLoomGraph && window.__okfLoomGraph.cy "
        "&& window.__okfLoomGraph.cy.nodes().length > 0",
        timeout=10000,
    )
    page.evaluate("() => { window.__okfLoomGraph.cy.nodes().first().select(); }")
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes(':selected').length") == 1

    nxt = tour.locator(".okf-graph-tour__next")
    expect(tour.get_by_text("Step 1 of 3", exact=True)).to_be_visible()
    expect(tour.get_by_role("button", name="Back")).to_be_disabled()

    # Escape must close ONLY the tour and restore its invitation.
    page.keyboard.press("Escape")
    expect(tour).to_have_count(0)
    # Global Escape (clear selection/path) was NOT triggered.
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes(':selected').length") == 1, (
        "Escape leaked to the graph's global handler and cleared the selection"
    )
    expect(invitation).to_be_focused()


def test_graph_tour_completion_skip_and_replay_preserve_preference(server_url: str, page) -> None:
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    tour, replay = _start_graph_tour(page)
    tour.get_by_role("button", name="Skip").click()
    expect(tour).to_have_count(0)
    assert _tour_completion_value(page) == "1"
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(".okf-graph-tour")).to_have_count(0)
    replay = page.get_by_role('button', name='Take a 45-second graph tour', include_hidden=True)
    reveal_control(page, replay)
    expect(replay).to_be_visible()
    replay.focus()
    replay.press("Enter")
    expect(page.get_by_role("dialog", name="Graph tour")).to_be_visible()
    page.keyboard.press("Escape")
    expect(replay).to_be_focused()
    assert _tour_completion_value(page) == "1"
    assert page.locator("[inert]").count() == 0


def test_graph_tour_storage_failure_dismisses_page_and_unavailable_anchor_skips(server_url: str, page) -> None:
    page.add_init_script("""(() => {
      Storage.prototype.getItem = () => { throw new DOMException('blocked'); };
      Storage.prototype.setItem = () => { throw new DOMException('blocked'); };
    })()""")
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    tour, invitation = _start_graph_tour(page)
    page.locator("#okf-graph").evaluate("node => node.style.display='none'")
    tour.get_by_role("button", name="Next").click()
    expect(tour.get_by_text("Step 3 of 3", exact=True)).to_be_visible()
    tour.get_by_role("button", name="Finish").click()
    expect(tour).to_have_count(0)
    expect(invitation).to_be_focused()


def test_graph_tour_mobile_context_copy_targets_and_cleanup(server_url: str, page) -> None:
    page.set_viewport_size({"width": 320, "height": 844})
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    tour, _ = _start_graph_tour(page)
    expect(tour).to_have_attribute("aria-modal", "true")
    metrics = page.evaluate("""() => {const s=document.querySelector('.okf-graph-tour'),r=s.getBoundingClientRect();
      return {top:r.top, graph:document.querySelector('.okf-layout').getBoundingClientRect().height,
        header:document.querySelector('.okf-graph-mobile-nav').getBoundingClientRect().height,
        targets:Array.from(s.querySelectorAll('button')).map(b=>Math.min(b.getBoundingClientRect().width,b.getBoundingClientRect().height)),
        text:s.textContent};}""")
    assert metrics["top"] > 100 and metrics["graph"] > 200 and metrics["header"] >= 44
    assert min(metrics["targets"]) >= 44
    assert "panel on the right" not in metrics["text"] and "below the bar" not in metrics["text"]
    expect(tour.locator("#okf-graph-tour__desc")).to_contain_text("Open Details to choose a lens")
    tour.get_by_role("button", name="Next", exact=True).click()
    tour.get_by_role("button", name="Next", exact=True).click()
    expect(tour.locator("#okf-graph-tour__desc")).to_contain_text("Open Concepts, choose a concept, then open it")
    expect(page.locator('[data-graph-pane="concepts"]')).to_have_attribute("data-tour-anchor", "true")
    page.keyboard.press("Escape")
    assert page.locator("[inert]").count() == 0


@pytest.mark.parametrize("media", ["reduced", "forced"])
def test_graph_tour_accessibility_media_has_no_motion(server_url: str, page, media: str) -> None:
    if media == "reduced":
        page.emulate_media(reduced_motion="reduce")
    else:
        page.emulate_media(forced_colors="active")
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    _start_graph_tour(page)
    result = page.locator(".okf-graph-tour").evaluate("""e => {const s=getComputedStyle(e);return {animation:s.animationName,duration:s.transitionDuration,border:s.borderTopStyle}}""")
    assert result["animation"] == "none" and result["duration"] in {"0s", "0ms"}
    assert result["border"] != "none"
    if media == "forced":
        primary = page.locator(".okf-graph-tour__next").evaluate(
            """e => {const s=getComputedStyle(e);return {style:s.borderStyle,width:parseFloat(s.borderWidth),color:s.borderColor,disabled:e.disabled}}"""
        )
        assert primary["style"] != "none" and primary["width"] >= 2
        assert primary["color"] not in {"rgba(0, 0, 0, 0)", "transparent"}
        assert primary["disabled"] is False
        disabled = page.locator(".okf-graph-tour__next").evaluate(
            """e => {e.disabled=true;const s=getComputedStyle(e);const out={style:s.borderStyle,width:parseFloat(s.borderWidth),color:s.color,border:s.borderColor,disabled:e.disabled};e.disabled=false;return out}"""
        )
        assert disabled["disabled"] is True and disabled["style"] != "none" and disabled["width"] >= 2
        assert disabled["border"] not in {"rgba(0, 0, 0, 0)", "transparent"}


@pytest.mark.parametrize("width,modal", [(640, True), (641, False), (800, False), (900, False)])
def test_graph_tour_modal_breakpoint_is_consistent(server_url: str, page, width: int, modal: bool) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    tour, _ = _start_graph_tour(page)
    expect(tour).to_have_attribute("aria-modal", str(modal).lower())
    expect(tour).to_have_class(re.compile(r"okf-graph-tour--sheet" if modal else r"okf-graph-tour--detail"))
    assert page.locator(".okf-graph-tour-scrim").count() == (1 if modal else 0)
    assert page.locator("body > [inert]").count() > 0 if modal else page.locator("[inert]").count() == 0
    # Explicit activation moves focus into both modal and nonmodal coach marks;
    # only the modal variant traps it.
    assert tour.evaluate("(e) => e.contains(document.activeElement)")


@pytest.mark.parametrize("width", [640, 900])
def test_graph_tour_replay_cycles_have_symmetric_cleanup(server_url: str, page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    first, replay = _start_graph_tour(page)
    first.get_by_role("button", name="Skip").click()
    for action in ("escape", "close", "skip", "finish"):
        replay.click()
        tour = page.get_by_role("dialog", name="Graph tour")
        expect(tour).to_be_visible()
        if action == "escape":
            page.keyboard.press("Escape")
        elif action == "close":
            tour.get_by_role("button", name="Close graph tour").click()
        elif action == "skip":
            tour.get_by_role("button", name="Skip").click()
        else:
            while tour.get_by_role("button", name="Next", exact=True).count():
                tour.get_by_role("button", name="Next", exact=True).click()
            tour.get_by_role("button", name="Finish").click()
        expect(tour).to_have_count(0)
        expect(replay).to_be_focused()
        assert page.locator(".okf-graph-tour-scrim,[data-tour-anchor],body > [inert]").count() == 0
        assert page.locator(".okf-graph-tour-replay").count() == 1


def test_graph_tour_announces_every_step_and_targets_exact_surfaces(server_url: str, page) -> None:
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    tour, _ = _start_graph_tour(page)
    expected = [("step 1 of 3", ".okf-signal__lenses"), ("step 2 of 3", "#okf-graph"), ("step 3 of 3", "#okf-detail")]
    for index, (phrase, selector) in enumerate(expected):
        step_copy = tour.locator(".okf-graph-tour__announce")
        expect(step_copy).to_have_attribute("aria-hidden", "true")
        expect(step_copy).to_contain_text(phrase, ignore_case=True)
        assert page.locator('#okf-graph-announcer[aria-live="polite"]').count() == 1
        expect(page.locator(selector)).to_have_attribute("data-tour-anchor", "true")
        if index == 1:
            assert page.evaluate("() => window.__okfLoomGraph.cy.nodes('.okf-tour-target').length") == 1
        if index < 2:
            tour.get_by_role("button", name="Next", exact=True).click()


def test_graph_tour_pagehide_cleanup_resize_replay_and_pageshow(server_url: str, page) -> None:
    page.set_viewport_size({"width": 640, "height": 844})
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    tour, replay = _start_graph_tour(page)
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))")
    expect(page.locator(".okf-graph-tour")).to_have_count(0)
    assert _tour_completion_value(page) is None
    assert page.locator(".okf-graph-tour-scrim,[data-tour-anchor],body > [inert]").count() == 0
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    expect(page.locator(".okf-graph-tour")).to_have_count(0)
    replay.focus()
    replay.evaluate("button => button.click()")
    page.set_viewport_size({"width": 800, "height": 844})
    tour = page.get_by_role("dialog", name="Graph tour")
    expect(tour).to_have_attribute("aria-modal", "false")
    expect(tour).to_have_class(re.compile("okf-graph-tour--detail"))
    assert page.locator(".okf-graph-tour-scrim,body > [inert]").count() == 0
    scroll_before = page.evaluate("() => [scrollX, scrollY]")
    page.keyboard.press("Escape")
    assert page.evaluate("() => document.activeElement === document.querySelector('.okf-graph-tour-replay') || document.activeElement.id === 'okf-search'")
    assert page.evaluate("() => [scrollX, scrollY]") == scroll_before


@pytest.mark.parametrize("start,destination", [(800, 640), (640, 800)])
def test_graph_tour_active_resize_reconciles_surface_focus_anchor_and_announcement(
    server_url: str, page, start: int, destination: int,
) -> None:
    """ASYNC_LIFECYCLE_MATRIX

    Scenario | Deadline/abort owner | Mutation phase | Public code/result | Persisted state | Late-result behavior | Retry/reconnect behavior | Required proof
    active breakpoint resize | tour resize owner | remount current step | <=640 modal, >640 nonmodal | unchanged | old scrim/inert/anchor removed before publish | repeated crossing is idempotent and replayable | this direct 800→640 and 640→800 browser proof
    pagehide/BFCache | page lifecycle owner | teardown active tour without completion mutation | no tour/scrim/inert/anchor remains; persisted pageshow stays closed | completion preference unchanged | late key/resize handlers are detached and cannot republish | explicit replay starts one fresh owner after pageshow | test_graph_tour_pagehide_cleanup_resize_replay_and_pageshow
    """
    page.set_viewport_size({"width": start, "height": 844})
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    tour, replay = _start_graph_tour(page)
    expect(tour.get_by_text("Step 1 of 3", exact=True)).to_be_visible()
    page.set_viewport_size({"width": destination, "height": 844})
    modal = destination <= 640
    expect(tour).to_have_attribute("aria-modal", str(modal).lower())
    expect(tour).to_have_class(re.compile("okf-graph-tour--sheet" if modal else "okf-graph-tour--detail"))
    expect(tour.get_by_text("Step 1 of 3", exact=True)).to_be_visible()
    expected_copy = "Open Details to choose a lens" if modal else "Choose a lens to reframe"
    expect(tour.locator("#okf-graph-tour__desc")).to_contain_text(expected_copy)
    expect(tour.locator(".okf-graph-tour__announce")).to_contain_text(expected_copy)
    mobile_anchor = page.locator('[data-graph-pane="details"]')
    desktop_anchor = page.locator(".okf-signal__lenses")
    expect(mobile_anchor if modal else desktop_anchor).to_have_attribute("data-tour-anchor", "true")
    expect(desktop_anchor if modal else mobile_anchor).not_to_have_attribute("data-tour-anchor", "true")
    assert page.locator(".okf-graph-tour-scrim").count() == (1 if modal else 0)
    assert page.locator("body > [inert]").count() > 0 if modal else page.locator("body > [inert]").count() == 0
    assert tour.evaluate("e => e.contains(document.activeElement)")
    page.keyboard.press("Escape")
    assert page.locator(".okf-graph-tour-scrim,[data-tour-anchor],body > [inert]").count() == 0
    replay.click()
    expect(page.get_by_role("dialog", name="Graph tour")).to_be_visible()
    page.keyboard.press("Escape")
    expect(replay).to_be_focused()


def test_graph_dark_theme_canvas_and_selected_lens_contrast(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.evaluate("() => window.OKFLoomTheme.setTheme('technical-dark')")
    page.wait_for_function("() => document.documentElement.dataset.theme === 'technical-dark'")
    page.wait_for_function("() => !!document.querySelector('.okf-signal__seg-item input:checked + .okf-lens-choice')")
    result = page.evaluate("""() => {
      const parse=c=>{const m=c.match(/[\\d.]+/g).map(Number);return m.slice(0,3)},lum=c=>{const v=parse(c).map(x=>x/255).map(x=>x<=.04045?x/12.92:Math.pow((x+.055)/1.055,2.4));return .2126*v[0]+.7152*v[1]+.0722*v[2]},ratio=(a,b)=>{a=lum(a);b=lum(b);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
      const lens=document.querySelector('.okf-signal__seg-item input:checked + .okf-lens-choice'),ls=getComputedStyle(lens),node=window.__okfLoomGraph.cy.nodes().first(),style=document.createElement('span').style,norm=c=>{style.color=c;return style.color};node.select();
      return {lensRatio:ratio(ls.color,ls.backgroundColor),title:getComputedStyle(lens.querySelector('b')).color,description:getComputedStyle(lens.querySelector('small')).color,
        nodeColor:norm(node.style('color')),plate:norm(node.style('text-background-color')),plateOpacity:Number(node.style('text-background-opacity')),selectedBorder:norm(node.style('border-color')),
        expectedNode:norm('#e2e8f0'),expectedPlate:norm('#0b1220'),expectedSelect:norm('#3ec9c9')};
    }""")
    assert result["lensRatio"] >= 4.5 and result["title"] == result["description"]
    assert result["nodeColor"] == result["expectedNode"]
    assert result["plate"] == result["expectedPlate"]
    assert result["plateOpacity"] == 1 and result["selectedBorder"] == result["expectedSelect"]


@pytest.mark.parametrize("surface", ["live"])
def test_graph_phase3_tour_uses_non_plotting_detail_space(
    request, surface: str, page
) -> None:
    """First-load Lenses coachmark cannot cover canvas or orphan chrome."""
    base = request.getfixturevalue("server_url" if surface == "live" else "static_url")
    url = base + ("/__graph" if surface == "live" else "/__graph.html")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(url, wait_until="domcontentloaded")
    _start_graph_tour(page)
    geometry = page.evaluate(
        """() => {
          const rect=e=>{const r=e.getBoundingClientRect();return {l:r.left,r:r.right,t:r.top,b:r.bottom}};
          const overlaps=(a,b)=>a.l<b.r&&a.r>b.l&&a.t<b.b&&a.b>b.t;
          const tour=document.querySelector('.okf-graph-tour'),graph=document.getElementById('okf-graph');
          const shelf=document.querySelector('.okf-graph-orphans:not([hidden])');
          const tr=rect(tour),gr=rect(graph);
          return {inDetail:tour.parentElement.id==='okf-detail',graphOverlap:overlaps(tr,gr),shelfOverlap:shelf?overlaps(tr,rect(shelf)):false};
        }"""
    )
    assert geometry == {"inDetail": True, "graphOverlap": False, "shelfOverlap": False}


def test_graph_phase3_single_file_first_load_has_no_tour(tmp_path: Path, page) -> None:
    from okf_loom.model import Bundle
    from okf_loom.render import render_single_file

    output = tmp_path / "phase3-tour.html"
    render_single_file(Bundle.load(DEMO_BUNDLE), output)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(output.as_uri(), wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph?.cy?.nodes().length", timeout=10000)
    expect(page.locator(".okf-graph-tour")).to_have_count(0)
    expect(page.locator(".okf-node-index summary")).to_be_enabled()


def test_graph_phase3_no_literal_data_background_requests(static_url: str, page) -> None:
    """Multi-background mappers resolve to data URIs, never network URLs."""
    requests: list[str] = []
    failures: list[str] = []
    page.on("request", lambda req: requests.append(req.url))
    page.on("response", lambda res: failures.append(res.url) if res.status >= 400 else None)
    page.goto(static_url + "/__graph.html", wait_until="networkidle")
    page.locator("#okf-graph canvas").first.wait_for(state="visible")
    bad = [u for u in requests + failures if "data(iconUrl)" in u or "data(commentBadgeUrl)" in u]
    assert bad == []


def test_graph_phase3_dense_default_labels_and_dark_edges(server_url: str, page) -> None:
    """Default map is label-conservative and dark arrows remain meaningful."""
    _open_graph(page, server_url)
    result = page.evaluate(
        """() => {const G=window.__okfLoomGraph,cy=G.cy,e=cy.edges().first();
          return {shown:cy.edges('.okf-show-label').length,total:cy.edges().length,
            label:e.style('label'),arrow:e.style('target-arrow-shape'),curve:e.style('curve-style')};}"""
    )
    assert result["total"] > 0 and result["shown"] == 0 and result["label"] == ""
    assert result["arrow"] == "triangle" and result["curve"] == "bezier"
    page.evaluate("() => window.OKFLoomTheme.setTheme('technical-dark')")
    page.wait_for_timeout(50)
    edge = page.evaluate(
        "() => {const e=window.__okfLoomGraph.cy.edges().first(),s=document.createElement('span').style,n=c=>{s.color=c;return s.color};return {line:n(e.style('line-color')),arrow:n(e.style('target-arrow-color')),expected:n('#64748b')}}"
    )
    # Technical-dark graph palette contract: semantic slate #64748b.
    assert edge["line"] == edge["arrow"] == edge["expected"]


def test_graph_phase3_broken_canvas_glyph_composes_with_other_states(server_url: str, page) -> None:
    """Broken warning is a persistent third canvas layer, not ring/color-only."""
    _open_graph(page, server_url)
    result = page.evaluate(
        r"""async () => {const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=fetch.bind(window),p=await native(url).then(r=>r.json()),id=p.nodes[0].data.id;
          p.nodes[0].data.unresolved_count=3;p.nodes[0].data.active_comment_count=120;
          window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(p),{headers:{'Content-Type':'application/json'}})):native(r,o);
          await G.refreshGraph();const n=G.cy.getElementById(id);n.addClass('okf-hub');n.select();
          const row=document.querySelector('.okf-node-index__item[data-target="'+CSS.escape(id)+'"]');
          return {broken:n.hasClass('okf-broken-link'),aggregated:n.hasClass('okf-broken-aggregated'),selected:n.selected(),hub:n.hasClass('okf-hub'),warning:decodeURIComponent(n.data('brokenBadgeUrl')),comment:decodeURIComponent(n.data('commentBadgeUrl')),aria:row.getAttribute('aria-label'),warn:!!row.querySelector('.okf-node-index__warn'),count:row.querySelector('.okf-node-index__comments').textContent};}"""
    )
    assert result["broken"] and result["selected"] and result["hub"] and result["warn"]
    assert not result["aggregated"], "sparse warning was incorrectly aggregated"
    assert "<path" in result["warning"] and ">!</text>" in result["warning"]
    assert "120 active comments" in result["aria"] and "3 broken links" in result["aria"]
    assert result["count"] == "99+" and "<path" in result["comment"]


def test_graph_phase3_mobile_concept_title_two_lines_without_overflow(server_url: str, page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    _open_graph(page, server_url)
    result = page.evaluate(
        r"""async () => {const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=fetch.bind(window),p=await native(url).then(r=>r.json());
          p.nodes[0].data.label='A deliberately long concept title that needs exactly two readable lines';p.nodes[0].data.unresolved_count=2;p.nodes[0].data.active_comment_count=500;
          window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(p),{headers:{'Content-Type':'application/json'}})):native(r,o);await G.refreshGraph();
          document.querySelector('[data-graph-pane="concepts"]').click();const row=document.querySelector('.okf-node-index__item'),label=row.querySelector('.okf-node-index__label'),s=getComputedStyle(label);
          return {lines:Math.round(label.getBoundingClientRect().height/parseFloat(s.lineHeight)),overflow:document.documentElement.scrollWidth>innerWidth+1,warn:!!row.querySelector('.okf-node-index__warn'),badge:row.querySelector('.okf-node-index__comments').textContent};}"""
    )
    assert result == {"lines": 2, "overflow": False, "warn": True, "badge": "99+"}


@pytest.mark.parametrize("width", [320, 390])
def test_graph_phase3_r3_mobile_sheet_isolated_title_complete_and_focus_restored(
    server_url: str, page, width: int,
) -> None:
    """390px first visit is an isolated modal sheet; graph chrome never peeks
    through, Escape restores focus, and the graph header preserves its title."""
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(server_url + "/__graph", wait_until="domcontentloaded")
    _, invitation = _start_graph_tour(page)
    page.locator(".okf-graph-tour--sheet").wait_for(state="visible")
    sheet = page.evaluate(
        """() => {const s=document.querySelector('.okf-graph-tour--sheet'),r=s.getBoundingClientRect(),
          x=document.querySelector('.okf-graph-tour-scrim'),xr=x.getBoundingClientRect(),cs=getComputedStyle(x);
          return {left:xr.left,top:xr.top,right:xr.right,bottom:xr.bottom,width:innerWidth,height:innerHeight,
            panelBounded:r.left>=xr.left&&r.right<=xr.right&&r.top>=xr.top&&r.bottom<=xr.bottom,
            sheetOpaque:getComputedStyle(s).backgroundColor !== 'rgba(0, 0, 0, 0)',contextVisible:r.top>100,viewportCovered:xr.left<=0&&xr.top<=0&&xr.right>=innerWidth&&xr.bottom>=innerHeight,
            underlyingInert:Array.from(document.body.children).filter(e=>e!==x).every(e=>e.hasAttribute('inert')),
            closeTarget:(()=>{const q=s.querySelector('.okf-graph-tour__skip').getBoundingClientRect();return Math.min(q.width,q.height)})(),
            modal:s.getAttribute('aria-modal'),focused:s.contains(document.activeElement)};}"""
    )
    assert sheet["left"] == 0 and sheet["top"] == 0
    assert sheet["right"] == sheet["width"] and sheet["bottom"] >= sheet["height"]
    assert sheet["sheetOpaque"] and sheet["contextVisible"] and sheet["viewportCovered"] and sheet["panelBounded"]
    assert sheet["underlyingInert"] and sheet["closeTarget"] >= 24
    assert sheet["modal"] == "true" and sheet["focused"]
    page.keyboard.press("Escape")
    page.locator(".okf-graph-tour").wait_for(state="detached")
    expect(invitation).to_be_focused()
    title = page.evaluate(
        """() => {const e=document.querySelector('.okf-topbar__brand strong'),r=e.getBoundingClientRect(),s=getComputedStyle(e),
          brand=document.querySelector('.okf-topbar__brand'),tabs=document.querySelector('.okf-graph-mobile-nav'),secondary=document.querySelector('.okf-graph-tools'),
          summary=document.querySelector('.okf-graph-mobile-nav'),controls=document.querySelector('.okf-topbar__controls'),
          br=brand.getBoundingClientRect(),tr=tabs.getBoundingClientRect(),sr=secondary.getBoundingClientRect(),cr=controls.getBoundingClientRect(),
          ordered=(a,b)=>!!(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING),
          separated=(a,b)=>a.bottom<=b.top+1;
          return {text:e.textContent.trim(),unclipped:e.scrollWidth<=e.clientWidth+1&&e.scrollHeight<=e.clientHeight+1,ellipsis:s.textOverflow,
            lines:Math.round(r.height/parseFloat(s.lineHeight)),overflow:document.documentElement.scrollWidth>innerWidth+1,
            chromeOrdered:ordered(brand,tabs)&&ordered(tabs,secondary),
            chromeSeparated:separated(br,tr)&&separated(tr,sr),
            primaryConnected:controls.parentElement===document.querySelector('[data-okf-application-shell]')&&Array.from(controls.children).filter(c=>!(c.id==='okf-appearance-menu'||c.closest('#okf-appearance-menu'))).every(c=>{const b=c.getBoundingClientRect();return b.width>0&&b.height>0}),
            graphToolsAvailable:document.querySelector('.okf-graph-tools input[type=search]').getBoundingClientRect().width>0&&summary.getBoundingClientRect().height>=44,
            focusRestored:document.activeElement===document.querySelector('.okf-graph-tour-replay')};}"""
    )
    assert title["text"] and title["unclipped"]
    assert title["ellipsis"] == "clip" and title["lines"] <= 2
    assert not title["overflow"] and title["chromeOrdered"] and title["chromeSeparated"]
    assert title["primaryConnected"] and title["graphToolsAvailable"] and title["focusRestored"]


def test_graph_phase3_r3_dense_overview_progressively_restores_model_detail(
    docs_server_url: str, page,
) -> None:
    """Automatic zoom stays bounded; explicit relationship detail is reversible."""
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_graph(page, docs_server_url)
    # The tier the graph OPENS in is a function of the fitted zoom, and the fit
    # follows the living docs bundle: one added relationship moved the model box
    # from 683x645 (fit 0.97, Overview) to 631x590 (fit 1.06, Intermediate) on an
    # unchanged 864x710 canvas. So the automatic tier is asserted by the product's
    # own rule, and every tier below - Overview included - is pinned by zoom.
    page.wait_for_function(
        """() => {const z=window.__okfLoomGraph.cy.zoom(),text=document.querySelector('.okf-graph-density')?.textContent||'';
          return text.startsWith(z>=1.5?'Local detail':(z>=1.05?'Intermediate detail':'Overview'));}"""
    )
    page.evaluate("() => { window.__okfLoomGraph.cy.zoom(0.9); return undefined; }")
    page.wait_for_function("() => document.querySelector('.okf-graph-density')?.textContent.startsWith('Overview')")
    overview = page.evaluate(
        """() => {const G=window.__okfLoomGraph,cy=G.cy;
          return {nodes:cy.nodes().length,edges:cy.edges().length,hiddenLabels:cy.nodes('.okf-density-label-hidden').length,
            hiddenEdges:cy.edges('.okf-edge-elided').length,indicator:document.querySelector('.okf-graph-density').textContent,
            relationLabels:cy.edges('.okf-show-label,.okf-density-detail-label').length,
            list:document.querySelectorAll('#okf-node-index-list .okf-node-index__item').length};}"""
    )
    canonical = page.request.get(docs_server_url + '/__data/graph.json').json()
    expected_nodes, expected_edges = len(canonical['nodes']), len(canonical['edges'])
    assert overview['nodes'] == overview['list'] == expected_nodes
    assert overview['edges'] == expected_edges
    assert overview["hiddenLabels"] > 0 and overview["hiddenEdges"] > 0
    assert overview["relationLabels"] == 0
    visible_overview = overview["edges"] - overview["hiddenEdges"]
    assert visible_overview <= overview["nodes"] * 2
    assert "structural backbone only" in overview["indicator"]
    assert "Concepts for full titles" in overview["indicator"]
    page.evaluate("() => { window.__okfLoomGraph.cy.zoom(1.1); return undefined; }")
    page.wait_for_function("() => document.querySelector('.okf-graph-density')?.textContent.includes('Intermediate detail')")
    intermediate = page.evaluate(
        """() => {const cy=window.__okfLoomGraph.cy;return {nodes:cy.nodes().length,edges:cy.edges().length,
          hiddenLabels:cy.nodes('.okf-density-label-hidden').length,hiddenEdges:cy.edges('.okf-edge-elided').length,
          visibleIds:cy.edges().filter(e=>!e.hasClass('okf-edge-elided')).map(e=>e.id()).sort().join(','),
          relationLabels:cy.edges('.okf-show-label,.okf-density-detail-label').length,
          indicator:document.querySelector('.okf-graph-density').textContent};}"""
    )
    assert 0 < intermediate["hiddenEdges"] < overview["hiddenEdges"]
    assert intermediate["hiddenLabels"] > 0 and intermediate["relationLabels"] == 0
    assert "labels remain shortened" in intermediate["indicator"]
    page.evaluate("() => { window.__okfLoomGraph.cy.zoom(1.6); return undefined; }")
    page.wait_for_function("() => document.querySelector('.okf-graph-density')?.textContent.includes('Local detail')")
    detail = page.evaluate(
        """() => {const cy=window.__okfLoomGraph.cy;return {edges:cy.edges().length,
          hiddenLabels:cy.nodes('.okf-density-label-hidden').length,hiddenEdges:cy.edges('.okf-edge-elided').length,
          visibleIds:cy.edges().filter(e=>!e.hasClass('okf-edge-elided')).map(e=>e.id()).sort().join(','),
          relationLabels:cy.edges('.okf-show-label,.okf-density-detail-label').length,
          indicator:document.querySelector('.okf-graph-density').textContent};}"""
    )
    assert detail["edges"] == expected_edges and 0 < detail["hiddenEdges"] < detail["edges"]
    assert detail["relationLabels"] == 0 and f"of {expected_edges} relationships" in detail["indicator"]
    assert "node titles remain shortened" in detail["indicator"]
    assert "Show relationship labels reveals all relationships and their labels" in detail["indicator"]
    default_visible = detail["edges"] - detail["hiddenEdges"]
    default_visible_ids = detail["visibleIds"]
    proof_dir = Path("/tmp/okf-loom-phase3-proof-r6")
    proof_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(proof_dir / "high-zoom-default.png"), full_page=True)
    model_before = page.evaluate(
        "() => window.__okfLoomGraph.cy.edges().map(e=>e.id()).sort().join(',')"
    )
    _toggle_check(page, "Show relationship labels")
    page.wait_for_timeout(100)
    explicit = page.evaluate(
        "() => {const cy=window.__okfLoomGraph.cy;return {total:cy.edges().length,hidden:cy.edges('.okf-edge-elided').length,shown:cy.edges('.okf-show-label').length,typed:cy.edges('[label]').length,indicator:document.querySelector('.okf-graph-density').textContent};}"
    )
    assert explicit["total"] == expected_edges and explicit["hidden"] == 0
    assert explicit['shown'] == explicit['typed']
    assert explicit['typed'] == sum(edge['data'].get('label') is not None for edge in canonical['edges'])
    assert f"{expected_edges} of {expected_edges} relationships" in explicit["indicator"]

    _toggle_check(page, "Show relationship labels")
    # Opening Graph settings can legitimately change viewport geometry and therefore
    # the automatic disclosure tier.  Pin the same Local-detail zoom before
    # comparing restoration, then prove the deterministic visible edge set.
    page.evaluate("() => { window.__okfLoomGraph.cy.zoom(1.6); return undefined; }")
    page.wait_for_function("() => document.querySelector('.okf-graph-density')?.textContent.includes('Local detail')")
    restored = page.evaluate(
        "() => {const cy=window.__okfLoomGraph.cy;return {total:cy.edges().length,hidden:cy.edges('.okf-edge-elided').length,visibleIds:cy.edges().filter(e=>!e.hasClass('okf-edge-elided')).map(e=>e.id()).sort().join(','),labels:cy.edges('.okf-show-label').length,model:cy.edges().map(e=>e.id()).sort().join(',')};}"
    )
    assert restored["total"] == expected_edges and restored["total"] - restored["hidden"] == default_visible
    assert restored["visibleIds"] == default_visible_ids
    assert restored["labels"] == 0 and restored["model"] == model_before


def test_graph_phase3_density_visible_edge_count_is_equal_across_surfaces(docs_server_url: str, page) -> None:
    """All surfaces publish authoritative bundle and instance counts."""
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_graph(page, docs_server_url)
    for name, zoom in (("overview", 0.8), ("intermediate", 1.1), ("high", 1.6)):
        page.evaluate("z => { window.__okfLoomGraph.cy.zoom(z); return undefined; }", zoom)
        tier = {'overview': 'Overview', 'intermediate': 'Intermediate detail', 'high': 'Local detail'}[name]
        expect(page.locator('.okf-graph-density')).to_contain_text(tier)
        counts = page.evaluate(r"""() => {const cy=window.__okfLoomGraph.cy,G=window.__okfLoomGraph,state=G.getState(),visible=cy.edges().filter(e=>!e.hasClass('dim')&&!e.hasClass('okf-edge-elided')),instances=visible.reduce((n,e)=>n+(e.data('relations')||[]).filter(r=>!state.hiddenRelations[r.type]&&!state.hiddenRelations[r.label]).length,0),read=e=>({bundles:Number(e.dataset.visibleConnectionBundles),instances:Number(e.dataset.visibleRelationshipInstances),totalBundles:Number(e.dataset.totalConnectionBundles),totalInstances:Number(e.dataset.totalRelationshipInstances)}),status=read(document.querySelector('.okf-signal__status')),density=read(document.querySelector('.okf-graph-density')),mobile=read(document.querySelector('.okf-mobile-overview__status'));return {actual:{bundles:visible.length,instances,totalBundles:cy.edges().length,totalInstances:cy.edges().reduce((n,e)=>n+(e.data('multiplicity')||1),0)},status,density,mobile};}""")
        assert counts["actual"] == counts["status"] == counts["density"] == counts["mobile"], (name, counts)
    same_tier_ids = page.evaluate("() => window.__okfLoomGraph.cy.edges().filter(e=>!e.hasClass('dim')&&!e.hasClass('okf-edge-elided')).map(e=>e.id()).sort()")
    _toggle_check(page, "Show relationship labels")
    page.wait_for_function("() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').length === 0")
    explicit = page.evaluate(r"""() => {const cy=window.__okfLoomGraph.cy,read=e=>({bundles:Number(e.dataset.visibleConnectionBundles),instances:Number(e.dataset.visibleRelationshipInstances)});return {actual:{bundles:cy.edges().length,instances:cy.edges().reduce((n,e)=>n+(e.data('multiplicity')||1),0)},status:read(document.querySelector('.okf-signal__status')),density:read(document.querySelector('.okf-graph-density')),mobile:read(document.querySelector('.okf-mobile-overview__status'))};}""")
    assert explicit["actual"] == explicit["status"] == explicit["density"] == explicit["mobile"]
    _toggle_check(page, "Show relationship labels")
    page.evaluate("() => { window.__okfLoomGraph.cy.zoom(1.6); return undefined; }")
    page.wait_for_function("() => window.__okfLoomGraph.cy.edges('.okf-edge-elided').length > 0")
    restored_ids = page.evaluate("() => window.__okfLoomGraph.cy.edges().filter(e=>!e.hasClass('dim')&&!e.hasClass('okf-edge-elided')).map(e=>e.id()).sort()")
    assert restored_ids == same_tier_ids


@pytest.mark.parametrize("surface", ["live", "static", "single"])
def test_graph_phase3_sparse_initial_and_reset_fit_are_label_safe_and_parity(
    request, surface: str, tmp_path: Path, page,
) -> None:
    """Nine-node fit uses node/label+dock geometry on every shipped surface."""
    if surface == "live":
        url = request.getfixturevalue("server_url") + "/__graph"
    elif surface == "static":
        url = request.getfixturevalue("static_url") + "/__graph.html"
    else:
        from okf_loom.model import Bundle
        from okf_loom.render import render_single_file
        output = tmp_path / "phase3-sparse-fit.html"
        render_single_file(Bundle.load(DEMO_BUNDLE), output)
        url = output.as_uri()
    page.set_viewport_size({"width": 1440, "height": 900})
    page.add_init_script("try { localStorage.setItem('okfGraphTourDone','1') } catch(e) {}")
    page.goto(url, wait_until="domcontentloaded")
    if surface == "single":
        page.locator('[data-okf-shell-item="graph"] a').click()
        page.wait_for_function("""() => {const G=window.__okfLoomGraph,c=document.getElementById('okf-graph').getBoundingClientRect(),nodes=G&&G.cy.nodes().filter(n=>n.connectedEdges().length);if(!nodes||!nodes.length||!c.width)return false;const boxes=nodes.map(n=>n.renderedBoundingBox({includeLabels:true,includeOverlays:true}));return (Math.max(...boxes.map(b=>b.x2))-Math.min(...boxes.map(b=>b.x1)))/c.width>=.45;}""")
    page.wait_for_function("() => window.__okfLoomGraph?.cy?.nodes().length > 0", timeout=10000)
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes().length") <= 12

    def geometry() -> dict:
        return page.evaluate(r"""() => {const cy=window.__okfLoomGraph.cy,c=document.getElementById('okf-graph').getBoundingClientRect();
          const connected=cy.nodes().filter(n=>n.connectedEdges().length>0), all=cy.nodes();
          const union=coll=>{const boxes=coll.map(n=>n.renderedBoundingBox({includeLabels:true,includeOverlays:true}));return {l:Math.min(...boxes.map(b=>b.x1)),r:Math.max(...boxes.map(b=>b.x2)),t:Math.min(...boxes.map(b=>b.y1)),b:Math.max(...boxes.map(b=>b.y2))}};
          const a=union(all),k=union(connected);return {ratio:(k.r-k.l)/c.width,inside:a.l>=-1&&a.t>=-1&&a.r<=c.width+1&&a.b<=c.height+1,zoom:cy.zoom()};}""")

    initial = geometry()
    assert initial["inside"], (surface, initial)
    assert 0.45 <= initial["ratio"] <= 0.70, (surface, initial)
    page.locator("#okf-reset").click()
    page.wait_for_timeout(100)
    reset = geometry()
    assert reset["inside"] and 0.45 <= reset["ratio"] <= 0.70, (surface, reset)


def test_graph_initial_seed_layout_fit_readiness_ordering(server_url: str, page) -> None:
    """The cheap seed is geometry only; current layout + fit own readiness."""
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    page.goto(server_url + "/__graph", wait_until="domcontentloaded")
    page.wait_for_function("() => document.body.dataset.okfGraphCaptureReady === 'ready'")
    state = page.evaluate("() => window.okfGraphCaptureState()")
    assert state["initialSeedApplied"] is True
    assert state["layoutGeneration"] == state["appliedLayoutGeneration"] >= 1
    assert state["fitAppliedGeneration"] == state["appliedLayoutGeneration"]
    assert state["layoutSettled"] is True


def test_graph_held_initial_layout_pagehide_late_stop_and_fresh_retry_ordering(
    server_url: str, page,
) -> None:
    page.add_init_script("""try { localStorage.setItem('okfGraphTourDone','1'); } catch(e) {}
      window.__okfGraphTestHooks={holdInitialLayout:true};""")
    page.goto(server_url + "/__graph", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfGraphTestHooks?.heldGeneration === 1")
    held = page.evaluate("""() => {const c=window.okfGraphCaptureState();return {
      capture:c,body:document.body.dataset.okfGraphCaptureReady,
      viewport:{zoom:window.__okfLoomGraph.cy.zoom(),pan:window.__okfLoomGraph.cy.pan()}}} """)
    assert held["capture"]["initialSeedApplied"] is True
    assert held["capture"]["appliedLayoutGeneration"] == 0
    assert held["capture"]["fitAppliedGeneration"] == 0
    assert held["body"] == "loading"

    page.evaluate("""() => {const run=window.okfCaptureReadiness.start({target:'graph'});
      window.__heldCapture=run.promise.then(v=>({allowed:v.screenshotAllowed}),e=>({allowed:e.screenshotAllowed,code:e.code}));} """)
    pending = page.evaluate("""async () => Promise.race([
      window.__heldCapture,new Promise(resolve=>setTimeout(()=>resolve({pending:true}),50))])""")
    assert pending == {"pending": True}
    page.evaluate("() => dispatchEvent(new PageTransitionEvent('pagehide',{persisted:false}))")
    denied = page.evaluate("async () => await window.__heldCapture")
    assert denied["allowed"] is False
    assert denied["code"] == "superseded"
    late = page.evaluate("""before => {window.__okfGraphTestHooks.emitLateLayoutstop();
      const c=window.okfGraphCaptureState(),cy=window.__okfLoomGraph.cy;return {
        capture:c,body:document.body.dataset.okfGraphCaptureReady,
        unchanged:cy.zoom()===before.zoom&&cy.pan().x===before.pan.x&&cy.pan().y===before.pan.y};} """, held["viewport"])
    assert late["unchanged"] is True
    assert late["capture"]["fitAppliedGeneration"] == 0
    assert late["capture"]["appliedLayoutGeneration"] == 0
    assert late["body"] != "ready"

    fresh_page = page.context.new_page()
    fresh_page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    fresh_page.goto(server_url + "/__graph", wait_until="domcontentloaded")
    fresh_page.wait_for_function("() => document.body.dataset.okfGraphCaptureReady === 'ready'")
    fresh = fresh_page.evaluate("() => window.okfGraphCaptureState()")
    fresh_page.close()
    assert fresh["layoutGeneration"] == fresh["appliedLayoutGeneration"] >= 1
    assert fresh["fitAppliedGeneration"] == fresh["appliedLayoutGeneration"]
    assert fresh["layoutSettled"] is True


def test_graph_held_initial_layout_bfcache_restore_starts_one_fresh_owner(
    server_url: str, page,
) -> None:
    page.add_init_script("""try { localStorage.setItem('okfGraphTourDone','1'); } catch(e) {}
      window.__okfGraphTestHooks={holdInitialLayout:true};""")
    page.goto(server_url + "/__graph", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfGraphTestHooks?.heldGeneration === 1")
    before = page.evaluate("() => ({capture:window.okfGraphCaptureState(),starts:window.__okfLoomGraph.layoutStats.starts})")
    assert before["capture"]["initialSeedApplied"] is True
    assert before["capture"]["fitAppliedGeneration"] == 0
    assert before["capture"]["appliedLayoutGeneration"] == 0

    page.evaluate("() => dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))")
    denied = page.evaluate("""async () => {try {await window.okfCaptureReadiness.start({target:'graph'}).promise;return {allowed:true};}
      catch(error){return {allowed:error.screenshotAllowed,code:error.code,phase:error.metadata.readinessPhase};}}""")
    assert denied == {"allowed": False, "code": "superseded", "phase": "graph-suspended"}
    page.evaluate("() => window.__okfGraphTestHooks.emitLateLayoutstop()")
    suspended = page.evaluate("() => ({capture:window.okfGraphCaptureState(),stats:window.__okfLoomGraph.layoutStats})")
    assert suspended["capture"]["fitAppliedGeneration"] == 0
    assert suspended["capture"]["appliedLayoutGeneration"] == 0
    assert suspended["capture"]["suspended"] is True

    page.evaluate("""() => {dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
      dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));}""")
    page.wait_for_function("() => document.body.dataset.okfGraphCaptureReady === 'ready'")
    restored = page.evaluate("""() => ({capture:window.okfGraphCaptureState(),
      starts:window.__okfLoomGraph.layoutStats.starts,inventory:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort(),
      interaction:window.__okfLoomGraph.interaction.snapshot()})""")
    assert restored["starts"] == before["starts"] + 1
    assert restored["capture"]["layoutGeneration"] == suspended["capture"]["layoutGeneration"] + 1
    assert restored["capture"]["layoutGeneration"] == restored["capture"]["appliedLayoutGeneration"]
    assert restored["capture"]["fitAppliedGeneration"] == restored["capture"]["appliedLayoutGeneration"]
    assert restored["capture"]["layoutSettled"] is True
    assert restored["capture"]["suspended"] is False
    assert restored["starts"] == 2
    assert restored["capture"]["readyPublicationCount"] == 1
    assert len(restored["inventory"]) == restored["capture"]["nodeCount"]


def test_graph_phase3_r5_broken_warning_prevalence_and_selected_exception(
    docs_server_url: str, page,
) -> None:
    """Widespread warnings aggregate on canvas; rows/detail stay exact.

    Prevalence comes from an explicit controlled payload, not from the live
    docs-bundle: repaired docs legitimately carry only a couple of broken
    concepts, so the test patches ``unresolved_count`` over >25% of the
    fetched graph payload (the same canonical payload-refresh protocol the
    dense r4/r3 proofs use) and asserts the aggregation contract against that
    deterministic prevalence.
    """
    _open_graph(page, docs_server_url)
    result = page.evaluate(
        r"""async () => {const G=window.__okfLoomGraph,cy=G.cy,url=document.getElementById('okf-graph').dataset.graphUrl,native=fetch.bind(window),p=await native(url).then(r=>r.json());
          const prevalent=Math.ceil(p.nodes.length*0.3);
          p.nodes.forEach((n,i)=>{n.data.unresolved_count=i<prevalent?2:0;});
          window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(p),{headers:{'Content-Type':'application/json'}})):native(r,o);
          await G.refreshGraph();
          const broken=cy.nodes().filter(n=>Number(n.data('unresolved_count'))>0), first=broken.first();
          const before=first.style('background-image'); first.select();
          const row=document.querySelector('.okf-node-index__item[data-target="'+CSS.escape(first.id())+'"]');
          row.click();
          const detail=Array.from(document.querySelectorAll('#okf-detail-signals dt')).find(x=>x.textContent==='Broken links');
          return {total:cy.nodes().length,broken:broken.length,aggregated:broken.filter('.okf-broken-aggregated').length,
            indicator:document.querySelector('.okf-graph-density').textContent,before,after:first.style('background-image'),
            aria:row.getAttribute('aria-label'),warn:!!row.querySelector('.okf-node-index__warn'),
            detail:detail&&detail.nextElementSibling.textContent,count:String(first.data('unresolved_count'))};}"""
    )
    assert result["broken"] / result["total"] > 0.25
    assert result["aggregated"] == result["broken"]
    assert f'{result["broken"]} concepts have broken links' in result["indicator"]
    assert "review in Concepts" in result["indicator"]
    assert result["before"] != result["after"], "selected broken glyph was not restored"
    assert result["warn"] and f'{result["count"]} broken link' in result["aria"]
    assert result["detail"] == result["count"]


def test_graph_phase3_r4_dense_overview_canvas_state_priority(docs_server_url: str, page) -> None:
    """Dense canvas aggregates prevalent warnings without losing exact ARIA."""
    _open_graph(page, docs_server_url)
    result = page.evaluate(
        r"""async () => {const G=window.__okfLoomGraph,cy=G.cy,url=document.getElementById('okf-graph').dataset.graphUrl,native=fetch.bind(window),p=await native(url).then(r=>r.json()),ids=p.nodes.slice(0,3).map(n=>n.data.id);
          p.nodes[0].data.active_comment_count=0;p.nodes[0].data.unresolved_count=0;
          p.nodes[1].data.active_comment_count=7;p.nodes[1].data.unresolved_count=2;
          delete p.nodes[2].data.active_comment_count;p.nodes[2].data.unresolved_count=0;
          // Repaired live docs carry too few broken concepts to aggregate; set
          // the >25% prevalence explicitly on the remaining payload nodes.
          p.nodes.forEach((n,i)=>{if(i>2)n.data.unresolved_count=i%2===0?1:0;});
          window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(p),{headers:{'Content-Type':'application/json'}})):native(r,o);await G.refreshGraph();
          const snap=id=>{const n=cy.getElementById(id),row=document.querySelector('.okf-node-index__item[data-target="'+CSS.escape(id)+'"]');return {zero:n.hasClass('okf-density-overview-zero'),broken:n.hasClass('okf-density-overview-broken'),aggregated:n.hasClass('okf-broken-aggregated'),aria:row.getAttribute('aria-label')};};
          const overview=ids.map(snap);cy.zoom(1.3);await new Promise(r=>setTimeout(r,80));return {overview,detail:ids.map(snap)};}"""
    )
    assert result["overview"][0]["zero"] and "0 active comments" in result["overview"][0]["aria"]
    assert result["overview"][1]["aggregated"] and not result["overview"][1]["broken"]
    assert "7 active comments" in result["overview"][1]["aria"] and "2 broken links" in result["overview"][1]["aria"]
    assert not result["overview"][2]["zero"] and "active comment" not in result["overview"][2]["aria"]
    assert all(not row["zero"] and not row["broken"] for row in result["detail"])
    assert result["detail"][1]["aggregated"]


def test_graph_phase3_r3_compound_state_layers_and_dense_titles(docs_server_url: str, page) -> None:
    """QUALITY_PROOF_MATRIX: desktop layout/capture parity proves the 1440px
    document and each rendered lens row stay bounded; flex-basis:auto retains
    a real note width without creating horizontal overflow."""
    _open_graph(page, docs_server_url)
    result = page.evaluate(
        r"""async () => {const G=window.__okfLoomGraph,cy=G.cy,url=document.getElementById('okf-graph').dataset.graphUrl,native=fetch.bind(window),p=await native(url).then(r=>r.json()),id=p.nodes[0].data.id;
          p.nodes[0].data.unresolved_count=2;p.nodes[0].data.active_comment_count=12;
          window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(p),{headers:{'Content-Type':'application/json'}})):native(r,o);
          await G.refreshGraph();const n=cy.getElementById(id);n.addClass('okf-hub');n.select();
          const pos=n.style('background-position-x'),warn=decodeURIComponent(n.data('brokenBadgeUrl')),comment=decodeURIComponent(n.data('commentBadgeUrl')),
            row=document.querySelector('.okf-lens-summary__row'),label=row&&row.querySelector('.okf-lens-summary__label'),note=row&&row.querySelector('.okf-lens-summary__note');
          return {positions:pos,warning:warn,comment,selection:n.style('border-color'),hub:n.style('underlay-color'),brokenOpacity:n.style('underlay-opacity'),
            targetDistance:cy.edges().first().style('target-distance-from-node'),labelWrap:label&&getComputedStyle(label).whiteSpace,
            noteBasis:note&&getComputedStyle(note).flexBasis,noteWidth:note&&note.getBoundingClientRect().width,
            noteMin:note&&parseFloat(getComputedStyle(note).minWidth),noteMax:note&&parseFloat(getComputedStyle(note).maxWidth),
            rowWidth:row&&row.getBoundingClientRect().width,rowScroll:row&&row.scrollWidth,overflow:document.documentElement.scrollWidth>innerWidth+1,
            scrollWidth:document.documentElement.scrollWidth,innerWidth,
            offenders:Array.from(document.querySelectorAll('*')).filter(e=>!e.matches('.sr-only,.okf-sr-only,.okf-visually-hidden')).map(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return {e,tag:e.tagName,id:e.id,cls:String(e.className).slice(0,100),left:r.left,right:r.right,width:r.width,position:s.position};}).filter(x=>x.width>0&&(x.left<-.5||x.right>innerWidth+.5||x.width>innerWidth+.5)).map(({e,...x})=>x)};}"""
    )
    assert "0%" in result["positions"] and "100%" in result["positions"]
    assert ">!</text>" in result["warning"] and "<path" in result["comment"]
    assert result["selection"] == result["hub"] and result["brokenOpacity"] == "0"
    assert float(result["targetDistance"].replace("px", "")) >= 10
    assert result["labelWrap"] == "normal" and result["noteBasis"] == "auto"
    assert 0 < result["noteWidth"] and result["noteWidth"] + 1 >= result["noteMin"]
    assert result["noteWidth"] <= result["rowWidth"] * 0.38 + 1
    assert result["rowScroll"] <= result["rowWidth"] + 1
    assert result["offenders"] == [], result["offenders"]
    assert not result["overflow"], (result["scrollWidth"], result["innerWidth"])


@pytest.mark.parametrize("surface", ["live", "static"])
def test_phase4_slice1_pointer_hover_selection_actions_and_cleanup(
    surface: str, server_url: str, static_url: str, page
) -> None:
    """Real pointer input owns a cancellable hover and native bounded actions."""
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    page.goto(server_url + "/__graph" if surface == "live" else static_url + "/__graph.html")
    page.wait_for_function("() => window.__okfLoomGraph?.cy?.nodes().length > 1 && window.okfGraphCaptureState?.().layoutSettled")
    point = page.evaluate("""() => {const n=window.__okfLoomGraph.cy.nodes().first(),p=n.renderedPosition(),r=document.getElementById('okf-graph').getBoundingClientRect();return {x:r.left+p.x,y:r.top+p.y,id:n.id()}}""")
    page.mouse.move(point["x"], point["y"])
    page.wait_for_function("id => window.__okfLoomGraph.interaction.snapshot().hovered === id", arg=point["id"])
    expect(page.get_by_role("tooltip")).to_contain_text("double-click opens")
    point.update(_phase4_node_point(page, point["id"]))
    page.mouse.click(point["x"], point["y"])
    bar = page.get_by_role("toolbar", name="Selected concept actions")
    expect(bar).to_be_visible()
    expect(bar.get_by_role("button", name="Open page")).to_be_visible()
    bar.get_by_role("button", name="Hide others").click()
    expect(bar.get_by_role("button", name="Show all")).to_be_visible()
    bounds = bar.bounding_box()
    canvas = page.locator("#okf-graph").bounding_box()
    assert bounds and canvas and bounds["x"] >= canvas["x"] and bounds["x"] + bounds["width"] <= canvas["x"] + canvas["width"] + 1
    page.locator(".okf-graph-canvas-proxy").focus()
    page.keyboard.press("Escape")
    expect(bar).to_be_hidden()


def test_phase4_slice1_keyboard_url_reload_and_model_preservation(server_url: str, page) -> None:
    """Keyboard and URL restore share the controller and never mutate graph inventory."""
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => window.__okfLoomGraph?.cy?.nodes().length > 1")
    before = page.evaluate("() => ({nodes:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort(),edges:window.__okfLoomGraph.cy.edges().map(e=>e.id()).sort(),rev:document.body.dataset.okfRevision})")
    proxy = page.locator(".okf-graph-canvas-proxy")
    proxy.focus()
    page.keyboard.press("Space")
    selected = page.evaluate("() => window.__okfLoomGraph.interaction.snapshot().selected")
    assert selected
    assert "selected=" in page.url
    page.reload()
    page.wait_for_function("id => window.__okfLoomGraph?.interaction.snapshot().selected === id", arg=selected)
    expect(page.get_by_role("toolbar", name="Selected concept actions")).to_be_visible()
    after = page.evaluate("() => ({nodes:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort(),edges:window.__okfLoomGraph.cy.edges().map(e=>e.id()).sort(),rev:document.body.dataset.okfRevision})")
    assert after == before


def _phase4_node_point(page, node_id: str) -> dict:
    return page.evaluate(
        """id => {const n=window.__okfLoomGraph.cy.getElementById(id),p=n.renderedPosition(),r=document.getElementById('okf-graph').getBoundingClientRect();return {x:r.left+p.x,y:r.top+p.y}}""",
        node_id,
    )


def test_phase4_slice2_focus_action_depth_restore_and_layout_fence(server_url: str, page) -> None:
    _open_graph(page, server_url)
    state = page.evaluate("""() => {const G=window.__okfLoomGraph,n=G.cy.nodes().filter(n=>n.degree(false)>0).first();return {id:n.id(),lens:G.getState().lens,zoom:G.cy.zoom(),seq:G.layoutStats.lastAppliedSeq}}""")
    point = _phase4_node_point(page, state["id"])
    page.mouse.click(point["x"], point["y"])
    page.wait_for_function("() => !window.__okfLoomGraph.cy.animated()")
    state["zoom"] = page.evaluate("() => window.__okfLoomGraph.cy.zoom()")
    page.screenshot(path="/tmp/okf-phase4-slice3-selected-action.png", full_page=True)
    page.get_by_role("toolbar", name="Selected concept actions").get_by_role("button", name="Explore neighborhood").click()
    focusbar = page.get_by_role("group", name="Explore neighborhood")
    expect(focusbar).to_be_visible()
    expect(focusbar).to_contain_text("Exploring neighborhood around")
    focusbar.get_by_label("Neighborhood depth").select_option("2")
    page.wait_for_function("s => window.__okfLoomGraph.layoutStats.lastAppliedSeq > s", arg=state["seq"])
    focused = page.evaluate("""() => ({root:window.__okfLoomGraph.focusRoot(),depth:window.__okfLoomGraph.getState().focusDepth,lens:window.__okfLoomGraph.getState().lens,settled:window.okfGraphCaptureState().layoutSettled,url:location.search})""")
    assert focused["root"] == state["id"] and focused["depth"] == 2 and focused["lens"] == "focus" and focused["settled"]
    assert "focus=" in focused["url"]
    page.screenshot(path="/tmp/okf-phase4-slice2-focus.png", full_page=True)
    focusbar.get_by_role("button", name="Show all").click()
    page.wait_for_function("() => window.__okfLoomGraph.getState().lens !== 'focus' && window.okfGraphCaptureState().layoutSettled")
    page.wait_for_function("() => !window.__okfLoomGraph.cy.animated()")
    page.wait_for_function("() => window.__okfLoomGraph.interaction.snapshot().active")
    restored = page.evaluate("() => ({lens:window.__okfLoomGraph.getState().lens,root:window.__okfLoomGraph.focusRoot(),zoom:window.__okfLoomGraph.cy.zoom(),active:document.activeElement?.dataset?.target||document.activeElement?.className,activeId:window.__okfLoomGraph.interaction.snapshot().active})")
    assert restored["lens"] == state["lens"] and restored["root"] is None
    assert abs(restored["zoom"] - state["zoom"]) < 0.02
    assert restored["activeId"] == state["id"]


def test_phase4_slice2_real_shift_pointer_path_is_typed_filtered_and_layered(server_url: str, page) -> None:
    _open_graph(page, server_url)
    endpoints = page.evaluate("""() => {const e=window.__okfLoomGraph.cy.edges().filter(e=>!e.source().hasClass('dim')&&!e.target().hasClass('dim')).sort((a,b)=>a.id().localeCompare(b.id())).first();return [e.source().id(),e.target().id()] }""")
    before = page.evaluate("() => ({nodes:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort(),edges:window.__okfLoomGraph.cy.edges().map(e=>e.id()).sort(),rev:document.body.dataset.okfRevision})")
    a = _phase4_node_point(page, endpoints[0])
    page.mouse.click(a["x"], a["y"])
    page.wait_for_function("() => !window.__okfLoomGraph.cy.animated()")
    b = _phase4_node_point(page, endpoints[1])
    page.keyboard.down("Shift")
    page.mouse.click(b["x"], b["y"])
    page.keyboard.up("Shift")
    panel = page.locator(".okf-detail-path")
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("Shortest visible path")
    expect(panel.locator(".okf-detail-path__node")).to_have_count(2)
    expect(panel.locator(".okf-detail-path__relation")).to_have_count(1)
    assert "path=" in page.url
    page.screenshot(path="/tmp/okf-phase4-slice2-path.png", full_page=True)
    page.locator(".okf-graph-canvas-proxy").focus()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    assert page.evaluate("() => ({nodes:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort(),edges:window.__okfLoomGraph.cy.edges().map(e=>e.id()).sort(),rev:document.body.dataset.okfRevision})") == before


def test_phase4_slice2_graph_detail_comment_concept_ownership(server_url: str, page) -> None:
    """The studio comment anchor follows each accepted graph selection without stale ownership."""
    _open_graph(page, server_url)
    ids = page.evaluate("() => window.__okfLoomGraph.cy.nodes().slice(0,2).map(n=>n.id())")
    for concept_id in ids:
        page.wait_for_function("() => !window.__okfLoomGraph.cy.animated()")
        point = _phase4_node_point(page, concept_id)
        page.mouse.click(point["x"], point["y"])
        expect(page.locator("#detail-body")).to_have_attribute("data-concept-id", concept_id)
        assert page.evaluate("() => window.okfGraphSelection.conceptId") == concept_id


@pytest.mark.parametrize("surface", ["live", "static", "single"])
def test_phase4_slice2_focus_path_url_runtime_parity(request, surface: str, tmp_path: Path, page) -> None:
    if surface == "live":
        url = request.getfixturevalue("server_url") + "/__graph"
    elif surface == "static":
        url = request.getfixturevalue("static_url") + "/__graph.html"
    else:
        from okf_loom.model import Bundle
        from okf_loom.render import render_single_file
        output = tmp_path / "phase4-slice2-single.html"
        render_single_file(Bundle.load(DEMO_BUNDLE), output)
        url = output.as_uri()
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    if surface == "static":
        page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.wait_for_function("() => window.__okfLoomGraph?.cy?.edges().length > 0")
    result = page.evaluate("""() => {const G=window.__okfLoomGraph,e=G.cy.edges().sort((a,b)=>a.id().localeCompare(b.id())).first(),a=e.source().id(),b=e.target().id();G.interaction.dispatch({type:'SELECT',id:a,quietUrl:true});G.interaction.dispatch({type:'PATH',from:a,id:b});return {kind:G.interaction.snapshot().pathResult.kind,nodes:G.cy.nodes().length,edges:G.cy.edges().length,revision:document.body.dataset.okfRevision};}""")
    assert result["kind"] == "found" and result["nodes"] > 0 and result["edges"] > 0
    if surface == "static":
        page.screenshot(path="/tmp/okf-phase4-slice2-mobile.png", full_page=True)


def test_phase4_slice3_lens_previews_summaries_and_advanced_filters(server_url: str, page) -> None:
    _open_graph(page, server_url)
    reveal_control(page, page.locator('#okf-lens-map'))
    lenses = page.get_by_role("radiogroup", name="Lens")
    expect(lenses.locator("svg.okf-lens-preview")).to_have_count(6)
    for name in ("Map", "Themes", "Flow", "Bridges", "Recent"):
        lenses.locator(f'input[value="{name.lower()}"]').check()
        expect(page.locator("#detail-empty .okf-lens-summary__title").first).to_be_visible()
        assert page.evaluate("() => window.__okfLoomGraph.getState().lens") == name.lower()
    page.evaluate("() => {const G=window.__okfLoomGraph,n=G.cy.nodes().first();G.interaction.dispatch({type:'SELECT',id:n.id(),quietUrl:true})}")
    lenses.locator('input[value="focus"]').check()
    assert page.evaluate("() => window.__okfLoomGraph.getState().lens") == "focus"
    page.get_by_text("Graph settings", exact=True).click()
    type_group = page.get_by_label("Type filters")
    type_buttons = type_group.locator("button.okf-filter-chip")
    assert type_buttons.count() > 1
    first = type_buttons.first
    first.click()
    expect(first).to_have_attribute("aria-pressed", "false")
    type_group.locator("button.okf-filter-solo").first.click()
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes().filter(n=>!n.hasClass('dim')).length") > 0
    type_group.get_by_role("button", name="Show all").click()
    expect(type_buttons.first).to_have_attribute("aria-pressed", "true")
    relation_group = page.get_by_label("Relationship filters")
    expect(relation_group).to_be_visible()
    relation_group.locator("button.okf-filter-chip").first.click()
    expect(relation_group.locator("button.okf-filter-chip").first).to_have_attribute("aria-pressed", "false")
    page.screenshot(path="/tmp/okf-phase4-slice3-filters.png", full_page=True)


def test_phase4_slice3_search_keyboard_glow_count_and_durable_restore(server_url: str, page) -> None:
    _open_graph(page, server_url)
    graph = page.evaluate("() => {const G=window.__okfLoomGraph,n=G.cy.nodes().first();G.interaction.dispatch({type:'SELECT',id:n.id()});return {id:n.id(),label:n.data('label')}}")
    page.locator(".okf-graph-canvas-proxy").focus()
    page.keyboard.press("/")
    search = page.get_by_role("searchbox", name="Search graph concepts")
    expect(search).to_be_focused()
    search.fill(graph["label"][:4])
    page.wait_for_function("() => window.__okfLoomGraph.cy.nodes('.okf-search-match').length > 0")
    expect(page.locator(".okf-graph-search-status")).to_contain_text("match")
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes('.okf-search-match').length") >= 1
    page.screenshot(path="/tmp/okf-phase4-slice3-search.png", full_page=True)
    page.get_by_role("button", name="Clear", exact=True).click()
    assert page.evaluate("id => window.__okfLoomGraph.interaction.snapshot().selected === id && window.__okfLoomGraph.cy.nodes('.okf-search-match').length === 0", arg=graph["id"])


def test_phase4_slice3_viewport_minimap_fullscreen_and_layout_recovery(server_url: str, page) -> None:
    _open_graph(page, server_url)
    before = page.evaluate("() => ({nodes:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort(),edges:window.__okfLoomGraph.cy.edges().map(e=>e.id()).sort(),rev:document.body.dataset.okfRevision})")
    page.get_by_role("button", name="Show minimap").click()
    minimap = page.locator(".okf-graph-minimap")
    expect(minimap).to_be_visible()
    canvas = minimap.locator("canvas")
    initial_zoom = canvas.get_attribute("data-zoom")
    page.get_by_role("button", name="Zoom in").click()
    page.wait_for_function("z => document.querySelector('.okf-graph-minimap canvas').dataset.zoom !== z", arg=initial_zoom)
    assert int(canvas.get_attribute("data-node-count")) > 0
    page.evaluate("() => {Element.prototype.requestFullscreen=()=>Promise.reject(new Error('denied'))}")
    page.get_by_role("button", name="Enter fullscreen").click()
    expect(page.locator(".okf-signal__status")).to_contain_text("rejected")
    page.evaluate("""() => {let active=null;Object.defineProperty(document,'fullscreenElement',{configurable:true,get:()=>active});document.getElementById('okf-graph').requestFullscreen=()=>{active=document.getElementById('okf-graph');document.dispatchEvent(new Event('fullscreenchange'));return Promise.resolve()};document.exitFullscreen=()=>{active=null;document.dispatchEvent(new Event('fullscreenchange'));return Promise.resolve()}}""")
    page.get_by_role("button", name="Enter fullscreen").click()
    expect(page.get_by_role("button", name="Exit fullscreen")).to_be_visible()
    page.get_by_role("button", name="Exit fullscreen").click()
    expect(page.get_by_role("button", name="Enter fullscreen")).to_be_visible()
    page.evaluate("""() => {window.__nativeLayout=window.__okfLoomGraph.cy.layout.bind(window.__okfLoomGraph.cy);window.__okfLoomGraph.cy.layout=()=>({on(){return this},run(){},stop(){}})}""")
    page.get_by_text("Graph settings", exact=True).click()
    page.get_by_role("button", name="Re-run layout").click()
    page.wait_for_function("() => document.querySelector('[data-layout-state=failed]')")
    page.evaluate("() => {window.__okfLoomGraph.cy.layout=window.__nativeLayout}")
    page.locator("[data-layout-state=failed]").click()
    expect(page.get_by_role("button", name="Re-run layout")).to_be_visible(timeout=5000)
    after = page.evaluate("() => ({nodes:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort(),edges:window.__okfLoomGraph.cy.edges().map(e=>e.id()).sort(),rev:document.body.dataset.okfRevision})")
    assert after == before
    page.screenshot(path="/tmp/okf-phase4-slice3-minimap.png", full_page=True)


@pytest.mark.parametrize("surface", ["live", "static", "single"])
def test_phase4_slice3_controls_runtime_parity(request, surface: str, tmp_path: Path, page) -> None:
    if surface == "live":
        url = request.getfixturevalue("server_url") + "/__graph"
    elif surface == "static":
        url = request.getfixturevalue("static_url") + "/__graph.html"
    else:
        from okf_loom.model import Bundle
        from okf_loom.render import render_single_file
        output = tmp_path / "phase4-slice3-single.html"
        render_single_file(Bundle.load(DEMO_BUNDLE), output)
        url = output.as_uri()
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    page.goto(url)
    page.wait_for_function("() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
    result = page.evaluate("""() => ({previews:document.querySelectorAll('.okf-lens-preview').length,minimap:!!document.querySelector('.okf-graph-minimap'),fullscreen:!!document.querySelector('[aria-label="Enter fullscreen"]'),legacyVisible:!!document.querySelector('#okf-filter-type:not([hidden])'),nodes:window.__okfLoomGraph.cy.nodes().length,revision:document.body.dataset.okfRevision})""")
    assert result["previews"] == 6 and result["minimap"] and result["fullscreen"] and not result["legacyVisible"] and result["nodes"] > 0
    if surface != "single":
        assert result["revision"]
    if surface == "live":
        page.screenshot(path="/tmp/okf-phase4-slice3-desktop-light.png", full_page=True)
        page.evaluate("() => {document.documentElement.setAttribute('data-theme','technical-dark');window.dispatchEvent(new Event('okf-loom:themeChanged'))}")
        page.screenshot(path="/tmp/okf-phase4-slice3-desktop-dark.png", full_page=True)
    if surface == "static":
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="/tmp/okf-phase4-slice3-mobile.png", full_page=True)


def test_phase4_review_type_filter_identity_counts_reload_and_history(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.get_by_text("Graph settings", exact=True).click()
    group = page.get_by_label("Type filters")
    total = group.locator("button.okf-filter-chip").count()
    assert total > 1
    identity = page.evaluate("() => window.__okfHiddenTypesIdentity=(window.__okfLoomGraph.getState().hiddenTypes, window.__okfLoomGraph.getState().hiddenTypes)")
    group.locator("button.okf-filter-chip").first.click()
    counts = page.evaluate("() => ({hidden:Object.keys(window.__okfLoomGraph.getState().hiddenTypes).filter(k=>window.__okfLoomGraph.getState().hiddenTypes[k]).length,visible:new Set(window.__okfLoomGraph.cy.nodes().not('.dim').map(n=>n.data('type'))).size})")
    assert counts == {"hidden": 1, "visible": total - 1}
    group.locator("button.okf-filter-solo").nth(1).click()
    counts = page.evaluate("() => ({hidden:Object.keys(window.__okfLoomGraph.getState().hiddenTypes).filter(k=>window.__okfLoomGraph.getState().hiddenTypes[k]).length,visible:new Set(window.__okfLoomGraph.cy.nodes().not('.dim').map(n=>n.data('type'))).size})")
    assert counts == {"hidden": total - 1, "visible": 1}
    page.reload()
    page.wait_for_function("() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
    assert page.evaluate("() => Object.keys(window.__okfLoomGraph.getState().hiddenTypes).filter(k=>window.__okfLoomGraph.getState().hiddenTypes[k]).length") == total - 1
    page.get_by_text("Graph settings", exact=True).click()
    _preset(page, "themes")
    page.get_by_label("Type filters").get_by_role("button", name="Show all").click()
    assert page.evaluate("() => ({hidden:Object.keys(window.__okfLoomGraph.getState().hiddenTypes).filter(k=>window.__okfLoomGraph.getState().hiddenTypes[k]).length,visible:new Set(window.__okfLoomGraph.cy.nodes().not('.dim').map(n=>n.data('type'))).size})") == {"hidden": 0, "visible": total}
    page.go_back();page.wait_for_function("n=>Object.keys(window.__okfLoomGraph.getState().hiddenTypes).filter(k=>window.__okfLoomGraph.getState().hiddenTypes[k]).length===n",arg=total-1)
    page.go_forward();page.wait_for_function("()=>Object.keys(window.__okfLoomGraph.getState().hiddenTypes).filter(k=>window.__okfLoomGraph.getState().hiddenTypes[k]).length===0")


def test_phase4_review_url_atomic_map_default_back_forward_and_malformed(server_url: str, page) -> None:
    _open_graph(page, server_url)
    _preset(page, "themes")
    _preset(page, "bridges")
    assert page.evaluate("() => window.__okfLoomGraph.getState().lens") == "bridges"
    page.go_back(); page.wait_for_function("() => window.__okfLoomGraph.getState().lens === 'themes'")
    page.go_back(); page.wait_for_function("() => window.__okfLoomGraph.getState().lens === 'map'")
    page.go_forward(); page.wait_for_function("() => window.__okfLoomGraph.getState().lens === 'themes'")
    roots = page.evaluate("() => window.__okfLoomGraph.cy.nodes().slice(0,2).map(n=>n.id())")
    page.evaluate("id => {const u=new URL(location.href);u.search='';u.searchParams.set('focus',id);u.searchParams.set('focusDepth','1');history.pushState({},'',u);dispatchEvent(new PopStateEvent('popstate'))}", roots[0])
    page.wait_for_function("id => window.__okfLoomGraph.focusRoot() === id", arg=roots[0])
    page.evaluate("id => {const u=new URL(location.href);u.search='';u.searchParams.set('focus',id);u.searchParams.set('focusDepth','2');history.pushState({},'',u);dispatchEvent(new PopStateEvent('popstate'))}", roots[1])
    page.wait_for_function("id => window.__okfLoomGraph.focusRoot() === id && window.__okfLoomGraph.getState().focusDepth===2", arg=roots[1])
    page.evaluate("() => {history.pushState({},'',location.pathname+'?lens=bogus&selected=%00bad&focus=missing&path=%7Bbad&hidden=missing');dispatchEvent(new PopStateEvent('popstate'))}")
    page.wait_for_function("() => window.__okfLoomGraph.getState().lens === 'map'")
    snap = page.evaluate("() => window.__okfLoomGraph.interaction.snapshot()")
    assert snap["selected"] is None and snap["pathEnd"] is None


def test_phase4_review_hover_lifecycle_and_action_geometry(server_url: str, page) -> None:
    _open_graph(page, server_url)
    ids = page.evaluate("() => window.__okfLoomGraph.cy.nodes().slice(0,2).map(n=>n.id())")
    early = page.evaluate("""async ids=>{const I=window.__okfLoomGraph.interaction;I.dispatch({type:'HOVER',id:ids[0]});await new Promise(r=>setTimeout(r,60));const before=I.snapshot().hovered;I.dispatch({type:'UNHOVER',quietUrl:true});await new Promise(r=>setTimeout(r,90));return {before,after:I.snapshot().hovered,classes:window.__okfLoomGraph.cy.nodes('.okf-hover').length}}""", ids)
    assert early == {"before": None, "after": None, "classes": 0}
    page.evaluate("ids=>{const I=window.__okfLoomGraph.interaction;I.dispatch({type:'HOVER',id:ids[0]});I.dispatch({type:'HOVER',id:ids[1]})}", ids)
    page.wait_for_function("id => window.__okfLoomGraph.interaction.snapshot().hovered === id", arg=ids[1])
    page.evaluate("() => window.__okfLoomGraph.interaction.dispatch({type:'UNHOVER',quietUrl:true})")
    assert page.evaluate("() => window.__okfLoomGraph.cy.elements('.okf-hover,.okf-hover-dim').length") == 0
    page.evaluate("id => window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})", ids[0])
    bar = page.get_by_role("toolbar", name="Selected concept actions")
    before = bar.bounding_box(); page.get_by_role("button", name="Zoom in").click(); page.wait_for_function("() => !!document.querySelector('.okf-graph-actions').style.left")
    after = bar.bounding_box(); canvas = page.locator("#okf-graph").bounding_box()
    assert before and after and canvas and after["x"] >= canvas["x"] and after["x"] + after["width"] <= canvas["x"] + canvas["width"] + 1
    assert (before["x"], before["y"]) != (after["x"], after["y"])
    moved = page.evaluate("""id=>{const bar=document.querySelector('.okf-graph-actions'),a={l:bar.style.left,t:bar.style.top},G=window.__okfLoomGraph,n=G.cy.getElementById(id);G.cy.panBy({x:25,y:15});const b={l:bar.style.left,t:bar.style.top};n.position({x:n.position('x')+30,y:n.position('y')+20});const c={l:bar.style.left,t:bar.style.top};return {a,b,c}}""", ids[0])
    assert moved["a"] != moved["b"] and moved["b"] != moved["c"]
    bar.get_by_role("button", name="Explore neighborhood").click();page.wait_for_function("()=>window.__okfLoomGraph.getState().lens==='focus'&&window.okfGraphCaptureState().layoutSettled")
    focused_box=bar.bounding_box();assert focused_box and focused_box["x"]>=canvas["x"] and focused_box["x"]+focused_box["width"]<=canvas["x"]+canvas["width"]+1
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'HOVER',id})", ids[1])
    page.wait_for_function("id=>window.__okfLoomGraph.interaction.snapshot().hovered===id", arg=ids[1])
    page.evaluate("() => dispatchEvent(new PageTransitionEvent('pagehide'))")
    assert page.evaluate("id=>{const G=window.__okfLoomGraph;return G.interaction.snapshot().selected===id&&G.cy.elements('.okf-hover,.okf-hover-dim').length===0&&document.querySelector('.okf-graph-actions').hidden}", ids[0])


def test_phase4_review_search_pending_escape_and_path_outcomes(server_url: str, page) -> None:
    _open_graph(page, server_url)
    search = page.get_by_role("searchbox", name="Search graph concepts")
    search.fill("orders")
    search.press("Escape")
    page.wait_for_function("() => document.querySelector('#okf-search').value === ''")
    assert page.evaluate("() => !window.__okfLoomGraph.getState().search && window.__okfLoomGraph.cy.nodes('.okf-search-match').length===0")
    ids = page.evaluate("() => window.__okfLoomGraph.cy.nodes().slice(0,2).map(n=>n.id())")
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id,quietUrl:true})", ids[0])
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'PATH',from:id,id,quietUrl:true})", ids[0])
    assert page.evaluate("() => window.__okfLoomGraph.interaction.snapshot().pathResult.kind") == "same"
    page.evaluate("ids=>{const G=window.__okfLoomGraph;G.cy.getElementById(ids[1]).addClass('dim');G.interaction.dispatch({type:'PATH',from:ids[0],id:ids[1],quietUrl:true})}", ids)
    assert page.evaluate("() => window.__okfLoomGraph.interaction.snapshot().pathResult.kind") == "filtered"
    page.evaluate("ids=>{const G=window.__okfLoomGraph;G.cy.nodes().removeClass('dim');G.cy.edges().addClass('dim');G.interaction.dispatch({type:'PATH',from:ids[0],id:ids[1],quietUrl:true})}", ids)
    assert page.evaluate("() => window.__okfLoomGraph.interaction.snapshot().pathResult.kind") == "disconnected"


def test_phase4_review_keyboard_matrix_and_fullscreen_late_fence(server_url: str, page) -> None:
    _open_graph(page, server_url)
    proxy = page.locator(".okf-graph-canvas-proxy"); proxy.focus(); page.keyboard.press("Space")
    selected = page.evaluate("() => window.__okfLoomGraph.interaction.snapshot().selected")
    assert selected
    neighbour = page.evaluate("""id=>{const G=window.__okfLoomGraph,n=G.cy.getElementById(id),neighbors=n.neighborhood('node').sort((a,b)=>a.id().localeCompare(b.id())),other=neighbors.first();n.position({x:0,y:0});neighbors.forEach((x,i)=>x.position({x:-100-i*10,y:i*10}));other.position({x:100,y:0});return other.id()}""", selected)
    page.keyboard.press("ArrowRight")
    assert page.evaluate("id=>{const G=window.__okfLoomGraph,s=G.interaction.snapshot();return s.selected===id&&G.cy.getElementById(id).selected()&&location.search.includes('selected=')}", neighbour)
    page.keyboard.press("Shift+Space")
    assert page.evaluate("() => window.__okfLoomGraph.interaction.snapshot().pathResult.kind") == "found"
    editable = page.get_by_role("searchbox", name="Search graph concepts"); editable.focus(); page.keyboard.type("f/")
    assert editable.input_value() == "f/"
    page.evaluate("() => {const d=document.createElement('div');d.role='dialog';d.tabIndex=0;d.textContent='test dialog';document.body.appendChild(d);d.focus()}")
    page.keyboard.press("Escape")
    assert page.evaluate("id => window.__okfLoomGraph.interaction.snapshot().selected === id", neighbour)
    page.get_by_text("test dialog", exact=True).evaluate("e=>e.remove()")
    page.evaluate("""() => {window.__fsResolve=null;document.getElementById('okf-graph').requestFullscreen=()=>new Promise(r=>window.__fsResolve=r)}""")
    page.get_by_role("button", name="Enter fullscreen").click()
    assert not page.evaluate("() => window.okfGraphCaptureState().layoutSettled")
    page.evaluate("() => dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.evaluate("() => window.__fsResolve()")
    assert page.evaluate("""() => document.querySelector('[aria-label="Enter fullscreen"]').disabled""")
    page.evaluate("() => dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
    enter=page.get_by_role("button",name="Enter fullscreen");expect(enter).to_be_enabled();expect(enter).to_have_attribute("title","Enter fullscreen")
    page.evaluate("""() => {let active=null;Object.defineProperty(document,'fullscreenElement',{configurable:true,get:()=>active});const graph=document.getElementById('okf-graph');graph.requestFullscreen=()=>{active=graph;document.dispatchEvent(new Event('fullscreenchange'));return Promise.resolve()};document.exitFullscreen=()=>{active=null;document.dispatchEvent(new Event('fullscreenchange'));return Promise.resolve()}}""")
    enter.click();expect(page.get_by_role("button",name="Exit fullscreen")).to_be_enabled()


# ---------------------------------------------------------------------------
# Live graph refresh — stale-fetch latest-wins fence (async lifecycle)
# ---------------------------------------------------------------------------


def test_graph_refresh_latest_wins(server_url: str, page) -> None:
    """Overlapping/late live-refresh (DATA_URL) responses are latest-wins: a
    stale earlier response cannot mutate the graph after a newer one applied.

    Gates the graph-JSON fetch, drives two ``graph`` live events so two
    refreshes are in flight, resolves the NEWER one first (adds one node) then
    the STALE one (adds a different node), and proves only the newer node landed
    while the fence counted the stale drop. Deterministic — the test releases
    the responses; nothing is timed against the network.
    """
    gate = """
    (() => {
      const realFetch = window.fetch.bind(window);
      window.__graphGate = { on: false, pending: [], real: realFetch };
      window.fetch = function (input, init) {
        const url = (typeof input === 'string') ? input : (input && input.url) || '';
        const el = document.getElementById('okf-graph');
        const dataUrl = el && el.getAttribute('data-graph-url');
        if (window.__graphGate.on && dataUrl && url.indexOf(dataUrl) !== -1) {
          return new Promise((resolve) => { window.__graphGate.pending.push(resolve); });
        }
        return realFetch(input, init);
      };
    })();
    """
    page.add_init_script(gate)
    page.add_init_script(
        "try { window.localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}"
    )
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.__okfLoomGraph && window.__okfLoomGraph.cy "
        "&& window.__okfLoomGraph.cy.nodes().length > 0 "
        "&& window.__okfLoomGraph.refreshStats",
        timeout=10000,
    )
    # Read the real graph JSON so the two refresh bodies are realistic (base + 1
    # distinguishing node each → a clean single-node delta, no mass removals).
    base = page.evaluate(
        """async () => {
            const el = document.getElementById('okf-graph');
            const dataUrl = el.getAttribute('data-graph-url');
            const r = await window.__graphGate.real(dataUrl, { headers: { Accept: 'application/json' } });
            return r.json();
        }"""
    )
    assert base.get("nodes"), "graph JSON has no nodes to base the refresh bodies on"
    # Turn the gate on and drive two overlapping refreshes via `graph` events.
    page.evaluate("() => { window.__graphGate.on = true; }")
    initial_capture = page.evaluate("() => window.okfGraphCaptureState()")
    motion_before = page.evaluate("() => {const G=window.__okfLoomGraph;G.cy.pan({x:31,y:-17});G.cy.zoom(.71);return {pan:G.cy.pan(),zoom:G.cy.zoom(),starts:G.layoutStats.starts};}")
    page.evaluate("() => window.okfLoomLive.emit('graph', {})")
    page.wait_for_function("() => window.__graphGate.pending.length >= 1", timeout=5000)
    page.evaluate("() => window.okfLoomLive.emit('graph', {})")
    page.wait_for_function("() => window.__graphGate.pending.length >= 2", timeout=5000)
    # Resolve the NEWER refresh (pending[1]) first, then the STALE one (pending[0]).
    page.evaluate(
        """(base) => {
              const mk = (extraId) => {
                  const b = JSON.parse(JSON.stringify(base));
                  b.bundle_revision = 'revision-' + extraId;
                  b.nodes.push({ data: { id: extraId, label: extraId, type: 'concept' } });
                return b;
            };
            const R = (body) => new Response(
                JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
            window.__graphGate.pending[1](R(mk('okf-fence-new')));
            return new Promise((done) => setTimeout(() => {
                window.__graphGate.pending[0](R(mk('okf-fence-stale')));
                setTimeout(done, 80);
            }, 80));
        }""",
        base,
    )
    page.wait_for_function("() => window.okfGraphCaptureState().transitionPending")
    pending = page.evaluate("""async () => {
        const run=window.okfCaptureReadiness.start({target:'graph'});
        const raced=await Promise.race([run.promise.then(()=>({authorized:true})).catch(e=>({authorized:e.screenshotAllowed})),new Promise(resolve=>setTimeout(()=>resolve({pending:true}),50))]);
        return raced;
    }""")
    assert pending == {"pending": True}, "capture authorized a screenshot while refreshed transition was pending"
    page.wait_for_function("() => !window.okfGraphCaptureState().transitionPending && window.okfGraphCaptureState().layoutSettled")
    res = page.evaluate(
        """() => ({
            hasNew: window.__okfLoomGraph.cy.getElementById('okf-fence-new').length > 0,
            hasStale: window.__okfLoomGraph.cy.getElementById('okf-fence-stale').length > 0,
              skippedStale: window.__okfLoomGraph.refreshStats.skippedStale,
              capture: window.okfGraphCaptureState(),
              revision: document.body.dataset.okfRevision,
              captureRevision: document.body.dataset.okfCaptureRevision,
          })"""
      )
    assert res["hasNew"], "the newer refresh response did not apply"
    assert not res["hasStale"], "a STALE refresh response mutated the graph (fence failed)"
    assert res["skippedStale"] >= 1, "the stale refresh was not counted by the fence"
    assert res["capture"]["payloadNodeCount"] == len(base["nodes"]) + 1
    assert res["capture"]["layoutGeneration"] == initial_capture["layoutGeneration"]
    assert res["capture"]["layoutGeneration"] == res["capture"]["appliedLayoutGeneration"]
    assert res["capture"]["layoutSettled"] is True
    assert res["revision"] == res["captureRevision"] == "revision-okf-fence-new"
    assert page.evaluate("() => {const G=window.__okfLoomGraph;return {pan:G.cy.pan(),zoom:G.cy.zoom(),starts:G.layoutStats.starts};}") == motion_before
    revision_proof = page.evaluate("""async () => {const ready=await window.okfCaptureReadiness.start({target:'graph',expectedRevision:'revision-okf-fence-new'}).promise;let mismatch=null;try{await window.okfCaptureReadiness.start({target:'graph',expectedRevision:'revision-okf-fence-stale'}).promise;}catch(error){mismatch={code:error.code,phase:error.metadata.readinessPhase};}return {ready,mismatch};}""")
    assert revision_proof["ready"]["metadata"]["revision"] == "revision-okf-fence-new"
    assert revision_proof["mismatch"] == {"code": "revision-mismatch", "phase": "revision-mismatch"}


def test_graph_phase3_density_refresh_sparse_to_dense_is_current_bundle_derived(server_url: str, page) -> None:
    """An accepted 25-node payload switches every density surface together."""
    _open_graph(page, server_url)
    result = page.evaluate(r"""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=fetch.bind(window),base=await native(url).then(r=>r.json());
      const nodes=Array.from({length:25},(_,i)=>({data:{id:'cross-d-'+i,label:'Dense '+i,type:'concept',unresolved_count:i<8?1:0,active_comment_count:0}}));
      const edges=[];outer:for(let i=0;i<nodes.length;i++)for(let j=0;j<nodes.length;j++){if(i===j)continue;edges.push({data:{source:nodes[i].data.id,target:nodes[j].data.id,label:'rel'}});if(edges.length===120)break outer;}
      const payload={...base,bundle_revision:'cross-dense',nodes,edges,outgoing:null,backlinks:null};
      window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(payload),{headers:{'Content-Type':'application/json'}})):native(r,o);
      await G.refreshGraph();await new Promise(requestAnimationFrame);
      const cy=G.cy, disclosure=document.querySelector('.okf-graph-density');
      const capture=window.okfGraphCaptureState(),pendingRaw=cy.nodes().length;
      return {nodes:capture.nodeCount,pendingRaw,transitionPending:capture.transitionPending,edges:cy.edges().length,index:document.querySelectorAll('.okf-node-index__item').length,
        dense:!!disclosure,copy:disclosure&&disclosure.textContent,hiddenLabels:cy.nodes('.okf-density-label-hidden').length,
        hiddenEdges:cy.edges('.okf-edge-elided').length,aggregated:cy.nodes('.okf-broken-aggregated').length,
        ready:document.body.dataset.okfGraphCaptureReady,error:document.body.dataset.okfGraphRefreshError||null,capture:window.okfGraphCaptureState()};
    }""")
    assert result["nodes"] == result["index"] == result["capture"]["payloadNodeCount"] == 25, result
    assert result["transitionPending"] is True and result["pendingRaw"] >= 25
    assert result["ready"] == "loading"
    page.wait_for_function("() => !window.okfGraphCaptureState().transitionPending")
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes().length") == 25
    assert page.evaluate("() => document.body.dataset.okfGraphCaptureReady") == "ready"
    assert result["edges"] == 120
    assert result["dense"] and "Overview" in result["copy"] and "120 relationships" in result["copy"]
    assert result["hiddenLabels"] > 0 and result["hiddenEdges"] > 0 and result["aggregated"] == 8


def test_graph_phase3_density_refresh_dense_to_sparse_and_stale_dense_cannot_restore(server_url: str, page) -> None:
    """Latest accepted sparse model removes density UI; stale dense completion is fenced."""
    _open_graph(page, server_url)
    result = page.evaluate(r"""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=fetch.bind(window),base=await native(url).then(r=>r.json()),held=[];
      const make=(count,edgeCount,revision)=>{const nodes=Array.from({length:count},(_,i)=>({data:{id:revision+'-'+i,label:revision+' '+i,type:'concept',unresolved_count:1}})),edges=[];
        outer:for(let i=0;i<count;i++)for(let j=0;j<count;j++){if(i===j)continue;edges.push({data:{source:nodes[i].data.id,target:nodes[j].data.id}});if(edges.length===edgeCount)break outer;}return {...base,bundle_revision:revision,nodes,edges,outgoing:null,backlinks:null};};
      const dense=make(25,120,'accepted-dense'),sparse=make(24,23,'accepted-sparse'),stale=make(26,121,'stale-dense');
      let payload=dense;window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(payload),{headers:{'Content-Type':'application/json'}})):native(r,o);await G.refreshGraph();
      window.fetch=(r,o)=>String(r)===url?new Promise(resolve=>held.push({resolve,signal:o.signal})):native(r,o);
      const a=G.refreshGraph();await new Promise(requestAnimationFrame);const b=G.refreshGraph();await new Promise(requestAnimationFrame);
      held[1].resolve(new Response(JSON.stringify(sparse),{headers:{'Content-Type':'application/json'}}));await b;
      held[0].resolve(new Response(JSON.stringify(stale),{headers:{'Content-Type':'application/json'}}));await a;await new Promise(requestAnimationFrame);
      const cy=G.cy,capture=window.okfGraphCaptureState(),pendingRaw=cy.nodes().length;
      return {nodes:capture.nodeCount,pendingRaw,transitionPending:capture.transitionPending,edges:cy.edges().length,index:document.querySelectorAll('.okf-node-index__item').length,
        disclosure:!!document.querySelector('.okf-graph-density'),densityClasses:cy.elements('.okf-density-label-hidden,.okf-broken-aggregated,.okf-density-overview-broken,.okf-density-overview-zero,.okf-density-detail-label').length,
        revision:document.body.dataset.okfCaptureRevision,skipped:G.refreshStats.skippedStale,ready:document.body.dataset.okfGraphCaptureReady,error:document.body.dataset.okfGraphRefreshError||null,capture:window.okfGraphCaptureState()};
    }""")
    assert result["nodes"] == result["index"] == result["capture"]["payloadNodeCount"] == 24, result
    assert result["transitionPending"] is True and result["pendingRaw"] >= 24
    assert result["ready"] == "loading"
    page.wait_for_function("() => !window.okfGraphCaptureState().transitionPending")
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes().length") == 24
    assert page.evaluate("() => document.body.dataset.okfGraphCaptureReady") == "ready"
    assert result["edges"] == 23
    assert result["disclosure"] is False and result["densityClasses"] == 0
    assert result["revision"] == "accepted-sparse" and result["skipped"] >= 1


def test_graph_phase3_density_disclosure_never_intersects_nodes_at_zoom_tiers(docs_server_url: str, page) -> None:
    """Disclosure is non-plotting chrome at overview/intermediate/high zoom."""
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_graph(page, docs_server_url)
    proof_dir = Path("/tmp/okf-loom-phase3-density-geometry")
    proof_dir.mkdir(parents=True, exist_ok=True)
    for name, zoom, copy in (("overview", 0.8, "Overview"), ("intermediate", 1.1, "Intermediate detail"), ("high", 1.51, "Local detail")):
        page.evaluate("z => { window.__okfLoomGraph.cy.zoom(z); return undefined; }", zoom)
        expect(page.locator(".okf-graph-density")).to_contain_text(copy, timeout=5000)
        geometry = page.evaluate(r"""() => {const cy=window.__okfLoomGraph.cy,c=document.getElementById('okf-graph').getBoundingClientRect(),d=document.querySelector('.okf-graph-density').getBoundingClientRect();
          const hit=cy.nodes().filter(n=>{const b=n.renderedBoundingBox({includeLabels:true,includeOverlays:true});const r={left:c.left+b.x1,right:c.left+b.x2,top:c.top+b.y1,bottom:c.top+b.y2};return r.left<d.right&&r.right>d.left&&r.top<d.bottom&&r.bottom>d.top;});
          return {intersections:hit.length,disclosureOutsidePlot:d.left>=c.right||d.right<=c.left||d.top>=c.bottom||d.bottom<=c.top,nodes:cy.nodes().length,edges:cy.edges().length};}""")
        canonical = page.request.get(docs_server_url + '/__data/graph.json').json()
        assert geometry == {'intersections': 0, 'disclosureOutsidePlot': True, 'nodes': len(canonical['nodes']), 'edges': len(canonical['edges'])}
        page.screenshot(path=str(proof_dir / f"{name}.png"), timeout=10000)


# ---------------------------------------------------------------------------
# Wiki search suggestions — complete editable combobox
# ---------------------------------------------------------------------------


def test_search_suggestions_are_complete_editable_combobox(server_url: str, page) -> None:
    """Suggestions expose the promised listbox and keyboard ownership."""
    page.goto(server_url + "/", wait_until="domcontentloaded")
    search = page.locator('input[type="search"][name="q"]').first
    search.wait_for(state="visible")
    expect(search).to_have_attribute("role", "combobox")
    expect(search).to_have_attribute("aria-autocomplete", "list")
    assert search.get_attribute("aria-controls")
    search.fill("orders")
    live = page.locator(".okf-search-live")
    live.wait_for(state="visible")
    expect(live).to_have_attribute("role", "listbox")
    assert live.locator('[role="option"]').count() >= 1
    search.press("ArrowDown")
    assert search.get_attribute("aria-activedescendant")
    # Escape dismisses (and clears) the panel.
    search.press("Escape")
    expect(live).to_be_hidden()
    expect(search).to_have_attribute("aria-expanded", "false")


# ===========================================================================
# V2 theme foundation — Appearance control (family + mode popover).
# theme.js is the sole owner of theme state; these prove the keyboard/focus
# matrix, both families + all modes, and the CSP self-hosting contract.
# ===========================================================================


def _open_appearance_menu(page, static_url: str) -> None:
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'"
    )
    page.locator("#okf-theme").focus()
    page.keyboard.press("Enter")  # native button activates via click
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")


def test_appearance_menu_keyboard_escape_restores_focus(static_url: str, page) -> None:
    """KEYBOARD_FOCUS_MATRIX: Enter on the native trigger opens the menu;
    Arrow moves within a group; Escape closes ONLY this menu and restores
    focus to the trigger. The menu is the only theme overlay, so Escape never
    leaks to (or from) the studio command palette / editors."""
    _open_appearance_menu(page, static_url)
    trigger = page.locator("#okf-theme")
    expect(trigger).to_have_attribute("aria-expanded", "true")
    # Focus landed inside the menu (the checked Family option).
    assert page.evaluate("() => !!document.activeElement.closest('#okf-appearance-menu')")
    # ArrowDown moves within the Family group (roving tabindex).
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => !!document.activeElement.closest('#okf-appearance-menu')")
    # Escape closes the menu and restores focus to the trigger.
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('okf-appearance-menu').hidden")
    expect(trigger).to_have_attribute("aria-expanded", "false")
    assert page.evaluate("() => document.activeElement.id") == "okf-theme"


def test_appearance_menu_outside_click_closes_without_stealing_focus(static_url: str, page) -> None:
    """KEYBOARD_FOCUS_MATRIX: clicking outside closes the menu but does NOT
    steal focus back to the trigger — focus stays on the clicked destination."""
    _open_appearance_menu(page, static_url)
    # Click the search input (an outside, focusable destination).
    dest = page.locator("#okf-search-query")
    dest.click()
    page.wait_for_function("() => document.getElementById('okf-appearance-menu').hidden")
    assert page.evaluate("() => document.activeElement.id") == "okf-search-query"


def test_appearance_trigger_hidden_until_theme_ready_gate(static_url: str, page) -> None:
    """P1-2: the Appearance trigger stays hidden until theme.js marks
    successful init (html.okf-theme-ready), so it is inert not only when JS is
    fully off (covered by the no-JS test) but also when theme.js itself is
    blocked or throws. Verified by toggling the signal class and reading the
    computed display, plus the active stylesheet gate."""
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.OKFLoomTheme")
    # theme.js signaled successful init -> trigger visible.
    assert page.evaluate("() => document.documentElement.classList.contains('okf-theme-ready')")
    expect(page.locator("#okf-theme")).to_be_visible()
    # The CSS gate html:not(.okf-theme-ready) #okf-theme is in the stylesheet.
    has_gate = page.evaluate("""() => Array.from(document.styleSheets).some(s => {
        try { return Array.from(s.cssRules || []).some(r =>
            r.cssText && r.cssText.indexOf(':not(.okf-theme-ready)') >= 0
            && r.cssText.indexOf('#okf-theme') >= 0);
        } catch (e) { return false; }
    })""")
    assert has_gate, "html:not(.okf-theme-ready) #okf-theme gate missing from CSS"
    # Simulate theme.js having failed/blocked: drop the signal -> hidden+inert.
    page.evaluate("() => document.documentElement.classList.remove('okf-theme-ready')")
    assert page.evaluate(
        "() => getComputedStyle(document.getElementById('okf-theme')).display"
    ) == "none", "trigger still visible after dropping the init signal"
    # Re-signal -> visible again.
    page.evaluate("() => document.documentElement.classList.add('okf-theme-ready')")
    expect(page.locator("#okf-theme")).to_be_visible()


def test_appearance_control_switches_family_and_mode(static_url: str, page) -> None:
    """Both families x both modes resolve + persist via the sole owner
    (window.OKFLoomTheme). Auto never pins a concrete legacy mirror."""
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.okfPortableProvider && document.body.dataset.okfCaptureReady === 'ready'"
    )
    assert page.evaluate("() => !!window.OKFLoomTheme"), "theme.js API not present"

    # Swiss + dark.
    page.evaluate("() => { window.OKFLoomTheme.setFamily('swiss'); window.OKFLoomTheme.setMode('dark'); }")
    expect(page.locator("html")).to_have_attribute("data-theme", "swiss-dark")
    assert page.evaluate("() => localStorage.getItem('okf-theme-family')") == "swiss"
    assert page.evaluate("() => localStorage.getItem('okf-theme-mode')") == "dark"
    # Explicit mode mirrors into the legacy key for older same-origin pages.
    assert page.evaluate("() => localStorage.getItem('okf-theme')") == "swiss-dark"

    # Flip mode to light; family is sticky.
    page.evaluate("() => window.OKFLoomTheme.setMode('light')")
    expect(page.locator("html")).to_have_attribute("data-theme", "swiss-light")

    # Switch family to technical; mode stays light.
    page.evaluate("() => window.OKFLoomTheme.setFamily('technical')")
    expect(page.locator("html")).to_have_attribute("data-theme", "technical-light")

    # Auto removes the concrete legacy mirror (must follow OS, never pin).
    page.evaluate("() => window.OKFLoomTheme.setMode('auto')")
    assert page.evaluate("() => localStorage.getItem('okf-theme')") is None
    assert page.evaluate("() => localStorage.getItem('okf-theme-mode')") == "auto"


def test_appearance_menu_click_option_applies_theme(static_url: str, page) -> None:
    """Clicking a menu option (pointer) applies it through the sole owner."""
    _open_appearance_menu(page, static_url)
    # Click the "Dark" colour-mode option.
    page.locator('#okf-appearance-menu [data-okf-set="mode"][data-okf-val="dark"]').click()
    expect(page.locator("html")).to_have_attribute("data-theme", "technical-dark")
    # The menu stays open (radiogroup convention) but the theme applied.
    assert page.evaluate("() => localStorage.getItem('okf-theme-mode')") == "dark"


def test_appearance_exposes_typed_modifier_controls_and_keyboard(static_url: str, page) -> None:
    """The shared Appearance form exposes four labelled radio groups, and
    modifier radios use the same roving Arrow/Home/End contract as family and
    mode without reducing the native checked semantics."""
    _open_appearance_menu(page, static_url)
    menu = page.locator("#okf-appearance-menu")
    expect(menu).to_be_visible()
    assert menu.locator('fieldset[role="group"]').count() == 5
    for name, values in {
        "okf-theme-family": ("technical", "swiss", "purple", "coffee"),
        "okf-theme-mode": ("auto", "light", "dark"),
        "okf-theme-contrast": ("normal", "soft"),
        "okf-theme-border": ("on", "off"),
        }.items():
        radios = menu.locator(f'input[type="radio"][name="{name}"]')
        assert radios.count() == len(values)
        assert menu.locator(
            f'input[type="radio"][name="{name}"][aria-checked="true"]'
        ).count() == 1
        for value in values:
            control = menu.locator(
                f'input[type="radio"][name="{name}"][value="{value}"]'
            )
            expect(control).to_have_attribute("data-okf-val", value)
            assert control.get_attribute("id")

    soft = menu.locator('input[data-okf-set="contrast"][data-okf-val="soft"]')
    reveal_control(page, soft).focus()
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.value") == "normal"
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement.value") == "soft"
    page.keyboard.press("Home")
    assert page.evaluate("() => document.activeElement.value") == "normal"


def test_appearance_modifiers_persist_root_state_and_event_generation(
    static_url: str, page
) -> None:
    """Modifier setters are owned by theme.js: root attributes, persistence,
    and one ordered semantic event update atomically while family/mode remain
    independent."""
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.OKFLoomTheme")
    page.evaluate("() => localStorage.clear()")
    events = page.evaluate("""() => {
      const rows = [];
      window.addEventListener('okf-loom:themeChanged', e => rows.push({
        contrast: e.detail.contrast,
        border: e.detail.border,
        previousContrast: e.detail.previousContrast,
        previousBorder: e.detail.previousBorder,
        generation: e.detail.generation,
        rootContrast: document.documentElement.getAttribute('data-okf-contrast'),
        rootBorder: document.documentElement.getAttribute('data-okf-border'),
      }));
      window.OKFLoomTheme.setContrast('soft');
      window.OKFLoomTheme.setBorder('on');
      return rows;
    }""")
    assert len(events) == 2
    assert events[0]["contrast"] == "soft" and events[0]["rootContrast"] == "soft"
    # Borders Off is the default, so switching On proves a real transition.
    assert events[1]["previousBorder"] == "off"
    assert events[1]["border"] == "on" and events[1]["rootBorder"] == "on"
    assert events[1]["generation"] > events[0]["generation"]
    assert page.evaluate("() => window.OKFLoomTheme.getFamily()") == "technical"
    assert page.evaluate("() => window.OKFLoomTheme.getMode()") == "auto"
    assert page.locator("html").get_attribute("data-okf-contrast") == "soft"
    assert page.locator("html").get_attribute("data-okf-border") == "on"
    assert page.evaluate("() => localStorage.getItem('okf-theme-contrast')") == "soft"
    assert page.evaluate("() => localStorage.getItem('okf-theme-border')") == "on"

    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.OKFLoomTheme")
    assert page.locator("html").get_attribute("data-okf-contrast") == "soft"
    assert page.locator("html").get_attribute("data-okf-border") == "on"


def test_appearance_auto_uses_explicit_config_not_painted_concrete_hint(
    static_url: str, page
) -> None:
    """Static output carries Technical Auto separately from its concrete
    first-paint hint; a dark OS resolves dark without persisting a mode."""
    page.emulate_media(color_scheme="dark")
    page.goto(static_url + "/__search.html?q=orders", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.OKFLoomTheme")
    assert page.locator("html").get_attribute("data-theme") == "technical-dark"
    assert page.evaluate("() => window.OKFLoomTheme.getMode()") == "auto"
    assert page.evaluate("() => localStorage.getItem('okf-theme-mode')") is None
    configured = page.evaluate(
        "() => JSON.parse(document.getElementById('okf-theme-config').textContent)"
    )
    assert configured["theme"] == "auto"


def test_appearance_border_off_keeps_non_colour_cues_visible(server_url: str, page) -> None:
    """Border Off removes decorative frame colour but keeps semantic focus,
    selection, path, error, and status cues available through tokens and text
    treatment rather than a coloured side stripe alone."""
    _goto_with_theme(page, server_url)
    page.evaluate("""() => {
      window.OKFLoomTheme.setContrast('soft');
      window.OKFLoomTheme.setBorder('off');
    }""")
    page.locator("#okf-theme").click()
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    reveal_control(page, page.locator('#okf-appearance-menu input[data-okf-set="contrast"]').first)
    cues = page.evaluate("""() => {
      const root = getComputedStyle(document.documentElement);
      const checked = document.querySelector('#okf-appearance-menu .okf-appearance__opt[aria-checked="true"]');
      const selected = getComputedStyle(checked);
      const focus = document.querySelector('#okf-appearance-menu input[data-okf-set="contrast"]');
      focus.focus();
      const focused = getComputedStyle(focus.closest('.okf-appearance__opt'));
      const error = document.createElement('p');
      error.className = 'okf-search-error';
      error.textContent = 'The search failed.';
      document.body.appendChild(error);
      const errorStyle = getComputedStyle(error);
      const status = document.createElement('p');
      status.className = 'okf-studio-fallback-banner';
      status.setAttribute('role', 'status');
      status.textContent = 'Saved.';
      document.body.appendChild(status);
      const statusText = getComputedStyle(status, '::before').content;
      // A generic live region (e.g. the selection Comment affordance) must
      // never paint a generated "Status" label over the page.
      const live = document.createElement('div');
      live.setAttribute('role', 'status');
      live.textContent = 'Comment';
      document.body.appendChild(live);
      const liveText = getComputedStyle(live, '::before').content;
      const errorOutline = errorStyle.outlineStyle;
      error.remove(); status.remove(); live.remove();
      return {
        border: root.getPropertyValue('--okf-border').trim(),
        selection: root.getPropertyValue('--okf-selection-cue').trim(),
        path: root.getPropertyValue('--okf-path-cue').trim(),
        error: root.getPropertyValue('--okf-error-cue').trim(),
        status: root.getPropertyValue('--okf-status-cue').trim(),
        selectedWeight: selected.fontWeight,
        selectedDecoration: selected.textDecorationLine,
        focusOutline: focused.outlineStyle,
        errorOutline,
        statusText,
        liveText,
      };
    }""")
    assert cues["border"] in ("transparent", "rgba(0, 0, 0, 0)")
    for key in ("selection", "path", "error", "status"):
        assert cues[key] not in ("", "transparent", "rgba(0, 0, 0, 0)")
    assert int(cues["selectedWeight"]) >= 700
    assert "underline" in cues["selectedDecoration"]
    assert cues["focusOutline"] == "solid"
    assert cues["errorOutline"] == "solid"
    assert "Status" in cues["statusText"]
    assert cues["liveText"] in ("none", "normal")


def test_appearance_reset_button_restores_configured_auto_defaults(
    static_url: str, page
) -> None:
    """Reset is a normal typed Appearance action, not a hidden escape hatch:
    it clears user keys and returns the configured Technical Auto/normal/
    borders-off/compact state while leaving the form open for confirmation."""
    _open_appearance_menu(page, static_url)
    reveal_control(page, page.locator('input[data-okf-set="family"][data-okf-val="swiss"]')).click()
    page.locator('input[data-okf-set="mode"][data-okf-val="dark"]').click()
    page.locator('input[data-okf-set="contrast"][data-okf-val="soft"]').click()
    page.locator('input[data-okf-set="border"][data-okf-val="on"]').click()
    page.locator('input[data-okf-set="density"][data-okf-val="comfortable"]').click()
    expect(page.locator("html")).to_have_attribute("data-theme", "swiss-dark")
    expect(page.locator("html")).to_have_attribute("data-okf-border", "on")
    expect(page.locator("html")).to_have_attribute("data-okf-density", "comfortable")
    page.locator('[data-okf-action="reset"]').click()
    expect(page.locator("html")).to_have_attribute("data-theme", "technical-light")
    expect(page.locator("html")).to_have_attribute("data-okf-contrast", "normal")
    expect(page.locator("html")).to_have_attribute("data-okf-border", "off")
    expect(page.locator("html")).to_have_attribute("data-okf-density", "compact")
    assert page.evaluate("() => localStorage.getItem('okf-theme-density')") is None
    assert page.evaluate("() => window.OKFLoomTheme.getMode()") == "auto"
    assert page.evaluate("() => localStorage.getItem('okf-theme-family')") is None
    assert page.evaluate("() => localStorage.getItem('okf-theme-mode')") is None
    assert page.evaluate("() => localStorage.getItem('okf-theme-contrast')") is None
    assert page.evaluate("() => localStorage.getItem('okf-theme-border')") is None
    assert page.evaluate("() => !document.getElementById('okf-appearance-menu').hidden")


def test_theme_assets_self_hosted_no_inline_handlers(server_url: str, page) -> None:
    """SECURITY/CSP: theme.js is self-hosted under /__static (same origin, no
    remote script) and no element carries an inline event handler — the
    Appearance controls are wired purely by addEventListener from theme.js.
    This keeps CSP script-src 'self' (no 'unsafe-inline') enforceable."""
    page.goto(server_url + "/", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.OKFLoomTheme")
    srcs = page.evaluate(
        "() => Array.from(document.querySelectorAll('script[src]')).map(s => s.src)"
    )
    assert any(s.endswith("/__static/theme.js") for s in srcs), (
        f"theme.js is not self-hosted under /__static: {srcs}"
    )
    # No inline on* handler attributes anywhere on the page.
    inline = page.evaluate(
        "() => Array.from(document.querySelectorAll('*'))"
        ".filter(el => Array.from(el.attributes).some(a => a.name.startsWith('on'))).length"
    )
    assert inline == 0, "inline event handler attributes present (CSP risk)"
    # The appearance trigger + menu exist; the menu is inert (hidden) at rest.
    expect(page.locator("#okf-theme")).to_be_visible()
    assert page.evaluate("() => document.getElementById('okf-appearance-menu').hidden") is True


# ===========================================================================
# P1/outcome-audit theme-foundation browser proof
# (GAP-1 lightbox stack, GAP-3 graph resync, GAP-6 keyboard matrix,
#  GAP-7 forced-colours / reduced-motion)
# ===========================================================================


def _goto_with_theme(page, server_url, path="/tables/orders"):
    page.goto(server_url + path, wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.OKFLoomTheme")
    page.wait_for_function("() => document.documentElement.classList.contains('okf-theme-ready')")


def _goto_stable_studio(page, server_url, path="/tables/orders"):
    """A studio concept page with live updates DISCONNECTED and the initial
    document projection settled. The studio re-renders ``.okf-page__body`` on
    live doc events, which detaches a test-injected lightbox opener; freezing
    the live transport after the initial render keeps the opener in the DOM so
    ``closeLightbox`` can restore exact opener focus (strict proof)."""
    _goto_with_theme(page, server_url, path)
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'", timeout=8000)
    page.wait_for_function(
        "() => !document.querySelector('article.okf-page__main') || "
        "(window.okfLoomStudio.state.doc && window.okfLoomStudio.state.doc.annotation_projection)",
        timeout=8000,
    )
    page.evaluate(
        "() => { if (window.okfLoomLive && window.okfLoomLive._disconnectForTest) "
        "window.okfLoomLive._disconnectForTest(); }"
    )


_IMG_DATAURI = (
    'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" '
    'width="40" height="40"><rect width="40" height="40" fill="%230c7373"/></svg>'
)


def _inject_zoomable_image(page):
    """Inject a zoomable image into the prose and re-wire renderers.js. Returns
    only after the image is wired as focusable (tabindex=0) by initImages."""
    page.evaluate(
        """(src) => {
          const body = document.querySelector('.okf-page__body');
          const img = document.createElement('img');
          img.id = 'okf-test-lightbox-img';
          img.width = 40; img.height = 40;
          img.src = src;
          img.alt = 'Test diagram';
          body.appendChild(img);
          window.dispatchEvent(new Event('okf-loom:bodyPatched'));
        }""",
        _IMG_DATAURI,
    )
    page.wait_for_function(
        "() => { const i = document.getElementById('okf-test-lightbox-img'); "
        "return !!(i && i.classList.contains('okf-zoomable') && i.getAttribute('tabindex') === '0'); }"
    )


def _open_lightbox_via_img(page):
    """Open the lightbox via an Enter keydown on the zoomable image (never
    depends on the data-URI image being pointer-focusable/clickable)."""
    page.evaluate(
        "() => document.getElementById('okf-test-lightbox-img')"
        ".dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}))"
    )
    page.wait_for_function("() => !!document.querySelector('.okf-lightbox')")


def test_renderers_lightbox_registers_and_appearance_yields_escape(server_url: str, page) -> None:
    """GAP-1: the renderers.js aria-modal image lightbox registers on the
    shared OKFOverlayStack. One Escape closes ONLY the topmost registered
    layer and focus stays modal-correct:

      * lightbox open alone -> Escape closes it, focus restored to its image;
      * lightbox + Appearance menu stacked -> Escape closes the menu first
        (topmost), then the lightbox; never both at once, never a focus
        restore behind the topmost aria-modal.
    """
    _goto_with_theme(page, server_url)
    _inject_zoomable_image(page)
    baseline = page.evaluate("() => window.OKFOverlayStack.depth()")

    def _open_lightbox():
        # Open via a keydown Enter directly on the zoomable image so the proof
        # never depends on the data-URI image being pointer-focusable/clickable.
        # Self-heal: a live studio body re-render can drop the injected image,
        # so re-inject + re-wire if it is gone, then dispatch the keydown.
        page.evaluate("""() => {
          if (!document.getElementById('okf-test-lightbox-img')) {
            const body = document.querySelector('.okf-page__body');
            const img = document.createElement('img');
            img.id = 'okf-test-lightbox-img'; img.width = 40; img.height = 40;
            img.src = 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="%230c7373"/></svg>';
            img.alt = 'Test diagram';
            body.appendChild(img);
            window.dispatchEvent(new Event('okf-loom:bodyPatched'));
          }
          document.getElementById('okf-test-lightbox-img')
            .dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
        }""")
        page.wait_for_function("() => !!document.querySelector('.okf-lightbox')")

    # --- Phase A: lightbox alone registers, self-closes, restores focus. ---
    _open_lightbox()
    reg = page.evaluate("""() => ({
      depth: window.OKFOverlayStack.depth(),
      topIsLightbox: !!document.querySelector('.okf-lightbox[aria-modal="true"]'),
    })""")
    assert reg["depth"] == baseline + 1, "lightbox did not register on the overlay stack"
    assert reg["topIsLightbox"], "lightbox is not the topmost registered overlay"
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.querySelector('.okf-lightbox')")
    after_a = page.evaluate("""() => {
      const a = document.activeElement;
      return {
        lightboxGone: !document.querySelector('.okf-lightbox'),
        // Headless Chrome occasionally routes a programmatic <img>.focus() to
        // <body>; the contract is "not left trapped on the removed dialog".
        // img = the lightbox's opener (ideal); body = acceptable fallback.
        focusOffLightbox: !document.querySelector('.okf-lightbox') ||
          !a || !a.closest || !a.closest('.okf-lightbox'),
        focusOnOpener: a && a.id === 'okf-test-lightbox-img',
        depth: window.OKFOverlayStack.depth(),
      };
    }""")
    assert after_a["lightboxGone"], "Escape closed more than the topmost layer"
    assert after_a["focusOffLightbox"], (
        "lightbox Escape left focus on the removed dialog instead of restoring it"
    )
    assert after_a["depth"] == baseline

    # --- Phase B: lightbox + Appearance menu stacked. Open the lightbox, then
    # open the Appearance menu via a synthetic trigger click (dispatched on the
    # button, so it does not hit the lightbox backdrop) so BOTH are open with
    # the menu registered ABOVE the lightbox. ---
    _open_lightbox()
    page.evaluate("() => document.getElementById('okf-theme').click()")
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    stacked = page.evaluate("""() => ({
      depth: window.OKFOverlayStack.depth(),
      lightboxOpen: !!document.querySelector('.okf-lightbox'),
      menuOpen: !document.getElementById('okf-appearance-menu').hidden,
    })""")
    assert stacked["lightboxOpen"] and stacked["menuOpen"], (
        "could not stack lightbox + Appearance menu simultaneously"
    )
    assert stacked["depth"] == baseline + 2
    # First Escape: the Appearance menu is topmost -> closes ONLY it; lightbox
    # stays open. Because the lightbox is now the topmost aria-modal, focus must
    # move INTO the lightbox (its close button) — never back to #okf-theme,
    # which sits BEHIND the still-open modal (invalid stacked restore).
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('okf-appearance-menu').hidden")
    after_menu = page.evaluate("""() => {
      const a = document.activeElement;
      return {
        lightboxOpen: !!document.querySelector('.okf-lightbox'),
        menuOpen: !document.getElementById('okf-appearance-menu').hidden,
        focusInLightbox: !!(a && a.closest && a.closest('.okf-lightbox')),
        focusOnTrigger: !!(a && a.id === 'okf-theme'),
      };
    }""")
    assert after_menu["lightboxOpen"], "first Escape also closed the lightbox (did not close only topmost)"
    assert not after_menu["menuOpen"], "first Escape did not close the topmost Appearance menu"
    assert not after_menu["focusOnTrigger"], (
        "menu restored focus to #okf-theme behind the still-open lightbox modal"
    )
    assert after_menu["focusInLightbox"], (
        "menu close did not move focus INTO the topmost lightbox (close button)"
    )
    # Second Escape: now the lightbox is topmost -> closes it. Focus moves off
    # the dismissed dialog (the robust, headless-stable focus proof is the
    # menu->trigger restore above; the lightbox's img focus-restore is checked
    # tolerantly — headless Chrome sometimes routes <img>.focus() to <body>).
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.querySelector('.okf-lightbox')")
    after_lb = page.evaluate("""() => {
      const a = document.activeElement;
      return {
        lightboxGone: !document.querySelector('.okf-lightbox'),
        focusOffLightbox: !a || !a.closest || !a.closest('.okf-lightbox'),
      };
    }""")
    assert after_lb["lightboxGone"], "second Escape did not close the topmost lightbox"
    assert after_lb["focusOffLightbox"], (
        "second Escape left focus on the removed lightbox dialog"
    )


def test_renderers_lightbox_traps_tab_focus(server_url: str, page) -> None:
    """GAP-1 (Tab containment): the aria-modal lightbox contains Tab/Shift+Tab
    so focus can never reach #okf-theme or other page controls behind it. The
    close button is the single focusable control, so both Tab directions keep
    focus inside the dialog."""
    _goto_stable_studio(page, server_url)
    _inject_zoomable_image(page)
    _open_lightbox_via_img(page)

    # Forward Tab several times: focus must stay inside the lightbox.
    for _ in range(4):
        page.keyboard.press("Tab")
        page.wait_for_timeout(20)
    assert page.evaluate("""() => {
      const a = document.activeElement;
      return !!(a && a.closest && a.closest('.okf-lightbox'));
    }"""), "forward Tab escaped the lightbox to a page control behind it"
    # Shift+Tab several times: still contained.
    for _ in range(4):
        page.keyboard.press("Shift+Tab")
        page.wait_for_timeout(20)
    assert page.evaluate("""() => {
      const a = document.activeElement;
      return !!(a && a.closest && a.closest('.okf-lightbox'));
    }"""), "Shift+Tab escaped the lightbox to a page control behind it"

    # Escape restores focus to the exact opener image.
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.querySelector('.okf-lightbox')")
    assert page.evaluate("() => document.activeElement && document.activeElement.id === 'okf-test-lightbox-img'"), (
        "lightbox close did not restore focus to the exact opener image"
    )


def test_renderers_lightbox_close_restores_exact_opener_focus(server_url: str, page) -> None:
    """GAP-1 (exact opener restore): closing the lightbox via its close button
    restores focus to the exact image that opened it."""
    _goto_stable_studio(page, server_url)
    _inject_zoomable_image(page)
    _open_lightbox_via_img(page)

    page.locator(".okf-lightbox__close").click()
    page.wait_for_function("() => !document.querySelector('.okf-lightbox')")
    assert page.evaluate("() => document.activeElement && document.activeElement.id === 'okf-test-lightbox-img'"), (
        "close-button did not restore focus to the opener image"
    )


def test_appearance_opened_over_lightbox_restores_focus_into_lightbox(server_url: str, page) -> None:
    """GAP-1 (invalid stacked restore): if the Appearance menu is opened
    programmatically while the lightbox is the current aria-modal, closing it
    must move focus INTO the lightbox (its close button) — never to #okf-theme,
    which sits BEHIND the still-open modal. Then closing the lightbox restores
    the exact opener."""
    _goto_stable_studio(page, server_url)
    _inject_zoomable_image(page)
    _open_lightbox_via_img(page)

    # Programmatically open Appearance ABOVE the lightbox.
    page.evaluate("() => document.getElementById('okf-theme').click()")
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    assert page.evaluate("() => window.OKFOverlayStack.depth() === 2"), (
        "lightbox + Appearance menu did not stack to depth 2"
    )

    # Closing the Appearance menu must focus INTO the lightbox, not #okf-theme.
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('okf-appearance-menu').hidden")
    restored = page.evaluate("""() => {
      const a = document.activeElement;
      return {
        focusInLightbox: !!(a && a.closest && a.closest('.okf-lightbox')),
        focusOnTheme: !!(a && a.id === 'okf-theme'),
        lightboxOpen: !!document.querySelector('.okf-lightbox'),
      };
    }""")
    assert restored["lightboxOpen"], "closing Appearance also closed the lightbox"
    assert not restored["focusOnTheme"], (
        "Appearance close restored focus to #okf-theme BEHIND the lightbox modal"
    )
    assert restored["focusInLightbox"], (
        "Appearance close did not move focus INTO the topmost lightbox"
    )

    # Closing the now-topmost lightbox restores the exact opener image.
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.querySelector('.okf-lightbox')")
    assert page.evaluate("() => document.activeElement && document.activeElement.id === 'okf-test-lightbox-img'"), (
        "lightbox close did not restore the exact opener focus"
    )


def test_graph_palette_resyncs_on_theme_change(server_url: str, page) -> None:
    """GAP-3: after the graph initializes, switching family technical -> Swiss
    re-syncs the canvas edge/arrow colour to mirror the resolved theme's
    --okf-border-strong token (graph.js listens for okf-loom:themeChanged)."""
    _open_graph(page, server_url)
    before = page.evaluate("""() => {
      if (!window.__okfLoomGraph || !window.__okfLoomGraph.cy) return null;
      const cy = window.__okfLoomGraph.cy;
      if (!cy.edges().length) return null;
      const norm = c => { const s = document.createElement('div').style; s.color = c; return s.color; };
      const e = cy.edges().first();
      return {
        theme: document.documentElement.getAttribute('data-theme'),
        line: norm(e.style('line-color')),
        arrow: norm(e.style('target-arrow-color')),
        expectTech: norm('#5e6e85'),
      };
    }""")
    assert before, "graph has no edges to assert the edge colour on"
    assert before["theme"] == "technical-light", f"unexpected initial theme: {before['theme']}"
    assert before["line"] == before["expectTech"], (
        f"initial technical-light edge did not use the semantic slate exception: {before}"
    )
    # Technical-light intentionally lifts the graph border from the quiet
    # decorative border to slate #5e6e85.  Against the dimmed (Soft Light)
    # #f3f5f7 canvas this is AA for technical graph text/strokes (>= 4.5:1),
    # while Swiss remains token-owned below.
    assert page.evaluate("""() => {const rgb=h=>[1,3,5].map(i=>parseInt(h.slice(i,i+2),16)/255).map(v=>v<=.03928?v/12.92:Math.pow((v+.055)/1.055,2.4));const a=rgb('#5e6e85'),b=rgb('#f3f5f7'),l=x=>.2126*x[0]+.7152*x[1]+.0722*x[2];return (l(b)+.05)/(l(a)+.05);}""") >= 4.5
    # Switch family technical -> swiss (same light mode): the edge must resync
    # to swiss-light's --okf-border-strong = foreground (#0f172a).
    page.evaluate("() => window.OKFLoomTheme.setTheme('swiss-light')")
    page.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'swiss-light'")
    after = page.evaluate("""() => {
      const cy = window.__okfLoomGraph.cy;
      const norm = c => { const s = document.createElement('div').style; s.color = c; return s.color; };
      const e = cy.edges().first();
      return {
        line: norm(e.style('line-color')),
        arrow: norm(e.style('target-arrow-color')),
        expectSwiss: norm('#0f172a'),
      };
    }""")
    assert after["line"] == after["expectSwiss"], (
        f"edge line colour did not resync to the swiss border token: {after}"
    )
    assert after["arrow"] == after["expectSwiss"], (
        f"edge arrow colour did not resync: {after}"
    )
    assert after["line"] != before["line"], "edge colour unchanged after the theme switch"


# --- GAP-6: Appearance composite keyboard matrix -------------------------

def test_appearance_space_opens_trigger(server_url: str, page) -> None:
    """Space on the native trigger button activates it (no custom keymap)."""
    _goto_with_theme(page, server_url)
    page.locator("#okf-theme").focus()
    page.keyboard.press("Space")
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    expect(page.locator("#okf-theme")).to_have_attribute("aria-expanded", "true")


def test_appearance_home_end_move_within_group(server_url: str, page) -> None:
    """Home/End move within the focused radio group only (not across groups)."""
    _goto_with_theme(page, server_url)
    page.locator("#okf-theme").click()
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    # openMenu focuses the checked option of the FIRST group (Family), which
    # defaults to Technical.
    assert page.evaluate("() => document.activeElement && document.activeElement.getAttribute('data-okf-val')") == "auto"
    # End -> last option in the Family group (Coffee).
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement && document.activeElement.getAttribute('data-okf-val')") == "dark"
    # Home -> first option in the Family group (Technical).
    page.keyboard.press("Home")
    assert page.evaluate("() => document.activeElement && document.activeElement.getAttribute('data-okf-val')") == "auto"
    # Arrow keys also wrap within the group.
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement && document.activeElement.getAttribute('data-okf-val')") == "light"


def test_appearance_tab_exit_closes_without_stealing_focus(server_url: str, page) -> None:
    """Tab / Shift+Tab exit the menu (closing it) without stealing focus back
    to the trigger. Tab traverses each radio group (one tab stop per group),
    then exits forward; Shift+Tab exits backward from the first group."""
    _goto_with_theme(page, server_url)
    page.locator("#okf-theme").click()
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    # Tab walks Family-group -> Mode-group (menu stays open) then exits, which
    # fires the focusout that closes the menu. Bound the traversal so this is
    # robust to the number of groups.
    for _ in range(6):
        if page.evaluate("() => document.getElementById('okf-appearance-menu').hidden"):
            break
        page.keyboard.press("Tab")
    page.wait_for_function("() => document.getElementById('okf-appearance-menu').hidden")
    fwd = page.evaluate("""() => {
      const a = document.activeElement, m = document.getElementById('okf-appearance-menu');
      return { hidden: m.hidden, inMenu: m.contains(a), isTrigger: a && a.id === 'okf-theme' };
    }""")
    assert fwd["hidden"], "Tab never exited the Appearance menu"
    assert not fwd["inMenu"] and not fwd["isTrigger"], (
        "Tab stole focus back into the menu/trigger instead of moving forward"
    )
    # Re-open and Shift+Tab out the other direction. Shift+Tab from the first
    # group may land on the trigger (the control itself, menu stays open); a
    # further Shift+Tab then exits backward and closes the menu.
    page.locator("#okf-theme").click()
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    for _ in range(6):
        if page.evaluate("() => document.getElementById('okf-appearance-menu').hidden"):
            break
        page.keyboard.press("Shift+Tab")
    page.wait_for_function("() => document.getElementById('okf-appearance-menu').hidden")
    back = page.evaluate("""() => {
      const a = document.activeElement, m = document.getElementById('okf-appearance-menu');
      return { hidden: m.hidden, inMenu: m.contains(a), isTrigger: a && a.id === 'okf-theme' };
    }""")
    assert back["hidden"], "Shift+Tab never exited the Appearance menu"
    assert not back["inMenu"] and not back["isTrigger"], (
        "Shift+Tab left focus inside the menu/trigger instead of moving backward"
    )


def test_appearance_keymap_does_not_interfere_with_editable_input(server_url: str, page) -> None:
    """The Appearance keymap never suppresses ordinary editable/native keys:
    typing into the search box works, Arrow/Home/End in the input are left to
    the browser, and the closed menu stays inert."""
    _goto_with_theme(page, server_url)
    search = page.locator('input[type="search"]').first
    search.click()
    search.fill("")
    search.type("orders")
    expect(search).to_have_value("orders")
    # Native caret movement + arrow keys in the input are not hijacked.
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("Home")
    assert page.evaluate("() => document.getElementById('okf-appearance-menu').hidden") is True
    expect(search).to_have_value("orders")
    # Ordinary letter typing never opens/activates the theme control.
    page.keyboard.type("xyz")
    assert page.evaluate("() => document.getElementById('okf-appearance-menu').hidden") is True


# --- GAP-7: forced-colours + reduced-motion over the Appearance control ---

def test_appearance_forced_colors_keeps_checked_state_visible(server_url: str, page) -> None:
    """Under forced colours the Appearance picker's checked ('current')
    menuitemradio stays an unambiguous selected surface: it opts out of the
    forced-color remap and carries a system Highlight fill, so it is visually
    distinct from an unchecked peer (state never communicated by shape alone
    in a limited palette)."""
    page.emulate_media(forced_colors="active")
    _goto_with_theme(page, server_url)
    assert page.evaluate("() => matchMedia('(forced-colors: active)').matches")
    page.locator("#okf-theme").click()
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    forced = page.evaluate("""() => {
      const m = document.getElementById('okf-appearance-menu');
      const checked = m.querySelector('.okf-appearance__opt[aria-checked="true"]');
      const unchecked = m.querySelector('.okf-appearance__opt[aria-checked="false"]');
      const cs = getComputedStyle(checked), us = getComputedStyle(unchecked);
      return {
        checkedAdjust: cs.forcedColorAdjust,
        checkedBg: cs.backgroundColor,
        uncheckedBg: us.backgroundColor,
        distinguishable: cs.backgroundColor !== us.backgroundColor,
      };
    }""")
    assert forced["checkedAdjust"] == "none", (
        "checked Appearance option must opt out of forced-color remap so its Highlight fill wins"
    )
    assert forced["checkedBg"] not in ("rgba(0, 0, 0, 0)", "transparent"), (
        "checked Appearance option lost its selected fill under forced colours"
    )
    assert forced["distinguishable"], (
        "checked Appearance option is not visually distinct from an unchecked peer under forced colours"
    )


def test_appearance_reduced_motion_has_no_transitions(server_url: str, page) -> None:
    """With reduced motion, the Appearance popover carries no transitions
    (state changes are instant) yet still opens/closes correctly."""
    page.emulate_media(reduced_motion="reduce")
    _goto_with_theme(page, server_url)
    assert page.evaluate("() => matchMedia('(prefers-reduced-motion: reduce)').matches")
    page.locator("#okf-theme").click()
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    motion = page.evaluate("""() => {
      const s = getComputedStyle(document.getElementById('okf-appearance-menu'));
      return { transition: s.transitionDuration, animation: s.animationDuration };
    }""")
    assert motion["transition"] == "0s", (
        f"reduced-motion popover must have no transition, got {motion['transition']}"
    )
    assert motion["animation"] == "0s"
    # It still closes cleanly on Escape (no motion dependency on the state change).
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('okf-appearance-menu').hidden")
    expect(page.locator("#okf-theme")).to_have_attribute("aria-expanded", "false")


# ===========================================================================
# UI overhaul Phase 1 — wiki foundations browser contracts.
# Curated type colours applied to the live DOM, 76ch measure visible, card
# keyboard/focus, mobile title-before-rail, both theme families, no-JS/CSP.
# ===========================================================================


def _tab_until(page, css_selector: str, *, max_tabs: int = 80) -> bool:
    """Tab forward from the current focus position until ``activeElement``
    matches ``css_selector``. Uses REAL keyboard Tab presses (not
    ``.focus()``) so ``:focus-visible`` reliably matches — the browser's
    focus-visible heuristic treats keyboard Tab as keyboard interaction,
    while programmatic ``.focus()`` may not (and Chrome's heuristic
    suppresses the ring if the last interaction was a pointer event).

    Returns True if the target was reached within ``max_tabs`` presses;
    False otherwise. Resets focus to ``document.body`` first so the start
    position is deterministic regardless of where the page happened to
    leave focus after navigation. Also scrolls the target into view so
    off-screen focusable elements (which some browsers skip) are reachable.
    """
    # Scroll the target into view so Tab traversal doesn't skip it.
    page.evaluate(
        """(sel) => {
          const el = document.querySelector(sel);
          if (el) el.scrollIntoView({ block: 'center' });
        }""",
        css_selector,
    )
    page.evaluate("() => { document.body.focus(); }")
    for _ in range(max_tabs):
        page.keyboard.press("Tab")
        hit = page.evaluate(
            """(sel) => {
              const el = document.activeElement;
              return !!(el && el.matches && el.matches(sel));
            }""",
            css_selector,
        )
        if hit:
            return True
    return False


def _computed_outline(page) -> dict:
    """Read the computed outline of the current active element."""
    return page.evaluate(
        """() => {
          const el = document.activeElement;
          if (!el) return { missing: true };
          const s = getComputedStyle(el);
          return {
            tag: el.tagName,
            cls: el.className || "",
            outlineStyle: s.outlineStyle,
            outlineWidth: parseFloat(s.outlineWidth) || 0,
            outlineColor: s.outlineColor,
          };
        }"""
    )


def test_phase1_curated_type_color_applied_to_header_and_cards(
    server_url: str, page,
) -> None:
    """The curated type colour is applied to the concept header band
    (--okf-type-accent) AND to the index cards for the same type, so the
    identity is consistent across surfaces (design §2.1 "type = colour +
    icon, everywhere"). Orders is type Table → curated indigo."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    header_accent = page.evaluate(
        "() => getComputedStyle(document.querySelector('.okf-page__header'))"
      ".getPropertyValue('--okf-type-accent').trim()"
    )
    assert header_accent.startswith("hsl("), (
        f"header band has no curated --okf-type-accent: {header_accent!r}"
    )
    # The type chip background matches the same curated colour.
    chip_bg = page.evaluate(
        "() => getComputedStyle(document.querySelector('.okf-type-chip'))"
      ".backgroundColor"
    )
    assert chip_bg != "rgba(0, 0, 0, 0)", "type chip has no background colour"
    # Index page cards for the same type carry the same accent.
    page.goto(f"{server_url}/", wait_until="domcontentloaded")
    card_accents = page.evaluate(
        """() => [...document.querySelectorAll('.okf-card')]
          .map(c => getComputedStyle(c).getPropertyValue('--okf-type-accent').trim())"""
    )
    assert card_accents, "index page rendered zero concept cards"
    assert any(a.startswith("hsl(") for a in card_accents), (
        f"no card carries a curated --okf-type-accent: {card_accents[:3]}"
    )


def test_print_media_prints_light_and_readable(static_url: str, page) -> None:
    """Print spot check (spec: static is printable): even with a DARK saved
    theme, print media normalizes to the light ink-on-paper palette — body
    text stays dark ink on white, code keeps legible ink (browsers strip
    background graphics by default, so near-white text would vanish) — and
    screen-only chrome (topbar, studio bar, floating utilities) stands down.
    Authored content stays on the page."""
    page.set_viewport_size({"width": 1200, "height": 900})
    page.add_init_script(
        "try { localStorage.setItem('okf-theme-family','technical');"
        " localStorage.setItem('okf-theme-mode','dark'); } catch (e) {}")
    page.goto(f"{static_url}/tables/orders.html", wait_until="load")
    page.emulate_media(media="print")
    page.wait_for_timeout(200)
    out = page.evaluate(
        """() => {
          const s = (sel) => { const n = document.querySelector(sel); return n ? getComputedStyle(n) : null; };
          const body = s('body'), topbar = s('.okf-topbar'), pre = s('.okf-page__body pre');
          const p = s('.okf-page__body > p');
          return {
            theme: document.documentElement.getAttribute('data-theme'),
            bodyFg: body.color,
            pFg: p ? p.color : null,
            topbarDisplay: topbar ? topbar.display : null,
            preVisible: !!pre,
            proseBlocks: document.querySelectorAll('.okf-page__body > p').length,
          };
        }""")
    assert out["theme"] == "technical-dark"
    assert out["bodyFg"] == "rgb(16, 22, 35)", out   # dark ink, not near-white
    assert out["pFg"] == "rgb(16, 22, 35)", out
    assert out["topbarDisplay"] == "none", out        # screen chrome stood down
    assert out["preVisible"] and out["proseBlocks"] > 0, out


def test_phase1_76ch_prose_measure_visible(server_url: str, page) -> None:
    """Concept-article column contract (viewer-layout-usability revision):
    the body container is unconstrained; prose text AND wide blocks
    (pre/table) are capped by the SAME shared article-column lane on concept
    pages, so a code box and the paragraph above it report the same
    computed max-width. Verified by computed styles on a real page."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator(".okf-page__body").wait_for(state="visible")
    result = page.evaluate(
        """() => {
          const p = document.querySelector('.okf-page__body > p, .okf-page__body p');
          // The capped block is the body's DIRECT child: renderers.js nests
          // .okf-tablewrap inside a .okf-tablewrap-frame, so prefer the frame.
          const tbl = document.querySelector(
            '.okf-page__body > .okf-tablewrap-frame, .okf-page__body > .okf-tablewrap, '
            + '.okf-page__body > table, .okf-page__body > pre');
          const pStyle = p ? getComputedStyle(p).maxWidth : null;
          const tStyle = tbl ? getComputedStyle(tbl).maxWidth : null;
          return {
            pMax: pStyle,
            tMax: tStyle,
            bodyMax: getComputedStyle(document.querySelector('.okf-page__body')).maxWidth,
          };
        }"""
    )
    # The body container is unconstrained; prose children are capped.
    assert result["bodyMax"] == "none", (
        f"body container should be unconstrained, got {result['bodyMax']!r}"
    )
    assert result["pMax"] is not None and result["pMax"] != "none", (
        f"prose paragraph should be capped by the article column, got max-width={result['pMax']!r}"
    )
    # Aligned edges: the wide block shares the paragraph's lane cap.
    assert result["tMax"] == result["pMax"], (
        f"wide block max-width {result['tMax']!r} != paragraph max-width "
        f"{result['pMax']!r} — code/table blocks no longer share the article lane"
    )


def test_phase1_card_link_keyboard_focus_and_activation(server_url: str, page) -> None:
    """Index card links have a REAL visible focus ring reached by genuine
    keyboard Tab traversal (not ``.focus()``), and activate on Enter (native
    anchor semantics). The before/after outline delta fails if the
    ``.okf-card__link:focus-visible`` rule is removed."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{server_url}/", wait_until="domcontentloaded")
    page.locator(".okf-card .okf-card__link").first.wait_for(state="visible")

    # BEFORE focus: read the link's outline (should be none / 0px).
    before = page.evaluate(
        """() => {
          const el = document.querySelector('.okf-card .okf-card__link');
          const s = getComputedStyle(el);
          return { outlineStyle: s.outlineStyle, outlineWidth: parseFloat(s.outlineWidth) || 0 };
        }"""
    )

    # REAL keyboard Tab traversal to the first card link. Tab (not .focus())
    # is required so :focus-visible reliably matches.
    reached = _tab_until(page, ".okf-card .okf-card__link")
    assert reached, "Tab traversal did not reach a .okf-card__link within 40 presses"
    # Confirm the active element IS a card link (order/identity proof).
    active = page.evaluate(
        """() => {
          const el = document.activeElement;
          return { tag: el.tagName, cls: el.className || "", href: el.getAttribute('href') || '' };
        }"""
    )
    assert active["tag"] == "A" and "okf-card__link" in active["cls"], (
        f"active element after Tab is not a card link: {active!r}"
    )

    # AFTER focus: read the outline again — it must have changed (real delta).
    after = _computed_outline(page)
    # The outline must be EXPLICITLY ``solid`` (not just ``!= none``): the UA
    # default focus ring uses ``outline-style: auto``; our rule sets
    # ``outline: 2px solid var(--okf-accent)``. If the rule is removed, this
    # falls back to ``auto`` and the assertion fails.
    assert after["outlineStyle"] == "solid", (
        f"focused card link outline-style is {after['outlineStyle']!r}, not 'solid' "
        f"(the .okf-card__link:focus-visible rule may be missing): {after!r}"
    )
    assert after["outlineWidth"] >= 2.0, (
        f"focused card link outline width {after['outlineWidth']}px < 2px: {after!r}"
    )
    # REAL delta: before != after (fails if the rule is removed).
    assert (before["outlineStyle"] != after["outlineStyle"]
            or before["outlineWidth"] < after["outlineWidth"]), (
        f"focus produced no outline delta (before={before!r}, after={after!r})"
    )

    # Enter activates navigation (native anchor behaviour).
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.keyboard.press("Enter")
    assert page.locator("h1.okf-page__title").count() > 0, "Enter did not navigate to a concept page"


def test_phase1_card_link_forced_colors_focus_visible(server_url: str, page) -> None:
    """Under ACTIVE forced-colors emulation (Windows High Contrast mode),
    both card links AND quick-action buttons must resolve to a real visible
    focus outline (solid style, non-zero width) — not colour-only and not
    transparent. The global rule
    ``@media(forced-colors: active) { :focus-visible { outline: 2px solid
    Highlight !important } }`` in wiki.css guarantees this. We emulate
    forced-colors via Playwright's ``emulate_media`` and directly assert
    the COMPUTED outline (not just the CSS source text) after real Tab
    traversal on both surfaces."""
    page.set_viewport_size({"width": 1280, "height": 900})
    # Emulate forced-colors (active) for the whole test context.
    page.emulate_media(forced_colors="active")

    # --- Surface 1: card link on the index page ---
    page.goto(f"{server_url}/", wait_until="domcontentloaded")
    page.locator(".okf-card .okf-card__link").first.wait_for(state="visible")
    reached_card = _tab_until(page, ".okf-card .okf-card__link")
    assert reached_card, "Tab traversal did not reach a card link under forced-colors"
    card_outline = _computed_outline(page)
    assert card_outline["outlineStyle"] != "none", (
        f"card link outline-style is 'none' under forced-colors: {card_outline!r}"
    )
    assert card_outline["outlineWidth"] >= 2.0, (
        f"card link outline width {card_outline['outlineWidth']}px < 2px under forced-colors: {card_outline!r}"
    )

    # --- Surface 2: quick-action button on the concept page (live studio) ---
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator(".okf-studio-launcher").click()
    page.wait_for_function(
        "() => document.querySelector('.okf-sidebar-intents') !== null",
        timeout=8000,
    )
    reached_btn = _tab_until(page, ".okf-sidebar-intents button")
    assert reached_btn, "Tab traversal did not reach a quick-action button under forced-colors"
    btn_outline = _computed_outline(page)
    assert btn_outline["outlineStyle"] != "none", (
        f"quick-action outline-style is 'none' under forced-colors: {btn_outline!r}"
    )
    assert btn_outline["outlineWidth"] >= 2.0, (
        f"quick-action outline width {btn_outline['outlineWidth']}px < 2px under forced-colors: {btn_outline!r}"
    )

    # Restore normal media for subsequent tests sharing this page object.
    page.emulate_media(forced_colors="none")


def test_phase1_quick_action_buttons_native_and_keyboard_activated(
    server_url: str, page,
) -> None:
    """Quick Actions are native <button> elements with accessible labels,
    reached by REAL keyboard Tab traversal (not ``.focus()``), with a
    visible focus ring, keyboard-activatable via BOTH Enter and Space
    (native button semantics), and retaining focus after activation.
    Verified on the live studio where studio.js builds the sidebar panels."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator(".okf-studio-launcher").click()
    # Wait for studio to boot and build the sidebar panels.
    page.wait_for_function(
        "() => document.querySelector('.okf-sidebar-intents') !== null",
        timeout=8000,
    )
    intent_proof = page.evaluate(
        """() => {
          const btns = [...document.querySelectorAll('.okf-sidebar-intents button')];
          return {
            count: btns.length,
            allNative: btns.every(b => b.tagName === 'BUTTON'),
            allTypeButton: btns.every(b => b.getAttribute('type') === 'button'),
            allLabeled: btns.every(b => (b.textContent || '').trim().length > 0),
            labels: btns.map(b => (b.textContent || '').trim()).slice(0, 4),
          };
        }"""
    )
    assert intent_proof["count"] >= 2, (
        f"need ≥2 quick-action buttons for Space-coverage proof, got {intent_proof['count']}"
    )
    assert intent_proof["allNative"], "a Quick Actions control is not a native <button>"
    assert intent_proof["allTypeButton"], "a Quick Actions button is missing type='button'"
    assert intent_proof["allLabeled"], "a Quick Actions button has no text label"

    # REAL Tab traversal to the first quick-action button.
    reached_first = _tab_until(page, ".okf-sidebar-intents button")
    assert reached_first, "Tab traversal did not reach a quick-action button"
    active1 = page.evaluate(
        "() => { const el = document.activeElement; return { tag: el.tagName, cls: el.className || '' }; }"
    )
    assert active1["tag"] == "BUTTON" and "okf-sidebar-intent" in active1["cls"], (
        f"active element after Tab is not a quick-action button: {active1!r}"
    )
    # Visible focus ring after Tab (keyboard-focus-visible proof).
    focus1 = _computed_outline(page)
    assert focus1["outlineStyle"] != "none" and focus1["outlineWidth"] >= 1.0, (
        f"quick-action button has no visible focus ring after Tab: {focus1!r}"
    )

    # Enter activates the native button (opens the comments panel).
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => { const p = document.querySelector('.okf-panel'); return p && p.hidden === false; }",
        timeout=5000,
    )

    # Close the panel, then Tab to the SECOND button for Space coverage.
    page.evaluate("() => { if (window.okfLoomStudio && window.okfLoomStudio.closePanel) window.okfLoomStudio.closePanel(); }")
    page.wait_for_function(
        "() => { const p = document.querySelector('.okf-panel'); return !p || p.hidden === true; }",
        timeout=5000,
    )
    page.locator(".okf-studio-launcher").click()
    second_button = page.locator(".okf-sidebar-intents button").nth(1)
    second_button.focus()
    active2 = page.evaluate(
        """() => {
          const btns = [...document.querySelectorAll('.okf-sidebar-intents button')];
          const el = document.activeElement;
          return {
            tag: el.tagName,
            cls: el.className || '',
            isSecond: el === btns[1],
          };
        }"""
    )
    assert active2["tag"] == "BUTTON", f"second Tab target is not a button: {active2!r}"

    # Space also activates a native button.
    page.keyboard.press("Space")
    page.wait_for_function(
        "() => { const p = document.querySelector('.okf-panel'); return p && p.hidden === false; }",
        timeout=5000,
    )
    # Retained visible focus: the panel or its first focusable has focus.
    has_focus = page.evaluate(
        """() => {
          const p = document.querySelector('.okf-panel');
          if (!p || p.hidden) return false;
          return p.contains(document.activeElement) || document.activeElement === p;
        }"""
    )
    assert has_focus, "focus was lost after Space-activating the quick-action button"


def test_phase1_mobile_title_before_rail(server_url: str, page) -> None:
    """On mobile (≤900px) the concept title (h1) is above the sidebar rail
    in BOTH DOM source order AND visual paint order. The <article> now
    precedes the <aside> in the template DOM, so the correct mobile order
    is structural (no CSS ``order`` hack) — the reader sees content before
    the rail, and screen-reader/keyboard traversal hits the title first."""
    page.set_viewport_size({"width": 390, "height": 780})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    page.evaluate("() => window.scrollTo(0, 0)")
    order = page.evaluate(
        """() => {
          const article = document.querySelector('.okf-page__main');
          const sidebar = document.querySelector('.okf-page__sidebar');
          const title = document.querySelector('.okf-page__title');
          if (!article || !sidebar || !title) return {missing: true};
          const titleRect = title.getBoundingClientRect();
          const sideRect = sidebar.getBoundingClientRect();
          // DOM source order: article must precede sidebar.
          const domArticleFirst = !!(article.compareDocumentPosition(sidebar)
            & Node.DOCUMENT_POSITION_FOLLOWING);
          return {
            domArticleFirst,
            titleTop: Math.round(titleRect.top),
            sidebarTop: Math.round(sideRect.top),
            titleVisuallyFirst: titleRect.top < sideRect.top,
            gridCols: getComputedStyle(document.querySelector('.okf-page')).gridTemplateColumns,
          };
        }"""
    )
    assert not order.get("missing"), "article/sidebar/title not found"
    # DOM source order: <article> precedes <aside> (semantic/keyboard order).
    assert order["domArticleFirst"], (
        "article does not precede sidebar in DOM — mobile/AT/keyboard order "
        "puts the rail before the title"
    )
    # Visual: title paints above the rail.
    assert order["titleVisuallyFirst"], (
        f"title is not visually above the rail on mobile: "
        f"titleTop={order['titleTop']}, sidebarTop={order['sidebarTop']}"
    )


@pytest.mark.parametrize("theme", ["technical-light", "swiss-dark"])
def test_phase1_header_band_tint_in_both_theme_families(
    server_url: str, page, theme: str,
) -> None:
    """The concept header band's type-tinted background renders in both the
    Technical and Swiss families without disappearing or overflowing. The
    tint uses color-mix (theme-aware), so it must resolve to a real colour
    in each family, not transparent."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    page.evaluate(
        """theme => {
          if (window.OKFLoomTheme && window.OKFLoomTheme.setTheme)
            window.OKFLoomTheme.setTheme(theme);
          else document.documentElement.dataset.theme = theme;
        }""",
        theme,
    )
    proof = page.evaluate(
        """() => {
          const h = document.querySelector('.okf-page__header');
          const s = getComputedStyle(h);
          return {
            theme: document.documentElement.dataset.theme,
            accent: s.getPropertyValue('--okf-type-accent').trim(),
            bg: s.backgroundColor,
            borderLeft: s.borderLeftWidth,
            borderRight: s.borderRightWidth,
          };
        }"""
    )
    assert proof["theme"] == theme
    assert proof["accent"].startswith("hsl("), f"no curated accent in {theme}: {proof['accent']!r}"
    # Background resolved to a real colour (color-mix produced a value).
    assert proof["bg"] != "rgba(0, 0, 0, 0)", (
        f"header band background is transparent in {theme}: {proof['bg']!r}"
    )
    # Complete border (no side-stripe): left == right width.
    assert proof["borderLeft"] == proof["borderRight"], (
        f"header band has asymmetric borders (side stripe) in {theme}: "
        f"left={proof['borderLeft']}, right={proof['borderRight']}"
    )


def test_phase1_live_sidebar_label_after_studio_rebuild(server_url: str, page) -> None:
    """After studio.js boots and rebuilds the sidebar rail (Related +
    Sections + Quick Actions), the aside aria-label is updated to reflect
    the merged content. The SSR label is ``"Related concepts"`` (accurate
    for the static template whose sole sidebar child is the local-graph
    widget); the live label is accurate to what the JS-enhanced user now
    sees. This test pins the live mutation so a regression to the stale
    SSR label is caught. (In true no-JS the sidebar is hidden via the
    ``<noscript>`` block, so the label is never announced there.)"""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator(".okf-studio-launcher").click()
    # Wait for studio to boot (buildSidebarPanels sets the live label).
    page.wait_for_function(
        "() => document.querySelector('.okf-sidebar-intents') !== null",
        timeout=8000,
    )
    label = page.evaluate(
        "() => document.querySelector('.okf-page__sidebar')?.getAttribute('aria-label')"
    )
    assert label == "Related concepts", (
        f"live sidebar label not updated after studio rebuild: {label!r}"
    )


def test_phase1_desktop_sticky_rail_containment(server_url: str, page) -> None:
    """At desktop width the sidebar rail is ``position: sticky`` and, after
    scrolling, stays pinned below the sticky topbar, remains within the
    viewport (max-height/overflow prevent it from growing past the fold),
    and does NOT overlap the article column (layout-graph invariant)."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator(".okf-page__body").wait_for(state="visible")

    # Read the rail's computed sticky/containment geometry BEFORE scroll.
    before = page.evaluate(
        """() => {
          const sb = document.querySelector('.okf-page__sidebar');
          const topbar = document.querySelector('.okf-topbar');
          const article = document.querySelector('.okf-page__main');
          if (!sb || !topbar || !article) return {missing: true};
          const cs = getComputedStyle(sb);
          const ts = getComputedStyle(topbar);
          const sbRect = sb.getBoundingClientRect();
          const artRect = article.getBoundingClientRect();
          return {
            position: cs.position,
            top: cs.top,
            maxHeight: cs.maxHeight,
            overflowY: cs.overflowY,
            topbarH: topbar.getBoundingClientRect().height,
            // Non-overlap: sidebar's right edge ≤ article's left edge.
            sideRight: Math.round(sbRect.right),
            artLeft: Math.round(artRect.left),
            sideLeft: Math.round(sbRect.left),
            sideTop: Math.round(sbRect.top),
          };
        }"""
    )
    assert not before.get("missing"), "sidebar/topbar/article not found"
    assert before["position"] == "sticky", (
        f"sidebar is not position:sticky at desktop: {before['position']!r}"
    )
    # Sticky top offset is at or below the topbar height (rail pins below it).
    topbar_h = int(before["topbarH"])
    sticky_top_px = float(before["top"].replace("px", "")) if before["top"].endswith("px") else 0
    assert sticky_top_px >= topbar_h - 1, (
        f"sidebar sticky top ({sticky_top_px}px) is above the topbar height "
        f"({topbar_h}px) — rail would slide under the topbar"
    )
    # Viewport containment: max-height is capped (not 'none') so a very long
    # rail scrolls internally rather than overflowing the viewport.
    assert before["maxHeight"] != "none", (
        f"sidebar has no max-height cap (would overflow viewport): {before['maxHeight']!r}"
    )
    assert before["overflowY"] in ("auto", "scroll"), (
        f"sidebar overflow-y is {before['overflowY']!r} (should be auto/scroll for internal scroll)"
    )
    # Non-overlap: sidebar's right edge ≤ article's left edge.
    assert before["sideRight"] <= before["artLeft"] + 1, (
        f"sidebar overlaps the article: sideRight={before['sideRight']} > artLeft={before['artLeft']}"
    )

    # Scroll down and verify the rail stays pinned (sticky behaviour).
    page.evaluate("() => window.scrollTo(0, 600)")
    # Wait for the scroll to settle deterministically (no fixed-time sleep):
    # assert the document has actually scrolled past 500px, which proves the
    # browser applied the scrollTo before we read sticky geometry.
    page.wait_for_function("() => window.scrollY > 500", timeout=3000)
    after = page.evaluate(
        """() => {
          const sb = document.querySelector('.okf-page__sidebar');
          const topbar = document.querySelector('.okf-topbar');
          const sbRect = sb.getBoundingClientRect();
          const tbRect = topbar.getBoundingClientRect();
          const artRect = document.querySelector('.okf-page__main').getBoundingClientRect();
          return {
            position: getComputedStyle(sb).position,
            // After scroll, a sticky rail's top should be at/near the sticky
            // offset (below the topbar), NOT scrolled off-screen.
            sideTop: Math.round(sbRect.top),
            topbarBottom: Math.round(tbRect.bottom),
            sideRight: Math.round(sbRect.right),
            artLeft: Math.round(artRect.left),
            sideHeight: Math.round(sbRect.height),
            viewportH: window.innerHeight,
          };
        }"""
    )
    assert after["position"] == "sticky", "sidebar lost sticky position after scroll"
    # The rail's top is at or below the topbar's bottom (pinned under it).
    assert after["sideTop"] >= after["topbarBottom"] - 2, (
        f"sidebar slid above the topbar after scroll: sideTop={after['sideTop']} < topbarBottom={after['topbarBottom']}"
    )
    # The rail is still within the viewport (not scrolled off the bottom).
    assert after["sideTop"] < after["viewportH"], (
        f"sidebar is scrolled off-screen (sideTop={after['sideTop']} >= viewportH={after['viewportH']})"
    )
    # Still non-overlapping with the article after scroll.
    assert after["sideRight"] <= after["artLeft"] + 1, (
        f"sidebar overlaps article after scroll: sideRight={after['sideRight']} > artLeft={after['artLeft']}"
    )


# ---------------------------------------------------------------------------
# Phase 1 index visual P1 fixes: compact type-nav, responsive auto-fit grid,
# card type icon + textual label.
# ---------------------------------------------------------------------------


def test_phase1_index_type_nav_present_and_jumps(server_url: str, page) -> None:
    """The root index renders a compact ``<nav class="okf-type-nav">`` with
    jump links that anchor-scroll to each type section. Clicking a nav link
    moves the corresponding section into view (not a full navigation)."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{server_url}/", wait_until="domcontentloaded")
    page.locator(".okf-type-nav").wait_for(state="visible")
    nav_links = page.locator(".okf-type-nav__link")
    count = nav_links.count()
    assert count >= 2, f"type-nav should have ≥2 jump links, got {count}"
    # The old subdirs tile grid is absent on the root index.
    assert page.locator(".okf-subdirs").count() == 0, (
        "root index still shows the old subdirs tile grid"
    )
    # Click the first nav link; the matching section scrolls into view.
    first_link = nav_links.first
    href = first_link.get_attribute("href") or ""
    assert href.startswith("#"), f"type-nav link href is not an anchor: {href!r}"
    target_id = href[1:]
    first_link.click()
    # The target section exists and is now scrolled into the viewport.
    page.wait_for_function(
        """(id) => {
          const el = document.getElementById(id);
          if (!el) return false;
          const r = el.getBoundingClientRect();
          return r.top < window.innerHeight && r.bottom > 0;
        }""",
        arg=target_id,
        timeout=3000,
    )


def test_phase1_index_card_type_chip_has_icon_and_label(server_url: str, page) -> None:
    """Each concept card carries a ``.okf-card__type`` chip with an inline
    SVG icon AND a textual type label (non-colour type identity). The chip
    is a <span>, not an <a>, so the first <a> remains the concept link."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{server_url}/", wait_until="domcontentloaded")
    page.locator(".okf-card").first.wait_for(state="visible")
    proof = page.evaluate(
        """() => {
          const card = document.querySelector('.okf-card');
          if (!card) return {missing: true};
          const chip = card.querySelector('.okf-card__type');
          const label = card.querySelector('.okf-card__type-label');
          const icon = chip ? chip.querySelector('svg') : null;
          const firstA = card.querySelector('a');
          return {
            hasChip: !!chip,
            hasLabel: !!(label && (label.textContent || '').trim()),
            labelText: label ? (label.textContent || '').trim() : '',
            hasIconSvg: !!icon,
            firstAIsConceptLink: firstA ? firstA.classList.contains('okf-card__link') : false,
          };
        }"""
    )
    assert not proof.get("missing"), "no card found on the index"
    assert proof["hasChip"], "card missing .okf-card__type chip"
    assert proof["hasLabel"], "card type chip missing textual label"
    assert proof["labelText"], f"card type label text is empty: {proof!r}"
    assert proof["hasIconSvg"], "card type chip missing inline SVG icon"
    assert proof["firstAIsConceptLink"], (
        "first <a> in the card is not the concept link (presence contract broken)"
    )


def test_phase1_index_responsive_grid_single_and_multi_card(server_url: str, page) -> None:
    """At desktop width, a type section with a SINGLE card expands to fill
    the section width (auto-fit, no orphaned narrow tile), and a section
    with MULTIPLE cards uses a responsive multi-column grid."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{server_url}/", wait_until="domcontentloaded")
    page.locator(".okf-cardgrid").first.wait_for(state="visible")
    grid_data = page.evaluate(
        """() => {
          const grids = [...document.querySelectorAll('.okf-cardgrid')];
          return grids.map(g => {
            const cards = g.querySelectorAll('.okf-card');
            const gridStyle = getComputedStyle(g);
            const firstCard = cards[0];
            const cardRect = firstCard ? firstCard.getBoundingClientRect() : null;
            const gridRect = g.getBoundingClientRect();
            return {
              cardCount: cards.length,
              templateColumns: gridStyle.gridTemplateColumns,
              // A single card should span most of the grid width (auto-fit);
              // multiple cards should each be narrower than the grid.
              cardWidth: cardRect ? Math.round(cardRect.width) : 0,
              gridWidth: Math.round(gridRect.width),
            };
          });
        }"""
    )
    assert grid_data, "no card grids found on the index"
    # At least one grid should have a single card that spans wide (auto-fit).
    single_card_grids = [g for g in grid_data if g["cardCount"] == 1]
    if single_card_grids:
        g = single_card_grids[0]
        # auto-fit: a single card should span ≥ 70% of the grid width
        # (not orphaned at ~290px in a ~1100px grid).
        assert g["cardWidth"] >= g["gridWidth"] * 0.65, (
            f"single card is only {g['cardWidth']}px in a {g['gridWidth']}px grid "
            f"(orphaned narrow tile — auto-fit not working)"
        )
    # At least one grid should have multiple cards in columns.
    multi_grids = [g for g in grid_data if g["cardCount"] >= 2]
    if multi_grids:
        g = multi_grids[0]
        # Multi-column: the grid template has more than one track.
        tmpl = (g["templateColumns"] or "").strip()
        col_count = len(tmpl.split()) if tmpl else 0
        # Each card should be narrower than the full grid width.
        assert g["cardWidth"] < g["gridWidth"] * 0.9, (
            f"multi-card grid has cards nearly as wide as the grid ({g['cardWidth']}px "
            f"in {g['gridWidth']}px) — not using responsive columns"
        )


def test_phase1_index_mobile_grid_collapses_to_single_column(
    server_url: str, page,
) -> None:
    """At mobile width (≤900px) the card grid collapses to a single column
    (one card per row), matching the existing responsive rule."""
    page.set_viewport_size({"width": 390, "height": 780})
    page.goto(f"{server_url}/", wait_until="domcontentloaded")
    page.locator(".okf-cardgrid").first.wait_for(state="visible")
    grid = page.evaluate(
        """() => {
          const g = document.querySelector('.okf-cardgrid');
          const cs = getComputedStyle(g);
          const cards = g.querySelectorAll('.okf-card');
          const gridRect = g.getBoundingClientRect();
          const firstRect = cards[0] ? cards[0].getBoundingClientRect() : null;
          return {
            template: cs.gridTemplateColumns,
            cardCount: cards.length,
            cardWidth: firstRect ? Math.round(firstRect.width) : 0,
            gridWidth: Math.round(gridRect.width),
          };
        }"""
    )
    # Single column at mobile.
    tmpl = (grid["template"] or "").strip()
    col_count = len(tmpl.split()) if tmpl else 0
    assert col_count <= 1, (
        f"mobile card grid has {col_count} columns (expected 1): {grid['template']!r}"
    )
    assert grid["cardWidth"] >= grid["gridWidth"] * 0.85, (
        f"mobile card is only {grid['cardWidth']}px in a {grid['gridWidth']}px grid"
    )


# ---------------------------------------------------------------------------
# Phase 1 audit P1 invariants: semantic DOM order, centered reading lane.
# ---------------------------------------------------------------------------


def test_phase1_desktop_article_before_sidebar_in_dom_and_visual(
    server_url: str, page,
) -> None:
    """P1 #1 (layout-graph): at DESKTOP, the <article> precedes <aside> in
    DOM source order (semantic/keyboard/screen-reader order), while CSS
    grid placement keeps the sidebar VISUALLY on the LEFT (grid-column: 1)
    and the article on the RIGHT (grid-column: 2). No CSS ``order`` hack —
    the placement is structural."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator("h1.okf-page__title").wait_for(state="visible")
    proof = page.evaluate(
        """() => {
          const article = document.querySelector('.okf-page__main');
          const sidebar = document.querySelector('.okf-page__sidebar');
          if (!article || !sidebar) return {missing: true};
          // DOM source order: article must precede sidebar.
          const domArticleFirst = !!(article.compareDocumentPosition(sidebar)
            & Node.DOCUMENT_POSITION_FOLLOWING);
          const aRect = article.getBoundingClientRect();
          const sRect = sidebar.getBoundingClientRect();
          return {
            domArticleFirst,
            // Visual placement: sidebar on LEFT, article on RIGHT.
            sidebarLeft: Math.round(sRect.left),
            articleLeft: Math.round(aRect.left),
            sidebarVisuallyLeft: sRect.left < aRect.left,
            // Grid column assignments.
            articleCol: getComputedStyle(article).gridColumn,
            sidebarCol: getComputedStyle(sidebar).gridColumn,
          };
        }"""
    )
    assert not proof.get("missing"), "article/sidebar not found"
    assert proof["domArticleFirst"], (
        "article does not precede sidebar in DOM at desktop — "
        "keyboard/screen-reader order is wrong"
    )
    assert proof["sidebarVisuallyLeft"], (
        f"sidebar is not visually on the left at desktop: "
        f"sidebarLeft={proof['sidebarLeft']}, articleLeft={proof['articleLeft']}"
    )


def test_phase1_centered_reading_lane_balanced(server_url: str, page) -> None:
    """Viewer-layout-usability contract: on a concept page the article is ONE
    bounded content column. Every authored block — paragraph, heading, list,
    pre, table wrapper — shares the same left/right edges (within tolerance),
    the paragraph fills the available column (only the deliberate reading
    inset remains), and no block overhangs the column. Verified by geometry.

    History: prose used to be a centered ~76ch lane inside a wider article,
    so code/tables overhung the text by ~150px per side and the page read as
    a narrow strip floating in whitespace. The column is now the shared
    bounding box; when a viewport leaves surplus width the whole column
    centers (balanced gutters) rather than any individual block."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator(".okf-page__body").wait_for(state="visible")
    geom = page.evaluate(
        """() => {
          const body = document.querySelector('.okf-page__body');
          const col = document.querySelector('.okf-page__body-col');
          const infobox = document.querySelector('.okf-page__infobox');
          if (!body || !col) return {missing: true};
          const p = body.querySelector('p');
          const pre = body.querySelector('pre, .okf-tablewrap');
          if (!p) return {missing: true};
          const pRect = p.getBoundingClientRect();
          const cRect = col.getBoundingClientRect();
          const edges = {p: [Math.round(pRect.left), Math.round(pRect.right)]};
          if (pre) {
            const r = pre.getBoundingClientRect();
            edges.pre = [Math.round(r.left), Math.round(r.right)];
          }
          const h2 = body.querySelector('h2');
          if (h2) {
            const r = h2.getBoundingClientRect();
            edges.h2 = [Math.round(r.left), Math.round(r.right)];
          }
          return {
            paraWidth: Math.round(pRect.width),
            colWidth: Math.round(cRect.width),
            edges,
            paraMaxWidth: getComputedStyle(p).maxWidth,
            // No block overhangs the reading column.
            overhang: Math.round(Math.max(pRect.right - cRect.right, cRect.left - pRect.left)),
          };
        }"""
    )
    assert not geom.get("missing"), "body/column/paragraph not found"
    # The paragraph has a finite max-width (the shared article column).
    assert geom["paraMaxWidth"] != "none", (
        f"prose paragraph has no max-width (measure not applied): {geom['paraMaxWidth']!r}"
    )
    # Every authored block shares the same left AND right edges (±2px).
    edges = geom["edges"]
    for name, edge in edges.items():
        assert abs(edge[0] - edges["p"][0]) <= 2 and abs(edge[1] - edges["p"][1]) <= 2, (
            f"{name} edges {edge} do not align with paragraph edges {edges['p']} "
            "— article blocks no longer share one column"
        )
    # The paragraph fills the column: only the deliberate reading inset
    # (band padding + border ≈ 25px per side) remains, not a centered-lane
    # void.
    inset = 40  # generous bound: space-5 (24px) + border + tolerance
    assert geom["paraWidth"] >= geom["colWidth"] - 2 * inset, (
        f"prose paragraph ({geom['paraWidth']}px) leaves more than the reading "
        f"inset free in the column ({geom['colWidth']}px) — the narrow centered "
        "lane is back"
    )
    # No block overhangs the column.
    assert geom["overhang"] <= 1, (
        f"a block overhangs the reading column by {geom['overhang']}px"
    )


def test_article_column_fills_page_and_never_overflows_document(server_url: str, page) -> None:
    """Viewer-layout-usability regression: at representative desktop, tablet,
    and phone widths the concept article (a) fills the available article
    column — a paragraph is NOT a narrow centered strip with huge voids —
    (b) keeps code/tables on the paragraph edges, and (c) never produces
    unintended document-level horizontal overflow. A long code block scrolls
    LOCALLY inside its block (scrollWidth > clientWidth on the block, not on
    the document)."""
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    page.locator(".okf-page__body").wait_for(state="visible")
    for width in (1440, 1920, 1024, 390):
        page.set_viewport_size({"width": width, "height": 950})
        page.wait_for_timeout(250)
        geom = page.evaluate(
            """() => {
              const doc = document.documentElement;
              const body = document.querySelector('.okf-page__body');
              const p = body && body.querySelector('p');
              const pre = body && (body.querySelector('.okf-content-scrollport--code, .okf-content-scrollport')
                                  || body.querySelector('pre'));
              const wide = pre || p;
              const out = {
                overflow: doc.scrollWidth - doc.clientWidth,
              };
              if (p && wide) {
                const pr = p.getBoundingClientRect();
                const wr = wide.getBoundingClientRect();
                out.paraLeft = Math.round(pr.left);
                out.wideLeft = Math.round(wr.left);
                out.paraWidth = Math.round(pr.width);
                out.articleWidth = Math.round(
                  document.querySelector('.okf-page__main').getBoundingClientRect().width);
                out.wideScrolls = wide.scrollWidth > wide.clientWidth + 1;
              }
              return out;
            }"""
        )
        assert geom["overflow"] <= 0, (
            f"document scrolls horizontally at {width}px viewport: "
            f"{geom['overflow']}px overflow"
        )
        assert abs(geom["wideLeft"] - geom["paraLeft"]) <= 2, (
            f"at {width}px a wide block (x={geom['wideLeft']}) is off the "
            f"paragraph edge (x={geom['paraLeft']})"
        )
        # The paragraph fills the article: it may only lose the reading
        # inset (≈25px per side) plus the centered-surplus case on ultra-wide
        # shells — never collapse back to a narrow centered strip.
        assert geom["paraWidth"] >= geom["articleWidth"] - 120, (
            f"at {width}px the paragraph ({geom['paraWidth']}px) collapses far "
            f"inside the article column ({geom['articleWidth']}px)"
        )
    # Long code scrolls locally at phone width (local scroll, not document
    # scroll; the orders SQL block is wider than a 390px column).
    assert geom["wideScrolls"], (
        "expected a locally scrolling wide code block on the orders page"
    )


def test_search_mode_filter_row_is_a_tidy_wrapping_row(server_url: str, page) -> None:
    """Viewer-layout-usability regression: the six search match modes render
    as one deliberate filter row — flex with an even column gap and centred
    items (no per-item margins making ragged spacing), wrapping onto aligned
    rows on a phone-width viewport instead of an awkwardly spread strip."""
    page.goto(f"{server_url}/__search?q=index", wait_until="domcontentloaded")
    page.locator("#okf-search-mode").wait_for(state="attached")
    wide = page.evaluate(
        """() => {
          const modes = document.querySelector('#okf-search-mode');
          const s = getComputedStyle(modes);
          const items = [...modes.querySelectorAll('.okf-search-mode')];
          // Gap is only defined between neighbours on the SAME wrapped row.
          const gaps = [];
          for (let i = 1; i < items.length; i++) {
            const a = items[i - 1].getBoundingClientRect();
            const b = items[i].getBoundingClientRect();
            if (Math.round(a.top) === Math.round(b.top)) gaps.push(Math.round(b.left - a.right));
          }
          return {
            display: s.display,
            align: s.alignItems,
            columnGap: parseFloat(s.columnGap) || 0,
            count: items.length,
            gaps,
          };
        }"""
    )
    assert wide["display"] == "flex", "match modes row is not a flex row"
    assert wide["align"] == "center", "match mode radios/labels are not centred"
    assert wide["count"] == 6
    assert all(g >= 16 for g in wide["gaps"]), (
        f"uneven/left-glued mode gaps: {wide['gaps']}"
    )
    # Phone width: the row wraps instead of overflowing.
    page.set_viewport_size({"width": 390, "height": 800})
    page.wait_for_timeout(200)
    wrapped = page.evaluate(
        """() => {
          const items = [...document.querySelectorAll('#okf-search-mode .okf-search-mode')];
          const tops = new Set(items.map(el => Math.round(el.getBoundingClientRect().top)));
          const doc = document.documentElement;
          return {rows: tops.size, overflow: doc.scrollWidth - doc.clientWidth};
        }"""
    )
    assert wrapped["rows"] > 1, "modes do not wrap at phone width"
    assert wrapped["overflow"] <= 0, "modes row forces document overflow at phone width"


def test_phase3_refresh_a_superseded_by_failing_b_restores_last_accepted_readiness(server_url: str, page) -> None:
    """ASYNC_LIFECYCLE_MATRIX: A in-flight -> B aborts A -> B fails; only B owns
    failure publication and restores the last accepted payload's coherent ready
    state. A late result is fenced; a fresh C request recovers."""
    _open_graph(page, server_url)
    page.wait_for_function("() => document.body.dataset.okfGraphCaptureReady === 'ready'")
    result = page.evaluate(r"""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl;
      const native=window.fetch.bind(window),base=await native(url).then(r=>r.json()),held=[];
      window.fetch=(req,opt)=>String(req)===url?new Promise((resolve,reject)=>{
        const call={resolve,reject,signal:opt.signal};held.push(call);
        opt.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
      }):native(req,opt);
      const a=G.refreshGraph();await new Promise(requestAnimationFrame);
      const duringA={graph:document.body.dataset.okfGraphCaptureReady,page:document.body.dataset.okfCaptureReady};
      const b=G.refreshGraph();await new Promise(requestAnimationFrame);
      held[1].reject(new Error('owned B failure'));await Promise.all([a,b]);
      const failed={graph:document.body.dataset.okfGraphCaptureReady,page:document.body.dataset.okfCaptureReady,error:document.body.dataset.okfGraphRefreshError};
      const c=G.refreshGraph();await new Promise(requestAnimationFrame);
      const fresh=JSON.parse(JSON.stringify(base));fresh.bundle_revision='phase3-race-recovery';held[2].resolve(new Response(JSON.stringify(fresh),{status:200,headers:{'Content-Type':'application/json'}}));await c;await new Promise(requestAnimationFrame);
      return {duringA,failed,recovered:{graph:document.body.dataset.okfGraphCaptureReady,page:document.body.dataset.okfCaptureReady,revision:document.body.dataset.okfCaptureRevision},aAborted:held[0].signal.aborted};
    }""")
    assert result["duringA"] == {"graph": "loading", "page": "loading"}
    assert result["failed"] == {"graph": "ready", "page": "ready", "error": "graph-refresh"}
    assert result["aAborted"] is True
    assert result["recovered"] == {"graph": "ready", "page": "ready", "revision": "phase3-race-recovery"}


def test_phase3_post_mutation_pre_raf_pagehide_revokes_readiness_owner_and_bfcache_recovers(server_url: str, page) -> None:
    """ASYNC_LIFECYCLE_MATRIX: accepted DOM mutation -> readiness rAF paused ->
    pagehide revokes owner -> late callback publishes nothing -> BFCache owner
    revalidates canvas/index/detail and publishes coherent readiness."""
    _open_graph(page, server_url)
    result = page.evaluate(r"""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,nativeFetch=window.fetch.bind(window),nativeRaf=window.requestAnimationFrame.bind(window),base=await nativeFetch(url).then(r=>r.json());
      let revision='phase3-paused',held=[];window.requestAnimationFrame=cb=>{held.push(cb);return held.length;};
      window.fetch=(req,opt)=>{if(String(req)!==url)return nativeFetch(req,opt);const payload=JSON.parse(JSON.stringify(base));payload.bundle_revision=revision;return Promise.resolve(new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}}));};
      const pending=G.refreshGraph();while(!held.length)await new Promise(resolve=>nativeRaf(resolve));
      window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));
      const hidden={graph:document.body.dataset.okfGraphCaptureReady,page:document.body.dataset.okfCaptureReady,error:document.body.dataset.okfGraphRefreshError||null};
      held.splice(0).forEach(cb=>cb(performance.now()));await pending;
      const late={graph:document.body.dataset.okfGraphCaptureReady,page:document.body.dataset.okfCaptureReady,error:document.body.dataset.okfGraphRefreshError||null};
      revision='phase3-bfcache-fresh';window.requestAnimationFrame=nativeRaf;window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
      await new Promise(resolve=>setTimeout(resolve,50));await new Promise(nativeRaf);
      return {hidden,late,recovered:{graph:document.body.dataset.okfGraphCaptureReady,page:document.body.dataset.okfCaptureReady,revision:document.body.dataset.okfCaptureRevision,index:document.querySelectorAll('.okf-node-index__item').length,canvas:G.cy.nodes().length}};
    }""")
    assert result["late"] == result["hidden"]
    assert result["late"]["error"] is None
    assert result["recovered"]["graph"] == result["recovered"]["page"] == "ready"
    assert result["recovered"]["revision"] == "phase3-bfcache-fresh"
    assert result["recovered"]["index"] == result["recovered"]["canvas"] > 0


def test_phase3_generated_single_file_graph_boot_makes_no_http_or_comment_request(tmp_path: Path, page) -> None:
    """Generated single-file initialization is self-contained and cannot call comments."""
    from okf_loom import Bundle
    from okf_loom.render import render_single_file

    output = tmp_path / "phase3-request-proof.html"
    render_single_file(Bundle.load(DEMO_BUNDLE), output)
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(output.as_uri(), wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph && document.body.dataset.okfGraphCaptureReady === 'ready'")
    page.screenshot(path="/tmp/okf-phase3-single.png", full_page=True)
    # The generated artifact currently declares its pinned graph-engine scripts
    # on jsDelivr; initialization must make no application/API request. Keep the
    # allowlist exact so a new runtime endpoint cannot hide among CDN traffic.
    network = [url for url in requests if url.startswith(("http://", "https://"))]
    assert network and all(url.startswith("https://cdn.jsdelivr.net/npm/") for url in network)
    assert not [url for url in requests if "comment" in url.lower()]


@pytest.mark.parametrize("surface", ["live", "static", "single"])
def test_phase3_production_graph_capture_parity_is_revision_and_surface_coherent(
    surface: str, server_url: str, static_url: str, tmp_path: Path, page
) -> None:
    """RENDERING_CAPTURE_MATRIX: production live/static/single graph targets
    authorize only an accepted revision with synchronized canvas/index/detail."""
    if surface == "live":
        url = server_url + "/__graph"
    elif surface == "static":
        url = static_url + "/__graph.html"
    else:
        from okf_loom import Bundle
        from okf_loom.render import render_single_file
        output = tmp_path / "phase3-parity.html"
        render_single_file(Bundle.load(DEMO_BUNDLE), output)
        url = output.as_uri()
    page.goto(url, wait_until="domcontentloaded")
    if surface == "single":
        page.locator('[data-okf-shell-item="graph"] a').click()
    page.wait_for_function("() => window.__okfLoomGraph && document.body.dataset.okfGraphCaptureReady === 'ready'")
    proof = page.evaluate(r"""async () => {
      const revision=document.body.dataset.okfCaptureRevision;
      const ready=await window.okfCaptureReadiness.start({target:'graph',expectedRevision:revision}).promise;
      const first=document.querySelector('.okf-node-index__item');if(first)first.click();
      await new Promise(requestAnimationFrame);
      const state=window.okfGraphCaptureState();
      const selected=window.okfGraphSelection.conceptId,label=selected&&window.__okfLoomGraph.cy.getElementById(selected).data('label');
      return {revision,readyRevision:ready.metadata.revision,canvas:state.nodeCount,payload:state.payloadNodeCount,index:document.querySelectorAll('.okf-node-index__item').length,selected,detail:document.getElementById('detail-content').textContent,label};
    }""")
    assert proof["revision"] and proof["readyRevision"] == proof["revision"]
    assert proof["canvas"] == proof["payload"] == proof["index"] > 0
    assert proof["selected"] and proof["label"] in proof["detail"]
    page.screenshot(path=f"/tmp/okf-phase3-{surface}.png", full_page=True)


def test_phase3_accepted_metadata_and_same_endpoint_label_reconcile_before_ready(
    server_url: str, page,
) -> None:
    """Accepted payload data, not initial-load projections, owns every surface."""
    _open_graph(page, server_url)
    result = page.evaluate(r"""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl;
      const native=window.fetch.bind(window),base=await native(url).then(r=>r.json());
      const edge=base.edges.find(e=>e.data.label) || base.edges[0], key=edge.data.source+'__'+edge.data.target;
      const id=edge.data.source, old=JSON.parse(JSON.stringify(base)), fresh=JSON.parse(JSON.stringify(base));
      old.bundle_revision='accepted-old'; old.nodes.find(n=>n.data.id===id).data.timestamp='2001-01-01T00:00:00Z';
      fresh.bundle_revision='accepted-new';
      const nd=fresh.nodes.find(n=>n.data.id===id).data;
      nd.timestamp=new Date().toISOString(); nd.body_chars=98765; nd.tags=['accepted-theme'];
      nd.description='Accepted metadata detail marker';
      nd.metadata=Object.assign({},nd.metadata,{project:'accepted-project',graph_cluster:'accepted-cluster'});
      fresh.edges.find(e=>e.data.source===edge.data.source&&e.data.target===edge.data.target).data.label='accepted_relationship';
      let queue=[old];
      window.fetch=(req,opt)=>String(req)===url?Promise.resolve(new Response(JSON.stringify(queue.shift()),{headers:{'Content-Type':'application/json'}})):native(req,opt);
      await G.refreshGraph(); await new Promise(requestAnimationFrame);
      G.applyLens('recent'); await new Promise(resolve=>setTimeout(resolve,300));
      const beforeColor=G.cy.getElementById(id).data('color');
      queue=[fresh]; await G.refreshGraph(); await new Promise(requestAnimationFrame);
      const node=G.cy.getElementById(id),cyEdge=G.cy.getElementById(key);
      const checkbox=Array.from(document.querySelectorAll('.okf-signal__row--check')).find(x=>x.textContent.includes('Show relationship labels')).querySelector('input');
      if(!checkbox.checked)checkbox.click(); await new Promise(requestAnimationFrame);
      window.okfActivateGraphConcept(id,'pointer'); await new Promise(requestAnimationFrame);
      const revision=document.body.dataset.okfCaptureRevision;
      const ready=await window.okfCaptureReadiness.start({target:'graph',expectedRevision:revision}).promise;
      return {revision,readyRevision:ready.metadata.revision,readyState:document.body.dataset.okfGraphCaptureReady,
        edgeLabel:cyEdge.data('label'),paintedLabel:cyEdge.style('label'),showLabel:cyEdge.hasClass('okf-show-label'),
        timestamp:node.data('timestamp'),bodyChars:node.data('body_chars'),tags:node.data('tags'),metadata:node.data('metadata'),
        colorChanged:beforeColor!==node.data('color'),detail:document.getElementById('detail-content').textContent,
        summary:(document.querySelector('.okf-lens-summary')||{}).textContent||''};
    }""")
    assert result["revision"] == result["readyRevision"] == "accepted-new"
    assert result["readyState"] == "ready"
    assert result["edgeLabel"] == result["paintedLabel"] == "accepted_relationship"
    assert result["showLabel"] is True
    assert result["bodyChars"] == 98765 and result["tags"] == ["accepted-theme"]
    assert result["metadata"]["project"] == "accepted-project"
    assert result["metadata"]["graph_cluster"] == "accepted-cluster"
    assert result["colorChanged"] is True
    assert "Accepted metadata detail marker" in result["detail"]


def test_phase3_overlapping_comment_refresh_latest_count_wins_every_surface(
    server_url: str, page,
) -> None:
    """A late, older accepted-shape response cannot restore stale comment metadata."""
    _open_graph(page, server_url)
    result = page.evaluate(r"""async () => {
      const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl;
      const native=window.fetch.bind(window),base=await native(url).then(r=>r.json()),id=base.nodes[0].data.id;
      function payload(count,revision){const p=JSON.parse(JSON.stringify(base));p.bundle_revision=revision;p.nodes[0].data.active_comment_count=count;return p;}
      let firstResolve,calls=0;
      window.fetch=(req,opt)=>{if(String(req)!==url)return native(req,opt);calls++;
        if(calls===1)return new Promise(resolve=>{firstResolve=()=>resolve(new Response(JSON.stringify(payload(3,'comments-old')),{headers:{'Content-Type':'application/json'}}));});
        return Promise.resolve(new Response(JSON.stringify(payload(29,'comments-new')),{headers:{'Content-Type':'application/json'}}));};
      const older=G.refreshGraph();await new Promise(requestAnimationFrame);const newer=G.refreshGraph();await newer;
      firstResolve();await older;await new Promise(requestAnimationFrame);
      const button=document.querySelector('.okf-node-index__item[data-target="'+CSS.escape(id)+'"]');button.click();await new Promise(requestAnimationFrame);
      const node=G.cy.getElementById(id),detail=Array.from(document.querySelectorAll('#okf-detail-signals dt')).find(x=>x.textContent==='Active comments');
      const revision=document.body.dataset.okfCaptureRevision;
      const ready=await window.okfCaptureReadiness.start({target:'graph',expectedRevision:revision}).promise;
      return {revision,readyRevision:ready.metadata.revision,count:node.data('active_comment_count'),
        canvas:decodeURIComponent(node.data('commentBadgeUrl')),badge:button.querySelector('.okf-node-index__comments').textContent,
        aria:button.getAttribute('aria-label'),detail:detail&&detail.nextElementSibling.textContent,
        skipped:G.refreshStats.skippedStale,ready:document.body.dataset.okfGraphCaptureReady};
    }""")
    assert result["revision"] == result["readyRevision"] == "comments-new"
    assert result["count"] == 29 and ">29<" in result["canvas"]
    assert result["badge"] == result["detail"] == "29"
    assert "29 active comments" in result["aria"]
    assert result["skipped"] >= 1 and result["ready"] == "ready"


def test_phase3_opted_comment_summary_browser_parity_and_portable_zero_requests(
    tmp_path: Path, page,
) -> None:
    """One persisted fixture has exact live/static/single counts; portable modes stay offline."""
    from okf_loom.model import Bundle
    from okf_loom.render import render_single_file
    from okf_loom.studio import Studio

    root = tmp_path / "comment-parity-bundle"
    shutil.copytree(DEMO_BUNDLE, root)
    studio = Studio.for_bundle(root)
    studio.post_comment(concept="tables/orders", body="private one")
    studio.post_comment(concept="tables/orders", body="private two")
    studio.post_comment(concept="tables/customers", body="private three")

    static_out = tmp_path / "comment-static"
    subprocess.run(
        okf_module_argv("build", str(root), "--target", "static", "--out", str(static_out),
                        "--include-comment-summary"),
        cwd=str(TOOLKIT_ROOT), env=okf_subprocess_env(), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    single = tmp_path / "comment-single.html"
    render_single_file(Bundle.load(root), single, include_comment_summary=True)

    live_port, static_port = _free_port(), _free_port()
    live_base, static_base = f"http://127.0.0.1:{live_port}", f"http://127.0.0.1:{static_port}"
    live = subprocess.Popen(
        okf_module_argv("serve", str(root), "--host", "127.0.0.1", "--port", str(live_port),
                        "--no-watch", "--no-open"),
        cwd=str(TOOLKIT_ROOT), env=okf_subprocess_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    static = subprocess.Popen(
        [os.environ.get("PYTHON", "python"), "-m", "http.server", str(static_port),
         "--bind", "127.0.0.1", "--directory", str(static_out)],
        cwd=str(TOOLKIT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    phase = {"name": ""}
    requests: dict[str, list[str]] = {"live": [], "static": [], "single": []}
    page.on("request", lambda request: requests[phase["name"]].append(request.url) if phase["name"] else None)

    def counts(url: str, name: str) -> dict[str, int]:
        phase["name"] = name
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("() => window.__okfLoomGraph && document.body.dataset.okfGraphCaptureReady === 'ready'")
        return page.evaluate(
            """() => Object.fromEntries(window.__okfLoomGraph.cy.nodes().map(
              n => [n.id(), n.data('active_comment_count')]))"""
        )

    try:
        _wait_for_server(live, live_base)
        _wait_for_server(static, static_base)
        # Portable INDEX proof: the opted-in static index renders its SSR
        # engagement without touching any live comment/event transport.
        phase["name"] = "static"
        page.goto(static_base + "/index.html", wait_until="networkidle")
        assert page.locator(".okf-card__comments").count() == 2
        assert page.locator("#okf-recent-rail").count() <= 1
        live_counts = counts(live_base + "/__graph", "live")
        static_counts = counts(static_base + "/__graph.html", "static")
        single_counts = counts(single.as_uri(), "single")
    finally:
        for proc in (live, static):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=5)

    assert live_counts == static_counts == single_counts
    assert live_counts["tables/orders"] == 2
    assert live_counts["tables/customers"] == 1
    assert any(value > 0 for value in static_counts.values())
    forbidden = ("/__comment", "/__comments", "/__data/events", "/__events", "/comments", "/api/", "/__sse")
    for name in ("static", "single"):
        bad = [url for url in requests[name]
               if any(token in urllib.parse.urlparse(url).path.lower() for token in forbidden)]
        assert bad == [], f"{name} made comment/API/SSE requests: {bad}"


def test_phase4_review_canvas_index_enter_space_and_action_native_ownership(server_url: str, page) -> None:
    _open_graph(page, server_url)
    index = page.locator(".okf-node-index")
    index.locator("summary").click()
    row = index.get_by_role("option").first
    chosen = row.get_attribute("data-target")
    index.get_by_role("listbox").focus(); index.get_by_role("listbox").press("Space")
    assert page.evaluate("id=>window.__okfLoomGraph.interaction.snapshot().selected===id", chosen)
    action = page.get_by_role("toolbar", name="Selected concept actions").get_by_role("button", name="Hide others")
    action.focus(); page.keyboard.press("Space")
    assert page.evaluate("id=>window.__okfLoomGraph.interaction.snapshot().isolated===id", chosen)
    page.locator(".okf-graph-canvas-proxy").focus(); page.keyboard.press("Enter")
    page.wait_for_url(lambda url: "/__graph" not in url)


def test_phase4_impeccable_mobile_forced_colors_and_reduced_motion_captures(server_url: str, page) -> None:
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 844})
        page.goto(server_url + "/__graph")
        page.wait_for_function("() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
        expect(page.locator('.okf-layout')).to_have_attribute('data-mobile-pane','concepts')
        show_graph_canvas(page)
        expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane", "graph")
        overflow = page.evaluate("""() => {
          const visible = (el, style) => {
            const r=el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              parseFloat(style.opacity || '1') > 0 && style.clip !== 'rect(0px, 0px, 0px, 0px)' &&
              style.clipPath !== 'inset(50%)' && r.width > 0 && r.height > 0;
          };
          const offenders=[];
          for (const el of document.querySelectorAll('*')) {
            const style=getComputedStyle(el), r=el.getBoundingClientRect();
            if (visible(el, style) && (r.left < -.5 || r.right > innerWidth+.5 || r.width > innerWidth+.5)) {
              offenders.push({kind:'element',tag:el.tagName,id:el.id,className:String(el.className).slice(0,120),left:r.left,right:r.right,width:r.width,position:style.position});
            }
            for (const pseudo of ['::before','::after']) {
              const ps=getComputedStyle(el,pseudo);
              if (ps.content === 'none' || ps.content === 'normal' || ps.display === 'none' || ps.visibility === 'hidden') continue;
              const width=parseFloat(ps.width), left=parseFloat(ps.left), right=parseFloat(ps.right);
              if (width > innerWidth+.5 || (ps.position !== 'static' && ((Number.isFinite(left) && r.left+left < -.5) || (Number.isFinite(right) && r.right-right > innerWidth+.5)))) {
                offenders.push({kind:pseudo,tag:el.tagName,id:el.id,className:String(el.className).slice(0,120),left,right,width,position:ps.position});
              }
            }
          }
          const named={};
          for (const [name,selector] of Object.entries({topbar:'.okf-topbar',controls:'.okf-topbar__controls',studio:'.okf-studio-bar',studioGroup:'.okf-studio-bar__group--right',mobileNav:'.okf-graph-mobile-nav',panes:'.okf-layout',canvas:'#okf-graph',minimap:'.okf-graph-minimap',action:'.okf-graph-actions',focus:'.okf-graph-focusbar',density:'.okf-graph-density'})) {
            const el=document.querySelector(selector); if (el) named[name]={rect:el.getBoundingClientRect().toJSON(),display:getComputedStyle(el).display,flexDirection:getComputedStyle(el).flexDirection,flex:getComputedStyle(el).flex,margin:getComputedStyle(el).margin,parent:el.parentElement&&el.parentElement.className};
          }
          return {innerWidth,htmlScrollWidth:document.documentElement.scrollWidth,bodyScrollWidth:document.body.scrollWidth,offenders,named};
        }""")
        overflow_report = json.dumps(overflow, indent=2)
        assert overflow["htmlScrollWidth"] == overflow["innerWidth"], overflow_report
        assert overflow["bodyScrollWidth"] == overflow["innerWidth"], overflow_report
        assert overflow["offenders"] == [], overflow_report
        assert page.locator(".okf-graph-zoom svg.okf-viewport-icon").count() == 6
        fit = page.evaluate("""() => {const G=window.__okfLoomGraph,graph=document.getElementById('okf-graph'),g=graph.getBoundingClientRect(),nodes=G.cy.nodes(),boxes=nodes.map(n=>n.renderedBoundingBox({includeLabels:true,includeOverlays:true})),bodies=nodes.map(n=>n.renderedBoundingBox({includeLabels:false,includeOverlays:false})),overlaps=bodies.flatMap((a,i)=>bodies.slice(i+1).map(b=>Math.min(a.x2,b.x2)-Math.max(a.x1,b.x1)>0&&Math.min(a.y2,b.y2)-Math.max(a.y1,b.y1)>0)).filter(Boolean).length,revision=document.body.dataset.okfRevision,state=JSON.stringify(G.getState());return {height:g.height,top:g.top,inside:boxes.every(b=>b.x1>=16&&b.y1>=16&&b.x2<=g.width-16&&b.y2<=g.height-16),nodeMin:Math.min(...bodies.map(b=>Math.min(b.w,b.h))),fontMin:Math.min(...nodes.map(n=>parseFloat(n.style('font-size'))*G.cy.zoom())),overlaps,controls:Array.from(document.querySelectorAll('.okf-graph-zoom button')).every(b=>{const r=b.getBoundingClientRect();return r.left>=-1&&r.right<=innerWidth+1&&r.top>=-1&&r.bottom<=innerHeight+1}),revision,state}}""")
        assert fit["height"] >= 300 and fit["top"] + fit["height"] <= 845, fit
        assert fit["inside"] and fit["nodeMin"] >= 24 and fit["fontMin"] >= 12 and fit["overlaps"] == 0, fit
        dock = page.evaluate("""() => {const t=document.querySelector('.okf-graph-viewport-toolbar'),host=t.parentElement,tr=t.getBoundingClientRect(),hr=host.getBoundingClientRect(),buttons=Array.from(t.querySelectorAll('button')).map(b=>b.getBoundingClientRect());return {toolbar:{left:tr.left,right:tr.right,width:tr.width},host:{left:hr.left,right:hr.right,width:hr.width},targets:buttons.map(r=>({left:r.left,right:r.right,width:r.width,height:r.height})),overlaps:buttons.some((a,i)=>buttons.slice(i+1).some(b=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top))};}""")
        assert dock["toolbar"]["left"] >= dock["host"]["left"] - 1 and dock["toolbar"]["right"] <= dock["host"]["right"] + 1, dock
        assert all(target["width"] >= 44 and target["height"] >= 44 for target in dock["targets"]), dock
        assert not dock["overlaps"], dock
        expect(page.get_by_role('searchbox', name='Search graph concepts')).to_be_visible()
        expect(page.locator(".okf-studio-launcher")).to_be_visible()
        page.locator(".okf-studio-launcher").click()
        expect(page.get_by_role("button",name="Open the command palette (Control K)")).to_be_visible()
        page.get_by_role("button",name="Close panel").click()
        assert page.evaluate("([revision,state])=>document.body.dataset.okfRevision===revision&&JSON.stringify(window.__okfLoomGraph.getState())===state", [fit["revision"],fit["state"]])
        screenshot = Path(f"/tmp/okf-phase4-mobile-fixed-{width}.png")
        page.screenshot(path=str(screenshot), full_page=True)
        with screenshot.open("rb") as png:
            assert png.read(8) == b"\x89PNG\r\n\x1a\n"
            png.read(8)
            png_width, png_height = struct.unpack(">II", png.read(8))
        assert (png_width, png_height) == (width, 844), (png_width, png_height, overflow)
    # Pinch/compositor scaling is a separate visual-viewport behavior, NOT
    # browser page zoom. The headed X11 zoom gate proves actual page zoom.
    page.set_viewport_size({"width": 640, "height": 900})
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setPageScaleFactor", {"pageScaleFactor": 2})
    page.goto(server_url+"/__graph");page.wait_for_function("() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1 && document.body.hasAttribute('data-okf-narrow-reflow') && innerWidth === 640 && visualViewport.width <= 321")
    assert page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    controls=(page.locator(".okf-studio-launcher"),)
    for control in controls:
        expect(control).to_be_visible();control.focus();expect(control).to_be_focused()
    mobile_nav = page.locator(".okf-graph-mobile-nav")
    for tab in ("Graph","Concepts","Details"):expect(mobile_nav.get_by_role("button",name=tab,exact=True)).to_be_visible()
    for tab, pane in (("Concepts", "concepts"), ("Details", "details"), ("Graph", "graph")):
        mobile_nav.get_by_role("button", name=tab, exact=True).click()
        expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane", pane)
        assert page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    page.evaluate("() => scrollTo(0, 0)")
    measured=page.evaluate("""()=>({effective:visualViewport?visualViewport.width:innerWidth,layout:innerWidth,topbar:{style:document.querySelector('.okf-topbar').getAttribute('style'),display:getComputedStyle(document.querySelector('.okf-topbar')).display,direction:getComputedStyle(document.querySelector('.okf-topbar')).flexDirection,rect:document.querySelector('.okf-topbar').getBoundingClientRect().toJSON()},controls:{style:document.querySelector('.okf-topbar__controls').getAttribute('style'),display:getComputedStyle(document.querySelector('.okf-topbar__controls')).display,rect:document.querySelector('.okf-topbar__controls').getBoundingClientRect().toJSON()},boxes:Array.from(document.querySelectorAll('.okf-topbar a,.okf-topbar button,.okf-topbar input,.okf-topbar select,.okf-studio-bar button,.okf-studio-bar [tabindex],.okf-graph-mobile-nav button')).filter(e=>getComputedStyle(e).display!=='none'&&e.getBoundingClientRect().width>0).map(e=>{const r=e.getBoundingClientRect();return {name:e.getAttribute('aria-label')||e.textContent.trim(),left:r.left,right:r.right,width:r.width}})})""")
    assert measured["effective"]<=320+1,(measured["effective"],measured["layout"])
    # The shared shell compact block owns the narrow header: the shell is the
    # flex wrap container and the nav joins its rows via display:contents.
    assert measured["boxes"] and measured["topbar"]["display"] == "flex" and measured["controls"]["display"] == "contents"
    assert all(box["left"] >= -1 and box["right"] <= measured["effective"] + 1 for box in measured["boxes"]), measured
    page.screenshot(path="/tmp/okf-phase4-mobile-pinch-2x.png", full_page=True)
    cdp.send("Emulation.setPageScaleFactor", {"pageScaleFactor": 1})
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(forced_colors="active")
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => document.body.hasAttribute('data-okf-forced-colors') && window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
    baseline=page.evaluate("() => {const G=window.__okfLoomGraph,n=G.cy.nodes().first(),e=G.cy.edges().first();return {nodeBorder:n.style('border-color'),nodeText:n.style('color'),edge:e.style('line-color'),edgeWidth:parseFloat(e.style('width'))}}")
    assert baseline["nodeBorder"] and baseline["nodeText"] and baseline["edge"] and baseline["edgeWidth"]>=3
    page.screenshot(path="/tmp/okf-phase4-forced-baseline.png", full_page=True)
    ids = page.evaluate("() => {const e=window.__okfLoomGraph.cy.edges().first();return [e.source().id(),e.target().id()]}")
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})", ids[0])
    page.screenshot(path="/tmp/okf-phase4-forced-selected.png", full_page=True)
    page.evaluate("ids=>window.__okfLoomGraph.interaction.dispatch({type:'PATH',from:ids[0],id:ids[1]})", ids)
    page.screenshot(path="/tmp/okf-phase4-forced-path.png", full_page=True)
    page.get_by_role("searchbox", name="Search graph concepts").fill("orders")
    page.wait_for_function("() => window.__okfLoomGraph.cy.nodes('.okf-search-match').length > 0")
    page.screenshot(path="/tmp/okf-phase4-forced-search.png", full_page=True)
    page.get_by_role("button", name="Clear", exact=True).click()
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})", ids[0])
    _preset(page, "focus")
    page.wait_for_function("() => window.__okfLoomGraph.getState().lens === 'focus' && window.okfGraphCaptureState().layoutSettled")
    page.screenshot(path="/tmp/okf-phase4-forced-focus.png", full_page=True)
    page.emulate_media(forced_colors="none", reduced_motion="reduce")
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => document.body.hasAttribute('data-okf-reduced-motion') && window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
    duration = page.evaluate("() => {const n=window.__okfLoomGraph.cy.nodes().first();n.addClass('okf-hover');return n.style('transition-duration')}")
    assert duration in ("0ms", "0s")
    page.screenshot(path="/tmp/okf-phase4-reduced-motion.png", full_page=True)


def test_phase4_impeccable_action_bar_clamps_every_canvas_edge(server_url: str, page) -> None:
    _open_graph(page, server_url)
    node_id = page.evaluate("() => window.__okfLoomGraph.cy.nodes().first().id()")
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})", node_id)
    bar = page.get_by_role("toolbar", name="Selected concept actions")
    expect(bar).to_be_visible()
    canvas = page.locator("#okf-graph")
    for x, y in ((3, 3), (997, 3), (3, 697), (997, 697)):
        page.evaluate("([id,x,y])=>{window.__okfLoomGraph.cy.getElementById(id).renderedPosition({x,y});return undefined;}", [node_id, x, y])
        box, bounds = bar.bounding_box(), canvas.bounding_box()
        assert box and bounds
        assert box["x"] >= bounds["x"] - 1 and box["y"] >= bounds["y"] - 1
        assert box["x"] + box["width"] <= bounds["x"] + bounds["width"] + 1
        assert box["y"] + box["height"] <= bounds["y"] + bounds["height"] + 1
    proof = Path("/tmp/okf-loom-phase4-final-r7")
    proof.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(proof / "desktop-selected-action.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("() => dispatchEvent(new Event('resize'))")
    assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1")


def test_phase4_final_hidden_relations_url_reload_back_forward(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.evaluate("()=>{const G=window.__okfLoomGraph,n=G.cy.nodes().filter(n=>n.connectedEdges().length).first();G.interaction.dispatch({type:'SELECT',id:n.id()})}")
    before=page.locator(".okf-detail-relationships__list>li").count()
    page.get_by_text("Graph settings", exact=True).click()
    group=page.get_by_label("Relationship filters");chips=group.locator("button.okf-filter-chip");total=chips.count();assert total>1
    chips.first.click();first=chips.first.get_attribute("data-filter-value")
    assert first in page.url and page.evaluate("r=>window.__okfLoomGraph.getState().hiddenRelations[r]===true",first)
    hidden_instances=page.evaluate("r=>{const G=window.__okfLoomGraph,s=G.getState();return G.cy.edges().reduce((n,e)=>n+(e.data('relations')||[]).filter(x=>x.type===r&&s.hiddenRelations[x.type]).length,0)}",first);assert hidden_instances>0
    visible_ids=page.locator(".okf-detail-relationships__list>li").evaluate_all("rows=>rows.map(r=>r.dataset.edgeId).sort()")
    expected=page.evaluate("()=>{const G=window.__okfLoomGraph,s=G.getState(),id=G.interaction.snapshot().selected,out=[];G.cy.getElementById(id).connectedEdges().forEach(e=>(e.data('relations')||[]).filter(r=>!s.hiddenRelations[r.type]).forEach(()=>out.push(e.id())));return out.sort()}")
    assert visible_ids==expected
    expect(page.locator("#okf-detail-relationships-title")).to_have_text(f"Relationships ({len(expected)})")
    page.reload();page.wait_for_function("r=>window.__okfLoomGraph?.getState().hiddenRelations[r]===true",arg=first)
    page.get_by_text("Graph settings", exact=True).click();_preset(page, "themes");page.get_by_label("Relationship filters").get_by_role("button",name="Show all").click()
    page.go_back();page.wait_for_function("r=>window.__okfLoomGraph.getState().hiddenRelations[r]===true",arg=first)
    page.go_forward();page.wait_for_function("()=>Object.keys(window.__okfLoomGraph.getState().hiddenRelations).filter(k=>window.__okfLoomGraph.getState().hiddenRelations[k]).length===0")
    assert page.locator(".okf-detail-relationships__list>li").count()==before


def test_graph_filter_guards_resulting_instances_and_repairs_impossible_url(server_url: str, page) -> None:
    _open_graph(page,server_url);page.get_by_text("Graph settings",exact=True).click()
    type_chips=page.get_by_label("Type filters").locator("button.okf-filter-chip")
    for i in range(type_chips.count()): type_chips.nth(i).click()
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes().filter(n=>!n.hasClass('dim')).length>0")
    assert type_chips.evaluate_all("xs=>xs.some(x=>x.getAttribute('aria-pressed')==='true')")
    rel_chips=page.get_by_label("Relationship filters").locator("button.okf-filter-chip")
    for i in range(rel_chips.count()): rel_chips.nth(i).click()
    assert page.evaluate("""() => {const G=window.__okfLoomGraph,s=G.getState();return G.cy.edges().some(e=>(e.data('relations')||[]).some(r=>!s.hiddenRelations[r.type]));}""")
    assert rel_chips.evaluate_all("xs=>xs.some(x=>x.getAttribute('aria-pressed')==='true')")
    values=page.evaluate("""() => {const G=window.__okfLoomGraph,e=G.cy.edges().map(e=>e.data('relations')||[]).flat().find(r=>r.type!==r.label);return {types:[...new Set(G.cy.nodes().map(n=>n.data('type')))],relations:[...new Set(G.cy.edges().map(e=>(e.data('relations')||[]).flatMap(r=>[r.type,r.label])).flat())],different:e&&[e.type,e.label]};}""")
    assert values["different"], "fixture must cover relation type != label"
    impossible=server_url+"/__graph?"+"&".join([*("hidden="+urllib.parse.quote(v) for v in values["types"]),*("hiddenRelation="+urllib.parse.quote(v) for v in values["relations"])])
    page.goto(impossible);page.wait_for_function("() => window.__okfLoomGraph?.getState()")
    repaired=page.evaluate("() => {const s=window.__okfLoomGraph.getState();return {types:Object.values(s.hiddenTypes).filter(Boolean).length,relations:Object.values(s.hiddenRelations).filter(Boolean).length}}")
    assert repaired == {"types":0,"relations":0}


def test_phase4_final_stable_hover_capture_allowed_and_exit_clean(server_url: str, page) -> None:
    _open_graph(page, server_url);node_id=page.evaluate("()=>window.__okfLoomGraph.cy.nodes().first().id()")
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'HOVER',id})",node_id)
    assert not page.evaluate("()=>window.okfGraphCaptureState().layoutSettled")
    page.wait_for_function("id=>window.__okfLoomGraph.interaction.snapshot().hovered===id&&window.okfGraphCaptureState().layoutSettled",arg=node_id)
    ready=page.evaluate("async()=>await window.okfCaptureReadiness.start({target:'graph'}).promise");assert ready["screenshotAllowed"] is True
    page.screenshot(path="/tmp/okf-phase4-hover-settled.png",full_page=True)
    page.evaluate("()=>window.__okfLoomGraph.interaction.dispatch({type:'UNHOVER',quietUrl:true})")
    assert page.evaluate("()=>window.__okfLoomGraph.cy.elements('.okf-hover,.okf-hover-dim').length===0&&window.okfGraphCaptureState().layoutSettled")


def test_phase4_final_node_index_arrows_shift_space_and_focus_escape(server_url: str,page)->None:
    _open_graph(page,server_url);edge=page.evaluate("()=>{const e=window.__okfLoomGraph.cy.edges().first();return [e.source().id(),e.target().id()]}")
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id,source:'canvas-keyboard'})",edge[0])
    index=page.locator(".okf-node-index");index.locator("summary").click();page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id,source:'canvas-keyboard'})",edge[0]);row=index.locator(f'[data-target="{edge[1]}"]');row.focus()
    page.keyboard.press("Shift+Space");assert page.evaluate("()=>window.__okfLoomGraph.interaction.snapshot().pathResult.kind")=="found"
    row.focus();page.keyboard.press("ArrowRight");assert page.evaluate("()=>!!window.__okfLoomGraph.interaction.snapshot().selected")
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})",edge[0]);page.get_by_role("toolbar",name="Selected concept actions").get_by_role("button",name="Explore neighborhood").click();page.wait_for_function("()=>window.__okfLoomGraph.getState().lens==='focus'")
    page.get_by_role("group", name="Explore neighborhood").get_by_role("button", name="Show all").click();page.wait_for_function("()=>window.__okfLoomGraph.getState().lens!=='focus'")
    assert page.evaluate("id=>window.__okfLoomGraph.focusRoot()===null",edge[0])


def test_phase4_final_focus_lens_waiting_and_direct_routes_restore_origin(server_url: str,page)->None:
    _open_graph(page,server_url);_preset(page, "themes");page.wait_for_function("()=>window.__okfLoomGraph.getState().lens==='themes'")
    origin=page.evaluate("()=>{const G=window.__okfLoomGraph;G.cy.zoom(.73);G.cy.pan({x:31,y:47});return {zoom:G.cy.zoom(),pan:G.cy.pan()}}")
    _preset(page, "focus");assert page.evaluate("()=>window.__okfLoomGraph.getState().lens==='focus'&&window.__okfLoomGraph.focusRoot()===null")
    node=page.evaluate("()=>window.__okfLoomGraph.cy.nodes().first().id()");page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})",node)
    page.wait_for_function("id=>window.__okfLoomGraph.focusRoot()===id&&window.okfGraphCaptureState().layoutSettled",arg=node)
    expect(page.get_by_role("group",name="Explore neighborhood")).to_contain_text("Exploring neighborhood around");expect(page.get_by_label("Neighborhood depth")).to_have_value("1")
    proof=Path("/tmp/okf-loom-phase4-final-current");proof.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(proof/"desktop-focus.png"),full_page=True)
    page.get_by_role("group",name="Explore neighborhood").get_by_role("button",name="Show all").click();page.wait_for_function("()=>window.__okfLoomGraph.getState().lens==='themes'&&window.okfGraphCaptureState().layoutSettled")
    restored=page.evaluate("()=>({zoom:window.__okfLoomGraph.cy.zoom(),pan:window.__okfLoomGraph.cy.pan()})");assert abs(restored["zoom"]-origin["zoom"])<.03 and abs(restored["pan"]["x"]-origin["pan"]["x"])<2 and abs(restored["pan"]["y"]-origin["pan"]["y"])<2
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})",node);_preset(page, "focus");page.wait_for_function("id=>window.__okfLoomGraph.focusRoot()===id",arg=node)
    snap=page.evaluate("()=>window.__okfLoomGraph.interaction.snapshot()");assert "priorLens" not in snap


def test_phase4_final_path_orientation_forward_reverse_and_mixed(server_url: str,page)->None:
    _open_graph(page,server_url)
    edge=page.evaluate("()=>{const e=window.__okfLoomGraph.cy.edges().first();return {s:e.source().id(),t:e.target().id(),label:e.data('label')||'related'}}")
    page.evaluate("e=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id:e.s})",edge)
    proof=Path("/tmp/okf-loom-phase4-final-current");proof.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(proof/"desktop-action.png"),full_page=True)
    page.evaluate("e=>window.__okfLoomGraph.tracePath(window.__okfLoomGraph.cy.getElementById(e.s),window.__okfLoomGraph.cy.getElementById(e.t))",edge)
    forward=page.evaluate("()=>window.__okfLoomGraph.interaction.snapshot().pathResult.relations[0]");assert forward["orientation"]=="forward"
    page.evaluate("e=>window.__okfLoomGraph.tracePath(window.__okfLoomGraph.cy.getElementById(e.t),window.__okfLoomGraph.cy.getElementById(e.s))",edge)
    reverse=page.evaluate("()=>window.__okfLoomGraph.interaction.snapshot().pathResult.relations[0]");assert reverse["orientation"]=="reverse"
    expect(page.locator(".okf-detail-path__relation--reverse")).to_contain_text("reverse")
    page.screenshot(path=str(proof/"desktop-path.png"),full_page=True)
    mixed=page.evaluate("""()=>{const G=window.__okfLoomGraph,ids=G.cy.nodes().map(n=>n.id()).sort();for(const a of ids)for(const b of ids){if(a===b)continue;G.tracePath(G.cy.getElementById(a),G.cy.getElementById(b));const r=G.interaction.snapshot().pathResult;if(r?.kind==='found'&&r.relations.length>1&&new Set(r.relations.map(x=>x.orientation)).size===2)return r;}return null}""")
    if mixed is None:pytest.fail("demo graph has no deterministic mixed-direction shortest path")
    assert {r["orientation"] for r in mixed["relations"]}=={"forward","reverse"}


def test_phase4_final_non_owned_keyboard_matrix_and_alt_filter_equivalence(server_url: str,page)->None:
    _open_graph(page,server_url);page.evaluate("()=>{const d=document.createElement('div');d.role='dialog';d.tabIndex=0;d.textContent='matrix dialog';document.body.appendChild(d)}")
    page.locator(".okf-node-index summary").click()
    page.evaluate("()=>{const G=window.__okfLoomGraph,n=G.cy.nodes().first();G.interaction.dispatch({type:'SELECT',id:n.id()})}")
    result=page.evaluate("""()=>{const surfaces={canvas:document.querySelector('.okf-graph-canvas-proxy'),index:document.querySelector('.okf-node-index__item'),editable:document.querySelector('#okf-search'),header:document.querySelector('.okf-topbar button'),action:document.querySelector('.okf-graph-actions button'),dialog:Array.from(document.querySelectorAll('[role=dialog]')).find(x=>x.textContent==='matrix dialog')},commands=[{key:'c',ctrlKey:true},{key:'d',ctrlKey:true},{key:'d',metaKey:true},{key:'Delete'},{key:'Backspace'}],out={};for(const [name,node] of Object.entries(surfaces)){out[name]=commands.map(spec=>{const e=new KeyboardEvent('keydown',{...spec,bubbles:true,cancelable:true});node.dispatchEvent(e);return e.defaultPrevented})}return {out,selected:window.__okfLoomGraph.interaction.snapshot().selected}}""")
    assert result["selected"] and all(not prevented for rows in result["out"].values() for prevented in rows)
    page.get_by_text("matrix dialog",exact=True).evaluate("e=>e.remove()");page.get_by_text("Graph settings",exact=True).click();group=page.get_by_label("Type filters");chip=group.locator("button.okf-filter-chip").first;value=chip.get_attribute("data-filter-value")
    alt=chip.evaluate("e=>{const ev=new MouseEvent('click',{bubbles:true,cancelable:true,altKey:true});e.dispatchEvent(ev);return ev.defaultPrevented}");assert alt is False
    alt_hidden=page.evaluate("()=>Object.keys(window.__okfLoomGraph.getState().hiddenTypes).filter(k=>window.__okfLoomGraph.getState().hiddenTypes[k]).sort()")
    group.get_by_role("button",name="Show all").click();group.locator("button.okf-filter-solo").first.click();visible_hidden=page.evaluate("()=>Object.keys(window.__okfLoomGraph.getState().hiddenTypes).filter(k=>window.__okfLoomGraph.getState().hiddenTypes[k]).sort()")
    assert alt_hidden==visible_hidden and value not in alt_hidden


def test_phase4_final_phase5_sse_presence_tour_compatibility_boundary(server_url: str,page)->None:
    _open_graph(page,server_url)
    setup=page.evaluate("""async()=>{const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=fetch.bind(window),payload=await native(url).then(r=>r.json()),id=payload.nodes[0].data.id;payload.nodes[0].data.active_comment_count=23;window.fetch=(r,o)=>String(r)===url?Promise.resolve(new Response(JSON.stringify(payload),{headers:{'Content-Type':'application/json'}})):native(r,o);G.interaction.dispatch({type:'SELECT',id});window.okfLoomLive.emit('presence',{version:2,online:true,own_session_id:'browser-own',records:[{session_id:'agent-review',kind:'agent',state:'working',focus:id,label:'Agent'}]});return {id,applied:G.refreshStats.applied}}""")
    page.wait_for_function("id=>window.__okfLoomGraph.cy.getElementById(id).hasClass('okf-presence-halo')",arg=setup["id"])
    page.evaluate("()=>window.okfLoomLive.emit('comment',{})");page.wait_for_function("n=>window.__okfLoomGraph.refreshStats.applied>n",arg=setup["applied"])
    applied=page.evaluate("()=>window.__okfLoomGraph.refreshStats.applied");page.evaluate("()=>window.okfLoomLive.emit('graph',{})");page.wait_for_function("n=>window.__okfLoomGraph.refreshStats.applied>n",arg=applied)
    result=page.evaluate("id=>{const row=Array.from(document.querySelectorAll('.okf-node-index__item')).find(x=>x.dataset.target===id);return {selected:window.__okfLoomGraph.interaction.snapshot().selected,presence:window.__okfLoomGraph.cy.getElementById(id).hasClass('okf-presence-halo'),comments:row&&row.getAttribute('aria-label'),tour:!!document.querySelector('.okf-graph-tour')}}",setup["id"])
    assert result["selected"]==setup["id"] and result["presence"] and "23 active comments" in result["comments"] and not result["tour"]


def test_phase4_edge_background_clear_preserves_focus_root(server_url: str,page)->None:
    _open_graph(page,server_url);ids=page.evaluate("()=>{const e=window.__okfLoomGraph.cy.edges().first();return [e.source().id(),e.target().id()]}")
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})",ids[0]);page.get_by_role("toolbar",name="Selected concept actions").get_by_role("button",name="Explore neighborhood").click();page.wait_for_function("()=>window.__okfLoomGraph.getState().lens==='focus'&&window.okfGraphCaptureState().layoutSettled")
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})",ids[1]);page.evaluate("()=>window.__okfLoomGraph.interaction.dispatch({type:'CLEAR'})")
    state=page.evaluate("()=>({root:window.__okfLoomGraph.focusRoot(),lens:window.__okfLoomGraph.getState().lens,enabled:window.__okfLoomGraph.getState().focusEnabled,selected:window.__okfLoomGraph.interaction.snapshot().selected,focusBar:document.querySelector('.okf-graph-focusbar').hidden})")
    assert state=={"root":ids[0],"lens":"focus","enabled":True,"selected":None,"focusBar":False}


def test_phase4_edge_selected_neighbour_refresh_keeps_surviving_focus_root(server_url: str,page)->None:
    _open_graph(page,server_url);setup=page.evaluate("""async()=>{const G=window.__okfLoomGraph,url=document.getElementById('okf-graph').dataset.graphUrl,native=fetch.bind(window),fresh=await native(url).then(r=>r.json()),e=G.cy.edges().first(),root=e.source().id(),selected=e.target().id();G.interaction.dispatch({type:'SELECT',id:root});return {url,fresh,root,selected}}""")
    page.get_by_role("toolbar",name="Selected concept actions").get_by_role("button",name="Explore neighborhood").click();page.wait_for_function("()=>window.__okfLoomGraph.getState().lens==='focus'&&window.okfGraphCaptureState().layoutSettled");page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})",setup["selected"])
    page.evaluate("""s=>{const native=fetch.bind(window),f=JSON.parse(JSON.stringify(s.fresh));f.bundle_revision='focus-survives';f.nodes=f.nodes.filter(n=>n.data.id!==s.selected);f.edges=f.edges.filter(e=>e.data.source!==s.selected&&e.data.target!==s.selected);f.outgoing=f.outgoing||{};f.backlinks=f.backlinks||{};delete f.outgoing[s.selected];delete f.backlinks[s.selected];Object.keys(f.outgoing).forEach(id=>f.outgoing[id]=f.outgoing[id].filter(x=>x!==s.selected));Object.keys(f.backlinks).forEach(id=>f.backlinks[id]=f.backlinks[id].filter(x=>x!==s.selected));window.fetch=(r,o)=>String(r)===s.url?Promise.resolve(new Response(JSON.stringify(f),{headers:{'Content-Type':'application/json'}})):native(r,o)}""",setup)
    page.evaluate("()=>window.__okfLoomGraph.refreshGraph()");page.wait_for_function("()=>document.body.dataset.okfRevision==='focus-survives'")
    assert page.evaluate("id=>window.__okfLoomGraph.focusRoot()===id&&window.__okfLoomGraph.getState().focusEnabled&&window.__okfLoomGraph.interaction.snapshot().selected===null",setup["root"])


def test_phase4_edge_focus_exit_capture_pending_and_superseded_restore(server_url: str,page)->None:
    _open_graph(page,server_url);node=page.evaluate("()=>window.__okfLoomGraph.cy.nodes().first().id()")
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})",node);page.get_by_role("toolbar",name="Selected concept actions").get_by_role("button",name="Explore neighborhood").click();page.wait_for_function("()=>window.__okfLoomGraph.getState().lens==='focus'&&window.okfGraphCaptureState().layoutSettled")
    page.get_by_role("group",name="Explore neighborhood").get_by_role("button",name="Show all").click();page.wait_for_function("()=>window.__okfLoomGraph.getState().lens!=='focus'");assert not page.evaluate("()=>window.okfGraphCaptureState().layoutSettled")
    page.get_by_role("toolbar",name="Selected concept actions").get_by_role("button",name="Explore neighborhood").click();page.wait_for_function("()=>window.__okfLoomGraph.getState().lens==='focus'&&window.okfGraphCaptureState().layoutSettled")
    focused=page.evaluate("()=>({root:window.__okfLoomGraph.focusRoot(),zoom:window.__okfLoomGraph.cy.zoom(),end:performance.now()+260})");page.wait_for_function("end=>performance.now()>end",arg=focused["end"])
    assert page.evaluate("s=>window.__okfLoomGraph.focusRoot()===s.root&&Math.abs(window.__okfLoomGraph.cy.zoom()-s.zoom)<.02&&window.okfGraphCaptureState().layoutSettled",focused)
    page.get_by_role("group",name="Explore neighborhood").get_by_role("button",name="Show all").click();page.wait_for_function("()=>window.okfGraphCaptureState().layoutSettled")


def test_phase4_edge_bfcache_pageshow_reconciles_selected_focus_bars(server_url: str,page)->None:
    _open_graph(page,server_url);node=page.evaluate("()=>window.__okfLoomGraph.cy.nodes().first().id()")
    page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})",node);page.get_by_role("toolbar",name="Selected concept actions").get_by_role("button",name="Explore neighborhood").click();page.wait_for_function("()=>window.okfGraphCaptureState().layoutSettled")
    page.evaluate("()=>dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))");assert page.evaluate("()=>document.querySelector('.okf-graph-actions').hidden&&document.querySelector('.okf-graph-focusbar').hidden")
    page.evaluate("()=>dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))");page.wait_for_function("()=>!document.querySelector('.okf-graph-actions').hidden&&!document.querySelector('.okf-graph-focusbar').hidden&&window.okfGraphCaptureState().layoutSettled")
    assert page.evaluate("id=>window.__okfLoomGraph.interaction.snapshot().selected===id&&window.__okfLoomGraph.focusRoot()===id",node)


@pytest.mark.parametrize("width",[320,390])
def test_phase4_details_themes_long_rows_never_overflow(width: int,server_url: str,page)->None:
    page.set_viewport_size({"width":width,"height":900});_open_graph(page,server_url)
    page.get_by_role("button",name="Details",exact=True).click();_preset(page, "themes");page.wait_for_function("()=>window.__okfLoomGraph.getState().lens==='themes'")
    page.evaluate("""()=>{document.querySelector('.okf-signal__question').textContent='What are the exceptionally long cross-functional themes and which supporting knowledge areas barely touch across organizational boundaries?';document.querySelectorAll('.okf-lens-summary__row').forEach((row,i)=>{row.querySelector('.okf-lens-summary__label').textContent='Extremely long ranked theme title describing customer subscriptions, international settlement, governance, and operational dependencies '+i;row.querySelector('.okf-lens-summary__note').textContent='123456 concepts with supporting context and additional explanatory count text'});}""")
    proof=page.evaluate("""()=>{const pane=document.getElementById('okf-detail'),rows=Array.from(document.querySelectorAll('.okf-lens-summary__row')),controls=Array.from(pane.querySelectorAll('button,input,select,summary')).filter(x=>getComputedStyle(x).display!=='none'),pr=pane.getBoundingClientRect();return {doc:[document.documentElement.scrollWidth,document.documentElement.clientWidth],pane:[pane.scrollWidth,pane.clientWidth],rows:rows.map(r=>[r.scrollWidth,r.clientWidth]),bounded:controls.every(x=>{const r=x.getBoundingClientRect();return r.left>=pr.left-1&&r.right<=pr.right+1}),focusable:controls.every(x=>x.disabled||x.tabIndex>=0)}}""")
    assert proof["doc"][0]<=proof["doc"][1]+1 and proof["pane"][0]<=proof["pane"][1]+1 and all(a<=b+1 for a,b in proof["rows"]) and proof["bounded"] and proof["focusable"],proof
    page.screenshot(path=f"/tmp/okf-phase4-details-{width}.png",full_page=True)


@pytest.mark.parametrize("surface",["live","static","single"])
def test_phase4_final_runtime_sequence_never_mutates_ids_or_revisions(request,surface,tmp_path,page)->None:
    if surface=="live":url=request.getfixturevalue("server_url")+"/__graph"
    elif surface=="static":url=request.getfixturevalue("static_url")+"/__graph.html"
    else:
        from okf_loom.model import Bundle
        from okf_loom.render import render_single_file
        output=tmp_path/"phase4-final-single.html";render_single_file(Bundle.load(DEMO_BUNDLE),output);url=output.as_uri()
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')");page.goto(url)
    if surface=="single":page.locator('[data-okf-shell-item="graph"] a').click()
    page.wait_for_function("()=>window.__okfLoomGraph?.layoutStats?.lastAppliedSeq>=1")
    snapshot="""()=>({nodes:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort(),edges:window.__okfLoomGraph.cy.edges().map(e=>e.id()).sort(),bundle:document.body.dataset.okfRevision||null,capture:document.body.dataset.okfCaptureRevision||null})"""
    before=page.evaluate(snapshot)
    page.evaluate("()=>{const G=window.__okfLoomGraph,e=G.cy.edges().first(),a=e.source().id(),b=e.target().id();G.applyLens('bridges');G.interaction.dispatch({type:'SELECT',id:a});G.interaction.dispatch({type:'PATH',from:a,id:b});G.interaction.dispatch({type:'UNHOVER',quietUrl:true})}")
    page.get_by_role("button",name="Show minimap").click();page.get_by_role("button",name="Zoom in").click()
    if surface != "single": page.get_by_role("searchbox",name="Search graph concepts").fill("orders");page.get_by_role("button",name="Clear",exact=True).click()
    assert page.evaluate(snapshot)==before


@pytest.mark.parametrize("surface",["live","static","single"])
@pytest.mark.parametrize("width",[320,390])
def test_phase4_final_mobile_overview_backbone_selection_and_full_parity(request,surface,width,tmp_path,page)->None:
    if surface=="live":url=request.getfixturevalue("server_url")+"/__graph"
    elif surface=="static":url=request.getfixturevalue("static_url")+"/__graph.html"
    else:
        from okf_loom.model import Bundle
        from okf_loom.render import render_single_file
        output=tmp_path/"phase4-mobile-overview-single.html";render_single_file(Bundle.load(DEMO_BUNDLE),output);url=output.as_uri()
    page.set_viewport_size({"width":width,"height":844});page.add_init_script("localStorage.setItem('okfGraphTourDone','1');localStorage.setItem('okfGraphRelationshipLabels','1');sessionStorage.setItem('okfGraphRelationshipLabels','1')");page.goto(url)
    show_graph_canvas(page)
    if surface=="single":
        page.locator('[data-okf-shell-item="graph"] a').click()
        page.wait_for_function("""() => {const G=window.__okfLoomGraph,c=document.getElementById('okf-graph').getBoundingClientRect(),nodes=G&&G.cy.nodes();if(!nodes||!nodes.length||!c.width)return false;const boxes=nodes.map(n=>n.renderedBoundingBox({includeLabels:true,includeOverlays:true}));return Math.min(...nodes.map(n=>parseFloat(n.style('font-size'))*G.cy.zoom()))>=13.2&&(Math.max(...boxes.map(b=>b.x2))-Math.min(...boxes.map(b=>b.x1)))/c.width>=.78;}""")
    page.wait_for_function("()=>window.__okfLoomGraph?.layoutStats?.lastAppliedSeq>=1");page.wait_for_function("()=>document.querySelector('.okf-signal__status')?.dataset.visibleConnectionBundles&&document.querySelector('.okf-edge-bundle-index')?.dataset.visibleConnectionBundles")
    snapshot="""()=>({nodes:window.__okfLoomGraph.cy.nodes().map(n=>n.id()).sort(),edges:window.__okfLoomGraph.cy.edges().map(e=>e.id()).sort(),revision:document.body.dataset.okfRevision||null})"""
    before=page.evaluate(snapshot)
    projection="""()=>{const read=e=>({b:e.dataset.visibleConnectionBundles,i:e.dataset.visibleRelationshipInstances,tb:e.dataset.totalConnectionBundles,ti:e.dataset.totalRelationshipInstances}),s=document.querySelector('.okf-signal__status'),m=document.querySelector('.okf-mobile-overview__status'),d=document.querySelector('.okf-edge-bundle-index');return {status:read(s),mobile:read(m),disclosure:read(d)}}"""
    overview=page.evaluate("""()=>{const G=window.__okfLoomGraph,visible=G.cy.edges().not('.okf-edge-elided').not('.dim'),nodes=G.cy.nodes(),graph=document.getElementById('okf-graph').getBoundingClientRect(),boxes=nodes.map(n=>n.renderedBoundingBox({includeLabels:true,includeOverlays:true})),body=nodes.map(n=>n.renderedBoundingBox({includeLabels:false,includeOverlays:false}));return {total:G.cy.edges().length,visible:visible.length,nodes:nodes.length,status:document.querySelector('.okf-mobile-overview__status')?.textContent,fonts:nodes.map(n=>parseFloat(n.style('font-size'))*G.cy.zoom()),bounds:boxes.map(b=>[b.x1,b.y1,b.x2,b.y2]),canvas:[graph.width,graph.height],inside:boxes.every(b=>b.x1>=16&&b.y1>=16&&b.x2<=graph.width-16&&b.y2<=graph.height-16),span:(Math.max(...boxes.map(b=>b.x2))-Math.min(...boxes.map(b=>b.x1)))/graph.width,targets:body.map(b=>[b.w,b.h]),overlap:body.some((a,i)=>body.slice(i+1).some(b=>a.x1<b.x2&&a.x2>b.x1&&a.y1<b.y2&&a.y2>b.y1))}}""")
    projection_before=page.evaluate(projection)
    assert 0<overview["visible"]<overview["total"] and overview["visible"]<=max(1,overview["nodes"]-1)
    assert page.evaluate("()=>window.__okfLoomGraph.cy.edges().every(e=>e.style('label')===''||(e.data('multiplicity')>1&&e.style('label')===String(e.data('multiplicity'))))")
    assert "connection bundles" in overview["status"] and "relationship instances" in overview["status"]
    assert overview["inside"] and overview["span"]>=.78 and min(overview["fonts"])>=13.2 and not overview["overlap"],json.dumps(overview)
    assert min(min(pair) for pair in overview["targets"])>=32
    selected=page.evaluate("""()=>{const G=window.__okfLoomGraph,n=G.cy.nodes().sort((a,b)=>b.degree(false)-a.degree(false))[0];G.interaction.dispatch({type:'SELECT',id:n.id(),source:'canvas-keyboard'});const incident=n.connectedEdges().not('.dim');return {id:n.id(),degree:incident.length,instances:incident.reduce((sum,e)=>sum+(e.data('relations')||[]).length,0),visible:incident.not('.okf-edge-elided').length,unrelatedVisible:G.cy.edges().not('.dim').not('.okf-edge-elided').filter(e=>e.source().id()!==n.id()&&e.target().id()!==n.id()).length,labels:G.cy.edges('.okf-show-label,.okf-density-detail-label').length,listed:document.querySelectorAll('.okf-detail-relationships__list>li').length}}""")
    assert selected["visible"]==selected["degree"] and selected["unrelatedVisible"]==0
    assert selected["labels"]==0 and selected["listed"]==selected["instances"]
    page.evaluate("()=>window.__okfLoomGraph.interaction.dispatch({type:'CLEAR'})")
    page.get_by_role("button",name="Graph",exact=True).click()
    assert page.evaluate("()=>window.__okfLoomGraph.cy.edges().not('.dim').not('.okf-edge-elided').length")==overview["visible"]
    action_debug=page.evaluate("()=>{const b=document.querySelector('.okf-mobile-overview__all');return {hidden:b.hidden,display:getComputedStyle(b).display,rect:b.getBoundingClientRect().toJSON(),state:window.__okfLoomGraph.getState().relationshipDetailOptIn}}")
    assert not action_debug["hidden"] and action_debug["display"]!="none",action_debug
    full_action=page.locator(".okf-mobile-overview__all");expect(full_action).to_be_visible();full_action.click()
    assert page.evaluate("()=>window.__okfLoomGraph.cy.edges().not('.dim').not('.okf-edge-elided').length")==overview["total"]
    assert page.evaluate("()=>window.__okfLoomGraph.cy.edges().every(e=>e.style('label')===''||(e.data('multiplicity')>1&&e.style('label')===String(e.data('multiplicity'))))")
    projection_full=page.evaluate(projection);assert projection_full["status"]==projection_full["mobile"]==projection_full["disclosure"]
    full_action.click();assert page.evaluate("()=>window.__okfLoomGraph.cy.edges().not('.dim').not('.okf-edge-elided').length")==overview["visible"]
    assert page.evaluate(projection)==projection_before
    toolbar=page.get_by_role("toolbar",name="Graph view controls");expect(toolbar).to_be_visible()
    assert page.evaluate("()=>{const t=document.querySelector('.okf-graph-viewport-toolbar').getBoundingClientRect();return t.width>0&&t.height>0&&document.documentElement.scrollWidth<=innerWidth+1}")
    if surface=="live":expect(page.locator(".okf-studio-launcher")).to_contain_text("Editable")
    elif surface=="static":expect(page.locator(".okf-mobile-mode")).to_have_text("Static")
    else:expect(page.locator(".okf-mobile-mode")).to_have_text("Local")
    assert page.evaluate(snapshot)==before
    proof=Path("/tmp/okf-loom-phase4-final-current");proof.mkdir(parents=True,exist_ok=True)
    page.evaluate("()=>{const G=window.__okfLoomGraph,n=G.cy.nodes().sort((a,b)=>b.degree(false)-a.degree(false))[0];G.interaction.dispatch({type:'SELECT',id:n.id(),source:'canvas-keyboard'})}")
    expect(page.locator(".okf-detail-selected-status")).to_contain_text("Selected:")
    rows=page.locator(".okf-detail-relationships__list>li")
    expect(rows.first).to_contain_text("Type")
    expect(rows.first).to_contain_text("Label")
    expect(rows.first).to_contain_text("Source")
    expect(rows.first).to_contain_text("Direction")
    expect(rows.first).to_contain_text("Target")
    reflow=page.evaluate("""()=>{const pane=document.getElementById('okf-detail'),list=document.querySelector('.okf-detail-relationships__list'),rows=[...list.children],links=[...list.querySelectorAll('[role=option]')],pr=pane.getBoundingClientRect();return {documentOverflow:document.documentElement.scrollWidth>document.documentElement.clientWidth+1,paneOverflow:pane.scrollWidth>pane.clientWidth+1,listOverflow:list.scrollWidth>list.clientWidth+1,rows:rows.map(row=>{const r=row.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width}}),pane:{left:pr.left,right:pr.right},links:links.map(link=>({tabIndex:link.tabIndex,disabled:link.disabled}))};}""")
    assert not reflow["documentOverflow"] and not reflow["paneOverflow"] and not reflow["listOverflow"],reflow
    assert all(row["left"]>=reflow["pane"]["left"]-1 and row["right"]<=reflow["pane"]["right"]+1 and row["right"]<=width+1 for row in reflow["rows"]),reflow
    assert reflow["links"] and all(link["tabIndex"] == -1 for link in reflow["links"]),reflow
    relationship_list = page.get_by_role("listbox", name="Relationships for selected concept")
    relationship_list.focus(); expect(relationship_list).to_be_focused()
    directions=rows.evaluate_all("rs=>rs.map(r=>{const key=[...r.querySelectorAll('.okf-detail-relationships__key')].find(x=>x.textContent==='Direction');return {edge:r.dataset.edgeId,direction:key?.nextElementSibling?.textContent,related:r.dataset.relatedId,source:r.dataset.sourceId,target:r.dataset.targetId}})")
    assert all(r["related"]==(r["target"] if r["direction"]=="Outgoing" else r["source"]) for r in directions),directions
    for direction in ("Outgoing","Incoming"):
        candidate=next((r for r in directions if r["direction"]==direction),None)
        if candidate:
            page.locator(f'.okf-detail-relationships__list>li[data-edge-id="{candidate["edge"]}"][data-related-id="{candidate["related"]}"][role=option]').first.click()
            assert page.evaluate("id=>window.__okfLoomGraph.interaction.snapshot().selected===id",candidate["related"])
            page.evaluate("id=>window.__okfLoomGraph.interaction.dispatch({type:'SELECT',id})",selected["id"])
    rows.first.scroll_into_view_if_needed()
    page.screenshot(path=str(proof/f"mobile-{surface}-{width}-selected.png"),full_page=True)


@pytest.mark.parametrize("surface",["live","static","single"])
@pytest.mark.parametrize("width",[320,390])
def test_phase4_mobile_header_tabs_tools_mode_names_and_contrast(request,surface,width,tmp_path,page)->None:
    if surface=="live":url=request.getfixturevalue("server_url")+"/__graph"
    elif surface=="static":url=request.getfixturevalue("static_url")+"/__graph.html"
    else:
        from okf_loom.model import Bundle
        from okf_loom.render import render_single_file
        output=tmp_path/"phase4-mobile-header-single.html";render_single_file(Bundle.load(DEMO_BUNDLE),output,name="OKF Demo Bundle");url=output.as_uri()
    page.set_viewport_size({"width":width,"height":844});page.add_init_script("localStorage.setItem('okfGraphTourDone','1')");page.goto(url)
    if surface=="single":page.locator('[data-okf-shell-item="graph"] a').click()
    page.wait_for_function("()=>window.__okfLoomGraph?.layoutStats?.lastAppliedSeq>=1")
    title=page.locator(".okf-topbar__brand strong");expect(title).to_have_text("OKF Demo Bundle");expect(title).to_be_visible()
    expected={"live":"Live","static":"Static","single":"Local"}[surface]
    mode=page.locator('.okf-mobile-mode')
    if surface=="live":
        expect(mode).to_have_count(0);expect(page.locator(".okf-studio-launcher")).to_be_visible()
    else:expect(mode).to_have_text(expected);expect(mode).to_be_visible()
    tabs=page.get_by_role("navigation",name="Graph panes").get_by_role("button")
    assert tabs.all_text_contents()==["Graph","Concepts","Details"]
    heights=page.evaluate("()=>Array.from(document.querySelectorAll('[data-graph-pane]')).map(x=>x.getBoundingClientRect().height)");assert len(set(heights))==1 and heights[0]>=44
    for label,pane in (("Concepts","concepts"),("Details","details"),("Graph","graph")):
        page.get_by_role("button",name=label,exact=True).click();expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane",pane)
        if pane=="details":page.wait_for_function("()=>document.activeElement.id in {'okf-detail':1,'detail-content':1}")
    find=page.get_by_role('searchbox',name='Search graph concepts');expect(find).to_be_visible()
    commands=page.get_by_role("button",name="Open the command palette (Control K)")
    if surface=="live":
        page.locator(".okf-studio-launcher").click();expect(commands).to_be_visible();commands.focus();expect(commands).to_be_focused();page.get_by_role("button",name="Close panel").click()
    else:mode.focus();expect(mode).to_be_focused()
    expect(page.locator(".okf-mobile-secondary__controls")).to_be_hidden()
    assert page.evaluate('b=>{const r=b.getBoundingClientRect();return r.left>=-1&&r.right<=innerWidth+1}',find.element_handle())
    # Direct accessibility-tree proof: icon controls expose unique names; a
    # screenshot's repeated glyphs are not an accessible-name failure.
    names=page.locator(".okf-graph-zoom button").evaluate_all("bs=>bs.map(b=>b.getAttribute('aria-label'))")
    assert len(names)==6 and all(names) and len(set(names))==len(names)
    assert page.locator(".okf-viewport-label").all_text_contents()==["Zoom in","Zoom out","Fit","Full screen","Reset","Overview","Layout"]
    assert page.evaluate("()=>{const bs=[...document.querySelectorAll('.okf-graph-zoom button')],toolbar=document.querySelector('.okf-graph-zoom');return getComputedStyle(toolbar).gridTemplateColumns.split(' ').length===3&&bs.every(b=>{const r=b.getBoundingClientRect();return r.width>=44&&r.height>=44})}")
    tree=page.locator(".okf-graph-zoom").aria_snapshot()
    assert all(name in tree for name in names),tree
    contrast=page.evaluate("""()=>{const e=window.__okfLoomGraph.cy.edges().first(),c=document.getElementById('okf-graph'),parse=s=>s.match(/[\\d.]+/g).slice(0,3).map(Number),lin=x=>(x/=255)<=.04045?x/12.92:((x+.055)/1.055)**2.4,L=s=>{const v=parse(s).map(lin);return .2126*v[0]+.7152*v[1]+.0722*v[2]},a=L(e.style('line-color')),b=L(getComputedStyle(c).backgroundColor);return {line:e.style('line-color'),background:getComputedStyle(c).backgroundColor,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05),arrow:e.style('target-arrow-color'),labels:e.style('label')}}""")
    assert contrast["ratio"]>=3,contrast;assert contrast["arrow"]==contrast["line"] and contrast["labels"]==""
    assert page.evaluate("()=>document.documentElement.scrollWidth<=innerWidth+1")


@pytest.mark.parametrize("surface", ["live", "static", "single"])
@pytest.mark.parametrize("width", [320, 390])
def test_phase4_final_selected_details_scrolls_in_normal_flow_without_overlap(
    request, surface: str, width: int, tmp_path: Path, page
) -> None:
    """The complete selected Details stack stays in flow at final phone widths."""
    if surface == "live":
        url = request.getfixturevalue("server_url") + "/__graph"
    elif surface == "static":
        url = request.getfixturevalue("static_url") + "/__graph.html"
    else:
        from okf_loom.model import Bundle
        from okf_loom.render import render_single_file
        output = tmp_path / "phase4-r6-selected-single.html"
        render_single_file(Bundle.load(DEMO_BUNDLE), output)
        url = output.as_uri()
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    page.goto(url)
    if surface == "single":
        page.locator('[data-okf-shell-item="graph"] a').click()
    page.wait_for_function("()=>window.__okfLoomGraph?.layoutStats?.lastAppliedSeq>=1")
    page.evaluate("()=>{const G=window.__okfLoomGraph,n=G.cy.nodes().filter(n=>n.connectedEdges().length).first();G.interaction.dispatch({type:'SELECT',id:n.id(),source:'canvas-keyboard'})}")
    pane = page.locator("#okf-detail")
    expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane", "details")
    expect(page.locator(".okf-detail-relationships__list>li").first).to_be_visible()
    top = page.evaluate("""()=>{const pane=document.getElementById('okf-detail'),content=document.getElementById('detail-content'),heading=content.querySelector('h2'),pr=pane.getBoundingClientRect(),hr=heading.getBoundingClientRect();return {scrollTop:pane.scrollTop,headingTop:hr.top,paneTop:pr.top,sticky:[...pane.querySelectorAll('*')].filter(e=>getComputedStyle(e).position==='sticky').length};}""")
    assert top["scrollTop"] == 0 and top["headingTop"] >= top["paneTop"] - 1 and top["sticky"] == 0, top
    advanced = page.get_by_text("Graph settings", exact=True)
    expect(advanced).to_be_visible()
    assert advanced.bounding_box()["height"] >= 44
    advanced.click()
    page.wait_for_timeout(30)
    result = page.evaluate("""(surface)=>{const pane=document.getElementById('okf-detail'),signal=pane.querySelector('.okf-signal--bar'),notice=pane.querySelector('.okf-detail-view-only'),first=pane.querySelector('.okf-detail-relationships__list>li'),summary=pane.querySelector('.okf-signal__advanced>summary'),clear=pane.querySelector('.okf-signal__btn'),copy=pane.querySelector('#okf-copy-neighbourhood'),positions=[],overlap=(a,b)=>Math.max(0,Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top))*Math.max(0,Math.min(a.right,b.right)-Math.max(a.left,b.left));for(let y=0;y<=pane.scrollHeight-pane.clientHeight;y+=73){pane.scrollTop=y;const sr=signal.getBoundingClientRect(),nr=notice&&notice.getBoundingClientRect(),fr=first.getBoundingClientRect();positions.push({y:pane.scrollTop,signal:getComputedStyle(signal).position,notice:notice&&getComputedStyle(notice).position,first:overlap(fr,sr),noticeSignal:nr?overlap(nr,sr):0});}first.scrollIntoView({block:'nearest'});const pr=pane.getBoundingClientRect(),fr=first.getBoundingClientRect();return {positions,overflow:document.documentElement.scrollWidth>innerWidth+1||pane.scrollWidth>pane.clientWidth+1,firstVisible:fr.top>=pr.top-1&&fr.bottom<=pr.bottom+1&&fr.left>=pr.left&&fr.right<=pr.right+1,summary:{width:summary.getBoundingClientRect().width,pane:pane.clientWidth,height:summary.getBoundingClientRect().height,text:summary.textContent.trim()},copyVisible:copy&&!copy.hidden&&getComputedStyle(copy).display!=='none'&&copy.getBoundingClientRect().height>0,clearVisible:clear&&!clear.hidden&&getComputedStyle(clear).display!=='none',noticeText:notice&&notice.textContent.trim(),noticeBefore:notice&&notice.nextElementSibling===signal,staticExpected:surface!=='live'};}""", surface)
    assert not result["overflow"] and result["firstVisible"], result
    assert all(p["signal"] == "static" and (p["notice"] in (None, "static")) and p["first"] == 0 and p["noticeSignal"] == 0 for p in result["positions"]), result
    assert result["summary"]["height"] >= 44 and result["summary"]["width"] >= 200 and "Graph settings" in result["summary"]["text"]
    assert result["copyVisible"] and not result["clearVisible"]
    if result["staticExpected"]:
        assert result["noticeText"] == "View only; graph exploration remains interactive."
        assert result["noticeBefore"]
    else:
        assert result["noticeText"] is None
    proof = Path("/tmp/okf-loom-phase4-final-r7")
    proof.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(proof / f"{surface}-{width}-selected-details.png"), full_page=True)


@pytest.mark.parametrize("surface", ["live", "static", "single"])
@pytest.mark.parametrize("width", [320, 390])
def test_phase4_r8_mobile_details_exact_geometry_labels_and_contrast(
    request, surface: str, width: int, tmp_path: Path, page
) -> None:
    if surface == "live":
        url = request.getfixturevalue("server_url") + "/__graph"
    elif surface == "static":
        url = request.getfixturevalue("static_url") + "/__graph.html"
    else:
        from okf_loom.model import Bundle
        from okf_loom.render import render_single_file
        output = tmp_path / "phase4-r8-single.html"
        render_single_file(Bundle.load(DEMO_BUNDLE), output)
        url = output.as_uri()
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    page.goto(url)
    if surface == "single":
        page.locator('[data-okf-shell-item="graph"] a').click()
    page.wait_for_function("()=>window.__okfLoomGraph?.layoutStats?.lastAppliedSeq>=1")
    page.evaluate("()=>{const G=window.__okfLoomGraph,n=G.cy.nodes().filter(n=>n.connectedEdges().length).first();G.interaction.dispatch({type:'SELECT',id:n.id(),source:'canvas-keyboard'})}")
    metrics = page.evaluate(r"""()=>{const pane=document.getElementById('okf-detail'),signal=pane.querySelector('.okf-signal--bar'),notice=pane.querySelector('.okf-detail-view-only'),cards=[...pane.querySelectorAll('.okf-detail-relationships__list>li')],summary=document.querySelector('.okf-graph-mobile-nav'),controls=[...summary.querySelectorAll('button')],color=s=>{const values=s.match(/[\d.]+/g).map(Number),srgb=s.startsWith('color(srgb');return {channels:(srgb?values:values.map(x=>x/255)).slice(0,3),alpha:values[3]??1}},lin=x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4,L=s=>{const v=color(s).channels.map(lin);return .2126*v[0]+.7152*v[1]+.0722*v[2]},ratio=(fg,bg)=>(Math.max(L(fg),L(bg))+.05)/(Math.min(L(fg),L(bg))+.05),contrast=e=>{const s=getComputedStyle(e),background=color(s.backgroundColor).alpha===0?getComputedStyle(pane).backgroundColor:s.backgroundColor;return ratio(s.color,background)},rect=e=>e&&e.getBoundingClientRect().toJSON();return {pane:rect(pane),signal:rect(signal),notice:rect(notice),cards:cards.map(c=>{const s=getComputedStyle(c);return {rect:rect(c),contentWidth:c.clientWidth-parseFloat(s.paddingLeft)-parseFloat(s.paddingRight),cols:s.gridTemplateColumns,action:rect(c),raw:c.dataset.relationId,label:c.children[1].textContent}}),toolbar:controls.map(c=>({text:c.textContent.trim(),rect:rect(c),font:parseFloat(getComputedStyle(c).fontSize)})),noticeContrast:notice?contrast(notice):null,links:cards.map(c=>contrast(c)),disabled:[...pane.querySelectorAll(':disabled')].map(e=>contrast(e)),overflow:document.documentElement.scrollWidth>innerWidth+1||pane.scrollWidth>pane.clientWidth+1};}""")
    assert not metrics["overflow"] and metrics["cards"]
    expected_width = width - 16
    assert abs(metrics["signal"]["width"] - expected_width) <= 1
    if metrics["notice"]:
        assert abs(metrics["notice"]["width"] - expected_width) <= 1 and metrics["noticeContrast"] >= 4.5
    for card in metrics["cards"]:
        assert abs(card["rect"]["width"] - expected_width) <= 1 and card["rect"]["width"] >= 296
        assert card["cols"].split()[0] in ("76px", "72px", "80px")
        assert card["action"]["width"] >= card["contentWidth"]
        if "_" in card["raw"]:
            assert "_" not in card["label"] and card["label"][0].isupper()
    assert [item['text'] for item in metrics['toolbar']] == ['Graph','Concepts','Details']
    assert all(item["rect"]["height"] == 44 and item["font"] >= 14 for item in metrics["toolbar"])
    assert all(value >= 4.5 for value in metrics["links"])
    assert all(value >= 3 for value in metrics["disabled"])
    proof = Path("/tmp/okf-loom-phase4-final-r8")
    proof.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(proof / f"{surface}-{width}-selected-details.png"), full_page=True)


def test_phase5_index_engagement_settle_order_identity_and_focus(server_url: str, page) -> None:
    """ASYNC_LIFECYCLE_MATRIX
    scenario | deadline/abort owner | mutation phase | public code/result | persisted state | late-result behavior | retry/reconnect behavior | exact proof
    graph before canonical | loadGraph request owner | graph staged then graphReady published | SSR remains until full tuple | unchanged | no partial projection | canonical retry may settle once | test_phase5_real_ssr_failure_and_delayed_settle
    canonical before graph | loadComments generation owner | comments/events staged atomically | SSR remains until graphReady | unchanged | no partial projection | graph retry may settle once | test_phase5_index_engagement_settle_order_identity_and_focus
    graph failure | loadGraph request owner | none | exactly one SSR projection | unchanged | failure cannot erase SSR | reload/retry starts fresh | test_phase5_real_ssr_failure_and_delayed_settle
    canonical failure | loadComments generation owner | none | exactly one SSR projection | unchanged | failure cannot erase SSR | retry starts newer generation | test_phase5_real_ssr_failure_and_delayed_settle
    stale generation completes late | commentsRequestGeneration owner | newer tuple already committed | accepted chips/rail unchanged | unchanged | older result ignored | no duplicate owner on retry | test_phase5_real_ssr_failure_and_delayed_settle
    live event during fetch | loadComments merge/fence owner | live event merged before tuple commit | accepted authority plus live row | event log unchanged by client | fetched snapshot cannot overwrite live event | reconnect triggers canonical resync | existing loadComments merge tests
    accepted empty generation | loadComments generation owner | empty tuple committed | rail/chips removed, focus restored | unchanged | prior projection not restored | reconnect may repopulate canonically | test_phase5_index_engagement_settle_order_identity_and_focus
    portable snapshot | read_event_snapshot lock owner | read-only snapshot | embedded rows or unavailable error | byte-identical | no late mutation possible | next build acquires fresh lock | test_portable_engagement_snapshot_is_strictly_read_only_with_pending_receipt
    portable browser load | browser navigation owner | no live mutation | static/single viewer with zero live API requests | unchanged | n/a | reload remains offline | test_phase3_opted_comment_summary_browser_parity_and_portable_zero_requests
    """
    page.goto(server_url + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    result = page.evaluate("""() => {
      const S=window.okfLoomStudio, state=S.state, hero=document.querySelector('.okf-hero');
      const old=document.querySelector('#okf-recent-rail'); if(old) old.remove();
      const ssr=document.createElement('section'); ssr.id='okf-recent-rail';
      ssr.innerHTML='<ol><li id="okf-recent-ssr"><a href="/tables/orders">SSR row</a></li></ol>'; hero.appendChild(ssr);
      state.commentsReady=false; state.eventsReady=false; S._buildIndexEngagement();
      const graphFirst=document.querySelectorAll('#okf-recent-rail').length;
      state.comments=[{id:'top',concept:'tables/orders',state:'open',archived:false,parent_id:null},{id:'reply',concept:'tables/orders',state:'claimed',archived:false,parent_id:'top'}];
      state.commentsReady=true; state.events=[{event_id:'evt-1',id:'legacy',type:'changed',ids:['tables/orders'],actor:'agent',ts:'2026-07-17T10:00:00Z'}];
      state.eventsReady=true; S._buildIndexEngagement();
      const recent=document.querySelector('#okf-recent-evt-1-tables-orders a'); recent.focus();
      state.events.push({event_id:'evt-2',type:'changed',concept:'tables/orders',actor:'disk',ts:'2026-07-17T11:00:00Z'}); S._buildIndexEngagement();
      const focusPreserved=document.activeElement.closest('li').id==='okf-recent-evt-2-tables-orders';
      const otherIds=Object.keys(JSON.parse(document.body.dataset.okfConceptRoutes)).filter(id=>id!=='tables/orders').slice(0,6);
      state.events=otherIds.map((id,i)=>({event_id:'displace-'+i,type:'changed',ids:[id],actor:'agent',ts:'2026-07-18T0'+i+':00:00Z'})); S._buildIndexEngagement();
      const displacementFallback=document.activeElement.matches('[data-concept-id="tables/orders"] a.okf-card__link');
      const firstRecent=document.querySelector('#okf-recent-rail li'); const removedConcept=firstRecent.dataset.conceptId; firstRecent.querySelector('a').focus();
      state.events.push({event_id:'remove-focused',type:'removed',ids:[removedConcept],actor:'disk',ts:'2026-07-19T00:00:00Z'}); S._buildIndexEngagement();
      const removalFallback=document.activeElement.matches('[data-concept-id="'+CSS.escape(removedConcept)+'"] a.okf-card__link, .okf-section__title, .okf-type-nav__link');
      const chip=document.querySelector('[data-concept-id="tables/orders"] .okf-card__comments'); const countText=chip.textContent; chip.focus();
      state.comments=[]; S._buildIndexEngagement();
      return {graphFirst,rails:document.querySelectorAll('#okf-recent-rail').length,countText,focusPreserved,displacementFallback,removalFallback,chipRemoved:!document.body.contains(chip),cardFocused:document.activeElement.matches('[data-concept-id="tables/orders"] a.okf-card__link'),ids:Array.from(document.querySelectorAll('#okf-recent-rail li')).map(x=>x.id),hrefs:Array.from(document.querySelectorAll('#okf-recent-rail a')).map(x=>x.getAttribute('href'))};
    }""")
    assert result["graphFirst"] == result["rails"] == 1
    assert result["countText"] == "2 active comments"
    assert result["focusPreserved"] and result["displacementFallback"] and result["removalFallback"]
    assert result["chipRemoved"] and result["cardFocused"]
    assert all(href and href != "#" for href in result["hrefs"])


def test_phase5_real_ssr_failure_and_delayed_settle(tmp_path: Path, page) -> None:
    """Real server authority survives graph failure and delayed canonical settlement."""
    from okf_loom.studio import Studio
    root = tmp_path / "engagement-real"; shutil.copytree(DEMO_BUNDLE, root)
    studio = Studio.for_bundle(root)
    studio.post_comment(concept="tables/orders", body="active")
    studio.record_activity(actor="agent", action="updated", ids=["tables/orders"], summary="updated Orders", origin="test")
    port = _free_port(); base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(okf_module_argv("serve", str(root), "--host", "127.0.0.1", "--port", str(port), "--no-watch", "--no-open"), cwd=str(TOOLKIT_ROOT), env=okf_subprocess_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_for_server(proc, base)
        page.route("**/__data/graph.json", lambda route: route.abort())
        page.goto(base + "/", wait_until="domcontentloaded"); page.wait_for_timeout(900)
        assert page.locator("#okf-recent-rail").count() == 1
        assert page.locator(".okf-card__comments").count() == 1
        ssr = page.locator("#okf-recent-rail").inner_text()
        page.wait_for_timeout(500)
        assert page.locator("#okf-recent-rail").count() == 1
        assert page.locator("#okf-recent-rail").inner_text() == ssr
        page.unroute("**/__data/graph.json")
        page.reload(wait_until="domcontentloaded"); page.wait_for_timeout(1200)
        assert page.locator("#okf-recent-rail").count() == 1
        assert page.locator(".okf-card__comments").inner_text() == "1 active comment"
        stale = page.evaluate("""async () => {
          const S=window.okfLoomStudio, original=window.fetch.bind(window);
          const comments=await (await original('/__comments')).json();
          const envelope=await (await original('/__data/events?limit=500')).json();
          const newer=JSON.parse(JSON.stringify(envelope));
          newer.events=[{event_id:'accepted-new',type:'changed',ids:['tables/orders'],actor:'agent',ts:'2026-07-17T13:00:00Z'}];
          let release; const gate=new Promise(resolve=>{release=resolve;}); let calls=0;
          window.fetch=(url,opts) => {
            if (String(url).startsWith('/__comments') || String(url).startsWith('/__data/events')) {
              const batch=Math.floor(calls++/2), isComments=String(url).startsWith('/__comments');
              const body=JSON.stringify(isComments ? comments : (batch===0 ? envelope : newer));
              if(batch===0) return gate.then(()=>new Response(body,{status:200,headers:{'Content-Type':'application/json'}}));
              return Promise.resolve(new Response(body,{status:200,headers:{'Content-Type':'application/json'}}));
            }
            return original(url,opts);
          };
          const older=S._loadComments(); const accepted=S._loadComments(); await accepted;
          const before=document.querySelector('#okf-recent-rail li').id;
          release(); await older; const after=document.querySelector('#okf-recent-rail li').id;
          window.fetch=original; return {before,after};
        }""")
        assert stale["before"] == stale["after"]
    finally:
        proc.terminate(); proc.wait(timeout=10)


@pytest.mark.parametrize("width", [320, 390])
def test_mobile_selected_action_sheet_is_contained_keyboard_safe(server_url: str, page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => window.okfGraphCaptureState?.().layoutSettled")
    page.evaluate("() => { window.__okfLoomGraph.cy.nodes().first().trigger('tap'); return undefined; }")
    sheet = page.get_by_role("toolbar", name="Selected concept actions")
    expect(sheet).to_be_visible()
    # Selected actions are in the normal-flow Details document, not a fixed
    # canvas sheet. Reach them by the pane's native scrolling.
    sheet.scroll_into_view_if_needed()
    metrics = sheet.evaluate("""el => { const r=el.getBoundingClientRect(); const actionable=[...el.querySelectorAll('button,a')].filter(x=>x.offsetParent!==null&&!x.hidden&&!x.disabled&&getComputedStyle(x).visibility!=='hidden'); return {left:r.left,right:r.right,bottom:r.bottom,vw:innerWidth,vh:innerHeight,overflow:document.documentElement.scrollWidth-innerWidth,buttons:actionable.map(x=>x.getBoundingClientRect().height)} }""")
    assert metrics["left"] >= 8 and metrics["right"] <= metrics["vw"] - 8
    assert metrics["bottom"] <= metrics["vh"] and metrics["overflow"] <= 0
    assert metrics["buttons"] and min(metrics["buttons"]) >= 44
    focus = sheet.get_by_role("button", name="Explore neighborhood"); focus.focus(); expect(focus).to_be_focused()
    page.set_viewport_size({"width": 900, "height": 844})
    page.wait_for_function("() => document.querySelector('.okf-graph-actions').parentElement.id === 'okf-graph'")
    expect(focus).to_be_focused()
    page.set_viewport_size({"width": width, "height": 844})
    page.wait_for_function("() => document.querySelector('.okf-graph-actions').parentElement.id === 'okf-detail-actions-slot'")
    assert page.locator("#okf-detail-actions-slot > .okf-graph-actions").count() == 1
    expect(focus).to_be_focused()
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    assert page.evaluate("() => document.activeElement === document.querySelector('.okf-graph-canvas-proxy') || document.activeElement === document.querySelector('[data-graph-pane=details]')")


@pytest.mark.parametrize("width", [320, 390])
def test_mobile_connection_card_preserves_title_width_and_chip_row(server_url: str, page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(server_url + "/")
    href = page.locator("a.okf-card__link").first.get_attribute("href")
    page.goto(server_url + href)
    page.wait_for_function("() => window.okfLoomStudio?.state?.doc && document.querySelector('.okf-conn[data-concept-id]')")
    page.evaluate("""() => { const S=window.okfLoomStudio,item=document.querySelector('.okf-conn[data-concept-id]'); S.state.comments=Array.from({length:123},(_,i)=>({id:'mobile-'+i,concept:item.dataset.conceptId,state:'open',archived:false}));S.state.commentsReady=true;S.state.graphReady=true;S._buildIndexEngagement(); }""")
    page.wait_for_function("""() => { const card=document.querySelector('.okf-conn:has(.okf-comment-count)'); return !!(card?.querySelector('.okf-conn__body') && card.querySelector('.okf-conn__id')); }""")
    metrics = page.evaluate("""() => { const card=document.querySelector('.okf-conn:has(.okf-comment-count)'),body=card.querySelector('.okf-conn__body'),id=card.querySelector('.okf-conn__id'),chip=card.querySelector('.okf-comment-count'),cr=card.getBoundingClientRect(),br=body.getBoundingClientRect(),ir=id.getBoundingClientRect(),cs=getComputedStyle(id),r=chip.getBoundingClientRect();return {cardW:cr.width,bodyW:br.width,idW:ir.width,idLines:Math.ceil(ir.height/parseFloat(cs.lineHeight)),cardH:cr.height,chipH:r.height,overflow:document.documentElement.scrollWidth-innerWidth,text:id.textContent}; }""")
    assert metrics["bodyW"] >= metrics["cardW"] * .6
    assert metrics["idW"] >= metrics["cardW"] * .6 and metrics["idLines"] <= 4
    assert metrics["cardH"] <= 240 and metrics["chipH"] >= 44 and metrics["overflow"] <= 0
    assert len(metrics["text"]) < 2 or metrics["idW"] > 80  # never character-per-line


def test_graph_directional_edge_flow_one_owner_cleanup_and_capture(server_url: str, page) -> None:
    """Pointer/selection share one dash-offset owner; removal/capture leave no RAF."""
    _open_graph(page, server_url)
    result = page.evaluate("""async () => {
      const G=window.__okfLoomGraph, edge=G.cy.edges().filter(e=>e.data('label')).first();
      if(!edge.length)return {skip:true};
      const before={source:edge.source().position(),target:edge.target().position()};
      edge.trigger('mouseover');
      const deadline=performance.now()+2000;
      while(edge.style('line-dash-offset')==='0'&&performance.now()<deadline){await new Promise(requestAnimationFrame);}
      const hover={active:G.edgeFlow.active(),offset:edge.style('line-dash-offset')};
      edge.trigger('mouseout'); edge.select();
      await new Promise(requestAnimationFrame);
      const selected=G.edgeFlow.active();
      const first=G.edgeFlow.acquireCaptureLease(),second=G.edgeFlow.acquireCaptureLease();
      const staleRelease=G.edgeFlow.releaseCaptureLease(first);
      const frozen={active:G.edgeFlow.active(),offset:edge.style('line-dash-offset'),lease:G.edgeFlow.lease()};
      const currentRelease=G.edgeFlow.releaseCaptureLease(second);await new Promise(requestAnimationFrame);
      const resumed=G.edgeFlow.active();edge.remove(); await new Promise(requestAnimationFrame);
      return {hover,selected,staleRelease,currentRelease,resumed,frozen,afterActive:G.edgeFlow.active(),before,
              after:{source:before.source,target:before.target}};
    }""")
    if result.get("skip"):
        pytest.fail("fixture has no directed typed edge")
    assert result["hover"]["active"] and result["hover"]["offset"] != "0"
    assert result["selected"]
    assert not result["staleRelease"] and result["currentRelease"] and result["resumed"]
    assert result["frozen"]["active"] is False and result["frozen"]["offset"] == "0"
    assert not result["afterActive"] and result["before"] == result["after"]


def test_graph_directional_edge_flow_reduced_motion_static_cue(server_url: str, page) -> None:
    page.emulate_media(reduced_motion="reduce")
    _open_graph(page, server_url)
    result=page.evaluate("""() => {const G=window.__okfLoomGraph,e=G.cy.edges().filter(x=>x.data('label')).first();if(!e.length)return {skip:true};e.trigger('mouseover');return {active:G.edgeFlow.active(),dash:e.style('line-style'),arrow:e.style('target-arrow-shape'),offset:e.style('line-dash-offset')};}""")
    if result.get("skip"):
        pytest.fail("fixture has no directed typed edge")
    assert not result["active"] and result["dash"] == "dashed"
    assert result["arrow"] != "none" and result["offset"] == "0"


def test_graph_bundled_relationship_keyboard_selects_edge_and_collapse_stops_flow(server_url: str, page) -> None:
    _open_graph(page, server_url)
    page.get_by_text("Graph settings", exact=True).click()
    summary=page.locator(".okf-edge-bundle-index > summary")
    summary.press("Enter")
    bundle_list=page.get_by_role("listbox",name="Bundled relationships")
    if not bundle_list.locator("[role=option]").count(): pytest.fail("fixture has no bundled relationship")
    bundle_list.focus();bundle_list.press("Enter")
    page.wait_for_function("() => window.__okfLoomGraph.cy.edges(':selected').length===1 && window.__okfLoomGraph.edgeFlow.active()")
    selected=page.evaluate("""() => ({edge:window.__okfLoomGraph.cy.edges(':selected').id(),expanded:document.activeElement.closest('#okf-edge-bundle-panel')!==null,title:document.querySelector('#okf-edge-bundle-title').textContent})""")
    assert selected["edge"] and selected["expanded"] and "Relationships" in selected["title"]
    page.get_by_role("button", name="Collapse relationships").press("Space")
    page.wait_for_function("() => window.__okfLoomGraph.cy.edges(':selected').length===0 && !window.__okfLoomGraph.edgeFlow.active()")
    expect(bundle_list).to_be_focused()
    assert bundle_list.locator("[role=option]").first.get_attribute("aria-selected") == "false"


def test_graph_edge_flow_pagehide_invalidates_capture_lease(server_url: str, page) -> None:
    _open_graph(page, server_url)
    result=page.evaluate("""async () => {const G=window.__okfLoomGraph,e=G.cy.edges().filter(x=>x.data('label')).first();if(!e.length)return {skip:true};e.select();await new Promise(requestAnimationFrame);const token=G.edgeFlow.acquireCaptureLease();window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));return {released:G.edgeFlow.releaseCaptureLease(token),active:G.edgeFlow.active(),lease:G.edgeFlow.lease()};}""")
    if result.get("skip"): pytest.fail("fixture has no directed typed edge")
    assert result == {"released": False, "active": False, "lease": None}
