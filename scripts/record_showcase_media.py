#!/usr/bin/env python3
"""Record the okf-loom showcase clips in docs/media/ from the live v2 studio.

Four short screen recordings, each published as an MP4 (H.264, 1280x720,
yuv420p, faststart) and a looping README GIF (960 px wide):

* ``comment-loop`` -- select a sentence and comment on it; the agent's
                      ``wait`` returns the comment, then it claims it, edits the
                      Markdown (the page patches live), and resolves the thread,
                      ending on the whole resolved card in the Comments rail
* ``appearance``   -- the Appearance menu restyling the studio home's ruled
                      inventory live through the Ledger palettes: Slate, Clay,
                      Plum, System, back to Bone
* ``graph``        -- the bundle graph: lenses, Find in graph, a hover
                      preview, and opening a node's page
* ``search``       -- live header search, the full results page with a
                      result's cited evidence, then the Ctrl+K command palette
* ``walkthrough``  -- a captioned tour of the whole studio (MP4 only, for the
                      README's inline player): the home inventory, reading
                      views, the comment loop, search, the palette, the graph
                      and the palettes. Not part of the default run; ask for
                      it with ``--only walkthrough``.

Every clip runs against its own scratch copy of the docs bundle's git-tracked
files, served by ``scripts/okf-loom serve`` on a free local port, so comment
history and edits never touch the checkout. The scratch directory holds
``docs-bundle/`` plus a ``scripts`` link to this checkout's ``scripts/``, so
the terminal panel in the comment loop shows the literal commands the
recorder runs there.

Frames come from Chromium's screencast of a 1280x720 viewport rendered at
device scale 1.5. The screencast delivers CSS-pixel (1280x720) frames, which
keeps the MP4 at 1:1. Each frame keeps its real on-screen timing, and ffmpeg
downscales the GIF with Lanczos. The pointer, click ripples, captions, key hints and
terminal panel are a recording-only overlay: an aria-hidden,
pointer-events:none shadow root injected by this script. It is never part of
the product.

Requires Playwright with a Chromium-family browser, and ffmpeg/ffprobe with
libx264 and the GIF encoder on PATH::

    python scripts/record_showcase_media.py
    python scripts/record_showcase_media.py --only search --only graph
    python scripts/record_showcase_media.py --only walkthrough

A host that cannot reach the studio's pinned CDN libraries adds
``--cdn-mirror vendor`` (see ``cdn_mirror.py``).

Browser resolution follows ``capture_viewer_proof.launch_chromium``:
``--chrome PATH``, ``OKF_CHROME``, ``AIC_PLAYWRIGHT_CHROME_PATH``, system
Chrome, the ``chrome`` channel, then Playwright-managed Chromium.

Nothing is published unless every requested clip records cleanly, encodes
within its size budget and lands inside its duration window. The run fails on
any console error, uncaught page error, failed request or HTTP error
response. It also fails if machine-specific text (checkout or scratch paths,
the home directory, the host name) is visible on screen or in the terminal
panel.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from capture_readme_media import (
    DEFAULT_BUNDLE, REPO_ROOT, _BrowserDiagnostics, _SCROLL_UNDER_CHROME_JS,
    _STUDIO_SETTLED_JS, _free_port, _script_env, _stylesheet_href,
    _tracked_bundle_snapshot, _wait_for_http_ok,
)
from okf_loom import LOOM_VERSION, SPEC_VERSION
import cdn_mirror
from capture_viewer_proof import _claim_destination, _publish_if_current, launch_chromium

__all__ = ["main"]

DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "media"

VIEWPORT = {"width": 1280, "height": 720}
DEVICE_SCALE = 1.5
# Page.startScreencast delivers frames at CSS-pixel size whatever the
# device scale; the 1.5x render still smooths glyph shapes in those frames.
FRAME_SIZE = (1280, 720)
MP4_SIZE = (1280, 720)
MP4_FPS = 30
GIF_WIDTH = 960
GIF_FPS = 10
MB = 1_000_000
MP4_BUDGET = int(2.5 * MB)
MP4_CRF_LADDER = (18, 20, 22, 24, 26, 28)
# (palette colours, paletteuse dither). Bayer scale 5 is the lightest ordered
# dither: flat UI surfaces stay clean and the LZW stream stays small.
GIF_LADDER = ((256, "bayer:bayer_scale=5"), (192, "bayer:bayer_scale=5"),
              (128, "bayer:bayer_scale=5"), (96, "bayer:bayer_scale=5"))

_SERVER_STARTUP_TIMEOUT = 20.0
_CLI_TIMEOUT = 30.0
_WAIT_FOR_COMMENT_TIMEOUT = 20.0
# Characters per displayed terminal line: the panel is 700 px wide with
# 15 px monospace (about 9 px per character, 12 px padding a side), so its
# text stays legible once GitHub scales the 960 px GIF to its README column.
_TERMINAL_WIDTH = 74

# The comment loop reads a plain-language tutorial opening: the body's own
# first heading is scrolled up under the sticky chrome so the frame's top edge
# is the title and first paragraphs, not the header band's ids, paths and
# metadata.
COMMENT_PAGE = "tutorials/live_studio_basics"
COMMENT_TITLE = "Use the live studio"
COMMENT_SENTENCE = "This tutorial assumes you have a small bundle to point at."
COMMENT_BODY = "What is a bundle? Say it in plain words."
# The edit adds the explanation right after the anchored sentence without
# touching the sentence itself, so the annotation stays attached to it.
EDIT_OLD = "If you do not, finish"
EDIT_NEW = ("A bundle is just a folder of Markdown pages.\n"
            "If you do not have one, finish")
RESOLVE_SUMMARY = "Explained what a bundle is"
RESOLVE_REPLY = "Added one plain sentence that says what a bundle is."
ACTOR = "docs-agent"
# The studio home, framed on its ruled inventory: type chips, numbered rows,
# mono ids and tabular link counts are the Ledger structure every palette keeps.
APPEARANCE_PAGE = ""
APPEARANCE_HEADING = "What do you want to explore?"
SEARCH_QUERY = "live studio"
PALETTE_QUERY = "search"
PALETTE_TARGET = "Search modes"
GRAPH_NODE = "explanation/live_studio_design"
# The graph opens on Map; two more lenses reframe it. Flow is left out: at
# 1280x720 it draws this bundle as small unlabelled dots joined by long
# crossing edges, which does not read as left-to-right layers.
GRAPH_LENSES = ("Themes", "Bridges")
GRAPH_FIND = "comment"


def _log(msg: str) -> None:
    print(f"[record_showcase_media] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Recording-only overlay (pointer, ripple, caption, key hints, terminal)
# ---------------------------------------------------------------------------

_OVERLAY_JS = r"""(() => {
  if (window.__okfRec) return;
  // Paint gate: a new document closes it at once; it reopens when the page is
  // ready to be seen (a Studio page once its live runtime has booted, which
  // hides the pre-boot "Live Studio is unavailable" notice; any other page
  // once it has parsed). The recorder keeps no frame while it is closed.
  // Both edges carry wall-clock milliseconds, the clock the screencast stamps
  // its frames with, so frames painted before the binding call lands are
  // still sorted to the right side of the edge.
  const gate = (open, at) => { try { window.__okfRecGate && window.__okfRecGate(open, at); } catch (e) { /* no binding */ } };
  const now = () => performance.timeOrigin + performance.now();
  if (window.top === window) {
    gate(false, performance.timeOrigin);
    // A still page paints no new frame once the gate is open, so force one
    // just after the recorder's settle margin; otherwise the last frame kept
    // would be the previous page.
    const afterPaint = () => requestAnimationFrame(() => requestAnimationFrame(() => {
      gate(true, now());
      setTimeout(() => { if (window.__okfRec) window.__okfRec.nudge(); }, 200);
    }));
    const watchBoot = () => {
      const root = document.documentElement;
      const settled = () => root.classList.contains('okf-studio-booted')
        || ['ready', 'failed'].includes(root.dataset.okfStudioStatus || '');
      if (!document.querySelector('.okf-live-studio-fallback') || settled()) { afterPaint(); return; }
      const watcher = new MutationObserver(() => { if (settled()) { watcher.disconnect(); afterPaint(); } });
      watcher.observe(root, {attributes: true, attributeFilter: ['class', 'data-okf-studio-status']});
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', watchBoot, {once: true});
    else watchBoot();
  }
  const KEY = '__okf_showcase_overlay__';
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(KEY) || '{}') || {}; } catch (e) { saved = {}; }
  const st = Object.assign({x: null, y: null, cap: '', step: '', term: false, lines: []}, saved);
  addEventListener('pagehide', () => {
    try { sessionStorage.setItem(KEY, JSON.stringify(st)); } catch (e) { /* storage full */ }
  });
  const CSS = `
:host { all: initial; }
.cur { position: fixed; left: 0; top: 0; width: 22px; height: 26px;
  transform: translate(-60px, -60px); will-change: transform;
  filter: drop-shadow(0 1px 1.5px rgba(0,0,0,.5)); }
.cur svg { display: block; transform-origin: 2px 2px; transition: transform 90ms ease-out; }
.cur.down svg { transform: scale(.84); }
.rip { position: fixed; left: 0; top: 0; width: 46px; height: 46px; margin: -23px 0 0 -23px;
  border-radius: 50%; border: 3px solid #14b8a6; background: rgba(20,184,166,.16);
  box-shadow: 0 0 0 1px rgba(255,255,255,.75); animation: rip 560ms ease-out forwards; }
@keyframes rip { from { transform: scale(.25); opacity: 1; } to { transform: scale(1.3); opacity: 0; } }
.cap { position: fixed; left: 16px; bottom: 16px; display: flex; align-items: center; gap: 10px;
  max-width: calc(100vw - 32px); padding: 8px 16px 8px 8px; border-radius: 999px;
  background: rgba(15,23,42,.93); color: #f8fafc; box-shadow: 0 8px 24px rgba(0,0,0,.28);
  font: 600 17px/1.3 system-ui, "DejaVu Sans", "Segoe UI", Arial, sans-serif;
  transition: opacity 220ms ease, transform 220ms ease; }
.cap.off { opacity: 0; transform: translateY(8px); }
.chip { flex: none; padding: 3px 10px; border-radius: 999px; background: #2dd4bf; color: #042f2e;
  font: 700 14px/1.3 system-ui, "DejaVu Sans", "Segoe UI", Arial, sans-serif; }
.chip:empty { display: none; }
.keys { display: flex; gap: 5px; margin-left: 4px; transition: opacity 200ms ease; }
.keys.off { opacity: 0; }
kbd { display: inline-block; min-width: 14px; padding: 2px 8px; border-radius: 6px;
  border: 1px solid #94a3b8; border-bottom-width: 3px; background: #1e293b; color: #f8fafc;
  text-align: center; font: 700 13px/1.3 "DejaVu Sans Mono", ui-monospace, Menlo, Consolas, monospace; }
.term { position: fixed; left: 16px; bottom: 92px; width: 700px; border-radius: 10px;
  background: #0b1220; color: #cbd5e1; overflow: hidden;
  border: 1px solid rgba(148,163,184,.28); box-shadow: 0 14px 34px rgba(0,0,0,.34);
  transition: opacity 260ms ease, transform 260ms ease; }
.term.off { opacity: 0; transform: translateY(14px); }
.bar { display: flex; align-items: center; gap: 7px; height: 26px; padding: 0 11px;
  background: #121b2f; border-bottom: 1px solid rgba(148,163,184,.18); color: #94a3b8;
  font: 600 13px/1 system-ui, "DejaVu Sans", "Segoe UI", Arial, sans-serif; }
.bar i { width: 10px; height: 10px; border-radius: 50%; background: #334155; }
.bar span { margin-left: 6px; }
.pad { padding: 6px 12px; }
.body { height: 114px; overflow: hidden; display: flex;
  flex-direction: column; justify-content: flex-end; white-space: pre;
  font: 15px/19px "DejaVu Sans Mono", ui-monospace, Menlo, Consolas, monospace; }
.ln { min-height: 19px; }
.ln.cmd { color: #e2e8f0; }
.ln.out { color: #94a3b8; }
.ln.ok { color: #6ee7b7; }
.ps { color: #34d399; }
.caret { display: inline-block; width: 8px; height: 15px; vertical-align: -2px; background: #e2e8f0;
  animation: blink 1s steps(1) infinite; }
@keyframes blink { 50% { opacity: 0; } }`;
  const CURSOR = '<svg width="22" height="26" viewBox="0 0 22 26" aria-hidden="true">'
    + '<path d="M2 2 L2 21 L6.9 16.6 L10.2 24 L13.6 22.5 L10.4 15.3 L17 15 Z" fill="#fff" '
    + 'stroke="#0f172a" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  let host = null, els = null;
  function place() {
    if (!els || st.x === null) return;
    els.cur.style.transform = `translate(${st.x - 2}px, ${st.y - 2}px)`;
  }
  function renderLine(kind, text) {
    const ln = document.createElement('div');
    ln.className = 'ln ' + kind;
    if (kind === 'cmd' && text.startsWith('$ ')) {
      const ps = document.createElement('span'); ps.className = 'ps'; ps.textContent = '$ ';
      const rest = document.createElement('span'); rest.textContent = text.slice(2);
      ln.append(ps, rest);
    } else {
      ln.textContent = text;
    }
    return ln;
  }
  function renderTerm() {
    if (!els) return;
    els.term.classList.toggle('off', !st.term);
    els.body.replaceChildren(...st.lines.slice(-12).map(([k, t]) => renderLine(k, t)));
  }
  function renderCap() {
    if (!els) return;
    // Keys that have already faded out still take room in the pill; a new
    // caption drops them so it is only as wide as its own words.
    if (els.keys.classList.contains('off')) els.keys.replaceChildren();
    els.chip.textContent = st.step || '';
    els.txt.textContent = st.cap || '';
    els.cap.classList.toggle('off', !st.cap);
  }
  function install() {
    if (host) return;
    host = document.createElement('okf-showcase-overlay');
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;'
      + 'display:block;contain:strict;opacity:1;';
    const root = host.attachShadow({mode: 'open'});
    root.innerHTML = `<style>${CSS}</style>
      <div class="term off"><div class="bar"><i></i><i></i><i></i><span>docs-agent \u2014 terminal</span></div>
      <div class="pad"><div class="body"></div></div></div>
      <div class="cap off"><span class="chip"></span><span class="txt"></span><span class="keys off"></span></div>
      <div class="cur">${CURSOR}</div>`;
    els = {
      root, term: root.querySelector('.term'), body: root.querySelector('.body'),
      cap: root.querySelector('.cap'), chip: root.querySelector('.chip'),
      txt: root.querySelector('.txt'), keys: root.querySelector('.keys'),
      cur: root.querySelector('.cur'),
    };
    document.documentElement.appendChild(host);
    place(); renderCap(); renderTerm();
  }
  addEventListener('mousemove', (e) => { st.x = e.clientX; st.y = e.clientY; place(); },
                   {capture: true, passive: true});
  addEventListener('mousedown', (e) => {
    if (!els) return;
    els.cur.classList.add('down');
    const r = document.createElement('div');
    r.className = 'rip';
    r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px';
    els.root.appendChild(r);
    setTimeout(() => r.remove(), 700);
  }, {capture: true, passive: true});
  addEventListener('mouseup', () => { if (els) els.cur.classList.remove('down'); },
                   {capture: true, passive: true});
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  let keysTimer = 0;
  window.__okfRec = {
    ready: () => !!els,
    caption(text, step) { st.cap = text || ''; st.step = step || ''; renderCap(); },
    keys(names, ms) {
      if (!els) return;
      els.keys.replaceChildren(...names.map((n) => {
        const k = document.createElement('kbd'); k.textContent = n; return k;
      }));
      els.keys.classList.remove('off');
      clearTimeout(keysTimer);
      keysTimer = setTimeout(() => els.keys.classList.add('off'), ms || 1400);
    },
    term(on) { st.term = !!on; renderTerm(); },
    termOut(lines) { st.lines.push(...lines); renderTerm(); },
    async termType(lines, cps) {
      // Types command lines at a steady rate; resolves when the last is shown.
      for (const text of lines) {
        st.lines.push(['cmd', '']);
        const slot = st.lines[st.lines.length - 1];
        const t0 = performance.now();
        let shown = 0;
        while (shown < text.length) {
          await sleep(16);
          const want = Math.min(text.length,
            Math.floor((performance.now() - t0) / 1000 * cps) + 1);
          if (want !== shown) { shown = want; slot[1] = text.slice(0, shown); renderTerm(); }
        }
      }
      return true;
    },
    termWaiting(on) {
      if (!els) return;
      const last = els.body.lastElementChild;
      els.root.querySelectorAll('.caret').forEach((c) => c.remove());
      if (on) {
        const line = document.createElement('div'); line.className = 'ln';
        const caret = document.createElement('span'); caret.className = 'caret';
        line.appendChild(caret); els.body.appendChild(line);
      } else if (last && !last.textContent) {
        last.remove();
      }
    },
    // Screencast only emits a frame when something paints; this forces one.
    nudge() {
      if (!host) return;
      host.style.opacity = host.style.opacity === '1' ? '0.999' : '1';
    },
    text() { return els ? [st.cap, st.step, ...st.lines.map((l) => l[1])].join('\n') : ''; },
    // Layout boxes (ignoring the show/hide transform) of the caption and the
    // terminal, so a director can keep page content and toasts clear of them.
    rects() {
      if (!els) return null;
      const box = (e) => ({left: e.offsetLeft, top: e.offsetTop,
                           right: e.offsetLeft + e.offsetWidth, bottom: e.offsetTop + e.offsetHeight});
      return {cap: st.cap ? box(els.cap) : null, term: box(els.term)};
    },
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, {once: true});
  else install();
})();"""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def shell_display(argv: list[str], width: int = _TERMINAL_WIDTH) -> list[str]:
    """Render ``argv`` as the literal shell lines a person would type.

    Arguments are POSIX-quoted with :func:`shlex.quote`, an option stays on
    the same line as its value, and long commands wrap with `` \\``
    continuations indented four spaces. A quoted value that contains a
    newline continues on the next physical line, exactly as a shell shows it.
    """
    words = [shlex.quote(a) for a in argv]
    groups: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word.startswith("--") and i + 1 < len(words) and not words[i + 1].startswith("--"):
            groups.append(f"{word} {words[i + 1]}")
            i += 2
        else:
            groups.append(word)
            i += 1
    lines: list[str] = []
    current = "$ " + groups[0]
    for group in groups[1:]:
        tail = current.rsplit("\n", 1)[-1]
        head = group.split("\n", 1)[0]
        if len(tail) + 1 + len(head) + 2 > width:
            lines.append(current + " \\")
            current = "    " + group
        else:
            current += " " + group
    lines.append(current)
    return [physical for line in lines for physical in line.split("\n")]


def wrap_output(line: str, width: int = _TERMINAL_WIDTH) -> list[str]:
    """Soft-wrap one line of command output to ``width`` columns.

    Breaks at spaces where it can (continuations keep the line's indent plus
    two spaces) and hard-breaks a word longer than the line, so nothing is
    ever clipped at the panel's right edge.
    """
    if len(line) <= width:
        return [line]
    indent = line[:len(line) - len(line.lstrip(" "))]
    return textwrap.wrap(line, width, initial_indent="", subsequent_indent=indent + "  ",
                         break_on_hyphens=False, drop_whitespace=True) or [line[:width]]


def concat_script(frames: list[tuple[float, str]], tail: float) -> str:
    """ffconcat list giving each screencast frame its real on-screen duration.

    ``frames`` are ``(timestamp_seconds, file_name)``; a frame lasts until the
    next one arrives, and the final frame lasts ``tail`` seconds. The concat
    demuxer ignores the duration of the last entry, so it is listed twice.
    """
    if not frames:
        raise ValueError("no frames were recorded")
    ordered = sorted(frames)
    lines = ["ffconcat version 1.0"]
    for index, (stamp, name) in enumerate(ordered):
        following = ordered[index + 1][0] if index + 1 < len(ordered) else stamp + tail
        lines.append(f"file '{name}'")
        lines.append(f"duration {max(following - stamp, 0.001):.6f}")
    lines.append(f"file '{ordered[-1][1]}'")
    return "\n".join(lines) + "\n"


def private_text_patterns(extra: tuple[str, ...] = ()) -> list[re.Pattern]:
    """Patterns for machine-specific text that must never appear on screen."""
    literals = {str(REPO_ROOT), str(Path.home()), *extra}
    patterns = [re.compile(re.escape(value)) for value in sorted(literals) if len(value) > 1]
    host = socket.gethostname()
    if host and host not in {"localhost"}:
        patterns.append(re.compile(rf"(?<![\w-]){re.escape(host)}(?![\w-])"))
    patterns.append(re.compile(r"trycloudflare\.com|/home/[\w.-]+/|/Users/[\w.-]+/"))
    return patterns


def find_private_text(text: str, patterns: list[re.Pattern]) -> list[str]:
    return sorted({m.group(0) for p in patterns for m in p.finditer(text)})


def condensed_comment(raw: str, width: int = _TERMINAL_WIDTH) -> list[str]:
    """The JSON object ``okf-loom wait`` prints, trimmed to what a reader needs.

    Keeps ``id``, ``state`` and ``body`` in their printed order, marks any
    omitted keys with an ellipsis, and wraps between members to ``width``.
    """
    item = json.loads(raw.strip().splitlines()[-1])
    members = [f"{json.dumps(key)}: {json.dumps(item[key], ensure_ascii=False)}"
               for key in item if key in ("id", "state", "body")]
    if len(members) < len(item):
        members.append("\u2026")
    lines: list[str] = []
    current = "{"
    for index, member in enumerate(members):
        piece = member + ("," if index < len(members) - 1 else "}")
        if current not in ("{", " ") and len(current) + 1 + len(piece) > width:
            lines.append(current)
            current = " "
        current += piece if current in ("{", " ") else " " + piece
    lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Scratch workspace and live studio
# ---------------------------------------------------------------------------


class _Scratch:
    """``<tmp>/docs-bundle`` (git-tracked files) plus ``<tmp>/scripts`` -> checkout."""

    def __init__(self, bundle: Path) -> None:
        self.bundle = bundle
        self._tmp: tempfile.TemporaryDirectory | None = None
        self.root = Path()

    def __enter__(self) -> "_Scratch":
        self._tmp = tempfile.TemporaryDirectory(prefix="okf-showcase-")
        self.root = Path(self._tmp.name)
        # The scripts/ link below already makes links into scripts/ resolve.
        snapshot = _tracked_bundle_snapshot(self.bundle, self.root, skip_context=("scripts",))
        if snapshot is None or snapshot.name != "docs-bundle":
            self.__exit__()
            raise RuntimeError("the showcase needs the git-tracked docs-bundle/ of this checkout")
        (self.root / "scripts").symlink_to(REPO_ROOT / "scripts", target_is_directory=True)
        return self

    def __exit__(self, *exc) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def cli(self, args: list[str], **kwargs) -> subprocess.Popen:
        return subprocess.Popen(
            [str(self.root / "scripts" / "okf-loom"), *args], cwd=str(self.root),
            env={**_script_env(), "NO_COLOR": "1"}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


class _Studio:
    """``scripts/okf-loom serve docs-bundle`` (watching) on a free port."""

    def __init__(self, scratch: _Scratch) -> None:
        self.scratch = scratch
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._proc: subprocess.Popen | None = None
        self._log_path = scratch.root / "serve.log"

    def __enter__(self) -> str:
        with self._log_path.open("wb") as log:
            self._proc = subprocess.Popen(
                [str(self.scratch.root / "scripts" / "okf-loom"), "serve", "docs-bundle",
                 "--host", "127.0.0.1", "--port", str(self.port), "--no-open"],
                cwd=str(self.scratch.root), env=_script_env(),
                stdout=log, stderr=subprocess.STDOUT)
        if not _wait_for_http_ok(self.base, timeout=_SERVER_STARTUP_TIMEOUT):
            tail = self._log_path.read_text(errors="replace")[-2000:]
            self.__exit__()
            raise RuntimeError(f"scripts/okf-loom serve did not become ready:\n{tail}")
        return self.base

    def __exit__(self, *exc) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Screencast recorder
# ---------------------------------------------------------------------------


class _PaintGate:
    """The spans of time a newly loaded document was not yet ready to be seen.

    The overlay reports, in wall-clock milliseconds, when each new document
    started and when it was ready (a Studio page once its live runtime has
    booted and hidden the pre-boot notice; see ``_OVERLAY_JS``). Frames the
    screencast stamped inside a span are dropped, so a navigation cuts
    straight from the old page to the ready new one. A span that never
    closes ends ``limit`` seconds after it opened.
    """

    # The page and the compositor stamp time separately; a span ends a few
    # frames late so the frame that still showed the notice is never kept.
    # (The overlay forces a fresh frame 0.2 s after a gate opens, past this.)
    SETTLE = 0.12

    def __init__(self, limit: float = 4.0) -> None:
        self.limit = limit
        self.spans: list[list[float]] = []

    def set(self, _source, is_open, at_ms) -> None:
        at = float(at_ms) / 1000.0
        if not is_open:
            self.spans.append([at, at + self.limit])
        elif self.spans and self.spans[-1][0] <= at:
            self.spans[-1][1] = at + self.SETTLE

    def hides(self, stamp: float) -> bool:
        return any(start <= stamp < end for start, end in self.spans)


class _Screencast:
    """Chromium ``Page.startScreencast`` frames, written to disk with timestamps."""

    def __init__(self, page, frames_dir: Path, gate: _PaintGate | None = None) -> None:
        self.page = page
        self.dir = frames_dir
        self.gate = gate or _PaintGate()
        self.frames: list[tuple[float, str]] = []
        self._recording = False
        self._cdp = page.context.new_cdp_session(page)
        self._cdp.on("Page.screencastFrame", self._on_frame)

    def _on_frame(self, params: dict) -> None:
        if self._recording:
            stamp = float((params.get("metadata") or {}).get("timestamp") or time.time())
            name = f"{len(self.frames):06d}.jpg"
            (self.dir / name).write_bytes(base64.b64decode(params["data"]))
            self.frames.append((stamp, name))
        # Every frame is acknowledged, or the screencast stalls.
        self._cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

    def start(self) -> None:
        self._recording = True
        self._cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 92, "everyNthFrame": 1,
            "maxWidth": FRAME_SIZE[0], "maxHeight": FRAME_SIZE[1],
        })
        self.page.evaluate("() => window.__okfRec.nudge()")
        self.page.wait_for_timeout(120)
        if not self.frames:
            raise RuntimeError("the screencast produced no first frame")

    def stop(self) -> list[tuple[float, str]]:
        self.page.evaluate("() => window.__okfRec.nudge()")
        self.page.wait_for_timeout(150)
        self._recording = False
        self._cdp.send("Page.stopScreencast")
        self._cdp.detach()
        kept = [frame for frame in self.frames if not self.gate.hides(frame[0])]
        if not kept:
            raise RuntimeError("every screencast frame fell inside a page load")
        return kept


# ---------------------------------------------------------------------------
# Take: one clip's page, pointer, overlay and CLI
# ---------------------------------------------------------------------------


def _ease(t: float) -> float:
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


class _Take:
    def __init__(self, page, base: str, scratch: _Scratch, patterns: list[re.Pattern]) -> None:
        self.page = page
        self.base = base
        self.scratch = scratch
        self.patterns = patterns
        self.x = VIEWPORT["width"] * 0.62
        self.y = VIEWPORT["height"] * 0.55
        self._background: list[subprocess.Popen] = []

    # -- page ----------------------------------------------------------------
    def goto(self, route: str) -> None:
        self.page.goto(f"{self.base}/{route.lstrip('/')}", wait_until="load", timeout=20_000)
        self.page.wait_for_function(_STUDIO_SETTLED_JS, timeout=15_000)
        self.page.wait_for_function("() => window.__okfRec && window.__okfRec.ready()",
                                    timeout=5_000)
        self.page.evaluate("() => document.fonts.ready.then(() => true)")

    def hold(self, ms: float) -> None:
        self.page.wait_for_timeout(ms)

    def check_public(self, where: str) -> None:
        visible = self.page.evaluate(
            "() => [document.title, document.body.innerText, window.__okfRec.text()].join('\\n')")
        leaks = find_private_text(visible, self.patterns)
        if leaks:
            raise RuntimeError(f"machine-specific text on screen ({where}): {leaks}")

    # -- overlay --------------------------------------------------------------
    def caption(self, text: str, step: str = "") -> None:
        self.page.evaluate("([t, s]) => window.__okfRec.caption(t, s)", [text, step])

    def keys(self, *names: str) -> None:
        self.page.evaluate("(k) => window.__okfRec.keys(k, 1500)", list(names))

    # -- pointer --------------------------------------------------------------
    def park(self, x: float, y: float) -> None:
        self.x, self.y = x, y
        self.page.mouse.move(x, y)

    def glide(self, x: float, y: float, ms: float = 600) -> None:
        steps = max(6, int(ms / 16))
        x0, y0 = self.x, self.y
        for i in range(1, steps + 1):
            e = _ease(i / steps)
            self.page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            self.page.wait_for_timeout(ms / steps * 0.7)
        self.x, self.y = x, y

    def center(self, locator, dx: float = 0.5, dy: float = 0.5) -> tuple[float, float]:
        locator.wait_for(state="visible", timeout=10_000)
        box = locator.bounding_box()
        if not box:
            raise RuntimeError(f"no box for {locator}")
        return box["x"] + box["width"] * dx, box["y"] + box["height"] * dy

    def click(self, locator, *, ms: float = 550, dx: float = 0.5, dy: float = 0.5,
              settle: float = 120) -> None:
        x, y = self.center(locator, dx, dy)
        self.glide(x, y, ms)
        self.hold(settle)
        self.page.mouse.down()
        self.hold(90)
        self.page.mouse.up()

    def wheel(self, dy: float, *, steps: int = 6, pause: float = 45) -> None:
        for _ in range(steps):
            self.page.mouse.wheel(0, dy / steps)
            self.page.wait_for_timeout(pause)

    # -- terminal -------------------------------------------------------------
    def type_command(self, args: list[str], cps: float) -> list[str]:
        lines = shell_display(["scripts/okf-loom", *args])
        leaks = find_private_text("\n".join(lines), self.patterns)
        if leaks:
            raise RuntimeError(f"machine-specific text in a displayed command: {leaks}")
        self.page.evaluate("([l, c]) => window.__okfRec.termType(l, c)", [lines, cps])
        return lines

    def show_output(self, lines: list[tuple[str, str]]) -> None:
        leaks = find_private_text("\n".join(t for _, t in lines), self.patterns)
        if leaks:
            raise RuntimeError(f"machine-specific text in displayed output: {leaks}")
        self.page.evaluate("(l) => window.__okfRec.termOut(l)", [list(x) for x in lines])

    def start_cli(self, args: list[str]) -> subprocess.Popen:
        proc = self.scratch.cli(args)
        self._background.append(proc)
        return proc

    def finish_cli(self, proc: subprocess.Popen, what: str, timeout: float = _CLI_TIMEOUT) -> str:
        """Pump the page (so screencast frames keep flowing) until ``proc`` exits."""
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{what} did not finish within {timeout:g}s")
            self.page.wait_for_timeout(40)
        out, err = proc.communicate()
        self._background.remove(proc)
        if proc.returncode != 0:
            raise RuntimeError(f"{what} exited {proc.returncode}: {err.strip() or out.strip()}")
        return out

    def run_cli(self, args: list[str], *, cps: float = 95) -> str:
        self.type_command(args, cps)
        out = self.finish_cli(self.start_cli(args), " ".join(args[:1]))
        shown: list[tuple[str, str]] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            kind = ("ok" if "\u2713" in line or re.search(r"\b(claimed|resolved)\b", line)
                    else "out")
            # The panel wraps a long output line the way a terminal would,
            # instead of clipping it at the right edge.
            shown += [(kind, part) for part in wrap_output(line.rstrip())]
        self.show_output(shown)
        return out

    def close(self) -> None:
        """Stop any CLI still running (a failed take must not leave a waiter behind)."""
        for proc in self._background:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()
        self._background.clear()


# ---------------------------------------------------------------------------
# Clip directors
# ---------------------------------------------------------------------------

_SENTENCE_BOX_JS = """([sentence, top]) => {
  const body = document.querySelector('.okf-page__body');
  if (!body) return null;
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const at = node.nodeValue.indexOf(sentence);
    if (at < 0) continue;
    const range = document.createRange();
    range.setStart(node, at);
    range.setEnd(node, at + sentence.length);
    if (top !== null) {
      window.scrollBy({top: range.getBoundingClientRect().top - top, behavior: 'instant'});
    }
    const rects = [...range.getClientRects()];
    const first = rects[0], last = rects[rects.length - 1];
    return {x0: first.left + 1, y0: first.top + first.height / 2,
            x1: last.right - 1, y1: last.top + last.height / 2,
            top: first.top, bottom: last.bottom};
  }
  return null;
}"""

# The thread card (#comment-<id>) inside the Comments rail's scroller, the
# scroller's visible box, and how far the scroller is from its end.
_COMMENT_CARD_JS = """(id) => {
  const panel = document.querySelector('#okf-panel .okf-panel__body');
  const card = document.getElementById('comment-' + id);
  if (!panel || !card || !panel.contains(card)) return null;
  const r = card.getBoundingClientRect();
  const p = panel.getBoundingClientRect();
  const state = card.querySelector('.okf-comment__state');
  return {top: r.top, bottom: r.bottom, panelTop: p.top,
          panelBottom: Math.min(p.bottom, innerHeight), panelLeft: p.left, panelRight: p.right,
          room: panel.scrollHeight - panel.clientHeight - panel.scrollTop,
          state: state ? state.textContent.trim() : ''};
}"""

# The empty strip between the reading column's text and the Comments rail:
# the pointer rests there between steps, clear of the text and every control.
_READING_MARGIN_JS = """() => {
  const body = document.querySelector('.okf-page__body');
  const rail = document.querySelector('#okf-panel');
  if (!body || !rail) return null;
  const b = body.getBoundingClientRect(), r = rail.getBoundingClientRect();
  return {x: (b.right + r.left) / 2, gap: r.left - b.right};
}"""

_TOASTS_JS = """() => [...document.querySelectorAll('.okf-toast')]
  .map((t) => t.getBoundingClientRect())
  .filter((r) => r.width > 0 && r.height > 0)
  .map((r) => ({left: r.left, top: r.top, right: r.right, bottom: r.bottom}))"""


def _overlap(a: dict, b: dict, pad: float = 4) -> bool:
    return (a["left"] < b["right"] + pad and b["left"] < a["right"] + pad
            and a["top"] < b["bottom"] + pad and b["top"] < a["bottom"] + pad)


def _overlay_clear_of_toasts(take: _Take, where: str) -> None:
    """Fail the take if the caption or terminal would cover a studio toast."""
    rects = take.page.evaluate("() => window.__okfRec.rects()") or {}
    for toast in take.page.evaluate(_TOASTS_JS):
        for name in ("cap", "term"):
            box = rects.get(name)
            if box and _overlap(box, toast):
                raise RuntimeError(f"the recording {name} covers a studio toast ({where}): "
                                   f"{box} vs {toast}")


def _scroll_rail_to_end(take: _Take, comment_id: str, rest: tuple[float, float]) -> None:
    """Wheel the Comments rail (only the rail) to its end, then rest the pointer.

    At the end, the rail keeps following the last card as the agent's claim,
    reply and resolution grow it, so the finished card lands fully in view.
    """
    page = take.page
    card = page.evaluate(_COMMENT_CARD_JS, comment_id)
    if card is None:
        raise RuntimeError(f"comment {comment_id} has no card in the Comments rail")
    if card["room"] <= 1:
        return
    page_y = page.evaluate("() => window.scrollY")
    # Hover the rail before the wheel: a wheel over the reading column would
    # scroll the page and carry the anchored sentence out of view.
    take.glide((card["panelLeft"] + card["panelRight"]) / 2,
               card["panelTop"] + (card["panelBottom"] - card["panelTop"]) * 0.45, 600)
    for _ in range(3):
        # An exact wheel: an over-long one would chain to the page at the end.
        take.wheel(card["room"], steps=6, pause=40)
        take.hold(260)
        card = page.evaluate(_COMMENT_CARD_JS, comment_id)
        if card["room"] <= 1:
            break
    else:
        raise RuntimeError("the Comments rail did not reach its end")
    if page.evaluate("() => window.scrollY") != page_y:
        raise RuntimeError("the wheel scrolled the page instead of the Comments rail")
    take.glide(*rest, 500)


def _setup_comment_loop(take: _Take) -> tuple[float, float]:
    page = take.page
    take.goto(COMMENT_PAGE)
    # Open the Comments drawer first: at 1280 px it becomes a rail that
    # reflows the header, which must not happen in the middle of a selection.
    page.locator("[data-okf-live-utilities] button", has_text=re.compile(r"^\s*Comments\s*$")).click()
    page.wait_for_function("() => document.body.classList.contains('okf-panel-rail')",
                           timeout=5_000)
    take.hold(700)
    # Frame the tutorial's plain-language opening: its own first heading
    # (the Markdown H1, rendered as an H2 under the page title) just under the
    # sticky chrome, the anchored sentence a few lines below, and the
    # terminal panel clear underneath it.
    if not page.evaluate(_SCROLL_UNDER_CHROME_JS,
                         [".okf-page__body :is(h1, h2)", rf"^\s*{re.escape(COMMENT_TITLE)}", 14]):
        raise RuntimeError(f"/{COMMENT_PAGE} has no {COMMENT_TITLE!r} heading")
    take.hold(300)
    box = page.evaluate(_SENTENCE_BOX_JS, [COMMENT_SENTENCE, None])
    if not box:
        raise RuntimeError(f"{COMMENT_SENTENCE!r} is not on /{COMMENT_PAGE}")
    term = page.evaluate("() => window.__okfRec.rects()")["term"]
    if box["bottom"] + 24 > term["top"]:
        raise RuntimeError(f"{COMMENT_SENTENCE!r} would sit under the terminal panel "
                           f"(sentence bottom {box['bottom']:.0f}, panel top {term['top']})")
    margin = page.evaluate(_READING_MARGIN_JS)
    if not margin or margin["gap"] < 24:
        raise RuntimeError(f"no empty margin between the reading column and the rail: {margin}")
    rest = (margin["x"], box["top"] - 36)
    take.park(*rest)
    take.hold(400)
    take.check_public("comment-loop start")
    return rest


def _act_comment_loop(take: _Take, rest: tuple[float, float]) -> None:
    page = take.page
    take.caption("Comment on any sentence", "1/4")
    page.evaluate("() => window.__okfRec.term(true)")
    take.hold(700)
    wait_args = ["wait", "docs-bundle", "--for", "comment", "--timeout", "60"]
    take.type_command(wait_args, 80)
    waiter = take.start_cli(wait_args)
    page.evaluate("() => window.__okfRec.termWaiting(true)")
    take.hold(500)

    box = page.evaluate(_SENTENCE_BOX_JS, [COMMENT_SENTENCE, None])
    take.glide(box["x0"], box["y0"], 800)
    take.hold(150)
    page.mouse.down()
    take.glide(box["x1"], box["y1"], 750)
    page.mouse.up()
    take.hold(500)
    take.click(page.locator(".okf-comment-afford button"), ms=500)
    composer = page.locator('textarea[aria-label="Comment for agent"]')
    composer.wait_for(state="visible", timeout=5_000)
    take.hold(350)
    take.click(composer, ms=600, dx=0.3, dy=0.4)
    take.hold(200)
    page.keyboard.type(COMMENT_BODY, delay=48)
    take.hold(350)
    take.keys("Enter")
    page.keyboard.press("Enter")

    out = take.finish_cli(waiter, "wait", timeout=_WAIT_FOR_COMMENT_TIMEOUT)
    comment_id = json.loads(out.strip().splitlines()[-1])["id"]
    page.evaluate("() => window.__okfRec.termWaiting(false)")
    take.show_output([("out", line) for line in condensed_comment(out)])
    take.hold(700)

    take.caption("The agent picks it up", "2/4")
    page.wait_for_function(f"(id) => ({_COMMENT_CARD_JS})(id) !== null", arg=comment_id,
                           timeout=5_000)
    _scroll_rail_to_end(take, comment_id, rest)
    take.hold(200)
    take.run_cli(["comment-claim", "docs-bundle", comment_id, "--actor", ACTOR])
    take.hold(1300)

    take.caption("The page updates live", "3/4")
    take.run_cli(["replace-text", "--bundle", "docs-bundle", "--id", COMMENT_PAGE,
                  "--actor", ACTOR, "--old", EDIT_OLD, "--new", EDIT_NEW], cps=120)
    page.wait_for_function(
        "(s) => document.querySelector('.okf-page__body').innerText.includes(s)",
        arg=EDIT_NEW.split("\n", 1)[0], timeout=10_000)
    take.hold(900)
    _overlay_clear_of_toasts(take, "after the edit")
    take.hold(1900)

    take.caption("It resolves with a reply", "4/4")
    _scroll_rail_to_end(take, comment_id, rest)
    take.run_cli(["comment-resolve", "docs-bundle", comment_id, "--actor", ACTOR,
                  "--summary", RESOLVE_SUMMARY, "--reply", RESOLVE_REPLY], cps=120)
    page.wait_for_function(
        f"(id) => {{ const c = ({_COMMENT_CARD_JS})(id);"
        " return !!c && /resolved/i.test(c.state); }",
        arg=comment_id, timeout=10_000)
    page.wait_for_function(
        "(s) => document.querySelector('#okf-panel').innerText.includes(s)",
        arg=RESOLVE_REPLY, timeout=10_000)
    take.hold(700)
    _overlay_clear_of_toasts(take, "after the resolution")
    # The clip ends on the whole resolved card: nothing of it may be cut off
    # by the rail's edges.
    card = page.evaluate(_COMMENT_CARD_JS, comment_id)
    if card["top"] < card["panelTop"] - 0.5 or card["bottom"] > card["panelBottom"] + 0.5:
        raise RuntimeError(f"the resolved card is not fully inside the Comments rail: {card}")
    take.hold(2700)
    card = page.evaluate(_COMMENT_CARD_JS, comment_id)
    if card["top"] < card["panelTop"] - 0.5 or card["bottom"] > card["panelBottom"] + 0.5:
        raise RuntimeError(f"the resolved card left the Comments rail's view: {card}")
    take.check_public("comment-loop end")


def direct_comment_loop(take: _Take, rec: _Screencast) -> None:
    rest = _setup_comment_loop(take)
    rec.start()
    _act_comment_loop(take, rest)


def _appearance_option(page, group: str, value: str):
    return page.locator(f"label[for='okf-appearance-{group}-{value}']")


def _frame_home_inventory(take: _Take) -> None:
    """Scroll the studio home so its inventory heading sits under the top bar."""
    if not take.page.evaluate(_SCROLL_UNDER_CHROME_JS,
                              ["h2", rf"^\s*{re.escape(APPEARANCE_HEADING)}", 18]):
        raise RuntimeError(f"the studio home has no {APPEARANCE_HEADING!r} heading")


def _setup_appearance(take: _Take) -> None:
    page = take.page
    take.goto(APPEARANCE_PAGE)
    _frame_home_inventory(take)
    take.hold(600)
    take.park(330, 300)  # the gutter beside the inventory heading, over no control
    take.check_public("appearance start")


def _act_appearance(take: _Take) -> None:
    page = take.page
    take.caption("Appearance: one menu, applied live")
    take.hold(900)
    take.click(page.locator(".okf-appearance-trigger"), ms=700)
    page.locator("#okf-appearance-menu").wait_for(state="visible", timeout=5_000)
    take.hold(700)

    for palette in ("slate", "clay", "plum", "system", "bone"):
        take.caption("Palette", palette.title())
        take.click(_appearance_option(page, "theme", palette), ms=450)
        take.hold(1500)

    take.keys("Esc")
    page.keyboard.press("Escape")
    take.hold(1600)
    theme = page.evaluate("() => document.documentElement.dataset.theme")
    if theme != "bone":
        raise RuntimeError(f"palette selection did not return to bone (got {theme!r})")
    take.check_public("appearance end")


def direct_appearance(take: _Take, rec: _Screencast) -> None:
    _setup_appearance(take)
    rec.start()
    _act_appearance(take)


_GRAPH_NODE_JS = """(id) => {
  const g = window.__okfLoomGraph;
  if (!g || !g.cy) return null;
  const n = g.cy.getElementById(id);
  if (!n || !n.length) return null;
  const c = g.cy.container().getBoundingClientRect();
  const p = n.renderedPosition();
  return {x: c.left + p.x, y: c.top + p.y, left: c.left, top: c.top,
          right: c.right, bottom: c.bottom};
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


def _reveal_in_detail_panel(take: _Take, locator) -> None:
    """Wheel the graph's detail panel (#okf-detail) until ``locator`` is on screen.

    At 1280x720 the lens chips sit below the fold of that scrolling panel,
    so the pointer scrolls it the way a reader would before choosing one.
    """
    page = take.page
    panel = page.locator("#okf-detail")
    for _ in range(4):
        box = locator.bounding_box()
        if box is None:
            raise RuntimeError(f"no box for {locator}")
        overflow = box["y"] + box["height"] - (VIEWPORT["height"] - 24)
        if overflow <= 0:
            return
        px, py = take.center(panel)
        if abs(take.x - px) > 200 or abs(take.y - py) > 200:
            take.glide(px, py, 450)
        take.wheel(overflow + 8, steps=5, pause=40)
        take.hold(250)
    raise RuntimeError(f"could not scroll {locator} into view in #okf-detail")


def _setup_graph(take: _Take) -> object:
    page = take.page
    take.goto("__graph")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.cy"
                           " && window.__okfLoomGraph.cy.nodes().length > 5", timeout=15_000)
    page.wait_for_function(_GRAPH_SETTLED_JS, timeout=15_000)
    take.hold(900)
    # At 1280x720 the lens disclosure sits below the detail panel's fold.
    # Scroll it into view before recording, so the GIF budget goes on the
    # lenses rather than on a panel scroll.
    summary = page.locator("details.okf-signal__lenses summary")
    _reveal_in_detail_panel(take, summary)
    take.park(650, 330)  # empty canvas, clear of nodes and the detail panel
    take.check_public("graph start")
    return summary


