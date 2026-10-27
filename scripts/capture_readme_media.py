#!/usr/bin/env python3
"""Capture the curated README media set (docs/media/) from the live studio.

Unlike ``capture_viewer_proof.py`` (dated proof artefacts for spec §17),
this script regenerates the STABLE, hand-picked set of images the README
links to. Filenames are fixed so the README never needs updating when the
media is refreshed:

* ``hero-light.png``         — the README hero: a resolved comment loop (the
                               anchored sentence, the agent's edit and its
                               reply in the Comments rail), Bone palette
* ``hero-dark.png``          — the same moment in Slate, for GitHub's dark theme
* ``studio-home.png``        — dashboard index with the studio connected,
                               ending on an inventory group boundary
* ``palettes.png``           — the studio home in Bone, Slate, Clay and Plum
* ``showcase-rendering.png`` — Mermaid/KaTeX section of demo/showcase in
                               Focus view, wide enough for the whole flowchart
* ``comment-composer.png``   — an exact-text selection with the composer open
* ``graph-bridges-dark.png`` — Bridges lens, dark mode, hovering the top
                               bridger so its connections and preview show
* ``search.png``             — Best overall results for a query, the first
                               result's evidence open: its ``[path#Lx-Ly]``
                               citation and why it ranked
* ``phone-top.png``          — a tutorial on a 390x844 touch phone, at the
                               top of the page with the full chrome
* ``phone-scrolled.png``     — the same page after two swipes, with the
                               chrome scrolled away to the Sections row
* ``social-preview.png``     — 1280x640 repository card built from the
                               dark hero and the live design tokens

The animated showcase clips (``comment-loop``, ``appearance``, ``graph`` and
``search``, each as MP4 + GIF) are recorded by ``record_showcase_media.py``.

Requires Playwright plus a Chromium-family browser::

    python -m pip install playwright
    playwright install chromium               # OR have Google Chrome installed
    PYTHONPATH=scripts python scripts/capture_readme_media.py

Browser resolution order: ``--chrome PATH``, ``OKF_CHROME``,
``AIC_PLAYWRIGHT_CHROME_PATH``, known system Chrome paths, the ``chrome``
channel, then Playwright-managed Chromium. Stills are DPR-2 for crispness.
A host that cannot reach the studio's pinned CDN libraries adds
``--cdn-mirror vendor`` (see ``cdn_mirror.py``).

The stills show the product defaults a new reader gets: no appearance
storage is seeded, and each still selects its Ledger palette (Bone, or
Slate for the dark graph) through theme.js's public API only. The
studio is served from a snapshot of the bundle's git-tracked files, so
ignored session state in a working checkout (comment history, activity
logs, lock files) never leaks into the media; ``--in-place`` serves the
bundle directory as-is instead. The hero runs a real comment loop (posted in
the page, then claimed, edited and resolved with ``scripts/okf-loom``) on a
separate throwaway snapshot, so the other stills never see its edit. Any
console error, uncaught page error, failed request, or HTTP error response
fails the run.
"""
from __future__ import annotations

import argparse
import html
import json
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
import urllib.parse
import urllib.request
from pathlib import Path

import cdn_mirror
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
# A 3:2 hero: tall enough that the Comments rail can start on a whole block
# and still end below the resolved card's actions.
HERO_VIEWPORT = {"width": 1440, "height": 960}
# The home frame runs on past the fold to the first ruled inventory groups,
# then trims to end just above the group named here.
HOME_VIEWPORT = {"width": 1440, "height": 1200}
HOME_END_BEFORE_GROUP = 4
# The showcase uses Focus view, whose wider workspace fits the flowchart
# without a horizontal scroll; both diagrams and the KaTeX block share the
# frame, which is trimmed to end just below the math.
SHOWCASE_VIEWPORT = {"width": 1440, "height": 1100}
# The Comments rail sits beside the page; the Ledger header fits at 1440.
COMPOSER_VIEWPORT = STILL_VIEWPORT
# Search shows the match modes, then the first result's evidence; the frame
# trims to end below its "why it ranked" table.
SEARCH_VIEWPORT = {"width": 1440, "height": 1300}
SOCIAL_VIEWPORT = {"width": 1280, "height": 640}
# Each palette panel of palettes.png, before it is set two by two.
PALETTE_PANEL = {"width": 1280, "height": 800}
PALETTES = (("bone", "light", "Bone", "default"), ("slate", "dark", "Slate", ""),
            ("clay", "dark", "Clay", ""), ("plum", "dark", "Plum", ""))
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
SEARCH_QUERY = "live studio"
SEARCH_MODE = "hybrid"  # "Best overall": its evidence names both rankers
GRAPH_HOVER_NODE = "reference/cli"  # the Bridges lens's top bridger

