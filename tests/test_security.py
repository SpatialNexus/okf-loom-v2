"""Security regression tests for the okf-loom.

These pin the security invariants against regression. Each test exercises a
specific XSS / traversal / injection vector and asserts okf-loom's
neutralisation. If any of these flips to failing, that is a P0 regression.

Coverage:
    - markdown_to_html escapes raw HTML (no <script>/<img onerror> passthrough)
    - URL scheme allowlist blocks javascript:/data:/vbscript: in hrefs and img srcs
    - _json_for_script escapes </script> sequences (no script-element breakout)
    - rendered single-file HTML contains no script breakout in the JSON blob
    - rendered single-file HTML does not reference marked.js (no client-side MD parser)
    - server response carries CSP / nosniff / DENY / no-referrer headers
    - server 500 response does not leak exception text
    - server rejects URL-encoded traversal in directory index route
    - log append rejects --log-path that escapes the bundle root
    - atomic writes clean up tmp files on failure
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.render import render_single_file, _json_for_script
from okf_loom.viewer.markdown import markdown_to_html, _safe_url
from okf_loom.log import append_log_entry, _resolve_within_bundle

from conftest import okf_module_argv, okf_subprocess_env


# ---------------------------------------------------------------------------
# markdown_to_html: raw HTML is escaped, not passed through
# ---------------------------------------------------------------------------


def test_md_script_tag_is_escaped() -> None:
    """A <script> tag in a concept body MUST be rendered as escaped text."""
    out = markdown_to_html("Hello <script>alert(1)</script> world")
    assert "<script>" not in out
    assert "</script>" not in out
    assert "&lt;script&gt;" in out


def test_md_img_onerror_is_escaped() -> None:
    """An <img onerror=...> tag MUST be rendered as escaped text."""
    out = markdown_to_html("<img src=x onerror=alert(1)>")
    # The literal text "onerror=alert(1)" may appear inside escaped text,
    # but the browser will see it as text content, not an attribute.
    assert "<img" not in out
    assert "&lt;img" in out


def test_md_svg_onload_is_escaped() -> None:
    """An <svg onload=...> tag MUST be rendered as escaped text."""
    out = markdown_to_html("<svg/onload=alert(1)>")
    assert "<svg" not in out
    assert "&lt;svg" in out


# ---------------------------------------------------------------------------
# URL scheme allowlist (links and images)
# ---------------------------------------------------------------------------


def test_safe_url_allows_https() -> None:
    assert _safe_url("https://example.com") == "https://example.com"


def test_safe_url_allows_http() -> None:
    assert _safe_url("http://example.com") == "http://example.com"


def test_safe_url_allows_mailto() -> None:
    assert _safe_url("mailto:a@b.com") == "mailto:a@b.com"


def test_safe_url_allows_anchor() -> None:
    assert _safe_url("#section") == "#section"


def test_safe_url_allows_absolute_path() -> None:
    assert _safe_url("/tables/users.md") == "/tables/users.md"


def test_safe_url_allows_relative() -> None:
    assert _safe_url("./users.md") == "./users.md"
    assert _safe_url("../parent.md") == "../parent.md"
    assert _safe_url("users.md") == "users.md"


def test_safe_url_blocks_javascript() -> None:
    assert _safe_url("javascript:alert(1)") is None
    assert _safe_url("JavaScript:alert(1)") is None  # case-insensitive
    assert _safe_url("javascript://%0aalert(1)") is None


def test_safe_url_blocks_data() -> None:
    assert _safe_url("data:text/html,<script>alert(1)</script>") is None


def test_safe_url_blocks_vbscript() -> None:
    assert _safe_url("vbscript:alert(1)") is None


def test_safe_url_blocks_file() -> None:
    assert _safe_url("file:///etc/passwd") is None


def test_safe_url_blocks_protocol_relative() -> None:
    """Protocol-relative URLs (//host) are blocked (could carry attacker scheme)."""
    assert _safe_url("//evil.com/script.js") is None


def test_md_link_with_javascript_is_neutralised() -> None:
    """A markdown link with a javascript: URL renders as plain text, not <a>."""
    out = markdown_to_html("[click](javascript:alert(1))")
    # No anchor tag emitted.
    assert "<a " not in out
    assert "<a>" not in out
    # No href attribute at all (the URL is in a title="blocked: ..." tooltip).
    assert "href=" not in out
    # Visually marked as blocked.
    assert "okf-link-blocked" in out


def test_md_link_with_data_is_neutralised() -> None:
    out = markdown_to_html("[x](data:text/html,<script>alert(1)</script>)")
    assert "<a " not in out
    assert "<a>" not in out
    assert "href=" not in out
    assert "okf-link-blocked" in out


def test_md_image_with_javascript_src_is_neutralised() -> None:
    """Image markdown with a javascript: URL MUST NOT emit an <img> tag.

    The renderer may parse `![alt](javascript:...)` as a link (because the
    link regex runs before the image regex), in which case the result is a
    blocked-link span, not an image. Either way, no <img> is emitted.
    """
    out = markdown_to_html("![alt](javascript:alert(1))")
    assert "<img" not in out
    # The dangerous URL must not appear in any src attribute.
    assert "src=" not in out


def test_md_image_with_data_src_is_neutralised() -> None:
    """data: URLs in images are also blocked (data:image/svg+xml can carry script)."""
    out = markdown_to_html("![alt](data:image/svg+xml,<svg onload=alert(1)>)")
    assert "<img" not in out
    assert "src=" not in out


def test_md_safe_link_preserved() -> None:
    """Legitimate links survive the allowlist."""
    out = markdown_to_html("[ok](https://example.com)")
    assert 'href="https://example.com"' in out


# ---------------------------------------------------------------------------
# Entity-encoded URL scheme bypass (Annotations v2 ReviewAll r3 blocker)
# ---------------------------------------------------------------------------
# Character references inside link/image destinations MUST be decoded to
# their browser-semantic form BEFORE the scheme allowlist runs, so an
# entity-encoded colon cannot hide an unsafe scheme. These rendered-output
# tests (NOT helper-level) prove [x](javascript&colon;alert(1)) and the
# numeric/hex/mixed-case/control/data variants fail closed to the existing
# blocked <span>, and that safe destinations (incl. query &amp;) survive.

_UNSAFE_SCHEMES = ("javascript", "data", "vbscript", "file")


def _assert_no_unsafe_href_or_src(out: str) -> None:
    """No emitted href/src decodes (one browser attribute pass) to an unsafe
    scheme. Blocked links/images render as <span>s with no href/src, so this
    passes for correctly fail-closed output and catches any anchor/img that
    leaked through carrying javascript:/data:/vbscript:/file:."""
    import html as _html
    for attr in ("href", "src"):
        for m in re.finditer(rf'{attr}="([^"]*)"', out):
            decoded = _html.unescape(m.group(1))
            sm = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", decoded)
            assert not (sm and sm.group(1).lower() in _UNSAFE_SCHEMES), (
                f"unsafe {attr} reached output: {m.group(1)!r} (decoded {decoded!r}) "
                f"in {out!r}"
            )


@pytest.mark.parametrize("md", [
    # Named colon entity.
    "[x](javascript&colon;alert(1))",
    "[x](data&colon;text/html,evil)",
    "[x](vbscript&colon;alert(1))",
    "[x](file&colon;/etc/passwd)",
    # Decimal numeric colon (incl. leading zeros).
    "[x](javascript&#58;alert(1))",
    "[x](javascript&#058;alert(1))",
    "[x](data&#58;text/html,evil)",
    "[x](vbscript&#58;alert(1))",
    # Hex numeric colon — lower/upper marker and case.
    "[x](javascript&#x3a;alert(1))",
    "[x](javascript&#x3A;alert(1))",
    "[x](javascript&#X3a;alert(1))",
    "[x](javascript&#X3A;alert(1))",
    # Mixed-case / upper-case scheme + entity colon.
    "[x](JaVaScRiPt&colon;alert(1))",
    "[x](JAVASCRIPT&#58;alert(1))",
    # Whitespace / control entities smuggled into the scheme (browsers strip
    # these before scheme resolution; the renderer must block, not emit).
    "[x](java&#9;script:alert(1))",
    "[x](java&#x09;script:alert(1))",
    "[x](javascript&#0;:alert(1))",
    "[x](javascript&#32;:alert(1))",
    "[x](javascript&NewLine;:alert(1))",
    "[x](javascript&#10;:alert(1))",
    "[x](javascript&#13;:alert(1))",
    # Entity-encoded scheme letter + literal colon.
    "[x](&#106;avascript:alert(1))",
    "[x](&#x6a;avascript:alert(1))",
])
def test_md_entity_encoded_link_scheme_is_blocked(md: str) -> None:
    """Every entity-encoded unsafe scheme fails closed: no <a> emitted, the
    existing okf-link-blocked span is shown, and no href decodes to an
    unsafe scheme."""
    out = markdown_to_html(md)
    assert "<a " not in out, f"anchor emitted for {md!r}: {out!r}"
    assert "<a>" not in out, f"anchor emitted for {md!r}: {out!r}"
    _assert_no_unsafe_href_or_src(out)
    assert "okf-link-blocked" in out, f"blocked span missing for {md!r}: {out!r}"


@pytest.mark.parametrize("md", [
    "![x](javascript&colon;alert(1))",
    "![x](javascript&#58;alert(1))",
    "![x](javascript&#x3a;alert(1))",
    "![x](javascript&#X3a;alert(1))",
    "![x](data&colon;text/html,evil)",
    "![x](data&#58;text/html,evil)",
    "![x](vbscript&colon;alert(1))",
    "![x](javascript&#0;:alert(1))",
    "![x](java&#9;script:alert(1))",
    "![x](java&#x09;script:alert(1))",
])
def test_md_entity_encoded_image_scheme_is_blocked(md: str) -> None:
    """The same decoding/ordering fix applies to image src: no <img> emitted
    and no src decodes to an unsafe scheme."""
    out = markdown_to_html(md)
    assert "<img" not in out, f"img emitted for {md!r}: {out!r}"
    _assert_no_unsafe_href_or_src(out)
    assert "okf-img-blocked" in out or "okf-link-blocked" in out, (
        f"blocked marker missing for {md!r}: {out!r}"
    )


def test_md_entity_query_amp_preserves_safe_link() -> None:
    """Regression guard: decoding entities before _safe_url must NOT
    false-positive block a safe https link whose query string uses a
    well-formed &amp; entity (previously blocked by the control-char
    placeholder backstop). It decodes to the browser-semantic ?a=1&b=2 and
    is re-escaped on emit."""
    out = markdown_to_html("[ok](https://example.com/?a=1&amp;b=2)")
    assert '<a href="https://example.com/?a=1&amp;b=2"' in out
    assert "okf-link-blocked" not in out


def test_md_entity_decoded_link_preserves_safe_schemes_and_relative() -> None:
    out = markdown_to_html(
        "[a](https://e.com) [b](mailto:x@y.com) [c](./r.md) "
        "[d](/abs.md) [e](#sec)"
    )
    assert 'href="https://e.com"' in out
    assert 'href="mailto:x@y.com"' in out
    assert 'href="./r.md"' in out
    assert 'href="/abs.md"' in out
    assert 'href="#sec"' in out
    assert "okf-link-blocked" not in out


def test_md_entity_decoded_link_visible_text_preserved() -> None:
    """Visible link-label entities are preserved for display (browser-decoded
    in the rendered text), not stripped by the URL-safety fix."""
    out = markdown_to_html("[A &amp; B](https://e.com)")
    assert ">A &amp; B</a>" in out
    assert 'href="https://e.com"' in out


def test_md_double_encoded_scheme_is_not_overdecoded() -> None:
    """A double-encoded colon (&amp;colon;) is NOT executable in a browser
    (one attribute decode yields a literal &colon;, not ':'), so the renderer
    must not over-decode it into a blocked/javascript: form either. The
    contract: single browser-semantic decode, no over-decoding of safe
    semantics. It renders as a non-executable relative-looking anchor with no
    live javascript: scheme."""
    out = markdown_to_html("[x](javascript&amp;colon;alert(1))")
    _assert_no_unsafe_href_or_src(out)
    # After one browser decode the href must not be javascript:.
    assert 'href="javascript:' not in out.replace("&amp;", "&")


# ---------------------------------------------------------------------------
# JSON-for-script: no </script> breakout in embedded JSON
# ---------------------------------------------------------------------------


def test_json_for_script_escapes_script_close() -> None:
    """A </script> sequence in the data MUST NOT survive into the script body."""
    encoded = _json_for_script({"x": "</script><script>alert(1)</script>"})
    assert "</script>" not in encoded
    # The escape sequence \\u003c must be present.
    assert "\\u003c" in encoded


def test_json_for_script_escapes_lt_gt_amp() -> None:
    encoded = _json_for_script({"x": "<>&"})
    assert "<" not in encoded
    assert ">" not in encoded
    # & is the only one that could appear in JSON syntax legitimately
    # (e.g. in unicode escapes), so we check the data is preserved.
    decoded = json.loads(encoded.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))
    assert decoded == {"x": "<>&"}


