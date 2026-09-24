/**
 * OKF renderers.js — progressive enhancement for rich content blocks.
 *
 * Two halves:
 *
 * 1. CDN renderers: Mermaid (diagrams), highlight.js (syntax highlighting),
 *    and KaTeX (math), loaded ONLY when the page contains elements that
 *    need them. Degrades gracefully: if the CDN is unreachable, the raw
 *    source text remains visible (readable but unstyled).
 *
 * 2. Local markdown UX enhancers (no CDN, no network): sortable /
 *    filterable / column-resizable tables with sticky headers and
 *    copy-as-CSV, copy buttons + language badges on code blocks, and a
 *    click-to-zoom lightbox for images. All DOM the enhancers inject is
 *    additive and idempotent; studio.js's live block-diff sees through the
 *    wrappers via their data-source attribute (same contract as mermaid).
 *
 * CSP-safe: loaded as a local <script defer> from /__static/renderers.js.
 * The CDN scripts are dynamically inserted only when needed, so pages
 * without mermaid/code/math incur zero overhead.
 *
 * Supply-chain posture: every CDN URL is pinned to an EXACT
 * version (jsdelivr per-version URLs are immutable), matching the pinned
 * cytoscape tag in render.py. <link>/<script> assets additionally carry
 * SRI integrity hashes. The two dynamic import() modules (mermaid, hljs)
 * cannot carry SRI (the import spec has no integrity slot, and their ESM
 * graphs pull sub-modules anyway) — exact-version pinning is the
 * practical mitigation there. When bumping a version, recompute hashes:
 *   curl -s <url> | openssl dgst -sha384 -binary | openssl base64 -A
 *
 * Offline assets (X1, `build|render --assets offline`): a page whose <head>
 * carries script[type=application/json]#okf-asset-bootstrap
 * {"schema":1,"mode":"offline","delivery":...} (body elements never count)
 * loads the SAME pinned versions from byte-verified local copies instead:
 * "files" resolves them next to this script (__static/vendor/...), "inline"
 * reads the single-file's embedded <head> #okf-vendor-* JSON carriers. Mermaid and
 * highlight.js then load as classic scripts (window.mermaid / window.hljs).
 * There is NEVER a CDN fallback: a missing or invalid local asset fails the
 * renderer with code "offline-asset-unavailable" and the raw source stays.
 * Integrity is checked at build time (viewer/vendor.py), so local loads
 * carry no SRI/crossorigin attributes (those would break file://).
 */
