"""Bundle-local media: serve route, §5 visibility oracle, build parity,
and asset validation.

Feature background: the studio could not display bundle-local images —
``_route()`` sent every non-``__`` path to ``_handle_concept`` (404 for
non-concepts) and ``_content_type_for`` knew no image types, while the CSP
(by design) blocks remote hot-linking and the sanitizer blocks ``data:``
URIs. These tests pin the fix end to end:

* ``GET /<path>.<media ext>`` serves §5-visible, root-contained bundle
  files with correct content types, a sandboxed CSP (bundle media is user
  content — a bundle SVG must not script in the studio origin), and
  ETag/304 revalidation.
* Everything the §5 scan prunes (session state, ``.git``, gitignored
  paths) stays unreachable; traversal and symlink escapes are refused.
* ``okf_loom.ignore.is_path_visible`` agrees with the walker
  decision-for-decision (parity oracle).
* Static/spa builds copy the same §5-visible media set; static pages
  relativize absolute image srcs.
* ``validate`` flags missing image targets (asset.missing) — images are
  not graph edges, so nothing else would catch a typo'd screenshot path.
"""
from __future__ import annotations

import os
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.ignore import is_path_visible, iter_bundle_files, iter_markdown_files
from okf_loom.server import OKFWikiHandler, _content_type_for
from okf_loom.validate import Severity, validate_bundle, validate_bundle_with_profile

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(bundle: Bundle, *, port: int):
    server = ThreadingHTTPServer(("127.0.0.1", port), OKFWikiHandler)
    server.bundle = bundle  # type: ignore[attr-defined]
    server.config = {}  # type: ignore[attr-defined]
    server.name = bundle.name  # type: ignore[attr-defined]
    server.plugin = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(port: int, path: str, headers: dict | None = None, method: str = "GET"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture()
def media_bundle(tmp_path: Path) -> Path:
    """A bundle with visible media, pruned media, and a symlink escape."""
    root = tmp_path / "bundle"
    (root / "research/xero/assets").mkdir(parents=True)
    (root / "research/xero/overview.md").write_text(
        "---\ntype: note\ntitle: Overview\n---\n\n"
        "![shot](/research/xero/assets/shot.png)\n",
        encoding="utf-8",
    )
    (root / "research/xero/assets/shot.png").write_bytes(PNG)
    (root / "research/xero/assets/clip.webm").write_bytes(b"\x1aE\xdf\xa3webm")
    # Pruned locations: session state, gitignored dir.
    (root / ".okf-loom/session").mkdir(parents=True)
    (root / ".okf-loom/session/leak.png").write_bytes(b"secret")
    (root / "private").mkdir()
    (root / "private/hidden.png").write_bytes(b"hidden")
    (root / ".gitignore").write_text("private/\n", encoding="utf-8")
    # Symlinked file escaping the root.
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    os.symlink(outside, root / "research/xero/assets/escape.png")
    return root


@pytest.fixture()
def served(media_bundle: Path):
    bundle = Bundle.load(media_bundle)
    port = _free_port()
    server, thread = _start_server(bundle, port=port)
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Serve route
# ---------------------------------------------------------------------------

def test_serves_bundle_image_with_type_and_sandbox_csp(served: int) -> None:
    status, headers, body = _get(served, "/research/xero/assets/shot.png")
    assert status == 200
    assert body == PNG
    assert headers["Content-Type"] == "image/png"
    # Bundle media is user content: the response CSP must sandbox direct
    # navigation (an SVG with <script> must not run in the studio origin,
    # where the per-session token is embedded in every page).
    assert "sandbox" in headers["Content-Security-Policy"]
    assert "script-src" not in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_page_csp_is_not_sandboxed(served: int) -> None:
    status, headers, _ = _get(served, "/research/xero/overview")
    assert status == 200
    assert "sandbox" not in headers["Content-Security-Policy"]


def test_etag_revalidation_304(served: int) -> None:
    _, headers, _ = _get(served, "/research/xero/assets/shot.png")
    etag = headers["ETag"]
    status, headers2, body = _get(
        served, "/research/xero/assets/shot.png", {"If-None-Match": etag}
    )
    assert status == 304
    assert body == b""
    assert headers2["ETag"] == etag


def test_head_request_has_no_body(served: int) -> None:
    status, headers, body = _get(
        served, "/research/xero/assets/shot.png", method="HEAD"
    )
    assert status == 200
    assert body == b""
    assert int(headers["Content-Length"]) == len(PNG)


def test_pruned_paths_are_unreachable(served: int) -> None:
    # Session state (holds the CSRF .token next door) and gitignored dirs
    # are outside the §5-visible set — 404 even though the files exist.
    for path in ("/.okf-loom/session/leak.png", "/private/hidden.png"):
        status, _, _ = _get(served, path)
        assert status == 404, path


def test_symlink_escape_refused(served: int) -> None:
    status, _, _ = _get(served, "/research/xero/assets/escape.png")
    assert status == 404


def test_traversal_refused(served: int) -> None:
    for path in (
        "/../outside.png",
        "/%2e%2e/outside.png",
        "/research/../../outside.png",
        "/research/./xero/assets/shot.png",
    ):
        status, _, _ = _get(served, path)
        assert status == 400, path


def test_non_media_extensions_never_route_to_assets(served: int) -> None:
    # .gitignore exists at the bundle root but has no media extension:
    # it falls through to concept routing and 404s there.
    status, _, body = _get(served, "/.gitignore")
    assert status == 404
    # Markdown stays on the concept route (200 as a rendered page).
    status, headers, _ = _get(served, "/research/xero/overview.md")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")


def test_oversized_media_413(served: int, monkeypatch: pytest.MonkeyPatch) -> None:
    import okf_loom.server as srv

    monkeypatch.setattr(srv, "MAX_RAW_RESPONSE_BYTES", 4)
    status, _, _ = _get(served, "/research/xero/assets/shot.png")
    assert status == 413


def test_media_suffixed_concept_still_routes(tmp_path: Path) -> None:
    """A concept id may legally end in a media suffix (``shot.png.md``).

    When no FILE exists at the media path, the asset route must fall
    through to concept routing so ``/shot.png`` keeps rendering the
    concept page exactly as before the media route existed.
    """
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "shot.png.md").write_text(
        "---\ntype: note\ntitle: Odd name\n---\nbody text\n", encoding="utf-8"
    )
    bundle = Bundle.load(root)
    port = _free_port()
    server, thread = _start_server(bundle, port=port)
    try:
        status, headers, body = _get(port, "/shot.png")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert b"Odd name" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_include_revival_serves_pruned_media(tmp_path: Path) -> None:
    """bundle.include revives a gitignored dir for media exactly as for .md."""
    root = tmp_path / "bundle"
    (root / "vendor").mkdir(parents=True)
    (root / "vendor/logo.png").write_bytes(PNG)
    (root / ".gitignore").write_text("vendor/\n", encoding="utf-8")
    (root / "note.md").write_text("---\ntype: note\n---\nbody\n", encoding="utf-8")
    (root / "okf-loom.config.yaml").write_text(
        "bundle:\n  include:\n    - vendor/\n", encoding="utf-8"
    )
    bundle = Bundle.load(root)
    port = _free_port()
    server, thread = _start_server(bundle, port=port)
    try:
        status, _, body = _get(port, "/vendor/logo.png")
        assert status == 200
        assert body == PNG
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_content_types_for_media() -> None:
    cases = {
        "a.png": "image/png",
        "b.JPG": "image/jpeg",
        "c.jpeg": "image/jpeg",
        "d.gif": "image/gif",
        "e.webp": "image/webp",
        "f.svg": "image/svg+xml",
        "g.mp4": "video/mp4",
        "h.webm": "video/webm",
        "i.pdf": "application/pdf",
        "wiki.css": "text/css; charset=utf-8",
        "live.js": "application/javascript; charset=utf-8",
        "unknown.token": "application/octet-stream",
    }
    for name, want in cases.items():
        assert _content_type_for(name) == want, name