def _act_graph(take: _Take, summary) -> None:
    page = take.page
    take.caption("The graph: every concept and how it links")
    take.hold(950)
    # Find in graph narrows the canvas to the concepts that match a word. It
    # runs on the Map layout, where the matches read as labelled nodes.
    take.caption("Find in graph by title, id or tag")
    find = page.locator("#okf-search")
    take.click(find, ms=650, dx=0.25)
    take.hold(200)
    page.keyboard.type(GRAPH_FIND, delay=85)
    page.wait_for_function(
        "() => /\\d+ of \\d+ concepts? match/.test(document.querySelector('.okf-graph-tools').innerText)",
        timeout=5_000)
    take.hold(1300)
    clear = page.locator(".okf-graph-tools button", has_text=re.compile(r"^\s*Clear\s*$")).first
    take.click(clear, ms=450)
    page.wait_for_function("() => !document.querySelector('#okf-search').value", timeout=5_000)
    take.hold(300)
    page.wait_for_function(_GRAPH_SETTLED_JS, timeout=10_000)

    take.caption("Lenses reframe the same bundle")
    _reveal_in_detail_panel(take, summary)
    take.click(summary, ms=700)
    if not page.locator("details.okf-signal__lenses").evaluate("d => d.open"):
        raise RuntimeError("the Explore with lenses disclosure did not open")
    take.hold(350)
    for lens in GRAPH_LENSES:
        radio = page.get_by_role("radio", name=re.compile(rf"^{re.escape(lens)}:"))
        label = page.locator("label.okf-signal__seg-item", has=radio)
        _reveal_in_detail_panel(take, label)
        take.click(label, ms=450)
        if not radio.is_checked():
            raise RuntimeError(f"graph lens {lens!r} did not become the checked lens")
        take.caption("Lenses reframe the same bundle", lens)
        # Hold on the lens once its layout has settled and been fitted, not
        # from the click, so every lens is seen at rest for the same time.
        take.hold(150)
        page.wait_for_function(_GRAPH_SETTLED_JS, timeout=10_000)
        take.hold(1100)

    # Hover previews a concept; a double-click opens its page (the graph's
    # own hint). A wheel zoom repaints the whole canvas every frame, which
    # the README GIF budget cannot afford, so the clip stays at the fit.
    take.caption("Hover to preview, double-click to open")
    node = page.evaluate(_GRAPH_NODE_JS, GRAPH_NODE)
    if not node:
        raise RuntimeError(f"graph node {GRAPH_NODE!r} not found")
    take.glide(node["x"], node["y"], 700)
    take.hold(1400)
    page.mouse.dblclick(node["x"], node["y"])
    page.wait_for_url(lambda url: url.split("?")[0].rstrip("/").endswith("/" + GRAPH_NODE),
                      timeout=10_000)
    page.wait_for_load_state("load")
    page.wait_for_function(_STUDIO_SETTLED_JS, timeout=15_000)
    take.glide(318, 596, 500)  # the gutter between the link list and the body, above the caption
    take.hold(1200)
    take.check_public("graph end")


