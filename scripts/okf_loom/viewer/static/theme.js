/* OKF viewer — canonical theme preference/state contract (sole owner).
 *
 * SINGLE OWNER of viewer appearance state for EVERY output target: served
 * wiki pages, the full-page graph view, the live studio, static builds, and
 * the single-file viewer. Loaded (or inlined) BEFORE capture-readiness.js /
 * portable-retrieval.js / wiki.js / graph.js / studio.js in each context;
 * those files consume `window.OKFLoomTheme` and the resolved `data-theme`
 * attribute and NEVER interpret theme storage themselves.
 *
 * State model (state-ownership contract):
 *   - User family  -> localStorage["okf-theme-family"]: "technical" | "swiss"
 *     | "purple" | "coffee".
 *   - User mode    -> localStorage["okf-theme-mode"]:   "auto" | "light" | "dark".
 *     Family and mode are orthogonal; each persists on its own, so
 *     Technical + Auto survives reload and navigation.
 *   - User modifiers -> localStorage["okf-theme-contrast"]: "normal" | "soft"
 *     and localStorage["okf-theme-border"]: "on" | "off". Modifiers are
 *     independent of family/mode and are applied as semantic root attributes.
 *   - Resolved theme (data-theme on <html>) is DERIVED ONLY:
 *     family + (mode, with "auto" following prefers-color-scheme LIVE).
 *     It is never persisted while mode is Auto — that would freeze Auto.
 *   - Precedence: valid saved user preference > valid configured preference
 *     > Technical Auto/OS fallback. The configured preference is captured at
 *     boot from the CSP-safe JSON blocks emitted by the renderer: the served
 *     studio bootstrap (okf-studio-bootstrap) wins for its supplied fields,
 *     while okf-theme-config carries the viewer config for every portable
 *     target. "auto" seeds a follow-OS runtime mode and a concrete value
 *     seeds family + mode. The server-painted concrete data-theme is ONLY a
 *     FOUC-safe first paint; it is never interpreted as a saved or configured
 *     Light/Dark choice. Booting never writes configured values to storage.
 *   - Legacy localStorage["okf-theme"] (concrete theme = pinned choice,
 *     absent = auto) is migrated ONCE into the two canonical keys when
 *     neither exists, then maintained as a WRITE-ONLY compatibility mirror:
 *     set to the resolved concrete theme while mode is explicit, removed
 *     while mode is Auto, and reconciled at boot. It is never read as
 *     authority again.
 *   - Storage denial disables persistence ONLY: resolution and in-page
 *     switching keep working from module state; the theme never changes
 *     because storage threw.
 *
 * On every ACTUAL visual-state change (resolved theme or modifier) this
 * dispatches ONE `okf-loom:themeChanged` CustomEvent on window (no
 * duplicate/no-op events) with detail {theme, previousTheme, family, mode, resolvedMode,
 * previousResolvedMode, contrast, previousContrast, border, previousBorder,
 * generation} — enough for the graph canvas and light/dark-sensitive
 * renderers (highlight.js / Mermaid) to consume the latest state without
 * becoming owners.
 *
 * Also owns the Appearance popover wiring (#okf-theme trigger +
 * #okf-appearance-menu, server-rendered by render.py:_appearance_control_html)
 * because the popover exists on wiki, graph, studio AND single-file pages —
 * previously each surface carried its own copy.
 *
 * CSP: self-hosted (or inlined by the single-file builder). No inline
 * handlers, no remote scripts, no eval. No-JS: the trigger is hidden and the
 * menu carries `hidden` so both are inert without this script.
 */
