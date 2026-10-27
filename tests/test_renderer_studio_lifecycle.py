"""Deterministic lifecycle proof for the current-v2 viewer.

The renderer tests run the shipped browser source in a small VM DOM.  The
test-only resource hooks let the proof hold Mermaid imports/renders without a
network, then release them in the wrong order.  The capture tests exercise the
same public readiness boundary and its typed terminal outcomes.  The optional
browser tests finish the Studio half against the real served page.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "scripts" / "okf_loom" / "viewer" / "static"
RENDERERS = (STATIC / "renderers.js").read_text(encoding="utf-8")
CAPTURE = (STATIC / "capture-readiness.js").read_text(encoding="utf-8")

if shutil.which("node") is None:
    pytest.skip("node is required for lifecycle VM proofs", allow_module_level=True)


def _run_node(program: str, *, timeout: float = 20.0) -> dict:
    result = subprocess.run(
        ["node", "--input-type=commonjs", "-e", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, f"node lifecycle harness failed:\n{result.stderr}\n{result.stdout}"
    text = result.stdout.strip()
    start = text.find("{")
    assert start >= 0, f"node lifecycle harness emitted no JSON:\n{text}"
    return json.loads(text[start:])


_RENDERER_HARNESS = r"""
const vm = require("vm");
const SOURCE = __SOURCE__;

function classList() {
  const values = new Set();
  return {
    add: (...names) => names.forEach(n => values.add(n)),
    remove: (...names) => names.forEach(n => values.delete(n)),
    contains: name => values.has(name),
    toggle: (name, force) => {
      const next = force === undefined ? !values.has(name) : !!force;
      if (next) values.add(name); else values.delete(name);
      return next;
    },
  };
}

function element(tag, text) {
  const attrs = {};
  let inner = "";
  let textValue = text || "";
  const node = {
    nodeType: 1, tagName: tag.toUpperCase(), dataset: {}, style: {},
    classList: classList(), parentNode: null, children: [], id: "",
    className: "", hidden: false,
    getAttribute(name) { return attrs[name] === undefined ? null : attrs[name]; },
    setAttribute(name, value) { attrs[name] = String(value); if (name === "id") this.id = String(value); },
    removeAttribute(name) { delete attrs[name]; },
    hasAttribute(name) { return Object.prototype.hasOwnProperty.call(attrs, name); },
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
    removeChild(child) { this.children = this.children.filter(item => item !== child); child.parentNode = null; },
    insertBefore(child, before) { child.parentNode = this; const i = this.children.indexOf(before); if (i < 0) this.children.push(child); else this.children.splice(i, 0, child); return child; },
    addEventListener() {},
    closest() { return null; },
    querySelector(selector) {
      if (selector === "svg" && this.__hasSvg) return { tagName: "SVG" };
      return null;
    },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { width: 10, height: 10, top: 0, left: 0, right: 10, bottom: 10 }; },
  };
  Object.defineProperty(node, "textContent", {
    configurable: true,
    get() { return textValue; },
    set(value) { textValue = String(value); inner = ""; node.__hasSvg = false; },
  });
  node.isConnected = true;
  Object.defineProperty(node, "innerHTML", {
    get() { return inner; },
    set(value) { inner = String(value); textValue = ""; node.__hasSvg = /<svg\b/i.test(inner); },
  });
  return node;
}

