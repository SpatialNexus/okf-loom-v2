"""Regenerate the CDN render golden used by the offline parity browser proofs.

This is the ONLY step that uses the network: it builds the offline fixture
bundle in the DEFAULT CDN mode, renders ``rich.html`` in Chromium with only
the local server and ``https://cdn.jsdelivr.net/`` reachable, and writes the
normalised render (``RENDER_SNAPSHOT`` in ``test_offline_assets_browser.py``)
with its provenance to ``tests/fixtures/offline_parity/cdn_render_golden.json``.
The committed tests compare the offline renders with it and never touch the
network.

Run it in the same reviewed change as a version bump (after
``scripts/fetch_vendor_assets.py --init-lock``), then review the diff:

    python tests/regenerate_offline_cdn_golden.py [--check]

``--check`` renders again and compares with the committed golden instead of
writing it (it ignores the provenance's date and browser version).
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(TESTS), str(TESTS.parent / "scripts")]

from browser_support import free_port, launch_chromium, managed_server, sync_playwright  # noqa: E402
from okf_loom.model import Bundle  # noqa: E402
from okf_loom.render import build_site  # noqa: E402
from test_offline_assets import write_offline_fixture  # noqa: E402
from test_offline_assets_browser import CDN_GOLDEN, RENDER_SNAPSHOT, VIEWPORT  # noqa: E402

CDN_ORIGIN = "https://cdn.jsdelivr.net/"
SCOPE = ".okf-page__body"
_PINNED = re.compile(r"/npm/(mermaid|katex|highlight\.js)@(\d+\.\d+\.\d+)/")


def render_cdn_golden(work: Path) -> dict:
    bundle = Bundle.load(write_offline_fixture(work / "bundle"))
    out = work / "static-cdn"
    build_site(bundle, out, target="static", assets="cdn")
    with ExitStack() as stack:
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        stack.enter_context(managed_server(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(out)],
            base, work / "http.log"))
        pw = stack.enter_context(sync_playwright())
        browser = launch_chromium(pw)
        stack.callback(browser.close)
        context = browser.new_context(viewport=VIEWPORT, reduced_motion="reduce")
        requests: list[str] = []
        refused: list[str] = []
        context.on("request", lambda request: requests.append(request.url))

        def route(route):
            url = route.request.url
            if url.startswith((base + "/", CDN_ORIGIN, "data:")):
                route.continue_()
            else:
                refused.append(url)
                route.abort()
        context.route("**/*", route)
        page = context.new_page()
        csp: list[str] = []
        page.on("console", lambda m: csp.append(m.text) if "Content Security Policy" in m.text else None)
        page.goto(base + "/rich.html")
        page.wait_for_function("() => ['ready', 'failed'].includes("
                               "document.documentElement.dataset.okfRenderersStatus)", timeout=90000)
        status = page.evaluate("() => document.documentElement.dataset.okfRenderersStatus")
        if status != "ready" or refused or csp:
            raise SystemExit(f"CDN render is not clean: status={status} refused={refused} csp={csp}")
        snapshot = page.evaluate(RENDER_SNAPSHOT, SCOPE)
        cdn = sorted({url for url in requests if url.startswith(CDN_ORIGIN)})
        versions = {}
        for url in cdn:
            match = _PINNED.search(url)
            if match:
                if versions.setdefault(match.group(1), match.group(2)) != match.group(2):
                    raise SystemExit(f"two versions of {match.group(1)} requested")
        provenance = {
            "generator": "tests/regenerate_offline_cdn_golden.py",
            "generated": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
            "source": ("default CDN build (`build --target static --assets cdn`) of the "
                       "write_offline_fixture bundle in tests/test_offline_assets.py; page rich.html, "
                       f"scope {SCOPE}, theme technical-light, viewport {VIEWPORT['width']}x{VIEWPORT['height']}"),
            "browser": f"Chromium {browser.version} (Playwright)",
            "versions": dict(sorted(versions.items())),
            "cdn_requests": cdn,
            "normalisation": ("RENDER_SNAPSHOT in tests/test_offline_assets_browser.py: Mermaid SVG "
                              "element/id/class/role/text structure (render counter normalised, <style> "
                              "text and geometry excluded), highlight.js token class/text sequences per "
                              "block, KaTeX MathML"),
        }
        return {"provenance": provenance, **snapshot}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="compare with the committed golden")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="okf-cdn-golden-") as work:
        golden = render_cdn_golden(Path(work))
    if args.check:
        current = json.loads(CDN_GOLDEN.read_text(encoding="utf-8"))
        same = all(golden[part] == current[part] for part in ("mermaid", "hljs", "katex")) and \
            golden["provenance"]["versions"] == current["provenance"]["versions"]
        print("CDN golden reproduces" if same else "CDN golden differs from a fresh CDN render")
        return 0 if same else 1
    CDN_GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    CDN_GOLDEN.write_text(json.dumps(golden, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {CDN_GOLDEN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
