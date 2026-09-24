"""Focused proof for viewer readiness and static fallback parity.

The server paints a document before the deferred renderer (and, in live mode,
Studio) has settled.  These tests keep that first-paint contract separate from
the runtime-owned terminal readiness checks, and exercise every shipped page
shape in a static browser with and without JavaScript.
"""
from __future__ import annotations

import http.server
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.render import build_site, render_single_file


from browser_support import sync_playwright, launch_chromium, copy_bundle, live_server


class _BodyStartTag(HTMLParser):
    """Stop at the document's real ``<body>`` start tag."""

    class Found(Exception):
        pass

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            raise self.Found(self.get_starttag_text())


def _body_tag(html: str) -> str:
    # Inlined CSS/JS (the single-file target inlines wiki.css) may mention
    # "<body>" in a comment; HTMLParser treats <style>/<script> as raw text,
    # so only the real start tag is reported.
    try:
        _BodyStartTag(convert_charrefs=True).feed(html)
    except _BodyStartTag.Found as found:
        return str(found)
    raise AssertionError("no <body> start tag")


def _concept_page(site: Path, bundle: Bundle) -> Path:
    concept = sorted(bundle.concepts.values(), key=lambda item: item.id)[0]
    return site.joinpath(*concept.id).with_suffix(".html")


def _target_pages(site: Path, bundle: Bundle) -> dict[str, Path]:
    return {
        "index": site / "index.html",
        "concept": _concept_page(site, bundle),
        "search": site / "__search.html",
        "graph": site / "__graph.html",
    }


def test_initial_markup_defers_capture_readiness_for_every_output_target(
    tiny_good_bundle: Path, tmp_path: Path
) -> None:
    """No server-rendered page may claim terminal ``ready`` on first paint."""
    bundle = Bundle.load(tiny_good_bundle)

    for target in ("static", "spa"):
        site = tmp_path / target
        build_site(bundle, site, target=target)
        for label, path in _target_pages(site, bundle).items():
            body = _body_tag(path.read_text(encoding="utf-8"))
            assert 'data-okf-capture-ready="ready"' not in body, (
                f"{target}/{label} pre-published terminal capture readiness"
            )
            if label == "graph":
                assert 'data-okf-capture-ready="loading"' in body
            else:
                # Document/search readiness is published by its runtime owner;
                # an absent surface state lets capture-readiness.js wait for
                # renderer/theme/Studio terminal signals without a stale
                # server-owned loading value that can never be cleared.
                assert "data-okf-capture-ready" not in body

    single = tmp_path / "single.html"
    render_single_file(bundle, single)
    body = _body_tag(single.read_text(encoding="utf-8"))
    assert 'data-okf-capture-ready="ready"' not in body
    assert 'data-okf-capture-ready="loading"' in body


