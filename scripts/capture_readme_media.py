#!/usr/bin/env python3
"""Capture the curated README media set (docs/media/) from the live studio.

Unlike ``capture_viewer_proof.py`` (dated proof artefacts for spec §17),
this script regenerates the STABLE, hand-picked set of images the README
links to. Filenames are fixed so the README never needs updating when the
media is refreshed:

* ``studio-home.png``        — dashboard index with the studio connected
* ``showcase-rendering.png`` — Mermaid/KaTeX section of demo/showcase
* ``comment-composer.png``   — an exact-text selection with the composer open
* ``graph-map.png``          — full-page graph, Map lens, light mode
* ``graph-bridges-dark.png`` — Bridges lens, dark mode
* ``search.png``             — live search results
* ``phone-top.png``          — a tutorial on a 390x844 touch phone, at the
                               top of the page with the full chrome
* ``phone-scrolled.png``     — the same page after two swipes, with the
                               chrome scrolled away to the Sections row
* ``social-preview.png``     — 1280x640 repository card built from the
                               dark Bridges still and the live design tokens

The animated showcase clips (``comment-loop``, ``appearance``, ``graph`` and
``search``, each as MP4 + GIF) are recorded by ``record_showcase_media.py``.

Requires Playwright plus a Chromium-family browser::

    python -m pip install playwright
    playwright install chromium               # OR have Google Chrome installed
    PYTHONPATH=scripts python scripts/capture_readme_media.py

Browser resolution order: ``--chrome PATH``, ``OKF_CHROME``,
``AIC_PLAYWRIGHT_CHROME_PATH``, known system Chrome paths, the ``chrome``
channel, then Playwright-managed Chromium. Stills are DPR-2 for crispness.

The stills show the product defaults a new reader gets: no appearance
storage is seeded, and each still selects its Ledger palette (Bone, or
Slate for the dark graph) through theme.js's public API only. The
studio is served from a snapshot of the bundle's git-tracked files, so
ignored session state in a working checkout (comment history, activity
logs, lock files) never leaks into the media; ``--in-place`` serves the
bundle directory as-is instead. Any console error, uncaught page error,
failed request, or HTTP error response fails the run.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import socket
import string
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
from okf_loom import LOOM_VERSION, SPEC_VERSION

__all__ = ["main"]

_SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _SCRIPT_DIR.parent
DEFAULT_BUNDLE = REPO_ROOT / "docs-bundle"
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "media"

_SERVER_STARTUP_TIMEOUT = 20.0
_SERVER_POLL_INTERVAL = 0.15
_POLL_HTTP_TIMEOUT = 1.0

STILL_VIEWPORT = {"width": 1440, "height": 900}
# Both Mermaid diagrams and the KaTeX block need ~100px more to share a
# frame; the capture then trims the height to end above the next section.
SHOWCASE_VIEWPORT = {"width": 1440, "height": 1000}
# The comments drawer pushes the page; below 1600px the header wraps.
COMPOSER_VIEWPORT = {"width": 1600, "height": 1000}
SOCIAL_VIEWPORT = {"width": 1280, "height": 640}
# A phone held upright, with touch input. At this width the top bar and the
# view tabs scroll away while reading, leaving the Sections row pinned.
PHONE_VIEWPORT = {"width": 390, "height": 844}
PHONE_PAGE = "tutorials/live_studio_basics"
# Two upward swipes of this finger travel land in the tutorial's opening prose.
PHONE_SWIPES = 2
PHONE_SWIPE_PX = 560

COMPOSER_PAGE = "explanation/live_studio_design"
COMPOSER_SENTENCE = "This essay explains the reasoning behind the live collaborative studio."
COMPOSER_NOTE = ("Lead with what a reader can do in the studio, then link to the "
                 "graph-lens rationale.")
SEARCH_QUERY = "studio"


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


class _BrowserDiagnostics:
    """Collect browser-side faults; any entry fails the capture run."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def watch(self, page, label: str) -> None:
        def on_console(msg) -> None:
            if msg.type == "error":
                self.failures.append(f"{label}: console error: {msg.text}")

        def on_request_failed(request) -> None:
            failure = request.failure or ""
            # Navigating away or closing a context aborts in-flight requests
            # (notably the studio's event stream); that is not a page fault.
            if "ERR_ABORTED" in failure:
                return
            self.failures.append(
                f"{label}: request failed: {request.method} {request.url} ({failure})")

        def on_response(response) -> None:
            if response.status >= 400:
                self.failures.append(
                    f"{label}: HTTP {response.status}: {response.request.method} {response.url}")

        page.on("console", on_console)
        page.on("pageerror", lambda exc: self.failures.append(f"{label}: page error: {exc}"))
        page.on("requestfailed", on_request_failed)
        page.on("response", on_response)

    def raise_if_failed(self) -> None:
        if self.failures:
            raise RuntimeError("browser reported faults during capture:\n- "
                               + "\n- ".join(self.failures))