function sandbox(tokens) {
  const root = element("html");
  const body = element("body");
  const head = element("head");
  root.setAttribute("data-theme", "bone");
  const nodes = { mermaid: [] };
  const documentListeners = {};
  const windowListeners = {};
  const document = {
    readyState: "loading", documentElement: root, body, head,
    createElement: tag => element(tag),
    getElementById(id) {
      // Faithful lookup over the sandbox tree (root/head/body and their
      // descendants); the review-bootstrap element is absent here, so this
      // returns null exactly like a real document without that node.
      const stack = [root, head, body];
      while (stack.length) {
        const node = stack.pop();
        if (node.id === id) return node;
        for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
      }
      return null;
    },
    addEventListener(type, fn) { (documentListeners[type] ||= []).push(fn); },
    fire(type, event) { (documentListeners[type] || []).slice().forEach(fn => fn(event || {type})); },
    querySelectorAll(selector) {
      if (selector === "div.mermaid") return nodes.mermaid.slice();
      return [];
    },
    querySelector() { return null; },
  };
  head.appendChild = node => { node.parentNode = head; head.children.push(node); return node; };
  const moduleGate = {};
  moduleGate.promise = new Promise(resolve => { moduleGate.resolve = resolve; });
  const renderCalls = [];
  const initializations = [];
  let oldRenderResolve = null;
  const mermaid = {
    initialize(options) { initializations.push(options); },
    render(id, source) {
      const init = initializations.at(-1);
      renderCalls.push({id, source, theme: init.theme, variables: init.themeVariables || null, security: init.securityLevel});
      if (source === "graph A") {
        return new Promise(resolve => { oldRenderResolve = () => resolve({svg: '<svg data-render="A"></svg>'}); });
      }
      return Promise.resolve({svg: '<svg data-render="' + source.replace(/[^A-Z]/g, '') + '"></svg>'});
    },
  };
  const win = {
    document, innerWidth: 1280, innerHeight: 800,
    navigator: {}, location: {href: "http://test/"}, visualViewport: null,
    __OKF_RENDERER_TEST_HOOKS__: { loadModule: () => moduleGate.promise },
    addEventListener(type, fn) { (windowListeners[type] ||= []).push(fn); },
    dispatchEvent(event) { (windowListeners[event.type] || []).slice().forEach(fn => fn(event)); },
    setTimeout, clearTimeout, requestAnimationFrame: fn => setTimeout(fn, 0),
    cancelAnimationFrame: clearTimeout,
    getComputedStyle: tokens
      ? () => ({display: "block", visibility: "visible", opacity: "1", getPropertyValue: name => tokens[root.getAttribute("data-theme")][name] || ""})
      : () => ({display: "block", visibility: "visible", opacity: "1"}),
    CustomEvent: function(name, init) { this.type = name; this.detail = (init || {}).detail; },
  };
  win.window = win;
  const context = {
    ...win, window: win, document, MutationObserver: function() { this.observe = () => {}; this.disconnect = () => {}; },
    CustomEvent: win.CustomEvent, Promise, Array, Object, Error, Set, String, Number,
    console, setTimeout, clearTimeout, requestAnimationFrame: win.requestAnimationFrame,
    cancelAnimationFrame: win.cancelAnimationFrame,
  };
  return {context, win, document, root, body, nodes, moduleGate, mermaid, renderCalls, getOldRenderResolve: () => oldRenderResolve};
}

function tick() { return new Promise(resolve => setTimeout(resolve, 0)); }
async function settle() { await tick(); await tick(); }

async function raceProof() {
  const s = sandbox();
  const first = element("div", "graph A");
  const second = element("div", "graph B");
  first.classList.add("mermaid"); second.classList.add("mermaid");
  s.nodes.mermaid = [first];
  vm.runInNewContext(SOURCE, s.context, {filename: "renderers.js"});
  s.document.fire("DOMContentLoaded");
  await settle();
  if (!s.body.dataset.okfRenderersReady || s.body.dataset.okfRenderersReady !== "loading") throw new Error("initial renderer did not publish loading");
  s.moduleGate.resolve({default: s.mermaid});
  await settle();
  if (s.renderCalls.length !== 1 || s.renderCalls[0].source !== "graph A") throw new Error("initial Mermaid render did not start");

  // Body generation 2 wins while generation 1's render promise is still held.
  s.nodes.mermaid = [second];
  s.win.dispatchEvent(new s.win.CustomEvent("okf-loom:bodyPatched"));
  await settle();
  const afterBody = s.win.okfRendererReadiness;
  if (afterBody.status !== "ready" || afterBody.bodyGeneration !== 1) throw new Error("body generation did not settle ready");
  if (!second.querySelector("svg") || second.innerHTML.indexOf('data-render="B"') < 0) throw new Error("new body did not win");
  const winningGeneration = afterBody.generation;
  if (!s.getOldRenderResolve()) throw new Error("old render was not held");
  s.getOldRenderResolve();
  await settle();
  if (s.win.okfRendererReadiness.generation !== winningGeneration) throw new Error("late renderer changed readiness");
  if (second.innerHTML.indexOf('data-render="B"') < 0) throw new Error("late old render changed the new body");

  // Bone -> Slate (light -> dark) re-renders once, in place. This
  // VM host exposes no CSS tokens, so Mermaid falls back to its own scheme
  // theme; the security level is strict on every render.
  s.root.setAttribute("data-theme", "slate");
  s.win.dispatchEvent(new s.win.CustomEvent("okf-loom:themeChanged", {detail: {theme: "slate", previous: "bone", scheme: "dark"}}));
  await settle();
  const afterDark = s.renderCalls.length;
  if (afterDark !== 3 || s.renderCalls.at(-1).theme !== "dark") throw new Error("dark palette did not rerender Mermaid: " + JSON.stringify({afterDark, calls: s.renderCalls, state: s.win.okfRendererReadiness}));
  // Slate -> Clay keeps the dark scheme but changes every colour: Ledger
  // re-renders on EVERY palette change, not only on a scheme change.
  s.root.setAttribute("data-theme", "clay");
  s.win.dispatchEvent(new s.win.CustomEvent("okf-loom:themeChanged", {detail: {theme: "clay", previous: "slate", scheme: "dark"}}));
  await settle();
  const afterClay = s.renderCalls.length;
  if (afterClay !== afterDark + 1) throw new Error("same-scheme palette change did not rerender Mermaid: " + JSON.stringify(s.renderCalls));
  // A repeated event for the palette already painted fences a new cycle but
  // does not re-render the diagram.
  s.win.dispatchEvent(new s.win.CustomEvent("okf-loom:themeChanged", {detail: {theme: "clay", previous: "clay", scheme: "dark"}}));
  await settle();
  if (s.renderCalls.length !== afterClay) throw new Error("repeated palette event rerendered Mermaid");
  if (s.renderCalls.some(call => call.security !== "strict")) throw new Error("Mermaid security level changed");
  return {status: s.win.okfRendererReadiness.status, generation: s.win.okfRendererReadiness.generation, bodyGeneration: s.win.okfRendererReadiness.bodyGeneration, themeGeneration: s.win.okfRendererReadiness.themeGeneration, themeName: s.win.okfRendererReadiness.themeName, renderCalls: s.renderCalls.length};
}