(function () {
  "use strict";

  // Captured synchronously: only valid while this script is executing.
  var scriptBase = (document.currentScript && document.currentScript.src) || "";

  // Only the server's parsed, offset-qualified expiry reaches this clock.
  // No Date.parse normalization of invalid dates and no persisted stale flag.
  var metadataTimer = null;
  function refreshMetadataClock() {
    if (metadataTimer) clearTimeout(metadataTimer);
    metadataTimer = null;
    var now = Date.now(), next = Infinity;
    document.querySelectorAll("[data-okf-stale-at]").forEach(function (badge) {
      var at = Number(badge.dataset.okfStaleAt);
      if (!Number.isFinite(at)) return;
      var label = now >= at ? "Stale" : "Not stale";
      if (badge.textContent !== label) badge.textContent = label;
      if (at > now) next = Math.min(next, at);
    });
    if (Number.isFinite(next)) metadataTimer = setTimeout(refreshMetadataClock, Math.min(2147483647, Math.max(1, next - now)));
  }
  document.addEventListener("click", function (event) {
    var ref = event.target.closest && event.target.closest("a[data-okf-source-ref]");
    if (!ref) return;
    var target = document.getElementById(ref.getAttribute("href").slice(1));
    if (!target) return;
    var disclosure = target.closest("details");
    if (disclosure) disclosure.open = true;
    var graphPane = target.closest("#okf-detail");
    if (graphPane) {
      // Graph routes own concept selection; a claim jump must not trigger a
      // popstate selection restore (which would replace this very disclosure).
      // Keep the hash in history, but scroll only the document's local pane.
      event.preventDefault();
      history.pushState(history.state, "", ref.getAttribute("href"));
      document.body.scrollTop = 0;
      var graphLayout = graphPane.closest(".okf-layout");
      if (graphLayout) graphLayout.scrollTop = 0;
      target.focus({preventScroll:true});
      graphPane.scrollTop += target.getBoundingClientRect().top - graphPane.getBoundingClientRect().top - 16;
      return;
    }
    // Native fragment scrolling must account for the real wrapped chrome,
    // not a desktop-only header constant. Measure only visible sticky/fixed
    // strips overlapping this reading lane; the nearest scrollport owns it.
    var owner = target.parentElement;
    while (owner && !(owner.scrollHeight > owner.clientHeight && /auto|scroll/.test(getComputedStyle(owner).overflowY))) owner = owner.parentElement;
    var top = owner ? Math.max(0, owner.getBoundingClientRect().top) : 0;
    var lane = target.getBoundingClientRect(), covered = top;
    document.querySelectorAll(".okf-topbar, .okf-static-viewer-note, .okf-studio-bar, .okf-onthispage").forEach(function(strip) {
      var rect = strip.getBoundingClientRect(), position = getComputedStyle(strip).position;
      if ((position === "fixed" || position === "sticky") && rect.height && rect.right > lane.left && rect.left < lane.right && rect.top <= covered + 1) covered = Math.max(covered, rect.bottom);
    });
    target.style.scrollMarginTop = Math.max(16, covered - top + 16) + "px";
    target.focus({preventScroll:true});
  });
  window.addEventListener("okf-loom:bodyPatched", refreshMetadataClock);
  document.addEventListener("visibilitychange", refreshMetadataClock);
  window.addEventListener("pageshow", refreshMetadataClock);
  window.addEventListener("pagehide", function () { if (metadataTimer) clearTimeout(metadataTimer); metadataTimer = null; });
  refreshMetadataClock();

  // Review in Studio -------------------------------------------------------
  // Portable exports carry credential-free review metadata in
  // #okf-review-bootstrap. With a configured base, the read-only note gains a
  // real link to the document on screen (and the heading in the address bar)
  // in the live Studio. Without one, the note says why commenting is not
  // offered here. Nothing but the public base, the document path and a heading
  // id ever reaches the URL: no query, token, selection text or referrer.
  function reviewBootstrap() {
    var el = document.getElementById("okf-review-bootstrap");
    if (!el) return null;
    try {
      var data = JSON.parse(el.textContent || "");
      return data && data.schema === 1 ? data : null;
    } catch (e) { return null; }
  }
  // Same admission as the build-time validator: a client must never trust a
  // hand-edited export into emitting a credentialed or scripted destination.
  function reviewBase(value) {
    if (typeof value !== "string" || !value || value.length > 2048 || /[^\x21-\x7e]|[?#@\\%]/.test(value)) return null;
    try {
      var url = new URL(value);
      if ((url.protocol !== "http:" && url.protocol !== "https:") || !url.hostname || url.username || url.password || url.search || url.hash) return null;
    } catch (e) { return null; }
    return value.replace(/\/+$/, "") + "/";
  }
  function reviewDocumentPath(value) {
    if (typeof value !== "string" || !/\.md$/.test(value) || /^\/|\\|[\x00-\x1f]/.test(value)) return null;
    var parts = value.split("/");
    for (var i = 0; i < parts.length; i++) if (!parts[i] || parts[i] === "." || parts[i] === "..") return null;
    return value;
  }
  // Byte-for-byte urllib.parse.quote, so a link built here equals review_url().
  function reviewQuote(text, keepSlash) {
    var out = encodeURIComponent(text).replace(/[!'()*]/g, function (c) { return "%" + c.charCodeAt(0).toString(16).toUpperCase(); });
    return keepSlash ? out.replace(/%2F/g, "/") : out;
  }
  function mountReviewLink() {
    var note = document.querySelector(".okf-static-viewer-note");
    var data = reviewBootstrap();
    if (!note || !data || document.body.getAttribute("data-okf-mode") === "serve") return;
    var base = reviewBase(data.base_url);
    var slot = document.createElement("span");
    slot.className = "okf-review";
    note.appendChild(document.createTextNode(" "));
    note.appendChild(slot);
    if (!base) {
      note.setAttribute("data-okf-review", "unavailable");
      slot.className = "okf-review okf-review--unavailable";
      slot.textContent = "Review in Studio is unavailable: this export has no live Studio address, so comments cannot be left from here.";
      return;
    }
    var host = base;
    try { host = new URL(base).host; } catch (e) {}
    var link = document.createElement("a");
    link.className = "okf-review__link";
    link.rel = "noopener noreferrer";
    link.referrerPolicy = "no-referrer";
    link.textContent = "Review in Studio";
    var where = document.createElement("span");
    where.className = "okf-review__where";
    where.id = "okf-review-where";
    link.setAttribute("aria-describedby", where.id);
    slot.appendChild(link);
    slot.appendChild(document.createTextNode(" "));
    slot.appendChild(where);
    note.setAttribute("data-okf-review", "enabled");

    var pageDocument = reviewDocumentPath(data.document);
    var selectedConcept = null;
    function shownDocument() {
      if (pageDocument) return pageDocument;
      // The single-file concept list is a synthesized destination, not a
      // document; a selection left behind it is not what the reader sees.
      if (document.body.getAttribute("data-okf-single-destination") === "index") return null;
      var reserved = document.getElementById("detail-document");
      var reservedBody = document.getElementById("detail-document-body");
      if (reserved && !reserved.hidden && reservedBody) return reviewDocumentPath(reservedBody.getAttribute("data-okf-document"));
      return selectedConcept ? reviewDocumentPath(selectedConcept + ".md") : null;
    }
    // Only a heading/anchor of the document being read is a review fragment;
    // viewer chrome ids (#detail-content, #okf-main, #node-…) are not.
    function shownFragment() {
      var raw = (location.hash || "").slice(1);
      if (!raw) return "";
      var id = raw;
      try { id = decodeURIComponent(raw); } catch (e) {}
      var target = document.getElementById(id);
      return target && target.closest(".okf-prose, .okf-page__body, #detail-body, #detail-document-body") ? id : "";
    }
    function sync() {
      var doc = shownDocument();
      var fragment = doc ? shownFragment() : "";
      link.href = base + (doc ? reviewQuote(doc, true) : "") + (fragment ? "#" + reviewQuote(fragment, false) : "");
      link.setAttribute("aria-label", "Review in Studio: " + (doc ? doc + (fragment ? ", section " + fragment : "") : "bundle home"));
      if (doc) link.setAttribute("data-okf-review-document", doc); else link.removeAttribute("data-okf-review-document");
      where.textContent = "opens " + (doc || "the bundle home") + " on " + host + " to comment.";
    }
    window.addEventListener("hashchange", sync);
    window.addEventListener("popstate", sync);
    window.addEventListener("okf-loom:graph-selection", function (event) {
      selectedConcept = event.detail && event.detail.conceptId ? String(event.detail.conceptId) : null;
      sync();
    });
    var pane = document.getElementById("okf-detail");
    if (pane && !pageDocument) {
      var body = document.getElementById("detail-body"), content = document.getElementById("detail-content");
      if (body && content && !content.hidden && body.getAttribute("data-concept-id")) selectedConcept = body.getAttribute("data-concept-id");
      // The reserved-document reader is created lazily and toggled by `hidden`.
      new MutationObserver(sync).observe(pane, {childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "data-okf-document"]});
      new MutationObserver(sync).observe(document.body, {attributes: true, attributeFilter: ["data-okf-single-destination"]});
    }
    sync();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mountReviewLink);
  else mountReviewLink();

  // Single source of truth for CDN versions + SRI hashes.
  var PINS = {
    mermaidEsm: "https://cdn.jsdelivr.net/npm/mermaid@11.17.2/dist/mermaid.esm.min.mjs",
    // jsDelivr's +esm transform: the package's own es/index.js is a
    // bundler-oriented wrapper whose raw browser import provides no
    // default export (verified in-browser: highlighting silently never
    // ran). +esm serves a self-contained ESM bundle with hljs as the
    // default export, still pinned to the exact version.
    hljsEsm: "https://cdn.jsdelivr.net/npm/highlight.js@11.12.0/+esm",
    hljsThemeLight: {
      href: "https://cdn.jsdelivr.net/npm/highlight.js@11.12.0/styles/github.min.css",
      integrity: "sha384-eFTL69TLRZTkNfYZOLM+G04821K1qZao/4QLJbet1pP4tcF+fdXq/9CdqAbWRl/L",
    },
    hljsThemeDark: {
      href: "https://cdn.jsdelivr.net/npm/highlight.js@11.12.0/styles/github-dark.min.css",
      integrity: "sha384-wH75j6z1lH97ZOpMOInqhgKzFkAInZPPSPlZpYKYTOqsaizPvhQZmAtLcPKXpLyH",
    },
    katexCss: {
      href: "https://cdn.jsdelivr.net/npm/katex@0.16.47/dist/katex.min.css",
      integrity: "sha384-nH0MfJ44wi1dd7w6jinlyBgljjS8EJAh2JBoRad8a3VDw2K69vfaaqm4WnR+gXtA",
    },
    katexJs: {
      src: "https://cdn.jsdelivr.net/npm/katex@0.16.47/dist/katex.min.js",
      integrity: "sha384-CwjPRVHTvLiMBFjEoij+QZViMV5rhTOIp7CJzl24JEqpRDA1sJFHVXXLURktbYYp",
    },
  };

  // Offline pin table: path below __static/ and single-file carrier id.
  // KEEP IN SYNC with RENDERER_ASSETS in viewer/vendor.py (drift-tested).
  var OFFLINE_ASSETS = {
    mermaid: { file: "vendor/mermaid-11.17.2/mermaid.min.js", carrier: "okf-vendor-mermaid" },
    hljs: { file: "vendor/highlightjs-11.12.0/highlight.offline.min.js", carrier: "okf-vendor-hljs" },
    hljsThemeLight: { file: "vendor/highlightjs-11.12.0/github.min.css", carrier: "okf-vendor-hljs-light" },
    hljsThemeDark: { file: "vendor/highlightjs-11.12.0/github-dark.min.css", carrier: "okf-vendor-hljs-dark" },
    katexCss: { file: "vendor/katex-0.16.47/katex.offline.min.css", carrier: "okf-vendor-katex-css" },
    katexJs: { file: "vendor/katex-0.16.47/katex.min.js", carrier: "okf-vendor-katex-js" },
  };
  // Only the GENERATED configuration counts: a script[type=application/json]
  // in <head>. Authored body content (e.g. a heading slugged
  // "okf-asset-bootstrap") can never switch modes or supply a carrier.
  function headJson(id) {
    var head = document.head;
    if (!head) return null;
    var nodes = head.querySelectorAll('script[type="application/json"]');
    for (var i = 0; i < nodes.length; i++) if (nodes[i].id === id) return nodes[i];
    return null;
  }
  var BOOTSTRAP = headJson("okf-asset-bootstrap");
  var OFFLINE = !!BOOTSTRAP;
  var LOAD_FAILURE = OFFLINE ? "offline-asset-unavailable" : "blocked-script";

  function offlinePins() {
    var boot = null;
    try { boot = JSON.parse(BOOTSTRAP.textContent || ""); } catch (e) { boot = null; }
    var delivery = boot && boot.schema === 1 && boot.mode === "offline" ? boot.delivery : null;
    function pin(key, kind) {
      var asset = OFFLINE_ASSETS[key], out = { offline: true };
      if (delivery === "inline") {
        out.carrier = asset.carrier;
        out[kind] = "okf-vendor:" + asset.carrier;
      } else if (delivery === "files" && scriptBase) {
        try { out[kind] = new URL(asset.file, scriptBase).href; } catch (e) { out.invalid = true; }
      } else {
        out.invalid = true; // fail closed: never fall back to the CDN
      }
      if (out.invalid) out[kind] = "okf-vendor:unavailable:" + key;
      return out;
    }
    return {
      mermaidScript: pin("mermaid", "src"),
      hljsScript: pin("hljs", "src"),
      hljsThemeLight: pin("hljsThemeLight", "href"),
      hljsThemeDark: pin("hljsThemeDark", "href"),
      katexCss: pin("katexCss", "href"),
      katexJs: pin("katexJs", "src"),
    };
  }
  if (OFFLINE) PINS = offlinePins();

  function offlineUnavailable(what) {
    return rendererError("offline-asset-unavailable", "The offline " + what + " is unavailable.");
  }

  function carrierText(pin) {
    var el = pin.carrier ? headJson(pin.carrier) : null;
    if (!el) return null;
    try {
      var text = JSON.parse(el.textContent || "");
      return typeof text === "string" ? text : null;
    } catch (e) { return null; }
  }

  // ------------------------------------------------------------------
  // Async renderer lifecycle
  // ------------------------------------------------------------------
  //
  // The page renderer is an optional enhancement, but it is still part of
  // the capture contract.  A page must not be reported as ready while a
  // renderer can still replace authored text, and a late CDN result must not
  // write into a newer body/theme generation.  The token below is deliberately
  // local to this consumer: theme.js remains the sole owner of root theme
  // state, while live.js/studio.js remain the owners of body replacement.
  var testHooks = window.__OKF_RENDERER_TEST_HOOKS__ || null;
  var bodyGeneration = 0;
  var themeGeneration = 0;
  var renderGeneration = 0;
  var activeToken = null;
  var currentThemeName = themeName();
  var currentResolvedMode = resolvedMode(currentThemeName);
  var hljsThemeLink = null;
  var hljsThemeRequest = null;
  var hljsModulePromise = null;
  var katexAssetsPromise = null;
  var mermaidModulePromise = null;
  var mermaidId = 0;

  function themeName() {
    var root = document.documentElement;
    return (root && (root.getAttribute("data-theme") || root.dataset.theme)) || "technical-light";
  }

  function resolvedMode(name) {
    return /-dark$/.test(name || "") ? "dark" : "light";
  }

  function isDark() {
    // Resolved themes are "<family>-<mode>" (e.g. "technical-dark"). A
    // theme is dark whenever its resolved mode is dark, regardless of family.
    var root = document.documentElement;
    var name = root && (root.getAttribute("data-theme") || root.dataset.theme) || "technical-light";
    return /-dark$/.test(name);
  }

  function rendererError(code, message, cause) {
    var error = new Error(message || code);
    error.name = "OkfRendererError";
    error.code = code || "renderer-failure";
    if (cause) error.cause = cause;
    return error;
  }

  function asRendererError(error, fallbackCode, fallbackMessage) {
    if (error && error.name === "OkfRendererError" && error.code) return error;
    return rendererError(
      (error && error.code) || fallbackCode || "renderer-failure",
      (error && error.message) || fallbackMessage || "A document renderer failed.",
      error
    );
  }

  function removeNode(node) {
    if (node && node.parentNode) node.parentNode.removeChild(node);
  }

  function loadScript(pin, type) {
    if (testHooks && typeof testHooks.loadScript === "function") {
      return Promise.resolve().then(function () {
        return testHooks.loadScript(pin, type);
      });
    }
    if (pin && pin.offline && (pin.invalid || pin.carrier)) {
      return new Promise(function (resolve, reject) {
        var code = pin.invalid ? null : carrierText(pin);
        if (code === null) { reject(offlineUnavailable("renderer script")); return; }
        var inline = document.createElement("script");
        inline.setAttribute("data-okf-renderer-resource", "script");
        inline.setAttribute("data-okf-vendor", pin.carrier);
        inline.text = code; // executes synchronously on insertion
        document.head.appendChild(inline);
        resolve(inline);
      });
    }
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = pin.src || pin;
      s.setAttribute("data-okf-renderer-resource", "script");
      if (pin.integrity) {
        s.integrity = pin.integrity;
        s.crossOrigin = "anonymous";
      }
      if (type) s.type = type;
      s.onload = function () { resolve(s); };
      s.onerror = function (event) {
        removeNode(s);
        reject(rendererError(LOAD_FAILURE, "A renderer script was blocked.", event));
      };
      document.head.appendChild(s);
    });
  }

  function loadCSS(pin) {
    if (testHooks && typeof testHooks.loadCSS === "function") {
      return Promise.resolve().then(function () {
        return testHooks.loadCSS(pin);
      });
    }
    if (pin && pin.offline && (pin.invalid || pin.carrier)) {
      return new Promise(function (resolve, reject) {
        var css = pin.invalid ? null : carrierText(pin);
        if (css === null) { reject(offlineUnavailable("renderer stylesheet")); return; }
        // Same inert-until-promoted contract as the <link> path below.
        var style = document.createElement("style");
        style.media = "not all";
        style.setAttribute("data-okf-renderer-resource", "stylesheet");
        style.setAttribute("data-okf-href", pin.href);
        style.textContent = css;
        document.head.appendChild(style);
        resolve(style);
      });
    }
    return new Promise(function (resolve, reject) {
      var l = document.createElement("link");
      l.rel = "stylesheet";
      l.href = pin.href || pin;
      l.media = "not all";
      l.setAttribute("data-okf-renderer-resource", "stylesheet");
      if (pin.integrity) {
        l.integrity = pin.integrity;
        l.crossOrigin = "anonymous";
      }
      l.onload = function () { resolve(l); };
      l.onerror = function (event) {
        removeNode(l);
        reject(rendererError(LOAD_FAILURE, "A renderer stylesheet was blocked.", event));
      };
      // The link is inert until the current generation's completion handler
      // promotes it to media=all.  An old theme can therefore never flash
      // stale CSS while its request is in flight.
      document.head.appendChild(l);
    });
  }

  function loadModule(url) {
    if (testHooks && typeof testHooks.loadModule === "function") {
      return Promise.resolve().then(function () { return testHooks.loadModule(url); });
    }
    return import(url);
  }

  function currentToken(token) {
    return !!token && activeToken === token &&
      token.bodyGeneration === bodyGeneration &&
      token.themeGeneration === themeGeneration &&
      token.themeName === currentThemeName;
  }

  function setDataset(target, name, value) {
    if (!target || !target.dataset) return;
    if (value == null) delete target.dataset[name];
    else target.dataset[name] = String(value);
  }

  function publishReadiness(token, status, error) {
    if (!currentToken(token)) return;
    var detail = {
      status: status,
      ready: status === "ready",
      code: error ? (error.code || "renderer-failure") : null,
      message: error ? String(error.message || error) : null,
      generation: token.renderGeneration,
      bodyGeneration: token.bodyGeneration,
      themeGeneration: token.themeGeneration,
      themeName: token.themeName,
      resolvedMode: token.resolvedMode,
    };
    detail = Object.freeze(detail);
    window.okfRendererReadiness = detail;
    var root = document.documentElement;
    var body = document.body;
    // Keep the historical loading/ready data key for capture clients, while
    // adding an explicit terminal status and generation so a stale "ready"
    // value can never be mistaken for this scan.
    setDataset(root, "okfRenderersReady", status === "loading" ? "loading" : status);
    setDataset(root, "okfRenderersStatus", status);
    setDataset(root, "okfRenderersGeneration", token.renderGeneration);
    setDataset(root, "okfRenderersBodyGeneration", token.bodyGeneration);
    setDataset(root, "okfRenderersThemeGeneration", token.themeGeneration);
    setDataset(root, "okfRenderersCode", detail.code);
    setDataset(body, "okfRenderersReady", status === "loading" ? "loading" : status);
    setDataset(body, "okfRenderersStatus", status);
    setDataset(body, "okfRenderersGeneration", token.renderGeneration);
    setDataset(body, "okfRenderersBodyGeneration", token.bodyGeneration);
    setDataset(body, "okfRenderersThemeGeneration", token.themeGeneration);
    setDataset(body, "okfRenderersCode", detail.code);
    try {
      window.dispatchEvent(new CustomEvent("okf-loom:renderersReady", { detail: detail }));
    } catch (eventError) { /* readiness remains observable through state */ }
  }

  function beginCycle(reason, forceMermaid) {
    var token = {
      reason: reason || "scan",
      forceMermaid: !!forceMermaid,
      renderGeneration: ++renderGeneration,
      bodyGeneration: bodyGeneration,
      themeGeneration: themeGeneration,
      themeName: currentThemeName,
      resolvedMode: currentResolvedMode,
    };
    activeToken = token;
    // This is intentionally the first observable operation in a scan. All
    // resource requests below are children of this generation.
    publishReadiness(token, "loading", null);
    return token;
  }

  function settled(promise) {
    return Promise.resolve(promise).then(function (value) {
      return { ok: true, value: value };
    }, function (error) {
      return { ok: false, error: error };
    });
  }

  function finishCycle(token, tasks) {
    Promise.all((tasks || []).map(settled)).then(function (results) {
      if (!currentToken(token)) return;
      updateContentScrollports();
      var failed = results.filter(function (result) { return !result.ok; })[0];
      if (failed) {
        publishReadiness(token, "failed", asRendererError(
          failed.error,
          "renderer-failure",
          "A document renderer failed."
        ));
      } else {
        publishReadiness(token, "ready", null);
      }
    });
  }

  // --- Mermaid ---------------------------------------------------------
  // Mermaid's render() API returns SVG text. Rendering off-DOM and committing
  // only after the token check is what makes a late theme/body completion
  // unable to overwrite a newer diagram. The authored source is preserved in
  // data-source and is restored only for a current, resolved mode change.
  function getMermaidModule() {
    if (!mermaidModulePromise) {
      mermaidModulePromise = OFFLINE
        ? loadScript(PINS.mermaidScript).then(function () { return { default: window.mermaid }; })
        : loadModule(PINS.mermaidEsm);
    }
    return mermaidModulePromise;
  }

  function initMermaid(token) {
    var allDivs = Array.prototype.slice.call(document.querySelectorAll("div.mermaid"));
    if (!allDivs.length) return Promise.resolve();
    var mermaidDivs = allDivs.filter(function (el) {
      if (token.forceMermaid && currentToken(token) && el.hasAttribute("data-source")) {
        // Restore the exact authored source as text. This is a synchronous
        // mutation owned by the current theme generation.
        el.textContent = el.getAttribute("data-source");
        el.classList.remove("mermaid--fallback");
      }
      return !el.querySelector("svg");
    });
    if (!mermaidDivs.length) return Promise.resolve();
    mermaidDivs.forEach(function (el) {
      if (!el.hasAttribute("data-source")) el.setAttribute("data-source", el.textContent || "");
      if (!el.id) el.id = "okf-mermaid-" + (++mermaidId);
    });
    return getMermaidModule().then(function (mod) {
      if (!currentToken(token)) return;
      var mermaid = (mod && (mod.default || mod.mermaid)) || window.mermaid;
      if (!mermaid || typeof mermaid.initialize !== "function" || typeof mermaid.render !== "function") {
        throw rendererError(LOAD_FAILURE, "Mermaid did not expose its renderer API.");
      }
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: token.resolvedMode === "dark" ? "dark" : "default" });
      return Promise.all(mermaidDivs.map(function (el) {
        if (!currentToken(token)) return Promise.resolve();
        var source = el.getAttribute("data-source") || "";
        // Mermaid owns/removes a temporary DOM element with the render id.
        // Never give it the authored host id (that deletes our projection node).
        var renderId = "okf-mermaid-svg-" + (++mermaidId);
        return Promise.resolve(mermaid.render(renderId, source)).then(function (rendered) {
          if (!currentToken(token) || !el.isConnected) return;
          var svg = rendered && rendered.svg != null ? rendered.svg : rendered;
          if (typeof svg !== "string" || !svg) {
            throw rendererError("renderer-failure", "Mermaid returned no SVG output.");
          }
          el.innerHTML = svg;
          el.classList.remove("mermaid--fallback");
          if (rendered && typeof rendered.bindFunctions === "function") {
            rendered.bindFunctions(el);
          }
        });
      }));
    }).catch(function (error) {
      if (!currentToken(token)) return;
      mermaidDivs.forEach(function (el) {
        if (currentToken(token)) el.classList.add("mermaid--fallback");
      });
      throw asRendererError(error, "blocked-script", "Mermaid could not be loaded.");
    });
  }

  // --- Syntax highlighting (highlight.js) -----------------------------
  function cssHref(link) {
    return link && (link.href || (link.getAttribute &&
      (link.getAttribute("href") || link.getAttribute("data-okf-href")))) || "";
  }

  function ensureHljsTheme(token) {
    var pin = token.resolvedMode === "dark" ? PINS.hljsThemeDark : PINS.hljsThemeLight;
    if (hljsThemeLink && cssHref(hljsThemeLink) === pin.href) return Promise.resolve(hljsThemeLink);
    if (hljsThemeRequest && hljsThemeRequest.href === pin.href && hljsThemeRequest.removed) {
      hljsThemeRequest = null;
    }
    if (!hljsThemeRequest || hljsThemeRequest.href !== pin.href) {
      hljsThemeRequest = { href: pin.href, promise: loadCSS(pin) };
    }
    var request = hljsThemeRequest;
    return request.promise.then(function (link) {
      if (!currentToken(token)) {
        // A newer mode will either adopt the same request or remove this
        // inert sheet. It is never promoted to active CSS by this completion.
        if (currentResolvedMode !== token.resolvedMode && link !== hljsThemeLink) {
          request.removed = true;
          removeNode(link);
        }
        return link;
      }
      request.removed = false;
      link.media = "all";
      var old = hljsThemeLink;
      hljsThemeLink = link;
      if (old && old !== link) removeNode(old);
      return link;
    });
  }

  function getHljsModule() {
    if (!hljsModulePromise) {
      hljsModulePromise = OFFLINE
        ? loadScript(PINS.hljsScript).then(function () { return { default: window.hljs }; })
        : loadModule(PINS.hljsEsm);
    }
    return hljsModulePromise;
  }

  function initHighlight(token) {
    var allBlocks = Array.prototype.slice.call(document.querySelectorAll("pre > code[class*='language-']"));
    if (!allBlocks.length) return Promise.resolve();
    var codeBlocks = allBlocks.filter(function (el) { return !el.dataset.highlighted; });
    var tasks = [ensureHljsTheme(token)];
    if (!codeBlocks.length) return Promise.all(tasks);
    tasks.push(getHljsModule().then(function (mod) {
      if (!currentToken(token)) return;
      var hljs = (mod && mod.default) || (mod && mod.hljs) || window.hljs;
      if (!hljs || typeof hljs.highlightElement !== "function") {
        throw rendererError(LOAD_FAILURE, "highlight.js did not expose its renderer API.");
      }
      codeBlocks.forEach(function (block) {
        if (!currentToken(token)) return;
        try { hljs.highlightElement(block); } catch (error) {
          throw asRendererError(error, "renderer-failure", "highlight.js failed to render a code block.");
        }
        if (currentToken(token)) block.dataset.highlighted = "true";
      });
    }));
    return Promise.all(tasks);
  }

  // --- Math (KaTeX) ----------------------------------------------------
  function getKatexAssets() {
    if (!katexAssetsPromise) {
      katexAssetsPromise = Promise.all([
        loadCSS(PINS.katexCss),
        loadScript(PINS.katexJs),
      ]).then(function (assets) {
        if (!window.katex || typeof window.katex.render !== "function") {
          throw rendererError(LOAD_FAILURE, "KaTeX did not expose its renderer API.");
        }
        return assets;
      });
    }
    return katexAssetsPromise;
  }

  function initMath(token) {
    var allMath = Array.prototype.slice.call(document.querySelectorAll("div.math"));
    var mathDivs = allMath.filter(function (el) { return !el.dataset.mathRendered; });
    if (!mathDivs.length) return Promise.resolve();
    return getKatexAssets().then(function (assets) {
      if (!currentToken(token)) return;
      if (assets && assets[0]) assets[0].media = "all";
      mathDivs.forEach(function (el, i) {
        if (!currentToken(token) || el.dataset.mathRendered) return;
        var tex = el.textContent;
        el.id = el.id || ("okf-math-" + i);
        window.katex.render(tex, el, { throwOnError: false, displayMode: true });
        if (currentToken(token)) el.dataset.mathRendered = "true";
      });
    }).catch(function (error) {
      if (!currentToken(token)) return;
      throw asRendererError(error, "blocked-script", "KaTeX could not be loaded.");
    });
  }

  // ====================================================================
  // Local markdown UX enhancers (tables / code blocks / images)
  // ====================================================================
  // Scope: rendered concept bodies everywhere they appear — concept pages
  // (.okf-prose.okf-page__body), the index intro (.okf-prose), and the
  // graph / single-file detail panel (graph.js swaps the class list to
  // .okf-page__body). NOT the studio source pane (pre.okf-source lives
  // outside these containers) and NOT the frontmatter table.
  var PROSE_SELECTOR = ".okf-prose, .okf-page__body";
  // Show the filter/count/copy toolbar only when a table has enough rows
  // for scanning to hurt; sorting + resizing apply to every table.
  var TABLE_TOOLBAR_MIN_ROWS = 5;

  function proseRoots() {
    return Array.prototype.slice.call(document.querySelectorAll(PROSE_SELECTOR));
  }

  function collapseWs(s) {
    return (s || "").replace(/\s+/g, " ").trim();
  }

  // --- authored wide blocks: measured keyboard scroll regions -----------
  // SSR supplies only stable structure + a contextual label.  Geometry can
  // change after Mermaid/KaTeX/highlight rendering, live body replacement,
  // image load, font load, zoom, and viewport resize, so role/name/tab stop
  // are runtime-owned and present only while real horizontal overflow exists.
  var contentResizeObserver = typeof ResizeObserver === "function"
    ? new ResizeObserver(function () { scheduleContentScrollports(); }) : null;
  var contentFitRaf = 0;

  function contentScrollLabel(port) {
    var base = collapseWs(port.getAttribute("data-okf-scroll-label"));
    if (!base) {
      var kind = port.getAttribute("data-okf-scroll-kind");
      base = kind === "code" ? "Code block" : kind === "diagram" ? "Diagram" : "Figure or media";
    }
    return base + ". Scroll horizontally to view all content.";
  }

  function applyContentScrollport(port) {
    var overflows = port.scrollWidth > port.clientWidth + 1;
    port.classList.toggle("okf-content-scrollport--scrolls", overflows);
    if (overflows) {
      port.setAttribute("role", "region");
      port.setAttribute("aria-label", contentScrollLabel(port));
      port.tabIndex = 0;
    } else {
      port.removeAttribute("role");
      port.removeAttribute("aria-label");
      port.removeAttribute("tabindex");
    }
    if (contentResizeObserver && port.dataset.okfResizeObserved !== "1") {
      contentResizeObserver.observe(port);
      Array.prototype.forEach.call(port.children, function (child) {
        contentResizeObserver.observe(child);
      });
      port.dataset.okfResizeObserved = "1";
    }
  }

  function updateContentScrollports() {
    Array.prototype.forEach.call(document.querySelectorAll(".okf-content-scrollport"), applyContentScrollport);
  }

  function scheduleContentScrollports() {
    if (contentFitRaf) return;
    contentFitRaf = requestAnimationFrame(function () {
      contentFitRaf = 0;
      updateContentScrollports();
    });
  }

  // --- clipboard helper (shared by table CSV + code copy buttons) -------
  function copyText(text, btn, okLabel) {
    function flash(label) {
      var prev = btn.textContent;
      btn.textContent = label;
      btn.disabled = true;
      setTimeout(function () { btn.textContent = prev; btn.disabled = false; }, 1500);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { flash(okLabel); },
        function () { flash("Copy failed"); }
      );
      return;
    }
    // Legacy fallback (non-secure contexts): hidden textarea + execCommand.
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    flash(ok ? okLabel : "Copy failed");
  }

  // --- tables: sort / filter / resize / sticky / copy --------------------
  function initTables() {
    proseRoots().forEach(function (root) {
      Array.prototype.forEach.call(root.querySelectorAll("table"), function (t) {
        if (t.dataset.okfEnhanced) return;
        if (t.closest(".okf-tablewrap")) return; // already wrapped (nested scan)
        enhanceTable(t);
      });
    });
    updateTableFits();
  }

  function tbodyRows(table) {
    var tb = table.tBodies[0];
    return tb ? Array.prototype.slice.call(tb.rows) : [];
  }

  function enhanceTable(table) {
    table.dataset.okfEnhanced = "1";
    if (!table.tHead || !table.tHead.rows.length) return; // headerless: leave as-is

    // Wrapper carries the horizontal scroll (previously on the table itself)
    // plus data-source: the ORIGINAL whitespace-collapsed text, which
    // studio.js's blockSig uses so a user-applied sort/filter never makes an
    // unchanged table look "changed" to the live diff (and vice versa).
    var wrap = document.createElement("div");
    wrap.className = "okf-tablewrap";
    wrap.setAttribute("data-source", collapseWs(table.textContent));
    table.parentNode.insertBefore(wrap, table);

    var rows = tbodyRows(table);
    if (rows.length >= TABLE_TOOLBAR_MIN_ROWS) {
      wrap.appendChild(buildTableToolbar(table));
    }
    wrap.appendChild(table);
    table.classList.add("okf-table--enhanced");
    // P1 cue-geometry: nest the scroll port (.okf-tablewrap) inside a
    // NON-SCROLLING .okf-tablewrap-frame. The edge-fade cues live on the
    // frame so they pin to the wrapper's visible edges and do NOT scroll
    // away with the table content (an abspos cue inside the scroll port
    // scrolls with the content). The frame holds the --scrolls /
    // --scrolled-* affordance classes; .okf-tablewrap keeps --fit (overflow).
    var frame = document.createElement("div");
    frame.className = "okf-tablewrap-frame";
    wrap.parentNode.insertBefore(frame, wrap);
    frame.appendChild(wrap);
    // Phase 2 (mobile a11y): wire the horizontal-scroll affordance (semantic
    // region + edge fade cues) once per wrapper. The actual region/cue state
    // is applied/removed by applyTableScrollAffordance whenever overflow is
    // recomputed (enhance, resize, live patch, column-drag end), so a table
    // that fits gains no extra tab stop and a table that overflows always
    // gets the cue back.
    wireTableScrollAffordance(frame, wrap, table);

    // Original row order, for the third state of the sort cycle.
    var originalRows = rows.slice();

    var headRow = table.tHead.rows[0];
    Array.prototype.forEach.call(headRow.cells, function (th) {
      th.classList.add("okf-th-sortable");
      th.setAttribute("aria-sort", "none");
      th.tabIndex = 0;
      // The resizer is a drag handle on the column's right edge. It sits
      // inside the th but is aria-hidden decorative chrome; keyboard users
      // get horizontal scrolling instead (resize is a pointer nicety).
      var grip = document.createElement("span");
      grip.className = "okf-col-resizer";
      grip.setAttribute("aria-hidden", "true");
      th.appendChild(grip);
      wireColumnResize(table, wrap, th, grip);

      th.addEventListener("click", function (e) {
        if (e.target.closest(".okf-col-resizer")) return;
        cycleSort(table, th, originalRows);
      });
      th.addEventListener("keydown", function (e) {
        if (e.key !== "Enter" && e.key !== " ") return;
        e.preventDefault();
        cycleSort(table, th, originalRows);
      });
    });
  }

  function buildTableToolbar(table) {
    var bar = document.createElement("div");
    bar.className = "okf-table-toolbar";
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Table tools");

    var filter = document.createElement("input");
    filter.type = "search";
    filter.className = "okf-table-filter";
    filter.placeholder = "Filter rows…";
    filter.setAttribute("aria-label", "Filter table rows");

    var count = document.createElement("span");
    count.className = "okf-table-count";
    count.setAttribute("aria-live", "polite");

    var copy = document.createElement("button");
    copy.type = "button";
    copy.className = "okf-table-copy";
    copy.textContent = "Copy CSV";
    copy.setAttribute("aria-label", "Copy table as CSV");

    function updateCount() {
      var rows = tbodyRows(table);
      var visible = rows.filter(function (r) { return !r.classList.contains("okf-row-hidden"); });
      count.textContent = filter.value
        ? visible.length + " of " + rows.length + " rows"
        : rows.length + " rows";
    }
    updateCount();

    filter.addEventListener("input", function () {
      var q = filter.value.trim().toLowerCase();
      tbodyRows(table).forEach(function (r) {
        var hit = !q || (r.textContent || "").toLowerCase().indexOf(q) >= 0;
        r.classList.toggle("okf-row-hidden", !hit);
      });
      updateCount();
    });
    filter.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && filter.value) {
        filter.value = "";
        filter.dispatchEvent(new Event("input"));
        e.stopPropagation(); // keep Esc from also closing studio panels
      }
    });
    copy.addEventListener("click", function () {
      copyText(tableToCsv(table), copy, "Copied ✓");
    });

    bar.appendChild(filter);
    bar.appendChild(count);
    bar.appendChild(copy);
    return bar;
  }

  function tableToCsv(table) {
    // Current sort order, ALL rows (a filtered view still copies the whole
    // table — partial exports silently masquerading as full ones are worse).
    var lines = [];
    function pushRow(cells) {
      lines.push(Array.prototype.map.call(cells, function (c) {
        var v = collapseWs(c.textContent);
        return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
      }).join(","));
    }
    if (table.tHead && table.tHead.rows.length) pushRow(table.tHead.rows[0].cells);
    tbodyRows(table).forEach(function (r) { pushRow(r.cells); });
    return lines.join("\n");
  }

  // Sort comparator: numeric when every non-empty cell in the column parses
  // as a number (currency/percent/thousands tolerated), else natural-order
  // string compare (so v1.10 > v1.9). Empty cells always sort last.
  function numericValue(s) {
    if (!s) return null;
    var cleaned = s.replace(/[,$£€%\s]/g, "");
    if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(cleaned)) return null;
    return parseFloat(cleaned);
  }

  function cycleSort(table, th, originalRows) {
    var headRow = table.tHead.rows[0];
    var prev = th.getAttribute("aria-sort") || "none";
    Array.prototype.forEach.call(headRow.cells, function (h) {
      h.setAttribute("aria-sort", "none");
    });
    var next = prev === "none" ? "ascending" : prev === "ascending" ? "descending" : "none";
    th.setAttribute("aria-sort", next);

    var tb = table.tBodies[0];
    if (!tb) return;
    if (next === "none") {
      originalRows.forEach(function (r) { tb.appendChild(r); });
      return;
    }
    var idx = th.cellIndex;
    var rows = tbodyRows(table);
    var keys = rows.map(function (r) {
      var c = r.cells[idx];
      return collapseWs(c ? c.textContent : "");
    });
    var nonEmpty = keys.filter(function (k) { return k !== ""; });
    var numeric = nonEmpty.length > 0 && nonEmpty.every(function (k) { return numericValue(k) !== null; });
    var dir = next === "ascending" ? 1 : -1;
    var decorated = rows.map(function (r, i) { return { row: r, key: keys[i], i: i }; });
    decorated.sort(function (a, b) {
      if (a.key === "" || b.key === "") {
        if (a.key === b.key) return a.i - b.i;
        return a.key === "" ? 1 : -1; // empties last regardless of direction
      }
      var cmp;
      if (numeric) {
        cmp = numericValue(a.key) - numericValue(b.key);
      } else {
        cmp = a.key.localeCompare(b.key, undefined, { numeric: true, sensitivity: "base" });
      }
      return cmp !== 0 ? dir * cmp : a.i - b.i; // stable
    });
    decorated.forEach(function (d) { tb.appendChild(d.row); });
  }

  function wireColumnResize(table, wrap, th, grip) {
    var startX = 0;
    var startW = 0;
    function freezeWidths() {
      // First drag: pin every column at its rendered width and switch to
      // fixed layout so one column's change doesn't reflow the others.
      if (table.style.tableLayout === "fixed") return;
      Array.prototype.forEach.call(table.tHead.rows[0].cells, function (h) {
        h.style.width = h.offsetWidth + "px";
      });
      table.style.tableLayout = "fixed";
    }
    grip.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      e.stopPropagation();
      freezeWidths();
      startX = e.clientX;
      startW = th.offsetWidth;
      grip.setPointerCapture(e.pointerId);
      grip.classList.add("okf-col-resizer--active");
    });
    grip.addEventListener("pointermove", function (e) {
      if (!grip.classList.contains("okf-col-resizer--active")) return;
      th.style.width = Math.max(48, startW + (e.clientX - startX)) + "px";
    });
    function endDrag() {
      if (!grip.classList.contains("okf-col-resizer--active")) return;
      grip.classList.remove("okf-col-resizer--active");
      updateTableFits();
    }
    grip.addEventListener("pointerup", endDrag);
    grip.addEventListener("pointercancel", endDrag);
    // Double-click a grip → back to automatic layout for the whole table.
    grip.addEventListener("dblclick", function (e) {
      e.stopPropagation();
      Array.prototype.forEach.call(table.tHead.rows[0].cells, function (h) {
        h.style.width = "";
      });
      table.style.tableLayout = "";
      updateTableFits();
    });
  }

  // Sticky headers only work when the wrapper is NOT a horizontal scroll
  // container (position:sticky pins to the nearest scrollport). When the
  // table fits, mark the wrapper so the stylesheet can lift the overflow
  // and let thead stick under the page topbar.
  //
  // Phase 2 (mobile a11y): the same recomputation also syncs the scroll
  // affordance — a wide table that overflows its column becomes a named
  // scrollable region (role=region + aria-label + tabindex=0) with visible
  // left/right edge-fade cues, so mobile readers and keyboard/AT users can
  // reach every column without document-level horizontal overflow. A table
  // that fits has the affordance stripped (no spurious tab stop, no chrome).
  function updateTableFits() {
    Array.prototype.forEach.call(document.querySelectorAll(".okf-tablewrap"), function (wrap) {
      var table = wrap.querySelector("table");
      if (!table) return;
      var frame = wrap.closest(".okf-tablewrap-frame") || wrap.parentNode;
      var fits = table.scrollWidth <= wrap.clientWidth + 1;
      wrap.classList.toggle("okf-tablewrap--fit", fits);
      wireTableScrollAffordance(frame, wrap, table);
      applyTableScrollAffordance(frame, wrap, table);
    });
  }

  // --- Phase 2: wide-table mobile accessibility affordance --------------
  // Two complementary affordances, both scoped to tables that actually
  // overflow their column:
  //   (a) PROGRAMMATIC: the scroll port (.okf-tablewrap) becomes role=region
  //       with an aria-label that names the table and says to scroll
  //       horizontally, plus tabindex=0 so keyboard users can focus it and
  //       arrow-scroll.
  //   (b) VISUAL: aria-hidden, pointer-events:none edge-fade cue overlays
  //       (left/right) on the NON-SCROLLING frame that are visible only on
  //       the side(s) where more content remains, fading as the reader
  //       scrolls to each end.
  // Cues are appended once per frame (idempotent via dataset) and their
  // visibility is driven entirely by toggled classes on the frame, so resize
  // / live patch / column-drag all stay in sync through updateTableFits.
  function wireTableScrollAffordance(frame, wrap, table) {
    if (frame.dataset.okfScrollCue === "1") return;
    frame.dataset.okfScrollCue = "1";
    ["left", "right"].forEach(function (side) {
      var cue = document.createElement("span");
      cue.className = "okf-tablewrap__cue okf-tablewrap__cue--" + side;
      cue.setAttribute("aria-hidden", "true");
      frame.appendChild(cue);
    });
    var raf = 0;
    // passive scroll listener on the SCROLL PORT: only flip the scrolled-edge
    // marker classes (cheap) on the frame; region role/aria-label are owned
    // by applyTableScrollAffordance.
    wrap.addEventListener("scroll", function () {
      if (raf) return;
      raf = requestAnimationFrame(function () { raf = 0; updateScrollCueState(frame, wrap, table); });
    }, { passive: true });
  }

  function tableAriaLabel(table) {
    var name = "";
    var caption = table.querySelector("caption");
    if (caption) name = collapseWs(caption.textContent);
    if (!name) {
      var title = document.querySelector(".okf-page__title");
      if (title) name = collapseWs(title.textContent);
    }
    if (!name) name = "Table";
    var cols = 0;
    if (table.tHead && table.tHead.rows[0]) cols = table.tHead.rows[0].cells.length;
    else {
      var firstRow = table.rows[0] || (table.tBodies[0] && table.tBodies[0].rows[0]);
      cols = firstRow ? firstRow.cells.length : 0;
    }
    var colPart = cols > 0 ? ", " + cols + (cols === 1 ? " column" : " columns") : "";
    return name + colPart + ". Scroll horizontally to view all columns.";
  }

  function applyTableScrollAffordance(frame, wrap, table) {
    var overflows = table.scrollWidth > wrap.clientWidth + 1;
    // Affordance marker classes live on the (non-scrolling) FRAME so the cue
    // selectors resolve and the cues stay pinned; the semantic region role /
    // tabindex / aria-label live on the SCROLL PORT (.okf-tablewrap).
    frame.classList.toggle("okf-tablewrap--scrolls", overflows);
    if (overflows) {
      if (wrap.getAttribute("role") !== "region") {
        wrap.setAttribute("role", "region");
        wrap.setAttribute("aria-label", tableAriaLabel(table));
        wrap.tabIndex = 0;
      }
      updateScrollCueState(frame, wrap, table);
    } else {
      if (wrap.getAttribute("role") === "region") {
        // Fitting table: strip the affordance so it is not an extra tab stop
        // and carries no stale "scroll" hint.
        wrap.removeAttribute("role");
        wrap.removeAttribute("aria-label");
        wrap.removeAttribute("tabindex");
      }
      frame.classList.remove("okf-tablewrap--scrolled-start", "okf-tablewrap--scrolled-end");
    }
  }

  function updateScrollCueState(frame, wrap, table) {
    var max = table.scrollWidth - wrap.clientWidth;
    if (max < 0) max = 0;
    var left = wrap.scrollLeft;
    frame.classList.toggle("okf-tablewrap--scrolled-start", left <= 1);
    frame.classList.toggle("okf-tablewrap--scrolled-end", left >= max - 1);
  }
  var fitTimer = null;
  window.addEventListener("resize", function () {
    if (fitTimer) clearTimeout(fitTimer);
    fitTimer = setTimeout(function () {
      updateTableFits();
      updateContentScrollports();
    }, 150);
  });

  // --- code blocks: copy button + language badge -------------------------
  function initCodeBlocks() {
    proseRoots().forEach(function (root) {
      Array.prototype.forEach.call(root.querySelectorAll("pre"), function (pre) {
        if (pre.dataset.okfEnhanced) return;
        if (pre.closest(".okf-codewrap")) return;
        pre.dataset.okfEnhanced = "1";
        var wrap = document.createElement("div");
        wrap.className = "okf-codewrap";
        pre.parentNode.insertBefore(wrap, pre);
        wrap.appendChild(pre);

        var tools = document.createElement("div");
        tools.className = "okf-code-tools";
        var code = pre.querySelector("code");
        var m = code && /language-([\w+-]+)/.exec(code.className || "");
        if (m) {
          var badge = document.createElement("span");
          badge.className = "okf-code-lang";
          badge.setAttribute("aria-hidden", "true");
          badge.textContent = m[1];
          tools.appendChild(badge);
        }
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "okf-code-copy";
        btn.textContent = "Copy";
        btn.setAttribute("aria-label", "Copy code to clipboard");
        btn.addEventListener("click", function () {
          copyText((code || pre).textContent || "", btn, "Copied ✓");
        });
        tools.appendChild(btn);
        wrap.appendChild(tools);
      });
    });
  }

  // --- images: click-to-zoom lightbox ------------------------------------
  var lightbox = null;
  var lightboxReturnFocus = null;
  // Registration on the shared window.OKFOverlayStack (created by theme.js,
  // the earliest viewer script) so ONE Escape closes only the topmost
  // registered overlay. The Appearance popover (theme.js) and the studio
  // command palette register here too; this lightbox registers itself while
  // open so a single Escape dismisses the actually-topmost aria-modal —
  // never this dialog plus a focus restore behind a higher modal.
  var lightboxOverlay = null;
  // Tab/Shift+Tab focus containment for the aria-modal lightbox: the only
  // focusable control is the close button, but this resolves focusables in
  // DOM order and wraps at the edges so Tab can NEVER reach #okf-theme or
  // any page control behind the modal.
  function _lightboxFocusables() {
    if (!lightbox) return [];
    var sel = 'button:not(:disabled), a[href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])';
    var nodes = lightbox.querySelectorAll(sel);
    var out = [];
    Array.prototype.forEach.call(nodes, function (n) {
      if (n.hasAttribute("hidden")) return;
      var cs;
      try { cs = window.getComputedStyle(n); } catch (e) { cs = null; }
      if (!cs || (cs.display !== "none" && cs.visibility !== "hidden")) out.push(n);
    });
    return out;
  }
  // Restore focus INTO the lightbox (its close button) — used when a higher
  // registered overlay (e.g. the Appearance menu) closes above this dialog so
  // focus never lands on #okf-theme behind the modal.
  function _focusIntoLightbox() {
    var f = _lightboxFocusables();
    var target = f.length ? f[0] : null;
    if (target && typeof target.focus === "function") {
      try { target.focus({ preventScroll: true }); } catch (e) {}
    }
  }
  function closeLightbox() {
    if (!lightbox) return;
    if (lightboxOverlay && window.OKFOverlayStack) {
      window.OKFOverlayStack.remove(lightboxOverlay);
    }
    lightboxOverlay = null;
    lightbox.remove();
    lightbox = null;
    // Close restores the EXACT opener (the image that was activated).
    if (lightboxReturnFocus && typeof lightboxReturnFocus.focus === "function") {
      try { lightboxReturnFocus.focus({ preventScroll: true }); } catch (e) {}
    }
    lightboxReturnFocus = null;
  }
  function openLightbox(img) {
    closeLightbox();
    lightboxReturnFocus = img;
    lightbox = document.createElement("div");
    lightbox.className = "okf-lightbox";
    lightbox.setAttribute("role", "dialog");
    lightbox.setAttribute("aria-modal", "true");
    lightbox.setAttribute("aria-label", img.alt || "Image preview");

    var fig = document.createElement("figure");
    fig.className = "okf-lightbox__figure";
    var full = document.createElement("img");
    full.src = img.src;
    full.alt = img.alt || "";
    fig.appendChild(full);
    if (img.alt) {
      var cap = document.createElement("figcaption");
      cap.textContent = img.alt;
      fig.appendChild(cap);
    }
    var close = document.createElement("button");
    close.type = "button";
    close.className = "okf-lightbox__close";
    close.textContent = "×";
    close.setAttribute("aria-label", "Close image preview");
    close.addEventListener("click", closeLightbox);

    lightbox.appendChild(close);
    lightbox.appendChild(fig);
    lightbox.addEventListener("click", function (e) {
      if (e.target === lightbox) closeLightbox();
    });
    // Focus containment (aria-modal="true"): Tab/Shift+Tab cycle inside the
    // dialog only, so focus can never reach #okf-theme or other page controls
    // behind it. Handled on the dialog element so it only fires while focus
    // is genuinely within the lightbox.
    lightbox.addEventListener("keydown", function (e) {
      if (e.key !== "Tab") return;
      e.preventDefault();
      var f = _lightboxFocusables();
      if (!f.length) { try { lightbox.focus(); } catch (err) {} return; }
      var cur = document.activeElement;
      var idx = f.indexOf(cur);
      var next;
      if (idx < 0) next = e.shiftKey ? f[f.length - 1] : f[0];
      else if (e.shiftKey) next = f[(idx - 1 + f.length) % f.length];
      else next = f[(idx + 1) % f.length];
      try { next.focus({ preventScroll: true }); } catch (err) {}
    });
    document.body.appendChild(lightbox);
    // Register as the topmost overlay so the Appearance popover (and any other
    // registered layer) yields Escape to this aria-modal while it is open.
    // focusIn lets the Appearance menu restore focus INTO this lightbox when
    // it closes above it, instead of to #okf-theme behind the modal.
    if (window.OKFOverlayStack) {
      lightboxOverlay = {
        close: function () { closeLightbox(); },
        focusIn: function () { _focusIntoLightbox(); }
      };
      window.OKFOverlayStack.push(lightboxOverlay);
    }
    try { close.focus({ preventScroll: true }); } catch (e) {}
  }
  document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !lightbox) return;
      // Yield to a higher registered overlay (symmetric with theme.js): when
      // something above this dialog owns the topmost slot, a single Escape
      // must close THAT layer, not this one. When this lightbox is topmost
      // (or nothing else registers), it closes and restores focus to the image.
      if (window.OKFOverlayStack && lightboxOverlay &&
          window.OKFOverlayStack.top() !== lightboxOverlay) return;
      e.preventDefault(); closeLightbox();
  });

  function initImages() {
    proseRoots().forEach(function (root) {
      Array.prototype.forEach.call(root.querySelectorAll("img"), function (img) {
        if (img.dataset.okfZoom) return;
        if (img.closest("a")) return; // linked images navigate, not zoom
        img.dataset.okfZoom = "1";
        img.classList.add("okf-zoomable");
        img.tabIndex = 0;
        img.setAttribute("role", "button");
        img.setAttribute("aria-label", "View full-size image" + (img.alt ? ": " + img.alt : ""));
        img.addEventListener("click", function () { openLightbox(img); });
        img.addEventListener("keydown", function (e) {
          if (e.key !== "Enter" && e.key !== " ") return;
          e.preventDefault();
          openLightbox(img);
        });
      });
    });
  }

  function initAll(reason, forceMermaid) {
    var token = beginCycle(reason || "scan", forceMermaid);
    var tasks = [];
    try {
      tasks.push(initMermaid(token));
      tasks.push(initHighlight(token));
      tasks.push(initMath(token));
      // Local enhancers are synchronous and idempotent. They intentionally
      // run after the pending state is published, so capture never observes
      // a false terminal state while a local wrapper is being installed.
      initTables();
      initCodeBlocks();
      initImages();
      updateContentScrollports();
    } catch (error) {
      tasks.push(Promise.reject(asRendererError(error, "renderer-failure")));
    }
    finishCycle(token, tasks);
  }

  function onThemeChanged(event) {
    var nextName = themeName();
    var previousName = currentThemeName;
    var previousMode = currentResolvedMode;
    var detail = event && event.detail;
    currentThemeName = nextName;
    currentResolvedMode = resolvedMode(nextName);
    // A real theme event, including a family-only or modifier-only change,
    // revokes every older renderer token. Mermaid receives force=true only
    // when the resolved light/dark mode changed; family-only changes therefore
    // do not rerender an already-rendered diagram.
    if (nextName === previousName && !detail) return;
    themeGeneration += 1;
    var resolvedModeChanged = previousMode !== currentResolvedMode;
    if (detail && detail.previousResolvedMode === detail.resolvedMode) {
      resolvedModeChanged = false;
    }
    initAll("theme", resolvedModeChanged);
  }

  // theme.js dispatches the semantic event. The observer is only a defensive
  // bridge for hosts that update the canonical root attribute without the
  // event; it never writes root state and is de-duplicated by themeName().
  // The resolved-mode branch below is the fenced equivalent of the former
  // direct initMermaid(true) call: the complete cycle also owns readiness.
  var themeObserver = null;
  if (typeof MutationObserver === "function" && document.documentElement) {
    themeObserver = new MutationObserver(function () {
      var nextName = themeName();
      if (nextName !== currentThemeName) onThemeChanged(null);
    });
    themeObserver.observe(document.documentElement, {
      attributes: true, attributeFilter: ["data-theme"],
    });
  }

  // Run after DOM is ready. The initial cycle publishes loading before any
  // external module/style request is started.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initAll("initial", false); }, { once: true });
  } else {
    initAll("initial", false);
  }

  // Re-run on live studio patches (when the concept body is re-rendered).
  window.addEventListener("okf-loom:bodyPatched", function () {
    bodyGeneration += 1;
    initAll("body", false);
  });

  // Mermaid SVGs contain theme-specific colours and are otherwise skipped by
  // initMermaid(). The semantic event is the only theme lifecycle input.
  window.addEventListener("okf-loom:themeChanged", onThemeChanged);

  // Phase 2 P1 test/debug hook: exposes the table-fit/affordance recompute so
  // browser proof can drive the wide-table affordance deterministically (the
  // alternative — waiting for the 150ms debounced resize handler — races the
  // studio's background canonical re-sync). Mirrors studio.js's
  // _rebuildSectionsToc hook pattern.
  window.okfRenderers = window.okfRenderers || {};
  window.okfRenderers._updateTableFits = updateTableFits;
  window.okfRenderers._updateContentScrollports = updateContentScrollports;
  window.okfRenderers._readiness = function () { return window.okfRendererReadiness || null; };
})();
