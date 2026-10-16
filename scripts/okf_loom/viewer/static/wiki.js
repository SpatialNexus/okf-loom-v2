/* OKF wiki viewer - client-side enhancements for the served/built pages.
 *
 * Two concerns (theme state was REMOVED — it now lives in viewer/static/
 * theme.js, the sole owner, loaded before this file):
 *   1. Search-as-you-type on the topbar search box (debounced; hits /__search
 *      and renders results inline OR navigates on Enter). Disabled in static
 *      builds (no /__search backend) - a notice replaces live results (P2-65).
 *   2. Hover/focus popover preview for internal links: fetches /__raw/<id> on
 *      hover OR keyboard focus, shows title + description + first lines.
 *      role="tooltip" + aria-describedby linkage (P2-70).
 *
 * Loaded via <script defer>. No CDN deps. Degrades gracefully if a route
 * is missing (e.g. on a static site without /__search).
 */
(function () {
  "use strict";
  document.body.setAttribute("data-okf-js", "1");

  // Route focus is opt-in and provenance-bound: only a normal activation of
  // a primary-shell destination records intent for the next document. Local
  // disclosures, filters, view toggles, hash links, reloads, and direct entry
  // therefore never steal focus.
  var ROUTE_FOCUS_KEY = "okf-shell-route-focus-v1";
  var ROUTE_FOCUS_WINDOW_PREFIX = ROUTE_FOCUS_KEY + ":";
  var routeHeading = document.querySelector(
    ".okf-index__title, .okf-page__title, .okf-search__title, .okf-graph-route-title"
  );
  if (routeHeading) {
    routeHeading.setAttribute("data-okf-route-heading", "");
    routeHeading.setAttribute("tabindex", "-1");
  }
  var shellDestinations = document.querySelectorAll(
    '[data-okf-shell-item="search"], [data-okf-shell-item="index"], [data-okf-shell-item="graph"], .okf-shell__search-form'
  );
  function rememberRouteFocus(rawTarget) {
    try {
      var target = new URL(rawTarget, window.location.href);
      if (target.origin !== window.location.origin || target.hash) return;
      sessionStorage.setItem(ROUTE_FOCUS_KEY, target.pathname + target.search);
    } catch (error) {
      // Browsing-context fallback for storage-denied environments. It carries
      // only the non-secret destination and is cleared by the next document.
      try { window.name = ROUTE_FOCUS_WINDOW_PREFIX + target.pathname + target.search; } catch (nameError) {}
    }
  }
  Array.prototype.forEach.call(shellDestinations, function (control) {
    if (control.tagName === "FORM") {
      control.addEventListener("submit", function () {
        var target = new URL(control.action, window.location.href);
        var data = new FormData(control);
        data.forEach(function (value, key) { target.searchParams.set(key, String(value)); });
        rememberRouteFocus(target.href);
      });
      return;
    }
    control.addEventListener("click", function (event) {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey ||
          event.shiftKey || event.altKey || control.target === "_blank") return;
      var link = control.matches("a[href]") ? control : control.querySelector("a[href]");
      if (link) rememberRouteFocus(link.href);
    });
  });
  var focusRouteHeading = function () {
    if (routeHeading && routeHeading.isConnected) routeHeading.focus({ preventScroll: true });
  };
  var routeStorageDenied = false;
  try {
    var expectedRoute = sessionStorage.getItem(ROUTE_FOCUS_KEY);
    var currentRoute = window.location.pathname + window.location.search;
    if (expectedRoute === currentRoute) {
      sessionStorage.removeItem(ROUTE_FOCUS_KEY);
      window.requestAnimationFrame(focusRouteHeading);
      // Deferred graph/runtime modules finish before load and may otherwise
      // replace focus while creating their controls. Reassert the explicit
      // shell-navigation target at the document-ready boundary.
      window.addEventListener("load", focusRouteHeading, { once: true });
    } else if (expectedRoute) {
      sessionStorage.removeItem(ROUTE_FOCUS_KEY);
    }
  } catch (error) { routeStorageDenied = true; }
  // When sessionStorage is denied, same-origin navigation provenance remains
  // available through the navigation entry + referrer. Never infer intent for
  // reload/back-forward/direct entry, which would steal reading focus.
  if (routeStorageDenied) {
    try {
      var windowRoute = window.name && window.name.indexOf(ROUTE_FOCUS_WINDOW_PREFIX) === 0
        ? window.name.slice(ROUTE_FOCUS_WINDOW_PREFIX.length) : null;
      if (windowRoute === location.pathname + location.search) {
        window.name = "";
        window.requestAnimationFrame(focusRouteHeading);
        window.addEventListener("load", focusRouteHeading, { once: true });
      }
      var navigation = performance.getEntriesByType("navigation")[0];
      var referrer = document.referrer ? new URL(document.referrer) : null;
      if (navigation && navigation.type === "navigate" && referrer &&
          referrer.origin === location.origin &&
          referrer.pathname + referrer.search !== location.pathname + location.search) {
        window.requestAnimationFrame(focusRouteHeading);
        window.addEventListener("load", focusRouteHeading, { once: true });
      }
    } catch (error) { /* browser-native document focus remains */ }
  }

  var TEST_DEADLINES = window.OKF_LOOM_TEST_DEADLINES || {};
  var SUGGESTION_DEADLINE = TEST_DEADLINES.suggestions || 8000;
  var CONTEXT_DEADLINE = TEST_DEADLINES.context || 30000;
  var CLIPBOARD_DEADLINE = TEST_DEADLINES.clipboard || 5000;
  var REDUCED_MOTION = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Theme state (family/mode/Auto/OS, persistence, the Appearance popover)
  // is owned ENTIRELY by theme.js. This file no longer reads or writes
  // localStorage for theme and never sets data-theme. The #okf-theme
  // Appearance control and #okf-appearance-menu are wired by theme.js.

  // ---- Static-mode detection (P2-65) -----------------------------------
  // The server emits data-okf-enhance="1" for spa/serve and "0" for static
  // builds. Static builds have no /__search backend; live results would
  // silently 404. Detect once and show a notice instead of live search.
  var isStatic = document.body.getAttribute("data-okf-enhance") === "0";
  // The live link remains a truthful native "Open" fallback without JS; once
  // the copy enhancement owns activation, its visible action label matches it.
  if (!isStatic) {
    Array.prototype.forEach.call(document.querySelectorAll("a.okf-context-copy[data-context-concept]"), function (control) {
      control.dataset.copyLabel = "Copy concept context";
      control.textContent = "Copy concept context";
    });
  }

  // ---- Recency: relative time text (Ledger) ------------------------------
  // One format for every "Recently changed" row, shared with render.py
  // (_recent_time_text / _recent_time_title) and published for the live
  // Studio rail as window.OKFLoomRecency:
  //   text  = "just now" (< 1 min), else whole elapsed units rounded DOWN:
  //           "Nm ago" (< 1 h), "Nh ago" (< 1 day), "Nd ago"; future
  //           instants (clock skew) read "just now"; unparseable "" (callers
  //           keep the raw record).
  //   title = the instant in UTC as "YYYY-MM-DD HH:MM UTC".
  // The <time datetime> attribute keeps the authored instant. Server text is
  // relative to the render; this re-renders it against the reader's clock
  // (static builds never show stale build-time phrasing once scripts run).
  function okfRelativeTime(ts, now) {
    var then = Date.parse(ts);
    if (typeof ts !== "string" || isNaN(then)) return "";
    var reference = typeof now === "number" ? now : Date.now();
    var seconds = Math.max(0, (reference - then) / 1000);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
    return Math.floor(seconds / 86400) + "d ago";
  }
  function okfUtcTitle(ts) {
    var then = Date.parse(ts);
    if (typeof ts !== "string" || isNaN(then)) return "";
    return new Date(then).toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }
  function refreshRecencyTimes() {
    var now = Date.now();
    Array.prototype.forEach.call(
      document.querySelectorAll("time.okf-recent__time[datetime]"),
      function (node) {
        var ts = node.getAttribute("datetime");
        var text = okfRelativeTime(ts, now);
        if (!text) return;
        if (node.textContent !== text) node.textContent = text;
        var title = okfUtcTitle(ts);
        if (title && node.getAttribute("title") !== title) node.setAttribute("title", title);
      }
    );
  }
  try {
    window.OKFLoomRecency = Object.freeze({
      relativeTime: okfRelativeTime,
      utcTitle: okfUtcTitle,
      refresh: refreshRecencyTimes
    });
  } catch (e) { /* frozen/locked window: the page still refreshes itself */ }
  refreshRecencyTimes();
  if (document.querySelector(".okf-viewer--index")) {
    window.setInterval(refreshRecencyTimes, 60000);
  }

  // ---- Static/live concept-URL parity (P1 recent-pages navigation) -----
  // Mirrors the established P1-9 convention in renderLocalGraph below: the
  // server emits data-okf-portable-root on <body> = the relative path from
  // THIS concept's file back to the bundle root ("./" or "../../" for static
  // nested concepts; "/" for serve/spa; absent on root-level index/search
  // pages where "" correctly resolves a root-relative cid). Static concept
  // pages live at ``<rootPrefix><id>.html``; spa/serve live at ``/<id>``.
  //
  // The search combobox + recent-pages options use this so a recent entry
  // navigates to the correct .html page in a STATIC build and the correct
  // extensionless route in serve/spa. The source is renderer-provided
  // metadata ONLY — this never guesses or hardcodes an external base path.
  var rootPrefix = document.body.getAttribute("data-okf-portable-root") || "";
  function conceptHref(cid) {
    if (!cid) return "";
    return isStatic ? rootPrefix + cid + ".html" : "/" + cid;
  }

  // ---- Local graph widget (concept page sidebar) -----------------------
  // P2-62: a vertical list of 1-hop neighbour titles, each with a neutral
  // square marker + truncated label (Ledger: types are not colour-coded).
  // Keyboard accessible.
  // The data is embedded by the server in <div id="okf-local-graph" data-graph="...">.
  function renderLocalGraph() {
    var container = document.getElementById("okf-local-graph");
    if (!container) return;
    var raw = container.getAttribute("data-graph");
    if (!raw) return;
    var data;
    try { data = JSON.parse(raw); } catch (e) { return; }
    if (!data.nodes || !data.nodes.length) { container.style.display = "none"; return; }

    // P1-9: build neighbour URLs from a root-prefix + mode, embedded by
    // the server. Static concepts live at ``<rootPrefix><id>.html`` (e.g.
    // ``../tables/orders.html``); spa/serve concepts live at ``/<id>``.
    // Falls back to "/" + id when no prefix is embedded so the function
    // still works for older bundles / partial templates.
    var rootPrefix = container.getAttribute("data-root-prefix") || "";

    var nodes = data.nodes;
    var center = nodes.find(function (n) { return n.is_center; }) || nodes[0];
    var neighbors = nodes.filter(function (n) { return n !== center; });

    container.innerHTML = "";

    var title = document.createElement("p");
    title.className = "okf-local-graph__title";
    // P2-4 (iter-1): the widget is a flat labelled pill list of related
    // neighbours, not a graph. The title and the aside aria-label both say
    // "Related" so the affordance matches what the user sees. (The class
    // name .okf-local-graph is kept to avoid churning the CSS; only the
    // user-facing label was dishonest.)
    title.textContent = neighbors.length
      ? "Related (" + neighbors.length + ")"
      : "Related";
    container.appendChild(title);

    if (!neighbors.length) {
      var empty = document.createElement("p");
      empty.className = "okf-local-graph__empty";
      empty.textContent = "No linked concepts.";
      container.appendChild(empty);
      return;
    }

    neighbors.forEach(function (n) {
      var label = n.label || n.id;
      // P3-4: don't JS-truncate the label - `.okf-local-graph__label`
      // already ellipsizes via CSS text-overflow. Slicing on a character
      // boundary split surrogate pairs (emoji) and CJK clusters.
      // Use a <button> so it's keyboard-focusable and clickable uniformly.
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "okf-local-graph__node";
      btn.setAttribute("data-target", n.id);
      btn.setAttribute("title", label);   // full label on hover (native tooltip)
      btn.setAttribute("aria-label", "Open " + label);

      var swatch = document.createElement("span");
      swatch.className = "okf-local-graph__swatch";
      swatch.setAttribute("aria-hidden", "true");
      btn.appendChild(swatch);

      var lbl = document.createElement("span");
      lbl.className = "okf-local-graph__label";
      lbl.textContent = label;
      btn.appendChild(lbl);

      var navigate = function (event) {
        event.preventDefault();
        // P1-9: static builds use <rootPrefix><id>.html; spa/serve use /<id>.
        var url = isStatic
          ? rootPrefix + n.id + ".html"
          : "/" + n.id;
        window.location.href = url;
      };
      btn.addEventListener("click", navigate);
      container.appendChild(btn);
    });
  }

  // ---- Search-as-you-type ----------------------------------------------
  function debounce(fn, ms) {
    var t = null;
    return function () {
      var ctx = this, args = arguments;
      if (t) clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, ms);
    };
  }

  var workbench = document.querySelector(".okf-search-workbench");
  if (workbench) {
    function syncWorkbenchMode() {
      var selected = workbench.querySelector('[name="mode"]:checked');
      var value = selected ? selected.value : "lexical";
      Array.prototype.forEach.call(workbench.querySelectorAll("[data-mode-field]"), function (wrap) {
        var active = wrap.getAttribute("data-mode-field") === value;
        wrap.hidden = !active;
        Array.prototype.forEach.call(wrap.querySelectorAll("input,select"), function (control) { control.disabled = !active; });
      });
    }
    workbench.addEventListener("change", function (event) { if (event.target && event.target.name === "mode") syncWorkbenchMode(); });
    // Omit unused fields in the submission, not in reader-owned controls.
    // This preserves filters on Back/BFCache and while switching modes.
    workbench.addEventListener("formdata", function (event) {
      var mode = event.formData.get("mode") || "lexical";
      ["type", "tag", "entity", "relation", "source", "target"].forEach(function (key) {
        if (!event.formData.get(key) || (key === "entity" && mode !== "entity") ||
            (["relation", "source", "target"].indexOf(key) !== -1 && mode !== "relation")) event.formData.delete(key);
      });
    });
    if (document.body.dataset.okfMode === "serve") workbench.addEventListener("submit", function (event) {
      event.preventDefault();
      syncWorkbenchMode();
      var params = new URLSearchParams(new FormData(workbench));
      // Canonical concept filters use literal path separators; native forms
      // without JS go through the same presentation adapter on the server.
      location.assign("/__search?" + params.toString().replace(/%2F/gi, "/"));
    });
    syncWorkbenchMode();
    var firstInvalid = workbench.querySelector('[aria-invalid="true"]');
    if (firstInvalid) window.setTimeout(function () { if (firstInvalid.isConnected) firstInvalid.focus(); }, 0);
  }

  var searchInputs = document.querySelectorAll('.okf-topbar input[type="search"][name="q"]');
  searchInputs.forEach(function (input) {
    var form = input.form;
    if (!form) return;

    // Static builds serve a client-side searcher (static-search.js on the
    // search page). NO static page has a /__search backend for live
    // (typed-query) suggestions — including the static search page, whose
    // topbar field is a separate control from the workbench searcher. The
    // topbar form itself is the honest route on every static page: its
    // visible labelled Search submit (or Enter) navigates natively to the
    // dedicated search page, which carries the full client-side searcher
    // over __data/search.json.
    //
    // P1 static recent-pages parity: staticSubmitOnly disables ONLY live
    // (typed-query) suggestions — it must NOT disable the recent-pages
    // affordance, which is a localStorage-only nicety that needs NO
    // /__search backend. So we fall through and wire the full combobox; the
    // typed-query debounce AND the bfcache-restored replay below are both
    // gated on staticSubmitOnly so a static build never fires a doomed
    // /__search fetch, while empty-query/focus recent pages keep working.
    // (The former search-page exclusion made the static search page's own
    // topbar field announce "Suggestions unavailable" on every typed
    // query.) Without this wiring, a static concept page would ship a dead
    // search affordance AND no recent-page navigation at all.
    var staticSubmitOnly = isStatic;

    // Complete editable combobox. The input keeps native editing shortcuts;
    // only the composite's arrows/Enter/Escape are owned here.
    var live = document.createElement("div");
    live.className = "okf-search-live";
    live.setAttribute("role", "listbox");
    live.setAttribute("aria-label", "Search suggestions");
    live.hidden = true;
    live.id = "okf-search-suggestions-" + Array.prototype.indexOf.call(searchInputs, input);
    input.parentNode.appendChild(live);
    var status = document.createElement("span");
    status.className = "okf-visually-hidden";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    input.parentNode.appendChild(status);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", live.id);
      var active = -1, options = [], generation = 0, controller = null, debounceTimer = null;

    function closeSuggestions() {
      live.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
      options = [];
    }
    function setActive(next) {
      if (!options.length) return;
      active = (next + options.length) % options.length;
      options.forEach(function (node, idx) {
        node.setAttribute("aria-selected", idx === active ? "true" : "false");
      });
      input.setAttribute("aria-activedescendant", options[active].id);
      options[active].scrollIntoView({ block: "nearest" });
    }
    input._okfSetActive = setActive;

      function dispatchSuggestions(q, own) {
        var localController = new AbortController();
        controller = localController;
          var timeout = setTimeout(function () { localController.abort(); }, SUGGESTION_DEADLINE);
        var url = "/__search?q=" + encodeURIComponent(q) + "&format=json&limit=8";
        fetch(url, { headers: { "Accept": "application/json" }, signal: localController.signal })
        .then(function (r) { if (!r.ok) throw new Error("unavailable"); return r.json(); })
        .then(function (items) {
          if (own !== generation) return;
          renderLive(live, items, input, q);
            options = Array.prototype.slice.call(live.querySelectorAll('[role="option"]'));
            active = -1;
            input.setAttribute("aria-expanded", options.length ? "true" : "false");
              var directCount = live.querySelectorAll('[role="option"]:not(.okf-search-live__all)').length;
              status.textContent = directCount ? directCount + (directCount===1?" direct suggestion. ":" direct suggestions. ")+"View all results is also available." : "No direct suggestions. View all results is available.";
        })
            .catch(function (error) {
              if (own !== generation) return;
                live.textContent = "";
                closeSuggestions();
                status.textContent = "Suggestions unavailable. Submit the search form for full results.";
            })
          .finally(function () { clearTimeout(timeout); });
      }

        input.addEventListener("input", function () {
        generation++;
        if (controller) controller.abort();
        if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
        closeSuggestions(); live.textContent = "";
        var q = input.value.trim(), own = generation;
        if (!q) {
          // Empty query: show recent pages (never a dead empty affordance).
          renderRecent(live, input);
          options = Array.prototype.slice.call(live.querySelectorAll('[role="option"]'));
          active = -1;
          input.setAttribute("aria-expanded", options.length ? "true" : "false");
          return;
        }
        if (q.length < 2) return;
        // P1 static parity: a static build has no /__search backend, so a
        // typed query never fires live suggestions (the labelled Search
        // submit / Enter already routes to the dedicated search page,
        // __search.html, natively). Recent pages (empty query, above) is a
        // localStorage-only nicety and stays wired in static mode.
        if (staticSubmitOnly) return;
          debounceTimer = setTimeout(function () { debounceTimer = null; dispatchSuggestions(q, own); }, 180);
        });
        // Focus on an empty field surfaces recent pages too (plan §3.4:
        // "recent pages when the query is empty").
        input.addEventListener("focus", function () {
          if (input.value.trim()) return;
          if (live.hidden || !live.querySelectorAll('[role="option"]').length) {
            renderRecent(live, input);
            options = Array.prototype.slice.call(live.querySelectorAll('[role="option"]'));
            active = -1;
            input.setAttribute("aria-expanded", options.length ? "true" : "false");
          }
        });
        window.addEventListener("pagehide", function () { generation++; if(controller)controller.abort(); controller=null; if(debounceTimer)clearTimeout(debounceTimer); debounceTimer=null; closeSuggestions(); live.textContent=""; });
        window.addEventListener("pageshow", function (event) { if(!event.persisted)return; generation++; var q=input.value.trim(),own=generation; if(!staticSubmitOnly && q.length>=2)dispatchSuggestions(q,own); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" && options.length) { e.preventDefault(); setActive(active + 1); }
      else if (e.key === "ArrowUp" && options.length) { e.preventDefault(); setActive(active - 1); }
      else if (e.key === "Enter" && active >= 0 && options[active]) {
        e.preventDefault(); window.location.href = options[active].getAttribute("data-href");
        } else if (e.key === "Escape" && (!live.hidden || input.hasAttribute("aria-activedescendant"))) { e.preventDefault(); closeSuggestions(); }
      else if (e.key === "Tab") closeSuggestions();
    });
    document.addEventListener("click", function (e) {
      if (!input.parentNode.contains(e.target)) closeSuggestions();
    });
  });

  // ---- Phase 2 search polish (plan §3.4): type identity, safe highlight,
  // grouping, and recent pages ------------------------------------------
  // Recent-pages store (localStorage). Survives reloads; capped to 8; the
  // newest visit wins. Used to populate the live-suggest box on an EMPTY
  // query / focus so the box is never a dead empty affordance. Safe:
  // values are only ever rendered via textContent / getAttribute into
  // option nodes built with createElement (never innerHTML), so a poisoned
  // title in localStorage cannot inject markup.
  var RECENT_KEY = "okf-loom-recent-pages";
  var RECENT_MAX = 8;
  function loadRecent() {
    try {
      var raw = localStorage.getItem(RECENT_KEY);
      if (!raw) return [];
      var arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr.filter(function (e) {
        return e && typeof e.id === "string" && typeof e.title === "string";
      }).slice(0, RECENT_MAX) : [];
    } catch (e) { return []; }
  }
  function saveRecent(list) {
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX))); }
    catch (e) { /* private mode / quota — recent pages is a nicety, not critical */ }
  }
  function recordRecent(id, title, type, iconKey) {
    if (!id || !title) return;
    // type_color stays in the stored shape (always empty) so entries written
    // by older builds and by this one share one schema.
    var entry = { id: id, title: title, type: type || "", type_color: "", icon: iconKey || "" };
    var list = loadRecent().filter(function (e) { return e.id !== id; });
    list.unshift(entry);
    saveRecent(list);
  }
  // Record the current concept page on load (concept pages carry the id on
  // body.dataset.okfConcept; the title is the visible h1).
  (function recordCurrentPage() {
    var cid = document.body.dataset.okfConcept;
    if (!cid) return;
    var h1 = document.querySelector(".okf-page__title");
    var title = h1 ? (h1.textContent || "").trim() : "";
    if (!title) title = cid;
    var iconKey = "";
    var infobox = document.querySelector(".okf-page__infobox");
    if (infobox) {
      // The renderer emits the resolved icon key on the infobox element so
      // the recent-page entry can carry the same icon as a fresh search hit.
      if (infobox.dataset.okfTypeIcon) iconKey = infobox.dataset.okfTypeIcon;
    }
    var type = "";
    var typeEl = document.querySelector(".okf-infobox__type");
    if (typeEl) type = (typeEl.textContent || "").trim();
    recordRecent(cid, title, type, iconKey);
  })();

  // Type chips (search suggestions, recent pages, link popover) carry the
  // type ICON + NAME only. Ledger does not colour-code content types, so a
  // payload's type_color is deliberately ignored and never reaches an inline
  // style; the chip's neutral outlined look is wiki.css.
  // Phase 2 search-chip type-icon parity (plan §3.4): the chip must carry
  // the SAME type icon the rest of the UI uses, not text/colour only. The
  // icon-key → SVG-inner-markup map is shipped once per page by the server
  // via a non-executed <script type="application/json" id="okf-type-icons">
  // block (CSP-safe). The server resolves the icon key for each result via
  // the SAME type_icon_key the renderer uses, so the chip carries EXACT type
  // identity. The SVG is built with the SVG namespace (createElementNS) so it
  // renders as a real <svg>, not an unknown element; the inner markup comes
  // ONLY from the trusted server JSON (never from a result field), so a
  // hostile result cannot inject arbitrary SVG.
  var _TYPE_ICON_PATHS = (function loadTypeIconPaths() {
    var node = document.getElementById("okf-type-icons");
    if (!node) return {};
    try {
      var parsed = JSON.parse(node.textContent || "{}");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (e) { return {}; }
  })();
  var SVG_NS = "http://www.w3.org/2000/svg";
  function makeTypeIconSvg(iconKey) {
    // SAFE: inner markup is sourced ONLY from the trusted server-shipped
    // _TYPE_ICON_PATHS map (a fixed, curated set of 14 path strings); the
    // icon key from the search result selects which entry to use but cannot
    // inject markup. Falls back to the "file" glyph when the key is unknown.
    var key = (typeof iconKey === "string" && _TYPE_ICON_PATHS[iconKey]) ? iconKey : "file";
    var inner = _TYPE_ICON_PATHS[key] || _TYPE_ICON_PATHS.file || "";
    if (!inner) return null;
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "okf-type-icon okf-search-live__type-icon");
    svg.setAttribute("width", "12");
    svg.setAttribute("height", "12");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    // The curated path strings are <path>/<circle>/<rect>/<ellipse> only.
    // Wrap them in a DOMParser so the inner elements are created in the SVG
    // namespace (innerHTML on an SVG element does NOT do this in all browsers,
    // which would render the paths as unknown HTML elements).
    var wrapper = document.createElementNS(SVG_NS, "g");
    var parser = new DOMParser();
    var parsed = parser.parseFromString(
      "<svg xmlns=\"" + SVG_NS + "\">" + inner + "</svg>", "image/svg+xml"
    );
    var root = parsed.documentElement;
    while (root.firstChild) wrapper.appendChild(root.firstChild);
    svg.appendChild(wrapper);
    return svg;
  }
  function makeTypeChip(typeName, iconKey) {
    var chip = document.createElement("span");
    chip.className = "okf-search-live__type";
    var label = typeName || "concept";
    // Prepend the type ICON so the chip reads [icon][TYPE] — the icon is the
    // identity signal; the text is the disambiguator. aria-hidden on the SVG
    // keeps the chip's accessible name as just the type label (no duplicate
    // announcement from the glyph).
    var icon = makeTypeIconSvg(iconKey);
    if (icon) chip.appendChild(icon);
    var labelNode = document.createElement("span");
    labelNode.className = "okf-search-live__type-label";
    labelNode.textContent = label;
    chip.appendChild(labelNode);
    return chip;
  }

  // Safe matched-term highlight: append text to `parent`, wrapping query
  // term matches in <mark>. Built entirely from text nodes + elements (no
  // innerHTML), so a hostile title/snippet can never inject markup. Matches
  // whole query-term words (>=2 chars) case-insensitively, bounded so a
  // tiny term can't mark tens of thousands of spans in a huge passage.
  function highlightPattern(query) {
    var terms = (query || "")
      .toLowerCase()
      .split(/[^0-9a-z]+/i)
      .filter(function (t) { return t.length >= 2; })
      .slice(0, 8);
    if (!terms.length) return null;
    return new RegExp("(" + terms.map(function (t) {
      return t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }).join("|") + ")", "gi");
  }

  function appendHighlighted(parent, text, query) {
    if (!text) return;
    text = String(text);
    var re = highlightPattern(query);
    if (!re) { parent.appendChild(document.createTextNode(text)); return; }
    var last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      var mark = document.createElement("mark");
      mark.textContent = m[0];
      parent.appendChild(mark);
      last = m.index + m[0].length;
      if (re.lastIndex === m.index) re.lastIndex++; // zero-width guard
      if (last >= 4000) break; // bound runaway matches on huge passages
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  // A suggestion's evidence is the passage's EXACT Markdown source span: the
  // search JSON keeps it verbatim because citations and Context Packs depend
  // on it. A reader needs the words, not the syntax, so the dropdown reduces
  // it to what the rendered page shows: links and images collapse to their
  // label, code ticks, emphasis, heading/list/quote markers, fences, table
  // pipes and tag-like HTML drop, and backslash escapes resolve. This is
  // presentation only; safety still comes from appendHighlighted building
  // text nodes (never innerHTML).
  function readableExcerpt(markdown) { return window.okfReadableText(markdown); }

  // Keep the first matched term inside the short evidence line: when the
  // match sits past the visible budget, start the window a little before it
  // (on a word boundary) so the highlight is what the reader sees.
  var EVIDENCE_CHARS = 140;
  function evidenceWindow(text, query) {
    if (text.length <= EVIDENCE_CHARS) return text;
    var start = 0;
    var re = highlightPattern(query);
    var m = re && re.exec(text);
    if (m && m.index + m[0].length > EVIDENCE_CHARS - 16) {
      start = Math.max(0, m.index - 40);
      var space = text.indexOf(" ", start);
      if (space !== -1 && space < m.index) start = space + 1;
    }
    var end = Math.min(text.length, start + EVIDENCE_CHARS);
    if (end < text.length) {
      var cut = text.lastIndexOf(" ", end);
      if (cut > start + EVIDENCE_CHARS * 0.6 && (!m || cut >= m.index + m[0].length)) end = cut;
    }
    return (start > 0 ? "\u2026" : "") + text.slice(start, end).trim() + (end < text.length ? "\u2026" : "");
  }

  function makeOption(container, input, it, query, cidFallback) {
    var cid = it.concept_id || it.id || cidFallback || "";
    var opt = document.createElement("div");
    opt.id = container.id + "-" + container.querySelectorAll('[role="option"]').length;
    opt.setAttribute("role", "option");
    opt.setAttribute("aria-selected", "false");
    // P1 static/live parity: build the concept URL via conceptHref so a
    // static build resolves to <rootPrefix><cid>.html (e.g.
    // ../tables/orders.html) and serve/spa resolves to /<cid>. The prior
    // hard-coded "/" + cid produced extensionless root URLs that 404'd in
    // static output and ignored nested deployment roots.
    opt.setAttribute("data-href", conceptHref(cid));
    opt.className = "okf-search-live__option";
    // Deterministic accessible name so the combobox announces a clean
    // "title, type, id" regardless of the visual chip/title/path layout.
    opt.setAttribute("aria-label", (it.title || cid) + (it.type ? ", " + it.type : "") + (cid ? ", " + cid : ""));
    var head = document.createElement("span");
    head.className = "okf-search-live__head";
    head.appendChild(makeTypeChip(it.type, it.icon));
    var title = document.createElement("span");
    title.className = "okf-search-live__title";
    appendHighlighted(title, it.title || cid, query);
    head.appendChild(title);
    opt.appendChild(head);
    var sub = document.createElement("span");
    sub.className = "okf-search-live__path";
    sub.textContent = cid;
    opt.appendChild(sub);
    var passage = it.evidence && it.evidence.passage;
    var evidenceText = readableExcerpt((passage && passage.excerpt) || it.description || (it.snippets && it.snippets[0]) || "");
    if (evidenceText) {
      var d = document.createElement("div");
      d.className = "okf-search-live__evidence";
      appendHighlighted(d, evidenceWindow(evidenceText, query), query);
      opt.appendChild(d);
    }
    opt.addEventListener("pointerenter", function () {
      var all = Array.prototype.slice.call(container.querySelectorAll('[role="option"]'));
      var idx = all.indexOf(opt);
      if (typeof input._okfSetActive === "function") input._okfSetActive(idx);
    });
    opt.addEventListener("mousedown", function (e) { e.preventDefault(); });
    opt.addEventListener("click", function () { window.location.href = opt.getAttribute("data-href"); });
    return opt;
  }

  function makeGroupHeader(label, modifier) {
    var h = document.createElement("div");
    h.className = "okf-search-live__group" + (modifier ? " okf-search-live__group--" + modifier : "");
    h.textContent = label;
    return h;
  }

  function renderLive(container, items, input, query) {
    container.textContent = "";
    if (!items) items = [];
    var shown = items.slice(0, 8);
    if (!shown.length) {
      var empty = document.createElement("div");
      empty.className = "okf-search-live__empty";
      empty.setAttribute("role", "presentation");
      empty.textContent = "No matching pages";
      container.appendChild(empty);
    }
    // Group by type when there are many results (>6) so the box reads as
    // type-banded sections rather than a flat monoculture; otherwise a flat
    // list (lighter for 1-6 hits). Group headers are dividers, NOT options.
    if (shown.length > 6) {
      var byType = {};
      var order = [];
      shown.forEach(function (it) {
        var key = it.type || "concept";
        if (!byType[key]) { byType[key] = []; order.push(key); }
        byType[key].push(it);
      });
      order.forEach(function (key) {
        container.appendChild(makeGroupHeader(key));
        byType[key].forEach(function (it) {
          container.appendChild(makeOption(container, input, it, query));
        });
      });
    } else {
      shown.forEach(function (it) {
        container.appendChild(makeOption(container, input, it, query));
      });
    }
    var viewAll = document.createElement("div");
    viewAll.id = container.id + "-all";
    viewAll.className = "okf-search-live__option okf-search-live__all";
    viewAll.setAttribute("role", "option");
    viewAll.setAttribute("aria-selected", "false");
    viewAll.setAttribute("data-href", "/__search?q=" + encodeURIComponent(query));
    viewAll.textContent = "View all results";
    viewAll.addEventListener("mousedown", function (e) { e.preventDefault(); });
    viewAll.addEventListener("click", function () { window.location.href = viewAll.getAttribute("data-href"); });
    container.appendChild(viewAll);
    container.hidden = false;
  }

  // Recent-pages panel for an empty query (shown on focus / cleared input).
  // Exclude the current page and stay closed with no other visits. Options are
  // real role=option nodes so the existing combobox keyboard model owns them.
  function renderRecent(container, input) {
    container.textContent = "";
    var recent = loadRecent().filter(function (it) { return it.id !== document.body.dataset.okfConcept; });
    container.hidden = !recent.length;
    if (!recent.length) return;
    container.appendChild(makeGroupHeader("Recent pages", "recent"));
    recent.forEach(function (it) {
      container.appendChild(makeOption(container, input, it, "", it.id));
    });
  }

  // ---- Deterministic Context Pack copy --------------------------------
  var copyGeneration = 0;
  var copyController = null;
  var pageIdentity = window.location.href;

  function copyStatus(message, assertive) {
    var node = document.getElementById("okf-copy-status");
    if (!node) {
      node = document.createElement("p");
      node.id = "okf-copy-status";
      node.className = "okf-visually-hidden";
      node.setAttribute("role", assertive ? "alert" : "status");
      node.setAttribute("aria-live", assertive ? "assertive" : "polite");
      document.body.appendChild(node);
    }
    node.setAttribute("role", assertive ? "alert" : "status");
    node.textContent = message;
  }

    function manualCopy(text, owner) {
      var wrap = document.getElementById("okf-copy-fallback");
      var area = document.getElementById("okf-copy-fallback-text");
      if (!wrap || !area) {
        wrap = document.createElement("div");
        wrap.id = "okf-copy-fallback";
        wrap.className = "okf-copy-fallback";
        wrap.setAttribute("role", "region");
        wrap.setAttribute("aria-labelledby", "okf-copy-fallback-title");
        var title = document.createElement("h3");
        title.id = "okf-copy-fallback-title";
        var source = document.createElement("p"); source.className = "okf-copy-fallback__source";
        var instruction = document.createElement("p"); instruction.className = "okf-copy-fallback__instruction";
          instruction.textContent = "Automatic copy was unavailable. This field contains the exact Markdown, including any fences. Select all text, then use Ctrl/Cmd+C or your device’s Copy action.";
        var label = document.createElement("label"); label.className = "okf-visually-hidden";
          label.setAttribute("for", "okf-copy-fallback-text"); label.textContent = "Exact Markdown context";
        area = document.createElement("textarea");
        area.id = "okf-copy-fallback-text";
        area.readOnly = true;
        area.rows = 4;
        var select = document.createElement("button");
        select.type = "button";
        select.className = "okf-btn";
          select.textContent = "Select all text";
          select.addEventListener("click", function () { area.focus(); area.select(); copyStatus("All exact Markdown selected. Copy it with Ctrl/Cmd+C or your device’s Copy action.", false); });
        wrap.appendChild(title); wrap.appendChild(source); wrap.appendChild(instruction);
        wrap.appendChild(label); wrap.appendChild(area); wrap.appendChild(select);
      }
      var article = owner && owner.closest ? owner.closest(".okf-search-result") : null;
      var ownerTitle = article && article.querySelector("h3") ? article.querySelector("h3").textContent.trim() : "this context";
      var citation = (owner && owner.dataset && owner.dataset.citation) || (article && article.querySelector(".okf-search-citation") ? article.querySelector(".okf-search-citation").textContent.trim() : "");
        var titleNode = wrap.querySelector("#okf-copy-fallback-title");
        var sourceNode = wrap.querySelector(".okf-copy-fallback__source");
        var instructionNode = wrap.querySelector(".okf-copy-fallback__instruction");
        var labelNode = wrap.querySelector('label[for="okf-copy-fallback-text"]');
        var selectNode = wrap.querySelector("[data-select-copy-fallback], button");
      if (titleNode) titleNode.textContent = "Copy manually — " + ownerTitle;
        if (sourceNode) sourceNode.textContent = citation ? "Source: " + ownerTitle + " · " + citation : "Source: " + ownerTitle;
        if (instructionNode) instructionNode.textContent = "Automatic copy was unavailable. This field contains the exact Markdown, including any fences. Select all text, then use Ctrl/Cmd+C or your device’s Copy action.";
        if (labelNode) labelNode.textContent = "Exact Markdown context";
        if (selectNode) selectNode.textContent = "Select all text";
      wrap.dataset.sourceTitle = ownerTitle;
      wrap.dataset.sourceCitation = citation;
      (article || (owner && owner.parentNode) || document.body).appendChild(wrap);
      area.value = text;
      area.rows = Math.max(3, Math.min(8, String(text || "").split("\n").length + 1));
      wrap.hidden = false;
      copyStatus("Automatic copy was unavailable. The exact context is ready for manual copy.", true);
  }

  function clipboardExact(text, owner, own) {
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
      owner.textContent = owner.dataset.copyLabel || "Copy";
      manualCopy(text, owner); return Promise.resolve(false);
    }
    var settled = false;
    return new Promise(function (resolve) {
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        if (own === copyGeneration && window.location.href === pageIdentity) {
          owner.textContent = owner.dataset.copyLabel || "Copy";
          manualCopy(text, owner);
        }
        resolve(false);
        }, CLIPBOARD_DEADLINE);
      Promise.resolve(navigator.clipboard.writeText(text)).then(function () {
        if (settled) return;
        settled = true; clearTimeout(timer);
        if (own === copyGeneration && window.location.href === pageIdentity) {
          owner.textContent = "Copied";
          copyStatus("Copied exact cited context.", false);
          setTimeout(function () { if (own === copyGeneration && owner.isConnected) owner.textContent = owner.dataset.copyLabel || "Copy context"; }, 1800);
        }
        resolve(true);
      }, function () {
        if (settled) return;
        settled = true; clearTimeout(timer);
        if (own === copyGeneration && window.location.href === pageIdentity) {
          owner.textContent = owner.dataset.copyLabel || "Copy";
          manualCopy(text, owner);
        }
        resolve(false);
      });
    });
  }

    function contextUrl(target, options, format) {
    options = options || {};
    var params = new URLSearchParams();
    params.set("format", format || "markdown");
    params.set("mode", options.mode || "hybrid");
    params.set("budget_tokens", String(options.budget || 6000));
    params.set("neighbors", String(options.neighbors == null ? 1 : options.neighbors));
    params.set("limit", String(options.limit || 20));
    ["type", "tag", "entity", "relation", "source", "target"].forEach(function (key) {
      if (options[key]) params.set(key, options[key]);
    });
    if (target.concept && !/^[A-Za-z0-9_][A-Za-z0-9_.-]*(\/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$/.test(target.concept)) {
      throw new Error("Context target is not a canonical concept id.");
    }
      if (target.concept) params.set("concept", target.concept);
      if (target.query) params.set("q", target.query);
      if (target.selection_exact) params.set("selection_exact", target.selection_exact);
      if (target.selection_prefix) params.set("selection_prefix", target.selection_prefix);
      if (target.selection_suffix) params.set("selection_suffix", target.selection_suffix);
      if (target.selection_heading) params.set("selection_heading", target.selection_heading);
    var encoded = params.toString();
    if (target.concept) encoded = encoded.replace("concept=" + encodeURIComponent(target.concept), "concept=" + target.concept);
      return "/__context?" + encoded;
    }

    var portableProviderPromise = null;
    var portableProviderController = null;
    var portableProviderGeneration = 0;
      var portableProviderActive = true;
      var portableProviderPageIdentity = window.location.href;
      var portablePublishedProvider = null;
      function revokePortableProvider() {
        portableProviderGeneration++;
        var staleController=portableProviderController;
        portableProviderPromise=null;portableProviderController=null;
        if(window.okfPortableProvider===portablePublishedProvider)window.okfPortableProvider=null;
        portablePublishedProvider=null;
        if(staleController)staleController.abort();
      }
      function ensurePortableProvider() {
      if (window.okfPortableProvider) return Promise.resolve(window.okfPortableProvider);
      if (!portableProviderActive || window.location.href !== portableProviderPageIdentity) {
        return Promise.reject(new DOMException("Concept page is no longer active.", "AbortError"));
      }
      if (!window.okfPortableRetrieval || !document.body.dataset.okfConcept) {
        return Promise.reject(new Error("Portable evidence is unavailable on this surface."));
      }
      if (!portableProviderPromise) {
        var root = (document.body.dataset.okfPortableRoot || "./") + "__data/";
        var own = ++portableProviderGeneration;
        var identity = portableProviderPageIdentity;
        portableProviderController = new AbortController();
        portableProviderPromise = window.okfPortableRetrieval.loadStatic(root, {
          controller: portableProviderController,
          generation: own
        }).then(function (provider) {
          if (!portableProviderActive || own !== portableProviderGeneration ||
              identity !== portableProviderPageIdentity || window.location.href !== identity) {
            throw new DOMException("Concept page load was superseded.", "AbortError");
          }
          portablePublishedProvider = provider;
          window.okfPortableProvider = provider;
          return provider;
        }).finally(function () {
          if (own === portableProviderGeneration) {
            portableProviderPromise = null;
            portableProviderController = null;
          }
        });
      }
      return portableProviderPromise;
    }
    if (isStatic && document.body.dataset.okfConcept) {
      window.okfStaticContextCopy = function (target, options) {
        return ensurePortableProvider().then(function (provider) {
          return provider.context(target, options || {}, []);
        });
      };
    }

    var contextPageGeneration = 1;
    var contextOwnerSequence = 0;
    var activeContextFingerprint = null;
    var activeContextAction = null;

    function contextOwnerId(owner) {
      if (!owner.dataset.okfContextOwner) owner.dataset.okfContextOwner = "context-owner-" + (++contextOwnerSequence);
      return owner.dataset.okfContextOwner;
    }

    function canonicalSearchState(owner) {
      var form = owner && owner.closest && owner.closest("form") || document.querySelector(".okf-search-workbench");
      var params = new URLSearchParams(window.location.search);
      var modeControl = form && form.querySelector('[name="mode"]:checked');
      var queryControl = form && form.elements && form.elements.q;
      var state = {
        query: queryControl ? queryControl.value.trim() : (params.get("q") || (owner && owner.dataset.contextQuery) || "").trim(),
        options: {
          mode: modeControl ? modeControl.value : params.get("mode") || (owner && owner.dataset.contextMode) || "hybrid",
          budget: Number((document.querySelector("[data-context-budget]") || {}).value || params.get("budget_tokens") || 6000),
          neighbors: Number((document.querySelector("[data-context-neighbors]") || {}).value || params.get("neighbors") || 1),
          limit: Number((document.querySelector("[data-context-limit]") || {}).value || params.get("limit") || 20)
        },
        hasFilters: false
      };
      ["type", "tag", "entity", "relation", "source", "target"].forEach(function (key) {
        var control = form && form.elements && form.elements[key];
        var value = control ? (!control.disabled ? control.value.trim() : "") : (params.get(key) || "").trim();
        if (value) { state.options[key] = value; state.hasFilters = true; }
      });
      return state;
    }
    window.okfReadSearchContextState = canonicalSearchState;

    function contextFingerprint(target, owner, options, ownedRevision) {
      var provider = window.okfPortableProvider;
      var normalizedTarget = {};
      ["concept", "query", "selection_exact", "selection_prefix", "selection_suffix", "selection_heading"].forEach(function (key) {
        normalizedTarget[key] = String(target && target[key] || "").trim();
      });
      var normalizedOptions = {};
      ["mode", "type", "tag", "entity", "relation", "source", "target", "budget", "neighbors", "limit"].forEach(function (key) {
        normalizedOptions[key] = options && options[key] != null ? String(options[key]).trim() : "";
      });
      return JSON.stringify({
        target: normalizedTarget,
        options: normalizedOptions,
        revision: String(ownedRevision != null ? ownedRevision : (provider && provider.manifest && provider.manifest.bundle_revision) || document.body.dataset.okfRevision || "pending"),
        page: pageIdentity,
        pageGeneration: contextPageGeneration,
        owner: contextOwnerId(owner)
      });
    }

    function clearContextFeedback() {
      var error = document.getElementById("okf-context-error"); if (error) error.remove();
      var fallback = document.getElementById("okf-selection-context-fallback"); if (fallback) fallback.remove();
    }

      function revokePreparedContext(reason, invalidateProvider) {
      var preview = document.querySelector("[data-context-preview]"); if (preview) preview.remove();
      window.__okfContextLifecycle = window.__okfContextLifecycle || [];
      window.__okfContextLifecycle.push({ reason: reason, generation: copyGeneration + 1, at: Date.now() });
      if (window.__okfContextLifecycle.length > 50) window.__okfContextLifecycle.shift();
      copyGeneration++;
      activeContextFingerprint = null;
      activeContextAction = null;
        if (copyController) copyController.abort();
        copyController = null;
          if (invalidateProvider) revokePortableProvider();
      document.querySelectorAll("[data-context-prepare], [data-context-copy-prepared], .okf-context-copy").forEach(function (control) {
        control._okfPreparedContext = null;
        control._okfPreparedFingerprint = null;
        control.removeAttribute("aria-busy");
        if (control.dataset.copyLabel) control.textContent = control.dataset.copyLabel;
      });
      document.querySelectorAll("[data-context-copy-prepared]").forEach(function (button) { button.disabled = true; });
      var estimate = document.querySelector("[data-context-estimate]");
      if (estimate) estimate.textContent = estimate.dataset.defaultText || "Choose bounds, then prepare context.";
      clearContextFeedback();
        var manual = document.getElementById("okf-copy-fallback"); if (manual) { manual.hidden = true; delete manual.dataset.sourceTitle; delete manual.dataset.sourceCitation; }
        var manualArea = document.getElementById("okf-copy-fallback-text"); if (manualArea) manualArea.value = "";
        copyStatus("", false);
      return reason;
    }
      window.okfRevokePreparedContext = revokePreparedContext;

      function contextFallback(action) {
        if (!isStatic) return { href: contextUrl(action.target, action.options, "markdown"), label: "Open canonical Markdown context" };
        var article = action.owner && action.owner.closest && action.owner.closest(".okf-search-result");
        var firstResult = document.querySelector(".okf-search-result[data-concept-id]");
        var firstAction = document.querySelector(".okf-search-result .okf-context-copy[data-context-concept]");
        var concept = action.target.concept || (article && article.dataset.conceptId) || (firstResult && firstResult.dataset.conceptId) || (firstAction && firstAction.dataset.contextConcept);
        if (concept) {
          var root = document.body.dataset.okfPortableRoot || "";
          return { href: root + "__context/" + concept + ".txt", label: "Open emitted concept-only Markdown" };
        }
        return { href: document.body.classList.contains("okf-viewer--search") ? "index.html" : (document.body.dataset.okfPortableRoot || "./"), label: "Browse concepts for emitted context" };
      }

    function showContextFailure(owner, action, timedOut) {
      clearContextFeedback();
      var region = document.createElement("div");
      region.id = "okf-context-error"; region.className = "okf-context-error";
      region.setAttribute("role", "alert"); region.setAttribute("aria-labelledby", "okf-context-error-title");
      var title = document.createElement("p"); title.id = "okf-context-error-title";
        var fallbackTarget = contextFallback(action);
        title.textContent = timedOut ? "Context preparation took longer than 30 seconds. Retry to start a fresh request." : "Context could not be prepared. Retry, or use the available fallback.";
        var retry = document.createElement("button"); retry.type = "button"; retry.className = "okf-btn okf-btn--primary"; retry.textContent = "Retry";
        var fallback = document.createElement("a"); fallback.className = "okf-btn okf-btn--quiet"; fallback.textContent = fallbackTarget.label;
        fallback.href = fallbackTarget.href;
      retry.addEventListener("click", function () { requestContextCopy(action.target, action.owner, action.options, action.copyNow, true); });
      region.appendChild(title); region.appendChild(retry); region.appendChild(fallback);
      (owner.closest(".okf-search-context") || owner.parentNode || document.body).appendChild(region);
      copyStatus(title.textContent, true);
      retry.focus();
    }

    function requestContextCopy(target, owner, options, copyNow, retrying) {
      if (!owner) return Promise.resolve(false);
      options = options || {};
      revokePreparedContext("new-context-action", false);
      var own = ++copyGeneration;
      var ownedRevision = String((window.okfPortableProvider && window.okfPortableProvider.manifest && window.okfPortableProvider.manifest.bundle_revision) || document.body.dataset.okfRevision || "pending");
      var fingerprint = contextFingerprint(target, owner, options, ownedRevision);
      var action = { target: target, owner: owner, options: options, copyNow: copyNow, revision: ownedRevision };
      activeContextFingerprint = fingerprint;
      activeContextAction = action;
      owner.dataset.copyLabel = owner.dataset.copyLabel || owner.textContent;
      owner.textContent = "Preparing"; owner.setAttribute("aria-busy", "true");
      copyStatus("Preparing deterministic context.", false);
      var localController = new AbortController(); copyController = localController;
      var started = Date.now(); var timedOut = false;
      var timer;
      var deadline = new Promise(function (_, reject) {
          timer = setTimeout(function () { timedOut = true; revokePortableProvider(); localController.abort(); reject(new DOMException("Context deadline exceeded", "TimeoutError")); }, CONTEXT_DEADLINE);
      });
      var acquisition;
      if (typeof window.okfStaticContextCopy === "function") {
        acquisition = window.okfStaticContextCopy(target, options);
      } else {
        acquisition = fetch(contextUrl(target, options, "json"), { signal: localController.signal, headers: { "Accept": "application/json" } }).then(function (response) {
          if (!response.ok) throw new Error("Context request failed (" + response.status + ")");
          return response.json();
        });
      }
        return Promise.race([Promise.resolve(acquisition), deadline]).then(function (pack) {
        if (Date.now() - started >= CONTEXT_DEADLINE) throw new DOMException("Context deadline exceeded", "TimeoutError");
        if (own !== copyGeneration || fingerprint !== activeContextFingerprint || !owner.isConnected || fingerprint !== contextFingerprint(target, owner, options, action.revision)) {
          throw new DOMException("Context action was superseded", "AbortError");
        }
          if (!pack || pack.schema_version !== 1 || !pack.budget || !Array.isArray(pack.items)) throw new Error("Context response did not match the supported schema.");
          if (action.revision !== "pending" && pack.bundle_revision !== action.revision) {
            var mismatch = new Error("Context revision does not match the rendered bundle."); mismatch.name = "RevisionMismatchError"; throw mismatch;
          }
          return finishPreparedContext(pack, owner, own, copyNow !== false, fingerprint, retrying === true);
        }).catch(function (error) {
          if (own !== copyGeneration || fingerprint !== activeContextFingerprint || error.name === "AbortError" && !timedOut) return false;
          if (error.name === "RevisionMismatchError") revokePreparedContext("bundle-revision-mismatch", true);
          owner.textContent = owner.dataset.copyLabel || "Copy context";
          showContextFailure(owner, action, timedOut || error.name === "TimeoutError");
        return false;
      }).finally(function () {
        clearTimeout(timer);
        if (own === copyGeneration && owner.isConnected) owner.removeAttribute("aria-busy");
      });
    }
  function showSelectionFallback(owner, concept) {
    var existing = document.getElementById("okf-selection-context-fallback"); if (existing) existing.remove();
    var fallback = document.createElement("button"); fallback.type="button"; fallback.id="okf-selection-context-fallback"; fallback.className="okf-btn";
    fallback.textContent="Copy concept context instead";
    fallback.addEventListener("click",function(){requestContextCopy({concept:concept},fallback,{mode:"hybrid"},true);});
    owner.insertAdjacentElement("afterend",fallback);
  }
    function finishPreparedContext(pack, owner, own, copyNow, fingerprint, retrying) {
      if (own !== copyGeneration || fingerprint !== activeContextFingerprint || fingerprint !== contextFingerprint(activeContextAction.target, owner, activeContextAction.options, activeContextAction.revision)) return Promise.resolve(false);
      if (!pack || !pack.representations || typeof pack.representations.markdown !== "string") throw new Error("Context response did not include canonical Markdown.");
      var providerRevision = window.okfPortableProvider && window.okfPortableProvider.manifest && window.okfPortableProvider.manifest.bundle_revision;
      if (providerRevision && pack.bundle_revision !== providerRevision) { revokePreparedContext("provider-revision-mismatch", true); throw new DOMException("Context provider revision changed", "AbortError"); }
    var estimate = document.querySelector("[data-context-estimate]");
    if (estimate) estimate.textContent = pack.budget.estimated_tokens + " estimated tokens, " + pack.included +
      " passages included, " + pack.omitted + " omitted" + (pack.truncated ? "; truncated to the requested bounds." : ".");
    if (!copyNow && estimate) {
      var preview = document.createElement("section");
      preview.dataset.contextPreview = "";
      preview.className = "okf-context-preview";
      preview.tabIndex = 0;
      preview.setAttribute("aria-label", "Selected cited passages");
      var label = document.createElement("p");
      label.textContent = "Selected cited passages for reuse. Copy includes canonical citations and recorded metadata; nothing is executed.";
      preview.appendChild(label);
      (pack.items || []).forEach(function (item) {
        var heading = document.createElement("h3"), citation = document.createElement("p"), excerpt = document.createElement("pre");
        heading.textContent = item.title || item.concept_id;
        citation.textContent = item.citation;
        excerpt.textContent = item.excerpt;
        preview.appendChild(heading); preview.appendChild(citation); preview.appendChild(excerpt);
      });
      estimate.parentNode.appendChild(preview);
    }
      owner._okfPreparedContext = pack;
      owner._okfPreparedFingerprint = fingerprint;
      var prepared = document.querySelector("[data-context-copy-prepared]");
      if (prepared) { prepared.disabled = false; prepared._okfPreparedContext = pack; prepared._okfPreparedFingerprint = fingerprint; }
    owner.textContent = owner.dataset.copyLabel || owner.textContent;
    copyStatus("Prepared " + pack.included + " passages, " + pack.budget.estimated_tokens + " estimated tokens" + (pack.truncated ? ", truncated." : "."), false);
      clearContextFeedback();
      if (!copyNow) { if (retrying && prepared) prepared.focus(); return Promise.resolve(pack); }
    owner.textContent = "Copying";
    return clipboardExact(pack.representations.markdown, owner, own);
  }
    window.okfContextCopy = requestContextCopy;
    window.addEventListener("pagehide", function () {
      contextPageGeneration++;
      revokePreparedContext("pagehide", true);
      portableProviderActive = false;
    portableProviderGeneration++;
    if (portableProviderController) portableProviderController.abort();
    portableProviderPromise = null;
    portableProviderController = null;
    if (window.okfPortableProvider === portablePublishedProvider) window.okfPortableProvider = null;
    portablePublishedProvider = null;
  });
  window.addEventListener("pageshow", function (event) {
      if (!event.persisted) return;
      contextPageGeneration++;
      portableProviderActive = true;
      portableProviderPageIdentity = window.location.href;
    });

    function pageContextOptions(owner) { return canonicalSearchState(owner).options; }

  document.addEventListener("click", function (event) {
    var passageButton = event.target.closest("[data-copy-passage]");
      if (passageButton) {
        revokePreparedContext("cited-passage-copy", false);
        var own = ++copyGeneration;
      passageButton.dataset.copyLabel = passageButton.dataset.copyLabel || passageButton.textContent;
      passageButton.textContent = "Copying";
      clipboardExact((passageButton.dataset.excerpt || "") + "\n\n" + (passageButton.dataset.citation || ""), passageButton, own);
      return;
    }
    var prepare = event.target.closest("[data-context-prepare]");
      if (prepare) {
        var prepareState = canonicalSearchState(prepare);
        requestContextCopy({query:prepareState.query},prepare,prepareState.options,false);
        return;
      }
      var preparedButton = event.target.closest("[data-context-copy-prepared]");
      if (preparedButton && preparedButton._okfPreparedContext) {
        if (!activeContextAction || preparedButton._okfPreparedFingerprint !== activeContextFingerprint || activeContextFingerprint !== contextFingerprint(activeContextAction.target, activeContextAction.owner, activeContextAction.options, activeContextAction.revision)) {
          revokePreparedContext("prepared-context-state-changed", false); return;
        }
        var preparedOwn=++copyGeneration;preparedButton.dataset.copyLabel=preparedButton.dataset.copyLabel||preparedButton.textContent;preparedButton.textContent="Copying";clipboardExact(preparedButton._okfPreparedContext.representations.markdown,preparedButton,preparedOwn);return;
    }
    var button = event.target.closest(".okf-context-copy");
    if (!button) return;
    if (button.tagName === "A") event.preventDefault();
    if (button.dataset.contextMarkdown) {
      var portableOwn = ++copyGeneration;
      button.dataset.copyLabel = button.dataset.copyLabel || button.textContent;
      button.textContent = "Copying";
      clipboardExact(button.dataset.contextMarkdown, button, portableOwn);
      return;
    }
    var budget = document.querySelector("[data-context-budget]");
    var neighbors = document.querySelector("[data-context-neighbors]");
    var limit = document.querySelector("[data-context-limit]");
      var buttonOptions = pageContextOptions(button);
      if (!document.querySelector(".okf-search-workbench")) {
        buttonOptions.mode = button.dataset.contextMode || "hybrid";
        buttonOptions.budget = Number(budget && budget.value || 6000);
        buttonOptions.neighbors = Number(neighbors && neighbors.value || 1);
        buttonOptions.limit = Number(limit && limit.value || 20);
      }
        requestContextCopy({ concept: button.dataset.contextConcept || "", query: button.dataset.contextQuery || "" }, button, buttonOptions);
    });
    function owningContextControl(target) {
      return target && target.matches && target.matches('[name="q"], [name="mode"], [name="type"], [name="tag"], [name="entity"], [name="relation"], [name="source"], [name="target"], [data-context-budget], [data-context-neighbors], [data-context-limit]');
    }
    document.addEventListener("input", function (event) { if (owningContextControl(event.target)) revokePreparedContext("context-owner-input", false); });
    document.addEventListener("change", function (event) { if (owningContextControl(event.target)) revokePreparedContext("context-owner-change", false); });
  var selectFallback = document.querySelector("[data-select-copy-fallback]");
  if (selectFallback) selectFallback.addEventListener("click", function () {
    var area = document.getElementById("okf-copy-fallback-text");
    if (area) { area.focus(); area.select(); copyStatus("All exact Markdown selected. Copy it with Ctrl/Cmd+C or your device’s Copy action.", false); }
  });

  // Context for an exact rendered-text selection. If Studio has mounted its
  // Comment affordance, this action joins it; otherwise static pages get a
  // small adjacent floating action. It never clears or owns the selection.
  var selectionContextButton = null;
      function revokeSelectionContext() {
      if (activeContextAction && activeContextAction.target && activeContextAction.target.selection_exact) {
        revokePreparedContext("selection-cleared", false);
      }
      window.okfSelectionContext = null;
      if (selectionContextButton) selectionContextButton.hidden = true;
    }
    function publishSelectionContext(selectionTarget, range, label) {
      window.okfSelectionContext = selectionTarget;
      if (!selectionContextButton) {
        selectionContextButton = document.createElement("button");
        selectionContextButton.type = "button";
        selectionContextButton.className = "okf-btn okf-selection-context";
        selectionContextButton.textContent = "Copy selection with citation";
        selectionContextButton.addEventListener("mousedown", function (e) { e.preventDefault(); });
        selectionContextButton.addEventListener("click", function () {
          if (window.okfSelectionContext) requestContextCopy(window.okfSelectionContext, selectionContextButton, {}, true);
        });
        document.body.appendChild(selectionContextButton);
      }
      selectionContextButton.dataset.query = label;
      selectionContextButton.hidden = false;
      var commentAffordance = document.querySelector(".okf-comment-afford:not([hidden])");
      if (commentAffordance && selectionContextButton.parentNode !== commentAffordance) {
        commentAffordance.appendChild(selectionContextButton);
        selectionContextButton.classList.add("okf-selection-context--joined");
        selectionContextButton.style.left = "";
        selectionContextButton.style.top = "";
        return;
      }
      if (!commentAffordance && selectionContextButton.parentNode !== document.body) {
        document.body.appendChild(selectionContextButton);
        selectionContextButton.classList.remove("okf-selection-context--joined");
      }
        var rect = range.getBoundingClientRect();
        var rangeElement = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
        var sourceBlock = rangeElement && rangeElement.closest("p,li,td,th,dd,dt,blockquote,pre,h1,h2,h3,h4,h5,h6");
        var anchorRect = sourceBlock ? sourceBlock.getBoundingClientRect() : rect;
        var previousRect = sourceBlock && sourceBlock.previousElementSibling ? sourceBlock.previousElementSibling.getBoundingClientRect() : null;
        var nextRect = sourceBlock && sourceBlock.nextElementSibling ? sourceBlock.nextElementSibling.getBoundingClientRect() : null;
        var buttonRect = selectionContextButton.getBoundingClientRect();
        var clearance = 8;
        var below = anchorRect.bottom + clearance;
        var above = anchorRect.top - buttonRect.height - clearance;
        var belowSafe = below + buttonRect.height <= window.innerHeight - clearance && (!nextRect || below + buttonRect.height + clearance <= nextRect.top);
        var aboveSafe = above >= clearance && (!previousRect || previousRect.bottom + clearance <= above);
        if (!belowSafe && !aboveSafe && sourceBlock) {
          sourceBlock.insertAdjacentElement("afterend", selectionContextButton);
          selectionContextButton.classList.add("okf-selection-context--flow");
          selectionContextButton.dataset.placement = "flow-below";
          selectionContextButton.style.left = "";
          selectionContextButton.style.top = "";
          return;
        }
        if (selectionContextButton.parentNode !== document.body) document.body.appendChild(selectionContextButton);
        selectionContextButton.classList.remove("okf-selection-context--flow");
        var useBelow = belowSafe || !aboveSafe;
        var top = useBelow ? below : above;
        if (top < clearance) top = Math.min(window.innerHeight - buttonRect.height - clearance, below);
        selectionContextButton.dataset.placement = useBelow ? "below" : "above";
        selectionContextButton.style.left = Math.max(clearance, Math.min(rect.left, window.innerWidth - buttonRect.width - clearance)) + "px";
        selectionContextButton.style.top = Math.max(clearance, Math.min(top, window.innerHeight - buttonRect.height - clearance)) + "px";
    }
    var updateSelectionContext = debounce(function () {
    var selection = window.getSelection();
    var body = document.querySelector(".okf-page__body");
    if (!selection || selection.isCollapsed || !selection.rangeCount || !body ||
        !body.contains(selection.anchorNode) || !body.contains(selection.focusNode)) {
      revokeSelectionContext();
      return;
    }
        var selectedText = selection.toString();
        var leadingSpace = (selectedText.match(/^\s+/)||[""])[0];
        var trailingSpace = (selectedText.match(/\s+$/)||[""])[0];
        var exact = selectedText.slice(leadingSpace.length, selectedText.length-trailingSpace.length);
      if (!exact || Array.from(exact).length > 4096) { revokeSelectionContext(); return; }
    var conceptButton = document.querySelector("[data-context-concept]");
    if (!conceptButton) return;
      var range = selection.getRangeAt(0);
      var startElement = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
      var endElement = range.endContainer.nodeType === 1 ? range.endContainer : range.endContainer.parentElement;
      var block = startElement && startElement.closest("p,li,td,th,dd,dt,blockquote,pre,h1,h2,h3,h4,h5,h6");
      var endBlock = endElement && endElement.closest("p,li,td,th,dd,dt,blockquote,pre,h1,h2,h3,h4,h5,h6");
      if (!block || block !== endBlock || !body.contains(block)) {
        revokeSelectionContext();
        copyStatus("Selection context requires text within one source-owned passage.", false);
        return;
      }
    var prefix = "", suffix = "", heading = "";
    if (block) {
      try {
          var before = document.createRange(); before.selectNodeContents(block); before.setEnd(range.startContainer, range.startOffset); prefix = (before.toString()+leadingSpace).slice(-512);
          var after = document.createRange(); after.selectNodeContents(block); after.setStart(range.endContainer, range.endOffset); suffix = (trailingSpace+after.toString()).slice(0, 512);
      } catch (ignored) { prefix = ""; suffix = ""; }
    }
    var headings = body.querySelectorAll("h1,h2,h3,h4,h5,h6");
    for (var hi = 0; hi < headings.length; hi++) {
      if (headings[hi].compareDocumentPosition(startElement) & Node.DOCUMENT_POSITION_PRECEDING) break;
      heading = (headings[hi].textContent || "").replace(/¶\s*$/, "").trim().slice(0, 256);
    }
      var conceptId = conceptButton.dataset.contextConcept;
      var renderedTarget = {concept:conceptId,rendered_exact:exact,rendered_prefix:prefix,rendered_suffix:suffix,selection_heading:heading};
      if (isStatic) {
        var selectionOwn = copyGeneration;
        ensurePortableProvider().then(function (provider) {
          if (selectionOwn !== copyGeneration || !selection.rangeCount) return;
          var mapped = provider.mapRenderedSelection(renderedTarget);
          publishSelectionContext(mapped, range, exact);
        }).catch(function (error) {
          if (selectionOwn !== copyGeneration) return;
          window.okfSelectionContext = null;
          if (selectionContextButton) selectionContextButton.hidden = true;
          copyStatus(error.message || "Selected text could not be mapped to canonical source.", true);
        });
        return;
      }
      try {
        var sourceArea = document.getElementById("okf-canonical-source");
        if (!sourceArea || !window.okfPortableRetrieval) throw new Error("Canonical selection mapping is unavailable.");
        var binary = atob(sourceArea.value), bytes = new Uint8Array(binary.length);
        for (var bi = 0; bi < binary.length; bi++) bytes[bi] = binary.charCodeAt(bi);
        var canonicalSource = new TextDecoder("utf-8", {fatal:true}).decode(bytes);
        publishSelectionContext(window.okfPortableRetrieval.mapRenderedSource(canonicalSource, renderedTarget), range, exact);
      } catch (error) {
        revokeSelectionContext();
        copyStatus(error.message || "Selected text could not be mapped to canonical source.", true);
      }
  }, 120);
  document.addEventListener("selectionchange", function () {
    revokeSelectionContext();
    updateSelectionContext();
  });

  // ---- Hover/focus popovers on internal links (P2-70) ------------------
  // role="tooltip" + aria-describedby. Triggered on hover (mouse) AND
  // focus (keyboard). Delay reduced to ~200ms (P2-70).
  var popover = null;
  var popoverTimer = null;
  var popoverId = "okf-popover";
  // popoverCache was unused fetch memoisation; removed (P3-9).

  function ensurePopover() {
    if (popover) return popover;
    popover = document.createElement("div");
    popover.className = "okf-popover";
    popover.id = popoverId;
    popover.setAttribute("role", "tooltip");
    popover.style.display = "none";
    document.body.appendChild(popover);
    return popover;
  }

  // Hover sequence token: hovering quickly across several links fires
  // overlapping fetches, and a slow EARLIER response could otherwise
  // populate the popover for the wrong link. Only the latest
  // hover's response may render; hidePopover also invalidates in-flight
  // responses so a popover can't reappear after mouseout.
  var popoverSeq = 0;
   function showPopover(target, x, y) {
    var seq = ++popoverSeq;
    // /__raw/<id> is the body only (no frontmatter), so a preview built from
    // it showed the id as the title and a generic type. /__data/summary is
    // the concept's parsed identity card: title, type, description, type
    // colour/icon and link/relation counts, without the body.
    fetch("/__data/summary?id=" + encodeURIComponent(target), { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (fm) {
        if (!fm || typeof fm !== "object" || seq !== popoverSeq) return;
        var p = ensurePopover();
        // SAFE rebuild: clear via textContent and rebuild with createElement
        // only (never innerHTML from fetched/server data). A hostile
        // summary response cannot inject markup or script.
        p.textContent = "";
        // Type band: the type icon + name as a neutral outlined micro-label
        // (Ledger: content types are not colour-coded).
        var typeEl = document.createElement("span");
        typeEl.className = "okf-popover__type";
        var typeName = typeof fm.type === "string" && fm.type ? fm.type : "concept";
        var typeIcon = makeTypeIconSvg(fm.icon);
        if (typeIcon) typeEl.appendChild(typeIcon);
        typeEl.appendChild(document.createTextNode(typeName));
        var titleEl = document.createElement("h4");
        titleEl.textContent = typeof fm.title === "string" && fm.title ? fm.title : target;
        p.appendChild(typeEl);
        p.appendChild(titleEl);
        if (typeof fm.description === "string" && fm.description) {
          var d = document.createElement("div");
          d.className = "okf-popover__body";
          d.textContent = readableExcerpt(fm.description);
          p.appendChild(d);
        }
        // Link/relation counts (plan §3.4): internal body links and typed
        // `relations` entries, counted by the server (numbers only).
        var counts = {
          outgoing: Number.isInteger(fm.links) && fm.links > 0 ? fm.links : 0,
          relations: Number.isInteger(fm.relations) && fm.relations > 0 ? fm.relations : 0
        };
        if (counts.outgoing || counts.relations) {
          var row = document.createElement("div");
          row.className = "okf-popover__counts";
          if (counts.outgoing) {
            var o = document.createElement("span");
            o.className = "okf-popover__count";
            o.appendChild(document.createTextNode("Links "));
            var os = document.createElement("strong"); os.textContent = String(counts.outgoing);
            o.appendChild(os);
            row.appendChild(o);
          }
          if (counts.relations) {
            var rr = document.createElement("span");
            rr.className = "okf-popover__count";
            rr.appendChild(document.createTextNode("Relations "));
            var rs = document.createElement("strong"); rs.textContent = String(counts.relations);
            rr.appendChild(rs);
            row.appendChild(rr);
          }
          p.appendChild(row);
        }
        p.style.display = "block";
        // Position near cursor (or focus point), clamped to viewport.
        var rect = p.getBoundingClientRect();
        var px = Math.min((x || 12) + 12, window.innerWidth - rect.width - 12);
        var py = Math.min((y || 12) + 12, window.innerHeight - rect.height - 12);
        p.style.left = Math.max(12, px) + "px";
        p.style.top = Math.max(12, py) + "px";
      })
      .catch(function () {});
  }

  function hidePopover() {
    popoverSeq++; // invalidate any in-flight popover fetch
    if (popover) popover.style.display = "none";
  }

  function scheduleShow(target, x, y) {
    if (popoverTimer) clearTimeout(popoverTimer);
    // Phase 2 P1 race fix (cache-invalidation-invariants): a NEW hover must
    // invalidate any EARLIER hover's in-flight fetch IMMEDIATELY — before this
    // hover's own ~200ms show delay AND before cancelShow's 80ms hide delay.
    // Without this bump, a slow earlier response resolving inside the 80ms
    // window (between mouseleave and hidePopover) still sees seq===popoverSeq
    // and renders stale content for the previous link. Bumping the sequence
    // here makes the earlier fetch's captured seq permanently stale, so it is
    // dropped by showPopover's existing seq check no matter when it resolves.
    // The intentional hover UX (show/hide delays) and the popoverSeq lifecycle
    // (also bumped by showPopover on fire and by hidePopover on cancel) are
    // unchanged.
    popoverSeq++;
    var delay = REDUCED_MOTION ? 0 : 200;   // P2-70: ~200ms (was 350ms)
    popoverTimer = setTimeout(function () {
      showPopover(target, x, y);
    }, delay);
  }
  function cancelShow() {
    if (popoverTimer) clearTimeout(popoverTimer);
    setTimeout(hidePopover, 80);
  }

  // Attach hover AND focus handlers to all internal links (P2-70).
  var hoverBoundLinks = new WeakSet();
  function bindLinkHovers() {
    // The preview fetches /__data/summary — a live-server capability (see
    // http_routes.md: live search/context is server-backed; static and SPA
    // builds read the portable corpus instead). Binding on static/SPA output
    // requested a 404 on every hover AND focus and pointed aria-describedby
    // at a tooltip that could never populate. Gate the enhancement on the
    // same serve-mode discriminator the workbench submit wiring uses: serve
    // keeps previews; static/SPA keep plain links — SPA navigation and live
    // previews are separate, unaffected paths.
    if (document.body.dataset.okfMode !== "serve") return;
    var links = document.querySelectorAll('a.okf-internal, a[href^="/"]:not([href="/"])');
    links.forEach(function (a) {
      var href = a.getAttribute("href") || "";
      // Only internal concept URLs of the form /<seg>/<seg>(...)
      if (!/^\/[A-Za-z0-9_][A-Za-z0-9_.\-/]*$/.test(href)) return;
      if (href.indexOf("/__") === 0) return;
      if (href === "/") return;
      var target = href.replace(/^\//, "");
      // aria-describedby links the trigger to its tooltip (P2-70).
      a.setAttribute("aria-describedby", popoverId);
      if (hoverBoundLinks.has(a)) return;
      hoverBoundLinks.add(a);
      a.addEventListener("mouseenter", function (e) {
        scheduleShow(target, e.clientX, e.clientY);
      });
      a.addEventListener("mouseleave", cancelShow);
      // Keyboard parity (P2-70): focus shows, blur hides.
      a.addEventListener("focus", function (e) {
        var rect = a.getBoundingClientRect();
        scheduleShow(target, rect.left, rect.bottom);
      });
      a.addEventListener("blur", cancelShow);
    });
    // Ensure the tooltip node exists so aria-describedby resolves.
    ensurePopover();
    // Hide popover on Escape for keyboard users.
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") hidePopover();
  });

  // ---- Document details open-state ownership --------------------------
  // Track reader ownership of the native Document details disclosure across
  // shared viewer targets (wiki.js is the shared loader for static, spa and
  // serve). It starts collapsed at every width; resizing never opens it.
  // The media-query listener below only re-runs syncInfoboxOpenState, which
  // performs no automatic opening.
    var _INFOBOX_WIDE_MQ = window.matchMedia ? window.matchMedia("(min-width: 901px)") : null;
  var _infoboxBootSettled = false;
  var _infoboxUserOwned = false;
  function syncInfoboxOpenState() {
    var box = document.querySelector("details.okf-page__infobox");
    if (!box) return;
    if (_infoboxUserOwned) return; // user toggled it — they own the state now
    // Reading starts with prose at every width. Native disclosure state is
    // user-owned; viewport size no longer silently opens a metadata wall.
  }
  // Mark the disclosure user-owned once the reader toggles it after boot.
  // Bound exactly once via a dataset flag (okfInfoboxBound). wiki.js is the
  // only owner of this state; studio.js neither reads nor changes it.
  function bindInfoboxToggleOwnership() {
    var box = document.querySelector("details.okf-page__infobox");
    if (!box || box.dataset.okfInfoboxBound === "1") return;
    box.dataset.okfInfoboxBound = "1";
    box.addEventListener("toggle", function () {
      if (_infoboxBootSettled) {
        _infoboxUserOwned = true;
        // Record reader ownership on the element as well (dataset flag), so
        // any observer can see it without re-deriving local state.
        box.dataset.okfInfoboxUserOwned = "1";
      }
    });
  }
  function bootInfoboxOpenState() {
    syncInfoboxOpenState();
    bindInfoboxToggleOwnership();
    // Settle boot on the next task so only reader toggles made after boot
    // mark user ownership.
    setTimeout(function () { _infoboxBootSettled = true; }, 0);
    if (_INFOBOX_WIDE_MQ && typeof _INFOBOX_WIDE_MQ.addEventListener === "function") {
      _INFOBOX_WIDE_MQ.addEventListener("change", syncInfoboxOpenState);
    } else if (_INFOBOX_WIDE_MQ && typeof _INFOBOX_WIDE_MQ.addListener === "function") {
      _INFOBOX_WIDE_MQ.addListener(syncInfoboxOpenState); // legacy Safari
    }
  }

  // Phase 2 reading affordance: hover a body heading → a ¶ anchor link
  // appears (click copies a deep link into the URL bar via navigation).
  // Only headings that already carry an id (the renderer assigns them)
  // get an anchor; markup is created once per heading.
  function bindHeadingAnchors() {
    var body = document.querySelector(".okf-page__body");
    if (!body) return;
    var heads = body.querySelectorAll("h1[id], h2[id], h3[id], h4[id], h5[id], h6[id]");
    heads.forEach(function (h) {
      if (h.querySelector(".okf-heading-anchor")) return;
      var a = document.createElement("a");
      a.className = "okf-heading-anchor";
      a.href = "#" + h.id;
      a.textContent = "¶";
      a.setAttribute("aria-label", "Link to this section");
      h.appendChild(a);
    });
  }

  // ---- Scroll-away chrome on phones ------------------------------------
  // At <=640px (sticky viewports) the top bar and the live document view bar
  // under it cost a third to a half of the screen. While the reader scrolls
  // down past them they slide away and only the view bar's Sections row stays
  // pinned (nothing stays on pages without Sections); scrolling up, reaching
  // the top, keyboard focus in the chrome, an open overlay, or a capture
  // bring the whole chrome back. State lives on <html data-okf-chrome>;
  // shell.css owns the presentation (transforms only, so nothing shifts).
  // Wider viewports never get the attribute, and the graph route keeps its
  // own full-screen layout.
  (function scrollAwayChrome() {
    var shell = document.querySelector(".okf-topbar.okf-shell");
    if (!shell || !window.matchMedia || document.body.classList.contains("okf-viewer--graph")) return;
    var root = document.documentElement;
    var query = window.matchMedia("(max-width: 640px) and (min-height: 481px)");
    var GUTTER = 4;   // view-bar surface kept above the pinned Sections row
    var TRAVEL = 10;  // one-direction scroll distance before the state flips
    var state = "", lastY = 0, travel = 0, frame = 0, compactHeight = 0;

    function scrollTop() { return Math.max(0, window.scrollY || window.pageYOffset || 0); }
    function boxHeight(node) {
      return node && node.getClientRects().length ? node.getBoundingClientRect().height : 0;
    }
    // Publishes the shifts that move the top bar wholly above the viewport
    // and the view bar up to its Sections row; returns the full stack height.
    function measure() {
      var bar = document.querySelector(".okf-concept-viewbar");
      var topHeight = boxHeight(shell), barHeight = boxHeight(bar), offset = barHeight;
      var kept = bar && bar.querySelector(".okf-outline-jump");
      if (kept && kept.getClientRects().length) {
        offset = Math.max(0, kept.getBoundingClientRect().top - bar.getBoundingClientRect().top - GUTTER);
      }
      root.style.setProperty("--okf-chrome-shift-top", -topHeight + "px");
      root.style.setProperty("--okf-chrome-shift-bar", -(topHeight + offset) + "px");
      compactHeight = barHeight - offset;
      return topHeight + barHeight;
    }
    // The full chrome must stay: during a capture, under any registered
    // overlay (menus, palette, Studio dialog), and while the live Studio
    // loading notice is anchored below the bar.
    function held() {
      if (root.hasAttribute("data-okf-capture")) return true;
      if (window.OKFOverlayStack && window.OKFOverlayStack.depth() > 0) return true;
      var notice = document.querySelector("body > .okf-live-studio-fallback");
      return !!(notice && notice.getClientRects().length && getComputedStyle(notice).position === "absolute");
    }
    function apply(next, animate) {
      if (!state || next === state) return;
      if (animate) root.setAttribute("data-okf-chrome-motion", "");
      else root.removeAttribute("data-okf-chrome-motion");
      state = next;
      root.setAttribute("data-okf-chrome", next);
      if (window.okfDocumentOutline && typeof window.okfDocumentOutline.updateOcclusion === "function") {
        window.okfDocumentOutline.updateOcclusion();
      }
    }
    function onScroll() {
      frame = 0;
      if (!state) return;
      var y = scrollTop(), delta = y - lastY;
      lastY = y;
      if (y <= 1 || held()) { travel = 0; apply("revealed", y <= 1); return; }
      if (!delta) return;
      travel = (delta > 0) === (travel > 0) ? travel + delta : delta;
      if (travel >= TRAVEL && state === "revealed" && y > measure()) apply("compact", true);
      else if (travel <= -TRAVEL && state === "compact") apply("revealed", true);
    }
    function sync() {
      if (query.matches && !state) {
        state = "revealed";
        root.setAttribute("data-okf-chrome", state);
      } else if (!query.matches && state) {
        state = "";
        root.removeAttribute("data-okf-chrome");
        root.removeAttribute("data-okf-chrome-motion");
        if (window.okfDocumentOutline && typeof window.okfDocumentOutline.updateOcclusion === "function") {
          window.okfDocumentOutline.updateOcclusion();
        }
      }
      lastY = scrollTop();
      travel = 0;
    }

    window.addEventListener("scroll", function () {
      if (state && !frame) frame = requestAnimationFrame(onScroll);
    }, { passive: true });
    if (query.addEventListener) query.addEventListener("change", sync);
    else if (query.addListener) query.addListener(sync);
    // Keyboard (or programmatic) focus entering hidden chrome brings it all
    // back; the pinned Sections row is already on screen and never moves
    // under the reader's finger. shell.css reveals synchronously too, so the
    // control is visible before the browser scrolls to it.
    document.addEventListener("focusin", function (event) {
      var bar = document.querySelector(".okf-concept-viewbar");
      var target = event.target;
      if (state === "compact" && target && target.closest &&
          (shell.contains(target) || (bar && bar.contains(target) && !target.closest(".okf-outline-jump")))) {
        apply("revealed", false);
      }
    });
    window.addEventListener("okf-loom:overlaychange", function () {
      if (state === "compact" && held()) apply("revealed", false);
    });
    // Geometry is re-measured whenever compact is entered or a jump is
    // prepared; the published shifts also follow viewport resizes.
    window.addEventListener("resize", function () { if (state === "compact") measure(); });
    sync();

    window.okfShellChrome = Object.freeze({
      get state() { return state || "static"; },
      // The height of chrome occluding the document top, given the full
      // sticky stack height.
      occlusion: function (full) { return state === "compact" ? compactHeight : full; },
      // Heading jumps: past the full chrome the jump lands under the pinned
      // row alone, so settle compact first (no slide); near the top keep the
      // full chrome. document-outline.js then scrolls and calls settle().
      prepareJump: function (target) {
        if (!state || !target || held()) return;
        var full = measure();
        var top = target.getBoundingClientRect().top + scrollTop();
        apply(top - compactHeight - 16 > full ? "compact" : "revealed", false);
      },
      settle: function () { lastY = scrollTop(); travel = 0; },
    });
  })();

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      bindLinkHovers();
      renderLocalGraph();
      bindHeadingAnchors();
      bootInfoboxOpenState();
    });
  } else {
    bindLinkHovers();
    renderLocalGraph();
    bindHeadingAnchors();
    bootInfoboxOpenState();
  }
  // Re-apply after live SSE body patches (studio dispatches this event).
  window.addEventListener("okf-loom:bodyPatched", bindHeadingAnchors);
  window.addEventListener("okf-loom:bodyPatched", function () {
    // The live renderer can replace the canonical anchors. Retire the old
    // hover generation and bind each new node exactly once, not via copied
    // DOM attributes that could claim a listener survived replacement.
    cancelShow();
    bindLinkHovers();
  });
})();