async function failureProof() {
  const s = sandbox();
  const diagram = element("div", "blocked diagram"); diagram.classList.add("mermaid"); s.nodes.mermaid = [diagram];
  s.win.__OKF_RENDERER_TEST_HOOKS__.loadModule = () => Promise.reject(new Error("cdn blocked"));
  vm.runInNewContext(SOURCE, s.context, {filename: "renderers.js"});
  s.document.fire("DOMContentLoaded");
  await settle();
  if (s.win.okfRendererReadiness.status !== "failed" || s.win.okfRendererReadiness.code !== "blocked-script") throw new Error("blocked renderer did not publish typed failure");
  if (!diagram.classList.contains("mermaid--fallback")) throw new Error("blocked Mermaid did not retain truthful fallback");
  return {status: s.win.okfRendererReadiness.status, code: s.win.okfRendererReadiness.code};
}

const TOKEN_NAMES = ["--bg", "--bg-2", "--surface", "--surface-2", "--border", "--border-strong",
  "--text", "--text-2", "--graph-edge", "--status-error"];
function palette(seed) {
  const out = {"--font-body": '"Public Sans", sans-serif'};
  TOKEN_NAMES.forEach((name, i) => { out[name] = "#" + (seed + i).toString(16).padStart(6, "0"); });
  return out;
}

async function tokenProof() {
  // Real hosts expose the palette tokens: Mermaid gets the "base" theme with
  // variables read from the ACTIVE palette, re-derived after a palette change.
  const tokens = {bone: palette(0x101010), plum: palette(0x202020), broken: {...palette(0x303030), "--surface-2": "var(--missing)"}};
  const s = sandbox(tokens);
  const diagram = element("div", "graph T"); diagram.classList.add("mermaid"); s.nodes.mermaid = [diagram];
  vm.runInNewContext(SOURCE, s.context, {filename: "renderers.js"});
  s.document.fire("DOMContentLoaded");
  s.moduleGate.resolve({default: s.mermaid});
  await settle();
  const bone = s.renderCalls.at(-1);
  s.root.setAttribute("data-theme", "plum");
  s.win.dispatchEvent(new s.win.CustomEvent("okf-loom:themeChanged", {detail: {theme: "plum", previous: "bone", scheme: "dark"}}));
  await settle();
  const plum = s.renderCalls.at(-1);
  s.root.setAttribute("data-theme", "broken");
  s.win.dispatchEvent(new s.win.CustomEvent("okf-loom:themeChanged", {detail: {theme: "broken", previous: "plum", scheme: "light"}}));
  await settle();
  const broken = s.renderCalls.at(-1);
  return {
    calls: s.renderCalls.length,
    bone: {theme: bone.theme, primary: bone.variables.primaryColor, line: bone.variables.lineColor, dark: bone.variables.darkMode, font: bone.variables.fontFamily, security: bone.security},
    plum: {theme: plum.theme, primary: plum.variables.primaryColor, background: plum.variables.background, dark: plum.variables.darkMode},
    broken: {theme: broken.theme, variables: broken.variables},
    expected: {bone: tokens.bone["--surface-2"], boneLine: tokens.bone["--graph-edge"], plum: tokens.plum["--surface-2"], plumBg: tokens.plum["--bg"]},
  };
}

