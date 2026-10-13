"""X1 browser proofs: ``--assets offline`` renders with the network cut off.

Every request that is not ``file:``/``data:``/``blob:`` or to the local test
server is aborted through Playwright routing, and every request is logged. A
page passes only with zero non-local requests, zero CSP violations and:
Mermaid SVGs (flowchart + gantt), KaTeX math with its font faces loaded,
highlight.js tokens for a common and a non-common language with the full
language set of the CDN build, a theme swap without a request, and the graph
(Cytoscape 3.34.3 + fCoSE + dagre) rendering.

Parity is proved against a checked-in golden of the DEFAULT CDN build's
render of the same fixture (``tests/fixtures/offline_parity/``, provenance
inside; regenerate with ``tests/regenerate_offline_cdn_golden.py``, which is
the only network use): Mermaid SVG element/class/role/text structure,
highlight.js token sequences and KaTeX MathML must be equal.

CSP violations are collected OUTSIDE the page (console messages plus a
``securitypolicyviolation`` listener reporting through an exposed binding),
so page script cannot clear them and navigation cannot lose them; they are
asserted before every navigation and at the end. Missing and corrupt local
assets must fail with ``offline-asset-unavailable``, keep the authored source
visible and still make no CDN request.

Matrix: hosted static, SPA and single-file; ``file://`` static and
single-file. Known limits (documented, not fixed by X1): SPA uses
root-absolute paths so it never worked from ``file://``; a ``file://`` static
graph page cannot fetch ``__data/graph.json``, so there only library loading
and zero network are proved.
"""
from __future__ import annotations

import json
import re
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from browser_support import expect, free_port, launch_chromium, managed_server, sync_playwright
from okf_loom.model import Bundle
from okf_loom.render import build_site
from okf_loom.viewer import vendor
from test_offline_assets import write_offline_fixture

pytestmark = pytest.mark.browser

GRAPH_LIBRARIES = """() => ({
  cytoscape: window.cytoscape && window.cytoscape.version,
  layoutBase: window.layoutBase != null, coseBase: window.coseBase != null,
  fcose: typeof window.cytoscapeFcose, dagre: window.dagre != null,
  cytoscapeDagre: typeof window.cytoscapeDagre,
})"""
LIBRARIES_LOADED = {"cytoscape": "3.34.3", "layoutBase": True, "coseBase": True,
                    "fcose": "function", "dagre": True, "cytoscapeDagre": "function"}
# Reports every violation to the test process at once; the reference is taken
# before any page script runs, so the page cannot redirect it.
CSP_REPORTER = """(() => {
  const report = window.__okfReportCsp;
  document.addEventListener('securitypolicyviolation', (e) => (report || window.__okfReportCsp)(
    [e.effectiveDirective || e.violatedDirective, e.blockedURI || '(inline)',
     (e.sourceFile || '') + ':' + e.lineNumber].join(' ')), true);
})();"""
CDN_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "offline_parity" / "cdn_render_golden.json"
# The normalised render of one scope: Mermaid SVG structure (tags, ids with
# the render counter normalised, classes, roles, text; <style> text and all
# geometry excluded), highlight.js token class/text sequences, KaTeX MathML.
RENDER_SNAPSHOT = r"""(scope) => {
  const root = document.querySelector(scope);
  const norm = (value) => value.replace(/okf-mermaid-svg-\d+/g, "okf-mermaid-svg-N");
  const mermaid = [...root.querySelectorAll("div.mermaid svg")].map((svg) => {
    const out = [];
    const walk = (el) => {
      const tag = el.tagName.toLowerCase();
      const attrs = ["id", "class", "role", "aria-roledescription"]
        .map((name) => el.getAttribute(name)).map((v) => (v == null ? "" : norm(v)));
      out.push([tag, ...attrs].join("|"));
      if (tag === "style") return;
      for (const child of el.childNodes) {
        if (child.nodeType === 3) {
          const text = child.textContent.replace(/\s+/g, " ").trim();
          if (text) out.push("#" + text);
        } else if (child.nodeType === 1) walk(child);
      }
    };
    walk(svg);
    return out;
  });
  const hljs = [...root.querySelectorAll("pre > code.hljs")].map((code) => ({
    language: [...code.classList].filter((c) => c.startsWith("language-")).join(" "),
    tokens: [...code.querySelectorAll('span[class*="hljs-"]')].map((span) => [span.className, span.textContent]),
  }));
  const katex = [...root.querySelectorAll("div.math .katex-mathml")].map((node) => node.innerHTML);
  return { mermaid, hljs, katex };
}"""
VIEWPORT = {"width": 1280, "height": 900}