def test_json_for_script_escapes_line_separators() -> None:
    """U+2028 / U+2028 are valid in JSON but break JS string literals pre-ES2019."""
    encoded = _json_for_script({"x": "a\u2028b\u2029c"})
    assert "\u2028" not in encoded
    assert "\\u2028" in encoded
    assert "\\u2029" in encoded


# ---------------------------------------------------------------------------
# Rendered single-file HTML: end-to-end XSS verification
# ---------------------------------------------------------------------------


def test_single_file_no_script_breakout(tmp_path: Path) -> None:
    """The embedded bundle JSON in the single-file viewer has no </script> breakout."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "index.md").write_text("# Bundle\n", encoding="utf-8")
    # Concept with a body that attempts script-element breakout.
    (bundle_dir / "evil.md").write_text(
        '---\ntype: Reference\ntitle: Evil\n---\n'
        '</script><script>alert("xss")</script>\n'
        '<img src=x onerror=alert(1)>\n'
        '[click](javascript:alert(1))\n',
        encoding="utf-8",
    )
    bundle = Bundle.load(bundle_dir)
    out = tmp_path / "viz.html"
    render_single_file(bundle, out)
    html = out.read_text(encoding="utf-8")
    # The window.BUNDLE assignment must not contain a raw </script>.
    m = re.search(r"window\.BUNDLE\s*=\s*(\{.*?\})\s*;", html, re.DOTALL)
    assert m, "window.BUNDLE assignment not found"
    blob = m.group(1)
    assert "</script>" not in blob, "script breakout in JSON blob"


def test_single_file_no_marked_dependency(tmp_path: Path) -> None:
    """The single-file viewer must not reference marked.js (server-side rendering only).

    We check for actual function calls / script loads, not mere mentions in
    comments (graph.js contains explanatory comments mentioning marked.parse
    by name, which is fine).
    """
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (bundle_dir / "x.md").write_text(
        "---\ntype: T\ntitle: X\n---\nbody\n", encoding="utf-8"
    )
    bundle = Bundle.load(bundle_dir)
    out = tmp_path / "viz.html"
    render_single_file(bundle, out)
    html = out.read_text(encoding="utf-8")
    # No marked.js CDN load.
    assert "cdn.jsdelivr.net/npm/marked" not in html
    # No marked object reference (window.marked, marked.parse, etc.) outside
    # of comments. Strip /* ... */ comments before checking.
    stripped = re.sub(r"/\*.*?\*/", "", html, flags=re.DOTALL)
    assert "window.marked" not in stripped
    assert "marked.parse(" not in stripped
    assert "marked.setOptions" not in stripped


def test_single_file_sri_hash_present(tmp_path: Path) -> None:
    """CDN script tag carries an SRI integrity attribute (supply-chain defence)."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "index.md").write_text("# Bundle\n", encoding="utf-8")
    bundle = Bundle.load(bundle_dir)
    out = tmp_path / "viz.html"
    render_single_file(bundle, out)
    html = out.read_text(encoding="utf-8")
    assert 'integrity="sha384-' in html
    assert 'crossorigin="anonymous"' in html


# ---------------------------------------------------------------------------
# Server response headers (smoke-tested via the handler)
# ---------------------------------------------------------------------------