async function swapProof() {
  // A palette re-render never puts the source text back: the old SVG stays
  // (aria-busy) until the new one replaces it in one write. Renders are held
  // and released out of order to prove latest-wins and failure retention.
  const s = sandbox();
  const el = element("div", "graph S"); el.classList.add("mermaid"); s.nodes.mermaid = [el];
  let sourceWrites = 0;
  const textDesc = Object.getOwnPropertyDescriptor(el, "textContent");
  Object.defineProperty(el, "textContent", {get: textDesc.get, set(v) { sourceWrites += 1; textDesc.set(v); }});
  const held = [];
  let serial = 0;
  let holding = false;
  const render = s.mermaid.render.bind(s.mermaid);
  s.mermaid.render = (id, source, host) => {
    render(id, source);
    const theme = s.renderCalls.at(-1).theme;
    const svg = '<svg data-n="' + (++serial) + '" data-theme="' + theme + '"></svg>';
    if (!holding) return Promise.resolve({svg});
    return new Promise((resolve, reject) => held.push({resolve: () => resolve({svg}), reject, svg, hostInBody: !!host && s.body.children.includes(host)}));
  };
  vm.runInNewContext(SOURCE, s.context, {filename: "renderers.js"});
  s.document.fire("DOMContentLoaded");
  s.moduleGate.resolve({default: s.mermaid});
  await settle();
  const ready = () => s.win.okfRendererReadiness;
  const check = (cond, msg) => { if (!cond) throw new Error(msg + " " + JSON.stringify({html: el.innerHTML, busy: el.getAttribute("aria-busy"), stamp: el.getAttribute("data-okf-mermaid-theme"), readiness: ready()})); };
  const theme = (name) => { const prev = s.root.getAttribute("data-theme"); s.root.setAttribute("data-theme", name); s.win.dispatchEvent(new s.win.CustomEvent("okf-loom:themeChanged", {detail: {theme: name, previous: prev}})); };
  check(el.innerHTML.includes('data-n="1"') && el.getAttribute("data-okf-mermaid-theme") === "bone" && ready().status === "ready", "first render did not stamp its palette");
  holding = true;

  // Bone -> Slate: the Bone SVG stays visible while Slate renders.
  theme("slate"); await settle();
  check(held.length === 1 && held[0].hostInBody, "slate render was not started in a hidden host");
  check(el.innerHTML.includes('data-n="1"') && el.getAttribute("aria-busy") === "true" && ready().status === "loading", "old diagram was not kept busy while re-rendering");
  // Slate -> Plum before Slate finished; Plum finishes first, Slate late.
  theme("plum"); await settle();
  check(held.length === 2, "plum render did not start");
  held[1].resolve(); await settle();
  check(el.innerHTML === held[1].svg && el.getAttribute("data-okf-mermaid-theme") === "plum" && !el.hasAttribute("aria-busy") && ready().status === "ready" && ready().themeName === "plum", "newest render did not swap in");
  held[0].resolve(); await settle();
  check(el.innerHTML === held[1].svg && ready().themeName === "plum", "stale render replaced a newer diagram");

  // A failed re-render keeps the previous diagram, reports failure, and is
  // retried by the next cycle (here a body patch reusing the element).
  theme("bone"); await settle();
  held[2].reject(new Error("parse exploded")); await settle();
  check(el.innerHTML === held[1].svg && !el.classList.contains("mermaid--fallback") && !el.hasAttribute("aria-busy") && el.getAttribute("data-okf-mermaid-theme") === "plum", "failed re-render did not keep the old diagram");
  check(ready().status === "failed" && ready().themeName === "bone", "failed re-render did not publish failure");
  s.win.dispatchEvent(new s.win.CustomEvent("okf-loom:bodyPatched")); await settle();
  check(held.length === 4, "stale diagram was not retried by the next cycle");
  held[3].resolve(); await settle();
  check(el.innerHTML === held[3].svg && el.getAttribute("data-okf-mermaid-theme") === "bone" && ready().status === "ready", "retry did not swap in");

  // Bone -> Slate -> Bone before Slate finishes: Bone is already painted.
  const callsBefore = s.renderCalls.length;
  theme("slate"); await settle();
  theme("bone"); await settle();
  check(s.renderCalls.length === callsBefore + 1 && !el.hasAttribute("aria-busy") && ready().status === "ready" && ready().themeName === "bone", "returning to the painted palette did not settle");
  held[4].resolve(); await settle();
  check(el.innerHTML === held[3].svg, "fenced slate render replaced the bone diagram");
  check(sourceWrites === 0, "source text was written back into a rendered diagram");
  check(!s.body.children.some(n => n.hasAttribute && n.hasAttribute("data-okf-mermaid-render-host")), "render hosts leaked");
  return {renders: serial, final: el.getAttribute("data-okf-mermaid-theme"), status: ready().status};
}