class NetworkGuard:
    """Abort and record every non-local request of one browser context."""

    def __init__(self, context, origin: str | None):
        self.origin = origin
        self.requests: list[str] = []
        self.blocked: list[str] = []
        context.on("request", lambda request: self.requests.append(request.url))
        context.route("**/*", self._route)

    def local(self, url: str) -> bool:
        if url.startswith(("file:", "data:", "blob:", "about:")):
            return True
        return bool(self.origin) and (url == self.origin or url.startswith(self.origin + "/"))

    def _route(self, route):
        url = route.request.url
        if self.local(url):
            route.continue_()
        else:
            self.blocked.append(url)
            route.abort()

    def assert_offline(self):
        non_local = [url for url in self.requests if not self.local(url)]
        assert non_local == [] and self.blocked == [], (non_local, self.blocked)
        assert self.requests, "the request log recorded nothing; the guard is not observing"


def _build(tmp_path, target, assets="offline"):
    bundle = Bundle.load(write_offline_fixture(tmp_path / "bundle"))
    out = tmp_path / f"{target}-{assets}"
    build_site(bundle, out, target=target, assets=assets)
    return out


def _host(stack, tmp_path, out):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    stack.enter_context(managed_server(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(out)],
        base, tmp_path / f"http-{port}.log"))
    return base


class Session:
    """One page with its network guard and out-of-page CSP/error collectors."""

    def __init__(self, context, guard):
        self.guard = guard
        self.csp: list[str] = []
        self.errors: list[str] = []
        context.expose_binding("__okfReportCsp", lambda _source, text: self.csp.append(f"event: {text}"))
        context.add_init_script(CSP_REPORTER)
        self.page = context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", self._console)
        self.navigations = 0

    def _console(self, message):
        if "Content Security Policy" in message.text:
            self.csp.append(f"console: {message.text}")

    def assert_no_csp_violation(self):
        # Let queued violation events and their binding calls arrive.
        self.page.evaluate("() => new Promise((resolve) => setTimeout(resolve, 50))")
        self.page.evaluate("() => 0")
        assert self.csp == [], self.csp

    def goto(self, url):
        """Navigate, first proving the page being left had no CSP violation."""
        if self.navigations:
            self.assert_no_csp_violation()
        self.navigations += 1
        self.page.goto(url)


def _session(stack, origin):
    pw = stack.enter_context(sync_playwright())
    browser = launch_chromium(pw)
    stack.callback(browser.close)
    context = browser.new_context(viewport=VIEWPORT, reduced_motion="reduce")
    return Session(context, NetworkGuard(context, origin))


def _assert_matches_cdn_golden(page, scope: str) -> None:
    golden = json.loads(CDN_GOLDEN.read_text(encoding="utf-8"))
    snapshot = page.evaluate(RENDER_SNAPSHOT, scope)
    assert [len(svg) for svg in snapshot["mermaid"]] and snapshot["hljs"] and snapshot["katex"], snapshot
    for part in ("mermaid", "hljs", "katex"):
        assert snapshot[part] == golden[part], f"{part} differs from the CDN golden ({CDN_GOLDEN.name})"


def _assert_rich_rendered(page, scope: str) -> None:
    """Mermaid, KaTeX (with fonts) and highlight.js inside ``scope``."""
    _assert_ledger_fonts(page)
    page.wait_for_function(
        """(scope) => {
          const root = document.querySelector(scope);
          const status = document.documentElement.dataset.okfRenderersStatus;
          return root && (status === 'failed' || (status === 'ready'
            && root.querySelectorAll('div.mermaid svg').length === 2
            && root.querySelector('div.math .katex')
            && root.querySelectorAll('code.hljs').length === 2));
        }""", arg=scope, timeout=30000)
    state = page.evaluate(
        """async (scope) => {
          const root = document.querySelector(scope);
          await document.fonts.ready;
          const loaded = [...document.fonts].filter(f => f.status === 'loaded' && /KaTeX/.test(f.family))
            .map(f => f.family.replace(/"/g, ''));
          return {
            status: document.documentElement.dataset.okfRenderersStatus,
            code: document.documentElement.dataset.okfRenderersCode || null,
            mermaid: root.querySelectorAll('div.mermaid svg').length,
            gantt: !!root.querySelector('div.mermaid svg[aria-roledescription="gantt"]'),
            katex: root.querySelectorAll('div.math .katex').length,
            fonts: [...new Set(loaded)].sort(),
            python: root.querySelectorAll('code.language-python .hljs-keyword').length,
            elixir: root.querySelectorAll('code.language-elixir [class^="hljs-"]').length,
            languages: window.hljs ? window.hljs.listLanguages().slice().sort() : null,
            cdnModule: !!document.querySelector('script[src*="cdn.jsdelivr"], link[href*="cdn.jsdelivr"]'),
          };
        }""", scope)
    assert state["status"] == "ready", state
    assert state["mermaid"] == 2 and state["gantt"], state
    assert state["katex"] == 1, state
    for face in ("KaTeX_Main", "KaTeX_Math", "KaTeX_AMS", "KaTeX_Fraktur", "KaTeX_Script"):
        assert face in state["fonts"], state["fonts"]
    assert any(face.startswith("KaTeX_Size") for face in state["fonts"]), state["fonts"]
    assert state["python"] > 0 and state["elixir"] > 0, state
    assert state["languages"] == sorted(vendor.load_lock()["parity"]["highlightjs_languages"])
    assert not state["cdnModule"]


