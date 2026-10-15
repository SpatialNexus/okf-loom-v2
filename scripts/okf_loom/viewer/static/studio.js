/* OKF Studio - the studio UI (spec §8 / §9 / §12 / §13).
 *
 * Owns the presentation layer; live.js owns the SSE transport + the
 * no-refresh body patch. studio.js subscribes to window.okfLoomLive for
 * presence/activity/comment/graph events and renders every studio surface.
 *
 * Surfaces:
 *   - Studio bar: presence chip, view-mode switch (Rendered|Source|Split),
 *     Comments / Changes panel toggles, connection indicator, palette btn.
 *   - Comments (§9): selection → "Comment for agent" affordance, composer
 *     with optimistic posting, margin markers, panel listing open/resolved
 *     per concept + across the bundle, filterable.
 *   - Presence (current spec §12): status chip; focused concept highlighted.
 *   - Change list (§12.2): live timeline from /__data/events with one-click
 *     Undo (single + group) where undoable; filterable.
 *   - Command palette (§13.4): Ctrl/Cmd-K, keyboard-first.
 *   - Extension API (§13.6): okfLoomStudio.register(kind, impl). Wired kinds:
 *     {panel, viewMode}. Reserved (forward-compat, accepted + warned):
 *     {toolbar, graphDecorator, suggestionRenderer}.
 *   - Themes (§13.5): owned entirely by viewer/static/theme.js (Ledger
 *     palettes bone/slate/clay/plum + system). This file only calls the
 *     theme.js API and registers its own overlays into the shared
 *     window.OKFOverlayStack so the theme Appearance menu can yield Escape
 *     to the actually-topmost studio modal.
 *
 * Security: untrusted strings (comment bodies, summaries, ids) go through
 * textContent only. The only innerHTML assignment is the server-rendered
 * concept body (same escaped renderer as the initial page - §7.3). All
 * mutating fetches attach X-OKF-Token from window.__OKF_LOOM_STUDIO__.token.
 *
 * Vanilla ES module, no framework, no bundler. Degrades gracefully: a
 * no-op when window.__OKF_LOOM_STUDIO__ is absent.
 */
(function () {
  "use strict";

  // CSP-safe bootstrap read (see live.js for rationale). studio.js reuses
  // window.__OKF_LOOM_STUDIO__ once live.js publishes it, but reads the data
  // block directly too so the two modules are independently robust.
  const BOOT_NODE = document.getElementById("okf-studio-bootstrap");
  function readBoot() {
    if (window.__OKF_LOOM_STUDIO__ && typeof window.__OKF_LOOM_STUDIO__ === "object") {
      return window.__OKF_LOOM_STUDIO__;
    }
    const node = document.getElementById("okf-studio-bootstrap");
    if (node) {
      try {
        const cfg = JSON.parse(node.textContent || "{}");
        window.__OKF_LOOM_STUDIO__ = cfg;
        return cfg;
      } catch (e) { /* fall through */ }
    }
    return null;
  }
  function publishStudioStatus(status, code, error) {
    const detail = Object.freeze({
      status,
      ready: status === "ready",
      code: code || null,
      message: error ? String(error.message || error) : null,
      generation: 1,
    });
    window.okfStudioReadiness = detail;
    window.okfStudioStatus = detail;
    const root = document.documentElement;
    const body = document.body;
    [root, body].forEach(function (node) {
      if (!node || !node.dataset) return;
      node.dataset.okfStudioStatus = status;
      node.dataset.okfStudioGeneration = "1";
      if (detail.code) node.dataset.okfStudioCode = detail.code;
      else delete node.dataset.okfStudioCode;
    });
    try {
      window.dispatchEvent(new CustomEvent("okf-loom:studioStatus", { detail }));
    } catch (eventError) { /* state remains synchronously observable */ }
  }
  const BOOT = readBoot();
  if (!BOOT) {
    // A present bootstrap block is an explicit promise that this page has a
    // Studio surface. Invalid JSON/config is therefore a terminal boot
    // failure, not a silent no-op. Pages without the block never load this
    // module in the first place and remain unaffected.
    if (BOOT_NODE) {
      document.documentElement.classList.remove("okf-studio-booted");
      publishStudioStatus("failed", "boot-failure", new Error("Studio bootstrap data is invalid."));
    }
    return;
  }
  // Keep the SSR fallback visible while initialization is in progress. The
  // success class is added only by startBoot(), after boot() has completed and
  // the usable public API exists. This is the critical distinction between a
  // loaded script and a working Studio.
  document.documentElement.classList.remove("okf-studio-booted");
  publishStudioStatus("loading", null, null);
  const EDIT = BOOT.edit !== false; // read-only kiosk when false
  // Commenting is its own capability: a comment-only shared session has
  // comment:true with edit:false. Older bootstraps without the field keep the
  // historical behaviour (commenting follows edit). It never implies edit,
  // lifecycle, presence, agent or admin authority.
  const CAN_COMMENT = BOOT.comment === true || (BOOT.comment === undefined && EDIT);
  const TOKEN = BOOT.token || "";
  const AUTHORITY_MODE = BOOT.authority_mode || (EDIT ? "local-edit" : "local-read");
  const CAN_CONTROL_SESSION = AUTHORITY_MODE === "local-edit";
  // X2: local operator page for importing portable feedback notes.
  const FEEDBACK_IMPORT_PATH = "/__feedback-import";
  const CAN_READ_PRESENCE = BOOT.presence !== false && !/^public-/.test(AUTHORITY_MODE);
  // F9: /__diff needs the per-session token (the server 403s without it) and
  // is route-denied for EVERY public-ingress request. Derive the authority
  // from the actual bootstrap rather than assuming edit===diff: a public-edit
  // session carries a token yet still cannot read /__diff, and a local
  // read-only page embeds no token at all. When this is false the conflict
  // modal says why instead of launching a doomed privileged request.
  const PUBLIC_INGRESS = /^public-/.test(AUTHORITY_MODE);
  const CAN_VIEW_DIFF = TOKEN !== "" && !PUBLIC_INGRESS;
  const PROPOSAL_CAPABILITY = BOOT.proposal_review || {
    status: "experimental", active: false, inactive_reason: "not_enabled",
  };
  // X3: the inspection-only Proposals panel exists only when capability
  // discovery says active AND the request is local. Public tunnel pages and
  // inactive sessions get no entry and never touch /__proposal/. A local
  // read-only (--no-edit) page embeds no session token while the read routes
  // admit only X-OKF-Token requests, so it gets an honest CLI pointer instead
  // of requests that are certain to be refused.
  const PROPOSAL_INSPECTION = (PROPOSAL_CAPABILITY && PROPOSAL_CAPABILITY.active === true &&
      (AUTHORITY_MODE === "local-edit" || AUTHORITY_MODE === "local-read"))
    ? (TOKEN !== "" ? "available" : "needs-token")
    : "off";
  const REDUCED_MOTION =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ====================================================================
  // 0. Helpers
  // ====================================================================
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));

    function isEditableTarget(target) {
      const candidates = [target, document.activeElement];
      return candidates.some(function (candidate) {
        const element = candidate && candidate.nodeType === Node.ELEMENT_NODE ? candidate : candidate && candidate.parentElement;
        if (!element || typeof element.closest !== "function") return false;
        if (element.closest("input, textarea, select, [role='textbox']")) return true;
        if (element.isContentEditable) return true;
        const editable = element.closest("[contenteditable]");
        return !!editable && editable.getAttribute("contenteditable") !== "false";
      });
    }

  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k === "html") n.innerHTML = v; // ONLY for trusted/server HTML
      else if (k === "dataset") for (const d in v) n.dataset[d] = v[d];
      else if (k === "on" && typeof v === "object") for (const ev in v) n.addEventListener(ev, v[ev]);
      else if (k === "hidden") {
        // Treat "", true, "hidden", "until-found" as present (boolean IDL
        // property - assigning "" directly would coerce to false / visible).
        n.hidden = (v === true || v === "" || v === "hidden" || v === "until-found");
      }
      else if (k in n && k !== "list") {
        try { n[k] = v; } catch (e) { n.setAttribute(k, v); }
      } else n.setAttribute(k, v);
    }
    if (kids) appendKids(n, kids);
    return n;
  }
  function appendKids(n, kids) {
    if (!kids) return;
    if (!Array.isArray(kids)) kids = [kids];
    kids.forEach((k) => {
      if (k == null) return;
      if (typeof k === "string" || typeof k === "number") n.appendChild(document.createTextNode(String(k)));
      else n.appendChild(k);
    });
  }

    async function tokenFetch(url, opts) {
    opts = opts || {};
    // §9.4 conflict UX (INTENT-008): preserve the ORIGINAL body object so a
    // 409 with ``conflict: true`` from /__apply can surface a modal whose
    // "Take the agent's" action re-submits the apply WITHOUT ``expected_rev``
    // (force-overwrite). The body is JSON-stringified just below; we hold
    // the live object reference here.
    const originalBodyObj = (opts.body && typeof opts.body !== "string") ? opts.body : null;
    const headers = Object.assign({ "X-OKF-Token": TOKEN }, opts.headers || {});
    if (opts.body && typeof opts.body !== "string") {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    opts.headers = headers;
    const res = await fetch(url, opts);
    // §9.4: detect a conflict response from /__apply and surface the modal.
    // The caller's await resolves to a Response chosen by the user action:
    //   - "Keep mine" / Esc → the original 409 Response (caller sees !ok).
    //   - "Take the agent's" → a fresh fetch WITHOUT expected_rev.
    if (res.status === 409 && url === "/__apply") {
      try {
        const cloned = res.clone();
        const data = await cloned.json();
        if (data && data.conflict) {
          return showConflictModal({
            data, url, opts, originalBodyObj, originalResponse: res,
          });
        }
      } catch (e) { /* not JSON or no conflict field — fall through */ }
    }
      return res;
    }

    function annotationDelay(attempt, signal) {
      const configured = state && state.annotationRetryDelays;
      const base = configured && configured[attempt - 1] != null ? configured[attempt - 1] : (attempt === 1 ? 250 : 500);
      const jitter = state && typeof state.annotationJitter === "function" ? state.annotationJitter() : Math.floor(Math.random() * 101);
      return new Promise(function (resolve, reject) {
        const timer = setTimeout(resolve, Math.max(0, base + jitter));
        if (signal) signal.addEventListener("abort", function () { clearTimeout(timer); reject(new DOMException("Superseded", "AbortError")); }, { once: true });
      });
    }
    function abortAnnotationOperations(reason) {
      state.annotationOperations.forEach(function (controller) { try { controller.abort(reason); } catch (e) {} });
      state.annotationOperations.clear();
    }
      async function annotationFetch(url, opts, policy, ownerGeneration) {
        const max = 3;
        const operationKey = ((opts && opts.operationKey) || policy + ":" + url) + ":" + (ownerGeneration == null ? "shared" : ownerGeneration);
        const prior = state.annotationOperations.get(operationKey);
        if (prior) {
          // R5-1: mark the prior as superseded and abort it. The prior's
          // in-flight fetch continuation checks _superseded immediately on
          // resume, BEFORE any retry/backoff logic. Also directly cancel
          // any pending backoff timer for the case where the prior already
          // entered backoff.
          prior._superseded = true;
          prior.abort(new DOMException("superseded", "AbortError"));
          if (prior._backoffReject) { try { prior._backoffReject(new DOMException("Superseded", "AbortError")); } catch (e) {} prior._backoffReject = null; }
          if (prior._backoffTimer) { clearTimeout(prior._backoffTimer); prior._backoffTimer = null; }
        }
        const operation = new AbortController(); state.annotationOperations.set(operationKey, operation);
        // Final-blocker: honor an OPTIONAL external abort signal (e.g. an inline-
        // reply form-lifetime AbortController) so the canonical deadline owner
        // also cancels immediately on form cancel/replacement/pagehide/navigation.
        // Additive: callers that omit opts.abortSignal are completely unaffected.
        const externalSignal = opts && opts.abortSignal;
        if (externalSignal) {
          if (externalSignal.aborted) { operation.abort(externalSignal.reason || new DOMException("aborted", "AbortError")); }
          else { externalSignal.addEventListener("abort", function () { operation.abort(externalSignal.reason || new DOMException("aborted", "AbortError")); }, { once: true }); }
        }
        // Group 6: one absolute operation deadline spans all attempts, backoff,
        // and jitter. GET = 10s total, POST = 15s total (§12.1). Each attempt
        // gets only the remaining budget, never a fresh full timer.
        const configuredDeadline = state && state.annotationDeadlines && state.annotationDeadlines[policy];
        const totalDeadlineMs = configuredDeadline != null ? configuredDeadline : (policy === "post" ? 15000 : 10000);
        const operationStart = Date.now();
        try { for (let attempt = 1; attempt <= max; attempt++) {
          if (ownerGeneration != null && ownerGeneration !== state.annotationGeneration) throw new DOMException("Superseded", "AbortError");
          // R4-2: synchronous supersession check. A newer same-key request
          // aborts this operation's signal (prior.abort('superseded')) which
          // does NOT change ownerGeneration; without this check the
          // superseded request would still issue its fetch and enter backoff.
          if (operation.signal.aborted) throw new DOMException("Superseded", "AbortError");
          const controller = new AbortController();
          const abortRequest = function () { controller.abort(operation.signal.reason); };
          operation.signal.addEventListener("abort", abortRequest, { once: true });
          const remaining = totalDeadlineMs - (Date.now() - operationStart);
          if (remaining <= 0) throw new DOMException("Operation deadline exceeded", "AbortError");
          const deadline = setTimeout(function () { controller.abort(); }, remaining);
        try {
            const request = Object.assign({}, opts || {}, { signal: controller.signal });
            delete request.operationKey;
            delete request.abortSignal;
            delete request.readJson;
           const response = policy === "post" && typeof state.annotationTransport === "function"
             ? await state.annotationTransport(url, request, attempt)
             : policy === "post" ? await tokenFetch(url, request) : await fetch(url, request);
          // R5-1: check supersession IMMEDIATELY after fetch resolves. If a
          // newer same-key call marked this operation as superseded while the
          // fetch was in flight, throw before processing the response.
          if (operation._superseded || operation.signal.aborted) throw new DOMException("Superseded", "AbortError");
          if (ownerGeneration != null && ownerGeneration !== state.annotationGeneration) throw new DOMException("Superseded", "AbortError");
          const retryable = policy === "post"
            ? (response.status === 429 || response.status === 503)
            : (response.status === 429 || response.status >= 500);
          if (!retryable || attempt === max) {
            if (!(opts && opts.readJson)) return response;
            // Q4: with readJson the final response body is read while this
            // attempt's deadline and abort owner still run, so a stalled body
            // ends as a deadline abort instead of hanging the caller. An
            // unreadable body is reported, never thrown: the caller decides
            // what it means (a 2xx whose body cannot be read is unconfirmed).
            let data = null, unreadable = false;
            try { data = await response.json(); }
            catch (bodyError) {
              if (controller.signal.aborted) throw bodyError;
              unreadable = true;
            }
            if (operation._superseded || operation.signal.aborted) throw new DOMException("Superseded", "AbortError");
            if (ownerGeneration != null && ownerGeneration !== state.annotationGeneration) throw new DOMException("Superseded", "AbortError");
            if (!data || typeof data !== "object") { data = {}; unreadable = true; }
            return { response: response, data: data, unreadable: unreadable };
          }
        } catch (error) {
          // R4-2: an explicitly superseded operation (aborted by a newer
          // same-key request, navigation, or disposal) must stop
          // synchronously and never enter retry/backoff. Normalise to an
          // AbortError DOMException regardless of how the underlying abort
          // surfaced (fetch rejects with signal.reason, which may be a
          // DOMException or, for older callers, a string). Only a genuine
          // transport/deadline abort that still matches the current
          // generation is retryable.
          if (operation.signal.aborted) throw new DOMException("Superseded", "AbortError");
          const retryableAbort = error && error.name === "AbortError" && (ownerGeneration == null || ownerGeneration === state.annotationGeneration);
          if (!retryableAbort || attempt === max) throw error;
        } finally { clearTimeout(deadline); operation.signal.removeEventListener("abort", abortRequest); }
            // R3-3: cap backoff delay to remaining operation budget so no
            // delay overshoots the absolute deadline. The signal races the
            // timer; whichever fires first wins.
            const delayRemaining = totalDeadlineMs - (Date.now() - operationStart);
            if (delayRemaining <= 0) throw new DOMException("Operation deadline exceeded", "AbortError");
            // R4-2: re-check supersession synchronously before scheduling the
            // backoff timer. An operation aborted between attempts (e.g. a
            // newer same-key request arrived while the response was being
            // inspected) must not sleep and retry.
            if (operation.signal.aborted) throw new DOMException("Superseded", "AbortError");
            const delayBase = (state && state.annotationRetryDelays && state.annotationRetryDelays[attempt - 1] != null)
              ? state.annotationRetryDelays[attempt - 1] : (attempt === 1 ? 250 : 500);
            const delayJitter = state && typeof state.annotationJitter === "function" ? state.annotationJitter() : Math.floor(Math.random() * 101);
            const delayMs = Math.min(Math.max(0, delayBase + delayJitter), delayRemaining, 4000);
            await new Promise(function (resolve, reject) {
              // R5-1: three-layer supersession guard:
              // 1. Check aborted at executor entry (already-aborted signal)
              // 2. addEventListener("abort") — fires when abort arrives later
              // 3. Store timer+rejecter on operation for direct cancellation
              //    by the next same-key caller (bypasses addEventListener
              //    entirely for guaranteed synchronous supersession)
              if (operation.signal.aborted) { reject(new DOMException("Superseded", "AbortError")); return; }
              var settled = false;
              function settle(fn, val) { if (settled) return; settled = true; clearTimeout(timer); clearInterval(poll); operation._backoffTimer = null; operation._backoffReject = null; fn(val); }
              var timer = setTimeout(function () { settle(resolve); }, delayMs);
              var poll = setInterval(function () {
                if (operation.signal.aborted) settle(reject, new DOMException("Superseded", "AbortError"));
              }, 50);
              operation._backoffTimer = timer;
              operation._backoffReject = function (err) { settle(reject, err); };
              operation.signal.addEventListener("abort", function () {
                settle(reject, new DOMException("Superseded", "AbortError"));
              }, { once: true });
            });
        if (ownerGeneration != null && ownerGeneration !== state.annotationGeneration) throw new DOMException("Superseded", "AbortError");
      }
      throw new Error("Annotation request exhausted retries.");
      } finally { if (state.annotationOperations.get(operationKey) === operation) state.annotationOperations.delete(operationKey); }
    }

  function currentConceptId() {
    let p = window.location.pathname.replace(/^\/+/, "").replace(/\/+$/, "");
    if (p.endsWith(".md")) p = p.slice(0, -3);
    return p;
  }
  const isConceptPage = () => !!document.querySelector("article.okf-page__main");

  function fmtTime(ts) {
    if (!ts) return "";
    try {
      const d = new Date(ts);
      if (isNaN(d.getTime())) return String(ts);
      return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch (e) { return String(ts); }
  }
  // Phase 5: human relative time for the index recency rail ("4m ago").
  // "Recently changed" rows (render.py SSR and the live rail below) share
  // one format: whole elapsed units, floored ("just now", "Nm ago",
  // "Nh ago", "Nd ago"), with the full UTC timestamp in the <time> title
  // for hover and assistive tech.
  function fmtRecentAgo(ts) {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  function fmtUtcStamp(ts) {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";
    const p = (n) => String(n).padStart(2, "0");
    return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) +
      " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + " UTC";
  }
  function shortId(id) { return id ? id.slice(-6) : ""; }

  // ---- toast region ----------------------------------------------------
  const TOAST_GLYPHS = { success: "OK", error: "!", warn: "?" };
  let toastRegion;
  function toast(msg, opts) {
    opts = opts || {};
    if (!toastRegion) {
      toastRegion = el("div", { class: "okf-toast-region", role: "status", "aria-live": "polite", "aria-atomic": "false" });
      document.body.appendChild(toastRegion);
    }
    const node = el("div", { class: "okf-toast", dataset: { tone: opts.tone || "" } });
    // A toned toast carries a glyph beside its 2px status bar so the tone is
    // never colour-only (WCAG 1.4.1). Decorative: the message says it.
    const glyph = TOAST_GLYPHS[opts.tone];
    if (glyph) node.appendChild(el("span", { class: "okf-toast__icon", "aria-hidden": "true", text: glyph }));
    node.appendChild(el("span", { class: "okf-toast__msg" }));
    node.querySelector(".okf-toast__msg").innerHTML = ""; // safety
    if (typeof msg === "string") {
      node.querySelector(".okf-toast__msg").textContent = msg;
    } else if (msg && msg.nodeType) {
      node.querySelector(".okf-toast__msg").appendChild(msg);
    }
    const close = el("button", { class: "okf-toast__close", type: "button", "aria-label": "Dismiss notification", text: "×" });
    node.appendChild(close);
    toastRegion.appendChild(node);
    const dismiss = () => { if (node.parentNode) node.parentNode.removeChild(node); };
    close.addEventListener("click", dismiss);
    if (!opts.sticky) setTimeout(dismiss, opts.ttl || 4500);
    return { close: dismiss, node };
  }

  // ====================================================================
  // 1. State
  // ====================================================================
  const state = {
    view: readView(),                // rendered | source | split
    focusView: readFocusView(),      // concept-local reading focus; URL owned
    conceptId: currentConceptId(),
    doc: null,                       // cached /__data/doc for the open concept
    graph: null,                     // cached /__data/graph.json
    graphReady: false,
    comments: [],                    // [{id, concept, anchor, state, body, claimed_by, resolved_activity, reply, ts}]
    events: [],                      // canonical change-list rows
    attention: { initialized: false, known: new Set(), unread: new Set() },
    presence: { state: "idle" },
    presenceHistory: [],   // iter2 G13: [{state, focus, ts}] recent transitions (cap 24)
    // F12: bumped by every canonical presence render (renderPresence). Watch
    // toggle writes fence their settlement against the value captured at click
    // time so a late ack/rejection can never roll back a newer canonical state.
    _presenceSeq: 0,
    openPanel: null,                 // current slide-over panel id
    draftBody: "",                   // preserved across view toggles + patches
    draftAnchor: null,
    draftContentRev: null,           // R5-12: concept_content_rev a v2 draft was captured against (rev-qualifies the pending mark)
    composerPost: null,              // in-flight/retry state for the composer POST lifecycle (key/body/anchor/outcome)
    composerStatus: null,            // persistent inline composer lifecycle status {message, tone} (null = cleared)
    draftTargetMissing: "",          // R4: targetKey a post was rejected for (404 target_not_found); Send stays off while the draft is saved there
    draftGeneration: 0,              // bumped on genuine user draft edits; fences stale restores (state-ownership)
    selectionDraft: null,             // last visible text selection for the comment affordance
    selectionOrigin: null,            // content block that owned focus when selection began
    commentLifecycle: "idle",         // idle | selection-action | anchored-composer | concept-composer | closed
    filters: { actor: "", concept: "", action: "" },
    nextCommentSeq: 1,               // for local optimistic ids
      commentView: loadCommentView(),  // panel prefs (sort/filter/expand)
      annotationEditor: null,          // one local editor; canonical state stays server-owned
      annotationGeneration: 0,         // fences late async work after navigation/disposal
      annotationAnnouncement: "",
      commentsRequestGeneration: 0,
      commentsReady: false,
      eventsReady: false,
      commentsStatus: "loading",
      selectionAffordanceReady: false,
      annotationRetryDelays: null,
      annotationJitter: null,
      annotationTransport: null,
      annotationDeadlines: null,
      annotationOperations: new Map(),
      pendingFocusCommentId: null,
      documentGeneration: 0,
      // Q4: bumped when a successful commitDocument changes the shown
      // revision, including the first (null -> doc) publication that
      // documentGeneration does not count; a same-revision re-commit is not a
      // publication. A canonical read that started before a publication of a
      // different revision is superseded by it (never aborted: this is not an
      // owner).
      documentPublication: 0,
      sourceLoadGeneration: 0,  // F6: lazy-Source read admission owner
      // F10: exact-identity reveal requests. Set by the comment-card Jump and
      // the change-row reverse link; consumed by the next render of the
      // owning panel (or kept pending while that panel's data is still
      // loading). Never a timer: resolution happens against retained data.
      pendingChangeReveal: null,
      pendingCommentReveal: null,
  };
  function readView() {
    const q = new URLSearchParams(window.location.search).get("view");
    if (q === "source" || q === "split") return q;
    return "rendered";
  }
  function readFocusView() {
    const params = new URLSearchParams(window.location.search);
    return params.get("focus") === "1" || params.get("view") === "focus";
  }

  // ====================================================================
  // 2. Themes (§13.5) — state ownership delegated to theme.js
  // ====================================================================
  // Theme state (palette choice incl. System/OS, persistence, the Appearance popover)
  // is owned ENTIRELY by viewer/static/theme.js, which loads before this
  // module and resolves data-theme before first paint. The studio NEVER
  // reads/writes localStorage for theme and never sets data-theme; command
  // palette items call window.OKFLoomTheme (see commandItems) so theme.js
  // remains the single writer.
  function themeApi() {
    return (typeof window.OKFLoomTheme === "object" && window.OKFLoomTheme) || null;
  }

  // ====================================================================
  // 3. Studio bar
  // ====================================================================
  const bar = document.querySelector("[data-okf-viewbar-placeholder]") ||
    el("div", { class: "okf-concept-viewbar", role: "region", "aria-label": "Document view" });
  const leftGroup = el("div", { class: "okf-studio-home__group" });
  const rightGroup = el("div", { class: "okf-studio-home__group" });
  const studioSlot = document.querySelector("[data-okf-studio-capability='available']");
  const studioLauncherLabel = el("span", { class: "okf-studio-launcher__label", text: "Studio" });
  // Boot truth: nothing is connected yet, so the launcher never claims
  // "Live". It names what this session may do (the same authority word
  // updateBadges() shows once online); a stream that is still not up after
  // LAUNCHER_CONNECTING_GRACE_MS then says "Connecting" (see updateBadges).
  // Printing "Connecting…" here made every healthy load flash it for the
  // few hundred milliseconds the SSE handshake takes.
  const studioLauncherSummary = el("span", {
    class: "okf-studio-launcher__summary",
    text: " · " + (EDIT ? "Editable" : CAN_COMMENT ? "Comment-only" : "Read-only"),
  });
  // How long a connecting/reconnecting stream may stay quiet in the launcher.
  // Healthy handshakes finish well inside it; a stream that is genuinely not
  // up is announced one second later, and "offline" is never delayed.
  const LAUNCHER_CONNECTING_GRACE_MS = 1000;
  let degradedSince = performance.now(); // boot is the first "connecting" stretch
  let connectingGraceTimer = 0;
  const studioLauncher = el("button", {
    type: "button", class: "okf-studiobtn okf-studio-launcher",
    "aria-expanded": "false", "aria-controls": "okf-panel",
  }, [studioLauncherLabel, studioLauncherSummary]);
  studioLauncher.addEventListener("click", () => togglePanel("studio"));
  // The Studio home status node while the home panel shows it (see
  // panels.studio.render); updateBadges() keeps it on the connection state.
  let studioHomeStatus = null;

  // Presence chip
  const presenceDot = el("span", { class: "okf-presence__dot", "aria-hidden": "true" });
  const presenceLabel = el("span", { class: "okf-presence__label" });
  presenceLabel.appendChild(el("span", { class: "okf-presence__actor", text: "Agent" }));
  presenceLabel.appendChild(document.createTextNode(" idle"));
  const presenceChip = el("span", { class: "okf-presence", "data-state": "idle", role: "status",
    "aria-live": "polite", "aria-label": "Agent presence: idle",
    "aria-describedby": "okf-presence-note",
    // Reported status: the agent or the Watching toggle sets it. It is not a
    // liveness check, and the chip must not read as one.
    title: "Reported agent status. Set by the agent or by the Watching toggle; it does not show whether an agent is running." },
    [presenceDot, presenceLabel]);
  // R3: the hover title is not available to keyboard or touch users, so the
  // same caveat is the chip's description. It sits beside the chip, outside
  // its live region, so a presence change never re-announces it. It is
  // hidden, like the selection Comment button's note: a description
  // reference still reads it, and browse mode does not read it a second
  // time as the text after the chip.
  const presenceNote = el("span", { id: "okf-presence-note", class: "okf-sr-only", hidden: "",
    text: "Reported status, not proof an agent is running." });
  if (CAN_READ_PRESENCE) { leftGroup.appendChild(presenceChip); leftGroup.appendChild(presenceNote); }
  presenceChip.hidden = !CAN_READ_PRESENCE;

  // iter2 G11 (§3 watch question): an "Agent watching" switch in the studio
  // bar. It writes the LEGACY EXPLICIT agent-row status flag: on POSTs
  // /__presence {actor:"agent", state:"watching"}, off POSTs {state:"idle"}.
  // It is a reported status only — never a process launcher, never liveness,
  // and deliberately separate from live.js's capability-bound browser-session
  // publisher. It also mirrors the canonical agent row (renderPresence syncs
  // aria-pressed), so an agent reporting watching via the CLI moves it too.
  // Visible label "Watching" + a square status glyph + an accessible name.
  const watchingToggle = el("button", { type: "button", class: "okf-studiobtn okf-watch-toggle",
    "aria-pressed": "false", "aria-label": "Agent watching, currently off",
    "aria-describedby": "okf-watch-toggle-note",
    title: "Marks the agent as watching in this Studio. A status flag only: it does not start an agent." });
  // The flag is shared presence, not a process. Say so to every reader of the
  // control, not only to those who hover it.
  watchingToggle.appendChild(el("span", { id: "okf-watch-toggle-note", class: "okf-sr-only",
    text: "Status flag only. It does not start an agent; an external agent must be running to act on comments." }));
  watchingToggle.appendChild(el("span", { class: "okf-watch-toggle__icon", "aria-hidden": "true", text: "\u25A0" }));
  watchingToggle.appendChild(el("span", { class: "okf-watch-toggle__label", text: "Watching" }));
  watchingToggle.disabled = !CAN_CONTROL_SESSION;
  watchingToggle.hidden = !CAN_CONTROL_SESSION;
  if (!CAN_CONTROL_SESSION) watchingToggle.title = "Watching can be controlled only from a local editable Studio.";
  // F12: keep aria-pressed, the accessible name and the visible control on one
  // state at all times. Both renderPresence and the click settlement go through
  // this single writer.
  function syncWatchingToggle(on) {
    watchingToggle.setAttribute("aria-pressed", on ? "true" : "false");
    watchingToggle.setAttribute("aria-label",
      "Agent watching, currently " + (on ? "on" : "off"));
  }
  // F12: the optimistic flip is announced immediately, then the control
  // settles ONLY from evidence and never overrides a newer canonical presence
  // render (the state._presenceSeq fence):
  //   - 200 + {ok:true, presence:{state}} adopts the acknowledged canonical
  //     reported state;
  //   - a definite HTTP/envelope rejection (403/400/429/503 or ok:false) rolls
  //     back to the last known state and says so — the server provably did not
  //     apply the request;
  //   - transport loss or an invalid acknowledgement claims nothing: the
  //     request may still have reached the server, so the switch returns to
  //     the last known state and canonical presence reconciliation owns the
  //     truth from there.
  // One write at a time: a click while a write is pending never stacks a
  // second conflicting POST, and there is no automatic retry.
  let watchingWritePending = false;
  watchingToggle.addEventListener("click", () => {
    if (!CAN_CONTROL_SESSION || watchingWritePending) return;
    const nowOn = watchingToggle.getAttribute("aria-pressed") !== "true";
    const knownOn = !nowOn;
    syncWatchingToggle(nowOn);
    watchingToggle.setAttribute("aria-busy", "true");
    watchingWritePending = true;
    const clickSeq = state._presenceSeq;
    const superseded = () => state._presenceSeq !== clickSeq;
    tokenFetch("/__presence", { method: "POST", body: { actor: "agent", state: nowOn ? "watching" : "idle" } })
      .then(async (res) => {
        let ack = null;
        try { ack = await res.json(); } catch (e) { ack = null; }
        if (res.ok && ack && ack.ok === true && ack.presence
            && typeof ack.presence.state === "string") {
          if (!superseded()) syncWatchingToggle(ack.presence.state === "watching");
          return;
        }
        if (superseded()) return; // a newer canonical presence render owns the switch
        syncWatchingToggle(knownOn);
        // Definite refusal = a real HTTP rejection OR a parsed {ok:false}
        // envelope: the server answered and did not apply the write. Anything
        // else on the wire (unparseable body, missing ack fields) is an
        // invalid acknowledgement — honest uncertainty, never a claimed
        // rejection.
        if (!res.ok || (ack && ack.ok === false)) {
          toast("Could not update agent watching state.", { tone: "error" });
        } else {
          toast("Watching update could not be confirmed; the switch follows the agent's reported presence.", { tone: "warn" });
        }
      })
      .catch(() => {
        if (superseded()) return;
        // Transport failure before an acknowledgement: the request may still
        // have been applied server-side, so this is uncertainty, not rejection.
        syncWatchingToggle(knownOn);
        toast("Watching update could not be confirmed; the switch follows the agent's reported presence.", { tone: "warn" });
      })
      .finally(() => {
        watchingToggle.removeAttribute("aria-busy");
        watchingWritePending = false;
      });
  });
  if (CAN_CONTROL_SESSION) leftGroup.appendChild(watchingToggle);

  // Connection indicator (driven by live.js hub). iter1 CRI-015: aria-live
  // so "Reconnecting…" / "Live" / "Offline" state changes are announced to
  // assistive tech (presence + toasts already were; the conn chip was the
  // odd one out).
  const connDot = el("span", { class: "okf-conn__dot", "aria-hidden": "true" });
  const connLabel = el("span", { text: "Connecting…" });
  // Boot default is "connecting" — the same state live.js initializes with.
  // The former "reconnecting" default asserted a dropped connection during a
  // healthy first load (and updateBadges raised the failure projection from
  // it before wireLive ever ran).
  const connChip = el("span", { class: "okf-conn", "data-state": "connecting",
    title: "Live updates connection", role: "status", "aria-live": "polite",
    "aria-label": "Live updates connection: connecting" }, [connDot, connLabel]);

  // View-mode switch (concept pages only)
  const viewSwitch = el("div", { class: "okf-viewswitch", role: "group", "aria-label": "View mode" });
  function viewBtn(mode, label) {
    const b = el("button", { type: "button", class: "okf-viewswitch__btn",
      "aria-pressed": state.view === mode ? "true" : "false", text: label });
    b.addEventListener("click", () => setView(mode));
    b.dataset.mode = mode;
    return b;
  }
  const viewBtns = {
    rendered: viewBtn("rendered", "Rendered"),
    source: viewBtn("source", "Source"),
    split: viewBtn("split", "Split"),
  };
  Object.keys(viewBtns).forEach((k) => viewSwitch.appendChild(viewBtns[k]));
  // Source soft-wraps by default so long lines never need a horizontal trip;
  // the control is only offered while a Source pane is on screen. Unwrapped
  // Source scrolls locally and becomes a keyboard-focusable region.
  let sourceWrap = true;
  try { sourceWrap = localStorage.getItem("okf-source-wrap") !== "off"; } catch (e) {}
  // One label element (a single flex item, so wide layouts render exactly
  // as plain text did); phones hide " lines" visually (wiki.css) so the view
  // tabs keep one row, while the button's name stays "Wrap lines".
  const wrapLabel = el("span", { class: "okf-viewswitch__label" });
  wrapLabel.append("Wrap", el("span", { class: "okf-viewswitch__more", text: " lines" }));
  const wrapBtn = el("button", { type: "button", class: "okf-viewswitch__btn okf-source-wrap",
    "aria-pressed": sourceWrap ? "true" : "false",
    title: "Wrap long Source lines", hidden: state.view === "rendered" }, [wrapLabel]);
  wrapBtn.addEventListener("click", () => {
    sourceWrap = !sourceWrap;
    try { localStorage.setItem("okf-source-wrap", sourceWrap ? "on" : "off"); } catch (e) {}
    applySourceWrap();
  });
  viewSwitch.appendChild(wrapBtn);
  const focusViewBtn = el("button", { type: "button", class: "okf-viewswitch__btn okf-focus-toggle",
    "aria-pressed": state.focusView ? "true" : "false", text: "Focus view" });
  function setFocusView(active) {
    state.focusView = !!active;
    document.body.classList.toggle("okf-focus-view", state.focusView);
    focusViewBtn.setAttribute("aria-pressed", state.focusView ? "true" : "false");
    // Focus hides the global shell, which changes the measured sticky stack
    // (viewbar offset + heading scroll clearance). document-outline.js owns
    // that geometry; refresh it deterministically instead of waiting for a
    // resize tick.
    if (window.okfDocumentOutline && typeof window.okfDocumentOutline.updateOcclusion === "function") {
      window.okfDocumentOutline.updateOcclusion();
    }
    const url = new URL(window.location.href);
    if (url.searchParams.get("view") === "focus") url.searchParams.delete("view");
    if (state.focusView) url.searchParams.set("focus", "1");
    else url.searchParams.delete("focus");
    window.history.replaceState(null, "", url.toString());
  }
  focusViewBtn.addEventListener("click", () => setFocusView(!state.focusView));
  viewSwitch.appendChild(focusViewBtn);

  // Panel toggle buttons (right group)
  const commentsBtn = el("button", { type: "button", class: "okf-studiobtn", "aria-expanded": "false",
    "aria-controls": "okf-panel", text: "Comments" });
  const commentsBadge = el("span", { class: "okf-badge", "aria-hidden": "true", text: "0" });
  commentsBtn.insertBefore(commentsBadge, commentsBtn.firstChild);
  commentsBtn.addEventListener("click", () => togglePanel("comments"));

  const changesBtn = el("button", { type: "button", class: "okf-studiobtn", "aria-expanded": "false",
    "aria-controls": "okf-panel", text: "Changes" });
  const changesBadge = el("span", { class: "okf-badge", "aria-hidden": "true", text: "0" });
  changesBtn.insertBefore(changesBadge, changesBtn.firstChild);
  changesBtn.addEventListener("click", () => togglePanel("changes"));

  // User feedback: the palette trigger used to be a bare "⌘K"
  // glyph — meaningless on the ~90% of machines without a command key.
  // It now says what it does ("Commands") with a platform-correct
  // shortcut hint (⌘K on Apple devices, Ctrl+K everywhere else).
  const isApplePlatform = /Mac|iPhone|iPad|iPod/.test(
    (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "");
  const paletteHint = isApplePlatform ? "⌘K" : "Ctrl+K";
  const paletteBtn = el("button", { type: "button", class: "okf-studiobtn okf-palettebtn",
    "aria-label": "Open the command palette (" + (isApplePlatform ? "Command K" : "Control K") + ")",
    title: "Search pages and run commands  (" + paletteHint + ")" },
    [document.createTextNode("Commands "),
     el("kbd", { class: "okf-kbd", "aria-hidden": "true", text: paletteHint })]);
  paletteBtn.addEventListener("click", openPalette);

  // The shell receives one calm launcher. Detailed controls live in the
  // Studio home panel; concept reading controls remain page-local.
  rightGroup.appendChild(commentsBtn);
  rightGroup.appendChild(changesBtn);
  rightGroup.appendChild(paletteBtn);
  rightGroup.appendChild(connChip);

  function mountBar() {
    if (studioSlot) {
      studioSlot.replaceChildren(studioLauncher);
      studioSlot.hidden = false;
    }
    if (isConceptPage()) {
      // The outline owner may already have mounted Sections in the SSR bar.
      // Keep that connected node (or its inert placeholder) when filling it.
      const sections = bar.querySelector("[data-okf-outline-jump], [data-okf-outline-jump-placeholder]");
      bar.replaceChildren(viewSwitch, ...(sections ? [sections] : []));
      bar.removeAttribute("data-okf-viewbar-placeholder");
      bar.removeAttribute("aria-hidden");
      bar.setAttribute("role", "region");
      bar.setAttribute("aria-label", "Document view");
      const topbar = $(".okf-topbar");
      if (topbar && topbar.parentNode) topbar.parentNode.insertBefore(bar, topbar.nextSibling);
      else document.body.insertBefore(bar, document.body.firstChild);
      setFocusView(state.focusView);
      const utilitySlot = document.querySelector("[data-okf-live-utilities]");
      if (utilitySlot) {
        const sourceUtility = el("button", { type: "button", class: "okf-btn okf-page-utilities__live", text: "Source" });
        sourceUtility.addEventListener("click", () => setView("source"));
        const commentsUtility = el("button", { type: "button", class: "okf-btn okf-page-utilities__live", text: "Comments" });
        commentsUtility.addEventListener("click", () => togglePanel("comments"));
        utilitySlot.replaceChildren(sourceUtility, commentsUtility);
      }
    }
  }

  // ---- Comment on this page ------------------------------------------
  // One visible, keyboard- and touch-reachable entry point on every live
  // document surface (concept, root/directory index, log). It opens the same
  // durable composer as every other route; the target is the stable document
  // identity from defaultCommentTarget(), never a fabricated concept.
  let pageFeedback = null;
  function pageFeedbackHost() {
    if (isConceptPage()) return document.querySelector("[data-okf-page-utilities]");
    // Graph: the target is the selected concept, so the entry lives in the
    // detail header (hidden with the pane while nothing is selected).
    if (document.getElementById("detail-body")) return document.getElementById("okf-detail-actions-slot");
    const main = document.getElementById("okf-main");
    if (!main || !hasCommentTarget(defaultCommentTarget())) return null;
    return main.querySelector(".okf-reserved__header") || main.querySelector(".okf-index__title");
  }
  function openPageComposer() {
    // Never discard a retained selection or an in-flight/uncertain attempt.
    const draft = state.draftAnchor;
    const busy = state.composerPost && state.composerPost.outcome !== "rejected";
    // A pinned graph draft keeps its own concept (R4).
    if (!busy && (!draft || isPlainPageDraft(draft)) && !draftIsPinned()) state.draftAnchor = defaultDraftAnchor();
    openPanel("comments", { focusComposer: CAN_COMMENT });
  }
  function mountPageFeedback() {
    const host = pageFeedbackHost();
    if (!host || pageFeedback) {
      document.querySelectorAll("[data-okf-page-feedback-placeholder]").forEach((node) => node.remove());
      if (!host) document.querySelector(".okf-hero--feedback")?.classList.remove("okf-hero--feedback");
      return;
    }
    const inGraph = host.id === "okf-detail-actions-slot";
    const noun = inGraph ? "concept" : "page";
    const placeholder = document.querySelector("[data-okf-page-feedback-placeholder]");
    const group = placeholder || el("div", { class: "okf-page-feedback" + (inGraph ? " okf-page-feedback--graph" : ""), role: "group",
      "aria-label": inGraph ? "Concept feedback" : "Page feedback" });
    if (placeholder) {
      group.replaceChildren();
      group.removeAttribute("data-okf-page-feedback-placeholder");
      group.removeAttribute("aria-hidden");
      group.setAttribute("role", "group");
      group.setAttribute("aria-label", "Page feedback");
    }
    const comment = el("button", { type: "button", class: "okf-btn okf-page-feedback__comment",
      "aria-controls": "okf-panel", text: "Comment on this " + noun });
    comment.addEventListener("click", openPageComposer);
    const count = el("button", { type: "button", class: "okf-btn okf-btn--quiet okf-page-feedback__count", hidden: "" });
    count.addEventListener("click", () => openPanel("comments"));
    const note = el("p", { class: "okf-page-feedback__note", hidden: "" });
    if (CAN_COMMENT) group.appendChild(comment);
    group.appendChild(count);
    group.appendChild(note);
    if (host.matches("[data-okf-page-utilities]")) host.appendChild(group);
    else if (inGraph) {
      const openLine = host.querySelector(".okf-detail__open");
      host.insertBefore(group, openLine ? openLine.nextSibling : null);
      // The preview changes concept without a navigation. Follow what the
      // whole detail pane shows (concept preview, reserved index/log reader,
      // or nothing after a clear) rather than guessing at event ordering;
      // the handler ignores mutations that leave the shown target unchanged.
      const detailPane = document.getElementById("okf-detail") || document.getElementById("detail-body");
      new MutationObserver(onGraphShownTargetChange).observe(detailPane, { subtree: true, childList: true,
        attributes: true, attributeFilter: ["hidden", "data-concept-id", "data-okf-document"] });
      window.addEventListener("okf-loom:graph-selection", onGraphShownTargetChange);
      _graphShownKey = targetKey(shownGraphTarget());
    } else if (host.matches(".okf-index__title")) {
      // Index hero: directly under the title, in its own named grid area. A
      // directory index renders its whole authored body in the intro cell, so
      // anywhere later would put the entry point screens below the fold.
      const hero = host.parentNode;
      hero.insertBefore(group, host.nextSibling);
      hero.classList.add("okf-hero--feedback");
    } else host.appendChild(group);
    pageFeedback = { group, comment, count, note, noun };
    updatePageFeedback();
  }
  // The count below "Comment on this page" appears, disappears and rewraps as
  // comments arrive, which changes the height of the document tools above
  // the body. When the reader has scrolled that strip up behind the sticky
  // chrome, hold the text they are reading still instead of letting it jump
  // (the browser's own scroll anchoring picks an anchor inside the hidden
  // strip, so it does not compensate). A strip the reader can see simply
  // grows in place.
  function keepReadingPlace(strip) {
    const body = document.querySelector(".okf-page__body");
    if (!strip || !body || !strip.isConnected || !body.isConnected) return () => {};
    const chrome = document.querySelector(".okf-concept-viewbar") || document.querySelector(".okf-topbar");
    const occluded = chrome ? Math.max(0, chrome.getBoundingClientRect().bottom) : 0;
    const hidden = strip.getBoundingClientRect().bottom <= occluded + 1;
    const bodyTop = body.getBoundingClientRect().top;
    return () => {
      if (!hidden || !body.isConnected) return;
      const moved = body.getBoundingClientRect().top - bodyTop;
      if (Math.abs(moved) >= 1) window.scrollBy({ top: moved, behavior: "instant" });
    };
  }
  function updatePageFeedback() {
    if (!pageFeedback) return;
    const group = pageFeedback.group;
    const settle = keepReadingPlace(group.closest(".okf-document-tools") || group);
    try { renderPageFeedback(); } finally { settle(); }
  }
  function renderPageFeedback() {
    const target = defaultCommentTarget();
    pageFeedback.group.hidden = !hasCommentTarget(target);
    if (pageFeedback.group.hidden) return;
    const noun = pageFeedback.noun;
    // Saved rows only: an optimistic row is "sending", never a counted comment.
    const rows = state.comments.filter((c) => rowMatchesTarget(c, target) && !c.archived && !c._posting);
    // F11: count lifecycle states explicitly. Only a genuinely resolved-only
    // page says "resolved"; dismissed rows are never announced as resolved,
    // mixed terminal states (or uncountable legacy rows) fall back to a
    // neutral total, and active (open/claimed) rows keep the same active-only
    // semantics the index/graph chips use.
    const open = rows.filter((c) => c.state === "open" || c.state === "claimed").length;
    const resolved = rows.filter((c) => c.state === "resolved").length;
    const dismissed = rows.filter((c) => c.state === "dismissed").length;
    const status = state.commentsStatus;
    let label = "";
    if (status === "ready" && rows.length) {
      const countLabel = (n, word) => n + " " + word + (n === 1 ? "" : "s") + " on this " + noun;
      if (open) label = countLabel(open, "open comment");
      else if (resolved && !dismissed && open + resolved + dismissed === rows.length) label = countLabel(resolved, "resolved comment");
      else if (dismissed && !resolved && open + resolved + dismissed === rows.length) label = countLabel(dismissed, "dismissed comment");
      else label = "View " + rows.length + " comment" + (rows.length === 1 ? "" : "s") + " on this " + noun;
    } else if (!CAN_COMMENT && status !== "error") label = "View comments";  // reserved before load; the panel shows loading
    pageFeedback.count.textContent = label;
    pageFeedback.count.hidden = !label;
    let note = "";
    if (status === "error") note = "Comments could not be loaded. Open Comments to retry.";
    else if (!CAN_COMMENT) note = "This session is read-only: comments can be read here but not added.";
    pageFeedback.note.textContent = note;
    pageFeedback.note.hidden = !note;
    pageFeedback.group.dataset.state = status === "ready" ? (CAN_COMMENT ? "ready" : "read-only") : status;
  }

  function unmountStudioShell() {
    if (pageFeedback) {
      const hero = pageFeedback.group.parentNode;
      if (hero && hero.classList) hero.classList.remove("okf-hero--feedback");
      pageFeedback.group.remove(); pageFeedback = null;
    }
    if (studioSlot) {
      studioSlot.replaceChildren(); studioSlot.hidden = true;
      studioSlot.dataset.okfStudioCapability = "unavailable";
    }
    if (bar.parentNode) bar.parentNode.removeChild(bar);
  }

  // ====================================================================
  // 4. View modes (§8) + applyDoc(doc)
  // ====================================================================
  let sourcePre = null;
  let viewWrap = null;
  let splitDivider = null;
  // iter2 G8: split-pane niceties — draggable divider (pointer + keyboard)
  // and proportional synced scroll between the rendered + source panes.
  // splitPct is the rendered-pane fraction (0.2–0.8). Persisted to
  // localStorage so a user's preferred split survives reloads.
  const SPLIT_MIN = 0.2, SPLIT_MAX = 0.8;
  let splitPct = 0.5;
  try {
    const saved = parseFloat(localStorage.getItem("okf-split-pct"));
    if (!isNaN(saved) && saved >= SPLIT_MIN && saved <= SPLIT_MAX) splitPct = saved;
  } catch (e) {}
  let splitSyncGuard = false;  // prevents A→B→A scroll-feedback loops

  function applySplitPct(pct) {
    splitPct = Math.max(SPLIT_MIN, Math.min(SPLIT_MAX, pct));
    if (viewWrap) viewWrap.style.setProperty("--okf-split-pct", (splitPct * 100) + "%");
    if (splitDivider) {
      splitDivider.setAttribute("aria-valuenow", String(Math.round(splitPct * 100)));
      splitDivider.setAttribute("aria-valuetext",
        "Rendered pane " + Math.round(splitPct * 100) + " percent, source " + Math.round((1 - splitPct) * 100) + " percent");
    }
  }

  function ensureViewWrap() {
    if (viewWrap) return;
    const body = $(".okf-page__body");
    if (!body) return;
    viewWrap = el("div", { class: "okf-view", dataset: { okfView: state.view } });
    body.parentNode.insertBefore(viewWrap, body);
    viewWrap.appendChild(body);
    // iter2 G8: draggable divider between the rendered + source panes. Lives
    // in the DOM always but only visible/active in split mode (CSS hides it
    // otherwise + under 900px). role=separator + aria-orientation so AT + kb
    // users can resize with Arrow Left / Right (3% per press, Shift = 10%).
    splitDivider = el("div", {
      class: "okf-split__divider",
      role: "separator",
      "aria-orientation": "vertical",
      "aria-label": "Resize rendered and source panes. Arrow keys to adjust.",
      "aria-valuemin": "20", "aria-valuemax": "80", "aria-valuenow": "50",
      tabindex: "0",
    });
    viewWrap.appendChild(splitDivider);
    sourcePre = el("pre", { class: "okf-source", hidden: state.view === "rendered",
      "aria-label": "Source markdown (read-only)" });
    viewWrap.appendChild(sourcePre);
    wireSplitDivider();
    wireSplitScroll();
    applySplitPct(splitPct);
    if (typeof ResizeObserver === "function" && !workspaceObserver) {
      workspaceObserver = new ResizeObserver(() => syncSplitAvailability());
      workspaceObserver.observe(viewWrap);
      workspaceObserver.observe(sourcePre);
    }
  }

  // Draggable divider: pointer drag + keyboard arrows. The drag uses
  // pointer events (works for mouse + touch + pen) and updates the split
  // ratio from the pointer's X within the viewWrap bounding rect.
  function wireSplitDivider() {
    if (!splitDivider || !viewWrap) return;
    let dragging = false;
    splitDivider.addEventListener("pointerdown", (e) => {
      if (!isSplitView()) return;
      dragging = true;
      splitDivider.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    splitDivider.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const rect = viewWrap.getBoundingClientRect();
      if (!rect.width) return;
      const pct = (e.clientX - rect.left) / rect.width;
      applySplitPct(pct);
    });
    const endDrag = (e) => {
      if (!dragging) return;
      dragging = false;
      try { splitDivider.releasePointerCapture(e.pointerId); } catch (err) {}
      try { localStorage.setItem("okf-split-pct", String(splitPct)); } catch (err) {}
    };
    splitDivider.addEventListener("pointerup", endDrag);
    splitDivider.addEventListener("pointercancel", endDrag);
    // Keyboard: Arrow Left/Right adjust 3%, Shift+Arrow 10%. Home/End reset
    // to the extremes (clamped to SPLIT_MIN/MAX).
    splitDivider.addEventListener("keydown", (e) => {
      if (!isSplitView()) return;
      const step = e.shiftKey ? 0.10 : 0.03;
      let handled = true;
      if (e.key === "ArrowLeft") applySplitPct(splitPct - step);
      else if (e.key === "ArrowRight") applySplitPct(splitPct + step);
      else if (e.key === "Home") applySplitPct(SPLIT_MIN);
      else if (e.key === "End") applySplitPct(SPLIT_MAX);
      else handled = false;
      if (handled) {
        e.preventDefault();
        try { localStorage.setItem("okf-split-pct", String(splitPct)); } catch (err) {}
      }
    });
  }

  // Proportional synced scroll: scrolling one pane scrolls the other by the
  // same fraction of its scroll range. The guard flag breaks the feedback
  // loop (B's scroll firing back into A).
  function wireSplitScroll() {
    const body = $(".okf-page__body");
    if (!body || !sourcePre) return;
    const sync = (src, dst) => {
      if (splitSyncGuard) return;
      if (!isSplitView()) return;
      const max = src.scrollHeight - src.clientHeight;
      if (max <= 0) return;
      const ratio = src.scrollTop / max;
      const dstMax = dst.scrollHeight - dst.clientHeight;
      if (dstMax > 0) {
        splitSyncGuard = true;
        dst.scrollTop = ratio * dstMax;
        splitSyncGuard = false;
      }
    };
    body.addEventListener("scroll", () => sync(body, sourcePre), { passive: true });
    sourcePre.addEventListener("scroll", () => sync(sourcePre, body), { passive: true });
  }

  const splitSurfaceQuery = window.matchMedia("(min-width: 900px)");
  // Split is admitted by the width the workspace REALLY has, not by the
  // viewport: a Comments rail and the Related rail both take from it, so a
  // 1200px window can leave two ~200px panes. Each pane keeps a readable
  // minimum or Split is not a simultaneous surface. The 900px media query
  // stays as the CSS fail-safe floor before this script runs.
  const SPLIT_PANE_MIN = 300, SPLIT_DIVIDER_W = 7;
  // A Split the layout took away (Comments opened, window narrowed) comes
  // back when the room does. An explicit Rendered/Source choice clears it.
  let splitSuspended = false;
  let workspaceObserver = null;

  function workspaceWidth() {
    const node = viewWrap || $(".okf-page__body-col");
    return node ? node.getBoundingClientRect().width : 0;
  }
  function splitAdmitted() {
    return splitSurfaceQuery.matches && workspaceWidth() >= SPLIT_PANE_MIN * 2 + SPLIT_DIVIDER_W;
  }
  function applySourceWrap() {
    wrapBtn.setAttribute("aria-pressed", sourceWrap ? "true" : "false");
    wrapBtn.hidden = state.view === "rendered";
    if (sourcePre) sourcePre.dataset.okfWrap = sourceWrap ? "on" : "off";
    syncPaneScrollports();
  }
  // A pane is a tab stop and a named region only while it really scrolls
  // locally (unwrapped Source, or either Split pane), so keyboard users can
  // reach every line without an extra stop on panes that fit.
  function syncPaneScrollport(node, label) {
    if (!node) return;
    const visible = !node.hidden && node.getClientRects().length > 0;
    const scrolls = visible && (node.scrollWidth > node.clientWidth + 1 ||
      (isSplitView() && node.scrollHeight > node.clientHeight + 1));
    if (scrolls) {
      node.setAttribute("role", "region");
      node.setAttribute("aria-label", label);
      node.tabIndex = 0;
      node.dataset.okfPaneScrolls = "1";
    } else if (node.dataset.okfPaneScrolls === "1") {
      node.removeAttribute("role");
      node.removeAttribute("tabindex");
      delete node.dataset.okfPaneScrolls;
      if (node === sourcePre) node.setAttribute("aria-label", "Source markdown (read-only)");
      else node.removeAttribute("aria-label");
    }
  }
  function syncPaneScrollports() {
    syncPaneScrollport(sourcePre, "Source markdown (read-only). Scroll to view all lines.");
    syncPaneScrollport($(".okf-page__body"), "Rendered document. Scroll to view all content.");
  }
  function syncSplitAvailability() {
    const admitted = splitAdmitted();
    const btn = viewBtns.split;
    if (admitted) {
      btn.removeAttribute("aria-disabled");
      btn.removeAttribute("title");
    } else {
      btn.setAttribute("aria-disabled", "true");
      btn.title = "Split needs a wider workspace. Close Comments or widen the window.";
    }
    if (state.view === "split" && !admitted) {
      setView("rendered", { keepSuspended: true });
      splitSuspended = true;
      announceAnnotation("Split paused: the workspace is too narrow for two panes. It returns when there is room.");
    } else if (admitted && splitSuspended && state.view === "rendered") {
      setView("split");
    }
    syncPaneScrollports();
  }

  function isSplitView() {
    return !!viewWrap && viewWrap.dataset.okfView === "split"
      && splitSurfaceQuery.matches;
  }

  function setView(mode, opts) {
    if (mode !== "rendered" && mode !== "source" && mode !== "split") return;
    ensureViewWrap();
    // Split is a simultaneous surface only where both panes stay readable. A
    // split deep-link or request without the room resolves to the
    // safe/default Rendered pane and is remembered, so it returns with the
    // room; source content and the persisted split ratio remain intact.
    const wantedSplit = mode === "split";
    if (wantedSplit && !splitAdmitted()) mode = "rendered";
    if (!(opts && opts.keepSuspended)) splitSuspended = wantedSplit && mode !== "split";
    state.view = mode;
    if (viewWrap) viewWrap.dataset.okfView = mode;
    if (sourcePre) sourcePre.hidden = (mode === "rendered");
    if (splitDivider) splitDivider.hidden = (mode !== "split");
    Object.keys(viewBtns).forEach((k) => viewBtns[k].setAttribute("aria-pressed", k === mode ? "true" : "false"));
    // Deep-link via ?view=. Preserves everything else (no reload).
    const url = new URL(window.location.href);
    if (mode === "rendered") url.searchParams.delete("view");
    else url.searchParams.set("view", mode);
    window.history.replaceState(null, "", url.toString());
    // Source/split need the raw markdown; load lazily.
    if (mode !== "rendered") ensureSourceLoaded();
    if (sourceStateNode) setSourceState();
    applySourceWrap();
    if (wantedSplit && mode !== "split") {
      announceAnnotation("Split needs a wider workspace. Showing Rendered; Split returns when there is room.");
    }
  }

  const reconcileSplitSurface = () => syncSplitAvailability();
  if (typeof splitSurfaceQuery.addEventListener === "function") {
    splitSurfaceQuery.addEventListener("change", reconcileSplitSurface);
  } else if (typeof splitSurfaceQuery.addListener === "function") {
    splitSurfaceQuery.addListener(reconcileSplitSurface);
  }

  // ---- F6: lazy Source acquisition is a canonical document read ---------
  // The pane used to bare-fetch /__data/doc, ignore the commitDocument()
  // verdict, and publish itself unconditionally: no generation/route
  // admission, no deadline, and a failure was console-only (blank pane). A
  // held stale response could then commit AFTER a newer live revision and
  // regress the visible body, Source, and annotation state — the A→B→A
  // class the spec's async-lifecycle matrix forbids. The pane now reads
  // through the shared bounded GET owner (absolute deadline + bounded
  // attempts + operation-registry supersession), is admitted only while its
  // concept/navigation-epoch/document-generation identity still owns the
  // document, publishes only after commitDocument() succeeds, and reports
  // loading/unavailable/Retry instead of failing silently.
  let sourceStateNode = null;
  var sourceStateMode = null;
  function setSourceState(mode) {
    if (!viewWrap || !sourcePre) return;
    if (mode === "loading" || mode === "error" || mode === null) sourceStateMode = mode;
    if (!sourceStateNode || !sourceStateNode.isConnected) {
      // A sibling ABOVE the view wrap: viewWrap is a positional grid in
      // split view, so the notice must never become a grid item.
      sourceStateNode = el("div", { class: "okf-source-state", hidden: "" });
      viewWrap.parentNode.insertBefore(sourceStateNode, viewWrap);
    }
    sourceStateNode.hidden = !sourceStateMode || state.view === "rendered";
    if (sourceStateNode.hidden) return;
    if (sourceStateMode === "loading") {
      sourceStateNode.className = "okf-source-state";
      sourceStateNode.setAttribute("role", "status");
      sourceStateNode.setAttribute("aria-live", "polite");
      sourceStateNode.setAttribute("aria-busy", "true");
      sourceStateNode.replaceChildren(document.createTextNode("Loading source…"));
      return;
    }
    if (sourceStateMode === "error") {
      sourceStateNode.className = "okf-source-state okf-source-state--error";
      sourceStateNode.setAttribute("role", "alert");
      sourceStateNode.removeAttribute("aria-live");
      sourceStateNode.removeAttribute("aria-busy");
      var retry = el("button", { type: "button", class: "okf-studiobtn", text: "Retry loading source" });
      retry.addEventListener("click", function () {
        setSourceState("loading");
        ensureSourceLoaded(true);
      });
      sourceStateNode.replaceChildren(
        el("p", { text: "Source could not be loaded. The rendered document is unchanged." }),
        retry);
      return;
    }
    sourceStateNode.hidden = true;
    sourceStateNode.replaceChildren();
  }
  // A superseded source read never publishes. When the pane is already
  // owned and populated by a newer commit, drop stale feedback; otherwise a
  // newer in-flight request owns the loading state.
  function settleSupersededSource() {
    if (sourcePre && sourcePre.dataset.loaded === "1") setSourceState(null);
  }

  function ensureSourceLoaded(force) {
    if (!sourcePre || !viewWrap) return;
    if (!force && sourcePre.dataset.loaded === "1") return;
    const id = state.conceptId;
    if (!id) return;
    const requestGeneration = ++state.sourceLoadGeneration;
    const fenceConcept = id;
    const fenceDocGen = state.documentGeneration;
    const fenceNavEpoch = window.okfLoomLive ? window.okfLoomLive._getNavEpoch() : 0;
    const stillOwns = () => requestGeneration === state.sourceLoadGeneration &&
      state.conceptId === fenceConcept &&
      state.documentGeneration === fenceDocGen &&
      !(window.okfLoomLive && window.okfLoomLive._getNavEpoch() !== fenceNavEpoch);
    setSourceState("loading");
    annotationFetch("/__data/doc?id=" + encodeURIComponent(id),
      { headers: { Accept: "application/json" }, operationKey: "source-doc" }, "get")
      .then((res) => {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then((doc) => {
        if (!doc || !stillOwns()) { settleSupersededSource(); return; }
        // F6 publication-identity fence: the FIRST canonical publication
        // (null→doc, e.g. the boot-time comments/document reader or the live
        // initial reconciliation committing before this read returns) does
        // not advance documentGeneration, so the generation fence above alone
        // admits a response that predates that first commit and would roll
        // the canonical document back. A lazy Source response is therefore
        // admitted only while the canonical revision it carries is still the
        // revision the committed document owns: a same-revision re-commit
        // (harmless, idempotent content) still admits, while any intervening
        // publication of a DIFFERENT revision — including the first one —
        // supersedes this read. Existing generation/epoch semantics are
        // untouched; this is a narrow response-side identity check.
        var canonicalRev = state.doc && state.doc.concept_content_rev;
        if (canonicalRev && canonicalRev !== doc.concept_content_rev) {
          settleSupersededSource();
          console.debug("[okf-studio] source load superseded by a newer canonical publication (expected)");
          return;
        }
        // Publish only through the canonical commit owner. A false return
        // means staging failed and every touched region rolled back — the
        // Source pane must never show bytes the committed document does
        // not own.
        if (!commitDocument(doc, { pulse: false })) {
          setSourceState("error");
          return;
        }
        setSourceState(null);
        syncPaneScrollports();
      })
      .catch((e) => {
        // F15: owned cancellation (superseded by a newer revision commit, a
        // navigation-epoch advance, or a newer source request) is the
        // expected terminal state, not a failure — no error feedback, and
        // the diagnostic stays at debug level. Genuine transport, HTTP,
        // parse, and absolute-deadline failures stay loud and show the
        // explicit unavailable + Retry state.
        if ((e && e.name === "AbortError" && e.message === "Superseded") || !stillOwns()) {
          settleSupersededSource();
          console.debug("[okf-studio] source load superseded by a newer owner (expected)", e);
          return;
        }
        console.error("[okf-studio] source load failed", e);
        // H3: a failed read whose pane another canonical publication has
        // already filled reports nothing false: the pane shows committed
        // bytes, so "Source could not be loaded" would be untrue there.
        if (sourcePre && sourcePre.dataset.loaded === "1") { setSourceState(null); return; }
        setSourceState("error");
      });
  }

  // Called by live.js after a `changed` on the open concept. Renders the
  // doc in the current view mode and updates title/description in place.
  // iter1 CRI-001: replaces the whole-body innerHTML swap with a block-level
  // diff that patches only the changed/moved/inserted/removed blocks, so a
  // single sentence edit no longer reads as a full-body flash. Returns true
  // so live.js knows the scoped pulse was handled here (not in its fallback).
      function applyDoc(doc, opts) {
      opts = opts || {};
      if (!doc) return false;
      return commitDocument(doc, opts);
    }
    // Q4: true when a canonical read captured `publication` before its
    // request and, meanwhile, a document of a DIFFERENT revision than `rev`
    // was published. Its response must not commit over that newer document.
    // A same-revision re-commit is harmless and never supersedes.
    function supersededByPublication(publication, rev) {
      var current = state.doc && state.doc.concept_content_rev;
      return state.documentPublication !== publication && !!current && current !== rev;
    }
    // What the page body currently shows. Until the first commit that is the
    // server-rendered body, which is byte-identical to /__data/doc `html` at
    // the revision the page's own source map names; afterwards it is the last
    // committed HTML. It outlives commitDocument(null): invalidating state
    // does not change what is on screen.
    var shownBodyHtml = null;
    var SSR_CONTENT_REV = (function() {
      try {
        var node = document.getElementById("okf-annotation-projection");
        var rev = node && JSON.parse(node.textContent || "null");
        rev = rev && rev.concept_content_rev;
        return /^[0-9a-f]{64}$/.test(rev || "") ? rev : "";
      } catch (e) { return ""; }
    })();
    // Group 4: the single canonical document-commit path. Every code path
      // that replaces the authoritative document MUST route through here:
      // loadComments, ensureCanonicalDoc, live patches, and conflict refresh.
      // This centralises generation invalidation, operation abort, selection
      // clearing, mark/projection invalidation, and editor conflict.
    function commitDocument(doc, opts) {
        opts = opts || {};
        if (!doc) {
          // Group 4: null invalidation must invalidate generations, operations,
          // selection/draft, marks/projection, and conflict the editor.
          state.doc = null;
          state.documentGeneration++;
          state.annotationGeneration++;
          state.selectionDraft = null;
          if (typeof document !== "undefined" && document.body) delete document.body.dataset.okfSelectionReady;
          abortAnnotationOperations("doc-nulled");
          if (state.annotationEditor) {
            state.annotationEditor.mode = "conflict";
            state.annotationEditor.anchor = null;
            state.annotationEditor.message = "The document is being refreshed.";
            syncSelectionModeBanner();
          }
          return false;
        }
        const previousContentRev = state.doc && state.doc.concept_content_rev;
        const focusedMark = document.activeElement && document.activeElement.matches && document.activeElement.matches("mark.okf-comment-mark") ? document.activeElement.getAttribute("data-comment-id") : null;
        const revisionChanged = !!(previousContentRev && doc.concept_content_rev && previousContentRev !== doc.concept_content_rev);
        const editorRevisionChanged = !!(state.annotationEditor && revisionChanged);
        // R6-4/R7-4: TRANSACTIONAL commit. Stage ALL DOM mutations (title, desc,
        // body diff, sourcePre) first. Only if staging succeeds, publish
        // state/generation/draft/editor/operation changes atomically. On
        // injected failure at ANY phase, ROLL BACK every touched DOM region to
        // its pre-patch snapshot so the document is never left in a
        // mixed-revision state (old state.doc while DOM contains new title /
        // partially-diffed body). The previous code only returned false on
        // failure without restoring the earlier mutations in the same try
        // block — leaving a partially-patched DOM.
        var changedBlocks = [];
        var titleEl = $(".okf-page__title");
        var descEl = $(".okf-page__description");
        var body = $(".okf-page__body");
        var metadataSlot = $("[data-okf-metadata-slot]");
        var snapMetadata = metadataSlot ? metadataSlot.innerHTML : null;
        var snapMetadataInput = metadataSlot ? metadataSlot._okfMetadataInput : null;
        var metadataOpen = metadataSlot && metadataSlot.querySelector("details[open]");
        var metadataFocus = metadataSlot && metadataSlot.contains(document.activeElement) ? document.activeElement : null;
        var metadataFocusHref = metadataFocus && metadataFocus.getAttribute("href");
        // R7-4: capture old values for EVERY touched region before ANY mutation
        // so a failure at any phase can fully restore all regions.
        var snapTitle = titleEl ? titleEl.textContent : null;
        var snapDesc = descEl ? descEl.textContent : null;
        var snapBodyHtml = body ? body.innerHTML : null;
        var snapSourceText = sourcePre ? sourcePre.textContent : null;
        var snapSourceLoaded = sourcePre ? sourcePre.dataset.loaded : null;
        try {
          if (metadataSlot && typeof doc.metadata_html === "string" && doc.metadata_html !== metadataSlot._okfMetadataInput) {
            metadataSlot.innerHTML = doc.metadata_html;
            metadataSlot._okfMetadataInput = doc.metadata_html;
            var metadataDetails = metadataSlot.querySelector("details");
            if (metadataOpen && metadataDetails) metadataDetails.open = true;
            if (metadataFocus) {
              var restoredMetadataFocus = metadataFocus.id && document.getElementById(metadataFocus.id);
              if (!restoredMetadataFocus && metadataFocusHref) {
                restoredMetadataFocus = Array.from(metadataSlot.querySelectorAll("a[href]")).find(function(link) {
                  return link.getAttribute("href") === metadataFocusHref && link.textContent === metadataFocus.textContent;
                });
              }
              (restoredMetadataFocus || metadataSlot.querySelector("summary")).focus({preventScroll:true});
            }
          }
          if (titleEl && typeof doc.title === "string" && doc.title !== titleEl.textContent) {
            titleEl.textContent = doc.title;
          }
          if (descEl && typeof doc.description === "string") descEl.textContent = doc.description;
          // Patch only when the body does not already show this HTML. The
          // diff compares raw server HTML with the enhanced DOM, so patching an
          // unchanged document replaced every enhanced block (anchored
          // headings, code, tables), pulsed it as "changed", and dropped a
          // selection inside it. That happened on every load, twice: the
          // comments load and the live "ready" handshake both re-fetch the
          // document the server has just rendered into the page.
          var sameHtml = typeof doc.html === "string" && (shownBodyHtml !== null
            ? shownBodyHtml === doc.html
            : !!(SSR_CONTENT_REV && SSR_CONTENT_REV === doc.concept_content_rev));
          if (body && typeof doc.html === "string" && !sameHtml) {
            var sel = saveSelectionAcrossPatch(body);
            changedBlocks = diffAndPatchBody(body, doc.html);
            sel.restore(changedBlocks);
          }
          if (sourcePre) {
            sourcePre.textContent = doc.raw || "";
            sourcePre.dataset.loaded = "1";
          }
        } catch (patchErr) {
          // R7-4: staging failed — restore EVERY touched DOM region to its
          // pre-patch snapshot so no partial/mixed-revision DOM is visible.
          console.error("[okf-studio] commitDocument staging failed", patchErr);
          try {
            if (titleEl && snapTitle != null) titleEl.textContent = snapTitle;
            if (descEl && snapDesc != null) descEl.textContent = snapDesc;
            if (body && snapBodyHtml != null) body.innerHTML = snapBodyHtml;
            if (metadataSlot && snapMetadata != null) {
              metadataSlot.innerHTML = snapMetadata;
              metadataSlot._okfMetadataInput = snapMetadataInput;
            }
            if (sourcePre && snapSourceText != null) {
              sourcePre.textContent = snapSourceText;
              if (snapSourceLoaded != null) sourcePre.dataset.loaded = snapSourceLoaded;
            }
          } catch (restoreErr) { console.error("[okf-studio] commitDocument rollback failed", restoreErr); }
          return false;
        }
        // COMMIT: publish state changes atomically. All DOM staging succeeded.
        state.doc = doc;
        // Q4: a publication is a change of the shown revision, including the
        // first document. A same-revision re-commit (the refetch after a
        // comment event, lazy Source) is not one, so it cannot make a newer
        // canonical read look stale and leave the page behind the server.
        if (!previousContentRev || previousContentRev !== doc.concept_content_rev) state.documentPublication++;
        if (body && typeof doc.html === "string") shownBodyHtml = doc.html;
        if (revisionChanged) {
          state.documentGeneration++;
          state.annotationGeneration++;
          state.selectionDraft = null;
          state._pendingMarkId = null;
          state.draftContentRev = null;
          // Exact evidence is keyed to the old revision and is dropped. A
          // page-level draft has no offsets, so its retained selection context
          // (Source/graph fallback) survives the revision change.
          if (!(state.draftAnchor && isPageLevelAnchor(state.draftAnchor) && state.draftAnchor.context) && !draftIsPinned()) state.draftAnchor = defaultDraftAnchor();
          if (typeof document !== "undefined" && document.body) delete document.body.dataset.okfSelectionReady;
          abortAnnotationOperations("revision-changed");
        }
        if (state.annotationEditor && revisionChanged) {
          state.annotationEditor.generation = state.annotationGeneration;
          state.annotationEditor.mode = "conflict";
          state.annotationEditor.anchor = null;
          state.annotationEditor.message = "The document changed before this anchor was saved.";
          syncSelectionModeBanner();
        }
    // Post-commit DOM updates: marks, markers, pulse — all read the new
    // state.doc and operate on the already-patched body.
    bindSelectionAffordance();
        applyCommentMarks();
        if (focusedMark) {
          const currentMark = document.querySelector('.okf-comment-mark[data-comment-id="' + cssEscape(focusedMark) + '"]');
          if (currentMark) { currentMark.setAttribute("tabindex", "-1"); currentMark.focus({ preventScroll: true }); }
        }
    applyPendingDraftMark();
        rebuildMarginMarkers();
        if (editorRevisionChanged) renderCommentsPanel();
    // R6-4: a revision change can enable/disable the composer submit gate
    // (draftContentRev vs the new doc rev). Refresh after every commit so
    // the button reflects the current canonical rev even when the composer
    // zone is preserved across the rebuild.
    _refreshComposerSubmitState();
    // H3: the Source pane's loading/unavailable notice describes the PANE,
    // not one request. Every successful canonical publication writes the
    // committed source into the pane (above), so it settles the notice here.
    // Before, only the lazy Source read itself cleared it: when the boot
    // comments/document read, the live "ready" refetch or a live patch
    // filled the pane first, Split showed the loaded source under a stale
    // "Loading source…" line until the redundant read landed (seconds on a
    // loaded host). The read in flight is not cancelled: it may carry a
    // newer revision, and its own admission fences still decide whether it
    // commits.
    if (sourcePre && sourcePre.dataset.loaded === "1" && sourceStateMode) setSourceState(null);
    if (opts.pulse && changedBlocks.length) pulseBlocks(changedBlocks);
    if (changedBlocks.length > 0) {
      try { window.dispatchEvent(new CustomEvent("okf-loom:bodyPatched")); } catch (e) {}
    }
    return true; // live.js must NOT also pulse the whole body.
  }

  // ---- block-level diff + patch (iter1 CRI-001) ------------------------
  // Splits rendered HTML at block boundaries (heading / paragraph / list /
  // table / pre / blockquote / hr), aligns old vs new via LCS on a
  // normalised signature, and applies the minimal DOM mutation set. List
  // and table blocks that exist on both sides but differ recurse one level
  // to diff their <li>/<tr> children, so "agent added one item to a list"
  // swaps only that item, not the whole list. Depth is capped at 2 so a
  // pathological nested structure can't blow the stack.
  const BLOCK_SELECTOR = "h1,h2,h3,h4,h5,h6,p,ul,ol,table,pre,blockquote,hr,div";
  function blockChildren(parent) {
    const out = [];
    for (let n = parent.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 1) out.push(n);
    }
    return out;
  }
  function blockSig(el) {
    // Whitespace-collapsed text + tag + id (heading ids are stable anchors
    // for comment marks; including them keeps a renamed heading "changed").
    const tag = el.tagName.toLowerCase();
    const id = el.getAttribute("id") || "";
    // Mermaid/math blocks: after CDN rendering (mermaid.js, KaTeX), the
    // element's textContent changes from the raw source to the rendered
    // SVG/MathML output. Use the data attribute (the original source) for
    // the signature so the diff treats "rendered" and "raw" versions of
    // the SAME diagram as equal — preventing a visible flash-to-raw-text
    // on live patches that don't actually change the diagram.
    if (el.classList && (el.classList.contains("mermaid") || el.classList.contains("math"))) {
      var source = el.getAttribute("data-source") || el.textContent || "";
      return tag + "|" + id + "|" + source.replace(/\s+/g, " ").trim().slice(0, 200);
    }
    // Enhancement wrappers (renderers.js table/code UX): sign as the INNER
    // block so an enhanced live table/pre compares equal to the bare
    // server-rendered element on the other side of the diff. The tablewrap
    // uses data-source (the original text captured at enhance time) because
    // user-applied sorting reorders the live textContent without the
    // content having changed. Full-length (no 200-char slice) on both the
    // wrapper AND bare table/pre sides: a sorted table means row-level
    // recursion can't reconcile order, so equality must be exact — a
    // truncated signature would silently drop edits past the prefix.
    if (el.classList && el.classList.contains("okf-tablewrap")) {
      return "table|" + id + "|" + (el.getAttribute("data-source") || "");
    }
    if (el.classList && el.classList.contains("okf-codewrap")) {
      var inner = el.querySelector("pre");
      var innerText = inner ? (inner.textContent || "") : "";
      return "pre|" + id + "|" + innerText.replace(/\s+/g, " ").trim();
    }
    if (tag === "table" || tag === "pre") {
      return tag + "|" + id + "|" + (el.textContent || "").replace(/\s+/g, " ").trim();
    }
    const text = (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 200);
    return tag + "|" + id + "|" + text;
  }
  function parseHtmlToBlocks(html) {
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    return blockChildren(tmp);
  }
  // Standard LCS DP over signature arrays → edit ops. Returns an array of
  // {op: "keep"|"replace"|"insert"|"remove", oldEl?, newEl?}.
  function lcsOps(oldBlocks, newBlocks) {
    const n = oldBlocks.length, m = newBlocks.length;
    const oldSigs = oldBlocks.map(blockSig);
    const newSigs = newBlocks.map(blockSig);
    // dp[i][j] = LCS length of oldSigs[i:] and newSigs[j:].
    const dp = [];
    for (let i = 0; i <= n; i++) dp.push(new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = oldSigs[i] === newSigs[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const ops = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (oldSigs[i] === newSigs[j]) { ops.push({ op: "keep", oldEl: oldBlocks[i], newEl: newBlocks[j] }); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ op: "remove", oldEl: oldBlocks[i] }); i++; }
      else { ops.push({ op: "insert", newEl: newBlocks[j] }); j++; }
    }
    while (i < n) { ops.push({ op: "remove", oldEl: oldBlocks[i] }); i++; }
    while (j < m) { ops.push({ op: "insert", newEl: newBlocks[j] }); j++; }
    return ops;
  }
  // diffAndPatchBody mutates `parent` to match `newHtml` at the block level.
  // Returns the list of newly-inserted or replaced element nodes (for the
  // scoped pulse). Recurses into matching list/table blocks that differ.
  function diffAndPatchBody(parent, newHtml) {
    const newBlocks = parseHtmlToBlocks(newHtml);
    return diffChildren(parent, newBlocks, 0);
  }
  function diffChildren(parent, newBlocks, depth) {
    const oldBlocks = blockChildren(parent);
    const ops = lcsOps(oldBlocks, newBlocks);
    const changed = [];
    // Apply ops. We rebuild the child list by walking ops and using
    // parent.insertBefore / removeChild so untouched nodes keep their
    // identity (and any in-body selection/focus on them survives).
    let cursor = parent.firstChild; // node we're currently considering in the live DOM
    for (let k = 0; k < ops.length; k++) {
      const op = ops[k];
      if (op.op === "keep") {
        // Recurse into matching containers whose children may differ.
        if (depth < 2 && (op.oldEl.tagName === "UL" || op.oldEl.tagName === "OL" || op.oldEl.tagName === "TABLE")) {
          const innerChanged = diffChildren(op.oldEl, blockChildren(op.newEl), depth + 1);
          if (innerChanged.length) { changed.push.apply(changed, innerChanged); }
        }
        cursor = op.oldEl.nextSibling;
      } else if (op.op === "replace") {
        // (Not produced by lcsOps directly; handled as remove+insert.)
      } else if (op.op === "remove") {
        const next = op.oldEl.nextSibling;
        parent.removeChild(op.oldEl);
        cursor = next;
      } else if (op.op === "insert") {
        const imported = parent.ownerDocument.importNode(op.newEl, true);
        parent.insertBefore(imported, cursor);
        changed.push(imported);
      }
    }
    return changed;
  }
  function pulseBlocks(blocks) {
    if (REDUCED_MOTION) return;
    blocks.forEach((b) => {
      b.classList.remove("okf-pulse");
      void b.offsetWidth; // restart the animation on consecutive patches
      b.classList.add("okf-pulse");
    });
  }

  // ---- selection preservation across a block patch (iter1 CRI-001) -----
  // Captures the current in-body selection. Because unchanged blocks keep
  // their DOM identity (diffChildren never touches them), a selection that
  // lives entirely inside an unchanged block survives automatically. Only
  // selections spanning a CHANGED block need a best-effort text-search
  // restore after the patch. Returns {restore(changedBlocks)}.
  function saveSelectionAcrossPatch(body) {
    const sel = window.getSelection && window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return { restore() {} };
    const range = sel.getRangeAt(0);
    if (!body.contains(range.commonAncestorContainer)) return { restore() {} };
    const text = sel.toString();
    if (!text) return { restore() {} };
    // Record the text of the block containing the anchor so we can tell
    // whether that block was replaced.
    const anchorBlock = containingBlock(range.startContainer, body);
    const anchorBlockText = anchorBlock ? (anchorBlock.textContent || "").replace(/\s+/g, " ").trim() : "";
    return {
      _text: text,
      _anchorBlockText: anchorBlockText,
      restore(changedBlocks) {
        if (!text) return;
        // If the anchor block wasn't changed, the live Selection is still
        // valid (the DOM node is untouched). Only re-resolve when a changed
        // block overlaps the prior selection.
        const changedSigs = changedBlocks.map((b) => (b.textContent || "").replace(/\s+/g, " ").trim());
        const anchorChanged = !anchorBlockText || changedSigs.indexOf(anchorBlockText) >= 0;
        if (!anchorChanged) return; // selection survived untouched
        // Best-effort: find the text anywhere in the body and reselect it.
        const found = findTextNode(body, text);
        if (!found) return;
        try {
          const r = document.createRange();
          r.setStart(found.node, found.start);
          r.setEnd(found.node, found.end);
          sel.removeAllRanges();
          sel.addRange(r);
        } catch (e) { /* give up silently; selection is best-effort */ }
      },
    };
  }
  function containingBlock(node, root) {
    let el = node.nodeType === 1 ? node : node.parentElement;
    while (el && el !== root) {
      if (el.tagName === "P" || /^H[1-6]$/.test(el.tagName) || el.tagName === "LI" ||
          el.tagName === "UL" || el.tagName === "OL" || el.tagName === "TABLE" ||
          el.tagName === "PRE" || el.tagName === "BLOCKQUOTE") return el;
      el = el.parentElement;
    }
    return root;
  }
  function findTextNode(root, text) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      const idx = node.nodeValue ? node.nodeValue.indexOf(text) : -1;
      if (idx >= 0) return { node: node, start: idx, end: idx + text.length };
    }
    return null;
  }

  // ====================================================================
  // 5. Comments (§9)
  // ====================================================================
  // Initial state reconstructed from the events feed (comment events), then
  // kept live by the `comment` SSE signal. Each event carries the full
  // latest record for an id (see studio.post_comment/update_comment).
        async function loadComments(skewAttempt, loadOptions) {
          skewAttempt = skewAttempt || 0;
          loadOptions = loadOptions || {};
          const requestGeneration = ++state.commentsRequestGeneration;
          // Snapshot transport identities at request admission. During an
          // authoritative reconnect, only live rows admitted after this point
          // may survive when absent from the fetched authority.
          const eventKeysAtRequest = new Set((state.events || []).map(eventAttentionKey).filter((key) => key != null));
          state.commentsReady = false;
          state.commentsStatus = "loading";
      const conceptKey = state.conceptId;
      const documentGeneration = state.documentGeneration;
      const documentPublication = state.documentPublication;
      // Req7: propagate settle success/failure to the caller (true on a
      // committed canonical tuple, false on every superseded / HTTP-failure /
      // skew / staging-failure path) so submission callers cannot resolve into
      // a false-success path. Existing callers that ignore the return value
      // are unaffected.
      try {
      // ARCH4-003 / QUA4-001 fix: fetch canonical comment state from
      // /__comments (directives.jsonl, last-write-wins) instead of
      // reconstructing from /__data/events (which had a desc-order +
      // last-iteration inversion bug showing claimed/resolved as "open").
        const [commentRes, eventRes, docRes] = await Promise.all([
            annotationFetch("/__comments", { headers: { Accept: "application/json" }, operationKey: "comments" }, "get"),
            annotationFetch("/__data/events?limit=500", { headers: { Accept: "application/json" }, operationKey: "events" }, "get"),
          state.conceptId && isConceptPage()
              ? annotationFetch("/__data/doc?id=" + encodeURIComponent(state.conceptId), { headers: { Accept: "application/json" }, operationKey: "comments-doc" }, "get")
            : Promise.resolve(null),
        ]);
          if (requestGeneration !== state.commentsRequestGeneration || conceptKey !== state.conceptId || documentGeneration !== state.documentGeneration) return false;
            // Group 5: mandatory responses must be successful before mutating
            // canonical client state. A terminal 4xx/5xx must preserve the
            // previous authoritative comments/doc/events and report transport
            // failure. Comments, events, AND document are a mandatory tuple.
            if (!commentRes.ok || !eventRes.ok) {
              console.error("[okf-studio] load comments/events HTTP failure", commentRes.status, eventRes.status);
              state.commentsStatus = "error"; return false;
            }
            if (docRes && !docRes.ok) {
              console.error("[okf-studio] load doc HTTP failure", docRes.status);
              state.commentsStatus = "error"; return false;
            }
          const commentData = await commentRes.json();
          const eventData = await eventRes.json();
          const fetchedDoc = docRes && docRes.ok ? await docRes.json() : null;
          if (requestGeneration !== state.commentsRequestGeneration || conceptKey !== state.conceptId || documentGeneration !== state.documentGeneration) return false;
            // R8-ATOMIC: STAGE the complete doc+comments+events tuple and
            // validate ALL of it (doc identity/projection, comment target
            // identity, row_token, anchor, and revision parity) BEFORE
            // committing ANY of it. The previous code committed the document
            // first and only then checked comment parity — so a new document
            // could coexist with stale comments/events while the operation
            // reported uncertainty or failure. Now the document is committed
            // only after the comment tuple is verified against it.
            var stagedDoc = null;
            if (fetchedDoc) {
              // Fail closed, but never silently: every sibling failure branch
              // names its cause, and this one decides the whole concept load.
              var docFault = fetchedDoc.id !== conceptKey ? "document identity mismatch"
                : projectionAdmissionError(fetchedDoc.annotation_projection, fetchedDoc.concept_content_rev, fetchedDoc.annotation_source, fetchedDoc.html);
              if (docFault) {
                console.error("[okf-studio] load doc projection invalid", docFault);
                return false;
              }
              stagedDoc = fetchedDoc;
            }
            // projectionRev is the rev the committed comments MUST match:
            // the staged doc's rev (if we have one), else the current doc's.
            var projectionRev = stagedDoc ? stagedDoc.concept_content_rev : (state.doc && state.doc.concept_content_rev);
          // A non-concept surface (graph, indexes) has no document member in its
          // tuple and no rows of its own, so no projection revision is required;
          // a concept page still fails closed without one.
          var needsProjectionRev = isConceptPage();
          if (requestGeneration !== state.commentsRequestGeneration || conceptKey !== state.conceptId || (needsProjectionRev && !projectionRev)) return false;
          // R10: route the FULL envelope (doc identity/projection, comments
          // container, every row's identity/row_token/anchor/resolution, events
          // container) through the shared staged-tuple validator BEFORE any
          // mutation. A non-array comments/events value (e.g. {}), a token-
          // bearing row without a valid anchor, or a foreign-concept document is
          // an all-or-nothing skew signal: retry then error, with nothing
          // silently filtered and no document committed next to an invalid tuple.
          var tuple = validateStagedTuple({ doc: stagedDoc, commentsData: commentData, eventData: eventData, conceptKey: conceptKey, fallbackProjectionRev: projectionRev });
          if (tuple.skewSignal) {
            console.error("[okf-studio] load comments tuple skew", tuple.skewSignal);
            if (skewAttempt < 2) return loadComments(skewAttempt + 1, loadOptions);
            state.commentsStatus = "error"; return false;
          }
          // R10: normalize/merge events BEFORE committing the doc. Live SSE
          // events may have been upserted into state.events WHILE this fetch was
          // in flight — a slow /__data/events response must NOT wipe a live
          // burst that already rendered. Merge by stable id (live record wins on
          // conflict; live extras stay on top in arrival order) into a LOCAL
          // array. state.events is assigned only AFTER the doc commit succeeds,
          // so a commit failure cannot leave a freshly-committed document beside
          // unmerged events (previously the doc committed first, then events
          // were normalized).
          var liveEvents = state.events || [];
          var liveByKey = new Map();
          liveEvents.forEach((e) => { const key = eventAttentionKey(e); if (key != null) liveByKey.set(key, e); });
          var seenKeys = new Set();
          var mergedEvents = [];
          tuple.events.forEach((e) => {
            if (!e) return;
            const key = eventAttentionKey(e);
            if (key != null && seenKeys.has(key)) return;
            if (key != null && liveByKey.has(key)) mergedEvents.push(liveByKey.get(key));
            else mergedEvents.push(e);
            if (key != null) seenKeys.add(key);
          });
          // An authoritative response removes rows that existed when capture
          // began, while preserving transport-keyed SSE rows admitted during
          // the request. Domain `id` is payload data and never participates in
          // event identity, so two events for the same concept both survive.
          var liveExtras = liveEvents.filter((e) => {
            const key = eventAttentionKey(e);
            if (key == null || seenKeys.has(key)) return false;
            return !loadOptions.authoritative || !eventKeysAtRequest.has(key);
          });
          var nextEvents = liveExtras.concat(mergedEvents);
          // Build the canonical comment list locally before any commit. Never
          // let a background snapshot erase the row owned by a local recovery
          // editor; explicit live removal remains authoritative via its own path.
          var canonicalComments = tuple.comments.slice();
          if (state.annotationEditor && !canonicalComments.some(function (row) { return row.id === state.annotationEditor.commentId; })) {
            const editingRow = state.comments.find(function (row) { return row.id === state.annotationEditor.commentId; });
            if (editingRow) canonicalComments.push(editingRow);
          }
          canonicalComments = canonicalComments.sort(byTsDesc);
            // R10-COMMIT: commit the doc only after the FULL tuple validated and
            // events normalized; REQUIRE commitDocument() to return true before
            // committing comments/events. commitDocument returns false when its
            // DOM staging fails (it rolls back every touched region) — the
            // previous code ignored the return value, leaving comments/events
            // committed beside a rolled-back document. A concurrent document
            // change during the fetch+validate window must not let a stale doc
            // regress state, so re-fence first.
            if (stagedDoc) {
              if (requestGeneration !== state.commentsRequestGeneration || conceptKey !== state.conceptId || documentGeneration !== state.documentGeneration) return false;
              // Q4: the first publication (null -> doc) does not move
              // documentGeneration, so a boot read that returned revision A
              // after a live patch published B would roll the page back to A.
              // A tuple read before a newer publication of another revision is
              // superseded: read again within the retry budget, else report
              // the error (the finally never leaves "loading" behind).
              if (supersededByPublication(documentPublication, stagedDoc.concept_content_rev)) {
                console.debug("[okf-studio] load comments superseded by a newer document publication (expected)");
                if (skewAttempt < 2) return loadComments(skewAttempt + 1, loadOptions);
                state.commentsStatus = "error"; return false;
              }
              if (!commitDocument(stagedDoc, { pulse: false })) {
                console.error("[okf-studio] load comments commitDocument staging failed");
                state.commentsStatus = "error"; return false;
              }
              projectionRev = state.doc && state.doc.concept_content_rev;
            }
          if (requestGeneration !== state.commentsRequestGeneration || conceptKey !== state.conceptId || (needsProjectionRev && !projectionRev)) return false;
          // COMMIT: all validation + normalization succeeded and the doc
          // committed (when staged). Publish events + comments together.
          reconcileAttention(nextEvents);
          state.events = nextEvents;
          state.comments = canonicalComments;
          // Resolve disposable projections before rendering health/actions so
          // Attached never advertises Jump without a verified DOM target.
          applyCommentMarks();
          // F4: publish terminal readiness BEFORE rendering the committed
          // tuple. The panel renderer inserts an aria-busy "Loading…" node
          // whenever !state.commentsReady, so the previous order (render
          // first, publish after) made every successful refresh re-insert
          // that node with no redraw of its own — a quiet single-user window
          // kept saved rows coexisting with "Loading comments…" (and
          // first-load-empty panels loading-only) until an unrelated event
          // happened to rerender. Publishing the settled status first makes
          // the success render show real rows with honest busy semantics.
          state.commentsReady = true;
          state.eventsReady = true;
          state.commentsStatus = "ready";
          // A background snapshot must not replace the focused recovery DOM.
          // Relevant live events own conflict transitions separately; save
          // completion closes and renders only after reconciliation.
          if (!state.annotationEditor) renderCommentsPanel();
      renderChangeList();
      updateBadges();
          rebuildMarginMarkers();
          // The page entry reads the settled status (published above).
          updatePageFeedback();
          buildIndexEngagement();
          return true;
        } catch (e) {
          if (requestGeneration !== state.commentsRequestGeneration) return false;
          state.commentsStatus = "error";
          // F15: annotationFetch normalises every owned operation-signal
          // abort (same-key supersession, revision-changed/doc-nulled
          // invalidation, pagehide disposal) to exactly
          // DOMException("Superseded", "AbortError"). That class is the
          // expected terminal state of supersession/page-leave cancellation,
          // not an application error — report it at debug level. Genuine
          // transport failure, HTTP failure, parse failure, the absolute
          // deadline, and any unowned abort keep the loud error path.
          if (e && e.name === "AbortError" && e.message === "Superseded") {
            console.debug("[okf-studio] load comments/events superseded by a newer owner (expected)", e);
          } else {
            console.error("[okf-studio] load comments/events failed", e);
          }
          return false;
        } finally {
          if (requestGeneration === state.commentsRequestGeneration && state.commentsStatus === "loading") state.commentsStatus = "error";
          if (requestGeneration === state.commentsRequestGeneration && state.commentsStatus === "error") {
            updatePageFeedback();
            if (state.openPanel === "comments") renderCommentsPanel();
            else if (state.openPanel === "changes") renderChangeList();
            else if (state.openPanel === "agent-activity" && panels["agent-activity"]) panels["agent-activity"].render(panelBody);
          }
        }
  }
  function byTsDesc(a, b) {
    const ta = a.ts || "", tb = b.ts || "";
    if (ta < tb) return 1; if (ta > tb) return -1; return 0;
  }
  // id-based upsert (last write wins). Guards against the SSE-vs-POST race
  // where the `comment` event and the POST /__comment response both carry the
  // confirmed record - without dedup the optimistic row + the SSE insert +
  // the POST reconcile could leave 2-3 copies of the same id in state.
  function upsertComment(c) {
    if (!c || !c.id) return;
    const idx = state.comments.findIndex((x) => x.id === c.id);
    if (idx >= 0) state.comments[idx] = Object.assign({}, state.comments[idx], c);
    else state.comments.unshift(Object.assign({ ts: new Date().toISOString() }, c));
    state.comments.sort(byTsDesc);
  }

  // --- selection affordance --------------------------------------------
  // iter1 CRI-017: aria-live announces the affordance when it appears (it
  // shows in response to a user selection), and the leading glyph is an
  // inline SVG with aria-hidden instead of an emoji.
  let affordance;
  let selectionInput = "pointer";
  const COMMENT_ICON_SVG = '<svg class="okf-comment-afford__icon" viewBox="0 0 16 16" fill="none" aria-hidden="true" focusable="false"><path d="M2 3h12v8H6l-3 3v-3H2z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  function ensureAffordance() {
    if (affordance) return affordance;
    affordance = el("div", { class: "okf-comment-afford", hidden: "", role: "status", "aria-live": "polite" });
    // U6: the accessible name states the honest action. The button hands the
    // selection to the comment composer (an agent may later pick up the
    // comment as work, but the user is never messaging an agent directly
    // here), so the former "Comment for agent…" possessive is retired.
    const btn = el("button", { type: "button", class: "okf-studiobtn", "aria-label": "Comment on the selected text",
      "aria-describedby": "okf-comment-afford-note" });
    btn.innerHTML = COMMENT_ICON_SVG;
    btn.appendChild(document.createTextNode(" Comment"));
    btn.addEventListener("mousedown", (e) => e.preventDefault()); // keep selection
    btn.addEventListener("click", onAffordanceClick);
    affordance.appendChild(btn);
    document.body.appendChild(affordance);
    // R3: one selection can feed two actions (this comment, and the reader's
    // Copy selection with citation). The difference is the button's
    // description. The note lives OUTSIDE the role=status wrapper so it is
    // not announced every time the button appears, and it is hidden: a
    // description reference still reads it, but browse mode never meets it
    // as stray text at the end of the page.
    document.body.appendChild(el("span", { id: "okf-comment-afford-note", hidden: "",
      text: "Starts a comment on the selected text. To reuse the text elsewhere, use Copy selection with citation." }));
    return affordance;
  }
  // R3: the concept template's hint names only the copy action a static page
  // has. A Studio that can comment also starts a comment from the same
  // selection, so say both, once, at boot (no page swap re-renders it).
  if (CAN_COMMENT) {
    const selectionHint = document.querySelector(".okf-selection-hint");
    if (selectionHint) selectionHint.textContent = "Select text to comment on it, or to copy it with a citation.";
  }
  function commentSelectionOrigin(inBody) {
    if (!inBody || !inBody.range) return null;
    const node = inBody.range.startContainer;
    const start = node && (node.nodeType === 1 ? node : node.parentElement);
    if (!start) return null;
    const block = start.closest("[data-okf-projection-key], p, li, blockquote, pre, h1, h2, h3, h4, h5, h6, .okf-source, #detail-body");
    return block && block.isConnected ? block : null;
  }
  function transitionCommentLifecycle(next, opts) {
    opts = opts || {};
    if (["idle", "selection-action", "anchored-composer", "concept-composer", "closed"].indexOf(next) < 0) return false;
    if (next === "selection-action") {
      state.selectionDraft = opts.selectionDraft || state.selectionDraft;
      state.selectionOrigin = opts.origin || state.selectionOrigin;
    } else if (next === "idle") {
      state.selectionDraft = null;
      if (affordance) affordance.hidden = true;
      delete document.body.dataset.okfSelectionReady;
      if (opts.clearBrowserSelection) {
        try { window.getSelection().removeAllRanges(); } catch (e) {}
      }
    } else if (next === "concept-composer") {
      if (state._pendingMarkId) removeCommentMark(state._pendingMarkId);
      state._pendingMarkId = null;
      // Opening the panel and Escape drop selected-text context, but a pinned
      // graph draft keeps the concept it was written for (R4).
      state.draftAnchor = retainedDraftAnchor({ dropContext: true });
      state.draftContentRev = null;
    }
    state.commentLifecycle = next;
    return true;
  }
  // The selection affordance owns exactly one lifecycle state:
  // "selection-action". Its end-of-selection signals are late and unrelated to
  // the Comments panel - selectionchange is debounced 120ms and also fires for
  // caret moves inside the composer textarea; scroll and resize fire anywhere.
  // Releasing therefore always retires the affordance and its captured draft,
  // but hands the lifecycle back only when the affordance still holds it. An
  // open composer keeps its composer state and a closed panel stays "closed";
  // neither is overwritten with "idle" by a selection that was already gone.
  function releaseSelectionAction() {
    if (affordance) affordance.hidden = true;
    state.selectionDraft = null;
    delete document.body.dataset.okfSelectionReady;
    if (state.commentLifecycle !== "selection-action" && state.commentLifecycle !== "idle") return;
    if (state.openPanel === "comments") {
      // Same derivation as openPanel, without its draft-resetting transition:
      // a selection made beside an open composer never discards that draft.
      const draftAnchor = state.draftAnchor || {};
      state.commentLifecycle = draftAnchor.kind && !isPlainPageDraft(draftAnchor) ? "anchored-composer" : "concept-composer";
      return;
    }
    state.commentLifecycle = "idle";
  }
  function restoreCommentOriginFocus() {
    // F5: the fallback must be a PERSISTENT, connected control. The shell
    // Comments toggle mounts only inside the Studio home panel, so by the
    // time the Comments panel closes it is disconnected and focusing it was
    // a silent no-op that left focus on <body> (exactly where a palette
    // activation had left it). The Studio launcher is mounted on every
    // route at every width and owns the Studio home with Comments inside.
    const commentsFallback = commentsBtn.isConnected ? commentsBtn : studioLauncher;
    let target = state.selectionOrigin;
    if (target && target.isConnected && !target.closest("[hidden], [aria-hidden='true']")) {
      if (!target.matches("a[href], button, input, textarea, select, details, [tabindex]")) target.setAttribute("tabindex", "-1");
    } else target = commentsFallback;
    if (!target || !target.isConnected || target === document.body) target = studioLauncher;
    try { target.focus({ preventScroll: true }); } catch (e) { try { studioLauncher.focus({ preventScroll: true }); } catch (err) {} }
  }
  document.addEventListener("pointerdown", function () { selectionInput = "pointer"; }, true);
  document.addEventListener("keydown", function (e) {
    if (e.shiftKey && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].indexOf(e.key) >= 0) {
      selectionInput = "keyboard";
    }
    if (e.key !== "Escape" || e.defaultPrevented || state.commentLifecycle !== "selection-action") return;
    if (state.openPanel || isEditableTarget(e.target)) return;
    if (!_overlayIsTopmost(affordance)) return;
    e.preventDefault();
    e.stopPropagation();
    transitionCommentLifecycle("idle", { clearBrowserSelection: true });
  }, true);
  // Stable debounced handlers, created once: bindSelectionAffordance is
  // re-run after every SSE body patch (applyDoc), and a fresh closure per
  // call would stack a new listener each time (addEventListener only
  // dedupes identical function references) — an unbounded leak over a
  // live session.
  const _affordanceOnSelection = debounce(updateAffordance, 120);
  const _affordanceOnResize = debounce(hideAffordance, 120);
    function bindSelectionAffordance() {
    if (!CAN_COMMENT) return;
    // Bind on every commentable surface: concept pages, the graph detail
    // panel, and reserved index/log documents (page-level feedback only).
    if (!isConceptPage() && !document.getElementById("detail-body") && !feedbackPageRoot()) return;
    ensureAffordance();
    // On concept pages, .okf-page__body exists. On graph pages, it doesn't
    // but #detail-body does — don't bail if one is missing, just check the
    // other in getSelectionInBody. Stable references make re-binding after
    // each patch a no-op.
      document.addEventListener("selectionchange", _affordanceOnSelection);
      document.addEventListener("scroll", hideAffordance, { passive: true });
      window.addEventListener("resize", _affordanceOnResize);
      state.selectionAffordanceReady = true;
  }
  // The region whose text belongs to the current document but has no exact
  // source mapping of its own: a concept's title/metadata chrome, or the whole
  // main landmark of a reserved index/log page. Selections here become an
  // explicit page comment that keeps the selected text as context.
  function feedbackPageRoot() {
    if (isConceptPage()) return document.querySelector("article.okf-page__main");
    if (document.getElementById("detail-body")) return null;
    return BOOT.document ? document.getElementById("okf-main") : null;
  }
  function getSelectionInBody() {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    const range = sel.getRangeAt(0);
    // Check the rendered body, the source pane, AND the graph detail panel
    // so comments work in all three surfaces.
    const body = $(".okf-page__body");
    const source = $(".okf-source");
    const graphDetail = document.getElementById("detail-body");
    const inRendered = body && body.contains(range.commonAncestorContainer);
    const inSource = source && source.contains(range.commonAncestorContainer);
    const inGraph = graphDetail && graphDetail.contains(range.commonAncestorContainer);
    const pageRoot = feedbackPageRoot();
    const inPage = !inRendered && !inSource && !inGraph && !!pageRoot
      && pageRoot.contains(range.commonAncestorContainer)
      && !(range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement)
        .closest(".okf-page-feedback, .okf-studiobar, [data-okf-live-utilities]");
    if (!inRendered && !inSource && !inGraph && !inPage) return null;
    const text = sel.toString().trim();
    if (!text) return null;
    return { sel, range, text, inSource, inGraph, inPage };
  }
  // iter1 CRI-002: find the nearest heading WITH AN ID (the renderer stamps
  // stable slug ids on every heading) so the comment anchor survives block
  // re-rendering. Falls back to the heading text if no id is present.
  function nearestHeadingAnchor(node) {
    const body = $(".okf-page__body");
    if (!body) return { id: "", text: "" };
    const headings = $$("h1, h2, h3, h4, h5, h6", body);
    const startNode = (node && node.nodeType === 1) ? node : (node && node.parentElement);
    if (!startNode) return { id: "", text: "" };
    let last = null;
    for (let i = 0; i < headings.length; i++) {
      if (startNode.compareDocumentPosition(headings[i]) & Node.DOCUMENT_POSITION_PRECEDING) {
        last = headings[i];
      } else break;
    }
    if (!last) return { id: "", text: "" };
    return { id: last.getAttribute("id") || "", text: (last.textContent || "").trim() };
  }
  function updateAffordance() {
    if (!affordance) return;
    const inBody = getSelectionInBody();
    if (!inBody) {
      // Tabbing from a keyboard-created selection into its nearby action can
      // collapse the native Selection. Keep the captured Range/action alive
      // until activation or Escape; otherwise the action vanishes under focus.
      if (affordance.contains(document.activeElement) && state.selectionDraft) return;
      releaseSelectionAction();
      return;
    }
    let rect;
    try { rect = inBody.range.getBoundingClientRect(); } catch (e) { releaseSelectionAction(); return; }
    if (!rect || (rect.width === 0 && rect.height === 0)) { releaseSelectionAction(); return; }
    const draft = selectionDraftFromRange(inBody);
    const origin = commentSelectionOrigin(inBody);
    transitionCommentLifecycle("selection-action", { selectionDraft: draft, origin: origin });
    affordance.classList.toggle("okf-comment-afford--keyboard", selectionInput === "keyboard");
    affordance.dataset.input = selectionInput;
    if (selectionInput === "keyboard" && origin && origin.parentNode) {
      origin.parentNode.insertBefore(affordance, origin.nextSibling);
    } else if (affordance.parentNode !== document.body) document.body.appendChild(affordance);
    document.body.dataset.okfSelectionReady = "true";
    affordance.hidden = false;
    const btn = affordance.firstChild;
    const bw = btn.offsetWidth || 180;
    const bh = btn.offsetHeight || 36;
    const margin = 8;
    const maxLeft = Math.max(margin, window.innerWidth - bw - margin);
    const left = Math.min(Math.max(margin, rect.left + rect.width / 2 - bw / 2), maxLeft);
    let top = rect.bottom + 6;
    if (top + bh > window.innerHeight - margin) top = rect.top - bh - 6;
    top = Math.min(Math.max(margin, top), Math.max(margin, window.innerHeight - bh - margin));
    affordance.style.left = left + "px";
    affordance.style.top = top + "px";
  }
  function hideAffordance(opts) {
    opts = opts || {};
    if (!opts.preserveLifecycle) {
      // Scroll and resize: retire the affordance without touching a lifecycle
      // state that the composer or a closed panel owns.
      releaseSelectionAction();
      return;
    }
    if (affordance) affordance.hidden = true;
    state.selectionDraft = null;
    delete document.body.dataset.okfSelectionReady;
  }
    // Q4: while the composer's post is held (pending, unconfirmed with the
    // sent draft, or accepted but not yet confirmed; see draftPostHeld) its
    // target is fixed: a new selection must not retarget the composer, turn
    // a same-key retry into a new comment, or leave an orphan mark.
    function composerPostPending() {
      return !!(state.composerPost && state.composerPost.outcome === "pending");
    }
    function refuseSelectionWhilePosting() {
      if (!draftPostHeld()) return false;
      const outcome = state.composerPost.outcome;
      toast(outcome === "pending" ? "Your comment is still being posted. Select the text again when it has finished."
        : outcome === "accepted-refresh-failed" ? "Your comment is still being confirmed. Use Retry refresh or Dismiss, then select the text again."
        : "Your comment could not be confirmed. Retry it, or edit or cancel the draft, then select the text again.", { tone: "info" });
      return true;
    }
    async function onAffordanceClick() {
      const inBody = getSelectionInBody() || resolveSelectionDraft();
      if (!inBody) return;
      if (refuseSelectionWhilePosting()) return;
    // Determine which concept the comment is about. On the graph page,
    // the detail panel shows a different concept than state.conceptId.
    // A reserved index/log page is targeted by its document, never a concept.
    var commentConcept = defaultCommentTarget();
    if (!inBody.inGraph && isConceptPage()) commentConcept = { concept: state.conceptId, document: conceptDocument(state.conceptId) };
    if (!hasCommentTarget(commentConcept)) {
      toast("There is no document to comment on here. Open a document, or select a concept in the graph.", { tone: "error", ttl: 7000 });
      return;
    }
    if (inBody.inPage) {
      // Title, metadata, generated listings and reserved index/log text have
      // no canonical source mapping: an explicit page comment keeps the text.
      if (state._pendingMarkId) removeCommentMark(state._pendingMarkId);
      state._pendingMarkId = null;
      state.draftAnchor = pageDraftAnchor(commentConcept, selectionContext("page", inBody.text,
        isConceptPage() ? "this part of the page is not mapped to the saved Markdown" : "index and log pages take page-level comments"));
      state.draftContentRev = null;
    } else if (inBody.inSource) {
      // Exact where the canonical mapping is provable; otherwise an explicit
      // page comment that keeps the selected Source text as context. Never a
      // guessed span, and never a silently discarded selection.
      let captured = null, unavailable = null;
      try { captured = await captureSourceAnchor(inBody); }
      catch (e) {
        if (e instanceof SourceMappingUnavailable) unavailable = e.reason;
        else if (e && e.name === "AbortError") return;
        else unavailable = "the saved file could not be read";
      }
      if (refuseSelectionWhilePosting()) return;
      if (captured) {
        if (state._pendingMarkId) removeCommentMark(state._pendingMarkId);
        state._pendingMarkId = null;
        state.draftAnchor = captured.anchor;
        state.draftContentRev = captured.rev;
      } else {
        if (state._pendingMarkId) removeCommentMark(state._pendingMarkId);
        state._pendingMarkId = null;
        state.draftAnchor = pageDraftAnchor(commentConcept, selectionContext("source", inBody.text, unavailable));
        state.draftContentRev = null;
      }
      } else if (!inBody.inGraph) {
        let canonicalAnchor = null;
        try { canonicalAnchor = await captureCanonicalAnchor(); }
        catch (e) {
          if (e && e.name === "AbortError") return;
          canonicalAnchor = null;
        }
        if (refuseSelectionWhilePosting()) return;
        if (canonicalAnchor) {
        const localId = "local-" + (state.nextCommentSeq++);
        const mark = wrapRangeInMark(inBody.range, localId, "open");
        // Public v2 anchors are closed objects: concept ownership stays in
        // the top-level comment request and must not be smuggled into anchor.
        state.draftAnchor = canonicalAnchor;
        state._pendingMarkId = localId;
        // R5-12: revision-qualify the pending draft so a later content-revision
        // change does not reapply the mark to new bytes (the block/position
        // evidence is keyed to the capture rev).
        state.draftContentRev = (state.doc && state.doc.concept_content_rev) || null;
        } else {
          // Cross-block, table/code/diagram or otherwise unmappable rendered
          // text: never a guessed span and never a discarded selection. The
          // comment attaches to the page and keeps the selected text.
          if (state._pendingMarkId) removeCommentMark(state._pendingMarkId);
          state._pendingMarkId = null;
          state.draftAnchor = pageDraftAnchor(commentConcept, selectionContext("rendered", inBody.text,
            "the selection does not map to one exact passage of the saved Markdown"));
          state.draftContentRev = null;
        }
      } else {
        // The graph detail renderer does not expose canonical source mapping;
        // keep graph comments concept-level rather than inventing a text span.
        if (state._pendingMarkId) removeCommentMark(state._pendingMarkId);
        state._pendingMarkId = null;
        state.draftAnchor = pageDraftAnchor(commentConcept, selectionContext("graph", inBody.text,
          "the graph preview has no canonical source mapping"));
        state.draftContentRev = null;
    }
      state.draftBody = state.draftBody || "";
      transitionCommentLifecycle("anchored-composer");
      openPanel("comments", { focusComposer: true });
      // Q4: a new selection changes an unconfirmed draft: Send (a new
      // comment), not the same-key Retry send.
      if (state.composerPost) _refreshComposerSubmitState();
      hideAffordance({ preserveLifecycle: true });
    try { window.getSelection().removeAllRanges(); } catch (e) {}
  }

  function selectionDraftFromRange(inBody) {
    const draft = {
      text: inBody.text,
      inSource: !!inBody.inSource,
      inGraph: !!inBody.inGraph,
      inPage: !!inBody.inPage,
      concept: commentConceptForSelection(inBody),
      block_id: "",
      block_text: "",
      range: null,
    };
    if (!inBody.inSource && !inBody.inGraph && !inBody.inPage) {
      const heading = nearestHeadingAnchor(inBody.range.startContainer);
      draft.block_id = heading.id;
      draft.block_text = heading.text;
    }
    try { draft.range = inBody.range.cloneRange(); } catch (e) { draft.range = null; }
    return draft;
  }

  function selectionDraftRoot(draft) {
    if (!draft) return null;
    if (draft.inSource) return $(".okf-source");
    if (draft.inGraph) return document.getElementById("detail-body");
    if (draft.inPage) return feedbackPageRoot();
    return $(".okf-page__body");
  }

  function resolveSelectionDraft() {
    const draft = state.selectionDraft;
    if (!draft || !draft.text) return null;
    const root = selectionDraftRoot(draft);
    if (!root) return null;
    if (draft.range && root.contains(draft.range.commonAncestorContainer)) {
      try {
        return {
          range: draft.range.cloneRange(),
          text: draft.text,
          inSource: draft.inSource,
          inGraph: draft.inGraph,
          inPage: draft.inPage,
        };
      } catch (e) { /* fall through to text re-resolution */ }
    }
    let found = null;
    if (!draft.inSource && !draft.inGraph && !draft.inPage && draft.block_id) {
      const block = root.querySelector('#' + cssEscape(draft.block_id));
      if (block) found = findTextNode(block, draft.text);
    }
    if (!found) found = findTextNode(root, draft.text);
    if (!found) return null;
    try {
      const range = document.createRange();
      range.setStart(found.node, found.start);
      range.setEnd(found.node, found.end);
      return { range, text: draft.text, inSource: draft.inSource, inGraph: draft.inGraph, inPage: draft.inPage };
    } catch (e) { return null; }
  }

    function commentConceptForSelection(inBody) {
    var commentConcept = state.conceptId;
    if (inBody && inBody.inGraph) {
      var graphBody = document.getElementById("detail-body");
      if (graphBody) commentConcept = graphBody.getAttribute("data-concept-id") || commentConcept;
    }
      return commentConcept;
    }

    // ---- Annotations v2 canonical source mapping -----------------------
    // DOM Range offsets are UTF-16 code units. Canonical offsets are Unicode
    // code points, so every boundary conversion is explicit.
    const ANNOTATION_MAX_EXACT = 16384;
    function cpLength(value) { return Array.from(value || "").length; }
    function cpSlice(value, start, end) { return Array.from(value || "").slice(start, end).join(""); }
    function utf16ToCodePoint(value, offset) {
      if (!Number.isInteger(offset) || offset < 0 || offset > value.length) return null;
      const prefix = value.slice(0, offset);
      // A boundary between the two halves of a surrogate pair is invalid.
      if (offset > 0 && offset < value.length && /[\uD800-\uDBFF]/.test(value[offset - 1]) && /[\uDC00-\uDFFF]/.test(value[offset])) return null;
      return Array.from(prefix).length;
    }
    function cpIndexOfAll(source, exact) {
      const hay = Array.from(source), needle = Array.from(exact), out = [];
      if (!needle.length) return out;
      for (let i = 0; i + needle.length <= hay.length; i++) {
        let same = true;
        for (let j = 0; j < needle.length; j++) if (hay[i + j] !== needle[j]) { same = false; break; }
        if (same) out.push(i);
      }
      return out;
    }
    async function sha256Text(value) {
      const bytes = new TextEncoder().encode(value.replace(/\r\n/g, "\n"));
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      return "sha256:" + Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
    }
    function annotationLines(source) {
      const result = []; let utf16 = 0; let cp = 0;
      const parts = source.match(/.*(?:\r\n|\n|\r)|.+$/g) || [];
      parts.forEach((text) => {
        const size = cpLength(text);
        result.push({ start: cp, end: cp + size, text });
        cp += size; utf16 += text.length;
      });
      return result;
    }
    function extractAnnotationBlocks(source) {
      const lines = annotationLines(source), raw = [], headings = [];
      const headingRe = /^( {0,3})(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$/;
      const fenceRe = /^ {0,3}(`{3,}|~{3,})/;
      const listRe = /^ {0,3}(?:[-+*]|\d+[.)])[ \t]+/;
      const quoteRe = /^ {0,3}>/;
      const tableRe = /^\s*\|.*\|\s*(?:\r?\n)?$/;
      const htmlRe = /^ {0,3}<\/?[A-Za-z][^>]*>/;
      let i = 0;
      while (i < lines.length) {
        const line = lines[i];
        if (!line.text.trim()) { i++; continue; }
        const hm = line.text.match(headingRe);
        if (hm) {
          const level = hm[2].length; headings.splice(level - 1, headings.length, hm[3]);
          raw.push({ kind: "heading", start: line.start, end: line.end, heading: headings.slice() }); i++; continue;
        }
        const fm = line.text.match(fenceRe);
        if (fm) {
          const ch = fm[1][0], width = fm[1].length; let j = i + 1;
          const close = new RegExp("^ {0,3}" + (ch === "`" ? "`" : "~") + "{" + width + ",}[ \\t]*(?:\\r?\\n)?$");
          while (j < lines.length) { if (close.test(lines[j++].text)) break; }
          raw.push({ kind: "fenced-code", start: line.start, end: lines[j - 1].end, heading: headings.slice() }); i = j; continue;
        }
        if (/^(    |\t)/.test(line.text)) {
          let j = i + 1; while (j < lines.length && (/^(    |\t)/.test(lines[j].text) || !lines[j].text.trim())) j++;
          raw.push({ kind: "indented-code", start: line.start, end: lines[j - 1].end, heading: headings.slice() }); i = j; continue;
        }
        const kind = listRe.test(line.text) ? "list-item" : quoteRe.test(line.text) ? "block-quote" : tableRe.test(line.text) ? "table-row" : htmlRe.test(line.text) ? "html" : "paragraph";
        let j = i + 1;
        if (kind === "paragraph" || kind === "block-quote" || kind === "html") {
          while (j < lines.length && lines[j].text.trim()) {
            const t = lines[j].text;
            if (headingRe.test(t) || fenceRe.test(t) || listRe.test(t) || (kind === "paragraph" && (quoteRe.test(t) || htmlRe.test(t))) || (tableRe.test(t) && kind !== "html")) break;
            if (kind === "block-quote" && !quoteRe.test(t)) break;
            if (kind === "html" && !htmlRe.test(t)) break;
            j++;
          }
        }
        // List-item continuation is intentionally conservative: accepting a
        // wrong block would be worse than asking the user to narrow selection.
        raw.push({ kind, start: line.start, end: lines[j - 1].end, heading: headings.slice() }); i = j;
      }
      return raw.map((b, index) => Object.assign(b, { id: b.kind + ":" + (index + 1), text: cpSlice(source, b.start, b.end) }));
    }
    function renderedBlockForRange(range) {
      const body = $(".okf-page__body");
      if (!body || !body.contains(range.startContainer) || !body.contains(range.endContainer)) return null;
      const start = canonicalBlockFor(range.startContainer, body);
      const end = canonicalBlockFor(range.endContainer, body);
      return start && start === end && start !== body ? start : null;
    }
    // A selection's boundary often rests on the far edge of a neighbouring text
    // node: a drag that began just before a cell, or a triple-click whose end
    // is offset 0 of the next block. Those edge nodes contribute no selected
    // character, yet they decided which block "owned" the selection. Trim the
    // range to the text it actually covers; the selected text is unchanged.
    function rangeOverSelectedText(range) {
      const nodes = textNodesInRange(range).filter(function (node) {
        const length = (node.nodeValue || "").length;
        const from = node === range.startContainer ? range.startOffset : 0;
        const to = node === range.endContainer ? range.endOffset : length;
        return to > from;
      });
      if (!nodes.length) return range;
      const first = nodes[0], last = nodes[nodes.length - 1];
      const trimmed = document.createRange();
      trimmed.setStart(first, first === range.startContainer ? range.startOffset : 0);
      trimmed.setEnd(last, last === range.endContainer ? range.endOffset : (last.nodeValue || "").length);
      return trimmed;
    }
    // The canonical source map owns a table per ROW: each mapped <tr> carries
    // its own projection key and the <table> carries none. Resolving a cell
    // selection to the table therefore never found a mapping, and every table
    // selection fell back to a page comment. A selection inside one mapped row
    // resolves to that row; two rows are two blocks and stay a page comment.
    function canonicalBlockFor(node, body) {
      const block = containingBlock(node, body);
      if (!block || block.tagName !== "TABLE") return canonicalListOwnerForBlock(block, body);
      const element = node.nodeType === 1 ? node : node.parentElement;
      const row = element && element.closest("tr[data-okf-projection-key]");
      return row && block.contains(row) ? row : block;
    }
    // R5: a nested list item renders WITHOUT its own projection key — the
    // renderer attaches one owner nonce per top-level list item, so the
    // enclosing keyed <li> owns the whole verified list-item record, nested
    // children included. Choosing the inner unkeyed li forced every
    // nested-child selection into an explained page comment. Resolve an
    // unkeyed <li> to its nearest keyed ancestor <li>; every later proof
    // still runs unchanged against that owner: both selection boundaries
    // must resolve to the same block (renderedBlockForRange), the record
    // must be verified, and sourceSpanForDomRange must produce a contiguous
    // canonical span — so cross-owner, structural-gap, and ambiguous ranges
    // keep the honest page fallback. Ownership gains nothing else: no new
    // keys, no decoder change, and no broadening beyond nested list
    // structure (unkeyed blocks outside a keyed list item stay unowned).
    function canonicalListOwnerForBlock(block, body) {
      if (!block || block.tagName !== "LI" || block.hasAttribute("data-okf-projection-key")) return block;
      const owner = block.closest("[data-okf-projection-key]");
      return owner && owner.tagName === "LI" && body.contains(owner) ? owner : block;
    }
    // Table enhancement (sorting, filtering, resizing) decorates live cells. A
    // row is mapped only while its live text is still exactly the text the
    // projection was verified against; otherwise offsets could drift silently.
    function liveBlockMatchesProjection(doc, element) {
      if (!element || element.tagName !== "TR") return true;
      const template = document.createElement("template");
      template.innerHTML = doc.html || "";
      const key = element.getAttribute("data-okf-projection-key");
      const verified = Array.from(template.content.querySelectorAll("tr[data-okf-projection-key]"))
        .find(function (node) { return node.getAttribute("data-okf-projection-key") === key; });
      return !!verified && (verified.textContent || "") === (element.textContent || "");
    }
      async function ensureCanonicalDoc() {
        if (state.doc && typeof state.doc.annotation_source === "string" && state.doc.concept_content_rev) return state.doc;
        const owner = state.annotationGeneration;
        const publication = state.documentPublication;
        const res = await annotationFetch("/__data/doc?id=" + encodeURIComponent(state.conceptId), { headers: { Accept: "application/json" } }, "get", owner);
        if (!res.ok) throw new Error("Canonical document unavailable.");
        const doc = await res.json();
        if (owner !== state.annotationGeneration || doc.id !== state.conceptId) throw new DOMException("Superseded", "AbortError");
          // Req6: surface the offending block/index/reason from projectionError
          // (structural only — no response bodies, source text, or secrets)
          // instead of a generic "invalid" message, so the user can see WHICH
          // projection block failed verification.
          if (doc.id !== state.conceptId) throw new Error("Canonical document response was invalid: concept mismatch.");
          if (typeof doc.annotation_source !== "string") throw new Error("Canonical document response was invalid: missing annotation source.");
          if (!/^[0-9a-f]{64}$/.test(doc.concept_content_rev || "")) throw new Error("Canonical document response was invalid: content revision missing.");
          const projErr = projectionError(doc.annotation_projection, doc.concept_content_rev, doc.annotation_source, doc.html);
          if (projErr) throw new Error("Canonical document response was invalid: " + projErr + ".");
        // Q4: a document of another revision was published while this read
        // was in flight (the first publication does not move the owner
        // generation). Capture works on the committed document instead of
        // rolling the page back to the older bytes.
        if (supersededByPublication(publication, doc.concept_content_rev)) {
          const current = state.doc;
          if (typeof current.annotation_source === "string") return current;
          throw new DOMException("Superseded", "AbortError");
        }
        commitDocument(doc, { pulse: false }); return doc;
      }
        // Req6: projectionError returns null when the canonical projection is
        // valid, or a concise human-readable reason identifying the offending
        // block/index/cause. Reasons are STRUCTURAL only — they never echo
        // source text, html bodies, response payloads, or secrets — so a
        // surfaced projection failure is safe to show in a status surface.
        // validProjection (below) delegates to projectionError === null so the
        // strict boolean contract every canonical commit path depends on is
        // unchanged. The keyed-zero-segment rule stays strict: a keyed record
        // with no segments is valid ONLY when its block_id starts with "hr:".
        //
        // X4: one comment post asks this question six or more times about the
        // same document (send gate, admission, staged tuple, every exact mark,
        // selection). The verdict has two halves:
        //   - metadata and revision parity: cheap, and the revision differs
        //     per caller (a mark passes its comment's resolution revision), so
        //     it is always checked first and never cached;
        //   - structure and HTML parity: a pure function of the projection
        //     object, the source string and the html string. It is remembered
        //     per projection object and reused only while source and html are
        //     strictly equal; a rejection is remembered as that rejection, so
        //     admission still fails closed.
        // Reuse is sound only while the projection cannot change, so a
        // projection is deep-frozen before its first remembered verdict
        // (studio.js is strict mode: a later write throws instead of silently
        // diverging from the remembered verdict). A projection that is not
        // plain JSON data is judged afresh on every call and left unfrozen.
        const _projectionVerdicts = new WeakMap();
        let _projectionValidations = 0;
        // Test seam: counts the uncached structure/HTML validations only.
        function projectionValidationCountForTest() { return _projectionValidations; }
        function freezeJsonProjection(projection) {
          const pending = [projection], seen = new Set();
          while (pending.length) {
            const value = pending.pop();
            if (seen.has(value)) continue;
            const proto = Object.getPrototypeOf(value);
            if (Array.isArray(value) ? proto !== Array.prototype : proto !== Object.prototype && proto !== null) return false;
            seen.add(value);
            const names = Object.getOwnPropertyNames(value);
            for (let i = 0; i < names.length; i++) {
              const property = Object.getOwnPropertyDescriptor(value, names[i]);
              if (!("value" in property) || typeof property.value === "function") return false;
              if (property.value !== null && typeof property.value === "object") pending.push(property.value);
            }
          }
          seen.forEach(function (value) { Object.freeze(value); });
          return true;
        }
        function projectionError(projection, revision, source, html) {
          if (!projection || projection.schema !== 1 || projection.resolver_version !== 2 || projection.offset_unit !== "unicode-code-point" || projection.concept_content_rev !== revision || projection.complete !== true || !Array.isArray(projection.blocks)) return "projection metadata invalid";
          const remembered = _projectionVerdicts.get(projection);
          if (remembered && remembered.source === source && remembered.html === html) return remembered.verdict;
          const cacheable = !!remembered || freezeJsonProjection(projection);
          _projectionValidations++;
          const verdict = projectionContentError(projection, source, html);
          if (cacheable) _projectionVerdicts.set(projection, { source: source, html: html, verdict: verdict });
          return verdict;
        }
        function projectionContentError(projection, source, html) {
          // Split the source once; a per-segment split made this quadratic.
          const sourceCps = typeof source === "string" ? Array.from(source) : null;
          const sourceLength = sourceCps ? sourceCps.length : null, keys = new Set(), recordByKey = new Map();
          function bid(record) { return (record && typeof record.block_id === "string") ? record.block_id : "?"; }
          for (let bi = 0; bi < projection.blocks.length; bi++) {
            const record = projection.blocks[bi];
            const where = "block " + bi + " (" + bid(record) + ")";
            if (!record || !Number.isInteger(record.source_start) || !Number.isInteger(record.source_end) || record.source_start < 0 || record.source_start >= record.source_end || (sourceLength != null && record.source_end > sourceLength) || !Array.isArray(record.segments)) return where + ": source offsets out of range";
            if (record.dom_key == null) { if (record.segments.length) return where + ": unkeyed record must have no segments"; continue; }
            const zeroVisibleBreak = typeof record.block_id === "string" && record.block_id.indexOf("hr:") === 0 && record.segments.length === 0;
            if (!Number.isSafeInteger(record.dom_key) || record.dom_key < 0) return where + ": invalid projection key";
            if (keys.has(record.dom_key)) return where + ": duplicate projection key " + record.dom_key;
            if (!record.segments.length && !zeroVisibleBreak) return where + ": keyed record has no segments";
            keys.add(record.dom_key); recordByKey.set(record.dom_key, record); let lastDom = 0, lastSource = record.source_start;
              // Allow ordered unmapped DOM gaps (dom_start >= lastDom) while
              // rejecting overlap/reversal (dom_start < lastDom). The Python
              // projection contract (_concat_quote_units) legitimately leaves
              // 1-code-point \n separators between block-quote sub-units
              // unmapped; these are structural DOM gaps, not source-coverable
              // text. Source offsets MUST stay monotonically non-decreasing
              // and within the record's [source_start, source_end] bounds.
              for (const segment of record.segments) {
                if (!segment || !Number.isInteger(segment.dom_start) || !Number.isInteger(segment.dom_end) || !Number.isInteger(segment.source_start) || !Number.isInteger(segment.source_end) || segment.dom_start < lastDom || segment.dom_start >= segment.dom_end || segment.source_start < lastSource || segment.source_start >= segment.source_end || segment.source_start < record.source_start || segment.source_end > record.source_end) return where + ": segment offsets invalid";
                lastDom = segment.dom_end; lastSource = segment.source_end;
              }
          }
          if (typeof html === "string") {
            const template = document.createElement("template"); template.innerHTML = html;
            const nodes = Array.from(template.content.querySelectorAll("[data-okf-projection-key]"));
            if (nodes.length !== keys.size) return "dom node count mismatch (keys=" + keys.size + ", nodes=" + nodes.length + ")";
            // Keys are unique on both sides by the time a lookup runs (the
            // duplicate checks come first), so a map finds what .find found.
            const seen = new Set(), nodeByKey = new Map();
            var domGroups = new Map();
            for (var ni = 0; ni < nodes.length; ni++) {
              var node = nodes[ni];
              var raw = node.getAttribute("data-okf-projection-key");
              if (!/^(0|[1-9][0-9]*)$/.test(raw || "")) return "projection key attribute invalid";
              var key = Number(raw);
              if (!Number.isSafeInteger(key) || !keys.has(key)) return "projection key " + raw + " has no mapped block";
              if (seen.has(key)) return "duplicate projection key " + key;
              seen.add(key); nodeByKey.set(key, node);
              var record = recordByKey.get(key);
              var visibleText = node.textContent || "";
              if (!record.segments.length) {
                if (node.tagName !== "HR" || cpLength(visibleText) !== 0) return "block (" + bid(record) + "): hr block has visible text";
                continue;
              }
                var expectedLength = record.segments[record.segments.length - 1].dom_end;
                // Reject out-of-bounds: the highest mapped DOM offset must not
                // exceed the visible text length. A trailing unmapped region
                // (if any) is allowed; position-anchored verification below
                // independently bounds-checks every segment.
                if (cpLength(visibleText) < expectedLength) return "block (" + bid(record) + "): segment exceeds visible text";
                if (typeof source === "string") {
                  // Decode each segment and position-anchored cross-validate
                  // its source->DOM mapping. The Python projection contract
                  // (_concat_quote_units) legitimately leaves 1-code-point \n
                  // separators between block-quote sub-units UNMAPPED; these
                  // structural DOM gaps make the old "concatenated decoded
                  // segments equal all visible text" assumption false. Instead
                  // we verify per-segment that the decoded source text equals
                  // the visible text at [dom_start, dom_end) code points, which
                  // independently catches wrong offsets, entity mismatches, and
                  // equal-length substitutions -- including across gaps.
                  var segDecoded = [], segDecodedCp = [];
                  var canonicalVisible = "";
                  var visCp = Array.from(visibleText);
                  var visLen = visCp.length;
                  for (var si = 0; si < record.segments.length; si++) {
                    var seg = record.segments[si];
                    var decoded = _decodeCanonicalSegment(sourceCps, seg, record);
                    if (decoded === null) return "block (" + bid(record) + "): segment decode failed";
                    segDecoded.push(decoded);
                    if (seg.dom_end > visLen) return "block (" + bid(record) + "): segment exceeds visible text";
                    var decodedCp = Array.from(decoded);
                    segDecodedCp.push(decodedCp);
                    for (var ci = 0; ci < decodedCp.length; ci++) {
                      if (visCp[seg.dom_start + ci] !== decodedCp[ci]) return "block (" + bid(record) + "): segment text mismatch";
                    }
                    canonicalVisible += decoded;
                  }
                  // Per-record occurrence count + ordinal parity. canonicalVisible
                  // is the decoded-segment sequence WITHOUT unmapped gaps;
                  // visibleText includes them. For each decoded signature,
                  // occurrence counts must be equal and the segment's ordinal
                  // must match -- this catches swapped duplicate-text segments
                  // that position-anchored verification and source monotonicity
                  // alone cannot fully exclude.
                  // canonOffset is a CODE POINT offset, like dom_start and the
                  // positions _overlappingPositions returns. Advancing it by
                  // UTF-16 length drifted one unit per astral character, so any
                  // segment after an emoji in the same block failed parity and
                  // the whole page lost exact anchoring.
                  // Split once per record, not once per segment. The joined
                  // string is split (never the per-segment arrays concatenated)
                  // because a split can pair surrogates across segments.
                  var canonCp = Array.from(canonicalVisible);
                  var canonOffset = 0;
                  for (var si2 = 0; si2 < record.segments.length; si2++) {
                    var sig = segDecoded[si2];
                    if (sig.length < 1) { continue; }
                    var sigCp = segDecodedCp[si2];
                    var canonPos = _overlappingPositions(canonCp, sigCp);
                    var visPos = _overlappingPositions(visCp, sigCp);
                    if (canonPos.length !== visPos.length) return "block (" + bid(record) + "): segment occurrence count mismatch";
                    var canonOrdinal = canonPos.indexOf(canonOffset);
                    var visOrdinal = visPos.indexOf(record.segments[si2].dom_start);
                    if (canonOrdinal < 0 || visOrdinal < 0 || canonOrdinal !== visOrdinal) return "block (" + bid(record) + "): segment occurrence parity mismatch";
                    canonOffset += sigCp.length;
                  }
              }
              if (!domGroups.has(visibleText)) domGroups.set(visibleText, []);
              domGroups.get(visibleText).push(key);
            }
            // Cross-record ordered occurrence parity.
            if (typeof source === "string" && domGroups.size > 0) {
              var mapped = projection.blocks.filter(function(r) { return r.dom_key != null && r.segments.length > 0; });
              mapped.sort(function(a, b) { return a.source_start - b.source_start; });
              for (var mi = 1; mi < mapped.length; mi++) {
                if (mapped[mi].dom_key <= mapped[mi - 1].dom_key) return "cross-record projection key order mismatch";
              }
              var sigGroups = new Map();
              mapped.forEach(function(rec) {
                var n = nodeByKey.get(rec.dom_key);
                if (!n) return;
                var s = n.textContent || "";
                if (!sigGroups.has(s)) sigGroups.set(s, []);
                sigGroups.get(s).push(rec.dom_key);
              });
              var allOk = true;
              sigGroups.forEach(function(srcKeys, sig) {
                var domKeys = domGroups.get(sig);
                if (!domKeys || domKeys.length !== srcKeys.length) { allOk = false; return; }
                for (var di = 0; di < srcKeys.length; di++) {
                  if (srcKeys[di] !== domKeys[di]) { allOk = false; return; }
                }
              });
              if (!allOk) return "cross-record occurrence parity mismatch";
              domGroups.forEach(function(domKeys, visText) {
                var srcKeys = sigGroups.get(visText);
                if (!srcKeys || srcKeys.length !== domKeys.length) { allOk = false; return; }
              });
              if (!allOk) return "cross-record occurrence parity mismatch";
            }
          }
          return null;
      }
        // Strict boolean projection contract preserved for every canonical
        // commit path. validProjection is validProjection(projectionError===null).
        function validProjection(projection, revision, source, html) {
          return projectionError(projection, revision, source, html) === null;
        }
        // Document ADMISSION and EXACT ANCHORING are separate questions.
        //   Admission: does this projection belong to this document revision?
        //     (schema, resolver, offset unit, revision parity, a block list.)
        //     That is all page-level feedback depends on.
        //   Exact anchoring: is the source map verified end to end? Only the
        //     strict validProjection answers that, and capture, marks and
        //     re-anchor each keep calling it and fail closed.
        // The server honestly reports `complete:false` when an eligible block
        // (commonly a table) has no mapping, and a mapped block can still fail
        // decode parity. Neither may take a whole page's comments down: the
        // document loads, page comments work, and a selection becomes an
        // explicit page comment with context. A projection for another revision
        // or an unknown contract still rejects the tuple.
        function projectionAdmissionError(projection, revision, source, html) {
          var strict = projectionError(projection, revision, source, html);
          if (strict === null) return null;
          var coherent = !!projection && projection.schema === 1 && projection.resolver_version === 2 &&
            projection.offset_unit === "unicode-code-point" && projection.concept_content_rev === revision &&
            typeof projection.complete === "boolean" && Array.isArray(projection.blocks);
          return coherent ? null : strict;
        }
        function projectionAdmissible(projection, revision, source, html) {
          return projectionAdmissionError(projection, revision, source, html) === null;
        }
        // null = not a concept page / no document yet; false = page-level only.
        var _exactAnchoringByDoc = new WeakMap();
        function exactAnchoringAvailable() {
          var doc = state.doc;
          if (!isConceptPage() || !doc || !doc.annotation_projection) return null;
          if (!_exactAnchoringByDoc.has(doc)) {
            var fault = projectionError(doc.annotation_projection, doc.concept_content_rev, doc.annotation_source, doc.html);
            // Structural reason only (block id / cause) — never source text.
            if (fault) console.warn("[okf-studio] exact anchoring unavailable on this page:", fault);
            _exactAnchoringByDoc.set(doc, fault === null);
          }
          return _exactAnchoringByDoc.get(doc);
        }
      // R10: canonical staged-tuple validator shared by EVERY canonical commit
      // path (loadComments, saveReanchor post-save reconciliation,
      // reconcileReanchor, refreshAfterConflict). Validates the COMPLETE
      // doc+comments(+events) envelope BEFORE any mutation:
      //   - doc identity: when a doc is staged, its id MUST equal the active
      //     concept (a projection-valid document for ANOTHER concept must not
      //     be committed) and its projection MUST be valid;
      //   - containers: comments MUST be a real array and events (when present)
      //     MUST be a real array — a non-array value such as {} previously
      //     bypassed row validation entirely;
      //   - every row MUST carry stable identity (nonempty string id + concept),
      //     a row_token, and a valid anchor object (with a string kind);
      //   - rows anchored to THIS concept MUST carry anchor_resolution parity
      //     against the staged projectionRev, and a non-concept (text) anchor
      //     MUST carry resolution evidence.
      // Returns { comments, events, projectionRev } on success or
      // { skewSignal } on failure. This closes the r10 finding where a
      // non-array comments/events value, a token-bearing row without an
      // anchor, or a foreign-concept document could coexist with a freshly-
      // committed document.
        // R11: discriminated anchor validation. Only the kinds the frontend
        // renders are accepted; any other kind is rejected (fail closed). A
        // text-quote anchor MUST carry a nonempty `exact` quote (the
        // discriminator applyCommentMarks uses). A legacy text anchor MUST
        // carry a nonempty `ref`.
        //
        // Page-level rows: the server's canonical page anchor is EXACTLY
        // {kind:"concept"} — concept ownership lives in the row's top-level
        // `concept`, never inside the anchor. Rows written without an anchor
        // (agent `comment-reply`, anchorless API posts) persist `{}`. Older
        // bundles may hold {kind:"concept", ref|concept: <row concept>} and
        // {kind:"source", ref: <selected source text>} rows. All of these are
        // read as page-level comments; none is ever projected as a highlight.
        // A concept anchor naming a DIFFERENT concept still fails closed.
        var _SUPPORTED_ANCHOR_KINDS = { "concept": true, "page": true, "source": true, "text-quote": true, "text": true };
        // Stable document identity. A row is owned by a real concept id, by a
        // canonical bundle-relative Markdown `document` path (root/directory
        // indexes and logs carry `concept: ""`), or by both when they agree.
        // A reserved page is never given a fabricated concept.
        var _DOCUMENT_PATH = /^(?:[A-Za-z0-9_][A-Za-z0-9._-]*\/)*[A-Za-z0-9_][A-Za-z0-9._-]*\.md$/;
        function validDocumentPath(value) {
          // Mirrors documents.document_path / paths._SEGMENT_RE exactly: a segment
          // starts with [A-Za-z0-9_], so "." / ".." / hidden parts cannot occur.
          return typeof value === "string" && value.length <= 1024 && _DOCUMENT_PATH.test(value);
        }
        function conceptDocument(concept) { return concept ? concept + ".md" : ""; }
        function rowDocument(row) {
          if (!row) return "";
          if (typeof row.document === "string" && row.document) return row.document;
          return typeof row.concept === "string" ? conceptDocument(row.concept) : "";
        }
        function validRowIdentity(row) {
          if (!row || typeof row.concept !== "string") return false;
          if (row.document === undefined || row.document === null) return !!row.concept;
          if (!validDocumentPath(row.document)) return false;
          return !row.concept || row.document === conceptDocument(row.concept);
        }
        function rowMatchesTarget(row, target) {
          if (!row || !target) return false;
          if (target.concept) return row.concept === target.concept;
          return !!target.document && !row.concept && row.document === target.document;
        }
        function _validAnchor(anchor, rowConcept) {
          if (!anchor || typeof anchor !== "object" || Array.isArray(anchor)) return false;
          var kind = anchor.kind;
          if (kind === undefined) return Object.keys(anchor).length === 0;
          if (typeof kind !== "string" || !kind || !_SUPPORTED_ANCHOR_KINDS[kind]) return false;
          // Closed document-wide anchor: exactly {kind:"page"}. Ownership is the
          // row's top-level document/concept, never smuggled into the anchor.
          if (kind === "page") return Object.keys(anchor).length === 1;
          if (kind === "concept") {
            if (anchor.ref !== undefined && anchor.ref !== rowConcept) return false;
            if (anchor.concept !== undefined && anchor.concept !== rowConcept) return false;
            return true;
          }
          if (kind === "source") {
            return typeof anchor.ref === "string" && !!anchor.ref
              && (anchor.concept === undefined || anchor.concept === rowConcept);
          }
          if (kind === "text-quote") {
            return typeof anchor.exact === "string" && !!anchor.exact;
          }
          // legacy "text"
          return typeof anchor.ref === "string" && !!anchor.ref;
        }
        // Page-level = no text span: canonical {kind:"concept"}, anchorless {},
        // or a legacy concept/source row. Never highlighted, never re-anchored.
        function isPageLevelAnchor(anchor) {
          if (!anchor || typeof anchor !== "object") return false;
          return anchor.kind === undefined || anchor.kind === "concept" || anchor.kind === "page" || anchor.kind === "source";
        }
        // The composer keeps a LOCAL page draft {kind:"concept", ref, concept,
        // context?}: `concept` is the comment's target (the graph detail concept
        // differs from the route) and `context` is selected text that could not
        // become an exact anchor. Neither may be smuggled into the closed wire
        // anchor, so every request is built through wireAnchor/wireDetail.
        // A reserved document (root/directory index, log) has no concept: its
        // LOCAL page draft is {kind:"page", document, context?} and its wire
        // request names top-level `document` with the closed {kind:"page"}.
        function pageDraftAnchor(target, context) {
          var concept = typeof target === "string" ? target : (target && target.concept) || "";
          var doc = typeof target === "string" ? "" : (target && target.document) || "";
          var draft = concept || !doc
            ? { kind: "concept", ref: concept, concept: concept }
            : { kind: "page", document: doc };
          if (context) draft.context = context;
          return draft;
        }
        // The document a plain composer targets: the concept page itself; the
        // served index/log document named by the bootstrap; or, on the graph,
        // the concept (or reserved document) shown in the detail preview — never
        // the `__graph` route. Empty = no supported target on this surface (the
        // composer then explains why and refuses to send).
        var PAGE_DOCUMENT = validDocumentPath(BOOT.document) ? BOOT.document : "";
        // The graph route: its comment target is whatever the detail pane shows.
        function isGraphCommentSurface() { return !isConceptPage() && !!document.getElementById("detail-body"); }
        // What the graph detail pane actually shows (the same reading as the
        // export Review link): a visible reserved index/log reader names its
        // document; otherwise the visible concept preview names its concept;
        // otherwise nothing. Clearing the selection hides #detail-content but
        // leaves its last data-concept-id behind, which is not a target.
        function shownGraphTarget() {
          var reserved = document.getElementById("detail-document");
          var reservedBody = document.getElementById("detail-document-body");
          if (reserved && !reserved.hidden && reservedBody) {
            var doc = reservedBody.getAttribute("data-okf-document") || "";
            return { concept: "", document: validDocumentPath(doc) ? doc : "" };
          }
          var content = document.getElementById("detail-content");
          var body = document.getElementById("detail-body");
          var shown = body && content && !content.hidden ? body.getAttribute("data-concept-id") || "" : "";
          return shown ? { concept: shown, document: conceptDocument(shown) } : { concept: "", document: "" };
        }
        function defaultCommentTarget() {
          if (isConceptPage()) return { concept: state.conceptId || "", document: conceptDocument(state.conceptId || "") };
          if (isGraphCommentSurface()) return shownGraphTarget();
          return { concept: "", document: PAGE_DOCUMENT };
        }
        function defaultCommentConcept() { return defaultCommentTarget().concept; }
        function hasCommentTarget(target) { return !!(target && (target.concept || target.document)); }
        function defaultDraftAnchor() { return pageDraftAnchor(defaultCommentTarget()); }
        function draftTarget(draft) {
          if (draft && typeof draft.concept === "string" && draft.concept) return { concept: draft.concept, document: conceptDocument(draft.concept) };
          if (draft && draft.kind === "page" && validDocumentPath(draft.document)) return { concept: "", document: draft.document };
          return defaultCommentTarget();
        }
        function draftTargetConcept(draft) { return draftTarget(draft).concept; }
        // Exactly one owner field travels on the wire (the server rejects both).
        function wireTarget(target) {
          return target.concept ? { concept: target.concept } : { document: target.document };
        }
        function targetKey(target) { return target ? (target.concept ? "c:" + target.concept : "d:" + (target.document || "")) : ""; }
        function targetLabel(target) {
          if (!hasCommentTarget(target)) return "";
          return target.concept ? target.concept : target.document;
        }
        // ---- Graph draft target ownership --------------------------------
        // The graph preview changes concept without a navigation. On the graph
        // (only: a concept or reserved page route owns its target) a draft is
        // PINNED when it has a saved target and something worth keeping there:
        // typed text, selected-text context, or a post that is still pending,
        // uncertain, or accepted but not yet confirmed. A pinned draft keeps
        // its target when the selection changes; only deliberate actions
        // change it: Move, Cancel, a successful post, or explicitly
        // commenting on a selection in another concept (onAffordanceClick
        // retargets the draft there, keeping its text). Empty drafts, and
        // drafts with no target yet, follow the selection.
        function storedDraftTarget(a) {
          if (a && a.kind !== "text-quote" && typeof a.concept === "string" && a.concept) return { concept: a.concept, document: conceptDocument(a.concept) };
          if (a && a.kind === "page" && validDocumentPath(a.document)) return { concept: "", document: a.document };
          return null;
        }
        // An unconfirmed post holds the draft only while the draft is still
        // the one that was sent (its same-key retry); a changed draft is a new
        // comment and is free to move or take a new selection.
        function draftPostHeld() {
          var cp = state.composerPost;
          if (!cp) return false;
          if (cp.outcome === "pending" || cp.outcome === "accepted-refresh-failed") return true;
          return cp.outcome === "uncertain" && composerDraftMatchesPost(cp);
        }
        // The draft the composer would send now: text, anchor and target, as
        // postCommentFromComposer derives them.
        function currentComposerDraft() {
          var ta = document.querySelector(".okf-composer__textarea");
          var body = ((ta ? ta.value : state.draftBody) || "").trim();
          var anchor = state.draftAnchor || defaultDraftAnchor();
          var target = anchor.kind === "text-quote"
            ? { concept: state.conceptId, document: conceptDocument(state.conceptId) }
            : draftTarget(anchor);
          return { body: body, anchor: anchor, target: target };
        }
        // Same-draft identity of a recorded post: body, anchor (deep-equal),
        // target and reply parent.
        function sameComposerDraft(cp, body, anchor, target, parent) {
          return !!(cp && cp.body === body
            && deepEqual(cp.anchor, anchor)
            && cp.concept === target.concept
            && (cp.document || "") === (target.document || "")
            && (cp.parent || null) === (parent || null));
        }
        // An unconfirmed retry reuses the recorded parent, so only text,
        // anchor and target can differ from what was sent.
        function composerDraftMatchesPost(cp) {
          var draft = currentComposerDraft();
          return sameComposerDraft(cp, draft.body, draft.anchor, draft.target, cp.parent);
        }
        function draftIsPinned(opts) {
          if (!isGraphCommentSurface()) return false;
          var a = state.draftAnchor;
          if (!storedDraftTarget(a)) return false;
          if ((state.draftBody || "").trim()) return true;
          if (!(opts && opts.ignoreContext) && a.context) return true;
          return draftPostHeld();
        }
        // The anchor a composer transition keeps: a pinned draft keeps its
        // target (optionally without its selected-text context, which Escape
        // removes); anything else takes the current default target.
        function retainedDraftAnchor(opts) {
          var drop = !!(opts && opts.dropContext);
          if (!draftIsPinned({ ignoreContext: drop })) return defaultDraftAnchor();
          var a = state.draftAnchor;
          return pageDraftAnchor(storedDraftTarget(a), drop ? null : a.context);
        }
        // A post rejected with 404 target_not_found names a target that no
        // longer exists. Send stays disabled while the draft is still saved to
        // it; Move, Cancel, or that target being shown again clears it.
        function draftTargetIsMissing() {
          if (!state.draftTargetMissing) return false;
          var target = storedDraftTarget(state.draftAnchor);
          return !!target && targetKey(target) === state.draftTargetMissing;
        }
        function missingTargetMessage(target) {
          return "The comment was not posted because " + targetLabel(target) + " no longer exists. Your text is still in the composer.";
        }
        function showDraftTargetMissing(target) {
          // Only the graph can show the target again or offer Move, so only
          // there does the missing target hold Send off; a route that lost its
          // own document is replaced by the live page anyway.
          state.draftTargetMissing = isGraphCommentSurface() ? targetKey(target) : "";
          state.composerStatus = { message: missingTargetMessage(target), tone: "error", targetMissing: true };
          _renderComposerStatus();
        }
        function clearDraftTargetMissing() {
          if (!state.draftTargetMissing) return;
          state.draftTargetMissing = "";
          if (state.composerStatus && state.composerStatus.targetMissing) state.composerStatus = null;
          _renderComposerStatus();
        }
        function syncDefaultDraftTarget() {
          if (isConceptPage()) return;
          var a = state.draftAnchor;
          if (!isGraphCommentSurface()) {
            // A reserved page's target never changes; keep selected context
            // and an attempt in flight exactly as before.
            if (a && (!isPageLevelAnchor(a) || a.context)) return;
            if (draftPostHeld()) return;
            state.draftAnchor = defaultDraftAnchor();
          } else {
            if (a && !isPageLevelAnchor(a)) return;
            if (!draftIsPinned()) state.draftAnchor = defaultDraftAnchor();
            // The missing target is on screen again (it can take the post), or
            // the draft is no longer saved to it.
            if (state.draftTargetMissing && (!draftTargetIsMissing() || targetKey(shownGraphTarget()) === state.draftTargetMissing)) clearDraftTargetMissing();
          }
          var wrap = document.querySelector(".okf-composer");
          if (wrap && wrap._refreshAnchor) wrap._refreshAnchor();
        }
        // One announcement per distinct selection that differs from a pinned
        // unsent draft's target. Clearing the selection announces nothing;
        // showing the draft's own target again resets the record. A post that
        // is still being confirmed is explained by the composer note instead,
        // so it never displaces "Posting comment…" in the shared status.
        var _ownershipAnnounced = {};
        function resetDraftOwnershipAnnouncements() { _ownershipAnnounced = {}; }
        function announceDraftOwnership() {
          if (!draftIsPinned() || draftPostHeld()) { resetDraftOwnershipAnnouncements(); return; }
          var pinned = storedDraftTarget(state.draftAnchor);
          var shown = shownGraphTarget();
          if (!hasCommentTarget(shown)) return;
          var key = targetKey(shown);
          if (key === targetKey(pinned)) { resetDraftOwnershipAnnouncements(); return; }
          if (_ownershipAnnounced[key]) return;
          _ownershipAnnounced[key] = true;
          announceAnnotation("Your unsent comment stays on " + targetLabel(pinned) + ". It was not moved to " + targetLabel(shown) + ".");
        }
        // What the composer's "Draft target" group says: null when the draft
        // is where the reader is (or follows the selection), otherwise the
        // note and, when the draft can be moved as it is, Move's destination.
        function draftOwnershipView() {
          if (!draftIsPinned()) return null;
          var a = state.draftAnchor;
          var pinned = storedDraftTarget(a);
          var shown = shownGraphTarget();
          if (targetKey(shown) === targetKey(pinned)) return null;
          var here = targetLabel(pinned);
          if (draftPostHeld()) return { note: "This comment is still being confirmed on " + here + ", so it can't be moved.", moveTo: null };
          if (!hasCommentTarget(shown)) return { note: "Your unsent comment stays on " + here + ". No concept is selected.", moveTo: null };
          // Escape in the composer removes selected text in every layout.
          if (a.context) return { note: "Your unsent comment and its selected text stay on " + here + ". Press Esc to remove the selection before moving it.", moveTo: null };
          return { note: "Your unsent comment stays on " + here + ".", moveTo: shown };
        }
        // Selection triggers (pane attributes, reserved reader, graph
        // selection event, body patches) all land here. Nothing happens unless
        // the shown target really changed, so a re-render inside the pane —
        // including this handler's own count update — never redraws the
        // comment list while the reader types.
        var _graphShownKey = null;
        function onGraphShownTargetChange() {
          if (!isGraphCommentSurface()) return;
          var key = targetKey(shownGraphTarget());
          if (key === _graphShownKey) return;
          _graphShownKey = key;
          syncDefaultDraftTarget();
          announceDraftOwnership();
          updatePageFeedback();
          if (state.openPanel === "comments") renderCommentsPanel();
        }
        window.addEventListener("okf-loom:bodyPatched", function () {
          if (isGraphCommentSurface()) onGraphShownTargetChange();
          else syncDefaultDraftTarget();
        });
        function wireAnchor(draft) {
          if (!isPageLevelAnchor(draft)) return draft;
          return draft && draft.kind === "page" ? { kind: "page" } : { kind: "concept" };
        }
        function wireDetail(draft) {
          return draft && draft.context ? { selection: draft.context } : {};
        }
        // Selected text retained beside a page-level comment. It is advisory
        // context for the reader and the agent — never an anchor, never a span.
        var SELECTION_CONTEXT_MAX = 1024;
        function selectionContext(surface, text, reason) {
          var quote = cpSlice(String(text || ""), 0, SELECTION_CONTEXT_MAX);
          return { surface: surface, precision: "page", quote: quote, truncated: cpLength(String(text || "")) > SELECTION_CONTEXT_MAX, reason: reason };
        }
        // A page draft with no retained selection: the ordinary "Comment on this
        // page" composer for a concept ({kind:"concept"}) or reserved document
        // ({kind:"page"}).
        function isPlainPageDraft(a) {
          return !!a && (a.kind === "concept" || a.kind === "page") && !a.context;
        }
        var _SELECTION_SURFACES = { source: "Source", graph: "graph preview", rendered: "rendered", page: "page" };
        function selectionSurfaceLabel(surface) { return _SELECTION_SURFACES[surface] || "text"; }
        function storedSelectionContext(c) {
          var sel = c && c.detail && typeof c.detail === "object" ? c.detail.selection : null;
          if (sel && typeof sel === "object" && typeof sel.quote === "string" && sel.quote) {
            return { surface: _SELECTION_SURFACES[sel.surface] ? sel.surface : "", quote: sel.quote, truncated: sel.truncated === true };
          }
          if (c && c.anchor && c.anchor.kind === "source" && typeof c.anchor.ref === "string" && c.anchor.ref) {
            return { surface: "source", quote: c.anchor.ref, truncated: false };
          }
          return null;
        }
        // Did the server acknowledge the page-level row this request asked for?
        // `owner` is a concept id (legacy callers) or a {concept, document} target:
        // a concept row acknowledges {kind:"concept"}, a reserved document
        // {kind:"page"}.
        function acknowledgedPageAnchor(anchor, owner) {
          var concept = typeof owner === "string" ? owner : (owner && owner.concept) || "";
          if (!anchor || !_validAnchor(anchor, concept)) return false;
          return concept ? anchor.kind === "concept" : anchor.kind === "page";
        }
        // Blocker 7: stable, key-order-independent deep equality. JSON.stringify
        // is key-order-dependent, so two semantically-equal anchors with
        // different property orders would mismatch (breaking same-draft identity
        // and reply parent preservation). Used for anchor same-draft matching.
        function deepEqual(a, b) {
          if (a === b) return true;
          if (a == null || b == null || typeof a !== "object" || typeof b !== "object") return false;
          if (Array.isArray(a) || Array.isArray(b)) {
            if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
            for (var i = 0; i < a.length; i++) { if (!deepEqual(a[i], b[i])) return false; }
            return true;
          }
          var ka = Object.keys(a), kb = Object.keys(b);
          if (ka.length !== kb.length) return false;
          for (var j = 0; j < ka.length; j++) {
            var k = ka[j];
            if (!Object.prototype.hasOwnProperty.call(b, k)) return false;
            if (!deepEqual(a[k], b[k])) return false;
          }
          return true;
        }
        // Blocker 2: validate a 2xx-acknowledged canonical comment row BEFORE
        // adopting it. Requires a non-empty string id (matching the expected id
        // when provided), an exact concept match, a non-empty row_token, and a
        // discriminated anchor valid for the row's concept. Cross-concept or
        // malformed rows must not be upserted or announced as success.
        function _validCanonicalRow(row, opts) {
          opts = opts || {};
          if (!row || typeof row !== "object") return false;
          if (typeof row.id !== "string" || !row.id) return false;
          if (opts.id && row.id !== opts.id) return false;
          if (!validRowIdentity(row)) return false;
          if (opts.concept && row.concept !== opts.concept) return false;
          if (opts.document && rowDocument(row) !== opts.document) return false;
          if (typeof row.row_token !== "string" || !row.row_token) return false;
          if (!_validAnchor(row.anchor, row.concept)) return false;
          return true;
        }
        function validateStagedTuple(opts) {
          var conceptKey = opts.conceptKey;
          var doc = opts.doc || null;
          if (doc) {
            if (typeof doc.id !== "string" || doc.id !== conceptKey) return { skewSignal: "doc-identity-mismatch" };
            if (!projectionAdmissible(doc.annotation_projection, doc.concept_content_rev, doc.annotation_source, doc.html)) return { skewSignal: "doc-projection-invalid" };
          }
          var projectionRev = doc ? doc.concept_content_rev : opts.fallbackProjectionRev;
          var comments = opts.commentsData && opts.commentsData.comments;
          if (!Array.isArray(comments)) return { skewSignal: "comments-not-array" };
          // R12: events are REQUIRED by default so a commit-capable caller
          // cannot silently downgrade whole-tuple validation by omitting
          // eventData (the canonical commit paths — loadComments, saveReanchor,
          // reconcileReanchor, refreshAfterConflict — all fetch and pass
          // eventData). Only an EXPLICIT opts.commentsOnly === true skips
          // events, and that mode is noncanonical: it must never be used by a
          // commit path. Omitting eventData without commentsOnly fails closed.
          var events = null;
          if (opts.commentsOnly === true) {
            events = null;
          } else {
            if (!opts.eventData) return { skewSignal: "events-required" };
            events = opts.eventData.events;
            if (!Array.isArray(events)) return { skewSignal: "events-not-array" };
          }
          for (var i = 0; i < comments.length; i++) {
            var row = comments[i];
            if (!row || typeof row !== "object") return { skewSignal: "malformed-comment-row" };
            if (typeof row.id !== "string" || !row.id || !validRowIdentity(row)) return { skewSignal: "missing-comment-identity" };
            // Exact text anchors exist only on real concept documents.
            if (!row.concept && !isPageLevelAnchor(row.anchor)) return { skewSignal: "malformed-comment-anchor" };
            if (typeof row.row_token !== "string" || !row.row_token) return { skewSignal: "missing-comment-row-token" };
            // R11: discriminated anchor — closed kind set + exact concept
            // correlation for concept anchors (rejects missing/foreign ref and
            // unsupported kinds), superseding the old "anchor object with a
            // string kind" shape check.
            if (!_validAnchor(row.anchor, row.concept)) return { skewSignal: "malformed-comment-anchor" };
            if (row.concept === conceptKey) {
              if (row.anchor_resolution) {
                if (row.anchor_resolution.concept_content_rev !== projectionRev) return { skewSignal: "comment-anchor-resolution-skew" };
              } else if (!isPageLevelAnchor(row.anchor)) {
                // A non-concept (text) anchor on this concept MUST carry
                // resolution evidence; without it the anchor cannot be verified
                // against the committed document.
                return { skewSignal: "missing-comment-anchor-resolution" };
              }
            }
          }
          // R11: validate every event row. A primitive or malformed member
          // (non-object, missing/non-string id, or a row with no nonempty
          // string discriminator) must not enter authoritative state alongside
          // a freshly-committed document. Persisted events.jsonl rows carry a
          // nonempty `type` (presence/activity/graph/comment/...); accept `kind`
          // too so neither real shape is rejected while primitives and
          // undiscriminated objects fail closed.
          if (events) {
            for (var j = 0; j < events.length; j++) {
              var ev = events[j];
              if (!ev || typeof ev !== "object") return { skewSignal: "malformed-event-row" };
              if (typeof ev.id !== "string" || !ev.id) return { skewSignal: "missing-event-identity" };
              var _evHasType = typeof ev.type === "string" && !!ev.type;
              var _evHasKind = typeof ev.kind === "string" && !!ev.kind;
              if (!_evHasType && !_evHasKind) return { skewSignal: "missing-event-identity" };
            }
          }
          return { comments: comments, events: events ? events.slice() : null, projectionRev: projectionRev };
        }
        function _decodeCanonicalSegment(sourceCps, segment, record) {
          // Decode a segment's raw source text into exactly dom_end-dom_start
          // code points, mirroring the Python producer (markdown_blocks.py):
          // 1. Full HTML entity token -> decoded code points
          // 2. List-item LF/CR/CRLF -> one visible newline; paragraph and
          //    block-quote line endings remain a single space (when dom
          //    width is 1)
          // 3. Block-quote continuation structural separator: \n> , \r\n> ,
          //    \n>\t, nested \n> >  etc -> single space (dom width is 1).
          //    These source spans contain structural > markers stripped
          //    during rendering, not literal visible text.
          // 4. Otherwise direct source characters
          // sourceCps is the caller's one code-point split of the source.
          var raw = sourceCps.slice(segment.source_start, segment.source_end).join("");
          var domLen = segment.dom_end - segment.dom_start;
          var isListItem = !!(record && typeof record.block_id === "string" &&
            /^list-item:[1-9][0-9]*$/.test(record.block_id));
          // Check for full HTML entity token
          if (/^(?:&#[0-9]{1,7};|&#x[0-9A-Fa-f]{1,6};|&[A-Za-z][A-Za-z0-9]{1,31};)$/.test(raw)) {
            var el = document.createElement("span");
            el.innerHTML = raw;
            var decoded = el.textContent || "";
            if (cpLength(decoded) !== domLen) return null;
            return decoded;
          }
          // Check for newline/CR normalization
          var rawCp = Array.from(raw);
          var isNewline = rawCp.length >= 1 && rawCp.every(function(c) { return c === "\n" || c === "\r"; });
          if (isNewline) {
            if (domLen !== 1) return null;
            if (isListItem) {
              return raw === "\n" || raw === "\r" || raw === "\r\n" ? "\n" : null;
            }
            return " ";
          }
          // Check for block-quote continuation structural separator. The
          // Python producer (_quote_content -> _paragraph_content soft-break
          // remap -> _remap_quote_segments) maps continuation markers such
          // as "\n> ", "\r\n>", "\n>\t", and nested "\n> > " to a single
          // DOM space. The pattern is NARROW: exactly one line ending
          // (CRLF | CR | LF) followed by one or more (> + optional single
          // space/tab) groups, mapping to exactly 1 DOM code point. This
          // does NOT broaden to general whitespace -- it matches only the
          // exact structural marker the producer emits for block-quote
          // continuation lines.
          if (domLen === 1 && /^(?:\r\n|\r|\n)(?:>[ \t]?)+$/.test(raw)) {
            return " ";
          }
          // Table-cell escaped pipe. A table row splits cells on unescaped
          // pipes, so the producer (markdown_blocks.py _table_row_content)
          // emits the two source code points "\|" as their own segment mapped
          // to the one visible "|" - in prose and inside a code span alike.
          // NARROW on purpose: the whole segment is exactly "\|", its DOM
          // width is exactly 1, and the record is a table row. This is not
          // backslash stripping: every other backslash (ordinary code, other
          // block kinds, other widths) still has to map one-to-one below, and
          // the caller then checks that the visible code point here is "|".
          if (raw === "\\|" && domLen === 1 && record && typeof record.block_id === "string" &&
              /^table-row:[1-9][0-9]*$/.test(record.block_id)) {
            return "|";
          }
          // GFM task-list checkbox. The producer (markdown_blocks.py
          // _list_item_content) maps the marker source span "[x] " - state
          // x/X/space plus trailing blanks - to the ONE visible template
          // space the renderer emits after the disabled <input> checkbox;
          // the checkbox itself has no textContent. NARROW on purpose: the
          // whole segment is exactly "[c] blanks", its DOM width is exactly
          // 1, and the record is a list item. Every other bracket/blank
          // source text still has to map one-to-one below, and the caller
          // then checks that the decoded code point here is " ".
          if (isListItem && domLen === 1 && /^\[[ xX]\][ \t]+$/.test(raw)) {
            return " ";
          }
          // Direct mapping: source length must equal dom length
          if (rawCp.length === domLen) return raw;
          return null;
        }
      // Both arguments are code-point arrays the caller split once.
      function _overlappingPositions(cpHay, cpNeedle) {
        var positions = [];
        for (var i = 0; i + cpNeedle.length <= cpHay.length; i++) {
          var match = true;
          for (var j = 0; j < cpNeedle.length; j++) {
            if (cpHay[i + j] !== cpNeedle[j]) { match = false; break; }
          }
          if (match) positions.push(i);
        }
        return positions;
      }
    function projectionRecordForElement(doc, element) {
        if (!doc || !validProjection(doc.annotation_projection, doc.concept_content_rev, doc.annotation_source, doc.html) || !element) return null;
      const rawKey = element.getAttribute("data-okf-projection-key");
      if (!/^(0|[1-9][0-9]*)$/.test(rawKey || "")) return null;
      const key = Number(rawKey);
      const record = doc.annotation_projection.blocks.find(function (item) { return item && item.dom_key === key; });
      if (!record || !Number.isInteger(record.source_start) || !Number.isInteger(record.source_end) || record.source_start >= record.source_end) return null;
      return record;
    }
    function domCodePointOffsets(block, range) {
      try {
        const before = document.createRange(); before.selectNodeContents(block); before.setEnd(range.startContainer, range.startOffset);
        const through = document.createRange(); through.selectNodeContents(block); through.setEnd(range.endContainer, range.endOffset);
        return { start: cpLength(before.toString()), end: cpLength(through.toString()) };
      } catch (e) { return null; }
    }
      function sourceSpanForDomRange(record, domStart, domEnd) {
        const segments = Array.isArray(record && record.segments) ? record.segments : [];
        const used = segments.filter(function (segment) { return segment.dom_end > domStart && segment.dom_start < domEnd; });
        if (!used.length) return null;
        for (let i = 1; i < used.length; i++) if (used[i - 1].dom_end !== used[i].dom_start || used[i - 1].source_end !== used[i].source_start) return null;
        function boundary(segment, offset) {
          if (offset === segment.dom_start) return segment.source_start;
          if (offset === segment.dom_end) return segment.source_end;
          const domLength = segment.dom_end - segment.dom_start, sourceLength = segment.source_end - segment.source_start;
          return domLength === sourceLength && offset > segment.dom_start && offset < segment.dom_end
            ? segment.source_start + (offset - segment.dom_start) : null;
        }
        const start = boundary(used[0], domStart), end = boundary(used[used.length - 1], domEnd);
        return start == null || end == null ? null : { start, end };
      }
      function domSpanForSourceRange(record, sourceStart, sourceEnd) {
        const segments = Array.isArray(record && record.segments) ? record.segments : [];
        const used = segments.filter(function (segment) { return segment.source_end > sourceStart && segment.source_start < sourceEnd; });
        if (!used.length) return null;
        for (let i = 1; i < used.length; i++) if (used[i - 1].source_end !== used[i].source_start || used[i - 1].dom_end !== used[i].dom_start) return null;
        function boundary(segment, offset) {
          if (offset === segment.source_start) return segment.dom_start;
          if (offset === segment.source_end) return segment.dom_end;
          const sourceLength = segment.source_end - segment.source_start, domLength = segment.dom_end - segment.dom_start;
          return sourceLength === domLength && offset > segment.source_start && offset < segment.source_end
            ? segment.dom_start + (offset - segment.source_start) : null;
        }
        const start = boundary(used[0], sourceStart), end = boundary(used[used.length - 1], sourceEnd);
        return start == null || end == null ? null : { start, end };
      }
    async function captureCanonicalAnchor() {
      const selected = getSelectionInBody() || resolveSelectionDraft();
      if (!selected) throw new Error("Select the exact text this comment refers to in the document.");
      if (selected.inSource || selected.inGraph) throw new Error("Select text inside one rendered document block.");
      const renderedExact = selected.sel ? selected.sel.toString() : selected.text;
      if (!renderedExact || !renderedExact.trim()) throw new Error("Select the exact text this comment refers to in the document.");
      // R6-3: capture ownership fences BEFORE the canonical-doc await. A
      // navigation, editor supersession, or document-generation bump during
      // the await invalidates the capture; we revalidate after the await.
      const fenceGeneration = state.annotationGeneration;
      const fenceConcept = state.conceptId;
      // Ensure the canonical document FIRST. commitDocument may REPLACE the
      // body during this await, which detaches the captured DOM Range. All
      // Range/block/offset/occurrence evidence MUST be (re)computed AFTER
      // this await against the canonical bytes + the live DOM — never mapped
      // through a detached Range onto new bytes.
      const doc = await ensureCanonicalDoc();
      // R6-3: revalidate ownership after the await.
      if (state.annotationGeneration !== fenceGeneration || state.conceptId !== fenceConcept) {
        throw new DOMException("Superseded", "AbortError");
      }
      // R6-3: re-resolve the selection against the LIVE body. If the body was
      // replaced during the await, the original Range is detached; re-reading
      // the live selection (or the draft, which carries the stable text)
      // yields a fresh Range against canonical bytes. If neither resolves,
      // fail closed — never map stale offsets onto new bytes.
      const bodyEl = $(".okf-page__body");
      const originalDetached = !bodyEl || !selected.range ||
        !selected.range.startContainer || !bodyEl.contains(selected.range.startContainer);
      let live = null;
      try { live = getSelectionInBody(); } catch (e) { live = null; }
      if (originalDetached) {
        const reResolved = resolveSelectionDraft();
        if (!reResolved) throw new Error("The selection is no longer valid; the document was refreshed. Please reselect the exact text.");
        selected.sel = reResolved.sel;
        selected.range = reResolved.range;
        selected.text = reResolved.text;
        selected.inSource = reResolved.inSource;
        selected.inGraph = reResolved.inGraph;
      } else if (live && live.text && live.text === renderedExact) {
        // Live selection survived unchanged — use its Range (against the live body).
        selected.sel = live.sel;
        selected.range = live.range;
      }
      selected.range = rangeOverSelectedText(selected.range);
      const domBlock = renderedBlockForRange(selected.range);
      if (!domBlock) throw new Error("Select text inside a single document block.");
      // Prove both Range boundaries are valid UTF-16 boundaries before using
      // the browser selection. This catches half-surrogate test fixtures.
      // R6-3: checked AFTER the await against the live (re-resolved) Range.
      if (selected.range.startContainer.nodeType === Node.TEXT_NODE && utf16ToCodePoint(selected.range.startContainer.nodeValue || "", selected.range.startOffset) == null) throw new Error("The selection boundary is invalid.");
      if (selected.range.endContainer.nodeType === Node.TEXT_NODE && utf16ToCodePoint(selected.range.endContainer.nodeValue || "", selected.range.endOffset) == null) throw new Error("The selection boundary is invalid.");
      const source = doc.annotation_source;
      const record = projectionRecordForElement(doc, domBlock);
      if (!record) throw new Error("This rendered block has no canonical source mapping.");
      if (!liveBlockMatchesProjection(doc, domBlock)) throw new Error("This rendered block no longer matches its canonical source mapping.");
      const domOffsets = domCodePointOffsets(domBlock, selected.range);
      const canonical = domOffsets && sourceSpanForDomRange(record, domOffsets.start, domOffsets.end);
      if (!canonical) throw new Error("The selection crosses an unmappable rendered boundary.");
      const start = canonical.start, end = canonical.end;
      const exact = cpSlice(source, start, end);
      if (!exact || cpLength(exact) > ANNOTATION_MAX_EXACT) throw new Error("The selection is too long. Select 16,384 characters or fewer.");
      return quoteAnchorForSpan(source, record, start, end);
    }
    // The one closed schema-2 anchor constructor, shared by rendered and Source
    // capture so both produce byte-identical evidence for the same span.
    async function quoteAnchorForSpan(source, record, start, end) {
      const prefixStart = Math.max(record.source_start, start - 256), suffixEnd = Math.min(record.source_end, end + 256);
      return {
        schema: 2, kind: "text-quote", exact: cpSlice(source, start, end),
        prefix: cpSlice(source, prefixStart, start), suffix: cpSlice(source, end, suffixEnd),
        heading: { path: Array.isArray(record.heading_path) ? record.heading_path.slice() : [] },
        block: { id: record.block_id, hash: await sha256Text(cpSlice(source, record.source_start, record.source_end)), start: record.source_start, end: record.source_end },
        position: { start: start, end: end },
      };
    }
    // ---- Source pane → canonical span ---------------------------------
    // The pane shows doc.raw: the body after frontmatter, with the file's
    // final line terminator dropped. Its offset inside the canonical source is
    // established as a VERIFIED IDENTITY (the canonical source ends with the
    // pane text, optionally followed by one "\n"), never by searching for the
    // selected words. When the identity does not hold — CRLF files, a stale
    // pane, an ambiguous tail — there is no exact anchor and the caller falls
    // back to an explicit page comment that keeps the selection as context.
    function SourceMappingUnavailable(reason) { this.reason = reason; }
    function sourcePaneOffset(doc, paneText) {
      const source = doc && doc.annotation_source;
      if (typeof source !== "string" || typeof paneText !== "string" || !paneText || doc.raw !== paneText) return null;
      const bare = source.endsWith(paneText), terminated = source.endsWith(paneText + "\n");
      if (bare === terminated) return null; // neither holds, or both do (ambiguous)
      return cpLength(source) - cpLength(paneText) - (terminated ? 1 : 0);
    }
    async function captureSourceAnchor(selected) {
      const pane = $(".okf-source");
      if (!pane || !selected || !selected.range || !pane.contains(selected.range.commonAncestorContainer)) {
        throw new SourceMappingUnavailable("the Source selection is no longer available");
      }
      const fenceGeneration = state.annotationGeneration, fenceConcept = state.conceptId;
      const doc = await ensureCanonicalDoc();
      if (state.annotationGeneration !== fenceGeneration || state.conceptId !== fenceConcept) throw new DOMException("Superseded", "AbortError");
      const range = selected.range;
      if (!pane.isConnected || !pane.contains(range.commonAncestorContainer)) throw new SourceMappingUnavailable("the Source view was refreshed");
      if (range.startContainer.nodeType === Node.TEXT_NODE && utf16ToCodePoint(range.startContainer.nodeValue || "", range.startOffset) == null) throw new SourceMappingUnavailable("the selection boundary is invalid");
      if (range.endContainer.nodeType === Node.TEXT_NODE && utf16ToCodePoint(range.endContainer.nodeValue || "", range.endOffset) == null) throw new SourceMappingUnavailable("the selection boundary is invalid");
      const offset = sourcePaneOffset(doc, pane.textContent || "");
      if (offset == null) throw new SourceMappingUnavailable("this Source view is not a byte-exact view of the saved file");
      const paneOffsets = domCodePointOffsets(pane, range);
      if (!paneOffsets) throw new SourceMappingUnavailable("the selection boundary is invalid");
      // Dropping the selection's own leading/trailing whitespace is exact
      // arithmetic on the selected string, not a guess about intent.
      const selectedText = range.toString();
      const lead = cpLength((selectedText.match(/^\s*/) || [""])[0]), trail = cpLength((selectedText.match(/\s*$/) || [""])[0]);
      const start = offset + paneOffsets.start + lead, end = offset + paneOffsets.end - trail;
      if (start >= end) throw new SourceMappingUnavailable("the selection contains no text");
      const source = doc.annotation_source;
      if (cpSlice(source, start, end) !== selectedText.trim()) throw new SourceMappingUnavailable("the selection does not match the saved file");
      if (end - start > ANNOTATION_MAX_EXACT) throw new SourceMappingUnavailable("the selection is longer than 16,384 characters");
      // Mirror the server's capture rule: exactly one containing block.
      const records = doc.annotation_projection.blocks.filter(function (item) {
        return item && Number.isInteger(item.source_start) && Number.isInteger(item.source_end)
          && item.source_start <= start && end <= item.source_end;
      });
      if (records.length !== 1) throw new SourceMappingUnavailable("the selection is not inside a single source block");
      return { anchor: await quoteAnchorForSpan(source, records[0], start, end), rev: doc.concept_content_rev };
    }

  // ---- comment text-range marks (iter1 CRI-002) -----------------------
  // Wraps a Range in <mark data-comment-id class="okf-comment-mark">. Used
  // optimistically on comment creation and re-applied from persisted anchors
  // after every body patch. surroundContents fails when the range crosses
  // element boundaries, so we extract + rewrap node-by-node.
  function wrapRangeInMark(range, commentId, commentState) {
    try {
      const mark = el("mark", { class: "okf-comment-mark", "data-comment-id": commentId });
      if (commentState) mark.setAttribute("data-comment-state", commentState);
      range.surroundContents(mark);
      return mark;
    } catch (e) {
      // Range crosses element boundaries: fall back to wrapping each text
      // node segment. Collect the wrapped marks so the caller can track them.
      return wrapRangeAcrossElements(range, commentId, commentState);
    }
  }
  function wrapRangeAcrossElements(range, commentId, commentState) {
    const marks = [];
    const nodes = textNodesInRange(range);
    nodes.forEach((tn) => {
      const parent = tn.parentNode;
      if (!parent) return;
      const start = (tn === range.startContainer) ? range.startOffset : 0;
      const end = (tn === range.endContainer) ? range.endOffset : tn.nodeValue.length;
      if (start >= end) return;
      const sub = tn.nodeValue.slice(start, end);
      if (!sub) return;
      const mark = el("mark", { class: "okf-comment-mark", "data-comment-id": commentId });
      if (commentState) mark.setAttribute("data-comment-state", commentState);
      mark.appendChild(document.createTextNode(sub));
      const frag = document.createDocumentFragment();
      const before = tn.nodeValue.slice(0, start);
      const after = tn.nodeValue.slice(end);
      if (before) frag.appendChild(document.createTextNode(before));
      frag.appendChild(mark);
      if (after) frag.appendChild(document.createTextNode(after));
      parent.replaceChild(frag, tn);
      marks.push(mark);
    });
    return marks[0] || null;
  }
  function textNodesInRange(range) {
    const out = [];
    if (range.commonAncestorContainer.nodeType === Node.TEXT_NODE) {
      if (range.intersectsNode(range.commonAncestorContainer)) {
        out.push(range.commonAncestorContainer);
      }
      return out;
    }
    const walker = document.createTreeWalker(range.commonAncestorContainer, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (!range.intersectsNode(n)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let n; while ((n = walker.nextNode())) out.push(n);
    return out;
  }
  // Removes a comment's mark(s) by id, unwrapping the text back into the
  // flow. Used on failed post and to clear a stale optimistic mark.
  function removeCommentMark(commentId) {
    if (!commentId) return;
    const marks = $$('.okf-comment-mark[data-comment-id="' + cssEscape(commentId) + '"]');
    marks.forEach((m) => {
      const parent = m.parentNode;
      if (!parent) return;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize(); // merge adjacent text nodes back together
    });
  }
  function renameCommentMark(oldId, newId) {
    if (!oldId || !newId || oldId === newId) return;
    const marks = $$('.okf-comment-mark[data-comment-id="' + cssEscape(oldId) + '"]');
    marks.forEach((m) => m.setAttribute("data-comment-id", newId));
  }
  function setCommentMarkState(commentId, commentState) {
    if (!commentId) return;
    const marks = $$('.okf-comment-mark[data-comment-id="' + cssEscape(commentId) + '"]');
    marks.forEach((m) => {
      if (commentState) m.setAttribute("data-comment-state", commentState);
      else m.removeAttribute("data-comment-state");
    });
  }
    function visibleRangesForText(root, text) {
      const nodes = [], starts = []; let all = "";
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode(n) { return n.parentElement && n.parentElement.closest(".okf-comment-mark, button, .okf-code-toolbar") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT; },
      });
      let node; while ((node = walker.nextNode())) { starts.push(all.length); nodes.push(node); all += node.nodeValue || ""; }
      const ranges = []; let from = 0;
      while (text && (from = all.indexOf(text, from)) >= 0) {
        const to = from + text.length;
        let si = starts.length - 1, ei = starts.length - 1;
        for (let i = 0; i < starts.length; i++) {
          const end = starts[i] + (nodes[i].nodeValue || "").length;
          if (from >= starts[i] && from < end) { si = i; break; }
        }
        for (let i = si; i < starts.length; i++) {
          const end = starts[i] + (nodes[i].nodeValue || "").length;
          if (to <= end) { ei = i; break; }
        }
        try {
          const range = document.createRange();
          range.setStart(nodes[si], from - starts[si]); range.setEnd(nodes[ei], to - starts[ei]);
          ranges.push(range);
        } catch (e) { /* fail closed */ }
        from += 1;
      }
      return ranges;
    }
    function rangeForDomCodePointOffsets(root, startCp, endCp) {
      const nodes = []; let total = 0; let startPoint = null; let endPoint = null;
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode(n) { return n.parentElement && n.parentElement.closest("button, .okf-code-toolbar") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT; },
      });
      let node;
      while ((node = walker.nextNode())) {
        const chars = Array.from(node.nodeValue || ""); const next = total + chars.length;
        if (!startPoint && startCp >= total && startCp <= next) startPoint = { node, offset: chars.slice(0, startCp - total).join("").length };
        if (!endPoint && endCp >= total && endCp <= next) { endPoint = { node, offset: chars.slice(0, endCp - total).join("").length }; break; }
        total = next; nodes.push(node);
      }
      if (!startPoint || !endPoint || startCp >= endCp) return null;
      try { const range = document.createRange(); range.setStart(startPoint.node, startPoint.offset); range.setEnd(endPoint.node, endPoint.offset); return range; } catch (e) { return null; }
    }

    // Marks consume the server's typed resolution. No client first-match or
    // fuzzy fallback is permitted; a mark exists only for a unique resolved
    // projection and is removed for every other health state.
    function applyCommentMarks() {
    if (!isConceptPage()) return;
    const body = $(".okf-page__body");
    if (!body) return;
      const mine = state.comments.filter((c) => c.concept === state.conceptId && c.anchor);
      const heldSelection = bodySelectionTextOffsets(body);
      // Canonical refresh invalidates disposable projections first.
      $$(".okf-comment-mark", body).forEach((m) => removeCommentMark(m.getAttribute("data-comment-id")));
      mine.forEach((c) => {
        const resolution = c.anchor_resolution || {};
        c._anchorHealth = resolution.status || "checking";
        if (resolution.status !== "resolved") return;
        const exact = c.anchor.kind === "text-quote" ? c.anchor.exact : c.anchor.kind === "text" ? c.anchor.ref : "";
        if (!exact) return;
        const doc = state.doc;
        const resolvedBlock = resolution.block || {};
          const record = doc && validProjection(doc.annotation_projection, resolution.concept_content_rev, doc.annotation_source, doc.html)
          ? doc.annotation_projection.blocks.find(function (item) {
              return item && item.block_id === resolvedBlock.id && item.source_start === resolvedBlock.start && item.source_end === resolvedBlock.end && Number.isInteger(item.dom_key);
            }) : null;
        const projectedBlock = record ? body.querySelector('[data-okf-projection-key="' + record.dom_key + '"]') : null;
        if (!record || !projectedBlock || !resolution.position) { c._projectionUnavailable = true; return; }
        const domSpan = domSpanForSourceRange(record, resolution.position.start, resolution.position.end);
        const projectedRange = domSpan && rangeForDomCodePointOffsets(projectedBlock, domSpan.start, domSpan.end);
        if (!projectedRange) { c._projectionUnavailable = true; return; }
        c._projectionUnavailable = false;
        wrapRangeInMark(projectedRange, c.id, c.state || "open");
      });
      restoreBodySelection(body, heldSelection);
    }
    // Refreshing marks unwraps and re-wraps text nodes. The body's text does not
    // change, but the DOM relocates a live Range boundary that sat inside a
    // rewritten node to the edge of that node - so a reader's selection that
    // began inside a highlighted passage silently grew to the whole passage
    // when any comment refresh landed (the live handshake re-fetches comments
    // just after load). The selection is therefore held as offsets into the
    // body's text, which a mark refresh cannot change, and put back exactly.
    function bodySelectionTextOffsets(body) {
      const sel = window.getSelection && window.getSelection();
      if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
      const range = sel.getRangeAt(0);
      if (!body.contains(range.startContainer) || !body.contains(range.endContainer)) return null;
      function measure(node, offset) {
        const before = document.createRange();
        before.selectNodeContents(body);
        before.setEnd(node, offset);
        return before.toString().length;
      }
      try {
        const backward = sel.anchorNode === range.endContainer && sel.anchorOffset === range.endOffset &&
          !(sel.focusNode === range.endContainer && sel.focusOffset === range.endOffset);
        return { start: measure(range.startContainer, range.startOffset), end: measure(range.endContainer, range.endOffset), text: sel.toString(), backward: backward };
      } catch (e) { return null; }
    }
    function restoreBodySelection(body, held) {
      if (!held || held.start >= held.end) return;
      const sel = window.getSelection && window.getSelection();
      if (!sel) return;
      // Untouched by this refresh (no mark met the selection): leave it alone.
      const now = bodySelectionTextOffsets(body);
      if (now && now.start === held.start && now.end === held.end) return;
      const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, null);
      let total = 0, startPoint = null, endPoint = null, node;
      while ((node = walker.nextNode())) {
        const next = total + (node.nodeValue || "").length;
        // A start on a node junction belongs to the following node, an end to
        // the preceding one, so the range never gains an empty edge node.
        if (!startPoint && held.start >= total && held.start < next) startPoint = { node: node, offset: held.start - total };
        if (startPoint && held.end > total && held.end <= next) { endPoint = { node: node, offset: held.end - total }; break; }
        total = next;
      }
      if (!startPoint || !endPoint) return;
      try {
        if (held.backward) sel.setBaseAndExtent(endPoint.node, endPoint.offset, startPoint.node, startPoint.offset);
        else sel.setBaseAndExtent(startPoint.node, startPoint.offset, endPoint.node, endPoint.offset);
      } catch (e) { /* the selection is the reader's; never throw over it */ }
    }
  // A user can select text and open the comment composer before the lazy
  // /__data/doc load (or a live patch) settles. The body patch correctly
  // re-applies persisted comments via applyCommentMarks(), but a not-yet-sent
  // draft only exists as state._pendingMarkId + state.draftAnchor. Re-resolve
  // that pending local mark as well so the user-visible selection highlight
  // does not disappear underneath the open composer.
  function applyPendingDraftMark() {
    if (!isConceptPage()) return;
    const pendingId = state._pendingMarkId;
    const anchor = state.draftAnchor || {};
    if (!pendingId) return;
    if (anchor.concept && anchor.concept !== state.conceptId) return;
    const body = $(".okf-page__body");
    if (!body) return;
    const selector = '.okf-comment-mark[data-comment-id="' + cssEscape(pendingId) + '"]';
    if (body.querySelector(selector)) return;
    // R5-12: a v2 text-quote draft carries validated block/position evidence
    // captured against a specific concept_content_rev. Project it through the
    // annotation projection (the SAME path applyCommentMarks uses for resolved
    // comments) so the highlight targets the UNIQUE occurrence — not a
    // body-wide first-text match that can land on a duplicate after a patch.
    // Revision-qualify: only re-project when the current doc's content rev
    // matches the capture rev. On a content-revision change the block/position
    // evidence is keyed to OLD bytes, so the draft mark is NOT reapplied to
    // new bytes; the composer retains the anchor and the server validates it
    // honestly on send.
    if (anchor.kind === "text-quote" && anchor.block && anchor.position) {
      const doc = state.doc;
      const captureRev = state.draftContentRev || null;
      if (!doc || !doc.concept_content_rev || captureRev !== doc.concept_content_rev) return;
      if (!validProjection(doc.annotation_projection, doc.concept_content_rev, doc.annotation_source, doc.html)) return;
      const draftBlock = anchor.block;
      const record = doc.annotation_projection.blocks.find(function (item) {
        return item && item.block_id === draftBlock.id && item.source_start === draftBlock.start && item.source_end === draftBlock.end && Number.isInteger(item.dom_key);
      });
      const projectedBlock = record ? body.querySelector('[data-okf-projection-key="' + record.dom_key + '"]') : null;
      if (!record || !projectedBlock) return;
      const domSpan = domSpanForSourceRange(record, anchor.position.start, anchor.position.end);
      const projectedRange = domSpan && rangeForDomCodePointOffsets(projectedBlock, domSpan.start, domSpan.end);
      if (!projectedRange) return;
      wrapRangeInMark(projectedRange, pendingId, "open");
      return;
    }
    // Legacy {kind:"text",ref} drafts carry no block/position evidence, so
    // keep the heading-scoped first-text fallback for backward compatibility.
    let needle = null;
    if (anchor.kind === "text") {
      needle = typeof anchor.ref === "string" ? anchor.ref : null;
    }
    if (!needle) return;
    let scope = body;
    // Legacy drafts may carry a DOM heading id to narrow the search.
    if (anchor.kind === "text" && anchor.block_id) {
      const block = body.querySelector('#' + cssEscape(anchor.block_id));
      if (block) scope = block;
    }
    const found = findTextNode(scope, needle);
    if (found) wrapTextNode(found, pendingId, "open");
  }
  function wrapTextNode(found, commentId, commentState) {
    try {
      const range = document.createRange();
      range.setStart(found.node, found.start);
      range.setEnd(found.node, found.end);
      const mark = el("mark", { class: "okf-comment-mark", "data-comment-id": commentId });
      if (commentState) mark.setAttribute("data-comment-state", commentState);
      range.surroundContents(mark);
    } catch (e) { /* selection crossed a boundary; skip this mark */ }
  }
    function jumpToCommentMark(commentId) {
    const mark = document.querySelector('.okf-comment-mark[data-comment-id="' + cssEscape(commentId) + '"]');
      if (!mark) return false;
      mark.setAttribute("tabindex", "-1");
    mark.scrollIntoView({ block: "center", behavior: REDUCED_MOTION ? "auto" : "smooth" });
      if (!REDUCED_MOTION) {
      mark.classList.remove("okf-pulse");
      void mark.offsetWidth;
      mark.classList.add("okf-pulse");
      }
      try { mark.focus({ preventScroll: true }); } catch (e) {}
      return true;
  }
  function cssEscape(s) {
    // Minimal CSS.escape polyfill (attribute selector on comment ids, which
    // are ULID/local-* strings - safe character set, but guard anyway).
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, (ch) => "\\" + ch);
  }

  // --- composer ---------------------------------------------------------
  // The composer textarea's aria-describedby is a list several features
  // share. Each adds or removes only its own id; the known ids keep a fixed
  // order (validation first, then the draft-target note, then the always-on
  // purpose line) and any other id keeps its place after them.
  var COMPOSER_DESCRIPTION_ORDER = ["okf-composer-validation", "okf-composer-ownership-note", "okf-composer-purpose"];
  function setComposerDescription(node, id, on) {
    var ids = (node.getAttribute("aria-describedby") || "").split(/\s+/).filter(function (x) { return x && x !== id; });
    if (on) ids.push(id);
    function rank(x) { var i = COMPOSER_DESCRIPTION_ORDER.indexOf(x); return i < 0 ? COMPOSER_DESCRIPTION_ORDER.length : i; }
    ids = ids.map(function (x, i) { return [x, i]; })
      .sort(function (p, q) { return rank(p[0]) - rank(q[0]) || p[1] - q[1]; })
      .map(function (p) { return p[0]; });
    var value = ids.join(" ");
    if (value === (node.getAttribute("aria-describedby") || "")) return;
    if (value) node.setAttribute("aria-describedby", value);
    else node.removeAttribute("aria-describedby");
  }
  function composerNode() {
    const wrap = el("div", { class: "okf-composer" });
    const anchorRow = el("div", { class: "okf-composer__anchor", "aria-live": "polite" });
    const anchorLabel = el("span", { text: "On: " });
    const anchorRef = el("code");
    anchorRow.appendChild(anchorLabel);
    anchorRow.appendChild(anchorRef);
    // R3 precision line: says whether the draft is an exact annotation or a
    // page comment (and, for a page-level fallback, that the Source/graph
    // selection is quoted as a page-level selection), or that this surface has
    // no commentable document. It lives in the anchor row's polite live
    // region, so it is written only when its text actually changes.
    const anchorNote = el("p", { class: "okf-composer__anchor-note", hidden: "" });
    anchorRow.appendChild(anchorNote);
    // R3: what a comment is, said where one is written. Static and not live;
    // the textarea is always described by it (after any validation message
    // and draft-target note).
    const purposeId = "okf-composer-purpose";
    const purpose = el("p", { id: purposeId, class: "okf-composer__purpose okf-muted",
      text: "A comment asks for work or discussion. It is saved on this bundle for readers and its agent, which picks it up through its comment loop; Studio does not start an agent." });
    const textarea = el("textarea", { class: "okf-composer__textarea", rows: "3",
      "aria-label": "Comment for agent", placeholder: "Describe the work or question…" });
    setComposerDescription(textarea, purposeId, true);
    const validationId = "okf-composer-validation";
    const validation = el("p", { id: validationId, class: "okf-composer__error", hidden: "", "aria-live": "polite" });
    textarea.value = state.draftBody || "";
    function setDraftValidation(message) {
      if (message) {
        validation.textContent = message;
        validation.hidden = false;
        textarea.setAttribute("aria-invalid", "true");
        setComposerDescription(textarea, validationId, true);
      } else {
        validation.textContent = "";
        validation.hidden = true;
        textarea.removeAttribute("aria-invalid");
        setComposerDescription(textarea, validationId, false);
      }
    }
    // R4 draft target: on the graph a pinned draft can sit on another concept
    // than the one selected. This labelled group (deliberately not a live
    // region; the one-off announcement goes to the shared status) says where
    // the draft stays and, when it can be moved as it is, offers Move. It sits
    // before the textarea, so Tab goes Move -> textarea -> Cancel -> Send.
    const ownershipNoteId = "okf-composer-ownership-note";
    const ownership = el("div", { class: "okf-composer__ownership", role: "group", "aria-label": "Draft target", hidden: "" });
    const ownershipNote = el("p", { id: ownershipNoteId, class: "okf-composer__ownership-note" });
    const moveBtn = el("button", { type: "button", class: "okf-btn okf-composer__move", hidden: "" });
    ownership.appendChild(ownershipNote);
    ownership.appendChild(moveBtn);
    function refreshOwnership() {
      const view = draftOwnershipView();
      const note = view ? view.note : "";
      if (ownershipNote.textContent !== note) ownershipNote.textContent = note;
      ownership.hidden = !view;
      const moveTo = view && view.moveTo;
      const moveLabel = moveTo ? "Move draft to " + targetLabel(moveTo) : "";
      if (moveBtn.textContent !== moveLabel) moveBtn.textContent = moveLabel;
      moveBtn.hidden = !moveTo;
      setComposerDescription(textarea, ownershipNoteId, !!view);
    }
    moveBtn.addEventListener("click", () => {
      const view = draftOwnershipView();
      if (!view || !view.moveTo) { refreshOwnership(); return; }
      state.draftAnchor = pageDraftAnchor(view.moveTo);
      state.draftContentRev = null;
      state.draftGeneration = (state.draftGeneration || 0) + 1;
      clearDraftTargetMissing();
      if (state.composerPost && state.composerPost.outcome === "rejected") { state.composerPost = null; state.composerStatus = null; }
      resetDraftOwnershipAnnouncements();
      // The live anchor row announces the new target ("On page: B").
      refreshAnchor();
      _refreshComposerSubmitState();
      try { textarea.focus({ preventScroll: true }); } catch (e) {}
    });
    // Req1/state-ownership: a genuine user edit to the draft invalidates any
    // pending retry key (the draft no longer matches the in-flight/failed
    // attempt) AND bumps draftGeneration so a late restore cannot overwrite a
    // newer user draft. Programmatic .value restores do not fire 'input', so
    // the post-failure restore is safe.
    textarea.addEventListener("input", () => {
      state.draftBody = textarea.value;
      state.draftGeneration = (state.draftGeneration || 0) + 1;
      // A rejected post is over once the draft changes. An unconfirmed post
      // is kept: it may already be stored, so its key stays for the sent
      // draft, and _refreshComposerSubmitState offers "Retry send" only while
      // the draft is still that one (else "Send" with a warning).
      const settledPost = !!(state.composerPost && state.composerPost.outcome !== "pending");
      if (state.composerPost && state.composerPost.outcome === "rejected") {
        state.composerPost = null;
        // A deleted target stays explained while Send stays off for it.
        if (!draftTargetIsMissing()) state.composerStatus = null;
      }
      if (textarea.value.trim()) setDraftValidation(null);
      // An emptied graph draft is no longer pinned: it follows the selection.
      else if (isGraphCommentSurface()) syncDefaultDraftTarget();
      refreshSubmitState();
      if (settledPost) _refreshComposerSubmitState();
    });
    const actions = el("div", { class: "okf-composer__actions" });
    // iter3 CRI3-011: was "⏎ to send · Esc clears selection anchor" — a
    // bare emoji glyph that renders inconsistently across platforms
    // (Apple's return-symbol, some Windows fonts show a missing-glyph
    // box) and was the only non-SVG/Unicode-glyph icon strategy in the
    // composer. Use a <kbd> element per WCAG/UU conventions for keyboard
    // hints: accessible (semantics), consistent across platforms, and
    // conventional for kbd hints. The studio.css already styles kbd
    // (wiki.css:516 .okf-prose code applies; composer adds its own
    // .okf-composer__kbd weight).
    const hint = el("span", { class: "okf-composer__hint" });
    hint.appendChild(el("kbd", { class: "okf-composer__kbd", text: "Enter" }));
    hint.appendChild(document.createTextNode(" to send · "));
    hint.appendChild(el("kbd", { class: "okf-composer__kbd", text: "Esc" }));
    hint.appendChild(document.createTextNode(" clears selection anchor"));
    const cancel = el("button", { type: "button", class: "okf-iconbtn okf-composer__cancel", text: "Cancel" });
    const submit = el("button", { type: "button", class: "okf-studiobtn okf-composer__submit", text: "Send" });
    // R6-4: a text-anchor (v2 text-quote) draft carries block/position
    // evidence captured against a specific concept_content_rev. Submission
    // must be disabled until the canonical revision is known AND the draft's
    // capture rev exactly matches the currently-rendered doc rev — otherwise
    // the server would validate the anchor against bytes the user can no
    // longer see, or accept a pre-canonical draft with no rev at all.
    // Concept/source/graph anchors have no rev, so they are never gated here.
    function composerSubmitDisabled() {
      if (!CAN_COMMENT) return false;
      // Retry refresh only re-reads the comments: it never depends on the
      // draft (whose anchor a newer revision may have reset).
      if (state.composerPost && state.composerPost.outcome === "accepted-refresh-failed") return false;
      if (state.composerPost && state.composerPost.outcome === "pending") return true;
      if (!textarea.value.trim()) return true;
      const a = state.draftAnchor || {};
      if (a.kind !== "text-quote") return !hasCommentTarget(draftTarget(state.draftAnchor)) || draftTargetIsMissing();
      const docRev = state.doc && state.doc.concept_content_rev;
      return !state.draftContentRev || state.draftContentRev !== docRev;
    }
    function refreshSubmitState() {
      if (submit) submit.disabled = composerSubmitDisabled();
    }
    refreshSubmitState();
    // The anchor row is a polite live region: rewriting a node with the text
    // it already shows can make a screen reader repeat it, so every write in
    // it goes through this change-only setter.
    function setAnchorText(node, text) {
      if (node.textContent !== text) node.textContent = text;
    }
    function refreshAnchor() {
      const a = state.draftAnchor || defaultDraftAnchor();
      const target = a.kind === "text-quote" ? (state.conceptId || "") : targetLabel(draftTarget(a));
      setAnchorText(anchorLabel, (a.kind === "text" || a.kind === "text-quote") ? "On: " : "On page: ");
      let note;
      if (a.kind === "text" || a.kind === "text-quote") {
        setAnchorText(anchorRef, "“" + (a.exact || a.ref || "") + "”");
        // "once sent": an exact Source-view draft has no pending mark, so
        // nothing is highlighted until the saved comment is drawn.
        note = "Exact annotation: pinned to this passage and, once sent, highlighted on the page. It is re-checked when the page changes.";
      } else if (!target) {
        setAnchorText(anchorRef, "no document");
        note = isConceptPage() ? "" : "Open a document, or select a concept in the graph, to comment on it.";
      } else if (a.context) {
        setAnchorText(anchorRef, target);
        note = "Page comment. Your " + selectionSurfaceLabel(a.context.surface)
          + " selection “" + cpSlice(a.context.quote, 0, 140) + (cpLength(a.context.quote) > 140 || a.context.truncated ? "…" : "")
          + "” is quoted in the comment, not attached to exact text: " + a.context.reason + ".";
      } else if (exactAnchoringAvailable() === false) {
        setAnchorText(anchorRef, target);
        note = "Page comments only: this page's source map could not be fully verified, so text cannot be pinned exactly. Selected text is quoted in the comment.";
      } else {
        setAnchorText(anchorRef, target);
        note = "Page comment: about the whole page. No text is highlighted.";
      }
      setAnchorText(anchorNote, note);
      anchorNote.hidden = !note;
      refreshOwnership();
    }
    refreshAnchor();
      cancel.addEventListener("click", () => {
      // R6-7: remove the pending mark DOM BEFORE clearing the ID so
      // removeCommentMark can find the element by its data-comment-id.
      if (state._pendingMarkId) removeCommentMark(state._pendingMarkId);
      state.draftAnchor = defaultDraftAnchor();
      state.draftBody = "";
      state._pendingMarkId = null;
      state.draftContentRev = null;
      state.replyTo = null;  // Blocker 1: clear reply target on cancel
      // Req1/Q4: cancelling abandons an in-flight or rejected attempt. A post
      // whose result could not be confirmed (Cancel) or was accepted but not
      // re-read (Dismiss) may already be stored, so its record and key stay,
      // as an inline reply's do across Cancel: sending the same text and
      // anchor again is "Retry send" under that key, anything else is sent as
      // new with the "may already be posted" warning. A reply record is
      // dropped with its reply target, so its parent never carries over.
      const unconfirmed = state.composerPost;
      state.composerPost = unconfirmed && !unconfirmed.parent
        && (unconfirmed.outcome === "uncertain" || unconfirmed.outcome === "accepted-refresh-failed")
        ? { key: unconfirmed.key, body: unconfirmed.body, anchor: unconfirmed.anchor, concept: unconfirmed.concept,
          document: unconfirmed.document, outcome: "uncertain", parent: null }
        : null;
      state.composerStatus = null;
      state.draftTargetMissing = "";
      resetDraftOwnershipAnnouncements();
      state.draftGeneration = (state.draftGeneration || 0) + 1;
        textarea.value = "";
        setDraftValidation(null);
      // The abandoned attempt no longer owns the composer: it is not busy,
      // even though that attempt's late result has not arrived yet.
      setComposerBusy(false);
      refreshAnchor();
      _refreshComposerSubmitState();
    });
    submit.addEventListener("click", () => postCommentFromComposer(textarea, submit, refreshAnchor, setDraftValidation));
    textarea.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (!textarea.value.trim()) { setDraftValidation("Enter a comment to send."); refreshSubmitState(); return; }
        postCommentFromComposer(textarea, submit, refreshAnchor, setDraftValidation);
      } else if (e.key === "Escape") {
        // Escape first removes a selection anchor, in every layout, keeping
        // the draft and the target it is saved to. With no anchor left, the
        // desktop rail keeps native editable Escape (it never closes the
        // composer there); the modal overlay closes.
        const anchored = state.commentLifecycle === "anchored-composer" || !!state._pendingMarkId
          || !!(state.draftAnchor && !isPlainPageDraft(state.draftAnchor));
        if (!anchored && document.body.classList.contains("okf-panel-rail")) return;
        e.preventDefault();
        e.stopPropagation();
        state.draftBody = textarea.value;
        if (anchored) {
          transitionCommentLifecycle("concept-composer");
          refreshAnchor();
          refreshSubmitState();
          // Removing the selection changes an unconfirmed draft: no same-key
          // Retry for it any more.
          if (state.composerPost) _refreshComposerSubmitState();
          try { textarea.focus({ preventScroll: true }); } catch (err) {}
        } else {
          closePanel();
        }
      }
    });
    actions.appendChild(hint); actions.appendChild(cancel); actions.appendChild(submit);
    wrap.appendChild(anchorRow); wrap.appendChild(purpose); wrap.appendChild(ownership); wrap.appendChild(textarea); wrap.appendChild(validation); wrap.appendChild(actions);
    wrap._focus = () => { try { textarea.focus(); } catch (e) {} };
    wrap._refreshSubmit = refreshSubmitState;
    wrap._refreshAnchor = () => { refreshAnchor(); refreshSubmitState(); };
    wrap._refreshOwnership = refreshOwnership;
    wrap._setAnchorConceptLevel = () => {
      state.draftAnchor = defaultDraftAnchor();
      refreshAnchor();
      refreshSubmitState();
    };
    return wrap;
  }
  // R6-4: refresh the composer submit button's disabled state from anywhere
  // (called after commitDocument and renderCommentsPanel). Queries the live
  // DOM so it works even when the composer wrap reference is not in scope.
  // Req1/Req3: the label reflects the composer POST lifecycle outcome so a
  // background rebuild keeps the correct action name (Send / Retry send /
  // Retry refresh). Also re-applies textarea editability, aria-busy, and the
  // persistent inline status so a rebuilt composer zone stays consistent.
  function _refreshComposerSubmitState() {
    const btn = document.querySelector(".okf-composer__submit");
    const cp = state.composerPost;
    // Q4: an unconfirmed post offers the same-key "Retry send" only while the
    // draft (text, anchor, target) is still the one that was sent. A changed
    // draft is sent as a new comment under a new key, and the status says
    // the earlier one may already be posted. Restoring the sent draft
    // restores the Retry.
    const uncertain = !!(cp && cp.outcome === "uncertain");
    const retrySame = uncertain && composerDraftMatchesPost(cp);
    if (btn) {
      if (retrySame) btn.textContent = "Retry send";
      else if (cp && cp.outcome === "accepted-refresh-failed") btn.textContent = "Retry refresh";
      else btn.textContent = "Send";
      if (!CAN_COMMENT) { btn.disabled = false; }
      else if (cp && cp.outcome === "pending") { btn.disabled = true; }
      // Retry refresh only re-reads the comments; it never depends on the
      // draft, whose anchor a newer revision may have reset.
      else if (cp && cp.outcome === "accepted-refresh-failed") { btn.disabled = false; }
      else {
        const liveTextarea = document.querySelector(".okf-composer__textarea");
        const a = state.draftAnchor || {};
        if (!liveTextarea || !liveTextarea.value.trim()) { btn.disabled = true; }
        else if (a.kind !== "text-quote") { btn.disabled = !hasCommentTarget(draftTarget(state.draftAnchor)) || draftTargetIsMissing(); }
        else {
          const docRev = state.doc && state.doc.concept_content_rev;
          btn.disabled = !state.draftContentRev || state.draftContentRev !== docRev;
        }
      }
    }
    // Blocker 6: during accepted-refresh-failed recovery the cancel control
    // only dismisses/clears the recovery (the comment is already accepted),
    // so its label must say "Dismiss" — not "Cancel" (which implies aborting
    // the accepted comment).
    const cancelBtn = document.querySelector(".okf-composer__cancel");
    if (cancelBtn) cancelBtn.textContent = (cp && cp.outcome === "accepted-refresh-failed") ? "Dismiss" : "Cancel";
    // state-ownership (finding 2): the textarea is non-editable while a
    // submission is pending or while an accepted payload is in refresh-only
    // recovery (edits there would imply reposting, which the retry does not
    // do). Uncertain/rejected outcomes re-enable editing so the user can
    // revise and resend.
    _setComposerEditable(!(cp && (cp.outcome === "pending" || cp.outcome === "accepted-refresh-failed")));
    // Only the two unconfirmed-post messages are replaced here; any other
    // status (a failed verification, a missing target) stays until its own
    // owner clears it.
    if (uncertain) {
      const cs = state.composerStatus;
      if (!cs || !cs.message || cs.message === COMPOSER_UNCERTAIN_TEXT || cs.message === COMPOSER_CHANGED_TEXT) {
        state.composerStatus = { message: retrySame ? COMPOSER_UNCERTAIN_TEXT : COMPOSER_CHANGED_TEXT, tone: "warn" };
      }
    }
    _renderComposerStatus();
    // The Draft target note depends on the post state (a post still being
    // confirmed can't be moved), and every post-state change ends here —
    // including typing, which ends an unconfirmed attempt.
    const composerWrap = document.querySelector(".okf-composer");
    if (composerWrap && composerWrap._refreshOwnership) composerWrap._refreshOwnership();
  }
  // Q4: the composer's unconfirmed-post statuses (see
  // _refreshComposerSubmitState): the sent draft can be retried safely under
  // its key; a changed draft is a new comment.
  var COMPOSER_UNCERTAIN_TEXT = "Cannot confirm the comment was posted. Retrying is safe and won’t create a duplicate.";
  var COMPOSER_CHANGED_TEXT = "Your earlier comment may already be posted. Sending this changed comment posts it as a new comment; check the comments first.";
  // Q4: the page an async composer operation started on. live.js advances its
  // navigation epoch on every popstate, including an in-document Back or
  // Forward (a hash or history entry of this same page), which keeps the
  // concept and its document: the operation still settles there. The page
  // is left when the concept changed, or when the epoch moved together with
  // the document generation and no newer document of this page was
  // published meanwhile (a route replacement).
  function captureComposerPage() {
    const page = {
      concept: state.conceptId,
      navEpoch: window.okfLoomLive ? window.okfLoomLive._getNavEpoch() : 0,
      docGen: state.documentGeneration,
      publication: state.documentPublication,
    };
    page.same = function () {
      if (state.conceptId !== page.concept) return false;
      if (!window.okfLoomLive || window.okfLoomLive._getNavEpoch() === page.navEpoch) return true;
      return state.documentGeneration === page.docGen || state.documentPublication !== page.publication;
    };
    return page;
  }
  // Req1: stable high-entropy idempotency key for the composer POST. Mirrors
  // newIdempotencyKey (reanchor) but namespaced "comment:" so the two flows
  // never collide in the server's key registry.
  function newCommentKey() {
    if (crypto.randomUUID) return "comment:" + crypto.randomUUID();
    const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
    return "comment:" + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  // Req4: aria-busy on the composer while a submission is in flight, cleared
  // on every outcome. Toggles on the live .okf-composer wrap so a rebuild
  // during flight is handled by the post handler's finally.
  function setComposerBusy(busy) {
    const wrap = document.querySelector(".okf-composer");
    if (!wrap) return;
    if (busy) wrap.setAttribute("aria-busy", "true");
    else wrap.removeAttribute("aria-busy");
  }
  // finding 2: toggle the composer textarea editability on the live DOM.
  function _setComposerEditable(editable) {
    const ta = document.querySelector(".okf-composer__textarea");
    if (ta) ta.disabled = !editable;
  }
  // Visual P1: a PERSISTENT inline lifecycle status rendered just above the
  // action row (right beside Retry send / Retry refresh), so reassurance copy
  // does not depend on fading toasts. Reuses theme tokens via var() so it is
  // readable in every theme without a CSS-file change (no redesign). The
  // element is created/removed on demand; state.composerStatus is the source
  // of truth so a rebuild re-renders it via _refreshComposerSubmitState.
  function _renderComposerStatus() {
    const wrap = document.querySelector(".okf-composer");
    if (!wrap) return;
    let status = wrap.querySelector(".okf-composer__status");
    const cs = state.composerStatus;
    if (!cs || !cs.message) { if (status) status.remove(); return; }
    if (!status) {
      status = el("p", { class: "okf-composer__status", role: "status", "aria-live": "polite", "aria-atomic": "true" });
      const actions = wrap.querySelector(".okf-composer__actions");
      if (actions) wrap.insertBefore(status, actions); else wrap.appendChild(status);
    }
    status.textContent = cs.message;
    const tone = cs.tone || "info";
    status.setAttribute("data-tone", tone);
  }
  function setComposerStatus(message, tone) {
    state.composerStatus = message ? { message: message, tone: tone || "info" } : null;
    _renderComposerStatus();
  }
  // Blocker A: persistent inline status for an inline reply composer, mirroring
  // the main composer status but scoped to the replyWrap. Uses the same theme
  // tokens (warn for uncertain, error for reject) so caution/failure semantics
  // match the main composer.
  function _inlineReplyStatus(replyWrap, message, tone) {
    if (!replyWrap) return;
    var status = replyWrap.querySelector(".okf-composer__status");
    if (!message) { if (status) status.remove(); return; }
    if (!status) {
      status = el("p", { class: "okf-composer__status", role: "status", "aria-live": "polite", "aria-atomic": "true" });
      var actions = replyWrap.querySelector(".okf-composer__actions");
      if (actions) replyWrap.insertBefore(status, actions); else replyWrap.appendChild(status);
    }
    status.textContent = message;
    var t = tone || "info";
    status.setAttribute("data-tone", t);
  }
  // Visual P1: lifecycle toasts are tagged by operation identity so a terminal
  // transition (success, or a newer outcome) clears any prior composer toast —
  // the success frame must not show a stale uncertainty error beside it.
  let _composerToastHandle = null;
  function composerToast(msg, opts) {
    if (_composerToastHandle && _composerToastHandle.close) { try { _composerToastHandle.close(); } catch (e) {} }
    const handle = toast(msg, opts);
    if (handle && handle.node) handle.node.setAttribute("data-okf-composer-toast", "1");
    _composerToastHandle = handle;
    return handle;
  }
  function clearComposerToasts() {
    if (_composerToastHandle && _composerToastHandle.close) { try { _composerToastHandle.close(); } catch (e) {} }
    _composerToastHandle = null;
    // Also clear any detached composer toasts (e.g. from a prior generation).
    const region = document.querySelector(".okf-toast-region");
    if (region) region.querySelectorAll('[data-okf-composer-toast="1"]').forEach((n) => { if (n.parentNode) n.parentNode.removeChild(n); });
  }
  // keyboard-focus (finding 3): deterministic focus after each lifecycle
  // outcome. Success focus is intentional (textarea, ready for the next
  // comment). Runs after the DOM settles (rAF) so a renderCommentsPanel
  // rebuild does not steal the target.
  function _focusAfterComposerOutcome() {
    const cp = state.composerPost;
    requestAnimationFrame(() => {
      let target = null;
      const ta = document.querySelector(".okf-composer__textarea");
      if (!cp) { target = ta; }                       // success / idle → ready for next
      else if (cp.outcome === "uncertain") { target = document.querySelector('.okf-composer__submit'); }
      else if (cp.outcome === "accepted-refresh-failed") { target = document.querySelector('.okf-composer__submit'); }
      else if (cp.outcome === "rejected") { target = ta; }
      // Never steal focus from an interactive control the user already owns.
      const act = document.activeElement;
      const userFocused = act && act !== document.body && act.closest && (act.closest("button, input, textarea, select, a[href], [tabindex], [contenteditable='true']") || act.isContentEditable);
      if (target && !(userFocused && act !== target)) { try { target.focus({ preventScroll: true }); } catch (e) {} }
    });
  }
  async function postCommentFromComposer(textarea, submit, refreshAnchor, setDraftValidation) {
    // Req3/Q4: verification-only retry. A POST that was accepted but whose
    // confirming refresh failed is only ever re-checked, never re-posted,
    // even when a newer revision reset the draft's anchor meanwhile (which
    // made it look like a different draft and sent it again).
    const prior = state.composerPost;
    if (prior && prior.outcome === "accepted-refresh-failed") {
      if (setDraftValidation) setDraftValidation(null);
      return _composerVerificationRetry(submit, refreshAnchor);
    }
    const body = (textarea.value || "").trim();
    if (!body) {
      if (setDraftValidation) setDraftValidation("Enter a comment to send.");
      if (submit) submit.disabled = true;
      textarea.focus();
      return;
    }
    if (setDraftValidation) setDraftValidation(null);
    if (!CAN_COMMENT) { toast("Commenting is disabled (read-only studio).", { tone: "error" }); return; }
    if (!isConceptPage()) syncDefaultDraftTarget();
    const anchor = state.draftAnchor || defaultDraftAnchor();
    // Text-quote drafts are captured on the concept page they belong to;
    // page-level drafts carry their own target (graph preview ≠ route, and a
    // reserved index/log document has no concept at all).
    const target = anchor.kind === "text-quote"
      ? { concept: state.conceptId, document: conceptDocument(state.conceptId) }
      : draftTarget(anchor);
    const concept = target.concept;
    if (!hasCommentTarget(target)) {
      if (setDraftValidation) setDraftValidation("There is no document to comment on here. Open a document, or select a concept in the graph.");
      if (submit) submit.disabled = true;
      return;
    }
    // Enter reaches here even while Send is disabled: a draft still saved to
    // a target the server said no longer exists is not re-sent to it.
    if (draftTargetIsMissing()) {
      showDraftTargetMissing(target);
      if (submit) submit.disabled = true;
      return;
    }

    // Blocker 1: parent_id is part of same-draft identity. Blocker 7: use
    // stable deep-equal (not key-order-dependent JSON.stringify) for the anchor.
    // Q4: the same comparison labels the button (composerDraftMatchesPost).
    const parentForPost = (prior && prior.outcome === "uncertain")
      ? (prior.parent || null)            // uncertain retry reuses the stored parent
      : (state.replyTo || null);          // fresh submission uses the active reply target
    const sameDraft = sameComposerDraft(prior, body, anchor, target, parentForPost);

    // Req3/Req6: pre-POST canonical verification (fail-closed). A text-quote
    // draft must not be posted until the canonical revision is known, the
    // draft's capture rev matches the rendered doc rev, AND the held doc's
    // projection is currently valid. No network request is issued when this
    // gate blocks; the user is told the selected text could not be verified.
    if (anchor.kind === "text-quote") {
      const docRev = state.doc && state.doc.concept_content_rev;
      if (!state.draftContentRev || state.draftContentRev !== docRev) {
        const msg = "The comment was not posted because the selected text could not be verified. Reselect the exact text, then send.";
        setComposerStatus(msg, "error"); announceAnnotation(msg);
        composerToast("The selected text could not be verified. Reselect the exact text, then send.", { tone: "error", ttl: 7000 });
        if (submit) submit.disabled = true;
        return;
      }
      const projErr = projectionError(state.doc.annotation_projection, docRev, state.doc.annotation_source, state.doc.html);
      if (projErr) {
        const msg = "The comment was not posted because the selected text could not be verified: " + projErr + ".";
        setComposerStatus(msg, "error"); announceAnnotation(msg);
        composerToast("The selected text could not be verified (" + projErr + "). Reselect the exact text, then send.", { tone: "error", ttl: 7000 });
        if (submit) submit.disabled = true;
        return;
      }
    }

    // Req1: idempotency-key lifecycle. Reuse the existing key ONLY when
    // retrying the SAME draft (body + anchor + concept + parent) after an
    // UNCERTAIN outcome (network/timeout — the POST may or may not have
    // landed, so repeating the same idempotent request is safe). A confirmed
    // rejection, a draft/parent change, or a fresh submission starts a new key.
    const reuseKey = !!(prior && prior.outcome === "uncertain" && sameDraft);
    const key = reuseKey ? prior.key : newCommentKey();
    // Blocker 1: keep state.replyTo set during pending/uncertain so the visible
    // reply context survives; clear it only on verified success/cancel.
    const localId = state._pendingMarkId || ("local-" + (state.nextCommentSeq++));
    if (!reuseKey) state._pendingMarkId = null;
    const capturedDraftRev = state.draftContentRev;
    // The wire request is closed: a page comment is EXACTLY {kind:"concept"}
    // (ownership is the top-level concept); retained selection context travels
    // in `detail`. It is rebuilt deterministically from the same draft, so an
    // uncertain same-key retry replays byte-identical JSON.
    // Exactly one owner field: `concept` for a concept, `document` for a
    // reserved index/log page.
    const requestBody = Object.assign(wireTarget(target), { body: body, anchor: wireAnchor(anchor), actor: "user", detail: wireDetail(anchor), parent_id: parentForPost || null });

    // Req5: capture fences before the async POST. A late completion after
     // pagehide or A→B→A navigation must NOT mutate/announce/clear state.
     // Q4: a content-revision change on the same page does not orphan the
     // post: it still settles (uncertain, rejected, or posted) so the composer
     // never stays on "Posting comment…". Every settlement first checks that
     // this attempt is still the composer's (ownsPost): a cancelled attempt's
     // late result only updates the comments list.
    const fenceDocGen = state.documentGeneration;
    const fenceAnnotationGen = state.annotationGeneration;
    const fenceContentRev = state.doc && state.doc.concept_content_rev;
    // state-ownership (finding 2): capture the draft generation so a late
    // restore cannot overwrite a NEWER user draft. The textarea is disabled
    // while pending (primary guard); this is the belt-and-suspenders.
    const fenceDraftGen = state.draftGeneration || 0;
    // Same page (see captureComposerPage); the revision may have moved.
    const fencePage = captureComposerPage();
    function samePage() { return fencePage.same(); }
    function ownsPost() { return !!(state.composerPost && state.composerPost.key === key); }
    let settledHere = false;
    function dropOptimisticRow() {
      state.comments = state.comments.filter((c) => c.id !== localId);
      removeCommentMark(localId);
    }
    // R4 D2: this attempt was cancelled (or replaced) while in flight. The
    // composer belongs to a newer draft now; only the comments list learns
    // the late result.
    function settleAbandoned(confirmedRow) {
      dropOptimisticRow();
      if (confirmedRow) upsertComment(confirmedRow);
      renderCommentsPanel(); updateBadges(); applyCommentMarks(); rebuildMarginMarkers();
    }
    function acknowledgedRowValid(confirmed) {
      return !!(confirmed && typeof confirmed.id === "string" && confirmed.id
        && _validCanonicalRow(confirmed, concept ? { concept: concept } : { document: target.document })
        && (confirmed.concept || "") === concept
        && (isPageLevelAnchor(anchor)
          ? acknowledgedPageAnchor(confirmed.anchor, target)
          : deepEqual(confirmed.anchor, anchor))
        && (confirmed.parent_id || null) === (parentForPost || null));
    }
    function sameRevNow() {
      return !!(fenceContentRev && state.doc && state.doc.concept_content_rev === fenceContentRev);
    }
    // The page shows another revision than the one this post started on. A
    // surface without a document revision (the graph, reserved pages) never
    // counts as moved.
    function revisionMoved() {
      return state.documentGeneration !== fenceDocGen || (!!fenceContentRev && !sameRevNow());
    }
    function draftUntouched() { return (state.draftGeneration || 0) === fenceDraftGen; }
    // Restore the draft on a recoverable failure so the user can retry. NEVER
    // overwrites a newer user draft (draft-generation guard).
    function restoreDraft() {
      if (!draftUntouched()) return; // a newer draft owns the composer now
      state.draftBody = body;
      state.comments = state.comments.filter((c) => c.id !== localId);
      removeCommentMark(localId);
      renderCommentsPanel(); updateBadges(); applyCommentMarks(); rebuildMarginMarkers();
      var liveTa = document.querySelector(".okf-composer__textarea");
      if (liveTa) liveTa.value = body;
        // Page-level drafts hold no revision-keyed evidence (and the graph has
        // no document revision at all), so they are always restorable.
        if (sameRevNow() || isPageLevelAnchor(anchor)) {
          state.draftAnchor = anchor;
          state.draftContentRev = isPageLevelAnchor(anchor) ? null : (capturedDraftRev || fenceContentRev);
          // Restore the lifecycle WITHOUT resetting the draft: a page draft
          // that carries selection context is still an anchored composer.
          state.commentLifecycle = (isPageLevelAnchor(anchor) && !anchor.context) ? "concept-composer" : "anchored-composer";
          refreshAnchor();
      }
    }

    // Record the in-flight attempt (outcome 'pending').
    state.composerPost = { key: key, body: body, anchor: anchor, concept: concept, document: target.document, outcome: "pending", parent: parentForPost };

    // Optimistic UI + in-flight status (Req4). The textarea is disabled while
    // pending via _refreshComposerSubmitState (finding 2).
    const optimistic = {
      id: localId, concept, document: target.document, anchor, body, state: "open", claimed_by: null,
      resolved_activity: [], ts: new Date().toISOString(), _posting: true,
      parent_id: parentForPost,
    };
    state.comments.unshift(optimistic);
    renderCommentsPanel(); updateBadges(); rebuildMarginMarkers();
    textarea.value = ""; state.draftBody = "";
    // On the graph the composer keeps showing the concept this comment is
    // being posted to, not whatever is selected meanwhile (R4).
    state.draftAnchor = isGraphCommentSurface() ? pageDraftAnchor(target) : defaultDraftAnchor();
    state.draftContentRev = null;
    refreshAnchor();
    setComposerBusy(true);
    setComposerStatus("Posting comment…", "info");
    announceAnnotation("Posting comment…");
    submit.disabled = true; submit.textContent = "Sending…";

    // Req3: UNCERTAIN — network/timeout/abort/5xx. The POST may or may not
    // have landed. Preserve draft+anchor and the SAME key (Req1) for a safe
    // retry. Q4: an exact selection captured on a revision the page no longer
    // shows cannot be replayed (its evidence was dropped with the revision),
    // so a resend would be a different comment: say so instead of promising
    // that retrying is safe.
    function settleUncertain(toastMsg) {
      settledHere = true;
      if (!isPageLevelAnchor(anchor) && !sameRevNow()) {
        state.composerPost = null;
        restoreDraft();
        const lost = "Cannot confirm the comment was posted, and the page changed, so its text selection was lost. Check the comments before sending it again.";
        setComposerStatus(lost, "warn");
        announceAnnotation(lost);
        composerToast(lost, { tone: "warn", ttl: 8000 });
        return;
      }
      state.composerPost = { key: key, body: body, anchor: anchor, concept: concept, document: target.document, outcome: "uncertain", parent: parentForPost };
      restoreDraft();
      const msg = COMPOSER_UNCERTAIN_TEXT;
      setComposerStatus(msg, "warn");
      announceAnnotation(msg);
      composerToast(toastMsg || msg, { tone: "warn", ttl: 8000 });
    }

    try {
      let res = null, data = null, unreadable = false, fetchErr = null;
      try {
        // Q4: readJson reads the body under the POST's own deadline.
        const result = await annotationFetch("/__comment", {
          method: "POST", headers: { "Idempotency-Key": key }, body: requestBody, operationKey: "composer-post:" + key, readJson: true,
        }, "post", fenceAnnotationGen);
        res = result.response; data = result.data; unreadable = result.unreadable;
      } catch (e) { fetchErr = e; }
      // Req5: a late result after navigation/A→B→A must NOT
      // mutate/announce/clear. Drop the optimistic row; a post the composer
      // still records is over for this page.
      if (!samePage()) {
        dropOptimisticRow();
        if (ownsPost()) state.composerPost = null;
        return;
      }
      if (!ownsPost()) {
        settleAbandoned(res && res.ok && !unreadable && data.ok && acknowledgedRowValid(data.comment) ? data.comment : null);
        return;
      }
      // Q4: a 2xx whose body cannot be read (or does not say ok) may have
      // stored the comment: it is unconfirmed (same key), never a rejection.
      if (fetchErr || (res.ok && (unreadable || !data.ok))) {
        settleUncertain();
        return;
      }
      if (!res.ok || !data.ok) {
        // Req3: classify. 5xx/429 = uncertain (safe retry, SAME key); 4xx =
        // definite reject (preserved draft, NEW key on next send).
        const uncertainStatus = res.status >= 500 || res.status === 429;
        settledHere = true;
        if (uncertainStatus) {
          settleUncertain("Cannot confirm the comment was posted (" + ((typeof data.error === "string" && data.error) || (data.error && data.error.code) || ("HTTP " + res.status)) + "). Safe to retry.");
        } else if (res.status === 404 && (data.error === "target_not_found" || (data.error && data.error.code === "target_not_found"))) {
          // The concept or document was deleted. Keep the text and its target;
          // Send stays off for it until Move, Cancel, or the target returns.
          state.composerPost = { key: newCommentKey(), body: body, anchor: anchor, concept: concept, document: target.document, outcome: "rejected", parent: parentForPost };
          restoreDraft();
          showDraftTargetMissing(target);
          const msg = missingTargetMessage(target);
          announceAnnotation(msg);
          composerToast(msg, { tone: "error", ttl: 8000 });
        } else {
          state.composerPost = { key: newCommentKey(), body: body, anchor: anchor, concept: concept, document: target.document, outcome: "rejected", parent: parentForPost };
          restoreDraft();
          const reason = (typeof data.error === "string" && data.error) || (data.error && data.error.code) || ("HTTP " + res.status);
          const msg = "The comment was not posted: " + reason + ". Your text is still in the composer.";
          setComposerStatus(msg, "error");
          announceAnnotation(msg);
          composerToast(msg, { tone: "error", ttl: 8000 });
        }
        return;
      }
      // Blocker 2: POST accepted (res.ok && data.ok). Validate the success
      // envelope fail-closed: the acknowledged row must carry a non-empty id,
      // an EXACT concept match, a valid row_token, and an anchor correlated to
      // the submitted request (concept anchor → concept ref === submitted
      // concept; text-quote anchor → deep-equal the submitted anchor). A
      // malformed/cross-concept/anchor-mismatched 2xx row is NOT upserted or
      // announced; it stays accepted-unverified (→ accepted-refresh-failed).
      const confirmed = data.comment;
      const confirmedId = (confirmed && typeof confirmed.id === "string" && confirmed.id) ? confirmed.id : null;
      const envelopeOk = acknowledgedRowValid(confirmed);
      state.comments = state.comments.filter((c) => c.id !== localId);
      if (envelopeOk) {
        upsertComment(confirmed);
        if (confirmedId !== localId) renameCommentMark(localId, confirmedId);
      }
      // Require a canonical refetch/settle before any success announcement or
      // draft dismissal (Req2/Req7). Never announce "Comment posted", clear
      // the draft, or dismiss the composer if the canonical refresh fails.
      const settled = envelopeOk ? await loadComments() : false;
      // Req5: late settle after navigation — do not announce.
      if (!samePage()) { if (ownsPost()) state.composerPost = null; return; }
      // R4 D2: cancelled while the refresh ran; the list already has the row.
      if (!ownsPost()) return;
      settledHere = true;
      // finding 1: the accepted identity MUST exist in the settled canonical
      // tuple (same id + same concept) before success. A valid tuple omitting
      // the confirmed id, or a malformed/cross-concept envelope, remains
      // accepted-refresh-failed. Q4: when the page's revision moved during
      // the refresh (which that change supersedes or aborts), the accepted
      // row already listed is the confirmation. Only a revision change does
      // that: after Back/Forward within the page a failed refresh is still a
      // failed confirmation (Retry refresh).
      const listed = !!(envelopeOk && confirmedId && state.comments.some((c) => c.id === confirmedId && rowMatchesTarget(c, target)));
      const verified = listed && (settled || revisionMoved());
      if (verified) {
        state.composerPost = null;
        state.replyTo = null;  // Blocker 1: clear reply target only on verified success
        // R4 D2: a draft the user changed meanwhile (e.g. Move) is theirs.
        if (draftUntouched()) {
          state.draftBody = ""; state.draftAnchor = defaultDraftAnchor();
          state.draftContentRev = null;
          state.draftTargetMissing = "";
          resetDraftOwnershipAnnouncements();
          transitionCommentLifecycle("concept-composer");
          var liveTa2 = document.querySelector(".okf-composer__textarea");
          if (liveTa2) liveTa2.value = "";
        }
        refreshAnchor();
        setComposerStatus(null);
        clearComposerToasts(); // terminal transition clears any stale lifecycle toast
        renderCommentsPanel();
        announceAnnotation("Comment posted.");
        composerToast("Comment posted.", { tone: "success" });
      } else {
        // Req2/Req3/finding 1: accepted but canonical refresh failed OR the
        // accepted identity is absent from the settled tuple. The comment may
        // be saved server-side, but it is not verified here. Preserve the
        // draft (read-only) and offer a verification-only retry (no re-POST).
        state.composerPost = { key: key, body: body, anchor: anchor, concept: concept, document: target.document, outcome: "accepted-refresh-failed", parent: parentForPost, confirmedId: confirmedId };
        if (draftUntouched()) {
          state.draftBody = body;
          var liveTa3 = document.querySelector(".okf-composer__textarea");
          if (liveTa3) liveTa3.value = body;
          if (sameRevNow() || isPageLevelAnchor(anchor)) { state.draftAnchor = anchor; state.draftContentRev = isPageLevelAnchor(anchor) ? null : (capturedDraftRev || fenceContentRev); refreshAnchor(); }
        }
        const msg = "Comment accepted, but the page could not be refreshed to confirm it. Retry refresh re-checks without re-posting.";
        setComposerStatus(msg, "warn");
        announceAnnotation(msg);
        composerToast("Accepted but unable to refresh. Retry will re-check without re-posting.", { tone: "warn", ttl: 8000 });
        renderCommentsPanel();
      }
    } finally {
      // Q4: the composer is busy only while a post is pending. Whenever this
      // attempt is no longer pending — settled here, cancelled, or dropped by
      // navigation — clear busy, unless a newer attempt owns it.
      if (!composerPostPending()) {
        setComposerBusy(false);
        _refreshComposerSubmitState();
        if (settledHere) _focusAfterComposerOutcome();
      }
    }
  }
  // Req3: verification-only retry after a known acceptance (POST already
  // landed; only re-attempt the canonical refresh). Never re-POST. finding 1:
  // success requires the confirmed id to appear in the settled tuple.
  async function _composerVerificationRetry(submit, refreshAnchor) {
    const fenceConcept = state.conceptId;
    const fenceDocGen = state.documentGeneration;
    const fencePage = captureComposerPage();
    const cp = state.composerPost;
    const confirmedId = cp && cp.confirmedId;
    // The accepted row belongs to the comment's target concept, which on the
    // graph is not the route identity.
    const confirmedTarget = cp && !cp.concept && cp.document
      ? { concept: "", document: cp.document }
      : { concept: (cp && cp.concept) || fenceConcept, document: conceptDocument((cp && cp.concept) || fenceConcept) };
    setComposerBusy(true);
    setComposerStatus("Refreshing to confirm the accepted comment…", "info");
    // Blocker 7: the verification-only retry announces refreshing/confirming,
    // not posting (no POST is issued).
    announceAnnotation("Refreshing to confirm the accepted comment…");
    submit.disabled = true; submit.textContent = "Refreshing…";
    let settledHere = false;
    try {
      const settled = await loadComments();
      // Q4: navigation away ends the recovery for this page (an in-document
      // Back/Forward does not: see captureComposerPage); Dismiss/Cancel or a
      // newer attempt owns the composer now.
      if (!fencePage.same()) {
        if (state.composerPost === cp) state.composerPost = null;
        return;
      }
      if (state.composerPost !== cp) return;
      settledHere = true;
      // finding 1: only verified presence of the confirmed id in the settled
      // tuple completes success; otherwise stay accepted-refresh-failed. Q4:
      // a revision change that supersedes the refresh leaves the listed
      // accepted row as the confirmation; Back/Forward within the page does
      // not.
      const listed = !!(confirmedId && state.comments.some((c) => c.id === confirmedId && rowMatchesTarget(c, confirmedTarget)));
      const verified = listed && (settled || state.documentGeneration !== fenceDocGen);
      if (verified) {
        state.composerPost = null;
        state.replyTo = null;  // Blocker 1: clear reply target on verified success
        state.draftBody = ""; state.draftAnchor = defaultDraftAnchor();
        state.draftContentRev = null;
        resetDraftOwnershipAnnouncements();
        var liveTa = document.querySelector(".okf-composer__textarea");
        if (liveTa) liveTa.value = "";
        refreshAnchor();
        setComposerStatus(null);
        clearComposerToasts();
        renderCommentsPanel();
        announceAnnotation("Comment posted.");
        composerToast("Comment posted.", { tone: "success" });
      } else {
        const msg = "Comment accepted, but the page could not be refreshed to confirm it. Retry refresh re-checks without re-posting.";
        setComposerStatus(msg, "warn");
        announceAnnotation(msg);
        composerToast("Accepted but unable to refresh. Retry will re-check without re-posting.", { tone: "warn", ttl: 8000 });
      }
    } finally {
      if (!composerPostPending()) { setComposerBusy(false); _refreshComposerSubmitState(); if (settledHere) _focusAfterComposerOutcome(); }
    }
  }

  // --- margin markers ---------------------------------------------------
  // iter1 CRI-002/CRI-014: markers now anchor to the commented <mark>
  // element (not the section heading), so the indicator sits beside the
  // exact text the user pointed at. Stale comments (mark not found after a
  // patch) get no marker. Resolved comments collapse to a small stub.
  function rebuildMarginMarkers() {
    const article = $("article.okf-page__main");
    if (!article) return;
    let rail = $(".okf-comment-rail", article);
    if (!CAN_COMMENT || !isConceptPage()) { if (rail) rail.remove(); return; }
    if (!rail) {
      // The article needs relative positioning for the absolute rail.
      const cs = getComputedStyle(article);
      if (cs.position === "static") article.style.position = "relative";
      rail = el("div", { class: "okf-comment-rail", "aria-hidden": "true" });
      article.appendChild(rail);
    }
    rail.innerHTML = "";
    const mine = state.comments.filter((c) => c.concept === state.conceptId);
    if (!mine.length) return;
    const body = $(".okf-page__body") || article;
    const placed = []; // {top, comment} to stack overlapping markers
    mine.forEach((c) => {
      const marks = $$('.okf-comment-mark[data-comment-id="' + cssEscape(c.id) + '"]', body);
      if (!marks.length) return; // stale or not-yet-applied: no marker
      // Use the first mark's vertical position; stack if it overlaps a prior.
      const first = marks[0];
      const top = (first.offsetTop != null)
        ? first.offsetTop
        : (first.getBoundingClientRect().top - article.getBoundingClientRect().top + article.scrollTop);
      // Stack: nudge down if within 22px of a prior marker.
      let adjusted = top;
      for (let attempt = 0; attempt < 8; attempt++) {
        const clash = placed.some((p) => Math.abs(p.top - adjusted) < 22);
        if (!clash) break;
        adjusted += 20;
      }
      placed.push({ top: adjusted, comment: c });
      const isResolved = c.state === "resolved";
      // iter2 CRI2-010: the marker is a real <button> (keyboard-focusable,
      // click opens the comment) but had only a visual title=, so screen
      // readers announced an empty button. Give it an accessible name that
      // conveys what it's on + its lifecycle state. Comments in this model
      // are user-authored (the agent claims/resolves), so the actor is the
      // user; claimed_by (the agent) surfaces in the state chip + panel.
      const markerQuote = c.anchor && !isPageLevelAnchor(c.anchor) ? (c.anchor.exact || c.anchor.ref) : "";
      const sel = markerQuote ? ("\u201c" + markerQuote + "\u201d") : ("\u201c" + c.body.slice(0, 60) + "\u201d");
      const ariaLabel = "Comment by user on " + sel + ", " + (c.state || "open");
      const marker = el("button", {
        type: "button",
        class: "okf-comment-marker" + (isResolved ? " okf-comment-marker--stub" : ""),
        "data-state": c.state || "open",
        "data-comment-id": c.id,
        "aria-label": ariaLabel,
        title: (isResolved ? "Resolved: " : "Comment on ") + sel,
        style: { top: adjusted + "px" },
      });
      if (!isResolved) marker.textContent = "•";
      marker.addEventListener("click", () => {
        const jumped = jumpToCommentMark(c.id);
        if (!jumped) openPanel("comments"); else openPanel("comments");
      });
      rail.appendChild(marker);
    });
  }

  // --- comments panel render -------------------------------------------
  //
  // Comments panel structure. The panel is split into three persistent zones that
  // live inside panelBody for the lifetime of one "open" session:
  //
  //   .okf-comment-composer-section  — composer (or read-only notice)
  //   .okf-comment-toolbar-wrap      — sort / time / archive / expand-all
  //   .okf-comment-list-wrap         — the threaded list (2-level cap)
  //
  // Each render mutates ONE zone's innerHTML at a time. The composer zone
  // is left untouched when the user is typing in it (preserveComposer
  // guard below) so a live SSE rebuild NEVER blurs the textarea or wipes
  // the in-progress draft. The toolbar + list always rebuild — their
  // state lives in state.commentView / state.comments, never in DOM. Scroll
  // position is saved + restored around the list rebuild so the user
  // isn't yanked back to the top when a new comment arrives.
  //
  // Threading: 2 levels max (root → reply → reply-to-reply). Anything
  // deeper is rendered flat at level 2 (buildCommentTree /
  // effectiveLevel). Orphans (parent_id pointing at a comment not in the
  // current set) are hoisted to roots. Cycles are guarded.
  //
  // State: view prefs (sort / timeFilter / showArchived / expanded /
  // allExpanded) live in state.commentView, persisted to localStorage under
  // "okf:commentView" so they survive a refresh.

  // localStorage key for commentView. Inlined into load/save (rather than
  // a shared var) so the state literal at the top of the IIFE can call
  // loadCommentView() at init time without depending on a var-declaration
  // hoisting order: a top-level `var KEY = "..."` would still be undefined
  // when the state object literal runs.
  var COMMENT_VIEW_KEY = "okf:commentView";
  function loadCommentView() {
    var defaults = {
      sort: "newest",        // newest | status | updated
      timeFilter: "all",     // today | 7days | all
      showArchived: false,
      scope: "page",         // page | all — which documents' threads are listed
      expanded: {},          // { commentId: true/false }
      allExpanded: true,     // global default for threads w/o an override
    };
    try {
      var raw = localStorage.getItem("okf:commentView");
      if (!raw) return Object.assign({}, defaults);
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return Object.assign({}, defaults);
      return Object.assign({}, defaults, parsed, {
        expanded: Object.assign({}, parsed.expanded || {}),
      });
    } catch (e) {
      return Object.assign({}, defaults);
    }
  }
  function saveCommentView() {
    try {
      localStorage.setItem("okf:commentView", JSON.stringify(state.commentView));
    } catch (e) { /* localStorage may be unavailable (private mode, quota); non-fatal */ }
  }

  // Status rank for "sort by status" — open first, then claimed, then
  // resolved, then dismissed, then archived. Anything unknown sorts last.
  // NOTE: use nullish check, not `|| 5` — open's rank IS 0 (falsy), so
  // `0 || 5` would incorrectly fall through to 5 and bury open comments
  // at the bottom of a status sort.
  function commentStatusRank(s) {
    // Archived is no longer a state value (separate track).
    var v = ({ open: 0, claimed: 1, resolved: 2, dismissed: 3 })[s];
    return v === undefined ? 4 : v;
  }
  function sortRoots(list, mode) {
    var arr = list.slice();
    if (mode === "status") {
      arr.sort(function (a, b) {
        var ra = commentStatusRank(a.state), rb = commentStatusRank(b.state);
        if (ra !== rb) return ra - rb;
        return byTsDesc(a, b);
      });
    } else {
      // "newest" and "updated" both fall back to ts-desc. The directives
      // feed is append-only with last-write-wins, so the visible ts on a
      // root already reflects its latest activity (state transition OR new
      // reply post, both of which append a record). The two sorts are
      // distinct menu options because future schema work may split
      // created_ts from updated_ts; the client code is identical today.
      arr.sort(byTsDesc);
    }
    return arr;
  }
  // Epoch-ms within the time-filter window. today = last 24h, 7days = last
  // 7d. Sliding window in UTC epoch ms — no DST / timezone sensitivity
  // (the cutoff is "now - window", independent of the user's zone).
  function commentInTimeWindow(c, mode) {
    if (!mode || mode === "all") return true;
    if (!c.ts) return false;
    var t = Date.parse(c.ts);
    if (isNaN(t)) return false;
    var cutoff = Date.now() - (mode === "today" ? 24 * 3600 * 1000 : 7 * 24 * 3600 * 1000);
    return t >= cutoff;
  }

  // Build the 2-level-capped thread tree. Returns:
  //   roots              — array of effective-level-0 comments (sorted ts-desc)
  //   directChildren(id) — level-1 replies (children of a root)
  //   level2Of(id)       — flat list of all transitive descendants of a
  //                        level-1 reply, rendered at level 2
  //   byId               — id → comment lookup
  function buildCommentTree(comments) {
    var byId = {};
    comments.forEach(function (c) { if (c && c.id) byId[c.id] = c; });
    var byParent = {};
    comments.forEach(function (c) {
      if (!c || !c.parent_id) return;
      if (!byParent[c.parent_id]) byParent[c.parent_id] = [];
      byParent[c.parent_id].push(c);
    });
    function effectiveLevel(comment) {
      var lvl = 0;
      var guard = {};
      var cur = comment;
      // Walk parent chain until we hit a root, a missing parent, or a
      // cycle. Cap at 2 (rendered depth); guard set defends against
      // cycles in malformed data.
      while (cur && cur.parent_id && byId[cur.parent_id]) {
        if (guard[cur.id]) break;
        guard[cur.id] = true;
        lvl++;
        cur = byId[cur.parent_id];
        if (lvl >= 2) break; // 2-level cap; deeper renders as level 2
      }
      return lvl;
    }
    var roots = comments.filter(function (c) { return effectiveLevel(c) === 0; });
    roots = roots.slice().sort(byTsDesc);
    function directChildren(id) {
      return (byParent[id] || []).slice().sort(byTsDesc);
    }
    // Flat BFS over all descendants of a level-1 reply. The `seen` set
    // guards against cycles; everything collected renders at level 2.
    function level2Of(id) {
      var out = [];
      var stack = (byParent[id] || []).slice();
      var seen = {};
      while (stack.length) {
        var c = stack.shift();
        if (!c || seen[c.id]) continue;
        seen[c.id] = true;
        out.push(c);
        var kids = byParent[c.id] || [];
        for (var i = 0; i < kids.length; i++) stack.push(kids[i]);
      }
      return out.sort(byTsDesc);
    }
    // All transitive descendants of a root (for the time-filter "any
    // activity in this thread" rule).
    function threadHasRecentActivity(root, mode) {
      if (commentInTimeWindow(root, mode)) return true;
      var stack = (byParent[root.id] || []).slice();
      var seen = {};
      while (stack.length) {
        var c = stack.shift();
        if (!c || seen[c.id]) continue;
        seen[c.id] = true;
        if (commentInTimeWindow(c, mode)) return true;
        var kids = byParent[c.id] || [];
        for (var i = 0; i < kids.length; i++) stack.push(kids[i]);
      }
      return false;
    }
    return {
      roots: roots, byId: byId, byParent: byParent,
      directChildren: directChildren, level2Of: level2Of,
      threadHasRecentActivity: threadHasRecentActivity,
    };
  }

  // Expand/collapse. Per-comment overrides live in commentView.expanded
  // and take precedence over the global allExpanded default. Toggling a
  // single thread writes only that one id; "expand/collapse all" resets
  // the per-comment overrides and flips the global default.
  function isCommentExpanded(commentId) {
    if (Object.prototype.hasOwnProperty.call(state.commentView.expanded, commentId)) {
      return !!state.commentView.expanded[commentId];
    }
    return !!state.commentView.allExpanded;
  }
  function setCommentExpanded(commentId, expanded) {
    state.commentView.expanded[commentId] = !!expanded;
    saveCommentView();
  }
  function toggleCommentExpand(commentId) {
    setCommentExpanded(commentId, !isCommentExpanded(commentId));
    renderCommentsPanel();
  }
  function toggleAllExpand() {
    state.commentView.allExpanded = !state.commentView.allExpanded;
    state.commentView.expanded = {};
    saveCommentView();
    renderCommentsPanel();
  }

  // Identity of the comment data the list last rendered: a row's id, write
  // token and state. It tells a data refresh (a post, the agent's claim or
  // resolution) apart from a view change (expand, sort, filters, archive).
  var _renderedCommentsSignature = null;
  function commentsRenderSignature(rows) {
    return (rows || []).map(function (c) {
      return c ? [c.id, c.row_token || "", c.updated_at || c.ts || "", c.state || "",
        c.archived ? 1 : 0, c._posting ? 1 : 0].join("\u0001") : "";
    }).join("\u0002");
  }

    function renderCommentsPanel() {
    if (state.openPanel !== "comments") return;
      var body = panelBodyEl();
      if (!body) return;
      if (!state.commentsReady && (!state.comments || !state.comments.length)) {
        body.innerHTML = "";
        body.appendChild(dataStateNode("comments", "Comments"));
        return;
      }
      // List refreshes are canonical and may reorder/remove cards. Preserve a
      // stable focus identity rather than leaving keyboard focus on <body>.
      var priorActive = document.activeElement;
      var priorCard = priorActive && priorActive.closest ? priorActive.closest(".okf-comment") : null;
      var priorCommentId = priorCard ? priorCard.id.replace(/^comment-/, "") : "";
      var priorFocusKey = priorActive && priorActive.getAttribute ? priorActive.getAttribute("data-focus-key") : "";
      var priorCardIndex = priorCard && priorCard.parentNode ? Array.prototype.indexOf.call(priorCard.parentNode.children, priorCard) : -1;
      // A reader who scrolled to the end of the list is watching its last
      // card, and live data grows that card (the agent's claim, then its DONE
      // summary and reply). Restoring the old absolute offset left the grown
      // card cut off below the fold, so a data refresh keeps that reader at
      // the end. Measured before any zone is rebuilt. View changes keep the
      // exact offset, and so does a reader anywhere above the end.
      var commentsSignature = commentsRenderSignature(state.comments);
      var followListEnd = body.scrollTop > 0
        && body.scrollHeight - body.clientHeight - body.scrollTop <= 2
        && _renderedCommentsSignature !== null
        && commentsSignature !== _renderedCommentsSignature;
      _renderedCommentsSignature = commentsSignature;

    // Resolve or create the three persistent zones. On the first render
    // of an open session they don't exist yet; create + append them. On
    // subsequent renders (SSE, user toggles, etc.) they persist, so the
    // composer textarea node survives across rebuilds.
    var composerZone = body.querySelector(".okf-comment-composer-section");
    var toolbarZone = body.querySelector(".okf-comment-toolbar-wrap");
    var listZone = body.querySelector(".okf-comment-list-wrap");
    var initial = !(composerZone && toolbarZone && listZone);
    if (initial) {
      body.innerHTML = "";
      composerZone = el("div", { class: "okf-panel__section okf-comment-composer-section" });
      toolbarZone = el("div", { class: "okf-comment-toolbar-wrap" });
      listZone = el("div", { class: "okf-comment-list-wrap" });
      body.appendChild(composerZone);
      body.appendChild(toolbarZone);
      body.appendChild(listZone);
    }
    var existingDataNotice = body.querySelector(".okf-panel-state");
    if (existingDataNotice) existingDataNotice.remove();
    if (!state.commentsReady) body.insertBefore(dataStateNode("comments", "Comments"), composerZone);

    // Preserve the composer zone if the user is interacting with it. The
    // textarea is never removed from the DOM during a typing session, so
    // focus + selection + draft are all retained byte-for-byte.
    var ta = composerZone.querySelector(".okf-composer__textarea");
    var active = document.activeElement;
    var preserveComposer = !initial && ta &&
      (ta === active || (ta.value && ta.value.trim().length > 0));
      if (!preserveComposer) {
        var composerScroll = body.scrollTop;
        composerZone.innerHTML = "";
        if (CAN_COMMENT) {
        appendComposerContents(composerZone, body);
      } else {
        composerZone.appendChild(el("p", {
          class: "okf-empty okf-composer__readonly",
          text: "Commenting is disabled: this Studio session is read-only. Existing comments remain readable.",
        }));
      }
      body.scrollTop = composerScroll;
    }
    // R6-4: keep the composer submit gate in sync with the current doc rev on
    // every panel render (covers both the freshly-built and preserved cases).
    _refreshComposerSubmitState();
    // Availability sits with the composer but outside its preserved subtree, so
    // it stays current without ever touching a draft or its focus.
    if (CAN_COMMENT) {
      var availabilityLine = composerZone.querySelector(".okf-agent-availability");
      if (!availabilityLine) {
        availabilityLine = el("p", { class: "okf-agent-availability" });
        composerZone.appendChild(availabilityLine);
      }
      refreshAgentAvailability();
    }

    // Rebuild the toolbar + list, EXCEPT when the user is mid-
    // reply in an inline composer. The inline composer lives inside
    // listZone; rebuilding would destroy its textarea, losing the draft
    // and focus. Skip the whole list rebuild in that case — the next
    // SSE update (after the user submits or cancels) will catch up.
    // The toolbar zone has no user-input elements, so it's safe to
    // always rebuild.
    var listScroll = body.scrollTop;
    toolbarZone.innerHTML = "";
    toolbarZone.appendChild(buildCommentToolbar());
    var inlineBusy = _inlineReplyBusy();
    if (!inlineBusy) {
      listZone.innerHTML = "";
      listZone.appendChild(buildCommentList());
      // F3: reopen inline editors owned by the memory draft store when the
      // list was rebuilt — a panel replacement destroyed their previous DOM.
      _restoreOwnedInlineDrafts();
    }
      body.scrollTop = followListEnd ? body.scrollHeight - body.clientHeight : listScroll;
      // Cancel-race fix: if closeReanchorEditor set pendingFocusCommentId,
      // restore focus to the Re-anchor button by stable comment-id lookup
      // AFTER the list rebuild. This runs synchronously inside
      // renderCommentsPanel, so even a background loadComments-triggered
      // render cannot lose focus.
      if (state.pendingFocusCommentId) {
        _restorePendingFocus();
      } else if (priorCommentId && (!priorActive || !priorActive.isConnected) && !state.annotationEditor) {
        // R5: never run the deferred background focus-restoration while the
        // re-anchor editor is open. When openReanchorEditor rebuilds the card
        // the previously-focused Re-anchor button is disconnected, which would
        // otherwise queue requestAnimationFrame(chevron.focus()) (the chevron
        // is the first focusable control in the card). That deferred focus can
        // fire AFTER the editor places its own focus (the instruction, or a
        // later "Use selected text" focus move) and steal it to the chevron,
        // so the next Enter toggles the thread, rebuilds the card, destroys the
        // open editor and leaves Save anchor disabled. The editor manages its
        // own focus on open/capture/save/close; background loadComments only
        // calls renderCommentsPanel when the editor is already closed, so this
        // restoration still runs for genuine background rebuilds.
        var restoredCard = document.getElementById("comment-" + priorCommentId);
        var restored = restoredCard && priorFocusKey ? restoredCard.querySelector('[data-focus-key="' + cssEscape(priorFocusKey) + '"]') : null;
        if (!restored && restoredCard) restored = restoredCard.querySelector("button, [tabindex]");
        if (!restored) {
          var cards = $$(".okf-comment", listZone);
          var nearest = cards[Math.min(Math.max(priorCardIndex, 0), Math.max(cards.length - 1, 0))];
          restored = nearest && nearest.querySelector("button, [tabindex]");
          if (!restored) restored = panelTitle || body;
        }
        if (restored) requestAnimationFrame(function () { try { restored.focus({ preventScroll: true }); } catch (e) {} });
      }
      // F10: an exact-identity reveal owns focus last — it is an explicit
      // navigation act, so it outranks the passive background restores above.
      _consumePendingCommentReveal();
  }

  // F10: consume a pending comment reveal after the panel's canonical rows
  // are committed. Reconciles ONLY the visibility prefs that actually
  // exclude the target (archive toggle, page scope, time window, thread
  // collapse), then scrolls to, pulses, and focuses the real card — or
  // renders a truthful terminal note when the id is not in the retained
  // records. Kept pending while canonical comments are still loading.
  function _consumePendingCommentReveal() {
    var id = state.pendingCommentReveal;
    if (!id) return;
    if (!state.commentsReady) return; // still loading; keep the request pending
    var row = null;
    for (var i = 0; i < state.comments.length; i++) {
      if (String(state.comments[i].id) === id) { row = state.comments[i]; break; }
    }
    if (!row) {
      state.pendingCommentReveal = null;
      _showCommentRevealNote("That comment isn't available in the retained comments. It may have been deleted.");
      return;
    }
    var tree = buildCommentTree(state.comments);
    // Walk to the thread root: visibility filters apply at the root level.
    var root = row;
    var guard = 0;
    while (root && root.parent_id && tree.byId[root.parent_id] && guard < 10) {
      root = tree.byId[root.parent_id];
      guard++;
    }
    var changed = false;
    if (root && root.archived && !state.commentView.showArchived) {
      state.commentView.showArchived = true;
      changed = true;
    }
    var scopeTarget = commentScopeTarget();
    if (root && commentScope() === "page" && scopeTarget && !rowMatchesTarget(root, scopeTarget)) {
      state.commentView.scope = "all";
      changed = true;
    }
    if (root && !tree.threadHasRecentActivity(root, state.commentView.timeFilter)) {
      state.commentView.timeFilter = "all";
      changed = true;
    }
    // F10: collapse is a per-thread pref keyed to the ROOT. Reveal must
    // expand the target's thread whenever it is collapsed — whether the
    // target is the root itself or a reply under it — or a collapsed thread
    // would hide exactly the card the link is pointing at.
    if (root && !isCommentExpanded(root.id)) {
      setCommentExpanded(root.id, true);
      changed = true;
    }
    state.pendingCommentReveal = null;
    if (changed) {
      saveCommentView();
      renderCommentsPanel();
    }
    // F10: openPanel focuses the panel title AFTER render (the transition
    // path), so the reveal's focus move is deferred to the next frame — the
    // same discipline as the changes-panel reveal — so the card, not the
    // title, owns focus when the reveal opened the panel.
    requestAnimationFrame(function () {
      var card = document.getElementById("comment-" + id);
      if (!card) {
        // The list rebuild can be skipped while an inline composer holds a
        // draft; do not fake success — say the row cannot be shown right now.
        _showCommentRevealNote("That comment is saved but cannot be shown right now.");
        return;
      }
      card.setAttribute("tabindex", "-1");
      card.scrollIntoView({ block: "center", behavior: REDUCED_MOTION ? "auto" : "smooth" });
      card.classList.remove("okf-comment--pulse");
      void card.offsetWidth;
      card.classList.add("okf-comment--pulse");
      try { card.focus({ preventScroll: true }); } catch (e) {}
    });
  }

  function _showCommentRevealNote(message) {
    var listZone = document.querySelector(".okf-comment-list-wrap");
    if (!listZone) return;
    listZone.insertBefore(revealUnavailableNode(message), listZone.firstChild);
    var note = listZone.querySelector(".okf-panel-reveal-note");
    if (!note) return;
    // Deferred: openPanel's post-render title focus must not steal the
    // terminal explanation's focus when the reveal opened the panel.
    requestAnimationFrame(function () { try { note.focus(); } catch (e) {} });
  }

  // Returns true if there's an inline reply composer (the per-card
  // textarea created by buildCommentActions' Reply button) that is
  // currently focused OR contains non-empty draft text. Used to skip the
  // comment-list rebuild during renderCommentsPanel so a live update can
  // never destroy a draft the user is actively typing.
  //
  // Mirrors the top-level composer guard at line ~1440. Both guards exist
  // because the two composers live in different DOM zones (composerZone
  // vs listZone) with different rebuild semantics.
  function _inlineReplyBusy() {
    var inline = document.querySelector(".okf-inline-reply textarea");
    if (!inline) return false;
    var active = document.activeElement;
    if (inline === active) return true;
    var v = (inline.value || "").trim();
    return v.length > 0;
  }

  // Build the composer block (h3 + reply context + composerNode). Honors
  // the openPanel({focusComposer:true}) flag exactly once — the flag is
  // consumed on first build so an SSE rebuild mid-typing can't steal focus.
  function appendComposerContents(zone, body) {
    var replyContext = "";
    if (state.replyTo) {
      var parent = state.comments.find(function (c) { return c.id === state.replyTo; });
      if (parent) replyContext = "Replying to: " + (parent.body || "").slice(0, 60);
    }
    var composer = composerNode();
    if (replyContext) {
      var ctx = el("div", { class: "okf-composer__reply-context", text: replyContext });
      composer.insertBefore(ctx, composer.firstChild);
    }
    zone.appendChild(el("h3", {
      class: "okf-panel__section-title",
      text: replyContext ? "Reply" : "New comment",
    }));
    zone.appendChild(composer);
    if (composer._focus && body && body._focusComposer) {
      body._focusComposer = false; // consume: only the openPanel opener focuses
      setTimeout(function () { try { composer._focus(); } catch (e) {} }, 30);
    }
  }

  // Toolbar: sort + time-filter selects on the left, "show archived" +
  // "expand/collapse all" toggles on the right. Each control writes back
  // to state.commentView, persists, and re-renders.
  function buildCommentToolbar() {
    var tb = el("div", {
      class: "okf-comment-toolbar",
      role: "region",
      "aria-label": "Comment view controls",
    });
    var g1 = el("div", { class: "okf-comment-toolbar__group" });
    var sortSel = el("select", {
      class: "okf-comment-toolbar__select",
      "aria-label": "Sort comments",
      title: "Sort order",
    });
    [["newest", "Newest"], ["status", "Status"], ["updated", "Updated"]].forEach(function (opt) {
      var o = el("option", { value: opt[0], text: opt[1] });
      if (state.commentView.sort === opt[0]) o.selected = true;
      sortSel.appendChild(o);
    });
    sortSel.addEventListener("change", function () {
      state.commentView.sort = sortSel.value;
      saveCommentView();
      renderCommentsPanel();
    });
    g1.appendChild(sortSel);

    var timeSel = el("select", {
      class: "okf-comment-toolbar__select",
      "aria-label": "Filter comments by time",
      title: "Time window",
    });
    [["all", "All time"], ["today", "Today"], ["7days", "Last 7 days"]].forEach(function (opt) {
      var o = el("option", { value: opt[0], text: opt[1] });
      if (state.commentView.timeFilter === opt[0]) o.selected = true;
      timeSel.appendChild(o);
    });
    timeSel.addEventListener("change", function () {
      state.commentView.timeFilter = timeSel.value;
      saveCommentView();
      renderCommentsPanel();
    });
    g1.appendChild(timeSel);

    // Scope is offered only where "this page" is a real document target; the
    // graph with nothing selected has no page, so it always lists everything.
    if (commentScopeTarget()) {
      var scopeSel = el("select", {
        class: "okf-comment-toolbar__select",
        "aria-label": "Show comments from",
        title: "Which pages' comments are listed",
      });
      [["page", "This " + commentScopeNoun()], ["all", "All pages"]].forEach(function (opt) {
        var o = el("option", { value: opt[0], text: opt[1] });
        if (commentScope() === opt[0]) o.selected = true;
        scopeSel.appendChild(o);
      });
      scopeSel.addEventListener("change", function () {
        state.commentView.scope = scopeSel.value === "all" ? "all" : "page";
        saveCommentView();
        renderCommentsPanel();
        var again = document.querySelector('.okf-comment-toolbar__select[aria-label="Show comments from"]');
        if (again) again.focus({ preventScroll: true });
      });
      g1.appendChild(scopeSel);
    }
    tb.appendChild(g1);

    var g2 = el("div", { class: "okf-comment-toolbar__group" });
    var archBtn = el("button", {
      type: "button",
      class: "okf-studiobtn okf-comment-toolbar__toggle",
      "aria-pressed": state.commentView.showArchived ? "true" : "false",
      text: state.commentView.showArchived ? "Hide archived" : "Show archived",
    });
    archBtn.addEventListener("click", function () {
      state.commentView.showArchived = !state.commentView.showArchived;
      saveCommentView();
      renderCommentsPanel();
    });
    g2.appendChild(archBtn);

    var allOpen = state.commentView.allExpanded;
    var expBtn = el("button", {
      type: "button",
      class: "okf-studiobtn okf-comment-toolbar__toggle",
      text: allOpen ? "Collapse all" : "Expand all",
      title: allOpen ? "Collapse every thread" : "Expand every thread",
    });
    expBtn.addEventListener("click", toggleAllExpand);
    g2.appendChild(expBtn);
    tb.appendChild(g2);

    return tb;
  }

  // The document the list is scoped to, or null when this surface has no page
  // of its own (graph with nothing selected, synthesized root index).
  function commentScopeTarget() {
    var target = defaultCommentTarget();
    return hasCommentTarget(target) ? target : null;
  }
  function commentScopeNoun() { return document.getElementById("detail-body") ? "concept" : "page"; }
  function commentScope() {
    return commentScopeTarget() && state.commentView.scope !== "all" ? "page" : "all";
  }
  // Real live destination for a row's document. Reserved rows (indexes, logs)
  // carry only a document path; concept rows link to the concept page.
  function commentTargetHref(row) {
    if (row.concept) return "/" + row.concept.split("/").map(encodeURIComponent).join("/");
    var doc = rowDocument(row);
    if (!validDocumentPath(doc)) return "";
    var path = /(^|\/)index\.md$/.test(doc) ? doc.slice(0, -"index.md".length) : doc;
    return "/" + path.split("/").map(encodeURIComponent).join("/");
  }
  function commentTargetRow(row) {
    var here = commentScopeTarget();
    var label = row.concept || rowDocument(row);
    if (!label) return null;
    var line = el("p", { class: "okf-comment__target" });
    if (here && rowMatchesTarget(row, here)) {
      line.appendChild(document.createTextNode("On this " + commentScopeNoun()));
      return line;
    }
    line.appendChild(document.createTextNode("On "));
    var href = commentTargetHref(row);
    if (href) line.appendChild(el("a", { href: href, class: "okf-comment__target-link", text: label, dataset: { focusKey: "target" } }));
    else line.appendChild(el("code", { text: label }));
    return line;
  }

  function buildCommentList() {
    var wrap = el("div", { class: "okf-comment-list" });
    var tree = buildCommentTree(state.comments);
    var scopeTarget = commentScope() === "page" ? commentScopeTarget() : null;
    var elsewhere = 0;

    // Apply scope + archive + time filters at the ROOT level. Replies inherit
    // visibility from their root (collapsing a thread hides everything;
    // expanding shows all descendants regardless of their own ts).
    var visibleRoots = tree.roots.filter(function (root) {
      if (scopeTarget && !rowMatchesTarget(root, scopeTarget)) {
        if (!root.archived) elsewhere++;
        return false;
      }
      if (root.archived && !state.commentView.showArchived) return false;
      if (!tree.threadHasRecentActivity(root, state.commentView.timeFilter)) return false;
      return true;
    });
    var sorted = sortRoots(visibleRoots, state.commentView.sort);

    if (sorted.length === 0) {
      if (!state.comments || state.comments.length === 0) {
        wrap.appendChild(emptyState(
          "No comments yet",
          CAN_COMMENT ? "Comments save requests and discussion about this page for readers and its agent."
            : "Comments save requests and discussion, but this Studio is read-only.",
          CAN_COMMENT ? "Select text to comment" : "Learn about Studio authority",
          CAN_COMMENT ? beginCommentSelection : "/tutorials/live_studio_basics"
        ));
      } else if (scopeTarget && elsewhere) {
        wrap.appendChild(el("p", { class: "okf-empty", text: "No comments on this " + commentScopeNoun() + " yet." }));
        wrap.appendChild(otherPagesNotice(elsewhere));
      } else {
        wrap.appendChild(el("p", { class: "okf-empty", text: commentListEmptyMessage() }));
      }
      return wrap;
    }
    sorted.forEach(function (root) {
      wrap.appendChild(renderRootCard(root, tree));
    });
    if (scopeTarget && elsewhere) wrap.appendChild(otherPagesNotice(elsewhere));
    return wrap;
  }

  function otherPagesNotice(count) {
    var note = el("p", { class: "okf-comment-list__elsewhere" });
    note.appendChild(document.createTextNode(count + (count === 1 ? " thread is" : " threads are") + " on other pages. "));
    var show = el("button", { type: "button", class: "okf-comment__action", text: "Show all pages" });
    show.addEventListener("click", function () {
      state.commentView.scope = "all";
      saveCommentView();
      renderCommentsPanel();
      var sel = document.querySelector('.okf-comment-toolbar__select[aria-label="Show comments from"]');
      if (sel) sel.focus({ preventScroll: true });
    });
    note.appendChild(show);
    return note;
  }

  function beginCommentSelection() {
    const target = document.querySelector(".okf-page__body") || document.querySelector("main");
    closePanel();
    if (target) {
      if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
      try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); }
    }
    toast("Select text, then choose Comment.", { tone: "success" });
  }

  function commentListEmptyMessage() {
    // Blocker 6: while a submission's outcome is uncertain, do not show the
    // bare "No comments yet" — the comment may be awaiting confirmation.
    if ((!state.comments || state.comments.length === 0)) {
      var cp = state.composerPost;
      if (cp && cp.outcome === "uncertain") {
        return "A comment is awaiting confirmation. Retrying is safe and won’t create a duplicate.";
      }
      return "No comments yet. Select text and click Comment, or use the composer above.";
    }
    var hasArchived = state.comments.some(function (c) { return !!c.archived; });
    if (!state.commentView.showArchived && hasArchived &&
        state.comments.every(function (c) { return !!c.archived; })) {
      return "All comments are archived. Toggle “Show archived” to see them.";
    }
    if (state.commentView.timeFilter !== "all") {
      return "No comments in this time window. Try a wider filter.";
    }
    return "No comments match the current filter.";
  }

  function renderRootCard(root, tree) {
    var expanded = isCommentExpanded(root.id);
    // Gather all replies for the collapsed preview + thread-resolved check.
    var allReplies = [];
    tree.directChildren(root.id).forEach(function (l1) {
      allReplies.push(l1);
      tree.level2Of(l1.id).forEach(function (l2) { allReplies.push(l2); });
    });
    var threadResolved = root.state === "resolved" &&
      allReplies.every(function (r) { return r.state === "resolved"; });
    var card = commentCard(root, 0, {
      expanded: expanded,
      replies: allReplies,
      isRoot: true,
      threadResolved: threadResolved,
    });
    if (!expanded) return card;
    var l1 = tree.directChildren(root.id);
    if (!l1.length) return card;
    var children = el("div", { class: "okf-comment__children" });
    l1.forEach(function (reply) {
      children.appendChild(commentCard(reply, 1, { isRoot: false }));
      var l2 = tree.level2Of(reply.id);
      if (l2.length) {
        var inner = el("div", { class: "okf-comment__children" });
        l2.forEach(function (r2) { inner.appendChild(commentCard(r2, 2, { isRoot: false })); });
        children.appendChild(inner);
      }
    });
    card.appendChild(children);
    return card;
  }

  // X2: the server-owned provenance marker of a locally imported portable
  // note. Rows arrive from /__comments and merge by Object.assign, so the
  // marker survives SSE refreshes and lifecycle updates without a cache.
  function portableImportMarker(c) {
    var m = c && c.portable_import;
    if (!m || typeof m !== "object" || Array.isArray(m) || m.schema !== 1) return null;
    return m;
  }
  function portableImportDetails(m) {
    var wrap = el("div", { class: "okf-comment__import" });
    var claims = el("p", { class: "okf-comment__import-claims" });
    // Each claim carries its own "(unverified)" label, the pattern proposal
    // review uses for author names. The wording avoids "claimed by", which is
    // the card's lifecycle state for an agent that took the comment.
    var parts = [];
    if (typeof m.claimed_author === "string" && m.claimed_author) {
      parts.push(document.createTextNode("Note author (unverified): " + m.claimed_author));
    }
    if (typeof m.claimed_at === "string" && m.claimed_at) {
      var when = new Date(m.claimed_at);
      var shown = m.claimed_at;
      if (!isNaN(when.getTime())) {
        try { shown = when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); } catch (e) { shown = when.toISOString(); }
      }
      var span = el("span", {});
      span.appendChild(document.createTextNode("Note time (unverified): "));
      span.appendChild(el("time", { dateTime: m.claimed_at, text: shown }));
      parts.push(span);
    }
    if (!parts.length) parts.push(document.createTextNode("Origin unverified"));
    parts.forEach(function (part, i) {
      if (i) claims.appendChild(document.createTextNode(" \u00b7 "));
      claims.appendChild(part);
    });
    wrap.appendChild(claims);
    if (typeof m.quote === "string" && m.quote) {
      // R3: the quote is what the note's file claims; nothing verified it
      // against the page and it was never pinned, so the label says so.
      wrap.appendChild(el("p", { class: "okf-comment__import-quote-label",
        text: "Quote in the note (unverified) \u2014 not highlighted on the page" }));
      wrap.appendChild(el("blockquote", { class: "okf-comment__import-quote", text: m.quote }));
    }
    return wrap;
  }

  // level: 0 (root), 1 (reply), 2 (reply-to-reply, or anything deeper that
  // has been capped to level 2 by buildCommentTree). opts.expanded only
  // applies to roots; replies never collapse independently.
  function commentCard(c, level, opts) {
    level = level || 0;
    opts = opts || {};
    var expanded = Object.prototype.hasOwnProperty.call(opts, "expanded")
      ? !!opts.expanded
      : true;
    var isRoot = level === 0;
    var collapsed = isRoot && !expanded;

    var cls = "okf-comment okf-comment--level-" + level;
    if (level > 0) cls += " okf-comment--reply"; // compatibility hook for extensions
    if (collapsed) cls += " okf-comment--collapsed";

    var card = el("div", {
      class: cls,
      id: "comment-" + (c.id || ""),
      dataset: { state: c.state || "open", level: String(level) },
    });

    // Header row: chevron (root only) + state chip + anchor + meta.
    var header = el("div", { class: "okf-comment__header" });
    if (isRoot) {
      var chev = el("button", {
        type: "button",
        class: "okf-comment__chevron",
        "aria-expanded": expanded ? "true" : "false",
        "aria-controls": "comment-" + (c.id || ""),
        "aria-label": (expanded ? "Collapse" : "Expand") + " thread" +
          (c.body ? ": " + c.body.slice(0, 60) : ""),
        title: expanded ? "Collapse thread" : "Expand thread",
      });
      chev.addEventListener("click", function () { toggleCommentExpand(c.id); });
      header.appendChild(chev);
    }
    // "claimed" names who holds it: a claim is the one durable sign an agent
    // picked the thread up, as opposed to presence, which is only a flag.
    header.appendChild(el("span", { class: "okf-comment__state",
      text: c.state === "claimed" ? "claimed by " + (c.claimed_by || "agent") : (c.state || "open") }));
    if (c.archived) {
      header.appendChild(el("span", { class: "okf-comment__archived-chip", text: "archived" }));
    }
    // X2: provenance comes ONLY from the server-owned top-level marker on a
    // canonical /__comments row; `detail.*` is never read as a claim.
    var portableImport = portableImportMarker(c);
    if (portableImport) {
      header.appendChild(el("span", { class: "okf-comment__import-chip", text: "Imported portable note" }));
    }
    // Show created-at + updated-at timestamps. Server stamps updated_at on
    // every transition (claim/resolve/dismiss/reopen/archive/unarchive/
    // reply). If they differ, show both; otherwise show just created.
    var createdTs = c.ts || "";
    var updatedTs = c.updated_at || c.ts || "";
    if (createdTs && updatedTs && createdTs !== updatedTs) {
      header.appendChild(el("span", { class: "okf-comment__ts", text: "created " + fmtTime(createdTs) }));
      header.appendChild(el("span", { class: "okf-comment__ts okf-comment__ts--updated", text: "updated " + fmtTime(updatedTs) }));
    } else {
      header.appendChild(el("span", { class: "okf-comment__ts", text: fmtTime(createdTs) }));
    }
    if (c._posting) header.appendChild(el("span", { class: "okf-comment__posting", text: "posting…" }));
      card.appendChild(header);
      // Threads are listed across documents, so a root always says where it
      // lives; replies share their parent's target by contract.
      if (isRoot && commentScope() === "all") {
        var targetRow = commentTargetRow(c);
        if (targetRow) card.appendChild(targetRow);
      }
      card.appendChild(buildAnchorHealthRow(c));

    if (collapsed) {
      // Collapsed preview: show short body for EVERY comment in the thread
      // (root + all replies), not just the root. Each preview is a one-line
      // ellipsis with the actor prefix.
      //
      // Two summary fields, shown in priority order:
      //   - ``summary`` (resolve-time "what the agent did") — explicit,
      //     takes precedence when set (typically on agent replies).
      //   - ``request_summary`` (claim-time "what was asked" OR auto-
      //     derived from body) — always present, used for user comments
      //     and as a fallback.
      // The preview reads as a status board: each agent reply shows its
      // "done" tag; each user comment shows its "ask" tag.
      var allInThread = [c];
      if (opts.replies) {
        opts.replies.forEach(function (r) { allInThread.push(r); });
      }
      allInThread.forEach(function (tc) {
        var bodyText = (tc.body || "").trim();
        var doneSummary = (tc.summary || "").trim();
        var askSummary = (tc.request_summary || "").trim();
        var who = isAgentActor(tc.actor) ? (tc.actor === "agent" ? "Agent" : tc.actor) : "You";
        var line = el("div", { class: "okf-comment__preview" });
        line.appendChild(el("span", { class: "okf-comment__preview-who", text: who + ": " }));
        if (doneSummary) {
          // Agent's "done" tag — accent + italic.
          var dSlice = doneSummary.length > 100 ? doneSummary.slice(0, 100) + "\u2026" : doneSummary;
          line.appendChild(el("span", { class: "okf-comment__preview-summary", text: dSlice }));
        } else if (askSummary) {
          // Auto-derived or claim-set "ask" tag — muted + italic.
          var aSlice = askSummary.length > 100 ? askSummary.slice(0, 100) + "\u2026" : askSummary;
          line.appendChild(el("span", { class: "okf-comment__preview-ask", text: aSlice }));
        } else {
          var previewText = bodyText.length > 100 ? bodyText.slice(0, 100) + "\u2026" : bodyText;
          line.appendChild(document.createTextNode(previewText));
        }
        card.appendChild(line);
      });
      return card;
    }

    // Expanded body + ask/done summaries + reply + resolved details + actions.
    if (portableImport) card.appendChild(portableImportDetails(portableImport));
    card.appendChild(el("div", { class: "okf-comment__body", text: c.body || "" }));
    // Two summary chips side by side.
    //   - request_summary: short of what was ASKED (auto-derived from body
    //     or set explicitly via claim --summary). Labelled "asked".
    //   - summary: short of what the agent DID (set via resolve --summary).
    //     Labelled "done".
    // Both are shown above the longform reply so the card reads as
    // ask → done → detail at a glance.
    if (c.request_summary) {
      var askEl = el("div", { class: "okf-comment__summary okf-comment__summary--ask" });
      askEl.appendChild(el("span", { class: "okf-comment__summary-label", text: "asked: " }));
      askEl.appendChild(document.createTextNode(c.request_summary));
      card.appendChild(askEl);
    }
    if (c.summary) {
      var doneEl = el("div", { class: "okf-comment__summary okf-comment__summary--done" });
      doneEl.appendChild(el("span", { class: "okf-comment__summary-label", text: "done: " }));
      doneEl.appendChild(document.createTextNode(c.summary));
      card.appendChild(doneEl);
    }
    if (c.reply) {
      var rep = el("div", { class: "okf-comment__reply" });
      rep.appendChild(el("strong", { text: "Agent: " }));
      rep.appendChild(document.createTextNode(c.reply));
      card.appendChild(rep);
    }
    if (c.resolved_activity && c.resolved_activity.length) {
      var det = el("details");
      det.appendChild(el("summary", {
        text: "Resolved by " + c.resolved_activity.length + " change(s). Jump",
      }));
      c.resolved_activity.forEach(function (aid) {
        var a = el("a", { href: "#", text: "change #" + shortId(String(aid)) });
        a.addEventListener("click", function (e) {
          e.preventDefault();
          jumpToActivity(String(aid));
        });
        det.appendChild(el("div", {}, [a]));
      });
      card.appendChild(det);
    }

      // Action row. Hidden in read-only studios and while anchor recovery is
      // active so two independent mutation workflows cannot compete. A
      // comment-only session gets Reply alone (see buildCommentActions).
      // A row that is still being sent has only a local id: it is not yet a
      // thread the server knows, so it offers no Reply or lifecycle action.
      if (CAN_COMMENT && !c._posting && !(state.annotationEditor && state.annotationEditor.commentId === c.id)) {
      card.appendChild(buildCommentActions(c, {
        isRoot: opts.isRoot,
        threadResolved: opts.threadResolved,
      }));
    }
    return card;
  }

    function anchorHealthView(c) {
      const r = c.anchor_resolution || {};
      // A page-level row has no text span to resolve. The server still stamps
      // a resolver verdict on it ("invalid": nothing to project); reading that
      // as a broken anchor would be false, so page rows report what they are.
      if (isPageLevelAnchor(c.anchor)) {
        return { status: "page", label: c.parent_id ? "Reply" : "Page comment", action: "" };
      }
      const legacy = c.anchor && c.anchor.kind === "text";
      if (legacy) return { status: "legacy", label: "Legacy anchor", action: r.status === "resolved" && !c._projectionUnavailable ? "Jump" : EDIT ? "Re-anchor" : "" };
      // Blocker 6: a comment acknowledged by the composer whose canonical
      // refresh has not yet confirmed it must not read "Checking anchor" — it
      // is accepted-but-unverified, a terminal recovery state.
      var cp = state.composerPost;
      if (cp && cp.outcome === "accepted-refresh-failed" && cp.confirmedId && cp.confirmedId === c.id && !r.status) {
        return { status: "accepted-refresh", label: "Accepted · Refresh needed", action: "" };
      }
      if (!r.status) return { status: "checking", label: "Checking anchor", action: "" };
      if (r.status === "resolved" && c._projectionUnavailable) return { status: "invalid", label: "Anchor unavailable", action: EDIT ? "Re-anchor" : "Details" };
      if (r.status === "resolved") return { status: "resolved", label: "Attached", action: "Jump" };
      if (r.status === "stale") return { status: "stale", label: "Text not found", action: EDIT ? "Re-anchor" : "" };
      if (r.status === "ambiguous") return { status: "ambiguous", label: "Multiple matches", action: EDIT ? "Choose text" : "" };
      return { status: "invalid", label: "Anchor unavailable", action: EDIT ? "Re-anchor" : "Details" };
    }
    function buildAnchorHealthRow(c) {
      const view = anchorHealthView(c);
      const editing = state.annotationEditor && state.annotationEditor.commentId === c.id;
      // P1-8: the row reports ANCHOR HEALTH (where the quoted text resolved),
      // which is distinct from the thread lifecycle state (open/claimed/
      // resolved) shown by the comment state chip. The aria-label makes that
      // boundary explicit so "Attached" can never be read as the thread being
      // resolved. "Attached" stays the persistent, explicit resolved label.
      const row = el("section", { class: "okf-anchor-row" + (editing ? " okf-anchor-row--recovering" : ""), dataset: { anchorStatus: view.status }, "aria-label": "Anchor health" });
      const status = el("span", { class: "okf-anchor-row__status" });
      status.appendChild(el("span", { class: "okf-anchor-row__status-icon", "aria-hidden": "true", text: view.status === "resolved" ? "✓" : view.status === "ambiguous" ? "?" : view.status === "stale" ? "×" : view.status === "legacy" ? "L" : view.status === "page" ? "\u00b6" : "!" }));
      status.appendChild(document.createTextNode(view.label));
      row.appendChild(status);
      const pageContext = view.status === "page" ? storedSelectionContext(c) : null;
      // R3: the two healthy states say what they mean too, so "Attached"
      // and "Page comment" are never bare labels. A reply shares its
      // thread's target and gets no precision line of its own.
      const cause = view.status === "stale" ? "The quoted text is no longer present."
        : view.status === "ambiguous" ? "More than one matching occurrence remains."
        : view.status === "invalid" ? "The stored evidence cannot be projected safely."
        : view.status === "resolved" ? "Exact annotation: pinned to this passage and highlighted."
        : view.status === "page" && !pageContext && !c.parent_id ? "About the whole page; no text is highlighted."
        : "";
      if (cause && !editing) row.appendChild(el("span", { class: "okf-anchor-row__cause", text: cause }));
      if (pageContext) {
        row.appendChild(el("span", { class: "okf-anchor-row__cause", text: "Selected in the " + (pageContext.surface === "graph" ? "graph preview" : pageContext.surface === "source" ? "Source view" : "document") + " \u2014 quoted here, not attached to exact text." }));
      }
      const quote = view.status === "page"
        ? (pageContext ? pageContext.quote + (pageContext.truncated ? "\u2026" : "") : "")
        : (c.anchor && (c.anchor.exact || c.anchor.ref));
      if (quote) row.appendChild(el("q", { class: "okf-anchor-row__quote", text: quote }));
      if (view.action && !editing) {
        // Recovery stays outlined: several broken anchors can share the
        // drawer, whose composer owns the one filled primary action.
        // Jump (resolved) and Details remain quiet tertiary actions.
        const isRecovery = view.action !== "Jump" && view.action !== "Details";
        const actionCls = (isRecovery ? "okf-btn" : "okf-comment__action") + " okf-anchor-row__action";
        const action = el("button", { type: "button", class: actionCls, text: view.action, dataset: { focusKey: "anchor-" + view.action.toLowerCase().replace(/\s+/g, "-") } });
        action.addEventListener("click", function () {
          if (view.action === "Jump") {
            if (!jumpToCommentMark(c.id)) announceAnnotation("The verified anchor is not available in this view.");
          } else if (view.action === "Details") {
            announceAnnotation("Anchor unavailable. " + ((c.anchor_resolution && c.anchor_resolution.reason) || "The stored anchor is invalid."));
          } else openReanchorEditor(c, action);
        });
        row.appendChild(action);
      }
      if (editing) row.appendChild(buildReanchorEditor(c));
      return row;
    }
    function announceAnnotation(message) {
      state.annotationAnnouncement = message;
      let region = document.getElementById("okf-annotation-status");
      if (!region) {
        region = el("div", { id: "okf-annotation-status", class: "okf-sr-only", role: "status", "aria-live": "polite", "aria-atomic": "true" });
        document.body.appendChild(region);
      }
      region.textContent = ""; requestAnimationFrame(function () { region.textContent = message; });
    }
    function openReanchorEditor(c, initiator) {
      if (!EDIT) return;
      delete document.body.dataset.okfSelectionReady;
      state.annotationGeneration++;
      state.annotationEditor = { commentId: c.id, mode: "selecting", anchor: null, message: "", initiator: initiator, initiatorLabel: initiator.textContent, idempotencyKey: null, requestBody: null, generation: state.annotationGeneration };
      syncSelectionModeBanner();
      renderCommentsPanel();
      // Focus the instruction synchronously on open (a11y: announce it to
      // keyboard/SR users). This used to be deferred via requestAnimationFrame,
      // but a deferred focus can fire AFTER the user has already moved focus to
      // an interactive control (e.g. "Use selected text") and steal it back,
      // diverting the next Enter/Space away from that control so the capture
      // never runs and Save stays disabled. Synchronous focus happens before
      // any subsequent focus move, so it can never clobber the user's choice.
      // Guard: never override an interactive control that is already focused
      // (defensive — renderCommentsPanel just rebuilt the card, so focus is on
      // body here, but stay robust to re-entrant opens).
      (function focusReanchorInstruction() {
        const active = document.activeElement;
        const alreadyInteractive = active && active !== document.body && (
          active.closest("button, input, textarea, select, a[href], [tabindex], [contenteditable='true']") || active.isContentEditable);
        if (alreadyInteractive) return;
        const instruction = document.getElementById("okf-reanchor-instruction-" + c.id);
        if (instruction) { instruction.setAttribute("tabindex", "-1"); try { instruction.focus(); } catch (e) {} }
      })();
    }
    function closeReanchorEditor(restoreFocus) {
      const editor = state.annotationEditor;
      abortAnnotationOperations("editor-cancelled");
      state.annotationGeneration++;
      state.annotationEditor = null;
      // P1-3: if the drawer was peeked open (Back to document), restore it
      // before re-rendering so the panel is visible again for focus restore.
      _dismissReanchorPeek();
      syncSelectionModeBanner();
      // Set pending focus target so any subsequent renderCommentsPanel
      // (from background loadComments/rerender) restores focus atomically.
      if (restoreFocus && editor) state.pendingFocusCommentId = editor.commentId;
      renderCommentsPanel();
      _restorePendingFocus();
    }
    function _restorePendingFocus() {
      if (!state.pendingFocusCommentId) return;
      var cid = state.pendingFocusCommentId;
      state.pendingFocusCommentId = null;
      var card = document.getElementById("comment-" + cid);
      if (card) {
        var controls = $$(".okf-anchor-row__action", card);
        // R4: After cancel, a concurrent refresh may have resolved the anchor
        // (Re-anchor → Jump). Focus the first available action; if none
        // exist (e.g. legacy/no anchor row), fall back to the card's first
        // focusable control, then the panel heading.
        var target = controls.find(function (n) { return n.textContent === "Re-anchor"; })
          || controls.find(function (n) { return n.textContent === "Jump"; })
          || controls[0];
        if (target) { target.focus(); return; }
        // No anchor-row action — try any focusable control in the card.
        var cardFocusable = card.querySelector("button, [tabindex], a[href]");
        if (cardFocusable) { cardFocusable.focus(); return; }
      }
      // Fallback: panel heading
      var heading = document.querySelector(".okf-comments-panel h2, .okf-panel__title");
      if (heading) heading.focus();
    }
      function syncSelectionModeBanner() {
        const body = $(".okf-page__body");
        if (!body) return;
        let banner = document.getElementById("okf-annotation-selection-banner");
        if (!state.annotationEditor) {
          if (banner) banner.remove();
          body.classList.remove("okf-page__body--selecting-anchor");
          // R3-6: disconnect resize observer when editor closes.
          if (_bannerResizeObserver) { _bannerResizeObserver.disconnect(); _bannerResizeObserver = null; }
          return;
        }
        body.classList.add("okf-page__body--selecting-anchor");
        var created = false;
        if (!banner) {
          banner = el("div", { id: "okf-annotation-selection-banner", class: "okf-selection-mode-banner", role: "status" });
          body.parentNode.insertBefore(banner, body);
          created = true;
        }
        _updateBannerGeometry(banner);
        // P1-5: the in-document banner is a DIRECTIONAL bridge that marks the
        // document as the active selection surface — it does NOT repeat the
        // step-by-step instruction already shown in the reanchor editor. This
        // avoids the duplicated "select the exact text…" guidance that appeared
        // in both the banner and the editor.
        banner.textContent = state.annotationEditor.mode === "conflict"
          ? "This document is active. The previous selection was cleared — refresh, then select the new text here."
          : "↓ This document is active for anchor selection. Choose the exact text here, then return to the comment.";
        // P1-5/P1-6 (forced-colors rereview): when the banner first appears it
        // must be VISIBLE. In rail mode the narrowed document can push it below
        // the fold, so bring it into view on creation. scrollIntoView(block:
        // "start") would land it at viewport y=0 — directly behind the sticky
        // topbar — so nudge it down past the topbar so the instruction text is
        // not occluded. Instant behavior keeps this deterministic and
        // reduced-motion-safe.
        if (created) {
          try {
            banner.scrollIntoView({ block: "start", behavior: "auto" });
            var topbar = document.querySelector(".okf-topbar");
            var topbarH = topbar ? topbar.getBoundingClientRect().height : 0;
            if (topbarH > 0) window.scrollBy(0, -(topbarH + 8));
          } catch (e) {}
        }
        // R3-6: observe panel width changes continuously while editor is open.
        if (!_bannerResizeObserver && typeof ResizeObserver !== "undefined") {
          _bannerResizeObserver = new ResizeObserver(function () {
            var b = document.getElementById("okf-annotation-selection-banner");
            if (b) _updateBannerGeometry(b);
          });
          _bannerResizeObserver.observe(panelShell);
        }
      }
      var _bannerResizeObserver = null;
      function _updateBannerGeometry(banner) {
        var panelW = panelShell.offsetWidth || 0;
        // P1-1: in rail mode the body padding-right already reserves the
        // drawer column, so the banner must NOT add another right margin —
        // doing so would double-reserve the drawer width and crush the banner
        // text into a narrow, very tall column.
        if (document.body.classList.contains("okf-panel-rail")) { banner.style.marginRight = ""; return; }
        // At mobile widths (panel is full-width), don't add margin.
        if (panelW >= window.innerWidth - 4) banner.style.marginRight = "";
        else banner.style.marginRight = panelW + "px";
      }
      // P1-3: narrow "Back to document" / "Select text" path. When the drawer
      // overlays the whole page (mobile) and the user is selecting a new
      // anchor, _peekReanchorToDocument hides the drawer+scrim so the document
      // is visible for text selection, and shows a floating peek bar. Editor
      // state (state.annotationEditor) and the in-progress selection are
      // preserved; _restoreReanchorFromPeek brings the drawer back. Wide
      // viewports already show the document beside the drawer (rail mode), so
      // peeking is a no-op there.
      function _railModeActive() {
        return !panelShell.hidden && document.body.classList.contains("okf-panel-rail");
      }
      function _selectInDocument() {
        // P1-3: in rail mode the document is already visible beside the
        // drawer, so just move focus/scroll to it. Otherwise peek the drawer
        // open so the (covered) document becomes visible for text selection.
        if (_railModeActive()) {
          var body = $(".okf-page__body");
          if (body) {
            body.setAttribute("tabindex", "-1");
            try { body.focus(); } catch (e) {}
            try { body.scrollIntoView({ block: "start" }); } catch (e) {}
          }
          return;
        }
        _peekReanchorToDocument();
      }
      function _peekReanchorToDocument() {
        var editor = state.annotationEditor;
        if (!editor || editor.mode === "saving") return;
        if (_railModeActive()) return; // document already visible beside drawer
        state.annotationPeeked = true;
        panelShell.hidden = true;
        panelOverlay.hidden = true;
        document.body.classList.remove("okf-panel-rail");
        var title = (state.doc && state.doc.title) || state.conceptId || "the document";
        reanchorPeekText.innerHTML = "";
        reanchorPeekText.appendChild(el("strong", { text: "Selecting anchor" }));
        reanchorPeekText.appendChild(document.createTextNode(" in " + title + "."));
        reanchorPeek.hidden = false;
        // Move focus onto the document so keyboard users can select with the
        // caret/Shift+arrows; the peek bar's Return control is one Tab away.
        var body = $(".okf-page__body");
        if (body) { body.setAttribute("tabindex", "-1"); try { body.focus({ preventScroll: true }); } catch (e) {} }
      }
      function _restoreReanchorFromPeek() {
        if (!state.annotationPeeked) return;
        state.annotationPeeked = false;
        reanchorPeek.hidden = true;
        if (state.openPanel) { panelShell.hidden = false; panelOverlay.hidden = false; }
        _syncPanelRail();
        renderCommentsPanel();
        // Focus the capturing control so keyboard flow resumes inside the
        // restored drawer; fall back to the reanchor instruction.
        requestAnimationFrame(function () {
          var useBtn = document.querySelector(".okf-reanchor .okf-reanchor__actions button:not([disabled])");
          var instr = document.querySelector(".okf-reanchor__instruction");
          var target = useBtn || instr;
          if (target) { target.setAttribute("tabindex", "-1"); try { target.focus({ preventScroll: true }); } catch (e) {} }
        });
      }
      function _syncReanchorPeek() {
        // If the editor closed or we crossed into rail mode, drop the peek.
        if (!state.annotationEditor) { _dismissReanchorPeek(); return; }
        if (state.annotationPeeked && _railModeActive()) { _restoreReanchorFromPeek(); }
      }
      function _dismissReanchorPeek() {
        if (!state.annotationPeeked) return;
        state.annotationPeeked = false;
        reanchorPeek.hidden = true;
        if (state.openPanel) { panelShell.hidden = false; panelOverlay.hidden = false; }
      }
      function buildReanchorEditor(c) {
        const editor = state.annotationEditor;
        const wrap = el("section", { class: "okf-reanchor", dataset: { mode: editor.mode }, "aria-label": "Re-anchor editor for comment " + shortId(c.id), role: "group" });
      // P1-3/P1-7: persistent document context bar so the user always knows
      // which document the anchor belongs to — critical at 320px where the
      // drawer covers the page. Falls back to the concept id if no title.
      const ctxTitle = (state.doc && state.doc.title) || state.conceptId || "this document";
      const context = el("p", { class: "okf-reanchor__context" });
      context.appendChild(el("span", { class: "okf-reanchor__context-label", text: "Document" }));
      context.appendChild(el("span", { class: "okf-reanchor__context-title", text: ctxTitle }));
      wrap.appendChild(context);
      const instruction = el("p", { id: "okf-reanchor-instruction-" + c.id, class: "okf-reanchor__instruction", dataset: { focusKey: "reanchor-instruction" }, text: "Select the exact text this comment refers to in the document." });
      wrap.appendChild(instruction);
      if (editor.mode !== "conflict") {
        const preview = el("div", { class: "okf-reanchor__preview", "aria-live": "polite" });
        preview.appendChild(el("span", { class: "okf-reanchor__preview-label", text: "Selection" }));
        preview.appendChild(el("q", { text: editor.anchor ? editor.anchor.exact : "No valid selection yet" }));
        wrap.appendChild(preview);
      } else {
        // P1-4: conflict separates refresh (reload the changed document) from
        // reselect (choose new text). The cause states the document changed;
        // the primary action reloads then resumes selecting.
        wrap.appendChild(el("p", { class: "okf-reanchor__cause", text: "The document changed, so your previous selection is no longer valid. Refresh to load the latest text, then choose the new anchor." }));
      }
      if (editor.message) wrap.appendChild(el("div", { class: "okf-reanchor__error", role: "alert", tabindex: "-1", text: editor.message }));
      const actions = el("div", { class: "okf-reanchor__actions" });
      if (editor.mode === "conflict") {
        const refresh = el("button", { type: "button", class: "okf-studiobtn okf-studiobtn--primary", text: "Refresh and reselect" });
        refresh.addEventListener("click", function () { refreshAfterConflict(c); }); actions.appendChild(refresh);
      } else if (editor.mode === "save_failed") {
        wrap.appendChild(el("p", { class: "okf-reanchor__cause", text: "Retry is safe: Studio reuses the same request key and cannot append the anchor twice." }));
        const retry = el("button", { type: "button", class: "okf-studiobtn okf-studiobtn--primary", text: "Retry save" });
        retry.addEventListener("click", function () { saveReanchor(c, true); }); actions.appendChild(retry);
        const edit = el("button", { type: "button", class: "okf-studiobtn", text: "Edit selection" });
        edit.addEventListener("click", function () { editor.mode = "selecting"; editor.message = ""; editor.idempotencyKey = null; editor.requestBody = null; renderCommentsPanel(); }); actions.appendChild(edit);
      } else {
        const use = el("button", { type: "button", class: "okf-studiobtn", text: "Use selected text" });
        use.addEventListener("mousedown", function (e) { e.preventDefault(); });
        use.addEventListener("click", function () { updateReanchorSelection(c); }); actions.appendChild(use);
        // P1-3: deterministic "Back to document" / "Select text" bridge. When
        // the drawer overlays the page it peeks the drawer open so the
        // document is visible for selection; in rail mode it simply moves
        // focus/scroll to the document beside the drawer. Either way the
        // editor state is preserved and the user can return.
        const selectInDoc = el("button", { type: "button", class: "okf-studiobtn", text: "Select in document", dataset: { focusKey: "reanchor-select-in-document" } });
        selectInDoc.addEventListener("click", function () { _selectInDocument(); }); actions.appendChild(selectInDoc);
        const save = el("button", { type: "button", class: "okf-studiobtn okf-studiobtn--primary", text: editor.mode === "saving" ? "Saving…" : "Save anchor", disabled: !editor.anchor || editor.mode === "saving" });
        save.addEventListener("click", function () { saveReanchor(c, false); }); actions.appendChild(save);
      }
      const cancel = el("button", { type: "button", class: "okf-studiobtn", text: "Cancel", disabled: editor.mode === "saving" });
      cancel.addEventListener("click", function () { closeReanchorEditor(true); }); actions.appendChild(cancel);
      wrap.appendChild(actions);
      wrap.addEventListener("keydown", function (e) {
        if (e.key !== "Escape" || e.defaultPrevented) return;
        if (isEditableTarget(e.target)) return;
        e.preventDefault();
        if (editor.mode === "saving") { editor.message = "Checking the saved result…"; void reconcileReanchor(c); }
        else closeReanchorEditor(true);
      });
      return wrap;
    }
      async function updateReanchorSelection(c) {
        const editor = state.annotationEditor;
        if (!editor || editor.commentId !== c.id) return;
        try { editor.anchor = await captureCanonicalAnchor(); editor.message = ""; }
        catch (e) { editor.anchor = null; editor.message = e.message || "The selection is not valid."; }
        renderCommentsPanel();
        // R3-5: after successful capture, focus the newly enabled Save anchor
        // button. On error, focus the error summary. This MUST be synchronous,
        // not deferred via requestAnimationFrame: a deferred Save.focus() can
        // fire AFTER the user has already moved focus to Cancel (the test/auto-
        // wait returns the instant Save is enabled, then focuses Cancel), which
        // would steal focus back to Save so the next Enter triggers Save instead
        // of Cancel. Synchronous focus completes before any later focus move.
        // Guard: never override an interactive control the user already focused.
        (function focusAfterCapture() {
          var act = document.activeElement;
          var userFocusedInteractive = act && act !== document.body && (
            act.closest("button, input, textarea, select, a[href], [tabindex], [contenteditable='true']") || act.isContentEditable);
          if (editor.anchor) {
            var save = document.querySelector("#comment-" + cssEscape(c.id) + " .okf-studiobtn--primary:not(:disabled)");
            if (save && !(userFocusedInteractive && act !== save)) { try { save.focus(); } catch (e) {} }
            else if (!save) { var n = document.querySelector(".okf-reanchor__error"); if (n) { try { n.focus(); } catch (e) {} } }
          } else {
            var n = document.querySelector(".okf-reanchor__error"); if (n) { try { n.focus(); } catch (e) {} }
          }
        })();
      }
    function newIdempotencyKey() {
      if (crypto.randomUUID) return "reanchor:" + crypto.randomUUID();
      const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
      return "reanchor:" + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
    }
      async function saveReanchor(c, retry) {
      const editor = state.annotationEditor;
      if (!editor || editor.commentId !== c.id || (!editor.anchor && !retry)) return;
        if (!retry) {
          const latest = state.comments.find(function (item) { return item.id === c.id; });
          if (!latest || !latest.row_token || !state.doc) { editor.mode = "conflict"; editor.message = "Refresh the comment before saving this anchor."; renderCommentsPanel(); return; }
          editor.idempotencyKey = newIdempotencyKey();
          editor.requestBody = { comment_id: latest.id, expected_row_token: latest.row_token, expected_concept_content_rev: state.doc.concept_content_rev, anchor: JSON.parse(JSON.stringify(editor.anchor)) };
      }
      const generation = editor.generation; editor.mode = "saving"; editor.message = ""; renderCommentsPanel();
      try {
        const res = await annotationFetch("/__comment-reanchor", { method: "POST", headers: { "Idempotency-Key": editor.idempotencyKey }, body: editor.requestBody }, "post", generation);
        const data = await res.json().catch(function () { return {}; });
        if (!state.annotationEditor || state.annotationEditor.generation !== generation) return;
        const currentComment = state.comments.find(function (item) { return item.id === c.id; });
        if (!currentComment || currentComment.row_token !== editor.requestBody.expected_row_token || !state.doc || state.doc.concept_content_rev !== editor.requestBody.expected_concept_content_rev || state.conceptId !== c.concept) {
          editor.mode = "conflict"; editor.anchor = null; editor.message = "The document changed before this anchor was saved."; return;
          }
            if (res.ok && data.ok) {
              // Fetch the updated comment and doc directly rather than calling
              // loadComments() (which shares stable operation keys with SSE-
              // triggered loadComments and can be superseded under load).
              // Use unique operation keys scoped to this save.
              try {
                const [commentsRes2, docRes2, eventsRes2] = await Promise.all([
                  annotationFetch("/__comments", { headers: { Accept: "application/json" }, operationKey: "save-comments:" + generation }, "get"),
                  annotationFetch("/__data/doc?id=" + encodeURIComponent(state.conceptId), { headers: { Accept: "application/json" }, operationKey: "save-doc:" + generation }, "get"),
                  annotationFetch("/__data/events?limit=500", { headers: { Accept: "application/json" }, operationKey: "save-events:" + generation }, "get"),
                ]);
                if (!state.annotationEditor || state.annotationEditor.generation !== generation) return;
                if (!commentsRes2.ok || !docRes2.ok || !eventsRes2.ok) { editor.mode = "save_failed"; editor.message = "The save result is uncertain. Retry save safely or edit the selection."; return; }
                const commentsData2 = await commentsRes2.json();
                const doc2 = await docRes2.json();
                const eventData2 = await eventsRes2.json();
                  // R8-ATOMIC / R10 / R11 / R12: stage the COMPLETE doc+comments+
                  // events tuple and validate ALL of it (doc identity against the
                  // ACTIVE concept, projection, EVERY comment row's identity/
                  // row_token/discriminated anchor/resolution parity, AND every
                  // event row) BEFORE committing — not just the target comment.
                  // The previous code validated only the target comment, so a
                  // malformed sibling row could accompany an accepted re-anchor
                  // commit; R12 adds the authoritative events component so a
                  // commit cannot land beside a malformed/unvalidated events
                  // feed. validateStagedTuple subsumes the doc identity +
                  // projection check and rejects malformed comment/event rows.
                    var reanchorTuple = validateStagedTuple({ doc: doc2, commentsData: commentsData2, eventData: eventData2, conceptKey: state.conceptId });
                    if (reanchorTuple.skewSignal) { editor.mode = "save_failed"; editor.message = "The save succeeded but canonical state could not be reconciled."; return; }
                    var stagedFresh = reanchorTuple.comments.find(function (item) { return item.id === c.id && item.concept === state.conceptId; }) || null;
                  // R9-REANCHOR: never commit the doc without a matching validated
                  // comment carrying a row_token, anchor_resolution parity, AND
                  // the requested anchor. All four must hold BEFORE commitDocument;
                  // the previous code committed first and verified the anchor AFTER
                  // mutation, so a mismatched anchor could land on a fresh document
                  // and a missing/token-less comment could coexist with it.
                  if (!stagedFresh || !stagedFresh.row_token ||
                      !stagedFresh.anchor_resolution ||
                      stagedFresh.anchor_resolution.concept_content_rev !== doc2.concept_content_rev ||
                      !stagedFresh.anchor ||
                      JSON.stringify(stagedFresh.anchor) !== JSON.stringify(editor.requestBody.anchor)) {
                    editor.mode = "save_failed"; editor.message = "The save succeeded but canonical state could not be reconciled."; return;
                  }
                  // R10-COMMIT: REQUIRE commitDocument() to return true before
                  // comment/editor/toast mutations. A false return means DOM
                  // staging failed and rolled back; committing the comment or
                  // announcing success beside a rolled-back document is a skew.
                  if (!commitDocument(doc2, { pulse: false })) { editor.mode = "save_failed"; editor.message = "The save succeeded but canonical state could not be reconciled."; return; }
                  upsertComment(stagedFresh);
                  renderCommentsPanel(); rebuildMarginMarkers();
              } catch (fetchErr) {
                if (!state.annotationEditor || state.annotationEditor.generation !== generation) return;
                const reconciled = await reconcileReanchor(c, true);
                if (reconciled) return;
                editor.mode = "save_failed"; editor.message = "The save result is uncertain. Retry save safely or edit the selection."; return;
              }
                if (!state.annotationEditor || state.annotationEditor.generation !== generation) return;
                if (!state.doc || state.doc.concept_content_rev !== editor.requestBody.expected_concept_content_rev) { editor.mode = "conflict"; editor.anchor = null; editor.message = "The document changed while the save was confirmed."; return; }
                const refreshed = state.comments.find(function (item) { return item.id === c.id; });
                if (!refreshed || !refreshed.anchor || JSON.stringify(refreshed.anchor) !== JSON.stringify(editor.requestBody.anchor) || !refreshed.anchor_resolution || refreshed.anchor_resolution.concept_content_rev !== state.doc.concept_content_rev) {
                  editor.mode = "save_failed"; editor.message = "The save succeeded but canonical state could not be reconciled. Refresh before retrying."; return; }
            closeReanchorEditor(false); announceAnnotation("Anchor saved and attached.");
          requestAnimationFrame(function () { const jump = document.querySelector('#comment-' + cssEscape(c.id) + ' .okf-anchor-row__action'); if (jump) jump.focus(); }); return;
        }
        const code = data.error && data.error.code;
        if (code === "comment_conflict" || code === "concept_conflict" || code === "idempotency_conflict") {
          editor.mode = "conflict"; editor.anchor = null; editor.message = "The document changed before this anchor was saved.";
        } else {
          editor.mode = "save_failed"; editor.message = (data.error && data.error.message) || "The anchor could not be saved.";
        }
      } catch (e) {
        if (!state.annotationEditor || state.annotationEditor.generation !== generation) return;
        // A timeout/abort may happen after the server committed. Canonical
        // state decides before we expose Retry; never optimistically append.
        const reconciled = await reconcileReanchor(c, true);
        if (reconciled) return;
        editor.mode = "save_failed"; editor.message = "The save result is uncertain. Retry save safely or edit the selection.";
        } finally { if (state.annotationEditor && state.annotationEditor.generation === generation) { renderCommentsPanel(); requestAnimationFrame(function () { const n = document.querySelector(".okf-reanchor__error"); if (n) n.focus(); }); } }
    }
    async function reconcileReanchor(c, quiet) {
      const expected = state.annotationEditor && state.annotationEditor.requestBody;
      if (!expected) return false;
      const requestGeneration = ++state.commentsRequestGeneration;
      const conceptKey = state.conceptId;
      const documentGeneration = state.documentGeneration;
      let fresh = null;
        try {
          const responses = await Promise.all([
            annotationFetch("/__comments", { headers: { Accept: "application/json" } }, "get"),
            annotationFetch("/__data/doc?id=" + encodeURIComponent(conceptKey), { headers: { Accept: "application/json" } }, "get"),
            annotationFetch("/__data/events?limit=500", { headers: { Accept: "application/json" } }, "get"),
          ]);
          // Group 5: all three responses must be ok before committing any.
          if (!responses[0].ok || !responses[1].ok || !responses[2].ok) return false;
            const commentsData = await responses[0].json();
            const doc = await responses[1].json();
            const eventData = await responses[2].json();
            if (requestGeneration !== state.commentsRequestGeneration || conceptKey !== state.conceptId || documentGeneration !== state.documentGeneration) return false;
            // R11 / R12: validate the COMPLETE doc+comments+events tuple (doc
            // identity against the ACTIVE concept, projection, EVERY comment
            // row's identity/row_token/discriminated anchor/resolution parity,
            // AND every event row) BEFORE the target-specific checks — not just
            // the target comment. A malformed sibling comment row OR a
            // malformed/unvalidated events feed must not accompany an accepted
            // re-anchor commit. validateStagedTuple subsumes the doc identity +
            // projection guard (doc-identity-mismatch / doc-projection-invalid)
            // that previously lived here, and now requires the events component.
            var reconTuple = validateStagedTuple({ doc: doc, commentsData: commentsData, eventData: eventData, conceptKey: conceptKey });
            if (reconTuple.skewSignal) return false;
            // R8-ATOMIC / R9-REANCHOR: never commit the doc without a matching
            // validated comment carrying a row_token, anchor_resolution parity
            // against the staged doc, AND the requested anchor. All must hold
            // before commitDocument; the previous code committed the doc first
            // and only guarded on the optional anchor_resolution parity, so a
            // missing/token-less/wrong-anchor comment could coexist with a fresh
            // document.
              fresh = reconTuple.comments.find(function (item) { return item.id === c.id && item.concept === conceptKey; }) || null;
              if (!fresh || !fresh.row_token ||
                  !fresh.anchor_resolution ||
                  fresh.anchor_resolution.concept_content_rev !== doc.concept_content_rev ||
                  !fresh.anchor ||
                  JSON.stringify(fresh.anchor) !== JSON.stringify(expected.anchor)) {
                fresh = null;
              }
              if (!fresh) return false;
            // R10-COMMIT: REQUIRE commitDocument() to return true before the
            // comment/editor/toast mutations. A false return means DOM staging
            // failed and rolled back; upserting the comment or announcing
            // success beside a rolled-back document is a skew.
            if (!commitDocument(doc, { pulse: false })) { fresh = null; return false; }
          upsertComment(fresh);
      } catch (error) { fresh = null; }
      const sameIntent = fresh && expected && fresh.anchor && JSON.stringify(fresh.anchor) === JSON.stringify(expected.anchor);
      if (fresh && expected && state.annotationEditor && fresh.row_token !== expected.expected_row_token && sameIntent) {
        closeReanchorEditor(false); announceAnnotation("Anchor saved and attached.");
        requestAnimationFrame(function () { const jump = document.querySelector('#comment-' + cssEscape(c.id) + ' .okf-anchor-row__action'); if (jump) jump.focus(); });
        return true;
      }
      if (!quiet && state.annotationEditor) { state.annotationEditor.mode = "save_failed"; state.annotationEditor.message = "The save result is uncertain. Retry save safely or edit the selection."; renderCommentsPanel(); }
      return false;
    }
      async function refreshAfterConflict(c) {
          // R3-4: stage doc+comments together. On failure retain prior
          // canonical doc/comments/marks and keep editor in conflict mode.
          // On success commit atomically: null → fresh doc, fresh editor
          // generation, clear selection, remove obsolete marks, focus.
          // R5-7: capture the editor generation BEFORE the fetch. A superseding
          // editor (openReanchorEditor/closeReanchorEditor) or a navigation
          // bumps annotationGeneration. After the fetch resolves, verify the
          // editor still owns this comment+generation before committing.
            const editor = state.annotationEditor;
            const editorGen = editor ? editor.generation : null;
            const priorDoc = state.doc;
            const priorComments = state.comments.slice();
            const conceptId = state.conceptId;
            // R6-5: capture the document + annotation generation BEFORE the
            // fetch. A concurrent patch, navigation, or superseding editor
            // bumps these during the in-flight refresh. The catch path must
            // verify ALL fences before restoring prior state or scheduling
            // focus — otherwise a delayed failure from refresh A can overwrite
            // state owned by editor/navigation B.
            const priorDocGen = state.documentGeneration;
            const priorAnnotationGen = state.annotationGeneration;
            try {
              // Fetch new doc, comments, AND events without destroying current
              // state. R12: the conflict refresh is a canonical commit path, so
              // the authoritative events feed is fetched and validated alongside
              // the doc+comments — a malformed events feed must not coexist with
              // a freshly-committed conflict-resolution document.
              const docRes = await annotationFetch("/__data/doc?id=" + encodeURIComponent(state.conceptId), { headers: { Accept: "application/json" } }, "get");
              const commentsRes = await annotationFetch("/__comments", { headers: { Accept: "application/json" } }, "get");
              const eventsRes = await annotationFetch("/__data/events?limit=500", { headers: { Accept: "application/json" } }, "get");
              if (!docRes.ok || !commentsRes.ok || !eventsRes.ok) throw new Error("Refresh failed");
              const freshDoc = await docRes.json();
              const commentsData = await commentsRes.json();
              const eventData = await eventsRes.json();
              // R5-7: verify the editor still owns this comment and the
              // concept hasn't navigated away. A superseding editor aborts
              // the old refresh; do NOT commit stale state.
              if (!state.annotationEditor || state.annotationEditor.generation !== editorGen || state.annotationEditor.commentId !== c.id || state.conceptId !== conceptId) return;
              // R10 / R12: route through the SAME staged-tuple validator as
              // loadComments/reanchor. A non-array comments/events value, a
              // foreign-concept document, an invalid projection, a token-less/
              // anchor-less comment row, OR a malformed event row must NOT
              // coexist with a freshly-committed document. The previous refresh
              // adopted doc+comments without any of these checks (and even
              // skipped projection metadata); R12 adds the authoritative events
              // component. Throw on any skew so the catch path restores prior
              // state.
              var conflictTuple = validateStagedTuple({ doc: freshDoc, commentsData: commentsData, eventData: eventData, conceptKey: conceptId });
              if (conflictTuple.skewSignal) throw new Error("Refresh tuple invalid: " + conflictTuple.skewSignal);
              // R10-COMMIT: REQUIRE commitDocument() to return true before
              // bumping the annotation generation, committing comments, or
              // touching the editor. A false return means DOM staging failed
              // and rolled back; throw so the catch path restores prior state.
              // The generation bump happens AFTER a successful commit so a
              // failed commit does not fence the catch path's ownership check.
              if (!commitDocument(freshDoc, { pulse: false })) throw new Error("Refresh commitDocument staging failed");
              state.annotationGeneration++;
              state.comments = conflictTuple.comments.slice().sort(byTsDesc);
              applyCommentMarks();
              if (editor && editor.commentId === c.id) {
                editor.generation = state.annotationGeneration;
                editor.mode = "selecting";
                editor.anchor = null;
                editor.message = "";
                editor.idempotencyKey = null;
                editor.requestBody = null;
              }
              renderCommentsPanel();
              requestAnimationFrame(function () {
                var n = document.querySelector("#comment-" + cssEscape(c.id) + " .okf-reanchor__instruction");
                if (n) { n.setAttribute("tabindex", "-1"); n.focus(); }
              });
            } catch (refreshErr) {
              // R6-5: the catch path must respect the SAME ownership fences as
              // the success path. A delayed failure must NOT restore prior
              // doc/comments, rerender, or schedule focus if a newer owner
              // (superseding editor, navigation, or a concurrent doc commit)
              // took over during the in-flight refresh — otherwise it would
              // overwrite the newer owner's state. Bail silently when fenced.
              console.error("[okf-studio] conflict refresh failed", refreshErr);
              var fenced = !state.annotationEditor
                || state.annotationEditor.generation !== editorGen
                || state.annotationEditor.commentId !== c.id
                || state.conceptId !== conceptId
                || state.documentGeneration !== priorDocGen
                || state.annotationGeneration !== priorAnnotationGen;
              if (fenced) return;
              // Failure: restore prior state, keep conflict mode.
              state.doc = priorDoc;
              state.comments = priorComments;
              applyCommentMarks();
              if (editor && editor.commentId === c.id) {
                editor.message = "Could not refresh the document. Try again or select text manually.";
              }
              renderCommentsPanel();
              requestAnimationFrame(function () {
                var n = document.querySelector("#comment-" + cssEscape(c.id) + " .okf-reanchor__error");
                if (n) n.focus();
              });
            }
        }

  // ---- F3: memory-only owner for inline reply/edit drafts ---------------
  // Inline editors used to live only in panel DOM: any panel replacement
  // (Close/reopen, Comments→Changes, Back to Studio) destroyed the textarea
  // AND the uncertain-request identity (idempotency key, dispatched body,
  // parent) of an already-dispatched reply — treating a lost response or a
  // panel interruption as proof that no server commit happened, which is
  // exactly the case the idempotency design exists for. This Map is the
  // explicit owner, keyed by operation kind + the ACTUAL comment row id
  // (never DOM position). It is page-lifetime memory only: never
  // localStorage, never a backend. Explicit success and explicit cancel of
  // a non-dispatched draft clean their entry; a dispatched pending/uncertain
  // intent survives until a confirmed outcome replaces it.
  const inlineDraftOwner = new Map();
  // F3 ownership: the map key (kind:commentId) is shared by every composer
  // ever opened for that row, but a map ENTRY is owned by exactly one editor
  // session at a time. `inlineDraftRevisionSeq` mints that session identity:
  // each composer instance captures a fresh revision at open and tags every
  // entry it writes with it. An asynchronous outcome/cleanup from one session
  // may only read, settle, or delete an entry whose `rev` still equals the
  // revision the outcome was dispatched from — a replacement composer (after
  // a panel round trip) mints the next revision, so a departed editor's late
  // success/failure can never erase or corrupt the newer session's draft or
  // its dispatched intent (key/body/state).
  var inlineDraftRevisionSeq = 0;
  var INLINE_REPLY_UNCERTAIN_TEXT = "Cannot confirm the reply was posted. Retrying the same text is safe and won’t create a duplicate.";
  // Q4: an unconfirmed reply whose text was then changed is a different
  // reply; sending it cannot replay the earlier one.
  var INLINE_REPLY_CHANGED_TEXT = "Your earlier reply may already be posted. Sending this changed text posts it as a new reply; check the thread first.";
  function inlineDraftId(kind, commentId) { return kind + ":" + String(commentId || ""); }
  function inlineDraftEntry(kind, commentId, text, replyWrap, rev) {
    return {
      kind: kind, commentId: commentId, text: text || "", open: true,
      rev: rev || 0,
      state: (replyWrap && replyWrap._replyState) || "none",
      idempotencyKey: (replyWrap && replyWrap._replyKey) || null,
      dispatchedBody: (replyWrap && replyWrap._replyBody) || null,
      dispatchedParent: (replyWrap && replyWrap._replyParent) || null,
      dispatchedConcept: (replyWrap && replyWrap._replyConcept) != null ? replyWrap._replyConcept : null,
    };
  }
  function _retireOpenInlineComposer() {
    var existing = document.querySelector(".okf-inline-reply");
    if (!existing) return;
    // R9-REPLY: abort any in-flight POST owned by a prior inline composer.
    if (existing._replyAbort) { try { existing._replyAbort.abort("replaced"); } catch (e2) {} }
    // F3: the replaced composer leaves its owned draft behind. An aborted or
    // superseded dispatch stays uncertain (the request may have landed);
    // an untouched draft stays available for its next Reply/Edit open. The
    // close only touches the entry still owned by THIS composer session.
    var dkey = existing._ownedDraftId;
    if (dkey) {
      var entry = inlineDraftOwner.get(dkey);
      if (entry && (!existing._draftRev || entry.rev === existing._draftRev)) entry.open = false;
    }
    existing.remove();
  }
  // Reopen inline editors owned by the memory draft store after the comment
  // list was rebuilt (panel replacement destroys their previous DOM). Runs
  // only after a real list rebuild — the inline-busy guard above leaves an
  // intact composer (and its textarea) untouched.
  function _restoreOwnedInlineDrafts() {
    if (inlineDraftOwner.size === 0) return;
    var byId = {};
    state.comments.forEach(function (row) { if (row && row.id) byId[row.id] = row; });
    inlineDraftOwner.forEach(function (entry, dkey) {
      // Owned cleanup: an entry whose row is gone and that carries no
      // uncertain intent can never restore again — drop it instead of
      // keeping a permanent stale draft for the page life.
      if (!byId[entry.commentId] && entry.state !== "pending" && entry.state !== "uncertain") {
        inlineDraftOwner.delete(dkey);
        return;
      }
      if (!entry.open || !byId[entry.commentId]) return;
      // One inline composer at a time (product invariant); never attach twice.
      if (document.querySelector(".okf-inline-reply")) return;
      if (!document.getElementById("comment-" + entry.commentId)) { entry.open = false; return; }
      var row = byId[entry.commentId];
      if (entry.kind === "reply") {
        openInlineReplyComposer(row, { focus: false });
      } else if (!isAgentActor(row.actor) && row.state !== "resolved" && row.state !== "dismissed") {
        openInlineEditComposer(row, { focus: false });
      } else {
        entry.open = false;
      }
    });
  }

  // Open the inline reply composer for one comment row. Extracted from the
  // Reply button closure (F3) so the draft owner can restore it after a
  // panel rebuild, with identical idempotency, fencing, and abort semantics.
  function openInlineReplyComposer(c, openOpts) {
    openOpts = openOpts || {};
    var focusEditor = openOpts.focus !== false;
    // Insert an inline reply composer directly below this comment card.
    // Remove any existing inline composer first (one at a time).
    // R9-REPLY: abort any in-flight POST owned by a prior inline reply
    // before removing it, so an obsolete mutating request is cancelled
    // (not merely fenced at completion).
    _retireOpenInlineComposer();
    var card = document.getElementById("comment-" + c.id);
    if (!card) return;
    var replyWrap = el("div", { class: "okf-inline-reply" });
    // R9-REPLY: request-specific abort/supersession. Each inline reply owns
    // an AbortController aborted on composer cancel, replacement (another
    // inline reply opened), pagehide, or navigation — so an obsolete
    // mutating request is cancelled, not merely fenced at completion. The
    // route/document generation fence (stillOwns) blocks late adoption; the
    // per-composer abort additionally cancels the in-flight fetch and
    // distinguishes two same-document composers.
    var replyAbort = null;
    var onReplyPageHide = function () { try { replyAbort.abort("pagehide"); } catch (e2) {} };
    var onReplyNav = function () { try { replyAbort.abort("navigation"); } catch (e2) {} };
    var replyListenersActive = false;
    var cleanupReplyListeners = function () {
      if (!replyListenersActive) return;
      replyListenersActive = false;
      window.removeEventListener("pagehide", onReplyPageHide);
      window.removeEventListener("popstate", onReplyNav);
    };
    // Q4: (re)arms the request owner. Its listeners fire once, so an
    // in-document Back/Forward that aborted one send would leave the next
    // send (the Retry) without an owner: every send re-arms a spent one.
    function armReplyAbort() {
      if (!replyAbort || replyAbort.signal.aborted) {
        replyAbort = new AbortController();
        replyWrap._replyAbort = replyAbort;
      }
      cleanupReplyListeners();
      replyListenersActive = true;
      window.addEventListener("pagehide", onReplyPageHide, { once: true });
      window.addEventListener("popstate", onReplyNav, { once: true });
    }
    armReplyAbort();
    // F3: the draft owner keys this composer to the actual row. The session
    // revision minted here is the ownership token every async outcome and
    // cleanup from THIS composer is fenced against: a replacement composer
    // opened after a panel round trip mints the next revision, so a departed
    // editor's late callback can only settle the entry it still owns.
    var ownedDraftId = inlineDraftId("reply", c.id);
    var draftRev = ++inlineDraftRevisionSeq;
    replyWrap._ownedDraftId = ownedDraftId;
    replyWrap._draftRev = draftRev;
    // Context indicator.
    replyWrap.appendChild(el("div", {
      class: "okf-composer__reply-context",
      text: "Replying to: " + (c.body || "").slice(0, 80),
    }));
    // Inline textarea.
    var ta = el("textarea", {
      class: "okf-composer__textarea okf-inline-reply__textarea",
      rows: "2",
      placeholder: "Reply…",
      "aria-label": "Reply to comment",
    });
    replyWrap.appendChild(ta);
    // Action row.
    var row = el("div", { class: "okf-composer__actions" });
    var submit = el("button", { type: "button", class: "okf-studiobtn okf-studiobtn--primary", text: "Reply" });
    var cancel = el("button", { type: "button", class: "okf-studiobtn", text: "Cancel" });
    // F3: restore the owned draft. `text` is the current draft;
    // dispatchedBody/key/parent/concept are the DISPATCHED intent, kept
    // separately so an edited draft can never silently reuse the old key —
    // while an unchanged retry reuses it and the server replays exactly one
    // row. A dispatch whose outcome is still pending transfers as uncertain.
    var owned = inlineDraftOwner.get(ownedDraftId);
    if (owned) {
      // Taking over the visible draft: this session now owns the entry, so
      // its Cancel/cleanup works and every older session's late outcome stays
      // fenced out.
      owned.rev = draftRev;
      ta.value = owned.text || "";
      replyWrap._replyState = owned.state === "pending" ? "uncertain" : (owned.state || "none");
      replyWrap._replyKey = owned.idempotencyKey || null;
      replyWrap._replyBody = owned.dispatchedBody || null;
      replyWrap._replyParent = owned.dispatchedParent || c.id;
      replyWrap._replyConcept = owned.dispatchedConcept != null ? owned.dispatchedConcept : (c.concept || "");
    }
    // Q4: an unconfirmed reply offers Retry (same key, no duplicate) only
    // while the text is still what was sent. Changed text is a new reply
    // under a new key, and the status says the earlier one may be posted.
    // Changing the text back restores the same-key Retry.
    function syncUncertainReply() {
      if (replyWrap._replyState !== "uncertain") return;
      var same = (ta.value || "").trim() === replyWrap._replyBody;
      submit.textContent = same ? "Retry reply" : "Reply";
      _inlineReplyStatus(replyWrap, same ? INLINE_REPLY_UNCERTAIN_TEXT : INLINE_REPLY_CHANGED_TEXT, "warn");
    }
    syncUncertainReply();
    ta.addEventListener("input", function () {
      inlineDraftOwner.set(ownedDraftId, inlineDraftEntry("reply", c.id, ta.value, replyWrap, draftRev));
      syncUncertainReply();
    });
    // F3: explicit cancel owns its draft. A dispatched pending/uncertain
    // intent survives the composer — cancellation is never proof that no
    // server commit happened — while an untouched or definitely-rejected
    // draft is cleaned up instead of lingering forever.
    var cancelReply = function () {
      try { replyAbort.abort("cancel"); } catch (e2) {}
      cleanupReplyListeners();
      var entry = inlineDraftOwner.get(ownedDraftId);
      if (entry && entry.rev === draftRev) {
        if (entry.state === "pending" || entry.state === "uncertain") entry.open = false;
        else inlineDraftOwner.delete(ownedDraftId);
      }
      replyWrap.remove();
    };
    cancel.addEventListener("click", cancelReply);
    submit.addEventListener("click", function () {
      var body = (ta.value || "").trim();
      if (!body) { ta.focus(); return; }
      // Blocker A: inline reply idempotency. A stable per-intent
      // Idempotency-Key is reused across an uncertain retry (same parent +
      // body) so a lost response cannot create a duplicate; a new key is used
      // after a confirmed rejection or a draft change.
      var replyParent = c.id;
      // A reply inherits its parent's target: a concept row replies with
      // `concept` + {kind:"concept"}; a reserved index/log row with
      // `document` + {kind:"page"}.
      var replyTarget = c.concept
        ? { concept: c.concept, document: conceptDocument(c.concept) }
        : (validDocumentPath(c.document) ? { concept: "", document: c.document } : defaultCommentTarget());
      var replyConcept = replyTarget.concept;
      var reuseKey = (replyWrap._replyState === "uncertain" && replyWrap._replyBody === body && replyWrap._replyParent === replyParent);
      var key = reuseKey ? replyWrap._replyKey : newCommentKey();
      replyWrap._replyKey = key;
      replyWrap._replyBody = body;
      replyWrap._replyParent = replyParent;
      replyWrap._replyConcept = replyConcept;
      replyWrap._replyState = "pending";
      // F3: the dispatched intent is owned outside the DOM from now on, under
      // THIS session's revision — the immutable intent identity the async
      // outcome below is fenced against.
      inlineDraftOwner.set(ownedDraftId, {
        kind: "reply", commentId: c.id, text: body, open: true, state: "pending",
        rev: draftRev,
        idempotencyKey: key, dispatchedBody: body, dispatchedParent: replyParent,
        dispatchedConcept: replyConcept,
      });
      // Q4: the text is read-only while it is sent, so a success that closes
      // the reply box can never drop text typed after dispatch.
      submit.disabled = true; submit.textContent = "Sending…"; ta.disabled = true;
      _inlineReplyStatus(replyWrap, null);
      if (!hasCommentTarget(replyTarget)) {
        replyWrap._replyState = "rejected"; replyWrap._replyKey = null;
        var noTarget = inlineDraftOwner.get(ownedDraftId);
        if (noTarget) { noTarget.state = "rejected"; noTarget.idempotencyKey = null; noTarget.dispatchedBody = null; }
        submit.disabled = false; submit.textContent = "Reply"; ta.disabled = false;
        _inlineReplyStatus(replyWrap, "This reply has no document to attach to. Your draft is retained.", "error");
        return;
      }
      var reqBody = Object.assign(wireTarget(replyTarget), { body: body, anchor: { kind: replyConcept ? "concept" : "page" }, actor: "user", detail: {}, parent_id: replyParent });
      // R8-REPLY: route/document/page-lifecycle generation owner.
      var fenceConcept = state.conceptId;
      var fenceAnnotationGen = state.annotationGeneration;
      var fenceNavEpoch = window.okfLoomLive ? window.okfLoomLive._getNavEpoch() : 0;
      // F3: the dispatched intent's ownership revision. The outcome below may
      // only touch the draft entry that still carries this revision; a
      // replacement composer's newer intent (key/body/state) is never
      // overwritten by this callback settling late.
      var dispatchedRev = draftRev;
      // Q4: a reply's target is its parent row, never document bytes, so a
      // revision change on the same page does not orphan it: it still
      // settles (an aborted request is unconfirmed, with the same key).
      function stillOwns() {
        return state.conceptId === fenceConcept &&
          !(window.okfLoomLive && window.okfLoomLive._getNavEpoch() !== fenceNavEpoch);
      }
      function trackOutcome(stateName, keepKey) {
        replyWrap._replyState = stateName;
        if (!keepKey) replyWrap._replyKey = null; // next send uses a fresh key
        var entry = inlineDraftOwner.get(ownedDraftId);
        if (entry && entry.rev === dispatchedRev) {
          entry.state = stateName;
          entry.idempotencyKey = keepKey ? replyWrap._replyKey : null;
          if (!keepKey) entry.dispatchedBody = null;
        }
      }
      function setUncertain(msg) {
        trackOutcome("uncertain", true);
        submit.disabled = false; submit.textContent = "Retry reply"; ta.disabled = false;
        _inlineReplyStatus(replyWrap, msg, "warn");
        toast(msg, { tone: "warn", ttl: 8000 });
      }
      function setRejected(msg) {
        trackOutcome("rejected", false);
        submit.disabled = false; submit.textContent = "Reply"; ta.disabled = false;
        _inlineReplyStatus(replyWrap, msg, "error");
        toast(msg, { tone: "error", ttl: 8000 });
      }
      // Q4: this send lost its owner (an in-document Back/Forward aborts it
      // and advances the navigation epoch) while its box is still shown on
      // this concept: the reply is unconfirmed, with the same key, never
      // stuck on "Sending…". A box that Cancel or a replacement removed
      // settles nothing, and a fenced-out result is never listed.
      function settleUnowned() {
        if (replyWrap.isConnected && state.conceptId === fenceConcept) setUncertain(INLINE_REPLY_UNCERTAIN_TEXT);
        else cleanupReplyListeners();
      }
      armReplyAbort();
      var sendAbort = replyAbort;
      // Final-blocker: route through the canonical deadline-owning
      // annotationFetch (POST policy) so the inline reply is bounded by the
      // same absolute deadline as the main composer, while preserving the
      // stable Idempotency-Key, parent/body/anchor state, uncertain/definite
      // classification, AND form-lifetime cancellation via abortSignal
      // (cancel/replacement/pagehide/navigation abort the operation).
      annotationFetch("/__comment", {
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: reqBody,
        operationKey: "inline-reply:" + c.id + ":" + key,
        abortSignal: sendAbort.signal,
        // Q4: the body is read under the POST's own deadline.
        readJson: true,
      }, "post", fenceAnnotationGen).then(function (result) {
        var pair = { res: result.response, data: result.data };
        if (!stillOwns()) { settleUnowned(); return; }
        // An unreadable body may belong to a stored reply: unconfirmed.
        if (result.unreadable) { setUncertain(INLINE_REPLY_UNCERTAIN_TEXT); return; }
        if (!pair.res.ok || !pair.data.ok) {
          // 5xx/429 = uncertain (safe retry, SAME key); 4xx = reject.
          if (pair.res.status >= 500 || pair.res.status === 429) {
            setUncertain(INLINE_REPLY_UNCERTAIN_TEXT);
          } else {
            setRejected("The reply was not posted: " + (pair.data.error || ("HTTP " + pair.res.status)) + ". Your draft is retained.");
          }
          return;
        }
        var confirmed = pair.data.comment;
        var envelopeOk = !!(confirmed
          && _validCanonicalRow(confirmed, replyConcept ? { concept: replyConcept } : { document: replyTarget.document })
          && (confirmed.concept || "") === replyConcept
          && confirmed.parent_id === replyParent
          && acknowledgedPageAnchor(confirmed.anchor, replyTarget));
        if (!envelopeOk) {
          // Blocker A: a malformed/missing/cross-concept acknowledged row is
          // accepted-unverified: the reply may have posted. Settle canonically
          // and offer a same-key retry (the idempotent re-POST is safe).
          void loadComments(); renderCommentsPanel();
          setUncertain(INLINE_REPLY_UNCERTAIN_TEXT);
          return;
        }
        upsertComment(confirmed);
        if (c.parent_id) {
          var p = state.comments.find(function (x) { return x.id === c.parent_id; });
          if (p && p.archived) p.archived = false;
        }
        replyWrap.remove();
        // F3: the confirmed reply closes its intent — drop the owned draft
        // only when it still names THIS key AND still belongs to the session
        // that dispatched it, so a newer intent (or a replacement editor's
        // newly typed, unsent text that inherited the key) is never deleted
        // by an older in-flight callback (departed-DOM callbacks must not
        // corrupt another active editor).
        var settled = inlineDraftOwner.get(ownedDraftId);
        if (settled && settled.rev === dispatchedRev && settled.idempotencyKey === key) inlineDraftOwner.delete(ownedDraftId);
        renderCommentsPanel();
        toast("Reply posted.", { tone: "success" });
        cleanupReplyListeners();
      }).catch(function (e) {
        // R9-REPLY: a request-specific abort (cancel/replacement) is the
        // expected terminal state: the box is gone, bail silently. The owned
        // draft keeps the pending/uncertain identity (F3). Q4: an abort by an
        // in-document Back/Forward (or a pagehide the page came back from)
        // leaves the box shown: see settleUnowned.
        if (sendAbort.signal.aborted || !stillOwns()) { settleUnowned(); return; }
        // Blocker A: a network/timeout error (not an explicit abort) is an
        // UNCERTAIN outcome — the POST may or may not have landed. Retain the
        // draft + SAME key for a safe retry; never announce failure that
        // implies the reply definitely did not post.
        setUncertain(INLINE_REPLY_UNCERTAIN_TEXT);
      });
    });
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit.click(); }
      if (e.key === "Escape") { cancelReply(); }
    });
    row.appendChild(submit);
    row.appendChild(cancel);
    replyWrap.appendChild(row);
    // Insert AFTER the card's children container (if any) so the reply
    // box appears at the bottom of the thread, right where the Reply
    // button is. Fall back to after the card itself.
    var childrenEl = card.querySelector(":scope > .okf-comment__children");
    if (childrenEl) {
      childrenEl.appendChild(replyWrap);
    } else {
      card.appendChild(replyWrap);
    }
    if (focusEditor) setTimeout(function () { ta.focus(); }, 30);
  }

  // Open the inline edit composer for one comment row (F3: extracted from
  // the Edit button closure so the draft owner can restore it).
  function openInlineEditComposer(c, openOpts) {
    openOpts = openOpts || {};
    var focusEditor = openOpts.focus !== false;
    // R9-REPLY: abort any in-flight POST owned by a prior inline reply.
    _retireOpenInlineComposer();
    var card = document.getElementById("comment-" + c.id);
    if (!card) return;
    var editWrap = el("div", { class: "okf-inline-reply okf-inline-edit" });
    var ownedDraftId = inlineDraftId("edit", c.id);
    // F3: this session's ownership revision (see inlineDraftRevisionSeq).
    // The save callback below can settle after a panel round trip destroyed
    // this DOM and a replacement editor took over the row's draft — every
    // cleanup it runs is fenced against the revision captured here.
    var draftRev = ++inlineDraftRevisionSeq;
    editWrap._ownedDraftId = ownedDraftId;
    editWrap._draftRev = draftRev;
    editWrap.appendChild(el("div", {
      class: "okf-composer__reply-context",
      text: "Editing comment",
    }));
    var ta = el("textarea", {
      class: "okf-composer__textarea okf-inline-reply__textarea",
      rows: "3",
      "aria-label": "Edit comment",
    });
    var ownedEdit = inlineDraftOwner.get(ownedDraftId);
    if (ownedEdit) {
      // Taking over the visible draft: this session now owns the entry (see
      // the reply composer note) — its Cancel/Escape cleanup works, and any
      // departed editor's late save outcome stays fenced out.
      ownedEdit.rev = draftRev;
    }
    ta.value = ownedEdit ? (ownedEdit.text || "") : (c.body || "");
    editWrap.appendChild(ta);
    // F3: the edit draft is owned outside the panel DOM. An edit is a
    // row-token CAS (no idempotency key), so only text is retained.
    ta.addEventListener("input", function () {
      inlineDraftOwner.set(ownedDraftId, inlineDraftEntry("edit", c.id, ta.value, null, draftRev));
    });
    // F3: deleting the draft is fenced to THIS editor session. A replacement
    // editor opened after a panel round trip mints the next revision, so this
    // session's late success (or cancel/Escape on destroyed DOM) can never
    // delete a newer session's typed draft under the shared row key.
    var forgetEditDraft = function () {
      var entry = inlineDraftOwner.get(ownedDraftId);
      if (entry && entry.rev !== draftRev) return;
      inlineDraftOwner.delete(ownedDraftId);
    };
    var row = el("div", { class: "okf-composer__actions" });
    var save = el("button", { type: "button", class: "okf-studiobtn okf-studiobtn--primary", text: "Save" });
    var cancelEdit = el("button", { type: "button", class: "okf-studiobtn", text: "Cancel" });
    cancelEdit.addEventListener("click", function () { forgetEditDraft(); editWrap.remove(); });
    save.addEventListener("click", function () {
      var body = (ta.value || "").trim();
      if (!body) { ta.focus(); return; }
      if (body === (c.body || "").trim()) { forgetEditDraft(); editWrap.remove(); return; }
      // Q4: read-only while saving, like a reply being sent.
      save.disabled = true; save.textContent = "Saving…"; ta.disabled = true;
      // Q4: a save that never answers must not leave the editor read-only on
      // "Saving…". At the deadline the request is abandoned; it may still
      // have been applied, so the comments are re-read and the text is
      // editable again.
      var saveAbort = new AbortController();
      var saveDeadlines = state.annotationDeadlines;
      var saveTimer = setTimeout(function () { try { saveAbort.abort(); } catch (e2) {} },
        saveDeadlines && saveDeadlines.editSave != null ? saveDeadlines.editSave : 15000);
      tokenFetch("/__comment-update", {
        method: "POST",
        body: { id: c.id, body: body, expected_row_token: c.row_token || undefined },
        signal: saveAbort.signal,
      }).then(function (res) { return res.json().then(function (data) { return { res: res, data: data }; }); }).then(function (pair) {
        // R6-6: require res.ok && data.ok.
        if (!pair.res.ok || !pair.data.ok) throw new Error(pair.data.error || ("HTTP " + pair.res.status));
        // Blocker 3: validate the returned canonical row before adopting it.
        // A malformed/missing/cross-concept row must not be upserted or
        // announced as success; settle canonically instead.
        var updated = pair.data.comment;
        if (updated && _validCanonicalRow(updated, { id: c.id, concept: c.concept })) {
          upsertComment(updated);
          forgetEditDraft();
          editWrap.remove();
          renderCommentsPanel();
          toast("Comment updated.", { tone: "success" });
        } else {
          void loadComments();
          renderCommentsPanel();
          toast("Updated, but the server response could not be verified. Refreshed canonical state.", { tone: "error", ttl: 7000 });
          if (editWrap.parentNode) editWrap.remove();
          // F3: the outcome is unverified — keep the text owned (closed),
          // so the user can re-check canonical state and retry knowingly.
          // Only THIS session's entry is closed; a replacement editor's
          // newer draft stays open.
          var entry = inlineDraftOwner.get(ownedDraftId);
          if (entry && entry.rev === draftRev) entry.open = false;
        }
      }).catch(function (e) {
        save.disabled = false; save.textContent = "Save"; ta.disabled = false;
        if (saveAbort.signal.aborted) {
          void loadComments();
          toast("The edit could not be confirmed, so the comments are being re-read. Check the comment before saving again.", { tone: "warn", ttl: 8000 });
          return;
        }
        toast("Edit failed: " + e.message, { tone: "error" });
      }).finally(function () { clearTimeout(saveTimer); });
    });
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save.click(); }
      if (e.key === "Escape") { forgetEditDraft(); editWrap.remove(); }
    });
    row.appendChild(save);
    row.appendChild(cancelEdit);
    editWrap.appendChild(row);
    card.appendChild(editWrap);
    if (focusEditor) setTimeout(function () { ta.focus(); }, 30);
  }

  function buildCommentActions(c, opts) {
    opts = opts || {};
    var isRoot = !!opts.isRoot;
    var threadResolved = !!opts.threadResolved;
    var actions = el("div", { class: "okf-comment__actions" });
    var replyBtn = el("button", { type: "button", class: "okf-comment__action", text: "Reply" });
    replyBtn.addEventListener("click", function () { openInlineReplyComposer(c, { focus: true }); });
    actions.appendChild(replyBtn);
    // Comment-only authority is create + reply. Edit/dismiss/reopen/archive/
    // re-anchor are row updates the server denies without edit authority, so
    // they are never offered as controls that could only fail.
    if (!EDIT) return actions;

    // Edit: amend the body of a not-yet-resolved comment in place (the
    // enter-too-soon fix). Shown on user-authored comments only — agent
    // comments/replies are the agent's record, not the user's to rewrite.
    // The server rejects edits on resolved/dismissed comments (409).
    if (!isAgentActor(c.actor) && c.state !== "resolved" && c.state !== "dismissed") {
      var editBtn = el("button", { type: "button", class: "okf-comment__action", text: "Edit" });
      editBtn.addEventListener("click", function () { openInlineEditComposer(c, { focus: true }); });
      actions.appendChild(editBtn);
    }

    // Lifecycle verbs (Cancel / Reopen). These DO NOT touch the archive
    // flag — archive is a separate track.
    if (c.state === "open" && !c.claimed_by) {
      var cancelBtn = el("button", { type: "button", class: "okf-comment__action", text: "Cancel" });
      cancelBtn.addEventListener("click", function () { updateCommentState(c.id, "dismissed"); });
      actions.appendChild(cancelBtn);
    }
    if (c.state === "dismissed") {
      var reopenBtn = el("button", { type: "button", class: "okf-comment__action", text: "Reopen" });
      reopenBtn.addEventListener("click", function () { updateCommentState(c.id, "open"); });
      actions.appendChild(reopenBtn);
    }

    // Archive / Unarchive: ONLY on the root comment. Archive requires
    // every comment in the thread to be resolved (server-enforced, but
    // we also disable the button client-side to give a clear affordance).
    // Unarchive is always available on an archived root.
    if (isRoot) {
      if (c.archived) {
        var unarchBtn = el("button", {
          type: "button",
          class: "okf-comment__action",
          text: "Unarchive",
        });
        unarchBtn.addEventListener("click", function () { setCommentArchived(c.id, false); });
        actions.appendChild(unarchBtn);
      } else if (threadResolved) {
        var archBtn2 = el("button", {
          type: "button",
          class: "okf-comment__action",
          text: "Archive thread",
          title: "Archive this thread. Requires all comments resolved.",
        });
        archBtn2.addEventListener("click", function () { setCommentArchived(c.id, true); });
        actions.appendChild(archBtn2);
      } else {
        // Disabled affordance so the user can see what's missing.
        var archDisabled = el("button", {
          type: "button",
          class: "okf-comment__action okf-comment__action--disabled",
          text: "Archive thread",
          disabled: true,
          title: "Archive is available once every comment in the thread is resolved.",
        });
        actions.appendChild(archDisabled);
      }
    }
    return actions;
  }

  async function updateCommentState(commentId, newState) {
    // R6-6: include expected_row_token, require res.ok && data.ok, adopt
    // the canonical returned row/token. Preserve input on conflict.
    var existing = state.comments.find(function (x) { return x.id === commentId; });
    var expectedRowToken = existing && existing.row_token;
    try {
      var res = await tokenFetch("/__comment-update", {
        method: "POST",
        body: { id: commentId, state: newState, expected_row_token: expectedRowToken || undefined },
      });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok || !data.ok) throw new Error(data.error || ("HTTP " + res.status));
      // Blocker B: a missing data.comment on a 2xx-ok response is anomalous and
      // must be treated EXACTLY like a malformed/cross-concept row — no local
      // success mutation, no success toast; reconcile canonically instead. The
      // server always returns the canonical row on a successful update, so a
      // missing row is never a trustworthy success signal.
      var row = data.comment;
      if (row && _validCanonicalRow(row, { id: commentId, concept: existing && existing.concept })) {
        upsertComment(row);
      } else {
        void loadComments();
      }
      // F11: the lifecycle change moved the page's own row counts — refresh
      // the badges + page feedback immediately instead of waiting for the SSE
      // echo or a refetch that may not come while disconnected.
      updateBadges();
      renderCommentsPanel();
    } catch (e) {
      toast("Failed to update comment.", { tone: "error" });
    }
  }

  // Archive is a SEPARATE track from lifecycle state. Setting
  // archived=true/false does NOT touch open/resolved/dismissed. The server
  // enforces "archive only on root + only when whole thread is resolved";
  // we surface the rejection reason in the toast.
  async function setCommentArchived(commentId, archived) {
    // R6-6: include expected_row_token, require res.ok && data.ok, adopt
    // the canonical returned row/token. Preserve input on conflict.
    var existing = state.comments.find(function (x) { return x.id === commentId; });
    var expectedRowToken = existing && existing.row_token;
    try {
      var res = await tokenFetch("/__comment-update", {
        method: "POST",
        body: { id: commentId, archived: archived, expected_row_token: expectedRowToken || undefined },
      });
      var data = await res.json();
      if (!res.ok || !data.ok) {
        var msg = data.error || "failed";
        if (data.unresolved_ids && data.unresolved_ids.length) {
          msg = "Resolve every comment in the thread first (" +
            data.unresolved_ids.length + " unresolved).";
        }
        toast(msg, { tone: "error", ttl: 6000 });
        return;
      }
      // R6-6: adopt the canonical returned row/token. Blocker B: validate the
      // returned row first; a malformed/missing/cross-concept row must not be
      // adopted, mutated, or announced — settle canonically instead. A missing
      // row is never a success signal (the server always returns one on
      // success), so do NOT optimistically flip the local archive flag.
      var row = data.comment;
      if (row && _validCanonicalRow(row, { id: commentId, concept: existing && existing.concept })) {
        upsertComment(row);
        renderCommentsPanel();
        toast(archived ? "Thread archived." : "Thread unarchived.", { tone: "info" });
      } else {
        void loadComments();
        renderCommentsPanel();
        toast(archived ? "Archive sent, but the server response could not be verified. Refreshed canonical state." : "Unarchive sent, but the server response could not be verified. Refreshed canonical state.", { tone: "warn", ttl: 7000 });
      }
      // F11: archive is part of the page-feedback row filter, so refresh the
      // count together with the badges.
      updateBadges();
    } catch (e) {
      toast("Failed to update archive.", { tone: "error" });
    }
  }

  // ====================================================================
  // 6. Presence (current spec §12)
  // ====================================================================
  function renderPresence(p) {
    if (!CAN_READ_PRESENCE) return;
    // F12: every canonical presence render owns the Watching switch. Toggle
    // writes fence their settlement against this sequence so a late ack or
    // rejection can never roll back a newer canonical state.
    state._presenceSeq++;
    const records = p && p.version === 2 && p.online !== false && Array.isArray(p.records) ? p.records : [];
    const agent = records.find((row) => row && row.kind === "agent") || { state: "idle" };
    state.presence = agent;
    const st = agent.state || "idle";
    // iter2 G13: record presence transitions for the Agent-activity panel's
    // presence-history section (unique content the Changes panel doesn't
    // show). Dedup consecutive identical (state, focus) so a noisy feed
    // doesn't flood the log; cap at 24 entries.
    const focus = agent.focus || "";
    // The free-text progress line ("linking 3 of 7 tables…") is part of the
    // dedup key so mid-pass message updates land in the presence history —
    // that history IS the progress log for a long multi-concept pass.
    const message = agent.message || "";
    const last = state.presenceHistory[0];
    if (!last || last.state !== st || last.focus !== focus || (last.message || "") !== message) {
      state.presenceHistory.unshift({ state: st, focus: focus, message: message, ts: new Date().toISOString() });
      if (state.presenceHistory.length > 24) state.presenceHistory.length = 24;
    }
    presenceChip.dataset.state = st;
    presenceChip.setAttribute("aria-label", "Agent presence: " + st
      + (agent.focus ? " " + agent.focus : "")
      + (message ? " — " + message : ""));
    presenceLabel.innerHTML = "";
    presenceLabel.appendChild(el("span", { class: "okf-presence__actor", text: "Agent" }));
    const verb = ({ idle: "idle", watching: "watching", thinking: "thinking about", editing: "editing" })[st] || st;
    presenceLabel.appendChild(document.createTextNode(" " + verb));
    if (agent.focus) {
      presenceLabel.appendChild(document.createTextNode(" "));
      const a = el("a", { href: "/" + agent.focus });
      a.textContent = agent.focus;
      presenceLabel.appendChild(a);
    }
    if (message) {
      presenceLabel.appendChild(el("span", {
        class: "okf-presence__message",
        text: " — " + message,
        title: message,
      }));
    }
    // iter2 G11: keep the "Agent watching" toggle in sync with the agent's
    // canonical reported presence. Only "watching" reads as on; every other
    // state (idle, thinking, editing) reads as off. This canonical render is
    // the owner: a click settlement fences against state._presenceSeq, so it
    // never overwrites this value with a stale outcome.
    // iter3 CRI3-010: the concise aria-label moves with the state so the
    // screen-reader name always reflects the live value.
    if (watchingToggle) syncWatchingToggle(st === "watching");
    // Presence changes no list, so no panel re-renders for it; keep every
    // visible availability sentence current in place instead.
    refreshAgentAvailability();
    highlightFocusedConcept(agent.focus);
  }
  function highlightFocusedConcept(focus) {
    stampConceptIds();
    // iter1 CRI-004: clear every presence highlight (list rows + article),
    // then re-apply to the matching row if any. Idle (no focus) leaves the
    // workspace clean so a stale "agent here" cue never lingers.
    $$(".okf-presence-focus").forEach((n) => n.classList.remove("okf-presence-focus"));
    $$(".okf-focus-highlight").forEach((n) => n.classList.remove("okf-focus-highlight"));
    state._lastFocus = focus || null;
    if (!focus) return;
    // On the open concept page, outline the article.
    if (focus === state.conceptId) {
      const article = $("article.okf-page__main");
      if (article) article.classList.add("okf-focus-highlight");
    }
    // On index/search pages, highlight the matching row by data-concept-id.
    const row = document.querySelector('[data-concept-id="' + cssEscape(focus) + '"]');
    if (row) row.classList.add("okf-presence-focus");
  }
  // iter1 CRI-004: stamp data-concept-id on list/search/sidebar rows so the
  // presence focus highlight can resolve against them. Done lazily once per
  // page (rows are server-rendered; a patch would re-trigger via boot).
  let conceptIdsStamped = false;
  function stampConceptIds() {
    if (conceptIdsStamped) return;
    conceptIdsStamped = true;
    $$(".okf-concept-list li").forEach((li) => {
      if (li.getAttribute("data-concept-id")) return;
      const a = li.querySelector("a[href]");
      const id = hrefToConceptId(a && a.getAttribute("href"));
      if (id) li.setAttribute("data-concept-id", id);
    });
    $$(".okf-search-result").forEach((art) => {
      if (art.getAttribute("data-concept-id")) return;
      const a = art.querySelector("a[href]");
      const id = hrefToConceptId(a && a.getAttribute("href"));
      if (id) art.setAttribute("data-concept-id", id);
    });
    $$(".okf-local-graph__node").forEach((btn) => {
      if (btn.getAttribute("data-concept-id")) return;
      const t = btn.getAttribute("data-target");
      if (t) btn.setAttribute("data-concept-id", t);
    });
  }
  // ---- Phase 5: index engagement (recency rail + comment chips) --------
  // Runs after the structure-only graph view/loadComments() resolve
  // (called from both — idempotent; the rail rebuilds so labels upgrade
  // once structure lands). Non-index connection chips use the same view.
  // F14: reader words for the machine event verbs on the human-facing
  // recency rail — the same mapping the SSR rows own in render.py
  // (_RECENT_READER_VERBS / _RECENT_CHANNEL_ACTORS / _recent_row_reader_text).
  // Unknown actions keep their raw record (nothing invented); presenting a
  // transport as a person/action pair would misread the event record.
  const RECENT_READER_VERBS = { changed: "edited", updated: "edited", created: "created", removed: "removed" };
  const RECENT_CHANNEL_ACTORS = { disk: " on disk", mutator: " via mutator", undo: " via undo" };
  function recentRowReaderText(actor, action) {
    const rawActor = String(actor == null ? "" : actor);
    const rawAction = String(action == null ? "" : action);
    const verb = RECENT_READER_VERBS[rawAction.trim().toLowerCase()];
    if (Object.prototype.hasOwnProperty.call(RECENT_CHANNEL_ACTORS, rawActor)) {
      return (verb || rawAction) + RECENT_CHANNEL_ACTORS[rawActor];
    }
    return (rawActor + " " + (verb || rawAction)).trim();
  }
  function buildIndexEngagement() {
    // Connection chips are surface-independent and reconcile as soon as an
    // accepted graph/comments generation exists. Recent activity remains
    // index-only below.
    if (!state.graphReady || !state.commentsReady) return;
    let routeMap = {};
    try { routeMap = JSON.parse(document.body.dataset.okfConceptRoutes || "{}"); } catch (e) { routeMap = {}; }
    function routeFor(id) {
      const route = routeMap[id];
      if (typeof route === "string" && route && route !== "#") return route;
      const card = document.querySelector('.okf-card[data-concept-id="' + cssEscape(id) + '"] a.okf-card__link[href]');
      return card && card.getAttribute("href") || null;
    }
    const titleOf = {};
    if (state.graph && Array.isArray(state.graph.nodes)) state.graph.nodes.forEach((n) => {
      if (n && n.data && n.data.id) titleOf[n.data.id] = n.data.label || n.data.id;
    });
    const counts = {};
    (state.comments || []).forEach((c) => {
      if (!c || !c.concept || c.archived === true || c.state === "archived") return;
      if ((c.state !== "open" && c.state !== "claimed") || !Object.prototype.hasOwnProperty.call(titleOf, c.concept)) return;
      counts[c.concept] = (counts[c.concept] || 0) + 1;
    });
    $$(".okf-conn[data-concept-id]").forEach((item) => {
      const id = item.dataset.conceptId;
      const count = counts[id] || 0;
      let chip = item.querySelector(".okf-comment-count");
      if (!count) {
        if (chip) {
          const ownerLink = item.querySelector("a.okf-conn__title[href]");
          if (document.activeElement === chip && ownerLink) ownerLink.focus();
          chip.remove();
        }
        return;
      }
      if (!chip) {
        const ownerLink = item.querySelector("a.okf-conn__title[href]");
        if (!ownerLink) return;
        chip = el("a", { class: "okf-card__comments okf-comment-count", href: ownerLink.getAttribute("href"), dataset: { conceptId: id } });
        let meta = item.querySelector(".okf-conn__meta");
        if (!meta) { meta = el("span", { class: "okf-conn__meta" }); item.appendChild(meta); }
        meta.appendChild(chip);
      }
      const label = count + " active comment" + (count === 1 ? "" : "s");
      chip.textContent = label;
      chip.setAttribute("aria-label", label + " on " + (titleOf[id] || id));
    });
    if (!document.body.classList.contains("okf-viewer--index")) return;
    // SSR remains authoritative until the complete accepted tuple exists.
    if (!state.eventsReady) return;
    $$(".okf-concept-list li[data-concept-id]").forEach((li) => {
      const id = li.getAttribute("data-concept-id"); let chip = li.querySelector(".okf-card__comments");
      const count = counts[id] || 0;
      if (!count) {
        if (chip) {
          const ownerLink = li.querySelector("a.okf-card__link[href]");
          if (document.activeElement === chip && ownerLink) ownerLink.focus();
          chip.remove();
        }
        return;
      }
      const conceptLink = li.querySelector("a.okf-card__link[href]");
      if (!chip) {
        const route = routeFor(id); if (!route) return;
        chip = el("a", { class: "okf-card__comments", href: route });
        const meta = li.querySelector(".okf-card__meta");
        if (meta) meta.appendChild(chip); else li.appendChild(el("span", { class: "okf-card__meta" }, [chip]));
      }
      const label = count + " active comment" + (count === 1 ? "" : "s");
      chip.textContent = label; chip.setAttribute("aria-label", label + " on " + (titleOf[id] || id));
    });
    const hero = $(".okf-hero"); if (!hero) return;
    const focusedRecent = document.activeElement && document.activeElement.closest
      ? document.activeElement.closest("#okf-recent-rail li[data-concept-id]") : null;
    const focusedConceptId = focusedRecent && focusedRecent.dataset.conceptId;
    const latest = {};
    (state.events || []).forEach((ev) => {
      if (!ev || !ev.ts || !["changed", "created", "removed", "activity"].includes(ev.type)) return;
      const ids = Array.isArray(ev.ids) ? ev.ids.slice() : [];
      [ev.concept, ev.concept_id].forEach((id) => { if (typeof id === "string") ids.push(id); });
      ids.forEach((id) => {
        if (typeof id !== "string" || !Object.prototype.hasOwnProperty.call(titleOf, id)) return;
        const identity = eventAttentionKey(ev);
        if (identity && (!latest[id] || latest[id].ts < ev.ts)) latest[id] = { ts: ev.ts, type: ev.type, action: ev.action || ev.type, actor: ev.actor || ev.origin || "system", eventId: identity };
      });
    });
    const ranked = Object.keys(latest).filter((id) => latest[id].type !== "removed")
      .sort((a, b) => latest[a].ts < latest[b].ts ? 1 : (latest[a].ts > latest[b].ts ? -1 : a.localeCompare(b))).slice(0, 5);
    const oldRail = $("#okf-recent-rail"); if (oldRail) oldRail.remove();
    if (!ranked.length) return;
    const rail = el("section", { class: "okf-recent", id: "okf-recent-rail", "aria-labelledby": "okf-recent-heading" });
    rail.appendChild(el("h2", { class: "okf-recent__label", id: "okf-recent-heading", text: "Recently changed" }));
    const list = el("ol", { class: "okf-recent__list" });
    ranked.forEach((id) => {
      const route = routeFor(id); if (!route) return;
      const stable = String(latest[id].eventId).replace(/[^a-zA-Z0-9_-]/g, "-") + "-" + id.replace(/[^a-zA-Z0-9_-]/g, "-");
      // F14: the live rail speaks the same reader words as the SSR rows
      // (render.py _recent_row_reader_text) — machine verbs map to reader
      // words, transport origins become channel phrases, and unknown values
      // keep their raw audit record. The raw actor/action stay on the row
      // element as data-event-* attributes, exactly like the server rows.
      const li = el("li", {
        id: "okf-recent-" + stable,
        dataset: { conceptId: id, eventAction: latest[id].action, eventActor: latest[id].actor },
      });
      const link = el("a", { class: "okf-recent__item", href: route });
      link.appendChild(el("span", {}, [
        el("strong", { text: titleOf[id] }),
        document.createTextNode(" — " + recentRowReaderText(latest[id].actor, latest[id].action)),
      ]));
      // An explicit separator between the phrase and the time: the
      // accessible name must not glue them together ("disk changed14h ago").
      // It is screen-reader-only (same markup as render.py): a bare text
      // node would become an anonymous item in the row's two-column grid,
      // painting a stray dot and wrapping the time onto a second line.
      link.appendChild(el("span", { class: "sr-only", text: " \u00b7 " }));
      const stamp = fmtUtcStamp(latest[id].ts);
      const time = el("time", { class: "okf-recent__time", datetime: latest[id].ts, text: fmtRecentAgo(latest[id].ts) || stamp });
      if (stamp) time.setAttribute("title", stamp);
      link.appendChild(time);
      li.appendChild(link); list.appendChild(li);
    });
    rail.appendChild(list); hero.appendChild(rail);
    if (focusedConceptId) {
      const restored = rail.querySelector('li[data-concept-id="' + cssEscape(focusedConceptId) + '"] a');
      const cardFallback = document.querySelector('.okf-card[data-concept-id="' + cssEscape(focusedConceptId) + '"] a.okf-card__link');
      const sectionFallback = document.querySelector(".okf-section__title, .okf-type-nav__link");
      const target = restored || cardFallback || sectionFallback;
      if (target) { if (!target.matches("a,button,[tabindex]")) target.setAttribute("tabindex", "-1"); target.focus(); }
    }
  }

  function hrefToConceptId(href) {
    if (!href || typeof href !== "string") return null;
    // Strip .html (static mode) AND .md (some index render paths emit the
    // source extension), plus any leading slash, so the id matches the form
    // presence.focus / currentConceptId use (e.g. "tables/orders").
    let h = href.trim().replace(/\.(html|md)$/, "").replace(/^\/+/, "");
    if (!h || h.indexOf("__") === 0 || h.indexOf("://") >= 0 || h.indexOf("#") === 0) return null;
    return h;
  }

  // ====================================================================
  // 7. Change list (§12.2) + Undo (§12.5)
  // ====================================================================
  // iter1 CRI-005 → iter2 G9: virtualization constants. iter3 CRI3-005
  // hoists these ABOVE renderChangeList so the prepend-shift compensation
  // can read CHANGE_EST_ROW_H without hitting a `const` temporal-dead-zone
  // (the function is defined before the constants were under the old
  // structure).
  const CHANGE_VIRTUAL_WINDOW = 80;   // rows kept in the DOM at any time
  const CHANGE_VIRTUAL_OVERSCAN = 20; // extra rows rendered above/below the viewport
  const CHANGE_VIRTUAL_STEP = 30;     // how far the window slides per sentinel hit
  const CHANGE_EST_ROW_H = 56;        // initial estimate; re-measured after first paint
  const CHANGE_HARD_CAP = 1000;       // display cap (gates the upfront note)
  const CHANGE_TOTAL_CAP = (window.OKF_LOOM_CHANGE_TOTAL_CAP > 0) ? window.OKF_LOOM_CHANGE_TOTAL_CAP : 10000;

  // iter3 CRI3-005: when a live event is prepended to state.events (via
  // upsertEvent → unshift), the next renderChangeList rebuild produces an
  // ordered array that is 1 row longer at the TOP. The virtualizer
  // honours savedScroll by indexing into the NEW ordered array — so the
  // same INDEX window now points at different rows (everything shifted
  // down by `prepended`). The user's reading row drifts UP by one row
  // per live event. Compensate by tracking the previous render's ordered
  // count + the active filter tuple: when the filters are unchanged AND
  // the new ordered is longer at the head (a genuine prepend, not a
  // filter relaxation), shift savedScroll by `prepended * CHANGE_EST_ROW_H`
  // so the same ROWS stay at the same on-screen offset. Only shift when
  // the user was actually scrolled into the list (savedScroll > 0); a
  // user at the top wants to see the new event arrive at the top.
  let _lastChangeListOrderedCount = 0;
  let _lastChangeListFilters = { actor: "", action: "", concept: "" };

  // R3: the server records sharing the Studio through a public tunnel
  // (tunnel_attach, tunnel_detach, a lost tunnel) as an activity event with
  // origin "tunnel" and actor "cli". It writes nothing to the bundle, so it is
  // not a change: Changes, its unread count and the recent-writes count leave
  // it out, and Agent activity lists it under Sharing. It is not evidence of
  // an agent action either (isAgentActionEvent): the share command cannot tell
  // a person from an agent, and older sessions recorded these rows with actor
  // "agent", which must not read as an agent having acted.
  function isSharingEvent(e) {
    return !!e && e.origin === "tunnel";
  }

  // F10: single owner of the changes-panel row computation. The renderer and
  // the exact-identity reveal must agree on which events pass the filters and
  // how they collapse into groups/bursts, or a reveal could resolve an index
  // the renderer does not draw.
  function computeChangeItems() {
    // Timeline rows: agent activity (has `action`) AND bundle changes
    // (changed/created/removed - e.g. a disk save the watcher picked up).
    // comment/presence/graph/ready have their own surfaces and are excluded,
    // and so is sharing (see isSharingEvent).
    let rows = state.events.filter((e) => {
      const t = e.type;
      if (t === "comment" || t === "presence" || t === "graph" || t === "ready") return false;
      if (isSharingEvent(e)) return false;
      return !!e.action || (Array.isArray(e.ids) && e.ids.length) ||
        t === "changed" || t === "created" || t === "removed";
    });
    if (state.filters.actor) rows = rows.filter((e) => (e.actor || e.origin || "") === state.filters.actor);
    if (state.filters.action) rows = rows.filter((e) => String(e.action || e.type || "").indexOf(state.filters.action) >= 0);
    if (state.filters.concept) rows = rows.filter((e) => (e.ids || []).some((id) => id.indexOf(state.filters.concept) >= 0));
    // Group consecutive same-group_id rows into single composite items so
    // the window count reflects what the user actually sees. iter2 G12:
    // events tagged with a burst_id (10+ standalone events in 1s) collapse
    // the same way into one expandable "Agent made N changes" row.
    const groups = Object.create(null);
    const bursts = Object.create(null);
    const ordered = [];
    rows.forEach((r) => {
      if (r.group_id) {
        if (!groups[r.group_id]) { groups[r.group_id] = []; ordered.push({ group: r.group_id }); }
        groups[r.group_id].push(r);
      } else if (r.burst_id) {
        if (!bursts[r.burst_id]) { bursts[r.burst_id] = []; ordered.push({ burst: r.burst_id }); }
        bursts[r.burst_id].push(r);
      } else ordered.push({ row: r });
    });
    return { rows, ordered, groups, bursts };
  }

  // F10: terminal explanation for a reveal whose record is not in the
  // retained history. Focused so keyboard and screen-reader users get the
  // outcome instead of a silent no-op.
  function revealUnavailableNode(message) {
    return el("div", {
      class: "okf-panel-reveal-note", role: "status", tabindex: "-1",
      text: message,
    });
  }

  function renderChangeList() {
    if (state.openPanel !== "changes") return;
    const body = panelBodyEl();
    if (!body) return;
    if (!state.eventsReady && (!state.events || !state.events.length)) {
      body.innerHTML = "";
      body.appendChild(dataStateNode("changes", "Changes"));
      return;
    }
    // iter2 CRI2-003 / §7.3: renderChangeList rebuilds the panel from scratch
    // on every live event (activity/changed/created/removed). A from-scratch
    // rebuild resets scroll to the top AND drops focus/selection inside the
    // filter inputs, so a user scrolled down to row 200 reading history loses
    // their place the moment a new event lands. Capture the scroll offset +
    // the focused filter (with its caret selection) before the rebuild and
    // restore both after, so a live update is non-disruptive while the panel
    // is open. (The windowing IntersectionObserver still owns lazy row load.)
    const savedScroll = body.scrollTop;
    const active = document.activeElement;
    const savedFocus = (active && body.contains(active) && (active.tagName === "INPUT" || active.tagName === "SELECT"))
      ? { tag: active.tagName, ariaLabel: active.getAttribute("aria-label") || "",
          selStart: active.selectionStart, selEnd: active.selectionEnd,
          selDir: active.selectionDirection }
      : null;
    // iter3 CRI3-005: snapshot the previous render's bookkeeping BEFORE
    // the rebuild so we can detect a genuine prepend (vs a filter change).
    const prevOrderedCount = _lastChangeListOrderedCount;
    const prevFilters = _lastChangeListFilters;
    const filtersUnchanged =
      prevFilters.actor === state.filters.actor &&
      prevFilters.action === state.filters.action &&
      prevFilters.concept === state.filters.concept;
    body.innerHTML = "";
    if (!state.eventsReady) body.appendChild(dataStateNode("changes", "Changes"));

    // F10: reconcile an exact-identity reveal BEFORE any control or row is
    // built, so only the filters that actually exclude the requested record
    // are cleared — and only when the record exists in retained data. Doing
    // this after the filter controls were seeded from state rendered a stale
    // select value while the rows themselves already showed the record.
    const revealRequest = state.pendingChangeReveal ? String(state.pendingChangeReveal) : null;
    if (revealRequest) {
      const revealTarget = state.events.find((e) => e && String(e.id || "") === revealRequest);
      if (revealTarget) {
        if (state.filters.actor && (revealTarget.actor || revealTarget.origin || "") !== state.filters.actor) state.filters.actor = "";
        if (state.filters.action && String(revealTarget.action || revealTarget.type || "").indexOf(state.filters.action) < 0) state.filters.action = "";
        if (state.filters.concept && !(revealTarget.ids || []).some((id) => id.indexOf(state.filters.concept) >= 0)) state.filters.concept = "";
      }
    }

    const filters = el("div", { class: "okf-filters" });
    const actorSel = el("select", { "aria-label": "Filter by actor" });
    actorSel.appendChild(el("option", { value: "", text: "All actors" }));
    ["agent", "user", "cli", "disk"].forEach((a) => actorSel.appendChild(el("option", { value: a, text: a })));
    actorSel.value = state.filters.actor;
    actorSel.addEventListener("change", () => { state.filters.actor = actorSel.value; renderChangeList(); });
    const actionInput = el("input", { type: "search", placeholder: "filter action…",
      "aria-label": "Filter by action", value: state.filters.action });
    actionInput.addEventListener("input", () => { state.filters.action = actionInput.value.trim(); renderChangeList(); });
    const conceptInput = el("input", { type: "search", placeholder: "filter concept…",
      "aria-label": "Filter by concept", value: state.filters.concept });
    conceptInput.addEventListener("input", () => { state.filters.concept = conceptInput.value.trim(); renderChangeList(); });
    filters.appendChild(actorSel); filters.appendChild(actionInput); filters.appendChild(conceptInput);
    body.appendChild(filters);

    const list = el("div", { class: "okf-changes" });
    const items = computeChangeItems();
    const rows = items.rows;
    // iter3 CRI3-005: hoist `effectiveSavedScroll` and `newOrderedCount`
    // out of the else-branch so the post-rebuild scroll restore + the
    // next-render bookkeeping can both read them. effectiveSavedScroll
    // starts at savedScroll and is bumped inside the else-branch when a
    // genuine prepend is detected; newOrderedCount is 0 for the empty
    // state and `ordered.length` when rows render. F10: revealFired marks a
    // render where the reveal owns scroll + focus instead of the restore.
    let effectiveSavedScroll = savedScroll;
    let newOrderedCount = 0;
    let revealFired = false;
    if (!rows.length) {
      if (revealRequest && state.eventsReady) {
        // Nothing retained matches — including after filter reconciliation.
        state.pendingChangeReveal = null;
        list.appendChild(revealUnavailableNode(
          "That change isn't available in the retained history. Older activity rotates out as new events arrive."));
      }
      list.appendChild(emptyState(
        "No changes yet",
        "Changes appear here after the agent or a local editor updates the bundle.",
        "About authoring",
        function () { openPanel("studio", { returning: true }); const help = panelBody.querySelector(".okf-authoring-help"); if (help) help.open = true; }
      ));
    } else {
      const ordered = items.ordered;
      const groups = items.groups;
      const bursts = items.bursts;
      newOrderedCount = ordered.length;
      // iter1 CRI-005 / iter2 G9: virtualize the rendered rows so a long
      // session keeps only the visible window in the DOM. iter2 G12 burst
      // coalescing + G10 upfront cap messaging ride the same renderer.
      //
      // iter3 CRI3-005: if this rebuild prepended rows at the head (a live
      // event arrived, filters unchanged, ordered grew), shift savedScroll
      // by `prepended * CHANGE_EST_ROW_H` so the virtualizer renders the
      // window that contains the SAME rows the user was reading (instead
      // of the same INDICES, which now point at different rows). Skip the
      // shift when the user was at the top (savedScroll === 0) — they
      // expect to see new events land at the top.
      if (
        savedScroll > 0 && filtersUnchanged && ordered.length > prevOrderedCount
      ) {
        const prepended = ordered.length - prevOrderedCount;
        effectiveSavedScroll = savedScroll + prepended * CHANGE_EST_ROW_H;
      }
      // F10: resolve the pending reveal to its owning item index. Exact id
      // only — no substring, no row-text match. A grouped/burst owner is
      // expanded by the window's focus step below.
      let reveal = null;
      if (revealRequest) {
        state.pendingChangeReveal = null;
        const revealIndex = ordered.findIndex((item) =>
          (item.row && String(item.row.id || "") === revealRequest) ||
          (item.group && (groups[item.group] || []).some((m) => String(m.id || "") === revealRequest)) ||
          (item.burst && (bursts[item.burst] || []).some((m) => String(m.id || "") === revealRequest)));
        if (revealIndex >= 0) {
          reveal = { index: revealIndex, aid: revealRequest };
          revealFired = true;
        } else {
          list.insertBefore(revealUnavailableNode(
            "That change isn't available in the retained history. Older activity rotates out as new events arrive."),
            list.firstChild);
        }
      }
      renderWindowedChanges(list, ordered, groups, {
        savedScroll: effectiveSavedScroll, savedFocus, body, bursts, reveal,
      });
    }
    body.appendChild(list);
    // Remember this render's bookkeeping for the NEXT render's prepend
    // detection (CRI3-005). Snapshot the filters too so a filter change
    // (which can also change ordered.length) is not misread as a prepend.
    _lastChangeListOrderedCount = newOrderedCount;
    _lastChangeListFilters = {
      actor: state.filters.actor,
      action: state.filters.action,
      concept: state.filters.concept,
    };
    // iter2 CRI2-003: restore the panel scroll offset + filter focus captured
    // before the rebuild. The virtualizer (renderWindowedChanges) positions
    // its initial window at the rows visible at savedScroll, so setting
    // scrollTop here lands at the same reading position without a flash.
    // iter3 CRI3-005: when a prepend shifted the window, restore the
    // SHIFTED offset so the user keeps looking at the same row. F10: a
    // fired reveal owns scroll + focus for this render, so the generic
    // restore must not fight it.
    if (rows.length && !revealFired && typeof effectiveSavedScroll === "number") {
      body.scrollTop = effectiveSavedScroll;
    } else if (!revealFired && typeof savedScroll === "number") {
      body.scrollTop = savedScroll;
    }
    if (revealRequest && !revealFired) {
      const note = list.querySelector(".okf-panel-reveal-note");
      if (note) {
        // Deferred: openPanel's post-render title focus (the panel-transition
        // path) must not steal the terminal explanation's focus.
        requestAnimationFrame(() => { try { note.focus(); } catch (e) {} });
      }
    }
    if (savedFocus) {
      const sel = savedFocus.ariaLabel
        ? body.querySelector(savedFocus.tag + '[aria-label="' + savedFocus.ariaLabel + '"]')
        : null;
      if (sel) {
        try { sel.focus({ preventScroll: true }); } catch (e) {}
        try {
          if (typeof sel.setSelectionRange === "function" && savedFocus.selStart != null) {
            sel.setSelectionRange(savedFocus.selStart, savedFocus.selEnd, savedFocus.selDir || "none");
          }
        } catch (e) {}
      }
    }
  }

  // ---- change-list virtualization (iter1 CRI-005 → iter2 G9) -----------
  // iter1 shipped incremental-prepend windowing: the first N rows rendered,
  // then a bottom sentinel IntersectionObserver appended more as the user
  // scrolled, but rows were never REMOVED, so a long session left hundreds
  // of DOM nodes attached. iter2 G9 replaces it with TRUE virtualization:
  // only a sliding window of ~CHANGE_VIRTUAL_WINDOW rows is ever in the DOM,
  // driven by top + bottom IntersectionObserver sentinels; top/bottom
  // spacers (sized by a measured row height) maintain the scrollbar
  // geometry so the user can scroll through the full history. A display
  // cap (CHANGE_HARD_CAP) gates the visible count with an UPFRONT "Showing
  // first N of M. Show all" note (G10 — shown before the user scrolls, not
  // after). The absolute virtualization ceiling is CHANGE_TOTAL_CAP (10000,
  // configurable via window.OKF_LOOM_CHANGE_TOTAL_CAP); upsertEvent caps the
  // in-memory log to match so older entries rotate out of state cleanly.
  // (Constants declared above renderChangeList — iter3 CRI3-005.)

  function renderWindowedChanges(list, ordered, groups, restore) {
    restore = restore || {};
    const bursts = restore.bursts || Object.create(null);  // iter2 G12 burst members
    const scrollContainer = restore.body || list;  // .okf-panel__body (overflow-y: auto)
    const savedScroll = (typeof restore.savedScroll === "number") ? restore.savedScroll : 0;
    // F10: an exact-identity reveal request. It may reach past the display
    // cap (the explicit jump raises the ceiling exactly like "Show more")
    // and repositions the initial window onto the owning row.
    const reveal = restore.reveal || null;
    const revealIndexValid = !!(reveal && Number.isInteger(reveal.index) &&
      reveal.index >= 0 && reveal.index < ordered.length);

    // G10: the display cap. If the filtered set exceeds CHANGE_HARD_CAP,
    // surface the "Showing first N of M. Show all" note UP FRONT (before the
    // user scrolls), and virtualize only the first N until the user asks for
    // all. "Show all" raises the virtualization ceiling to CHANGE_TOTAL_CAP
    // (the absolute max; rows beyond it are rotated out of state by
    // upsertEvent).
    let virtualTotal = Math.min(ordered.length, CHANGE_HARD_CAP);
    let capped = ordered.length > CHANGE_HARD_CAP;
    if (revealIndexValid && reveal.index >= virtualTotal) {
      virtualTotal = Math.min(ordered.length, CHANGE_TOTAL_CAP);
      capped = ordered.length > CHANGE_TOTAL_CAP;
    }
    if (capped) {
      const note = el("div", { class: "okf-changes__cap-note" });
      note.appendChild(document.createTextNode(
        "Showing first " + CHANGE_HARD_CAP + " of " + ordered.length + " events. "));
      const show = el("button", { type: "button", text: "Show more" });
      show.addEventListener("click", () => {
        // Raise the ceiling to the absolute cap and re-virtualize the full
        // tail. The note stays (it now reads as the absolute ceiling).
        note.parentNode && note.parentNode.removeChild(note);
        virtualTotal = Math.min(ordered.length, CHANGE_TOTAL_CAP);
        capped = ordered.length > CHANGE_TOTAL_CAP;
        if (capped) appendAbsoluteCapNote();
        rebuild();
      });
      note.appendChild(show);
      list.appendChild(note);
    }
    function appendAbsoluteCapNote() {
      const note = el("div", { class: "okf-changes__cap-note", role: "status" });
      note.appendChild(document.createTextNode(
        "Showing the most recent " + CHANGE_TOTAL_CAP + " of " + ordered.length +
        " events. Older entries are in events.jsonl."));
      list.appendChild(note);
    }

    // Virtualization scaffold: topSpacer + window rows + bottomSpacer. The
    // spacers carry the scrollbar geometry (height = offscreen-rows *
    // rowH); the window rows are the only .okf-change nodes in the DOM.
    const topSpacer = el("div", { class: "okf-changes__spacer okf-changes__spacer--top", "aria-hidden": "true" });
    const bottomSpacer = el("div", { class: "okf-changes__spacer okf-changes__spacer--bottom", "aria-hidden": "true" });
    const topSentinel = el("div", { class: "okf-changes__sentinel okf-changes__sentinel--top", "aria-hidden": "true" });
    const bottomSentinel = el("div", { class: "okf-changes__sentinel okf-changes__sentinel--bottom", "aria-hidden": "true" });
    const rowsHost = el("div", { class: "okf-changes__rows" });
    list.appendChild(topSpacer);
    list.appendChild(topSentinel);
    list.appendChild(rowsHost);
    list.appendChild(bottomSentinel);
    list.appendChild(bottomSpacer);

    let windowStart = 0;           // index of the first rendered row
    let rowH = CHANGE_EST_ROW_H;   // measured row height (updated after paint)
    const renderedIdx = new Map(); // index -> DOM node currently attached

    function clampStart(s) {
      const maxStart = Math.max(0, virtualTotal - windowSize());
      return Math.max(0, Math.min(s, maxStart));
    }
    function windowSize() {
      return Math.min(CHANGE_VIRTUAL_WINDOW, virtualTotal);
    }
    // syncWindow(start): make the rendered set exactly the window
    // [start, start + windowSize) (clamped). Removes rows that scrolled out,
    // adds rows that scrolled in, and resizes the spacers so the scrollbar
    // reflects the full virtualTotal * rowH content height.
    function syncWindow(start) {
      const want = clampStart(start);
      if (want === windowStart && renderedIdx.size > 0) return;
      windowStart = want;
      const last = Math.min(virtualTotal, want + windowSize());
      // Drop rows outside [want, last).
      Array.from(renderedIdx.keys()).forEach((i) => {
        if (i < want || i >= last) {
          const node = renderedIdx.get(i);
          if (node && node.parentNode) node.parentNode.removeChild(node);
          renderedIdx.delete(i);
        }
      });
      // Add missing rows in [want, last), in order.
      for (let i = want; i < last; i++) {
        if (renderedIdx.has(i)) continue;
        const node = changeItemOrGroup(ordered[i], groups, bursts);
        node.setAttribute("data-virtual-idx", String(i));
        // Insert before any later-rendered node; since we iterate ascending,
        // append unless a higher-index node already exists.
        let inserted = false;
        for (let j = i + 1; j < last; j++) {
          const after = renderedIdx.get(j);
          if (after) { rowsHost.insertBefore(node, after); inserted = true; break; }
        }
        if (!inserted) rowsHost.appendChild(node);
        renderedIdx.set(i, node);
      }
      topSpacer.style.height = (want * rowH) + "px";
      bottomSpacer.style.height = (Math.max(0, virtualTotal - last) * rowH) + "px";
    }
    // Measure the real row height once rows are painted, then re-sync so the
    // spacers match actual geometry (avoids scrollbar drift on wrap).
    function measureRowH() {
      const sample = rowsHost.firstElementChild;
      if (!sample) return;
      const h = sample.getBoundingClientRect().height;
      if (h > 12 && Math.abs(h - rowH) > 1) {
        rowH = h;
        // Re-sync spacer heights with the corrected height (windowStart unchanged).
        const last = Math.min(virtualTotal, windowStart + windowSize());
        topSpacer.style.height = (windowStart * rowH) + "px";
        bottomSpacer.style.height = (Math.max(0, virtualTotal - last) * rowH) + "px";
      }
    }

    // F10: give the revealed record useful focus. Exact data-activity-id
    // identity only; a grouped/burst owner is opened so the member row is
    // visible, then the member row itself is scrolled to, pulsed, and
    // focused. If the window estimate missed the row, nothing is pulsed —
    // never a different row.
    function focusRevealRow(aid) {
      const sel = '.okf-change[data-activity-id="' + cssEscape(String(aid)) + '"]';
      const target = rowsHost.querySelector(sel);
      if (!target) return;
      const owner = target.closest("details");
      if (owner && !owner.open) owner.open = true;
      target.scrollIntoView({ block: "center", behavior: REDUCED_MOTION ? "auto" : "smooth" });
      target.classList.remove("okf-pulse");
      void target.offsetWidth;
      target.classList.add("okf-pulse");
      if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
      try { target.focus({ preventScroll: true }); } catch (e) {}
    }

    // Initial window. Honour a saved scroll offset (G1): start the window at
    // the rows that would be visible at savedScroll so the restore doesn't
    // flash the top. F10: a reveal positions the window directly on its row.
    const initialStart = (revealIndexValid && reveal.index < virtualTotal)
      ? clampStart(reveal.index - CHANGE_VIRTUAL_OVERSCAN)
      : clampStart(Math.floor((savedScroll || 0) / rowH) - CHANGE_VIRTUAL_OVERSCAN);
    syncWindow(initialStart);
    requestAnimationFrame(measureRowH);
    if (revealIndexValid && reveal.aid != null) {
      requestAnimationFrame(() => { focusRevealRow(reveal.aid); });
    }

    // IntersectionObserver sentinels: when the user scrolls to either edge
    // of the window, slide it by CHANGE_VIRTUAL_STEP (re-sync removes the
    // rows that left + adds the new ones). root is the scroll container
    // (.okf-panel__body), NOT panelShell (the .okf-panel aside does not
    // scroll; using it was a latent bug in the iter1 observer).
    function shift(delta) { syncWindow(windowStart + delta); }
    const ioOpts = { root: scrollContainer, rootMargin: "120px" };
    const topIo = new IntersectionObserver((entries) => {
      for (const ent of entries) {
        if (ent.isIntersecting && windowStart > 0) shift(-CHANGE_VIRTUAL_STEP);
      }
    }, ioOpts);
    const bottomIo = new IntersectionObserver((entries) => {
      for (const ent of entries) {
        if (ent.isIntersecting && windowStart + windowSize() < virtualTotal) shift(CHANGE_VIRTUAL_STEP);
      }
    }, ioOpts);
    topIo.observe(topSentinel);
    bottomIo.observe(bottomSentinel);

    // rebuild(): used by "Show more" to re-virtualize against a raised
    // ceiling without rebuilding the whole panel (preserves scroll + focus).
    function rebuild() {
      // Reset the window against the new virtualTotal, keeping the current
      // scroll position.
      const curScroll = scrollContainer.scrollTop;
      renderedIdx.forEach((n) => { if (n.parentNode) n.parentNode.removeChild(n); });
      renderedIdx.clear();
      const start = clampStart(Math.floor(curScroll / rowH) - CHANGE_VIRTUAL_OVERSCAN);
      syncWindow(start);
      requestAnimationFrame(measureRowH);
    }
  }
  // Builds a change row/group node without appending (used by the loader).
  function changeItemOrGroup(item, groups, bursts) {
    if (item.row) return changeRow(item.row);
    if (item.burst) {
      // iter2 G12: a burst composite (10+ standalone events in 1s). Renders
      // as a <details> so the expand is native + keyboard-accessible (Enter/
      // Space toggles). The summary is one "Agent made N changes" line; the
      // body lists the individual change rows so the user can drill in.
      const members = bursts[item.burst];
      const det = el("details", { class: "okf-change okf-change--burst", dataset: { actor: members[0].actor } });
      const sum = el("summary", { class: "okf-change__summary" });
      sum.appendChild(el("span", { class: "okf-change__icon", "aria-hidden": "true" }));
      const actorRaw = (members[0] && members[0].actor) || "agent";
      const actor = actorRaw.charAt(0).toUpperCase() + actorRaw.slice(1);
      sum.appendChild(el("span", { text: actor + " made " + members.length + " changes in one burst. " }));
      sum.appendChild(el("span", { class: "okf-change__burst-hint", text: "Expand for detail" }));
      det.appendChild(sum);
      const body = el("div", { class: "okf-change__burst-body" });
      members.forEach((m) => body.appendChild(changeRow(m)));
      det.appendChild(body);
      return det;
    }
    const members = groups[item.group] || [];
    // F10: a grouped pass renders as a native <details> (same pattern as a
    // burst) so an exact-identity reveal can expand it and focus the owning
    // member row; members carry their own data-activity-id. The Undo-group
    // control stays on the summary row; its click suppresses the disclosure
    // toggle so activating Undo never expands/collapses the group.
    const det = el("details", { class: "okf-change okf-change--group", dataset: { actor: (members[0] && members[0].actor) || "agent" } });
    const sum = el("summary", { class: "okf-change__summary" });
    sum.appendChild(el("span", { class: "okf-change__icon", "aria-hidden": "true" }));
    sum.appendChild(el("span", { text: members.length + " changes · group pass. " }));
    sum.appendChild(el("span", { class: "okf-change__group-hint", text: "Expand for detail" }));
    const actions = el("span", { class: "okf-change__actions" });
    const undoBtn = undoControl("Undo group", (btn) => undoGroup(item.group, btn));
    undoBtn.addEventListener("click", (e) => { e.preventDefault(); });
    actions.appendChild(undoBtn);
    sum.appendChild(actions);
    det.appendChild(sum);
    const body = el("div", { class: "okf-change__group-body" });
    members.forEach((m) => body.appendChild(changeRow(m)));
    det.appendChild(body);
    return det;
  }

  function changeRow(r) {
    const actor = r.actor || r.origin || "system";
    const kind = r.type === "changed" ? "Saved" : r.type === "created" ? "Created" : r.type === "removed" ? "Removed" : null;
    const summary = r.summary || (kind ? (kind + " " + ((r.ids || [])[0] || "")) : (actor + " " + (r.action || "acted")));
    const row = el("div", { class: "okf-change", dataset: { actor: actor, activityId: String(r.id || "") } });
    row.appendChild(el("span", { class: "okf-change__icon", "aria-hidden": "true" }));
    const b = el("div", { class: "okf-change__body" });
    b.appendChild(el("div", { class: "okf-change__summary", text: summary }));
    const meta = el("div", { class: "okf-change__meta" });
    meta.appendChild(el("span", { text: actor + " · " + (r.action || r.type || "") + " · " + fmtTime(r.ts) }));
    (r.ids || []).forEach((id) => {
      meta.appendChild(document.createTextNode(" · "));
      const a = el("a", { href: "/" + id, text: id });
      meta.appendChild(a);
    });
    b.appendChild(meta);
    // R1 (INTENT2-007): if a comment_link event exists for this activity,
    // render a back-link to the originating comment. The comment_link event
    // is emitted by resolve_comment and carries (activity_id, comment_id).
    const link = state.events.find(
      (e) => e.type === "comment_link" && e.activity_id === r.id,
    );
    if (link) {
      const commentDiv = el("div", { class: "okf-change__comment-link" });
      commentDiv.appendChild(document.createTextNode("Resolved "));
      const cLink = el("a", {
        href: "#comment-" + link.comment_id,
        text: "comment " + (link.comment_id || "").slice(0, 10),
        title: "Jump to the comment this change resolved",
      });
      cLink.addEventListener("click", (ev) => {
        ev.preventDefault();
        revealComment(link.comment_id);
      });
      commentDiv.appendChild(cLink);
      b.appendChild(commentDiv);
    }
    row.appendChild(b);
    const actions = el("div", { class: "okf-change__actions" });
    // Only mutator-recorded activity carries undoable + a rev to restore;
    // disk/changed events have no group snapshot to undo here. F2: whether
    // the control is operative follows the session's own edit capability
    // (undoControl), not this event metadata.
    if (r.undoable && r.id && (r.ids && r.ids[0])) {
      actions.appendChild(undoControl("Undo", (btn) => undoOne(r, btn)));
    }
    row.appendChild(actions);
    return row;
  }

  // ---- Undo (§12.5): one entry point owns the action + its busy-state ---
  // F1: toast and drawer undo used to run as two divergent entry points.
  // The standalone/grouped toast callbacks discarded the clicked button, so
  // ``Btn.busy(undefined)`` threw synchronously before any request (no
  // /__undo ever left, the rejection was unhandled), while the coalesced
  // "Undo all" fired every helper CONCURRENTLY against one shared button
  // and each helper re-enabled it mid-batch. runUndoBatch is now the single
  // owner: the caller always forwards the REAL button, the button stays
  // disabled from the first request until the whole batch settles, targets
  // restore in one deterministic order, and terminal feedback reports
  // exactly what the server reported — restored, replay (409
  // already_undone), partial restore (some snapshots pruned, body ok:true),
  // or failure. Nothing here widens or infers authority: every request
  // still crosses the server's own denial + audit checks, and the server
  // remains the only writer of undo history.
  const UNDO_UNAVAILABLE_TEXT = "Undo is unavailable in read-only views";

  // Rev resolution shared by toast events and change-list rows: prefer the
  // content-hash snapshot pointer stamped by save_concept (``detail.before``
  // is the sha1[:12] of the PRIOR concept bytes - NOT the integer bundle
  // rev, which undo_snapshot cannot resolve). Fall back to the old ids+rev
  // behaviour ONLY when ``detail.before`` is absent (older events recorded
  // before the pointer was wired through); that fallback cannot resolve
  // server-side, which is why the content-hash pointer is required for an
  // operative undo.
  function undoTargetFor(e) {
    const before = (e.detail && e.detail.before) || null;
    const concept = before
      ? (e.detail.before_concept || (e.ids && e.ids[0]) || state.conceptId)
      : ((e.ids && e.ids[0]) || state.conceptId);
    return { concept, rev: before || e.rev || "" };
  }

  async function runUndoBatch(jobs, btn, opts) {
    const label = (opts && opts.label) || "Undo";
    Btn.busy(btn);
    let restored = 0, restoredFiles = 0, already = 0, partial = 0;
    const failures = [];
    const partialNotes = [];
    try {
      // Sequential, newest-first (LIFO): a later restore in one batch can
      // overwrite an earlier one when both jobs target the same concept, so
      // undoing newest-first settles overlapping targets on the state as it
      // was before the batch. Every restore is itself an attributed,
      // undoable write, so the audit trail records the whole settlement.
      for (const job of jobs.slice().reverse()) {
        const body = job.groupId
          ? { group_id: job.groupId }
          : { concept: job.concept, rev: job.rev };
        try {
          const res = await tokenFetch("/__undo", { method: "POST", body });
          const data = await res.json().catch(() => ({}));
          const detail = (data && typeof data.message === "string" && data.message)
            || (data && typeof data.error === "string" && data.error)
            || ("HTTP " + res.status);
          if (data && data.conflict && data.error === "already_undone") {
            already += 1; // the server's replay observation: nothing left to restore
          } else if (data && data.ok && data.partial) {
            // The partial-restore contract answers 409 with ok:true and the
            // pruned targets listed — a real outcome for the restored part,
            // but a PARTIAL job: it is counted separately and its
            // restored/pruned detail survives into the summary, including
            // alongside outright failures (F1 mixed reporting).
            partial += 1;
            restoredFiles += data.restored || 0;
            partialNotes.push(detail);
          } else if (data && data.ok) {
            restored += 1;
            restoredFiles += data.restored || 1;
          } else {
            failures.push(detail);
          }
        } catch (e) {
          failures.push((e && e.message) || String(e));
        }
      }
      if (jobs.length === 1) {
        const job = jobs[0];
        if (partial && job.groupId) {
          // A partial group pass is not a clean success: say what restored
          // and what was pruned, in the warn tone.
          toast("Group undo finished only partially. "
            + partialNotes.join(" "), { tone: "warn" });
        } else if (restored && job.groupId) {
          toast("Reverted group pass (" + restoredFiles + " file(s)).", { tone: "success" });
        } else if (restored) {
          toast("Undid change on " + job.concept + ".", { tone: "success" });
        } else if (already) {
          toast("Nothing to undo — the content already matches the earlier revision.", { tone: "success" });
        } else if (job.groupId) {
          toast("Group undo failed: " + failures.join("; "), { tone: "error" });
        } else {
          toast("Undo failed: " + failures.join("; "), { tone: "error" });
        }
      } else if (failures.length) {
        // Mixed settlement (F1): failures, complete jobs AND partial jobs
        // report together, with the pruned-snapshot detail retained — never
        // a bare count that hides unrestored changes behind a failure.
        toast("Undo finished with failures: " + restored + " undone"
          + (partial ? ", " + partial + " partial" : "")
          + (already ? ", " + already + " already undone" : "")
          + ", " + failures.length + " failed. " + failures[0]
          + (partialNotes.length ? " " + partialNotes.join(" ") : ""),
          { tone: "error" });
      } else if (partial) {
        toast("Undid " + restored + " change(s); " + partial
          + " only partially restored (some snapshots were pruned). "
          + partialNotes.join(" ")
          + (already ? " (" + already + " already undone)" : ""), { tone: "warn" });
      } else if (already && !restored) {
        toast("Nothing to undo — all " + already + " targets were already undone.", { tone: "success" });
      } else {
        toast("Undid " + restored + " change(s)."
          + (already ? " (" + already + " already undone)" : ""), { tone: "success" });
      }
    } finally {
      // Settled: only now does the shared button come back — never
      // mid-batch, never while a failure is still being reported.
      Btn.done(btn, label);
    }
  }

  function undoOne(r, btn) {
    if (!r || !r.id) return;
    runUndoBatch([undoTargetFor(r)], btn, { label: "Undo" })
      .catch((e) => console.error("[okf-studio] undo failed:", e));
  }
  function undoGroup(groupId, btn) {
    runUndoBatch([{ groupId }], btn, { label: "Undo group" })
      .catch((e) => console.error("[okf-studio] group undo failed:", e));
  }
  const Btn = {
    busy(b) { b.disabled = true; b.dataset._txt = b.textContent; b.textContent = "…"; },
    done(b, txt) { b.disabled = false; b.textContent = txt || b.dataset._txt || "Undo"; },
  };

  // Undo controls in the Changes list follow the session's own bootstrap
  // edit capability, never event metadata alone (F2). Where edit is
  // unavailable the history stays readable and the control renders
  // explicitly unavailable — disabled, with the reason in its accessible
  // name and tooltip — instead of inviting a click only the server can
  // refuse (403). The server's denial + audit behaviour is unchanged.
  function undoControl(label, onActivate) {
    const btn = el("button", { type: "button", class: "okf-change__undo", text: label });
    if (EDIT) {
      btn.addEventListener("click", () => onActivate(btn));
    } else {
      btn.disabled = true;
      btn.title = UNDO_UNAVAILABLE_TEXT + ".";
      btn.setAttribute("aria-label", label + " (" + UNDO_UNAVAILABLE_TEXT + ")");
    }
    return btn;
  }

  // ---- Agent availability ---------------------------------------------
  // Studio never runs an agent and the server has no liveness signal for one:
  // `wait` publishes nothing, and the presence slot can be set by anyone who
  // holds the session token (including the Watching toggle). So availability is
  // reported strictly as evidence — what an agent has DONE in this session — and
  // the presence flag is named as a flag, never as proof of a running process.
  //
  // Who counts as an agent: the claim / reply / mutator workflow is the external
  // agent's, and it signs with a label - the default "agent" or a named one such
  // as "aic/Polaris". The labels below are the ones the product writes for a
  // person or for its own plumbing; they are never agent evidence. This is a
  // reading of recorded labels, not a role or an authority check.
  function isAgentActor(actor) {
    return typeof actor === "string" && actor.trim() !== "" && ["user", "cli", "browser", "local-user"].indexOf(actor) < 0;
  }
  function agentName(actor) { return actor === "agent" ? "An agent" : "Agent \u201c" + actor + "\u201d"; }
  // An event vouches for an agent action only when it says whose action it was.
  // Comment events are row snapshots: only operation:"lifecycle" carries the
  // acting label and the mutation time. An unmarked comment event repeats the
  // comment's AUTHOR and CREATION time, so it never dates a claim or resolve.
  // Sharing rows never count, whatever label they carry (see isSharingEvent).
  function isAgentActionEvent(e) {
    if (!e || e.type === "presence" || isSharingEvent(e) || !isAgentActor(e.actor)) return false;
    return e.type !== "comment" || e.operation === "lifecycle";
  }
  function agentAvailability() {
    if (!CAN_READ_PRESENCE) {
      return { level: "unknown", text: "Agent availability is not shown in a shared session. Comments are saved for the operator’s agent." };
    }
    if (!state.eventsReady && !state.commentsReady) {
      return { level: "loading", text: "Checking for agent activity…" };
    }
    // Every date below is the time of an agent's own action:
    //   claimed_at      - when the CURRENT claim was taken (cleared on release);
    //   a reply's ts    - when an agent-authored row was written;
    //   a lifecycle or activity event's ts - when that attributed action ran.
    // updated_at is none of these: a person archiving or editing a row moves it,
    // so a retained claimed_by proves an agent once held the row and nothing
    // about when. That stays "time not recorded" rather than a borrowed date.
    let last = 0, lastTs = "", lastActor = "", undatedOwner = "";
    const seen = (ts, actor) => {
      const at = typeof ts === "string" ? Date.parse(ts) : NaN;
      if (!isNaN(at) && at > last) { last = at; lastTs = ts; lastActor = actor; }
      return !isNaN(at);
    };
    const holders = [];
    state.comments.forEach((c) => {
      if (!c || c._posting) return;
      if (isAgentActor(c.actor)) seen(c.ts, c.actor);
      if (!isAgentActor(c.claimed_by)) return;
      if (c.state === "claimed") {
        if (!c.archived) holders.push(c.claimed_by);
        if (!seen(c.claimed_at, c.claimed_by)) undatedOwner = undatedOwner || c.claimed_by;
      } else undatedOwner = undatedOwner || c.claimed_by;
    });
    (state.events || []).forEach((e) => { if (isAgentActionEvent(e)) seen(e.ts, e.actor); });
    if (holders.length) {
      const one = holders.every((h) => h === holders[0]) ? holders[0] : "agent";
      return { level: "active", text: agentName(one) + " has claimed " + holders.length + " comment" + (holders.length === 1 ? "" : "s") + " in this session." };
    }
    if (last) {
      return { level: "seen", text: agentName(lastActor) + " last acted in this session " + fmtTime(lastTs) + ". Whether it is still running is not known here." };
    }
    if (undatedOwner) {
      return { level: "seen", text: agentName(undatedOwner) + " has held a comment in this session; the time it last acted is not recorded. Whether it is still running is not known here." };
    }
    const flag = state.presence && state.presence.state;
    if (flag && flag !== "idle") {
      return { level: "flagged", text: "Agent presence is marked “" + flag + "”, but no agent has acted in this session. The mark is a status flag, not proof that an agent is running." };
    }
    return { level: "none", text: "No agent has acted in this session. Comments are saved and wait until an external agent picks them up." };
  }
  function refreshAgentAvailability() {
    const lines = document.querySelectorAll(".okf-agent-availability");
    if (!lines.length) return;
    const now = agentAvailability();
    lines.forEach((line) => {
      line.dataset.level = now.level;
      if (line.textContent !== now.text) line.textContent = now.text;
    });
  }
  function agentSetupNode(open) {
    const box = el("details", { class: "okf-agent-setup" });
    if (open) box.open = true;
    box.appendChild(el("summary", { text: "Connect an agent" }));
    box.appendChild(el("p", { text: "Studio does not start or host an agent. Run one yourself (for example a coding-agent session in a terminal) against this bundle, and have it loop:" }));
    const pre = el("pre", { class: "okf-agent-setup__commands", tabindex: "0", role: "group", "aria-label": "Agent loop commands" });
    pre.appendChild(el("code", { text: [
      "scripts/okf-loom wait <bundle> --for comment",
      "scripts/okf-loom comment-claim <bundle> <comment-id> --summary \"…\"",
      "# edit with the okf-loom mutators, then",
      "scripts/okf-loom comment-resolve <bundle> <comment-id> --summary \"…\"",
    ].join("\n") }));
    box.appendChild(pre);
    box.appendChild(el("p", { class: "okf-muted", text: "wait runs in the agent’s foreground: it blocks, prints one work item, and exits. The agent acts on it and calls wait again. Marking the agent as Watching does not do any of this." }));
    return box;
  }

  function emptyState(title, description, actionLabel, action) {
    const box = el("section", { class: "okf-empty-state", "aria-label": title });
    box.appendChild(el("h3", { class: "okf-empty-state__title", text: title }));
    box.appendChild(el("p", { class: "okf-empty-state__description", text: description }));
    if (typeof action === "string") {
      box.appendChild(el("a", { class: "okf-studiobtn okf-empty-state__action", href: action, text: actionLabel }));
    } else if (typeof action === "function") {
      const button = el("button", { type: "button", class: "okf-studiobtn okf-empty-state__action", text: actionLabel });
      button.addEventListener("click", action);
      box.appendChild(button);
    }
    return box;
  }

  function retryStudioData() {
    state.commentsStatus = "loading";
    state.commentsReady = false;
    state.eventsReady = false;
    if (state.openPanel === "comments") renderCommentsPanel();
    else if (state.openPanel === "changes") renderChangeList();
    else if (state.openPanel === "agent-activity") panels["agent-activity"].render(panelBody);
    void loadComments();
  }

  function dataStateNode(kind, label) {
    if (state.commentsStatus === "loading") {
      return el("div", { class: "okf-panel-state", role: "status", "aria-label": "Loading " + kind,
        "aria-live": "polite", "aria-busy": "true", text: "Loading " + kind + "…" });
    }
    const alert = el("div", { class: "okf-panel-state okf-panel-state--error", role: "alert" });
    alert.appendChild(el("p", { text: label + " could not be loaded. Your last confirmed data is unchanged." }));
    const retry = el("button", { type: "button", class: "okf-studiobtn", text: "Retry loading " + kind });
    retry.addEventListener("click", retryStudioData);
    alert.appendChild(retry);
    return alert;
  }

  // ====================================================================
  // 8. Command palette (§13.4)
  // ====================================================================
  let paletteState = { open: false, items: [], active: 0, lastReturn: null };
  function buildPalette() {
    const overlay = el("div", { class: "okf-palette-overlay", hidden: "", role: "dialog",
      "aria-modal": "true", "aria-label": "Command palette" });
    const pal = el("div", { class: "okf-palette" });
    const input = el("input", { type: "search", class: "okf-palette__input",
      placeholder: "Jump to a concept, run a command…", "aria-label": "Command palette input", "aria-autocomplete": "list" });
    const list = el("ul", { class: "okf-palette__list", role: "listbox", "aria-label": "Commands" });
    const hint = el("div", { class: "okf-palette__hint" }, [
      el("span", { text: "↑↓ navigate" }), el("span", { text: "⏎ select" }), el("span", { text: "Esc close" }),
    ]);
    pal.appendChild(input); pal.appendChild(list); pal.appendChild(hint);
    overlay.appendChild(pal);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closePalette(); });
    input.addEventListener("input", () => refreshPaletteList(input.value));
    list.addEventListener("click", (e) => {
      const li = e.target.closest(".okf-palette__item");
      if (!li) return;
      paletteState.active = +li.dataset.idx;
      activatePalette();
    });
    document.body.appendChild(overlay);
    paletteState.overlay = overlay;
    paletteState.input = input;
    paletteState.list = list;
  }
  // Global keyboard wiring (registered at boot, independent of the lazy DOM
  // build, so Ctrl/Cmd-K toggles the palette on first press and arrow/enter
  // navigation works once it is open).
    function wirePaletteKeys() {
      document.addEventListener("keydown", (e) => {
        const meta = e.ctrlKey || e.metaKey;
        if (meta && (e.key === "k" || e.key === "K")) {
          const editable = e.target && e.target.closest && e.target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='textbox']");
          if (editable && !paletteState.open) return;
          e.preventDefault(); togglePalette(); return;
        }
      if (!paletteState.open) return;
      // Honor a prior topmost consumer (panel/conflict/lightbox/theme menu)
      // that already preventDefault'd this Escape, and yield Escape to a
      // higher registered overlay so a single Escape closes only the
      // actually-topmost layer — never the palette plus the panel under it.
      if (e.defaultPrevented) return;
      if (e.key === "Escape") {
        if (!_overlayIsTopmost(paletteState.overlay)) return;
        e.preventDefault(); e.stopPropagation(); closePalette();
      }
      else if (e.key === "ArrowDown") { e.preventDefault(); movePalette(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); movePalette(-1); }
      else if (e.key === "Enter") { e.preventDefault(); activatePalette(); }
      // iter1 CRI-016: trap focus inside the modal so Tab/Shift+Tab can't
      // escape to the page behind (aria-modal="true" claims a trap).
      else if (e.key === "Tab") {
        e.preventDefault();
        trapFocusIn(paletteState.overlay, !e.shiftKey);
      }
    });
  }
  // iter1 CRI-016: focus trap shared by the palette and the slide-over panel.
  // Returns the focusable elements of a container in DOM order, skipping
  // hidden/disabled/negative-tabindex nodes.
  function focusableIn(root) {
    if (!root) return [];
    const sel = 'a[href], button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])';
    return $$(sel, root).filter((n) => {
      if (n.hasAttribute("hidden")) return false;
      const cs = getComputedStyle(n);
      return cs.display !== "none" && cs.visibility !== "hidden" && cs.pointerEvents !== "none";
    });
  }
  // Move focus forward (or backward) within `root`, wrapping at the edges.
  function trapFocusIn(root, forward) {
    const focusables = focusableIn(root);
    if (!focusables.length) { try { root.focus(); } catch (e) {} return; }
    const cur = document.activeElement;
    const idx = focusables.indexOf(cur);
    let next;
    if (idx < 0) next = focusables[0];
    else if (forward) next = focusables[(idx + 1) % focusables.length];
    else next = focusables[(idx - 1 + focusables.length) % focusables.length];
    try { next.focus({ preventScroll: true }); } catch (e) {}
  }
  // Shared overlay-stack guard (mirrors theme.js _isTopmost): a registered
  // overlay's Escape handler closes ONLY when that overlay is the actually-
  // topmost entry on window.OKFOverlayStack, so a single Escape never closes
  // two stacked layers (e.g. panel + palette). When the shared stack is
  // absent (no theme.js — a plain page), the overlay behaves as before.
  function _overlayIsTopmost(overlay) {
    if (!window.OKFOverlayStack) return true;
    var top = window.OKFOverlayStack.top();
    return !top || top === overlay;
  }
  function openPalette() {
    if (!paletteState.overlay) buildPalette();
    paletteState.open = true;
    paletteState.overlay.hidden = false;
    // Register on the shared overlay stack so the theme Appearance menu
    // (and any other registered Escape owner) yields to this aria-modal when
    // it is the actually-topmost overlay instead of restoring focus behind it.
    if (window.OKFOverlayStack) window.OKFOverlayStack.push(paletteState.overlay);
    paletteState.input.value = "";
    refreshPaletteList("");
    if (!state.graphReady) void loadGraph();
    paletteState._lastFocus = document.activeElement;
    setTimeout(() => paletteState.input.focus(), 20);
  }
  function closePalette() {
    if (!paletteState.overlay) return;
    paletteState.open = false;
    paletteState.overlay.hidden = true;
    if (window.OKFOverlayStack) window.OKFOverlayStack.remove(paletteState.overlay);
    if (paletteState._lastFocus && typeof paletteState._lastFocus.focus === "function") {
      try { paletteState._lastFocus.focus(); } catch (e) {}
    }
  }
  function togglePalette() { paletteState.open ? closePalette() : openPalette(); }
  function movePalette(delta) {
    const n = paletteState.items.length;
    if (!n) return;
    paletteState.active = (paletteState.active + delta + n) % n;
    renderPaletteList();
  }
  function activatePalette() {
    const item = paletteState.items[paletteState.active];
    if (!item) return;
    closePalette();
    try { item.run(); } catch (e) { console.error(e); }
  }
    function commandItems() {
      const items = [];
      items.push({ label: "Search this bundle", sub: "retrieval", run: () => { window.location.href = "/__search"; } });
      const contextOwner = document.querySelector("[data-context-concept]");
      if (contextOwner && typeof window.okfContextCopy === "function") {
        items.push({ label: "Copy concept context", sub: "cited Markdown; sends nothing to the agent", run: () => {
          window.okfContextCopy({ concept: contextOwner.dataset.contextConcept }, contextOwner, {});
        } });
      }
      const selectionContext = window.okfSelectionContext;
      if (selectionContext && selectionContext.selection_exact && contextOwner && typeof window.okfContextCopy === "function") {
        items.push({ label: "Copy selection with citation", sub: "cited passage; sends nothing to the agent", run: () => {
          window.okfContextCopy(selectionContext, contextOwner, {});
        } });
      }
        const searchState = typeof window.okfReadSearchContextState === "function"
          ? window.okfReadSearchContextState()
          : null;
        if (window.location.pathname === "/__search" && searchState &&
            (searchState.query || searchState.hasFilters) && document.querySelector(".okf-search-result") &&
            typeof window.okfContextCopy === "function") {
          const searchOwner = document.querySelector("[data-context-query]");
          if (searchOwner) items.push({ label: "Build context pack", sub: "cited passages, not a summary", run: () => {
            window.okfContextCopy({ query: searchState.query }, searchOwner, searchState.options);
          } });
        }
      const graphSelection = window.okfGraphSelection;
      const graphOwner = document.getElementById("okf-copy-neighbourhood");
      if (graphSelection && graphSelection.conceptId && graphOwner && typeof window.okfGraphContextCopy === "function") {
        items.push({ label: "Copy concept context", sub: graphSelection.conceptId + " \u00b7 cited Markdown; sends nothing to the agent", run: () => {
          window.okfGraphContextCopy(graphOwner, { neighbors: 1, budget: 6000 });
        } });
      }
      items.push({ label: "Switch view: Rendered", sub: "view mode", run: () => setView("rendered") });
    items.push({ label: "Switch view: Source", sub: "view mode", run: () => setView("source") });
    items.push({ label: "Switch view: Split", sub: "view mode", run: () => setView("split") });
    if (isConceptPage()) items.push({ label: state.focusView ? "Exit Focus view" : "Enter Focus view", sub: "view mode", run: () => setFocusView(!state.focusView) });
    if (CAN_COMMENT && hasCommentTarget(defaultCommentTarget())) items.push({ label: "Comment on this page", sub: "saved request or question", run: () => openPanel("comments", { focusComposer: true }) });
    items.push({ label: "Open Comments panel", sub: "panel", run: () => openPanel("comments") });
    items.push({ label: "Open Changes panel", sub: "panel", run: () => openPanel("changes") });
    if (AUTHORITY_MODE === "local-edit" && window.location.pathname !== FEEDBACK_IMPORT_PATH) {
      items.push({ label: "Import feedback note", sub: "local Studio", run: () => { window.location.href = FEEDBACK_IMPORT_PATH; } });
    }
    // Appearance choices delegate to theme.js (sole owner of theme state).
    {
      const api = themeApi();
      if (api && typeof api.setTheme === "function") {
        items.push({ label: "Appearance: Bone", sub: "palette (default)", run: () => api.setTheme("bone") });
        items.push({ label: "Appearance: Slate", sub: "palette", run: () => api.setTheme("slate") });
        items.push({ label: "Appearance: Clay", sub: "palette", run: () => api.setTheme("clay") });
        items.push({ label: "Appearance: Plum", sub: "palette", run: () => api.setTheme("plum") });
        items.push({ label: "Appearance: System", sub: "Bone in light mode, Slate in dark mode", run: () => api.setTheme("system") });
      }
    }
    if (window.okfLoomLive && window.okfLoomLive.resync) items.push({ label: "Resync now", sub: "live", run: () => window.okfLoomLive.resync() });
    // Registered panels.
    Object.keys(panels).forEach((id) => {
      const p = panels[id];
      if (p.__builtin) return;
      items.push({ label: "Open panel: " + p.label, sub: "extension", run: () => openPanel(id) });
    });
    return items;
  }
  function conceptItems(q) {
    const nodes = (state.graph && state.graph.nodes) || [];
    const ql = q.toLowerCase();
    return nodes.map((n) => {
      const d = n.data || n;
      return {
        label: d.label || d.id,
        sub: d.id + (d.type ? " · " + d.type : ""),
        run: () => { window.location.href = "/" + d.id; },
      };
    }).filter((it) => !ql || it.label.toLowerCase().indexOf(ql) >= 0 || it.sub.toLowerCase().indexOf(ql) >= 0)
      .slice(0, 12);
  }
    function refreshPaletteList(q) {
    q = (q || "").trim();
    let items;
    if (!q) {
      items = commandItems().concat(conceptItems("").slice(0, 6));
    } else {
      const cmds = commandItems().filter((c) => c.label.toLowerCase().indexOf(q.toLowerCase()) >= 0);
      items = cmds.concat(conceptItems(q));
    }
    paletteState.items = items;
    paletteState.active = 0;
      renderPaletteList();
    }
    window.addEventListener("okf-loom:graph-selection", function () {
      if (paletteState.open && paletteState.input) refreshPaletteList(paletteState.input.value);
    });
  function renderPaletteList() {
    const list = paletteState.list;
    list.innerHTML = "";
    paletteState.items.forEach((item, i) => {
      const li = el("li", { class: "okf-palette__item", role: "option",
        "aria-selected": i === paletteState.active ? "true" : "false", dataset: { idx: String(i) } });
      li.appendChild(el("span", { class: "okf-palette__item-title", text: item.label }));
      li.appendChild(el("span", { class: "okf-palette__item-sub", text: item.sub }));
      li.appendChild(el("span", { class: "okf-palette__item-kbd", text: i === paletteState.active ? "⏎" : "" }));
      list.appendChild(li);
    });
  }

  // ====================================================================
  // 9. Slide-over panel + extension registry (§13.6)
  // ====================================================================
  const panels = {}; // id → { id, label, render(container, ctx), __builtin }
  const panelOverlay = el("div", { class: "okf-panel-overlay", hidden: "" });
  const panelShell = el("aside", { id: "okf-panel", class: "okf-panel", hidden: "", role: "dialog",
    "aria-modal": "true", "aria-labelledby": "okf-panel-title", tabindex: "-1" });
  const panelHeader = el("div", { class: "okf-panel__header" });
  const panelBack = el("button", { type: "button", class: "okf-panel__back", "aria-label": "Back to Studio", text: "← Studio", hidden: "" });
  const panelTitle = el("h2", { id: "okf-panel-title", class: "okf-panel__title", tabindex: "-1" });
  // The visible label must name the action for touch users too — "Esc" was
  // a keyboard-only hint. Escape still closes (panel keydown handler below);
  // closePanel restores focus to the trigger (CRI-016).
  const panelClose = el("button", { type: "button", class: "okf-panel__close", "aria-label": "Close panel", text: "Close" });
  panelHeader.appendChild(panelBack); panelHeader.appendChild(panelTitle); panelHeader.appendChild(panelClose);
  // R3: one plain sentence under the title of each built-in collaboration
  // panel saying what question it answers. It is a paragraph (not a heading
  // and not live) and it describes the panel only while it is shown.
  // Studio home, Proposals (it has its own intro) and extension panels get
  // none; register() is unchanged, so an extension that replaces a built-in
  // id does not inherit a sentence written for the built-in.
  const panelPurpose = el("p", { id: "okf-panel-purpose", class: "okf-panel__purpose", hidden: "" });
  const PANEL_PURPOSE = {
    comments: "Requests and discussion saved on this bundle for readers and its agent. Exact annotations are pinned to text; page comments are about the whole page.",
    changes: "What actually happened: saved changes to the bundle, newest first. Comments and presence marks are not listed here.",
    // A session that cannot read presence shows no status chip and no
    // presence history, so its sentence promises neither.
    "agent-activity": CAN_READ_PRESENCE
      ? "Evidence of recent agent work: comments an agent claimed, reported status changes, and agent writes in the last 5 minutes. A status mark is not proof an agent is running."
      : "Evidence of recent agent work: comments an agent claimed and agent writes in the last 5 minutes.",
  };
  const purposePanels = new WeakSet();
  function syncPanelPurpose(id) {
    const p = panels[id];
    const text = p && purposePanels.has(p) ? (PANEL_PURPOSE[id] || "") : "";
    if (panelPurpose.textContent !== text) panelPurpose.textContent = text;
    panelPurpose.hidden = !text;
    if (text) panelShell.setAttribute("aria-describedby", "okf-panel-purpose");
    else panelShell.removeAttribute("aria-describedby");
  }
  const panelBody = el("div", { class: "okf-panel__body", id: "okf-panel-body" });
  panelShell.appendChild(panelHeader); panelShell.appendChild(panelPurpose); panelShell.appendChild(panelBody);
  document.body.appendChild(panelOverlay); document.body.appendChild(panelShell);
  // P1-3: floating "Back to document" peek bar for narrow viewports. When the
  // drawer covers the page during anchor selection, "Select in document"
  // collapses the drawer and surfaces this bar so the document is visible for
  // text selection; "Return to comment" restores the drawer with editor state
  // intact. Hidden by default; only shown while the drawer is peeked open.
  const reanchorPeek = el("div", { class: "okf-reanchor-peek", hidden: "", role: "status", "aria-live": "polite" });
  const reanchorPeekText = el("span", { class: "okf-reanchor-peek__text" });
  const reanchorPeekReturn = el("button", { type: "button", class: "okf-studiobtn okf-studiobtn--primary", text: "Return to comment" });
  reanchorPeek.appendChild(reanchorPeekText); reanchorPeek.appendChild(reanchorPeekReturn);
  document.body.appendChild(reanchorPeek);
  reanchorPeekReturn.addEventListener("click", _restoreReanchorFromPeek);
  panelOverlay.addEventListener("click", closePanel);
  panelClose.addEventListener("click", closePanel);
  let panelChildTrigger = null;
  panelBack.addEventListener("click", function () {
    const restore = panelChildTrigger;
    openPanel("studio", { returning: true });
    panelChildTrigger = null;
    if (restore && restore.isConnected) restore.focus();
  });
    document.addEventListener("keydown", (e) => {
      if (!state.openPanel) return;
      if (e.defaultPrevented) return;
      // Yield Escape to any higher registered overlay (palette/conflict/
      // lightbox/theme menu) so a single Escape closes only the actually-
      // topmost layer — never the panel plus the palette stacked on it. Tab
      // is left alone so each open layer keeps its own focus trap.
      if (e.key === "Escape" && !_overlayIsTopmost(panelShell)) return;
      // P1-3: while the drawer is peeked open (Back to document), Escape
      // returns to the comment instead of closing the panel, and Tab is left
      // alone so the user can move focus into the visible document to select
      // text rather than being trapped in the hidden drawer.
      if (state.annotationPeeked) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); _restoreReanchorFromPeek(); return; }
        if (e.key === "Tab") { return; }
      }
      // R5-13: the conflict alertdialog is explicitly the topmost modal.
      // When it is open it EXCLUSIVELY owns Escape/Tab (its own
      // _conflictKeydown handler traps focus and closes). The panel handler
      // must yield entirely so a single Escape does not close BOTH the
      // conflict dialog and the underlying panel, and so Tab is not
      // processed by both focus traps. _conflictKeydown is registered after
      // this listener but runs second in document-listener order; yielding
      // here is what prevents the double-process.
      if (conflictState && conflictState.open) return;
        // A desktop rail is non-modal and preserves native editable Escape.
        // A reflow dialog is topmost modal UI and owns Escape from every
        // editable surface, including contenteditable and role=textbox.
        if (e.key === "Escape" && isEditableTarget(e.target) &&
            document.body.classList.contains("okf-panel-rail")) return;
      if (e.key === "Escape" && state.annotationEditor) {
        e.preventDefault();
        e.stopPropagation();
        if (state.annotationEditor.mode === "saving") {
          state.annotationEditor.message = "Checking the saved result…";
          const activeComment = state.comments.find(function (item) { return item.id === state.annotationEditor.commentId; });
          if (activeComment) void reconcileReanchor(activeComment);
        } else closeReanchorEditor(true);
        return;
      }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closePanel(); }
    // iter1 CRI-016: trap focus inside the slide-over panel so Tab can't
    // reach the page behind while aria-modal="true" is claimed.
      // R6-7: in rail mode the drawer is a non-modal complementary landmark
      // beside an interactive document; Tab must reach the document, so the
      // trap is disarmed. The narrow overlay keeps aria-modal="true" and the
      // trap (the panel covers the page).
      else if (e.key === "Tab") {
        if (document.body.classList.contains("okf-panel-rail")) return;
        e.preventDefault(); trapFocusIn(panelShell, !e.shiftKey);
      }
    });
    // U5: Escape exits Reading Focus when no higher-priority overlay owns
    // Escape. Ownership map (checked in real event order, no reordering of
    // existing handlers):
    //   1. e.defaultPrevented — any earlier topmost consumer that claimed
    //      this Escape wins (panel/palette/conflict handlers close their own
    //      layer and preventDefault; the selection-affordance capture handler
    //      likewise, so an active selection action is never double-processed).
    //   2. state.openPanel / paletteState.open / conflictState.open — the
    //      drawer, composer, palette and conflict alertdialog each own their
    //      own Escape; Reading Focus stays.
    //   3. window.OKFOverlayStack.top() — any registered overlay (theme
    //      Appearance menu, lightbox, future aria-modal) is above this page;
    //      yield.
    //   4. The Sections popover (document-outline.js) closes on Escape inside
    //      its own wrap with stopPropagation, so this document handler never
    //      sees that Escape; the wiki link-preview tooltip (wiki.js) hides on
    //      Escape without preventing default, so a visible tooltip is yielded
    //      to explicitly — one Escape, one dismissed layer.
    // Exiting runs the canonical setFocusView(false) writer (body class,
    // aria-pressed, URL ?focus removal) and restores focus to the connected
    // Focus control in the pinned viewbar. Single-file/doc-mode pages never
    // run this module, and Reading Focus is page-local, so other routes are
    // untouched.
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (!state.focusView || !document.body.classList.contains("okf-focus-view")) return;
      if (state.openPanel || state.commentLifecycle === "selection-action") return;
      if (paletteState.open || (conflictState && conflictState.open)) return;
      if (window.OKFOverlayStack && window.OKFOverlayStack.top()) return;
      const preview = document.getElementById("okf-popover");
      // wiki.js shows/hides the tooltip strictly via style.display "block"/"none".
      if (preview && preview.style.display === "block") return;
      e.preventDefault();
      e.stopPropagation();
      setFocusView(false);
      if (focusViewBtn.isConnected && focusViewBtn.getClientRects().length > 0) {
        try { focusViewBtn.focus({ preventScroll: true }); } catch (err) {}
      }
    });
  function panelBodyEl() { return panelBody; }
  // iter1 CRI-016: remember the trigger so focus is restored on close.
  let panelLastFocus = null;
  // F5: a focus origin/restore target must be a real, visible, activatable
  // control. document.activeElement can be <body> (activating a
  // non-focusable palette option, programmatic opens) or an already-hidden
  // node; capturing it made closePanel "restore" focus onto <body> and drop
  // the keyboard position entirely. Both the capture in openPanel and the
  // restore decision in closePanel share this one validity rule.
  function isValidPanelFocusOrigin(node) {
    return !!node && node !== document.body && typeof node.closest === "function" &&
      typeof node.matches === "function" &&
      node.isConnected && !node.closest("[hidden], [aria-hidden='true']") &&
      node.getClientRects().length > 0 &&
      node.matches("a[href], button, input, textarea, select, details, summary, [tabindex]");
  }

  function panelToggleButtons() {
    return [studioLauncher, commentsBtn, changesBtn].concat(
      Object.keys(panels).map((key) => panels[key] && panels[key]._btn).filter(Boolean)
    );
  }
  function openPanel(id, opts) {
    opts = opts || {};
    const p = panels[id];
    if (!p) return;
    if (id === "comments" && state.commentLifecycle !== "anchored-composer") {
      const draftAnchor = state.draftAnchor || {};
      // A page draft that carries retained selection context is still an
      // anchored draft: reopening the panel must not reset it away.
      transitionCommentLifecycle(draftAnchor.kind && !isPlainPageDraft(draftAnchor) ? "anchored-composer" : "concept-composer");
    }
    const previousPanel = state.openPanel;
    if (previousPanel === "studio" && id !== "studio" && document.activeElement && panelShell.contains(document.activeElement)) {
      panelChildTrigger = document.activeElement;
    }
    state.openPanel = id;
    panelShell.hidden = false;
    panelOverlay.hidden = false;
    // Register the slide-over on the shared overlay stack (see openPalette).
    if (window.OKFOverlayStack) window.OKFOverlayStack.push(panelShell);
    panelTitle.textContent = p.label;
    syncPanelPurpose(id);
    panelBack.hidden = !(id !== "studio" && panelChildTrigger);
    // Update aria-expanded on every built-in and extension toggle button.
    panelToggleButtons().forEach((b) => b.setAttribute("aria-expanded", "false"));
    if (id === "studio") studioLauncher.setAttribute("aria-expanded", "true");
    const tb = ({ comments: commentsBtn, changes: changesBtn })[id] || p._btn;
    if (tb) tb.setAttribute("aria-expanded", "true");
    if (id === "changes") acknowledgeChanges();
    // Save the trigger so closePanel can restore focus (CRI-016). F5: keep
    // the origin only when it is a valid control — an incidental <body>
    // activeElement (palette activation, programmatic open) must never
    // become the restore target. Launcher-initiated opens capture the real
    // launcher (activation focuses it); for other opens closePanel's
    // per-panel fallbacks own the safe restore.
    if (!panelLastFocus) {
      const origin = document.activeElement;
      panelLastFocus = isValidPanelFocusOrigin(origin) ? origin : null;
    }
    // Render. The wipe destroys any outgoing panel DOM, including an open
    // inline reply/edit composer and its in-flight POST (F3). Destruction is
    // deliberately NOT treated as cancellation — a destroyed composer is not
    // evidence the server mutation was canceled — so the departed callback
    // may still settle canonically (upsert/render), while its draft-store
    // outcome and cleanup are fenced to the destroyed session's ownership
    // revision and can never erase a replacement editor's newer draft or
    // intent. Uncertain dispatched intents survive in the draft owner.
    panelBody.innerHTML = "";
    panelBody._focusComposer = !!opts.focusComposer;
    // Per-panel geometry hook: a panel registered with `wide: true` (the
    // Proposals diff workspace) is wider than the 440px drawer. Set before
    // the rail sync so the rail measures the real width.
    panelShell.dataset.okfPanel = id;
    if (p.wide === true) panelShell.dataset.okfPanelWide = "true";
    else delete panelShell.dataset.okfPanelWide;
    _syncPanelRail();
    try { p.render(panelBody, ctx()); } catch (e) { console.error("[okf-studio] panel render", e); }
    try { panelTitle.focus(); } catch (e) {}
  }
  function closePanel() {
    const closingComments = state.openPanel === "comments";
    const liveComposer = closingComments && document.querySelector(".okf-composer__textarea");
    if (liveComposer) state.draftBody = liveComposer.value;
    if (closingComments) {
      // Retire the floating selection action and its captured draft in the
      // same synchronous close, and dismiss the native selection that fed
      // it: a surviving selection would let a late debounced selectionchange
      // recreate the affordance over the "closed" lifecycle. Retiring before
      // the focus restore also stops a still-visible affordance from being
      // restored as the focus owner it just stopped being. draftBody, the
      // composer anchor, and the focus restoration below are untouched.
      releaseSelectionAction();
      try { window.getSelection().removeAllRanges(); } catch (e) {}
    }
    state.openPanel = null;
    panelShell.hidden = true;
    panelOverlay.hidden = true;
    delete panelShell.dataset.okfPanel;
    delete panelShell.dataset.okfPanelWide;
    if (window.OKFOverlayStack) window.OKFOverlayStack.remove(panelShell);
    _syncPanelRail();
    panelToggleButtons().forEach((b) => b.setAttribute("aria-expanded", "false"));
    // Restore a connected launcher/origin (F5: same validity rule as the
    // capture — focusing a non-focusable or hidden node was already a
    // silent no-op, so routing those to the fallback is strictly more
    // honest). Nested Studio controls are removed with panel content, so
    // the persistent shell launcher is the safe owner.
    const candidateVisible = isValidPanelFocusOrigin(panelLastFocus);
    const restore = candidateVisible ? panelLastFocus : (closingComments ? null : studioLauncher);
    if (closingComments) transitionCommentLifecycle("closed");
    if (restore && restore.isConnected && typeof restore.focus === "function") {
      try { restore.focus({ preventScroll: true }); } catch (e) { restore.focus(); }
    } else if (closingComments) restoreCommentOriginFocus();
    panelLastFocus = null;
    panelChildTrigger = null;
  }
  // P1-1: at wide viewports the comments drawer reflows as a real layout
  // column (body padding-right reserves its width) so the toolbar, nav, and
  // authored concept content are not clipped beneath it. At narrow widths
  // the panel is a full-width overlay and no space is reserved. Rail mode is
  // derived from actual rendered geometry (panel visible AND narrower than
  // the viewport), so it tracks the CSS panel-width rule exactly without a
  // second hardcoded breakpoint.
    // Space priority on a concept page: the article keeps ARTICLE_MIN first.
    // Related yields (moves below the article) before the article is
    // squeezed; Comments is a side-by-side rail only if the article still
    // keeps that minimum once Related has yielded, and overlays otherwise.
    // Both decisions read measured geometry, so a user-resized Related rail
    // and the page's real padding count.
    var ARTICLE_MIN = 600;
    var relatedDesktopQuery = window.matchMedia("(min-width: 901px)");
    var relatedSideWidth = 0;
    function _pageInlinePadding(page) {
      var cs = getComputedStyle(page);
      return (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
    }
    function _syncRelatedRail() {
      var page = $(".okf-page"), sidebar = $(".okf-page__sidebar");
      if (!page || !sidebar) return;
      var below = page.dataset.okfRelated === "below";
      // Under 901px CSS already stacks; Focus view hides the rail entirely.
      if (!relatedDesktopQuery.matches || getComputedStyle(sidebar).display === "none") {
        if (below) delete page.dataset.okfRelated;
        return;
      }
      if (!below) relatedSideWidth = sidebar.getBoundingClientRect().width;
      var side = relatedSideWidth || 260;
      var gap = parseFloat(getComputedStyle(page).columnGap) || 0;
      var article = page.clientWidth - _pageInlinePadding(page) - side - gap;
      // Hysteresis so a scrollbar appearing with the taller stacked layout
      // cannot flip the decision back and forth.
      var yields = article < ARTICLE_MIN + (below ? 16 : 0);
      if (yields && !below) page.dataset.okfRelated = "below";
      else if (!yields && below) delete page.dataset.okfRelated;
    }
    function _syncPanelRail() {
      var open = !!state.openPanel && !panelShell.hidden;
      var panelW = panelShell.offsetWidth || 0;
      // Reserve a rail only when the remaining authored column keeps its
      // readable minimum. Mid-size screens use the modal handoff instead of
      // crushing navigation/article geometry beside a 440px rail.
      var conceptPage = $(".okf-page");
      var remaining = window.innerWidth - panelW - (conceptPage ? _pageInlinePadding(conceptPage) : 0);
      var rail = open && panelW > 0 && panelW < window.innerWidth - 4 &&
        (conceptPage ? remaining >= ARTICLE_MIN : (window.innerWidth >= 1200 && remaining >= 620));
      document.body.classList.toggle("okf-panel-rail", rail);
      // The reserved rail tracks the measured drawer width, so a wider panel
      // (Proposals) never overlaps the document it sits beside.
      if (rail) document.body.style.setProperty("--okf-panel-rail-w", panelW + "px");
      else document.body.style.removeProperty("--okf-panel-rail-w");
      // R6-7: rail vs overlay semantics. In rail mode the drawer sits BESIDE a
      // visibly interactive document, so it is a non-modal complementary
      // landmark (role=complementary, no aria-modal) and MUST NOT trap Tab —
      // keyboard users reach the authored document beside it. In the narrow
      // overlay the drawer covers the page, so it reverts to a modal dialog
      // (role=dialog, aria-modal=true) with the focus trap re-armed. Only
      // mutate these while open; when closed the defaults are harmless (the
      // panel is hidden), and openPanel restores dialog/modal before this runs.
      if (open) {
        if (rail) {
          panelShell.setAttribute("role", "complementary");
          panelShell.removeAttribute("aria-modal");
        } else {
          panelShell.setAttribute("role", "dialog");
          panelShell.setAttribute("aria-modal", "true");
        }
      } else {
        panelShell.setAttribute("role", "dialog");
        panelShell.setAttribute("aria-modal", "true");
      }
    // Re-evaluate the selection banner geometry now that rail state is set.
    // (_updateBannerGeometry lives in a nested scope, so mirror its rule here:
    // in rail mode the body padding already reserves the drawer, so clear the
    // banner's own margin; otherwise reserve via margin for a partial overlay.)
    var banner = document.getElementById("okf-annotation-selection-banner");
    if (banner) banner.style.marginRight = rail ? "" : ((panelW && panelW < window.innerWidth - 4) ? panelW + "px" : "");
    // If the drawer is peeked (Back to document), a rail resize must not
    // strand the peek bar; _syncReanchorPeek re-evaluates on the same tick.
    if (!open) _dismissReanchorPeek();
  }
  window.addEventListener("resize", function () { _syncPanelRail(); _syncRelatedRail(); _syncReanchorPeek(); });
  (function () {
    var page = $(".okf-page"), sidebar = $(".okf-page__sidebar");
    if (!page || !sidebar) return;
    if (typeof ResizeObserver === "function") {
      var ro = new ResizeObserver(function () { _syncRelatedRail(); });
      ro.observe(page);
      ro.observe(sidebar);
    }
    _syncRelatedRail();
  })();
  function togglePanel(id) { state.openPanel === id ? closePanel() : openPanel(id); }

  function ctx() {
    return {
      state, boot: BOOT, el, toast, tokenFetch,
      currentConceptId: state.conceptId,
      onLive: (type, fn) => window.okfLoomLive && window.okfLoomLive.on(type, fn),
    };
  }

  // register(kind, impl) - the documented client extension seam (§13.6).
  // iter1 CRI-007: the previously-documented {toolbar, graphDecorator,
  // suggestionRenderer} kinds were inert no-op stubs that over-promised the
  // extension surface. They are now RESERVED (accepted + warned, not wired)
  // and documented as forward-compat in the module header. Only {panel,
  // viewMode} ship wired today: `panel` powers the built-in Comments /
  // Changes / Agent-activity panels; `viewMode` lets an extension register
  // an additional read-only view mode (e.g. "outline") that joins the
  // Rendered/Source/Split switch + the ?view= deep link.
  function register(kind, impl) {
    if (kind === "panel" && impl && impl.id) {
      panels[impl.id] = Object.assign({ __builtin: false }, impl);
      // Add a bar toggle for non-builtin panels.
      if (!impl.__builtin && !impl._btn) {
        const btn = el("button", { type: "button", class: "okf-studiobtn", "aria-expanded": "false", "aria-controls": "okf-panel", text: impl.label });
        btn.addEventListener("click", () => togglePanel(impl.id));
        impl._btn = btn;
        panels[impl.id]._btn = btn;
        rightGroup.insertBefore(btn, paletteBtn);
      }
      if (state.openPanel === impl.id) openPanel(impl.id);
      return impl;
    }
    if (kind === "viewMode" && impl && impl.id) {
      viewModes[impl.id] = impl;
      addViewModeButton(impl);
      return impl;
    }
    // Reserved / forward-compat kinds. Accepted so an extension that targets
    // a future toolkit version doesn't throw, but warned so the author knows
    // the hook isn't wired yet. See viewer/OVERRIDES.md §6 for the full API.
    if (kind === "toolbar" || kind === "graphDecorator" || kind === "suggestionRenderer") {
      console.warn("[okf-studio] register('" + kind + "') is reserved for a future release and is not wired in this version. See viewer/OVERRIDES.md §6 for the documented extension surface.");
      return impl;
    }
    console.warn("[okf-studio] register: unknown kind or missing impl", kind);
  }
  const viewModes = {};
  function addViewModeButton(mode) {
    // Insert a new button into the view switch group. It activates the
    // extension mode; the extension's onActivate is responsible for any
    // custom rendering (e.g. swapping in an outline panel).
    if (!isConceptPage()) return;
    const btn = el("button", { type: "button", class: "okf-viewswitch__btn",
      "aria-pressed": "false", text: mode.label || mode.id });
    btn.dataset.mode = "ext:" + mode.id;
    btn.addEventListener("click", () => {
      try { if (typeof mode.onActivate === "function") mode.onActivate(ctx()); } catch (e) { console.error(e); }
      // Mark only this extension button pressed; clear the built-in trio.
      $$(".okf-viewswitch__btn").forEach((b) => b.setAttribute("aria-pressed", "false"));
      btn.setAttribute("aria-pressed", "true");
    });
    if (viewSwitch) viewSwitch.appendChild(btn);
  }

  // Built-in panels.
  panels.studio = {
    id: "studio", label: "Studio",
    render: function (container) {
      container.innerHTML = "";
      const intro = el("div", { class: "okf-studio-home__intro" });
      // R3: the home status is the live-updates connection only. Permission
      // lives under "Authority and sharing" and counts on the launcher, so
      // "Live" here can never be read as "editable". updateBadges() is the
      // one writer that keeps this node current while it exists.
      studioHomeStatus = el("p", { class: "okf-studio-home__status", text: connectionStatusText() });
      intro.appendChild(studioHomeStatus);
      intro.appendChild(el("p", { class: "okf-muted", text: EDIT ? "Review activity and direct changes within this session’s permissions." : CAN_COMMENT ? "Read, search and leave comments for the bundle’s agent. This shared view cannot edit content." : "Read, search, explore relationships, and copy cited passages. This view is read-only." }));
      container.appendChild(intro);
      if (!CAN_CONTROL_SESSION) {
        const help = el("details", { class: "okf-authoring-help" });
        help.appendChild(el("summary", { text: "Authoring unavailable here" }));
        help.appendChild(el("p", { text: "An operator must open a local editable Studio and connect an agent to its comment loop. This shared view cannot start an agent or grant permissions. Ask the bundle owner for an authoring session." }));
        container.appendChild(help);
      }

      const session = el("section", { class: "okf-studio-home__section", "aria-labelledby": "okf-studio-session-title" });
      session.appendChild(el("h3", { id: "okf-studio-session-title", class: "okf-panel__section-title", text: "Session" }));
      session.appendChild(leftGroup);
      if (CAN_READ_PRESENCE || CAN_CONTROL_SESSION) container.appendChild(session);

      const work = el("section", { class: "okf-studio-home__section", "aria-labelledby": "okf-studio-work-title" });
      work.appendChild(el("h3", { id: "okf-studio-work-title", class: "okf-panel__section-title", text: "Comments and history" }));
      work.appendChild(rightGroup);
      container.appendChild(work);

      // X2: portable feedback notes are imported only by the local editing
      // operator. The page itself re-checks locality (a tunnel refuses it).
      if (AUTHORITY_MODE === "local-edit") {
        const notes = el("section", { class: "okf-studio-home__section", "aria-labelledby": "okf-studio-notes-title" });
        notes.appendChild(el("h3", { id: "okf-studio-notes-title", class: "okf-panel__section-title", text: "Feedback notes" }));
        notes.appendChild(el("p", { class: "okf-muted", text: "Readers of an exported copy can send you a note file about one page. Check it before it becomes a comment." }));
        const importLink = el("a", { class: "okf-studiobtn okf-studio-import-link", href: FEEDBACK_IMPORT_PATH, text: "Import feedback note" });
        if (window.location.pathname === FEEDBACK_IMPORT_PATH) importLink.setAttribute("aria-current", "page");
        notes.appendChild(importLink);
        container.appendChild(notes);
      }

      if (isConceptPage() && EDIT) {
        // These actions start comments, not X3 proposals: the id says so.
        const intents = el("section", { class: "okf-studio-home__section", "aria-labelledby": "okf-studio-intents-title" });
        intents.appendChild(el("h3", { id: "okf-studio-intents-title", class: "okf-panel__section-title", text: "Ask the agent to improve this concept" }));
        intents.appendChild(el("p", { class: "okf-muted", text: "Each action starts a comment with a suggested request. Nothing edits Markdown until an agent picks it up." }));
        buildIntentsPanel(intents);
        container.appendChild(intents);

        const composer = el("section", { class: "okf-studio-home__section", "aria-labelledby": "okf-proposal-composer-title" });
        composer.appendChild(el("h3", { id: "okf-proposal-composer-title", class: "okf-panel__section-title", text: "Proposal review" }));
        renderProposalComposer(composer);
        if (PROPOSAL_INSPECTION !== "off") {
          const inspect = el("button", { type: "button", class: "okf-studiobtn okf-proposal-inspect-entry",
            "aria-controls": "okf-panel", text: "Inspect proposals" });
          inspect.addEventListener("click", () => openPanel(PROPOSAL_PANEL_ID));
          composer.appendChild(inspect);
        }
        container.appendChild(composer);
      }

      const authority = el("section", { class: "okf-studio-home__section", "aria-labelledby": "okf-studio-authority-title" });
      authority.appendChild(el("h3", { id: "okf-studio-authority-title", class: "okf-panel__section-title", text: "Authority and sharing" }));
      const authorityMode = AUTHORITY_MODE;
      const authorityCopy = {
        "local-edit": "Local editing authority is enabled. Public sharing remains operator-controlled.",
        "local-read": "This local Studio is read-only. Comments and mutations are unavailable.",
        "public-read": "Shared read-only link. Authoring and session controls remain with the operator.",
        "public-edit": "This public tunnel allows scoped comments and content edits. Local admin authority is not exposed.",
        "public-comment": "Comment-only shared link. You can add comments and replies. Editing content, changing or resolving comments, presence and session controls remain with the operator.",
      };
      authority.appendChild(el("p", { class: "okf-studio-authority", text: authorityCopy[authorityMode] || authorityCopy[EDIT ? "local-edit" : "local-read"] }));
      container.appendChild(authority);
    },
    __builtin: true,
  };
  // panels.comments delegates straight to renderCommentsPanel(),
  // which now preserves the composer node across rebuilds (typing guard
  // is internal — see renderCommentsPanel). The previous wrapper-level
  // "skip rebuild if typing" guard is no longer needed: the panel splits
  // into composer / toolbar / list zones and only the toolbar + list
  // rebuild on each event.
  panels.comments = {
    id: "comments", label: "Comments",
    render: function () { renderCommentsPanel(); },
    __builtin: true,
  };
  panels.changes = { id: "changes", label: "Changes", render: (c) => { c.innerHTML = ""; renderChangeList(); }, __builtin: true };
  purposePanels.add(panels.comments);
  purposePanels.add(panels.changes);

  // ---- Built-in extension panel demonstrating register(): "Agent activity" ----
  // iter2 G13 (CRI2-012): enriched with UNIQUE content the presence chip +
  // the Changes panel don't surface, so the panel earns its slot instead of
  // duplicating them. Three sections:
  //   1. The agent's OPEN/CLAIMED comment queue (comments the agent has
  //      claimed but not resolved) — distinct from the Changes panel, which
  //      shows events, not the directive queue.
  //   2. Presence history (recent state transitions) — a small log the chip
  //      can't show because it only carries the current state.
  //   3. iter3 CRI3-007: agent writes in the LAST 5 MINUTES — a unique time
  //      filter the Changes panel doesn't offer. Was a 30-row slice of the
  //      same changeRow() the Changes panel renders, which made the section
  //      a pure duplicate (the impeccable critic flagged: "a user who opens
  //      both panels side by side sees the same rows twice"). Replacing the
  //      30-row dump with a 5-minute window keeps the panel about state
  //      ("what is the agent doing RIGHT NOW"), with a "View in Changes →"
  //      link for the full history.
  register("panel", {
    id: "agent-activity",
    label: "Agent activity",
    render(container, c) {
      container.innerHTML = "";
      if (!state.eventsReady && (!state.events || !state.events.length)) {
        container.appendChild(dataStateNode("activity", "Activity"));
        return;
      }
      if (!state.eventsReady) container.appendChild(dataStateNode("activity", "Activity"));
      const st = CAN_READ_PRESENCE ? (state.presence.state || "idle") : "unavailable";
      const verb = ({ idle: "idle", watching: "watching", thinking: "thinking about", editing: "editing" })[st] || st;
      container.appendChild(el("h3", { class: "okf-panel__section-title",
        text: "Agent · " + verb + (state.presence.focus ? " · " + state.presence.focus : "") }));

      const claimedNow = state.comments.filter((cm) =>
        (cm.state === "claimed" || cm.state === "open") && isAgentActor(cm.claimed_by));
      const agentEvents = state.events.filter(isAgentActionEvent);
      const availability = agentAvailability();
      container.appendChild(el("p", { class: "okf-agent-availability", dataset: { level: availability.level }, text: availability.text }));
      if (!claimedNow.length && !agentEvents.length) {
        // Nothing here starts an agent, so nothing here offers to: the honest
        // next step is the setup an external agent needs.
        container.appendChild(emptyState(
          "No agent activity yet",
          "Studio does not run an agent. Activity appears when an external agent claims a comment, reports progress, or writes to the bundle.",
          CAN_CONTROL_SESSION ? null : "Authoring help",
          CAN_CONTROL_SESSION ? null
            : function () { openPanel("studio", { returning: true }); const help = panelBody.querySelector(".okf-authoring-help"); if (help) help.open = true; }
        ));
      }
      if (CAN_CONTROL_SESSION) container.appendChild(agentSetupNode(!claimedNow.length && !agentEvents.length));

      // Section 1: the agent's claimed/open comment queue (unique).
      const claimed = claimedNow;
      const q = el("div", { class: "okf-panel__section" });
      q.appendChild(el("h3", { class: "okf-panel__section-title", text: "Claimed queue (" + claimed.length + ")" }));
      if (!claimed.length) {
        q.appendChild(el("p", { class: "okf-empty", text: "No comments claimed by the agent right now." }));
      } else {
        claimed.slice(0, 20).forEach((cm) => q.appendChild(commentCard(cm)));
      }
      container.appendChild(q);

      // Section 2: presence history (unique — the chip carries only current).
      const hist = el("div", { class: "okf-panel__section" });
      hist.appendChild(el("h3", { class: "okf-panel__section-title", text: "Presence history (" + state.presenceHistory.length + ")" }));
      if (!state.presenceHistory.length) {
        hist.appendChild(el("p", { class: "okf-empty", text: "No presence transitions yet." }));
      } else {
        const log = el("ul", { class: "okf-presence-log" });
        state.presenceHistory.slice(0, 12).forEach((h) => {
          const li = el("li", { class: "okf-presence-log__item", "data-state": h.state });
          li.appendChild(el("span", { class: "okf-presence-log__state", text: h.state || "idle" }));
          if (h.focus) li.appendChild(el("span", { class: "okf-presence-log__focus", text: h.focus }));
          if (h.message) li.appendChild(el("span", { class: "okf-presence-log__message", text: h.message, title: h.message }));
          li.appendChild(el("span", { class: "okf-presence-log__ts", text: fmtTime(h.ts) }));
          log.appendChild(li);
        });
        hist.appendChild(log);
      }
      if (CAN_READ_PRESENCE) container.appendChild(hist);

      // Section 3 (iter3 CRI3-007): agent writes in the LAST 5 MINUTES.
      // Unique time-boxed filter the Changes panel doesn't offer; reads as
      // "what is the agent doing RIGHT NOW". Capped at 10 rows so the panel
      // stays compact; a "View in Changes →" button opens the full history
      // panel for everything older.
      const AGENT_RECENT_WINDOW_MS = 5 * 60 * 1000;
      const fiveMinAgo = Date.now() - AGENT_RECENT_WINDOW_MS;
      const recentAgent = state.events.filter((e) => {
        // Named agents sign with their own label. A comment event counts when it
        // is an attributed lifecycle action or the agent's own authored row;
        // an unmarked snapshot of a PERSON's comment never does (its actor is
        // that person, so isAgentActor already excludes it).
        if (!isAgentActor(e.actor) || e.type === "presence" || isSharingEvent(e)) return false;
        // Parse the event ts (ISO string) to epoch ms; tolerate missing or
        // malformed ts by treating the row as NOT recent (excluded).
        const t = Date.parse(e.ts || "");
        return !isNaN(t) && t >= fiveMinAgo;
      });
      const act = el("div", { class: "okf-panel__section" });
      act.appendChild(el("h3", {
        class: "okf-panel__section-title",
        text: "Recent writes · last 5 min (" + recentAgent.length + ")",
      }));
      if (!recentAgent.length) {
        act.appendChild(el("p", {
          class: "okf-empty",
          text: "No agent writes in the last 5 minutes.",
        }));
      } else {
        recentAgent.slice(0, 10).forEach((r) => act.appendChild(changeRow(r)));
      }
      // Single link to the full Changes panel — replaces the 30-row dump
      // that previously duplicated the Changes panel verbatim.
      const viewAll = el("button", {
        type: "button",
        class: "okf-studiobtn okf-panel__section-link",
        text: "View full history in Changes",
      });
      viewAll.addEventListener("click", () => openPanel("changes"));
      act.appendChild(viewAll);
      container.appendChild(act);

      // Section 4 (R3): sharing through a public tunnel. It is not a write,
      // so Changes does not list it; this is where it stays visible. Shown
      // only when there is some, newest first.
      const sharing = state.events.filter(isSharingEvent);
      if (sharing.length) {
        const share = el("div", { class: "okf-panel__section okf-agent-sharing" });
        share.appendChild(el("h3", { class: "okf-panel__section-title", text: "Sharing (" + sharing.length + ")" }));
        sharing.slice(0, 5).forEach((r) => share.appendChild(changeRow(r)));
        if (sharing.length > 5) share.appendChild(el("p", { class: "okf-muted", text: "Showing the latest 5." }));
        container.appendChild(share);
      }
    },
  });
  // register() stored its own copy; that copy is the built-in panel.
  purposePanels.add(panels["agent-activity"]);
  if (state.openPanel === "agent-activity") syncPanelPurpose("agent-activity");

  // ====================================================================
  // 10. Attention + badges. Canonical comments remain actionable until their
  // lifecycle resolves; change attention is unread and acknowledged by opening
  // Changes. Canonical resync removes stale unread keys and never resurrects
  // work that has already been reviewed.
  // ====================================================================
  function eventAttentionKey(ev) {
    if (!ev) return null;
    return ev.event_id != null ? ev.event_id : (ev.event_cursor != null ? ev.event_cursor : null);
  }
  function isActionableChange(ev) {
    if (isSharingEvent(ev)) return false; // not a change; see isSharingEvent
    return !!(ev && (ev.action || ["activity", "changed", "created", "removed"].indexOf(ev.type) >= 0));
  }
  function reconcileAttention(events) {
    const keys = new Set(events.filter(isActionableChange).map(eventAttentionKey).filter(Boolean));
    if (!state.attention.initialized) {
      state.attention.known = keys;
      state.attention.initialized = true;
      return;
    }
    keys.forEach((key) => {
      if (!state.attention.known.has(key)) state.attention.unread.add(key);
    });
    Array.from(state.attention.unread).forEach((key) => {
      if (!keys.has(key)) state.attention.unread.delete(key);
    });
    state.attention.known = keys;
  }
  function acknowledgeChanges() {
    state.attention.unread.clear();
    updateBadges();
  }
  function updateBadges() {
    const open = state.comments.filter((c) => c.state === "open" || c.state === "claimed").length;
    commentsBadge.textContent = String(open);
    commentsBadge.hidden = open === 0;
    commentsBadge.setAttribute("aria-label", open + " open comments");
    updatePageFeedback();
    const acts = state.attention.unread.size;
    changesBadge.textContent = String(acts);
    changesBadge.hidden = acts === 0;
    changesBadge.setAttribute("aria-label", acts + " unread changes");
    const connection = connChip.dataset.state || "connecting";
    // R3: the launcher names what this session may do ("Editable"); the
    // connection chip alone uses "Live", meaning connected.
    let summary = EDIT ? "Editable" : CAN_COMMENT ? "Comment-only" : "Read-only";
    let attention = "idle";
    // A connecting/reconnecting stream that has not yet outlasted the grace
    // keeps the authority word in the launcher (the in-panel chip and the
    // Studio home status stay immediate).
    let inGrace = false;
    if (connection === "online") {
      if (EDIT && (open || acts)) {
        const parts = [];
        if (open) parts.push(open + " comment" + (open === 1 ? "" : "s"));
        if (acts) parts.push(acts + " change" + (acts === 1 ? "" : "s"));
        summary = parts.join(" · ");
        attention = "pending";
      }
    } else if (connection === "offline") {
      summary = "Offline";
      attention = "failure";
    } else {
      // "connecting" (first boot) and "reconnecting" (SSE re-establishing)
      // are degraded-but-recovering states, not failures: the chip, the
      // launcher text, and the aria-label all say so truthfully. Only a dead
      // connection ("offline") raises the "· Error" failure projection, so a
      // healthy boot never announces an error and a real outage stays loud.
      // A handshake that completes within the grace (every healthy load, a
      // brief blip) never reaches the launcher at all.
      const waited = performance.now() - degradedSince;
      if (waited < LAUNCHER_CONNECTING_GRACE_MS) {
        inGrace = true;
        scheduleConnectingGraceCheck(LAUNCHER_CONNECTING_GRACE_MS - waited);
      } else {
        summary = connection === "connecting" ? "Connecting" : "Reconnecting";
      }
    }
    state.connectionSummary = summary;
    // Away from "online" the state word replaces the idle authority word, so
    // it is kept as a prefix, and a comment-only session is not called
    // read-only.
    const authorityPrefix = !EDIT && connection !== "online" && !inGrace
      ? (CAN_COMMENT ? "Comment-only · " : "Read-only · ") : "";
    studioLauncherSummary.textContent = summary ? " · " + authorityPrefix + summary : "";
    studioLauncher.dataset.attention = attention;
    if (studioHomeStatus && studioHomeStatus.isConnected) {
      const status = connectionStatusText();
      if (studioHomeStatus.textContent !== status) studioHomeStatus.textContent = status;
    }
  }
  function scheduleConnectingGraceCheck(delay) {
    if (connectingGraceTimer) return;
    connectingGraceTimer = window.setTimeout(() => {
      connectingGraceTimer = 0;
      updateBadges();
    }, Math.max(0, Math.ceil(delay)) + 16);
  }
  function connectionStatusText() {
    const connection = connChip.dataset.state || "connecting";
    return "Live updates: " + (connection === "online" ? "connected"
      : connection === "offline" ? "offline"
      : connection === "reconnecting" ? "reconnecting…" : "connecting…");
  }

  // F10: exact-identity reveal for a retained activity record. The old
  // implementation opened the panel and, after an arbitrary 60 ms, scanned
  // only the currently mounted virtualized rows with a shortId SUBSTRING
  // fallback that could light up the wrong row — and stayed silent when the
  // record was grouped away, out of window, filtered out, or pruned.
  // Resolution now happens in retained data (state.events) by exact id; the
  // pending id is consumed by the next renderChangeList, which reconciles
  // conflicting filters, repositions the virtualizer window, expands the
  // owning group, and focuses the exact row — or renders a terminal
  // "not available in the retained history" note.
  function jumpToActivity(aid) {
    revealActivity(aid);
  }

  function revealActivity(aid) {
    state.pendingChangeReveal = String(aid == null ? "" : aid);
    if (state.openPanel === "changes") renderChangeList();
    else openPanel("changes");
  }

  // F10 reverse link (history → comment): resolve the exact saved comment in
  // retained data, reconcile scope/archive/time-window/thread visibility, and
  // give the real card useful focus — or state plainly that it is not
  // available. The old rAF + getElementById probe silently did nothing
  // whenever the row was filtered, archived-hidden, or collapsed.
  function revealComment(commentId) {
    state.pendingCommentReveal = String(commentId == null ? "" : commentId);
    // A row created elsewhere moments ago (for example a local feedback
    // import) may not be in the committed canonical rows yet. Refresh them
    // first so the reveal resolves against current authority instead of
    // reporting a false absence; loadComments keeps the reveal pending.
    var wanted = state.pendingCommentReveal;
    if (wanted && state.commentsReady && !state.comments.some(function (row) { return String(row.id) === wanted; })) {
      void loadComments();
    }
    if (state.openPanel === "comments") renderCommentsPanel();
    else openPanel("comments");
  }

  // ====================================================================
  // 11. Graph cache (palette + local graph refresh + presence)
  // ====================================================================
  const fullGraphPage = ((document.getElementById("okf-graph") || {}).dataset || {}).graphUrl === "/__data/graph.json"
    || window.OKF_LOOM_GRAPH_DATA_URL === "/__data/graph.json";
  let studioGraphGeneration = 0;
  let studioGraphPending = null;
  function acceptStudioGraph(graph) {
    if (!graph || !Array.isArray(graph.nodes)) return;
    state.graph = graph;
    state.graphReady = true;
    buildIndexEngagement();
    if (paletteState.open) refreshPaletteList(paletteState.input.value);
  }
  function needsGraphStructure() {
    // Visible comment chips/recency need structure, never document bodies.
    // Backlinks already come from /__data/doc. Other pages acquire only when
    // the command palette is opened; badges do not read state.graph.
    return document.body.classList.contains("okf-viewer--index") || !!$(".okf-conn[data-concept-id]");
  }
  async function loadGraph(refresh = false) {
    if (!refresh && studioGraphPending) return studioGraphPending;
    const own = ++studioGraphGeneration;
    let acquisition;
    if (fullGraphPage) {
      if (!window.okfGraphData) {
        const pending = fetch("/__data/graph.json", { cache: "no-cache", headers: { Accept: "application/json" } })
          .catch((error) => { throw Object.assign(new Error("Graph data request failed."), { okfPhase: "graph-fetch", okfCode: "network-failure", cause: error }); })
          .then((res) => {
            if (!res.ok) throw Object.assign(new Error("HTTP " + res.status), { okfPhase: "graph-http", okfCode: "network-failure" });
            return res.json().catch((error) => { throw Object.assign(new Error("Graph JSON is malformed."), { okfPhase: "graph-json", okfCode: "network-failure", cause: error }); });
          });
        window.okfGraphData = pending;
        pending.catch(() => { if (window.okfGraphData === pending) window.okfGraphData = null; });
      }
      acquisition = window.okfGraphData;
    } else {
      acquisition = fetch("/__data/graph.json?view=structure", { cache: "no-cache", headers: { Accept: "application/json" } })
        .then((res) => { if (!res.ok) throw new Error("HTTP " + res.status); return res.json(); });
    }
    const pending = acquisition.then((graph) => {
      if (own === studioGraphGeneration) acceptStudioGraph(graph);
    }).catch(() => { /* palette still works with commands only */ });
    studioGraphPending = pending;
    await pending;
    if (studioGraphPending === pending) studioGraphPending = null;
  }
  window.addEventListener("okf-loom:graph-data", (event) => {
    // Only accepted latest-wins graph refreshes publish this event.
    ++studioGraphGeneration;
    acceptStudioGraph(event.detail);
    rebuildMarginMarkers();
  });
  function refreshGraph() {
    // On the graph page, graph.js owns refresh/deadline/latest-wins and
    // publishes accepted data above. Never start a second request here.
    if (fullGraphPage) return;
    if (state.graphReady || needsGraphStructure() || paletteState.open) {
      void loadGraph(true).then(() => { rebuildMarginMarkers(); });
    }
  }

  // ====================================================================
  // 11b. Proposal inspection (X3): opt-in, local, read-only
  // ====================================================================
  // Proposal Review is experimental and off by default. When capability
  // discovery says it is active for a LOCAL session (PROPOSAL_INSPECTION),
  // Studio offers one inspection-only Proposals panel over the two GET read
  // routes:
  //   GET /__proposal/v1/proposals?state=&limit=&cursor=
  //   GET /__proposal/v1/proposals/{id}/revisions/{rev}
  // It never POSTs, renders no decide/apply/withdraw/undo control, and never
  // sees the private X-OKF-Proposal-Token (the server never embeds it).
  // Decisions stay with the local agent or `scripts/okf-loom proposal`.
  // Proposal SSE frames are notifications only (ids + read_model_token), so
  // every live update refetches the canonical read model. Bundle edits never
  // emit conflict_changed, so changed/created/removed also invalidate the
  // panel while it is open. Reopening always refetches.
  const PROPOSAL_PANEL_ID = "proposals";
  const PROPOSAL_TIMEOUT_MS = 10000;
  const PROPOSAL_DIFF_CHUNK = 400;
  // Test-only page size behind the same explicit sentinel as the boot-failure
  // hook, so paging is provable without authoring 50+ proposals.
  const PROPOSAL_PAGE_SIZE = (window.__OKF_LOOM_TEST__ === true &&
      Number.isSafeInteger(window.__OKF_LOOM_PROPOSAL_PAGE_SIZE__) &&
      window.__OKF_LOOM_PROPOSAL_PAGE_SIZE__ >= 1 && window.__OKF_LOOM_PROPOSAL_PAGE_SIZE__ <= 100)
    ? window.__OKF_LOOM_PROPOSAL_PAGE_SIZE__ : 50;
  const PROPOSAL_STATES = ["in_review", "ready", "conflicted", "applied", "withdrawn", "superseded", "expired", "audit_expired"];
  const PROPOSAL_STATE_LABELS = {
    in_review: "In review", ready: "Ready", conflicted: "Conflicted", applied: "Applied",
    withdrawn: "Withdrawn", superseded: "Superseded", expired: "Expired", audit_expired: "Audit expired",
  };
  const PROPOSAL_OPEN_STATES = new Set(["in_review", "ready", "conflicted"]);
  const PROPOSAL_OP_STATE_LABELS = {
    undecided: "Undecided", accepted: "Accepted", rejected: "Rejected",
    blocked: "Blocked", conflicted: "Conflicted", invalid: "Invalid",
  };
  const PROPOSAL_DECISION_LABELS = { undecided: "Undecided", accepted: "Accepted", rejected: "Rejected" };
  const PROPOSAL_OP_KIND_LABELS = {
    set_frontmatter: "Set frontmatter", link_add: "Add link", entity_add: "Add entity",
    replace_text: "Replace text", update_section: "Update section",
  };
  const PROPOSAL_CONFLICT_LABELS = {
    snapshot_changed: "The bundle changed after this proposal was created",
    read_revision_changed: "A concept this proposal reads has changed",
    write_revision_changed: "A concept this proposal writes has changed",
    target_missing: "A target concept is missing",
    target_type_changed: "A target concept changed type",
    target_identity_changed: "A target file was replaced",
    capability_changed: "Bundle capabilities changed",
    config_changed: "Bundle configuration changed",
    preview_expired: "The preview expired",
    decision_stale: "A decision no longer matches this revision",
    dependency_unsatisfied: "A dependency is not satisfied",
    validation_stale: "Validation results are out of date",
  };
  const PROPOSAL_VALIDATION_LABELS = { pass: "Pass", fail: "Fail", not_run: "Not run" };
  const proposalWideQuery = typeof window.matchMedia === "function" ? window.matchMedia("(min-width: 900px)") : null;
  // One owner for everything the panel shows. Request ownership is
  // latest-wins: every load bumps its generation and aborts its predecessor,
  // and a settled response is applied only while its generation is current.
  const proposalView = {
    mode: "list", filter: "",
    items: [], nextCursor: null, snapshotExpiresAt: null,
    listStatus: "idle", listError: null, listLoadedAt: null, listStale: true,
    moreStatus: "idle", moreError: null,
    detailKey: null, detailSeed: null, detail: null,
    detailStatus: "idle", detailError: null, detailLoadedAt: null,
    latestRevision: new Map(),
    diffLayout: null, diffShown: new Map(), openDetails: new Set(),
    listGen: 0, detailGen: 0, listCtl: null, moreCtl: null, detailCtl: null,
    pendingFocusKey: null, resetScroll: false, restoreFocus: false,
  };
  let proposalRoot = null;
  let proposalLive = null;
  let proposalShell = null;
  // Proposal bytes that outlive their first render (full before/after text
  // not yet decoded, diff rows not yet shown) are reachable from their
  // controls only through these holders, so pagehide can drop them
  // synchronously even if a detached control is still referenced somewhere.
  const proposalByteHolders = new Set();
  function holdProposalBytes(value) {
    const holder = { value: value, live: true };
    proposalByteHolders.add(holder);
    return holder;
  }
  function dropProposalBytes(holder) {
    holder.value = null;
    holder.live = false;
    proposalByteHolders.delete(holder);
  }
  function releaseProposalBytes() {
    proposalByteHolders.forEach((holder) => { holder.value = null; holder.live = false; });
    proposalByteHolders.clear();
  }
  // Author and decision-actor names come from whoever called the proposal
  // API or CLI; nothing authenticates them (§13).
  const PROPOSAL_LABELS_NOTE = "Author and decision-actor names are unverified labels, not authenticated identities.";

  function proposalFailure(kind, info) {
    const err = new Error((info && info.serverMessage) || kind);
    err.proposalKind = kind; // timeout | network | superseded | http | malformed
    if (info) Object.assign(err, info);
    return err;
  }
  function abortProposalRequest(slot) {
    const controller = proposalView[slot];
    proposalView[slot] = null;
    if (controller) { try { controller.abort(); } catch (e) {} }
  }
  // GET-only, same-origin, session-token-only reader with one 10 s deadline.
  async function proposalGet(path, signal) {
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(function () { timedOut = true; controller.abort(); }, PROPOSAL_TIMEOUT_MS);
    const relay = function () { controller.abort(); };
    if (signal) {
      if (signal.aborted) controller.abort();
      else signal.addEventListener("abort", relay, { once: true });
    }
    const abortCause = function () {
      if (signal && signal.aborted) return proposalFailure("superseded");
      if (timedOut) return proposalFailure("timeout");
      return null;
    };
    try {
      let res;
      try {
        res = await fetch(path, {
          method: "GET", cache: "no-store", credentials: "same-origin", signal: controller.signal,
          headers: { "X-OKF-Token": TOKEN, Accept: "application/json" },
        });
      } catch (e) {
        throw abortCause() || proposalFailure("network");
      }
      let body = null;
      try { body = await res.json(); } catch (e) {
        const cause = abortCause();
        if (cause) throw cause;
        body = null;
      }
      if (!res.ok) {
        const error = body && body.error && typeof body.error === "object" ? body.error : null;
        throw proposalFailure("http", {
          status: res.status,
          code: error && typeof error.code === "string" ? error.code : "",
          serverMessage: error && typeof error.message === "string" ? error.message : "",
          requestId: error && typeof error.request_id === "string" ? error.request_id : "",
          retryable: !!(error && error.retryable === true),
          resync: !!(error && error.resync_required === true),
        });
      }
      if (!body || body.schema !== 1 || !body.data || typeof body.data !== "object" || Array.isArray(body.data)) {
        throw proposalFailure("malformed");
      }
      return body.data;
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", relay);
    }
  }
  function proposalErrorText(err) {
    const kind = err && err.proposalKind;
    let message;
    if (kind === "timeout") message = "The local Studio server didn’t answer within 10 seconds.";
    else if (kind === "network") message = "The local Studio server couldn’t be reached. Check that it is still running.";
    else if (kind === "malformed") message = "The server’s answer wasn’t a proposal read model this Studio understands.";
    else if (kind === "http") {
      const code = err.code || "";
      if (code === "invalid_session_token") message = "This page’s session token is no longer accepted. Reload the page to reconnect.";
      else if (code === "capability_unavailable") message = "Proposal review is no longer active on this server.";
      else if (code === "proposal_not_found" || code === "not_found") message = "This proposal revision wasn’t found. It may have been pruned after its retention ended.";
      else if (code === "read_model_too_large") message = "This proposal is too large to show in the browser. Use scripts/okf-loom proposal show to inspect it.";
      else if (err.status === 429 || err.status === 503 || err.retryable) message = "The proposal service is busy. Try again in a moment.";
      else message = err.serverMessage || ("The proposal service answered with HTTP " + err.status + ".");
    } else message = "Loading failed unexpectedly.";
    const meta = [];
    if (err && err.code) meta.push("Code " + err.code);
    else if (err && err.status) meta.push("HTTP " + err.status);
    if (err && err.requestId) meta.push("request " + err.requestId);
    return { message: message, meta: meta.join(" · ") };
  }
  function announceProposals(message) {
    if (proposalLive && proposalLive.isConnected) proposalLive.textContent = String(message || "");
  }

  function proposalItemValid(item) {
    return !!item && typeof item === "object" && typeof item.proposal_id === "string" && item.proposal_id !== "" &&
      Number.isSafeInteger(item.proposal_revision) && item.proposal_revision >= 1;
  }
  function proposalRowKey(id, rev) { return "proposal-row:" + id + ":" + rev; }
  function noteProposalRevision(id, rev) {
    if ((proposalView.latestRevision.get(id) || 0) < rev) proposalView.latestRevision.set(id, rev);
  }
  // Pages are appended in order; an id+revision already shown is never
  // duplicated when a later page overlaps it.
  function mergeProposalItems(existing, incoming) {
    const seen = new Set(existing.map((it) => it.proposal_id + ":" + it.proposal_revision));
    const out = existing.slice();
    (Array.isArray(incoming) ? incoming : []).forEach((it) => {
      if (!proposalItemValid(it)) return;
      const key = it.proposal_id + ":" + it.proposal_revision;
      if (seen.has(key)) return;
      seen.add(key);
      out.push(it);
      noteProposalRevision(it.proposal_id, it.proposal_revision);
    });
    return out;
  }
  function proposalListSignature(items) {
    return items.map((it) => it.proposal_id + ":" + it.proposal_revision + ":" + it.state + ":" + (it.read_model_token || "")).join("|");
  }
  function proposalListUrl(cursor) {
    // The cursor is bound to the query, so every page repeats state + limit.
    const query = [];
    if (proposalView.filter) query.push("state=" + encodeURIComponent(proposalView.filter));
    query.push("limit=" + PROPOSAL_PAGE_SIZE);
    if (cursor) query.push("cursor=" + encodeURIComponent(cursor));
    return "/__proposal/v1/proposals?" + query.join("&");
  }
  function proposalCountText() {
    const n = proposalView.items.length;
    const scope = proposalView.filter ? PROPOSAL_STATE_LABELS[proposalView.filter].toLowerCase() + " " : "";
    if (!n) return "No " + scope + "proposals.";
    return n + " " + scope + "proposal" + (n === 1 ? "" : "s") + " shown" + (proposalView.nextCursor ? "; more are available." : ".");
  }

  async function loadProposalList(opts) {
    opts = opts || {};
    if (PROPOSAL_INSPECTION !== "available") return;
    // A refresh supersedes any in-flight first page AND any in-flight
    // "load more": a page from the old snapshot must never append.
    abortProposalRequest("listCtl");
    abortProposalRequest("moreCtl");
    proposalView.moreStatus = "idle";
    proposalView.moreError = null;
    const gen = ++proposalView.listGen;
    const controller = new AbortController();
    proposalView.listCtl = controller;
    if (opts.reset) {
      proposalView.items = [];
      proposalView.nextCursor = null;
      proposalView.snapshotExpiresAt = null;
    }
    const before = proposalListSignature(proposalView.items);
    proposalView.listStatus = proposalView.items.length ? "refreshing" : "loading";
    paintProposalBusy("list");
    let page;
    try {
      page = await proposalGet(proposalListUrl(null), controller.signal);
    } catch (err) {
      if (gen !== proposalView.listGen || err.proposalKind === "superseded") return;
      proposalView.listCtl = null;
      proposalView.listStatus = "error";
      proposalView.listError = err;
      announceProposals("Proposals could not be loaded. " + proposalErrorText(err).message);
      renderProposalsIfOpen("list");
      return;
    }
    if (gen !== proposalView.listGen) return;
    proposalView.listCtl = null;
    proposalView.items = mergeProposalItems([], page.items);
    proposalView.nextCursor = typeof page.next_cursor === "string" && page.next_cursor ? page.next_cursor : null;
    proposalView.snapshotExpiresAt = typeof page.snapshot_expires_at === "string" ? page.snapshot_expires_at : null;
    proposalView.listStatus = "ready";
    proposalView.listError = null;
    proposalView.listLoadedAt = new Date();
    proposalView.listStale = false;
    const changed = proposalListSignature(proposalView.items) !== before;
    if (opts.announce) announceProposals(opts.announce + " " + proposalCountText());
    else if (!opts.live) announceProposals(proposalCountText());
    else if (changed) announceProposals("Proposal list updated. " + proposalCountText());
    renderProposalsIfOpen("list");
  }

  async function loadMoreProposals() {
    if (PROPOSAL_INSPECTION !== "available" || !proposalView.nextCursor || proposalView.moreCtl) return;
    const expiry = Date.parse(proposalView.snapshotExpiresAt || "");
    if (!isNaN(expiry) && expiry <= Date.now()) {
      proposalView.pendingFocusKey = "proposals-list-heading";
      void loadProposalList({ reset: true, announce: "The list snapshot expired, so the list was reloaded from the first page." });
      return;
    }
    const gen = proposalView.listGen;
    const controller = new AbortController();
    proposalView.moreCtl = controller;
    proposalView.moreStatus = "loading";
    proposalView.moreError = null;
    renderProposalsIfOpen("list");
    let page;
    try {
      page = await proposalGet(proposalListUrl(proposalView.nextCursor), controller.signal);
    } catch (err) {
      if (gen !== proposalView.listGen || proposalView.moreCtl !== controller || err.proposalKind === "superseded") return;
      proposalView.moreCtl = null;
      if (err.proposalKind === "http" && (err.code === "cursor_expired" || err.code === "invalid_cursor" || err.resync)) {
        // The page position is gone (expired, pruned or no longer valid for
        // this snapshot). Never skip silently: restart from the first page.
        proposalView.pendingFocusKey = "proposals-list-heading";
        void loadProposalList({ reset: true, announce: "The list changed or its page position expired, so it was reloaded from the first page." });
        return;
      }
      proposalView.moreStatus = "error";
      proposalView.moreError = err;
      announceProposals("More proposals could not be loaded. " + proposalErrorText(err).message);
      renderProposalsIfOpen("list");
      return;
    }
    if (gen !== proposalView.listGen || proposalView.moreCtl !== controller) return;
    proposalView.moreCtl = null;
    const before = proposalView.items.length;
    proposalView.items = mergeProposalItems(proposalView.items, page.items);
    const added = proposalView.items.slice(before);
    proposalView.nextCursor = typeof page.next_cursor === "string" && page.next_cursor ? page.next_cursor : null;
    if (typeof page.snapshot_expires_at === "string") proposalView.snapshotExpiresAt = page.snapshot_expires_at;
    proposalView.moreStatus = "idle";
    // Keep keyboard position: stay on "Load more" while more pages exist,
    // otherwise move to the first newly revealed proposal.
    proposalView.pendingFocusKey = proposalView.nextCursor ? "proposals-more"
      : (added.length ? proposalRowKey(added[0].proposal_id, added[0].proposal_revision) : "proposals-list-heading");
    announceProposals("Loaded " + added.length + " more proposal" + (added.length === 1 ? "" : "s") + ". " + proposalCountText());
    renderProposalsIfOpen("list");
  }

  async function loadProposalDetail(opts) {
    opts = opts || {};
    const key = proposalView.detailKey;
    if (PROPOSAL_INSPECTION !== "available" || !key) return;
    abortProposalRequest("detailCtl");
    const gen = ++proposalView.detailGen;
    const controller = new AbortController();
    proposalView.detailCtl = controller;
    const priorToken = proposalView.detail && proposalView.detail.read_model_token;
    proposalView.detailStatus = proposalView.detail ? "refreshing" : "loading";
    paintProposalBusy("detail");
    let data;
    try {
      data = await proposalGet("/__proposal/v1/proposals/" + encodeURIComponent(key.id) +
        "/revisions/" + encodeURIComponent(String(key.rev)), controller.signal);
      if (!data.proposal || typeof data.proposal !== "object" || typeof data.state !== "string") throw proposalFailure("malformed");
    } catch (err) {
      if (gen !== proposalView.detailGen || err.proposalKind === "superseded") return;
      proposalView.detailCtl = null;
      proposalView.detailStatus = "error";
      proposalView.detailError = err;
      announceProposals("This proposal could not be loaded. " + proposalErrorText(err).message);
      renderProposalsIfOpen("detail");
      return;
    }
    if (gen !== proposalView.detailGen) return;
    proposalView.detailCtl = null;
    proposalView.detail = data;
    proposalView.detailStatus = "ready";
    proposalView.detailError = null;
    proposalView.detailLoadedAt = new Date();
    if (Number.isSafeInteger(data.proposal.proposal_revision)) noteProposalRevision(key.id, data.proposal.proposal_revision);
    // The detail read model is newer than the list row it was opened from:
    // carry its state back so the list never shows an older state than the
    // one just read (the list itself is re-read on return).
    proposalView.items = proposalView.items.map((it) =>
      (it.proposal_id === key.id && it.proposal_revision === key.rev && it.state !== data.state)
        ? Object.assign({}, it, { state: data.state }) : it);
    const label = PROPOSAL_STATE_LABELS[data.state] || data.state;
    if (!priorToken) announceProposals("Showing proposal “" + (data.proposal.summary || key.id) + "”, " + label + ".");
    else if (data.read_model_token !== priorToken) announceProposals("This proposal was updated. It is now " + label + ".");
    else if (!opts.live) announceProposals("This proposal is up to date.");
    renderProposalsIfOpen("detail");
  }

  function openProposalDetail(item) {
    abortProposalRequest("detailCtl");
    proposalView.detailGen++;
    proposalView.mode = "detail";
    proposalView.detailKey = { id: item.proposal_id, rev: item.proposal_revision };
    proposalView.detailSeed = item;
    proposalView.detail = null;
    proposalView.detailStatus = "loading";
    proposalView.detailError = null;
    proposalView.detailLoadedAt = null;
    proposalView.diffShown.clear();
    proposalView.openDetails.clear();
    proposalView.pendingFocusKey = "proposal-heading";
    proposalView.resetScroll = true;
    renderProposalsIfOpen();
    void loadProposalDetail();
  }
  function closeProposalDetail() {
    abortProposalRequest("detailCtl");
    proposalView.detailGen++;
    const key = proposalView.detailKey;
    proposalView.mode = "list";
    proposalView.detailKey = null;
    proposalView.detailSeed = null;
    proposalView.detail = null;
    proposalView.detailStatus = "idle";
    proposalView.detailError = null;
    proposalView.diffShown.clear();
    proposalView.openDetails.clear();
    proposalView.pendingFocusKey = key ? proposalRowKey(key.id, key.rev) : "proposals-list-heading";
    renderProposalsIfOpen();
    // States are computed live (a bundle edit can conflict a proposal without
    // any proposal event), so returning to the list always re-reads it.
    void loadProposalList({ live: !!proposalView.listLoadedAt });
  }
  // A notification (or bundle edit, or canonical resync) never carries the
  // read model, so it only invalidates: while open, the visible view
  // refetches; while closed, the next open refetches anyway.
  function invalidateProposals(proposalId) {
    proposalView.listStale = true;
    if (state.openPanel !== PROPOSAL_PANEL_ID) return;
    if (proposalView.mode === "detail") {
      if (!proposalId || (proposalView.detailKey && proposalId === proposalView.detailKey.id)) {
        void loadProposalDetail({ live: true });
      }
      return;
    }
    void loadProposalList({ live: true });
  }

  // ---- rendering ------------------------------------------------------
  function renderProposalsIfOpen(kind) {
    if (state.openPanel !== PROPOSAL_PANEL_ID || !proposalRoot || !proposalRoot.isConnected) return;
    if (kind && kind !== proposalView.mode) return;
    renderProposalView();
  }
  // In-flight refresh keeps the last content visible and only marks it busy;
  // a first load (nothing to keep) renders its loading state.
  function paintProposalBusy(kind) {
    if (state.openPanel !== PROPOSAL_PANEL_ID || !proposalShell || proposalView.mode !== kind) return;
    const empty = kind === "list" ? !proposalView.items.length : !proposalView.detail;
    if (empty) { renderProposalView(); return; }
    proposalShell.body.setAttribute("aria-busy", "true");
    proposalShell.updated.textContent = "Refreshing…";
  }
  function findProposalFocusKey(key) {
    if (!key || !proposalRoot) return null;
    const nodes = proposalRoot.querySelectorAll("[data-okf-focus-key]");
    for (let i = 0; i < nodes.length; i++) {
      if (nodes[i].getAttribute("data-okf-focus-key") === key) return nodes[i];
    }
    return null;
  }
  function renderProposalView() {
    if (!proposalRoot) return;
    const mode = proposalView.mode;
    const active = document.activeElement;
    const activeInside = !!active && active !== proposalRoot && proposalRoot.contains(active);
    const keyed = activeInside && typeof active.closest === "function" ? active.closest("[data-okf-focus-key]") : null;
    const pending = proposalView.pendingFocusKey;
    const focusKey = pending || (keyed ? keyed.getAttribute("data-okf-focus-key") : null);
    proposalView.pendingFocusKey = null;
    const scrollTop = panelBody.scrollTop;
    if (!proposalShell || proposalShell.mode !== mode) {
      // The outgoing detail DOM is discarded, and with it any bytes it held.
      if (proposalShell && proposalShell.mode === "detail") releaseProposalBytes();
      const shell = mode === "detail" ? buildProposalDetailShell() : buildProposalListShell();
      if (proposalShell && proposalShell.node.isConnected) proposalShell.node.replaceWith(shell.node);
      else proposalRoot.appendChild(shell.node);
      proposalShell = shell;
    }
    proposalRoot.dataset.okfProposalsView = mode;
    if (mode === "detail") fillProposalDetail(proposalShell);
    else fillProposalList(proposalShell);
    if (proposalView.resetScroll) { panelBody.scrollTop = 0; proposalView.resetScroll = false; }
    else panelBody.scrollTop = scrollTop;
    // Focus moves only on an explicit navigation (pending) or when the
    // focused control was replaced by this render; otherwise it stays put.
    if (pending || (activeInside && !active.isConnected)) {
      const target = findProposalFocusKey(focusKey) || proposalShell.heading;
      try { target.focus({ preventScroll: !pending }); } catch (e) {}
    }
  }
  function proposalTimeNode(ts, missingText) {
    if (typeof ts !== "string" || !ts) return el("span", { class: "okf-muted", text: missingText || "not recorded" });
    const d = new Date(ts);
    if (isNaN(d.getTime())) return el("span", { text: ts });
    return el("time", { datetime: ts, title: d.toISOString(),
      text: d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) });
  }
  function proposalActor(author) {
    if (typeof author === "string" && author) return author;
    if (author && typeof author === "object") return String(author.actor || author.id || author.name || "unknown");
    return "unknown";
  }
  function proposalPill(kind, value, labels) {
    return el("span", { class: "okf-proposal-pill", dataset: { pill: kind, state: String(value || "") },
      text: labels[value] || String(value || "Unknown") });
  }
  function proposalConceptHref(concept) {
    return "/" + String(concept).split("/").map(encodeURIComponent).join("/");
  }
  function proposalErrorBanner(err, lead, keptSince, retry, retryKey) {
    const info = proposalErrorText(err);
    const box = el("div", { class: "okf-proposals__error" });
    box.appendChild(el("p", { class: "okf-proposals__error-title", text: lead }));
    box.appendChild(el("p", { text: info.message }));
    if (keptSince) box.appendChild(el("p", { class: "okf-proposals__error-meta",
      text: "Still showing what was loaded at " + fmtTime(keptSince.toISOString()) + "." }));
    if (info.meta) box.appendChild(el("p", { class: "okf-proposals__error-meta", text: info.meta }));
    const btn = el("button", { type: "button", class: "okf-studiobtn", dataset: { okfFocusKey: retryKey }, text: "Retry" });
    btn.addEventListener("click", retry);
    box.appendChild(btn);
    return box;
  }

  function buildProposalListShell() {
    const node = el("div", { class: "okf-proposals__view", dataset: { mode: "list" } });
    const header = el("div", { class: "okf-proposals__header" });
    const heading = el("h3", { id: "okf-proposals-list-title", class: "okf-proposals__title", tabindex: "-1",
      dataset: { okfFocusKey: "proposals-list-heading" }, text: "All proposals" });
    header.appendChild(heading);
    header.appendChild(el("p", { class: "okf-proposals__intro",
      text: "Inspection only: read what an agent proposes before anything touches Markdown. A proposal is neither a comment nor a saved change yet. Accepting, rejecting, applying and withdrawing happen through the local agent or scripts/okf-loom proposal, not in this browser." }));
    const controls = el("div", { class: "okf-proposals__controls" });
    const field = el("div", { class: "okf-proposals__field" });
    field.appendChild(el("label", { for: "okf-proposals-filter", text: "Show" }));
    const filter = el("select", { id: "okf-proposals-filter", dataset: { okfFocusKey: "proposals-filter" } });
    filter.appendChild(el("option", { value: "", text: "All states" }));
    PROPOSAL_STATES.forEach((s) => filter.appendChild(el("option", { value: s, text: PROPOSAL_STATE_LABELS[s] })));
    filter.value = proposalView.filter;
    filter.addEventListener("change", () => {
      proposalView.filter = filter.value;
      void loadProposalList({ reset: true });
    });
    field.appendChild(filter);
    controls.appendChild(field);
    const refresh = el("button", { type: "button", class: "okf-studiobtn", dataset: { okfFocusKey: "proposals-refresh" }, text: "Refresh" });
    refresh.addEventListener("click", () => { void loadProposalList(); });
    controls.appendChild(refresh);
    const updated = el("p", { class: "okf-proposals__updated" });
    controls.appendChild(updated);
    header.appendChild(controls);
    header.appendChild(el("p", { class: "okf-proposals__caption",
      text: "Rows show summary, state, revision, author and dates. Operation counts, decisions and validation are not reported in the list; open a proposal to see them. " + PROPOSAL_LABELS_NOTE }));
    node.appendChild(header);
    const body = el("div", { class: "okf-proposals__body" });
    node.appendChild(body);
    return { mode: "list", node: node, body: body, heading: heading, updated: updated, filter: filter };
  }
  function fillProposalList(shell) {
    const v = proposalView;
    shell.heading.textContent = v.filter ? PROPOSAL_STATE_LABELS[v.filter] + " proposals" : "All proposals";
    if (shell.filter.value !== v.filter) shell.filter.value = v.filter;
    const busy = v.listStatus === "loading" || v.listStatus === "refreshing";
    shell.body.setAttribute("aria-busy", busy ? "true" : "false");
    shell.updated.textContent = busy ? (v.items.length ? "Refreshing…" : "Loading…")
      : v.listLoadedAt ? v.items.length + " shown · updated " + fmtTime(v.listLoadedAt.toISOString()) : "";
    const kids = [];
    if (v.listStatus === "error" && v.listError) {
      kids.push(proposalErrorBanner(v.listError, "Proposals could not be loaded.", v.items.length ? v.listLoadedAt : null,
        () => { void loadProposalList(); }, "proposals-retry"));
    }
    if (!v.items.length) {
      if (v.listStatus === "ready") {
        kids.push(v.filter
          ? emptyState("No " + PROPOSAL_STATE_LABELS[v.filter].toLowerCase() + " proposals",
              "Nothing is in this state right now. Show all states to see every proposal revision.",
              "Show all states", () => {
                proposalView.filter = "";
                proposalView.pendingFocusKey = "proposals-filter";
                void loadProposalList({ reset: true });
              })
          : emptyState("No proposals yet",
              "When the local agent creates a proposal (scripts/okf-loom proposal create), it appears here automatically."));
      } else if (v.listStatus !== "error") {
        kids.push(el("p", { class: "okf-panel-state", text: "Loading proposals…" }));
      }
    } else {
      const list = el("ul", { class: "okf-proposals__list", "aria-labelledby": "okf-proposals-list-title" });
      v.items.forEach((item) => list.appendChild(buildProposalRow(item)));
      kids.push(list);
    }
    if (v.nextCursor && v.items.length) {
      const more = el("div", { class: "okf-proposals__more" });
      if (v.moreStatus === "error" && v.moreError) {
        more.appendChild(el("p", { class: "okf-proposals__error-meta",
          text: "More proposals could not be loaded. " + proposalErrorText(v.moreError).message }));
      }
      const btn = el("button", { type: "button", class: "okf-studiobtn", dataset: { okfFocusKey: "proposals-more" },
        text: v.moreStatus === "loading" ? "Loading more…" : v.moreStatus === "error" ? "Try loading more again" : "Load more proposals" });
      // aria-disabled (not disabled) keeps keyboard focus on the control
      // while its page is in flight.
      if (v.moreStatus === "loading") btn.setAttribute("aria-disabled", "true");
      btn.addEventListener("click", () => { if (proposalView.moreStatus !== "loading") void loadMoreProposals(); });
      more.appendChild(btn);
      kids.push(more);
    }
    shell.body.replaceChildren.apply(shell.body, kids);
  }
  function buildProposalRow(item) {
    const li = el("li", { class: "okf-proposal-row" });
    const btn = el("button", { type: "button", class: "okf-proposal-row__open",
      dataset: { okfFocusKey: proposalRowKey(item.proposal_id, item.proposal_revision), state: String(item.state || "") } });
    const top = el("span", { class: "okf-proposal-row__top" });
    top.appendChild(el("span", { class: "okf-proposal-row__summary", text: item.summary || "Untitled proposal" }));
    top.appendChild(proposalPill("proposal", item.state, PROPOSAL_STATE_LABELS));
    btn.appendChild(top);
    const meta = el("span", { class: "okf-proposal-row__meta" });
    meta.appendChild(document.createTextNode("Revision " + item.proposal_revision + " · by " + proposalActor(item.author) + " (unverified) · created "));
    meta.appendChild(proposalTimeNode(item.created_at));
    if (PROPOSAL_OPEN_STATES.has(item.state) && item.expires_at) {
      meta.appendChild(document.createTextNode(" · expires "));
      meta.appendChild(proposalTimeNode(item.expires_at));
    }
    btn.appendChild(meta);
    btn.appendChild(el("span", { class: "okf-proposal-row__id", text: item.proposal_id }));
    btn.addEventListener("click", () => openProposalDetail(item));
    li.appendChild(btn);
    return li;
  }

  function buildProposalDetailShell() {
    const node = el("div", { class: "okf-proposals__view okf-proposal-detail", dataset: { mode: "detail" } });
    const nav = el("div", { class: "okf-proposal-detail__nav" });
    const back = el("button", { type: "button", class: "okf-studiobtn", dataset: { okfFocusKey: "proposal-back" } },
      [el("span", { "aria-hidden": "true", text: "← " }), "All proposals"]);
    back.addEventListener("click", closeProposalDetail);
    const updated = el("p", { class: "okf-proposals__updated" });
    const refresh = el("button", { type: "button", class: "okf-studiobtn", dataset: { okfFocusKey: "proposal-refresh" }, text: "Refresh" });
    refresh.addEventListener("click", () => { void loadProposalDetail(); });
    nav.appendChild(back); nav.appendChild(updated); nav.appendChild(refresh);
    node.appendChild(nav);
    const heading = el("h3", { id: "okf-proposal-detail-title", class: "okf-proposal-detail__title", tabindex: "-1",
      dataset: { okfFocusKey: "proposal-heading" } });
    node.appendChild(heading);
    const body = el("div", { class: "okf-proposals__body okf-proposal-detail__body" });
    node.appendChild(body);
    return { mode: "detail", node: node, body: body, heading: heading, updated: updated };
  }
  function fillProposalDetail(shell) {
    const v = proposalView;
    const key = v.detailKey || { id: "", rev: 0 };
    const d = v.detail;
    const p = d && d.proposal && typeof d.proposal === "object" ? d.proposal : null;
    shell.heading.textContent = (p && p.summary) || (v.detailSeed && v.detailSeed.summary) || ("Proposal " + key.id);
    const busy = v.detailStatus === "loading" || v.detailStatus === "refreshing";
    shell.body.setAttribute("aria-busy", busy ? "true" : "false");
    shell.updated.textContent = busy ? (d ? "Refreshing…" : "Loading…")
      : v.detailLoadedAt ? "Updated " + fmtTime(v.detailLoadedAt.toISOString()) : "";
    // This fill replaces the previous detail body, so bytes its controls
    // still held are dropped before the new controls take their own.
    releaseProposalBytes();
    const kids = [];
    kids.push(el("p", { class: "okf-proposals__intro",
      text: "Inspection only. Decide, apply or withdraw this revision with the local agent or scripts/okf-loom proposal decide, apply or withdraw. " + PROPOSAL_LABELS_NOTE }));
    if (v.detailStatus === "error" && v.detailError) {
      kids.push(proposalErrorBanner(v.detailError, "This proposal could not be loaded.", d ? v.detailLoadedAt : null,
        () => { void loadProposalDetail(); }, "proposal-retry"));
    }
    if (!d || !p) {
      if (v.detailStatus !== "error") kids.push(el("p", { class: "okf-panel-state", text: "Loading proposal…" }));
      shell.body.replaceChildren.apply(shell.body, kids);
      return;
    }
    const latest = v.latestRevision.get(key.id) || 0;
    if (latest > key.rev) {
      const notice = el("div", { class: "okf-proposal-notice" });
      notice.appendChild(el("p", { text: "Revision " + latest + " of this proposal is newer than the revision shown here." }));
      const open = el("button", { type: "button", class: "okf-studiobtn", dataset: { okfFocusKey: "proposal-newer" }, text: "Open revision " + latest });
      // The listener keeps only this seed, never the whole read model.
      const newerSeed = { proposal_id: key.id, proposal_revision: latest, summary: p.summary };
      open.addEventListener("click", () => openProposalDetail(newerSeed));
      notice.appendChild(open);
      kids.push(notice);
    }
    kids.push(proposalFacts(d, p, key));
    const rationale = proposalSection("okf-proposal-sec-rationale", "Rationale");
    rationale.appendChild(el("p", { class: "okf-proposal-rationale",
      text: typeof p.rationale === "string" && p.rationale.trim() ? p.rationale : "No rationale was given." }));
    kids.push(rationale);
    kids.push(proposalConflictsSection(d));
    kids.push(proposalChangesSection(p, key));
    kids.push(proposalOperationsSection(d, p));
    kids.push(proposalValidationSection(p));
    kids.push(proposalLifecycleSection(d));
    shell.body.replaceChildren.apply(shell.body, kids);
  }
  function proposalSection(id, title, extraHead) {
    const sec = el("section", { class: "okf-proposal-section", "aria-labelledby": id });
    const head = el("div", { class: "okf-proposal-section__head" });
    head.appendChild(el("h4", { id: id, class: "okf-proposal-section__title", text: title }));
    if (extraHead) head.appendChild(extraHead);
    sec.appendChild(head);
    return sec;
  }
  function proposalFacts(d, p, key) {
    const dl = el("dl", { class: "okf-proposal-facts" });
    const add = (label, value) => {
      dl.appendChild(el("dt", { text: label }));
      const dd = el("dd");
      appendKids(dd, value);
      dl.appendChild(dd);
    };
    const rev = Number.isSafeInteger(p.proposal_revision) ? p.proposal_revision : key.rev;
    add("State", proposalPill("proposal", d.state, PROPOSAL_STATE_LABELS));
    add("Revision", String(rev) + (Number.isSafeInteger(p.supersedes_revision) ? " (supersedes revision " + p.supersedes_revision + ")" : ""));
    add("Author (unverified)", proposalActor(p.author));
    add("Created", proposalTimeNode(p.created_at));
    add("Expires", proposalTimeNode(p.expires_at));
    const states = Array.isArray(d.operation_states) ? d.operation_states.filter((s) => s && typeof s === "object") : [];
    const count = (value) => states.filter((s) => s.explicit_decision === value).length;
    add("Operations", states.length + " · " + count("accepted") + " accepted · " + count("rejected") + " rejected · " + count("undecided") + " undecided");
    const preview = p.preview && typeof p.preview === "object" ? p.preview : null;
    const validation = preview && !preview.expired && preview.validation && typeof preview.validation === "object" ? preview.validation : null;
    add("Validation", validation ? proposalPill("validation", validation.status, PROPOSAL_VALIDATION_LABELS)
      : (preview && preview.expired ? "Expired with the preview" : "Not available"));
    const concepts = Array.isArray(p.affected_concepts) ? p.affected_concepts.filter((c) => typeof c === "string" && c) : [];
    const conceptNode = el("span", { class: "okf-proposal-facts__concepts" });
    if (!concepts.length) conceptNode.textContent = "None";
    concepts.forEach((c, i) => {
      if (i) conceptNode.appendChild(document.createTextNode(", "));
      conceptNode.appendChild(el("a", { href: proposalConceptHref(c), text: c }));
    });
    add("Concepts", conceptNode);
    add("Proposal ID", el("code", { text: String(p.proposal_id || key.id) }));
    return dl;
  }
  function proposalConflictsSection(d) {
    const conflicts = Array.isArray(d.conflicts) ? d.conflicts.filter((c) => c && typeof c === "object") : [];
    const sec = proposalSection("okf-proposal-sec-conflicts", "Conflicts · " + conflicts.length);
    if (!conflicts.length) {
      sec.appendChild(el("p", { class: "okf-muted", text: "No conflicts with the current bundle." }));
      return sec;
    }
    const list = el("ul", { class: "okf-proposal-conflicts" });
    conflicts.forEach((c) => {
      const li = el("li", { class: "okf-proposal-conflict" });
      const title = el("p", { class: "okf-proposal-conflict__title" });
      title.appendChild(el("strong", { text: PROPOSAL_CONFLICT_LABELS[c.code] || "Conflict" }));
      if (c.code) { title.appendChild(document.createTextNode(" ")); title.appendChild(el("code", { text: String(c.code) })); }
      li.appendChild(title);
      if (typeof c.message === "string" && c.message) li.appendChild(el("p", { class: "okf-proposal-conflict__message", text: c.message }));
      const meta = el("p", { class: "okf-proposal-conflict__meta" });
      const parts = [];
      if (c.concept) parts.push("Concept " + c.concept);
      if (c.op_id) parts.push("operation " + c.op_id);
      meta.appendChild(document.createTextNode((parts.length ? parts.join(" · ") + " · " : "") + "detected "));
      meta.appendChild(proposalTimeNode(c.detected_at));
      li.appendChild(meta);
      list.appendChild(li);
    });
    sec.appendChild(list);
    return sec;
  }

  // ---- unified diff -> rows (textContent only; never HTML) ------------
  // Hunk bodies are consumed by their @@ counts, so a content line that
  // happens to start with "--- "/"+++ " can never be mistaken for a header.
  function parseProposalDiff(text) {
    const files = [];
    const lines = String(text || "").split("\n");
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    let file = null, hunk = null, oldLeft = 0, newLeft = 0, oldNo = 0, newNo = 0;
    const path = (raw) => raw.split("\t")[0].trim();
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const lead = line.charAt(0);
      if (hunk && (oldLeft > 0 || newLeft > 0)) {
        if (lead === "+") { hunk.rows.push({ kind: "add", newNo: newNo++, text: line.slice(1) }); newLeft--; }
        else if (lead === "-") { hunk.rows.push({ kind: "del", oldNo: oldNo++, text: line.slice(1) }); oldLeft--; }
        else if (lead === "\\") hunk.rows.push({ kind: "note", text: line.slice(1).trim() });
        else { hunk.rows.push({ kind: "ctx", oldNo: oldNo++, newNo: newNo++, text: line.slice(1) }); oldLeft--; newLeft--; }
        continue;
      }
      if (lead === "\\" && hunk) { hunk.rows.push({ kind: "note", text: line.slice(1).trim() }); continue; }
      if (line.indexOf("--- ") === 0 && i + 1 < lines.length && lines[i + 1].indexOf("+++ ") === 0) {
        file = { oldPath: path(line.slice(4)), newPath: path(lines[i + 1].slice(4)), hunks: [], notes: [] };
        files.push(file);
        hunk = null;
        i++;
        continue;
      }
      const m = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(line);
      if (m && file) {
        oldNo = +m[1]; newNo = +m[3];
        oldLeft = m[2] == null ? 1 : +m[2];
        newLeft = m[4] == null ? 1 : +m[4];
        hunk = { header: line, rows: [] };
        file.hunks.push(hunk);
        continue;
      }
      if (!file) { file = { oldPath: null, newPath: null, hunks: [], notes: [] }; files.push(file); }
      file.notes.push(line);
    }
    return files;
  }
  function proposalDiffConcept(file) {
    const raw = file && (file.newPath && file.newPath !== "/dev/null" ? file.newPath : file.oldPath);
    if (!raw || raw === "/dev/null") return null;
    return raw.replace(/^[ab]\//, "").replace(/\.md$/, "");
  }
  // Pair each removed run with the added run that follows it so the two
  // sides line up; unmatched lines leave the opposite side empty.
  function splitProposalRows(rows) {
    const out = [];
    let i = 0;
    while (i < rows.length) {
      const r = rows[i];
      if (r.kind === "del" || r.kind === "add") {
        const dels = [], adds = [];
        while (i < rows.length && rows[i].kind === "del") dels.push(rows[i++]);
        while (i < rows.length && rows[i].kind === "add") adds.push(rows[i++]);
        const n = Math.max(dels.length, adds.length);
        for (let k = 0; k < n; k++) out.push({ left: dels[k] || null, right: adds[k] || null });
        continue;
      }
      if (r.kind === "note") out.push({ note: r.text });
      else out.push({ left: r, right: r });
      i++;
    }
    return out;
  }
  function proposalDiffNum(n) {
    return el("td", { class: "okf-proposal-diff__num", text: n == null ? "" : String(n) });
  }
  function proposalDiffText(row, sr) {
    const kind = row ? row.kind : "empty";
    const td = el("td", { class: "okf-proposal-diff__text okf-proposal-diff__cell--" + kind });
    if (!row) return td;
    if (sr && (kind === "add" || kind === "del")) {
      td.appendChild(el("span", { class: "okf-sr-only", text: kind === "add" ? "Added: " : "Removed: " }));
      td.appendChild(el("span", { class: "okf-proposal-diff__marker", "aria-hidden": "true", text: kind === "add" ? "+" : "−" }));
    }
    td.appendChild(document.createTextNode(row.text));
    return td;
  }
  function proposalDiffEntryNode(entry, layout) {
    if (entry.hunk != null) {
      const tr = el("tr", { class: "okf-proposal-diff__hunk" });
      tr.appendChild(el("td", { colspan: "4", text: entry.hunk }));
      return tr;
    }
    const row = entry.row;
    if (row.note != null || row.kind === "note") {
      const tr = el("tr", { class: "okf-proposal-diff__note" });
      tr.appendChild(el("td", { colspan: "4", text: row.note != null ? row.note : row.text }));
      return tr;
    }
    if (layout === "split") {
      const tr = el("tr", { class: "okf-proposal-diff__row" });
      tr.appendChild(proposalDiffNum(row.left ? row.left.oldNo : null));
      tr.appendChild(proposalDiffText(row.left, true));
      tr.appendChild(proposalDiffNum(row.right ? row.right.newNo : null));
      tr.appendChild(proposalDiffText(row.right, true));
      return tr;
    }
    const tr = el("tr", { class: "okf-proposal-diff__row okf-proposal-diff__row--" + row.kind });
    tr.appendChild(proposalDiffNum(row.kind === "add" ? null : row.oldNo));
    tr.appendChild(proposalDiffNum(row.kind === "del" ? null : row.newNo));
    const kind = el("td", { class: "okf-proposal-diff__kind" });
    if (row.kind === "add" || row.kind === "del") {
      kind.appendChild(el("span", { class: "okf-proposal-diff__marker", "aria-hidden": "true", text: row.kind === "add" ? "+" : "−" }));
      kind.appendChild(el("span", { class: "okf-sr-only", text: row.kind === "add" ? "Added" : "Removed" }));
    }
    tr.appendChild(kind);
    tr.appendChild(proposalDiffText(row, false));
    return tr;
  }
  function proposalDiffEntries(file, layout) {
    const entries = [];
    file.hunks.forEach((h) => {
      entries.push({ hunk: h.header });
      (layout === "split" ? splitProposalRows(h.rows) : h.rows).forEach((r) => entries.push({ row: r }));
    });
    return entries;
  }
  function buildProposalDiffTable(file, layout, name, shownKey) {
    // Rows not shown yet stay reachable only through a byte holder (see
    // holdProposalBytes); no listener below captures the diff itself.
    const pending = holdProposalBytes(proposalDiffEntries(file, layout));
    const total = pending.value.length;
    const wrap = el("div", { class: "okf-proposal-diff__scroll", role: "region", tabindex: "0", "aria-label": "Diff of " + name });
    const table = el("table", { class: "okf-proposal-diff okf-proposal-diff--" + layout });
    table.appendChild(el("caption", { class: "okf-sr-only",
      text: (layout === "split" ? "Side-by-side" : "Unified") + " diff of " + name + ": snapshot before compared with proposed after" }));
    const head = el("thead");
    const hr = el("tr");
    // Narrow number/marker columns show a short visible label; the header's
    // accessible name stays the full label.
    const cols = layout === "split"
      ? [["num", "Before line", "Before"], ["text", "Snapshot before"], ["num", "After line", "After"], ["text", "Proposed after"]]
      : [["num", "Before line", "Before"], ["num", "After line", "After"], ["kind", "Change", "±"], ["text", "Line"]];
    cols.forEach((c) => {
      const th = el("th", { scope: "col", class: "okf-proposal-diff__th--" + c[0] });
      if (c[2]) {
        th.appendChild(el("span", { "aria-hidden": "true", text: c[2] }));
        th.appendChild(el("span", { class: "okf-sr-only", text: c[1] }));
      } else th.textContent = c[1];
      hr.appendChild(th);
    });
    head.appendChild(hr);
    table.appendChild(head);
    const body = el("tbody");
    table.appendChild(body);
    wrap.appendChild(table);
    const more = el("div", { class: "okf-proposal-diff__more" });
    let shown = 0;
    const appendUpTo = (limit) => {
      const rows = pending.value;
      if (!rows) return; // released by pagehide or a newer render
      const frag = document.createDocumentFragment();
      const end = Math.min(limit, total);
      for (; shown < end; shown++) frag.appendChild(proposalDiffEntryNode(rows[shown], layout));
      body.appendChild(frag);
      proposalView.diffShown.set(shownKey, shown);
      more.replaceChildren();
      const remaining = total - shown;
      if (remaining <= 0) dropProposalBytes(pending); // everything is rendered
      if (remaining > 0) {
        const btn = el("button", { type: "button", class: "okf-studiobtn", dataset: { okfFocusKey: "diff-more:" + shownKey },
          text: "Show " + Math.min(PROPOSAL_DIFF_CHUNK, remaining) + " more diff rows (" + remaining + " not shown)" });
        btn.addEventListener("click", () => {
          appendUpTo(shown + PROPOSAL_DIFF_CHUNK);
          const next = more.querySelector("button") || more.querySelector("[tabindex]");
          if (next) next.focus();
        });
        more.appendChild(btn);
      } else if (total > PROPOSAL_DIFF_CHUNK) {
        more.appendChild(el("p", { class: "okf-proposal-diff__all", tabindex: "-1", text: "All " + total + " diff rows are shown." }));
      }
    };
    appendUpTo(Math.max(PROPOSAL_DIFF_CHUNK, proposalView.diffShown.get(shownKey) || 0));
    return [wrap, more];
  }
  function decodeProposalText(b64) {
    try {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch (e) { return null; }
  }
  // Full before/after bytes are decoded only when the reader opens them.
  // Until then the toggle listener reaches them only through a byte holder,
  // which is emptied once decoded or when the page is hidden.
  function buildProposalFullText(label, b64, detailsKey, missingText) {
    const det = el("details", { class: "okf-proposal-fulltext" });
    det.appendChild(el("summary", { dataset: { okfFocusKey: "full:" + detailsKey }, text: label }));
    const body = el("div", { class: "okf-proposal-fulltext__body" });
    det.appendChild(body);
    const source = holdProposalBytes(b64);
    let rendered = false;
    const renderBody = () => {
      if (rendered || !source.live) return;
      rendered = true;
      const bytes = source.value;
      dropProposalBytes(source);
      if (typeof bytes !== "string") { body.appendChild(el("p", { class: "okf-muted", text: missingText })); return; }
      const decoded = decodeProposalText(bytes);
      if (decoded == null) {
        body.appendChild(el("p", { class: "okf-muted", text: "This content is not valid UTF-8 text, so it can’t be shown here." }));
        return;
      }
      const pre = el("pre", { class: "okf-proposal-fulltext__pre", tabindex: "0", "aria-label": label });
      pre.textContent = decoded;
      body.appendChild(pre);
    };
    det.addEventListener("toggle", () => {
      if (det.open) { proposalView.openDetails.add(detailsKey); renderBody(); }
      else proposalView.openDetails.delete(detailsKey);
    });
    if (proposalView.openDetails.has(detailsKey)) { det.open = true; renderBody(); }
    return det;
  }
  // Built outside proposalChangesSection so its listeners never share a
  // closure scope with the parsed diff.
  function proposalLayoutToggle(layout) {
    const toggle = el("div", { class: "okf-proposal-layout", role: "group", "aria-label": "Diff layout" });
    [["split", "Side by side"], ["unified", "Unified"]].forEach((opt) => {
      const b = el("button", { type: "button", class: "okf-studiobtn", "aria-pressed": layout === opt[0] ? "true" : "false",
        dataset: { okfFocusKey: "diff-layout:" + opt[0] }, text: opt[1] });
      b.addEventListener("click", () => {
        if (layout === opt[0]) return;
        proposalView.diffLayout = opt[0];
        renderProposalsIfOpen("detail");
      });
      toggle.appendChild(b);
    });
    return toggle;
  }
  function proposalChangesSection(p, key) {
    const preview = p.preview && typeof p.preview === "object" ? p.preview : null;
    const expired = !!(preview && preview.expired === true);
    const changes = preview && !expired && Array.isArray(preview.concept_changes)
      ? preview.concept_changes.filter((c) => c && typeof c === "object" && typeof c.concept === "string") : [];
    const diffText = preview && !expired && preview.diff && typeof preview.diff.text === "string" ? preview.diff.text : "";
    const files = parseProposalDiff(diffText);
    const wide = !proposalWideQuery || proposalWideQuery.matches;
    const layout = wide ? (proposalView.diffLayout || "split") : "unified";
    const toggle = wide && files.some((f) => f.hunks.length) ? proposalLayoutToggle(layout) : null;
    const title = "Changes" + (changes.length ? " · " + changes.length + " concept" + (changes.length === 1 ? "" : "s") : "");
    const sec = proposalSection("okf-proposal-sec-changes", title, toggle);
    if (!preview) {
      sec.appendChild(el("p", { class: "okf-muted", text: "No preview is available for this revision." }));
      return sec;
    }
    if (expired) {
      const note = el("p", { class: "okf-muted" });
      note.appendChild(document.createTextNode("This revision’s preview expired "));
      note.appendChild(proposalTimeNode(preview.expired_at));
      note.appendChild(document.createTextNode(". Its diff and full text are no longer retained, so there is nothing to compare."));
      sec.appendChild(note);
      return sec;
    }
    const byConcept = new Map();
    files.forEach((f) => { const c = proposalDiffConcept(f); if (c && !byConcept.has(c)) byConcept.set(c, f); });
    const used = new Set();
    changes.forEach((chg, idx) => {
      const f = byConcept.get(chg.concept) || null;
      if (f) used.add(f);
      sec.appendChild(proposalChangeBlock(chg, f, layout, key, "c" + idx));
    });
    files.forEach((f, idx) => { if (!used.has(f)) sec.appendChild(proposalChangeBlock(null, f, layout, key, "f" + idx)); });
    if (!changes.length && !files.length) {
      sec.appendChild(el("p", { class: "okf-muted", text: "This revision does not change any concept text." }));
    }
    return sec;
  }
  function proposalChangeBlock(chg, file, layout, key, idx) {
    const concept = chg ? chg.concept : (proposalDiffConcept(file) || "Unmapped change");
    const titleId = "okf-proposal-change-" + idx;
    const block = el("article", { class: "okf-proposal-change", "aria-labelledby": titleId });
    const head = el("div", { class: "okf-proposal-change__head" });
    head.appendChild(el("h5", { id: titleId, class: "okf-proposal-change__title", text: concept }));
    if (chg) head.appendChild(el("a", { class: "okf-proposal-change__link", href: proposalConceptHref(concept),
      "aria-label": "Open the current " + concept + " page", text: "Open current page" }));
    block.appendChild(head);
    if (chg) {
      const short = (r) => String(r).slice(0, 12);
      const ops = Array.isArray(chg.contributing_ops) ? chg.contributing_ops.filter((o) => typeof o === "string") : [];
      block.appendChild(el("p", { class: "okf-proposal-change__meta",
        text: (chg.before_revision ? "Revision " + short(chg.before_revision) : "New concept") + " → " +
          (chg.after_revision ? short(chg.after_revision) : "removed") +
          (ops.length ? " · from " + ops.join(", ") : "") }));
    }
    const baseKey = key.id + ":" + key.rev + ":" + concept;
    if (file && file.hunks.length) {
      const caption = el("p", { class: "okf-conflict__diff-caption" });
      caption.appendChild(el("span", { class: "okf-conflict__diff-side", text: "Snapshot before" }));
      caption.appendChild(el("code", { text: file.oldPath || "(none)" }));
      caption.appendChild(el("span", { "aria-hidden": "true", text: "→" }));
      caption.appendChild(el("span", { class: "okf-conflict__diff-side", text: "Proposed after" }));
      caption.appendChild(el("code", { text: file.newPath || "(none)" }));
      block.appendChild(caption);
      buildProposalDiffTable(file, layout, concept, baseKey + ":" + layout).forEach((n) => block.appendChild(n));
    } else if (file && file.notes.length) {
      block.appendChild(el("pre", { class: "okf-proposal-fulltext__pre", text: file.notes.join("\n") }));
    } else {
      block.appendChild(el("p", { class: "okf-muted", text: "No textual difference is reported for this concept." }));
    }
    if (chg) {
      const full = el("div", { class: "okf-proposal-change__full" });
      full.appendChild(buildProposalFullText("Full snapshot-before text", chg.before_base64, baseKey + ":before",
        "No snapshot-before text: this concept did not exist when the proposal was created."));
      full.appendChild(buildProposalFullText("Full proposed-after text", chg.after_base64, baseKey + ":after",
        "No proposed-after text: this proposal removes the concept."));
      block.appendChild(full);
    }
    return block;
  }
  function proposalOperationsSection(d, p) {
    const ops = Array.isArray(p.operations) ? p.operations.filter((o) => o && typeof o === "object") : [];
    const states = new Map();
    (Array.isArray(d.operation_states) ? d.operation_states : []).forEach((s) => { if (s && typeof s.op_id === "string") states.set(s.op_id, s); });
    const decisions = new Map();
    (Array.isArray(d.latest_decisions) ? d.latest_decisions : []).forEach((r) => { if (r && typeof r.op_id === "string") decisions.set(r.op_id, r); });
    const sec = proposalSection("okf-proposal-sec-ops", "Operations · " + ops.length);
    if (!ops.length) {
      sec.appendChild(el("p", { class: "okf-muted", text: "This revision has no operations." }));
      return sec;
    }
    const wrap = el("div", { class: "okf-proposal-table-wrap", role: "region", tabindex: "0", "aria-labelledby": "okf-proposal-sec-ops" });
    const table = el("table", { class: "okf-proposal-table" });
    table.appendChild(el("caption", { class: "okf-sr-only", text: "Operations with their decisions and current state" }));
    const head = el("thead");
    const hr = el("tr");
    ["Operation", "Target", "Decision", "Current state", "Notes"].forEach((t) => hr.appendChild(el("th", { scope: "col", text: t })));
    head.appendChild(hr);
    table.appendChild(head);
    const body = el("tbody");
    ops.forEach((op) => {
      const s = states.get(op.op_id) || {};
      const decision = decisions.get(op.op_id);
      const tr = el("tr");
      const opCell = el("td");
      opCell.appendChild(el("span", { class: "okf-proposal-table__strong", text: PROPOSAL_OP_KIND_LABELS[op.kind] || String(op.kind || "Operation") }));
      opCell.appendChild(el("code", { class: "okf-proposal-table__id", text: String(op.op_id || "") }));
      tr.appendChild(opCell);
      const targetCell = el("td");
      targetCell.appendChild(el("code", { text: String(op.target || "") }));
      tr.appendChild(targetCell);
      const decisionCell = el("td");
      decisionCell.appendChild(el("span", { text: PROPOSAL_DECISION_LABELS[s.explicit_decision] || "Undecided" }));
      if (decision) {
        const meta = el("span", { class: "okf-proposal-table__meta" });
        meta.appendChild(document.createTextNode("by " + proposalActor(decision.actor) + " (unverified) · "));
        meta.appendChild(proposalTimeNode(decision.decided_at));
        decisionCell.appendChild(meta);
        if (typeof decision.reason === "string" && decision.reason) decisionCell.appendChild(el("span", { class: "okf-proposal-table__meta", text: "“" + decision.reason + "”" }));
      }
      tr.appendChild(decisionCell);
      const stateCell = el("td");
      stateCell.appendChild(proposalPill("operation", s.effective_state, PROPOSAL_OP_STATE_LABELS));
      if (s.local_validity === "invalid") stateCell.appendChild(el("span", { class: "okf-proposal-table__meta", text: "Fails local validity checks" }));
      tr.appendChild(stateCell);
      const notes = [];
      const blockers = Array.isArray(s.blockers) ? s.blockers.filter(Array.isArray) : [];
      blockers.forEach((path) => notes.push("Blocked via " + path.join(" → ")));
      const conflicts = Array.isArray(s.conflicts) ? s.conflicts.length : 0;
      if (conflicts) notes.push(conflicts + " conflict" + (conflicts === 1 ? "" : "s"));
      const deps = Array.isArray(op.depends_on) ? op.depends_on.filter((x) => typeof x === "string") : [];
      if (deps.length) notes.push("Depends on " + deps.join(", "));
      const notesCell = el("td");
      if (!notes.length) notesCell.appendChild(el("span", { class: "okf-muted", text: "None" }));
      notes.forEach((n) => notesCell.appendChild(el("span", { class: "okf-proposal-table__line", text: n })));
      tr.appendChild(notesCell);
      body.appendChild(tr);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    sec.appendChild(wrap);
    return sec;
  }
  function proposalFindingList(findings) {
    const list = el("ul", { class: "okf-proposal-findings" });
    findings.forEach((f) => {
      if (!f || typeof f !== "object") return;
      const li = el("li");
      li.appendChild(el("span", { class: "okf-proposal-findings__severity", text: String(f.severity || "finding") }));
      li.appendChild(document.createTextNode(" "));
      li.appendChild(el("code", { text: String(f.code || "") }));
      li.appendChild(document.createTextNode(" " + String(f.message || "")));
      if (f.concept) li.appendChild(el("span", { class: "okf-proposal-table__meta",
        text: f.concept + (Number.isSafeInteger(f.line) && f.line > 0 ? ", line " + f.line : "") }));
      list.appendChild(li);
    });
    return list;
  }
  function proposalValidationSection(p) {
    const preview = p.preview && typeof p.preview === "object" ? p.preview : null;
    const v = preview && !preview.expired && preview.validation && typeof preview.validation === "object" ? preview.validation : null;
    const sec = proposalSection("okf-proposal-sec-validation", "Validation");
    if (!v) {
      sec.appendChild(el("p", { class: "okf-muted", text: preview && preview.expired
        ? "Validation results expired with the preview." : "Validation results are not available for this revision." }));
      return sec;
    }
    const len = (x) => (Array.isArray(x) ? x.length : 0);
    const status = el("p", { class: "okf-proposal-validation__status" });
    status.appendChild(proposalPill("validation", v.status, PROPOSAL_VALIDATION_LABELS));
    status.appendChild(document.createTextNode(" " + (v.status === "not_run" ? "Validation did not run for this preview." :
      len(v.added) + " finding" + (len(v.added) === 1 ? "" : "s") + " added, " + len(v.removed) + " resolved, " +
      len(v.unchanged) + " unchanged" + (v.profile ? " · profile " + v.profile : "") + ".")));
    sec.appendChild(status);
    if (len(v.added)) {
      sec.appendChild(el("h5", { class: "okf-proposal-subtitle", text: "Findings this proposal adds" }));
      sec.appendChild(proposalFindingList(v.added));
    }
    if (len(v.removed)) {
      sec.appendChild(el("h5", { class: "okf-proposal-subtitle", text: "Findings this proposal resolves" }));
      sec.appendChild(proposalFindingList(v.removed));
    }
    if (!len(v.added) && !len(v.removed) && v.status !== "not_run") {
      sec.appendChild(el("p", { class: "okf-muted", text: "This proposal neither adds nor resolves validation findings." }));
    }
    return sec;
  }
  function proposalResultFacts(result) {
    const dl = el("dl", { class: "okf-proposal-facts" });
    Object.keys(result).sort().forEach((k) => {
      const value = result[k];
      let text;
      if (value == null) text = "none";
      else if (Array.isArray(value)) text = value.length + " item" + (value.length === 1 ? "" : "s");
      else if (typeof value === "object") text = Object.keys(value).length + " field" + (Object.keys(value).length === 1 ? "" : "s");
      else text = String(value);
      dl.appendChild(el("dt", { text: k.replace(/_/g, " ") }));
      dl.appendChild(el("dd", { text: text }));
    });
    return dl;
  }
  // This view reports only what the read model carries, and the local
  // service fills little of it: `transaction_attempts` holds unfinished
  // (nonterminal) attempts only, and apply/undo results and the replay, undo
  // and audit horizons are not passed in today. So a missing value reads
  // "Not reported" and an empty attempts list never claims nothing happened.
  // A retention deadline is how long a record is kept, never a promise that
  // undo is still possible (§7.5: retention is never undo availability).
  function proposalLifecycleSection(d) {
    const sec = proposalSection("okf-proposal-sec-lifecycle", "Retention and apply/undo");
    const retention = d.retention && typeof d.retention === "object" ? d.retention : {};
    const dl = el("dl", { class: "okf-proposal-facts" });
    [["preview_expires_at", "Preview kept until"], ["replay_expires_at", "Replay record kept until"],
     ["undo_expires_at", "Undo backups kept until"], ["audit_expires_at", "Audit record kept until"]].forEach((pair) => {
      dl.appendChild(el("dt", { text: pair[1] }));
      const dd = el("dd");
      dd.appendChild(proposalTimeNode(retention[pair[0]], "Not reported"));
      dl.appendChild(dd);
    });
    sec.appendChild(dl);
    sec.appendChild(el("p", { class: "okf-muted",
      text: "Retention dates say how long records are kept, not whether undo is still possible." }));
    const attempts = Array.isArray(d.transaction_attempts) ? d.transaction_attempts.filter((a) => a && typeof a === "object") : [];
    const hasResult = !!((d.apply_result && typeof d.apply_result === "object") || (d.undo_result && typeof d.undo_result === "object"));
    if (!attempts.length) {
      if (!hasResult) sec.appendChild(el("p", { class: "okf-muted", text: "Apply/undo history is not reported by this view." }));
    } else {
      sec.appendChild(el("h5", { id: "okf-proposal-attempts-title", class: "okf-proposal-subtitle", text: "Unfinished apply and undo attempts" }));
      sec.appendChild(el("p", { class: "okf-muted", text: "Only attempts still in progress are reported here; finished apply/undo history is not." }));
      const wrap = el("div", { class: "okf-proposal-table-wrap", role: "region", tabindex: "0", "aria-labelledby": "okf-proposal-attempts-title" });
      const table = el("table", { class: "okf-proposal-table" });
      const head = el("thead");
      const hr = el("tr");
      ["Operation", "Status", "Phase", "Progress", "Started", "Updated", "Error"].forEach((t) => hr.appendChild(el("th", { scope: "col", text: t })));
      head.appendChild(hr);
      table.appendChild(head);
      const body = el("tbody");
      attempts.forEach((a) => {
        const tr = el("tr");
        [String(a.operation || ""), String(a.status || ""), String(a.phase || ""),
         (a.completed_targets != null && a.total_targets != null) ? a.completed_targets + " of " + a.total_targets + " targets" : ""]
          .forEach((t) => tr.appendChild(el("td", { text: t })));
        const started = el("td"); started.appendChild(proposalTimeNode(a.started_at)); tr.appendChild(started);
        const updatedCell = el("td"); updatedCell.appendChild(proposalTimeNode(a.updated_at)); tr.appendChild(updatedCell);
        tr.appendChild(el("td", { text: a.error_code ? String(a.error_code) : "None" }));
        body.appendChild(tr);
      });
      table.appendChild(body);
      wrap.appendChild(table);
      sec.appendChild(wrap);
    }
    // The read model reports apply/undo outcomes only once they exist.
    if (d.apply_result && typeof d.apply_result === "object") {
      sec.appendChild(el("h5", { class: "okf-proposal-subtitle", text: "Apply result" }));
      sec.appendChild(proposalResultFacts(d.apply_result));
    }
    if (d.undo_result && typeof d.undo_result === "object") {
      sec.appendChild(el("h5", { class: "okf-proposal-subtitle", text: "Undo result" }));
      sec.appendChild(proposalResultFacts(d.undo_result));
    }
    return sec;
  }
  function proposalNeedsTokenNode() {
    const box = el("section", { class: "okf-proposal-unavailable okf-proposals__blocked", "aria-labelledby": "okf-proposals-blocked-title" });
    box.appendChild(el("h3", { id: "okf-proposals-blocked-title", class: "okf-proposal-unavailable__title",
      text: "Proposals can’t be inspected from this read-only page" }));
    box.appendChild(el("p", { text: "Proposal review is active on this server, but read-only Studio pages (served with --no-edit) carry no session token and the proposal read routes require one. Nothing was requested." }));
    box.appendChild(el("p", { text: "Inspect proposals from a terminal instead:" }));
    const pre = el("pre", { class: "okf-agent-setup__commands", tabindex: "0", "aria-label": "Proposal inspection commands" });
    pre.textContent = "scripts/okf-loom proposal list <bundle>\nscripts/okf-loom proposal show <bundle> <proposal-id> --revision <n>";
    box.appendChild(pre);
    box.appendChild(el("p", { class: "okf-muted", text: "An editable local Studio (served without --no-edit) can inspect proposals here." }));
    return box;
  }
  function renderProposalsPanel(container) {
    container.innerHTML = "";
    // Any earlier panel DOM is gone, so bytes its controls held go too.
    releaseProposalBytes();
    proposalShell = null;
    proposalRoot = el("div", { class: "okf-proposals" });
    proposalLive = el("p", { class: "okf-sr-only", role: "status", "aria-live": "polite", "aria-atomic": "true" });
    proposalRoot.appendChild(proposalLive);
    container.appendChild(proposalRoot);
    if (PROPOSAL_INSPECTION !== "available") {
      proposalRoot.appendChild(proposalNeedsTokenNode());
      return;
    }
    renderProposalView();
    // Always refetch on open: SSE frames only notify, and the panel may have
    // been closed while proposals or the bundle changed.
    if (proposalView.mode === "detail" && proposalView.detailKey) void loadProposalDetail();
    else void loadProposalList({ live: !!proposalView.listLoadedAt });
  }
  function wireProposalLive() {
    if (PROPOSAL_INSPECTION !== "available" || !window.okfLoomLive) return;
    const live = window.okfLoomLive;
    live.on("proposal", (row) => {
      const note = row && row.data;
      if (!note || typeof note !== "object" || typeof note.proposal_id !== "string" || !note.proposal_id) return;
      if (Number.isSafeInteger(note.proposal_revision) && note.proposal_revision >= 1) {
        noteProposalRevision(note.proposal_id, note.proposal_revision);
      }
      invalidateProposals(note.proposal_id);
    });
    // Bundle edits never emit conflict_changed, yet they can create or clear
    // conflicts, so they invalidate the open inspection too.
    ["changed", "created", "removed"].forEach((type) => live.on(type, () => { invalidateProposals(null); }));
    // Block body on purpose: live.js awaits a returned promise during a
    // canonical resync, and this refetch must not hold that resync open.
    live.on("resync", () => { invalidateProposals(null); });
    // Connection recovery: while the stream was down, proposal notifications
    // and bundle edits may have been missed and no resync is guaranteed. So
    // every return to online after this page had already been online
    // (reconnect, retry after offline, BFCache restore) invalidates the
    // visible list or detail; invalidation keeps the selection and moves
    // focus only if its control was replaced. The first connect needs
    // nothing: opening the panel always reads canonically.
    let seenOnline = live.state === "online";
    let lostSinceOnline = false;
    live.on("conn", (d) => {
      if (!d || d.state !== "online") {
        if (seenOnline) lostSinceOnline = true;
        return;
      }
      const recovered = lostSinceOnline;
      seenOnline = true;
      lostSinceOnline = false;
      if (recovered) invalidateProposals(null);
    });
  }
  // Pagehide (navigation away or a BFCache freeze) drops every proposal byte
  // the page holds: the panel DOM whether open, closed (hidden) or detached
  // by another panel, the root/shell/live-region references, the read model,
  // list rows and opening seed, and every byte holder. Only non-sensitive
  // ids, the filter, layout preferences and the open-panel id survive (§12).
  function releaseProposalPage() {
    ["listCtl", "moreCtl", "detailCtl"].forEach(abortProposalRequest);
    proposalView.listGen++;
    proposalView.detailGen++;
    const root = proposalRoot;
    proposalView.restoreFocus = false;
    if (root) {
      const active = document.activeElement;
      proposalView.restoreFocus = !!active && active !== root && root.contains(active);
      if (panelLastFocus && root.contains(panelLastFocus)) panelLastFocus = null;
      if (panelChildTrigger && root.contains(panelChildTrigger)) panelChildTrigger = null;
      root.replaceChildren();
      root.remove();
    }
    proposalRoot = null;
    proposalShell = null;
    proposalLive = null;
    releaseProposalBytes();
    Object.assign(proposalView, {
      items: [], nextCursor: null, snapshotExpiresAt: null,
      listStatus: "idle", listError: null, listLoadedAt: null, listStale: true,
      moreStatus: "idle", moreError: null,
      detailSeed: null, detail: null, detailStatus: "idle", detailError: null, detailLoadedAt: null,
      pendingFocusKey: null,
    });
  }
  // Test seam: reports only whether proposal content is still referenced,
  // never the content itself.
  function proposalRetentionForTest() {
    return {
      root: !!proposalRoot, shell: !!proposalShell, live: !!proposalLive,
      rootText: proposalRoot ? proposalRoot.textContent.length : 0,
      seed: !!proposalView.detailSeed, detail: !!proposalView.detail,
      items: proposalView.items.length, byteHolders: proposalByteHolders.size,
    };
  }
  if (PROPOSAL_INSPECTION !== "off") {
    // Only the inspectable panel needs diff width; the read-only explanation
    // stays in the normal drawer.
    register("panel", { id: PROPOSAL_PANEL_ID, label: "Proposals", render: renderProposalsPanel,
      wide: PROPOSAL_INSPECTION === "available" });
  }
  if (PROPOSAL_INSPECTION === "available") {
    // Synchronous and persistent (not once): every pagehide, including
    // repeated BFCache cycles, empties the panel before the page is frozen.
    window.addEventListener("pagehide", releaseProposalPage);
    window.addEventListener("pageshow", function (event) {
      if (!event.persisted) return;
      const refocus = proposalView.restoreFocus;
      proposalView.restoreFocus = false;
      // A closed panel reads canonically when it is next opened.
      if (state.openPanel !== PROPOSAL_PANEL_ID) return;
      // BFCache restore: rebuild the emptied panel on the same view and
      // selection, then refetch it canonically. Focus returns to the view
      // heading only when pagehide removed it from inside the panel.
      if (refocus) proposalView.pendingFocusKey = proposalView.mode === "detail" ? "proposal-heading" : "proposals-list-heading";
      renderProposalsPanel(panelBody);
    });
    if (proposalWideQuery) {
      const onWidthClass = () => { renderProposalsIfOpen("detail"); };
      if (typeof proposalWideQuery.addEventListener === "function") proposalWideQuery.addEventListener("change", onWidthClass);
      else if (typeof proposalWideQuery.addListener === "function") proposalWideQuery.addListener(onWidthClass);
    }
  }

  // ====================================================================
  // 12. Wire live.js hub → studio surfaces
  // ====================================================================
  function applyConnectionState(rawState) {
    // live.js owns the raw lifecycle: connecting (module booted, stream not
    // confirmed) → online / reconnecting / offline. Keep "connecting"
    // distinct — collapsing it into "reconnecting" is what made every fresh
    // healthy load read as a connection failure until the stream opened.
    const connection = rawState === "online" ? "online"
      : rawState === "offline" ? "offline"
      : rawState === "connecting" ? "connecting"
      : "reconnecting";
    // The launcher's connecting grace runs from the start of one continuous
    // non-online, non-failed stretch: boot "connecting" that falls back to
    // "reconnecting" keeps its original start; online or offline ends it.
    const wasDegraded = connChip.dataset.state === undefined
      || connChip.dataset.state === "connecting" || connChip.dataset.state === "reconnecting";
    const isDegraded = connection === "connecting" || connection === "reconnecting";
    if (isDegraded && !wasDegraded) degradedSince = performance.now();
    connChip.dataset.state = connection;
    connLabel.textContent = connection === "online" ? "Live"
      : connection === "connecting" ? "Connecting…"
      : connection === "reconnecting" ? "Reconnecting…" : "Offline";
    connChip.setAttribute("aria-label", "Live updates connection: " + connection);
    updateBadges();
  }
  function wireLive() {
    if (!window.okfLoomLive) return;
    // live.js may have published before Studio subscribed. Replay its
    // canonical getter synchronously so connecting/reconnecting is never
    // mislabeled as Live during boot or reconnection.
    applyConnectionState(window.okfLoomLive.state);
    window.okfLoomLive.on("conn", (d) => applyConnectionState(d && d.state));
    window.okfLoomLive.on("presence", renderPresence);
      window.okfLoomLive.on("comment", (c) => {
        if (!c || !c.id) return;
        // SSE is notification-only. Refetch canonical rows so row_token and
        // server-derived anchor_resolution always travel together.
        void loadComments();
      // Also push to events so the timeline reflects comment lifecycle.
      upsertEvent(Object.assign({ type: "comment" }, c));
    });
    window.okfLoomLive.on("activity", (a) => {
      if (!a) return;
      upsertEvent(a);
      if (state.openPanel === "changes") { acknowledgeChanges(); renderChangeList(); }
      updateBadges();
      // iter1 CRI-010: collapse a burst of activity into one toast. Events
      // that share a group_id (a scoped enrichment pass) are coalesced into
      // "Agent made N changes" with a single group-Undo. Standalone events
      // still get their own toast, but a rapid stream no longer stacks 8.
      scheduleActivityToast(a);
    });
    window.okfLoomLive.on("changed", (d) => { upsertEvent(Object.assign({}, d, { type: "changed", ts: d.ts || new Date().toISOString() })); if (state.openPanel === "changes") { acknowledgeChanges(); renderChangeList(); } updateBadges(); });
    window.okfLoomLive.on("created", (d) => { upsertEvent(Object.assign({}, d, { type: "created", ts: d.ts || new Date().toISOString() })); if (state.openPanel === "changes") { acknowledgeChanges(); renderChangeList(); } updateBadges(); });
    window.okfLoomLive.on("removed", (d) => { upsertEvent(Object.assign({}, d, { type: "removed", ts: d.ts || new Date().toISOString() })); if (state.openPanel === "changes") { acknowledgeChanges(); renderChangeList(); } updateBadges(); });
    // R8-RESYNC: return the loadComments() promise so live.js doResync can
    // AWAIT the canonical refresh completion. Without this, doResync treated
    // the synchronous emit as completion and a newer resync overlapped the
    // real refresh work.
    window.okfLoomLive.on("resync", () => loadComments(0, { authoritative: true }));
    // INTENT5-001 / QUA5-002: comment_link consumer — upsert the event into
    // state.events so changeRow's lookup finds it and re-renders the back-link
    // live (without a page reload). Also re-render the changes panel if open.
    window.okfLoomLive.on("comment_link", (d) => {
      upsertEvent(d);
      if (state.openPanel === "changes") renderChangeList();
    });
    window.okfLoomLive.on("agent_conflict", (d) => {
      // §9.4 conflict from the CLI agent path: surface the modal. The SSE
      // event is the FLAT conflict object (update.py append_event), so it
      // rides in as `data`; there is no HTTP request to retry, so no
      // url/opts — showConflictModal hides "Take the agent's edit" for
      // this shape (the agent's payload lives in its process, not ours).
      showConflictModal({ data: d });
    });
    window.okfLoomLive.on("open-removed", (d) => {
      // R5-4: authoritative absence — the open concept was removed. Invalidate
      // all stale UI: commitDocument(null) clears state.doc, bumps generations,
      // aborts operations, clears selection/draft, and conflicts the editor.
      // Marks and panel are rebuilt on the now-empty doc so no stale highlight
      // or comment row lingers.
      commitDocument(null);
      applyCommentMarks();
      rebuildMarginMarkers();
      renderCommentsPanel();
      updateBadges();
      toast(el("span", {}, [document.createTextNode("The open concept “" + d.id + "” was removed. "),
        el("a", { href: "/", text: "Go to index" })]), { tone: "error", sticky: true });
    });
    window.okfLoomLive.on("patched", () => { /* change list already updated via changed */ });
    // R4-4: a live network patch that advanced the content revision leaves
    // persisted comment resolutions keyed to the OLD revision. Marks are
    // disposable projections removed by applyCommentMarks during the patch;
    // without this canonical refetch they would not recompute and health
    // would read stale until an unrelated comment event arrived. loadComments
    // fetches the doc+comments tuple consistently and is generation-fenced,
    // so a concurrent editor refresh (refreshAfterConflict) supersedes it.
    window.okfLoomLive.on("doc-rev-changed", () => { void loadComments(); });
    // R5-14: authoritative production consumer for the `suggestions` SSE event.
    // The agent (propose_only mode) posts a proposal via post_suggestion and
    // emits {type:"suggestions", proposal:<id>, concept:<id>}. Previously the
    // transport dispatched this frame but wireLive had no subscriber, so a real
    // suggestions frame had no user-visible or production-state effect despite
    // the live.js comment implying suggestion surfacing. Route it into the
    // changes timeline — the existing intended surface for agent-originated
    // events — so it produces a visible changes-panel row and bumps the badge.
    // (The reserved `suggestionRenderer` extension point remains forward-compat
    // for custom rendering; this consumer is the built-in default outcome.)
    window.okfLoomLive.on("suggestions", (d) => {
      if (!d) return;
      const concept = typeof d.concept === "string" ? d.concept : null;
      upsertEvent({
        type: "suggestions",
        event_id: d.event_id != null ? d.event_id : null,
        event_cursor: d.event_cursor != null ? d.event_cursor : null,
        id: d.proposal || null,
        action: "proposed",
        actor: "agent",
        ids: concept ? [concept] : [],
        summary: concept ? ("Proposed change to " + concept) : "Proposed change",
        ts: new Date().toISOString(),
      });
      if (state.openPanel === "changes") renderChangeList();
      updateBadges();
    });
    // X3: proposal notifications + bundle edits invalidate the read-only
    // Proposals panel (no-op unless inspection is available).
    wireProposalLive();
  }

  // ---- activity toast throttle + inline undo (iter1 CRI-010 / CRI-018) -
  // Within a short window (500ms) we coalesce activity events so a single
  // scoped-enrichment pass that writes 8 files produces ONE toast, not 8.
  // Grouped events (shared group_id) collapse to "Agent made N changes"
  // with a group-Undo; standalone events keep their own one-line toast.
  // The toast Undo button performs the undo INLINE (CRI-018) instead of
  // just opening the Changes panel.
  const ACTIVITY_TOAST_WINDOW_MS = 500;
  // iter2 CRI2-005: max inline Undo buttons in a single burst toast before
  // they collapse to one "Undo all (N)". A toast is glanceable; a 5-button
  // toast reads as a panel and the close × drifts from the summary on wrap.
  const ACTIVITY_TOAST_UNDO_CAP = 3;
  let activityToastTimer = null;
  let activityToastBuffer = [];
  function scheduleActivityToast(a) {
    activityToastBuffer.push(a);
    if (activityToastTimer) clearTimeout(activityToastTimer);
    activityToastTimer = setTimeout(flushActivityToast, ACTIVITY_TOAST_WINDOW_MS);
  }
  function flushActivityToast() {
    activityToastTimer = null;
    const batch = activityToastBuffer;
    activityToastBuffer = [];
    if (!batch.length) return;
    // Group the batch by group_id (standalone events share no group).
    const byGroup = Object.create(null);
    const standalone = [];
    batch.forEach((a) => {
      if (a.group_id) (byGroup[a.group_id] || (byGroup[a.group_id] = [])).push(a);
      else standalone.push(a);
    });
    const groups = Object.keys(byGroup);
    // One toast for the whole batch: summary line + grouped undo actions.
    const summary = buildActivitySummary(batch, groups, byGroup, standalone);
    const span = el("span", {});
    span.appendChild(document.createTextNode(summary + "  · "));
    // iter2 CRI2-005: cap inline Undo buttons so a 5-group burst doesn't
    // produce a 5-button toast (a toast is glanceable; a 5-button toast is a
    // panel). Collect the undoable targets (group ids + standalone undoable
    // events), and if there are more than ACTIVITY_TOAST_UNDO_CAP render a
    // single "Undo all (N)" button that undoes them in order; otherwise emit
    // the per-target buttons as before.
    // F2: toast Undo availability follows the session's own bootstrap edit
    // capability, not event metadata alone. In a read-only session the
    // activity stays readable, no operative Undo is offered (the server
    // would have to refuse it with 403), and the toast explains why instead.
    var undoableGroupIds = EDIT ? groups.filter(function (gid) {
      return byGroup[gid][0] && byGroup[gid][0].undoable;
    }) : [];
    var undoableStandalone = EDIT ? standalone.filter(function (a) { return a.undoable; }) : [];
    var totalUndoable = undoableGroupIds.length + undoableStandalone.length;
    function appendUndoButton(label, handler) {
      var link = el("button", { type: "button", class: "okf-toast__action", text: label });
      link.addEventListener("click", function (e) {
        e.preventDefault();
        handler(link);
      });
      span.appendChild(document.createTextNode(" "));
      span.appendChild(link);
    }
    if (totalUndoable > ACTIVITY_TOAST_UNDO_CAP) {
      // Single coalesced button (CRI2-005): undo every group + every
      // standalone through runUndoBatch, which owns the shared button's
      // busy-state — it stays disabled from the first request until the
      // whole batch settles (F1), so a double-click can't re-trigger and no
      // helper re-enables it mid-batch.
      // F1 chronology: collect the jobs in the burst's ACTUAL arrival order
      // (each group at the position of its first member, each standalone at
      // its own position). Partitioning all groups ahead of all standalone
      // events discarded that interleaving, so with a standalone edit to X
      // followed by a grouped edit to X the batch undid the standalone
      // first and the group's restore overwrote it — leaving X at its
      // mid-burst state instead of the pre-burst state. runUndoBatch
      // reverses this list, so true chronology in means newest-first out.
      var batchJobs = [];
      var batchGroupsCollected = Object.create(null);
      batch.forEach(function (a) {
        if (!a || !a.undoable) return;
        if (a.group_id) {
          if (batchGroupsCollected[a.group_id]) return;
          if (undoableGroupIds.indexOf(a.group_id) < 0) return;
          batchGroupsCollected[a.group_id] = true;
          batchJobs.push({ groupId: a.group_id });
        } else {
          batchJobs.push(undoTargetFor(a));
        }
      });
      appendUndoButton("Undo all (" + totalUndoable + ")", function (btn) {
        runUndoBatch(batchJobs, btn, { label: "Undo all (" + totalUndoable + ")" })
          .catch(function (e) { console.error("[okf-studio] batch undo failed:", e); });
      });
    } else {
      // Inline undo buttons (CRI-018): undo directly, don't open the panel.
      // The clicked button is forwarded (F1); runUndoBatch owns its
      // busy-state end to end.
      undoableGroupIds.forEach(function (gid) {
        var members = byGroup[gid];
        appendUndoButton("Undo group (" + members.length + ")", function (btn) { undoGroup(gid, btn); });
      });
      undoableStandalone.forEach(function (a) {
        appendUndoButton("Undo", function (btn) { undoOne(a, btn); });
      });
    }
    // A batch with no operative Undo still offers a "View" link to the
    // change list; where the session cannot undo, say so honestly (F2).
    const anyUndoable = totalUndoable > 0;
    if (!anyUndoable) {
      if (!EDIT && batch.some((a) => a.undoable)) {
        span.appendChild(document.createTextNode(" "));
        span.appendChild(el("span", { text: UNDO_UNAVAILABLE_TEXT + "." }));
      }
      const view = el("button", { type: "button", class: "okf-toast__action", text: "View" });
      // Sharing is listed in Agent activity, not Changes (isSharingEvent).
      const viewPanel = batch.every(isSharingEvent) ? "agent-activity" : "changes";
      view.addEventListener("click", (e) => { e.preventDefault(); openPanel(viewPanel); });
      span.appendChild(document.createTextNode(" "));
      span.appendChild(view);
    }
    toast(span, { ttl: 5000 });
  }
  function buildActivitySummary(batch, groups, byGroup, standalone) {
    // Browser-proof copy example: "Agent added 2 links to Orders · Undo". We build a
    // compact human line mapping the raw op id to a human verb + noun,
    // resolving concept ids to titles where possible. iter-1 closeout
    // visual review caught the earlier shape that surfaced the raw op id
    // ("add_links") and the raw concept id ("tables/orders") verbatim.
    const actorRaw = (batch[0] && batch[0].actor) || "agent";
    const actor = actorRaw.charAt(0).toUpperCase() + actorRaw.slice(1);
    const titleFor = (id) => {
      // The page being read names itself. Other concepts fall back to a
      // readable form of the id's last path segment, so "tables/orders" →
      // "Orders" and "tutorials/live_studio_basics" → "Live studio basics"
      // (never a raw snake_case token). The studio does not carry the
      // bundle's full title map; a lookup per toast would cost a round trip,
      // which is not worth the latency for a notification.
      if (!id || typeof id !== "string") return "";
      if (id === state.conceptId && isConceptPage()) {
        const own = $(".okf-page__title");
        const text = own ? own.textContent.replace(/\s+/g, " ").trim() : "";
        if (text) return text;
      }
      const last = id.split("/").filter(Boolean).pop() || id;
      const seg = last.replace(/[_-]+/g, " ").trim() || last;
      return seg.charAt(0).toUpperCase() + seg.slice(1);
    };
    if (batch.length === 1) {
      const a = batch[0];
      if (a.summary && !/_/.test(a.summary)) return a.summary;
      const hv = humanVerb(a.action, 1);
      const tgt = a.ids && a.ids[0] ? (" " + hv.prep + " " + titleFor(a.ids[0])) : "";
      return actor + " " + hv.phrase + tgt;
    }
    // Multiple: collapse by action verb.
    const verbCounts = Object.create(null);
    const targetSet = Object.create(null);
    batch.forEach((a) => {
      const verb = a.action || "change";
      verbCounts[verb] = (verbCounts[verb] || 0) + 1;
      (a.ids || []).forEach((id) => { targetSet[id] = 1; });
    });
    const targets = Object.keys(targetSet);
    const parts = Object.keys(verbCounts).map((v) => {
      const hv = humanVerb(v, verbCounts[v]);
      return hv.count + " " + hv.noun;
    });
    const targetText = targets.length === 1
      ? (" to " + titleFor(targets[0]))
      : (" to " + targets.length + " concepts");
    return actor + " added " + parts.join(", ") + targetText;
  }
  // Map raw op ids to human verb + noun forms. The shape is:
  //   {phrase: "<past-tense verb>", noun: "<plural noun>", prep: "to", count: N}
  // `phrase` is used in the singular case ("Agent added a link"),
  // `noun` is used in the collapsed multi case ("3 links"), and `prep`
  // joins the singular phrase to its target ("edited text in Orders").
  // Unknown ops fall back to "changed"/"changes" so we never surface a
  // raw snake_case token to the user (fit-and-finish rule).
  function humanVerb(action, count) {
    const map = {
      add_link:           { phrase: "added a link",     noun: "links"    },
      add_links:          { phrase: "added links",      noun: "links"    },
      remove_link:        { phrase: "removed a link",   noun: "removals" },
      add_relation:       { phrase: "added a relation", noun: "relations"},
      add_entity:         { phrase: "added an entity",  noun: "entities" },
      add_entities:       { phrase: "added entities",   noun: "entities" },
      set_frontmatter:    { phrase: "set a field",      noun: "fields"   },
      set_tag:            { phrase: "set a tag",        noun: "tags"     },
      add_tag:            { phrase: "added a tag",      noun: "tags"     },
      append_body_section:{ phrase: "added a section",  noun: "sections" },
      write_concept:      { phrase: "wrote the concept",noun: "writes"   },
      replace_text:       { phrase: "edited text",      noun: "text edits", prep: "in" },
      update_section:     { phrase: "updated a section",noun: "section updates", prep: "in" },
      undo_restore:       { phrase: "undid a change",   noun: "undos"    },
      repair:             { phrase: "ran a repair",     noun: "repairs"  },
      auto_repair:        { phrase: "auto-repaired",    noun: "repairs"  },
    };
    const entry = map[action] || { phrase: "made a change", noun: "changes" };
    return { phrase: entry.phrase, noun: entry.noun, prep: entry.prep || "to", count: count };
  }
  function verbLabel(verb, count) {
    // Crude pluralisation good enough for the known action set.
    if (count === 1) return verb;
    if (verb.endsWith("s")) return verb;
    if (verb.endsWith("y")) return verb.slice(0, -1) + "ies";
    return verb + "s";
  }

  // iter2 G12: burst-coalescing detection. When 10+ standalone events land
  // within 1s, tag them with a shared burst_id so the change list can render
  // them as ONE expandable "Agent made N changes" row instead of N rows at
  // the top. Grouped events (group_id) are already coalesced by the group
  // renderer, so burst detection only applies to standalone events. An OPEN
  // burst keeps absorbing fast arrivals (so a burst of 12 coalesces all 12,
  // not 10 + 2); it closes when an event arrives >1s after the last one.
  const BURST_WINDOW_MS = 1000;
  const BURST_THRESHOLD = 10;
  let _burstArrivalWindow = [];  // [{id, t}]
  let _burstOpenId = null;
  let _burstLastT = 0;
  function maybeTagBurst(ev) {
    if (!ev || ev.group_id || !ev.id) return;
    const now = (ev.ts && !isNaN(Date.parse(ev.ts))) ? Date.parse(ev.ts) : Date.now();
    // Close any open burst if this event arrived after the 1s quiet gap.
    if (now - _burstLastT > BURST_WINDOW_MS) {
      _burstArrivalWindow = [];
      _burstOpenId = null;
    }
    _burstLastT = now;
    if (_burstOpenId) {
      // A burst is open: keep tagging fast arrivals into it directly.
      if (!ev.burst_id) ev.burst_id = _burstOpenId;
      return;
    }
    _burstArrivalWindow.push({ id: ev.id, t: now });
    if (_burstArrivalWindow.length >= BURST_THRESHOLD) {
      _burstOpenId = "burst-" + now;
      _burstArrivalWindow.forEach((a) => {
        const e = state.events.find((x) => x.id === a.id);
        if (e && !e.burst_id && !e.group_id) e.burst_id = _burstOpenId;
      });
      _burstArrivalWindow = [];  // window served its purpose; open burst tags directly
    }
  }

  function upsertEvent(ev) {
    if (!ev) return;
    const id = eventAttentionKey(ev);
    if (id) {
      const i = state.events.findIndex((e) => eventAttentionKey(e) === id);
      if (i >= 0) { state.events[i] = ev; maybeTagBurst(ev); buildIndexEngagement(); return; }
    }
    state.events.unshift(ev);
    if (id && isActionableChange(ev)) {
      if (state.attention.initialized) state.attention.unread.add(id);
      state.attention.known.add(id);
    }
    maybeTagBurst(ev);
    // iter2 G9: raised from 4000 to 10000 to align with the change-list
    // virtualization ceiling (CHANGE_TOTAL_CAP). The virtualized list keeps
    // only ~80 rows in the DOM regardless, so a larger in-memory log no
    // longer means a larger DOM. Events beyond the cap rotate out (oldest
    // first); the full history lives in events.jsonl on disk.
    if (state.events.length > 10000) state.events.length = 10000;
    buildIndexEngagement();
  }

  // ====================================================================
  // 13. Boot
  // ====================================================================
  function debounce(fn, ms) {
    let t = null;
    return function () {
      const ctx = this, args = arguments;
      if (t) clearTimeout(t);
      t = setTimeout(() => fn.apply(ctx, args), ms);
    };
  }

  // ====================================================================
  // 9.4. Conflict-UX modal (INTENT-008) — disk-vs-agent write collision
  // ====================================================================
  // When /__apply returns 409 with {conflict: true, concept, expected_rev,
  // current_rev}, surface a modal asking the user how to resolve. Three
  // actions:
  //   * "View diff"     — open a panel that fetches /__diff and renders rows.
  //   * "Keep mine"     — dismiss (the on-disk bytes stay; the agent's write
  //                       is dropped).
  //   * "Take the agent's" — re-submit the apply WITHOUT expected_rev (force
  //                       overwrite). tokenFetch's caller sees the new Response.
  //
  // F9 (accurate comparison): rejection events carry revision REFERENCES,
  // never the rejected edit's proposed bytes, and /__diff reads BOTH sides
  // from the undo-snapshot ring. The comparison is therefore always labelled
  // what it actually is — the retained BASELINE revision the agent's edit was
  // prepared against, versus the retained CURRENT (on-disk) revision — and a
  // revision that was never snapshotted (the common case: an external editor
  // changed bytes no studio write ever captured) yields a specific truthful
  // "unavailable" explanation. Rejections stay side-effect-free: no snapshot
  // or history is manufactured for a rejected write.
  //
  // F9 (per-conflict response ownership, Q4): async handlers used to read the
  // GLOBAL _retryArgs/_resolve at await time, so a held conflict A's diff
  // fetch or force-retry could render into, re-enable, close, or settle a
  // NEWER conflict B that replaced it. Each modal show now mints an admission
  // session; every async handler captures its own session at entry and drops
  // its result when a newer session owns the modal. This is the smallest
  // owner guard for this modal — no broad cancellation abstraction — and the
  // R6-6 prior-owner settlement (a newer conflict settles the older one with
  // its original 409) is unchanged.
  //
  // ARIA (§13.5): role="alertdialog" + aria-labelledby + focus trap + Esc.
  // Reduced-motion aware (no jarring animation; the modal simply appears).
  let conflictState = { open: false, overlay: null, lastFocus: null, session: null };
  let _conflictSessionSeq = 0;

  function showConflictModal({ data, url, opts, originalBodyObj, originalResponse }) {
    // Build the overlay once; reuse across conflicts.
    if (!conflictState.overlay) _buildConflictModal();
    // R6-6: if a conflict is already open, settle the PRIOR owner before
    // overwriting the modal. Otherwise the prior caller's tokenFetch promise
    // hangs forever (an SSE agent_conflict arriving during an HTTP-409
    // conflict, or two rapid applies). "Keep mine" is the non-destructive
    // default — the prior owner resolves with its original 409 response and
    // sees !ok, exactly as if the user had dismissed it.
    if (conflictState.open) _closeConflict("keep");
    data = data || {};
    const concept = String(data.concept || "");
    // CLI-agent conflicts (SSE `agent_conflict`) carry no retry context —
    // there is no browser-side request to re-submit, so "Take the agent's
    // edit" is meaningless there and stays hidden. HTTP-409 conflicts
    // (tokenFetch) pass url/opts and get all three actions.
    const takeBtnEl = $(".okf-conflict__take", conflictState.overlay);
    if (takeBtnEl) takeBtnEl.hidden = !url;
    // F9: offer the comparison only where the session's actual bootstrap
    // authority supports /__diff. Everywhere else the modal states why it is
    // unavailable instead of launching a doomed privileged request.
    const diffBtn = $(".okf-conflict__diff-btn", conflictState.overlay);
    const diffNa = $(".okf-conflict__diff-na", conflictState.overlay);
    if (diffBtn && diffNa) {
      diffBtn.hidden = !CAN_VIEW_DIFF;
      diffNa.hidden = CAN_VIEW_DIFF;
      diffNa.textContent = PUBLIC_INGRESS
        ? "Comparison is not available over public links."
        : "Comparison is not available in this read-only view.";
    }
    const headingId = "okf-conflict-title";
    const heading = $("#" + headingId, conflictState.overlay) ||
      conflictState.overlay.querySelector("." + conflictState.overlay.getAttribute("aria-labelledby"));
    if (heading) heading.textContent = "You edited " + concept + " in your editor while the agent was updating it.";
    // Reset the diff panel + button states.
    const diffPanel = $(".okf-conflict__diff", conflictState.overlay);
    if (diffPanel) {
      diffPanel.innerHTML = "";
      diffPanel.setAttribute("hidden", "");
    }
    const diffBtnReset = $(".okf-conflict__diff-btn", conflictState.overlay);
    if (diffBtnReset) diffBtnReset.disabled = false;
    conflictState.open = true;
    conflictState.overlay.hidden = false;
    // Register the alertdialog on the shared overlay stack (see openPalette)
    // so it is correctly ordered above the slide-over panel and the theme
    // Appearance menu yields Escape to it as the topmost modal.
    if (window.OKFOverlayStack) window.OKFOverlayStack.push(conflictState.overlay);
    conflictState.lastFocus = document.activeElement;
    // Mint this conflict's admission session BEFORE focusing so the async
    // action handlers can capture and later re-admit against it.
    const session = {
      id: ++_conflictSessionSeq,
      retryArgs: { url, opts, originalBodyObj, data },
      originalResponse: originalResponse || null,
      resolve: null,
    };
    conflictState.session = session;
    // Focus the first action button after a tick (let the modal render).
    setTimeout(() => {
      if (conflictState.session !== session) return; // a newer conflict owns the modal
      const first = focusableIn(conflictState.overlay)[0];
      if (first) try { first.focus({ preventScroll: true }); } catch (e) {}
    }, 20);
    // Resolve the caller's promise once the user picks an action.
    return new Promise((resolve) => { session.resolve = resolve; });
  }

  function _buildConflictModal() {
    const overlay = el("div", {
      class: "okf-conflict-overlay", hidden: "",
      role: "alertdialog", "aria-modal": "true",
      "aria-labelledby": "okf-conflict-title",
    });
    const card = el("div", { class: "okf-conflict" });
    const title = el("h2", {
      class: "okf-conflict__title", id: "okf-conflict-title",
      text: "Conflict",  // replaced per-show with the concept-named copy
    });
    const lede = el("p", {
      class: "okf-conflict__lede",
      // Honest, non-jargon phrasing. The user just needs to pick a resolution.
      text: "Pick how to resolve this edit.",
    });
    const actions = el("div", { class: "okf-conflict__actions" });
    const viewBtn = el("button", {
      type: "button", class: "okf-studiobtn okf-conflict__diff-btn",
      text: "View diff",
    });
    // F9: where the session's bootstrap authority cannot read /__diff (no
    // token, or any public-ingress mode) the action is replaced by this note
    // so the unavailability is explained, never hidden.
    const diffNa = el("p", {
      class: "okf-conflict__diff-na", hidden: "", role: "note",
      text: "Comparison is not available in this view.",
    });
    // iter2 CRI2-008: "Keep mine" is the primary action (the non-destructive
    // default; the user's on-disk edit stays). "Take the agent's" discards
    // the user's edit and applies the agent's, so it is visually distinct
    // (warn-toned) but NOT primary, per platform HIG guidance that
    // destructive options not be the path of least visual resistance.
    const keepBtn = el("button", {
      type: "button", class: "okf-studiobtn okf-studiobtn--primary",
      text: "Keep mine",
    });
    const takeBtn = el("button", {
      type: "button", class: "okf-studiobtn okf-conflict__take",
      // iter3 CRI3-003: was "Take the agent's" (dangling possessive; read
      // as a typo). The full verb-phrase "Take the agent's edit" pairs
      // cleanly with "Keep mine" — both are now complete predicates.
      text: "Take the agent's edit",
      title: "Discards your on-disk edit; the agent's version is applied.",
    });
    const diffPanel = el("div", {
      class: "okf-conflict__diff", "aria-live": "polite",
      // F9: describe the actual comparison — retained revisions, not the
      // rejected edit's proposed bytes (those are never delivered).
      "aria-label": "Line diff between the agent's baseline revision and the retained current revision", hidden: "",
    });
    actions.appendChild(viewBtn);
    actions.appendChild(diffNa);
    actions.appendChild(keepBtn);
    actions.appendChild(takeBtn);
    card.appendChild(title);
    card.appendChild(lede);
    card.appendChild(actions);
    card.appendChild(diffPanel);
    overlay.appendChild(card);

    // "View diff": fetch /__diff?concept=…&from=…&to=… and render rows.
    // The endpoint requires the per-session X-OKF-Token header (server.py:
    // _handle_diff enforces it via _check_write_auth, returning 403
    // otherwise); tokenFetch is the studio's write-fetch wrapper that
    // attaches the token from the embedded BOOT payload. F9: the handler
    // captures its own conflict session and re-admits after every await —
    // a result owned by a superseded conflict must not render into (or
    // re-enable controls of) a newer conflict's modal. The outcome states
    // are distinct: missing snapshot, denied permission, transport failure,
    // and a genuine empty diff never share one message.
    viewBtn.addEventListener("click", async () => {
      const session = conflictState.session;
      if (!session) return;
      viewBtn.disabled = true;
      diffPanel.innerHTML = "";
      diffPanel.removeAttribute("hidden");
      const placeholder = el("div", { class: "okf-conflict__diff-loading", text: "Loading diff…" });
      diffPanel.appendChild(placeholder);
      try {
        const { data: cdata } = session.retryArgs;
        const concept = encodeURIComponent(String(cdata.concept || ""));
        const fromRev = encodeURIComponent(String(cdata.expected_rev || ""));
        const toRev = encodeURIComponent(String(cdata.current_rev || ""));
        const res = await tokenFetch(
          "/__diff?concept=" + concept + "&from=" + fromRev + "&to=" + toRev,
          { headers: { Accept: "application/json" } },
        );
        // F9 admission: a newer conflict replaced this one while the fetch
        // was in flight. Drop the stale result untouched.
        if (conflictState.session !== session) return;
        let payload = null;
        try { payload = await res.json(); } catch (e) { payload = null; }
        if (conflictState.session !== session) return;
        placeholder.remove();
        // F9 caption: the comparison is labelled what it actually is.
        const caption = el("div", { class: "okf-conflict__diff-caption" });
        caption.appendChild(el("span", { class: "okf-conflict__diff-side", text: "Baseline " }));
        caption.appendChild(el("code", { text: String(cdata.expected_rev || "?") }));
        caption.appendChild(document.createTextNode(" → "));
        caption.appendChild(el("span", { class: "okf-conflict__diff-side", text: "Current " }));
        caption.appendChild(el("code", { text: String(cdata.current_rev || "?") }));
        caption.appendChild(el("span", {
          class: "okf-conflict__diff-note",
          text: "Retained revisions only — the rejected edit's proposed bytes are not shown.",
        }));
        diffPanel.appendChild(caption);
        const unavailable = (message) => {
          diffPanel.appendChild(el("p", { class: "okf-conflict__diff-empty", text: message }));
        };
        if (!res.ok) {
          // Distinct, truthful outcomes. 404: one side was never snapshotted
          // (the common genuine conflict — externally changed bytes no studio
          // write captured). 403: the session lacks read authority. Anything
          // else: surface the server's own error when it has one.
          if (res.status === 404) {
            unavailable("Comparison unavailable: a revision needed for this comparison was not retained in " +
              "the snapshot history, so there is nothing to compare. Your on-disk content is unchanged.");
          } else if (res.status === 403) {
            unavailable("Comparison unavailable: this session does not have permission to read revisions.");
          } else {
            unavailable(payload && payload.error ? payload.error : "Diff unavailable (HTTP " + res.status + ").");
          }
          return;
        }
        if (!payload || payload.ok === false) {
          unavailable(payload && payload.error ? payload.error : "Diff unavailable.");
          return;
        }
        const rows = Array.isArray(payload.diff) ? payload.diff : [];
        if (!rows.length) {
          unavailable("The baseline and current revisions have no textual differences.");
          return;
        }
        const table = el("table", { class: "okf-conflict__diff-table" });
        const tbody = el("tbody");
        rows.forEach((row) => {
          const tr = el("tr", { class: "okf-conflict__diff-row okf-conflict__diff-row--" + (row.kind || "ctx") });
          tr.appendChild(el("td", { class: "okf-conflict__diff-num", text: String(row.num != null ? row.num : "") }));
          tr.appendChild(el("td", { class: "okf-conflict__diff-kind", text: row.kind === "add" ? "+" : (row.kind === "del" ? "-" : " ") }));
          const td = el("td", { class: "okf-conflict__diff-text" });
          td.textContent = String(row.text != null ? row.text : "");
          tr.appendChild(td);
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        diffPanel.appendChild(table);
      } catch (e) {
        if (conflictState.session !== session) return;
        placeholder.remove();
        diffPanel.appendChild(el("p", {
          class: "okf-conflict__diff-empty",
          text: "Diff fetch failed: " + (e && e.message ? e.message : String(e)),
        }));
      } finally {
        // Allow re-clicking to refresh — but only while this conflict still
        // owns the modal (a superseded session must never re-enable the
        // newer conflict's controls mid-fetch).
        setTimeout(() => {
          if (conflictState.session === session) viewBtn.disabled = false;
        }, 500);
      }
    });

    // "Keep mine": dismiss, the on-disk bytes stay. Resolve with the
    // original 409 Response so the caller sees !ok and knows the write
    // did not land.
    keepBtn.addEventListener("click", () => _closeConflict("keep"));

    // "Take the agent's": re-submit the apply WITHOUT expected_rev. F9: the
    // explicit user action keeps its force-retry authority; the session guard
    // only stops a superseded conflict's response from closing or settling a
    // newer conflict's modal (the retry itself already went out).
    takeBtn.addEventListener("click", async () => {
      const session = conflictState.session;
      if (!session || !(session.retryArgs && session.retryArgs.url)) return;
      takeBtn.disabled = true;
      keepBtn.disabled = true;
      viewBtn.disabled = true;
      try {
        const { url: rurl, opts: ropts, originalBodyObj: rbody } = session.retryArgs;
        // Build a fresh body: drop expected_rev from a cloned object.
        let bodyObj = rbody;
        if (bodyObj && typeof bodyObj === "object") {
          bodyObj = Object.assign({}, bodyObj);
          delete bodyObj.expected_rev;
        }
        const rheaders = Object.assign({ "X-OKF-Token": TOKEN }, ropts.headers || {});
        if (bodyObj) {
          rheaders["Content-Type"] = "application/json";
        }
        const newRes = await fetch(rurl, {
          method: ropts.method || "POST",
          headers: rheaders,
          body: bodyObj ? JSON.stringify(bodyObj) : ropts.body,
        });
        if (conflictState.session !== session) return;
        _closeConflict("take", newRes);
      } catch (e) {
        if (conflictState.session !== session) return;
        // The retry itself failed; resolve with a synthetic 503 so the
        // caller sees an error rather than hanging.
        _closeConflict("take", new Response(JSON.stringify({ ok: false, error: String(e) }), {
          status: 503, headers: { "Content-Type": "application/json" },
        }));
      }
    });

    // Click outside the card dismisses (acts like "Keep mine").
    overlay.addEventListener("mousedown", (e) => {
      if (e.target === overlay) _closeConflict("keep");
    });

    // Global keyboard wiring (registered once).
    document.addEventListener("keydown", _conflictKeydown);

    document.body.appendChild(overlay);
    conflictState.overlay = overlay;
  }

  function _conflictKeydown(e) {
    // R5-13: the conflict alertdialog is the explicit topmost modal. It owns
    // Escape and Tab while open. The slide-over panel keydown handler yields
    // when conflictState.open (see §9 panel wiring), so this handler is the
    // sole owner of these keys for the conflict layer. Guard against an
    // already-prevented event so a prior topmost consumer is never overridden.
    if (!conflictState.open) return;
    if (e.defaultPrevented) return;
    // Yield Escape to a higher registered overlay so a single Escape closes
    // only the actually-topmost layer (mirrors theme.js / the palette/panel).
    if (e.key === "Escape") {
      if (!_overlayIsTopmost(conflictState.overlay)) return;
      e.preventDefault(); e.stopPropagation(); _closeConflict("keep");
    }
    // Focus trap: Tab/Shift+Tab cycles inside the alertdialog.
    else if (e.key === "Tab") {
      e.preventDefault();
      trapFocusIn(conflictState.overlay, !e.shiftKey);
    }
  }

  function _closeConflict(action, newResponse) {
    if (!conflictState.open) return;
    // F9: settle exactly the session that owns the modal. A superseded
    // session was already settled by the show that replaced it (R6-6).
    const session = conflictState.session;
    conflictState.session = null;
    conflictState.open = false;
    conflictState.overlay.hidden = true;
    if (window.OKFOverlayStack) window.OKFOverlayStack.remove(conflictState.overlay);
    // Re-enable buttons for the next conflict. $$ (querySelectorAll): a
    // single $ here returned one element and the forEach threw before
    // session.resolve, leaving every settled conflict's caller hanging.
    $$("button", conflictState.overlay).forEach((b) => { b.disabled = false; });
    // Restore focus to the trigger.
    if (conflictState.lastFocus && typeof conflictState.lastFocus.focus === "function") {
      try { conflictState.lastFocus.focus({ preventScroll: true }); } catch (e) {}
    }
    conflictState.lastFocus = null;
    if (!session || !session.resolve) return;
    if (action === "take" && newResponse) {
      session.resolve(newResponse);
    } else {
      // "Keep" / Esc / outside-click → the original 409 Response.
      session.resolve(session.originalResponse);
    }
  }

  // ---- sidebar panel system (user-requested: collapsible, reorderable, resizable) ----
  var SIDEBAR_KEY = "okf:sidebar";
  // Document traversal belongs to document-outline.js and proposal operations
  // belong to Studio. The page rail contains recommendations only.
  var SIDEBAR_PANELS = ["related"];
  var INTENTS = [
    // R3: each intent starts an ordinary comment. The outcomes say what the
    // comment asks for and never borrow X3 proposal/review wording.
    { id: "add-section", label: "Draft a new section", prompt: "Draft a new section about", outcome: "Asks the agent to draft one scoped section." },
    { id: "split-doc", label: "Plan a document split", prompt: "Propose how to split this document into", outcome: "Asks the agent to suggest split boundaries before any broad change." },
    { id: "add-links", label: "Suggest useful links", prompt: "Suggest relevant links from this concept to", outcome: "Asks the agent to identify and explain candidate links." },
    { id: "enrich", label: "Suggest an enrichment", prompt: "Propose a scoped enrichment for this page about", outcome: "Asks the agent to describe the intended addition first." },
  ];

  function getSidebarState() {
    try {
      var s = JSON.parse(localStorage.getItem(SIDEBAR_KEY) || "{}");
      if (!s.order || !Array.isArray(s.order)) s.order = SIDEBAR_PANELS.slice();
      if (!s.collapsed) s.collapsed = {};
      if (!s.width) s.width = 260;
      return s;
    } catch (e) { return { order: SIDEBAR_PANELS.slice(), collapsed: {}, width: 260 }; }
  }
  function saveSidebarState(s) {
    try { localStorage.setItem(SIDEBAR_KEY, JSON.stringify(s)); } catch (e) {}
  }

  function buildSidebarPanels() {
    var sidebar = $(".okf-page__sidebar");
    if (!sidebar) return;
    var sbState = getSidebarState();
    // Apply saved width.
    document.documentElement.style.setProperty("--okf-sidebar-w", sbState.width + "px");

    // Capture the existing local graph node (preserve event listeners
    // by moving the actual node, not copying HTML).
    var existingGraph = $(".okf-local-graph", sidebar);

    // Clear sidebar.
    sidebar.innerHTML = "";

    // The page rail is recommendations only in both SSR and live Studio.
    sidebar.setAttribute("aria-label", "Related concepts");

    // Build panels in saved order.
    sbState.order.forEach(function (panelId) {
      var panel = buildPanel(panelId, sbState, existingGraph);
      if (panel) sidebar.appendChild(panel);
    });

    // Wire drag-and-drop reordering.
    wireSidebarDnD(sidebar, sbState);
    // Re-render the local graph pills inside the new panel location so
    // wiki.js's click handlers (navigation) are properly bound.
    if (window.okfWiki && window.okfWiki.renderLocalGraph) {
      try { window.okfWiki.renderLocalGraph(); } catch (e) {}
    }
  }

  function buildPanel(panelId, sbState, existingGraph) {
    var isCollapsed = !!sbState.collapsed[panelId];
    var panel = el("div", {
      class: "okf-sidebar-panel" + (isCollapsed ? " okf-sidebar-panel--collapsed" : ""),
      "data-panel-id": panelId,
      draggable: "true",
    });
    var header = el("div", { class: "okf-sidebar-panel__header" });
    var toggle = el("button", {
      class: "okf-sidebar-panel__toggle",
      type: "button",
      "aria-label": isCollapsed ? "Expand" : "Collapse",
      text: isCollapsed ? "+" : "-",
    });
    toggle.addEventListener("click", function () {
      var p = panel.classList.toggle("okf-sidebar-panel--collapsed");
      toggle.textContent = p ? "+" : "-";
      toggle.setAttribute("aria-label", p ? "Expand" : "Collapse");
      sbState.collapsed[panelId] = p;
      saveSidebarState(sbState);
    });
    header.appendChild(toggle);
    header.appendChild(el("span", { class: "okf-sidebar-panel__title", text: sidebarPanelTitle(panelId) }));
    panel.appendChild(header);

    var body = el("div", { class: "okf-sidebar-panel__body" });
    if (panelId === "related") {
      // Move the actual DOM node to preserve event listeners on the
      // local graph pills (buttons that navigate to concepts).
      if (existingGraph) body.appendChild(existingGraph);
    }
    panel.appendChild(body);
    return panel;
  }

  function sidebarPanelTitle(id) {
    return id === "related" ? "Related" : id;
  }

  function buildIntentsPanel(body) {
    var container = el("div", { class: "okf-sidebar-intents" });
    INTENTS.forEach(function (intent) {
      var row = el("div", { class: "okf-studio-quick-action" });
      var btn = el("button", { class: "okf-sidebar-intent", type: "button", text: intent.label,
        disabled: !EDIT, "aria-describedby": "okf-intent-" + intent.id });
      btn.addEventListener("click", function () {
        // A post that is pending, being confirmed, or unconfirmed with its
        // sent draft owns the draft: the prompt is refused as a new selection
        // is, never written over it.
        if (refuseSelectionWhilePosting()) return;
        // Pre-fill the comment composer with the intent prompt. It is a new
        // draft, so no earlier post's late restore may overwrite it.
        state.draftBody = intent.prompt + " ";
        state.draftAnchor = defaultDraftAnchor();
        state.draftGeneration = (state.draftGeneration || 0) + 1;
        openPanel("comments", { focusComposer: true });
      });
      row.appendChild(btn);
      row.appendChild(el("p", { id: "okf-intent-" + intent.id, class: "okf-studio-quick-action__outcome",
        text: "Starts a comment · " + intent.outcome + " This does not edit immediately." }));
      container.appendChild(row);
    });
    body.appendChild(container);
  }

  function renderProposalComposer(body) {
    // Proposal Review is explicitly experimental and its private mutation
    // capability is intentionally never exposed to ordinary Studio HTML.
    // Until a dedicated browser adapter ships, fail honestly instead of
    // presenting a form that cannot preserve its authority/idempotency contract.
    const active = PROPOSAL_CAPABILITY.active === true;
    const status = el("div", { class: "okf-proposal-unavailable", dataset: { proposalState: "unavailable" } });
    status.appendChild(el("p", { class: "okf-proposal-unavailable__title",
      text: active ? "Experimental capability active" : "Experimental capability unavailable" }));
    status.appendChild(el("p", { text: active
      ? "Proposal creation is not available in this browser because its private capability is never exposed. Use the local agent or proposal CLI; comments still request work directly."
      : "Proposal review is not enabled for this session, and its private capability is not exposed. Comments remain available; no review workflow is being simulated." }));
    status.appendChild(el("a", { class: "okf-studiobtn", href: "/reference/proposal_diff_review",
      text: "Open proposal review guide" }));
    body.appendChild(status);
  }

  function wireSidebarDnD(sidebar, sbState) {
    var dragSrc = null;
    sidebar.addEventListener("dragstart", function (e) {
      var panel = e.target.closest(".okf-sidebar-panel");
      if (!panel) return;
      dragSrc = panel;
      panel.classList.add("okf-sidebar-panel--dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    sidebar.addEventListener("dragend", function (e) {
      var panel = e.target.closest(".okf-sidebar-panel");
      if (panel) panel.classList.remove("okf-sidebar-panel--dragging");
      $$(".okf-sidebar-panel--drag-target", sidebar).forEach(function (p) {
        p.classList.remove("okf-sidebar-panel--drag-target");
      });
    });
    sidebar.addEventListener("dragover", function (e) {
      e.preventDefault();
      var panel = e.target.closest(".okf-sidebar-panel");
      if (!panel || panel === dragSrc) return;
      $$(".okf-sidebar-panel--drag-target", sidebar).forEach(function (p) {
        p.classList.remove("okf-sidebar-panel--drag-target");
      });
      panel.classList.add("okf-sidebar-panel--drag-target");
    });
    sidebar.addEventListener("drop", function (e) {
      e.preventDefault();
      var target = e.target.closest(".okf-sidebar-panel");
      if (!target || !dragSrc || target === dragSrc) return;
      // Insert dragSrc before or after target based on drop position.
      var rect = target.getBoundingClientRect();
      var after = (e.clientY - rect.top) > rect.height / 2;
      if (after) {
        target.parentNode.insertBefore(dragSrc, target.nextSibling);
      } else {
        target.parentNode.insertBefore(dragSrc, target);
      }
      // Save new order.
      sbState.order = $$(".okf-sidebar-panel", sidebar).map(function (p) {
        return p.getAttribute("data-panel-id");
      });
      saveSidebarState(sbState);
    });
  }

      function boot() {
      mountBar();
      wirePaletteKeys();
      // Test-only deterministic failure injection after boot has genuinely
      // started. It is opt-in behind a separate sentinel so a normal
      // bootstrap payload can never request a failure accidentally.
      if (window.__OKF_LOOM_TEST__ === true &&
          window.__OKF_LOOM_STUDIO_TEST_FAILURE__ === "boot") {
        throw new Error("Injected Studio boot failure.");
      }
      if (isConceptPage()) {
      ensureViewWrap();
      setView(state.view); // also deep-links + lazy-loads source if needed
      bindSelectionAffordance();
      buildSidebarPanels();
      // Document details stays as the reader left it: it starts collapsed at
      // every width and Studio never opens or closes it.
    } else if (document.getElementById("detail-body")) {
      // Graph page: bind selection affordance for the detail panel.
      bindSelectionAffordance();
    } else if (feedbackPageRoot()) {
      // Reserved index/log document: page-level feedback for selected text.
      bindSelectionAffordance();
    }
    mountPageFeedback();
    // iter1 CRI-004: stamp data-concept-id on list/search rows so presence
    // focus can highlight them (also runs on index/search pages).
    stampConceptIds();
    if (fullGraphPage || needsGraphStructure()) void loadGraph();
    loadComments();
    wireLive();
    // iter1 CRI-002: apply text-range marks for any pre-existing comments
    // on the open concept (loaded by loadComments above).
    applyCommentMarks();
    rebuildMarginMarkers();
    // Apply bootstrap presence highlight if focus present.
    renderPresence(state.presence);
    // Rebuild markers after fonts/layout settle.
      setTimeout(rebuildMarginMarkers, 400);
      // R6-3: pagehide must be PERSISTENT (not once) so repeated BFCache
      // cycles are handled. On every pagehide: abort operations, bump
      // generation, clear editor, dismiss peek bar. On persisted pageshow:
        // reconcile canonical state, re-apply marks, restore editor DOM.
        window.addEventListener("pagehide", function () {
          abortAnnotationOperations("pagehide");
          ++studioGraphGeneration;
          studioGraphPending = null;
          state.annotationGeneration++;
          state.annotationEditor = null;
          // R6-9: the selection-mode banner + body class + dataset describe an
          // active anchor-selection surface owned by the (now-cleared) editor.
          // They MUST be cleared on freeze so a BFCache restore never shows a
          // stale "document is active for selection" banner over a page whose
          // editor owner was discarded. syncSelectionModeBanner removes the
          // banner + body class (editor is null) and disconnects the resize
          // observer; also drop the dataset flag and any pending draft.
          state.selectionDraft = null;
          if (typeof document !== "undefined" && document.body) delete document.body.dataset.okfSelectionReady;
          try { syncSelectionModeBanner(); } catch (e) {}
          _dismissReanchorPeek();
        });
        window.addEventListener("pageshow", function (event) {
          if (!event.persisted) return;
          // pagehide fences pending structure completions. Restore only the
          // same lazy consumers as live updates; graph.js owns full pages.
          refreshGraph();
          // R6-3: BFCache restore — re-apply marks and rebuild markers from
          // the still-current state.doc. The live.js pageshow handler
          // triggers a canonical resync; we just reconcile presentation.
          // R6-9: defensively clear any selection banner/body class/dataset
          // that survived the freeze (e.g. injected by an extension or a race
          // where pagehide ran before the banner was torn down), then rebuild
          // marks/markers/panel from canonical state.
          state.selectionDraft = null;
          if (typeof document !== "undefined" && document.body) delete document.body.dataset.okfSelectionReady;
          try { syncSelectionModeBanner(); } catch (e) {}
          applyCommentMarks();
          rebuildMarginMarkers();
          renderCommentsPanel();
        });
    }

  // Expose the public API.
  window.okfLoomStudio = {
    register,
    applyDoc,
    refreshGraph,
    openPanel,
    closePanel,
    openPalette,
    setView,
    // F10: exact-identity navigation into the retained comments/changes
    // records. Resolution happens in retained data; a missing record gets a
    // terminal explanation instead of a silent no-op.
    revealActivity,
    revealComment,
    get state() { return state; },
    get panels() { return panels; },
    ctx,
    // Test/debug helpers.
      _toast: toast, _loadComments: loadComments, _renderChangeList: renderChangeList,
      _ensureSourceLoaded: ensureSourceLoaded,
      _captureCanonicalAnchor: captureCanonicalAnchor,
      _renderComments: renderCommentsPanel,
      _applyConnectionState: applyConnectionState,
      _proposalRetention: proposalRetentionForTest,
      _buildIndexEngagement: buildIndexEngagement,
    _annotationFetch: annotationFetch,
    _agentAvailability: agentAvailability,
    _validProjection: validProjection,
    _projectionError: projectionError,
    _projectionValidationCount: projectionValidationCountForTest,
    _validateStagedTuple: validateStagedTuple,
    _postCommentFromComposer: postCommentFromComposer,
    _showConflictModal: (payload) => showConflictModal({
      data: payload,
      url: "/__apply",
      opts: { method: "POST" },
      originalBodyObj: { kind: "add_tag", target: payload && payload.concept, args: { tag: "x" } },
      originalResponse: new Response(JSON.stringify(payload || {}), { status: 409 }),
    }),
  };

    function startBoot() {
      try {
        boot();
        if (!window.okfLoomStudio || typeof window.okfLoomStudio !== "object" ||
            typeof window.okfLoomStudio.openPanel !== "function" ||
            typeof window.okfLoomStudio.setView !== "function") {
          throw new Error("Studio boot did not expose a usable API.");
        }
        // Only now is the live fallback false. The class and the status event
        // are published together after all synchronous boot work completed.
        document.documentElement.classList.add("okf-studio-booted");
        publishStudioStatus("ready", null, null);
        try {
          window.dispatchEvent(new CustomEvent("okf-loom:studioBooted", {
            detail: window.okfStudioReadiness,
          }));
        } catch (eventError) { /* status remains observable */ }
      } catch (error) {
        // A partially-created API is not a usable Studio. Remove it so
        // capture/readiness cannot mistake a post-start exception for success.
        try { delete window.okfLoomStudio; } catch (deleteError) { window.okfLoomStudio = null; }
        document.documentElement.classList.remove("okf-studio-booted");
        unmountStudioShell();
        publishStudioStatus("failed", "boot-failure", error);
      }
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", startBoot, { once: true });
    } else {
      startBoot();
    }
  })();
