"""Behavioural unit tests for viewer/static/theme.js.

theme.js is browser code, but its core contract — validated persistence,
one-time legacy migration, Auto/OS resolution, storage-denial safety, and
the single-writer invariant — is pure logic over a small browser surface
(localStorage, document.documentElement, matchMedia, CustomEvent). These
tests run theme.js inside a Node `vm` sandbox with stubbed browser globals
(NO browser, NO jsdom dependency), one fresh sandbox per scenario so the
module's boot-time closure state is isolated.

Requires Node (already required by tests/test_js_lint.py for `node --check`).
Skips cleanly when Node is unavailable.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parent.parent / "scripts" / "okf_loom" / "viewer" / "static"
_THEME_JS = (_STATIC / "theme.js").read_text(encoding="utf-8")

_NODE = pytest.importorskip  # placeholder; real gate below
shutil_which = __import__("shutil").which
if not shutil_which("node"):
    pytest.skip("node not available", allow_module_level=True)


# The Node harness: a makeSandbox(opts) factory + a run() that evaluates
# theme.js in a fresh vm context and returns the public API + storage state.
_HARNESS = r"""
const vm = require("vm");
const THEME_JS = __THEME_JS__;

function makeSandbox(opts) {
  opts = opts || {};
  const storage = new Map();
  if (opts.storage) for (const k in opts.storage) storage.set(k, opts.storage[k]);
  const ls = {
    getItem: (k) => { if (opts.getItemFail) throw new Error("read-denied"); return storage.has(k) ? storage.get(k) : null; },
    setItem: (k, v) => { if (opts.deny) throw new Error("denied"); storage.set(k, String(v)); },
    removeItem: (k) => { if (opts.deny) throw new Error("denied"); storage.delete(k); },
    _dump: () => { const o = {}; storage.forEach((v, k) => { o[k] = v; }); return o; },
  };
  const html = {
    attrs: Object.assign({}, opts.htmlAttrs),
    getAttribute(n) { return this.attrs[n] !== undefined ? this.attrs[n] : null; },
    setAttribute(n, v) { this.attrs[n] = String(v); },
    classList: { add() {}, contains() { return false; }, remove() {} },
  };
  const events = [];
  // Optional studio bootstrap JSON (okf-studio-bootstrap): a string is taken
  // as the studio.theme value; an object is embedded verbatim.
  const bootEl = opts.bootstrap != null
    ? { textContent: JSON.stringify(typeof opts.bootstrap === "string" ? { theme: opts.bootstrap } : opts.bootstrap) }
    : null;
  const configEl = opts.themeConfig != null
    ? { textContent: JSON.stringify(typeof opts.themeConfig === "string" ? { theme: opts.themeConfig } : opts.themeConfig) }
    : null;
  const sandbox = {
    console,
    localStorage: ls,
    document: {
      documentElement: html,
      getElementById: (id) => id === "okf-studio-bootstrap" ? bootEl :
        (id === "okf-theme-config" ? configEl : null),
      addEventListener: () => {},
      readyState: "complete",
    },
    matchMedia: (q) => {
      // Return a STABLE singleton mq (theme.js caches one at boot AND
      // re-queries in osDark()), so flipping its `.matches` is visible to
      // every reader and a live change listener fires the captured callback.
      if (!sandbox.__mq) {
        sandbox.__mq = { matches: !!opts.osDark };
        sandbox.__mq.addEventListener = (type, cb) => { sandbox.__mqListener = cb; };
        sandbox.__mq.addListener = (cb) => { sandbox.__mqListener = cb; };
      }
      return sandbox.__mq;
    },
    visualViewport: null,
    innerWidth: 1280, innerHeight: 800,
    setTimeout, clearTimeout, requestAnimationFrame: (fn) => { return 0; },
  };
  // Drive a live prefers-color-scheme change: flip the mq matches flag AND
  // fire the captured change listener, exactly as the browser would.
  sandbox.__flipOs = (dark) => {
    if (!sandbox.__mq) return;
    sandbox.__mq.matches = !!dark;
    if (sandbox.__mqListener) {
      try { sandbox.__mqListener({ matches: !!dark, type: "change" }); } catch (e) {}
    }
  };
  sandbox.window = sandbox;
  sandbox.CustomEvent = function (name, init) { this.type = name; this.detail = (init || {}).detail; };
  sandbox.dispatchEvent = (e) => { events.push(e); };
  sandbox.window.dispatchEvent = sandbox.dispatchEvent;
  sandbox.__events = events;
  sandbox.__storage = ls;
  return sandbox;
}

