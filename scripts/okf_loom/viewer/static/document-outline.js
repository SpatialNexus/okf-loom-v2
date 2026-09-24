/* One document-local owner for concept heading traversal in every viewer target. */
(function () {
  "use strict";

  // `root`/`body` are LIVE bindings, re-resolved on every sync: concept pages
  // bind the stable SSR slot plus `.okf-page__body`; selected-document
  // surfaces (graph detail, single-file detail) bind whichever detail
  // document is currently visible and present the owner's own host beside it.
  var root = document.querySelector("[data-okf-document-outline]");
  var body = document.querySelector(".okf-page__body");
  var dynamicHost = null;

  // ---- Sticky chrome geometry (every document, outline or not) -----------
  // Shell wrapping, density, Studio loading, and Reading Focus change the
  // sticky footprint. Geometry owns scroll clearance; neither a timeout nor a
  // desktop constant can establish it. CSS also covers native heading links
  // and hash restores. The measured stack is the sticky global topbar plus
  // the sticky document viewbar (studio.css pins both; Focus hides the
  // topbar, so its measured height is 0 there). The same measurement drives
  // --okf-viewbar-top (the bar's sticky offset, stacked under the topbar) and
  // --okf-viewbar-h (consumed by the mobile outline disclosure's sticky top).
  // render.py always emits [data-okf-document-outline] (hidden + empty when
  // the body has no id-carrying headings), so this measurement MUST NOT
  // depend on outline visibility: with the vars unpublished, studio.css falls
  // back to the nominal --okf-topbar-h and a wrapped shell paints over the
  // pinned bar.
  var generation = 0;
  var activationOwner = "";
  var observedChrome = [];
  var shellObserver = typeof ResizeObserver !== "undefined" ? new ResizeObserver(updateOcclusion) : null;
  function observeChrome() {
    if (!shellObserver) return;
    [".okf-topbar", ".okf-concept-viewbar"].forEach(function (selector) {
      var node = document.querySelector(selector);
      if (node && observedChrome.indexOf(node) === -1) {
        observedChrome.push(node);
        shellObserver.observe(node);
      }
    });
  }
  var lastShellHeight = null;
  function updateOcclusion() {
    observeChrome();
    var shell = document.querySelector(".okf-topbar");
    var bar = document.querySelector(".okf-concept-viewbar");
    var shellHeight = shell ? shell.getBoundingClientRect().height : 0;
    var barHeight = bar ? bar.getBoundingClientRect().height : 0;
    var style = document.documentElement.style;
    style.setProperty("--okf-viewbar-top", shellHeight + "px");
    style.setProperty("--okf-viewbar-h", barHeight + "px");
    style.setProperty("--okf-document-occlusion", (shellHeight + barHeight + 16) + "px");
    var target = activationOwner && document.getElementById(activationOwner);
    if (lastShellHeight !== null && shellHeight !== lastShellHeight && target === document.activeElement &&
        target.getBoundingClientRect().top >= 0 && target.getBoundingClientRect().top < shellHeight + barHeight + 16) {
      target.scrollIntoView({ behavior: "auto", block: "start" });
    }
    lastShellHeight = shellHeight;
  }
  updateOcclusion();
  window.addEventListener("resize", updateOcclusion);

  // ---- R1/R2 destination admission ---------------------------------------
  // A hash or section intent is only ever re-admitted against the actually
  // displayed document: exactly ONE element with that id in the whole page
  // (duplicates and chrome never qualify), a real H1-H6 heading inside the
  // displayed body, and — for navigation intents — the renderer-owned label
  // binding the search result was proven against. Any failure falls back to
  // document-root reading and the unsafe fragment/intent is cleared WITHOUT
  // a history push. A native section href therefore carries a query intent
  // (?okf-section=&okf-rev=&okf-h=) instead of the raw fragment: the browser
  // has nothing to native-scroll to until admission publishes the real hash
  // via replaceState (no second history entry).
  var outlineData = (function () {
    function stableValue(v) {
      if (Array.isArray(v)) return "[" + v.map(stableValue).join(",") + "]";
      if (v && typeof v === "object") return "{" + Object.keys(v).sort().map(function (k) { return JSON.stringify(k) + ":" + stableValue(v[k]); }).join(",") + "}";
      return JSON.stringify(v);
    }
    function canonicalText(v) {
      return stableValue(v)
        .replace(/[\u0080-\uffff]/g, function (ch) { return "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"); })
        .replace(/</g, "\\u003c");
    }
    function intentHash(label) {
      // 64-bit FNV-1a, 16 lowercase hex — matches navigation.section_intent_hash.
      var digest = 0xcbf29ce484222325n, bytes = new TextEncoder().encode(String(label == null ? "" : label)), index;
      for (index = 0; index < bytes.length; index++) {
        digest ^= BigInt(bytes[index]);
        digest = (digest * 0x100000001b3n) & 0xffffffffffffffffn;
      }
      var hex = digest.toString(16);
      while (hex.length < 16) hex = "0" + hex;
      return hex;
    }
    var el = document.getElementById("okf-rendered-outline"), parsed;
    if (!el) return { data: null, intentHash: intentHash };
    if ((el.textContent || "").length > 128 * 1024) return { data: null, intentHash: intentHash };
    try { parsed = JSON.parse(el.textContent || ""); } catch (error) { return { data: null, intentHash: intentHash }; }
    if (canonicalText(parsed) !== (el.textContent || "")) return { data: null, intentHash: intentHash };
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || parsed.schema !== 1
        || Object.keys(parsed).sort().join(",") !== "concept,headings,schema,source_revision"
        || typeof parsed.concept !== "string" || !parsed.concept || parsed.concept.length > 512
        || !/^[A-Za-z0-9_][A-Za-z0-9_.-]*(\/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$/.test(parsed.concept)
        || typeof parsed.source_revision !== "string" || !parsed.source_revision
        || !/^[0-9a-f]{64}$/.test(parsed.source_revision)) return { data: null, intentHash: intentHash };
    // The heading map IS the destination proof: an outline without it (over
    // cap, absent) cannot admit any section intent.
    if (!parsed.headings || typeof parsed.headings !== "object" || Array.isArray(parsed.headings)) return { data: null, intentHash: intentHash };
    var ids = Object.keys(parsed.headings);
    if (ids.length > 1000) return { data: null, intentHash: intentHash };
    for (var i = 0; i < ids.length; i++) {
      if (unsafeFragmentId(ids[i]) || ["__proto__", "constructor", "prototype"].indexOf(ids[i]) !== -1
          || typeof parsed.headings[ids[i]] !== "string"
          || unsafeFragmentId(parsed.headings[ids[i]]) || parsed.headings[ids[i]].length > 256) return { data: null, intentHash: intentHash };
    }
    return { data: parsed, intentHash: intentHash };
  })();

  function unsafeFragmentId(fragment) {
    return typeof fragment !== "string" || !fragment || fragment.length > 512
      || /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/.test(fragment);
  }

  // The one element a fragment may address, or null: unique in the whole
  // document, H1-H6, inside the currently bound (displayed) body, and — when
  // an expected label is proven by the transport — carrying exactly that
  // renderer-owned text.
  function admissibleHeading(fragment, expectedLabel) {
    if (!body || unsafeFragmentId(fragment)) return null;
    var selector;
    try { selector = '[id="' + (typeof CSS !== "undefined" && CSS.escape ? CSS.escape(fragment) : fragment) + '"]'; }
    catch (error) { return null; }
    var inBody = body.querySelectorAll(selector);
    if (inBody.length !== 1) return null;
    var target = inBody[0];
    if (document.querySelectorAll(selector).length !== 1) return null;
    if (!/^[Hh][1-6]$/.test(target.tagName)) return null;
    if (target.closest("[hidden]")) return null;
    if (expectedLabel != null) {
      var copy = target.cloneNode(true);
      Array.prototype.forEach.call(copy.querySelectorAll(".okf-heading-anchor"), function (anchor) { anchor.remove(); });
      if ((copy.textContent || "").replace(/\s+/g, " ").trim() !== expectedLabel) return null;
    }
    return target;
  }

  function searchParamsWithoutIntent(search) {
    var params = new URLSearchParams(search || "");
    ["okf-section", "okf-rev", "okf-h", "okf-c"].forEach(function (name) { params.delete(name); });
    var query = params.toString();
    return query ? "?" + query : "";
  }

  function restoreFromLocation() {
    var params, id, target;
    // Re-resolve the displayed document first: on selected-document surfaces
    // (graph/single-file detail) the previous binding may point at a body
    // that is no longer presented (selection cleared by a traversal), and a
    // stale admission decision here would strip a hash the URL owner is
    // about to admit. Concept pages re-resolve to the same body.
    bindDocument();
    try { params = new URLSearchParams(location.search); } catch (error) { params = null; }
    if (params && params.has("okf-section")) {
      // A section action's bounded navigation intent. Every intent key must
      // appear EXACTLY once (duplicate keys reject the whole intent), and
      // the destination proof below is mandatory: the caller-supplied hash
      // is a short accidental-drift binding, never authorization.
      id = singleIntentParam(params, "okf-section") || "";
      var expectedRevision = singleIntentParam(params, "okf-rev");
      var expectedConcept = singleIntentParam(params, "okf-c");
      var expectedHash = singleIntentParam(params, "okf-h");
      target = (id && expectedRevision !== null && expectedConcept !== null
                && expectedHash !== null
                && expectedRevision.length <= 256
                && expectedConcept.length <= 512 && !unsafeFragmentId(expectedConcept)
                && /^[0-9a-f]{16}$/.test(expectedHash)
                && !unsafeFragmentId(id))
        ? admitOutlineDestination(id, expectedConcept, expectedRevision, expectedHash)
        : null;
      var cleanQuery = searchParamsWithoutIntent(location.search);
      if (target) {
        try {
          history.replaceState(Object.assign({}, history.state, {
            okfSectionIntent: { fragment: id, revision: expectedRevision,
                                heading: normalizeHeadingLabelStrict(target) }
          }), "", location.pathname + cleanQuery + "#" + encodeURIComponent(id));
        } catch (error) { /* keep intent: root reading is safe */ }
        admitTarget(target, id);
      } else {
        try { history.replaceState(history.state, "", location.pathname + cleanQuery); } catch (error) { /* root reading */ }
      }
      return;
    }
    if (!location.hash) return;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch (error) { return; }
    var stamp = ownedIntentStamp();
    if (stamp && typeof stamp === "object" && stamp.fragment === id) {
      if (!outlineData.data) {
        // An owned section stamp on a surface this outline does not prove
        // (single-file/graph detail): the hash belongs to the graph URL
        // owner, which re-reads the same stamp for its strict admission.
        // Leave the URL alone.
        return;
      }
      // Traversal (Back/Forward/reload) into an OWNED section entry: the
      // strict destination admission applies again — same proof, same rules;
      // a refusal strips the hash (root reading) without a history push.
      target = (typeof stamp.revision === "string" && typeof stamp.heading === "string")
        ? reAdmitOwnedSectionEntry(id, stamp)
        : null;
      if (target) {
        admitTarget(target, id);
      } else {
        try { history.replaceState(history.state, "", location.pathname + location.search); } catch (error) { /* root reading */ }
      }
      return;
    }
    if (!body) {
      // Selected-document surface (graph/single-file detail): the displayed
      // document is owned by the graph URL/selected owner, which performs
      // the same admission after it renders the body. Leave the URL alone.
      return;
    }
    // Plain document-local hash (source footnotes, citations, legacy deep
    // links): the shipped behavior — focus the destination when it exists
    // inside this document, never rewrite the URL, never a section action.
    target = document.getElementById(id);
    if (!target || !body.contains(target)) return;
    updateOcclusion();
    activationOwner = id;
    if (root) setCurrent(id, false);
    target.tabIndex = -1;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ behavior: "auto", block: "start" });
  }

  function singleIntentParam(params, name) {
    var values = params.getAll(name);
    return values.length === 1 ? values[0] : null;
  }

  function ownedIntentStamp() {
    try {
      var state = history.state;
      return state && typeof state === "object" ? state.okfSectionIntent || null : null;
    } catch (error) { return null; }
  }

  function outlineProofGate(expectedConcept, expectedRevision) {
    // The destination proof is REQUIRED, never optional: canonical parsed
    // outline metadata naming this page's canonical concept, carrying the
    // matching per-source revision and the unique renderer-owned heading
    // allowlist. Missing, over-cap, malformed, or identity-mismatched proof
    // refuses every intent; the FNV label hash only binds accidental drift.
    var outline = outlineData.data;
    if (!outline
        || typeof outline.concept !== "string" || !outline.concept
        || (expectedConcept !== null && outline.concept !== expectedConcept)
        || typeof outline.source_revision !== "string" || !outline.source_revision
        || outline.source_revision !== expectedRevision
        || !outline.headings) {
      return null;
    }
    return outline;
  }

  function admitOutlineDestination(fragment, expectedConcept, expectedRevision,
                                   expectedHash) {
    var outline = outlineProofGate(expectedConcept, expectedRevision);
    if (!outline
        || !Object.prototype.hasOwnProperty.call(outline.headings, fragment)) {
      return null;
    }
    var owned = outline.headings[fragment];
    if (!owned || owned.length > 256) return null;
    var admitted = admissibleHeading(fragment, owned);
    if (!admitted || outlineData.intentHash(owned) !== expectedHash) return null;
    return admitted;
  }

  function reAdmitOwnedSectionEntry(fragment, stamp) {
    var outline = outlineProofGate(null, stamp.revision);
    if (!outline
        || !Object.prototype.hasOwnProperty.call(outline.headings, fragment)) {
      return null;
    }
    var owned = outline.headings[fragment];
    if (owned !== stamp.heading) return null;
    return admissibleHeading(fragment, owned);
  }

  function normalizeHeadingLabelStrict(heading) {
    var copy = heading.cloneNode(true);
    Array.prototype.forEach.call(copy.querySelectorAll(".okf-heading-anchor"), function (anchor) { anchor.remove(); });
    return (copy.textContent || "").replace(/\s+/g, " ").trim();
  }

  function admitTarget(target, id) {
    updateOcclusion();
    activationOwner = id;
    if (root) setCurrent(id, false);
    target.tabIndex = -1;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ behavior: "auto", block: "start" });
  }

  window.addEventListener("hashchange", restoreFromLocation);
  window.addEventListener("popstate", restoreFromLocation);
  window.addEventListener("load", restoreFromLocation);
  window.addEventListener("pagehide", function () {
    generation++;
    disconnectObserver();
    if (shellObserver) shellObserver.disconnect();
    observedChrome = [];
    if (chromeWaiter) { chromeWaiter.disconnect(); chromeWaiter = null; }
  });
  window.addEventListener("pageshow", function (event) {
    // BFCache pagehide retired the observer. Rebuild from the restored DOM
    // under a fresh generation while preserving valid local outline state.
    if (event.persisted) {
      observedChrome = [];
      updateOcclusion();
      sync();
      mountJump();
      watchForChrome();
    }
  });

  window.okfDocumentOutline = {
    rebuild: function () { sync(); },
    updateOcclusion: updateOcclusion,
    get generation() { return generation; },
    get observerActive() { return !!(root && body) && observer !== null; }
  };

  // ---- Outline state -----------------------------------------------------
  var FILTER_THRESHOLD = 8;
  var compactQuery = window.matchMedia ? window.matchMedia("(max-width: 900px)") : null;
  var observer = null;
  var activeId = "";
  var filter = null;
  var status = null;
  var userDisclosureOwned = false;
  var disclosureBootSettled = false;
  var jump = null;            // viewbar Sections control (serve concept pages)
  var chromeWaiter = null;    // waits for studio.js's viewbar when it boots late
  var viewObserver = null;    // Rendered/Source/Split presentation changes

  function headings() {
    if (!body) return [];
    return Array.prototype.filter.call(
      body.querySelectorAll("h1[id],h2[id],h3[id],h4[id],h5[id],h6[id]"),
      function (heading) { return !!heading.id && !!(heading.textContent || "").trim(); }
    );
  }

  function headingLabel(heading) {
    var copy = heading.cloneNode(true);
    Array.prototype.forEach.call(copy.querySelectorAll(".okf-heading-anchor"), function (anchor) {
      anchor.remove();
    });
    return (copy.textContent || "").replace(/\s+/g, " ").trim();
  }

  function isDisplayed(el) {
    if (!el) return false;
    if (el.closest("[hidden]")) return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }

  // The document the outline currently owns. Concept pages have exactly one
  // body; selected-document surfaces (graph detail, single-file detail) can
  // hold several `.okf-page__body` nodes at once (selected concept, reserved
  // index/log document), of which exactly one is presented.
  function activeDocumentBody() {
    var candidates = document.querySelectorAll(".okf-page__body");
    for (var index = 0; index < candidates.length; index++) {
      if (isDisplayed(candidates[index])) return candidates[index];
    }
    return null;
  }

  function bindDocument() {
    if (root && !dynamicHost) {
      // Concept page: the SSR slot is the stable host (F8: present even for
      // headingless bodies); its body is the page body. A template override
      // that omits the slot keeps the old contract: no outline, geometry only.
      body = document.querySelector(".okf-page__body") || body;
      return;
    }
    // Selected-document surface: bind the PRESENTED detail document on every
    // sync. The host is created lazily and moves beside whichever document is
    // displayed now — the pane it was created in may already be hidden (e.g.
    // an outline born beside a reserved index document, then a concept is
    // selected), so first-in-DOM queries would silently strand it.
    var active = activeDocumentBody();
    if (!active || !active.classList.contains("okf-graph-detail__body")) {
      body = null;
      return;
    }
    body = active;
    if (!root) {
      root = ensureDynamicHost(active);
    } else if (root.nextSibling !== active || root.parentNode !== active.parentNode) {
      active.parentNode.insertBefore(root, active);
    }
  }

  function ensureDynamicHost(activeBody) {
    var host = document.createElement("details");
    host.className = "okf-onthispage okf-onthispage--detail";
    host.setAttribute("aria-label", "On this page");
    host.setAttribute("data-okf-document-outline", "");
    host.setAttribute("data-okf-outline-empty", "");
    host.hidden = true;
    // Same summary anatomy as the SSR host: label + live current-section span.
    var summary = document.createElement("summary");
    var label = document.createElement("span");
    label.textContent = "On this page";
    var current = document.createElement("span");
    current.className = "okf-onthispage__current";
    current.setAttribute("aria-hidden", "true");
    summary.appendChild(label);
    summary.appendChild(current);
    host.appendChild(summary);
    activeBody.parentNode.insertBefore(host, activeBody);
    // Record dynamic ownership: from here on every sync re-resolves the
    // displayed body (the concept-page branch above must never run again on
    // this surface, or it would bind the first `.okf-page__body` in DOM
    // order instead of the presented one).
    dynamicHost = host;
    wireDisclosure(host);
    return host;
  }

  function sync() {
    bindDocument();
    rebuild();
  }

  function disconnectObserver() {
    if (!observer) return;
    try { observer.disconnect(); } catch (error) { /* detached observer */ }
    observer = null;
  }

  function ensureEnhancements(count) {
    var nav = root.querySelector(".okf-onthispage__nav");
    if (!nav) return;
    status = root.querySelector(".okf-onthispage__status");
    if (!status) {
      status = document.createElement("p");
      status.className = "okf-visually-hidden okf-onthispage__status";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      status.setAttribute("aria-atomic", "true");
      nav.insertBefore(status, nav.firstChild);
    }
    var field = root.querySelector(".okf-onthispage__filter");
    if (count >= FILTER_THRESHOLD && !field) {
      var label = document.createElement("label");
      label.className = "okf-onthispage__filter-label";
      label.appendChild(document.createTextNode("Filter sections"));
      field = document.createElement("input");
      field.type = "search";
      field.className = "okf-onthispage__filter";
      field.setAttribute("autocomplete", "off");
      field.setAttribute("placeholder", "Filter sections");
      label.appendChild(field);
      nav.insertBefore(label, status.nextSibling);
      field.addEventListener("input", applyFilter);
      filter = field;
    } else if (count < FILTER_THRESHOLD && field) {
      field.parentNode.remove();
      filter = null;
    } else {
      filter = field;
    }
  }

  function ownedNavs() {
    var navs = [];
    if (root) {
      var nav = root.querySelector(".okf-onthispage__nav");
      if (nav) navs.push(nav);
    }
    if (jump) {
      var jumpNav = jump.panel.querySelector(".okf-onthispage__nav");
      if (jumpNav) navs.push(jumpNav);
    }
    return navs;
  }

  function links() {
    var found = [];
    ownedNavs().forEach(function (nav) {
      Array.prototype.push.apply(found, Array.prototype.slice.call(nav.querySelectorAll("a.okf-onthispage__link")));
    });
    return found;
  }

  function listLinks(nav) {
    return Array.prototype.slice.call(nav.querySelectorAll("a.okf-onthispage__link"));
  }

  function visibleLinksIn(nav) {
    return listLinks(nav).filter(function (link) { return !link.closest("li").hidden; });
  }

  // One roving target PER surface list: the disclosure and the viewbar
  // popover each keep exactly one tab-stop link so either surface can be
  // traversed with arrows without stealing the other's stop.
  function setRovingTarget(preferredId) {
    var targetHref = preferredId ? "#" + preferredId : null;
    ownedNavs().forEach(function (nav) {
      var available = visibleLinksIn(nav);
      var target = null;
      for (var index = 0; index < available.length; index++) {
        if (targetHref && available[index].getAttribute("href") === targetHref) { target = available[index]; break; }
      }
      if (!target) target = available[0] || null;
      listLinks(nav).forEach(function (link) { link.tabIndex = link === target ? 0 : -1; });
    });
  }

  function setCurrent(id, announce) {
    if (!id) return;
    activeId = id;
    var currentText = "";
    links().forEach(function (link) {
      var matches = link.getAttribute("href") === "#" + id;
      link.classList.toggle("is-active", matches);
      if (matches) {
        link.setAttribute("aria-current", "location");
        currentText = (link.textContent || "").trim();
      } else {
        link.removeAttribute("aria-current");
      }
    });
    setRovingTarget(id);
    // `root` is legitimately absent on selected-document surfaces until the
    // dynamic host is created beside a displayed body — a boot-time plain
    // hash (e.g. the concept list's #detail-content deep link) must not
    // throw here and kill the scripts bundled after this file.
    var current = root ? root.querySelector(".okf-onthispage__current") : null;
    if (current) current.textContent = currentText ? " · " + currentText : "";
    if (jump) { jump.current.textContent = currentText; jump.current.hidden = !currentText; }
    if (announce && status && currentText) status.textContent = "Current section: " + currentText;
  }

  function applyFilter() {
    // The filter field lives only in the near-header disclosure, so it owns
    // ONLY that list. The viewbar Sections popover keeps complete section
    // access — it must never silently inherit the disclosure's filter state
    // (below-fold wayfinding stays recoverable while a filter is active).
    var nav = root ? root.querySelector(".okf-onthispage__nav") : null;
    if (!nav) return;
    var query = filter ? filter.value.trim().toLocaleLowerCase() : "";
    listLinks(nav).forEach(function (link) {
      link.closest("li").hidden = !!query && (link.textContent || "").toLocaleLowerCase().indexOf(query) === -1;
    });
    setRovingTarget(activeId);
    var empty = root.querySelector(".okf-onthispage__empty");
    if (!empty) {
      empty = document.createElement("p");
      empty.className = "okf-onthispage__empty";
      empty.textContent = "No matching sections.";
      nav.appendChild(empty);
    }
    empty.hidden = !!visibleLinksIn(nav).length;
  }

  function activate(link) {
    var id = (link.getAttribute("href") || "").slice(1);
    var target = id && document.getElementById(id);
    if (!target || !body.contains(target)) return;
    activationOwner = id;
    setCurrent(id, true);
    if (compactQuery && compactQuery.matches) root.open = false;
    if (jump) closeJump(false);
    updateOcclusion();
    target.tabIndex = -1;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ behavior: "auto", block: "start" });
    if (window.history && window.history.pushState && location.hash !== "#" + encodeURIComponent(id)) {
      window.history.pushState(history.state, "", "#" + encodeURIComponent(id));
    }
  }

  function wireLink(link) {
    link.addEventListener("click", function (event) {
      if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); activate(link);
    });
    link.addEventListener("keydown", function (event) {
      var nav = link.closest(".okf-onthispage__nav") || link.parentNode;
      var available = visibleLinksIn(nav);
      var index = available.indexOf(link);
      var next = null;
      if (event.key === "ArrowDown") next = available[Math.min(available.length - 1, index + 1)];
      else if (event.key === "ArrowUp") next = available[Math.max(0, index - 1)];
      else if (event.key === "Home") next = available[0];
      else if (event.key === "End") next = available[available.length - 1];
      else if (event.key === "Enter") { event.preventDefault(); activate(link); return; }
      if (next) {
        event.preventDefault();
        listLinks(nav).forEach(function (item) { item.tabIndex = item === next ? 0 : -1; });
        next.focus();
      }
    });
  }

  function buildNav(currentHeadings) {
    var nav = document.createElement("nav");
    nav.className = "okf-onthispage__nav";
    nav.setAttribute("aria-label", "Document sections");
    var list = document.createElement("ol");
    list.className = "okf-onthispage__list";
    var minLevel = Math.min.apply(null, currentHeadings.map(function (heading) {
      return parseInt(heading.tagName.slice(1), 10);
    }));
    currentHeadings.forEach(function (heading) {
      var item = document.createElement("li");
      var depth = Math.max(0, parseInt(heading.tagName.slice(1), 10) - minLevel);
      item.className = "okf-onthispage__item" + (depth ? " okf-onthispage--l" + depth : "");
      var link = document.createElement("a");
      link.className = "okf-onthispage__link";
      link.href = "#" + heading.id;
      link.textContent = headingLabel(heading);
      item.appendChild(link);
      list.appendChild(item);
    });
    nav.appendChild(list);
    return nav;
  }

  function buildList(currentHeadings) {
    var nav = buildNav(currentHeadings);
    var oldNav = root.querySelector(".okf-onthispage__nav");
    if (oldNav) oldNav.replaceWith(nav); else root.appendChild(nav);
  }

  function observe(currentHeadings, ownGeneration) {
    if (!("IntersectionObserver" in window)) return;
    var visible = {};
    observer = new IntersectionObserver(function (entries) {
      if (ownGeneration !== generation) return;
      if (activationOwner) {
        var activated = document.getElementById(activationOwner);
        var rect = activated && activated.getBoundingClientRect();
        if (rect && rect.bottom > 0 && rect.top < window.innerHeight) return;
        activationOwner = "";
      }
      entries.forEach(function (entry) { visible[entry.target.id] = entry.isIntersecting; });
      var current = currentHeadings.find(function (heading) { return visible[heading.id]; });
      if (current) setCurrent(current.id, false);
    }, { rootMargin: "-10% 0px -70% 0px" });
    currentHeadings.forEach(function (heading) { observer.observe(heading); });
  }

  // Collapse every owned surface to the stable hidden host: no stale links,
  // no chrome, no observer. Used for zero-heading bodies AND for selected-
  // document surfaces where nothing is presented right now.
  function hideOutline() {
    root.hidden = true;
    root.open = false;
    root.setAttribute("data-okf-outline-empty", "");
    var staleNav = root.querySelector(".okf-onthispage__nav");
    if (staleNav) staleNav.remove();
    filter = null;
    status = null;
    if (jump) { jump.wrap.hidden = true; closeJump(false); }
    var focused = document.activeElement;
    if (focused && ((root && root.contains(focused)) ||
        (jump && jump.wrap.contains(focused)))) {
      var title = document.querySelector(".okf-page__title");
      if (title) { title.tabIndex = -1; title.focus({ preventScroll: true }); }
    }
  }

  function rebuild() {
    var ownGeneration = ++generation;
    if (!root) return;
    // Lifecycle invariant: the old observer is retired before any owned DOM
    // is torn down or hidden.
    disconnectObserver();
    if (!body) {
      // Selected-document surface with nothing presented (the detail pane
      // cleared between selections): retire the observer and hide stale
      // navigation before returning.
      hideOutline();
      return;
    }
    var focused = document.activeElement;
    var focusedFilter = !!(filter && focused === filter);
    var focusInsideOutline = !!(focused && ((root && root.contains(focused)) ||
      (jump && jump.wrap.contains(focused))));
    var focusedInJump = !!(jump && focused && jump.wrap.contains(focused) && focused !== jump.toggle);
    var focusedId = focused && focusInsideOutline && focused.matches && focused.matches("a[href^='#']")
      ? (focused.getAttribute("href") || "").slice(1) : "";
    var summary = root.querySelector("summary");
    var wasOpen = root.open;
    var filterValue = filter ? filter.value : "";
    var bodyPresented = isDisplayed(body);

    var currentHeadings = headings();
    if (!currentHeadings.length || !bodyPresented) {
      // Zero headings (including a body that is not the presented view, e.g.
      // Source mode) collapse to the stable hidden host: no stale links, no
      // chrome, no observer. Source/Split must not advertise navigation into
      // content that is not on screen.
      hideOutline();
      return;
    }

    root.hidden = false;
    root.removeAttribute("data-okf-outline-empty");
    buildList(currentHeadings);
    root.open = wasOpen;
    ensureEnhancements(currentHeadings.length);
    if (jump) { jump.wrap.hidden = false; buildJumpNav(currentHeadings); }
    // One wiring pass over every owned link (the jump nav was just replaced,
    // so nothing is wired twice).
    links().forEach(wireLink);
    if (filter && filterValue) { filter.value = filterValue; applyFilter(); }
    setCurrent(currentHeadings.some(function (heading) { return heading.id === activeId; }) ? activeId : currentHeadings[0].id, false);
    observe(currentHeadings, ownGeneration);

    if (focusedFilter) {
      if (filter) filter.focus({ preventScroll: true });
      else if (summary) { summary.tabIndex = -1; summary.focus({ preventScroll: true }); }
    } else if (focusedId) {
      // Restore focus to the rebuilt link in the surface that owned it.
      var restored = null;
      var orderedNavs = focusedInJump ? ownedNavs().slice().reverse() : ownedNavs();
      for (var index = 0; index < orderedNavs.length && !restored; index++) {
        restored = listLinks(orderedNavs[index]).find(function (link) {
          return link.getAttribute("href") === "#" + focusedId;
        }) || null;
      }
      if (restored) restored.focus({ preventScroll: true });
      else if (jump && focusedInJump) jump.toggle.focus({ preventScroll: true });
      else if (summary) { summary.tabIndex = -1; summary.focus({ preventScroll: true }); }
    } else if (focusInsideOutline && summary && !focusedInJump) {
      summary.tabIndex = -1;
      summary.focus({ preventScroll: true });
    }
  }

  // ---- Viewbar Sections popover (U3) -------------------------------------
  // The outline sits near the header; long documents scroll it far away. On
  // studio-capable concept pages the owner adds one restrained control to the
  // pinned document viewbar: the current section plus a popover list built
  // from the SAME heading state as the disclosure (never a second outline).
  // Default collapsed; Escape closes with focus returned to the toggle; the
  // popover claims Escape before any global handler while it is open.

  function jumpOpen() { return !!jump && !jump.panel.hidden; }

  function closeJump(restoreFocus) {
    if (!jump || jump.panel.hidden) return;
    jump.panel.hidden = true;
    jump.toggle.setAttribute("aria-expanded", "false");
    if (restoreFocus) jump.toggle.focus({ preventScroll: true });
  }

  // Replace the popover list from the shared heading state. Wiring is left to
  // the single `links().forEach(wireLink)` pass in rebuild (or the explicit
  // fresh-nav pass in mountJump), so a link is never wired twice.
  function buildJumpNav(currentHeadings) {
    var nav = buildNav(currentHeadings);
    nav.setAttribute("aria-label", "Document sections");
    var oldNav = jump.panel.querySelector(".okf-onthispage__nav");
    if (oldNav) oldNav.replaceWith(nav); else jump.panel.appendChild(nav);
  }

  function openJump() {
    if (!jump) return;
    var nav = jump.panel.querySelector(".okf-onthispage__nav");
    if (!nav) { buildJumpNav(headings()); links().forEach(wireLink); }
    setRovingTarget(activeId);
    jump.panel.hidden = false;
    jump.toggle.setAttribute("aria-expanded", "true");
    var target = visibleLinksIn(jump.panel.querySelector(".okf-onthispage__nav"))[0];
    if (target) target.focus({ preventScroll: true });
  }

  function mountJump() {
    if (jump || !root || dynamicHost) return;
    if (!document.querySelector("[data-okf-studio-capability='available']")) return;
    var bar = document.querySelector(".okf-concept-viewbar");
    if (!bar) return;
    var wrap = document.createElement("div");
    wrap.className = "okf-outline-jump";
    wrap.setAttribute("data-okf-outline-jump", "");
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "okf-studiobtn okf-outline-jump__toggle";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", "okf-outline-jump-panel");
    var label = document.createElement("span");
    label.className = "okf-outline-jump__label";
    label.textContent = "Sections";
    var current = document.createElement("span");
    current.className = "okf-outline-jump__current";
    current.hidden = true;
    toggle.appendChild(label);
    toggle.appendChild(current);
    var panel = document.createElement("div");
    panel.id = "okf-outline-jump-panel";
    panel.className = "okf-outline-jump__panel";
    panel.hidden = true;
    wrap.appendChild(toggle);
    wrap.appendChild(panel);
    bar.appendChild(wrap);
    jump = { wrap: wrap, toggle: toggle, current: current, panel: panel };

    toggle.addEventListener("click", function () {
      if (jumpOpen()) closeJump(true); else openJump();
    });
    // High-priority Escape: while the popover is open it owns Escape; the
    // event never reaches global palette/panel handlers.
    wrap.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (!jumpOpen()) return;
      event.preventDefault();
      event.stopPropagation();
      closeJump(true);
    });
    wrap.addEventListener("focusout", function (event) {
      if (!jumpOpen()) return;
      if (!event.relatedTarget || !wrap.contains(event.relatedTarget)) closeJump(false);
    });
    document.addEventListener("pointerdown", function (event) {
      if (jumpOpen() && !wrap.contains(event.target)) closeJump(false);
    });
    var view = document.querySelector(".okf-view");
    if (view && !viewObserver && typeof MutationObserver !== "undefined") {
      viewObserver = new MutationObserver(function () { sync(); });
      viewObserver.observe(view, { attributes: true, attributeFilter: ["data-okf-view"] });
    }
    jump.wrap.hidden = root.hidden;
    jump.current.hidden = true;
    if (!root.hidden) {
      // The outline may have been rebuilt before the viewbar existed; build
      // the popover list once and adopt the current-section state.
      var currentHeadings = headings();
      if (currentHeadings.length) {
        buildJumpNav(currentHeadings);
        listLinks(jump.panel.querySelector(".okf-onthispage__nav")).forEach(wireLink);
        setRovingTarget(activeId);
        if (activeId) setCurrent(activeId, false);
      }
    }
  }

  function watchForChrome() {
    if (chromeWaiter || jump) return;
    if (!document.querySelector("[data-okf-studio-capability='available']")) return;
    if (typeof MutationObserver === "undefined") return;
    chromeWaiter = new MutationObserver(function () {
      mountJump();
      if (jump && chromeWaiter) { chromeWaiter.disconnect(); chromeWaiter = null; }
    });
    chromeWaiter.observe(document.body, { childList: true, subtree: true });
  }

  // ---- Disclosure ownership ----------------------------------------------

  function wireDisclosure(target) {
    if (!target || target.__okfOutlineWired) return;
    target.__okfOutlineWired = true;
    target.addEventListener("toggle", function () {
      if (disclosureBootSettled) userDisclosureOwned = true;
    });
    target.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (filter && filter.value) {
        event.preventDefault();
        filter.value = "";
        applyFilter();
        return;
      }
      if (compactQuery && compactQuery.matches && target.open) {
        event.preventDefault();
        target.open = false;
        var summary = target.querySelector("summary");
        if (summary) summary.focus({ preventScroll: true });
      }
    });
  }

  function syncDisclosure() {
    if (userDisclosureOwned || !root) return;
    root.open = false;
  }
  if (compactQuery) {
    if (compactQuery.addEventListener) compactQuery.addEventListener("change", syncDisclosure);
    else if (compactQuery.addListener) compactQuery.addListener(syncDisclosure);
  }

  // ---- Boot ---------------------------------------------------------------
  // One lifecycle: bind, rebuild (handles the stable hidden host, populated
  // disclosures, and every 0 -> many -> 0 -> many transition), then keep the
  // disclosure default-collapsed until the reader takes it over.
  if (root) wireDisclosure(root);
  sync();
  syncDisclosure();
  setCurrent((location.hash || "").slice(1) || (headings()[0] && headings()[0].id), false);
  setTimeout(function () { disclosureBootSettled = true; }, 0);
  mountJump();
  watchForChrome();
  window.addEventListener("okf-loom:bodyPatched", sync);
}());