def direct_graph(take: _Take, rec: _Screencast) -> None:
    summary = _setup_graph(take)
    rec.start()
    _act_graph(take, summary)


_FIRST_RESULT_EVIDENCE_JS = r"""() => {
  const first = document.querySelector('.okf-search-results-list > li');
  const details = first && [...first.querySelectorAll('details')]
    .find((d) => /Evidence and context/.test((d.querySelector('summary') || {}).textContent || ''));
  if (!details || !details.open) return null;
  const cite = [...details.querySelectorAll('*')]
    .find((e) => e.children.length === 0 && /^\[[^\]\s]+\.md#L\d+(?:-L\d+)?\]$/.test(e.textContent.trim()));
  if (!cite) return null;
  const r = cite.getBoundingClientRect();
  return {text: cite.textContent.trim(), top: r.top, bottom: r.bottom};
}"""


def _setup_search(take: _Take) -> None:
    page = take.page
    take.goto("")
    take.hold(500)
    take.park(640, 470)  # empty space between the intro and the "New" card
    take.check_public("search start")


def _act_search(take: _Take) -> None:
    page = take.page
    take.caption("Search as you type")
    take.hold(300)
    field = page.locator(".okf-shell input[type=search]").first
    take.click(field, ms=650, dx=0.3)
    take.hold(200)
    page.keyboard.type(SEARCH_QUERY, delay=60)
    results = page.locator(".okf-shell [role=listbox] [role=option], .okf-shell [role=option]")
    results.first.wait_for(state="visible", timeout=5_000)
    take.hold(650)

    # Enter opens every result, each with the exact passage it matched.
    take.keys("Enter")
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/__search\?"), timeout=10_000)
    page.wait_for_load_state("load")
    page.wait_for_function(_STUDIO_SETTLED_JS, timeout=15_000)
    first = page.locator(".okf-search-results-list > li").first
    first.wait_for(state="visible", timeout=10_000)
    take.caption("Every result cites its source")
    take.hold(250)
    summary = first.locator("summary", has_text="Evidence and context").first
    take.wheel(page.evaluate("() => document.querySelector('.okf-search-results-list').getBoundingClientRect().top") - 80,
               steps=8, pause=35)
    take.hold(200)
    take.click(summary, ms=550, dx=0.3)
    page.wait_for_function(f"() => !!({_FIRST_RESULT_EVIDENCE_JS})()", timeout=5_000)
    take.hold(250)
    cite = page.evaluate(_FIRST_RESULT_EVIDENCE_JS)
    rects = page.evaluate("() => window.__okfRec.rects()") or {}
    limit = (rects.get("cap") or {}).get("top", VIEWPORT["height"]) - 30
    if cite["bottom"] > limit:
        take.wheel(cite["bottom"] - limit + 40, steps=6, pause=35)
        take.hold(150)
        cite = page.evaluate(_FIRST_RESULT_EVIDENCE_JS)
    if cite["top"] < 60 or cite["bottom"] > limit:
        raise RuntimeError(f"the citation {cite} is not clear of the chrome and caption")
    take.glide(1180, cite["top"] - 40, 500)  # rest in the empty right margin
    take.hold(1600)

    take.caption("Or jump anywhere with the command palette")
    take.hold(250)
    take.keys("Ctrl", "K")
    page.keyboard.press("Control+K")
    palette = page.get_by_role("dialog", name=re.compile("command", re.I))
    palette.wait_for(state="visible", timeout=5_000)
    take.hold(300)
    page.keyboard.type(PALETTE_QUERY, delay=85)
    # The active row's accessible name gains a "Selected ·" prefix, so match
    # the row by its title text instead of by name.
    target = palette.locator("[role=option]", has=page.locator(
        ".okf-palette__item-title", has_text=re.compile(rf"^{re.escape(PALETTE_TARGET)}$")))
    target.wait_for(state="visible", timeout=5_000)
    take.hold(350)
    for _ in range(10):
        if target.get_attribute("aria-selected") == "true":
            break
        take.keys("\u2193")
        page.keyboard.press("ArrowDown")
        take.hold(300)
    else:
        raise RuntimeError(f"the palette never selected {PALETTE_TARGET!r}")
    take.hold(200)
    take.keys("Enter")
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"search_modes"), timeout=10_000)
    page.wait_for_load_state("load")
    page.wait_for_function(_STUDIO_SETTLED_JS, timeout=15_000)
    take.hold(700)
    take.check_public("search end")