function snap(api, sb) {
  return {
    api: {
      family: api.getFamily(), mode: api.getMode(),
      resolved: api.getResolvedTheme(), resolvedMode: api.getResolvedMode(),
      contrast: api.getContrast(), border: api.getBorder(), density: api.getDensity(),
    },
    dataTheme: sb.document.documentElement.getAttribute("data-theme"),
    dataContrast: sb.document.documentElement.getAttribute("data-okf-contrast"),
    dataBorder: sb.document.documentElement.getAttribute("data-okf-border"),
    dataDensity: sb.document.documentElement.getAttribute("data-okf-density"),
    storage: sb.__storage._dump(),
  };
}

function run(opts) {
  const sb = makeSandbox(opts);
  vm.runInNewContext(THEME_JS, sb, { filename: "theme.js" });
  const api = sb.OKFLoomTheme;
  const out = {
    api: api ? snap(api, sb).api : null,
    dataTheme: sb.document.documentElement.getAttribute("data-theme"),
    dataContrast: sb.document.documentElement.getAttribute("data-okf-contrast"),
    dataBorder: sb.document.documentElement.getAttribute("data-okf-border"),
    dataDensity: sb.document.documentElement.getAttribute("data-okf-density"),
    storage: sb.__storage._dump(),
    events: sb.__events.map((e) => e.detail),
  };
  if (api && Array.isArray(opts.actions) && opts.actions.length) {
    opts.actions.forEach((a) => {
      // Setters must never throw on bad input; guard anyway so a regression
      // surfaces as a failed assertion instead of aborting the harness.
      try { api[a.fn].apply(api, a.args || []); } catch (e) {}
    });
    out.after = snap(api, sb);
    out.events = sb.__events.map((e) => e.detail);
  }
  // LIVE OS-colour-scheme change after boot (P1 outcome-audit): drive each
  // flip and snapshot. theme.js must follow the OS ONLY while the effective
  // mode is Auto; an explicit light/dark preference must NOT flip.
  if (api && Array.isArray(opts.osFlips)) {
    out.osFlips = opts.osFlips.map((dark) => {
      sb.__flipOs(dark);
      return snap(api, sb);
    });
  }
  return out;
}