# The comment loop the hero shows, and the showcase clip records. The edit
# adds the explanation right after the anchored sentence without touching the
# sentence itself, so the annotation stays attached to it.
LOOP_PAGE = "tutorials/live_studio_basics"
LOOP_TITLE = "Use the live studio"
LOOP_SENTENCE = "This tutorial assumes you have a small bundle to point at."
LOOP_BODY = "What is a bundle? Say it in plain words."
LOOP_EDIT_OLD = "If you do not, finish"
LOOP_EDIT_NEW = ("A bundle is just a folder of Markdown pages.\n"
                 "If you do not have one, finish")
LOOP_SUMMARY = "Explained what a bundle is"
LOOP_REPLY = "Added one plain sentence that says what a bundle is."
LOOP_ACTOR = "docs-agent"


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
    """Run ``scripts/okf-loom serve <bundle>`` on an ephemeral port.

    The stills serve a fixed snapshot, so the file watcher is off unless
    ``watch`` asks for it (the hero edits its snapshot while serving).
    """

    def __init__(self, bundle: Path, *, watch: bool = False) -> None:
        self.bundle = bundle
        self.watch = watch
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> str:
        self._proc = subprocess.Popen(
            [
                str(REPO_ROOT / "scripts" / "okf-loom"), "serve", str(self.bundle),
                "--host", "127.0.0.1", "--port", str(self.port),
                *(() if self.watch else ("--no-watch",)), "--no-open",
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


# Set by main() from --cdn-mirror; every capture context routes through it.
_CDN_MIRROR: cdn_mirror.CdnMirror | None = None


def _new_page(browser, *, viewport: dict, dpr: int, color_scheme: str = "light",
              diagnostics: _BrowserDiagnostics | None = None, label: str = "",
              touch: bool = False):
    """Context+page for one capture.

    No appearance storage is written, so theme.js (the only owner of that
    state) resolves the shipped defaults. Explicit capture palettes are selected
    through its public API by _settle, never by storage injection. The graph tour needs no pre-dismissal: the live graph only offers
    it behind an invitation button.
    """
    # A fixed locale and zone keep any time the studio prints (a comment's
    # "created" stamp) the same on every capture host.
    ctx = browser.new_context(viewport=viewport, device_scale_factor=dpr,
                              color_scheme=color_scheme, is_mobile=touch, has_touch=touch,
                              locale="en-US", timezone_id="UTC")
    if _CDN_MIRROR is not None:
        _CDN_MIRROR.attach(ctx)
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


# Markdown link destinations: ``](target)`` and ``](<target>)``.
_LINK_TARGET_RE = re.compile(r"\]\(\s*(?:<([^>\n]+)>|([^)\s]+))")


def _out_of_bundle_link_targets(bundle: Path, sources: list[Path],
                                tracked: set[Path]) -> set[Path]:
    """Git-tracked repository files outside ``bundle`` that its pages link to.

    A concept may link out of its bundle (``../../SKILL.md``). The model
    treats such a link as a valid out-of-bundle reference only when the file
    exists on disk, and as a broken link otherwise. A snapshot copied away
    from the repository would turn every one of them into a broken-link
    marker in the graph that no reader of the real checkout ever sees.
    """
    from urllib.parse import unquote

    bundle_root = bundle.resolve()
    found: set[Path] = set()
    for source in sources:
        if source.suffix.lower() != ".md":
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in _LINK_TARGET_RE.finditer(text):
            raw = (match.group(1) or match.group(2)).split("#", 1)[0].split("?", 1)[0]
            if not raw or raw.startswith("/") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw):
                continue
            target = (source.parent / unquote(raw)).resolve()
            if target.is_relative_to(bundle_root):
                continue
            if target in tracked:
                found.add(target)
    return found


def _tracked_bundle_snapshot(bundle: Path, dest: Path, *,
                             skip_context: tuple[str, ...] = ()) -> Path | None:
    """Copy the bundle's git-tracked files (working-tree content) into ``dest``.

    A working checkout accumulates ignored session state beside the concepts
    — ``.okf-loom/`` comment and activity history, per-route lock files —
    which the studio faithfully renders as "Recently changed" and agent
    activity. README media must show what a fresh clone shows, so the capture
    serves only what git tracks. Returns ``None`` when the bundle is not in a
    git work tree or git tracks none of it.

    Tracked repository files the bundle links out to (``../../SKILL.md``) are
    copied too, at the same position relative to the snapshot, so those links
    resolve exactly as they do in the checkout. ``skip_context`` names
    top-level entries of ``dest`` that the caller provides itself (for
    example a ``scripts`` link to the checkout); nothing is copied under them.
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
        everything = subprocess.run(
            ["git", "-C", top, "ls-files", "-z"],
            capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    root = Path(top).resolve()
    bundle_root = bundle.resolve()
    try:
        prefix = bundle_root.relative_to(root)
    except ValueError:
        return None
    snapshot = dest / bundle.name
    copied: list[Path] = []
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
        copied.append(source)
    if not copied:
        return None
    tracked = {(root / os.fsdecode(raw)).resolve() for raw in everything.split(b"\0") if raw}
    dest_root = dest.resolve()
    for source in sorted(_out_of_bundle_link_targets(bundle_root, copied, tracked)):
        if not source.is_file():
            continue
        target = Path(os.path.normpath(snapshot.resolve() / os.path.relpath(source, bundle_root)))
        if not target.is_relative_to(dest_root):
            continue  # the link climbs above the snapshot root; leave it as is
        parts = target.relative_to(dest_root).parts
        if parts and parts[0] in skip_context:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return snapshot


# ---------------------------------------------------------------------------
# Captures
# ---------------------------------------------------------------------------


# The home index's ruled inventory groups, in page order.
_HOME_GROUP_TOP_JS = """(index) => {
  const groups = [...document.querySelectorAll('main.okf-index section.okf-section')];
  const group = groups[index - 1];
  return group ? Math.floor(group.getBoundingClientRect().top + scrollY) : null;
}"""

# The graph is at rest: the current layout generation has settled and been
# applied (and fitted), and neither the viewport nor any node is animating.
_GRAPH_SETTLED_JS = """() => {
  const s = window.okfGraphCaptureState && window.okfGraphCaptureState();
  const g = window.__okfLoomGraph;
  if (!s || !g || !g.cy) return false;
  const moving = g.cy.animated() || g.cy.nodes().some((n) => n.animated());
  return !!(s.layoutSettled && !s.transitionPending && !moving
            && s.appliedLayoutGeneration === s.layoutGeneration);
}"""

# A node's centre in viewport coordinates.
_GRAPH_NODE_JS = """(id) => {
  const g = window.__okfLoomGraph;
  if (!g || !g.cy) return null;
  const n = g.cy.getElementById(id);
  if (!n || !n.length) return null;
  const c = g.cy.container().getBoundingClientRect();
  const p = n.renderedPosition();
  return {x: c.left + p.x, y: c.top + p.y};
}"""

_GRAPH_TIP_JS = """(title) => {
  const tip = document.querySelector('.okf-graph-tip');
  const name = tip && tip.querySelector('.okf-graph-tip__title');
  if (!tip || !name || name.textContent.trim() !== title) return false;
  const r = tip.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && getComputedStyle(tip).visibility !== 'hidden'
    && getComputedStyle(tip).display !== 'none';
}"""

# The first search result's evidence: its [path.md#Lx-Ly] citation is shown
# and its "why it ranked" table is laid out.
_SEARCH_EVIDENCE_JS = r"""() => {
  const first = document.querySelector('.okf-search-results-list > li');
  if (!first) return null;
  const details = [...first.querySelectorAll('details')]
    .find((d) => /Evidence and context/.test((d.querySelector('summary') || {}).textContent || ''));
  if (!details || !details.open) return null;
  const cite = /\[[^\]\s]+\.md#L\d+(?:-L\d+)?\]/.exec(details.innerText);
  const why = [...details.querySelectorAll('*')]
    .find((e) => /^\s*Why did this result rank here\?\s*$/.test(e.textContent));
  const table = why && (why.nextElementSibling || why.parentElement);
  if (!cite || !table) return null;
  const r = table.getBoundingClientRect();
  return {citation: cite[0], bottom: Math.ceil(r.bottom + scrollY)};
}"""

# The Comments rail scrolled so a thread card shows from its header to its
# action buttons, with the rail's top edge on a block boundary (no half line
# of the block above showing under the rail header).
_RAIL_FRAME_CARD_JS = """([id, margin]) => {
  const panel = document.querySelector('#okf-panel .okf-panel__body');
  const card = document.getElementById('comment-' + id);
  if (!panel || !card || !panel.contains(card)) return null;
  panel.scrollTop = 0;
  const box = panel.getBoundingClientRect();
  const visible = Math.min(box.bottom, innerHeight) - box.top;
  const content = (e) => e.getBoundingClientRect().top - box.top;
  const last = [...card.querySelectorAll('button')].pop() || card;
  const endBottom = last.getBoundingClientRect().bottom - box.top;
  const max = panel.scrollHeight - panel.clientHeight;
  const need = Math.max(0, endBottom - visible + margin);
  const tops = [...panel.querySelectorAll(':scope > *, :scope > * > *, :scope > * > * > *')]
    .map(content).filter((t) => t > 0).sort((a, b) => a - b);
  let target = null;
  for (const t of tops) {
    if (t - margin >= need && t - margin <= max) { target = t - margin; break; }
  }
  if (target === null) return {boundary: false, need, max};
  panel.scrollTop = target;
  return {top: card.getBoundingClientRect().top, bottom: last.getBoundingClientRect().bottom,
          panelTop: box.top, panelBottom: Math.min(box.bottom, innerHeight),
          scrollTop: panel.scrollTop, boundary: true};
}"""


def _okf(args: list[str], *, timeout: float = 30) -> str:
    """Run ``scripts/okf-loom`` from the checkout and return its stdout."""
    result = subprocess.run([str(REPO_ROOT / "scripts" / "okf-loom"), *args],
                            cwd=str(REPO_ROOT), env={**_script_env(), "NO_COLOR": "1"},
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"scripts/okf-loom {args[0]} exited {result.returncode}: "
                           f"{(result.stderr or result.stdout).strip()[-800:]}")
    return result.stdout


def _park_pointer(page, x: float, y: float) -> None:
    """Rest the pointer where it hovers nothing, so no hover state is captured."""
    page.mouse.move(x, y)


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

    # The hero first: it runs its own comment loop on a throwaway snapshot.
    if _HERO_BUNDLE is not None:
        _capture_hero(browser, stage, still, diagnostics)

    # Dashboard index, once the studio launcher reports its session. The
    # frame runs past the fold and ends on an inventory group boundary.
    def frame_home(p) -> None:
        settled_light(p)
        p.evaluate("() => window.scrollTo(0, 0)")
        cut = p.evaluate(_HOME_GROUP_TOP_JS, HOME_END_BEFORE_GROUP)
        if not cut or not STILL_VIEWPORT["height"] < cut <= HOME_VIEWPORT["height"]:
            raise RuntimeError(f"inventory group {HOME_END_BEFORE_GROUP} starts at {cut}px, "
                               f"outside the {HOME_VIEWPORT['height']}px home frame")
        p.set_viewport_size({"width": HOME_VIEWPORT["width"], "height": cut - 4})
        _park_pointer(p, 4, 300)
    ctx, page = page_for("studio-home", HOME_VIEWPORT)
    capture_target(page, f"{base}/", "document", still("studio-home.png"),
                   full_page=False, prepare=frame_home)
    ctx.close()

    # The same home in every Ledger palette, set two by two.
    _capture_palettes(browser, base, stage, still("palettes.png"), diagnostics)

    # Showcase concept in Focus view (the reader's own wide-workspace mode, so
    # the flowchart needs no horizontal scroll): the Diagrams heading sits just
    # under the sticky chrome, and the frame ends just below the KaTeX block.
    def frame_showcase(p) -> None:
        settled_light(p)
        p.wait_for_function(_SHOWCASE_RENDERED_JS, timeout=15_000)
        p.evaluate("() => document.fonts.ready.then(() => true)")
        p.locator(".okf-concept-viewbar").get_by_text("Focus view", exact=True).click(timeout=5_000)
        p.wait_for_function("""() => {
          const ports = [...document.querySelectorAll('.okf-page__body .okf-content-scrollport--diagram')];
          return ports.length >= 2 && ports.every((e) => e.scrollWidth <= e.clientWidth + 1);
        }""", timeout=5_000)
        if not p.evaluate(_SCROLL_UNDER_CHROME_JS, ["h2, h3", r"^\s*Diagrams", 16]):
            raise RuntimeError("demo/showcase has no Diagrams heading")
        p.wait_for_timeout(100)
        bottom = p.evaluate("""() => {
          const m = document.querySelector('.okf-page__body .katex-display');
          const box = m && (m.closest('.okf-content-scrollport') || m);
          return box ? Math.ceil(box.getBoundingClientRect().bottom) : null;
        }""")
        if not bottom or not STILL_VIEWPORT["height"] < bottom + 24 <= SHOWCASE_VIEWPORT["height"]:
            raise RuntimeError(f"the KaTeX block ends at {bottom}px, outside the showcase frame")
        p.set_viewport_size({"width": SHOWCASE_VIEWPORT["width"], "height": bottom + 24})
        p.wait_for_timeout(100)
        if not p.evaluate(_SCROLL_UNDER_CHROME_JS, ["h2, h3", r"^\s*Diagrams", 16]):
            raise RuntimeError("demo/showcase has no Diagrams heading")
        _park_pointer(p, SHOWCASE_VIEWPORT["width"] - 8, 400)
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
        if p.evaluate("() => document.querySelector('.okf-topbar').getBoundingClientRect().height") > 64:
            raise RuntimeError("the top bar wrapped beside the Comments rail")
    ctx, page = page_for("comment-composer", COMPOSER_VIEWPORT)
    capture_target(page, f"{base}/{COMPOSER_PAGE}", "document", still("comment-composer.png"),
                   full_page=False, prepare=compose)
    ctx.close()

    # Graph: the Bridges lens, with the pointer on the top bridger: its connections stay
    # lit, the rest dims, and its preview names it and its link counts.
    def bridges(p) -> None:
        _settle(p, theme="slate")
        _click_lens(p, "Bridges")
        p.wait_for_timeout(150)
        p.wait_for_function(_GRAPH_SETTLED_JS, timeout=15_000)
        # Clicking the lens scrolls the detail panel; show it from its top so
        # the overview note is not sliced.
        p.evaluate("() => { const d = document.getElementById('okf-detail'); if (d) d.scrollTop = 0; }")
        node = p.evaluate(_GRAPH_NODE_JS, GRAPH_HOVER_NODE)
        if not node:
            raise RuntimeError(f"graph node {GRAPH_HOVER_NODE!r} is not on the canvas")
        p.mouse.move(node["x"] - 40, node["y"] + 30)
        p.mouse.move(node["x"], node["y"], steps=6)
        title = p.evaluate("(id) => window.__okfLoomGraph.cy.getElementById(id).data('label')",
                           GRAPH_HOVER_NODE)
        p.wait_for_function(_GRAPH_TIP_JS, arg=title, timeout=5_000)
        p.wait_for_timeout(400)  # the neighbourhood highlight's fade
    ctx, page = page_for("graph-bridges-dark", STILL_VIEWPORT, color_scheme="dark")
    capture_target(page, f"{base}/__graph", "graph", still("graph-bridges-dark.png"),
                   full_page=False, prepare=bridges)
    ctx.close()

    # Search: the match modes, then the first result's evidence (its exact
    # citation and why it ranked), framed from the search terms down.
    def frame_search(p) -> None:
        settled_light(p)
        first = p.locator(".okf-search-results-list > li").first
        first.wait_for(state="visible", timeout=10_000)
        first.locator("summary", has_text="Evidence and context").first.click(timeout=5_000)
        p.wait_for_function(f"() => !!({_SEARCH_EVIDENCE_JS})()", timeout=5_000)
        if not p.evaluate(_SCROLL_UNDER_CHROME_JS, ["*", r"^\s*Search terms\s*$", 14]):
            raise RuntimeError("the search page has no Search terms label")
        p.wait_for_timeout(100)
        evidence = p.evaluate(_SEARCH_EVIDENCE_JS)
        bottom = evidence["bottom"] - p.evaluate("() => scrollY") + 28
        if not STILL_VIEWPORT["height"] < bottom <= SEARCH_VIEWPORT["height"]:
            raise RuntimeError(f"the first result's ranking ends at {bottom}px, outside the frame")
        p.set_viewport_size({"width": SEARCH_VIEWPORT["width"], "height": int(bottom)})
        p.wait_for_timeout(100)
        if not p.evaluate(_SCROLL_UNDER_CHROME_JS, ["*", r"^\s*Search terms\s*$", 14]):
            raise RuntimeError("the search page has no Search terms label")
        _park_pointer(p, SEARCH_VIEWPORT["width"] - 8, 300)
    ctx, page = page_for("search", SEARCH_VIEWPORT)
    query = urllib.parse.urlencode({"q": SEARCH_QUERY, "mode": SEARCH_MODE})
    capture_target(page, f"{base}/__search?{query}", "search", still("search.png"),
                   full_page=False, prepare=frame_search)
    ctx.close()

    # Phone: a tutorial at the top with the full chrome, then the same page
    # after two swipes with the chrome scrolled away to the Sections row.
    ctx, page = page_for("phone", PHONE_VIEWPORT, touch=True)
    capture_target(page, f"{base}/{PHONE_PAGE}", "document", still("phone-top.png"),
                   full_page=False, prepare=settled_light)
    _scroll_chrome_away(ctx, page)
    page.screenshot(path=str(still("phone-scrolled.png")), type="png", full_page=False)
    ctx.close()

    # Repository card, composed from the dark hero (or, without one, the
    # dark graph still).
    shot = stage / ("hero-dark.png" if (stage / "hero-dark.png").exists()
                    else "graph-bridges-dark.png")
    _capture_social_preview(browser, base, shot, still("social-preview.png"), diagnostics)
    return names


# ---------------------------------------------------------------------------
# Hero: one real comment loop, captured once it has resolved
# ---------------------------------------------------------------------------

# Set by main(): the bundle the hero copies for its own comment loop.
_HERO_BUNDLE: Path | None = None


def _hero_snapshot(bundle: Path, dest: Path) -> Path:
    """A throwaway copy of ``bundle`` the hero may comment on and edit."""
    snapshot = _tracked_bundle_snapshot(bundle, dest)
    if snapshot is not None:
        return snapshot
    snapshot = dest / bundle.name
    shutil.copytree(bundle, snapshot, ignore=shutil.ignore_patterns(
        ".okf-loom", "*.okf-lock", ".okf-loom-bundle.lock"))
    return snapshot


def _run_hero_loop(page, base: str, bundle: Path) -> str:
    """Post one exact comment in the page; claim, edit and resolve it by CLI."""
    page.goto(f"{base}/{LOOP_PAGE}", wait_until="load", timeout=20_000)
    _settle(page, theme="bone")
    waiter = subprocess.Popen(
        [str(REPO_ROOT / "scripts" / "okf-loom"), "wait", str(bundle),
         "--for", "comment", "--timeout", "60"],
        cwd=str(REPO_ROOT), env={**_script_env(), "NO_COLOR": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        page.wait_for_timeout(400)
        if not page.evaluate(_SELECT_SENTENCE_JS, LOOP_SENTENCE):
            raise RuntimeError(f"could not select {LOOP_SENTENCE!r} on /{LOOP_PAGE}")
        page.click(".okf-comment-afford button", timeout=5_000)
        composer = page.locator('textarea[aria-label="Comment for agent"]')
        composer.fill(LOOP_BODY)
        composer.press("Enter")
        out, err = waiter.communicate(timeout=30)
    finally:
        if waiter.poll() is None:
            waiter.kill()
            waiter.communicate()
    if waiter.returncode != 0:
        raise RuntimeError(f"scripts/okf-loom wait exited {waiter.returncode}: {err.strip()}")
    comment_id = json.loads(out.strip().splitlines()[-1])["id"]
    _okf(["comment-claim", str(bundle), comment_id, "--actor", LOOP_ACTOR])
    _okf(["replace-text", "--bundle", str(bundle), "--id", LOOP_PAGE, "--actor", LOOP_ACTOR,
          "--old", LOOP_EDIT_OLD, "--new", LOOP_EDIT_NEW])
    _okf(["comment-resolve", str(bundle), comment_id, "--actor", LOOP_ACTOR,
          "--summary", LOOP_SUMMARY, "--reply", LOOP_REPLY])
    return comment_id


def _capture_hero(browser, stage: Path, still, diagnostics: _BrowserDiagnostics) -> None:
    """``hero-light.png`` (Bone) and ``hero-dark.png`` (Slate) of one resolved loop."""
    with tempfile.TemporaryDirectory(prefix="okf-readme-hero-") as root:
        bundle = _hero_snapshot(_HERO_BUNDLE, Path(root))
        with _LiveServer(bundle, watch=True) as base:
            ctx, page = _new_page(browser, viewport=STILL_VIEWPORT, dpr=1,
                                  diagnostics=diagnostics, label="hero-loop")
            try:
                comment_id = _run_hero_loop(page, base, bundle)
            finally:
                ctx.close()

            def frame(theme: str):
                def prepare(p) -> None:
                    _settle(p, theme=theme)
                    p.locator("[data-okf-live-utilities] button",
                              has_text=re.compile(r"^\s*Comments\s*$")).first.click(timeout=5_000)
                    p.wait_for_function(
                        "() => document.body.classList.contains('okf-panel-rail')", timeout=5_000)
                    p.wait_for_function(
                        "([id, reply]) => { const c = document.getElementById('comment-' + id);"
                        " return !!c && /resolved/i.test(c.textContent) && c.textContent.includes(reply); }",
                        arg=[comment_id, LOOP_REPLY], timeout=10_000)
                    if not p.evaluate("(s) => document.querySelector('.okf-page__body')"
                                      ".innerText.includes(s)", LOOP_EDIT_NEW.split("\n", 1)[0]):
                        raise RuntimeError("the hero page does not show the agent's edit")
                    if not p.evaluate(_SCROLL_UNDER_CHROME_JS,
                                      [".okf-page__body :is(h1, h2)",
                                       rf"^\s*{re.escape(LOOP_TITLE)}", 14]):
                        raise RuntimeError(f"/{LOOP_PAGE} has no {LOOP_TITLE!r} heading")
                    card = p.evaluate(_RAIL_FRAME_CARD_JS, [comment_id, 10])
                    if (card is None or not card.get("boundary")
                            or card["top"] < card["panelTop"] - 0.5
                            or card["bottom"] > card["panelBottom"] + 0.5):
                        raise RuntimeError("the resolved card's header and actions do not fit "
                                           f"the Comments rail: {card}")
                    rail = p.locator("#okf-panel").bounding_box()
                    _park_pointer(p, rail["x"] - 10, HERO_VIEWPORT["height"] - 6)
                return prepare

            for theme, scheme, name in (("bone", "light", "hero-light.png"),
                                        ("slate", "dark", "hero-dark.png")):
                ctx, page = _new_page(browser, viewport=HERO_VIEWPORT, dpr=2,
                                      color_scheme=scheme, diagnostics=diagnostics, label=name)
                try:
                    capture_target(page, f"{base}/{LOOP_PAGE}", "document", still(name),
                                   full_page=False, prepare=frame(theme))
                finally:
                    ctx.close()


# ---------------------------------------------------------------------------
# Palettes: the studio home in each Ledger palette
# ---------------------------------------------------------------------------


def _capture_palettes(browser, base: str, stage: Path, out_path: Path,
                      diagnostics: _BrowserDiagnostics) -> Path:
    panels: list[tuple[str, str, str, str]] = []
    for theme, scheme, label, note in PALETTES:
        panel = stage / f".palette-{theme}.png"
        accent: list[str] = []

        def prepare(p, theme=theme, accent=accent) -> None:
            _settle(p, theme=theme)
            p.evaluate("() => window.scrollTo(0, 0)")
            # End the panel above the second inventory group, not across it.
            cut = p.evaluate(_HOME_GROUP_TOP_JS, 2)
            if not cut or not PALETTE_PANEL["height"] * 0.8 < cut <= PALETTE_PANEL["height"] + 4:
                raise RuntimeError(f"inventory group 2 starts at {cut}px, outside the palette panel")
            p.set_viewport_size({"width": PALETTE_PANEL["width"],
                                 "height": min(cut - 4, PALETTE_PANEL["height"])})
            accent.append(p.evaluate(
                "() => getComputedStyle(document.documentElement).getPropertyValue('--okf-accent').trim()"))
            _park_pointer(p, 4, PALETTE_PANEL["height"] - 4)
        ctx, page = _new_page(browser, viewport=PALETTE_PANEL, dpr=2, color_scheme=scheme,
                              diagnostics=diagnostics, label=f"palette-{theme}")
        try:
            capture_target(page, f"{base}/", "document", panel, full_page=False, prepare=prepare)
        finally:
            ctx.close()
        if not accent or not accent[0]:
            raise RuntimeError(f"palette {theme!r} resolved no --okf-accent")
        panels.append((panel.name, label, note, accent[0]))
    figures = "\n".join(
        f'<figure><img class="shot" src="/__readme-media/{html.escape(name)}" alt="">'
        f'<figcaption><i style="background:{html.escape(accent, quote=True)}"></i>'
        f'<b>{html.escape(label)}</b>{"<span>" + html.escape(note) + "</span>" if note else ""}'
        f"</figcaption></figure>"
        for name, label, note, accent in panels)
    page_html = _PALETTE_GRID.substitute(figures=figures)
    return _render_composition(browser, base, label="palettes", page_html=page_html,
                               assets={name: stage / name for name, *_ in panels},
                               viewport={"width": _PALETTE_GRID_WIDTH, "height": 600}, dpr=2,
                               color_scheme="light", out_path=out_path,
                               diagnostics=diagnostics, full_page=True)


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


# Compositions (the repository card, the palette grid) are plain HTML over
# the studio's own stylesheets, served on its origin, so their colours and
# faces follow the shipped design tokens instead of hand-copied values.
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
  .card__rule { width: 132px; height: 6px; margin: 28px 0 30px;
    background: var(--okf-accent); }
  .card__lede { margin: 0 0 22px; font-size: 27px; line-height: 1.4;
    color: var(--okf-fg-muted); }
  .card__pitch { margin: 0 0 34px; font-size: 27px; line-height: 1.35; font-weight: 700; }
  .card__cmd { margin: 0; color: var(--okf-accent);
    font: 500 20px/1 var(--font-mono); white-space: nowrap; }
  .card__shot { position: absolute; margin: 0; left: 612px; top: 56px; width: 668px;
    height: 528px; overflow: hidden; border: 1px solid var(--okf-border-strong);
    border-right: 0; background: var(--okf-bg-elev); }
  /* The page column of the hero, cropped in the gutter before its Related
     rail and the top bar's Studio item. */
  .card__shot img { display: block; width: 1195px; height: auto; }
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
    <p class="card__cmd">&#36; scripts/okf-loom serve docs-bundle</p>
  </div>
  <figure class="card__shot"><img src="/__readme-media/$shot" alt=""></figure>
</main>
</body>
</html>
""")