def direct_search(take: _Take, rec: _Screencast) -> None:
    _setup_search(take)
    rec.start()
    _act_search(take)


# ---------------------------------------------------------------------------
# Walkthrough: the whole studio in one captioned take
# ---------------------------------------------------------------------------

WALKTHROUGH_REPO = "github.com/SpatialNexus/okf-loom-v2"
# The home's jump chip for the tutorials group, and the row the tour opens.
WALKTHROUGH_TYPE_CHIP = "Tutorial"
WALKTHROUGH_PAGE_TITLE = COMMENT_TITLE

# Title and end cards: plain HTML over the studio's own Ledger stylesheets,
# served on the studio's origin, so they match the product they frame.
_CARD_HTML = string.Template("""<!doctype html>
<html lang="en" data-theme="slate">
<head>
<meta charset="utf-8">
<title>okf-loom</title>
<link rel="stylesheet" href="$fonts">
<link rel="stylesheet" href="$stylesheet">
<style>
  html, body { margin: 0; height: 100%; background: var(--okf-bg); color: var(--okf-fg);
    font-family: var(--okf-font-body); }
  main { box-sizing: border-box; height: 100%; display: flex; flex-direction: column;
    justify-content: center; padding: 0 120px; }
  .eyebrow { margin: 0 0 20px; color: var(--okf-fg-muted); font: 600 18px/1.2 var(--font-mono);
    letter-spacing: 0.04em; }
  h1 { margin: 0; font-family: var(--okf-font-display); font-size: 104px; line-height: 1;
    font-weight: 700; letter-spacing: -0.02em; }
  .rule { width: 140px; height: 6px; margin: 30px 0 32px; background: var(--okf-accent); }
  .lede { margin: 0; max-width: 900px; font-size: 32px; line-height: 1.35; font-weight: 700; }
  .sub { margin: 18px 0 0; max-width: 900px; font-size: 24px; line-height: 1.45;
    color: var(--okf-fg-muted); }
  .cmd { margin: 40px 0 0; color: var(--okf-accent); font: 500 24px/1 var(--font-mono); }
</style>
</head>
<body>
<main>
  <p class="eyebrow">$eyebrow</p>
  <h1>okf-loom</h1>
  <div class="rule"></div>
  <p class="lede">$lede</p>
  <p class="sub">$sub</p>
  <p class="cmd">$cmd</p>
</main>
</body>
</html>
""")


