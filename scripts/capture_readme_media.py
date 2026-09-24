#!/usr/bin/env python3
"""Capture the curated README media set (docs/media/) from the live studio.

Unlike ``capture_viewer_proof.py`` (dated proof artefacts for spec §17),
this script regenerates the STABLE, hand-picked set of images the README
links to. Filenames are fixed so the README never needs updating when the
media is refreshed:

* ``studio-home.png``        — dashboard index, light theme
* ``showcase-rendering.png`` — Mermaid/KaTeX section of demo/showcase
* ``comment-composer.png``   — select-to-comment affordance + composer
* ``graph-map.png``          — full-page graph, Map lens, light theme
* ``graph-bridges-dark.png`` — Bridges lens, dark theme
* ``search.png``             — live search results
* ``themes.gif``             — demo/showcase cycling all four resolved themes
* ``graph-lenses.gif``       — the graph cycling Map→Themes→Flow→Bridges→Recent

Requires Playwright plus a Chromium-family browser::

    python -m pip install playwright pillow   # pillow only for the GIFs
    playwright install chromium               # OR have Google Chrome installed
    PYTHONPATH=scripts python scripts/capture_readme_media.py

Browser resolution order: ``--chrome PATH``, ``OKF_CHROME``,
``AIC_PLAYWRIGHT_CHROME_PATH``, known system Chrome paths, the ``chrome``
channel, then Playwright-managed Chromium.
Without Pillow the two GIFs are skipped with a warning; the PNGs still
capture. Stills are DPR-2 for crispness; GIF frames are DPR-1 to keep the
files README-friendly.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from capture_viewer_proof import (
    _claim_destination, _publish_if_current, capture_target, launch_chromium,
)

__all__ = ["main"]

_SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _SCRIPT_DIR.parent
DEFAULT_BUNDLE = REPO_ROOT / "docs-bundle"
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "media"

_SERVER_STARTUP_TIMEOUT = 20.0
_SERVER_POLL_INTERVAL = 0.15
_POLL_HTTP_TIMEOUT = 1.0

STILL_VIEWPORT = {"width": 1440, "height": 900}
GIF_VIEWPORT = {"width": 1280, "height": 800}
GIF_FRAME_MS = 1_600
# Canonical RESOLVED theme vocabulary (the four concrete family-mode themes
# theme.js is the sole owner of). The retired legacy names (light/dark/pastel/
# sepia/midnight) are migrated once by theme.js and must not be written by any
# other consumer — so the capture seeds the canonical okf-theme-family /
# okf-theme-mode keys theme.js reads at boot, never the legacy okf-theme key.
THEMES = ("technical-light", "swiss-light", "technical-dark", "swiss-dark")
LENSES = ("Map", "Themes", "Flow", "Bridges", "Recent")


def _frame_staging(out_dir: Path):
    return tempfile.TemporaryDirectory(prefix=".okf-media-frames-", dir=out_dir)


def _log(msg: str) -> None:
    print(f"[capture_readme_media] {msg}", file=sys.stderr)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _script_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{_SCRIPT_DIR}{os.pathsep}{existing}" if existing else str(_SCRIPT_DIR)
    )
    return env


def _wait_for_http_ok(base: str, timeout: float = _SERVER_STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/", timeout=_POLL_HTTP_TIMEOUT) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(_SERVER_POLL_INTERVAL)
    return False


class _LiveServer:
    """Run ``scripts/okf-loom serve <bundle>`` on an ephemeral port."""

    def __init__(self, bundle: Path) -> None:
        self.bundle = bundle
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> str:
        self._proc = subprocess.Popen(
            [
                str(REPO_ROOT / "scripts" / "okf-loom"), "serve", str(self.bundle),
                "--host", "127.0.0.1", "--port", str(self.port),
                "--no-watch", "--no-open",
            ],
            cwd=str(REPO_ROOT),
            env=_script_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_for_http_ok(self.base):
            self.__exit__()
            raise RuntimeError(
                f"scripts/okf-loom serve did not become ready at {self.base} "
                f"within {_SERVER_STARTUP_TIMEOUT:g}s"
            )
        _log(f"live server ready at {self.base}")
        return self.base

    def __exit__(self, *exc) -> None:
        proc = self._proc
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        self._proc = None


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


def _theme_seed_script(theme: str) -> str:
    """Build the localStorage seed for a concrete resolved theme.

    ``theme`` is a canonical family-mode name (e.g. ``"swiss-dark"``). The seed
    writes the canonical ``okf-theme-family`` / ``okf-theme-mode`` keys that
    theme.js reads at boot — it NEVER writes the legacy ``okf-theme`` key,
    which theme.js owns as a migration/mirror only (one allowed state owner).
    """
    family, _, mode = theme.rpartition("-")
    return (
        f"localStorage.setItem('okf-theme-family','{family}');"
        f"localStorage.setItem('okf-theme-mode','{mode}');"
    )


def _new_page(browser, *, viewport: dict, dpr: int, theme: str | None = None):
    """Context+page with the graph tour pre-dismissed and an optional theme."""
    ctx = browser.new_context(viewport=viewport, device_scale_factor=dpr)
    boot = "try{localStorage.setItem('okfGraphTourDone','1');"
    if theme:
        boot += _theme_seed_script(theme)
    boot += "}catch(e){}"
    ctx.add_init_script(boot)
    return ctx, ctx.new_page()


def _click_lens(page, lens: str) -> None:
    """Select a graph lens chip by its accessible name."""
    page.get_by_role("radio", name=lens).click(timeout=5_000)


# ---------------------------------------------------------------------------
# Captures
# ---------------------------------------------------------------------------


def _capture_stills(browser, base: str, out_dir: Path) -> list[Path]:
    shots: list[Path] = []

    def still(name: str) -> Path:
        p = out_dir / name
        shots.append(p)
        return p

    # Dashboard index.
    ctx, page = _new_page(browser, viewport=STILL_VIEWPORT, dpr=2, theme="technical-light")
    capture_target(page, f"{base}/", "document", still("studio-home.png"), full_page=False)

    # Showcase concept, scrolled to the Mermaid/KaTeX stretch.
    showcase_url = f"{base}/demo/showcase"
    def scroll_showcase(p):
        p.evaluate("""() => {
            const h = [...document.querySelectorAll('h2,h3')]
                .find(x => /Diagrams/i.test(x.textContent));
            if (h) h.scrollIntoView({block: 'start'});
            window.scrollBy(0, -16);
        }""")
    capture_target(page, showcase_url, "document", still("showcase-rendering.png"), full_page=False, prepare=scroll_showcase)

    # Select-to-comment: stage a real selection so the affordance appears,
    # open the composer, and type a representative ask for the agent.
    def prepare_comment(p):
        p.evaluate("window.scrollTo(0, 0)")
        p.evaluate("""() => {
            const body = document.querySelector('.okf-page__body');
            const para = body &&
                [...body.querySelectorAll('p')].find(p => p.textContent.length > 80);
            if (!para) return;
            para.scrollIntoView({block: 'center'});
            const r = document.createRange();
            r.selectNodeContents(para);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(r);
        }""")
        p.click(".okf-comment-afford button", timeout=5_000)
        p.fill('textarea[aria-label="Comment for agent"]',
               "Tighten this intro and cross-link it to the graph lenses explanation.")
    capture_target(page, showcase_url, "document", still("comment-composer.png"), full_page=False, prepare=prepare_comment)
    ctx.close()

    # Graph: Map lens (light) and Bridges lens (dark).
    ctx, page = _new_page(browser, viewport=STILL_VIEWPORT, dpr=2, theme="technical-light")
    capture_target(page, f"{base}/__graph", "graph", still("graph-map.png"), full_page=False)
    ctx.close()

    ctx, page = _new_page(browser, viewport=STILL_VIEWPORT, dpr=2, theme="technical-dark")
    graph_url = f"{base}/__graph"
    capture_target(page, graph_url, "graph", still("graph-bridges-dark.png"), full_page=False,
                   prepare=lambda p: _click_lens(p, "Bridges"))
    ctx.close()

    # Live search.
    ctx, page = _new_page(browser, viewport=STILL_VIEWPORT, dpr=2, theme="technical-light")
    capture_target(page, f"{base}/__search?q=studio", "search", still("search.png"), full_page=False)
    ctx.close()

    return shots


def _assemble_gif(frames: list, out_path: Path) -> None:
    """Encode, validate, and atomically publish a looping GIF."""
    import io

    from PIL import Image

    out_path.parent.mkdir(parents=True, exist_ok=True)
    claim = _claim_destination(out_path)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{out_path.stem}.", suffix=".capture-tmp.gif", dir=out_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    opened = []
    images = []
    try:
        for frame in frames:
            source = Image.open(io.BytesIO(frame))
            opened.append(source)
            converted = source.convert("RGB")
            images.append(converted.quantize(colors=255, dither=Image.Dither.NONE))
            converted.close()
        if not images:
            raise ValueError("cannot assemble GIF without frames")
        images[0].save(tmp, save_all=True, append_images=images[1:],
                       duration=GIF_FRAME_MS, loop=0, optimize=True)
        with Image.open(tmp) as check:
            check.verify()
        if not _publish_if_current(tmp, out_path, claim):
            raise RuntimeError("A newer GIF assembly owns publication.")
    finally:
        for image in images:
            image.close()
        for source in opened:
            source.close()
        tmp.unlink(missing_ok=True)


def _capture_gifs(browser, base: str, out_dir: Path) -> list[Path]:
    try:
        import PIL  # noqa: F401
    except ImportError:
        _log("WARNING: pillow not installed - skipping themes.gif and "
             "graph-lenses.gif (python -m pip install pillow)")
        return []

    shots: list[Path] = []

    # Theme cycle on the showcase page. One context per theme: renderers
    # (Mermaid/hljs) pick their skin at render time, so a reload per theme
    # is the honest capture.
    with _frame_staging(out_dir) as staging:
        stage = Path(staging)
        frames = []
        for theme in THEMES:
            ctx, page = _new_page(browser, viewport=GIF_VIEWPORT, dpr=1, theme=theme)
            frame_path = stage / f"themes-{theme}.png"
            capture_target(page, f"{base}/demo/showcase", "document", frame_path, full_page=False)
            frames.append(frame_path.read_bytes())
            ctx.close()
        path = out_dir / "themes.gif"
        _assemble_gif(frames, path)
        shots.append(path)
        _log("themes.gif assembled")

        # Lens cycle on the graph page (Focus is omitted: it needs a chosen node).
        frames = []
        ctx, page = _new_page(browser, viewport=GIF_VIEWPORT, dpr=1, theme="technical-light")
        graph_url = f"{base}/__graph"
        frame_path = stage / "graph-map.png"
        capture_target(page, graph_url, "graph", frame_path, full_page=False)
        frames.append(frame_path.read_bytes())
        for lens in LENSES[1:]:
            frame_path = stage / f"graph-{lens.lower()}.png"
            capture_target(page, graph_url, "graph", frame_path, full_page=False,
                           navigate=False, prepare=lambda p, value=lens: _click_lens(p, value))
            frames.append(frame_path.read_bytes())
        ctx.close()
        path = out_dir / "graph-lenses.gif"
        _assemble_gif(frames, path)
        shots.append(path)
        _log("graph-lenses.gif assembled")

    return shots


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capture_readme_media.py",
        description=(
            "Regenerate the curated README media set under docs/media/ "
            "from a live studio serving the docs bundle."
        ),
    )
    p.add_argument(
        "--bundle",
        type=Path,
        default=DEFAULT_BUNDLE,
        help=f"OKF bundle to render (default: {DEFAULT_BUNDLE})",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"output directory for the media files (default: {DEFAULT_OUT_DIR})",
    )
    p.add_argument(
        "--chrome",
        default=None,
        help="path to Chrome/Chromium (then OKF_CHROME, "
             "AIC_PLAYWRIGHT_CHROME_PATH, system Chrome, managed Chromium)",
    )
    p.add_argument(
        "--skip-gifs",
        action="store_true",
        help="capture only the PNG stills",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "ERROR: playwright is not installed.\n"
            "  python -m pip install playwright pillow\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        return 1

    bundle = args.bundle.resolve()
    out_dir = args.out_dir.resolve()
    if not bundle.is_dir():
        print(f"ERROR: bundle directory not found: {bundle}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    _log(f"bundle={bundle}")
    _log(f"out_dir={out_dir}")

    captured: list[Path] = []
    try:
        with sync_playwright() as p:
            browser = launch_chromium(p, args.chrome)
            try:
                with _LiveServer(bundle) as base:
                    captured.extend(_capture_stills(browser, base, out_dir))
                    if not args.skip_gifs:
                        captured.extend(_capture_gifs(browser, base, out_dir))
            finally:
                browser.close()
    except (RuntimeError, SystemExit) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"# wrote {len(captured)} media files to {out_dir}")
    for path in captured:
        size = path.stat().st_size if path.exists() else 0
        print(f"  {path.relative_to(REPO_ROOT)}  ({size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