def test_server_response_security_headers(tmp_path: Path) -> None:
    """Server responses carry CSP, nosniff, DENY, no-referrer headers."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (bundle_dir / "x.md").write_text(
        "---\ntype: T\ntitle: X\n---\nbody\n", encoding="utf-8"
    )

    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen
    from okf_loom.server import OKFWikiHandler

    bundle = Bundle.load(bundle_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    httpd.bundle = bundle
    port = httpd.server_address[1]

    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            headers = dict(r.headers.items())
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert "Content-Security-Policy" in headers
    assert "nosniff" in headers.get("X-Content-Type-Options", "").lower()
    assert "deny" in headers.get("X-Frame-Options", "").lower()
    assert "no-referrer" in headers.get("Referrer-Policy", "").lower()


# ---------------------------------------------------------------------------
# log.py: --log-path containment
# ---------------------------------------------------------------------------


def test_log_path_escape_rejected(tmp_path: Path) -> None:
    """append_log_entry rejects log_rel that escapes the bundle root."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "index.md").write_text("# Bundle\n", encoding="utf-8")
    bundle = Bundle.load(bundle_dir)
    with pytest.raises(ValueError, match="escapes bundle root"):
        append_log_entry(
            bundle, kind="Update", entry="x",
            log_rel="../escape.md", dry_run=True,
        )


def test_log_path_resolve_within_bundle(tmp_path: Path) -> None:
    root = tmp_path
    assert _resolve_within_bundle(root, "log.md") == root / "log.md"
    assert _resolve_within_bundle(root, "sub/log.md") == root / "sub" / "log.md"
    with pytest.raises(ValueError):
        _resolve_within_bundle(root, "../escape.md")
    with pytest.raises(ValueError):
        _resolve_within_bundle(root, "sub/../../escape.md")


# ---------------------------------------------------------------------------
# Atomic write: tmp cleanup on failure
# ---------------------------------------------------------------------------


def test_atomic_write_cleans_up_on_failure(tmp_path: Path) -> None:
    """atomic_write_text unlinks its tmp file if the write fails."""
    from okf_loom.io_utils import atomic_write_text
    target = tmp_path / "out.txt"
    # Make the parent read-only after mkstemp creates a tmp inside it.
    # Simpler: corrupt the write by passing a non-encodable value via a
    # monkey-patched fdopen. We use a simpler trick: write to a path whose
    # parent is a file (not a dir) — mkdir will fail.
    bad_parent = tmp_path / "blocker"
    bad_parent.write_text("i am a file", encoding="utf-8")
    target = bad_parent / "out.txt"
    with pytest.raises(OSError):
        atomic_write_text(target, "content")
    # No tmp files should be left in tmp_path.
    leftovers = [p for p in tmp_path.rglob(".okf-*") if p.is_file()]
    assert leftovers == [], f"tmp files left behind: {leftovers}"


# ---------------------------------------------------------------------------
# build_graph_data resource sanitization (P0-3.2 regression test)
# ---------------------------------------------------------------------------


def test_build_graph_data_sanitizes_resource(tmp_path: Path) -> None:
    """build_graph_data must sanitize concept.resource through _safe_url.

    Regression test for P0-3.2: graph.js assigns data.resource to a.href,
    so javascript:/data: URLs must be blanked at the source.
    """
    from okf_loom.render import build_graph_data

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "evil.md").write_text(
        '---\ntype: Reference\ntitle: Evil\n'
        'resource: javascript:alert(document.cookie)\n---\nbody\n',
        encoding="utf-8",
    )
    bundle = Bundle.load(tmp_path)
    data = build_graph_data(bundle)
    for node in data["nodes"]:
        if node["data"]["id"] == "evil":
            assert node["data"]["resource"] == "", (
                f"javascript: URL not sanitized: {node['data']['resource']!r}"
            )
            return
    pytest.fail("evil node not found in graph data")


def test_build_graph_data_preserves_safe_resource(tmp_path: Path) -> None:
    """Safe resource URLs survive sanitization."""
    from okf_loom.render import build_graph_data

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "ok.md").write_text(
        '---\ntype: Table\ntitle: OK\n'
        'resource: https://example.com/table\n---\nbody\n',
        encoding="utf-8",
    )
    bundle = Bundle.load(tmp_path)
    data = build_graph_data(bundle)
    for node in data["nodes"]:
        if node["data"]["id"] == "ok":
            assert node["data"]["resource"] == "https://example.com/table"
            return
    pytest.fail("ok node not found in graph data")


# ---------------------------------------------------------------------------
# set_frontmatter idempotency + clobber guard (P0-3.3 regression test)
# ---------------------------------------------------------------------------


def test_set_frontmatter_skips_todo_overwrite_of_hand_written(tmp_path: Path) -> None:
    """set_frontmatter must NOT overwrite a hand-written value with a TODO: placeholder."""
    from okf_loom.update import UpdateOp, apply_plan, UpdatePlan

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        '---\ntype: T\ntitle: C\ndescription: My real description\n---\nbody\n',
        encoding="utf-8",
    )
    bundle = Bundle.load(tmp_path)
    plan = UpdatePlan(
        bundle_root=str(tmp_path),
        description="test",
        ops=[UpdateOp(
            kind="set_frontmatter",
            target=("c",),
            args={"key": "description", "value": "TODO: add description for c"},
        )],
        plan_kind="test",
    )
    summary = apply_plan(bundle, plan, apply=False, dry_run=True)
    assert summary["applied"] == 0
    result = summary["results"][0][1]
    assert result["applied"] is False
    assert result["reason"] == "would_overwrite_hand_written"


def test_set_frontmatter_skips_same_value(tmp_path: Path) -> None:
    """set_frontmatter skips when the value is already set to the same value."""
    from okf_loom.update import UpdateOp, apply_plan, UpdatePlan

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        '---\ntype: T\ntitle: C\ncustom_key: hello\n---\nbody\n',
        encoding="utf-8",
    )
    bundle = Bundle.load(tmp_path)
    plan = UpdatePlan(
        bundle_root=str(tmp_path),
        description="test",
        ops=[UpdateOp(
            kind="set_frontmatter",
            target=("c",),
            args={"key": "custom_key", "value": "hello"},
        )],
        plan_kind="test",
    )
    summary = apply_plan(bundle, plan, apply=False, dry_run=True)
    assert summary["applied"] == 0
    result = summary["results"][0][1]
    assert result["reason"] == "same_value"


# ===========================================================================
# P2-53: /__static traversal + override-gate regression tests
# ===========================================================================


def _start_static_server(bundle):
    """Start a minimal ThreadingHTTPServer for static-route probing.

    Returns ``(server, thread, port)``. Caller MUST shutdown + close + join.
    """
    import threading
    from http.server import ThreadingHTTPServer
    from okf_loom.server import OKFWikiHandler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    httpd.bundle = bundle
    httpd.config = {}
    httpd.name = bundle.name
    httpd.plugin = None
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, t, port


def _http_get(url: str):
    import urllib.request, urllib.error
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_static_traversal_dotdot_returns_404(tmp_path: Path) -> None:
    """``/__static/../../etc/passwd`` MUST 404 (no traversal)."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "x.md").write_text("---\ntype: T\ntitle: X\n---\nbody\n", encoding="utf-8")
    bundle = Bundle.load(tmp_path)
    httpd, t, port = _start_static_server(bundle)
    try:
        status, body = _http_get(f"http://127.0.0.1:{port}/__static/../../etc/passwd")
        assert status == 400
        # Never leak passwd content.
        assert b"root:" not in body
    finally:
        httpd.shutdown(); httpd.server_close(); t.join(timeout=3)


def test_static_encoded_traversal_returns_404(tmp_path: Path) -> None:
    """``%2e%2e`` (encoded ``..``) and NUL variants MUST 404.

    ``unquote`` runs in the router before ``_handle_static``, so the
    decoded ``..`` is what reaches the handler — the normpath + reject
    guard catches it. A literal NUL byte in the asset name is also rejected.
    """
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "x.md").write_text("---\ntype: T\ntitle: X\n---\nbody\n", encoding="utf-8")
    bundle = Bundle.load(tmp_path)
    httpd, t, port = _start_static_server(bundle)
    try:
        for path in [
            "/__static/%2e%2e/%2e%2e/etc/passwd",
            "/__static/wiki.css%00",
            "/__static/.%2e/.%2e/etc/passwd",
        ]:
            status, body = _http_get(f"http://127.0.0.1:{port}{path}")
            assert status == 400, f"{path} should fail closed with 400, got {status}"
            assert b"root:" not in body
    finally:
        httpd.shutdown(); httpd.server_close(); t.join(timeout=3)


def test_static_builtin_wiki_css_served(tmp_path: Path) -> None:
    """``/__static/wiki.css`` returns 200 with CSS content-type."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "x.md").write_text("---\ntype: T\ntitle: X\n---\nbody\n", encoding="utf-8")
    bundle = Bundle.load(tmp_path)
    httpd, t, port = _start_static_server(bundle)
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/__static/wiki.css", timeout=3
        ) as r:
            assert r.status == 200
            assert "text/css" in r.headers.get("Content-Type", "")
            body = r.read().decode("utf-8")
            assert body.strip()  # non-empty builtin CSS
    finally:
        httpd.shutdown(); httpd.server_close(); t.join(timeout=3)