def _assert_theme_swap_is_local(page, guard) -> None:
    # Ledger syntax is painted by wiki.css, not a light/dark vendor sheet.
    # Also exercise a same-scheme swap: Mermaid embeds palette colours and
    # must repaint Slate -> Clay even though both palettes are dark.
    previous_colours = None
    for palette in ("slate", "clay"):
        before = len(guard.requests)
        generation = page.evaluate("() => window.okfRendererReadiness.generation")
        page.evaluate("palette => window.OKFLoomTheme.setTheme(palette)", palette)
        page.wait_for_function("""({palette, generation}) => {
            const state = window.okfRendererReadiness;
            return document.documentElement.dataset.theme === palette
                && state.status === 'ready' && state.themeName === palette
                && state.generation > generation;
        }""", arg={"palette": palette, "generation": generation}, timeout=15000)
        colours = page.evaluate("""() => {
            const root = getComputedStyle(document.documentElement);
            const probe = document.createElement('span');
            document.body.appendChild(probe);
            const resolved = token => {
                probe.style.color = root.getPropertyValue(token);
                return getComputedStyle(probe).color;
            };
            const roles = ['.hljs-keyword', '.hljs-string', '.hljs-punctuation'];
            const colours = roles.map((selector, i) => {
                // The fixture has keys and strings. A punctuation probe
                // inside real highlighted code covers the third role too.
                let node = document.querySelector('code.hljs ' + selector);
                const synthetic = !node;
                if (synthetic) {
                    node = document.createElement('span');
                    node.className = selector.slice(1);
                    document.querySelector('code.hljs').appendChild(node);
                }
                const pair = [getComputedStyle(node).color, resolved('--syn-' + (i + 1))];
                if (synthetic) node.remove();
                return pair;
            });
            const diagram = document.querySelector('div.mermaid svg .node rect');
            const diagramColours = [getComputedStyle(diagram).fill, resolved('--surface-2')];
            probe.remove();
            return {syntax: colours, diagram: diagramColours};
        }""")
        assert all(actual == token for actual, token in colours["syntax"]), colours
        assert colours["diagram"][0] == colours["diagram"][1], colours
        if previous_colours is not None:
            assert colours != previous_colours, "same-scheme palette swap did not repaint"
        previous_colours = colours
        assert all(guard.local(url) for url in guard.requests[before:])
        assert page.locator('[data-okf-renderer-resource="stylesheet"]').evaluate_all("""nodes =>
            nodes.every(el => !/github(?:-dark)?|okf-vendor-hljs-(?:light|dark)/.test(
                el.href || el.getAttribute('data-okf-href') || ''))""")


def _assert_graph_renders(page) -> None:
    page.wait_for_function("() => !!(window.__okfLoomGraph && window.__okfLoomGraph.cy)", timeout=30000)
    assert page.evaluate(GRAPH_LIBRARIES) == LIBRARIES_LOADED
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes().length") >= 2
    page.evaluate('() => window.__okfLoomGraph.cy.layout({name: "fcose", animate: false}).run()')
    page.evaluate('() => window.__okfLoomGraph.cy.layout({name: "dagre", animate: false}).run()')
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes().every(n => Number.isFinite(n.position('x')))")


def _assert_clean(session) -> None:
    session.guard.assert_offline()
    session.assert_no_csp_violation()
    assert session.errors == [], session.errors