def _show_card(take: _Take, name: str, *, lede: str, sub: str, cmd: str) -> None:
    """Navigate to a title card served on the studio's origin."""
    page = take.page
    url = f"{take.base}/__showcase-card/{name}"
    body = _CARD_HTML.substitute(
        fonts=_stylesheet_href(take.base, "ledger-fonts.css"),
        stylesheet=_stylesheet_href(take.base),
        eyebrow=f"v{LOOM_VERSION} &middot; implements OKF {SPEC_VERSION}",
        lede=lede, sub=sub, cmd=cmd)
    page.route(url, lambda route: route.fulfill(
        status=200, content_type="text/html; charset=utf-8", body=body))
    page.goto(url, wait_until="load", timeout=15_000)
    page.wait_for_function("() => window.__okfRec && window.__okfRec.ready()", timeout=5_000)
    page.evaluate("() => document.fonts.ready.then(() => true)")
    if not page.evaluate("""() => ['Public Sans', 'Azeret Mono'].every(family =>
      [...document.fonts].some(face => face.family.replace(/["']/g, '') === family
        && face.status === 'loaded'))"""):
        raise RuntimeError(f"the {name} card did not load the Ledger faces")
    take.caption("")


def _walk_home_and_reading(take: _Take) -> None:
    """The home inventory, a jump to the tutorials, and the reading views."""
    page = take.page
    take.goto("")
    take.park(640, 470)
    take.caption("Your docs as one browsable inventory", "Home")
    take.hold(1800)
    take.glide(250, 280, 700)  # over the concept, type and link counts
    take.hold(1200)
    chip = page.locator(f'.okf-type-nav__link[data-index-type="{WALKTHROUGH_TYPE_CHIP}"]')
    take.click(chip.first, ms=700)
    take.hold(1300)
    row = page.locator("main.okf-index a.okf-card__link",
                       has_text=re.compile(rf"^\s*{re.escape(WALKTHROUGH_PAGE_TITLE)}")).first
    take.caption("Open any page to read it", "Read")
    take.click(row, ms=650, dx=0.2)
    page.wait_for_url(lambda url: url.rstrip("/").endswith("/" + COMMENT_PAGE), timeout=10_000)
    page.wait_for_load_state("load")
    page.wait_for_function(_STUDIO_SETTLED_JS, timeout=15_000)
    take.hold(1500)
    # Scroll the page header away, as a reader does, so the views below have
    # the frame to themselves.
    heading = page.evaluate("""(title) => {
      const h = [...document.querySelectorAll('.okf-page__body :is(h1, h2)')]
        .find((e) => e.textContent.trim().startsWith(title));
      return h ? h.getBoundingClientRect().top : null;
    }""", COMMENT_TITLE)
    if heading is None:
        raise RuntimeError(f"/{COMMENT_PAGE} has no {COMMENT_TITLE!r} heading")
    take.glide(1000, 420, 400)  # over the page, clear of the Related rail
    take.wheel(heading - 150, steps=12, pause=40)
    take.hold(900)
    view_bar = page.locator(".okf-concept-viewbar")
    for label, caption in (("Source", "The Markdown source, as it is on disk"),
                           ("Split", "Rendered and source side by side"),
                           ("Rendered", "Back to reading")):
        take.caption(caption, "Read")
        take.click(view_bar.get_by_role("tab", name=re.compile(rf"^\s*{label}\s*$")).first
                   if view_bar.get_by_role("tab").count()
                   else view_bar.get_by_text(label, exact=True).first, ms=550)
        take.hold(1700)
    take.check_public("walkthrough reading")