const out = [];
// 1. Fresh boot, OS light → Technical-light (the V2 default look).
out.push({ name: "fresh-os-light", ...run({ osDark: false }) });
// 2. Fresh boot, OS dark + Auto → Technical-dark (Auto follows OS live).
out.push({ name: "fresh-os-dark", ...run({ osDark: true }) });
// 2b. Every optional family resolves both explicit modes without changing
// the orthogonal Auto model.
out.push({ name: "purple-light", ...run({ osDark: true, actions: [
  { fn: "setFamily", args: ["purple"] },
  { fn: "setMode", args: ["light"] },
] }) });
out.push({ name: "purple-dark", ...run({ osDark: false, actions: [
  { fn: "setFamily", args: ["purple"] },
  { fn: "setMode", args: ["dark"] },
] }) });
out.push({ name: "coffee-light", ...run({ osDark: true, actions: [
  { fn: "setFamily", args: ["coffee"] },
  { fn: "setMode", args: ["light"] },
] }) });
out.push({ name: "coffee-dark", ...run({ osDark: false, actions: [
  { fn: "setFamily", args: ["coffee"] },
  { fn: "setMode", args: ["dark"] },
] }) });
out.push({ name: "purple-auto-light", ...run({ osDark: false, actions: [
  { fn: "setFamily", args: ["purple"] },
  { fn: "setMode", args: ["auto"] },
] }) });
out.push({ name: "purple-auto-dark", ...run({ osDark: true, actions: [
  { fn: "setFamily", args: ["purple"] },
  { fn: "setMode", args: ["auto"] },
] }) });
out.push({ name: "coffee-auto-light", ...run({ osDark: false, actions: [
  { fn: "setFamily", args: ["coffee"] },
  { fn: "setMode", args: ["auto"] },
] }) });
out.push({ name: "coffee-auto-dark", ...run({ osDark: true, actions: [
  { fn: "setFamily", args: ["coffee"] },
  { fn: "setMode", args: ["auto"] },
] }) });
// 3. Legacy migration: a stored legacy "dark" concrete choice migrates once
//    into canonical family+mode and resolves technical-dark.
out.push({ name: "legacy-dark", ...run({ storage: { "okf-theme": "dark" } }) });
// 4. Legacy retired names migrate to the documented family.
out.push({ name: "legacy-pastel", ...run({ storage: { "okf-theme": "pastel" } }) });
out.push({ name: "legacy-midnight", ...run({ storage: { "okf-theme": "midnight" } }) });
// 5. Canonical keys win over a stale legacy mirror: family=swiss, mode=auto
//    → swiss resolves by OS; the legacy mirror is REMOVED (auto never pins).
out.push({ name: "canonical-over-legacy-auto", ...run({
  storage: { "okf-theme-family": "swiss", "okf-theme-mode": "auto", "okf-theme": "technical-dark" },
}) });
// 6. Corrupt canonical values are ignored per-dimension (fall back) but the
//    raw key is RETAINED as the migration gate.
out.push({ name: "corrupt-family", ...run({
  storage: { "okf-theme-family": "bogus", "okf-theme-mode": "dark" }, osDark: true,
}) });
out.push({ name: "corrupt-mode", ...run({
  storage: { "okf-theme-family": "swiss", "okf-theme-mode": "midnight" }, osDark: false,
}) });
// 7. A concrete configured preference is carried by an explicit JSON config
//    block. A painted data-theme alone is never configuration authority.
out.push({ name: "configured-concrete", ...run({
  htmlAttrs: { "data-theme": "swiss-dark" }, themeConfig: "swiss-dark", osDark: false,
}) });
// 8. Storage denial: persistence throws, but resolution + data-theme still
//    work from module state; nothing throws.
out.push({ name: "storage-denial", ...run({ deny: true, osDark: true }) });
// 9. Precedence (P1-3): studio.theme via the served bootstrap JSON. "auto"
//    must seed a follow-OS runtime mode (NOT the painted concrete default),
//    and a concrete value seeds family+mode. The painted data-theme stays
//    the FOUC-safe first paint either way.
out.push({ name: "bootstrap-auto-oslight", ...run({
  htmlAttrs: { "data-theme": "technical-light" }, bootstrap: "auto", osDark: false,
}) });
out.push({ name: "bootstrap-auto-osdark", ...run({
  htmlAttrs: { "data-theme": "technical-light" }, bootstrap: "auto", osDark: true,
}) });
out.push({ name: "bootstrap-concrete", ...run({
  htmlAttrs: { "data-theme": "swiss-dark" }, bootstrap: "swiss-dark", osDark: false,
}) });
// 10. Precedence: a validated saved user preference beats studio.theme.
out.push({ name: "user-pref-beats-bootstrap", ...run({
  htmlAttrs: { "data-theme": "swiss-dark" }, bootstrap: "swiss-dark",
  storage: { "okf-theme-family": "technical", "okf-theme-mode": "light" }, osDark: true,
}) });
// 11. Precedence: an INVALID studio.theme ignores the painted concrete
//     data-theme (which is only a first-paint hint) and uses Technical Auto.
out.push({ name: "bootstrap-invalid-falls-to-painted", ...run({
  htmlAttrs: { "data-theme": "technical-dark" }, bootstrap: "garbage", osDark: false,
}) });
// 12. Storage hardening (P1-7): getItem throwing on every read must not break
//     resolution; theme falls back to defaults and sets data-theme.
out.push({ name: "getitem-fails", ...run({ getItemFail: true, osDark: true }) });
// 13. Storage hardening (P1-7): invalid setter input must NOT corrupt DOM or
//     storage — every dimension stays at its pre-call value.
out.push({ name: "invalid-setter-no-corrupt", ...run({ osDark: false, actions: [
  { fn: "setFamily", args: ["garbage"] },
  { fn: "setMode", args: ["midnight"] },
  { fn: "setContrast", args: ["high"] },
  { fn: "setBorder", args: ["maybe"] },
  { fn: "setTheme", args: ["nope"] },
] }) });
// 14. Storage hardening (P1-7): a write failure DURING a valid setter keeps
//     resolution working (session state) and leaves storage clean.
out.push({ name: "write-fails-on-setter", ...run({
  deny: true, osDark: false, actions: [ { fn: "setFamily", args: ["swiss"] } ],
}) });
// 15. Live OS change while Auto (P1 outcome-audit): fresh boot is Auto + OS
//     light (technical-light); flipping the OS to dark must flip the resolved
//     theme to technical-dark LIVE — Auto follows the OS, no reload.
out.push({ name: "auto-os-flip-to-dark", ...run({ osDark: false, osFlips: [true] }) });
// 16. Explicit mode does NOT flip on OS change: a pinned mode=light stays
//     technical-light even when the OS goes dark. Only Auto follows the OS.
out.push({ name: "explicit-light-no-flip-on-os-change", ...run({
  storage: { "okf-theme-mode": "light" }, osDark: false, osFlips: [true],
}) });
// 17. Modifiers are independent, persist their canonical values, and update
// root semantic attributes without changing the resolved family/mode.
out.push({ name: "modifiers-default-and-switch", ...run({
  osDark: false, actions: [
    { fn: "setContrast", args: ["soft"] },
    { fn: "setBorder", args: ["on"] },
  ],
}) });
// 18. Configured modifiers apply on every target but are never persisted at
// boot; a saved modifier preference wins over the configured value.
out.push({ name: "configured-modifiers", ...run({
  themeConfig: { theme: "auto", contrast: "soft", border: "on" }, osDark: true,
}) });
out.push({ name: "saved-modifiers-beat-config", ...run({
  themeConfig: { theme: "auto", contrast: "soft", border: "on" },
  storage: { "okf-theme-contrast": "normal", "okf-theme-border": "off" }, osDark: true,
}) });
// 19. Corrupt modifier values fall back per dimension and remain retained as
// the migration gate, just like corrupt family/mode values.
out.push({ name: "corrupt-modifiers", ...run({
  themeConfig: { theme: "auto", contrast: "soft", border: "on" },
  storage: { "okf-theme-contrast": "bogus", "okf-theme-border": "broken" }, osDark: false,
}) });
// 20. Persistence denial cannot prevent in-page modifier resolution.
out.push({ name: "modifier-write-denial", ...run({
  deny: true, osDark: false, actions: [
    { fn: "setContrast", args: ["soft"] },
    { fn: "setBorder", args: ["on"] },
  ],
}) });
// 21. Density is a validated, independent preference and updates the root.
out.push({ name: "density-default-and-switch", ...run({
  osDark: false, actions: [{ fn: "setDensity", args: ["comfortable"] }],
}) });
out.push({ name: "density-corrupt", ...run({
  storage: { "okf-theme-density": "tiny" }, osDark: false,
}) });
out.push({ name: "density-write-denial", ...run({
  deny: true, osDark: false, actions: [{ fn: "setDensity", args: ["comfortable"] }],
}) });
// 22. Reset clears every user-owned preference and returns to the configured
// Technical Auto/modifier defaults without persisting a concrete mode.
out.push({ name: "reset-preferences", ...run({
  themeConfig: { theme: "swiss-dark", contrast: "soft", border: "on" },
  storage: {
    "okf-theme-family": "technical", "okf-theme-mode": "light",
    "okf-theme-contrast": "normal", "okf-theme-border": "off",
    "okf-theme-density": "comfortable",
    "okf-theme": "technical-light",
  }, osDark: false, actions: [{ fn: "reset", args: [] }],
}) });
console.log(JSON.stringify(out, null, 2));
"""


def _run_scenarios() -> list[dict]:
    node = shutil_which("node")
    assert node, "node required"
    script = _HARNESS.replace("__THEME_JS__", json.dumps(_THEME_JS))
    proc = subprocess.run(
        [node, "--input-type=commonjs", "-e", script],
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    # The harness prints only JSON; tolerate leading non-JSON by slicing.
    text = proc.stdout.strip()
    start = text.find("[")
    assert start >= 0, f"no JSON in node output:\n{text}\n--stderr--\n{proc.stderr}"
    return json.loads(text[start:])


def _by(name: str, results: list[dict]) -> dict:
    for r in results:
        if r["name"] == name:
            return r
    raise AssertionError(f"scenario {name!r} missing from harness output")


# Run the Node harness ONCE at import (after the node-availability skip gate).
RESULTS: list[dict] = _run_scenarios()


def test_fresh_boot_defaults_to_technical() -> None:
    light = _by("fresh-os-light", RESULTS)
    assert light["dataTheme"] == "technical-light"
    assert light["api"]["family"] == "technical" and light["api"]["mode"] == "auto"
    # Fresh-visitor modifier defaults: Normal contrast, Borders Off, Compact.
    assert light["api"]["contrast"] == "normal"
    assert light["api"]["border"] == "off"
    assert light["api"]["density"] == "compact"
    assert light["dataContrast"] == "normal"
    assert light["dataBorder"] == "off"
    assert light["dataDensity"] == "compact"
    # No preference persisted on a fresh boot (boot never writes defaults).
    assert light["storage"] == {}


def test_auto_follows_os_live() -> None:
    dark = _by("fresh-os-dark", RESULTS)
    assert dark["dataTheme"] == "technical-dark"
    assert dark["api"]["resolvedMode"] == "dark"


@pytest.mark.parametrize(
    ("name", "family", "mode", "resolved"),
    [
        ("purple-light", "purple", "light", "purple-light"),
        ("purple-dark", "purple", "dark", "purple-dark"),
        ("coffee-light", "coffee", "light", "coffee-light"),
        ("coffee-dark", "coffee", "dark", "coffee-dark"),
    ],
)
def test_optional_families_resolve_explicit_modes(name, family, mode, resolved) -> None:
    result = _by(name, RESULTS)
    assert result["after"]["api"] == {
        "family": family,
        "mode": mode,
        "resolved": resolved,
        "resolvedMode": mode,
        "contrast": "normal",
        "border": "off",
        "density": "compact",
    }
    assert result["after"]["dataTheme"] == resolved
    assert result["after"]["storage"]["okf-theme-family"] == family
    assert result["after"]["storage"]["okf-theme-mode"] == mode


@pytest.mark.parametrize(
    ("name", "family", "resolved"),
    [
        ("purple-auto-light", "purple", "purple-light"),
        ("purple-auto-dark", "purple", "purple-dark"),
        ("coffee-auto-light", "coffee", "coffee-light"),
        ("coffee-auto-dark", "coffee", "coffee-dark"),
    ],
)
def test_optional_families_persist_auto_without_pinning_mode(name, family, resolved) -> None:
    result = _by(name, RESULTS)
    assert result["after"]["api"]["family"] == family
    assert result["after"]["api"]["mode"] == "auto"
    assert result["after"]["api"]["resolved"] == resolved
    assert result["after"]["storage"]["okf-theme-family"] == family
    assert result["after"]["storage"]["okf-theme-mode"] == "auto"
    assert result["after"]["storage"].get("okf-theme") is None


def test_legacy_dark_migrates_once() -> None:
    r = _by("legacy-dark", RESULTS)
    assert r["dataTheme"] == "technical-dark"
    # Migration writes BOTH canonical keys (explicit family + explicit mode).
    assert r["storage"].get("okf-theme-family") == "technical"
    assert r["storage"].get("okf-theme-mode") == "dark"
    # The legacy mirror is normalized to the new resolved theme (not "dark").
    assert r["storage"].get("okf-theme") == "technical-dark"


def test_legacy_retired_names_migrate_to_documented_family() -> None:
    pastel = _by("legacy-pastel", RESULTS)
    assert pastel["storage"].get("okf-theme-family") == "swiss"
    assert pastel["dataTheme"] == "swiss-light"
    midnight = _by("legacy-midnight", RESULTS)
    assert midnight["storage"].get("okf-theme-family") == "technical"
    assert midnight["dataTheme"] == "technical-dark"


def test_canonical_keys_win_and_auto_removes_mirror() -> None:
    r = _by("canonical-over-legacy-auto", RESULTS)
    # Canonical family=swiss + mode=auto → swiss-light (OS light), NOT the
    # stale technical-dark legacy mirror.
    assert r["dataTheme"] == "swiss-light"
    assert r["api"]["family"] == "swiss" and r["api"]["mode"] == "auto"
    # Auto mode must REMOVE the legacy concrete mirror (never pin Auto).
    assert "okf-theme" not in r["storage"], (
        "Auto mode left a concrete legacy mirror; Auto must follow OS"
    )


def test_corrupt_values_ignored_but_key_retained() -> None:
    # Corrupt family → that dimension falls back (technical default), but the
    # raw key stays (it's the migration gate; deleting it would resurrect a
    # stale legacy mirror as authority).
    cf = _by("corrupt-family", RESULTS)
    assert cf["api"]["family"] == "technical"  # fell back
    assert "okf-theme-family" in cf["storage"]  # key retained
    assert cf["api"]["mode"] == "dark"          # valid mode honoured
    cm = _by("corrupt-mode", RESULTS)
    assert cm["api"]["mode"] == "auto"          # fell back to auto
    assert "okf-theme-mode" in cm["storage"]
    assert cm["api"]["family"] == "swiss"       # valid family honoured


def test_configured_concrete_registers_without_user_pref() -> None:
    r = _by("configured-concrete", RESULTS)
    # No user pref; the explicit JSON configured preference registers. The
    # painted swiss-dark value alone would not have authority.
    assert r["api"]["family"] == "swiss" and r["api"]["mode"] == "dark"
    assert r["dataTheme"] == "swiss-dark"
    # Boot never persists the configured value into storage.
    assert r["storage"] == {}


def test_storage_denial_keeps_resolution_working() -> None:
    r = _by("storage-denial", RESULTS)
    # localStorage throws on every write, but theme still resolves and the
    # data-theme attribute is set — persistence is the only thing disabled.
    assert r["dataTheme"] == "technical-dark"
    assert r["api"]["resolvedMode"] == "dark"
    assert r["storage"] == {}  # nothing could be persisted


# ---------------------------------------------------------------------------
# P1-3 precedence: validated stored user preference > studio.theme > default.
# "auto" studio.theme must seed a follow-OS runtime mode (not the painted
# concrete default); concrete studio.theme seeds family+mode; an invalid
# studio.theme falls back to the painted concrete data-theme.
# ---------------------------------------------------------------------------

def test_bootstrap_auto_seeds_follow_os_mode() -> None:
    light = _by("bootstrap-auto-oslight", RESULTS)
    dark = _by("bootstrap-auto-osdark", RESULTS)
    # studio.theme "auto" -> runtime mode is Auto (follows OS), NOT the
    # painted technical-light's "light".
    assert light["api"]["mode"] == "auto"
    assert dark["api"]["mode"] == "auto"
    # Resolved follows OS live while Auto.
    assert light["dataTheme"] == "technical-light"
    assert dark["dataTheme"] == "technical-dark"
    assert dark["api"]["resolvedMode"] == "dark"
    # Boot never persists the configured "auto" into storage.
    assert light["storage"] == {}


def test_bootstrap_concrete_seeds_family_and_mode() -> None:
    r = _by("bootstrap-concrete", RESULTS)
    assert r["api"]["family"] == "swiss" and r["api"]["mode"] == "dark"
    assert r["dataTheme"] == "swiss-dark"
    assert r["storage"] == {}  # configured value never persisted at boot


def test_user_preference_beats_studio_theme() -> None:
    r = _by("user-pref-beats-bootstrap", RESULTS)
    # Saved technical+light wins over studio.theme swiss-dark (and OS dark).
    assert r["api"]["family"] == "technical"
    assert r["api"]["mode"] == "light"
    assert r["dataTheme"] == "technical-light"


def test_invalid_studio_theme_falls_to_painted_default() -> None:
    r = _by("bootstrap-invalid-falls-to-painted", RESULTS)
    # A bogus studio.theme falls through to Technical Auto, not the painted
    # concrete hint (which is intentionally not configuration authority).
    assert r["api"]["family"] == "technical"
    assert r["api"]["mode"] == "auto"
    assert r["dataTheme"] == "technical-light"


# ---------------------------------------------------------------------------
# P1-7 storage hardening: getItem/write failures + invalid setter input must
# never corrupt the DOM attribute or storage.
# ---------------------------------------------------------------------------

def test_getitem_failure_does_not_break_resolution() -> None:
    r = _by("getitem-fails", RESULTS)
    # localStorage.getItem throws on every read; theme still resolves from
    # defaults and sets data-theme (no user pref could be read).
    assert r["api"]["family"] == "technical"
    assert r["api"]["mode"] == "auto"
    assert r["dataTheme"] == "technical-dark"  # OS dark + auto


def test_invalid_setter_input_does_not_corrupt_state() -> None:
    r = _by("invalid-setter-no-corrupt", RESULTS)
    after = r["after"]
    # Every invalid setter is rejected: family/mode/resolved are unchanged
    # from the fresh boot, and nothing was persisted.
    assert after["api"]["family"] == "technical"
    assert after["api"]["mode"] == "auto"
    assert after["dataTheme"] == "technical-light"
    assert after["api"]["contrast"] == "normal" and after["api"]["border"] == "off"
    assert after["dataContrast"] == "normal" and after["dataBorder"] == "off"
    assert after["storage"] == {}


def test_write_failure_during_setter_keeps_resolution_and_clean_storage() -> None:
    r = _by("write-fails-on-setter", RESULTS)
    after = r["after"]
    # setFamily("swiss") is valid -> resolution switches (session state) even
    # though the persisting write threw; storage stays clean (denial-safe).
    assert after["api"]["family"] == "swiss"
    assert after["dataTheme"] == "swiss-light"  # auto + OS light
    assert after["storage"] == {}


# ---------------------------------------------------------------------------
# P1 outcome-audit (GAP-5): live Auto OS resolution + explicit-mode pinning.
# A matchMedia change while Auto flips the resolved theme live; an explicit
# mode is a pinned preference and must NOT flip.
# ---------------------------------------------------------------------------

def test_auto_follows_live_os_change() -> None:
    r = _by("auto-os-flip-to-dark", RESULTS)
    # Before the flip: Auto + OS light resolves technical-light.
    assert r["dataTheme"] == "technical-light"
    assert r["api"]["mode"] == "auto"
    flips = r["osFlips"]
    assert len(flips) == 1
    after = flips[0]
    # After OS -> dark: Auto follows it live; resolved flips to technical-dark.
    assert after["api"]["mode"] == "auto"  # still Auto (never auto-promoted)
    assert after["dataTheme"] == "technical-dark"
    assert after["api"]["resolved"] == "technical-dark"
    assert after["api"]["resolvedMode"] == "dark"


def test_explicit_mode_does_not_flip_on_os_change() -> None:
    r = _by("explicit-light-no-flip-on-os-change", RESULTS)
    # Pinned light; OS goes dark but the explicit preference holds.
    assert r["dataTheme"] == "technical-light"
    assert r["api"]["mode"] == "light"
    after = r["osFlips"][0]
    assert after["api"]["mode"] == "light"  # still explicit light, not Auto
    assert after["dataTheme"] == "technical-light"
    assert after["api"]["resolvedMode"] == "light"


def test_modifiers_apply_persist_and_remain_independent() -> None:
    r = _by("modifiers-default-and-switch", RESULTS)
    after = r["after"]
    assert r["api"]["contrast"] == "normal" and r["api"]["border"] == "off"
    assert after["api"]["family"] == "technical" and after["api"]["mode"] == "auto"
    assert after["api"]["resolved"] == "technical-light"
    assert after["api"]["contrast"] == "soft" and after["api"]["border"] == "on"
    assert after["dataContrast"] == "soft" and after["dataBorder"] == "on"
    assert after["storage"]["okf-theme-contrast"] == "soft"
    assert after["storage"]["okf-theme-border"] == "on"


def test_configured_and_saved_modifier_precedence() -> None:
    configured = _by("configured-modifiers", RESULTS)
    assert configured["api"]["contrast"] == "soft"
    # A configured non-default (Borders On) beats the Borders Off default.
    assert configured["api"]["border"] == "on"
    assert configured["dataBorder"] == "on"
    assert configured["storage"] == {}
    saved = _by("saved-modifiers-beat-config", RESULTS)
    assert saved["api"]["contrast"] == "normal"
    assert saved["api"]["border"] == "off"
    assert saved["dataContrast"] == "normal" and saved["dataBorder"] == "off"


def test_corrupt_modifiers_fall_back_and_are_retained() -> None:
    r = _by("corrupt-modifiers", RESULTS)
    assert r["api"]["contrast"] == "soft"
    assert r["api"]["border"] == "on"
    assert r["storage"]["okf-theme-contrast"] == "bogus"
    assert r["storage"]["okf-theme-border"] == "broken"


def test_modifier_write_denial_keeps_session_state_working() -> None:
    r = _by("modifier-write-denial", RESULTS)
    after = r["after"]
    assert after["api"]["contrast"] == "soft" and after["api"]["border"] == "on"
    assert after["dataContrast"] == "soft" and after["dataBorder"] == "on"
    assert after["storage"] == {}


def test_density_defaults_validates_persists_and_survives_denial() -> None:
    switched = _by("density-default-and-switch", RESULTS)
    assert switched["api"]["density"] == "compact"
    assert switched["after"]["api"]["density"] == "comfortable"
    assert switched["after"]["dataDensity"] == "comfortable"
    assert switched["after"]["storage"]["okf-theme-density"] == "comfortable"
    event = switched["events"][-1]
    assert event["density"] == "comfortable"
    assert event["previousDensity"] == "compact"
    corrupt = _by("density-corrupt", RESULTS)
    assert corrupt["api"]["density"] == "compact"
    assert corrupt["storage"]["okf-theme-density"] == "tiny"
    denied = _by("density-write-denial", RESULTS)
    assert denied["after"]["api"]["density"] == "comfortable"
    assert denied["after"]["dataDensity"] == "comfortable"
    assert denied["after"]["storage"] == {}


def test_reset_clears_user_state_and_returns_to_configured_preference() -> None:
    r = _by("reset-preferences", RESULTS)
    after = r["after"]
    assert after["api"]["family"] == "swiss"
    assert after["api"]["mode"] == "dark"
    assert after["api"]["contrast"] == "soft"
    assert after["api"]["border"] == "on"
    assert after["api"]["density"] == "compact"
    assert after["dataDensity"] == "compact"
    assert after["dataTheme"] == "swiss-dark"
    assert after["storage"] == {}