# ---------------------------------------------------------------------------
# Visibility oracle parity
# ---------------------------------------------------------------------------

def _all_files(root: Path) -> list[Path]:
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for f in filenames:
            out.append(Path(dirpath) / f)
    return out


def test_is_path_visible_matches_walker(tmp_path: Path) -> None:
    """The per-path oracle agrees with iter_markdown_files on every file."""
    root = tmp_path / "tree"
    (root / "docs").mkdir(parents=True)
    (root / "docs/a.md").write_text("x")
    (root / "docs/.hidden.md").write_text("x")  # hidden FILE: loads
    (root / ".hiddendir").mkdir()
    (root / ".hiddendir/b.md").write_text("x")
    (root / "node_modules/pkg").mkdir(parents=True)
    (root / "node_modules/pkg/c.md").write_text("x")
    (root / "nested-repo").mkdir()
    (root / "nested-repo/.git").mkdir()
    (root / "nested-repo/d.md").write_text("x")
    (root / "ignored").mkdir()
    (root / "ignored/e.md").write_text("x")
    (root / "keep").mkdir()
    (root / "keep/f.md").write_text("x")
    (root / ".gitignore").write_text("ignored/\nkeep/\n!keep/f.md\n")
    (root / "sub").mkdir()
    (root / "sub/.gitignore").write_text("local.md\n")
    (root / "sub/local.md").write_text("x")
    (root / "sub/g.md").write_text("x")

    for include in ([], ["node_modules/pkg/"], ["nested-repo/"]):
        found = {
            p.relative_to(root).as_posix()
            for p in iter_markdown_files(root, include=include)
        }
        for f in _all_files(root):
            rel = f.relative_to(root).as_posix()
            if not rel.endswith(".md"):
                continue
            assert is_path_visible(root, rel, include=include) == (rel in found), (
                f"oracle disagrees with walker for {rel!r} (include={include})"
            )