def _new_page(browser, *, viewport: dict, dpr: int, color_scheme: str = "light",
              diagnostics: _BrowserDiagnostics | None = None, label: str = "",
              touch: bool = False):
    """Context+page for one capture.

    No appearance storage is written, so theme.js (the only owner of that
    state) resolves the shipped defaults. Explicit capture palettes are selected
    through its public API by _settle, never by storage injection. The graph tour needs no pre-dismissal: the live graph only offers
    it behind an invitation button.
    """
    ctx = browser.new_context(viewport=viewport, device_scale_factor=dpr,
                              color_scheme=color_scheme, is_mobile=touch, has_touch=touch)
    page = ctx.new_page()
    if diagnostics is not None:
        diagnostics.watch(page, label)
    return ctx, page


_STUDIO_SETTLED_JS = """() => {
  const summary = document.querySelector('.okf-studio-launcher__summary');
  return !!summary && /\\w/.test(summary.textContent)
    && !/Connecting|Reconnecting|Offline/.test(summary.textContent);
}"""


def _settle(page, *, theme: str) -> None:
    """Wait for the live studio session and confirm the resolved theme.

    The studio launcher paints "Connecting…" until its event stream is up;
    a capture taken before that shows a transient state, not the product.
    """
    page.wait_for_function(_STUDIO_SETTLED_JS, timeout=15_000)
    page.evaluate("theme => window.OKFLoomTheme.setTheme(theme)", theme)
    resolved = page.evaluate("() => document.documentElement.dataset.theme")
    if resolved != theme:
        raise RuntimeError(f"expected resolved theme {theme!r}, got {resolved!r}")


def _click_lens(page, lens: str) -> None:
    """Select a graph lens the way a reader does, then verify it took.

    The lens chips live inside the collapsed "Explore with lenses"
    disclosure, so it is opened first; each radio's accessible name is
    "<Lens>: <question>".
    """
    disclosure = page.locator("details.okf-signal__lenses")
    if not disclosure.evaluate("d => d.open"):
        disclosure.locator("summary").click(timeout=5_000)
    radio = page.get_by_role("radio", name=re.compile(rf"^{re.escape(lens)}:"))
    page.locator("label.okf-signal__seg-item", has=radio).click(timeout=5_000)
    if not radio.is_checked():
        raise RuntimeError(f"graph lens {lens!r} did not become the checked lens")
    # The new lens re-flows the detail panel under the pointer; park it on
    # the inert route title so no lens chip is captured mid-hover.
    title = page.locator(".okf-graph-route-title").bounding_box()
    if title:
        page.mouse.move(title["x"] + title["width"] - 4, title["y"] + title["height"] / 2)


_SCROLL_UNDER_CHROME_JS = """([selector, pattern, gap]) => {
  const re = new RegExp(pattern, 'i');
  const target = [...document.querySelectorAll(selector)].find(x => re.test(x.textContent));
  if (!target) return false;
  // The sticky chrome is a stack (site bar, then the concept view bar):
  // follow wide bars downward while each one starts where the last ends.
  const chromeBottom = () => {
    const bars = [...document.querySelectorAll('body *')]
      .filter(e => ['sticky', 'fixed'].includes(getComputedStyle(e).position))
      .map(e => e.getBoundingClientRect())
      .filter(r => r.bottom > 0 && r.width >= innerWidth / 2 && r.height < innerHeight / 3)
      .sort((a, b) => a.top - b.top);
    let bottom = 0;
    for (const r of bars) if (r.top <= bottom + 1) bottom = Math.max(bottom, r.bottom);
    return bottom;
  };
  // Sticky bars only stick once the page has scrolled, so settle twice.
  for (let pass = 0; pass < 2; pass++)
    window.scrollBy({top: target.getBoundingClientRect().top - chromeBottom() - gap,
                     behavior: 'instant'});
  return true;
}"""