def test_static_fallback_copy_is_target_complete_and_not_a_boot_failure(
    tiny_good_bundle: Path, tmp_path: Path
) -> None:
    """Static output carries an explicit read-only outcome on every surface."""
    bundle = Bundle.load(tiny_good_bundle)
    single = tmp_path / "single.html"
    render_single_file(bundle, single)

    for target in ("static", "spa"):
        site = tmp_path / target
        build_site(bundle, site, target=target)
        pages = [*_target_pages(site, bundle).values(), single]
        for path in pages:
            html = path.read_text(encoding="utf-8")
            assert "okf-static-viewer-note" in html, path
            assert "Static viewer:" in html, path
            assert 'role="status"' in html, path
            assert "JavaScript is off or Studio failed to start" not in html, path


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def static_server(tmp_path: Path, tiny_good_bundle: Path):
    bundle = Bundle.load(tiny_good_bundle)
    site = tmp_path / "static"
    build_site(bundle, site, target="static")
    single_dir = tmp_path / "single"
    single_dir.mkdir()
    render_single_file(bundle, single_dir / "index.html")

    handler = lambda *args, **kwargs: _QuietStaticHandler(  # noqa: E731
        *args, directory=str(tmp_path), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", bundle, site
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def chromium_browser():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            yield browser
        finally:
            browser.close()


@pytest.mark.browser
def test_static_js_and_nojs_fallbacks_are_truthful_on_all_targets(
    static_server, chromium_browser
) -> None:
    """The visible static notice is honest; no-JS content remains available."""
    base, bundle, _site = static_server
    routes = [
        "/static/index.html",
        f"/static/{'/'.join(sorted(bundle.concepts)[0])}.html",
        "/static/__search.html",
        "/static/__graph.html",
        "/single/index.html",
    ]

    context = chromium_browser.new_context()
    page = context.new_page()
    try:
        for route in routes:
            page.goto(base + route, wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.body.getAttribute('data-okf-js') === '1'",
                timeout=10000,
            )
            state = page.evaluate(
                """() => {
                  const staticNote = document.querySelector('.okf-static-viewer-note');
                  const liveFallback = document.querySelector('.okf-live-studio-fallback');
                  return {
                    staticVisible: !!staticNote && getComputedStyle(staticNote).display !== 'none',
                    staticText: staticNote ? staticNote.textContent : '',
                    liveVisible: !!liveFallback && getComputedStyle(liveFallback).display !== 'none',
                    bodyMode: document.body.dataset.okfMode,
                  };
                }"""
            )
            assert state["bodyMode"] == "static"
            assert state["staticVisible"] is True
            assert "Static viewer:" in state["staticText"]
            assert state["liveVisible"] is False
            assert "JavaScript is off or Studio failed to start" not in page.locator("body").inner_text()

            # The same DOM keeps a distinct live failure outcome on the
            # multi-file surfaces. This models the server's Studio-enabled
            # state without relying on a network boot. The single-file target
            # intentionally has no live-failure banner because it never
            # advertises a Studio runtime.
            if not route.endswith("/single/index.html"):
                failure_state = page.evaluate(
                    """() => {
                      document.body.dataset.okfStudio = '1';
                      const live = document.querySelector('.okf-live-studio-fallback');
                      const staticNote = document.querySelector('.okf-static-viewer-note');
                      return {
                        liveVisible: !!live && getComputedStyle(live).display !== 'none',
                        liveText: live ? live.textContent : '',
                        staticVisible: !!staticNote && getComputedStyle(staticNote).display !== 'none',
                      };
                    }"""
                )
                assert failure_state["liveVisible"] is True, (route, failure_state)
                assert "Live Studio is unavailable." in failure_state["liveText"]
                assert failure_state["staticVisible"] is False
    finally:
        context.close()

    nojs_context = chromium_browser.new_context(java_script_enabled=False)
    nojs_page = nojs_context.new_page()
    try:
        for route in routes:
            nojs_page.goto(base + route, wait_until="domcontentloaded")
            text = nojs_page.locator("body").inner_text()
            assert (
                "JavaScript is off" in text or "without JavaScript" in text
            ), route
            assert "JavaScript is off or Studio failed to start" not in text
    finally:
        nojs_context.close()


@pytest.mark.browser
def test_document_capture_waits_for_runtime_renderer_terminal_state(
    static_server, chromium_browser
) -> None:
    """A loading renderer state keeps capture pending until it publishes ready."""
    base, _bundle, _site = static_server
    context = chromium_browser.new_context()
    page = context.new_page()
    page.route(
        "**/renderers.js",
        lambda route: route.fulfill(
            body=(
                "window.okfRendererReadiness={status:'loading',ready:false};"
                "document.documentElement.dataset.okfRenderersStatus='loading';"
                "document.body.dataset.okfRenderersStatus='loading';"
            ),
            content_type="application/javascript",
        ),
    )
    try:
        page.goto(base + "/static/index.html", wait_until="domcontentloaded")
        result = page.evaluate(
            """async () => {
              const run = window.okfCaptureReadiness.start({target: 'document'});
              const beforeTerminal = await Promise.race([
                run.promise.then(() => 'resolved'),
                new Promise(resolve => setTimeout(() => resolve('pending'), 50)),
              ]);
              window.okfRendererReadiness = {status: 'ready', ready: true};
              document.documentElement.dataset.okfRenderersStatus = 'ready';
              document.body.dataset.okfRenderersStatus = 'ready';
              const settled = await run.promise;
              return {beforeTerminal, phase: settled.metadata.readinessPhase};
            }"""
        )
        assert result == {"beforeTerminal": "pending", "phase": "ready"}
    finally:
        context.close()


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(base: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"okf serve exited early (rc={process.returncode})")
        try:
            with urllib.request.urlopen(base + "/", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.1)
    raise RuntimeError("okf serve did not become ready")


@pytest.mark.browser
def test_live_studio_failure_keeps_failure_copy_distinguishable(
    chromium_browser, tmp_path,
) -> None:
    """A live boot failure exposes the live outcome, not the static copy."""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    bundle = copy_bundle(Path(__file__).resolve().parents[1] / "samples/demo_bundle", tmp_path / "bundle")
    process = subprocess.Popen(
        [
            "scripts/okf-loom",
            "serve",
            str(bundle),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-open",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    page = chromium_browser.new_page()
    try:
        _wait_for_server(base, process)
        page.add_init_script(
            """(() => {
              window.__OKF_LOOM_TEST__ = true;
              window.__OKF_LOOM_STUDIO_TEST_FAILURE__ = 'boot';
              window.__OKF_RENDERER_TEST_HOOKS__ = {
                loadCSS: () => Promise.resolve({href: '', media: 'not all'}),
                loadModule: (url) => url.includes('highlight')
                  ? Promise.resolve({default: {highlightElement() {}}})
                  : Promise.resolve({default: {initialize() {}, render() { return {svg: '<svg></svg>'}; }}}),
              };
            })();"""
        )
        page.goto(base + "/tables/orders", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.okfStudioReadiness && window.okfStudioReadiness.status === 'failed'",
            timeout=10000,
        )
        state = page.evaluate(
            """() => {
              const live = document.querySelector('.okf-live-studio-fallback');
              const staticNote = document.querySelector('.okf-static-viewer-note');
              return {
                studioMode: document.body.dataset.okfStudio,
                liveVisible: !!live && getComputedStyle(live).display !== 'none',
                liveText: live ? live.textContent : '',
                staticVisible: !!staticNote && getComputedStyle(staticNote).display !== 'none',
              };
            }"""
        )
        assert state["studioMode"] == "1"
        assert state["liveVisible"] is True
        assert "Live Studio is unavailable." in state["liveText"]
        assert state["staticVisible"] is False
    finally:
        page.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