(function () {
  "use strict";

  // This is a capability marker, not theme state. Set it before any other
  // boot work so parser-blocking theme.js can suppress ordinary no-JS notes
  // before the body is parsed. The deferred viewer modules mirror the marker
  // on the body element when they start; no-JS pages never receive either marker.
  var root = document.documentElement;
  try { root.setAttribute("data-okf-js", "1"); } catch (e) {}

  // Idempotent: the asset can be both linked and inlined (single-file); the
  // first execution wins.
  if (window.OKFLoomTheme) return;

  // ---- Shared overlay stack (Escape topmost-layer coordination) ----------
  // Provided on every surface (theme.js is the earliest viewer script). The
  // theme menu registers here; other surfaces MAY register too so ONE Escape
  // closes only the topmost registered overlay. This file's own Escape
  // handler is self-contained (it only closes its own menu) so it never
  // interferes with surfaces that keep their own Escape handling (studio
  // command palette / editors) — see _onEscape below.
  if (!window.OKFOverlayStack) {
    var _stack = [];
    window.OKFOverlayStack = {
      push: function (e) { if (_stack.indexOf(e) < 0) _stack.push(e); },
      remove: function (e) {
        var i;
        while ((i = _stack.indexOf(e)) >= 0) _stack.splice(i, 1);
      },
      depth: function () { return _stack.length; },
      top: function () { return _stack.length ? _stack[_stack.length - 1] : null; },
    };
  }

  // ---- Allow-lists (KEEP IN SYNC with render.py / config.py / assets.py) -
  // tests/test_theme_contract.py enforces parity.
  var FAMILIES = ["technical", "swiss", "purple", "coffee"];
  var MODES = ["auto", "light", "dark"];
  var CONTRASTS = ["normal", "soft"];
  var BORDERS = ["on", "off"];
  var DENSITIES = ["comfortable", "compact"];
  var THEMES = [
    "technical-light", "technical-dark", "swiss-light", "swiss-dark",
    "purple-light", "purple-dark", "coffee-light", "coffee-dark",
  ];
  // Legacy localStorage["okf-theme"] concrete values → new family-mode
  // resolved theme. Old "light" (the V2 default look) maps to Technical-light
  // so existing users keep exactly the visuals they had.
  var LEGACY_THEMES = {
    light: "technical-light", dark: "technical-dark",
    pastel: "swiss-light", sepia: "swiss-light", midnight: "technical-dark",
  };

  var FAMILY_KEY = "okf-theme-family";
  var MODE_KEY = "okf-theme-mode";
  var CONTRAST_KEY = "okf-theme-contrast";
  var BORDER_KEY = "okf-theme-border";
  var DENSITY_KEY = "okf-theme-density";
  var LEGACY_KEY = "okf-theme";

  // Fresh-visitor defaults: Technical family, Auto (follow the OS colour
  // scheme — the "System" mode), Normal contrast, Borders Off, Compact
  // density. KEEP IN SYNC with render.py _DEFAULT_* and viewer/assets.py
  // _DEFAULT_CONFIG (the server paints the same modifiers on <html>).
  var DEFAULT_FAMILY = "technical";   // V2's existing look
  var DEFAULT_MODE = "auto";
  var DEFAULT_CONTRAST = "normal";
  var DEFAULT_BORDER = "off";
  var DEFAULT_DENSITY = "compact";

  // ---- Storage (denial-safe) -------------------------------------------
  // Every access is guarded: a throwing localStorage (privacy mode, denied
  // permission, file:// quirks) turns persistence off and nothing else.
  function readKey(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function writeKey(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function removeKey(k) { try { localStorage.removeItem(k); } catch (e) {} }
  function pick(value, allowed) {
    return allowed.indexOf(value) >= 0 ? value : null;
  }

  // ---- Configured preference (captured before any client write) ---------
  // Precedence for the configured (non-user) preference, consulted ONLY when
  // no validated user preference exists for that dimension:
  //   1. studio.theme from the served bootstrap JSON (okf-studio-bootstrap)
  //      — the live studio config. It may be "auto" (follow OS at runtime,
  //      no family preference) or a concrete family-mode theme. This is the
    //      only source that preserves the operator's actual "auto" choice.
    //   2. okf-theme-config from the renderer, or nothing -> viewer defaults
    //      (Technical + Auto). The server-painted concrete data-theme is only
    //      the FOUC-safe first paint and is never read as configuration.
  // The bootstrap JSON is emitted in <head> BEFORE this script (server.py),
  // so it is already in the DOM when this runs. Boot never persists the
  // configured value into storage.
  var configFamily = null;
  var configMode = null;
  var configContrast = null;
  var configBorder = null;
  (function captureConfigured() {
    function readConfig(id) {
      var node = document.getElementById(id);
      if (!node) return null;
      try {
        var parsed = JSON.parse(node.textContent || "null");
        return parsed && typeof parsed === "object" ? parsed : null;
      } catch (e) { return null; }
    }
    function applyConfig(raw) {
      if (!raw) return;
      // A configured "auto" carries no family preference; DEFAULT_FAMILY is
      // applied only by effectiveFamily(). This keeps Technical Auto distinct
      // from an explicit Technical Light/Dark choice.
      if (raw.theme === "auto") {
        configFamily = null;
        configMode = "auto";
      } else if (THEMES.indexOf(raw.theme) >= 0) {
        var sep = raw.theme.lastIndexOf("-");
        configFamily = raw.theme.slice(0, sep);
        configMode = raw.theme.slice(sep + 1);
      }
      var contrast = pick(raw.contrast, CONTRASTS);
      var border = pick(raw.border, BORDERS);
      if (contrast) configContrast = contrast;
      if (border) configBorder = border;
    }
    // The renderer emits this block for every target, including live pages.
    // A live studio bootstrap is an explicit higher-precedence configured
    // source, but only for fields it actually supplies.
    applyConfig(readConfig("okf-theme-config"));
    applyConfig(readConfig("okf-studio-bootstrap"));
  })();

  // ---- User preference (canonical keys; legacy migrated once) -----------
  // In-page state: read once at boot, written through on user changes, so
  // storage denial degrades to session-only preferences deterministically.
  //
  // Corruption policy: an unrecognized canonical value is IGNORED per
  // dimension (that dimension falls back to configured/default) but the raw
  // key is RETAINED — its presence is the migration gate, and deleting it
  // would let a stale legacy mirror be re-read as authority on the next boot.
  var rawFamily = readKey(FAMILY_KEY);
  var rawMode = readKey(MODE_KEY);
  var rawContrast = readKey(CONTRAST_KEY);
  var rawBorder = readKey(BORDER_KEY);
  var rawDensity = readKey(DENSITY_KEY);
  var userFamily = pick(rawFamily, FAMILIES);
  var userMode = pick(rawMode, MODES);
  var userContrast = pick(rawContrast, CONTRASTS);
  var userBorder = pick(rawBorder, BORDERS);
  var userDensity = pick(rawDensity, DENSITIES);

  if (rawFamily === null && rawMode === null) {
    // One-time legacy migration — only while NEITHER canonical key exists.
    var legacy = readKey(LEGACY_KEY);
    if (legacy && LEGACY_THEMES[legacy]) legacy = LEGACY_THEMES[legacy];
    legacy = pick(legacy, THEMES);
    if (legacy) {
      var sep = legacy.lastIndexOf("-");
      // A stored concrete theme was an explicit pinned choice, so it
      // migrates to explicit family AND explicit mode (not auto). The
      // parsed preference lands in module state FIRST: if storage turned
      // write-only since the legacy value was saved, the writes below fail
      // silently and this page still resolves the user's preference.
      userFamily = legacy.slice(0, sep);
      userMode = legacy.slice(sep + 1);
      writeKey(FAMILY_KEY, userFamily);
      writeKey(MODE_KEY, userMode);
      writeKey(LEGACY_KEY, legacy); // normalize a retired name in the mirror
    }
  } else {
    // Canonical keys exist: reconcile the write-only mirror so pages built
    // by older versions never read a stale/conflicting value. The mirror is
    // REMOVED unless the user state is a FULLY explicit concrete theme
    // (family AND mode both valid) — partial preferences and corrupt values
    // fall back per dimension, and a stale concrete mirror must never
    // survive that. Only USER state is ever written to the mirror.
    var rawMirror = readKey(LEGACY_KEY);
    if (userMode === "auto" || !(userFamily && userMode)) {
      if (rawMirror !== null) removeKey(LEGACY_KEY);
    } else {
      var want = userFamily + "-" + userMode;
      if (rawMirror !== want) writeKey(LEGACY_KEY, want);
    }
  }

  // ---- Resolution (derived only; never persisted) ------------------------
  function effectiveFamily() { return userFamily || configFamily || DEFAULT_FAMILY; }
  function effectiveMode() { return userMode || configMode || DEFAULT_MODE; }
  function effectiveContrast() { return userContrast || configContrast || DEFAULT_CONTRAST; }
  function effectiveBorder() { return userBorder || configBorder || DEFAULT_BORDER; }
  function effectiveDensity() { return userDensity || DEFAULT_DENSITY; }
  function osDark() {
    return !!(window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);
  }
  function resolvedTheme() {
    var mode = effectiveMode();
    var dark = mode === "auto" ? osDark() : mode === "dark";
    return effectiveFamily() + (dark ? "-dark" : "-light");
  }
  function resolvedModeOf(theme) {
    return theme && theme.indexOf("-dark") >= 0 ? "dark" : "light";
  }

  // data-theme may be a concrete first-paint value emitted by Python. It is a
  // derived paint hint, never configured/user authority. The modifier attrs
  // are treated the same way: the resolver owns their final values.
  var appliedTheme = pick(root.getAttribute("data-theme"), THEMES);
  var appliedContrast = effectiveContrast();
  var appliedBorder = effectiveBorder();
  var appliedDensity = effectiveDensity();
  var generation = 0;
  function apply() {
    var next = resolvedTheme();
    var nextContrast = effectiveContrast();
    var nextBorder = effectiveBorder();
    var nextDensity = effectiveDensity();
    var prev = appliedTheme;
    var prevContrast = appliedContrast;
    var prevBorder = appliedBorder;
    var prevDensity = appliedDensity;
    root.setAttribute("data-theme", next);
    root.setAttribute("data-okf-contrast", nextContrast);
    root.setAttribute("data-okf-border", nextBorder);
    root.setAttribute("data-okf-density", nextDensity);
    reflectMenu();
    if (next === prev && nextContrast === prevContrast && nextBorder === prevBorder && nextDensity === prevDensity) return;
    appliedTheme = next;
    appliedContrast = nextContrast;
    appliedBorder = nextBorder;
    appliedDensity = nextDensity;
    generation += 1;
    dispatch({
      theme: next,
      previousTheme: prev,
      family: effectiveFamily(),
      mode: effectiveMode(),
      resolvedMode: resolvedModeOf(next),
      previousResolvedMode: prev ? resolvedModeOf(prev) : null,
      contrast: nextContrast,
      previousContrast: prevContrast,
      border: nextBorder,
      previousBorder: prevBorder,
      density: nextDensity,
      previousDensity: prevDensity,
      generation: generation,
    });
  }

  function dispatch(detail) {
    try {
      window.dispatchEvent(new CustomEvent("okf-loom:themeChanged", { detail: detail }));
    } catch (e) {
      // CustomEvent may be unavailable in very old engines; theme still
      // applies via the data-theme attribute.
    }
  }

  function persistPreference() {
    if (userFamily) writeKey(FAMILY_KEY, userFamily);
    if (userMode) writeKey(MODE_KEY, userMode);
    if (userContrast) writeKey(CONTRAST_KEY, userContrast);
    if (userBorder) writeKey(BORDER_KEY, userBorder);
    if (userDensity) writeKey(DENSITY_KEY, userDensity);
    // Compatibility mirror: concrete only while the effective mode is
    // explicit; removed while Auto so older same-origin pages keep
    // following the OS.
    var mode = effectiveMode();
    if (mode !== "auto" && userFamily && userMode) {
      writeKey(LEGACY_KEY, effectiveFamily() + "-" + mode);
    } else {
      removeKey(LEGACY_KEY);
    }
  }

  // ---- Preference setters (the only writers) -----------------------------
  function setFamily(family) {
    if (FAMILIES.indexOf(family) < 0) return;
    userFamily = family;
    persistPreference();
    apply();
  }
  function setMode(mode) {
    // "auto" is persisted explicitly: a chosen Auto outranks a configured
    // concrete default, unlike the absence of any preference.
    if (MODES.indexOf(mode) < 0) return;
    userMode = mode;
    persistPreference();
    apply();
  }
  function setContrast(contrast) {
    if (CONTRASTS.indexOf(contrast) < 0) return;
    userContrast = contrast;
    persistPreference();
    apply();
  }
  function setBorder(border) {
    if (BORDERS.indexOf(border) < 0) return;
    userBorder = border;
    persistPreference();
    apply();
  }
  function setDensity(density) {
    if (DENSITIES.indexOf(density) < 0) return;
    userDensity = density;
    persistPreference();
    apply();
  }
  function resetPreferences() {
    userFamily = null;
    userMode = null;
    userContrast = null;
    userBorder = null;
    userDensity = null;
    removeKey(FAMILY_KEY);
    removeKey(MODE_KEY);
    removeKey(CONTRAST_KEY);
    removeKey(BORDER_KEY);
    removeKey(DENSITY_KEY);
    removeKey(LEGACY_KEY);
    apply();
  }
  function setTheme(theme) {
    // Concrete pick (e.g. studio command palette): explicit family + mode.
    if (THEMES.indexOf(theme) < 0) return;
    var sep = theme.lastIndexOf("-");
    userFamily = theme.slice(0, sep);
    userMode = theme.slice(sep + 1);
    persistPreference();
    apply();
  }

  // ---- Live OS resolution while Auto ------------------------------------
  // Only follows the OS while the effective mode is Auto; an explicit
  // light/dark choice is a pinned preference and must not flip.
  var _osMq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function _onOsChange() { if (effectiveMode() === "auto") apply(); }
  if (_osMq) {
    if (_osMq.addEventListener) _osMq.addEventListener("change", _onOsChange);
    else if (_osMq.addListener) _osMq.addListener(_onOsChange);
  }

  // Cross-tab storage changes are external inputs, never a second writer.
  // Validate each incoming value, update only module state, and let apply()
  // commit the complete root projection + one canonical event atomically.
  function _onStorage(e) {
    if (!e) return;
    if (e.key === FAMILY_KEY) userFamily = pick(e.newValue, FAMILIES);
    else if (e.key === MODE_KEY) userMode = pick(e.newValue, MODES);
    else if (e.key === CONTRAST_KEY) userContrast = pick(e.newValue, CONTRASTS);
    else if (e.key === BORDER_KEY) userBorder = pick(e.newValue, BORDERS);
    else if (e.key === DENSITY_KEY) userDensity = pick(e.newValue, DENSITIES);
    else return;
    apply();
  }
  if (window.addEventListener) window.addEventListener("storage", _onStorage);

  // ---- Appearance popover -----------------------------------------------
    // Server-rendered by render.py:_appearance_control_html on wiki, graph,
    // studio and single-file pages; wired here once. aria-checked/checked is
    // corrected at boot (the server cannot read localStorage). The popover is
    // a labelled form with native radio inputs grouped by fieldsets; roving
    // tabindex lets Tab enter each group once while Arrow/Home/End select
    // within a group. Module-scoped because the handlers close over them.
  var trigger = null, menu = null;
  var _menuOverlay = null;
  var _lastFocused = null;

  function reflectMenu() {
    var fam = effectiveFamily(), mode = effectiveMode();
    var familyLabel = fam.charAt(0).toUpperCase() + fam.slice(1);
    if (trigger) {
      var triggerLabel = trigger.querySelector(".okf-appearance-trigger__label");
      var triggerFamily = trigger.querySelector(".okf-appearance-trigger__family");
      var triggerSwatch = trigger.querySelector(".okf-appearance-trigger__swatch");
      if (triggerFamily) triggerFamily.textContent = familyLabel;
      else if (triggerLabel) triggerLabel.textContent = "Appearance · " + familyLabel;
      if (triggerSwatch) {
        triggerSwatch.className = "okf-appearance-trigger__swatch okf-appearance-trigger__swatch--" + fam;
      }
      // The trigger's aria-labelledby references the same visible prefix and
      // family nodes. Do not replace that label with parallel wording: WCAG
      // 2.5.3 requires the visible “Appearance · Family” text to remain in the
      // computed accessible name.
      trigger.removeAttribute("aria-label");
    }
    if (!menu) return;
    var autoSub = menu.querySelector('[data-okf-set="mode"][data-okf-val="auto"]');
    autoSub = autoSub && autoSub.closest(".okf-appearance__opt");
    autoSub = autoSub && autoSub.querySelector(".okf-appearance__sub");
    if (autoSub) autoSub.textContent = familyLabel + " family + follow OS colour mode";
    var opts = menu.querySelectorAll("input[data-okf-set]"), i, o, wrapper;
    for (i = 0; i < opts.length; i++) {
      o = opts[i];
      var key = o.getAttribute("data-okf-set");
      var val = o.getAttribute("data-okf-val");
      var isChecked = key === "family" ? fam === val :
        key === "mode" ? mode === val :
        key === "contrast" ? effectiveContrast() === val :
        key === "border" ? effectiveBorder() === val : effectiveDensity() === val;
      o.setAttribute("aria-checked", isChecked ? "true" : "false");
      o.checked = isChecked;
      wrapper = o.closest && o.closest(".okf-appearance__opt");
      if (wrapper) wrapper.setAttribute("aria-checked", isChecked ? "true" : "false");
    }
    syncTabindex();
    if (!menu.hidden) positionMenu();
  }

  // Roving tabindex: exactly one tabindex=0 per group (the checked option);
  // peers are tabindex=-1. Tab enters each group once; arrows move within.
  function syncTabindex() {
    if (!menu) return;
    var groups = menu.querySelectorAll('[role="group"]'), g;
    for (g = 0; g < groups.length; g++) {
      var opts = groups[g].querySelectorAll("input[data-okf-set]");
      var foundChecked = false;
      for (var i = 0; i < opts.length; i++) {
        if (!foundChecked && opts[i].getAttribute("aria-checked") === "true") {
          opts[i].setAttribute("tabindex", "0"); foundChecked = true;
        } else {
          opts[i].setAttribute("tabindex", "-1");
        }
      }
      if (!foundChecked && opts.length) opts[0].setAttribute("tabindex", "0");
    }
  }

  function _groupOf(opt) {
    var node = opt;
    while (node && node.getAttribute && node.getAttribute("role") !== "group") node = node.parentNode;
    return node && node.querySelectorAll ? node.querySelectorAll("[data-okf-set]") : null;
  }

  function _focusOpt(opt) {
    if (!opt) return;
    var control = opt.matches && opt.matches("input[data-okf-set]") ? opt :
      (opt.querySelector && opt.querySelector("input[data-okf-set]"));
    try { if (control) control.focus(); else if (opt.focus) opt.focus(); } catch (e) {}
  }

  function _selectOpt(opt) {
    if (!opt) return;
    var key = opt.getAttribute("data-okf-set");
    var val = opt.getAttribute("data-okf-val");
    if (key === "family") setFamily(val);
    else if (key === "mode") setMode(val);
    else if (key === "contrast") setContrast(val);
    else if (key === "border") setBorder(val);
    else if (key === "density") setDensity(val);
    _focusOpt(opt);
  }

  function positionMenu() {
    if (!menu || !trigger || menu.hidden) return;
    var previousScroll = menu.scrollTop;
    menu.style.width = "min(22rem, 100vw - 16px)";
    menu.style.maxHeight = "none";
    menu.style.overflowY = "visible";
    var r = trigger.getBoundingClientRect();
    var vw = window.innerWidth, vh = window.innerHeight;
    var vv = window.visualViewport;
    if (vv) { vw = vv.width; vh = vv.height; }
    var mw = menu.offsetWidth, mh = menu.offsetHeight;
    var availH = vh - 16;
    if (mh > availH) { menu.style.overflowY = "auto"; menu.style.maxHeight = availH + "px"; mh = availH; }
    var left = r.right - mw;
    var top = r.bottom + 4;
    if (top + mh > vh - 8) top = r.top - mh - 4;
    left = Math.max(8, Math.min(left, vw - mw - 8));
    top = Math.max(8, Math.min(top, vh - mh - 8));
    menu.style.left = Math.round(left) + "px";
    menu.style.top = Math.round(top) + "px";
    menu.scrollTop = previousScroll;
  }

  function openMenu() {
    if (!menu || !menu.hidden) return;
    menu.hidden = false;
    if (trigger) trigger.setAttribute("aria-expanded", "true");
    reflectMenu();
    positionMenu();
    if (!_menuOverlay) {
      _menuOverlay = { close: function () { closeMenu(true); } };
    }
    window.OKFOverlayStack.push(_menuOverlay);
    // Focus the checked option in the first visible group (Colour mode).
    var firstGroup = menu.querySelector('[role="group"]');
    if (firstGroup) {
      _focusOpt(firstGroup.querySelector('input[aria-checked="true"][data-okf-set]') ||
                firstGroup.querySelector("input[data-okf-set]"));
    }
  }

  function closeMenu(restoreFocus) {
    if (!menu || menu.hidden) return;
    if (_menuOverlay) window.OKFOverlayStack.remove(_menuOverlay);
    menu.hidden = true;
    menu.style.left = ""; menu.style.top = "";
    menu.style.maxHeight = ""; menu.style.overflowY = ""; menu.style.width = "";
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    // Only Escape/overlay-stack dismissal restores focus to the trigger.
    // Outside-click keeps focus on the clicked destination; Tab/focus-exit
    // keeps focus on the element the user moved to. CRITICAL: when a higher
    // registered overlay is now topmost (e.g. an open aria-modal lightbox
    // this menu opened ABOVE), focus must move INTO that overlay via its
    // focusIn() — never to this trigger behind the modal (which would put
    // background focus behind an aria-modal and break the modal's trap).
    if (restoreFocus) {
      var top = window.OKFOverlayStack ? window.OKFOverlayStack.top() : null;
      if (top && typeof top.focusIn === "function") {
        try { top.focusIn(); } catch (e) {}
      } else if (trigger) {
        _focusOpt(trigger);
      }
    }
  }

  // The theme menu yields Escape (and focus restoration) to whatever overlay
  // is actually above it on the shared stack. If a higher registered overlay
  // (studio command palette, slide-over panel, conflict alertdialog, or any
  // future aria-modal that registers) is topmost, THIS menu must not consume
  // Escape or restore focus behind that modal — that would move focus to the
  // trigger, which sits behind an aria-modal. When nothing else has
  // registered, the open theme menu is implicitly topmost.
  function _isTopmost() {
    var top = window.OKFOverlayStack ? window.OKFOverlayStack.top() : null;
    return !top || top === _menuOverlay;
  }

  // Self-contained Escape: closes ONLY this menu when it is the topmost
  // registered overlay, and stops propagation so a background handler does
  // not also react. When the menu is closed, or when a higher overlay owns
  // the topmost layer, this does NOTHING — surfaces that own their own
  // Escape (studio command palette, editors, panels) keep full ownership.
  function _onKeyDown(e) {
    if (!menu || menu.hidden) {
      if (e.key === "Escape" && _menuOverlay && window.OKFOverlayStack.top() === _menuOverlay) {
        // Defensive: menu marked open on the stack but DOM says hidden.
        e.preventDefault(); e.stopPropagation(); closeMenu(true);
      }
      return;
    }
    // Menu is open. Yield Escape to a higher registered overlay so a single
    // Escape closes only the actually-topmost aria-modal, never this menu
    // plus a focus restore behind that modal.
    if (e.key === "Escape") {
      if (!_isTopmost()) return;
      e.preventDefault(); e.stopPropagation(); closeMenu(true); return;
    }
    var opt = e.target.closest && e.target.closest("input[data-okf-set]");
    if (!opt) return; // Arrow/Home/end only meaningful on an option
    var peers = _groupOf(opt);
    if (!peers) return;
    var idx = Array.prototype.indexOf.call(peers, opt);
    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
      e.preventDefault(); _selectOpt(peers[(idx + 1) % peers.length]);
    } else if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
      e.preventDefault(); _selectOpt(peers[(idx - 1 + peers.length) % peers.length]);
    } else if (e.key === "Home") {
      e.preventDefault(); _selectOpt(peers[0]);
    } else if (e.key === "End") {
      e.preventDefault(); _selectOpt(peers[peers.length - 1]);
    }
  }

  function _onTriggerKeyDown(e) {
    // Enter/Space on the native button activate it (the click handler opens
    // the menu). ArrowDown opens straight onto the first option.
    if (!menu && (e.key === "Enter" || e.key === " " || e.key === "ArrowDown")) return;
    if (e.key === "ArrowDown") { e.preventDefault(); openMenu(); }
  }

  function _onFocusOut(e) {
    // Tab/shift-Tab out of the menu (or trigger) closes without stealing
    // focus: relatedTarget is where focus is going — leave it there.
    if (!menu || menu.hidden) return;
    var going = e.relatedTarget;
    if (going && (menu.contains(going) || going === trigger)) return;
    closeMenu(false);
  }

  function _onClick(e) {
    var action = e.target.closest && e.target.closest("[data-okf-action]");
    if (action && action.getAttribute("data-okf-action") === "reset") {
      resetPreferences();
      _focusOpt(action);
      return;
    }
    var option = e.target.closest && e.target.closest(".okf-appearance__opt");
    var opt = option && option.querySelector ? option.querySelector("input[data-okf-set]") : null;
    if (opt) {
      _selectOpt(opt);
      // Keep focus on the activated option (radiogroup convention) and stay
      // open so the user can adjust the other axis; Tab/Escape exits.
      return;
    }
    // Click on the trigger toggles.
    if (trigger && (e.target === trigger || trigger.contains(e.target))) {
      if (menu.hidden) openMenu(); else closeMenu(true);
      return;
    }
    // Outside click closes without stealing focus.
    if (!menu.hidden && !menu.contains(e.target)) closeMenu(false);
  }

  function _onResizeScroll() { positionMenu(); }

  function _onDocumentClick(e) {
    if (!menu || menu.hidden) return;
    if ((trigger && trigger.contains(e.target)) || menu.contains(e.target)) return;
    closeMenu(false);
  }

  function _onDocumentFocusIn(e) {
    if (!menu || menu.hidden) return;
    if ((trigger && trigger.contains(e.target)) || menu.contains(e.target)) return;
    closeMenu(false);
  }

  function wireAppearancePopover() {
    trigger = document.getElementById("okf-theme");
    menu = document.getElementById("okf-appearance-menu");
    if (!trigger || !menu) return;
    // A press on a non-focusable part of the open dialog (a group legend, the
    // note, padding) used to blur the focused option to <body>; _onFocusOut
    // then saw no related target and closed the menu under the pointer. As a
    // programmatic focus target (tabindex=-1: never a Tab stop) the dialog
    // takes that focus itself, so focus stays inside and the menu stays open.
    if (!menu.hasAttribute("tabindex")) menu.setAttribute("tabindex", "-1");
    // The menu starts hidden (no-JS inert). Reflect the resolved state once.
    reflectMenu();
    // Capture phase for Escape so a background handler cannot also react.
    document.addEventListener("keydown", _onKeyDown, true);
    trigger.addEventListener("keydown", _onTriggerKeyDown);
    trigger.addEventListener("click", _onClick);
    menu.addEventListener("click", _onClick);
    menu.addEventListener("submit", function (e) { e.preventDefault(); });
    menu.addEventListener("toggle", positionMenu, true);
    document.addEventListener("click", _onDocumentClick);
    document.addEventListener("focusin", _onDocumentFocusIn);
    menu.addEventListener("focusout", _onFocusOut);
    trigger.addEventListener("focusout", _onFocusOut);
    window.addEventListener("resize", _onResizeScroll);
    window.addEventListener("scroll", _onResizeScroll, true);
    if (window.visualViewport) window.visualViewport.addEventListener("resize", _onResizeScroll);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireAppearancePopover);
  } else {
    wireAppearancePopover();
  }

  // ---- Resolve the initial theme before first paint ---------------------
  apply();

  // ---- Public API (frozen) ----------------------------------------------
  var listeners = [];
  window.OKFLoomTheme = Object.freeze({
    FAMILIES: FAMILIES.slice(),
    MODES: MODES.slice(),
    CONTRASTS: CONTRASTS.slice(),
    BORDERS: BORDERS.slice(),
    DENSITIES: DENSITIES.slice(),
    THEMES: THEMES.slice(),
    LEGACY_THEMES: Object.assign({}, LEGACY_THEMES),
    getFamily: function () { return effectiveFamily(); },
    getMode: function () { return effectiveMode(); },
    getResolvedTheme: function () { return resolvedTheme(); },
    getResolvedMode: function () { return resolvedModeOf(resolvedTheme()); },
    getContrast: function () { return effectiveContrast(); },
    getBorder: function () { return effectiveBorder(); },
    getDensity: function () { return effectiveDensity(); },
    getGeneration: function () { return generation; },
    setFamily: setFamily,
    setMode: setMode,
    setContrast: setContrast,
    setBorder: setBorder,
    setDensity: setDensity,
    reset: resetPreferences,
    setTheme: setTheme,
    on: function (name, fn) {
      if (name !== "change" || typeof fn !== "function") return function () {};
      listeners.push(fn);
      var h = function (e) { try { fn(e.detail); } catch (err) {} };
      window.addEventListener("okf-loom:themeChanged", h);
      return function () {
        window.removeEventListener("okf-loom:themeChanged", h);
        var i = listeners.indexOf(fn);
        if (i >= 0) listeners.splice(i, 1);
      };
    },
  });

  // ---- Successful-initialization signal (no-JS / failed-init safety) -----
  // Mirrors studio.js's okf-studio-booted pattern. wiki.css keeps the
  // Appearance trigger (#okf-theme) hidden until <html> carries this class,
  // so the control is inert not only when JavaScript is fully off but also
  // when theme.js itself is blocked or throws before reaching here (the menu
  // is server-rendered `hidden` regardless). Placed LAST so any throw above
  // prevents the class and leaves the trigger hidden.
  try { root.classList.add("okf-theme-ready"); } catch (e) {}
}());