@pytest.mark.parametrize("target", ["static", "spa"])
def test_hosted_multi_file_offline_parity(tmp_path, target):
    out = _build(tmp_path, target)
    with ExitStack() as stack:
        base = _host(stack, tmp_path, out)
        session = _session(stack, base)
        page, guard = session.page, session.guard
        session.goto(base + "/rich.html")
        _assert_rich_rendered(page, ".okf-page__body")
        _assert_matches_cdn_golden(page, ".okf-page__body")
        _assert_theme_swap_is_local(page, guard)
        session.goto(base + "/__graph.html")
        _assert_graph_renders(page)
        _assert_clean(session)


def test_file_static_offline_parity_and_graph_libraries(tmp_path):
    out = _build(tmp_path, "static")
    with ExitStack() as stack:
        session = _session(stack, None)
        page, guard = session.page, session.guard
        session.goto((out / "rich.html").as_uri())
        _assert_rich_rendered(page, ".okf-page__body")
        _assert_matches_cdn_golden(page, ".okf-page__body")
        _assert_theme_swap_is_local(page, guard)
        # Known limit: file:// cannot fetch __data/graph.json, so only the
        # library chain (loaded from local files) and zero network are proved.
        session.goto((out / "__graph.html").as_uri())
        page.wait_for_function("() => !!window.cytoscapeDagre", timeout=30000)
        assert page.evaluate(GRAPH_LIBRARIES) == LIBRARIES_LOADED
        guard.assert_offline()
        session.assert_no_csp_violation()
        assert all(url.startswith("file:") or url.startswith("data:") for url in guard.requests)


@pytest.mark.parametrize("delivery", ["file", "hosted"])
def test_single_file_offline_parity(tmp_path, delivery):
    out = _build(tmp_path, "single-file")
    with ExitStack() as stack:
        base = _host(stack, tmp_path, out) if delivery == "hosted" else None
        session = _session(stack, base)
        page, guard = session.page, session.guard
        session.goto(base + "/index.html" if base else (out / "index.html").as_uri())
        _assert_graph_renders(page)
        page.evaluate('() => window.okfActivateGraphConcept("rich", "keyboard")')
        _assert_rich_rendered(page, "#detail-body")
        # The Details aside is a named landmark, as on the graph page.
        expect(page.get_by_role("complementary", name="Details", exact=True)).to_be_visible()
        _assert_matches_cdn_golden(page, "#detail-body")
        _assert_theme_swap_is_local(page, guard)
        _assert_clean(session)
        # One document request; everything else is embedded.
        assert [url for url in guard.requests if not url.startswith("data:")] == [page.url.split("?")[0].split("#")[0]]


def test_csp_collector_catches_a_violation_on_the_first_page(tmp_path):
    """Negative control: a blocked inline script on the FIRST page fails the
    assert-before-navigation, from both out-of-page channels."""
    out = _build(tmp_path, "static")
    page_path = out / "rich.html"
    html = page_path.read_text(encoding="utf-8")
    assert html.count("</body>") == 1
    page_path.write_text(html.replace("</body>", "<script>window.__okfInlineRan = 1;</script></body>"),
                         encoding="utf-8")
    with ExitStack() as stack:
        base = _host(stack, tmp_path, out)
        session = _session(stack, base)
        session.goto(base + "/rich.html")
        _assert_rich_rendered(session.page, ".okf-page__body")
        assert session.page.evaluate("() => window.__okfInlineRan") is None  # the CSP blocked it
        with pytest.raises(AssertionError, match="script-src"):
            session.goto(base + "/other.html")
        assert any(v.startswith("event: script-src") and "inline" in v and "/rich.html:" in v
                   for v in session.csp), session.csp
        assert any(v.startswith("console: ") for v in session.csp), session.csp
        session.guard.assert_offline()