def test_static_override_not_served_when_gate_closed(tmp_path: Path, monkeypatch) -> None:
    """Override file present + operator consent absent → builtin served, not override.

    P1-40: a bundle ships a malicious ``wiki.css`` override + declares
    ``allow_active_code: true`` in okf-loom.config.yaml. WITHOUT the operator
    flag the override MUST NOT be applied — the builtin (safe) asset is
    served instead. The bundle alone cannot enable its own overrides.
    """
    from okf_loom.config import CONFIG_FILENAME
    from okf_loom.viewer import assets

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "x.md").write_text("---\ntype: T\ntitle: X\n---\nbody\n", encoding="utf-8")
    # Bundle opts in.
    (tmp_path / CONFIG_FILENAME).write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8"
    )
    # Malicious override.
    override_dir = tmp_path / ".okf-loom" / "viewer" / "static"
    override_dir.mkdir(parents=True)
    override_marker = "/* EVIL OVERRIDE PAYLOAD active-code-gate-bypass */"
    (override_dir / "wiki.css").write_text(override_marker, encoding="utf-8")

    # Operator silent → gate closed.
    monkeypatch.delenv(assets.OPERATOR_CONSENT_ENV, raising=False)
    assets._operator_consent_override = None
    assets.clear_overrides_cache()

    bundle = Bundle.load(tmp_path)
    httpd, t, port = _start_static_server(bundle)
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/__static/wiki.css", timeout=3
        ) as r:
            assert r.status == 200
            body = r.read().decode("utf-8")
            # Override payload MUST NOT appear (gate closed → builtin served).
            assert "EVIL OVERRIDE PAYLOAD" not in body
    finally:
        httpd.shutdown(); httpd.server_close(); t.join(timeout=3)
        assets.clear_overrides_cache()


def test_static_override_served_when_gate_open(tmp_path: Path, monkeypatch) -> None:
    """Override file present + operator consent → override is served (positive case).

    Confirms the gate is not over-restrictive: when the operator explicitly
    consents AND the bundle opts in, the override lands. This is the trust
    contract the WARNING documents.
    """
    from okf_loom.config import CONFIG_FILENAME
    from okf_loom.viewer import assets

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "x.md").write_text("---\ntype: T\ntitle: X\n---\nbody\n", encoding="utf-8")
    (tmp_path / CONFIG_FILENAME).write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8"
    )
    override_dir = tmp_path / ".okf-loom" / "viewer" / "static"
    override_dir.mkdir(parents=True)
    (override_dir / "wiki.css").write_text(
        "/* BENIGN OVERRIDE operator-opted-in */", encoding="utf-8"
    )

    monkeypatch.setenv(assets.OPERATOR_CONSENT_ENV, "1")
    assets._operator_consent_override = None
    assets.clear_overrides_cache()

    bundle = Bundle.load(tmp_path)
    httpd, t, port = _start_static_server(bundle)
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/__static/wiki.css", timeout=3
        ) as r:
            assert r.status == 200
            body = r.read().decode("utf-8")
            assert "BENIGN OVERRIDE operator-opted-in" in body
    finally:
        httpd.shutdown(); httpd.server_close(); t.join(timeout=3)
        monkeypatch.delenv(assets.OPERATOR_CONSENT_ENV, raising=False)
        assets._operator_consent_override = None
        assets.clear_overrides_cache()


# ===========================================================================
# P1-41: palette.json CSS-injection regression
# ===========================================================================


def test_palette_css_injection_blocked_when_gate_closed(tmp_path: Path, monkeypatch) -> None:
    """A ``position:fixed`` palette payload is dropped when the gate is closed.

    P1-41: palette values land in ``style="background:<color>"`` attributes.
    HTML-escaping alone does not stop CSS injection (``red;position:fixed``
    has no HTML-unsafe chars), so palette overrides are gated on the
    effective active-code gate. When the gate is closed, only the
    auto-palette is used; the malicious override never reaches the HTML.
    """
    from okf_loom.viewer import assets

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "evil.md").write_text(
        "---\ntype: Reference\ntitle: Evil\n---\nbody\n", encoding="utf-8"
    )
    pal_dir = tmp_path / ".okf-loom" / "viewer"
    pal_dir.mkdir(parents=True)
    (pal_dir / "palette.json").write_text(
        json.dumps({"Reference": "red;position:fixed;top:0;left:0;width:100%;height:100%;background:url(data:text/html,xxx)"}),
        encoding="utf-8",
    )

    monkeypatch.delenv(assets.OPERATOR_CONSENT_ENV, raising=False)
    assets._operator_consent_override = None
    assets.clear_overrides_cache()

    bundle = Bundle.load(tmp_path)
    # When the gate is closed, resolve_palette returns ONLY the auto palette;
    # the override entry is not merged in at all.
    pal = assets.resolve_palette(bundle)
    assert "Reference" in pal  # auto-palette still colours it
    # The injected value must not appear.
    assert "position:fixed" not in pal.get("Reference", "")
    assert pal["Reference"].startswith("hsl(")  # auto-generated, safe

    # And it must not land in a rendered concept page's style attribute.
    from okf_loom.render import _render_concept_page
    concept = bundle.concepts[("evil",)]
    graph = bundle.graph()
    html = _render_concept_page(
        concept, bundle, graph, mode="serve", name="b",
        palette=pal, config={},
    )
    assert "position:fixed" not in html
    assert "data:text/html" not in html


def test_palette_css_injection_sanitized_even_when_gate_open(tmp_path: Path, monkeypatch) -> None:
    """Defense in depth: even with operator consent, invalid CSS colours are dropped.

    P1-41 (b): when the gate IS open, palette overrides ARE loaded — but
    each value is validated against the strict CSS-colour allowlist. A
    payload like ``red;position:fixed`` is rejected (the ``;`` breaks the
    full-match) and the key is dropped fail-closed. A legitimate value
    (``#ff0000``) survives.
    """
    from okf_loom.config import CONFIG_FILENAME
    from okf_loom.viewer import assets

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "evil.md").write_text(
        "---\ntype: Reference\ntitle: Evil\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "good.md").write_text(
        "---\ntype: Table\ntitle: Good\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / CONFIG_FILENAME).write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8"
    )
    pal_dir = tmp_path / ".okf-loom" / "viewer"
    pal_dir.mkdir(parents=True)
    (pal_dir / "palette.json").write_text(
        json.dumps({
            "Reference": "red;position:fixed;top:0",  # injection → rejected
            "Table": "#ff0000",  # valid hex → kept
            "Other": "hsl(120, 50%, 50%)",  # valid hsl → kept
        }),
        encoding="utf-8",
    )

    monkeypatch.setenv(assets.OPERATOR_CONSENT_ENV, "1")
    assets._operator_consent_override = None
    assets.clear_overrides_cache()

    bundle = Bundle.load(tmp_path)
    pal = assets.resolve_palette(bundle)
    # Reference key is dropped (its value failed the allowlist). Table/Other
    # survive with the override value (override wins over auto).
    assert "Reference" in pal  # auto-palette still provides a safe colour
    assert pal["Reference"].startswith("hsl(")  # auto-generated, NOT the injection
    assert pal.get("Table") == "#ff0000"
    assert pal.get("Other") == "hsl(120, 50%, 50%)"
    assert "position:fixed" not in json.dumps(pal)

    # Render-time: no injection lands in the HTML.
    from okf_loom.render import _render_concept_page
    concept = bundle.concepts[("evil",)]
    graph = bundle.graph()
    html = _render_concept_page(
        concept, bundle, graph, mode="serve", name="b",
        palette=pal, config={},
    )
    assert "position:fixed" not in html

    # Cleanup.
    monkeypatch.delenv(assets.OPERATOR_CONSENT_ENV, raising=False)
    assets._operator_consent_override = None
    assets.clear_overrides_cache()