def direct_walkthrough(take: _Take, rec: _Screencast) -> None:
    page = take.page
    _show_card(take, "title", lede="Read and comment on your Markdown docs in a live studio.",
               sub="Your coding agent picks up each comment, edits the files and replies. "
                   "The files stay plain Markdown.",
               cmd="A two-minute tour")
    take.park(1240, 690)
    rec.start()
    take.hold(3200)

    _walk_home_and_reading(take)

    rest = _setup_comment_loop(take)
    _act_comment_loop(take, rest)
    take.hold(600)
    # Put the agent's terminal away and close the Comments rail, so the next
    # chapters show the studio at its usual width.
    page.evaluate("() => window.__okfRec.term(false)")
    take.click(page.locator("#okf-panel button", has_text=re.compile(r"^\s*Close\s*$")).first,
               ms=600)
    page.wait_for_function("() => !document.body.classList.contains('okf-panel-rail')",
                           timeout=5_000)
    take.hold(500)

    _setup_search(take)
    _act_search(take)

    summary = _setup_graph(take)
    _act_graph(take, summary)

    _setup_appearance(take)
    _act_appearance(take)

    _show_card(take, "end", lede="You comment. Your agent edits. Files stay yours.",
               sub=f"Clone {WALKTHROUGH_REPO} and try it on its own docs:",
               cmd="$ scripts/okf-loom serve docs-bundle")
    take.glide(1240, 690, 500)  # out of the way, in the card's empty corner
    take.hold(4000)
    take.check_public("walkthrough end")