def _break_static_mermaid(out, damage):
    target = out / "__static" / "vendor" / "mermaid-11.17.2" / "mermaid.min.js"
    if damage == "missing":
        target.unlink()
    else:  # truncated mid-file: a SyntaxError, so window.mermaid never appears
        data = target.read_bytes()
        target.write_bytes(data[: len(data) // 2])


def _break_single_file_mermaid(out, damage):
    page = out / "index.html"
    html = page.read_text(encoding="utf-8")
    match = re.search(r'<script type="application/json" id="okf-vendor-mermaid">.*?</script>', html, re.S)
    assert match
    if damage == "missing":
        html = html[:match.start()] + html[match.end():]
    else:  # not a JSON string any more
        opening = '<script type="application/json" id="okf-vendor-mermaid">'
        html = html[:match.start()] + opening + '"unterminated' + html[match.start() + len(opening) + 1:]
    page.write_text(html, encoding="utf-8")


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("target", ["static", "single-file"])
def test_missing_or_corrupt_offline_asset_fails_closed_with_source_visible(tmp_path, target, damage):
    out = _build(tmp_path, target)
    (_break_static_mermaid if target == "static" else _break_single_file_mermaid)(out, damage)
    with ExitStack() as stack:
        base = _host(stack, tmp_path, out) if target == "static" else None
        session = _session(stack, base)
        page = session.page
        if target == "static":
            session.goto(base + "/rich.html")
            scope = ".okf-page__body"
        else:
            session.goto((out / "index.html").as_uri())
            _assert_graph_renders(page)
            page.evaluate('() => window.okfActivateGraphConcept("rich", "keyboard")')
            scope = "#detail-body"
        page.wait_for_function("() => document.documentElement.dataset.okfRenderersStatus === 'failed'",
                               timeout=30000)
        assert page.evaluate("() => document.documentElement.dataset.okfRenderersCode") \
            == "offline-asset-unavailable"
        diagrams = page.locator(f"{scope} div.mermaid")
        assert diagrams.count() == 2 and page.locator(f"{scope} div.mermaid svg").count() == 0
        for index in range(2):
            assert diagrams.nth(index).is_visible()
        assert "A[Start] --> B{Check}" in diagrams.nth(0).inner_text()
        assert "dateFormat YYYY-MM-DD" in diagrams.nth(1).inner_text()
        # The other renderers are unaffected and nothing reached for the CDN.
        assert page.locator(f"{scope} div.math .katex").count() == 1
        assert page.locator(f"{scope} code.hljs").count() == 2
        session.guard.assert_offline()
        assert not any("cdn.jsdelivr.net" in url for url in session.guard.requests)
        session.assert_no_csp_violation()
        if target == "static" and damage == "missing":
            assert any(url.endswith("/mermaid-11.17.2/mermaid.min.js") for url in session.guard.requests)


AUTHORED_STATE = """() => ({
  ran: window.__okfAuthoredRan === 1,
  injected: document.querySelectorAll('[data-okf-vendor]').length,
  bodyIds: ['okf-asset-bootstrap', 'okf-vendor-mermaid'].map(id => {
    const el = document.getElementById(id); return el ? el.tagName : null; }),
})"""


def test_reserved_ids_do_not_switch_the_default_cdn_build(tmp_path):
    """An authored '# okf-asset-bootstrap' heading keeps the CDN path."""
    from test_offline_assets_hardening import write_reserved_fixture
    bundle = Bundle.load(write_reserved_fixture(tmp_path / "bundle"))
    out = tmp_path / "static-cdn"
    build_site(bundle, out, target="static")
    with ExitStack() as stack:
        base = _host(stack, tmp_path, out)
        session = _session(stack, base)
        page, guard = session.page, session.guard
        session.goto(base + "/rich.html")
        page.wait_for_function("() => document.documentElement.dataset.okfRenderersStatus === 'failed'",
                               timeout=30000)
        # CDN was selected (its requests were attempted and aborted), never offline.
        assert any("cdn.jsdelivr.net" in url for url in guard.blocked)
        assert page.evaluate("() => document.documentElement.dataset.okfRenderersCode") \
            != "offline-asset-unavailable"
        state = page.evaluate(AUTHORED_STATE)
        # The colliding ids exist in the body (as headings), yet did not count.
        assert state["ran"] is False and state["injected"] == 0, state
        assert all(tag and re.fullmatch(r"H[1-6]", tag) for tag in state["bodyIds"]), state


@pytest.mark.parametrize("target, delivery", [("static", "hosted"), ("single-file", "file")])
def test_generated_offline_config_stays_authoritative_over_reserved_ids(tmp_path, target, delivery):
    from test_offline_assets_hardening import write_reserved_fixture
    bundle = Bundle.load(write_reserved_fixture(tmp_path / "bundle"))
    out = tmp_path / f"{target}-offline"
    build_site(bundle, out, target=target, assets="offline")
    with ExitStack() as stack:
        base = _host(stack, tmp_path, out) if delivery == "hosted" else None
        session = _session(stack, base)
        page = session.page
        if target == "static":
            session.goto(base + "/rich.html")
            _assert_rich_rendered(page, ".okf-page__body")
        else:
            session.goto((out / "index.html").as_uri())
            _assert_graph_renders(page)
            page.evaluate('() => window.okfActivateGraphConcept("rich", "keyboard")')
            _assert_rich_rendered(page, "#detail-body")
        _assert_clean(session)
        state = page.evaluate(AUTHORED_STATE)
        assert state["ran"] is False, state
        # Every injected vendor script came from a generated <head> carrier.
        assert page.evaluate("""() => [...document.querySelectorAll('[data-okf-vendor]')]
            .every(el => { const src = document.head.querySelector(
              'script[type="application/json"]#' + el.getAttribute('data-okf-vendor'));
              return !!src; })""")


def test_malformed_generated_bootstrap_fails_closed_without_cdn(tmp_path):
    out = _build(tmp_path, "static")
    page_path = out / "rich.html"
    html = page_path.read_text(encoding="utf-8")
    genuine = '<script type="application/json" id="okf-asset-bootstrap">{'
    assert html.count(genuine) == 1
    page_path.write_text(html.replace(genuine, genuine + "not json,", 1), encoding="utf-8")
    with ExitStack() as stack:
        base = _host(stack, tmp_path, out)
        session = _session(stack, base)
        page, guard = session.page, session.guard
        session.goto(base + "/rich.html")
        page.wait_for_function("() => document.documentElement.dataset.okfRenderersStatus === 'failed'",
                               timeout=30000)
        assert page.evaluate("() => document.documentElement.dataset.okfRenderersCode") \
            == "offline-asset-unavailable"
        guard.assert_offline()
        assert page.locator("div.mermaid svg").count() == 0


def test_guard_catches_the_default_cdn_build(tmp_path):
    """Negative control: the same guard aborts the CDN build's requests."""
    out = _build(tmp_path, "static", assets="cdn")
    with ExitStack() as stack:
        base = _host(stack, tmp_path, out)
        session = _session(stack, base)
        page, guard = session.page, session.guard
        session.goto(base + "/rich.html")
        page.wait_for_function("() => document.documentElement.dataset.okfRenderersStatus === 'failed'",
                               timeout=30000)
        assert any("cdn.jsdelivr.net" in url for url in guard.blocked)
        assert page.locator("div.mermaid svg").count() == 0


def _assert_ledger_fonts(page):
    # FontFaceSet.check alone can succeed with a fallback; require one loaded
    # registered face per requested family/style as well as the browser check.
    result = page.evaluate('''async () => {
      const results = [];
      for (const family of ['Public Sans', 'Azeret Mono']) {
        for (const style of ['normal', 'italic']) {
          const query = `${style} 400 16px "${family}"`;
          const faces = await document.fonts.load(query, 'Ledger 012345');
          results.push({family, style, loaded: faces.length,
            check: document.fonts.check(query, 'Ledger 012345'),
            match: faces.some(f => f.family.replaceAll('"', '') === family && f.style === style && f.status === 'loaded')});
        }
      }
      return results;
    }''')
    assert len(result) == 4
    assert all(row['loaded'] > 0 and row['check'] and row['match'] for row in result), result


@pytest.mark.parametrize("target,assets", [("static", "offline"), ("spa", "offline"),
                                           ("single-file", "offline"), ("static", "cdn"),
                                           ("single-file", "cdn")])
def test_ledger_fonts_without_network(tmp_path, target, assets):
    out = _build(tmp_path, target, assets)
    with ExitStack() as stack:
        # The single-file proof explicitly uses file://; CDN script requests
        # may be attempted in CDN mode but must not be needed for any font.
        base = None if target == "single-file" else _host(stack, tmp_path, out)
        session = _session(stack, base)
        url = (out / "index.html").as_uri() if base is None else base + "/rich.html"
        session.goto(url)
        _assert_ledger_fonts(session.page)
        session.assert_no_csp_violation()
        assert not any('font' in url or 'woff' in url for url in session.guard.blocked)
        if assets == "offline":
            session.guard.assert_offline()


def test_served_ledger_fonts_without_network(tmp_path):
    bundle = write_offline_fixture(tmp_path / "bundle")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ExitStack() as stack:
        stack.enter_context(managed_server(
            [sys.executable, "-m", "okf_loom", "serve", str(bundle), "--no-open", "--port", str(port)],
            base, tmp_path / "serve.log"))
        session = _session(stack, base)
        session.goto(base + "/other")
        _assert_ledger_fonts(session.page)
        session.assert_no_csp_violation()
        session.guard.assert_offline()
