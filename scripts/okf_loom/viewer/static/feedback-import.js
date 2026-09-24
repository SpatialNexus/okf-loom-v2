/* feedback-import.js: local operator import of one portable feedback note (X2).
 *
 * Served only by the live server on GET /__feedback-import. The operator
 * chooses a note file; its exact bytes are previewed by the server, and only
 * a ready preview offers an import. Confirm re-sends the SAME bytes with the
 * preview token. The server is the authority for every decision; this file
 * only presents it and marks a preview stale when it can no longer be trusted.
 *
 * States (main[data-state]):
 *   idle -> reading -> previewing -> preview_ready | blocked | stale
 *   previewing -> stale (a trigger seen while the request was in flight,
 *                        including a page event for the target it returns)
 *   preview_ready -> importing -> imported | blocked | stale
 *   preview_ready -> stale (TTL, target page event, resync, connection change,
 *                           bfcache restore)
 *   stale | blocked(retryable) -> previewing (same bytes)
 *   any -> reading (new file; the generation counter drops older results)
 *
 * Every string from the note or the server reaches the DOM through
 * textContent or setAttribute. The note bytes stay in memory only.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-okf-feedback-import]");
  if (!root || root.__okfFeedbackImport) return;
  root.__okfFeedbackImport = true;

  var PREVIEW_URL = "/__feedback-import/preview";
  var CONFIRM_URL = "/__feedback-import/confirm";
  var REQUEST_DEADLINE_MS = 30000;
  var TTL_MARGIN_MS = 5000;

  // ---- Copy (X2 design section 4 wording where it exists) ----------------
  var COPY = {
    revision_mismatch: "The page has changed since this note was written; it can't be imported.",
    target_missing: "The page no longer exists.",
    already_imported: "Already imported.",
    conflict: "A different note with this ID was already imported, or the ID has expired from the duplicate check.",
    stale: "The page or session changed; preview again.",
    local_only: "Import is available only on the local Studio, not through a tunnel.",
    read_only: "This Studio is read-only, so notes can't be imported here. Start the Studio without --no-edit to import.",
    forbidden: "This page's Studio session has ended. Reload the page to continue.",
    too_large: "This file is larger than a feedback note can be.",
    unreadable: "This file couldn't be read. Choose it again.",
    unavailable: "The Studio couldn't check the note right now. Try again.",
    network: "The Studio didn't answer. Check that it's still running, then try again.",
    uncertain: "The Studio didn't confirm the import, so it may or may not have happened. Check again to find out.",
  };
  var STALE_CAUSE = {
    expired: "Previews last 10 minutes.",
    page: "The target page was updated.",
    connection: "Live updates were interrupted, so a change may have been missed.",
    resync: "Live updates were resynchronised, so a change may have been missed.",
    restored: "This page was restored from the browser's history.",
    server: "The Studio no longer accepts this preview.",
  };
  var INVALID_REASON = {
    encoding: "it isn't UTF-8 text",
    too_large: "it's too large",
    bom: "it starts with a byte-order mark",
    json: "it isn't valid JSON",
    not_object: "it isn't a feedback note",
    keys: "it has missing or unexpected fields",
    format: "it isn't an okf-loom feedback note",
    version: "it uses a note version this Studio doesn't support",
    note_id: "its note ID is malformed",
    target: "its target is malformed",
    document: "its target isn't a page path this Studio accepts",
    source_sha256: "its page fingerprint is malformed",
    body: "its note text is empty, too long, or contains control characters",
    quote: "its quote is too long or contains control characters",
    claimed_author: "its name is too long or contains control characters",
    claimed_at: "its time isn't a valid timestamp",
    duplicate_key: "it repeats a field",
  };

  // ---- Configuration -----------------------------------------------------
  function readJson(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try { return JSON.parse(node.textContent || "null"); } catch (e) { return null; }
  }
  var config = readJson("okf-feedback-import-config") || {};
  var boot = readJson("okf-studio-bootstrap") || {};
  var TOKEN = typeof boot.token === "string" ? boot.token : "";
  var MAX_BYTES = Number.isSafeInteger(config.max_note_bytes) && config.max_note_bytes > 0 ? config.max_note_bytes : 32768;
  var TTL_MS = (Number.isSafeInteger(config.preview_ttl_seconds) && config.preview_ttl_seconds > 0
    ? config.preview_ttl_seconds : 600) * 1000;

  var fileInput = document.getElementById("okf-fbi-file");
  var result = document.getElementById("okf-fbi-result");
  var liveRegion = document.getElementById("okf-fbi-live");
  if (config.availability !== "available" || !fileInput || !result) return;

  // ---- State -------------------------------------------------------------
  var S = {
    state: "idle",
    generation: 0,
    file: null,         // {name, size}
    bytes: null,        // Uint8Array: exactly what preview and confirm send
    preview: null,      // last 200 preview payload (note echo, document, concept)
    previewToken: "",
    blocked: null,      // {kind, reason?, retry?}
    comment: null,      // imported / already-imported comment row
    replayed: false,
    staleCause: "",
    pendingStale: "",   // a trigger seen while a preview request was in flight
    // Concept ids of page events seen while a preview request was in flight.
    // The target is known only when the preview lands (a first preview has
    // none yet), so the ids are compared with the target it returns.
    pendingPageIds: null,
    controller: null,
    ttlTimer: 0,
  };
  var lastConn = null;

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (name) {
      var value = attrs[name];
      if (value === true) node.setAttribute(name, "");
      else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, String(value));
    });
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function announce(text) {
    if (!liveRegion) return;
    // Clear first so repeating the same message is announced again.
    liveRegion.textContent = "";
    window.setTimeout(function () { liveRegion.textContent = text; }, 30);
  }
  function formatSize(bytes) {
    if (bytes < 1024) return bytes + (bytes === 1 ? " byte" : " bytes");
    return (bytes / 1024).toFixed(1).replace(/\.0$/, "") + " KB";
  }
  function formatClaimedTime(value) {
    var date = new Date(value);
    if (isNaN(date.getTime())) return value;
    try { return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); }
    catch (e) { return date.toISOString(); }
  }
  function conceptHref(concept) {
    if (typeof concept !== "string" || !concept) return "";
    return "/" + concept.split("/").map(encodeURIComponent).join("/");
  }
  function targetConcept() { return S.preview && typeof S.preview.concept === "string" ? S.preview.concept : ""; }
  function targetDocument() {
    if (S.preview && typeof S.preview.document === "string") return S.preview.document;
    return "";
  }
  function clearTtl() { if (S.ttlTimer) { window.clearTimeout(S.ttlTimer); S.ttlTimer = 0; } }
  function abortInFlight() {
    if (S.controller) { try { S.controller.abort(); } catch (e) { /* already settled */ } }
    S.controller = null;
  }

  // ---- Transport ---------------------------------------------------------
  // Raw note bytes, the local Studio token, and a hard deadline. Returns
  // {status, data} or throws {kind: "network"} on transport failure/timeout.
  function send(url, bytes, previewToken) {
    abortInFlight();
    var controller = new AbortController();
    S.controller = controller;
    var timer = window.setTimeout(function () { controller.abort(); }, REQUEST_DEADLINE_MS);
    var headers = { "Content-Type": "application/json", "X-OKF-Token": TOKEN };
    if (previewToken) headers["X-OKF-Preview-Token"] = previewToken;
    return fetch(url, {
      method: "POST", headers: headers, body: bytes, credentials: "same-origin",
      cache: "no-store", signal: controller.signal,
    }).then(function (res) {
      return res.json().then(function (data) { return { status: res.status, data: data }; },
        function () { return { status: res.status, data: null }; });
    }).catch(function () {
      throw { kind: "network" };
    }).finally(function () {
      window.clearTimeout(timer);
      if (S.controller === controller) S.controller = null;
    });
  }
  function errorCode(data) {
    return data && data.error && typeof data.error === "object" && typeof data.error.code === "string"
      ? data.error.code : "";
  }
  function blockedFromError(status, data) {
    var code = errorCode(data);
    if (code === "invalid_note") {
      var fields = data.error.fields || {};
      return { kind: "invalid", reason: typeof fields.note === "string" ? fields.note : "" };
    }
    if (code === "note_too_large" || status === 413) return { kind: "too_large" };
    if (code === "local_only" || code === "read_only") return { kind: code };
    if (code === "forbidden" || status === 403) return { kind: "forbidden" };
    if (code === "revision_mismatch" || code === "target_missing") return { kind: code };
    if (code === "idempotency_conflict") return { kind: "conflict" };
    return { kind: "unavailable", retry: true };
  }

  // ---- Transitions -------------------------------------------------------
  function reset() {
    S.generation++;
    abortInFlight();
    clearTtl();
    S.state = "idle"; S.file = null; S.bytes = null; S.preview = null; S.previewToken = "";
    S.blocked = null; S.comment = null; S.replayed = false; S.staleCause = ""; S.pendingStale = "";
    S.pendingPageIds = null;
    render({});
  }

  function chooseFile(file) {
    if (!file) { reset(); return; }
    var generation = ++S.generation;
    abortInFlight();
    clearTtl();
    S.file = { name: file.name || "note", size: file.size };
    S.bytes = null; S.preview = null; S.previewToken = ""; S.blocked = null;
    S.comment = null; S.replayed = false; S.staleCause = ""; S.pendingStale = "";
    S.pendingPageIds = null;
    if (file.size > MAX_BYTES) {
      S.state = "blocked";
      S.blocked = { kind: "too_large" };
      render({ focus: "heading" });
      return;
    }
    S.state = "reading";
    render({});
    announce("Reading " + S.file.name + "…");
    file.arrayBuffer().then(function (buffer) {
      if (generation !== S.generation) return;
      S.bytes = new Uint8Array(buffer);
      runPreview(generation);
    }, function () {
      if (generation !== S.generation) return;
      S.state = "blocked";
      S.blocked = { kind: "unreadable" };
      render({ focus: "heading" });
    });
  }

  // The result of a preview always takes focus: the operator asked for it.
  function runPreview(generation) {
    if (!S.bytes || generation !== S.generation) return;
    clearTtl();
    S.state = "previewing";
    S.previewToken = ""; S.pendingStale = ""; S.staleCause = "";
    S.pendingPageIds = Object.create(null);
    render({ keepFocus: true });
    announce("Checking the note…");
    var started = performance.now();
    var connAtRequest = currentConn();
    send(PREVIEW_URL, S.bytes, "").then(function (res) {
      if (generation !== S.generation) return;
      var data = res.data;
      var seenIds = S.pendingPageIds;
      S.pendingPageIds = null;
      if (res.status !== 200 || !data || data.ok !== true || !data.note) {
        S.state = "blocked";
        S.blocked = blockedFromError(res.status, data);
        render({ focus: "heading" });
        return;
      }
      S.preview = data;
      S.comment = null;
      if (data.status === "ready" && typeof data.preview_token === "string" && data.preview_token) {
        // A trigger during the request (an event for the page it returned, a
        // resync, or a connection that moved while it was in flight) means
        // the answer may already be out of date. The page event is the most
        // specific cause, so it is the one reported.
        var moved = currentConn() !== connAtRequest && !(connAtRequest === "connecting" && currentConn() === "online");
        var pageChanged = !!(seenIds && typeof data.concept === "string" && data.concept && seenIds[data.concept]);
        if (pageChanged || S.pendingStale || moved) {
          S.state = "stale";
          S.staleCause = pageChanged ? "page" : S.pendingStale || "connection";
          S.pendingStale = "";
          render({ focus: "heading" });
          return;
        }
        S.state = "preview_ready";
        S.previewToken = data.preview_token;
        var remaining = Math.max(0, TTL_MS - (performance.now() - started) - TTL_MARGIN_MS);
        S.ttlTimer = window.setTimeout(function () { S.ttlTimer = 0; noteStale("expired"); }, remaining);
        render({ focus: "heading" });
        return;
      }
      S.state = "blocked";
      if (data.status === "already_imported") {
        S.comment = data.comment && typeof data.comment === "object" ? data.comment : null;
        S.blocked = { kind: "already_imported" };
      } else if (data.status === "conflict" || data.status === "revision_mismatch" || data.status === "target_missing") {
        S.blocked = { kind: data.status };
      } else {
        S.blocked = { kind: "unavailable", retry: true };
      }
      render({ focus: "heading" });
    }, function () {
      if (generation !== S.generation) return;
      S.pendingPageIds = null;
      S.state = "blocked";
      S.blocked = { kind: "network", retry: true };
      render({ focus: "heading" });
    });
  }

  function confirmImport() {
    if (S.state !== "preview_ready" || !S.bytes || !S.previewToken) return;
    var generation = S.generation;
    var token = S.previewToken;
    clearTtl();
    S.state = "importing";
    render({ keepFocus: true });
    announce("Importing the note…");
    send(CONFIRM_URL, S.bytes, token).then(function (res) {
      if (generation !== S.generation) return;
      var data = res.data;
      if ((res.status === 201 || res.status === 200) && data && data.ok === true && data.comment) {
        S.state = "imported";
        S.comment = data.comment;
        S.replayed = data.replayed === true;
        S.previewToken = "";
        render({ focus: "heading" });
        return;
      }
      S.previewToken = "";
      if (errorCode(data) === "stale_preview") {
        S.state = "stale";
        S.staleCause = "server";
        render({ focus: "heading" });
        return;
      }
      S.state = "blocked";
      S.blocked = blockedFromError(res.status, data);
      if (S.blocked.kind === "unavailable") S.blocked = { kind: "uncertain", retry: true };
      render({ focus: "heading" });
    }, function () {
      if (generation !== S.generation) return;
      // The request may have reached the server: never offer a blind retry.
      // Checking again previews the same bytes, which reports
      // already_imported when the comment exists.
      S.previewToken = "";
      S.state = "blocked";
      S.blocked = { kind: "uncertain", retry: true };
      render({ focus: "heading" });
    });
  }

  function noteStale(cause) {
    if (S.state === "previewing") { if (!S.pendingStale) S.pendingStale = cause; return; }
    if (S.state !== "preview_ready") return;
    clearTtl();
    S.state = "stale";
    S.previewToken = "";
    S.staleCause = cause;
    // Background change: announce, and only move focus if the control the
    // user was on is being replaced.
    render({ keepFocus: true, fallbackFocus: "reprieve" });
    announce(COPY.stale + " " + (STALE_CAUSE[cause] || ""));
  }

  // ---- Live staleness (live.js is a deferred module; attach when ready) ---
  function currentConn() {
    return window.okfLoomLive && typeof window.okfLoomLive.state === "string" ? window.okfLoomLive.state : null;
  }
  var liveAttached = false;
  function attachLive() {
    var live = window.okfLoomLive;
    if (liveAttached || !live || typeof live.on !== "function") return;
    liveAttached = true;
    lastConn = currentConn();
    ["changed", "created", "removed"].forEach(function (type) {
      live.on(type, function (d) {
        var ids = d && Array.isArray(d.ids) ? d.ids : [];
        if (S.state === "previewing" && S.pendingPageIds) {
          ids.forEach(function (id) { if (typeof id === "string") S.pendingPageIds[id] = true; });
          return;
        }
        var concept = targetConcept();
        if (concept && ids.indexOf(concept) >= 0) noteStale("page");
      });
    });
    live.on("resync", function () { noteStale("resync"); });
    live.on("conn", function (d) {
      var next = d && typeof d.state === "string" ? d.state : null;
      var prev = lastConn;
      lastConn = next;
      if (next === prev) return;
      // The first connection completing is not a gap in what we observed.
      if (prev === "connecting" && next === "online") return;
      if (prev === null && next === "online") return;
      noteStale("connection");
    });
  }
  attachLive();
  document.addEventListener("DOMContentLoaded", attachLive);
  window.addEventListener("load", attachLive);
  window.addEventListener("pageshow", function (event) {
    attachLive();
    if (event.persisted) noteStale("restored");
  });

  // ---- Rendering ---------------------------------------------------------
  function headingText() {
    switch (S.state) {
      case "reading": return "Reading the file…";
      case "previewing": return "Checking the note…";
      case "preview_ready": return "Ready to import";
      case "importing": return "Importing…";
      case "imported": return "Imported";
      case "stale": return "Preview out of date";
      case "blocked": {
        var kind = S.blocked && S.blocked.kind;
        if (kind === "already_imported") return "Nothing to import";
        if (kind === "local_only" || kind === "read_only" || kind === "forbidden") return "Import unavailable";
        if (kind === "network" || kind === "unavailable") return "Couldn't check the note";
        if (kind === "uncertain") return "Import not confirmed";
        return "Can't import this note";
      }
      default: return "";
    }
  }

  function strongText(parent, parts) {
    parts.forEach(function (part) {
      if (typeof part === "string") parent.appendChild(document.createTextNode(part));
      else parent.appendChild(el(part.tag, part.attrs || {}, part.text));
    });
    return parent;
  }

  function messageNode() {
    var doc = targetDocument();
    if (S.state === "blocked") {
      var kind = S.blocked.kind;
      if (kind === "invalid") {
        var reason = INVALID_REASON[S.blocked.reason];
        return el("p", { "class": "okf-fbi__notice okf-fbi__notice--error", "data-fbi-message": kind },
          "This file isn't a valid feedback note" + (reason ? ": " + reason + "." : "."));
      }
      var tone = kind === "already_imported" ? "info" : "error";
      var notice = el("p", { "class": "okf-fbi__notice okf-fbi__notice--" + tone, "data-fbi-message": kind });
      // A command-line flag is rendered as one unbreakable code token.
      var parts = (COPY[kind] || COPY.unavailable).split("--no-edit");
      parts.forEach(function (part, index) {
        if (index) notice.appendChild(el("code", { "class": "okf-fbi__flag" }, "--no-edit"));
        if (part) notice.appendChild(document.createTextNode(part));
      });
      return notice;
    }
    if (S.state === "stale") {
      var stale = el("div", { "class": "okf-fbi__notice okf-fbi__notice--warn", "data-fbi-message": "stale" });
      stale.appendChild(el("p", {}, COPY.stale));
      if (STALE_CAUSE[S.staleCause]) stale.appendChild(el("p", { "class": "okf-fbi__cause" }, STALE_CAUSE[S.staleCause]));
      return stale;
    }
    if (S.state === "imported") {
      var done = el("div", { "class": "okf-fbi__notice okf-fbi__notice--ok", "data-fbi-message": "imported" });
      strongText(done.appendChild(el("p")), ["The note is now an open comment on ", { tag: "code", text: doc }, "."]);
      if (S.replayed) done.appendChild(el("p", { "class": "okf-fbi__cause" }, "It had already been imported, so no second comment was created."));
      return done;
    }
    if (S.state === "preview_ready" || S.state === "importing") {
      return el("p", { "class": "okf-fbi__match" }, "The note matches the current source of its page.");
    }
    return null;
  }

  function factsNode() {
    var echo = S.preview && S.preview.note;
    if (!echo || typeof echo !== "object") return null;
    var wrap = el("div", { "class": "okf-fbi__note" });
    var facts = el("dl", { "class": "okf-fbi__facts" });
    function row(label, valueNode) {
      facts.appendChild(el("dt", {}, label));
      var dd = el("dd");
      dd.appendChild(valueNode);
      facts.appendChild(dd);
    }
    row("Page", el("code", {}, targetDocument()));
    if (S.file) row("File", document.createTextNode(S.file.name + " · " + formatSize(S.file.size)));
    if (typeof echo.note_id === "string") {
      var noteId = el("code", { title: echo.note_id }, echo.note_id.slice(0, 12) + "…");
      row("Note ID", noteId);
    }
    if (typeof echo.claimed_author === "string" && echo.claimed_author) {
      var who = el("span");
      who.appendChild(document.createTextNode(echo.claimed_author + " "));
      who.appendChild(el("span", { "class": "okf-fbi__claim" }, "unverified"));
      row("Name", who);
    }
    if (typeof echo.claimed_at === "string" && echo.claimed_at) {
      var when = el("span");
      when.appendChild(el("time", { datetime: echo.claimed_at }, formatClaimedTime(echo.claimed_at)));
      when.appendChild(document.createTextNode(" "));
      when.appendChild(el("span", { "class": "okf-fbi__claim" }, "unverified"));
      row("Time", when);
    }
    wrap.appendChild(facts);
    wrap.appendChild(el("h3", { "class": "okf-fbi__subhead" }, "Note"));
    wrap.appendChild(el("div", { "class": "okf-fbi__body" }, typeof echo.body === "string" ? echo.body : ""));
    if (typeof echo.quote === "string" && echo.quote) {
      wrap.appendChild(el("h3", { "class": "okf-fbi__subhead" }, "Quote in the note (unverified)"));
      wrap.appendChild(el("blockquote", { "class": "okf-fbi__quote" }, echo.quote));
    }
    // Import preview disclosure: always shown with a previewed note.
    var origin = el("p", { "class": "okf-fbi__notice okf-fbi__notice--info", "data-fbi-disclosure": "origin" });
    strongText(origin, ["Origin is ", { tag: "strong", text: "unauthenticated" },
      ": this file could have been written or edited by anyone. Byte-identical copies of this page are "
      + "indistinguishable, so we can't tell which copy it came from. Name and time are unverified claims."]);
    wrap.appendChild(origin);
    return wrap;
  }

  function confirmDisclosure() {
    var node = el("p", { "class": "okf-fbi__notice okf-fbi__notice--warn", "data-fbi-disclosure": "confirm" });
    return strongText(node, ["Importing creates an ordinary ", { tag: "strong", text: "open comment" }, " on ",
      { tag: "code", text: targetDocument() }, ", recorded as you (the local Studio user). It is visible to every "
      + "reader and agent of this bundle and ", { tag: "strong", text: "may trigger agent work" }, "."
      + " It is a page comment: any quote is shown with it but is not highlighted or pinned to exact text."]);
  }

  function button(label, key, handler, opts) {
    opts = opts || {};
    var node = el("button", {
      type: "button",
      "class": "okf-btn" + (opts.primary ? " okf-btn--primary" : ""),
      "data-fbi-key": key,
      "aria-disabled": opts.busy ? "true" : null,
    }, label);
    node.addEventListener("click", function () {
      if (node.getAttribute("aria-disabled") === "true") return;
      handler();
    });
    return node;
  }
  function chooseAnother() {
    fileInput.focus();
    try { fileInput.click(); } catch (e) { /* focus is enough */ }
  }
  function revealComment() {
    var studio = window.okfLoomStudio;
    var id = S.comment && S.comment.id;
    if (!id || !studio || typeof studio.revealComment !== "function") return;
    studio.revealComment(String(id));
  }

  function actionsNode() {
    var row = el("div", { "class": "okf-fbi__actions" });
    var kind = S.blocked && S.blocked.kind;
    var href = conceptHref(targetConcept());
    var studioReady = !!(window.okfLoomStudio && typeof window.okfLoomStudio.revealComment === "function");
    function pageLink() {
      if (!href) return;
      row.appendChild(el("a", { "class": "okf-btn", href: href, "data-fbi-key": "page" }, "Open " + targetDocument()));
    }
    if (S.state === "preview_ready" || S.state === "importing") {
      var busy = S.state === "importing";
      row.appendChild(button(busy ? "Importing…" : "Import as comment", "import", confirmImport, { primary: true, busy: busy }));
      if (!busy) row.appendChild(button("Choose another file", "choose", chooseAnother));
    } else if (S.state === "stale") {
      row.appendChild(button("Preview again", "reprieve", function () { runPreview(S.generation); }, { primary: true }));
      row.appendChild(button("Choose another file", "choose", chooseAnother));
    } else if (S.state === "imported" || kind === "already_imported") {
      if (studioReady && S.comment && S.comment.id) row.appendChild(button("Show the comment", "reveal", revealComment, { primary: true }));
      pageLink();
      row.appendChild(button(S.state === "imported" ? "Import another note" : "Choose another file",
        "choose", S.state === "imported" ? function () { fileInput.value = ""; reset(); fileInput.focus(); } : chooseAnother));
    } else if (S.state === "blocked") {
      if (kind === "uncertain") row.appendChild(button("Check again", "retry", function () { runPreview(S.generation); }, { primary: true }));
      else if (S.blocked.retry && S.bytes) row.appendChild(button("Try again", "retry", function () { runPreview(S.generation); }, { primary: true }));
      if (kind === "forbidden") row.appendChild(button("Reload page", "reload", function () { window.location.reload(); }, { primary: true }));
      if (kind === "revision_mismatch") pageLink();
      if (kind !== "forbidden" && kind !== "local_only" && kind !== "read_only") row.appendChild(button("Choose another file", "choose", chooseAnother));
    } else {
      return null;
    }
    return row.childNodes.length ? row : null;
  }

  function render(opts) {
    opts = opts || {};
    var active = document.activeElement;
    var activeKey = active && result.contains(active) && active.getAttribute ? active.getAttribute("data-fbi-key") : null;
    var focusWasInside = !!(active && result.contains(active));
    root.dataset.state = S.state;
    if (S.state === "blocked" && S.blocked) root.dataset.blocked = S.blocked.kind;
    else delete root.dataset.blocked;
    fileInput.disabled = S.state === "importing";

    result.textContent = "";
    if (S.state === "idle") {
      result.hidden = true;
      result.removeAttribute("aria-busy");
      if (focusWasInside) fileInput.focus();
      return;
    }
    result.hidden = false;
    var busy = S.state === "reading" || S.state === "previewing" || S.state === "importing";
    if (busy) result.setAttribute("aria-busy", "true"); else result.removeAttribute("aria-busy");

    var heading = el("h2", { id: "okf-fbi-result-title", "class": "okf-fbi__panel-title", tabindex: "-1" }, headingText());
    result.appendChild(heading);
    if ((S.state === "reading" || S.state === "previewing") && S.file) {
      result.appendChild(el("p", { "class": "okf-fbi__hint" }, S.file.name + " · " + formatSize(S.file.size)));
    }
    var message = messageNode();
    if (message) result.appendChild(message);
    if (S.state !== "reading" && S.state !== "previewing") {
      var facts = factsNode();
      if (facts) result.appendChild(facts);
    }
    if (S.state === "preview_ready" || S.state === "importing") result.appendChild(confirmDisclosure());
    var actions = actionsNode();
    if (actions) result.appendChild(actions);

    var target = null;
    if (opts.focus === "heading") target = heading;
    else if (opts.keepFocus && activeKey) target = result.querySelector('[data-fbi-key="' + activeKey + '"]');
    if (!target && focusWasInside) {
      target = (opts.fallbackFocus && result.querySelector('[data-fbi-key="' + opts.fallbackFocus + '"]')) || heading;
    }
    if (target) {
      try { target.focus({ preventScroll: target !== heading }); } catch (e) { target.focus(); }
      if (target === heading && typeof heading.scrollIntoView === "function") {
        var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        heading.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
      }
    }
  }

  // ---- Wiring ------------------------------------------------------------
  fileInput.addEventListener("change", function () {
    var file = fileInput.files && fileInput.files[0];
    chooseFile(file || null);
  });
  // A file restored by the browser (back/forward form restoration) is a
  // choice the operator did not just make: start clean instead of previewing.
  if (fileInput.files && fileInput.files.length) fileInput.value = "";
  render({});
})();