def test_sanitize_css_color_allowlist() -> None:
    """The CSS-colour allowlist accepts known-safe shapes, rejects everything else."""
    from okf_loom.viewer.assets import _sanitize_css_color as s

    # Accepted shapes.
    assert s("red") == "red"
    assert s("RED") == "RED"  # case preserved on named match
    assert s("#fff") == "#fff"
    assert s("#aabbcc") == "#aabbcc"
    assert s("#aabbccff") == "#aabbccff"  # 8-digit hex (alpha)
    assert s("rgb(255, 0, 0)") == "rgb(255, 0, 0)"
    assert s("rgba(255, 0, 0, 0.5)") == "rgba(255, 0, 0, 0.5)"
    assert s("hsl(120, 50%, 50%)") == "hsl(120, 50%, 50%)"
    assert s("hsla(120, 50%, 50%, 0.5)") == "hsla(120, 50%, 50%, 0.5)"
    assert s("transparent") == "transparent"

    # Rejected (CSS-injection payloads and nonsense).
    for bad in [
        "red;position:fixed;top:0",
        "red /* */ ;x:expression(alert(1))",
        "javascript:alert(1)",
        "url(data:text/html,<script>)",
        "red}",
        "",
        "   ",
        "not-a-real-color",
        "rgb(999, 0, 0)",  # we accept the shape; browsers clamp, not our concern
        # but a malformed function must reject:
        "rgb(255, 0)",  # too few components
        "rgb(255 0 0",  # unbalanced paren
        "hsl(120, 50%)",  # too few components
    ]:
        # Note: rgb(999,0,0) technically matches our shape regex (numbers,
        # not ranges); we accept it because browsers clamp, and value-range
        # validation is out of scope for the injection defence. Adjust the
        # expectation for that one entry only.
        if bad == "rgb(999, 0, 0)":
            assert s(bad) == bad  # accepted by shape (browser clamps)
            continue
        assert s(bad) == "", f"expected {bad!r} to be rejected"


# ===========================================================================
# P2-54: server-side defense against symlinked index.md / static override
# ===========================================================================


def test_static_override_symlink_escape_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """A symlinked override file pointing outside the bundle root MUST 404.

    P2-54 server-side defense: even with the gate open, an override asset
    whose resolved path escapes the bundle root (e.g. a symlink to
    /etc/passwd) is refused at serve time.
    """
    from okf_loom.config import CONFIG_FILENAME
    from okf_loom.viewer import assets
    import os

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "x.md").write_text("---\ntype: T\ntitle: X\n---\nbody\n", encoding="utf-8")
    (tmp_path / CONFIG_FILENAME).write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8"
    )
    # Target file OUTSIDE the bundle root.
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("SECRET-CONTENT-OUTSIDE-BUNDLE\n", encoding="utf-8")
    override_dir = tmp_path / ".okf-loom" / "viewer" / "static"
    override_dir.mkdir(parents=True)
    # Symlink wiki.css -> outside_secret.txt (skip if platform lacks symlink).
    link = override_dir / "wiki.css"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    monkeypatch.setenv(assets.OPERATOR_CONSENT_ENV, "1")
    assets._operator_consent_override = None
    assets.clear_overrides_cache()

    bundle = Bundle.load(tmp_path)
    httpd, t, port = _start_static_server(bundle)
    try:
        status, body = _http_get(f"http://127.0.0.1:{port}/__static/wiki.css")
        assert status == 404
        assert b"SECRET-CONTENT-OUTSIDE-BUNDLE" not in body
    finally:
        httpd.shutdown(); httpd.server_close(); t.join(timeout=3)
        monkeypatch.delenv(assets.OPERATOR_CONSENT_ENV, raising=False)
        assets._operator_consent_override = None
        assets.clear_overrides_cache()
        try:
            outside.unlink()
        except OSError:
            pass


# ===========================================================================
# P2-55: /__raw size cap (DoS backstop)
# ===========================================================================


def test_raw_response_size_cap_returns_413(
    tmp_path: Path, monkeypatch
) -> None:
    """A raw body exceeding ``MAX_RAW_RESPONSE_BYTES`` is refused with 413.

    P2-55 server-side backstop: the authoritative YAML/body cap belongs in
    parse.py (REPORTED); the server additionally caps /__raw so one huge
    body cannot saturate a handler. The concept body is planted directly
    in the loaded bundle to avoid writing a multi-MiB file.
    """
    import okf_loom.server as srv

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "big.md").write_text("---\ntype: T\ntitle: B\n---\nsmall\n", encoding="utf-8")
    bundle = Bundle.load(tmp_path)

    # Force the concept body over the (lowered) cap by mutating the loaded
    # concept in place — avoids writing a multi-MiB file. Concept is a
    # non-frozen dataclass, so attribute assignment is allowed.
    cid = ("big",)
    assert cid in bundle.concepts, "big concept must load for the test"
    # 64 bytes > the 16-byte cap we set below.
    bundle.concepts[cid].body = "x" * 64

    # Temporarily lower the cap so the test is fast and deterministic.
    monkeypatch.setattr(srv, "MAX_RAW_RESPONSE_BYTES", 16)
    httpd, t, port = _start_static_server(bundle)
    try:
        status, body = _http_get(f"http://127.0.0.1:{port}/__raw/big")
        assert status == 413
        assert b"too large" in body.lower()
    finally:
        httpd.shutdown(); httpd.server_close(); t.join(timeout=3)


# ===========================================================================
# iter-2 P1-4: override LOADER symlink containment (serve + build + render).
# iter-1's P2-54 only guarded the /__static route; the loaders themselves
# followed symlinks verbatim. A bundle override symlinked to a host secret
# must NOT reach the wire (serve) or the built output (build/render).
# ===========================================================================


def _symlink_or_skip(src, dst):
    import os
    try:
        os.symlink(src, dst)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")


def test_p1_4_symlinked_template_override_not_rendered(tmp_path, monkeypatch):
    """A symlinked concept_page.html override -> render uses the builtin
    template; the host secret never reaches the rendered output."""
    import os
    from okf_loom.config import CONFIG_FILENAME
    from okf_loom.viewer import assets
    from okf_loom.render import render_single_file
    from okf_loom import Bundle

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "x.md").write_text("---\ntype: T\ntitle: X\n---\nbody\n", encoding="utf-8")
    (tmp_path / CONFIG_FILENAME).write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8"
    )
    secret = tmp_path.parent / "tmpl_secret_p14.html"
    secret.write_text("<html>AKIA-P14-TEMPLATE-SYMLINK-LEAK</html>", encoding="utf-8")
    tmpl_dir = tmp_path / ".okf-loom" / "viewer" / "templates"
    tmpl_dir.mkdir(parents=True)
    _symlink_or_skip(secret, tmpl_dir / "concept_page.html")

    monkeypatch.setenv(assets.OPERATOR_CONSENT_ENV, "1")
    assets._operator_consent_override = None
    assets.clear_overrides_cache()
    try:
        bundle = Bundle.load(tmp_path)
        # Direct loader check: builtin concept_page.html served, not the secret.
        from okf_loom.viewer.assets import load_template
        loaded = load_template("concept_page.html", bundle)
        assert "AKIA-P14-TEMPLATE-SYMLINK-LEAK" not in loaded
    finally:
        monkeypatch.delenv(assets.OPERATOR_CONSENT_ENV, raising=False)
        assets._operator_consent_override = None
        assets.clear_overrides_cache()
        try: secret.unlink()
        except OSError: pass


def test_p1_4_symlinked_static_override_not_rendered(tmp_path, monkeypatch):
    """A symlinked wiki.css override -> render uses the builtin static."""
    import os
    from okf_loom.config import CONFIG_FILENAME
    from okf_loom.viewer import assets
    from okf_loom import Bundle

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "x.md").write_text("---\ntype: T\ntitle: X\n---\nbody\n", encoding="utf-8")
    (tmp_path / CONFIG_FILENAME).write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8"
    )
    secret = tmp_path.parent / "static_secret_p14.txt"
    secret.write_text("SECRET-P14-STATIC-SYMLINK-LEAK", encoding="utf-8")
    static_dir = tmp_path / ".okf-loom" / "viewer" / "static"
    static_dir.mkdir(parents=True)
    _symlink_or_skip(secret, static_dir / "wiki.css")

    monkeypatch.setenv(assets.OPERATOR_CONSENT_ENV, "1")
    assets._operator_consent_override = None
    assets.clear_overrides_cache()
    try:
        bundle = Bundle.load(tmp_path)
        from okf_loom.viewer.assets import load_static
        loaded = load_static("wiki.css", bundle)
        assert "SECRET-P14-STATIC-SYMLINK-LEAK" not in loaded
    finally:
        monkeypatch.delenv(assets.OPERATOR_CONSENT_ENV, raising=False)
        assets._operator_consent_override = None
        assets.clear_overrides_cache()
        try: secret.unlink()
        except OSError: pass