(async () => {
  const race = await raceProof();
  const failure = await failureProof();
  const tokens = await tokenProof();
  const swap = await swapProof();
  console.log(JSON.stringify({race, failure, tokens, swap}));
})().catch(error => { console.error(error.stack || error); process.exit(1); });
"""


def test_renderer_generation_fence_and_theme_mode_policy() -> None:
    result = _run_node(_RENDERER_HARNESS.replace("__SOURCE__", json.dumps(RENDERERS)))
    assert result["race"]["status"] == "ready"
    assert result["race"]["bodyGeneration"] == 1
    assert result["race"]["themeName"] == "clay"
    assert result["race"]["renderCalls"] == 4
    assert result["failure"] == {"status": "failed", "code": "blocked-script"}
    tokens = result["tokens"]
    assert tokens["calls"] == 3, tokens
    assert tokens["bone"] == {
        "theme": "base", "primary": tokens["expected"]["bone"],
        "line": tokens["expected"]["boneLine"], "dark": False,
        "font": '"Public Sans", sans-serif', "security": "strict",
    }
    assert tokens["plum"] == {
        "theme": "base", "primary": tokens["expected"]["plum"],
        "background": tokens["expected"]["plumBg"], "dark": True,
    }
    # An unresolved token never reaches Mermaid: its own scheme theme is used.
    assert tokens["broken"] == {"theme": "default", "variables": None}
    assert result["swap"] == {"renders": 6, "final": "bone", "status": "ready"}


_CAPTURE_HARNESS = r"""
const vm = require("vm");
const SOURCE = __SOURCE__;

