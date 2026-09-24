"""Browser proof for the one-notice no-JavaScript viewer contract."""
from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import okf_module_argv, okf_subprocess_env
from okf_loom import Bundle
from okf_loom.render import build_site, render_single_file


from browser_support import expect, sync_playwright, launch_chromium


ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _wait_for_server(base: str, process: subprocess.Popen[object]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"okf serve exited early (rc={process.returncode})")
        try:
            with urllib.request.urlopen(base + "/", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise RuntimeError("okf serve did not become ready")


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


def _start_static_server(
    root: Path,
) -> tuple[http.server.ThreadingHTTPServer, threading.Thread]:
    handler = lambda *args, **kwargs: _QuietStaticHandler(  # noqa: E731
        *args, directory=str(root), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.mark.browser
def test_nojs_viewer_targets_have_exactly_one_visible_notice(
    tiny_good_bundle: Path, tmp_path: Path
) -> None:
    """Every target exposes one accessible, rendered no-JS notice only.

    The old assertion used a ``:visible`` locator against content nested in
    ``<noscript>``. Chromium can give that content a layout box while omitting
    it from the accessibility tree, so that assertion was false confidence.
    These checks use the rendered text plus the semantic note role and also
    exercise live Studio's successful and failed boot states with scripting
    enabled.
    """
    bundle = Bundle.load(tiny_good_bundle)
    concept = "/".join(sorted(bundle.concepts)[0])

    static_site = tmp_path / "static"
    build_site(bundle, static_site, target="static")
    spa_site = tmp_path / "spa"
    build_site(bundle, spa_site, target="spa")
    single_site = tmp_path / "single"
    render_single_file(bundle, single_site / "index.html")

    static_server, static_thread = _start_static_server(tmp_path)
    spa_server, spa_thread = _start_static_server(spa_site)

    live_port = _free_port()
    live_base = f"http://127.0.0.1:{live_port}"
    live_process = subprocess.Popen(
        okf_module_argv(
            "serve",
            str(tiny_good_bundle),
            "--host",
            "127.0.0.1",
            "--port",
            str(live_port),
            "--no-open",
        ),
        cwd=ROOT,
        env=okf_subprocess_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        _wait_for_server(live_base, live_process)
        static_base = f"http://127.0.0.1:{static_server.server_port}"
        spa_base = f"http://127.0.0.1:{spa_server.server_port}"
        targets = [
            (
                "live/index",
                live_base + "/",
                "JavaScript is off. The authored index and concept links remain available.",
            ),
            (
                "live/concept",
                live_base + "/" + concept,
                "JavaScript is off. Live editing and annotation controls are unavailable; authored content remains readable. Browse the index.",
            ),
            (
                "live/search",
                live_base + "/__search",
                "JavaScript is off. Live GET search and canonical manual Context Pack links remain available.",
            ),
            (
                "live/graph",
                live_base + "/__graph",
                "JavaScript is off. The static relationship map and authored graph links remain available below.",
            ),
            (
                "spa/index",
                spa_base + "/index.html",
                "JavaScript is off. This SPA build keeps the authored index and concept links available; interactive enhancements are unavailable.",
            ),
            (
                "spa/concept",
                spa_base + "/" + concept + ".html",
                "JavaScript is off. This SPA build keeps authored content and links available; interactive enhancements are unavailable. Browse the index.",
            ),
            (
                "spa/search",
                spa_base + "/__search.html",
                "JavaScript is off. This SPA build keeps rendered results and concept links available; interactive search requires JavaScript.",
            ),
            (
                "spa/graph",
                spa_base + "/__graph.html",
                "JavaScript is off. This SPA build keeps the static relationship map and authored graph links available below.",
            ),
            (
                "static/index",
                static_base + "/static/index.html",
                "JavaScript is off. This static build keeps the authored index and concept links available; interactive enhancements are unavailable.",
            ),
            (
                "static/concept",
                static_base + "/static/" + concept + ".html",
                "JavaScript is off. This static build keeps authored content and links available; interactive enhancements are unavailable. Browse the index.",
            ),
            (
                "static/search",
                static_base + "/static/__search.html",
                "JavaScript is off. In this static build, arbitrary search requires JavaScript and is unavailable. Follow a recent concept below or browse the concept index by type or tag.",
            ),
            (
                "static/graph",
                static_base + "/static/__graph.html",
                "JavaScript is off. This static build keeps the static relationship map and authored graph links available below.",
            ),
            (
                "single-file",
                static_base + "/single/index.html",
                "JavaScript is off. Concept bodies, the graph view, and embedded Search and Context controls need JavaScript, so they are unavailable in this single-file viewer. The concept list above and the resolved in-bundle relationships below remain readable.",
            ),
        ]

        with sync_playwright() as playwright:
            browser = launch_chromium(playwright)

            context = browser.new_context(java_script_enabled=False)
            page = context.new_page()
            try:
                for label, url, expected_text in targets:
                    page.goto(url, wait_until="domcontentloaded")
                    banners = page.locator(".okf-studio-fallback-banner:visible")
                    expect(banners, label).to_have_count(1)
                    expect(banners.first, label).to_have_text(expected_text)
                    note = page.get_by_role("note")
                    expect(note, label).to_have_count(1)
                    expect(note, label).to_be_visible()
                    expect(note, label).to_have_text(expected_text)
                    aria = page.locator("body").aria_snapshot()
                    assert "- note:" in aria and "JavaScript is off." in aria, label
                    assert "JavaScript is off." in page.locator("body").inner_text(), label
            finally:
                context.close()

            # A parser-blocking theme bootstrap marks JS-enabled pages
            # before body content is painted. Successful Studio boot must
            # then remove every fallback, not merely the no-JS note.
            success_context = browser.new_context()
            success_page = success_context.new_page()
            try:
                success_page.goto(live_base + "/" + concept, wait_until="load")
                success_page.wait_for_function(
                    "() => document.documentElement.classList.contains('okf-studio-booted')",
                    timeout=10000,
                )
                expect(success_page.locator(".okf-nojs-fallback:visible")).to_have_count(0)
                expect(success_page.locator(".okf-studio-fallback-banner:visible")).to_have_count(0)
            finally:
                success_context.close()

            # A page whose Studio module cannot boot must retain the
            # truthful live failure notice, while still suppressing the
            # no-JS notice because scripting is enabled.
            failure_context = browser.new_context()
            failure_page = failure_context.new_page()
            failure_page.route("**/__static/studio.js", lambda route: route.abort())
            try:
                failure_page.goto(live_base + "/" + concept, wait_until="load")
                failure = failure_page.locator(".okf-live-studio-fallback")
                expect(failure).to_be_visible()
                expect(failure).to_have_text(
                    "Live Studio is unavailable. Editing and annotation controls cannot be loaded; authored content remains readable. Browse the index."
                )
                expect(failure_page.locator(".okf-nojs-fallback:visible")).to_have_count(0)
                expect(failure_page.locator(".okf-studio-fallback-banner:visible")).to_have_count(1)
                assert "Live Studio is unavailable." in failure_page.locator("body").aria_snapshot()
            finally:
                failure_context.close()
            browser.close()
    finally:
        live_process.terminate()
        try:
            live_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            live_process.kill()
            live_process.wait(timeout=5)
        static_server.shutdown()
        static_thread.join(timeout=5)
        static_server.server_close()
        spa_server.shutdown()
        spa_thread.join(timeout=5)
        spa_server.server_close()
