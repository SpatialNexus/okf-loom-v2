"""Answer the live studio's pinned CDN requests from the vendored copies.

``scripts/okf-loom serve`` loads Mermaid, KaTeX, highlight.js and the
Cytoscape graph chain from jsDelivr. A capture host without access to that
CDN (a sandbox, an offline CI runner) can pass ``--cdn-mirror vendor`` to the
README media scripts. Each browser context then answers those requests from
``scripts/okf_loom/viewer/vendor/``:

* Every SRI-pinned file is served as the byte-verified vendored copy. The
  browser still enforces the page's ``integrity`` attributes, so a mismatch
  fails the capture rather than rendering something different.
* KaTeX's fonts are the vendored, lock-verified WOFF2 files.
* The two ES-module entry points the studio imports dynamically (Mermaid's
  ``mermaid.esm.min.mjs`` and jsDelivr's generated ``highlight.js/+esm``) are
  not vendored. They are rebuilt from the same pinned versions: Mermaid's
  vendored bundle with its global assignment replaced by a default export,
  and the offline highlight.js bundle (core plus every language, exactly as
  ``build --assets offline`` ships it) with a default export appended.

Any other jsDelivr request is aborted and recorded, so the capture's
request-failure check names it. Nothing here changes what the studio serves;
this is a recording-host shim only.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from okf_loom.viewer import vendor as _vendor

__all__ = ["CDN_PREFIX", "MODES", "CdnMirror", "add_argument", "from_mode"]

CDN_PREFIX = "https://cdn.jsdelivr.net/npm/"
MODES = ("off", "vendor")

_MERMAID_UMD = "mermaid-11.17.2/mermaid.min.js"
_MERMAID_ESM = CDN_PREFIX + "mermaid@11.17.2/dist/mermaid.esm.min.mjs"
_MERMAID_TAIL = 'globalThis["mermaid"] = globalThis.__esbuild_esm_mermaid_nm["mermaid"].default;'
_HLJS_ESM = CDN_PREFIX + "highlight.js@11.12.0/+esm"
_KATEX_FONTS_URL = CDN_PREFIX + "katex@0.16.47/dist/fonts/"
_KATEX_FONTS_DIR = "katex-0.16.47/fonts/"

_TYPES = {
    ".js": "application/javascript", ".mjs": "application/javascript",
    ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
}


def add_argument(parser) -> None:
    """The shared ``--cdn-mirror`` option of the README media scripts."""
    parser.add_argument(
        "--cdn-mirror", choices=MODES, default="off",
        help="off (default): the browser fetches the studio's pinned CDN libraries. "
             "vendor: answer those requests from the byte-verified copies in "
             "scripts/okf_loom/viewer/vendor/ (SRI still enforced), for capture "
             "hosts that cannot reach cdn.jsdelivr.net")


def _mermaid_module(umd: bytes) -> bytes:
    text = umd.decode("utf-8").rstrip()
    if not text.endswith(_MERMAID_TAIL):
        raise RuntimeError("the vendored Mermaid bundle no longer ends with its global "
                           "assignment; update scripts/cdn_mirror.py")
    body = text[: -len(_MERMAID_TAIL)]
    return (body + 'export default __esbuild_esm_mermaid_nm["mermaid"].default;\n').encode("utf-8")


def _hljs_module(bundle: bytes) -> bytes:
    if not bundle.lstrip().startswith(b"/*!") or b"var hljs" not in bundle[:4096]:
        raise RuntimeError("the offline highlight.js bundle does not declare hljs; "
                           "update scripts/cdn_mirror.py")
    return bundle.rstrip(b"\n") + b"\nexport default hljs;\n"


class CdnMirror:
    """Verified ``url -> (bytes, content type)`` table plus a Playwright route."""

    def __init__(self) -> None:
        # Verifies every vendored file against the lock and the code's SRI pins.
        offline = _vendor.load_offline_assets()
        lock = _vendor.load_lock()
        entries = _vendor.lock_files(lock)
        table: dict[str, tuple[bytes, str]] = {}
        with _vendor.VendorTree() as tree:
            for entry in entries:
                dest = entry["dest"]
                url = entry.get("cdn")
                if url is None and dest.startswith(_KATEX_FONTS_DIR) and dest.endswith(".woff2"):
                    url = _KATEX_FONTS_URL + dest[len(_KATEX_FONTS_DIR):]
                if url is None:
                    continue
                data = tree.read(dest)
                _vendor._check_entry(entry, data)
                table[url] = (data, _TYPES.get(PurePosixPath(dest).suffix,
                                               "application/octet-stream"))
            mermaid_entry = next(e for e in entries if e["dest"] == _MERMAID_UMD)
            mermaid = tree.read(_MERMAID_UMD)
            _vendor._check_entry(mermaid_entry, mermaid)
        table[_MERMAID_ESM] = (_mermaid_module(mermaid), _TYPES[".mjs"])
        table[_HLJS_ESM] = (_hljs_module(offline.files[_vendor.HLJS_DERIVED]), _TYPES[".js"])
        self.table = table
        self.misses: list[str] = []

    def lookup(self, url: str) -> tuple[bytes, str] | None:
        return self.table.get(url.split("#", 1)[0].split("?", 1)[0])

    def _handle(self, route) -> None:
        url = route.request.url
        hit = self.lookup(url)
        if hit is None:
            self.misses.append(url)
            route.abort("blockedbyclient")
            return
        body, content_type = hit
        route.fulfill(status=200, body=body, headers={
            "content-type": content_type,
            "access-control-allow-origin": "*",
            "cache-control": "public, max-age=31536000, immutable",
        })

    def attach(self, context) -> None:
        """Route this browser context's jsDelivr requests through the table."""
        context.route(CDN_PREFIX + "**", self._handle)


def from_mode(mode: str) -> CdnMirror | None:
    if mode not in MODES:
        raise ValueError(f"unknown --cdn-mirror mode {mode!r}")
    return CdnMirror() if mode == "vendor" else None