function classes(initial) {
  const values = new Set(initial || []);
  return { add: n => values.add(n), remove: n => values.delete(n), contains: n => values.has(n) };
}
function makeCase(kind) {
  const root = {dataset: {theme: "bone"}, attrs: {"data-theme": "bone"}, classList: classes(["okf-theme-ready"]), hasAttribute(n) { return this.attrs[n] !== undefined; }, getAttribute(n) { return this.attrs[n] || null; }};
  const target = {hidden: false, matches: () => false, querySelector: () => null, getBoundingClientRect: () => ({width: 20, height: 20})};
  const body = {dataset: {okfCaptureReady: "ready"}, classList: classes(["okf-viewer"]), hidden: false, matches: () => false, querySelector: () => null, getBoundingClientRect: () => ({width: 20, height: 20})};
  const listeners = {};
  const scripts = [];
  const document = {
    body, documentElement: root,
    querySelector: selector => selector === "#capture" ? target : (selector.indexOf("renderers.js") >= 0 ? scripts.find(s => s.src.indexOf("renderers.js") >= 0) || null : (selector.indexOf("studio.js") >= 0 ? scripts.find(s => s.src.indexOf("studio.js") >= 0) || null : (selector.indexOf("theme.js") >= 0 ? scripts.find(s => s.src.indexOf("theme.js") >= 0) || null : null))),
    querySelectorAll: selector => selector === "script" ? scripts : [],
    getElementById: id => id === "capture" ? target : (kind === "boot-failure" ? {} : null),
  };
  const rafs = [];
  const win = {
    document, location: {href: "http://test/"}, innerWidth: 1280, innerHeight: 800,
    devicePixelRatio: 1, visualViewport: null, addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    dispatchEvent(event) { (listeners[event.type] || []).slice().forEach(fn => fn(event)); },
    requestAnimationFrame(fn) { rafs.push(fn); return rafs.length; }, cancelAnimationFrame() {},
    getComputedStyle: () => ({display: "block", visibility: "visible", opacity: "1"}),
    CustomEvent: function(name, init) { this.type = name; this.detail = (init || {}).detail; },
    okRendererReadiness: {status: "ready", generation: 2},
    okfStudioReadiness: {status: "ready", generation: 1},
    okfLoomStudio: {openPanel() {}, setView() {}},
    OKFLoomTheme: {getResolvedTheme: () => "bone"},
    pump() { const pending = rafs.splice(0); pending.forEach(fn => fn()); },
  };
  if (kind === "delayed-renderer") win.okRendererReadiness = {status: "loading", generation: 1};
  if (kind === "boot-failure") {
    win.okfStudioReadiness = {status: "failed", code: "boot-failure", generation: 1};
    win.okfLoomStudio = null;
  }
  if (kind === "blocked-script") {
    const script = {tagName: "SCRIPT", src: "/__static/studio.js", listeners: {}, getAttribute(n) { return n === "src" ? this.src : null; }, addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }};
    scripts.push(script);
    win.blockedScript = script;
  }
  const context = { ...win, window: win, document, Promise, Array, Object, Error, Number, String, console, setTimeout, clearTimeout, queueMicrotask, requestAnimationFrame: win.requestAnimationFrame, cancelAnimationFrame: win.cancelAnimationFrame, getComputedStyle: win.getComputedStyle, CustomEvent: win.CustomEvent };
  return {context, win, body, root, target};
}
function tick() { return new Promise(resolve => setTimeout(resolve, 0)); }
async function success(kind) {
  const s = makeCase(kind);
  vm.runInNewContext(SOURCE, s.context, {filename: "capture-readiness.js"});
  if (kind === "blocked-script") s.win.dispatchEvent({type: "error", target: s.win.blockedScript});
  const run = s.win.okfCaptureReadiness.start({target: "#capture"});
  const outcome = run.promise.then(result => ({ok: true, phase: result.metadata.readinessPhase}), error => ({ok: false, code: error.code, screenshotAllowed: error.screenshotAllowed, phase: error.metadata.readinessPhase}));
  await tick();
  if (kind === "delayed-renderer") {
    s.win.okRendererReadiness = {status: "ready", generation: 2};
    s.win.pump();
  }
  return await outcome;
}
(async () => {
  const delayed = await success("delayed-renderer");
  const blocked = await success("blocked-script");
  const boot = await success("boot-failure");
  if (!delayed.ok || blocked.code !== "blocked-script" || boot.code !== "boot-failure") throw new Error(JSON.stringify({delayed, blocked, boot}));
  console.log(JSON.stringify({delayed, blocked, boot}));
})().catch(error => { console.error(error.stack || error); process.exit(1); });
"""


def test_capture_waits_for_terminal_renderer_and_classifies_failures() -> None:
    result = _run_node(_CAPTURE_HARNESS.replace("__SOURCE__", json.dumps(CAPTURE)))
    assert result["delayed"]["ok"] is True
    assert result["blocked"] == {"ok": False, "code": "blocked-script", "screenshotAllowed": False, "phase": "studio-script"}
    assert result["boot"] == {"ok": False, "code": "boot-failure", "screenshotAllowed": False, "phase": "studio-boot-failure"}


# ---------------------------------------------------------------------------
# Real served-page proof for Studio's terminal status and fallback ownership.
# ---------------------------------------------------------------------------
from browser_support import expect, sync_playwright, launch_chromium, copy_bundle, live_server


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(proc: subprocess.Popen, base: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"okf serve exited before browser proof (rc={proc.returncode})")
        try:
            with urllib.request.urlopen(base + "/", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.1)
    pytest.fail("okf serve did not become ready for lifecycle browser proof")


@pytest.fixture(scope="module")
def lifecycle_server(tmp_path_factory):
    bundle = copy_bundle(ROOT / "samples/demo_bundle", tmp_path_factory.mktemp("lifecycle") / "bundle")
    with live_server(bundle) as base:
        yield base


@pytest.fixture
def lifecycle_page():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context()
        page = context.new_page()
        try:
            yield page
        finally:
            context.close(); browser.close()


@pytest.mark.browser
def test_mermaid_shared_tooltip_uses_square_viewer_geometry(lifecycle_server, lifecycle_page):
    lifecycle_page.goto(lifecycle_server + '/playbooks/refund_flow')
    lifecycle_page.wait_for_function("() => window.okfRendererReadiness && window.okfRendererReadiness.ready")
    expect(lifecycle_page.locator('div.mermaid svg')).to_have_count(1)
    tooltip = lifecycle_page.locator('body > div.mermaidTooltip')
    expect(tooltip).to_have_count(1)
    assert tooltip.evaluate("t => getComputedStyle(t).borderRadius") == '0px'
    assert tooltip.evaluate("t => t.style.borderRadius") == ''


@pytest.mark.browser
def test_mermaid_holds_a_legible_scale_and_scrolls_in_its_port(lifecycle_server, lifecycle_page):
    """M1-006/D1-019: a diagram wider than its column is never shrunk below
    ~12px labels (12/14 of Mermaid's 14px); it keeps that scale and scrolls
    inside its named diagram scroll port, without page overflow."""
    page = lifecycle_page
    page.set_viewport_size({"width": 320, "height": 740})
    page.goto(lifecycle_server + '/playbooks/refund_flow')
    page.wait_for_function("() => window.okfRendererReadiness && window.okfRendererReadiness.ready")
    expect(page.locator('div.mermaid svg')).to_have_count(1)
    page.wait_for_function("""() => { const p = document.querySelector('div.mermaid').closest('.okf-content-scrollport');
      return p && p.getAttribute('role') === 'region'; }""")
    fit = page.evaluate("""() => {
      const svg = document.querySelector('div.mermaid svg'), port = svg.closest('.okf-content-scrollport');
      return {scale: svg.getBoundingClientRect().width / svg.viewBox.baseVal.width,
              wide: svg.viewBox.baseVal.width * 12 / 14 > port.clientWidth,
              scrolls: port.scrollWidth > port.clientWidth + 1, tab: port.tabIndex,
              label: port.getAttribute('aria-label'),
              overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
    }""")
    assert fit["wide"], f"fixture diagram must be wider than the 320px column: {fit}"
    assert fit["scale"] >= 12 / 14 - 0.01, fit
    assert fit["scrolls"] and fit["tab"] == 0 and fit["label"].startswith("Diagram"), fit
    assert fit["overflow"] <= 0, fit


_MERMAID_SWAP_PROBE = """() => {
  // Every mutation (MutationObserver, before paint) and every animation frame
  // must show a drawn SVG, no raw source text and the same container box.
  const el = document.querySelector('div.mermaid');
  const r0 = el.getBoundingClientRect();
  const p = {violations: [], frames: 0, mutations: 0, svgIds: [], stop: false};
  const inspect = (where) => {
    const svg = el.querySelector(':scope > svg');
    const text = Array.from(el.childNodes).filter(n => n.nodeType === 3)
      .map(n => n.textContent).join('').trim();
    if (!svg) p.violations.push({where, kind: 'no-svg', text: text.slice(0, 40)});
    if (text) p.violations.push({where, kind: 'source-text', text: text.slice(0, 40)});
    const r = el.getBoundingClientRect();
    if (Math.abs(r.width - r0.width) > 0.5 || Math.abs(r.height - r0.height) > 0.5 ||
        Math.abs(r.top - r0.top) > 0.5 || Math.abs(r.left - r0.left) > 0.5) {
      p.violations.push({where, kind: 'shift', rect: [r.left, r.top, r.width, r.height],
        initial: [r0.left, r0.top, r0.width, r0.height]});
    }
    if (svg && p.svgIds[p.svgIds.length - 1] !== svg.id) p.svgIds.push(svg.id);
  };
  p.observer = new MutationObserver(() => { p.mutations += 1; inspect('mutation'); });
  p.observer.observe(el, {childList: true, subtree: true, characterData: true});
  const loop = () => { p.frames += 1; inspect('frame'); if (!p.stop) requestAnimationFrame(loop); };
  requestAnimationFrame(loop);
  window.__okfMermaidProbe = p;
}"""

_MERMAID_PALETTE_STATE = """() => {
  const el = document.querySelector('div.mermaid');
  const rgb = (value) => {
    const probe = document.createElement('span');
    probe.style.color = value; document.body.appendChild(probe);
    const out = getComputedStyle(probe).color; probe.remove(); return out;
  };
  const rect = el.querySelector('svg .node rect, svg .node polygon');
  const root = getComputedStyle(document.documentElement);
  return {
    theme: document.documentElement.getAttribute('data-theme'),
    painted: el.getAttribute('data-okf-mermaid-theme'),
    busy: el.getAttribute('aria-busy'),
    nodeFill: rect ? getComputedStyle(rect).fill : null,
    surface2: rgb(root.getPropertyValue('--surface-2').trim()),
    readiness: window.okfRendererReadiness && window.okfRendererReadiness.status,
    readinessTheme: window.okfRendererReadiness && window.okfRendererReadiness.themeName,
    hosts: document.querySelectorAll('[data-okf-mermaid-render-host]').length,
  };
}"""


@pytest.mark.browser
def test_mermaid_palette_switch_swaps_without_showing_source(lifecycle_server, lifecycle_page):
    """Bone -> Slate -> Bone (and back to Slate) in quick succession: the old
    diagram stays until the new one is drawn, no frame shows the raw source,
    the box never moves, the latest palette wins and capture waits for it."""
    page = lifecycle_page
    page.goto(lifecycle_server + '/playbooks/refund_flow')
    page.wait_for_function("() => window.okfRendererReadiness && window.okfRendererReadiness.ready")
    page.evaluate("() => window.OKFLoomTheme.setTheme('bone')")
    page.wait_for_function("() => window.okfRendererReadiness.ready && window.okfRendererReadiness.themeName === 'bone'"
                           " && document.querySelector('div.mermaid').getAttribute('data-okf-mermaid-theme') === 'bone'")
    expect(page.locator('div.mermaid > svg')).to_have_count(1)
    bone = page.evaluate(_MERMAID_PALETTE_STATE)
    page.evaluate(_MERMAID_SWAP_PROBE)

    settled = ("(name) => window.okfRendererReadiness.status === 'ready' && window.okfRendererReadiness.themeName === name"
               " && document.querySelector('div.mermaid').getAttribute('data-okf-mermaid-theme') === name"
               " && !document.querySelector('div.mermaid').hasAttribute('aria-busy')")
    # Bone -> Slate -> Bone, one frame apart.
    page.evaluate("""async () => {
      const frame = () => new Promise(r => requestAnimationFrame(r));
      window.OKFLoomTheme.setTheme('slate'); await frame();
      window.OKFLoomTheme.setTheme('bone');
    }""")
    page.wait_for_function(settled, arg='bone')
    page.wait_for_timeout(400)  # a fenced Slate render must not land late
    back_to_bone = page.evaluate(_MERMAID_PALETTE_STATE)

    # Slate -> Bone -> Slate; capture readiness started mid-flight must only
    # resolve on the final Slate diagram.
    captured = page.evaluate("""async () => {
      const frame = () => new Promise(r => requestAnimationFrame(r));
      const T = window.OKFLoomTheme;
      T.setTheme('slate'); await frame();
      T.setTheme('bone'); await frame();
      T.setTheme('slate');
      await window.okfCaptureReadiness.start({target: 'document'}).promise;
      const el = document.querySelector('div.mermaid');
      return {painted: el.getAttribute('data-okf-mermaid-theme'), busy: el.getAttribute('aria-busy')};
    }""")
    page.wait_for_function(settled, arg='slate')
    page.wait_for_timeout(400)
    slate = page.evaluate(_MERMAID_PALETTE_STATE)
    probe = page.evaluate("""() => { const p = window.__okfMermaidProbe; p.stop = true; p.observer.disconnect();
      return {violations: p.violations.slice(0, 5), count: p.violations.length, frames: p.frames,
              mutations: p.mutations, svgIds: p.svgIds}; }""")

    assert probe["count"] == 0, probe
    assert probe["frames"] > 5 and probe["mutations"] > 0, probe
    assert len(probe["svgIds"]) >= 2, probe  # a real swap happened
    assert captured == {"painted": "slate", "busy": None}, captured
    assert bone["nodeFill"] == bone["surface2"], bone
    assert back_to_bone["painted"] == "bone" and back_to_bone["nodeFill"] == bone["surface2"], back_to_bone
    assert slate["theme"] == "slate" and slate["painted"] == "slate", slate
    assert slate["nodeFill"] == slate["surface2"] != bone["surface2"], slate
    assert slate["readiness"] == "ready" and slate["readinessTheme"] == "slate", slate
    assert slate["hosts"] == 0 and slate["busy"] is None, slate


@pytest.mark.browser
def test_studio_ready_status_follows_usable_api(lifecycle_server: str, lifecycle_page) -> None:
    lifecycle_page.goto(lifecycle_server + "/tables/orders", wait_until="domcontentloaded")
    lifecycle_page.wait_for_function(
        "() => window.okfStudioReadiness && window.okfStudioReadiness.status === 'ready'",
        timeout=10000,
    )
    state = lifecycle_page.evaluate("""() => {
      const fallback = document.querySelector('.okf-studio-fallback-banner');
      return {
        status: window.okfStudioReadiness.status,
        ready: window.okfStudioReadiness.ready,
        api: !!window.okfLoomStudio && typeof window.okfLoomStudio.openPanel === 'function',
        classReady: document.documentElement.classList.contains('okf-studio-booted'),
        fallbackHidden: !fallback || getComputedStyle(fallback).display === 'none',
      };
    }""")
    assert state == {"status": "ready", "ready": True, "api": True, "classReady": True, "fallbackHidden": True}


@pytest.mark.browser
def test_studio_boot_failure_restores_fallback_and_capture_rejects(lifecycle_server: str, lifecycle_page) -> None:
    lifecycle_page.add_init_script("""(() => {
      window.__OKF_LOOM_TEST__ = true;
      window.__OKF_LOOM_STUDIO_TEST_FAILURE__ = 'boot';
      // Keep this proof independent of CDN availability. The production
      // renderer still owns the lifecycle; these hooks only supply a local
      // deterministic resource for this test.
      window.__OKF_RENDERER_TEST_HOOKS__ = {
        loadCSS: () => Promise.resolve({href: '', media: 'not all'}),
        loadModule: (url) => url.includes('highlight')
          ? Promise.resolve({default: {highlightElement() {}}})
          : Promise.resolve({default: {initialize() {}, render() { return {svg: '<svg></svg>'}; }}}),
      };
    })();""")
    lifecycle_page.goto(lifecycle_server + "/tables/orders", wait_until="domcontentloaded")
    lifecycle_page.wait_for_function(
        "() => window.okfStudioReadiness && window.okfStudioReadiness.status === 'failed'",
        timeout=10000,
    )
    state = lifecycle_page.evaluate("""async () => {
      const fallback = document.querySelector('.okf-live-studio-fallback');
      let capture;
      try {
        await window.okfCaptureReadiness.start({target: 'document'}).promise;
        capture = {ok: true};
      } catch (error) {
        capture = {ok: false, code: error.code, screenshotAllowed: error.screenshotAllowed};
      }
      return {
        status: window.okfStudioReadiness.status,
        code: window.okfStudioReadiness.code,
        api: !!window.okfLoomStudio,
        classReady: document.documentElement.classList.contains('okf-studio-booted'),
        fallbackVisible: !!fallback && getComputedStyle(fallback).display !== 'none',
        studioBarHidden: !document.querySelector('.okf-studio-bar') ||
          getComputedStyle(document.querySelector('.okf-studio-bar')).display === 'none',
        capture,
      };
    }""")
    assert state == {
        "status": "failed", "code": "boot-failure", "api": False,
        "classReady": False, "fallbackVisible": True, "studioBarHidden": True,
        "capture": {"ok": False, "code": "boot-failure", "screenshotAllowed": False},
    }
    assert lifecycle_page.locator(".okf-live-studio-fallback").is_visible()
    assert lifecycle_page.locator(".okf-nojs-fallback:visible").count() == 0
    assert lifecycle_page.locator(".okf-studio-fallback-banner:visible").count() == 1