# Both Mermaid diagrams and the KaTeX block have rendered, so the showcase
# frame is measured against its final layout.
_SHOWCASE_RENDERED_JS = """() => {
  const diagrams = [...document.querySelectorAll('.okf-page__body .mermaid')];
  return diagrams.length >= 2 && diagrams.every(d => d.querySelector('svg'))
    && !!document.querySelector('.okf-page__body .katex-display');
}"""


_POPOVER_HIDDEN_JS = """() => {
  const pop = document.getElementById('okf-popover');
  return !pop || getComputedStyle(pop).display === 'none';
}"""


# Resolves once the page has not scrolled for ten consecutive frames.
_SCROLL_SETTLED_JS = """() => new Promise(resolve => {
  let last = -1, still = 0;
  const tick = () => {
    const y = scrollY;
    still = y === last ? still + 1 : 0;
    last = y;
    if (still >= 10) resolve(y); else requestAnimationFrame(tick);
  };
  tick();
})"""


# The phone chrome has finished sliding away: the top bar is above the
# viewport and the view bar's Sections row is pinned at the top.
_CHROME_COMPACT_JS = """() => {
  if (document.documentElement.dataset.okfChrome !== 'compact') return false;
  const sliding = document.getAnimations().some(a => {
    const el = a.effect && a.effect.target;
    return a.playState === 'running' && el && el.closest
      && el.closest('.okf-topbar, .okf-concept-viewbar');
  });
  const bar = document.querySelector('.okf-topbar.okf-shell');
  const row = document.querySelector('.okf-concept-viewbar .okf-outline-jump');
  if (sliding || !bar || !row) return false;
  const pinned = row.getBoundingClientRect();
  return bar.getBoundingClientRect().bottom <= 0 && pinned.top >= 0 && pinned.top < 40;
}"""


def _swipe_up(page, cdp, distance: float, *, x: float = 195, start_y: float = 700) -> None:
    """One finger swipe upward, which scrolls the page down by ``distance``.

    The finger rests before lifting, so the swipe ends without a fling and
    the scroll position is repeatable.
    """
    steps = 40
    cdp.send("Input.dispatchTouchEvent",
             {"type": "touchStart", "touchPoints": [{"x": x, "y": start_y}]})
    for i in range(1, steps + 1):
        cdp.send("Input.dispatchTouchEvent", {
            "type": "touchMove",
            "touchPoints": [{"x": x, "y": start_y - distance * i / steps}]})
        page.wait_for_timeout(16)
    page.wait_for_timeout(150)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _scroll_chrome_away(ctx, page) -> None:
    """Swipe a ready phone page down until its chrome has scrolled away.

    The capture readiness owner marks the page ``data-okf-capture`` so stills
    show the full chrome. Here the scrolled-away chrome is the subject, and
    the page has already passed readiness, so that hold is released before
    the reader-style swipes.
    """
    page.evaluate("() => document.documentElement.removeAttribute('data-okf-capture')")
    cdp = ctx.new_cdp_session(page)
    try:
        for _ in range(PHONE_SWIPES):
            _swipe_up(page, cdp, PHONE_SWIPE_PX)
    finally:
        cdp.detach()
    page.evaluate(_SCROLL_SETTLED_JS)
    page.wait_for_function(_CHROME_COMPACT_JS, timeout=5_000)
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


_SELECT_SENTENCE_JS = """(sentence) => {
  const body = document.querySelector('.okf-page__body');
  if (!body) return false;
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const src = node.nodeValue;
    const at = src.replace(/\\s+/g, ' ').indexOf(sentence);
    if (at < 0) continue;
    // Map whitespace-normalised offsets back onto the raw text node.
    const toRaw = target => {
      let raw = 0, norm = 0, prevSpace = false;
      while (raw < src.length && norm < target) {
        const space = /\\s/.test(src[raw]);
        if (!(space && prevSpace)) norm++;
        prevSpace = space; raw++;
      }
      return raw;
    };
    const range = document.createRange();
    range.setStart(node, toRaw(at));
    range.setEnd(node, toRaw(at + sentence.length));
    const box = range.getBoundingClientRect();
    if (box.top < 0 || box.bottom > innerHeight - 80)
      window.scrollBy({top: box.top - innerHeight / 3, behavior: 'instant'});
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return selection.toString().replace(/\\s+/g, ' ').trim() === sentence;
  }
  return false;
}"""


# ---------------------------------------------------------------------------
# Bundle snapshot
# ---------------------------------------------------------------------------


