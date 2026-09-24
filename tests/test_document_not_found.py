"""HTML document recovery is presentation-only; API errors stay unchanged."""
from __future__ import annotations

import http.client
import json
import re
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

from okf_loom import Bundle
from okf_loom.server import OKFWikiHandler


@pytest.fixture
def document_server(tiny_good_bundle):
    server = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    server.bundle = Bundle.load(tiny_good_bundle)
    server.config = {}
    server.name = server.bundle.name
    server.plugin = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if getattr(server, "studio", None) is not None:
            server.studio.close()


def request(server, path, *, accept=None, method="GET"):
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request(method, path, headers={} if accept is None else {"Accept": accept})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        conn.close()


def test_missing_document_html_recovers_through_existing_shell(document_server):
    status, headers, body = request(document_server, "/no-such-page", accept="text/html")
    assert status == 404
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert "Page not found" in body
    assert 'data-okf-application-shell' in body
    assert 'href="/"' in body and "Browse the index" in body
    assert 'href="/__search"' in body
    assert 'action="/__search"' in body and 'name="q"' in body
    assert 'aria-current="page"' not in body
    assert 'href="/__static/wiki.css"' in body
    assert 'src="/__static/theme.js"' in body
    assert headers["Cache-Control"] == "no-cache"
    assert headers["Vary"] == "Accept"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "object-src 'none'" in headers["Content-Security-Policy"]


@pytest.mark.parametrize("accept", [
    None, "", "*/*", "text/*", "text/plain", "application/json",
    "text/html;q=0", "text/html;q=0, */*;q=1",
    "text/html;q=0.5, text/plain", "application/json, text/html",
    "text/html;q=0.8, */*;q=0.9", "text/html;q=bogus", "text/html;q=1.1",
    "text/html;q=1;q=0", "text/html, text/html;q=0", "not-text/html",
])
def test_non_html_preferences_keep_exact_plain_404(document_server, accept):
    status, headers, body = request(document_server, "/no-such-page", accept=accept)
    assert (status, headers["Content-Type"], body) == (
        404, "text/plain", "Unknown concept: no-such-page",
    )
    assert headers["Cache-Control"] == "no-cache"
    assert headers["Vary"] == "Accept"


@pytest.mark.parametrize("accept", [
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "TEXT/HTML;Q=1", "text/html;q=0.9, text/plain;q=0.8", "text/html, */*",
])
def test_browser_html_preferences(document_server, accept):
    status, headers, body = request(document_server, "/no-such-page.md/", accept=accept)
    assert status == 404 and headers["Content-Type"].startswith("text/html")
    assert "Page not found" in body


def test_hostile_missing_path_is_escaped_and_query_not_reflected(document_server):
    from html import escape

    path = '<img src=x onerror="alert(1)">&'
    document_server.name = '<bundle>&"'
    status, headers, body = request(
        document_server, "/" + quote(path, safe="") + "?token=do-not-reflect-this",
        accept="text/html",
    )
    assert status == 404 and headers["Content-Type"].startswith("text/html")
    assert escape(path) in body and path not in body
    assert escape(document_server.name) in body and document_server.name not in body
    assert "do-not-reflect-this" not in body
    status, headers, body = request(document_server, "/" + quote(path, safe=""))
    assert (status, headers["Content-Type"], body) == (404, "text/plain", "Not a concept id: " + path)


@pytest.mark.parametrize("path", [
    "/__raw/no-such-page", "/__data/doc?concept=no-such-page",
    "/__data/page?document=no-such-page.md", "/__static/no-such.css",
    "/__search?format=json&mode=bogus", "/__context?concept=no-such-page&format=json",
    "/bad%2Fpath", "/bad%ZZpath",
])
def test_raw_api_and_decoding_errors_ignore_html_accept(document_server, path):
    plain = request(document_server, path)
    html = request(document_server, path, accept="text/html")
    assert plain[0] >= 400
    assert (html[0], html[1]["Content-Type"], html[2]) == (plain[0], plain[1]["Content-Type"], plain[2])


def test_hidden_concept_not_disclosed_by_html_recovery(document_server):
    root = document_server.bundle.root
    (root / "hidden.md").write_text("---\ntype: Secret\ntitle: Secret hidden title\n---\nHidden body\n")
    (root / ".gitignore").write_text("hidden.md\n")
    status, _, body = request(document_server, "/hidden", accept="text/html")
    assert status == 404
    assert "Page not found" in body
    assert "Secret hidden title" not in body and "Hidden body" not in body
    assert "Secret" not in body


def test_missing_document_head_has_html_headers_without_body(document_server):
    status, headers, body = request(document_server, "/missing", accept="text/html", method="HEAD")
    assert status == 404 and headers["Content-Type"].startswith("text/html")
    assert int(headers["Content-Length"]) > 0 and body == ""


def test_live_read_only_recovery_has_no_token_or_previous_document(document_server):
    from okf_loom.studio import Studio

    server = document_server
    server.studio = Studio.for_bundle(server.bundle.root, bundle_name=server.bundle.name)
    server.studio.ensure_session()
    server.csrf_token = "local-test-token-not-for-read-only-pages"
    server.studio_edit = False
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        # Reuse the connection after a real document: recovery must not retain
        # that document's annotation identity in the normal Studio bootstrap.
        conn.request("GET", "/tables/users", headers={"Accept": "text/html"})
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        conn.request("GET", "/missing", headers={"Accept": "text/html"})
        response = conn.getresponse()
        assert response.status == 404
        body = response.read().decode()
    finally:
        conn.close()
    bootstrap = json.loads(re.search(
        r'<script type="application/json" id="okf-studio-bootstrap">(.*?)</script>', body,
    ).group(1))
    assert bootstrap["document"] is None
    assert bootstrap["authority_mode"] == "local-read"
    assert bootstrap["edit"] is False and bootstrap["comment"] is False
    assert bootstrap["token"] == "" and server.csrf_token not in body