@dataclass(frozen=True)
class ClipSpec:
    name: str
    director: Callable[[_Take, _Screencast], None]
    min_seconds: float
    max_seconds: float
    gif_budget: int
    gif_stats: str = "diff"
    mp4_budget: int = MP4_BUDGET
    gif: bool = True       # README clips ship a looping GIF beside the MP4
    default: bool = True   # recorded when no --only is given


CLIPS: dict[str, ClipSpec] = {spec.name: spec for spec in (
    ClipSpec("comment-loop", direct_comment_loop, 25, 35, 3 * MB),
    ClipSpec("appearance", direct_appearance, 15, 25, 2 * MB, gif_stats="full"),
    ClipSpec("graph", direct_graph, 15, 25, 2 * MB),
    # The search clip shows three surfaces (suggestions, cited results, the
    # palette), so it runs a second longer than the original 15 s cap.
    ClipSpec("search", direct_search, 10, 16, 2 * MB),
    # MP4 only, kept under GitHub's 10 MB limit for a video uploaded into a
    # README through the web editor (the way an inline player is embedded).
    ClipSpec("walkthrough", direct_walkthrough, 90, 180, 0,
             mp4_budget=int(9.5 * MB), gif=False, default=False),
)}


# ---------------------------------------------------------------------------
# Encoding and validation
# ---------------------------------------------------------------------------