def _tracked_bundle_snapshot(bundle: Path, dest: Path) -> Path | None:
    """Copy the bundle's git-tracked files (working-tree content) into ``dest``.

    A working checkout accumulates ignored session state beside the concepts
    — ``.okf-loom/`` comment and activity history, per-route lock files —
    which the studio faithfully renders as "Recently changed" and agent
    activity. README media must show what a fresh clone shows, so the capture
    serves only what git tracks. Returns ``None`` when the bundle is not in a
    git work tree or git tracks none of it.
    """
    try:
        top = subprocess.run(
            ["git", "-C", str(bundle), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        listing = subprocess.run(
            ["git", "-C", top, "ls-files", "-z", "--", str(bundle)],
            capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    root = Path(top).resolve()
    try:
        prefix = bundle.resolve().relative_to(root)
    except ValueError:
        return None
    snapshot = dest / bundle.name
    copied = 0
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        rel = Path(os.fsdecode(raw))
        source = root / rel
        if not source.is_file():  # tracked, but deleted in the working tree
            continue
        target = snapshot / rel.relative_to(prefix)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    return snapshot if copied else None


# ---------------------------------------------------------------------------
# Captures
# ---------------------------------------------------------------------------


def _stage_stills(browser, base: str, stage: Path,
                  diagnostics: _BrowserDiagnostics) -> list[str]:
    """Capture every still into ``stage``; returns the file names in order."""
    names: list[str] = []

    def still(name: str) -> Path:
        names.append(name)
        return stage / name

    def page_for(label: str, viewport: dict, color_scheme: str = "light", touch: bool = False):
        return _new_page(browser, viewport=viewport, dpr=2, color_scheme=color_scheme,
                         diagnostics=diagnostics, label=label, touch=touch)

    def settled_light(p) -> None:
        _settle(p, theme="bone")

    # Dashboard index, once the studio launcher reports its session.
    ctx, page = page_for("studio-home", STILL_VIEWPORT)
    capture_target(page, f"{base}/", "document", still("studio-home.png"),
                   full_page=False, prepare=settled_light)
    ctx.close()

    # Showcase concept: the Diagrams heading sits just under the sticky
    # chrome so the Mermaid diagrams and the KaTeX block share the frame.
    def frame_showcase(p) -> None:
        settled_light(p)
        p.wait_for_function(_SHOWCASE_RENDERED_JS, timeout=15_000)
        p.evaluate("() => document.fonts.ready.then(() => true)")
        if not p.evaluate(_SCROLL_UNDER_CHROME_JS, ["h2, h3", r"^\s*Diagrams", 16]):
            raise RuntimeError("demo/showcase has no Diagrams heading")
        # End the frame just above the Code section so no heading is sliced.
        cut = p.evaluate("""() => {
          const h = [...document.querySelectorAll('h2, h3')]
            .find(x => /^\\s*Code with syntax/i.test(x.textContent));
          return h ? Math.floor(h.getBoundingClientRect().top) : null;
        }""")
        if cut and STILL_VIEWPORT["height"] < cut <= SHOWCASE_VIEWPORT["height"] + 40:
            p.set_viewport_size({"width": SHOWCASE_VIEWPORT["width"],
                                 "height": min(cut - 4, SHOWCASE_VIEWPORT["height"])})
    ctx, page = page_for("showcase-rendering", SHOWCASE_VIEWPORT)
    capture_target(page, f"{base}/demo/showcase", "document", still("showcase-rendering.png"),
                   full_page=False, prepare=frame_showcase)
    ctx.close()

    # Select-to-comment: select one exact sentence so the affordance anchors
    # to it, open the composer, and type a representative ask for the agent.
    def compose(p) -> None:
        settled_light(p)
        if not p.evaluate(_SELECT_SENTENCE_JS, COMPOSER_SENTENCE):
            raise RuntimeError(f"could not select {COMPOSER_SENTENCE!r} on /{COMPOSER_PAGE}")
        p.click(".okf-comment-afford button", timeout=5_000)
        composer = p.locator('textarea[aria-label="Comment for agent"]')
        composer.fill(COMPOSER_NOTE)
        # The opening drawer pushes the page, which can leave a link under the
        # pointer; park it on the composer so no link preview covers the text.
        box = composer.bounding_box()
        if box:
            p.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        p.wait_for_function(_POPOVER_HIDDEN_JS, timeout=5_000)
    ctx, page = page_for("comment-composer", COMPOSER_VIEWPORT)
    capture_target(page, f"{base}/{COMPOSER_PAGE}", "document", still("comment-composer.png"),
                   full_page=False, prepare=compose)
    ctx.close()

    # Graph: Map lens in light mode, Bridges lens in dark mode.
    ctx, page = page_for("graph-map", STILL_VIEWPORT)
    capture_target(page, f"{base}/__graph", "graph", still("graph-map.png"),
                   full_page=False, prepare=settled_light)
    ctx.close()

    def bridges(p) -> None:
        _settle(p, theme="slate")
        _click_lens(p, "Bridges")
        # Clicking the lens scrolls the detail panel; show it from its top so
        # the overview note is not sliced.
        p.evaluate("() => { const d = document.getElementById('okf-detail'); if (d) d.scrollTop = 0; }")
    ctx, page = page_for("graph-bridges-dark", STILL_VIEWPORT, color_scheme="dark")
    capture_target(page, f"{base}/__graph", "graph", still("graph-bridges-dark.png"),
                   full_page=False, prepare=bridges)
    ctx.close()

    # Live search.
    ctx, page = page_for("search", STILL_VIEWPORT)
    capture_target(page, f"{base}/__search?q={SEARCH_QUERY}", "search", still("search.png"),
                   full_page=False, prepare=settled_light)
    ctx.close()

    # Phone: a tutorial at the top with the full chrome, then the same page
    # after two swipes with the chrome scrolled away to the Sections row.
    ctx, page = page_for("phone", PHONE_VIEWPORT, touch=True)
    capture_target(page, f"{base}/{PHONE_PAGE}", "document", still("phone-top.png"),
                   full_page=False, prepare=settled_light)
    _scroll_chrome_away(ctx, page)
    page.screenshot(path=str(still("phone-scrolled.png")), type="png", full_page=False)
    ctx.close()

    # Repository card, composed from the dark Bridges still.
    _capture_social_preview(browser, base, stage / "graph-bridges-dark.png",
                            still("social-preview.png"), diagnostics)
    return names


def _capture_stills(browser, base: str, out_dir: Path,
                    diagnostics: _BrowserDiagnostics) -> list[Path]:
    """Capture the stills as one set: nothing is published unless all pass."""
    published: list[Path] = []
    with tempfile.TemporaryDirectory(prefix=".okf-media-stills-", dir=out_dir) as staging:
        stage = Path(staging)
        names = _stage_stills(browser, base, stage, diagnostics)
        diagnostics.raise_if_failed()
        for name in names:
            destination = out_dir / name
            # Private capture temporaries are 0600; README media are public.
            os.chmod(stage / name, 0o644)
            claim = _claim_destination(destination)
            if not _publish_if_current(stage / name, destination, claim):
                raise RuntimeError(f"A newer capture owns {name} publication.")
            published.append(destination)
    return published


# The card is plain HTML over the studio's own stylesheet, so its colours and
# display face follow the shipped design tokens instead of hand-copied hex.
_SOCIAL_CARD = string.Template("""<!doctype html>
<html lang="en" data-theme="slate">
<head>
<meta charset="utf-8">
<title>okf-loom</title>
<link rel="stylesheet" href="$fonts">
<link rel="stylesheet" href="$stylesheet">
<style>
  html, body { margin: 0; width: 1280px; height: 640px; overflow: hidden;
    background: var(--okf-bg); }
  .card { position: relative; width: 1280px; height: 640px; color: var(--okf-fg);
    font-family: var(--okf-font-body); }
  .card__copy { position: absolute; left: 72px; top: 0; bottom: 0; width: 500px;
    display: flex; flex-direction: column; justify-content: center; }
  .card__eyebrow { margin: 0 0 18px; color: var(--okf-fg-muted);
    font: 600 16px/1.2 var(--font-mono);
    letter-spacing: 0.04em; }
  .card__title { margin: 0; font-family: var(--okf-font-display); font-size: 96px;
    line-height: 1; font-weight: 700; letter-spacing: -0.02em; }
  .card__rule { width: 132px; height: 6px; margin: 28px 0 30px; border-radius: 3px;
    background: var(--okf-accent); }
  .card__lede { margin: 0 0 22px; font-size: 27px; line-height: 1.4;
    color: var(--okf-fg-muted); }
  .card__pitch { margin: 0 0 34px; font-size: 27px; line-height: 1.35; font-weight: 700; }
  .card__cmd { margin: 0; color: var(--okf-accent);
    font: 500 21px/1 var(--font-mono); }
  .card__shot { position: absolute; margin: 0; left: 612px; top: 56px; width: 668px;
    height: 528px; overflow: hidden; border: 1px solid var(--okf-border-strong);
    border-right: 0; border-radius: 12px 0 0 12px; background: var(--okf-bg-elev);
    box-shadow: 0 28px 64px rgba(0, 0, 0, 0.5); }
  .card__shot img { display: block; width: 864px; height: 540px; }
</style>
</head>
<body>
<main class="card">
  <div class="card__copy">
    <p class="card__eyebrow">$eyebrow</p>
    <h1 class="card__title">okf-loom</h1>
    <div class="card__rule"></div>
    <p class="card__lede">Plain-Markdown knowledge trees, served as a live studio.</p>
    <p class="card__pitch">You comment. Your agent edits.<br>Files stay yours.</p>
    <p class="card__cmd">&#36; scripts/okf-loom serve docs</p>
  </div>
  <figure class="card__shot"><img src="$shot" alt=""></figure>
</main>
</body>
</html>
""")


def _stylesheet_href(base: str, name: str = "wiki.css") -> str:
    """The ``name`` stylesheet URL exactly as the live studio links it."""
    with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
        page_html = resp.read().decode("utf-8", "replace")
    match = re.search(rf'<link[^>]+href="([^"]*/{re.escape(name)}[^"]*)"', page_html)
    if not match:
        raise RuntimeError(f"the live studio index does not link {name}")
    return html.unescape(match.group(1))


def _capture_social_preview(browser, base: str, shot: Path, out_path: Path,
                            diagnostics: _BrowserDiagnostics) -> Path:
    """Compose the 1280x640 repository card around ``shot``.

    The card and the shot are served on the studio's own origin through
    request interception, so the production stylesheet resolves normally.
    """
    card_url = f"{base}/__readme-media/social-preview"
    shot_url = f"{base}/__readme-media/{shot.name}"
    card = _SOCIAL_CARD.substitute(
        stylesheet=html.escape(_stylesheet_href(base), quote=True),
        fonts=html.escape(_stylesheet_href(base, "ledger-fonts.css"), quote=True),
        shot=html.escape(shot_url, quote=True),
        eyebrow=html.escape(f"v{LOOM_VERSION} · implements OKF {SPEC_VERSION}"),
    )
    ctx, page = _new_page(browser, viewport=SOCIAL_VIEWPORT, dpr=1, color_scheme="dark",
                          diagnostics=diagnostics, label="social-preview")
    try:
        page.route(card_url, lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=card))
        page.route(shot_url, lambda route: route.fulfill(
            status=200, content_type="image/png", path=str(shot)))
        page.goto(card_url, wait_until="load", timeout=15_000)
        page.evaluate("() => document.fonts.ready.then(() => true)")
        page.wait_for_function("""() => {
          const img = document.querySelector('.card__shot img');
          return !!img && img.complete && img.naturalWidth > 0;
        }""", timeout=10_000)
        accent = page.evaluate("""() => getComputedStyle(document.documentElement)
          .getPropertyValue('--okf-accent').trim()""")
        if not accent:
            raise RuntimeError("social card did not pick up the studio design tokens")
        if not page.evaluate("""() => ['Public Sans', 'Azeret Mono'].every(family =>
          [...document.fonts].some(face => face.family.replace(/["']/g, '') === family
            && face.status === 'loaded'))"""):
            raise RuntimeError("social card did not load the Ledger faces")
        page.screenshot(path=str(out_path), type="png", full_page=False)
    finally:
        ctx.close()
    return out_path


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
        "--in-place",
        action="store_true",
        help="serve --bundle as-is, including ignored session state, instead of "
             "a snapshot of its git-tracked files",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "ERROR: playwright is not installed.\n"
            "  python -m pip install playwright\n"
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
    diagnostics = _BrowserDiagnostics()
    try:
        with tempfile.TemporaryDirectory(prefix="okf-readme-bundle-") as snapshot_root:
            served = bundle
            if not args.in_place:
                snapshot = _tracked_bundle_snapshot(bundle, Path(snapshot_root))
                if snapshot is None:
                    _log("bundle has no git-tracked files; serving it in place")
                else:
                    served = snapshot
                    _log("serving a snapshot of the bundle's git-tracked files")
            with sync_playwright() as p:
                browser = launch_chromium(p, args.chrome)
                try:
                    with _LiveServer(served) as base:
                        captured.extend(_capture_stills(browser, base, out_dir, diagnostics))
                finally:
                    browser.close()
    except (RuntimeError, SystemExit) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"# wrote {len(captured)} media files to {out_dir}")
    for path in captured:
        size = path.stat().st_size if path.exists() else 0
        shown = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        print(f"  {shown}  ({size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