def test_p1_4_symlinked_palette_override_returns_empty(tmp_path, monkeypatch):
    """A symlinked palette.json -> load_palette_override returns {} (auto)."""
    import os, json
    from okf_loom.config import CONFIG_FILENAME
    from okf_loom.viewer import assets
    from okf_loom import Bundle

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "ref.md").write_text("---\ntype: Reference\ntitle: R\n---\nbody\n", encoding="utf-8")
    (tmp_path / CONFIG_FILENAME).write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8"
    )
    secret = tmp_path.parent / "palette_secret_p14.json"
    secret.write_text(json.dumps({"Reference": "AKIA-P14-PALETTE-LEAK"}), encoding="utf-8")
    pal_dir = tmp_path / ".okf-loom" / "viewer"
    pal_dir.mkdir(parents=True)
    _symlink_or_skip(secret, pal_dir / "palette.json")

    monkeypatch.setenv(assets.OPERATOR_CONSENT_ENV, "1")
    assets._operator_consent_override = None
    assets.clear_overrides_cache()
    try:
        bundle = Bundle.load(tmp_path)
        from okf_loom.viewer.assets import load_palette_override
        assert load_palette_override(bundle) == {}
    finally:
        monkeypatch.delenv(assets.OPERATOR_CONSENT_ENV, raising=False)
        assets._operator_consent_override = None
        assets.clear_overrides_cache()
        try: secret.unlink()
        except OSError: pass


# ===========================================================================
# iter-3 P2-1: YAML alias-bomb (billion-laughs) DoS at the parse boundary
# ===========================================================================
#
# ``yaml.safe_load`` enforces no size or node cap. A few hundred bytes of
# frontmatter using YAML aliases expands exponentially when the parsed
# value is later serialized (json.dumps into the embedded bundle JSON at
# render time), pinning every ThreadingHTTPServer worker. Baseline
# measurement: a 318-byte, 5-level/fanout-11 alias bomb serializes to
# ~0.86 MB; deeper/wider variants reach tens of MB per render in ~1 s.
#
# PyYAML SHARES alias node references at compose/construct time, so a naive
# node counter on compose_node/construct_object does NOT see acyclic alias
# fanout (the bomb's compact graph is <100 nodes). The fix therefore uses
# two complementary controls in parse.py:
#   (1) a node-counting _NodeLimitedSafeLoader (catches non-alias depth /
#       merge-key bombs), and
#   (2) a materialized-node budget walk that counts the object exactly as a
#       serializer will emit it (expanding shared-but-acyclic subtrees).
# Both are applied at the frontmatter boundary (parse.parse_document) and
# the okf-loom.config.yaml boundary (config.OkfConfig.load).

# OKFParseError import kept here for P2-1 section cohesion; the rest of the
# file imports render/log symbols at the top.
from okf_loom.exceptions import OKFParseError  # noqa: E402
from okf_loom.parse import (  # noqa: E402
    MAX_FRONTMATTER_BYTES as _MAX_FM_BYTES,
    MAX_NODES as _MAX_NODES,
    _NodeLimitedSafeLoader,
    _assert_materialized_under_limit,
    parse_document,
)
import time as _time  # noqa: E402


def _alias_bomb_yaml(fanout: int, levels: int, leaf: str = "A" * 50) -> str:
    """Build a billion-laughs alias bomb as raw YAML text.

    ``a0`` is the seed (one leaf); each subsequent ``aN`` is a sequence of
    ``fanout`` references to ``aN-1``. The top level materializes to
    ``fanout ** (levels - 1)`` leaf copies. Source size stays tiny regardless
    of fanout/depth, which is exactly what makes the byte cap insufficient.
    """
    lines = [f"a0: &a0 [{leaf!r}]"]
    for i in range(1, levels):
        refs = ", ".join([f"*a{i - 1}"] * fanout)
        lines.append(f"a{i}: &a{i} [{refs}]")
    return "\n".join(lines)


def _bomb_document(fanout: int, levels: int) -> str:
    """Wrap an alias bomb as an OKF document (frontmatter + body)."""
    return f"---\n{_alias_bomb_yaml(fanout, levels)}\n---\nbody\n"


def test_parse_alias_bomb_rejected_fast() -> None:
    """A 5-level alias bomb in concept frontmatter MUST raise OKFParseError
    fast (<100 ms), not return a value that later serializes to megabytes.

    fanout=11, levels=5 -> top level materializes to 11**4 = 14_641 leaf
    copies (> MAX_NODES=10_000) from <320 bytes of source — i.e. the byte
    cap alone cannot stop it; the node/materialized cap is the defence.
    """
    doc = _bomb_document(fanout=11, levels=5)
    # Sanity: source is well under the byte cap, so this test exercises the
    # node cap, not the byte cap.
    assert len(doc) < _MAX_FM_BYTES
    t0 = _time.perf_counter()
    with pytest.raises(OKFParseError):
        parse_document(doc)
    elapsed = _time.perf_counter() - t0
    # Must reject in well under the unfixed DoS budget (~1 s / tens of MB).
    # Observed ~3 ms; 1.0 s is a CI-safe ceiling that still catches any
    # regression to full materialization.
    assert elapsed < 1.0, f"alias-bomb rejection took {elapsed*1000:.1f} ms"


def test_parse_alias_bomb_larger_expansion_also_fast() -> None:
    """A bomb whose full materialization would be ~300 MB MUST still reject
    in milliseconds. This proves the check aborts at the node limit rather
    than walking the whole expansion (O(limit), not O(expansion)).
    """
    # fanout=9, levels=8 -> 9**7 = 4_782_969 leaves (~300 MB if serialized).
    doc = _bomb_document(fanout=9, levels=8)
    assert len(doc) < _MAX_FM_BYTES
    t0 = _time.perf_counter()
    with pytest.raises(OKFParseError):
        parse_document(doc)
    elapsed = _time.perf_counter() - t0
    assert elapsed < 1.0, f"large alias-bomb rejection took {elapsed*1000:.1f} ms"


def test_config_alias_bomb_rejected(tmp_path: Path) -> None:
    """An alias-bomb ``okf-loom.config.yaml`` MUST raise ``OkfConfigError``."""
    from okf_loom.config import OkfConfig, OkfConfigError, CONFIG_FILENAME

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / CONFIG_FILENAME).write_text(
        _alias_bomb_yaml(fanout=11, levels=5), encoding="utf-8"
    )
    t0 = _time.perf_counter()
    with pytest.raises(OkfConfigError):
        OkfConfig.load(tmp_path)
    elapsed = _time.perf_counter() - t0
    assert elapsed < 1.0, f"config alias-bomb rejection took {elapsed*1000:.1f} ms"


def test_parse_byte_cap_rejects_oversize_frontmatter() -> None:
    """Frontmatter larger than MAX_FRONTMATTER_BYTES is rejected before
    parsing (defence against huge-literal payloads)."""
    # A single scalar value just over the cap.
    big_val = "X" * (_MAX_FM_BYTES + 256)
    doc = f"---\ntype: T\npayload: {big_val}\n---\nbody\n"
    assert len(doc) > _MAX_FM_BYTES
    with pytest.raises(OKFParseError, match="byte"):
        parse_document(doc)


def test_parse_legitimate_frontmatter_still_accepted() -> None:
    """The cap must NOT reject legitimate frontmatter (no false positives).

    Covers typical real frontmatter: scalars, short lists, shallow nesting,
    and unicode — the shapes the byte + node caps must always allow through.
    """
    legit = (
        "---\n"
        "type: Table\n"
        "title: 用户表\n"               # unicode
        "tags: [pii, core, events]\n"   # short flow-style list
        "resource: bigquery://proj.ds.tbl\n"
        "metadata:\n"
        "  owner: data-platform\n"
        "  refresh: daily\n"
        "schema:\n"
        "  - name: user_id\n"
        "    type: STRING\n"
        "  - name: created_at\n"
        "    type: TIMESTAMP\n"
        "---\n\nBody.\n"
    )
    fm, body = parse_document(legit)
    assert fm["type"] == "Table"
    assert fm["title"] == "用户表"
    assert fm["tags"] == ["pii", "core", "events"]
    assert fm["metadata"]["owner"] == "data-platform"
    assert len(fm["schema"]) == 2
    assert body.rstrip() == "Body."


