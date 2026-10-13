/* OKF viewer — canonical appearance preference/state contract (sole owner).
 *
 * SINGLE OWNER of viewer appearance state for EVERY output target: served
 * wiki pages, the full-page graph view, the live studio, static builds, and
 * the single-file viewer. Loaded (or inlined) BEFORE capture-readiness.js /
 * portable-retrieval.js / wiki.js / graph.js / studio.js in each context;
 * those files consume `window.OKFLoomTheme` and the resolved `data-theme`
 * attribute and NEVER interpret appearance storage themselves.
 *
 * State model (Ledger appearance):
 *   - User choice -> localStorage["okf-loom.appearance"]: one of the four
 *     palettes "bone" | "slate" | "clay" | "plum", or "system" (follow the
 *     OS: Bone in light mode, Slate in dark mode, LIVE).
 *   - Resolved palette (data-theme on <html>) is DERIVED ONLY. It is never
 *     persisted while the choice is "system" — that would freeze System.
 *   - Precedence: valid saved choice > valid configured choice > Bone. The
 *     configured choice is captured at boot from the CSP-safe JSON blocks
 *     emitted by the renderer: the served studio bootstrap
 *     (okf-studio-bootstrap) wins for its supplied fields, while
 *     okf-theme-config carries the viewer config for every portable target.
 *     The server-painted data-theme is ONLY a FOUC-safe first paint; it is
 *     never interpreted as a saved or configured choice. Booting never
 *     writes configured values to storage.
 *   - Migration: the retired keys (okf-theme-family + okf-theme-mode, and
 *     the older okf-theme) are read ONCE, only while the new key is absent,
 *     mapped to the nearest palette (every light variant -> Bone; Technical
 *     /Swiss dark -> Slate; Coffee dark -> Clay; Purple dark -> Plum; Auto
 *     -> System) and written to the new key. They are never authority again.
 *     Retired modifier keys (contrast/border/density) are ignored.
 *   - Storage denial disables persistence ONLY: resolution and in-page
 *     switching keep working from module state; the palette never changes
 *     because storage threw.
 *
 * On every ACTUAL palette change this dispatches ONE `okf-loom:themeChanged`
 * CustomEvent on window (no duplicate/no-op events) with detail {theme,
 * previousTheme, choice, scheme, previousScheme, resolvedMode,
 * previousResolvedMode, generation}. `scheme`/`resolvedMode` are "light" for
 * Bone and "dark" for Slate/Clay/Plum, so renderers (highlight.js, Mermaid)
 * and the graph canvas can follow the palette without becoming owners.
 *
 * Also owns the Appearance popover wiring (#okf-theme trigger +
 * #okf-appearance-menu, server-rendered by render.py:_appearance_control_html)
 * because the popover exists on wiki, graph, studio AND single-file pages.
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
  // command palette / editors) — see _onKeyDown below.
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
  var PALETTES = ["bone", "slate", "clay", "plum"];
  var CHOICES = ["bone", "slate", "clay", "plum", "system"];
  var SCHEMES = { bone: "light", slate: "dark", clay: "dark", plum: "dark" };
  var LABELS = { bone: "Bone", slate: "Slate", clay: "Clay", plum: "Plum", system: "System" };
  // Retired names -> Ledger choice. Mirrors config.py _LEGACY_THEME_MIGRATIONS.
  var LEGACY_THEMES = {
    auto: "system",
    "technical-light": "bone", "swiss-light": "bone",
    "purple-light": "bone", "coffee-light": "bone",
    "technical-dark": "slate", "swiss-dark": "slate",
    "coffee-dark": "clay", "purple-dark": "plum",
    light: "bone", pastel: "bone", sepia: "bone",
    dark: "slate", midnight: "slate",
  };
  var DEFAULT_CHOICE = "bone";
  var SYSTEM_LIGHT = "bone";
  var SYSTEM_DARK = "slate";

  var KEY = "okf-loom.appearance";
  var OLD_FAMILY_KEY = "okf-theme-family";
  var OLD_MODE_KEY = "okf-theme-mode";
  var OLD_THEME_KEY = "okf-theme";

  // ---- Storage (denial-safe) -------------------------------------------
  function readKey(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function writeKey(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function removeKey(k) { try { localStorage.removeItem(k); } catch (e) {} }
  function normalise(value) {
    if (typeof value !== "string") return null;
    if (Object.prototype.hasOwnProperty.call(LEGACY_THEMES, value)) value = LEGACY_THEMES[value];
    return CHOICES.indexOf(value) >= 0 ? value : null;
  }

  // ---- Configured choice (captured before any client write) -------------
  var configChoice = null;
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
      var choice = raw && normalise(raw.theme);
      if (choice) configChoice = choice;
    }
    applyConfig(readConfig("okf-theme-config"));
    applyConfig(readConfig("okf-studio-bootstrap"));
  })();

  // ---- User choice (one key; retired keys migrated once) ----------------
  // An unrecognised stored value is ignored (falls back to configured/
  // default) but RETAINED: its presence is the migration gate, so a stale
  // retired key can never be re-read as authority.
  var rawChoice = readKey(KEY);
  var userChoice = normalise(rawChoice);
  if (rawChoice === null) {
    var migrated = null;
    var oldFamily = readKey(OLD_FAMILY_KEY);
    var oldMode = readKey(OLD_MODE_KEY);
    if (oldMode === "auto") migrated = "system";
    else if (oldMode === "light" || oldMode === "dark") {
      migrated = normalise((oldFamily || "technical") + "-" + oldMode);
    } else if (["technical", "swiss", "coffee", "purple"].indexOf(oldFamily) >= 0 && oldMode === null) {
      // A family without an explicit mode was "family + follow OS".
      migrated = "system";
    } else {
      migrated = normalise(readKey(OLD_THEME_KEY));
    }
    if (migrated) {
      // Module state first: if storage became write-only the page still
      // resolves the migrated preference.
      userChoice = migrated;
      writeKey(KEY, migrated);
    }
  }

  // ---- Resolution (derived only; never persisted) ------------------------
  function effectiveChoice() { return userChoice || configChoice || DEFAULT_CHOICE; }
  function osDark() {
    return !!(window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);
  }
  function resolvedTheme() {
    var choice = effectiveChoice();
    if (choice === "system") return osDark() ? SYSTEM_DARK : SYSTEM_LIGHT;
    return choice;
  }
  function schemeOf(theme) { return SCHEMES[theme] || "light"; }

  // data-theme may be a concrete first-paint value emitted by Python. It is a
  // derived paint hint, never configured/user authority.
  var appliedTheme = PALETTES.indexOf(root.getAttribute("data-theme")) >= 0 ?
    root.getAttribute("data-theme") : null;
  var appliedChoice = null;
  var generation = 0;
  function apply() {
    var next = resolvedTheme();
    var choice = effectiveChoice();
    var prev = appliedTheme;
    var prevChoice = appliedChoice;
    root.setAttribute("data-theme", next);
    appliedChoice = choice;
    reflectMenu();
    if (next === prev) {
      // Palette unchanged (for example Bone -> System on a light OS): the
      // menu reflects the new choice, but no visual-state event fires.
      return;
    }
    appliedTheme = next;
    generation += 1;
    dispatch({
      theme: next,
      previousTheme: prev,
      previous: prev,
      choice: choice,
      previousChoice: prevChoice,
      scheme: schemeOf(next),
      previousScheme: prev ? schemeOf(prev) : null,
      resolvedMode: schemeOf(next),
      previousResolvedMode: prev ? schemeOf(prev) : null,
      generation: generation,
    });
  }

  function dispatch(detail) {
    try {
      window.dispatchEvent(new CustomEvent("okf-loom:themeChanged", { detail: detail }));
    } catch (e) {
      // CustomEvent may be unavailable in very old engines; the palette
      // still applies via the data-theme attribute.
    }
  }

  // ---- Preference setters (the only writers) -----------------------------
  function setTheme(choice) {
    choice = normalise(choice);
    if (!choice) return;
    userChoice = choice;
    writeKey(KEY, choice);
    apply();
  }
  function resetPreferences() {
    userChoice = null;
    // Do not resurrect an already-migrated preference on the next boot.
    removeKey(OLD_FAMILY_KEY);
    removeKey(OLD_MODE_KEY);
    removeKey(OLD_THEME_KEY);
    removeKey(KEY);
    apply();
  }

  // ---- Live OS resolution while System ----------------------------------
  var _osMq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function _onOsChange() { if (effectiveChoice() === "system") apply(); }
  if (_osMq) {
    if (_osMq.addEventListener) _osMq.addEventListener("change", _onOsChange);
    else if (_osMq.addListener) _osMq.addListener(_onOsChange);
  }

  // Cross-tab storage changes are external inputs, never a second writer.
  function _onStorage(e) {
    if (!e || (e.key !== KEY && e.key !== null)) return;
    try { if (e.storageArea && e.storageArea !== localStorage) return; } catch (err) { return; }
    userChoice = e.key === null ? null : normalise(e.newValue);
    apply();
  }
  if (window.addEventListener) window.addEventListener("storage", _onStorage);

  // ---- Appearance popover -----------------------------------------------
  // Server-rendered by render.py:_appearance_control_html on wiki, graph,
  // studio and single-file pages; wired here once. The popover is a labelled
  // dialog form holding ONE native radio group. Native radios already give
  // Tab-enters-once and Arrow-selects; this adds Home/End, keeps focus on
  // the chosen option and reflects the resolved state (the server cannot
  // read localStorage).
  var trigger = null, menu = null;
  var _menuOverlay = null;

  function reflectMenu() {
    var choice = effectiveChoice();
    if (trigger) {
      var triggerFamily = trigger.querySelector(".okf-appearance-trigger__family");
      if (triggerFamily) triggerFamily.textContent = LABELS[choice] || LABELS[DEFAULT_CHOICE];
      // aria-labelledby/visible text stays the accessible name (WCAG 2.5.3).
      trigger.removeAttribute("aria-label");
    }
    if (!menu) return;
    var opts = menu.querySelectorAll("input[data-okf-set]"), i, o;
    for (i = 0; i < opts.length; i++) {
      o = opts[i];
      var isChecked = o.getAttribute("data-okf-val") === choice;
      o.checked = isChecked;
      o.setAttribute("tabindex", isChecked ? "0" : "-1");
    }
    if (!menu.hidden) positionMenu();
  }

  function _options() {
    return menu ? Array.prototype.slice.call(menu.querySelectorAll("input[data-okf-set]")) : [];
  }

  function _focusOpt(control) {
    try { if (control && control.focus) control.focus(); } catch (e) {}
  }

  function _selectOpt(control) {
    if (!control) return;
    setTheme(control.getAttribute("data-okf-val"));
    _focusOpt(control);
  }

  function positionMenu() {
    if (!menu || !trigger || menu.hidden) return;
    var previousScroll = menu.scrollTop;
    menu.style.width = "min(19.5rem, 100vw - 16px)";
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
    var top = r.bottom - 1;
    if (top + mh > vh - 8) top = r.top - mh + 1;
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
    _focusOpt(menu.querySelector("input[data-okf-set]:checked") ||
              menu.querySelector("input[data-okf-set]"));
  }

  function closeMenu(restoreFocus) {
    if (!menu || menu.hidden) return;
    if (_menuOverlay) window.OKFOverlayStack.remove(_menuOverlay);
    menu.hidden = true;
    menu.style.left = ""; menu.style.top = "";
    menu.style.maxHeight = ""; menu.style.overflowY = ""; menu.style.width = "";
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    // Only Escape/overlay-stack dismissal restores focus. When a higher
    // registered overlay is now topmost (e.g. an aria-modal lightbox opened
    // above this menu), focus moves INTO that overlay via its focusIn(),
    // never to this trigger behind the modal.
    if (restoreFocus) {
      var top = window.OKFOverlayStack ? window.OKFOverlayStack.top() : null;
      if (top && typeof top.focusIn === "function") {
        try { top.focusIn(); } catch (e) {}
      } else if (trigger) {
        _focusOpt(trigger);
      }
    }
  }

  // The menu yields Escape (and focus restoration) to whatever overlay is
  // actually above it on the shared stack.
  function _isTopmost() {
    var top = window.OKFOverlayStack ? window.OKFOverlayStack.top() : null;
    return !top || top === _menuOverlay;
  }

  function _onKeyDown(e) {
    if (!menu || menu.hidden) {
      if (e.key === "Escape" && _menuOverlay && window.OKFOverlayStack.top() === _menuOverlay) {
        // Defensive: menu marked open on the stack but DOM says hidden.
        e.preventDefault(); e.stopPropagation(); closeMenu(true);
      }
      return;
    }
    if (e.key === "Escape") {
      if (!_isTopmost()) return;
      e.preventDefault(); e.stopPropagation(); closeMenu(true); return;
    }
    var opt = e.target.closest && e.target.closest("input[data-okf-set]");
    if (!opt) return;
    var peers = _options();
    var idx = peers.indexOf(opt);
    // Arrow keys are handled here (not left to the browser) so selection,
    // focus, wrap-around and the single owner write stay deterministic.
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
    // the menu). ArrowDown opens straight onto the checked option.
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

  function _onMenuChange(e) {
    // Native radio activation (click on the label, Space) lands here.
    var opt = e.target && e.target.matches && e.target.matches("input[data-okf-set]") ? e.target : null;
    if (opt && opt.checked) _selectOpt(opt);
  }

  function _onTriggerClick() {
    if (menu.hidden) openMenu(); else closeMenu(true);
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
    // A press on a non-focusable part of the open dialog (legend, padding)
    // must not blur to <body> and close the menu under the pointer: as a
    // programmatic focus target (tabindex=-1) the dialog keeps that focus.
    if (!menu.hasAttribute("tabindex")) menu.setAttribute("tabindex", "-1");
    reflectMenu();
    // Capture phase for Escape so a background handler cannot also react.
    document.addEventListener("keydown", _onKeyDown, true);
    trigger.addEventListener("keydown", _onTriggerKeyDown);
    trigger.addEventListener("click", _onTriggerClick);
    menu.addEventListener("change", _onMenuChange);
    menu.addEventListener("submit", function (e) { e.preventDefault(); });
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

  // ---- Resolve the initial palette before first paint --------------------
  apply();

  // ---- Public API (frozen) ----------------------------------------------
  window.OKFLoomTheme = Object.freeze({
    PALETTES: PALETTES.slice(),
    CHOICES: CHOICES.slice(),
    THEMES: PALETTES.slice(),
    LEGACY_THEMES: Object.assign({}, LEGACY_THEMES),
    STORAGE_KEY: KEY,
    getChoice: function () { return effectiveChoice(); },
    getResolvedTheme: function () { return resolvedTheme(); },
    getScheme: function () { return schemeOf(resolvedTheme()); },
    getResolvedMode: function () { return schemeOf(resolvedTheme()); },
    getGeneration: function () { return generation; },
    setTheme: setTheme,
    reset: resetPreferences,
    on: function (name, fn) {
      if (name !== "change" || typeof fn !== "function") return function () {};
      var h = function (e) { try { fn(e.detail); } catch (err) {} };
      window.addEventListener("okf-loom:themeChanged", h);
      return function () { window.removeEventListener("okf-loom:themeChanged", h); };
    },
  });

  // ---- Successful-initialization signal (no-JS / failed-init safety) -----
  // wiki.css keeps the Appearance trigger (#okf-theme) hidden until <html>
  // carries this class, so the control is inert when theme.js is blocked or
  // throws before reaching here. Placed LAST so any throw above prevents it.
  try { root.classList.add("okf-theme-ready"); } catch (e) {}
}());