# Two panels a row: 1280-wide panels drawn 688 wide, captioned like the
# studio's own micro-labels, with each palette's accent as its swatch.
_PALETTE_GRID_WIDTH = 32 + 688 + 32 + 688 + 32
_PALETTE_GRID = string.Template("""<!doctype html>
<html lang="en" data-theme="bone">
<head>
<meta charset="utf-8">
<title>okf-loom palettes</title>
<link rel="stylesheet" href="$$fonts">
<link rel="stylesheet" href="$$stylesheet">
<style>
  html, body { margin: 0; background: var(--okf-bg); color: var(--okf-fg); }
  .grid { display: grid; grid-template-columns: repeat(2, 688px); gap: 36px 32px;
    padding: 32px 32px 30px; width: max-content; }
  figure { margin: 0; }
  .shot { display: block; width: 688px; height: auto; box-sizing: border-box;
    border: 1px solid var(--okf-border-strong); }
  figcaption { display: flex; align-items: center; gap: 9px; margin-top: 13px;
    color: var(--okf-fg-muted); font: 600 12px/1 var(--font-mono);
    letter-spacing: 0.14em; text-transform: uppercase; }
  figcaption i { display: block; width: 10px; height: 10px; }
  figcaption b { color: var(--okf-fg); font: 600 15px/1 var(--okf-font-body);
    letter-spacing: 0; text-transform: none; }
</style>
</head>
<body>
<main class="grid">
$figures
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


def _render_composition(browser, base: str, *, label: str, page_html: str,
                        assets: dict[str, Path], viewport: dict, dpr: int,
                        color_scheme: str, out_path: Path,
                        diagnostics: _BrowserDiagnostics, full_page: bool = False) -> Path:
    """Screenshot ``page_html`` served on the studio's origin.

    ``$fonts`` and ``$stylesheet`` in the page resolve to the studio's own
    Ledger font and wiki stylesheets; ``/__readme-media/<name>`` serves each
    of ``assets``. The run fails unless the design tokens and both Ledger
    faces load and every image decodes.
    """
    url = f"{base}/__readme-media/{label}"
    page_html = string.Template(page_html).safe_substitute(
        stylesheet=html.escape(_stylesheet_href(base), quote=True),
        fonts=html.escape(_stylesheet_href(base, "ledger-fonts.css"), quote=True))
    ctx, page = _new_page(browser, viewport=viewport, dpr=dpr, color_scheme=color_scheme,
                          diagnostics=diagnostics, label=label)
    try:
        page.route(url, lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=page_html))
        def serve_file(path: Path):
            # Playwright passes (route, request) to a two-parameter handler, so
            # the path is bound by closure rather than as a default argument.
            return lambda route: route.fulfill(status=200, content_type="image/png",
                                               path=str(path))
        for name, path in assets.items():
            page.route(f"{base}/__readme-media/{name}", serve_file(path))
        page.goto(url, wait_until="load", timeout=15_000)
        page.evaluate("() => document.fonts.ready.then(() => true)")
        page.wait_for_function("""() => [...document.images].every(
          (img) => img.complete && img.naturalWidth > 0)""", timeout=10_000)
        accent = page.evaluate("""() => getComputedStyle(document.documentElement)
          .getPropertyValue('--okf-accent').trim()""")
        if not accent:
            raise RuntimeError(f"{label} did not pick up the studio design tokens")
        if not page.evaluate("""() => ['Public Sans', 'Azeret Mono'].every(family =>
          [...document.fonts].some(face => face.family.replace(/["']/g, '') === family
            && face.status === 'loaded'))"""):
            raise RuntimeError(f"{label} did not load the Ledger faces")
        page.screenshot(path=str(out_path), type="png", full_page=full_page)
    finally:
        ctx.close()
    return out_path


def _capture_social_preview(browser, base: str, shot: Path, out_path: Path,
                            diagnostics: _BrowserDiagnostics) -> Path:
    """Compose the 1280x640 repository card around ``shot``."""
    card = _SOCIAL_CARD.substitute(
        stylesheet="$stylesheet", fonts="$fonts",
        shot=html.escape(shot.name, quote=True),
        eyebrow=html.escape(f"v{LOOM_VERSION} · implements OKF {SPEC_VERSION}"),
    )
    return _render_composition(browser, base, label="social-preview", page_html=card,
                               assets={shot.name: shot}, viewport=SOCIAL_VIEWPORT, dpr=1,
                               color_scheme="dark", out_path=out_path, diagnostics=diagnostics)


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
    cdn_mirror.add_argument(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
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
    global _CDN_MIRROR, _HERO_BUNDLE
    _CDN_MIRROR = cdn_mirror.from_mode(args.cdn_mirror)
    _HERO_BUNDLE = bundle
    if _CDN_MIRROR is not None:
        _log("serving the pinned CDN libraries from the vendored copies")

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
    except (RuntimeError, SystemExit, PlaywrightError) as exc:
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
