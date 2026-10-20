"""Browser-free proofs for scripts/cdn_mirror.py (the media scripts' --cdn-mirror).

The mirror must answer every CDN request the live studio makes, with bytes
that satisfy the code's own SRI pins, and must refuse anything else.
"""
from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
mirror_mod = importlib.import_module("cdn_mirror")
vendor = importlib.import_module("okf_loom.viewer.vendor")

RENDERERS = SCRIPTS / "okf_loom" / "viewer" / "static" / "renderers.js"


@pytest.fixture(scope="module")
def mirror():
    return mirror_mod.CdnMirror()


def _studio_cdn_urls() -> set[str]:
    """Every jsDelivr URL the studio's own code can request."""
    urls = set(vendor.source_sri_pins()) | set(vendor.cdn_graph_chain())
    urls |= set(re.findall(r'"(https://cdn\.jsdelivr\.net/npm/[^"]+)"',
                           RENDERERS.read_text(encoding="utf-8")))
    return urls


def test_every_studio_cdn_url_is_answered(mirror):
    missing = sorted(url for url in _studio_cdn_urls() if mirror.lookup(url) is None)
    assert missing == []


def test_pinned_files_satisfy_the_codes_sri(mirror):
    pins = vendor.source_sri_pins()
    assert pins, "the studio pins no SRI hashes; this test would prove nothing"
    for url, integrity in pins.items():
        body, _ = mirror.lookup(url)
        assert vendor.sri_sha384(body) == integrity, url


def test_katex_stylesheet_fonts_are_all_served(mirror):
    css_url = mirror_mod.CDN_PREFIX + "katex@0.16.47/dist/katex.min.css"
    css, content_type = mirror.lookup(css_url)
    assert content_type.startswith("text/css")
    fonts = set(re.findall(r"url\((fonts/[^)]+\.woff2)\)", css.decode("utf-8")))
    assert len(fonts) >= 10
    base = css_url.rsplit("/", 1)[0] + "/"
    for rel in fonts:
        body, content_type = mirror.lookup(base + rel)
        assert content_type == "font/woff2" and body[:4] == b"wOF2", rel


def test_dynamic_modules_export_the_library_by_default(mirror):
    mermaid, kind = mirror.lookup(mirror_mod._MERMAID_ESM)
    assert kind == "application/javascript"
    assert mermaid.rstrip().endswith(b'export default __esbuild_esm_mermaid_nm["mermaid"].default;')
    assert b'globalThis["mermaid"] =' not in mermaid[-400:]
    hljs, _ = mirror.lookup(mirror_mod._HLJS_ESM)
    assert hljs.rstrip().endswith(b"export default hljs;")
    # Core plus every vendored language, the same bundle offline builds ship.
    assert hljs.count(b"hljs.registerLanguage(") >= 150


def test_query_strings_and_fragments_do_not_defeat_lookup(mirror):
    url = mirror_mod.CDN_PREFIX + "dagre@0.8.5/dist/dagre.min.js"
    assert mirror.lookup(url + "?v=1") == mirror.lookup(url + "#x") == mirror.lookup(url)


def test_unknown_cdn_paths_are_refused(mirror):
    class Route:
        def __init__(self, url):
            self.request = type("R", (), {"url": url})()
            self.aborted = None

        def abort(self, reason):
            self.aborted = reason

        def fulfill(self, **_):
            raise AssertionError("an unknown URL must not be fulfilled")

    route = Route(mirror_mod.CDN_PREFIX + "left-pad@1.3.0/index.js")
    mirror._handle(route)
    assert route.aborted == "blockedbyclient"


def test_mode_off_builds_nothing():
    assert mirror_mod.from_mode("off") is None
    with pytest.raises(ValueError):
        mirror_mod.from_mode("cdn")


@pytest.mark.parametrize("script", ["capture_readme_media.py", "record_showcase_media.py"])
def test_media_scripts_offer_the_option(script):
    result = subprocess.run([sys.executable, str(SCRIPTS / script), "--help"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "--cdn-mirror" in result.stdout and "vendor" in result.stdout