def test_is_path_visible_rejects_malformed() -> None:
    root = Path(".")
    for rel in ("", "..", "../x.png", "a/../b.png", "a//b.png", "a/./b.png",
                "a\\b.png", "a/\x00.png"):
        assert not is_path_visible(root, rel), rel


def test_iter_bundle_files_predicate(tmp_path: Path) -> None:
    root = tmp_path / "b"
    (root / "assets").mkdir(parents=True)
    (root / "assets/x.png").write_bytes(b"x")
    (root / "assets/y.md").write_text("y")
    found = iter_bundle_files(root, name_predicate=lambda n: n.endswith(".png"))
    assert [p.name for p in found] == ["x.png"]


# ---------------------------------------------------------------------------
# Build parity (static / spa)
# ---------------------------------------------------------------------------

def test_build_copies_visible_media_only(media_bundle: Path, tmp_path: Path) -> None:
    from okf_loom.render import build_site

    bundle = Bundle.load(media_bundle)
    for mode in ("static", "spa"):
        out = tmp_path / f"site-{mode}"
        build_site(bundle, out, target=mode)
        assert (out / "research/xero/assets/shot.png").read_bytes() == PNG
        assert (out / "research/xero/assets/clip.webm").is_file()
        # Pruned + escaping files must NOT be exported.
        assert not (out / "private/hidden.png").exists()
        assert not (out / ".okf-loom").exists()
        assert not (out / "research/xero/assets/escape.png").exists()


def test_static_relativizes_absolute_img_src(media_bundle: Path, tmp_path: Path) -> None:
    from okf_loom.render import build_site

    bundle = Bundle.load(media_bundle)
    out = tmp_path / "site"
    build_site(bundle, out, target="static")
    page = (out / "research/xero/overview.html").read_text(encoding="utf-8")
    assert 'src="../../research/xero/assets/shot.png"' in page
    assert 'src="/research/xero/assets/shot.png"' not in page


def test_spa_keeps_absolute_img_src(media_bundle: Path, tmp_path: Path) -> None:
    from okf_loom.render import build_site

    bundle = Bundle.load(media_bundle)
    out = tmp_path / "site"
    build_site(bundle, out, target="spa")
    page = (out / "research/xero/overview.html").read_text(encoding="utf-8")
    assert 'src="/research/xero/assets/shot.png"' in page


# ---------------------------------------------------------------------------
# Validation: asset.missing / asset.out_of_bundle
# ---------------------------------------------------------------------------

def _asset_findings(root: Path):
    report = validate_bundle(Bundle.load(root))
    return [f for f in report.findings if f.code.startswith("asset.")]


def test_validate_flags_missing_image_targets(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    (root / "assets").mkdir(parents=True)
    (root / "assets/ok.png").write_bytes(PNG)
    (root / "note.md").write_text(
        "---\ntype: note\n---\n\n"
        "![ok abs](/assets/ok.png)\n"
        "![ok rel](./assets/ok.png)\n"
        "![missing](/assets/nope.png)\n"
        "![external](https://example.com/x.png)\n"
        "```\n![fenced](/assets/fenced.png)\n```\n",
        encoding="utf-8",
    )
    findings = _asset_findings(root)
    assert [f.detail["target_raw"] for f in findings] == ["/assets/nope.png"]
    assert findings[0].code == "asset.missing"
    assert findings[0].severity is Severity.WARNING


def test_validate_flags_out_of_bundle_image(tmp_path: Path) -> None:
    (tmp_path / "shared.png").write_bytes(PNG)
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "note.md").write_text(
        "---\ntype: note\n---\n\n"
        "![exists outside](../shared.png)\n"
        "![missing outside](../gone.png)\n",
        encoding="utf-8",
    )
    findings = {f.detail["target_raw"]: f for f in _asset_findings(root)}
    assert findings["../shared.png"].code == "asset.out_of_bundle"
    assert findings["../shared.png"].severity is Severity.INFO
    assert findings["../gone.png"].code == "asset.missing"


def test_fail_on_broken_links_promotes_asset_missing(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "note.md").write_text(
        "---\ntype: note\n---\n\n![gone](/assets/gone.png)\n", encoding="utf-8"
    )
    report = validate_bundle_with_profile(
        Bundle.load(root), fail_on_broken_links=True
    )
    findings = [f for f in report.findings if f.code == "asset.missing"]
    assert findings and findings[0].severity is Severity.ERROR