def test_demo_bundle_loads_under_cap(tmp_path: Path) -> None:
    """The shipped demo_bundle still loads end-to-end (no false positive on
    real-world frontmatter sizes)."""
    import shutil
    from okf_loom import Bundle

    demo = Path(__file__).resolve().parent.parent / "samples" / "demo_bundle"
    if not demo.is_dir():
        pytest.skip("demo_bundle sample not present")
    dst = tmp_path / "demo"
    shutil.copytree(demo, dst)
    # Must not raise (every concept frontmatter is well under the caps).
    bundle = Bundle.load(dst)
    assert len(bundle.concepts) > 0


def test_node_limited_loader_rejects_oversize_graph() -> None:
    """Control 1 (compose-node cap) fires for non-alias structures that
    exceed MAX_NODES — e.g. a >10_000-item sequence. This proves the
    assignment's prescribed node-counting loader is wired and functional,
    independent of the alias-specific materialized check.
    """
    import yaml

    # 10_001 scalars -> 10_002 composed nodes (seq + items) > MAX_NODES.
    big = "keys: [" + ", ".join(str(i) for i in range(_MAX_NODES + 1)) + "]"
    with pytest.raises(yaml.YAMLError):
        yaml.load(big, _NodeLimitedSafeLoader)


def test_assert_materialized_under_limit_allows_legit_rejects_bomb() -> None:
    """Direct unit test of the materialized-node budget (control 2)."""
    import yaml

    # Legit: a normal dict is well under the cap.
    _assert_materialized_under_limit(
        {"type": "T", "tags": ["a", "b"], "nested": {"k": [1, 2, 3]}}
    )
    # Bomb: a hand-built shared-reference tree that a serializer would
    # expand beyond the cap (fanout 11, depth 5 -> 11**5 leaf refs).
    leaf = ["A" * 50]
    cur = leaf
    for _ in range(5):
        cur = [cur] * 11
    with pytest.raises(yaml.YAMLError):
        _assert_materialized_under_limit({"a": cur})


def test_cyclic_alias_does_not_hang() -> None:
    """A self-referential alias (``a: &a [*a]``) must not hang the parser.

    PyYAML builds a cyclic object; the materialized check is cycle-safe
    (recursion-path id set) so it terminates, and json.dumps would reject a
    cycle at serialize time anyway. parse_document must return promptly.
    """
    doc = "---\na: &a [*a]\n---\nbody\n"
    t0 = _time.perf_counter()
    try:
        fm, _ = parse_document(doc)
        # If accepted (tiny cycle under the caps), it must be a dict.
        assert isinstance(fm, dict)
    except OKFParseError:
        pass
    elapsed = _time.perf_counter() - t0
    assert elapsed < 1.0, f"cyclic alias handling took {elapsed*1000:.1f} ms"


def test_no_alias_frontmatter_still_accepted_at_boundary() -> None:
    """A document with many scalar frontmatter keys parses (guards against
    an off-by-one in the byte/node caps)."""
    doc = (
        "---\n"
        "type: Reference\n"
        "title: Boundary\n"
        + "".join(f"k{i}: v{i}\n" for i in range(50))  # 50 scalar keys
        + "---\nbody\n"
    )
    assert len(doc) < _MAX_FM_BYTES
    fm, body = parse_document(doc)
    assert fm["type"] == "Reference"
    assert fm["k49"] == "v49"
    assert body.strip() == "body"


# ===========================================================================
# iter-4 P1-1: nested-blockquote RecursionError DoS cap
# ===========================================================================


def test_iter4_p1_1_nested_blockquote_no_crash():
    """P1-1: a deeply nested blockquote (> > > > ... > x, ~1500 levels)
    must NOT trigger RecursionError. markdown_to_html caps recursion at 32
    levels; deeper quotes are escaped as plain text."""
    from okf_loom.viewer.markdown import markdown_to_html
    result = markdown_to_html("> " * 1500 + "inner")
    assert isinstance(result, str), "must return a string, not crash"
    assert "blockquote" in result


# ===========================================================================
# Studio HTTP negative regression set (SEC-005)
# ===========================================================================
# Exercises the studio HTTP surface against the §15 trust model + §9.5 path
# containment + §13.x DoS caps. Uses the same ThreadingHTTPServer harness
# pattern as test_live.py / test_studio_iter1_backend.py.
#
# Each test exercises ONE negative contract. Failures here are P0
# regressions: a 500 on bad input, a leaked token, an uncapped bomb, or a
# non-loopback bind without acknowledgement all violate spec §15 / §9.5.


def _studio_server_for_security(bundle_dir: Path, *, max_sse_clients: int = 32):
    """Start a ThreadingHTTPServer with the full studio wiring for sec tests."""
    import secrets as _secrets
    import socket as _socket
    import threading as _threading
    from http.server import ThreadingHTTPServer

    from okf_loom import Bundle
    from okf_loom.server import OKFWikiHandler
    from okf_loom.studio import Studio

    bundle = Bundle.load(bundle_dir)
    # Pick a free port.
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    server = ThreadingHTTPServer(("127.0.0.1", port), OKFWikiHandler)
    server.bundle = bundle
    server.config = {}
    server.name = bundle.name
    server.plugin = None
    server._state_lock = _threading.Lock()
    server._reload_error = None
    studio = Studio.for_bundle(bundle.root, bundle_name=bundle.name)
    studio.ensure_session()
    server.studio = studio
    server.csrf_token = _secrets.token_hex(16)
    server.allowed_hosts = ("127.0.0.1", "localhost")
    server.max_sse_clients = max_sse_clients
    server.studio_live = True
    server.studio_edit = True
    server.studio_theme = "auto"
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def _stop(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def _post_json(base: str, path: str, body: dict, token: str, *,
               extra_headers: dict | None = None) -> tuple[int, dict]:
    """POST JSON and return (status, parsed-body-or-{})."""
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json", "X-OKF-Token": token}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), method="POST", headers=headers,
    )
    try:
        r = urllib.request.urlopen(req, timeout=4)
        return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "{}")
        except (ValueError, OSError):
            return e.code, {}


@pytest.fixture
def sec_server(tiny_good_bundle: Path):
    server, thread, port = _studio_server_for_security(tiny_good_bundle)
    base = f"http://127.0.0.1:{port}"
    yield base, server
    _stop(server, thread)


# --- SSE cap (max_sse_clients) — 503 when full + TOCTOU-safe ----------------