def _ffmpeg(*args: str) -> None:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-1500:]}")


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe could not read {path.name}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def encode_mp4(concat: Path, out: Path, budget: int) -> int:
    width, height = MP4_SIZE
    for crf in MP4_CRF_LADDER:
        _ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat),
                "-vf", f"fps={MP4_FPS},scale={width}:{height}:flags=lanczos,format=yuv420p",
                "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-an", str(out))
        if out.stat().st_size <= budget:
            return crf
    raise RuntimeError(f"{out.name} exceeds {budget} bytes even at CRF {MP4_CRF_LADDER[-1]}; "
                       "shorten the clip")


def encode_gif(concat: Path, out: Path, budget: int, stats: str) -> str:
    for colours, dither in GIF_LADDER:
        graph = (f"fps={GIF_FPS},scale={GIF_WIDTH}:-2:flags=lanczos,split[a][b];"
                 f"[a]palettegen=max_colors={colours}:stats_mode={stats}[p];"
                 f"[b][p]paletteuse=dither={dither}:diff_mode=rectangle")
        _ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat), "-vf", graph,
                "-loop", "0", str(out))
        if out.stat().st_size <= budget:
            return f"{colours} colours, {dither}"
    raise RuntimeError(f"{out.name} exceeds {budget} bytes even at {GIF_LADDER[-1][0]} colours; "
                       "shorten or crop the clip")


def _faststart(path: Path) -> bool:
    head = path.read_bytes()[:1 << 20]
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    return moov != -1 and (mdat == -1 or moov < mdat)


def validate(path: Path, spec: ClipSpec) -> dict:
    info = _probe(path)
    stream = next(s for s in info["streams"] if s.get("codec_type") == "video")
    duration = float(info["format"].get("duration") or stream.get("duration") or 0)
    size = path.stat().st_size
    problems = []
    if not spec.min_seconds <= duration <= spec.max_seconds:
        problems.append(f"duration {duration:.2f}s outside {spec.min_seconds:g}-{spec.max_seconds:g}s")
    if path.suffix == ".mp4":
        if stream.get("codec_name") != "h264" or stream.get("pix_fmt") != "yuv420p":
            problems.append(f"codec {stream.get('codec_name')}/{stream.get('pix_fmt')}")
        if (stream.get("width"), stream.get("height")) != MP4_SIZE:
            problems.append(f"size {stream.get('width')}x{stream.get('height')}")
        if size > spec.mp4_budget:
            problems.append(f"{size} bytes > {spec.mp4_budget}")
        if not _faststart(path):
            problems.append("moov atom is not at the front (+faststart)")
    else:
        if stream.get("codec_name") != "gif" or stream.get("width") != GIF_WIDTH:
            problems.append(f"gif stream {stream.get('codec_name')} {stream.get('width')}px")
        if size > spec.gif_budget:
            problems.append(f"{size} bytes > {spec.gif_budget}")
    if problems:
        raise RuntimeError(f"{path.name}: " + "; ".join(problems))
    return {"file": path.name, "seconds": round(duration, 2), "bytes": size,
            "width": stream.get("width"), "height": stream.get("height")}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def record_clip(browser, spec: ClipSpec, stage: Path, work: Path,
                diagnostics: _BrowserDiagnostics,
                mirror: cdn_mirror.CdnMirror | None = None) -> list[dict]:
    frames_dir = work / spec.name
    frames_dir.mkdir(parents=True)
    with _Scratch(DEFAULT_BUNDLE) as scratch, _Studio(scratch) as base:
        patterns = private_text_patterns((str(scratch.root), str(work), str(stage)))
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=DEVICE_SCALE,
                                      color_scheme="light", locale="en-US",
                                      timezone_id="UTC")
        take = None
        try:
            if mirror is not None:
                mirror.attach(context)
            gate = _PaintGate()
            context.expose_binding("__okfRecGate", gate.set)
            context.add_init_script(_OVERLAY_JS)
            page = context.new_page()
            diagnostics.watch(page, spec.name)
            take = _Take(page, base, scratch, patterns)
            rec = _Screencast(page, frames_dir, gate)
            _log(f"{spec.name}: recording")
            spec.director(take, rec)
            frames = rec.stop()
        finally:
            if take is not None:
                take.close()
            context.close()
    diagnostics.raise_if_failed()
    _log(f"{spec.name}: {len(frames)} frames over {frames[-1][0] - frames[0][0]:.1f}s")

    concat = frames_dir / "frames.ffconcat"
    concat.write_text(concat_script(frames, tail=0.1))
    first = _probe(frames_dir / frames[0][1])["streams"][0]
    if (first.get("width"), first.get("height")) != FRAME_SIZE:
        raise RuntimeError(f"{spec.name}: screencast frames are "
                           f"{first.get('width')}x{first.get('height')}, expected {FRAME_SIZE}")
    mp4, gif = stage / f"{spec.name}.mp4", stage / f"{spec.name}.gif"
    crf = encode_mp4(concat, mp4, spec.mp4_budget)
    report = [validate(mp4, spec)]
    report[0]["encoding"] = f"CRF {crf}"
    if spec.gif:
        gif_setting = encode_gif(concat, gif, spec.gif_budget, spec.gif_stats)
        report.append(validate(gif, spec))
        report[1]["encoding"] = gif_setting
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="record_showcase_media.py",
        description="Record the showcase clips (MP4 + GIF) under docs/media/ from a live "
                    "studio serving a scratch copy of docs-bundle/.")
    p.add_argument("--only", action="append", choices=sorted(CLIPS), metavar="NAME",
                   help="record only this clip (repeatable): " + ", ".join(CLIPS)
                        + " (walkthrough is recorded only when named)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR,
                   help=f"output directory (default: {DEFAULT_OUT_DIR.relative_to(REPO_ROOT)})")
    p.add_argument("--chrome", default=None,
                   help="path to Chrome/Chromium (then OKF_CHROME, AIC_PLAYWRIGHT_CHROME_PATH, "
                        "system Chrome, managed Chromium)")
    p.add_argument("--keep-frames", type=Path, default=None, metavar="DIR",
                   help="also keep the raw screencast frames and ffconcat lists in DIR")
    cdn_mirror.add_argument(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"ERROR: {tool} is not on PATH", file=sys.stderr)
            return 1
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        print("ERROR: playwright is not installed.\n"
              "  python -m pip install playwright\n"
              "  playwright install chromium", file=sys.stderr)
        return 1

    names = list(dict.fromkeys(args.only or [n for n, spec in CLIPS.items() if spec.default]))
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    reports: list[dict] = []
    diagnostics = _BrowserDiagnostics()
    try:
        with tempfile.TemporaryDirectory(prefix=".okf-showcase-", dir=out_dir) as staging, \
                tempfile.TemporaryDirectory(prefix="okf-showcase-frames-") as frames_root:
            stage, work = Path(staging), Path(frames_root)
            mirror = cdn_mirror.from_mode(args.cdn_mirror)
            with sync_playwright() as p:
                browser = launch_chromium(p, args.chrome)
                try:
                    for name in names:
                        extra = {"mirror": mirror} if mirror is not None else {}
                        reports.extend(record_clip(browser, CLIPS[name], stage, work,
                                                   diagnostics, **extra))
                finally:
                    browser.close()
                    if args.keep_frames:
                        shutil.copytree(work, args.keep_frames, dirs_exist_ok=True)
            # Publish only once every requested clip has passed.
            for report in reports:
                destination = out_dir / report["file"]
                os.chmod(stage / report["file"], 0o644)
                claim = _claim_destination(destination)
                if not _publish_if_current(stage / report["file"], destination, claim):
                    raise RuntimeError(f"A newer recording owns {report['file']} publication.")
    except (RuntimeError, SystemExit, PlaywrightError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"# wrote {len(reports)} files to {out_dir}")
    for report in reports:
        print(f"  {report['file']:<18} {report['width']}x{report['height']}  "
              f"{report['seconds']:>6.2f}s  {report['bytes'] / 1024:>7.0f} KiB  "
              f"({report['encoding']})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
