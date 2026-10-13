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

  // Bone -> Slate (light -> dark) restores the source and renders once. This
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

(async () => {
  const race = await raceProof();
  const failure = await failureProof();
  const tokens = await tokenProof();
  console.log(JSON.stringify({race, failure, tokens}));
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


_CAPTURE_HARNESS = r"""
const vm = require("vm");
const SOURCE = __SOURCE__;

function classes(initial) {
  const values = new Set(initial || []);
  return { add: n => values.add(n), remove: n => values.delete(n), contains: n => values.has(n) };
}
function makeCase(kind) {
  const root = {dataset: {theme: "technical-light"}, attrs: {"data-theme": "technical-light"}, classList: classes(["okf-theme-ready"]), hasAttribute(n) { return this.attrs[n] !== undefined; }, getAttribute(n) { return this.attrs[n] || null; }};
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
    OKFLoomTheme: {getResolvedTheme: () => "technical-light"},
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