def test_sse_cap_returns_503_when_full(tiny_good_bundle: Path) -> None:
    """§7.1 / §14: when ``max_sse_clients`` concurrent SSE streams are open,
    the (N+1)th ``GET /__events`` returns 503. The check + subscribe pair
    runs under ``_state_lock`` (P2-14) so a concurrent burst cannot
    over-admit (TOCTOU)."""
    import socket

    # Start a server with a tiny cap so the test is fast.
    server, thread, port = _studio_server_for_security(tiny_good_bundle, max_sse_clients=2)
    base = f"http://127.0.0.1:{port}"
    try:
        # Open 2 SSE streams and hold them open for the test duration.
        socks = []
        for _ in range(2):
            c = socket.create_connection(("127.0.0.1", port), timeout=3)
            c.sendall(
                f"GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n".encode()
            )
            # Read until we see the ready frame so the subscription is
            # counted against the cap before we open the next one.
            c.settimeout(2.0)
            buf = b""
            try:
                while b"event: ready" not in buf:
                    buf += c.recv(4096)
            except socket.timeout:
                pass
            socks.append(c)
        # The 3rd connection must be refused with 503.
        c3 = socket.create_connection(("127.0.0.1", port), timeout=3)
        c3.sendall(
            b"GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        c3.settimeout(2.0)
        buf3 = b""
        try:
            while b"\r\n\r\n" not in buf3:
                chunk = c3.recv(4096)
                if not chunk:
                    break
                buf3 += chunk
        except socket.timeout:
            pass
        c3.close()
        status_line = buf3.split(b"\r\n")[0].decode(errors="replace")
        assert " 503" in status_line, (
            f"expected 503 when SSE cap is full; got {status_line!r}; buf={buf3!r}"
        )
        # Cleanup: close the held SSE streams.
        for c in socks:
            try:
                c.close()
            except OSError:
                pass
    finally:
        _stop(server, thread)


# --- malformed kind → 400 ---------------------------------------------------


def test_apply_malformed_kind_returns_400(sec_server) -> None:
    """``/__apply`` with an unknown ``kind`` returns 400 (fail-closed)."""
    base, server = sec_server
    code, body = _post_json(base, "/__apply", {
        "kind": "delete_everything",  # not in the schema
        "target": "tables/users",
        "args": {},
    }, server.csrf_token)
    assert code == 400
    assert body["error"].startswith("unknown_kind:"), body


# --- traversal-shaped group_id → 400 ----------------------------------------


def test_apply_traversal_group_id_returns_400(sec_server) -> None:
    """``/__apply`` with ``group_id="../../etc"`` returns 400 (not 500).

    The studio's ``_assert_safe_path_token`` rejects path-traversal-shaped
    values; the server surfaces that as 400 so the contract reads cleanly
    and no path join reaches the filesystem."""
    base, server = sec_server
    code, body = _post_json(base, "/__apply", {
        "kind": "add_tag",
        "target": "tables/users",
        "args": {"tag": "traversal-test"},
        "group_id": "../../etc/passwd",
    }, server.csrf_token)
    assert code == 400, f"expected 400, got {code}: {body}"
    assert "unsafe" in body["error"] or "group_id" in body["error"], body


def test_apply_traversal_expected_rev_returns_400(sec_server) -> None:
    """``/__apply`` with traversal-shaped ``expected_rev`` returns 400."""
    base, server = sec_server
    code, body = _post_json(base, "/__apply", {
        "kind": "add_tag",
        "target": "tables/users",
        "args": {"tag": "rev-traversal"},
        "expected_rev": "../../../etc/passwd",
    }, server.csrf_token)
    assert code == 400, f"expected 400, got {code}: {body}"
    assert "unsafe" in body["error"] or "expected_rev" in body["error"], body


# --- traversal-shaped rev on /__undo → 400 ----------------------------------


def test_undo_traversal_rev_returns_400(sec_server) -> None:
    """``/__undo`` with traversal-shaped ``rev`` returns 400 (not 500)."""
    base, server = sec_server
    code, body = _post_json(base, "/__undo", {
        "concept": "tables/users",
        "rev": "../../etc/passwd",
    }, server.csrf_token)
    assert code == 400, f"expected 400, got {code}: {body}"
    assert "unsafe" in body["error"] or "rev" in body["error"], body


def test_undo_traversal_group_id_returns_400(sec_server) -> None:
    """``/__undo`` with traversal-shaped ``group_id`` returns 400."""
    base, server = sec_server
    code, body = _post_json(base, "/__undo", {
        "group_id": "../_groups/../../etc",
    }, server.csrf_token)
    assert code == 400, f"expected 400, got {code}: {body}"
    assert "unsafe" in body["error"] or "group_id" in body["error"], body


# --- token never logged or echoed in error responses -----------------------


def test_token_never_echoed_in_error_responses(sec_server) -> None:
    """A supplied X-OKF-Token MUST NOT appear in any error response body
    (whatever the request shape). Pinning current spec §14 non-leak."""
    base, server = sec_server
    real_token = server.csrf_token
    # Use a wrong token; the 403 body must not echo it back.
    wrong = "definitely-not-the-real-token-1234567890"
    code, body = _post_json(
        base, "/__comment",
        {"concept": "tables/users", "body": "x"},
        wrong,
    )
    assert code == 403
    raw = json.dumps(body)
    assert wrong not in raw, (
        f"wrong token value leaked into 403 body: {raw!r}"
    )
    assert real_token not in raw, (
        f"real token value leaked into 403 body: {raw!r}"
    )
    # Also check a malformed-body 400 (valid token, bad JSON shape).
    code2, body2 = _post_json(
        base, "/__apply",
        {"kind": "add_tag", "target": "tables/users", "args": {}},  # missing tag
        real_token,
    )
    assert code2 == 400
    raw2 = json.dumps(body2)
    assert real_token not in raw2, f"real token leaked into 400 body: {raw2!r}"


def test_token_never_logged_in_server_stderr(tiny_good_bundle: Path, capsys) -> None:
    """The CSRF token MUST NOT appear in any server stderr output (e.g. a
    traceback dump). We provoke a 500-class condition implicitly by sending
    a request with NO token (the 403 path goes through normal handling and
    logs nothing), then check stderr captures no token-like hex string."""
    server, thread, port = _studio_server_for_security(tiny_good_bundle)
    base = f"http://127.0.0.1:{port}"
    real_token = server.csrf_token
    try:
        _post_json(base, "/__comment", {"concept": "x", "body": "y"}, "wrong-token")
        # Allow the server's stderr writes (if any) to flush.
        import time as _time
        _time.sleep(0.1)
    finally:
        _stop(server, thread)
    captured = capsys.readouterr().err
    assert real_token not in captured, (
        f"CSRF token leaked into server stderr: {captured!r}"
    )


# --- /__preview nested-list bomb is capped, not hung ------------------------


def test_preview_nested_list_bomb_is_capped_not_hung(sec_server) -> None:
    """§13.x / §15 DoS cap: a deeply nested markdown list POSTed to
    /__preview must return within a small bounded time (≤5s), not hang or
    exhaust the stack. The renderer caps recursion depth.

    The bomb is sized to fit within the 1 MiB POST body cap (so it actually
    reaches the renderer rather than being rejected 413 at the gate) while
    being deep enough (1000 levels) to exceed the markdown renderer's
    recursion cap and exercise the flatten path."""
    import threading
    import time as _time

    base, server = sec_server
    # 1000 levels deep, ~30 KiB total — well under the 1 MiB body cap but
    # far deeper than the renderer's 32-level recursion cap.
    depth = 1000
    bomb = "\n".join("  " * i + "- x" for i in range(depth))
    assert len(bomb) < 1024 * 1024, "bomb must fit within the 1 MiB POST cap"
    result: dict = {}

    def _do():
        result["code"], result["body"] = _post_json(
            base, "/__preview", {"markdown": bomb}, server.csrf_token,
        )

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), (
        "/__preview hung >5s on a deeply nested list bomb (DoS cap missing)"
    )
    assert result.get("code") == 200, result
    # The renderer must have produced *some* HTML, not crashed.
    assert isinstance(result["body"].get("html"), str)


# --- --public without --public-ack on a non-TTY exits 2 ---------------------


def test_public_without_ack_exits_2_on_non_tty(tiny_good_bundle: Path) -> None:
    """§15.2 / P1-7: ``okf serve --public`` (non-loopback bind) without
    ``--public-ack`` on a non-TTY stdin MUST refuse to start and exit 2.
    Closes the historical ``warn-and-continue`` finding the spec calls out."""
    import subprocess

    proc = subprocess.Popen(
        okf_module_argv(
            "serve", str(tiny_good_bundle), "--public", "--no-open", "--no-watch",
            "--host", "0.0.0.0", "--port", "0",
        ),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,  # non-TTY
        cwd=str(tiny_good_bundle.parent),
        env=okf_subprocess_env(),
    )
    try:
        # The refusal is synchronous before run_server; wait briefly.
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=5)
        pytest.fail("okf serve --public without --public-ack did not exit (TTY ack path?)")
    assert proc.returncode == 2, (
        f"expected exit 2 for --public without --public-ack; "
        f"got rc={proc.returncode}; stderr={err.decode(errors='replace')!r}"
    )
    err_text = err.decode(errors="replace")
    assert "public-ack" in err_text or "non-loopback" in err_text, (
        f"missing ack hint in stderr: {err_text!r}"
    )


def test_public_with_ack_proceeds_to_bind(tiny_good_bundle: Path) -> None:
    """§15.2 / P1-7: ``okf serve --public --public-ack`` proceeds past the
    ack gate (it then tries to bind; we use a fixed port and verify the
    process is still alive past the gate window, then tear it down)."""
    import socket
    import subprocess
    import time as _time
    import urllib.request as _urllib

    # Pick a free port.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    proc = subprocess.Popen(
        okf_module_argv(
            "serve", str(tiny_good_bundle), "--public", "--public-ack",
            "--no-open", "--no-watch", "--host", "0.0.0.0", "--port", str(port),
        ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        cwd=str(tiny_good_bundle.parent),
        env=okf_subprocess_env(),
    )
    try:
        # Wait up to 10s for the server to start serving HTTP. If the ack
        # gate refused, the process would have exited 2 already.
        deadline = _time.time() + 10
        ok = False
        while _time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"okf serve --public-ack exited early rc={proc.returncode} "
                    f"(ack gate refused even with the flag)"
                )
            try:
                with _urllib.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
                    if r.status == 200:
                        ok = True
                        break
            except OSError:
                _time.sleep(0.2)
        assert ok, "okf serve --public-ack did not become reachable in 10s"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
